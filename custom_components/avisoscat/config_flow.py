"""Config flow for the Avisos Meteocat (avisoscat) integration.

Two steps plus a fallback (docs/03-feature-spec.md §2):

1. **Location** (`async_step_user`): a map marker, prefilled with the Home
   Assistant home zone, resolved into a comarca by point-in-polygon over the
   comarques TopoJSON. Resolution is delegated to `comarques.py`, never
   reimplemented here.
2. **Comarca dropdown** (`async_step_comarca`): the fallback shown whenever the
   marker cannot be turned into a comarca (outside Catalonia, or the geometry
   could not be downloaded or decoded). The user picks one of the 43 land
   comarques manually. It is always reachable as a way out of a failed
   resolution, never a dead end.
3. **Options** (`async_step_options`): the per-comarca tuning of
   docs/03-feature-spec.md §2 step 2. An optional `api_key` is validated against
   the Meteocat quota endpoint and, when accepted, selects the official source.

A separate `OptionsFlow` exposes everything from step 3 except `api_key`, which
is rotated through reauth (docs/04-architecture.md §11). Reauth and reconfigure
are out of scope for this task.

Multi-entry by design: one config entry per comarca, `unique_id = str(id_comarca)`
with `_abort_if_unique_id_configured()`. There is no `single_config_entry` and
no YAML (docs/03-feature-spec.md §2).
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    LocationSelector,
    LocationSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .comarques import async_resolve_comarca, comarques_terrestres, id_mar, nom
from .const import (
    CONF_API_KEY,
    CONF_ID_COMARCA,
    CONF_INCLUDE_SEA,
    CONF_LOCATION,
    CONF_METEORS,
    CONF_SCAN_INTERVAL,
    CONF_SEVERE_THRESHOLD,
    DEFAULT_INCLUDE_SEA,
    DEFAULT_SEVERE_THRESHOLD,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .models import Meteor
from .smp import ApiKeySource

__all__ = ["AvisoscatConfigFlow", "AvisoscatOptionsFlow", "async_validate_api_key"]

# The 10 SMP meteors as multi-select options. The meteor keys come from the
# `Meteor` enum (docs/03-feature-spec.md §3.5); the labels are the Catalan names
# the source itself uses, which is also the reference UI language. The comarca
# dropdown below follows the same precedent: the names are Catalan data.
METEOR_LABELS: Final[dict[Meteor, str]] = {
    Meteor.VENT: "Vent",
    Meteor.PLUJA_30MIN: "Pluja (30 min)",
    Meteor.PLUJA_3H: "Pluja (3 h)",
    Meteor.PLUJA_ACUMULADA: "Pluja acumulada",
    Meteor.NEU: "Neu",
    Meteor.MAR: "Estat de la mar",
    Meteor.FRED: "Fred",
    Meteor.CALOR: "Calor",
    Meteor.CALOR_NOCTURNA: "Calor nocturna",
    Meteor.TEMPS_VIOLENT: "Temps violent",
}

METEOR_OPTIONS: Final[list[SelectOptionDict]] = [
    {"value": meteor.value, "label": METEOR_LABELS[meteor]} for meteor in Meteor
]

# All meteors followed by default (docs/03-feature-spec.md §2): a user who does
# not care simply gets every per-meteor sensor.
DEFAULT_METEORS: Final[list[str]] = [meteor.value for meteor in Meteor]

# The 43 land comarques the manual dropdown offers, sorted by name. Values are
# strings because `SelectSelector` keys are strings; the flow parses the id back
# to int. Maritime zones (ids 88-99) are never offered: they attach to a coastal
# comarca via `include_sea`, they are not chosen directly.
COMARCA_OPTIONS: Final[list[SelectOptionDict]] = [
    {"value": str(comarca.id_comarca), "label": comarca.nom}
    for comarca in comarques_terrestres()
]


def _entry_title(id_comarca: int) -> str:
    """The human name of an entry: the product name plus the comarca.

    The separator is part of the product's naming convention and is reproduced
    verbatim from docs/03-feature-spec.md §2 (mirrored by the device name in
    §3); it is the one em dash this integration writes on purpose.
    """
    return f"Avisos Meteocat — {nom(id_comarca)}"


def _location_schema(hass: HomeAssistant) -> vol.Schema:
    """The user step: a single location marker, prefilled with the home zone.

    No radius: warnings are published per comarca, and the marker only picks
    which comarca to follow (docs/03-feature-spec.md §2 step 1).
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_LOCATION,
                default={
                    CONF_LATITUDE: hass.config.latitude,
                    CONF_LONGITUDE: hass.config.longitude,
                },
            ): LocationSelector(LocationSelectorConfig(radius=False)),
        }
    )


