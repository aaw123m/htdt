from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_acoustic_treatment import (
    AcousticTreatmentPlacement,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
    semantic_surface_host_authority_sha256,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_scene import Position3, SceneDocument
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    RemoveTriangleRepair,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


def _canonical_digest(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _definition():
    dimensions = TreatmentDimensions(
        width_m=0.6,
        height_m=1.2,
        thickness_m=0.1,
    )
    evidence = build_treatment_evidence_authority(
        source_kind='user_defined',
        source_id='surface-binding-fixture',
        source_version='1',
        reference='Issue #171 x #167 exact binding fixture',
        extraction_id='manual-declaration',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='surface-bound-porous',
            definition_version='1.0',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.0,
            layers=(),
        ),
    )
    definition = build_acoustic_treatment_definition(
        definition_id='surface-bound-porous',
        version='1.0',
        name='Surface-bound porous fixture',
        treatment_type='porous_absorber',
        provenance=evidence.as_provenance(),
        dimensions=dimensions,
        layers=(),
    )
    return definition, (evidence,)


def _mesh():
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
    return import_raw_visual_mesh(asset, source_name='treatment-semantic-tetra.obj')


def _transform():
    return explicit_identity_source_to_scene_transform(
        reason='test fixture coordinates are explicitly HTDT scene metres',
    )


def _convert(
    mesh,
    *,
    source_scene_revision_id: str,
    assignments: tuple[SurfaceSemanticAssignment, ...],
    repairs: tuple[RemoveTriangleRepair, ...] = (),
):
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=source_scene_revision_id,
        source_to_scene_transform=_transform(),
        repairs=repairs,
        surface_assignments=assignments,
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _save_geometry_revision(
    repository: SceneRepository,
    parent,
    geometry,
):
    payload = parent.document.model_dump(mode='json')
    payload['schema_version'] = max(4, int(payload.get('schema_version', 1)))
    payload['r120_semantic_geometry'] = geometry.model_dump(mode='json')
    document = SceneDocument.model_validate(payload)
    # Alternative geometry outcomes for the same parent are deliberate
    # non-head lineage: the R120 binding rule requires the parent to equal the
    # geometry's source revision, so siblings off one parent are intended.
    return repository.save_detached_revision(
        document,
        parent_revision_id=parent.revision_id,
        reason='alternative_geometry_outcome',
    ).revision


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    root = scene_repository.save(
        SceneDocument(
            document_id='semantic-treatment-fixture',
            room=None,
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    mesh = _mesh()
    triangle_ids = raw_triangle_ids(mesh)
    geometry = _convert(
        mesh,
        source_scene_revision_id=root.revision_id,
        assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-panel-host',
                triangle_ids=(triangle_ids[0], triangle_ids[1]),
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key='object-panel-host',
                triangle_ids=(triangle_ids[2],),
                semantic_class='object_surface',
            ),
        ),
    )
    semantic_revision = _save_geometry_revision(scene_repository, root, geometry)
    variant_repository = CadSystemVariantRepository(scene_repository)
    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        variant_repository,
    )
    definition, evidence = _definition()
    for item in evidence:
        treatment_repository.save_evidence(item)
    definition = treatment_repository.save_definition(definition)
    return (
        scene_repository,
        treatment_repository,
        definition,
        root,
        semantic_revision,
        mesh,
        triangle_ids,
        geometry,
    )


def _surface(geometry, key: str):
    return next(surface for surface in geometry.surfaces if surface.surface_key == key)


def _placement(definition, revision, *, instance_id: str, surface_id: str):
    return build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id=instance_id,
        position=Position3(x_m=0.1, y_m=0.2, z_m=0.3),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        host_surface_id=surface_id,
    )


