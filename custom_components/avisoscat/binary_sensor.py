"""Binary sensors: four SAFETY switches per comarca (§3.8-§3.11).

Each switch answers one yes/no question about the current snapshot, sourced
from the projections the coordinator already holds on
`AvisoscatState.en_vigor` / `.anunciats`:

* `avis_actiu` (§3.8): is anything in force right now?
* `avis_greu` (§3.9): is anything in force at or above `severe_threshold`?
* `avis_greu_anunciat` (§3.10): is anything announced (later today / tomorrow
  / the day after) at or above `severe_threshold`?
* `temps_violent` (§3.11): is a violent-weather nowcast inside its 2 h window?

All four are `device_class: SAFETY`, where `on` means the hazard is present
(unsafe) and `off` means it is not (safe). The severe-threshold switches read
the option live (never cached on `__init__`), so the entities react whether or
not a reconfigure reloads them.

`temps_violent` needs no fetch to turn off: the coordinator's minute recompute
re-projects the cached snapshot against the clock, the violent window's `fi` is
`data_emissio + 2 h`, and the moment the clock crosses it the projection leaves
`en_vigor`, the coordinator notifies its listeners and this switch reads `off`
(docs/04-architecture.md §5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SEVERE_THRESHOLD, DEFAULT_SEVERE_THRESHOLD
from .entity import AvisoscatEntity, iso
from .vigencia import AfectacioProjectada, pic

if TYPE_CHECKING:
    from . import AvisoscatConfigEntry
    from .coordinator import AvisoscatDataUpdateCoordinator


# Coordinator-driven, read-only platform: entities never poll or write, so no
# parallelism limit is needed. Declared explicitly for the silver
# `parallel_updates` quality-scale rule, mirroring `sensor.py`.
PARALLEL_UPDATES = 0

# A warning that does not even reach grade 1 is not a hazard statement, so the
# `avis_actiu` switch treats grade 0 (also the failed-to-parse value) as absent.
# Filtering by grade is the consumer's call (docs/04-architecture.md §5).
_GRAU_MINIM_AVIS = 1

# Grade 5 is the "molt alt" band and the line above which a violent nowcast
# reports `alta` probability (§3.11). Below it, the label is `mitjana`.
_GRAU_PROBABILITAT_ALTA = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AvisoscatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the four binary sensors for one comarca.

    All four exist unconditionally per entry: the per-meteor and maritime
    entities that depend on options live elsewhere.
    """
    coordinator = entry.runtime_data
    async_add_entities(
        [
            AvisActiuBinarySensor(coordinator, entry),
            AvisGreuBinarySensor(coordinator, entry),
            AvisGreuAnunciatBinarySensor(coordinator, entry),
            TempsViolentBinarySensor(coordinator, entry),
        ]
    )


def _meteor_value(af: AfectacioProjectada | None) -> str | None:
    """The enum value of a projection's meteor, `None` when it is missing."""
    return af.meteor.value if af is not None and af.meteor is not None else None


def _severe_threshold(entry: AvisoscatConfigEntry) -> int:
    """The configured severe threshold, falling back to the documented default.

    Read live on every state access rather than cached in `__init__`, so the
    switch reacts whether or not HA reloads the entry on options change.
    """
    return int(entry.options.get(CONF_SEVERE_THRESHOLD, DEFAULT_SEVERE_THRESHOLD))


def _probabilitat(perill: int) -> str:
    """Grade to probability label for a violent nowcast (§3.11).

    The spec lists `alta` / `mitjana` as the two values a violent nowcast
    carries. A nowcast reaches this sensor only when its 2 h window is open,
    so it is always a live hazard; the label grades it.
    """
    return "alta" if perill >= _GRAU_PROBABILITAT_ALTA else "mitjana"


