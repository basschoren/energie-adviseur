"""Aantal wasjes dat je vandaag nog wilt draaien."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .appliance import ApplianceManager
from .entity import EAEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = entry.runtime_data
    if isinstance(manager, ApplianceManager) and manager.is_washer:
        async_add_entities([LoadsNumber(manager, "loads")])


class LoadsNumber(EAEntity, NumberEntity):
    """Telt automatisch af wanneer een was klaar is."""

    _attr_native_min_value = 0
    _attr_native_max_value = 6
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.manager.loads

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_loads(int(value))
