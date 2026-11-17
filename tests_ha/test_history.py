"""Historie inlezen uit de recorder. Eigen bestand: de recorder moet vóór hass worden opgezet."""

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from test_integration import DOMAIN, HUB, add, appliance, set_prices


@pytest.fixture(autouse=True)
def _recorder_first(recorder_mock, enable_custom_integrations):
    yield


async def test_import_history(recorder_mock, hass: HomeAssistant, freezer) -> None:
    set_prices(hass)
    # een cyclus van 40 minuten die de recorder vastlegt voordat het apparaat is toegevoegd
    for minute in range(60):
        watt = 1800 + minute % 2 if 5 <= minute < 45 else minute % 2
        hass.states.async_set("sensor.vw_power", str(watt), {"unit_of_measurement": "W"})
        freezer.tick(60)
    await async_wait_recording_done(hass)

    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    assert manager.last_cycle is None
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entry": entry.entry_id, "days": 1}, blocking=True, return_response=True
    )
    assert response == {"imported": 1}
    assert manager.last_cycle.energy_kwh == pytest.approx(1.2, abs=0.1)
    assert len(manager.unlabeled()) == 1

    # nogmaals inlezen (nu op naam) levert geen dubbele cycli op
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entry": "vaatwasser", "days": 1}, blocking=True, return_response=True
    )
    assert response == {"imported": 0}
    assert len(manager.cycles) == 1

    # dezelfde beurt met een iets andere starttijd (zoals live gemeten) telt niet als nieuw
    manager.cycles[0].start += 20
    manager.cycles[0].cycle_id = str(int(manager.cycles[0].start))
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entry": entry.entry_id, "days": 1}, blocking=True, return_response=True
    )
    assert response == {"imported": 0}

    # een verwijderde cyclus komt bij opnieuw inlezen niet terug
    await hass.services.async_call(
        DOMAIN, "delete_cycle", {"appliance": "Vaatwasser", "cycle_id": manager.cycles[0].cycle_id}, blocking=True
    )
    assert manager.cycles == []
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entry": entry.entry_id, "days": 1}, blocking=True, return_response=True
    )
    assert response == {"imported": 0}
    assert manager.cycles == []


async def test_import_history_from_page(hass: HomeAssistant, hass_ws_client) -> None:
    """Het bericht dat de zijbalkpagina stuurt voor 'Historie inlezen'."""
    set_prices(hass)
    await add(hass, HUB, "Energie Adviseur", "hub")
    hass.states.async_set("sensor.vw_power", "0", {"unit_of_measurement": "W"})
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {
            "id": 1,
            "type": "call_service",
            "domain": DOMAIN,
            "service": "import_history",
            "service_data": {"entry": entry.entry_id, "days": 10},
            "return_response": True,
        }
    )
    answer = await ws.receive_json()
    assert answer["success"], answer
    assert answer["result"]["response"] == {"imported": 0}


async def test_restore_cycle_from_recorder(hass: HomeAssistant, freezer) -> None:
    """Een oude, verkeerd gesplitste cyclus zonder bewaard origineel komt terug uit de recorder."""
    set_prices(hass)
    for minute in range(60):
        watt = 1800 + minute % 2 if 5 <= minute < 45 else minute % 2
        hass.states.async_set("sensor.vw_power", str(watt), {"unit_of_measurement": "W"})
        freezer.tick(60)
    await async_wait_recording_done(hass)
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    await manager.async_import_history(1)
    imported = manager.cycles[0]

    # een benoemde cyclus van voor 0.5 opnieuw ophalen: de naam en het programma blijven
    imported.detail = []
    manager.label_cycle("Eco 50")
    await manager.async_restore_cycle(imported.cycle_id)
    assert len(manager.cycles) == 1 and manager.cycles[0].step_s == 30
    assert manager.program_name(manager.cycles[0].program_id) == "Eco 50"
    assert manager.program_by_name("Eco 50").count == 1
    manager.delete_program("Eco 50")
    imported = manager.cycles[0]

    # zoals een cyclus van voor 0.5: alleen blokken van 5 minuten, gesplitst zonder bewaard origineel
    imported.detail = []
    manager.split_cycle(imported.cycle_id, minutes=20)
    manager.originals = []
    assert len(manager.cycles) == 2
    assert all(c.step_s == 300 for c in manager.cycles)
    manager.delete_cycle(manager.cycles[0].cycle_id)

    response = await hass.services.async_call(
        DOMAIN, "restore_cycle", {"appliance": "Vaatwasser", "cycle_id": manager.cycles[0].cycle_id},
        blocking=True, return_response=True,
    )
    assert len(response["cycles"]) == 1
    assert len(manager.cycles) == 1
    restored = manager.cycles[0]
    assert restored.step_s == 30
    assert restored.duration_s == pytest.approx(40 * 60, abs=60)
    assert restored.energy_kwh == pytest.approx(1.2, abs=0.1)
    assert manager.removed == []

    # splitsen kan nu per halve minuut, en met from_history komt de hele cyclus weer uit de recorder
    manager.split_cycle(restored.cycle_id, minutes=12.5)
    assert manager.cycles[0].duration_s == 12.5 * 60
    await manager.async_restore_cycle(manager.cycles[1].cycle_id, from_history=True)
    assert len(manager.cycles) == 1 and manager.originals == []
    assert manager.cycles[0].duration_s == pytest.approx(40 * 60, abs=60)


