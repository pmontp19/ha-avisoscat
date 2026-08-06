"""Tests for the SMP data model and its tolerant parser.

Structure: the official grade mapping, then **one test per tolerance trap** of
docs/01-data-sources.md §6 (each named `test_trap_<n>_...` so the trap it covers
is obvious), then the pre-warning shape and the real captured payload.

Every trap test asserts the tolerant *behaviour*, not merely that no exception
escaped: dropping data silently is exactly the failure mode these traps exist to
prevent.
"""

import importlib.util
import json
import logging
import sys
from datetime import UTC, date, datetime
from importlib.abc import MetaPathFinder
from pathlib import Path

import pytest
from custom_components.avisoscat import models
from custom_components.avisoscat.models import (
    Afectacio,
    Avis,
    Episodi,
    Evolucio,
    Meteor,
    NivellPerill,
    Preavis,
    SmpSnapshot,
    TipusAvis,
    is_closed,
    parse_snapshot,
)

MODELS_SOURCE = Path(models.__file__)

CAPTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "captures"
    / "smp-episodis-oberts-2026-08-05.json"
)


@pytest.fixture
def capture() -> list:
    """The real payload captured live on 2026-08-05."""
    return json.loads(CAPTURE.read_text(encoding="utf-8"))


def _avis(**overrides) -> dict:
    """A minimal well-formed warning, as the feed shapes it."""
    return {
        "tipus": "Avís",
        "estat": "Vigent",
        "dataEmisio": "2026-08-04T15:30Z",
        "dataInici": "2026-08-04T12:00Z",
        "dataFi": "2026-08-06T17:59Z",
        "evolucions": [],
    } | overrides


def _episodi(**overrides) -> dict:
    """A minimal well-formed episode, as the feed shapes it."""
    return {
        "id": None,
        "estat": {"nom": "Obert", "data": None},
        "meteor": {"idMeteor": None, "nom": "Intensitat de pluja en 30 minuts"},
        "avisos": [_avis()],
    } | overrides


def _afectacio(**overrides) -> dict:
    """A minimal well-formed affectation, floats included, as the feed sends it."""
    return {
        "dia": "2026-08-04T00:00Z",
        "llindar": "Intensitat > 20 mm / 30 minuts",
        "auxiliar": False,
        "perill": 2.0,
        "idComarca": 24.0,
        "nivell": 1.0,
    } | overrides


def _with_periodes(*periodes: object) -> dict:
    """An episode whose single warning has one evolution with these bands."""
    evolucio = {
        "dia": "2026-08-04T00:00Z",
        "comentari": "",
        "representatiu": 1.0,
        "llindar1": "Intensitat > 20 mm / 30 minuts",
        "llindar2": None,
        "distribucioGeografica": "LOCAL",
        "periodes": list(periodes),
    }
    return _episodi(avisos=[_avis(evolucions=[evolucio])])


def _only_evolucio(snapshot: SmpSnapshot) -> Evolucio:
    """The single evolution of a snapshot built by `_with_periodes`."""
    return snapshot.episodis[0].avisos[0].evolucions[0]


# ---------------------------------------------------------------------------
# The module stays pure Python (docs/04-architecture.md §4)
# ---------------------------------------------------------------------------


def _loaded_home_assistant_modules() -> dict[str, object]:
    """Every `homeassistant` module currently in `sys.modules`."""
    return {
        name: module
        for name, module in list(sys.modules.items())
        if name == "homeassistant" or name.startswith("homeassistant.")
    }


class _BlockHomeAssistant(MetaPathFinder):
    """An import hook that makes the `homeassistant` package unreachable."""

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> None:
        """Fail loudly on a `homeassistant` import; `None` defers everything else."""
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            msg = f"models.py must not import {fullname}"
            raise AssertionError(msg)


