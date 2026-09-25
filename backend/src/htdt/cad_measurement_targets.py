"""Seat/listener → measurement-point derivation and lineage (#472).

A measurement point created from a seat carries the seat's own acoustic
(listening-position) reference — the coordinate is copied, never retyped —
and the persisted ``CadMeasurementTargetLineage`` row records which seat,
which SceneRevision and which exact position the point was derived from.
The point is then independent evidence: moving the seat creates new scene
revisions and must never rewrite the point's stored coordinates or its
derivation lineage. ``measurement_target_drift`` reports the distance the
seat's current acoustic reference has moved from the point's recorded
position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_listener_pose import (
    ListenerPoseAuthority,
    pose_acoustic_reference_position,
)
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import (
    Position3,
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


class MeasurementTargetError(ValueError):
    """User-correctable measurement-target derivation error."""


class CadMeasurementTargetLineage(BaseModel):
    """Immutable derivation lineage for a seat-derived measurement point.

    Records the exact source seat, the SceneRevision the point was created
    in and the acoustic-reference position it was created at, so a later
    seat move can be reported as drift instead of being silently merged
    into the measurement point's stored coordinates.

    ``creation_revision_id`` has one adopted meaning (#847): the resulting
    SceneRevision that contains BOTH the source seat and the derived
    measurement point — the revision the derivation produced, not the
    pre-derivation head. Persisted reads re-verify the derivation in that
    pinned revision; current-head drift is reported by
    :func:`measurement_target_drift` and never invalidates the record.
    """

    model_config = ConfigDict(frozen=True)

    target_lineage_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_point_id: str = Field(min_length=1)
    source_seat_id: str = Field(min_length=1)
    creation_revision_id: str = Field(min_length=1)
    initial_position: Position3
    source_pose_ref: ExactExternalAuthorityRef | None = None
    # Explicit two-revision semantics (#862): ``creation_revision_id`` is the
    # revision that contains the created point; ``source_scene_revision_id`` is
    # the revision the seat was read from (the created revision's parent).
    creation_scene_content_hash: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    source_scene_revision_id: str | None = Field(
        default=None, min_length=1
    )
    source_scene_content_hash: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    created_at_utc: str = Field(min_length=1)
    target_lineage_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_lineage(self) -> 'CadMeasurementTargetLineage':
        _require_iso8601(self.created_at_utc, 'target lineage created_at_utc')
        if self.target_lineage_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement target lineage hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'target_lineage_id': self.target_lineage_id,
            'document_id': self.document_id,
            'measurement_point_id': self.measurement_point_id,
            'source_seat_id': self.source_seat_id,
            'creation_revision_id': self.creation_revision_id,
            'initial_position': self.initial_position.model_dump(mode='json'),
            'created_at_utc': self.created_at_utc,
        }
        # Optional fields join identity only when present (additive
        # convention) — lineage recorded before poses existed keeps its hash.
        if self.source_pose_ref is not None:
            payload['source_pose_ref'] = self.source_pose_ref.model_dump(
                mode='json'
            )
        if self.creation_scene_content_hash is not None:
            payload['creation_scene_content_hash'] = (
                self.creation_scene_content_hash
            )
        if self.source_scene_revision_id is not None:
            payload['source_scene_revision_id'] = self.source_scene_revision_id
        if self.source_scene_content_hash is not None:
            payload['source_scene_content_hash'] = self.source_scene_content_hash
        return payload


def build_measurement_target_lineage(
    *,
    document_id: str,
    measurement_point_id: str,
    source_seat_id: str,
    creation_revision_id: str,
    initial_position: Position3,
    source_pose_ref: ExactExternalAuthorityRef | None = None,
    creation_scene_content_hash: str | None = None,
    source_scene_revision_id: str | None = None,
    source_scene_content_hash: str | None = None,
    target_lineage_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadMeasurementTargetLineage:
    """Assemble a sealed seat→measurement-point lineage record."""
    payload: dict[str, Any] = {
        'target_lineage_id': target_lineage_id or str(uuid4()),
        'document_id': document_id,
        'measurement_point_id': measurement_point_id,
        'source_seat_id': source_seat_id,
        'creation_revision_id': creation_revision_id,
        'initial_position': initial_position,
        'source_pose_ref': source_pose_ref,
        'creation_scene_content_hash': creation_scene_content_hash,
        'source_scene_revision_id': source_scene_revision_id,
        'source_scene_content_hash': source_scene_content_hash,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = CadMeasurementTargetLineage.model_construct(
        **payload,
        target_lineage_sha256='0' * 64,
    )
    return CadMeasurementTargetLineage(
        **payload,
        target_lineage_sha256=_hash(provisional.identity_payload()),
    )


def derive_measurement_point_document(
    document: SceneDocument,
    *,
    source_seat_id: str,
    measurement_point_id: str,
    name: str | None = None,
    listener_pose: ListenerPoseAuthority | None = None,
) -> SceneDocument:
    """Return a new document with a measurement point at the seat reference.

    The point's position is copied from the seat's resolved acoustic
    reference position (its listening-position offset in world
    coordinates); the seat must declare that reference explicitly — a bare
    seat position is never treated as the listener's ear location. The
    caller persists the returned document as a new SceneRevision and stores
    the lineage row through the quality repository.
    """
    try:
        seat = document.entity(source_seat_id)
    except KeyError as exc:
        raise MeasurementTargetError(
            f'unknown seat entity: {source_seat_id}'
        ) from exc
    if seat.kind != 'seat':
        raise MeasurementTargetError(
            f'entity is not a seat: {source_seat_id}'
        )
    reference = acoustic_reference_position(seat)
    if reference is None:
        raise MeasurementTargetError(
            f'seat {source_seat_id} has no acoustic reference offset; '
            'define the listening position on the seat first'
        )
    try:
        document.entity(measurement_point_id)
    except KeyError:
        pass
    else:
        raise MeasurementTargetError(
            f'measurement point id already exists: {measurement_point_id}'
        )
    if listener_pose is not None:
        if listener_pose.seat_entity_id != seat.entity_id:
            raise MeasurementTargetError(
                f'listener pose {listener_pose.pose_id} is bound to '
                f'{listener_pose.seat_entity_id}, not {source_seat_id}'
            )
        # The bound pose is the acoustic-reference authority (#632): the point
        # materializes at the pose's reference resolved through the seat's
        # current placement, not a fresh derivation at read time.
        reference = pose_acoustic_reference_position(seat, listener_pose)
    point = SceneEntity(
        entity_id=measurement_point_id,
        kind='measurement_point',
        name=name or f'{seat.name} リスナー位置',
        position=reference,
    )
    return document.model_copy(
        update={'entities': (*document.entities, point)}
    )


@dataclass(frozen=True)
class MeasurementTargetDrift:
    """How far a seat-derived point has drifted from its seat reference.

    ``drift_m`` compares the *current* seat acoustic reference against the
    measurement point's *current* position — both read from their own
    revisions, so lineage history is never rewritten when either moves.
    ``initial_drift_m`` compares the current point position to the position
    it was created at.
    """

    lineage: CadMeasurementTargetLineage
    current_seat_revision_id: str
    current_point_revision_id: str
    seat_reference_position: Position3
    point_position: Position3
    drift_m: float
    initial_drift_m: float


def _distance(a: Position3, b: Position3) -> float:
    return sqrt(
        (a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2
    )


def measurement_target_drift(
    scene_repository: SceneRepository,
    lineage: CadMeasurementTargetLineage,
) -> MeasurementTargetDrift | None:
    """Current drift between a lineage-bound point and its source seat.

    Both positions are resolved on the document's current head. Returns
    ``None`` when either entity is missing from the head revision — the
    lineage row itself stays valid history either way.
    """
    head = scene_repository.current_head(lineage.document_id)
    if head is None:
        return None
    try:
        point = head.document.entity(lineage.measurement_point_id)
    except KeyError:
        return None
    try:
        seat = head.document.entity(lineage.source_seat_id)
    except KeyError:
        return None
    seat_reference = acoustic_reference_position(seat)
    if seat_reference is None:
        return None
    point_position = acoustic_reference_position(point)
    if point_position is None:
        return None
    return MeasurementTargetDrift(
        lineage=lineage,
        current_seat_revision_id=head.revision_id,
        current_point_revision_id=head.revision_id,
        seat_reference_position=seat_reference,
        point_position=point_position,
        drift_m=_distance(seat_reference, point_position),
        initial_drift_m=_distance(lineage.initial_position, point_position),
    )


def seat_candidates(document: SceneDocument) -> tuple[SceneEntity, ...]:
    """Seat entities that declare an acoustic (listener) reference."""
    return tuple(
        entity
        for entity in document.entities
        if entity.kind == 'seat' and acoustic_reference_position(entity) is not None
    )


__all__ = [
    'CadMeasurementTargetLineage',
    'MeasurementTargetDrift',
    'MeasurementTargetError',
    'build_measurement_target_lineage',
    'derive_measurement_point_document',
    'measurement_target_drift',
    'seat_candidates',
]
