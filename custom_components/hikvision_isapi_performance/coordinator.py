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
from datetime import datetime, timedelta
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
    ISAPI_CONTENT_MGMT_SEARCH,
    ISAPI_CONTENT_MGMT_STORAGE,
    ISAPI_INPUT_PROXY_CHANNELS,
    ISAPI_INPUT_PROXY_CHANNELS_STATUS,
    ISAPI_STREAMING_CHANNELS,
    ISAPI_STREAMING_CHANNELS_STATUS,
    ISAPI_SYSTEM_CAPABILITIES,
    ISAPI_SYSTEM_DEVICE_INFO,
    ISAPI_SYSTEM_NETWORK_INTERFACES,
    ISAPI_SYSTEM_STATUS,
    ISAPI_SYSTEM_STORAGE_HARDDISKS,
    ISAPI_SYSTEM_TIME,
    ISAPI_SYSTEM_VIDEO_INPUTS_CHANNELS_MOTION_DETECTION,
)
from . import capabilities as _caps
from . import dedup as _dedup
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
    """Parse ``/ISAPI/System/deviceInfo`` response.

    v0.6.17: also extracts ``encoderVersion`` and
    ``encoderReleasedDate`` (V4 firmware usually has these; V5
    may omit them).
    """
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
        # V4-specific fields (some firmwares put them on the
        # deviceInfo root; on V5 they're typically absent).
        "encoderVersion": _xml_text(root, "encoderVersion") or "",
        "encoderReleasedDate": _xml_text(root, "encoderReleasedDate") or "",
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


def _parse_system_status(root: ET.Element | None) -> dict[str, Any]:
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
            # v0.9: "rebootCount" is deliberately NOT written here.
            # Pre-v0.9 both this branch and the main branch below set the
            # key unconditionally (value None when the field was absent),
            # so the key ALWAYS existed. sensor.py gates the entity on
            # ``"rebootCount" not in status`` — a presence check, matching
            # ``capabilities.has_reboot_count``. Because the key was never
            # absent, that gate could never fire and the six devices that
            # don't report <totalRebootCount> (176.16/17/18 fixed-lens
            # IPCs + all three recorders) got a permanently "unknown"
            # entity — one of the symptoms the user reported.
            #
            # Contract from here on: absent field ⇒ absent key.
            "cpuDescription": None,
            # v0.6.26: device-reported clock (ISO 8601 with offset, or None).
            # Used by ``device_time_abnormal`` binary sensor to detect a
            # dead CMOS battery (V4 NVRs roll this back to 2004-05).
            "currentDeviceTime": None,
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

    # v0.6.25: Hikvision's firmware is INCONSISTENT about which
    # unit ``memoryUsage`` and ``memoryAvailable`` use:
    #
    #   V4 NVR (DS-7708N-I4 V4.1.18): both in decimal MB.
    #     memoryUsage=" 728.234375"  (MB)
    #     memoryAvailable=" 402.613281"  (MB)
    #
    #   V5 IPC (DS-FB2127 V5.2.2): MIXED units — memoryUsage in
    #   MB, memoryAvailable in KB:
    #     memoryUsage="61"           (MB)
    #     memoryAvailable="224756"   (KB)
    #
    # Pre-v0.6.25 the parser stored both as raw integers, then
    # ``_memory_usage_percent`` did ``used / (used + available)``
    # assuming both were in the same unit. On V5 IPC this produced
    # 0.03% memory usage (61 / (61 + 224756) ≈ 0.027%) — wildly
    # wrong because ``memoryAvailable`` was actually 219.5 MB.
    #
    # Heuristic: if ``memoryAvailable > memoryUsage * 50`` then
    # ``memoryAvailable`` is in KB. Convert it to MB so downstream
    # sensors (memory_usage_percent, memory_available_mb) can
    # assume both fields are MB. The 50x threshold is conservative
    # — V4 NVRs typically have used/available within the same order
    # of magnitude, so they fall well below it.
    mem_usage_int = _safe_int_mb(mem_usage) or 0
    mem_avail_int = _safe_int_mb(mem_avail) or 0
    if (
        mem_usage_int > 0
        and mem_avail_int > mem_usage_int * 50
    ):
        mem_avail_int = round(mem_avail_int / 1024)

    out: dict[str, Any] = {
        "deviceStatus": (
            _xml_text(root, "deviceStatus") or "Unknown"
        ),
        "cpuUtilization": cpu_util or "0",
        # v0.6.25: both fields normalised to MB. V4 NVR reports both
        # in MB (no change); V5 IPC's mixed units (MB + KB) get
        # converted upstream. memoryAvailable is decimal-MB (so 402.6
        # MB available on V4 NVR becomes 402 on disk, 0.6 discarded
        # for the percentage round).
        "memoryUsage": str(mem_usage_int),
        "memoryAvailable": str(mem_avail_int),
        "uptime": _xml_text(root, "deviceUpTime") or _xml_text(root, "uptime") or "0",
        "cpuDescription": _xml_text(cpu, "cpuDescription") if cpu is not None else None,
        # v0.6.26: device-reported clock (ISO 8601 with offset, or None).
        # Used by ``dev_time_abnormal`` binary sensor to detect a
        # dead CMOS battery (V4 NVRs roll this back to 2004-05).
        "currentDeviceTime": _xml_text(root, "currentDeviceTime"),
    }

    # v0.9: rebootCount is written ONLY when the device really reports
    # <totalRebootCount>. See the ``root is None`` branch above for why
    # an always-present key broke sensor.py's presence gate and left six
    # devices with a permanently "unknown" entity. A reported "0" is real
    # data and still creates the entity (176.13/51/52/53 all report 0).
    reboot = _xml_text(root, "totalRebootCount")
    if reboot is not None:
        out["rebootCount"] = reboot

    # v0.9 batch 1: expose the rest of this same response, which pre-v0.9
    # threw away. No extra network cost — /System/status is already
    # fetched every poll. Fleet coverage (12 devices, real captures):
    #   dome_info      6/12  dome IPCs only (PTZ lifetime counters)
    #   camera_usage   6/12  same six (lens actuation counters)
    #   memoryDescription  12/12
    #   batteryAllowance / videoRewritingTimes  1/12 (176.10 only)
    # Each is omitted when the device doesn't report it, so platforms gate
    # on presence and never build an "unknown" entity.
    dome = _caps.parse_dome_info(root)
    if dome:
        out["dome_info"] = dome
    camera = _caps.parse_camera_usage(root)
    if camera:
        out["camera_usage"] = camera

    extras = _caps.parse_status_extras(root)
    if extras.get("memory_description") is not None:
        out["memoryDescription"] = extras["memory_description"]
    # batteryAllowance=0 on 176.10 is a genuine reading (this model has no
    # battery), so gate on None rather than truthiness.
    if extras.get("battery_allowance") is not None:
        out["batteryAllowance"] = extras["battery_allowance"]
    if extras.get("video_rewriting_times") is not None:
        out["videoRewritingTimes"] = extras["video_rewriting_times"]

    return out