def test_exact_room_object_and_unknown_surface_binding_and_reopen(tmp_path: Path) -> None:
    (
        scene_repository,
        repository,
        definition,
        _root,
        revision,
        _mesh_value,
        _triangle_ids,
        geometry,
    ) = _fixture(tmp_path)

    room_surface = _surface(geometry, 'room-panel-host')
    object_surface = _surface(geometry, 'object-panel-host')
    unknown_surface = _surface(geometry, '__unassigned__')

    room = _placement(
        definition,
        revision,
        instance_id='room-panel-01',
        surface_id=room_surface.surface_id,
    )
    obj = _placement(
        definition,
        revision,
        instance_id='object-panel-01',
        surface_id=object_surface.surface_id,
    )
    unknown = _placement(
        definition,
        revision,
        instance_id='unknown-panel-01',
        surface_id=unknown_surface.surface_id,
    )

    assert room.host_surface_authority_sha256 == semantic_surface_host_authority_sha256(
        room_surface
    )
    assert obj.host_surface_authority_sha256 == semantic_surface_host_authority_sha256(
        object_surface
    )
    assert unknown.host_surface_authority_sha256 == semantic_surface_host_authority_sha256(
        unknown_surface
    )

    for placement in (room, obj, unknown):
        repository.save_placement(placement)

    room_result = repository.evaluate_placement_surface_binding(room)
    object_result = repository.evaluate_placement_surface_binding(obj)
    unknown_result = repository.evaluate_placement_surface_binding(unknown)

    assert room_result.binding_state == 'exact'
    assert room_result.placement_authority_valid is True
    assert room_result.actual_host_surface_semantic_class == 'room_boundary'
    assert room_result.host_surface_semantics_known is True
    assert room_result.solver_prediction_readiness == 'UNKNOWN'

    assert object_result.binding_state == 'exact'
    assert object_result.placement_authority_valid is True
    assert object_result.actual_host_surface_semantic_class == 'object_surface'

    assert unknown_result.binding_state == 'exact'
    assert unknown_result.placement_authority_valid is True
    assert unknown_result.actual_host_surface_semantic_class == 'unknown'
    assert unknown_result.host_surface_semantics_known is False
    assert unknown_result.solver_prediction_readiness == 'UNKNOWN'
    assert unknown_result.host_semantic_policy == 'semantic_class_does_not_gate_placement_authority'

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened = CadAcousticTreatmentRepository(reopened_scene, reopened_variants)
    reopened_room = reopened.get_placement(room.instance_id, room.placement_version)
    assert reopened_room == room
    reopened_result = reopened.evaluate_placement_surface_binding(reopened_room)
    assert reopened_result == room_result
    assert reopened_result.evaluation_sha256 == room_result.evaluation_sha256


def test_nonexistent_surface_and_hash_mismatch_fail_closed(tmp_path: Path) -> None:
    (
        _scene_repository,
        repository,
        definition,
        _root,
        revision,
        _mesh_value,
        _triangle_ids,
        geometry,
    ) = _fixture(tmp_path)
    room_surface = _surface(geometry, 'room-panel-host')

    with pytest.raises(ValueError, match='does not exist'):
        _placement(
            definition,
            revision,
            instance_id='missing-panel',
            surface_id='semantic-surface:' + ('0' * 64),
        )

    with pytest.raises(ValueError, match='authority hash mismatch'):
        build_treatment_placement(
            definition=definition,
            revision=revision,
            instance_id='wrong-hash-panel',
            position=Position3(x_m=0.1, y_m=0.2, z_m=0.3),
            coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
            host_surface_id=room_surface.surface_id,
            host_surface_authority_sha256='0' * 64,
        )

    valid = _placement(
        definition,
        revision,
        instance_id='repository-rejects-wrong-hash',
        surface_id=room_surface.surface_id,
    )
    payload = valid.model_dump(mode='python')
    identity = valid.identity_payload()
    identity['host_surface_authority_sha256'] = 'f' * 64
    payload['host_surface_authority_sha256'] = 'f' * 64
    payload['placement_sha256'] = _canonical_digest(identity)
    forged = AcousticTreatmentPlacement.model_validate(payload)

    with pytest.raises(ValueError, match='surface_authority_mismatch'):
        repository.save_placement(forged)


