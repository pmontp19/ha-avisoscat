"""Tests for the avisoscat config flow.

The point-in-polygon resolution path and the manual-dropdown fallback are the
two acceptance criteria, so they are driven end to end against the captured
comarques TopoJSON (no real network: `aioresponses` serves the fixture, exactly
as `tests/test_comarques.py` does). API-key validation is unit-tested in
isolation and patched in the flow tests so a flow never depends on the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import aioresponses
from custom_components.avisoscat import config_flow
from custom_components.avisoscat.comarques import ComarcaResolution, ResolutionError
from custom_components.avisoscat.const import (
    CONF_API_KEY,
    CONF_ID_COMARCA,
    CONF_INCLUDE_SEA,
    CONF_LOCATION,
    CONF_METEORS,
    CONF_SCAN_INTERVAL,
    CONF_SEVERE_THRESHOLD,
    DOMAIN,
)
from custom_components.avisoscat.models import Meteor
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import FakeSource, make_config_entry

FIXTURE = Path(__file__).parent / "fixtures" / "comarquesAmbMar.json"

# Real coordinates reused from tests/test_comarques.py so the resolution result
# is not in doubt: Vic resolves to Osona (24), Fraga (Aragon) to nothing.
VIC = (41.9301, 2.2545)
BARCELONA = (41.3874, 2.1686)  # Barcelonès (13), a coastal comarca
FRAGA_ARAGON = (41.5210, 0.3490)  # outside Catalonia

ALL_METEORS = [meteor.value for meteor in Meteor]


def _location(lat: float, lon: float) -> dict[str, dict[str, float]]:
    """Build the user-step input for a coordinate."""
    return {
        CONF_LOCATION: {"latitude": lat, "longitude": lon},
    }


def _options_input(
    *, api_key: str = "", severe_threshold: int = 3, include_sea: bool | None = None
) -> dict[str, Any]:
    """Build a valid options-step input.

    `scan_interval` is deliberately omitted so the entry stores adaptive
    polling (None), matching what a blank form submits.
    """
    data: dict[str, Any] = {
        CONF_API_KEY: api_key,
        CONF_METEORS: ALL_METEORS,
        CONF_SEVERE_THRESHOLD: severe_threshold,
    }
    if include_sea is not None:
        data[CONF_INCLUDE_SEA] = include_sea
    return data


def _field_names(result: dict[str, Any]) -> set[str]:
    """Pull the field names out of a form result's voluptuous schema."""
    return {field.schema for field in result["data_schema"].schema}


# ---------------------------------------------------------------------------
# User step: location shown, then resolved by point-in-polygon
# ---------------------------------------------------------------------------


