"""Base entity for Hikvision ISAPI Performance."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class HikvisionISAPIEntity(CoordinatorEntity):
    """Base class for Hikvision ISAPI entities."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator data."""
        data = self.coordinator.data
        if not data:
            return DeviceInfo(
                identifiers={(DOMAIN, self.coordinator.unique_id)},
            )
        di = data.device_info if hasattr(data, 'device_info') else {}
        return DeviceInfo(
            identifiers={(DOMAIN, di.get("serialNumber", self.coordinator.unique_id))},
            name=di.get("deviceName") or di.get("model", f"Hikvision {self.coordinator.host}"),
            model=di.get("model"),
            sw_version=di.get("firmwareVersion"),
            manufacturer="Hikvision",
            connections={(DOMAIN, di.get("macAddress", ""))} if di.get("macAddress") else set(),
        )
