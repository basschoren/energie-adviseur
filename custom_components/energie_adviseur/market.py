"""Prijzen en zonne-opwek uit Home Assistant, omgezet naar een planningsraster."""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_LOAD,
    CONF_EXPORT_DEDUCTION,
    CONF_KIND,
    CONF_PRESENCE_CALENDARS,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MULTIPLIER,
    CONF_SOLAR_ENTITY,
    CONF_USE_ENERGY_DASHBOARD,
    DOMAIN,
    KIND_HUB,
    UNAVAILABLE,
)
from .core import SLOT_S
from .core.presence import Period, is_away, merge
from .core.price import SOLAR_KEYS, Interval, parse_intervals, price_scale, to_epoch
from .core.scheduler import Grid, build_grid

_LOGGER = logging.getLogger(__name__)
SOLAR_REFRESH_S = 1800
PRESENCE_REFRESH_S = 300
PRESENCE_DAYS = 3  # zo ver vooruit worden de agenda's gelezen


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
        self._away: list[Period] = []
        self._away_ts = 0.0

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

    # ------------------------------------------------------------ aanwezigheid

    @property
    def away(self) -> list[Period]:
        """Perioden waarin je volgens de agenda niet thuis bent; daarin wordt geen start gepland."""
        return [p for p in self._away if p[1] > time.time()]

    def away_periods(self) -> list[dict[str, str]]:
        iso = lambda ts: dt_util.utc_from_timestamp(ts).isoformat()  # noqa: E731
        return [{"start": iso(start), "eind": iso(end)} for start, end in self.away]

    async def async_refresh_presence(self, force: bool = False) -> None:
        if not force and time.time() - self._away_ts < PRESENCE_REFRESH_S:
            return
        self._away_ts = time.time()
        calendars = (self.hub or {}).get(CONF_PRESENCE_CALENDARS) or []
        start = dt_util.utcnow()
        end = start + timedelta(days=PRESENCE_DAYS)
        away: list[Period] = []
        for entity_id in calendars:
            try:
                away += await self._busy_from_calendar(entity_id, start, end)
            except Exception:  # noqa: BLE001 - een agenda die niet antwoordt mag de planning niet stilleggen
                _LOGGER.warning("Agenda %s lezen is mislukt; die telt nu niet mee als afwezigheid", entity_id, exc_info=True)
        self._away = merge(away)

    async def _busy_from_calendar(self, entity_id: str, start: datetime, end: datetime) -> list[Period]:
        """Afspraken waarin je bezet bent. Een afspraak die in de agenda op 'vrij' staat, telt niet mee."""
        from homeassistant.components.calendar import DATA_COMPONENT

        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component else None
        if entity is None:
            return []
        spans: list[tuple[Any, Any]] | None = None
        coordinator = getattr(entity, "coordinator", None)
        event_filter = getattr(entity, "_event_filter", None)
        if hasattr(coordinator, "async_get_events") and callable(event_filter):
            # Google Agenda: Home Assistant geeft bezet/vrij niet door in de gewone lijst met afspraken,
            # dus lezen we de afspraken zoals Google ze levert. Lukt dat niet, dan volgt de gewone lijst.
            try:
                spans = [
                    (event.start.value, event.end.value)
                    for event in await coordinator.async_get_events(start, end)
                    if event_filter(event) and getattr(event, "transparency", "opaque") != "transparent"
                ]
            except Exception:  # noqa: BLE001 - interne API van de Google-integratie kan per versie verschillen
                _LOGGER.debug("Bezet/vrij van %s lezen is mislukt; alle afspraken tellen als bezet", entity_id, exc_info=True)
        if spans is None:
            spans = [(event.start, event.end) for event in await entity.async_get_events(self.hass, start, end)]
        return [(self._epoch(a), self._epoch(b)) for a, b in spans]

    @staticmethod
    def _epoch(value: date | datetime) -> float:
        """Tijdstip van een afspraak; een afspraak voor de hele dag begint en eindigt om middernacht."""
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())).timestamp()
        return dt_util.start_of_local_day(value).timestamp()

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

    @staticmethod
    def day_bounds(offset: int) -> tuple[float, float]:
        """Begin en einde (epoch) van vandaag (0) of morgen (1) in de tijdzone van Home Assistant."""
        day = dt_util.now().date() + timedelta(days=offset)
        start = dt_util.start_of_local_day(day)
        end = dt_util.start_of_local_day(day + timedelta(days=1))
        return start.timestamp(), end.timestamp()

    def tomorrow_known(self, grid: Grid | None = None) -> bool:
        """Zijn de prijzen van heel morgen bekend?"""
        grid = grid if grid is not None else self.grid()
        return grid is not None and grid.end >= self.day_bounds(1)[1]

    def allowed(self, grid: Grid, window_start: dtime, window_end: dtime) -> list[bool]:
        """Per blok: valt de starttijd binnen het venster waarin je het apparaat kunt aanzetten, en ben je dan thuis?"""
        out = []
        away = self.away
        for idx in range(len(grid)):
            ts = grid.t0 + idx * SLOT_S
            local = dt_util.as_local(dt_util.utc_from_timestamp(ts)).time()
            if window_start <= window_end:
                inside = window_start <= local <= window_end
            else:
                inside = local >= window_start or local <= window_end
            out.append(inside and not is_away(away, ts))
        return out


def get_market(hass: HomeAssistant) -> Market:
    return hass.data[DOMAIN]["market"]
