"""Append-only persistence for REV59-CODEPOLICY authorities.

Thirteen tables in one repository — seating circulation / egress /
accessibility evidence (#746), lighting temporal modulation (#748),
project data privacy & sharing (#722):

* ``cad_life_safety_profiles`` / ``cad_circulation_routes`` /
  ``cad_seating_accessibility_requirements`` /
  ``cad_egress_evidence_records`` / ``cad_professional_approval_refs``
* ``cad_dimming_temporal_profiles`` / ``cad_temporal_light_waveforms`` /
  ``cad_lighting_tlm_observations`` / ``cad_lighting_tla_assessments``
* ``cad_project_data_classifications`` /
  ``cad_sensitive_artifact_policies`` /
  ``cad_export_redaction_manifests`` / ``cad_retention_policy_records``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_life_safety import (
    CirculationRoute,
    EgressEvidence,
    ProfessionalApprovalReference,
    ProjectLifeSafetyProfile,
    SeatingAccessibilityRequirement,
)
from .cad_lighting_tlm import (
    DimmingTemporalProfile,
    LightingTLAAssessment,
    LightingTLMObservation,
    TemporalLightWaveform,
)
from .cad_project_data_privacy import (
    ExportRedactionManifest,
    ProjectDataClassification,
    RetentionPolicyRecord,
    SensitiveArtifactPolicy,
)


class CodePolicyConflictError(ValueError):
    """A code-policy save violated append-only identity rules."""


class CodePolicyIntegrityError(ValueError):
    """A stored code-policy row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CodePolicyIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CodePolicyIntegrityError(
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
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise CodePolicyConflictError(
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
            raise CodePolicyIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise CodePolicyIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise CodePolicyIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise CodePolicyIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCodePolicyRepository:
    """Native storage for the #746/#748/#722 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_life_safety_profiles',
                'cad_circulation_routes',
                'cad_seating_accessibility_requirements',
                'cad_egress_evidence_records',
                'cad_professional_approval_refs',
                'cad_dimming_temporal_profiles',
                'cad_temporal_light_waveforms',
                'cad_lighting_tlm_observations',
                'cad_lighting_tla_assessments',
                'cad_project_data_classifications',
                'cad_sensitive_artifact_policies',
                'cad_export_redaction_manifests',
                'cad_retention_policy_records',
            )
        self.life_safety_profiles = _SealedStore(
            self._connect, 'cad_life_safety_profiles',
            ProjectLifeSafetyProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('project_kind', 'project_kind'),
                ('applicability_decision', 'applicability_decision'),
            ),
        )
        self.circulation_routes = _SealedStore(
            self._connect, 'cad_circulation_routes',
            CirculationRoute, 'route_id', 'route_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('furniture_state', 'furniture_state'),
            ),
        )
        self.accessibility_requirements = _SealedStore(
            self._connect, 'cad_seating_accessibility_requirements',
            SeatingAccessibilityRequirement, 'requirement_id',
            'requirement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )
        self.egress_evidence = _SealedStore(
            self._connect, 'cad_egress_evidence_records',
            EgressEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('evidence_class', 'evidence_class'),
            ),
        )
        self.professional_approvals = _SealedStore(
            self._connect, 'cad_professional_approval_refs',
            ProfessionalApprovalReference, 'approval_id',
            'approval_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.dimming_profiles = _SealedStore(
            self._connect, 'cad_dimming_temporal_profiles',
            DimmingTemporalProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('luminaire_ref_id', 'luminaire_ref'),
            ),
        )
        self.light_waveforms = _SealedStore(
            self._connect, 'cad_temporal_light_waveforms',
            TemporalLightWaveform, 'waveform_id', 'waveform_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('illuminance_lx', 'illuminance_lx'),
            ),
        )
        self.tlm_observations = _SealedStore(
            self._connect, 'cad_lighting_tlm_observations',
            LightingTLMObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('waveform_ref_id', 'waveform_ref'),
                ('phenomenon', 'phenomenon'),
            ),
        )
        self.tla_assessments = _SealedStore(
            self._connect, 'cad_lighting_tla_assessments',
            LightingTLAAssessment, 'assessment_id', 'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                ('metric_id', 'metric_id'),
                ('verdict', 'verdict'),
            ),
        )
        self.data_classifications = _SealedStore(
            self._connect, 'cad_project_data_classifications',
            ProjectDataClassification, 'classification_id',
            'classification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
                ('data_class', 'data_class'),
            ),
        )
        self.artifact_policies = _SealedStore(
            self._connect, 'cad_sensitive_artifact_policies',
            SensitiveArtifactPolicy, 'policy_id', 'policy_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.export_manifests = _SealedStore(
            self._connect, 'cad_export_redaction_manifests',
            ExportRedactionManifest, 'manifest_id', 'manifest_sha256',
            (
                ('document_id', '__document_id__'),
                ('bundle_kind', 'bundle_kind'),
            ),
        )
        self.retention_records = _SealedStore(
            self._connect, 'cad_retention_policy_records',
            RetentionPolicyRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('artifact_ref_id', 'artifact_ref'),
                ('retention_class', 'retention_class'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # #746 — egress / circulation / accessibility
    def save_life_safety_profile(
            self, record: ProjectLifeSafetyProfile) -> None:
        self.life_safety_profiles.save(record)

    def get_life_safety_profile(
            self, rid: str) -> ProjectLifeSafetyProfile | None:
        return self.life_safety_profiles.get(rid)

    def save_circulation_route(self, record: CirculationRoute) -> None:
        self.circulation_routes.save(record)

    def get_circulation_route(self, rid: str) -> CirculationRoute | None:
        return self.circulation_routes.get(rid)

    def save_accessibility_requirement(
            self, record: SeatingAccessibilityRequirement) -> None:
        self.accessibility_requirements.save(record)

    def get_accessibility_requirement(
            self, rid: str) -> SeatingAccessibilityRequirement | None:
        return self.accessibility_requirements.get(rid)

    def save_egress_evidence(self, record: EgressEvidence) -> None:
        self.egress_evidence.save(record)

    def get_egress_evidence(self, rid: str) -> EgressEvidence | None:
        return self.egress_evidence.get(rid)

    def save_professional_approval(
            self, record: ProfessionalApprovalReference) -> None:
        self.professional_approvals.save(record)

    def get_professional_approval(
            self, rid: str) -> ProfessionalApprovalReference | None:
        return self.professional_approvals.get(rid)

    # #748 — lighting TLM / TLA
    def save_dimming_profile(self, record: DimmingTemporalProfile) -> None:
        self.dimming_profiles.save(record)

    def get_dimming_profile(
            self, rid: str) -> DimmingTemporalProfile | None:
        return self.dimming_profiles.get(rid)

    def save_light_waveform(self, record: TemporalLightWaveform) -> None:
        self.light_waveforms.save(record)

    def get_light_waveform(
            self, rid: str) -> TemporalLightWaveform | None:
        return self.light_waveforms.get(rid)

    def save_tlm_observation(self, record: LightingTLMObservation) -> None:
        self.tlm_observations.save(record)

    def get_tlm_observation(
            self, rid: str) -> LightingTLMObservation | None:
        return self.tlm_observations.get(rid)

    def save_tla_assessment(self, record: LightingTLAAssessment) -> None:
        self.tla_assessments.save(record)

    def get_tla_assessment(
            self, rid: str) -> LightingTLAAssessment | None:
        return self.tla_assessments.get(rid)

    # #722 — data privacy / sharing
    def save_data_classification(
            self, record: ProjectDataClassification) -> None:
        self.data_classifications.save(record)

    def get_data_classification(
            self, rid: str) -> ProjectDataClassification | None:
        return self.data_classifications.get(rid)

    def save_artifact_policy(
            self, record: SensitiveArtifactPolicy) -> None:
        self.artifact_policies.save(record)

    def get_artifact_policy(
            self, rid: str) -> SensitiveArtifactPolicy | None:
        return self.artifact_policies.get(rid)

    def save_export_manifest(
            self, record: ExportRedactionManifest) -> None:
        self.export_manifests.save(record)

    def get_export_manifest(
            self, rid: str) -> ExportRedactionManifest | None:
        return self.export_manifests.get(rid)

    def save_retention_record(
            self, record: RetentionPolicyRecord) -> None:
        self.retention_records.save(record)

    def get_retention_record(
            self, rid: str) -> RetentionPolicyRecord | None:
        return self.retention_records.get(rid)
