"""Resilience behaviour: consecutive failures, degraded event, repair issue.

Covers the four acceptance criteria of the implementation plan's diagnostics &
resilience task, the parts that live in the coordinator (docs/04-architecture.md
§10, docs/03-feature-spec.md §6):

1. Three consecutive fetch failures of the same kind fire exactly one
   `avisoscat_service_degraded` event and create one matching repair issue with
   a `learn_more_url`.
2. A fourth failure does not repeat either: the degraded flag stays set until
   a successful fetch clears it.
3. The diagnostic download never leaks `latitude`, `longitude` or `api_key`
   (covered in `test_diagnostics.py`).
4. The quota-driven interval maps `maxConsultes: 100` to 8 h, and the config
   flow warns when a validated key sits on a citizen plan.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from aioresponses import aioresponses
from custom_components.avisoscat import config_flow
from custom_components.avisoscat import const as c
from custom_components.avisoscat.const import (
    DEGRADED_FAILURE_THRESHOLD,
    DOMAIN,
    EVENT_SERVICE_DEGRADED,
    ISSUE_SERVICE_DEGRADED,
    LEARN_MORE_URL,
    QUOTA_HIGH_THRESHOLD,
    QUOTA_MEDIUM_THRESHOLD,
)
from custom_components.avisoscat.coordinator import (
    AvisoscatDataUpdateCoordinator,
    interval_for_quota,
)
from custom_components.avisoscat.models import SmpSnapshot
from custom_components.avisoscat.smp import ApiKeySource, PublicPageSource, QuotaInfo
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import FakeClock, make_config_entry
from .test_config_flow import FIXTURE, VIC, _location, _options_input

# ---------------------------------------------------------------------------
# Criterion 1: three failures fire one event and one repair issue
# ---------------------------------------------------------------------------


async def test_three_failures_fire_one_degraded_event_and_issue(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """Reaching the threshold fires the event and creates the repair issue."""
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed: a successful first fetch
    await hass.async_block_till_done()

    caught: list = []
    hass.bus.async_listen(EVENT_SERVICE_DEGRADED, caught.append)

    source._error = UpdateFailed("source down")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD):
        await coord.async_refresh()
        await hass.async_block_till_done()

    # Exactly one event for the three failures, never three.
    assert len(caught) == 1
    payload = caught[0].data
    assert payload["consecutive_failures"] == DEGRADED_FAILURE_THRESHOLD
    assert payload["comarca"]  # the comarca name is filled in
    assert payload["last_error"] == "source down"

    # And exactly one matching repair issue with a learn_more_url.
    registry = ir.async_get(hass)
    issue = registry.issues.get((DOMAIN, ISSUE_SERVICE_DEGRADED))
    assert issue is not None
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue.translation_key == ISSUE_SERVICE_DEGRADED
    assert issue.issue_domain == DOMAIN
    assert issue.is_fixable is False


# ---------------------------------------------------------------------------
# Criterion 2: a fourth failure does not repeat the event
# ---------------------------------------------------------------------------


async def test_fourth_failure_does_not_repeat_the_event(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """Only one degraded event fires per streak, no matter how long it runs."""
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed
    await hass.async_block_till_done()

    caught: list = []
    hass.bus.async_listen(EVENT_SERVICE_DEGRADED, caught.append)

    source._error = UpdateFailed("source down")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD + 1):
        await coord.async_refresh()
        await hass.async_block_till_done()

    assert len(caught) == 1
    # The coordinator's bookkeeping agrees: the streak is four, but the
    # announced flag has been set and stays set.
    assert coord.consecutive_failures == DEGRADED_FAILURE_THRESHOLD + 1
    assert coord.degraded_announced is True


async def test_two_failures_do_not_fire_the_event(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """Below the threshold the event never fires and the issue is not created."""
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed
    await hass.async_block_till_done()

    caught: list = []
    hass.bus.async_listen(EVENT_SERVICE_DEGRADED, caught.append)

    source._error = UpdateFailed("source down")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD - 1):
        await coord.async_refresh()
        await hass.async_block_till_done()

    assert caught == []
    assert coord.consecutive_failures == DEGRADED_FAILURE_THRESHOLD - 1
    assert coord.degraded_announced is False
    registry = ir.async_get(hass)
    assert (DOMAIN, ISSUE_SERVICE_DEGRADED) not in registry.issues


async def test_successful_fetch_clears_the_streak_and_the_issue(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A successful fetch resets the counter and deletes the repair issue.

    The repair issue is what would otherwise linger in the user's repairs
    inbox after the source has come back; clearing it on recovery keeps the
    integration's resilience bookkeeping in lockstep with the UI.
    """
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed
    await hass.async_block_till_done()

    source._error = UpdateFailed("source down")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD):
        await coord.async_refresh()
    await hass.async_block_till_done()
    assert coord.degraded_announced is True

    registry = ir.async_get(hass)
    assert (DOMAIN, ISSUE_SERVICE_DEGRADED) in registry.issues

    # A successful fetch closes the streak and deletes the issue.
    source._error = None  # type: ignore[attr-defined]
    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.consecutive_failures == 0
    assert coord.degraded_announced is False
    assert (DOMAIN, ISSUE_SERVICE_DEGRADED) not in registry.issues


