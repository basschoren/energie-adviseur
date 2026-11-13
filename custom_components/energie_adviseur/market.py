"""Prijzen en zonne-opwek uit Home Assistant, omgezet naar een planningsraster."""

from __future__ import annotations

from datetime import time as dtime
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_LOAD,
    CONF_EXPORT_DEDUCTION,
    CONF_KIND,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MULTIPLIER,
    CONF_SOLAR_ENTITY,
    CONF_USE_ENERGY_DASHBOARD,
    DOMAIN,
    KIND_HUB,
    UNAVAILABLE,
)
from .core import SLOT_S
from .core.price import SOLAR_KEYS, Interval, parse_intervals, price_scale, to_epoch
from .core.scheduler import Grid, build_grid

_LOGGER = logging.getLogger(__name__)
SOLAR_REFRESH_S = 1800


def parse_time(value: Any, default: dtime) -> dtime:
    try:
        return dtime.fromisoformat(str(value))
    except ValueError:
        return default


class Market:
    """Eén gedeeld object; leest bij elk gebruik de actuele sensoren."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._solar: list[Interval] = []
        self._solar_ts = 0.0
        self.solar_source = "geen"

    @property
    def hub(self) -> dict[str, Any] | None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_KIND) == KIND_HUB:
                return dict(entry.data)
        return None

    def prices(self) -> list[Interval]:
        """Stroomprijs in EUR/kWh per interval, uit de attributen van de prijssensor."""
        hub = self.hub
        state = self.hass.states.get(hub[CONF_PRICE_ENTITY]) if hub else None
        if state is None:
            return []
        scale = price_scale(state.attributes.get("unit_of_measurement")) * float(hub.get(CONF_PRICE_MULTIPLIER) or 1.0)
        tz = dt_util.get_default_time_zone()
        return parse_intervals(dict(state.attributes), default_tz=tz, scale=scale)

    def current_price(self) -> float | None:
        now = time.time()
        for start, end, value in self.prices():
            if start <= now < end:
                return value
        hub = self.hub
        state = self.hass.states.get(hub[CONF_PRICE_ENTITY]) if hub else None
        if state is None or state.state in UNAVAILABLE:
            return None
        try:
            return float(state.state) * price_scale(state.attributes.get("unit_of_measurement")) * float(
                hub.get(CONF_PRICE_MULTIPLIER) or 1.0
            )
        except ValueError:
            return None

    async def async_refresh_solar(self, force: bool = False) -> None:
        if not force and time.time() - self._solar_ts < SOLAR_REFRESH_S:
            return
        self._solar_ts = time.time()
        hub = self.hub
        if not hub:
            return
        solar: list[Interval] = []
        source = "geen"
        if hub.get(CONF_SOLAR_ENTITY):
            solar = self._solar_from_entity(hub[CONF_SOLAR_ENTITY])
            source = "sensor"
        if not solar and hub.get(CONF_USE_ENERGY_DASHBOARD, True):
            try:
                solar = await self._solar_from_energy_dashboard()
                source = "energie-dashboard"
            except Exception:  # noqa: BLE001 - interne API van HA kan per versie verschillen
                _LOGGER.warning("Zonne-forecast uit het Energie-dashboard lezen is mislukt", exc_info=True)
        self._solar = solar
        self.solar_source = source if solar else "geen"

    def _solar_from_entity(self, entity_id: str) -> list[Interval]:
        """Bijv. Solcast: lijst met period_start en pv_estimate (kW)."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        tz = dt_util.get_default_time_zone()
        intervals = parse_intervals(dict(state.attributes), value_keys=SOLAR_KEYS, default_tz=tz)
        if intervals and max(v for _, _, v in intervals) < 100:  # kW in plaats van W
            intervals = [(s, e, v * 1000.0) for s, e, v in intervals]
        return intervals

    async def _solar_from_energy_dashboard(self) -> list[Interval]:
        from homeassistant.components.energy import data as energy_data, websocket_api as energy_ws

        manager = await energy_data.async_get_manager(self.hass)
        platforms = await energy_ws.async_get_energy_platforms(self.hass)
        totals: dict[float, float] = {}
        for source in (manager.data or {}).get("energy_sources", []):
            if source.get("type") != "solar":
                continue
            for entry_id in source.get("config_entry_solar_forecast") or []:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is None or entry.domain not in platforms:
                    continue
                forecast = await platforms[entry.domain](self.hass, entry_id)
                for when, wh in ((forecast or {}).get("wh_hours") or {}).items():
                    ts = to_epoch(when)
                    if ts is not None:
                        totals[ts] = totals.get(ts, 0.0) + float(wh)
        starts = sorted(totals)
        steps = [b - a for a, b in zip(starts, starts[1:])]
        step = min(steps) if steps else 3600.0
        return [(s, s + step, totals[s] * 3600.0 / step) for s in starts]

    def grid(self) -> Grid | None:
        hub = self.hub
        prices = self.prices()
        if not hub or not prices:
            return None
        grid = build_grid(
            time.time(),
            prices,
            self._solar,
            float(hub.get(CONF_BASE_LOAD) or 0.0),
            float(hub.get(CONF_EXPORT_DEDUCTION) or 0.0),
        )
        return grid if len(grid) else None

    def allowed(self, grid: Grid, window_start: dtime, window_end: dtime) -> list[bool]:
        """Per blok: valt de starttijd binnen het venster waarin je het apparaat kunt aanzetten?"""
        out = []
        for idx in range(len(grid)):
            local = dt_util.as_local(dt_util.utc_from_timestamp(grid.t0 + idx * SLOT_S)).time()
            if window_start <= window_end:
                out.append(window_start <= local <= window_end)
            else:
                out.append(local >= window_start or local <= window_end)
        return out


def get_market(hass: HomeAssistant) -> Market:
    return hass.data[DOMAIN]["market"]
