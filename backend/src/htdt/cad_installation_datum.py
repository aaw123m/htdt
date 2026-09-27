"""Installation datum authority (#537).

Room coordinates must be reproducible on-site: an installer measures from a
primary corner along real walls. This module records the *installation
datum* — the anchor reference point and the direction walls the canonical
coordinate frame is declared against — as a versioned, append-only authority
pinned to an exact SceneRevision content hash.

Contract properties:

- the datum declares a primary anchor (a room vertex or a wall face) plus the
  two direction walls whose orientation defines +X/+Y; coordinate frame
  semantics (origin label, axis labels, handedness) are explicit fields, never
  assumed from a diagram;
- :func:`reproject_datum_coordinates` resolves the datum against room
  geometry deterministically and returns the origin position, the +X/+Y unit
  directions and the rotation versus the canonical axes — with the evidence
  (vertex/wall ids, datum hash, pinned revision) an install plan needs to
  reproduce coordinates on-site;
- reprojection fails closed: walls that are not meaningfully perpendicular
  or an anchor that cannot resolve to a vertex produce ``UNKNOWN``, never a
  silently-fudged frame;
- :func:`evaluate_datum_freshness` is a pure read-only drift detector — it
  reports ``current``/``stale``/``missing`` against a scene content hash and
  the wall/vertex ids present in a room, and never rewrites the stored
  datum; a stale or unresolvable datum is exactly what freshness consumers
  must surface as UNKNOWN rather than extrapolate.
"""

from __future__ import annotations

from math import atan2, degrees, hypot, isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import RoomPrism, room_vertices
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


INSTALLATION_DATUM_AUTHORITY_VERSION = 'installation-datum-1'

DatumAnchorKind = Literal['room_vertex', 'wall_face']
DatumStatus = Literal['AVAILABLE', 'UNKNOWN']
DatumFreshnessStatus = Literal['current', 'stale', 'missing']

#: Cosine tolerance for declaring two direction walls non-perpendicular —
#: |dot| <= 0.05 ≈ 87.1°–92.9°. Real corner datums are right angles; picking
#: walls outside this band is a datum definition error, not room noise.
_PERPENDICULAR_DOT_TOLERANCE = 0.05






class DatumReferencePoint(BaseModel):
    """One explicit field-reference selection for a datum.

    ``room_vertex`` names an anchor vertex (e.g. the front-left corner);
    ``wall_face`` names a wall segment the anchor sits on. The reference is
    by stable id, never by a free description.
    """

    model_config = ConfigDict(frozen=True)

    kind: DatumAnchorKind
    vertex_id: str | None = None
    wall_id: str | None = None
    label: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_reference(self) -> 'DatumReferencePoint':
        if self.kind == 'room_vertex':
            if not self.vertex_id:
                raise ValueError('room_vertex anchor requires vertex_id')
        elif not self.wall_id:
            raise ValueError('wall_face anchor requires wall_id')
        return self


class DatumFrameSemantics(BaseModel):
    """The declared meaning of the canonical coordinate axes.

    Labels are the human-facing semantics the install plan presents
    (e.g. "front wall direction", "left wall direction", "up"); the frame is
    right-handed by definition.
    """

    model_config = ConfigDict(frozen=True)

    handedness: Literal['right'] = 'right'
    origin_label: str = Field(min_length=1)
    x_label: str = Field(min_length=1)
    y_label: str = Field(min_length=1)
    z_label: str = Field(min_length=1)


