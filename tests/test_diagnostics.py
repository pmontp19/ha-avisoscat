"""Diagnostics redaction: `latitude`, `longitude` and `api_key` never leak.

The diagnostic download exists because the source is not an officially
supported API (docs/03-feature-spec.md §3.12), so a bug report without a
payload is usually useless. That same payload is what makes redaction
non-negotiable: the user's location and the optional Meteocat key must never
leave their instance (docs/04-architecture.md §11). This module proves the
redaction holds across the three shapes the diagnostic ships.

The runtime behaviour (failure counters, degraded event, repair issue) lives
in `test_resilience.py`; this file is the data-leak gate, and it stays
focused on it.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from custom_components.avisoscat.const import DOMAIN
from custom_components.avisoscat.coordinator import AvisoscatDataUpdateCoordinator
from custom_components.avisoscat.diagnostics import (
    TO_REDACT,
    _quota_band_label,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.avisoscat.models import SmpSnapshot
from custom_components.avisoscat.smp import ApiKeySource, QuotaInfo
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import ID_COMARCA_OSONA, FakeClock, FakeSource, make_config_entry

# The string the redactor replaces sensitive values with.
_REDACTED = "**REDACTED**"


def _entry_with_credentials(
    *,
    api_key: str | None,
    latitude: float = 41.69,
    longitude: float = 2.17,
) -> MockConfigEntry:
    """A config entry carrying the keys diagnostics must redact.

    `api_key` lives in `entry.data` (the documented place). `latitude` and
    `longitude` are not real avisoscat fields, but `async_redact_data` walks
    nested dicts: putting them under `entry.data` and under the runtime state
    proves the redaction catches them wherever a future field name happens to
    land them.
    """
    data: dict[str, Any] = {"id_comarca": ID_COMARCA_OSONA}
    if api_key is not None:
        data["api_key"] = api_key
    data["latitude"] = latitude
    data["longitude"] = longitude
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(ID_COMARCA_OSONA),
        data=data,
        options={"severe_threshold": 3},
    )


async def _setup_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, *, api_key: str | None
) -> MockConfigEntry:
    """Wire a config entry with a quiet source so setup succeeds, no network.

    Returns the entry after setup so the test can hand it to the diagnostics
    functions. The `FakeSource` is patched in for `build_source`, exactly the
    way the test setup smoke tests do it.
    """
    fake = FakeSource([SmpSnapshot()])
    monkeypatch.setattr(
        "custom_components.avisoscat.coordinator.build_source",
        lambda hass, entry: fake,
    )
    entry = _entry_with_credentials(api_key=api_key)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _assert_no_sensitive(value: Any) -> None:
    """Recursively assert that no nested dict or list carries a sensitive key.

    The redactor walks the whole structure, so this is the negative side of
    the contract: there must be no `latitude`, `longitude` or `api_key` key
    whose value escaped redaction, at any depth.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if key in TO_REDACT:
                # Empty strings and None are passed through by the redactor,
                # which is the documented behaviour: only present values are
                # sensitive. A non-empty value that survived is the leak.
                if child in (None, ""):
                    continue
                assert child == _REDACTED, f"unredacted sensitive value at {key}"
            else:
                _assert_no_sensitive(child)
    elif isinstance(value, list):
        for item in value:
            _assert_no_sensitive(item)


# ---------------------------------------------------------------------------
# Criterion 3a: config-entry diagnostics redact everything they should
# ---------------------------------------------------------------------------


