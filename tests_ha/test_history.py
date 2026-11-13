"""Historie inlezen uit de recorder. Eigen bestand: de recorder moet vóór hass worden opgezet."""

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.core import HomeAssistant

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
