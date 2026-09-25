"""Sensor platform for Hikvision ISAPI.

Exposes device_info and system_status as sensors. The mapping is
fixed in ``SENSORS`` — the user can rename / re-unit / hide any of
them in the HA UI.

v0.6.17 cleanup (per user feedback after v0.6.16):

- ``cpu_usage`` reads ``cpuUtilization`` (matches what the
  coordinator stores; pre-v0.6.17 this was the wrong key
  ``cpuUsage`` and the sensor always showed "unknown").
- ``memory_usage`` is now a *percentage* — pre-v0.6.17 the sensor
  read raw MB and HA rendered it under ``PERCENTAGE`` unit,
  producing absurd values like "728%".
- All uptime sensors now display in **hours** (rounded to 1
  decimal). The seconds-based ``uptime`` and ``device_uptime``
  sensors were deleted (they duplicated the hours version and
  weren't useful at human-readable precision).
- The 4 storage sensors were deleted — on the user's V4 NVR
  (DS-7708N-I4 V4.1.18) the storage endpoint family returns
  ``<ResponseStatus>`` 4 "Invalid Operation" for every variant,
  so these would always show "unknown" on this device. Storage
  on other (V5) firmwares isn't covered by the user's fleet
  but can be added in a future release if needed.
- New sensors exposed from data we already parse but didn't
  publish: ``device_mac`` (from deviceInfo, distinct from
  network's per-NIC MAC), ``device_type``, ``device_id``,
  ``firmware_release_date``, ``encoder_version``,
  ``encoder_release_date``. Useful for tracking the camera
  fleet's firmware / encoder versions without needing a
  browser tab open to each device's web UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HikvisionISAPIData, HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HikvisionISAPISensorDescription(SensorEntityDescription):
    """A sensor description for ISAPI-derived values."""

    value_fn: Any  # Callable[[HikvisionISAPIData], Any]


SENSORS: tuple[HikvisionISAPISensorDescription, ...] = (
    # ---- device identity (from /ISAPI/System/deviceInfo) ----
    HikvisionISAPISensorDescription(
        key="model",
        translation_key="model",
        name="型号",
        icon="mdi:information-outline",
        value_fn=lambda d: d.device_info.get("model", ""),
    ),
    HikvisionISAPISensorDescription(
        key="serial_number",
        translation_key="serial_number",
        name="序列号",
        icon="mdi:barcode",
        value_fn=lambda d: d.device_info.get("serialNumber", ""),
    ),
    HikvisionISAPISensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="固件版本",
        icon="mdi:chip",
        value_fn=lambda d: d.device_info.get("firmwareVersion", ""),
    ),
    HikvisionISAPISensorDescription(
        key="firmware_release_date",
        translation_key="firmware_release_date",
        name="固件发布日期",
        icon="mdi:calendar",
        # V5 firmware: "build 240522"; V4: same format.
        value_fn=lambda d: d.device_info.get("firmwareReleasedDate", ""),
    ),
    HikvisionISAPISensorDescription(
        key="device_type",
        translation_key="device_type",
        name="设备类型",
        icon="mdi:devices",
        # Direct from deviceInfo; values like "IPCamera",
        # "NetworkVideoRecorder", "DVR". (V5.x IPC may return
        # "IPZoom" or other product-line-specific strings.)
        value_fn=lambda d: d.device_info.get("deviceType", ""),
    ),
    HikvisionISAPISensorDescription(
        key="device_id",
        translation_key="device_id",
        name="设备 ID",
        icon="mdi:identifier",
        # UUID-style device ID from <deviceID>. Useful for
        # distinguishing physically-identical units in dashboards.
        value_fn=lambda d: d.device_info.get("deviceID", ""),
    ),
    HikvisionISAPISensorDescription(
        key="device_mac",
        translation_key="device_mac",
        name="设备 MAC",
        icon="mdi:network",
        # Direct from deviceInfo, distinct from the per-NIC MAC
        # reported by /System/Network/interfaces. On the user's
        # dual-NIC NVR these may differ; the deviceInfo MAC is
        # the canonical hardware address.
        value_fn=lambda d: d.device_info.get("macAddress", ""),
    ),
    HikvisionISAPISensorDescription(
        key="encoder_version",
        translation_key="encoder_version",
        name="编码器版本",
        icon="mdi:codec",
        # V4 firmware has this; V5 may omit (returns "").
        value_fn=lambda d: d.device_info.get("encoderVersion", ""),
    ),
    HikvisionISAPISensorDescription(
        key="encoder_release_date",
        translation_key="encoder_release_date",
        name="编码器发布日期",
        icon="mdi:calendar",
        value_fn=lambda d: d.device_info.get("encoderReleasedDate", ""),
    ),
    # ---- system status (from /ISAPI/System/status) ----
    HikvisionISAPISensorDescription(
        key="device_status",
        translation_key="device_status",
        name="设备状态",
        icon="mdi:check-circle",
        value_fn=lambda d: d.system_status.get("deviceStatus", "Unknown"),
    ),
    HikvisionISAPISensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        name="CPU 使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chip",
        # v0.6.17 fix: reads ``cpuUtilization`` (matching the key
        # the coordinator stores). Pre-v0.6.17 this was the wrong
        # key ``cpuUsage`` and the sensor always showed "unknown".
        value_fn=lambda d: _safe_int(d.system_status.get("cpuUtilization")),
    ),
    HikvisionISAPISensorDescription(
        key="memory_usage_percent",
        translation_key="memory_usage",
        name="内存使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        # v0.6.17 fix: was reading raw MB (728.234375 on V4 NVR)
        # and HA rendered it as PERCENTAGE → "728%". Now compute
        # percentage from ``memoryUsage / (memoryUsage +
        # memoryAvailable)``. Pre-v0.6.17 this was the
        # misnamed-and-miscomputed ``memory_usage`` sensor.
        value_fn=lambda d: _memory_usage_percent(d),
    ),
    HikvisionISAPISensorDescription(
        key="memory_available_mb",
        translation_key="memory_available",
        name="内存剩余 (MB)",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        # Raw available memory in MB, useful for monitoring
        # trend (capacity-constrained IPCs).
        value_fn=lambda d: _safe_int(d.system_status.get("memoryAvailable")),
    ),
    HikvisionISAPISensorDescription(
        key="uptime_hours",
        translation_key="uptime",
        name="运行时长",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:clock-outline",
        # v0.6.17: only sensor exposing runtime. Hours, rounded
        # to 1 decimal. Pre-v0.6.17 there were two seconds-based
        # sensors (``uptime`` / ``device_uptime``) which were
        # deleted because human-readable values are more useful
        # for runtime tracking.
        value_fn=lambda d: _uptime_hours(d.system_status.get("uptime")),
    ),
    # ---- channels (only populates if device exposes channel list) ----
    HikvisionISAPISensorDescription(
        key="channel_count",
        translation_key="channel_count",
        name="通道数",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=lambda d: len(d.channels),
    ),
    # ---- network (first interface) ----
    HikvisionISAPISensorDescription(
        key="network_ip",
        translation_key="network_ip",
        name="IP 地址",
        icon="mdi:ip",
        value_fn=lambda d: _first_iface(d, "ip_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_subnet",
        translation_key="network_subnet",
        name="子网掩码",
        icon="mdi:subnet",
        value_fn=lambda d: _first_iface(d, "subnet_mask"),
    ),
    HikvisionISAPISensorDescription(
        key="network_gateway",
        translation_key="network_gateway",
        name="默认网关",
        icon="mdi:router-network",
        value_fn=lambda d: _first_iface(d, "default_gateway"),
    ),
    HikvisionISAPISensorDescription(
        key="network_mac",
        translation_key="network_mac",
        name="网卡 MAC",
        icon="mdi:network",
        # v0.6.17 fix: V4 NVR nests MAC inside <Link>, V5 puts
        # it directly. coord's _parse_network_interfaces now
        # tries both paths so this sensor populates on both
        # firmware generations.
        value_fn=lambda d: _first_iface(d, "mac_address"),
    ),
    # ---- storage (re-added in v0.6.18) ----
    # Storage endpoints are 4xx on the user's V4 NVR
    # (DS-7708N-I4 V4.1.18) but work on V5 NVRs and modern
    # Hikvision DVRs. Keep these sensors so users with working
    # firmware see real values; V4 users continue to see
    # "unknown" gracefully.
    HikvisionISAPISensorDescription(
        key="storage_total_gb",
        translation_key="storage_total",
        name="存储总量 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        # Display in GB (HA's preferred unit for HDD sizes);
        # MB internally for cross-firmware compatibility.
        value_fn=lambda d: _mb_to_gb(d.storage.get("total_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_used_gb",
        translation_key="storage_used",
        name="存储已用 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("used_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_free_gb",
        translation_key="storage_free",
        name="存储剩余 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("free_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_usage_percent",
        translation_key="storage_usage",
        name="存储使用率",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk",
        value_fn=lambda d: _storage_usage_pct(d.storage),
    ),
    # ---- video encoding / streaming detail (v0.6.18) ----
    # Per-channel codec, resolution, frame rate. Populated from
    # ``/ISAPI/Streaming/channels`` (V5 IPCs). V4 NVR firmware
    # mostly returns 4 on this endpoint so these sensors stay
    # "unknown" on the user's NVR fleet.
    HikvisionISAPISensorDescription(
        key="channel_1_video_codec",
        translation_key="channel_1_video_codec",
        name="通道 1 视频编码",
        icon="mdi:codec",
        value_fn=lambda d: d.streaming_channel_detail.get("video_codec"),
    ),
    HikvisionISAPISensorDescription(
        key="channel_1_video_resolution",
        translation_key="channel_1_video_resolution",
        name="通道 1 分辨率",
        icon="mdi:aspect-ratio",
        value_fn=lambda d: d.streaming_channel_detail.get("video_resolution"),
    ),
    HikvisionISAPISensorDescription(
        key="channel_1_video_frame_rate",
        translation_key="channel_1_video_frame_rate",
        name="通道 1 帧率",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="fps",
        icon="mdi:speedometer",
        # Hikvision reports maxFrameRate in hundredths-of-fps
        # (1600 = 16 fps, 3000 = 30 fps); _parse_streaming_detail
        # has already divided by 100.
        value_fn=lambda d: d.streaming_channel_detail.get("video_frame_rate"),
    ),
    HikvisionISAPISensorDescription(
        key="channel_1_video_bitrate",
        translation_key="channel_1_video_bitrate",
        name="通道 1 码率",
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kbps",
        icon="mdi:video",
        value_fn=lambda d: d.streaming_channel_detail.get("video_bitrate_kbps"),
    ),
    HikvisionISAPISensorDescription(
        key="channel_1_audio_codec",
        translation_key="channel_1_audio_codec",
        name="通道 1 音频编码",
        icon="mdi:music-clef",
        value_fn=lambda d: d.streaming_channel_detail.get("audio_codec"),
    ),
    # ---- reboot count (V4 NVR doesn't return this; V5 IPC does) ----
    HikvisionISAPISensorDescription(
        key="reboot_count",
        translation_key="reboot_count",
        name="重启次数",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:restart",
        value_fn=lambda d: _safe_int(d.system_status.get("rebootCount")),
    ),
)


# ---- helpers ----


def _safe_int(value: Any) -> int | None:
    """Best-effort int parsing; returns ``None`` for missing / non-int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mb_to_gb(mb: Any) -> float | None:
    """Convert MB → GB (decimal, 1 GB = 10^3 MB) for HDD display.

    Pre-v0.6.17 this existed. v0.6.18 re-adds it for the storage
    sensors. Returns ``None`` for missing / non-numeric MB so the
    entity shows "unknown" rather than "0 GB" with no data.
    """
    if mb is None:
        return None
    try:
        return round(float(mb) / 1024.0, 1)
    except (TypeError, ValueError):
        return None


