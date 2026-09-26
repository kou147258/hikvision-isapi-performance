"""Hikvision ISAPI integration entry point."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant

from .const import (
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    CONF_HOST,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import HikvisionISAPICoordinator
from .services import async_register_ptz_service, async_unregister_ptz_service

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hikvision ISAPI from a config entry."""
    coordinator = HikvisionISAPICoordinator(
        hass,
        entry,
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, False),
        # v0.6.11: default to HTTP (False). Hikvision ISAPI is HTTP
        # by default; HTTPS is opt-in via the device's web-server
        # settings. Users who explicitly set use_https=True (e.g. for
        # firmware with HTTPS enabled) keep their setting.
        use_https=entry.data.get(CONF_USE_HTTPS, False),
        scan_interval=entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        ),
    )

    # v0.6.2 — do NOT block entry setup on the first refresh. The
    # config flow's credential check already proved the device is
    # reachable; the coordinator's first poll is best-effort and
    # runs in the background. Blocking here triggers HA's
    # "Setup taking over 10 seconds" warning on slow NVRs.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Kick off the first refresh in the background. Platforms already
    # have access to the coordinator; they'll pick up data on the
    # next listener fire.
    hass.async_create_task(
        coordinator.async_config_entry_first_refresh(),
        name="hikvision_isapi_performance.first_refresh",
    )

    # Register the PTZ service if the device supports it. Capabilities
    # are populated by the first refresh — but we may not have them yet.
    # If we don't, defer to a one-shot listener that fires as soon as
    # capabilities land.
    if coordinator.capabilities.get("ptz"):
        await async_register_ptz_service(hass, entry)
        _LOGGER.debug(
            "PTZ capability detected for %s — ptz_goto_preset service registered",
            entry.data[CONF_HOST],
        )
    else:
        def _on_update_maybe_register_ptz() -> None:
            if not coordinator.capabilities.get("ptz"):
                return
            hass.async_create_task(
                async_register_ptz_service(hass, entry),
                name="hikvision_isapi_performance.ptz_register",
            )

        coordinator.async_add_listener(_on_update_maybe_register_ptz)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await async_unregister_ptz_service(hass)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change (e.g. scan interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
