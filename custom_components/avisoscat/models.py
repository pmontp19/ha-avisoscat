"""Data model and tolerant parser for Meteocat SMP severe-weather warnings.

No Home Assistant import lives here on purpose: this module is pure Python and
testable in complete isolation (docs/04-architecture.md §4). It takes already
decoded JSON and returns typed objects. No network, no I/O, no HTML.

The keyless `meteo.cat` inline payload is not an official API, so every field is
read with `.get()` plus a default and every conversion is tolerant. The eleven
tolerance traps of docs/01-data-sources.md §6 are implemented here; each one has
a dedicated test in `tests/test_models.py`.

Two traps are worth repeating because getting them wrong fails silently:

- Status literals are never used as a filter. `estat` was observed live as
  `"Ampliat"` while the official client only ever compares against `"Vigent"`.
  Anything that is not an explicitly known closed state counts as open, and
  validity is decided later from the dates and the 6-hour UTC bands.
- Numbers arrive as floats (`2.0`, not `2`). They are converted tolerantly and
  never used to index a lookup table.

Deviations from the dataclass sketch in docs/04-architecture.md §4, both in the
direction of more tolerance:

- `dia` fields are `date | None`: an unparseable day must not drop the whole
  affectation.
- `tipus` is `TipusAvis | None` with the raw `tipus_nom` kept beside it, exactly
  like `meteor` / `meteor_nom`, because the type literal has historical variants
  (trap #9) and an unrecognised one must not discard the warning.
- The collections are tuples, so the frozen dataclasses compare by value, which
  is what lets the coordinator compare snapshots with `always_update=False`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Meteor(StrEnum):
    """Weather phenomenon a warning is issued for (docs/01-data-sources.md §1.3)."""

    VENT = "vent"
    PLUJA_30MIN = "pluja_30min"
    PLUJA_3H = "pluja_3h"
    PLUJA_ACUMULADA = "pluja_acumulada"
    NEU = "neu"
    MAR = "mar"
    FRED = "fred"
    CALOR = "calor"
    CALOR_NOCTURNA = "calor_nocturna"
    TEMPS_VIOLENT = "temps_violent"


class TipusAvis(StrEnum):
    """Warning type (docs/01-data-sources.md §1.5)."""

    PREAVIS = "preavis"
    AVIS = "avis"
    VIGILANCIA = "vigilancia"
    TEMPS_VIOLENT = "temps_violent"


class NivellPerill(StrEnum):
    """Official traffic-light grouping of the 0-6 danger grade.

    Verified against the official `meteo.cat` JavaScript
    (`_crearAvisosCombinatsLayer`, `switch(perillMax)`), not inferred
    (docs/01-data-sources.md §1.4).
    """

    CAP = "cap"  # 0
    MODERAT = "moderat"  # 1-2
    ALT = "alt"  # 3-4
    MOLT_ALT = "molt_alt"  # 5-6

    @classmethod
    def from_perill(cls, perill: Any) -> NivellPerill:
        """Map a danger grade to its category, tolerantly.

        The grade arrives as a float (`2.0`) so it is converted, never used as a
        lookup key. Anything unreadable degrades to `CAP` rather than raising,
        and grades outside 0-6 clamp to the nearest band instead of falling
        through to an undefined value.
        """
        grade = _as_int(perill, default=0)
        if grade <= 0:
            return cls.CAP
        if grade <= 2:
            return cls.MODERAT
        if grade <= 4:
            return cls.ALT
        return cls.MOLT_ALT


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# Catalan diacritics and the middle dot are folded away before prefix matching,
# so that "Anul·lat", "Anullat" and "anulat" all read the same and so that the
# tables below can be written in plain ASCII.
_FOLD_MAP = str.maketrans(
    {
        "à": "a",
        "á": "a",
        "è": "e",
        "é": "e",
        "í": "i",
        "ï": "i",
        "ò": "o",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ç": "c",
        "·": "",
        # Typographic apostrophe folded to the ASCII one, so that the two ways
        # of writing "Avis d'Observacio" both match. The key is deliberately the
        # ambiguous character: it is data the feed sends, not source text.
        "’": "'",  # noqa: RUF001
    }
)


def _normalize(value: Any) -> str:
    """Casefold, fold diacritics and collapse whitespace, for prefix matching."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().translate(_FOLD_MAP).split())


