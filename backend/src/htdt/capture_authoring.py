"""Capture semantic handoff — provenance-preserving authoring inputs (#336).

Imported capture annotations, measurements, and RoomPlan records are
exposed to HTDT authoring as *typed inputs* grouped by ingestion
lineage. Nothing in this module mutates a scene: the operator reviews a
typed record, and only an explicit apply call records provenance —
capture IDs, payload hashes, and coordinate-space alignment — against
the SceneRevision it informed.

Contract points enforced here:

- Coordinate space: every record's ``coordinate_space_id`` must resolve
  inside the ingestion's declared ``coordinate_space_ids``; a record
  outside the bundle's spaces is surfaced as a conflict, never entered.
- Equipment: an annotation's ``equipment_ref`` is only a *candidate*.
  It is resolved against the versioned equipment authority
  (``CadEquipmentRepository.get_definition_by_hash``); an id/version
  disagreement or an unknown hash is a conflict, never an auto-link.
- Measurements reconcile: ``reconcile_measurement`` compares the
  imported value against an existing quantity and reports
  ``consistent``/``conflict``/``new`` — imported measurements never
  overwrite existing values.
- RoomPlan records are suggestions only — they carry
  ``suggestion_only=True`` and there is no apply path for them.
- Display/screen entity types promote to their exact HTDT kinds per
  ``cad_direct_view.CAPTURE_ENTITY_TYPE_TO_SCENE_KIND``: Capture
  ``display`` -> ``display``, ``projection_screen`` -> ``screen`` —
  never a display→screen downgrade (issue #637).
- Internal lineage refs (``evidence_refs``, ``endpoint_refs``,
  ``placement.source_evidence_refs``) arrive already resolved to
  bundle-internal evidence by the transaction layer and are preserved
  verbatim in the emitted authoring lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from htdt.capture_ingestion_transaction import (
    CaptureAuthorityRecord,
    CaptureIngestionRepository,
    CaptureRoomPlanRecord,
)


class CaptureAuthoringError(ValueError):
    """Raised when an authoring handoff cannot be constructed/applied."""


#: Capture entity types that have no HTDT scene mapping yet. They stay
#: typed and importable, but applying them raises — no silent mapping.
#: Since #637 landed the ``display`` scene kind, ``display`` and
#: ``projection_screen`` are no longer blocked here; the deterministic
#: promotion map lives in ``cad_direct_view.CAPTURE_ENTITY_TYPE_TO_SCENE_KIND``
#: (``display`` -> ``display``, ``projection_screen`` -> ``screen``).
UNSUPPORTED_ENTITY_TYPES = frozenset()


@dataclass(frozen=True)
class CaptureAuthoringConflict:
    """A non-fatal discrepancy surfaced to the operator."""

    kind: Literal[
        'unresolved_reference',
        'coordinate_space_mismatch',
        'equipment_ref_unresolved',
        'equipment_ref_mismatch',
        'measurement_conflict',
        'unsupported_entity_type',
    ]
    detail: str


@dataclass(frozen=True)
class CaptureEquipmentRefCandidate:
    """An annotation's ``equipment_ref`` evaluated against the authority."""

    equipment_id: str
    equipment_version: str
    equipment_hash: str
    status: Literal['verified', 'mismatch', 'unresolved']
    resolved_definition_version: str | None


@dataclass(frozen=True)
class CaptureAuthoringAnnotation:
    """One imported entity annotation as an authoring input."""

    record_id: str
    entity_type: str
    label: str | None
    coordinate_space_id: str
    authority_record_handoff_id: str
    source_payload_sha256: str
    document: dict
    evidence_refs: tuple[str, ...]
    endpoint_refs: tuple[str, ...]
    resolved_evidence: tuple[dict, ...]
    resolved_endpoints: tuple[dict, ...]
    equipment_ref: CaptureEquipmentRefCandidate | None
    suggestion_only: bool
    conflicts: tuple[CaptureAuthoringConflict, ...]


@dataclass(frozen=True)
class CaptureAuthoringMeasurement:
    """One imported measurement as an authoring input."""

    record_id: str
    quantity_type: str
    value: float
    unit: str
    coordinate_space_id: str | None
    authority_record_handoff_id: str
    source_payload_sha256: str
    document: dict
    evidence_refs: tuple[str, ...]
    endpoint_refs: tuple[str, ...]
    resolved_evidence: tuple[dict, ...]
    resolved_endpoints: tuple[dict, ...]
    conflicts: tuple[CaptureAuthoringConflict, ...]


