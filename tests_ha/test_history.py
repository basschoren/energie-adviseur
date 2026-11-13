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

    # nogmaals inlezen levert geen dubbele cycli op
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"entry": entry.entry_id, "days": 1}, blocking=True, return_response=True
    )
    assert response == {"imported": 0}
    assert len(manager.cycles) == 1
