"""Number platform for Hikvision ISAPI (v0.8).

One ``number`` entity per channel whose motion-detection configuration the
device actually exposes: the detection sensitivity (0–100).

Probed on the 12-device fleet, ``GET /ISAPI/System/Video/inputs/channels/
{id}/motionDetection`` returns ``<sensitivityLevel>60</sensitivityLevel>``
on 11 of 12 devices; 176.65 (DS-7708N-I4 V4.1.18) answers 403. The entity
is therefore created only for channels present in
``coordinator.motion_detection`` — a device that 403s gets nothing instead
of a permanently-"unknown" slider.

This entity WRITES to the device on change. It shares
``capabilities.build_motion_detection_body`` with the motion-detection
switch, which mutates only the targeted field so the user's detection
region (``gridMap``) and timings survive the round trip.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import capabilities as _caps
from .const import (
    DOMAIN,
    ISAPI_SYSTEM_VIDEO_INPUTS_CHANNELS_MOTION_DETECTION,
)
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity
from .isapi_client import ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up motion-sensitivity numbers once the config has been probed.

    ``coordinator.motion_detection`` is filled by a one-shot background
    probe (not the 30 s poll), so it is normally empty at setup time and
    the listener below does the registering.
    """
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = _build_motion_sensitivity_entities(coordinator, entry)
    async_add_entities(entities)
    coordinator._hikvision_isapi_performance_number_added = bool(entities)  # type: ignore[attr-defined]

    if not entities:
        coordinator.async_add_listener(
            _make_number_listener(coordinator, entry, async_add_entities)
        )


def _make_number_listener(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """One-shot listener: register sensitivity numbers after the probe.

    Must stay a plain sync function: HA's ``async_add_listener`` expects
    ``Callable[[], None]`` and calls callbacks synchronously, discarding
    the return value — an ``async def`` here would never run its body
    (the v0.7.3 dead-listener defect).
    """

    def _on_update() -> None:
        if getattr(coordinator, "_hikvision_isapi_performance_number_added", False):
            return
        new_entities = _build_motion_sensitivity_entities(coordinator, entry)
        if not new_entities:
            return
        coordinator._hikvision_isapi_performance_number_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


def _build_motion_sensitivity_entities(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
) -> list[NumberEntity]:
    """Build one sensitivity number per probed channel.

    Gated on the probe result, not on ``coordinator.channels``: a channel
    the device refused (403) has no sensitivity to show.
    """
    motion = getattr(coordinator, "motion_detection", None) or {}
    entities: list[NumberEntity] = []
    for channel_id, cfg in motion.items():
        if cfg.get("sensitivity_level") is None:
            # Device exposed motionDetection but not the sensitivity
            # field — don't create a slider that can never read a value.
            continue
        entities.append(
            HikvisionISAPIMotionSensitivityNumber(
                coordinator, entry, str(channel_id),
                _channel_display_name(coordinator, channel_id),
            )
        )
    return entities


def _channel_display_name(
    coordinator: HikvisionISAPICoordinator, channel_id: str
) -> str:
    for ch in getattr(coordinator, "channels", None) or []:
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    data = getattr(coordinator, "data", None)
    for ch in (getattr(data, "channels", None) or []):
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    return f"通道 {channel_id}"


class HikvisionISAPIMotionSensitivityNumber(HikvisionISAPIEntity, NumberEntity):
    """Motion-detection sensitivity slider (0–100) for one channel.

    Writes on change and reads back from the coordinator cache, which the
    PUT updates optimistically and the device refresh confirms.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = _caps.MOTION_SENSITIVITY_MIN
    _attr_native_max_value = _caps.MOTION_SENSITIVITY_MAX
    _attr_native_step = 1
    _attr_icon = "mdi:tune-variant"

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._attr_unique_id = (
            f"{entry.entry_id}_channel_{channel_id}_motion_sensitivity"
        )
        self._attr_name = f"{channel_name} 移动侦测灵敏度"

    @property
    def _cfg(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "motion_detection", None) or {}).get(
            self._channel_id, {}
        )

    @property
    def native_value(self) -> int | None:
        value = self._cfg.get("sensitivity_level")
        return None if value is None else int(value)

    async def async_set_native_value(self, value: float) -> None:
        """PUT the new sensitivity, preserving all other config.

        On failure the cache is left untouched so the slider snaps back to
        what the device actually holds, rather than showing a value that
        was rejected (a 403 on 176.65-class firmware).
        """
        target = int(value)
        coordinator = self.coordinator
        cfg = self._cfg
        raw = cfg.get("_raw_xml", "")
        try:
            body = _caps.build_motion_detection_body(
                raw, sensitivity_level=target
            )
        except ValueError as exc:
            _LOGGER.warning(
                "Refusing to write motion sensitivity %s for channel %s: %s",
                target, self._channel_id, exc,
            )
            return

        path = ISAPI_SYSTEM_VIDEO_INPUTS_CHANNELS_MOTION_DETECTION.format(
            id=self._channel_id
        )
        try:
            async with ISAPIClient(
                host=coordinator._host,
                port=coordinator._port,
                username=coordinator._username,
                password=coordinator._password,
                verify_ssl=coordinator._verify_ssl,
                use_https=coordinator._use_https,
                timeout=10,
            ) as client:
                await client.put_xml(path, body)
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.warning(
                "Failed to set motion sensitivity=%s for channel %s: %s",
                target, self._channel_id, exc,
            )
            return

        # Optimistic cache update, then read back to confirm the device
        # accepted it (matches the recording switch's semantics).
        if cfg:
            cfg["sensitivity_level"] = target
        self.async_write_ha_state()
        refresh = getattr(
            coordinator, "async_refresh_motion_detection", None
        )
        if refresh is not None:
            await refresh(self._channel_id)
        else:
            await coordinator.async_request_refresh()
