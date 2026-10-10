"""Cable-run route geometry authority (#1011, M2).

A ``CableRun`` records *that* a cable runs between two bound endpoints and
how long each segment was declared to be; it records nothing about *where*
the route goes. Drawing straight lines between the endpoints would present
an invented route as fact. ``CableRunGeometry`` closes that gap as an
optional, immutable, linked authority: for each ``CableRunSegment.sequence``
it can carry, an arbitrary waypoint polyline (XYZ, metres, declared
coordinate frame), the concealment kind observed/declared for that segment,
the semantic surfaces the segment is recorded as traversing, the record's
source channel, and a design-vs-as-built distinction.

Contract properties:

- geometry is *linked*, never merged — the record pins the exact
  ``CableRun`` (run_id + version + semantic hash) and the same
  SceneRevision/content hash the run pins, so a run re-version or a scene
  edit marks the geometry non-current instead of silently reinterpreting
  it;
- waypoints are the only authored route — a renderer may connect recorded
  waypoints in order, and may never interpolate a route where no segment
  geometry exists;
- the geometric polyline length is stored as its own number beside the
  declared ``length_m`` — :func:`evaluate_cable_run_geometry_divergence`
  reports the difference per segment and in total (service loop is never
  part of geometry) without overwriting either authority;
- ``record_kind`` distinguishes design intent from an as-built record and
  ``source`` names the provenance channel; an as-built record is an
  operator attestation, never a surveyed measurement, unless its source
  says so;
- legacy length-only CableRuns stay valid and complete authorities — the
  absence of geometry is a displayed fact (``経路形状未登録``), not an
  error.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_cable_run import CablePathKind, CableRun
from .cad_scene import Position3
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


CABLE_RUN_GEOMETRY_AUTHORITY_VERSION = 'cable-run-geometry-1'

#: Waypoints are always expressed in the document's room coordinate frame
#: (metres). The literal is declared on the record — not assumed — so a
#: geometry minted under a different frame can never be read as room
#: coordinates by a later importer.
CableRunGeometryFrame = Literal['room_meters']

#: Where the recorded waypoints came from. ``authored`` = entered in the
#: app, ``imported`` = brought in from an external document/exchange,
#: ``as_built_survey`` = transcribed from a physical measurement record.
CableRunGeometrySource = Literal['authored', 'imported', 'as_built_survey']

#: What the record claims about itself: design intent versus an as-built
#: record of what was actually installed. An as-built record is still an
#: attestation — nothing here certifies construction truth.
CableRunGeometryRecordKind = Literal['design', 'as_built']

CableRunGeometryFreshnessStatus = Literal['current', 'stale', 'missing']

#: Divergence is reported whenever the geometric polyline length and the
#: declared segment length differ by more than this absolute tolerance —
#: they are different facts (declared install length vs recorded route
#: shape), so the check is not a float-noise guard but a real comparison.
GEOMETRY_LENGTH_TOLERANCE_M = 1e-9


def _polyline_length_m(waypoints: Sequence[Position3]) -> float:
    total = 0.0
    for first, second in zip(waypoints, waypoints[1:]):
        dx = float(second.x_m) - float(first.x_m)
        dy = float(second.y_m) - float(first.y_m)
        dz = float(second.z_m) - float(first.z_m)
        total += sqrt(dx * dx + dy * dy + dz * dz)
    return total


class CableRunSegmentGeometry(BaseModel):
    """Recorded route shape for one ``CableRunSegment.sequence``."""

    model_config = ConfigDict(frozen=True)

    segment_sequence: int = Field(ge=0)
    #: Ordered route vertices in metres; rendered as a polyline in exactly
    #: this order. Two points minimum — a single point is a marker, not a
    #: route.
    waypoints: tuple[Position3, ...] = Field(min_length=2)
    coordinate_frame: CableRunGeometryFrame = 'room_meters'
    #: The concealment/routing kind the geometry records for this segment.
    #: Kept distinct from the run's declared ``path_kind`` — as-built
    #: routing may diverge from the design declaration, and the divergence
    #: is surfaced rather than rejected.
    concealment_kind: CablePathKind
    #: Semantic surfaces (wall/opening/part ids) the route is recorded as
    #: traversing. Presence is a claim on the record; freshness re-checks
    #: each id against the current scene and reports missing references.
    traversed_surface_ids: tuple[str, ...] = ()
    source: CableRunGeometrySource = 'authored'
    record_kind: CableRunGeometryRecordKind = 'design'
    note: str | None = None

    @model_validator(mode='after')
    def valid_segment_geometry(self) -> 'CableRunSegmentGeometry':
        if len(set(self.traversed_surface_ids)) != len(
            self.traversed_surface_ids
        ):
            raise ValueError('traversed surface ids must be unique')
        if list(self.traversed_surface_ids) != sorted(
            self.traversed_surface_ids
        ):
            raise ValueError('traversed surface ids must be sorted')
        for waypoint in self.waypoints:
            if not (
                isfinite(float(waypoint.x_m))
                and isfinite(float(waypoint.y_m))
                and isfinite(float(waypoint.z_m))
            ):
                raise ValueError('waypoint coordinates must be finite')
        return self

    def polyline_length_m(self) -> float:
        """Geometric length of the recorded route, metres."""
        return _polyline_length_m(self.waypoints)


class CableRunGeometry(BaseModel):
    """Versioned route-geometry record pinned to one exact CableRun."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'cable-run-geometry-1'
    ] = CABLE_RUN_GEOMETRY_AUTHORITY_VERSION
    geometry_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    #: Exact CableRun pin: the geometry is meaningless re-attached to a
    #: different run version, so the run's semantic hash is pinned too.
    run_id: str = Field(min_length=1)
    run_version: str = Field(min_length=1)
    run_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    segment_geometries: tuple[CableRunSegmentGeometry, ...] = Field(
        min_length=1
    )
    #: Deterministic sum of every segment polyline length — kept beside,
    #: never merged into, the run's declared ``total_length_m``.
    geometric_length_m: float = Field(ge=0.0)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_geometry(self) -> 'CableRunGeometry':
        sequences = [
            segment.segment_sequence
            for segment in self.segment_geometries
        ]
        if len(set(sequences)) != len(sequences):
            raise ValueError(
                'cable run geometry segment sequences must be unique'
            )
        if sequences != sorted(sequences):
            raise ValueError(
                'cable run geometry segments must be ordered by sequence'
            )
        expected = sum(
            segment.polyline_length_m()
            for segment in self.segment_geometries
        )
        if not isfinite(float(self.geometric_length_m)):
            raise ValueError('geometric length must be finite')
        if abs(self.geometric_length_m - expected) > 1e-9:
            raise ValueError(
                'geometric_length_m must equal the summed polyline lengths'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CableRunGeometry semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'geometry_id': self.geometry_id,
            'version': self.version,
            'document_id': self.document_id,
            'run_id': self.run_id,
            'run_version': self.run_version,
            'run_semantic_sha256': self.run_semantic_sha256,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'segment_geometries': [
                segment.model_dump(mode='json')
                for segment in self.segment_geometries
            ],
            'geometric_length_m': self.geometric_length_m,
            'created_at_utc': self.created_at_utc,
        }

    def segment_geometry(self, sequence: int) -> CableRunSegmentGeometry | None:
        for segment in self.segment_geometries:
            if segment.segment_sequence == sequence:
                return segment
        return None


