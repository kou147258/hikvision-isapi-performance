"""Switch platform for Hikvision ISAPI.

One switch per channel that toggles recording on/off via
``/ISAPI/ContentMgmt/InputProxy/channels/{id}/capabilities?recording=On|Off``.

The switch is *optimistic*: it flips the local state immediately on
user toggle and then sends the ISAPI PUT. The coordinator's next
poll confirms / reverts if Hikvision rejected the change.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import capabilities as _caps
from .const import (
    DOMAIN,
    ISAPI_INPUT_PROXY_CHANNELS,
    ISAPI_SYSTEM_VIDEO_INPUTS_CHANNELS_MOTION_DETECTION,
)
from .coordinator import HikvisionISAPICoordinator
from .entity import (
    HikvisionISAPIEntity,
    channel_entities_disabled_by_default,
)
from .isapi_client import (
    ISAPIConnectionError,
    ISAPIClient,
    ISAPIError,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-channel recording + motion-detection switches."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        HikvisionISAPIRecordingSwitch(coordinator, entry, ch)
        for ch in coordinator.channels
    ]
    # v0.8: motion-detection switches. Built from the probe cache, which is
    # normally still empty here (the probe is deferred until channels land),
    # so the dedicated listener below usually does the registering.
    motion_entities = _build_motion_detection_entities(coordinator, entry)
    entities.extend(motion_entities)
    coordinator._hikvision_isapi_performance_motion_switch_added = [  # type: ignore[attr-defined]
        e._channel_id for e in motion_entities
    ]
    async_add_entities(entities)

    if not getattr(coordinator, "_hikvision_isapi_performance_switch_added", False):
        coordinator._hikvision_isapi_performance_switch_added = False  # type: ignore[attr-defined]

        # v0.7.3: was ``async def`` and therefore never ran — HA calls
        # ``async_add_listener`` callbacks synchronously and discards the
        # result, so the coroutine was never awaited. Because platforms are
        # set up BEFORE the first refresh, ``coordinator.channels`` is empty
        # at setup time and this listener was the only path that could
        # register the per-channel recording switches. Body has no await.
        def _on_update() -> None:
            if getattr(coordinator, "_hikvision_isapi_performance_switch_added", False):
                return
            if coordinator.data is None:
                return
            new_entities = [
                HikvisionISAPIRecordingSwitch(coordinator, entry, ch)
                for ch in coordinator.channels
            ]
            if not new_entities:
                return
            coordinator._hikvision_isapi_performance_switch_added = True  # type: ignore[attr-defined]
            async_add_entities(new_entities)

        coordinator.async_add_listener(_on_update)

    # v0.8: separate listener for motion-detection switches. It cannot
    # share ``_switch_added`` above: that flag latches on the first refresh
    # that has channels, which happens BEFORE the motion probe completes,
    # so the motion switches would never be registered. It is also
    # incremental — channels whose probe succeeds later still get a switch.
    coordinator.async_add_listener(
        _make_motion_switch_listener(coordinator, entry, async_add_entities)
    )


def _make_motion_switch_listener(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Incremental listener: register motion switches as probes complete.

    Must stay a plain sync function: HA's ``async_add_listener`` expects
    ``Callable[[], None]`` and calls callbacks synchronously, discarding
    the return value — an ``async def`` here would never run its body
    (the v0.7.3 dead-listener defect).
    """
    already: set[str] = set(
        getattr(
            coordinator,
            "_hikvision_isapi_performance_motion_switch_added",
            None,
        ) or []
    )

    def _on_update() -> None:
        motion = getattr(coordinator, "motion_detection", None) or {}
        if not motion:
            return
        new_ids = set(motion) - already
        if not new_ids:
            return
        built = _build_motion_detection_entities(coordinator, entry)
        new_entities = [e for e in built if e._channel_id in new_ids]
        if not new_entities:
            return
        already.update(new_ids)
        coordinator._hikvision_isapi_performance_motion_switch_added = list(already)  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