def test_models_loads_with_home_assistant_unreachable() -> None:
    """`models.py` must import with the whole `homeassistant` package blocked.

    The contract is that the model layer is testable in complete isolation
    (docs/04-architecture.md §4), so it is proven by loading the module while any
    `homeassistant` import raises, not by reading the source text.
    """
    spec = importlib.util.spec_from_file_location(
        "avisoscat_models_isolated", MODELS_SOURCE
    )
    assert spec is not None
    assert spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)

    # Purged first: `find_spec` is only consulted for a module not already loaded.
    unloaded = _loaded_home_assistant_modules()
    for name in unloaded:
        del sys.modules[name]
    blocker = _BlockHomeAssistant()
    sys.meta_path.insert(0, blocker)
    # `dataclasses` resolves the deferred annotations through `sys.modules`, so the
    # module under test has to be registered while it executes.
    sys.modules[spec.name] = isolated
    try:
        spec.loader.exec_module(isolated)
        assert _loaded_home_assistant_modules() == {}
    finally:
        del sys.modules[spec.name]
        sys.meta_path.remove(blocker)
        sys.modules.update(unloaded)

    # What loaded in isolation is the real module, not an empty shell.
    assert isolated.NivellPerill.from_perill(5.0) is isolated.NivellPerill.MOLT_ALT
    assert isolated.is_closed("Ampliat") is False


# ---------------------------------------------------------------------------
# Official grade mapping (docs/01-data-sources.md §1.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("perill", "expected"),
    [
        (0, NivellPerill.CAP),
        (1, NivellPerill.MODERAT),
        (2, NivellPerill.MODERAT),
        (3, NivellPerill.ALT),
        (4, NivellPerill.ALT),
        (5, NivellPerill.MOLT_ALT),
        (6, NivellPerill.MOLT_ALT),
    ],
)
def test_from_perill_official_mapping(perill: int, expected: NivellPerill) -> None:
    """0 → cap, 1-2 → moderat, 3-4 → alt, 5-6 → molt_alt."""
    assert NivellPerill.from_perill(perill) is expected


@pytest.mark.parametrize(
    ("perill", "expected"),
    [
        (2.0, NivellPerill.MODERAT),  # the float the feed really sends
        ("3", NivellPerill.ALT),
        (-1, NivellPerill.CAP),  # out of range clamps, never falls through
        (9, NivellPerill.MOLT_ALT),
        (None, NivellPerill.CAP),
        ("tempesta", NivellPerill.CAP),
        # `json.loads` accepts these, and `int(float(...))` overflows on them.
        (float("inf"), NivellPerill.CAP),
        (float("-inf"), NivellPerill.CAP),
        ("Infinity", NivellPerill.CAP),
        ("1e999", NivellPerill.CAP),
    ],
)
def test_from_perill_is_tolerant(perill: object, expected: NivellPerill) -> None:
    """A grade that is not a clean 0-6 int degrades instead of raising."""
    assert NivellPerill.from_perill(perill) is expected


# ---------------------------------------------------------------------------
# Trap #1 — `estat` is never filtered on a literal
# ---------------------------------------------------------------------------


def test_trap_1_ampliat_status_is_kept_as_open() -> None:
    """`"Ampliat"` was observed live; only "Vigent" would have kept it."""
    snapshot = parse_snapshot([_episodi(avisos=[_avis(estat="Ampliat")])])

    avis = snapshot.episodis[0].avisos[0]
    assert avis.estat == "Ampliat"
    assert avis.is_open is True
    assert is_closed("Ampliat") is False


@pytest.mark.parametrize(
    "estat", ["Vigent", "Ampliat", "Prorrogat", "", "Un estat que no existeix"]
)
def test_trap_1_unknown_status_counts_as_open(estat: str) -> None:
    """Anything that is not a known closure literal is open."""
    assert is_closed(estat) is False


@pytest.mark.parametrize("estat", ["Tancat", "tancada", "Anul·lat", "Cancel·lada"])
def test_trap_1_only_known_closure_literals_are_closed(estat: str) -> None:
    """Closure is an explicit, small allowlist, diacritics folded."""
    assert is_closed(estat) is True


@pytest.mark.parametrize("estat", [None, 42, {"nom": "Tancat"}, []])
def test_trap_1_non_textual_status_counts_as_open(estat: object) -> None:
    """A status that is not even a string still must not close a warning."""
    assert is_closed(estat) is False


def test_trap_1_episode_status_reads_the_nested_object_or_a_plain_string() -> None:
    """`estat` is `{"nom": ...}` on an episode but a plain string on a warning."""
    nested = parse_snapshot([_episodi(estat={"nom": "Tancat", "data": None})])
    plain = parse_snapshot([_episodi(estat="Obert")])
    missing = parse_snapshot([_episodi(estat=None)])

    assert nested.episodis[0].estat == "Tancat"
    assert nested.episodis[0].is_open is False  # closed, yet still parsed and kept
    assert plain.episodis[0].is_open is True
    assert missing.episodis[0].estat == ""
    assert missing.episodis[0].is_open is True