async def test_recovered_streak_can_fire_again(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """After recovery a fresh streak fires the event again, exactly once.

    Guards against the announced flag sticking True across a recovery and
    silencing every future outage.
    """
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed
    await hass.async_block_till_done()

    caught: list = []
    hass.bus.async_listen(EVENT_SERVICE_DEGRADED, caught.append)

    # First streak: threshold failures, one event.
    source._error = UpdateFailed("first outage")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD):
        await coord.async_refresh()
    await hass.async_block_till_done()
    assert len(caught) == 1

    # Recovery, then a second streak of threshold failures.
    source._error = None  # type: ignore[attr-defined]
    await coord.async_refresh()
    await hass.async_block_till_done()
    source._error = UpdateFailed("second outage")  # type: ignore[attr-defined]
    for _ in range(DEGRADED_FAILURE_THRESHOLD):
        await coord.async_refresh()
    await hass.async_block_till_done()

    assert len(caught) == 2  # one event per streak, never accumulated


# ---------------------------------------------------------------------------
# Auth failures do not count toward the degradation threshold
# ---------------------------------------------------------------------------


async def test_auth_failure_does_not_fire_degraded(
    hass: HomeAssistant, clock: FakeClock, make_coordinator
) -> None:
    """A `ConfigEntryAuthFailed` is not 'service degraded': the key is wrong.

    The 403 path has its own user-facing resolution (reauth), so it must not
    pile onto the resilience threshold or open a 'source is down' repair
    issue. The counter stays at zero across repeated auth failures.
    """
    coord, source = make_coordinator(clock)
    await coord.async_refresh()  # seed
    await hass.async_block_till_done()

    caught: list = []
    hass.bus.async_listen(EVENT_SERVICE_DEGRADED, caught.append)

    source._error = ConfigEntryAuthFailed("403")  # type: ignore[attr-defined]
    # `_async_update_data` raises directly; `async_refresh` swallows the auth
    # failure into HA's reauth machinery, so the threshold is exercised via
    # the data method itself rather than through `async_refresh`.
    for _ in range(DEGRADED_FAILURE_THRESHOLD):
        with pytest.raises(ConfigEntryAuthFailed):
            await coord._async_update_data()
    await hass.async_block_till_done()

    assert caught == []
    assert coord.consecutive_failures == 0
    assert coord.degraded_announced is False


# ---------------------------------------------------------------------------
# Criterion 4 (quota part): the static maxConsultes -> interval mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("max_consultes", "expected_minutes"),
    [
        # The documented bands of docs/03-feature-spec.md §6.
        (1, 8 * 60),
        (100, 8 * 60),
        (QUOTA_MEDIUM_THRESHOLD, 8 * 60),
        (QUOTA_MEDIUM_THRESHOLD + 1, 2 * 60),
        (500, 2 * 60),
        (QUOTA_HIGH_THRESHOLD, 2 * 60),
        (QUOTA_HIGH_THRESHOLD + 1, 30),
        (10_000, 30),
    ],
)
def test_interval_for_quota_bands(max_consultes: int, expected_minutes: int) -> None:
    """The three spec bands map to 30 min / 2 h / 8 h exactly.

    `maxConsultes: 100` is the headline case (citizen plan): 8 h, which is
    what makes the 2 h violent-nowcast horizon unservable on a citizen key.
    """
    assert interval_for_quota(max_consultes) == timedelta(minutes=expected_minutes)


