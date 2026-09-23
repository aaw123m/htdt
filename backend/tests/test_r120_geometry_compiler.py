from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from htdt.cad_repository import SceneRepository, SceneRevision
from htdt.cad_scene import SceneDocument, scene_content_hash
from htdt.r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    LeakDiagnosticSample,
    PortalAuthority,
    PortalBoundaryEdge,
    PortalDeclaration,
    R120CompiledGeometry,
    R120GeometryCompilationError,
    R120LeakPortalDiagnostic,
    SurfaceBoundaryAuthorityBinding,
    _semantic_hash,
    compile_r120_geometry,
    deserialize_r120_compiled_geometry,
    deserialize_r120_leak_portal_diagnostic,
    diagnose_r120_leak_and_portals,
    make_acoustic_region_authority,
    make_boundary_termination_authority,
    make_leak_portal_diagnostic_request,
    make_leak_sampling_authority,
    make_portal_authority,
    make_r120_geometry_compilation_request,
    serialize_r120_compiled_geometry,
    serialize_r120_leak_portal_diagnostic,
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


def _revision(
    asset: bytes = CLOSED_TETRA,
    *,
    assign_semantics: bool = True,
    remove_triangle_index: int | None = None,
    revision_id: str = 'scene-r120-fixture',
) -> tuple[SceneRevision, object]:
    mesh = import_raw_visual_mesh(asset, source_name='r120-fixture.obj')
    triangle_ids = raw_triangle_ids(mesh)
    repairs = ()
    retained = triangle_ids
    if remove_triangle_index is not None:
        removed = triangle_ids[remove_triangle_index]
        repairs = (
            RemoveTriangleRepair(
                triangle_id=removed,
                reason='fixture explicitly removes one face to create an unresolved opening',
            ),
        )
        retained = tuple(item for item in triangle_ids if item != removed)

    assignments = ()
    if assign_semantics:
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
        document_id='r120-compiler-fixture',
        schema_version=4,
        room=None,
        r120_semantic_geometry=geometry,
        entities=(),
    )
    revision = SceneRevision(
        revision_id=revision_id,
        document_id=document.document_id,
        parent_revision_id=None,
        created_at_utc='2026-09-19T00:00:00+00:00',
        content_hash=scene_content_hash(document),
        document=document,
    )
    return revision, mesh


def _compile(
    revision: SceneRevision,
    *,
    input_policy: str = 'require_contract_ready',
    tolerance: float = 1.0e-6,
    approximation_policy: str = 'none',
    tiny_feature_policy: str = 'preserve',
    **kwargs,
):
    request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=tolerance,
        input_policy=input_policy,
        approximation_policy=approximation_policy,
        tiny_feature_policy=tiny_feature_policy,
    )
    return compile_r120_geometry(revision, request, **kwargs)


def _sampling():
    return make_leak_sampling_authority(
        (
            LeakDiagnosticSample(
                sample_id='toward-z0-opening',
                origin_m=(0.1, 0.1, 0.1),
                direction_unit=(0.0, 0.0, -1.0),
            ),
        )
    )


def _surface_id(revision: SceneRevision) -> str:
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    return geometry.surfaces[0].surface_id


def _dummy_external_ref(name: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256='a' * 64,
    )


