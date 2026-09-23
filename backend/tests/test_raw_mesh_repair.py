from pathlib import Path

import pytest

from htdt.raw_mesh import diagnose_raw_visual_mesh, import_raw_visual_mesh
from htdt.raw_mesh_repair import (
    CorrectConsistentWinding,
    ExactDuplicateVertexConsolidation,
    RawMeshRepairBundle,
    RawMeshRepairError,
    RawMeshRepairRepository,
    RemoveDegenerateFaces,
    RemoveExactDuplicateFaces,
    ToleranceVertexWeld,
    UnsupportedRawMeshRepairOperation,
    apply_raw_mesh_repair,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_plan,
    repaired_triangle_ids,
)
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
)


def _mesh(asset: bytes, name: str = 'fixture.obj'):
    return import_raw_visual_mesh(asset, source_name=name)


def _duplicate_mesh():
    return _mesh(
        b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 0
f 1 2 3
f 4 2 3
f 1 2 3
''',
        'duplicates.obj',
    )


def _make_plan(mesh, operations):
    diagnostic = diagnose_raw_visual_mesh(mesh)
    plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=operations,
        requested_by='explicit_user_selected',
        request_reason='focused Issue #167 fixture',
    )
    return diagnostic, plan


def test_same_raw_mesh_same_plan_is_deterministic_and_preserves_original_asset() -> None:
    mesh = _duplicate_mesh()
    before = mesh.model_dump(mode='json')
    original = mesh.original_asset_bytes()
    diagnostic, plan = _make_plan(
        mesh,
        (
            ExactDuplicateVertexConsolidation(),
            RemoveExactDuplicateFaces(),
        ),
    )

    first = apply_raw_mesh_repair(mesh, diagnostic, plan)
    repeated = apply_raw_mesh_repair(mesh, diagnostic, plan)

    assert first == repeated
    assert first.repaired_mesh_id == repeated.repaired_mesh_id
    assert first.semantic_hash() == repeated.semantic_hash()
    assert mesh.model_dump(mode='json') == before
    assert mesh.original_asset_bytes() == original
    assert mesh.provenance.original_asset_sha256 == first.original_asset_sha256
    assert first.vertex_count_before == 4
    assert first.vertex_count_after == 3
    assert first.triangle_count_before == 3
    assert first.triangle_count_after == 1
    assert first.repair_plan_id == plan.plan_id


def test_explicit_tolerance_weld_records_displacement_and_parameter_changes_identity() -> None:
    mesh = _mesh(
        b'''\
v 0 0 0
v 0.0001 0 0
v 1 0 0
v 0 1 0
f 1 3 4
f 2 3 4
''',
        'weld.obj',
    )
    diagnostic = diagnose_raw_visual_mesh(mesh)

    loose_plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(
            ToleranceVertexWeld(tolerance_source_units=0.001),
            RemoveExactDuplicateFaces(),
        ),
        requested_by='workflow_selected',
        request_reason='explicit workflow-selected tolerance',
    )
    tighter_plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(
            ToleranceVertexWeld(tolerance_source_units=0.00001),
            RemoveExactDuplicateFaces(),
        ),
        requested_by='workflow_selected',
        request_reason='explicit workflow-selected tolerance',
    )

    loose = apply_raw_mesh_repair(mesh, diagnostic, loose_plan)
    tighter = apply_raw_mesh_repair(mesh, diagnostic, tighter_plan)
    weld_result = loose.operation_results[0]

    assert loose_plan.plan_id != tighter_plan.plan_id
    assert loose.repaired_mesh_id != tighter.repaired_mesh_id
    assert weld_result.operation.kind == 'tolerance_vertex_weld'
    assert weld_result.moved_vertex_count == 1
    assert weld_result.maximum_displacement_source_units == pytest.approx(0.0001)
    assert weld_result.topology_change_count > 0
    assert loose.vertex_count_after == 3
    assert loose.triangle_count_after == 1
    assert tighter.vertex_count_after == 4


def test_weld_that_collapses_face_requires_explicit_degenerate_removal() -> None:
    mesh = _mesh(
        b'''\
v 0 0 0
v 0.0001 0 0
v 0 1 0
v 1 0 0
f 1 2 3
f 1 3 4
''',
        'collapsed-face.obj',
    )
    diagnostic = diagnose_raw_visual_mesh(mesh)
    blocked_plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(ToleranceVertexWeld(tolerance_source_units=0.001),),
        requested_by='explicit_user_selected',
        request_reason='exercise explicit degenerate handling',
    )
    with pytest.raises(RawMeshRepairError, match='explicitly request remove_degenerate_faces'):
        apply_raw_mesh_repair(mesh, diagnostic, blocked_plan)

    explicit_plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(
            ToleranceVertexWeld(tolerance_source_units=0.001),
            RemoveDegenerateFaces(area_tolerance_source_units_squared=0.0),
        ),
        requested_by='explicit_user_selected',
        request_reason='explicitly remove weld-created degeneracy',
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, explicit_plan)
    assert repaired.triangle_count_after == 1
    assert repaired.operation_results[1].execution_state == 'applied'


def test_consistent_winding_correction_is_bounded_and_reuses_diagnostics() -> None:
    mesh = _mesh(
        b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 4 2
f 1 4 3
f 2 3 4
''',
        'winding.obj',
    )
    before = diagnose_raw_visual_mesh(mesh)
    assert next(f for f in before.findings if f.code == 'inverted_normal').state == 'fail'

    plan = make_raw_mesh_repair_plan(
        mesh,
        before,
        operations=(CorrectConsistentWinding(),),
        requested_by='explicit_user_selected',
        request_reason='correct relative winding only',
    )
    repaired = apply_raw_mesh_repair(mesh, before, plan)
    after = diagnose_repaired_raw_mesh(mesh, repaired)

    assert repaired.operation_results[0].topology_change_count > 0
    assert 'global outward orientation was not inferred' in repaired.operation_results[0].detail
    assert next(f for f in after.findings if f.code == 'inverted_normal').state == 'pass'
    assert after.solver_ready is False
    assert after.semantic_conversion_required is True


