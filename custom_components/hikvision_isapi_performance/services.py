"""Service: ptz_goto_preset.

The service is registered in ``async_setup_entry`` only when the
device reports PTZ capability. The service payload schema is
defined in ``services.yaml``.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import async_get as async_get_device_registry

from .const import DOMAIN, ISAPI_PTZ_CTRL_CHANNELS
from .coordinator import HikvisionISAPICoordinator
from .isapi_client import ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)

PTZ_GOTO_PRESET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required("channel"): vol.All(int, vol.Range(min=1, max=64)),
        vol.Required("preset"): vol.All(int, vol.Range(min=1, max=256)),
    }
)


def _coordinator_for_device_id(
    hass: HomeAssistant, device_id: str
) -> HikvisionISAPICoordinator | None:
    """Map a HA device_id back to the integration's coordinator."""
    registry = async_get_device_registry(hass)
    device = registry.async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        coordinator: HikvisionISAPICoordinator | None = hass.data.get(
            DOMAIN, {}
        ).get(entry_id)
        if coordinator is not None:
            return coordinator
    return None


async def async_register_ptz_service(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the ptz_goto_preset service on the hass services bus.

    Called once per entry setup (only if the device reports PTZ
    capability). The service is unregistered on entry unload.
    """

    async def _handler(call: ServiceCall) -> None:
        device_id = call.data[ATTR_DEVICE_ID]
        channel_id = call.data["channel"]
        preset_id = call.data["preset"]

        coordinator = _coordinator_for_device_id(hass, device_id)
        if coordinator is None:
            _LOGGER.warning(
                "ptz_goto_preset: no coordinator found for device %s",
                device_id,
            )
            return

        path = (
            f"{ISAPI_PTZ_CTRL_CHANNELS}/{channel_id}"
            f"/presets/{preset_id}/goto"
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
                "ptz_goto_preset failed for %s channel %s preset %s: %s",
                coordinator._host,
                channel_id,
                preset_id,
                exc,
            )
        else:
            _LOGGER.info(
                "ptz_goto_preset: %s channel %s -> preset %s",
                coordinator._host,
                channel_id,
                preset_id,
            )

    hass.services.async_register(
        DOMAIN, "ptz_goto_preset", _handler, schema=PTZ_GOTO_PRESET_SCHEMA
    )


async def async_unregister_ptz_service(hass: HomeAssistant) -> None:
    """Unregister the ptz_goto_preset service.

    v0.6.8: skip if the service wasn't registered (e.g. device has no
    PTZ capability, or was never set up). Pre-v0.6.8 HA logged
    "Unable to remove unknown service hikvision_isapi_performance/ptz_goto_preset"
    on every reload of an entry whose device had no PTZ.
    """
    if not hass.services.has_service(DOMAIN, "ptz_goto_preset"):
        return
    hass.services.async_remove(DOMAIN, "ptz_goto_preset")
