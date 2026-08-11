"""Tests for warning validity and the two time horizons.

Structure: the bands themselves, then the acceptance criteria of the two
horizons one by one, then the outlook grid, then violent weather as its own
case, then official local time, then the tolerance paths.

Every clock-dependent test drives the `clock` fixture (`FakeClock`) rather than
`freezegun` or a real `sleep()`: validity here is a pure function of the wall
clock, so the clock is an input like any other.

The subjects are built as raw feed payloads and put through `parse_snapshot()`,
not as hand-made dataclasses: what this module has to survive is the shape the
source really sends, floats and `null`s included.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from custom_components.avisoscat import vigencia
from custom_components.avisoscat.models import (
    Episodi,
    Meteor,
    TipusAvis,
    parse_snapshot,
)
from custom_components.avisoscat.vigencia import (
    DIES_OUTLOOK,
    FINESTRA_TEMPS_VIOLENT,
    PERIODES,
    AfectacioProjectada,
    Horitzo,
    afectacions_anunciades,
    afectacions_per_horitzo,
    afectacions_vigents,
    etiqueta_dia,
    outlook,
    periode_actual,
    periode_bounds,
    pic,
    preavisos_actius,
    projeccions,
)

from .conftest import ID_COMARCA_OSONA, FakeClock, run_in_isolated_interpreter

# The comarca every test asks about, and a neighbour that must never leak in.
OSONA = ID_COMARCA_OSONA
ALT_EMPORDA = 3
# Maritime zones share the affectation shape and only differ by id
# (docs/03-feature-spec.md §3.7).
MAR_MARESME = 90

# The day the `clock` fixture starts on, at 12:00 UTC.
AVUI = date(2026, 8, 5)
DEMA = date(2026, 8, 6)
DEMA_PASSAT = date(2026, 8, 7)

MADRID = ZoneInfo("Europe/Madrid")


# ---------------------------------------------------------------------------
# Payload builders: the feed's own shape, floats and `null`s included
# ---------------------------------------------------------------------------


def _afectacio(
    *,
    id_comarca: int = OSONA,
    perill: float | str = 3.0,
    nivell: float = 1.0,
    dia: str | None = "2026-08-05T00:00Z",
    llindar: str = "Ratxa màxima > 108 km/h (30 m/s)",
) -> dict:
    """One affectation, exactly as the feed shapes it."""
    return {
        "dia": dia,
        "llindar": llindar,
        "auxiliar": False,
        "perill": perill,
        "idComarca": float(id_comarca),
        "nivell": nivell,
    }


def _evolucio(
    periodes: dict[str, list[dict] | None],
    *,
    dia: str | None = "2026-08-05T00:00Z",
    comentari: str = "Ratxes molt fortes al litoral.",
    llindar1: str | None = "Ratxa màxima > 72 km/h (20 m/s)",
    llindar2: str | None = "Ratxa màxima > 108 km/h (30 m/s)",
) -> dict:
    """One forecast day.

    The four canonical bands are always sent, an unaffected one as `null` rather
    than as `[]`, exactly like the real payload. Any other key given (an alias,
    an unusable name) is appended so the tolerance paths see the real shape too.
    """
    noms = list(PERIODES) + [nom for nom in periodes if nom not in PERIODES]
    return {
        "dia": dia,
        "comentari": comentari,
        "representatiu": 1.0,
        "llindar1": llindar1,
        "llindar2": llindar2,
        "distribucioGeografica": "EXTENSA",
        "periodes": [{"nom": nom, "afectacions": periodes.get(nom)} for nom in noms],
    }


def _episodis(
    evolucions: list[dict],
    *,
    meteor: str = "Vent",
    tipus: str = "Avís",
    estat: str = "Vigent",
    estat_episodi: str = "Obert",
    data_emissio: str | None = "2026-08-04T15:30Z",
    data_inici: str | None = "2026-08-04T12:00Z",
    data_fi: str | None = "2026-08-06T23:59Z",
) -> tuple[Episodi, ...]:
    """Parse a one-episode payload, so the subject is what the parser produces."""
    payload = [
        [
            {
                "id": None,
                "estat": {"nom": estat_episodi, "data": None},
                "meteor": {"idMeteor": None, "nom": meteor},
                "avisos": [
                    {
                        "tipus": tipus,
                        "estat": estat,
                        "dataEmisio": data_emissio,
                        "dataInici": data_inici,
                        "dataFi": data_fi,
                        "evolucions": evolucions,
                    }
                ],
            }
        ]
    ]
    return parse_snapshot(payload).episodis


def _nomes_tarda(**kwargs) -> tuple[Episodi, ...]:
    """The reference subject: one warning affecting Osona only in band `12-18`."""
    return _episodis([_evolucio({"12-18": [_afectacio()]})], **kwargs)


@pytest.fixture(autouse=True)
def warn_once_memo_reset() -> Iterator[None]:
    """Clear the module's warn-once memo around every test.

    `vigencia` warns the first time an emission trips a tolerance path and debugs
    afterwards, so a test that inherited another's memo would assert the wrong
    level: the state is reset both before and after, never carried across.
    """
    vigencia._incidencies_reportades.clear()
    yield
    vigencia._incidencies_reportades.clear()


def _nostres(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this module's records: `models.py` warns per unparseable field too."""
    return [rec for rec in caplog.records if rec.name == vigencia.__name__]


def _horitzons(
    episodis: tuple[Episodi, ...], clock: FakeClock
) -> tuple[list[AfectacioProjectada], list[AfectacioProjectada]]:
    """The two horizons of the same snapshot at the same instant."""
    return (
        afectacions_vigents(episodis, OSONA, clock()),
        afectacions_anunciades(episodis, OSONA, clock()),
    )


# ---------------------------------------------------------------------------
# The module stays pure Python (docs/04-architecture.md §5)
# ---------------------------------------------------------------------------

# Loads `models.py` and `vigencia.py` into a synthetic package in the child, so
# `vigencia`'s relative import resolves without going through
# `custom_components.avisoscat.__init__`, which does import Home Assistant. The
# child then projects a real payload and reports what it computed and what it
# loaded.
_ISOLATION_SCRIPT = """
import datetime as dt
import importlib.util
import json
import sys
import types

directory = sys.argv[1]
package = types.ModuleType("avisoscat_pure")
package.__path__ = [directory]
sys.modules[package.__name__] = package


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", f"{directory}/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    setattr(package, name, module)
    return module


models = load("models")
vigencia = load("vigencia")

raw = [[{"estat": "Obert",
         "meteor": {"idMeteor": None, "nom": "Vent"},
         "avisos": [{"tipus": "Avis", "estat": "Vigent",
                     "dataEmisio": "2026-08-04T15:30Z",
                     "dataInici": "2026-08-04T12:00Z",
                     "dataFi": "2026-08-05T23:59Z",
                     "evolucions": [{"dia": "2026-08-05T00:00Z",
                                     "periodes": [
                                         {"nom": "12-18",
                                          "afectacions": [{"perill": 3.0,
                                                           "idComarca": 24.0,
                                                           "nivell": 1.0}]},
                                         {"nom": "18-00", "afectacions": None}]}]}]}]]
episodis = models.parse_snapshot(raw).episodis
now = dt.datetime(2026, 8, 5, 13, 0, tzinfo=dt.UTC)
vigents = vigencia.afectacions_vigents(episodis, 24, now)
print(json.dumps({
    "home_assistant": sorted(
        name for name in sys.modules if name.split(".")[0] == "homeassistant"
    ),
    "periode_actual": vigencia.periode_actual(now),
    "vigents": [{"horitzo": af.horitzo.value,
                 "periode": af.periode,
                 "perill": af.perill,
                 "etiqueta_dia": af.etiqueta_dia,
                 "fi": af.fi.isoformat()} for af in vigents],
    "anunciades": len(vigencia.afectacions_anunciades(episodis, 24, now)),
}))
"""


