"""Tekstveld om de laatste cyclus een (nieuwe) programmanaam te geven."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .appliance import ApplianceManager
from .entity import EAEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = entry.runtime_data
    if isinstance(manager, ApplianceManager):
        async_add_entities([NameCycleText(manager, "name_cycle")])


class NameCycleText(EAEntity, TextEntity):
    """Typ een naam: bestaat het programma nog niet, dan wordt het aangemaakt."""

    _attr_native_max = 60

    @property
    def native_value(self) -> str:
        cycle = self.manager.last_cycle
        return (self.manager.program_name(cycle.program_id) if cycle else None) or ""

    async def async_set_value(self, value: str) -> None:
        self.manager.label_cycle(value)
