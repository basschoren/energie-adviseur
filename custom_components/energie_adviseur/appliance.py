"""Beheert één apparaat: cycli detecteren, programma's leren, advies berekenen."""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta
from functools import partial
import logging
import time
from typing import Any
import uuid

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPLIANCE_TYPE,
    CONF_DRYER_ENTRY,
    CONF_POWER_ENTITY,
    CONF_START_W,
    CONF_STOP_DELAY,
    CONF_STOP_W,
    CONF_TOLERANCE,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DOMAIN,
    EVENT_CYCLE_FINISHED,
    MAX_CYCLES,
    TYPE_WASHER,
    UNAVAILABLE,
    signal,
)
from .core import SLOT_S
from .core.cycle_detector import Cycle, CycleDetector, DetectorConfig, quiet_gap, split_cycle
from .core.program_matcher import Program, match, rebuild
from .core.scheduler import Advice, ChainPlan, best_start, plan_chain
from .market import get_market, parse_time

_LOGGER = logging.getLogger(__name__)
TICK = timedelta(seconds=60)
SENSOR_LOST_S = 1800


def fmt_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}u{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


def fmt_time(ts: float) -> str:
    local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
    today = dt_util.now().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today + timedelta(days=1):
        return "morgen " + local.strftime("%H:%M")
    return local.strftime("%d-%m %H:%M")


def _same_run(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    """Overlappen twee perioden voor meer dan de helft van de kortste? Dan is het dezelfde beurt."""
    overlap = min(a_end, b_end) - max(a_start, b_start)
    return overlap > 0.5 * min(a_end - a_start, b_end - b_start)


class ApplianceManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        data = entry.data
        self.appliance_type: str = data[CONF_APPLIANCE_TYPE]
        self.power_entity: str = data[CONF_POWER_ENTITY]
        self.tolerance = float(data.get(CONF_TOLERANCE, 15)) / 100.0
        self.window = (
            parse_time(data.get(CONF_WINDOW_START), dtime(7, 0)),
            parse_time(data.get(CONF_WINDOW_END), dtime(22, 0)),
        )
        self.detector = CycleDetector(self._detector_config())
        self.programs: dict[str, Program] = {}
        self.cycles: list[Cycle] = []
        self.ignored: set[str] = set()  # cycli die bij het splitsen zijn vervallen
        self.removed: list[list[float]] = []  # [start, eind] van verwijderde cycli; inlezen slaat ze over
        self.selected: str | None = None
        self.loads = 0
        self.pending_dry = 0
        self.dry_program: str | None = None
        self.advice: Advice | None = None
        self.plan: ChainPlan | None = None
        self.signal = signal(entry.entry_id)
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Energie Adviseur"
        )
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._unsub: list = []
        self._lost_since: float | None = None
        self._ticks = 0
        self._plan_lock = asyncio.Lock()

    def _detector_config(self) -> DetectorConfig:
        data = self.entry.data
        return DetectorConfig(
            start_w=float(data.get(CONF_START_W, 10)),
            stop_w=float(data.get(CONF_STOP_W, 5)),
            stop_delay_s=float(data.get(CONF_STOP_DELAY, 5)) * 60.0,
        )

    # ------------------------------------------------------------ levensloop

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.programs = {p["program_id"]: Program.from_dict(p) for p in stored.get("programs", [])}
        self.cycles = [Cycle.from_dict(c) for c in stored.get("cycles", [])]
        self.ignored = set(stored.get("ignored", []))
        self.removed = [list(r) for r in stored.get("removed", [])]
        self.selected = stored.get("selected")
        self.loads = int(stored.get("loads", 0))
        self.pending_dry = int(stored.get("pending_dry", 0))
        self.dry_program = stored.get("dry_program")
        if stored.get("detector"):
            self.detector.load(stored["detector"])
        self._unsub = [
            async_track_state_change_event(self.hass, [self.power_entity], self._on_power),
            async_track_time_interval(self.hass, self._on_tick, TICK),
            self.hass.bus.async_listen(EVENT_CYCLE_FINISHED, self._on_other_cycle),
        ]
        self._refresh_advice()
        self.hass.async_create_task(self.async_replan())

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {
            "programs": [p.to_dict() for p in self.programs.values()],
            "cycles": [c.to_dict() for c in self.cycles[-MAX_CYCLES:]],
            "ignored": sorted(self.ignored)[-MAX_CYCLES:],
            "removed": self.removed[-MAX_CYCLES:],
            "selected": self.selected,
            "loads": self.loads,
            "pending_dry": self.pending_dry,
            "dry_program": self.dry_program,
            "detector": self.detector.to_dict(),
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 15)

    def notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------ meting

    @callback
    def _on_power(self, event: Event) -> None:
        state = event.data.get("new_state")
        if state is None or state.state in UNAVAILABLE:
            if self._lost_since is None:
                self._lost_since = time.time()
            return
        try:
            power = float(state.state)
        except ValueError:
            return
        if state.attributes.get("unit_of_measurement") == "kW":
            power *= 1000.0
        self._lost_since = None
        was_running = self.detector.running
        self._feed(state.last_updated.timestamp(), power)
        if self.detector.running != was_running:
            self.notify()

    @callback
    def _on_tick(self, now: datetime) -> None:
        power = None
        if self._lost_since is not None and time.time() - self._lost_since > SENSOR_LOST_S:
            power = 0.0  # sensor te lang weg: cyclus afsluiten
        self._feed(time.time(), power)
        self._ticks += 1
        self._refresh_advice()
        if self._ticks % 5 == 0:
            if self.detector.running:
                self._save()
            self.hass.async_create_task(self.async_replan())
        self.notify()

    def _feed(self, ts: float, power: float | None) -> None:
        cycle = self.detector.update(ts, power)
        if cycle is not None:
            self._handle_cycle(cycle)

    def _handle_cycle(self, cycle: Cycle, quiet: bool = False) -> None:
        result = match(cycle, list(self.programs.values()), self.tolerance)
        if result.program is not None:
            cycle.program_id = result.program.program_id
        self.cycles.append(cycle)
        self.cycles = self.cycles[-MAX_CYCLES:]
        if result.program is not None:
            rebuild(result.program, self.cycles)
        if quiet:
            return
        if self.is_washer and self.loads > 0:
            self.loads -= 1
            self.pending_dry += 1
        self._save()
        self.hass.bus.async_fire(
            EVENT_CYCLE_FINISHED,
            {
                "entry_id": self.entry.entry_id,
                "name": self.entry.title,
                "cycle_id": cycle.cycle_id,
                "program": result.program.name if result.program else None,
                "duration_min": round(cycle.duration_s / 60),
                "energy_kwh": cycle.energy_kwh,
            },
        )
        if result.program is None:
            persistent_notification.async_create(
                self.hass,
                f"{self.entry.title} draaide een programma dat ik nog niet ken "
                f"({fmt_duration(cycle.duration_s)}, {cycle.energy_kwh:.2f} kWh). "
                "Geef het een naam bij 'Laatste cyclus benoemen' of kies een bestaand programma "
                "bij 'Laatste cyclus toewijzen'.",
                title="Energie Adviseur: onbekend programma",
                notification_id=f"{DOMAIN}_{self.entry.entry_id}_unknown",
            )
        self._refresh_advice()
        self.hass.async_create_task(self.async_replan())
        self.notify()

    @callback
    def _on_other_cycle(self, event: Event) -> None:
        """Een droogbeurt van de gekoppelde droger is klaar."""
        if self.is_washer and event.data.get("entry_id") == self.entry.data.get(CONF_DRYER_ENTRY):
            if self.pending_dry > 0:
                self.pending_dry -= 1
                self._save()
                self.hass.async_create_task(self.async_replan())

    # ------------------------------------------------------------ programma's

    @property
    def is_washer(self) -> bool:
        return self.appliance_type == TYPE_WASHER

    @property
    def last_cycle(self) -> Cycle | None:
        return self.cycles[-1] if self.cycles else None

    def program_names(self) -> list[str]:
        return sorted(p.name for p in self.programs.values())

    def program_by_name(self, name: str | None) -> Program | None:
        wanted = (name or "").strip().casefold()
        return next((p for p in self.programs.values() if p.name.casefold() == wanted), None)

    def program_name(self, program_id: str | None) -> str | None:
        program = self.programs.get(program_id or "")
        return program.name if program else None

    @property
    def active_program(self) -> Program | None:
        """Het gekozen programma, of anders het meest gedraaide."""
        program = self.programs.get(self.selected or "")
        if program is None or program.count == 0:
            usable = [p for p in self.programs.values() if p.count > 0]
            program = max(usable, key=lambda p: p.count, default=None)
        return program

    def select_program(self, name: str) -> None:
        program = self.program_by_name(name)
        if program is None:
            raise HomeAssistantError(f"Onbekend programma: {name}")
        self.selected = program.program_id
        self._changed()

    def label_cycle(self, name: str, cycle_id: str | None = None) -> None:
        """Koppel een cyclus aan een (nieuw of bestaand) programma en leer ervan."""
        name = name.strip()
        if not name:
            raise HomeAssistantError("Geef een programmanaam op")
        cycle = self.last_cycle if cycle_id is None else next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden om te benoemen")
        program = self.program_by_name(name)
        if program is None:
            program = Program(uuid.uuid4().hex[:8], name)
            self.programs[program.program_id] = program
        previous = self.programs.get(cycle.program_id or "")
        cycle.program_id = program.program_id
        rebuild(program, self.cycles)
        if previous is not None and previous is not program:
            rebuild(previous, self.cycles)
        self._absorb_unlabeled()
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{self.entry.entry_id}_unknown")
        self._changed()

    def _absorb_unlabeled(self) -> None:
        """Eerdere onbekende cycli die nu op een programma lijken, krijgen dat programma."""
        touched: set[str] = set()
        for cycle in self.cycles:
            if cycle.program_id is None:
                result = match(cycle, list(self.programs.values()), self.tolerance)
                if result.program is not None:
                    cycle.program_id = result.program.program_id
                    touched.add(result.program.program_id)
        for program_id in touched:
            rebuild(self.programs[program_id], self.cycles)

    def rename_program(self, name: str, new_name: str) -> None:
        program = self.program_by_name(name)
        if program is None or not new_name.strip():
            raise HomeAssistantError(f"Onbekend programma: {name}")
        if self.program_by_name(new_name) not in (None, program):
            raise HomeAssistantError(f"Er bestaat al een programma met de naam {new_name}")
        program.name = new_name.strip()
        self._changed()

    def delete_program(self, name: str) -> None:
        program = self.program_by_name(name)
        if program is None:
            raise HomeAssistantError(f"Onbekend programma: {name}")
        del self.programs[program.program_id]
        for cycle in self.cycles:
            if cycle.program_id == program.program_id:
                cycle.program_id = None
        if self.selected == program.program_id:
            self.selected = None
        self._changed()

    def _describe(self, cycle: Cycle) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cycle_id": cycle.cycle_id,
            "start": dt_util.utc_from_timestamp(cycle.start).isoformat(),
            "duur": fmt_duration(cycle.duration_s),
            "kwh": cycle.energy_kwh,
        }
        gap = quiet_gap(cycle)
        if gap is not None:
            watt = sum(cycle.profile[gap[0] : gap[0] + gap[1]]) / gap[1]
            out["rustigste_periode"] = (
                f"na {fmt_duration(gap[0] * SLOT_S)}, {fmt_duration(gap[1] * SLOT_S)} lang, gemiddeld {watt:.0f} W"
            )
        return out

    def unlabeled(self) -> list[dict[str, Any]]:
        return [self._describe(c) for c in self.cycles[-30:] if c.program_id is None]

    def delete_cycle(self, cycle_id: str) -> None:
        """Verwijder een cyclus, bijvoorbeeld als dezelfde beurt twee keer is herkend.

        Het programma waar de cyclus bij hoorde wordt opnieuw berekend, en historie
        inlezen haalt de cyclus niet terug.
        """
        cycle = next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden om te verwijderen")
        previous = self.programs.get(cycle.program_id or "")
        self.cycles.remove(cycle)
        self.removed.append([cycle.start, cycle.end])
        if previous is not None:
            rebuild(previous, self.cycles)
        self._changed()

    def split_cycle(self, cycle_id: str | None = None, minutes: float | None = None, keep: str = "both") -> list[dict]:
        """Deel een cyclus in tweeën, bijvoorbeeld als twee beurten als één zijn geteld.

        Zonder `minutes` wordt geknipt op de rustigste periode. Met `keep` kan één
        helft worden weggegooid (nadraaien, kreukbeveiliging).
        """
        cycle = self.last_cycle if cycle_id is None else next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden om te splitsen")
        try:
            first, second = split_cycle(cycle, None if minutes is None else float(minutes) * 60.0)
        except ValueError as err:
            raise HomeAssistantError(f"Splitsen lukt niet: {err}") from err
        cfg = self.detector.config
        wanted = {"both": (first, second), "first": (first, None), "second": (None, second)}[keep]
        parts = [
            part
            for part in wanted
            if part is not None and part.duration_s >= cfg.min_duration_s and part.energy_kwh >= cfg.min_energy_kwh
        ]
        if not parts:
            raise HomeAssistantError("Na het splitsen blijft er geen bruikbare cyclus over; kies een ander moment")
        previous = self.programs.get(cycle.program_id or "")
        self.cycles.remove(cycle)
        self.ignored.add(cycle.cycle_id)
        for part in parts:
            self.ignored.discard(part.cycle_id)
            self.cycles = [c for c in self.cycles if c.cycle_id != part.cycle_id]
            self.cycles.append(part)
        self.cycles.sort(key=lambda c: c.start)
        if previous is not None:
            rebuild(previous, self.cycles)
        self._absorb_unlabeled()
        self._changed()
        return [self._describe(part) | {"programma": self.program_name(part.program_id)} for part in parts]

    def set_loads(self, value: int) -> None:
        self.loads = max(0, int(value))
        self._changed()

    def set_dry_program(self, name: str) -> None:
        dryer = self.dryer
        program = dryer.program_by_name(name) if dryer else None
        if program is None:
            raise HomeAssistantError(f"Onbekend droogprogramma: {name}")
        self.dry_program = program.program_id
        self._changed()

    def _changed(self) -> None:
        self._save()
        self._refresh_advice()
        self.hass.async_create_task(self.async_replan())
        self.notify()

    # ------------------------------------------------------------ advies

    def _refresh_advice(self) -> None:
        program = self.active_program
        grid = get_market(self.hass).grid()
        if program is None or grid is None:
            self.advice = None
            return
        allowed = get_market(self.hass).allowed(grid, *self.window)
        self.advice = best_start(grid, program.profile, allowed)

    @property
    def now_is_good(self) -> bool | None:
        """Kost nu starten hooguit 2 cent of 5% meer dan het beste moment?"""
        advice = self.advice
        if advice is None or advice.cost_now is None:
            return None
        return advice.cost_now - advice.cost <= max(0.02, 0.05 * abs(advice.cost))

    @property
    def dryer(self) -> ApplianceManager | None:
        entry = self.hass.config_entries.async_get_entry(self.entry.data.get(CONF_DRYER_ENTRY) or "")
        manager = getattr(entry, "runtime_data", None) if entry else None
        return manager if isinstance(manager, ApplianceManager) else None

    @property
    def active_dry_program(self) -> Program | None:
        dryer = self.dryer
        if dryer is None:
            return None
        program = dryer.programs.get(self.dry_program or "")
        return program if program and program.count else dryer.active_program

    def _busy_until(self, program: Program | None) -> float | None:
        if not self.detector.running or self.detector.start is None:
            return None
        expected = self.detector.start + (program.duration_s if program else 0)
        return max(expected, time.time())

    async def async_replan(self) -> None:
        """Wasdag: plan de resterende wasjes en droogbeurten."""
        if not self.is_washer:
            return
        async with self._plan_lock:
            dryer, wash, dry = self.dryer, self.active_program, self.active_dry_program
            market = get_market(self.hass)
            await market.async_refresh_solar()
            grid = market.grid()
            loads, pending = self.loads, self.pending_dry
            washer_free = self._busy_until(wash)
            ready = None
            if washer_free is not None and loads > 0:
                # de was die nu draait telt mee als wasje van vandaag
                loads, pending, ready = loads - 1, pending + 1, washer_free + 600
            if dryer is None or wash is None or dry is None or grid is None or loads + pending == 0:
                self.plan = None
            else:
                self.plan = await self.hass.async_add_executor_job(
                    partial(
                        plan_chain,
                        grid,
                        wash.profile,
                        dry.profile,
                        loads,
                        pending_dry=pending,
                        washer_free_at=washer_free,
                        dryer_free_at=dryer._busy_until(dry),
                        pending_dry_ready_at=ready,
                        allowed=market.allowed(grid, *self.window),
                    )
                )
        self.notify()

    def plan_steps(self) -> list[dict[str, Any]]:
        if self.plan is None:
            return []
        out = []
        for job in self.plan.as_list():
            what = "Was" if job["kind"] == "wash" else "Drogen"
            label = f"{what} {job['load']}" if job["load"] else "Drogen (ligt klaar)"
            out.append(
                {
                    "stap": label,
                    "start": dt_util.utc_from_timestamp(job["start"]).isoformat(),
                    "eind": dt_util.utc_from_timestamp(job["end"]).isoformat(),
                    "tekst": f"{label}: {fmt_time(job['start'])}–{fmt_time(job['end'])}",
                }
            )
        return out

    def plan_text(self) -> str:
        if self.loads + self.pending_dry == 0:
            return "Geen wasjes gepland"
        if self.plan is None:
            return "Geen planning mogelijk"
        steps = self.plan_steps()
        return f"{steps[0]['stap']} starten om {fmt_time(self.plan.as_list()[0]['start'])}" if steps else "Geen planning mogelijk"

    def summary(self) -> str | None:
        """Eén zin voor het dagadvies."""
        program, advice = self.active_program, self.advice
        if program is None or advice is None:
            return None
        text = (
            f"{self.entry.title} ({program.name}, {fmt_duration(program.duration_s)}, "
            f"{program.energy_kwh:.2f} kWh): beste start {fmt_time(advice.start)}, kosten ca. €{advice.cost:.2f}"
        )
        if advice.saving is not None and advice.saving > 0.005:
            text += f", €{advice.saving:.2f} goedkoper dan nu starten"
        if self.is_washer and self.plan is not None:
            text += ". Wasplanning: " + "; ".join(s["tekst"] for s in self.plan_steps())
            text += f" (totaal ca. €{self.plan.cost:.2f})"
        return text + "."

    # ------------------------------------------------------------ historie

    async def async_import_history(self, days: int) -> int:
        """Haal eerdere cycli uit de recorder (zolang die de ruwe metingen bewaart)."""
        from homeassistant.components.recorder import get_instance, history

        if "recorder" not in self.hass.config.components:
            raise HomeAssistantError("De recorder van Home Assistant is niet actief; er is geen historie om in te lezen")
        end = dt_util.utcnow()
        start = end - timedelta(days=days)
        result = await get_instance(self.hass).async_add_executor_job(
            partial(history.state_changes_during_period, self.hass, start, end, self.power_entity, no_attributes=True)
        )
        current = self.hass.states.get(self.power_entity)
        unit_kw = current is not None and current.attributes.get("unit_of_measurement") == "kW"
        detector = CycleDetector(self._detector_config())
        known = {c.cycle_id for c in self.cycles} | self.ignored
        found: list[Cycle] = []
        last_ts: float | None = None
        delay = detector.config.stop_delay_s + 1

        # perioden die al bekend of bewust verwijderd zijn; de starttijd kan een paar
        # seconden verschillen met de live gemeten cyclus, dus vergelijken op overlap
        taken = [(c.start, c.end) for c in self.cycles] + [(r[0], r[1]) for r in self.removed]

        def feed(ts: float, power: float | None) -> None:
            cycle = detector.update(ts, power)
            if cycle is None or cycle.cycle_id in known:
                return
            if any(_same_run(cycle.start, cycle.end, start, end) for start, end in taken):
                return
            found.append(cycle)

        for state in result.get(self.power_entity, []):
            ts = state.last_updated.timestamp()
            if last_ts is not None and ts - last_ts > delay:
                feed(last_ts + delay, None)
            if state.state in UNAVAILABLE:
                continue
            try:
                power = float(state.state) * (1000.0 if unit_kw else 1.0)
            except ValueError:
                continue
            feed(ts, power)
            last_ts = ts
        current_start = self.detector.start if self.detector.running else None
        for cycle in found:
            if current_start is not None and cycle.end >= current_start:
                continue
            self._handle_cycle(cycle, quiet=True)
        self.cycles.sort(key=lambda c: c.start)
        self._changed()
        return len(found)