def _by_longest_prefix[T](
    table: tuple[tuple[str, T], ...],
) -> tuple[tuple[str, T], ...]:
    """Normalise a prefix table and order it longest prefix first.

    Longest first is what makes prefix matching unambiguous: "avís vigilància
    per temps violent" has to be tried before "avís vigilància", which has to be
    tried before "avís".
    """
    normalized = tuple((_normalize(prefix), value) for prefix, value in table)
    return tuple(sorted(normalized, key=lambda item: len(item[0]), reverse=True))


# Meteor names are free Catalan text (trap #5), so they are matched casefolded
# and by prefix: that way the qualifiers the SMC likes to append ("Neu acumulada
# en 24 h") still resolve, and "Intensitat de pluja en 30 minuts" is never
# confused with "Intensitat de pluja en 3 hores".
_METEOR_PREFIXES = _by_longest_prefix(
    (
        ("intensitat de pluja en 30", Meteor.PLUJA_30MIN),
        ("intensitat de pluja en 3 h", Meteor.PLUJA_3H),
        ("acumulacio de pluja", Meteor.PLUJA_ACUMULADA),
        ("pluja acumulada", Meteor.PLUJA_ACUMULADA),
        ("neu", Meteor.NEU),
        ("estat de la mar", Meteor.MAR),
        ("mar", Meteor.MAR),
        ("vent", Meteor.VENT),
        ("fred", Meteor.FRED),
        ("calor nocturna", Meteor.CALOR_NOCTURNA),
        ("calor", Meteor.CALOR),
        ("temps violent", Meteor.TEMPS_VIOLENT),
    )
)

# The type literal has historical variants ("Avís d'Observació", "Avís temps
# violent"), so it is never compared for equality (trap #9).
_TIPUS_PREFIXES = _by_longest_prefix(
    (
        ("avis vigilancia per temps violent", TipusAvis.TEMPS_VIOLENT),
        ("avis vigilancia temps violent", TipusAvis.TEMPS_VIOLENT),
        ("avis temps violent", TipusAvis.TEMPS_VIOLENT),
        ("avis vigilancia", TipusAvis.VIGILANCIA),
        ("avis d'observacio", TipusAvis.VIGILANCIA),
        ("avis observacio", TipusAvis.VIGILANCIA),
        ("preavis", TipusAvis.PREAVIS),
        ("avis", TipusAvis.AVIS),
    )
)

# Closure literals we actually know about. This tuple is deliberately small and
# is the *only* thing that makes a warning closed: trap #1 says never filter on a
# status literal, so an unknown `estat` (`"Ampliat"` was observed live) counts as
# open and validity is left to the dates and the time band.
_CLOSED_ESTAT_PREFIXES: tuple[str, ...] = (
    "tancat",
    "tancada",
    "finalitzat",
    "finalitzada",
    "anullat",
    "anullada",
    "cancellat",
    "cancellada",
    "caducat",
    "caducada",
    "expirat",
    "expirada",
)

_TRUE_LITERALS = frozenset({"true", "t", "1", "yes", "si", "cert"})


# ---------------------------------------------------------------------------
# Tolerant scalar conversion
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    """Convert to `int` through `float`, because `2.0` is what arrives (trap #2).

    `OverflowError` is absorbed too: `json.loads` accepts `Infinity` and `1e999`,
    and `int(float("inf"))` raises rather than returning a number.
    """
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Convert to `bool` accepting the JSON booleans, numbers and text forms."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return _normalize(value) in _TRUE_LITERALS
    return default


