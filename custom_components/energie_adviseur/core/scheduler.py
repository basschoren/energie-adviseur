"""Zoekt de goedkoopste starttijden op een raster van prijs en zonne-overschot."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import SLOT_S
from .price import Interval

STEP = 3  # kandidaat-starttijden per kwartier (3 blokken van 5 minuten)
LIVE_HORIZON_S = 3 * 3600.0  # zo lang werkt een meting van nu door in de verwachting
LIVE_MIN_FORECAST_W = 100.0  # onder deze verwachting wordt het verschil opgeteld in plaats van geschaald
LIVE_MAX_RATIO = 3.0  # de meting groeit verderop hooguit zoveel keer mee met een stijgende verwachting


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

    @property
    def end(self) -> float:
        """Einde van het laatste blok: tot hier zijn de prijzen bekend."""
        return self.time(len(self.price))

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
    measured_solar_w: float | None = None,
    measured_house_w: float | None = None,
    horizon_s: float = LIVE_HORIZON_S,
) -> Grid:
    """Raster vanaf het eerstvolgende blok tot het einde van de bekende prijzen.

    Het overschot per blok is de verwachte opwek min het verbruik van het huis. Met
    `measured_solar_w` (opwek nu) en `measured_house_w` (verbruik van het huis nu, zonder
    de apparaten die apart worden gepland) telt voor de eerstkomende blokken de meting:
    die weegt nu volledig mee en neemt lineair af tot nul na `horizon_s`. Daarna gelden
    weer de verwachting en het ingestelde basisverbruik.
    """
    t0 = -(-now // SLOT_S) * SLOT_S
    forecast_now = (_lookup(solar_w, now) if solar_w else None) or 0.0
    price: list[float] = []
    surplus: list[float] = []
    ts = t0
    while True:
        p = _lookup(prices, ts)
        if p is None:
            break
        price.append(p)
        solar = (_lookup(solar_w, ts) if solar_w else None) or 0.0
        house = base_load_w
        weight = max(0.0, 1.0 - (ts - now) / horizon_s) if horizon_s > 0 else 0.0
        if weight and measured_solar_w is not None:
            if forecast_now >= LIVE_MIN_FORECAST_W:
                # nu geldt de meting; verderop volgt die de vorm van de verwachting (stijgend of dalend)
                shape = min(solar / forecast_now, LIVE_MAX_RATIO)
                solar += weight * (max(measured_solar_w, 0.0) * shape - solar)
            else:
                solar = max(0.0, solar + weight * (measured_solar_w - forecast_now))
        if weight and measured_house_w is not None:
            house = max(0.0, house + weight * (measured_house_w - house))
        surplus.append(max(0.0, solar - house))
        ts += SLOT_S
    return Grid(t0, price, surplus, export_deduction)


def load_on_grid(grid: Grid, start: float, profile: list[float]) -> list[float]:
    """Verbruik (W) per blok van een beurt die op `start` (epoch) begint of al begonnen is."""
    out = [0.0] * len(grid)
    for idx in range(len(grid)):
        k = int((grid.time(idx) - start) // SLOT_S)
        if 0 <= k < len(profile):
            out[idx] = profile[k]
        elif k >= len(profile):
            break
    return out


def add_loads(size: int, *loads: list[float] | None) -> list[float]:
    """Tel verbruik per blok op tot een lijst van `size` blokken."""
    out = [0.0] * size
    for load in loads:
        for idx, w in enumerate((load or [])[:size]):
            out[idx] += w
    return out


def day_mask(grid: Grid, start: float, end: float, allowed: list[bool] | None = None) -> list[bool]:
    """Per blok: begint het in [start, end) en mag er (volgens allowed) gestart worden?"""
    return [start <= grid.time(i) < end and (allowed is None or allowed[i]) for i in range(len(grid))]


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


def _added_solar_kwh(grid: Grid, base: list[float], start: int, profile: list[float]) -> float:
    """Hoeveel van het profiel uit zonne-overschot komt dat niet al door `base` wordt gebruikt."""
    total = 0.0
    for i, w in enumerate(profile):
        idx = start + i
        total += min(base[idx] + w, grid.surplus[idx]) - min(base[idx], grid.surplus[idx])
    return total * SLOT_S / 3_600_000.0


@dataclass
class Advice:
    start: float
    end: float
    cost: float
    cost_now: float | None  # kosten bij direct starten; None als dat niet past
    energy_kwh: float
    solar_kwh: float = 0.0  # deel van het verbruik dat uit eigen zonne-overschot komt

    @property
    def saving(self) -> float | None:
        return None if self.cost_now is None else self.cost_now - self.cost


def best_start(
    grid: Grid,
    profile: list[float],
    allowed: list[bool] | None = None,
    deadline: float | None = None,
    base: list[float] | None = None,
) -> Advice | None:
    """Goedkoopste start voor één apparaat. allowed[i]: mag in blok i starten.

    `base` is het verbruik (W) per blok van andere apparaten die al draaien of gepland
    staan: zonne-overschot dat zij gebruiken, is er voor dit apparaat niet meer.
    """
    n = len(profile)
    last = len(grid) - n
    if deadline is not None:
        last = min(last, int((deadline - grid.t0) // SLOT_S) - n)
    if n == 0 or last < 0:
        return None
    base = add_loads(len(grid), base)
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
        solar_kwh=round(_added_solar_kwh(grid, base, idx, profile), 3),
    )


# ---------------------------------------------------------------- wasketen


@dataclass(eq=False)
class Job:
    kind: str  # "wash" of "dry"
    load: int  # volgnummer van de lading
    profile: list[float]
    earliest: int = 0
    start: int = -1
    cost: float = 0.0  # meerkosten van deze beurt in het plan
    latest: int | None = None  # uiterste start (losse droogbeurt: 3 uur na de was, op dezelfde dag)
    fixed: bool = False  # het venster is niet meer haalbaar: zo vroeg mogelijk en niet meer schuiven

    @property
    def end(self) -> int:
        return self.start + len(self.profile)


@dataclass
class ChainPlan:
    jobs: list[Job] = field(default_factory=list)
    cost: float = 0.0
    t0: float = 0.0
    cost_asap: float | None = None  # kosten als alles zo vroeg mogelijk achter elkaar start

    def as_list(self) -> list[dict]:
        return [
            {
                "kind": j.kind,
                "load": j.load,
                "start": self.t0 + j.start * SLOT_S,
                "end": self.t0 + j.end * SLOT_S,
                "cost": j.cost,
            }
            for j in sorted(self.jobs, key=lambda j: j.start)
        ]


def _per_job(profile: list, count: int) -> list[list[float]]:
    """Eén profiel voor alle beurten, of een lijst met een profiel per beurt."""
    if profile and isinstance(profile[0], (list, tuple)):
        if len(profile) != count:
            raise ValueError("aantal profielen klopt niet met het aantal beurten")
        return [list(p) for p in profile]
    return [profile] * count


def plan_chain(
    grid: Grid,
    wash_profile: list,
    dry_profile: list,
    loads: int,
    pending_dry: int = 0,
    washer_free_at: float | None = None,
    dryer_free_at: float | None = None,
    pending_dry_ready_at: float | None = None,
    allowed: list[bool] | None = None,
    gap_s: float = 600.0,
    max_wait_s: float = 3 * 3600.0,
    pending_dry_washed_at: list[float | None] | None = None,
    day_ends: list[float] | None = None,
    base: list[float] | None = None,
) -> ChainPlan | None:
    """Plan `loads` wasjes met elk een droogbeurt, plus `pending_dry` losse droogbeurten.

    Regels: elke machine één lading tegelijk; drogen begint minstens gap_s na
    het einde van de was en hooguit max_wait_s daarna, en met `day_ends` (de
    middernachten, epoch) ook op dezelfde dag als waarop de was klaar is.

    `pending_dry_washed_at` geeft per losse droogbeurt wanneer zijn was klaar was
    (of klaar zal zijn): dan geldt voor die droogbeurt dezelfde grens. Is dat
    venster al voorbij of niet toegestaan, dan start hij zo vroeg mogelijk.

    `wash_profile` en `dry_profile` zijn één profiel voor alle beurten, of een lijst
    met een profiel per beurt: per wasje, en voor de droger eerst de losse
    droogbeurten en dan per wasje. `pending_dry_ready_at` geldt voor de laatste losse
    droogbeurt: de was die nu nog draait.

    `base` is het verbruik (W) per blok van apparaten buiten dit plan die al draaien of
    gepland staan; zonne-overschot dat zij gebruiken, telt voor dit plan niet mee.
    """
    size = len(grid)
    other = add_loads(size, base)
    gap = int(-(-gap_s // SLOT_S))
    max_wait = int(max_wait_s // SLOT_S)
    w_free = grid.index(washer_free_at) if washer_free_at else 0
    d_free = grid.index(dryer_free_at) if dryer_free_at else 0
    ready = grid.index(pending_dry_ready_at) if pending_dry_ready_at else 0

    wash_profiles = _per_job(wash_profile, loads)
    dry_profiles = _per_job(dry_profile, pending_dry + loads)
    if not (loads or pending_dry) or not all(wash_profiles) or not all(dry_profiles):
        return None
    washes = [Job("wash", i + 1, wash_profiles[i], w_free) for i in range(loads)]
    dries_loose = [
        Job("dry", 0, dry_profiles[i], max(d_free, ready) if i == pending_dry - 1 else d_free) for i in range(pending_dry)
    ]
    dries = [Job("dry", i + 1, dry_profiles[pending_dry + i], d_free) for i in range(loads)]
    dry_order = dries_loose + dries
    jobs = washes + dry_order

    midnights = sorted(day_ends or [])
    # middernachten als blokindex; alleen die binnen het raster doen ertoe
    mid_idx = sorted({int(round((m - grid.t0) / SLOT_S)) for m in midnights if m > grid.t0})
    for job, washed in zip(dries_loose, pending_dry_washed_at or []):
        if washed is None:
            continue
        limit = washed + max_wait_s
        midnight = next((m for m in midnights if m > washed), None)
        if midnight is not None:
            limit = min(limit, midnight - SLOT_S)
        job.latest = int((limit - grid.t0) // SLOT_S)  # negatief: het venster is al voorbij

    def day_hi(end: int) -> int:
        """Laatste blok op dezelfde dag als een was die bij blok `end` klaar is."""
        return next((m - 1 for m in mid_idx if m > end), size)

    def day_lo(start: int) -> int:
        """Begin van de dag waarin blok `start` valt."""
        return max((m for m in mid_idx if m <= start), default=0)

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
            lo = max(lo, partner.start - max_wait - len(job.profile), day_lo(partner.start) - len(job.profile))
            hi = min(hi, partner.start - gap - len(job.profile))
        else:
            if job.fixed:
                return job.start, job.start
            if job.latest is not None:
                hi = min(hi, job.latest)
            i = dry_order.index(job)
            if i > 0:
                lo = max(lo, dry_order[i - 1].end)
            if i + 1 < len(dry_order):
                hi = min(hi, dry_order[i + 1].start - len(job.profile))
            if job.load:
                partner = washes[job.load - 1]
                lo = max(lo, partner.end + gap)
                hi = min(hi, partner.end + max_wait, day_hi(partner.end))
        return lo, hi

    def first_allowed(lo: int, hi: int) -> int | None:
        return next((i for i in range(lo, hi + 1) if ok(i)), None)

    def compact(origin: int) -> bool:
        """Zet alles zo vroeg mogelijk neer vanaf origin."""
        w_next, d_next = max(w_free, origin), d_free
        for job in dries_loose:
            top = size - len(job.profile)
            job.fixed = False
            if job.latest is None:
                s = first_allowed(max(job.earliest, d_next, origin), top)
            else:
                # gebonden aan zijn was: schuift niet mee met het startpunt
                lo = max(job.earliest, d_next)
                s = first_allowed(lo, min(top, job.latest))
                if s is None:
                    s, job.fixed = first_allowed(lo, top), True
            if s is None:
                return False
            job.start, d_next = s, s + len(job.profile)
        for wash, dry in zip(washes, dries):
            s = first_allowed(w_next, size - len(wash.profile))
            while s is not None:
                wash.start = s
                d_lo = max(wash.end + gap, d_next)
                d = first_allowed(d_lo, min(wash.end + max_wait, day_hi(wash.end), size - len(dry.profile)))
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
        base = list(other)
        for job in jobs:
            if job is not skip:
                for i, w in enumerate(job.profile):
                    base[job.start + i] += w
        return base

    def total(assign: bool = False) -> float:
        base = list(other)
        cost = 0.0
        for job in jobs:
            added = _added_cost(grid, base, job.start, job.profile)
            if assign:
                job.cost = round(added, 4)
            cost += added
            for i, w in enumerate(job.profile):
                base[job.start + i] += w
        return cost

    best: tuple[float, list[int]] | None = None
    cost_asap: float | None = None
    for origin in range(0, size, 12):  # elk uur een startpunt
        if not compact(origin):
            break
        if origin == 0:
            cost_asap = round(total(), 4)
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
    total(assign=True)
    return ChainPlan(jobs=jobs, cost=round(best[0], 4), t0=grid.t0, cost_asap=cost_asap)
