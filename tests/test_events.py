"""The six bus events of the avisoscat integration (docs/03-feature-spec.md §4).

Each event is the diff the coordinator reports between two projections of the
same feed, so the subjects are built as raw payloads through `make_snapshot()`
and driven through a `FakeSource` one cycle at a time. Validity is a function of
the clock, so every test advances the `clock` fixture rather than sleeping.

Coverage of the acceptance criteria:

- criterion 1: a new warning for tomorrow fires `avisoscat_warning_announced`
  with `hores_per_endavant` and no `started`.
- criterion 2: the band arriving in force fires `avisoscat_warning_started`
  carrying the real notice (`anunciat_amb_hores`).
- criterion 3: a grade rise fires `upgraded`, a fall `downgraded`, a
  disappearance `cleared` with a `motiu`.
- criterion 4: the same emission does not announce twice, but an ampliation
  (a fresh `data_emissio`) does.
- criterion 6: `avisoscat_violent_weather` fires once per `data_emissio`, not
  every cycle.
"""

from collections import Counter
from datetime import UTC, datetime

from custom_components.avisoscat import const as c
from homeassistant.core import Event, HomeAssistant

from .conftest import (
    FakeClock,
    afectacio_raw,
    episodi_raw,
    evolucio_raw,
    make_snapshot,
)

# All six event types, for the catch-all listener.
ALL_TYPES = (
    c.EVENT_WARNING_ANNOUNCED,
    c.EVENT_WARNING_STARTED,
    c.EVENT_WARNING_UPGRADED,
    c.EVENT_WARNING_DOWNGRADED,
    c.EVENT_WARNING_CLEARED,
    c.EVENT_VIOLENT_WEATHER,
)


def _listen(hass: HomeAssistant) -> list[Event]:
    """Collect every avisoscat event this test fires, in order."""
    caught: list[Event] = []
    for event_type in ALL_TYPES:
        hass.bus.async_listen(event_type, caught.append)
    return caught


def _types(caught: list[Event]) -> list[str]:
    """The event types that fired, in order, payload dropped."""
    return [event.event_type for event in caught]


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------


def _announced_vent(*, perill: float = 3.0, data_emissio: str = "2026-08-04T15:30Z"):
    """A wind warning for tomorrow's `12-18` band: announced at the clock start.

    The clock fixture sits at 2026-08-05 12:00 UTC, so a band opening the next
    day is strictly in the future and reads as `Horitzo.ANUNCIAT`.
    """
    dema = "2026-08-06T00:00Z"
    tomorrow = evolucio_raw(
        {"12-18": [afectacio_raw(perill=perill, dia=dema)]}, dia=dema
    )
    return make_snapshot([episodi_raw([tomorrow], data_emissio=data_emissio)])


def _late_vent(*, perill: float = 3.0, data_emissio: str = "2026-08-05T06:00Z"):
    """A wind warning for today's `18-00` band: announced at 12:00, live at 18:00."""
    band = evolucio_raw({"18-00": [afectacio_raw(perill=perill)]})
    return make_snapshot([episodi_raw([band], data_emissio=data_emissio)])


def _in_force_vent(*, perill: float = 3.0, data_emissio: str = "2026-08-05T06:00Z"):
    """A wind warning in force now: today's `12-18` band at the clock start."""
    band = evolucio_raw({"12-18": [afectacio_raw(perill=perill)]})
    return make_snapshot([episodi_raw([band], data_emissio=data_emissio)])


