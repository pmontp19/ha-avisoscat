"""Tests for the territorial reference and the point-in-polygon resolution.

The geometry cases run against `tests/fixtures/comarquesAmbMar.json`, a real
capture of the file the config flow downloads. No test touches the network:
`aioresponses` serves that fixture instead.
"""

import json
import logging
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aioresponses import aioresponses
from custom_components.avisoscat import comarques
from custom_components.avisoscat.comarques import (
    COMARQUES,
    TOPOJSON_OBJECT,
    ComarcaResolution,
    ResolutionError,
    async_resolve_comarca,
    comarca_at,
    comarques_terrestres,
    decode_topology,
    es_maritima,
    id_mar,
    nom,
)
from custom_components.avisoscat.const import COMARQUES_TOPOJSON_URL

FIXTURE = Path(__file__).parent / "fixtures" / "comarquesAmbMar.json"

# Real coordinates, chosen so each one lands in a comarca whose identity is
# not in doubt: two capitals, the two recent comarques, and two points that
# must resolve to nothing.
VIC = (41.9301, 2.2545)
BARCELONA = (41.3874, 2.1686)
MOIA = (41.8100, 2.0970)
PRATS_DE_LLUCANES = (42.0100, 2.0300)
FRAGA_ARAGON = (41.5210, 0.3490)
OFF_BARCELONA = (41.3500, 2.2500)
# On the Baix Llobregat shoreline, where the comarca (11) and the maritime zone
# in front of it (91) both contain the point.
CASTELLDEFELS_SHORE = (41.2800, 2.0800)


@pytest.fixture(autouse=True)
def _forget_unknown_ids() -> None:
    """`nom()` warns once per unseen id; that memory outlives a single test."""
    comarques._warned_unknown_ids.clear()


@pytest.fixture(name="topology")
def topology_fixture() -> dict:
    """The captured TopoJSON payload, parsed."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(name="geometries")
def geometries_fixture(topology: dict) -> dict:
    """The captured TopoJSON decoded into rings keyed by comarca id."""
    return decode_topology(topology)


# ---------------------------------------------------------------------------
# Static table
# ---------------------------------------------------------------------------


def test_table_has_every_zone() -> None:
    """43 land comarques plus 12 maritime zones, the ids the payload uses."""
    assert len(COMARQUES) == 55
    assert set(COMARQUES) == set(range(1, 44)) | set(range(88, 100))


def test_recent_comarques_are_present() -> None:
    """Moianès and Lluçanès were added late and are easy to miss."""
    assert nom(42) == "Moianès"
    assert nom(43) == "Lluçanès"


def test_names_keep_their_catalan_spelling() -> None:
    """Names are data: accents and lowercase articles are part of them."""
    assert nom(24) == "Osona"
    assert nom(13) == "Barcelonès"
    assert nom(39) == "Val d'Aran"
    assert nom(99) == "Mar Alt Empordà"


def test_unknown_id_degrades_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An id the table never saw must stay readable, never raise."""
    with caplog.at_level(logging.WARNING):
        assert nom(77) == "Comarca 77"
    assert "77" in caplog.text