def test_vigencia_projects_in_an_interpreter_without_home_assistant() -> None:
    """Validity logic must run in an interpreter that never imports HA.

    The contract of docs/04-architecture.md §5 is that this layer is pure Python
    over already-typed objects. A fresh child interpreter proves it by actually
    projecting a payload there and reporting the `homeassistant` modules it ended
    up with, which a grep over this file's source could never establish.
    """
    report = run_in_isolated_interpreter(
        _ISOLATION_SCRIPT, str(Path(vigencia.__file__).parent)
    )

    assert report["home_assistant"] == []
    # What ran in isolation is the real module, not an empty shell.
    assert report["periode_actual"] == "12-18"
    assert report["vigents"] == [
        {
            "horitzo": "vigent",
            "periode": "12-18",
            "perill": 3,
            "etiqueta_dia": "avui",
            "fi": "2026-08-05T18:00:00+00:00",
        }
    ]
    assert report["anunciades"] == 0


# ---------------------------------------------------------------------------
# The four 6-hour UTC bands (docs/01-data-sources.md §1.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hora", "minut", "esperat"),
    [
        (0, 0, "00-06"),
        (5, 59, "00-06"),
        (6, 0, "06-12"),
        (11, 59, "06-12"),
        (12, 0, "12-18"),
        (17, 59, "12-18"),
        (18, 0, "18-00"),
        (23, 59, "18-00"),
    ],
)
def test_periode_actual(clock: FakeClock, hora: int, minut: int, esperat: str) -> None:
    """Each instant falls in exactly one band, boundaries included."""
    clock.now = clock.now.replace(hour=hora, minute=minut)
    assert periode_actual(clock()) == esperat


def test_band_18_00_covers_18_00_to_23_59(clock: FakeClock) -> None:
    """The last band runs to the end of the day, not to 23:00 or to 00:00.

    Its name is `18-00` because that is what the JSON sends, but it does not
    wrap: it ends at the next midnight (docs/01-data-sources.md §1.2).
    """
    inici, fi = periode_bounds(AVUI, "18-00")
    assert inici == datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
    assert fi == datetime(2026, 8, 6, 0, 0, tzinfo=UTC)

    clock.now = datetime(2026, 8, 5, 23, 59, 59, tzinfo=UTC)
    assert periode_actual(clock()) == "18-00"
    clock.advance(seconds=1)
    assert periode_actual(clock()) == "00-06"


def test_band_18_00_warning_is_in_force_until_midnight(clock: FakeClock) -> None:
    """A `18-00` affectation applies at 23:59 UTC and no longer at 00:00."""
    episodis = _episodis([_evolucio({"18-00": [_afectacio()]})])

    clock.now = datetime(2026, 8, 5, 23, 59, tzinfo=UTC)
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1

    clock.advance(minutes=1)
    assert afectacions_vigents(episodis, OSONA, clock()) == []


def test_periode_bounds_rejects_an_unusable_name() -> None:
    """An unparseable band name has no interval, rather than a wrong one."""
    assert periode_bounds(AVUI, "vespre") is None


# ---------------------------------------------------------------------------
# The two horizons (docs/03-feature-spec.md §1.1)
# ---------------------------------------------------------------------------


def test_afternoon_warning_is_in_force_at_13_00(clock: FakeClock) -> None:
    """Inside its band, a warning is in force and no longer merely announced."""
    clock.advance(hours=1)  # 13:00 UTC, inside `12-18`
    vigents, anunciades = _horitzons(_nomes_tarda(), clock)

    assert [af.horitzo for af in vigents] == [Horitzo.VIGENT]
    assert anunciades == []
    afectacio = vigents[0]
    assert afectacio.periode == "12-18"
    assert afectacio.meteor is Meteor.VENT
    assert afectacio.tipus is TipusAvis.AVIS
    assert afectacio.perill == 3
    assert afectacio.nivell_perill == "alt"
    assert afectacio.inici == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    assert afectacio.fi == datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
    assert afectacio.hores_per_endavant == 0


def test_afternoon_warning_is_announced_at_11_59(clock: FakeClock) -> None:
    """One minute before its band, the same warning is announced, not in force."""
    clock.advance(minutes=-1)  # 11:59 UTC
    vigents, anunciades = _horitzons(_nomes_tarda(), clock)

    assert vigents == []
    assert [af.horitzo for af in anunciades] == [Horitzo.ANUNCIAT]
    assert anunciades[0].inici == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    assert anunciades[0].etiqueta_dia == "avui"


def test_afternoon_warning_is_neither_at_19_00(clock: FakeClock) -> None:
    """Once its band is over the warning is gone from both horizons."""
    clock.advance(hours=7)  # 19:00 UTC
    vigents, anunciades = _horitzons(_nomes_tarda(), clock)

    assert vigents == []
    assert anunciades == []
    assert [af.horitzo for af in projeccions(_nomes_tarda(), OSONA, clock())] == [
        Horitzo.PASSAT
    ]


def test_the_band_becomes_live_without_the_source_changing(clock: FakeClock) -> None:
    """The same bytes flip from announced to in force as the clock crosses 12:00.

    This is the whole reason the module exists: the coordinator can poll slowly
    because validity is recomputed against the clock, not against the payload.
    """
    episodis = _nomes_tarda()
    clock.now = datetime(2026, 8, 5, 11, 59, 59, tzinfo=UTC)
    assert afectacions_anunciades(episodis, OSONA, clock())
    clock.advance(seconds=1)
    assert afectacions_vigents(episodis, OSONA, clock())
    assert afectacions_anunciades(episodis, OSONA, clock()) == []


def test_warning_ending_mid_band_stops_at_its_own_end(clock: FakeClock) -> None:
    """`dataFi` inside a band ends the affectation there, not at the band's end."""
    episodis = _nomes_tarda(data_fi="2026-08-05T15:30Z")

    clock.now = datetime(2026, 8, 5, 15, 29, tzinfo=UTC)
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert vigents[0].fi == datetime(2026, 8, 5, 15, 30, tzinfo=UTC)

    clock.advance(minutes=1)  # 15:30, `dataFi` reached, band still running
    assert afectacions_vigents(episodis, OSONA, clock()) == []
    assert periode_actual(clock()) == "12-18"


def test_warning_starting_mid_band_is_announced_until_its_own_start(
    clock: FakeClock,
) -> None:
    """`dataInici` inside a band delays entry in force to that instant."""
    episodis = _nomes_tarda(data_inici="2026-08-05T14:00Z")

    assert afectacions_vigents(episodis, OSONA, clock()) == []  # 12:00, band open
    anunciades = afectacions_anunciades(episodis, OSONA, clock())
    assert anunciades[0].inici == datetime(2026, 8, 5, 14, 0, tzinfo=UTC)

    clock.advance(hours=2)
    assert afectacions_vigents(episodis, OSONA, clock())


