from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    build_biquad_filter,
    build_calibration_plan,
)
from htdt.cad_calibration_repository import CadCalibrationRepository
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_extended_search import (
    CadExtendedSearchAxis,
    build_extended_model_capability,
    build_extended_parameter_evidence,
    build_extended_search_spec,
    direction_with_aim_pitch,
    direction_with_horizontal_yaw,
)
from htdt.cad_extended_search_repository import CadExtendedSearchRepository
from htdt.cad_joint_evaluation_authority import ResolvedJointEvaluationInput
from htdt.cad_joint_optimization import (
    JointCandidate,
    JointDecisionValue,
    JointDspVariable,
    JointEvaluationInputRef,
    JointEvaluatorIdentity,
    JointHardConstraintRef,
    JointObjectiveVectorRef,
    JointOptimizationSpec,
    JointRobustnessSpecRef,
    JointRobustnessVariableMapping,
    bind_joint_candidate_evaluation,
    build_joint_candidate,
    build_joint_candidate_selection,
    build_joint_optimization_spec,
    canonical_joint_sha256,
    joint_pareto_front,
    require_joint_decision_materialization,
)
from htdt.cad_joint_optimization_repository import CadJointOptimizationRepository
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    dataset_sha256,
    measurement_sha256,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_objective_authority import ResolvedObjectiveInput
from htdt.cad_objective_models import CadObjectiveInputRef, canonical_objective_sha256
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_repository import SceneRepository
from htdt.cad_robustness_repository import CadRobustnessRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
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
from htdt.optimization_robustness import UncertaintyAxis, build_robustness_spec


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
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
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
    crossovers=(),
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
        crossovers=tuple(crossovers),
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
    evaluator_id: str = 'issue174-fixture-evaluator',
    model_id: str = 'issue174-fixture-model',
    fidelity: str = 'deterministic-fixture',
) -> JointEvaluatorIdentity:
    return JointEvaluatorIdentity(
        evaluator_id=evaluator_id,
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
    # Typed authorities (#392): observation metadata resolves to a persisted
    # observation bound to this measurement; timing evidence resolves to a
    # persisted acquisition context covering it.
    record_observation = build_measurement_observation(
        observation_id=f'obs-{report_id}',
        measurement_id=measurement.measurement_id,
        source_kind='manual',
        observed_at_utc=NOW,
        usable_frequency_band_hz=(20.0, 20000.0),
        polarity_correct=True if polarity else None,
        polarity_confidence=0.99 if polarity else None,
    )
    quality_repository.save_observation(record_observation)
    acquisition = None
    if common_timing:
        context = build_acquisition_context(
            acquisition_context_id=f'issue174-acquisition-{report_id}',
            source_kind='manual',
            subject_measurement_ids=(measurement.measurement_id,),
            timing_reference_valid=True,
            timing_reference_id='loopback-174',
            clock_source='fixture-clock',
            sample_rate_hz=48000,
            delay_correction_s=0.0,
            created_at_utc=NOW,
        )
        quality_repository.save_acquisition_context(context)
        acquisition = acquisition_context_binding(context)
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
        observation=observation_binding(record_observation),
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


def _prediction_fixture_resolver(context, ref):
    """Evaluator-owned fixture evidence pinned by exact source identity."""

    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=canonical_objective_sha256(
            {'source_kind': ref.source_kind, 'source_id': ref.source_id}
        ),
    )


def _spec_metrics_evaluator(context):
    """Evaluator-owned fixture: replay objective metrics from the versioned spec."""

    metrics = context.spec.get('metrics')
    if not isinstance(metrics, list) or not metrics:
        raise ValueError('fixture metrics spec requires a non-empty metrics list')
    resolved = []
    for entry in metrics:
        definition = entry.get('definition')
        resolved.append(
            ObjectiveMetric(
                objective_id=str(entry['objective_id']),
                value=float(entry['value']),
                unit=str(entry['unit']),
                direction=str(entry.get('direction', 'minimize')),
                definition=(
                    ObjectiveDefinition.model_validate(definition)
                    if isinstance(definition, dict)
                    else definition
                ),
            )
        )
    return ObjectiveVector(
        candidate_id=context.evaluation.candidate_id,
        metrics=tuple(resolved),
    )


def _joint_fixture_result_resolver(store):
    """Resolver for evaluator-owned, content-addressed fixture result records.

    ``store`` maps ``source_id`` to an immutable result payload carrying the
    evaluated candidate identity and the metric entries the pinned evaluator
    will emit — an external evaluation-result authority whose input identity
    and output vector are both content-addressed by ``source_sha256``.
    """

    def resolve(context, ref):
        if ref.evidence_class != 'predicted':
            raise ValueError(
                'joint_fixture_result evidence class must be predicted'
            )
        payload = store.get(ref.source_id)
        if payload is None:
            raise ValueError('joint_fixture_result evidence does not exist')
        if payload['candidate_id'] != context.candidate.candidate_id:
            raise ValueError(
                'joint_fixture_result evidence belongs to another candidate'
            )
        return ResolvedJointEvaluationInput(
            ref=ref,
            source_sha256=canonical_joint_sha256(payload),
            authority=payload,
        )

    return resolve


def _joint_fixture_result_evaluator(context):
    """Pinned fixture evaluator: rebuild the vector from the result record."""

    inputs = [
        item
        for item in context.inputs
        if item.ref.source_kind == 'joint_fixture_result'
    ]
    if len(inputs) != 1:
        raise ValueError(
            'joint fixture evaluation requires exactly one result input'
        )
    payload = inputs[0].authority
    if payload['candidate_id'] != context.candidate.candidate_id:
        raise ValueError(
            'joint fixture result belongs to another candidate'
        )
    definitions = {item.objective_id: item for item in context.spec.objectives}
    metrics = tuple(
        ObjectiveMetric(
            objective_id=str(entry['objective_id']),
            value=float(entry['value']),
            unit=str(entry['unit']),
            direction=str(entry['direction']),
            definition=definitions[str(entry['objective_id'])],
        )
        for entry in payload['metrics']
    )
    return ObjectiveVector(
        candidate_id=context.candidate.candidate_id,
        metrics=metrics,
    )


def _robustness_ref(fixture, *, mapping=None) -> JointRobustnessSpecRef:
    spec = fixture.robustness_spec
    return JointRobustnessSpecRef(
        robustness_spec_id=spec.robustness_spec_id,
        robustness_spec_sha256=spec.robustness_spec_sha256,
        variable_mapping=(
            mapping
            if mapping is not None
            else (
                JointRobustnessVariableMapping(
                    joint_variable_id='physical:speaker-fl:x_m',
                    o90_axis_id='speaker-x',
                ),
            )
        ),
        dsp_perturbation_policy='none',
    )


