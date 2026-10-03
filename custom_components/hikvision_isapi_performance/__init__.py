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
    # v0.8: motion-detection sensitivity sliders. Must be listed here or
    # number.py is never loaded and the platform silently does nothing.
    Platform.NUMBER,
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

    # v0.8: start the alertStream push reader. Best-effort and fully
    # independent of the poll cycle — it opens its own long-lived client
    # and negotiates auth itself (176.18 only accepts Basic), so a push
    # failure can never take the integration below its polling behaviour.
    # Started here rather than in a platform because the task's lifetime
    # belongs to the config entry, and ``coordinator.async_shutdown``
    # (called on unload) is what stops it — no orphaned connection.
    coordinator.async_start_event_stream()

    # v0.8: probe per-channel motion-detection config ONCE, after the
    # first refresh has populated ``channels``.
    #
    # This is not part of the poll cycle on purpose: motionDetection is
    # configuration that rarely changes (11/12 fleet devices report
    # sensitivityLevel=60), so polling it would add one request per
    # channel per cycle — 13 extra requests every 30 s on an NVR. The
    # result is cached and only re-read after a PUT.
    #
    # Deferred via a listener because platforms are forwarded BEFORE the
    # first refresh runs, so ``coordinator.channels`` is still empty here.
    # Without the deferral the probe would find no channels, mark itself
    # done, and the motion switch / sensitivity slider would never appear
    # — the same class of bug as the v0.7.3 dead listeners.
    def _maybe_probe_motion_detection() -> None:
        if getattr(coordinator, "_hikvision_isapi_performance_motion_probed", False):
            return
        if not coordinator.channels:
            return
        coordinator._hikvision_isapi_performance_motion_probed = True  # type: ignore[attr-defined]
        hass.async_create_task(
            coordinator.async_probe_motion_detection(),
            name="hikvision_isapi_performance.motion_probe",
        )

    coordinator.async_add_listener(_maybe_probe_motion_detection)

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
    """Unload a config entry.

    v0.8: must stop the alertStream reader. ``DataUpdateCoordinator`` only
    auto-registers its ``async_shutdown`` when ``config_entry`` is passed to
    ``super().__init__``, and this integration constructs the coordinator
    without it — so nothing else would ever call it. Without the explicit
    call below, unloading (or reloading) the entry would leave the reader
    task and its long-lived HTTP connection alive forever: a leaked
    connection per unload, plus ghost tasks writing into a coordinator
    nobody reads.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
        await async_unregister_ptz_service(hass)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change (e.g. scan interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
