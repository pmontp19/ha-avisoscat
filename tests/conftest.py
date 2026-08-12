"""Shared pytest fixtures for avisoscat tests."""

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from custom_components.avisoscat.const import (
    CONF_ID_COMARCA,
    CONF_SEVERE_THRESHOLD,
    DEFAULT_SEVERE_THRESHOLD,
    DOMAIN,
)
from custom_components.avisoscat.coordinator import AvisoscatDataUpdateCoordinator
from custom_components.avisoscat.models import (
    SmpSnapshot,
    compute_payload_hash,
    parse_snapshot,
)
from custom_components.avisoscat.vigencia import PERIODES
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"


def run_in_isolated_interpreter(script: str, *args: str) -> dict:
    """Run `script` in a fresh interpreter and return the JSON report it prints.

    The pure layers (`models.py`, `vigencia.py`) must work in an interpreter that
    never imports Home Assistant (docs/04-architecture.md §4 and §5). A child
    interpreter proves it behaviourally without touching this process's
    `sys.modules`, and cannot be fooled by an already imported `homeassistant`:
    the child is asked what it actually ended up loading, and its report is
    computed by really running the module.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        pytest.fail(f"Could not run a child interpreter: {err}")

    assert result.returncode == 0, f"the module failed in isolation:\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for every test automatically.

    Required by pytest-homeassistant-custom-component so that Home Assistant
    picks up custom_components/avisoscat during tests.
    """
    return enable_custom_integrations


# Osona, the comarca used as the default subject across tests.
ID_COMARCA_OSONA = 24


def make_config_entry(
    *,
    id_comarca: int = ID_COMARCA_OSONA,
    options: dict | None = None,
) -> MockConfigEntry:
    """Build a `MockConfigEntry` for the avisoscat domain with sane defaults."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(id_comarca),
        data={CONF_ID_COMARCA: id_comarca},
        options=options
        if options is not None
        else {CONF_SEVERE_THRESHOLD: DEFAULT_SEVERE_THRESHOLD},
    )


class FakeClock:
    """A controllable stand-in for `homeassistant.util.dt.utcnow`.

    Every warning in this integration is scoped to a 6-hour UTC band, so
    "is this warning in force?" is a pure function of the wall clock. Tests
    advance this clock explicitly instead of sleeping for real hours or
    fighting `freezegun` across many `async_refresh()` calls
    (docs/04-architecture.md §12).

    Patch it over the `utcnow` reference of the module under test, e.g.:

        monkeypatch.setattr("custom_components.avisoscat.coordinator.utcnow", clock)
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    """A `FakeClock` starting mid-band, at 12:00 UTC of a fixed day.

    12:00 UTC is the "12-18" band boundary, the most interesting instant for
    validity tests: one tick either way changes which band applies.
    """
    return FakeClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Raw payload builders
#
# The subjects of the coordinator and event tests are built as raw feed payloads
# and put through `parse_snapshot()`, not as hand-made dataclasses: what the
# coordinator has to survive is the shape the source really sends, floats and
# `null`s included. These mirror the helpers of `tests/test_vigencia.py` but
# stay generic (a raw dict out, not a parsed tuple) so a test can compose many
# episodes into one snapshot.
# ---------------------------------------------------------------------------


def afectacio_raw(
    *,
    id_comarca: int = ID_COMARCA_OSONA,
    perill: float | str = 3.0,
    nivell: float = 1.0,
    dia: str | None = "2026-08-05T00:00Z",
    llindar: str = "Ratxa màxima > 108 km/h (30 m/s)",
    auxiliar: bool = False,
) -> dict:
    """One affectation, exactly as the feed shapes it (trap #2: floats)."""
    return {
        "dia": dia,
        "llindar": llindar,
        "auxiliar": auxiliar,
        "perill": perill,
        "idComarca": float(id_comarca),
        "nivell": nivell,
    }


def evolucio_raw(
    periodes: dict[str, list[dict] | None],
    *,
    dia: str | None = "2026-08-05T00:00Z",
    comentari: str = "Ratxes molt fortes al litoral.",
    llindar1: str | None = "Ratxa màxima > 72 km/h (20 m/s)",
    llindar2: str | None = "Ratxa màxima > 108 km/h (30 m/s)",
    distribucio_geografica: str | None = "EXTENSA",
) -> dict:
    """One forecast day, always sending all four bands (`null` when empty)."""
    noms = list(PERIODES) + [nom for nom in periodes if nom not in PERIODES]
    return {
        "dia": dia,
        "comentari": comentari,
        "representatiu": 1.0,
        "llindar1": llindar1,
        "llindar2": llindar2,
        "distribucioGeografica": distribucio_geografica,
        "periodes": [{"nom": nom, "afectacions": periodes.get(nom)} for nom in noms],
    }


