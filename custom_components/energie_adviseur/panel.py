"""Zijbalkpagina 'Energie Adviseur' en de websocket-opdracht waarmee de pagina cycli ophaalt.

Wijzigen (benoemen, splitsen, historie inlezen) loopt via de gewone diensten;
deze module levert alleen de gegevens en de pagina zelf.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .appliance import ApplianceManager
from .const import DOMAIN
from .core import SLOT_S
from .core.cycle_detector import quiet_gap

PANEL_URL_PATH = "energie-adviseur"
PANEL_ELEMENT = "energie-adviseur-panel"
PANEL_JS_URL = "/energie_adviseur_static/panel.js"
PANEL_JS_FILE = Path(__file__).parent / "frontend" / "panel.js"


async def async_setup_panel(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths([StaticPathConfig(PANEL_JS_URL, str(PANEL_JS_FILE), False)])
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="Energie Adviseur",
        sidebar_icon="mdi:lightning-bolt-circle",
        # het versienummer voorkomt dat de browser na een update de oude pagina blijft gebruiken
        module_url=f"{PANEL_JS_URL}?v={integration.version}",
        require_admin=True,
    )
    websocket_api.async_register_command(hass, ws_cycles)


def appliance_payload(manager: ApplianceManager) -> dict[str, Any]:
    cycles = []
    for cycle in manager.cycles:
        gap = quiet_gap(cycle)
        cycles.append(
            {
                "cycle_id": cycle.cycle_id,
                "start": cycle.start,
                "end": cycle.end,
                "energy_kwh": cycle.energy_kwh,
                "peak_w": cycle.peak_w,
                "program": manager.program_name(cycle.program_id),
                # het fijnste bewaarde verloop: per 30 seconden, of per 5 minuten bij oudere cycli
                "series": cycle.series,
                "step_s": cycle.step_s,
                # rustigste periode in blokken van step_s
                "quiet_gap": [g * (SLOT_S // cycle.step_s) for g in gap] if gap else None,
                "has_original": manager.original_of(cycle) is not None,
            }
        )
    return {
        "entry_id": manager.entry.entry_id,
        "name": manager.entry.title,
        "type": manager.appliance_type,
        "running": manager.detector.running,
        "programs": [
            {"name": p.name, "count": p.count, "duration_s": p.duration_s, "energy_kwh": p.energy_kwh}
            for p in sorted(manager.programs.values(), key=lambda p: p.name.casefold())
        ],
        "cycles": cycles,
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/cycles"})
@websocket_api.require_admin
@callback
def ws_cycles(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Alle apparaten met hun programma's en cycli, inclusief het vermogensprofiel per cyclus."""
    appliances = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        manager = getattr(entry, "runtime_data", None)
        if isinstance(manager, ApplianceManager):
            appliances.append(appliance_payload(manager))
    connection.send_result(msg["id"], {"slot_s": SLOT_S, "appliances": appliances})
