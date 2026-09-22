from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.cad_standards_evidence import build_standards_observation_authority
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_video_geometry import (
    AngleRange,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecificationProvenance,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
)
from htdt.cad_video_geometry_repository import CadVideoGeometryRepository
from htdt.report import (
    InstallationOutput,
    build_installation_output,
    render_installation_csv,
    render_installation_report_html,
)


NOW = '2026-09-19T12:00:00+00:00'


def _scene():
    base = make_f1_scene()
    additions = (
        SceneEntity(
            entity_id='projector-main',
            kind='projector',
            name='Projector',
            position=Position3(x_m=3.0, y_m=4.0, z_m=1.8),
            size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
        ),
        SceneEntity(
            entity_id='screen-main',
            kind='screen',
            name='Screen',
            position=Position3(x_m=3.0, y_m=0.15, z_m=1.4),
            size_m=Size3(x_m=2.4, y_m=0.1, z_m=1.4),
        ),
        SceneEntity(
            entity_id='seat-front',
            kind='seat',
            name='Front seat',
            position=Position3(x_m=3.0, y_m=2.0, z_m=0.5),
            size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        ),
        SceneEntity(
            entity_id='seat-rear',
            kind='seat',
            name='Rear seat',
            position=Position3(x_m=3.0, y_m=3.2, z_m=0.5),
            size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        ),
    )
    return base.model_copy(update={'entities': base.entities + additions})


