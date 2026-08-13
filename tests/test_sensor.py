"""Level sensor platform tests (docs/05-implementation-plan.md §"Task 8").

Covers the five acceptance criteria and the entity contract of
docs/04-architecture.md §9:

* criterion 1: `nivell_d_avis` and `avis_anunciat` are `ENUM` sensors with the
  four traffic-light options.
* criterion 2: the §1.1 design-error guard. A warning issued for tomorrow
  leaves `nivell_d_avis` on `cap` and moves `avis_anunciat` to its grade.
* criterion 3: `avis_anunciat` exposes `comenca`, `hores_per_endavant`, `dia`.
* criterion 4: `grau_maxim_dema.graella` has exactly the four bands of the day.
* criterion 5: `avisos_actius` is `state_class: MEASUREMENT`.

The sensors are exercised directly against a coordinator that has refreshed on
one snapshot, so the platform setup path and the per-entity read paths are both
covered. No real network, no real clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from custom_components.avisoscat.models import (
    NivellPerill,
    compute_payload_hash,
    parse_snapshot,
)
from custom_components.avisoscat.sensor import (
    DIA_AVUI,
    DIA_DEMA,
    DIA_DEMA_PASSAT,
    NIVELL_OPTIONS,
    AvisAnunciatSensor,
    AvisosActiusSensor,
    GrauMaximSensor,
    NivellDAvisSensor,
    PreavisSensor,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
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

OPTIONS_EXPECTED: list[str] = [
    NivellPerill.CAP.value,
    NivellPerill.MODERAT.value,
    NivellPerill.ALT.value,
    NivellPerill.MOLT_ALT.value,
]


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


def _wind_for_tomorrow(*, perill: float = 4.0) -> object:
    """A wind warning whose only affectation is tomorrow's `12-18` band.

    Tomorrow is `dies_per_endavant=1` from the clock's today, so this is the
    announced-not-in-force scenario of criterion 2.
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