async def test_user_step_prefills_home_location(hass: HomeAssistant) -> None:
    """The first form is the location marker, defaulting to the home zone."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    assert CONF_LOCATION in _field_names(result)
    # The default is the Home Assistant home-zone coordinate pair. Voluptuous
    # wraps a mutable dict default in a factory, so call it to recover the value.
    location_marker = next(
        field for field in result["data_schema"].schema if field.schema == CONF_LOCATION
    )
    default = location_marker.default
    location_default = default() if callable(default) else default
    assert location_default == {
        "latitude": hass.config.latitude,
        "longitude": hass.config.longitude,
    }


async def test_user_location_resolves_to_comarca_via_point_in_polygon(
    hass: HomeAssistant,
) -> None:
    """The acceptance path: a marker in Vic resolves to Osona and proceeds.

    This is the end-to-end proof that the flow delegates resolution to
    `comarques.py` (real point-in-polygon over the captured geometry) instead
    of reimplementing it.
    """
    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )

    # Resolved: straight to options, no dropdown.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "options"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _options_input()
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Avisos Meteocat — Osona"
    assert result["data"] == {CONF_ID_COMARCA: 24}
    assert CONF_API_KEY not in result["data"]
    assert result["options"][CONF_METEORS] == ALL_METEORS
    assert result["options"][CONF_SEVERE_THRESHOLD] == 3
    # Adaptive polling: a blank interval is stored as None, not a number.
    assert result["options"][CONF_SCAN_INTERVAL] is None
    assert CONF_INCLUDE_SEA not in result["options"]


# ---------------------------------------------------------------------------
# Dropdown fallback: every resolution failure lands on the same way out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "error_key"),
    [
        (FRAGA_ARAGON, "location_outside_catalonia"),
    ],
)
async def test_location_outside_catalonia_falls_back_to_dropdown(
    hass: HomeAssistant, point: tuple[float, float], error_key: str
) -> None:
    """An unresolved location shows the comarca dropdown with the reason."""
    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*point)
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "comarca"
    assert result["errors"] == {"base": error_key}
    assert CONF_ID_COMARCA in _field_names(result)


async def test_cannot_connect_falls_back_to_dropdown(hass: HomeAssistant) -> None:
    """A dead source must never block the flow: the dropdown is the way out."""
    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            status=500,
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "comarca"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_geometry_falls_back_to_dropdown(hass: HomeAssistant) -> None:
    """A payload that yields no usable geometry is still a dropdown, not a crash."""
    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            payload={},
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "comarca"
    assert result["errors"] == {"base": "invalid_geometry"}


async def test_dropdown_pick_creates_an_entry(hass: HomeAssistant) -> None:
    """The manual dropdown is a complete path, not just an error screen."""
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    # Drive via the comarca step by patching resolution to fail first.
    with patch(
        "custom_components.avisoscat.config_flow.async_resolve_comarca",
        return_value=ComarcaResolution(
            error=ResolutionError.LOCATION_OUTSIDE_CATALONIA
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _location(*FRAGA_ARAGON)
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "comarca"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ID_COMARCA: "24"}
    )
    assert result["step_id"] == "options"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _options_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Avisos Meteocat — Osona"
    assert result["data"] == {CONF_ID_COMARCA: 24}


# ---------------------------------------------------------------------------
# Multi-entry: same comarca aborts, different comarques coexist
# ---------------------------------------------------------------------------


async def test_duplicate_comarca_aborts(hass: HomeAssistant) -> None:
    """A second entry for the same comarca aborts before the options form."""
    # First entry, fully created.
    existing = make_config_entry(id_comarca=24)
    existing.add_to_hass(hass)

    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_two_different_comarques_coexist(hass: HomeAssistant) -> None:
    """Multi-entry by design: a second, different comarca is allowed."""
    osona = make_config_entry(id_comarca=24)
    osona.add_to_hass(hass)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*BARCELONA)
        )
    assert result["step_id"] == "options"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _options_input(include_sea=False)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ID_COMARCA: 13}
    assert result["options"][CONF_INCLUDE_SEA] is False
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# ---------------------------------------------------------------------------
# Options step: api_key validation and the coastal include_sea field
# ---------------------------------------------------------------------------


async def test_api_key_validated_and_stored(hass: HomeAssistant) -> None:
    """A valid key is stored in entry.data and selects the official source."""
    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            return_value=None,
        ) as mock_validate,
        aioresponses() as mocked,
    ):
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input(api_key="a-good-key")
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "a-good-key"
    mock_validate.assert_awaited_once_with(hass, "a-good-key")


async def test_invalid_api_key_reshows_options(hass: HomeAssistant) -> None:
    """A rejected key reopens the options form with the error, losing nothing."""
    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            return_value="invalid_auth",
        ),
        aioresponses() as mocked,
    ):
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input(api_key="a-bad-key")
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "options"
    assert result["errors"] == {CONF_API_KEY: "invalid_auth"}


async def test_include_sea_only_offered_for_a_coastal_comarca(
    hass: HomeAssistant,
) -> None:
    """An inland comarca never sees the maritime option; a coastal one does."""
    with aioresponses() as mocked:
        # Two flows resolve in this block, so the fixture must be repeatable.
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
            repeat=True,
        )
        inland = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        inland = await hass.config_entries.flow.async_configure(
            inland["flow_id"], _location(*VIC)
        )  # Osona, inland
        coastal = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        coastal = await hass.config_entries.flow.async_configure(
            coastal["flow_id"], _location(*BARCELONA)
        )  # Barcelonès, coastal

    assert inland["step_id"] == "options"
    assert coastal["step_id"] == "options"
    assert CONF_INCLUDE_SEA not in _field_names(inland)
    assert CONF_INCLUDE_SEA in _field_names(coastal)


# ---------------------------------------------------------------------------
# Options flow: step 2 minus api_key
# ---------------------------------------------------------------------------


async def test_options_flow_has_no_api_key(hass: HomeAssistant) -> None:
    """The options flow edits everything except the api_key."""
    entry = make_config_entry(id_comarca=24)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    fields = _field_names(result)
    assert CONF_API_KEY not in fields
    assert CONF_METEORS in fields
    assert CONF_SEVERE_THRESHOLD in fields
    assert CONF_SCAN_INTERVAL in fields
    assert CONF_INCLUDE_SEA not in fields  # Osona is inland


async def test_options_flow_updates_options(hass: HomeAssistant) -> None:
    """Submitting the options flow writes the new values onto the entry."""
    entry = make_config_entry(id_comarca=24)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 4},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_METEORS] == ["vent"]
    assert entry.options[CONF_SEVERE_THRESHOLD] == 4
    assert entry.options[CONF_SCAN_INTERVAL] is None


async def test_options_flow_coastal_offers_include_sea(hass: HomeAssistant) -> None:
    """The maritime option reappears in the options flow of a coastal comarca."""
    entry = make_config_entry(id_comarca=13)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_INCLUDE_SEA in _field_names(result)

    # Submitting persists the maritime toggle alongside the other options.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_METEORS: ["mar"], CONF_SEVERE_THRESHOLD: 2, CONF_INCLUDE_SEA: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_METEORS] == ["mar"]
    assert entry.options[CONF_INCLUDE_SEA] is True


# ---------------------------------------------------------------------------
# async_validate_api_key: the three outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (None, None),
        (ConfigEntryAuthFailed("rejected"), "invalid_auth"),
        (UpdateFailed("boom"), "cannot_connect"),
    ],
)
async def test_validate_api_key_outcomes(
    hass: HomeAssistant, raised: Exception | None, expected: str | None
) -> None:
    """A 403 is invalid_auth; any fetch failure is cannot_connect; else valid."""

    class _FakeSource:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def fetch_quota(self) -> None:
            if raised is not None:
                raise raised

    with patch.object(config_flow, "ApiKeySource", _FakeSource):
        assert await config_flow.async_validate_api_key(hass, "any-key") == expected


# ---------------------------------------------------------------------------
# Created entry actually loads (ties the config flow to async_setup_entry)
# ---------------------------------------------------------------------------


async def test_created_entry_loads(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """A config entry produced by the flow sets up and tears down cleanly.

    `quiet_source` keeps the coordinator's first refresh off the network: this
    test is about the flow-to-setup wiring, not the SMP fetch (which is covered
    by `tests/test_smp.py`), so `build_source` is patched to a quiet fake.
    """
    with aioresponses() as mocked:
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input()
        )

    entry = result["result"]
    assert entry is not None
    # A flow-created entry is set up automatically when the integration is
    # already loaded, so the entry should already be in the loaded state.
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
