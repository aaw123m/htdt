"""Append-only persistence for REV59-DISPLAY3 authorities.

Seven tables — displayed gradation (#660), colour volume (#688),
spatial resolution (#672), low-luminance capability (#756), dynamic
contrast (#759), display-wall boundary + impact (#760).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_display_fidelity import (
    ColourVolumeMeasurement,
    DisplayedGradationObservation,
    SpatialResolutionEvidence,
)
from .cad_display_metrology import (
    DynamicContrastMeasurement,
    LowLuminanceCapability,
)
from .cad_display_boundary import (
    DisplayWallBoundary,
    WallAcousticImpact,
)


class DisplayAuthorityConflictError(ValueError):
    """A DISPLAY3 save violated append-only identity rules."""


class DisplayAuthorityIntegrityError(ValueError):
    """A stored DISPLAY3 row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DisplayAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DisplayAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise DisplayAuthorityConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise DisplayAuthorityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise DisplayAuthorityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise DisplayAuthorityIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadDisplayAuthorityRepository:
    """Native storage for the #660/#688/#672/#756/#759/#760
    authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_displayed_gradation_observations',
                'cad_colour_volume_measurements',
                'cad_spatial_resolution_evidence',
                'cad_low_luminance_capabilities',
                'cad_dynamic_contrast_measurements',
                'cad_display_wall_boundaries',
                'cad_wall_acoustic_impacts',
            )

        self.gradation_observations = _SealedStore(
            self._connect, 'cad_displayed_gradation_observations',
            DisplayedGradationObservation, 'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('range_semantics', 'range_semantics'),
                ('banding_observed', 'banding_observed'),
            ),
        )

        self.colour_volumes = _SealedStore(
            self._connect, 'cad_colour_volume_measurements',
            ColourVolumeMeasurement, 'volume_id', 'volume_sha256',
            (
                ('document_id', '__document_id__'),
                ('colour_space', 'colour_space'),
                ('method', 'method'),
            ),
        )

        self.resolution_evidence = _SealedStore(
            self._connect, 'cad_spatial_resolution_evidence',
            SpatialResolutionEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
            ),
        )

        self.luminance_capabilitys = _SealedStore(
            self._connect, 'cad_low_luminance_capabilities',
            LowLuminanceCapability, 'capability_id', 'capability_sha256',
            (
                ('document_id', '__document_id__'),
                ('stray_light_control', 'stray_light_control'),
            ),
        )

        self.contrast_measurements = _SealedStore(
            self._connect, 'cad_dynamic_contrast_measurements',
            DynamicContrastMeasurement, 'measurement_id', 'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                ('contrast_kind', 'contrast_kind'),
            ),
        )

        self.wall_boundarys = _SealedStore(
            self._connect, 'cad_display_wall_boundaries',
            DisplayWallBoundary, 'boundary_id', 'boundary_sha256',
            (
                ('document_id', '__document_id__'),
                ('wall_kind', 'wall_kind'),
                ('acoustic_transparency_claim', 'acoustic_transparency_claim'),
            ),
        )

        self.wall_impacts = _SealedStore(
            self._connect, 'cad_wall_acoustic_impacts',
            WallAcousticImpact, 'impact_id', 'impact_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('boundary_ref_id', 'boundary_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_gradation_observation(self, record: DisplayedGradationObservation) -> None:
        self.gradation_observations.save(record)

    def get_gradation_observation(self, rid: str) -> DisplayedGradationObservation | None:
        return self.gradation_observations.get(rid)

    def save_colour_volume(self, record: ColourVolumeMeasurement) -> None:
        self.colour_volumes.save(record)

    def get_colour_volume(self, rid: str) -> ColourVolumeMeasurement | None:
        return self.colour_volumes.get(rid)

    def save_resolution_evidence(self, record: SpatialResolutionEvidence) -> None:
        self.resolution_evidence.save(record)

    def get_resolution_evidence(self, rid: str) -> SpatialResolutionEvidence | None:
        return self.resolution_evidence.get(rid)

    def save_luminance_capability(self, record: LowLuminanceCapability) -> None:
        self.luminance_capabilitys.save(record)

    def get_luminance_capability(self, rid: str) -> LowLuminanceCapability | None:
        return self.luminance_capabilitys.get(rid)

    def save_contrast_measurement(self, record: DynamicContrastMeasurement) -> None:
        self.contrast_measurements.save(record)

    def get_contrast_measurement(self, rid: str) -> DynamicContrastMeasurement | None:
        return self.contrast_measurements.get(rid)

    def save_wall_boundary(self, record: DisplayWallBoundary) -> None:
        self.wall_boundarys.save(record)

    def get_wall_boundary(self, rid: str) -> DisplayWallBoundary | None:
        return self.wall_boundarys.get(rid)

    def save_wall_impact(self, record: WallAcousticImpact) -> None:
        self.wall_impacts.save(record)

    def get_wall_impact(self, rid: str) -> WallAcousticImpact | None:
        return self.wall_impacts.get(rid)