def _comarca_schema() -> vol.Schema:
    """The fallback step: pick a comarca from the 43 land comarques."""
    return vol.Schema(
        {
            vol.Required(CONF_ID_COMARCA): SelectSelector(
                SelectSelectorConfig(
                    options=COMARCA_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _options_schema(
    *,
    id_comarca: int,
    with_api_key: bool,
    defaults: dict[str, Any],
) -> vol.Schema:
    """The step 2 schema, shared by the init flow and the options flow.

    `with_api_key` is True only on first setup: the key lives in `entry.data`
    and is rotated through reauth, so the options flow never edits it
    (docs/04-architecture.md §11). `include_sea` appears only for a coastal
    comarca, since an inland one has no maritime zone in front of it. A blank
    `scan_interval` means adaptive polling (docs/03-feature-spec.md §6).
    """
    fields: dict[Any, Any] = {}

    if with_api_key:
        fields[vol.Optional(CONF_API_KEY, default=defaults.get(CONF_API_KEY, ""))] = (
            TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        )

    fields[
        vol.Required(CONF_METEORS, default=defaults.get(CONF_METEORS, DEFAULT_METEORS))
    ] = SelectSelector(SelectSelectorConfig(options=METEOR_OPTIONS, multiple=True))

    fields[
        vol.Required(
            CONF_SEVERE_THRESHOLD,
            default=defaults.get(CONF_SEVERE_THRESHOLD, DEFAULT_SEVERE_THRESHOLD),
        )
    ] = NumberSelector(
        NumberSelectorConfig(min=1, max=6, step=1, mode=NumberSelectorMode.SLIDER)
    )

    # Only a coastal comarca can offer the maritime zone in front of it.
    if id_mar(id_comarca) is not None:
        fields[
            vol.Required(
                CONF_INCLUDE_SEA,
                default=defaults.get(CONF_INCLUDE_SEA, DEFAULT_INCLUDE_SEA),
            )
        ] = BooleanSelector()

    # A blank interval means adaptive polling. `NumberSelector` rejects `None`,
    # so the default is wired only when there is an actual number to prefill;
    # otherwise the field is a plain optional that the user may leave empty.
    scan_default = defaults.get(CONF_SCAN_INTERVAL)
    scan_field: Any = (
        vol.Optional(CONF_SCAN_INTERVAL, default=scan_default)
        if scan_default is not None
        else vol.Optional(CONF_SCAN_INTERVAL)
    )
    fields[scan_field] = NumberSelector(
        NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL_MINUTES,
            max=MAX_SCAN_INTERVAL_MINUTES,
            step=5,
            unit_of_measurement="min",
            mode=NumberSelectorMode.BOX,
        )
    )

    return vol.Schema(fields)


async def async_validate_api_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Validate a Meteocat API key against the quota endpoint.

    Returns a config-flow error key, or `None` when the key is accepted. The
    check reuses the live `ApiKeySource` so the config flow and the runtime
    agree on what a valid key is: a `403` is `invalid_auth`, and anything else
    that prevents a clean answer (quota exhausted, network) is `cannot_connect`.
    A response with no recognisable plan still counts as valid, because the
    request returned `200`; the quota sensor simply has nothing to show.
    """
    session = async_get_clientsession(hass)
    try:
        await ApiKeySource(session, api_key).fetch_quota()
    except ConfigEntryAuthFailed:
        return "invalid_auth"
    except UpdateFailed:
        return "cannot_connect"
    return None


class AvisoscatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for avisoscat."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow with no comarca selected yet."""
        self._id_comarca: int | None = None
        # Why the user landed on the comarca fallback, if they did. Doubles as a
        # config-flow error key surfaced above the dropdown.
        self._location_error: str | None = None
        # Last options-form input, kept so a validation error does not wipe it.
        self._options_input: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve a location into a comarca, or fall back to the dropdown."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_location_schema(self.hass)
            )

        location = user_input[CONF_LOCATION]
        result = await async_resolve_comarca(
            async_get_clientsession(self.hass),
            location[CONF_LATITUDE],
            location[CONF_LONGITUDE],
        )
        if result.ok:
            assert result.id_comarca is not None
            return await self._select_comarca(result.id_comarca)

        # Any resolution failure routes to the manual dropdown, carrying the
        # reason so it can be shown above the list. The dropdown is never a dead
        # end: outside Catalonia, a dead source, or unusable geometry all land
        # on the same way out.
        self._location_error = result.error.value if result.error else "cannot_connect"
        return await self.async_step_comarca()

    async def async_step_comarca(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a comarca manually when the location did not resolve."""
        if user_input is not None:
            return await self._select_comarca(int(user_input[CONF_ID_COMARCA]))

        errors: dict[str, str] = {}
        if self._location_error is not None:
            errors["base"] = self._location_error
        return self.async_show_form(
            step_id="comarca", data_schema=_comarca_schema(), errors=errors
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the step 2 options, validating the API key when one is given."""
        assert self._id_comarca is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options_input = user_input
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            if api_key:
                error = await async_validate_api_key(self.hass, api_key)
                if error is not None:
                    errors[CONF_API_KEY] = error

            if not errors:
                data: dict[str, Any] = {CONF_ID_COMARCA: self._id_comarca}
                if api_key:
                    data[CONF_API_KEY] = api_key
                options = {
                    CONF_METEORS: user_input[CONF_METEORS],
                    CONF_SEVERE_THRESHOLD: user_input[CONF_SEVERE_THRESHOLD],
                    CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL),
                }
                if id_mar(self._id_comarca) is not None:
                    options[CONF_INCLUDE_SEA] = user_input[CONF_INCLUDE_SEA]
                return self.async_create_entry(
                    title=_entry_title(self._id_comarca), data=data, options=options
                )

        return self.async_show_form(
            step_id="options",
            data_schema=_options_schema(
                id_comarca=self._id_comarca,
                with_api_key=True,
                defaults=self._options_input,
            ),
            errors=errors,
        )

    async def _select_comarca(self, id_comarca: int) -> ConfigFlowResult:
        """Fix the comarca, guard against duplicates, and move to options.

        `unique_id` is `str(id_comarca)`, so a second entry for the same comarca
        aborts here, before the options form is ever shown. Different comarques
        stay allowed: that is the multi-entry contract.
        """
        await self.async_set_unique_id(str(id_comarca))
        self._abort_if_unique_id_configured()
        self._id_comarca = id_comarca
        return await self.async_step_options()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AvisoscatOptionsFlow:
        """Return the options flow handler."""
        return AvisoscatOptionsFlow()


class AvisoscatOptionsFlow(OptionsFlow):
    """Edit the step 2 options of an existing entry.

    Everything from step 2 except `api_key`, which is rotated through reauth
    (docs/03-feature-spec.md §2, docs/04-architecture.md §11).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the per-comarca options."""
        id_comarca = int(self.config_entry.data[CONF_ID_COMARCA])

        if user_input is not None:
            options: dict[str, Any] = {
                CONF_METEORS: user_input[CONF_METEORS],
                CONF_SEVERE_THRESHOLD: user_input[CONF_SEVERE_THRESHOLD],
                CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL),
            }
            if id_mar(id_comarca) is not None:
                options[CONF_INCLUDE_SEA] = user_input[CONF_INCLUDE_SEA]
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                id_comarca=id_comarca,
                with_api_key=False,
                defaults=dict(self.config_entry.options),
            ),
        )
