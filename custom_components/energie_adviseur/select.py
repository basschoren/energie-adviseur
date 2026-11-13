"""Keuzelijsten: programma voor advies, laatste cyclus toewijzen, droogprogramma."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .appliance import ApplianceManager
from .entity import EAEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = entry.runtime_data
    if not isinstance(manager, ApplianceManager):
        return
    entities: list[SelectEntity] = [ProgramSelect(manager, "program"), AssignSelect(manager, "assign_cycle")]
    if manager.is_washer:
        entities.append(DryProgramSelect(manager, "dry_program"))
    async_add_entities(entities)


class ProgramSelect(EAEntity, SelectEntity):
    """Het programma waarvoor het advies wordt berekend."""

    @property
    def options(self) -> list[str]:
        return self.manager.program_names()

    @property
    def current_option(self) -> str | None:
        program = self.manager.active_program
        return program.name if program else None

    async def async_select_option(self, option: str) -> None:
        self.manager.select_program(option)


class AssignSelect(EAEntity, SelectEntity):
    """Koppel de laatste cyclus aan een bestaand programma."""

    @property
    def options(self) -> list[str]:
        return self.manager.program_names()

    @property
    def current_option(self) -> str | None:
        cycle = self.manager.last_cycle
        return self.manager.program_name(cycle.program_id) if cycle else None

    async def async_select_option(self, option: str) -> None:
        self.manager.label_cycle(option)


class DryProgramSelect(EAEntity, SelectEntity):
    """Het droogprogramma dat na elke was volgt (programma's van de gekoppelde droger)."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        dryer = self.manager.dryer
        if dryer is not None:
            self.async_on_remove(async_dispatcher_connect(self.hass, dryer.signal, self.async_write_ha_state))

    @property
    def options(self) -> list[str]:
        dryer = self.manager.dryer
        return dryer.program_names() if dryer else []

    @property
    def current_option(self) -> str | None:
        program = self.manager.active_dry_program
        return program.name if program else None

    async def async_select_option(self, option: str) -> None:
        self.manager.set_dry_program(option)