def _projector_spec(specification_id: str = 'projector-spec-main'):
    return build_projector_specification(
        specification_id=specification_id,
        version='1',
        manufacturer='Example',
        model='P1',
        provenance=ProjectorSpecificationProvenance(
            source_kind='manufacturer',
            publisher='Example',
            document_title='P1 installation manual',
            document_version='1.0',
            reference='projection specifications',
            source_uri='https://example.invalid/p1',
            source_sha256='b' * 64,
        ),
        lens_reference_offset_m=Offset3(),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=3.0,
        optical_zoom_ratio=2.0,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _video_request(specification):
    return build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=specification,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=2.0,
            visible_height_m=1.125,
            frame_clearance_m=0.02,
            acoustically_transparent=True,
        ),
        seats=(
            SeatGeometryBinding(
                entity_id='seat-front',
                row_id='front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.18,
            ),
            SeatGeometryBinding(
                entity_id='seat-rear',
                row_id='rear',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.18,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(
                minimum_deg=1.0,
                maximum_deg=179.0,
            ),
            vertical_viewing_angle_deg=AngleRange(
                minimum_deg=1.0,
                maximum_deg=179.0,
            ),
            center_elevation_angle_deg=AngleRange(
                minimum_deg=-89.0,
                maximum_deg=89.0,
            ),
            sightline_samples=(
                SightlineSample(
                    sample_id='center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.5,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=('projector-main', 'screen-main'),
    )


def _standards(revision, *, variant=None):
    source = CriterionSource(
        publisher='HTDT fixture standards body',
        document_title='Video geometry criteria',
        document_version='2026.1',
        reference='clauses 1-3',
        source_uri='https://example.invalid/standards',
    )
    profile = build_user_standards_profile(
        profile_id='video-installation-profile',
        version='1',
        name='Video installation fixture profile',
        criteria=(
            CriterionDefinition(
                criterion_id='pass-angle',
                name='Passing angle',
                source=source,
                quantity='angle',
                unit='deg',
                applicable_domains=('video',),
                evidence_requirement='none',
                rule=CriterionRule(operator='max', maximum=20.0),
            ),
            CriterionDefinition(
                criterion_id='fail-angle',
                name='Failing angle',
                source=source,
                quantity='angle',
                unit='deg',
                applicable_domains=('video',),
                evidence_requirement='none',
                rule=CriterionRule(operator='max', maximum=5.0),
            ),
            CriterionDefinition(
                criterion_id='unknown-clearance',
                name='Unknown clearance',
                source=source,
                quantity='clearance',
                unit='m',
                applicable_domains=('video',),
                required_inputs=('qualified-clearance',),
                evidence_requirement='none',
                rule=CriterionRule(operator='min', minimum=0.02),
            ),
        ),
    )
    target = StandardsEvaluationTarget(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        entity_ids=('projector-main', 'screen-main'),
        applicable_domains=('video',),
    )
    angle_authority = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        quantity='angle',
        unit='deg',
        observed_value=10.0,
        evidence_basis='predicted',
        entity_ids=('projector-main',),
        observed_at_utc=NOW,
    )
    clearance_authority = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        quantity='clearance',
        unit='m',
        observed_value=0.04,
        evidence_basis='predicted',
        entity_ids=('screen-main',),
        observed_at_utc=NOW,
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(
            CriterionObservation(
                criterion_id='pass-angle',
                entity_ids=('projector-main',),
                observed_value=10.0,
                unit='deg',
                evidence_basis='predicted',
                evidence_refs=(angle_authority.ref(),),
            ),
            CriterionObservation(
                criterion_id='fail-angle',
                entity_ids=('projector-main',),
                observed_value=10.0,
                unit='deg',
                evidence_basis='predicted',
                evidence_refs=(angle_authority.ref(),),
            ),
            CriterionObservation(
                criterion_id='unknown-clearance',
                entity_ids=('screen-main',),
                observed_value=0.04,
                unit='m',
                evidence_basis='predicted',
                evidence_refs=(clearance_authority.ref(),),
            ),
        ),
        created_at_utc=NOW,
    )
    authorities = (angle_authority, clearance_authority)
    return profile, evaluation, authorities


def _authorities(tmp_path: Path):
    database = tmp_path / 'scene.sqlite3'
    scene_repository = SceneRepository(database)
    saved = scene_repository.save(_scene(), parent_revision_id=None)
    specification = _projector_spec()
    video = evaluate_video_geometry(
        baseline=saved.revision,
        variant=None,
        projector_specification=specification,
        request=_video_request(specification),
    )
    profile, standards, observation_authorities = _standards(saved.revision)
    return (
        database,
        scene_repository,
        saved.revision,
        specification,
        video,
        profile,
        standards,
        observation_authorities,
    )


def _build(revision, specification, video, profile, standards, *, variant=None):
    return build_installation_output(
        revision,
        variant=variant,
        projector_specification=specification,
        video_geometry_evaluation=video,
        standards_profile=profile,
        standards_evaluation=standards,
    )


def test_projector_and_standards_are_exact_authority_summaries(tmp_path: Path) -> None:
    (
        _database,
        _repo,
        revision,
        specification,
        video,
        profile,
        standards,
        _observation_authorities,
    ) = _authorities(tmp_path)
    output = _build(revision, specification, video, profile, standards)

    assert output.schema_version == 4
    assert output.authority_version == 'installation-output-4'
    assert output.projector is not None
    assert output.projector.status == 'AVAILABLE'
    assert output.projector.specification_id == specification.specification_id
    assert output.projector.specification_version == specification.version
    assert output.projector.specification_sha256 == specification.specification_sha256
    assert output.projector.projector_entity_id == 'projector-main'
    assert output.projector.lens_position_m == pytest.approx((3.0, 4.0, 1.8))
    assert output.projector.optical_axis_local == pytest.approx((0.0, -1.0, 0.0))
    assert output.projector.evaluated_throw_ratio == pytest.approx(1.925)
    assert output.projector.zoom_position == pytest.approx((1.925 - 1.0) / 2.0)
    assert output.projector.screen_entity_id == 'screen-main'
    assert output.projector.screen_visible_width_m == pytest.approx(2.0)
    assert output.projector.screen_visible_height_m == pytest.approx(1.125)
    assert output.projector.screen_frame_clearance_m == pytest.approx(0.02)
    assert len(output.projector.image_plane_corners_m) == 4
    assert len(output.projector.projection_cone_directions) == 4
    assert output.projector.screen_acoustic_effect_status == 'UNKNOWN'

    sightlines = {item.seat_entity_id: item for item in output.projector.sightlines}
    assert sightlines['seat-front'].status == 'PASS'
    assert sightlines['seat-rear'].status == 'FAIL'
    assert sightlines['seat-rear'].blocking_seat_ids == ('seat-front',)
    assert len(output.projector.collisions) == 1
    assert output.projector.collisions[0].status == 'PASS'

    assert output.standards is not None
    assert output.standards.status == 'AVAILABLE'
    assert output.standards.profile_id == profile.profile_id
    assert output.standards.profile_version == profile.version
    assert output.standards.profile_semantic_hash == profile.profile_semantic_hash
    assert output.standards.evaluation_id == standards.evaluation_id
    assert output.standards.evaluation_sha256 == standards.evaluation_sha256
    status_by_id = {item.criterion_id: item.status for item in output.standards.criteria}
    assert status_by_id == {
        'pass-angle': 'PASS',
        'fail-angle': 'FAIL',
        'unknown-clearance': 'UNKNOWN',
    }
    assert all(item.source_reference == 'clauses 1-3' for item in output.standards.criteria)
    assert not hasattr(output.standards, 'score')


def test_scene_presence_does_not_promote_missing_authority_from_unknown(
    tmp_path: Path,
) -> None:
    _database, _repo, revision, *_rest = _authorities(tmp_path)
    output = build_installation_output(revision)

    assert any(item.entity_kind == 'projector' for item in output.entities)
    assert output.projector is not None
    assert output.projector.status == 'UNKNOWN'
    assert output.standards is not None
    assert output.standards.status == 'UNKNOWN'
    assert output.treatment is not None
    assert output.treatment.status == 'UNKNOWN'
    assert output.calibration is not None
    assert output.calibration.status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'projector_coordinates'
    ).status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'standards_profile'
    ).status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'treatment_plan'
    ).status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'calibration_plan'
    ).status == 'UNKNOWN'


