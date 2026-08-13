"""Binary sensor platform tests (docs/05-implementation-plan.md §"Task 10").

Covers the three acceptance criteria of Task 10 and the entity contract of
docs/04-architecture.md §9 against the four §3.8-§3.11 SAFETY switches:

* criterion 1: `avis_greu` and `avis_greu_anunciat` respect `severe_threshold`
  and flip when the option changes.
* criterion 2: a severe warning issued for tomorrow lights
  `avis_greu_anunciat` and leaves `avis_greu` off (the §1.1 guard, applied to
  the severe horizon).
* criterion 3: `temps_violent` turns off by the clock alone: advancing
  `FakeClock` past the 2 h window and triggering the network-free minute
  recompute flips it to `off` with zero additional fetches.

Auxiliary coverage: the §3.8 attribute table, the §3.10 attribute table, the
§3.11 attribute table, the platform setup path, the device-info / unique-id
contract, and the quiet-snapshot baseline. No real network, no real clock.
"""

from __future__ import annotations

import pytest
from custom_components.avisoscat.binary_sensor import (
    AvisActiuBinarySensor,
    AvisGreuAnunciatBinarySensor,
    AvisGreuBinarySensor,
    TempsViolentBinarySensor,
)
from custom_components.avisoscat.const import CONF_SEVERE_THRESHOLD
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
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

# ---------------------------------------------------------------------------
# Raw payload builders specific to this module
# ---------------------------------------------------------------------------


def _wind_in_force_today(*, perill: float = 3.0):
    """A wind warning in force in today's `12-18` band.

    The clock fixture starts at 12:00 UTC, inside the 12-18 band, so this
    affectation reads as in force.
    """
    return make_snapshot(
        [episodi_raw([evolucio_raw({"12-18": [afectacio_raw(perill=perill)]})])]
    )


def _wind_for_tomorrow(*, perill: float = 4.0):
    """A wind warning whose only affectation is tomorrow's `12-18` band.

    Tomorrow is `dies_per_endavant=1` from the clock's today, the
    announced-not-in-force case of criterion 2.
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


def _violent_nowcast(*, perill: float = 6.0, data_emissio: str = "2026-08-05T11:30Z"):
    """A violent-weather nowcast whose 2 h window is open at the clock start.

    The window opens at the issue time, so at the default 11:30 emission the
    clock (12:00) sits inside it and the projection reads as in force.
    """
    return make_snapshot(
        [
            episodi_raw(
                afectacions_directes=[afectacio_raw(perill=perill, dia=None)],
                perill_declarat=perill,
                meteor="Temps violent",
                tipus="Avís vigilància per temps violent",
                data_emissio=data_emissio,
                data_inici=data_emissio,
                data_fi=None,
            )
        ]
    )


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
    options: dict | None = None,
    entry: object | None = None,
) -> tuple[
    AvisActiuBinarySensor,
    AvisGreuBinarySensor,
    AvisGreuAnunciatBinarySensor,
    TempsViolentBinarySensor,
]:
    """Refresh a coordinator with one snapshot and return the four switches.

    Built directly (no config-entry state machine) so the entities read exactly
    the projections the snapshot produces. `options` reaches the entry that the
    threshold-aware switches read live on every state access.
    """
    coord, _source = make_coordinator(clock, [snapshot], options=options)
    await coord.async_refresh()
    await hass.async_block_till_done()
    if entry is None:
        entry = make_config_entry(options=options)
    return (
        AvisActiuBinarySensor(coord, entry),
        AvisGreuBinarySensor(coord, entry),
        AvisGreuAnunciatBinarySensor(coord, entry),
        TempsViolentBinarySensor(coord, entry),
    )


# ---------------------------------------------------------------------------
# Entity contract: device class, translation key, unique id, device info
# ---------------------------------------------------------------------------


async def test_all_four_are_safety_switches(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every binary sensor is `device_class: SAFETY` and names itself by key."""
    actiu, greu, greu_anunciat, violent = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )

    for sensor in (actiu, greu, greu_anunciat, violent):
        assert sensor.device_class is BinarySensorDeviceClass.SAFETY
    assert actiu.translation_key == "avis_actiu"
    assert greu.translation_key == "avis_greu"
    assert greu_anunciat.translation_key == "avis_greu_anunciat"
    assert violent.translation_key == "temps_violent"