def test_compiler_contract_preserves_exact_scene_geometry_and_surface_mapping() -> None:
    revision, _ = _revision()
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    assert geometry.geometry_compiler_readiness == 'ready_for_r120_geometry_compiler_contract'
    assert geometry.solver_ready is False

    compiled = _compile(revision)

    assert compiled.exact_scene_revision_id == revision.revision_id
    assert compiled.exact_scene_revision_content_hash == revision.content_hash
    assert compiled.exact_semantic_geometry_id == geometry.geometry_id
    assert compiled.exact_semantic_geometry_hash_sha256 == geometry.semantic_hash_sha256
    assert compiled.request.compiler_id == 'htdt.r120.solver_neutral_geometry_compiler'
    assert compiled.request.compiler_version == '1'
    assert compiled.request.target_representation == 'indexed_triangle_surface_v1'
    assert compiled.request.coordinate_convention == 'htdt-x-right-y-rear-z-up'
    assert compiled.request.unit_convention == 'metre'
    assert compiled.closed_shell_diagnostics.closed_shell is True
    assert compiled.closed_shell_diagnostics.boundary_edge_count == 0
    assert compiled.closed_shell_diagnostics.enclosed_volume_m3 == 1.0 / 6.0
    assert len(compiled.surface_mapping) == 1
    assert compiled.surface_mapping[0].source_surface_id == geometry.surfaces[0].surface_id
    assert compiled.surface_mapping[0].source_triangle_ids == geometry.surfaces[0].triangle_ids
    assert compiled.surface_mapping[0].compiled_triangle_indices == (0, 1, 2, 3)
    assert compiled.readiness.geometry_compiled is True
    assert compiled.readiness.wave_geometry_ready is False
    assert compiled.readiness.geometric_acoustics_geometry_ready is False
    assert compiled.readiness.material_assignment_missing is True
    assert compiled.readiness.region_definition_missing is True
    assert compiled.readiness.portal_definition_missing is True
    assert compiled.readiness.boundary_physics_missing is True


def test_exact_external_authorities_can_complete_closed_geometry_readiness_without_invented_materials() -> None:
    revision, _ = _revision()
    surface_id = _surface_id(revision)
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    bindings = (
        SurfaceBoundaryAuthorityBinding(
            source_surface_id=surface_id,
            material_authority=_dummy_external_ref('fixture-material'),
            boundary_physics_authority=_dummy_external_ref('fixture-boundary-physics'),
        ),
    )

    compiled = _compile(
        revision,
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
    )

    assert compiled.readiness.material_assignment_missing is False
    assert compiled.readiness.region_definition_missing is False
    assert compiled.readiness.portal_definition_missing is False
    assert compiled.readiness.boundary_physics_missing is False
    assert compiled.readiness.wave_geometry_ready is True
    assert compiled.readiness.geometric_acoustics_geometry_ready is True
    without_physics = _compile(revision)
    assert compiled.topology_identity_sha256 == without_physics.topology_identity_sha256
    assert compiled.compiled_hash_sha256 != without_physics.compiled_hash_sha256
    assert compiled.surface_mapping[0].material_authority == bindings[0].material_authority
    assert compiled.surface_mapping[0].boundary_physics_authority == bindings[0].boundary_physics_authority


def test_unresolved_semantic_geometry_is_blocked_normally_but_can_compile_for_diagnostics() -> None:
    revision, _ = _revision(remove_triangle_index=0)
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    assert geometry.geometry_compiler_readiness == 'blocked_by_geometry'

    request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    try:
        compile_r120_geometry(revision, request)
    except R120GeometryCompilationError as exc:
        assert 'not ready_for_r120_geometry_compiler_contract' in str(exc)
    else:
        raise AssertionError('unresolved semantic geometry must fail normal compilation')

    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')
    assert compiled.readiness.geometry_compiled is True
    assert compiled.readiness.wave_geometry_ready is False
    assert 'input_semantic_geometry_not_compiler_contract_ready' in compiled.unresolved_conditions
    assert compiled.closed_shell_diagnostics.closed_shell is False
    assert compiled.closed_shell_diagnostics.boundary_edge_count == 3


def test_unknown_semantic_surface_stays_explicitly_blocked() -> None:
    revision, _ = _revision(assign_semantics=False)
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    assert geometry.geometry_compiler_readiness == 'blocked_by_surface_semantics'
    assert geometry.surfaces[0].semantic_class == 'unknown'

    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')

    assert compiled.surface_mapping[0].source_surface_id == geometry.surfaces[0].surface_id
    assert compiled.surface_mapping[0].semantic_class == 'unknown'
    assert 'unknown_semantic_surface' in compiled.unresolved_conditions
    assert compiled.readiness.wave_geometry_ready is False


