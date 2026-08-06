"""Warning validity: what applies *now*, what is only announced, what is coming.

The SMP tells you *which* warnings exist; this module decides *when* each one
applies, by crossing a warning's own start and end with the 6-hour UTC band the
clock is currently in (docs/04-architecture.md §5).

That has a consequence the whole integration is built on: **a warning starts and
stops without the source changing at all.** At 12:00 UTC a warning whose only
affected band is `12-18` becomes live even though the payload is byte-identical
to five minutes earlier. This is why the coordinator can poll slowly and still
fire its events on time, and why `__init__.py` recomputes every minute without
touching the network.

The two horizons of docs/03-feature-spec.md §1.1, which are two projections of
the same snapshot separated only by the clock:

- **In force** (`Horitzo.VIGENT`): the affected band contains this instant and
  the warning's own end has not passed.
- **Announced** (`Horitzo.ANUNCIAT`): the warning has been issued and applies
  later (later today, tomorrow or the day after) but its band has not started.

A wind warning issued Tuesday for Thursday afternoon is announced on Tuesday
*and* in force on Thursday at 16:00. Both moments are reportable, and both are
actionable by a different automation.

Everything here is UTC. `now_utc` is normalised on the way in (a naive value is
read as UTC, an aware one is converted), so a local-time offset can never leak
into a band comparison. No Home Assistant import and no I/O: like `models.py`,
this is pure Python over already-typed objects.

Three deliberate deviations from the sketch in docs/04-architecture.md §5:

- One `AfectacioProjectada` instead of an `AfectacioVigent`, because an in-force
  and an announced affectation carry exactly the same payload and differ only in
  which side of the clock they fall on. A class named "vigent" holding an
  announced affectation would lie; the `horitzo` field says which one it is.
- A third horizon, `Horitzo.PASSAT`, exists so `outlook()` can still report the
  grade of a band that has already gone by today. It is never returned by either
  headline projection.
- Band names are parsed rather than looked up, so the `"18-24"` spelling of the
  SMC's written documentation still resolves even though the JSON says `"18-00"`
  (docs/01-data-sources.md §1.2). Both fold onto the canonical `"18-00"`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Final

from .models import (
    Afectacio,
    Avis,
    Episodi,
    Evolucio,
    Meteor,
    NivellPerill,
    Preavis,
    TipusAvis,
)

_LOGGER = logging.getLogger(__name__)

# The four 6-hour UTC bands, in order, keyed exactly as the JSON keys them: the
# last one is `"18-00"`, not the `"18-24"` of the written SMC documentation
# (docs/01-data-sources.md §1.2). The values are the half-open hour range, so
# `18-00` is 18:00 up to but not including the next midnight, which is what
# "covers 18:00 to 23:59 UTC" means once seconds exist.
PERIODES: Final[dict[str, tuple[int, int]]] = {
    "00-06": (0, 6),
    "06-12": (6, 12),
    "12-18": (12, 18),
    "18-00": (18, 24),
}

_PERIODE_NOMS: Final[tuple[str, ...]] = tuple(PERIODES)
_PERIODE_PER_HORES: Final[dict[tuple[int, int], str]] = {
    hores: nom for nom, hores in PERIODES.items()
}

_HORES_PER_PERIODE: Final = 24 // len(PERIODES)

# An `Avís Vigilància per Temps Violent` does not follow bands at all: it is
# valid for two hours from its issue time (docs/01-data-sources.md §1.5).
FINESTRA_TEMPS_VIOLENT: Final = timedelta(hours=2)

# The SMP forecasts the present day plus two (docs/01-data-sources.md §1.5).
DIES_OUTLOOK: Final = 3

# Payload literals for the relative day, fixed by docs/03-feature-spec.md §4.1.
# They are data the events carry, not translated user-facing text.
_ETIQUETES_DIA: Final[tuple[str, ...]] = ("avui", "dema", "dema_passat")


class Horitzo(StrEnum):
    """Where an affectation falls relative to the current instant."""

    VIGENT = "vigent"  # its band contains now
    ANUNCIAT = "anunciat"  # issued, its band has not started yet
    PASSAT = "passat"  # its band is over


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Normalise an instant to UTC; a naive value is read as UTC.

    Every comparison in this module goes through here, which is what keeps the
    official local time (UTC+1 in winter, UTC+2 in summer) out of the band
    arithmetic.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def periode_actual(now_utc: datetime) -> str:
    """Name of the 6-hour band containing `now_utc`.

    The four bands tile the day in equal steps, so the hour indexes them
    directly and there is no unreachable fall-through.
    """
    return _PERIODE_NOMS[_as_utc(now_utc).hour // _HORES_PER_PERIODE]


def _periode_hores(nom: str) -> tuple[int, int] | None:
    """Half-open hour range of a band name; `None` when it is unusable.

    The known names are a plain lookup. Anything else is parsed as `HH-HH`, so
    the `"18-24"` spelling of the written documentation resolves to the same
    range as the `"18-00"` the JSON actually sends, and a band the SMC invents
    later still places its affectations instead of dropping them.

    An unusable name is reported at debug level, not at warning: this runs on the
    once-a-minute recompute of `__init__.py`, so a single malformed field would
    otherwise repeat the same line ~1440 times a day per config entry. The
    payload-level report belongs to `models.py`, which logs once per fetch.
    """
    known = PERIODES.get(nom)
    if known is not None:
        return known
    inici, _, fi = nom.partition("-")
    try:
        hora_inici, hora_fi = int(inici), int(fi)
    except ValueError:
        hora_inici, hora_fi = -1, -1
    # A band ending at midnight is written as hour 0 by the JSON and as hour 24
    # by the documentation; both mean the end of the day.
    hora_fi = hora_fi or 24
    if 0 <= hora_inici < hora_fi <= 24:
        return hora_inici, hora_fi
    _LOGGER.debug("Unusable SMP time band %r, ignoring its affectations", nom)
    return None


def _periode_canonic(hores: tuple[int, int]) -> str:
    """Canonical name of an hour range, so the outlook grid has stable keys."""
    canonic = _PERIODE_PER_HORES.get(hores)
    if canonic is not None:
        return canonic
    inici, fi = hores
    return f"{inici:02d}-{fi % 24:02d}"


def periode_bounds(dia: date, periode: str) -> tuple[datetime, datetime] | None:
    """Half-open UTC interval of a band on a day; `None` for an unusable name.

    The end is exclusive, so `18-00` ends at the next midnight: that is the
    instant the band stops applying, and it is what makes 23:59:59 still count.
    """
    hores = _periode_hores(periode)
    if hores is None:
        return None
    return _bounds(dia, hores)


def _bounds(dia: date, hores: tuple[int, int]) -> tuple[datetime, datetime]:
    """Half-open UTC interval of an already-validated hour range."""
    hora_inici, hora_fi = hores
    mitjanit = datetime.combine(dia, time(), tzinfo=UTC)
    return mitjanit + timedelta(hours=hora_inici), mitjanit + timedelta(hours=hora_fi)


def etiqueta_dia(dies_per_endavant: int) -> str:
    """`avui` / `dema` / `dema_passat`, or the day offset outside that horizon.

    The SMP never forecasts past the third day, so the numeric fall-through is
    there to stay readable if it ever does, not because it is expected.
    """
    if 0 <= dies_per_endavant < len(_ETIQUETES_DIA):
        return _ETIQUETES_DIA[dies_per_endavant]
    return str(dies_per_endavant)


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AfectacioProjectada:
    """One affectation of one comarca, placed on the clock.

    Carries what the entity and event layers need without making them walk the
    snapshot again: the affectation's own grade and threshold, the warning it
    came from, and the effective interval during which it applies. That interval
    is the band clipped by the warning's own `dataInici`/`dataFi`, which is why
    a warning ending mid-band stops being in force at its end and not at the
    band's end.
    """

    horitzo: Horitzo
    id_comarca: int
    meteor: Meteor | None
    meteor_nom: str
    tipus: TipusAvis | None
    tipus_nom: str
    perill: int  # 0-6
    nivell: int  # 1 = low threshold, 2 = high threshold
    llindar: str
    auxiliar: bool
    dia: date  # day it applies to; the day it is read in for a live nowcast
    periode: str  # canonical band name; the band of the issue time for nowcasts
    inici: datetime  # effective start, band start clipped by `dataInici`
    fi: datetime  # effective end, exclusive: band end clipped by `dataFi`
    dies_per_endavant: int
    hores_per_endavant: int
    comentari: str
    distribucio_geografica: str | None
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None

    @property
    def nivell_perill(self) -> NivellPerill:
        """Traffic-light category of this affectation's grade."""
        return NivellPerill.from_perill(self.perill)

    @property
    def is_temps_violent(self) -> bool:
        """Whether this comes from a violent-weather nowcast."""
        return self.tipus is TipusAvis.TEMPS_VIOLENT

    @property
    def etiqueta_dia(self) -> str:
        """`avui` / `dema` / `dema_passat` for the day it applies to."""
        return etiqueta_dia(self.dies_per_endavant)

    @property
    def anunciat_amb_hores(self) -> int | None:
        """Real notice: whole hours between the emission and the entry in force.

        `None` when the emission time is missing. This is what distinguishes a
        warning planned days ahead from a last-minute one inside the same
        automation (docs/03-feature-spec.md §4.2).
        """
        if self.data_emissio is None:
            return None
        return _hores_entre(_as_utc(self.data_emissio), self.inici)


