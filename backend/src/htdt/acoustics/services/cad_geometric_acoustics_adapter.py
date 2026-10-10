
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version as distribution_version
from itertools import product
from math import acos, atan2, degrees, isfinite, sqrt
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

from ..domain.acoustic_benchmark import (
    AcousticMaterial,
    GeometricIncidenceCondition,
)
from ..domain.cad_acoustic_snapshot import AcousticPredictionRequest, AcousticSceneSnapshot
from ..persistence.cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from ..domain.cad_acoustic_solver_adapter import (
    AcousticSolverAdapterDescriptor,
    AcousticSolverDispatchBinding,
)
from ..persistence.cad_acoustic_solver_dispatch_repository import CadAcousticSolverDispatchRepository
from ..domain.cad_solver_capability_manifest import (
    SolverCapabilityManifest,
    build_solver_capability_manifest,
    derive_solver_capability_rows,
)
from ..domain.cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from ...cad_directivity import DirectivityDataset, evaluate_directivity
from ..domain.cad_geometric_acoustics_portal import (
    GeometricPortalAperture,
    GeometricPortalGraph,
    PORTAL_SIDE_SEMANTICS,
    _region_shell_triangles,
    compile_portal_graph,
    compile_single_portal_aperture,
    directed_region_reachable,
    enumerate_simple_directed_region_paths,
    region_membership_with_portal_cap,
    region_membership_with_portal_caps,
    region_segment_membership_with_portal_caps,
    resolve_direct_portal_crossing,
)
from ...cad_repository import SceneRepository
from ...cad_scene import Direction3, Position3
from ...cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from ...r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    BoundaryTerminationAuthority,
    CompiledSurfaceMapping,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
)
from ...canonical_json import canonical_sha256 as _semantic_hash
from ...clock import utc_now_iso as _utc_now
from ...occluder_grid_index import _IndexedOccluderRows
from ..domain.cad_geometric_acoustics_contracts import (
    AxisName,
    BoundaryIncidenceEvaluation,
    BoundaryMaterialContribution,
    DETERMINISTIC_GA_ADAPTER_ID,
    DETERMINISTIC_GA_ADAPTER_VERSION,
    DETERMINISTIC_GA_AUTHORITY_VERSION,
    DETERMINISTIC_GA_SCHEMA_VERSION,
    DETERMINISTIC_PATHS_OBSERVABLE,
    DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
    DeterministicAcousticPath,
    DeterministicGaConfiguration,
    DeterministicGaExecutionInput,
    DeterministicGaReceiverInput,
    DeterministicGaSourceInput,
    DeterministicGaUnsupportedError,
    DeterministicPathArtifact,
    DeterministicPathBandQuantity,
    DeterministicPathInteraction,
    GeometricMaterialAuthority,
    GeometricSurfacePlane,
    GeometryPolicy,
    HTDT_PLANAR_ENGINE_ID,
    HTDT_PLANAR_ENGINE_VERSION,
    HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF,
    HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION,
    HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF,
    HTDT_PORTAL_DIRECT_ENGINE_ID,
    HTDT_PORTAL_DIRECT_ENGINE_VERSION,
    HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF,
    HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
    HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION,
    HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF,
    HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION,
    HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF,
    HTDT_PORTAL_HIGH_ORDER_CHAIN_MAXIMUM_EVALUATIONS,
    HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION,
    HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF,
    HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION,
    HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF,
    HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION,
    HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF,
    MIN_SUPPORTED_FEATURE_M,
    PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT,
    PYROOMACOUSTICS_ENGINE_ID,
    PYROOMACOUSTICS_ENGINE_VERSION,
    PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF,
    PathCandidateDecision,
    PathType,
    PlaneSide,
    PortalRegionSegmentEvidence,
    RejectedPathCandidate,
    SourceDirectivityContribution,
    UnsupportedCapabilityReason,
    _INCIDENCE_CONTRIBUTION_KEYS,
    _authority_ref,
    _cross,
    _direction,
    _distance,
    _dot,
    _frozen_model_json_payload,
    _norm,
    _point,
    _point_strictly_inside_box,
    _portal_segment_region_ids,
    _position_tuple,
    _prune_band_payload,
    _ref_key,
    _round_float,
    _rounded_direction,
    _rounded_position,
    _unit,
    _vector,
    build_deterministic_ga_configuration,
)
from ...cad_equipment import (
    FrequencyDomain,
)

# Smallest compiled-triangle feature scale the deterministic verdict kernels
# support (docs/R150_DETERMINISTIC_GA_ADAPTER.md §"Supported feature scale").
# REV28 showed occluder verdicts diverging between scaled and unscaled
# tolerance paths below ~1.5 mm; 2 mm pins the supported regime just above
# that boundary so finer geometry fails closed instead of returning a
# verdict we cannot stand behind.

# Declared enumeration bound for the order-5/6 lane: the exact number of
# (order, plane-sequence) candidates the engine may enumerate per
# source/receiver pair before it fails closed. 2^21 admits the declared
# test-scale scenes while larger scenes fail instead of running unbounded.

@dataclass(frozen=True)
class NativeImageSource:
    position_local_m: tuple[float, float, float]

class DeterministicImageSourceEngine(Protocol):
    engine_id: str
    engine_version: str
    candidate_source_commit: str | None
    solver_implementation_ref: ExactExternalAuthorityRef

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        ...

class HtdtPlanarImageSourceEngine:
    """Deterministic HTDT analytic kernel for bounded arbitrary-planar images."""

    engine_id = HTDT_PLANAR_ENGINE_ID
    candidate_source_commit = None

    def __init__(self, maximum_reflection_order: Literal[1, 2] = 1) -> None:
        self.maximum_reflection_order = maximum_reflection_order
        if maximum_reflection_order == 2:
            self.engine_version = HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION
            self.solver_implementation_ref = (
                HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF
            )
        else:
            self.engine_version = HTDT_PLANAR_ENGINE_VERSION
            self.solver_implementation_ref = HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'HTDT general-planar kernel does not construct or approximate a shoebox'
        )

class HtdtPortalDirectEngine:
    """Bounded exact engine marker for one directed Portal crossing."""

    engine_id = HTDT_PORTAL_DIRECT_ENGINE_ID
    engine_version = HTDT_PORTAL_DIRECT_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError('explicit Portal direct engine does not execute a shoebox')

class HtdtPortalGraphDirectEngine:
    """Bounded exact engine marker for directed simple-path Portal graph propagation."""

    engine_id = HTDT_PORTAL_DIRECT_ENGINE_ID
    engine_version = HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError('explicit Portal graph direct engine does not execute a shoebox')

class HtdtPortalFirstOrderReflectionEngine:
    """Exact engine marker for one ordinary reflection plus one Portal crossing."""

    engine_id = HTDT_PORTAL_FIRST_ORDER_ENGINE_ID
    engine_version = HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError('explicit Portal reflection engine does not execute a shoebox')

class HtdtPortalSpecularGraphEngine:
    """Exact engine marker for bounded interleaved reflections on the directed Portal graph."""

    engine_id = HTDT_PORTAL_FIRST_ORDER_ENGINE_ID
    engine_version = HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'explicit Portal graph specular engine does not execute a shoebox'
        )

class HtdtPortalSpecularChainGraphEngine:
    """Exact engine marker for bounded-order specular chains on the directed Portal graph."""

    engine_id = HTDT_PORTAL_FIRST_ORDER_ENGINE_ID
    engine_version = HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'explicit Portal graph specular chain engine does not execute a shoebox'
        )

class HtdtPortalHighOrderSpecularChainGraphEngine:
    """Exact engine marker for bounded order-5/6 specular chains on the
    directed Portal graph, under the declared enumeration bound."""

    engine_id = HTDT_PORTAL_FIRST_ORDER_ENGINE_ID
    engine_version = HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = (
        HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF
    )

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'explicit Portal graph high-order specular chain engine does not '
            'execute a shoebox'
        )

# ``engine_id`` is the engine-family id: several implementations share one
# family and are distinguished by ``engine_version``. The (id, version)
# pair — not the id alone — names one exact solver implementation, and
# every artifact/digest/provenance surface carries both fields.
_ENGINE_SOLVER_REF_BY_ID_VERSION = {
    (
        HTDT_PLANAR_ENGINE_ID,
        HTDT_PLANAR_ENGINE_VERSION,
    ): HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF,
    (
        HTDT_PLANAR_ENGINE_ID,
        HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION,
    ): HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_DIRECT_ENGINE_ID,
        HTDT_PORTAL_DIRECT_ENGINE_VERSION,
    ): HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_DIRECT_ENGINE_ID,
        HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION,
    ): HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
        HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION,
    ): HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
        HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION,
    ): HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
        HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION,
    ): HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF,
    (
        HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
        HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION,
    ): HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF,
    (
        PYROOMACOUSTICS_ENGINE_ID,
        PYROOMACOUSTICS_ENGINE_VERSION,
    ): PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF,
}
_REGISTERED_ENGINE_IDS = frozenset(
    engine_id for engine_id, _version in _ENGINE_SOLVER_REF_BY_ID_VERSION
)

class PyroomacousticsImageSourceEngine:
    """Candidate engine bridge. pyroomacoustics objects never cross this adapter."""

    engine_id = PYROOMACOUSTICS_ENGINE_ID
    engine_version = PYROOMACOUSTICS_ENGINE_VERSION
    candidate_source_commit = PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT
    solver_implementation_ref = PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        import numpy as np
        import pyroomacoustics as pra

        installed = distribution_version('pyroomacoustics')
        if installed != self.engine_version:
            raise RuntimeError(
                f'expected pyroomacoustics {self.engine_version}, installed {installed}'
            )
        room = pra.ShoeBox(
            list(dimensions_m),
            fs=16000,
            materials=pra.Material(0.0),
            max_order=1,
        )
        room.add_source(list(source_local_m))
        room.add_microphone(list(receiver_local_m))
        room.image_source_model()
        images = np.asarray(room.sources[0].images, dtype=np.float64).T
        if images.ndim != 2 or images.shape[1] != 3:
            raise RuntimeError(
                f'unexpected pyroomacoustics image-source shape: {images.shape}'
            )
        return tuple(
            NativeImageSource(
                position_local_m=(
                    float(image[0]),
                    float(image[1]),
                    float(image[2]),
                )
            )
            for image in images
        )

def _validate_dispatch_chain(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    configuration: DeterministicGaConfiguration,
) -> None:
    if dispatch.state != 'READY':
        raise ValueError('deterministic GA execution requires READY dispatch')
    if request.requested_observables != ('deterministic_paths',):
        raise ValueError(
            'deterministic GA foundation requires exactly deterministic_paths observable'
        )
    if (
        request.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA request does not bind exact snapshot')
    if (
        dispatch.prediction_request_id != request.request_id
        or dispatch.prediction_request_semantic_sha256
        != request.request_semantic_sha256
        or dispatch.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or dispatch.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA dispatch/request/snapshot exact identity mismatch')
    if (
        dispatch.adapter_descriptor_id != descriptor.descriptor_id
        or dispatch.adapter_descriptor_semantic_sha256 != descriptor.semantic_sha256
    ):
        raise ValueError('GA dispatch adapter descriptor identity mismatch')
    if descriptor.adapter_id != DETERMINISTIC_GA_ADAPTER_ID:
        raise ValueError('unexpected deterministic GA adapter id')
    if descriptor.adapter_version != DETERMINISTIC_GA_ADAPTER_VERSION:
        raise ValueError('unexpected deterministic GA adapter version')
    if descriptor.acoustic_domain != 'geometric':
        raise ValueError('deterministic GA adapter descriptor must be geometric')
    if dispatch.solver_implementation_ref != descriptor.solver_implementation_ref:
        raise ValueError('GA solver implementation exact authority mismatch')
    if dispatch.solver_configuration_ref != configuration.as_external_ref():
        raise ValueError('GA solver configuration exact authority mismatch')

def _room_boundary_shell_metrics(
    compiled: R120CompiledGeometry,
    mappings: Sequence[CompiledSurfaceMapping],
) -> tuple[int, int, float]:
    triangle_indices = tuple(
        sorted(
            {
                index
                for mapping in mappings
                for index in mapping.compiled_triangle_indices
            }
        )
    )
    edge_counts: dict[tuple[int, int], int] = {}
    signed_volume_times_six = 0.0
    for index in triangle_indices:
        triangle = compiled.triangles[index]
        for left, right in (
            (triangle.a, triangle.b),
            (triangle.b, triangle.c),
            (triangle.c, triangle.a),
        ):
            key = (left, right) if left < right else (right, left)
            edge_counts[key] = edge_counts.get(key, 0) + 1
        a, b, c = _triangle_vertices(compiled, index)
        signed_volume_times_six += _dot(a, _cross(b, c))
    boundary_edge_count = sum(1 for count in edge_counts.values() if count == 1)
    non_manifold_edge_count = sum(1 for count in edge_counts.values() if count > 2)
    return (
        boundary_edge_count,
        non_manifold_edge_count,
        abs(signed_volume_times_six) / 6.0,
    )

def _surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    bounds = compiled.bounding_volume
    plane_candidates = (
        ('x', 'min', float(bounds.min_x_m)),
        ('x', 'max', float(bounds.max_x_m)),
        ('y', 'min', float(bounds.min_y_m)),
        ('y', 'max', float(bounds.max_y_m)),
        ('z', 'min', float(bounds.min_z_m)),
        ('z', 'max', float(bounds.max_z_m)),
    )
    vertex_indices: set[int] = set()
    for triangle_index in mapping.compiled_triangle_indices:
        triangle = compiled.triangles[triangle_index]
        vertex_indices.update((triangle.a, triangle.b, triangle.c))
    if not vertex_indices:
        raise ValueError(
            f'room boundary surface has no compiled triangles: {mapping.source_surface_id}'
        )

    matches: list[tuple[AxisName, PlaneSide, float]] = []
    for axis, side, coordinate in plane_candidates:
        axis_index = {'x': 0, 'y': 1, 'z': 2}[axis]
        if all(
            abs(
                (
                    compiled.vertices[index].x_m,
                    compiled.vertices[index].y_m,
                    compiled.vertices[index].z_m,
                )[axis_index]
                - coordinate
            )
            <= tolerance_m
            for index in vertex_indices
        ):
            matches.append((axis, side, coordinate))  # type: ignore[arg-type]
    if len(matches) != 1:
        raise ValueError(
            'candidate pyroomacoustics adapter supports only one exact axis-aligned '
            f'box plane per semantic room surface: {mapping.source_surface_id}'
        )
    axis, side, coordinate = matches[0]
    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        axis=axis,
        side=side,
        coordinate_m=coordinate,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )

def _general_surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    indices = tuple(sorted(mapping.compiled_triangle_indices))
    if not indices:
        raise ValueError(
            f'semantic surface has no compiled triangles: {mapping.source_surface_id}'
        )

    all_vertices: list[tuple[float, float, float]] = []
    reference_normal: tuple[float, float, float] | None = None
    for triangle_index in indices:
        triangle = _triangle_vertices(compiled, triangle_index)
        edge1 = _vector(triangle[0], triangle[1])
        edge2 = _vector(triangle[0], triangle[2])
        raw_normal = _cross(edge1, edge2)
        if _norm(raw_normal) <= tolerance_m * tolerance_m:
            raise ValueError(
                f'degenerate triangle in semantic surface {mapping.source_surface_id}'
            )
        normal = _unit(raw_normal)
        if reference_normal is None:
            for component in normal:
                if abs(component) > tolerance_m:
                    if component < 0.0:
                        normal = tuple(-value for value in normal)
                    break
            reference_normal = normal
        all_vertices.extend(triangle)

    assert reference_normal is not None
    point = min(set(all_vertices))
    for vertex in all_vertices:
        signed_distance = _dot(_vector(point, vertex), reference_normal)
        if abs(signed_distance) > tolerance_m:
            raise ValueError(
                f'nonplanar semantic surface {mapping.source_surface_id} exceeds '
                f'declared tolerance {tolerance_m}'
            )

    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        point_m=_point(point),
        normal=_direction(reference_normal),
        compiled_triangle_indices=indices,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )

def _validate_supported_topology(
    *,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
) -> None:
    if portal_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'deterministic GA foundation does not simplify or traverse explicit/unknown portals',
        )
    if boundary_termination_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_BOUNDARY_TERMINATION',
            'deterministic GA foundation does not simplify explicit/unknown terminations',
        )
    if len(region_authority.declarations) != 1:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'deterministic GA foundation supports exactly one explicit acoustic region',
        )

def _compile_single_portal_execution_input_legacy(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    configuration: DeterministicGaConfiguration,
    source_region_bindings: dict[str, str] | None,
    receiver_region_bindings: dict[str, str] | None,
) -> DeterministicGaExecutionInput:
    """Reproduce the persisted PR #265 two-region / one-Portal execution authority."""

    if boundary_termination_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_BOUNDARY_TERMINATION',
            'multi-region deterministic GA does not reinterpret or transmit '
            'nontrivial BoundaryTermination authority',
        )
    if (
        len(region_authority.declarations) != 2
        or portal_authority.declaration_mode != 'explicit_list'
        or len(portal_authority.declarations) != 1
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'bounded multi-region GA supports exactly two explicit AcousticRegions '
            'connected by exactly one explicit Portal',
        )
    if configuration.maximum_portal_crossings != 1:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'bounded multi-region GA requires maximum_portal_crossings=1',
        )
    if configuration.maximum_reflection_order != 0:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'Portal propagation lane is direct-only; reflected Portal paths are unsupported',
        )
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError(
            'multi-region deterministic GA rejects approximated/dropped R120 geometry'
        )
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError(
            'multi-region deterministic GA requires exact R120 geometry preservation'
        )
    supported_global_topology_markers = {
        'compiled_non_manifold_edges',
        'input_semantic_geometry_not_compiler_contract_ready',
    }
    unsupported_readiness = (
        set(compiled_geometry.readiness.unresolved_conditions)
        - supported_global_topology_markers
    )
    if unsupported_readiness:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'R120 geometry has unresolved conditions outside the exact two-region '
            f'interface allowance: {sorted(unsupported_readiness)}',
        )
    if snapshot.environment is None or snapshot.environment.sound_speed_m_s is None:
        raise ValueError('deterministic GA path delay requires exact sound-speed authority')
    sound_speed = float(snapshot.environment.sound_speed_m_s)
    for center in configuration.frequency_centers_hz:
        if not request.requested_frequency_domain.contains(center):
            raise ValueError(
                f'GA frequency center {center} is outside requested frequency domain'
            )

    declaration = portal_authority.declarations[0]
    if declaration.state != 'open':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_STATE',
            'Portal propagation requires explicit state=open; closed/legacy state is fail-closed',
        )
    if declaration.region_side_semantics != PORTAL_SIDE_SEMANTICS:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_ORIENTATION',
            'Portal propagation requires explicit directed region-side semantics',
        )
    try:
        aperture = compile_single_portal_aperture(
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            declaration=declaration,
            tolerance_m=configuration.geometric_tolerance_m,
        )
    except ValueError as exc:
        message = str(exc)
        reason: UnsupportedCapabilityReason = (
            'UNSUPPORTED_PORTAL_ORIENTATION'
            if 'orientation' in message or 'first region to second region' in message
            else 'UNSUPPORTED_PORTAL_APERTURE'
        )
        raise DeterministicGaUnsupportedError(reason, message) from exc

    source_ids = {item.source_entity_id for item in snapshot.sources}
    receiver_ids = {item.receiver_id for item in snapshot.receivers}
    if (
        source_region_bindings is None
        or set(source_region_bindings) != source_ids
        or receiver_region_bindings is None
        or set(receiver_region_bindings) != receiver_ids
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_MEMBERSHIP',
            'multi-region GA requires explicit source and receiver AcousticRegion '
            'bindings for every endpoint; membership is never inferred',
        )

    region_by_id = {item.region_id: item for item in region_authority.declarations}
    if (
        set(source_region_bindings.values()) - set(region_by_id)
        or set(receiver_region_bindings.values()) - set(region_by_id)
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_MEMBERSHIP',
            'source/receiver binding references a region outside exact AcousticRegion authority',
        )

    def require_exact_membership(
        point: tuple[float, float, float],
        *,
        bound_region_id: str,
        label: str,
    ) -> None:
        decisions = {
            region_id: region_membership_with_portal_cap(
                compiled_geometry=compiled_geometry,
                region=region,
                aperture=aperture,
                point=point,
                tolerance_m=configuration.geometric_tolerance_m,
            )
            for region_id, region in region_by_id.items()
        }
        if decisions.get(bound_region_id) != 'inside':
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} is not strictly inside explicitly bound region '
                f'{bound_region_id} (membership={decisions.get(bound_region_id)})',
            )
        unexpected = {
            region_id: decision
            for region_id, decision in decisions.items()
            if region_id != bound_region_id and decision != 'outside'
        }
        if unexpected:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} has ambiguous/non-exclusive region membership: {unexpected}',
            )

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    source_inputs: list[DeterministicGaSourceInput] = []
    for source in sorted(snapshot.sources, key=lambda item: item.source_entity_id):
        region_id = source_region_bindings[source.source_entity_id]
        require_exact_membership(
            _position_tuple(source.source_reference_point),
            bound_region_id=region_id,
            label=f'source {source.source_entity_id} acoustic reference point',
        )
        if source.geometric_directivity_state != 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY':
            raise ValueError(
                f'source {source.source_entity_id} lacks exact geometric directivity capability'
            )
        if source.source_axis is None:
            raise ValueError(
                f'source {source.source_entity_id} lacks exact directivity reference axis'
            )
        if (
            source.directivity_dataset_id is None
            or source.directivity_dataset_version is None
            or source.directivity_dataset_sha256 is None
        ):
            raise ValueError(
                'multi-region deterministic GA does not substitute omnidirectional '
                f'directivity for source {source.source_entity_id}'
            )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'exact DirectivityDataset is unresolved for source {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('snapshot/directivity dataset exact identity mismatch')
        source_inputs.append(
            DeterministicGaSourceInput(
                source_entity_id=source.source_entity_id,
                r110_compiled_source_sha256=source.r110_compiled_source_sha256,
                source_reference_point=source.source_reference_point,
                source_axis=source.source_axis,
                directivity_dataset_id=dataset.dataset_id,
                directivity_dataset_version=dataset.version,
                directivity_dataset_sha256=dataset.semantic_sha256,
                acoustic_region_id=region_id,
            )
        )

    receiver_inputs: list[DeterministicGaReceiverInput] = []
    for receiver in sorted(snapshot.receivers, key=lambda item: item.receiver_id):
        region_id = receiver_region_bindings[receiver.receiver_id]
        require_exact_membership(
            _position_tuple(receiver.world_position),
            bound_region_id=region_id,
            label=f'receiver {receiver.receiver_id} position',
        )
        receiver_inputs.append(
            DeterministicGaReceiverInput(
                receiver_id=receiver.receiver_id,
                entity_id=receiver.entity_id,
                world_position=receiver.world_position,
                acoustic_region_id=region_id,
            )
        )
    if not source_inputs or not receiver_inputs:
        raise ValueError('deterministic GA execution requires source and receiver authority')

    bounds = compiled_geometry.bounding_volume
    origin = Position3(
        x_m=bounds.min_x_m,
        y_m=bounds.min_y_m,
        z_m=bounds.min_z_m,
    )
    dimensions = (
        float(bounds.max_x_m - bounds.min_x_m),
        float(bounds.max_y_m - bounds.min_y_m),
        float(bounds.max_z_m - bounds.min_z_m),
    )
    if any(value <= 0.0 for value in dimensions):
        raise ValueError('R120 bounding dimensions must be positive')

    object_triangle_indices = _occluder_object_triangle_indices(
        compiled_geometry
    )
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_sha256': request.request_semantic_sha256,
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_sha256': descriptor.semantic_sha256,
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'region_authority_ref': _authority_ref(region_authority).model_dump(mode='json'),
        'portal_authority_ref': _authority_ref(portal_authority).model_dump(mode='json'),
        'boundary_termination_authority_ref': _authority_ref(
            boundary_termination_authority
        ).model_dump(mode='json'),
        'room_origin_m': origin.model_dump(mode='json'),
        'room_dimensions_m': list(dimensions),
        'boundary_planes': [],
        'geometry_policy': 'general_planar_multi_region_portal_v1',
        'unsupported_reflection_surface_ids': [],
        'portal_apertures': [aperture.model_dump(mode='json')],
        'maximum_portal_crossings': 1,
        'maximum_reflection_order': 0,
        'occluder_triangle_indices': list(object_triangle_indices),
        'sources': [item.model_dump(mode='json', exclude_none=True) for item in source_inputs],
        'receivers': [item.model_dump(mode='json', exclude_none=True) for item in receiver_inputs],
        'sound_speed_m_s': sound_speed,
        'frequency_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'frequency_centers_hz': list(configuration.frequency_centers_hz),
        'geometric_tolerance_m': configuration.geometric_tolerance_m,
        'engine_image_match_tolerance_m': configuration.engine_image_match_tolerance_m,
        'identity_decimal_places': configuration.identity_decimal_places,
        'incidence_exact_angle_tolerance_deg': float(
            configuration.incidence_exact_angle_tolerance_deg
        ),
    }
    digest = _semantic_hash(core)
    return DeterministicGaExecutionInput(
        execution_input_id=f'r150-ga-execution-input:{digest}',
        semantic_sha256=digest,
        **core,
    )

