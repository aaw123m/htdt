from __future__ import annotations

from htdt.cad_repository import SceneRevision
from htdt.cad_scene import SceneDocument, scene_content_hash
from htdt.r120_geometry_compiler import (
    AcousticRegionDeclaration,
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
    make_acoustic_region_authority,
    make_portal_authority,
    make_r120_geometry_compilation_request,
)
from htdt.raw_mesh import diagnose_raw_visual_mesh, import_raw_visual_mesh
from htdt.raw_mesh_repair import (
    ExactDuplicateVertexConsolidation,
    RemoveUnreferencedVertices,
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


IMPERFECT_CLOSED_TETRA = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
v 0 0 0
v 5 5 5
f 1 3 2
f 5 2 4
f 1 4 3
f 2 3 4
'''


def _external_ref(authority_id: str, digest_char: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority_id,
        authority_version='fixture-v1',
        semantic_hash_sha256=digest_char * 64,
    )


def test_imperfect_import_repair_semantic_conversion_and_r120_compile_are_exact() -> None:
    raw = import_raw_visual_mesh(
        IMPERFECT_CLOSED_TETRA,
        source_name='issue-167-r120-acceptance.obj',
    )
    source_diagnostic = diagnose_raw_visual_mesh(raw)

    repair_plan = make_raw_mesh_repair_plan(
        raw,
        source_diagnostic,
        operations=(
            ExactDuplicateVertexConsolidation(),
            RemoveUnreferencedVertices(),
        ),
        requested_by='explicit_user_selected',
        request_reason=(
            'Issue #167 acceptance fixture explicitly consolidates one duplicate '
            'vertex and removes one unreferenced vertex'
        ),
    )
    repaired = apply_raw_mesh_repair(raw, source_diagnostic, repair_plan)
    post_diagnostic = diagnose_repaired_raw_mesh(raw, repaired)

    assert raw.original_asset_bytes() == IMPERFECT_CLOSED_TETRA
    assert repaired.original_asset_sha256 == raw.provenance.original_asset_sha256
    assert repaired.vertex_count_before == 6
    assert repaired.vertex_count_after == 4
    assert repaired.triangle_count_before == 4
    assert repaired.triangle_count_after == 4
    assert post_diagnostic.acoustic_volume_readiness == (
        'geometry_checks_pass_but_semantic_conversion_required'
    )
    assert all(
        finding.state == 'pass'
        for finding in post_diagnostic.findings
    )

    triangle_ids = repaired_triangle_ids(repaired)
    conversion_request = make_semantic_geometry_conversion_request(
        raw,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='acceptance fixture OBJ coordinates are explicitly HTDT metres',
        ),
        repaired_mesh=repaired,
        repaired_diagnostic=post_diagnostic,
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='repaired-room-shell',
                triangle_ids=triangle_ids,
                semantic_class='room_boundary',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(
        raw,
        conversion_request,
        repaired_mesh=repaired,
        repaired_diagnostic=post_diagnostic,
    )

    lineage = geometry.conversion_request.raw_mesh_repair_lineage
    assert lineage is not None
    assert lineage.source_raw_mesh_id == raw.mesh_id
    assert lineage.repair_plan_id == repair_plan.plan_id
    assert lineage.repaired_mesh_id == repaired.repaired_mesh_id
    assert lineage.post_repair_diagnostic_id == post_diagnostic.diagnostic_id
    assert geometry.geometry_compiler_readiness == (
        'ready_for_r120_geometry_compiler_contract'
    )
    assert geometry.solver_ready is False

    document = SceneDocument(
        document_id='issue-167-r120-acceptance',
        schema_version=4,
        room=None,
        r120_semantic_geometry=geometry,
        entities=(),
    )
    revision = SceneRevision(
        revision_id='scene-issue-167-r120-acceptance',
        document_id=document.document_id,
        parent_revision_id=None,
        created_at_utc='2026-09-20T00:00:00+00:00',
        content_hash=scene_content_hash(document),
        document=document,
    )

    surface_id = geometry.surfaces[0].surface_id
    material_ref = _external_ref('fixture-material', 'a')
    boundary_ref = _external_ref('fixture-boundary-physics', 'b')
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(
        revision,
        request,
        surface_boundary_bindings=(
            SurfaceBoundaryAuthorityBinding(
                source_surface_id=surface_id,
                material_authority=material_ref,
                boundary_physics_authority=boundary_ref,
            ),
        ),
        region_authority=region,
        portal_authority=portals,
    )

    assert compiled.exact_scene_revision_id == revision.revision_id
    assert compiled.exact_scene_revision_content_hash == revision.content_hash
    assert compiled.exact_semantic_geometry_id == geometry.geometry_id
    assert compiled.exact_semantic_geometry_hash_sha256 == geometry.semantic_hash_sha256
    assert compiled.closed_shell_diagnostics.closed_shell is True
    assert compiled.closed_shell_diagnostics.enclosed_volume_m3 == 1.0 / 6.0
    assert compiled.readiness.geometry_compiled is True
    assert compiled.readiness.wave_geometry_ready is True
    assert compiled.readiness.geometric_acoustics_geometry_ready is True
    assert compiled.unresolved_conditions == ()
    assert compiled.surface_mapping[0].material_authority == material_ref
    assert compiled.surface_mapping[0].boundary_physics_authority == boundary_ref
