"""Tests for the dual SMP data source: the public page and the API-key client.

Structure: the public page (success, the quiet-day non-trigger, the two failure
paths that do trigger the fallback, the hash short-circuit), then the API-key
source (the header, the 403/429/5xx policy, the quota reader), then the two
sources compared against the same raw JSON.

`aioresponses` intercepts every request, so nothing here touches the real
network. The retry backoff is neutered by the `_no_real_delay` fixture below
rather than by shortening `smp._RETRY_DELAYS_SECONDS`, so the real delay
schedule stays covered by the production code path.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import ClientConnectionError, ClientSession
from aioresponses import aioresponses
from custom_components.avisoscat import smp
from custom_components.avisoscat.const import (
    SMP_API_EPISODIS_OBERTS_URL,
    SMP_API_PREAVISOS_URL,
    SMP_API_QUOTA_URL,
    SMP_PAGE_FALLBACK_URL,
    SMP_PAGE_URL,
)
from custom_components.avisoscat.smp import ApiKeySource, PublicPageSource, QuotaInfo
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from yarl import URL

CAPTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "captures"
    / "smp-episodis-oberts-2026-08-05.json"
)

FIXED_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
EPISODES_URL = SMP_API_EPISODIS_OBERTS_URL.format(data=FIXED_NOW.date().isoformat())
API_KEY = "s3cret-x-api-key-must-never-be-logged"


@pytest.fixture
def sample_avisos() -> list:
    """The real payload captured live on 2026-08-05, day-nested as the feed sends it."""
    return json.loads(CAPTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the retry backoff sleep with a no-op so retry tests stay fast."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(smp, "_sleep", _instant)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `smp._now()` so `fetched_at` and the API `data=` query are deterministic."""
    monkeypatch.setattr(smp, "_now", lambda: FIXED_NOW)


@pytest.fixture
def mock_http():
    """An `aioresponses` context covering every request made in a test."""
    with aioresponses() as mocked:
        yield mocked


def _page(*, avisos: list, preavisos: list | None = None) -> str:
    """Wrap a raw SMP payload the way `meteo.cat` embeds it inline.

    Mirrors `tests/test_parser.py`'s own page wrapper: the call sits inside a
    `<script>` alongside other markup, which is what a sloppier anchor than
    `parser.py`'s would trip on.
    """
    avisos_json = json.dumps(avisos, ensure_ascii=False)
    preavisos_json = json.dumps(
        preavisos if preavisos is not None else [], ensure_ascii=False
    )
    call_body = (
        f"dom: 'mapaAvisos',\n"
        f"avisos: {avisos_json},\n"
        f"episodisPreavisos: {preavisos_json}"
    )
    return (
        "<!DOCTYPE html>\n<html lang='ca'>\n<body>\n"
        "<script type='text/javascript'>\n"
        f"    Meteocat.avisosSMP({{\n{call_body}\n    }});\n"
        "</script>\n</body>\n</html>\n"
    )


def _request_count(mock_http: aioresponses, url: str) -> int:
    return len(mock_http.requests.get(("GET", URL(url)), []))


# ---------------------------------------------------------------------------
# The `SmpSource` protocol
# ---------------------------------------------------------------------------


def test_both_sources_satisfy_the_smp_source_protocol() -> None:
    """The coordinator can swap implementations by changing one line."""
    assert isinstance(PublicPageSource(session=None), smp.SmpSource)  # type: ignore[arg-type]
    assert isinstance(ApiKeySource(session=None, api_key=API_KEY), smp.SmpSource)  # type: ignore[arg-type]