# ---------------------------------------------------------------------------
# Trap #2 — numbers arrive as floats
# ---------------------------------------------------------------------------


def test_trap_2_floats_become_ints() -> None:
    """`perill`, `idComarca`, `nivell` and `representatiu` arrive as `2.0`."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {
                    "nom": "12-18",
                    "afectacions": [_afectacio(perill=2.0, idComarca=24.0, nivell=1.0)],
                }
            )
        ]
    )

    evolucio = _only_evolucio(snapshot)
    afectacio = evolucio.periodes["12-18"][0]
    assert afectacio.perill == 2
    assert afectacio.id_comarca == 24
    assert afectacio.nivell == 1
    assert evolucio.representatiu == 1
    for value in (afectacio.perill, afectacio.id_comarca, afectacio.nivell):
        assert isinstance(value, int)
        assert not isinstance(value, float)


def test_trap_2_unreadable_numbers_fall_back_without_raising() -> None:
    """A number that is not a number degrades to the documented default."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {
                    "nom": "12-18",
                    "afectacions": [
                        _afectacio(perill=None, idComarca="molt", nivell=None)
                    ],
                }
            )
        ]
    )

    afectacio = _only_evolucio(snapshot).periodes["12-18"][0]
    assert afectacio.perill == 0
    assert afectacio.id_comarca == 0
    assert afectacio.nivell == 1


def test_trap_2_infinite_numbers_do_not_discard_a_healthy_episode() -> None:
    """`Infinity` is valid JSON, and `int(float("inf"))` raises `OverflowError`.

    An overflow escaping the per-entry guards would land in the snapshot-wide net
    and take every healthy episode of the payload down with it.
    """
    infinite = _with_periodes(
        {
            "nom": "12-18",
            "afectacions": [
                _afectacio(perill=float("inf"), idComarca="Infinity", nivell="1e999")
            ],
        }
    )
    healthy = _episodi(meteor={"idMeteor": None, "nom": "Vent"})
    snapshot = parse_snapshot([infinite, healthy])

    afectacio = _only_evolucio(snapshot).periodes["12-18"][0]
    assert (afectacio.perill, afectacio.id_comarca, afectacio.nivell) == (0, 0, 1)
    assert afectacio.nivell_perill is NivellPerill.CAP
    assert len(snapshot.episodis) == 2
    assert snapshot.episodis[1].meteor is Meteor.VENT


@pytest.mark.parametrize(
    ("auxiliar", "expected"),
    [
        (True, True),
        (False, False),
        (1.0, True),  # the same float treatment as the grades
        (0, False),
        ("true", True),
        ("sí", True),
        ("no", False),
        (None, False),
        ([], False),  # not even a scalar
    ],
)
def test_trap_2_auxiliar_flag_is_converted_tolerantly(
    auxiliar: object, expected: bool
) -> None:
    """`auxiliar` is a boolean in the samples, but the feed is not typed."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {"nom": "12-18", "afectacions": [_afectacio(auxiliar=auxiliar)]}
            )
        ]
    )

    assert _only_evolucio(snapshot).periodes["12-18"][0].auxiliar is expected


# ---------------------------------------------------------------------------
# Trap #3 — `afectacions` is `null`, not `[]`
# ---------------------------------------------------------------------------


def test_trap_3_null_afectacions_becomes_an_empty_band() -> None:
    """A band with nothing in it still exists, with no affectation inside."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {"nom": "00-06", "afectacions": None},
                {"nom": "06-12", "afectacions": []},
                {"nom": "12-18", "afectacions": [_afectacio()]},
            )
        ]
    )

    evolucio = _only_evolucio(snapshot)
    assert evolucio.periodes["00-06"] == ()
    assert evolucio.periodes["06-12"] == ()
    assert len(evolucio.periodes["12-18"]) == 1
    assert evolucio.perill_maxim == 2


# ---------------------------------------------------------------------------
# Trap #4 — several `avisos` per episode
# ---------------------------------------------------------------------------


def test_trap_4_repeated_emissions_keep_the_newest() -> None:
    """Successive emissions of the same warning collapse to the newest one."""
    snapshot = parse_snapshot(
        [
            _episodi(
                avisos=[
                    _avis(dataEmisio="2026-08-04T07:43Z"),
                    _avis(dataEmisio="2026-08-04T15:30Z"),
                ]
            )
        ]
    )

    avisos = snapshot.episodis[0].avisos
    assert len(avisos) == 1
    assert avisos[0].data_emissio == datetime(2026, 8, 4, 15, 30, tzinfo=UTC)


