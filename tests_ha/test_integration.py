"""Tests tegen een echte Home Assistant-kern (pytest-homeassistant-custom-component).

Draaien vanuit de projectmap: pytest tests_ha
"""

from datetime import timedelta
import time

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

DOMAIN = "energie_adviseur"
PRICE = "sensor.stroomprijs"

HUB = {
    "kind": "hub",
    "price_entity": PRICE,
    "price_multiplier": 1.0,
    "use_energy_dashboard": False,
    "base_load_w": 300,
    "export_deduction": 0.0,
    "advice_time": "07:00:00",
}


def appliance(name, kind, power, **extra):
    return {
        "kind": "appliance",
        "name": name,
        "appliance_type": kind,
        "power_entity": power,
        "start_w": 10,
        "stop_w": 5,
        "stop_delay_min": 5,
        "tolerance_pct": 15,
        "window_start": "00:00:00",
        "window_end": "23:59:59",
        **extra,
    }


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def set_prices(hass: HomeAssistant) -> None:
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    values = [0.30, 0.28, 0.25, 0.10, 0.08, 0.09, 0.20, 0.30] * 4
    raw = [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]
    hass.states.async_set(PRICE, "0.30", {"unit_of_measurement": "EUR/kWh", "raw_today": raw})


async def add(hass: HomeAssistant, data: dict, title: str, unique: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, title=title, unique_id=unique)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    registry = er.async_get(hass)
    found = [e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.unique_id == f"{entry.entry_id}_{key}"]
    assert found, f"geen entiteit voor {key}"
    return found[0]


def run_cycle(manager, segments, end_gap=True):
    """Voer een nagebootste cyclus in het verleden door de detector."""
    total = sum(m for m, _ in segments) * 60 + 900
    ts = time.time() - total - 3600
    for minutes, watt in segments + ([(10, 0)] if end_gap else []):
        for _ in range(minutes * 2):
            manager._feed(ts, watt)
            ts += 30


WASH = [(15, 2000), (40, 150), (30, 300), (10, 500)]
DRY = [(100, 900), (10, 200)]


async def test_config_flow_hub_then_menu(hass: HomeAssistant) -> None:
    set_prices(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"
    user_input = {k: v for k, v in HUB.items() if k != "kind"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["kind"] == "hub"
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "appliance"})
    assert result["type"] is FlowResultType.FORM
    hass.states.async_set("sensor.vaatwasser_power", "0", {"unit_of_measurement": "W"})
    data = {k: v for k, v in appliance("Vaatwasser", "dishwasher", "sensor.vaatwasser_power").items() if k != "kind"}
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], data | {"stop_w": 50})
    assert bad["errors"] == {"stop_w": "stop_above_start"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Vaatwasser"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "appliance"
    changed = {k: v for k, v in data.items() if k not in ("name", "appliance_type")} | {"start_w": 20}
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["start_w"] == 20
    assert entry.data["name"] == "Vaatwasser"


async def test_appliance_learns_and_advises(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.droger_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    hub = await add(hass, HUB, "Energie Adviseur", "hub")
    dryer = await add(hass, appliance("Droger", "dryer", "sensor.droger_power"), "Droger", "sensor.droger_power")
    washer = await add(
        hass,
        appliance("Wasmachine", "washer", "sensor.was_power", dryer_entry=dryer.entry_id),
        "Wasmachine",
        "sensor.was_power",
    )
    wm, dm = washer.runtime_data, dryer.runtime_data

    # onbekende cyclus -> benoemen via het tekstveld
    run_cycle(wm, WASH)
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, washer, "last_program")).state == "Onbekend"
    await hass.services.async_call(
        "text", "set_value", {"entity_id": entity(hass, washer, "name_cycle"), "value": "Katoen 40"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, washer, "last_program")).state == "Katoen 40"
    assert hass.states.get(entity(hass, washer, "program")).attributes["options"] == ["Katoen 40"]

    # zelfde programma opnieuw -> herkend
    run_cycle(wm, [(16, 2000), (41, 150), (30, 300), (10, 480)])
    await hass.async_block_till_done()
    assert wm.program_by_name("katoen 40").count == 2
    assert wm.last_cycle.program_id is not None

    # advies
    best = hass.states.get(entity(hass, washer, "best_start"))
    assert best.state not in ("unknown", "unavailable")
    assert best.attributes["programma"] == "Katoen 40"
    assert float(hass.states.get(entity(hass, washer, "expected_cost")).state) > 0
    assert float(hass.states.get(entity(hass, washer, "saving")).state) > 0
    assert hass.states.get(entity(hass, washer, "good_moment")).state == "off"

    # droger leren via de dienst, daarna wasdag plannen
    run_cycle(dm, DRY)
    await hass.services.async_call(
        DOMAIN, "label_cycle", {"appliance": dryer.entry_id, "program": "Kastdroog"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity(hass, washer, "loads"), "value": 2}, blocking=True
    )
    await hass.async_block_till_done()
    await wm.async_replan()
    await hass.async_block_till_done()
    plan = hass.states.get(entity(hass, washer, "laundry_plan"))
    assert plan.state.startswith("Was 1 starten om")
    assert len(plan.attributes["planning"]) == 4
    assert plan.attributes["kosten"] > 0
    assert hass.states.get(entity(hass, washer, "dry_program")).state == "Kastdroog"

    # een was klaar -> teller omlaag, droogbeurt staat klaar; droger klaar -> weg
    run_cycle(wm, WASH)
    await hass.async_block_till_done()
    assert (wm.loads, wm.pending_dry) == (1, 1)
    run_cycle(dm, DRY)
    await hass.async_block_till_done()
    assert wm.pending_dry == 0

    # dagadvies zonder AI
    response = await hass.services.async_call(DOMAIN, "generate_advice", {}, blocking=True, return_response=True)
    assert "Wasmachine" in response["advice"] and response["source"] == "regels"
    assert "Stroomprijs" in hass.states.get(entity(hass, hub, "daily_advice")).attributes["advies"]
    assert float(hass.states.get(entity(hass, hub, "current_price")).state) == pytest.approx(0.30)

    # herladen behoudt het geleerde
    assert await hass.config_entries.async_reload(washer.entry_id)
    await hass.async_block_till_done()
    assert washer.runtime_data.program_by_name("Katoen 40").count == 3

    # hernoemen en verwijderen
    await hass.services.async_call(
        DOMAIN, "rename_program", {"appliance": washer.entry_id, "program": "Katoen 40", "new_name": "Katoen"}, blocking=True
    )
    assert washer.runtime_data.program_names() == ["Katoen"]
    await hass.services.async_call(DOMAIN, "delete_program", {"appliance": washer.entry_id, "program": "Katoen"}, blocking=True)
    assert washer.runtime_data.program_names() == []
    assert len(washer.runtime_data.unlabeled()) == 3


