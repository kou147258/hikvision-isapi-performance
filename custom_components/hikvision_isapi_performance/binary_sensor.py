"""Binary sensor platform for Hikvision ISAPI.

Three per-channel binary sensors are created for each detected channel:
- ``channel_{N}_online`` — whether the channel is reachable
- ``channel_{N}_recording`` — whether the channel is currently recording
- ``channel_{N}_motion`` — whether motion is currently being detected

These complement the v0.1.0 device-level ``online`` / ``recording``
binary sensors (which aggregate "any channel"). Per-channel
breakdown is more useful for HA automations like
"if camera_3_motion → turn on hallway light".

The v0.1.23 pattern (from hikvision-snmp) is followed: system / per-
channel entities are registered immediately if data is available;
otherwise a coordinator listener adds them as soon as the first
poll completes.

v0.6.19 adds a device-level ``device_online`` binary sensor (CONNECTIVITY
class) for HA automations like "if device offline → notify". Coordinator
failure already takes the entity unavailable, so ``is_on`` is True when
the latest poll returned data and False only when ``data`` is None.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-channel binary sensors (online / recording / motion)."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = [
        HikvisionISAPIDeviceOnlineBinarySensor(coordinator, entry),
    ]
    for ch in coordinator.channels:
        entities.extend(_entities_for_channel(coordinator, entry, ch))
    async_add_entities(entities)

    # Per-channel listener for late-arriving channels.
    if not getattr(coordinator, "_hikvision_isapi_performance_binary_added", False):
        coordinator._hikvision_isapi_performance_binary_added = False  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_binary_listener(hass, entry, coordinator, async_add_entities)
        )


def _make_binary_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
):
    """Coordinator listener: add per-channel binary entities on late data."""

    async def _on_update() -> None:
        if getattr(coordinator, "_hikvision_isapi_performance_binary_added", False):
            return
        if coordinator.data is None:
            return
        new_entities: list[BinarySensorEntity] = []
        for ch in coordinator.channels:
            new_entities.extend(_entities_for_channel(coordinator, entry, ch))
        if not new_entities:
            return
        coordinator._hikvision_isapi_performance_binary_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


def _entities_for_channel(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    channel: dict[str, Any],
) -> list[BinarySensorEntity]:
    """Build the three binary sensors for one channel."""
    ch_id = channel.get("id", "")
    ch_name = channel.get("name") or f"Channel {ch_id}"
    return [
        HikvisionISAPIChannelOnlineBinarySensor(coordinator, entry, ch_id, ch_name),
        HikvisionISAPIChannelRecordingBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
        HikvisionISAPIChannelMotionBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
    ]


class HikvisionISAPIChannelOnlineBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel online / reachability binary sensor."""

    _attr_translation_key = "channel_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_online"
        self._attr_name = f"{channel_name} 在线"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                return bool(ch.get("online", False))
        return None


class HikvisionISAPIChannelRecordingBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel recording state binary sensor."""

    _attr_translation_key = "channel_recording"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_recording"
        self._attr_name = f"{channel_name} 录像中"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                return bool(ch.get("recording", False))
        return None


class HikvisionISAPIChannelMotionBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel motion-detection binary sensor.

    Reads ``motionDetection`` from the per-channel status endpoint
    (``/ISAPI/ContentMgmt/InputProxy/channels/<id>/status``). On
    devices that don't implement the field, the entity shows
    ``unknown``.
    """

    _attr_translation_key = "channel_motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_motion"
        self._attr_name = f"{channel_name} 运动检测"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                # The coordinator may not have enriched this channel
                # with motion data if the per-channel status endpoint
                # is unavailable. Default to None (unknown) rather
                # than False in that case.
                if "motion_detected" not in ch:
                    return None
                return bool(ch.get("motion_detected", False))
        return None


class HikvisionISAPIDeviceOnlineBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Device-level online / reachability binary sensor (v0.6.19).

    ON when the coordinator's latest poll returned parsed data;
    OFF when the coordinator failed (returns ``None`` data — HA
    already takes the entity ``unavailable`` on UpdateFailed, so
    OFF only fires after the device recovers then fails again
    with an empty response). Useful for HA automations like
    ``if not device_online → notify`` without polling the camera
    snapshot URL.

    Distinct from per-channel ``channel_{N}_online``: this sensor
    reflects the **device's ISAPI HTTP responder**, while the
    per-channel one reflects **each mounted IPC stream** (e.g. an
    NVR-mounted camera can be online at device level but offline
    at channel level if its RTSP feed drops).
    """

    _attr_translation_key = "device_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_device_online"
        self._attr_name = "设备在线"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return False
        return True
