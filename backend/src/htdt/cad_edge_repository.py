"""Append-only persistence for REV60-EDGE authorities.

Sixteen tables in one repository — sub-20 Hz / infrasonic acoustics
(#779), external noise ingress / façade isolation (#781), material
fire-safety evidence (#782), accessible media playback (#783):

* ``cad_ulf_acoustic_profiles`` /
  ``cad_infrasonic_measurement_capabilities`` /
  ``cad_ulf_acoustic_observations`` / ``cad_ulf_system_qualifications``
* ``cad_external_noise_ingress_scenarios`` /
  ``cad_facade_transmission_models`` /
  ``cad_external_noise_ingress_measurements`` /
  ``cad_indoor_noise_ingress_qualifications``
* ``cad_fire_safety_evidence_profiles`` /
  ``cad_material_reaction_to_fire_evidence`` /
  ``cad_installed_material_safety_requirements`` /
  ``cad_fire_safety_approval_refs``
* ``cad_accessible_media_profiles`` /
  ``cad_caption_presentation_observations`` /
  ``cad_audio_description_playback_observations`` /
  ``cad_accessible_playback_qualifications``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_ulf_acoustics import (
    InfrasonicMeasurementCapability,
    ULFAcousticObservation,
    ULFSystemQualification,
    UltraLowFrequencyAcousticProfile,
)
from .cad_noise_ingress import (
    ExternalNoiseIngressMeasurement,
    ExternalNoiseIngressScenario,
    FacadeTransmissionModel,
    IndoorNoiseIngressQualification,
)
from .cad_material_fire_safety import (
    FireSafetyApprovalReference,
    FireSafetyEvidenceProfile,
    InstalledMaterialSafetyRequirement,
    MaterialReactionToFireEvidence,
)
from .cad_accessible_media import (
    AccessibleMediaProfile,
    AccessiblePlaybackQualification,
    AudioDescriptionPlaybackObservation,
    CaptionPresentationObservation,
)


class EdgeConflictError(ValueError):
    """An edge-authority save violated append-only identity rules."""


class EdgeIntegrityError(ValueError):
    """A stored edge-authority row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise EdgeIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise EdgeIntegrityError(
            'record id does not match its sealed sha256')


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
                    record, self.sha_field):
                return
            raise EdgeConflictError(
                f'{self.table} records are append-only')
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(self._column_value(record, path) for _, path in self.columns),
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
            raise EdgeIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise EdgeIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise EdgeIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
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


