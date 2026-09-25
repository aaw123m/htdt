from __future__ import annotations

from hashlib import sha256
import json
from math import acos, degrees, isclose, sqrt
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import AcousticMaterial, GeometricAcousticBand
from htdt.cad_acoustic_snapshot import (
    SnapshotEnvironmentAuthorityRef,
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
    receiver_binding_from_scene,
)
from htdt.cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from htdt.cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    bind_prediction_request_to_solver_adapter,
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_acoustic_solver_result import CadAcousticSolverResultRepository
from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_geometric_acoustics_adapter import (
    DETERMINISTIC_GA_ADAPTER_ID,
    DETERMINISTIC_GA_ADAPTER_VERSION,
    CadDeterministicPathArtifactRepository,
    DeterministicAcousticPath,
    DeterministicGaConfiguration,
    DeterministicGaExecutionInput,
    DeterministicGaUnsupportedError,
    GeometricMaterialAuthority,
    HtdtPlanarImageSourceEngine,
    HtdtPortalDirectEngine,
    HtdtPortalGraphDirectEngine,
    HtdtPortalFirstOrderReflectionEngine,
    HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF,
    HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF,
    HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF,
    HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF,
    HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF,
    NativeImageSource,
    PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF,
    PyroomacousticsImageSourceEngine,
    build_deterministic_ga_configuration,
    build_deterministic_ga_result_envelope,
    compile_deterministic_ga_execution_input,
    execute_deterministic_ga,
)
from htdt.cad_geometric_acoustics_portal import (
    PORTAL_SIDE_SEMANTICS,
    compile_portal_graph,
)
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    BoundaryTerminationAuthority,
    BoundaryTerminationDeclaration,
    ExactExternalAuthorityRef,
    PortalAuthority,
    PortalBoundaryEdge,
    PortalDeclaration,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
    make_acoustic_region_authority,
    make_boundary_termination_authority,
    make_portal_authority,
    make_r120_geometry_compilation_request,
)
from htdt.r120_geometry_compiler_repository import R120GeometryCompilerRepository
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


NOW = '2026-09-20T00:30:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


SHOEBOX_OBJ = b'''\
v 0 0 0
v 4 0 0
v 4 3 0
v 0 3 0
v 0 0 2
v 4 0 2
v 4 3 2
v 0 3 2
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 4 8 7
f 4 7 3
f 1 5 8
f 1 8 4
f 2 3 7
f 2 7 6
'''

SHOEBOX_WITH_TETRA_OCCLUDER_OBJ = SHOEBOX_OBJ + b'''\
v 1.8 1.3 0.5
v 2.2 1.3 0.5
v 2.0 1.7 0.5
v 2.0 1.5 1.5
f 9 11 10
f 9 10 12
f 9 12 11
f 10 11 12
'''


TRAPEZOID_OBJ = b'''\
v 0 0 0
v 4 0 0
v 3 3 0
v 0 3 0
v 0 0 2
v 4 0 2
v 3 3 2
v 0 3 2
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 4 8 7
f 4 7 3
f 1 5 8
f 1 8 4
f 2 3 7
f 2 7 6
'''

TRAPEZOID_WITH_REFLECTION_BLOCKER_OBJ = TRAPEZOID_OBJ + b'''\
v 2.25 1.60 0.80
v 2.50 1.60 0.80
v 2.36 1.86 0.80
v 2.36 1.72 1.20
f 9 11 10
f 9 10 12
f 9 12 11
f 10 11 12
'''

TRAPEZOID_WITH_SMALL_CUBE_OBJ = TRAPEZOID_OBJ + b'''\
v 2.4 0.3 0.2
v 2.6 0.3 0.2
v 2.6 0.6 0.2
v 2.4 0.6 0.2
v 2.4 0.3 0.5
v 2.6 0.3 0.5
v 2.6 0.6 0.5
v 2.4 0.6 0.5
f 9 11 10
f 9 12 11
f 13 14 15
f 13 15 16
f 9 10 14
f 9 14 13
f 12 16 15
f 12 15 11
f 9 13 16
f 9 16 12
f 10 11 15
f 10 15 14
'''


def _digest(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _ref(name: str, seed: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=sha256(seed.encode('utf-8')).hexdigest(),
    )


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


def _snapshot_authority_resolvers(
    snapshot,
    *,
    preflight_graph=None,
) -> AcousticSnapshotAuthorityResolvers:
    """Honest registry resolving every external authority a snapshot claims."""
    external: dict[tuple[str, str, str], ExactExternalAuthorityRef] = {}
    for ref in (
        snapshot.acoustic_region_authority_ref,
        snapshot.portal_authority_ref,
        snapshot.boundary_termination_authority_ref,
    ):
        if ref is not None:
            external[_ref_key(ref)] = ref
    for surface in snapshot.surface_boundary_configuration:
        for ref in (
            surface.material_authority,
            surface.boundary_physics_authority,
        ):
            if ref is not None:
                external[_ref_key(ref)] = ref
    environment = snapshot.environment
    domain_ref = snapshot.valid_frequency_domain_authority_ref
    preflight_ref = snapshot.geometric_acoustics_topology_preflight_ref
    return AcousticSnapshotAuthorityResolvers(
        environment=(
            lambda ref: environment
            if environment is not None and ref == environment.authority
            else None
        ),
        sound_speed_source=(
            lambda ref: environment.sound_speed_m_s
            if environment is not None
            and ref == environment.sound_speed_source_authority
            else None
        ),
        temperature_source=(
            lambda ref: environment.temperature_c
            if environment is not None
            and ref == environment.temperature_source_authority
            else None
        ),
        valid_frequency_domain=(
            lambda ref: snapshot.valid_frequency_domain
            if domain_ref is not None and ref == domain_ref
            else None
        ),
        geometric_topology_preflight=(
            lambda ref: preflight_graph
            if preflight_graph is not None
            and preflight_ref is not None
            and ref == preflight_ref
            and preflight_graph.as_external_ref() == ref
            else None
        ),
        external_authority=lambda ref: external.get(_ref_key(ref)),
    )


def _material(*, supported: bool = True) -> GeometricMaterialAuthority:
    material = AcousticMaterial(
        material_id='fixture-wall',
        provenance='fixture exact GA material',
        version='1',
        wave_model='unsupported',
        geometric_model='banded' if supported else 'unsupported',
        geometric_bands=(
            (
                GeometricAcousticBand(
                    center_hz=500.0,
                    absorption=0.2,
                    scattering=0.1,
                ),
                GeometricAcousticBand(
                    center_hz=1000.0,
                    absorption=0.3,
                    scattering=0.2,
                ),
            )
            if supported
            else ()
        ),
    )
    payload = material.model_dump(mode='json')
    ref = ExactExternalAuthorityRef(
        authority_id='fixture-material:wall',
        authority_version=material.version,
        semantic_hash_sha256=_digest(payload),
    )
    return GeometricMaterialAuthority(authority_ref=ref, material=material)


def _semantic_geometry(*, occluder: bool):
    mesh = import_raw_visual_mesh(
        SHOEBOX_WITH_TETRA_OCCLUDER_OBJ if occluder else SHOEBOX_OBJ,
        source_name='r150-ga-fixture.obj',
    )
    ids = raw_triangle_ids(mesh)
    assignments = [
        SurfaceSemanticAssignment(
            surface_key='floor-z-min',
            triangle_ids=ids[0:2],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='ceiling-z-max',
            triangle_ids=ids[2:4],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='front-y-min',
            triangle_ids=ids[4:6],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='rear-y-max',
            triangle_ids=ids[6:8],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='left-x-min',
            triangle_ids=ids[8:10],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='right-x-max',
            triangle_ids=ids[10:12],
            semantic_class='room_boundary',
        ),
    ]
    if occluder:
        assignments.append(
            SurfaceSemanticAssignment(
                surface_key='internal-occluder',
                triangle_ids=ids[12:16],
                semantic_class='object_surface',
            )
        )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are explicit HTDT metres',
        ),
        surface_assignments=tuple(assignments),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)



def _general_semantic_geometry(
    mesh_bytes: bytes,
    *,
    object_mode: str = 'none',
):
    mesh = import_raw_visual_mesh(
        mesh_bytes,
        source_name='r150-ga-general-fixture.obj',
    )
    ids = raw_triangle_ids(mesh)
    assignments = [
        SurfaceSemanticAssignment(
            surface_key='floor-z-min',
            triangle_ids=ids[0:2],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='ceiling-z-max',
            triangle_ids=ids[2:4],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='front-y-min',
            triangle_ids=ids[4:6],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='rear-y-max',
            triangle_ids=ids[6:8],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='left-x-min',
            triangle_ids=ids[8:10],
            semantic_class='room_boundary',
        ),
        SurfaceSemanticAssignment(
            surface_key='right-slanted',
            triangle_ids=ids[10:12],
            semantic_class='room_boundary',
        ),
    ]
    if object_mode == 'grouped-tetra':
        assignments.append(
            SurfaceSemanticAssignment(
                surface_key='internal-blocker',
                triangle_ids=ids[12:16],
                semantic_class='object_surface',
            )
        )
    elif object_mode == 'cube-faces':
        object_keys = (
            'cube-z-min',
            'cube-z-max',
            'cube-y-min',
            'cube-y-max',
            'cube-x-min',
            'cube-x-max',
        )
        for offset, key in enumerate(object_keys):
            start = 12 + 2 * offset
            assignments.append(
                SurfaceSemanticAssignment(
                    surface_key=key,
                    triangle_ids=ids[start:start + 2],
                    semantic_class='object_surface',
                )
            )
    elif object_mode != 'none':
        raise ValueError(f'unknown object_mode: {object_mode}')
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are explicit HTDT metres',
        ),
        surface_assignments=tuple(assignments),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)



PORTAL_POLICY = 'general_planar_multi_region_portal_v1'
PORTAL_REGION_SURFACES = {
    'region-a': (
        'portal-floor-a',
        'portal-ceiling-a',
        'portal-front-a',
        'portal-rear-a',
        'portal-left-a',
        'portal-interface',
    ),
    'region-b': (
        'portal-floor-b',
        'portal-ceiling-b',
        'portal-front-b',
        'portal-rear-b',
        'portal-right-b',
        'portal-interface',
    ),
}


