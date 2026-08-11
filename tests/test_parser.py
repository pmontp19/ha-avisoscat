"""Tests for the inline SMP payload extraction.

Structure: the real captured page first, then **one test per documented trap**
(brackets inside prose, the `avisos` decoys, a quiet page, missing markup), then
the handoff to `models.parse_snapshot()`.

The page belongs to a third party and can change without notice, so these tests
assert extraction *behaviour* on markup shapes actually observed live, not merely
that no exception escaped. The three shapes that matter are recorded in
docs/01-data-sources.md §3.2 and docs/captures/smp-page-choice-2026-08-06.md.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from custom_components.avisoscat import parser
from custom_components.avisoscat.models import Meteor, TipusAvis, parse_snapshot
from custom_components.avisoscat.parser import SmpParseError, extract_smp_payload

PARSER_SOURCE = Path(parser.__file__)

# Real page captured on 2026-08-06 with an episode open, trimmed but with the
# payload byte-for-byte intact.
FIXTURE = Path(__file__).parent / "fixtures" / "smp_page_sample.html"


@pytest.fixture
def real_page() -> str:
    """The trimmed real `meteo.cat` page."""
    return FIXTURE.read_text(encoding="utf-8")


def _page(*, call_body: str, marker: str = "Meteocat.avisosSMP(") -> str:
    """Wrap a call argument list in the surrounding markup the real page has.

    The wrapper is not decoration: it puts the call inside a `<script>` in a
    document that also contains prose and other JavaScript, which is where a
    sloppier anchor would go wrong.
    """
    return (
        "<!DOCTYPE html>\n<html lang='ca'>\n<body>\n"
        "<p>Avisos de temps sever [SMP] a Catalunya</p>\n"
        "<div id='mapaWidget'></div>\n"
        "<script type='text/javascript'>\n"
        "    var opcionsMapa = { maxBounds: [[38.86, -1.44], [44.58, 6.16]] };\n"
        "    var llistaAvisos = false;\n"
        f"    {marker}{{\n{call_body}\n    }});\n"
        "</script>\n</body>\n</html>\n"
    )


def _episodi(*, meteor: str = "Vent", comentari: str = "", perill: float = 3.0) -> str:
    """One episode, encoded the way the server encodes it: compact JSON."""
    return json.dumps(
        [
            {
                "id": None,
                "estat": {"nom": "Obert", "data": None},
                "meteor": {"idMeteor": None, "nom": meteor},
                "avisos": [
                    {
                        "tipus": "Avís",
                        "estat": "Vigent",
                        "dataEmisio": "2026-08-06T07:42Z",
                        "dataInici": "2026-08-06T06:00Z",
                        "dataFi": "2026-08-06T23:59Z",
                        "evolucions": [
                            {
                                "dia": "2026-08-06T00:00Z",
                                "comentari": comentari,
                                "representatiu": 1.0,
                                "llindar1": "Ratxes > 72 km/h",
                                "llindar2": None,
                                "distribucioGeografica": "LOCAL",
                                "periodes": [
                                    {"nom": "00-06", "afectacions": None},
                                    {"nom": "06-12", "afectacions": None},
                                    {
                                        "nom": "12-18",
                                        "afectacions": [
                                            {
                                                "dia": "2026-08-06T00:00Z",
                                                "llindar": "Ratxes > 72 km/h",
                                                "auxiliar": False,
                                                "perill": perill,
                                                "idComarca": 24.0,
                                                "nivell": 1.0,
                                            }
                                        ],
                                    },
                                    {"nom": "18-00", "afectacions": None},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        ensure_ascii=False,
    )


def _warnings(avisos: list) -> list[dict]:
    """Flatten the day-nested episode payload into its warning objects."""
    episodes = []
    for entry in avisos:
        episodes.extend(entry if isinstance(entry, list) else [entry])
    return [
        {"meteor": (ep.get("meteor") or {}).get("nom"), **avis}
        for ep in episodes
        for avis in ep.get("avisos") or []
    ]


# ---------------------------------------------------------------------------
# The real captured page
# ---------------------------------------------------------------------------


def test_extracts_the_payload_from_the_real_page(real_page: str) -> None:
    """The real 2026-08-06 page yields its three forecast days of episodes."""
    avisos, preavisos = extract_smp_payload(real_page)

    # One sub-array per forecast day, which is the shape the widget renders.
    assert len(avisos) == 3
    assert all(isinstance(day, list) for day in avisos)
    assert preavisos == []

    warnings = _warnings(avisos)
    assert len(warnings) == 14
    # The violent-weather nowcast that was in force at capture time, with its
    # affectations hanging off the warning instead of off an evolution.
    violent = [w for w in warnings if w["meteor"] == "Temps violent"]
    assert len(violent) == 1
    assert violent[0]["tipus"] == "Avís Vigilància per Temps Violent"
    assert len(violent[0]["afectacions"]) == 5


def test_the_real_page_keeps_the_payload_verbatim(real_page: str) -> None:
    """Nothing is reshaped on the way out: text, floats and band names survive.

    The whole point of this module is that it does not touch the data, so the
    assertions are on the raw JSON values: Catalan text with its accents, the
    float-shaped numbers of trap #2, `afectacions: null` of trap #3 and the
    `"18-00"` band name of trap #8.
    """
    avisos, _ = extract_smp_payload(real_page)
    evolucio = next(
        ev
        for w in _warnings(avisos)
        for ev in w.get("evolucions") or []
        if ev.get("periodes")
    )

    assert [p["nom"] for p in evolucio["periodes"]] == [
        "00-06",
        "06-12",
        "12-18",
        "18-00",
    ]
    assert any(p["afectacions"] is None for p in evolucio["periodes"])
    afectacio = next(
        af for p in evolucio["periodes"] if p["afectacions"] for af in p["afectacions"]
    )
    assert isinstance(afectacio["perill"], float)
    assert isinstance(afectacio["idComarca"], float)
    # `>` in the source decodes to a real `>`, and the accents are intact.
    assert ">" in afectacio["llindar"]
    assert "à" in evolucio["comentari"] or "è" in evolucio["comentari"]


def test_the_real_page_feeds_parse_snapshot_unchanged(real_page: str) -> None:
    """The extracted JSON goes straight into `models.parse_snapshot()`.

    This is the seam between the two tasks, so it is asserted end to end rather
    than assumed: no reshaping, no rewrapping, no second parser.
    """
    avisos, preavisos = extract_smp_payload(real_page)
    snapshot = parse_snapshot(avisos, preavisos)

    assert not snapshot.is_empty
    meteors = {ep.meteor for ep in snapshot.episodis}
    assert Meteor.TEMPS_VIOLENT in meteors
    assert Meteor.PLUJA_30MIN in meteors
    assert Meteor.PLUJA_3H in meteors
    # Every meteor name in the live payload resolved, so nothing fell through to
    # the raw-text fallback.
    assert None not in meteors

    # Osona (24) was under a rain warning at capture time, at a real grade.
    # Enumerated through `Avis.totes_afectacions`, not `avis.evolucions`: the
    # violent-weather nowcast in this same capture hangs its five affectations
    # directly off the avis, so walking `evolucions` alone drops them.
    osona = [
        af
        for ep in snapshot.episodis
        for avis in ep.avisos
        for af in avis.totes_afectacions
        if af.id_comarca == 24
    ]
    assert osona
    assert max(af.perill for af in osona) >= 1

    violent = next(ep for ep in snapshot.episodis if ep.meteor is Meteor.TEMPS_VIOLENT)
    violent_avis = violent.avisos[0]
    assert violent_avis.tipus is TipusAvis.TEMPS_VIOLENT
    # The five affectations the raw payload carries on the avis itself survive
    # parsing, and its real grade is not read as "no danger".
    assert violent_avis.evolucions == ()
    assert len(violent_avis.afectacions_directes) == 5
    assert violent_avis.totes_afectacions == violent_avis.afectacions_directes
    assert violent_avis.perill_maxim >= 1


# ---------------------------------------------------------------------------
# Trap: brackets inside string values (docs/01-data-sources.md §3.2)
# ---------------------------------------------------------------------------


def test_brackets_inside_a_comentari_do_not_end_the_array() -> None:
    """`[`, `]` and `{` inside `comentari` must not terminate the extraction.

    This is the trap that rules out a greedy regular expression: a comment
    mentioning "[SMP]" would truncate the array at the first `]`, silently losing
    every episode after it. The comment here also carries an unbalanced `{`, a
    `]}` pair, an escaped quote and a literal `avisos: [` that would fool a
    textual key search.
    """
    comentari = (
        "Xàfecs [localment forts] amb ratxes > 72 km/h {vegeu el mapa} "
        'i pedra ]} de 2 cm; l\'"episodi" segueix obert. avisos: [ '
        "Ratxes de vent \\ pedra"
    )
    html = _page(
        call_body=(
            "        dom: 'mapaWidget',\n"
            "        episodisPreavisos: [],\n"
            f"        avisos: [{_episodi(comentari=comentari)}],\n"
            "        data: '2026-08-06Z'\n"
        )
    )

    avisos, preavisos = extract_smp_payload(html)

    warnings = _warnings(avisos)
    assert len(warnings) == 1
    assert warnings[0]["evolucions"][0]["comentari"] == comentari
    assert preavisos == []
    # And the payload still survives the model layer untouched.
    snapshot = parse_snapshot(avisos, preavisos)
    assert snapshot.episodis[0].avisos[0].evolucions[0].comentari == comentari


def test_single_quoted_javascript_strings_with_brackets_do_not_confuse_the_scan() -> (
    None
):
    """A `]` inside a single-quoted JS option must not close the argument list."""
    html = _page(
        call_body=(
            "        dom: 'mapa]Widget',\n"
            '        domLlistat: "llistat[Avisos",\n'
            "        episodisPreavisos: [],\n"
            f"        avisos: [{_episodi()}]\n"
        )
    )

    avisos, _ = extract_smp_payload(html)

    assert len(_warnings(avisos)) == 1


# ---------------------------------------------------------------------------
# Trap: the decoy `avisos` keys (docs/01-data-sources.md §3.2)
# ---------------------------------------------------------------------------


def test_the_empty_avisos_key_inside_opcions_is_not_the_real_one() -> None:
    """The `opcions` object also has an `avisos` key, and it is empty.

    It is rendered *before* the real one, so anchoring on the first match returns
    nothing at all. Keys are read only at the top level of the call, which makes
    the decoy structurally invisible rather than merely outranked.
    """
    html = _page(
        call_body=(
            "        dom: 'mapaWidget',\n"
            "        opcions: { ambAvisos: true, avisos: [], dies: 3 },\n"
            "        episodisPreavisos: [],\n"
            f"        avisos: [{_episodi(meteor='Vent')}]\n"
        )
    )

    avisos, _ = extract_smp_payload(html)

    warnings = _warnings(avisos)
    assert len(warnings) == 1
    assert warnings[0]["meteor"] == "Vent"


def test_a_decoy_after_the_real_key_does_not_win_either() -> None:
    """Order must not matter: a nested empty `avisos` never replaces the payload."""
    html = _page(
        call_body=(
            f"        avisos: [{_episodi(meteor='Neu')}],\n"
            "        episodisPreavisos: [],\n"
            "        opcions: { avisos: [], separarFranjes: false }\n"
        )
    )

    avisos, _ = extract_smp_payload(html)

    assert [w["meteor"] for w in _warnings(avisos)] == ["Neu"]


def test_the_avisos_key_of_each_episode_is_not_mistaken_for_the_payload(
    real_page: str,
) -> None:
    """Every episode in the payload carries its own `avisos` key, nested deeper.

    On the real page that inner key holds the emissions of one episode. Picking it
    up would return warnings where episodes are expected, so the result is checked
    for the outer shape: a list of days, each holding episodes with a `meteor`.
    """
    avisos, _ = extract_smp_payload(real_page)

    assert all(isinstance(day, list) for day in avisos)
    assert all("meteor" in episode for day in avisos for episode in day)


def test_the_richest_call_wins_when_a_page_renders_the_call_twice() -> None:
    """The homepage renders a 1-day visor and a 3-day widget.

    The first call holds only today's episodes and is a strict subset of the
    second (docs/captures/smp-page-choice-2026-08-06.md), so anchoring on the
    first non-empty array would silently drop tomorrow's warnings.
    """
    today = _episodi(meteor="Vent")
    tomorrow = _episodi(meteor="Neu")
    html = _page(
        call_body=f"        episodisPreavisos: [],\n        avisos: [{today}]\n"
    ) + _page(
        call_body=(
            f"        episodisPreavisos: [],\n        avisos: [{today}, {tomorrow}]\n"
        )
    )

    avisos, _ = extract_smp_payload(html)

    assert [w["meteor"] for w in _warnings(avisos)] == ["Vent", "Neu"]


# ---------------------------------------------------------------------------
# Trap: the marker also appears outside a call
# ---------------------------------------------------------------------------


def _page_naming_the_call_in_prose(prose: str) -> str:
    """A readable one-episode page whose prose also names the call, before it."""
    html = _page(
        call_body=(
            "        episodisPreavisos: [],\n"
            f"        avisos: [{_episodi(meteor='Neu')}]\n"
        )
    )
    return html.replace(
        "<div id='mapaWidget'></div>",
        f"<p>{prose}</p>\n<div id='mapaWidget'></div>",
        1,
    )


def test_an_unbalanced_marker_in_prose_before_the_call_does_not_hide_it() -> None:
    """Page prose naming the call must never become the anchor for the real one.

    A `Meteocat.avisosSMP(` occurrence outside a call need not balance (the real
    captured fixture has one, in its provenance comment). Taking such an occurrence
    as a call makes its `(` the depth origin for everything after it, so every
    top-level key of the real call sits one level too deep and a perfectly readable
    page fails as unreadable. Three of those and the coordinator declares the
    service degraded while the source is fine.
    """
    html = _page_naming_the_call_in_prose("La crida Meteocat.avisosSMP( del giny")

    avisos, preavisos = extract_smp_payload(html)

    assert [w["meteor"] for w in _warnings(avisos)] == ["Neu"]
    assert preavisos == []


def test_a_prose_marker_closed_after_the_real_call_does_not_swallow_it() -> None:
    """The same trap when the stray occurrence *does* balance, just too late.

    Here the stray `(` closes on a `)` sitting after the real call, so its bracket
    group spans the real call instead of stopping short of it. Resuming the search
    past the span rather than past the marker would skip the real call entirely.
    """
    html = _page_naming_the_call_in_prose(
        "Consulteu Meteocat.avisosSMP( al giny"
    ).replace("</body>", "<p>fi de la nota)</p>\n</body>", 1)

    avisos, _ = extract_smp_payload(html)

    assert [w["meteor"] for w in _warnings(avisos)] == ["Neu"]


# ---------------------------------------------------------------------------
# Trap: a quiet page is not an error (docs/01-data-sources.md §3.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "avisos_value",
    [
        pytest.param("[]", id="empty"),
        pytest.param("[[]]", id="empty-day-array"),
        pytest.param("[[], [], []]", id="three-empty-days"),
    ],
)
def test_a_page_with_no_open_episode_returns_two_empty_lists(
    avisos_value: str,
) -> None:
    """No warning open is the normal state of a quiet day, not a failure.

    All three encodings collapse to the same empty answer, so the caller gets one
    falsy result to test instead of three shapes. Falsy means quiet, not broken:
    what sends `smp.py` to the fallback page is a fetch or parse failure
    (docs/04-architecture.md §3).
    """
    html = _page(
        call_body=(
            "        dom: 'mapaWidget',\n"
            "        episodisPreavisos: [],\n"
            f"        avisos: {avisos_value}\n"
        )
    )

    assert extract_smp_payload(html) == ([], [])


def test_a_quiet_page_still_reports_no_pre_warnings() -> None:
    """The quiet case returns an empty list for pre-warnings too, not `None`."""
    html = _page(call_body="        episodisPreavisos: [],\n        avisos: [[]]\n")

    avisos, preavisos = extract_smp_payload(html)

    assert _warnings(avisos) == []
    assert preavisos == []


# ---------------------------------------------------------------------------
# Trap: unreadable markup is the only parse failure
# ---------------------------------------------------------------------------


def test_a_page_without_the_call_raises() -> None:
    """No `Meteocat.avisosSMP(` at all means the markup changed."""
    html = "<html><body><p>Servei temporalment no disponible</p></body></html>"

    with pytest.raises(SmpParseError, match=r"No Meteocat\.avisosSMP"):
        extract_smp_payload(html)


def test_a_page_that_is_not_a_string_raises() -> None:
    """A non-textual body fails as a parse error, not as an `AttributeError`."""
    with pytest.raises(SmpParseError):
        extract_smp_payload(None)  # type: ignore[arg-type]


def test_a_call_without_a_top_level_avisos_key_raises() -> None:
    """The call is there but its episode key is gone: the payload shape changed."""
    html = _page(
        call_body=(
            "        dom: 'mapaWidget',\n"
            "        episodisPreavisos: [],\n"
            "        opcions: { avisos: [] }\n"
        )
    )

    with pytest.raises(SmpParseError, match="no top-level `avisos` key"):
        extract_smp_payload(html)


def test_an_undecodable_avisos_value_raises() -> None:
    """A key we can find but not decode is a parse failure, not an empty result.

    Returning `([], [])` here would be the dangerous answer: it reads as "no
    warnings" when the truth is "we cannot read the warnings".
    """
    html = _page(
        call_body=(
            "        episodisPreavisos: [],\n"
            "        avisos: [{meteor: undefined, avisos: []}]\n"
        )
    )

    with pytest.raises(SmpParseError, match="could not be decoded"):
        extract_smp_payload(html)


def test_a_truncated_page_raises_instead_of_inventing_a_boundary() -> None:
    """A response cut mid-array must fail, not guess where the array ended."""
    html = _page(
        call_body=(f"        episodisPreavisos: [],\n        avisos: [{_episodi()}]\n")
    )
    truncated = html[: html.index('"periodes"')]

    with pytest.raises(SmpParseError):
        extract_smp_payload(truncated)


def test_an_avisos_key_that_holds_something_other_than_an_array_raises() -> None:
    """`avisos: null` is not an array, and there is no second candidate."""
    html = _page(
        call_body="        episodisPreavisos: [],\n        avisos: null\n",
    )

    with pytest.raises(SmpParseError, match="could not be decoded"):
        extract_smp_payload(html)


# ---------------------------------------------------------------------------
# Pre-warnings
# ---------------------------------------------------------------------------


def test_pre_warnings_are_extracted_and_feed_the_model() -> None:
    """`episodisPreavisos` has its own flat shape and comes out ready to parse."""
    preavis = json.dumps(
        [
            {
                "nivell": 1,
                "tipus": "Preavís",
                "dataInici": "2026-08-09T00:00Z",
                "dataFi": "2026-08-10T23:59Z",
                "dataEmisio": "2026-08-06T12:07Z",
                "estat": "Vigent",
                "llindar": "Calor intensa",
                "perill": 2,
                "comentari": "Calor intensa [localment molt intensa]",
            }
        ],
        ensure_ascii=False,
    )
    html = _page(
        call_body=(
            f"        episodisPreavisos: {preavis},\n        avisos: [{_episodi()}]\n"
        )
    )

    avisos, preavisos = extract_smp_payload(html)

    assert len(preavisos) == 1
    snapshot = parse_snapshot(avisos, preavisos)
    assert len(snapshot.preavisos) == 1
    assert snapshot.preavisos[0].tipus is TipusAvis.PREAVIS
    assert snapshot.preavisos[0].llindar == "Calor intensa"


def test_missing_pre_warnings_key_degrades_instead_of_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing the pre-warnings must not discard warnings that are in force.

    Pre-warnings are a 3-day-out planning aid; a warning in force is what the user
    needs right now. So a missing `episodisPreavisos` key warns and returns an
    empty list rather than raising.
    """
    html = _page(call_body=f"        avisos: [{_episodi()}]\n")

    with caplog.at_level(logging.WARNING, logger=parser.__name__):
        avisos, preavisos = extract_smp_payload(html)

    assert len(_warnings(avisos)) == 1
    assert preavisos == []
    assert "episodisPreavisos" in caplog.text


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