async def test_import_history_with_state_sensor(hass: HomeAssistant, freezer) -> None:
    """Historie inlezen gebruikt ook de historie van de statussensor, inclusief de pauze."""
    set_prices(hass)
    state_entity = "sensor.dishwasher_appliance_state"
    script = [("Idle", 2, 5), ("Running", 1800, 20), ("Paused", 4, 10), ("Running", 900, 20), ("End of cycle", 3, 2), ("Idle", 2, 5)]
    for state, watt, minutes in script:
        hass.states.async_set(state_entity, state)
        for minute in range(minutes):
            hass.states.async_set("sensor.vw_power", str(watt + minute % 2), {"unit_of_measurement": "W"})
            freezer.tick(60)
    await async_wait_recording_done(hass)
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(
        hass,
        appliance("Vaatwasser", "dishwasher", "sensor.vw_power", state_entity=state_entity, min_running_s=60),
        "Vaatwasser",
        "sensor.vw_power",
    )
    manager = entry.runtime_data
    assert await manager.async_import_history(1) == 1
    cycle = manager.cycles[0]
    assert cycle.duration_s == pytest.approx(40 * 60, abs=1)
    assert cycle.paused_s == pytest.approx(10 * 60, abs=1)
    assert cycle.energy_kwh == pytest.approx(0.6 + 0.3, abs=0.02)