def _parse_capabilities(root: ET.Element | None) -> dict[str, Any]:
    """Parse ``/ISAPI/System/capabilities`` response.

    v0.6.28: cross-check device classification. The ``deviceType``
    string returned by ``/System/deviceInfo`` is usually enough to
    distinguish IPC / NVR / DVR, but a small number of newer /
    obscure firmwares emit unexpected values. The capabilities
    endpoint gives an independent confirmation:

    - ``status_supported`` (``True`` when the response contains
      ``<SysStatus supported="true" />``) — pre-checks whether
      ``/System/status`` is likely to succeed. Devices that return
      ``notSupport`` on ``/System/status`` (e.g. some V4 NVRs)
      usually also omit ``<SysStatus>`` from the capabilities
      response, so this flag is a useful early signal.
    - ``video_input_channels`` (``<VideoInputChannelNums>``) — IPCs
      are 1, NVRs are 4/8/16/32. Cross-check against
      ``len(coordinator.channels)`` to detect channel-detection
      bugs on new device types.
    - ``device_types`` (``<SupportDeviceType><DeviceType>`` list) —
      the canonical device-type strings the firmware thinks it is.
      We log the intersection with our ``deviceType`` mapping as a
      diagnostic but still trust ``deviceInfo.deviceType`` (more
      specific than the capabilities enumeration).

    Returns an empty dict on parse failure / missing XML so the
    coordinator can carry a stable ``capabilities`` shape through
    every refresh.
    """
    if root is None:
        return {}
    out: dict[str, Any] = {}

    # Status supported: <SysStatus supported="true" /> — sometimes
    # the attribute is absent (older firmwares), default to True.
    sys_status = root.find(".//SysStatus")
    if sys_status is not None:
        supported_attr = sys_status.attrib.get("supported")
        if supported_attr is not None:
            out["status_supported"] = supported_attr.strip().lower() == "true"
        else:
            out["status_supported"] = True
    else:
        # No <SysStatus> element at all → most likely a very old
        # or non-standard firmware. Default to "unknown" (None) so
        # the consumer doesn't assume one way or the other.
        out["status_supported"] = None

    # v0.6.32: video input channel count. Hikvision firmwares use
    # multiple tag names across versions:
    # - V5.x NVR: <VideoInputChannelNums>
    # - Some V4 / older NVR: <videoInputChannelNums> (camelCase)
    # - Some firmware variants: <ChannelNum>, <InputChannelNum>
    # - v0.6.33: V5 IPC puts the count under
    #   <DeviceCap/SysCap/VideoCap/videoInputPortNums> — completely
    #   different schema from NVR. Walk candidates in order; first
    #   hit wins.
    out["video_input_channels"] = _find_int_field(
        root,
        candidates=(
            ".//VideoCap/videoInputPortNums",
            ".//videoInputPortNums",
            ".//VideoInputPortNums",
            ".//VideoInputChannelNums",
            ".//videoInputChannelNums",
            ".//InputChannelNum",
            ".//ChannelNum",
        ),
    )

    # v0.6.32: ethernet / NIC count — same multi-tag problem.
    # v0.6.33: V5 IPC puts NIC count under
    # <DeviceCap/SysCap/NetworkCap/...> without an obvious integer
    # leaf (the NetworkCap block is mostly boolean flags). NIC count
    # for IPCs stays ``None`` until we find a confirmed response.
    out["ethernet_interfaces"] = _find_int_field(
        root,
        candidates=(
            ".//EthernetNums",
            ".//ethernetNums",
            ".//NICNum",
            ".//NetworkInterfaceNum",
        ),
    )

    # Device types the firmware claims to support.
    types: list[str] = []
    for dt in root.findall(".//SupportDeviceType/DeviceType"):
        if dt.text:
            types.append(dt.text.strip())
    out["device_types"] = types

    # v0.7.1: PTZ capability. ``button.py`` and ``__init__.py`` both gate on
    # ``coordinator.capabilities.get("ptz")``, but this parser never set that
    # key — so PTZ direction buttons and the ``ptz_goto_preset`` service were
    # never registered on ANY device (dead code since v0.6.19).
    #
    # ``<PTZCtrlCap>`` is the authoritative signal. It is nested deep in the
    # tree (``DeviceCap/SysCap/...`` on V4 NVR, ``DeviceCap/PTZCtrlCap`` on
    # V5 IPC), hence the ``.//`` recursive search.
    #
    # Deliberately NOT using "has presets" as the signal: the user's
    # ``DS-FB2127`` V5.2.2 IPC lists 10 presets yet returns
    # ``methodNotAllowed`` on ``/PTZCtrl/channels/01/continuous`` and omits
    # ``<PTZCtrlCap>`` — it can read the preset table but cannot be driven.
    # Gating on PTZCtrlCap keeps buttons off for such devices.
    out["ptz"] = root.find(".//PTZCtrlCap") is not None

    return out


def _find_int_field(
    root: ET.Element,
    candidates: tuple[str, ...],
) -> int | None:
    """Find first matching numeric field across multiple XPath variants.

    v0.6.32: Hikvision firmware tags vary across versions — some
    use PascalCase (``VideoInputChannelNums``), some camelCase
    (``videoInputChannelNums``), some drop the suffix
    (``ChannelNum``). Walk the candidates list and return the
    first non-empty numeric value, or ``None`` if no candidate
    matched.

    Used by ``_parse_capabilities`` for fields that vary across
    firmware generations.
    """
    for path in candidates:
        node = root.find(path)
        if node is not None and node.text:
            value = _safe_int_mb(node.text)
            if value is not None:
                return value
    return None


def _tri_record_status(value: str | None) -> bool | None:
    """Map a ``recordStatus`` XML value to True / False / None.

    ``None`` means "the device never reported this", which is different
    from ``False`` ("the device says it is not recording"). Conflating
    them is what made every NVR channel display "录像中: 未在运行":
    neither ``InputProxyChannelList`` nor
    ``/ISAPI/ContentMgmt/InputProxy/channels/<id>/status`` carries a
    ``recordStatus`` element on the user's DS-7708N-I4 / DS-8632N-I8
    (probed: 0 occurrences), and every ``/ISAPI/ContentMgmt/Recording/*``
    endpoint returned 404. There is simply no data source, so the entity
    must render ``unknown`` instead of asserting a false negative.
    """
    if value is None:
        return None
    s = value.strip().lower()
    if not s:
        return None
    return s == "recording"


def _tri_online(value: str | None) -> bool | None:
    """Map an ``<online>`` XML value to True / False / None.

    v0.8: the same tri-state reasoning as ``_tri_record_status``, applied
    to ``online``. ``None`` means "the device never reported this".

    Real-fleet evidence (probe_status_id.py / probe_online_field.py,
    12 devices, 2026-10-04):

    * IPC ``/ISAPI/Streaming/channels/{id}/status`` does **not** return a
      channel status. It returns ``StreamingSessionStatusList`` — the list
      of active streaming sessions with client IP addresses — and carries
      **no ``<online>`` element at all**.
    * Meanwhile ``/ISAPI/Streaming/channels`` reports
      ``<enabled>true</enabled>`` for every stream of those same IPCs
      (176.10: streams 101/102/103 all enabled=true).

    Pre-v0.8 the parser returned ``False`` for the missing field and the
    coordinator merged it unconditionally, so every healthy, actively
    streaming IPC displayed "离线". This is exactly the defect shape that
    v0.7.4 fixed for ``recording`` (a missing field coerced into a false
    negative); ``online`` had simply been missed.

    Side note: that session list is a genuinely useful *other* signal
    (active stream count + client IPs) which this integration does not yet
    expose. Parsed but currently unused; kept out of scope for this fix.
    """
    if value is None:
        return None
    s = value.strip().lower()
    if not s:
        return None
    return s == "true"


def _merge_channel_status(
    ch: dict[str, Any],
    ch_status: dict[str, Any],
) -> None:
    """Merge per-channel status into a channel dict, in place.

    Extracted from ``_async_update_data`` in v0.8 so the merge rules are
    unit-testable — previously they lived inline in a 400-line refresh
    method where no test could reach them, which is exactly how the
    ``online`` regression stayed hidden.

    Rules (``None`` = "this endpoint never reported the field"):

    * ``online`` / ``recording`` — override only on an explicit
      True/False. A ``None`` must not clobber a value the channel list
      already supplied. This preserves the v0.6.12 intent (an explicit
      ``online=false`` from the fresher endpoint DOES override a stale
      ``True`` from the list) while fixing the v0.8 bug (an absent field
      must not fabricate a false negative).
    * ``recording`` — if neither source reported it, record ``None``
      explicitly so the entity renders unknown rather than falling
      through to a ``False`` default (v0.7.4 behaviour).
    * ``motion_detected`` and the extended health fields — only written
      when present, so an endpoint that omits them cannot erase data.
    """
    online = ch_status.get("online")
    if online is not None:
        ch["online"] = online

    recording = ch_status.get("recording")
    if recording is not None:
        ch["recording"] = recording
    elif "recording" not in ch:
        ch["recording"] = None

    # motion_detected is tri-state now (v0.8). The motion binary sensor
    # treats "key absent" as unknown, so a None must NOT be written — that
    # would make bool(None) render "off" (a false negative) instead of
    # unknown. Same reasoning as online/recording above.
    motion = ch_status.get("motion_detected")
    if motion is not None:
        ch["motion_detected"] = motion

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

    # v0.8: streaming-session count. Written only when the status response
    # really was a StreamingSessionStatusList (None otherwise). ``0`` is a
    # genuine reading — "nobody is pulling this stream right now" — so the
    # ``is not None`` test is what keeps it, while NVR/DVR channels (whose
    # status endpoint returns InputProxyChannelStatus) get no key at all and
    # therefore no permanently-empty sensor.
    sessions = ch_status.get("streaming_sessions")
    if sessions is not None:
        ch["streaming_sessions"] = sessions