def _as_str(value: Any, default: str = "") -> str:
    """Return external text verbatim; anything non-textual becomes the default.

    The text is never escaped or reshaped here: `comentari`, `llindar` and
    `meteor_nom` are untrusted external strings and stay exactly as the feed
    sent them. Rendering them safely is the consumer's job
    (docs/04-architecture.md §11).
    """
    if isinstance(value, str):
        return value
    return default


def _as_optional_str(value: Any) -> str | None:
    """Like `_as_str`, but keeps the difference between absent and empty."""
    return value if isinstance(value, str) else None


def _as_list(value: Any) -> list[Any]:
    """Read a list field, which arrives as `null` instead of `[]` (trap #3).

    That is the whole of trap #3 in one function: the `null` the feed sends for an
    empty collection, and anything else that is not a list, reads as no entries.
    The container that holds the field is not affected by its emptiness.
    """
    return value if isinstance(value, list) else []


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp such as `"2026-08-04T15:30Z"`; `None` if unusable.

    A naive timestamp is assumed to be UTC: the whole SMP model is expressed in
    UTC bands and a naive datetime would blow up later comparisons.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        _LOGGER.warning("Unparseable SMP timestamp %r, ignoring it", value)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_date(value: Any) -> date | None:
    """Parse the `dia` field, which arrives as a midnight timestamp."""
    parsed = _parse_datetime(value)
    return parsed.date() if parsed is not None else None


def _parse_meteor(nom: Any) -> Meteor | None:
    """Resolve a raw Catalan meteor name; `None` plus a warning when unknown.

    Returning `None` instead of raising is trap #5: the raw name is always kept
    in `Episodi.meteor_nom`, so nothing is lost and nothing explodes.
    """
    normalized = _normalize(nom)
    if not normalized:
        return None
    for prefix, meteor in _METEOR_PREFIXES:
        if normalized.startswith(prefix):
            return meteor
    _LOGGER.warning("Unknown SMP meteor name %r, keeping it as raw text", nom)
    return None


def _parse_tipus(nom: Any) -> TipusAvis | None:
    """Resolve a raw warning-type literal by prefix; `None` when unrecognised."""
    normalized = _normalize(nom)
    if not normalized:
        return None
    for prefix, tipus in _TIPUS_PREFIXES:
        if normalized.startswith(prefix):
            return tipus
    _LOGGER.warning("Unknown SMP warning type %r, keeping it as raw text", nom)
    return None


def is_closed(estat: Any) -> bool:
    """Whether a status literal is an explicitly known *closed* state.

    Everything else, including the unknown and the empty, is open. This is the
    whole of trap #1: `"Ampliat"` is not a closure literal and dropping it would
    silently lose real warnings.
    """
    normalized = _normalize(estat)
    return bool(normalized) and normalized.startswith(_CLOSED_ESTAT_PREFIXES)


def _parse_estat(value: Any) -> str:
    """Read a status, which is either a plain string or `{"nom": ..., ...}`."""
    if isinstance(value, dict):
        return _as_str(value.get("nom"))
    return _as_str(value)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Afectacio:
    """One comarca affected within one 6-hour band."""

    id_comarca: int
    perill: int  # 0-6
    nivell: int  # 1 = low threshold, 2 = high threshold
    llindar: str
    auxiliar: bool
    dia: date | None

    @property
    def nivell_perill(self) -> NivellPerill:
        """Traffic-light category of this affectation's grade."""
        return NivellPerill.from_perill(self.perill)


@dataclass(frozen=True, slots=True)
class Evolucio:
    """One forecast day of a warning, split into its time bands."""

    dia: date | None
    comentari: str
    llindar_baix: str | None
    llindar_alt: str | None
    distribucio_geografica: str | None
    representatiu: int | None
    # Keys come from the JSON (`"18-00"`, not `"18-24"`, trap #8) and keep their
    # original order.
    periodes: dict[str, tuple[Afectacio, ...]] = field(default_factory=dict)

    @property
    def afectacions(self) -> tuple[Afectacio, ...]:
        """Every affectation of this day, across all bands."""
        return tuple(af for afs in self.periodes.values() for af in afs)

    @property
    def perill_maxim(self) -> int:
        """Highest grade of this day, 0 when there is no affectation."""
        return max((af.perill for af in self.afectacions), default=0)


