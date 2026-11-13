"""Zoekt de goedkoopste starttijden op een raster van prijs en zonne-overschot."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import SLOT_S
from .price import Interval

STEP = 3  # kandidaat-starttijden per kwartier (3 blokken van 5 minuten)


@dataclass
class Grid:
    t0: float  # begin van blok 0 (epoch)
    price: list[float]  # EUR/kWh per blok
    surplus: list[float]  # zonne-overschot (W) per blok
    export_deduction: float = 0.0  # EUR/kWh minder opbrengst bij terugleveren dan de afnameprijs

    def __len__(self) -> int:
        return len(self.price)

    def time(self, idx: int) -> float:
        return self.t0 + idx * SLOT_S

    def aligned(self, idx: int) -> bool:
        """Valt blok idx op een heel kwartier?"""
        return int(self.t0 // SLOT_S + idx) % STEP == 0

    def index(self, ts: float) -> int:
        """Eerste blok dat op of na ts begint."""
        return max(0, int(-(-(ts - self.t0) // SLOT_S)))


def _lookup(intervals: list[Interval], ts: float) -> float | None:
    for start, end, value in intervals:
        if start <= ts < end:
            return value
    return None


def build_grid(
    now: float,
    prices: list[Interval],
    solar_w: list[Interval] | None = None,
    base_load_w: float = 0.0,
    export_deduction: float = 0.0,
) -> Grid:
    """Raster vanaf het eerstvolgende blok tot het einde van de bekende prijzen."""
    t0 = -(-now // SLOT_S) * SLOT_S
    price: list[float] = []
    surplus: list[float] = []
    ts = t0
    while True:
        p = _lookup(prices, ts)
        if p is None:
            break
        price.append(p)
        s = _lookup(solar_w, ts) if solar_w else None
        surplus.append(max(0.0, (s or 0.0) - base_load_w))
        ts += SLOT_S
    return Grid(t0, price, surplus, export_deduction)


def _slot_cost(grid: Grid, idx: int, load_w: float) -> float:
    """Kosten van load_w in dit blok: netafname plus gemiste teruglevering."""
    from_solar = min(load_w, grid.surplus[idx])
    from_grid = load_w - from_solar
    kwh = SLOT_S / 3_600_000.0
    return (from_grid * grid.price[idx] + from_solar * (grid.price[idx] - grid.export_deduction)) * kwh


def _added_cost(grid: Grid, base: list[float], start: int, profile: list[float]) -> float:
    total = 0.0
    for i, w in enumerate(profile):
        idx = start + i
        total += _slot_cost(grid, idx, base[idx] + w) - _slot_cost(grid, idx, base[idx])
    return total


@dataclass
class Advice:
    start: float
    end: float
    cost: float
    cost_now: float | None  # kosten bij direct starten; None als dat niet past
    energy_kwh: float

    @property
    def saving(self) -> float | None:
        return None if self.cost_now is None else self.cost_now - self.cost


def best_start(
    grid: Grid,
    profile: list[float],
    allowed: list[bool] | None = None,
    deadline: float | None = None,
) -> Advice | None:
    """Goedkoopste start voor één apparaat. allowed[i]: mag in blok i starten."""
    n = len(profile)
    last = len(grid) - n
    if deadline is not None:
        last = min(last, int((deadline - grid.t0) // SLOT_S) - n)
    if n == 0 or last < 0:
        return None
    base = [0.0] * len(grid)
    cost_now = _added_cost(grid, base, 0, profile)
    best: tuple[float, int] | None = None
    for idx in range(0, last + 1):
        if (idx and not grid.aligned(idx)) or (allowed is not None and not allowed[idx]):
            continue
        cost = _added_cost(grid, base, idx, profile)
        if best is None or cost < best[0] - 1e-9:
            best = (cost, idx)
    if best is None:
        return None
    cost, idx = best
    return Advice(
        start=grid.time(idx),
        end=grid.time(idx + n),
        cost=round(cost, 4),
        cost_now=round(cost_now, 4),
        energy_kwh=round(sum(profile) * SLOT_S / 3_600_000.0, 3),
    )


# ---------------------------------------------------------------- wasketen


@dataclass(eq=False)
class Job:
    kind: str  # "wash" of "dry"
    load: int  # volgnummer van de lading
    profile: list[float]
    earliest: int = 0
    start: int = -1

    @property
    def end(self) -> int:
        return self.start + len(self.profile)


@dataclass
class ChainPlan:
    jobs: list[Job] = field(default_factory=list)
    cost: float = 0.0
    t0: float = 0.0

    def as_list(self) -> list[dict]:
        return [
            {
                "kind": j.kind,
                "load": j.load,
                "start": self.t0 + j.start * SLOT_S,
                "end": self.t0 + j.end * SLOT_S,
            }
            for j in sorted(self.jobs, key=lambda j: j.start)
        ]


def plan_chain(
    grid: Grid,
    wash_profile: list[float],
    dry_profile: list[float],
    loads: int,
    pending_dry: int = 0,
    washer_free_at: float | None = None,
    dryer_free_at: float | None = None,
    pending_dry_ready_at: float | None = None,
    allowed: list[bool] | None = None,
    gap_s: float = 600.0,
    max_wait_s: float = 3 * 3600.0,
) -> ChainPlan | None:
    """Plan `loads` wasjes met elk een droogbeurt, plus `pending_dry` losse droogbeurten.

    Regels: elke machine één lading tegelijk; drogen begint minstens gap_s na
    het einde van de was en hooguit max_wait_s daarna.
    """
    size = len(grid)
    gap = int(-(-gap_s // SLOT_S))
    max_wait = int(max_wait_s // SLOT_S)
    w_free = grid.index(washer_free_at) if washer_free_at else 0
    d_free = grid.index(dryer_free_at) if dryer_free_at else 0
    ready = grid.index(pending_dry_ready_at) if pending_dry_ready_at else 0

    washes = [Job("wash", i + 1, wash_profile, w_free) for i in range(loads)]
    dries_loose = [Job("dry", 0, dry_profile, max(d_free, ready)) for _ in range(pending_dry)]
    dries = [Job("dry", i + 1, dry_profile, d_free) for i in range(loads)]
    dry_order = dries_loose + dries
    jobs = washes + dry_order
    if not jobs or (loads and not wash_profile) or (dry_order and not dry_profile):
        return None

    def ok(idx: int) -> bool:
        return allowed is None or (idx < len(allowed) and allowed[idx])

    def bounds(job: Job) -> tuple[int, int]:
        """Toegestaan bereik voor de start van job, gegeven de andere jobs."""
        lo, hi = job.earliest, size - len(job.profile)
        if job.kind == "wash":
            i = washes.index(job)
            if i > 0:
                lo = max(lo, washes[i - 1].end + gap)
            if i + 1 < len(washes):
                hi = min(hi, washes[i + 1].start - gap - len(job.profile))
            partner = dries[i]
            lo = max(lo, partner.start - max_wait - len(job.profile))
            hi = min(hi, partner.start - gap - len(job.profile))
        else:
            i = dry_order.index(job)
            if i > 0:
                lo = max(lo, dry_order[i - 1].end)
            if i + 1 < len(dry_order):
                hi = min(hi, dry_order[i + 1].start - len(job.profile))
            if job.load:
                partner = washes[job.load - 1]
                lo = max(lo, partner.end + gap)
                hi = min(hi, partner.end + max_wait)
        return lo, hi

    def first_allowed(lo: int, hi: int) -> int | None:
        return next((i for i in range(lo, hi + 1) if ok(i)), None)

    def compact(origin: int) -> bool:
        """Zet alles zo vroeg mogelijk neer vanaf origin."""
        w_next, d_next = max(w_free, origin), max(d_free, origin)
        for job in dries_loose:
            s = first_allowed(max(job.earliest, d_next), size - len(job.profile))
            if s is None:
                return False
            job.start, d_next = s, s + len(job.profile)
        for wash, dry in zip(washes, dries):
            s = first_allowed(w_next, size - len(wash.profile))
            while s is not None:
                wash.start = s
                d_lo = max(wash.end + gap, d_next)
                d = first_allowed(d_lo, min(wash.end + max_wait, size - len(dry.profile)))
                if d is not None:
                    break
                # droger te lang bezet of buiten venster: was later starten
                s = first_allowed(max(s + 1, d_lo - max_wait - len(wash.profile)), size - len(wash.profile))
            if s is None:
                return False
            dry.start = d
            w_next, d_next = wash.end + gap, dry.end
        return True

    def load_without(skip: Job) -> list[float]:
        base = [0.0] * size
        for job in jobs:
            if job is not skip:
                for i, w in enumerate(job.profile):
                    base[job.start + i] += w
        return base

    def total() -> float:
        base = [0.0] * size
        cost = 0.0
        for job in jobs:
            cost += _added_cost(grid, base, job.start, job.profile)
            for i, w in enumerate(job.profile):
                base[job.start + i] += w
        return cost

    best: tuple[float, list[int]] | None = None
    for origin in range(0, size, 12):  # elk uur een startpunt
        if not compact(origin):
            break
        for _ in range(8):
            improved = False
            for job in jobs:
                lo, hi = bounds(job)
                base = load_without(job)
                current = _added_cost(grid, base, job.start, job.profile)
                choice = job.start
                for idx in range(lo, hi + 1):
                    if not ok(idx) or not (grid.aligned(idx) or idx == lo):
                        continue
                    cost = _added_cost(grid, base, idx, job.profile)
                    if cost < current - 1e-6:
                        current, choice = cost, idx
                if choice != job.start:
                    job.start, improved = choice, True
            if not improved:
                break
        cost = total()
        if best is None or cost < best[0] - 1e-9:
            best = (cost, [j.start for j in jobs])
    if best is None:
        return None
    for job, start in zip(jobs, best[1]):
        job.start = start
    return ChainPlan(jobs=jobs, cost=round(best[0], 4), t0=grid.t0)
