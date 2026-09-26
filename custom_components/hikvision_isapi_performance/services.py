"""Services for Hikvision ISAPI Performance."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_extract_referenced_device_ids

from .const import DOMAIN
from .coordinator import HikvisionISAPICoordinator
from .isapi_client import ISAPIClient

_LOGGER = logging.getLogger(__name__)

SERVICE_PTZ_GOTO_PRESET = "ptz_goto_preset"

PTZ_GOTO_PRESET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=256)),
        vol.Required("preset"): vol.All(vol.Coerce(int), vol.Range(min=1, max=256)),
    }
)


async def async_register_ptz_service(
    hass: HomeAssistant,
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
) -> None:
    """Register the PTZ goto preset service."""

    if hass.services.has_service(DOMAIN, SERVICE_PTZ_GOTO_PRESET):
        return

    async def handle_ptz_goto_preset(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        channel = call.data["channel"]
        preset = call.data["preset"]

        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get(device_id)
        if not device_entry:
            _LOGGER.warning("Device %s not found", device_id)
            return

        # Find the coordinator for this device
        target_coordinator = None
        for eid in device_entry.config_entries:
            if eid in hass.data.get(DOMAIN, {}):
                target_coordinator = hass.data[DOMAIN][eid]
                break

        if not target_coordinator:
            _LOGGER.warning("No coordinator found for device %s", device_id)
            return

        xml_body = (
            "<PTZData>"
            "<preset>"
            f"<id>{preset}</id>"
            "</preset>"
            "</PTZData>"
        )
        try:
            # [FIX #7] Pass verify_ssl from coordinator
            client = ISAPIClient(
                host=target_coordinator.host,
                port=target_coordinator.port,
                username=target_coordinator.username,
                password=target_coordinator.password,
                use_https=target_coordinator.use_https,
                verify_ssl=target_coordinator.verify_ssl,
            )
            async with client:
                url = f"/ISAPI/PTZCtrl/channels/{channel}/presets/{preset}/goto"
                await client.put_xml(url, "")
                _LOGGER.info(
                    "PTZ goto preset %s on channel %s for %s",
                    preset,
                    channel,
                    target_coordinator.host,
                )
        except Exception as err:
            _LOGGER.warning(
                "Failed to send PTZ goto preset %s on channel %s: %s",
                preset,
                channel,
                err,
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PTZ_GOTO_PRESET,
        handle_ptz_goto_preset,
        schema=PTZ_GOTO_PRESET_SCHEMA,
    )