def _citizen_api_key_source() -> "type":
    """An `ApiKeySource` subclass whose quota endpoint reports a citizen plan.

    Subclasses the real type so the coordinator's `isinstance` check gates the
    quota read the way it does in production, without any network session
    behind it: the overrides never reach `aiohttp`. `fetch_quota_calls`
    exposes the call count so a test can prove the daily-quota concern has
    not turned into a per-fetch concern.
    """

    class _CitizenApiKeySource(ApiKeySource):
        fetch_quota_calls = 0

        async def fetch(self):  # type: ignore[override]
            return SmpSnapshot()

        async def fetch_quota(self):  # type: ignore[override]
            type(self).fetch_quota_calls += 1
            return QuotaInfo(
                plan_nom="dades de predicció",
                periode=None,
                max_consultes=100,
                consultes_restants=90,
                consultes_realitzades=10,
            )

    return _CitizenApiKeySource


async def test_quota_interval_overrides_the_adaptive_cadence(
    hass: HomeAssistant, clock: FakeClock
) -> None:
    """An API-key source with a citizen quota pins the cadence to 8 h.

    The first successful fetch reads the quota once and stores the resulting
    interval on the coordinator, which then overrides the adaptive 30/10 min
    logic for every subsequent cycle.
    """
    source = _citizen_api_key_source()(None, "irrelevant-key")  # type: ignore[arg-type]
    entry = make_config_entry()
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.quota_interval == timedelta(hours=8)
    assert coord.update_interval == timedelta(hours=8)
    # `source_kind` reports the actual class name, which is enough for
    # diagnostic triage between the API-key and public-page paths.
    assert "ApiKey" in coord.source_kind


async def test_quota_check_failure_leaves_adaptive_cadence_in_place(
    hass: HomeAssistant, clock: FakeClock
) -> None:
    """A quota read that raises never fails the fetch or the cadence.

    Quota is diagnostic, not load-bearing: if the endpoint is flaky the
    integration keeps working on the adaptive 30/10 min logic rather than
    failing a fetch that just succeeded.
    """

    class _QuotalessApiKeySource(ApiKeySource):
        async def fetch(self):  # type: ignore[override]
            return SmpSnapshot()

        async def fetch_quota(self):  # type: ignore[override]
            raise UpdateFailed("quota endpoint down")

    source = _QuotalessApiKeySource(None, "irrelevant-key")  # type: ignore[arg-type]
    entry = make_config_entry()
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.quota_interval is None
    # Falls back to the adaptive idle cadence for a quiet snapshot.
    assert coord.update_interval == timedelta(
        minutes=c.DEFAULT_SCAN_INTERVAL_IDLE_MINUTES
    )


@pytest.mark.parametrize(
    "quota",
    [
        None,  # no plan recognised in the response
        QuotaInfo(  # plan recognised but the count field is missing
            plan_nom="dades de predicció",
            periode=None,
            max_consultes=None,
            consultes_restants=None,
            consultes_realitzades=None,
        ),
    ],
)
async def test_quota_without_max_consultes_leaves_adaptive_cadence(
    hass: HomeAssistant, clock: FakeClock, quota
) -> None:
    """A quota response without a usable `maxConsultes` keeps the adaptive logic.

    The endpoint returns 200 (so the key is valid), but the plan entry is
    missing or its count is unusable: the coordinator cannot derive an
    interval from it, so it stays adaptive and the diagnostic sensor has
    nothing to show.
    """

    class _QuotaApiKeySource(ApiKeySource):
        async def fetch(self):  # type: ignore[override]
            return SmpSnapshot()

        async def fetch_quota(self):  # type: ignore[override]
            return quota

    source = _QuotaApiKeySource(None, "irrelevant-key")  # type: ignore[arg-type]
    entry = make_config_entry()
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.quota_interval is None
    assert coord.update_interval == timedelta(
        minutes=c.DEFAULT_SCAN_INTERVAL_IDLE_MINUTES
    )


async def test_quota_read_only_happens_once(
    hass: HomeAssistant, clock: FakeClock
) -> None:
    """The quota endpoint is read exactly once per coordinator, not per fetch.

    A daily re-read is a future concern; for now the interval is set on the
    first successful fetch and never re-queried, so the source's
    `fetch_quota` runs at most once across many refreshes.
    """
    source_cls = _citizen_api_key_source()
    source = source_cls(None, "irrelevant-key")  # type: ignore[arg-type]
    entry = make_config_entry()
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    for _ in range(5):
        await coord.async_refresh()
    await hass.async_block_till_done()

    assert source_cls.fetch_quota_calls == 1


