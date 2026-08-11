"""Dual SMP data source: the keyless public page and the optional API-key client.

Two implementations behind one `Protocol`, so the coordinator never learns which
one it has (docs/04-architecture.md §3): `PublicPageSource` scrapes the payload
`meteo.cat` embeds inline in its own pages, `ApiKeySource` queries
`api.meteo.cat` with an `x-api-key` header. Turning raw JSON into an `SmpSnapshot`
is `models.parse_snapshot()`'s job and finding that JSON inside an HTML page is
`parser.extract_smp_payload()`'s; this module only fetches and hands the result
upward.

Error policy (docs/04-architecture.md §10), the part that differs between the two
sources:

- A network failure or `SmpParseError` on the primary public page tries the
  fallback page (`https://www.meteo.cat/`) before giving up. A *successful*
  fetch that yields no episodes is not a failure and never triggers it: a quiet
  day is the normal state and the two candidate pages carry the same payload
  byte for byte (docs/captures/smp-page-choice-2026-08-06.md), so the fallback
  could not add episodes the primary did not already have.
- `403` from the API-key source means the key was rejected, so it raises
  `ConfigEntryAuthFailed` and lets Home Assistant open reauth.
  `429` means the citizen quota is exhausted, so it raises `UpdateFailed`
  **without retrying**: retrying would burn quota that is already gone.
  `5xx` and timeouts retry with a 1s/2s/4s backoff. Any other `4xx` does not
  retry either, since retrying a request the server has already rejected as
  malformed cannot succeed.

Neither source ever discards a snapshot it already produced: a failed fetch
raises `ConfigEntryAuthFailed` or `UpdateFailed` instead of returning an empty
`SmpSnapshot`, so the coordinator's own last good state is what survives, not a
false "nothing is happening" reading (docs/04-architecture.md §7).

`models.compute_payload_hash()` is what gates reprocessing: it costs more than
`parse_snapshot()` on its own (docs/04-architecture.md §3), so the only way that
expense pays for itself is by actually skipping `parse_snapshot()` when the
digest has not changed, which is what `_snapshot_or_cached()` below does. A
digest that *has* changed is not evidence the new payload is any good, only that
it differs from the last one; `parse_snapshot()` still has to run on it and
still never raises.
"""

from __future__ import annotations

import logging
from asyncio import sleep as _sleep
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    SMP_API_EPISODIS_OBERTS_URL,
    SMP_API_PREAVISOS_URL,
    SMP_API_QUOTA_URL,
    SMP_PAGE_FALLBACK_URL,
    SMP_PAGE_URL,
)
from .models import SmpSnapshot, compute_payload_hash, parse_snapshot
from .parser import SmpParseError, extract_smp_payload

_LOGGER = logging.getLogger(__name__)

__all__ = ["ApiKeySource", "PublicPageSource", "QuotaInfo", "SmpSource"]

_REQUEST_TIMEOUT_SECONDS: Final = 20
# 3 retries after the initial attempt, per docs/04-architecture.md §10.
_RETRY_DELAYS_SECONDS: Final = (0.0, 1.0, 2.0, 4.0)
_GZIP_HEADERS: Final = {"Accept-Encoding": "gzip"}
_SERVER_ERROR_STATUS: Final = 500
_STATUS_FORBIDDEN: Final = 403
_STATUS_TOO_MANY_REQUESTS: Final = 429

# The plan the SMP endpoints are billed against (docs/01-data-sources.md §2.4).
_QUOTA_PLAN_NAME: Final = "dades de predicció"


def _now() -> datetime:
    """The current UTC instant, as a seam tests patch (`smp._now`).

    Kept free of Home Assistant's `dt_util` on purpose: this module has no other
    reason to import it, and a plain patchable function is enough for the
    `fetched_at` stamp and the API source's `{data}` query parameter.
    """
    return datetime.now(UTC)


@runtime_checkable
class SmpSource(Protocol):
    """What the coordinator needs from either SMP data source.

    Swapping `PublicPageSource` for `ApiKeySource` is changing one line: neither
    the coordinator, the models nor the entities know which one produced a given
    `SmpSnapshot` (docs/04-architecture.md §3). `@runtime_checkable` is only for
    the `isinstance()` check in `tests/test_smp.py` that pins this contract; it
    does not make either method signature checked at runtime.
    """

    async def fetch(self) -> SmpSnapshot:
        """Fetch and parse the current SMP snapshot.

        Raises `ConfigEntryAuthFailed` or `UpdateFailed` on failure; never
        returns a snapshot that silently drops data the source actually read.
        """
        ...


