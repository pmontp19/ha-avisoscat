"""Tests for the avisoscat options, reauth and reconfigure flows.

Covers the three acceptance criteria behind the options/reauth/reconfigure work:

* changing the options reloads the entry and lets the per-meteor sensors follow,
* a `403` from the official source surfaces as a reauth flow in the UI,
* `async_step_reconfigure` moves the entry to a different comarca without
  touching its `unique_id` (entity history is preserved that way).

The reauth and reconfigure handlers are reached through the standard
`pytest_homeassistant_custom_component` helpers (`start_reauth_flow`,
`MockConfigEntry.start_reconfigure_flow`) so the context HA itself builds for
those sources is reproduced. The API-key validation seam is shared with
`tests/test_config_flow.py`: `async_validate_api_key` is patched so no flow
exercises the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.avisoscat import config_flow
from custom_components.avisoscat.const import (
    CONF_API_KEY,
    CONF_ID_COMARCA,
    CONF_INCLUDE_SEA,
    CONF_METEORS,
    CONF_SCAN_INTERVAL,
    CONF_SEVERE_THRESHOLD,
    DOMAIN,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    start_reauth_flow,
)

from .conftest import FakeSource, make_config_entry

# Osona (24) is inland; Barcelonès (13) and Baix Camp (8) are coastal. The
# comarca ids come from `comarques.py` and are pinned here so a test that
# changes comarca is not at the mercy of the dropdown ordering.
ID_OSONA = 24
ID_BARCELONES = 13
ID_BAIX_CAMP = 8

ALL_METEORS = ["vent", "pluja_30min", "pluja_3h", "pluja_acumulada", "neu", "mar"]


def _field_names(result: dict[str, Any]) -> set[str]:
    """Pull the field names out of a form result's voluptuous schema."""
    return {field.schema for field in result["data_schema"].schema}


def _field_default(result: dict[str, Any], name: str) -> Any:
    """Resolve a form field's default, calling voluptuous' factory when needed."""
    field = next(f for f in result["data_schema"].schema if f.schema == name)
    default = field.default
    return default() if callable(default) else default