def test_trap_4_emission_tie_keeps_the_highest_grade() -> None:
    """Same `dataEmisio` on both: the more severe emission wins."""
    mild = _avis(
        evolucions=[
            {
                "dia": "2026-08-04T00:00Z",
                "periodes": [{"nom": "12-18", "afectacions": [_afectacio(perill=1.0)]}],
            }
        ]
    )
    severe = _avis(
        evolucions=[
            {
                "dia": "2026-08-04T00:00Z",
                "periodes": [{"nom": "12-18", "afectacions": [_afectacio(perill=5.0)]}],
            }
        ]
    )
    snapshot = parse_snapshot([_episodi(avisos=[mild, severe])])

    avisos = snapshot.episodis[0].avisos
    assert len(avisos) == 1
    assert avisos[0].perill_maxim == 5


def test_trap_4_different_types_are_not_merged() -> None:
    """Deduplication is per warning type, so a vigilance never eats an Avís."""
    snapshot = parse_snapshot(
        [_episodi(avisos=[_avis(tipus="Avís"), _avis(tipus="Avís Vigilància")])]
    )

    assert [avis.tipus for avis in snapshot.episodis[0].avisos] == [
        TipusAvis.AVIS,
        TipusAvis.VIGILANCIA,
    ]


# ---------------------------------------------------------------------------
# Trap #5 — meteor names are free Catalan text
# ---------------------------------------------------------------------------


def test_trap_5_unknown_meteor_is_none_and_keeps_the_raw_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The enum degrades to `None`, the Catalan name survives, a warning is logged."""
    with caplog.at_level(logging.WARNING):
        snapshot = parse_snapshot(
            [_episodi(meteor={"idMeteor": None, "nom": "Pluja de gripaus"})]
        )

    episodi = snapshot.episodis[0]
    assert episodi.meteor is None
    assert episodi.meteor_nom == "Pluja de gripaus"
    assert episodi.avisos  # the warning itself is not discarded
    assert "Pluja de gripaus" in caplog.text


@pytest.mark.parametrize(
    ("nom", "expected"),
    [
        ("Intensitat de pluja en 30 minuts", Meteor.PLUJA_30MIN),
        ("Intensitat de pluja en 3 hores", Meteor.PLUJA_3H),
        ("Acumulació de pluja", Meteor.PLUJA_ACUMULADA),
        ("Neu acumulada en 24 h", Meteor.NEU),
        ("Estat de la mar", Meteor.MAR),
        ("VENT", Meteor.VENT),
        ("  Fred  ", Meteor.FRED),
        ("Calor nocturna", Meteor.CALOR_NOCTURNA),
        ("Calor", Meteor.CALOR),
        ("Temps violent", Meteor.TEMPS_VIOLENT),
    ],
)
def test_trap_5_meteor_names_match_case_insensitively_by_prefix(
    nom: str, expected: Meteor
) -> None:
    """Matching is casefolded, whitespace-tolerant and by longest prefix."""
    snapshot = parse_snapshot([_episodi(meteor={"idMeteor": None, "nom": nom})])
    assert snapshot.episodis[0].meteor is expected


# ---------------------------------------------------------------------------
# Trap #6 — `idMeteor` is `null`
# ---------------------------------------------------------------------------


def test_trap_6_null_id_meteor_is_never_used_as_a_key() -> None:
    """The public payload always sends `idMeteor: null`; only the name is read."""
    snapshot = parse_snapshot(
        [_episodi(meteor={"idMeteor": None, "nom": "Estat de la mar"})]
    )

    episodi = snapshot.episodis[0]
    assert episodi.meteor is Meteor.MAR
    assert not hasattr(episodi, "id_meteor")


def test_trap_6_missing_meteor_object_does_not_drop_the_episode() -> None:
    """No `meteor` key at all: unknown meteor, warnings still parsed."""
    snapshot = parse_snapshot([_episodi(meteor=None)])

    episodi = snapshot.episodis[0]
    assert episodi.meteor is None
    assert episodi.meteor_nom == ""
    assert len(episodi.avisos) == 1


# ---------------------------------------------------------------------------
# Trap #7 — unknown `idComarca`
# ---------------------------------------------------------------------------


def test_trap_7_unknown_id_comarca_is_kept_verbatim() -> None:
    """A brand-new comarca or a maritime zone (88-99) parses like any other."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {
                    "nom": "12-18",
                    "afectacions": [
                        _afectacio(idComarca=91.0),
                        _afectacio(idComarca=999.0),
                    ],
                }
            )
        ]
    )

    afectacions = _only_evolucio(snapshot).periodes["12-18"]
    assert [af.id_comarca for af in afectacions] == [91, 999]


