"""Per-meteor sensor platform tests (docs/05-implementation-plan.md §"Task 9").

Covers the three acceptance criteria and the entity contract of
docs/03-feature-spec.md §3.5:

* criterion 1: with ``meteors: ["vent"]`` only ``avis_vent`` is created, not
  the other nine.
* criterion 2: ``graus_per_periode`` returns the four 6-hour bands of the
  current day, filtered to this meteor alone.
* criterion 3: an unknown meteor value in the options creates no entity and
  leaves a warning in the log.

The sensors are exercised both directly (built against a refreshed coordinator)
and through the config-entry state machine, so the platform setup path and the
per-entity read paths are both covered. No real network, no real clock.
"""

from __future__ import annotations

import logging

import pytest
from custom_components.avisoscat.const import CONF_METEORS
from custom_components.avisoscat.models import Meteor, NivellPerill
from custom_components.avisoscat.sensor import (
    _METEOR_TRANSLATION_KEYS,
    MeteorSensor,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    FakeClock,
    FakeSource,
    afectacio_raw,
    episodi_raw,
    evolucio_raw,
    make_config_entry,
    make_snapshot,
)

# All translation keys the per-meteor platform is supposed to expose, in
# `Meteor` enum order. The exact set is the contract: one key per known meteor,
# no more, no less (docs/03-feature-spec.md §3.5).
EXPECTED_TRANSLATION_KEYS: set[str] = {
    "warning_wind",
    "warning_rain_30min",
    "warning_rain_3h",
    "warning_rain_accumulated",
    "warning_snow",
    "warning_sea",
    "warning_cold",
    "warning_heat",
    "warning_night_heat",
    "warning_violent_weather",
}


# ---------------------------------------------------------------------------
# Raw payload builders specific to this module
# ---------------------------------------------------------------------------


def _wind_in_force_today(*, perill: float = 3.0) -> object:
    """A wind warning in force in today's `12-18` band.

    The clock fixture starts at 12:00 UTC, which sits inside the 12-18 band, so
    this affectation reads as in force.
    """
    return make_snapshot(
        [episodi_raw([evolucio_raw({"12-18": [afectacio_raw(perill=perill)]})])]
    )


def _two_meteors_in_force_today(*, vent: float = 3.0, calor: float = 4.0) -> object:
    """A wind and a heat warning both in force in today's `12-18` band.

    Used to prove `graus_per_periode` filters by meteor: the wind sensor's grid
    carries only the wind grade, the heat sensor's only the heat grade.
    """
    return make_snapshot(
        [
            episodi_raw(
                [evolucio_raw({"12-18": [afectacio_raw(perill=vent, llindar="vent")]})],
            ),
            episodi_raw(
                [
                    evolucio_raw(
                        {"12-18": [afectacio_raw(perill=calor, llindar="calor")]}
                    )
                ],
                meteor="Calor",
            ),
        ]
    )