def _compile_multi_region_portal_execution_input(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    configuration: DeterministicGaConfiguration,
    source_region_bindings: dict[str, str] | None,
    receiver_region_bindings: dict[str, str] | None,
) -> DeterministicGaExecutionInput:
    """Compile bounded arbitrary directed AcousticRegion / multi-Portal direct authority."""

    if configuration.portal_traversal_policy is None:
        return _compile_single_portal_execution_input_legacy(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=boundary_termination_authority,
            directivity_datasets=directivity_datasets,
            configuration=configuration,
            source_region_bindings=source_region_bindings,
            receiver_region_bindings=receiver_region_bindings,
        )

    if boundary_termination_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_BOUNDARY_TERMINATION',
            'multi-Portal deterministic GA does not reinterpret or transmit '
            'nontrivial BoundaryTermination authority',
        )
    if configuration.portal_traversal_policy != 'simple_region_path_v1':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'multi-Portal deterministic GA supports simple_region_path_v1 only',
        )
    if configuration.maximum_reflection_order not in (0, 1, 2, 3, 4, 5, 6):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'Portal graph propagation supports direct order 0 or bounded '
            'specular reflection within declared order six',
        )
    if configuration.maximum_reflection_order >= 1:
        if (
            dispatch.solver_implementation_ref
            == HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF
        ):
            if (
                configuration.maximum_reflection_order != 1
                or len(region_authority.declarations) != 2
                or portal_authority.declaration_mode != 'explicit_list'
                or len(portal_authority.declarations) != 1
                or configuration.maximum_portal_crossings != 1
            ):
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_PORTAL_TOPOLOGY',
                    'cross-region reflection under the bounded one-Portal engine '
                    'is bounded to exact 2 AcousticRegions, exact 1 Portal, '
                    'exact 1 Portal crossing, and exact 1 ordinary-surface '
                    'first-order reflection',
                )
        elif (
            dispatch.solver_implementation_ref
            == HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF
        ):
            if configuration.maximum_reflection_order > 2:
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_PORTAL_TOPOLOGY',
                    'Portal graph specular engine authority is bounded to '
                    'second-order reflection; third/fourth-order chains '
                    'require the bounded specular chain authority',
                )
        elif (
            dispatch.solver_implementation_ref
            == HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF
        ):
            if not 3 <= configuration.maximum_reflection_order <= 4:
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_PORTAL_TOPOLOGY',
                    'bounded specular chain Portal engine authority requires a '
                    'declared reflection order of three or four',
                )
        elif (
            dispatch.solver_implementation_ref
            == HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF
        ):
            if configuration.maximum_reflection_order < 5:
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_PORTAL_TOPOLOGY',
                    'bounded high-order specular chain Portal engine '
                    'authority requires a declared reflection order of five '
                    'or six',
                )
        else:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'reflected Portal propagation requires an explicit reflected '
                'Portal graph engine authority',
            )
    if (
        configuration.maximum_portal_crossings is None
        or configuration.maximum_portal_crossings < 1
        or configuration.maximum_portal_crossings > 16
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'Portal graph propagation requires an explicit crossing limit within 1..16',
        )
    if (
        len(region_authority.declarations) < 2
        or portal_authority.declaration_mode != 'explicit_list'
        or not portal_authority.declarations
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'Portal graph propagation requires at least two explicit AcousticRegions '
            'and one or more explicit Portals',
        )
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError(
            'multi-Portal deterministic GA rejects approximated/dropped R120 geometry'
        )
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError(
            'multi-Portal deterministic GA requires exact R120 geometry preservation'
        )
    supported_global_topology_markers = {
        'compiled_non_manifold_edges',
        'input_semantic_geometry_not_compiler_contract_ready',
    }
    unsupported_readiness = (
        set(compiled_geometry.readiness.unresolved_conditions)
        - supported_global_topology_markers
    )
    if unsupported_readiness:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'R120 geometry has unresolved conditions outside the exact Portal-interface '
            f'allowance: {sorted(unsupported_readiness)}',
        )
    if snapshot.environment is None or snapshot.environment.sound_speed_m_s is None:
        raise ValueError('deterministic GA path delay requires exact sound-speed authority')
    sound_speed = float(snapshot.environment.sound_speed_m_s)
    for center in configuration.frequency_centers_hz:
        if not request.requested_frequency_domain.contains(center):
            raise ValueError(
                f'GA frequency center {center} is outside requested frequency domain'
            )

    for declaration in portal_authority.declarations:
        if declaration.state != 'open':
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_STATE',
                f'Portal {declaration.portal_id} requires explicit state=open',
            )
        if declaration.region_side_semantics != PORTAL_SIDE_SEMANTICS:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_ORIENTATION',
                f'Portal {declaration.portal_id} lacks explicit directed region-side semantics',
            )

    try:
        apertures, portal_graph = compile_portal_graph(
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            portal_authority=portal_authority,
            tolerance_m=configuration.geometric_tolerance_m,
            maximum_portal_crossings=configuration.maximum_portal_crossings,
        )
    except ValueError as exc:
        message = str(exc)
        lowered = message.lower()
        if 'orientation' in lowered or 'first region to second region' in lowered:
            reason: UnsupportedCapabilityReason = 'UNSUPPORTED_PORTAL_ORIENTATION'
        elif any(
            token in lowered
            for token in (
                'adjacency',
                'unknown acousticregion',
                'outside exact acousticregion',
                'duplicate portal',
                'parallel portal',
                'portal graph',
            )
        ):
            reason = 'UNSUPPORTED_PORTAL_TOPOLOGY'
        else:
            reason = 'UNSUPPORTED_PORTAL_APERTURE'
        raise DeterministicGaUnsupportedError(reason, message) from exc

    source_ids = {item.source_entity_id for item in snapshot.sources}
    receiver_ids = {item.receiver_id for item in snapshot.receivers}
    if (
        source_region_bindings is None
        or set(source_region_bindings) != source_ids
        or receiver_region_bindings is None
        or set(receiver_region_bindings) != receiver_ids
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_MEMBERSHIP',
            'multi-Portal GA requires explicit source and receiver AcousticRegion '
            'bindings for every endpoint; membership is never inferred',
        )

    region_by_id = {item.region_id: item for item in region_authority.declarations}
    if len(region_by_id) != len(region_authority.declarations):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'AcousticRegion authority contains duplicate region identity',
        )
    if (
        set(source_region_bindings.values()) - set(region_by_id)
        or set(receiver_region_bindings.values()) - set(region_by_id)
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_MEMBERSHIP',
            'source/receiver binding references a region outside exact AcousticRegion authority',
        )

    def require_exact_membership(
        point: tuple[float, float, float],
        *,
        bound_region_id: str,
        label: str,
    ) -> None:
        decisions = {
            region_id: region_membership_with_portal_caps(
                compiled_geometry=compiled_geometry,
                region=region,
                apertures=apertures,
                point=point,
                tolerance_m=configuration.geometric_tolerance_m,
            )
            for region_id, region in region_by_id.items()
        }
        if decisions.get(bound_region_id) != 'inside':
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} is not strictly inside explicitly bound region '
                f'{bound_region_id} (membership={decisions.get(bound_region_id)})',
            )
        unexpected = {
            region_id: decision
            for region_id, decision in decisions.items()
            if region_id != bound_region_id and decision != 'outside'
        }
        if unexpected:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} has ambiguous/non-exclusive region membership: {unexpected}',
            )

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    source_inputs: list[DeterministicGaSourceInput] = []
    for source in sorted(snapshot.sources, key=lambda item: item.source_entity_id):
        region_id = source_region_bindings[source.source_entity_id]
        require_exact_membership(
            _position_tuple(source.source_reference_point),
            bound_region_id=region_id,
            label=f'source {source.source_entity_id} acoustic reference point',
        )
        if source.geometric_directivity_state != 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY':
            raise ValueError(
                f'source {source.source_entity_id} lacks exact geometric directivity capability'
            )
        if source.source_axis is None:
            raise ValueError(
                f'source {source.source_entity_id} lacks exact directivity reference axis'
            )
        if (
            source.directivity_dataset_id is None
            or source.directivity_dataset_version is None
            or source.directivity_dataset_sha256 is None
        ):
            raise ValueError(
                'multi-Portal deterministic GA does not substitute omnidirectional '
                f'directivity for source {source.source_entity_id}'
            )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'exact DirectivityDataset is unresolved for source {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('snapshot/directivity dataset exact identity mismatch')
        source_inputs.append(
            DeterministicGaSourceInput(
                source_entity_id=source.source_entity_id,
                r110_compiled_source_sha256=source.r110_compiled_source_sha256,
                source_reference_point=source.source_reference_point,
                source_axis=source.source_axis,
                directivity_dataset_id=dataset.dataset_id,
                directivity_dataset_version=dataset.version,
                directivity_dataset_sha256=dataset.semantic_sha256,
                acoustic_region_id=region_id,
            )
        )

    receiver_inputs: list[DeterministicGaReceiverInput] = []
    for receiver in sorted(snapshot.receivers, key=lambda item: item.receiver_id):
        region_id = receiver_region_bindings[receiver.receiver_id]
        require_exact_membership(
            _position_tuple(receiver.world_position),
            bound_region_id=region_id,
            label=f'receiver {receiver.receiver_id} position',
        )
        receiver_inputs.append(
            DeterministicGaReceiverInput(
                receiver_id=receiver.receiver_id,
                entity_id=receiver.entity_id,
                world_position=receiver.world_position,
                acoustic_region_id=region_id,
            )
        )
    if not source_inputs or not receiver_inputs:
        raise ValueError('deterministic GA execution requires source and receiver authority')

    bounds = compiled_geometry.bounding_volume
    origin = Position3(
        x_m=bounds.min_x_m,
        y_m=bounds.min_y_m,
        z_m=bounds.min_z_m,
    )
    dimensions = (
        float(bounds.max_x_m - bounds.min_x_m),
        float(bounds.max_y_m - bounds.min_y_m),
        float(bounds.max_z_m - bounds.min_z_m),
    )
    if any(value <= 0.0 for value in dimensions):
        raise ValueError('R120 bounding dimensions must be positive')

    object_triangle_indices = _occluder_object_triangle_indices(
        compiled_geometry
    )
    reflection_planes: tuple[GeometricSurfacePlane, ...] = ()
    if configuration.maximum_reflection_order >= 1:
        portal_surface_ids = {
            surface_id
            for aperture in apertures
            for surface_id in aperture.source_surface_ids
        }
        region_boundary_surface_ids = {
            surface_id
            for region in region_authority.declarations
            for surface_id in region.boundary_surface_ids
        }
        if dispatch.solver_implementation_ref in (
            HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF,
            HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF,
            HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF,
        ):
            # The Portal graph specular engines also admit exact finite
            # reflection on Portal surfaces where triangles physically exist.
            reflection_surface_ids = tuple(sorted(region_boundary_surface_ids))
        else:
            reflection_surface_ids = tuple(
                sorted(region_boundary_surface_ids - portal_surface_ids)
            )
        mapping_by_id = {
            mapping.source_surface_id: mapping
            for mapping in compiled_geometry.surface_mapping
        }
        missing = set(reflection_surface_ids) - set(mapping_by_id)
        if missing:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                f'reflection surfaces are unresolved in exact R120 mapping: {sorted(missing)}',
            )
        plane_list: list[GeometricSurfacePlane] = []
        for surface_id in reflection_surface_ids:
            try:
                plane_list.append(
                    _general_surface_plane(
                        compiled_geometry,
                        mapping_by_id[surface_id],
                        tolerance_m=configuration.geometric_tolerance_m,
                    )
                )
            except ValueError as exc:
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_GEOMETRY',
                    f'cross-region reflection surface is not exact finite planar geometry: {exc}',
                ) from exc
        reflection_planes = tuple(plane_list)
        if not reflection_planes:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'cross-region reflection requires at least one exact finite planar surface',
            )
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_sha256': request.request_semantic_sha256,
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_sha256': descriptor.semantic_sha256,
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'region_authority_ref': _authority_ref(region_authority).model_dump(mode='json'),
        'portal_authority_ref': _authority_ref(portal_authority).model_dump(mode='json'),
        'boundary_termination_authority_ref': _authority_ref(
            boundary_termination_authority
        ).model_dump(mode='json'),
        'room_origin_m': origin.model_dump(mode='json'),
        'room_dimensions_m': list(dimensions),
        'boundary_planes': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in reflection_planes
        ],
        'geometry_policy': 'general_planar_multi_region_portal_v1',
        'unsupported_reflection_surface_ids': [],
        'portal_apertures': [item.model_dump(mode='json') for item in apertures],
        'portal_graph': portal_graph.model_dump(mode='json'),
        'region_declarations': [
            item.model_dump(mode='json')
            for item in sorted(
                region_authority.declarations,
                key=lambda item: item.region_id,
            )
        ],
        'maximum_portal_crossings': configuration.maximum_portal_crossings,
        'maximum_reflection_order': configuration.maximum_reflection_order,
        'occluder_triangle_indices': list(object_triangle_indices),
        'sources': [item.model_dump(mode='json', exclude_none=True) for item in source_inputs],
        'receivers': [item.model_dump(mode='json', exclude_none=True) for item in receiver_inputs],
        'sound_speed_m_s': sound_speed,
        'frequency_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'frequency_centers_hz': list(configuration.frequency_centers_hz),
        'geometric_tolerance_m': configuration.geometric_tolerance_m,
        'engine_image_match_tolerance_m': configuration.engine_image_match_tolerance_m,
        'identity_decimal_places': configuration.identity_decimal_places,
        'incidence_exact_angle_tolerance_deg': float(
            configuration.incidence_exact_angle_tolerance_deg
        ),
    }
    digest = _semantic_hash(core)
    return DeterministicGaExecutionInput(
        execution_input_id=f'r150-ga-execution-input:{digest}',
        semantic_sha256=digest,
        **core,
    )

def compile_deterministic_ga_execution_input(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    configuration: DeterministicGaConfiguration,
    source_region_bindings: dict[str, str] | None = None,
    receiver_region_bindings: dict[str, str] | None = None,
) -> DeterministicGaExecutionInput:
    _validate_dispatch_chain(
        snapshot=snapshot,
        request=request,
        dispatch=dispatch,
        descriptor=descriptor,
        configuration=configuration,
    )
    if snapshot.treatment_boundary_bindings:
        raise ValueError(
            'deterministic GA foundation does not flatten attached-treatment '
            'composition into one specular surface quantity; active treatment '
            'boundary bindings require a future exact GA composition evaluator'
        )
    if (
        snapshot.r120_compiled_geometry_id != compiled_geometry.compiled_geometry_id
        or snapshot.r120_compiled_geometry_sha256
        != compiled_geometry.compiled_hash_sha256
        or snapshot.compiled_topology_sha256
        != compiled_geometry.topology_identity_sha256
    ):
        raise ValueError('GA execution R120 geometry identity mismatch')
    for expected, actual, label in (
        (compiled_geometry.region_authority_ref, _authority_ref(region_authority), 'region'),
        (compiled_geometry.portal_authority_ref, _authority_ref(portal_authority), 'portal'),
        (
            compiled_geometry.boundary_termination_authority_ref,
            _authority_ref(boundary_termination_authority),
            'boundary termination',
        ),
    ):
        if expected is None or expected != actual:
            raise ValueError(f'GA exact {label} authority mismatch')
    if configuration.room_policy == 'general_planar_multi_region_portal_v1':
        return _compile_multi_region_portal_execution_input(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=boundary_termination_authority,
            directivity_datasets=directivity_datasets,
            configuration=configuration,
            source_region_bindings=source_region_bindings,
            receiver_region_bindings=receiver_region_bindings,
        )
    _validate_supported_topology(
        region_authority=region_authority,
        portal_authority=portal_authority,
        boundary_termination_authority=boundary_termination_authority,
    )
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError('deterministic GA foundation rejects approximated/dropped R120 geometry')
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError('deterministic GA foundation requires exact R120 preservation')

    if snapshot.environment is None or snapshot.environment.sound_speed_m_s is None:
        raise ValueError('deterministic GA path delay requires exact sound-speed authority')
    sound_speed = float(snapshot.environment.sound_speed_m_s)

    for center in configuration.frequency_centers_hz:
        if not request.requested_frequency_domain.contains(center):
            raise ValueError(
                f'GA frequency center {center} is outside requested frequency domain'
            )

    room_mappings = tuple(
        item
        for item in compiled_geometry.surface_mapping
        if item.semantic_class == 'room_boundary'
    )
    bounds = compiled_geometry.bounding_volume
    region_surfaces = set(region_authority.declarations[0].boundary_surface_ids)
    room_mapping_by_id = {item.source_surface_id: item for item in room_mappings}
    room_surface_ids = set(room_mapping_by_id)
    if region_surfaces != room_surface_ids:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'explicit acoustic region boundary surfaces do not match exact room-boundary shell',
        )

    general_geometry = configuration.room_policy == 'general_planar_closed_polyhedral_v1'
    if configuration.maximum_reflection_order > 2:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'single-region deterministic GA specular reflection is bounded to '
            'second order; third-order and higher chains require the '
            'multi-region Portal lane',
        )
    if configuration.maximum_reflection_order == 2 and not general_geometry:
        raise ValueError(
            'second-order deterministic GA execution requires the general-planar lane'
        )
    unsupported_reflection_surface_ids: tuple[str, ...] | None = None
    region_triangle_indices: tuple[int, ...] = ()
    region_bounds: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ] | None = None

    if general_geometry:
        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(
                compiled_geometry,
                tuple(room_mapping_by_id[surface_id] for surface_id in sorted(region_surfaces)),
            )
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA requires the exact region room-boundary subset '
                'to be a closed manifold; holes/open edges are not filled',
            )
        if shell_volume_m3 <= configuration.geometric_tolerance_m ** 3:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA region shell is degenerate within declared tolerance',
            )

        plane_list: list[GeometricSurfacePlane] = []
        unsupported_surface_ids: list[str] = []
        for mapping in sorted(
            compiled_geometry.surface_mapping,
            key=lambda item: item.source_surface_id,
        ):
            try:
                plane_list.append(
                    _general_surface_plane(
                        compiled_geometry,
                        mapping,
                        tolerance_m=configuration.geometric_tolerance_m,
                    )
                )
            except ValueError as exc:
                if mapping.source_surface_id in region_surfaces:
                    raise DeterministicGaUnsupportedError(
                        'UNSUPPORTED_GEOMETRY',
                        f'region boundary surface is not exact planar geometry: {exc}',
                    ) from exc
                unsupported_surface_ids.append(mapping.source_surface_id)
        planes = tuple(plane_list)
        unsupported_reflection_surface_ids = tuple(sorted(unsupported_surface_ids))
        region_triangle_indices = tuple(
            sorted(
                {
                    index
                    for surface_id in region_surfaces
                    for index in room_mapping_by_id[surface_id].compiled_triangle_indices
                }
            )
        )
    else:
        planes = tuple(
            _surface_plane(
                compiled_geometry,
                item,
                tolerance_m=configuration.geometric_tolerance_m,
            )
            for item in room_mappings
        )
        plane_keys = [(item.axis, item.side) for item in planes]
        required_plane_keys = {
            ('x', 'min'),
            ('x', 'max'),
            ('y', 'min'),
            ('y', 'max'),
            ('z', 'min'),
            ('z', 'max'),
        }
        if len(planes) != 6 or set(plane_keys) != required_plane_keys:
            raise ValueError(
                'candidate pyroomacoustics adapter requires six exact semantic '
                'room-boundary surfaces, one per shoebox plane'
            )
        if len(plane_keys) != len(set(plane_keys)):
            raise ValueError('multiple semantic surfaces map to one shoebox boundary plane')

        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(compiled_geometry, room_mappings)
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise ValueError(
                'candidate pyroomacoustics adapter requires the exact semantic '
                'room-boundary subset itself to be a closed manifold; holes/open '
                'edges are not filled into a shoebox'
            )
        expected_room_volume_m3 = (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        room_surface_area_m2 = 2.0 * (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            + float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_z_m - bounds.min_z_m)
            + float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        volume_tolerance_m3 = max(
            configuration.geometric_tolerance_m ** 3,
            room_surface_area_m2 * configuration.geometric_tolerance_m,
        )
        if abs(shell_volume_m3 - expected_room_volume_m3) > volume_tolerance_m3:
            raise ValueError(
                'semantic room-boundary shell volume does not exactly match its '
                'shoebox bounds within the declared geometric tolerance; candidate '
                'geometry must not fill or clip the R120 shell'
            )
        region_bounds = (
            (float(bounds.min_x_m), float(bounds.max_x_m)),
            (float(bounds.min_y_m), float(bounds.max_y_m)),
            (float(bounds.min_z_m), float(bounds.max_z_m)),
        )

    def require_inside_region(
        point: tuple[float, float, float],
        *,
        label: str,
        legacy_message: str,
    ) -> None:
        if general_geometry:
            membership = _region_point_membership(
                compiled_geometry,
                region_triangle_indices,
                point,
                tolerance=configuration.geometric_tolerance_m,
            )
            if membership != 'inside':
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_REGION_MEMBERSHIP',
                    f'{label} is not unambiguously inside the sole explicit acoustic '
                    f'region (membership={membership})',
                )
        else:
            assert region_bounds is not None
            if not _point_strictly_inside_box(
                point,
                bounds=region_bounds,
                tolerance_m=configuration.geometric_tolerance_m,
            ):
                raise ValueError(legacy_message)

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    source_inputs: list[DeterministicGaSourceInput] = []
    for source in sorted(snapshot.sources, key=lambda item: item.source_entity_id):
        require_inside_region(
            _position_tuple(source.source_reference_point),
            label=f'source {source.source_entity_id} acoustic reference point',
            legacy_message=(
                f'source {source.source_entity_id} acoustic reference point is not '
                'strictly inside the sole explicit acoustic region; unmodeled '
                'external space is not an implicit propagation region'
            ),
        )
        if (
            source.geometric_directivity_state
            != 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
        ):
            raise ValueError(
                f'source {source.source_entity_id} lacks exact geometric directivity capability'
            )
        if source.source_axis is None:
            raise ValueError(
                f'source {source.source_entity_id} lacks exact directivity reference axis'
            )
        if (
            source.directivity_dataset_id is None
            or source.directivity_dataset_version is None
            or source.directivity_dataset_sha256 is None
        ):
            raise ValueError(
                'deterministic GA foundation does not substitute omnidirectional '
                f'directivity for source {source.source_entity_id}'
            )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'exact DirectivityDataset is unresolved for source {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('snapshot/directivity dataset exact identity mismatch')
        source_inputs.append(
            DeterministicGaSourceInput(
                source_entity_id=source.source_entity_id,
                r110_compiled_source_sha256=source.r110_compiled_source_sha256,
                source_reference_point=source.source_reference_point,
                source_axis=source.source_axis,
                directivity_dataset_id=dataset.dataset_id,
                directivity_dataset_version=dataset.version,
                directivity_dataset_sha256=dataset.semantic_sha256,
            )
        )

    receiver_inputs_list: list[DeterministicGaReceiverInput] = []
    for item in sorted(snapshot.receivers, key=lambda item: item.receiver_id):
        require_inside_region(
            _position_tuple(item.world_position),
            label=f'receiver {item.receiver_id} position',
            legacy_message=(
                f'receiver {item.receiver_id} position is not strictly inside the '
                'sole explicit acoustic region; unmodeled external space is not '
                'an implicit propagation region'
            ),
        )
        receiver_inputs_list.append(
            DeterministicGaReceiverInput(
                receiver_id=item.receiver_id,
                entity_id=item.entity_id,
                world_position=item.world_position,
            )
        )
    receiver_inputs = tuple(receiver_inputs_list)
    if not source_inputs or not receiver_inputs:
        raise ValueError('deterministic GA execution requires source and receiver authority')

    object_triangle_indices = _occluder_object_triangle_indices(
        compiled_geometry
    )
    origin = Position3(
        x_m=bounds.min_x_m,
        y_m=bounds.min_y_m,
        z_m=bounds.min_z_m,
    )
    dimensions = (
        float(bounds.max_x_m - bounds.min_x_m),
        float(bounds.max_y_m - bounds.min_y_m),
        float(bounds.max_z_m - bounds.min_z_m),
    )
    if any(value <= 0.0 for value in dimensions):
        raise ValueError('R120 bounding dimensions must be positive')

    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_sha256': request.request_semantic_sha256,
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_sha256': descriptor.semantic_sha256,
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'region_authority_ref': _authority_ref(region_authority).model_dump(mode='json'),
        'portal_authority_ref': _authority_ref(portal_authority).model_dump(mode='json'),
        'boundary_termination_authority_ref': _authority_ref(
            boundary_termination_authority
        ).model_dump(mode='json'),
        'room_origin_m': origin.model_dump(mode='json'),
        'room_dimensions_m': list(dimensions),
        'boundary_planes': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in (
                sorted(planes, key=lambda item: item.source_surface_id)
                if general_geometry
                else sorted(planes, key=lambda item: (item.axis, item.side))
            )
        ],
        'occluder_triangle_indices': list(object_triangle_indices),
        'sources': [item.model_dump(mode='json', exclude_none=True) for item in source_inputs],
        'receivers': [item.model_dump(mode='json', exclude_none=True) for item in receiver_inputs],
        'sound_speed_m_s': sound_speed,
        'frequency_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'frequency_centers_hz': list(configuration.frequency_centers_hz),
        'geometric_tolerance_m': configuration.geometric_tolerance_m,
        'engine_image_match_tolerance_m': configuration.engine_image_match_tolerance_m,
        'identity_decimal_places': configuration.identity_decimal_places,
        'incidence_exact_angle_tolerance_deg': float(
            configuration.incidence_exact_angle_tolerance_deg
        ),
    }
    if general_geometry:
        core['geometry_policy'] = 'general_planar_closed_polyhedral_v1'
        core['unsupported_reflection_surface_ids'] = list(
            unsupported_reflection_surface_ids or ()
        )
        if configuration.maximum_reflection_order == 2:
            core['maximum_reflection_order'] = 2
    digest = _semantic_hash(core)
    return DeterministicGaExecutionInput(
        execution_input_id=f'r150-ga-execution-input:{digest}',
        semantic_sha256=digest,
        **core,
    )

