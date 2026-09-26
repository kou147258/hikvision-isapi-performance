"""DataUpdateCoordinator for Hikvision ISAPI Performance."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISAPI_CONTENT_MGMT_INPUT_PROXY_CHANNELS,
    ISAPI_CONTENT_MGMT_STORAGE,
    ISAPI_CONTENT_MGMT_STORAGE_HDDLIST,
    ISAPI_NETWORK_INTERFACES,
    ISAPI_NETWORK_INTERFACES_ALT,
    ISAPI_PICTURE,
    ISAPI_STREAMING_CHANNELS,
    ISAPI_SYSTEM_CAPABILITIES,
    ISAPI_SYSTEM_DEVICE_INFO,
    ISAPI_SYSTEM_STATUS,
    ISAPI_SYSTEM_STORAGE_HARDDISKS,
    ISAPI_SYSTEM_TIME,
)
from .isapi_client import (
    ISAPIAuthError,
    ISAPIClient,
    ISAPIConnectionError,
    ISAPIError,
)

_LOGGER = logging.getLogger(__name__)


def _or_none(value: Any) -> str | None:
    """Return None for empty strings, otherwise the value as str."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _safe_int(value: Any, default: int = 0) -> int:
    """Parse an integer from a possibly-decimal or whitespace-padded string."""
    if value is None:
        return default
    try:
        return round(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Parse a float from a string."""
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def normalize_device_type(raw: str) -> str:
    """Normalize Hikvision deviceType to lowercase canonical form."""
    if not raw:
        return ""
    lower = raw.strip().lower()
    if "nvr" in lower:
        return "nvr"
    if "dvr" in lower:
        return "dvr"
    if "ipc" in lower or "camera" in lower:
        return "ipcamera"
    return lower


def _memory_usage_percent(used_mb: float, available_mb: float) -> float | None:
    """Compute memory usage percentage from used and available MB."""
    total = used_mb + available_mb
    if total <= 0:
        return None
    return round(used_mb / total * 100, 1)


class HikvisionISAPIData:
    """Container for parsed ISAPI data."""

    def __init__(self) -> None:
        self.device_info: dict[str, Any] = {}
        self.system_status: dict[str, Any] = {}
        self.channels: list[dict[str, Any]] = []
        self.capabilities: dict[str, Any] = {}
        self.storage: dict[str, Any] = {}
        self.network_interfaces: list[dict[str, Any]] = []
        self.streaming_channel_detail: dict[int, dict[str, Any]] = {}
        self.time_info: dict[str, Any] = {}

    # [FIX #1] Add dict-like access so sensor/binary_sensor code that uses
    # data.get("key") works correctly. HikvisionISAPIData is stored as
    # coordinator.data and accessed like a dict throughout the codebase.
    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style get for backward compatibility."""
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


class HikvisionISAPICoordinator(DataUpdateCoordinator[HikvisionISAPIData]):
    """Coordinator that polls Hikvision ISAPI endpoints."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        use_https: bool = False,
        verify_ssl: bool = False,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.device_type: str = ""
        self.network_interfaces: list[dict[str, Any]] = []
        self.capabilities: dict[str, Any] = {}
        # [FIX #2] Set unique_id so entities can build stable unique IDs.
        # Format: DOMAIN + host + port to distinguish multiple devices.
        self.unique_id = f"{DOMAIN}_{host}_{port}"
        self._hikvision_isapi_performance_binary_added = False
        self._hikvision_isapi_performance_camera_added = False
        self._hikvision_isapi_performance_switch_added = False

    def _make_client(self) -> ISAPIClient:
        return ISAPIClient(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_https=self.use_https,
            verify_ssl=self.verify_ssl,
        )

    # [FIX #9] Add update_scan_interval method so options flow works.
    def update_scan_interval(self, new_interval: int) -> None:
        """Update the polling interval (called from options flow)."""
        self.update_interval = timedelta(seconds=new_interval)

    # ── Parsers ──────────────────────────────────────────────

    @staticmethod
    def _parse_device_info(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        return {
            "deviceName": _or_none(root.findtext("deviceName")),
            "model": _or_none(root.findtext("model")),
            "serialNumber": _or_none(root.findtext("serialNumber")),
            "firmwareVersion": _or_none(root.findtext("firmwareVersion")),
            "firmwareReleasedDate": _or_none(
                root.findtext("firmwareReleasedDate")
            ),
            "deviceType": _or_none(root.findtext("deviceType")),
            "deviceID": _or_none(root.findtext("deviceID")),
            "macAddress": _or_none(root.findtext("macAddress")),
            "encoderVersion": _or_none(root.findtext("encoderVersion")),
            "channelCount": _safe_int(root.findtext("videoInputChannelNums")),
        }

    @staticmethod
    def _parse_system_status(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        mem_usage_raw = root.findtext("memoryUsage")
        mem_avail_raw = root.findtext("memoryAvailable")
        mem_usage = _safe_float(mem_usage_raw)
        mem_avail = _safe_float(mem_avail_raw)

        # V5 IPC heuristic: if available > usage * 50, available is in KB
        if mem_usage > 0 and mem_avail > mem_usage * 50:
            mem_avail = mem_avail / 1024

        return {
            "cpuUtilization": _safe_int(root.findtext("cpuUtilization")),
            "memoryUsage": mem_usage,
            "memoryAvailable": mem_avail,
            "uptime": _or_none(root.findtext("upTime")),
        }

    @staticmethod
    def _parse_channels(xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        channels: list[dict[str, Any]] = []
        for ch in root.iter("InputProxyChannel"):
            # [FIX #12] Check the actual value of recordMode, not just
            # whether the element exists. Hikvision returns "Manual" or
            # "Schedule" for recording, None/empty for not recording.
            record_mode = ch.findtext("recordMode")
            channels.append({
                "id": _safe_int(ch.findtext("id")),
                "name": _or_none(ch.findtext("name")),
                "online": ch.findtext("online") == "true",
                "recording": record_mode is not None and record_mode.strip() != "",
            })
        return channels

    @staticmethod
    def _parse_streaming_channels_list(
        xml_text: str,
    ) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        channels: list[dict[str, Any]] = []
        for ch in root.iter("StreamingChannel"):
            ch_id = _safe_int(
                ch.findtext("id") or ch.findtext("videoInputChannelID")
            )
            channels.append({
                "id": ch_id,
                "name": _or_none(
                    ch.findtext("channelName")
                    or ch.findtext("name")
                ),
                "online": ch.findtext("enabled") == "true",
                "recording": False,
            })
        return channels

    @staticmethod
    def _parse_storage(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)

        # V5 shape: <Storage><totalCapacity>MB</totalCapacity>...
        total_cap = root.findtext("totalCapacity")
        free_cap = root.findtext("freeSpace") or root.findtext("freeCapacity")
        if total_cap is not None:
            total_mb = _safe_int(total_cap)
            free_mb = _safe_int(free_cap)
            used_mb = total_mb - free_mb if free_mb is not None else None
            return {
                "total_mb": total_mb,
                "free_mb": free_mb,
                "used_mb": used_mb,
            }

        # V5 IPC lowercase: <hdd><capacity>MB</capacity><freeSpace>MB</freeSpace>
        hdd = root.find("hdd")
        if hdd is not None:
            cap = hdd.findtext("capacity")
            free = hdd.findtext("freeSpace")
            if cap is not None:
                total_mb = _safe_int(cap)
                free_mb = _safe_int(free)
                used_mb = total_mb - free_mb if free_mb is not None else None
                return {
                    "total_mb": total_mb,
                    "free_mb": free_mb,
                    "used_mb": used_mb,
                }

        # V4 shape: <Storage><hddList><HDD><size>BYTES</size>...</HDD>
        total_bytes = 0
        free_bytes = 0
        for hdd in root.iter("HDD"):
            size = _safe_int(hdd.findtext("size"))
            free = _safe_int(hdd.findtext("freeSpace") or hdd.findtext("freespace"))
            total_bytes += size
            free_bytes += free
        if total_bytes > 0:
            return {
                "total_mb": total_bytes // (1024 * 1024),
                "free_mb": free_bytes // (1024 * 1024),
                "used_mb": (total_bytes - free_bytes) // (1024 * 1024),
            }

        return {}

    @staticmethod
    def _parse_network_interfaces(
        xml_text: str,
    ) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        interfaces: list[dict[str, Any]] = []

        for iface in root.iter("NetworkInterface"):
            ip_field = None
            subnet_field = None
            gw_field = None

            # V4 nested: IPAddress/IPAddress
            ip_addr_elem = iface.find("IPAddress")
            if ip_addr_elem is not None:
                nested_ip = ip_addr_elem.findtext("IPAddress")
                if nested_ip:
                    ip_field = nested_ip
                    subnet_field = ip_addr_elem.findtext("subnetMask")
                    gw_field = ip_addr_elem.findtext("gateway")

            # V5 direct
            if not ip_field:
                ip_field = iface.findtext("IPAddress")
                subnet_field = iface.findtext("subnetMask")
                gw_field = iface.findtext("gateway")

            # MAC: V4 Link/MACAddress, V5 direct
            mac = iface.findtext("MACAddress")
            if not mac:
                link = iface.find("Link")
                if link is not None:
                    mac = link.findtext("MACAddress")

            # MTU: V5 NetworkInterface/MTU, V4 direct
            mtu = iface.findtext("MTU")
            if mtu is None:
                net = iface.find("NetworkInterface")
                if net is not None:
                    mtu = net.findtext("MTU")

            interfaces.append({
                "ip": _or_none(ip_field),
                "subnet": _or_none(subnet_field),
                "gateway": _or_none(gw_field),
                "mac": _or_none(mac),
                "mtu": _safe_int(mtu) if mtu else None,
            })

        return interfaces

    @staticmethod
    def _parse_capabilities(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        caps: dict[str, Any] = {}

        # PTZ capability
        ptz = root.find(".//PTZCtrlCap")
        if ptz is not None:
            caps["ptz"] = True

        # Video input channels
        vic = root.findtext("videoInputChannelNums")
        if vic is None:
            # V5 IPC: VideoCap/videoInputPortNums
            vcap = root.find("VideoCap")
            if vcap is not None:
                vic = vcap.findtext("videoInputPortNums")
        if vic is not None:
            caps["video_input_channels"] = _safe_int(vic)

        return caps

    @staticmethod
    def _parse_time(xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        mode = root.findtext("timeMode") or root.findtext("mode")
        return {"time_mode": _or_none(mode)}

    @staticmethod
    def _parse_streaming_detail(
        xml_text: str,
    ) -> dict[int, dict[str, Any]]:
        root = ET.fromstring(xml_text)
        details: dict[int, dict[str, Any]] = {}
        for ch in root.iter("StreamingChannel"):
            ch_id = _safe_int(
                ch.findtext("id") or ch.findtext("videoInputChannelID")
            )
            detail: dict[str, Any] = {}

            video = ch.find("Video")
            if video is not None:
                detail["video_codec"] = _or_none(
                    video.findtext("videoCodecType")
                )
                res_w = video.findtext("videoResolutionWidth")
                res_h = video.findtext("videoResolutionHeight")
                if res_w and res_h:
                    detail["video_resolution"] = f"{res_w}x{res_h}"
                fps = video.findtext("maxFrameRate")
                if fps:
                    detail["video_frame_rate"] = round(
                        _safe_float(fps) / 100, 1
                    )
                bitrate = video.findtext("constantBitRate") or video.findtext(
                    "videoAverageBitrate"
                )
                if bitrate:
                    detail["video_bitrate"] = _safe_int(bitrate)

            audio = ch.find("Audio")
            if audio is not None:
                detail["audio_codec"] = _or_none(
                    audio.findtext("audioCodecType")
                )

            if detail:
                details[ch_id] = detail

        return details

    # ── Fetch helpers ────────────────────────────────────────

    async def _fetch_device_info(
        self, client: ISAPIClient
    ) -> dict[str, Any]:
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_DEVICE_INFO)
            return self._parse_device_info(xml)
        except ISAPIError as err:
            _LOGGER.warning("%s: deviceInfo fetch failed: %s", self.host, err)
            return {}

    async def _fetch_system_status(
        self, client: ISAPIClient
    ) -> dict[str, Any]:
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_STATUS)
            return self._parse_system_status(xml)
        except ISAPIError as err:
            _LOGGER.info("%s: systemStatus fetch failed: %s", self.host, err)
            return {}

    async def _fetch_channels(
        self, client: ISAPIClient, device_type: str
    ) -> list[dict[str, Any]]:
        endpoints = (
            [ISAPI_STREAMING_CHANNELS, ISAPI_CONTENT_MGMT_INPUT_PROXY_CHANNELS]
            if device_type == "ipcamera"
            else [
                ISAPI_CONTENT_MGMT_INPUT_PROXY_CHANNELS,
                ISAPI_STREAMING_CHANNELS,
            ]
        )
        for ep in endpoints:
            try:
                xml = await client.get_xml(ep)
                root_tag = ET.fromstring(xml).tag
                if "StreamingChannelList" in root_tag:
                    return self._parse_streaming_channels_list(xml)
                return self._parse_channels(xml)
            except ISAPIError:
                continue
        _LOGGER.info("%s: channels endpoint unavailable", self.host)
        return []

    async def _fetch_storage(
        self, client: ISAPIClient
    ) -> dict[str, Any]:
        for ep in (
            ISAPI_CONTENT_MGMT_STORAGE,
            ISAPI_SYSTEM_STORAGE_HARDDISKS,
            ISAPI_CONTENT_MGMT_STORAGE_HDDLIST,
        ):
            try:
                xml = await client.get_xml(ep)
                result = self._parse_storage(xml)
                if result:
                    return result
            except ISAPIError:
                continue
        return {}

    async def _fetch_network(
        self, client: ISAPIClient
    ) -> list[dict[str, Any]]:
        for ep in (ISAPI_NETWORK_INTERFACES, ISAPI_NETWORK_INTERFACES_ALT):
            try:
                xml = await client.get_xml(ep)
                ifaces = self._parse_network_interfaces(xml)
                if ifaces:
                    return ifaces
            except ISAPIError:
                continue
        return []

    async def _fetch_capabilities(
        self, client: ISAPIClient
    ) -> dict[str, Any]:
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_CAPABILITIES)
            return self._parse_capabilities(xml)
        except ISAPIError as err:
            _LOGGER.info("%s: capabilities fetch failed: %s", self.host, err)
            return {}

    async def _fetch_time(self, client: ISAPIClient) -> dict[str, Any]:
        try:
            xml = await client.get_xml(ISAPI_SYSTEM_TIME)
            return self._parse_time(xml)
        except ISAPIError as err:
            _LOGGER.info("%s: time fetch failed: %s", self.host, err)
            return {}

    async def _fetch_streaming(
        self, client: ISAPIClient
    ) -> dict[int, dict[str, Any]]:
        # [FIX #11] Use ISAPI_STREAMING_CHANNELS directly (removed duplicate
        # ISAPI_STREAMING_CHANNELS_LIST constant that was identical).
        try:
            xml = await client.get_xml(ISAPI_STREAMING_CHANNELS)
            return self._parse_streaming_detail(xml)
        except ISAPIError as err:
            _LOGGER.info(
                "%s: streaming detail fetch failed: %s", self.host, err
            )
            return {}

    # ── Main refresh ─────────────────────────────────────────

    async def _async_update_data(self) -> HikvisionISAPIData:
        _LOGGER.info(
            "%s:%s refresh start (scheme=%s)",
            self.host,
            self.port,
            "https" if self.use_https else "http",
        )
        data = HikvisionISAPIData()

        try:
            async with self._make_client() as client:
                data.device_info = await self._fetch_device_info(client)
                if data.device_info:
                    self.device_type = normalize_device_type(
                        data.device_info.get("deviceType", "")
                    )

                data.system_status = await self._fetch_system_status(client)
                data.channels = await self._fetch_channels(
                    client, self.device_type
                )
                data.storage = await self._fetch_storage(client)
                data.network_interfaces = await self._fetch_network(client)
                self.network_interfaces = data.network_interfaces
                data.capabilities = await self._fetch_capabilities(client)
                self.capabilities = data.capabilities
                data.time_info = await self._fetch_time(client)
                data.streaming_channel_detail = (
                    await self._fetch_streaming(client)
                )
        except ISAPIConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err
        except ISAPIAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err

        summary_parts = [
            f"device_info={'OK' if data.device_info else 'missing'}",
            f"channels={len(data.channels)}",
            f"storage={'OK' if data.storage else 'missing'}",
            f"network={'OK' if data.network_interfaces else 'missing'}",
            f"time={'OK' if data.time_info else 'missing'}",
            f"streaming={len(data.streaming_channel_detail)}",
        ]
        _LOGGER.info("%s refresh summary: %s", self.host, " ".join(summary_parts))

        return data
