"""Configuratie via de UI: eerst het algemene deel, daarna apparaten en verwarming."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    APPLIANCE_TYPES,
    CONF_ADVICE_TIME,
    CONF_AI_ENTITY,
    CONF_AIRCO_ENTITIES,
    CONF_APPLIANCE_TYPE,
    CONF_BASE_LOAD,
    CONF_BOILER_EFF,
    CONF_COP_7,
    CONF_COP_MINUS7,
    CONF_DRYER_ENTRY,
    CONF_MIN_RUNNING_S,
    CONF_PAUSED_STATES,
    CONF_RUNNING_STATES,
    CONF_STATE_ENTITY,
    CONF_EXPORT_DEDUCTION,
    CONF_GAS_ENTITY,
    CONF_GAS_PRICE,
    CONF_GAS_PRICE_ENTITY,
    CONF_INDOOR_ENTITIES,
    CONF_KIND,
    CONF_NAME,
    CONF_OUTDOOR_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MULTIPLIER,
    CONF_SOLAR_ENTITY,
    CONF_START_W,
    CONF_STOP_DELAY,
    CONF_STOP_W,
    CONF_TOLERANCE,
    CONF_USE_ENERGY_DASHBOARD,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DOMAIN,
    KIND_APPLIANCE,
    KIND_HEATING,
    KIND_HUB,
    TYPE_DRYER,
    TYPE_WASHER,
)


def _num(minimum: float, maximum: float, step: float, unit: str | None = None) -> sel.NumberSelector:
    config = sel.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=sel.NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return sel.NumberSelector(config)


def _states(suggestions: list[str]) -> sel.SelectSelector:
    """Lijst van statussen; eigen waarden mogen, want elk merk noemt ze anders."""
    return sel.SelectSelector(
        sel.SelectSelectorConfig(
            options=suggestions, multiple=True, custom_value=True, mode=sel.SelectSelectorMode.DROPDOWN
        )
    )


def _entity(domain: str | list[str], multiple: bool = False) -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain=domain, multiple=multiple))


def hub_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRICE_ENTITY): _entity("sensor"),
            vol.Required(CONF_PRICE_MULTIPLIER, default=1.0): _num(0.0000001, 1000, "any"),
            vol.Required(CONF_USE_ENERGY_DASHBOARD, default=True): sel.BooleanSelector(),
            vol.Optional(CONF_SOLAR_ENTITY): _entity("sensor"),
            vol.Required(CONF_BASE_LOAD, default=300): _num(0, 5000, 10, "W"),
            vol.Required(CONF_EXPORT_DEDUCTION, default=0.0): _num(0, 1, 0.001, "€/kWh"),
            vol.Optional(CONF_AI_ENTITY): _entity(["ai_task", "conversation"]),
            vol.Required(CONF_ADVICE_TIME, default="07:00:00"): sel.TimeSelector(),
        }
    )


def appliance_schema(appliance_type: str | None, dryers: dict[str, str], with_identity: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_identity:
        fields[vol.Required(CONF_NAME)] = sel.TextSelector()
        fields[vol.Required(CONF_APPLIANCE_TYPE, default=TYPE_WASHER)] = sel.SelectSelector(
            sel.SelectSelectorConfig(options=APPLIANCE_TYPES, translation_key="appliance_type")
        )
    fields.update(
        {
            vol.Required(CONF_POWER_ENTITY): _entity("sensor"),
            vol.Required(CONF_START_W, default=10): _num(1, 3000, 1, "W"),
            vol.Required(CONF_STOP_W, default=5): _num(0, 3000, 0.5, "W"),
            vol.Required(CONF_STOP_DELAY, default=5): _num(1, 60, 1, "min"),
            vol.Required(CONF_TOLERANCE, default=15): _num(5, 40, 1, "%"),
            vol.Required(CONF_WINDOW_START, default="07:00:00"): sel.TimeSelector(),
            vol.Required(CONF_WINDOW_END, default="22:00:00"): sel.TimeSelector(),
            # optioneel: de statussensor van een slim apparaat bepaalt begin, einde en pauzes
            vol.Optional(CONF_STATE_ENTITY): _entity(["sensor", "binary_sensor", "select", "input_select"]),
            vol.Required(CONF_RUNNING_STATES, default=["running"]): _states(["running"]),
            vol.Required(CONF_PAUSED_STATES, default=["paused"]): _states(["paused"]),
            vol.Required(CONF_MIN_RUNNING_S, default=60): _num(0, 3600, 1, "s"),
        }
    )
    if dryers and (with_identity or appliance_type == TYPE_WASHER):
        fields[vol.Optional(CONF_DRYER_ENTRY)] = sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[sel.SelectOptionDict(value=k, label=v) for k, v in dryers.items()],
                mode=sel.SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(fields)


def heating_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_GAS_ENTITY): _entity("sensor"),
            vol.Required(CONF_INDOOR_ENTITIES): _entity("sensor", multiple=True),
            vol.Required(CONF_OUTDOOR_ENTITY): _entity(["sensor", "weather"]),
            vol.Optional(CONF_AIRCO_ENTITIES): _entity("sensor", multiple=True),
            vol.Required(CONF_GAS_PRICE, default=1.30): _num(0, 10, 0.001, "€/m³"),
            vol.Optional(CONF_GAS_PRICE_ENTITY): _entity(["sensor", "input_number", "number"]),
            vol.Required(CONF_BOILER_EFF, default=95): _num(50, 110, 1, "%"),
            vol.Required(CONF_COP_7, default=4.0): _num(1, 8, 0.1),
            vol.Required(CONF_COP_MINUS7, default=2.5): _num(1, 8, 0.1),
        }
    )


def _dryers(entries: list[ConfigEntry]) -> dict[str, str]:
    return {e.entry_id: e.title for e in entries if e.data.get(CONF_APPLIANCE_TYPE) == TYPE_DRYER}


def _validate_appliance(data: dict[str, Any]) -> dict[str, str]:
    if float(data[CONF_STOP_W]) > float(data[CONF_START_W]):
        return {CONF_STOP_W: "stop_above_start"}
    return {}


class EnergieAdviseurConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _kinds(self) -> set[str]:
        return {e.data.get(CONF_KIND) for e in self._async_current_entries()}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if KIND_HUB not in self._kinds():
            return await self.async_step_hub()
        return self.async_show_menu(step_id="user", menu_options=["appliance", "heating"])

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(KIND_HUB)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Energie Adviseur", data={CONF_KIND: KIND_HUB, **user_input})
        return self.async_show_form(step_id="hub", data_schema=hub_schema())

    async def async_step_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        schema = appliance_schema(None, _dryers(self._async_current_entries()), with_identity=True)
        if user_input is not None:
            errors = _validate_appliance(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_POWER_ENTITY])
                self._abort_if_unique_id_configured()
                if user_input[CONF_APPLIANCE_TYPE] != TYPE_WASHER:
                    user_input.pop(CONF_DRYER_ENTRY, None)
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data={CONF_KIND: KIND_APPLIANCE, **user_input}
                )
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="appliance", data_schema=schema, errors=errors)

    async def async_step_heating(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if KIND_HEATING in self._kinds():
            return self.async_abort(reason="heating_exists")
        if user_input is not None:
            await self.async_set_unique_id(KIND_HEATING)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Verwarming", data={CONF_KIND: KIND_HEATING, **user_input})
        return self.async_show_form(step_id="heating", data_schema=heating_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergieAdviseurOptionsFlow()


class EnergieAdviseurOptionsFlow(OptionsFlow):
    """Instellingen wijzigen; de waarden staan in entry.data en het onderdeel herlaadt daarna."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        kind = self.config_entry.data[CONF_KIND]
        return await getattr(self, f"async_step_{kind}")()

    def _finish(self, user_input: dict[str, Any], keep: tuple[str, ...]) -> ConfigFlowResult:
        entry = self.config_entry
        data = {key: entry.data[key] for key in keep if key in entry.data} | user_input
        self.hass.config_entries.async_update_entry(entry, data=data)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})

    def _form(self, step: str, schema: vol.Schema, errors: dict[str, str] | None = None, values: dict | None = None):
        suggested = values if values is not None else dict(self.config_entry.data)
        return self.async_show_form(
            step_id=step, data_schema=self.add_suggested_values_to_schema(schema, suggested), errors=errors or {}
        )

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish(user_input, (CONF_KIND,))
        return self._form("hub", hub_schema())

    async def async_step_appliance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        dryers = _dryers(self.hass.config_entries.async_entries(DOMAIN))
        schema = appliance_schema(entry.data.get(CONF_APPLIANCE_TYPE), dryers, with_identity=False)
        if user_input is not None:
            errors = _validate_appliance(user_input)
            if not errors:
                return self._finish(user_input, (CONF_KIND, CONF_NAME, CONF_APPLIANCE_TYPE))
            return self._form("appliance", schema, errors, user_input)
        return self._form("appliance", schema)

    async def async_step_heating(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._finish(user_input, (CONF_KIND,))
        return self._form("heating", heating_schema())
