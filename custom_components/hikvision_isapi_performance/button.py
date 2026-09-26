"""Button platform for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HikvisionISAPIEntity
from .isapi_client import ISAPIClient

_LOGGER = logging.getLogger(__name__)


class HikvisionISAPIRebootButton(HikvisionISAPIEntity, ButtonEntity):
    """Button to reboot the device."""

    _attr_unique_id_suffix = "reboot"
    _attr_translation_key = "reboot"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}_reboot"

    async def async_press(self) -> None:
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
                await client.put_xml("/ISAPI/System/reboot", "")
        except Exception:
            _LOGGER.warning("Failed to reboot device")


class HikvisionISAPIPTZButton(HikvisionISAPIEntity, ButtonEntity):
    """Button for PTZ direction control."""

    def __init__(self, coordinator, direction: str) -> None:
        super().__init__(coordinator)
        self._direction = direction
        self._attr_unique_id = f"{coordinator.unique_id}_ptz_{direction}"
        self._attr_translation_key = f"ptz_{direction}"

    async def async_press(self) -> None:
        xml_body = (
            "<PTZData>"
            "<continuous>"
            f"<direction>{self._direction}</direction>"
            "<duration>1000</duration>"
            "</continuous>"
            "</PTZData>"
        )
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
                await client.put_xml("/ISAPI/PTZCtrl/channels/1/continuous", xml_body)
        except Exception:
            _LOGGER.warning("Failed to send PTZ %s command", self._direction)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [HikvisionISAPIRebootButton(coordinator)]

    # PTZ buttons only when device supports PTZ
    caps = {}
    if coordinator.data and hasattr(coordinator.data, 'capabilities'):
        caps = coordinator.data.capabilities
    if caps.get("ptz"):
        for direction in ("up", "down", "left", "right"):
            entities.append(HikvisionISAPIPTZButton(coordinator, direction))

    async_add_entities(entities)