# ---------------------------------------------------------------------------
# Trap #8 — the last band is called "18-00"
# ---------------------------------------------------------------------------


def test_trap_8_band_keys_come_from_the_json() -> None:
    """`"18-00"`, not `"18-24"`, and an unexpected band name is kept as well."""
    snapshot = parse_snapshot(
        [
            _with_periodes(
                {"nom": "00-06", "afectacions": None},
                {"nom": "06-12", "afectacions": None},
                {"nom": "12-18", "afectacions": None},
                {"nom": "18-00", "afectacions": [_afectacio()]},
                {"nom": "franja-nova", "afectacions": [_afectacio()]},
            )
        ]
    )

    periodes = _only_evolucio(snapshot).periodes
    assert list(periodes) == ["00-06", "06-12", "12-18", "18-00", "franja-nova"]
    assert "18-24" not in periodes


def test_trap_8_unusable_bands_are_skipped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A band that is not an object, or has no name, must not vanish silently.

    Its affectations are dropped with it, so the skip has to be traceable in the
    log like every other skipped entry.
    """
    with caplog.at_level(logging.WARNING):
        snapshot = parse_snapshot(
            [
                _with_periodes(
                    "not an object",
                    {"afectacions": [_afectacio()]},
                    {"nom": "12-18", "afectacions": [_afectacio()]},
                )
            ]
        )

    assert list(_only_evolucio(snapshot).periodes) == ["12-18"]
    assert "non-object SMP time band" in caplog.text
    assert "unnamed SMP time band" in caplog.text


# ---------------------------------------------------------------------------
# Trap #9 — historical variants of the type literal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipus", "expected"),
    [
        ("Avís", TipusAvis.AVIS),
        ("avis", TipusAvis.AVIS),
        ("Preavís", TipusAvis.PREAVIS),
        ("Avís Vigilància", TipusAvis.VIGILANCIA),
        ("Avís d'Observació", TipusAvis.VIGILANCIA),
        ("Avís Vigilància per Temps Violent", TipusAvis.TEMPS_VIOLENT),
        ("Avís temps violent", TipusAvis.TEMPS_VIOLENT),
    ],
)
def test_trap_9_type_variants_normalize_by_prefix(
    tipus: str, expected: TipusAvis
) -> None:
    """Never strict equality: prefix matching over the casefolded literal."""
    snapshot = parse_snapshot([_episodi(avisos=[_avis(tipus=tipus)])])
    assert snapshot.episodis[0].avisos[0].tipus is expected


def test_trap_9_unknown_type_keeps_the_warning_and_the_raw_literal() -> None:
    """An unrecognised type is `None` plus raw text, not a discarded warning."""
    snapshot = parse_snapshot([_episodi(avisos=[_avis(tipus="Comunicat especial")])])

    avis = snapshot.episodis[0].avisos[0]
    assert avis.tipus is None
    assert avis.tipus_nom == "Comunicat especial"
    assert avis.data_inici == datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Trap #10 — untrusted external text
# ---------------------------------------------------------------------------


def test_trap_10_external_text_is_stored_verbatim_never_transformed() -> None:
    """The model neither escapes nor strips markup: it stores the bytes as sent.

    Escaping here would hide from the consumer that the text is untrusted. The
    contract is "verbatim in the model, never `allow_html` at the edge"
    (docs/04-architecture.md §11).
    """
    hostile = "<img src=x onerror=alert(1)> [1] {2} \"cometes\" i 'apòstrofs'"
    snapshot = parse_snapshot(
        [
            _episodi(
                avisos=[
                    _avis(
                        evolucions=[
                            {
                                "dia": "2026-08-04T00:00Z",
                                "comentari": hostile,
                                "llindar1": hostile,
                                "periodes": [
                                    {
                                        "nom": "12-18",
                                        "afectacions": [_afectacio(llindar=hostile)],
                                    }
                                ],
                            }
                        ]
                    )
                ],
                meteor={"idMeteor": None, "nom": hostile},
            )
        ]
    )

    episodi = snapshot.episodis[0]
    evolucio = episodi.avisos[0].evolucions[0]
    assert episodi.meteor_nom == hostile
    assert evolucio.comentari == hostile
    assert evolucio.llindar_baix == hostile
    assert evolucio.periodes["12-18"][0].llindar == hostile


# ---------------------------------------------------------------------------
# Trap #11 — tolerant date parsing, `None` when it fails
# ---------------------------------------------------------------------------


def test_trap_11_unparseable_dates_become_none_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-ISO timestamp (the CECAT feed's `DD/MM/YYYY HH:MM`) yields `None`."""
    with caplog.at_level(logging.WARNING):
        snapshot = parse_snapshot(
            [
                _episodi(
                    avisos=[
                        _avis(
                            dataEmisio="05/08/2026 15:30",
                            dataInici=None,
                            dataFi="",
                            evolucions=[
                                {
                                    "dia": "no és una data",
                                    "periodes": [
                                        {
                                            "nom": "12-18",
                                            "afectacions": [_afectacio(dia=None)],
                                        }
                                    ],
                                }
                            ],
                        )
                    ]
                )
            ]
        )

    avis = snapshot.episodis[0].avisos[0]
    assert avis.data_emissio is None
    assert avis.data_inici is None
    assert avis.data_fi is None
    assert avis.evolucions[0].dia is None
    assert avis.evolucions[0].periodes["12-18"][0].dia is None
    assert "05/08/2026 15:30" in caplog.text