def test_bands_entirely_past_the_warning_end_are_dropped(clock: FakeClock) -> None:
    """The feed keeps sending bands after `dataFi`; they no longer apply."""
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio()], "18-00": [_afectacio()]})],
        data_fi="2026-08-05T17:59Z",
    )
    assert [af.periode for af in projeccions(episodis, OSONA, clock())] == ["12-18"]


def test_hours_ahead_for_a_warning_issued_today_for_the_day_after_tomorrow(
    clock: FakeClock,
) -> None:
    """The advance notice of the day-after-tomorrow case of the spec example.

    Issued 2026-08-05 at 23:00 UTC for the `12-18` band of 2026-08-07, starting
    at 16:00 because the warning's own start clips the band: 41 hours ahead
    (docs/03-feature-spec.md §4.1).
    """
    clock.now = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)
    episodis = _episodis(
        [
            _evolucio(
                {"12-18": [_afectacio(dia="2026-08-07T00:00Z", perill=4.0)]},
                dia="2026-08-07T00:00Z",
            )
        ],
        data_emissio="2026-08-05T23:00Z",
        data_inici="2026-08-07T16:00Z",
        data_fi="2026-08-07T23:59Z",
    )

    anunciades = afectacions_anunciades(episodis, OSONA, clock())
    assert len(anunciades) == 1
    afectacio = anunciades[0]
    assert afectacio.inici == datetime(2026, 8, 7, 16, 0, tzinfo=UTC)
    assert afectacio.hores_per_endavant == 41
    assert afectacio.anunciat_amb_hores == 41
    assert afectacio.dia == DEMA_PASSAT
    assert afectacio.dies_per_endavant == 2
    assert afectacio.etiqueta_dia == "dema_passat"
    assert afectacio.periode == "12-18"


def test_hours_ahead_is_truncated_not_rounded(clock: FakeClock) -> None:
    """Two hours and forty minutes ahead reads as two hours, never as three."""
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio()]})], data_inici="2026-08-05T14:40Z"
    )
    assert afectacions_anunciades(episodis, OSONA, clock())[0].hores_per_endavant == 2


def test_announced_warning_for_tomorrow_is_labelled_tomorrow(clock: FakeClock) -> None:
    """The relative-day label follows the UTC date, per horizon entry."""
    episodis = _episodis(
        [
            _evolucio({"12-18": [_afectacio()]}),
            _evolucio(
                {"06-12": [_afectacio(dia="2026-08-06T00:00Z")]},
                dia="2026-08-06T00:00Z",
            ),
        ]
    )
    anunciades = afectacions_anunciades(episodis, OSONA, clock())
    assert [af.etiqueta_dia for af in anunciades] == ["dema"]
    assert anunciades[0].dia == DEMA


def test_projections_are_ordered_by_severity_then_the_peak_is_first(
    clock: FakeClock,
) -> None:
    """`[0]` of a horizon is its peak, so the sensor does not have to sort."""
    episodis = _episodis(
        [
            _evolucio(
                {
                    "12-18": [
                        _afectacio(perill=2.0),
                        _afectacio(perill=5.0, nivell=2.0),
                        _afectacio(perill=4.0),
                    ]
                }
            )
        ]
    )
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert [af.perill for af in vigents] == [5, 4, 2]
    assert pic(vigents).perill == 5


def test_pic_of_nothing_is_none() -> None:
    """No affectation means no peak, not a zero-grade stand-in."""
    assert pic([]) is None


def test_a_single_walk_serves_both_horizons(clock: FakeClock) -> None:
    """`projeccions()` is the shared walk both horizons are filtered out of."""
    episodis = _episodis(
        [
            _evolucio(
                {"06-12": [_afectacio(perill=2.0)], "18-00": [_afectacio(perill=4.0)]}
            )
        ]
    )
    totes = projeccions(episodis, OSONA, clock())
    assert [af.horitzo for af in totes] == [Horitzo.PASSAT, Horitzo.ANUNCIAT]
    assert afectacions_per_horitzo(totes, Horitzo.ANUNCIAT) == afectacions_anunciades(
        episodis, OSONA, clock()
    )


# ---------------------------------------------------------------------------
# Three-day outlook (docs/03-feature-spec.md §3.4)
# ---------------------------------------------------------------------------


def test_outlook_returns_four_bands_for_each_of_three_days(clock: FakeClock) -> None:
    """The grid is complete and zero-filled: a calm band is not a missing band."""
    episodis = _episodis(
        [
            _evolucio({"12-18": [_afectacio(perill=2.0)]}),
            _evolucio(
                {
                    "18-00": [
                        _afectacio(dia="2026-08-06T00:00Z", perill=5.0, nivell=2.0)
                    ]
                },
                dia="2026-08-06T00:00Z",
            ),
        ]
    )
    graella = outlook(episodis, OSONA, clock())

    assert [dia.dia for dia in graella] == [AVUI, DEMA, DEMA_PASSAT]
    assert [dia.etiqueta for dia in graella] == ["avui", "dema", "dema_passat"]
    assert all(len(dia.periodes) == len(PERIODES) for dia in graella)
    assert graella[0].graella == {"00-06": 0, "06-12": 0, "12-18": 2, "18-00": 0}
    assert graella[1].graella == {"00-06": 0, "06-12": 0, "12-18": 0, "18-00": 5}
    assert graella[2].graella == dict.fromkeys(PERIODES, 0)
    assert [dia.perill_maxim for dia in graella] == [2, 5, 0]
    assert graella[1].pic.nivell == 2
    assert graella[2].pic is None
    assert graella[0].periodes[2].pic.perill == 2
    assert graella[0].periodes[0].pic is None


def test_outlook_cells_carry_their_own_interval(clock: FakeClock) -> None:
    """Each cell knows its UTC interval, so a dashboard need not recompute it."""
    dia = outlook([], OSONA, clock())[1]
    assert dia.periodes[3].inici == datetime(2026, 8, 6, 18, 0, tzinfo=UTC)
    assert dia.periodes[3].fi == datetime(2026, 8, 7, 0, 0, tzinfo=UTC)


def test_outlook_covers_a_band_already_gone_by(clock: FakeClock) -> None:
    """This morning's grade stays in today's grid even though it is over."""
    episodis = _episodis([_evolucio({"06-12": [_afectacio(perill=1.0)]})])
    avui = outlook(episodis, OSONA, clock())[0]
    assert avui.graella["06-12"] == 1
    assert avui.periodes[1].afectacions[0].horitzo is Horitzo.PASSAT


def test_outlook_beyond_the_smp_horizon_labels_the_offset(clock: FakeClock) -> None:
    """A fourth day has no name in the spec, so it reports its offset."""
    graella = outlook([], OSONA, clock(), dies=DIES_OUTLOOK + 1)
    assert graella[3].etiqueta == "3"
    assert etiqueta_dia(-1) == "-1"


# ---------------------------------------------------------------------------
# Violent weather: two hours from the issue time, never announced
# ---------------------------------------------------------------------------

TEMPS_VIOLENT = "Avís Vigilància per Temps Violent"


