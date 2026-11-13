"""Algemeen onderdeel: verzamelt de adviezen tot een dagadvies, optioneel verwoord door een lokale AI."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .appliance import ApplianceManager, fmt_time
from .const import CONF_ADVICE_TIME, CONF_AI_ENTITY, DOMAIN, signal
from .heating import HeatingManager
from .market import get_market, parse_time

_LOGGER = logging.getLogger(__name__)
AI_TIMEOUT_S = 900

PROMPT = (
    "Je bent een energie-assistent voor een huishouden in Nederland. Schrijf een kort dagadvies in het Nederlands "
    "van hooguit 120 woorden, in gewone zinnen zonder opsomming. Gebruik uitsluitend de feiten hieronder en "
    "verzin geen tijden of bedragen. Noem per apparaat het beste startmoment.\n\nFeiten:\n{facts}"
)


class HubManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.signal = signal(entry.entry_id)
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Energie Adviseur"
        )
        self.facts: list[str] = []
        self.advice: str | None = None
        self.advice_source = "regels"
        self.generated: datetime | None = None
        self._unsub: list = []
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        at = parse_time(self.entry.data.get(CONF_ADVICE_TIME), datetime.min.time().replace(hour=7))
        self._unsub = [
            async_track_time_change(self.hass, self._on_time, hour=at.hour, minute=at.minute, second=0),
            async_track_time_interval(self.hass, self._on_refresh, timedelta(minutes=15)),
        ]
        self.hass.async_create_task(get_market(self.hass).async_refresh_solar(force=True))

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()

    @callback
    def _on_time(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_generate())

    @callback
    def _on_refresh(self, now: datetime) -> None:
        self.hass.async_create_task(get_market(self.hass).async_refresh_solar())
        async_dispatcher_send(self.hass, self.signal)

    def managers(self) -> list[Any]:
        out = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            manager = getattr(entry, "runtime_data", None)
            if isinstance(manager, (ApplianceManager, HeatingManager)):
                out.append(manager)
        return out

    def collect_facts(self) -> list[str]:
        market = get_market(self.hass)
        facts: list[str] = []
        prices = [(s, v) for s, e, v in market.prices() if e > time.time()]
        if prices:
            low, high = min(prices, key=lambda p: p[1]), max(prices, key=lambda p: p[1])
            facts.append(
                f"Stroomprijs: laagst €{low[1]:.3f}/kWh om {fmt_time(low[0])}, hoogst €{high[1]:.3f}/kWh om "
                f"{fmt_time(high[0])}; prijzen bekend tot {fmt_time(prices[-1][0] + 3600)}."
            )
        else:
            facts.append("Er is geen prijsvoorspelling beschikbaar; controleer de prijssensor.")
        for manager in self.managers():
            text = manager.summary()
            if text:
                facts.append(text)
        return facts

    async def async_generate(self) -> None:
        async with self._lock:
            await get_market(self.hass).async_refresh_solar()
            self.facts = self.collect_facts()
            self.advice = " ".join(self.facts)
            self.advice_source = "regels"
            self.generated = dt_util.utcnow()
            async_dispatcher_send(self.hass, self.signal)
            ai_entity = self.entry.data.get(CONF_AI_ENTITY)
            if not ai_entity:
                return
            try:
                async with asyncio.timeout(AI_TIMEOUT_S):
                    text = await self._ask_ai(ai_entity, PROMPT.format(facts="\n".join(f"- {f}" for f in self.facts)))
            except Exception:  # noqa: BLE001 - AI is een extra; het advies op basis van regels blijft staan
                _LOGGER.warning("Lokale AI gaf geen antwoord; het advies zonder AI blijft staan", exc_info=True)
                return
            if text:
                self.advice = text.strip()
                self.advice_source = "ai"
                self.generated = dt_util.utcnow()
                async_dispatcher_send(self.hass, self.signal)

    async def _ask_ai(self, entity_id: str, prompt: str) -> str | None:
        if entity_id.startswith("ai_task."):
            response = await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {"task_name": "Energieadvies", "instructions": prompt, "entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            data = (response or {}).get("data")
            return data if isinstance(data, str) else None
        response = await self.hass.services.async_call(
            "conversation", "process", {"text": prompt, "agent_id": entity_id}, blocking=True, return_response=True
        )
        return (((response or {}).get("response") or {}).get("speech") or {}).get("plain", {}).get("speech")