def _entry_with_api_key(
    id_comarca: int, api_key: str, *, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Build a `MockConfigEntry` whose `data` carries an API key.

    Modern `ConfigEntry` forbids assigning `entry.data` after the fact, so the
    key is part of the constructor payload instead. Reused by every reauth test
    that needs an entry that "came in with a key".
    """
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(id_comarca),
        data={CONF_ID_COMARCA: id_comarca, CONF_API_KEY: api_key},
        options=options
        if options is not None
        else {CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 3},
        title=f"Avisos Meteocat — comarca {id_comarca}",
    )


# ---------------------------------------------------------------------------
# Options flow: form, submission, reload behaviour
# ---------------------------------------------------------------------------


async def test_options_flow_form_is_prefilled_with_current_options(
    hass: HomeAssistant,
) -> None:
    """The options form opens with the values currently on the entry."""
    entry = make_config_entry(
        id_comarca=ID_OSONA,
        options={
            CONF_METEORS: ["vent"],
            CONF_SEVERE_THRESHOLD: 4,
            CONF_SCAN_INTERVAL: 20,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    # The api_key never appears in the options flow.
    assert CONF_API_KEY not in _field_names(result)
    # Defaults come from entry.options, not from the schema-wide defaults.
    assert _field_default(result, CONF_METEORS) == ["vent"]
    assert _field_default(result, CONF_SEVERE_THRESHOLD) == 4


async def test_options_flow_does_not_reload_when_entry_not_loaded(
    hass: HomeAssistant,
) -> None:
    """An entry that never set up does not get reloaded from its options flow.

    `make_config_entry().add_to_hass(hass)` leaves the entry in NOT_LOADED, so a
    user opening Options for an entry that failed to set up must not be punished
    with a forced setup attempt that would mask the original failure behind an
    unrelated network call.
    """
    entry = make_config_entry(id_comarca=ID_OSONA)
    entry.add_to_hass(hass)
    assert entry.state is ConfigEntryState.NOT_LOADED

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 4},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_METEORS] == ["vent"]
    assert entry.options[CONF_SEVERE_THRESHOLD] == 4
    # No reload was issued: the entry was not loaded in the first place.
    mock_reload.assert_not_called()


async def test_options_flow_reloads_loaded_entry(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """Changing the options reloads a loaded entry so its entities adjust.

    This is the first acceptance criterion: a loaded entry must be torn down and
    set up again with the new options, which is what rebuilds the per-meteor
    sensors against the new selection (the sensor-side wiring lands in a later
    task; the reload is the contract here).
    """
    entry = make_config_entry(
        id_comarca=ID_OSONA,
        options={
            CONF_METEORS: ["vent"],
            CONF_SEVERE_THRESHOLD: 3,
            CONF_SCAN_INTERVAL: None,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    reload_mock = AsyncMock(return_value=True)
    with patch.object(hass.config_entries, "async_reload", new=reload_mock):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_METEORS: ["vent", "neu"], CONF_SEVERE_THRESHOLD: 5},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_METEORS] == ["vent", "neu"]
    assert entry.options[CONF_SEVERE_THRESHOLD] == 5
    assert entry.options[CONF_SCAN_INTERVAL] is None
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_coastal_writes_include_sea(
    hass: HomeAssistant,
) -> None:
    """The maritime toggle is persisted for a coastal comarca."""
    entry = make_config_entry(
        id_comarca=ID_BARCELONES,
        options={CONF_METEORS: ["mar"], CONF_SEVERE_THRESHOLD: 2},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_INCLUDE_SEA in _field_names(result)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_METEORS: ["mar"], CONF_SEVERE_THRESHOLD: 2, CONF_INCLUDE_SEA: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_INCLUDE_SEA] is True


async def test_options_flow_keeps_adaptive_interval_as_none(
    hass: HomeAssistant,
) -> None:
    """A blank scan interval stays `None` (adaptive), not 0 or a default."""
    entry = make_config_entry(id_comarca=ID_OSONA)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 3},
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_SCAN_INTERVAL] is None


# ---------------------------------------------------------------------------
# Reauth: the 403 path
# ---------------------------------------------------------------------------


async def test_reauth_flow_shows_confirm_form_with_old_key_prefilled(
    hass: HomeAssistant,
) -> None:
    """`async_step_reauth` opens a focused form with the previous key as default."""
    entry = _entry_with_api_key(ID_OSONA, "the-old-key")
    entry.add_to_hass(hass)

    result = await start_reauth_flow(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert CONF_API_KEY in _field_names(result)
    assert _field_default(result, CONF_API_KEY) == "the-old-key"


async def test_reauth_flow_accepts_a_new_key_and_reloads(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """A valid new key overwrites `entry.data` and reloads the entry.

    This is the second acceptance criterion: the user-facing recovery from a 403
    is the reauth flow, and a successful submit must both persist the new key
    and revive the entry so the coordinator rebuilds the source with it.
    """
    entry = _entry_with_api_key(ID_OSONA, "the-old-key")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    reload_mock = AsyncMock(return_value=True)
    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            new=AsyncMock(return_value=None),
        ) as mock_validate,
        patch.object(hass.config_entries, "async_reload", new=reload_mock),
    ):
        result = await start_reauth_flow(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "the-new-key"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "the-new-key"
    mock_validate.assert_awaited_once_with(hass, "the-new-key")
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_reauth_flow_rejects_a_blank_key(hass: HomeAssistant) -> None:
    """Reauth fixes a rejected key, so blank is sent back, not accepted.

    Dropping back to the keyless source is a different action (delete and
    re-add): reauth is for getting the official source working again, so an
    empty submit is `api_key_required` and the entry is left untouched.
    """
    entry = _entry_with_api_key(ID_OSONA, "the-old-key")
    entry.add_to_hass(hass)

    with patch(
        "custom_components.avisoscat.config_flow.async_validate_api_key",
        new=AsyncMock(return_value=None),
    ) as mock_validate:
        result = await start_reauth_flow(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: ""}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {CONF_API_KEY: "api_key_required"}
    # The validator never ran: blank is caught before validation.
    mock_validate.assert_not_awaited()
    assert entry.data[CONF_API_KEY] == "the-old-key"


async def test_reauth_flow_rejects_an_invalid_key(hass: HomeAssistant) -> None:
    """A key the server still rejects reopens the form with `invalid_auth`."""
    entry = _entry_with_api_key(ID_OSONA, "the-old-key")
    entry.add_to_hass(hass)

    with patch(
        "custom_components.avisoscat.config_flow.async_validate_api_key",
        new=AsyncMock(return_value="invalid_auth"),
    ):
        result = await start_reauth_flow(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "another-bad-key"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_KEY: "invalid_auth"}
    assert entry.data[CONF_API_KEY] == "the-old-key"


async def test_reauth_flow_propagates_cannot_connect_as_form_error(
    hass: HomeAssistant,
) -> None:
    """A network failure during validation is shown as `cannot_connect`."""
    entry = _entry_with_api_key(ID_OSONA, "the-old-key")
    entry.add_to_hass(hass)

    with patch(
        "custom_components.avisoscat.config_flow.async_validate_api_key",
        new=AsyncMock(return_value="cannot_connect"),
    ):
        result = await start_reauth_flow(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "any-key"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_KEY: "cannot_connect"}


async def test_coordinator_403_starts_a_reauth_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `ConfigEntryAuthFailed` from the coordinator opens reauth in the UI.

    End-to-end check of the second acceptance criterion: the coordinator already
    lets `ConfigEntryAuthFailed` propagate (so HA offers reauth), and this test
    pins that the flow does in fact appear as a progress entry a user can click
    on. The fake source stands in for an `ApiKeySource` that just got a 403.
    """
    failing = FakeSource(error=ConfigEntryAuthFailed("403 Forbidden"))
    monkeypatch.setattr(
        "custom_components.avisoscat.coordinator.build_source",
        lambda hass, entry: failing,
    )

    entry = _entry_with_api_key(ID_OSONA, "the-bad-key")
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # HA translated the auth failure into a reauth flow against this entry.
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    reauth = [f for f in flows if f["context"]["source"] == SOURCE_REAUTH]
    assert reauth, "A 403 from the coordinator should open a reauth flow"
    assert reauth[0]["context"]["entry_id"] == entry.entry_id
    # The failed entry is parked in SETUP_ERROR while waiting for the new key:
    # `ConfigEntryAuthFailed` during initial setup marks the entry as failed,
    # which is also what makes the reauth flow appear in the UI for the user.
    assert entry.state is ConfigEntryState.SETUP_ERROR


# ---------------------------------------------------------------------------
# Reconfigure: moving an entry to a different comarca
# ---------------------------------------------------------------------------


async def test_reconfigure_flow_shows_comarca_dropdown(
    hass: HomeAssistant,
) -> None:
    """The reconfigure form is the same comarca dropdown as the initial fallback."""
    entry = make_config_entry(id_comarca=ID_OSONA)
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert CONF_ID_COMARCA in _field_names(result)


async def test_reconfigure_changes_comarca_and_preserves_unique_id(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """The comarca moves; the `unique_id` is deliberately left untouched.

    This is the third acceptance criterion: entities whose `unique_id` derives
    from the entry's keep their state history across the move, so the title and
    `entry.data[CONF_ID_COMARCA]` follow the new comarca but `unique_id` does not.
    """
    entry = make_config_entry(id_comarca=ID_OSONA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original_unique_id = entry.unique_id
    original_entry_id = entry.entry_id

    reload_mock = AsyncMock(return_value=True)
    with patch.object(hass.config_entries, "async_reload", new=reload_mock):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ID_COMARCA: str(ID_BARCELONES)}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # The comarca and the title followed the new selection...
    assert entry.data[CONF_ID_COMARCA] == ID_BARCELONES
    assert entry.title == "Avisos Meteocat — Barcelonès"
    # ...but the unique id (and therefore entity identity) is preserved.
    assert entry.unique_id == original_unique_id
    assert entry.entry_id == original_entry_id
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_reconfigure_strips_include_sea_when_target_is_inland(
    hass: HomeAssistant,
) -> None:
    """A left-over `include_sea=True` is dropped when moving to an inland comarca."""
    entry = make_config_entry(
        id_comarca=ID_BARCELONES,
        options={
            CONF_METEORS: ["mar"],
            CONF_SEVERE_THRESHOLD: 2,
            CONF_INCLUDE_SEA: True,
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ID_COMARCA: str(ID_OSONA)}
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ID_COMARCA] == ID_OSONA
    assert CONF_INCLUDE_SEA not in entry.options


async def test_reconfigure_defaults_include_sea_when_target_is_coastal(
    hass: HomeAssistant,
) -> None:
    """Moving inland to coastal defaults `include_sea` to `False`.

    The coastal option was never on the inland entry's options, so the reconfigure
    helper applies the same default the initial setup would have applied, instead
    of leaving the field missing and the maritime sensor failing to load.
    """
    entry = make_config_entry(
        id_comarca=ID_OSONA,
        options={CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 3},
    )
    entry.add_to_hass(hass)
    assert CONF_INCLUDE_SEA not in entry.options

    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ID_COMARCA: str(ID_BAIX_CAMP)}
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ID_COMARCA] == ID_BAIX_CAMP
    assert entry.options[CONF_INCLUDE_SEA] is False