def test_wrong_scene_revision_is_distinct_from_surface_identity(tmp_path: Path) -> None:
    (
        scene_repository,
        repository,
        definition,
        _root,
        revision,
        _mesh_value,
        _triangle_ids,
        geometry,
    ) = _fixture(tmp_path)
    room_surface = _surface(geometry, 'room-panel-host')
    placement = _placement(
        definition,
        revision,
        instance_id='wrong-scene-check',
        surface_id=room_surface.surface_id,
    )
    repository.save_placement(placement)

    other_revision = scene_repository.save(
        SceneDocument(
            document_id='other-scene-document',
            room=None,
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    result = repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=other_revision.revision_id,
    )

    assert result.bound_authority_valid is True
    assert result.placement_authority_valid is False
    assert result.binding_state == 'wrong_scene_revision'
    assert result.evaluated_scene_revision_id == other_revision.revision_id


def test_repair_tracks_stable_surface_but_exact_binding_becomes_stale(tmp_path: Path) -> None:
    (
        _scene_repository,
        repository,
        definition,
        _root,
        revision,
        mesh,
        triangle_ids,
        geometry,
    ) = _fixture(tmp_path)
    room_surface = _surface(geometry, 'room-panel-host')
    placement = _placement(
        definition,
        revision,
        instance_id='stable-repair-panel',
        surface_id=room_surface.surface_id,
    )
    repository.save_placement(placement)

    repaired_geometry = _convert(
        mesh,
        source_scene_revision_id=revision.revision_id,
        repairs=(
            RemoveTriangleRepair(
                triangle_id=triangle_ids[3],
                reason='repair an unrelated triangle while preserving the host surface',
            ),
        ),
        assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-panel-host',
                triangle_ids=(triangle_ids[0], triangle_ids[1]),
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key='object-panel-host',
                triangle_ids=(triangle_ids[2],),
                semantic_class='object_surface',
            ),
        ),
    )
    repaired_revision = _save_geometry_revision(
        repository.scene_repository,
        revision,
        repaired_geometry,
    )
    repaired_room_surface = _surface(repaired_geometry, 'room-panel-host')

    assert repaired_room_surface.surface_id == room_surface.surface_id
    assert semantic_surface_host_authority_sha256(repaired_room_surface) == (
        placement.host_surface_authority_sha256
    )

    result = repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=repaired_revision.revision_id,
    )
    assert result.binding_state == 'stale_semantic_geometry'
    assert result.bound_authority_valid is True
    assert result.placement_authority_valid is False
    assert result.host_surface_lifecycle == 'stable_same_authority'
    assert result.bound_semantic_geometry_sha256 == geometry.semantic_hash_sha256
    assert result.evaluated_semantic_geometry_sha256 == repaired_geometry.semantic_hash_sha256
    assert result.bound_semantic_geometry_sha256 != result.evaluated_semantic_geometry_sha256

    rebound = revise_treatment_placement(
        placement,
        revision=repaired_revision,
    )
    repository.save_placement(rebound)
    rebound_result = repository.evaluate_placement_surface_binding(rebound)
    assert rebound_result.binding_state == 'exact'
    assert rebound_result.placement_authority_valid is True
    assert rebound.host_surface_id == placement.host_surface_id
    assert rebound.host_surface_authority_sha256 == placement.host_surface_authority_sha256


def test_repair_changed_authority_and_surface_removal_are_detected(tmp_path: Path) -> None:
    (
        _scene_repository,
        repository,
        definition,
        _root,
        revision,
        mesh,
        triangle_ids,
        geometry,
    ) = _fixture(tmp_path)
    room_surface = _surface(geometry, 'room-panel-host')
    placement = _placement(
        definition,
        revision,
        instance_id='changed-repair-panel',
        surface_id=room_surface.surface_id,
    )
    repository.save_placement(placement)

    changed_geometry = _convert(
        mesh,
        source_scene_revision_id=revision.revision_id,
        repairs=(
            RemoveTriangleRepair(
                triangle_id=triangle_ids[1],
                reason='repair removes one triangle that belonged to the host surface',
            ),
        ),
        assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-panel-host',
                triangle_ids=(triangle_ids[0],),
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key='object-panel-host',
                triangle_ids=(triangle_ids[2], triangle_ids[3]),
                semantic_class='object_surface',
            ),
        ),
    )
    changed_revision = _save_geometry_revision(
        repository.scene_repository,
        revision,
        changed_geometry,
    )
    changed_room_surface = _surface(changed_geometry, 'room-panel-host')

    assert changed_room_surface.surface_id == room_surface.surface_id
    assert semantic_surface_host_authority_sha256(changed_room_surface) != (
        placement.host_surface_authority_sha256
    )

    changed_result = repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=changed_revision.revision_id,
    )
    assert changed_result.binding_state == 'stale_semantic_geometry'
    assert changed_result.host_surface_lifecycle == 'stable_authority_changed'
    assert changed_result.placement_authority_valid is False

    removed_geometry = _convert(
        mesh,
        source_scene_revision_id=revision.revision_id,
        assignments=(
            SurfaceSemanticAssignment(
                surface_key='object-panel-host',
                triangle_ids=triangle_ids,
                semantic_class='object_surface',
            ),
        ),
    )
    removed_revision = _save_geometry_revision(
        repository.scene_repository,
        revision,
        removed_geometry,
    )
    removed_result = repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=removed_revision.revision_id,
    )
    assert removed_result.binding_state == 'stale_semantic_geometry'
    assert removed_result.host_surface_lifecycle == 'removed'
    assert removed_result.placement_authority_valid is False
