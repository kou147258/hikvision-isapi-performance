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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ISAPI_INPUT_PROXY_CHANNELS
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity
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
    """Set up per-channel recording switch entities."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        HikvisionISAPIRecordingSwitch(coordinator, entry, ch)
        for ch in coordinator.channels
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
                return bool(ch.get("recording"))
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