class HikvisionISAPIRecordingSwitch(HikvisionISAPIEntity, SwitchEntity):
    """A switch that toggles recording on/off for one channel."""

    _attr_translation_key = "recording"

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel = channel
        self._attr_unique_id = f"{entry.entry_id}_record_{channel['id']}"
        self._attr_name = channel.get("name") or f"Channel {channel['id']}"

    @property
    def channel_id(self) -> str:
        return self._channel["id"]

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch["id"] == self.channel_id:
                # v0.7.4: ``recording`` is tri-state (see
                # coordinator._tri_record_status). ``None`` means no
                # endpoint reported <recordStatus>, so the switch must
                # render "unknown" instead of asserting "off" — the old
                # ``bool(ch.get("recording"))`` coerced None to False.
                recording = ch.get("recording")
                if recording is None:
                    return None
                return bool(recording)
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_recording(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_recording(False)

    async def _set_recording(self, on: bool) -> None:
        coordinator = self.coordinator
        path = (
            f"{ISAPI_INPUT_PROXY_CHANNELS}/{self.channel_id}/"
            f"capabilities?recording={'On' if on else 'Off'}"
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
                await client.put_text(path, "")
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.warning(
                "Failed to set recording=%s for channel %s: %s",
                "on" if on else "off",
                self.channel_id,
                exc,
            )
            # Trigger a refresh so the next poll re-reads the actual state.
            await coordinator.async_request_refresh()
        else:
            # Optimistic local state update.
            self._channel["recording"] = on
            self.async_write_ha_state()
            await coordinator.async_request_refresh()


# ── v0.8: motion-detection enable switch ────────────────────────────


def _motion_channel_name(
    coordinator: HikvisionISAPICoordinator, channel_id: str
) -> str:
    """Display name for a channel, preferring the camera's real name."""
    for ch in getattr(coordinator, "channels", None) or []:
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    data = getattr(coordinator, "data", None)
    for ch in (getattr(data, "channels", None) or []):
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    return f"通道 {channel_id}"


def _build_motion_detection_entities(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
) -> list[SwitchEntity]:
    """Build one motion-detection switch per probed channel.

    Gated on the probe result rather than ``coordinator.channels``: a
    device that answers 403 on motionDetection (176.65 / DS-7708N-I4
    V4.1.18) has no such setting to toggle, and creating one would leave a
    permanently-"unknown" switch.
    """
    motion = getattr(coordinator, "motion_detection", None) or {}
    entities = [
        HikvisionISAPIMotionDetectionSwitch(
            coordinator, entry, str(channel_id),
            _motion_channel_name(coordinator, channel_id),
        )
        for channel_id, cfg in motion.items()
        if cfg.get("enabled") is not None
    ]
    # v0.8 dedup: disable the NVR-side switch for a camera that is also
    # configured directly, matching the other per-channel platforms.
    for ent in entities:
        if channel_entities_disabled_by_default(coordinator, ent._channel_id):
            ent._attr_entity_registry_enabled_default = False
    return entities


class HikvisionISAPIMotionDetectionSwitch(HikvisionISAPIEntity, SwitchEntity):
    """Toggle per-channel motion detection (v0.8).

    This entity WRITES device configuration. It shares
    ``capabilities.build_motion_detection_body`` with the sensitivity
    number, which mutates only ``<enabled>`` on the captured document so
    the user's detection region (``gridMap``), sampling interval and
    trigger times survive the round trip — a hand-built minimal document
    would reset all of them.

    On PUT failure the cache is left untouched, so the switch snaps back
    to what the device actually holds instead of showing a rejected value.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:motion-sensor"

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
            f"{entry.entry_id}_channel_{channel_id}_motion_detection"
        )
        self._attr_name = f"{channel_name} 移动侦测"

    @property
    def _cfg(self) -> dict[str, Any]:
        return (getattr(self.coordinator, "motion_detection", None) or {}).get(
            self._channel_id, {}
        )

    @property
    def is_on(self) -> bool | None:
        enabled = self._cfg.get("enabled")
        if enabled is None:
            return None
        return bool(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, on: bool) -> None:
        coordinator = self.coordinator
        cfg = self._cfg
        body = _caps.build_motion_detection_body(
            cfg.get("_raw_xml", ""), enabled=on
        )
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
                "Failed to set motionDetection enabled=%s for channel %s: %s",
                on, self._channel_id, exc,
            )
            return

        # Optimistic update, then read back to confirm the device took it.
        if cfg:
            cfg["enabled"] = on
        self.async_write_ha_state()
        refresh = getattr(
            coordinator, "async_refresh_motion_detection", None
        )
        if refresh is not None:
            await refresh(self._channel_id)
        else:
            await coordinator.async_request_refresh()
