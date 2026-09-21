from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadDeviceCapabilityConstraints,
    build_biquad_filter,
    build_calibration_plan,
)
from htdt.cad_calibration_repository import CadCalibrationRepository
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_joint_optimization import (
    JointDecisionValue,
    JointDspVariable,
    JointEvaluationInputRef,
    JointEvaluatorIdentity,
    JointRobustnessSpecRef,
    JointRobustnessVariableMapping,
    bind_joint_candidate_evaluation,
    build_joint_candidate,
    build_joint_candidate_selection,
    build_joint_optimization_spec,
    joint_pareto_front,
)
from htdt.cad_joint_optimization_repository import CadJointOptimizationRepository
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadAcquisitionContextBinding,
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)


NOW = '2026-09-19T13:00:00+00:00'
DOCUMENT_ID = 'issue-174-joint-fixture'


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _device() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='issue174-fixture-device',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        max_filters_per_channel=2,
        max_boost_db=6.0,
        max_cut_db=6.0,
        min_gain_db=-12.0,
        max_gain_db=12.0,
        max_delay_s=0.020,
        supported_crossover_orders=(2, 4),
        allowed_physical_outputs=('out-fl',),
        frequency_resolution_hz=1.0,
        q_resolution=0.1,
        filter_gain_resolution_db=0.5,
        channel_gain_resolution_db=0.5,
        delay_resolution_s=0.001,
    )


def _channel(
    *,
    gain_db: float = 0.0,
    delay_s: float = 0.0,
    polarity: str = 'normal',
    peq=(),
) -> CadCalibrationChannel:
    return CadCalibrationChannel(
        channel_id='FL',
        role_id='FL',
        source_entity_id='speaker-fl',
        physical_output_id='out-fl',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=delay_s,
        polarity=polarity,
        peq=tuple(peq),
        routing=('main',),
    )


def _peq(
    filter_id: str,
    *,
    frequency_hz: float,
    gain_db: float,
):
    return build_biquad_filter(
        filter_id=filter_id,
        filter_type='peaking',
        frequency_hz=frequency_hz,
        q=1.0,
        gain_db=gain_db,
        sample_rate_hz=48000,
    )


def _objectives() -> tuple[ObjectiveDefinition, ...]:
    return (
        ObjectiveDefinition(
            objective_id='fixture.response_error_db',
            quantity='response_error',
            unit='dB',
            direction='minimize',
            valid_domain=ObjectiveValidDomain(
                kind='bounded_real',
                minimum=0.0,
            ),
            comparison_model_id='issue174-fixture-objectives',
            comparison_model_version='1',
        ),
        ObjectiveDefinition(
            objective_id='fixture.headroom_db',
            quantity='headroom',
            unit='dB',
            direction='maximize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
            comparison_model_id='issue174-fixture-objectives',
            comparison_model_version='1',
        ),
    )


def _evaluator(
    *,
    model_id: str = 'issue174-fixture-model',
    fidelity: str = 'deterministic-fixture',
) -> JointEvaluatorIdentity:
    return JointEvaluatorIdentity(
        evaluator_id='issue174-fixture-evaluator',
        evaluator_version='1',
        model_id=model_id,
        model_version='1',
        fidelity=fidelity,
        evidence_scope='fixture_only',
        fixture_only=True,
        synthetic=True,
        production_eligible=False,
    )