async def test_power_sensor_drives_detector(hass: HomeAssistant, freezer) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    status = entity(hass, entry, "status")
    assert hass.states.get(status).state == "idle"
    hass.states.async_set("sensor.vw_power", "1800", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    for _ in range(30):
        freezer.tick(60)
        manager._on_tick(dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "running"
    hass.states.async_set("sensor.vw_power", "1", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    for _ in range(7):
        freezer.tick(60)
        manager._on_tick(dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "idle"
    assert manager.last_cycle is not None
    assert manager.last_cycle.energy_kwh == pytest.approx(0.9, abs=0.1)


async def test_heating(hass: HomeAssistant, freezer) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.gas", "1000.0", {"unit_of_measurement": "m³"})
    hass.states.async_set("sensor.woonkamer", "20.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.buiten", "5.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.airco_energy", "50.0", {"unit_of_measurement": "kWh"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(
        hass,
        {
            "kind": "heating",
            "gas_entity": "sensor.gas",
            "indoor_entities": ["sensor.woonkamer"],
            "outdoor_entity": "sensor.buiten",
            "airco_entities": ["sensor.airco_energy"],
            "gas_price": 1.30,
            "boiler_eff_pct": 95,
            "cop_7": 4.0,
            "cop_minus7": 2.5,
        },
        "Verwarming",
        "heating",
    )
    manager = entry.runtime_data
    manager._on_sample(dt_util.utcnow())
    freezer.tick(300)
    hass.states.async_set("sensor.gas", "1000.2", {"unit_of_measurement": "m³"})
    hass.states.async_set("sensor.airco_energy", "50.1", {"unit_of_measurement": "kWh"})
    manager._on_sample(dt_util.utcnow())
    await hass.async_block_till_done()
    assert float(hass.states.get(entity(hass, entry, "gas_today")).state) == pytest.approx(0.2)
    assert manager.today["airco_kwh"] == pytest.approx(0.1)
    assert manager.today["degree_hours"] == pytest.approx(15 * 300 / 3600, rel=0.05)
    assert float(hass.states.get(entity(hass, entry, "gas_heat_cost")).state) == pytest.approx(1.30 / (9.77 * 0.95), abs=0.001)
    # 0,30 / COP bij 5 graden (3,79) = 0,079 < 0,140
    assert hass.states.get(entity(hass, entry, "airco_cheaper")).state == "on"
    advice = hass.states.get(entity(hass, entry, "heating_advice"))
    assert advice.state == "airco"
    assert advice.attributes["airco_voordeliger"]
    assert "Verwarming" in advice.attributes["tekst"]


HEATING = {
    "gas_entity": "sensor.gas",
    "indoor_entities": ["sensor.woonkamer"],
    "outdoor_entity": "sensor.buiten",
    "airco_entities": ["sensor.airco_energy"],
    "gas_price": 1.30,
    "boiler_eff_pct": 95,
    "cop_7": 4.0,
    "cop_minus7": 2.5,
}


async def test_heating_flow_and_options(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.gas", "1000.0", {"unit_of_measurement": "m³"})
    hass.states.async_set("sensor.woonkamer", "20.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.buiten", "5.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.airco_energy", "50.0", {"unit_of_measurement": "kWh"})
    hub = await add(hass, HUB, "Energie Adviseur", "hub")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heating"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "heating"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], HEATING)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["kind"] == "heating"
    await hass.async_block_till_done()
    heating = result["result"]

    # een tweede verwarming kan niet
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "heating"})
    assert result["type"] is FlowResultType.ABORT

    # verwarming wijzigen
    result = await hass.config_entries.options.async_init(heating.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "heating"
    result = await hass.config_entries.options.async_configure(result["flow_id"], HEATING | {"gas_price": 1.45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert heating.data["gas_price"] == 1.45
    assert heating.data["kind"] == "heating"

    # algemene instellingen wijzigen
    result = await hass.config_entries.options.async_init(hub.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"
    changed = {k: v for k, v in HUB.items() if k != "kind"} | {"base_load_w": 450}
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hub.data["base_load_w"] == 450
    assert hub.data["kind"] == "hub"


async def test_washer_flow_links_dryer(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.droger_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    dryer = await add(hass, appliance("Droger", "dryer", "sensor.droger_power"), "Droger", "sensor.droger_power")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "appliance"})
    assert result["type"] is FlowResultType.FORM
    assert "dryer_entry" in {str(key) for key in result["data_schema"].schema}
    data = {k: v for k, v in appliance("Wasmachine", "washer", "sensor.was_power").items() if k != "kind"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data | {"dryer_entry": dryer.entry_id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    washer = result["result"]
    assert washer.runtime_data.dryer is dryer.runtime_data

    # dezelfde vermogenssensor kan niet twee keer
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "appliance"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.ABORT

    # wasmachine wijzigen: de droger blijft kiesbaar
    result = await hass.config_entries.options.async_init(washer.entry_id)
    assert result["step_id"] == "appliance"
    assert "dryer_entry" in {str(key) for key in result["data_schema"].schema}
    changed = {k: v for k, v in data.items() if k not in ("name", "appliance_type")}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], changed | {"dryer_entry": dryer.entry_id, "tolerance_pct": 20}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert washer.data["tolerance_pct"] == 20
    assert washer.data["dryer_entry"] == dryer.entry_id


async def test_quarter_hour_prices_known_until(hass: HomeAssistant) -> None:
    """Kwartierprijzen in het formaat van Frank Energie: 'bekend tot' is het einde van het laatste blok."""
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    blocks = [
        {
            "from": (start + timedelta(minutes=15 * i)).isoformat(),
            "till": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "price": 0.30 + i / 1000,
        }
        for i in range(8)
    ]
    hass.states.async_set(PRICE, "0.30", {"unit_of_measurement": "€/kWh", "prices": blocks})
    hub = await add(hass, HUB, "Energie Adviseur", "hub")
    assert hass.states.get(entity(hass, hub, "current_price")).attributes["aantal_prijsblokken"] == 8
    response = await hass.services.async_call(DOMAIN, "generate_advice", {}, blocking=True, return_response=True)
    end = dt_util.as_local(start + timedelta(hours=2))
    expected = ("" if end.date() == dt_util.now().date() else "morgen ") + end.strftime("%H:%M")
    assert f"prijzen bekend tot {expected}." in response["advice"]


async def test_split_cycle(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    washer = await add(hass, appliance("Wasmachine", "washer", "sensor.was_power"), "Wasmachine", "sensor.was_power")
    wm = washer.runtime_data

    # een gewone was leren, daarna twee wassen die door stand-by van 8 W als één cyclus tellen
    run_cycle(wm, WASH)
    wm.label_cycle("Katoen 40")
    wm.cycles[0].start -= 86400
    wm.cycles[0].end -= 86400
    wm.cycles[0].cycle_id = str(int(wm.cycles[0].start))
    run_cycle(wm, WASH + [(20, 8)] + WASH)
    await hass.async_block_till_done()
    assert len(wm.cycles) == 2
    merged = wm.last_cycle
    assert merged.program_id is None
    described = hass.states.get(entity(hass, washer, "last_program")).attributes["onbekende_cycli"]
    assert described[0]["rustigste_periode"].startswith("na 1u35, 20 min lang")

    response = await hass.services.async_call(
        DOMAIN, "split_cycle", {"appliance": "Wasmachine", "cycle_id": merged.cycle_id}, blocking=True, return_response=True
    )
    assert [part["programma"] for part in response["cycles"]] == ["Katoen 40", "Katoen 40"]
    assert len(wm.cycles) == 3
    assert wm.program_by_name("Katoen 40").count == 3
    assert wm.unlabeled() == []

    # alleen het eerste deel bewaren (nadraaien weggooien); opnieuw inlezen haalt het origineel niet terug
    run_cycle(wm, WASH + [(60, 8)])
    await hass.async_block_till_done()
    tail = wm.last_cycle
    response = await hass.services.async_call(
        DOMAIN, "split_cycle", {"appliance": washer.entry_id, "minutes": 95, "keep": "first"}, blocking=True, return_response=True
    )
    assert len(response["cycles"]) == 1
    kept = next(c for c in wm.cycles if c.cycle_id == tail.cycle_id)
    assert kept.duration_s == 95 * 60
    assert kept.program_id == wm.program_by_name("Katoen 40").program_id
    assert len(wm.cycles) == 4

    # een onmogelijk splitspunt geeft een nette fout
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "split_cycle", {"appliance": "Wasmachine", "minutes": 900}, blocking=True)


async def test_panel_and_cycles_websocket(hass: HomeAssistant, hass_client, hass_ws_client, hass_admin_user) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    washer = await add(hass, appliance("Wasmachine", "washer", "sensor.was_power"), "Wasmachine", "sensor.was_power")
    wm = washer.runtime_data
    run_cycle(wm, WASH)
    wm.label_cycle("Katoen 40")

    # de pagina staat in de zijbalk en het script is op te halen
    panel = hass.data["frontend_panels"]["energie-adviseur"]
    assert panel.sidebar_title == "Energie Adviseur"
    assert panel.require_admin
    module_url = panel.config["_panel_custom"]["module_url"]
    assert module_url.startswith("/energie_adviseur_static/panel.js?v=")
    client = await hass_client()
    response = await client.get(module_url)
    assert response.status == 200
    assert "energie-adviseur-panel" in await response.text()

    # herladen van een onderdeel registreert de pagina niet opnieuw
    assert await hass.config_entries.async_reload(washer.entry_id)
    await hass.async_block_till_done()

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "energie_adviseur/cycles"})
    message = await ws.receive_json()
    assert message["success"]
    result = message["result"]
    assert result["slot_s"] == 300
    assert [a["name"] for a in result["appliances"]] == ["Wasmachine"]
    found = result["appliances"][0]
    assert found["entry_id"] == washer.entry_id
    assert found["programs"][0]["name"] == "Katoen 40"
    cycle = found["cycles"][0]
    assert cycle["program"] == "Katoen 40"
    assert len(cycle["profile"]) == 19
    assert cycle["end"] - cycle["start"] == 95 * 60
    assert cycle["quiet_gap"] is None or len(cycle["quiet_gap"]) == 2

    # alleen beheerders mogen de cycli opvragen
    hass_admin_user.groups = []
    await ws.send_json({"id": 2, "type": "energie_adviseur/cycles"})
    message = await ws.receive_json()
    assert not message["success"]
    assert message["error"]["code"] == "unauthorized"


async def test_page_service_calls(hass: HomeAssistant, hass_ws_client) -> None:
    """De exacte call_service-berichten die de zijbalkpagina stuurt."""
    set_prices(hass)
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    washer = await add(hass, appliance("Wasmachine", "washer", "sensor.was_power"), "Wasmachine", "sensor.was_power")
    wm = washer.runtime_data
    run_cycle(wm, WASH + [(20, 8)] + WASH)
    cycle_id = wm.last_cycle.cycle_id
    ws = await hass_ws_client(hass)

    async def call(msg_id: int, service: str, data: dict, respond: bool) -> dict:
        message = {"id": msg_id, "type": "call_service", "domain": DOMAIN, "service": service, "service_data": data}
        if respond:
            message["return_response"] = True
        await ws.send_json(message)
        answer = await ws.receive_json()
        assert answer["success"], answer
        return answer["result"]

    result = await call(
        1, "split_cycle", {"appliance": washer.entry_id, "cycle_id": cycle_id, "keep": "both", "minutes": 105}, True
    )
    assert len(result["response"]["cycles"]) == 2
    await call(2, "label_cycle", {"appliance": washer.entry_id, "program": "Katoen 40", "cycle_id": cycle_id}, False)
    assert wm.program_by_name("Katoen 40").count == 2

    # zonder recorder (zoals in deze test) geeft inlezen een nette foutmelding
    await ws.send_json(
        {
            "id": 3,
            "type": "call_service",
            "domain": DOMAIN,
            "service": "import_history",
            "service_data": {"entry": washer.entry_id, "days": 10},
            "return_response": True,
        }
    )
    answer = await ws.receive_json()
    assert not answer["success"]
    assert "recorder" in answer["error"]["message"]