@dataclass(frozen=True, slots=True)
class QuotaInfo:
    """Consumption of the citizen quota for the SMP forecast plan.

    Read from `/quotes/v1/consum-actual` (docs/01-data-sources.md §2.4). Deriving
    a polling interval from this is the coordinator's job (docs/03-feature-spec.md
    §6, Task 13), not this module's: `ApiKeySource` only exposes what it read.
    """

    plan_nom: str
    periode: str | None
    max_consultes: int | None
    consultes_restants: int | None
    consultes_realitzades: int | None


@dataclass
class _PayloadCache:
    """Last hash/snapshot pair a source produced.

    Not frozen, unlike everything in `models.py`: this is mutated in place as
    fetches complete. Both `PublicPageSource` and `ApiKeySource` keep one of
    these so an unchanged payload short-circuits on the hash instead of paying
    for `parse_snapshot()` again.
    """

    payload_hash: str | None = None
    snapshot: SmpSnapshot | None = None


def _snapshot_or_cached(
    cache: _PayloadCache, episodis_raw: Any, preavisos_raw: Any
) -> SmpSnapshot:
    """Parse a raw payload, or return the cached snapshot when nothing changed.

    `compute_payload_hash()` never raises and canonicalises list order first, so
    two fetches of identical SMP data hash equal even though the feed rotates its
    `afectacions` list between requests (docs/01-data-sources.md §3.1). Only a
    changed digest re-runs `parse_snapshot()`; a changed digest is still not
    evidence the new payload is usable, so `parse_snapshot()` is trusted to
    degrade on its own if it is not.
    """
    payload_hash = compute_payload_hash(episodis_raw, preavisos_raw)
    if cache.snapshot is not None and payload_hash == cache.payload_hash:
        return cache.snapshot
    snapshot = parse_snapshot(
        episodis_raw, preavisos_raw, fetched_at=_now(), payload_hash=payload_hash
    )
    cache.payload_hash = payload_hash
    cache.snapshot = snapshot
    return snapshot


