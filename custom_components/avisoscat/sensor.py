"""Level sensors: the warning level now, the active count, and the outlook grid.

The platform that answers the two questions a user opens the integration for
(docs/03-feature-spec.md §3.1-§3.6):

* **How bad is it right now?** `nivell_d_avis` (§3.1) and `avisos_actius` (§3.2)
  read the in-force projection (`state.en_vigor`).
* **What is coming?** `avis_anunciat` (§3.3) reads the announced projection
  (`state.anunciats`), and the three `grau_maxim_*` sensors (§3.4) read the
  per-day outlook (`state.outlook`). Mixing these two horizons up is the design
  error of §1.1, so each sensor sticks to one projection.
* `preavis` (§3.6) reads the Catalonia-wide pre-warnings
  (`state.snapshot.preavisos`), which have no comarca.
* The ten `avis_<meteor>` sensors (§3.5) narrow §3.1 to one phenomenon, so a
  dashboard or automation can follow "is there a heat warning?" without parsing
  the aggregate. They are created only for the meteors the user selected.

Every level sensor is `SensorDeviceClass.ENUM` with the four traffic-light
states (docs/04-architecture.md §9), so dashboards and automation conditions
treat the level as a discrete state instead of a string to compare against.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import utcnow

from .config_flow import DEFAULT_METEORS
from .const import CONF_METEORS
from .entity import AvisoscatEntity, iso
from .models import Meteor, NivellPerill, Preavis
from .vigencia import (
    AfectacioProjectada,
    OutlookDia,
    pic,
    preavisos_actius,
)

if TYPE_CHECKING:
    from . import AvisoscatConfigEntry
    from .coordinator import AvisoscatDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


# Coordinator-driven, read-only platform: entities never poll or write, so no
# parallelism limit is needed. Declared explicitly for the silver
# `parallel_updates` quality-scale rule (docs/04-architecture.md §9).
PARALLEL_UPDATES = 0


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

    The seven aggregate sensors exist for every entry unconditionally. The ten
    per-meteor sensors (§3.5) are created only for the meteors the user selected
    in the options flow: when that selection changes HA reloads the entry, which
    is how this function runs again against the new list. The maritime sensor
    (§3.7) is the other option-dependent entity and lands in its own task.
    """
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NivellDAvisSensor(coordinator, entry),
        AvisosActiusSensor(coordinator, entry),
        AvisAnunciatSensor(coordinator, entry),
        GrauMaximSensor(coordinator, entry, DIA_AVUI),
        GrauMaximSensor(coordinator, entry, DIA_DEMA),
        GrauMaximSensor(coordinator, entry, DIA_DEMA_PASSAT),
        PreavisSensor(coordinator, entry),
    ]
    entities.extend(_meteor_sensors(coordinator, entry))
    async_add_entities(entities)


