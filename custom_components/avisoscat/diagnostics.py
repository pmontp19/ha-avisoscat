"""Diagnostics export for the avisoscat integration.

Home Assistant's diagnostics platform lets a user download a snapshot of one
config entry's state, which is what makes bug reports actionable for a source
that is not an official API. This module ships that snapshot in two shapes
(docs/03-feature-spec.md §3.12):

* `async_get_config_entry_diagnostics` is the entry-level download: the config
  entry, the coordinator's projections and failure counters, and the poll
  cadence in force.
* `async_get_device_diagnostics` adds the device-level view, which for this
  integration is the same entry projected onto its per-comarca device.

Both redact the three things the architecture (docs/04-architecture.md §11)
says must never leave the user's instance: `latitude`, `longitude` (the user's
location, which the config flow resolved into a comarca) and `api_key` (the
optional Meteocat key). The redaction runs through Home Assistant's own
`async_redact_data`, which walks nested dicts and lists, so a future field that
happens to be called `api_key` under any depth is covered automatically.

External text (`comentari`, `llindar`, `meteor_nom`) is **not** redacted: the
user already sees it on their dashboard, and redacting it would make the
diagnostic useless for the parsing regressions it exists to debug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_ID_COMARCA
from .coordinator import AvisoscatDataUpdateCoordinator

if TYPE_CHECKING:
    from . import AvisoscatConfigEntry

# Keys whose values are sensitive enough that they must never appear in a
# diagnostic download (docs/04-architecture.md §11). `async_redact_data`
# walks nested dicts and lists, so this also covers the same keys at any
# depth, not just the top of `config_entry.data`.
TO_REDACT: frozenset[str] = frozenset({"latitude", "longitude", "api_key"})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # kept for the diagnostics platform signature
    entry: AvisoscatConfigEntry,
) -> dict[str, Any]:
    """Return the diagnostic snapshot for one config entry.

    The shape is stable enough for diffing across reports: the entry's data
    and options, the coordinator's failure counters, the in-force and
    announced projections (their `repr` is the user-facing summary), the
    poll cadence actually in force, and the source kind that produced the
    snapshot. Sensitive keys are redacted recursively before the dict leaves
    this function.
    """
    coordinator: AvisoscatDataUpdateCoordinator = entry.runtime_data
    state = coordinator.data

    return async_redact_data(
        {
            "config_entry": _config_entry_view(entry, coordinator),
            "coordinator": _coordinator_view(coordinator, state),
            "source": _source_view(coordinator),
        },
        TO_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: AvisoscatConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return the diagnostic snapshot for one device.

    Every avisoscat device is the per-comarca device of exactly one config
    entry (docs/04-architecture.md §9), so the device view is the config entry
    view plus the device's own registry entry. The comarca id is cross-checked
    against the entry to make the diagnostic self-describing.
    """
    base = await async_get_config_entry_diagnostics(hass, entry)
    base["device"] = {
        "id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "identifiers": [list(ident) for ident in device.identifiers],
        "id_comarca": int(entry.data[CONF_ID_COMARCA]),
    }
    return async_redact_data(base, TO_REDACT)


# ---------------------------------------------------------------------------
# Views: each picks the fields worth reporting and never re-fetches anything.
#
# A diagnostic that triggered a fetch would be worse than useless during an
# outage, so every view reads only what the coordinator already holds in
# memory. The projections are converted through their `repr` (the dataclasses
# carry `__repr__`), which is good enough for diffing without dragging in the
# full projection shape, and `last_error` is the failure summary the
# coordinator already stringified.
# ---------------------------------------------------------------------------


def _config_entry_view(
    entry: AvisoscatConfigEntry,
    coordinator: AvisoscatDataUpdateCoordinator,
) -> dict[str, Any]:
    """The entry's data and options, plus the live poll cadence.

    `as_dict()` is the canonical HA shape, used by every core diagnostics
    module; running it through `async_redact_data` at the top level covers
    `api_key` whether it lives in `data` (the documented place) or anywhere
    else the user might have ended up putting it.
    """
    raw = entry.as_dict()
    return {
        "title": raw.get("title"),
        "data": raw.get("data", {}),
        "options": raw.get("options", {}),
        "update_interval_minutes": _interval_minutes(coordinator),
    }


def _coordinator_view(
    coordinator: AvisoscatDataUpdateCoordinator,
    state: Any,
) -> dict[str, Any]:
    """The runtime state worth reporting, none of which needs a fetch.

    `consecutive_failures` and `degraded_announced` are the resilience
    bookkeeping (docs/04-architecture.md §10); the projections are the
    cleaned-up picture the entities already render; `last_update_success` and
    `update_interval` come from the `DataUpdateCoordinator` base.
    """
    return {
        "last_update_success": coordinator.last_update_success,
        "update_interval_seconds": _interval_seconds(coordinator),
        "consecutive_failures": coordinator.consecutive_failures,
        "degraded_announced": coordinator.degraded_announced,
        "last_error": state.last_error if state is not None else None,
        "en_vigor": [repr(af) for af in state.en_vigor] if state is not None else [],
        "anunciats": [repr(af) for af in state.anunciats] if state is not None else [],
        "outlook": [repr(day) for day in state.outlook] if state is not None else [],
    }


def _source_view(coordinator: AvisoscatDataUpdateCoordinator) -> dict[str, Any]:
    """The source kind and any quota info it carries.

    The class name is enough for triage: `ApiKeySource` and `PublicPageSource`
    fail in characteristically different ways. The quota band is reported only
    when a quota-driven interval is actually in force (API-key source whose
    quota endpoint was read successfully).
    """
    view: dict[str, Any] = {"kind": coordinator.source_kind}
    quota_interval = coordinator.quota_interval
    if quota_interval is not None:
        view["quota_interval_seconds"] = int(quota_interval.total_seconds())
        view["quota_band"] = _quota_band_label(quota_interval.total_seconds())
    return view


def _quota_band_label(interval_seconds: float) -> str:
    """Reverse-map a quota interval to the spec band it belongs to (§6).

    Used only to annotate the diagnostic so a reader can tell at a glance
    whether the user is on the citizen plan (8 h) or above it, without
    repeating the arithmetic the coordinator already did.
    """
    seconds = int(interval_seconds)
    if seconds == 30 * 60:
        return "> 500"
    if seconds == 2 * 60 * 60:
        return "200-500"
    if seconds == 8 * 60 * 60:
        return "<= 200 (citizen)"
    return "unknown"


def _interval_minutes(coordinator: AvisoscatDataUpdateCoordinator) -> float | None:
    """The current poll cadence in minutes, for at-a-glance reading."""
    interval = coordinator.update_interval
    return None if interval is None else interval.total_seconds() / 60


def _interval_seconds(coordinator: AvisoscatDataUpdateCoordinator) -> float | None:
    """The current poll cadence in seconds, the unit HA's UI does not surface."""
    interval = coordinator.update_interval
    return None if interval is None else interval.total_seconds()