async def _get_text_with_retry(session: ClientSession, url: str) -> str:
    """GET a page as text, retrying 5xx and timeouts/connection errors.

    Any other failure (a 4xx, for instance) is not retryable and propagates
    straight away: the server has already told us the request will not succeed
    no matter how many times it is repeated.
    """
    last_error: Exception | None = None
    for delay in _RETRY_DELAYS_SECONDS:
        if delay:
            await _sleep(delay)
        try:
            async with session.get(
                url,
                headers=_GZIP_HEADERS,
                timeout=ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                return await response.text()
        except ClientResponseError as err:
            if err.status < _SERVER_ERROR_STATUS:
                raise
            last_error = err
        except (ClientError, TimeoutError) as err:
            last_error = err
    # Every branch above either returned or set `last_error`, so this is only
    # reached once every attempt has failed.
    raise last_error


class PublicPageSource:
    """Default, keyless source: scrape the payload `meteo.cat` embeds inline.

    Downloads `page_url` and, only if that fetch or its parse fails, retries
    `fallback_url`. The two pages carry the same payload byte for byte
    (docs/captures/smp-page-choice-2026-08-06.md), so the fallback exists for
    availability, never to complete a result the primary page already gave in
    full.
    """

    def __init__(
        self,
        session: ClientSession,
        *,
        page_url: str = SMP_PAGE_URL,
        fallback_url: str = SMP_PAGE_FALLBACK_URL,
    ) -> None:
        self._session = session
        self._page_url = page_url
        self._fallback_url = fallback_url
        self._cache = _PayloadCache()

    async def fetch(self) -> SmpSnapshot:
        """Fetch and parse the SMP snapshot, falling back only on failure."""
        try:
            html = await _get_text_with_retry(self._session, self._page_url)
            return self._parse(html)
        except (ClientError, TimeoutError, SmpParseError) as err:
            _LOGGER.warning(
                "Could not read the SMP payload from the primary page %s (%s); "
                "trying the fallback %s",
                self._page_url,
                err,
                self._fallback_url,
            )
        try:
            html = await _get_text_with_retry(self._session, self._fallback_url)
            return self._parse(html)
        except (ClientError, TimeoutError, SmpParseError) as err:
            raise UpdateFailed(
                f"Could not read the SMP payload from either meteo.cat page: {err}"
            ) from err

    def _parse(self, html: str) -> SmpSnapshot:
        episodis_raw, preavisos_raw = extract_smp_payload(html)
        return _snapshot_or_cached(self._cache, episodis_raw, preavisos_raw)


class ApiKeySource:
    """Optional, `x-api-key`-authenticated source (docs/01-data-sources.md §2).

    The key lives only in the `x-api-key` request header: it is never
    interpolated into a log line, an exception message, or this object's `repr`.
    """

    def __init__(self, session: ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key
        self._cache = _PayloadCache()

    def __repr__(self) -> str:
        # Deliberately omits `_api_key`: a stray `repr()` in a log or a
        # traceback must never be the thing that leaks it.
        return f"{type(self).__name__}(session={self._session!r})"

    async def fetch(self) -> SmpSnapshot:
        """Fetch open episodes and pre-warnings, and parse them into a snapshot."""
        today = _now().date().isoformat()
        episodes_url = SMP_API_EPISODIS_OBERTS_URL.format(data=today)
        episodis_raw = await self._get_json(episodes_url)
        preavisos_raw = await self._get_json(SMP_API_PREAVISOS_URL)
        return _snapshot_or_cached(self._cache, episodis_raw, preavisos_raw)

    async def fetch_quota(self) -> QuotaInfo | None:
        """Read the current consumption of the SMP forecast plan quota.

        Not part of `SmpSource`: quota only exists with an API key, and how
        often to call this is the coordinator's decision (docs/03-feature-spec.md
        §6), not this module's. `None` means the response did not carry a
        recognisable "Dades de Predicció" plan entry, not that the request
        failed - a request failure still raises like `fetch()` does.
        """
        payload = await self._get_json(SMP_API_QUOTA_URL)
        return _parse_quota(payload)

    async def _get_json(self, url: str) -> Any:
        """GET a JSON endpoint with the API-key error policy applied.

        `403` and `429` are terminal on the first response and raise
        immediately, with no retry at all: a rejected key will not start
        working on a second try, and retrying a `429` would only burn more of
        an already-exhausted quota. `5xx` and connection/timeout errors retry
        with backoff; any other failure, including a body that will not decode
        as JSON, is not retryable and becomes `UpdateFailed`.
        """
        last_error: Exception | None = None
        for delay in _RETRY_DELAYS_SECONDS:
            if delay:
                await _sleep(delay)
            try:
                async with self._session.get(
                    url,
                    headers={"x-api-key": self._api_key},
                    timeout=ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                ) as response:
                    if response.status == _STATUS_FORBIDDEN:
                        raise ConfigEntryAuthFailed(
                            "The Meteocat SMP API key was rejected (403 Forbidden)"
                        )
                    if response.status == _STATUS_TOO_MANY_REQUESTS:
                        raise UpdateFailed(
                            "The Meteocat SMP API quota is exhausted (429); "
                            "not retrying"
                        )
                    response.raise_for_status()
                    return await response.json(content_type=None)
            except (ConfigEntryAuthFailed, UpdateFailed):
                raise
            except ClientResponseError as err:
                if err.status < _SERVER_ERROR_STATUS:
                    raise UpdateFailed(
                        f"SMP API request to {url} failed with status {err.status}"
                    ) from err
                last_error = err
            except (ClientError, TimeoutError) as err:
                last_error = err
            except ValueError as err:
                raise UpdateFailed(
                    f"SMP API response from {url} was not valid JSON"
                ) from err
        raise UpdateFailed(
            f"SMP API request to {url} failed after retries: {last_error}"
        ) from last_error


def _as_optional_int(value: Any) -> int | None:
    """Tolerant int conversion for the quota response; `None` when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    return None


def _parse_quota(payload: Any) -> QuotaInfo | None:
    """Read the "Dades de Predicció" plan off a `/quotes/v1/consum-actual` body.

    Never raises: an unreadable or reshaped quota payload only means the
    diagnostic quota sensor stays unavailable, which is a poor reason to fail
    the whole fetch cycle. `client.apiKey`, which the endpoint echoes back
    (docs/01-data-sources.md §2.4), is never read here or anywhere else in this
    module.
    """
    if not isinstance(payload, dict):
        return None
    plans = payload.get("plans")
    if not isinstance(plans, list):
        return None
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        nom = plan.get("nom")
        if isinstance(nom, str) and nom.casefold() == _QUOTA_PLAN_NAME:
            periode = plan.get("periode")
            return QuotaInfo(
                plan_nom=nom,
                periode=periode if isinstance(periode, str) else None,
                max_consultes=_as_optional_int(plan.get("maxConsultes")),
                consultes_restants=_as_optional_int(plan.get("consultesRestants")),
                consultes_realitzades=_as_optional_int(
                    plan.get("consultesRealitzades")
                ),
            )
    return None