def _portal_semantic_geometry(*, occluder: bool = False):
    vertices: list[tuple[float, float, float]] = []
    vertex_index: dict[tuple[float, float, float], int] = {}
    faces: list[tuple[int, int, int]] = []
    face_indices: dict[str, list[int]] = {}

    def vid(point: tuple[float, float, float]) -> int:
        if point not in vertex_index:
            vertex_index[point] = len(vertices)
            vertices.append(point)
        return vertex_index[point]

    def add_quad(
        key: str,
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        c_: tuple[float, float, float],
        d: tuple[float, float, float],
    ) -> None:
        start = len(faces)
        ia, ib, ic, id_ = (vid(point) for point in (a, b, c_, d))
        faces.extend(((ia, ib, ic), (ia, ic, id_)))
        face_indices.setdefault(key, []).extend((start, start + 1))

    y_values = (0.0, 1.0, 2.0, 3.0)
    z_values = (0.0, 0.5, 1.5, 2.0)

    for region_key, x0, x1 in (
        ('a', 0.0, 2.0),
        ('b', 2.0, 4.0),
    ):
        for y0, y1 in zip(y_values[:-1], y_values[1:], strict=True):
            add_quad(
                f'portal-floor-{region_key}',
                (x0, y0, 0.0),
                (x1, y0, 0.0),
                (x1, y1, 0.0),
                (x0, y1, 0.0),
            )
            add_quad(
                f'portal-ceiling-{region_key}',
                (x0, y0, 2.0),
                (x0, y1, 2.0),
                (x1, y1, 2.0),
                (x1, y0, 2.0),
            )
        for z0, z1 in zip(z_values[:-1], z_values[1:], strict=True):
            add_quad(
                f'portal-front-{region_key}',
                (x0, 0.0, z0),
                (x0, 0.0, z1),
                (x1, 0.0, z1),
                (x1, 0.0, z0),
            )
            add_quad(
                f'portal-rear-{region_key}',
                (x0, 3.0, z0),
                (x1, 3.0, z0),
                (x1, 3.0, z1),
                (x0, 3.0, z1),
            )

    for y0, y1 in zip(y_values[:-1], y_values[1:], strict=True):
        for z0, z1 in zip(z_values[:-1], z_values[1:], strict=True):
            add_quad(
                'portal-left-a',
                (0.0, y0, z0),
                (0.0, y1, z0),
                (0.0, y1, z1),
                (0.0, y0, z1),
            )
            add_quad(
                'portal-right-b',
                (4.0, y0, z0),
                (4.0, y0, z1),
                (4.0, y1, z1),
                (4.0, y1, z0),
            )
            if not (y0 == 1.0 and y1 == 2.0 and z0 == 0.5 and z1 == 1.5):
                add_quad(
                    'portal-interface',
                    (2.0, y0, z0),
                    (2.0, y0, z1),
                    (2.0, y1, z1),
                    (2.0, y1, z0),
                )

    if occluder:
        add_quad(
            'portal-opaque-blocker',
            (2.5, 0.5, 0.5),
            (2.5, 2.5, 0.5),
            (2.5, 2.5, 1.5),
            (2.5, 0.5, 1.5),
        )

    portal_points = (
        (2.0, 1.0, 0.5),
        (2.0, 2.0, 0.5),
        (2.0, 2.0, 1.5),
        (2.0, 1.0, 1.5),
    )
    portal_loop = tuple(vid(point) for point in portal_points)

    obj_lines = [
        *(f'v {x} {y} {z}' for x, y, z in vertices),
        *(
            f'f {a + 1} {b + 1} {c_ + 1}'
            for a, b, c_ in faces
        ),
    ]
    mesh = import_raw_visual_mesh(
        ('\n'.join(obj_lines) + '\n').encode('utf-8'),
        source_name='r150-ga-two-region-portal.obj',
    )
    ids = raw_triangle_ids(mesh)
    assignments = []
    for key, indices in face_indices.items():
        assignments.append(
            SurfaceSemanticAssignment(
                surface_key=key,
                triangle_ids=tuple(ids[index] for index in indices),
                semantic_class=(
                    'object_surface'
                    if key == 'portal-opaque-blocker'
                    else 'room_boundary'
                ),
            )
        )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='two-region Portal fixture uses explicit HTDT metre coordinates',
        ),
        surface_assignments=tuple(assignments),
    )
    return (
        convert_raw_visual_mesh_to_semantic_geometry(mesh, request),
        portal_loop,
    )


def _portal_chain_semantic_geometry(region_count: int):
    if region_count < 2:
        raise ValueError('Portal chain fixture requires at least two regions')

    vertices: list[tuple[float, float, float]] = []
    vertex_index: dict[tuple[float, float, float], int] = {}
    faces: list[tuple[int, int, int]] = []
    face_indices: dict[str, list[int]] = {}

    def vid(point: tuple[float, float, float]) -> int:
        if point not in vertex_index:
            vertex_index[point] = len(vertices)
            vertices.append(point)
        return vertex_index[point]

    def add_quad(
        key: str,
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        c_: tuple[float, float, float],
        d: tuple[float, float, float],
    ) -> None:
        start = len(faces)
        ia, ib, ic, id_ = (vid(point) for point in (a, b, c_, d))
        faces.extend(((ia, ib, ic), (ia, ic, id_)))
        face_indices.setdefault(key, []).extend((start, start + 1))

    y_values = (0.0, 1.0, 2.0, 3.0)
    z_values = (0.0, 0.5, 1.5, 2.0)

    region_surface_keys: dict[str, tuple[str, ...]] = {}
    for index in range(region_count):
        x0 = 2.0 * index
        x1 = 2.0 * (index + 1)
        region_id = f'region-{index}'
        keys = [
            f'chain-floor-{index}',
            f'chain-ceiling-{index}',
            f'chain-front-{index}',
            f'chain-rear-{index}',
        ]
        for y0, y1 in zip(y_values[:-1], y_values[1:], strict=True):
            add_quad(
                keys[0],
                (x0, y0, 0.0),
                (x1, y0, 0.0),
                (x1, y1, 0.0),
                (x0, y1, 0.0),
            )
            add_quad(
                keys[1],
                (x0, y0, 2.0),
                (x0, y1, 2.0),
                (x1, y1, 2.0),
                (x1, y0, 2.0),
            )
        for z0, z1 in zip(z_values[:-1], z_values[1:], strict=True):
            add_quad(
                keys[2],
                (x0, 0.0, z0),
                (x0, 0.0, z1),
                (x1, 0.0, z1),
                (x1, 0.0, z0),
            )
            add_quad(
                keys[3],
                (x0, 3.0, z0),
                (x1, 3.0, z0),
                (x1, 3.0, z1),
                (x0, 3.0, z1),
            )

        left_key = (
            f'chain-left-{index}'
            if index == 0
            else f'chain-interface-{index - 1}-{index}'
        )
        right_key = (
            f'chain-right-{index}'
            if index == region_count - 1
            else f'chain-interface-{index}-{index + 1}'
        )
        keys.extend((left_key, right_key))
        region_surface_keys[region_id] = tuple(keys)

    for boundary_index in range(region_count + 1):
        x = 2.0 * boundary_index
        if boundary_index == 0:
            key = 'chain-left-0'
        elif boundary_index == region_count:
            key = f'chain-right-{region_count - 1}'
        else:
            key = f'chain-interface-{boundary_index - 1}-{boundary_index}'
        for y0, y1 in zip(y_values[:-1], y_values[1:], strict=True):
            for z0, z1 in zip(z_values[:-1], z_values[1:], strict=True):
                if (
                    0 < boundary_index < region_count
                    and y0 == 1.0
                    and y1 == 2.0
                    and z0 == 0.5
                    and z1 == 1.5
                ):
                    continue
                if boundary_index == 0:
                    add_quad(
                        key,
                        (x, y0, z0),
                        (x, y1, z0),
                        (x, y1, z1),
                        (x, y0, z1),
                    )
                else:
                    add_quad(
                        key,
                        (x, y0, z0),
                        (x, y0, z1),
                        (x, y1, z1),
                        (x, y1, z0),
                    )

    portal_specs: list[dict[str, object]] = []
    for index in range(region_count - 1):
        x = 2.0 * (index + 1)
        loop = tuple(
            vid(point)
            for point in (
                (x, 1.0, 0.5),
                (x, 2.0, 0.5),
                (x, 2.0, 1.5),
                (x, 1.0, 1.5),
            )
        )
        portal_specs.append(
            {
                'portal_id': f'fixture-portal-{index}-{index + 1}',
                'loop': loop,
                'surface_key': f'chain-interface-{index}-{index + 1}',
                'region_ids': (f'region-{index}', f'region-{index + 1}'),
                'state': 'open',
                'reverse': False,
            }
        )

    obj_lines = [
        *(f'v {x} {y} {z}' for x, y, z in vertices),
        *(f'f {a + 1} {b + 1} {c_ + 1}' for a, b, c_ in faces),
    ]
    mesh = import_raw_visual_mesh(
        ('\n'.join(obj_lines) + '\n').encode('utf-8'),
        source_name=f'r150-ga-{region_count}-region-portal-chain.obj',
    )
    ids = raw_triangle_ids(mesh)
    assignments = tuple(
        SurfaceSemanticAssignment(
            surface_key=key,
            triangle_ids=tuple(ids[index] for index in indices),
            semantic_class='room_boundary',
        )
        for key, indices in face_indices.items()
    )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='multi-Portal chain fixture uses explicit HTDT metre coordinates',
        ),
        surface_assignments=assignments,
    )
    return (
        convert_raw_visual_mesh_to_semantic_geometry(mesh, request),
        region_surface_keys,
        tuple(portal_specs),
    )


def _portal_chain_fixture(
    tmp_path: Path,
    *,
    region_count: int,
    maximum_portal_crossings: int | None = None,
    receiver_position: Position3 | None = None,
    portal_specs_transform=None,
):
    geometry, region_surfaces, portal_specs = _portal_chain_semantic_geometry(
        region_count
    )
    if portal_specs_transform is not None:
        portal_specs = tuple(portal_specs_transform(portal_specs))
    return _fixture(
        tmp_path,
        semantic_geometry=geometry,
        receiver_position=(
            receiver_position
            if receiver_position is not None
            else Position3(
                x_m=2.0 * region_count - 1.0,
                y_m=2.0,
                z_m=1.0,
            )
        ),
        room_policy=PORTAL_POLICY,
        region_surface_keys_by_id=region_surfaces,
        portal_specs=portal_specs,
        source_region_id='region-0',
        receiver_region_id=f'region-{region_count - 1}',
        maximum_reflection_order=0,
        maximum_portal_crossings=(
            region_count - 1
            if maximum_portal_crossings is None
            else maximum_portal_crossings
        ),
    )