def _preavis_raw(
    *,
    perill: float = 4.0,
    estat: str = "Vigent",
    data_fi: str = "2026-08-07T23:59Z",
    meteor: str = "Vent",
    nivell: float = 2.0,
) -> dict:
    """A raw pre-warning at the Catalonia scale, ready for `make_snapshot`."""
    return {
        "tipus": "Preavís",
        "estat": estat,
        "perill": perill,
        "nivell": nivell,
        "llindar": "Ratxa màxima > 90 km/h (25 m/s)",
        "comentari": "Ratxes al litoral.",
        "dataEmisio": "2026-08-05T10:00Z",
        "dataInici": "2026-08-06T12:00Z",
        "dataFi": data_fi,
        "meteor": {"idMeteor": None, "nom": meteor},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_sensors(
    hass: HomeAssistant,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    snapshot: object,
    *,
    entry: object | None = None,
) -> tuple[
    NivellDAvisSensor,
    AvisosActiusSensor,
    AvisAnunciatSensor,
    GrauMaximSensor,
    GrauMaximSensor,
    GrauMaximSensor,
    PreavisSensor,
]:
    """Refresh a coordinator with one snapshot and return the seven sensors.

    The coordinator is built directly (no config-entry state machine) so the
    entities read exactly the projections the snapshot produces. The fake clock
    is patched onto the sensor module too, so `PreavisSensor` (the only sensor
    that re-evaluates against `now` rather than reading a projection) sees the
    same time the coordinator did.

    Pass an `entry` when the test needs to assert against `unique_id` or
    `device_info`; otherwise a fresh one is built and discarded.
    """
    coord, _source = make_coordinator(clock, [snapshot])
    monkeypatch.setattr("custom_components.avisoscat.sensor.utcnow", clock)
    await coord.async_refresh()
    await hass.async_block_till_done()
    if entry is None:
        entry = make_config_entry()
    return (
        NivellDAvisSensor(coord, entry),
        AvisosActiusSensor(coord, entry),
        AvisAnunciatSensor(coord, entry),
        GrauMaximSensor(coord, entry, DIA_AVUI),
        GrauMaximSensor(coord, entry, DIA_DEMA),
        GrauMaximSensor(coord, entry, DIA_DEMA_PASSAT),
        PreavisSensor(coord, entry),
    )


# ---------------------------------------------------------------------------
# Criterion 1: ENUM sensors with the four traffic-light options
# ---------------------------------------------------------------------------


async def test_enum_sensors_have_four_traffic_light_options(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nivell_d_avis` and `avis_anunciat` are ENUM with the documented options."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )
    nivell, _actius, anunciat, *_ = sensors

    assert nivell.device_class is SensorDeviceClass.ENUM
    assert anunciat.device_class is SensorDeviceClass.ENUM
    assert list(nivell.options) == OPTIONS_EXPECTED
    assert list(anunciat.options) == OPTIONS_EXPECTED
    assert tuple(OPTIONS_EXPECTED) == NIVELL_OPTIONS


# ---------------------------------------------------------------------------
# Criterion 2: the §1.1 design-error guard
# ---------------------------------------------------------------------------


async def test_announced_warning_for_tomorrow_keeps_today_clear(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warning for tomorrow is announced, not in force.

    The whole point of §1.1: confusing the announced horizon with the in-force
    one would report a current danger that does not exist yet. `nivell_d_avis`
    reads the in-force projection (empty here), `avis_anunciat` reads the
    announced one (alt at grade 4).
    """
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_for_tomorrow(perill=4.0)
    )
    nivell, _actius, anunciat, *_ = sensors

    assert nivell.native_value == NivellPerill.CAP.value
    assert anunciat.native_value == NivellPerill.ALT.value


# ---------------------------------------------------------------------------
# Criterion 3: avis_anunciat exposes comenca / hores_per_endavant / dia
# ---------------------------------------------------------------------------


async def test_avis_anunciat_exposes_comenca_hores_dia(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The announced-warning attribute table is anchored to the announced peak."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_for_tomorrow(perill=4.0)
    )
    _nivell, _actius, anunciat, *_ = sensors

    attrs = anunciat.extra_state_attributes
    # The 12-18 band of 2026-08-06 starts at 12:00 UTC; the clock is at
    # 2026-08-05 12:00 UTC, exactly 24 h earlier; tomorrow is one day ahead.
    assert attrs["comenca"] == "2026-08-06T12:00:00+00:00"
    assert attrs["hores_per_endavant"] == 24
    assert attrs["dia"] == "dema"
    assert attrs["perill"] == 4
    assert attrs["periode"] == "12-18"


# ---------------------------------------------------------------------------
# Criterion 4: grau_maxim_dema.graella has exactly the four bands
# ---------------------------------------------------------------------------


async def test_grau_maxim_dema_graella_has_four_bands(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graella attribute always carries the four 6-hour UTC bands."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_for_tomorrow(perill=4.0)
    )
    _nivell, _actius, _anunciat, _avui, dema, _dema_passat, _preavis = sensors

    graella = dema.extra_state_attributes["graella"]
    assert set(graella) == {"00-06", "06-12", "12-18", "18-00"}
    assert graella["00-06"] == 0
    assert graella["06-12"] == 0
    assert graella["12-18"] == 4
    assert graella["18-00"] == 0

    # The day-level peak follows the graella: alt at grade 4.
    assert dema.native_value == NivellPerill.ALT.value


# ---------------------------------------------------------------------------
# Criterion 5: avisos_actius is state_class MEASUREMENT
# ---------------------------------------------------------------------------


async def test_avisos_actius_is_measurement_and_counts_in_force(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The active count sensor is a MEASUREMENT and reports the in-force count."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=3.0)
    )
    _nivell, actius, *_ = sensors

    assert actius.state_class is SensorStateClass.MEASUREMENT
    assert actius.native_unit_of_measurement == "avisos"
    assert actius.native_value == 1

    avisos = actius.extra_state_attributes["avisos"]
    assert len(avisos) == 1
    assert avisos[0]["meteor"] == "vent"
    assert avisos[0]["perill"] == 3
    assert avisos[0]["periode"] == "12-18"
    assert avisos[0]["llindar"] == "Ratxa màxima > 108 km/h (30 m/s)"


# ---------------------------------------------------------------------------
# Auxiliary: in-force nivell_d_avis attributes (§3.1 table)
# ---------------------------------------------------------------------------


async def test_nivell_d_avis_exposes_in_force_attributes(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §3.1 attribute table is anchored to the in-force peak."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=4.0)
    )
    nivell, actius, _anunciat, *_rest = sensors

    assert nivell.native_value == NivellPerill.ALT.value
    attrs = nivell.extra_state_attributes
    assert attrs["perill"] == 4
    assert attrs["meteor"] == "vent"
    assert attrs["tipus"] == "avis"
    assert attrs["periode"] == "12-18"
    assert attrs["nivell"] == 1
    assert attrs["llindar"] == "Ratxa màxima > 108 km/h (30 m/s)"
    assert attrs["distribucio_geografica"] == "EXTENSA"
    assert attrs["data_inici"] == "2026-08-04T12:00:00+00:00"
    assert attrs["data_fi"] == "2026-08-06T23:59:00+00:00"
    assert attrs["data_emissio"] == "2026-08-04T15:30:00+00:00"
    # `actius` is consistent with the in-force count.
    assert actius.native_value == 1


# ---------------------------------------------------------------------------
# Auxiliary: a quiet snapshot leaves every level on `cap` / 0
# ---------------------------------------------------------------------------


async def test_quiet_snapshot_leaves_everything_cap(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No warnings at all: levels `cap`, count `0`, empty lists."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )
    nivell, actius, anunciat, avui, dema, dema_passat, preavis = sensors

    assert nivell.native_value == NivellPerill.CAP.value
    assert nivell.extra_state_attributes == {}
    assert actius.native_value == 0
    assert actius.extra_state_attributes == {"avisos": []}
    assert anunciat.native_value == NivellPerill.CAP.value
    assert anunciat.extra_state_attributes == {}
    for grau in (avui, dema, dema_passat):
        assert grau.native_value == NivellPerill.CAP.value
        # The graella still has the four bands, all zero.
        graella = grau.extra_state_attributes["graella"]
        assert set(graella) == {"00-06", "06-12", "12-18", "18-00"}
        assert all(value == 0 for value in graella.values())
    assert preavis.native_value == NivellPerill.CAP.value
    assert preavis.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Auxiliary: preavis sensor reads the Catalonia-scale pre-warnings (§3.6)
# ---------------------------------------------------------------------------


async def test_preavis_sensor_reports_severest_open_preavis(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The severest open pre-warning drives the preavis sensor."""
    snapshot = make_snapshot(
        [],
        preavisos=[_preavis_raw(perill=2.0), _preavis_raw(perill=4.0, meteor="Calor")],
    )
    sensors = await _build_sensors(hass, make_coordinator, monkeypatch, clock, snapshot)
    preavis = sensors[-1]

    assert preavis.native_value == NivellPerill.ALT.value
    attrs = preavis.extra_state_attributes
    assert attrs["perill"] == 4
    assert attrs["meteor"] == "calor"
    assert attrs["llindar"] == "Ratxa màxima > 90 km/h (25 m/s)"
    assert attrs["data_inici"] == "2026-08-06T12:00:00+00:00"
    assert attrs["data_fi"] == "2026-08-07T23:59:00+00:00"
    assert attrs["comentari"] == "Ratxes al litoral."


async def test_preavis_sensor_ignores_closed_preavis(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed pre-warning is dropped by `preavisos_actius`."""
    snapshot = make_snapshot([], preavisos=[_preavis_raw(estat="Tancat")])
    sensors = await _build_sensors(hass, make_coordinator, monkeypatch, clock, snapshot)
    preavis = sensors[-1]

    assert preavis.native_value == NivellPerill.CAP.value
    assert preavis.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Auxiliary: the per-day outlook matches the day label
# ---------------------------------------------------------------------------


async def test_grau_maxim_today_follows_in_force_warning(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warning in force today lifts `grau_maxim_avui` to its grade."""
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, _wind_in_force_today(perill=3.0)
    )
    _nivell, _actius, _anunciat, avui, _dema, _dema_passat, _preavis = sensors

    assert avui.native_value == NivellPerill.ALT.value
    attrs = avui.extra_state_attributes
    assert attrs["meteor"] == "vent"
    assert attrs["periode"] == "12-18"
    assert attrs["nivell"] == 1
    assert attrs["llindar"] == "Ratxa màxima > 108 km/h (30 m/s)"
    assert attrs["graella"]["12-18"] == 3


# ---------------------------------------------------------------------------
# Auxiliary: device info, unique id, translation key
# ---------------------------------------------------------------------------


async def test_entity_wires_device_info_and_unique_id(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sensor carries the per-comarca device and a unique id keyed by entry."""
    entry = make_config_entry()
    sensors = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot(), entry=entry
    )
    nivell, _actius, _anunciat, avui, _dema, _dema_passat, _preavis = sensors

    assert nivell.unique_id == f"{entry.entry_id}_warning_level"
    assert nivell.device_info is not None
    assert nivell.device_info["name"] == "Avisos Meteocat — Osona"
    assert nivell.device_info["manufacturer"] == "Servei Meteorològic de Catalunya"
    assert nivell.translation_key == "warning_level"
    assert avui.translation_key == "max_grade_today"
    assert avui.unique_id == f"{entry.entry_id}_max_grade_today"


# ---------------------------------------------------------------------------
# Auxiliary: the platform setup path creates all seven entities
# ---------------------------------------------------------------------------


async def test_async_setup_entry_creates_seven_sensors(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """`async_setup_entry` adds the seven aggregate level sensors of one comarca.

    The per-meteor sensors (§3.5) are created on top of these seven and have
    their own creation tests; this one isolates the aggregates by deselecting
    every meteor, so the count stays at seven.
    """
    entry = make_config_entry(options={"meteors": []})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    # The binary_sensor platform also forwards under the same entry, so the
    # sensor count is the slice whose domain is `sensor`.
    sensors = [e for e in entities if e.domain == "sensor"]
    assert len(sensors) == 7

    # unique_id is `{entry_id}_{translation_key}`; the translation keys are the
    # stable contract, so they are what the assertion is anchored against.
    expected_keys = {
        "warning_level",
        "active_warnings",
        "announced_warning",
        "max_grade_today",
        "max_grade_tomorrow",
        "max_grade_day_after",
        "prewarning",
    }
    # `unique_id` is `{entry_id}_{translation_key}`, and translation keys
    # themselves contain underscores, so each unique_id is matched by suffix.
    seen_keys: set[str] = set()
    for entity in sensors:
        for key in expected_keys:
            if entity.unique_id.endswith(f"_{key}"):
                seen_keys.add(key)
                break
    assert seen_keys == expected_keys


# ---------------------------------------------------------------------------
# End to end on the real capture: what the state machine publishes
# ---------------------------------------------------------------------------

CAPTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "captures"
    / "smp-episodis-oberts-2026-08-05.json"
)

# The entity ids Home Assistant derives for Osona from the Catalan names, which
# is what a dashboard or an automation is written against.
NIVELL_ID = "sensor.avisos_meteocat_osona_nivell_d_avis"
ACTIUS_ID = "sensor.avisos_meteocat_osona_avisos_actius"
ANUNCIAT_ID = "sensor.avisos_meteocat_osona_avis_anunciat"
AVUI_ID = "sensor.avisos_meteocat_osona_grau_maxim_avui"
DEMA_ID = "sensor.avisos_meteocat_osona_grau_maxim_dema"


async def _load_capture(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> tuple[object, FakeSource]:
    """Load an Osona entry whose source serves the real captured payload.

    Goes through the config-entry state machine, so the assertions read the
    published states rather than the entity objects: that is the surface a
    dashboard, an automation and the states developer tool see. The fake clock
    is patched onto the coordinator and the sensor module, so validity is
    decided by the test's wall clock and never by the real one.
    """
    raw = json.loads(CAPTURE.read_text(encoding="utf-8"))
    snapshot = parse_snapshot(raw, None, payload_hash=compute_payload_hash(raw, None))
    source = FakeSource([snapshot])
    monkeypatch.setattr("custom_components.avisoscat.coordinator.utcnow", clock)
    monkeypatch.setattr("custom_components.avisoscat.sensor.utcnow", clock)
    monkeypatch.setattr(
        "custom_components.avisoscat.coordinator.build_source",
        lambda hass, entry: source,
    )
    # Catalan is the reference language, so the published names and entity ids
    # are the Catalan ones.
    hass.config.language = "ca"
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, source


async def test_published_states_split_announced_from_in_force(
    hass: HomeAssistant, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §1.1 guard as the user reads it, on the real captured payload.

    The captured warning's only remaining affectation for Osona is the `12-18`
    band of the 6th, so at 12:00 on the 5th the published `nivell_d_avis` is
    `cap` and `avis_anunciat` carries the grade, 24 hours ahead. `tests/
    test_vigencia.py` pins the same case at the projection layer; this one pins
    what reaches the state machine, translated name included.
    """
    await _load_capture(hass, monkeypatch, clock)

    nivell = hass.states.get(NIVELL_ID)
    anunciat = hass.states.get(ANUNCIAT_ID)
    actius = hass.states.get(ACTIUS_ID)
    dema = hass.states.get(DEMA_ID)
    assert nivell is not None and anunciat is not None
    assert actius is not None and dema is not None

    assert nivell.state == NivellPerill.CAP.value
    assert nivell.attributes["friendly_name"] == "Avisos Meteocat — Osona Nivell d'avís"
    assert nivell.attributes["options"] == OPTIONS_EXPECTED
    assert nivell.attributes["device_class"] == SensorDeviceClass.ENUM

    assert anunciat.state == NivellPerill.ALT.value
    assert anunciat.attributes["comenca"] == "2026-08-06T12:00:00+00:00"
    assert anunciat.attributes["hores_per_endavant"] == 24
    assert anunciat.attributes["dia"] == "dema"
    assert anunciat.attributes["meteor"] == "pluja_30min"

    assert actius.state == "0"
    assert actius.attributes["avisos"] == []
    assert actius.attributes["state_class"] == SensorStateClass.MEASUREMENT

    assert dema.state == NivellPerill.ALT.value
    assert dema.attributes["graella"] == {
        "00-06": 0,
        "06-12": 0,
        "12-18": 3,
        "18-00": 0,
    }
    assert dema.attributes["perill"] == 3


async def test_published_states_follow_the_band_without_fetching(
    hass: HomeAssistant, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The band opening while Home Assistant runs moves the published states.

    Validity is a function of the wall clock, so the minute recompute has to
    reach the entities with no new payload: the user sees `nivell_d_avis` turn
    `alt` when the band opens and fall back to `cap` when it closes, while the
    source is fetched exactly once. The sensors stay `available` throughout,
    proving the last-good-state override on `AvisoscatEntity` is wired.
    """
    clock.now = datetime(2026, 8, 6, 11, 59, tzinfo=UTC)
    entry, source = await _load_capture(hass, monkeypatch, clock)
    coordinator = entry.runtime_data

    assert hass.states.get(NIVELL_ID).state == NivellPerill.CAP.value
    assert hass.states.get(ANUNCIAT_ID).state == NivellPerill.ALT.value

    # The 12-18 band of the 6th opens.
    clock.advance(minutes=2)
    coordinator.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()

    assert hass.states.get(NIVELL_ID).state == NivellPerill.ALT.value
    assert hass.states.get(ACTIUS_ID).state == "1"
    assert hass.states.get(ACTIUS_ID).attributes["avisos"] == [
        {
            "meteor": "pluja_30min",
            "perill": 3,
            "tipus": "avis",
            "periode": "12-18",
            "llindar": "Intensitat > 20 mm / 30 minuts",
        }
    ]
    # Nothing is announced any more: the announced horizon emptied as the
    # affectation moved into force.
    assert hass.states.get(ANUNCIAT_ID).state == NivellPerill.CAP.value

    # The warning's own dataFi is 17:59 on the 6th, so the band closes there.
    clock.advance(hours=6)
    coordinator.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()

    assert hass.states.get(NIVELL_ID).state == NivellPerill.CAP.value
    assert hass.states.get(ACTIUS_ID).state == "0"
    # The day's outlook keeps the peak: the band is over, the day still had it.
    assert hass.states.get(AVUI_ID).attributes["perill"] == 3
    assert source.calls == 1


# ---------------------------------------------------------------------------
# Auxiliary: coordinator.data is None before the first refresh
# ---------------------------------------------------------------------------


def test_sensor_returns_cap_before_first_refresh(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the first refresh every sensor reports `cap`/0 without raising."""
    coord, _source = make_coordinator(clock, [make_snapshot()])
    # No refresh: `coord.data` is None, the entity guards must hold.
    assert coord.data is None
    entry = make_config_entry()
    sensors = (
        NivellDAvisSensor(coord, entry),
        AvisosActiusSensor(coord, entry),
        AvisAnunciatSensor(coord, entry),
        GrauMaximSensor(coord, entry, DIA_AVUI),
        PreavisSensor(coord, entry),
    )

    assert sensors[0].native_value == NivellPerill.CAP.value
    assert sensors[0].extra_state_attributes == {}
    assert sensors[1].native_value == 0
    assert sensors[1].extra_state_attributes == {"avisos": []}
    assert sensors[2].native_value == NivellPerill.CAP.value
    assert sensors[3].native_value == NivellPerill.CAP.value
    assert sensors[3].extra_state_attributes == {}
    assert sensors[4].native_value == NivellPerill.CAP.value
    assert sensors[4].extra_state_attributes == {}