def test_scene_revision_and_system_variant_mismatch_fail_closed(tmp_path: Path) -> None:
    (
        _database,
        scene_repository,
        revision,
        specification,
        video,
        profile,
        standards,
        _observation_authorities,
    ) = _authorities(tmp_path)

    moved_projector = revision.document.entity('projector-main').model_copy(
        update={'position': Position3(x_m=3.1, y_m=4.0, z_m=1.8)}
    )
    changed = revision.document.model_copy(
        update={
            'entities': tuple(
                moved_projector if item.entity_id == 'projector-main' else item
                for item in revision.document.entities
            )
        }
    )
    newer = scene_repository.save(changed, parent_revision_id=revision.revision_id).revision
    with pytest.raises(ValueError, match='video geometry evaluation authority'):
        _build(newer, specification, video, profile, standards)

    changed_speaker = revision.document.entity('speaker-fl').model_copy(
        update={
            'orientation': quaternion_from_euler_deg(
                yaw_deg=2.0,
                pitch_deg=0.0,
                roll_deg=0.0,
            )
        }
    )
    variant = build_system_variant(
        baseline=revision,
        name='mismatch variant',
        role_bindings=(ChannelRoleBinding(role_id='FL', display_name='Front Left'),),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='speaker-fl-change',
                entity=changed_speaker,
                role_binding_id='FL',
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='video geometry evaluation authority'):
        _build(
            revision,
            specification,
            video,
            profile,
            standards,
            variant=variant,
        )


def test_projector_specification_evaluation_hash_binding_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    (
        _database,
        _repo,
        revision,
        specification,
        _video,
        profile,
        standards,
        _observation_authorities,
    ) = _authorities(tmp_path)
    other_specification = _projector_spec('projector-spec-other')
    other_video = evaluate_video_geometry(
        baseline=revision,
        variant=None,
        projector_specification=other_specification,
        request=_video_request(other_specification),
    )
    with pytest.raises(ValueError, match='ProjectorSpecification/VideoGeometryEvaluation'):
        _build(
            revision,
            specification,
            other_video,
            profile,
            standards,
        )


