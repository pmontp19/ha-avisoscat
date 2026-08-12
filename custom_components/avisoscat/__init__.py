"""The Avisos Meteocat (avisoscat) integration.

Exposes the Catalan Meteorological Service's severe-weather warnings
(Situació Meteorològica de Perill) per comarca, sourced from meteo.cat.

Each config entry owns one comarca and one `AvisoscatDataUpdateCoordinator`
that lives on `entry.runtime_data` (docs/04-architecture.md §9). Setup arms the
coordinator with a first refresh, hooks the once-a-minute validity recompute
that fires `started` / `cleared` between polls without touching the network
(docs/04-architecture.md §5), and forwards the entry to its entity platforms.

`PLATFORMS` grows as platform modules land: forwarding an entry to a platform
with no module raises and makes the integration unloadable, so each one is
wired only when there is something to forward to. The sensor platform is the
first; `binary_sensor` joins when its task lands. The coordinator and events do
not depend on any platform.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from . import coordinator
from .const import DOMAIN

# Target set is BINARY_SENSOR + SENSOR (docs/04-architecture.md §9). Each
# platform is wired only when its module exists: forwarding a config entry to a
# platform with no module raises and would make the integration unloadable, so
# `binary_sensor` joins this tuple when its task lands.
PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR,)

# The coordinator a config entry carries on its `runtime_data`. Typing the entry
# this way gives every platform `entry.runtime_data` already typed as the
# coordinator, with no cast and no `hass.data` lookup (docs/04-architecture.md
# §9; multi-entry, so one coordinator per comarca, never a shared dict).
AvisoscatConfigEntry = ConfigEntry[coordinator.AvisoscatDataUpdateCoordinator]

__all__ = ["DOMAIN", "PLATFORMS", "AvisoscatConfigEntry"]


async def async_setup_entry(hass: HomeAssistant, entry: AvisoscatConfigEntry) -> bool:
    """Set up avisoscat from a config entry.

    Builds the source for the entry (the API-key client when a key was
    validated, the keyless public page otherwise), arms the coordinator with a
    first refresh, registers the network-free minute recompute against the
    cached snapshot, and forwards the entry to its platforms.
    """
    # `build_source` is reached through the module (`coordinator.build_source`)
    # rather than a name re-imported into this file, so the test fixture that
    # patches `custom_components.avisoscat.coordinator.build_source` is the seam.
    coord = coordinator.AvisoscatDataUpdateCoordinator(
        hass, entry, coordinator.build_source(hass, entry)
    )
    entry.runtime_data = coord

    # The 6-hour UTC bands change without the source changing, so validity is
    # re-evaluated every minute against the cached snapshot. The unsubscribe it
    # returns is registered for cleanup, so unloading the entry cancels it.
    entry.async_on_unload(
        async_track_time_change(hass, coord.async_schedule_minute_recompute, second=0)
    )

    await coord.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AvisoscatConfigEntry) -> bool:
    """Unload an avisoscat config entry and stop its coordinator."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_shutdown()
    return unload_ok
