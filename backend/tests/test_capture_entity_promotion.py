"""Capture annotation -> SceneEntity promotion executor tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.capture_entity_promotion import (
    CaptureEntityPromotionError,
    CaptureEntityPromotionService,
    promoted_entity_id,
)
from htdt.capture_inbox import CaptureInboxRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository


DOCUMENT_ID = 'promotion-target'
ENTITY_ID = support.ANNOTATION_ID


def _entities_document(**overrides) -> dict:
    entities = json.loads(
        support.fixture_payloads()['annotations/entities.json']
    )
    entities['entities'][0].update(overrides)
    entities['schema_version'] = '1.3.0'
    entities['relations'] = []
    return entities


def _candidates_document(candidates: list[dict]) -> dict:
    return {
        'schema': 'htdt.capture.derived-geometry-candidates',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'candidates': candidates,
    }


def _candidate(candidate_id: str, geometry: dict) -> dict:
    return {
        'candidate_id': candidate_id,
        'record_kind': 'object',
        'resolution': 'resolved',
        'geometry': geometry,
        'shape_kind': (
            'oriented_rectangle'
            if 'orientedRectangle' in geometry
            else next(iter(geometry))
        ),
        'ambiguity_shape_kinds': [],
        'coordinate_space_id': support.SPACE_ID,
        'source_mode': 'fused',
        'source_evidence_refs': [],
        'derivation_algorithm': 'test_fitter',
        'derivation_version': '1',
        'fit_score': 0.9,
        'support_score': 0.9,
        'observation_start_seconds': 0.0,
        'observation_end_seconds': 1.0,
        'contour_points': [],
    }


def _derived_spec(document: dict) -> dict:
    from htdt.capture_bundle import canonical_payload_json_bytes

    return {
        'bytes': canonical_payload_json_bytes(document),
        'media_type': 'application/json',
        'producer': 'derived_analysis',
        'provenance_class': 'capture_app_derived',
        'role': 'derived',
        'source_refs': ['path:mesh/anchors.json'],
    }


def _services(tmp_path: Path, *, entities_overrides: dict | None = None,
              extra_specs: dict[str, dict] | None = None):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    scene.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.6),
            entities=(),
        ),
        parent_revision_id=None,
    )
    ingestion = CaptureIngestionRepository(scene)
    files = support.default_file_specs()
    if extra_specs:
        files.update(extra_specs)
    plan, payloads, manifest = support.plan_and_payloads(
        tmp_path,
        files=files,
        json_overrides=(
            {'annotations/entities.json': _entities_document(
                **(entities_overrides or {}))}
            if entities_overrides is not None
            else None
        ),
    )
    result = ingestion.ingest(plan, payloads, manifest=manifest)
    service = CaptureEntityPromotionService(ingestion, scene)
    return service, scene, ingestion, result.lineage_digest


def test_speaker_annotation_materializes(tmp_path: Path) -> None:
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
        },
    )
    outcome = service.promote_annotations(
        ingestion_plan(lineage, tmp_path), DOCUMENT_ID
    )
    document = scene.current_head(DOCUMENT_ID).document
    assert len(document.entities) == 1
    entity = document.entities[0]
    assert entity.entity_id == outcome.promoted_entity_ids[0]
    assert entity.kind == 'speaker'
    assert entity.speaker_role == 'L'
    assert entity.name == 'Left speaker'
    # T_world translation column (1.0, 0.0, 2.0), identity rotation.
    assert entity.position.x_m == pytest.approx(1.0)
    assert entity.position.y_m == pytest.approx(0.0)
    assert entity.position.z_m == pytest.approx(2.0)
    assert entity.size_m is not None
    assert entity.size_m.x_m == pytest.approx(0.3)
    assert entity.size_m.y_m == pytest.approx(0.25)
    assert entity.size_m.z_m == pytest.approx(0.5)


def test_promotion_is_idempotent(tmp_path: Path) -> None:
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
        },
    )
    plan = ingestion_plan(lineage, tmp_path)
    first = service.promote_annotations(plan, DOCUMENT_ID)
    second = service.promote_annotations(plan, DOCUMENT_ID)
    assert second.promoted_entity_ids == ()
    assert second.reused_existing_entity_ids == first.promoted_entity_ids
    assert second.scene_revision_id == first.scene_revision_id
    assert len(scene.current_head(DOCUMENT_ID).document.entities) == 1


def test_centered_circle_candidate_becomes_cylinder(tmp_path: Path) -> None:
    candidate_id = '30000000-0000-4000-8000-000000000001'
    # entity origin is (1.0, 0.0, 2.0); the candidate center is the same
    # plan point so the cylinder is centered on the entity.
    candidate = _candidate(
        candidate_id,
        {'circle': {'center': {'x': 1.0, 'y': 0.0}, 'radius': 0.15}},
    )
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.3,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'evidence_refs': [f'derived_candidate:{candidate_id}'],
        },
        extra_specs={
            'derived/geometry-candidates.json': _derived_spec(
                _candidates_document([candidate])
            ),
        },
    )
    service.promote_annotations(ingestion_plan(lineage, tmp_path), DOCUMENT_ID)
    entity = scene.current_head(DOCUMENT_ID).document.entities[0]
    assert entity.body_geometry is not None
    assert entity.body_geometry.kind == 'cylinder'
    assert entity.body_geometry.radius_m == pytest.approx(0.15)


def test_offcenter_candidate_becomes_extruded_polygon(tmp_path: Path) -> None:
    candidate_id = '30000000-0000-4000-8000-000000000002'
    # Center offset +0.2 x from the entity origin — the body keeps its true
    # plan position as a sampled polygon instead of a fake centered prism.
    candidate = _candidate(
        candidate_id,
        {'circle': {'center': {'x': 1.2, 'y': 0.0}, 'radius': 0.15}},
    )
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.9, 'height_m': 0.5, 'depth_m': 0.9,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'evidence_refs': [f'derived_candidate:{candidate_id}'],
        },
        extra_specs={
            'derived/geometry-candidates.json': _derived_spec(
                _candidates_document([candidate])
            ),
        },
    )
    service.promote_annotations(ingestion_plan(lineage, tmp_path), DOCUMENT_ID)
    entity = scene.current_head(DOCUMENT_ID).document.entities[0]
    assert entity.body_geometry is not None
    assert entity.body_geometry.kind == 'extruded_polygon'
    vertices = entity.body_geometry.footprint_vertices
    assert vertices is not None and len(vertices) >= 8
    cx = sum(v.x_m for v in vertices) / len(vertices)
    assert cx == pytest.approx(0.2, abs=0.02)


def test_oriented_rectangle_candidate_becomes_polygon(tmp_path: Path) -> None:
    candidate_id = '30000000-0000-4000-8000-000000000003'
    candidate = _candidate(
        candidate_id,
        {
            'orientedRectangle': {
                'center': {'x': 1.0, 'y': 0.0},
                'width': 0.4,
                'depth': 0.2,
                'headingRadians': 0.0,
            }
        },
    )
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.4, 'height_m': 0.5, 'depth_m': 0.2,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'evidence_refs': [f'derived_candidate:{candidate_id}'],
        },
        extra_specs={
            'derived/geometry-candidates.json': _derived_spec(
                _candidates_document([candidate])
            ),
        },
    )
    service.promote_annotations(ingestion_plan(lineage, tmp_path), DOCUMENT_ID)
    entity = scene.current_head(DOCUMENT_ID).document.entities[0]
    assert entity.body_geometry is not None
    assert entity.body_geometry.kind == 'extruded_polygon'
    vertices = entity.body_geometry.footprint_vertices
    assert vertices is not None and len(vertices) == 4
    xs = sorted(v.x_m for v in vertices)
    assert xs[0] == pytest.approx(-0.2) and xs[-1] == pytest.approx(0.2)


def test_missing_envelope_blocks_physical_entity(tmp_path: Path) -> None:
    # The vendored speaker record has no physical_envelope — the executor
    # refuses to fabricate dimensions.
    service, _scene, _ingestion, lineage = _services(tmp_path)
    with pytest.raises(CaptureEntityPromotionError, match='physical'):
        service.promote_annotations(
            ingestion_plan(lineage, tmp_path), DOCUMENT_ID
        )


def test_malformed_transform_blocks(tmp_path: Path) -> None:
    service, _scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'T_world_from_annotation': {
                'representation': 'column_major_4x4_f32',
                'values': [1.0] * 16,
            },
        },
    )
    with pytest.raises(CaptureEntityPromotionError, match='transform'):
        service.promote_annotations(
            ingestion_plan(lineage, tmp_path), DOCUMENT_ID
        )


def test_projective_transform_rejected(tmp_path: Path) -> None:
    # Row-major projective matrix: upper 3x3 identity, last row (0.1, 0, 0, 1).
    # Column-major flat values as declared by matrix4f.
    service, _scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'T_world_from_annotation': {
                'representation': 'column_major_4x4_f32',
                'values': [
                    1.0, 0.0, 0.0, 0.1,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    1.0, 2.0, 3.0, 1.0,
                ],
            },
        },
    )
    with pytest.raises(CaptureEntityPromotionError, match='not affine'):
        service.promote_annotations(
            ingestion_plan(lineage, tmp_path), DOCUMENT_ID
        )


def test_shear_transform_rejected(tmp_path: Path) -> None:
    # Equal column norms and positive determinant but non-orthogonal columns:
    # rows [1 s 0; s 1 0; 0 0 sqrt(1+s^2)] with s=0.1 (issue #545 example).
    s = 0.1
    z = (1.0 + s * s) ** 0.5
    service, _scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'T_world_from_annotation': {
                'representation': 'column_major_4x4_f32',
                'values': [
                    1.0, s, 0.0, 0.0,
                    s, 1.0, 0.0, 0.0,
                    0.0, 0.0, z, 0.0,
                    0.0, 0.0, 0.0, 1.0,
                ],
            },
        },
    )
    with pytest.raises(CaptureEntityPromotionError, match='rigid/similarity'):
        service.promote_annotations(
            ingestion_plan(lineage, tmp_path), DOCUMENT_ID
        )


def test_rotated_scaled_transform_promotes(tmp_path: Path) -> None:
    # 90-degree rotation about z with uniform scale 2 and translation
    # (1, 0, 2): orthogonal columns must not be rejected as shear.
    service, scene, _ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
            'T_world_from_annotation': {
                'representation': 'column_major_4x4_f32',
                'values': [
                    0.0, 2.0, 0.0, 0.0,
                    -2.0, 0.0, 0.0, 0.0,
                    0.0, 0.0, 2.0, 0.0,
                    1.0, 0.0, 2.0, 1.0,
                ],
            },
        },
    )
    outcome = service.promote_annotations(
        ingestion_plan(lineage, tmp_path), DOCUMENT_ID
    )
    assert len(outcome.promoted_entity_ids) == 1
    entity = scene.current_head(DOCUMENT_ID).document.entities[0]
    assert entity.position.x_m == pytest.approx(1.0)
    assert entity.position.z_m == pytest.approx(2.0)


def test_inbox_promote_executes_annotations(tmp_path: Path) -> None:
    service, scene, ingestion, lineage = _services(
        tmp_path,
        entities_overrides={
            'physical_envelope': {
                'width_m': 0.3, 'height_m': 0.5, 'depth_m': 0.25,
                'provenance': 'user_measured',
                'source_evidence_refs': [],
            },
        },
    )
    inbox = CaptureInboxRepository(scene, ingestion)
    plan = ingestion.get_ingestion(lineage)
    staged = inbox.stage(plan, arrival_source='test')
    inbox.assign_scope(lineage, DOCUMENT_ID)
    records = inbox.promote(
        lineage,
        ('annotations',),
        reason='promote capture entities',
        executor=service.promotion_executor(DOCUMENT_ID),
    )
    assert [r.outcome for r in records] == ['promoted']
    assert records[0].created_authority_id.startswith('scene-entities:')
    assert len(scene.current_head(DOCUMENT_ID).document.entities) == 1
    inspection = inbox.inspect(lineage)
    assert inspection is not None
    assert 'annotations' in inspection.promoted_authority_kinds


def test_executor_rejects_other_kinds(tmp_path: Path) -> None:
    service, _scene, ingestion, lineage = _services(tmp_path)
    plan = ingestion.get_ingestion(lineage)
    executor = service.promotion_executor(DOCUMENT_ID)
    with pytest.raises(CaptureEntityPromotionError):
        executor(plan, 'measurements')


def test_unassigned_document_has_no_head(tmp_path: Path) -> None:
    service, _scene, ingestion, lineage = _services(tmp_path)
    plan = ingestion.get_ingestion(lineage)
    with pytest.raises(CaptureEntityPromotionError, match='no scene revision'):
        service.promote_annotations(plan, 'nonexistent-document')


def ingestion_plan(lineage_digest: str, tmp_path: Path):
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan = repository.get_ingestion(lineage_digest)
    assert plan is not None
    return plan
