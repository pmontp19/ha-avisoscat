"""Level sensors: the warning level now, the active count, and the outlook grid.

The platform that answers the two questions a user opens the integration for
(docs/03-feature-spec.md §3.1-§3.4, §3.6):

* **How bad is it right now?** `nivell_d_avis` (§3.1) and `avisos_actius` (§3.2)
  read the in-force projection (`state.en_vigor`).
* **What is coming?** `avis_anunciat` (§3.3) reads the announced projection
  (`state.anunciats`), and the three `grau_maxim_*` sensors (§3.4) read the
  per-day outlook (`state.outlook`). Mixing these two horizons up is the design
  error of §1.1, so each sensor sticks to one projection.
* `preavis` (§3.6) reads the Catalonia-wide pre-warnings
  (`state.snapshot.preavisos`), which have no comarca.

Every level sensor is `SensorDeviceClass.ENUM` with the four traffic-light
states (docs/04-architecture.md §9), so dashboards and automation conditions
treat the level as a discrete state instead of a string to compare against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import utcnow

from .entity import AvisoscatEntity, iso
from .models import NivellPerill, Preavis
from .vigencia import (
    AfectacioProjectada,
    OutlookDia,
    pic,
    preavisos_actius,
)

if TYPE_CHECKING:
    from . import AvisoscatConfigEntry
    from .coordinator import AvisoscatDataUpdateCoordinator


# The four traffic-light states, in increasing severity, fixed by
# docs/03-feature-spec.md §3.1 and docs/04-architecture.md §9. Exposed in this
# exact order so the `ENUM` device class lists them in the natural progression.
NIVELL_OPTIONS: tuple[str, ...] = (
    NivellPerill.CAP.value,
    NivellPerill.MODERAT.value,
    NivellPerill.ALT.value,
    NivellPerill.MOLT_ALT.value,
)

# Relative-day labels carried by `OutlookDia.etiqueta`, mirrored here so each
# `grau_maxim_*` sensor can pick its day out of `state.outlook` by name without
# importing vigencia's internals.
DIA_AVUI = "avui"
DIA_DEMA = "dema"
DIA_DEMA_PASSAT = "dema_passat"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AvisoscatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the level sensors for one comarca.

    These seven sensors exist for every entry unconditionally: the per-meteor
    sensors (§3.5) and the maritime sensor (§3.7) are the ones that depend on
    options, and they land in their own tasks.
    """
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NivellDAvisSensor(coordinator, entry),
            AvisosActiusSensor(coordinator, entry),
            AvisAnunciatSensor(coordinator, entry),
            GrauMaximSensor(coordinator, entry, DIA_AVUI),
            GrauMaximSensor(coordinator, entry, DIA_DEMA),
            GrauMaximSensor(coordinator, entry, DIA_DEMA_PASSAT),
            PreavisSensor(coordinator, entry),
        ]
    )


def _meteor_value(af: AfectacioProjectada | None) -> str | None:
    """The enum value of a projection's meteor, `None` when it is missing."""
    return af.meteor.value if af is not None and af.meteor is not None else None


def _tipus_value(af: AfectacioProjectada | None) -> str | None:
    """The enum value of a projection's warning type, `None` when it is missing."""
    return af.tipus.value if af is not None and af.tipus is not None else None


class _EnumSensor(AvisoscatEntity, SensorEntity):
    """An avisoscat sensor whose state is one of the four traffic-light levels.

    Subclasses implement `_peak()` to return the projection whose grade this
    sensor reports, or `None` when nothing applies. `native_value` then folds
    that to the traffic-light category, `cap` for the empty case.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = list(NIVELL_OPTIONS)

    @property
    def native_value(self) -> str:
        """The traffic-light level, `cap` when nothing applies."""
        peak = self._peak()
        if peak is None:
            return NivellPerill.CAP.value
        return peak.nivell_perill.value

    def _peak(self) -> AfectacioProjectada | None:
        """The peak in-force projection this sensor reads.

        Default to `None`; subclasses override to point at one of the three
        projections on `AvisoscatState`.
        """
        return None


class NivellDAvisSensor(_EnumSensor):
    """Grau més alt vigent ara a la comarca (§3.1)."""

    _attr_translation_key = "warning_level"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "warning_level")

    def _peak(self) -> AfectacioProjectada | None:
        state = self.coordinator.data
        if state is None:
            return None
        return pic(state.en_vigor)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The §3.1 attribute table, anchored to the in-force peak."""
        peak = self._peak()
        if peak is None:
            return {}
        return {
            "perill": peak.perill,
            "meteor": _meteor_value(peak),
            "tipus": _tipus_value(peak),
            "llindar": peak.llindar,
            "nivell": peak.nivell,
            "periode": peak.periode,
            "distribucio_geografica": peak.distribucio_geografica,
            "comentari": peak.comentari,
            "data_inici": iso(peak.data_inici),
            "data_fi": iso(peak.data_fi),
            "data_emissio": iso(peak.data_emissio),
        }


