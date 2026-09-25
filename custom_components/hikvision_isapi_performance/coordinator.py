"""DataUpdateCoordinator for the Hikvision ISAPI integration.

Polls the device's ``/ISAPI/System/deviceInfo`` and
``/ISAPI/System/status`` endpoints on a configurable interval. Each
refresh builds the data dict that the sensor / camera / switch /
button platforms read.

The data shape is::

    {
        "deviceInfo": {
            "deviceName": "...",
            "deviceID": "...",
            "model": "DS-...",
            "serialNumber": "...",
            "firmwareVersion": "V5.x.x",
            "firmwareReleasedDate": "...",
            "deviceType": "...",
            "manufacturer": "Hikvision",
        },
        "systemStatus": {
            "deviceStatus": "OK" | "...",
            "cpuUsage": "27",       # percent as string
            "memoryUsage": "30",    # percent as string
            "uptime": "12345",      # seconds as string
        },
        "channels": [
            {"id": "1", "name": "Camera 1", "online": True, "recording": True},
            ...
        ],
        "capabilities": {
            "ptz": True | False,
        },
    }
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEVICE_TYPE_DVR,
    DEVICE_TYPE_IPCAMERA,
    DEVICE_TYPE_NETWORK_VIDEO_RECORDER,
    DOMAIN,
    ISAPI_CONTENT_MGMT_STORAGE,
    ISAPI_INPUT_PROXY_CHANNELS,
    ISAPI_INPUT_PROXY_CHANNELS_STATUS,
    ISAPI_STREAMING_CHANNELS,
    ISAPI_STREAMING_CHANNELS_STATUS,
    ISAPI_SYSTEM_DEVICE_INFO,
    ISAPI_SYSTEM_NETWORK_INTERFACES,
    ISAPI_SYSTEM_STATUS,
    ISAPI_SYSTEM_STORAGE_HARDDISKS,
)
from .isapi_client import ISAPIAuthError, ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)


def entry_id_hint(coordinator: "HikvisionISAPICoordinator") -> str:
    """Short human-readable label for log lines.

    We deliberately don't include credentials or full entry IDs in
    INFO logs (they'd leak into HA's persistent log file). The
    entry ID prefix is enough to disambiguate N devices on the same
    HA host.
    """
    return f"entry_id={getattr(coordinator, 'entry_id', '?')[:8]}"


def _xml_text(element: ET.Element | None, *path: str) -> str | None:
    """Return the text of an XML element at ``path`` (relative)."""
    if element is None:
        return None
    for tag in path:
        element = element.find(tag)
        if element is None:
            return None
    return element.text


def _parse_device_info(root: ET.Element | None) -> dict[str, str]:
    """Parse ``/ISAPI/System/deviceInfo`` response."""
    if root is None:
        return {}
    return {
        "deviceName": _xml_text(root, "deviceName") or "",
        "deviceID": _xml_text(root, "deviceID") or "",
        "model": _xml_text(root, "model") or "",
        "serialNumber": _xml_text(root, "serialNumber") or "",
        "firmwareVersion": _xml_text(root, "firmwareVersion") or "",
        "firmwareReleasedDate": _xml_text(root, "firmwareReleasedDate") or "",
        "deviceType": _xml_text(root, "deviceType") or "",
        "macAddress": _xml_text(root, "macAddress") or "",
        "manufacturer": "Hikvision",
    }


def normalize_device_type(raw: str) -> str:
    """Map ``deviceInfo.deviceType`` to one of the canonical DEVICE_TYPE_*.

    Hikvision's ``deviceType`` field is free-form text — common
    values are ``IPCamera``, ``NetworkVideoRecorder``, ``DVR``,
    ``ipc``, ``nvr``, ``dvr`` (any case). The path selection in
    ``camera.py`` depends on the canonical IPCamera / NVR / DVR
    mapping; everything else (unknown / empty) defaults to IPCamera
    since the user fleet is mostly IPC.
    """
    s = (raw or "").strip().lower()
    if s in {"nvr", "networkvideorecorder"}:
        return DEVICE_TYPE_NETWORK_VIDEO_RECORDER
    if s in {"dvr", "digitalvideorecorder"}:
        return DEVICE_TYPE_DVR
    # Default to IPC for "ipcamera", "ipc", "" or anything unknown.
    return DEVICE_TYPE_IPCAMERA


def _parse_system_status(root: ET.Element | None) -> dict[str, str]:
    """Parse ``/ISAPI/System/status`` response.

    Hikvision's response is::

        <DeviceStatus version="2.0" ...>
          <currentDeviceTime>2026-09-24T...</currentDeviceTime>
          <deviceUpTime>12345</deviceUpTime>
          <CPUList>
            <CPU>
              <cpuDescription>ARMv7 ...</cpuDescription>
              <cpuUtilization>27</cpuUtilization>   (%)
            </CPU>
          </CPUList>
          <MemoryList>
            <Memory>
              <memoryDescription>DDR Memory</memoryDescription>
              <memoryUsage>1234</memoryUsage>         (KB on IPC, MB on NVR)
              <memoryAvailable>5678</memoryAvailable>
            </Memory>
          </MemoryList>
          <totalRebootCount>40</totalRebootCount>     (optional — IPC only)
        </DeviceStatus>

    Older / future firmwares sometimes use a flat ``<SystemStatus>``
    schema (``<CPUUsage>`` / ``<memoryUsage>`` as direct children).
    We accept both.
    """
    if root is None:
        return {
            "deviceStatus": "Unknown",
            "cpuUtilization": "0",
            "memoryUsage": "0",
            "memoryAvailable": "0",
            "uptime": "0",
            "rebootCount": None,
            "cpuDescription": None,
        }
    # New schema (V5.x) — nested CPUList / MemoryList.
    cpu = root.find(".//CPU")
    memory = root.find(".//Memory")
    cpu_util = _xml_text(cpu, "cpuUtilization") if cpu is not None else None
    if cpu_util is None:
        # Flat schema fallback
        cpu_util = _xml_text(root, "CPUUsage")
    mem_usage = _xml_text(memory, "memoryUsage") if memory is not None else None
    if mem_usage is None:
        mem_usage = _xml_text(root, "memoryUsage")
    mem_avail = _xml_text(memory, "memoryAvailable") if memory is not None else None
    if mem_avail is None:
        mem_avail = _xml_text(root, "memoryFree")

    return {
        "deviceStatus": (
            _xml_text(root, "deviceStatus") or "Unknown"
        ),
        "cpuUtilization": cpu_util or "0",
        # v0.6.16: V4 NVR firmware reports memoryUsage as a decimal
        # MB count (``"728.234375"``) with optional leading whitespace,
        # V5 reports as integer (``"61"``). Use ``_safe_int_mb`` to
        # accept both — pre-v0.6.16 this returned the raw text which
        # sensors downstream could not coerce to a number.
        "memoryUsage": str(_safe_int_mb(mem_usage) or 0),
        "memoryAvailable": str(_safe_int_mb(mem_avail) or 0),
        "uptime": _xml_text(root, "deviceUpTime") or _xml_text(root, "uptime") or "0",
        "rebootCount": _xml_text(root, "totalRebootCount"),
        "cpuDescription": _xml_text(cpu, "cpuDescription") if cpu is not None else None,
    }


def _parse_channels(root: ET.Element | None) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/ContentMgmt/InputProxy/channels`` response.

    Hikvision's response is::

        <InputProxyChannelList>
          <InputProxyChannel>
            <id>1</id>
            <name>Camera 1</name>
            <online>true</online>
            ...
          </InputProxyChannel>
        </InputProxyChannelList>
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for ch in root.findall(".//InputProxyChannel"):
        ch_id = _xml_text(ch, "id") or ""
        if not ch_id:
            continue
        out.append(
            {
                "id": ch_id,
                "name": _xml_text(ch, "name") or f"Channel {ch_id}",
                "online": (_xml_text(ch, "online") or "").lower() == "true",
                "recording": (_xml_text(ch, "recordStatus") or "").lower()
                == "recording",
            }
        )
    return out


def _parse_streaming_channels_list(
    root: ET.Element | None,
) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/Streaming/channels`` response (IPC-side).

    Hikvision's response shape is::

        <StreamingChannelList>
          <StreamingChannel>
            <id>1</id>
            <videoInputChannelID>1</videoInputChannelID>
            <name>Camera 1</name>            (optional)
            <online>true</online>           (optional)
            ...
          </StreamingChannel>
        </StreamingChannelList>

    Unlike the InputProxy list, this response shape doesn't always
    include ``recordStatus`` or ``online`` — those come from the
    per-channel status endpoint instead.
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for ch in root.findall(".//StreamingChannel"):
        ch_id = (
            _xml_text(ch, "id")
            or _xml_text(ch, "videoInputChannelID")
            or ""
        )
        if not ch_id:
            continue
        out.append(
            {
                "id": ch_id,
                "name": _xml_text(ch, "name") or f"Channel {ch_id}",
                "online": (_xml_text(ch, "online") or "").lower() == "true",
                "recording": (_xml_text(ch, "recordStatus") or "").lower()
                == "recording",
            }
        )
    return out


def _parse_storage(root: ET.Element | None) -> dict[str, Any]:
    """Parse ``/ISAPI/ContentMgmt/storage`` (V5) or
    ``/ISAPI/System/Storage/hardDisks`` (V4 NVR fallback).

    Two Hikvision shapes are accepted.

    **V5 (newer firmware)** — direct fields::

        <Storage xmlns="...">
          <totalCapacity>2000000</totalCapacity>          (MB)
          <usedCapacity>1234567</usedCapacity>           (MB)
          <freeCapacity>765433</freeCapacity>            (MB)
          <status>normal</status>
        </Storage>

    **V4.x firmware** (e.g. ``DS-7708N-I4`` V4.1.18)::

        <Storage xmlns="...">
          <hddList>
            <HDD>
              <id>1</id>
              <size>2000396746752</size>           (bytes)
              <freeSize>1234567890123</freeSize>   (bytes)
              <status>normal</status>
            </HDD>
            <HDD>
              <id>2</id>
              <size>4000796746752</size>
              <freeSize>...</freeSize>
              <status>normal</status>
            </HDD>
          </hddList>
        </Storage>

    V4 returns total/used/free in *bytes*, per-HDD. We sum across
    all HDDs and convert to MB (1 MB = 1 000 000 bytes, matching
    V5's MB convention; 1 GiB = 1024³ would give slightly different
    numbers, but consistency with V5 wins here).
    """
    empty = {
        "total_mb": None,
        "used_mb": None,
        "free_mb": None,
        "status": "unknown",
    }
    if root is None:
        return empty

    # V5 path: direct fields.
    total_mb = _safe_int_mb(_xml_text(root, "totalCapacity"))
    used_mb = _safe_int_mb(_xml_text(root, "usedCapacity"))
    free_mb = _safe_int_mb(_xml_text(root, "freeCapacity"))
    status = _xml_text(root, "status")

    if total_mb is not None or used_mb is not None or free_mb is not None:
        # V5 shape — done.
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "free_mb": free_mb,
            "status": status or "unknown",
        }

    # V4 path: sum over <hddList><HDD><size>/<freeSize>.
    hdds = root.findall(".//HDD")
    if not hdds:
        return empty

    total_bytes = 0
    free_bytes = 0
    status_aggregate = "normal"
    seen = False
    for hdd in hdds:
        size_raw = _safe_int_mb(_xml_text(hdd, "size"))
        free_raw = _safe_int_mb(_xml_text(hdd, "freeSize"))
        if size_raw is None and free_raw is None:
            continue
        seen = True
        if size_raw is not None:
            total_bytes += size_raw
        if free_raw is not None:
            free_bytes += free_raw
        hdd_status = _xml_text(hdd, "status")
        if hdd_status and hdd_status != "normal":
            status_aggregate = "exception"

    if not seen:
        return empty

    return {
        # Convert bytes → MB using decimal (1 MB = 10^6 bytes) so
        # the value matches V5's MB convention.
        "total_mb": round(total_bytes / 1_000_000, 1) if total_bytes else None,
        "used_mb": (
            round((total_bytes - free_bytes) / 1_000_000, 1)
            if total_bytes and free_bytes
            else None
        ),
        "free_mb": round(free_bytes / 1_000_000, 1) if free_bytes else None,
        "status": status_aggregate,
    }


def _parse_network_interfaces(
    root: ET.Element | None,
) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/System/Network/interfaces`` response.

    Two shapes are accepted.

    **V5 firmware** (most IPCs / NVRs)::

        <NetworkInterface>
          <IPAddress>192.168.1.10</IPAddress>
          <subnetMask>255.255.255.0</subnetMask>
          <DefaultGateway>192.168.1.1</DefaultGateway>
          ...
        </NetworkInterface>

    **V4 firmware** (e.g. ``DS-7708N-I4`` V4.1.18) — the IP fields
    are nested inside another ``<IPAddress>`` element::

        <NetworkInterface>
          <IPAddress>
            <ipVersion>dual</ipVersion>
            <addressingType>static</addressingType>
            <ipAddress>10.18.176.65</ipAddress>
            <subnetMask>255.255.255.0</subnetMask>
            <DefaultGateway>
              <ipAddress>10.18.176.1</ipAddress>
            </DefaultGateway>
          </IPAddress>
          ...
        </NetworkInterface>

    ``_ip_field`` tries the V4 nested path first, then falls back
    to the V5 direct path — so both shapes return the real
    address.
    """
    if root is None:
        return []

    def _ip_field(iface: ET.Element, tag: str) -> str:
        """Read an IP-like field by its leaf tag name. Supports
        both V5 direct schema and V4 NVR nested schema.

        For ``tag="ipAddress"``:
        - V4 NVR: ``<IPAddress><ipAddress>10.x.x.x</ipAddress></IPAddress>``
        - V5: ``<IPAddress>10.x.x.x</IPAddress>`` (the uppercase
          ``<IPAddress>`` element's TEXT directly).

        For ``tag="subnetMask"``:
        - V4 NVR: ``<IPAddress><subnetMask>...</subnetMask></IPAddress>``
        - V5: ``<subnetMask>...</subnetMask>`` (direct child of
          ``<NetworkInterface>``).

        For ``tag="DefaultGateway"``:
        - V4 NVR: ``<IPAddress><DefaultGateway><ipAddress>...</ipAddress></DefaultGateway></IPAddress>``
        - V5: ``<DefaultGateway>...</DefaultGateway>`` (direct text).
        """
        # For ipAddress: try nested IPAddress/ipAddress (V4), then
        # direct IPAddress text (V5). Note: V5 uses <IPAddress>
        # (uppercase) as direct text, but findtext("IPAddress")
        # returns the text 192.168.1.10 — confirmed in debug.
        if tag == "ipAddress":
            nested = iface.find("IPAddress/ipAddress")
            if nested is not None and nested.text:
                return nested.text.strip()
            return (iface.findtext("IPAddress") or "").strip()
        # For subnetMask: try nested, then direct.
        if tag == "subnetMask":
            nested = iface.find("IPAddress/subnetMask")
            if nested is not None and nested.text:
                return nested.text.strip()
            return (iface.findtext("subnetMask") or "").strip()
        # For DefaultGateway: try nested (V4 has 2-level), then direct.
        if tag == "DefaultGateway":
            for path in (
                "IPAddress/DefaultGateway/ipAddress",
                "IPAddress/DefaultGateway",
                "DefaultGateway",
            ):
                found = iface.find(path)
                if found is not None and found.text:
                    return found.text.strip()
            return ""
        # Generic fallback: try nested form, then direct.
        nested = iface.find(f"IPAddress/{tag}")
        if nested is not None and nested.text:
            return nested.text.strip()
        return (iface.findtext(tag) or "").strip()

    out: list[dict[str, Any]] = []
    for iface in root.findall(".//NetworkInterface"):
        out.append(
            {
                "id": _xml_text(iface, "id") or "",
                "name": _xml_text(iface, "interfaceName") or "",
                "ip_address": _ip_field(iface, "ipAddress"),
                "subnet_mask": _ip_field(iface, "subnetMask"),
                "default_gateway": _ip_field(iface, "DefaultGateway"),
                "mtu": _safe_int_mb(_xml_text(iface, "MTU")),
                "mac_address": _xml_text(iface, "MACAddress") or "",
            }
        )
    return out


