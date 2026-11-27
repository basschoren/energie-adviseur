"""Metingen van nu (opwek, verbruik) rustig maken voordat ze de planning sturen."""

from __future__ import annotations

from math import exp


class Average:
    """Voortschrijdend gemiddelde over ongeveer `tau_s` seconden; vervalt als er te lang niets binnenkomt."""

    def __init__(self, tau_s: float = 600.0, max_age_s: float = 900.0) -> None:
        self.tau_s = tau_s
        self.max_age_s = max_age_s
        self._value: float | None = None
        self._ts = 0.0

    def update(self, ts: float, value: float | None) -> None:
        if value is None:
            return
        if self._value is None or ts - self._ts > self.max_age_s or ts < self._ts:
            self._value = value  # eerste meting, of de vorige is te oud om mee te middelen
        else:
            alpha = 1.0 - exp(-(ts - self._ts) / self.tau_s)
            self._value += alpha * (value - self._value)
        self._ts = ts

    def value(self, now: float) -> float | None:
        """Het gemiddelde, of None als er geen recente meting is."""
        if self._value is None or now - self._ts > self.max_age_s:
            return None
        return self._value
