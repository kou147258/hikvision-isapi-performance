"""Button platform for Hikvision ISAPI.

One device-level button: ``Reboot Device``. Sends
``PUT /ISAPI/System/reboot``. The device reboots within ~30-60 s, during
which the integration's coordinator polls will fail with
connection errors. The coordinator recovers on the next successful
poll (10-30 s after the device is back up).

v0.6.19 also adds four PTZ direction buttons (上 / 下 / 左 / 右)
registered only when the device reports PTZ capability
(``capabilities["ptz"]``). Each sends a single instant PTZ move
via ``PUT /ISAPI/PTZCtrl/channels/1/continuous`` with a 1-second
duration argument — Hikvision's PTZ endpoint requires both the
direction and the duration on the same PUT.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ISAPI_SYSTEM_REBOOT
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity
from .isapi_client import (
    ISAPIConnectionError,
    ISAPIClient,
    ISAPIError,
)

_LOGGER = logging.getLogger(__name__)

# Hikvision PTZ continuous-move command XML. ``<duration>`` is in
# milliseconds; 1000 = 1 second of motion. The camera auto-stops
# after the duration. The exact XML body must include the
# ``PTZData`` wrapper — sending only ``<PTZData>`` without a root
# element returns ``<ResponseStatus>`` 4 "Invalid XML Content".
_PTZ_CONTINUOUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PTZData>
  <continuous>
    <direction>{direction}</direction>
    <duration>1000</duration>
  </continuous>
</PTZData>"""

# v0.6.19: PTZ direction button descriptors. ``ptz_command`` is the
# Hikvision direction token (``"up"`` / ``"down"`` / ``"left"`` /
# ``"right"``); the matching button entity assembles the XML body
# and PUTs it to ``/ISAPI/PTZCtrl/channels/1/continuous`` on press.
_PTZ_DIRECTIONS: tuple[dict[str, str], ...] = (
    {"key": "ptz_up",    "command": "up",    "name": "云台上转"},
    {"key": "ptz_down",  "command": "down",  "name": "云台下转"},
    {"key": "ptz_left",  "command": "left",  "name": "云台左转"},
    {"key": "ptz_right", "command": "right", "name": "云台右转"},
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device-level Reboot button + PTZ direction buttons."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [
        HikvisionISAPIRebootButton(coordinator, entry),
    ]
    # v0.6.19: only register PTZ buttons when the device reports
    # PTZ capability. Pre-v0.6.19 there was no PTZ button entity
    # at all (only the ptz_goto_preset service). On non-PTZ
    # devices the four direction buttons would appear in HA with
    # no useful effect.
    if coordinator.capabilities.get("ptz"):
        for desc in _PTZ_DIRECTIONS:
            entities.append(
                HikvisionISAPIPTZButton(
                    coordinator, entry,
                    key=desc["key"],
                    command=desc["command"],
                    name=desc["name"],
                ),
            )
    async_add_entities(entities)


class HikvisionISAPIRebootButton(HikvisionISAPIEntity, ButtonEntity):
    """A button that reboots the device via ``PUT /ISAPI/System/reboot``."""

    _attr_translation_key = "reboot"

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reboot"
        self._attr_name = "重启设备"

    async def async_press(self) -> None:
        coordinator = self.coordinator
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
                await client.put_text(ISAPI_SYSTEM_REBOOT, "")
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.warning("Failed to send reboot to %s: %s", coordinator._host, exc)
            # The reboot may have succeeded anyway; the connection drops
            # during the device's reboot sequence. We don't raise.
        else:
            _LOGGER.info("Sent reboot to %s", coordinator._host)
        # Trigger a refresh so HA reflects any state changes after reboot.
        await coordinator.async_request_refresh()


class HikvisionISAPIPTZButton(HikvisionISAPIEntity, ButtonEntity):
    """One of the four PTZ direction buttons (v0.6.19).

    Press sends a single 1-second continuous PTZ move in the
    configured direction. Registered only when the device
    reports PTZ capability. Channel defaults to 1 (the user's
    PTZ fleet is single-channel; for multi-channel NVR-mounted
    PTZ cameras, the ptz_goto_preset service remains the
    full-featured API).
    """

    _attr_translation_key = "ptz_direction"

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        *,
        key: str,
        command: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._ptz_command = command
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name

    async def async_press(self) -> None:
        coordinator = self.coordinator
        path = "/ISAPI/PTZCtrl/channels/1/continuous"
        body = _PTZ_CONTINUOUS_XML.format(direction=self._ptz_command)
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
                await client.put_text(path, body)
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.warning(
                "PTZ %s failed for %s: %s",
                self._ptz_command, coordinator._host, exc,
            )
        else:
            _LOGGER.info(
                "PTZ %s sent to %s", self._ptz_command, coordinator._host,
            )