def _temps_violent(
    *,
    data_emissio: str | None = "2026-08-05T11:30Z",
    data_fi: str | None = "2026-08-05T23:59Z",
    periodes: dict[str, list[dict] | None] | None = None,
) -> tuple[Episodi, ...]:
    """A nowcast issued at 11:30 UTC, listed under the band that contains it.

    `data_fi` defaults to the end of the day, well past the two-hour window, so a
    test only sees the clipping when it asks for it.
    """
    return _episodis(
        [_evolucio(periodes if periodes is not None else {"06-12": [_afectacio()]})],
        meteor="Temps violent",
        tipus=TEMPS_VIOLENT,
        data_emissio=data_emissio,
        data_inici=data_emissio,
        data_fi=data_fi,
    )


def _temps_violent_directes(
    *,
    data_emissio: str | None = "2026-08-05T11:30Z",
    data_fi: str | None = "2026-08-05T23:59Z",
    afectacions: list[dict] | None = None,
) -> tuple[Episodi, ...]:
    """A nowcast in the live trap-12 shape: affectations directly off the avis.

    No `evolucions` key at all, the shape measured 2026-08-06: a band walk alone
    finds nothing, so only the directes half of the union reaches the comarca.
    """
    payload = [
        [
            {
                "id": None,
                "estat": {"nom": "Obert", "data": None},
                "meteor": {"idMeteor": None, "nom": "Temps violent"},
                "avisos": [
                    {
                        "tipus": TEMPS_VIOLENT,
                        "estat": "Vigent",
                        "dataEmisio": data_emissio,
                        "dataInici": data_emissio,
                        "dataFi": data_fi,
                        "afectacions": afectacions
                        if afectacions is not None
                        else [_afectacio(perill=6.0, nivell=2.0, dia=None)],
                    }
                ],
            }
        ]
    ]
    return parse_snapshot(payload).episodis


def test_violent_weather_projects_against_the_live_directes_shape(
    clock: FakeClock,
) -> None:
    """A real vigilance avis carries its affectations off the avis, not evolucions.

    The live shape measured 2026-08-06 (trap #12) has `evolucions` empty and the
    affectation in `afectacions_directes`. A band-only walk finds nothing for it,
    so the violent dispatch must read the union: a grade-6 hail nowcast is then in
    force for its two-hour window rather than silently reading as no danger.
    """
    episodis = _temps_violent_directes()

    vigents = afectacions_vigents(episodis, OSONA, clock())  # 12:00, next band
    assert len(vigents) == 1
    afectacio = vigents[0]
    assert afectacio.is_temps_violent
    assert afectacio.perill == 6
    assert afectacio.inici == datetime(2026, 8, 5, 11, 30, tzinfo=UTC)
    assert afectacio.fi == afectacio.inici + FINESTRA_TEMPS_VIOLENT


def test_violent_weather_is_in_force_for_two_hours_ignoring_bands(
    clock: FakeClock,
) -> None:
    """Issued 11:30 in band `06-12`, still in force at 12:00 and until 13:30."""
    episodis = _temps_violent()

    vigents = afectacions_vigents(episodis, OSONA, clock())  # 12:00, next band
    assert len(vigents) == 1
    afectacio = vigents[0]
    assert afectacio.is_temps_violent
    assert afectacio.tipus is TipusAvis.TEMPS_VIOLENT
    assert afectacio.inici == datetime(2026, 8, 5, 11, 30, tzinfo=UTC)
    assert afectacio.fi == afectacio.inici + FINESTRA_TEMPS_VIOLENT
    assert afectacio.periode == "06-12"

    clock.now = datetime(2026, 8, 5, 13, 29, tzinfo=UTC)
    assert afectacions_vigents(episodis, OSONA, clock())
    clock.advance(minutes=1)  # 13:30, the window closes
    assert afectacions_vigents(episodis, OSONA, clock()) == []


def test_violent_weather_is_in_force_before_its_listed_band_starts(
    clock: FakeClock,
) -> None:
    """The listed band is irrelevant: only the two-hour window decides.

    Listed under `18-00` but issued at 11:30, it is in force at 12:00 anyway,
    which a band-based reading would get exactly backwards.
    """
    episodis = _temps_violent(periodes={"18-00": [_afectacio()]})
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1


def test_violent_weather_is_never_announced(clock: FakeClock) -> None:
    """By the time a nowcast exists it is already in force.

    A future-dated emission (clock skew, or a source mistake) reports nothing
    rather than becoming a forecast.
    """
    episodis = _temps_violent(data_emissio="2026-08-05T14:00Z")
    assert afectacions_anunciades(episodis, OSONA, clock()) == []
    assert projeccions(episodis, OSONA, clock()) == []

    # And within its window it is in force, never announced.
    clock.advance(hours=3)
    assert afectacions_anunciades(episodis, OSONA, clock()) == []
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1


def test_violent_weather_listed_in_two_bands_counts_once(clock: FakeClock) -> None:
    """One nowcast is one live warning, whichever bands the feed files it under."""
    episodis = _temps_violent(
        periodes={
            "06-12": [_afectacio(perill=3.0)],
            "12-18": [_afectacio(perill=5.0, nivell=2.0)],
        }
    )
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert len(vigents) == 1
    assert vigents[0].perill == 5  # the most severe of the two entries


def test_violent_weather_window_spans_the_bands_it_overlaps(clock: FakeClock) -> None:
    """A window straddling 18:00 shows up in both cells of the outlook grid."""
    episodis = _temps_violent(data_emissio="2026-08-05T17:30Z")
    clock.now = datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
    avui = outlook(episodis, OSONA, clock())[0]
    assert avui.graella["12-18"] == 3
    assert avui.graella["18-00"] == 3


def test_violent_weather_crossing_midnight_is_in_force_today(clock: FakeClock) -> None:
    """A window issued at 23:30 and read at 00:30 is in force, and it is `avui`.

    The relative day of a live nowcast is the day it is read in: the emission's
    own date would read as `-1` day ahead, outside the `avui`/`dema`/`dema_passat`
    enumeration the events carry (docs/03-feature-spec.md §4.1). Being never
    announced is unaffected by the crossing.
    """
    episodis = _temps_violent(
        data_emissio="2026-08-05T23:30Z", data_fi="2026-08-06T01:30Z"
    )
    clock.now = datetime(2026, 8, 6, 0, 30, tzinfo=UTC)

    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert len(vigents) == 1
    afectacio = vigents[0]
    assert afectacio.horitzo is Horitzo.VIGENT
    assert afectacio.fi == datetime(2026, 8, 6, 1, 30, tzinfo=UTC)
    assert afectacio.dia == DEMA  # the day it is read in, i.e. the 6th
    assert afectacio.dies_per_endavant == 0
    assert afectacio.etiqueta_dia == "avui"
    assert afectacions_anunciades(episodis, OSONA, clock()) == []

    # Once the window has closed it belongs to the day it was issued on again.
    clock.now = datetime(2026, 8, 6, 1, 30, tzinfo=UTC)
    passades = projeccions(episodis, OSONA, clock())
    assert [(af.horitzo, af.dia) for af in passades] == [(Horitzo.PASSAT, AVUI)]


def test_violent_weather_window_is_clipped_by_its_own_data_fi(
    clock: FakeClock,
) -> None:
    """A `dataFi` inside the two-hour window ends the nowcast there.

    The same clipping every other type gets: a projection reported in force after
    the end the source itself declared would contradict the `data_fi` it carries.
    """
    episodis = _temps_violent(data_fi="2026-08-05T12:30Z")  # issued 11:30

    vigents = afectacions_vigents(episodis, OSONA, clock())  # 12:00, still open
    assert len(vigents) == 1
    assert vigents[0].fi == datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
    assert vigents[0].fi < vigents[0].inici + FINESTRA_TEMPS_VIOLENT

    clock.advance(minutes=30)  # 12:30, the declared end
    assert afectacions_vigents(episodis, OSONA, clock()) == []
    # And the two hours are over too, so nothing comes back later either.
    clock.advance(minutes=30)
    assert afectacions_vigents(episodis, OSONA, clock()) == []