def test_now_returns_a_timezone_aware_utc_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real clock seam, not just the fixed stand-in the other tests patch in."""
    monkeypatch.undo()  # revert this test's own autouse `_fixed_clock` patch
    assert smp._now().tzinfo is UTC


# ---------------------------------------------------------------------------
# `PublicPageSource`
# ---------------------------------------------------------------------------


async def test_public_page_source_parses_the_primary_page(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """A healthy primary page is parsed without ever touching the fallback."""
    mock_http.get(SMP_PAGE_URL, status=200, body=_page(avisos=sample_avisos))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert not snapshot.is_empty
    assert snapshot.fetched_at == FIXED_NOW
    assert _request_count(mock_http, SMP_PAGE_URL) == 1


async def test_public_page_source_does_not_fall_back_on_a_quiet_day(
    mock_http: aioresponses,
) -> None:
    """No open episode is a normal result, not a failure: the fallback stays cold.

    No route is registered for the fallback URL at all, so if `fetch()` ever
    requested it `aioresponses` would raise for the unmatched request and this
    test would fail on that, not on a silent behaviour difference.
    """
    mock_http.get(SMP_PAGE_URL, status=200, body=_page(avisos=[[]], preavisos=[]))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert snapshot.is_empty


async def test_public_page_source_falls_back_on_download_failure(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """A connection error on the primary page is retried, then falls back."""
    mock_http.get(SMP_PAGE_URL, exception=ClientConnectionError("boom"), repeat=True)
    mock_http.get(SMP_PAGE_FALLBACK_URL, status=200, body=_page(avisos=sample_avisos))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert not snapshot.is_empty
    # One attempt plus the three retries of docs/04-architecture.md §10.
    assert _request_count(mock_http, SMP_PAGE_URL) == len(smp._RETRY_DELAYS_SECONDS)
    assert _request_count(mock_http, SMP_PAGE_FALLBACK_URL) == 1


async def test_public_page_source_falls_back_on_parse_error(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """Markup that does not carry the SMP call falls back, with no retry needed.

    Unlike a download failure, a `SmpParseError` is not a transient condition,
    so the primary page is only requested once before the fallback is tried.
    """
    mock_http.get(
        SMP_PAGE_URL, status=200, body="<html><body>no payload here</body></html>"
    )
    mock_http.get(SMP_PAGE_FALLBACK_URL, status=200, body=_page(avisos=sample_avisos))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert not snapshot.is_empty
    assert _request_count(mock_http, SMP_PAGE_URL) == 1
    assert _request_count(mock_http, SMP_PAGE_FALLBACK_URL) == 1


async def test_public_page_source_falls_back_on_a_non_retryable_status(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """A plain 4xx on the primary page is not retried, but still falls back."""
    mock_http.get(SMP_PAGE_URL, status=404)
    mock_http.get(SMP_PAGE_FALLBACK_URL, status=200, body=_page(avisos=sample_avisos))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert not snapshot.is_empty
    assert _request_count(mock_http, SMP_PAGE_URL) == 1
    assert _request_count(mock_http, SMP_PAGE_FALLBACK_URL) == 1


async def test_public_page_source_retries_5xx_before_falling_back(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """A transient server error on the primary page is retried, then falls back."""
    mock_http.get(SMP_PAGE_URL, status=502, repeat=True)
    mock_http.get(SMP_PAGE_FALLBACK_URL, status=200, body=_page(avisos=sample_avisos))

    async with ClientSession() as session:
        snapshot = await PublicPageSource(session).fetch()

    assert not snapshot.is_empty
    assert _request_count(mock_http, SMP_PAGE_URL) == len(smp._RETRY_DELAYS_SECONDS)
    assert _request_count(mock_http, SMP_PAGE_FALLBACK_URL) == 1


async def test_public_page_source_raises_update_failed_when_both_pages_fail(
    mock_http: aioresponses,
) -> None:
    """The caller keeps its last good state: a total failure raises, not returns."""
    mock_http.get(SMP_PAGE_URL, exception=ClientConnectionError("boom"), repeat=True)
    mock_http.get(
        SMP_PAGE_FALLBACK_URL, exception=ClientConnectionError("boom"), repeat=True
    )

    async with ClientSession() as session:
        with pytest.raises(UpdateFailed):
            await PublicPageSource(session).fetch()


async def test_public_page_source_skips_reparsing_on_unchanged_hash(
    mock_http: aioresponses, sample_avisos: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second fetch with an unchanged payload short-circuits on the hash."""
    mock_http.get(
        SMP_PAGE_URL, status=200, body=_page(avisos=sample_avisos), repeat=True
    )
    calls = 0
    original_parse_snapshot = smp.parse_snapshot

    def _counting_parse_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_parse_snapshot(*args, **kwargs)

    monkeypatch.setattr(smp, "parse_snapshot", _counting_parse_snapshot)

    async with ClientSession() as session:
        source = PublicPageSource(session)
        first = await source.fetch()
        second = await source.fetch()

    assert calls == 1
    assert first is second