def test_unintended_opening_is_distinct_from_explicit_portal_and_has_deterministic_ray_evidence() -> None:
    revision, _ = _revision(remove_triangle_index=0)
    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')
    portals = make_portal_authority(declaration_mode='explicit_none')
    request = make_leak_portal_diagnostic_request(
        compiled,
        closed_boundary_expectation=True,
        sampling_authority=_sampling(),
    )

    result = diagnose_r120_leak_and_portals(compiled, request, portal_authority=portals)

    codes = {finding.code for finding in result.findings}
    assert 'unintended_geometric_opening' in codes
    assert 'unintended_ray_escape' in codes
    assert 'explicit_portal_opening' not in codes
    assert len(result.unintended_boundary_edges) == 3
    assert result.explicit_portal_boundary_edges == ()
    assert result.ray_escape_evidence[0].escaped_without_intersection is True
    assert result.ray_escape_evidence[0].positive_intersection_count == 0
    assert 'unintended_geometric_opening' in result.unresolved_conditions


def test_explicit_portal_matches_opening_without_auto_repair() -> None:
    revision, _ = _revision(remove_triangle_index=0)
    geometry_before = deepcopy(revision.document.r120_semantic_geometry.model_dump(mode='json'))
    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')
    surface_id = _surface_id(revision)
    portal_edges = (
        PortalBoundaryEdge(source_surface_id=surface_id, vertex_a=0, vertex_b=1),
        PortalBoundaryEdge(source_surface_id=surface_id, vertex_a=0, vertex_b=2),
        PortalBoundaryEdge(source_surface_id=surface_id, vertex_a=1, vertex_b=2),
    )
    portals = make_portal_authority(
        declaration_mode='explicit_list',
        declarations=(
            PortalDeclaration(
                portal_id='front-opening',
                region_ids=('room-air',),
                boundary_edges=portal_edges,
            ),
        ),
    )
    request = make_leak_portal_diagnostic_request(
        compiled,
        closed_boundary_expectation=True,
        sampling_authority=_sampling(),
    )

    result = diagnose_r120_leak_and_portals(compiled, request, portal_authority=portals)

    codes = {finding.code for finding in result.findings}
    assert 'explicit_portal_opening' in codes
    assert 'explicit_portal_ray_escape' in codes
    assert 'unintended_geometric_opening' not in codes
    assert result.unintended_boundary_edges == ()
    assert len(result.explicit_portal_boundary_edges) == 3
    assert result.portal_declaration_mismatch_edges == ()
    assert result.unresolved_conditions == ()
    assert revision.document.r120_semantic_geometry.model_dump(mode='json') == geometry_before


def test_portal_declaration_mismatch_is_fail_closed() -> None:
    revision, _ = _revision(remove_triangle_index=0)
    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')
    surface_id = _surface_id(revision)
    portals = make_portal_authority(
        declaration_mode='explicit_list',
        declarations=(
            PortalDeclaration(
                portal_id='mismatched-opening',
                region_ids=('room-air',),
                boundary_edges=(
                    PortalBoundaryEdge(
                        source_surface_id=surface_id,
                        vertex_a=0,
                        vertex_b=3,
                    ),
                ),
            ),
        ),
    )
    request = make_leak_portal_diagnostic_request(
        compiled,
        closed_boundary_expectation=True,
        sampling_authority=_sampling(),
    )

    result = diagnose_r120_leak_and_portals(compiled, request, portal_authority=portals)

    codes = {finding.code for finding in result.findings}
    assert 'portal_boundary_mismatch' in codes
    assert 'unintended_geometric_opening' in codes
    assert result.portal_declaration_mismatch_edges
    assert 'portal_boundary_mismatch' in result.unresolved_conditions