def _triangle_vertices(
    compiled: R120CompiledGeometry,
    triangle_index: int,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    triangle = compiled.triangles[triangle_index]
    return (
        (
            compiled.vertices[triangle.a].x_m,
            compiled.vertices[triangle.a].y_m,
            compiled.vertices[triangle.a].z_m,
        ),
        (
            compiled.vertices[triangle.b].x_m,
            compiled.vertices[triangle.b].y_m,
            compiled.vertices[triangle.b].z_m,
        ),
        (
            compiled.vertices[triangle.c].x_m,
            compiled.vertices[triangle.c].y_m,
            compiled.vertices[triangle.c].z_m,
        ),
    )

def _segment_triangle_intersection_parameter(
    start: Sequence[float],
    end: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
    distance_scaled_tolerance: bool = False,
) -> float | None:
    direction = _vector(start, end)
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)

    if distance_scaled_tolerance:
        segment_length = _norm(direction)
        edge1_length = _norm(edge1)
        edge2_length = _norm(edge2)
        if (
            segment_length <= tolerance
            or edge1_length <= tolerance
            or edge2_length <= tolerance
        ):
            return None
        geometry_scale = max(segment_length, edge1_length, edge2_length)
        relative_tolerance = min(0.25, tolerance / geometry_scale)
        endpoint_parameter_tolerance = min(0.25, tolerance / segment_length)
        determinant_scale = segment_length * edge1_length * edge2_length
        if abs(determinant) <= determinant_scale * relative_tolerance:
            return None
    else:
        relative_tolerance = tolerance
        endpoint_parameter_tolerance = tolerance
        if abs(determinant) <= tolerance:
            return None

    inv_det = 1.0 / determinant
    tvec = _vector(triangle[0], start)
    u = _dot(tvec, pvec) * inv_det
    if u < -relative_tolerance or u > 1.0 + relative_tolerance:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv_det
    if v < -relative_tolerance or u + v > 1.0 + relative_tolerance:
        return None
    t = _dot(edge2, qvec) * inv_det
    if (
        t <= endpoint_parameter_tolerance
        or t >= 1.0 - endpoint_parameter_tolerance
    ):
        return None
    return t

def _segment_triangle_intersection_prepared(
    start: Sequence[float],
    direction: tuple[float, float, float],
    segment_length: float,
    vertex_a: Sequence[float],
    prepared: tuple,
    *,
    tolerance: float,
    distance_scaled_tolerance: bool,
) -> float | None:
    """``_segment_triangle_intersection_parameter`` over precomputed geometry.

    ``direction``/``segment_length`` are the per-segment invariants and
    ``prepared`` carries the per-triangle quantities the index derived once
    (same float operations), so the result is bit-identical to calling
    ``_segment_triangle_intersection_parameter`` on the raw vertices.
    """
    edge1, edge2, edge1_length, edge2_length, _e1sq, _e2sq, _normal, _nlen = (
        prepared
    )
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)

    if distance_scaled_tolerance:
        if (
            segment_length <= tolerance
            or edge1_length <= tolerance
            or edge2_length <= tolerance
        ):
            return None
        geometry_scale = max(segment_length, edge1_length, edge2_length)
        relative_tolerance = min(0.25, tolerance / geometry_scale)
        endpoint_parameter_tolerance = min(0.25, tolerance / segment_length)
        determinant_scale = segment_length * edge1_length * edge2_length
        if abs(determinant) <= determinant_scale * relative_tolerance:
            return None
    else:
        relative_tolerance = tolerance
        endpoint_parameter_tolerance = tolerance
        if abs(determinant) <= tolerance:
            return None

    inv_det = 1.0 / determinant
    tvec = _vector(vertex_a, start)
    u = _dot(tvec, pvec) * inv_det
    if u < -relative_tolerance or u > 1.0 + relative_tolerance:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv_det
    if v < -relative_tolerance or u + v > 1.0 + relative_tolerance:
        return None
    t = _dot(edge2, qvec) * inv_det
    if (
        t <= endpoint_parameter_tolerance
        or t >= 1.0 - endpoint_parameter_tolerance
    ):
        return None
    return t

def _point_on_triangle_surface_prepared(
    point: Sequence[float],
    vertex_a: Sequence[float],
    prepared: tuple,
    *,
    tolerance: float,
) -> bool:
    """``_point_on_triangle_surface`` over precomputed edge/normal geometry."""
    _e1, _e2, _l1, _l2, _s1, _s2, normal, normal_length = prepared
    if normal_length <= tolerance * tolerance:
        return False
    plane_distance = abs(_dot(_vector(vertex_a, point), normal)) / normal_length
    return plane_distance <= tolerance and _point_in_triangle_prepared(
        point,
        vertex_a,
        prepared,
        tolerance=tolerance,
    )

def _point_in_triangle_prepared(
    point: Sequence[float],
    vertex_a: Sequence[float],
    prepared: tuple,
    *,
    tolerance: float,
) -> bool:
    """``_point_in_triangle`` over precomputed edges and self-dot products."""
    edge1, edge2, _l1, _l2, dot11, dot00, _normal, _nlen = prepared
    v2 = _vector(vertex_a, point)
    dot01 = _dot(edge2, edge1)
    dot02 = _dot(edge2, v2)
    dot12 = _dot(edge1, v2)
    denominator = dot00 * dot11 - dot01 * dot01
    if abs(denominator) <= tolerance * tolerance:
        return False
    inverse = 1.0 / denominator
    u = (dot11 * dot02 - dot01 * dot12) * inverse
    v = (dot00 * dot12 - dot01 * dot02) * inverse
    return u >= -tolerance and v >= -tolerance and u + v <= 1.0 + tolerance

def _occluder_object_triangle_indices(
    compiled_geometry: R120CompiledGeometry,
) -> tuple[int, ...]:
    return tuple(
        sorted(
            index
            for mapping in compiled_geometry.surface_mapping
            if mapping.semantic_class != 'room_boundary'
            for index in mapping.compiled_triangle_indices
        )
    )

def _occluder_triangles(
    compiled: R120CompiledGeometry,
) -> tuple[
    tuple[
        str,
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
    ...,
]:
    return _IndexedOccluderRows(
        (triangle.source_surface_id,) + _triangle_vertices(compiled, index)
        for index, triangle in enumerate(compiled.triangles)
    )

def _require_supported_feature_scale(compiled: R120CompiledGeometry) -> None:
    """Fail closed when any compiled triangle is below the supported scale.

    Deterministic visibility/membership verdicts are only qualified for
    occluder features of at least ``MIN_SUPPORTED_FEATURE_M``; a single
    smaller triangle silently changes blocking and boundary decisions, so
    the execution input is rejected as unsupported geometry.
    """
    for index in range(len(compiled.triangles)):
        vertex_a, vertex_b, vertex_c = _triangle_vertices(compiled, index)
        max_edge = max(
            _norm(_vector(vertex_a, vertex_b)),
            _norm(_vector(vertex_b, vertex_c)),
            _norm(_vector(vertex_c, vertex_a)),
        )
        if max_edge < MIN_SUPPORTED_FEATURE_M:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                f'compiled triangle {index} has maximum edge '
                f'{max_edge:.6e} m below the supported minimum feature '
                f'scale {MIN_SUPPORTED_FEATURE_M:.6e} m; deterministic '
                'acoustic verdicts are not supported for sub-millimetre '
                'geometry',
            )

def _segment_blocked(
    compiled: R120CompiledGeometry,
    start: Sequence[float],
    end: Sequence[float],
    *,
    tolerance: float,
    ignored_surface_ids: frozenset[str] = frozenset(),
    distance_scaled_tolerance: bool = False,
    occluder_triangles: Sequence[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ] | None = None,
) -> bool:
    if occluder_triangles is None:
        occluder_triangles = tuple(_occluder_triangles(compiled))
    if isinstance(occluder_triangles, _IndexedOccluderRows):
        direction = _vector(start, end)
        segment_length = (
            _norm(direction) if distance_scaled_tolerance else 0.0
        )
        for row, prepared in occluder_triangles._segment_prepared_candidates(
            start,
            end,
        ):
            if row[0] in ignored_surface_ids:
                continue
            hit = _segment_triangle_intersection_prepared(
                start,
                direction,
                segment_length,
                row[1],
                prepared,
                tolerance=tolerance,
                distance_scaled_tolerance=distance_scaled_tolerance,
            )
            if hit is not None:
                return True
        return False
    for surface_id, vertex_a, vertex_b, vertex_c in occluder_triangles:
        if surface_id in ignored_surface_ids:
            continue
        hit = _segment_triangle_intersection_parameter(
            start,
            end,
            (vertex_a, vertex_b, vertex_c),
            tolerance=tolerance,
            distance_scaled_tolerance=distance_scaled_tolerance,
        )
        if hit is not None:
            return True
    return False

