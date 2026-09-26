"""Shared base / helpers for the ISAPI entity platforms.

Kept separate from ``__init__.py`` to avoid circular imports — the
platform modules (camera / sensor / switch / button / binary_sensor)
need access to ``build_device_info`` but the integration's
``__init__.py`` is what HA loads first.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST, DOMAIN, MANUFACTURER
from .coordinator import HikvisionISAPICoordinator


def build_device_info(
    entry: ConfigEntry, device_info: dict[str, Any]
) -> DeviceInfo:
    """Build a Home Assistant DeviceInfo for the ISAPI device.

    Used by every platform (camera / sensor / switch / button /
    binary_sensor) so all entities for one device group under a single
    "device" tile in HA's UI. Without this, entities show up orphaned
    in the entities list with no device parent — which is what v0.6.1
    did and what we fix in v0.6.2.
    """
    host = entry.data.get(CONF_HOST, entry.entry_id)
    mac = (device_info.get("macAddress") or "").strip()
    model = device_info.get("model", "Hikvision Device")
    name = device_info.get("deviceName") or f"Hikvision {host}"
    connections: set[tuple[str, str]] = set()
    if mac:
        connections.add(("mac", mac.lower()))
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        connections=connections,
        manufacturer=MANUFACTURER,
        model=model,
        name=name,
        sw_version=device_info.get("firmwareVersion", ""),
        configuration_url=f"https://{host}",
    )


class HikvisionISAPIEntity(CoordinatorEntity[HikvisionISAPICoordinator]):
    """Base class for all ISAPI entities (camera / sensor / switch / button).

    Adds automatic ``device_info`` binding: when the coordinator's
    first refresh populates device info, the entity picks it up and
    re-publishes its state so HA groups it under the device.

    Subclasses must still define their own ``_attr_unique_id`` /
    ``_attr_name`` / entity-specific properties.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = None  # filled by _refresh_device_info

    async def async_added_to_hass(self) -> None:
        """Refresh device_info from the coordinator when the entity joins HA."""
        await super().async_added_to_hass()
        self._refresh_device_info()

    def _refresh_device_info(self) -> None:
        """Pull device_info from the latest coordinator data, if present."""
        coordinator = self.coordinator
        if coordinator.data is not None and coordinator.data.device_info:
            self._attr_device_info = build_device_info(
                self._entry, coordinator.data.device_info
            )

    def _handle_coordinator_update(self) -> None:
        """Re-bind device_info when the coordinator's data refreshes."""
        self._refresh_device_info()
        super()._handle_coordinator_update()