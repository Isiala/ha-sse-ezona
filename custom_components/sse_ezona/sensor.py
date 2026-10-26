"""Status and yesterday energy. Historical consumption uses external statistics."""
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SSESensor(entry.runtime_data, key) for key in ("yesterday", "sync_status", "last_success")])


class SSESensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.eic}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.eic)}, name=f"SSE {coordinator.eic}",
            manufacturer="Stredoslovenská energetika", entry_type=DeviceEntryType.SERVICE)
        self._attr_name = {"yesterday": "Yesterday energy", "sync_status": "Sync status", "last_success": "Last successful sync"}[key]
        if key == "yesterday":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            # Deliberately no state_class: recording yesterday as today's live
            # energy would place delayed readings on the wrong day.
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "last_success":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        data = self.coordinator.data or self.coordinator.snapshot()
        if self.key == "yesterday":
            return data["yesterday_kwh"]
        if self.key == "last_success":
            return datetime.fromisoformat(data["last_success"]) if data["last_success"] else None
        return data["status"]

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or self.coordinator.snapshot()
        if self.key == "sync_status":
            return {"sse_eic": self.coordinator.eic, "profile_revision": data["profile_revision"],
                    "latest_day": data["latest_day"], "pending_dates": data["pending"],
                    "statistic_id": self.coordinator.statistic_id, "meter_timezone": "Europe/Bratislava"}
        if self.key == "yesterday":
            return {"date": data["yesterday"], "interval_minutes": 15}
        return None