@dataclass(frozen=True, slots=True)
class Avis:
    """One emission of a warning: its dates plus its per-day evolution."""

    tipus: TipusAvis | None  # None when the type literal is unrecognised
    tipus_nom: str  # raw Meteocat literal, always preserved
    estat: str
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None
    evolucions: tuple[Evolucio, ...]

    @property
    def is_open(self) -> bool:
        """Whether this emission is not in an explicitly closed state."""
        return not is_closed(self.estat)

    @property
    def perill_maxim(self) -> int:
        """Highest grade across every day and band of this emission."""
        return max((ev.perill_maxim for ev in self.evolucions), default=0)


@dataclass(frozen=True, slots=True)
class Episodi:
    """A meteor under warning, with its successive emissions deduplicated."""

    meteor: Meteor | None  # None when the name is not recognised
    meteor_nom: str  # raw Meteocat name, always preserved
    estat: str
    avisos: tuple[Avis, ...]

    @property
    def is_open(self) -> bool:
        """Whether the episode is not in an explicitly closed state."""
        return not is_closed(self.estat)


@dataclass(frozen=True, slots=True)
class Preavis:
    """A pre-warning: whole-of-Catalonia scope, no comarca and no time bands.

    Its payload shape differs from an ordinary warning
    (docs/01-data-sources.md §6): the grade and the threshold sit on the
    pre-warning itself instead of on a per-band affectation.
    """

    tipus: TipusAvis | None
    tipus_nom: str
    estat: str
    perill: int
    nivell: int
    llindar: str
    comentari: str
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None
    meteor: Meteor | None = None
    meteor_nom: str = ""

    @property
    def is_open(self) -> bool:
        """Whether the pre-warning is not in an explicitly closed state."""
        return not is_closed(self.estat)

    @property
    def nivell_perill(self) -> NivellPerill:
        """Traffic-light category of the pre-warning's grade."""
        return NivellPerill.from_perill(self.perill)