async def test_reconfigure_preserves_other_options(
    hass: HomeAssistant,
) -> None:
    """Meteors, threshold and scan interval survive a comarca move."""
    entry = make_config_entry(
        id_comarca=ID_OSONA,
        options={
            CONF_METEORS: ["vent", "neu"],
            CONF_SEVERE_THRESHOLD: 5,
            CONF_SCAN_INTERVAL: 25,
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ID_COMARCA: str(ID_BARCELONES)}
    )
    await hass.async_block_till_done()

    # `include_sea` is added by the reconcile helper; the rest is unchanged.
    assert entry.options[CONF_METEORS] == ["vent", "neu"]
    assert entry.options[CONF_SEVERE_THRESHOLD] == 5
    assert entry.options[CONF_SCAN_INTERVAL] == 25
    assert entry.options[CONF_INCLUDE_SEA] is False


# ---------------------------------------------------------------------------
# OptionsFlow handler: explicit config_entry wiring (no self.config_entry)
# ---------------------------------------------------------------------------


async def test_async_get_options_flow_passes_config_entry(
    hass: HomeAssistant,
) -> None:
    """`async_get_options_flow` hands the entry to the options flow handler.

    The entry is wired through the constructor so the flow never reaches for the
    base-class `self.config_entry` shortcut, which is the contract the task
    pins. A round-trip through the public API proves the wiring is live.
    """
    entry = make_config_entry(
        id_comarca=ID_OSONA,
        options={CONF_METEORS: ["vent"], CONF_SEVERE_THRESHOLD: 3},
    )
    entry.add_to_hass(hass)

    handler = config_flow.AvisoscatConfigFlow.async_get_options_flow(entry)
    assert isinstance(handler, config_flow.AvisoscatOptionsFlow)
    assert handler._config_entry is entry


def test_reconcile_options_strips_and_defaults_include_sea() -> None:
    """The comarca-change reconcile helper is a pure function of the target id.

    Unit-tested in isolation because the reconfigure integration test only
    exercises one direction at a time; this pins both branches plus the
    no-op behaviour for the comarca-independent options.
    """
    # Inland target strips a left-over include_sea.
    stripped = config_flow._reconcile_options_for_comarca(
        ID_OSONA, {CONF_INCLUDE_SEA: True, CONF_METEORS: ["mar"]}
    )
    assert CONF_INCLUDE_SEA not in stripped
    assert stripped[CONF_METEORS] == ["mar"]

    # Coastal target defaults include_sea when it was absent.
    defaulted = config_flow._reconcile_options_for_comarca(
        ID_BARCELONES, {CONF_METEORS: ["vent"]}
    )
    assert defaulted[CONF_INCLUDE_SEA] is False

    # Coastal target keeps an explicit include_sea.
    kept = config_flow._reconcile_options_for_comarca(
        ID_BARCELONES, {CONF_INCLUDE_SEA: True, CONF_METEORS: ["mar"]}
    )
    assert kept[CONF_INCLUDE_SEA] is True