async def test_adjust_cycle(hass: HomeAssistant, freezer, hass_ws_client) -> None:
    """Begin en einde verleggen: verlengen meet opnieuw uit de recorder, inkorten gebruikt het bewaarde verloop."""
    set_prices(hass)
    ws = await hass_ws_client(hass)  # eerst verbinden: de aanmelding verloopt als de klok een uur opschuift
    # 5 min rust, 10 min voorspoelen op 8 W (onder de startdrempel), 40 min 1800 W, 15 min rust
    for minute in range(70):
        watt = 8 if 5 <= minute < 15 else 1800 if 15 <= minute < 55 else 0
        hass.states.async_set("sensor.vw_power", str(watt + minute % 2), {"unit_of_measurement": "W"})
        freezer.tick(60)
    await async_wait_recording_done(hass)
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(hass, appliance("Vaatwasser", "dishwasher", "sensor.vw_power"), "Vaatwasser", "sensor.vw_power")
    manager = entry.runtime_data
    assert await manager.async_import_history(1) == 1
    first = manager.cycles[0]
    assert first.duration_s == pytest.approx(40 * 60, abs=1)
    manager.label_cycle("Eco 50")
    start, end, kwh = first.start, first.end, first.energy_kwh

    # de pagina vraagt het vermogen rond de cyclus op: daar is het voorspoelen te zien
    await ws.send_json({"id": 1, "type": "energie_adviseur/cycle_context", "entry_id": entry.entry_id, "cycle_id": first.cycle_id})
    answer = await ws.receive_json()
    assert answer["success"], answer
    context = answer["result"]
    assert context["step_s"] == 30
    assert context["start"] == pytest.approx(start - 15 * 60, abs=1)  # de eerste meting: verder terug is er niets
    assert context["end"] == pytest.approx(end + 15 * 60, abs=1)  # tot nu
    assert context["min_start"] == 0 and context["max_end"] == pytest.approx(end + 15 * 60, abs=1)
    before = context["series"][10:30]  # 5 tot 15 minuten na de eerste meting
    assert all(8 <= watt <= 9 for watt in before)
    await ws.send_json({"id": 2, "type": "energie_adviseur/cycle_context", "entry_id": entry.entry_id, "cycle_id": "0"})
    assert not (await ws.receive_json())["success"]

    # het begin 10 minuten eerder: het voorspoelen hoort erbij
    response = await hass.services.async_call(
        DOMAIN, "adjust_cycle", {"appliance": "Vaatwasser", "cycle_id": first.cycle_id, "start_shift": -10},
        blocking=True, return_response=True,
    )
    assert len(manager.cycles) == 1
    longer = manager.cycles[0]
    assert response["cycles"][0]["cycle_id"] == longer.cycle_id == str(int(start - 600))
    assert response["cycles"][0]["programma"] == "Eco 50"
    assert (longer.start, longer.end) == (start - 600, end)
    assert longer.step_s == 30 and len(longer.detail) == 100
    assert all(8 <= watt <= 9 for watt in longer.detail[:20])
    assert longer.energy_kwh == pytest.approx(kwh + 8.5 * 600 / 3.6e6, abs=0.001)
    # het programma is opnieuw berekend en het advies rekent met de langere duur
    program = manager.program_by_name("Eco 50")
    assert (program.count, program.duration_s) == (1, pytest.approx(50 * 60, abs=1))
    # historie inlezen ziet dit als dezelfde beurt
    assert await manager.async_import_history(1) == 0

    # het einde 5 minuten eerder, zoals de pagina het stuurt
    await ws.send_json(
        {
            "id": 3,
            "type": "call_service",
            "domain": DOMAIN,
            "service": "adjust_cycle",
            "service_data": {"appliance": entry.entry_id, "cycle_id": longer.cycle_id, "start_shift": 0, "end_shift": -5},
            "return_response": True,
        }
    )
    answer = await ws.receive_json()
    assert answer["success"], answer
    shorter = manager.cycles[0]
    assert (shorter.start, shorter.end) == (start - 600, end - 300)
    assert shorter.energy_kwh == pytest.approx(longer.energy_kwh - 0.15, abs=0.002)
    assert manager.program_name(shorter.program_id) == "Eco 50"
    assert manager.program_by_name("Eco 50").duration_s == pytest.approx(45 * 60, abs=1)

    # fouten: geen verschuiving, einde voor het begin, einde in de toekomst
    for data in ({}, {"start_shift": 60}, {"end_shift": -60}, {"end_shift": 30}):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "adjust_cycle", {"appliance": "Vaatwasser", **data}, blocking=True)
    assert manager.cycles == [shorter]

    # ongedaan maken gaat stap voor stap terug, met behoud van de naam
    assert manager.original_of(shorter) is not None
    await manager.async_restore_cycle(shorter.cycle_id)
    assert (manager.cycles[0].start, manager.cycles[0].end) == (start - 600, end)
    await manager.async_restore_cycle(manager.cycles[0].cycle_id)
    assert len(manager.cycles) == 1
    restored = manager.cycles[0]
    assert (restored.start, restored.end, restored.energy_kwh) == (start, end, kwh)
    assert manager.program_name(restored.program_id) == "Eco 50"
    assert manager.original_of(restored) is None

    # een cyclus mag een andere cyclus niet overlappen
    manager.split_cycle(restored.cycle_id, minutes=20)
    with pytest.raises(HomeAssistantError):
        await manager.async_adjust_cycle(manager.cycles[0].cycle_id, end_shift=2)
    with pytest.raises(HomeAssistantError):
        await manager.async_adjust_cycle(manager.cycles[1].cycle_id, start_shift=-2)
    # inkorten van het eerste deel mag wel; ongedaan maken geeft eerst het deel terug, dan de hele cyclus
    await manager.async_adjust_cycle(manager.cycles[0].cycle_id, end_shift=-2)
    assert manager.cycles[0].duration_s == 18 * 60
    await manager.async_restore_cycle(manager.cycles[0].cycle_id)
    assert [c.duration_s for c in manager.cycles] == [20 * 60, pytest.approx(20 * 60, abs=1)]
    await manager.async_restore_cycle(manager.cycles[0].cycle_id)
    assert len(manager.cycles) == 1 and manager.cycles[0].end == end


