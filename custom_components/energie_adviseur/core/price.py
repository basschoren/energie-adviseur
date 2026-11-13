"""Leest prijs- en opwekvoorspellingen uit sensorattributen van uiteenlopende integraties."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any, Iterable

Interval = tuple[float, float, float]  # (start, eind, waarde) met tijden in epoch-seconden

START_KEYS = ("start", "start_time", "startsAt", "starts_at", "from", "datetime", "time", "period_start", "date", "hour")
END_KEYS = ("end", "end_time", "till", "to", "until", "period_end")
PRICE_KEYS = ("value", "price", "total", "electricity_price", "price_per_kwh", "marketPrice", "market_price")
SOLAR_KEYS = ("pv_estimate", "watts", "power", "value")


def to_epoch(value: Any, default_tz: tzinfo = timezone.utc) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 1e9 else None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=default_tz)
        return value.timestamp()
    return None


def _first(item: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def parse_intervals(
    attributes: dict[str, Any],
    value_keys: Iterable[str] = PRICE_KEYS,
    default_tz: tzinfo = timezone.utc,
    scale: float = 1.0,
) -> list[Interval]:
    """Verzamel alle lijsten van {start, [eind], waarde} uit de attributen."""
    found: dict[float, tuple[float | None, float]] = {}
    for attr in attributes.values():
        if not isinstance(attr, (list, tuple)):
            continue
        for item in attr:
            if not isinstance(item, dict):
                continue
            start = to_epoch(_first(item, START_KEYS), default_tz)
            raw = _first(item, value_keys)
            if start is None or isinstance(raw, bool):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            found[start] = (to_epoch(_first(item, END_KEYS), default_tz), value * scale)
    starts = sorted(found)
    steps = [b - a for a, b in zip(starts, starts[1:]) if b > a]
    typical = min(steps) if steps else 3600.0
    out: list[Interval] = []
    for i, start in enumerate(starts):
        end, value = found[start]
        if end is None or end <= start:
            nxt = starts[i + 1] if i + 1 < len(starts) else start + typical
            end = min(nxt, start + max(typical, 900.0) * 4)
        out.append((start, end, value))
    return out


def price_scale(unit: str | None) -> float:
    """Omrekenfactor naar EUR/kWh op basis van de eenheid van de sensor."""
    unit = (unit or "").lower()
    if "mwh" in unit:
        return 0.001
    if "ct" in unit or "cent" in unit or "¢" in unit:
        return 0.01
    return 1.0
