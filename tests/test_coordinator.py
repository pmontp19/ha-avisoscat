"""The coordinator mechanics behind the events (docs/04-architecture.md §5, §10).

The event semantics live in `test_events.py`; this module covers the parts that
are about the coordinator itself rather than any one event type:

- criterion 5: a setup that lands on an already-active day fires nothing.
- criterion 7: a failed fetch keeps the last good projections and records the
  failure on `last_error`.
- criterion 8: a band transition at the 12:00 UTC boundary fires `started`
  through the minute recompute with no fetch at all.
- criterion 9: the poll interval drops from 30 to 10 minutes the moment an open
  episode appears.
"""

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.avisoscat import const as c
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import (
    FakeClock,
    afectacio_raw,
    episodi_raw,
    evolucio_raw,
    make_snapshot,
)


def _midday_vent(*, perill: float = 3.0):
    """A wind warning in today's `12-18` band: announced before 12:00, then in force."""
    return make_snapshot(
        [episodi_raw([evolucio_raw({"12-18": [afectacio_raw(perill=perill)]})])]
    )


def _busy_seed():
    """A first picture that is already active: one in force, one announced."""
    tomorrow = "2026-08-06T00:00Z"
    return make_snapshot(
        [
            episodi_raw([evolucio_raw({"12-18": [afectacio_raw(perill=3.0)]})]),
            episodi_raw(
                [
                    evolucio_raw(
                        {"12-18": [afectacio_raw(perill=2.0, dia=tomorrow)]},
                        dia=tomorrow,
                    )
                ],
                meteor="Pluja",
            ),
        ]
    )


def _violent_nowcast(*, perill: float = 6.0, data_emissio: str = "2026-08-05T11:30Z"):
    """A violent-weather nowcast in force at the clock start (trap-#12 shape).

    Its two-hour window opens at the issue time, so at the default 11:30 issue
    the clock (12:00) sits inside it and it reads as in force.
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
# Criterion 5: a busy first picture fires nothing
# ---------------------------------------------------------------------------


async def test_busy_startup_fires_nothing(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """Landing on an already-active day seeds the dedup memory and stays quiet."""
    coord, _source = make_coordinator(clock, [_busy_seed()])
    caught: list = []
    for event_type in (
        c.EVENT_WARNING_ANNOUNCED,
        c.EVENT_WARNING_STARTED,
        c.EVENT_WARNING_UPGRADED,
        c.EVENT_VIOLENT_WEATHER,
    ):
        hass.bus.async_listen(event_type, caught.append)

    await coord.async_refresh()  # seed
    await coord.async_refresh()  # a second cycle, still nothing new
    await hass.async_block_till_done()

    assert caught == []
    assert coord.data is not None
    assert len(coord.data.en_vigor) == 1
    assert len(coord.data.anunciats) == 1


# ---------------------------------------------------------------------------
# Criterion 7: a failed fetch keeps the last good state and records the error
# ---------------------------------------------------------------------------


async def test_failed_fetch_keeps_state_and_records_error(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """The last good projections survive a fetch failure, and `last_error` is set."""
    coord, source = make_coordinator(clock, [_midday_vent(perill=3.0)])
    await coord.async_refresh()  # seed: one warning in force
    await hass.async_block_till_done()
    assert coord.data is not None
    assert coord.data.last_error is None
    assert len(coord.data.en_vigor) == 1

    source._error = UpdateFailed("boom")  # type: ignore[attr-defined]
    await coord.async_refresh()  # the fetch fails
    await hass.async_block_till_done()

    assert coord.last_update_success is False
    # Last good projections preserved, not cleared.
    assert len(coord.data.en_vigor) == 1
    assert coord.data.en_vigor[0].perill == 3
    assert coord.data.last_error == "boom"


# ---------------------------------------------------------------------------
# Criterion 8: a band transition fires through the recompute with no fetch
# ---------------------------------------------------------------------------


async def test_minute_recompute_fires_started_without_fetching(
    hass: HomeAssistant, make_coordinator
) -> None:
    """The 12:00 UTC band opening fires `started` from cached state, no HTTP."""
    clock = FakeClock(datetime(2026, 8, 5, 11, 0, tzinfo=UTC))  # before the 12-18 band
    coord, source = make_coordinator(clock, [_midday_vent()])
    caught: list = []
    hass.bus.async_listen(c.EVENT_WARNING_STARTED, caught.append)

    await coord.async_refresh()  # 11:00: seed, the 12-18 band is announced
    await hass.async_block_till_done()
    assert source.calls == 1
    assert coord.data is not None
    assert coord.data.en_vigor == []  # not in force yet

    clock.advance(hours=1)  # 12:00: the 12-18 band opens
    coord.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()

    # The recompute emitted `started` against the cached snapshot, without fetching.
    assert source.calls == 1
    assert len(caught) == 1
    assert coord.data.en_vigor[0].perill == 3


# ---------------------------------------------------------------------------
# Criterion 9: the interval drops to 10 minutes when something is active
# ---------------------------------------------------------------------------


async def test_interval_drops_when_an_episode_is_active(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A quiet day polls every 30 min; an open episode brings it to 10 min."""
    coord, _source = make_coordinator(clock, [make_snapshot(), _midday_vent()])

    await coord.async_refresh()  # seed: empty, idle
    await hass.async_block_till_done()
    assert coord.update_interval == timedelta(
        minutes=c.DEFAULT_SCAN_INTERVAL_IDLE_MINUTES
    )

    await coord.async_refresh()  # an in-force episode appears
    await hass.async_block_till_done()
    assert coord.update_interval == timedelta(
        minutes=c.DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES
    )