def episodi_raw(
    evolucions: list[dict] | None = None,
    *,
    meteor: str = "Vent",
    tipus: str = "Avís",
    estat: str = "Vigent",
    estat_episodi: str = "Obert",
    data_emissio: str | None = "2026-08-04T15:30Z",
    data_inici: str | None = "2026-08-04T12:00Z",
    data_fi: str | None = "2026-08-06T23:59Z",
    afectacions_directes: list[dict] | None = None,
    perill_declarat: float = 0.0,
) -> dict:
    """One raw episode object, ready to feed `make_snapshot`.

    `afectacions_directes` plus `perill_declarat` cover the "temps violent"
    vigilance shape (trap #12), whose affectations hang directly off the avis
    with no `evolucions`/`periodes` wrapper; the ordinary shape leaves them off.
    """
    avis: dict = {
        "tipus": tipus,
        "estat": estat,
        "dataEmisio": data_emissio,
        "dataInici": data_inici,
        "dataFi": data_fi,
        "evolucions": evolucions or [],
    }
    if afectacions_directes is not None:
        avis["afectacions"] = afectacions_directes
        avis["perill"] = perill_declarat
    return {
        "id": None,
        "estat": {"nom": estat_episodi, "data": None},
        "meteor": {"idMeteor": None, "nom": meteor},
        "avisos": [avis],
    }


def make_snapshot(
    episodis: list[dict] | None = None,
    *,
    preavisos: list[dict] | None = None,
    fetched_at: datetime | None = None,
) -> SmpSnapshot:
    """Parse a list of raw episodes into an `SmpSnapshot`, hashing the payload.

    Wraps the episodes in the extra list level the captured payload nests them
    in (`[[...]]`, which `parse_snapshot` flattens) and computes the order-
    insensitive hash the coordinator would attach, so a built snapshot is
    indistinguishable from a fetched one.
    """
    raw_episodis = [episodis or []]
    return parse_snapshot(
        raw_episodis,
        preavisos,
        fetched_at=fetched_at,
        payload_hash=compute_payload_hash(raw_episodis, preavisos),
    )


# ---------------------------------------------------------------------------
# Coordinator test doubles
# ---------------------------------------------------------------------------


class FakeSource:
    """A scriptable `SmpSource` double for coordinator and event tests.

    Queued snapshots come back one per fetch, popping the front; once the queue
    holds a single snapshot it repeats, so a coordinator exercised across several
    cycles keeps answering after the scripted transitions. `error` is raised on
    every fetch instead, to inject a failure. `calls` counts fetches, which is
    how the network-free minute-recompute test proves a clock tick never fetched.
    """

    def __init__(
        self,
        snapshots: list[SmpSnapshot] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._snapshots = list(snapshots) if snapshots else [SmpSnapshot()]
        self._error = error
        self.calls = 0

    async def fetch(self) -> SmpSnapshot:
        """Return the next scripted snapshot, or raise the scripted error."""
        self.calls += 1
        if self._error is not None:
            raise self._error
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


@pytest.fixture
def quiet_source(monkeypatch: pytest.MonkeyPatch) -> FakeSource:
    """Patch `build_source` to a no-network fake, so setup's first refresh is quiet.

    `async_setup_entry` arms the coordinator with a first refresh, which for a
    real source would hit the network. Coordinator and event tests inject their
    own `FakeSource` directly; only the setup smoke tests need this patch, so it
    is opt-in (take it as a parameter) rather than autouse.
    """
    fake = FakeSource([SmpSnapshot()])
    monkeypatch.setattr(
        "custom_components.avisoscat.coordinator.build_source",
        lambda hass, entry: fake,
    )
    return fake


@pytest.fixture
def make_coordinator(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[AvisoscatDataUpdateCoordinator, FakeSource]]:
    """Factory: a coordinator wired to a `FakeSource` and a clock under test.

    Returns ``(coordinator, source)``. The caller advances the `clock` and queues
    snapshots on `source` before each `await coordinator.async_refresh()`. The
    clock is patched onto the coordinator module so every projection reads it.
    Constructed directly rather than through setup, so the entry state machine is
    not involved and every fetch comes from the fake.
    """

    def _factory(
        clock: FakeClock,
        snapshots: list[SmpSnapshot] | None = None,
        *,
        error: Exception | None = None,
        options: dict | None = None,
    ) -> tuple[AvisoscatDataUpdateCoordinator, FakeSource]:
        monkeypatch.setattr("custom_components.avisoscat.coordinator.utcnow", clock)
        source = FakeSource(snapshots, error=error)
        entry = make_config_entry(options=options)
        coord = AvisoscatDataUpdateCoordinator(hass, entry, source)
        return coord, source

    return _factory
