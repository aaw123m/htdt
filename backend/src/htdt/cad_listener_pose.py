"""Listener-pose authority — the R-Series seat listener geometry contract
(#632).

A ``ListenerPoseAuthority`` is a sealed, versioned record bound to one seat
``SceneEntity`` that carries the full listening-geometry convention in one
place: head-center, eye, acoustic-reference and ear offsets in seat-local
coordinates, plus posture/facing. It is the single authority for ear/eye/head
geometry used by seat ``SeatGeometryBinding``s, measurement-target derivation
and receiver reference — ear, eye and head positions never silently reuse
each other (a missing eye/head reference is UNKNOWN, not inferred from ears).
The seat entity keeps its physical footprint geometry; the pose carries the
listener-body convention.

Poses carry no identity data about a person — a pose is a posture convention
labelled by geometry, never a "user profile".
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_schema import ensure_native_schema, require_native_tables
from .cad_scene import (
    Offset3,
    Position3,
    SceneEntity,
    acoustic_reference_position,
    quaternion_to_euler_deg,
    quaternion_to_matrix3,
)
from .cad_video_geometry import SeatGeometryBinding
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now

if TYPE_CHECKING:
    from .cad_repository import SceneRepository


ListenerPostureKind = Literal[
    'upright',
    'reclined',
    'leaning_forward',
    'custom',
]

_LISTENER_POSE_PREFIX = 'listener-pose:'


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class ListenerPoseAuthority(BaseModel):
    """Sealed listener-geometry convention bound to one seat entity.

    All reference offsets are explicit seat-local coordinates — the pose
    never derives eye/head from ear geometry (or vice versa). ``ear_left``
    /``ear_right`` are optional because symmetric ears are a modeling choice
    some flows (single point measurement) do not use; the acoustic reference
    offset is the mandatory listening-reference point.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    pose_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    seat_entity_id: str = Field(min_length=1)
    #: Project the seat binding was authored under. ``None`` declares an
    #: unscoped/reusable pose; a project-local pose always pins the exact
    #: document so a colliding local entity id in another project cannot
    #: satisfy the binding by string match alone.
    document_id: str | None = Field(default=None, min_length=1)
    label: str = Field(min_length=1)
    head_center_offset_local_m: Offset3
    eye_reference_offset_local_m: Offset3
    acoustic_reference_offset_local_m: Offset3
    head_radius_m: float = Field(gt=0.0, le=0.5)
    ear_left_offset_local_m: Offset3 | None = None
    ear_right_offset_local_m: Offset3 | None = None
    facing_yaw_deg: float = 0.0
    posture_kind: ListenerPostureKind = 'upright'
    provenance: str = Field(min_length=1)
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_pose(self) -> 'ListenerPoseAuthority':
        _require_iso8601(self.created_at_utc, 'pose created_at_utc')
        if (self.ear_left_offset_local_m is None) != (
            self.ear_right_offset_local_m is None
        ):
            raise ValueError(
                'pose ear offsets must be provided as a left/right pair'
            )
        if not self.pose_id.startswith(_LISTENER_POSE_PREFIX):
            raise ValueError('listener pose id must use listener-pose: prefix')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('listener pose semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'pose_id': self.pose_id,
            'authority_version': self.authority_version,
            'seat_entity_id': self.seat_entity_id,
            'label': self.label,
            'head_center_offset_local_m': (
                self.head_center_offset_local_m.model_dump(mode='json')
            ),
            'eye_reference_offset_local_m': (
                self.eye_reference_offset_local_m.model_dump(mode='json')
            ),
            'acoustic_reference_offset_local_m': (
                self.acoustic_reference_offset_local_m.model_dump(mode='json')
            ),
            'head_radius_m': self.head_radius_m,
            'facing_yaw_deg': self.facing_yaw_deg,
            'posture_kind': self.posture_kind,
            'provenance': self.provenance,
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }
        # Optional fields join the identity only when present — the same
        # additive convention the acquisition context uses, so a future pose
        # extension does not rewrite existing authority hashes.
        if self.ear_left_offset_local_m is not None:
            payload['ear_left_offset_local_m'] = (
                self.ear_left_offset_local_m.model_dump(mode='json')
            )
        if self.ear_right_offset_local_m is not None:
            payload['ear_right_offset_local_m'] = (
                self.ear_right_offset_local_m.model_dump(mode='json')
            )
        if self.document_id is not None:
            payload['document_id'] = self.document_id
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.pose_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_listener_pose(
    *,
    seat_entity_id: str,
    label: str,
    head_center_offset_local_m: Offset3,
    eye_reference_offset_local_m: Offset3,
    acoustic_reference_offset_local_m: Offset3,
    head_radius_m: float = 0.10,
    ear_left_offset_local_m: Offset3 | None = None,
    ear_right_offset_local_m: Offset3 | None = None,
    facing_yaw_deg: float = 0.0,
    posture_kind: ListenerPostureKind = 'upright',
    provenance: str,
    notes: str = '',
    authority_version: str = '1',
    pose_id: str | None = None,
    document_id: str | None = None,
    created_at_utc: str | None = None,
) -> ListenerPoseAuthority:
    """Assemble a sealed listener pose for a seat."""

    payload: dict[str, Any] = {
        'pose_id': pose_id or f'{_LISTENER_POSE_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'seat_entity_id': seat_entity_id,
        'document_id': document_id,
        'label': label,
        'head_center_offset_local_m': head_center_offset_local_m,
        'eye_reference_offset_local_m': eye_reference_offset_local_m,
        'acoustic_reference_offset_local_m': acoustic_reference_offset_local_m,
        'head_radius_m': head_radius_m,
        'ear_left_offset_local_m': ear_left_offset_local_m,
        'ear_right_offset_local_m': ear_right_offset_local_m,
        'facing_yaw_deg': facing_yaw_deg,
        'posture_kind': posture_kind,
        'provenance': provenance,
        'notes': notes,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = ListenerPoseAuthority.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return ListenerPoseAuthority.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def listener_pose_for_seat(
    seat: SceneEntity,
    *,
    label: str,
    eye_reference_offset_local_m: Offset3,
    head_center_offset_local_m: Offset3,
    head_radius_m: float = 0.10,
    provenance: str,
    posture_kind: ListenerPostureKind = 'upright',
    facing_yaw_deg: float | None = None,
    ear_left_offset_local_m: Offset3 | None = None,
    ear_right_offset_local_m: Offset3 | None = None,
    notes: str = '',
    document_id: str | None = None,
) -> ListenerPoseAuthority:
    """Materialize a pose whose acoustic reference mirrors the seat's
    ``acoustic_reference_offset_m`` — migration without coordinate changes:
    the same seat-local offset becomes the pose's acoustic convention. The
    seat must declare the offset; a bare seat position is never treated as
    the listener's ear location.
    """

    if seat.kind != 'seat':
        raise ValueError(f'entity is not a seat: {seat.entity_id}')
    offset = seat.acoustic_reference_offset_m
    if offset is None:
        raise ValueError(
            f'seat {seat.entity_id} has no acoustic reference offset; '
            'define the listening position on the seat first'
        )
    return build_listener_pose(
        seat_entity_id=seat.entity_id,
        label=label,
        head_center_offset_local_m=head_center_offset_local_m,
        eye_reference_offset_local_m=eye_reference_offset_local_m,
        acoustic_reference_offset_local_m=offset,
        head_radius_m=head_radius_m,
        ear_left_offset_local_m=ear_left_offset_local_m,
        ear_right_offset_local_m=ear_right_offset_local_m,
        facing_yaw_deg=(
            quaternion_to_euler_deg(seat.orientation)[0]
            if facing_yaw_deg is None and seat.orientation is not None
            else (0.0 if facing_yaw_deg is None else facing_yaw_deg)
        ),
        posture_kind=posture_kind,
        provenance=provenance,
        notes=notes,
        document_id=document_id,
    )


def pose_acoustic_reference_position(
    seat: SceneEntity,
    pose: ListenerPoseAuthority,
) -> Position3:
    """Resolve the pose's acoustic reference into world coordinates through
    the seat's current placement — the same convention
    ``acoustic_reference_position`` applies to seat-local offsets."""

    if pose.seat_entity_id != seat.entity_id:
        raise ValueError(
            f'pose {pose.pose_id} is bound to seat {pose.seat_entity_id}, '
            f'not {seat.entity_id}'
        )
    matrix = quaternion_to_matrix3(seat.orientation)
    local = (
        pose.acoustic_reference_offset_local_m.x_m,
        pose.acoustic_reference_offset_local_m.y_m,
        pose.acoustic_reference_offset_local_m.z_m,
    )
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return Position3(
        x_m=seat.position.x_m + rotated[0],
        y_m=seat.position.y_m + rotated[1],
        z_m=seat.position.z_m + rotated[2],
    )


def pose_eye_reference_position(
    seat: SceneEntity,
    pose: ListenerPoseAuthority,
) -> Position3:
    """Resolve the pose's eye reference into world coordinates."""

    if pose.seat_entity_id != seat.entity_id:
        raise ValueError(
            f'pose {pose.pose_id} is bound to seat {pose.seat_entity_id}, '
            f'not {seat.entity_id}'
        )
    matrix = quaternion_to_matrix3(seat.orientation)
    local = (
        pose.eye_reference_offset_local_m.x_m,
        pose.eye_reference_offset_local_m.y_m,
        pose.eye_reference_offset_local_m.z_m,
    )
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return Position3(
        x_m=seat.position.x_m + rotated[0],
        y_m=seat.position.y_m + rotated[1],
        z_m=seat.position.z_m + rotated[2],
    )


# #939: one canonical receiver resolver shared by Room prediction, acoustic
# snapshot compilation, seat-population evaluation and measurement targets.
# A seat receiver + selected exact ListenerPose resolves through the pose;
# the legacy seat acoustic offset is only an explicitly labelled fallback.
LISTENER_RECEIVER_ORIENTATION_SEMANTICS = (
    'scalar_point_receiver_ignores_orientation'
)
ListenerReceiverResolutionKind = Literal[
    'listener_pose',
    'legacy_seat_offset',
]


@dataclass(frozen=True, slots=True)
class ResolvedListenerReceiver:
    """Result of resolving one receiver entity through the pose authority.

    ``pose_ref`` is the exact ListenerPose authority (id/version/semantic
    hash) the position was derived from, or ``None`` on the legacy seat
    offset fallback. ``facing_yaw_deg``/``posture_kind`` are recorded for
    provenance; ``orientation_semantics`` states explicitly that the scalar
    point-pressure receiver ignores head orientation — directional/binaural
    consumers must bind ear geometry separately and may not reuse this
    center reference silently.
    """

    entity_id: str
    position: Position3
    resolution: ListenerReceiverResolutionKind
    pose_ref: ExactExternalAuthorityRef | None
    facing_yaw_deg: float | None
    posture_kind: str | None
    orientation_semantics: Literal[
        'scalar_point_receiver_ignores_orientation'
    ] = LISTENER_RECEIVER_ORIENTATION_SEMANTICS


def resolve_listener_receiver(
    entity: SceneEntity,
    pose: ListenerPoseAuthority | None = None,
) -> ResolvedListenerReceiver | None:
    """Resolve the canonical acoustic receiver for one scene entity.

    Returns ``None`` when the entity carries no acoustic reference at all.
    A bound ``pose`` must name this exact seat entity and, when scoped, the
    pose's project document id — mismatches raise instead of silently
    falling back to the legacy offset.
    """

    if pose is not None:
        if entity.kind != 'seat':
            raise ValueError(
                'a listener pose can only resolve a seat receiver '
                f'({entity.entity_id} is {entity.kind})'
            )
        position = pose_acoustic_reference_position(entity, pose)
        return ResolvedListenerReceiver(
            entity_id=entity.entity_id,
            position=position,
            resolution='listener_pose',
            pose_ref=pose.authority_ref(),
            facing_yaw_deg=float(pose.facing_yaw_deg),
            posture_kind=str(pose.posture_kind),
        )
    reference = acoustic_reference_position(entity)
    if reference is None:
        return None
    return ResolvedListenerReceiver(
        entity_id=entity.entity_id,
        position=reference,
        resolution='legacy_seat_offset',
        pose_ref=None,
        facing_yaw_deg=None,
        posture_kind=None,
    )


def seat_binding_from_pose(
    pose: ListenerPoseAuthority,
    *,
    row_id: str = 'seat-1',
    riser_entity_id: str | None = None,
) -> SeatGeometryBinding:
    """Derive the validated SeatGeometryBinding from the pose's own eye/head
    convention — the binding carries the resolved numbers, so the request
    identity stays content-addressed.
    """

    return SeatGeometryBinding(
        entity_id=pose.seat_entity_id,
        row_id=row_id,
        eye_reference_offset_local_m=pose.eye_reference_offset_local_m,
        head_center_offset_local_m=pose.head_center_offset_local_m,
        head_radius_m=pose.head_radius_m,
        riser_entity_id=riser_entity_id,
        # #1056: the binding must prove the exact pose it derives from.
        geometry_source='listener_pose',
        pose_ref=pose.authority_ref(),
    )


class CadListenerPoseRepository:
    """SQLite persistence for listener poses and per-seat selection.

    Pass the project's ``SceneRepository`` so ``select_pose`` can prove the
    document and seat the pose binds actually exist; without it selection
    still enforces the persisted-authority + exact-hash contract only.
    """

    def __init__(
        self,
        path: Path | str,
        scene_repository: 'SceneRepository | None' = None,
    ) -> None:
        self.path = Path(path)
        self.scene_repository = scene_repository
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_listener_poses',
                'cad_listener_pose_selections',
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def save_pose(self, pose: ListenerPoseAuthority) -> None:
        """Persist an immutable pose authority: same id + byte-identical
        payload is an idempotent no-op; a different payload under an existing
        id is a collision — a revised pose needs a new authority identity."""
        payload_json = pose.model_dump_json()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_listener_poses WHERE pose_id=?',
                (pose.pose_id,),
            ).fetchone()
            if row is not None:
                if row['payload_json'] == payload_json:
                    return
                raise ValueError(
                    f'listener pose id collision with different payload: '
                    f'{pose.pose_id}'
                )
            connection.execute(
                'INSERT INTO cad_listener_poses'
                '(pose_id, seat_entity_id, payload_json) VALUES(?,?,?)',
                (pose.pose_id, pose.seat_entity_id, payload_json),
            )

    def get_pose(self, pose_id: str) -> ListenerPoseAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_listener_poses WHERE pose_id=?',
                (pose_id,),
            ).fetchone()
        if row is None:
            return None
        return ListenerPoseAuthority.model_validate_json(row['payload_json'])

    def list_poses_for_seat(
        self,
        seat_entity_id: str,
    ) -> tuple[ListenerPoseAuthority, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_listener_poses'
                ' WHERE seat_entity_id=? ORDER BY pose_id ASC',
                (seat_entity_id,),
            ).fetchall()
        return tuple(
            ListenerPoseAuthority.model_validate_json(row['payload_json'])
            for row in rows
        )

    def select_pose(
        self,
        document_id: str,
        pose: ListenerPoseAuthority,
    ) -> None:
        """Record the pose choice for one seat in one document.

        The pose must be persisted with an identical semantic hash, and a
        project-bound pose may only serve the document it was authored for.
        When a ``SceneRepository`` is wired the document must exist and the
        named seat entity must be present with kind ``seat`` — a colliding
        local entity id from another project can never satisfy the binding.
        """
        persisted = self.get_pose(pose.pose_id)
        if persisted is None:
            raise ValueError(
                f'selected listener pose is not persisted: {pose.pose_id}'
            )
        if persisted.semantic_sha256 != pose.semantic_sha256:
            raise ValueError(
                f'selected listener pose hash does not match the persisted '
                f'authority: {pose.pose_id}'
            )
        if pose.document_id is not None and pose.document_id != document_id:
            raise ValueError(
                f'listener pose {pose.pose_id} was authored for document '
                f'{pose.document_id}, not {document_id}'
            )
        if self.scene_repository is not None:
            revision = self.scene_repository.current_head(document_id)
            if revision is None:
                raise ValueError(f'document does not exist: {document_id}')
            seat = next(
                (
                    entity
                    for entity in revision.document.entities
                    if entity.entity_id == pose.seat_entity_id
                ),
                None,
            )
            if seat is None:
                raise ValueError(
                    f'seat entity {pose.seat_entity_id} does not exist in '
                    f'document {document_id}'
                )
            if seat.kind != 'seat':
                raise ValueError(
                    f'entity {pose.seat_entity_id} in document {document_id} '
                    f'is not a seat (kind={seat.kind})'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_listener_pose_selections'
                '(document_id, seat_entity_id, pose_id, pose_sha256)'
                ' VALUES(?,?,?,?)'
                ' ON CONFLICT(document_id, seat_entity_id) DO UPDATE SET'
                ' pose_id=excluded.pose_id,'
                ' pose_sha256=excluded.pose_sha256',
                (
                    document_id,
                    pose.seat_entity_id,
                    pose.pose_id,
                    pose.semantic_sha256,
                ),
            )

    def selections_for_document(
        self,
        document_id: str,
    ) -> dict[str, ListenerPoseAuthority]:
        """Every verified pose selection recorded for this document.

        Rows whose bound authority hash no longer matches the persisted
        record are skipped — a stale selection is not an authority claim.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT seat_entity_id, pose_id, pose_sha256'
                ' FROM cad_listener_pose_selections WHERE document_id=?'
                ' ORDER BY seat_entity_id ASC',
                (document_id,),
            ).fetchall()
        result: dict[str, ListenerPoseAuthority] = {}
        for row in rows:
            pose = self.get_pose(row['pose_id'])
            if pose is None or pose.semantic_sha256 != row['pose_sha256']:
                continue
            result[row['seat_entity_id']] = pose
        return result

    def clear_selection(self, document_id: str, seat_entity_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_listener_pose_selections'
                ' WHERE document_id=? AND seat_entity_id=?',
                (document_id, seat_entity_id),
            )

    def selected_pose(
        self,
        document_id: str,
        seat_entity_id: str,
    ) -> ListenerPoseAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT pose_id, pose_sha256 FROM cad_listener_pose_selections'
                ' WHERE document_id=? AND seat_entity_id=?',
                (document_id, seat_entity_id),
            ).fetchone()
        if row is None:
            return None
        pose = self.get_pose(row['pose_id'])
        if pose is None:
            return None
        if pose.semantic_sha256 != row['pose_sha256']:
            raise ValueError(
                f'selected listener pose {row["pose_id"]} hash mismatch — '
                'refusing to resolve a different pose than was selected'
            )
        return pose


__all__ = [
    'CadListenerPoseRepository',
    'LISTENER_RECEIVER_ORIENTATION_SEMANTICS',
    'ListenerPoseAuthority',
    'ListenerPostureKind',
    'ListenerReceiverResolutionKind',
    'ResolvedListenerReceiver',
    'build_listener_pose',
    'listener_pose_for_seat',
    'pose_acoustic_reference_position',
    'pose_eye_reference_position',
    'resolve_listener_receiver',
    'seat_binding_from_pose',
]
