"""Herkent programma's aan duur, energie en profielvorm, en leert ervan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import SLOT_S
from .cycle_detector import Cycle

SHAPE_POINTS = 24
LEARN_FROM_LAST = 20


@dataclass
class Program:
    program_id: str
    name: str
    count: int = 0
    duration_s: float = 0.0
    energy_kwh: float = 0.0
    profile: list[float] = field(default_factory=list)  # W per blok van SLOT_S

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Program:
        return cls(**data)


@dataclass
class Match:
    program: Program | None
    distance: float | None  # 0 = identiek; None als er geen programma's zijn


def resample(profile: list[float], n: int) -> list[float]:
    """Lineair herbemonsteren naar n punten."""
    if not profile or n <= 0:
        return [0.0] * max(n, 0)
    if len(profile) == 1 or n == 1:
        return [sum(profile) / len(profile)] * n
    out = []
    scale = (len(profile) - 1) / (n - 1)
    for i in range(n):
        x = i * scale
        lo = int(x)
        hi = min(lo + 1, len(profile) - 1)
        out.append(profile[lo] + (profile[hi] - profile[lo]) * (x - lo))
    return out


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(a, b, 1e-9)


def _shape_distance(a: list[float], b: list[float]) -> float:
    ra, rb = resample(a, SHAPE_POINTS), resample(b, SHAPE_POINTS)
    sa, sb = sum(ra) or 1.0, sum(rb) or 1.0
    # helft van de L1-afstand tussen genormaliseerde profielen: 0..1
    return 0.5 * sum(abs(x / sa - y / sb) for x, y in zip(ra, rb))


def distance(cycle: Cycle, program: Program) -> float:
    return (
        0.4 * _rel(cycle.duration_s, program.duration_s)
        + 0.4 * _rel(cycle.energy_kwh, program.energy_kwh)
        + 0.2 * _shape_distance(cycle.profile, program.profile)
    )


def match(cycle: Cycle, programs: list[Program], tolerance: float = 0.15) -> Match:
    """Zoek het best passende programma.

    tolerance is de maximale gewogen afwijking; duur en energie mogen elk
    hooguit 2x de tolerantie afwijken.
    """
    best: Program | None = None
    best_d: float | None = None
    for program in programs:
        if program.count == 0:
            continue
        d = distance(cycle, program)
        if best_d is None or d < best_d:
            best, best_d = program, d
    if best is None or best_d is None:
        return Match(None, None)
    ok = (
        best_d <= tolerance
        and _rel(cycle.duration_s, best.duration_s) <= 2 * tolerance
        and _rel(cycle.energy_kwh, best.energy_kwh) <= 2 * tolerance
    )
    return Match(best if ok else None, round(best_d, 4))


def rebuild(program: Program, cycles: list[Cycle]) -> None:
    """Bereken het programma opnieuw uit de cycli die eraan gekoppeld zijn."""
    own = [c for c in cycles if c.program_id == program.program_id]
    own = sorted(own, key=lambda c: c.start)[-LEARN_FROM_LAST:]
    program.count = len(own)
    if not own:
        return
    n = len(own)
    program.duration_s = sum(c.duration_s for c in own) / n
    program.energy_kwh = round(sum(c.energy_kwh for c in own) / n, 4)
    length = max(1, round(program.duration_s / SLOT_S))
    acc = [0.0] * length
    for c in own:
        for i, w in enumerate(resample(c.profile, length)):
            acc[i] += w
    profile = [w / n for w in acc]
    # schaal zodat het profiel de gemiddelde energie bevat
    wh = sum(profile) * SLOT_S / 3600.0
    factor = (program.energy_kwh * 1000.0 / wh) if wh > 0 else 1.0
    program.profile = [round(w * factor, 1) for w in profile]