def _build_spec(
    fixture,
    *,
    base_plan=None,
    report=None,
    evaluator=None,
    spec_id='joint-spec-fixture',
    candidate_budget=32,
    physical_search_spec=None,
    extended_search_spec=None,
    robustness=None,
    hard_constraints=(),
    dsp_variables=None,
):
    return build_joint_optimization_spec(
        scene_revision=fixture.revision,
        base_system_variant=fixture.base_variant,
        physical_search_spec=physical_search_spec or fixture.search_spec,
        extended_search_spec=extended_search_spec,
        base_calibration_plan=base_plan or fixture.base_plan,
        measurement_quality_report=report or fixture.quality_report,
        dsp_variables=_dsp_variables() if dsp_variables is None else dsp_variables,
        objectives=_objectives(),
        robustness=robustness or _robustness_ref(fixture),
        evaluator=evaluator or _evaluator(),
        candidate_budget=candidate_budget,
        created_at_utc=NOW,
        spec_id=spec_id,
        hard_constraints=hard_constraints,
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

    # A distinct variant whose materialized speaker x_m stays at the baseline
    # grid minimum: needed wherever a second consistent physical candidate is
    # required (decision values must match the materialized scene, #389).
    home_variant = build_system_variant(
        baseline=revision,
        name='Issue 389 unchanged speaker position',
        role_bindings=roles,
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='hold-speaker-fl',
                entity=speaker,
                role_binding_id='FL',
            ),
        ),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(home_variant)

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
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(search_spec)
    extended_search_repository = CadExtendedSearchRepository(search_repository)
    base_page = generate_cad_candidates(scene_repository, search_spec)
    base_candidate = base_page.candidates[0]
    prediction_ref = f'prediction:{base_candidate.candidate_id}'
    nominal_objective = build_objective_evaluation(
        revision,
        search_spec,
        base_candidate.candidate_id,
        ObjectiveVector(
            candidate_id=base_candidate.candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='fixture.response_error_db',
                    value=1.0,
                    unit='dB',
                ),
            ),
        ),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objective_method': 'fixture-metrics-1',
            'objectives': ['fixture.response_error_db'],
            'metrics': [
                {
                    'objective_id': 'fixture.response_error_db',
                    'value': 1.0,
                    'unit': 'dB',
                    'direction': 'minimize',
                },
            ],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=base_candidate.candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=prediction_ref,
            ),
        ),
    )
    objective_repository = CadObjectiveRepository(
        scene_repository,
        search_repository,
        input_resolvers={'prediction_fixture': _prediction_fixture_resolver},
        vector_evaluators={'fixture-metrics-1': _spec_metrics_evaluator},
    )
    objective_repository.save_evaluation(nominal_objective)
    robustness_repository = CadRobustnessRepository(
        scene_repository=scene_repository,
        search_repository=search_repository,
        objective_repository=objective_repository,
        extended_search_repository=extended_search_repository,
    )
    robustness_spec = build_robustness_spec(
        source_revision=revision,
        search_spec=search_spec,
        candidate=base_candidate,
        candidate_set_sha256=base_page.candidate_set_sha256,
        nominal_objective=nominal_objective,
        nominal_prediction_result_ref=prediction_ref,
        model_id='issue174-fixture-robustness-model',
        model_version='1',
        prediction_provider_id='issue174-fixture-provider',
        fidelity='deterministic-fixture',
        axes=(
            UncertaintyAxis(
                axis_id='speaker-x',
                entity_id='speaker-fl',
                parameter='speaker_x_m',
                unit='m',
                nominal_value=base_candidate.positions['speaker-fl']['x_m'],
                minus_delta=0.05,
                plus_delta=0.05,
            ),
            UncertaintyAxis(
                axis_id='speaker-y',
                entity_id='speaker-fl',
                parameter='speaker_y_m',
                unit='m',
                nominal_value=base_candidate.positions['speaker-fl']['y_m'],
                minus_delta=0.05,
                plus_delta=0.05,
            ),
            UncertaintyAxis(
                axis_id='aim-yaw',
                entity_id='speaker-fl',
                parameter='aim_yaw_deg',
                unit='deg',
                nominal_value=0.0,
                minus_delta=2.0,
                plus_delta=2.0,
            ),
        ),
        software_version='issue174-fixture',
        created_at_utc=NOW,
    )
    robustness_repository.save_spec(robustness_spec)

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
        home_variant=home_variant,
        search_repository=search_repository,
        search_spec=search_spec,
        base_page=base_page,
        base_candidate=base_candidate,
        extended_search_repository=extended_search_repository,
        objective_repository=objective_repository,
        robustness_repository=robustness_repository,
        robustness_spec=robustness_spec,
        measurement_repository=measurement_repository,
        measurement=measurement,
        dataset=dataset,
        quality_repository=quality_repository,
        quality_report=quality_report,
        calibration_repository=calibration_repository,
        joint_results={},
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