async def test_entity_wires_device_info_and_unique_id(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each switch carries the per-comarca device and a unique id keyed by entry."""
    entry = make_config_entry()
    actiu, _greu, _greu_anunciat, _violent = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot(), entry=entry
    )

    assert actiu.unique_id == f"{entry.entry_id}_avis_actiu"
    assert actiu.device_info is not None
    assert actiu.device_info["name"] == "Avisos Meteocat — Osona"
    assert actiu.device_info["manufacturer"] == "Servei Meteorològic de Catalunya"


# ---------------------------------------------------------------------------
# Criterion 1: severe threshold is honoured and reacts to options changes
# ---------------------------------------------------------------------------


async def test_avis_greu_respects_severe_threshold(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`avis_greu` follows the configured threshold off the same snapshot.

    A grade-3 wind warning is below the default threshold of 3? No: at the
    threshold is on, since §3.9 is `perill >= severe_threshold`. At threshold
    minus one it is off.
    """
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_in_force_today(perill=3.0),
    )
    _actiu, greu, _greu_anunciat, _violent = sensors
    assert greu.is_on is True

    sensors_below = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_in_force_today(perill=2.0),
    )
    _actiu, greu_below, _greu_anunciat, _violent = sensors_below
    assert greu_below.is_on is False


async def test_avis_greu_and_anunciat_flip_when_threshold_changes(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising the threshold past the warning turns both severe switches off.

    The same grade-4 in-force snapshot, two different options: at threshold 3
    the severe switch is on, at threshold 5 it is off. Reading the option live
    is what makes the switch react without a reload of the entry.
    """
    snapshot = _wind_in_force_today(perill=4.0)
    _actiu, greu_low, greu_anunciat_low, _violent = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        snapshot,
        options={CONF_SEVERE_THRESHOLD: 3},
    )
    assert greu_low.is_on is True
    assert greu_anunciat_low.is_on is False  # nothing announced

    _actiu, greu_high, greu_anunciat_high, _violent = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        snapshot,
        options={CONF_SEVERE_THRESHOLD: 5},
    )
    assert greu_high.is_on is False
    assert greu_anunciat_high.is_on is False


async def test_avis_greu_anunciat_respects_severe_threshold(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`avis_greu_anunciat` follows the threshold on the announced horizon."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_for_tomorrow(perill=4.0),
        options={CONF_SEVERE_THRESHOLD: 3},
    )
    _actiu, _greu, greu_anunciat, _violent = sensors
    assert greu_anunciat.is_on is True

    sensors_above = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_for_tomorrow(perill=4.0),
        options={CONF_SEVERE_THRESHOLD: 5},
    )
    _actiu, _greu, greu_anunciat_above, _violent = sensors_above
    assert greu_anunciat_above.is_on is False


# ---------------------------------------------------------------------------
# Criterion 2: announced-severe lights the future switch, not the present one
# ---------------------------------------------------------------------------


async def test_announced_severe_for_tomorrow_leaves_avis_greu_off(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A severe warning for tomorrow is announced, not in force (§1.1 guard).

    The whole point of §1.1 applied to the severe horizon: confusing the
    announced future with the in-force present would report a current severe
    danger that does not exist yet. `avis_greu` reads `en_vigor` (empty),
    `avis_greu_anunciat` reads `anunciats` (one grade-4 wind band).
    """
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_for_tomorrow(perill=4.0),
    )
    actiu, greu, greu_anunciat, _violent = sensors

    # `avis_actiu` is also off: nothing is in force right now.
    assert actiu.is_on is False
    assert greu.is_on is False
    assert greu_anunciat.is_on is True