def _point_on_triangle_surface(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    normal = _cross(edge1, edge2)
    normal_length = _norm(normal)
    if normal_length <= tolerance * tolerance:
        return False
    plane_distance = abs(_dot(_vector(triangle[0], point), normal)) / normal_length
    return plane_distance <= tolerance and _point_in_triangle(
        point,
        triangle,
        tolerance=tolerance,
    )

def _region_point_membership(
    compiled: R120CompiledGeometry,
    triangle_indices: Sequence[int],
    point: Sequence[float],
    *,
    tolerance: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    if not triangle_indices:
        return 'ambiguous'
    triangles = tuple(
        _triangle_vertices(compiled, index)
        for index in triangle_indices
    )
    if any(
        _point_on_triangle_surface(point, triangle, tolerance=tolerance)
        for triangle in triangles
    ):
        return 'boundary'

    bounds = compiled.bounding_volume
    diagonal = sqrt(
        (bounds.max_x_m - bounds.min_x_m) ** 2
        + (bounds.max_y_m - bounds.min_y_m) ** 2
        + (bounds.max_z_m - bounds.min_z_m) ** 2
    )
    ray_length = max(1.0, diagonal * 4.0)
    directions = (
        _unit((1.0, 0.3713906763541037, 0.217031)),
        _unit((-0.419, 1.0, 0.163)),
        _unit((0.271, -0.337, 1.0)),
    )
    decisions: list[bool] = []
    t_tolerance = max(1.0e-12, tolerance / ray_length * 4.0)
    for direction in directions:
        end = tuple(
            float(point[index]) + ray_length * direction[index]
            for index in range(3)
        )
        hits = sorted(
            hit
            for triangle in triangles
            if (
                hit := _segment_triangle_intersection_parameter(
                    point,
                    end,
                    triangle,
                    tolerance=tolerance,
                    distance_scaled_tolerance=True,
                )
            )
            is not None
        )
        distinct_hits: list[float] = []
        for hit in hits:
            if not distinct_hits or abs(hit - distinct_hits[-1]) > t_tolerance:
                distinct_hits.append(hit)
        decisions.append(len(distinct_hits) % 2 == 1)
    if len(set(decisions)) != 1:
        return 'ambiguous'
    return 'inside' if decisions[0] else 'outside'

def _point_in_triangle(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    v0 = _vector(triangle[0], triangle[2])
    v1 = _vector(triangle[0], triangle[1])
    v2 = _vector(triangle[0], point)
    dot00 = _dot(v0, v0)
    dot01 = _dot(v0, v1)
    dot02 = _dot(v0, v2)
    dot11 = _dot(v1, v1)
    dot12 = _dot(v1, v2)
    denominator = dot00 * dot11 - dot01 * dot01
    if abs(denominator) <= tolerance * tolerance:
        return False
    inverse = 1.0 / denominator
    u = (dot11 * dot02 - dot01 * dot12) * inverse
    v = (dot00 * dot12 - dot01 * dot02) * inverse
    return u >= -tolerance and v >= -tolerance and u + v <= 1.0 + tolerance

def _point_on_surface(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    mapping = next(
        item
        for item in compiled.surface_mapping
        if item.source_surface_id == surface_id
    )
    return any(
        _point_on_triangle_surface(
            point,
            _triangle_vertices(compiled, index),
            tolerance=tolerance,
        )
        for index in mapping.compiled_triangle_indices
    )

def _point_has_other_surface_contact(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
    occluder_triangles: Sequence[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ] | None = None,
) -> bool:
    if isinstance(occluder_triangles, _IndexedOccluderRows):
        # The occluder rows carry exactly the same (surface, triangle) pairs
        # as ``surface_mapping`` (both enumerate ``compiled.triangles``), so
        # the grid's point query yields a strict superset of the candidates
        # the linear scan below would test — the exact per-triangle test
        # decides each verdict identically.
        for row, prepared in occluder_triangles._point_prepared_candidates(
            point,
            tolerance,
        ):
            if row[0] == surface_id:
                continue
            if _point_on_triangle_surface_prepared(
                point,
                row[1],
                prepared,
                tolerance=tolerance,
            ):
                return True
        return False
    return any(
        mapping.source_surface_id != surface_id
        and any(
            _point_on_triangle_surface(
                point,
                _triangle_vertices(compiled, index),
                tolerance=tolerance,
            )
            for index in mapping.compiled_triangle_indices
        )
        for mapping in compiled.surface_mapping
    )

def _local(
    world: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(world[0]) - float(origin[0]),
        float(world[1]) - float(origin[1]),
        float(world[2]) - float(origin[2]),
    )

def _world(
    local: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(local[0]) + float(origin[0]),
        float(local[1]) + float(origin[1]),
        float(local[2]) + float(origin[2]),
    )

def _nearest_engine_image(
    images: Sequence[NativeImageSource],
    target_local_m: Sequence[float],
    *,
    tolerance_m: float,
    label: str,
) -> tuple[float, float, float]:
    if not images:
        raise RuntimeError('candidate image-source engine returned no images')
    ranked = sorted(
        (
            (_distance(image.position_local_m, target_local_m), image.position_local_m)
            for image in images
        ),
        key=lambda item: (item[0], item[1]),
    )
    distance, position = ranked[0]
    if distance > tolerance_m:
        raise RuntimeError(
            f'candidate engine did not emit expected {label} image source; '
            f'nearest distance={distance}'
        )
    return position

def _mirror_source(
    source_world: Sequence[float],
    plane: GeometricSurfacePlane,
) -> tuple[float, float, float]:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        signed_distance = _dot(_vector(point, source_world), normal)
        return tuple(
            float(source_world[index]) - 2.0 * signed_distance * normal[index]
            for index in range(3)
        )  # type: ignore[return-value]
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    values = [float(item) for item in source_world]
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    values[axis_index] = 2.0 * float(plane.coordinate_m) - values[axis_index]
    return (values[0], values[1], values[2])

def _plane_point_normal(
    plane: GeometricSurfacePlane,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if plane.point_m is not None and plane.normal is not None:
        return (
            _position_tuple(plane.point_m),
            _unit((plane.normal.x, plane.normal.y, plane.normal.z)),
        )
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    point = [0.0, 0.0, 0.0]
    normal = [0.0, 0.0, 0.0]
    point[axis_index] = float(plane.coordinate_m)
    normal[axis_index] = 1.0
    return (
        (point[0], point[1], point[2]),
        (normal[0], normal[1], normal[2]),
    )

def _planes_coincident(
    left: GeometricSurfacePlane,
    right: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    left_point, left_normal = _plane_point_normal(left)
    right_point, right_normal = _plane_point_normal(right)
    alignment = abs(_dot(left_normal, right_normal))
    if 1.0 - alignment > min(0.25, tolerance):
        return False
    return abs(_dot(_vector(left_point, right_point), left_normal)) <= tolerance

def _segment_grazes_plane(
    start: Sequence[float],
    end: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    segment = _vector(start, end)
    length = _norm(segment)
    if length <= tolerance:
        return True
    _, normal = _plane_point_normal(plane)
    angular_tolerance = min(0.25, tolerance / length)
    return abs(_dot(_unit(segment), normal)) <= angular_tolerance

def _reflection_point(
    image_world: Sequence[float],
    receiver_world: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[float, float, float] | None:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        denominator = _dot(_vector(image_world, receiver_world), normal)
        if abs(denominator) <= tolerance:
            return None
        t = _dot(_vector(image_world, point), normal) / denominator
    else:
        if plane.axis is None or plane.coordinate_m is None:
            raise ValueError('surface plane representation is incomplete')
        axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
        denominator = float(receiver_world[axis_index]) - float(image_world[axis_index])
        if abs(denominator) <= tolerance:
            return None
        t = (float(plane.coordinate_m) - float(image_world[axis_index])) / denominator
    if t < -tolerance or t > 1.0 + tolerance:
        return None
    return tuple(
        float(image_world[index])
        + t * (float(receiver_world[index]) - float(image_world[index]))
        for index in range(3)
    )  # type: ignore[return-value]

def _second_order_reflection_points(
    source_world: Sequence[float],
    receiver_world: Sequence[float],
    first_plane: GeometricSurfacePlane,
    second_plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
] | None:
    first_image = _mirror_source(source_world, first_plane)
    second_image = _mirror_source(first_image, second_plane)
    second_point = _reflection_point(
        second_image,
        receiver_world,
        second_plane,
        tolerance=tolerance,
    )
    if second_point is None:
        return None
    first_point = _reflection_point(
        first_image,
        second_point,
        first_plane,
        tolerance=tolerance,
    )
    if first_point is None:
        return None
    return first_point, second_point

def _higher_order_reflection_points(
    source_world: Sequence[float],
    receiver_world: Sequence[float],
    planes: Sequence[GeometricSurfacePlane],
    *,
    tolerance: float,
) -> tuple[tuple[float, float, float], ...] | None:
    """Reconstruct ordered reflection points for a bounded specular chain.

    The source is mirrored through each ordered plane in turn; the final image
    then walks the receiver ray back through the chain, recovering every
    physical contact point in interaction order.
    """

    images: list[tuple[float, float, float]] = [
        (
            float(source_world[0]),
            float(source_world[1]),
            float(source_world[2]),
        )
    ]
    for plane in planes:
        images.append(_mirror_source(images[-1], plane))
    points: list[tuple[float, float, float]] = []
    target = (
        float(receiver_world[0]),
        float(receiver_world[1]),
        float(receiver_world[2]),
    )
    for index in range(len(planes) - 1, -1, -1):
        point = _reflection_point(
            images[index + 1],
            target,
            planes[index],
            tolerance=tolerance,
        )
        if point is None:
            return None
        points.append(point)
        target = point
    points.reverse()
    return tuple(points)

@lru_cache(maxsize=512)
def _equatorial_frame(
    axis_x: float,
    axis_y: float,
    axis_z: float,
    tolerance: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    """Source equatorial frame (forward, left, up) for a directivity axis.

    The frame depends only on the axis and tolerance — one source reuses it
    across every path and band evaluated in an execution. Pure function of
    its float arguments, so the content-keyed memo is exact.
    """
    forward = _unit((axis_x, axis_y, axis_z))
    forward_horizontal = sqrt(forward[0] ** 2 + forward[1] ** 2)
    if forward_horizontal <= tolerance:
        raise ValueError(
            'R150 foundation directivity evaluator requires a source axis with '
            'a defined horizontal heading; a vertical-only axis has no heading'
        )
    horizontal_forward = _unit((forward[0], forward[1], 0.0))
    left = _unit((-horizontal_forward[1], horizontal_forward[0], 0.0))
    # 'up' completes the source's equatorial frame (forward x left) so a
    # pitched axis evaluates directivity angles in its own tilted plane
    # instead of being silently dropped. For a horizontal axis this reduces
    # to world +Z and to the previous horizontal-axis convention exactly.
    up = _unit(_cross(forward, left))
    return forward, left, up

def _directivity_angles(
    source_axis: Direction3,
    departure_direction: Sequence[float],
    *,
    tolerance: float,
) -> tuple[float, float]:
    forward, left, up = _equatorial_frame(
        float(source_axis.x),
        float(source_axis.y),
        float(source_axis.z),
        tolerance,
    )
    direction = _unit(departure_direction)
    forward_component = _dot(direction, forward)
    left_component = _dot(direction, left)
    up_component = _dot(direction, up)
    equatorial_projection = sqrt(forward_component ** 2 + left_component ** 2)
    elevation = degrees(atan2(up_component, equatorial_projection))
    if equatorial_projection <= tolerance:
        horizontal = 0.0
    else:
        horizontal = degrees(atan2(left_component, forward_component))
    return horizontal, elevation

def _directivity_contribution(
    dataset: DirectivityDataset,
    *,
    frequency_hz: float,
    source_axis: Direction3,
    departure_direction: Sequence[float],
    tolerance: float,
) -> SourceDirectivityContribution | None:
    try:
        horizontal, vertical = _directivity_angles(
            source_axis,
            departure_direction,
            tolerance=tolerance,
        )
    except ValueError:
        return None
    evaluation = evaluate_directivity(
        dataset,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        request='magnitude',
    )
    if (
        evaluation.decision != 'SUPPORTED'
        or evaluation.magnitude_db is None
        or evaluation.magnitude_linear is None
    ):
        return None
    try:
        energy_factor = evaluation.magnitude_linear ** 2
    except OverflowError:
        return None
    if not isfinite(energy_factor):
        return None
    return SourceDirectivityContribution(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        evaluation_semantic_sha256=evaluation.semantic_sha256,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        magnitude_db=evaluation.magnitude_db,
        magnitude_linear=evaluation.magnitude_linear,
        energy_factor=energy_factor,
    )

def _reflection_incidence(
    plane: GeometricSurfacePlane,
    incoming_direction: Sequence[float],
    *,
    decimals: int,
) -> tuple[float, float]:
    """Evaluated reflection incidence (angle_deg from the surface normal, cosine).

    Incidence is measured between the incoming propagation segment direction
    and the surface normal: 0 deg / cosine 1.0 is normal incidence.
    """
    _, normal = _plane_point_normal(plane)
    cosine = abs(_dot(_unit(incoming_direction), normal))
    cosine = min(1.0, max(0.0, cosine))
    angle = degrees(acos(cosine))
    return (_round_float(angle, decimals), _round_float(cosine, decimals))

def _incidence_evaluation(
    band_condition: GeometricIncidenceCondition,
    band_angle_deg: float | None,
    evaluated_angle_deg: float,
    *,
    angle_tolerance_deg: float,
) -> BoundaryIncidenceEvaluation:
    """Classify how the declared band incidence evidence applies at one angle.

    Only an angle-specific band evaluated at its declared angle is exact;
    normal-incidence evidence evaluated at normal incidence records a declared
    condition match. Every other combination applies the declared scalar band
    coefficient across angles under the explicit versioned
    ``scalar_coefficient_all_angles_v1`` approximation policy — non-exact
    evidence is never silently treated as angle-specific truth.
    """
    if band_condition == 'angle_specific':
        assert band_angle_deg is not None
        if abs(evaluated_angle_deg - band_angle_deg) <= angle_tolerance_deg:
            return 'angle_specific_exact'
    elif band_condition == 'normal_incidence':
        if evaluated_angle_deg <= angle_tolerance_deg:
            return 'declared_condition_match'
    return 'scalar_coefficient_all_angles_v1'

def _material_contribution(
    authority: GeometricMaterialAuthority,
    plane: GeometricSurfacePlane,
    *,
    frequency_hz: float,
    tolerance: float,
    incidence: tuple[float, float] | None = None,
    incidence_angle_tolerance_deg: float = 1.0,
) -> BoundaryMaterialContribution | None:
    if authority.authority_ref != plane.material_authority:
        raise ValueError('resolved material exact authority does not match surface binding')
    material = authority.material
    if material.geometric_model != 'banded':
        return None
    matches = [
        band
        for band in material.geometric_bands
        if abs(float(band.center_hz) - float(frequency_hz)) <= tolerance
    ]
    if len(matches) != 1:
        return None
    band = matches[0]
    specular = (1.0 - float(band.absorption)) * (
        1.0 - float(band.scattering)
    )
    incidence_fields: dict[str, Any] = {}
    if incidence is not None:
        incidence_angle_deg, incidence_cosine = incidence
        incidence_fields = {
            'incidence_angle_deg': incidence_angle_deg,
            'incidence_cosine': incidence_cosine,
            'coefficient_incidence_condition': band.incidence_condition,
            'incidence_evaluation': _incidence_evaluation(
                band.incidence_condition,
                band.incidence_angle_deg,
                incidence_angle_deg,
                angle_tolerance_deg=incidence_angle_tolerance_deg,
            ),
        }
    return BoundaryMaterialContribution(
        source_surface_id=plane.source_surface_id,
        material_authority=authority.authority_ref,
        boundary_physics_authority=plane.boundary_physics_authority,
        frequency_hz=frequency_hz,
        absorption=band.absorption,
        scattering=band.scattering,
        specular_energy_factor=specular,
        **incidence_fields,
    )

def _make_path(
    *,
    path_type: PathType,
    source: DeterministicGaSourceInput,
    receiver: DeterministicGaReceiverInput,
    points: tuple[tuple[float, float, float], ...],
    surface_ids: tuple[str, ...],
    length_m: float,
    sound_speed_m_s: float,
    departure: Sequence[float],
    arrival: Sequence[float],
    bands: Sequence[DeterministicPathBandQuantity],
    decimals: int,
    solver_implementation_ref: ExactExternalAuthorityRef,
    typed_interactions: Sequence[DeterministicPathInteraction] | None = None,
    ordered_region_ids: Sequence[str] | None = None,
    region_segment_evidence: Sequence[PortalRegionSegmentEvidence] | None = None,
    execution_input_semantic_sha256: str | None = None,
) -> DeterministicAcousticPath:
    point_models = tuple(_rounded_position(point, decimals) for point in points)
    departure_model = _rounded_direction(departure, decimals)
    arrival_model = _rounded_direction(arrival, decimals)
    core: dict[str, Any] = {
        'source_entity_id': source.source_entity_id,
        'receiver_id': receiver.receiver_id,
        'receiver_entity_id': receiver.entity_id,
        'path_type': path_type,
        'ordered_interaction_surface_ids': list(surface_ids),
        'ordered_interaction_points': [
            {'x_m': item.x_m, 'y_m': item.y_m, 'z_m': item.z_m}
            for item in point_models
        ],
        'geometric_path_length_m': _round_float(length_m, decimals),
        'propagation_delay_s': _round_float(
            length_m / sound_speed_m_s,
            decimals,
        ),
        'departure_direction': {
            'x': departure_model.x,
            'y': departure_model.y,
            'z': departure_model.z,
        },
        'arrival_direction': {
            'x': arrival_model.x,
            'y': arrival_model.y,
            'z': arrival_model.z,
        },
        'direction_semantics': (
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        'bands': [
            _prune_band_payload(item.model_dump(mode='json'))
            for item in bands
        ],
        'adapter_id': DETERMINISTIC_GA_ADAPTER_ID,
        'adapter_version': DETERMINISTIC_GA_ADAPTER_VERSION,
        'solver_implementation_ref': _frozen_model_json_payload(
            solver_implementation_ref
        ),
    }
    if typed_interactions is not None:
        core['ordered_interactions'] = [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in typed_interactions
        ]
    if ordered_region_ids is not None:
        core['ordered_region_ids'] = list(ordered_region_ids)
    if region_segment_evidence is not None:
        core['region_segment_evidence'] = [
            item.model_dump(mode='json')
            for item in region_segment_evidence
        ]
    if execution_input_semantic_sha256 is not None:
        core['execution_input_semantic_sha256'] = execution_input_semantic_sha256
    digest = _semantic_hash(core)
    # The constructor receives the already-validated model instances
    # (revalidate_instances='never' skips re-running their validators);
    # ``core`` stays the JSON-dict payload the semantic digest is computed
    # over, unchanged.
    init_payload: dict[str, Any] = dict(core)
    init_payload['ordered_interaction_points'] = point_models
    init_payload['departure_direction'] = departure_model
    init_payload['arrival_direction'] = arrival_model
    init_payload['solver_implementation_ref'] = solver_implementation_ref
    init_payload['bands'] = tuple(bands)
    if typed_interactions is not None:
        init_payload['ordered_interactions'] = tuple(typed_interactions)
    if region_segment_evidence is not None:
        init_payload['region_segment_evidence'] = tuple(region_segment_evidence)
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **init_payload,
    )

MaterialAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometricMaterialAuthority | None,
]

def _append_single_portal_first_order_reflections(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    source: DeterministicGaSourceInput,
    receiver: DeterministicGaReceiverInput,
    dataset: DirectivityDataset,
    material_resolver: MaterialAuthorityResolver,
    paths: list[DeterministicAcousticPath],
    rejected: list[RejectedPathCandidate],
    occluder_triangles: Sequence[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ] | None = None,
    region_shell_cache: dict[str, tuple] | None = None,
) -> None:
    """Emit exact one-reflection/one-Portal paths for the bounded two-region lane."""

    if execution_input.maximum_reflection_order != 1:
        return
    graph = execution_input.portal_graph
    if (
        graph is None
        or len(graph.region_ids) != 2
        or len(graph.directed_edges) != 1
        or graph.maximum_portal_crossings != 1
        or execution_input.portal_apertures is None
        or len(execution_input.portal_apertures) != 1
        or execution_input.region_declarations is None
        or len(execution_input.region_declarations) != 2
        or source.acoustic_region_id is None
        or receiver.acoustic_region_id is None
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'reflected Portal execution requires exact 2 regions / 1 Portal / 1 crossing',
        )
    edge = graph.directed_edges[0]
    aperture = execution_input.portal_apertures[0]
    if (
        edge.portal_id != aperture.portal_id
        or edge.aperture_id != aperture.aperture_id
        or edge.aperture_sha256 != aperture.semantic_sha256
        or (edge.from_region_id, edge.to_region_id)
        != (source.acoustic_region_id, receiver.acoustic_region_id)
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'source/receiver reflected Portal route does not match exact directed graph authority',
        )

    source_world = _position_tuple(source.source_reference_point)
    receiver_world = _position_tuple(receiver.world_position)
    region_by_id = {item.region_id: item for item in execution_input.region_declarations}
    portal_surface_ids = set(aperture.source_surface_ids)
    if occluder_triangles is None:
        occluder_triangles = _occluder_triangles(compiled_geometry)
    if region_shell_cache is None:
        region_shell_cache = {}

    for plane in sorted(execution_input.boundary_planes, key=lambda item: item.source_surface_id):
        surface_id = plane.source_surface_id
        if surface_id in portal_surface_ids:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                f'Portal surface {surface_id} cannot be used as a reflection surface',
            )
        owning_regions = tuple(
            item.region_id
            for item in execution_input.region_declarations
            if surface_id in item.boundary_surface_ids
        )
        if len(owning_regions) != 1:
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='UNSUPPORTED_PORTAL_TOPOLOGY',
                    reason=(
                        'ordinary reflection surface does not bind uniquely to exactly one '
                        'AcousticRegion in the bounded reflected Portal topology'
                    ),
                )
            )
            continue
        reflection_region_id = owning_regions[0]
        if reflection_region_id == source.acoustic_region_id:
            image = _mirror_source(source_world, plane)
            reflection = _reflection_point(
                image, receiver_world, plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
            event_order = ('reflection', 'portal_crossing')
        elif reflection_region_id == receiver.acoustic_region_id:
            image = _mirror_source(receiver_world, plane)
            reflection = _reflection_point(
                image, source_world, plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
            event_order = ('portal_crossing', 'reflection')
        else:
            continue

        if reflection is None or not _point_on_surface(
            compiled_geometry, surface_id, reflection,
            tolerance=execution_input.geometric_tolerance_m,
        ):
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='UNSUPPORTED_GEOMETRY',
                    reason=(
                        'cross-region reflection point lies outside the exact finite '
                        'semantic R120 surface triangle extent'
                    ),
                )
            )
            continue
        if _point_has_other_surface_contact(
            compiled_geometry, surface_id, reflection,
            tolerance=execution_input.geometric_tolerance_m,
            occluder_triangles=occluder_triangles,
        ):
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='UNSUPPORTED_GEOMETRY',
                    reason=(
                        'cross-region reflection contact is shared-edge or multi-surface '
                        'ambiguous within declared tolerance'
                    ),
                )
            )
            continue

        segment_points = None
        segment_region_ids = None
        if event_order == ('reflection', 'portal_crossing'):
            crossing = resolve_direct_portal_crossing(
                aperture,
                start=reflection,
                end=receiver_world,
                from_region_id=source.acoustic_region_id,
                to_region_id=receiver.acoustic_region_id,
                tolerance_m=execution_input.geometric_tolerance_m,
            )
            if crossing is not None:
                segment_points = (source_world, reflection, crossing, receiver_world)
                segment_region_ids = (
                    source.acoustic_region_id,
                    source.acoustic_region_id,
                    receiver.acoustic_region_id,
                )
        else:
            crossing = resolve_direct_portal_crossing(
                aperture,
                start=source_world,
                end=reflection,
                from_region_id=source.acoustic_region_id,
                to_region_id=receiver.acoustic_region_id,
                tolerance_m=execution_input.geometric_tolerance_m,
            )
            if crossing is not None:
                segment_points = (source_world, crossing, reflection, receiver_world)
                segment_region_ids = (
                    source.acoustic_region_id,
                    receiver.acoustic_region_id,
                    receiver.acoustic_region_id,
                )
        if crossing is None or segment_points is None or segment_region_ids is None:
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='INVALID_PORTAL_CROSSING',
                    reason=(
                        'reflected propagation segment misses the exact directed Portal '
                        'aperture or crosses it in the wrong physical event order'
                    ),
                )
            )
            continue

        segment_lengths = tuple(
            _distance(segment_points[index], segment_points[index + 1])
            for index in range(3)
        )
        if any(value <= execution_input.geometric_tolerance_m for value in segment_lengths):
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='INVALID_REGION_SEQUENCE',
                    reason='reflection/Portal event ordering collapses a propagation segment',
                )
            )
            continue

        segment_evidence: list[PortalRegionSegmentEvidence] = []
        failed = False
        for index, region_id in enumerate(segment_region_ids):
            region = region_by_id.get(region_id)
            if region is None:
                raise ValueError(f'reflected Portal path references unresolved AcousticRegion {region_id}')
            shell_triangles = region_shell_cache.get(region_id)
            if shell_triangles is None:
                shell_triangles = _region_shell_triangles(
                    compiled_geometry,
                    region,
                    execution_input.portal_apertures,
                )
                region_shell_cache[region_id] = shell_triangles
            membership = region_segment_membership_with_portal_caps(
                compiled_geometry=compiled_geometry,
                region=region,
                apertures=execution_input.portal_apertures,
                start=segment_points[index],
                end=segment_points[index + 1],
                tolerance_m=execution_input.geometric_tolerance_m,
                shell_triangles=shell_triangles,
            )
            if membership != 'valid':
                rejected.append(
                    RejectedPathCandidate(
                        source_entity_id=source.source_entity_id,
                        receiver_id=receiver.receiver_id,
                        path_type='specular_reflection',
                        interaction_surface_ids=(surface_id,),
                        decision='INTERMEDIATE_REGION_MEMBERSHIP_FAILURE',
                        reason=(
                            f'reflected segment {index} leaves exact AcousticRegion '
                            f'{region_id} (membership={membership})'
                        ),
                    )
                )
                failed = True
                break
            if _segment_blocked(
                compiled_geometry,
                segment_points[index],
                segment_points[index + 1],
                tolerance=execution_input.geometric_tolerance_m,
                distance_scaled_tolerance=True,
                occluder_triangles=occluder_triangles,
            ):
                rejected.append(
                    RejectedPathCandidate(
                        source_entity_id=source.source_entity_id,
                        receiver_id=receiver.receiver_id,
                        path_type='specular_reflection',
                        interaction_surface_ids=(surface_id,),
                        decision='BLOCKED_VISIBILITY',
                        reason=(
                            f'exact R120 triangle occludes reflected Portal segment {index}; '
                            'expected endpoint contact is handled by endpoint tolerance'
                        ),
                    )
                )
                failed = True
                break
            segment_evidence.append(
                PortalRegionSegmentEvidence(
                    segment_index=index,
                    region_id=region_id,
                    start_point=_rounded_position(
                        segment_points[index], execution_input.identity_decimal_places
                    ),
                    end_point=_rounded_position(
                        segment_points[index + 1], execution_input.identity_decimal_places
                    ),
                )
            )
        if failed:
            continue

        if plane.material_authority is None:
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                    reason='cross-region reflection surface has no exact material authority',
                )
            )
            continue
        resolved_material = material_resolver(plane.material_authority)
        if resolved_material is None or resolved_material.authority_ref != plane.material_authority:
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                    reason='cross-region reflection material authority is stale or unresolved',
                )
            )
            continue

        departure = _vector(segment_points[0], segment_points[1])
        arrival = _vector(segment_points[-2], segment_points[-1])
        path_length = sum(segment_lengths)
        reflection_segment_index = (
            1 if event_order == ('reflection', 'portal_crossing') else 2
        )
        incidence = _reflection_incidence(
            plane,
            _vector(
                segment_points[reflection_segment_index - 1],
                segment_points[reflection_segment_index],
            ),
            decimals=execution_input.identity_decimal_places,
        )
        incidence_angle_tolerance = (
            execution_input.incidence_exact_angle_tolerance_deg or 1.0
        )
        reflection_bands: list[DeterministicPathBandQuantity] = []
        failure: PathCandidateDecision | None = None
        failure_reason = ''
        for frequency_hz in execution_input.frequency_centers_hz:
            directivity = _directivity_contribution(
                dataset,
                frequency_hz=frequency_hz,
                source_axis=source.source_axis,
                departure_direction=departure,
                tolerance=execution_input.geometric_tolerance_m,
            )
            if directivity is None:
                failure = 'UNSUPPORTED_DIRECTIVITY'
                failure_reason = (
                    'exact source directivity cannot evaluate cross-region reflected-path '
                    'departure angle/frequency'
                )
                break
            boundary = _material_contribution(
                resolved_material,
                plane,
                frequency_hz=frequency_hz,
                tolerance=execution_input.geometric_tolerance_m,
                incidence=incidence,
                incidence_angle_tolerance_deg=incidence_angle_tolerance,
            )
            if boundary is None:
                failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                failure_reason = (
                    'cross-region reflection surface lacks exact matching banded '
                    'absorption/scattering authority; no coefficient is fabricated'
                )
                break
            spreading = 1.0 / (path_length * path_length)
            energy = (
                spreading
                * directivity.energy_factor
                * boundary.specular_energy_factor
            )
            if not isfinite(energy):
                failure = 'UNSUPPORTED_DIRECTIVITY'
                failure_reason = (
                    'exact source directivity energy is not finitely representable'
                )
                break
            reflection_bands.append(
                DeterministicPathBandQuantity(
                    center_hz=frequency_hz,
                    spreading_factor_per_m2=spreading,
                    source_directivity=directivity,
                    boundary_material=boundary,
                    relative_energy_transport_per_m2=energy,
                )
            )
        if failure is not None:
            rejected.append(
                RejectedPathCandidate(
                    source_entity_id=source.source_entity_id,
                    receiver_id=receiver.receiver_id,
                    path_type='specular_reflection',
                    interaction_surface_ids=(surface_id,),
                    decision=failure,
                    reason=failure_reason,
                )
            )
            continue

        reflection_interaction = DeterministicPathInteraction(
            kind='reflection',
            point=_rounded_position(reflection, execution_input.identity_decimal_places),
            surface_id=surface_id,
            incidence_angle_deg=incidence[0],
            incidence_cosine=incidence[1],
        )
        portal_interaction = DeterministicPathInteraction(
            kind='portal_crossing',
            point=_rounded_position(crossing, execution_input.identity_decimal_places),
            portal_id=aperture.portal_id,
            from_region_id=source.acoustic_region_id,
            to_region_id=receiver.acoustic_region_id,
        )
        typed_interactions = (
            (reflection_interaction, portal_interaction)
            if event_order == ('reflection', 'portal_crossing')
            else (portal_interaction, reflection_interaction)
        )
        paths.append(
            _make_path(
                path_type='specular_reflection',
                source=source,
                receiver=receiver,
                points=(reflection,),
                surface_ids=(surface_id,),
                length_m=path_length,
                sound_speed_m_s=execution_input.sound_speed_m_s,
                departure=departure,
                arrival=arrival,
                bands=reflection_bands,
                decimals=execution_input.identity_decimal_places,
                solver_implementation_ref=execution_input.solver_implementation_ref,
                typed_interactions=typed_interactions,
                ordered_region_ids=(source.acoustic_region_id, receiver.acoustic_region_id),
                region_segment_evidence=tuple(segment_evidence),
                execution_input_semantic_sha256=execution_input.semantic_sha256,
            )
        )

