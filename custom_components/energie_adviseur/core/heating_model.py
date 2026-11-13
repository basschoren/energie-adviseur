"""Leert de warmtevraag van het huis uit gasverbruik en temperaturen, en vergelijkt gas met airco."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

GAS_KWH_PER_M3 = 9.77  # bovenwaarde Gronings gas
MIN_DAYS = 7


@dataclass
class DayRecord:
    day: str  # JJJJ-MM-DD
    gas_m3: float
    degree_days: float  # som over de uren van max(0, binnen - buiten) / 24
    airco_kwh: float = 0.0
    hours: int = 24

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayRecord:
        return cls(**data)


@dataclass
class HeatModel:
    base_m3: float  # gas per dag dat niet van de buitentemperatuur afhangt (warm water, koken)
    m3_per_degree_day: float
    days: int
    r2: float

    def heat_kwh_per_degree_day(self, boiler_eff: float) -> float:
        return self.m3_per_degree_day * GAS_KWH_PER_M3 * boiler_eff

    def gas_m3(self, degree_days: float) -> float:
        return self.base_m3 + self.m3_per_degree_day * degree_days


def fit(records: list[DayRecord], max_airco_kwh: float = 0.5) -> HeatModel | None:
    """Lineaire regressie gas = basis + helling x graaddagen, op dagen zonder airco."""
    pts = [
        (r.degree_days, r.gas_m3)
        for r in records
        if r.hours >= 20 and r.airco_kwh <= max_airco_kwh and r.gas_m3 >= 0
    ]
    n = len(pts)
    if n < MIN_DAYS:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx < 1e-6:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    base = my - slope * mx
    if slope <= 0:
        return None
    base = max(0.0, base)
    ss_tot = sum((y - my) ** 2 for _, y in pts)
    ss_res = sum((y - (base + slope * x)) ** 2 for x, y in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return HeatModel(round(base, 3), round(slope, 4), n, round(r2, 3))


def implied_cop(records: list[DayRecord], model: HeatModel, boiler_eff: float, min_airco_kwh: float = 1.0) -> float | None:
    """Indicatieve COP: warmte die het gas volgens het model had moeten leveren maar niet leverde,
    gedeeld door het stroomverbruik van de airco's. Gevoelig voor zon, wind en stookgedrag."""
    saved_heat = airco = 0.0
    for r in records:
        if r.hours < 20 or r.airco_kwh < min_airco_kwh:
            continue
        saved_heat += (model.gas_m3(r.degree_days) - r.gas_m3) * GAS_KWH_PER_M3 * boiler_eff
        airco += r.airco_kwh
    if airco < 5.0 or saved_heat <= 0:
        return None
    return round(saved_heat / airco, 2)


def cop_at(t_out: float, cop_7: float, cop_minus7: float) -> float:
    """Lineair tussen de COP bij -7 en +7 graden buiten, begrensd op 1..(cop_7 + 1)."""
    cop = cop_minus7 + (cop_7 - cop_minus7) * (t_out + 7.0) / 14.0
    return max(1.0, min(cop, cop_7 + 1.0))


def gas_heat_cost(gas_price_m3: float, boiler_eff: float) -> float:
    """EUR per kWh warmte uit gas."""
    return gas_price_m3 / (GAS_KWH_PER_M3 * boiler_eff)


def airco_heat_cost(elec_price_kwh: float, cop: float) -> float:
    """EUR per kWh warmte uit de airco."""
    return elec_price_kwh / cop


@dataclass
class HourAdvice:
    start: float
    gas_cost: float
    airco_cost: float
    heat_kwh: float | None  # verwachte warmtevraag in dit uur

    @property
    def airco_cheaper(self) -> bool:
        return self.airco_cost < self.gas_cost

    @property
    def saving(self) -> float | None:
        if self.heat_kwh is None:
            return None
        return max(0.0, self.gas_cost - self.airco_cost) * self.heat_kwh


def hourly_advice(
    hours: list[tuple[float, float, float]],  # (start, stroomprijs EUR/kWh, buitentemperatuur)
    gas_price_m3: float,
    boiler_eff: float,
    cop_7: float,
    cop_minus7: float,
    t_in: float,
    model: HeatModel | None,
) -> list[HourAdvice]:
    gas = gas_heat_cost(gas_price_m3, boiler_eff)
    out = []
    for start, price, t_out in hours:
        heat = None
        if model is not None:
            heat = model.heat_kwh_per_degree_day(boiler_eff) * max(0.0, t_in - t_out) / 24.0
        out.append(HourAdvice(start, round(gas, 4), round(airco_heat_cost(price, cop_at(t_out, cop_7, cop_minus7)), 4), heat))
    return out