def test_csv_html_and_export_timestamp_keep_semantic_identity_deterministic(
    tmp_path: Path,
) -> None:
    (
        _database,
        _repo,
        revision,
        specification,
        video,
        profile,
        standards,
        _observation_authorities,
    ) = _authorities(tmp_path)
    output = _build(revision, specification, video, profile, standards)
    semantic_hash = output.semantic_sha256

    first_csv = render_installation_csv(output)
    second_csv = render_installation_csv(output)
    assert first_csv == second_csv
    assert 'authority_record,projector,' in first_csv
    assert 'authority_record,treatment,' in first_csv
    assert 'authority_record,calibration,' in first_csv
    assert specification.specification_sha256 in first_csv
    assert standards.evaluation_sha256 in first_csv

    first_html = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T12:01:00+00:00',
    )
    second_html = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T13:02:00+00:00',
    )
    assert first_html != second_html
    assert output.semantic_sha256 == semantic_hash
    assert specification.specification_sha256 in first_html
    assert standards.evaluation_sha256 in first_html
    assert 'PASS' in first_html
    assert 'FAIL' in first_html
    assert 'UNKNOWN' in first_html

    marker = '<script type="application/json" id="htdt-installation-output">'
    first_payload = first_html.split(marker, 1)[1].split('</script>', 1)[0]
    second_payload = second_html.split(marker, 1)[1].split('</script>', 1)[0]
    assert first_payload == second_payload
    assert 'exported_at' not in first_payload


def test_save_reopen_and_regeneration_preserve_semantic_output(tmp_path: Path) -> None:
    (
        database,
        scene_repository,
        revision,
        specification,
        video,
        profile,
        standards,
        observation_authorities,
    ) = _authorities(tmp_path)
    video_repository = CadVideoGeometryRepository(scene_repository)
    standards_repository = CadStandardsRepository(scene_repository)
    video_repository.save_projector_specification(specification)
    video_repository.save_evaluation(video)
    standards_repository.save_profile(profile)
    for authority in observation_authorities:
        standards_repository.save_observation_authority(authority)
    standards_repository.save_evaluation(standards)

    before = _build(revision, specification, video, profile, standards)

    reopened_scene_repository = SceneRepository(database)
    reopened_video_repository = CadVideoGeometryRepository(reopened_scene_repository)
    reopened_standards_repository = CadStandardsRepository(reopened_scene_repository)
    reopened_revision = reopened_scene_repository.get(revision.revision_id)
    reopened_specification = reopened_video_repository.get_projector_specification(
        specification.specification_id,
        specification.version,
    )
    reopened_video = reopened_video_repository.get_evaluation(video.evaluation_id)
    reopened_profile = reopened_standards_repository.get_profile(profile.profile_id, profile.version)
    reopened_standards = reopened_standards_repository.get_evaluation(
        standards.evaluation_id
    )
    assert reopened_revision is not None
    assert reopened_specification is not None
    assert reopened_video is not None
    assert reopened_profile is not None
    assert reopened_standards is not None

    after = _build(
        reopened_revision,
        reopened_specification,
        reopened_video,
        reopened_profile,
        reopened_standards,
    )
    assert after == before
    assert render_installation_csv(after) == render_installation_csv(before)
    assert render_installation_report_html(
        after,
        exported_at_utc='2026-09-19T14:00:00+00:00',
    ).split('<script type="application/json" id="htdt-installation-output">', 1)[1] == (
        render_installation_report_html(
            before,
            exported_at_utc='2026-09-19T15:00:00+00:00',
        ).split('<script type="application/json" id="htdt-installation-output">', 1)[1]
    )


def test_v1_serialized_output_remains_loadable(tmp_path: Path) -> None:
    _database, _repo, revision, *_rest = _authorities(tmp_path)
    v2 = build_installation_output(revision)
    payload = {
        'schema_version': 1,
        'authority_version': 'installation-output-1',
        'coordinate_system': v2.coordinate_system,
        'authority': v2.authority.model_dump(mode='json'),
        'evidence': [item.model_dump(mode='json') for item in v2.evidence],
        'entities': [item.model_dump(mode='json') for item in v2.entities],
        'dimensions': [item.model_dump(mode='json') for item in v2.dimensions],
        'sections': [item.model_dump(mode='json') for item in v2.sections],
    }
    semantic = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()
    restored = InstallationOutput.model_validate({**payload, 'semantic_sha256': semantic})
    assert restored.schema_version == 1
    assert restored.authority_version == 'installation-output-1'
    assert restored.projector is None
    assert restored.standards is None
