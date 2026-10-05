"""REV55-CORRQUAL regression tests: correction qualification gate (#568).

Covers the fail-closed qualification authority: sealed immutable records,
the evidence ladder (design → simulated → measured → holdout-verified →
deployed), capability/safety gates (boost, usable band, headroom,
pre-ringing), mechanical design/holdout/repeatability partition, honest
UNKNOWN/not_evaluated, material-change invalidation, and persistence
integrity.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_calibration import (
    CadBiquadFilter,
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
    calculate_biquad_coefficients,
)
from htdt.cad_correction_design_policy import (
    build_correction_design_policy,
)
from htdt.cad_correction_qualification import (
    ConstraintDeclaration,
    CorrectionQualificationRecord,
    DeploymentObservation,
    ListeningRegion,
    PositionResponseSet,
    RegistrationEvidence,
    SubjectiveEvidenceRef,
    CorrectionSubjectRef,
    evaluate_correction_qualification,
    qualification_scope_label,
    qualification_state_label,
)
from htdt.cad_correction_qualification_repository import (
    CadCorrectionQualificationRepository,
    QualificationConflictError,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_fir_filter import build_fir_filter_artifact
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.canonical_json import canonical_sha256, canonicalize_payload

NOW = '2026-10-05T00:00:00+00:00'


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _target() -> CadTargetCurve:
    return CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=80.0),
            CadTargetCurvePoint(frequency_hz=200.0, level_db=80.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='absolute_level', reference_level_db=80.0
        ),
    )


def _peq(
    filter_id: str,
    frequency_hz: float,
    gain_db: float,
    q: float = 1.0,
    sample_rate_hz: int = 48000,
) -> CadBiquadFilter:
    return CadBiquadFilter(
        filter_id=filter_id,
        filter_type='peaking',
        frequency_hz=frequency_hz,
        q=q,
        gain_db=gain_db,
        sample_rate_hz=sample_rate_hz,
        coefficients=calculate_biquad_coefficients(
            filter_type='peaking',
            frequency_hz=frequency_hz,
            q=q,
            gain_db=gain_db,
            sample_rate_hz=sample_rate_hz,
        ),
    )


def _channel(
    peq: tuple[CadBiquadFilter, ...] = (),
    gain_db: float = 0.0,
    channel_id: str = 'FL',
    sample_rate_hz: int = 48000,
) -> CadCalibrationChannel:
    return CadCalibrationChannel(
        channel_id=channel_id,
        role_id='front_left',
        source_entity_id='speaker-fl',
        physical_output_id='dac-out-1',
        sample_rate_hz=sample_rate_hz,
        gain_db=gain_db,
        delay_s=0.0,
        polarity='normal',
        crossovers=(
            CadCrossoverSetting(
                crossover_type='high_pass',
                frequency_hz=80.0,
                filter_order=2,
            ),
        ),
        peq=peq,
        routing=('dac-out-1',),
    )


def _device(
    max_boost_db: float | None = 12.0,
    max_cut_db: float | None = 24.0,
) -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='cap-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        max_boost_db=max_boost_db,
        max_cut_db=max_cut_db,
    )


def _plan(
    *,
    plan_id: str = 'plan-1',
    channels: tuple[CadCalibrationChannel, ...] | None = None,
    support_state: str = 'SUPPORTED',
    unsupported_reasons: tuple[str, ...] = (),
    device: CadDeviceCapabilityConstraints | None = None,
    target: CadTargetCurve | None = None,
) -> CadCalibrationPlan:
    payload = {
        'plan_id': plan_id,
        'plan_version': 'fixture-1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': 'doc-1',
        'scene_revision_id': 'rev-1',
        'scene_content_hash': _hash('scene'),
        'system_variant_id': 'variant-1',
        'system_variant_sha256': _hash('variant'),
        'source_measurement_id': 'meas-1',
        'source_measurement_sha256': _hash('meas'),
        'source_dataset_id': 'ds-1',
        'source_dataset_sha256': _hash('ds'),
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': _hash('q'),
        'sample_rate_hz': 48000,
        'channels': channels if channels is not None else (_channel(),),
        'target_curve': target if target is not None else _target(),
        'max_boost_db': 6.0,
        'max_cut_db': 12.0,
        'device_constraints': device if device is not None else _device(),
        'support_state': support_state,
        'unsupported_reasons': unsupported_reasons,
    }
    provisional = CadCalibrationPlan.model_construct(
        **canonicalize_payload(
            CadCalibrationPlan, dict(**payload, plan_semantic_sha256='0' * 64)
        )
    )
    return CadCalibrationPlan(
        **payload,
        plan_semantic_sha256=canonical_sha256(provisional.semantic_payload()),
    )


def _subject(
    plan: CadCalibrationPlan | None = None,
    *,
    kind: str = 'calibration_plan',
    subject_id: str = 'plan-1',
    subject_sha256: str | None = None,
    topology: str = 'peq',
    regularization=None,
    regularization_source: str = 'none',
) -> CorrectionSubjectRef:
    return CorrectionSubjectRef(
        kind=kind,
        subject_id=subject_id,
        subject_sha256=(
            subject_sha256
            if subject_sha256 is not None
            else (plan.plan_semantic_sha256 if plan is not None else _hash('x'))
        ),
        topology=topology,
        regularization=regularization,
        regularization_source=regularization_source,
    )


def _response_set(
    positions: tuple[str, ...],
    *,
    level_db: float,
    kind: str = 'physical_measurement',
) -> PositionResponseSet:
    freqs = (30.0, 50.0, 100.0, 150.0)
    return PositionResponseSet(
        evidence_kind=kind,
        position_ids=positions,
        samples_frequencies_hz=tuple(freqs for _ in positions),
        samples_magnitudes_db=tuple(
            (level_db, level_db, level_db, level_db) for _ in positions
        ),
    )


def _region(
    design: tuple[str, ...] = ('mlp', 'seat-a'),
    holdout: tuple[str, ...] = (),
    repeatability: tuple[str, ...] = (),
) -> ListeningRegion:
    return ListeningRegion(
        design_position_ids=design,
        holdout_position_ids=holdout,
        repeatability_position_ids=repeatability,
    )


def _constraints(**overrides) -> ConstraintDeclaration:
    values = {
        'max_boost_db': 6.0,
        'max_cut_db': 12.0,
        'usable_band_hz': FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0),
        'headroom_db': 9.0,
        'max_pre_ringing_ratio': 0.5,
        'max_latency_s': 0.1,
    }
    values.update(overrides)
    return ConstraintDeclaration(**values)


def _evaluate(**overrides):
    plan = overrides.pop('plan', None) or _plan(
        channels=(_channel(peq=(_peq('eq-1', 60.0, -4.0),)),)
    )
    payload = {
        'subject': _subject(plan),
        'region': _region(),
        'constraints': _constraints(),
        'target': _target(),
        'correction_bands': ((30.0, 150.0),),
        'plan': plan,
        'baseline': _response_set(('mlp', 'seat-a'), level_db=84.0),
        'recorded_at_utc': NOW,
    }
    payload.update(overrides)
    return evaluate_correction_qualification(**payload)


def _gate(record: CorrectionQualificationRecord, name: str):
    return record.gate(name)  # type: ignore[arg-type]


# -- record sealing ---------------------------------------------------------

def test_record_is_sealed_and_self_verifying() -> None:
    record = _evaluate()
    assert record.qualification_id == (
        f'correction-qualification:{record.semantic_sha256}'
    )
    payload = record.model_dump(mode='json')
    payload['scope'] = 'qualified_region'
    with pytest.raises(ValueError, match='hash mismatch'):
        CorrectionQualificationRecord.model_validate(payload)


def test_gates_cover_every_gate_kind_once() -> None:
    record = _evaluate()
    assert [g.gate for g in record.gates] == [
        'identity', 'boost_cut', 'usable_band', 'headroom',
        'pre_ringing', 'control_fidelity', 'holdout_independence',
        'closed_loop_deployment',
    ]


def test_generated_filter_is_not_automatically_qualified() -> None:
    """No post-correction measurement: evidence stops at DESIGN_ONLY."""
    record = _evaluate()
    assert record.state == 'DESIGN_ONLY'
    assert record.scope == 'candidate'
    assert _gate(record, 'control_fidelity').status == 'not_evaluated'
    assert _gate(record, 'closed_loop_deployment').status == 'not_evaluated'


def test_no_evidence_is_insufficient_not_candidate() -> None:
    record = _evaluate(baseline=None, spatial_evidence=None)
    assert record.state == 'INSUFFICIENT_EVIDENCE'
    assert record.scope == 'unqualified'


def test_simulated_post_never_verifies() -> None:
    """A solver-predicted post set may reach SIMULATED — never higher."""
    record = _evaluate(
        post=_response_set(
            ('mlp', 'seat-a'), level_db=80.5, kind='simulated_prediction'
        )
    )
    assert record.state == 'SIMULATED'
    assert record.scope == 'candidate'
    assert _gate(record, 'control_fidelity').status == 'not_evaluated'


def test_physical_post_at_design_points_qualified_point() -> None:
    record = _evaluate(
        post=_response_set(('mlp', 'seat-a'), level_db=80.5),
    )
    assert record.state == 'QUALIFIED_WITH_LIMITATIONS'
    assert record.scope == 'qualified_point'
    assert _gate(record, 'control_fidelity').status == 'pass'
    # no holdout declared → region claim is honestly out of reach
    assert _gate(record, 'holdout_independence').status == 'limitation'


def test_holdout_verified_reaches_region_scope() -> None:
    record = _evaluate(
        region=_region(holdout=('seat-b',)),
        baseline=_response_set(('mlp', 'seat-a', 'seat-b'), level_db=84.0),
        post=_response_set(('mlp', 'seat-a', 'seat-b'), level_db=80.4),
    )
    assert record.scope == 'qualified_region'
    assert record.state in (
        'SPATIALLY_HOLDOUT_VERIFIED', 'QUALIFIED_WITH_LIMITATIONS'
    )
    assert _gate(record, 'holdout_independence').status == 'pass'


def test_deployed_and_remeasured_requires_observed_match() -> None:
    record = _evaluate(
        region=_region(holdout=('seat-b',)),
        baseline=_response_set(('mlp', 'seat-a', 'seat-b'), level_db=84.0),
        post=_response_set(('mlp', 'seat-a', 'seat-b'), level_db=80.4),
        deployment=DeploymentObservation(
            state='observed_match',
            observed_ref_id='camilladsp-cfg-1',
            observed_ref_sha256=_hash('cfg'),
        ),
    )
    assert record.scope == 'qualified_region'


def test_single_point_evidence_never_claims_region() -> None:
    """One measured position cannot satisfy a declared multi-point holdout."""
    record = _evaluate(
        region=_region(design=('mlp',), holdout=('seat-b', 'seat-c')),
        baseline=_response_set(('mlp', 'seat-b', 'seat-c'), level_db=84.0),
        post=_response_set(('mlp', 'seat-b'), level_db=80.4),
    )
    assert _gate(record, 'holdout_independence').status == 'limitation'
    assert record.scope == 'qualified_point'


# -- fail-closed safety gates -------------------------------------------------

def test_boost_above_declared_max_is_incompatible() -> None:
    # peq at +9dB while declared max is 6 dB → hard fail
    plan = _plan(channels=(_channel(peq=(_peq('eq-1', 60.0, 9.0),)),))
    record = _evaluate(
        plan=plan,
        subject=_subject(plan),
        constraints=_constraints(max_boost_db=6.0),
    )
    assert record.state == 'INCOMPATIBLE'
    assert record.scope == 'unqualified'
    assert _gate(record, 'boost_cut').status == 'fail'


def test_boost_above_device_capability_is_incompatible() -> None:
    plan = _plan(
        channels=(_channel(peq=(_peq('eq-1', 60.0, 8.0),)),),
        device=_device(max_boost_db=5.0),
    )
    record = _evaluate(plan=plan, subject=_subject(plan))
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'boost_cut').status == 'fail'


def test_spatial_evidence_null_band_caps_boost() -> None:
    """Kirkeby-Nelson: a deep local null may not be boosted past its cap."""
    policy = build_correction_design_policy(
        measurement_population={
            'population_id': 'pop-1',
            'population_version': '1',
            'semantic_sha256': _hash('pop'),
            'sample_count': 2,
        },
        frequency_domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0),
        correction_bands=((30.0, 150.0),),
        aggregation='arithmetic_db_mean',
        smoothing='none',
        seat_ids=('mlp', 'seat-a'),
        min_positions=2,
        max_boost_db=6.0,
        max_cut_db=12.0,
        regularization={
            'formulation': 'peq',
            'strength_db': 3.0,
            'frequency_dependence': 'none',
            'gain_penalty_db': 0.5,
            'max_filters': 8,
            'max_q': 8.0,
        },
        spatial_robustness={
            'persistence_fraction': 0.5,
            'error_significance_db': 1.0,
            'sign_consistency_required': False,
            'local_null_max_boost_db': 2.0,
            'movement_sensitivity_threshold_db': 3.0,
        },
        validation={
            'rule': 'none',
            'holdout_position_ids': (),
            'validation_algorithm_version': 'fixture-1',
        },
        algorithm_id='fixture',
        algorithm_version='1',
    )
    from htdt.cad_correction_design_policy import (
        SpatialCorrectionEvidence,
    )
    band_payload = {
        'schema_version': 1,
        'authority_version': 'eqp10-spatial-evidence-1',
        'policy_id': policy.policy_id,
        'policy_sha256': policy.semantic_sha256,
        'position_ids_used': ('mlp', 'seat-a'),
        'holdout_position_ids': (),
        'bands': (
            {
                'low_hz': 30.0,
                'high_hz': 150.0,
                'positions_with_evidence': 2,
                'classification': 'local_null',
                'aggregated_error_db': -18.0,
                'position_spread_db': 6.0,
                'sign_consistent': True,
                'persistent_fraction': 1.0,
                'max_allowed_boost_db': 2.0,
                'max_allowed_cut_db': 0.0,
                'recommend_geometry_review': True,
                'headroom_feasible': False,
            },
        ),
    }
    provisional = SpatialCorrectionEvidence.model_construct(
        **canonicalize_payload(
            SpatialCorrectionEvidence,
            dict(
                **band_payload,
                evidence_id='spatial-correction-evidence:' + '0' * 64,
                semantic_sha256='0' * 64,
            ),
        )
    )
    evidence = SpatialCorrectionEvidence(
        **band_payload,
        evidence_id='spatial-correction-evidence:'
        + canonical_sha256(provisional.semantic_payload()),
        semantic_sha256=canonical_sha256(provisional.semantic_payload()),
    )
    plan = _plan(channels=(_channel(peq=(_peq('eq-1', 60.0, 5.0),)),))
    record = _evaluate(
        plan=plan,
        subject=_subject(
            plan,
            regularization=policy.regularization,
            regularization_source='eqp10_policy',
        ),
        design_policy=policy,
        spatial_evidence=evidence,
    )
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'boost_cut').status == 'fail'
    assert 'local_null' in _gate(record, 'boost_cut').reasons[0]


def test_correction_band_outside_usable_band_fails() -> None:
    record = _evaluate(
        correction_bands=((300.0, 500.0),),
        constraints=_constraints(
            usable_band_hz=FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)
        ),
    )
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'usable_band').status == 'fail'


def test_partial_band_coverage_is_limitation_not_failure() -> None:
    record = _evaluate(
        correction_bands=((150.0, 250.0),),
        constraints=_constraints(
            usable_band_hz=FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)
        ),
    )
    assert _gate(record, 'usable_band').status == 'limitation'
    assert record.state != 'INCOMPATIBLE'


def test_headroom_insufficient_fails() -> None:
    plan = _plan(channels=(_channel(peq=(_peq('eq-1', 60.0, 10.0),)),))
    record = _evaluate(
        plan=plan,
        subject=_subject(plan),
        constraints=_constraints(max_boost_db=12.0, headroom_db=6.0),
    )
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'headroom').status == 'fail'


def test_undeclared_headroom_is_limitation_not_pass() -> None:
    record = _evaluate(constraints=_constraints(headroom_db=None))
    gate = _gate(record, 'headroom')
    assert gate.status == 'not_evaluated'
    assert 'headroom undeclared' in record.limitations


def test_holdout_regression_is_incompatible() -> None:
    record = _evaluate(
        region=_region(holdout=('seat-b',)),
        baseline=_response_set(('mlp', 'seat-a', 'seat-b'), level_db=84.0),
        post=_response_set(('mlp', 'seat-a'), level_db=80.4)
        .model_copy(update={
            'position_ids': ('mlp', 'seat-a', 'seat-b'),
            'samples_magnitudes_db': (
                (80.4, 80.4, 80.4, 80.4),
                (80.4, 80.4, 80.4, 80.4),
                (95.0, 95.0, 95.0, 95.0),
            ),
            'samples_frequencies_hz': (
                (30.0, 50.0, 100.0, 150.0),
                (30.0, 50.0, 100.0, 150.0),
                (30.0, 50.0, 100.0, 150.0),
            ),
        }),
    )
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'holdout_independence').status == 'fail'
    assert record.scope == 'unqualified'


def test_repeatability_never_satisfies_holdout() -> None:
    """Re-measurement at the same seat proves stability, not generalization."""
    record = _evaluate(
        region=_region(
            design=('mlp', 'seat-a'),
            repeatability=('mlp',),
            holdout=(),
        ),
        baseline=_response_set(('mlp', 'seat-a'), level_db=84.0),
        post=_response_set(('mlp', 'seat-a'), level_db=80.4),
    )
    assert record.scope != 'qualified_region'
    assert _gate(record, 'holdout_independence').status == 'limitation'


def test_region_partitions_must_be_disjoint() -> None:
    with pytest.raises(ValueError, match='disjoint'):
        ListeningRegion(
            design_position_ids=('mlp',),
            holdout_position_ids=('mlp',),
        )


def test_post_positions_outside_region_rejected() -> None:
    with pytest.raises(ValueError, match='undeclared positions'):
        _evaluate(
            post=_response_set(('mlp', 'stranger'), level_db=80.0)
        )


def test_subject_sha_mismatch_fails_closed() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match='subject_sha256'):
        _evaluate(subject=_subject(plan, subject_sha256=_hash('other')))


def test_unsupported_plan_is_incompatible() -> None:
    plan = _plan(
        support_state='UNSUPPORTED',
        unsupported_reasons=('no_quality_report',),
    )
    record = _evaluate(plan=plan, subject=_subject(plan))
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'identity').status == 'fail'


def test_observed_mismatch_is_incompatible() -> None:
    record = _evaluate(
        post=_response_set(('mlp', 'seat-a'), level_db=80.4),
        deployment=DeploymentObservation(
            state='observed_mismatch', detail='read-back differs'
        ),
    )
    assert record.state == 'INCOMPATIBLE'
    assert _gate(record, 'closed_loop_deployment').status == 'fail'


def test_subjective_evidence_never_opens_a_gate() -> None:
    record = _evaluate(
        post=None,
        subjective_evidence=(
            SubjectiveEvidenceRef(
                evidence_id='listening-note-1', note='sounds better'
            ),
        ),
    )
    assert record.state == 'DESIGN_ONLY'
    assert record.scope == 'candidate'
    assert 'subjective_listening' in record.evidence_kinds


def test_incomparable_registration_is_disqualifier() -> None:
    record = _evaluate(
        post=_response_set(('mlp', 'seat-a'), level_db=80.4),
        registrations=(
            RegistrationEvidence(
                registration_id='pm-registration:' + _hash('r1'),
                semantic_sha256=_hash('r1'),
                partition='holdout',
                comparability='incomparable',
            ),
        ),
    )
    assert record.state == 'INCOMPATIBLE'
    assert record.scope == 'unqualified'


def test_comparable_with_limitations_registration_is_limitation() -> None:
    record = _evaluate(
        post=_response_set(('mlp', 'seat-a'), level_db=80.4),
        registrations=(
            RegistrationEvidence(
                registration_id='pm-registration:' + _hash('r2'),
                semantic_sha256=_hash('r2'),
                partition='calibration',
                comparability='comparable_with_limitations',
            ),
        ),
        constraints=_constraints(),  # all declared → only the registration limits
    )
    assert record.state == 'QUALIFIED_WITH_LIMITATIONS'
    assert any('comparable with limitations' in lim for lim in record.limitations)


def test_fir_subject_pre_ringing_inspected() -> None:
    taps = [0.05, -0.4, 1.0, -0.3, 0.1]  # energy before main peak → mixed-phase
    artifact = build_fir_filter_artifact(
        filter_class='arbitrary_fir',
        sample_rate_hz=48000,
        taps=taps,
        tap_format='float64',
        channel_id='FL',
        gain_db=0.0,
        normalization='none',
        time_reference_sample=2,
        latency_s=0.001,
        source_producer='fixture',
        source_version='1',
    )
    record = evaluate_correction_qualification(
        subject=CorrectionSubjectRef(
            kind='fir_artifact',
            subject_id=artifact.artifact_id,
            subject_sha256=artifact.semantic_sha256,
            topology='fir',
        ),
        region=_region(),
        constraints=_constraints(max_pre_ringing_ratio=0.01),
        fir=artifact,
        baseline=_response_set(('mlp', 'seat-a'), level_db=84.0),
        recorded_at_utc=NOW,
    )
    assert _gate(record, 'pre_ringing').status == 'fail'
    assert record.state == 'INCOMPATIBLE'


def test_observables_report_independent_axes() -> None:
    record = _evaluate(
        post=_response_set(('mlp', 'seat-a'), level_db=80.4),
    )
    by_name = {o.observable: o for o in record.observables}
    assert by_name['target_deviation'].status == 'improved'
    assert by_name['before_after_measurement'].status == 'improved'
    assert by_name['deployed_state_match'].status == 'not_evaluated'
    assert by_name['repeatability'].status == 'not_evaluated'
    assert by_name['headroom_margin'].status == 'within_limits'


def test_state_scope_labels_exist() -> None:
    assert qualification_state_label('INCOMPATIBLE') == '不適合'
    assert qualification_scope_label('qualified_region') == '領域修飾済み'


# -- persistence --------------------------------------------------------------

def _repository(tmp_path: Path) -> CadCorrectionQualificationRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    return CadCorrectionQualificationRepository(scene_repository)


def test_persistence_roundtrip(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _evaluate(document_id='doc-1')
    repository.save(record)
    loaded = repository.get(record.qualification_id)
    assert loaded == record
    assert repository.list_for_document('doc-1') == (record,)


def test_save_requires_document_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _evaluate()
    with pytest.raises(ValueError, match='document_id'):
        repository.save(record)


def test_current_for_correction_enforces_subject_sha(tmp_path: Path) -> None:
    """A re-planned filter has a different semantic sha → stale record."""
    repository = _repository(tmp_path)
    plan = _plan()
    record = _evaluate(
        plan=plan, subject=_subject(plan), document_id='doc-1'
    )
    repository.save(record)
    current = repository.current_for_correction(
        'doc-1', plan.plan_id, plan.plan_semantic_sha256
    )
    assert current == record
    stale = repository.current_for_correction(
        'doc-1', plan.plan_id, _hash('replanned')
    )
    assert stale is None


def test_duplicate_save_is_idempotent_conflict_fails(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _evaluate(document_id='doc-1')
    repository.save(record)
    repository.save(record)  # same content is a no-op
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_correction_qualifications SET payload_json=? '
            'WHERE qualification_id=?',
            (record.model_copy(update={}).model_dump_json()
             .replace('candidate', 'qualified_point'),
             record.qualification_id),
        )
        connection.commit()
    with pytest.raises((ValueError, Exception)):
        repository.get(record.qualification_id)


def test_scope_labels_per_subject(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _evaluate(document_id='doc-1')
    repository.save(record)
    labels = repository.scope_labels('doc-1')
    assert labels[record.subject.subject_id] == 'candidate'
