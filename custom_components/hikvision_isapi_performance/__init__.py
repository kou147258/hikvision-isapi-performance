"""Hikvision ISAPI Performance integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_USE_HTTPS, DOMAIN
from .coordinator import HikvisionISAPICoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hikvision ISAPI Performance from a config entry."""
    coordinator = HikvisionISAPICoordinator(
        hass=hass,
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, 80),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        use_https=entry.data.get(CONF_USE_HTTPS, False),
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, False),
        scan_interval=entry.options.get("scan_interval", 30),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # [FIX #8] Use async_config_entry_first_refresh which is designed for
    # first-refresh — it doesn't block the event loop for the full timeout.
    # We do NOT await it so platforms set up immediately.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register PTZ service if device supports it (deferred until first data)
    from .services import async_register_ptz_service  # noqa: E402

    async def _maybe_register_ptz(data):
        if data and data.capabilities.get("ptz"):
            await async_register_ptz_service(hass, coordinator, entry)

    if coordinator.data:
        await _maybe_register_ptz(coordinator.data)
    else:
        entry.async_on_unload(
            coordinator.async_add_listener(
                lambda: hass.async_create_task(
                    _maybe_register_ptz(coordinator.data)
                )
            )
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]
    # [FIX #9] update_scan_interval now exists on the coordinator.
    coordinator.update_scan_interval(entry.options.get("scan_interval", 30))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # [FIX #10] Only remove the PTZ service if this is the LAST entry using
    # this integration. Previously any single device unload would remove the
    # service for all devices.
    remaining = [
        eid for eid in hass.data.get(DOMAIN, {})
        if eid != entry.entry_id
    ]
    if not remaining and hass.services.has_service(DOMAIN, "ptz_goto_preset"):
        hass.services.async_remove(DOMAIN, "ptz_goto_preset")

    return unload_ok