@dataclass(frozen=True, slots=True)
class SmpSnapshot:
    """Everything one fetch of the SMP feed produced."""

    episodis: tuple[Episodi, ...] = ()
    preavisos: tuple[Preavis, ...] = ()
    fetched_at: datetime | None = None
    # Cheap stand-in for the `Last-Modified` the source does not send: lets the
    # coordinator skip reprocessing when nothing changed.
    payload_hash: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether the snapshot carries no warning at all."""
        return not self.episodis and not self.preavisos


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_all[T](
    raw_entries: Iterable[Any], parse: Callable[[Any], T | None]
) -> tuple[T, ...]:
    """Parse every entry of a collection, dropping the ones the parser rejects.

    A parser returns `None` for an entry it cannot use, having logged why, so one
    malformed member never discards its healthy neighbours.
    """
    return tuple(
        parsed for parsed in (parse(raw) for raw in raw_entries) if parsed is not None
    )


def _read_meteor(raw: dict[str, Any]) -> tuple[Meteor | None, str]:
    """Read the `meteor` object as the resolved enum plus the raw Catalan name.

    `idMeteor` is `null` in the public payload (trap #6): only the name is ever
    read, and never as a lookup key. An unrecognised name resolves to `None` while
    the raw text is kept, so nothing is lost (trap #5).
    """
    raw_meteor = raw.get("meteor")
    nom = _as_str(raw_meteor.get("nom")) if isinstance(raw_meteor, dict) else ""
    return _parse_meteor(nom), nom


def _parse_afectacio(raw: Any) -> Afectacio | None:
    """Parse one affectation; `None` when the entry is not even an object."""
    if not isinstance(raw, dict):
        _LOGGER.warning("Skipping non-object SMP affectation %r", raw)
        return None
    return Afectacio(
        id_comarca=_as_int(raw.get("idComarca"), default=0),
        perill=_as_int(raw.get("perill"), default=0),
        nivell=_as_int(raw.get("nivell"), default=1),
        llindar=_as_str(raw.get("llindar")),
        auxiliar=_as_bool(raw.get("auxiliar")),
        dia=_parse_date(raw.get("dia")),
    )


def _parse_periodes(raw_periodes: Any) -> dict[str, tuple[Afectacio, ...]]:
    """Build the band → affectations mapping, keyed by the JSON's own names."""
    periodes: dict[str, tuple[Afectacio, ...]] = {}
    for raw_periode in _as_list(raw_periodes):
        if not isinstance(raw_periode, dict):
            _LOGGER.warning("Skipping non-object SMP time band %r", raw_periode)
            continue
        nom = _as_str(raw_periode.get("nom"))
        if not nom:
            _LOGGER.warning("Skipping unnamed SMP time band %r", raw_periode)
            continue
        # `afectacions` is `null`, not `[]`, on a band with nothing in it
        # (trap #3, absorbed by `_as_list`). The band itself still becomes a key.
        raw_afectacions = _as_list(raw_periode.get("afectacions"))
        parsed = _parse_all(raw_afectacions, _parse_afectacio)
        periodes[nom] = periodes.get(nom, ()) + parsed
    return periodes


def _parse_evolucio(raw: Any) -> Evolucio | None:
    """Parse one forecast day of a warning."""
    if not isinstance(raw, dict):
        _LOGGER.warning("Skipping non-object SMP evolution %r", raw)
        return None
    representatiu = raw.get("representatiu")
    return Evolucio(
        dia=_parse_date(raw.get("dia")),
        comentari=_as_str(raw.get("comentari")),
        llindar_baix=_as_optional_str(raw.get("llindar1")),
        llindar_alt=_as_optional_str(raw.get("llindar2")),
        distribucio_geografica=_as_optional_str(raw.get("distribucioGeografica")),
        representatiu=None if representatiu is None else _as_int(representatiu),
        periodes=_parse_periodes(raw.get("periodes")),
    )


def _parse_avis(raw: Any) -> Avis | None:
    """Parse one emission of a warning."""
    if not isinstance(raw, dict):
        _LOGGER.warning("Skipping non-object SMP warning %r", raw)
        return None
    tipus_nom = _as_str(raw.get("tipus"))
    return Avis(
        tipus=_parse_tipus(tipus_nom),
        tipus_nom=tipus_nom,
        estat=_parse_estat(raw.get("estat")),
        # Note the single "s": the feed's key really is `dataEmisio`.
        data_emissio=_parse_datetime(raw.get("dataEmisio")),
        data_inici=_parse_datetime(raw.get("dataInici")),
        data_fi=_parse_datetime(raw.get("dataFi")),
        evolucions=_parse_all(_as_list(raw.get("evolucions")), _parse_evolucio),
    )


def _dedupe_avisos(avisos: tuple[Avis, ...]) -> tuple[Avis, ...]:
    """Keep one emission per warning type: the newest, then the most severe.

    An episode carries several `avisos` when the SMC re-emits the same warning
    (trap #4). The meteor is fixed within an episode, so grouping by type is
    what "group by (meteor, tipus)" amounts to here. An unrecognised type groups
    by its raw literal so two unknown types are not merged into one.
    """
    winners: dict[str, Avis] = {}
    order: list[str] = []
    for avis in avisos:
        key = avis.tipus.value if avis.tipus is not None else f"?{avis.tipus_nom}"
        current = winners.get(key)
        if current is None:
            winners[key] = avis
            order.append(key)
        elif _dedupe_rank(avis) > _dedupe_rank(current):
            winners[key] = avis
    return tuple(winners[key] for key in order)


def _dedupe_rank(avis: Avis) -> tuple[datetime, int]:
    """Sort key for deduplication: newest emission wins, ties go to the max grade.

    A missing `dataEmisio` ranks lowest instead of blowing up the comparison, so
    a dated emission always beats an undated one.
    """
    emissio = avis.data_emissio or datetime.min.replace(tzinfo=UTC)
    return (emissio, avis.perill_maxim)


def _parse_episodi(raw: Any) -> Episodi | None:
    """Parse one episode, deduplicating its successive emissions."""
    if not isinstance(raw, dict):
        _LOGGER.warning("Skipping non-object SMP episode %r", raw)
        return None
    meteor, meteor_nom = _read_meteor(raw)
    avisos = _parse_all(_as_list(raw.get("avisos")), _parse_avis)
    return Episodi(
        meteor=meteor,
        meteor_nom=meteor_nom,
        estat=_parse_estat(raw.get("estat")),
        avisos=_dedupe_avisos(avisos),
    )


def _parse_preavis(raw: Any) -> Preavis | None:
    """Parse one pre-warning, which has its own flat shape."""
    if not isinstance(raw, dict):
        _LOGGER.warning("Skipping non-object SMP pre-warning %r", raw)
        return None
    tipus_nom = _as_str(raw.get("tipus"))
    # The API-key endpoint may wrap a meteor in; the public flat shape does not.
    meteor, meteor_nom = _read_meteor(raw)
    return Preavis(
        tipus=_parse_tipus(tipus_nom) or TipusAvis.PREAVIS,
        tipus_nom=tipus_nom,
        estat=_parse_estat(raw.get("estat")),
        perill=_as_int(raw.get("perill"), default=0),
        nivell=_as_int(raw.get("nivell"), default=1),
        llindar=_as_str(raw.get("llindar")),
        comentari=_as_str(raw.get("comentari")),
        data_emissio=_parse_datetime(raw.get("dataEmisio")),
        data_inici=_parse_datetime(raw.get("dataInici")),
        data_fi=_parse_datetime(raw.get("dataFi")),
        meteor=meteor,
        meteor_nom=meteor_nom,
    )


def _flatten(raw: Any) -> list[Any]:
    """Normalise the payload into a flat list of objects.

    The captured payload nests the episodes one level deeper
    (`[[{...}]]`), so a list of lists is flattened rather than parsed as a list
    of malformed episodes.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        _LOGGER.warning(
            "Expected a list in the SMP payload, got %s", type(raw).__name__
        )
        return []
    items: list[Any] = []
    for entry in raw:
        if isinstance(entry, list):
            items.extend(entry)
        else:
            items.append(entry)
    return items


def parse_snapshot(
    episodis_raw: Any = None,
    preavisos_raw: Any = None,
    *,
    fetched_at: datetime | None = None,
    payload_hash: str | None = None,
) -> SmpSnapshot:
    """Turn decoded SMP JSON into an `SmpSnapshot`.

    Never raises. Malformed input in, empty snapshot out, with a warning logged:
    a broken payload must degrade the integration, not crash the coordinator
    (docs/04-architecture.md §10). Individual malformed entries are skipped so
    one bad episode does not discard the good ones beside it.
    """
    try:
        episodis = _parse_all(_flatten(episodis_raw), _parse_episodi)
        preavisos = _parse_all(_flatten(preavisos_raw), _parse_preavis)
    # Broad on purpose: a shape we have never seen must degrade to "no data",
    # never propagate out of the model layer.
    except Exception as err:
        _LOGGER.warning("Could not parse the SMP payload, returning nothing: %s", err)
        return SmpSnapshot(fetched_at=fetched_at, payload_hash=payload_hash)
    return SmpSnapshot(
        episodis=episodis,
        preavisos=preavisos,
        fetched_at=fetched_at,
        payload_hash=payload_hash,
    )