# ---------------------------------------------------------------------------
# `ApiKeySource`
# ---------------------------------------------------------------------------


async def test_api_key_source_sends_the_header_and_never_logs_the_key(
    mock_http: aioresponses, caplog: pytest.LogCaptureFixture
) -> None:
    """The key travels only in the `x-api-key` header, never in a log or repr."""
    caplog.set_level(logging.DEBUG, logger="custom_components.avisoscat.smp")
    mock_http.get(EPISODES_URL, status=200, payload=[])
    mock_http.get(SMP_API_PREAVISOS_URL, status=200, payload=[])

    async with ClientSession() as session:
        source = ApiKeySource(session, API_KEY)
        await source.fetch()
        assert API_KEY not in repr(source)

    sent_headers = mock_http.requests[("GET", URL(EPISODES_URL))][0].kwargs["headers"]
    assert sent_headers["x-api-key"] == API_KEY
    assert API_KEY not in caplog.text


async def test_api_key_source_429_does_not_retry(mock_http: aioresponses) -> None:
    """A quota-exhausted response raises immediately: retrying would burn more."""
    mock_http.get(EPISODES_URL, status=429)

    async with ClientSession() as session:
        with pytest.raises(UpdateFailed):
            await ApiKeySource(session, API_KEY).fetch()

    assert _request_count(mock_http, EPISODES_URL) == 1


async def test_api_key_source_403_raises_auth_failed(mock_http: aioresponses) -> None:
    """A rejected key opens reauth instead of being treated as a transient error."""
    mock_http.get(EPISODES_URL, status=403)

    async with ClientSession() as session:
        with pytest.raises(ConfigEntryAuthFailed):
            await ApiKeySource(session, API_KEY).fetch()

    assert _request_count(mock_http, EPISODES_URL) == 1


async def test_api_key_source_other_4xx_does_not_retry(mock_http: aioresponses) -> None:
    """A malformed-request response is not retryable either, just not special-cased."""
    mock_http.get(EPISODES_URL, status=400)

    async with ClientSession() as session:
        with pytest.raises(UpdateFailed):
            await ApiKeySource(session, API_KEY).fetch()

    assert _request_count(mock_http, EPISODES_URL) == 1


async def test_api_key_source_retries_5xx_then_succeeds(
    mock_http: aioresponses,
) -> None:
    """A transient server error is retried with backoff instead of failing outright."""
    mock_http.get(EPISODES_URL, status=503)
    mock_http.get(EPISODES_URL, status=200, payload=[])
    mock_http.get(SMP_API_PREAVISOS_URL, status=200, payload=[])

    async with ClientSession() as session:
        snapshot = await ApiKeySource(session, API_KEY).fetch()

    assert snapshot.is_empty
    assert _request_count(mock_http, EPISODES_URL) == 2


async def test_api_key_source_retries_connection_errors_then_succeeds(
    mock_http: aioresponses,
) -> None:
    """A connection-level failure is just as retryable as a 5xx status."""
    mock_http.get(EPISODES_URL, exception=ClientConnectionError("boom"))
    mock_http.get(EPISODES_URL, status=200, payload=[])
    mock_http.get(SMP_API_PREAVISOS_URL, status=200, payload=[])

    async with ClientSession() as session:
        snapshot = await ApiKeySource(session, API_KEY).fetch()

    assert snapshot.is_empty
    assert _request_count(mock_http, EPISODES_URL) == 2


async def test_api_key_source_raises_update_failed_after_exhausting_retries(
    mock_http: aioresponses,
) -> None:
    """Persistent 5xx/connection failures still surface as `UpdateFailed`."""
    mock_http.get(EPISODES_URL, exception=ClientConnectionError("boom"), repeat=True)

    async with ClientSession() as session:
        with pytest.raises(UpdateFailed):
            await ApiKeySource(session, API_KEY).fetch()

    assert _request_count(mock_http, EPISODES_URL) == len(smp._RETRY_DELAYS_SECONDS)


async def test_api_key_source_raises_update_failed_on_invalid_json(
    mock_http: aioresponses,
) -> None:
    """A body that will not decode as JSON is not retryable either."""
    mock_http.get(EPISODES_URL, status=200, body="not json", content_type="text/plain")

    async with ClientSession() as session:
        with pytest.raises(UpdateFailed):
            await ApiKeySource(session, API_KEY).fetch()

    assert _request_count(mock_http, EPISODES_URL) == 1


