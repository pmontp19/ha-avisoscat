"""Shared pytest fixtures for avisoscat tests."""

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.avisoscat.const import (
    CONF_ID_COMARCA,
    CONF_SEVERE_THRESHOLD,
    DEFAULT_SEVERE_THRESHOLD,
    DOMAIN,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"


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