def _parse_channels(root: ET.Element | None) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/ContentMgmt/InputProxy/channels`` response.

    Hikvision's response is::

        <InputProxyChannelList>
          <InputProxyChannel>
            <id>1</id>
            <name>Camera 1</name>
            <online>true</online>
            <sourceInputPortDescriptor>
              <ipAddress>10.18.176.10</ipAddress>
              <serialNumber>DS-2DF8C832MX-ZDK2025...</serialNumber>
            </sourceInputPortDescriptor>
            ...
          </InputProxyChannel>
        </InputProxyChannelList>

    v0.8 also captures ``serial_number`` and ``source_ip`` for
    cross-entry duplicate detection (see ``dedup.py``). Both are needed
    because firmware support differs: 176.64 reports serials for all 13
    channels, while 176.65 (DS-7708N-I4 V4.1.18) reports an **empty**
    ``serialNumber`` on every channel and can only be matched by IP.

    Note the open tag may carry attributes (``<InputProxyChannel
    version="1.0">`` on 176.65 / 192.168.10.17); ``findall`` matches by
    tag name so both forms are found. The list's own ``size`` attribute
    is NOT trusted — 176.64 declares size=19 with 13 real channels and
    176.65 declares size=0 with 8.
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for ch in root.findall(".//InputProxyChannel"):
        ch_id = _xml_text(ch, "id") or ""
        if not ch_id:
            continue
        # Identity fields live under <sourceInputPortDescriptor> on the
        # fleet; fall back to a subtree search for firmwares that flatten
        # them onto the channel element.
        descriptor = ch.find("sourceInputPortDescriptor")
        serial = (
            _xml_text(descriptor, "serialNumber")
            or _xml_text(ch, ".//serialNumber")
            or ""
        )
        source_ip = (
            _xml_text(descriptor, "ipAddress")
            or _xml_text(ch, ".//ipAddress")
            or ""
        )
        out.append(
            {
                "id": ch_id,
                "name": _xml_text(ch, "name") or f"Channel {ch_id}",
                "online": (_xml_text(ch, "online") or "").lower() == "true",
                # v0.7.4: None when the firmware omits <recordStatus>.
                # Was hardcoded False, which made every NVR channel show
                # "录像中: 未在运行" despite no data source — see
                # _tri_record_status.
                "recording": _tri_record_status(_xml_text(ch, "recordStatus")),
                # v0.8: cross-entry dedup identity.
                "serial_number": (serial or "").strip(),
                "source_ip": (source_ip or "").strip(),
            }
        )
    return out


def _stream_owner_channel(ch: ET.Element) -> str | None:
    """Return the *physical* channel number a ``<StreamingChannel>`` belongs to.

    ``/ISAPI/Streaming/channels`` lists one entry per **stream**, not per
    camera. A single IPC returns several entries (main / sub / third
    stream) that all belong to physical channel 1.

    The owner lives inside ``<Video>`` and uses one of two tag names
    depending on the firmware schema — both verified on the live fleet:

    - ``<dynVideoInputChannelID>`` — NVR schema, and newer IPCs that use
      ``101``/``102`` style stream ids (DS-2DF8C832MX-ZDK 摄像机10,
      DS-2CD8027F 摄像机12, DS-8632N-I8 录像机01).
    - ``<videoInputChannelID>`` — older IPC schema using ``1``/``2``/``3``
      style stream ids (DS-FB2127 摄像机06).

    Returns ``None`` when the field is absent, so the caller can fall back
    to the stream's own id instead of silently dropping the channel.
    """
    video = ch.find("Video")
    if video is None:
        return None
    for tag in ("dynVideoInputChannelID", "videoInputChannelID"):
        value = _xml_text(video, tag)
        if value:
            return value
    return None