@pytest.mark.parametrize("data_fi", ["2026-08-05T11:30Z", "2026-08-05T11:29Z"])
def test_violent_weather_ending_at_its_issue_time_keeps_its_two_hours(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, data_fi: str
) -> None:
    """A `dataFi` at or before the emission is not usable as a window bound.

    The nowcast keeps its full two hours and the shape is reported, because a
    nowcast that vanishes is worse than one that lingers: a maximum-grade hail
    warning reading as no danger is the failure this integration guards against,
    and no real temps-violent payload has ever been captured to say which shape
    the source actually sends.
    """
    episodis = _temps_violent(data_fi=data_fi)  # issued 11:30

    with caplog.at_level(logging.WARNING, logger=vigencia.__name__):
        vigents = afectacions_vigents(episodis, OSONA, clock())  # 12:00

    assert len(vigents) == 1
    assert vigents[0].fi == datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
    assert vigents[0].fi == vigents[0].inici + FINESTRA_TEMPS_VIOLENT
    assert "at or before its issue time" in caplog.text
    assert "'Temps violent'" in caplog.text
    assert [rec.levelno for rec in _nostres(caplog)] == [logging.WARNING]

    clock.advance(hours=2)  # 14:00, past the window
    assert afectacions_vigents(episodis, OSONA, clock()) == []


def test_violent_weather_without_an_end_keeps_its_two_hours(clock: FakeClock) -> None:
    """No `dataFi` at all clips nothing, and says nothing either."""
    episodis = _temps_violent(data_fi=None)
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert len(vigents) == 1
    assert vigents[0].fi == vigents[0].inici + FINESTRA_TEMPS_VIOLENT


def test_violent_weather_without_an_issue_time_is_ignored(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """No issue time means no window to compute, and it is said out loud.

    At debug level, like the other tolerance paths of the once-a-minute walk.
    """
    episodis = _temps_violent(data_emissio=None)
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        assert projeccions(episodis, OSONA, clock()) == []
    assert "without an issue time" in caplog.text
    assert [rec.levelno for rec in caplog.records] == [logging.DEBUG]


def test_violent_weather_without_an_issue_time_is_silent_for_other_comarques(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """One entry per comarca, so a nowcast that never names ours says nothing.

    Logging before the comarca filter would make every configured comarca report
    a nowcast none of them appears in.
    """
    episodis = _temps_violent(
        data_emissio=None, periodes={"06-12": [_afectacio(id_comarca=ALT_EMPORDA)]}
    )
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        assert projeccions(episodis, OSONA, clock()) == []
    assert caplog.records == []
    # And the comarca it does name still reports the missing issue time.
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        assert projeccions(episodis, ALT_EMPORDA, clock()) == []
    assert "without an issue time" in caplog.text


def test_violent_weather_without_an_affectation_for_the_comarca_is_ignored(
    clock: FakeClock,
) -> None:
    """A nowcast that does not name the comarca says nothing about it."""
    episodis = _temps_violent(periodes={"06-12": [_afectacio(id_comarca=ALT_EMPORDA)]})
    assert projeccions(episodis, OSONA, clock()) == []


# ---------------------------------------------------------------------------
# Official local time must not leak into the bands
# ---------------------------------------------------------------------------


def test_summer_official_time_does_not_shift_the_bands(clock: FakeClock) -> None:
    """14:00 in Barcelona in August is 12:00 UTC, so band `12-18` opens then."""
    episodis = _nomes_tarda()

    clock.now = datetime(2026, 8, 5, 14, 0, tzinfo=MADRID)  # 12:00 UTC
    assert periode_actual(clock()) == "12-18"
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1

    # The proof the offset does not leak in: local 12:00 is 10:00 UTC, two hours
    # before the band opens, so the warning is announced and not in force.
    clock.now = datetime(2026, 8, 5, 12, 0, tzinfo=MADRID)
    assert periode_actual(clock()) == "06-12"
    assert afectacions_vigents(episodis, OSONA, clock()) == []
    assert afectacions_anunciades(episodis, OSONA, clock())[0].hores_per_endavant == 2


def test_winter_official_time_does_not_shift_the_bands(clock: FakeClock) -> None:
    """13:00 in Barcelona in January is 12:00 UTC: one hour of offset, same UTC."""
    episodis = _episodis(
        [
            _evolucio(
                {"12-18": [_afectacio(dia="2026-01-15T00:00Z")]},
                dia="2026-01-15T00:00Z",
            )
        ],
        data_emissio="2026-01-14T15:30Z",
        data_inici="2026-01-14T12:00Z",
        data_fi="2026-01-15T23:59Z",
    )

    clock.now = datetime(2026, 1, 15, 13, 0, tzinfo=MADRID)  # 12:00 UTC
    assert periode_actual(clock()) == "12-18"
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1

    clock.now = datetime(2026, 1, 15, 12, 0, tzinfo=MADRID)  # 11:00 UTC
    assert periode_actual(clock()) == "06-12"
    assert afectacions_vigents(episodis, OSONA, clock()) == []


def test_a_naive_instant_is_read_as_utc(clock: FakeClock) -> None:
    """A caller that loses the tzinfo gets UTC, never the machine's local zone."""
    clock.now = datetime(2026, 8, 5, 13, 0)  # naive on purpose
    assert periode_actual(clock()) == "12-18"
    assert len(afectacions_vigents(_nomes_tarda(), OSONA, clock())) == 1


# ---------------------------------------------------------------------------
# Tolerance: nothing is dropped silently
# ---------------------------------------------------------------------------


def test_only_the_asked_comarca_is_reported(clock: FakeClock) -> None:
    """The projection is per comarca: a neighbour's warning is not ours."""
    episodis = _episodis(
        [
            _evolucio(
                {
                    "12-18": [
                        _afectacio(id_comarca=ALT_EMPORDA, perill=6.0),
                        _afectacio(perill=2.0),
                    ]
                }
            )
        ]
    )
    assert [af.perill for af in afectacions_vigents(episodis, OSONA, clock())] == [2]
    assert [
        af.perill for af in afectacions_vigents(episodis, ALT_EMPORDA, clock())
    ] == [6]


def test_a_maritime_zone_is_just_another_id(clock: FakeClock) -> None:
    """The adjacent sea is asked for by its own id (docs §3.7), same machinery."""
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(id_comarca=MAR_MARESME, perill=4.0)]})]
    )
    assert afectacions_vigents(episodis, MAR_MARESME, clock())[0].perill == 4
    assert afectacions_vigents(episodis, OSONA, clock()) == []


def test_closed_episodes_and_emissions_are_skipped(clock: FakeClock) -> None:
    """An explicitly closed state is the one thing that removes a warning."""
    clock.advance(hours=1)
    assert projeccions(_nomes_tarda(estat="Anul·lat"), OSONA, clock()) == []
    assert projeccions(_nomes_tarda(estat_episodi="Tancat"), OSONA, clock()) == []


