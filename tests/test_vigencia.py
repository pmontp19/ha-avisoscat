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

from .conftest import ID_COMARCA_OSONA, FakeClock

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
        "comentari": "Ratxes molt fortes al litoral.",
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


def test_no_home_assistant_import() -> None:
    """Validity logic must be testable without a Home Assistant runtime."""
    assert "homeassistant" not in Path(vigencia.__file__).read_text(encoding="utf-8")


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
    periodes: dict[str, list[dict] | None] | None = None,
) -> tuple[Episodi, ...]:
    """A nowcast issued at 11:30 UTC, listed under the band that contains it."""
    return _episodis(
        [_evolucio(periodes if periodes is not None else {"06-12": [_afectacio()]})],
        meteor="Temps violent",
        tipus=TEMPS_VIOLENT,
        data_emissio=data_emissio,
        data_inici=data_emissio,
        data_fi="2026-08-05T23:59Z",
    )


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


def test_violent_weather_without_an_issue_time_is_ignored(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """No issue time means no window to compute, and it is said out loud."""
    episodis = _temps_violent(data_emissio=None)
    with caplog.at_level(logging.WARNING):
        assert projeccions(episodis, OSONA, clock()) == []
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


def test_an_unknown_but_parseable_band_keeps_its_own_name(clock: FakeClock) -> None:
    """A band the SMC invents later still places its affectations."""
    episodis = _episodis([_evolucio({"09-12": [_afectacio()]})])
    clock.now = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    assert [af.periode for af in afectacions_vigents(episodis, OSONA, clock())] == [
        "09-12"
    ]


def test_an_unusable_band_name_is_ignored_with_a_warning(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A band that cannot be placed in time is dropped loudly, never guessed."""
    episodis = _episodis([_evolucio({"vespre": [_afectacio()]})])
    with caplog.at_level(logging.WARNING):
        assert projeccions(episodis, OSONA, clock()) == []
    assert "Unusable SMP time band" in caplog.text
    assert [rec.name for rec in caplog.records] == [vigencia.__name__]


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


def test_an_undatable_affectation_is_ignored_with_a_warning(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """With no day anywhere there is no interval to compute, and we say so."""
    episodis = _episodis(
        [_evolucio({"12-18": [_afectacio(dia=None)]}, dia=None)],
        data_inici=None,
    )
    with caplog.at_level(logging.WARNING):
        assert projeccions(episodis, OSONA, clock()) == []
    assert "Undatable SMP affectation" in caplog.text


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