def test_approximation_policy_records_every_dropped_feature_and_preserves_surface_identity() -> None:
    revision, _ = _revision(TWO_TETRA_WITH_TINY_SECOND)
    geometry = revision.document.r120_semantic_geometry
    assert geometry is not None
    before = deepcopy(geometry.model_dump(mode='json'))

    compiled = _compile(
        revision,
        input_policy='diagnostic_compile_unresolved',
        tolerance=0.001,
        approximation_policy='explicit_policy_only',
        tiny_feature_policy='drop_below_tolerance',
    )

    assert len(compiled.approximation_operations) == 4
    assert len(compiled.dropped_features) == 4
    assert compiled.approximation_error_bound_m is None
    assert compiled.approximation_error_status == 'not_computed_for_dropped_features'
    assert compiled.maximum_dropped_feature_extent_m > 0.0
    assert all(item.tolerance_m == 0.001 for item in compiled.approximation_operations)
    assert compiled.surface_mapping[0].source_surface_id == geometry.surfaces[0].surface_id
    assert len(compiled.surface_mapping[0].source_triangle_ids) == 8
    assert len(compiled.surface_mapping[0].compiled_triangle_indices) == 4
    assert len(compiled.surface_mapping[0].dropped_source_triangle_ids) == 4
    assert geometry.model_dump(mode='json') == before


def test_same_input_and_settings_have_same_compiled_hash_and_round_trip_exactly() -> None:
    revision, _ = _revision()
    request_a = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    request_b = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled_a = compile_r120_geometry(revision, request_a)
    compiled_b = compile_r120_geometry(revision, request_b)

    assert request_a == request_b
    assert request_a.request_semantic_hash_sha256 == request_b.request_semantic_hash_sha256
    assert compiled_a.compiled_hash_sha256 == compiled_b.compiled_hash_sha256
    assert compiled_a.compiled_geometry_id == compiled_b.compiled_geometry_id
    assert compiled_a.topology_identity_sha256 == compiled_b.topology_identity_sha256

    reopened = deserialize_r120_compiled_geometry(serialize_r120_compiled_geometry(compiled_a))
    assert reopened == compiled_a
    assert reopened.compiled_hash_sha256 == compiled_a.compiled_hash_sha256
    assert reopened.exact_scene_revision_content_hash == revision.content_hash


def test_leak_diagnostic_round_trip_preserves_exact_compiled_input_hash() -> None:
    revision, _ = _revision(remove_triangle_index=0)
    compiled = _compile(revision, input_policy='diagnostic_compile_unresolved')
    request = make_leak_portal_diagnostic_request(
        compiled,
        closed_boundary_expectation=True,
        sampling_authority=_sampling(),
    )
    result = diagnose_r120_leak_and_portals(
        compiled,
        request,
        portal_authority=make_portal_authority(declaration_mode='explicit_none'),
    )

    reopened = deserialize_r120_leak_portal_diagnostic(
        serialize_r120_leak_portal_diagnostic(result)
    )

    assert reopened == result
    assert reopened.exact_compiled_geometry_id == compiled.compiled_geometry_id
    assert reopened.exact_compiled_geometry_hash_sha256 == compiled.compiled_hash_sha256
    assert reopened.request.sampling_authority == request.sampling_authority
    assert reopened.diagnostic_hash_sha256 == result.diagnostic_hash_sha256