def _hores_entre(des_de: datetime, fins_a: datetime) -> int:
    """Whole hours from one instant to a later one; never negative.

    Truncated, not rounded: "3 hours ahead" must not be reported for something
    starting in 2 h 40 min.
    """
    segons = (fins_a - des_de).total_seconds()
    return int(max(segons, 0.0) // 3600)


def _horitzo(now: datetime, inici: datetime, fi: datetime) -> Horitzo:
    """Which side of the clock a half-open interval falls on."""
    if now < inici:
        return Horitzo.ANUNCIAT
    if now < fi:
        return Horitzo.VIGENT
    return Horitzo.PASSAT


def _dia_afectacio(afectacio: Afectacio, evolucio: Evolucio, avis: Avis) -> date | None:
    """Day an affectation belongs to, falling back rather than dropping it.

    Both `dia` fields are `date | None` because an unparseable day must not
    discard the affectation (`models.py`), so the warning's own start is the last
    resort. Without any of the three there is no interval to compute and the
    affectation really is unusable.
    """
    if afectacio.dia is not None:
        return afectacio.dia
    if evolucio.dia is not None:
        return evolucio.dia
    if avis.data_inici is not None:
        return _as_utc(avis.data_inici).date()
    return None


def _interval_efectiu(
    dia: date, hores: tuple[int, int], avis: Avis
) -> tuple[datetime, datetime] | None:
    """The band clipped by the warning's own start and end.

    `None` when the clipping leaves nothing, which is how a warning ending
    mid-day silently stops covering the bands after its end: the feed keeps
    sending those bands, they simply no longer apply.
    """
    inici, fi = _bounds(dia, hores)
    if avis.data_inici is not None:
        inici = max(inici, _as_utc(avis.data_inici))
    if avis.data_fi is not None:
        fi = min(fi, _as_utc(avis.data_fi))
    if inici >= fi:
        return None
    return inici, fi


def _llindar_del_dia(evolucio: Evolucio, nivell: int) -> str:
    """Threshold text to fall back on when the affectation carries none.

    The day carries the two thresholds it was computed against (`llindar1` /
    `llindar2`), so the one matching the affectation's level is the right
    fallback: reporting the low threshold for a high-threshold affectation would
    understate the warning.
    """
    llindar = evolucio.llindar_alt if nivell >= 2 else evolucio.llindar_baix
    return llindar or ""


def _projecta(
    *,
    episodi: Episodi,
    avis: Avis,
    evolucio: Evolucio,
    afectacio: Afectacio,
    periode: str,
    dia: date,
    inici: datetime,
    fi: datetime,
    now: datetime,
) -> AfectacioProjectada:
    """Assemble one projection, resolving its horizon against `now`."""
    return AfectacioProjectada(
        horitzo=_horitzo(now, inici, fi),
        id_comarca=afectacio.id_comarca,
        meteor=episodi.meteor,
        meteor_nom=episodi.meteor_nom,
        tipus=avis.tipus,
        tipus_nom=avis.tipus_nom,
        perill=afectacio.perill,
        nivell=afectacio.nivell,
        llindar=afectacio.llindar or _llindar_del_dia(evolucio, afectacio.nivell),
        auxiliar=afectacio.auxiliar,
        dia=dia,
        periode=periode,
        inici=inici,
        fi=fi,
        dies_per_endavant=(dia - now.date()).days,
        hores_per_endavant=_hores_entre(now, inici),
        comentari=evolucio.comentari,
        distribucio_geografica=evolucio.distribucio_geografica,
        data_emissio=avis.data_emissio,
        data_inici=avis.data_inici,
        data_fi=avis.data_fi,
    )


def _afectacions_de_la_comarca(
    avis: Avis, id_comarca: int
) -> Iterator[tuple[Evolucio, str, Afectacio]]:
    """Every affectation of this warning that names `id_comarca`.

    The comarca is matched by identity, never by name, and the maritime zones
    (`idComarca` 88-99) are just other ids: a caller wanting the adjacent sea
    asks for its id (docs/03-feature-spec.md §3.7).
    """
    for evolucio in avis.evolucions:
        for nom, afectacions in evolucio.periodes.items():
            for afectacio in afectacions:
                if afectacio.id_comarca == id_comarca:
                    yield evolucio, nom, afectacio


def _projecta_bandes(
    episodi: Episodi,
    avis: Avis,
    candidates: Iterable[tuple[Evolucio, str, Afectacio]],
    now: datetime,
) -> list[AfectacioProjectada]:
    """Project an ordinary warning: one entry per affected day and band.

    Like `_periode_hores`, the tolerance paths here log at debug level because
    this walk repeats every minute for every configured comarca.
    """
    projeccions_avis: list[AfectacioProjectada] = []
    for evolucio, nom, afectacio in candidates:
        hores = _periode_hores(nom)
        if hores is None:
            continue
        dia = _dia_afectacio(afectacio, evolucio, avis)
        if dia is None:
            _LOGGER.debug(
                "Undatable SMP affectation for comarca %s in band %s, ignoring it",
                afectacio.id_comarca,
                nom,
            )
            continue
        interval = _interval_efectiu(dia, hores, avis)
        if interval is None:
            continue
        inici, fi = interval
        projeccions_avis.append(
            _projecta(
                episodi=episodi,
                avis=avis,
                evolucio=evolucio,
                afectacio=afectacio,
                periode=_periode_canonic(hores),
                dia=dia,
                inici=inici,
                fi=fi,
                now=now,
            )
        )
    return projeccions_avis


def _projecta_temps_violent(
    episodi: Episodi,
    avis: Avis,
    candidates: Iterable[tuple[Evolucio, str, Afectacio]],
    now: datetime,
) -> list[AfectacioProjectada]:
    """Project a violent-weather nowcast: two hours from the issue time.

    Its own case rather than a bent band, because it shares none of the band
    logic: the window comes from `dataEmisio`, the bands the feed happens to
    list it under are irrelevant, and it is **never announced** - by the time it
    exists it is already in force, so a window that has not opened yet (a
    future-dated emission, i.e. clock skew) reports nothing rather than
    pretending to be a forecast.

    The listed bands collapse to a single projection, the most severe one, so a
    nowcast repeated across two bands is not counted as two live warnings.

    **`dia` is the day the window is read in, not the day of the emission**,
    which is the one place in this module where the relative day is not plain
    arithmetic over the affectation's own date. A nowcast issued at 23:30 and
    read at 00:30 is in force *today*: `(inici.date() - now.date()).days` would
    label it `-1`, outside the `avui`/`dema`/`dema_passat` payload enumeration of
    docs/03-feature-spec.md §4.1, and a forward-looking label never meant
    anything for a warning that is never announced. A window that has already
    closed keeps the day it was issued on.

    The comarca filter runs first: a nowcast that never names the comarca must be
    silent for that config entry, including its missing-issue-time report, which
    is at debug level because this runs on the once-a-minute recompute.
    """
    triples = list(candidates)
    if not triples:
        return []
    emissio = avis.data_emissio or avis.data_inici
    if emissio is None:
        _LOGGER.debug(
            "Violent-weather warning %r without an issue time, ignoring it",
            avis.tipus_nom,
        )
        return []
    evolucio, _nom, afectacio = max(
        triples, key=lambda triple: (triple[2].perill, triple[2].nivell)
    )
    inici = _as_utc(emissio)
    fi = inici + FINESTRA_TEMPS_VIOLENT
    if now < inici:
        _LOGGER.debug(
            "Violent-weather warning issued in the future (%s), not announcing it",
            inici.isoformat(),
        )
        return []
    return [
        _projecta(
            episodi=episodi,
            avis=avis,
            evolucio=evolucio,
            afectacio=afectacio,
            # The band containing the issue instant, for reporting only: the
            # window itself can spill into the next band and still be in force.
            periode=periode_actual(inici),
            dia=now.date() if now < fi else inici.date(),
            inici=inici,
            fi=fi,
            now=now,
        )
    ]


def projeccions(
    episodis: Sequence[Episodi], id_comarca: int, now_utc: datetime
) -> list[AfectacioProjectada]:
    """Every affectation of `id_comarca`, each placed on the clock.

    The single walk the two horizons and the outlook are all derived from: a
    caller needing more than one of them walks once here and filters the result
    with `afectacions_per_horitzo`. Ordered by start instant, most severe first
    within the same instant.

    `episodis` is a `Sequence` and not merely an `Iterable` because every entry
    point that can be called repeatedly over the same argument walks it again; a
    one-shot generator would silently answer "nothing" the second time, which in
    a warning integration is a wrong answer rather than an error.

    Explicitly closed episodes and emissions are skipped, and nothing else is:
    an unknown `estat` counts as open (`models.py` trap #1) and grade 0 is kept,
    because a grade that failed to parse also reads as 0 and dropping it here
    would lose a real affectation. Filtering by grade is the consumer's call.
    """
    now = _as_utc(now_utc)
    resultat: list[AfectacioProjectada] = []
    for episodi in episodis:
        if not episodi.is_open:
            continue
        for avis in episodi.avisos:
            if not avis.is_open:
                continue
            candidates = _afectacions_de_la_comarca(avis, id_comarca)
            resultat.extend(
                _projecta_temps_violent(episodi, avis, candidates, now)
                if avis.tipus is TipusAvis.TEMPS_VIOLENT
                else _projecta_bandes(episodi, avis, candidates, now)
            )
    return sorted(resultat, key=_ordre_cronologic)


def _ordre_cronologic(afectacio: AfectacioProjectada) -> tuple[datetime, int, str, str]:
    """Sort key: soonest first, most severe first, then stable on the names."""
    return (
        afectacio.inici,
        -afectacio.perill,
        afectacio.meteor_nom,
        afectacio.periode,
    )


def _ordre_severitat(afectacio: AfectacioProjectada) -> tuple[int, int, datetime, str]:
    """Sort key: most severe first, so `[0]` is the peak of a projection list."""
    return (
        -afectacio.perill,
        -afectacio.nivell,
        afectacio.inici,
        afectacio.meteor_nom,
    )


def afectacions_vigents(
    episodis: Sequence[Episodi], id_comarca: int, now_utc: datetime
) -> list[AfectacioProjectada]:
    """Affectations applying to `id_comarca` **right now**, most severe first.

    In force means the affected band contains this instant *and* the warning's
    own end has not passed. A violent-weather nowcast is in force for the two
    hours following its emission instead.
    """
    return afectacions_per_horitzo(
        projeccions(episodis, id_comarca, now_utc), Horitzo.VIGENT
    )


def afectacions_anunciades(
    episodis: Sequence[Episodi], id_comarca: int, now_utc: datetime
) -> list[AfectacioProjectada]:
    """Affectations issued for `id_comarca` but **not yet in force**.

    Later today, tomorrow or the day after: the other half of the value of this
    integration (docs/03-feature-spec.md §1.1 and §3.3). Most severe first, so
    `[0]` is the announced peak; the earliest start is `min(af.inici ...)`.

    A violent-weather nowcast never appears here.
    """
    return afectacions_per_horitzo(
        projeccions(episodis, id_comarca, now_utc), Horitzo.ANUNCIAT
    )


def afectacions_per_horitzo(
    afectacions: Iterable[AfectacioProjectada], horitzo: Horitzo
) -> list[AfectacioProjectada]:
    """Pick one horizon out of an already-computed projection, severest first."""
    return sorted(
        (af for af in afectacions if af.horitzo is horitzo), key=_ordre_severitat
    )


def pic(afectacions: Iterable[AfectacioProjectada]) -> AfectacioProjectada | None:
    """The most severe affectation of a collection, `None` when it is empty."""
    return min(afectacions, key=_ordre_severitat, default=None)


# ---------------------------------------------------------------------------
# Three-day outlook
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutlookPeriode:
    """One cell of the outlook grid: one band of one day."""

    periode: str
    inici: datetime
    fi: datetime
    afectacions: tuple[AfectacioProjectada, ...]

    @property
    def perill_maxim(self) -> int:
        """Highest grade in this band, 0 when nothing applies to it."""
        return max((af.perill for af in self.afectacions), default=0)

    @property
    def pic(self) -> AfectacioProjectada | None:
        """The most severe affectation of this band, `None` when there is none."""
        return pic(self.afectacions)


@dataclass(frozen=True, slots=True)
class OutlookDia:
    """One day of the outlook, always split into all four bands."""

    dia: date
    dies_per_endavant: int
    periodes: tuple[OutlookPeriode, ...]

    @property
    def etiqueta(self) -> str:
        """`avui` / `dema` / `dema_passat`."""
        return etiqueta_dia(self.dies_per_endavant)

    @property
    def graella(self) -> dict[str, int]:
        """Grade of each band, zero included: the `graella` attribute of §3.4."""
        return {periode.periode: periode.perill_maxim for periode in self.periodes}

    @property
    def perill_maxim(self) -> int:
        """Highest grade of any band of this day."""
        return max((periode.perill_maxim for periode in self.periodes), default=0)

    @property
    def pic(self) -> AfectacioProjectada | None:
        """The most severe affectation of the whole day."""
        return pic(af for periode in self.periodes for af in periode.afectacions)


def outlook(
    episodis: Sequence[Episodi],
    id_comarca: int,
    now_utc: datetime,
    *,
    dies: int = DIES_OUTLOOK,
) -> list[OutlookDia]:
    """The day-by-band grid for today and the next two days.

    Always the four bands of each of the days, with grade 0 where nothing
    applies, because the grid is a forecast display: a missing cell and a calm
    cell are different statements (docs/03-feature-spec.md §3.4).

    A cell collects the affectations whose effective interval *overlaps* it
    rather than the ones whose band name matches, which is what lets a
    violent-weather window straddling two bands appear in both.
    """
    now = _as_utc(now_utc)
    afectacions = projeccions(episodis, id_comarca, now_utc)
    return [
        _outlook_dia(
            now.date() + timedelta(days=desplacament), desplacament, afectacions
        )
        for desplacament in range(dies)
    ]


def _outlook_dia(
    dia: date, dies_per_endavant: int, afectacions: Sequence[AfectacioProjectada]
) -> OutlookDia:
    """Build one day of the grid, band by band."""
    periodes = []
    for nom, hores in PERIODES.items():
        inici, fi = _bounds(dia, hores)
        periodes.append(
            OutlookPeriode(
                periode=nom,
                inici=inici,
                fi=fi,
                afectacions=tuple(
                    af for af in afectacions if af.inici < fi and af.fi > inici
                ),
            )
        )
    return OutlookDia(
        dia=dia, dies_per_endavant=dies_per_endavant, periodes=tuple(periodes)
    )


# ---------------------------------------------------------------------------
# Pre-warnings
# ---------------------------------------------------------------------------


def preavisos_actius(preavisos: Iterable[Preavis], now_utc: datetime) -> list[Preavis]:
    """Open pre-warnings whose period has not ended, most severe first.

    Not split into the two horizons on purpose: a pre-warning covers the whole
    of Catalonia with no comarca and no band, and its entire point is the 3-days
    -and-more horizon (docs/01-data-sources.md §1.5), so "already started" is not
    a meaningful distinction for it. A missing `dataFi` keeps the pre-warning:
    dropping it would be the silent data loss the model layer exists to prevent.
    """
    now = _as_utc(now_utc)
    actius = [
        preavis
        for preavis in preavisos
        if preavis.is_open
        and (preavis.data_fi is None or now < _as_utc(preavis.data_fi))
    ]
    return sorted(actius, key=lambda preavis: (-preavis.perill, -preavis.nivell))