# Loads parser.py by file spec in a child interpreter, runs it against the real
# page, and reports what it saw.
_ISOLATION_SCRIPT = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("avisoscat_parser", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

with open(sys.argv[2], encoding="utf-8") as page:
    avisos, preavisos = module.extract_smp_payload(page.read())

print(json.dumps({
    "home_assistant": sorted(
        name for name in sys.modules if name.split(".")[0] == "homeassistant"
    ),
    "days": len(avisos),
    "episodes": sum(len(day) for day in avisos),
    "preavisos": len(preavisos),
}))
"""


def test_parser_loads_in_an_interpreter_without_home_assistant() -> None:
    """`parser.py` must extract in an interpreter that never imports HA.

    Same contract as `models.py` (docs/04-architecture.md §3-§4): the extraction
    is pure text handling, so it has to work with nothing but the standard
    library. The child interpreter is asked what it ended up loading, so an
    already imported `homeassistant` in this process cannot mask a stray import.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _ISOLATION_SCRIPT,
                str(PARSER_SOURCE),
                str(FIXTURE),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        pytest.fail(f"Could not run a child interpreter to load parser.py: {err}")

    assert result.returncode == 0, f"parser.py failed to load:\n{result.stderr}"
    report = json.loads(result.stdout)
    assert report["home_assistant"] == []
    # What ran in isolation really did the extraction.
    assert report["days"] == 3
    assert report["episodes"] == 6
    assert report["preavisos"] == 0
