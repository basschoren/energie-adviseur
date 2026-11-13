"""Detecteert cycli van een apparaat uit een vermogenssignaal (W)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import SLOT_S


@dataclass
class DetectorConfig:
    start_w: float = 10.0
    stop_w: float = 5.0
    start_delay_s: float = 60.0
    stop_delay_s: float = 300.0
    min_duration_s: float = 600.0
    min_energy_kwh: float = 0.03


@dataclass
class Cycle:
    start: float
    end: float
    energy_kwh: float
    profile: list[float]  # gemiddeld vermogen (W) per blok van SLOT_S
    peak_w: float = 0.0
    program_id: str | None = None
    cycle_id: str = ""

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Cycle:
        return cls(**data)


def _part(cycle: Cycle, first: int, stop: int) -> Cycle | None:
    """Het deel van een cyclus dat de profielblokken [first, stop) beslaat."""
    if stop <= first:
        return None
    start = cycle.start + first * SLOT_S
    end = min(cycle.end, cycle.start + stop * SLOT_S)
    profile = cycle.profile[first:stop]
    wh = 0.0
    for i, watt in enumerate(profile):
        wh += watt * min(SLOT_S, end - (start + i * SLOT_S)) / 3600.0
    peak = cycle.peak_w if max(profile) >= max(cycle.profile) else max(profile)
    return Cycle(
        start=start,
        end=end,
        energy_kwh=round(wh / 1000.0, 4),
        profile=list(profile),
        peak_w=round(peak, 1),
        cycle_id=str(int(start)),
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
    met `at_s` wordt er precies op dat blok geknipt.
    """
    blocks = len(cycle.profile)
    if at_s is None:
        gap = quiet_gap(cycle)
        if gap is None:
            raise ValueError("cyclus is te kort om te splitsen")
        first_stop, second_start = gap[0], gap[0] + gap[1]
    else:
        first_stop = second_start = round(at_s / SLOT_S)
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
    buckets: list[float] = field(default_factory=list)  # Wh per blok

    def update(self, ts: float, power: float | None = None) -> Cycle | None:
        """Verwerk een meting (of een tik zonder nieuwe meting).

        Geeft een Cycle terug op het moment dat een cyclus is afgerond.
        """
        if self.last_ts is not None and ts > self.last_ts and self.start is not None:
            self._integrate(self.last_ts, ts, self.last_power)
        self.last_ts = ts
        if power is not None:
            self.last_power = max(0.0, power)
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
        self.wh = self.wh_at_low = self.peak_w = 0.0
        self.buckets = []

    def _integrate(self, t0: float, t1: float, power: float) -> None:
        assert self.start is not None
        t = t0
        while t < t1:
            idx = int((t - self.start) // SLOT_S)
            edge = self.start + (idx + 1) * SLOT_S
            seg_end = min(t1, edge)
            wh = power * (seg_end - t) / 3600.0
            while len(self.buckets) <= idx:
                self.buckets.append(0.0)
            self.buckets[idx] += wh
            self.wh += wh
            t = seg_end

    def _finish(self) -> Cycle | None:
        assert self.start is not None and self.low_since is not None
        start, end = self.start, self.low_since
        n = max(1, int(-(-(end - start) // SLOT_S)))
        buckets = (self.buckets + [0.0] * n)[:n]
        # laatste blok kan korter zijn dan SLOT_S
        profile = []
        for i, wh in enumerate(buckets):
            length = min(SLOT_S, end - (start + i * SLOT_S))
            profile.append(round(wh * 3600.0 / length, 1) if length > 0 else 0.0)
        cycle = Cycle(
            start=start,
            end=end,
            energy_kwh=round(self.wh_at_low / 1000.0, 4),
            profile=profile,
            peak_w=round(self.peak_w, 1),
            cycle_id=str(int(start)),
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