def test_sqlite_save_reopen_preserves_exact_scene_and_diagnostic_hashes(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    template_revision, _ = _revision()
    saved_revision = scene_repository.save(
        template_revision.document,
        parent_revision_id=None,
    ).revision

    request = make_r120_geometry_compilation_request(
        saved_revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(saved_revision, request)
    portals = make_portal_authority(declaration_mode='explicit_none')
    diagnostic_request = make_leak_portal_diagnostic_request(
        compiled,
        closed_boundary_expectation=True,
        sampling_authority=_sampling(),
    )
    diagnostic = diagnose_r120_leak_and_portals(
        compiled,
        diagnostic_request,
        portal_authority=portals,
    )

    repository = R120GeometryCompilerRepository(scene_repository)
    repository.save_compiled_geometry(compiled)
    repository.save_leak_portal_diagnostic(diagnostic, portal_authority=portals)

    reopened_repository = R120GeometryCompilerRepository(
        SceneRepository(scene_repository.path)
    )
    reopened_compiled = reopened_repository.get_compiled_geometry(
        compiled.compiled_geometry_id
    )
    reopened_diagnostic = reopened_repository.get_leak_portal_diagnostic(
        diagnostic.diagnostic_result_id
    )

    assert reopened_compiled == compiled
    assert reopened_compiled is not None
    assert reopened_compiled.exact_scene_revision_id == saved_revision.revision_id
    assert reopened_compiled.exact_scene_revision_content_hash == saved_revision.content_hash
    assert reopened_compiled.exact_semantic_geometry_hash_sha256 == (
        saved_revision.document.r120_semantic_geometry.semantic_hash_sha256
    )
    assert reopened_diagnostic == diagnostic
    assert reopened_diagnostic is not None
    assert reopened_diagnostic.exact_compiled_geometry_hash_sha256 == (
        compiled.compiled_hash_sha256
    )


def _compile_authorities(
    revision: SceneRevision,
) -> tuple[
    tuple[SurfaceBoundaryAuthorityBinding, ...],
    AcousticRegionAuthority,
    PortalAuthority,
    BoundaryTerminationAuthority,
]:
    surface_id = _surface_id(revision)
    bindings = (
        SurfaceBoundaryAuthorityBinding(
            source_surface_id=surface_id,
            material_authority=_dummy_external_ref('fixture-material'),
            boundary_physics_authority=_dummy_external_ref(
                'fixture-boundary-physics'
            ),
        ),
    )
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    terminations = make_boundary_termination_authority(
        declaration_mode='explicit_none'
    )
    return bindings, region, portals, terminations


def _authority_fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _revision()[0].document,
        parent_revision_id=None,
    ).revision
    bindings, region, portals, terminations = _compile_authorities(revision)
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
        ),
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    repository = R120GeometryCompilerRepository(scene_repository)
    repository.save_compiled_geometry(
        compiled,
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    return {
        'scene_repository': scene_repository,
        'repository': repository,
        'revision': revision,
        'compiled': compiled,
        'bindings': bindings,
        'region': region,
        'portals': portals,
        'terminations': terminations,
    }


def _retamper_compiled(
    compiled: R120CompiledGeometry,
    **overrides: object,
) -> R120CompiledGeometry:
    """Rebuild a self-hash-valid compiled authority over tampered fields."""
    payload = compiled.model_dump(mode='json')
    payload.update(overrides)
    core = {
        key: value
        for key, value in payload.items()
        if key not in ('compiled_geometry_id', 'compiled_hash_sha256')
    }
    digest = _semantic_hash(core)
    payload['compiled_hash_sha256'] = digest
    payload['compiled_geometry_id'] = f'r120-compiled-geometry:{digest}'
    return R120CompiledGeometry.model_validate(payload)


def _retamper_diagnostic(
    diagnostic: R120LeakPortalDiagnostic,
    **overrides: object,
) -> R120LeakPortalDiagnostic:
    """Rebuild a self-hash-valid diagnostic over tampered fields."""
    payload = diagnostic.model_dump(mode='json')
    payload.update(overrides)
    core = {
        key: value
        for key, value in payload.items()
        if key not in ('diagnostic_result_id', 'diagnostic_hash_sha256')
    }
    digest = _semantic_hash(core)
    payload['diagnostic_hash_sha256'] = digest
    payload['diagnostic_result_id'] = f'r120-leak-portal-diagnostic:{digest}'
    return R120LeakPortalDiagnostic.model_validate(payload)


def test_repository_replays_compiled_geometry_from_retained_exact_authorities(
    tmp_path: Path,
) -> None:
    fixture = _authority_fixture(tmp_path)
    compiled = fixture['compiled']
    repository = fixture['repository']

    # Idempotent re-save resolves the retained inputs; authorities need not be
    # supplied twice.
    assert repository.save_compiled_geometry(compiled) == compiled

    reopened_repository = R120GeometryCompilerRepository(
        SceneRepository(fixture['scene_repository'].path)
    )
    reopened = reopened_repository.get_compiled_geometry(
        compiled.compiled_geometry_id
    )
    assert reopened == compiled
    assert reopened is not None
    assert reopened.region_authority_ref == fixture['compiled'].region_authority_ref
    assert reopened_repository.get_compiled_geometry_by_hash(
        compiled.compiled_hash_sha256
    ) == compiled


def test_self_hash_valid_tampered_compiled_geometry_is_rejected_at_save(
    tmp_path: Path,
) -> None:
    fixture = _authority_fixture(tmp_path)
    compiled = fixture['compiled']
    repository = fixture['repository']

    readiness = compiled.readiness.model_dump(mode='json')
    readiness['wave_geometry_ready'] = not readiness['wave_geometry_ready']
    forged_readiness = _retamper_compiled(compiled, readiness=readiness)
    try:
        repository.save_compiled_geometry(
            forged_readiness,
            surface_boundary_bindings=fixture['bindings'],
            region_authority=fixture['region'],
            portal_authority=fixture['portals'],
            boundary_termination_authority=fixture['terminations'],
        )
    except ValueError as exc:
        assert 'does not reproduce' in str(exc)
    else:
        raise AssertionError('tampered readiness flags must fail closed')

    bounding_volume = compiled.bounding_volume.model_dump(mode='json')
    bounding_volume['max_x_m'] = bounding_volume['max_x_m'] + 1.0
    forged_bounds = _retamper_compiled(
        compiled,
        bounding_volume=bounding_volume,
    )
    try:
        repository.save_compiled_geometry(
            forged_bounds,
            surface_boundary_bindings=fixture['bindings'],
            region_authority=fixture['region'],
            portal_authority=fixture['portals'],
            boundary_termination_authority=fixture['terminations'],
        )
    except ValueError as exc:
        assert 'does not reproduce' in str(exc)
    else:
        raise AssertionError('tampered bounding volume must fail closed')

    # Passthrough surface-mapping authorities are pinned to the retained exact
    # inputs: the same payload under the original bindings diverges.
    mapping = [
        dict(item)
        for item in compiled.model_dump(mode='json')['surface_mapping']
    ]
    mapping[0]['material_authority'] = _dummy_external_ref(
        'forged-material'
    ).model_dump(mode='json')
    forged_mapping = _retamper_compiled(compiled, surface_mapping=mapping)
    try:
        repository.save_compiled_geometry(
            forged_mapping,
            surface_boundary_bindings=fixture['bindings'],
            region_authority=fixture['region'],
            portal_authority=fixture['portals'],
            boundary_termination_authority=fixture['terminations'],
        )
    except ValueError as exc:
        assert 'does not reproduce' in str(exc)
    else:
        raise AssertionError('swapped material authority must fail closed')


def test_compiled_geometry_dangling_input_authority_fails_closed(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _revision()[0].document,
        parent_revision_id=None,
    ).revision
    bindings, region, portals, terminations = _compile_authorities(revision)
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
        ),
        surface_boundary_bindings=bindings,
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    repository = R120GeometryCompilerRepository(scene_repository)

    try:
        repository.save_compiled_geometry(compiled)
    except ValueError as exc:
        assert 'unretained acoustic region authority' in str(exc)
    else:
        raise AssertionError('dangling region authority must fail closed')

    other_region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='other-region',
                boundary_surface_ids=(_surface_id(revision),),
            ),
        )
    )
    try:
        repository.save_compiled_geometry(
            compiled,
            surface_boundary_bindings=bindings,
            region_authority=other_region,
            portal_authority=portals,
            boundary_termination_authority=terminations,
        )
    except ValueError as exc:
        assert 'acoustic region authority mismatch' in str(exc)
    else:
        raise AssertionError('mismatched region authority must fail closed')

    authority_free = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
        ),
    )
    try:
        repository.save_compiled_geometry(
            authority_free,
            region_authority=region,
        )
    except ValueError as exc:
        assert 'without a compiled ref' in str(exc)
    else:
        raise AssertionError('unreferenced authority must fail closed')