def _violent(*, data_emissio: str, perill: float = 6.0):
    """A violent-weather nowcast for Osona, issued at `data_emissio`.

    Uses the trap-#12 shape: affectations hang directly off the avis, and the
    grade is declared on the avis itself. Its two-hour window opens at the issue
    time, so the clock (12:00) must sit inside it for the nowcast to be in force.
    """
    return make_snapshot(
        [
            episodi_raw(
                afectacions_directes=[afectacio_raw(perill=perill, dia=None)],
                perill_declarat=perill,
                meteor="Temps violent",
                tipus="Avís vigilància per temps violent",
                data_emissio=data_emissio,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Criterion 1: a new warning for tomorrow is announced, not started
# ---------------------------------------------------------------------------


async def test_new_warning_for_tomorrow_is_announced(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A future warning fires `announced` with `hores_per_endavant`, never `started`."""
    coord, _source = make_coordinator(clock, [make_snapshot(), _announced_vent()])
    caught = _listen(hass)

    await coord.async_refresh()  # seed: empty, quiet
    await coord.async_refresh()  # the tomorrow warning appears
    await hass.async_block_till_done()

    assert _types(caught) == [c.EVENT_WARNING_ANNOUNCED]
    payload = caught[0].data
    assert payload["dia"] == "dema"
    assert payload["hores_per_endavant"] > 0
    assert payload["meteor_nom"] == "Vent"


# ---------------------------------------------------------------------------
# Criterion 2: the band arriving in force fires `started`
# ---------------------------------------------------------------------------


async def test_band_arriving_in_force_fires_started(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A warning that was announced and then enters force fires `started`."""
    coord, _source = make_coordinator(clock, [_late_vent(), _late_vent()])
    caught = _listen(hass)

    await coord.async_refresh()  # 12:00: seed, the 18-00 band is announced
    clock.advance(hours=6)  # 18:00: the 18-00 band is now in force
    await coord.async_refresh()
    await hass.async_block_till_done()

    assert _types(caught) == [c.EVENT_WARNING_STARTED]
    payload = caught[0].data
    # The real notice the warning gave, in whole hours (§4.2).
    assert payload["anunciat_amb_hores"] is not None
    assert payload["anunciat_amb_hores"] >= 0


# ---------------------------------------------------------------------------
# Criterion 3: upgrade, downgrade, clear
# ---------------------------------------------------------------------------


async def test_upgrade_downgrade_clear(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A grade rise upgrades, a fall downgrades, a disappearance clears."""
    coord, _source = make_coordinator(
        clock,
        [
            _in_force_vent(perill=2.0),  # seed: in force at grade 2
            _in_force_vent(perill=4.0),  # upgraded to 4
            _in_force_vent(perill=2.0),  # downgraded to 2
            make_snapshot(),  # cleared
        ],
    )
    caught = _listen(hass)

    await coord.async_refresh()  # seed, quiet
    await coord.async_refresh()  # upgraded 2 -> 4
    await coord.async_refresh()  # downgraded 4 -> 2
    await coord.async_refresh()  # cleared
    await hass.async_block_till_done()

    # Exactly one of each transition; bus arrival order between distinct
    # transitions is not guaranteed under back-to-back refreshes, so match the
    # multiset of types and then check each payload by what it carries.
    assert Counter(_types(caught)) == Counter(
        {
            c.EVENT_WARNING_UPGRADED: 1,
            c.EVENT_WARNING_DOWNGRADED: 1,
            c.EVENT_WARNING_CLEARED: 1,
        }
    )
    by_type = {event.event_type: event.data for event in caught}
    assert by_type[c.EVENT_WARNING_UPGRADED]["perill_anterior"] == 2
    assert by_type[c.EVENT_WARNING_UPGRADED]["perill"] == 4
    assert by_type[c.EVENT_WARNING_DOWNGRADED]["perill_anterior"] == 4
    assert by_type[c.EVENT_WARNING_DOWNGRADED]["perill"] == 2
    # The band (12-18) has not ended at 12:00, so the source withdrew it.
    assert by_type[c.EVENT_WARNING_CLEARED]["motiu"] == "retirat"
    assert by_type[c.EVENT_WARNING_CLEARED]["perill_final"] == 2


async def test_clear_at_band_end_is_expiry(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A warning that runs past its band end clears as `expirat`."""
    coord, _source = make_coordinator(
        clock,
        [
            _in_force_vent(perill=3.0),  # seed: in force in the 12-18 band
            make_snapshot(),  # cleared
        ],
    )
    caught = _listen(hass)

    await coord.async_refresh()  # seed, quiet
    clock.advance(hours=7)  # 19:00: the 12-18 band has ended
    await coord.async_refresh()  # cleared
    await hass.async_block_till_done()

    assert _types(caught) == [c.EVENT_WARNING_CLEARED]
    assert caught[0].data["motiu"] == "expirat"


# ---------------------------------------------------------------------------
# Criterion 4: idempotent announce; an ampliation re-announces
# ---------------------------------------------------------------------------


async def test_same_emission_does_not_reannounce_but_ampliacio_does(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A re-emission of the same content is silent; a fresh `data_emissio` is not."""
    coord, _source = make_coordinator(
        clock,
        [
            make_snapshot(),  # seed
            _announced_vent(data_emissio="2026-08-05T06:00Z"),  # first issue
            _announced_vent(data_emissio="2026-08-05T06:00Z"),  # same: silent
            _announced_vent(data_emissio="2026-08-05T09:00Z"),  # ampliacio
        ],
    )
    caught = _listen(hass)

    for _ in range(4):
        await coord.async_refresh()
    await hass.async_block_till_done()

    announced = [
        event.data for event in caught if event.event_type == c.EVENT_WARNING_ANNOUNCED
    ]
    assert len(announced) == 2
    # `data_emissio` is the parsed issue time in ISO form. Each emission fires
    # exactly once; the contract does not guarantee bus arrival order between
    # distinct emissions, so compare as a multiset of clock times.
    times = Counter(
        (dt.hour, dt.minute)
        for dt in (datetime.fromisoformat(a["data_emissio"]) for a in announced)
    )
    assert times == Counter({(6, 0): 1, (9, 0): 1})


# ---------------------------------------------------------------------------
# Criterion 6: violent weather fires once per issue, not per cycle
# ---------------------------------------------------------------------------


async def test_violent_weather_fires_once_per_emission(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A violent nowcast fires once per `data_emissio` and never as a `started`."""
    coord, _source = make_coordinator(
        clock,
        [
            make_snapshot(),  # seed
            _violent(data_emissio="2026-08-05T11:30Z"),  # first nowcast
            _violent(data_emissio="2026-08-05T11:30Z"),  # same: silent
            _violent(data_emissio="2026-08-05T12:00Z"),  # fresh nowcast
        ],
    )
    caught = _listen(hass)

    for _ in range(4):
        await coord.async_refresh()
    await hass.async_block_till_done()

    violent = [
        event.data for event in caught if event.event_type == c.EVENT_VIOLENT_WEATHER
    ]
    # Never routed through the generic in-force loop.
    assert all(event.event_type != c.EVENT_WARNING_STARTED for event in caught)
    assert len(violent) == 2
    # Each emission fires exactly once; bus arrival order between distinct
    # emissions is not guaranteed under back-to-back refreshes, so compare as a
    # multiset of clock times.
    times = Counter(
        (dt.hour, dt.minute)
        for dt in (datetime.fromisoformat(v["data_emissio"]) for v in violent)
    )
    assert times == Counter({(11, 30): 1, (12, 0): 1})
    assert all(v["probabilitat"] == "alta" for v in violent)  # grade 6


async def test_violent_weather_probability_label_follows_the_grade(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """The probability label tracks the declared grade: alta, moderada, baixa."""
    coord, _source = make_coordinator(
        clock,
        [
            make_snapshot(),  # seed
            _violent(perill=6.0, data_emissio="2026-08-05T11:00Z"),  # alta
            _violent(perill=4.0, data_emissio="2026-08-05T11:20Z"),  # moderada
            _violent(perill=2.0, data_emissio="2026-08-05T11:40Z"),  # baixa
        ],
    )
    caught = _listen(hass)

    for _ in range(4):
        await coord.async_refresh()
    await hass.async_block_till_done()

    violent = [
        event.data for event in caught if event.event_type == c.EVENT_VIOLENT_WEATHER
    ]
    # Arrival order between distinct emissions is not guaranteed; match the
    # grade to its label as a multiset.
    labels = Counter(v["probabilitat"] for v in violent)
    assert labels == Counter({"alta": 1, "moderada": 1, "baixa": 1})


# ---------------------------------------------------------------------------
# Auxiliary: the reference clock is mid-band, as the fixtures assume
# ---------------------------------------------------------------------------


def test_clock_starts_at_the_12_18_boundary(clock: FakeClock) -> None:
    """The event tests assume 12:00 UTC: inside `12-18`, before `18-00`."""
    assert clock() == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