def test_naive_timestamps_are_assumed_utc() -> None:
    """The whole SMP model is UTC; a naive timestamp must not stay naive."""
    snapshot = parse_snapshot(
        [_episodi(avisos=[_avis(dataInici="2026-08-04T12:00:00")])]
    )

    assert snapshot.episodis[0].avisos[0].data_inici == datetime(
        2026, 8, 4, 12, 0, tzinfo=UTC
    )


# ---------------------------------------------------------------------------
# `parse_snapshot()` never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        "",
        "not json at all",
        42,
        [None],
        ["a string where an episode should be"],
        [{"avisos": "not a list"}],
        [{"avisos": [{"evolucions": {"nope": 1}}]}],
        [{"avisos": [{"evolucions": [{"periodes": "not a list"}]}]}],
        [{"avisos": [{"evolucions": [{"periodes": [{"afectacions": 7}]}]}]}],
        [[[{"deeply": "nested"}]]],
    ],
)
def test_parse_snapshot_never_raises_on_malformed_input(payload: object) -> None:
    """Malformed in, snapshot out. Never an exception."""
    snapshot = parse_snapshot(payload, payload)
    assert isinstance(snapshot, SmpSnapshot)


@pytest.mark.parametrize("payload", [None, {}, "not json at all", 42, [None]])
def test_parse_snapshot_returns_an_empty_snapshot_for_malformed_input(
    payload: object,
) -> None:
    """A payload we cannot make sense of yields nothing, not partial garbage."""
    snapshot = parse_snapshot(payload, payload)

    assert snapshot.episodis == ()
    assert snapshot.preavisos == ()
    assert snapshot.is_empty is True


def test_parse_snapshot_keeps_the_good_episodes_beside_a_broken_one() -> None:
    """One malformed entry must not discard its healthy neighbours."""
    snapshot = parse_snapshot(["rubbish", _episodi(), None])

    assert len(snapshot.episodis) == 1
    assert snapshot.episodis[0].meteor is Meteor.PLUJA_30MIN


def test_parse_snapshot_skips_malformed_members_at_every_level() -> None:
    """A broken warning, evolution, band or affectation drops only itself."""
    good_evolucio = {
        "dia": "2026-08-04T00:00Z",
        "periodes": [
            "not an object",
            {"afectacions": [_afectacio()]},  # no band name: unusable, dropped
            {"nom": "12-18", "afectacions": "not a list"},
            {"nom": "18-00", "afectacions": ["rubbish", _afectacio(perill=4.0)]},
        ],
    }
    snapshot = parse_snapshot(
        [
            _episodi(
                avisos=[
                    "not an object",
                    _avis(evolucions=["not an object", good_evolucio]),
                ]
            )
        ]
    )

    avisos = snapshot.episodis[0].avisos
    assert len(avisos) == 1
    evolucions = avisos[0].evolucions
    assert len(evolucions) == 1
    periodes = evolucions[0].periodes
    assert list(periodes) == ["12-18", "18-00"]
    assert periodes["12-18"] == ()  # band kept, unusable affectations dropped
    assert len(periodes["18-00"]) == 1
    assert periodes["18-00"][0].perill == 4


