"""Territorial reference for the Avisos Meteocat integration.

Two independent halves (docs/04-architecture.md §6):

* A **static table** of the 55 warning zones the SMP payload can name: the 43
  land comarques (ids 1-43) and the 12 maritime zones (ids 88-99), generated
  once from `comarquesAmbMar.json` and embedded here. It costs no request and
  no dependency, and it is the only half the runtime ever touches.
* **Geometry**, needed only to turn a map location into a comarca while the
  config flow is running. The 58 KB TopoJSON is downloaded at that moment,
  decoded and thrown away; it is deliberately not vendored into the repository
  and never fetched by the coordinator.

The decoding and the point-in-polygon test are written out here (arc delta
arithmetic plus ray casting, a few dozen lines) because `manifest.json`
declares no requirements and keeps it that way: a geometry library is not an
option.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import COMARQUES_TOPOJSON_URL

_LOGGER = logging.getLogger(__name__)

# Name of the `objects` member holding the 55 geometries, and of the property
# carrying the same `idComarca` the warnings payload uses
# (docs/01-data-sources.md §4.1).
TOPOJSON_OBJECT: Final = "comarquesAmbMarCorrectes84"
TOPOJSON_ID_PROPERTY: Final = "IDComarca"

# Maritime zones are ids 88-99; the official site treats them as a special
# case and so does the `include_sea` option.
FIRST_MARITIME_ID: Final = 88
LAST_MARITIME_ID: Final = 99

DOWNLOAD_TIMEOUT_SECONDS: Final = 30


@dataclass(frozen=True, slots=True)
class Comarca:
    """One warning zone: a land comarca or a maritime zone."""

    id_comarca: int
    nom: str
    # For a coastal comarca, the id of the maritime zone in front of it, so a
    # config entry can offer to include sea warnings. `None` inland and on the
    # maritime zones themselves.
    id_mar: int | None = None

    @property
    def es_maritima(self) -> bool:
        """Whether this zone is a stretch of sea rather than land."""
        return es_maritima(self.id_comarca)


# Generated from docs/captures/comarques-idcomarca-2026-08-05.json. Catalan
# spelling is data and is reproduced exactly, accents included.
COMARQUES: Final[Mapping[int, Comarca]] = {
    comarca.id_comarca: comarca
    for comarca in (
        Comarca(1, "Alt Camp"),
        Comarca(2, "Alt Empordà", id_mar=99),
        Comarca(3, "Alt Penedès"),
        Comarca(4, "Alt Urgell"),
        Comarca(5, "Alta Ribagorça"),
        Comarca(6, "Anoia"),
        Comarca(7, "Bages"),
        Comarca(8, "Baix Camp", id_mar=90),
        Comarca(9, "Baix Ebre", id_mar=89),
        Comarca(10, "Baix Empordà", id_mar=98),
        Comarca(11, "Baix Llobregat", id_mar=91),
        Comarca(12, "Baix Penedès", id_mar=92),
        Comarca(13, "Barcelonès", id_mar=95),
        Comarca(14, "Berguedà"),
        Comarca(15, "Cerdanya"),
        Comarca(16, "Conca de Barberà"),
        Comarca(17, "Garraf", id_mar=93),
        Comarca(18, "Garrigues"),
        Comarca(19, "Garrotxa"),
        Comarca(20, "Gironès"),
        Comarca(21, "Maresme", id_mar=96),
        Comarca(22, "Montsià", id_mar=88),
        Comarca(23, "Noguera"),
        Comarca(24, "Osona"),
        Comarca(25, "Pallars Jussà"),
        Comarca(26, "Pallars Sobirà"),
        Comarca(27, "Pla d'Urgell"),
        Comarca(28, "Pla de l'Estany"),
        Comarca(29, "Priorat"),
        Comarca(30, "Ribera d'Ebre"),
        Comarca(31, "Ripollès"),
        Comarca(32, "Segarra"),
        Comarca(33, "Segrià"),
        Comarca(34, "Selva", id_mar=97),
        Comarca(35, "Solsonès"),
        Comarca(36, "Tarragonès", id_mar=94),
        Comarca(37, "Terra Alta"),
        Comarca(38, "Urgell"),
        Comarca(39, "Val d'Aran"),
        Comarca(40, "Vallès Occidental"),
        Comarca(41, "Vallès Oriental"),
        # Moianès and Lluçanès are recent additions; more can appear, which is
        # why every lookup below degrades instead of raising.
        Comarca(42, "Moianès"),
        Comarca(43, "Lluçanès"),
        Comarca(88, "Mar Montsià"),
        Comarca(89, "Mar Baix Ebre"),
        Comarca(90, "Mar Baix Camp"),
        Comarca(91, "Mar Baix Llobregat"),
        Comarca(92, "Mar Baix Penedès"),
        Comarca(93, "Mar Garraf"),
        Comarca(94, "Mar Tarragonès"),
        Comarca(95, "Mar Barcelonès"),
        Comarca(96, "Mar Maresme"),
        Comarca(97, "Mar Selva"),
        Comarca(98, "Mar Baix Empordà"),
        Comarca(99, "Mar Alt Empordà"),
    )
}


# Ids already reported as unknown. `nom()` is the name source for entities and
# is recomputed on every coordinator refresh, so the warning is worth one line
# per id, not one line per refresh forever.
_warned_unknown_ids: set[int] = set()


def nom(id_comarca: int) -> str:
    """Return the name of a zone, or a readable placeholder for unknown ids.

    The comarca list is not frozen (Moianès and Lluçanès were added in 2015 and
    2023), so an id the table has never seen must degrade into something a user
    can still read in an entity name, never a `KeyError`.
    """
    comarca = COMARQUES.get(id_comarca)
    if comarca is None:
        if id_comarca not in _warned_unknown_ids:
            _warned_unknown_ids.add(id_comarca)
            _LOGGER.warning(
                "Unknown comarca id %s; the territorial table may be outdated",
                id_comarca,
            )
        return f"Comarca {id_comarca}"
    return comarca.nom


def id_mar(id_comarca: int) -> int | None:
    """Return the maritime zone adjacent to a comarca, `None` if inland."""
    comarca = COMARQUES.get(id_comarca)
    return comarca.id_mar if comarca is not None else None


def es_maritima(id_comarca: int) -> bool:
    """Whether an id names a maritime zone rather than a land comarca."""
    return FIRST_MARITIME_ID <= id_comarca <= LAST_MARITIME_ID


def comarques_terrestres() -> list[Comarca]:
    """The 43 land comarques, sorted by name, for the manual dropdown."""
    return sorted(
        (c for c in COMARQUES.values() if not c.es_maritima),
        key=lambda c: c.nom,
    )


class ResolutionError(StrEnum):
    """Why a location could not be turned into a comarca.

    These double as config-flow error keys: failure is an ordinary return
    value, so the flow can fall back to the manual dropdown instead of having
    to catch anything.
    """

    CANNOT_CONNECT = "cannot_connect"
    INVALID_GEOMETRY = "invalid_geometry"
    LOCATION_OUTSIDE_CATALONIA = "location_outside_catalonia"


@dataclass(frozen=True, slots=True)
class ComarcaResolution:
    """Outcome of resolving a coordinate: an id, or a reason there is none."""

    id_comarca: int | None = None
    error: ResolutionError | None = None

    @property
    def ok(self) -> bool:
        """Whether a comarca was found."""
        return self.id_comarca is not None


# A ring is a closed sequence of (longitude, latitude) points; a polygon is an
# outer ring followed by its holes; a zone can be several polygons.
Ring = list[tuple[float, float]]
Polygon = list[Ring]
Geometries = dict[int, list[Polygon]]


def _decode_arcs(topology: Mapping[str, Any]) -> list[Ring]:
    """Turn quantized TopoJSON arcs into absolute (lon, lat) rings.

    Positions are stored as deltas from the previous point of the same arc and
    then scaled by `transform`. An untransformed topology (no `transform`) is
    already absolute, which the identity scale below covers.
    """
    transform = topology.get("transform") or {}
    scale_x, scale_y = transform.get("scale", (1.0, 1.0))
    translate_x, translate_y = transform.get("translate", (0.0, 0.0))

    decoded: list[Ring] = []
    for arc in topology.get("arcs", []):
        points: Ring = []
        x = y = 0
        for position in arc:
            x += position[0]
            y += position[1]
            points.append((x * scale_x + translate_x, y * scale_y + translate_y))
        decoded.append(points)
    return decoded


def _stitch(ring_arcs: Sequence[int], arcs: Sequence[Ring]) -> Ring:
    """Join the arc indices of one ring into a single point sequence.

    A negative index `i` means arc `~i` traversed backwards; the shared
    endpoint between consecutive arcs is dropped so it is not duplicated.
    """
    ring: Ring = []
    for index in ring_arcs:
        arc = arcs[~index][::-1] if index < 0 else arcs[index]
        ring.extend(arc[1:] if ring else arc)
    return ring


def _iter_polygons(geometry: Mapping[str, Any]) -> Iterator[Sequence[Sequence[int]]]:
    """Yield the raw arc-index rings of each polygon of a geometry."""
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        yield geometry.get("arcs", [])
    elif geometry_type == "MultiPolygon":
        yield from geometry.get("arcs", [])


def _decode_polygon(polygon: Sequence[Sequence[int]], arcs: Sequence[Ring]) -> Polygon:
    """Stitch one polygon, dropping the rings that carry no point.

    A geometry whose `arcs` member is missing or empty stitches to nothing. Such
    a polygon can never contain a coordinate, so it must not survive decoding:
    an entry made only of empty rings would look like usable geometry and turn
    every location into "outside Catalonia".
    """
    return [
        ring for ring in (_stitch(ring_arcs, arcs) for ring_arcs in polygon) if ring
    ]


def decode_topology(payload: Mapping[str, Any]) -> Geometries:
    """Decode `comarquesAmbMar.json` into rings keyed by comarca id.

    Reads every field with `.get()` and a default: the file is served from the
    public site, is not a supported API, and can change shape without notice.
    """
    arcs = _decode_arcs(payload)
    collection = (payload.get("objects") or {}).get(TOPOJSON_OBJECT) or {}

    geometries: Geometries = {}
    for geometry in collection.get("geometries", []):
        properties = geometry.get("properties") or {}
        raw_id = properties.get(TOPOJSON_ID_PROPERTY)
        if not isinstance(raw_id, int):
            continue
        polygons = [
            rings
            for rings in (
                _decode_polygon(polygon, arcs) for polygon in _iter_polygons(geometry)
            )
            if rings
        ]
        if polygons:
            geometries.setdefault(raw_id, []).extend(polygons)
    return geometries


def _point_in_polygon(longitude: float, latitude: float, polygon: Polygon) -> bool:
    """Ray casting with the even-odd rule, so holes exclude themselves.

    Crossings are counted against every ring of the polygon at once: a point
    inside a hole crosses the outer ring and the hole, an even number of times,
    and therefore reads as outside.
    """
    inside = False
    for ring in polygon:
        for index in range(len(ring)):
            x1, y1 = ring[index - 1]
            x2, y2 = ring[index]
            if (y1 > latitude) == (y2 > latitude):
                continue
            crossing_x = x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
            if longitude < crossing_x:
                inside = not inside
    return inside


def _first_zone_containing(
    geometries: Geometries,
    latitude: float,
    longitude: float,
    *,
    maritime: bool,
) -> int | None:
    """First zone of the requested kind whose polygons contain the coordinate."""
    for id_comarca, polygons in geometries.items():
        if es_maritima(id_comarca) is not maritime:
            continue
        for polygon in polygons:
            if _point_in_polygon(longitude, latitude, polygon):
                return id_comarca
    return None


def comarca_at(
    geometries: Geometries,
    latitude: float,
    longitude: float,
    *,
    include_sea: bool = False,
) -> int | None:
    """Return the id of the zone containing a coordinate, `None` if outside.

    The maritime zones reach over the coastline of the comarques they face, so
    land is scanned first and the sea only as a fallback: a coastal point must
    resolve to its comarca, not to the sea in front of it, whatever order the
    payload happens to list the zones in.
    """
    on_land = _first_zone_containing(geometries, latitude, longitude, maritime=False)
    if on_land is not None or not include_sea:
        return on_land
    return _first_zone_containing(geometries, latitude, longitude, maritime=True)


async def async_resolve_comarca(
    session: ClientSession,
    latitude: float,
    longitude: float,
    *,
    url: str = COMARQUES_TOPOJSON_URL,
) -> ComarcaResolution:
    """Resolve a location into a comarca id, downloading the geometry once.

    Never raises: the caller is a config flow that must always be able to fall
    back to the manual comarca dropdown, so every failure comes back as a
    `ResolutionError` value.
    """
    timeout = ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with session.get(url, timeout=timeout) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Could not download the comarques geometry: %s", err)
        return ComarcaResolution(error=ResolutionError.CANNOT_CONNECT)

    if not isinstance(payload, Mapping):
        _LOGGER.debug("Unexpected comarques geometry payload: %s", type(payload))
        return ComarcaResolution(error=ResolutionError.INVALID_GEOMETRY)

    try:
        geometries = decode_topology(payload)
    except (TypeError, ValueError, IndexError, KeyError) as err:
        _LOGGER.debug("Could not decode the comarques geometry: %s", err)
        return ComarcaResolution(error=ResolutionError.INVALID_GEOMETRY)

    if not geometries:
        return ComarcaResolution(error=ResolutionError.INVALID_GEOMETRY)

    id_comarca = comarca_at(geometries, latitude, longitude)
    if id_comarca is None:
        return ComarcaResolution(error=ResolutionError.LOCATION_OUTSIDE_CATALONIA)
    return ComarcaResolution(id_comarca=id_comarca)