def test_tampered_persisted_compiled_inputs_fail_closed_on_read(
    tmp_path: Path,
) -> None:
    fixture = _authority_fixture(tmp_path)
    compiled = fixture['compiled']
    repository = fixture['repository']

    # A self-hash-valid forged payload with consistent index columns has no
    # retained inputs for the forged identity and fails closed.
    readiness = compiled.readiness.model_dump(mode='json')
    readiness['wave_geometry_ready'] = not readiness['wave_geometry_ready']
    forged = _retamper_compiled(compiled, readiness=readiness)
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            UPDATE cad_r120_compiled_geometry
            SET compiled_geometry_id=?,
                compiled_hash_sha256=?,
                payload_json=?
            WHERE compiled_geometry_id=?
            """,
            (
                forged.compiled_geometry_id,
                forged.compiled_hash_sha256,
                forged.model_dump_json(),
                compiled.compiled_geometry_id,
            ),
        )
        connection.commit()
    try:
        repository.get_compiled_geometry(forged.compiled_geometry_id)
    except ValueError:
        pass
    else:
        raise AssertionError('forged persisted payload must fail closed')


def test_tampered_retained_compile_inputs_fail_closed_on_read(
    tmp_path: Path,
) -> None:
    fixture = _authority_fixture(tmp_path)
    compiled = fixture['compiled']
    repository = fixture['repository']

    other_region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='forged-region',
                boundary_surface_ids=(
                    fixture['region'].declarations[0].boundary_surface_ids
                ),
            ),
        )
    )
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            """
            SELECT inputs_json
            FROM cad_r120_compile_inputs
            WHERE compiled_geometry_id=?
            """,
            (compiled.compiled_geometry_id,),
        ).fetchone()
        inputs = json.loads(row[0])
        inputs['region_authority'] = other_region.model_dump(mode='json')
        connection.execute(
            """
            UPDATE cad_r120_compile_inputs
            SET inputs_json=?
            WHERE compiled_geometry_id=?
            """,
            (json.dumps(inputs), compiled.compiled_geometry_id),
        )
        connection.commit()
    try:
        repository.get_compiled_geometry(compiled.compiled_geometry_id)
    except ValueError as exc:
        assert 'authority mismatch' in str(exc)
    else:
        raise AssertionError('swapped retained region authority must fail closed')

    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            """
            SELECT inputs_json
            FROM cad_r120_compile_inputs
            WHERE compiled_geometry_id=?
            """,
            (compiled.compiled_geometry_id,),
        ).fetchone()
        inputs = json.loads(row[0])
        inputs['region_authority'] = fixture['region'].model_dump(mode='json')
        inputs['surface_boundary_bindings'] = []
        connection.execute(
            """
            UPDATE cad_r120_compile_inputs
            SET inputs_json=?
            WHERE compiled_geometry_id=?
            """,
            (json.dumps(inputs), compiled.compiled_geometry_id),
        )
        connection.commit()
    try:
        repository.get_compiled_geometry(compiled.compiled_geometry_id)
    except ValueError as exc:
        assert 'does not reproduce' in str(exc)
    else:
        raise AssertionError('stripped retained bindings must fail closed')


def test_leak_portal_diagnostic_replays_from_retained_portal_authority(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _revision(remove_triangle_index=0)[0].document,
        parent_revision_id=None,
    ).revision
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
            input_policy='diagnostic_compile_unresolved',
        ),
    )
    repository = R120GeometryCompilerRepository(scene_repository)
    repository.save_compiled_geometry(compiled)

    portals = make_portal_authority(declaration_mode='explicit_none')
    diagnostic = diagnose_r120_leak_and_portals(
        compiled,
        make_leak_portal_diagnostic_request(
            compiled,
            closed_boundary_expectation=True,
            sampling_authority=_sampling(),
        ),
        portal_authority=portals,
    )

    # The diagnostic consumed a portal authority that is only referenced by
    # ref; persistence must receive the exact authority to retain it.
    try:
        repository.save_leak_portal_diagnostic(diagnostic)
    except ValueError as exc:
        assert 'unretained portal authority' in str(exc)
    else:
        raise AssertionError('dangling diagnostic portal authority must fail closed')

    repository.save_leak_portal_diagnostic(diagnostic, portal_authority=portals)

    # Idempotent re-save resolves the retained portal authority.
    assert repository.save_leak_portal_diagnostic(diagnostic) == diagnostic

    reopened_repository = R120GeometryCompilerRepository(
        SceneRepository(scene_repository.path)
    )
    reopened = reopened_repository.get_leak_portal_diagnostic(
        diagnostic.diagnostic_result_id
    )
    assert reopened == diagnostic
    assert reopened is not None
    assert reopened.portal_authority_ref == diagnostic.portal_authority_ref


def test_tampered_leak_portal_diagnostic_is_rejected(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _revision(remove_triangle_index=0)[0].document,
        parent_revision_id=None,
    ).revision
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
            input_policy='diagnostic_compile_unresolved',
        ),
    )
    repository = R120GeometryCompilerRepository(scene_repository)
    repository.save_compiled_geometry(compiled)

    portals = make_portal_authority(declaration_mode='explicit_none')
    diagnostic = diagnose_r120_leak_and_portals(
        compiled,
        make_leak_portal_diagnostic_request(
            compiled,
            closed_boundary_expectation=True,
            sampling_authority=_sampling(),
        ),
        portal_authority=portals,
    )

    # Fabricated findings under a recomputed self hash are rejected at save.
    forged_findings = _retamper_diagnostic(diagnostic, findings=())
    try:
        repository.save_leak_portal_diagnostic(
            forged_findings,
            portal_authority=portals,
        )
    except ValueError as exc:
        assert 'does not reproduce' in str(exc)
    else:
        raise AssertionError('fabricated diagnostic findings must fail closed')

    repository.save_leak_portal_diagnostic(diagnostic, portal_authority=portals)

    # Fabricated ray evidence in the persisted row fails closed on read: the
    # forged identity has no retained inputs.
    evidence = [
        dict(item)
        for item in diagnostic.model_dump(mode='json')['ray_escape_evidence']
    ]
    evidence[0]['escaped_without_intersection'] = not evidence[0][
        'escaped_without_intersection'
    ]
    forged_evidence = _retamper_diagnostic(
        diagnostic,
        ray_escape_evidence=evidence,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            UPDATE cad_r120_leak_portal_diagnostics
            SET diagnostic_result_id=?,
                diagnostic_hash_sha256=?,
                payload_json=?
            WHERE diagnostic_result_id=?
            """,
            (
                forged_evidence.diagnostic_result_id,
                forged_evidence.diagnostic_hash_sha256,
                forged_evidence.model_dump_json(),
                diagnostic.diagnostic_result_id,
            ),
        )
        connection.commit()
    try:
        repository.get_leak_portal_diagnostic(
            forged_evidence.diagnostic_result_id
        )
    except ValueError:
        pass
    else:
        raise AssertionError('forged persisted diagnostic must fail closed')