def _storage_usage_pct(storage: dict[str, Any]) -> float | None:
    """Compute used/total percentage for storage.

    Returns ``None`` when total/used aren't both populated (e.g.
    the device doesn't expose storage endpoints) so the entity
    shows "unknown" rather than "0%" placeholder values.
    """
    total = storage.get("total_mb")
    used = storage.get("used_mb")
    if total is None or used is None or total <= 0:
        return None
    return round(used * 100 / total, 1)


def _memory_usage_percent(data: HikvisionISAPIData) -> float | None:
    """Compute memory usage percentage from ``memoryUsage`` and
    ``memoryAvailable``.

    Both fields are in MB. Memory percentage isn't a Hikvision
    device-reported metric — it's ``used / (used + available) * 100``
    rounded to 1 decimal. Returns ``None`` if either field is
    missing (sensor shows "unknown" instead of "0%" with no data).
    """
    used = _safe_int(data.system_status.get("memoryUsage"))
    available = _safe_int(data.system_status.get("memoryAvailable"))
    if used is None or available is None:
        return None
    total = used + available
    if total <= 0:
        return None
    return round(used * 100 / total, 1)


def _uptime_hours(value: Any) -> float | None:
    """Convert uptime seconds to hours, rounded to 1 decimal.

    Pre-v0.6.17 this was hidden behind a seconds-based sensor
    pair the user didn't want. Now it's the only uptime sensor,
    presented in human-friendly hours.
    """
    secs = _safe_int(value)
    if secs is None:
        return None
    return round(secs / 3600, 1)


def _first_iface(data: HikvisionISAPIData, field: str) -> str | None:
    """Return ``field`` from the first network interface, or None."""
    ifaces = data.network_interfaces
    if not ifaces:
        return None
    value = ifaces[0].get(field)
    return value or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up static ISAPI sensors (model, firmware, cpu, memory, etc.)."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        HikvisionISAPISensor(coordinator, entry, desc) for desc in SENSORS
    ]
    async_add_entities(entities)


class HikvisionISAPISensor(HikvisionISAPIEntity, SensorEntity):
    """Static ISAPI sensor entity."""

    entity_description: HikvisionISAPISensorDescription

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        description: HikvisionISAPISensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
