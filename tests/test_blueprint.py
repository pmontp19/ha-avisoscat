"""Tests for `blueprints/automation/avisoscat_warning_notification.yaml`.

Two horizons, one blueprint (docs/03-feature-spec.md §1.1): an `announced`
event fires hours-to-days ahead ("d'aquí a N h"), a `started` event fires when
the warning enters force with no lead-time text. These tests pin both that
distinction and the feature-spec §5 filter contract.

Three layers of validation:

1. Structural: the blueprint declares exactly the inputs required by
   `docs/03-feature-spec.md` §5, with the selectors/defaults documented there.
2. Schema: the blueprint is loaded through Home Assistant's real blueprint
   machinery and substituted with concrete inputs into a valid automation.
3. Behavioural: the substituted blueprint is installed as a live automation
   entity and exercised by firing the six avisoscat events on `hass.bus`; we
   then assert on the resulting `notify_service` calls, the way a real
   installation would be verified manually (the task's "import manual" note).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml as pyyaml
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import async_mock_service

BLUEPRINT_PATH = str(
    Path(__file__).resolve().parent.parent
    / "blueprints"
    / "automation"
    / "avisoscat_warning_notification.yaml"
)

# Every input feature-spec §5 documents. The meteor selector enumerates the
# same 10 meteors the integration emits (models.Meteor).
EXPECTED_INPUTS = {
    "notification_service",
    "meteors",
    "minimum_perill",
    "notify_on",
    "max_hores_antelacio",
    "include_upgrades",
    "include_cleared",
    "include_violent_weather",
    "critical_alert",
}

ALL_METEORS = [
    "vent",
    "pluja_30min",
    "pluja_3h",
    "pluja_acumulada",
    "neu",
    "mar",
    "fred",
    "calor",
    "calor_nocturna",
    "temps_violent",
]

# The six bus events the coordinator fires (const.py, docs/03-feature-spec.md
# §4). Built to match the payload shapes of `coordinator._payload_*` exactly.
ANNOUNCED_PAYLOAD = {
    "comarca": "Osona",
    "id_comarca": 24,
    "meteor": "vent",
    "meteor_nom": "Vent",
    "tipus": "avis",
    "perill": 4,
    "nivell_text": "alt",
    "nivell": 2,
    "llindar": "Ratxa màxima > 108 km/h (30 m/s)",
    "comenca": "2026-08-07T16:00:00+00:00",
    "hores_per_endavant": 41,
    "dia": "dema_passat",
    "periode": "12-18",
    "distribucio_geografica": "EXTENSA",
    "comentari": "Ratxes molt fortes al litoral.",
    "data_emissio": "2026-08-05T23:00:00+00:00",
    "data_inici": "2026-08-07T12:00:00+00:00",
    "data_fi": "2026-08-07T23:59:00+00:00",
}

STARTED_PAYLOAD = {
    "comarca": "Osona",
    "id_comarca": 24,
    "meteor": "pluja_30min",
    "meteor_nom": "Intensitat de pluja en 30 minuts",
    "tipus": "avis",
    "perill": 3,
    "nivell_text": "alt",
    "nivell": 1,
    "llindar": "Intensitat > 20 mm / 30 minuts",
    "periode": "12-18",
    "distribucio_geografica": "LOCAL",
    "comentari": "Els xàfecs aniran acompanyats de tempesta.",
    "data_inici": "2026-08-04T12:00:00+00:00",
    "data_fi": "2026-08-06T17:59:00+00:00",
    "data_emissio": "2026-08-04T15:30:00+00:00",
    "anunciat_amb_hores": 20,
}

UPGRADED_PAYLOAD = {
    "comarca": "Osona",
    "id_comarca": 24,
    "meteor": "vent",
    "perill_anterior": 2,
    "perill": 4,
    "nivell_text_anterior": "moderat",
    "nivell_text": "alt",
    "periode": "18-00",
    "llindar": "Ratxa màxima > 108 km/h (30 m/s)",
}

CLEARED_PAYLOAD = {
    "comarca": "Osona",
    "id_comarca": 24,
    "meteor": "vent",
    "perill_final": 4,
    "durada_min": 372,
    "motiu": "expirat",
}

VIOLENT_PAYLOAD = {
    "comarca": "Osona",
    "id_comarca": 24,
    "probabilitat": "alta",
    "llindar": "Pedra de diàmetre > 2 cm",
    "comentari": "",
    "data_emissio": "2026-08-05T16:12:00+00:00",
    "valid_fins": "2026-08-05T18:12:00+00:00",
}


# ---------------------------------------------------------------------------
# 1. Structural checks — a permissive YAML parse to inspect `input:` and the
#    trigger list without spinning up a full HA runtime (fast, no `hass`).
# ---------------------------------------------------------------------------


def _load_raw() -> dict[str, Any]:
    """Parse the blueprint with a loader that tolerates the `!input` tag.

    `!input` is an HA-specific YAML tag; plain `yaml.safe_load` doesn't know
    it, so register a tolerant multi-constructor that turns any unknown tag
    into a plain string/marker instead of raising.
    """

    class _TolerantLoader(pyyaml.SafeLoader):
        pass

    def _construct_unknown(
        loader: pyyaml.SafeLoader, tag_suffix: str, node: Any
    ) -> Any:
        if isinstance(node, pyyaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, pyyaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _TolerantLoader.add_multi_constructor("!", _construct_unknown)
    with open(BLUEPRINT_PATH, encoding="utf-8") as handle:
        return pyyaml.load(handle, Loader=_TolerantLoader)


def test_blueprint_metadata() -> None:
    """The blueprint declares the domain/name/source_url required by the spec."""
    raw = _load_raw()
    meta = raw["blueprint"]
    assert meta["domain"] == "automation"
    assert "Avisos Meteocat" in meta["name"]
    assert meta["source_url"].startswith(
        "https://github.com/pmontp19/ha-avisoscat/blob/main/"
    )
    assert "homeassistant" in meta
    assert "min_version" in meta["homeassistant"]


def test_blueprint_declares_all_required_inputs() -> None:
    """Every input from feature-spec §5 is present with the right selector/default."""
    raw = _load_raw()
    inputs = raw["blueprint"]["input"]
    assert set(inputs.keys()) == EXPECTED_INPUTS

    assert inputs["notification_service"]["default"] == "notify.notify"
    assert "text" in inputs["notification_service"]["selector"]

    assert inputs["meteors"]["default"] == ALL_METEORS
    meteo_selector = inputs["meteors"]["selector"]["select"]
    assert meteo_selector["multiple"] is True
    assert [opt["value"] for opt in meteo_selector["options"]] == ALL_METEORS

    assert inputs["minimum_perill"]["default"] == 3
    assert "number" in inputs["minimum_perill"]["selector"]

    notify_options = inputs["notify_on"]["selector"]["select"]["options"]
    assert [opt["value"] for opt in notify_options] == [
        "anunciat",
        "en_vigor",
        "tots dos",
    ]
    assert inputs["notify_on"]["default"] == "tots dos"

    assert inputs["max_hores_antelacio"]["default"] == 0
    assert "number" in inputs["max_hores_antelacio"]["selector"]

    assert inputs["include_upgrades"]["default"] is False
    assert inputs["include_cleared"]["default"] is False
    assert inputs["include_violent_weather"]["default"] is True
    assert inputs["critical_alert"]["default"] is False


def test_blueprint_triggers_on_all_six_events() -> None:
    """All six lifecycle events are wired as triggers (filtering is in actions)."""
    raw = _load_raw()
    event_types = {t["event_type"] for t in raw["triggers"]}
    assert event_types == {
        "avisoscat_warning_announced",
        "avisoscat_warning_started",
        "avisoscat_warning_upgraded",
        "avisoscat_warning_downgraded",
        "avisoscat_warning_cleared",
        "avisoscat_violent_weather",
    }


# ---------------------------------------------------------------------------
# 2. Full HA schema validation, via the real blueprint substitution pipeline.
# ---------------------------------------------------------------------------


def _substitute(user_inputs: dict[str, Any]) -> dict[str, Any]:
    data = yaml_util.load_yaml(BLUEPRINT_PATH)
    blueprint = Blueprint(
        data,
        path=BLUEPRINT_PATH,
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    inputs = BlueprintInputs(
        blueprint,
        {"use_blueprint": {"path": BLUEPRINT_PATH, "input": user_inputs}},
    )
    inputs.validate()
    return inputs.async_substitute()


@pytest.mark.parametrize(
    "user_inputs",
    [
        {"notification_service": "notify.notify"},
        {
            "notification_service": "notify.mobile_app_test",
            "meteors": ["vent", "calor"],
            "minimum_perill": 4,
            "notify_on": "anunciat",
            "max_hores_antelacio": 24,
            "include_upgrades": True,
            "include_cleared": True,
            "include_violent_weather": False,
            "critical_alert": True,
        },
        {"notification_service": "notify.notify", "notify_on": "en_vigor"},
    ],
)
async def test_blueprint_produces_valid_automation_config(
    hass: HomeAssistant, user_inputs: dict[str, Any]
) -> None:
    """The blueprint, substituted with any valid inputs, is a valid automation."""
    config = _substitute(user_inputs)
    validated = PLATFORM_SCHEMA(config)
    assert validated["triggers"]
    assert validated["actions"]


# ---------------------------------------------------------------------------
# 3. Behavioural: install as a real automation, fire events, assert on the
#    resulting notify service calls.
# ---------------------------------------------------------------------------


async def _install_automation(
    hass: HomeAssistant, *, alias: str, user_inputs: dict[str, Any]
) -> list[dict[str, Any]]:
    """Install the blueprint as a live automation and return captured calls."""

    def _copy_blueprint() -> None:
        blueprint_dir = Path(hass.config.path("blueprints", "automation", "avisoscat"))
        blueprint_dir.mkdir(parents=True, exist_ok=True)
        dest = blueprint_dir / "notification.yaml"
        dest.write_text(
            Path(BLUEPRINT_PATH).read_text(encoding="utf-8"), encoding="utf-8"
        )

    await hass.async_add_executor_job(_copy_blueprint)

    domain, service = user_inputs.get("notification_service", "notify.notify").split(
        ".", 1
    )
    calls = async_mock_service(hass, domain, service)

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": alias,
                    "use_blueprint": {
                        "path": "avisoscat/notification.yaml",
                        "input": user_inputs,
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()
    return calls


async def test_announced_notifies_with_lead_time_text(
    hass: HomeAssistant,
) -> None:
    """Default inputs: an announced wind warning fires a notify with "d'aquí a 41 h"."""
    calls = await _install_automation(
        hass,
        alias="announced_default",
        user_inputs={"notification_service": "notify.notify"},
    )
    hass.bus.async_fire("avisoscat_warning_announced", ANNOUNCED_PAYLOAD)
    await hass.async_block_till_done()

    assert len(calls) == 1
    data = calls[0].data
    # The anunci text must carry the lead time; the en-vigor branch must not.
    assert "d'aquí a 41 h" in data["title"]
    assert "Vent" in data["title"]
    assert "alt" in data["title"]
    assert "Perill 4/6" in data["message"]


async def test_started_notifies_without_lead_time_text(
    hass: HomeAssistant,
) -> None:
    """A started warning fires a notify marked "En vigor", with no lead time."""
    calls = await _install_automation(
        hass,
        alias="started_default",
        user_inputs={"notification_service": "notify.notify"},
    )
    hass.bus.async_fire("avisoscat_warning_started", STARTED_PAYLOAD)
    await hass.async_block_till_done()

    assert len(calls) == 1
    data = calls[0].data
    assert "En vigor" in data["title"]
    # The en-vigor branch never carries the announce lead-time phrase.
    assert "d'aquí a" not in data["title"]
    assert "d'aquí a" not in data["message"]


async def test_meteor_filter_excludes_unselected_meteor(
    hass: HomeAssistant,
) -> None:
    """`meteors` narrowed to calor: a vent announcement produces no call."""
    calls = await _install_automation(
        hass,
        alias="meteor_filter",
        user_inputs={"notification_service": "notify.notify", "meteors": ["calor"]},
    )
    hass.bus.async_fire("avisoscat_warning_announced", ANNOUNCED_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_minimum_perill_filters_low_grade(hass: HomeAssistant) -> None:
    """A grade-2 announcement below the default minimum_perill (3) is dropped."""
    calls = await _install_automation(
        hass,
        alias="perill_filter",
        user_inputs={"notification_service": "notify.notify"},
    )
    low_grade = dict(ANNOUNCED_PAYLOAD, perill=2, nivell_text="moderat")
    hass.bus.async_fire("avisoscat_warning_announced", low_grade)
    await hass.async_block_till_done()
    assert calls == []


async def test_notify_on_anunciat_skips_in_force(hass: HomeAssistant) -> None:
    """`notify_on: anunciat` must not notify when a warning enters force."""
    calls = await _install_automation(
        hass,
        alias="only_announced",
        user_inputs={"notification_service": "notify.notify", "notify_on": "anunciat"},
    )
    hass.bus.async_fire("avisoscat_warning_started", STARTED_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_notify_on_en_vigor_skips_announce(hass: HomeAssistant) -> None:
    """`notify_on: en_vigor` must not notify when a warning is merely announced."""
    calls = await _install_automation(
        hass,
        alias="only_in_force",
        user_inputs={"notification_service": "notify.notify", "notify_on": "en_vigor"},
    )
    hass.bus.async_fire("avisoscat_warning_announced", ANNOUNCED_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_max_hores_antelacio_drops_far_announcement(
    hass: HomeAssistant,
) -> None:
    """A 50 h announcement is dropped when max_hores_antelacio is 48."""
    calls = await _install_automation(
        hass,
        alias="antelacio_cap",
        user_inputs={
            "notification_service": "notify.notify",
            "max_hores_antelacio": 48,
        },
    )
    far = dict(ANNOUNCED_PAYLOAD, hores_per_endavant=50)
    hass.bus.async_fire("avisoscat_warning_announced", far)
    await hass.async_block_till_done()
    assert calls == []


async def test_max_hores_antelacio_keeps_near_announcement(
    hass: HomeAssistant,
) -> None:
    """A 41 h announcement passes when max_hores_antelacio is 48 (boundary)."""
    calls = await _install_automation(
        hass,
        alias="antelacio_ok",
        user_inputs={
            "notification_service": "notify.notify",
            "max_hores_antelacio": 48,
        },
    )
    hass.bus.async_fire("avisoscat_warning_announced", ANNOUNCED_PAYLOAD)
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_violent_weather_notifies_by_default(hass: HomeAssistant) -> None:
    """`include_violent_weather` defaults to true: a nowcast fires a notify."""
    calls = await _install_automation(
        hass,
        alias="violent_default",
        user_inputs={"notification_service": "notify.notify"},
    )
    hass.bus.async_fire("avisoscat_violent_weather", VIOLENT_PAYLOAD)
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert "TEMPS VIOLENT" in data["title"]
    assert "alta" in data["title"]


async def test_violent_weather_disabled(hass: HomeAssistant) -> None:
    """With `include_violent_weather: false`, the nowcast produces no call."""
    calls = await _install_automation(
        hass,
        alias="violent_off",
        user_inputs={
            "notification_service": "notify.notify",
            "include_violent_weather": False,
        },
    )
    hass.bus.async_fire("avisoscat_violent_weather", VIOLENT_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_cleared_ignored_by_default(hass: HomeAssistant) -> None:
    """`include_cleared` defaults to false: cleared events produce no call."""
    calls = await _install_automation(
        hass,
        alias="cleared_default",
        user_inputs={"notification_service": "notify.notify"},
    )
    hass.bus.async_fire("avisoscat_warning_cleared", CLEARED_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_cleared_notifies_when_enabled(hass: HomeAssistant) -> None:
    """With `include_cleared: true`, a cleared event fires a notify."""
    calls = await _install_automation(
        hass,
        alias="cleared_on",
        user_inputs={
            "notification_service": "notify.notify",
            "include_cleared": True,
        },
    )
    hass.bus.async_fire("avisoscat_warning_cleared", CLEARED_PAYLOAD)
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert "Resolt" in data["title"]
    assert "372 min" in data["message"]


async def test_upgraded_ignored_by_default(hass: HomeAssistant) -> None:
    """`include_upgrades` defaults to false: upgraded events produce no call."""
    calls = await _install_automation(
        hass,
        alias="upgraded_default",
        user_inputs={"notification_service": "notify.notify"},
    )
    hass.bus.async_fire("avisoscat_warning_upgraded", UPGRADED_PAYLOAD)
    await hass.async_block_till_done()
    assert calls == []


async def test_upgraded_notifies_when_enabled(hass: HomeAssistant) -> None:
    """With `include_upgrades: true`, an upgraded event fires a notify."""
    calls = await _install_automation(
        hass,
        alias="upgraded_on",
        user_inputs={
            "notification_service": "notify.notify",
            "include_upgrades": True,
        },
    )
    hass.bus.async_fire("avisoscat_warning_upgraded", UPGRADED_PAYLOAD)
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert "2 → 4" in data["message"]


async def test_critical_alert_sets_push_payload(hass: HomeAssistant) -> None:
    """`critical_alert: true` adds the iOS/Android critical-notification payload."""
    calls = await _install_automation(
        hass,
        alias="critical_alert",
        user_inputs={"notification_service": "notify.notify", "critical_alert": True},
    )
    hass.bus.async_fire("avisoscat_violent_weather", VIOLENT_PAYLOAD)
    await hass.async_block_till_done()

    assert len(calls) == 1
    push = calls[0].data["data"]["push"]
    assert push["interruption-level"] == "critical"
    assert calls[0].data["data"]["priority"] == "high"
