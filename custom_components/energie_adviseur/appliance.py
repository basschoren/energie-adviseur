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
    CONF_MIN_RUNNING_S,
    CONF_PAUSED_STATES,
    CONF_POWER_ENTITY,
    CONF_RUNNING_STATES,
    CONF_START_W,
    CONF_STATE_ENTITY,
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
from .core import DETAIL_S, SLOT_S
from .core.cycle_detector import (
    OTHER,
    PAUSED,
    RUNNING,
    Cycle,
    CycleDetector,
    DetectorConfig,
    measure_cycle,
    quiet_gap,
    split_cycle,
    trim_cycle,
)
from .core.program_matcher import Program, match, rebuild
from .core.scheduler import Advice, ChainPlan, best_start, day_mask, plan_chain
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


def fmt_clock(ts: float) -> str:
    """Alleen de kloktijd, voor zinnen die de dag al noemen."""
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).strftime("%H:%M")


MAX_ORIGINALS = 50
MAX_CYCLE_S = 86400  # langer dan een etmaal is geen cyclus
CONTEXT_MARGIN_S = 3600  # hoeveel de pagina voor en na een cyclus laat zien bij het aanpassen


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
        # optionele statussensor van een slim apparaat: bepaalt begin, einde en pauzes
        self.state_entity: str | None = data.get(CONF_STATE_ENTITY) or None
        self.running_states = {str(s).strip().casefold() for s in data.get(CONF_RUNNING_STATES) or ["running"]}
        self.paused_states = {str(s).strip().casefold() for s in data.get(CONF_PAUSED_STATES) or ["paused"]}
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
        self.originals: list[dict[str, Any]] = []  # cycli van voor een splitsing, om die ongedaan te maken
        self.selected: str | None = None
        # naam die de cyclus die nu loopt alvast heeft gekregen: {"name": ..., "start": begin van die cyclus}
        self.running_name: dict[str, Any] | None = None
        self.loads = 0
        self.pending_dry = 0
        self.dry_program: str | None = None
        # programma per ronde van de wasdag (programma-id; None volgt het gekozen programma)
        self.wash_rounds: list[str | None] = []  # per wasje dat nog moet draaien
        self.dry_rounds: list[str | None] = []  # de droogbeurt bij elk van die wasjes
        self.pending_dry_programs: list[str | None] = []  # per droogbeurt die klaarligt
        self.plan_names: dict[str, list[str]] = {}  # programmanamen bij de beurten in het plan
        self.plan_offset = 0  # 1 als ronde 1 nu draait: het plan begint dan bij was 2
        self.unplanned = 0  # wasjes die niet meer binnen de bekende prijzen passen
        self.advice: Advice | None = None  # beste start over alle bekende prijzen
        self.advice_today: Advice | None = None  # beste start die vandaag begint
        self.advice_tomorrow: Advice | None = None  # beste start morgen; alleen als de prijzen van morgen bekend zijn
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
        config = DetectorConfig(
            start_w=float(data.get(CONF_START_W, 10)),
            stop_w=float(data.get(CONF_STOP_W, 5)),
            stop_delay_s=float(data.get(CONF_STOP_DELAY, 5)) * 60.0,
        )
        if self.state_entity:
            config.state_mode = True
            config.start_delay_s = float(data.get(CONF_MIN_RUNNING_S, 60))
        return config

    def _phase(self, value: Any) -> str | None:
        """Vertaal de status van het apparaat; None als de sensor niets bruikbaars meldt."""
        if value in UNAVAILABLE:
            return None
        text = str(value).strip().casefold()
        if text in self.running_states:
            return RUNNING
        if text in self.paused_states:
            return PAUSED
        return OTHER

    # ------------------------------------------------------------ levensloop

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.programs = {p["program_id"]: Program.from_dict(p) for p in stored.get("programs", [])}
        self.cycles = [Cycle.from_dict(c) for c in stored.get("cycles", [])]
        self.ignored = set(stored.get("ignored", []))
        self.removed = [list(r) for r in stored.get("removed", [])]
        self.originals = list(stored.get("originals", []))
        self.selected = stored.get("selected")
        self.running_name = stored.get("running_name")
        self.loads = int(stored.get("loads", 0))
        self.pending_dry = int(stored.get("pending_dry", 0))
        self.dry_program = stored.get("dry_program")
        self.wash_rounds = list(stored.get("wash_rounds", []))
        self.dry_rounds = list(stored.get("dry_rounds", []))
        self.pending_dry_programs = list(stored.get("pending_dry_programs", []))
        self._sync_rounds()
        # een lopende meting alleen hervatten als de manier van detecteren niet is gewijzigd
        if stored.get("detector") and stored.get("state_mode", False) == self.detector.config.state_mode:
            self.detector.load(stored["detector"])
        self._unsub = [
            async_track_state_change_event(self.hass, [self.power_entity], self._on_power),
            async_track_time_interval(self.hass, self._on_tick, TICK),
            self.hass.bus.async_listen(EVENT_CYCLE_FINISHED, self._on_other_cycle),
        ]
        if self.state_entity:
            self._unsub.append(async_track_state_change_event(self.hass, [self.state_entity], self._on_state))
            # de status van nu overnemen: na een herstart komt er geen wijziging als het apparaat al draait
            current = self.hass.states.get(self.state_entity)
            phase = self._phase(current.state) if current else None
            if phase is not None:
                # ook het vermogen van nu, anders telt het verbruik pas mee vanaf de volgende meting
                power = self.hass.states.get(self.power_entity)
                try:
                    watt = float(power.state) if power is not None else None
                except ValueError:
                    watt = None
                if watt is not None:
                    self.detector.last_power = watt * (1000.0 if power.attributes.get("unit_of_measurement") == "kW" else 1.0)
                self._feed_state(time.time(), phase)
        self.refresh_advice()
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
            "originals": self.originals[-MAX_ORIGINALS:],
            "selected": self.selected,
            "running_name": self.running_name,
            "loads": self.loads,
            "pending_dry": self.pending_dry,
            "dry_program": self.dry_program,
            "wash_rounds": self.wash_rounds,
            "dry_rounds": self.dry_rounds,
            "pending_dry_programs": self.pending_dry_programs,
            "detector": self.detector.to_dict(),
            "state_mode": self.detector.config.state_mode,
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
    def _on_state(self, event: Event) -> None:
        state = event.data.get("new_state")
        phase = self._phase(state.state) if state is not None else None
        if phase is None:
            return  # sensor even weg: de laatste bekende status blijft gelden
        was_running = self.detector.running
        self._feed_state(state.last_updated.timestamp(), phase)
        if self.detector.running != was_running:
            self.notify()

    def _feed_state(self, ts: float, phase: str) -> None:
        self._after_update(self.detector.update_state(ts, phase))

    @callback
    def _on_tick(self, now: datetime) -> None:
        power = None
        if self._lost_since is not None and time.time() - self._lost_since > SENSOR_LOST_S:
            power = 0.0  # sensor te lang weg: cyclus afsluiten
        self._feed(time.time(), power)
        self._ticks += 1
        self.refresh_advice()
        # zonder plan elke minuut opnieuw proberen: vlak na het opstarten is de droger nog niet geladen
        waiting = self.is_washer and self.plan is None and self.loads + self.pending_dry > 0
        if self._ticks % 5 == 0 or waiting:
            if self.detector.running and self._ticks % 5 == 0:
                self._save()
            self.hass.async_create_task(self.async_replan())
        self.notify()

    def _feed(self, ts: float, power: float | None) -> None:
        self._after_update(self.detector.update(ts, power))

    def _after_update(self, cycle: Cycle | None) -> None:
        if cycle is not None:
            self._handle_cycle(cycle)
        elif self.running_name is not None and not self.detector.running:
            # de beurt is gestopt zonder bruikbare cyclus (te kort, te weinig verbruik): naam vervalt
            self.running_name = None
            self._save()

    def _handle_cycle(self, cycle: Cycle, quiet: bool = False) -> None:
        named = None if quiet else self._take_running_name(cycle)
        if named is not None:
            program: Program | None = self._program_for(named)  # de gekozen naam gaat voor herkenning
        else:
            program = match(cycle, list(self.programs.values()), self.tolerance).program
        if program is not None:
            cycle.program_id = program.program_id
        self.cycles.append(cycle)
        self.cycles = self.cycles[-MAX_CYCLES:]
        if program is not None:
            rebuild(program, self.cycles)
        if named is not None:
            self._absorb_unlabeled()
        if quiet:
            return
        if self.is_washer and self.loads > 0:
            # deze ronde is gewassen: zijn droogprogramma sluit achteraan bij wat klaarligt
            self._sync_rounds()
            self.loads -= 1
            self.pending_dry += 1
            self.wash_rounds.pop(0)
            self.pending_dry_programs.append(self.dry_rounds.pop(0))
        self._save()
        self.hass.bus.async_fire(
            EVENT_CYCLE_FINISHED,
            {
                "entry_id": self.entry.entry_id,
                "name": self.entry.title,
                "cycle_id": cycle.cycle_id,
                "program": program.name if program else None,
                "duration_min": round(cycle.duration_s / 60),
                "energy_kwh": cycle.energy_kwh,
            },
        )
        if program is None:
            persistent_notification.async_create(
                self.hass,
                f"{self.entry.title} draaide een programma dat ik nog niet ken "
                f"({fmt_duration(cycle.duration_s)}, {cycle.energy_kwh:.2f} kWh). "
                "Geef het een naam bij 'Laatste cyclus benoemen' of kies een bestaand programma "
                "bij 'Laatste cyclus toewijzen'.",
                title="Energie Adviseur: onbekend programma",
                notification_id=f"{DOMAIN}_{self.entry.entry_id}_unknown",
            )
        self.refresh_advice()
        self.hass.async_create_task(self.async_replan())
        self.notify()

    @callback
    def _on_other_cycle(self, event: Event) -> None:
        """Een droogbeurt van de gekoppelde droger is klaar."""
        if self.is_washer and event.data.get("entry_id") == self.entry.data.get(CONF_DRYER_ENTRY):
            if self.pending_dry > 0:
                self._sync_rounds()
                self.pending_dry -= 1
                self.pending_dry_programs.pop(0)
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
        program = self._program_for(name)
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

    def _program_for(self, name: str) -> Program:
        """Het programma met deze naam; bestaat het nog niet, dan wordt het aangemaakt."""
        program = self.program_by_name(name)
        if program is None:
            program = Program(uuid.uuid4().hex[:8], name.strip())
            self.programs[program.program_id] = program
        return program

    def rename_program(self, name: str, new_name: str, merge: bool = False) -> None:
        """Geef een programma een andere naam.

        Heeft een ander programma die naam al, dan worden ze met `merge` samengevoegd:
        de cycli gaan naar het bestaande programma en dit programma vervalt.
        """
        program = self.program_by_name(name)
        if program is None:
            raise HomeAssistantError(f"Onbekend programma: {name}")
        new_name = new_name.strip()
        if not new_name:
            raise HomeAssistantError("Geef een nieuwe naam op")
        other = self.program_by_name(new_name)
        if other is not None and other is not program:
            if not merge:
                raise HomeAssistantError(
                    f"Er bestaat al een programma met de naam {other.name}; kies samenvoegen om ze samen te voegen"
                )
            self._merge_program(program, other)
            return
        old = program.name
        program.name = new_name
        self._rename_running(old, new_name)
        self._changed()

    def _merge_program(self, source: Program, target: Program) -> None:
        for cycle in self.cycles:
            if cycle.program_id == source.program_id:
                cycle.program_id = target.program_id
        del self.programs[source.program_id]
        if self.selected == source.program_id:
            self.selected = target.program_id
        self.wash_rounds = [target.program_id if p == source.program_id else p for p in self.wash_rounds]
        for washer in self._washers():
            swap = lambda rounds: [target.program_id if p == source.program_id else p for p in rounds]  # noqa: E731
            touched = washer.dry_program == source.program_id or source.program_id in (
                washer.dry_rounds + washer.pending_dry_programs
            )
            if washer.dry_program == source.program_id:
                washer.dry_program = target.program_id
            washer.dry_rounds, washer.pending_dry_programs = swap(washer.dry_rounds), swap(washer.pending_dry_programs)
            if touched:
                washer._changed()
        self._rename_running(source.name, target.name)
        rebuild(target, self.cycles)
        self._absorb_unlabeled()
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
        for washer in self._washers():
            # rondes met dit programma volgen daarna vanzelf weer het gekozen droogprogramma
            if washer.dry_program == program.program_id:
                washer.dry_program = None
            washer._changed()
        if self.running_name is not None and self.running_name["name"].casefold() == program.name.casefold():
            self.running_name = None
        self._changed()

    def _washers(self) -> list[ApplianceManager]:
        """Wasmachines die dit apparaat als droger gebruiken."""
        managers = (getattr(e, "runtime_data", None) for e in self.hass.config_entries.async_entries(DOMAIN))
        return [m for m in managers if isinstance(m, ApplianceManager) and m.is_washer and m.dryer is self]

    # ------------------------------------------------------------ lopende cyclus

    @property
    def running_program_name(self) -> str | None:
        """De naam die de cyclus die nu loopt alvast heeft gekregen."""
        pending, detector = self.running_name, self.detector
        if pending is None or not detector.running or detector.start is None:
            return None
        return pending["name"] if int(pending["start"]) == int(detector.start) else None

    def label_running(self, name: str | None) -> None:
        """Geef de cyclus die nu loopt alvast een naam; die wordt gekoppeld zodra de cyclus klaar is.

        Een lege naam wist de gekozen naam, dan wordt het programma na afloop weer gewoon herkend.
        """
        if not self.detector.running or self.detector.start is None:
            raise HomeAssistantError(f"{self.entry.title} draait nu niet")
        name = (name or "").strip()
        if name:
            existing = self.program_by_name(name)
            self.running_name = {"name": existing.name if existing else name, "start": self.detector.start}
        else:
            self.running_name = None
        self._changed()

    def _take_running_name(self, cycle: Cycle) -> str | None:
        """De gekozen naam voor deze cyclus, als die bij precies deze beurt hoort; daarna is hij gebruikt."""
        pending, self.running_name = self.running_name, None
        if pending is not None and str(int(pending["start"])) == cycle.cycle_id:
            return pending["name"]
        return None

    def _rename_running(self, old: str, new: str) -> None:
        if self.running_name is not None and self.running_name["name"].casefold() == old.casefold():
            self.running_name = {**self.running_name, "name": new}

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
        self.originals = (self.originals + [cycle.to_dict()])[-MAX_ORIGINALS:]
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
        self._sync_rounds()
        self._changed()

    def _sync_rounds(self) -> None:
        """Houd de lijsten met programma's per ronde even lang als het aantal wasjes en droogbeurten."""
        for name, count in (("wash_rounds", self.loads), ("dry_rounds", self.loads), ("pending_dry_programs", self.pending_dry)):
            rounds = getattr(self, name)
            setattr(self, name, (rounds + [None] * count)[:count])

    def _wash_program(self, program_id: str | None) -> Program | None:
        """Het wasprogramma van een ronde; zonder eigen keuze (of na verwijderen) het gekozen programma."""
        program = self.programs.get(program_id or "")
        return program if program and program.count else self.active_program

    def _dry_program(self, program_id: str | None) -> Program | None:
        dryer = self.dryer
        program = dryer.programs.get(program_id or "") if dryer else None
        return program if program and program.count else self.active_dry_program

    @property
    def round_one_running(self) -> bool:
        """Draait de wasmachine terwijl er nog wasjes op de teller staan? Dan is dat ronde 1."""
        return self.is_washer and self.detector.running and self.loads > 0

    def set_round_program(self, round_no: int, wash: str | None = None, dry: str | None = None) -> None:
        """Kies voor één ronde het was- en/of droogprogramma; een lege naam volgt weer het gekozen programma."""
        self._sync_rounds()
        index = int(round_no) - 1
        if not 0 <= index < self.loads:
            raise HomeAssistantError(f"Ronde {round_no} bestaat niet; er staan {self.loads} wasjes gepland")
        if wash is not None:
            program = self.program_by_name(wash) if wash.strip() else None
            if wash.strip() and program is None:
                raise HomeAssistantError(f"Onbekend programma: {wash}")
            self.wash_rounds[index] = program.program_id if program else None
        if dry is not None:
            dryer = self.dryer
            program = dryer.program_by_name(dry) if dryer and dry.strip() else None
            if dry.strip() and program is None:
                raise HomeAssistantError(f"Onbekend droogprogramma: {dry}")
            self.dry_rounds[index] = program.program_id if program else None
        self._changed()

    def rounds(self) -> list[dict[str, Any]]:
        """De rondes van de wasdag met het programma dat voor elke ronde geldt."""
        self._sync_rounds()
        out = []
        for i in range(self.loads):
            wash, dry = self._wash_program(self.wash_rounds[i]), self._dry_program(self.dry_rounds[i])
            out.append(
                {
                    "ronde": i + 1,
                    "wasprogramma": wash.name if wash else None,
                    "droogprogramma": dry.name if dry else None,
                    # False: de ronde volgt het gekozen programma en verandert daarmee mee
                    "eigen_wasprogramma": wash is not None and wash.program_id == self.wash_rounds[i],
                    "eigen_droogprogramma": dry is not None and dry.program_id == self.dry_rounds[i],
                    "was_draait": i == 0 and self.round_one_running,
                }
            )
        return out

    def pending_dry_names(self) -> list[str | None]:
        self._sync_rounds()
        programs = [self._dry_program(p) for p in self.pending_dry_programs]
        return [p.name if p else None for p in programs]

    def set_dry_program(self, name: str) -> None:
        dryer = self.dryer
        program = dryer.program_by_name(name) if dryer else None
        if program is None:
            raise HomeAssistantError(f"Onbekend droogprogramma: {name}")
        self.dry_program = program.program_id
        self._changed()

    def _changed(self) -> None:
        self._save()
        self.refresh_advice()
        self.hass.async_create_task(self.async_replan())
        self.notify()

    # ------------------------------------------------------------ advies

    def refresh_advice(self) -> None:
        program = self.active_program
        market = get_market(self.hass)
        grid = market.grid()
        if program is None or grid is None:
            self.advice = self.advice_today = self.advice_tomorrow = None
            return
        allowed = market.allowed(grid, *self.window)
        self.advice = best_start(grid, program.profile, allowed)
        self.advice_today = best_start(grid, program.profile, day_mask(grid, *market.day_bounds(0), allowed))
        self.advice_tomorrow = (
            best_start(grid, program.profile, day_mask(grid, *market.day_bounds(1), allowed))
            if market.tomorrow_known(grid)
            else None
        )

    @property
    def tomorrow_saving(self) -> float | None:
        """Hoeveel goedkoper morgen is dan vandaag, elk op het beste moment; negatief als vandaag goedkoper is."""
        if self.advice_today is None or self.advice_tomorrow is None:
            return None
        return round(self.advice_today.cost - self.advice_tomorrow.cost, 4)

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
        # heeft de lopende cyclus al een naam, dan is bekend welk programma draait
        running = self.program_by_name(self.running_program_name)
        if running is not None and running.count:
            program = running
        expected = self.detector.start + (program.duration_s if program else 0)
        return max(expected, time.time())

    async def async_replan(self) -> None:
        """Wasdag: plan de resterende wasjes en droogbeurten."""
        if not self.is_washer:
            return
        async with self._plan_lock:
            self._sync_rounds()
            dryer = self.dryer
            market = get_market(self.hass)
            await market.async_refresh_solar()
            grid = market.grid()
            washes = [self._wash_program(p) for p in self.wash_rounds]
            dries = [self._dry_program(p) for p in self.dry_rounds]
            loose = [self._dry_program(p) for p in self.pending_dry_programs]
            washer_free = self._busy_until(washes[0] if washes else self.active_program)
            ready = None
            offset = 0
            if washer_free is not None and washes:
                # de was die nu draait is ronde 1: alleen zijn droogbeurt moet nog worden gepland
                loose, washes, dries = loose + [dries[0]], washes[1:], dries[1:]
                ready, offset = washer_free + 600, 1
            plan, unplanned = None, 0
            if dryer is not None and grid is not None and (washes or loose) and all(washes + dries + loose):
                allowed = market.allowed(grid, *self.window)
                dryer_free = dryer._busy_until((loose + dries)[0])
                # past niet alles binnen de bekende prijzen, dan vervallen de laatste rondes uit het plan
                for count in range(len(washes), -1, -1):
                    if count + len(loose) == 0:
                        break
                    plan = await self.hass.async_add_executor_job(
                        partial(
                            plan_chain,
                            grid,
                            [p.profile for p in washes[:count]],
                            [p.profile for p in loose + dries[:count]],
                            count,
                            pending_dry=len(loose),
                            washer_free_at=washer_free,
                            dryer_free_at=dryer_free,
                            pending_dry_ready_at=ready,
                            allowed=allowed,
                        )
                    )
                    if plan is not None:
                        unplanned = len(washes) - count
                        break
                unplanned = unplanned if plan is not None else len(washes)
            self.plan, self.plan_offset, self.unplanned = plan, offset, unplanned
            self.plan_names = {
                "wash": [p.name for p in washes if p],
                "dry": [p.name for p in dries if p],
                "loose": [p.name for p in loose if p],
            }
        self.notify()

    def plan_steps(self) -> list[dict[str, Any]]:
        if self.plan is None:
            return []
        out = []
        names, offset = self.plan_names, self.plan_offset
        loose_total = len(names.get("loose", []))
        loose_seen = 0
        for job in self.plan.as_list():
            wash = job["kind"] == "wash"
            what = "Was" if wash else "Drogen"
            round_no: int | None = job["load"] + offset if job["load"] else None
            if job["load"]:
                program = (names.get("wash" if wash else "dry") or [])[job["load"] - 1 : job["load"]]
            else:
                program = names.get("loose", [])[loose_seen : loose_seen + 1]
                loose_seen += 1
                if offset and loose_seen == loose_total:
                    round_no = 1  # de droogbeurt van de was die nu draait
            label = f"{what} {round_no}" if round_no else "Drogen (ligt klaar)"
            out.append(
                {
                    "stap": label,
                    "soort": "was" if wash else "drogen",
                    "ronde": round_no,
                    "programma": program[0] if program else None,
                    "start": dt_util.utc_from_timestamp(job["start"]).isoformat(),
                    "eind": dt_util.utc_from_timestamp(job["end"]).isoformat(),
                    "kosten": job["cost"],
                    "tekst": f"{label}: {fmt_time(job['start'])}–{fmt_time(job['end'])}",
                }
            )
        return out

    def plan_note(self) -> str | None:
        """Waarom (een deel van) de wasdag niet is ingepland; None als alles in het plan staat."""
        if self.loads + self.pending_dry == 0 or (self.plan is not None and not self.unplanned):
            return None
        if self.dryer is None:
            return "Er is geen droger gekoppeld aan deze wasmachine"
        if self.active_program is None or self.active_dry_program is None:
            return "Er is nog geen was- of droogprogramma geleerd"
        grid = get_market(self.hass).grid()
        if grid is None:
            return "Er zijn geen stroomprijzen bekend"
        until = f"De stroomprijzen zijn bekend tot {fmt_time(grid.end)}"
        if self.plan is None:
            return f"{until}; daarvoor past de wasdag niet meer binnen je tijdvenster"
        rest = "1 wasje past" if self.unplanned == 1 else f"{self.unplanned} wasjes passen"
        return f"{until}; {rest} daar niet meer voor en staat nog niet in het plan"

    def plan_text(self) -> str:
        if self.loads + self.pending_dry == 0:
            return "Geen wasjes gepland"
        if self.plan is None:
            return "Geen planning mogelijk"
        steps = self.plan_steps()
        return f"{steps[0]['stap']} starten om {fmt_time(self.plan.as_list()[0]['start'])}" if steps else "Geen planning mogelijk"

    @property
    def plan_saving(self) -> float | None:
        """Wat het plan scheelt met alles zo vroeg mogelijk achter elkaar draaien."""
        if self.plan is None or self.plan.cost_asap is None:
            return None
        return round(self.plan.cost_asap - self.plan.cost, 4)

    def summary(self) -> str | None:
        """Eén zin voor het dagadvies."""
        program, advice = self.active_program, self.advice
        if program is None or advice is None:
            return None
        text = f"{self.entry.title} ({program.name}, {fmt_duration(program.duration_s)}, {program.energy_kwh:.2f} kWh): "
        today, tomorrow = self.advice_today, self.advice_tomorrow
        if tomorrow is None:
            text += f"beste start {fmt_time(advice.start)}, kosten ca. €{advice.cost:.2f}"
            if advice.saving is not None and advice.saving > 0.005:
                text += f", €{advice.saving:.2f} goedkoper dan nu starten"
        else:
            if today is None:
                text += "vandaag past een start niet meer in je tijdvenster"
            else:
                text += f"vandaag het best om {fmt_clock(today.start)}, ca. €{today.cost:.2f}"
            text += f"; morgen het best om {fmt_clock(tomorrow.start)}, ca. €{tomorrow.cost:.2f}"
            saving = self.tomorrow_saving
            if saving is not None and saving > 0.005:
                text += f". Wacht je tot morgen, dan bespaar je ca. €{saving:.2f}"
            elif saving is not None and saving < -0.005:
                text += f". Vandaag is ca. €{-saving:.2f} goedkoper dan morgen"
            elif saving is not None:
                text += ". Vandaag en morgen kosten ongeveer hetzelfde"
        if self.is_washer and self.plan is not None:
            text += ". Wasplanning: " + "; ".join(s["tekst"] for s in self.plan_steps())
            text += f" (totaal ca. €{self.plan.cost:.2f})"
        return text + "."

    # ------------------------------------------------------------ historie

    async def _history_events(self, start: datetime, end: datetime) -> list[tuple[float, float | None, str | None]]:
        """Metingen en statuswijzigingen uit de recorder, op tijd door elkaar: (tijd, vermogen, status)."""
        from homeassistant.components.recorder import get_instance, history

        if "recorder" not in self.hass.config.components:
            raise HomeAssistantError("De recorder van Home Assistant is niet actief; er is geen historie om in te lezen")
        recorder = get_instance(self.hass)

        async def changes(entity_id: str) -> list:
            result = await recorder.async_add_executor_job(
                partial(history.state_changes_during_period, self.hass, start, end, entity_id, no_attributes=True)
            )
            return result.get(entity_id, [])

        current = self.hass.states.get(self.power_entity)
        unit_kw = current is not None and current.attributes.get("unit_of_measurement") == "kW"
        events: list[tuple[float, float | None, str | None]] = []
        for state in await changes(self.power_entity):
            if state.state in UNAVAILABLE:
                continue
            try:
                power = float(state.state) * (1000.0 if unit_kw else 1.0)
            except ValueError:
                continue
            events.append((state.last_updated.timestamp(), power, None))
        if self.state_entity:
            for state in await changes(self.state_entity):
                phase = self._phase(state.state)
                if phase is not None:
                    events.append((state.last_updated.timestamp(), None, phase))
            events.sort(key=lambda e: e[0])
        return events

    async def _detect_from_history(self, start: datetime, end: datetime) -> list[Cycle]:
        """Alle cycli die de detector in de ruwe metingen van de recorder vindt."""
        events = await self._history_events(start, end)
        detector = CycleDetector(self._detector_config())
        found: list[Cycle] = []

        last_ts: float | None = None
        delay = detector.config.stop_delay_s + 1
        for ts, power, phase in events:
            if phase is not None:
                cycle = detector.update_state(ts, phase)
            else:
                if not self.state_entity and last_ts is not None and ts - last_ts > delay:
                    # gat in de metingen: een lopende cyclus eerst laten aflopen
                    closing = detector.update(last_ts + delay, None)
                    if closing is not None:
                        found.append(closing)
                cycle = detector.update(ts, power)
                last_ts = ts
            if cycle is not None:
                found.append(cycle)
        current_start = self.detector.start if self.detector.running else None
        return [c for c in found if current_start is None or c.end < current_start]

    async def async_import_history(self, days: int) -> int:
        """Haal eerdere cycli uit de recorder (zolang die de ruwe metingen bewaart)."""
        end = dt_util.utcnow()
        detected = await self._detect_from_history(end - timedelta(days=days), end)
        known = {c.cycle_id for c in self.cycles} | self.ignored
        # perioden die al bekend of bewust verwijderd zijn; de starttijd kan een paar
        # seconden verschillen met de live gemeten cyclus, dus vergelijken op overlap
        taken = [(c.start, c.end) for c in self.cycles] + [(r[0], r[1]) for r in self.removed]
        found = [
            c
            for c in detected
            if c.cycle_id not in known and not any(_same_run(c.start, c.end, start, end_) for start, end_ in taken)
        ]
        for cycle in found:
            self._handle_cycle(cycle, quiet=True)
        self.cycles.sort(key=lambda c: c.start)
        self._changed()
        return len(found)

    # ------------------------------------------------------------ begin en einde aanpassen

    async def _history_since(self, start: float, end: float) -> list[tuple[float, float | None, str | None]]:
        """Metingen vanaf `start`. De recorder geeft een meting die precies op het begin van de
        gevraagde periode valt niet terug, en een cyclus begint juist op zo'n meting: lees een seconde eerder."""
        return await self._history_events(dt_util.utc_from_timestamp(start - 1), dt_util.utc_from_timestamp(end))

    async def _measure(self, start: float, end: float) -> Cycle:
        """Meet de periode [start, end] opnieuw uit de recorder; pauzes van de statussensor tellen niet mee."""
        events = await self._history_since(start, end)
        samples = [(ts, power) for ts, power, _ in events if power is not None]
        pauses: list[tuple[float, float]] = []
        since: float | None = None
        for ts, _, phase in events:
            if phase == PAUSED and since is None:
                since = ts
            elif phase is not None and phase != PAUSED and since is not None:
                pauses.append((since, ts))
                since = None
        if since is not None:
            pauses.append((since, end))
        try:
            return measure_cycle(samples, start, end, pauses)
        except ValueError as err:
            raise HomeAssistantError(
                "De recorder heeft van deze periode geen metingen meer; de cyclus kan alleen nog worden ingekort"
            ) from err

    def _adjust_limits(self, cycle: Cycle) -> tuple[float, float]:
        """Tussen welke tijden deze cyclus mag liggen: na de vorige cyclus, voor de volgende en voor de beurt die nu loopt."""
        now = time.time()
        low = max((c.end for c in self.cycles if c is not cycle and c.end <= cycle.start + 1), default=0.0)
        high = min((c.start for c in self.cycles if c is not cycle and c.start >= cycle.end - 1), default=now)
        if self.detector.start is not None:
            high = min(high, self.detector.start)
        return low, min(high, now)

    async def async_adjust_cycle(
        self, cycle_id: str | None = None, start_shift: float = 0.0, end_shift: float = 0.0
    ) -> list[dict[str, Any]]:
        """Verschuif het begin en/of het einde van een cyclus, als die niet goed zijn herkend.

        De verschuivingen zijn in minuten: negatief is eerder, positief is later.
        Inkorten gebruikt het bewaarde verloop. Voor verlengen, en voor een cyclus met
        een pauze, wordt de hele nieuwe periode opnieuw uit de recorder gemeten. De
        cyclus houdt zijn programma, en dat programma wordt opnieuw berekend.
        """
        cycle = self.last_cycle if cycle_id is None else next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden om aan te passen")
        start, end = cycle.start + float(start_shift) * 60.0, cycle.end + float(end_shift) * 60.0
        if start == cycle.start and end == cycle.end:
            raise HomeAssistantError("Geef op hoeveel het begin of het einde moet verschuiven")
        if end <= start:
            raise HomeAssistantError("Het einde moet na het begin liggen")
        if end - start > MAX_CYCLE_S:
            raise HomeAssistantError("Een cyclus kan niet langer dan een etmaal duren")
        low, high = self._adjust_limits(cycle)
        if end > time.time() + 1:
            raise HomeAssistantError("Het einde kan niet in de toekomst liggen")
        if start < low - 1 or end > high + 1:
            raise HomeAssistantError("De cyclus zou een andere cyclus of de beurt die nu loopt overlappen")
        shorten = start >= cycle.start and end <= cycle.end
        new: Cycle | None = None
        if not shorten or cycle.paused_s:
            # ook inkorten van een cyclus met een pauze: waar die pauze lag staat alleen in de historie
            try:
                new = await self._measure(start, end)
            except HomeAssistantError:
                if not shorten:
                    raise
            if cycle not in self.cycles:
                raise HomeAssistantError("De cyclus is intussen gewijzigd; probeer het opnieuw")
        if new is not None:
            new.program_id = cycle.program_id
        else:
            try:
                new = trim_cycle(cycle, start, end)
            except ValueError as err:
                raise HomeAssistantError(f"Inkorten lukt niet: {err}") from err
        if abs(new.start - cycle.start) < 1 and abs(new.end - cycle.end) < 1:
            # het bewaarde verloop is grover dan de gevraagde verschuiving (per 5 minuten bij oudere cycli)
            raise HomeAssistantError(
                "De verschuiving is kleiner dan een blok van het bewaarde verloop; kies een grotere stap "
                "of haal de cyclus eerst opnieuw op"
            )
        cfg = self.detector.config
        if new.duration_s < cfg.min_duration_s or new.energy_kwh < cfg.min_energy_kwh:
            raise HomeAssistantError("Na het aanpassen blijft er geen bruikbare cyclus over")
        # 'adjusted' markeert dat deze bewaarde cyclus ook korter mag zijn dan de cyclus die hem vervangt
        self.originals = (self.originals + [cycle.to_dict() | {"adjusted": True}])[-MAX_ORIGINALS:]
        self.cycles.remove(cycle)
        self.ignored.discard(new.cycle_id)
        self.cycles.append(new)
        self.cycles.sort(key=lambda c: c.start)
        program = self.programs.get(new.program_id or "")
        if program is not None:
            rebuild(program, self.cycles)
        self._absorb_unlabeled()
        self._changed()
        return [self._describe(new) | {"programma": self.program_name(new.program_id)}]

    async def async_cycle_context(self, cycle_id: str) -> dict[str, Any]:
        """Het gemeten vermogen rond een cyclus, zodat de pagina kan tonen wat er voor en na gebeurde."""
        cycle = next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden")
        low, high = self._adjust_limits(cycle)
        out: dict[str, Any] = {"cycle_id": cycle.cycle_id, "min_start": low, "max_end": high, "series": None}
        start = cycle.start - CONTEXT_MARGIN_S
        end = min(time.time(), cycle.end + CONTEXT_MARGIN_S)
        try:
            events = await self._history_since(start, end)
            samples = [(ts, power) for ts, power, _ in events if power is not None]
            if samples and start < samples[0][0] < cycle.end:
                start = samples[0][0]  # verder terug heeft de recorder niets: daar begint de grafiek
            measured = measure_cycle(samples, start, end)
        except (HomeAssistantError, ValueError):
            return out  # geen ruwe metingen meer: de pagina toont dan alleen de cyclus zelf
        return out | {"start": start, "end": end, "step_s": DETAIL_S, "series": measured.detail}

    # ------------------------------------------------------------ herstellen

    def original_of(self, cycle: Cycle) -> dict[str, Any] | None:
        """De laatst bewaarde eerdere versie van deze cyclus: van voor een splitsing of van voor het aanpassen.

        Een cyclus van voor een splitsing omvat deze cyclus helemaal. Alleen een cyclus
        van voor het aanpassen van begin of einde mag ook korter of verschoven zijn.
        """
        earlier = [
            o
            for o in self.originals
            if abs(o["start"] - cycle.start) > 1 or abs(o["end"] - cycle.end) > 1
            if (o["start"] - 1 <= cycle.start and cycle.end <= o["end"] + 1)
            or (o.get("adjusted") and _same_run(o["start"], o["end"], cycle.start, cycle.end))
        ]
        return earlier[-1] if earlier else None

    def _replace(self, new: list[Cycle]) -> list[dict[str, Any]]:
        """Zet cycli terug en haal alles weg wat dezelfde tijd beslaat (de delen van een splitsing).

        Vervangt één cyclus precies één andere (opnieuw ophalen zonder splitsing), dan
        houdt die zijn naam. Worden delen samengevoegd, dan vervallen hun namen.
        """
        replaced: list[Cycle] = []
        keep: list[Cycle] = []
        for existing in self.cycles:
            same = any(_same_run(existing.start, existing.end, c.start, c.end) for c in new)
            (replaced if same else keep).append(existing)
        one_to_one = len(new) == 1 and len(replaced) == 1
        for cycle in new:
            cycle.program_id = replaced[0].program_id if one_to_one else None
            self.ignored.discard(cycle.cycle_id)
        self.removed = [r for r in self.removed if not any(_same_run(r[0], r[1], c.start, c.end) for c in new)]
        self.cycles = sorted(keep + new, key=lambda c: c.start)
        for program_id in {c.program_id for c in replaced if c.program_id}:
            if program_id in self.programs:
                rebuild(self.programs[program_id], self.cycles)
        self._absorb_unlabeled()
        self._changed()
        return [self._describe(c) | {"programma": self.program_name(c.program_id)} for c in new]

    async def async_restore_cycle(self, cycle_id: str, from_history: bool = False) -> list[dict[str, Any]]:
        """Haal de oorspronkelijke cyclus terug.

        Is de cyclus een deel van een splitsing of zijn begin of einde aangepast, dan
        komt de cyclus van daarvoor terug. Anders (of met `from_history`) wordt de periode opnieuw uit
        de recorder gelezen, met het verloop per 30 seconden.
        """
        cycle = next((c for c in self.cycles if c.cycle_id == cycle_id), None)
        if cycle is None:
            raise HomeAssistantError("Geen cyclus gevonden om terug te halen")
        original = None if from_history else self.original_of(cycle)
        if original is not None:
            # wat na dit origineel binnen dezelfde periode is bewaard, hoort bij wijzigingen die nu vervallen
            index = self.originals.index(original)
            self.originals = self.originals[:index] + [
                o
                for o in self.originals[index + 1 :]
                if min(o["end"], original["end"]) - max(o["start"], original["start"]) <= 0
            ]
            return self._replace([Cycle.from_dict({k: v for k, v in original.items() if k != "adjusted"})])
        margin = timedelta(hours=8)
        detected = await self._detect_from_history(
            dt_util.utc_from_timestamp(cycle.start) - margin, dt_util.utc_from_timestamp(cycle.end) + margin
        )
        hits = [c for c in detected if min(c.end, cycle.end) - max(c.start, cycle.start) > 0]
        if not hits:
            raise HomeAssistantError(
                "De recorder heeft van deze periode geen metingen meer; de cyclus kan niet opnieuw worden opgehaald"
            )
        # splitsingen binnen de teruggehaalde periode zijn daarmee vervallen
        self.originals = [
            o for o in self.originals if not any(_same_run(o["start"], o["end"], c.start, c.end) for c in hits)
        ]
        return self._replace(hits)