def test_unknown_id_warns_once_but_degrades_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`nom()` runs on every coordinator refresh: one log line per id, not per call."""
    with caplog.at_level(logging.WARNING):
        assert [nom(77) for _ in range(5)] == ["Comarca 77"] * 5
        assert nom(78) == "Comarca 78"

    warned = [
        record.args
        for record in caplog.records
        if record.name == comarques.__name__ and record.levelno == logging.WARNING
    ]
    assert warned == [(77,), (78,)]


def test_coastal_comarques_expose_their_maritime_zone() -> None:
    """Only the 12 coastal comarques have a sea in front of them."""
    assert id_mar(13) == 95  # Barcelonès -> Mar Barcelonès
    assert id_mar(22) == 88  # Montsià -> Mar Montsià
    assert id_mar(24) is None  # Osona is inland
    assert id_mar(95) is None  # a maritime zone has no sea of its own
    assert id_mar(77) is None  # unknown id, no crash

    with_sea = [c for c in COMARQUES.values() if c.id_mar is not None]
    assert len(with_sea) == 12
    assert {c.id_mar for c in with_sea} == set(range(88, 100))


def test_maritime_zones_are_recognised() -> None:
    """Ids 88-99 are sea, everything else is land."""
    assert es_maritima(88)
    assert es_maritima(99)
    assert not es_maritima(43)
    assert not es_maritima(100)
    assert COMARQUES[95].es_maritima
    assert not COMARQUES[13].es_maritima


def test_land_comarques_are_listed_sorted_by_name() -> None:
    """What the manual dropdown offers when geometry is unavailable."""
    land = comarques_terrestres()
    assert len(land) == 43
    assert [c.nom for c in land] == sorted(c.nom for c in land)
    assert all(not c.es_maritima for c in land)


def test_table_matches_the_captured_topojson(topology: dict) -> None:
    """The embedded table must not drift from the source it was generated from."""
    geometries = topology["objects"][TOPOJSON_OBJECT]["geometries"]
    assert len(geometries) == 55
    for geometry in geometries:
        properties = geometry["properties"]
        assert nom(properties["IDComarca"]) == properties["NOM_COMAR"]


# ---------------------------------------------------------------------------
# TopoJSON decoding and point in polygon
# ---------------------------------------------------------------------------


def test_decoding_yields_one_entry_per_zone(geometries: dict) -> None:
    """Every geometry decodes into at least one closed ring."""
    assert len(geometries) == 55
    for polygons in geometries.values():
        assert polygons
        for polygon in polygons:
            assert polygon[0][0] == polygon[0][-1]


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (VIC, 24),  # Osona
        (BARCELONA, 13),  # Barcelonès
        (MOIA, 42),  # Moianès
        (PRATS_DE_LLUCANES, 43),  # Lluçanès
        (FRAGA_ARAGON, None),  # outside Catalonia
        (OFF_BARCELONA, None),  # at sea, and the sea is opt-in
    ],
)
def test_point_in_polygon_on_real_coordinates(
    geometries: dict,
    point: tuple[float, float],
    expected: int | None,
) -> None:
    """Real coordinates resolve to the comarca that actually contains them."""
    assert comarca_at(geometries, *point) == expected


def test_sea_is_resolved_only_when_asked_for(geometries: dict) -> None:
    """A coastal point belongs to its comarca, not to the sea in front of it."""
    assert comarca_at(geometries, *OFF_BARCELONA, include_sea=True) == 95
    assert comarca_at(geometries, *BARCELONA, include_sea=True) == 13


def test_land_wins_where_a_maritime_zone_overlaps_the_coast(geometries: dict) -> None:
    """The shoreline is inside both zones; land must win regardless of payload order."""
    # The point really is inside Mar Baix Llobregat as well, so only explicit
    # land-first precedence keeps it in Baix Llobregat.
    sea_only = {91: geometries[91]}
    assert comarca_at(sea_only, *CASTELLDEFELS_SHORE, include_sea=True) == 91

    assert comarca_at(geometries, *CASTELLDEFELS_SHORE) == 11
    assert comarca_at(geometries, *CASTELLDEFELS_SHORE, include_sea=True) == 11

    reversed_order = dict(reversed(list(geometries.items())))
    assert comarca_at(reversed_order, *CASTELLDEFELS_SHORE, include_sea=True) == 11


def test_holes_read_as_outside() -> None:
    """Even-odd counting makes an enclave exclude itself from its container."""
    # One square with a square hole, stored untransformed: outer ring as arc 0,
    # hole as arc 1 referenced backwards.
    topology = {
        "arcs": [
            [[0, 0], [10, 0], [0, 10], [-10, 0], [0, -10]],
            [[4, 4], [2, 0], [0, 2], [-2, 0], [0, -2]],
        ],
        "objects": {
            TOPOJSON_OBJECT: {
                "geometries": [
                    {
                        "type": "Polygon",
                        "properties": {"IDComarca": 1},
                        "arcs": [[0], [~1]],
                    }
                ]
            }
        },
    }
    geometries = decode_topology(topology)
    assert comarca_at(geometries, 1.0, 1.0) == 1  # inside, outside the hole
    assert comarca_at(geometries, 5.0, 5.0) is None  # inside the hole


def test_multipolygon_parts_all_count() -> None:
    """A zone made of separate islands is inside on any of them."""
    topology = {
        "arcs": [
            [[0, 0], [1, 0], [0, 1], [-1, 0], [0, -1]],
            [[10, 10], [1, 0], [0, 1], [-1, 0], [0, -1]],
        ],
        "objects": {
            TOPOJSON_OBJECT: {
                "geometries": [
                    {
                        "type": "MultiPolygon",
                        "properties": {"IDComarca": 7},
                        "arcs": [[[0]], [[1]]],
                    }
                ]
            }
        },
    }
    geometries = decode_topology(topology)
    assert comarca_at(geometries, 0.5, 0.5) == 7
    assert comarca_at(geometries, 10.5, 10.5) == 7
    assert comarca_at(geometries, 5.0, 5.0) is None


def test_decoding_skips_unusable_geometries() -> None:
    """Missing ids, unknown types and absent members are read, not assumed."""
    topology = {
        "arcs": [[[0, 0], [1, 0], [0, 1], [-1, 0], [0, -1]]],
        "objects": {
            TOPOJSON_OBJECT: {
                "geometries": [
                    {"type": "Polygon", "properties": {}, "arcs": [[0]]},
                    {"type": "Point", "properties": {"IDComarca": 2}},
                    {"properties": {"IDComarca": 3}},
                    {"type": "Polygon", "properties": {"IDComarca": 4}, "arcs": [[0]]},
                ]
            }
        },
    }
    assert set(decode_topology(topology)) == {4}
    assert decode_topology({}) == {}
    assert decode_topology({"objects": {}}) == {}


@pytest.mark.parametrize(
    "geometry",
    [
        # `arcs` renamed or dropped upstream.
        {"type": "Polygon", "properties": {"IDComarca": 1}},
        # Present but stitching to nothing.
        {"type": "Polygon", "properties": {"IDComarca": 1}, "arcs": []},
        {"type": "Polygon", "properties": {"IDComarca": 1}, "arcs": [[]]},
        {"type": "MultiPolygon", "properties": {"IDComarca": 1}, "arcs": [[]]},
        {"type": "MultiPolygon", "properties": {"IDComarca": 1}, "arcs": [[[]]]},
    ],
)
def test_geometries_without_usable_rings_are_dropped(geometry: dict) -> None:
    """An id with no point in it is not geometry, and must not look like some."""
    topology = {
        "arcs": [],
        "objects": {TOPOJSON_OBJECT: {"geometries": [geometry]}},
    }

    assert decode_topology(topology) == {}


# ---------------------------------------------------------------------------
# Resolution: failure is a value, never an exception
# ---------------------------------------------------------------------------


async def _resolve(
    latitude: float, longitude: float, **response: object
) -> ComarcaResolution:
    """Resolve a point against a mocked geometry response, no real network."""
    with aioresponses() as mocked:
        mocked.get(COMARQUES_TOPOJSON_URL, **response)
        async with ClientSession() as session:
            return await async_resolve_comarca(session, latitude, longitude)


async def test_resolution_downloads_the_geometry_once() -> None:
    """The happy path: one request, one comarca."""
    with aioresponses() as mocked:
        mocked.get(COMARQUES_TOPOJSON_URL, body=FIXTURE.read_bytes())
        async with ClientSession() as session:
            result = await async_resolve_comarca(session, *VIC)
        requests = sum(len(calls) for calls in mocked.requests.values())

    assert requests == 1
    assert result == ComarcaResolution(id_comarca=24)
    assert result.ok
    assert nom(result.id_comarca) == "Osona"


async def test_point_outside_catalonia_is_reported_not_raised() -> None:
    """Outside the geometry is a normal answer the flow can act on."""
    result = await _resolve(*FRAGA_ARAGON, body=FIXTURE.read_bytes())

    assert result.id_comarca is None
    assert not result.ok
    assert result.error is ResolutionError.LOCATION_OUTSIDE_CATALONIA
    # The value doubles as the config-flow error key documented in
    # docs/03-feature-spec.md §2.
    assert result.error == "location_outside_catalonia"


async def test_download_failure_falls_back_instead_of_raising() -> None:
    """A dead source must never block the config flow."""
    result = await _resolve(*VIC, status=500)

    assert result == ComarcaResolution(error=ResolutionError.CANNOT_CONNECT)


async def test_timeout_is_reported_as_a_value() -> None:
    """Same for a request that never comes back."""
    result = await _resolve(*VIC, exception=TimeoutError())

    assert result.error is ResolutionError.CANNOT_CONNECT


async def test_unparseable_body_is_reported_as_a_value() -> None:
    """The endpoint is not an official API: it may answer with anything."""
    result = await _resolve(*VIC, body="<html>maintenance</html>")

    assert result.error is ResolutionError.CANNOT_CONNECT


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "topology"],
        {"objects": {}},
        {"objects": {TOPOJSON_OBJECT: {"geometries": []}}},
        # Arc indices that point nowhere.
        {
            "arcs": [],
            "objects": {
                TOPOJSON_OBJECT: {
                    "geometries": [
                        {
                            "type": "Polygon",
                            "properties": {"IDComarca": 1},
                            "arcs": [[9]],
                        }
                    ]
                }
            },
        },
        # Well-formed ids carrying no ring at all: unusable geometry, and the
        # flow must say so rather than claim the location is outside Catalonia.
        {
            "arcs": [],
            "objects": {
                TOPOJSON_OBJECT: {
                    "geometries": [
                        {"type": "Polygon", "properties": {"IDComarca": 1}},
                        {
                            "type": "MultiPolygon",
                            "properties": {"IDComarca": 2},
                            "arcs": [[]],
                        },
                    ]
                }
            },
        },
    ],
)
async def test_unusable_payloads_are_reported_as_a_value(payload: object) -> None:
    """A payload that parses but yields no usable geometry is still a fallback."""
    result = await _resolve(*VIC, payload=payload)

    assert result.error is ResolutionError.INVALID_GEOMETRY