def test_an_unknown_status_still_counts_as_open(clock: FakeClock) -> None:
    """`Ampliat` was observed live and is not a closure literal (trap #1)."""
    clock.advance(hours=1)
    assert len(afectacions_vigents(_nomes_tarda(estat="Ampliat"), OSONA, clock())) == 1


def test_grade_zero_is_kept_for_the_consumer_to_filter(clock: FakeClock) -> None:
    """An unreadable grade also parses as 0, so dropping 0 would lose real data."""
    clock.advance(hours=1)
    episodis = _episodis([_evolucio({"12-18": [_afectacio(perill="?")]})])
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert [af.perill for af in vigents] == [0]
    assert vigents[0].nivell_perill == "cap"


def test_the_band_alias_of_the_written_documentation_resolves(clock: FakeClock) -> None:
    """`18-24` is what the SMC's prose calls the band the JSON names `18-00`."""
    episodis = _episodis([_evolucio({"18-24": [_afectacio()]})])
    clock.now = datetime(2026, 8, 5, 20, 0, tzinfo=UTC)
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert [af.periode for af in vigents] == ["18-00"]
    assert outlook(episodis, OSONA, clock())[0].graella["18-00"] == 3


@pytest.mark.parametrize(
    ("nom", "hora"),
    [
        ("09-12", 10),
        # A band the SMC invents ending at midnight, written the way the JSON
        # writes `18-00`: the zero end hour still means the end of the day.
        ("20-00", 22),
    ],
)
def test_an_unknown_but_parseable_band_keeps_its_own_name(
    clock: FakeClock, nom: str, hora: int
) -> None:
    """A band the SMC invents later still places its affectations."""
    episodis = _episodis([_evolucio({nom: [_afectacio()]})])
    clock.now = datetime(2026, 8, 5, hora, 0, tzinfo=UTC)
    assert [af.periode for af in afectacions_vigents(episodis, OSONA, clock())] == [nom]


@pytest.mark.parametrize("nom", ["0-0", "00-00", "12-12"])
def test_a_band_that_spans_nothing_is_not_read_as_a_whole_day(
    clock: FakeClock, nom: str
) -> None:
    """A band whose two hours are the same places nothing, so it is dropped.

    The "hour 0 means the end of the day" reading exists for `18-00`; applying it
    to `0-0` would invent a 24-hour affectation, and because the outlook places
    cells by overlap it would light up all four bands of the day at that grade.
    """
    episodis = _episodis([_evolucio({nom: [_afectacio(perill=5.0)]})])
    assert projeccions(episodis, OSONA, clock()) == []
    assert outlook(episodis, OSONA, clock())[0].graella == dict.fromkeys(PERIODES, 0)


def test_an_unusable_band_name_is_ignored_and_logged_at_debug(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A band that cannot be placed in time is dropped, never guessed.

    At debug level on purpose: this walk repeats every minute per config entry,
    so a warning would repeat ~1440 times a day for one malformed field. Nothing
    reaches the log at warning level.
    """
    episodis = _episodis([_evolucio({"vespre": [_afectacio()]})])
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        assert projeccions(episodis, OSONA, clock()) == []
    assert "Unusable SMP time band" in caplog.text
    assert [rec.name for rec in caplog.records] == [vigencia.__name__]
    assert [rec.levelno for rec in caplog.records] == [logging.DEBUG]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=vigencia.__name__):
        assert projeccions(episodis, OSONA, clock()) == []
    assert caplog.records == []


@pytest.mark.parametrize(
    ("dia_afectacio", "dia_evolucio"),
    [
        (None, "2026-08-05T00:00Z"),  # falls back to the evolution's day
        (None, None),  # falls back to the warning's own start
    ],
)
def test_a_missing_day_falls_back_instead_of_dropping_the_affectation(
    clock: FakeClock, dia_afectacio: str | None, dia_evolucio: str | None
) -> None:
    """Both `dia` fields are optional in the model; the warning's start remains."""
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(dia=dia_afectacio)]}, dia=dia_evolucio)],
        data_inici="2026-08-05T00:00Z",
    )
    clock.advance(hours=1)
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1


def _dies_illegibles(
    *,
    dies: int = 3,
    perills: list[float] | None = None,
    comentaris: list[str] | None = None,
    data_inici: str = "2026-08-05T00:00Z",
    data_fi: str | None = "2026-08-07T23:59Z",
) -> tuple[Episodi, ...]:
    """A warning whose every `dia` reads `05/08/2026`, i.e. none of them parses.

    The plausible upstream break: the source switches to the local date format
    the CECAT already uses, `models.py` rejects every `dia` at once, and the only
    dates left are the warning's own. Each forecast day carries one `12-18`
    affectation, with its own grade and comment when asked for, because a
    multi-day warning normally differs from day to day and a collapse that only
    dropped identical clones would not be a collapse at all.
    """
    illegible = "05/08/2026"
    graus = perills if perills is not None else [3.0] * dies
    textos = comentaris if comentaris is not None else ["Ratxes al litoral."] * dies
    return _episodis(
        [
            _evolucio(
                {"12-18": [_afectacio(dia=illegible, perill=grau)]},
                dia=illegible,
                comentari=text,
            )
            for grau, text in zip(graus, textos, strict=True)
        ],
        data_inici=data_inici,
        data_fi=data_fi,
    )


def test_unparseable_days_are_placed_one_per_forecast_day(clock: FakeClock) -> None:
    """Three undatable forecast days are three days, not three copies of one.

    The evolutions arrive in chronological daily order from `dataInici`'s date
    (docs/01-data-sources.md §6 trap #12), so the nth one is placed n days on.
    Collapsing them all onto the start date would make a count sensor read 3 for
    a single band of a single comarca.
    """
    episodis = _dies_illegibles()
    clock.advance(hours=1)  # 13:00, inside the `12-18` band of the first day

    projectades = projeccions(episodis, OSONA, clock())
    assert [(af.dia, af.periode) for af in projectades] == [
        (AVUI, "12-18"),
        (DEMA, "12-18"),
        (DEMA_PASSAT, "12-18"),
    ]
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert len(vigents) == 1
    assert vigents[0].dia == AVUI
    anunciades = afectacions_anunciades(episodis, OSONA, clock())
    assert [af.etiqueta_dia for af in anunciades] == ["dema", "dema_passat"]


@pytest.mark.parametrize(
    ("dies", "data_fi"),
    [
        (3, "2026-08-05T23:59Z"),  # the derived days run past the declared end
        (4, None),  # and past the three-day SMP horizon
    ],
)
def test_the_derived_days_fall_back_when_the_inference_breaks(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, dies: int, data_fi: str | None
) -> None:
    """One day per evolution is an inference, checked against the warning itself.

    When the derived days would run past the warning's own `dataFi` or past the
    documented three-day horizon, the feed no longer has the shape the capture
    showed. It is reported, everything falls back to the start date, and the
    per-(day, band) collapse leaves one projection: the most severe. The days
    differ in grade and comment here, which is the normal shape of a multi-day
    warning and the case a byte-identical dedupe would not have collapsed.
    """
    graus = [3.0, 5.0, 4.0, 2.0][:dies]
    episodis = _dies_illegibles(
        dies=dies,
        perills=graus,
        comentaris=[f"Dia {index}." for index in range(dies)],
        data_fi=data_fi,
    )
    clock.advance(hours=1)

    with caplog.at_level(logging.WARNING, logger=vigencia.__name__):
        projectades = projeccions(episodis, OSONA, clock())

    assert "forecast days with no usable date" in caplog.text
    assert "'Vent'" in caplog.text  # the meteor, not the useless `Avís` literal
    assert [rec.levelno for rec in _nostres(caplog)] == [logging.WARNING]
    assert [(af.dia, af.periode, af.horitzo, af.perill) for af in projectades] == [
        (AVUI, "12-18", Horitzo.VIGENT, 5)
    ]
    assert len(afectacions_vigents(episodis, OSONA, clock())) == 1


