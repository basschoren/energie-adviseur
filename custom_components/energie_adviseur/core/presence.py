"""Afwezigheid: perioden waarin niemand thuis is om een apparaat aan te zetten."""

from __future__ import annotations

Period = tuple[float, float]  # [begin, einde) in epoch


def merge(periods: list[Period]) -> list[Period]:
    """Sorteer de perioden en voeg overlappende of aansluitende samen."""
    out: list[Period] = []
    for start, end in sorted(p for p in periods if p[1] > p[0]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def is_away(periods: list[Period], ts: float) -> bool:
    return any(start <= ts < end for start, end in periods)
