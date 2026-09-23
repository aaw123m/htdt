from pathlib import Path
import sqlite3

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument, make_empty_scene
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    RemoveTriangleRepair,
    SemanticCoordinateTransform,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    deserialize_semantic_acoustic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
    serialize_semantic_acoustic_geometry,
)



def _identity_transform():
    return explicit_identity_source_to_scene_transform(
        reason='fixture coordinates are explicitly declared to be HTDT metres',
    )


def _imperfect_mesh():
    asset = (Path(__file__).parent / 'fixtures' / 'imperfect_room.obj').read_bytes()
    return asset, import_raw_visual_mesh(asset, source_name='imperfect_room.obj')


def _closed_tetra_mesh():
    asset = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
'''
    return import_raw_visual_mesh(asset, source_name='closed.obj')


def test_imperfect_mesh_explicit_repair_keeps_raw_immutable_and_lineage_deterministic() -> None:
    asset, mesh = _imperfect_mesh()
    before_snapshot = mesh.model_dump(mode='json')
    triangle_ids = raw_triangle_ids(mesh)
    removed = triangle_ids[2]
    retained = tuple(triangle_id for triangle_id in triangle_ids if triangle_id != removed)
    repair = RemoveTriangleRepair(
        triangle_id=removed,
        reason='explicitly remove the duplicate copy identified by the import diagnostic',
    )
    assignment = SurfaceSemanticAssignment(
        surface_key='captured-object-surfaces',
        triangle_ids=retained,
        semantic_class='object_surface',
    )

    before_repair_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id='scene-revision-parent',
        source_to_scene_transform=_identity_transform(),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='captured-object-surfaces',
                triangle_ids=triangle_ids,
                semantic_class='object_surface',
            ),
        ),
    )
    before_repair = convert_raw_visual_mesh_to_semantic_geometry(mesh, before_repair_request)

    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id='scene-revision-parent',
        source_to_scene_transform=_identity_transform(),
        repairs=(repair,),
        surface_assignments=(assignment,),
    )
    first = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)
    repeated = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)

    assert mesh.model_dump(mode='json') == before_snapshot
    assert mesh.original_asset_bytes() == asset
    assert first == repeated
    assert first.geometry_id == repeated.geometry_id
    assert first.semantic_hash() == repeated.semantic_hash()
    assert first.surfaces[0].surface_id == before_repair.surfaces[0].surface_id
    assert len(first.repair_lineage) == 1
    assert first.repair_lineage[0].input_geometry_hash == first.lineage_root_geometry_hash
    assert first.repair_lineage[0].output_geometry_hash == first.derived_geometry_hash
    assert first.repair_lineage[0].provenance == 'explicit_guided_repair'
    assert first.geometry_compiler_readiness == 'blocked_by_geometry'
    assert first.solver_ready is False
    assert first.unresolved_conditions
    assert all(not condition.startswith('surface_semantics:') for condition in first.unresolved_conditions)
    assert first.material_assignment_status == 'not_part_of_this_authority'
    assert first.acoustic_regions_status == 'not_inferred'
    assert first.portals_status == 'not_inferred'
    assert first.boundary_terminations_status == 'not_inferred'



def test_source_to_scene_transform_is_explicit_and_part_of_semantic_identity() -> None:
    mesh = _closed_tetra_mesh()
    identity_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=_identity_transform(),
    )
    identity = convert_raw_visual_mesh_to_semantic_geometry(mesh, identity_request)

    transformed_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=SemanticCoordinateTransform(
            matrix_source_to_scene_m=(
                (2.0, 0.0, 0.0, 1.0),
                (0.0, 3.0, 0.0, -2.0),
                (0.0, 0.0, 4.0, 0.5),
                (0.0, 0.0, 0.0, 1.0),
            ),
            provenance='explicit_user_authority',
            reason='fixture exercises explicit scale and translation into HTDT metres',
        ),
    )
    transformed = convert_raw_visual_mesh_to_semantic_geometry(mesh, transformed_request)

    assert transformed.vertices[0].model_dump() == {'x_m': 1.0, 'y_m': -2.0, 'z_m': 0.5}
    assert transformed.vertices[1].model_dump() == {'x_m': 3.0, 'y_m': -2.0, 'z_m': 0.5}
    assert transformed.conversion_request_id != identity.conversion_request_id
    assert transformed.derived_geometry_hash != identity.derived_geometry_hash
    assert transformed.geometry_id != identity.geometry_id


def test_clean_geometry_without_explicit_semantics_is_not_promoted() -> None:
    mesh = _closed_tetra_mesh()
    unassigned_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=_identity_transform(),
    )
    unassigned = convert_raw_visual_mesh_to_semantic_geometry(mesh, unassigned_request)

    assert all(finding.state == 'pass' for finding in unassigned.conversion_findings)
    assert unassigned.geometry_compiler_readiness == 'blocked_by_surface_semantics'
    assert unassigned.solver_ready is False
    assert any(surface.semantic_class == 'unknown' for surface in unassigned.surfaces)
    assert any(
        condition.startswith('surface_semantics:')
        for condition in unassigned.unresolved_conditions
    )

    explicit_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=_identity_transform(),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    explicit = convert_raw_visual_mesh_to_semantic_geometry(mesh, explicit_request)

    assert explicit.geometry_compiler_readiness == 'ready_for_r120_geometry_compiler_contract'
    assert explicit.solver_ready is False
    assert explicit.unresolved_conditions == ()
    assert len(explicit.surfaces) == 1
    assert explicit.surfaces[0].semantic_class == 'room_boundary'
    assert explicit.surfaces[0].surface_id.startswith('semantic-surface:')

    reordered_request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=_identity_transform(),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=tuple(reversed(raw_triangle_ids(mesh))),
                semantic_class='room_boundary',
            ),
        ),
    )
    reordered = convert_raw_visual_mesh_to_semantic_geometry(mesh, reordered_request)
    assert reordered.surfaces[0].surface_id == explicit.surfaces[0].surface_id
    assert reordered.surfaces[0].triangle_ids == explicit.surfaces[0].triangle_ids


def test_semantic_geometry_round_trip_preserves_exact_identity() -> None:
    mesh = _closed_tetra_mesh()
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=_identity_transform(),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)
    reopened = deserialize_semantic_acoustic_geometry(
        serialize_semantic_acoustic_geometry(geometry)
    )

    assert reopened == geometry
    assert reopened.conversion_request == request
    assert reopened.geometry_id == geometry.geometry_id
    assert reopened.semantic_hash() == geometry.semantic_hash()
    assert reopened.surfaces[0].surface_id == geometry.surfaces[0].surface_id


def test_scene_revision_binding_save_and_reopen_tracks_same_semantic_result(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    base = repository.save(make_empty_scene('semantic-import-fixture'), parent_revision_id=None).revision

    _, mesh = _imperfect_mesh()
    triangle_ids = raw_triangle_ids(mesh)
    removed = triangle_ids[2]
    retained = tuple(triangle_id for triangle_id in triangle_ids if triangle_id != removed)
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=base.revision_id,
        source_to_scene_transform=_identity_transform(),
        repairs=(
            RemoveTriangleRepair(
                triangle_id=removed,
                reason='explicit duplicate-face repair for the representative import fixture',
            ),
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='captured-object-surfaces',
                triangle_ids=retained,
                semantic_class='object_surface',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)

    payload = base.document.model_dump(mode='json')
    payload['schema_version'] = 4
    payload['r120_semantic_geometry'] = geometry.model_dump(mode='json')
    document = SceneDocument.model_validate(payload)
    saved = repository.save(document, parent_revision_id=base.revision_id).revision

    binding = repository.semantic_geometry_binding(saved.revision_id)
    assert binding is not None
    assert binding.scene_revision_id == saved.revision_id
    assert binding.source_scene_revision_id == base.revision_id
    assert binding.geometry_id == geometry.geometry_id
    assert binding.geometry_semantic_hash == geometry.semantic_hash()
    assert binding.input_raw_mesh_id == mesh.mesh_id
    assert binding.input_asset_sha256 == mesh.provenance.original_asset_sha256
    assert binding.conversion_request_id == request.request_id

    reopened = repository.get(saved.revision_id)
    assert reopened is not None
    assert reopened.document.r120_semantic_geometry == geometry
    assert reopened.document.r120_semantic_geometry.conversion_request == request
    assert reopened.document.r120_semantic_geometry.semantic_hash() == geometry.semantic_hash()
    assert repository.latest(document.document_id).revision_id == saved.revision_id

    with sqlite3.connect(repository.path) as connection:
        duplicate_binding_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='cad_r120_semantic_geometry_bindings'"
        ).fetchone()
    assert duplicate_binding_table is None