def _directivity_definition(*, narrow: bool):
    horizontal_grid = (-30.0, 0.0, 30.0) if narrow else (-180.0, 0.0, 180.0)
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': 'r150-ga-speaker-dataset',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': 'r150-ga-fixture',
        'source_version': '1',
        'source_reference': 'unit-fixture',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': None,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'r150-ga-fixture-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': list(horizontal_grid),
        'vertical_angles_deg': [-90.0, 0.0, 90.0],
        'samples': [
            {
                'frequency_hz': frequency,
                'horizontal_angle_deg': horizontal_angle,
                'vertical_angle_deg': vertical_angle,
                'magnitude': 0.0,
                'phase_deg': None,
            }
            for frequency in (500.0, 1000.0)
            for horizontal_angle in horizontal_grid
            for vertical_angle in (-90.0, 0.0, 90.0)
        ],
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='r150-ga-fixture',
        source_version='1',
        source_reference='unit-fixture',
        source_sha256=source_hash,
    )
    horizontal = (
        (-30.0, 30.0) if narrow else (-180.0, 180.0)
    )
    domain = DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
        horizontal=AngleDomain(
            minimum_deg=horizontal[0],
            maximum_deg=horizontal[-1],
        ),
        vertical=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='r150-ga-fixture-linear',
        implementation_version='1',
        provenance=provenance,
    )
    definition = build_equipment_definition(
        definition_id='r150-ga-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='R150 GA fixture source',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.0),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=domain,
            interpolation=interpolation,
            coherent_phase=False,
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


class FixtureImageEngine:
    engine_id = 'fixture.image-source'
    engine_version = '1'
    candidate_source_commit = None
    solver_implementation_ref = _ref(
        'fixture:deterministic-image-source',
        'fixture-image-source-implementation',
    )

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del receiver_local_m
        x, y, z = source_local_m
        width, depth, height = dimensions_m
        positions = (
            (x, y, z),
            (-x, y, z),
            (2.0 * width - x, y, z),
            (x, -y, z),
            (x, 2.0 * depth - y, z),
            (x, y, -z),
            (x, y, 2.0 * height - z),
        )
        return tuple(NativeImageSource(position_local_m=item) for item in positions)


def _fixture(
    tmp_path: Path,
    *,
    occluder: bool = False,
    narrow_directivity: bool = False,
    supported_material: bool = True,
    material: GeometricMaterialAuthority | None = None,
    use_pyroomacoustics: bool = False,
    receiver_position: Position3 | None = None,
    semantic_geometry=None,
    room_surface_keys: tuple[str, ...] | None = None,
    room_policy: str = 'exact_axis_aligned_closed_shoebox_v1',
    maximum_reflection_order: int = 1,
    multi_region: bool = False,
    explicit_portal: bool = False,
    region_surface_keys_by_id: dict[str, tuple[str, ...]] | None = None,
    portal_loop_vertex_indices: tuple[int, ...] | None = None,
    portal_surface_key: str | None = None,
    portal_region_ids: tuple[str, str] | None = None,
    portal_specs: tuple[dict[str, object], ...] | None = None,
    portal_state: str = 'open',
    reverse_portal_orientation: bool = False,
    source_region_id: str | None = None,
    receiver_region_id: str | None = None,
    boundary_termination_authority: BoundaryTerminationAuthority | None = None,
    nontrivial_boundary_termination: bool = False,
    maximum_portal_crossings: int | None = None,
    expected_dispatch_state: str = 'READY',
):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='r150-ga-fixture',
        schema_version=4,
        room=None,
        r120_semantic_geometry=(
            _semantic_geometry(occluder=occluder)
            if semantic_geometry is None
            else semantic_geometry
        ),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='receiver-mlp',
                kind='measurement_point',
                name='MLP',
                position=(
                    Position3(x_m=3.0, y_m=2.0, z_m=1.0)
                    if receiver_position is None
                    else receiver_position
                ),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision

    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    r110_repository = CadR110SourceRepository(
        scene_repository,
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        directivity_repository=directivity_repository,
    )
    r120_repository = R120GeometryCompilerRepository(scene_repository)

    source_bytes, definition, dataset = _directivity_definition(
        narrow=narrow_directivity,
    )
    _save_equipment(equipment_repository, definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename='r150-ga-speaker.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )
    variant = build_system_variant(
        baseline=revision,
        name='R150 GA fixture current',
        role_bindings=(ChannelRoleBinding(role_id='FL', display_name='Front left'),),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    source = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
        directivity_dataset=dataset,
    )
    r110_repository.save_model(source)

    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    surface_by_key = {item.surface_key: item.surface_id for item in geometry.surfaces}
    selected_room_keys = (
        room_surface_keys
        or (
            next(iter(region_surface_keys_by_id.values()))
            if region_surface_keys_by_id is not None
            else (
                'floor-z-min',
                'ceiling-z-max',
                'front-y-min',
                'rear-y-max',
                'left-x-min',
                'right-x-max',
            )
        )
    )
    room_surface_ids = tuple(surface_by_key[key] for key in selected_room_keys)
    if region_surface_keys_by_id is not None:
        declarations = [
            AcousticRegionDeclaration(
                region_id=region_id,
                boundary_surface_ids=tuple(
                    surface_by_key[key] for key in surface_keys
                ),
            )
            for region_id, surface_keys in region_surface_keys_by_id.items()
        ]
    else:
        declarations = [
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=room_surface_ids,
            )
        ]
        if multi_region:
            declarations.append(
                AcousticRegionDeclaration(
                    region_id='room-air-secondary',
                    boundary_surface_ids=(room_surface_ids[0],),
                )
            )
    region = make_acoustic_region_authority(tuple(declarations))
    if portal_specs is not None:
        declarations = []
        for spec in portal_specs:
            loop_value = tuple(int(item) for item in spec['loop'])
            if bool(spec.get('reverse', False)):
                loop_value = tuple(reversed(loop_value))
            surface_key = str(spec['surface_key'])
            region_ids = tuple(str(item) for item in spec['region_ids'])
            declarations.append(
                PortalDeclaration(
                    portal_id=str(spec['portal_id']),
                    region_ids=region_ids,
                    boundary_edges=tuple(
                        PortalBoundaryEdge(
                            source_surface_id=surface_by_key[surface_key],
                            vertex_a=loop_value[index],
                            vertex_b=loop_value[(index + 1) % len(loop_value)],
                        )
                        for index in range(len(loop_value))
                    ),
                    state=str(spec.get('state', 'open')),
                    region_side_semantics=PORTAL_SIDE_SEMANTICS,
                )
            )
        portals = make_portal_authority(
            declaration_mode='explicit_list',
            declarations=tuple(declarations),
        )
    elif portal_loop_vertex_indices is not None:
        if portal_surface_key is None or portal_region_ids is None:
            raise ValueError('Portal fixture requires surface key and region ids')
        loop = (
            tuple(reversed(portal_loop_vertex_indices))
            if reverse_portal_orientation
            else portal_loop_vertex_indices
        )
        portals = make_portal_authority(
            declaration_mode='explicit_list',
            declarations=(
                PortalDeclaration(
                    portal_id='fixture-portal',
                    region_ids=portal_region_ids,
                    boundary_edges=tuple(
                        PortalBoundaryEdge(
                            source_surface_id=surface_by_key[portal_surface_key],
                            vertex_a=loop[index],
                            vertex_b=loop[(index + 1) % len(loop)],
                        )
                        for index in range(len(loop))
                    ),
                    state=portal_state,
                    region_side_semantics=PORTAL_SIDE_SEMANTICS,
                ),
            ),
        )
    elif explicit_portal:
        portals = make_portal_authority(
            declaration_mode='explicit_list',
            declarations=(
                PortalDeclaration(
                    portal_id='fixture-portal',
                    region_ids=('room-air',),
                    boundary_edges=(
                        PortalBoundaryEdge(
                            source_surface_id=room_surface_ids[2],
                            vertex_a=0,
                            vertex_b=1,
                        ),
                    ),
                ),
            ),
        )
    else:
        portals = make_portal_authority(declaration_mode='explicit_none')
    if boundary_termination_authority is not None:
        terminations = boundary_termination_authority
    elif nontrivial_boundary_termination:
        if portal_loop_vertex_indices is None or portal_surface_key is None:
            raise ValueError('nontrivial termination fixture requires Portal geometry')
        terminations = make_boundary_termination_authority(
            declaration_mode='explicit_list',
            declarations=(
                BoundaryTerminationDeclaration(
                    termination_id='fixture-termination',
                    boundary_edges=(
                        PortalBoundaryEdge(
                            source_surface_id=surface_by_key[portal_surface_key],
                            vertex_a=portal_loop_vertex_indices[0],
                            vertex_b=portal_loop_vertex_indices[1],
                        ),
                    ),
                    external_authority=_ref(
                        'fixture-boundary-termination',
                        'boundary-termination',
                    ),
                ),
            ),
        )
    else:
        terminations = make_boundary_termination_authority(
            declaration_mode='explicit_none'
        )
    if material is None:
        material = _material(supported=supported_material)
    boundary_ref = _ref('fixture-boundary-physics', 'boundary')
    bindings = tuple(
        SurfaceBoundaryAuthorityBinding(
            source_surface_id=item.surface_id,
            material_authority=material.authority_ref,
            boundary_physics_authority=boundary_ref,
        )
        for item in geometry.surfaces
    )
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-9,
            input_policy=(
                'diagnostic_compile_unresolved'
                if room_policy == PORTAL_POLICY
                else 'require_contract_ready'
            ),
        ),
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    r120_repository.save_compiled_geometry(
        compiled,
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )

    topology_preflight_ref = None
    preflight_graph = None
    if (
        room_policy == PORTAL_POLICY
        and portal_specs is not None
        and maximum_portal_crossings is not None
        and not compiled.readiness.geometric_acoustics_geometry_ready
    ):
        _, preflight_graph = compile_portal_graph(
            compiled_geometry=compiled,
            region_authority=region,
            portal_authority=portals,
            tolerance_m=1.0e-9,
            maximum_portal_crossings=maximum_portal_crossings,
        )
        topology_preflight_ref = preflight_graph.as_external_ref()

    receiver = receiver_binding_from_scene(
        scene_revision=revision,
        system_variant=variant,
        entity_id='receiver-mlp',
        requested_output_capabilities=('deterministic_paths',),
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref('fixture-environment', 'environment'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref(
            'fixture-sound-speed',
            'sound-speed',
        ),
    )
    domain = FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0)
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=revision,
        system_variant=variant,
        compiled_geometry=compiled,
        source_models=(source,),
        receivers=(receiver,),
        requested_frequency_domain=domain,
        requested_observables=('deterministic_paths',),
        environment=environment,
        valid_frequency_domain=domain,
        valid_frequency_domain_authority_ref=_ref(
            'fixture-valid-domain',
            'valid-domain',
        ),
        geometric_acoustics_topology_preflight_ref=topology_preflight_ref,
    )
    fidelity_policy = AcousticNumericalFidelityPolicy(
        authority_ref=_ref(
            'fixture-fidelity-policy',
            'fidelity',
        ),
        acoustic_domain='geometric',
        model_solver_role_ids=('r150-deterministic-ga',),
        supported_observables=('deterministic_paths',),
        valid_frequency_domain=domain,
        parameter_bounds={},
    )

    def fidelity_policy_resolver(ref: ExactExternalAuthorityRef):
        return (
            fidelity_policy if fidelity_policy.authority_ref == ref else None
        )

    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='r150-deterministic-ga',
        requested_frequency_domain=domain,
        requested_observables=('deterministic_paths',),
        numerical_fidelity_policy_ref=fidelity_policy.authority_ref,
    )
    configuration = build_deterministic_ga_configuration(
        frequency_centers_hz=(500.0, 1000.0),
        geometric_tolerance_m=1.0e-9,
        engine_image_match_tolerance_m=1.0e-8,
        room_policy=room_policy,
        maximum_reflection_order=maximum_reflection_order,
        maximum_portal_crossings=maximum_portal_crossings,
    )
    implementation_ref = (
        PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF
        if use_pyroomacoustics
        else (
            (
                HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF
                if maximum_reflection_order == 1
                else HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF
            )
            if room_policy == 'general_planar_multi_region_portal_v1'
            else (
                (
                    HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF
                    if maximum_reflection_order == 2
                    else HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF
                )
                if room_policy == 'general_planar_closed_polyhedral_v1'
                else FixtureImageEngine.solver_implementation_ref
            )
        )
    )
    configuration_schema_ref = _ref(
        'htdt.r150.deterministic-ga-configuration.schema',
        'config-schema',
    )
    descriptor = build_acoustic_solver_adapter_descriptor(
        adapter_id=DETERMINISTIC_GA_ADAPTER_ID,
        adapter_version=DETERMINISTIC_GA_ADAPTER_VERSION,
        model_solver_role_id='r150-deterministic-ga',
        acoustic_domain='geometric',
        solver_implementation_ref=implementation_ref,
        solver_configuration_schema_ref=configuration_schema_ref,
        supported_snapshot_schema_versions=(1, 2, 3),
        supported_observables=('deterministic_paths',),
        valid_frequency_domain=domain,
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=descriptor,
        solver_configuration_ref=configuration.as_external_ref(),
        numerical_fidelity_policy=fidelity_policy,
    )
    assert dispatch.state == expected_dispatch_state, {
        'dispatch_reasons': dispatch.reasons,
        'compiled_unresolved': compiled.unresolved_conditions,
        'compiled_readiness': compiled.readiness.model_dump(mode='json'),
        'compiler_warnings': compiled.compiler_warnings,
        'closed_shell': compiled.closed_shell_diagnostics.model_dump(mode='json'),
    }
    if expected_dispatch_state != 'READY':
        return {
            'dispatch': dispatch,
            'compiled': compiled,
            'region': region,
            'portals': portals,
            'terminations': terminations,
        }

    snapshot_authority_resolvers = _snapshot_authority_resolvers(
        snapshot,
        preflight_graph=preflight_graph,
    )
    snapshot_repository = CadAcousticSnapshotRepository(
        scene_repository,
        variant_repository=variant_repository,
        r110_repository=r110_repository,
        r120_repository=r120_repository,
        fidelity_policy_resolver=fidelity_policy_resolver,
        authority_resolvers=snapshot_authority_resolvers,
    )
    snapshot_repository.save_snapshot(snapshot)
    snapshot_repository.save_prediction_request(request)

    static_external = {
        tuple(
            (
                ref.authority_id,
                ref.authority_version,
                ref.semantic_hash_sha256,
            )
        ): ref
        for ref in (
            implementation_ref,
            configuration_schema_ref,
            configuration.as_external_ref(),
        )
    }

    def external_resolver(ref: ExactExternalAuthorityRef):
        return static_external.get(
            (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)
        )

    dispatch_repository = CadAcousticSolverDispatchRepository(
        scene_repository,
        snapshot_repository=snapshot_repository,
        external_authority_resolver=external_resolver,
        fidelity_policy_resolver=fidelity_policy_resolver,
    )
    dispatch_repository.save_descriptor(descriptor)
    dispatch_repository.save_dispatch(dispatch)

    geometry_authorities = {
        item.authority_id: item
        for item in (region, portals, terminations)
    }
    material_box = {'value': material}

    def geometry_resolver(ref: ExactExternalAuthorityRef):
        item = geometry_authorities.get(ref.authority_id)
        if item is None:
            return None
        actual = ExactExternalAuthorityRef(
            authority_id=item.authority_id,
            authority_version=item.authority_version,
            semantic_hash_sha256=item.semantic_hash_sha256,
        )
        return item if actual == ref else None

    def material_resolver(ref: ExactExternalAuthorityRef):
        item = material_box['value']
        return (
            item
            if item is not None and item.authority_ref == ref
            else None
        )

    def configuration_resolver(ref: ExactExternalAuthorityRef):
        return configuration if configuration.as_external_ref() == ref else None

    execution_input = compile_deterministic_ga_execution_input(
        snapshot=snapshot,
        request=request,
        dispatch=dispatch,
        descriptor=descriptor,
        compiled_geometry=compiled,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
        directivity_datasets=(dataset,),
        configuration=configuration,
        source_region_bindings=(
            {'speaker-fl': source_region_id}
            if source_region_id is not None
            else None
        ),
        receiver_region_bindings=(
            {receiver.receiver_id: receiver_region_id}
            if receiver_region_id is not None
            else None
        ),
    )

    return {
        'scene_repository': scene_repository,
        'snapshot_repository': snapshot_repository,
        'dispatch_repository': dispatch_repository,
        'snapshot': snapshot,
        'request': request,
        'descriptor': descriptor,
        'dispatch': dispatch,
        'compiled': compiled,
        'region': region,
        'portals': portals,
        'terminations': terminations,
        'dataset': dataset,
        'material': material,
        'material_box': material_box,
        'material_resolver': material_resolver,
        'configuration': configuration,
        'configuration_resolver': configuration_resolver,
        'geometry_resolver': geometry_resolver,
        'geometry_authorities': geometry_authorities,
        'external_resolver': external_resolver,
        'snapshot_authority_resolvers': snapshot_authority_resolvers,
        'fidelity_policy_resolver': fidelity_policy_resolver,
        'fidelity_policy': fidelity_policy,
        'execution_input': execution_input,
        'surface_by_key': surface_by_key,
    }