async def test_fixed_interval_overrides_the_adaptive_one(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A user-chosen fixed interval never yields to the 10-minute active cadence."""
    coord, _source = make_coordinator(
        clock,
        [make_snapshot(), _midday_vent()],
        options={c.CONF_SEVERE_THRESHOLD: 3, c.CONF_SCAN_INTERVAL: 20},
    )

    await coord.async_refresh()  # seed: empty
    await coord.async_refresh()  # active
    await hass.async_block_till_done()

    assert coord.update_interval == timedelta(minutes=20)


# ---------------------------------------------------------------------------
# Auxiliary: the coordinator's failure on a non-update error is wrapped
# ---------------------------------------------------------------------------


async def test_unexpected_source_error_is_wrapped_as_update_failure(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A non-`UpdateFailed` escape from the source still degrades, never crashes."""
    coord, source = make_coordinator(clock, [_midday_vent()])
    await coord.async_refresh()  # seed
    source._error = RuntimeError("the source exploded")  # type: ignore[attr-defined]

    # Must not raise: the coordinator wraps it and keeps the last good state.
    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.last_update_success is False
    assert coord.data is not None
    assert coord.data.last_error is not None


# ---------------------------------------------------------------------------
# Auxiliary: an auth failure propagates instead of degrading like a fetch error
# ---------------------------------------------------------------------------


async def test_auth_failure_propagates_out_of_refresh(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A `ConfigEntryAuthFailed` is re-raised, not swallowed as a fetch failure.

    A bad or expired API key is not transient: the coordinator must hand it back
    to Home Assistant so the entry is flagged for reauth, rather than retrying
    forever as if the feed were merely down. The data method is exercised
    directly because the `DataUpdateCoordinator` framework intercepts this
    exception at `async_refresh` to start the (still-pending) reauth flow.
    """
    coord, _source = make_coordinator(
        clock, [_midday_vent()], error=ConfigEntryAuthFailed("bad key")
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


# ---------------------------------------------------------------------------
# Auxiliary: a busy startup that lands on a violent nowcast seeds its dedup
# ---------------------------------------------------------------------------


async def test_busy_startup_with_violent_nowcast_does_not_replay_it(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """Landing mid-nowcast seeds `_violent_seen`, so the next cycle stays quiet."""
    coord, _source = make_coordinator(clock, [_violent_nowcast()])
    caught: list = []
    hass.bus.async_listen(c.EVENT_VIOLENT_WEATHER, caught.append)

    await coord.async_refresh()  # seed: the nowcast is already in force
    await coord.async_refresh()  # identical picture: nothing new to say
    await hass.async_block_till_done()

    assert caught == []


async def test_minute_recompute_is_a_noop_before_the_first_fetch(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A recompute before any successful fetch returns early, never crashes.

    Setup arms the recompute only after the first refresh, but a failed first
    fetch leaves `data` None until a retry succeeds; a minute tick landing in
    that window must be a silent no-op rather than a `None` dereference.
    """
    coord, _source = make_coordinator(clock, [make_snapshot()])
    assert coord.data is None  # no refresh yet

    coord.async_schedule_minute_recompute(clock())
    await hass.async_block_till_done()

    assert coord.data is None  # still nothing to recompute against