def _dsp_variables() -> tuple[JointDspVariable, ...]:
    return (
        JointDspVariable(
            variable_id='dsp:gain',
            channel_id='FL',
            parameter='gain_db',
            minimum=-12.0,
            maximum=12.0,
            step=0.5,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
        JointDspVariable(
            variable_id='dsp:peq-gain',
            channel_id='FL',
            parameter='peq_gain_db',
            filter_id='peq-1',
            minimum=-12.0,
            maximum=12.0,
            step=0.5,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
        JointDspVariable(
            variable_id='dsp:delay',
            channel_id='FL',
            parameter='delay_s',
            minimum=0.0,
            maximum=0.020,
            step=0.001,
            required_measurement_claim='common_timing',
        ),
        JointDspVariable(
            variable_id='dsp:polarity',
            channel_id='FL',
            parameter='polarity',
            allowed_values=('normal', 'inverted'),
            required_measurement_claim='polarity',
        ),
    )


def _save_quality(
    quality_repository: CadMeasurementQualityRepository,
    measurement,
    dataset,
    *,
    report_id: str,
    common_timing: bool = True,
    polarity: bool = True,
):
    evidence = CadMeasurementQualityEvidence(
        usable_frequency_band_hz=(20.0, 20000.0),
        timing_reference_valid=True if common_timing else None,
        timing_reference_id='loopback-174' if common_timing else None,
        clock_source='fixture-clock' if common_timing else None,
        sample_rate_hz=48000 if common_timing else None,
        delay_correction_s=0.0 if common_timing else None,
        polarity_correct=True if polarity else None,
        polarity_confidence=0.99 if polarity else None,
        evidence_source='manual',
    )
    acquisition = (
        CadAcquisitionContextBinding(
            acquisition_context_id='issue174-acquisition',
            acquisition_context_sha256=sha256(
                b'issue174-acquisition'
            ).hexdigest(),
            source_kind='manual',
        )
        if common_timing
        else None
    )
    profile = build_measurement_quality_profile(
        profile_version='issue174-quality-1',
        minimum_polarity_confidence=0.9,
    )
    report = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=acquisition,
        report_id=report_id,
        created_at_utc=NOW,
    )
    quality_repository.save_report(report)
    return report


def _save_plan(
    fixture,
    *,
    plan_id: str,
    variant=None,
    report=None,
    channel=None,
):
    plan = build_calibration_plan(
        scene_revision=fixture.revision,
        system_variant=variant or fixture.base_variant,
        measurement=fixture.measurement,
        dataset=fixture.dataset,
        quality_report=report or fixture.quality_report,
        channels=(channel or _channel(),),
        sample_rate_hz=48000,
        device_constraints=_device(),
        max_boost_db=6.0,
        max_cut_db=6.0,
        target_curve=None,
        plan_id=plan_id,
        plan_version='issue174-fixture-1',
        created_at_utc=NOW,
        source_kind='provided_fixture',
    )
    fixture.calibration_repository.save_plan(plan)
    return plan


def _build_spec(
    fixture,
    *,
    base_plan=None,
    report=None,
    evaluator=None,
    spec_id='joint-spec-fixture',
    candidate_budget=32,
):
    return build_joint_optimization_spec(
        scene_revision=fixture.revision,
        base_system_variant=fixture.base_variant,
        physical_search_spec=fixture.search_spec,
        extended_search_spec=None,
        base_calibration_plan=base_plan or fixture.base_plan,
        measurement_quality_report=report or fixture.quality_report,
        dsp_variables=_dsp_variables(),
        objectives=_objectives(),
        robustness=JointRobustnessSpecRef(
            robustness_spec_id='o90-fixture-spec',
            robustness_spec_sha256='9' * 64,
            variable_mapping=(
                JointRobustnessVariableMapping(
                    joint_variable_id='physical:speaker-fl:x_m',
                    o90_axis_id='speaker_x_m',
                ),
            ),
            dsp_perturbation_policy='none',
        ),
        evaluator=evaluator or _evaluator(),
        candidate_budget=candidate_budget,
        created_at_utc=NOW,
        spec_id=spec_id,
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision

    system_variant_repository = CadSystemVariantRepository(scene_repository)
    roles = (ChannelRoleBinding(role_id='FL', display_name='Front Left'),)
    base_variant = build_system_variant(
        baseline=revision,
        name='Issue 174 baseline',
        role_bindings=roles,
        proposed_entities=(),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(base_variant)

    speaker = revision.document.entity('speaker-fl')
    moved_speaker = speaker.model_copy(
        update={'position': Position3(x_m=2.0, y_m=1.0, z_m=1.0)}
    )
    moved_variant = build_system_variant(
        baseline=revision,
        name='Issue 174 moved speaker',
        role_bindings=roles,
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='move-speaker-fl',
                entity=moved_speaker,
                role_binding_id='FL',
            ),
        ),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(moved_variant)

    constraints = CadConstraintSet(
        document_id=DOCUMENT_ID,
        constraints=(),
    )
    search_spec, _estimate = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=1.0,
                max_m=2.0,
                step_m=1.0,
            ),
        ),
        candidate_limit=8,
        name='Issue 174 physical authority',
    )

    measurement_repository = CadMeasurementRepository(scene_repository)
    raw = declared_fr_raw(
        frequency_hz=(20.0, 100.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing={'fixture_raw': 'issue174-measurement'},
    )
    measurement = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id='measurement-174',
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at=NOW,
        source_kind='unknown',
        external_source_id='rew-174',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-174',
        measurement_id=measurement.measurement_id,
        frequency_hz=(20.0, 100.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=None,
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json({'fixture_raw': 'issue174-measurement'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        measurement,
        dataset,
        raw_filename='issue174.json',
        raw_bytes=raw,
    )

    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    quality_report = _save_quality(
        quality_repository,
        measurement,
        dataset,
        report_id='quality-174',
    )
    calibration_repository = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=system_variant_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )

    fixture = SimpleNamespace(
        scene_repository=scene_repository,
        revision=revision,
        system_variant_repository=system_variant_repository,
        base_variant=base_variant,
        moved_variant=moved_variant,
        search_spec=search_spec,
        measurement_repository=measurement_repository,
        measurement=measurement,
        dataset=dataset,
        quality_repository=quality_repository,
        quality_report=quality_report,
        calibration_repository=calibration_repository,
    )
    fixture.base_plan = _save_plan(
        fixture,
        plan_id='base-plan-174',
    )
    fixture.spec = _build_spec(fixture)
    return fixture


def _physical_decision():
    return JointDecisionValue(
        domain='physical',
        variable_id='physical:speaker-fl:x_m',
        value=2.0,
    )


def _dsp_decision(variable_id: str, value):
    return JointDecisionValue(
        domain='dsp',
        variable_id=variable_id,
        value=value,
    )


def _evaluation(spec, candidate, *, error: float, headroom: float):
    definitions = {item.objective_id: item for item in spec.objectives}
    vector = ObjectiveVector(
        candidate_id=candidate.candidate_id,
        metrics=(
            ObjectiveMetric(
                objective_id='fixture.response_error_db',
                value=error,
                unit='dB',
                direction='minimize',
                definition=definitions['fixture.response_error_db'],
            ),
            ObjectiveMetric(
                objective_id='fixture.headroom_db',
                value=headroom,
                unit='dB',
                direction='maximize',
                definition=definitions['fixture.headroom_db'],
            ),
        ),
    )
    return bind_joint_candidate_evaluation(
        spec=spec,
        candidate=candidate,
        objective_vector=vector,
        input_refs=(
            JointEvaluationInputRef(
                evidence_class='hypothesis',
                source_kind='issue174_deterministic_fixture',
                source_id=f'fixture:{candidate.candidate_id}',
                source_sha256=sha256(candidate.candidate_id.encode()).hexdigest(),
            ),
        ),
        created_at_utc=NOW,
    )


def test_position_only_candidate_uses_exact_physical_variant(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )

    assert candidate.candidate_class == 'position_only'
    assert candidate.eligibility_state == 'ELIGIBLE'
    assert candidate.calibration_candidate is None
    assert candidate.physical_system_variant_id == fixture.moved_variant.variant_id


def test_dsp_only_candidate_reuses_baseline_physical_variant(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    assert candidate.candidate_class == 'dsp_only'
    assert candidate.eligibility_state == 'ELIGIBLE'
    assert candidate.calibration_candidate is not None
    assert candidate.calibration_candidate.plan_id == plan.plan_id


def test_joint_candidate_composes_variant_and_calibration_by_exact_reference(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='joint-gain-plan-174',
        variant=fixture.moved_variant,
        channel=_channel(gain_db=1.5),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(
            _physical_decision(),
            _dsp_decision('dsp:gain', 1.5),
        ),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    assert candidate.candidate_class == 'joint'
    assert candidate.eligibility_state == 'ELIGIBLE'
    assert candidate.physical_system_variant_sha256 == fixture.moved_variant.variant_sha256
    assert candidate.calibration_candidate is not None
    assert candidate.calibration_candidate.plan_semantic_sha256 == plan.plan_semantic_sha256


def test_magnitude_safe_peq_candidate_passes_calibration_support_gate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='peq-safe-plan-174',
        channel=_channel(
            peq=(
                _peq(
                    'peq-1',
                    frequency_hz=100.0,
                    gain_db=2.0,
                ),
            )
        ),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:peq-gain', 2.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    assert plan.support_state == 'SUPPORTED'
    assert candidate.eligibility_state == 'ELIGIBLE'
    assert candidate.blocked_reasons == ()


def test_delay_candidate_without_common_timing_is_blocked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    report = _save_quality(
        fixture.quality_repository,
        fixture.measurement,
        fixture.dataset,
        report_id='quality-no-timing-174',
        common_timing=False,
    )
    base_plan = _save_plan(
        fixture,
        plan_id='base-no-timing-174',
        report=report,
    )
    spec = _build_spec(
        fixture,
        base_plan=base_plan,
        report=report,
        spec_id='joint-spec-no-timing',
    )
    delay_plan = _save_plan(
        fixture,
        plan_id='delay-no-timing-174',
        report=report,
        channel=_channel(delay_s=0.005),
    )
    candidate = build_joint_candidate(
        spec=spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:delay', 0.005),),
        calibration_plan=delay_plan,
        measurement_quality_report=report,
    )

    assert candidate.eligibility_state == 'BLOCKED'
    assert any('common timing' in reason for reason in candidate.blocked_reasons)


def test_polarity_candidate_without_polarity_authority_is_blocked(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    report = _save_quality(
        fixture.quality_repository,
        fixture.measurement,
        fixture.dataset,
        report_id='quality-no-polarity-174',
        polarity=False,
    )
    base_plan = _save_plan(
        fixture,
        plan_id='base-no-polarity-174',
        report=report,
    )
    spec = _build_spec(
        fixture,
        base_plan=base_plan,
        report=report,
        spec_id='joint-spec-no-polarity',
    )
    polarity_plan = _save_plan(
        fixture,
        plan_id='polarity-no-authority-174',
        report=report,
        channel=_channel(polarity='inverted'),
    )
    candidate = build_joint_candidate(
        spec=spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:polarity', 'inverted'),),
        calibration_plan=polarity_plan,
        measurement_quality_report=report,
    )

    assert candidate.eligibility_state == 'BLOCKED'
    assert any('polarity' in reason for reason in candidate.blocked_reasons)


def test_boost_limit_violation_is_blocked_after_plan_generation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='boost-limit-plan-174',
        channel=_channel(
            peq=(
                _peq(
                    'peq-1',
                    frequency_hz=100.0,
                    gain_db=8.0,
                ),
            )
        ),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:peq-gain', 8.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    assert plan.support_state == 'UNSUPPORTED'
    assert candidate.eligibility_state == 'BLOCKED'
    assert any('boost' in reason for reason in candidate.blocked_reasons)


def test_filter_count_violation_is_blocked_after_plan_generation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='filter-count-plan-174',
        channel=_channel(
            peq=(
                _peq('peq-1', frequency_hz=100.0, gain_db=0.0),
                _peq('peq-2', frequency_hz=125.0, gain_db=0.0),
                _peq('peq-3', frequency_hz=160.0, gain_db=0.0),
            )
        ),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:peq-gain', 0.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    assert candidate.eligibility_state == 'BLOCKED'
    assert any('filter count' in reason for reason in candidate.blocked_reasons)


def test_compatible_position_and_dsp_candidates_share_existing_pareto(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    position = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )
    gain_plan = _save_plan(
        fixture,
        plan_id='pareto-gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    dsp = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=gain_plan,
        measurement_quality_report=fixture.quality_report,
    )

    position_eval = _evaluation(fixture.spec, position, error=2.0, headroom=5.0)
    dsp_eval = _evaluation(fixture.spec, dsp, error=1.0, headroom=6.0)
    result = joint_pareto_front(
        (position_eval, dsp_eval),
        (position, dsp),
    )

    assert result.non_dominated_candidate_ids == (dsp.candidate_id,)
    assert result.algorithm_version == 'pareto-front-2'


def test_pareto_refuses_incompatible_evaluator_model_or_fidelity(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )
    first_eval = _evaluation(fixture.spec, first, error=2.0, headroom=5.0)

    other_spec = _build_spec(
        fixture,
        evaluator=_evaluator(
            model_id='issue174-other-fixture-model',
            fidelity='other-fixture-fidelity',
        ),
        spec_id='joint-spec-other-fidelity',
    )
    second = build_joint_candidate(
        spec=other_spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )
    second_eval = _evaluation(other_spec, second, error=1.0, headroom=6.0)

    with pytest.raises(ValueError, match='incompatible .*model/fidelity'):
        joint_pareto_front(
            (first_eval, second_eval),
            (first, second),
        )


def test_joint_candidate_identity_is_deterministic_for_exact_references(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='deterministic-gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    kwargs = dict(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )

    first = build_joint_candidate(**kwargs)
    second = build_joint_candidate(**kwargs)

    assert first == second
    assert first.candidate_id == second.candidate_id
    assert first.candidate_sha256 == second.candidate_sha256


def test_spec_candidate_evaluation_and_selection_save_reopen_deterministically(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='persist-gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    evaluation = _evaluation(
        fixture.spec,
        candidate,
        error=1.0,
        headroom=6.0,
    )
    selection = build_joint_candidate_selection(
        spec=fixture.spec,
        candidate=candidate,
        evaluation=evaluation,
        selected_at_utc=NOW,
        selection_id='selection-persist-174',
    )

    repository = CadJointOptimizationRepository(
        scene_repository=fixture.scene_repository,
        system_variant_repository=fixture.system_variant_repository,
        calibration_repository=fixture.calibration_repository,
    )
    repository.save_spec(fixture.spec)
    repository.save_candidate(candidate)
    repository.save_evaluation(evaluation)
    repository.save_selection(selection)

    reopened_scene = SceneRepository(fixture.scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_measurements = CadMeasurementRepository(reopened_scene)
    reopened_quality = CadMeasurementQualityRepository(reopened_measurements)
    reopened_calibration = CadCalibrationRepository(
        scene_repository=reopened_scene,
        system_variant_repository=reopened_variants,
        measurement_repository=reopened_measurements,
        quality_repository=reopened_quality,
    )
    reopened = CadJointOptimizationRepository(
        scene_repository=reopened_scene,
        system_variant_repository=reopened_variants,
        calibration_repository=reopened_calibration,
    )

    assert reopened.get_spec(fixture.spec.spec_id) == fixture.spec
    assert reopened.get_candidate(candidate.candidate_id) == candidate
    assert reopened.get_evaluation(evaluation.evaluation_binding_id) == evaluation
    assert reopened.get_selection(selection.selection_id) == selection
    assert reopened.latest_selection(fixture.spec.spec_id) == selection


def test_selected_candidate_does_not_mutate_scene_revision(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )
    selection = build_joint_candidate_selection(
        spec=fixture.spec,
        candidate=candidate,
        evaluation=None,
        selected_at_utc=NOW,
        selection_id='selection-no-apply-174',
    )
    repository = CadJointOptimizationRepository(
        scene_repository=fixture.scene_repository,
        system_variant_repository=fixture.system_variant_repository,
        calibration_repository=fixture.calibration_repository,
    )
    repository.save_spec(fixture.spec)
    repository.save_candidate(candidate)

    before = fixture.scene_repository.latest(DOCUMENT_ID)
    repository.save_selection(selection)
    after = fixture.scene_repository.latest(DOCUMENT_ID)

    assert before == after == fixture.revision
    assert fixture.system_variant_repository.application_for_variant(
        fixture.moved_variant.variant_id
    ) is None
    assert selection.scene_application_state == 'not_applied'


def test_selected_dsp_candidate_does_not_auto_export_or_advance_apply_state(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='no-export-gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    selection = build_joint_candidate_selection(
        spec=fixture.spec,
        candidate=candidate,
        evaluation=None,
        selected_at_utc=NOW,
        selection_id='selection-no-export-174',
    )
    repository = CadJointOptimizationRepository(
        scene_repository=fixture.scene_repository,
        system_variant_repository=fixture.system_variant_repository,
        calibration_repository=fixture.calibration_repository,
    )
    repository.save_spec(fixture.spec)
    repository.save_candidate(candidate)
    repository.save_selection(selection)

    assert selection.state == 'selected_only'
    assert selection.scene_application_state == 'not_applied'
    assert selection.dsp_export_state == 'not_exported'
    assert fixture.calibration_repository.list_exports(plan.plan_id) == ()
    assert fixture.system_variant_repository.application_for_variant(
        fixture.base_variant.variant_id
    ) is None


def _repository(fixture) -> CadJointOptimizationRepository:
    return CadJointOptimizationRepository(
        scene_repository=fixture.scene_repository,
        system_variant_repository=fixture.system_variant_repository,
        calibration_repository=fixture.calibration_repository,
    )


def _position_candidate(spec, variant, value_m: float):
    return build_joint_candidate(
        spec=spec,
        physical_system_variant=variant,
        decisions=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:x_m',
                value=value_m,
            ),
        ),
    )


def test_candidate_budget_rejects_distinct_candidate_beyond_persisted_budget(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _build_spec(
        fixture,
        spec_id='joint-spec-budget-one',
        candidate_budget=1,
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    admitted = _position_candidate(spec, fixture.moved_variant, 2.0)
    repository.save_candidate(admitted)

    overflow = _position_candidate(spec, fixture.moved_variant, 1.0)
    with pytest.raises(ValueError, match='candidate budget is exhausted'):
        repository.save_candidate(overflow)

    assert repository.list_candidates(spec.spec_id) == (admitted,)

    # A different spec keeps its own budget accounting.
    other = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_spec(fixture.spec)
    repository.save_candidate(other)
    assert repository.list_candidates(fixture.spec.spec_id) == (other,)


def test_save_candidate_exact_duplicate_is_idempotent_at_full_budget(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _build_spec(
        fixture,
        spec_id='joint-spec-budget-duplicate',
        candidate_budget=1,
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    candidate = _position_candidate(spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)

    # Duplicate identity is rechecked before budget admission, so an exact
    # duplicate stays idempotent even when the budget is already durable-full.
    duplicate = repository.save_candidate(candidate)
    assert duplicate == candidate
    assert repository.list_candidates(spec.spec_id) == (candidate,)


def test_concurrent_candidate_admission_serializes_last_budget_slot(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _build_spec(
        fixture,
        spec_id='joint-spec-budget-race',
        candidate_budget=2,
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    admitted = _position_candidate(spec, fixture.moved_variant, 2.0)
    repository.save_candidate(admitted)

    # One slot remains; two distinct candidates race for it.
    contender_a = _position_candidate(spec, fixture.moved_variant, 1.0)
    plan = _save_plan(
        fixture,
        plan_id='budget-race-gain-plan-174',
        channel=_channel(gain_db=1.0),
    )
    contender_b = build_joint_candidate(
        spec=spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    assert contender_a.candidate_id != contender_b.candidate_id

    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[str, object]] = {}

    def attempt(key: str, candidate) -> None:
        barrier.wait(10.0)
        try:
            outcomes[key] = ('saved', repository.save_candidate(candidate))
        except ValueError as exc:
            outcomes[key] = ('rejected', exc)

    threads = (
        threading.Thread(target=attempt, args=('a', contender_a)),
        threading.Thread(target=attempt, args=('b', contender_b)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30.0)
    assert not any(thread.is_alive() for thread in threads)

    saved = [
        value for status, value in outcomes.values() if status == 'saved'
    ]
    rejected = [
        value for status, value in outcomes.values() if status == 'rejected'
    ]
    assert len(saved) == 1
    assert len(rejected) == 1
    assert 'candidate budget is exhausted' in str(rejected[0])

    persisted = repository.list_candidates(spec.spec_id)
    assert len(persisted) == spec.candidate_budget
    assert {item.candidate_id for item in persisted} == {
        admitted.candidate_id,
        saved[0].candidate_id,
    }