def _parse_streaming_channels(
    root: ET.Element | None,
) -> dict[str, int]:
    """Parse ``/ISAPI/Streaming/channels`` for per-channel bitrate.

    Returns ``{channel_id: bitrate_kbps}``. Bitrate may be reported as
    ``videoAverageBitrate`` (kbps) or absent (older firmware); we
    silently skip channels without a bitrate field.
    """
    if root is None:
        return {}
    out: dict[str, int] = {}
    for ch in root.findall(".//StreamingChannel"):
        ch_id = _xml_text(ch, "id") or ""
        if not ch_id:
            continue
        bitrate = _safe_int_mb(
            _xml_text(ch, "videoAverageBitrate")
        )
        if bitrate is None:
            bitrate = _safe_int_mb(_xml_text(ch, "maxBitrate"))
        if bitrate is not None:
            out[ch_id] = bitrate
    return out


def _parse_channel_status(
    root: ET.Element | None,
) -> dict[str, bool]:
    """Parse ``/ISAPI/ContentMgmt/InputProxy/channels/<id>/status``.

    Hikvision's response shape is::

        <InputProxyChannelStatus>
          <online>true|false</online>
          <recordStatus>recording|idle</recordStatus>
          <signalLost>true|false</signalLost>     (optional — signal state)
          <motionDetection>true|false</motionDetection>  (optional)
        </InputProxyChannelStatus>

    We return a dict with three booleans; missing fields default to
    False (we don't know the channel is offline just because the
    firmware didn't report the field).
    """
    if root is None:
        return {
            "online": False,
            "recording": False,
            "motion_detected": False,
        }
    return {
        "online": (_xml_text(root, "online") or "").lower() == "true",
        "recording": (_xml_text(root, "recordStatus") or "").lower()
        == "recording",
        "motion_detected": (
            _xml_text(root, "motionDetection") or ""
        ).lower() == "true",
    }