def test_parse_snapshot_survives_a_payload_that_explodes_while_iterated() -> None:
    """The last-resort net: an unforeseen shape degrades to an empty snapshot.

    Every known malformation is handled entry by entry above. This exercises the
    outer guard, which exists so that a shape nobody has seen yet cannot take the
    coordinator down with it.
    """

    class ExplodingList(list):
        """A payload that looks like a list until something iterates it."""

        def __iter__(self):
            raise RuntimeError("the feed changed shape again")

    snapshot = parse_snapshot(ExplodingList([_episodi()]), payload_hash="abc123")

    assert snapshot.is_empty is True
    assert snapshot.payload_hash == "abc123"


def test_parse_snapshot_carries_fetch_metadata() -> None:
    """`fetched_at` and `payload_hash` pass straight through, even when empty."""
    fetched_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    snapshot = parse_snapshot("garbage", fetched_at=fetched_at, payload_hash="abc123")

    assert snapshot.is_empty is True
    assert snapshot.fetched_at == fetched_at
    assert snapshot.payload_hash == "abc123"


def test_snapshot_defaults_are_empty_and_compare_by_value() -> None:
    """The dataclasses are frozen with tuple collections, so equality is by value.

    Equality is all the coordinator needs for `always_update=False`
    (docs/04-architecture.md §7): `Evolucio.periodes` is a `dict`, as the contract
    specifies, so a parsed snapshot is deliberately not hashable.
    """
    assert SmpSnapshot().is_empty is True
    assert SmpSnapshot() == SmpSnapshot()
    assert Episodi(Meteor.VENT, "Vent", "Obert", ()) == Episodi(
        Meteor.VENT, "Vent", "Obert", ()
    )

    band = {"nom": "12-18", "afectacions": [_afectacio()]}
    populated = parse_snapshot([_with_periodes(band)])
    assert populated == parse_snapshot([_with_periodes(band)])
    assert populated != SmpSnapshot()


# ---------------------------------------------------------------------------
# Pre-warnings: their own shape, no comarca and no time bands
# ---------------------------------------------------------------------------


def test_preavis_parses_its_own_flat_shape() -> None:
    """Grade and threshold sit on the pre-warning itself, not on a band."""
    snapshot = parse_snapshot(
        None,
        [
            {
                "nivell": 1,
                "tipus": "Preavís",
                "dataInici": "2017-03-06T00:00Z",
                "dataFi": "2017-03-08T23:59Z",
                "dataEmisio": "2017-03-06T12:07Z",
                "estat": "Vigent",
                "llindar": "Calor intensa",
                "perill": 2,
                "comentari": "",
            }
        ],
    )

    assert snapshot.episodis == ()
    preavis = snapshot.preavisos[0]
    assert preavis == Preavis(
        tipus=TipusAvis.PREAVIS,
        tipus_nom="Preavís",
        estat="Vigent",
        perill=2,
        nivell=1,
        llindar="Calor intensa",
        comentari="",
        data_emissio=datetime(2017, 3, 6, 12, 7, tzinfo=UTC),
        data_inici=datetime(2017, 3, 6, 0, 0, tzinfo=UTC),
        data_fi=datetime(2017, 3, 8, 23, 59, tzinfo=UTC),
    )
    assert preavis.nivell_perill is NivellPerill.MODERAT
    assert preavis.is_open is True
    assert preavis.meteor is None  # no meteor in the pre-warning shape


def test_preavis_tolerates_floats_and_a_missing_type() -> None:
    """Same float and status tolerance as an ordinary warning."""
    snapshot = parse_snapshot(
        None, [{"perill": 5.0, "nivell": 2.0, "estat": "Ampliat"}]
    )

    preavis = snapshot.preavisos[0]
    assert (preavis.perill, preavis.nivell) == (5, 2)
    assert preavis.nivell_perill is NivellPerill.MOLT_ALT
    # No usable `tipus` literal: it is a pre-warning by virtue of where it came
    # from, so the type defaults instead of becoming `None`.
    assert preavis.tipus is TipusAvis.PREAVIS
    assert preavis.tipus_nom == ""
    assert preavis.is_open is True


