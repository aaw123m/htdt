from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionEvidenceRef,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    build_user_standards_profile,
    evaluate_standards_profile,
    explicit_hard_constraint_gate,
    reevaluate_standards_profile,
)
from htdt.cad_standards_profiles import (
    auro3d_home_v12_profile,
    builtin_standards_profiles,
    dolby_atmos_home_5_1_2_profile,
    rp22_spatial_profile,
)
from htdt.cad_standards_evidence import build_standards_observation_authority
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import ChannelRoleBinding, build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-19T06:00:00+00:00'
SOURCE = CriterionSource(
    publisher='Fixture publisher',
    document_title='Fixture criteria',
    document_version='1.0',
    reference='Fixture §1',
)


def _criterion(
    criterion_id: str,
    *,
    maximum: float = 1.0,
    domain: str = 'room',
    capability: str = 'fixture-capability-v1',
) -> CriterionDefinition:
    return CriterionDefinition(
        criterion_id=criterion_id,
        name=criterion_id,
        source=SOURCE,
        quantity=f'{criterion_id}_quantity',
        unit='m',
        applicable_domains=(domain,),
        required_inputs=(f'{criterion_id}_input',),
        required_capabilities=(capability,),
        rule=CriterionRule(operator='max', maximum=maximum),
    )


def _target(
    *,
    revision,
    domains: tuple[str, ...] = ('room',),
    variant=None,
    entity_ids: tuple[str, ...] = (),
) -> StandardsEvaluationTarget:
    return StandardsEvaluationTarget(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        entity_ids=entity_ids,
        applicable_domains=domains,
    )


def _evidence(evidence_id: str) -> tuple[CriterionEvidenceRef, ...]:
    return (
        CriterionEvidenceRef(kind='fixture-evidence', evidence_id=evidence_id),
    )


def test_status_evidence_basis_and_explicit_hard_constraint_semantics() -> None:
    profile = build_user_standards_profile(
        profile_id='fixture-status-profile',
        version='1.0',
        name='Status fixture',
        criteria=(
            _criterion('pass'),
            _criterion('fail'),
            _criterion('unknown'),
            _criterion('not-applicable', maximum=1.0, domain='speaker_layout'),
        ),
    )
    target = StandardsEvaluationTarget(
        document_id='doc',
        scene_revision_id='rev',
        scene_content_hash='0' * 64,
        applicable_domains=('room',),
    )
    observations = (
        CriterionObservation(
            criterion_id='pass',
            observed_value=0.5,
            unit='m',
            evidence_basis='predicted',
            evidence_refs=_evidence('prediction-pass'),
            provided_inputs=('pass_input',),
            capabilities=('fixture-capability-v1',),
        ),
        CriterionObservation(
            criterion_id='fail',
            observed_value=2.0,
            unit='m',
            evidence_basis='measured',
            evidence_refs=_evidence('measurement-fail'),
            provided_inputs=('fail_input',),
            capabilities=('fixture-capability-v1',),
        ),
        CriterionObservation(
            criterion_id='unknown',
            observed_value=0.5,
            unit='m',
            evidence_basis='predicted',
            evidence_refs=_evidence('prediction-without-capability'),
            provided_inputs=('unknown_input',),
            capabilities=(),
        ),
    )

    advisory = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=observations,
        created_at_utc=NOW,
    )
    status = {result.criterion_id: result.status for result in advisory.results}
    assert status == {
        'pass': 'PASS',
        'fail': 'FAIL',
        'unknown': 'UNKNOWN',
        'not-applicable': 'NOT_APPLICABLE',
    }
    by_id = {result.criterion_id: result for result in advisory.results}
    assert by_id['pass'].evidence_basis == 'predicted'
    assert by_id['fail'].evidence_basis == 'measured'
    assert by_id['unknown'].reason_code == 'missing_input_or_capability'
    assert by_id['unknown'].missing_capabilities == ('fixture-capability-v1',)

    # FAIL is advisory until the user explicitly selects the criterion.
    assert explicit_hard_constraint_gate(
        advisory,
        selected_criterion_ids=(),
    ).allowed
    fail_gate = explicit_hard_constraint_gate(
        advisory,
        selected_criterion_ids=('fail',),
    )
    assert not fail_gate.allowed
    assert fail_gate.blocking_criterion_ids == ('fail',)
    assert explicit_hard_constraint_gate(
        advisory,
        selected_criterion_ids=('unknown',),
    ).blocking_criterion_ids == ('unknown',)

    # Downstream policy is not part of criterion evidence identity.
    assert fail_gate.evaluation_id == advisory.evaluation_id


