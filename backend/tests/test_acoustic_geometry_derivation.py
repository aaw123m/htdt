"""REV47-ISS2: explicit Architectural -> Derivation -> SolverGeometry record.

The chain itself existed hop-by-hop; these tests pin the single auditable
record that names it and enumerates the derivation decision categories
fail-closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_geometry_derivation import (
    AcousticGeometryDerivation,
    build_acoustic_geometry_derivation,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.r120_geometry_compiler import (
    PortalBoundaryEdge,
    PortalDeclaration,
    compile_r120_geometry,
    make_portal_authority,
    make_r120_geometry_compilation_request,
)
from htdt.r120_geometry_compiler_repository import R120GeometryCompilerRepository
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    RemoveTriangleRepair,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


CLOSED_TETRA = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
'''

TWO_TETRA_WITH_TINY_SECOND = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
v 2 0 0
v 2.0001 0 0
v 2 0.0001 0
v 2 0 0.0001
f 5 7 6
f 5 6 8
f 5 8 7
f 6 7 8
'''


def _scene(
    tmp_path: Path,
    *,
    asset: bytes = CLOSED_TETRA,
    remove_triangle: bool = False,
    input_policy: str = 'require_contract_ready',
    tolerance: float = 1.0e-6,
    approximation_policy: str = 'none',
    tiny_feature_policy: str = 'preserve',
    persist_compiled: bool = True,
):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    mesh = import_raw_visual_mesh(asset, source_name='derivation-fixture.obj')
    triangle_ids = raw_triangle_ids(mesh)
    repairs = ()
    retained = triangle_ids
    if remove_triangle:
        removed = triangle_ids[0]
        repairs = (
            RemoveTriangleRepair(
                triangle_id=removed,
                reason='fixture removes one face to create an opening',
            ),
        )
        retained = tuple(item for item in triangle_ids if item != removed)
    assignments = (
        SurfaceSemanticAssignment(
            surface_key='room-shell',
            triangle_ids=retained,
            semantic_class='room_boundary',
        ),
    )
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture OBJ coordinates are explicitly HTDT metres',
        ),
        repairs=repairs,
        surface_assignments=assignments,
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)
    document = SceneDocument(
        document_id='derivation-fixture',
        schema_version=4,
        room=None,
        r120_semantic_geometry=geometry,
        entities=(),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision
    compile_request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=tolerance,
        input_policy=input_policy,
        approximation_policy=approximation_policy,
        tiny_feature_policy=tiny_feature_policy,
    )
    compiled = compile_r120_geometry(revision, compile_request)
    repository = R120GeometryCompilerRepository(scene_repository)
    if persist_compiled:
        repository.save_compiled_geometry(compiled)
    return {
        'scene_repository': scene_repository,
        'repository': repository,
        'mesh': mesh,
        'request': request,
        'geometry': geometry,
        'revision': revision,
        'compiled': compiled,
    }


def _decisions(derivation: AcousticGeometryDerivation):
    return {item.category: item for item in derivation.category_decisions}


def test_derivation_binds_every_hop_exactly(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )

    compiled = fx['compiled']
    geometry = fx['geometry']
    assert derivation.scene_revision_id == compiled.exact_scene_revision_id
    assert (
        derivation.scene_revision_content_hash
        == compiled.exact_scene_revision_content_hash
    )
    assert derivation.input_raw_mesh_id == geometry.input_raw_mesh_id
    assert (
        derivation.input_raw_mesh_semantic_hash
        == geometry.input_raw_mesh_semantic_hash
    )
    assert derivation.input_diagnostic_id == geometry.input_diagnostic_id
    assert derivation.semantic_geometry_id == geometry.geometry_id
    assert (
        derivation.semantic_geometry_hash_sha256
        == geometry.semantic_hash_sha256
    )
    assert derivation.conversion_request_id == geometry.conversion_request_id
    assert derivation.r120_compile_request_id == compiled.request.request_id
    assert derivation.r120_compiled_geometry_id == compiled.compiled_geometry_id
    assert (
        derivation.r120_compiled_geometry_hash_sha256
        == compiled.compiled_hash_sha256
    )
    assert (
        derivation.topology_identity_sha256
        == compiled.topology_identity_sha256
    )
    assert derivation.compiler_id == 'htdt.r120.solver_neutral_geometry_compiler'
    assert derivation.derivation_id.startswith('acoustic-geometry-derivation:')
    assert len(derivation.semantic_sha256) == 64


def test_derivation_declares_every_category_fail_closed(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    decisions = _decisions(derivation)

    assert set(decisions) == {
        'removed_or_simplified_features',
        'merged_faces',
        'opening_treatment',
        'edge_diffraction_treatment',
        'scattering_substitution',
    }
    # Mechanisms that do not exist in the chain are declared NOT_APPLIED —
    # never silently absent and never fabricated as applied.
    assert decisions['merged_faces'].state == 'NOT_APPLIED'
    assert decisions['edge_diffraction_treatment'].state == 'NOT_APPLIED'
    assert decisions['scattering_substitution'].state == 'NOT_APPLIED'
    assert decisions['opening_treatment'].state == 'APPLIED'
    assert decisions['removed_or_simplified_features'].state == 'NOT_APPLIED'
    assert derivation.closed_shell is True
    assert derivation.boundary_edge_count == 0
    assert derivation.declared_portal_ids == ()


def test_derivation_records_opening_classification(tmp_path: Path) -> None:
    fx = _scene(
        tmp_path,
        remove_triangle=True,
        input_policy='diagnostic_compile_unresolved',
        persist_compiled=False,
    )
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    decisions = _decisions(derivation)

    assert derivation.closed_shell is False
    assert derivation.boundary_edge_count > 0
    assert decisions['opening_treatment'].state == 'APPLIED'
    assert 'boundary_edges' in decisions['opening_treatment'].detail


def test_derivation_records_dropped_feature_category_applied(
    tmp_path: Path,
) -> None:
    fx = _scene(
        tmp_path,
        asset=TWO_TETRA_WITH_TINY_SECOND,
        input_policy='diagnostic_compile_unresolved',
        tolerance=0.001,
        approximation_policy='explicit_policy_only',
        tiny_feature_policy='drop_below_tolerance',
        persist_compiled=False,
    )
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    decisions = _decisions(derivation)

    assert decisions['removed_or_simplified_features'].state == 'APPLIED'
    assert len(derivation.approximation_operations) == 4
    assert len(derivation.dropped_features) == 4
    assert derivation.approximation_error_status == (
        'not_computed_for_dropped_features'
    )
    assert derivation.maximum_dropped_feature_extent_m > 0.0


def test_derivation_rejects_never_applied_category_claims(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    payload = derivation.model_dump(mode='python')
    payload['category_decisions'] = [
        item.model_dump(mode='python')
        for item in derivation.category_decisions
    ]
    for item in payload['category_decisions']:
        if item['category'] == 'edge_diffraction_treatment':
            item['state'] = 'APPLIED'
            item['detail'] = 'fraudulent claim for test'
    # Fresh ids so the model must re-validate on the corrupted payload.
    payload['derivation_id'] = f"acoustic-geometry-derivation:{'0' * 64}"
    payload['semantic_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='cannot be declared APPLIED'):
        AcousticGeometryDerivation.model_validate(payload)


def test_derivation_rejects_missing_category(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    payload = derivation.model_dump(mode='python')
    payload['category_decisions'] = [
        item.model_dump(mode='python')
        for item in derivation.category_decisions
        if item.category != 'scattering_substitution'
    ]
    payload['derivation_id'] = f"acoustic-geometry-derivation:{'0' * 64}"
    payload['semantic_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='every category exactly once'):
        AcousticGeometryDerivation.model_validate(payload)


def test_derivation_builder_rejects_unbound_geometry(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    other = _scene(
        tmp_path / 'other',
        remove_triangle=True,
        input_policy='diagnostic_compile_unresolved',
        persist_compiled=False,
    )
    with pytest.raises(ValueError, match='semantic geometry'):
        build_acoustic_geometry_derivation(
            semantic_geometry=other['geometry'],
            compiled_geometry=fx['compiled'],
        )


def test_derivation_builder_rejects_unbound_portal_authority(
    tmp_path: Path,
) -> None:
    fx = _scene(tmp_path)
    foreign_portals = make_portal_authority(
        declaration_mode='explicit_list',
        declarations=(
            PortalDeclaration(
                portal_id='p-foreign',
                region_ids=('room-a', 'room-b'),
                boundary_edges=(
                    PortalBoundaryEdge(
                        source_surface_id=f'semantic-surface:{"1" * 64}',
                        vertex_a=0,
                        vertex_b=1,
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match='portal authority'):
        build_acoustic_geometry_derivation(
            semantic_geometry=fx['geometry'],
            compiled_geometry=fx['compiled'],
            portal_authority=foreign_portals,
        )


def test_derivation_persists_and_revalidates(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    repository = fx['repository']

    saved = repository.save_derivation(derivation)
    assert saved == derivation

    reopened = R120GeometryCompilerRepository(
        SceneRepository(fx['scene_repository'].path)
    )
    loaded = reopened.get_derivation(derivation.derivation_id)
    assert loaded == derivation
    by_compiled = reopened.get_derivation_for_compiled(
        fx['compiled'].compiled_geometry_id
    )
    assert by_compiled == derivation


def test_derivation_save_requires_persisted_compiled_geometry(
    tmp_path: Path,
) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    fresh_repository = R120GeometryCompilerRepository(
        SceneRepository(tmp_path / 'empty.sqlite3')
    )
    with pytest.raises(ValueError, match='not persisted'):
        fresh_repository.save_derivation(derivation)


def test_derivation_id_is_content_bound(tmp_path: Path) -> None:
    fx = _scene(tmp_path)
    derivation = build_acoustic_geometry_derivation(
        semantic_geometry=fx['geometry'],
        compiled_geometry=fx['compiled'],
    )
    payload = derivation.model_dump(mode='python')
    payload['compiler_warnings'] = ['tampered']
    with pytest.raises(ValueError, match='hash mismatch'):
        AcousticGeometryDerivation.model_validate(payload)