def _resolve_reflected_leg_portal_crossings(
    *,
    tolerance_m: float,
    aperture_by_portal_id: dict[str, GeometricPortalAperture],
    leg_start: Sequence[float],
    leg_end: Sequence[float],
    portal_ids: tuple[str, ...],
    portal_region_pairs: tuple[tuple[str, str], ...],
) -> tuple[tuple[float, float, float], ...] | None:
    """Resolve the declared directed Portal crossings of one straight leg of a
    reflected propagation path.

    Each leg between two non-Portal events is one straight segment; the
    declared ordered Portal sequence must pierce its exact directed apertures
    in order within it. Returns the crossing points in event order, or None
    when an aperture is missed or the declared order is violated.
    """

    if not portal_ids:
        return ()
    leg_direction = _vector(leg_start, leg_end)
    norm_sq = _dot(leg_direction, leg_direction)
    if norm_sq <= tolerance_m * tolerance_m:
        return None
    leg_length = _norm(leg_direction)
    parameter_tolerance = min(0.25, tolerance_m / max(leg_length, tolerance_m))
    crossings: list[tuple[float, float, float]] = []
    previous_parameter = -1.0
    for portal_id, (from_region_id, to_region_id) in zip(
        portal_ids, portal_region_pairs, strict=True
    ):
        aperture = aperture_by_portal_id.get(portal_id)
        if aperture is None:
            raise ValueError(
                f'Portal graph references unresolved aperture identity {portal_id}'
            )
        crossing = resolve_direct_portal_crossing(
            aperture,
            start=leg_start,
            end=leg_end,
            from_region_id=from_region_id,
            to_region_id=to_region_id,
            tolerance_m=tolerance_m,
        )
        if crossing is None:
            return None
        parameter = _dot(_vector(leg_start, crossing), leg_direction) / norm_sq
        if parameter <= previous_parameter + parameter_tolerance:
            return None
        previous_parameter = parameter
        crossings.append(crossing)
    return tuple(crossings)