class AvisosActiusSensor(AvisoscatEntity, SensorEntity):
    """Nombre d'avisos vigents ara (§3.2)."""

    _attr_native_unit_of_measurement = "avisos"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "active_warnings"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "active_warnings")

    @property
    def native_value(self) -> int:
        """How many affectations are in force now."""
        state = self.coordinator.data
        if state is None:
            return 0
        return len(state.en_vigor)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """One `{meteor, perill, tipus, periode, llindar}` row per in-force band."""
        state = self.coordinator.data
        if state is None:
            return {"avisos": []}
        avisos: list[dict[str, Any]] = [
            {
                "meteor": _meteor_value(af),
                "perill": af.perill,
                "tipus": _tipus_value(af),
                "periode": af.periode,
                "llindar": af.llindar,
            }
            for af in state.en_vigor
        ]
        return {"avisos": avisos}


class AvisAnunciatSensor(_EnumSensor):
    """Grau més alt d'un avís emès que encara no ha entrat en vigor (§3.3).

    The §1.1 design-error guard: this sensor reports the announced horizon only.
    A warning issued for tomorrow moves this sensor but leaves `nivell_d_avis`
    on `cap`, because the in-force horizon is still empty.
    """

    _attr_translation_key = "announced_warning"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "announced_warning")

    def _peak(self) -> AfectacioProjectada | None:
        state = self.coordinator.data
        if state is None:
            return None
        return pic(state.anunciats)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The §3.3 attribute table, anchored to the announced peak."""
        peak = self._peak()
        if peak is None:
            return {}
        return {
            "perill": peak.perill,
            "meteor": _meteor_value(peak),
            "llindar": peak.llindar,
            "nivell": peak.nivell,
            "comenca": iso(peak.inici),
            "hores_per_endavant": peak.hores_per_endavant,
            "dia": peak.etiqueta_dia,
            "periode": peak.periode,
        }


class GrauMaximSensor(_EnumSensor):
    """Grau màxim previst per a un dia, amb la graella de 4 franges (§3.4).

    One instance per relative day (`avui` / `dema` / `dema_passat`), reading
    the matching `OutlookDia` from `state.outlook`. The state is the
    traffic-light category of the day's peak grade; the `graella` attribute
    keeps the raw 0-6 grade per band, exactly the four bands of the day.
    """

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
        etiqueta: str,
    ) -> None:
        """Pin the relative day and the translation key that names this sensor."""
        translation_key = _GRAU_MAXIM_KEYS[etiqueta]
        super().__init__(coordinator, entry, translation_key)
        self._attr_translation_key = translation_key
        self._etiqueta = etiqueta

    def _dia(self) -> OutlookDia | None:
        """The day of the outlook this sensor reports, `None` before the first fetch."""
        state = self.coordinator.data
        if state is None:
            return None
        for dia in state.outlook:
            if dia.etiqueta == self._etiqueta:
                return dia
        return None

    def _peak(self) -> AfectacioProjectada | None:
        dia = self._dia()
        return dia.pic if dia is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The four bands of the day plus the peak's meteor and threshold."""
        dia = self._dia()
        if dia is None:
            return {}
        peak = dia.pic
        return {
            "meteor": _meteor_value(peak),
            "periode": peak.periode if peak is not None else None,
            "nivell": peak.nivell if peak is not None else None,
            "llindar": peak.llindar if peak is not None else None,
            "graella": dia.graella,
        }


# Relative-day label → translation key. The translation key also becomes the
# entity name suffix and the second half of the unique id.
_GRAU_MAXIM_KEYS: dict[str, str] = {
    DIA_AVUI: "max_grade_today",
    DIA_DEMA: "max_grade_tomorrow",
    DIA_DEMA_PASSAT: "max_grade_day_after",
}


class PreavisSensor(_EnumSensor):
    """Grau màxim del preavís vigent a escala de Catalunya (§3.6).

    Pre-warnings have no comarca and no time bands, so this sensor does not
    read `state.en_vigor`: it picks the severest open pre-warning from the
    snapshot, evaluated against the clock by `vigencia.preavisos_actius`.
    """

    _attr_translation_key = "prewarning"

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry, "prewarning")

    def _preavis(self) -> Preavis | None:
        """The severest open pre-warning at the Catalonia scale, `None` if none."""
        state = self.coordinator.data
        if state is None or state.snapshot is None:
            return None
        actius = preavisos_actius(state.snapshot.preavisos, utcnow())
        return actius[0] if actius else None

    @property
    def native_value(self) -> str:
        """Category of the severest pre-warning; `cap` when none is open."""
        preavis = self._preavis()
        if preavis is None:
            return NivellPerill.CAP.value
        return preavis.nivell_perill.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The §3.6 attribute table, anchored to the severest open pre-warning."""
        preavis = self._preavis()
        if preavis is None:
            return {}
        return {
            "meteor": preavis.meteor.value if preavis.meteor is not None else None,
            "perill": preavis.perill,
            "llindar": preavis.llindar,
            "data_inici": iso(preavis.data_inici),
            "data_fi": iso(preavis.data_fi),
            "comentari": preavis.comentari,
        }