def test_diagnostic_without_portal_authority_rejects_supplied_authority(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _revision(remove_triangle_index=0)[0].document,
        parent_revision_id=None,
    ).revision
    compiled = compile_r120_geometry(
        revision,
        make_r120_geometry_compilation_request(
            revision,
            geometric_tolerance_m=1.0e-6,
            input_policy='diagnostic_compile_unresolved',
        ),
    )
    repository = R120GeometryCompilerRepository(scene_repository)
    repository.save_compiled_geometry(compiled)

    diagnostic = diagnose_r120_leak_and_portals(
        compiled,
        make_leak_portal_diagnostic_request(
            compiled,
            closed_boundary_expectation=True,
            sampling_authority=_sampling(),
        ),
    )
    assert diagnostic.portal_authority_ref is None
    repository.save_leak_portal_diagnostic(diagnostic)

    try:
        repository.save_leak_portal_diagnostic(
            diagnostic,
            portal_authority=make_portal_authority(
                declaration_mode='explicit_none'
            ),
        )
    except ValueError as exc:
        assert 'without a compiled ref' in str(exc)
    else:
        raise AssertionError('unreferenced diagnostic authority must fail closed')

    reopened_repository = R120GeometryCompilerRepository(
        SceneRepository(scene_repository.path)
    )
    assert (
        reopened_repository.get_leak_portal_diagnostic(
            diagnostic.diagnostic_result_id
        )
        == diagnostic
    )