class InstallationDatum(BaseModel):
    """Versioned installation datum record pinned to one SceneRevision.

    ``scene_content_hash`` pins the room geometry the wall/vertex ids were
    selected against; ``evidence_refs`` names the external evidence (survey
    notes, diagrams, photo evidence ids) the selection is based on.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'installation-datum-1'
    ] = INSTALLATION_DATUM_AUTHORITY_VERSION
    datum_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    frame_semantics: DatumFrameSemantics
    primary_anchor: DatumReferencePoint
    x_direction_wall_id: str = Field(min_length=1)
    y_direction_wall_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    notes: str | None = None
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_datum(self) -> 'InstallationDatum':
        if self.x_direction_wall_id == self.y_direction_wall_id:
            raise ValueError('datum direction walls must be distinct')
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError('datum evidence refs must be unique')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('InstallationDatum semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'datum_id': self.datum_id,
            'version': self.version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'frame_semantics': self.frame_semantics.model_dump(mode='json'),
            'primary_anchor': self.primary_anchor.model_dump(mode='json'),
            'x_direction_wall_id': self.x_direction_wall_id,
            'y_direction_wall_id': self.y_direction_wall_id,
            'evidence_refs': list(self.evidence_refs),
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }


def build_installation_datum(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    frame_semantics: DatumFrameSemantics,
    primary_anchor: DatumReferencePoint,
    x_direction_wall_id: str,
    y_direction_wall_id: str,
    created_at_utc: str,
    datum_id: str | None = None,
    version: str = '1',
    evidence_refs: Sequence[str] = (),
    notes: str | None = None,
) -> InstallationDatum:
    payload: dict[str, Any] = {
        'authority_version': INSTALLATION_DATUM_AUTHORITY_VERSION,
        'datum_id': datum_id or str(uuid4()),
        'version': version,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'frame_semantics': frame_semantics,
        'primary_anchor': primary_anchor,
        'x_direction_wall_id': x_direction_wall_id,
        'y_direction_wall_id': y_direction_wall_id,
        'evidence_refs': tuple(evidence_refs),
        'notes': notes,
        'created_at_utc': created_at_utc,
    }
    provisional = InstallationDatum.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return InstallationDatum(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class DatumReprojection(BaseModel):
    """Deterministic resolution of a datum against room geometry.

    ``rotation_deg`` is the angle of the +X direction versus the canonical
    +X axis. The ``evidence`` dict names every input an auditor needs to
    reproduce the frame on-site (anchor/wall ids, datum hash, revision).
    """

    model_config = ConfigDict(frozen=True)

    status: DatumStatus
    origin_x_m: float | None = None
    origin_y_m: float | None = None
    x_axis_dx: float | None = None
    x_axis_dy: float | None = None
    y_axis_dx: float | None = None
    y_axis_dy: float | None = None
    rotation_deg: float | None = None
    reason: str = Field(min_length=1)
    evidence: dict[str, Any]


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    length = hypot(dx, dy)
    if not isfinite(length) or length <= 0.0:
        return None
    return (dx / length, dy / length)


def reproject_datum_coordinates(
    datum: InstallationDatum,
    *,
    room: RoomPrism,
    walls: tuple,
) -> DatumReprojection:
    """Resolve datum anchor + direction walls into a concrete frame.

    The origin is the primary anchor position: for ``room_vertex`` anchors
    the vertex coordinates; for ``wall_face`` anchors the selected wall's
    ``from_vertex`` (the reference end of that wall face). +X/+Y point along
    the declared walls away from the anchor when the anchor is a wall
    endpoint, else along the wall's from→to direction.
    """

    vertices = {vertex.vertex_id: vertex for vertex in room_vertices(room)}
    wall_map = {wall.wall_id: wall for wall in walls}
    evidence = {
        'datum_id': datum.datum_id,
        'datum_version': datum.version,
        'datum_semantic_sha256': datum.semantic_sha256,
        'scene_revision_id': datum.scene_revision_id,
        'scene_content_hash': datum.scene_content_hash,
        'anchor_kind': datum.primary_anchor.kind,
        'anchor_vertex_id': datum.primary_anchor.vertex_id,
        'anchor_wall_id': datum.primary_anchor.wall_id,
        'x_direction_wall_id': datum.x_direction_wall_id,
        'y_direction_wall_id': datum.y_direction_wall_id,
    }

    anchor_vertex_id: str | None = None
    if datum.primary_anchor.kind == 'room_vertex':
        anchor_vertex_id = datum.primary_anchor.vertex_id
        if anchor_vertex_id not in vertices:
            return DatumReprojection(
                status='UNKNOWN',
                reason='primary anchor vertex is not present in the room',
                evidence=evidence,
            )
    else:
        wall = wall_map.get(datum.primary_anchor.wall_id or '')
        if wall is None:
            return DatumReprojection(
                status='UNKNOWN',
                reason='primary anchor wall is not present in the room',
                evidence=evidence,
            )
        anchor_vertex_id = wall.from_vertex_id
        if anchor_vertex_id not in vertices:
            return DatumReprojection(
                status='UNKNOWN',
                reason='anchor wall endpoint vertex is not present in the room',
                evidence=evidence,
            )
    anchor = vertices[anchor_vertex_id]

    for wall_id in (datum.x_direction_wall_id, datum.y_direction_wall_id):
        if wall_id not in wall_map:
            return DatumReprojection(
                status='UNKNOWN',
                reason=f'direction wall is not present in the room: {wall_id}',
                evidence=evidence,
            )

    def wall_direction(wall_id: str) -> tuple[float, float] | None:
        wall = wall_map[wall_id]
        from_v = vertices.get(wall.from_vertex_id)
        to_v = vertices.get(wall.to_vertex_id)
        if from_v is None or to_v is None:
            return None
        if anchor_vertex_id == wall.to_vertex_id:
            # Away from the anchor: to→from.
            return _unit(from_v.x_m - to_v.x_m, from_v.y_m - to_v.y_m)
        return _unit(to_v.x_m - from_v.x_m, to_v.y_m - from_v.y_m)

    x_axis = wall_direction(datum.x_direction_wall_id)
    y_axis = wall_direction(datum.y_direction_wall_id)
    if x_axis is None or y_axis is None:
        return DatumReprojection(
            status='UNKNOWN',
            reason='a direction wall endpoint is missing or degenerate',
            evidence=evidence,
        )
    dot = x_axis[0] * y_axis[0] + x_axis[1] * y_axis[1]
    if abs(dot) > _PERPENDICULAR_DOT_TOLERANCE:
        return DatumReprojection(
            status='UNKNOWN',
            reason=(
                'declared direction walls are not perpendicular '
                f'(cos={dot:.3f}); the datum cannot define a room frame'
            ),
            evidence=evidence,
        )
    return DatumReprojection(
        status='AVAILABLE',
        origin_x_m=anchor.x_m,
        origin_y_m=anchor.y_m,
        x_axis_dx=x_axis[0],
        x_axis_dy=x_axis[1],
        y_axis_dx=y_axis[0],
        y_axis_dy=y_axis[1],
        rotation_deg=degrees(atan2(x_axis[1], x_axis[0])),
        reason='datum resolved against the pinned room geometry',
        evidence=evidence,
    )


class DatumFreshness(BaseModel):
    """Read-only drift check for one persisted datum (never rewrites it)."""

    model_config = ConfigDict(frozen=True)

    datum_id: str
    datum_version: str
    status: DatumFreshnessStatus
    reasons: tuple[str, ...] = ()


def evaluate_datum_freshness(
    datum: InstallationDatum,
    *,
    scene_content_hash: str,
    present_vertex_ids: Sequence[str],
    present_wall_ids: Sequence[str],
) -> DatumFreshness:
    """Report whether a saved datum still maps onto current room geometry.

    ``stale`` means the pinned scene content hash no longer matches (the room
    may have been edited since the datum was declared); ``missing`` means a
    referenced anchor/wall id is absent. The stored datum is never changed —
    consumers surface UNKNOWN rather than extrapolate.
    """

    reasons: list[str] = []
    status: DatumFreshnessStatus = 'current'
    if datum.scene_content_hash != scene_content_hash:
        status = 'stale'
        reasons.append(
            'scene content hash changed since the datum was recorded'
        )
    wall_ids = set(present_wall_ids)
    vertex_ids = set(present_vertex_ids)
    if datum.x_direction_wall_id not in wall_ids:
        status = 'missing'
        reasons.append(
            f'x direction wall absent: {datum.x_direction_wall_id}'
        )
    if datum.y_direction_wall_id not in wall_ids:
        status = 'missing'
        reasons.append(
            f'y direction wall absent: {datum.y_direction_wall_id}'
        )
    anchor = datum.primary_anchor
    if anchor.kind == 'room_vertex' and anchor.vertex_id not in vertex_ids:
        status = 'missing'
        reasons.append(f'anchor vertex absent: {anchor.vertex_id}')
    if anchor.kind == 'wall_face' and anchor.wall_id not in wall_ids:
        status = 'missing'
        reasons.append(f'anchor wall absent: {anchor.wall_id}')
    return DatumFreshness(
        datum_id=datum.datum_id,
        datum_version=datum.version,
        status=status,
        reasons=tuple(reasons),
    )


__all__ = [
    'DatumAnchorKind',
    'DatumFrameSemantics',
    'DatumFreshness',
    'DatumFreshnessStatus',
    'DatumReferencePoint',
    'DatumReprojection',
    'DatumStatus',
    'INSTALLATION_DATUM_AUTHORITY_VERSION',
    'InstallationDatum',
    'build_installation_datum',
    'evaluate_datum_freshness',
    'reproject_datum_coordinates',
]
