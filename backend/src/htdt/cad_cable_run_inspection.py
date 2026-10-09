"""Read-only cable-run listing projection (#1011, M1).

Surfaces every persisted ``CableRun`` of a document with the facts an
operator needs to understand the recorded wiring: which endpoints are
exactly bound to live scene entities (and where those entities stand),
which signal-path edge the run realizes, per-segment path kinds and
declared lengths, the service loop, the run's full version lineage, and
its current/stale/missing freshness against the head revision.

The listing is also the honesty boundary for the 3D surface: endpoint
positions are reported only for endpoints exactly bound to an entity that
exists in the current head document — nothing here infers or invents a
route, and a run with no ``CableRunGeometry`` reports
``route_state='unregistered'`` so surfaces can label the in-between
``経路形状未登録`` instead of drawing a made-up path.
"""

from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from .cad_cable_run import (
    CableMedium,
    CablePathKind,
    CableRun,
    CableRunFreshnessStatus,
    CableRunKind,
    evaluate_cable_run_freshness,
)
from .cad_cable_run_geometry import (
    CableRunGeometry,
    CableRunGeometryDivergence,
    CableRunGeometryFreshnessStatus,
    CableRunGeometryRecordKind,
    CableRunGeometrySource,
    evaluate_cable_run_geometry_divergence,
    evaluate_cable_run_geometry_freshness,
)
from .cad_cable_run_geometry_repository import CadCableRunGeometryRepository
from .cad_cable_run_repository import CadCableRunRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3, SceneDocument
from .cad_signal_path_repository import CadSignalPathRepository


#: Endpoint binding state against the current head document. ``bound`` =
#: entity_id set and the entity exists now (position reportable);
#: ``unbound`` = the endpoint names a non-scene location (panel, rack) so
#: no position can exist; ``missing`` = entity_id set but the entity is
#: gone — the run is drifting.
CableEndpointBindingStatus = Literal['bound', 'unbound', 'missing']

#: Route display state. ``unregistered`` = no geometry at all — the only
#: honest render is endpoints + the unregistered label; ``registered`` =
#: a current geometry exists (its waypoints may be drawn); ``stale`` = a
#: geometry exists but failed its freshness gate and must not be shown as
#: the current route.
CableRouteState = Literal['unregistered', 'registered', 'stale']


class CableEndpointBinding(BaseModel):
    """Where one recorded endpoint stands against the head document."""

    model_config = ConfigDict(frozen=True)

    label: str
    entity_id: str | None
    status: CableEndpointBindingStatus
    #: Entity position in the head document — present only when bound.
    position: Position3 | None = None


class CableRunSegmentListing(BaseModel):
    """One declared segment plus its geometry-registration state."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    path_kind: CablePathKind
    declared_length_m: float
    description: str | None
    geometry_registered: bool
    geometric_length_m: float | None = None


class CableRunVersionEntry(BaseModel):
    """One entry of the run's append-only lineage."""

    model_config = ConfigDict(frozen=True)

    version: str
    created_at_utc: str
    semantic_sha256: str
    is_listed: bool


class CableRunGeometryListing(BaseModel):
    """The geometry record linked to a listed run, when one exists."""

    model_config = ConfigDict(frozen=True)

    geometry_id: str
    version: str
    semantic_sha256: str
    record_kinds: tuple[CableRunGeometryRecordKind, ...]
    sources: tuple[CableRunGeometrySource, ...]
    freshness_status: CableRunGeometryFreshnessStatus
    freshness_reasons: tuple[str, ...]
    divergence: CableRunGeometryDivergence