def _wind_for_tomorrow(*, perill: float = 4.0) -> object:
    """A wind warning whose only affectation is tomorrow's `12-18` band.

    Tomorrow is `dies_per_endavant=1` from the clock's today, so the wind is
    announced, not in force: the meteor sensor stays `cap` but its
    `graus_per_periode` still paints today as empty.
    """
    tomorrow = "2026-08-06T00:00Z"
    return make_snapshot(
        [
            episodi_raw(
                [
                    evolucio_raw(
                        {"12-18": [afectacio_raw(perill=perill, dia=tomorrow)]},
                        dia=tomorrow,
                    )
                ]
            )
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_meteor_sensor(
    hass: HomeAssistant,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    snapshot: object,
    *,
    meteor: Meteor = Meteor.VENT,
) -> MeteorSensor:
    """Refresh a coordinator on one snapshot and return a single meteor sensor.

    The coordinator is built directly (no config-entry state machine) so the
    entity reads exactly the projections the snapshot produces. The fake clock
    is patched onto the sensor module for consistency with the other sensor
    tests.
    """
    coord, _source = make_coordinator(clock, [snapshot])
    monkeypatch.setattr("custom_components.avisoscat.sensor.utcnow", clock)
    await coord.async_refresh()
    await hass.async_block_till_done()
    entry = make_config_entry()
    return MeteorSensor(coord, entry, meteor)


# ---------------------------------------------------------------------------
# Static contract: the translation-key table covers exactly the ten meteors
# ---------------------------------------------------------------------------


def test_meteor_translation_keys_cover_all_ten_meteors() -> None:
    """One translation key per meteor, and exactly the documented set."""
    assert set(_METEOR_TRANSLATION_KEYS) == set(Meteor)
    assert set(_METEOR_TRANSLATION_KEYS.values()) == EXPECTED_TRANSLATION_KEYS


# ---------------------------------------------------------------------------
# Criterion 1: with `meteors: ["vent"]` only `avis_vent` is created
# ---------------------------------------------------------------------------


async def test_only_selected_meteor_creates_sensor(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """`meteors: ["vent"]` creates only the wind sensor, not the other nine.

    Exercises the full platform setup path: the options drive which
    `MeteorSensor` instances `_meteor_sensors` produces, and only those reach
    the entity registry.
    """
    entry = make_config_entry(options={CONF_METEORS: ["vent"]})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    sensors = [e for e in entities if e.domain == "sensor"]
    # Seven aggregate sensors plus exactly one per-meteor sensor.
    assert len(sensors) == 8
    meteor_keys = {
        _METEOR_TRANSLATION_KEYS[Meteor.VENT],
    }
    for entity in sensors:
        for key in meteor_keys:
            if entity.unique_id.endswith(f"_{key}"):
                break
        else:
            continue
    # The wind translation key is present.
    wind_key = _METEOR_TRANSLATION_KEYS[Meteor.VENT]
    wind = [e for e in sensors if e.unique_id.endswith(f"_{wind_key}")]
    assert len(wind) == 1
    # No other meteor sensor leaked in.
    other_keys = EXPECTED_TRANSLATION_KEYS - {_METEOR_TRANSLATION_KEYS[Meteor.VENT]}
    for key in other_keys:
        assert not any(e.unique_id.endswith(f"_{key}") for e in sensors)


async def test_default_options_create_all_ten_meteor_sensors(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """Missing `meteors` in options defaults to following all ten.

    The config-flow default (docs/03-feature-spec.md §2) is "all meteors"; a
    config entry that predates the per-meteor option, or one where the user
    never touched the multi-select, must produce all ten sensors on top of the
    seven aggregates.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    sensors = [e for e in entities if e.domain == "sensor"]
    # Seven aggregates plus ten per-meteor.
    assert len(sensors) == 17
    seen_keys: set[str] = set()
    for entity in sensors:
        for key in EXPECTED_TRANSLATION_KEYS:
            if entity.unique_id.endswith(f"_{key}"):
                seen_keys.add(key)
                break
    assert seen_keys == EXPECTED_TRANSLATION_KEYS


# ---------------------------------------------------------------------------
# Criterion 2: graus_per_periode returns the four bands of the current day
# ---------------------------------------------------------------------------


async def test_graus_per_periode_returns_four_bands(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`graus_per_periode` carries the four 6-hour UTC bands of today.

    Only the band with a live wind warning carries its grade; the other three
    stay at zero. The set of keys is exactly the four canonical bands.
    """
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=3.0)
    )

    graus = sensor.extra_state_attributes["graus_per_periode"]
    assert set(graus) == {"00-06", "06-12", "12-18", "18-00"}
    assert graus["00-06"] == 0
    assert graus["06-12"] == 0
    assert graus["12-18"] == 3
    assert graus["18-00"] == 0


async def test_graus_per_periode_filters_by_meteor(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-meteor grid carries only that meteor's grade per band.

    With wind at grade 3 and heat at grade 4 in the same band, the wind sensor's
    `graus_per_periode` shows 3 and the heat sensor's shows 4: the grid never
    folds the other meteor in, which is the point of a per-meteor sensor.
    """
    snapshot = _two_meteors_in_force_today(vent=3.0, calor=4.0)
    vent = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, snapshot, meteor=Meteor.VENT
    )
    calor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, snapshot, meteor=Meteor.CALOR
    )

    assert vent.extra_state_attributes["graus_per_periode"]["12-18"] == 3
    assert calor.extra_state_attributes["graus_per_periode"]["12-18"] == 4


async def test_graus_per_periode_all_zero_when_no_warning_for_meteor(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meteor with no in-force warning still has a full zero grid.

    The grid paints the day ahead for the meteor regardless of whether a warning
    is live, so the attribute is always present with four cells once the
    coordinator has data.
    """
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )

    graus = sensor.extra_state_attributes["graus_per_periode"]
    assert set(graus) == {"00-06", "06-12", "12-18", "18-00"}
    assert all(v == 0 for v in graus.values())


# ---------------------------------------------------------------------------
# Criterion 3: an unknown meteor creates no entity and warns
# ---------------------------------------------------------------------------


async def test_unknown_meteor_in_options_creates_no_entity_and_warns(
    hass: HomeAssistant,
    quiet_source: FakeSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrecognised meteor value is skipped with a warning, never raised.

    The options store meteor *values* (plain strings). A value that does not
    map to a `Meteor` enum member - a name the source sent that the parser did
    not recognise, or a stale literal from an older entry - must not produce a
    sensor and must not crash setup. The warning is what surfaces the skip.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.avisoscat.sensor")
    entry = make_config_entry(options={CONF_METEORS: ["vent", "unknown_meteor"]})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # The unknown meteor surfaced a warning naming it.
    assert any(
        "unknown_meteor" in record.getMessage().lower() for record in caplog.records
    )

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    sensors = [e for e in entities if e.domain == "sensor"]
    # Seven aggregates plus the one recognised meteor (`vent`), nothing for the
    # unknown one.
    assert len(sensors) == 8
    # No entity whose unique id hints at the unknown value.
    assert not any("unknown" in e.unique_id.lower() for e in sensors)


# ---------------------------------------------------------------------------
# Auxiliary: the per-meteor sensor state follows the in-force peak
# ---------------------------------------------------------------------------


async def test_meteor_sensor_state_cap_when_no_in_force_warning(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No in-force wind warning leaves the wind sensor on `cap`."""
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )

    assert sensor.native_value == NivellPerill.CAP.value
    # The peak-anchored attributes are absent; the grid still paints the day.
    attrs = sensor.extra_state_attributes
    assert "perill" not in attrs
    assert "graus_per_periode" in attrs


async def test_meteor_sensor_state_follows_in_force_peak(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wind sensor's state is the category of the in-force wind peak."""
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=4.0)
    )

    assert sensor.native_value == NivellPerill.ALT.value


async def test_meteor_sensor_attributes_anchored_to_in_force_peak(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §3.5 peak attributes mirror §3.1 for this meteor's in-force peak."""
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=4.0)
    )

    attrs = sensor.extra_state_attributes
    assert attrs["perill"] == 4
    assert attrs["nivell"] == 1
    assert attrs["periode"] == "12-18"
    assert attrs["llindar"] == "Ratxa màxima > 108 km/h (30 m/s)"
    assert attrs["distribucio_geografica"] == "EXTENSA"
    assert attrs["data_inici"] == "2026-08-04T12:00:00+00:00"
    assert attrs["data_fi"] == "2026-08-06T23:59:00+00:00"


async def test_meteor_sensor_announced_warning_keeps_state_cap(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wind warning issued for tomorrow leaves the wind sensor on `cap`.

    The per-meteor sensor reads the in-force projection like §3.1: an announced
    warning is not yet in force, so the state stays `cap`. The grid still shows
    today as empty (the warning is tomorrow).
    """
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, _wind_for_tomorrow(perill=4.0)
    )

    assert sensor.native_value == NivellPerill.CAP.value
    attrs = sensor.extra_state_attributes
    assert "perill" not in attrs
    graus = attrs["graus_per_periode"]
    assert all(v == 0 for v in graus.values())


# ---------------------------------------------------------------------------
# Auxiliary: ENUM device class and translation key wiring
# ---------------------------------------------------------------------------


async def test_meteor_sensor_is_enum_with_translation_key(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each per-meteor sensor is an ENUM with the four states and its own key."""
    sensor = await _build_meteor_sensor(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )

    assert sensor.device_class is SensorDeviceClass.ENUM
    assert set(sensor.options) == {
        NivellPerill.CAP.value,
        NivellPerill.MODERAT.value,
        NivellPerill.ALT.value,
        NivellPerill.MOLT_ALT.value,
    }
    assert sensor.translation_key == "warning_wind"


# ---------------------------------------------------------------------------
# Auxiliary: device info and unique id keyed by translation key
# ---------------------------------------------------------------------------


async def test_meteor_sensor_wires_device_info_and_unique_id(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-meteor sensor carries the device and a unique id keyed by meteor."""
    entry = make_config_entry()
    coord, _source = make_coordinator(clock, [make_snapshot()])
    monkeypatch.setattr("custom_components.avisoscat.sensor.utcnow", clock)
    await coord.async_refresh()
    await hass.async_block_till_done()
    sensor = MeteorSensor(coord, entry, Meteor.VENT)

    assert sensor.unique_id == f"{entry.entry_id}_warning_wind"
    assert sensor.device_info is not None
    assert sensor.device_info["name"] == "Avisos Meteocat — Osona"


# ---------------------------------------------------------------------------
# Auxiliary: before the first refresh everything reads as `cap`
# ---------------------------------------------------------------------------


def test_meteor_sensor_returns_cap_before_first_refresh(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the first refresh the meteor sensor reports `cap` without raising."""
    coord, _source = make_coordinator(clock, [make_snapshot()])
    monkeypatch.setattr("custom_components.avisoscat.sensor.utcnow", clock)
    assert coord.data is None
    entry = make_config_entry()
    sensor = MeteorSensor(coord, entry, Meteor.VENT)

    assert sensor.native_value == NivellPerill.CAP.value
    # No peak, no grid: both the in-force list and the outlook are empty before
    # the first fetch.
    assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Auxiliary: empty meteor list creates no meteor sensors
# ---------------------------------------------------------------------------


async def test_empty_meteors_creates_no_meteor_sensors(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """`meteors: []` creates the seven aggregates and zero meteor sensors."""
    entry = make_config_entry(options={CONF_METEORS: []})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    sensors = [e for e in entities if e.domain == "sensor"]
    assert len(sensors) == 7
    for entity in sensors:
        assert not any(
            entity.unique_id.endswith(f"_{key}") for key in EXPECTED_TRANSLATION_KEYS
        )


# ---------------------------------------------------------------------------
# Auxiliary: the platform setup forwards the coordinator's entities correctly
# ---------------------------------------------------------------------------


async def test_async_setup_entry_creates_meteor_sensors_for_all_selected(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """A multi-meteor selection creates one sensor per selected meteor."""
    meteors = ["vent", "neu", "calor"]
    entry = make_config_entry(options={CONF_METEORS: meteors})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    sensors = [e for e in entities if e.domain == "sensor"]
    # Seven aggregates plus three per-meteor.
    assert len(sensors) == 10
    expected_keys = {
        _METEOR_TRANSLATION_KEYS[Meteor.VENT],
        _METEOR_TRANSLATION_KEYS[Meteor.NEU],
        _METEOR_TRANSLATION_KEYS[Meteor.CALOR],
    }
    seen_keys: set[str] = set()
    for entity in sensors:
        for key in expected_keys:
            if entity.unique_id.endswith(f"_{key}"):
                seen_keys.add(key)
                break
    assert seen_keys == expected_keys