def _append_portal_graph_reflections(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    source: DeterministicGaSourceInput,
    receiver: DeterministicGaReceiverInput,
    dataset: DirectivityDataset,
    material_resolver: MaterialAuthorityResolver,
    topology_paths: Sequence[tuple[tuple[str, ...], tuple[str, ...]]],
    paths: list[DeterministicAcousticPath],
    rejected: list[RejectedPathCandidate],
    occluder_triangles: Sequence[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ] | None = None,
    region_shell_cache: dict[str, tuple] | None = None,
) -> None:
    """Emit bounded interleaved specular reflections along directed Portal graph paths.

    The bounded one-Portal engine keeps its exact legacy construction; the
    Portal graph specular engine emits, for every simple directed region path,
    first-order reflections in any region slot and ordered second-order pairs,
    where each straight leg pierces its declared directed Portal sequence in
    order and every propagation segment proves exact region membership and
    occlusion clearance. The bounded specular chain authority additionally
    emits ordered third/fourth-order reflection chains under the same
    constraints.
    """

    if execution_input.maximum_reflection_order not in (1, 2, 3, 4, 5, 6):
        return
    if (
        execution_input.solver_implementation_ref
        == HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF
    ):
        _append_single_portal_first_order_reflections(
            execution_input=execution_input,
            compiled_geometry=compiled_geometry,
            source=source,
            receiver=receiver,
            dataset=dataset,
            material_resolver=material_resolver,
            paths=paths,
            rejected=rejected,
            occluder_triangles=occluder_triangles,
            region_shell_cache=region_shell_cache,
        )
        return
    specular_chain = execution_input.solver_implementation_ref in (
        HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF,
        HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF,
    )
    if (
        execution_input.solver_implementation_ref
        == HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF
    ):
        if execution_input.maximum_reflection_order > 2:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'Portal graph specular engine authority is bounded to '
                'second-order reflection',
            )
    elif (
        execution_input.solver_implementation_ref
        == HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF
    ):
        if not 3 <= execution_input.maximum_reflection_order <= 4:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'bounded specular chain engine authority requires a declared '
                'reflection order of three or four',
            )
    elif (
        execution_input.solver_implementation_ref
        == HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF
    ):
        if execution_input.maximum_reflection_order < 5:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'bounded high-order specular chain engine authority requires '
                'a declared reflection order of five or six',
            )
    else:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'reflected Portal propagation requires an explicit reflected '
            'Portal graph engine authority',
        )
    graph = execution_input.portal_graph
    if (
        graph is None
        or execution_input.portal_apertures is None
        or execution_input.region_declarations is None
        or source.acoustic_region_id is None
        or receiver.acoustic_region_id is None
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'reflected Portal graph execution requires exact Portal graph '
            'topology authority',
        )

    source_world = _position_tuple(source.source_reference_point)
    receiver_world = _position_tuple(receiver.world_position)
    aperture_by_portal_id = {
        item.portal_id: item for item in execution_input.portal_apertures
    }
    if len(aperture_by_portal_id) != len(execution_input.portal_apertures):
        raise ValueError('reflected Portal execution input has duplicate Portal identity')
    region_by_id = {
        item.region_id: item for item in execution_input.region_declarations
    }
    if len(region_by_id) != len(execution_input.region_declarations):
        raise ValueError('reflected Portal execution input has duplicate region identity')
    region_surface_ids = {
        item.region_id: set(item.boundary_surface_ids)
        for item in execution_input.region_declarations
    }
    ordered_planes = tuple(
        sorted(execution_input.boundary_planes, key=lambda item: item.source_surface_id)
    )
    incidence_angle_tolerance = (
        execution_input.incidence_exact_angle_tolerance_deg or 1.0
    )
    if occluder_triangles is None:
        occluder_triangles = _occluder_triangles(compiled_geometry)
    if region_shell_cache is None:
        region_shell_cache = {}
    first_order_plane_evals: dict[str, tuple] = {}
    second_order_pair_evals: dict[tuple[str, str], tuple] = {}
    specular_chain_evals: dict[tuple[str, ...], tuple] = {}
    emitted_rejections: set[tuple[tuple[str, ...], str, str]] = set()
    chain_sequence_evaluations = 0

    def reject(
        surface_ids: tuple[str, ...],
        decision: PathCandidateDecision,
        reason: str,
    ) -> None:
        key = (surface_ids, decision, reason)
        if key in emitted_rejections:
            return
        emitted_rejections.add(key)
        rejected.append(
            RejectedPathCandidate(
                source_entity_id=source.source_entity_id,
                receiver_id=receiver.receiver_id,
                path_type='specular_reflection',
                interaction_surface_ids=surface_ids,
                decision=decision,
                reason=reason,
            )
        )

    def assemble_chain(
        ordered_region_ids: tuple[str, ...],
        ordered_portal_ids: tuple[str, ...],
        reflection_slots: tuple[int, ...],
        reflection_planes: tuple[GeometricSurfacePlane, ...],
        reflection_points: tuple[tuple[float, float, float], ...],
        surface_ids: tuple[str, ...],
    ) -> (
        tuple[
            tuple[tuple[float, float, float], ...],
            tuple[DeterministicPathInteraction, ...],
            tuple[PortalRegionSegmentEvidence, ...],
            float,
        ]
        | None
    ):
        """Build one candidate event chain: ordered leg crossings plus per-segment
        region-membership/occlusion evidence. Emits exactly one rejection and
        returns None when the candidate fails its bounds."""

        anchors = (source_world,) + reflection_points + (receiver_world,)
        if any(
            _distance(anchors[index], anchors[index + 1])
            <= execution_input.geometric_tolerance_m
            for index in range(len(anchors) - 1)
        ):
            reject(
                surface_ids,
                'INVALID_REGION_SEQUENCE',
                'reflection/Portal event ordering collapses a propagation segment',
            )
            return None
        leg_starts = (0,) + reflection_slots
        leg_ends = reflection_slots + (len(ordered_portal_ids),)
        event_points: list[tuple[float, float, float]] = [source_world]
        interactions: list[DeterministicPathInteraction] = []
        for leg_index, (portal_start, portal_end) in enumerate(
            zip(leg_starts, leg_ends, strict=True)
        ):
            leg_crossings = _resolve_reflected_leg_portal_crossings(
                tolerance_m=execution_input.geometric_tolerance_m,
                aperture_by_portal_id=aperture_by_portal_id,
                leg_start=anchors[leg_index],
                leg_end=anchors[leg_index + 1],
                portal_ids=ordered_portal_ids[portal_start:portal_end],
                portal_region_pairs=tuple(
                    (ordered_region_ids[m], ordered_region_ids[m + 1])
                    for m in range(portal_start, portal_end)
                ),
            )
            if leg_crossings is None:
                reject(
                    surface_ids,
                    'INVALID_PORTAL_CROSSING',
                    'reflected propagation segment misses the exact directed '
                    'Portal aperture or crosses it in the wrong physical event order',
                )
                return None
            for portal_index, crossing in zip(
                range(portal_start, portal_end), leg_crossings, strict=True
            ):
                event_points.append(crossing)
                interactions.append(
                    DeterministicPathInteraction(
                        kind='portal_crossing',
                        point=_rounded_position(
                            crossing, execution_input.identity_decimal_places
                        ),
                        portal_id=ordered_portal_ids[portal_index],
                        from_region_id=ordered_region_ids[portal_index],
                        to_region_id=ordered_region_ids[portal_index + 1],
                    )
                )
            event_points.append(anchors[leg_index + 1])
            if leg_index < len(reflection_planes):
                incidence = _reflection_incidence(
                    reflection_planes[leg_index],
                    _vector(anchors[leg_index], anchors[leg_index + 1]),
                    decimals=execution_input.identity_decimal_places,
                )
                interactions.append(
                    DeterministicPathInteraction(
                        kind='reflection',
                        point=_rounded_position(
                            anchors[leg_index + 1],
                            execution_input.identity_decimal_places,
                        ),
                        surface_id=reflection_planes[leg_index].source_surface_id,
                        incidence_angle_deg=incidence[0],
                        incidence_cosine=incidence[1],
                    )
                )
        points = tuple(event_points)
        segment_lengths = tuple(
            _distance(points[index], points[index + 1])
            for index in range(len(points) - 1)
        )
        if any(
            value <= execution_input.geometric_tolerance_m
            for value in segment_lengths
        ):
            reject(
                surface_ids,
                'INVALID_REGION_SEQUENCE',
                'reflection/Portal event ordering collapses a propagation segment',
            )
            return None
        segment_region_ids = _portal_segment_region_ids(
            ordered_region_ids, reflection_slots
        )
        segment_evidence: list[PortalRegionSegmentEvidence] = []
        for segment_index, region_id in enumerate(segment_region_ids):
            region = region_by_id.get(region_id)
            if region is None:
                raise ValueError(
                    f'reflected Portal path references unresolved AcousticRegion {region_id}'
                )
            shell_triangles = region_shell_cache.get(region_id)
            if shell_triangles is None:
                shell_triangles = _region_shell_triangles(
                    compiled_geometry,
                    region,
                    execution_input.portal_apertures,
                )
                region_shell_cache[region_id] = shell_triangles
            membership = region_segment_membership_with_portal_caps(
                compiled_geometry=compiled_geometry,
                region=region,
                apertures=execution_input.portal_apertures,
                start=points[segment_index],
                end=points[segment_index + 1],
                tolerance_m=execution_input.geometric_tolerance_m,
                shell_triangles=shell_triangles,
            )
            if membership != 'valid':
                reject(
                    surface_ids,
                    'INTERMEDIATE_REGION_MEMBERSHIP_FAILURE',
                    f'reflected segment {segment_index} leaves exact AcousticRegion '
                    f'{region_id} (membership={membership})',
                )
                return None
            if _segment_blocked(
                compiled_geometry,
                points[segment_index],
                points[segment_index + 1],
                tolerance=execution_input.geometric_tolerance_m,
                distance_scaled_tolerance=True,
                occluder_triangles=occluder_triangles,
            ):
                reject(
                    surface_ids,
                    'BLOCKED_VISIBILITY',
                    'exact R120 opaque triangle surface blocks reflected '
                    f'Portal segment {segment_index}',
                )
                return None
            segment_evidence.append(
                PortalRegionSegmentEvidence(
                    segment_index=segment_index,
                    region_id=region_id,
                    start_point=_rounded_position(
                        points[segment_index],
                        execution_input.identity_decimal_places,
                    ),
                    end_point=_rounded_position(
                        points[segment_index + 1],
                        execution_input.identity_decimal_places,
                    ),
                )
            )
        return (
            points,
            tuple(interactions),
            tuple(segment_evidence),
            sum(segment_lengths),
        )

    def evaluate_first_order_plane(
        plane: GeometricSurfacePlane,
    ) -> tuple:
        """Path-independent single-plane reflection evaluation.

        Returns (decision, reason) on failure or
        (None, reflection, resolved_material, incidence) on success."""
        image = _mirror_source(source_world, plane)
        reflection = _reflection_point(
            image,
            receiver_world,
            plane,
            tolerance=execution_input.geometric_tolerance_m,
        )
        if reflection is None or not _point_on_surface(
            compiled_geometry,
            plane.source_surface_id,
            reflection,
            tolerance=execution_input.geometric_tolerance_m,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'cross-region reflection point lies outside the exact finite '
                'semantic R120 surface triangle extent',
            )
        if _point_has_other_surface_contact(
            compiled_geometry,
            plane.source_surface_id,
            reflection,
            tolerance=execution_input.geometric_tolerance_m,
            occluder_triangles=occluder_triangles,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'cross-region reflection contact is shared-edge or '
                'multi-surface ambiguous within declared tolerance',
            )
        if plane.material_authority is None:
            return (
                'UNSUPPORTED_BOUNDARY_QUANTITY',
                'cross-region reflection surface has no exact material authority',
            )
        resolved_material = material_resolver(plane.material_authority)
        if (
            resolved_material is None
            or resolved_material.authority_ref != plane.material_authority
        ):
            return (
                'UNSUPPORTED_BOUNDARY_QUANTITY',
                'cross-region reflection material authority is stale or unresolved',
            )
        incidence = _reflection_incidence(
            plane,
            _vector(source_world, reflection),
            decimals=execution_input.identity_decimal_places,
        )
        return (None, reflection, resolved_material, incidence)

    def evaluate_second_order_pair(
        first_plane: GeometricSurfacePlane,
        second_plane: GeometricSurfacePlane,
    ) -> tuple:
        """Path-independent ordered plane-pair evaluation.

        Returns (decision, reason) on failure or
        (None, first_point, second_point, resolved_materials, incidences)
        on success."""
        if first_plane.source_surface_id == second_plane.source_surface_id:
            return (
                'UNSUPPORTED_GEOMETRY',
                'same-surface immediate repeat is a degenerate second-order '
                'interaction and is not synthesized',
            )
        if _planes_coincident(
            first_plane,
            second_plane,
            tolerance=execution_input.geometric_tolerance_m,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'ordered second-order planes are coincident within the '
                'declared geometric tolerance',
            )
        reconstructed = _second_order_reflection_points(
            source_world,
            receiver_world,
            first_plane,
            second_plane,
            tolerance=execution_input.geometric_tolerance_m,
        )
        if reconstructed is None:
            return (
                'UNSUPPORTED_GEOMETRY',
                'ordered second-order image reconstruction has no '
                'unambiguous finite plane intersection',
            )
        first_point, second_point = reconstructed
        leg_lengths = (
            _distance(source_world, first_point),
            _distance(first_point, second_point),
            _distance(second_point, receiver_world),
        )
        if any(
            value <= execution_input.geometric_tolerance_m
            for value in leg_lengths
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'second-order reflection points collapse or create a '
                'zero-length propagation segment',
            )
        if not _point_on_surface(
            compiled_geometry,
            first_plane.source_surface_id,
            first_point,
            tolerance=execution_input.geometric_tolerance_m,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'first second-order reflection point lies outside the exact '
                'finite semantic R120 surface triangle extent',
            )
        if not _point_on_surface(
            compiled_geometry,
            second_plane.source_surface_id,
            second_point,
            tolerance=execution_input.geometric_tolerance_m,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'second second-order reflection point lies outside the exact '
                'finite semantic R120 surface triangle extent',
            )
        if _point_has_other_surface_contact(
            compiled_geometry,
            first_plane.source_surface_id,
            first_point,
            tolerance=execution_input.geometric_tolerance_m,
            occluder_triangles=occluder_triangles,
        ) or _point_has_other_surface_contact(
            compiled_geometry,
            second_plane.source_surface_id,
            second_point,
            tolerance=execution_input.geometric_tolerance_m,
            occluder_triangles=occluder_triangles,
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'second-order reflection contact is shared-edge or '
                'multi-surface ambiguous within declared tolerance',
            )
        if (
            _segment_grazes_plane(
                source_world,
                first_point,
                first_plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
            or _segment_grazes_plane(
                first_point,
                second_point,
                first_plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
            or _segment_grazes_plane(
                first_point,
                second_point,
                second_plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
            or _segment_grazes_plane(
                second_point,
                receiver_world,
                second_plane,
                tolerance=execution_input.geometric_tolerance_m,
            )
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'grazing or plane-parallel second-order contact is '
                'ambiguous within declared tolerance',
            )
        resolved_materials: list[GeometricMaterialAuthority] = []
        material_failure = False
        for interaction_plane in (first_plane, second_plane):
            if interaction_plane.material_authority is None:
                material_failure = True
                break
            resolved = material_resolver(interaction_plane.material_authority)
            if (
                resolved is None
                or resolved.authority_ref
                != interaction_plane.material_authority
            ):
                material_failure = True
                break
            resolved_materials.append(resolved)
        if material_failure or len(resolved_materials) != 2:
            return (
                'UNSUPPORTED_BOUNDARY_QUANTITY',
                'one or more ordered second-order surfaces lack an exact '
                'resolvable geometric material authority',
            )
        incidences = (
            _reflection_incidence(
                first_plane,
                _vector(source_world, first_point),
                decimals=execution_input.identity_decimal_places,
            ),
            _reflection_incidence(
                second_plane,
                _vector(first_point, second_point),
                decimals=execution_input.identity_decimal_places,
            ),
        )
        return (
            None,
            first_point,
            second_point,
            resolved_materials,
            incidences,
        )

    def evaluate_chain(
        planes: tuple[GeometricSurfacePlane, ...],
    ) -> tuple:
        """Path-independent ordered plane-chain evaluation.

        Returns (decision, reason) on failure or
        (None, points, resolved_materials, incidences) on success."""
        order = len(planes)
        for index in range(order - 1):
            if (
                planes[index].source_surface_id
                == planes[index + 1].source_surface_id
            ):
                return (
                    'UNSUPPORTED_GEOMETRY',
                    'same-surface immediate repeat is a degenerate specular '
                    'chain interaction and is not synthesized',
                )
            if _planes_coincident(
                planes[index],
                planes[index + 1],
                tolerance=execution_input.geometric_tolerance_m,
            ):
                return (
                    'UNSUPPORTED_GEOMETRY',
                    'adjacent ordered specular chain planes are coincident '
                    'within the declared geometric tolerance',
                )
        chain_points = _higher_order_reflection_points(
            source_world,
            receiver_world,
            planes,
            tolerance=execution_input.geometric_tolerance_m,
        )
        if chain_points is None:
            return (
                'UNSUPPORTED_GEOMETRY',
                'ordered specular chain image reconstruction has no '
                'unambiguous finite plane intersection',
            )
        anchors = (source_world,) + chain_points + (receiver_world,)
        if any(
            _distance(anchors[index], anchors[index + 1])
            <= execution_input.geometric_tolerance_m
            for index in range(len(anchors) - 1)
        ):
            return (
                'UNSUPPORTED_GEOMETRY',
                'ordered specular chain reflection points collapse or create '
                'a zero-length propagation segment',
            )
        for plane, point in zip(planes, chain_points, strict=True):
            if not _point_on_surface(
                compiled_geometry,
                plane.source_surface_id,
                point,
                tolerance=execution_input.geometric_tolerance_m,
            ):
                return (
                    'UNSUPPORTED_GEOMETRY',
                    'ordered specular chain reflection point lies outside the '
                    'exact finite semantic R120 surface triangle extent',
                )
        for plane, point in zip(planes, chain_points, strict=True):
            if _point_has_other_surface_contact(
                compiled_geometry,
                plane.source_surface_id,
                point,
                tolerance=execution_input.geometric_tolerance_m,
                occluder_triangles=occluder_triangles,
            ):
                return (
                    'UNSUPPORTED_GEOMETRY',
                    'ordered specular chain reflection contact is shared-edge '
                    'or multi-surface ambiguous within declared tolerance',
                )
        for leg_index in range(order + 1):
            for plane_index in range(
                max(0, leg_index - 1), min(order, leg_index + 1)
            ):
                if _segment_grazes_plane(
                    anchors[leg_index],
                    anchors[leg_index + 1],
                    planes[plane_index],
                    tolerance=execution_input.geometric_tolerance_m,
                ):
                    return (
                        'UNSUPPORTED_GEOMETRY',
                        'grazing or plane-parallel specular chain contact is '
                        'ambiguous within declared tolerance',
                    )
        resolved_materials: list[GeometricMaterialAuthority] = []
        material_failure = False
        for interaction_plane in planes:
            if interaction_plane.material_authority is None:
                material_failure = True
                break
            resolved = material_resolver(interaction_plane.material_authority)
            if (
                resolved is None
                or resolved.authority_ref
                != interaction_plane.material_authority
            ):
                material_failure = True
                break
            resolved_materials.append(resolved)
        if material_failure or len(resolved_materials) != order:
            return (
                'UNSUPPORTED_BOUNDARY_QUANTITY',
                'one or more ordered specular chain surfaces lack an exact '
                'resolvable geometric material authority',
            )
        incidences = tuple(
            _reflection_incidence(
                planes[index],
                _vector(anchors[index], anchors[index + 1]),
                decimals=execution_input.identity_decimal_places,
            )
            for index in range(order)
        )
        return (
            None,
            chain_points,
            resolved_materials,
            incidences,
        )

    for ordered_region_ids, ordered_portal_ids in topology_paths:
        if not ordered_portal_ids:
            continue
        portal_count = len(ordered_portal_ids)
        for plane in ordered_planes:
            surface_ids = (plane.source_surface_id,)
            slots = tuple(
                index
                for index, region_id in enumerate(ordered_region_ids)
                if plane.source_surface_id in region_surface_ids[region_id]
            )
            if not slots:
                continue
            plane_eval = first_order_plane_evals.get(plane.source_surface_id)
            if plane_eval is None:
                plane_eval = evaluate_first_order_plane(plane)
                first_order_plane_evals[plane.source_surface_id] = plane_eval
            if plane_eval[0] is not None:
                reject(surface_ids, plane_eval[0], plane_eval[1])
                continue
            _, reflection, resolved_material, incidence = plane_eval
            for slot in slots:
                assembled = assemble_chain(
                    ordered_region_ids,
                    ordered_portal_ids,
                    (slot,),
                    (plane,),
                    (reflection,),
                    surface_ids,
                )
                if assembled is None:
                    continue
                (
                    points,
                    interactions,
                    segment_evidence,
                    path_length,
                ) = assembled
                departure = _vector(points[0], points[1])
                arrival = _vector(points[-2], points[-1])
                reflection_bands: list[DeterministicPathBandQuantity] = []
                failure: PathCandidateDecision | None = None
                failure_reason = ''
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity cannot evaluate cross-region '
                            'reflected-path departure angle/frequency'
                        )
                        break
                    boundary = _material_contribution(
                        resolved_material,
                        plane,
                        frequency_hz=frequency_hz,
                        tolerance=execution_input.geometric_tolerance_m,
                        incidence=incidence,
                        incidence_angle_tolerance_deg=incidence_angle_tolerance,
                    )
                    if boundary is None:
                        failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                        failure_reason = (
                            'cross-region reflection surface lacks exact matching banded '
                            'absorption/scattering authority; no coefficient is fabricated'
                        )
                        break
                    spreading = 1.0 / (path_length * path_length)
                    energy = (
                        spreading
                        * directivity.energy_factor
                        * boundary.specular_energy_factor
                    )
                    if not isfinite(energy):
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity energy is not finitely '
                            'representable'
                        )
                        break
                    reflection_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            boundary_material=boundary,
                            relative_energy_transport_per_m2=energy,
                        )
                    )
                if failure is not None:
                    reject(surface_ids, failure, failure_reason)
                    continue
                paths.append(
                    _make_path(
                        path_type='specular_reflection',
                        source=source,
                        receiver=receiver,
                        points=(reflection,),
                        surface_ids=surface_ids,
                        length_m=path_length,
                        sound_speed_m_s=execution_input.sound_speed_m_s,
                        departure=departure,
                        arrival=arrival,
                        bands=reflection_bands,
                        decimals=execution_input.identity_decimal_places,
                        solver_implementation_ref=(
                            execution_input.solver_implementation_ref
                        ),
                        typed_interactions=interactions,
                        ordered_region_ids=ordered_region_ids,
                        region_segment_evidence=segment_evidence,
                        execution_input_semantic_sha256=execution_input.semantic_sha256,
                    )
                )

        if execution_input.maximum_reflection_order < 2:
            continue
        for first_plane in ordered_planes:
            slots_first = tuple(
                index
                for index, region_id in enumerate(ordered_region_ids)
                if first_plane.source_surface_id in region_surface_ids[region_id]
            )
            if not slots_first:
                continue
            for second_plane in ordered_planes:
                surface_ids = (
                    first_plane.source_surface_id,
                    second_plane.source_surface_id,
                )
                slots_second = tuple(
                    index
                    for index, region_id in enumerate(ordered_region_ids)
                    if second_plane.source_surface_id in region_surface_ids[region_id]
                )
                if not slots_second:
                    continue
                pair_key = (
                    first_plane.source_surface_id,
                    second_plane.source_surface_id,
                )
                pair_eval = second_order_pair_evals.get(pair_key)
                if pair_eval is None:
                    pair_eval = evaluate_second_order_pair(
                        first_plane,
                        second_plane,
                    )
                    second_order_pair_evals[pair_key] = pair_eval
                if pair_eval[0] is not None:
                    reject(surface_ids, pair_eval[0], pair_eval[1])
                    continue
                (
                    _,
                    first_point,
                    second_point,
                    resolved_materials,
                    incidences,
                ) = pair_eval
                for first_slot in slots_first:
                    for second_slot in slots_second:
                        if second_slot < first_slot:
                            continue
                        assembled = assemble_chain(
                            ordered_region_ids,
                            ordered_portal_ids,
                            (first_slot, second_slot),
                            (first_plane, second_plane),
                            (first_point, second_point),
                            surface_ids,
                        )
                        if assembled is None:
                            continue
                        (
                            points,
                            interactions,
                            segment_evidence,
                            path_length,
                        ) = assembled
                        departure = _vector(points[0], points[1])
                        arrival = _vector(points[-2], points[-1])
                        second_order_bands: list[DeterministicPathBandQuantity] = []
                        failure = None
                        failure_reason = ''
                        for frequency_hz in execution_input.frequency_centers_hz:
                            directivity = _directivity_contribution(
                                dataset,
                                frequency_hz=frequency_hz,
                                source_axis=source.source_axis,
                                departure_direction=departure,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            if directivity is None:
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity cannot evaluate '
                                    'second-order departure angle/frequency'
                                )
                                break
                            boundary_contributions: list[
                                BoundaryMaterialContribution
                            ] = []
                            for interaction_plane, resolved, incidence in zip(
                                (first_plane, second_plane),
                                resolved_materials,
                                incidences,
                                strict=True,
                            ):
                                boundary = _material_contribution(
                                    resolved,
                                    interaction_plane,
                                    frequency_hz=frequency_hz,
                                    tolerance=execution_input.geometric_tolerance_m,
                                    incidence=incidence,
                                    incidence_angle_tolerance_deg=(
                                        incidence_angle_tolerance
                                    ),
                                )
                                if boundary is None:
                                    failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                                    failure_reason = (
                                        'one or more second-order surfaces lack an exact '
                                        'matching banded absorption/scattering quantity; '
                                        'no reflection coefficient/phase is fabricated'
                                    )
                                    break
                                boundary_contributions.append(boundary)
                            if failure is not None:
                                break
                            spreading = 1.0 / (path_length * path_length)
                            specular_product = 1.0
                            for boundary in boundary_contributions:
                                specular_product *= boundary.specular_energy_factor
                            energy = (
                                spreading
                                * directivity.energy_factor
                                * specular_product
                            )
                            if not isfinite(energy):
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity energy is not '
                                    'finitely representable'
                                )
                                break
                            second_order_bands.append(
                                DeterministicPathBandQuantity(
                                    center_hz=frequency_hz,
                                    spreading_factor_per_m2=spreading,
                                    source_directivity=directivity,
                                    boundary_materials=tuple(boundary_contributions),
                                    relative_energy_transport_per_m2=energy,
                                )
                            )
                        if failure is not None:
                            reject(surface_ids, failure, failure_reason)
                            continue
                        paths.append(
                            _make_path(
                                path_type='specular_reflection',
                                source=source,
                                receiver=receiver,
                                points=(first_point, second_point),
                                surface_ids=surface_ids,
                                length_m=path_length,
                                sound_speed_m_s=execution_input.sound_speed_m_s,
                                departure=departure,
                                arrival=arrival,
                                bands=second_order_bands,
                                decimals=execution_input.identity_decimal_places,
                                solver_implementation_ref=(
                                    execution_input.solver_implementation_ref
                                ),
                                typed_interactions=interactions,
                                ordered_region_ids=ordered_region_ids,
                                region_segment_evidence=segment_evidence,
                                execution_input_semantic_sha256=(
                                    execution_input.semantic_sha256
                                ),
                            )
                        )

        if not specular_chain:
            continue
        for chain_order in range(
            3, int(execution_input.maximum_reflection_order) + 1
        ):
            for sequence in product(ordered_planes, repeat=chain_order):
                if (
                    execution_input.solver_implementation_ref
                    == HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF
                ):
                    chain_sequence_evaluations += 1
                    if (
                        chain_sequence_evaluations
                        > HTDT_PORTAL_HIGH_ORDER_CHAIN_MAXIMUM_EVALUATIONS
                    ):
                        raise DeterministicGaUnsupportedError(
                            'UNSUPPORTED_PORTAL_TOPOLOGY',
                            'bounded high-order specular chain engine '
                            'exceeded the declared sequence-evaluation bound '
                            f'{HTDT_PORTAL_HIGH_ORDER_CHAIN_MAXIMUM_EVALUATIONS}',
                        )
                surface_ids = tuple(
                    plane.source_surface_id for plane in sequence
                )
                slots_per_position = tuple(
                    tuple(
                        index
                        for index, region_id in enumerate(ordered_region_ids)
                        if plane.source_surface_id
                        in region_surface_ids[region_id]
                    )
                    for plane in sequence
                )
                if any(not item for item in slots_per_position):
                    continue
                chain_eval = specular_chain_evals.get(surface_ids)
                if chain_eval is None:
                    chain_eval = evaluate_chain(sequence)
                    specular_chain_evals[surface_ids] = chain_eval
                if chain_eval[0] is not None:
                    reject(surface_ids, chain_eval[0], chain_eval[1])
                    continue
                (
                    _,
                    chain_points,
                    resolved_materials,
                    incidences,
                ) = chain_eval
                for slots in product(*slots_per_position):
                    if any(
                        slots[index + 1] < slots[index]
                        for index in range(chain_order - 1)
                    ):
                        continue
                    assembled = assemble_chain(
                        ordered_region_ids,
                        ordered_portal_ids,
                        slots,
                        sequence,
                        chain_points,
                        surface_ids,
                    )
                    if assembled is None:
                        continue
                    (
                        points,
                        interactions,
                        segment_evidence,
                        path_length,
                    ) = assembled
                    departure = _vector(points[0], points[1])
                    arrival = _vector(points[-2], points[-1])
                    chain_bands: list[DeterministicPathBandQuantity] = []
                    failure = None
                    failure_reason = ''
                    for frequency_hz in execution_input.frequency_centers_hz:
                        directivity = _directivity_contribution(
                            dataset,
                            frequency_hz=frequency_hz,
                            source_axis=source.source_axis,
                            departure_direction=departure,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if directivity is None:
                            failure = 'UNSUPPORTED_DIRECTIVITY'
                            failure_reason = (
                                'exact source directivity cannot evaluate '
                                'ordered specular chain departure '
                                'angle/frequency'
                            )
                            break
                        boundary_contributions: list[
                            BoundaryMaterialContribution
                        ] = []
                        for interaction_plane, resolved, incidence in zip(
                            sequence,
                            resolved_materials,
                            incidences,
                            strict=True,
                        ):
                            boundary = _material_contribution(
                                resolved,
                                interaction_plane,
                                frequency_hz=frequency_hz,
                                tolerance=execution_input.geometric_tolerance_m,
                                incidence=incidence,
                                incidence_angle_tolerance_deg=(
                                    incidence_angle_tolerance
                                ),
                            )
                            if boundary is None:
                                failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                                failure_reason = (
                                    'one or more ordered specular chain '
                                    'surfaces lack an exact matching banded '
                                    'absorption/scattering quantity; no '
                                    'reflection coefficient/phase is fabricated'
                                )
                                break
                            boundary_contributions.append(boundary)
                        if failure is not None:
                            break
                        spreading = 1.0 / (path_length * path_length)
                        specular_product = 1.0
                        for boundary in boundary_contributions:
                            specular_product *= boundary.specular_energy_factor
                        energy = (
                            spreading
                            * directivity.energy_factor
                            * specular_product
                        )
                        if not isfinite(energy):
                            failure = 'UNSUPPORTED_DIRECTIVITY'
                            failure_reason = (
                                'exact source directivity energy is not '
                                'finitely representable'
                            )
                            break
                        chain_bands.append(
                            DeterministicPathBandQuantity(
                                center_hz=frequency_hz,
                                spreading_factor_per_m2=spreading,
                                source_directivity=directivity,
                                boundary_materials=tuple(
                                    boundary_contributions
                                ),
                                relative_energy_transport_per_m2=energy,
                            )
                        )
                    if failure is not None:
                        reject(surface_ids, failure, failure_reason)
                        continue
                    paths.append(
                        _make_path(
                            path_type='specular_reflection',
                            source=source,
                            receiver=receiver,
                            points=chain_points,
                            surface_ids=surface_ids,
                            length_m=path_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=departure,
                            arrival=arrival,
                            bands=chain_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                            typed_interactions=interactions,
                            ordered_region_ids=ordered_region_ids,
                            region_segment_evidence=segment_evidence,
                            execution_input_semantic_sha256=(
                                execution_input.semantic_sha256
                            ),
                        )
                    )

def execute_deterministic_ga(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    directivity_datasets: Sequence[DirectivityDataset],
    material_resolver: MaterialAuthorityResolver,
    engine: DeterministicImageSourceEngine,
) -> DeterministicPathArtifact:
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
    )
    if engine.solver_implementation_ref != execution_input.solver_implementation_ref:
        raise ValueError(
            'candidate engine exact solver implementation authority does not '
            'match the READY dispatch execution input'
        )
    if engine.engine_id in _REGISTERED_ENGINE_IDS:
        registered_ref = _ENGINE_SOLVER_REF_BY_ID_VERSION.get(
            (engine.engine_id, engine.engine_version)
        )
        if (
            registered_ref is None
            or engine.solver_implementation_ref != registered_ref
        ):
            raise ValueError(
                'candidate engine id/version pair does not reproduce the '
                'registered exact solver implementation authority'
            )
    if (
        compiled_geometry.compiled_geometry_id
        != execution_input.r120_compiled_geometry_id
        or compiled_geometry.compiled_hash_sha256
        != execution_input.r120_compiled_geometry_sha256
        or compiled_geometry.topology_identity_sha256
        != execution_input.topology_identity_sha256
    ):
        raise ValueError('GA execution compiled geometry exact identity mismatch')
    _require_supported_feature_scale(compiled_geometry)
    if execution_input.occluder_triangle_indices != (
        _occluder_object_triangle_indices(compiled_geometry)
    ):
        raise ValueError(
            'execution input occluder triangle set does not reproduce the '
            'compiled geometry authority'
        )

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    origin = _position_tuple(execution_input.room_origin_m)
    portal_geometry = (
        execution_input.geometry_policy == 'general_planar_multi_region_portal_v1'
    )
    general_geometry = execution_input.geometry_policy in (
        'general_planar_closed_polyhedral_v1',
        'general_planar_multi_region_portal_v1',
    )
    paths: list[DeterministicAcousticPath] = []
    rejected: list[RejectedPathCandidate] = []
    occluder_triangles = _occluder_triangles(compiled_geometry)
    region_shell_cache: dict[str, tuple] = {}
    topology_paths_cache: dict[
        tuple[str | None, str | None],
        tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | ValueError,
    ] = {}
    portal_aperture_by_id: dict | None = None
    portal_region_by_id: dict | None = None

    for source in execution_input.sources:
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'GA execution missing exact DirectivityDataset for {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('GA execution DirectivityDataset identity mismatch')
        source_world = _position_tuple(source.source_reference_point)
        source_local = _local(source_world, origin)

        for receiver in execution_input.receivers:
            receiver_world = _position_tuple(receiver.world_position)
            receiver_local = _local(receiver_world, origin)

            if portal_geometry:
                if execution_input.portal_graph is None:
                    if (
                        execution_input.maximum_portal_crossings != 1
                        or execution_input.portal_apertures is None
                        or len(execution_input.portal_apertures) != 1
                        or source.acoustic_region_id is None
                        or receiver.acoustic_region_id is None
                    ):
                        raise ValueError(
                            'multi-region Portal execution input is missing exact bounded topology'
                        )
                    aperture = execution_input.portal_apertures[0]
                    if source.acoustic_region_id == receiver.acoustic_region_id:
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='INVALID_REGION_SEQUENCE',
                                reason=(
                                    'current bounded multi-region lane requires endpoints on '
                                    'opposite sides of exactly one explicit Portal'
                                ),
                            )
                        )
                        continue
    
                    crossing = resolve_direct_portal_crossing(
                        aperture,
                        start=source_world,
                        end=receiver_world,
                        from_region_id=source.acoustic_region_id,
                        to_region_id=receiver.acoustic_region_id,
                        tolerance_m=execution_input.geometric_tolerance_m,
                    )
                    if crossing is None:
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='INVALID_PORTAL_CROSSING',
                                reason=(
                                    'source-to-receiver segment does not cross the exact '
                                    'directed Portal aperture within declared tolerance'
                                ),
                            )
                        )
                        continue
    
                    if any(
                        _segment_blocked(
                            compiled_geometry,
                            segment_start,
                            segment_end,
                            tolerance=execution_input.geometric_tolerance_m,
                            distance_scaled_tolerance=True,
                            occluder_triangles=occluder_triangles,
                        )
                        for segment_start, segment_end in (
                            (source_world, crossing),
                            (crossing, receiver_world),
                        )
                    ):
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='BLOCKED_VISIBILITY',
                                reason=(
                                    'exact R120 opaque triangle surface blocks a Portal '
                                    'propagation segment'
                                ),
                            )
                        )
                        continue
    
                    direct_departure = _vector(source_world, receiver_world)
                    direct_length = _distance(source_world, receiver_world)
                    direct_bands: list[DeterministicPathBandQuantity] = []
                    directivity_failed = False
                    for frequency_hz in execution_input.frequency_centers_hz:
                        directivity = _directivity_contribution(
                            dataset,
                            frequency_hz=frequency_hz,
                            source_axis=source.source_axis,
                            departure_direction=direct_departure,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if directivity is None:
                            directivity_failed = True
                            break
                        spreading = 1.0 / (direct_length * direct_length)
                        energy = spreading * directivity.energy_factor
                        if not isfinite(energy):
                            directivity_failed = True
                            break
                        direct_bands.append(
                            DeterministicPathBandQuantity(
                                center_hz=frequency_hz,
                                spreading_factor_per_m2=spreading,
                                source_directivity=directivity,
                                relative_energy_transport_per_m2=energy,
                            )
                        )
                    if directivity_failed:
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='UNSUPPORTED_DIRECTIVITY',
                                reason=(
                                    'exact source directivity cannot evaluate Portal-path '
                                    'departure angle/frequency'
                                ),
                            )
                        )
                        continue
    
                    interaction = DeterministicPathInteraction(
                        kind='portal_crossing',
                        point=_rounded_position(
                            crossing,
                            execution_input.identity_decimal_places,
                        ),
                        portal_id=aperture.portal_id,
                        from_region_id=source.acoustic_region_id,
                        to_region_id=receiver.acoustic_region_id,
                    )
                    paths.append(
                        _make_path(
                            path_type='direct',
                            source=source,
                            receiver=receiver,
                            points=(),
                            surface_ids=(),
                            length_m=direct_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=direct_departure,
                            arrival=direct_departure,
                            bands=direct_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                            typed_interactions=(interaction,),
                            ordered_region_ids=(
                                source.acoustic_region_id,
                                receiver.acoustic_region_id,
                            ),
                        )
                    )
                    continue
    
                graph = execution_input.portal_graph
                if (
                    execution_input.maximum_portal_crossings
                    != graph.maximum_portal_crossings
                    or execution_input.portal_apertures is None
                    or execution_input.region_declarations is None
                    or source.acoustic_region_id is None
                    or receiver.acoustic_region_id is None
                ):
                    raise ValueError(
                        'multi-Portal execution input is missing exact bounded topology authority'
                    )

                if portal_aperture_by_id is None:
                    portal_aperture_by_id = {
                        item.portal_id: item
                        for item in execution_input.portal_apertures
                    }
                    if len(portal_aperture_by_id) != len(
                        execution_input.portal_apertures
                    ):
                        raise ValueError('multi-Portal execution input has duplicate Portal identity')
                    portal_region_by_id = {
                        item.region_id: item
                        for item in execution_input.region_declarations
                    }
                    if len(portal_region_by_id) != len(
                        execution_input.region_declarations
                    ):
                        raise ValueError('multi-Portal execution input has duplicate region identity')

                topology_key = (
                    source.acoustic_region_id,
                    receiver.acoustic_region_id,
                )
                cached_topology = topology_paths_cache.get(topology_key)
                if cached_topology is None:
                    try:
                        cached_topology = enumerate_simple_directed_region_paths(
                            graph,
                            source_region_id=source.acoustic_region_id,
                            receiver_region_id=receiver.acoustic_region_id,
                        )
                    except ValueError as exc:
                        cached_topology = exc
                    topology_paths_cache[topology_key] = cached_topology
                if isinstance(cached_topology, ValueError):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='direct',
                            decision='UNSUPPORTED_PORTAL_TOPOLOGY',
                            reason=str(cached_topology),
                        )
                    )
                    continue
                topology_paths = cached_topology

                if not topology_paths:
                    if directed_region_reachable(
                        graph,
                        source_region_id=source.acoustic_region_id,
                        receiver_region_id=receiver.acoustic_region_id,
                    ):
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='PORTAL_CROSSING_LIMIT_EXCEEDED',
                                reason=(
                                    'source/receiver regions are connected by explicit '
                                    'directed Portal adjacency, but every simple path exceeds '
                                    f'maximum_portal_crossings={graph.maximum_portal_crossings}'
                                ),
                            )
                        )
                    else:
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='DISCONNECTED_REGION_GRAPH',
                                reason=(
                                    'no directed simple Portal path connects the explicitly '
                                    'bound source and receiver AcousticRegions'
                                ),
                            )
                        )
                    continue

                direct_departure = _vector(source_world, receiver_world)
                direct_length = _distance(source_world, receiver_world)
                direction_norm_sq = _dot(direct_departure, direct_departure)
                if direction_norm_sq <= execution_input.geometric_tolerance_m ** 2:
                    raise ValueError('Portal path source/receiver segment is degenerate')

                for ordered_region_ids, ordered_portal_ids in topology_paths:
                    crossings: list[tuple[float, float, float]] = []
                    interactions: list[DeterministicPathInteraction] = []
                    candidate_failed = False
                    previous_parameter = -1.0
                    parameter_tolerance = min(
                        0.25,
                        execution_input.geometric_tolerance_m
                        / max(direct_length, execution_input.geometric_tolerance_m),
                    )

                    for portal_index, portal_id in enumerate(ordered_portal_ids):
                        aperture = portal_aperture_by_id.get(portal_id)
                        if aperture is None:
                            raise ValueError(
                                f'Portal graph references unresolved aperture identity {portal_id}'
                            )
                        from_region_id = ordered_region_ids[portal_index]
                        to_region_id = ordered_region_ids[portal_index + 1]
                        crossing = resolve_direct_portal_crossing(
                            aperture,
                            start=source_world,
                            end=receiver_world,
                            from_region_id=from_region_id,
                            to_region_id=to_region_id,
                            tolerance_m=execution_input.geometric_tolerance_m,
                        )
                        if crossing is None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='direct',
                                    decision='INVALID_PORTAL_CROSSING',
                                    reason=(
                                        f'Portal sequence {ordered_portal_ids} misses exact '
                                        f'directed aperture {portal_id} or its orientation'
                                    ),
                                )
                            )
                            candidate_failed = True
                            break
                        parameter = (
                            _dot(_vector(source_world, crossing), direct_departure)
                            / direction_norm_sq
                        )
                        if parameter <= previous_parameter + parameter_tolerance:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='direct',
                                    decision='INVALID_REGION_SEQUENCE',
                                    reason=(
                                        f'Portal sequence {ordered_portal_ids} does not intersect '
                                        'the source-to-receiver segment in declared region order'
                                    ),
                                )
                            )
                            candidate_failed = True
                            break
                        previous_parameter = parameter
                        crossings.append(crossing)
                        interactions.append(
                            DeterministicPathInteraction(
                                kind='portal_crossing',
                                point=_rounded_position(
                                    crossing,
                                    execution_input.identity_decimal_places,
                                ),
                                portal_id=portal_id,
                                from_region_id=from_region_id,
                                to_region_id=to_region_id,
                            )
                        )
                    if candidate_failed:
                        continue

                    segment_points = (
                        (source_world,)
                        + tuple(crossings)
                        + (receiver_world,)
                    )
                    segment_evidence: list[PortalRegionSegmentEvidence] = []
                    for segment_index, region_id in enumerate(ordered_region_ids):
                        region = portal_region_by_id.get(region_id)
                        if region is None:
                            raise ValueError(
                                f'Portal graph path references unresolved region {region_id}'
                            )
                        segment_start = segment_points[segment_index]
                        segment_end = segment_points[segment_index + 1]
                        shell_triangles = region_shell_cache.get(region_id)
                        if shell_triangles is None:
                            shell_triangles = _region_shell_triangles(
                                compiled_geometry,
                                region,
                                execution_input.portal_apertures,
                            )
                            region_shell_cache[region_id] = shell_triangles
                        membership = region_segment_membership_with_portal_caps(
                            compiled_geometry=compiled_geometry,
                            region=region,
                            apertures=execution_input.portal_apertures,
                            start=segment_start,
                            end=segment_end,
                            tolerance_m=execution_input.geometric_tolerance_m,
                            shell_triangles=shell_triangles,
                        )
                        if membership != 'valid':
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='direct',
                                    decision='INTERMEDIATE_REGION_MEMBERSHIP_FAILURE',
                                    reason=(
                                        f'Portal sequence {ordered_portal_ids} segment '
                                        f'{segment_index} is not strictly valid in explicit '
                                        f'AcousticRegion {region_id} (membership={membership})'
                                    ),
                                )
                            )
                            candidate_failed = True
                            break
                        if _segment_blocked(
                            compiled_geometry,
                            segment_start,
                            segment_end,
                            tolerance=execution_input.geometric_tolerance_m,
                            distance_scaled_tolerance=True,
                            occluder_triangles=occluder_triangles,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='direct',
                                    decision='BLOCKED_VISIBILITY',
                                    reason=(
                                        f'exact R120 opaque triangle surface blocks segment '
                                        f'{segment_index} of Portal sequence {ordered_portal_ids}'
                                    ),
                                )
                            )
                            candidate_failed = True
                            break
                        segment_evidence.append(
                            PortalRegionSegmentEvidence(
                                segment_index=segment_index,
                                region_id=region_id,
                                start_point=_rounded_position(
                                    segment_start,
                                    execution_input.identity_decimal_places,
                                ),
                                end_point=_rounded_position(
                                    segment_end,
                                    execution_input.identity_decimal_places,
                                ),
                                membership_result='valid',
                                occlusion_result='clear',
                            )
                        )
                    if candidate_failed:
                        continue

                    direct_bands: list[DeterministicPathBandQuantity] = []
                    directivity_failed = False
                    for frequency_hz in execution_input.frequency_centers_hz:
                        directivity = _directivity_contribution(
                            dataset,
                            frequency_hz=frequency_hz,
                            source_axis=source.source_axis,
                            departure_direction=direct_departure,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if directivity is None:
                            directivity_failed = True
                            break
                        spreading = 1.0 / (direct_length * direct_length)
                        energy = spreading * directivity.energy_factor
                        if not isfinite(energy):
                            directivity_failed = True
                            break
                        direct_bands.append(
                            DeterministicPathBandQuantity(
                                center_hz=frequency_hz,
                                spreading_factor_per_m2=spreading,
                                source_directivity=directivity,
                                relative_energy_transport_per_m2=energy,
                            )
                        )
                    if directivity_failed:
                        rejected.append(
                            RejectedPathCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                path_type='direct',
                                decision='UNSUPPORTED_DIRECTIVITY',
                                reason=(
                                    f'exact source directivity cannot evaluate Portal graph '
                                    f'path {ordered_portal_ids}'
                                ),
                            )
                        )
                        continue

                    paths.append(
                        _make_path(
                            path_type='direct',
                            source=source,
                            receiver=receiver,
                            points=(),
                            surface_ids=(),
                            length_m=direct_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=direct_departure,
                            arrival=direct_departure,
                            bands=direct_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                            typed_interactions=tuple(interactions),
                            ordered_region_ids=ordered_region_ids,
                            region_segment_evidence=tuple(segment_evidence),
                        )
                    )
                _append_portal_graph_reflections(
                    execution_input=execution_input,
                    compiled_geometry=compiled_geometry,
                    source=source,
                    receiver=receiver,
                    dataset=dataset,
                    material_resolver=material_resolver,
                    topology_paths=topology_paths,
                    paths=paths,
                    rejected=rejected,
                    occluder_triangles=occluder_triangles,
                    region_shell_cache=region_shell_cache,
                )
                continue

            images: tuple[NativeImageSource, ...] = ()
            if not general_geometry:
                images = engine.execute_shoebox(
                    dimensions_m=execution_input.room_dimensions_m,
                    source_local_m=source_local,
                    receiver_local_m=receiver_local,
                )

                _nearest_engine_image(
                    images,
                    source_local,
                    tolerance_m=execution_input.engine_image_match_tolerance_m,
                    label='direct',
                )
            direct_departure = _vector(source_world, receiver_world)
            direct_length = _distance(source_world, receiver_world)
            if _segment_blocked(
                compiled_geometry,
                source_world,
                receiver_world,
                tolerance=execution_input.geometric_tolerance_m,
                distance_scaled_tolerance=general_geometry,
                occluder_triangles=occluder_triangles,
            ):
                rejected.append(
                    RejectedPathCandidate(
                        source_entity_id=source.source_entity_id,
                        receiver_id=receiver.receiver_id,
                        path_type='direct',
                        decision='BLOCKED_VISIBILITY',
                        reason='exact R120 triangle surface blocks source-to-receiver segment',
                    )
                )
            else:
                direct_bands: list[DeterministicPathBandQuantity] = []
                directivity_failed = False
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=direct_departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        directivity_failed = True
                        break
                    spreading = 1.0 / (direct_length * direct_length)
                    energy = spreading * directivity.energy_factor
                    if not isfinite(energy):
                        directivity_failed = True
                        break
                    direct_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            relative_energy_transport_per_m2=energy,
                        )
                    )
                if directivity_failed:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='direct',
                            decision='UNSUPPORTED_DIRECTIVITY',
                            reason=(
                                'exact source directivity cannot evaluate departure '
                                'angle/frequency; no omnidirectional fallback is permitted'
                            ),
                        )
                    )
                else:
                    paths.append(
                        _make_path(
                            path_type='direct',
                            source=source,
                            receiver=receiver,
                            points=(),
                            surface_ids=(),
                            length_m=direct_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=direct_departure,
                            arrival=direct_departure,
                            bands=direct_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                        )
                    )

            if general_geometry:
                for surface_id in execution_input.unsupported_reflection_surface_ids or ():
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'semantic surface is nonplanar or degenerate within the '
                                'declared general-planar tolerance; it is retained for '
                                'visibility but not silently planarized'
                            ),
                        )
                    )
            else:
                for mapping in sorted(
                    (
                        item
                        for item in compiled_geometry.surface_mapping
                        if item.semantic_class != 'room_boundary'
                    ),
                    key=lambda item: item.source_surface_id,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(mapping.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate pyroomacoustics image-source foundation does not '
                                'silently synthesize first-order images for non-shoebox '
                                'semantic surfaces; the exact surface remains an occluder'
                            ),
                        )
                    )

            for plane in execution_input.boundary_planes:
                mirrored_world = _mirror_source(source_world, plane)
                if general_geometry:
                    image_world = mirrored_world
                else:
                    target_local = _local(mirrored_world, origin)
                    image_local = _nearest_engine_image(
                        images,
                        target_local,
                        tolerance_m=execution_input.engine_image_match_tolerance_m,
                        label=f'first reflection {plane.source_surface_id}',
                    )
                    image_world = _world(image_local, origin)
                reflection = _reflection_point(
                    image_world,
                    receiver_world,
                    plane,
                    tolerance=execution_input.geometric_tolerance_m,
                )
                if reflection is None or not _point_on_surface(
                    compiled_geometry,
                    plane.source_surface_id,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate image source does not intersect the exact '
                                'semantic R120 surface triangle extent'
                            ),
                        )
                    )
                    continue

                ignored = (
                    frozenset()
                    if general_geometry
                    else frozenset((plane.source_surface_id,))
                )
                if _segment_blocked(
                    compiled_geometry,
                    source_world,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                    occluder_triangles=occluder_triangles,
                ) or _segment_blocked(
                    compiled_geometry,
                    reflection,
                    receiver_world,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                    occluder_triangles=occluder_triangles,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='BLOCKED_VISIBILITY',
                            reason=(
                                'exact R120 triangle surface blocks one reflection segment'
                            ),
                        )
                    )
                    continue

                departure = _vector(source_world, reflection)
                arrival = _vector(reflection, receiver_world)
                path_length = _distance(source_world, reflection) + _distance(
                    reflection,
                    receiver_world,
                )
                incidence = _reflection_incidence(
                    plane,
                    departure,
                    decimals=execution_input.identity_decimal_places,
                )
                incidence_angle_tolerance = (
                    execution_input.incidence_exact_angle_tolerance_deg or 1.0
                )
                if plane.material_authority is None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason=(
                                'reflection surface has no exact geometric material authority'
                            ),
                        )
                    )
                    continue
                resolved_material = material_resolver(plane.material_authority)
                if (
                    resolved_material is None
                    or resolved_material.authority_ref != plane.material_authority
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason='exact reflection material authority cannot be resolved',
                        )
                    )
                    continue

                reflection_bands: list[DeterministicPathBandQuantity] = []
                failure: PathCandidateDecision | None = None
                failure_reason = ''
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity cannot evaluate reflected-path '
                            'departure angle/frequency'
                        )
                        break
                    boundary = _material_contribution(
                        resolved_material,
                        plane,
                        frequency_hz=frequency_hz,
                        tolerance=execution_input.geometric_tolerance_m,
                        incidence=incidence,
                        incidence_angle_tolerance_deg=incidence_angle_tolerance,
                    )
                    if boundary is None:
                        failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                        failure_reason = (
                            'surface lacks an exact matching banded absorption/scattering '
                            'quantity; no reflection coefficient/phase is fabricated'
                        )
                        break
                    spreading = 1.0 / (path_length * path_length)
                    energy = (
                        spreading
                        * directivity.energy_factor
                        * boundary.specular_energy_factor
                    )
                    if not isfinite(energy):
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity energy is not finitely '
                            'representable'
                        )
                        break
                    reflection_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            boundary_material=boundary,
                            relative_energy_transport_per_m2=energy,
                        )
                    )
                if failure is not None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision=failure,
                            reason=failure_reason,
                        )
                    )
                    continue

                paths.append(
                    _make_path(
                        path_type='specular_reflection',
                        source=source,
                        receiver=receiver,
                        points=(reflection,),
                        surface_ids=(plane.source_surface_id,),
                        length_m=path_length,
                        sound_speed_m_s=execution_input.sound_speed_m_s,
                        departure=departure,
                        arrival=arrival,
                        bands=reflection_bands,
                        decimals=execution_input.identity_decimal_places,
                        solver_implementation_ref=execution_input.solver_implementation_ref,
                        typed_interactions=(
                            DeterministicPathInteraction(
                                kind='reflection',
                                point=_rounded_position(
                                    reflection,
                                    execution_input.identity_decimal_places,
                                ),
                                surface_id=plane.source_surface_id,
                                incidence_angle_deg=incidence[0],
                                incidence_cosine=incidence[1],
                            ),
                        ),
                    )
                )

            if general_geometry and execution_input.maximum_reflection_order == 2:
                ordered_planes = tuple(
                    sorted(
                        execution_input.boundary_planes,
                        key=lambda item: item.source_surface_id,
                    )
                )
                for first_plane in ordered_planes:
                    for second_plane in ordered_planes:
                        surface_ids = (
                            first_plane.source_surface_id,
                            second_plane.source_surface_id,
                        )
                        if first_plane.source_surface_id == second_plane.source_surface_id:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'same-surface immediate repeat is a degenerate '
                                        'second-order interaction and is not synthesized'
                                    ),
                                )
                            )
                            continue
                        if _planes_coincident(
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order planes are coincident within '
                                        'the declared geometric tolerance'
                                    ),
                                )
                            )
                            continue

                        reconstructed = _second_order_reflection_points(
                            source_world,
                            receiver_world,
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if reconstructed is None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order image reconstruction has no '
                                        'unambiguous finite plane intersection'
                                    ),
                                )
                            )
                            continue
                        first_point, second_point = reconstructed

                        segment_lengths = (
                            _distance(source_world, first_point),
                            _distance(first_point, second_point),
                            _distance(second_point, receiver_world),
                        )
                        if any(
                            value <= execution_input.geometric_tolerance_m
                            for value in segment_lengths
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection points collapse or create '
                                        'a zero-length propagation segment'
                                    ),
                                )
                            )
                            continue

                        if not _point_on_surface(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'first second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue
                        if not _point_on_surface(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue

                        if _point_has_other_surface_contact(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                            occluder_triangles=occluder_triangles,
                        ) or _point_has_other_surface_contact(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                            occluder_triangles=occluder_triangles,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection contact is shared-edge or '
                                        'multi-surface ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if (
                            _segment_grazes_plane(
                                source_world,
                                first_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                second_point,
                                receiver_world,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'grazing or plane-parallel second-order contact is '
                                        'ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if any(
                            _segment_blocked(
                                compiled_geometry,
                                segment_start,
                                segment_end,
                                tolerance=execution_input.geometric_tolerance_m,
                                distance_scaled_tolerance=True,
                                occluder_triangles=occluder_triangles,
                            )
                            for segment_start, segment_end in (
                                (source_world, first_point),
                                (first_point, second_point),
                                (second_point, receiver_world),
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='BLOCKED_VISIBILITY',
                                    reason=(
                                        'exact R120 triangle surface blocks one of three '
                                        'ordered second-order propagation segments'
                                    ),
                                )
                            )
                            continue

                        resolved_materials: list[GeometricMaterialAuthority] = []
                        material_failure = False
                        for interaction_plane in (first_plane, second_plane):
                            if interaction_plane.material_authority is None:
                                material_failure = True
                                break
                            resolved = material_resolver(
                                interaction_plane.material_authority
                            )
                            if (
                                resolved is None
                                or resolved.authority_ref
                                != interaction_plane.material_authority
                            ):
                                material_failure = True
                                break
                            resolved_materials.append(resolved)
                        if material_failure or len(resolved_materials) != 2:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                                    reason=(
                                        'one or more ordered second-order surfaces lack '
                                        'an exact resolvable geometric material authority'
                                    ),
                                )
                            )
                            continue

                        departure = _vector(source_world, first_point)
                        arrival = _vector(second_point, receiver_world)
                        path_length = sum(segment_lengths)
                        incidences = (
                            _reflection_incidence(
                                first_plane,
                                departure,
                                decimals=execution_input.identity_decimal_places,
                            ),
                            _reflection_incidence(
                                second_plane,
                                _vector(first_point, second_point),
                                decimals=execution_input.identity_decimal_places,
                            ),
                        )
                        incidence_angle_tolerance = (
                            execution_input.incidence_exact_angle_tolerance_deg or 1.0
                        )
                        second_order_bands: list[
                            DeterministicPathBandQuantity
                        ] = []
                        failure: PathCandidateDecision | None = None
                        failure_reason = ''
                        for frequency_hz in execution_input.frequency_centers_hz:
                            directivity = _directivity_contribution(
                                dataset,
                                frequency_hz=frequency_hz,
                                source_axis=source.source_axis,
                                departure_direction=departure,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            if directivity is None:
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity cannot evaluate second-order '
                                    'departure angle/frequency'
                                )
                                break

                            boundary_contributions: list[
                                BoundaryMaterialContribution
                            ] = []
                            for interaction_plane, resolved, incidence in zip(
                                (first_plane, second_plane),
                                resolved_materials,
                                incidences,
                                strict=True,
                            ):
                                boundary = _material_contribution(
                                    resolved,
                                    interaction_plane,
                                    frequency_hz=frequency_hz,
                                    tolerance=execution_input.geometric_tolerance_m,
                                    incidence=incidence,
                                    incidence_angle_tolerance_deg=(
                                        incidence_angle_tolerance
                                    ),
                                )
                                if boundary is None:
                                    failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                                    failure_reason = (
                                        'one or more second-order surfaces lack an exact '
                                        'matching banded absorption/scattering quantity; '
                                        'no reflection coefficient/phase is fabricated'
                                    )
                                    break
                                boundary_contributions.append(boundary)
                            if failure is not None:
                                break

                            spreading = 1.0 / (path_length * path_length)
                            specular_product = 1.0
                            for boundary in boundary_contributions:
                                specular_product *= boundary.specular_energy_factor
                            energy = (
                                spreading
                                * directivity.energy_factor
                                * specular_product
                            )
                            if not isfinite(energy):
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity energy is not '
                                    'finitely representable'
                                )
                                break
                            second_order_bands.append(
                                DeterministicPathBandQuantity(
                                    center_hz=frequency_hz,
                                    spreading_factor_per_m2=spreading,
                                    source_directivity=directivity,
                                    boundary_materials=tuple(
                                        boundary_contributions
                                    ),
                                    relative_energy_transport_per_m2=energy,
                                )
                            )
                        if failure is not None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision=failure,
                                    reason=failure_reason,
                                )
                            )
                            continue

                        paths.append(
                            _make_path(
                                path_type='specular_reflection',
                                source=source,
                                receiver=receiver,
                                points=(first_point, second_point),
                                surface_ids=surface_ids,
                                length_m=path_length,
                                sound_speed_m_s=execution_input.sound_speed_m_s,
                                departure=departure,
                                arrival=arrival,
                                bands=second_order_bands,
                                decimals=execution_input.identity_decimal_places,
                                solver_implementation_ref=(
                                    execution_input.solver_implementation_ref
                                ),
                                typed_interactions=(
                                    DeterministicPathInteraction(
                                        kind='reflection',
                                        point=_rounded_position(
                                            first_point,
                                            execution_input.identity_decimal_places,
                                        ),
                                        surface_id=surface_ids[0],
                                        incidence_angle_deg=incidences[0][0],
                                        incidence_cosine=incidences[0][1],
                                    ),
                                    DeterministicPathInteraction(
                                        kind='reflection',
                                        point=_rounded_position(
                                            second_point,
                                            execution_input.identity_decimal_places,
                                        ),
                                        surface_id=surface_ids[1],
                                        incidence_angle_deg=incidences[1][0],
                                        incidence_cosine=incidences[1][1],
                                    ),
                                ),
                            )
                        )

    paths.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.ordered_interaction_surface_ids),
            item.ordered_interaction_surface_ids,
            item.path_id,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.interaction_surface_ids),
            item.interaction_surface_ids,
            item.decision,
            item.reason,
        )
    )
    execution_digest = _semantic_hash(
        {
            'execution_input_id': execution_input.execution_input_id,
            'execution_input_sha256': execution_input.semantic_sha256,
            'engine_id': engine.engine_id,
            'engine_version': engine.engine_version,
            'candidate_source_commit': engine.candidate_source_commit,
        }
    )
    execution_id = f'r150-ga-execution:{execution_digest}'
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'execution_id': execution_id,
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'prediction_request_sha256': execution_input.prediction_request_sha256,
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'dispatch_binding_sha256': execution_input.dispatch_binding_sha256,
        'adapter_descriptor_id': execution_input.adapter_descriptor_id,
        'adapter_descriptor_sha256': execution_input.adapter_descriptor_sha256,
        'solver_implementation_ref': execution_input.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': execution_input.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': (
            execution_input.r120_compiled_geometry_sha256
        ),
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'candidate_source_commit': engine.candidate_source_commit,
        'numeric_comparison_tolerance_m': execution_input.geometric_tolerance_m,
        'identity_decimal_places': execution_input.identity_decimal_places,
        'frequency_domain': execution_input.frequency_domain.model_dump(mode='json'),
        'path_scope': (
            (
                'multi_portal_sixth_order_specular'
                if execution_input.maximum_reflection_order == 6
                else (
                    'multi_portal_fifth_order_specular'
                    if execution_input.maximum_reflection_order == 5
                    else (
                        'multi_portal_fourth_order_specular'
                        if execution_input.maximum_reflection_order == 4
                        else 'multi_portal_third_order_specular'
                    )
                    if execution_input.maximum_reflection_order >= 3
                    else (
                        'multi_portal_second_order_specular'
                        if execution_input.maximum_reflection_order == 2
                        else (
                            'multi_portal_first_order_specular'
                            if execution_input.solver_implementation_ref
                            == HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF
                            else 'single_portal_first_order_specular'
                        )
                        if execution_input.maximum_reflection_order == 1
                        else (
                            'direct_bounded_portal_graph_propagation'
                            if execution_input.portal_graph is not None
                            else 'direct_single_portal_propagation'
                        )
                    )
                )
            )
            if portal_geometry
            else (
                'direct_through_second_order_specular'
                if execution_input.maximum_reflection_order == 2
                else 'direct_and_first_order_specular'
            )
        ),
        'coherent_phase_authority': 'UNAVAILABLE_NOT_SYNTHESIZED',
        'paths': [
            {
                **item.model_dump(mode='json'),
                'bands': [
                    {
                        key: value
                        for key, value in band.model_dump(mode='json').items()
                        if not (key == 'boundary_materials' and value is None)
                    }
                    for band in item.bands
                ],
            }
            for item in paths
        ],
        'rejected_candidates': [
            item.model_dump(mode='json') for item in rejected
        ],
    }
    digest = _semantic_hash(core)
    # Construct the artifact over the already-validated model instances
    # (revalidate_instances='never' skips re-running each path's nested
    # validation); ``core`` remains the dict payload the digest hashes.
    init_payload: dict[str, Any] = dict(core)
    init_payload['solver_implementation_ref'] = (
        execution_input.solver_implementation_ref
    )
    init_payload['solver_configuration_ref'] = (
        execution_input.solver_configuration_ref
    )
    init_payload['frequency_domain'] = execution_input.frequency_domain
    init_payload['paths'] = tuple(paths)
    init_payload['rejected_candidates'] = tuple(rejected)
    return DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **init_payload,
    )