def test_preavis_carries_a_meteor_when_the_feed_sends_one() -> None:
    """The API-key endpoint may wrap the meteor in; it is read when present."""
    snapshot = parse_snapshot(
        None,
        [{"tipus": "Preavís", "meteor": {"idMeteor": None, "nom": "Calor"}}],
    )

    preavis = snapshot.preavisos[0]
    assert preavis.meteor is Meteor.CALOR
    assert preavis.meteor_nom == "Calor"


# ---------------------------------------------------------------------------
# The real captured payload
# ---------------------------------------------------------------------------


def test_real_capture_parses_into_the_expected_objects(capture: list) -> None:
    """Asserted against the actual content of the 2026-08-05 capture.

    One episode of 30-minute rainfall intensity, two successive emissions of the
    same `Avís` in state `"Ampliat"`, three forecast days, floats throughout and
    `afectacions: null` on most bands. Five of the eleven traps at once.
    """
    snapshot = parse_snapshot(capture)

    assert len(snapshot.episodis) == 1
    episodi = snapshot.episodis[0]
    assert episodi.meteor is Meteor.PLUJA_30MIN
    assert episodi.meteor_nom == "Intensitat de pluja en 30 minuts"
    assert episodi.estat == "Obert"
    assert episodi.is_open is True

    # Two emissions in, one out: 15:30 beats 07:43 (trap #4).
    assert len(episodi.avisos) == 1
    avis = episodi.avisos[0]
    assert avis.tipus is TipusAvis.AVIS
    assert avis.estat == "Ampliat"  # trap #1: not dropped
    assert avis.is_open is True
    assert avis.data_emissio == datetime(2026, 8, 4, 15, 30, tzinfo=UTC)
    assert avis.data_inici == datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    assert avis.data_fi == datetime(2026, 8, 6, 17, 59, tzinfo=UTC)

    assert [ev.dia for ev in avis.evolucions] == [
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
    ]
    for evolucio in avis.evolucions:
        assert list(evolucio.periodes) == ["00-06", "06-12", "12-18", "18-00"]
        assert evolucio.llindar_baix == "Intensitat > 20 mm / 30 minuts"
        assert evolucio.llindar_alt is None
        assert evolucio.representatiu == 1

    first, second, third = avis.evolucions
    assert first.distribucio_geografica == "LOCAL"
    assert third.distribucio_geografica == "EXTENSA"
    assert first.comentari.startswith("Els xàfecs aniran acompanyats de tempesta")

    # First day: 23 comarques in "12-18", 6 in "18-00", the other bands null.
    assert len(first.periodes["12-18"]) == 23
    assert len(first.periodes["18-00"]) == 6
    assert first.periodes["00-06"] == ()
    assert first.periodes["06-12"] == ()
    assert first.perill_maxim == 3

    # Middle day: emitted, but with no affectation at all.
    assert second.afectacions == ()
    assert second.perill_maxim == 0

    assert first.periodes["12-18"][0] == Afectacio(
        id_comarca=1,
        perill=2,
        nivell=1,
        llindar="Intensitat > 20 mm / 30 minuts",
        auxiliar=False,
        dia=date(2026, 8, 4),
    )
    assert first.periodes["12-18"][0].nivell_perill is NivellPerill.MODERAT
    assert avis.perill_maxim == 3


def test_real_capture_grades_are_ints_not_floats(capture: list) -> None:
    """Every number in the real payload comes out as an `int` (trap #2)."""
    snapshot = parse_snapshot(capture)

    afectacions = [
        af
        for episodi in snapshot.episodis
        for avis in episodi.avisos
        for evolucio in avis.evolucions
        for af in evolucio.afectacions
    ]
    assert afectacions
    for afectacio in afectacions:
        assert type(afectacio.perill) is int
        assert type(afectacio.id_comarca) is int
        assert type(afectacio.nivell) is int


def test_real_capture_has_no_preavisos(capture: list) -> None:
    """The capture is the open-episodes payload; pre-warnings come separately."""
    assert parse_snapshot(capture).preavisos == ()


def test_avis_defaults_report_no_danger() -> None:
    """An emission with no evolution at all has grade 0, not an error."""
    avis = Avis(
        tipus=TipusAvis.AVIS,
        tipus_nom="Avís",
        estat="Vigent",
        data_emissio=None,
        data_inici=None,
        data_fi=None,
        evolucions=(),
    )
    assert avis.perill_maxim == 0