@pytest.mark.parametrize(
    "ordre",
    [(0, 1, 2), (2, 1, 0), (1, 2, 0)],
)
def test_the_fallback_collapse_does_not_depend_on_arrival_order(
    clock: FakeClock, ordre: tuple[int, ...]
) -> None:
    """Equal grades are broken by the projection's own content, never by order.

    The order of the affectations inside the payload is not a property the source
    guarantees between requests, so a collapse that let it decide would make the
    reported comment flip from poll to poll with the data unchanged.
    """
    comentaris = ["Aiguats al prelitoral.", "Calamarsa al pla.", "Ratxes al litoral."]
    episodis = _dies_illegibles(
        perills=[4.0, 4.0, 4.0],
        comentaris=[comentaris[index] for index in ordre],
        data_fi="2026-08-05T23:59Z",  # trips the guard, so everything collapses
    )
    clock.advance(hours=1)

    projectades = projeccions(episodis, OSONA, clock())
    assert [(af.perill, af.comentari) for af in projectades] == [(4, comentaris[0])]


@pytest.mark.parametrize("dies", [1, 2])
def test_the_fallback_collapse_handles_the_degenerate_counts(
    clock: FakeClock, dies: int
) -> None:
    """One projection stays one, and a collapse of nothing is nothing.

    `dataFi` a day before `dataInici` trips the guard for any number of forecast
    days; with the band clipped away by that same `dataFi` there is nothing left
    to collapse, which must be no projection rather than an error.
    """
    clock.advance(hours=1)
    episodis = _dies_illegibles(dies=dies, data_fi="2026-08-04T23:59Z")
    assert projeccions(episodis, OSONA, clock()) == []

    # And the same guard with a usable end keeps exactly one projection.
    episodis = _dies_illegibles(dies=dies, data_fi="2026-08-05T14:00Z")
    projectades = projeccions(episodis, OSONA, clock())
    assert [(af.dia, af.fi) for af in projectades] == [
        (AVUI, datetime(2026, 8, 5, 14, 0, tzinfo=UTC))
    ]


def test_the_derived_day_guard_warns_once_and_then_debugs(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard says it once per emission, not once per recompute.

    One recompute cycle is three walks (the two horizons and the grid), and the
    recompute itself runs every minute per config entry, so a warning per walk
    would be thousands of identical lines a day and would bury the signal.
    """
    episodis = _dies_illegibles(dies=4, data_fi=None)
    clock.advance(hours=1)

    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        projeccions(episodis, OSONA, clock())
    assert [rec.levelno for rec in _nostres(caplog)] == [logging.WARNING]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        afectacions_vigents(episodis, OSONA, clock())
        afectacions_anunciades(episodis, OSONA, clock())
        outlook(episodis, OSONA, clock())
    repeticions = _nostres(caplog)
    assert repeticions  # the message is still there, just not at warning level
    assert {rec.levelno for rec in repeticions} == {logging.DEBUG}


def test_the_report_memo_is_bounded_by_the_walked_snapshot(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The memo holds only emissions of the snapshot just walked.

    Same purge discipline as `announced_seen` (docs/04-architecture.md §8): an
    emission that leaves the snapshot is forgotten, so the set cannot grow without
    limit, and if it comes back it is worth saying again.
    """
    episodis = _dies_illegibles(dies=4, data_fi=None)
    clock.advance(hours=1)
    projeccions(episodis, OSONA, clock())

    assert vigencia._incidencies_reportades == {
        (
            vigencia._MOTIU_DIES_DERIVATS,
            ("Vent", "Avís", datetime(2026, 8, 4, 15, 30, tzinfo=UTC)),
        )
    }

    projeccions((), OSONA, clock())  # the emission is gone from the snapshot
    assert vigencia._incidencies_reportades == set()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=vigencia.__name__):
        projeccions(episodis, OSONA, clock())  # and it comes back
    assert [rec.levelno for rec in _nostres(caplog)] == [logging.WARNING]


