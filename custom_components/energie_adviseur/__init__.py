"""Energie Adviseur: zelflerend advies voor grootverbruikers en verwarming."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .appliance import ApplianceManager
from .const import CONF_KIND, DOMAIN, KIND_APPLIANCE, KIND_HEATING, KIND_HUB
from .heating import HeatingManager
from .hub import HubManager
from .market import Market
from .panel import async_setup_panel

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.TEXT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
MANAGERS = {KIND_HUB: HubManager, KIND_APPLIANCE: ApplianceManager, KIND_HEATING: HeatingManager}


def _manager(hass: HomeAssistant, entry_id: str, expected: type | tuple[type, ...]) -> Any:
    """Zoek het onderdeel op ID of, als dat niets oplevert, op naam (bijvoorbeeld 'Wasmachine')."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        wanted = entry_id.strip().casefold()
        entry = next((e for e in hass.config_entries.async_entries(DOMAIN) if e.title.casefold() == wanted), None)
    manager = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
    if not isinstance(manager, expected):
        raise HomeAssistantError("Dit onderdeel van Energie Adviseur is niet gevonden of niet geladen")
    return manager


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})["market"] = Market(hass)

    async def label_cycle(call: ServiceCall) -> None:
        _manager(hass, call.data["appliance"], ApplianceManager).label_cycle(call.data["program"], call.data.get("cycle_id"))

    async def rename_program(call: ServiceCall) -> None:
        _manager(hass, call.data["appliance"], ApplianceManager).rename_program(call.data["program"], call.data["new_name"])

    async def delete_program(call: ServiceCall) -> None:
        _manager(hass, call.data["appliance"], ApplianceManager).delete_program(call.data["program"])

    async def delete_cycle(call: ServiceCall) -> None:
        _manager(hass, call.data["appliance"], ApplianceManager).delete_cycle(call.data["cycle_id"])

    async def split_cycle(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass, call.data["appliance"], ApplianceManager)
        parts = manager.split_cycle(call.data.get("cycle_id"), call.data.get("minutes"), call.data["keep"])
        return {"cycles": parts}

    async def restore_cycle(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass, call.data["appliance"], ApplianceManager)
        return {"cycles": await manager.async_restore_cycle(call.data["cycle_id"], call.data["from_history"])}

    async def import_history(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass, call.data["entry"], (ApplianceManager, HeatingManager))
        return {"imported": await manager.async_import_history(call.data["days"])}

    async def generate_advice(call: ServiceCall) -> ServiceResponse:
        for entry in hass.config_entries.async_entries(DOMAIN):
            manager = getattr(entry, "runtime_data", None)
            if isinstance(manager, HubManager):
                await manager.async_generate()
                return {"advice": manager.advice, "source": manager.advice_source}
        raise HomeAssistantError("Het algemene onderdeel van Energie Adviseur is niet ingesteld")

    appliance = {vol.Required("appliance"): cv.string}
    hass.services.async_register(
        DOMAIN,
        "label_cycle",
        label_cycle,
        vol.Schema({**appliance, vol.Required("program"): cv.string, vol.Optional("cycle_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        "rename_program",
        rename_program,
        vol.Schema({**appliance, vol.Required("program"): cv.string, vol.Required("new_name"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, "delete_program", delete_program, vol.Schema({**appliance, vol.Required("program"): cv.string})
    )
    hass.services.async_register(
        DOMAIN, "delete_cycle", delete_cycle, vol.Schema({**appliance, vol.Required("cycle_id"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        "split_cycle",
        split_cycle,
        vol.Schema(
            {
                **appliance,
                vol.Optional("cycle_id"): cv.string,
                vol.Optional("minutes"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1440)),
                vol.Optional("keep", default="both"): vol.In(["both", "first", "second"]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "restore_cycle",
        restore_cycle,
        vol.Schema(
            {**appliance, vol.Required("cycle_id"): cv.string, vol.Optional("from_history", default=False): cv.boolean}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "import_history",
        import_history,
        vol.Schema(
            {vol.Required("entry"): cv.string, vol.Optional("days", default=10): vol.All(vol.Coerce(int), vol.Range(1, 730))}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "generate_advice", generate_advice, vol.Schema({}), supports_response=SupportsResponse.OPTIONAL
    )
    await async_setup_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = MANAGERS[entry.data[CONF_KIND]](hass, entry)
    await manager.async_start()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