def _evaluation(
    fixture,
    spec,
    candidate,
    *,
    error: float,
    headroom: float,
    extra_refs=(),
):
    """Bind an evaluation to a content-addressed fixture result record.

    The evaluator-owned result payload is pinned by exact candidate identity
    and content hash, mirroring an external evaluation-result authority: the
    repository resolver must resolve it and the pinned evaluator must
    reproduce the bound ObjectiveVector from it.
    """

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
    payload = {
        'candidate_id': candidate.candidate_id,
        'metrics': [
            {
                'objective_id': metric.objective_id,
                'value': metric.value,
                'unit': metric.unit,
                'direction': metric.direction,
            }
            for metric in vector.metrics
        ],
    }
    record_id = (
        f'joint-fixture-result-{canonical_joint_sha256(payload)[:24]}'
    )
    fixture.joint_results[record_id] = payload
    return bind_joint_candidate_evaluation(
        spec=spec,
        candidate=candidate,
        objective_vector=vector,
        input_refs=(
            JointEvaluationInputRef(
                evidence_class='predicted',
                source_kind='joint_fixture_result',
                source_id=record_id,
                source_sha256=canonical_joint_sha256(payload),
            ),
            *extra_refs,
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

    position_eval = _evaluation(
        fixture, fixture.spec, position, error=2.0, headroom=5.0
    )
    dsp_eval = _evaluation(fixture, fixture.spec, dsp, error=1.0, headroom=6.0)
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
    first_eval = _evaluation(fixture, fixture.spec, first, error=2.0, headroom=5.0)

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
    second_eval = _evaluation(fixture, other_spec, second, error=1.0, headroom=6.0)

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
        fixture,
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

    repository = _repository(fixture)
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
    reopened_search = CadSearchRepository(reopened_scene)
    reopened_extended = CadExtendedSearchRepository(reopened_search)
    reopened = CadJointOptimizationRepository(
        scene_repository=reopened_scene,
        system_variant_repository=reopened_variants,
        calibration_repository=reopened_calibration,
        search_repository=reopened_search,
        extended_search_repository=reopened_extended,
        robustness_repository=CadRobustnessRepository(
            scene_repository=reopened_scene,
            search_repository=reopened_search,
            objective_repository=CadObjectiveRepository(
                reopened_scene,
                reopened_search,
                input_resolvers={
                    'prediction_fixture': _prediction_fixture_resolver,
                },
                vector_evaluators={
                    'fixture-metrics-1': _spec_metrics_evaluator,
                },
            ),
            extended_search_repository=reopened_extended,
        ),
        input_resolvers={
            'joint_fixture_result': _joint_fixture_result_resolver(
                fixture.joint_results
            ),
        },
        vector_evaluators={
            'issue174-fixture-evaluator': _joint_fixture_result_evaluator,
        },
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
    repository = _repository(fixture)
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
    repository = _repository(fixture)
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
        search_repository=fixture.search_repository,
        extended_search_repository=fixture.extended_search_repository,
        robustness_repository=fixture.robustness_repository,
        input_resolvers={
            'joint_fixture_result': _joint_fixture_result_resolver(
                fixture.joint_results
            ),
        },
        vector_evaluators={
            'issue174-fixture-evaluator': _joint_fixture_result_evaluator,
        },
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

    overflow = _position_candidate(spec, fixture.home_variant, 1.0)
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
    contender_a = _position_candidate(spec, fixture.home_variant, 1.0)
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


def _resigned(spec: JointOptimizationSpec, **updates) -> JointOptimizationSpec:
    """Return a mutated spec whose semantic_sha256 is honestly recomputed.

    This produces the strongest forgery shape: an attacker-controlled payload
    that still passes the model-level self-hash check.
    """

    mutated = spec.model_copy(update=updates)
    unsigned = mutated.model_copy(update={'semantic_sha256': '0' * 64})
    return unsigned.model_copy(
        update={
            'semantic_sha256': canonical_joint_sha256(
                unsigned.semantic_payload()
            )
        }
    )


def _resigned_candidate(candidate: JointCandidate, **updates) -> JointCandidate:
    """Return a mutated candidate whose candidate_sha256/id are honestly recomputed.

    Same strongest-forgery shape as ``_resigned``: an attacker-controlled
    payload that still passes the model-level self-hash and deterministic-ID
    checks.
    """

    mutated = candidate.model_copy(update=updates)
    unsigned = mutated.model_copy(update={'candidate_sha256': '0' * 64})
    digest = canonical_joint_sha256(unsigned.semantic_payload())
    return unsigned.model_copy(
        update={
            'candidate_sha256': digest,
            'candidate_id': f'joint-candidate-{digest[:24]}',
        }
    )


def _other_search_spec(fixture):
    spec, _estimate = build_cad_search_spec(
        fixture.revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='y',
                min_m=1.0,
                max_m=2.0,
                step_m=1.0,
            ),
        ),
        candidate_limit=8,
        name='Issue 388 unrelated physical authority',
    )
    fixture.search_repository.save(spec)
    return spec


def _save_extended_spec(fixture, *, base_spec, base_page):
    evidence = build_extended_parameter_evidence(
        parameter='aim_yaw_deg',
        model_id='issue388-fixture-aim-model',
        model_version='1',
        evidence_scope='synthetic_fixture',
        tested_min_deg=-45.0,
        tested_max_deg=45.0,
        source_kind='synthetic_fixture',
        source_id=(
            f'issue388-fixture-aim-evidence:{base_spec.search_spec_id}'
        ),
        source_sha256=sha256(
            f'issue388-aim-evidence:{base_spec.search_spec_id}'.encode(
                'utf-8'
            )
        ).hexdigest(),
        detail='issue388 fixture aim evidence',
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_parameter_evidence(evidence)
    capability = build_extended_model_capability(
        model_id='issue388-fixture-aim-model',
        model_version='1',
        evidence_scope='synthetic_fixture',
        supported_parameters=('aim_yaw_deg',),
        parameter_evidence=(evidence,),
        detail=f'issue388 fixture aim capability for {base_spec.search_spec_id}',
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_capability(capability)
    extended = build_extended_search_spec(
        source_revision=fixture.revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=len(base_page.candidates),
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='speaker-fl',
                parameter='aim_yaw_deg',
                min_value=-5.0,
                max_value=5.0,
                step=5.0,
            ),
        ),
        candidate_limit=64,
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_spec(extended)
    return extended


def test_spec_rejects_nonexistent_or_mismatched_physical_search_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    unknown = _resigned(
        fixture.spec,
        physical_search_spec_id='search-spec-does-not-exist',
    )
    with pytest.raises(ValueError, match='unknown physical SearchSpec'):
        repository.save_spec(unknown)

    mismatched = _resigned(
        fixture.spec,
        physical_search_spec_sha256='0' * 64,
    )
    with pytest.raises(ValueError, match='physical SearchSpec hash mismatch'):
        repository.save_spec(mismatched)

    # A real persisted SearchSpec that is not the declared derivation authority
    # is still rejected when the physical variables are rederived.
    other = _other_search_spec(fixture)
    unrelated = _resigned(
        fixture.spec,
        physical_search_spec_id=other.search_spec_id,
        physical_search_spec_sha256=other.search_spec_sha256,
    )
    with pytest.raises(ValueError, match='not the canonical derivation'):
        repository.save_spec(unrelated)


def test_spec_rejects_modified_physical_bounds_with_recomputed_hash(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    widened = fixture.spec.physical_variables[0].model_copy(
        update={'maximum': fixture.spec.physical_variables[0].maximum + 1.0}
    )
    forged = _resigned(
        fixture.spec,
        physical_variables=(widened,) + fixture.spec.physical_variables[1:],
    )
    with pytest.raises(ValueError, match='not the canonical derivation'):
        repository.save_spec(forged)

    slower = fixture.spec.physical_variables[0].model_copy(
        update={'step': fixture.spec.physical_variables[0].step * 2.0}
    )
    forged_step = _resigned(
        fixture.spec,
        physical_variables=(slower,) + fixture.spec.physical_variables[1:],
    )
    with pytest.raises(ValueError, match='not the canonical derivation'):
        repository.save_spec(forged_step)


def test_spec_rejects_unrelated_extended_search_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    unknown = _resigned(
        fixture.spec,
        extended_search_spec_id='extended-spec-does-not-exist',
        extended_search_spec_sha256='1' * 64,
    )
    with pytest.raises(ValueError, match='unknown extended SearchSpec'):
        repository.save_spec(unknown)

    extended = _save_extended_spec(
        fixture,
        base_spec=fixture.search_spec,
        base_page=fixture.base_page,
    )
    mismatched = _resigned(
        fixture.spec,
        extended_search_spec_id=extended.extended_search_id,
        extended_search_spec_sha256='1' * 64,
    )
    with pytest.raises(ValueError, match='extended SearchSpec hash mismatch'):
        repository.save_spec(mismatched)

    # An extended spec persisted against a different base search is unrelated.
    other_base = _other_search_spec(fixture)
    other_page = generate_cad_candidates(fixture.scene_repository, other_base)
    unrelated_extended = _save_extended_spec(
        fixture,
        base_spec=other_base,
        base_page=other_page,
    )
    forged = _resigned(
        fixture.spec,
        extended_search_spec_id=unrelated_extended.extended_search_id,
        extended_search_spec_sha256=unrelated_extended.extended_search_sha256,
    )
    with pytest.raises(ValueError, match='exact base physical search'):
        repository.save_spec(forged)


def test_spec_with_exact_extended_authority_saves_and_reopens(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    extended = _save_extended_spec(
        fixture,
        base_spec=fixture.search_spec,
        base_page=fixture.base_page,
    )
    spec = _build_spec(
        fixture,
        extended_search_spec=extended,
        spec_id='joint-spec-extended-388',
    )

    variable_ids = {item.variable_id for item in spec.physical_variables}
    assert 'physical:speaker-fl:aim_yaw_deg' in variable_ids

    repository.save_spec(spec)
    assert repository.get_spec(spec.spec_id) == spec
    assert repository.list_specs(DOCUMENT_ID) == (spec,)


def test_spec_rejects_missing_or_fabricated_hard_constraint_refs(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    # Fabricated workspace hash.
    forged_workspace = _resigned(
        fixture.spec,
        hard_constraints=(
            JointHardConstraintRef(
                constraint_kind='physical_search_workspace',
                authority_id=fixture.search_spec.search_spec_id,
                authority_sha256='0' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match='does not resolve'):
        repository.save_spec(forged_workspace)

    # Fabricated device-capability authority.
    fabricated_capability = _resigned(
        fixture.spec,
        hard_constraints=fixture.spec.hard_constraints
        + (
            JointHardConstraintRef(
                constraint_kind='device_capability',
                authority_id='capability-does-not-exist',
                authority_sha256='2' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match='does not resolve'):
        repository.save_spec(fabricated_capability)

    # External refs have no resolvable authority in this slice: fail closed.
    external = _resigned(
        fixture.spec,
        hard_constraints=fixture.spec.hard_constraints
        + (
            JointHardConstraintRef(
                constraint_kind='external',
                authority_id='policy-external-1',
                authority_sha256='3' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match='no .*resolvable authority'):
        repository.save_spec(external)

    # Dropping a declared constraint ref is equally non-canonical.
    dropped = _resigned(
        fixture.spec,
        hard_constraints=fixture.spec.hard_constraints[1:],
    )
    with pytest.raises(ValueError, match='not the canonical compilation'):
        repository.save_spec(dropped)


def test_spec_rejects_unknown_or_unrelated_robustness_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)

    unknown_ref = JointRobustnessSpecRef(
        robustness_spec_id='o90-spec-does-not-exist',
        robustness_spec_sha256='4' * 64,
        variable_mapping=fixture.spec.robustness.variable_mapping,
        dsp_perturbation_policy='none',
    )
    with pytest.raises(ValueError, match='unknown O90 RobustnessSpec'):
        repository.save_spec(_resigned(fixture.spec, robustness=unknown_ref))

    mismatched_ref = JointRobustnessSpecRef(
        robustness_spec_id=fixture.robustness_spec.robustness_spec_id,
        robustness_spec_sha256='4' * 64,
        variable_mapping=fixture.spec.robustness.variable_mapping,
        dsp_perturbation_policy='none',
    )
    with pytest.raises(ValueError, match='O90 RobustnessSpec hash mismatch'):
        repository.save_spec(_resigned(fixture.spec, robustness=mismatched_ref))

    # Mapping to an axis that does not exist on the resolved O90 spec.
    unknown_axis = _robustness_ref(
        fixture,
        mapping=(
            JointRobustnessVariableMapping(
                joint_variable_id='physical:speaker-fl:x_m',
                o90_axis_id='axis-does-not-exist',
            ),
        ),
    )
    with pytest.raises(ValueError, match='unknown robustness axis'):
        repository.save_spec(_resigned(fixture.spec, robustness=unknown_axis))

    # Mapping to a real axis whose subject (unit) does not match the variable.
    unrelated_axis = _robustness_ref(
        fixture,
        mapping=(
            JointRobustnessVariableMapping(
                joint_variable_id='physical:speaker-fl:x_m',
                o90_axis_id='aim-yaw',
            ),
        ),
    )
    with pytest.raises(ValueError, match='does not match the joint variable subject'):
        repository.save_spec(_resigned(fixture.spec, robustness=unrelated_axis))

    # Mapping to a real axis whose parameter is unrelated to the joint variable.
    other_parameter = _robustness_ref(
        fixture,
        mapping=(
            JointRobustnessVariableMapping(
                joint_variable_id='physical:speaker-fl:x_m',
                o90_axis_id='speaker-y',
            ),
        ),
    )
    with pytest.raises(ValueError, match='unrelated to the joint variable'):
        repository.save_spec(_resigned(fixture.spec, robustness=other_parameter))

    # Mapping must name a physical joint decision variable.
    not_a_variable = JointRobustnessSpecRef(
        robustness_spec_id=fixture.robustness_spec.robustness_spec_id,
        robustness_spec_sha256=fixture.robustness_spec.robustness_spec_sha256,
        variable_mapping=(
            JointRobustnessVariableMapping(
                joint_variable_id='dsp:gain',
                o90_axis_id='speaker-x',
            ),
        ),
        dsp_perturbation_policy='none',
    )
    with pytest.raises(ValueError, match='does not resolve to a physical'):
        repository.save_spec(_resigned(fixture.spec, robustness=not_a_variable))


def test_get_spec_replays_authority_validation_on_tampered_persistence(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    forged = _resigned(
        fixture.spec,
        physical_search_spec_sha256='0' * 64,
    )
    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_joint_optimization_specs
            SET payload_json=?, semantic_sha256=?
            WHERE spec_id=?
            """,
            (
                forged.model_dump_json(),
                forged.semantic_sha256,
                fixture.spec.spec_id,
            ),
        )

    with pytest.raises(ValueError, match='physical SearchSpec hash mismatch'):
        repository.get_spec(fixture.spec.spec_id)
    with pytest.raises(ValueError, match='physical SearchSpec hash mismatch'):
        repository.list_specs(DOCUMENT_ID)


def test_get_spec_replays_authority_after_authority_row_removed(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_search_specs WHERE search_spec_id=?',
            (fixture.search_spec.search_spec_id,),
        )

    with pytest.raises(ValueError, match='unknown physical SearchSpec'):
        repository.get_spec(fixture.spec.spec_id)


def test_candidate_rejects_rehashed_decisions_outside_declared_bounds(
    tmp_path: Path,
) -> None:
    """Self-hash-valid candidates still replay the canonical decision vector."""

    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)

    out_of_bounds = _resigned_candidate(
        candidate,
        decision_vector=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:x_m',
                value=9.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match='outside declared decision bounds'):
        repository.save_candidate(out_of_bounds)

    off_grid = _resigned_candidate(
        candidate,
        decision_vector=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:x_m',
                value=1.5,
            ),
        ),
    )
    with pytest.raises(ValueError, match='not aligned to declared decision step'):
        repository.save_candidate(off_grid)

    unknown_variable = _resigned_candidate(
        candidate,
        decision_vector=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:y_m',
                value=1.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match='unknown physical decision variable'):
        repository.save_candidate(unknown_variable)


def test_candidate_rejects_decision_mismatched_with_materialized_variant(
    tmp_path: Path,
) -> None:
    """An in-grid value that the variant does not materialize is rejected."""

    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    # In-bounds, on-grid, bound to a real changed SystemVariant — but the
    # variant materializes speaker-fl at x_m=2.0, not the claimed 1.0.
    inconsistent = _position_candidate(fixture.spec, fixture.moved_variant, 1.0)
    with pytest.raises(
        ValueError,
        match='does not match the materialized SystemVariant scene',
    ):
        repository.save_candidate(inconsistent)

    # The consistent mirror image persists and reopens unchanged.
    consistent = _position_candidate(fixture.spec, fixture.home_variant, 1.0)
    repository.save_candidate(consistent)
    assert repository.get_candidate(consistent.candidate_id) == consistent


def test_candidate_rejects_decision_mismatched_with_calibration_plan(
    tmp_path: Path,
) -> None:
    """DSP decision values must equal the exact candidate CalibrationPlan."""

    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    plan = _save_plan(
        fixture,
        plan_id='mismatch-gain-plan-389',
        channel=_channel(gain_db=1.0),
    )
    mismatched_gain = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 2.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    with pytest.raises(
        ValueError,
        match='does not match the candidate CalibrationPlan',
    ):
        repository.save_candidate(mismatched_gain)

    polarity_plan = _save_plan(
        fixture,
        plan_id='mismatch-polarity-plan-389',
        channel=_channel(polarity='inverted'),
    )
    mismatched_polarity = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:polarity', 'normal'),),
        calibration_plan=polarity_plan,
        measurement_quality_report=fixture.quality_report,
    )
    with pytest.raises(
        ValueError,
        match='does not match the candidate CalibrationPlan',
    ):
        repository.save_candidate(mismatched_polarity)


def test_candidate_rejects_decision_mismatched_with_peq_and_crossover_settings(
    tmp_path: Path,
) -> None:
    """PEQ filter and crossover decision values resolve to exact plan rows."""

    fixture = _fixture(tmp_path)
    dsp_variables = _dsp_variables() + (
        JointDspVariable(
            variable_id='dsp:peq-freq',
            channel_id='FL',
            parameter='peq_frequency_hz',
            filter_id='peq-1',
            minimum=20.0,
            maximum=20000.0,
            step=1.0,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
        JointDspVariable(
            variable_id='dsp:xo-freq',
            channel_id='FL',
            parameter='crossover_frequency_hz',
            crossover_index=0,
            minimum=40.0,
            maximum=200.0,
            step=1.0,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
        JointDspVariable(
            variable_id='dsp:xo-order',
            channel_id='FL',
            parameter='crossover_order',
            crossover_index=0,
            minimum=2.0,
            maximum=4.0,
            step=2.0,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
    )
    spec = _build_spec(
        fixture,
        dsp_variables=dsp_variables,
        spec_id='joint-spec-389-peq-xo',
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    plan = _save_plan(
        fixture,
        plan_id='peq-xo-plan-389',
        channel=_channel(
            peq=(_peq('peq-1', frequency_hz=100.0, gain_db=1.0),),
            crossovers=(
                CadCrossoverSetting(
                    crossover_type='low_pass',
                    frequency_hz=80.0,
                    filter_order=4,
                ),
            ),
        ),
    )

    consistent = build_joint_candidate(
        spec=spec,
        physical_system_variant=fixture.base_variant,
        decisions=(
            _dsp_decision('dsp:peq-freq', 100.0),
            _dsp_decision('dsp:xo-freq', 80.0),
            _dsp_decision('dsp:xo-order', 4),
        ),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository.save_candidate(consistent)
    assert repository.get_candidate(consistent.candidate_id) == consistent

    for variable_id, value in (
        ('dsp:peq-freq', 150.0),
        ('dsp:xo-freq', 120.0),
        ('dsp:xo-order', 2),
    ):
        mismatched = _resigned_candidate(
            consistent,
            decision_vector=(
                JointDecisionValue(
                    domain='dsp',
                    variable_id=variable_id,
                    value=value,
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match='does not match the candidate CalibrationPlan',
        ):
            repository.save_candidate(mismatched)


def test_candidate_recomputes_eligibility_for_rehashed_blocked_candidate(
    tmp_path: Path,
) -> None:
    """A BLOCKED candidate cannot be relabeled ELIGIBLE by rehashing."""

    fixture = _fixture(tmp_path)
    report = _save_quality(
        fixture.quality_repository,
        fixture.measurement,
        fixture.dataset,
        report_id='quality-389-no-timing',
        common_timing=False,
    )
    base_plan = _save_plan(
        fixture,
        plan_id='base-389-no-timing',
        report=report,
    )
    spec = _build_spec(
        fixture,
        base_plan=base_plan,
        report=report,
        spec_id='joint-spec-389-no-timing',
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    delay_plan = _save_plan(
        fixture,
        plan_id='delay-389-no-timing',
        report=report,
        channel=_channel(delay_s=0.005),
    )
    blocked = build_joint_candidate(
        spec=spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:delay', 0.005),),
        calibration_plan=delay_plan,
        measurement_quality_report=report,
    )
    assert blocked.eligibility_state == 'BLOCKED'
    assert blocked.blocked_reasons

    # The canonically blocked candidate itself persists and reopens unchanged.
    repository.save_candidate(blocked)
    assert repository.get_candidate(blocked.candidate_id) == blocked

    relabeled = _resigned_candidate(
        blocked,
        eligibility_state='ELIGIBLE',
        blocked_reasons=(),
    )
    assert relabeled.candidate_id != blocked.candidate_id
    with pytest.raises(ValueError, match='not the canonical compilation'):
        repository.save_candidate(relabeled)


def test_candidate_rejects_payload_objective_vector_ref(tmp_path: Path) -> None:
    """Evaluation provenance cannot be smuggled inside a candidate payload."""

    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)

    forged = _resigned_candidate(
        candidate,
        objective_vector_ref=JointObjectiveVectorRef(
            evaluation_binding_id='joint-evaluation-forged-389',
            evaluation_binding_sha256='1' * 64,
            objective_vector_sha256='2' * 64,
        ),
    )
    with pytest.raises(ValueError, match='not the canonical compilation'):
        repository.save_candidate(forged)


def test_candidate_verifies_extended_yaw_decisions_against_materialized_scene(
    tmp_path: Path,
) -> None:
    """Extended-search yaw decisions replay against the materialized entity."""

    fixture = _fixture(tmp_path)
    evidence = tuple(
        build_extended_parameter_evidence(
            parameter=parameter,
            model_id='issue389-fixture-yaw-model',
            model_version='1',
            evidence_scope='synthetic_fixture',
            tested_min_deg=-45.0,
            tested_max_deg=45.0,
            source_kind='synthetic_fixture',
            source_id=f'issue389-fixture-yaw-evidence:{parameter}',
            source_sha256=sha256(
                f'issue389-yaw-evidence:{parameter}'.encode('utf-8')
            ).hexdigest(),
            detail='issue389 fixture yaw evidence',
            created_at_utc=NOW,
        )
        for parameter in ('aim_yaw_deg', 'body_yaw_deg')
    )
    for item in evidence:
        fixture.extended_search_repository.save_parameter_evidence(item)
    capability = build_extended_model_capability(
        model_id='issue389-fixture-yaw-model',
        model_version='1',
        evidence_scope='synthetic_fixture',
        supported_parameters=('aim_yaw_deg', 'body_yaw_deg'),
        parameter_evidence=evidence,
        detail='issue389 fixture yaw capability',
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_capability(capability)
    extended = build_extended_search_spec(
        source_revision=fixture.revision,
        base_spec=fixture.search_spec,
        base_candidate_set_sha256=fixture.base_page.candidate_set_sha256,
        base_candidate_count=len(fixture.base_page.candidates),
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='speaker-fl',
                parameter='aim_yaw_deg',
                min_value=-5.0,
                max_value=5.0,
                step=5.0,
            ),
            CadExtendedSearchAxis(
                entity_id='speaker-fl',
                parameter='body_yaw_deg',
                min_value=-10.0,
                max_value=10.0,
                step=5.0,
            ),
        ),
        candidate_limit=256,
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_spec(extended)
    spec = _build_spec(
        fixture,
        extended_search_spec=extended,
        spec_id='joint-spec-389-yaw',
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    speaker = fixture.revision.document.entity('speaker-fl')
    rotated = speaker.model_copy(
        update={
            'aim_xyz': direction_with_horizontal_yaw(speaker.aim_xyz, 5.0),
            'orientation': quaternion_from_euler_deg(
                yaw_deg=5.0,
                pitch_deg=0.0,
                roll_deg=0.0,
            ),
        }
    )
    yaw_variant = build_system_variant(
        baseline=fixture.revision,
        name='Issue 389 yaw speaker',
        role_bindings=(ChannelRoleBinding(role_id='FL', display_name='Front Left'),),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='yaw-speaker-fl',
                entity=rotated,
                role_binding_id='FL',
            ),
        ),
        created_at_utc=NOW,
    )
    fixture.system_variant_repository.save_variant(yaw_variant)

    consistent = build_joint_candidate(
        spec=spec,
        physical_system_variant=yaw_variant,
        decisions=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:aim_yaw_deg',
                value=5.0,
            ),
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:body_yaw_deg',
                value=5.0,
            ),
        ),
    )
    repository.save_candidate(consistent)
    assert repository.get_candidate(consistent.candidate_id) == consistent

    mismatched_aim = build_joint_candidate(
        spec=spec,
        physical_system_variant=yaw_variant,
        decisions=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:aim_yaw_deg',
                value=-5.0,
            ),
        ),
    )
    with pytest.raises(
        ValueError,
        match='does not match the materialized SystemVariant scene',
    ):
        repository.save_candidate(mismatched_aim)

    mismatched_body = build_joint_candidate(
        spec=spec,
        physical_system_variant=yaw_variant,
        decisions=(
            JointDecisionValue(
                domain='physical',
                variable_id='physical:speaker-fl:body_yaw_deg',
                value=-5.0,
            ),
        ),
    )
    with pytest.raises(
        ValueError,
        match='does not match the materialized SystemVariant scene',
    ):
        repository.save_candidate(mismatched_body)


def test_get_candidate_replays_canonical_rebuild_on_tampered_payload(
    tmp_path: Path,
) -> None:
    """Reads re-derive eligibility and materialization; payload is never trusted."""

    fixture = _fixture(tmp_path)
    plan = _save_plan(
        fixture,
        plan_id='tamper-gain-plan-389',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    repository.save_candidate(candidate)

    # A self-hash-valid payload whose decision value does not match the
    # persisted CalibrationPlan is rejected on every read path.
    forged = _resigned_candidate(
        candidate,
        decision_vector=(
            JointDecisionValue(domain='dsp', variable_id='dsp:gain', value=2.0),
        ),
    )
    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_joint_candidates
            SET payload_json=?
            WHERE candidate_id=?
            """,
            (forged.model_dump_json(), candidate.candidate_id),
        )
    with pytest.raises(
        ValueError,
        match='does not match the candidate CalibrationPlan',
    ):
        repository.get_candidate(candidate.candidate_id)
    with pytest.raises(
        ValueError,
        match='does not match the candidate CalibrationPlan',
    ):
        repository.list_candidates(fixture.spec.spec_id)

    # Eligibility smuggled into the payload is equally non-canonical on read.
    relabeled = _resigned_candidate(
        candidate,
        eligibility_state='BLOCKED',
        blocked_reasons=('forged blocked reason',),
    )
    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_joint_candidates
            SET payload_json=?
            WHERE candidate_id=?
            """,
            (relabeled.model_dump_json(), candidate.candidate_id),
        )
    with pytest.raises(ValueError, match='not the canonical compilation'):
        repository.get_candidate(candidate.candidate_id)




def _resigned_evaluation(evaluation, **updates):
    """Return a mutated evaluation whose binding hash is honestly recomputed.

    Same strongest-forgery shape as ``_resigned``: a caller-controlled
    payload that still passes the model-level self-hash check.
    """

    mutated = evaluation.model_copy(update=updates)
    mutated = mutated.model_copy(
        update={
            'objective_vector_sha256': canonical_joint_sha256(
                mutated.objective_vector.identity_payload()
            ),
        }
    )
    digest = canonical_joint_sha256(mutated.semantic_payload())
    return mutated.model_copy(
        update={
            'evaluation_binding_sha256': digest,
            'evaluation_binding_id': f'joint-evaluation-{digest[:24]}',
        }
    )


def _result_ref(evaluation) -> JointEvaluationInputRef:
    return next(
        ref
        for ref in evaluation.input_refs
        if ref.source_kind == 'joint_fixture_result'
    )


def _builtin_evidence_refs(fixture, spec, candidate, plan):
    return (
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='joint_candidate',
            source_id=candidate.candidate_id,
            source_sha256=candidate.candidate_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='joint_optimization_spec',
            source_id=spec.spec_id,
            source_sha256=spec.semantic_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='scene_revision',
            source_id=spec.scene_revision_id,
            source_sha256=fixture.revision.content_hash,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='system_variant',
            source_id=candidate.physical_system_variant_id,
            source_sha256=candidate.physical_system_variant_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='calibration_plan',
            source_id=plan.plan_id,
            source_sha256=plan.plan_semantic_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='measured',
            source_kind='cad_measurement',
            source_id=spec.dsp_authority.source_measurement_id,
            source_sha256=measurement_sha256(fixture.measurement),
        ),
        JointEvaluationInputRef(
            evidence_class='measured',
            source_kind='cad_measurement_dataset',
            source_id=spec.dsp_authority.source_dataset_id,
            source_sha256=dataset_sha256(fixture.dataset),
        ),
        JointEvaluationInputRef(
            evidence_class='measured',
            source_kind='measurement_quality_report',
            source_id=spec.dsp_authority.measurement_quality_report_id,
            source_sha256=fixture.quality_report.report_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='physical_search_spec',
            source_id=spec.physical_search_spec_id,
            source_sha256=fixture.search_spec.search_spec_sha256,
        ),
        JointEvaluationInputRef(
            evidence_class='derived',
            source_kind='robustness_spec',
            source_id=spec.robustness.robustness_spec_id,
            source_sha256=fixture.robustness_spec.robustness_spec_sha256,
        ),
        # Declared-only hypothesis claims stay admissible beside resolved
        # evidence; the binding hash pins their caller-attested identity.
        JointEvaluationInputRef(
            evidence_class='hypothesis',
            source_kind='issue390_stated_assumption',
            source_id='assumption-1',
            source_sha256='7' * 64,
        ),
    )


def test_evaluation_resolves_builtin_evidence_and_reproduces_vector(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    plan = _save_plan(
        fixture,
        plan_id='eval-authority-plan-390',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository.save_candidate(candidate)

    evaluation = _evaluation(
        fixture,
        fixture.spec,
        candidate,
        error=1.0,
        headroom=6.0,
        extra_refs=_builtin_evidence_refs(fixture, fixture.spec, candidate, plan),
    )
    persisted = repository.save_evaluation(evaluation)

    assert persisted == evaluation
    assert repository.get_evaluation(evaluation.evaluation_binding_id) == evaluation
    assert repository.list_evaluations(fixture.spec.spec_id) == (evaluation,)


def test_evaluation_rejects_fabricated_or_unresolvable_input_refs(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )
    result_ref = _result_ref(evaluation)

    # A fabricated source identity: the resolver finds no such record.
    forged = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref.model_copy(
                update={'source_id': 'joint-fixture-result-missing'}
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='evidence does not exist'):
        repository.save_evaluation(forged)

    # A declared semantic hash the resolver cannot reproduce.
    forged_sha = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref.model_copy(update={'source_sha256': '0' * 64}),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='input source hash mismatch'):
        repository.save_evaluation(forged_sha)

    # A measured input whose source kind has no registered resolver.
    unresolvable = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref,
            JointEvaluationInputRef(
                evidence_class='measured',
                source_kind='unregistered_measurement_kind',
                source_id='measurement-x',
                source_sha256='1' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='no registered authority'):
        repository.save_evaluation(unresolvable)


def test_evaluation_rejects_evidence_bound_to_another_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )
    result_ref = _result_ref(evaluation)
    other = _position_candidate(fixture.spec, fixture.moved_variant, 1.0)

    # A real evaluator-owned result record pinned to a different candidate.
    foreign_payload = dict(
        fixture.joint_results[result_ref.source_id],
        candidate_id=other.candidate_id,
    )
    fixture.joint_results['joint-fixture-result-foreign'] = foreign_payload
    forged = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref.model_copy(
                update={
                    'source_id': 'joint-fixture-result-foreign',
                    'source_sha256': canonical_joint_sha256(foreign_payload),
                }
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='belongs to another candidate'):
        repository.save_evaluation(forged)

    # A built-in ref naming a different candidate's evidence.
    forged_candidate = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref,
            JointEvaluationInputRef(
                evidence_class='derived',
                source_kind='joint_candidate',
                source_id=other.candidate_id,
                source_sha256=other.candidate_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='exact evaluated candidate'):
        repository.save_evaluation(forged_candidate)

    # The candidate's physical SystemVariant is moved_variant; the base
    # variant is real persisted evidence that belongs to another candidate.
    forged_variant = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            result_ref,
            JointEvaluationInputRef(
                evidence_class='derived',
                source_kind='system_variant',
                source_id=fixture.base_variant.variant_id,
                source_sha256=fixture.base_variant.variant_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='exact candidate physical'):
        repository.save_evaluation(forged_variant)


def test_evaluation_rejects_calibration_plan_evidence_without_dsp_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )

    forged = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=evaluation.objective_vector,
        input_refs=(
            _result_ref(evaluation),
            JointEvaluationInputRef(
                evidence_class='derived',
                source_kind='calibration_plan',
                source_id=fixture.base_plan.plan_id,
                source_sha256=fixture.base_plan.plan_semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='requires the candidate to carry'):
        repository.save_evaluation(forged)


def test_evaluation_rejects_rehashed_metric_tamper(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )
    repository.save_evaluation(evaluation)

    # Change one objective value and honestly recompute every hash: the
    # pinned evaluator still derives the recorded evidence output.
    forged_vector = evaluation.objective_vector.model_copy(
        update={
            'metrics': (
                evaluation.objective_vector.metrics[0].model_copy(
                    update={'value': 99.0}
                ),
                evaluation.objective_vector.metrics[1],
            )
        }
    )
    forged = bind_joint_candidate_evaluation(
        spec=fixture.spec,
        candidate=candidate,
        objective_vector=forged_vector,
        input_refs=evaluation.input_refs,
        created_at_utc=evaluation.created_at_utc,
    )
    assert forged.evaluation_binding_id != evaluation.evaluation_binding_id
    with pytest.raises(ValueError, match='does not reproduce'):
        repository.save_evaluation(forged)

    # A metric carrying a forged ObjectiveDefinition fails the canonical
    # binding replay before evaluator invocation.
    forged_definition = evaluation.objective_vector.model_copy(
        update={
            'metrics': (
                evaluation.objective_vector.metrics[0].model_copy(
                    update={
                        'definition': (
                            evaluation.objective_vector.metrics[0]
                            .definition.model_copy(
                                update={'comparison_model_id': 'forged-model'}
                            )
                        )
                    }
                ),
                evaluation.objective_vector.metrics[1],
            )
        }
    )
    with pytest.raises(ValueError, match='definition/model/fidelity mismatch'):
        bind_joint_candidate_evaluation(
            spec=fixture.spec,
            candidate=candidate,
            objective_vector=forged_definition,
            input_refs=evaluation.input_refs,
            created_at_utc=evaluation.created_at_utc,
        )


def test_evaluation_rejects_evaluator_identity_mismatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )

    # Evaluator/model/fidelity must equal the spec-pinned identity exactly.
    mismatched = _resigned_evaluation(
        evaluation,
        evaluator=_evaluator(model_id='issue174-other-fixture-model'),
    )
    with pytest.raises(ValueError, match='evaluator authority mismatch'):
        repository.save_evaluation(mismatched)

    # A spec pinned to an evaluator with no registered replay fails closed.
    other_spec = _build_spec(
        fixture,
        evaluator=_evaluator(evaluator_id='issue390-unregistered-evaluator'),
        spec_id='joint-spec-unregistered-evaluator',
    )
    repository.save_spec(other_spec)
    other_candidate = _position_candidate(other_spec, fixture.moved_variant, 2.0)
    repository.save_candidate(other_candidate)
    unresolvable = _evaluation(
        fixture, other_spec, other_candidate, error=1.0, headroom=6.0
    )
    with pytest.raises(ValueError, match='not registered for replay'):
        repository.save_evaluation(unresolvable)


def test_evaluation_rejects_noncanonical_binding_payload(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture,
        fixture.spec,
        candidate,
        error=1.0,
        headroom=6.0,
        extra_refs=(
            JointEvaluationInputRef(
                evidence_class='hypothesis',
                source_kind='issue390_stated_assumption',
                source_id='assumption-1',
                source_sha256='7' * 64,
            ),
        ),
    )
    assert len(evaluation.input_refs) == 2

    # Honestly rehashed but carrying input refs out of canonical order: the
    # record is not the canonical binding of its declared inputs.
    forged = _resigned_evaluation(
        evaluation,
        input_refs=tuple(reversed(evaluation.input_refs)),
    )
    with pytest.raises(ValueError, match='not the canonical binding'):
        repository.save_evaluation(forged)


def test_get_evaluation_detects_disappeared_and_tampered_evidence(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    plan = _save_plan(
        fixture,
        plan_id='eval-evidence-plan-390',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture,
        fixture.spec,
        candidate,
        error=1.0,
        headroom=6.0,
        extra_refs=(
            JointEvaluationInputRef(
                evidence_class='measured',
                source_kind='measurement_quality_report',
                source_id=fixture.spec.dsp_authority.measurement_quality_report_id,
                source_sha256=fixture.quality_report.report_sha256,
            ),
        ),
    )
    repository.save_evaluation(evaluation)
    result_ref = _result_ref(evaluation)

    # Tamper the evaluator-owned result record: resolution can no longer
    # reproduce the declared semantic hash.
    fixture.joint_results[result_ref.source_id] = dict(
        fixture.joint_results[result_ref.source_id],
        candidate_id='joint-candidate-foreign',
    )
    with pytest.raises(ValueError, match='belongs to another candidate'):
        repository.get_evaluation(evaluation.evaluation_binding_id)

    # Disappeared evaluator evidence fails the read closed.
    del fixture.joint_results[result_ref.source_id]
    with pytest.raises(ValueError, match='evidence does not exist'):
        repository.get_evaluation(evaluation.evaluation_binding_id)
    with pytest.raises(ValueError, match='evidence does not exist'):
        repository.list_evaluations(fixture.spec.spec_id)


def test_get_evaluation_detects_disappeared_persisted_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    plan = _save_plan(
        fixture,
        plan_id='eval-authority-loss-plan-390',
        channel=_channel(gain_db=1.0),
    )
    candidate = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )
    repository.save_evaluation(evaluation)

    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_measurement_quality_reports WHERE report_id=?',
            (fixture.quality_report.report_id,),
        )

    with pytest.raises(ValueError, match='MeasurementQualityReport'):
        repository.get_evaluation(evaluation.evaluation_binding_id)


def test_get_evaluation_replays_authority_on_tampered_persistence(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)
    candidate = _position_candidate(fixture.spec, fixture.moved_variant, 2.0)
    repository.save_candidate(candidate)
    evaluation = _evaluation(
        fixture, fixture.spec, candidate, error=1.0, headroom=6.0
    )
    repository.save_evaluation(evaluation)

    # Tamper the persisted metrics and honestly rehash the row: read-side
    # replay derives the vector from resolved evidence and fails closed.
    forged_vector = evaluation.objective_vector.model_copy(
        update={
            'metrics': (
                evaluation.objective_vector.metrics[0].model_copy(
                    update={'value': 42.0}
                ),
                evaluation.objective_vector.metrics[1],
            )
        }
    )
    forged = _resigned_evaluation(
        evaluation,
        objective_vector=forged_vector,
    )
    with closing(
        sqlite3.connect(fixture.scene_repository.path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_joint_candidate_evaluations
            SET payload_json=?, evaluation_binding_sha256=?,
                evaluation_binding_id=?
            WHERE evaluation_binding_id=?
            """,
            (
                forged.model_dump_json(),
                forged.evaluation_binding_sha256,
                forged.evaluation_binding_id,
                evaluation.evaluation_binding_id,
            ),
        )

    with pytest.raises(ValueError, match='does not reproduce'):
        repository.get_evaluation(forged.evaluation_binding_id)
    with pytest.raises(ValueError, match='does not reproduce'):
        repository.list_evaluations(fixture.spec.spec_id)


def test_pareto_front_consumes_only_canonically_bound_evaluations(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    repository = _repository(fixture)
    repository.save_spec(fixture.spec)

    position = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.moved_variant,
        decisions=(_physical_decision(),),
    )
    plan = _save_plan(
        fixture,
        plan_id='pareto-persist-gain-390',
        channel=_channel(gain_db=1.0),
    )
    dsp = build_joint_candidate(
        spec=fixture.spec,
        physical_system_variant=fixture.base_variant,
        decisions=(_dsp_decision('dsp:gain', 1.0),),
        calibration_plan=plan,
        measurement_quality_report=fixture.quality_report,
    )
    repository.save_candidate(position)
    repository.save_candidate(dsp)
    repository.save_evaluation(
        _evaluation(fixture, fixture.spec, position, error=2.0, headroom=5.0)
    )
    dsp_evaluation = _evaluation(
        fixture, fixture.spec, dsp, error=1.0, headroom=6.0
    )
    repository.save_evaluation(dsp_evaluation)

    result = repository.pareto_front(fixture.spec.spec_id)
    assert result.non_dominated_candidate_ids == (dsp.candidate_id,)
    assert result.algorithm_version == 'pareto-front-2'

    with pytest.raises(
        ValueError, match='unpersisted JointOptimizationSpec'
    ):
        repository.pareto_front('joint-spec-missing')

    # Tampered evidence can no longer feed the front.
    result_ref = _result_ref(dsp_evaluation)
    fixture.joint_results[result_ref.source_id] = dict(
        fixture.joint_results[result_ref.source_id],
        candidate_id='joint-candidate-foreign',
    )
    with pytest.raises(ValueError, match='belongs to another candidate'):
        repository.pareto_front(fixture.spec.spec_id)


def _save_extended_pitch_spec(fixture, *, base_spec, base_page):
    evidence = build_extended_parameter_evidence(
        parameter='aim_pitch_deg',
        model_id='issue946-fixture-aim-model',
        model_version='1',
        evidence_scope='synthetic_fixture',
        tested_min_deg=-45.0,
        tested_max_deg=45.0,
        source_kind='synthetic_fixture',
        source_id=(
            f'issue946-fixture-pitch-evidence:{base_spec.search_spec_id}'
        ),
        source_sha256=sha256(
            f'issue946-pitch-evidence:{base_spec.search_spec_id}'.encode(
                'utf-8'
            )
        ).hexdigest(),
        detail='issue946 fixture pitch evidence',
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_parameter_evidence(evidence)
    capability = build_extended_model_capability(
        model_id='issue946-fixture-aim-model',
        model_version='1',
        evidence_scope='synthetic_fixture',
        supported_parameters=('aim_pitch_deg',),
        parameter_evidence=(evidence,),
        detail=f'issue946 fixture pitch capability for {base_spec.search_spec_id}',
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_capability(capability)
    extended = build_extended_search_spec(
        source_revision=fixture.revision,
        base_spec=base_spec,
        base_candidate_set_sha256=base_page.candidate_set_sha256,
        base_candidate_count=len(base_page.candidates),
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='speaker-fl',
                parameter='aim_pitch_deg',
                min_value=-15.0,
                max_value=15.0,
                step=15.0,
            ),
        ),
        candidate_limit=64,
        created_at_utc=NOW,
    )
    fixture.extended_search_repository.save_spec(extended)
    return extended


def test_joint_spec_derives_aim_pitch_variable_and_verifies_materialization(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    extended = _save_extended_pitch_spec(
        fixture, base_spec=fixture.search_spec, base_page=fixture.base_page
    )
    spec = _build_spec(fixture, extended_search_spec=extended)

    variable = next(
        item
        for item in spec.physical_variables
        if item.parameter == 'aim_pitch_deg'
    )
    assert variable.variable_id == 'physical:speaker-fl:aim_pitch_deg'
    assert variable.unit == 'deg'
    assert variable.authority_kind == 'extended_search'
    assert (variable.minimum, variable.maximum) == (-15.0, 15.0)

    speaker = fixture.revision.document.entity('speaker-fl')
    pitched = speaker.model_copy(
        update={'aim_xyz': direction_with_aim_pitch(speaker.aim_xyz, 15.0)}
    )
    pitched_variant = build_system_variant(
        baseline=fixture.revision,
        name='Issue 946 pitched speaker',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='pitch-speaker-fl',
                entity=pitched,
                role_binding_id='FL',
            ),
        ),
        created_at_utc=NOW,
    )
    decisions = (
        JointDecisionValue(
            domain='physical',
            variable_id='physical:speaker-fl:x_m',
            value=1.0,
        ),
        JointDecisionValue(
            domain='physical',
            variable_id='physical:speaker-fl:aim_pitch_deg',
            value=15.0,
        ),
    )
    require_joint_decision_materialization(
        spec=spec,
        baseline=fixture.revision,
        physical_system_variant=pitched_variant,
        calibration_plan=None,
        decisions=decisions,
    )
    wrong_pitch = (
        decisions[0],
        JointDecisionValue(
            domain='physical',
            variable_id='physical:speaker-fl:aim_pitch_deg',
            value=16.0,
        ),
    )
    with pytest.raises(ValueError, match='does not match the materialized'):
        require_joint_decision_materialization(
            spec=spec,
            baseline=fixture.revision,
            physical_system_variant=pitched_variant,
            calibration_plan=None,
            decisions=wrong_pitch,
        )
