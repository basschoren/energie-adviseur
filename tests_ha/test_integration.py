"""Tests tegen een echte Home Assistant-kern (pytest-homeassistant-custom-component).

Draaien vanuit de projectmap: pytest tests_ha
"""

from datetime import datetime, timedelta
import time

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

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


async def test_laundry_rounds(hass: HomeAssistant) -> None:
    """Wasdag met een eigen was- en droogprogramma per ronde."""
    set_prices(hass)
    hass.states.async_set("sensor.droger_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    dryer = await add(hass, appliance("Droger", "dryer", "sensor.droger_power"), "Droger", "sensor.droger_power")
    washer = await add(
        hass, appliance("Wasmachine", "washer", "sensor.was_power", dryer_entry=dryer.entry_id), "Wasmachine", "sensor.was_power"
    )
    wm, dm = washer.runtime_data, dryer.runtime_data
    for manager, segments, name in (
        (wm, WASH, "Katoen 40"),
        (wm, [(10, 2000), (20, 150)], "Kort"),
        (dm, DRY, "Kastdroog"),
        (dm, [(50, 900)], "Strijkdroog"),
    ):
        run_cycle(manager, segments)
        manager.label_cycle(name)
    wm.select_program("Katoen 40")
    wm.set_dry_program("Kastdroog")

    def plan():
        return hass.states.get(entity(hass, washer, "laundry_plan")).attributes

    def minutes(step):
        return (dt_util.parse_datetime(step["eind"]) - dt_util.parse_datetime(step["start"])).total_seconds() / 60

    # drie wasjes: elke ronde volgt eerst het gekozen programma
    await hass.services.async_call("number", "set_value", {"entity_id": entity(hass, washer, "loads"), "value": 3}, blocking=True)
    await wm.async_replan()
    assert [(r["wasprogramma"], r["droogprogramma"]) for r in plan()["rondes"]] == [("Katoen 40", "Kastdroog")] * 3
    assert not any(r["eigen_wasprogramma"] or r["eigen_droogprogramma"] or r["was_draait"] for r in plan()["rondes"])
    assert plan()["wasprogrammas"] == ["Katoen 40", "Kort"] and plan()["droogprogrammas"] == ["Kastdroog", "Strijkdroog"]

    # ronde 2 krijgt een eigen was- en droogprogramma
    await hass.services.async_call(
        DOMAIN, "set_round_program",
        {"appliance": "Wasmachine", "round": 2, "wash_program": "kort", "dry_program": "Strijkdroog"}, blocking=True,
    )
    await wm.async_replan()
    attrs = plan()
    steps = {s["stap"]: s for s in attrs["planning"]}
    assert sorted(steps) == ["Drogen 1", "Drogen 2", "Drogen 3", "Was 1", "Was 2", "Was 3"]
    assert [steps[k]["programma"] for k in ("Was 1", "Was 2", "Was 3")] == ["Katoen 40", "Kort", "Katoen 40"]
    assert [steps[k]["programma"] for k in ("Drogen 1", "Drogen 2", "Drogen 3")] == ["Kastdroog", "Strijkdroog", "Kastdroog"]
    assert [minutes(steps[k]) for k in ("Was 1", "Was 2", "Drogen 1", "Drogen 2")] == [95, 30, 110, 50]
    assert (steps["Was 2"]["soort"], steps["Drogen 2"]["soort"], steps["Was 2"]["ronde"]) == ("was", "drogen", 2)
    for n in (1, 2, 3):
        assert steps[f"Drogen {n}"]["start"] > steps[f"Was {n}"]["eind"]  # drogen na de was van dezelfde ronde
    assert steps["Was 2"]["start"] < steps["Drogen 1"]["eind"]  # de wasmachine draait door tijdens het drogen
    assert sum(s["kosten"] for s in attrs["planning"]) == pytest.approx(attrs["kosten"], abs=0.001)
    assert attrs["besparing"] == pytest.approx(attrs["kosten_zo_vroeg_mogelijk"] - attrs["kosten"], abs=0.0001)
    assert attrs["besparing"] >= 0 and attrs["niet_ingepland"] == 0 and attrs["toelichting"] is None
    assert attrs["rondes"][1] == {
        "ronde": 2, "wasprogramma": "Kort", "droogprogramma": "Strijkdroog",
        "eigen_wasprogramma": True, "eigen_droogprogramma": True, "was_draait": False,
    }

    # fouten: een ronde die niet bestaat, een onbekend programma, niets opgegeven
    for data in ({"round": 4, "wash_program": "Kort"}, {"round": 1, "wash_program": "Wol"}, {"round": 1, "dry_program": "Wol"}, {"round": 1}):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "set_round_program", {"appliance": "Wasmachine", **data}, blocking=True)

    # ronde 1 is gewassen: de rondes schuiven op en zijn droogprogramma ligt klaar
    await hass.services.async_call(
        DOMAIN, "set_round_program", {"appliance": "Wasmachine", "round": 1, "dry_program": "Strijkdroog"}, blocking=True
    )
    run_cycle(wm, WASH)
    await hass.async_block_till_done()
    await wm.async_replan()
    attrs = plan()
    assert (wm.loads, wm.pending_dry) == (2, 1)
    assert [(r["wasprogramma"], r["droogprogramma"]) for r in attrs["rondes"]] == [("Kort", "Strijkdroog"), ("Katoen 40", "Kastdroog")]
    assert attrs["droogbeurten_die_klaarliggen"] == ["Strijkdroog"]
    loose = next(s for s in attrs["planning"] if s["stap"] == "Drogen (ligt klaar)")
    assert (loose["programma"], minutes(loose), loose["ronde"]) == ("Strijkdroog", 50, None)

    # de was van ronde 1 draait: alleen zijn droogbeurt wordt nog gepland, de volgende was is ronde 2
    wm._feed(time.time() - 120, 2000)
    wm._feed(time.time(), 2000)
    assert wm.round_one_running
    await wm.async_replan()
    attrs = plan()
    assert [s["stap"] for s in sorted(attrs["planning"], key=lambda s: (s["soort"], s["ronde"] or 0))] == [
        "Drogen (ligt klaar)", "Drogen 1", "Drogen 2", "Was 2",
    ]
    by_name = {s["stap"]: s for s in attrs["planning"]}
    assert (by_name["Drogen 1"]["programma"], by_name["Was 2"]["programma"]) == ("Strijkdroog", "Katoen 40")
    assert attrs["rondes"][0]["was_draait"] and not attrs["rondes"][1]["was_draait"]
    wm.detector._reset(start=None)

    # de droger is klaar: de droogbeurt die klaarlag is weg
    run_cycle(dm, [(50, 900)])
    await hass.async_block_till_done()
    assert (wm.pending_dry, wm.pending_dry_programs) == (0, [])

    # minder wasjes: rondes vervallen; meer wasjes: nieuwe rondes volgen het gekozen programma
    wm.set_loads(1)
    assert [r["wasprogramma"] for r in wm.rounds()] == ["Kort"]
    wm.set_loads(3)
    assert [(r["wasprogramma"], r["eigen_wasprogramma"]) for r in wm.rounds()] == [("Kort", True), ("Katoen 40", False), ("Katoen 40", False)]
    wm.select_program("Kort")
    assert [r["wasprogramma"] for r in wm.rounds()] == ["Kort"] * 3
    wm.select_program("Katoen 40")

    # opslaan en herladen behoudt de rondes
    assert await hass.config_entries.async_reload(washer.entry_id)
    await hass.async_block_till_done()
    wm = washer.runtime_data
    assert [(r["wasprogramma"], r["droogprogramma"]) for r in wm.rounds()][0] == ("Kort", "Strijkdroog")

    # een leeg programma volgt weer het gekozen programma
    await hass.services.async_call(DOMAIN, "set_round_program", {"appliance": "Wasmachine", "round": 1, "dry_program": ""}, blocking=True)
    assert (wm.rounds()[0]["droogprogramma"], wm.rounds()[0]["eigen_droogprogramma"]) == ("Kastdroog", False)

    # samenvoegen en verwijderen van programma's werkt door in de rondes
    wm.set_round_program(2, dry="Strijkdroog")
    dm.rename_program("Strijkdroog", "Kastdroog", merge=True)
    assert (wm.rounds()[1]["droogprogramma"], wm.rounds()[1]["eigen_droogprogramma"]) == ("Kastdroog", True)
    wm.delete_program("Kort")
    assert (wm.rounds()[0]["wasprogramma"], wm.rounds()[0]["eigen_wasprogramma"]) == ("Katoen 40", False)

    # prijzen voor maar 5 uur: één ronde past nog, de rest staat niet in het plan en dat wordt gemeld
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)
    raw = [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(), "value": 0.2}
        for i in range(5)
    ]
    hass.states.async_set(PRICE, "0.20", {"unit_of_measurement": "EUR/kWh", "raw_today": raw})
    await wm.async_replan()
    attrs = plan()
    assert [s["stap"] for s in attrs["planning"]] == ["Was 1", "Drogen 1"]
    assert attrs["niet_ingepland"] == 2
    assert attrs["toelichting"].startswith("De stroomprijzen zijn bekend tot ") and "2 wasjes passen" in attrs["toelichting"]
    wm.set_loads(0)
    await wm.async_replan()
    assert plan()["planning"] == [] and plan()["toelichting"] is None


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