def deterministic_path_observable_manifest(
    artifact: DeterministicPathArtifact,
) -> AcousticSolverObservableArtifact:
    return AcousticSolverObservableArtifact(
        observable=DETERMINISTIC_PATHS_OBSERVABLE,
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )

def build_deterministic_ga_result_envelope(
    *,
    artifact: DeterministicPathArtifact,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    completed_at_utc: str,
) -> AcousticSolverResultEnvelope:
    if artifact.dispatch_binding_id != dispatch.binding_id:
        raise ValueError('path artifact/dispatch identity mismatch')
    if artifact.prediction_request_id != request.request_id:
        raise ValueError('path artifact/request identity mismatch')
    return build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id=artifact.execution_id,
        execution_provenance_ref=artifact.execution_provenance_ref(),
        artifacts=(deterministic_path_observable_manifest(artifact),),
        completed_at_utc=completed_at_utc,
    )

ConfigurationResolver = Callable[
    [ExactExternalAuthorityRef],
    DeterministicGaConfiguration | None,
]
GeometryAuthority = AcousticRegionAuthority | PortalAuthority | BoundaryTerminationAuthority
GeometryAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometryAuthority | None,
]

class CadDeterministicPathArtifactRepository:
    """Append-only typed path persistence with exact authority re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver: ConfigurationResolver,
        material_resolver: MaterialAuthorityResolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.material_resolver = material_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'deterministic path and {label} repositories must share one CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_deterministic_ga_execution_inputs', 'cad_deterministic_path_artifacts')

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        authority = self.geometry_authority_resolver(ref)
        if authority is None or not isinstance(authority, expected_type):
            raise ValueError(f'{label} exact authority does not exist with expected type')
        if _authority_ref(authority) != ref:
            raise ValueError(f'{label} exact authority mismatch')
        return authority

    def _validate_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
        *,
        _resolved_out: dict | None = None,
    ) -> DeterministicGaExecutionInput:
        execution_input = DeterministicGaExecutionInput.model_validate(
            execution_input.model_dump(mode='python')
        )
        snapshot = self.snapshot_repository.get_snapshot(execution_input.snapshot_id)
        if (
            snapshot is None
            or snapshot.semantic_sha256 != execution_input.snapshot_sha256
        ):
            raise ValueError('GA execution input exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            execution_input.prediction_request_id,
            _validated_snapshot=snapshot,
        )
        if (
            request is None
            or request.request_semantic_sha256
            != execution_input.prediction_request_sha256
        ):
            raise ValueError(
                'GA execution input exact prediction request is missing or mismatched'
            )
        dispatch = self.dispatch_repository.get_dispatch(
            execution_input.dispatch_binding_id,
            _validated_snapshot=snapshot,
            _validated_request=request,
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != execution_input.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError(
                'GA execution input exact READY dispatch is missing or mismatched'
            )
        descriptor = self.dispatch_repository.get_descriptor(
            execution_input.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != execution_input.adapter_descriptor_sha256
        ):
            raise ValueError(
                'GA execution input exact adapter descriptor is missing or mismatched'
            )
        configuration = self.configuration_resolver(
            execution_input.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref()
            != execution_input.solver_configuration_ref
        ):
            raise ValueError(
                'GA execution input exact configuration is missing or mismatched'
            )
        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            execution_input.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != execution_input.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != execution_input.topology_identity_sha256
        ):
            raise ValueError('GA execution input exact R120 geometry is missing or mismatched')
        if compiled.region_authority_ref is None:
            raise ValueError('GA execution input R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('GA execution input R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('GA execution input R120 termination authority is missing')
        region = self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        portals = self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        terminations = self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )
        datasets: list[DirectivityDataset] = []
        for source in execution_input.sources:
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError(
                    'GA execution input exact DirectivityDataset is missing'
                )
            datasets.append(dataset)
        regenerated = compile_deterministic_ga_execution_input(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled,
            region_authority=region,
            portal_authority=portals,
            boundary_termination_authority=terminations,
            directivity_datasets=datasets,
            configuration=configuration,
            source_region_bindings={
                item.source_entity_id: item.acoustic_region_id
                for item in execution_input.sources
                if item.acoustic_region_id is not None
            } or None,
            receiver_region_bindings={
                item.receiver_id: item.acoustic_region_id
                for item in execution_input.receivers
                if item.acoustic_region_id is not None
            } or None,
        )
        if regenerated != execution_input:
            raise ValueError(
                'GA execution input does not reproduce from exact persisted authorities'
            )
        if _resolved_out is not None:
            _resolved_out.update(
                {
                    'snapshot': snapshot,
                    'request': request,
                    'dispatch': dispatch,
                    'descriptor': descriptor,
                    'compiled': compiled,
                    'datasets': datasets,
                }
            )
        return execution_input

    def save_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
    ) -> DeterministicGaExecutionInput:
        execution_input = self._validate_execution_input(execution_input)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input.execution_input_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicGaExecutionInput.model_validate_json(
                    existing['payload_json']
                )
                if persisted != execution_input:
                    raise ValueError(
                        'GA execution input id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_deterministic_ga_execution_inputs(
                    execution_input_id,
                    semantic_sha256,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_input.execution_input_id,
                    execution_input.semantic_sha256,
                    execution_input.snapshot_id,
                    execution_input.prediction_request_id,
                    execution_input.dispatch_binding_id,
                    execution_input.r120_compiled_geometry_id,
                    execution_input.model_dump_json(),
                    _utc_now(),
                ),
            )
        return execution_input

    def get_execution_input(
        self,
        execution_input_id: str,
        *,
        _resolved_out: dict | None = None,
    ) -> DeterministicGaExecutionInput | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_execution_input(
            DeterministicGaExecutionInput.model_validate_json(
                row['payload_json']
            ),
            _resolved_out=_resolved_out,
        )

    def _validate(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = DeterministicPathArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        resolved_bundle: dict = {}
        execution_input = self.get_execution_input(
            artifact.execution_input_id,
            _resolved_out=resolved_bundle,
        )
        if (
            execution_input is None
            or execution_input.semantic_sha256 != artifact.execution_input_sha256
            or execution_input.snapshot_id != artifact.snapshot_id
            or execution_input.prediction_request_id != artifact.prediction_request_id
            or execution_input.dispatch_binding_id != artifact.dispatch_binding_id
            or execution_input.r120_compiled_geometry_id
            != artifact.r120_compiled_geometry_id
        ):
            raise ValueError(
                'path artifact exact GA execution input is missing or mismatched'
            )
        snapshot = resolved_bundle.get('snapshot')
        if snapshot is None or snapshot.snapshot_id != artifact.snapshot_id:
            snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if snapshot is None or snapshot.semantic_sha256 != artifact.snapshot_sha256:
            raise ValueError('path artifact exact snapshot is missing or mismatched')
        request = resolved_bundle.get('request')
        if request is None or request.request_id != artifact.prediction_request_id:
            request = self.snapshot_repository.get_prediction_request(
                artifact.prediction_request_id,
                _validated_snapshot=snapshot,
            )
        if (
            request is None
            or request.request_semantic_sha256 != artifact.prediction_request_sha256
        ):
            raise ValueError('path artifact exact prediction request is missing or mismatched')
        dispatch = resolved_bundle.get('dispatch')
        if dispatch is None or dispatch.binding_id != artifact.dispatch_binding_id:
            dispatch = self.dispatch_repository.get_dispatch(
                artifact.dispatch_binding_id,
                _validated_snapshot=snapshot,
                _validated_request=request,
            )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != artifact.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError('path artifact exact READY dispatch is missing or mismatched')
        descriptor = resolved_bundle.get('descriptor')
        if (
            descriptor is None
            or descriptor.descriptor_id != artifact.adapter_descriptor_id
        ):
            descriptor = self.dispatch_repository.get_descriptor(
                artifact.adapter_descriptor_id
            )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != artifact.adapter_descriptor_sha256
        ):
            raise ValueError('path artifact exact adapter descriptor is missing or mismatched')
        if (
            artifact.solver_implementation_ref != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref != dispatch.solver_configuration_ref
        ):
            raise ValueError('path artifact solver implementation/config mismatch')

        configuration = self.configuration_resolver(
            artifact.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref() != artifact.solver_configuration_ref
        ):
            raise ValueError('path artifact exact GA configuration is missing or mismatched')

        compiled = resolved_bundle.get('compiled')
        if (
            compiled is None
            or compiled.compiled_geometry_id != artifact.r120_compiled_geometry_id
        ):
            compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
                artifact.r120_compiled_geometry_id
            )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256 != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError('path artifact exact R120 geometry is missing or mismatched')

        if compiled.region_authority_ref is None:
            raise ValueError('path artifact R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('path artifact R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('path artifact R120 termination authority is missing')
        self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )

        if artifact.path_scope in (
            'single_portal_first_order_specular',
            'multi_portal_first_order_specular',
            'multi_portal_second_order_specular',
            'multi_portal_third_order_specular',
            'multi_portal_fourth_order_specular',
            'multi_portal_fifth_order_specular',
            'multi_portal_sixth_order_specular',
        ):
            resolved_datasets = resolved_bundle.get('datasets')
            datasets_by_sha = (
                {item.semantic_sha256: item for item in resolved_datasets}
                if resolved_datasets is not None
                else {}
            )
            datasets: list[DirectivityDataset] = []
            for source_input in execution_input.sources:
                dataset = datasets_by_sha.get(
                    source_input.directivity_dataset_sha256
                )
                if dataset is None:
                    dataset = (
                        self.snapshot_repository.r110_repository
                        .directivity_repository.get_dataset_by_hash(
                            source_input.directivity_dataset_sha256
                        )
                    )
                if dataset is None:
                    raise ValueError('reflected Portal path exact DirectivityDataset is missing')
                datasets.append(dataset)
            if (
                artifact.solver_implementation_ref
                == HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF
            ):
                engine: DeterministicImageSourceEngine = (
                    HtdtPortalHighOrderSpecularChainGraphEngine()
                )
            elif (
                artifact.solver_implementation_ref
                == HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF
            ):
                engine = HtdtPortalSpecularChainGraphEngine()
            elif (
                artifact.solver_implementation_ref
                == HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF
            ):
                engine = HtdtPortalSpecularGraphEngine()
            elif (
                artifact.solver_implementation_ref
                == HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF
            ):
                engine = HtdtPortalFirstOrderReflectionEngine()
            else:
                raise ValueError(
                    'reflected Portal path artifact has unknown solver authority'
                )
            regenerated_artifact = execute_deterministic_ga(
                execution_input=execution_input,
                compiled_geometry=compiled,
                directivity_datasets=datasets,
                material_resolver=self.material_resolver,
                engine=engine,
            )
            if regenerated_artifact != artifact:
                raise ValueError(
                    'reflected Portal path artifact does not reproduce from exact current authorities'
                )
            return artifact

        source_by_id = {item.source_entity_id: item for item in snapshot.sources}
        receiver_by_id = {item.receiver_id: item for item in snapshot.receivers}
        execution_source_by_id = {
            item.source_entity_id: item for item in execution_input.sources
        }
        execution_receiver_by_id = {
            item.receiver_id: item for item in execution_input.receivers
        }
        expected_routes_cache: dict = {}
        region_shell_cache: dict[str, tuple] = {}
        occluder_triangles = _occluder_triangles(compiled)
        model_by_sha: dict = {}
        dataset_by_sha: dict = {}
        portal_apertures = execution_input.portal_apertures
        aperture_by_portal_id = (
            {item.portal_id: item for item in portal_apertures}
            if portal_apertures is not None
            else {}
        )
        region_declarations = execution_input.region_declarations
        region_by_id = (
            {item.region_id: item for item in region_declarations}
            if region_declarations is not None
            else {}
        )
        for path in artifact.paths:
            source = source_by_id.get(path.source_entity_id)
            receiver = receiver_by_id.get(path.receiver_id)
            if source is None or receiver is None:
                raise ValueError('path artifact source/receiver no longer resolves')

            portal_interactions = tuple(
                item
                for item in (path.ordered_interactions or ())
                if item.kind == 'portal_crossing'
            )
            execution_source = execution_source_by_id.get(path.source_entity_id)
            execution_receiver = execution_receiver_by_id.get(path.receiver_id)

            if execution_input.portal_graph is not None and path.ordered_region_ids is not None:
                graph = execution_input.portal_graph
                if (
                    execution_source is None
                    or execution_receiver is None
                    or execution_source.acoustic_region_id is None
                    or execution_receiver.acoustic_region_id is None
                    or execution_input.portal_apertures is None
                    or execution_input.region_declarations is None
                ):
                    raise ValueError(
                        'path Portal graph authority no longer resolves from exact execution input'
                    )
                routes_key = (
                    execution_source.acoustic_region_id,
                    execution_receiver.acoustic_region_id,
                )
                expected_routes = expected_routes_cache.get(routes_key)
                if expected_routes is None:
                    expected_routes = enumerate_simple_directed_region_paths(
                        graph,
                        source_region_id=execution_source.acoustic_region_id,
                        receiver_region_id=execution_receiver.acoustic_region_id,
                    )
                    expected_routes_cache[routes_key] = expected_routes
                portal_ids = tuple(
                    item.portal_id
                    for item in portal_interactions
                )
                if (path.ordered_region_ids, portal_ids) not in expected_routes:
                    raise ValueError(
                        'path ordered region/Portal route no longer resolves from exact graph authority'
                    )

                source_point = _position_tuple(execution_source.source_reference_point)
                receiver_point = _position_tuple(execution_receiver.world_position)
                direct = _vector(source_point, receiver_point)
                direct_norm_sq = _dot(direct, direct)
                if direct_norm_sq <= execution_input.geometric_tolerance_m ** 2:
                    raise ValueError('persisted Portal graph path has degenerate endpoints')

                crossings: list[tuple[float, float, float]] = []
                previous_parameter = -1.0
                path_length = _distance(source_point, receiver_point)
                parameter_tolerance = min(
                    0.25,
                    execution_input.geometric_tolerance_m
                    / max(path_length, execution_input.geometric_tolerance_m),
                )
                for index, interaction in enumerate(portal_interactions):
                    aperture = aperture_by_portal_id.get(interaction.portal_id or '')
                    if aperture is None:
                        raise ValueError(
                            'path Portal interaction aperture no longer resolves exactly'
                        )
                    if (
                        interaction.from_region_id != path.ordered_region_ids[index]
                        or interaction.to_region_id != path.ordered_region_ids[index + 1]
                    ):
                        raise ValueError(
                            'path Portal interaction directed adjacency no longer resolves exactly'
                        )
                    crossing = resolve_direct_portal_crossing(
                        aperture,
                        start=source_point,
                        end=receiver_point,
                        from_region_id=interaction.from_region_id,
                        to_region_id=interaction.to_region_id,
                        tolerance_m=execution_input.geometric_tolerance_m,
                    )
                    if (
                        crossing is None
                        or interaction.point
                        != _rounded_position(
                            crossing,
                            execution_input.identity_decimal_places,
                        )
                    ):
                        raise ValueError(
                            'path Portal crossing point no longer reproduces exactly'
                        )
                    parameter = (
                        _dot(_vector(source_point, crossing), direct)
                        / direct_norm_sq
                    )
                    if parameter <= previous_parameter + parameter_tolerance:
                        raise ValueError(
                            'path Portal crossing order no longer matches directed region sequence'
                        )
                    previous_parameter = parameter
                    crossings.append(crossing)

                segment_points = (
                    (source_point,)
                    + tuple(crossings)
                    + (receiver_point,)
                )
                if (
                    path.region_segment_evidence is None
                    or len(path.region_segment_evidence) != len(path.ordered_region_ids)
                ):
                    raise ValueError(
                        'path Portal graph segment proof authority is missing or incomplete'
                    )
                for index, region_id in enumerate(path.ordered_region_ids):
                    region = region_by_id.get(region_id)
                    if region is None:
                        raise ValueError(
                            'path AcousticRegion declaration no longer resolves exactly'
                        )
                    shell_triangles = region_shell_cache.get(region_id)
                    if shell_triangles is None:
                        shell_triangles = _region_shell_triangles(
                            compiled,
                            region,
                            execution_input.portal_apertures,
                        )
                        region_shell_cache[region_id] = shell_triangles
                    membership = region_segment_membership_with_portal_caps(
                        compiled_geometry=compiled,
                        region=region,
                        apertures=execution_input.portal_apertures,
                        start=segment_points[index],
                        end=segment_points[index + 1],
                        tolerance_m=execution_input.geometric_tolerance_m,
                        shell_triangles=shell_triangles,
                    )
                    if membership != 'valid':
                        raise ValueError(
                            'path region segment membership no longer reproduces exactly'
                        )
                    if _segment_blocked(
                        compiled,
                        segment_points[index],
                        segment_points[index + 1],
                        tolerance=execution_input.geometric_tolerance_m,
                        distance_scaled_tolerance=True,
                        occluder_triangles=occluder_triangles,
                    ):
                        raise ValueError(
                            'path Portal graph segment is no longer unoccluded'
                        )
                    evidence = path.region_segment_evidence[index]
                    if (
                        evidence.segment_index != index
                        or evidence.region_id != region_id
                        or evidence.start_point
                        != _rounded_position(
                            segment_points[index],
                            execution_input.identity_decimal_places,
                        )
                        or evidence.end_point
                        != _rounded_position(
                            segment_points[index + 1],
                            execution_input.identity_decimal_places,
                        )
                        or evidence.membership_result != 'valid'
                        or evidence.occlusion_result != 'clear'
                    ):
                        raise ValueError(
                            'path Portal graph persisted segment proof no longer reproduces exactly'
                        )
                if abs(path.geometric_path_length_m - path_length) > (
                    execution_input.geometric_tolerance_m
                ):
                    raise ValueError(
                        'path Portal graph geometric length no longer reproduces exactly'
                    )

            elif portal_interactions:
                if (
                    execution_source is None
                    or execution_receiver is None
                    or execution_source.acoustic_region_id is None
                    or execution_receiver.acoustic_region_id is None
                    or execution_input.portal_apertures is None
                    or len(execution_input.portal_apertures) != 1
                    or len(portal_interactions) != 1
                ):
                    raise ValueError(
                        'path Portal interaction no longer resolves from exact execution input'
                    )
                aperture = execution_input.portal_apertures[0]
                interaction = portal_interactions[0]
                if (
                    interaction.portal_id != aperture.portal_id
                    or path.ordered_region_ids
                    != (
                        execution_source.acoustic_region_id,
                        execution_receiver.acoustic_region_id,
                    )
                ):
                    raise ValueError(
                        'path Portal/region ordered identity no longer resolves exactly'
                    )
                crossing = resolve_direct_portal_crossing(
                    aperture,
                    start=_position_tuple(execution_source.source_reference_point),
                    end=_position_tuple(execution_receiver.world_position),
                    from_region_id=execution_source.acoustic_region_id,
                    to_region_id=execution_receiver.acoustic_region_id,
                    tolerance_m=execution_input.geometric_tolerance_m,
                )
                if (
                    crossing is None
                    or interaction.point
                    != _rounded_position(
                        crossing,
                        execution_input.identity_decimal_places,
                    )
                ):
                    raise ValueError(
                        'path Portal crossing point no longer reproduces exactly'
                    )

            model = model_by_sha.get(source.r110_compiled_source_sha256)
            if model is None:
                model = self.snapshot_repository.r110_repository.get_model(
                    source.r110_compiled_source_sha256
                )
                if model is not None:
                    model_by_sha[source.r110_compiled_source_sha256] = model
            if model is None:
                raise ValueError('path artifact exact R110 source is missing')
            if source.directivity_dataset_sha256 is None:
                raise ValueError('path artifact source DirectivityDataset is missing')
            dataset = dataset_by_sha.get(source.directivity_dataset_sha256)
            if dataset is None:
                dataset = (
                    self.snapshot_repository.r110_repository.directivity_repository
                    .get_dataset_by_hash(source.directivity_dataset_sha256)
                )
                if dataset is not None:
                    dataset_by_sha[source.directivity_dataset_sha256] = dataset
            if dataset is None:
                raise ValueError('path artifact exact DirectivityDataset is missing')
            for band in path.bands:
                contribution = band.source_directivity
                if contribution.dataset_semantic_sha256 != dataset.semantic_sha256:
                    raise ValueError('path directivity dataset hash mismatch')
                reevaluated = evaluate_directivity(
                    dataset,
                    frequency_hz=contribution.frequency_hz,
                    horizontal_angle_deg=contribution.horizontal_angle_deg,
                    vertical_angle_deg=contribution.vertical_angle_deg,
                    request='magnitude',
                )
                if (
                    reevaluated.decision != 'SUPPORTED'
                    or reevaluated.semantic_sha256
                    != contribution.evaluation_semantic_sha256
                    or reevaluated.magnitude_linear
                    != contribution.magnitude_linear
                ):
                    raise ValueError(
                        'path directivity contribution no longer reproduces exactly'
                    )
                boundary = band.boundary_material
                if boundary is not None:
                    material = self.material_resolver(
                        boundary.material_authority
                    )
                    if (
                        material is None
                        or material.authority_ref != boundary.material_authority
                    ):
                        raise ValueError(
                            'path boundary material exact authority is missing'
                        )
                    matching = [
                        item
                        for item in material.material.geometric_bands
                        if abs(
                            float(item.center_hz) - float(boundary.frequency_hz)
                        )
                        <= configuration.geometric_tolerance_m
                    ]
                    if len(matching) != 1:
                        raise ValueError(
                            'path boundary material band no longer resolves exactly'
                        )
                    item = matching[0]
                    expected_specular = (1.0 - item.absorption) * (
                        1.0 - item.scattering
                    )
                    if (
                        item.absorption != boundary.absorption
                        or item.scattering != boundary.scattering
                        or expected_specular != boundary.specular_energy_factor
                    ):
                        raise ValueError(
                            'path boundary material contribution no longer reproduces'
                        )

        return artifact

    def save(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicPathArtifact.model_validate_json(
                    existing['payload_json']
                )
                if persisted != artifact:
                    raise ValueError(
                        'deterministic path artifact id exists with different semantics'
                    )
                artifact = persisted
            else:
                connection.execute(
                    """
                    INSERT INTO cad_deterministic_path_artifacts(
                        artifact_id,
                        semantic_sha256,
                        execution_id,
                        execution_provenance_authority_id,
                        execution_input_id,
                        snapshot_id,
                        prediction_request_id,
                        dispatch_binding_id,
                        r120_compiled_geometry_id,
                        payload_json,
                        recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.semantic_sha256,
                        artifact.execution_id,
                        provenance.authority_id,
                        artifact.execution_input_id,
                        artifact.snapshot_id,
                        artifact.prediction_request_id,
                        artifact.dispatch_binding_id,
                        artifact.r120_compiled_geometry_id,
                        artifact.model_dump_json(),
                        _utc_now(),
                    ),
                )
        self._emit_produced_capability_manifest(artifact)
        return artifact

    def _emit_produced_capability_manifest(
        self,
        artifact: DeterministicPathArtifact,
    ) -> SolverCapabilityManifest:
        """Persist the capability manifest this committed artifact produced.

        GA result-commit emit — same shape as the R130A/polyhedral executor
        emit: produced observables narrow the descriptor-declared
        derivation, and a produced observable outside the declaration
        raises (fail closed).
        """
        descriptor = self.dispatch_repository.get_descriptor(
            artifact.adapter_descriptor_id
        )
        if descriptor is None or descriptor.semantic_sha256 != (
            artifact.adapter_descriptor_sha256
        ):
            raise ValueError(
                'deterministic GA capability emit requires the persisted '
                'exact adapter descriptor'
            )
        manifest = build_solver_capability_manifest(
            descriptor=descriptor,
            rows=derive_solver_capability_rows(
                descriptor,
                produced_observables=(DETERMINISTIC_PATHS_OBSERVABLE,),
            ),
        )
        return self.dispatch_repository.save_capability_manifest(manifest)

    def get(self, artifact_id: str) -> DeterministicPathArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            DeterministicPathArtifact.model_validate_json(row['payload_json'])
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith('deterministic-path-artifact:'):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith('r150-ga-execution-provenance:'):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_deterministic_path_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                DeterministicPathArtifact.model_validate_json(row['payload_json'])
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None

    def resolve_artifact_manifest(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverArtifactManifest | None:
        """Resolve the persisted path artifact as a typed solver manifest."""
        if not ref.authority_id.startswith('deterministic-path-artifact:'):
            return None
        artifact = self.get(ref.authority_id)
        if artifact is None or artifact.as_external_ref() != ref:
            return None
        return AcousticSolverArtifactManifest(
            artifact_ref=ref,
            observable=DETERMINISTIC_PATHS_OBSERVABLE,
            encoding_schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
            valid_frequency_domain=artifact.frequency_domain,
            solver_lineage={
                'execution_id': artifact.execution_id,
                'execution_input_id': artifact.execution_input_id,
                'execution_input_sha256': artifact.execution_input_sha256,
                'dispatch_binding_id': artifact.dispatch_binding_id,
                'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
            },
        )