def _execute(fx, engine=None):
    if engine is None:
        engine = (
            (
                HtdtPortalFirstOrderReflectionEngine()
                if fx['execution_input'].maximum_reflection_order == 1
                else (
                    HtdtPortalGraphDirectEngine()
                    if fx['execution_input'].portal_graph is not None
                    else HtdtPortalDirectEngine()
                )
            )
            if fx['execution_input'].geometry_policy
            == 'general_planar_multi_region_portal_v1'
            else (
                HtdtPlanarImageSourceEngine(
                    maximum_reflection_order=(
                        fx['execution_input'].maximum_reflection_order or 1
                    )
                )
                if fx['execution_input'].geometry_policy
                == 'general_planar_closed_polyhedral_v1'
                else FixtureImageEngine()
            )
        )
    return execute_deterministic_ga(
        execution_input=fx['execution_input'],
        compiled_geometry=fx['compiled'],
        directivity_datasets=(fx['dataset'],),
        material_resolver=fx['material_resolver'],
        engine=engine,
    )



GENERAL_ROOM_KEYS = (
    'floor-z-min',
    'ceiling-z-max',
    'front-y-min',
    'rear-y-max',
    'left-x-min',
    'right-slanted',
)
GENERAL_POLICY = 'general_planar_closed_polyhedral_v1'