class CableRunInspection(BaseModel):
    """One persisted run, resolved for listing and honest 3D display."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    version: str
    label: str
    kind: CableRunKind
    medium: CableMedium
    gauge: str | None
    from_endpoint: CableEndpointBinding
    to_endpoint: CableEndpointBinding
    signal_path_edge_id: str | None
    #: ``None`` when the run declares no edge binding; True/False = the
    #: bound edge still resolves / no longer resolves.
    signal_path_edge_present: bool | None
    segments: tuple[CableRunSegmentListing, ...]
    service_loop_m: float
    total_length_m: float
    freshness_status: CableRunFreshnessStatus
    freshness_reasons: tuple[str, ...]
    versions: tuple[CableRunVersionEntry, ...]
    geometry: CableRunGeometryListing | None
    route_state: CableRouteState
    scene_revision_id: str
    scene_content_hash: str


def scene_surface_ids(document: SceneDocument) -> tuple[str, ...]:
    """Semantic surface ids a recorded route may traverse.

    Wall-topology walls and openings plus the room-authoring primitives
    (soffits, risers, partial-height walls, adjacent regions) — the set a
    ``traversed_surface_ids`` freshness check resolves against.
    """

    ids: set[str] = set()
    if document.wall_topology is not None:
        ids.update(wall.wall_id for wall in document.wall_topology.walls)
        ids.update(
            opening.opening_id for opening in document.wall_topology.openings
        )
    if document.room_authoring is not None:
        authoring = document.room_authoring
        ids.update(item.soffit_id for item in authoring.soffits)
        ids.update(item.riser_id for item in authoring.risers)
        ids.update(item.wall_id for item in authoring.partial_walls)
        ids.update(item.region_id for item in authoring.adjacent_regions)
    return tuple(sorted(ids))


def _endpoint_binding(
    endpoint_label: str,
    entity_id: str | None,
    *,
    entities: dict[str, object],
) -> CableEndpointBinding:
    if entity_id is None:
        return CableEndpointBinding(
            label=endpoint_label, entity_id=None, status='unbound'
        )
    entity = entities.get(entity_id)
    if entity is None:
        return CableEndpointBinding(
            label=endpoint_label, entity_id=entity_id, status='missing'
        )
    return CableEndpointBinding(
        label=endpoint_label,
        entity_id=entity_id,
        status='bound',
        position=getattr(entity, 'position', None),
    )


def _edge_ids(
    signal_path_repository: CadSignalPathRepository | None,
    document_id: str,
) -> tuple[str, ...]:
    if signal_path_repository is None:
        return ()
    ids: list[str] = []
    for path in signal_path_repository.list_paths(document_id):
        ids.extend(edge.edge_id for edge in path.edges)
    return tuple(ids)


def inspect_cable_runs(
    *,
    scene_repository: SceneRepository,
    document_id: str,
    cable_run_repository: CadCableRunRepository | None = None,
    geometry_repository: CadCableRunGeometryRepository | None = None,
    signal_path_repository: CadSignalPathRepository | None = None,
) -> tuple[CableRunInspection, ...]:
    """Resolve the document's persisted runs against the current head.

    Freshness is computed against the *current head* content hash — a run
    pinned to an older revision reports ``stale`` instead of being shown
    as if nothing moved. Signal-path edges are a separate authority; when
    no repository is supplied every edge binding reports absent rather
    than guessing resolvable.
    """

    cable_runs = (
        cable_run_repository
        if cable_run_repository is not None
        else CadCableRunRepository(scene_repository)
    )
    head = scene_repository.current_head(document_id)
    document = head.document if head is not None else None
    head_hash = head.content_hash if head is not None else ''
    entities = (
        {entity.entity_id: entity for entity in document.entities}
        if document is not None
        else {}
    )
    present_surface_ids = (
        scene_surface_ids(document) if document is not None else ()
    )
    edge_ids = _edge_ids(signal_path_repository, document_id)
    edge_id_set = set(edge_ids)

    inspections: list[CableRunInspection] = []
    for run in cable_runs.list_runs(document_id):
        freshness = evaluate_cable_run_freshness(
            run,
            scene_content_hash=head_hash,
            present_entity_ids=tuple(entities),
            present_signal_path_edge_ids=edge_ids,
        )
        geometry = (
            geometry_repository.latest_geometry_for_run(
                run.run_id, run.version
            )
            if geometry_repository is not None
            else None
        )
        geometry_listing: CableRunGeometryListing | None = None
        if geometry is not None:
            geometry_freshness = evaluate_cable_run_geometry_freshness(
                geometry,
                run=run,
                scene_content_hash=head_hash,
                present_surface_ids=present_surface_ids,
            )
            divergence = evaluate_cable_run_geometry_divergence(
                run, geometry
            )
            geometry_listing = CableRunGeometryListing(
                geometry_id=geometry.geometry_id,
                version=geometry.version,
                semantic_sha256=geometry.semantic_sha256,
                record_kinds=tuple(
                    dict.fromkeys(
                        segment.record_kind
                        for segment in geometry.segment_geometries
                    )
                ),
                sources=tuple(
                    dict.fromkeys(
                        segment.source
                        for segment in geometry.segment_geometries
                    )
                ),
                freshness_status=geometry_freshness.status,
                freshness_reasons=geometry_freshness.reasons,
                divergence=divergence,
            )
        segments = tuple(
            CableRunSegmentListing(
                sequence=segment.sequence,
                path_kind=segment.path_kind,
                declared_length_m=segment.length_m,
                description=segment.description,
                geometry_registered=(
                    geometry is not None
                    and geometry.segment_geometry(segment.sequence)
                    is not None
                ),
                geometric_length_m=(
                    None
                    if geometry is None
                    or geometry.segment_geometry(segment.sequence) is None
                    else geometry.segment_geometry(
                        segment.sequence
                    ).polyline_length_m()
                ),
            )
            for segment in run.segments
        )
        lineage = tuple(
            CableRunVersionEntry(
                version=version.version,
                created_at_utc=version.created_at_utc,
                semantic_sha256=version.semantic_sha256,
                is_listed=version.version == run.version,
            )
            for version in cable_runs.list_run_versions(run.run_id)
        )
        if geometry_listing is None:
            route_state: CableRouteState = 'unregistered'
        elif geometry_listing.freshness_status == 'current':
            route_state = 'registered'
        else:
            route_state = 'stale'
        inspections.append(
            CableRunInspection(
                run_id=run.run_id,
                version=run.version,
                label=run.label,
                kind=run.kind,
                medium=run.medium,
                gauge=run.gauge,
                from_endpoint=_endpoint_binding(
                    run.from_endpoint.label,
                    run.from_endpoint.entity_id,
                    entities=entities,
                ),
                to_endpoint=_endpoint_binding(
                    run.to_endpoint.label,
                    run.to_endpoint.entity_id,
                    entities=entities,
                ),
                signal_path_edge_id=run.signal_path_edge_id,
                signal_path_edge_present=(
                    None
                    if run.signal_path_edge_id is None
                    else run.signal_path_edge_id in edge_id_set
                ),
                segments=segments,
                service_loop_m=run.service_loop_m,
                total_length_m=run.total_length_m,
                freshness_status=freshness.status,
                freshness_reasons=freshness.reasons,
                versions=lineage,
                geometry=geometry_listing,
                route_state=route_state,
                scene_revision_id=run.scene_revision_id,
                scene_content_hash=run.scene_content_hash,
            )
        )
    return tuple(inspections)


__all__ = [
    'CableEndpointBinding',
    'CableEndpointBindingStatus',
    'CableRouteState',
    'CableRunGeometryListing',
    'CableRunInspection',
    'CableRunSegmentListing',
    'CableRunVersionEntry',
    'inspect_cable_runs',
    'scene_surface_ids',
]
