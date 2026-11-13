"""Detecteert cycli van een apparaat uit een vermogenssignaal (W)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import DETAIL_S, SLOT_S


@dataclass
class DetectorConfig:
    start_w: float = 10.0
    stop_w: float = 5.0
    start_delay_s: float = 60.0
    stop_delay_s: float = 300.0
    min_duration_s: float = 600.0
    min_energy_kwh: float = 0.03
    # Begin en einde volgen een statussensor in plaats van het vermogen;
    # start_delay_s is dan de tijd die de status minimaal 'running' moet blijven.
    state_mode: bool = False


# Wat een statussensor kan melden, teruggebracht tot wat de detector nodig heeft
RUNNING = "running"
PAUSED = "paused"
OTHER = "other"


@dataclass
class Cycle:
    start: float
    end: float
    energy_kwh: float
    profile: list[float]  # gemiddeld vermogen (W) per blok van SLOT_S
    peak_w: float = 0.0
    program_id: str | None = None
    cycle_id: str = ""
    detail: list[float] = field(default_factory=list)  # W per blok van DETAIL_S; leeg bij cycli van voor 0.5
    paused_s: float = 0.0  # tijd tussen start en eind waarin het apparaat gepauzeerd was; telt niet mee

    @property
    def duration_s(self) -> float:
        """Actieve duur: de pauzes zitten niet in de duur en niet in het verloop."""
        return self.end - self.start - self.paused_s

    @property
    def step_s(self) -> int:
        """Resolutie van het fijnste bewaarde verloop."""
        return DETAIL_S if self.detail else SLOT_S

    @property
    def series(self) -> list[float]:
        """Het fijnste bewaarde verloop: W per blok van step_s."""
        return self.detail or self.profile

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Cycle:
        return cls(**data)


def _to_watts(wh_buckets: list[float], size: float, duration: float) -> list[float]:
    """Wh per blok -> gemiddeld vermogen per blok; het laatste blok kan korter zijn."""
    n = max(1, int(-(-duration // size)))
    out = []
    for i, wh in enumerate((wh_buckets + [0.0] * n)[:n]):
        length = min(size, duration - i * size)
        out.append(round(wh * 3600.0 / length, 1) if length > 0 else 0.0)
    return out


def _regroup(series: list[float], step: float, size: float, duration: float) -> list[float]:
    """Verloop in blokken van `step` omrekenen naar blokken van `size`."""
    wh: list[float] = []
    for i, watt in enumerate(series):
        length = max(0.0, min(step, duration - i * step))
        j = int(i * step // size)
        while len(wh) <= j:
            wh.append(0.0)
        wh[j] += watt * length / 3600.0
    return _to_watts(wh, size, duration)


def _part(cycle: Cycle, first: int, stop: int) -> Cycle | None:
    """Het deel van een cyclus dat de blokken [first, stop) van het fijnste verloop beslaat."""
    if stop <= first:
        return None
    step, series = cycle.step_s, cycle.series
    # het verloop loopt over de actieve tijd; een deel krijgt daarom geen pauze mee
    start = cycle.start + first * step
    duration = min(cycle.duration_s, stop * step) - first * step
    end = start + duration
    piece = list(series[first:stop])
    wh = sum(watt * max(0.0, min(step, duration - i * step)) / 3600.0 for i, watt in enumerate(piece))
    peak = cycle.peak_w if max(piece) >= max(series) else max(piece)
    return Cycle(
        start=start,
        end=end,
        energy_kwh=round(wh / 1000.0, 4),
        profile=_regroup(piece, step, SLOT_S, duration) if cycle.detail else piece,
        peak_w=round(peak, 1),
        cycle_id=str(int(start)),
        detail=piece if cycle.detail else [],
    )


def quiet_gap(cycle: Cycle, edge: int = 2) -> tuple[int, int] | None:
    """De langste rustige periode midden in een cyclus: (eerste blok, aantal blokken).

    Rustig is: hooguit 5 W of 10% van het gemiddelde boven het laagste blok.
    De eerste en laatste `edge` blokken tellen niet mee.
    """
    profile = cycle.profile
    last = len(profile) - edge
    if last - edge < 1:
        return None
    limit = min(profile[edge:last]) + max(5.0, 0.1 * sum(profile) / len(profile))
    best: tuple[int, int] | None = None
    run_start: int | None = None
    for i in range(edge, last + 1):
        quiet = i < last and profile[i] <= limit
        if quiet and run_start is None:
            run_start = i
        elif not quiet and run_start is not None:
            if best is None or i - run_start > best[1]:
                best = (run_start, i - run_start)
            run_start = None
    return best


def split_cycle(cycle: Cycle, at_s: float | None = None) -> tuple[Cycle | None, Cycle | None]:
    """Deel een cyclus in tweeën.

    Zonder `at_s` (seconden na de start) valt de rustigste periode ertussenuit;
    met `at_s` wordt geknipt op het dichtstbijzijnde blok van het fijnste verloop
    (30 seconden, of 5 minuten bij cycli zonder detail).
    """
    step = cycle.step_s
    blocks = len(cycle.series)
    if at_s is None:
        gap = quiet_gap(cycle)
        if gap is None:
            raise ValueError("cyclus is te kort om te splitsen")
        ratio = SLOT_S // step
        first_stop, second_start = gap[0] * ratio, (gap[0] + gap[1]) * ratio
    else:
        first_stop = second_start = round(at_s / step)
        if not 0 < first_stop < blocks:
            raise ValueError("splitspunt ligt buiten de cyclus")
    return _part(cycle, 0, first_stop), _part(cycle, second_start, blocks)


@dataclass
class CycleDetector:
    """Toestandsmachine: rust -> kandidaat -> actief -> rust.

    Het laatst gemelde vermogen geldt tot de volgende meting, zoals bij
    sensoren die alleen bij verandering rapporteren.
    """

    config: DetectorConfig = field(default_factory=DetectorConfig)
    running: bool = False
    start: float | None = None  # begin van (kandidaat-)cyclus
    last_ts: float | None = None
    last_power: float = 0.0
    low_since: float | None = None
    wh: float = 0.0
    wh_at_low: float = 0.0
    peak_w: float = 0.0
    buckets: list[float] = field(default_factory=list)  # Wh per blok van SLOT_S
    fine: list[float] = field(default_factory=list)  # Wh per blok van DETAIL_S
    phase: str = OTHER  # laatst gemelde status (alleen met een statussensor)
    pause_since: float | None = None
    paused_s: float = 0.0

    def update(self, ts: float, power: float | None = None) -> Cycle | None:
        """Verwerk een meting (of een tik zonder nieuwe meting).

        Geeft een Cycle terug op het moment dat een cyclus is afgerond.
        """
        self._advance(ts)
        if power is not None:
            self.last_power = max(0.0, power)
        if self.config.state_mode:
            return self._step_state(ts)
        p = self.last_power
        cfg = self.config

        if not self.running:
            if p >= cfg.start_w:
                if self.start is None:
                    self._reset(start=ts)
                self.peak_w = max(self.peak_w, p)
                if ts - self.start >= cfg.start_delay_s:
                    self.running = True
            elif self.start is not None:
                self._reset(start=None)
            return None

        self.peak_w = max(self.peak_w, p)
        if p >= cfg.stop_w:
            self.low_since = None
            return None
        if self.low_since is None:
            self.low_since = ts
            self.wh_at_low = self.wh
        if ts - self.low_since < cfg.stop_delay_s:
            return None
        return self._finish()

    def update_state(self, ts: float, phase: str) -> Cycle | None:
        """Verwerk een statuswijziging van het apparaat: RUNNING, PAUSED of OTHER."""
        self._advance(ts)
        self.phase = phase
        return self._step_state(ts)

    def _advance(self, ts: float) -> None:
        """Tel het verbruik sinds de vorige aanroep mee; tijdens een pauze telt niets."""
        if self.last_ts is not None and ts > self.last_ts and self.start is not None and self.pause_since is None:
            self._integrate(self.last_ts, ts, self.last_power)
        self.last_ts = ts

    def _step_state(self, ts: float) -> Cycle | None:
        """Statussensor: start na start_delay_s onafgebroken RUNNING, einde zodra de status iets anders wordt.

        PAUSED (deur open) houdt de cyclus vast zonder dat de tijd doortelt.
        """
        if not self.running:
            if self.phase == RUNNING:
                if self.start is None:
                    self._reset(start=ts)
                self.peak_w = max(self.peak_w, self.last_power)
                if ts - self.start >= self.config.start_delay_s:
                    self.running = True
            elif self.start is not None:
                # te kort 'running' geweest, bijvoorbeeld vlak voor een uitgestelde start
                self._reset(start=None)
            return None

        self.peak_w = max(self.peak_w, self.last_power)
        if self.phase == RUNNING:
            if self.pause_since is not None:
                self.paused_s += ts - self.pause_since
                self.pause_since = None
            return None
        if self.phase == PAUSED:
            if self.pause_since is None:
                self.pause_since = ts
            return None
        return self._finish_state(ts)

    def restored(self) -> None:
        """Aanroepen na herstart: het gat in de meting niet meetellen."""
        self.last_ts = None

    @property
    def energy_kwh(self) -> float:
        return self.wh / 1000.0

    def _reset(self, start: float | None) -> None:
        self.start = start
        self.running = False
        self.low_since = None
        self.pause_since = None
        self.paused_s = 0.0
        self.wh = self.wh_at_low = self.peak_w = 0.0
        self.buckets = []
        self.fine = []

    def _integrate(self, t0: float, t1: float, power: float) -> None:
        assert self.start is not None
        self.wh += power * (t1 - t0) / 3600.0
        origin = self.start + self.paused_s  # pauzes schuiven het verloop op
        for buckets, size in ((self.buckets, SLOT_S), (self.fine, DETAIL_S)):
            t = t0
            while t < t1:
                idx = int((t - origin) // size)
                seg_end = min(t1, origin + (idx + 1) * size)
                while len(buckets) <= idx:
                    buckets.append(0.0)
                buckets[idx] += power * (seg_end - t) / 3600.0
                t = seg_end

    def _finish(self) -> Cycle | None:
        assert self.start is not None and self.low_since is not None
        start, end = self.start, self.low_since
        cycle = Cycle(
            start=start,
            end=end,
            energy_kwh=round(self.wh_at_low / 1000.0, 4),
            profile=_to_watts(self.buckets, SLOT_S, end - start),
            peak_w=round(self.peak_w, 1),
            cycle_id=str(int(start)),
            detail=_to_watts(self.fine, DETAIL_S, end - start),
        )
        self._reset(start=None)
        cfg = self.config
        if cycle.duration_s < cfg.min_duration_s or cycle.energy_kwh < cfg.min_energy_kwh:
            return None
        return cycle

    def _finish_state(self, ts: float) -> Cycle | None:
        assert self.start is not None
        start = self.start
        end = self.pause_since if self.pause_since is not None else ts  # gestopt vanuit pauze: einde is het begin van de pauze
        duration = end - start - self.paused_s
        cycle = Cycle(
            start=start,
            end=end,
            energy_kwh=round(self.wh / 1000.0, 4),
            profile=_to_watts(self.buckets, SLOT_S, duration),
            peak_w=round(self.peak_w, 1),
            cycle_id=str(int(start)),
            detail=_to_watts(self.fine, DETAIL_S, duration),
            paused_s=round(self.paused_s, 1),
        )
        self._reset(start=None)
        cfg = self.config
        if cycle.duration_s < cfg.min_duration_s or cycle.energy_kwh < cfg.min_energy_kwh:
            return None
        return cycle

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("config")
        return data

    def load(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            if key != "config" and hasattr(self, key):
                setattr(self, key, value)
        self.restored()