def test_general_planar_second_order_parallel_walls_match_analytic_geometry_and_identity(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_semantic_geometry(occluder=False),
        room_policy=GENERAL_POLICY,
        maximum_reflection_order=2,
    )
    artifact = _execute(fx)

    left_id = fx['surface_by_key']['left-x-min']
    right_id = fx['surface_by_key']['right-x-max']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (left_id, right_id)
    )
    first_point, second_point = reflected.ordered_interaction_points
    assert isclose(first_point.x_m, 0.0, abs_tol=1.0e-9)
    assert isclose(first_point.y_m, 7.0 / 6.0, abs_tol=1.0e-9)
    assert isclose(first_point.z_m, 1.0, abs_tol=1.0e-9)
    assert isclose(second_point.x_m, 4.0, abs_tol=1.0e-9)
    assert isclose(second_point.y_m, 11.0 / 6.0, abs_tol=1.0e-9)
    assert isclose(second_point.z_m, 1.0, abs_tol=1.0e-9)

    expected_length = sqrt(37.0)
    assert isclose(
        reflected.geometric_path_length_m,
        expected_length,
        abs_tol=1.0e-9,
    )
    assert isclose(
        reflected.propagation_delay_s,
        expected_length / 343.0,
        abs_tol=1.0e-12,
    )
    assert reflected.ordered_interaction_surface_ids == (left_id, right_id)
    assert artifact.path_scope == 'direct_through_second_order_specular'

    band_500 = next(item for item in reflected.bands if item.center_hz == 500.0)
    assert band_500.boundary_material is None
    assert band_500.boundary_materials is not None
    assert tuple(
        item.source_surface_id for item in band_500.boundary_materials
    ) == (left_id, right_id)
    assert all(
        isclose(item.specular_energy_factor, 0.72, abs_tol=1.0e-12)
        for item in band_500.boundary_materials
    )
    assert isclose(
        band_500.relative_energy_transport_per_m2,
        (1.0 / 37.0)
        * band_500.source_directivity.energy_factor
        * 0.72
        * 0.72,
        abs_tol=1.0e-12,
    )
    assert band_500.coherent_phase == 'UNAVAILABLE_NOT_SYNTHESIZED'
    assert artifact.coherent_phase_authority == 'UNAVAILABLE_NOT_SYNTHESIZED'

    reversed_path = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (right_id, left_id)
    )
    assert reversed_path.path_id != reflected.path_id
    reversed_first, reversed_second = reversed_path.ordered_interaction_points
    assert isclose(reversed_first.x_m, 4.0, abs_tol=1.0e-9)
    assert isclose(reversed_first.y_m, 13.0 / 10.0, abs_tol=1.0e-9)
    assert isclose(reversed_second.x_m, 0.0, abs_tol=1.0e-9)
    assert isclose(reversed_second.y_m, 17.0 / 10.0, abs_tol=1.0e-9)
    assert isclose(
        reversed_path.geometric_path_length_m,
        sqrt(101.0),
        abs_tol=1.0e-9,
    )

    same_surface = next(
        item
        for item in artifact.rejected_candidates
        if item.interaction_surface_ids == (left_id, left_id)
    )
    assert same_surface.decision == 'UNSUPPORTED_GEOMETRY'
    assert 'same-surface immediate repeat' in same_surface.reason

    rerun = _execute(fx)
    assert rerun == artifact
    order_keys = [
        (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.ordered_interaction_surface_ids),
            item.ordered_interaction_surface_ids,
            item.path_id,
        )
        for item in artifact.paths
    ]
    assert order_keys == sorted(order_keys)

    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)
    assert repository.get_execution_input(fx['execution_input'].execution_input_id) == (
        fx['execution_input']
    )
    assert repository.get(artifact.artifact_id) == artifact


def test_general_planar_second_order_second_point_outside_finite_surface_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(
            TRAPEZOID_WITH_SMALL_CUBE_OBJ,
            object_mode='cube-faces',
        ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        maximum_reflection_order=2,
        receiver_position=Position3(x_m=3.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)

    first_id = fx['surface_by_key']['right-slanted']
    second_id = fx['surface_by_key']['cube-x-max']
    assert not any(
        item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (first_id, second_id)
        for item in artifact.paths
    )
    rejected = next(
        item
        for item in artifact.rejected_candidates
        if item.interaction_surface_ids == (first_id, second_id)
        and 'second second-order reflection point lies outside' in item.reason
    )
    assert rejected.decision == 'UNSUPPORTED_GEOMETRY'
    assert 'exact semantic R120 surface triangle extent' in rejected.reason


def test_general_planar_second_order_intermediate_segment_occlusion_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_semantic_geometry(occluder=True),
        room_policy=GENERAL_POLICY,
        maximum_reflection_order=2,
    )
    artifact = _execute(fx)

    left_id = fx['surface_by_key']['left-x-min']
    right_id = fx['surface_by_key']['right-x-max']
    assert not any(
        item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (left_id, right_id)
        for item in artifact.paths
    )
    rejected = [
        item
        for item in artifact.rejected_candidates
        if item.interaction_surface_ids == (left_id, right_id)
    ]
    assert any(item.decision == 'BLOCKED_VISIBILITY' for item in rejected)
    assert any('one of three' in item.reason for item in rejected)


def test_general_planar_slanted_wall_reflection_matches_analytic_geometry(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(TRAPEZOID_OBJ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        receiver_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)

    surface_id = fx['surface_by_key']['right-slanted']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (surface_id,)
    )
    point = reflected.ordered_interaction_points[0]
    assert isclose(point.x_m, 49.0 / 15.0, abs_tol=1.0e-9)
    assert isclose(point.y_m, 11.0 / 5.0, abs_tol=1.0e-9)
    assert isclose(point.z_m, 1.0, abs_tol=1.0e-9)
    expected_length = sqrt(74.0 / 5.0)
    assert isclose(reflected.geometric_path_length_m, expected_length, abs_tol=1.0e-9)
    assert isclose(
        reflected.propagation_delay_s,
        expected_length / 343.0,
        abs_tol=1.0e-12,
    )
    assert reflected.ordered_interaction_surface_ids == (surface_id,)


def test_general_planar_reflection_outside_exact_surface_polygon_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(
            TRAPEZOID_WITH_SMALL_CUBE_OBJ,
            object_mode='cube-faces',
        ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        receiver_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)

    surface_id = fx['surface_by_key']['cube-x-max']
    assert not any(
        item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (surface_id,)
        for item in artifact.paths
    )
    rejected = next(
        item
        for item in artifact.rejected_candidates
        if item.interaction_surface_ids == (surface_id,)
    )
    assert rejected.decision == 'UNSUPPORTED_GEOMETRY'
    assert 'exact semantic R120 surface triangle extent' in rejected.reason


def test_general_planar_reflection_blocked_by_exact_triangle_visibility_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(
            TRAPEZOID_WITH_REFLECTION_BLOCKER_OBJ,
            object_mode='grouped-tetra',
        ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        receiver_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)

    surface_id = fx['surface_by_key']['right-slanted']
    rejected = [
        item
        for item in artifact.rejected_candidates
        if item.interaction_surface_ids == (surface_id,)
    ]
    assert any(item.decision == 'BLOCKED_VISIBILITY' for item in rejected)
    assert not any(
        item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (surface_id,)
        for item in artifact.paths
    )


def test_general_planar_nonplanar_semantic_surface_is_not_silently_planarized(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(
            TRAPEZOID_WITH_REFLECTION_BLOCKER_OBJ,
            object_mode='grouped-tetra',
        ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        receiver_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)
    blocker_id = fx['surface_by_key']['internal-blocker']
    assert blocker_id in (fx['execution_input'].unsupported_reflection_surface_ids or ())
    assert any(
        item.interaction_surface_ids == (blocker_id,)
        and item.decision == 'UNSUPPORTED_GEOMETRY'
        for item in artifact.rejected_candidates
    )


def test_general_planar_topology_gates_have_typed_unsupported_reasons(
    tmp_path: Path,
) -> None:
    geometry = _general_semantic_geometry(TRAPEZOID_OBJ)
    with pytest.raises(DeterministicGaUnsupportedError) as multi_region_error:
        _fixture(
            tmp_path / 'multi-region',
            semantic_geometry=geometry,
            room_surface_keys=GENERAL_ROOM_KEYS,
            room_policy=GENERAL_POLICY,
            multi_region=True,
        )
    assert multi_region_error.value.reason_code == 'UNSUPPORTED_REGION_TOPOLOGY'

    with pytest.raises(DeterministicGaUnsupportedError) as portal_error:
        _fixture(
            tmp_path / 'portal',
            semantic_geometry=geometry,
            room_surface_keys=GENERAL_ROOM_KEYS,
            room_policy=GENERAL_POLICY,
            explicit_portal=True,
        )
    assert portal_error.value.reason_code == 'UNSUPPORTED_PORTAL_TOPOLOGY'


def test_legacy_execution_input_payload_loads_without_general_planar_fields(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    payload = fx['execution_input'].model_dump(mode='json')
    payload.pop('geometry_policy')
    payload.pop('unsupported_reflection_surface_ids')
    payload.pop('maximum_reflection_order')
    for plane in payload['boundary_planes']:
        plane.pop('point_m')
        plane.pop('normal')
        plane.pop('compiled_triangle_indices')
    reopened = DeterministicGaExecutionInput.model_validate(payload)
    assert reopened == fx['execution_input']


def test_general_planar_execution_input_and_artifact_save_reopen_exact_identity(
    tmp_path: Path,
) -> None:
    fx = _fixture(
        tmp_path,
        semantic_geometry=_general_semantic_geometry(TRAPEZOID_OBJ),
        room_surface_keys=GENERAL_ROOM_KEYS,
        room_policy=GENERAL_POLICY,
        receiver_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0),
    )
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)
    assert repository.get_execution_input(fx['execution_input'].execution_input_id) == (
        fx['execution_input']
    )
    assert repository.get(artifact.artifact_id) == artifact


