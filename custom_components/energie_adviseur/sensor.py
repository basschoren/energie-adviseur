"""Sensoren van Energie Adviseur."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .appliance import ApplianceManager, fmt_duration
from .entity import EAEntity
from .heating import HeatingManager
from .hub import HubManager
from .market import get_market


@dataclass(frozen=True, kw_only=True)
class EADescription(SensorEntityDescription):
    value: Callable[[Any], Any]
    attrs: Callable[[Any], dict[str, Any]] | None = None
    washer_only: bool = False


def _ts(value: float | None):
    return dt_util.utc_from_timestamp(value) if value else None


def _advice(m: ApplianceManager, field: str):
    return getattr(m.advice, field) if m.advice else None


def _last(m: ApplianceManager, field: str):
    return getattr(m.last_cycle, field) if m.last_cycle else None


def _last_program(m: ApplianceManager) -> str | None:
    if m.last_cycle is None:
        return None
    return m.program_name(m.last_cycle.program_id) or "Onbekend"


def _program_attrs(m: ApplianceManager) -> dict[str, Any]:
    return {
        "programmas": [
            {"naam": p.name, "keer_gedraaid": p.count, "duur": fmt_duration(p.duration_s), "kwh": p.energy_kwh}
            for p in sorted(m.programs.values(), key=lambda p: p.name)
        ],
        "onbekende_cycli": m.unlabeled(),
    }


def _advice_attrs(m: ApplianceManager) -> dict[str, Any]:
    program, advice = m.active_program, m.advice
    if program is None or advice is None:
        return {}
    return {
        "programma": program.name,
        "klaar_om": _ts(advice.end).isoformat(),
        "kosten": advice.cost,
        "kosten_bij_nu_starten": advice.cost_now,
        "besparing": advice.saving,
        "verbruik_kwh": advice.energy_kwh,
    }


APPLIANCE: tuple[EADescription, ...] = (
    EADescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "running"],
        value=lambda m: "running" if m.detector.running else "idle",
        attrs=lambda m: {
            "gestart": _ts(m.detector.start).isoformat() if m.detector.running else None,
            "verbruik_tot_nu_kwh": round(m.detector.energy_kwh, 3) if m.detector.running else None,
        },
    ),
    EADescription(key="last_program", value=_last_program, attrs=_program_attrs),
    EADescription(
        key="last_cycle_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda m: _last(m, "energy_kwh"),
        attrs=lambda m: {
            "cycle_id": _last(m, "cycle_id"),
            "start": _ts(_last(m, "start")).isoformat() if m.last_cycle else None,
            "duur_min": round(m.last_cycle.duration_s / 60) if m.last_cycle else None,
        },
    ),
    EADescription(
        key="best_start", device_class=SensorDeviceClass.TIMESTAMP, value=lambda m: _ts(_advice(m, "start")), attrs=_advice_attrs
    ),
    EADescription(
        key="expected_cost", native_unit_of_measurement="€", suggested_display_precision=2, value=lambda m: _advice(m, "cost")
    ),
    EADescription(
        key="saving", native_unit_of_measurement="€", suggested_display_precision=2, value=lambda m: _advice(m, "saving")
    ),
    EADescription(
        key="laundry_plan",
        washer_only=True,
        value=lambda m: m.plan_text(),
        attrs=lambda m: {
            "planning": m.plan_steps(),
            "kosten": m.plan.cost if m.plan else None,
            "nog_te_wassen": m.loads,
            "nog_te_drogen": m.pending_dry,
        },
    ),
)

HEATING: tuple[EADescription, ...] = (
    EADescription(
        key="gas_today",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value=lambda m: round(m.today.get("gas_m3", 0.0), 3),
        attrs=lambda m: {
            "graaddagen": round(m.today.get("degree_hours", 0.0) / 24.0, 2),
            "airco_kwh": round(m.today.get("airco_kwh", 0.0), 2),
        },
    ),
    EADescription(
        key="heat_demand",
        native_unit_of_measurement="kWh/K·d",
        suggested_display_precision=1,
        value=lambda m: round(m.model.heat_kwh_per_degree_day(m.boiler_eff), 2) if m.model else None,
        attrs=lambda m: {
            "gas_per_graaddag_m3": m.model.m3_per_degree_day if m.model else None,
            "basisverbruik_m3_per_dag": m.model.base_m3 if m.model else None,
            "dagen_gebruikt": m.model.days if m.model else 0,
            "dagen_opgeslagen": len(m.days),
            "r2": m.model.r2 if m.model else None,
        },
    ),
    EADescription(
        key="gas_heat_cost",
        native_unit_of_measurement="€/kWh",
        suggested_display_precision=3,
        value=lambda m: m.now_advice.gas_cost if m.now_advice else None,
    ),
    EADescription(
        key="airco_heat_cost",
        native_unit_of_measurement="€/kWh",
        suggested_display_precision=3,
        value=lambda m: m.now_advice.airco_cost if m.now_advice else None,
        attrs=lambda m: {"aangenomen_cop": m.current_cop},
    ),
    EADescription(key="estimated_cop", suggested_display_precision=1, value=lambda m: m.cop_estimate),
    EADescription(
        key="heating_advice",
        device_class=SensorDeviceClass.ENUM,
        options=["airco", "gas", "unknown"],
        value=lambda m: "unknown" if m.now_advice is None else ("airco" if m.now_advice.airco_cheaper else "gas"),
        attrs=lambda m: {
            "airco_voordeliger": m.cheaper_blocks(),
            "verwachte_besparing": m.expected_saving(),
            "tekst": m.summary(),
        },
    ),
)

HUB: tuple[EADescription, ...] = (
    EADescription(
        key="daily_advice",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda m: m.generated,
        attrs=lambda m: {"advies": m.advice, "bron": m.advice_source, "feiten": m.facts},
    ),
    EADescription(
        key="current_price",
        native_unit_of_measurement="€/kWh",
        suggested_display_precision=3,
        value=lambda m: get_market(m.hass).current_price(),
        attrs=lambda m: {
            "prijzen_bekend_tot": _ts(max((e for _, e, _ in get_market(m.hass).prices()), default=None)),
            "aantal_prijsblokken": len(get_market(m.hass).prices()),
            "zonne_forecast": get_market(m.hass).solar_source,
        },
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = entry.runtime_data
    if isinstance(manager, ApplianceManager):
        descriptions = [d for d in APPLIANCE if manager.is_washer or not d.washer_only]
    elif isinstance(manager, HeatingManager):
        descriptions = list(HEATING)
    elif isinstance(manager, HubManager):
        descriptions = list(HUB)
    else:
        return
    async_add_entities(EASensor(manager, d) for d in descriptions)


class EASensor(EAEntity, SensorEntity):
    entity_description: EADescription

    def __init__(self, manager: Any, description: EADescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs
        return fn(self.manager) if fn else None
