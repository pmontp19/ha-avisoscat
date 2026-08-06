"""Constants for the Avisos Meteocat (avisoscat) integration.

Endpoint URLs come from docs/01-data-sources.md §7 ("Endpoints definitius per a
la integració"). Polling defaults come from docs/03-feature-spec.md §6 and the
config/option keys from docs/03-feature-spec.md §2.
"""

DOMAIN = "avisoscat"

# Data ownership and legal notice: docs/01-data-sources.md §8.
ATTRIBUTION = "Dades del Servei Meteorològic de Catalunya (Meteocat)"

# ---------------------------------------------------------------------------
# SMP warnings without an API key — default source
# (docs/01-data-sources.md §7)
#
# Both pages embed the same inline `Meteocat.avisosSMP(...)` payload, but
# docs/01-data-sources.md §3.1 recorded them returning slightly different
# episode sets. The parser task validates which one is authoritative and
# whether the fallback is still needed; until then treat the radar page as
# primary and the homepage as the fallback, exactly as documented.
# ---------------------------------------------------------------------------

SMP_PAGE_URL = "https://www.meteo.cat/observacions/radar"
SMP_PAGE_FALLBACK_URL = "https://www.meteo.cat/"

# ---------------------------------------------------------------------------
# SMP warnings with an API key — optional, `x-api-key` header
# (docs/01-data-sources.md §7)
#
# The open-episodes endpoint takes a date parameter, so it is a template:
# format it with an ISO date, e.g. `SMP_API_EPISODIS_OBERTS_URL.format(
# data="2026-08-06")`.
# ---------------------------------------------------------------------------

SMP_API_EPISODIS_OBERTS_URL = (
    "https://api.meteo.cat/pronostic/v2/smp/episodis-oberts?data={data}Z"
)
SMP_API_PREAVISOS_URL = (
    "https://api.meteo.cat/pronostic/v1/smp/episodis-oberts/preavisos"
)
SMP_API_QUOTA_URL = "https://api.meteo.cat/quotes/v1/consum-actual"

# ---------------------------------------------------------------------------
# Territorial reference, no API key (docs/01-data-sources.md §7)
#
# TopoJSON of the 43 comarques plus the 12 maritime zones. Downloaded once by
# the config flow to resolve a location into a comarca; never polled at
# runtime (docs/04-architecture.md §6).
# ---------------------------------------------------------------------------

COMARQUES_TOPOJSON_URL = (
    "https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json"
)

# ---------------------------------------------------------------------------
# Polling strategy (docs/03-feature-spec.md §6)
#
# Adaptive with the public source: slow when nothing is happening, faster only
# while some episode is open (the only situation where the violent-weather
# nowcast matters). The floor is dictated by the source's own
# `cache-control: max-age=600`; polling faster returns the same bytes.
# ---------------------------------------------------------------------------

DEFAULT_SCAN_INTERVAL_IDLE_MINUTES = 30
DEFAULT_SCAN_INTERVAL_ACTIVE_MINUTES = 10
MIN_SCAN_INTERVAL_MINUTES = 10
MAX_SCAN_INTERVAL_MINUTES = 120

# Consecutive failures of the same kind before firing
# `avisoscat_service_degraded` and raising a repair issue
# (docs/04-architecture.md §8 and §10).
DEGRADED_FAILURE_THRESHOLD = 3

# ---------------------------------------------------------------------------
# Config entry data keys (docs/03-feature-spec.md §2)
#
# `api_key` lives in `entry.data`, never in `entry.options`: it is rotated
# through reauth, not through the options flow (docs/04-architecture.md §11).
# ---------------------------------------------------------------------------

CONF_API_KEY = "api_key"
CONF_COMARCA = "comarca"
CONF_ID_COMARCA = "id_comarca"

# ---------------------------------------------------------------------------
# Option keys (docs/03-feature-spec.md §2, step 2 and options flow)
# ---------------------------------------------------------------------------

CONF_METEORS = "meteors"
CONF_SEVERE_THRESHOLD = "severe_threshold"
CONF_INCLUDE_SEA = "include_sea"
CONF_SCAN_INTERVAL = "scan_interval"

# Danger grade (0-6) at or above which `binary_sensor.severe_warning` turns on.
# 3 is the official "Alt" band (docs/03-feature-spec.md §2).
DEFAULT_SEVERE_THRESHOLD = 3
DEFAULT_INCLUDE_SEA = False
