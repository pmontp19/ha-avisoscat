"""The data coordinator and the bus events of the avisoscat integration.

Two responsibilities sit here, on purpose together:

1. **Fetch and hold** the SMP snapshot for one comarca, preserving the last good
   state when the source fails (docs/04-architecture.md §10). A
   `DataUpdateCoordinator` with `always_update=False`, comparing snapshots by
   their projections only so a re-ordered but identical payload does not wake
   the entities every cycle.
2. **Translate projections into events**. `vigencia.py` answers *what* applies;
   this module answers *what changed since last cycle* and fires the six event
   types of docs/03-feature-spec.md §4 on `hass.bus`. Two horizons, two
   idempotent emission loops: one for the announced future, one for the in-force
   present, plus a dedicated loop for the real-time violent-weather nowcast.

The clock does work the source never reports: a warning enters and leaves force
when its 6-hour UTC band starts and ends, with the payload byte-identical. So a
once-a-minute recompute (`async_schedule_minute_recompute`) re-runs the
projection against the cached snapshot and re-emits in-force events **without
any fetch** (docs/04-architecture.md §5, docs/03-feature-spec.md §6). The
recompute updates `self.data` and notifies listeners directly rather than
through `async_set_updated_data`, which would reschedule the poll every minute
and starve it.

Events are deduplicated against memory that lives on the coordinator, not in the
state, so a frozen state can be compared for "did the projections change?" while
the "have we already announced this emission?" bookkeeping keeps its own
lifecycle. Both are bounded by the same purge discipline as `vigencia.py`'s
report memo: every cycle forgets the emissions the current snapshot no longer
carries, so the memory can never outgrow the live snapshot.

State on `entry.runtime_data`, never `hass.data` (docs/04-architecture.md §9).
Multi-entry by design: one coordinator per comarca, fully independent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.dt import utcnow

from .comarques import nom
from .const import (
    CONF_API_KEY,
    CONF_ID_COMARCA,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES,
    DEFAULT_SCAN_INTERVAL_IDLE_MINUTES,
    DEGRADED_FAILURE_THRESHOLD,
    DOMAIN,
    EVENT_SERVICE_DEGRADED,
    EVENT_VIOLENT_WEATHER,
    EVENT_WARNING_ANNOUNCED,
    EVENT_WARNING_CLEARED,
    EVENT_WARNING_DOWNGRADED,
    EVENT_WARNING_STARTED,
    EVENT_WARNING_UPGRADED,
    ISSUE_SERVICE_DEGRADED,
    LEARN_MORE_URL,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    QUOTA_HIGH_THRESHOLD,
    QUOTA_INTERVAL_MINUTES_HIGH,
    QUOTA_INTERVAL_MINUTES_LOW,
    QUOTA_INTERVAL_MINUTES_MEDIUM,
    QUOTA_MEDIUM_THRESHOLD,
)
from .models import SmpSnapshot
from .smp import ApiKeySource, PublicPageSource, SmpSource
from .vigencia import (
    AfectacioProjectada,
    OutlookDia,
    afectacions_anunciades,
    afectacions_vigents,
    outlook,
)

_LOGGER = logging.getLogger(__name__)

# One emission, the way docs/04-architecture.md §8 keys `announced_seen` and the
# way `vigencia._identitat_avis` already keys its own memo: the raw Catalan names
# rather than the enums, so an unrecognised literal still identifies itself
# instead of collapsing onto every other unrecognised one.
_EmissioKey = tuple[str, str, datetime | None]


@dataclass(eq=False)
class AvisoscatState:
    """What one cycle produced for the entities: three projections plus flags.

    `__eq__` compares the projections only, never `snapshot` or `last_error`,
    so `always_update=False` wakes the entities exactly when the warnings that
    apply changed, and never because the clock advanced or a fetch failed.
    """

    snapshot: SmpSnapshot | None
    en_vigor: list[AfectacioProjectada]
    anunciats: list[AfectacioProjectada]
    outlook: list[OutlookDia]
    last_error: str | None = None

    # Unhashable on purpose: a mutable state compared by projection value, never
    # used as a dict key or set member, so the explicit sentinel keeps the
    # default Python behaviour when `__eq__` is defined.
    __hash__: ClassVar[None] = None

    def __eq__(self, other: object) -> bool:
        """Equal when the three projections are equal, ignoring fetch metadata."""
        if not isinstance(other, AvisoscatState):
            return NotImplemented
        return (
            self.en_vigor == other.en_vigor
            and self.anunciats == other.anunciats
            and self.outlook == other.outlook
        )


def build_source(hass: HomeAssistant, entry: ConfigEntry) -> SmpSource:
    """Pick the SMP source a config entry uses: the API-key client when a key
    was validated at setup, the keyless public page otherwise.

    Centralised here so the config flow and the runtime agree, and so tests can
    swap it out without touching network code: the integration's own setup calls
    this, and the test fixture patches this reference to return a fake.
    """
    session = async_get_clientsession(hass)
    api_key = entry.data.get(CONF_API_KEY)
    if api_key:
        return ApiKeySource(session, api_key)
    return PublicPageSource(session)


def interval_for_quota(max_consultes: int) -> timedelta:
    """The polling interval a given monthly quota dictates (§6, "Amb API key").

    Bands mirror the spec: `> 500` keeps the 30 min public cadence, `200-500`
    widens to 2 h, and `<= 200` (the citizen plan, where `maxConsultes` sits at
    ~100) widens to 8 h. The 8 h floor is what makes the 2 h violent-nowcast
    horizon unservable on a citizen key, which the config flow warns about.
    """
    if max_consultes > QUOTA_HIGH_THRESHOLD:
        return timedelta(minutes=QUOTA_INTERVAL_MINUTES_HIGH)
    if max_consultes > QUOTA_MEDIUM_THRESHOLD:
        return timedelta(minutes=QUOTA_INTERVAL_MINUTES_MEDIUM)
    return timedelta(minutes=QUOTA_INTERVAL_MINUTES_LOW)


class AvisoscatDataUpdateCoordinator(DataUpdateCoordinator[AvisoscatState]):
    """Holds one comarca's snapshot and fires the events its changes imply."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        source: SmpSource,
    ) -> None:
        """Arm the coordinator for one comarca with an adaptive or fixed poll."""
        fixed = self._fixed_interval(entry)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=fixed
            or timedelta(minutes=DEFAULT_SCAN_INTERVAL_IDLE_MINUTES),
            config_entry=entry,
            always_update=False,
        )
        self._source = source
        self._id_comarca = int(entry.data[CONF_ID_COMARCA])
        self._comarca_nom = nom(self._id_comarca)
        self._fixed_interval = fixed
        # Dedup memory: what has already been reported, keyed for idempotency.
        self._announced_seen: set[_EmissioKey] = set()
        self._violent_seen: set[datetime] = set()
        # The previous in-force picture per meteor, the baseline `started` /
        # `upgraded` / `downgraded` / `cleared` diff against.
        self._in_force: dict[str, AfectacioProjectada] = {}
        self._initialised = False
        # Resilience bookkeeping (docs/04-architecture.md §10): how many
        # fetches in a row have failed, and whether the single
        # `avisoscat_service_degraded` event for the current streak has
        # already fired. Reset to this state on the next successful fetch.
        self._consecutive_failures = 0
        self._degraded_announced = False
        # Quota-driven interval (docs/03-feature-spec.md §6, "Amb API key"):
        # populated once on the first successful fetch with an API-key source,
        # after which it overrides the adaptive 30/10 min cadence. `None` for
        # the keyless public source, which stays on the adaptive logic.
        self._quota_interval: timedelta | None = None
        self._quota_checked = False

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    @property
    def consecutive_failures(self) -> int:
        """How many fetches in a row have failed (docs/04-architecture.md §10).

        Read-only exposure for diagnostics: the diagnostic download surfaces
        the streak without giving callers a way to mutate it.
        """
        return self._consecutive_failures

    @property
    def degraded_announced(self) -> bool:
        """Whether `avisoscat_service_degraded` has fired for this streak.

        Read-only exposure for diagnostics: distinguishes a streak that is
        still under the threshold from one the user has already been told
        about.
        """
        return self._degraded_announced

    @property
    def quota_interval(self) -> timedelta | None:
        """The quota-driven poll interval, or `None` for the public source.

        Read-only exposure for diagnostics: reports the cadence a citizen
        quota dictates when an API-key source is in use.
        """
        return self._quota_interval

    @property
    def source_kind(self) -> str:
        """The class name of the SMP source the coordinator fetches from.

        Used by diagnostics to distinguish `ApiKeySource` from
        `PublicPageSource` without exposing the source object itself.
        """
        return type(self._source).__name__

    async def _async_update_data(self) -> AvisoscatState:
        """Fetch one snapshot and fold it into a fresh state.

        On failure the last good projections stay: HA keeps `self.data` when
        `_async_update_data` raises, so the entities keep showing the last known
        warnings. The failure is recorded onto that state's `last_error` first,
        which is the detail the criterion checks and a future repair issue will
        carry. `ConfigEntryAuthFailed` propagates unchanged so HA offers reauth.
        """
        try:
            snapshot = await self._source.fetch()
        except ConfigEntryAuthFailed:
            # An auth failure is not "service degraded": the key is wrong, not
            # the source, and HA's own reauth flow is the right answer. It must
            # neither count towards the degradation threshold nor fire the
            # repair issue.
            raise
        except UpdateFailed as err:
            self._record_failure(err)
            raise
        except Exception as err:
            # Defensive: the source contract is UpdateFailed |
            # ConfigEntryAuthFailed, but an escape is still a fetch failure,
            # recorded and wrapped so the coordinator degrades rather than
            # crashing the refresh.
            self._record_failure(err)
            raise UpdateFailed("Unexpected error fetching the SMP feed") from err

        # A successful fetch ends any standing degradation streak and reopens
        # the door to the next one. The quota check is best-effort: quota is
        # diagnostic, not load-bearing, so a failure to read it never fails
        # the fetch that just succeeded.
        self._on_fetch_success()
        await self._maybe_apply_quota_interval()
        return self._apply(snapshot)

    def _record_failure(self, err: Exception) -> None:
        """Count the failure and fire `service_degraded` when it persists.

        Wraps `_record_error`'s old "stamp `last_error` onto the last good
        state" with the resilience bookkeeping: every fetch failure increments
        the consecutive counter, and once it reaches the documented threshold a
        single `avisoscat_service_degraded` event and a matching repair issue
        are produced. The fourth failure does not repeat either: the
        `_degraded_announced` flag stays set until a successful fetch clears
        the streak (docs/04-architecture.md §10).
        """
        if self.data is not None:
            self.data.last_error = str(err) or type(err).__name__
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= DEGRADED_FAILURE_THRESHOLD
            and not self._degraded_announced
        ):
            self._announce_degraded()

    def _on_fetch_success(self) -> None:
        """Reset the failure streak and clear any standing repair issue."""
        self._consecutive_failures = 0
        if self._degraded_announced:
            self._degraded_announced = False
            async_delete_issue(self.hass, DOMAIN, ISSUE_SERVICE_DEGRADED)

    def _announce_degraded(self) -> None:
        """Fire the degraded event once and create the matching repair issue.

        The event carries the count and the last error so a `trigger: event`
        automation can branch on the failure mode; the repair issue is what
        surfaces the problem to a user who has no such automation, with a
        `learn_more_url` that points at the project's documentation.
        """
        self._degraded_announced = True
        self._fire(
            EVENT_SERVICE_DEGRADED,
            {
                "comarca": self._comarca_nom,
                "id_comarca": self._id_comarca,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self.data.last_error if self.data is not None else None,
            },
        )
        async_create_issue(
            self.hass,
            domain=DOMAIN,
            issue_id=ISSUE_SERVICE_DEGRADED,
            is_fixable=False,
            issue_domain=DOMAIN,
            learn_more_url=LEARN_MORE_URL,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_SERVICE_DEGRADED,
            translation_placeholders={
                "comarca": self._comarca_nom,
                "id_comarca": str(self._id_comarca),
            },
        )

    # ------------------------------------------------------------------
    # Projection + emission, shared by the fetch and the minute recompute
    # ------------------------------------------------------------------

    def _apply(self, snapshot: SmpSnapshot) -> AvisoscatState:
        """Project the snapshot onto the clock, emit what changed, return state.

        Runs on every fetch and on every minute recompute, which is why it is
        network-free and why the emission loops are idempotent: re-running them
        against an unchanged projection fires nothing, because the dedup memory
        and the in-force baseline already reflect it.
        """
        now = utcnow()
        episodis = snapshot.episodis
        en_vigor = afectacions_vigents(episodis, self._id_comarca, now)
        anunciats = afectacions_anunciades(episodis, self._id_comarca, now)
        outlook_grid = outlook(episodis, self._id_comarca, now)

        if not self._initialised:
            # Quiet start (criterion 5): the first successful picture seeds the
            # dedup memory and the in-force baseline, so a setup that lands on
            # an already-active day does not replay every live warning as new.
            self._seed(en_vigor, anunciats)
            self._initialised = True
        else:
            self._emit_announced(anunciats)
            self._emit_in_force(en_vigor, now)
            self._emit_violent(en_vigor)
            self._purge_announced(anunciats)
            self._purge_violent(en_vigor)

        # The 10-minute cadence is justified only while something is happening
        # for this comarca (docs/03-feature-spec.md §6): a violent nowcast, an
        # in-force warning, or an announced one. A quiet day polls slowly.
        self._update_interval_for(bool(en_vigor) or bool(anunciats))

        return AvisoscatState(
            snapshot=snapshot,
            en_vigor=en_vigor,
            anunciats=anunciats,
            outlook=outlook_grid,
        )

    def _seed(
        self,
        en_vigor: list[AfectacioProjectada],
        anunciats: list[AfectacioProjectada],
    ) -> None:
        """Pre-load the dedup memory so the first cycle fires nothing."""
        for af in anunciats:
            self._announced_seen.add(self._emissio_key(af))
        self._in_force = {}
        for af in en_vigor:
            if af.is_temps_violent:
                self._violent_seen.add(af.data_emissio)  # type: ignore[arg-type]
            else:
                # Severe-first list, so the first per meteor is its peak.
                self._in_force.setdefault(af.meteor_nom, af)

    def _emit_announced(self, anunciats: list[AfectacioProjectada]) -> None:
        """Fire one `announced` per emission never seen before.

        One event per meteor carries that meteor's most severe announced band
        (the list is severe-first), deduplicated by the emission identity so a
        re-emission of the same content does not repeat and an ampliation with
        a fresh `data_emissio` does (docs/03-feature-spec.md §4.1).
        """
        for af in _per_meteor(anunciats).values():
            key = self._emissio_key(af)
            if key in self._announced_seen:
                continue
            self._announced_seen.add(key)
            self._fire(
                EVENT_WARNING_ANNOUNCED,
                _payload_announced(af, self._id_comarca, self._comarca_nom),
            )

    def _emit_in_force(
        self, en_vigor: list[AfectacioProjectada], now: datetime
    ) -> None:
        """Diff the in-force picture per meteor and fire start/grade/clear.

        Violent-weather nowcasts are excluded here: they never appear as a
        generic `started`, only as their own `avisoscat_violent_weather` event,
        which is the in-force signal for a warning that has no announce phase.
        """
        ordinary = _per_meteor(af for af in en_vigor if not af.is_temps_violent)
        for meteor_nom, af in ordinary.items():
            old = self._in_force.get(meteor_nom)
            if old is None:
                self._fire(
                    EVENT_WARNING_STARTED,
                    _payload_started(af, self._id_comarca, self._comarca_nom),
                )
            elif af.perill > old.perill:
                self._fire(
                    EVENT_WARNING_UPGRADED,
                    _payload_grade(af, old, self._id_comarca, self._comarca_nom),
                )
            elif af.perill < old.perill:
                self._fire(
                    EVENT_WARNING_DOWNGRADED,
                    _payload_grade(af, old, self._id_comarca, self._comarca_nom),
                )
        for meteor_nom, old in self._in_force.items():
            if meteor_nom not in ordinary:
                self._fire(
                    EVENT_WARNING_CLEARED,
                    _payload_cleared(old, now, self._id_comarca, self._comarca_nom),
                )
        self._in_force = ordinary

    def _emit_violent(self, en_vigor: list[AfectacioProjectada]) -> None:
        """Fire `violent_weather` once per nowcast emission.

        The clock can open a pre-fetched violent window (a nowcast whose
        `data_emissio` the last poll already carried), so this runs on the minute
        recompute too. Dedup by `data_emissio` fires it once and repeats only
        when the SMC issues a fresh nowcast (criterion 6).
        """
        for af in en_vigor:
            if not af.is_temps_violent:
                continue
            if af.data_emissio in self._violent_seen:
                continue
            self._violent_seen.add(af.data_emissio)  # type: ignore[arg-type]
            self._fire(
                EVENT_VIOLENT_WEATHER,
                _payload_violent(af, self._id_comarca, self._comarca_nom),
            )

    def _purge_announced(self, anunciats: list[AfectacioProjectada]) -> None:
        """Forget announced emissions the snapshot no longer carries."""
        current = {self._emissio_key(af) for af in anunciats}
        self._announced_seen &= current

    def _purge_violent(self, en_vigor: list[AfectacioProjectada]) -> None:
        """Forget violent emissions no longer in force."""
        current = {
            af.data_emissio  # type: ignore[misc]
            for af in en_vigor
            if af.is_temps_violent
        }
        self._violent_seen &= current

    @staticmethod
    def _emissio_key(af: AfectacioProjectada) -> _EmissioKey:
        """Identity of one emission: meteor, type, issue instant."""
        return (af.meteor_nom, af.tipus_nom, af.data_emissio)

    def _update_interval_for(self, active: bool) -> None:
        """Set the poll cadence in the priority order fixed > quota > adaptive.

        A user-chosen fixed interval always wins. With an API key, the
        quota-driven interval (§6, "Amb API key") comes next: a citizen plan
        cannot honour the 10 min nowcast cadence, so the interval widens to
        keep the month inside the quota. The adaptive 30/10 min logic is the
        default for the keyless public source.
        """
        if self._fixed_interval is not None:
            target = self._fixed_interval
        elif self._quota_interval is not None:
            target = self._quota_interval
        elif active:
            target = timedelta(minutes=DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES)
        else:
            target = timedelta(minutes=DEFAULT_SCAN_INTERVAL_IDLE_MINUTES)
        # The setter stores only; the new cadence takes effect at the next
        # refresh boundary, never mid-cycle.
        if self.update_interval != target:
            self.update_interval = target

    async def _maybe_apply_quota_interval(self) -> None:
        """Read the API quota once and pin the poll interval to it (§6).

        Runs only on the first successful fetch and only when the source is the
        API-key client: the public source has no quota and stays on the
        adaptive 30/10 min logic. A failed quota read is silent: the quota
        sensor is a diagnostic, not a load-bearing input, so the integration
        keeps working on the adaptive cadence when it cannot be read.
        """
        if self._quota_checked or not isinstance(self._source, ApiKeySource):
            return
        self._quota_checked = True
        try:
            quota = await self._source.fetch_quota()
        except Exception:
            # Quota is diagnostic, never worth failing the fetch over.
            return
        if quota is None or quota.max_consultes is None:
            return
        self._quota_interval = interval_for_quota(quota.max_consultes)

    @staticmethod
    def _fixed_interval(entry: ConfigEntry) -> timedelta | None:
        """The user-chosen fixed interval, clamped, or `None` for adaptive."""
        minutes = entry.options.get(CONF_SCAN_INTERVAL)
        if minutes is None:
            return None
        clamped = max(
            MIN_SCAN_INTERVAL_MINUTES,
            min(MAX_SCAN_INTERVAL_MINUTES, int(minutes)),
        )
        return timedelta(minutes=clamped)

    def _fire(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fire one bus event with its schema (docs/03-feature-spec.md §4)."""
        self.hass.bus.async_fire(event_type, payload)

    # ------------------------------------------------------------------
    # Minute recompute, network-free
    # ------------------------------------------------------------------

    @callback
    def async_schedule_minute_recompute(self, _now: datetime) -> None:
        """Re-evaluate validity against the cached snapshot, without a fetch.

        Registered through `async_track_time_change(..., second=0)` so the 6-hour
        band transitions drive `started` / `cleared` on the minute, between
        polls. The work is offloaded to a task so the time-change listener stays
        a cheap callback.
        """
        self.hass.async_create_task(self._async_recompute_against_cache())

    async def _async_recompute_against_cache(self) -> None:
        """Re-project the last snapshot and notify when validity changed.

        Notifies through `async_update_listeners` directly, never
        `async_set_updated_data`: the latter reschedules the poll, and calling it
        every minute would push the next fetch forward forever. The projections
        changed check is the state's own `__eq__`, so entities wake only when a
        band actually transitioned.
        """
        if self.data is None or self.data.snapshot is None:
            return
        refreshed = self._apply(self.data.snapshot)
        # A recompute never fetches, so it must never reset the failure flag.
        refreshed.last_error = self.data.last_error
        if refreshed != self.data:
            self.data = refreshed
            self.async_update_listeners()


def _per_meteor(
    afectacions: Iterable[AfectacioProjectada],
) -> dict[str, AfectacioProjectada]:
    """One projection per meteor: the most severe, since lists arrive severe-first."""
    by_meteor: dict[str, AfectacioProjectada] = {}
    for af in afectacions:
        by_meteor.setdefault(af.meteor_nom, af)
    return by_meteor


# ---------------------------------------------------------------------------
# Event payloads (docs/03-feature-spec.md §4)
#
# Every field the spec lists, in the order it lists them. External text
# (`meteor_nom`, `llindar`, `comentari`, `distribucio_geografica`) is copied
# verbatim from the projection, never escaped here: rendering is the consumer's
# job (docs/04-architecture.md §11).
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """ISO 8601 of a timestamp, `None` preserved for the absent case."""
    return value.isoformat() if value is not None else None


def _payload_announced(
    af: AfectacioProjectada, id_comarca: int, comarca: str
) -> dict[str, Any]:
    """`avisoscat_warning_announced` (§4.1)."""
    return {
        "comarca": comarca,
        "id_comarca": id_comarca,
        "meteor": af.meteor.value if af.meteor else None,
        "meteor_nom": af.meteor_nom,
        "tipus": af.tipus.value if af.tipus else None,
        "perill": af.perill,
        "nivell_text": af.nivell_perill.value,
        "nivell": af.nivell,
        "llindar": af.llindar,
        "comenca": _iso(af.inici),
        "hores_per_endavant": af.hores_per_endavant,
        "dia": af.etiqueta_dia,
        "periode": af.periode,
        "distribucio_geografica": af.distribucio_geografica,
        "comentari": af.comentari,
        "data_emissio": _iso(af.data_emissio),
        "data_inici": _iso(af.data_inici),
        "data_fi": _iso(af.data_fi),
    }


def _payload_started(
    af: AfectacioProjectada, id_comarca: int, comarca: str
) -> dict[str, Any]:
    """`avisoscat_warning_started` (§4.2), with the real notice it gave."""
    return {
        "comarca": comarca,
        "id_comarca": id_comarca,
        "meteor": af.meteor.value if af.meteor else None,
        "meteor_nom": af.meteor_nom,
        "tipus": af.tipus.value if af.tipus else None,
        "perill": af.perill,
        "nivell_text": af.nivell_perill.value,
        "nivell": af.nivell,
        "llindar": af.llindar,
        "periode": af.periode,
        "distribucio_geografica": af.distribucio_geografica,
        "comentari": af.comentari,
        "data_inici": _iso(af.data_inici),
        "data_fi": _iso(af.data_fi),
        "data_emissio": _iso(af.data_emissio),
        "anunciat_amb_hores": af.anunciat_amb_hores,
    }


def _payload_grade(
    af: AfectacioProjectada,
    old: AfectacioProjectada,
    id_comarca: int,
    comarca: str,
) -> dict[str, Any]:
    """`avisoscat_warning_upgraded` / `_downgraded` (§4.3)."""
    return {
        "comarca": comarca,
        "id_comarca": id_comarca,
        "meteor": af.meteor.value if af.meteor else None,
        "perill_anterior": old.perill,
        "perill": af.perill,
        "nivell_text_anterior": old.nivell_perill.value,
        "nivell_text": af.nivell_perill.value,
        "periode": af.periode,
        "llindar": af.llindar,
    }


def _payload_cleared(
    old: AfectacioProjectada, now: datetime, id_comarca: int, comarca: str
) -> dict[str, Any]:
    """`avisoscat_warning_cleared` (§4.4) with its reason and live duration."""
    return {
        "comarca": comarca,
        "id_comarca": id_comarca,
        "meteor": old.meteor.value if old.meteor else None,
        "perill_final": old.perill,
        "durada_min": _durada_min(old, now),
        "motiu": _motiu(old, now),
    }


def _payload_violent(
    af: AfectacioProjectada, id_comarca: int, comarca: str
) -> dict[str, Any]:
    """`avisoscat_violent_weather` (§4.5).

    `probabilitat` is derived from the grade: like `vigencia.py`'s violent
    projection, there is no captured temps-violent payload yet, so the real
    field the SMC carries is unverified. The grade-to-probability mapping is the
    safe reading and degrades cleanly if the real data is coarser.
    """
    return {
        "comarca": comarca,
        "id_comarca": id_comarca,
        "probabilitat": _probabilitat(af.perill),
        "llindar": af.llindar,
        "comentari": af.comentari,
        "data_emissio": _iso(af.data_emissio),
        "valid_fins": _iso(af.fi),
    }


def _probabilitat(perill: int) -> str:
    """Grade to probability label, the safe reading of an unverified field."""
    if perill >= 5:
        return "alta"
    if perill >= 3:
        return "moderada"
    return "baixa"


def _durada_min(old: AfectacioProjectada, now: datetime) -> int:
    """Whole minutes the warning was in force, capped at its own end.

    For an `expirat` clearance the clock has already passed `fi`, so reporting
    `now - inici` would count the detection lag as in-force time; capping at `fi`
    reports the warning's true duration instead.
    """
    end = old.fi if (old.fi is not None and now > old.fi) else now
    seconds = (end - old.inici).total_seconds()
    return max(0, int(seconds // 60))


def _motiu(old: AfectacioProjectada, now: datetime) -> str:
    """Why the warning left force: its time ran out, or the source withdrew it."""
    if old.fi is not None and now >= old.fi:
        return "expirat"
    return "retirat"
