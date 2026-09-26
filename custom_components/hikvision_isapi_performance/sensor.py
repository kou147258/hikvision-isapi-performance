"""Sensor platform for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)


def _or_none(val: Any) -> str | None:
    """Return None for empty strings, otherwise str."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _first_iface(data: Any, field: str) -> str | None:
    """Get a field from the first network interface."""
    ifaces = data.network_interfaces if hasattr(data, 'network_interfaces') else []
    if not ifaces:
        return None
    return _or_none(ifaces[0].get(field))


def _second_iface(data: Any, field: str) -> str | None:
    """Get a field from the second network interface (if present)."""
    ifaces = data.network_interfaces if hasattr(data, 'network_interfaces') else []
    if len(ifaces) < 2:
        return None
    return _or_none(ifaces[1].get(field))


def _memory_usage_percent(data: Any) -> float | None:
    """Compute memory usage percentage from used + available MB."""
    ss = data.system_status if hasattr(data, 'system_status') else {}
    used = ss.get("memoryUsage")
    avail = ss.get("memoryAvailable")
    if used is None or avail is None:
        return None
    try:
        u = float(used)
        a = float(avail)
    except (ValueError, TypeError):
        return None
    total = u + a
    if total <= 0:
        return None
    return round(u / total * 100, 1)


def _storage_usage_pct(data: Any) -> float | None:
    """Compute storage usage percentage."""
    storage = data.storage if hasattr(data, 'storage') else {}
    total = storage.get("total_mb")
    used = storage.get("used_mb")
    if total is None or used is None or total == 0:
        return None
    return round(used / total * 100, 1)


# ── Always-on sensor descriptions ──────────────────────────────────────
ALWAYS_ON_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(key="model", translation_key="model"),
    SensorEntityDescription(key="serial_number", translation_key="serial_number"),
    SensorEntityDescription(key="firmware_version", translation_key="firmware_version"),
    SensorEntityDescription(key="firmware_release_date", translation_key="firmware_release_date"),
    SensorEntityDescription(key="device_type", translation_key="device_type"),
    SensorEntityDescription(key="device_id", translation_key="device_id"),
    SensorEntityDescription(key="device_mac", translation_key="device_mac"),
    SensorEntityDescription(key="encoder_version", translation_key="encoder_version"),
    SensorEntityDescription(key="device_status", translation_key="device_status"),
    SensorEntityDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="memory_usage_percent",
        translation_key="memory_usage_percent",
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="memory_available_mb",
        translation_key="memory_available",
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
    ),
    SensorEntityDescription(
        key="uptime_hours",
        translation_key="uptime_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
    ),
    SensorEntityDescription(key="time_mode", translation_key="time_mode"),
    SensorEntityDescription(key="channel_count", translation_key="channel_count"),
    SensorEntityDescription(
        key="capability_video_input_channels",
        translation_key="capability_video_input_channels",
    ),
    SensorEntityDescription(key="network_ip", translation_key="network_ip"),
    SensorEntityDescription(key="network_subnet", translation_key="network_subnet"),
    SensorEntityDescription(key="network_gateway", translation_key="network_gateway"),
    SensorEntityDescription(key="network_mac", translation_key="network_mac"),
    SensorEntityDescription(key="network_mtu", translation_key="network_mtu"),
)

# Storage sensors — NVR/DVR only
STORAGE_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="storage_total_gb",
        translation_key="storage_total_gb",
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
    ),
    SensorEntityDescription(
        key="storage_used_gb",
        translation_key="storage_used_gb",
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
    ),
    SensorEntityDescription(
        key="storage_free_gb",
        translation_key="storage_free_gb",
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
    ),
    SensorEntityDescription(
        key="storage_usage_percent",
        translation_key="storage_usage_percent",
        native_unit_of_measurement=PERCENTAGE,
    ),
)

# NIC 2 sensors — dual-NIC devices only
NIC2_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(key="network_2_ip", translation_key="network_2_ip"),
    SensorEntityDescription(key="network_2_subnet", translation_key="network_2_subnet"),
    SensorEntityDescription(key="network_2_gateway", translation_key="network_2_gateway"),
    SensorEntityDescription(key="network_2_mac", translation_key="network_2_mac"),
    SensorEntityDescription(key="network_2_mtu", translation_key="network_2_mtu"),
)

# Per-channel streaming detail sensor templates
CHANNEL_STREAMING_KEYS = (
    "video_codec",
    "video_resolution",
    "video_frame_rate",
    "video_bitrate",
    "audio_codec",
    "channel_name",
)