class _AvisoscatBinarySensor(AvisoscatEntity, BinarySensorEntity):
    """Common base: SAFETY device class, read-only, lives off the coordinator.

    `is_on` defaults to `False` (the safe state) before the first refresh and
    during a fetch failure: `AvisoscatEntity.available` already keeps the
    entity readable on the last good state, and a hazard switch reporting
    `off` while the source is down would be a worse failure than holding the
    last known value.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY


class AvisActiuBinarySensor(_AvisoscatBinarySensor):
    """ON quan hi ha qualsevol avís en vigor (§3.8).

    The most permissive of the four: any in-force warning at grade 1 or above
    turns it on, regardless of meteor or threshold. The peak (most severe
    in-force projection) anchors the attribute table.
    """

    _attr_translation_key = "avis_actiu"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "avis_actiu")

    def _actius(self) -> list[AfectacioProjectada]:
        """In-force projections at grade 1 or above, never the grade-0 noise."""
        state = self.coordinator.data
        if state is None:
            return []
        return [af for af in state.en_vigor if af.perill >= _GRAU_MINIM_AVIS]

    @property
    def is_on(self) -> bool:
        """`True` while at least one warning is in force."""
        return bool(self._actius())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`meteor_principal`, `perill_maxim`, `nombre_avisos` (§3.8)."""
        actius = self._actius()
        if not actius:
            return {}
        peak = pic(actius)
        return {
            "meteor_principal": _meteor_value(peak),
            "perill_maxim": peak.perill if peak is not None else 0,
            "nombre_avisos": len(actius),
        }


class AvisGreuBinarySensor(_AvisoscatBinarySensor):
    """ON quan hi ha un avís en vigor amb grau ≥ severe_threshold (§3.9).

    The recommended trigger for immediate-protection automations, so it is
    deliberately inclusive: the violent-weather nowcast (grade 5-6) counts
    here too, and a single switch covers the whole "act now" horizon.
    """

    _attr_translation_key = "avis_greu"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "avis_greu")
        self._entry = entry

    def _greus(self) -> list[AfectacioProjectada]:
        """In-force projections at or above the configured severe threshold."""
        state = self.coordinator.data
        if state is None:
            return []
        threshold = _severe_threshold(self._entry)
        return [af for af in state.en_vigor if af.perill >= threshold]

    @property
    def is_on(self) -> bool:
        """`True` while any in-force warning reaches the severe threshold."""
        return bool(self._greus())


class AvisGreuAnunciatBinarySensor(_AvisoscatBinarySensor):
    """ON quan hi ha un avís anunciat (futur) amb grau ≥ severe_threshold (§3.10).

    The §1.1 design-error guard applied to severe warnings: a warning issued
    for tomorrow that crosses the threshold lights this switch but leaves
    `avis_greu` off, because nothing severe is in force *yet*. The
    preparation-automation horizon (§1.1).
    """

    _attr_translation_key = "avis_greu_anunciat"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "avis_greu_anunciat")
        self._entry = entry

    def _greus_anunciats(self) -> list[AfectacioProjectada]:
        """Announced projections at or above the configured severe threshold."""
        state = self.coordinator.data
        if state is None:
            return []
        threshold = _severe_threshold(self._entry)
        return [af for af in state.anunciats if af.perill >= threshold]

    @property
    def is_on(self) -> bool:
        """`True` while any announced warning reaches the severe threshold."""
        return bool(self._greus_anunciats())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`comenca`, `hores_per_endavant`, `meteor`, `perill` of the peak."""
        greus = self._greus_anunciats()
        if not greus:
            return {}
        # `pic` returns the most severe of a non-empty list, never `None` here.
        peak = pic(greus)
        return {
            "comenca": iso(peak.inici),
            "hores_per_endavant": peak.hores_per_endavant,
            "meteor": _meteor_value(peak),
            "perill": peak.perill,
        }


class TempsViolentBinarySensor(_AvisoscatBinarySensor):
    """ON mentre un avís de temps violent és dins la seva finestra de 2 h (§3.11).

    Turns off by the clock, not by the poll: the coordinator's once-a-minute
    recompute re-projects the cached snapshot, the violent window's `fi` is
    `data_emissio + 2 h`, and the moment the clock crosses it the projection
    leaves `en_vigor`. This switch reads `en_vigor`, so it goes `off` on the
    next minute tick after the window closes, with no fetch in between.
    """

    _attr_translation_key = "temps_violent"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "temps_violent")

    def _violent(self) -> AfectacioProjectada | None:
        """The most severe in-force violent nowcast, `None` when none is live."""
        state = self.coordinator.data
        if state is None:
            return None
        return pic([af for af in state.en_vigor if af.is_temps_violent])

    @property
    def is_on(self) -> bool:
        """`True` while a violent-weather nowcast is inside its 2 h window."""
        return self._violent() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`probabilitat`, `llindar`, `data_emissio`, `valid_fins` (§3.11)."""
        violent = self._violent()
        if violent is None:
            return {}
        return {
            "probabilitat": _probabilitat(violent.perill),
            "llindar": violent.llindar,
            "data_emissio": iso(violent.data_emissio),
            "valid_fins": iso(violent.fi),
        }
