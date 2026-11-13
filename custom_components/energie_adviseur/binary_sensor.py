"""Aan/uit-sensoren van Energie Adviseur."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .appliance import ApplianceManager
from .entity import EAEntity
from .heating import HeatingManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = entry.runtime_data
    if isinstance(manager, ApplianceManager):
        async_add_entities([GoodMoment(manager, "good_moment")])
    elif isinstance(manager, HeatingManager):
        async_add_entities([AircoCheaper(manager, "airco_cheaper")])


class GoodMoment(EAEntity, BinarySensorEntity):
    """Aan als nu starten vrijwel even goedkoop is als het beste moment."""

    @property
    def is_on(self) -> bool | None:
        return self.manager.now_is_good


class AircoCheaper(EAEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        advice = self.manager.now_advice
        return advice.airco_cheaper if advice else None
