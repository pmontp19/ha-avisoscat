"""Smoke tests for the avisoscat scaffold: it loads, unloads, and its
constants match the documented endpoints and defaults.
"""

from datetime import UTC, datetime

from custom_components.avisoscat import PLATFORMS, const
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .conftest import FakeClock, make_config_entry


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """A config entry loads and unloads cleanly."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_two_comarques_load_at_the_same_time(hass: HomeAssistant) -> None:
    """Multi-entry by design: one config entry per comarca, both loaded.

    `single_config_entry` is enforced by the config flow, not by setup, so
    `test_manifest_as_home_assistant_loads_it` guards the manifest side.
    """
    osona = make_config_entry(id_comarca=24)
    bages = make_config_entry(id_comarca=7)
    osona.add_to_hass(hass)
    bages.add_to_hass(hass)

    assert await hass.config_entries.async_setup(osona.entry_id)
    await hass.async_block_till_done()

    assert osona.state is ConfigEntryState.LOADED
    assert bages.state is ConfigEntryState.LOADED
    assert len(hass.config_entries.async_entries(const.DOMAIN)) == 2


async def test_manifest_as_home_assistant_loads_it(hass: HomeAssistant) -> None:
    """Home Assistant's own loader reports the documented manifest contract."""
    integration = await async_get_integration(hass, const.DOMAIN)

    assert integration.domain == const.DOMAIN
    assert integration.integration_type == "service"
    assert integration.iot_class == "cloud_polling"
    assert integration.config_flow is True
    assert integration.requirements == []
    assert integration.single_config_entry is False
    # `integration.quality_scale` always reports "custom" for a custom
    # integration, so the declared scale is read from the parsed manifest.
    assert integration.manifest["quality_scale"] == "silver"


def test_no_platforms_yet() -> None:
    """The scaffold forwards to no platform: sensor/binary_sensor come later."""
    assert PLATFORMS == ()


def test_documented_endpoints_are_defined() -> None:
    """Every endpoint of docs/01-data-sources.md §7 exists as a constant."""
    assert const.SMP_PAGE_URL == "https://www.meteo.cat/observacions/radar"
    assert const.SMP_PAGE_FALLBACK_URL == "https://www.meteo.cat/"
    assert const.SMP_API_EPISODIS_OBERTS_URL.format(data="2026-08-05") == (
        "https://api.meteo.cat/pronostic/v2/smp/episodis-oberts?data=2026-08-05Z"
    )
    assert const.SMP_API_PREAVISOS_URL == (
        "https://api.meteo.cat/pronostic/v1/smp/episodis-oberts/preavisos"
    )
    assert const.SMP_API_QUOTA_URL == "https://api.meteo.cat/quotes/v1/consum-actual"
    assert const.COMARQUES_TOPOJSON_URL == (
        "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json"
    )


def test_polling_defaults_respect_the_cache_floor() -> None:
    """Adaptive polling stays within the source's `max-age=600` floor."""
    assert const.DEFAULT_SCAN_INTERVAL_IDLE_MINUTES == 30
    assert const.DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES == 10
    assert const.MIN_SCAN_INTERVAL_MINUTES == 10
    assert (
        const.MIN_SCAN_INTERVAL_MINUTES
        <= const.DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES
        <= const.DEFAULT_SCAN_INTERVAL_IDLE_MINUTES
        <= const.MAX_SCAN_INTERVAL_MINUTES
    )
    assert const.DEGRADED_FAILURE_THRESHOLD == 3


def test_fake_clock_advances_without_sleeping(clock: FakeClock) -> None:
    """`FakeClock` is callable and monotonic under `advance()`."""
    assert clock() == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    clock.advance(minutes=90)
    assert clock() == datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