def test_unsupported_hole_and_non_manifold_repairs_are_not_executed() -> None:
    mesh = _mesh(
        b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 -1 0
v 0 0 1
f 1 2 3
f 2 1 4
f 1 2 5
''',
        'open-non-manifold.obj',
    )
    diagnostic = diagnose_raw_visual_mesh(mesh)
    plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=(
            UnsupportedRawMeshRepairOperation(
                kind='fill_hole',
                reason='hole filling is outside bounded repair v1',
            ),
            UnsupportedRawMeshRepairOperation(
                kind='non_manifold_surgery',
                reason='non-manifold surgery is outside bounded repair v1',
            ),
        ),
        requested_by='explicit_user_selected',
        request_reason='prove unsupported repairs remain explicit',
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, plan)
    after = diagnose_repaired_raw_mesh(mesh, repaired)
    findings = {finding.code: finding for finding in after.findings}

    assert repaired.vertices == mesh.vertices
    assert repaired.triangles == mesh.triangles
    assert plan.unsupported_operation_kinds == ('fill_hole', 'non_manifold_surgery')
    assert all(result.execution_state == 'unsupported' for result in repaired.operation_results)
    assert 'unsupported_operation:fill_hole' in repaired.unsupported_unresolved_findings
    assert 'unsupported_operation:non_manifold_surgery' in repaired.unsupported_unresolved_findings
    assert findings['open_boundary'].state == 'fail'
    assert findings['non_manifold_edge'].state == 'fail'
    assert after.acoustic_volume_readiness == 'not_ready'


def test_repaired_mesh_semantic_conversion_preserves_exact_raw_repair_lineage() -> None:
    mesh = _duplicate_mesh()
    diagnostic, plan = _make_plan(
        mesh,
        (
            ExactDuplicateVertexConsolidation(),
            RemoveExactDuplicateFaces(),
        ),
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, plan)
    post = diagnose_repaired_raw_mesh(mesh, repaired)
    triangle_ids = repaired_triangle_ids(repaired)

    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id='scene-revision-parent',
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture source coordinates are explicitly metres',
        ),
        repaired_mesh=repaired,
        repaired_diagnostic=post,
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='repaired-surface',
                triangle_ids=triangle_ids,
                semantic_class='object_surface',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(
        mesh,
        request,
        repaired_mesh=repaired,
        repaired_diagnostic=post,
    )

    lineage = request.raw_mesh_repair_lineage
    assert lineage is not None
    assert lineage.source_raw_mesh_id == mesh.mesh_id
    assert lineage.source_diagnostic_id == diagnostic.diagnostic_id
    assert lineage.repair_plan_id == plan.plan_id
    assert lineage.repaired_mesh_id == repaired.repaired_mesh_id
    assert lineage.post_repair_diagnostic_id == post.diagnostic_id
    assert geometry.conversion_request == request
    assert geometry.input_raw_mesh_id == mesh.mesh_id
    assert geometry.input_diagnostic_id == post.diagnostic_id
    assert geometry.triangles[0].source_triangle_id == triangle_ids[0]
    assert geometry.solver_ready is False
    assert geometry.geometry_compiler_readiness == 'blocked_by_geometry'


def test_native_sqlite_save_reopen_revalidates_all_repair_identities(tmp_path: Path) -> None:
    mesh = _duplicate_mesh()
    diagnostic, plan = _make_plan(
        mesh,
        (
            ExactDuplicateVertexConsolidation(),
            RemoveExactDuplicateFaces(),
        ),
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, plan)
    post = diagnose_repaired_raw_mesh(mesh, repaired)
    bundle = RawMeshRepairBundle(
        source_raw_mesh=mesh,
        source_diagnostic=diagnostic,
        repair_plan=plan,
        repaired_mesh=repaired,
        post_repair_diagnostic=post,
    )
    repository = RawMeshRepairRepository(tmp_path / 'cad.sqlite3')

    assert repository.save(bundle) is True
    assert repository.save(bundle) is False
    reopened = repository.get(repaired.repaired_mesh_id)

    assert reopened is not None
    assert reopened.source_raw_mesh.mesh_id == mesh.mesh_id
    assert reopened.source_raw_mesh.semantic_hash() == mesh.semantic_hash()
    assert reopened.repair_plan.plan_id == plan.plan_id
    assert reopened.repair_plan.semantic_hash() == plan.semantic_hash()
    assert reopened.repaired_mesh.repaired_mesh_id == repaired.repaired_mesh_id
    assert reopened.repaired_mesh.semantic_hash() == repaired.semantic_hash()
    assert reopened.post_repair_diagnostic.diagnostic_id == post.diagnostic_id
    assert reopened.post_repair_diagnostic.semantic_hash() == post.semantic_hash()


def test_stale_source_or_raw_hash_mismatch_is_rejected() -> None:
    first = _duplicate_mesh()
    first_diagnostic, plan = _make_plan(
        first,
        (RemoveExactDuplicateFaces(),),
    )
    second = _mesh(
        b'''\
v 0 0 0
v 2 0 0
v 0 2 0
f 1 2 3
''',
        'different.obj',
    )

    with pytest.raises(RawMeshRepairError, match='source diagnostic raw mesh id mismatch'):
        apply_raw_mesh_repair(second, first_diagnostic, plan)
