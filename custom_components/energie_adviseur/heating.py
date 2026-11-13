"""Houdt gasverbruik, temperaturen en airco-verbruik bij en geeft stookadvies."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .appliance import fmt_time
from .const import (
    CONF_AIRCO_ENTITIES,
    CONF_BOILER_EFF,
    CONF_COP_7,
    CONF_COP_MINUS7,
    CONF_GAS_ENTITY,
    CONF_GAS_PRICE,
    CONF_GAS_PRICE_ENTITY,
    CONF_INDOOR_ENTITIES,
    CONF_OUTDOOR_ENTITY,
    DOMAIN,
    MAX_DAYS,
    UNAVAILABLE,
    signal,
)
from .core.heating_model import DayRecord, HeatModel, HourAdvice, cop_at, fit, hourly_advice, implied_cop
from .core.price import to_epoch
from .market import get_market

_LOGGER = logging.getLogger(__name__)
SAMPLE = timedelta(minutes=5)
MAX_GAP_H = 0.5  # langere gaten tellen niet mee


class HeatingManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        data = entry.data
        self.gas_entity: str = data[CONF_GAS_ENTITY]
        self.indoor: list[str] = list(data.get(CONF_INDOOR_ENTITIES) or [])
        self.outdoor: str = data[CONF_OUTDOOR_ENTITY]
        self.aircos: list[str] = list(data.get(CONF_AIRCO_ENTITIES) or [])
        self.boiler_eff = float(data.get(CONF_BOILER_EFF, 95)) / 100.0
        self.cop_7 = float(data.get(CONF_COP_7, 4.0))
        self.cop_minus7 = float(data.get(CONF_COP_MINUS7, 2.5))
        self.days: list[DayRecord] = []
        self.today: dict[str, Any] = {}
        self.model: HeatModel | None = None
        self.cop_estimate: float | None = None
        self.hours: list[HourAdvice] = []
        self.signal = signal(entry.entry_id)
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Energie Adviseur"
        )
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._unsub: list = []
        self._last: dict[str, float] = {}  # laatste meterstanden
        self._last_ts: float | None = None
        self._forecast: list[tuple[float, float]] = []
        self._forecast_ts = 0.0

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.days = [DayRecord.from_dict(d) for d in stored.get("days", [])]
        self.today = stored.get("today") or {}
        self._last = stored.get("last") or {}
        self._refit()
        self._unsub = [async_track_time_interval(self.hass, self._on_sample, SAMPLE)]
        if not self.days:
            self.hass.async_create_task(self._first_import())
        self.hass.async_create_task(self.async_refresh_advice())

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"days": [d.to_dict() for d in self.days[-MAX_DAYS:]], "today": self.today, "last": self._last}

    def notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------ sensoren lezen

    def _number(self, entity_id: str, attribute: str | None = None) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE:
            return None
        try:
            return float(state.attributes[attribute] if attribute else state.state)
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def t_in(self) -> float | None:
        values = [v for v in (self._number(e) for e in self.indoor) if v is not None]
        return sum(values) / len(values) if values else None

    @property
    def t_out(self) -> float | None:
        if self.outdoor.startswith("weather."):
            return self._number(self.outdoor, "temperature")
        return self._number(self.outdoor)

    @property
    def gas_price(self) -> float:
        entity = self.entry.data.get(CONF_GAS_PRICE_ENTITY)
        value = self._number(entity) if entity else None
        return value if value is not None else float(self.entry.data.get(CONF_GAS_PRICE, 1.30))

    # ------------------------------------------------------------ dagtotalen

    @callback
    def _on_sample(self, now: datetime) -> None:
        ts = time.time()
        day = dt_util.now().date().isoformat()
        if self.today.get("day") != day:
            self._close_day()
            self.today = {"day": day, "gas_m3": 0.0, "degree_hours": 0.0, "airco_kwh": 0.0, "hours": 0.0}
        dt_h = (ts - self._last_ts) / 3600.0 if self._last_ts else 0.0
        self._last_ts = ts
        valid = 0 < dt_h <= MAX_GAP_H

        gas = self._number(self.gas_entity)
        if gas is not None:
            delta = gas - self._last.get(self.gas_entity, gas)
            if 0 <= delta < 20:
                self.today["gas_m3"] += delta
            self._last[self.gas_entity] = gas

        for entity in self.aircos:
            value = self._number(entity)
            if value is None:
                continue
            unit = (self.hass.states.get(entity).attributes.get("unit_of_measurement") or "").lower()
            if unit in ("w", "kw"):  # vermogen: integreren
                if valid:
                    self.today["airco_kwh"] += value * (0.001 if unit == "w" else 1.0) * dt_h
            else:  # energieteller
                factor = 0.001 if unit == "wh" else 1.0
                delta = (value - self._last.get(entity, value)) * factor
                if 0 <= delta < 50:
                    self.today["airco_kwh"] += delta
                self._last[entity] = value

        t_in, t_out = self.t_in, self.t_out
        if valid and t_in is not None and t_out is not None:
            self.today["degree_hours"] += max(0.0, t_in - t_out) * dt_h
            self.today["hours"] += dt_h
        self._store.async_delay_save(self._data, 60)
        self.hass.async_create_task(self.async_refresh_advice())

    def _close_day(self) -> None:
        today = self.today
        if not today.get("day"):
            return
        record = DayRecord(
            day=today["day"],
            gas_m3=round(today["gas_m3"], 3),
            degree_days=round(today["degree_hours"] / 24.0, 3),
            airco_kwh=round(today["airco_kwh"], 3),
            hours=round(today["hours"]),
        )
        self.days = [d for d in self.days if d.day != record.day] + [record]
        self.days.sort(key=lambda d: d.day)
        self._refit()

    def _refit(self) -> None:
        self.model = fit(self.days)
        self.cop_estimate = implied_cop(self.days, self.model, self.boiler_eff) if self.model else None

    # ------------------------------------------------------------ advies

    async def _outdoor_forecast(self) -> list[tuple[float, float]]:
        """Uurverwachting buitentemperatuur, als de buitensensor een weer-entiteit is."""
        if not self.outdoor.startswith("weather."):
            return []
        if time.time() - self._forecast_ts < 3600:
            return self._forecast
        self._forecast_ts = time.time()
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self.outdoor, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            items = (response or {}).get(self.outdoor, {}).get("forecast", [])
            self._forecast = [
                (ts, float(item["temperature"]))
                for item in items
                if (ts := to_epoch(item.get("datetime"))) is not None and item.get("temperature") is not None
            ]
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Weersverwachting ophalen mislukt", exc_info=True)
            self._forecast = []
        return self._forecast

    async def async_refresh_advice(self) -> None:
        t_out_now = self.t_out
        prices = get_market(self.hass).prices()
        forecast = await self._outdoor_forecast()
        now = time.time()
        hour = now - now % 3600
        hours: list[tuple[float, float, float]] = []
        while len(hours) < 48:
            inside = [v for s, e, v in prices if s < hour + 3600 and e > hour]
            if not inside:
                break
            near = min(forecast, key=lambda f: abs(f[0] - hour), default=None)
            t_out = near[1] if near and abs(near[0] - hour) <= 5400 else t_out_now
            if t_out is None:
                break
            hours.append((hour, sum(inside) / len(inside), t_out))
            hour += 3600
        if not hours and t_out_now is not None:
            price = get_market(self.hass).current_price()
            if price is not None:
                hours = [(now - now % 3600, price, t_out_now)]
        self.hours = hourly_advice(
            hours, self.gas_price, self.boiler_eff, self.cop_7, self.cop_minus7, self.t_in or 20.0, self.model
        )
        self.notify()

    @property
    def now_advice(self) -> HourAdvice | None:
        return self.hours[0] if self.hours else None

    @property
    def current_cop(self) -> float | None:
        t_out = self.t_out
        return round(cop_at(t_out, self.cop_7, self.cop_minus7), 2) if t_out is not None else None

    def cheaper_blocks(self) -> list[str]:
        """Aaneengesloten perioden waarin de airco goedkoper stookt dan gas."""
        blocks: list[str] = []
        start: float | None = None
        for hour in self.hours + [None]:
            cheaper = hour is not None and hour.airco_cheaper
            if cheaper and start is None:
                start = hour.start
            elif not cheaper and start is not None:
                end = hour.start if hour is not None else self.hours[-1].start + 3600
                blocks.append(f"{fmt_time(start)}–{fmt_time(end)}")
                start = None
        return blocks

    def expected_saving(self) -> float | None:
        """Besparing als je in alle goedkopere uren met de airco stookt (binnen de bekende prijzen)."""
        savings = [h.saving for h in self.hours]
        if not savings or any(s is None for s in savings):
            return None
        return round(sum(savings), 2)

    def summary(self) -> str | None:
        now = self.now_advice
        if now is None:
            return None
        blocks = self.cheaper_blocks()
        text = (
            f"Verwarming: warmte uit gas kost €{now.gas_cost:.3f}/kWh, uit de airco nu €{now.airco_cost:.3f}/kWh "
            f"(aangenomen COP {self.current_cop}). "
        )
        text += ("Airco is voordeliger: " + ", ".join(blocks)) if blocks else "Gas is de komende uren voordeliger"
        saving = self.expected_saving()
        if blocks and saving:
            text += f" (verwachte besparing ca. €{saving:.2f})"
        return text + "."

    # ------------------------------------------------------------ historie

    async def _first_import(self) -> None:
        try:
            count = await self.async_import_history(120)
            _LOGGER.info("Verwarming: %s dagen historie ingelezen", count)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Historie voor verwarming inlezen is mislukt", exc_info=True)

    async def async_import_history(self, days: int) -> int:
        """Bouw dagtotalen uit de langetermijnstatistieken van de recorder."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        if "recorder" not in self.hass.config.components:
            raise HomeAssistantError("De recorder van Home Assistant is niet actief; er is geen historie om in te lezen")
        end = dt_util.start_of_local_day()
        start = end - timedelta(days=days)
        outdoor_stat = None if self.outdoor.startswith("weather.") else self.outdoor
        ids = {self.gas_entity, *self.indoor, *self.aircos}
        if outdoor_stat:
            ids.add(outdoor_stat)
        stats = await get_instance(self.hass).async_add_executor_job(
            partial(statistics_during_period, self.hass, start, end, ids, "hour", None, {"sum", "mean", "state"})
        )

        def rows(entity: str) -> dict[float, dict[str, Any]]:
            out = {}
            for row in stats.get(entity, []):
                ts = to_epoch(row.get("start"))
                if ts is None and isinstance(row.get("start"), (int, float)):
                    ts = float(row["start"])
                if ts is not None:
                    out[ts / 1000.0 if ts > 1e11 else ts] = row
            return out

        def deltas(entity: str) -> dict[float, float]:
            series = sorted(rows(entity).items())
            out = {}
            for (_, prev), (ts, cur) in zip(series, series[1:]):
                if prev.get("sum") is not None and cur.get("sum") is not None:
                    out[ts] = max(0.0, cur["sum"] - prev["sum"])
            return out

        gas = deltas(self.gas_entity)
        airco: dict[float, float] = {}
        for entity in self.aircos:
            state = self.hass.states.get(entity)
            unit = (state.attributes.get("unit_of_measurement") or "").lower() if state else ""
            if unit in ("w", "kw"):
                for ts, row in rows(entity).items():
                    if row.get("mean") is not None:
                        airco[ts] = airco.get(ts, 0.0) + row["mean"] * (0.001 if unit == "w" else 1.0)
            else:
                for ts, value in deltas(entity).items():
                    airco[ts] = airco.get(ts, 0.0) + value * (0.001 if unit == "wh" else 1.0)
        indoor = [rows(e) for e in self.indoor]
        outdoor = rows(outdoor_stat) if outdoor_stat else {}

        totals: dict[str, dict[str, float]] = {}
        for ts, m3 in gas.items():
            day = dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()
            acc = totals.setdefault(day, {"gas": 0.0, "dh": 0.0, "airco": 0.0, "hours": 0.0})
            acc["gas"] += m3
            acc["airco"] += airco.get(ts, 0.0)
            temps = [r[ts]["mean"] for r in indoor if ts in r and r[ts].get("mean") is not None]
            t_out = outdoor.get(ts, {}).get("mean")
            if temps and t_out is not None:
                acc["dh"] += max(0.0, sum(temps) / len(temps) - t_out)
                acc["hours"] += 1
        known = {d.day for d in self.days}
        added = 0
        for day, acc in totals.items():
            if day in known or acc["hours"] < 20:
                continue
            self.days.append(
                DayRecord(day, round(acc["gas"], 3), round(acc["dh"] / 24.0, 3), round(acc["airco"], 3), int(acc["hours"]))
            )
            added += 1
        self.days.sort(key=lambda d: d.day)
        self._refit()
        self._store.async_delay_save(self._data, 5)
        await self.async_refresh_advice()
        return added