class HikvisionISAPISensor(HikvisionISAPIEntity, SensorEntity):
    """Representation of a Hikvision ISAPI sensor."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, description: SensorEntityDescription, channel: int | None = None) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._channel = channel
        if channel is not None:
            self._attr_unique_id = f"{coordinator.unique_id}_{description.key}".replace(
                "{N}", str(channel)
            )
        else:
            self._attr_unique_id = f"{coordinator.unique_id}_{description.key}"

    @property
    def native_value(self):
        """Return the sensor value."""
        if not self.coordinator.data:
            return None
        data = self.coordinator.data
        key = self.entity_description.key

        # Per-channel streaming detail
        if self._channel is not None:
            ch_details = data.streaming_channel_detail if hasattr(data, 'streaming_channel_detail') else {}
            ch_data = ch_details.get(str(self._channel), {})
            return _or_none(ch_data.get(key.replace(f"channel_{{N}}_", "")))

        # Computed values
        if key == "memory_usage_percent":
            return _memory_usage_percent(data)
        if key == "storage_usage_percent":
            return _storage_usage_pct(data)
        if key == "device_status":
            di = data.device_info if hasattr(data, 'device_info') else {}
            return "在线" if di else "离线"

        # Network fields (NIC 1)
        if key.startswith("network_") and not key.startswith("network_2_"):
            field = key.replace("network_", "")
            return _first_iface(data, field)

        # Network fields (NIC 2)
        if key.startswith("network_2_"):
            field = key.replace("network_2_", "")
            return _second_iface(data, field)

        # Device info fields
        di = data.device_info if hasattr(data, 'device_info') else {}
        ss = data.system_status if hasattr(data, 'system_status') else {}
        caps = data.capabilities if hasattr(data, 'capabilities') else {}
        storage = data.storage if hasattr(data, 'storage') else {}
        time_info = data.time_info if hasattr(data, 'time_info') else {}

        mapping = {
            "model": lambda: _or_none(di.get("model")),
            "serial_number": lambda: _or_none(di.get("serialNumber")),
            "firmware_version": lambda: _or_none(di.get("firmwareVersion")),
            "firmware_release_date": lambda: _or_none(di.get("firmwareReleasedDate")),
            "device_type": lambda: _or_none(di.get("deviceType")),
            "device_id": lambda: _or_none(di.get("deviceID")),
            "device_mac": lambda: _or_none(di.get("macAddress")),
            "encoder_version": lambda: _or_none(di.get("encoderVersion")),
            "cpu_usage": lambda: ss.get("cpuUtilization"),
            "memory_available_mb": lambda: ss.get("memoryAvailable"),
            # [FIX #4] uptime key is "uptime" not "uptimeHours", and convert
            # seconds to hours. Hikvision reports uptime in seconds.
            "uptime_hours": lambda: _uptime_to_hours(ss.get("uptime")),
            # [FIX #5] time_info key is "time_mode" not "mode"
            "time_mode": lambda: _or_none(time_info.get("time_mode")),
            "channel_count": lambda: di.get("channelCount"),
            # [FIX #6] capabilities key is "video_input_channels" not "videoInputChannels"
            "capability_video_input_channels": lambda: caps.get("video_input_channels"),
            # [FIX #3] storage keys are "total_mb"/"used_mb"/"free_mb", convert to GB
            "storage_total_gb": lambda: _mb_to_gb(storage.get("total_mb")),
            "storage_used_gb": lambda: _mb_to_gb(storage.get("used_mb")),
            "storage_free_gb": lambda: _mb_to_gb(storage.get("free_mb")),
        }

        fn = mapping.get(key)
        if fn:
            return fn()
        return None


def _uptime_to_hours(uptime_str: str | None) -> float | None:
    """Convert uptime string (seconds) to hours."""
    if uptime_str is None:
        return None
    try:
        seconds = float(str(uptime_str).strip())
        return round(seconds / 3600, 1)
    except (ValueError, TypeError):
        return None


def _mb_to_gb(mb: int | float | None) -> float | None:
    """Convert MB to GB."""
    if mb is None:
        return None
    try:
        return round(float(mb) / 1024, 1)
    except (ValueError, TypeError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[HikvisionISAPISensor] = []

    device_type = coordinator.device_type

    # Always-on sensors
    for desc in ALWAYS_ON_SENSORS:
        # Suppress cpu_usage on V4 NVR/DVR (firmware bug returns 0)
        if desc.key == "cpu_usage" and device_type in ("nvr", "dvr"):
            fw = ""
            if coordinator.data and hasattr(coordinator.data, 'device_info'):
                fw = coordinator.data.device_info.get("firmwareVersion", "") or ""
            if fw and fw.startswith("V4"):
                continue
        entities.append(HikvisionISAPISensor(coordinator, desc))

    # Storage sensors — NVR/DVR only
    if device_type in ("nvr", "dvr"):
        for desc in STORAGE_SENSORS:
            entities.append(HikvisionISAPISensor(coordinator, desc))

    # NIC 2 sensors — only when device has 2+ interfaces
    if len(getattr(coordinator, "network_interfaces", [])) >= 2:
        for desc in NIC2_SENSORS:
            entities.append(HikvisionISAPISensor(coordinator, desc))

    async_add_entities(entities)

    # Dynamic per-channel streaming sensors
    added_channels: set[int] = set()

    @callback
    def _add_channel_sensors():
        if not coordinator.data:
            return
        channels = coordinator.data.channels if hasattr(coordinator.data, 'channels') else []
        new_entities = []
        for ch in channels:
            ch_id = ch.get("id")
            if ch_id is None or ch_id in added_channels:
                continue
            added_channels.add(ch_id)
            for suffix in CHANNEL_STREAMING_KEYS:
                key = f"channel_{{N}}_{suffix}"
                desc = SensorEntityDescription(
                    key=key,
                    translation_key=key,
                )
                new_entities.append(HikvisionISAPISensor(coordinator, desc, channel=ch_id))
        if new_entities:
            async_add_entities(new_entities)

    _add_channel_sensors()

    # Also add NIC 2 sensors dynamically if they appear later
    nic2_added = len(getattr(coordinator, "network_interfaces", [])) >= 2

    @callback
    def _check_nic2():
        nonlocal nic2_added
        if nic2_added:
            return
        if len(getattr(coordinator, "network_interfaces", [])) >= 2:
            nic2_added = True
            new_entities = [HikvisionISAPISensor(coordinator, desc) for desc in NIC2_SENSORS]
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_channel_sensors))
    entry.async_on_unload(coordinator.async_add_listener(_check_nic2))
