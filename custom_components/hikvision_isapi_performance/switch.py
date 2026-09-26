"""Switch platform for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HikvisionISAPIEntity
from .isapi_client import ISAPIClient

_LOGGER = logging.getLogger(__name__)


class HikvisionISAPIRecordingSwitch(HikvisionISAPIEntity, SwitchEntity):
    """Switch to control per-channel recording."""

    def __init__(self, coordinator, channel_id: int, channel_name: str) -> None:
        super().__init__(coordinator)
        self._channel_id = channel_id
        self._attr_unique_id = f"{coordinator.unique_id}_recording_{channel_id}"
        self._attr_name = f"{channel_name} Recording" if channel_name else f"Channel {channel_id} Recording"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        # [FIX #1] Access attribute instead of .get()
        channels = self.coordinator.data.channels if hasattr(self.coordinator.data, 'channels') else []
        for ch in channels:
            if ch.get("id") == self._channel_id:
                return ch.get("recording")
        return None

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_recording(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_recording(False)

    async def _set_recording(self, state: bool) -> None:
        value = "On" if state else "Off"
        try:
            # [FIX #7] Pass verify_ssl from coordinator
            client = ISAPIClient(
                host=self.coordinator.host,
                port=self.coordinator.port,
                username=self.coordinator.username,
                password=self.coordinator.password,
                use_https=self.coordinator.use_https,
                verify_ssl=self.coordinator.verify_ssl,
            )
            async with client:
                url = f"/ISAPI/ContentMgmt/InputProxy/channels/{self._channel_id}/capabilities?recording={value}"
                await client.put_xml(url, "")
        except Exception:
            _LOGGER.warning("Failed to set recording %s for channel %s", value, self._channel_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    added_channels: set[int] = set()

    @callback
    def _add_switches():
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
            name = ch.get("name", f"Channel {ch_id}")
            new_entities.append(HikvisionISAPIRecordingSwitch(coordinator, ch_id, name))
        if new_entities:
            async_add_entities(new_entities)

    _add_switches()
    entry.async_on_unload(coordinator.async_add_listener(_add_switches))
