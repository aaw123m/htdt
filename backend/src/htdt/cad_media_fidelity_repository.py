"""Append-only persistence for REV59-VIDMETA authorities.

Ten tables in one repository — projector dynamic light (#759),
low-luminance metrology (#756), display acoustic boundary (#760),
codec/transcode fidelity (#753/#747):

* ``cad_projector_light_profiles`` / ``cad_temporal_contrast_measures`` /
  ``cad_dynamic_contrast_qualifications``
* ``cad_light_measurement_capabilities`` / ``cad_low_luminance_observations``
* ``cad_display_boundary_profiles`` / ``cad_front_stage_variants``
* ``cad_codec_chain_profiles`` / ``cad_quality_method_profiles`` /
  ``cad_codec_fidelity_observations``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_projector_dynamic_light import (
    DynamicContrastQualification,
    ProjectorDynamicLightProfile,
    TemporalContrastMeasurement,
)
from .cad_low_luminance import (
    DisplayLightMeasurementCapability,
    LowLuminanceObservation,
)
from .cad_display_acoustic_boundary import (
    DisplayAcousticBoundaryProfile,
    FrontStageVariantRecord,
)
from .cad_codec_fidelity import (
    CodecChainProfile,
    CodecFidelityObservation,
    QualityMethodProfile,
)


class MediaFidelityConflictError(ValueError):
    """A media-fidelity save violated append-only identity rules."""


class MediaFidelityIntegrityError(ValueError):
    """A stored media-fidelity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise MediaFidelityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MediaFidelityIntegrityError(
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
            if value is None:
                return None
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
            raise MediaFidelityConflictError(
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
            raise MediaFidelityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise MediaFidelityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise MediaFidelityIntegrityError(
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


class CadMediaFidelityRepository:
    """Native storage for the #759/#756/#760/#753/#747 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_projector_light_profiles',
                'cad_temporal_contrast_measures',
                'cad_dynamic_contrast_qualifications',
                'cad_light_measurement_capabilities',
                'cad_low_luminance_observations',
                'cad_display_boundary_profiles',
                'cad_front_stage_variants',
                'cad_codec_chain_profiles',
                'cad_quality_method_profiles',
                'cad_codec_fidelity_observations',
            )
        self.light_profiles = _SealedStore(
            self._connect, 'cad_projector_light_profiles',
            ProjectorDynamicLightProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.contrast_measures = _SealedStore(
            self._connect, 'cad_temporal_contrast_measures',
            TemporalContrastMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                ('measurand', 'measurand'),
                ('light_mode', 'light_mode'),
            ),
        )
        self.contrast_qualifications = _SealedStore(
            self._connect, 'cad_dynamic_contrast_qualifications',
            DynamicContrastQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('measurement_ref_id', 'measurement_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.light_capabilities = _SealedStore(
            self._connect, 'cad_light_measurement_capabilities',
            DisplayLightMeasurementCapability, 'capability_id',
            'capability_sha256',
            (
                ('document_id', '__document_id__'),
                ('stray_light_control', 'stray_light_control'),
            ),
        )
        self.luminance_observations = _SealedStore(
            self._connect, 'cad_low_luminance_observations',
            LowLuminanceObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('capability_ref_id', 'capability_ref'),
            ),
        )
        self.boundary_profiles = _SealedStore(
            self._connect, 'cad_display_boundary_profiles',
            DisplayAcousticBoundaryProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('transmission', 'transmission'),
            ),
        )
        self.front_stage_variants = _SealedStore(
            self._connect, 'cad_front_stage_variants',
            FrontStageVariantRecord, 'variant_id', 'variant_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('boundary_ref_id', 'boundary_ref'),
                ('strategy', 'strategy'),
                ('verdict', 'verdict'),
            ),
        )
        self.codec_chains = _SealedStore(
            self._connect, 'cad_codec_chain_profiles',
            CodecChainProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('media_kind', 'media_kind'),
            ),
        )
        self.quality_methods = _SealedStore(
            self._connect, 'cad_quality_method_profiles',
            QualityMethodProfile, 'method_id', 'method_sha256',
            (
                ('document_id', '__document_id__'),
                ('method_kind', 'method_kind'),
            ),
        )
        self.fidelity_observations = _SealedStore(
            self._connect, 'cad_codec_fidelity_observations',
            CodecFidelityObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('chain_ref_id', 'chain_ref'),
                _ref('method_ref_id', 'method_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_light_profile(
        self, record: ProjectorDynamicLightProfile
    ) -> None:
        self.light_profiles.save(record)

    def get_light_profile(
        self, profile_id: str
    ) -> ProjectorDynamicLightProfile | None:
        return self.light_profiles.get(profile_id)

    def save_contrast_measure(
        self, record: TemporalContrastMeasurement
    ) -> None:
        self.contrast_measures.save(record)

    def get_contrast_measure(
        self, measurement_id: str
    ) -> TemporalContrastMeasurement | None:
        return self.contrast_measures.get(measurement_id)

    def save_contrast_qualification(
        self, record: DynamicContrastQualification
    ) -> None:
        self.contrast_qualifications.save(record)

    def get_contrast_qualification(
        self, qualification_id: str
    ) -> DynamicContrastQualification | None:
        return self.contrast_qualifications.get(qualification_id)

    def save_light_capability(
        self, record: DisplayLightMeasurementCapability
    ) -> None:
        self.light_capabilities.save(record)

    def get_light_capability(
        self, capability_id: str
    ) -> DisplayLightMeasurementCapability | None:
        return self.light_capabilities.get(capability_id)

    def save_luminance_observation(
        self, record: LowLuminanceObservation
    ) -> None:
        self.luminance_observations.save(record)

    def get_luminance_observation(
        self, observation_id: str
    ) -> LowLuminanceObservation | None:
        return self.luminance_observations.get(observation_id)

    def save_boundary_profile(
        self, record: DisplayAcousticBoundaryProfile
    ) -> None:
        self.boundary_profiles.save(record)

    def get_boundary_profile(
        self, profile_id: str
    ) -> DisplayAcousticBoundaryProfile | None:
        return self.boundary_profiles.get(profile_id)

    def save_front_stage_variant(
        self, record: FrontStageVariantRecord
    ) -> None:
        self.front_stage_variants.save(record)

    def get_front_stage_variant(
        self, variant_id: str
    ) -> FrontStageVariantRecord | None:
        return self.front_stage_variants.get(variant_id)

    def save_codec_chain(self, record: CodecChainProfile) -> None:
        self.codec_chains.save(record)

    def get_codec_chain(
        self, profile_id: str
    ) -> CodecChainProfile | None:
        return self.codec_chains.get(profile_id)

    def save_quality_method(
        self, record: QualityMethodProfile
    ) -> None:
        self.quality_methods.save(record)

    def get_quality_method(
        self, method_id: str
    ) -> QualityMethodProfile | None:
        return self.quality_methods.get(method_id)

    def save_fidelity_observation(
        self, record: CodecFidelityObservation
    ) -> None:
        self.fidelity_observations.save(record)

    def get_fidelity_observation(
        self, observation_id: str
    ) -> CodecFidelityObservation | None:
        return self.fidelity_observations.get(observation_id)