async def test_split_on_half_minute_and_undo(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    washer = await add(hass, appliance("Wasmachine", "washer", "sensor.was_power"), "Wasmachine", "sensor.was_power")
    wm = washer.runtime_data
    run_cycle(wm, WASH + [(20, 8)] + WASH)
    await hass.async_block_till_done()
    original = wm.last_cycle
    assert original.step_s == 30

    # verkeerd gesplitst op 60,5 minuut, en daarna het eerste deel nog eens
    response = await hass.services.async_call(
        DOMAIN, "split_cycle", {"appliance": "Wasmachine", "cycle_id": original.cycle_id, "minutes": 60.5},
        blocking=True, return_response=True,
    )
    first, second = wm.cycles
    assert first.duration_s == 60.5 * 60
    assert second.start == original.start + 60.5 * 60
    assert len(response["cycles"]) == 2
    await hass.services.async_call(
        DOMAIN, "split_cycle", {"appliance": "Wasmachine", "cycle_id": first.cycle_id, "minutes": 30}, blocking=True
    )
    assert len(wm.cycles) == 3
    wm.label_cycle("Rest", second.cycle_id)

    # terughalen gaat stap voor stap: eerst de laatste splitsing ongedaan
    response = await hass.services.async_call(
        DOMAIN, "restore_cycle", {"appliance": "Wasmachine", "cycle_id": wm.cycles[1].cycle_id},
        blocking=True, return_response=True,
    )
    assert [c.duration_s for c in wm.cycles] == [60.5 * 60, second.duration_s]
    assert len(response["cycles"]) == 1
    # daarna de eerste; vanaf het tweede deel werkt dat ook, en de naam van dat deel vervalt
    await hass.services.async_call(
        DOMAIN, "restore_cycle", {"appliance": "Wasmachine", "cycle_id": second.cycle_id}, blocking=True
    )
    assert len(wm.cycles) == 1
    restored = wm.cycles[0]
    assert (restored.start, restored.end, restored.energy_kwh) == (original.start, original.end, original.energy_kwh)
    assert restored.detail == original.detail
    assert restored.program_id is None
    assert wm.program_by_name("Rest").count == 0
    assert wm.originals == []

    # nu goed splitsen op de rustige periode
    await hass.services.async_call(
        DOMAIN, "split_cycle", {"appliance": "Wasmachine", "cycle_id": restored.cycle_id, "minutes": 105}, blocking=True
    )
    assert [c.duration_s for c in wm.cycles] == [105 * 60, 105 * 60]

    # de bewaarde cyclus overleeft een herstart
    assert await hass.config_entries.async_reload(washer.entry_id)
    await hass.async_block_till_done()
    wm = washer.runtime_data
    assert len(wm.originals) == 1
    await wm.async_restore_cycle(wm.cycles[1].cycle_id)
    assert len(wm.cycles) == 1 and wm.cycles[0].duration_s == original.duration_s


async def test_undo_drops_stale_originals(hass: HomeAssistant) -> None:
    """Na het terughalen van de hele cyclus mag een bewaard deel van een latere splitsing niet meer meedoen."""
    set_prices(hass)
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    washer = await add(hass, appliance("Wasmachine", "washer", "sensor.was_power"), "Wasmachine", "sensor.was_power")
    wm = washer.runtime_data
    run_cycle(wm, WASH + [(20, 8)] + WASH)
    whole = wm.cycles[0]
    wm.split_cycle(whole.cycle_id, minutes=105)
    second = wm.cycles[1]
    wm.split_cycle(second.cycle_id, minutes=30)
    assert len(wm.cycles) == 3 and len(wm.originals) == 2

    # het eerste deel ongedaan maken zet de hele cyclus terug; de bewaarde tweede helft vervalt
    await wm.async_restore_cycle(wm.cycles[0].cycle_id)
    assert [(c.start, c.end) for c in wm.cycles] == [(whole.start, whole.end)]
    assert wm.originals == []
    assert wm.original_of(wm.cycles[0]) is None

    # een restant in de opslag van een eerdere versie biedt geen 'ongedaan maken' aan op de hele cyclus
    wm.originals = [second.to_dict()]
    assert wm.original_of(wm.cycles[0]) is None

    # inkorten zonder recorder gebruikt het bewaarde verloop; ongedaan maken zet de cyclus terug
    await hass.services.async_call(DOMAIN, "adjust_cycle", {"appliance": "Wasmachine", "end_shift": -20.5}, blocking=True)
    assert wm.cycles[0].end == whole.end - 20.5 * 60
    assert wm.original_of(wm.cycles[0])["adjusted"]
    await wm.async_restore_cycle(wm.cycles[0].cycle_id)
    assert (wm.cycles[0].end, wm.cycles[0].energy_kwh, wm.cycles[0].detail) == (whole.end, whole.energy_kwh, whole.detail)
    # verlengen kan niet zonder historie
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "adjust_cycle", {"appliance": "Wasmachine", "start_shift": -5}, blocking=True)


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
    # de dashboardkaart wordt meegeleverd
    response = await client.get("/energie_adviseur_static/energie-adviseur-card.js")
    assert response.status == 200
    assert "energie-adviseur-card" in await response.text()

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
    assert cycle["step_s"] == 30
    assert len(cycle["series"]) == 190
    assert cycle["has_original"] is False
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

    # een dubbele cyclus verwijderen: het programma leert alleen nog van de overgebleven cyclus
    await call(4, "delete_cycle", {"appliance": washer.entry_id, "cycle_id": cycle_id}, False)
    assert len(wm.cycles) == 1
    assert wm.program_by_name("Katoen 40").count == 1
    assert len(wm.removed) == 1
    with pytest.raises(HomeAssistantError):
        wm.delete_cycle("bestaat-niet")
    assert await hass.config_entries.async_reload(washer.entry_id)
    await hass.async_block_till_done()
    wm = washer.runtime_data
    assert len(wm.cycles) == 1 and len(wm.removed) == 1

    # zonder recorder (zoals in deze test) geeft inlezen een nette foutmelding
    await ws.send_json(
        {
            "id": 5,
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


STATE = "sensor.dishwasher_appliance_state"


async def test_state_sensor_controls_cycle(hass: HomeAssistant, freezer) -> None:
    """Slimme vaatwasser: de statussensor bepaalt begin, einde en pauzes; hoofdletters maken niet uit."""
    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "2", {"unit_of_measurement": "W"})
    hass.states.async_set(STATE, "Idle")
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(
        hass,
        appliance("Vaatwasser", "dishwasher", "sensor.vw_power", state_entity=STATE, min_running_s=60),
        "Vaatwasser",
        "sensor.vw_power",
    )
    manager = entry.runtime_data
    status = entity(hass, entry, "status")

    async def step(state: str, watt: int, minutes: float) -> None:
        hass.states.async_set(STATE, state)
        hass.states.async_set("sensor.vw_power", str(watt), {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
        for _ in range(int(minutes * 2)):
            freezer.tick(30)
            manager._on_tick(dt_util.utcnow())
        await hass.async_block_till_done()

    # idle -> ready to start -> running (kort) -> delayed start: er start niets, ook al is er vermogen
    await step("Ready to start", 30, 1)
    await step("Running", 30, 0.5)
    assert hass.states.get(status).state == "idle"
    await step("Delayed start", 30, 20)
    assert hass.states.get(status).state == "idle"
    assert manager.detector.start is None

    # nu echt: running, deur open (paused), verder, einde
    await step("Running", 1800, 20)
    assert hass.states.get(status).state == "running"
    await step("Paused", 4, 10)
    assert hass.states.get(status).state == "running"
    await step("Running", 900, 30)
    await step("End of cycle", 3, 1)
    assert hass.states.get(status).state == "idle"
    await step("Idle", 2, 1)

    assert len(manager.cycles) == 1
    cycle = manager.last_cycle
    assert cycle.duration_s == pytest.approx(50 * 60, abs=1)
    assert cycle.paused_s == pytest.approx(10 * 60, abs=1)
    assert cycle.energy_kwh == pytest.approx(0.6 + 0.45, abs=0.02)
    assert hass.states.get(entity(hass, entry, "last_program")).attributes["onbekende_cycli"][0]["duur"] == "50 min"

    # de sensor valt even weg tijdens een cyclus: die loopt gewoon door
    await step("Running", 1500, 15)
    hass.states.async_set(STATE, "unavailable")
    await hass.async_block_till_done()
    assert manager.detector.running
    await step("Running", 1500, 5)
    await step("Idle", 2, 1)
    assert len(manager.cycles) == 2
    assert manager.last_cycle.duration_s == pytest.approx(20 * 60, abs=1)

    # tijdens een pauze alvast een naam geven: na afloop krijgt de cyclus die naam
    async def name(program: str) -> None:
        await hass.services.async_call(
            DOMAIN, "label_running_cycle", {"appliance": entry.entry_id, "program": program}, blocking=True
        )

    await step("Running", 1500, 20)
    await step("Paused", 4, 5)
    await name("Snel 45")
    await step("Running", 1500, 10)
    await step("Idle", 2, 1)
    assert len(manager.cycles) == 3
    assert manager.program_name(manager.last_cycle.program_id) == "Snel 45"
    assert manager.last_cycle.paused_s == pytest.approx(5 * 60, abs=1)

    # de volgende beurt dezelfde naam: de verwachte eindtijd volgt dan dat programma
    await step("Running", 1500, 5)
    assert manager._busy_until(None) == pytest.approx(time.time(), abs=1)
    await name("snel 45")
    program = manager.program_by_name("Snel 45")
    assert manager._busy_until(None) == pytest.approx(manager.detector.start + program.duration_s)


async def test_state_sensor_running_at_startup(hass: HomeAssistant, freezer) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "1500", {"unit_of_measurement": "W"})
    hass.states.async_set(STATE, "running")
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(
        hass,
        appliance("Vaatwasser", "dishwasher", "sensor.vw_power", state_entity=STATE, min_running_s=0, running_states=["RUNNING"]),
        "Vaatwasser",
        "sensor.vw_power",
    )
    manager = entry.runtime_data
    assert manager.detector.running
    for _ in range(40):
        freezer.tick(30)
        manager._on_tick(dt_util.utcnow())
    hass.states.async_set(STATE, "End of cycle")
    await hass.async_block_till_done()
    assert manager.last_cycle.duration_s == pytest.approx(20 * 60, abs=1)


async def test_state_sensor_in_flow(hass: HomeAssistant) -> None:
    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set(STATE, "Idle")
    await add(hass, HUB, "Energie Adviseur", "hub")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "appliance"})
    keys = {str(key) for key in result["data_schema"].schema}
    assert {"state_entity", "running_states", "paused_states", "min_running_s"} <= keys
    data = {k: v for k, v in appliance("Vaatwasser", "dishwasher", "sensor.vw_power").items() if k != "kind"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        data | {"state_entity": STATE, "running_states": ["running", "Rinsing"], "paused_states": ["paused"], "min_running_s": 45},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    manager = entry.runtime_data
    assert manager.state_entity == STATE
    assert manager.running_states == {"running", "rinsing"}
    assert manager.detector.config.state_mode and manager.detector.config.start_delay_s == 45

    # zonder de optie ingevuld krijgt een apparaat de standaardwaarden en blijft het op vermogen werken
    assert entry.data["min_running_s"] == 45
    result = await hass.config_entries.options.async_init(entry.entry_id)
    changed = {k: v for k, v in data.items() if k not in ("name", "appliance_type")}
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "state_entity" not in entry.data
    assert entry.data["running_states"] == ["running"]
    assert not entry.runtime_data.detector.config.state_mode


async def test_switching_to_state_sensor_drops_running_measurement(hass: HomeAssistant) -> None:
    """Stand-by boven de startdrempel mag na het instellen van de statussensor geen cyclus opleveren."""
    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "15", {"unit_of_measurement": "W"})
    hass.states.async_set(STATE, "Ready To Start")
    await add(hass, HUB, "Energie Adviseur", "hub")
    data = appliance("Vaatwasser", "dishwasher", "sensor.vw_power")
    entry = await add(hass, data, "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    for i in range(10):
        manager._feed(time.time() - 3600 + i * 60, 15)
    assert manager.detector.running  # op vermogen: 15 W ligt boven de startdrempel van 10 W

    hass.config_entries.async_update_entry(entry, data=data | {"state_entity": STATE})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data
    assert manager.detector.config.state_mode
    assert not manager.detector.running and manager.detector.start is None
    assert manager.cycles == []


async def test_name_running_cycle(hass: HomeAssistant, freezer) -> None:
    """Een programma dat nog draait alvast een naam geven: die gaat mee met de cyclus als die klaar is."""
    from custom_components.energie_adviseur.panel import appliance_payload

    set_prices(hass)
    hass.states.async_set("sensor.vw_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    status = entity(hass, entry, "status")
    finished = []
    hass.bus.async_listen(f"{DOMAIN}_cycle_finished", lambda event: finished.append(event.data))

    async def power(watt: float) -> None:
        hass.states.async_set("sensor.vw_power", str(watt), {"unit_of_measurement": "W"})
        await hass.async_block_till_done()

    async def minutes(count: int) -> None:
        for _ in range(count):
            freezer.tick(60)
            entry.runtime_data._on_tick(dt_util.utcnow())
        await hass.async_block_till_done()

    async def name(program: str | None) -> None:
        data = {"appliance": "Vaatwasser"} | ({} if program is None else {"program": program})
        await hass.services.async_call(DOMAIN, "label_running_cycle", data, blocking=True)
        await hass.async_block_till_done()

    async def run(before: int, after: int, program: str | None = None) -> None:
        """Een beurt van before + after minuten op 1800 W; halverwege eventueel een naam geven."""
        await power(1800)
        await minutes(before)
        if program is not None:
            await name(program)
        await minutes(after)
        await power(1)
        await minutes(7)

    # er draait niets: alvast benoemen kan niet
    with pytest.raises(HomeAssistantError, match="draait nu niet"):
        await name("Eco")
    assert appliance_payload(manager)["current"] is None

    # tijdens de beurt een nieuwe naam geven; het programma ontstaat pas als de cyclus klaar is
    await power(1800)
    await minutes(5)
    await name("Eco")
    assert hass.states.get(status).attributes["programma"] == "Eco"
    current = appliance_payload(manager)["current"]
    assert current["program"] == "Eco" and current["start"] == manager.detector.start
    assert manager.program_names() == []

    # de naam overleeft een herstart midden in de beurt
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data
    assert manager.detector.running and manager.running_program_name == "Eco"

    await minutes(25)
    await power(1)
    await minutes(7)
    assert not manager.detector.running
    assert manager.program_name(manager.last_cycle.program_id) == "Eco"
    assert manager.program_by_name("Eco").count == 1
    assert manager.running_name is None
    assert finished[-1]["program"] == "Eco"
    assert hass.states.get(status).attributes["programma"] is None

    # een bestaand programma, anders geschreven; hernoemen tijdens de beurt neemt de naam mee
    await power(1800)
    await minutes(5)
    await name("eco")
    assert manager.running_program_name == "Eco"
    await hass.services.async_call(
        DOMAIN, "rename_program", {"appliance": "Vaatwasser", "program": "Eco", "new_name": "Eco 50"}, blocking=True
    )
    assert manager.running_program_name == "Eco 50"
    await minutes(25)
    await power(1)
    await minutes(7)
    assert manager.program_names() == ["Eco 50"]
    assert manager.program_by_name("Eco 50").count == 2

    # een beurt die te kort is voor een cyclus: de naam vervalt en gaat niet naar de volgende beurt
    await run(3, 0, "Kort")
    assert manager.running_name is None
    assert len(manager.cycles) == 2
    await run(10, 2)
    assert len(manager.cycles) == 3
    assert manager.program_name(manager.last_cycle.program_id) != "Kort"
    assert "Kort" not in manager.program_names()

    # een gekozen naam weer wissen: dan wordt het programma gewoon herkend
    await power(1800)
    await minutes(5)
    await name("Iets anders")
    await name(None)
    assert manager.running_program_name is None
    await minutes(25)
    await power(1)
    await minutes(7)
    assert manager.program_name(manager.last_cycle.program_id) == "Eco 50"
    assert "Iets anders" not in manager.program_names()


async def test_page_program_messages(hass: HomeAssistant, hass_ws_client) -> None:
    """Programma's hernoemen, samenvoegen en verwijderen met de berichten die de pagina stuurt."""
    set_prices(hass)
    hass.states.async_set("sensor.droger_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.was_power", "0", {"unit_of_measurement": "W"})
    await add(hass, HUB, "Energie Adviseur", "hub")
    dryer = await add(hass, appliance("Droger", "dryer", "sensor.droger_power"), "Droger", "sensor.droger_power")
    washer = await add(
        hass,
        appliance("Wasmachine", "washer", "sensor.was_power", dryer_entry=dryer.entry_id),
        "Wasmachine",
        "sensor.was_power",
    )
    dm, wm = dryer.runtime_data, washer.runtime_data
    run_cycle(dm, DRY)
    run_cycle(dm, DRY)
    first, second = (c.cycle_id for c in dm.cycles)
    dm.label_cycle("Kastdroog", first)
    dm.label_cycle("Strijkdroog", second)
    dm.select_program("Strijkdroog")
    wm.set_dry_program("Strijkdroog")
    ws = await hass_ws_client(hass)
    ids = iter(range(1, 100))

    async def call(service: str, data: dict) -> dict:
        message = {"id": next(ids), "type": "call_service", "domain": DOMAIN, "service": service, "service_data": data}
        await ws.send_json(message)
        return await ws.receive_json()

    # alvast benoemen kan alleen als het apparaat draait
    answer = await call("label_running_cycle", {"appliance": dryer.entry_id, "program": "Kastdroog"})
    assert not answer["success"] and "draait nu niet" in answer["error"]["message"]

    # hernoemen naar een naam die al bestaat vraagt om samenvoegen
    answer = await call("rename_program", {"appliance": dryer.entry_id, "program": "Strijkdroog", "new_name": "kastdroog"})
    assert not answer["success"] and "samenvoegen" in answer["error"]["message"]
    assert dm.program_names() == ["Kastdroog", "Strijkdroog"]

    answer = await call(
        "rename_program", {"appliance": dryer.entry_id, "program": "Strijkdroog", "new_name": "Kastdroog", "merge": True}
    )
    assert answer["success"], answer
    target = dm.program_by_name("Kastdroog")
    assert dm.program_names() == ["Kastdroog"] and target.count == 2
    assert all(c.program_id == target.program_id for c in dm.cycles)
    assert dm.selected == target.program_id
    assert wm.dry_program == target.program_id

    # alleen hoofdletters aanpassen mag, gewoon hernoemen ook; een lege naam niet
    answer = await call("rename_program", {"appliance": dryer.entry_id, "program": "Kastdroog", "new_name": "KASTDROOG"})
    assert answer["success"], answer
    answer = await call("rename_program", {"appliance": dryer.entry_id, "program": "kastdroog", "new_name": "Kastdroog+"})
    assert answer["success"], answer
    assert dm.program_names() == ["Kastdroog+"]
    answer = await call("rename_program", {"appliance": dryer.entry_id, "program": "Kastdroog+", "new_name": "  "})
    assert not answer["success"]

    # verwijderen: de cycli worden weer onbekend en de wasmachine verwijst er niet meer naar
    answer = await call("delete_program", {"appliance": dryer.entry_id, "program": "Kastdroog+"})
    assert answer["success"], answer
    assert dm.program_names() == []
    assert all(c.program_id is None for c in dm.cycles)
    assert wm.dry_program is None

    # na herladen is alles nog zo
    assert await hass.config_entries.async_reload(dryer.entry_id)
    await hass.async_block_till_done()
    assert dryer.runtime_data.program_names() == []


def set_day_prices(hass: HomeAssistant, today: list[float], tomorrow: list[float] | None = None) -> None:
    """Uurprijzen vanaf middernacht: heel vandaag en, als gegeven, heel morgen."""
    start = dt_util.start_of_local_day()
    raw = [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(), "value": v}
        for i, v in enumerate(today + (tomorrow or []))
    ]
    hass.states.async_set(PRICE, "0.30", {"unit_of_measurement": "EUR/kWh", "raw_today": raw})


async def test_advice_today_and_tomorrow(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(datetime(2026, 11, 18, 12, 0, tzinfo=dt_util.get_default_time_zone()))
    today = [0.30] * 18 + [0.20, 0.20] + [0.30] * 4  # vandaag het goedkoopst 18:00-20:00
    tomorrow = [0.30, 0.30, 0.10, 0.10] + [0.30] * 20  # morgen het goedkoopst 02:00-04:00
    set_day_prices(hass, today)
    hass.states.async_set("sensor.vw_power", "0", {"unit_of_measurement": "W"})
    hub = await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    run_cycle(manager, [(120, 1000)])  # 2 uur, 1 kW
    await hass.services.async_call(DOMAIN, "label_cycle", {"appliance": entry.entry_id, "program": "Eco"}, blocking=True)
    await hass.async_block_till_done()

    def state(key):
        return hass.states.get(entity(hass, entry, key))

    def local(value):
        return dt_util.as_local(dt_util.parse_datetime(value))

    # alleen de prijzen van vandaag: advies voor vandaag, niets voor morgen
    assert local(state("best_start_today").state).hour == 18
    assert state("best_start_tomorrow").state == "unknown"
    assert state("tomorrow_saving").state == "unknown"
    assert "beste start 18:00" in manager.summary()

    # prijzen van morgen binnen
    set_day_prices(hass, today, tomorrow)
    manager.refresh_advice()
    manager.notify()
    await hass.async_block_till_done()
    first = local(state("best_start_today").state)
    assert (first.date(), first.hour) == (dt_util.now().date(), 18)
    second = local(state("best_start_tomorrow").state)
    assert (second.date(), second.hour) == (dt_util.now().date() + timedelta(days=1), 2)
    assert state("best_start_tomorrow").attributes["kosten"] == pytest.approx(0.20, abs=0.02)
    saving = state("tomorrow_saving")
    assert float(saving.state) == pytest.approx(0.20, abs=0.02)
    assert saving.attributes["kosten_vandaag"] == pytest.approx(0.40, abs=0.02)
    text = manager.summary()
    assert "vandaag het best om 18:00" in text and "morgen het best om 02:00" in text
    assert "Wacht je tot morgen, dan bespaar je ca. €0.2" in text

    # de hub maakt het dagadvies één keer opnieuw zodra de prijzen van morgen bekend zijn
    hub_manager = hub.runtime_data
    assert hub_manager.generated is None
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hub_manager.generated is not None
    assert "morgen het best om 02:00" in hass.states.get(entity(hass, hub, "daily_advice")).attributes["advies"]
    generated = hub_manager.generated
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hub_manager.generated == generated

    # is vandaag goedkoper, dan zegt het advies dat
    set_day_prices(hass, today, [0.30] * 24)
    manager.refresh_advice()
    assert manager.tomorrow_saving == pytest.approx(-0.20, abs=0.02)
    assert "Vandaag is ca. €0.2" in manager.summary()
