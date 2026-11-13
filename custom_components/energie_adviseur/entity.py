"""Basisklasse voor alle entiteiten van Energie Adviseur."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity


class EAEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: Any, key: str) -> None:
        self.manager = manager
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = manager.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.manager.signal, self.async_write_ha_state))