def _meteor_sensors(
    coordinator: AvisoscatDataUpdateCoordinator,
    entry: AvisoscatConfigEntry,
) -> list[MeteorSensor]:
    """Build the per-meteor sensors for the selected meteors (§3.5).

    The options store meteor *values* (the strings the `Meteor` enum maps to),
    never the enums. A value that is not a known meteor - a name the source sent
    that the parser did not recognise, or a literal left over from an old entry
    after the enum evolved - is skipped with a warning, never silently turned
    into a sensor for a generic meteor (docs/03-feature-spec.md §3.5, trap #5).
    Missing options default to "follow all ten", matching the config flow.
    """
    selected = entry.options.get(CONF_METEORS, DEFAULT_METEORS)
    sensors: list[MeteorSensor] = []
    for value in selected:
        meteor = _METEOR_BY_VALUE.get(value)
        if meteor is None:
            _LOGGER.warning(
                "Unknown meteor %r in the options of entry %s; "
                "not creating a per-meteor sensor for it",
                value,
                entry.entry_id,
            )
            continue
        sensors.append(MeteorSensor(coordinator, entry, meteor))
    return sensors


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
        """The day's peak grade and bands plus the peak's meteor and threshold."""
        dia = self._dia()
        if dia is None:
            return {}
        peak = dia.pic
        return {
            "perill": dia.perill_maxim,
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


# Meteor enum value → enum member, so the options list (plain strings) is
# resolved without a try/except and an unknown value reads as `None`.
_METEOR_BY_VALUE: Final[dict[str, Meteor]] = {meteor.value: meteor for meteor in Meteor}

# Meteor → translation key (docs/03-feature-spec.md §3.5). The translation key
# also becomes the entity name suffix and the second half of the unique id, so
# `avis_vent` is `warning_wind` everywhere: in the UI, in the entity registry,
# and in the dashboard the user writes against it.
_METEOR_TRANSLATION_KEYS: Final[dict[Meteor, str]] = {
    Meteor.VENT: "warning_wind",
    Meteor.PLUJA_30MIN: "warning_rain_30min",
    Meteor.PLUJA_3H: "warning_rain_3h",
    Meteor.PLUJA_ACUMULADA: "warning_rain_accumulated",
    Meteor.NEU: "warning_snow",
    Meteor.MAR: "warning_sea",
    Meteor.FRED: "warning_cold",
    Meteor.CALOR: "warning_heat",
    Meteor.CALOR_NOCTURNA: "warning_night_heat",
    Meteor.TEMPS_VIOLENT: "warning_violent_weather",
}


class MeteorSensor(_EnumSensor):
    """Grau de l'avís d'un meteor concret (§3.5).

    One instance per selected meteor. Restricts §3.1's aggregate picture to a
    single phenomenon: the state is that meteor's in-force traffic-light
    category (`cap` when no in-force warning of this meteor exists), and the
    `graus_per_periode` attribute exposes today's per-band max for this meteor
    alone, so a dashboard can paint a per-meteor bar of the day ahead without
    folding in unrelated phenomena.

    The peak attributes (`perill`, `nivell`, `llindar`, ...) are anchored to the
    in-force peak of this meteor and are absent when none is in force, mirroring
    `NivellDAvisSensor`. The `graus_per_periode` grid is independent of the
    in-force status: it carries the four bands of the current day for this
    meteor, including bands that are only announced later today.
    """

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
        meteor: Meteor,
    ) -> None:
        """Pin the meteor and the translation key that names this sensor."""
        translation_key = _METEOR_TRANSLATION_KEYS[meteor]
        super().__init__(coordinator, entry, translation_key)
        self._attr_translation_key = translation_key
        self._meteor = meteor

    def _peak(self) -> AfectacioProjectada | None:
        """The severest in-force projection for this meteor, `None` when none."""
        return pic(self._en_vigor_del_meteor())

    def _en_vigor_del_meteor(self) -> list[AfectacioProjectada]:
        """The in-force projections that belong to this sensor's meteor."""
        state = self.coordinator.data
        if state is None:
            return []
        return [af for af in state.en_vigor if af.meteor is self._meteor]

    def _avui(self) -> OutlookDia | None:
        """Today's outlook day, `None` before the first fetch."""
        state = self.coordinator.data
        if state is None:
            return None
        for dia in state.outlook:
            if dia.etiqueta == DIA_AVUI:
                return dia
        return None

    def _graus_per_periode(self) -> dict[str, int]:
        """Today's per-band max for this meteor only, all four bands present.

        A cell holds the highest grade of this meteor's affectations whose
        effective interval overlaps the band, which is the same overlap test the
        outlook uses (docs/03-feature-spec.md §3.4): a violent-weather window
        straddling two bands appears in both. Grade 0 fills the bands this
        meteor does not reach, so the grid never has a missing cell.
        """
        avui = self._avui()
        if avui is None:
            return {}
        return {
            periode.periode: max(
                (af.perill for af in periode.afectacions if af.meteor is self._meteor),
                default=0,
            )
            for periode in avui.periodes
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The §3.5 attribute table, anchored to this meteor's in-force peak."""
        attrs: dict[str, Any] = {}
        peak = self._peak()
        if peak is not None:
            attrs["perill"] = peak.perill
            attrs["nivell"] = peak.nivell
            attrs["llindar"] = peak.llindar
            attrs["periode"] = peak.periode
            attrs["distribucio_geografica"] = peak.distribucio_geografica
            attrs["comentari"] = peak.comentari
            attrs["data_inici"] = iso(peak.data_inici)
            attrs["data_fi"] = iso(peak.data_fi)
        # `graus_per_periode` is independent of the in-force peak: it paints
        # the day ahead for this meteor, announced bands included.
        graus = self._graus_per_periode()
        if graus:
            attrs["graus_per_periode"] = graus
        return attrs
