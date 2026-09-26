"""Binary sensor platform for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)

DEVICE_BINARY_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="device_online",
        translation_key="device_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="dev_time_abnormal",
        translation_key="dev_time_abnormal",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="mem_calibration_warn",
        translation_key="mem_calibration_warn",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
)


class HikvisionISAPIBinarySensor(HikvisionISAPIEntity, BinarySensorEntity):
    """Representation of a Hikvision ISAPI binary sensor."""

    entity_description: BinarySensorEntityDescription

    def __init__(self, coordinator, description: BinarySensorEntityDescription, channel: int | None = None) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._channel = channel
        if channel is not None:
            self._attr_unique_id = f"{coordinator.unique_id}_{description.key}".replace("{N}", str(channel))
        else:
            self._attr_unique_id = f"{coordinator.unique_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        data = self.coordinator.data
        key = self.entity_description.key

        if key == "device_online":
            # [FIX #1] Access attribute instead of .get()
            di = data.device_info if hasattr(data, 'device_info') else {}
            return bool(di)

        if key == "dev_time_abnormal":
            # [FIX #1] Access attribute instead of .get()
            time_info = data.time_info if hasattr(data, 'time_info') else {}
            device_time_str = time_info.get("deviceTime")
            if not device_time_str:
                return None
            try:
                dt = datetime.fromisoformat(device_time_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                diff = abs((now - dt).total_seconds())
                return diff > 86400  # 24 hours
            except (ValueError, TypeError):
                return None

        if key == "mem_calibration_warn":
            # [FIX #1] Access attribute instead of .get()
            ss = data.system_status if hasattr(data, 'system_status') else {}
            used = ss.get("memoryUsage")
            avail = ss.get("memoryAvailable")
            if used is None or avail is None:
                return False
            try:
                u = float(used)
                a = float(avail)
            except (ValueError, TypeError):
                return False
            if u <= 0:
                return False
            ratio = a / u if u > 0 else 0
            # After v0.6.25 normalisation both should be in MB.
            # If ratio is still > 50x something is wrong.
            return ratio > 50

        # Per-channel binary sensors
        if self._channel is not None:
            # [FIX #1] Access attribute instead of .get()
            channels = data.channels if hasattr(data, 'channels') else []
            for ch in channels:
                if ch.get("id") == self._channel:
                    if key.endswith("_online"):
                        return ch.get("online")
                    if key.endswith("_recording"):
                        return ch.get("recording")
                    if key.endswith("_motion"):
                        return ch.get("motion")
            return None

        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[HikvisionISAPIBinarySensor] = []

    # Device-level binary sensors
    for desc in DEVICE_BINARY_SENSORS:
        entities.append(HikvisionISAPIBinarySensor(coordinator, desc))

    async_add_entities(entities)

    # Dynamic per-channel binary sensors
    added_channels: set[int] = set()

    @callback
    def _add_channel_binary_sensors():
        if not coordinator.data:
            return
        # [FIX #1] Access attribute instead of .get()
        channels = coordinator.data.channels if hasattr(coordinator.data, 'channels') else []
        new_entities = []
        for ch in channels:
            ch_id = ch.get("id")
            if ch_id is None or ch_id in added_channels:
                continue
            added_channels.add(ch_id)
            for suffix in ("online", "recording", "motion"):
                key = f"channel_{{N}}_{suffix}"
                desc = BinarySensorEntityDescription(
                    key=key,
                    translation_key=key,
                )
                new_entities.append(HikvisionISAPIBinarySensor(coordinator, desc, channel=ch_id))
        if new_entities:
            async_add_entities(new_entities)

    _add_channel_binary_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_channel_binary_sensors))
