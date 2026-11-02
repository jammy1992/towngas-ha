"""Button platform for TownGas Meter — manual data refresh."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_SUBS_CODE, CONF_SUBS_ID, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data["coordinator"]
    subs_id = entry.data[CONF_SUBS_ID]
    subs_code = entry.data.get(CONF_SUBS_CODE, subs_id[:8])

    async_add_entities([TownGasRefreshButton(coordinator, subs_id, subs_code)])


class TownGasRefreshButton(ButtonEntity):
    """Button that triggers an immediate data refresh."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "refresh_data"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        subs_id: str,
        subs_code: str,
    ) -> None:
        self._coordinator = coordinator
        self._subs_id = subs_id
        self._subs_code = subs_code
        self._attr_unique_id = f"towngas_{subs_id}_refresh"

    @property
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, self._subs_id)}}

    async def async_press(self) -> None:
        """Trigger an immediate coordinator refresh."""
        await self._coordinator.async_request_refresh()