async def test_adjust_cycle_with_state_sensor(hass: HomeAssistant, freezer) -> None:
    """Bij een apparaat met statussensor blijft de pauze buiten de aangepaste cyclus."""
    set_prices(hass)
    state_entity = "sensor.dishwasher_appliance_state"
    script = [("Idle", 2, 5), ("Running", 1800, 20), ("Paused", 4, 10), ("Running", 900, 20), ("End of cycle", 30, 4), ("Idle", 2, 5)]
    for state, watt, minutes in script:
        hass.states.async_set(state_entity, state)
        for minute in range(minutes):
            hass.states.async_set("sensor.vw_power", str(watt + minute % 2), {"unit_of_measurement": "W"})
            freezer.tick(60)
    await async_wait_recording_done(hass)
    await add(hass, HUB, "Energie Adviseur", "hub")
    entry = await add(
        hass,
        appliance("Vaatwasser", "dishwasher", "sensor.vw_power", state_entity=state_entity, min_running_s=60),
        "Vaatwasser",
        "sensor.vw_power",
    )
    manager = entry.runtime_data
    assert await manager.async_import_history(1) == 1
    cycle = manager.cycles[0]
    start, end, kwh = cycle.start, cycle.end, cycle.energy_kwh
    assert (cycle.duration_s, cycle.paused_s) == (pytest.approx(40 * 60, abs=1), pytest.approx(10 * 60, abs=1))

    # het nadrogen van 4 minuten hoort erbij: opnieuw gemeten, de pauze telt nog steeds niet mee
    await manager.async_adjust_cycle(cycle.cycle_id, end_shift=4)
    longer = manager.cycles[0]
    assert (longer.start, longer.end) == (start, end + 240)
    assert longer.paused_s == pytest.approx(10 * 60, abs=1)
    assert longer.duration_s == pytest.approx(44 * 60, abs=1)
    assert longer.energy_kwh == pytest.approx(kwh + 30.5 * 240 / 3.6e6, abs=0.001)
    assert len(longer.detail) == 88

    # inkorten aan het begin: de pauze en het einde blijven staan
    await manager.async_adjust_cycle(longer.cycle_id, start_shift=5)
    shorter = manager.cycles[0]
    assert (shorter.start, shorter.end) == (start + 300, end + 240)
    assert shorter.paused_s == longer.paused_s
    assert shorter.duration_s == pytest.approx(39 * 60, abs=1)
    assert shorter.energy_kwh == pytest.approx(longer.energy_kwh - 0.15, abs=0.002)

    # het einde tot voor de pauze: opnieuw gemeten, dus zonder pauze en zonder dat er looptijd verdwijnt
    await manager.async_adjust_cycle(shorter.cycle_id, end_shift=(start + 900 - shorter.end) / 60)
    early = manager.cycles[0]
    assert (early.start, early.end, early.paused_s) == (start + 300, start + 900, 0)
    assert early.duration_s == 600
    assert early.energy_kwh == pytest.approx(0.30, abs=0.002)

    # zonder historie valt inkorten terug op het bewaarde verloop; de pauze blijft dan in zijn geheel staan
    await manager.async_restore_cycle(early.cycle_id)
    assert manager.cycles[0].end == end + 240
    hass.config.components.remove("recorder")
    await manager.async_adjust_cycle(manager.cycles[0].cycle_id, end_shift=-4)
    assert (manager.cycles[0].end, manager.cycles[0].paused_s) == (end, longer.paused_s)
    with pytest.raises(HomeAssistantError):
        await manager.async_adjust_cycle(manager.cycles[0].cycle_id, end_shift=1)
    hass.config.components.add("recorder")
