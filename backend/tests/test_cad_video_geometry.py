from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_video_geometry import (
    PROJECTOR_SPEC_EVIDENCED_FIELDS,
    AngleRange,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecification,
    ProjectorSpecificationEvidence,
    ProjectorSpecificationProvenance,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryEvaluation,
    VideoGeometryPolicy,
    VideoGeometryRequest,
    _digest,
    build_projector_spec_document_evidence,
    build_projector_spec_field_assertions,
    build_projector_spec_manual_evidence,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
    projector_spec_optical_values,
)
from htdt.cad_video_geometry_repository import CadVideoGeometryRepository


DOCUMENT_ID = 'video-geometry-fixture'
NOW = '2026-09-19T07:30:00+00:00'

SPEC_SOURCE_FILENAME = 'example-projector-p-spec.json'
SPEC_SOURCE_MEDIA_TYPE = 'application/json'


def _screen() -> SceneEntity:
    return SceneEntity(
        entity_id='screen-main',
        kind='screen',
        name='Main Screen',
        position=Position3(x_m=3.0, y_m=0.2, z_m=1.5),
        size_m=Size3(x_m=3.2, y_m=0.1, z_m=1.8),
    )


def _projector(*, y_m: float = 4.3, z_m: float = 2.25) -> SceneEntity:
    return SceneEntity(
        entity_id='projector-main',
        kind='projector',
        name='Projector',
        position=Position3(x_m=3.0, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.50, y_m=0.50, z_m=0.20),
    )


def _seat(entity_id: str, *, y_m: float, z_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=3.0, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
    )


def _speaker(*, x_m: float = 0.7, y_m: float = 0.7) -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-c',
        kind='speaker',
        name='Center',
        speaker_role='C',
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.4, y_m=0.3, z_m=0.3),
    )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=3.0),
        entities=(
            _screen(),
            _projector(),
            _seat('seat-front', y_m=2.2, z_m=0.5),
            _seat('seat-rear', y_m=3.4, z_m=0.5),
            _speaker(),
        ),
    )