async def test_public_source_never_reads_quota(
    hass: HomeAssistant, clock: FakeClock
) -> None:
    """The keyless public source has no quota to read, so it stays adaptive.

    Guards against the quota check accidentally firing for `PublicPageSource`,
    which does not implement `fetch_quota` at all.
    """

    class _QuietPublicSource(PublicPageSource):
        async def fetch(self):  # type: ignore[override]
            return SmpSnapshot()

    source = _QuietPublicSource(None)  # type: ignore[arg-type]
    entry = make_config_entry()
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.quota_interval is None
    # `source_kind` reports the actual class name; the test only needs to
    # prove the public-page path was taken, not the API-key one.
    assert "Public" in coord.source_kind


async def test_fixed_interval_still_wins_over_quota(
    hass: HomeAssistant, clock: FakeClock
) -> None:
    """A user-chosen fixed interval overrides the quota-driven cadence.

    Priority is fixed > quota > adaptive: a user who deliberately forces an
    interval accepts the quota trade-off, so their choice wins.
    """
    source = _citizen_api_key_source()(None, "irrelevant-key")  # type: ignore[arg-type]
    entry = make_config_entry(
        options={c.CONF_SEVERE_THRESHOLD: 3, c.CONF_SCAN_INTERVAL: 15}
    )
    with patch("custom_components.avisoscat.coordinator.utcnow", clock):
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)

    await coord.async_refresh()
    await hass.async_block_till_done()

    assert coord.quota_interval == timedelta(hours=8)
    assert coord.update_interval == timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Criterion 4 (config-flow part): the low-quota warning
# ---------------------------------------------------------------------------


def _low_quota() -> QuotaInfo:
    """A quota info reporting a citizen plan (maxConsultes: 100)."""
    return QuotaInfo(
        plan_nom="dades de predicció",
        periode=None,
        max_consultes=100,
        consultes_restants=90,
        consultes_realitzades=10,
    )


def _high_quota() -> QuotaInfo:
    """A quota info reporting a plan above the citizen threshold."""
    return QuotaInfo(
        plan_nom="dades de predicció",
        periode=None,
        max_consultes=2000,
        consultes_restants=1900,
        consultes_realitzades=100,
    )


async def test_low_quota_key_routes_to_confirmation_step(hass: HomeAssistant) -> None:
    """A validated citizen key opens the low-quota warning step.

    The user must confirm they accept the slow cadence before the entry is
    committed. The first submit does not create the entry; the warning step
    does.
    """

    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            return_value=(None, _low_quota()),
        ),
        aioresponses() as mocked,
    ):
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input(api_key="a-citizen-key")
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "low_quota_warning"


async def test_high_quota_key_creates_entry_directly(hass: HomeAssistant) -> None:
    """A validated key above the citizen threshold skips the warning step."""

    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            return_value=(None, _high_quota()),
        ),
        aioresponses() as mocked,
    ):
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input(api_key="a-pro-key")
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][c.CONF_API_KEY] == "a-pro-key"


async def test_low_quota_warning_confirmation_creates_entry(
    hass: HomeAssistant,
) -> None:
    """Submitting the warning step commits the pending entry unchanged."""

    with (
        patch(
            "custom_components.avisoscat.config_flow.async_validate_api_key",
            return_value=(None, _low_quota()),
        ),
        aioresponses() as mocked,
    ):
        mocked.get(
            "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json",
            body=FIXTURE.read_bytes(),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _location(*VIC)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _options_input(api_key="a-citizen-key")
        )
        assert result["step_id"] == "low_quota_warning"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][c.CONF_API_KEY] == "a-citizen-key"


def test_is_low_quota_thresholds() -> None:
    """`_is_low_quota` follows the spec's `<= 200` band, not a strict less-than."""
    assert c.LOW_QUOTA_WARNING_THRESHOLD == QUOTA_MEDIUM_THRESHOLD

    citizen = QuotaInfo(
        plan_nom="p",
        periode=None,
        max_consultes=QUOTA_MEDIUM_THRESHOLD,
        consultes_restants=1,
        consultes_realitzades=1,
    )
    assert config_flow._is_low_quota(citizen)

    pro = QuotaInfo(
        plan_nom="p",
        periode=None,
        max_consultes=QUOTA_MEDIUM_THRESHOLD + 1,
        consultes_restants=1,
        consultes_realitzades=1,
    )
    assert not config_flow._is_low_quota(pro)

    # A missing plan or missing max_consultes is treated as not-low: the
    # request returned 200, so the key is valid, and there is no value to
    # warn about.
    assert config_flow._is_low_quota(None) is False
    assert (
        config_flow._is_low_quota(
            QuotaInfo(
                plan_nom="p",
                periode=None,
                max_consultes=None,
                consultes_restants=None,
                consultes_realitzades=None,
            )
        )
        is False
    )
