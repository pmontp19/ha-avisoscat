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
    DOMAIN,
    EVENT_VIOLENT_WEATHER,
    EVENT_WARNING_ANNOUNCED,
    EVENT_WARNING_CLEARED,
    EVENT_WARNING_DOWNGRADED,
    EVENT_WARNING_STARTED,
    EVENT_WARNING_UPGRADED,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
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

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

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
            raise
        except UpdateFailed as err:
            self._record_error(err)
            raise
        except Exception as err:
            # Defensive: the source contract is UpdateFailed |
            # ConfigEntryAuthFailed, but an escape is still a fetch failure,
            # recorded and wrapped so the coordinator degrades rather than
            # crashing the refresh.
            self._record_error(err)
            raise UpdateFailed("Unexpected error fetching the SMP feed") from err
        return self._apply(snapshot)

    def _record_error(self, err: Exception) -> None:
        """Stamp the failure onto the last good state, which HA then retains."""
        if self.data is not None:
            self.data.last_error = str(err) or type(err).__name__

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
        """Set the poll cadence: the configured fixed interval, else adaptive."""
        if self._fixed_interval is not None:
            target = self._fixed_interval
        elif active:
            target = timedelta(minutes=DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES)
        else:
            target = timedelta(minutes=DEFAULT_SCAN_INTERVAL_IDLE_MINUTES)
        # The setter stores only; the new cadence takes effect at the next
        # refresh boundary, never mid-cycle.
        if self.update_interval != target:
            self.update_interval = target

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