async def test_config_entry_diagnostics_redacts_credentials(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config-entry download never exposes `api_key`, `latitude`, `longitude`."""
    entry = await _setup_entry(hass, monkeypatch, api_key="s3cret-meteocat-key")

    data = await async_get_config_entry_diagnostics(hass, entry)

    text = json.dumps(data, default=str)
    # The headline guarantee: the literal sensitive values are nowhere in the
    # serialised output, not even under a different key or as a substring.
    assert "s3cret-meteocat-key" not in text
    assert "41.69" not in text
    assert "2.17" not in text
    # The recursive walk confirms every sensitive key's value is REDACTED.
    _assert_no_sensitive(data)
    # And the redacted placeholder is actually present, three times, so the
    # redaction did not silently drop the keys either.
    assert text.count(_REDACTED) == 3


async def test_config_entry_diagnostics_redacts_without_an_api_key(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless entry still redacts `latitude` and `longitude`."""
    entry = await _setup_entry(hass, monkeypatch, api_key=None)

    data = await async_get_config_entry_diagnostics(hass, entry)

    _assert_no_sensitive(data)
    text = json.dumps(data, default=str)
    assert "41.69" not in text
    assert "2.17" not in text
    # `api_key` was absent, so the redactor's pass-through of `None` left it
    # alone; the two coordinates are the redactions that count.
    assert text.count(_REDACTED) == 2


async def test_config_entry_diagnostics_shape(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagnostic shape carries the runtime state needed to triage a bug.

    The exact shape is what bug reports diff against, so it is part of the
    contract: the entry, the coordinator's failure counters and projections,
    and the source kind. None of this should fetch.
    """
    entry = await _setup_entry(hass, monkeypatch, api_key="some-key")

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert set(data.keys()) == {"config_entry", "coordinator", "source"}
    assert data["config_entry"]["data"]["id_comarca"] == ID_COMARCA_OSONA
    # The resilience counters are exposed even on a healthy coordinator, so a
    # diagnostic taken mid-outage shows where in the streak the source is.
    assert data["coordinator"]["consecutive_failures"] == 0
    assert data["coordinator"]["degraded_announced"] is False
    assert data["source"]["kind"] == "FakeSource"


async def test_config_entry_diagnostics_reports_quota_band(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API-key source with a citizen quota surfaces the band in diagnostics.

    The coordinator reads the quota once and pins the cadence to 8 h; the
    diagnostic must show that band so a bug report explains why the user is
    on a slow     cadence without the reader having to ask.
    """

    class _CitizenApiKeySource(ApiKeySource):
        async def fetch(self):  # type: ignore[override]
            return SmpSnapshot()

        async def fetch_quota(self):  # type: ignore[override]
            return QuotaInfo(
                plan_nom="dades de predicció",
                periode=None,
                max_consultes=100,
                consultes_restants=90,
                consultes_realitzades=10,
            )

    entry = make_config_entry()
    entry.add_to_hass(hass)
    clock = FakeClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("custom_components.avisoscat.coordinator.utcnow", clock)
    coord = AvisoscatDataUpdateCoordinator(
        hass,
        entry,
        _CitizenApiKeySource(None, "key"),  # type: ignore[arg-type]
    )
    entry.runtime_data = coord
    await coord.async_refresh()
    await hass.async_block_till_done()

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["source"]["quota_band"] == "<= 200 (citizen)"
    assert data["source"]["quota_interval_seconds"] == 8 * 60 * 60
    # The same redaction guarantee holds: an API key, had the entry carried
    # one in `entry.data`, would have been redacted.
    _assert_no_sensitive(data)


# ---------------------------------------------------------------------------
# Criterion 3b: device-level diagnostics share the same redaction
# ---------------------------------------------------------------------------


async def test_device_diagnostics_redact_credentials(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device download inherits the config-entry redaction."""
    entry = await _setup_entry(hass, monkeypatch, api_key="s3cret-meteocat-key")

    # The device that `async_get_device_diagnostics` expects is the one the
    # integration registered at setup; the test only needs a `DeviceEntry`
    # with the right identifier for the cross-check the function does.
    device_registry = hass.data["device_registry"]
    devices = list(device_registry.devices.values())
    assert devices, "setup should have registered the per-comarca device"
    device = devices[0]

    data = await async_get_device_diagnostics(hass, entry, device)

    _assert_no_sensitive(data)
    text = json.dumps(data, default=str)
    assert "s3cret-meteocat-key" not in text
    assert "41.69" not in text
    assert "2.17" not in text
    # The device view augments the entry view: same redactions, plus a
    # device block that carries the comarca id for cross-checking.
    assert data["device"]["id_comarca"] == ID_COMARCA_OSONA


# ---------------------------------------------------------------------------
# Auxiliary: the redaction list is the documented one
# ---------------------------------------------------------------------------


def test_redact_set_matches_the_documented_contract() -> None:
    """Only `latitude`, `longitude` and `api_key` are sensitive (§11)."""
    assert frozenset({"latitude", "longitude", "api_key"}) == TO_REDACT


# ---------------------------------------------------------------------------
# Quota band label reverse-mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interval_seconds", "expected"),
    [
        (30 * 60, "> 500"),
        (2 * 60 * 60, "200-500"),
        (8 * 60 * 60, "<= 200 (citizen)"),
        (1, "unknown"),  # an interval outside the documented bands
    ],
)
def test_quota_band_label(interval_seconds: int, expected: str) -> None:
    """The three documented intervals map to the three spec bands.

    Anything else maps to `unknown`: the function never raises on an
    unfamiliar interval, so a future band can be added without breaking the
    diagnostic.
    """
    assert _quota_band_label(interval_seconds) == expected
