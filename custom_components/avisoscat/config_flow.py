"""Config flow for the Avisos Meteocat (avisoscat) integration.

Scaffold: the handler is declared so that Home Assistant (and hassfest, which
rejects `config_flow: true` without this module) can load the integration. The
steps themselves — location to comarca via point-in-polygon, options, reauth,
reconfigure (docs/03-feature-spec.md §2) — arrive with the config-flow task.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class AvisoscatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for avisoscat."""

    VERSION = 1