def test_missing_source_is_rejected_and_measured_requirement_fails_closed() -> None:
    with pytest.raises(ValidationError):
        CriterionDefinition.model_validate(
            {
                'criterion_id': 'missing-source',
                'name': 'Missing source',
                'quantity': 'value',
                'unit': 'm',
                'applicable_domains': ['room'],
                'rule': {'operator': 'max', 'maximum': 1.0},
            }
        )

    measured_only = CriterionDefinition(
        criterion_id='measured-only',
        name='Measurement verified only',
        source=SOURCE,
        quantity='measured_quantity',
        unit='m',
        applicable_domains=('room',),
        required_inputs=('measured_input',),
        required_capabilities=('measurement-capability-v1',),
        evidence_requirement='measured',
        rule=CriterionRule(operator='max', maximum=1.0),
    )
    profile = build_user_standards_profile(
        profile_id='fixture-measured-only',
        version='1.0',
        name='Measured-only fixture',
        criteria=(measured_only,),
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=StandardsEvaluationTarget(
            document_id='doc',
            scene_revision_id='rev',
            scene_content_hash='0' * 64,
            applicable_domains=('room',),
        ),
        observations=(
            CriterionObservation(
                criterion_id='measured-only',
                observed_value=0.5,
                unit='m',
                evidence_basis='predicted',
                evidence_refs=_evidence('prediction'),
                provided_inputs=('measured_input',),
                capabilities=('measurement-capability-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert evaluation.results[0].status == 'UNKNOWN'
    assert evaluation.results[0].reason_code == 'measurement_evidence_required'

    measured_evaluation = evaluate_standards_profile(
        profile=profile,
        target=evaluation.target,
        observations=(
            CriterionObservation(
                criterion_id='measured-only',
                observed_value=0.5,
                unit='m',
                evidence_basis='measured',
                evidence_refs=_evidence('measurement'),
                provided_inputs=('measured_input',),
                capabilities=('measurement-capability-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert measured_evaluation.results[0].status == 'PASS'
    assert measured_evaluation.results[0].evidence_basis == 'measured'
    assert measured_evaluation.evaluation_id != evaluation.evaluation_id


def test_published_boundaries_and_angle_wrap_are_explicit() -> None:
    rp22_profiles = tuple(rp22_spatial_profile(level) for level in range(1, 5))
    assert tuple(profile.version for profile in rp22_profiles) == (
        '1.2-2023-09-prov1',
        '1.2-2023-09-prov1',
        '1.2-2023-09-prov1',
        '1.2-2023-09-prov1',
    )
    level3_upfiring = next(
        criterion
        for criterion in rp22_profiles[2].criteria
        if criterion.criterion_id == 'rp22.p08.upfiring-elevation-speakers-prohibited'
    )
    assert level3_upfiring.rule.expected is False

    rp22 = rp22_profiles[1]
    listener = next(
        criterion
        for criterion in rp22.criteria
        if criterion.criterion_id == 'rp22.p01.listener-boundary-distance'
    )
    assert listener.rule.minimum == pytest.approx(0.8)
    assert listener.rule.lower_inclusive is False

    target = StandardsEvaluationTarget(
        document_id='doc',
        scene_revision_id='rev',
        scene_content_hash='0' * 64,
        applicable_domains=('seat',),
    )
    at_boundary = evaluate_standards_profile(
        profile=rp22,
        target=target,
        observations=(
            CriterionObservation(
                criterion_id=listener.criterion_id,
                observed_value=0.8,
                unit='m',
                evidence_basis='predicted',
                evidence_refs=_evidence('scene-distance'),
                provided_inputs=('listener_head_to_nearest_room_boundary_m',),
                capabilities=('scene-geometry-distance-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert next(
        result
        for result in at_boundary.results
        if result.criterion_id == listener.criterion_id
    ).status == 'FAIL'

    above_boundary = evaluate_standards_profile(
        profile=rp22,
        target=target,
        observations=(
            CriterionObservation(
                criterion_id=listener.criterion_id,
                observed_value=0.800001,
                unit='m',
                evidence_basis='predicted',
                evidence_refs=_evidence('scene-distance'),
                provided_inputs=('listener_head_to_nearest_room_boundary_m',),
                capabilities=('scene-geometry-distance-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert next(
        result
        for result in above_boundary.results
        if result.criterion_id == listener.criterion_id
    ).status == 'PASS'

    dolby = dolby_atmos_home_5_1_2_profile()
    front_left = 'dolby.5.1.2.front-left-azimuth'
    wrapped = evaluate_standards_profile(
        profile=dolby,
        target=StandardsEvaluationTarget(
            document_id='doc',
            scene_revision_id='rev',
            scene_content_hash='0' * 64,
            applicable_domains=('speaker_layout',),
        ),
        observations=(
            CriterionObservation(
                criterion_id=front_left,
                observed_value=330.0,
                unit='deg',
                evidence_basis='predicted',
                evidence_refs=_evidence('azimuth'),
                provided_inputs=('front_left_azimuth_deg',),
                capabilities=('layout-angle-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert next(
        result for result in wrapped.results if result.criterion_id == front_left
    ).status == 'PASS'

    outside = evaluate_standards_profile(
        profile=dolby,
        target=wrapped.target,
        observations=(
            CriterionObservation(
                criterion_id=front_left,
                observed_value=329.0,
                unit='deg',
                evidence_basis='predicted',
                evidence_refs=_evidence('azimuth'),
                provided_inputs=('front_left_azimuth_deg',),
                capabilities=('layout-angle-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert next(
        result for result in outside.results if result.criterion_id == front_left
    ).status == 'FAIL'


    auro = auro3d_home_v12_profile()
    assert auro.version == 'rev12-2024-05-16-prov1'
    assert auro.profile_id in {
        profile.profile_id for profile in builtin_standards_profiles()
    }
    height_criterion = next(
        criterion
        for criterion in auro.criteria
        if criterion.criterion_id == 'auro.v12.height-layer-elevation'
    )
    assert height_criterion.rule.minimum == pytest.approx(25.0)
    assert height_criterion.rule.maximum == pytest.approx(40.0)

    auro_height = evaluate_standards_profile(
        profile=auro,
        target=StandardsEvaluationTarget(
            document_id='doc',
            scene_revision_id='rev',
            scene_content_hash='0' * 64,
            entity_ids=('height-left',),
            applicable_domains=('auro_height_speaker',),
        ),
        observations=(
            CriterionObservation(
                criterion_id=height_criterion.criterion_id,
                entity_ids=('height-left',),
                observed_value=25.0,
                unit='deg',
                evidence_basis='predicted',
                evidence_refs=_evidence('height-angle'),
                provided_inputs=('height_layer_speaker_elevation_deg',),
                capabilities=('layout-angle-v1',),
            ),
        ),
        created_at_utc=NOW,
    )
    assert next(
        result
        for result in auro_height.results
        if result.criterion_id == height_criterion.criterion_id
    ).status == 'PASS'
    assert next(
        result
        for result in auro_height.results
        if result.criterion_id == height_criterion.criterion_id
    ).entity_ids == ('height-left',)

    with pytest.raises(ValueError, match='outside evaluation target'):
        evaluate_standards_profile(
            profile=auro,
            target=auro_height.target,
            observations=(
                CriterionObservation(
                    criterion_id=height_criterion.criterion_id,
                    entity_ids=('height-right',),
                    observed_value=30.0,
                    unit='deg',
                    evidence_basis='predicted',
                    evidence_refs=_evidence('height-angle-other'),
                    provided_inputs=('height_layer_speaker_elevation_deg',),
                    capabilities=('layout-angle-v1',),
                ),
            ),
            created_at_utc=NOW,
        )


def test_persistence_exact_variant_binding_and_historical_reevaluation(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        make_f1_scene(),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Standards binding fixture',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ChannelRoleBinding(role_id='C', display_name='Center'),
            ChannelRoleBinding(role_id='FR', display_name='Front Right'),
        ),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    repository = CadStandardsRepository(scene_repository, variant_repository)
    criterion_v1 = _criterion('distance', maximum=1.0)
    profile_v1 = build_user_standards_profile(
        profile_id='fixture-versioned-profile',
        version='1.0',
        name='Versioned fixture',
        criteria=(criterion_v1,),
    )
    repository.save_profile(profile_v1)

    target = _target(
        revision=baseline,
        variant=variant,
        entity_ids=('speaker-fl',),
    )
    authority = build_standards_observation_authority(
        document_id=baseline.document_id,
        scene_revision_id=baseline.revision_id,
        scene_content_hash=baseline.content_hash,
        system_variant_id=variant.variant_id,
        system_variant_sha256=variant.variant_sha256,
        quantity='distance_quantity',
        unit='m',
        observed_value=0.8,
        evidence_basis='predicted',
        entity_ids=('speaker-fl',),
        provided_inputs=('distance_input',),
        capabilities=('fixture-capability-v1',),
        observed_at_utc=NOW,
    )
    repository.save_observation_authority(authority)
    observation = CriterionObservation(
        criterion_id='distance',
        entity_ids=('speaker-fl',),
        observed_value=0.8,
        unit='m',
        evidence_basis='predicted',
        evidence_refs=(authority.ref(),),
        provided_inputs=('distance_input',),
        capabilities=('fixture-capability-v1',),
    )
    evaluation_v1 = evaluate_standards_profile(
        profile=profile_v1,
        target=target,
        observations=(observation,),
        created_at_utc=NOW,
    )
    assert evaluation_v1.results[0].status == 'PASS'
    repository.save_evaluation(evaluation_v1)

    # Same profile + target + evidence gets the same deterministic identity,
    # regardless of the timestamp attached to an attempted duplicate save.
    repeated = evaluate_standards_profile(
        profile=profile_v1,
        target=target,
        observations=(observation,),
        created_at_utc='2026-09-19T06:05:00+00:00',
    )
    assert repeated.evaluation_id == evaluation_v1.evaluation_id
    assert repeated.evaluation_sha256 == evaluation_v1.evaluation_sha256
    assert repository.save_evaluation(repeated) == evaluation_v1

    reopened = CadStandardsRepository(scene_repository, variant_repository)
    assert reopened.get_profile(profile_v1.profile_id, profile_v1.version) == profile_v1
    assert reopened.get_evaluation(evaluation_v1.evaluation_id) == evaluation_v1

    profile_v2 = build_user_standards_profile(
        profile_id='fixture-versioned-profile',
        version='2.0',
        name='Versioned fixture',
        criteria=(_criterion('distance', maximum=0.5),),
    )
    reopened.save_profile(profile_v2)
    evaluation_v2 = reevaluate_standards_profile(
        previous=evaluation_v1,
        profile=profile_v2,
        observations=(observation,),
        created_at_utc='2026-09-19T06:10:00+00:00',
    )
    assert evaluation_v2.results[0].status == 'FAIL'
    assert evaluation_v2.reevaluation_of_id == evaluation_v1.evaluation_id
    reopened.save_evaluation(evaluation_v2)

    history = reopened.list_evaluations_for_scene(baseline.revision_id)
    assert history == (evaluation_v1, evaluation_v2)
    assert reopened.get_evaluation(evaluation_v1.evaluation_id) == evaluation_v1

    conflicting_v1 = build_user_standards_profile(
        profile_id='fixture-versioned-profile',
        version='1.0',
        name='Versioned fixture',
        criteria=(_criterion('distance', maximum=2.0),),
    )
    with pytest.raises(ValueError, match='immutable'):
        reopened.save_profile(conflicting_v1)

    bad_target = target.model_copy(
        update={'system_variant_sha256': 'f' * 64}
    )
    bad_evaluation = evaluate_standards_profile(
        profile=profile_v2,
        target=bad_target,
        observations=(observation,),
        created_at_utc='2026-09-19T06:20:00+00:00',
    )
    with pytest.raises(ValueError, match='SystemVariant authority mismatch'):
        reopened.save_evaluation(bad_evaluation)