def build_cable_run_geometry(
    *,
    run: CableRun,
    segment_geometries: Sequence[CableRunSegmentGeometry],
    created_at_utc: str,
    geometry_id: str | None = None,
    version: str = '1',
) -> CableRunGeometry:
    """Mint a geometry record bound to the exact given ``CableRun``.

    Every declared ``segment_sequence`` must exist on the run — geometry
    cannot register route shape for a segment the run never declared.
    Partial coverage is legal: segments without geometry keep their
    honest ``経路形状未登録`` display state.
    """

    ordered = tuple(segment_geometries)
    declared = {segment.sequence for segment in run.segments}
    unknown = [
        segment.segment_sequence
        for segment in ordered
        if segment.segment_sequence not in declared
    ]
    if unknown:
        raise ValueError(
            'geometry registers sequences the CableRun never declared: '
            + ', '.join(str(item) for item in unknown)
        )
    total = sum(segment.polyline_length_m() for segment in ordered)
    payload: dict[str, Any] = {
        'authority_version': CABLE_RUN_GEOMETRY_AUTHORITY_VERSION,
        'geometry_id': geometry_id or str(uuid4()),
        'version': version,
        'document_id': run.document_id,
        'run_id': run.run_id,
        'run_version': run.version,
        'run_semantic_sha256': run.semantic_sha256,
        'scene_revision_id': run.scene_revision_id,
        'scene_content_hash': run.scene_content_hash,
        'segment_geometries': ordered,
        'geometric_length_m': total,
        'created_at_utc': created_at_utc,
    }
    provisional = CableRunGeometry.model_construct(
        **canonicalize_payload(
            CableRunGeometry,
            dict(**payload, semantic_sha256='0' * 64),
        )
    )
    return CableRunGeometry(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class CableRunGeometryFreshness(BaseModel):
    """Read-only drift check for one persisted geometry (never rewrites)."""

    model_config = ConfigDict(frozen=True)

    geometry_id: str
    version: str
    run_id: str
    status: CableRunGeometryFreshnessStatus
    reasons: tuple[str, ...] = ()


def evaluate_cable_run_geometry_freshness(
    geometry: CableRunGeometry,
    *,
    run: CableRun | None,
    scene_content_hash: str,
    present_surface_ids: Sequence[str] = (),
) -> CableRunGeometryFreshness:
    """Report whether a saved geometry still maps onto current truth.

    Shares the ``CableRun`` freshness gate vocabulary (current/stale/
    missing): a geometry whose pinned run record has moved is stale, a
    geometry pinning an absent run or traversing an absent surface is
    missing. Stale or missing geometry must never be presented as the
    current route.
    """

    reasons: list[str] = []
    status: CableRunGeometryFreshnessStatus = 'current'
    if run is None:
        status = 'missing'
        reasons.append(
            'pinned cable run no longer resolves: '
            f'{geometry.run_id}/{geometry.run_version}'
        )
    else:
        if run.semantic_sha256 != geometry.run_semantic_sha256:
            status = 'stale'
            reasons.append(
                'pinned cable run record changed since the geometry '
                'was recorded'
            )
        if (
            run.scene_revision_id != geometry.scene_revision_id
            or run.scene_content_hash != geometry.scene_content_hash
        ):
            status = 'stale'
            reasons.append(
                'geometry and run pin different scene revisions'
            )
    if geometry.scene_content_hash != scene_content_hash:
        status = 'stale' if status == 'current' else status
        reasons.append(
            'scene content hash changed since the geometry was recorded'
        )
    present = set(present_surface_ids)
    for segment in geometry.segment_geometries:
        for surface_id in segment.traversed_surface_ids:
            if surface_id not in present:
                status = 'missing'
                reasons.append(
                    'traversed surface absent: '
                    f'{surface_id} (segment {segment.segment_sequence})'
                )
    return CableRunGeometryFreshness(
        geometry_id=geometry.geometry_id,
        version=geometry.version,
        run_id=geometry.run_id,
        status=status,
        reasons=tuple(reasons),
    )


class CableRunSegmentDivergence(BaseModel):
    """Declared-vs-geometric comparison for one segment."""

    model_config = ConfigDict(frozen=True)

    segment_sequence: int
    declared_length_m: float
    geometric_length_m: float
    delta_m: float
    divergent: bool
    declared_path_kind: CablePathKind
    recorded_concealment_kind: CablePathKind
    concealment_mismatch: bool


class CableRunGeometryDivergence(BaseModel):
    """Declared-vs-geometric comparison for a whole run.

    Both number sets stay visible: the run's declared lengths (the
    installation/BOM authority) and the geometry's integrated polyline
    lengths (the recorded route shape). The service loop has no geometry
    by construction, so totals compare declared segment lengths to
    geometric lengths and carry the loop as its own figure.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    run_version: str
    geometry_id: str
    geometry_version: str
    segments: tuple[CableRunSegmentDivergence, ...]
    #: Declared segments with no recorded segment geometry.
    unregistered_sequences: tuple[int, ...]
    declared_segment_total_m: float
    service_loop_m: float
    declared_total_length_m: float
    geometric_length_m: float
    total_delta_m: float
    divergent: bool

    @model_validator(mode='after')
    def consistent(self) -> 'CableRunGeometryDivergence':
        sequences = [segment.segment_sequence for segment in self.segments]
        if sequences != sorted(sequences):
            raise ValueError('divergence segments must be ordered')
        return self


def evaluate_cable_run_geometry_divergence(
    run: CableRun,
    geometry: CableRunGeometry,
    *,
    tolerance_m: float = GEOMETRY_LENGTH_TOLERANCE_M,
) -> CableRunGeometryDivergence:
    """Compare the recorded route shape against the declared lengths.

    A difference is a *warning surface*, not a correction: vertical
    routing, unaccounted bends, slack and real as-built measurement all
    legitimately separate the two numbers, and nothing here rewrites the
    recorded installation length or any BOM derived from it.
    """

    segments: list[CableRunSegmentDivergence] = []
    unregistered: list[int] = []
    geometric_total = 0.0
    for segment in run.segments:
        segment_geometry = geometry.segment_geometry(segment.sequence)
        if segment_geometry is None:
            unregistered.append(segment.sequence)
            continue
        geometric = segment_geometry.polyline_length_m()
        geometric_total += geometric
        delta = geometric - segment.length_m
        segments.append(
            CableRunSegmentDivergence(
                segment_sequence=segment.sequence,
                declared_length_m=segment.length_m,
                geometric_length_m=geometric,
                delta_m=delta,
                divergent=abs(delta) > tolerance_m,
                declared_path_kind=segment.path_kind,
                recorded_concealment_kind=(
                    segment_geometry.concealment_kind
                ),
                concealment_mismatch=(
                    segment_geometry.concealment_kind != segment.path_kind
                ),
            )
        )
    declared_segment_total = sum(
        segment.length_m for segment in run.segments
    )
    total_delta = geometric_total - declared_segment_total
    return CableRunGeometryDivergence(
        run_id=run.run_id,
        run_version=run.version,
        geometry_id=geometry.geometry_id,
        geometry_version=geometry.version,
        segments=tuple(segments),
        unregistered_sequences=tuple(unregistered),
        declared_segment_total_m=declared_segment_total,
        service_loop_m=run.service_loop_m,
        declared_total_length_m=run.total_length_m,
        geometric_length_m=geometric_total,
        total_delta_m=total_delta,
        divergent=abs(total_delta) > tolerance_m
        or any(segment.divergent for segment in segments),
    )


__all__ = [
    'CABLE_RUN_GEOMETRY_AUTHORITY_VERSION',
    'GEOMETRY_LENGTH_TOLERANCE_M',
    'CableRunGeometry',
    'CableRunGeometryDivergence',
    'CableRunGeometryFrame',
    'CableRunGeometryFreshness',
    'CableRunGeometryFreshnessStatus',
    'CableRunGeometryRecordKind',
    'CableRunGeometrySource',
    'CableRunSegmentDivergence',
    'CableRunSegmentGeometry',
    'build_cable_run_geometry',
    'evaluate_cable_run_geometry_divergence',
    'evaluate_cable_run_geometry_freshness',
]
