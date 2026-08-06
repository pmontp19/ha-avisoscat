"""The Avisos Meteocat (avisoscat) integration.

Exposes the Catalan Meteorological Service's severe-weather warnings
(Situació Meteorològica de Perill) per comarca, sourced from meteo.cat.

This module is the scaffold's entry point: it sets up and tears down a config
entry cleanly, which is all HA needs to load the integration. The coordinator,
the one-minute validity recompute (docs/04-architecture.md §5) and the entity
platforms arrive with the tasks that implement them.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Target set is BINARY_SENSOR + SENSOR (docs/04-architecture.md §9). It stays
# empty until those platform modules exist: forwarding a config entry to a
# platform with no module raises and would make the integration unloadable.
PLATFORMS: tuple[Platform, ...] = ()

__all__ = ["DOMAIN", "PLATFORMS"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up avisoscat from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an avisoscat config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