def test_first_order_reflection_incidence_is_evaluated_and_persisted(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute(fx)

    front_id = fx['surface_by_key']['front-y-min']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    # Source (1,1,1) -> reflection point (5/3,0,1): incoming segment
    # (2/3,-1,0) hits the y=0 plane, so the incidence cosine against the
    # surface normal is 1/|(2/3,-1,0)| = 3/sqrt(13).
    expected_cosine = 3.0 / sqrt(13.0)
    expected_angle = degrees(acos(expected_cosine))
    contribution = reflected.bands[0].boundary_material
    assert contribution is not None
    assert contribution.incidence_cosine == pytest.approx(expected_cosine)
    assert contribution.incidence_angle_deg == pytest.approx(expected_angle)
    assert contribution.coefficient_incidence_condition == 'unknown_incidence'
    assert contribution.incidence_evaluation == 'scalar_coefficient_all_angles_v1'
    assert reflected.ordered_interactions is not None
    interaction = reflected.ordered_interactions[0]
    assert interaction.kind == 'reflection'
    assert interaction.incidence_cosine == pytest.approx(expected_cosine)
    assert interaction.incidence_angle_deg == pytest.approx(expected_angle)


def test_declared_incidence_conditions_classify_evaluation_exactness(
    tmp_path: Path,
) -> None:
    expected_angle = degrees(acos(3.0 / sqrt(13.0)))

    def material_with(condition: str, angle_deg: float | None):
        material = AcousticMaterial(
            material_id='fixture-wall-incidence',
            provenance='fixture exact GA material with incidence evidence',
            version='1',
            wave_model='unsupported',
            geometric_model='banded',
            geometric_bands=(
                GeometricAcousticBand(
                    center_hz=500.0,
                    absorption=0.2,
                    scattering=0.1,
                    incidence_condition=condition,
                    incidence_angle_deg=angle_deg,
                ),
                GeometricAcousticBand(
                    center_hz=1000.0,
                    absorption=0.3,
                    scattering=0.2,
                    incidence_condition=condition,
                    incidence_angle_deg=angle_deg,
                ),
            ),
        )
        payload = material.model_dump(mode='json')
        return GeometricMaterialAuthority(
            authority_ref=ExactExternalAuthorityRef(
                authority_id='fixture-material:incidence',
                authority_version=material.version,
                semantic_hash_sha256=_digest(payload),
            ),
            material=material,
        )

    # angle-specific evidence evaluated at its declared angle is exact; the
    # same band applied at other surfaces' angles is only the versioned
    # all-angle scalar policy.
    fx = _fixture(
        tmp_path / 'angle-specific',
        material=material_with('angle_specific', expected_angle),
    )
    front_id = fx['surface_by_key']['front-y-min']
    artifact = _execute(fx)
    front_path = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    contribution = front_path.bands[0].boundary_material
    assert contribution is not None
    assert contribution.incidence_evaluation == 'angle_specific_exact'
    assert contribution.coefficient_incidence_condition == 'angle_specific'
    other = [
        item.bands[0].boundary_material
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids != (front_id,)
        and item.bands[0].boundary_material is not None
        and item.bands[0].boundary_material.incidence_evaluation
        != 'angle_specific_exact'
    ]
    assert other
    assert all(
        item.incidence_evaluation == 'scalar_coefficient_all_angles_v1'
        for item in other
    )

    # normal-incidence evidence at an oblique reflection is never silently
    # exact: it is applied only under the versioned all-angle scalar policy.
    artifact = _execute(
        _fixture(
            tmp_path / 'normal',
            material=material_with('normal_incidence', None),
        )
    )
    contributions = [
        item.bands[0].boundary_material
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.bands[0].boundary_material is not None
        and item.bands[0].boundary_material.incidence_cosine < 1.0
    ]
    assert contributions
    assert all(
        item.incidence_evaluation == 'scalar_coefficient_all_angles_v1'
        for item in contributions
    )


def test_direct_and_first_reflection_match_analytic_geometry(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute(fx)

    direct = next(item for item in artifact.paths if item.path_type == 'direct')
    assert isclose(direct.geometric_path_length_m, sqrt(5.0), abs_tol=1.0e-9)
    assert isclose(
        direct.propagation_delay_s,
        sqrt(5.0) / 343.0,
        abs_tol=1.0e-12,
    )

    front_id = fx['surface_by_key']['front-y-min']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    assert isclose(reflected.geometric_path_length_m, sqrt(13.0), abs_tol=1.0e-9)
    point = reflected.ordered_interaction_points[0]
    assert isclose(point.x_m, 5.0 / 3.0, abs_tol=1.0e-9)
    assert isclose(point.y_m, 0.0, abs_tol=1.0e-9)
    assert isclose(point.z_m, 1.0, abs_tol=1.0e-9)
    assert reflected.bands[0].boundary_material is not None
    assert reflected.bands[0].coherent_phase == 'UNAVAILABLE_NOT_SYNTHESIZED'
    assert artifact.coherent_phase_authority == 'UNAVAILABLE_NOT_SYNTHESIZED'


def test_blocked_direct_path_is_not_retained_and_object_reflection_is_not_invented(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, occluder=True)
    artifact = _execute(fx)

    assert not any(item.path_type == 'direct' for item in artifact.paths)
    assert any(
        item.path_type == 'direct' and item.decision == 'BLOCKED_VISIBILITY'
        for item in artifact.rejected_candidates
    )
    object_id = fx['surface_by_key']['internal-occluder']
    assert any(
        item.interaction_surface_ids == (object_id,)
        and item.decision == 'UNSUPPORTED_GEOMETRY'
        for item in artifact.rejected_candidates
    )


def test_receiver_outside_explicit_region_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match='receiver receiver-mlp position is not strictly inside',
    ):
        _fixture(
            tmp_path,
            receiver_position=Position3(x_m=4.5, y_m=2.0, z_m=1.0),
        )


def test_same_exact_input_has_same_path_identity_and_order(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    first = _execute(fx)
    second = _execute(fx)

    assert second == first
    assert second.artifact_id == first.artifact_id
    assert [item.path_id for item in second.paths] == [
        item.path_id for item in first.paths
    ]


def test_engine_must_match_ready_dispatch_solver_implementation(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)

    class WrongImplementationEngine(FixtureImageEngine):
        solver_implementation_ref = _ref(
            'fixture:wrong-image-source',
            'wrong-image-source-implementation',
        )

    with pytest.raises(ValueError, match='solver implementation authority'):
        _execute(fx, engine=WrongImplementationEngine())


def test_unsupported_directivity_angle_is_not_filled_as_omnidirectional(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, narrow_directivity=True)
    artifact = _execute(fx)

    assert not any(item.path_type == 'direct' for item in artifact.paths)
    assert any(
        item.path_type == 'direct'
        and item.decision == 'UNSUPPORTED_DIRECTIVITY'
        for item in artifact.rejected_candidates
    )


def test_missing_geometric_boundary_quantity_does_not_fabricate_reflection(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, supported_material=False)
    artifact = _execute(fx)

    assert any(item.path_type == 'direct' for item in artifact.paths)
    assert not any(
        item.path_type == 'specular_reflection' for item in artifact.paths
    )
    assert any(
        item.decision == 'UNSUPPORTED_BOUNDARY_QUANTITY'
        for item in artifact.rejected_candidates
    )


def test_execution_input_artifact_and_result_save_reopen_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    artifact = _execute(fx)

    path_repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    path_repository.save_execution_input(fx['execution_input'])
    path_repository.save(artifact)

    reopened = CadDeterministicPathArtifactRepository(
        SceneRepository(fx['scene_repository'].path),
        snapshot_repository=CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        dispatch_repository=CadAcousticSolverDispatchRepository(
            SceneRepository(fx['scene_repository'].path),
            external_authority_resolver=fx['external_resolver'],
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            snapshot_authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    assert reopened.get_execution_input(fx['execution_input'].execution_input_id) == (
        fx['execution_input']
    )
    assert reopened.get(artifact.artifact_id) == artifact

    result = build_deterministic_ga_result_envelope(
        artifact=artifact,
        dispatch=fx['dispatch'],
        request=fx['request'],
        completed_at_utc='2026-09-20T01:00:00+00:00',
    )

    def result_external_resolver(ref: ExactExternalAuthorityRef):
        from_path = reopened.resolve_external_authority(ref)
        if from_path is not None:
            return from_path
        return fx['external_resolver'](ref)

    result_repository = CadAcousticSolverResultRepository(
        SceneRepository(fx['scene_repository'].path),
        dispatch_resolver=reopened.dispatch_repository,
        request_resolver=reopened.snapshot_repository,
        external_authority_resolver=result_external_resolver,
        artifact_manifest_resolver=reopened.resolve_artifact_manifest,
    )
    result_repository.save(result)
    reopened_result_repository = CadAcousticSolverResultRepository(
        SceneRepository(fx['scene_repository'].path),
        dispatch_resolver=reopened.dispatch_repository,
        request_resolver=reopened.snapshot_repository,
        external_authority_resolver=result_external_resolver,
        artifact_manifest_resolver=reopened.resolve_artifact_manifest,
    )
    assert reopened_result_repository.get(result.result_id) == result

    fx['material_box']['value'] = None
    with pytest.raises(ValueError, match='boundary material exact authority'):
        reopened.get(artifact.artifact_id)


def test_actual_pyroomacoustics_candidate_executes_same_direct_first_reflection_fixture(
    tmp_path: Path,
) -> None:
    pytest.importorskip('pyroomacoustics')
    fx = _fixture(tmp_path, use_pyroomacoustics=True)
    artifact = _execute(fx, engine=PyroomacousticsImageSourceEngine())

    assert any(item.path_type == 'direct' for item in artifact.paths)
    front_id = fx['surface_by_key']['front-y-min']
    reflected = next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (front_id,)
    )
    assert isclose(reflected.geometric_path_length_m, sqrt(13.0), abs_tol=1.0e-8)


def _portal_fixture(
    tmp_path: Path,
    *,
    portal_state: str = 'open',
    reverse_portal_orientation: bool = False,
    portal_region_ids: tuple[str, str] = ('region-a', 'region-b'),
    source_region_id: str = 'region-a',
    receiver_region_id: str = 'region-b',
    receiver_position: Position3 | None = None,
    occluder: bool = False,
    nontrivial_boundary_termination: bool = False,
    maximum_reflection_order: int = 0,
    expected_dispatch_state: str = 'READY',
):
    geometry, portal_loop = _portal_semantic_geometry(occluder=occluder)
    return _fixture(
        tmp_path,
        semantic_geometry=geometry,
        receiver_position=receiver_position,
        room_policy=PORTAL_POLICY,
        region_surface_keys_by_id=PORTAL_REGION_SURFACES,
        portal_loop_vertex_indices=portal_loop,
        portal_surface_key='portal-interface',
        portal_region_ids=portal_region_ids,
        portal_state=portal_state,
        reverse_portal_orientation=reverse_portal_orientation,
        source_region_id=source_region_id,
        receiver_region_id=receiver_region_id,
        nontrivial_boundary_termination=nontrivial_boundary_termination,
        maximum_reflection_order=maximum_reflection_order,
        maximum_portal_crossings=1,
        expected_dispatch_state=expected_dispatch_state,
    )


def test_portal_reflection_configuration_is_bounded_to_exact_one_crossing() -> None:
    configuration = build_deterministic_ga_configuration(
        frequency_centers_hz=(500.0, 1000.0),
        room_policy=PORTAL_POLICY,
        maximum_reflection_order=1,
        maximum_portal_crossings=1,
    )
    assert configuration.maximum_reflection_order == 1
    assert configuration.maximum_portal_crossings == 1

    with pytest.raises(ValueError, match='exactly one Portal crossing'):
        build_deterministic_ga_configuration(
            frequency_centers_hz=(500.0, 1000.0),
            room_policy=PORTAL_POLICY,
            maximum_reflection_order=1,
            maximum_portal_crossings=2,
        )
    with pytest.raises(ValueError, match='bounded second-order specular execution'):
        build_deterministic_ga_configuration(
            frequency_centers_hz=(500.0, 1000.0),
            room_policy=PORTAL_POLICY,
            maximum_reflection_order=2,
            maximum_portal_crossings=1,
        )


def _portal_reflected_path(artifact, surface_id: str):
    return next(
        item
        for item in artifact.paths
        if item.path_type == 'specular_reflection'
        and item.ordered_interaction_surface_ids == (surface_id,)
    )


def test_portal_reflection_source_and_receiver_side_paths_are_analytic_and_ordered(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, maximum_reflection_order=1)
    artifact = _execute(fx)

    assert artifact.path_scope == 'single_portal_first_order_specular'
    assert fx['execution_input'].maximum_reflection_order == 1
    portal_surface_id = fx['surface_by_key']['portal-interface']
    assert portal_surface_id not in {
        plane.source_surface_id for plane in fx['execution_input'].boundary_planes
    }

    source_surface_id = fx['surface_by_key']['portal-left-a']
    source_path = _portal_reflected_path(artifact, source_surface_id)
    assert source_path.execution_input_semantic_sha256 == fx['execution_input'].semantic_sha256
    assert source_path.ordered_interactions is not None
    assert tuple(item.kind for item in source_path.ordered_interactions) == (
        'reflection',
        'portal_crossing',
    )
    reflection, crossing = source_path.ordered_interactions
    assert reflection.point == Position3(x_m=0.0, y_m=1.25, z_m=1.0)
    assert crossing.point == Position3(x_m=2.0, y_m=1.75, z_m=1.0)
    assert source_path.ordered_region_ids == ('region-a', 'region-b')
    assert tuple(item.region_id for item in source_path.region_segment_evidence or ()) == (
        'region-a',
        'region-a',
        'region-b',
    )
    assert isclose(source_path.geometric_path_length_m, sqrt(17.0), abs_tol=1.0e-9)
    band_500 = next(item for item in source_path.bands if item.center_hz == 500.0)
    assert band_500.boundary_material is not None
    assert band_500.boundary_material.specular_energy_factor == pytest.approx(0.72)
    assert band_500.relative_energy_transport_per_m2 == pytest.approx(0.72 / 17.0)

    receiver_surface_id = fx['surface_by_key']['portal-right-b']
    receiver_path = _portal_reflected_path(artifact, receiver_surface_id)
    assert receiver_path.ordered_interactions is not None
    assert tuple(item.kind for item in receiver_path.ordered_interactions) == (
        'portal_crossing',
        'reflection',
    )
    crossing, reflection = receiver_path.ordered_interactions
    assert crossing.point == Position3(x_m=2.0, y_m=1.25, z_m=1.0)
    assert reflection.point == Position3(x_m=4.0, y_m=1.75, z_m=1.0)
    assert tuple(item.region_id for item in receiver_path.region_segment_evidence or ()) == (
        'region-a',
        'region-b',
        'region-b',
    )
    assert isclose(receiver_path.geometric_path_length_m, sqrt(17.0), abs_tol=1.0e-9)


def test_portal_reflection_finite_surface_aperture_and_occlusion_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, maximum_reflection_order=1)
    artifact = _execute(fx)

    rear_a = fx['surface_by_key']['portal-rear-a']
    assert any(
        item.interaction_surface_ids == (rear_a,)
        and item.decision == 'UNSUPPORTED_GEOMETRY'
        and 'outside the exact finite' in item.reason
        for item in artifact.rejected_candidates
    )

    front_a = fx['surface_by_key']['portal-front-a']
    assert any(
        item.interaction_surface_ids == (front_a,)
        and item.decision == 'INVALID_PORTAL_CROSSING'
        for item in artifact.rejected_candidates
    )

    blocked = _portal_fixture(
        tmp_path / 'blocked',
        maximum_reflection_order=1,
        occluder=True,
    )
    blocked_artifact = _execute(blocked)
    for key in ('portal-left-a', 'portal-right-b'):
        surface_id = blocked['surface_by_key'][key]
        assert not any(
            item.path_type == 'specular_reflection'
            and item.ordered_interaction_surface_ids == (surface_id,)
            for item in blocked_artifact.paths
        )
        assert any(
            item.interaction_surface_ids == (surface_id,)
            and item.decision == 'BLOCKED_VISIBILITY'
            for item in blocked_artifact.rejected_candidates
        )


def test_portal_reflection_wrong_event_order_and_stale_surface_fail_closed(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, maximum_reflection_order=1)
    artifact = _execute(fx)
    path = _portal_reflected_path(
        artifact,
        fx['surface_by_key']['portal-left-a'],
    )
    assert path.ordered_interactions is not None
    tampered = path.model_dump(mode='python')
    tampered['ordered_interactions'] = tuple(reversed(path.ordered_interactions))
    with pytest.raises(ValueError, match='physical event order'):
        DeterministicAcousticPath.model_validate(tampered)

    stale = dict(fx)
    stale['compiled'] = fx['compiled'].model_copy(
        update={'compiled_hash_sha256': 'f' * 64}
    )
    with pytest.raises(ValueError, match='compiled geometry exact identity mismatch'):
        _execute(stale)


def test_portal_reflection_material_authority_is_identity_binding_and_stale_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, maximum_reflection_order=1)
    artifact = _execute(fx)
    path = _portal_reflected_path(
        artifact,
        fx['surface_by_key']['portal-left-a'],
    )
    payload = path.semantic_payload()
    payload['bands'][0]['boundary_material']['material_authority']['semantic_hash_sha256'] = (
        'e' * 64
    )
    assert _digest(payload) != path.semantic_sha256

    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)
    fx['material_box']['value'] = None
    with pytest.raises(ValueError, match='does not reproduce from exact current authorities'):
        repository.get(artifact.artifact_id)