def _parse_streaming_channels_list(
    root: ET.Element | None,
) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/Streaming/channels`` response (IPC-side).

    Hikvision's actual response shape — captured verbatim from the
    DS-FB2127 (10.18.176.18) and DS-8632N-I8 (192.168.10.9)::

        <StreamingChannelList>
          <StreamingChannel>
            <id>1</id>
            <channelName>摄像机06</channelName>
            <enabled>true</enabled>
            <Video>
              <videoInputChannelID>1</videoInputChannelID>   (IPC)
              <dynVideoInputChannelID>1</dynVideoInputChannelID>  (NVR)
              ...
            </Video>
          </StreamingChannel>
        </StreamingChannelList>

    v0.7.6: this read ``name`` and ``online``, but the real elements are
    ``channelName`` and ``enabled``. Both lookups returned None, so every
    IPC channel displayed the fallback label "Channel 1" and its
    channel-online binary sensor read False despite the camera streaming.
    The docstring above previously documented the wrong tag names, which
    is how the mistake survived review.

    v0.7.7: streams are grouped by their physical channel number. Before
    this, one channel entry was emitted per ``<StreamingChannel>``, but on
    an IPC each of those entries is a *stream* (main / sub / third), not a
    separate camera. Probed live: the 摄像机10 IPC returned 5 entries that
    all belong to physical channel 1, producing 5 duplicate sets of camera
    / recording-switch / binary-sensor / per-channel-sensor entities for a
    single camera. Grouping uses ``_stream_owner_channel``; a stream with
    no owner field falls back to its own id so nothing is silently dropped.

    Aggregation rules per group:

    - ``name``: first non-empty ``channelName`` (all streams of a camera
      normally carry the same name).
    - ``online``: OR across streams -- a camera counts as online when any
      of its streams is enabled.
    - ``recording``: first non-``None`` value, so a real state is never
      overwritten by an absent ``recordStatus``.

    Output is sorted numerically by channel id (falling back to string
    order) so the result is deterministic regardless of the order the
    device happens to list its streams in.

    ``recordStatus`` is genuinely absent from this shape -- that value
    comes from the per-channel status endpoint (see _tri_record_status).
    """
    if root is None:
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for ch in root.findall(".//StreamingChannel"):
        stream_id = _xml_text(ch, "id") or ""
        # Group key = physical channel number when the firmware reports
        # one, else the stream's own id (nothing gets dropped).
        owner = _stream_owner_channel(ch)
        key = owner or stream_id or _xml_text(ch, "videoInputChannelID") or ""
        if not key:
            continue

        # v0.7.6: the real element is <channelName>, not <name>.
        # <name> is kept as a fallback for firmware variants.
        name = _xml_text(ch, "channelName") or _xml_text(ch, "name") or ""
        # v0.7.6: the real element is <enabled>, not <online>. Probed on
        # DS-FB2127 (true) and its sub-stream (false), so both polarities
        # are real. <online> stays as a fallback.
        online = (
            _xml_text(ch, "enabled") or _xml_text(ch, "online") or ""
        ).lower() == "true"
        # v0.7.4: None when <recordStatus> is absent — see
        # _tri_record_status.
        recording = _tri_record_status(_xml_text(ch, "recordStatus"))

        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "id": key,
                "name": name or f"Channel {key}",
                "online": online,
                "recording": recording,
            }
            continue

        # Merge into the already-seen entry for this physical channel.
        if not existing["name"] or existing["name"] == f"Channel {key}":
            if name:
                existing["name"] = name
        existing["online"] = bool(existing["online"]) or online
        if existing["recording"] is None and recording is not None:
            existing["recording"] = recording

    def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
        cid = str(item["id"])
        try:
            return (0, f"{int(cid):08d}")
        except (TypeError, ValueError):
            return (1, cid)

    return sorted(grouped.values(), key=_sort_key)


def _parse_storage(root: ET.Element | None) -> dict[str, Any]:
    """Parse ``/ISAPI/ContentMgmt/storage`` (V5) or
    ``/ISAPI/System/Storage/hardDisks`` (V4 NVR fallback).

    Three Hikvision shapes are accepted.

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

    **V5 IPC firmware** (e.g. ``DS-2CD`` series) -- same shape but
    **lowercase** tags, capacity already in **MB**::

        <storage>
          <hddList size="8">
            <hdd>
              <id>1</id>
              <hddName>hdde</hddName>
              <capacity>119290</capacity>
              <freeSpace>0</freeSpace>
              <status>ok</status>
            </hdd>
          </hddList>
        </storage>

    The parser auto-detects per-HDD: a hit on ``<size>`` means
    V4 (BYTES); only ``<capacity>`` hits means V5 IPC (MB).

    **v0.8 — per-HDD health (real-fleet fix)**

    ``<status>`` carries more values than the old ``("normal", "ok")``
    whitelist allowed. Probed on the live fleet:

        176.64 (DS-8632N-I8, 5 bays)::

            hdd1=ok  hdd2=ok  hdd3=notexist  hdd4=ok  hdd5=ok

    ``notexist`` is an **empty bay** and ``idle`` is a **spare/standby
    disk** (observed as ``idle`` on DS-8632N-I8 hdd5 in earlier
    captures). Neither is a fault, yet the old aggregate logic
    (``status not in ("normal", "ok") -> exception``) flagged the whole
    device as an exception — the user saw a storage alarm on a machine
    with three healthy disks and one empty bay.

    v0.8 therefore:
      * skips ``notexist`` bays entirely (no capacity, no entity)
      * treats ``{ok, normal, idle, unformatted}`` as healthy
      * reports any other value as an exception AND keeps the raw value
        in ``status_detail`` so the user can see *what* is wrong
      * returns ``hdds`` (per-disk detail) and ``hdd_error_count``
    """
    empty = {
        "total_mb": None,
        "used_mb": None,
        "free_mb": None,
        "status": "unknown",
        # v0.8: per-disk detail + fault count. Always present so
        # callers can use ``.get`` without branching on firmware shape.
        "hdds": [],
        "hdd_error_count": 0,
        "status_detail": [],
    }
    if root is None:
        return empty

    # v0.8: healthy per-disk states. ``notexist`` is handled separately
    # (the bay is skipped rather than counted as healthy).
    _HEALTHY = ("ok", "normal", "idle", "unformatted")
    _ABSENT = "notexist"

    # V5 path: direct fields.
    total_mb = _safe_int_mb(_xml_text(root, "totalCapacity"))
    used_mb = _safe_int_mb(_xml_text(root, "usedCapacity"))
    free_mb = _safe_int_mb(_xml_text(root, "freeCapacity"))
    status = _xml_text(root, "status")

    if total_mb is not None or used_mb is not None or free_mb is not None:
        # V5 shape — done. No per-disk list in this shape.
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "free_mb": free_mb,
            "status": status or "unknown",
            "hdds": [],
            "hdd_error_count": 0,
            "status_detail": [],
        }

    # V4 NVR + V5 IPC fallback: per-HDD list.
    # v0.6.33: V5 IPC uses lowercase <hdd> with
    # <capacity>/<freeSpace> in MB; V4 NVR uses <HDD> with
    # <size>/<freeSize> in bytes. Try both tag names.
    hdds = root.findall(".//HDD")
    if not hdds:
        hdds = root.findall(".//hdd")
    if not hdds:
        return empty

    total_units = 0  # bytes for V4, MB for V5 IPC
    free_units = 0
    status_aggregate = "normal"
    seen = False
    is_bytes = False  # detected per-loop; once True stays True
    per_hdd: list[dict[str, Any]] = []
    error_count = 0
    status_detail: list[str] = []

    def _units(raw: int | None) -> float | None:
        """Convert a raw unit value to MB once the shape is known."""
        if raw is None:
            return None
        return round(raw / 1_000_000, 1) if is_bytes else round(raw, 1)

    for hdd in hdds:
        # Prefer V4 uppercase tags first; fall back to V5 IPC
        # lowercase tags. Detection of unit: if <size> is hit first,
        # the response is V4 (bytes); if only <capacity> hits, it's
        # V5 IPC (MB).
        size_raw = _safe_int_mb(_xml_text(hdd, "size"))
        if size_raw is None:
            size_raw = _safe_int_mb(_xml_text(hdd, "capacity"))
        else:
            is_bytes = True
        free_raw = _safe_int_mb(_xml_text(hdd, "freeSize"))
        if free_raw is None:
            free_raw = _safe_int_mb(_xml_text(hdd, "freeSpace"))

        # V4 NVR uses ``<status>normal</status>``/``error``/etc.
        # V5 IPC uses ``<status>ok</status>``/``idle``/etc.
        hdd_status = (_xml_text(hdd, "status") or "").strip().lower()

        # v0.8: an empty bay is not a disk. Skip it entirely so it
        # contributes no capacity, no entity and no fault.
        if hdd_status == _ABSENT:
            continue

        if size_raw is None and free_raw is None:
            continue
        seen = True
        if size_raw is not None:
            total_units += size_raw
        if free_raw is not None:
            free_units += free_raw

        if hdd_status and hdd_status not in _HEALTHY:
            status_aggregate = "exception"
            error_count += 1
            status_detail.append(hdd_status)

        per_hdd.append({
            "id": _xml_text(hdd, "id") or "",
            "name": _xml_text(hdd, "hddName") or "",
            "type": _xml_text(hdd, "hddType") or "",
            "status": hdd_status or "unknown",
            "capacity_mb": _units(size_raw),
            "free_mb": _units(free_raw),
        })

    if not seen:
        return empty

    if is_bytes:
        # V4 NVR units: BYTES -> MB (1 MB = 10^6 bytes).
        total_mb = (
            round(total_units / 1_000_000, 1) if total_units else None
        )
        # v0.6.33 fix: free==0 (full disk) used to leave
        # used_mb=None because of falsy ``and``. Compute used as
        # total - free whenever total > 0 AND free was actually
        # read off the device (not None).
        used_mb = (
            round((total_units - free_units) / 1_000_000, 1)
            if total_units and free_units is not None
            else None
        )
        free_mb = (
            round(free_units / 1_000_000, 1)
            if free_units is not None else None
        )
    else:
        # V5 IPC units: already in MB.
        total_mb = round(total_units, 1) if total_units else None
        used_mb = (
            round(total_units - free_units, 1)
            if total_units and free_units is not None
            else None
        )
        free_mb = round(free_units, 1) if free_units is not None else None

    return {
        "total_mb": total_mb,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "status": status_aggregate,
        "hdds": per_hdd,
        "hdd_error_count": error_count,
        "status_detail": status_detail,
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

    def _mac_address(iface: ET.Element) -> str:
        """v0.6.17: read MAC from either V4 nested or V5 direct.

        V4 NVR firmware wraps MAC inside ``<Link>``::

            <Link>
              <MACAddress>02:00:00:00:00:0e</MACAddress>
            </Link>

        V5 firmware has ``<MACAddress>`` as a direct child of
        ``<NetworkInterface>``.

        Also fall back to the V4 link's ``speed`` / ``MTU`` shape
        — V5's MTU is often on the link, but some firmwares put
        it on the interface directly.
        """
        nested = iface.find("Link/MACAddress")
        if nested is not None and nested.text:
            return nested.text
        return (iface.findtext("MACAddress") or "").strip()

    def _mtu(iface: ET.Element) -> int | None:
        """v0.6.19: read MTU from either V4 direct or V5 ``<Link>``-nested.

        V5 firmware wraps MTU inside ``<Link>`` alongside MACAddress::

            <Link>
              <MACAddress>02:00:00:00:00:0d</MACAddress>
              <MTU>1500</MTU>
            </Link>

        V4 NVR firmware puts ``<MTU>`` as a direct child of
        ``<NetworkInterface>``. Try both.
        """
        link_mtu = _safe_int_mb(_xml_text(iface, "Link/MTU"))
        if link_mtu is not None:
            return link_mtu
        return _safe_int_mb(_xml_text(iface, "MTU"))

    out: list[dict[str, Any]] = []
    for iface in root.findall(".//NetworkInterface"):
        out.append(
            {
                "id": _xml_text(iface, "id") or "",
                "name": _xml_text(iface, "interfaceName") or "",
                "ip_address": _ip_field(iface, "ipAddress"),
                "subnet_mask": _ip_field(iface, "subnetMask"),
                "default_gateway": _ip_field(iface, "DefaultGateway"),
                "mtu": _mtu(iface),
                "mac_address": _mac_address(iface),
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


def _parse_time(root: ET.Element | None) -> dict[str, str | None]:
    """Parse ``/ISAPI/System/time`` for clock mode + local time.

    Returns ``{"time_mode": "...", "local_time": "...", "time_zone": "..."}``
    with ``None`` for any field missing on the device's firmware.
    """
    if root is None:
        return {"time_mode": None, "local_time": None, "time_zone": None}
    # V5 + V4 both use ``<timeMode>``. Some V4 firmwares emit
    # ``<TimeMode>`` (capitalised) instead — fall back to that.
    time_mode = _xml_text(root, "timeMode") or _xml_text(root, "TimeMode")
    local_time = _xml_text(root, "localTime") or _xml_text(root, "LocalTime")
    time_zone = _xml_text(root, "timeZone") or _xml_text(root, "TimeZone")
    return {
        "time_mode": time_mode or None,
        "local_time": local_time or None,
        "time_zone": time_zone or None,
    }


def _parse_streaming_detail(
    root: ET.Element | None,
) -> dict[str, Any]:
    """Parse ``/ISAPI/Streaming/channels`` for per-channel streaming
    detail (codec, resolution, frame rate, audio codec, video bitrate).

    Returns a dict shaped::

        {
          "first_channel_id": "1",
          "video_codec": "H.264",
          "video_resolution": "1920x1080",
          "video_frame_rate": 16.0,
          "video_bitrate_kbps": 2048,
          "video_resolution_width": 1920,
          "video_resolution_height": 1080,
          "video_input_channel_id": 1,
          "audio_codec": "G.711alaw",
          "channels": [
            {"id": "1", "video_codec": ..., ...},
            ...
          ]
        }

    The top-level fields describe the first ``<StreamingChannel>`` in
    the response (the primary video stream on a single-input IPC) so
    a single-IPC user sees coherent values without needing to know
    ``channels[*]``. The ``channels`` list carries all entries for
    users with multi-channel IPCs / PTZ cameras with sub-streams.

    Per the user's V5 IPC ``DS-FB2127`` actual response:

        <StreamingChannel>
          <id>1</id>
          <Video>
            <videoCodecType>H.264</videoCodecType>
            <videoResolutionWidth>1920</videoResolutionWidth>
            <videoResolutionHeight>1080</videoResolutionHeight>
            <maxFrameRate>1600</maxFrameRate>
            <constantBitRate>2048</constantBitRate>
          </Video>
          <Audio>
            <audioCompressionType>G.711alaw</audioCompressionType>
          </Audio>
        </StreamingChannel>

    ``maxFrameRate`` is reported in hundredths-of-fps (so 1600 = 16 fps,
    3000 = 30 fps) — we divide by 100 to get integer FPS.
    """
    empty: dict[str, Any] = {
        "first_channel_id": None,
        "video_codec": None,
        "video_resolution": None,
        "video_frame_rate": None,
        "video_bitrate_kbps": None,
        "video_resolution_width": None,
        "video_resolution_height": None,
        "video_input_channel_id": None,
        "audio_codec": None,
        "channels": [],
    }
    if root is None:
        return empty

    def _channel_summary(ch: ET.Element) -> dict[str, Any]:
        """Pull per-channel codec / resolution / framerate / etc.

        Hikvision's ``<StreamingChannel>`` nests the video
        fields inside a ``<Video>`` element and audio fields
        inside ``<Audio>`` — we look those up by descendant
        search rather than direct-child read so the nesting
        doesn't matter.
        """
        video = ch.find("Video")
        audio = ch.find("Audio")
        max_frame_raw = _safe_int_mb(
            _xml_text(video, "maxFrameRate") if video is not None else None
        )
        codec = _xml_text(video, "videoCodecType") if video is not None else None
        width = _safe_int_mb(
            _xml_text(video, "videoResolutionWidth") if video is not None else None
        )
        height = _safe_int_mb(
            _xml_text(video, "videoResolutionHeight") if video is not None else None
        )
        bitrate = _safe_int_mb(
            _xml_text(video, "videoAverageBitrate") if video is not None else None
        )
        if bitrate is None and video is not None:
            bitrate = _safe_int_mb(_xml_text(video, "constantBitRate"))
        # v0.7.1: VBR streams report neither of the above. The user's
        # DS-8632N-I8 returns ``<videoQualityControlType>VBR`` with
        # ``<vbrUpperCap>16384</vbrUpperCap>`` and no constantBitRate,
        # so video_bitrate_kbps stayed None on every VBR channel.
        #
        # vbrUpperCap is the configured ceiling (the "码率上限" shown in
        # the device web UI), not the instantaneous rate — it is the
        # closest meaningful configured value for a VBR stream, and
        # beats showing "unknown".
        if bitrate is None and video is not None:
            bitrate = _safe_int_mb(_xml_text(video, "vbrUpperCap"))
        audio_codec = (
            _xml_text(audio, "audioCompressionType") if audio is not None else None
        )
        video_input = _safe_int_mb(
            _xml_text(video, "videoInputChannelID") if video is not None else None
        )
        # v0.7.5: NVRs report the owning channel here instead. Probed on
        # DS-8632N-I8 (192.168.10.9): every <Video> block carries
        # <dynVideoInputChannelID> and NO <videoInputChannelID>, so
        # video_input alone was None on NVRs and no stream tier could be
        # derived. The IPC DS-FB2127 is the mirror image — it carries
        # videoInputChannelID only. Both are kept as separate keys so
        # consumers can tell "1" (real channel id) from an absent field.
        dyn_video_input = _safe_int_mb(
            _xml_text(video, "dynVideoInputChannelID")
            if video is not None else None
        )
        return {
            "id": _xml_text(ch, "id") or None,
            "video_codec": codec or None,
            "video_resolution_width": width,
            "video_resolution_height": height,
            "video_resolution": (
                f"{width}x{height}" if width and height else None
            ),
            "video_frame_rate": (
                round(max_frame_raw / 100, 2)
                if max_frame_raw is not None else None
            ),
            "video_bitrate_kbps": bitrate,
            "video_input_channel_id": video_input,
            "dyn_video_input_channel_id": dyn_video_input,
            "audio_codec": audio_codec or None,
        }

    channels = [_channel_summary(ch) for ch in root.findall(".//StreamingChannel")]
    if not channels:
        return empty
    first = channels[0]
    return {
        "first_channel_id": first["id"],
        "video_codec": first["video_codec"],
        "video_resolution": first["video_resolution"],
        "video_frame_rate": first["video_frame_rate"],
        "video_bitrate_kbps": first["video_bitrate_kbps"],
        "video_resolution_width": first["video_resolution_width"],
        "video_resolution_height": first["video_resolution_height"],
        "video_input_channel_id": first["video_input_channel_id"],
        "audio_codec": first["audio_codec"],
        "channels": channels,
    }


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
            "online": None,
            "recording": None,
            "motion_detected": None,
        }
    return {
        # v0.8: tri-state — a missing <online> is None ("not reported"),
        # not False. See _tri_online for the IPC session-list evidence.
        "online": _tri_online(_xml_text(root, "online")),
        # v0.7.4: None when <recordStatus> is absent — see
        # _tri_record_status. Was False, which reported a false
        # "not recording" on NVRs that never carry this field.
        "recording": _tri_record_status(_xml_text(root, "recordStatus")),
        # v0.8: tri-state for the same reason as online. The IPC session
        # list carries no <motionDetection> either.
        "motion_detected": _tri_online(_xml_text(root, "motionDetection")),
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
            "online": None,
            # v0.7.4: None, not False — no XML means the device told us
            # nothing, which must not render as "未在运行".
            "recording": None,
            "motion_detected": None,
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
        # v0.8: tri-state. See _tri_online — IPC /Streaming/channels/{id}/
        # status returns a session list with no <online> at all.
        "online": _tri_online(_xml_text(root, "online")),
        # v0.7.4: None when <recordStatus> is absent — see
        # _tri_record_status. Probed on DS-7708N-I4 / DS-8632N-I8: the
        # per-channel status response carries <online> but no
        # <recordStatus>, so False here was a fabricated "not recording".
        "recording": _tri_record_status(_xml_text(root, "recordStatus")),
        # v0.8: tri-state for the same reason as online.
        "motion_detected": _tri_online(_xml_text(root, "motionDetection")),
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
        # v0.8: active streaming sessions. IPCs answer this same status
        # endpoint with a StreamingSessionStatusList (no <online> at all,
        # which is what caused the "every IPC shows offline" bug above).
        # That list is the only session-count source on the fleet — the
        # device-level /Streaming/sessions endpoint fails on 12/12.
        # NVR/DVR answer with InputProxyChannelStatus, so this is None
        # there and no session sensor is created (0 would be a lie: they
        # don't report sessions at all, they don't have zero of them).
        "streaming_sessions": _caps.parse_streaming_sessions(root),
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
        # v0.9: values are str for the pre-v0.9 fields, but also int
        # (batteryAllowance / videoRewritingTimes) and nested dicts
        # (dome_info / camera_usage). Hence Any, not str.
        system_status: dict[str, Any],
        channels: list[dict[str, Any]],
        capabilities: dict[str, bool],
        storage: dict[str, Any] | None = None,
        network_interfaces: list[dict[str, Any]] | None = None,
        streaming_bitrate_kbps: dict[str, int] | None = None,
        streaming_channel_detail: dict[str, Any] | None = None,
        time_info: dict[str, str | None] | None = None,
        system_capabilities: dict[str, Any] | None = None,
        recording_status: dict[str, Any] | None = None,
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
        # v0.6.18: per-channel streaming detail (codec, resolution,
        # frame rate, audio codec). Populated from
        # ``/ISAPI/Streaming/channels``. Empty dict on devices that
        # don't expose that endpoint.
        self.streaming_channel_detail = streaming_channel_detail or {}
        # v0.6.19: device clock / NTP info from /ISAPI/System/time.
        self.time_info = time_info or {
            "time_mode": None, "local_time": None, "time_zone": None,
        }
        # v0.6.28: parsed ``/ISAPI/System/capabilities`` response.
        # Keys: ``status_supported`` (bool|None), ``video_input_channels``
        # (int|None), ``device_types`` (list[str]), ``ethernet_interfaces``
        # (int|None). Empty dict when the endpoint isn't reachable.
        self.system_capabilities = system_capabilities or {}
        # v0.8: per-channel recording activity derived from
        # ``/ISAPI/ContentMgmt/search`` segments. Shape:
        # ``{channel_id: {recording_active, last_recording_time,
        # codec_type, record_type}}``. Empty dict when the device has no
        # recording-search endpoint (IPCs, older firmwares) or no
        # recordings — in which case no recording entities are created.
        # This is a *derived* value: see capabilities.derive_recording_status.
        self.recording_status = recording_status or {}


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
        # v0.8: kept for cross-entry dedup, which needs both this entry's
        # id (to exclude itself from "other entries") and the entry object
        # (to write the device identity back into ``entry.data``).
        # ``entry_id_hint`` already read ``coordinator.entry_id``
        # defensively, but nothing ever set it — it always logged "?".
        self.entry = entry
        self.entry_id = getattr(entry, "entry_id", "") or ""
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._use_https = use_https
        self.device_info: dict[str, str] = {}
        # v0.9: also carries int and nested-dict values (dome_info,
        # camera_usage, batteryAllowance) — see _parse_system_status.
        self.system_status: dict[str, Any] = {}
        self.channels: list[dict[str, Any]] = []
        self.capabilities: dict[str, bool] = {}
        self.storage: dict[str, Any] = {}
        self.network_interfaces: list[dict[str, Any]] = []
        self.streaming_bitrate_kbps: dict[str, int] = {}
        self.streaming_channel_detail: dict[str, Any] = {}
        # v0.6.28: parsed /ISAPI/System/capabilities response.
        # Empty dict until the first coordinator refresh populates it.
        self.system_capabilities: dict[str, Any] = {}
        # v0.6.24: initialize device_type to empty string in __init__
        # so platforms (sensor.async_setup_entry) can read it
        # BEFORE the first coordinator refresh completes. The
        # actual value lands via ``self.device_type = ...`` inside
        # ``_async_update_data`` after the first parse, but
        # platforms need a default for their filter logic.
        self.device_type: str = ""
        # v0.8: alertStream push state. ``event_state`` maps
        # ``sensor_key -> {channel_id: bool}`` for the event types the
        # device actually pushes (motion / video_loss / tamper), and
        # ``event_meta`` keeps the last channel name + timestamp per
        # channel for the event-bus payload. Both are written only from
        # the reader task, on the event loop, so no lock is needed.
        self.event_state: dict[str, dict[str, bool]] = {}
        self.event_meta: dict[str, dict[str, Any]] = {}
        self.event_types_seen: set[str] = set()
        self._event_reader: Any = None
        self._event_task: Any = None
        # v0.8: per-channel motion-detection CONFIG cache, keyed by
        # channel id -> {enabled, sensitivity_level, _raw_xml}.
        #
        # This is deliberately NOT part of the 30 s poll. motionDetection
        # is a configuration value that rarely changes (11/12 fleet
        # devices report sensitivityLevel=60), so polling it per refresh
        # would add ``len(channels)`` extra requests every cycle — 13 on
        # an 8/16/32-channel NVR. Instead it is probed once after setup
        # and refreshed only after a PUT (see the switch / number
        # platforms). Storing the raw XML lets a write-back preserve the
        # user's detection gridMap instead of clobbering it.
        self.motion_detection: dict[str, dict[str, Any]] = {}
        self._motion_probe_done = False
        # v0.8: cross-entry duplicate detection (see dedup.py). Channel
        # ids on THIS device whose hardware is also configured as its own
        # entry — probed on the fleet: 9 of NVR 176.64's 13 channels have
        # serialNumbers identical to 9 separately-added IPCs. Platforms
        # register those channels' entities with
        # ``entity_registry_enabled_default=False`` so one camera does not
        # produce two full entity sets. Empty set when the user has only
        # added one side.
        self.duplicate_channels: set[str] = set()
        self._identity_published = False
        self._dedup_applied_signature: Any = None
        # NOTE: ``self.entry`` / ``self.entry_id`` are assigned at the top
        # of this constructor (next to ``self._host``). Do not re-set them
        # to None here — an earlier draft did, which made
        # ``publish_identity`` no-op and silently disabled all dedup.

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

    # ---- v0.8: alertStream push events ----

    def async_start_event_stream(self) -> None:
        """Start the alertStream reader task (idempotent).

        Called from ``__init__.async_setup_entry`` after platforms are
        forwarded. The reader is best-effort: if the device doesn't serve
        alertStream, it backs off and retries — polling continues to work
        regardless, so a push failure never degrades the integration below
        its v0.7 behaviour.

        Events land in ``self.event_state`` (consumed by the binary
        sensors) and are re-published to HA's event bus for automations.
        """
        if self._event_task is not None and not self._event_task.done():
            return

        from .event_stream import AlertStreamReader

        def _factory() -> ISAPIClient:
            client = self._make_client()
            # The reader owns a long-lived connection, so it opens the
            # httpx client via the public ``open()`` rather than relying on
            # a per-request ``async with`` block. This also keeps
            # coordinator.py free of any httpx import or client privates.
            client.open()
            return client

        self._event_reader = AlertStreamReader(
            client_factory=_factory,
            on_event=self._on_stream_event,
        )
        self._event_task = self.hass.async_create_task(
            self._event_reader.run(),
            name=f"hikvision_isapi_performance.alertstream.{self._host}",
        )
        _LOGGER.info("%s alertStream reader started", self._host)

    def _on_stream_event(self, ev: Any) -> None:
        """Handle one parsed alertStream event.

        Runs on the event loop (the reader is an asyncio task), so plain
        dict mutation is safe without a lock.
        """
        key = _caps.event_types_to_sensor_keys({ev.event_type})
        if not key:
            # An event type we don't model (diskfull, ipconflict, …).
            # Still recorded for diagnostics + the event bus, but it drives
            # no binary sensor.
            sensor_key = None
        else:
            sensor_key = next(iter(key))

        active = (ev.event_state or "").strip().lower() == "active"

        if sensor_key is not None:
            by_channel = self.event_state.setdefault(sensor_key, {})
            ch = ev.channel_id or "0"
            if by_channel.get(ch) != active:
                by_channel[ch] = active
                # Only wake listeners on a real transition, so a
                # fire-hose device (176.10 pushed 2.25 MB in 8 s) can't
                # thrash entity state updates.
                self.async_update_listeners()

        self.event_types_seen.add(ev.event_type)
        self.event_meta[ev.channel_id or "0"] = {
            "channel_name": ev.channel_name,
            "date_time": ev.date_time,
            "event_type": ev.event_type,
            "event_state": ev.event_state,
        }

        # Publish to HA's event bus so automations can react without
        # needing a binary sensor per event type.
        try:
            self.hass.bus.async_fire(
                f"{DOMAIN}_event",
                {
                    "host": self._host,
                    "channel_id": ev.channel_id,
                    "dyn_channel_id": ev.dyn_channel_id,
                    "channel_name": ev.channel_name,
                    "event_type": ev.event_type,
                    "event_state": ev.event_state,
                    "date_time": ev.date_time,
                    "active_post_count": ev.active_post_count,
                },
            )
        except Exception as exc:  # noqa: BLE001 - bus is best-effort
            _LOGGER.debug("%s event bus fire failed: %s", self._host, exc)

    async def async_shutdown(self) -> None:
        """Stop the alertStream reader, then run the base shutdown.

        HA calls this when the config entry is unloaded. Without it the
        reader task and its httpx connection would outlive the entry.
        """
        reader, task = self._event_reader, self._event_task
        self._event_reader = None
        self._event_task = None
        if reader is not None:
            try:
                await reader.stop(task)
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.debug("%s alertStream stop failed: %s", self._host, exc)
        await super().async_shutdown()

    # ---- v0.8: motion-detection config (probe-once, not polled) ----

    async def async_probe_motion_detection(self) -> None:
        """Probe per-channel motionDetection config once.

        Called from ``__init__`` as a background task after the first
        refresh (so ``self.channels`` is populated). Deliberately not part
        of ``_async_update_data``: it is configuration that rarely changes,
        and polling it would add ``len(channels)`` requests to every 30 s
        cycle.

        Capability-gated by outcome: a device that 403s (176.65 /
        DS-7708N-I4 V4.1.18) simply gets no entry, so the switch and
        sensitivity entities are never created for it. The raw XML is kept
        per channel so that writing ``enabled`` / ``sensitivityLevel`` back
        preserves the user's detection gridMap instead of clobbering it.
        """
        if self._motion_probe_done:
            return
        self._motion_probe_done = True

        channel_ids = [
            str(ch.get("id", "")).strip()
            for ch in self.channels
            if str(ch.get("id", "")).strip()
        ]
        if not channel_ids:
            return

        try:
            async with self._make_client() as client:
                for cid in channel_ids:
                    await self._probe_motion_channel(client, cid)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s motion-detection probe aborted (%s)", self._host, exc
            )

        if self.motion_detection:
            # Wake platforms so the switch / number entities register.
            self.async_update_listeners()
            _LOGGER.info(
                "%s motion-detection config probed for %d/%d channel(s)",
                self._host, len(self.motion_detection), len(channel_ids),
            )

    async def _probe_motion_channel(
        self, client: ISAPIClient, channel_id: str
    ) -> None:
        """Fetch + cache motionDetection for one channel. Silent on 4xx."""
        path = ISAPI_SYSTEM_VIDEO_INPUTS_CHANNELS_MOTION_DETECTION.format(
            id=channel_id
        )
        try:
            root = await client.get_xml(path)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            # 403 on 176.65, 404 on firmwares without the endpoint. Not a
            # warning: half the fleet legitimately lacks it.
            _LOGGER.debug(
                "%s motionDetection ch %s unavailable: %s",
                self._host, channel_id, exc,
            )
            return

        parsed = _caps.parse_motion_detection(root)
        if parsed is None:
            return
        # Keep the raw document so a later PUT can preserve gridMap /
        # samplingInterval / trigger times that this integration does not
        # model but must not destroy.
        try:
            raw = ET.tostring(root, encoding="unicode")
        except Exception:  # noqa: BLE001 - raw is best-effort
            raw = ""
        parsed["_raw_xml"] = raw
        self.motion_detection[channel_id] = parsed

    async def async_refresh_motion_detection(
        self, channel_id: str | None = None
    ) -> None:
        """Re-read motionDetection config after a PUT, then wake platforms.

        The switch and number entities call this so their displayed value
        reflects what the device actually accepted rather than the
        optimistic guess. Re-reading also refreshes ``_raw_xml``, which is
        what the next PUT will use as its base document — skipping this
        would mean a later write is built from a stale document and could
        revert an out-of-band change the user made in the camera's own web
        UI.

        ``channel_id=None`` re-probes every known channel.
        """
        ids = (
            [str(channel_id)] if channel_id is not None
            else list(self.motion_detection)
        )
        if not ids:
            return
        try:
            async with self._make_client() as client:
                for cid in ids:
                    await self._probe_motion_channel(client, cid)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.debug(
                "%s motion-detection readback failed: %s", self._host, exc
            )
            return
        self.async_update_listeners()

    # ---- v0.8: cross-entry duplicate detection ----

    def publish_identity(self) -> None:
        """Write this device's serial number into the config entry.

        Other entries read it back via ``dedup.collect_other_identities``
        to recognise that an NVR channel and a separately-added IPC are
        the same camera. Without publishing, dedup can never match — the
        NVR would have nothing to compare its channels against.

        No-ops until ``deviceInfo`` has actually been parsed: writing an
        empty serial would clobber a previously recorded identity and
        silently break dedup on the next refresh.
        """
        if self._identity_published or self.entry is None:
            return
        serial = str((self.device_info or {}).get("serialNumber") or "").strip()
        if not serial:
            return
        try:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    "identity_serial": serial,
                    "identity_model": str(
                        (self.device_info or {}).get("model") or ""
                    ).strip(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - dedup is best-effort
            _LOGGER.debug(
                "%s could not publish identity to config entry: %s",
                self._host, exc,
            )
            return
        self._identity_published = True
        _LOGGER.debug("%s published device identity for dedup", self._host)

    def apply_dedup(self) -> None:
        """Recompute ``duplicate_channels`` and wake platforms if it changed.

        Called after each refresh (channels and other entries' identities
        can both appear late). Wake listeners only when the result actually
        changes — this runs every 30 s, and re-firing on an unchanged set
        would churn every entity on every poll.
        """
        self.publish_identity()

        others = _dedup.collect_other_identities(self.hass, self.entry_id)
        duplicates = _dedup.find_duplicate_channels(self.channels, others)

        # Signature covers both inputs: our channels and the identities we
        # matched against. Either changing must re-evaluate.
        signature = (
            tuple(sorted(duplicates)),
            tuple(sorted((o.get("serial_number"), o.get("host")) for o in others)),
            tuple(sorted(str(c.get("id", "")) for c in self.channels)),
        )
        if signature == self._dedup_applied_signature:
            return
        changed = duplicates != self.duplicate_channels
        self._dedup_applied_signature = signature
        self.duplicate_channels = duplicates

        if not changed:
            return

        if duplicates:
            report = _dedup.describe_duplicates(self.channels, others)
            _LOGGER.info(
                "%s: %d channel(s) are the same hardware as a separately "
                "configured device; their NVR-side entities are disabled by "
                "default (enable them in the entity registry if you want the "
                "NVR view). %s",
                self._host, len(duplicates),
                "; ".join(
                    f"ch{r['channel_id']} {r['channel_name']} = {r['duplicate_of']}"
                    for r in report[:12]
                ),
            )
        else:
            _LOGGER.info(
                "%s: no cross-entry duplicate channels detected", self._host
            )
        self.async_update_listeners()

    def is_duplicate_channel(self, channel_id: Any) -> bool:
        """Whether a channel's entities should be disabled by default."""
        return _dedup.should_disable_channel(channel_id, self.duplicate_channels)

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

    async def _fetch_capabilities(
        self, client: ISAPIClient,
    ) -> dict[str, Any]:
        """``GET /ISAPI/System/capabilities`` (v0.6.28).

        Cross-checks device classification independently of the
        ``deviceType`` field returned by ``/deviceInfo``. The
        endpoint usually succeeds even when ``/System/status``
        returns ``notSupport`` (V4 NVRs), so we can use its
        ``status_supported`` flag to mark the system_status
        sensors as unknown instead of bogus zeros.

        Returns an empty dict on failure — callers must handle
        missing keys gracefully.
        """
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_CAPABILITIES)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            level = (
                _LOGGER.info if exc.__class__ is ISAPIError else _LOGGER.warning
            )
            level(
                "%s /ISAPI/System/capabilities failed: %s — capabilities "
                "stays empty; refresh continues normally.",
                self._host, exc,
            )
            return {}
        return _parse_capabilities(xml)

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

    async def _fetch_recording(
        self,
        client: ISAPIClient,
        channels: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch per-channel recording activity via segment search.

        v0.8. This is the ONLY recording-state source on the fleet —
        ``/ContentMgmt/Recording/channels/{i}/status`` is 403/404 on all
        12 devices, and ``InputProxy/.../status`` carries no record
        field. We instead POST a ``CMSearchDescription`` to
        ``/ContentMgmt/search`` and interpret the returned recording
        segments (see ``capabilities.derive_recording_status`` for why
        the verdict is *derived* and lags a real stop by up to one
        segment length).

        One batched POST covers every channel. trackIDs are generated
        from the exact channel count — a stray out-of-range trackID makes
        some firmwares reject the *whole* request with 400. ``maxResults``
        scales to ``2 × channels`` so no channel is truncated away
        (probed: 13 tracks + maxResults=13 returned only 12).

        Degrades to ``{}`` (→ no recording entities) on: no channels,
        endpoint 403 (176.51/52/53), 400 (bad trackID on some firmware),
        connection error, or a NO MATCHES response (device has no storage
        or recording is not configured). Never raises — a recording probe
        failure must not take the whole refresh down.
        """
        # trackIDs only make sense for channels with a numeric id.
        track_ids: list[str] = []
        for ch in channels:
            cid = str(ch.get("id", "")).strip()
            if cid.isdigit():
                track_ids.append(f"{cid}01")
        if not track_ids:
            return {}

        body = _caps.build_search_body(
            track_ids,
            window_minutes=_caps.SEARCH_WINDOW_MINUTES,
            max_results=_caps.search_max_results(len(track_ids)),
            now=now,
        )
        try:
            root = await client.post_xml(ISAPI_CONTENT_MGMT_SEARCH, body)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            # 403 / 400 / network — this device simply has no usable
            # recording-search endpoint. INFO once so it shows in the
            # diagnostic summary; not a warning, it's expected on IPCs
            # and older firmwares.
            _LOGGER.info(
                "%s recording search unavailable (%s); no recording "
                "entities for this device", self._host, exc,
            )
            return {}

        if not _caps.recording_available(root):
            return {}
        segments = _caps.parse_recording_segments(root)
        return _caps.derive_recording_status(segments, now=now)

    async def _fetch_network_interfaces(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        """Fetch network interfaces XML.

        v0.6.32: try multiple endpoint variants. Some V4 firmwares
        expose the interface list at a lowercase path
        (``/ISAPI/System/network/interfaces``) or at the ContentMgmt
        variant; the canonical ``/System/Network/interfaces`` returns
        404 on those. Without fallback, dual-NIC NVRs would only ever
        surface one NIC.
        """
        endpoints = (
            ISAPI_SYSTEM_NETWORK_INTERFACES,  # /System/Network/interfaces
            "/ISAPI/System/network/interfaces",  # lowercase n
            "/ISAPI/Networking/interfaces",
            "/ISAPI/System/NetworkInterface",
        )
        last_exc: Exception | None = None
        for endpoint in endpoints:
            try:
                xml = await client.get_xml(endpoint)
                if endpoint != ISAPI_SYSTEM_NETWORK_INTERFACES:
                    _LOGGER.info(
                        "%s network interfaces: using fallback endpoint %s "
                        "(primary %s unavailable)",
                        self._host, endpoint, ISAPI_SYSTEM_NETWORK_INTERFACES,
                    )
                return xml
            except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
                last_exc = exc
                continue
        _LOGGER.info(
            "%s /ISAPI/System/Network/interfaces: all fallback endpoints "
            "failed (last: %s); network_interfaces stays empty.",
            self._host, last_exc,
        )
        return None

    async def _fetch_streaming(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        """``GET /ISAPI/Streaming/channels`` for per-channel streaming
        detail (codec / resolution / framerate / audio).

        v0.6.22: changed from ``/Streaming/channels/1`` (per-channel
        config endpoint, returned a single ``<StreamingChannel>``
        without the nested ``<Video>``/``<Audio>`` blocks on V5 IPC)
        to the LIST endpoint ``/Streaming/channels`` (returns
        ``<StreamingChannelList><StreamingChannel>...</StreamingChannel>
        </StreamingChannelList>``). The list endpoint has the nested
        ``<Video>``/``<Audio>`` blocks per channel, which is what
        ``_parse_streaming_detail`` reads.

        Pre-v0.6.22 the per-channel endpoint returned XML without
        nested video blocks → every codec/resolution/frame-rate/
        audio sensor showed "unknown" on the user's V5 IPC
        (``DS-FB2127``) even though the data was available on the
        list endpoint.
        """
        try:
            return await client.get_xml(ISAPI_STREAMING_CHANNELS)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s %s failed: %s",
                self._host, ISAPI_STREAMING_CHANNELS, exc,
            )
            return None

    async def _fetch_time(
        self, client: ISAPIClient,
    ) -> ET.Element | None:
        """``GET /ISAPI/System/time`` for the device clock + NTP info.

        V5.x ``<Time>`` XML::

            <Time>
              <timeMode>NTP</timeMode>             (NTP / manual)
              <localTime>2026-09-25T08:27:27+08:00</localTime>
              <timeZone>CST-8:00:00</timeZone>
            </Time>

        V4 firmwares may use slightly different tag casing
        (``<TimeMode>``) — we accept both via direct lookup. Failure
        here is informational (not fatal); on success, the
        ``time_mode`` sensor populates with ``NTP`` / ``manual``
        etc.
        """
        try:
            return await client.get_xml(ISAPI_SYSTEM_TIME)
        except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
            _LOGGER.info(
                "%s /ISAPI/System/time failed: %s",
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
                # v0.6.28: capability probe (independent of /status
                # so a status notSupport failure doesn't hide the
                # device's actual capabilities).
                system_capabilities = await self._fetch_capabilities(client)
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
            # v0.6.22: _fetch_time was previously called AFTER the
            # `async with` block, by which point ``client`` had been
            # closed via ``__aexit__`` → ``aclose()``. The closed
            # httpx session raised errors that ``_fetch_time``'s
            # try/except caught (silent), so time_mode always
            # returned None → "unknown". Move the call inside the
            # block while we still have a live client.
            time_xml = await self._fetch_time(client)

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
        # v0.6.18: parse richer per-channel streaming detail
        # (codec, resolution, frame rate, audio). Same XML feeds
        # both this and the bitrate dict above.
        streaming_channel_detail = _parse_streaming_detail(streaming_xml)
        # v0.6.19: device clock / NTP / timezone info.
        time_info = _parse_time(time_xml)

        # v0.8: per-channel recording activity via segment search.
        # Runs in its own client block (the main fetch block's client is
        # already closed) and needs ``channels`` to build trackIDs, so it
        # goes after channel parsing. Best-effort: ``_fetch_recording``
        # swallows 403/400/network errors and returns {} so a device
        # without a usable search endpoint keeps the rest of its data.
        recording_status: dict[str, Any] = {}
        if channels:
            try:
                async with self._make_client() as client:
                    recording_status = await self._fetch_recording(client, channels)
            except (ISAPIError, ISAPIAuthError, ISAPIConnectionError) as exc:
                _LOGGER.info(
                    "%s recording search failed (%s); no recording entities",
                    self._host, exc,
                )

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
                # v0.8: merge rules moved into _merge_channel_status so
                # they are unit-testable (they previously lived inline in
                # this 400-line method where no test could reach them —
                # which is exactly how the IPC "online" regression hid:
                # the session-list endpoint has no <online>, so the parser
                # returned False and this line clobbered the correct
                # enabled=true from the channel list). Tri-state now:
                # None from the status endpoint never overwrites a value
                # the channel list already supplied.
                _merge_channel_status(ch, ch_status)
            except ISAPIError as exc:
                _LOGGER.debug(
                    "Per-channel status parse failed for %s ch %s: %s",
                    self._host, ch_id, exc,
                )

        # v0.7.1: PTZ detection. Pre-v0.7.1 this was::
        #
        #     "ptz": device_info.get("deviceType", "").lower()
        #            in {"ptz", "ptzdome"}
        #
        # but no Hikvision firmware reports ``deviceType`` as literally
        # "ptz"/"ptzdome" — the user's fleet returns ``IPZoom`` (DS-FB2127
        # V5.2.2), ``DVR`` (DS-7708N-I4 V4.1.18) and ``NVR``
        # (DS-7804N-R2/4P(C) V4.84.031). The comparison therefore never
        # matched, ``capabilities["ptz"]`` was permanently False, and both
        # consumers stayed dead on every device:
        #   - button.py:78   → PTZ direction buttons never registered
        #   - __init__.py:80 → ptz_goto_preset service never registered
        #
        # Authoritative signal is ``<PTZCtrlCap>`` in /System/capabilities,
        # already parsed into ``system_capabilities["ptz"]``. The deviceType
        # string comparison is kept only as a fallback for firmwares that
        # omit PTZCtrlCap.
        #
        # Note this is deliberately NOT based on "device lists presets":
        # the DS-FB2127 (deviceType ``IPZoom``) returns 10 presets yet
        # answers ``methodNotAllowed`` on /PTZCtrl/channels/01/continuous and
        # omits PTZCtrlCap — it can read the preset table but cannot be
        # driven. For that reason ``ipzoom`` is NOT in the fallback set;
        # PTZCtrlCap alone decides it, and it correctly evaluates False.
        _device_type_lower = device_info.get("deviceType", "").lower()
        capabilities: dict[str, bool] = {
            "ptz": bool(system_capabilities.get("ptz"))
            or _device_type_lower in {"ptz", "ptzdome"},
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
        self.streaming_channel_detail = streaming_channel_detail
        self.time_info = time_info
        # v0.6.28: store capability probe result on the coordinator
        # instance for diagnostic logging + the
        # ``capability_video_input_channels`` sensor.
        self.system_capabilities = system_capabilities
        # v0.8: cache recording status on the instance so the
        # binary_sensor / sensor platforms can read it during late
        # entity registration (mirrors self.storage).
        self.recording_status = recording_status

        return HikvisionISAPIData(
            device_info=device_info,
            system_status=system_status,
            channels=channels,
            capabilities=capabilities,
            storage=storage,
            network_interfaces=network_interfaces,
            streaming_bitrate_kbps=streaming_bitrate_kbps,
            streaming_channel_detail=streaming_channel_detail,
            time_info=time_info,
            system_capabilities=system_capabilities,
            recording_status=recording_status,
        )