async def test_api_key_source_fetch_quota_reads_the_forecast_plan(
    mock_http: aioresponses,
) -> None:
    """The "Dades de Predicció" plan is the one the SMP endpoints bill against."""
    payload = {
        "client": {"nom": "Client1", "apiKey": "xx...xxx"},
        "plans": [
            {"nom": "Quotes"},
            {
                "nom": "Dades de Predicció",
                "periode": "Mensual",
                "maxConsultes": 100,
                "consultesRestants": 82,
                "consultesRealitzades": 18,
            },
        ],
    }
    mock_http.get(SMP_API_QUOTA_URL, status=200, payload=payload)

    async with ClientSession() as session:
        quota = await ApiKeySource(session, API_KEY).fetch_quota()

    assert quota == QuotaInfo(
        plan_nom="Dades de Predicció",
        periode="Mensual",
        max_consultes=100,
        consultes_restants=82,
        consultes_realitzades=18,
    )


async def test_api_key_source_fetch_quota_returns_none_when_plan_missing(
    mock_http: aioresponses,
) -> None:
    """A reshaped quota response degrades to `None`, never an exception."""
    mock_http.get(SMP_API_QUOTA_URL, status=200, payload={"plans": [{"nom": "Quotes"}]})

    async with ClientSession() as session:
        quota = await ApiKeySource(session, API_KEY).fetch_quota()

    assert quota is None


async def test_api_key_source_fetch_quota_tolerates_a_non_dict_payload(
    mock_http: aioresponses,
) -> None:
    """A response shaped as something other than an object degrades to `None`."""
    mock_http.get(SMP_API_QUOTA_URL, status=200, payload=["unexpected"])

    async with ClientSession() as session:
        quota = await ApiKeySource(session, API_KEY).fetch_quota()

    assert quota is None


async def test_api_key_source_fetch_quota_tolerates_a_non_list_plans_field(
    mock_http: aioresponses,
) -> None:
    """A reshaped `plans` field degrades to `None` instead of raising."""
    mock_http.get(SMP_API_QUOTA_URL, status=200, payload={"plans": "not-a-list"})

    async with ClientSession() as session:
        quota = await ApiKeySource(session, API_KEY).fetch_quota()

    assert quota is None


async def test_api_key_source_fetch_quota_tolerates_malformed_numbers(
    mock_http: aioresponses,
) -> None:
    """Each numeric field degrades to `None` on its own instead of failing the plan.

    A non-object plan entry is skipped, a boolean is not read as a 0/1 count, an
    out-of-range float cannot become an `int`, and a stringly-typed count is not
    coerced: none of that is a reason to lose the plan name and period that did
    read cleanly.
    """
    mock_http.get(
        SMP_API_QUOTA_URL,
        status=200,
        payload={
            "plans": [
                "not-an-object",
                {
                    "nom": "Dades de Predicció",
                    "periode": "Mensual",
                    "maxConsultes": True,
                    "consultesRestants": float("inf"),
                    "consultesRealitzades": "18",
                },
            ]
        },
    )

    async with ClientSession() as session:
        quota = await ApiKeySource(session, API_KEY).fetch_quota()

    assert quota == QuotaInfo(
        plan_nom="Dades de Predicció",
        periode="Mensual",
        max_consultes=None,
        consultes_restants=None,
        consultes_realitzades=None,
    )


# ---------------------------------------------------------------------------
# Cross-source consistency
# ---------------------------------------------------------------------------


async def test_both_sources_produce_an_identical_snapshot(
    mock_http: aioresponses, sample_avisos: list
) -> None:
    """The public page and the API key read the same feed, so they must agree.

    `docs/01-data-sources.md` §3 states the public payload is the *same exact
    schema* as the API's `episodis-oberts` response, so the same raw JSON is fed
    to both sources here.
    """
    mock_http.get(SMP_PAGE_URL, status=200, body=_page(avisos=sample_avisos))
    mock_http.get(EPISODES_URL, status=200, payload=sample_avisos)
    mock_http.get(SMP_API_PREAVISOS_URL, status=200, payload=[])

    async with ClientSession() as session:
        public_snapshot = await PublicPageSource(session).fetch()
        api_snapshot = await ApiKeySource(session, API_KEY).fetch()

    assert public_snapshot == api_snapshot