def _parse_channel_status_extended(
    root: ET.Element | None,
) -> dict[str, Any]:
    """Extended per-channel status (v0.5.0).

    Same schema as ``_parse_channel_status`` but reads additional
    fields useful for IPC health monitoring:

    - ``uptime`` (seconds) — device uptime, same value as
      ``/ISAPI/System/status``'s ``deviceUpTime``.
    - ``reboot_count`` — total device reboots.
    - ``sd_card_writes`` — ``SDCardStatusInfo/videoRewritingTimes`` for
      IPCs with SD cards; the most actionable health indicator
      (SD cards typically last ~3,000-5,000 rewrite cycles before
      failing).
    - ``camera_run_total_time`` — ``Camera/cameraRunTotalTime`` for
      PTZ IPCs.
    - ``dome_heat_state`` / ``dome_fan_state`` — ``DomeInfo/heatState``
      / ``DomeInfo/fanState`` (0=ok, 1=running/active).
    - ``dome_runtime_over_40`` — ``DomeInfo/runtimeOverPositiveforty``
      (cumulative seconds operating above 40°C; high = thermal
      stress).
    """
    if root is None:
        return {
            "online": False,
            "recording": False,
            "motion_detected": False,
            "uptime": None,
            "reboot_count": None,
            "sd_card_writes": None,
            "camera_run_total_time": None,
            "dome_heat_state": None,
            "dome_fan_state": None,
            "dome_runtime_over_40": None,
        }
    dome = root.find(".//DomeInfo")
    camera = root.find(".//Camera")
    sdcard = root.find(".//SDCardStatusInfo")
    return {
        "online": (_xml_text(root, "online") or "").lower() == "true",
        "recording": (
            (_xml_text(root, "recordStatus") or "").lower() == "recording"
        ),
        "motion_detected": (
            _xml_text(root, "motionDetection") or ""
        ).lower() == "true",
        "uptime": _xml_text(root, "deviceUpTime") or _xml_text(root, "uptime"),
        "reboot_count": _xml_text(root, "totalRebootCount"),
        "sd_card_writes": (
            _xml_text(sdcard, "videoRewritingTimes") if sdcard is not None else None
        ),
        "camera_run_total_time": (
            _xml_text(camera, "cameraRunTotalTime") if camera is not None else None
        ),
        "dome_heat_state": (
            _xml_text(dome, "heatState") if dome is not None else None
        ),
        "dome_fan_state": (
            _xml_text(dome, "fanState") if dome is not None else None
        ),
        "dome_runtime_over_40": (
            _xml_text(dome, "runtimeOverPositiveforty") if dome is not None else None
        ),
    }


