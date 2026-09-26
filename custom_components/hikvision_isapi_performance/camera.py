"""Camera platform for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HikvisionISAPIEntity
from .isapi_client import ISAPIClient

_LOGGER = logging.getLogger(__name__)


class HikvisionISAPICamera(HikvisionISAPIEntity, Camera):
    """Representation of a Hikvision ISAPI camera."""

    def __init__(self, coordinator, channel_id: int, channel_name: str) -> None:
        super().__init__(coordinator)
        self._channel_id = channel_id
        self._attr_unique_id = f"{coordinator.unique_id}_camera_{channel_id}"
        self._attr_name = channel_name or f"Channel {channel_id}"

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image response from the camera."""
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
                url = f"/ISAPI/Streaming/channels/{self._channel_id}01/picture"
                return await client.get_bytes(url)
        except Exception:
            _LOGGER.debug("Failed to get snapshot for channel %s", self._channel_id)
            return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    added_channels: set[int] = set()

    @callback
    def _add_cameras():
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
            new_entities.append(HikvisionISAPICamera(coordinator, ch_id, name))
        if new_entities:
            async_add_entities(new_entities)

    _add_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_cameras))