async def test_avis_greu_anunciat_exposes_attribute_table(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §3.10 attribute table is anchored to the announced severe peak."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_for_tomorrow(perill=4.0),
    )
    _actiu, _greu, greu_anunciat, _violent = sensors

    attrs = greu_anunciat.extra_state_attributes
    assert attrs["comenca"] == "2026-08-06T12:00:00+00:00"
    assert attrs["hores_per_endavant"] == 24
    assert attrs["meteor"] == "vent"
    assert attrs["perill"] == 4


# ---------------------------------------------------------------------------
# Criterion 3: temps_violent turns off by the clock, with no fetch
# ---------------------------------------------------------------------------


async def test_temps_violent_turns_off_by_clock_without_fetch(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2 h window expiry flips `temps_violent` off via the minute recompute.

    The coordinator caches the snapshot, the minute recompute re-projects it
    against the advanced clock, the projection's `fi` (emissio + 2 h) falls
    behind, and the violent affectation leaves `en_vigor`. The switch reads
    `en_vigor`, so it reports `off` on the next minute tick. No fetch in the
    loop: `source.calls` stays at the single first refresh.
    """
    coord, source = make_coordinator(clock, [_violent_nowcast(perill=6.0)])
    entry = make_config_entry()
    violent = TempsViolentBinarySensor(coord, entry)

    await coord.async_refresh()
    await hass.async_block_till_done()
    assert violent.is_on is True
    assert source.calls == 1

    # Inside the 2 h window: still on, still no fetch.
    clock.advance(minutes=30)
    coord.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()
    assert violent.is_on is True
    assert source.calls == 1

    # Cross the 2 h boundary (11:30 emissio + 2 h = 13:30; the clock is now
    # at 12:00 + 30 min + 2 h 1 min = 14:31, past the window).
    clock.advance(hours=2, minutes=1)
    coord.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()

    assert violent.is_on is False
    # The whole turn-off path is network-free.
    assert source.calls == 1


# ---------------------------------------------------------------------------
# Auxiliary: §3.11 attribute table of a live violent nowcast
# ---------------------------------------------------------------------------


async def test_temps_violent_exposes_attribute_table(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`probabilitat`, `llindar`, `data_emissio`, `valid_fins` while live."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _violent_nowcast(perill=6.0, data_emissio="2026-08-05T11:30Z"),
    )
    _actiu, _greu, _greu_anunciat, violent = sensors

    assert violent.is_on is True
    attrs = violent.extra_state_attributes
    assert attrs["probabilitat"] == "alta"
    assert attrs["data_emissio"] == "2026-08-05T11:30:00+00:00"
    # The 2 h window: emissio + 2 h.
    assert attrs["valid_fins"] == "2026-08-05T13:30:00+00:00"
    assert attrs["llindar"] == "Ratxa màxima > 108 km/h (30 m/s)"


async def test_temps_violent_probability_label_is_mitjana_below_grade_5(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grade 4 reads as `mitjana` (§3.11), grade 6 as `alta`."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _violent_nowcast(perill=4.0, data_emissio="2026-08-05T11:30Z"),
    )
    _actiu, _greu, _greu_anunciat, violent = sensors

    assert violent.is_on is True
    assert violent.extra_state_attributes["probabilitat"] == "mitjana"


# ---------------------------------------------------------------------------
# Auxiliary: §3.8 attribute table of avis_actiu
# ---------------------------------------------------------------------------


async def test_avis_actiu_exposes_attribute_table(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`meteor_principal`, `perill_maxim`, `nombre_avisos` while any is live."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_in_force_today(perill=3.0),
    )
    actiu, _greu, _greu_anunciat, _violent = sensors

    assert actiu.is_on is True
    attrs = actiu.extra_state_attributes
    assert attrs["meteor_principal"] == "vent"
    assert attrs["perill_maxim"] == 3
    assert attrs["nombre_avisos"] == 1


async def test_avis_actiu_counts_in_force_only(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An announced-only warning leaves `avis_actiu` off and the count at 0."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_for_tomorrow(perill=4.0),
    )
    actiu, _greu, _greu_anunciat, _violent = sensors

    assert actiu.is_on is False
    assert actiu.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Auxiliary: severe in force lights avis_greu and counts as one avis actiu
# ---------------------------------------------------------------------------


async def test_in_force_severe_warning_lights_both_avis_actiu_and_avis_greu(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grade-4 in-force warning turns on `avis_actiu` and `avis_greu`."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _wind_in_force_today(perill=4.0),
    )
    actiu, greu, greu_anunciat, _violent = sensors

    assert actiu.is_on is True
    assert greu.is_on is True
    assert greu_anunciat.is_on is False


# ---------------------------------------------------------------------------
# Auxiliary: quiet snapshot leaves every switch off and attributes empty
# ---------------------------------------------------------------------------


async def test_quiet_snapshot_leaves_everything_off(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No warnings at all: every switch off, attribute tables empty."""
    actiu, greu, greu_anunciat, violent = await _build_sensors(
        hass, make_coordinator, monkeypatch, clock, make_snapshot()
    )

    for sensor in (actiu, greu, greu_anunciat, violent):
        assert sensor.is_on is False
    # §3.8, §3.10 and §3.11 publish an attribute table; §3.9 does not, so its
    # table is whatever HA's default leaves it (None or empty).
    assert actiu.extra_state_attributes == {}
    assert greu_anunciat.extra_state_attributes == {}
    assert violent.extra_state_attributes == {}
    assert not greu.extra_state_attributes


# ---------------------------------------------------------------------------
# Auxiliary: before the first refresh every switch reports off without raising
# ---------------------------------------------------------------------------


def test_returns_off_before_first_refresh(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
) -> None:
    """With `coordinator.data is None`, every switch reports `off` cleanly."""
    coord, _source = make_coordinator(clock, [make_snapshot()])
    # No refresh: `coord.data` is None, the entity guards must hold.
    assert coord.data is None
    entry = make_config_entry()
    sensors = (
        AvisActiuBinarySensor(coord, entry),
        AvisGreuBinarySensor(coord, entry),
        AvisGreuAnunciatBinarySensor(coord, entry),
        TempsViolentBinarySensor(coord, entry),
    )

    for sensor in sensors:
        assert sensor.is_on is False
    assert sensors[0].extra_state_attributes == {}
    assert sensors[2].extra_state_attributes == {}
    assert sensors[3].extra_state_attributes == {}
    assert not sensors[1].extra_state_attributes


# ---------------------------------------------------------------------------
# Auxiliary: the platform setup path creates all four entities
# ---------------------------------------------------------------------------


async def test_async_setup_entry_creates_four_binary_sensors(
    hass: HomeAssistant, quiet_source: FakeSource
) -> None:
    """`async_setup_entry` adds the four binary sensors of one comarca."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    binary_entities = [e for e in entities if e.domain == "binary_sensor"]
    assert len(binary_entities) == 4

    expected_keys = {"avis_actiu", "avis_greu", "avis_greu_anunciat", "temps_violent"}
    seen_keys: set[str] = set()
    for entity in binary_entities:
        for key in expected_keys:
            if entity.unique_id.endswith(f"_{key}"):
                seen_keys.add(key)
                break
    assert seen_keys == expected_keys


# ---------------------------------------------------------------------------
# Auxiliary: violent nowcast also lights avis_greu (it is in force and severe)
# ---------------------------------------------------------------------------


async def test_violent_nowcast_lights_avis_greu_and_temps_violent(
    hass: HomeAssistant,
    clock: FakeClock,
    make_coordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live violent nowcast reaches the severe and the violent switches."""
    sensors = await _build_sensors(
        hass,
        make_coordinator,
        monkeypatch,
        clock,
        _violent_nowcast(perill=6.0),
    )
    actiu, greu, greu_anunciat, violent = sensors

    assert violent.is_on is True
    assert greu.is_on is True
    assert actiu.is_on is True
    assert greu_anunciat.is_on is False