@dataclass(frozen=True)
class CaptureAuthoringRoomPlan:
    """One imported RoomPlan record — suggestion only, never auto-applied."""

    kind: Literal['raw_scan', 'postprocessed_inference']
    source_evidence_id: str
    path: str
    payload_sha256: str
    provenance_class: str
    suggestion_only: bool = True


@dataclass(frozen=True)
class CaptureAuthoringBatch:
    """The authoring inputs of one ingestion, grouped by lineage/revision."""

    lineage_digest: str
    bundle_digest: str
    capture_revision_id: str
    capture_series_id: str
    annotations: tuple[CaptureAuthoringAnnotation, ...]
    measurements: tuple[CaptureAuthoringMeasurement, ...]
    roomplan_suggestions: tuple[CaptureAuthoringRoomPlan, ...]
    conflicts: tuple[CaptureAuthoringConflict, ...]


@dataclass(frozen=True)
class CaptureAuthoringProvenance:
    """The provenance stamp an apply call leaves on a SceneRevision."""

    capture_lineage_digest: str
    bundle_digest: str
    capture_revision_id: str
    record_kind: str
    record_id: str
    authority_record_handoff_id: str | None
    source_payload_sha256: str
    coordinate_space_id: str | None
    applied_to_scene_revision_id: str
    operator_action: str
    resolved_refs: tuple[dict, ...]

    def summary(self) -> str:
        return (
            f'capture:{self.record_kind}:{self.record_id} '
            f'handoff={self.authority_record_handoff_id} '
            f'sha256={self.source_payload_sha256} '
            f'space={self.coordinate_space_id} '
            f'revision={self.capture_revision_id} '
            f'action={self.operator_action}'
        )