def _safe_int_mb(value: Any) -> int | None:
    """Best-effort numeric parsing for ISAPI capacity fields.

    Accepts both integer strings (``"61"`` — V5 firmware reports
    memoryUsage as an integer MB count) and decimal strings
    (``"728.234375"`` — V4 NVR firmware reports decimal MB with
    whitespace prefix). Returns ``int(round(...))`` so callers can
    safely treat the result as an integer MB value while we lose
    no precision in the round-trip.

    Pre-v0.6.16 this used ``int()`` directly, which rejected V4
    firmware's decimal fields with ``ValueError`` → ``None``
    → memory sensors showed "Unknown" on every V4 NVR despite
    the device returning perfectly valid data.
    """
    if value is None:
        return None
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


class HikvisionISAPIData:
    """Container for one coordinator refresh result."""

    def __init__(
        self,
        device_info: dict[str, str],
        system_status: dict[str, str],
        channels: list[dict[str, Any]],
        capabilities: dict[str, bool],
        storage: dict[str, Any] | None = None,
        network_interfaces: list[dict[str, Any]] | None = None,
        streaming_bitrate_kbps: dict[str, int] | None = None,
    ) -> None:
        self.device_info = device_info
        # Normalized device type: "ipcamera" / "networkvideorecorder" /
        # "dvr". Used by camera.py to route the snapshot endpoint.
        self.device_type: str = normalize_device_type(
            device_info.get("deviceType", "")
        )
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities
        self.storage = storage or {}
        self.network_interfaces = network_interfaces or []
        self.streaming_bitrate_kbps = streaming_bitrate_kbps or {}