class CadEdgeAuthorityRepository:
    """Native storage for the #779/#781/#782/#783 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_ulf_acoustic_profiles',
                'cad_infrasonic_measurement_capabilities',
                'cad_ulf_acoustic_observations',
                'cad_ulf_system_qualifications',
                'cad_external_noise_ingress_scenarios',
                'cad_facade_transmission_models',
                'cad_external_noise_ingress_measurements',
                'cad_indoor_noise_ingress_qualifications',
                'cad_fire_safety_evidence_profiles',
                'cad_material_reaction_to_fire_evidence',
                'cad_installed_material_safety_requirements',
                'cad_fire_safety_approval_refs',
                'cad_accessible_media_profiles',
                'cad_caption_presentation_observations',
                'cad_audio_description_playback_observations',
                'cad_accessible_playback_qualifications',
            )
        self.ulf_profiles = _SealedStore(
            self._connect, 'cad_ulf_acoustic_profiles',
            UltraLowFrequencyAcousticProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('request_low_hz', 'request_low_hz'),
                ('request_high_hz', 'request_high_hz'),
            ),
        )
        self.ulf_capabilities = _SealedStore(
            self._connect,
            'cad_infrasonic_measurement_capabilities',
            InfrasonicMeasurementCapability, 'capability_id',
            'capability_sha256',
            (
                ('document_id', '__document_id__'),
                ('capability_state', 'capability_state'),
            ),
        )
        self.ulf_observations = _SealedStore(
            self._connect, 'cad_ulf_acoustic_observations',
            ULFAcousticObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                _ref('capability_ref_id', 'capability_ref'),
                ('quantity_kind', 'quantity_kind'),
            ),
        )
        self.ulf_qualifications = _SealedStore(
            self._connect, 'cad_ulf_system_qualifications',
            ULFSystemQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('qualification_state', 'qualification_state'),
            ),
        )
        self.ingress_scenarios = _SealedStore(
            self._connect, 'cad_external_noise_ingress_scenarios',
            ExternalNoiseIngressScenario, 'scenario_id',
            'scenario_sha256',
            (
                ('document_id', '__document_id__'),
                ('source_kind', 'source_kind'),
            ),
        )
        self.facade_models = _SealedStore(
            self._connect, 'cad_facade_transmission_models',
            FacadeTransmissionModel, 'model_id', 'model_sha256',
            (
                ('document_id', '__document_id__'),
                ('envelope_state', 'envelope_state'),
            ),
        )
        self.ingress_measurements = _SealedStore(
            self._connect,
            'cad_external_noise_ingress_measurements',
            ExternalNoiseIngressMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('scenario_ref_id', 'scenario_ref'),
                _ref('model_ref_id', 'model_ref'),
                ('measurement_class', 'measurement_class'),
                ('domain_state', 'domain_state'),
            ),
        )
        self.ingress_qualifications = _SealedStore(
            self._connect,
            'cad_indoor_noise_ingress_qualifications',
            IndoorNoiseIngressQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('scenario_ref_id', 'scenario_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.fire_safety_profiles = _SealedStore(
            self._connect, 'cad_fire_safety_evidence_profiles',
            FireSafetyEvidenceProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('project_class', 'project_class'),
                ('approval_status', 'approval_status'),
            ),
        )
        self.fire_evidence = _SealedStore(
            self._connect, 'cad_material_reaction_to_fire_evidence',
            MaterialReactionToFireEvidence, 'evidence_id',
            'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('evidence_kind', 'evidence_kind'),
                ('specimen_applicability', 'specimen_applicability'),
            ),
        )
        self.material_requirements = _SealedStore(
            self._connect,
            'cad_installed_material_safety_requirements',
            InstalledMaterialSafetyRequirement, 'requirement_id',
            'requirement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('state', 'state'),
            ),
        )
        self.fire_safety_approvals = _SealedStore(
            self._connect, 'cad_fire_safety_approval_refs',
            FireSafetyApprovalReference, 'approval_id',
            'approval_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('requirement_ref_id', 'requirement_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.accessible_media_profiles = _SealedStore(
            self._connect, 'cad_accessible_media_profiles',
            AccessibleMediaProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('profile_source', 'profile_source'),
            ),
        )
        self.caption_observations = _SealedStore(
            self._connect, 'cad_caption_presentation_observations',
            CaptionPresentationObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('component_kind', 'component_kind'),
                ('reached_stage', 'reached_stage'),
                ('readability_state', 'readability_state'),
            ),
        )
        self.ad_observations = _SealedStore(
            self._connect,
            'cad_audio_description_playback_observations',
            AudioDescriptionPlaybackObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('mix_semantics', 'mix_semantics'),
                ('output_state', 'output_state'),
            ),
        )
        self.accessible_qualifications = _SealedStore(
            self._connect, 'cad_accessible_playback_qualifications',
            AccessiblePlaybackQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # #779 — sub-20 Hz / infrasonic acoustics
    def save_ulf_profile(
            self, record: UltraLowFrequencyAcousticProfile) -> None:
        self.ulf_profiles.save(record)

    def get_ulf_profile(
            self, rid: str
    ) -> UltraLowFrequencyAcousticProfile | None:
        return self.ulf_profiles.get(rid)

    def save_infrasonic_capability(
            self, record: InfrasonicMeasurementCapability) -> None:
        self.ulf_capabilities.save(record)

    def get_infrasonic_capability(
            self, rid: str
    ) -> InfrasonicMeasurementCapability | None:
        return self.ulf_capabilities.get(rid)

    def save_ulf_observation(
            self, record: ULFAcousticObservation) -> None:
        self.ulf_observations.save(record)

    def get_ulf_observation(
            self, rid: str) -> ULFAcousticObservation | None:
        return self.ulf_observations.get(rid)

    def save_ulf_qualification(
            self, record: ULFSystemQualification) -> None:
        self.ulf_qualifications.save(record)

    def get_ulf_qualification(
            self, rid: str) -> ULFSystemQualification | None:
        return self.ulf_qualifications.get(rid)

    # #781 — external noise ingress / façade isolation
    def save_ingress_scenario(
            self, record: ExternalNoiseIngressScenario) -> None:
        self.ingress_scenarios.save(record)

    def get_ingress_scenario(
            self, rid: str) -> ExternalNoiseIngressScenario | None:
        return self.ingress_scenarios.get(rid)

    def save_facade_model(self, record: FacadeTransmissionModel) -> None:
        self.facade_models.save(record)

    def get_facade_model(
            self, rid: str) -> FacadeTransmissionModel | None:
        return self.facade_models.get(rid)

    def save_ingress_measurement(
            self, record: ExternalNoiseIngressMeasurement) -> None:
        self.ingress_measurements.save(record)

    def get_ingress_measurement(
            self, rid: str) -> ExternalNoiseIngressMeasurement | None:
        return self.ingress_measurements.get(rid)

    def save_ingress_qualification(
            self, record: IndoorNoiseIngressQualification) -> None:
        self.ingress_qualifications.save(record)

    def get_ingress_qualification(
            self, rid: str) -> IndoorNoiseIngressQualification | None:
        return self.ingress_qualifications.get(rid)

    # #782 — material fire-safety evidence
    def save_fire_safety_profile(
            self, record: FireSafetyEvidenceProfile) -> None:
        self.fire_safety_profiles.save(record)

    def get_fire_safety_profile(
            self, rid: str) -> FireSafetyEvidenceProfile | None:
        return self.fire_safety_profiles.get(rid)

    def save_fire_evidence(
            self, record: MaterialReactionToFireEvidence) -> None:
        self.fire_evidence.save(record)

    def get_fire_evidence(
            self, rid: str) -> MaterialReactionToFireEvidence | None:
        return self.fire_evidence.get(rid)

    def save_material_requirement(
            self, record: InstalledMaterialSafetyRequirement) -> None:
        self.material_requirements.save(record)

    def get_material_requirement(
            self, rid: str
    ) -> InstalledMaterialSafetyRequirement | None:
        return self.material_requirements.get(rid)

    def save_fire_safety_approval(
            self, record: FireSafetyApprovalReference) -> None:
        self.fire_safety_approvals.save(record)

    def get_fire_safety_approval(
            self, rid: str) -> FireSafetyApprovalReference | None:
        return self.fire_safety_approvals.get(rid)

    # #783 — accessible media playback
    def save_accessible_media_profile(
            self, record: AccessibleMediaProfile) -> None:
        self.accessible_media_profiles.save(record)

    def get_accessible_media_profile(
            self, rid: str) -> AccessibleMediaProfile | None:
        return self.accessible_media_profiles.get(rid)

    def save_caption_observation(
            self, record: CaptionPresentationObservation) -> None:
        self.caption_observations.save(record)

    def get_caption_observation(
            self, rid: str) -> CaptionPresentationObservation | None:
        return self.caption_observations.get(rid)

    def save_ad_observation(
            self, record: AudioDescriptionPlaybackObservation) -> None:
        self.ad_observations.save(record)

    def get_ad_observation(
            self, rid: str
    ) -> AudioDescriptionPlaybackObservation | None:
        return self.ad_observations.get(rid)

    def save_accessible_qualification(
            self, record: AccessiblePlaybackQualification) -> None:
        self.accessible_qualifications.save(record)

    def get_accessible_qualification(
            self, rid: str) -> AccessiblePlaybackQualification | None:
        return self.accessible_qualifications.get(rid)