def test_portal_reflection_save_reopen_identity_and_stale_portal_rejection(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, maximum_reflection_order=1)
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    reopened = CadDeterministicPathArtifactRepository(
        SceneRepository(fx['scene_repository'].path),
        snapshot_repository=CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        dispatch_repository=CadAcousticSolverDispatchRepository(
            SceneRepository(fx['scene_repository'].path),
            external_authority_resolver=fx['external_resolver'],
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            snapshot_authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    assert reopened.get(artifact.artifact_id) == artifact

    fx['geometry_authorities'].pop(fx['portals'].authority_id)
    with pytest.raises(ValueError, match='portal exact authority'):
        repository.get(artifact.artifact_id)


def test_portal_endpoint_region_bindings_are_mandatory(tmp_path: Path) -> None:
    geometry, portal_loop = _portal_semantic_geometry()
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _fixture(
            tmp_path,
            semantic_geometry=geometry,
            room_policy=PORTAL_POLICY,
            region_surface_keys_by_id=PORTAL_REGION_SURFACES,
            portal_loop_vertex_indices=portal_loop,
            portal_surface_key='portal-interface',
            portal_region_ids=('region-a', 'region-b'),
            source_region_id=None,
            receiver_region_id='region-b',
            maximum_reflection_order=0,
            maximum_portal_crossings=1,
        )
    assert error.value.reason_code == 'UNSUPPORTED_REGION_MEMBERSHIP'


def test_portal_non_manifold_region_topology_blocks_dispatch(tmp_path: Path) -> None:
    geometry, portal_loop = _portal_semantic_geometry()
    broken_regions = dict(PORTAL_REGION_SURFACES)
    broken_regions['region-a'] = tuple(
        key for key in broken_regions['region-a']
        if key != 'portal-floor-a'
    )
    fx = _fixture(
        tmp_path,
        semantic_geometry=geometry,
        room_policy=PORTAL_POLICY,
        region_surface_keys_by_id=broken_regions,
        portal_loop_vertex_indices=portal_loop,
        portal_surface_key='portal-interface',
        portal_region_ids=('region-a', 'region-b'),
        source_region_id='region-a',
        receiver_region_id='region-b',
        maximum_reflection_order=0,
        maximum_portal_crossings=1,
        expected_dispatch_state='BLOCKED',
    )
    assert fx['compiled'].readiness.geometric_acoustics_geometry_ready is False


def test_multi_region_open_portal_direct_path_has_exact_ordered_region_sequence(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path)
    artifact = _execute(fx)

    assert fx['configuration'].maximum_reflection_order == 0
    assert fx['execution_input'].maximum_reflection_order == 0
    assert artifact.path_scope == 'direct_bounded_portal_graph_propagation'
    assert len(artifact.paths) == 1
    path = artifact.paths[0]
    assert path.path_type == 'direct'
    assert path.ordered_interaction_surface_ids == ()
    assert path.ordered_interaction_points == ()
    assert path.ordered_region_ids == ('region-a', 'region-b')
    assert path.ordered_interactions is not None
    assert len(path.ordered_interactions) == 1
    interaction = path.ordered_interactions[0]
    assert interaction.kind == 'portal_crossing'
    assert interaction.portal_id == 'fixture-portal'
    assert interaction.from_region_id == 'region-a'
    assert interaction.to_region_id == 'region-b'
    assert isclose(interaction.point.x_m, 2.0, abs_tol=1.0e-9)
    assert 1.0 <= interaction.point.y_m <= 2.0
    assert 0.5 <= interaction.point.z_m <= 1.5
    for band in path.bands:
        assert band.boundary_material is None
        assert band.boundary_materials is None
        assert band.coherent_phase == 'UNAVAILABLE_NOT_SYNTHESIZED'


def test_same_multi_region_geometry_with_closed_portal_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _portal_fixture(tmp_path, portal_state='closed')
    assert error.value.reason_code == 'UNSUPPORTED_PORTAL_STATE'


def test_portal_plane_crossing_outside_exact_polygon_is_rejected(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(
        tmp_path,
        receiver_position=Position3(x_m=3.0, y_m=0.2, z_m=1.0),
    )
    artifact = _execute(fx)
    assert artifact.paths == ()
    assert any(
        item.decision == 'INVALID_PORTAL_CROSSING'
        for item in artifact.rejected_candidates
    )


def test_portal_wrong_region_adjacency_fails_closed(tmp_path: Path) -> None:
    fx = _portal_fixture(
        tmp_path,
        portal_region_ids=('region-a', 'ghost-region'),
        expected_dispatch_state='BLOCKED',
    )
    assert fx['dispatch'].state == 'BLOCKED'


def test_portal_source_wrong_explicit_region_binding_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _portal_fixture(tmp_path, source_region_id='region-b')
    assert error.value.reason_code == 'UNSUPPORTED_REGION_MEMBERSHIP'


def test_portal_receiver_wrong_explicit_region_binding_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _portal_fixture(tmp_path, receiver_region_id='region-a')
    assert error.value.reason_code == 'UNSUPPORTED_REGION_MEMBERSHIP'


def test_portal_direct_path_is_blocked_by_opaque_surface(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path, occluder=True)
    artifact = _execute(fx)
    assert artifact.paths == ()
    assert any(
        item.decision == 'BLOCKED_VISIBILITY'
        for item in artifact.rejected_candidates
    )


def test_portal_reversed_directed_edge_loop_fails_orientation_semantics(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _portal_fixture(tmp_path, reverse_portal_orientation=True)
    assert error.value.reason_code == 'UNSUPPORTED_PORTAL_ORIENTATION'


def test_portal_path_identity_is_deterministic(tmp_path: Path) -> None:
    fx = _portal_fixture(tmp_path)
    first = _execute(fx)
    second = _execute(fx)
    assert second == first
    assert second.artifact_id == first.artifact_id
    assert second.paths[0].path_id == first.paths[0].path_id


def test_multi_region_portal_execution_input_and_artifact_save_reopen_exact_identity(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path)
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    reopened = CadDeterministicPathArtifactRepository(
        SceneRepository(fx['scene_repository'].path),
        snapshot_repository=CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        dispatch_repository=CadAcousticSolverDispatchRepository(
            SceneRepository(fx['scene_repository'].path),
            external_authority_resolver=fx['external_resolver'],
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            snapshot_authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    assert reopened.get_execution_input(
        fx['execution_input'].execution_input_id
    ) == fx['execution_input']
    assert reopened.get(artifact.artifact_id) == artifact


def test_stale_portal_authority_does_not_reopen_as_current(
    tmp_path: Path,
) -> None:
    fx = _portal_fixture(tmp_path)
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    fx['geometry_authorities'].pop(fx['portals'].authority_id)
    with pytest.raises(ValueError, match='portal exact authority'):
        repository.get(artifact.artifact_id)


def test_nontrivial_boundary_termination_is_not_transmitted_as_portal(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _portal_fixture(tmp_path, nontrivial_boundary_termination=True)
    assert error.value.reason_code == 'UNSUPPORTED_BOUNDARY_TERMINATION'


def test_three_regions_two_portals_direct_path_preserves_exact_graph_order(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(tmp_path, region_count=3)
    artifact = _execute(fx)

    graph = fx['execution_input'].portal_graph
    assert graph is not None
    assert graph.maximum_portal_crossings == 2
    assert graph.traversal_policy == 'simple_region_path_v1'
    assert graph.repeated_region_traversal is False
    assert graph.repeated_portal_traversal is False
    assert artifact.path_scope == 'direct_bounded_portal_graph_propagation'
    assert len(artifact.paths) == 1

    path = artifact.paths[0]
    assert path.ordered_region_ids == ('region-0', 'region-1', 'region-2')
    assert path.ordered_interactions is not None
    portal_interactions = tuple(
        item for item in path.ordered_interactions
        if item.kind == 'portal_crossing'
    )
    assert tuple(item.portal_id for item in portal_interactions) == (
        'fixture-portal-0-1',
        'fixture-portal-1-2',
    )
    assert tuple(
        (item.from_region_id, item.to_region_id)
        for item in portal_interactions
    ) == (
        ('region-0', 'region-1'),
        ('region-1', 'region-2'),
    )
    assert tuple(item.point.x_m for item in portal_interactions) == (2.0, 4.0)
    assert path.region_segment_evidence is not None
    assert tuple(
        (
            item.segment_index,
            item.region_id,
            item.membership_result,
            item.occlusion_result,
        )
        for item in path.region_segment_evidence
    ) == (
        (0, 'region-0', 'valid', 'clear'),
        (1, 'region-1', 'valid', 'clear'),
        (2, 'region-2', 'valid', 'clear'),
    )
    assert path.region_segment_evidence[0].start_point.x_m == 1.0
    assert path.region_segment_evidence[0].end_point.x_m == 2.0
    assert path.region_segment_evidence[1].start_point.x_m == 2.0
    assert path.region_segment_evidence[1].end_point.x_m == 4.0
    assert path.region_segment_evidence[2].start_point.x_m == 4.0
    assert path.region_segment_evidence[2].end_point.x_m == 5.0


def test_four_regions_three_portals_direct_path_is_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(tmp_path, region_count=4)
    first = _execute(fx)
    second = _execute(fx)

    assert second == first
    assert second.artifact_id == first.artifact_id
    assert len(first.paths) == 1
    path = first.paths[0]
    assert path.path_id == second.paths[0].path_id
    assert path.ordered_region_ids == (
        'region-0',
        'region-1',
        'region-2',
        'region-3',
    )
    assert tuple(
        item.portal_id
        for item in path.ordered_interactions or ()
        if item.kind == 'portal_crossing'
    ) == (
        'fixture-portal-0-1',
        'fixture-portal-1-2',
        'fixture-portal-2-3',
    )


def test_multi_portal_disconnected_directed_graph_fails_closed(
    tmp_path: Path,
) -> None:
    def reverse_last(specs):
        items = [dict(item) for item in specs]
        last = items[-1]
        last['region_ids'] = tuple(reversed(last['region_ids']))
        last['reverse'] = True
        return items

    fx = _portal_chain_fixture(
        tmp_path,
        region_count=3,
        portal_specs_transform=reverse_last,
    )
    artifact = _execute(fx)

    assert artifact.paths == ()
    assert any(
        item.decision == 'DISCONNECTED_REGION_GRAPH'
        and 'no directed simple Portal path' in item.reason
        for item in artifact.rejected_candidates
    )


def test_multi_portal_wrong_explicit_adjacency_fails_closed(
    tmp_path: Path,
) -> None:
    def wrong_adjacency(specs):
        items = [dict(item) for item in specs]
        items[1]['region_ids'] = ('region-0', 'region-2')
        return items

    with pytest.raises(
        ValueError,
        match='Portal aperture surfaces|Portal adjacency',
    ):
        _portal_chain_fixture(
            tmp_path,
            region_count=3,
            portal_specs_transform=wrong_adjacency,
        )


def test_multi_portal_aperture_miss_fails_closed(tmp_path: Path) -> None:
    fx = _portal_chain_fixture(
        tmp_path,
        region_count=3,
        receiver_position=Position3(x_m=5.0, y_m=0.2, z_m=1.0),
    )
    artifact = _execute(fx)

    assert artifact.paths == ()
    assert any(
        item.decision == 'INVALID_PORTAL_CROSSING'
        and 'fixture-portal-0-1' in item.reason
        for item in artifact.rejected_candidates
    )


def test_multi_portal_crossing_limit_fails_closed_with_explicit_reason(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(
        tmp_path,
        region_count=4,
        maximum_portal_crossings=2,
    )
    artifact = _execute(fx)

    assert artifact.paths == ()
    assert any(
        item.decision == 'PORTAL_CROSSING_LIMIT_EXCEEDED'
        and 'maximum_portal_crossings=2' in item.reason
        for item in artifact.rejected_candidates
    )


def test_multi_portal_execution_input_and_artifact_save_reopen_exact_identity(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(tmp_path, region_count=3)
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened = CadDeterministicPathArtifactRepository(
        reopened_scene,
        snapshot_repository=CadAcousticSnapshotRepository(
            reopened_scene,
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        dispatch_repository=CadAcousticSolverDispatchRepository(
            reopened_scene,
            external_authority_resolver=fx['external_resolver'],
            fidelity_policy_resolver=fx['fidelity_policy_resolver'],
            snapshot_authority_resolvers=fx['snapshot_authority_resolvers'],
        ),
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    assert reopened.get_execution_input(
        fx['execution_input'].execution_input_id
    ) == fx['execution_input']
    assert reopened.get(artifact.artifact_id) == artifact


def test_stale_multi_portal_authority_does_not_reopen_as_current(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(tmp_path, region_count=3)
    artifact = _execute(fx)
    repository = CadDeterministicPathArtifactRepository(
        fx['scene_repository'],
        snapshot_repository=fx['snapshot_repository'],
        dispatch_repository=fx['dispatch_repository'],
        configuration_resolver=fx['configuration_resolver'],
        material_resolver=fx['material_resolver'],
        geometry_authority_resolver=fx['geometry_resolver'],
    )
    repository.save_execution_input(fx['execution_input'])
    repository.save(artifact)

    fx['geometry_authorities'].pop(fx['portals'].authority_id)
    with pytest.raises(ValueError, match='portal exact authority'):
        repository.get(artifact.artifact_id)


def test_snapshot_read_reresolves_exact_r150_topology_preflight(
    tmp_path: Path,
) -> None:
    fx = _portal_chain_fixture(tmp_path, region_count=3)
    snapshot = fx['snapshot']
    preflight_ref = snapshot.geometric_acoustics_topology_preflight_ref
    assert preflight_ref is not None
    assert snapshot.readiness.requested_observable_ready

    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        fidelity_policy_resolver=fx['fidelity_policy_resolver'],
        authority_resolvers=fx['snapshot_authority_resolvers'],
    )
    assert reopened.get_snapshot(snapshot.snapshot_id) == snapshot

    missing = fx['snapshot_authority_resolvers']._replace(
        geometric_topology_preflight=lambda ref: None
    )
    with pytest.raises(
        ValueError,
        match='geometric topology preflight exact external authority '
        'does not exist',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=missing,
        ).get_snapshot(snapshot.snapshot_id)

    with pytest.raises(
        ValueError,
        match='geometric topology preflight authority requires a typed '
        'preflight resolver',
    ):
        CadAcousticSnapshotRepository(
            SceneRepository(fx['scene_repository'].path),
            authority_resolvers=fx['snapshot_authority_resolvers']._replace(
                geometric_topology_preflight=None
            ),
        ).get_snapshot(snapshot.snapshot_id)
