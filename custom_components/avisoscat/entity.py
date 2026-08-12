"""Base entity shared by every avisoscat platform.

Every entity belongs to the per-comarca device declared in
docs/04-architecture.md §9 and docs/03-feature-spec.md §3, so the device info,
the attribution and the unique-id scheme live here once. Subclasses pass a
`key` that becomes the second half of the unique id and contributes the entity
name through `_attr_translation_key`.

The comarca name in the device name is resolved here from the entry data, so
the entity layer never reaches into the coordinator's private state.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .comarques import nom
from .const import ATTRIBUTION, CONF_ID_COMARCA, DOMAIN

if TYPE_CHECKING:
    from . import AvisoscatConfigEntry
    from .coordinator import AvisoscatDataUpdateCoordinator


def iso(value: datetime | None) -> str | None:
    """ISO 8601 of a timestamp, `None` preserved for the absent case.

    External text and datetimes the source omits reach the entities as `None`;
    rendering them as the literal string `"None"` would be a leak, so they are
    kept as `None` and Home Assistant drops the attribute.
    """
    return value.isoformat() if value is not None else None


class AvisoscatEntity(CoordinatorEntity["AvisoscatDataUpdateCoordinator"]):
    """Common base: per-comarca device info, attribution, unique id.

    `_attr_has_entity_name = True` makes the entity name come from the
    translation key, so the device name carries the comarca once and the
    per-platform translation carries the rest (docs/04-architecture.md §9).
    """

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Whether the entity has a last-good state to show.

        The coordinator preserves the last good projections on fetch failure
        (`coordinator.py` keeps `self.data` when `_async_update_data` raises),
        so the entities stay readable while the source is down: a transient
        timeout or `UpdateFailed` does not flip the sensors to `unavailable`
        and wipe the level the user was watching. `last_update_success`
        becomes false but the data is still there, and docs/04-architecture.md
        §10 designates `service_connected` (a later diagnostic entity) as the
        signal for source failure, not the entities going unavailable.
        """
        return self.coordinator.data is not None

    def __init__(
        self,
        coordinator: AvisoscatDataUpdateCoordinator,
        entry: AvisoscatConfigEntry,
        key: str,
    ) -> None:
        """Wire the entity to its device and unique id."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        comarca_nom = nom(int(entry.data[CONF_ID_COMARCA]))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Avisos Meteocat — {comarca_nom}",
            manufacturer="Servei Meteorològic de Catalunya",
            model="Situació Meteorològica de Perill",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.meteo.cat/",
        )