class CaptureAuthoringService:
    """Builds and applies capture authoring inputs over the transaction."""

    def __init__(
        self,
        capture_repository: CaptureIngestionRepository,
        equipment_repository=None,
    ) -> None:
        self.capture_repository = capture_repository
        self.equipment_repository = equipment_repository

    def authoring_inputs(
        self,
        lineage_digest: str,
    ) -> CaptureAuthoringBatch:
        """Return the typed authoring inputs for one ingestion lineage."""
        plan = self.capture_repository.get_ingestion(lineage_digest)
        if plan is None:
            raise CaptureAuthoringError(
                f'capture ingestion not found: {lineage_digest}'
            )
        bundle_spaces = set(plan.bundle.coordinate_space_ids)
        bundle_evidence_ids = {
            record.source_evidence_id for record in plan.source_evidence
        }

        annotations: list[CaptureAuthoringAnnotation] = []
        measurements: list[CaptureAuthoringMeasurement] = []
        batch_conflicts: list[CaptureAuthoringConflict] = []
        for record in self.capture_repository.authority_records_for_ingestion(
            lineage_digest
        ):
            document = self._record_document(record)
            conflicts = self._reference_conflicts(
                record, bundle_evidence_ids
            )
            if (
                record.coordinate_space_id is not None
                and record.coordinate_space_id not in bundle_spaces
            ):
                conflicts.append(
                    CaptureAuthoringConflict(
                        kind='coordinate_space_mismatch',
                        detail=(
                            f'coordinate_space_id '
                            f'{record.coordinate_space_id} is not declared '
                            f'by the bundle'
                        ),
                    )
                )
            if record.record_kind == 'annotation':
                annotation = self._annotation(
                    record, document, tuple(conflicts)
                )
                conflicts = list(annotation.conflicts)
                annotations.append(annotation)
            else:
                measurements.append(
                    self._measurement(record, document, tuple(conflicts))
                )

        roomplan_suggestions = tuple(
            CaptureAuthoringRoomPlan(
                kind=record.kind,
                source_evidence_id=record.source_evidence_id,
                path=record.path,
                payload_sha256=record.payload_sha256,
                provenance_class=record.provenance_class,
            )
            for record in self.capture_repository.roomplan_records_for_ingestion(
                lineage_digest
            )
        )
        return CaptureAuthoringBatch(
            lineage_digest=lineage_digest,
            bundle_digest=plan.bundle.bundle_digest,
            capture_revision_id=plan.bundle.capture_revision_id,
            capture_series_id=plan.bundle.capture_series_id,
            annotations=tuple(annotations),
            measurements=tuple(measurements),
            roomplan_suggestions=roomplan_suggestions,
            conflicts=tuple(batch_conflicts),
        )

    def _record_document(self, record: CaptureAuthorityRecord) -> dict:
        """Extract the typed record from its source evidence payload."""
        evidence = self.capture_repository.get_source_evidence(
            record.source_evidence_id
        )
        if evidence is None:
            raise CaptureAuthoringError(
                f'source evidence missing for {record.record_locator}'
            )
        if record.provenance_class == 'user_annotation':
            collection_key, id_key = 'entities', 'entity_id'
        else:
            collection_key, id_key = 'measurements', 'measurement_id'
        document = json.loads(evidence.payload)
        for item in document.get(collection_key, ()):
            if item.get(id_key) == record.record_id:
                return item
        raise CaptureAuthoringError(
            f'record not resolvable from evidence: {record.record_locator}'
        )

    def _reference_conflicts(
        self,
        record: CaptureAuthorityRecord,
        bundle_evidence_ids: set[str],
    ) -> list[CaptureAuthoringConflict]:
        conflicts: list[CaptureAuthoringConflict] = []
        if record.source_evidence_id not in bundle_evidence_ids:
            conflicts.append(
                CaptureAuthoringConflict(
                    kind='unresolved_reference',
                    detail=(
                        f'source_evidence_id {record.source_evidence_id} '
                        f'not in ingestion evidence set'
                    ),
                )
            )
        return conflicts

    def _annotation(
        self,
        record: CaptureAuthorityRecord,
        document: dict,
        conflicts: tuple[CaptureAuthoringConflict, ...],
    ) -> CaptureAuthoringAnnotation:
        conflicts = list(conflicts)
        entity_type = str(document.get('type', ''))
        unsupported = entity_type in UNSUPPORTED_ENTITY_TYPES
        if unsupported:
            conflicts.append(
                CaptureAuthoringConflict(
                    kind='unsupported_entity_type',
                    detail=(
                        f'entity type {entity_type!r} has no HTDT scene '
                        'kind mapping'
                    ),
                )
            )
        equipment_ref = self._equipment_ref(document.get('equipment_ref'))
        if equipment_ref is not None and equipment_ref.status != 'verified':
            conflicts.append(
                CaptureAuthoringConflict(
                    kind=(
                        'equipment_ref_unresolved'
                        if equipment_ref.status == 'unresolved'
                        else 'equipment_ref_mismatch'
                    ),
                    detail=(
                        f'equipment_ref {equipment_ref.equipment_id}@'
                        f'{equipment_ref.equipment_version} '
                        f'{equipment_ref.status} against the versioned '
                        f'equipment authority'
                    ),
                )
            )
        return CaptureAuthoringAnnotation(
            record_id=record.record_id,
            entity_type=entity_type,
            label=document.get('label'),
            coordinate_space_id=record.coordinate_space_id or '',
            authority_record_handoff_id=record.authority_record_handoff_id,
            source_payload_sha256=record.source_payload_sha256,
            document=document,
            evidence_refs=record.evidence_refs,
            endpoint_refs=record.endpoint_refs,
            resolved_evidence=tuple(
                ref.model_dump(mode='json')
                for ref in record.resolved_evidence
            ),
            resolved_endpoints=tuple(
                ref.model_dump(mode='json')
                for ref in record.resolved_endpoints
            ),
            equipment_ref=equipment_ref,
            suggestion_only=unsupported,
            conflicts=tuple(conflicts),
        )

    def _measurement(
        self,
        record: CaptureAuthorityRecord,
        document: dict,
        conflicts: tuple[CaptureAuthoringConflict, ...],
    ) -> CaptureAuthoringMeasurement:
        return CaptureAuthoringMeasurement(
            record_id=record.record_id,
            quantity_type=str(document.get('quantity_type', '')),
            value=float(document.get('value', 0.0)),
            unit=str(document.get('unit', '')),
            coordinate_space_id=record.coordinate_space_id,
            authority_record_handoff_id=record.authority_record_handoff_id,
            source_payload_sha256=record.source_payload_sha256,
            document=document,
            evidence_refs=record.evidence_refs,
            endpoint_refs=record.endpoint_refs,
            resolved_evidence=tuple(
                ref.model_dump(mode='json')
                for ref in record.resolved_evidence
            ),
            resolved_endpoints=tuple(
                ref.model_dump(mode='json')
                for ref in record.resolved_endpoints
            ),
            conflicts=conflicts,
        )

    def _equipment_ref(
        self,
        raw: object,
    ) -> CaptureEquipmentRefCandidate | None:
        if not isinstance(raw, dict):
            return None
        candidate = CaptureEquipmentRefCandidate(
            equipment_id=str(raw['equipment_id']),
            equipment_version=str(raw['equipment_version']),
            equipment_hash=str(raw['equipment_hash']),
            status='unresolved',
            resolved_definition_version=None,
        )
        if self.equipment_repository is None:
            return candidate
        definition = self.equipment_repository.get_definition_by_hash(
            candidate.equipment_hash
        )
        if definition is None:
            return candidate
        if (
            definition.definition_id == candidate.equipment_id
            and definition.version == candidate.equipment_version
        ):
            return CaptureEquipmentRefCandidate(
                equipment_id=candidate.equipment_id,
                equipment_version=candidate.equipment_version,
                equipment_hash=candidate.equipment_hash,
                status='verified',
                resolved_definition_version=definition.version,
            )
        return CaptureEquipmentRefCandidate(
            equipment_id=candidate.equipment_id,
            equipment_version=candidate.equipment_version,
            equipment_hash=candidate.equipment_hash,
            status='mismatch',
            resolved_definition_version=definition.version,
        )

    def reconcile_measurement(
        self,
        measurement: CaptureAuthoringMeasurement,
        existing_value: float | None,
        *,
        tolerance: float = 1e-6,
    ) -> tuple[Literal['new', 'consistent', 'conflict'], str]:
        """Reconcile an imported measurement with an existing value.

        Imported measurements are authoring evidence; they never
        overwrite. 'new' = nothing to compare, 'consistent' = within
        tolerance, 'conflict' = surfaced for the operator.
        """
        if existing_value is None:
            return ('new', 'no existing value to reconcile against')
        if abs(measurement.value - existing_value) <= tolerance:
            return (
                'consistent',
                f'{measurement.quantity_type} matches within {tolerance}',
            )
        return (
            'conflict',
            f'{measurement.quantity_type}: imported '
            f'{measurement.value}{measurement.unit} differs from existing '
            f'{existing_value}{measurement.unit}',
        )

    def apply(
        self,
        batch: CaptureAuthoringBatch,
        record: CaptureAuthoringAnnotation | CaptureAuthoringMeasurement,
        *,
        scene_revision_id: str,
        operator_action: str,
    ) -> CaptureAuthoringProvenance:
        """Record that an imported record informed a scene revision.

        This is the only mutation-adjacent surface: it is explicit,
        operator-initiated, and it preserves the capture identity chain
        (handoff id, payload hash, coordinate space, revision) rather
        than copying values into the scene itself.
        """
        if isinstance(record, CaptureAuthoringAnnotation):
            if record.entity_type in UNSUPPORTED_ENTITY_TYPES:
                raise CaptureAuthoringError(
                    f'entity type {record.entity_type!r} is not '
                    'scene-enterable'
                )
            blocking = [
                c for c in record.conflicts
                if c.kind in (
                    'coordinate_space_mismatch',
                    'unresolved_reference',
                )
            ]
            if blocking:
                raise CaptureAuthoringError(
                    '; '.join(c.detail for c in blocking)
                )
            known = {a.record_id: a for a in batch.annotations}
            if record.record_id not in known:
                raise CaptureAuthoringError(
                    'annotation does not belong to this ingestion lineage'
                )
        else:
            known = {m.record_id: m for m in batch.measurements}
            if record.record_id not in known:
                raise CaptureAuthoringError(
                    'measurement does not belong to this ingestion lineage'
                )
        return CaptureAuthoringProvenance(
            capture_lineage_digest=batch.lineage_digest,
            bundle_digest=batch.bundle_digest,
            capture_revision_id=batch.capture_revision_id,
            record_kind=(
                'annotation'
                if isinstance(record, CaptureAuthoringAnnotation)
                else 'measurement'
            ),
            record_id=record.record_id,
            authority_record_handoff_id=record.authority_record_handoff_id,
            source_payload_sha256=record.source_payload_sha256,
            coordinate_space_id=record.coordinate_space_id,
            applied_to_scene_revision_id=scene_revision_id,
            operator_action=operator_action,
            resolved_refs=(
                record.resolved_evidence + record.resolved_endpoints
            ),
        )