def test_a_warning_with_dates_never_triggers_the_derived_day_guard(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The inference is only reached by an affectation that needs it.

    Four dated forecast days would break the horizon check if it were applied
    eagerly, so a payload whose dates all parse must stay silent.
    """
    episodis = _episodis(
        [
            _evolucio(
                {"12-18": [_afectacio(dia=f"2026-08-0{5 + offset}T00:00Z")]},
                dia=f"2026-08-0{5 + offset}T00:00Z",
            )
            for offset in range(4)
        ],
        data_fi="2026-08-08T23:59Z",
    )
    with caplog.at_level(logging.WARNING, logger=vigencia.__name__):
        assert len(projeccions(episodis, OSONA, clock())) == 4
    assert caplog.records == []


def test_identical_affectations_are_reported_once(clock: FakeClock) -> None:
    """The same band, comarca, grade and interval twice is one affectation.

    The feed repeating an entry must not double a count sensor; anything that
    differs in any field is still its own projection.
    """
    clock.advance(hours=1)
    episodis = _episodis(
        [
            _evolucio(
                {
                    "12-18": [
                        _afectacio(perill=3.0),
                        _afectacio(perill=3.0),
                        _afectacio(perill=4.0),
                    ]
                }
            )
        ]
    )
    assert [af.perill for af in afectacions_vigents(episodis, OSONA, clock())] == [4, 3]


def test_an_undatable_affectation_is_ignored_and_logged_at_debug(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """With no day anywhere there is no interval to compute, and we say so.

    At debug level, for the same once-a-minute reason as the unusable band.
    """
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(dia=None)]}, dia=None)],
        data_inici=None,
    )
    with caplog.at_level(logging.DEBUG, logger=vigencia.__name__):
        assert projeccions(episodis, OSONA, clock()) == []
    assert "Undatable SMP affectation" in caplog.text
    assert [rec.levelno for rec in caplog.records] == [logging.DEBUG]


def test_an_open_ended_warning_stays_in_force(clock: FakeClock) -> None:
    """Missing `dataInici`/`dataFi` clip nothing: the band alone decides."""
    clock.advance(hours=1)
    episodis = _nomes_tarda(data_inici=None, data_fi=None)
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert vigents[0].inici == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    assert vigents[0].fi == datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
    # Emitted the previous day at 15:30 for a band starting at 12:00: 20 h of
    # real notice, whole hours, truncated.
    assert vigents[0].anunciat_amb_hores == 20


def test_notice_is_unknown_without_an_emission_time(clock: FakeClock) -> None:
    """`anunciat_amb_hores` is `None`, not 0: unknown notice is not zero notice."""
    clock.advance(hours=1)
    episodis = _nomes_tarda(data_emissio=None)
    assert afectacions_vigents(episodis, OSONA, clock())[0].anunciat_amb_hores is None


@pytest.mark.parametrize(
    ("nivell", "esperat"),
    [
        (1.0, "Ratxa màxima > 72 km/h (20 m/s)"),
        (2.0, "Ratxa màxima > 108 km/h (30 m/s)"),
    ],
)
def test_the_threshold_falls_back_to_the_one_of_the_matching_level(
    clock: FakeClock, nivell: float, esperat: str
) -> None:
    """Without its own `llindar`, the affectation takes the day's, by level.

    Taking the low threshold for a high-threshold affectation would understate
    the warning in the sensor attribute.
    """
    clock.advance(hours=1)
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(llindar="", nivell=nivell)]})]
    )
    assert afectacions_vigents(episodis, OSONA, clock())[0].llindar == esperat


def test_the_threshold_is_empty_when_the_day_carries_none(clock: FakeClock) -> None:
    """No threshold anywhere reads as no text, never as `None` in an attribute."""
    clock.advance(hours=1)
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(llindar="")]}, llindar1=None, llindar2=None)]
    )
    assert afectacions_vigents(episodis, OSONA, clock())[0].llindar == ""


def test_the_warning_text_is_carried_verbatim(clock: FakeClock) -> None:
    """Untrusted external text is passed through unchanged, never reshaped."""
    clock.advance(hours=1)
    afectacio = afectacions_vigents(_nomes_tarda(), OSONA, clock())[0]
    assert afectacio.comentari == "Ratxes molt fortes al litoral."
    assert afectacio.distribucio_geografica == "EXTENSA"


def test_an_empty_snapshot_projects_to_nothing(clock: FakeClock) -> None:
    """No episode, no affectation, and an all-zero grid rather than an error."""
    assert projeccions([], OSONA, clock()) == []
    assert afectacions_vigents([], OSONA, clock()) == []
    assert afectacions_anunciades([], OSONA, clock()) == []
    assert all(dia.perill_maxim == 0 for dia in outlook([], OSONA, clock()))


# ---------------------------------------------------------------------------
# Pre-warnings: Catalonia-wide, no comarca and no band
# ---------------------------------------------------------------------------


def _preavisos(*raws: dict) -> tuple:
    """Parse a pre-warning payload, which has its own flat shape."""
    return parse_snapshot(None, list(raws)).preavisos


def _preavis(**overrides) -> dict:
    """One pre-warning as the feed shapes it."""
    return {
        "tipus": "Preavís",
        "estat": "Vigent",
        "perill": 3.0,
        "nivell": 1.0,
        "llindar": "Ratxa màxima > 72 km/h (20 m/s)",
        "comentari": "Ventada del nord a partir de dijous.",
        "dataEmisio": "2026-08-05T11:00Z",
        "dataInici": "2026-08-08T00:00Z",
        "dataFi": "2026-08-09T23:59Z",
    } | overrides


def test_pre_warnings_are_active_until_their_period_ends(clock: FakeClock) -> None:
    """A pre-warning is for the third day on: not yet started is still active."""
    actius = preavisos_actius(_preavisos(_preavis()), clock())
    assert [preavis.perill for preavis in actius] == [3]

    clock.now = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    assert preavisos_actius(_preavisos(_preavis()), clock()) == []


def test_pre_warnings_without_an_end_are_kept(clock: FakeClock) -> None:
    """Dropping an undated pre-warning would be the silent loss we avoid."""
    assert len(preavisos_actius(_preavisos(_preavis(dataFi=None)), clock())) == 1


def test_closed_pre_warnings_are_skipped(clock: FakeClock) -> None:
    """The same closure rule as the warnings themselves."""
    assert preavisos_actius(_preavisos(_preavis(estat="Finalitzat")), clock()) == []


def test_pre_warnings_are_ordered_by_severity(clock: FakeClock) -> None:
    """Most severe first, so the sensor reads `[0]`."""
    actius = preavisos_actius(
        _preavisos(
            _preavis(perill=2.0),
            _preavis(perill=5.0, nivell=2.0),
            _preavis(perill=5.0),
        ),
        clock(),
    )
    assert [(preavis.perill, preavis.nivell) for preavis in actius] == [
        (5, 2),
        (5, 1),
        (2, 1),
    ]


# ---------------------------------------------------------------------------
# The real captured payload
# ---------------------------------------------------------------------------

CAPTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "captures"
    / "smp-episodis-oberts-2026-08-05.json"
)


def _capture() -> tuple[Episodi, ...]:
    """The episodes of the payload captured live on 2026-08-05."""
    return parse_snapshot(json.loads(CAPTURE.read_text(encoding="utf-8"))).episodis


def test_the_real_capture_announces_tomorrow_afternoon(clock: FakeClock) -> None:
    """The captured payload is a real announced-not-in-force case.

    A 30-minute rain-intensity warning issued on the 4th at 15:30 UTC and
    running to the 6th at 17:59. Its remaining affectation for Osona is the
    `12-18` band of the 6th, so at 12:00 on the 5th the comarca has nothing in
    force and exactly one thing announced, 24 hours ahead.
    """
    episodis = _capture()

    assert afectacions_vigents(episodis, OSONA, clock()) == []
    anunciades = afectacions_anunciades(episodis, OSONA, clock())
    assert len(anunciades) == 1
    afectacio = anunciades[0]
    assert afectacio.meteor is Meteor.PLUJA_30MIN
    assert afectacio.periode == "12-18"
    assert afectacio.perill == 3
    assert afectacio.inici == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    assert afectacio.hores_per_endavant == 24
    assert afectacio.etiqueta_dia == "dema"

    graella = outlook(episodis, OSONA, clock())
    assert [dia.dia for dia in graella] == [AVUI, DEMA, DEMA_PASSAT]
    assert all(set(dia.graella) == set(PERIODES) for dia in graella)
    assert [dia.perill_maxim for dia in graella] == [0, 3, 0]


def test_the_real_capture_recomputes_as_the_clock_advances(clock: FakeClock) -> None:
    """Advancing the clock alone changes what is in force, with no new payload.

    The last leg of the real warning also shows the mid-band clipping on real
    data: the `12-18` band of the 6th ends at the warning's own 17:59, not at
    18:00.
    """
    episodis = _capture()
    assert afectacions_vigents(episodis, OSONA, clock()) == []

    clock.now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)  # the band opens
    vigents = afectacions_vigents(episodis, OSONA, clock())
    assert len(vigents) == 1
    assert vigents[0].fi == datetime(2026, 8, 6, 17, 59, tzinfo=UTC)
    assert afectacions_anunciades(episodis, OSONA, clock()) == []

    clock.now = datetime(2026, 8, 6, 17, 59, tzinfo=UTC)  # `dataFi` reached
    assert afectacions_vigents(episodis, OSONA, clock()) == []
    assert periode_actual(clock()) == "12-18"


def test_the_capture_keeps_the_band_already_gone_by(clock: FakeClock) -> None:
    """The 4th is inside the same warning and reads as past, not as missing.

    The forecast span of one emission never exceeds the documented three days
    (docs/01-data-sources.md §1.5), which is what makes a three-day grid enough.
    """
    projectades = projeccions(_capture(), OSONA, clock())
    assert [(af.dia, af.periode, af.horitzo) for af in projectades] == [
        (date(2026, 8, 4), "12-18", Horitzo.PASSAT),
        (date(2026, 8, 4), "18-00", Horitzo.PASSAT),
        (DEMA, "12-18", Horitzo.ANUNCIAT),
    ]
    dies = {af.dia for af in projectades}
    assert max(dies) - min(dies) <= timedelta(days=DIES_OUTLOOK - 1)