def _baseline(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _spec_optical_values() -> dict:
    return projector_spec_optical_values(
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.2,
        throw_ratio_max=1.6,
        optical_zoom_ratio=1.33,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-0.25,
            maximum_fraction=0.25,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-0.65,
            maximum_fraction=0.65,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _spec_locators() -> dict:
    return {
        field: f'datasheet p.4, optical table, row "{field}"'
        for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
    }


def _spec_source_bytes() -> bytes:
    return json.dumps(
        {
            'schema': 'example.projector-spec-sheet.v1',
            'manufacturer': 'Example Projection Co.',
            'model': 'P',
            'document_version': '2026.1',
            'optical': _spec_optical_values(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _projector_spec_evidence() -> ProjectorSpecificationEvidence:
    return build_projector_spec_document_evidence(
        evidence_kind='manufacturer_document',
        manufacturer='Example Projection Co.',
        model='P',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=sha256(_spec_source_bytes()).hexdigest(),
        extractor_id='htdt-test-spec-table',
        extractor_version='1.0',
        field_assertions=build_projector_spec_field_assertions(
            optical_values=_spec_optical_values(),
            field_locators=_spec_locators(),
        ),
    )


def _projector_spec() -> ProjectorSpecification:
    evidence = _projector_spec_evidence()
    provenance = ProjectorSpecificationProvenance(
        source_kind='manufacturer',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=evidence.source_sha256,
        evidence=evidence.ref(),
    )
    return build_projector_specification(
        specification_id='example-projector-p',
        version='2026.1',
        manufacturer='Example Projection Co.',
        model='P',
        provenance=provenance,
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.2,
        throw_ratio_max=1.6,
        optical_zoom_ratio=1.33,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-0.25,
            maximum_fraction=0.25,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-0.65,
            maximum_fraction=0.65,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _save_projector_specification(
    repository: CadVideoGeometryRepository,
    specification: ProjectorSpecification,
) -> ProjectorSpecification:
    """Persist the fixture spec together with its exact retained evidence."""
    return repository.save_projector_specification(
        specification,
        evidence=_projector_spec_evidence(),
        source_bytes=_spec_source_bytes(),
        source_filename=SPEC_SOURCE_FILENAME,
        media_type=SPEC_SOURCE_MEDIA_TYPE,
    )


def _request(specification):
    return build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=specification,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=8.0 / 3.0,
            visible_height_m=1.5,
            frame_clearance_m=0.05,
            acoustically_transparent=True,
        ),
        seats=(
            SeatGeometryBinding(
                entity_id='seat-front',
                row_id='row-front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
            ),
            SeatGeometryBinding(
                entity_id='seat-rear',
                row_id='row-rear',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
                riser_entity_id=None,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(
                minimum_deg=20.0,
                maximum_deg=80.0,
            ),
            vertical_viewing_angle_deg=AngleRange(
                minimum_deg=10.0,
                maximum_deg=50.0,
            ),
            center_elevation_angle_deg=AngleRange(
                minimum_deg=-15.0,
                maximum_deg=20.0,
            ),
            sightline_samples=(
                SightlineSample(
                    sample_id='bottom-center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.0,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=(
            'screen-main',
            'projector-main',
            'speaker-c',
        ),
    )


def _raised_rear_variant(baseline):
    raised_rear = _seat('seat-rear', y_m=3.4, z_m=1.1)
    riser = SceneEntity(
        entity_id='riser-rear',
        kind='riser',
        name='Rear Riser',
        position=Position3(x_m=3.0, y_m=3.4, z_m=0.3),
        size_m=Size3(x_m=2.0, y_m=1.4, z_m=0.6),
    )
    return build_system_variant(
        baseline=baseline,
        name='Raised rear row and projector adjustment',
        role_bindings=(ChannelRoleBinding(role_id='C', display_name='Center'),),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='projector-adjustment',
                entity=_projector(y_m=4.1),
            ),
            ProposedEntitySpec(
                spec_id='rear-seat-riser-placement',
                entity=raised_rear,
            ),
            ProposedEntitySpec(
                spec_id='rear-riser',
                entity=riser,
            ),
        ),
        created_at_utc=NOW,
    )


def _request_with_riser(
    request: VideoGeometryRequest,
    specification,
) -> VideoGeometryRequest:
    updated = request.model_copy(
        update={
            'seats': tuple(
                item.model_copy(update={'riser_entity_id': 'riser-rear'})
                if item.entity_id == 'seat-rear'
                else item
                for item in request.seats
            )
        }
    )
    return build_video_geometry_request(
        projector_entity_id=updated.projector_entity_id,
        projector_specification=specification,
        screen=updated.screen,
        seats=updated.seats,
        policy=updated.policy,
        collision_entity_ids=updated.collision_entity_ids,
    )


def _persisted_baseline_evaluation(tmp_path: Path):
    scene_repository, baseline = _baseline(tmp_path)
    specification = _projector_spec()
    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=None,
        projector_specification=specification,
        request=_request(specification),
    )
    repository = CadVideoGeometryRepository(scene_repository)
    _save_projector_specification(repository, specification)
    repository.save_evaluation(evaluation)
    return scene_repository, repository, baseline, evaluation


def _persisted_variant_evaluation(tmp_path: Path):
    scene_repository, baseline = _baseline(tmp_path)
    specification = _projector_spec()
    variant = _raised_rear_variant(baseline)
    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=variant,
        projector_specification=specification,
        request=_request_with_riser(_request(specification), specification),
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(variant)
    repository = CadVideoGeometryRepository(scene_repository, variant_repository)
    _save_projector_specification(repository, specification)
    repository.save_evaluation(evaluation)
    return scene_repository, repository, baseline, variant, evaluation


def _rehashed_evaluation(
    evaluation: VideoGeometryEvaluation,
    **updates,
) -> VideoGeometryEvaluation:
    """Coherently recompute the self hash/id of a modified evaluation."""
    candidate = evaluation.model_copy(update=updates)
    digest = _digest(candidate.identity_payload())
    return VideoGeometryEvaluation.model_validate(
        candidate.model_copy(
            update={
                'evaluation_sha256': digest,
                'evaluation_id': 'vge-' + digest[:24],
            }
        ).model_dump(mode='python')
    )


def _replace_persisted_evaluation(
    path: Path,
    evaluation_id: str,
    persisted: VideoGeometryEvaluation,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            UPDATE cad_video_geometry_evaluations
            SET evaluation_id=?, evaluation_sha256=?, payload_json=?
            WHERE evaluation_id=?
            """,
            (
                persisted.evaluation_id,
                persisted.evaluation_sha256,
                persisted.model_dump_json(),
                evaluation_id,
            ),
        )


def test_projector_specification_identity_and_user_defined_source_are_explicit() -> None:
    first = _projector_spec()
    second = _projector_spec()
    assert first == second
    assert first.specification_sha256 == second.specification_sha256
    assert first.provenance.source_kind == 'manufacturer'
    assert first.provenance.evidence.evidence_kind == 'manufacturer_document'
    assert first.lens_reference_offset_m.y_m == pytest.approx(-0.25)

    manual_optical = projector_spec_optical_values(
        lens_reference_offset_m=Offset3(),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=2.0,
    )
    manual_evidence = build_projector_spec_manual_evidence(
        publisher='HTDT user',
        document_title='Measured placement envelope',
        document_version='1',
        reference='manual entry',
        field_assertions=build_projector_spec_field_assertions(
            optical_values=manual_optical,
            field_locators={
                field: 'tape measurement notes'
                for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
            },
        ),
        source_citation='site measurement worksheet 2026-09-19',
        actor='installer-a',
        recorded_at_utc=NOW,
        evidence_basis='user_measurement',
    )
    user_defined = build_projector_specification(
        specification_id='custom-projector-envelope',
        version='1',
        provenance=ProjectorSpecificationProvenance(
            source_kind='user_defined',
            publisher='HTDT user',
            document_title='Measured placement envelope',
            document_version='1',
            reference='manual entry',
            evidence=manual_evidence.ref(),
        ),
        lens_reference_offset_m=Offset3(),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=2.0,
    )
    assert user_defined.manufacturer is None
    assert user_defined.model is None
    assert user_defined.provenance.source_kind == 'user_defined'
    assert user_defined.provenance.evidence.evidence_kind == 'manual_record'
    assert (
        user_defined.provenance.evidence.evidence_sha256
        == manual_evidence.evidence_sha256
    )


def test_baseline_projection_and_viewing_are_deterministic_but_rear_sightline_fails(
    tmp_path: Path,
) -> None:
    _scene_repository, baseline = _baseline(tmp_path)
    specification = _projector_spec()
    request = _request(specification)

    first = evaluate_video_geometry(
        baseline=baseline,
        variant=None,
        projector_specification=specification,
        request=request,
    )
    second = evaluate_video_geometry(
        baseline=baseline,
        variant=None,
        projector_specification=specification,
        request=request,
    )

    assert first == second
    assert first.evaluation_id == second.evaluation_id
    assert first.projection.status == 'PASS'
    assert first.projection.throw_ratio == pytest.approx(3.85 / (8.0 / 3.0))
    assert first.projection.required_horizontal_lens_shift_fraction == pytest.approx(0.0)
    assert first.projection.required_vertical_lens_shift_fraction == pytest.approx(-0.5)
    assert first.projection.image_plane_corners[0].x_m == pytest.approx(3.0 - 4.0 / 3.0)
    assert len(first.projection.projection_cone_directions) == 4
    assert all(
        direction.y < 0.0
        for direction in first.projection.projection_cone_directions
    )
    assert all(item.horizontal_status == 'PASS' for item in first.viewing)
    assert all(item.vertical_status == 'PASS' for item in first.viewing)
    sightline = {item.seat_entity_id: item for item in first.sightlines}
    assert sightline['seat-front'].status == 'PASS'
    assert sightline['seat-rear'].status == 'FAIL'
    assert sightline['seat-rear'].blocking_seat_ids == ('seat-front',)
    assert first.geometry_status == 'FAIL'
    assert first.screen_acoustic_effect_status == 'UNKNOWN'
    # #541: a legacy acoustically_transparent flag is never acoustic truth —
    # UNKNOWN stands until an exact AcousticScreenTransferAuthority is bound.
    assert 'AcousticScreenTransferAuthority' in first.screen_acoustic_effect_reason


def test_system_variant_can_raise_rear_row_on_riser_and_replace_projector_without_mutating_baseline(
    tmp_path: Path,
) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    specification = _projector_spec()
    request = _request(specification)

    variant = _raised_rear_variant(baseline)
    request_with_riser = _request_with_riser(request, specification)

    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=variant,
        projector_specification=specification,
        request=request_with_riser,
    )
    assert evaluation.target.scene_revision_id == baseline.revision_id
    assert evaluation.target.system_variant_id == variant.variant_id
    assert evaluation.target.system_variant_sha256 == variant.variant_sha256
    assert evaluation.target.evaluated_scene_content_hash != baseline.content_hash
    assert scene_repository.latest(DOCUMENT_ID).revision_id == baseline.revision_id

    sightline = {item.seat_entity_id: item for item in evaluation.sightlines}
    assert sightline['seat-rear'].status == 'PASS'
    risers = {item.seat_entity_id: item for item in evaluation.risers}
    assert risers['seat-front'].status == 'NOT_APPLICABLE'
    assert risers['seat-rear'].status == 'PASS'
    assert risers['seat-rear'].support_gap_m == pytest.approx(0.0)
    assert evaluation.projection.throw_ratio == pytest.approx(3.65 / (8.0 / 3.0))
    assert evaluation.geometry_status == 'PASS'

    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(variant)
    repository = CadVideoGeometryRepository(scene_repository, variant_repository)
    assert _save_projector_specification(repository, specification) == specification
    assert repository.save_evaluation(evaluation) == evaluation

    reopened = CadVideoGeometryRepository(scene_repository, variant_repository)
    assert reopened.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) == specification
    assert reopened.get_evaluation(evaluation.evaluation_id) == evaluation
    assert reopened.list_evaluations_for_variant(variant.variant_id) == (evaluation,)


def test_speaker_screen_collision_is_explicit_geometry_failure(tmp_path: Path) -> None:
    _scene_repository, baseline = _baseline(tmp_path)
    specification = _projector_spec()
    request = _request(specification)
    colliding = _speaker(x_m=3.0, y_m=0.35)

    variant = build_system_variant(
        baseline=baseline,
        name='Center speaker collision',
        role_bindings=(ChannelRoleBinding(role_id='C', display_name='Center'),),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='center-collision',
                entity=colliding,
                role_binding_id='C',
            ),
        ),
        created_at_utc=NOW,
    )
    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=variant,
        projector_specification=specification,
        request=request,
    )
    collision = next(
        item
        for item in evaluation.collisions
        if {item.entity_a, item.entity_b} == {'screen-main', 'speaker-c'}
    )
    assert collision.status == 'FAIL'
    assert collision.intersects_or_violates_clearance is True
    assert evaluation.geometry_status == 'FAIL'


def test_non_physical_proposed_entity_is_rejected() -> None:
    with pytest.raises(ValidationError, match='physical SceneEntity'):
        ProposedEntitySpec(
            spec_id='not-physical',
            entity=SceneEntity(
                entity_id='measurement',
                kind='measurement_point',
                name='Measurement',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            ),
        )


def test_read_apis_replay_persisted_evaluation_and_reject_coherent_tampering(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, evaluation = (
        _persisted_baseline_evaluation(tmp_path)
    )
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations_for_revision(baseline.revision_id) == (
        evaluation,
    )

    tampered = _rehashed_evaluation(
        evaluation,
        projection=evaluation.projection.model_copy(
            update={'throw_ratio': evaluation.projection.throw_ratio + 0.5}
        ),
    )
    assert tampered.evaluation_id != evaluation.evaluation_id
    _replace_persisted_evaluation(
        scene_repository.path,
        evaluation.evaluation_id,
        tampered,
    )

    assert repository.get_evaluation(evaluation.evaluation_id) is None
    with pytest.raises(ValueError, match='not reproducible'):
        repository.get_evaluation(tampered.evaluation_id)
    with pytest.raises(ValueError, match='not reproducible'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_variant_bound_read_apis_replay_and_reject_coherent_tampering(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, variant, evaluation = (
        _persisted_variant_evaluation(tmp_path)
    )
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations_for_revision(baseline.revision_id) == (
        evaluation,
    )
    assert repository.list_evaluations_for_variant(variant.variant_id) == (
        evaluation,
    )

    tampered = _rehashed_evaluation(evaluation, geometry_status='FAIL')
    _replace_persisted_evaluation(
        scene_repository.path,
        evaluation.evaluation_id,
        tampered,
    )

    with pytest.raises(ValueError, match='not reproducible'):
        repository.get_evaluation(tampered.evaluation_id)
    with pytest.raises(ValueError, match='not reproducible'):
        repository.list_evaluations_for_variant(variant.variant_id)
    with pytest.raises(ValueError, match='not reproducible'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_read_apis_fail_closed_when_projector_specification_disappears(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, evaluation = (
        _persisted_baseline_evaluation(tmp_path)
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('DELETE FROM cad_projector_specifications')

    with pytest.raises(ValueError, match='unpersisted projector specification'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='unpersisted projector specification'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_read_apis_fail_closed_when_scene_revision_authority_changes(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, evaluation = (
        _persisted_baseline_evaluation(tmp_path)
    )
    mutated = _scene().model_copy(
        update={'room': RoomPrism(width_m=7.0, depth_m=5.0, height_m=3.0)}
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            """
            UPDATE scene_revisions
            SET payload_json=?, content_hash=?
            WHERE revision_id=?
            """,
            (
                mutated.model_dump_json(),
                scene_content_hash(mutated),
                baseline.revision_id,
            ),
        )

    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_read_apis_fail_closed_when_system_variant_authority_changes(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, variant, evaluation = (
        _persisted_variant_evaluation(tmp_path)
    )
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_system_variants WHERE variant_id=?',
            (variant.variant_id,),
        )

    with pytest.raises(ValueError, match='SystemVariant does not exist'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='SystemVariant does not exist'):
        repository.list_evaluations_for_variant(variant.variant_id)
    with pytest.raises(ValueError, match='SystemVariant does not exist'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_read_apis_fail_closed_when_system_variant_hash_mismatches(
    tmp_path: Path,
) -> None:
    scene_repository, repository, baseline, variant, evaluation = (
        _persisted_variant_evaluation(tmp_path)
    )
    tampered = _rehashed_evaluation(
        evaluation,
        target=evaluation.target.model_copy(
            update={'system_variant_sha256': '0' * 64}
        ),
    )
    _replace_persisted_evaluation(
        scene_repository.path,
        evaluation.evaluation_id,
        tampered,
    )

    with pytest.raises(ValueError, match='SystemVariant hash mismatch'):
        repository.get_evaluation(tampered.evaluation_id)
    with pytest.raises(ValueError, match='SystemVariant hash mismatch'):
        repository.list_evaluations_for_variant(variant.variant_id)
    with pytest.raises(ValueError, match='SystemVariant hash mismatch'):
        repository.list_evaluations_for_revision(baseline.revision_id)