class HikvisionISAPICoordinator(DataUpdateCoordinator[HikvisionISAPIData]):
    """Polls ``/ISAPI/System/deviceInfo`` etc. on a configurable interval.

    Single shared client per entry (and per coordinator instance) —
    this matches the v0.1.12 pattern from the hikvision_snmp integration
    where one shared engine is faster than per-call setup.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool,
        use_https: bool,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._use_https = use_https
        self.device_info: dict[str, str] = {}
        self.system_status: dict[str, str] = {}
        self.channels: list[dict[str, Any]] = []
        self.capabilities: dict[str, bool] = {}
        self.storage: dict[str, Any] = {}
        self.network_interfaces: list[dict[str, Any]] = []
        self.streaming_bitrate_kbps: dict[str, int] = {}

    @property
    def host(self) -> str:
        return self._host

    def _make_client(self) -> ISAPIClient:
        return ISAPIClient(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            verify_ssl=self._verify_ssl,
            use_https=self._use_https,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    # ---- v0.6.14: per-endpoint fault-tolerant fetchers ----

    async def _fetch_device_info(
        self, client: ISAPIClient,
    ) -> tuple[ET.Element | None, dict[str, str]]:
        """``GET /ISAPI/System/deviceInfo``.

        Returns (raw_xml, parsed_dict). On failure: raw_xml=None,
        parsed_dict={}, with a WARNING log. Continues the refresh
        without raising — if deviceInfo fails, every "model",
        "firmware" etc. sensor shows "" but other endpoints'
        data still makes it to HA.
        """
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_DEVICE_INFO)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.warning(
                "%s /ISAPI/System/deviceInfo failed: %s — refresh "
                "continues with empty device_info",
                self._host, exc,
            )
            return None, {}
        return xml, _parse_device_info(xml)

    async def _fetch_system_status(
        self, client: ISAPIClient,
    ) -> tuple[ET.Element | None, dict[str, str]]:
        """``GET /ISAPI/System/status``.

        V4 NVRs (DS-7708N-I4 / DS-8632-I8 firmware V4.1.x) frequently
        return HTTP 404 for this endpoint. We catch it and continue
        with the empty default; the "device status" / "CPU" /
        "memory" sensors just show 0 / Unknown instead of taking
        the whole refresh down.
        """
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_STATUS)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            level = (
                _LOGGER.info if exc.__class__ is ISAPIError else _LOGGER.warning
            )
            level(
                "%s /ISAPI/System/status failed: %s — refresh "
                "continues with empty system_status",
                self._host, exc,
            )
            return None, {
                "deviceStatus": "Unknown",
                "cpuUtilization": "0",
                "memoryUsage": "0",
                "memoryAvailable": "0",
                "uptime": "0",
                "rebootCount": None,
                "cpuDescription": None,
            }
        return xml, _parse_system_status(xml)

    async def _fetch_channels(
        self,
        client: ISAPIClient,
        primary_path: str,
        alt_path: str,
    ) -> tuple[ET.Element | None, ET.Element | None, str | None]:
        """Try primary channels endpoint, fall back to alt.

        Returns (primary_xml, alt_xml, which_endpoint_won).
        Either XML may be None; on full failure both are None
        and the refresh continues with empty channels.
        """
        try:
            primary = await client.get_xml(primary_path)
            return primary, None, primary_path
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s channels primary %s failed (%s); trying %s",
                self._host, primary_path, exc, alt_path,
            )
        try:
            alt = await client.get_xml(alt_path)
            return None, alt, alt_path
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s channels alt %s also failed (%s); channels "
                "list will be empty",
                self._host, alt_path, exc,
            )
            return None, None, None

    async def _fetch_storage(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        """Try multiple storage endpoints; return the first that
        returns valid XML.

        Sequence (v0.6.16):

        1. ``/ISAPI/ContentMgmt/storage`` — V5 firmware (DS-2CDxxx
           IPCs and most V5 NVRs).
        2. ``/ISAPI/System/Storage/hardDisks`` — V4 NVR/DVR (e.g.
           ``DS-7708N-I4`` V4.1.18 uses one of these schemas).
        3. ``/ISAPI/ContentMgmt/storage/hddList`` — alternate V4
           path (some V4.8x firmwares).

        Empirically, the user's V4 ``DS-7708N-I4`` returns
        ``<ResponseStatus>`` 4 "Invalid Operation" for ALL three
        (NVR.txt line 34-46). On such devices no storage path
        exists and the user sees empty storage fields. We log the
        fact at INFO so the user can confirm via the diagnostic
        summary log; storage sensors stay Unknown.

        Returns the parsed XML root or None. IPCs typically have
        no storage at all — INFO on first call, then silent.
        """
        endpoints = (
            ISAPI_CONTENT_MGMT_STORAGE,
            ISAPI_SYSTEM_STORAGE_HARDDISKS,
            "/ISAPI/ContentMgmt/storage/hddList",
        )
        for endpoint in endpoints:
            try:
                return await client.get_xml(endpoint)
            except (ISAPIAuthError, ISAPIConnectionError) as exc:
                _LOGGER.warning(
                    "%s storage endpoint %s failed: %s",
                    self._host, endpoint, exc,
                )
                return None
            except ISAPIError as exc:
                # 404 / 4xx on storage is *normal* on IPCs and some
                # V4 firmwares. Try the next endpoint; only after
                # all three fail do we declare "missing" for this
                # refresh.
                _LOGGER.info(
                    "%s storage endpoint %s returned %s; trying next",
                    self._host, endpoint, exc,
                )
                continue
        _LOGGER.info(
            "%s storage: all candidate endpoints returned errors; "
            "no storage data this refresh (expected on IPCs without "
            "HDDs, and on V4 NVRs where firmware refuses the storage "
            "endpoint entirely)",
            self._host,
        )
        return None

    async def _fetch_network_interfaces(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        try:
            return await client.get_xml(ISAPI_SYSTEM_NETWORK_INTERFACES)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s /ISAPI/System/Network/interfaces failed: %s",
                self._host, exc,
            )
            return None

    async def _fetch_streaming(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        try:
            return await client.get_xml(f"{ISAPI_STREAMING_CHANNELS}/1")
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s /ISAPI/Streaming/channels/1 failed: %s",
                self._host, exc,
            )
            return None

    async def _async_update_data(self) -> HikvisionISAPIData:
        """One coordinator refresh: GET deviceInfo / status / channels / storage / network.

        v0.6.14: every endpoint is fetched independently under its
        own try/except. A single endpoint failure (HTTP 4xx, parse
        error, network blip on a sub-fetch) logs at WARNING and is
        recorded as "missing", but the entire refresh still returns
        whatever data DID come through. Pre-v0.6.14 the outer
        try/except would catch ISAPIError from any of the four main
        endpoints and convert it to ``UpdateFailed``, marking ALL
        entities for the device unavailable — even though half the
        endpoints may have succeeded.

        Specific cases this fixes:

        - V4 NVRs / DVRs (DS-7708N-I4 / DS-8632-I8 firmware V4.x)
          where ``/ISAPI/System/status`` returns HTTP 404 (V4
          firmware often lacks the V5 ``DeviceStatus`` schema). v0.6.13
          propagated that 404 → ``UpdateFailed`` → all entities
          unavailable. v0.6.14 catches the 404 and continues with
          empty status, so other endpoints' data still reaches the UI.
        - IPCs where ``/Streaming/channels`` returns an empty list
          because the IPC doesn't list its own videoInputChannel —
          v0.6.14 returns an empty channels list (was working in
          v0.6.13 but no INFO log to confirm).
        - DS-7708-I4 V4 NVR's lack of ``/ContentMgmt/InputProxy``
          endpoints: primary+alt both 404, coordinator still
          completes the refresh with empty channels rather than
          unavailable.

        v0.6.9: device-type-aware endpoint selection. The user has
        both IPCs (10.18.176.10, 10.18.176.65) and NVRs/DVRs
        (192.168.10.10) on the network, and the channel-list
        endpoint differs:

        - **IPC**: ``/ISAPI/Streaming/channels`` returns the
          streaming channel list (used for snapshot URL
          ``/Streaming/channels/{id}/picture``).
        - **NVR / DVR**: ``/ISAPI/ContentMgmt/InputProxy/channels``
          returns the mounted IPC channel list (used for snapshot
          URL ``/ContentMgmt/StreamingProxy/channels/{id}/picture``).

        Pre-v0.6.9 we hard-coded the NVR endpoint for all device
        types. IPCs returned HTTP 403 on the NVR endpoint, leaving
        the channels list empty for every IPC and no per-channel
        entities.

        We now fetch deviceInfo first (always succeeds on Hikvision
        firmware), determine the device type from
        ``deviceInfo.deviceType``, then choose the right endpoint.
        Fallbacks handle firmware quirks:

        - IPC: try ``/Streaming/channels``, fall back to
          ``/ContentMgmt/InputProxy/channels``.
        - NVR/DVR: try ``/ContentMgmt/InputProxy/channels``, fall
          back to ``/Streaming/channels``.

        Per-channel status endpoint is similarly chosen from device
        type (``/Streaming/channels/{id}/status`` for IPC,
        ``/ContentMgmt/InputProxy/channels/{id}/status`` for NVR/DVR).
        """
        # v0.6.12: log a single INFO line at the top of each refresh
        # so the user can confirm the device is being polled at all,
        # and which scheme/port we're trying. Pre-v0.6.12 all our
        # diagnostic logs were at DEBUG (off by default in HA),
        # making it look like "the integration isn't doing anything"
        # when actually the device was unreachable / returning 401.
        scheme = "https" if self._use_https else "http"
        _LOGGER.info(
            "Polling %s://%s:%d (%s)",
            scheme, self._host, self._port, entry_id_hint(self),
        )

        # All four endpoints are independently fault-tolerant.
        # Each ``_fetch_*`` helper logs its own WARNING on failure
        # and returns an empty/None result; the refresh continues.
        try:
            async with self._make_client() as client:
                device_info_xml, device_info = await self._fetch_device_info(client)
                status_xml, system_status = await self._fetch_system_status(client)
        except ISAPIConnectionError as exc:
            # The client itself couldn't be opened / connected at all.
            # This is the only failure mode that warrants UpdateFailed —
            # if we can't even hit the device, there's nothing useful
            # to populate. Other endpoint failures are NOT fatal.
            raise UpdateFailed(
                f"Network error talking to {self._host}: {exc}"
            ) from exc
        except ISAPIAuthError as exc:
            # Auth failed for the very first request (no challenge
            # even made it through); credentials are wrong or the
            # user account lacks ISAPI access entirely. Definitely
            # an UpdateFailed — there's no point polling if we
            # can't authenticate.
            raise UpdateFailed(
                f"Authentication failed for {self._host}: {exc}"
            ) from exc

        # Determine device type even if deviceInfo failed — default
        # to IPC so we still try channel endpoints with the IPC
        # routing (V4 DVRs may not have deviceInfo at all but
        # typically DO have storage endpoints).
        device_type = normalize_device_type(
            device_info.get("deviceType", "")
        )
        _LOGGER.info(
            "Detected %s at %s as deviceType=%r (routing channels "
            "endpoint to %s, per-channel status to %s)",
            device_info.get("model", "?"),
            self._host,
            device_info.get("deviceType", ""),
            ("/Streaming/channels" if device_type == DEVICE_TYPE_IPCAMERA
             else "/ContentMgmt/InputProxy/channels"),
            ("/Streaming/channels/{id}/status" if device_type == DEVICE_TYPE_IPCAMERA
             else "/ContentMgmt/InputProxy/channels/{id}/status"),
        )

        # Pick the right channel-list endpoint(s) for this device
        # type. We try the "primary" first then fall back to the
        # alternate in case of firmware that implements only one.
        if device_type == DEVICE_TYPE_IPCAMERA:
            primary_channels = ISAPI_STREAMING_CHANNELS
            alt_channels = ISAPI_INPUT_PROXY_CHANNELS
            primary_status_fmt = ISAPI_STREAMING_CHANNELS_STATUS
            alt_status_fmt = ISAPI_INPUT_PROXY_CHANNELS_STATUS
        else:
            # NVR or DVR
            primary_channels = ISAPI_INPUT_PROXY_CHANNELS
            alt_channels = ISAPI_STREAMING_CHANNELS
            primary_status_fmt = ISAPI_INPUT_PROXY_CHANNELS_STATUS
            alt_status_fmt = ISAPI_STREAMING_CHANNELS_STATUS

        # Second shared-client block for the per-device-type-dependent
        # endpoints. We open a new client because the auth class
        # may have switched from Digest to Basic during the first
        # session and we want both halves of the refresh to share
        # the same auth class.
        async with self._make_client() as client:
            channels_xml, channels_xml_alt, channels_endpoint_used = (
                await self._fetch_channels(
                    client, primary_channels, alt_channels,
                )
            )
            storage_xml = await self._fetch_storage(client)
            network_xml = await self._fetch_network_interfaces(client)
            streaming_xml = await self._fetch_streaming(client)

        # Channels parser-routing logic moved below; we now have
        # the raw XML and need to pick the right parser based on
        # which endpoint shape came back.

        # Channels parser-routing: the two endpoint response shapes
        # (InputProxyChannel vs StreamingChannel) differ in wrapper /
        # child element names; we pick the matching parser based on
        # the root tag.
        if channels_xml is not None or channels_xml_alt is not None:
            chosen = channels_xml if channels_xml is not None else channels_xml_alt
            root_tag = chosen.tag.split("}")[-1]  # strip namespace
            if root_tag.endswith("StreamingChannelList"):
                channels = _parse_streaming_channels_list(chosen)
            else:
                channels = _parse_channels(chosen)
        else:
            channels = []
        storage = _parse_storage(storage_xml)
        network_interfaces = _parse_network_interfaces(network_xml)
        streaming_bitrate_kbps = _parse_streaming_channels(streaming_xml)

        # v0.6.15: refresh summary log. One INFO line per refresh
        # showing which categories of data populated and which didn't.
        # The user can paste this single line back to confirm where
        # the data gaps are, without having to read every other
        # log line. Helps especially for V4 NVRs where some
        # endpoints return 404 due to firmware version differences.
        def _okfmt(value):
            """Format a category summary: count or 'missing'."""
            if isinstance(value, list):
                return str(len(value))
            if isinstance(value, dict):
                return "OK" if value else "missing"
            return "OK" if value else "missing"

        _LOGGER.info(
            "%s refresh summary: device_info=%s channels=%s "
            "storage=%s network=%s status=%s bitrate=%s",
            self._host,
            _okfmt(device_info),
            _okfmt(channels),
            _okfmt(storage),
            _okfmt(network_interfaces),
            _okfmt(system_status),
            _okfmt(streaming_bitrate_kbps),
        )

        # v0.6.9 — per-channel status endpoint chosen by device type.
        # IPCs use /Streaming/channels/{id}/status, NVR/DVRs use
        # /ContentMgmt/InputProxy/channels/{id}/status.
        # We determine the right format from device_type (already parsed
        # above into device_info). Fall back to primary_status_fmt
        # then alt_status_fmt if one returns ISAPIError.
        per_ch_status_fmt = primary_status_fmt

        # v0.3.0 — enrich each channel with detailed per-channel status
        # (online / recording / motion_detected). Best-effort per
        # channel: a small IPC that doesn't implement the per-channel
        # status endpoint keeps the channel-level online / recording
        # values from the channels list response (which are usually
        # present).
        for ch in channels:
            ch_id = ch.get("id", "")
            if not ch_id:
                continue
            status_xml = None
            # v0.6.12: the except tuple now includes ISAPIConnectionError
            # so a transient network blip on a single per-channel fetch
            # doesn't propagate out of _async_update_data and force
            # the entire device's entities into unavailable state.
            try:
                async with self._make_client() as client:
                    status_xml = await client.get_xml(
                        per_ch_status_fmt.format(id=ch_id)
                    )
            except (
                ISAPIError, ISAPIAuthError, ISAPIConnectionError,
            ) as exc:
                # Try the alt status endpoint in case device type
                # detection was wrong (e.g. deviceType is unknown and
                # defaulted to ipcamera but it's actually an NVR).
                if alt_status_fmt != per_ch_status_fmt:
                    try:
                        async with self._make_client() as client:
                            status_xml = await client.get_xml(
                                alt_status_fmt.format(id=ch_id)
                            )
                    except (
                        ISAPIError, ISAPIAuthError, ISAPIConnectionError,
                    ) as exc2:
                        _LOGGER.debug(
                            "Per-channel status unavailable for "
                            "%s ch %s: %s / %s",
                            self._host, ch_id, exc, exc2,
                        )
                else:
                    _LOGGER.debug(
                        "Per-channel status unavailable for %s "
                        "ch %s: %s",
                        self._host, ch_id, exc,
                    )
            if status_xml is None:
                continue
            try:
                ch_status = _parse_channel_status_extended(status_xml)
                # v0.6.12: per-channel status takes priority over the
                # channel-list values. Pre-v0.6.12 we used ``ch_status
                # or ch`` which put False from the per-channel endpoint
                # *behind* the channel-list value (a ``True`` in the
                # channel list would dominate a fresh ``False`` from
                # the per-channel endpoint). The current intent is the
                # reverse: if the per-channel endpoint reported
                # online=False, trust it. Only fall back to the
                # channel-list when the per-channel data wasn't
                # returned at all (already handled by the early
                # ``continue`` above).
                ch["online"] = ch_status["online"]
                ch["recording"] = ch_status["recording"]
                ch["motion_detected"] = ch_status["motion_detected"]
                # v0.5.0 — extended IPC health fields. The endpoint
                # reports the device uptime (not the channel's); useful
                # for the binary_sensor platforms and the new per-channel
                # sensor entities.
                for k in (
                    "uptime",
                    "reboot_count",
                    "sd_card_writes",
                    "camera_run_total_time",
                    "dome_heat_state",
                    "dome_fan_state",
                    "dome_runtime_over_40",
                ):
                    v = ch_status.get(k)
                    if v is not None:
                        ch[k] = v
            except ISAPIError as exc:
                _LOGGER.debug(
                    "Per-channel status parse failed for %s ch %s: %s",
                    self._host, ch_id, exc,
                )

        capabilities: dict[str, bool] = {
            "ptz": device_info.get("deviceType", "").lower()
            in {"ptz", "ptzdome"},
        }

        # Cache the parsed fields for downstream platforms.
        self.device_info = device_info
        self.device_type = normalize_device_type(
            device_info.get("deviceType", "")
        )
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities
        self.storage = storage
        self.network_interfaces = network_interfaces
        self.streaming_bitrate_kbps = streaming_bitrate_kbps

        return HikvisionISAPIData(
            device_info=device_info,
            system_status=system_status,
            channels=channels,
            capabilities=capabilities,
            storage=storage,
            network_interfaces=network_interfaces,
            streaming_bitrate_kbps=streaming_bitrate_kbps,
        )
