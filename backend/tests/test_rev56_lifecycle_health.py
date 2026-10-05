"""REV56-LIFECYCLE regression tests — #595 post-commissioning
health/drift monitoring authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_health_drift import (
    DependencyImpactRule,
    DriftComponent,
    MonitoringAuthorization,
    RestoreCheckEvidence,
    TrendSample,
    TrendThresholdSpec,
    assess_trend,
    build_change_event,
    build_drift_assessment,
    build_monitoring_declaration,
    build_observation,
    build_symptom_episode,
    classify_drift_component,
    compose_reverification,
    derive_stale_marks,
    evaluate_restore_confirmation,
)
from htdt.cad_health_drift_repository import (
    CadHealthDriftRepository,
    HealthDriftConflictError,
    HealthDriftIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene


DOC = 'doc-rev56-lifecycle'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
T3 = '2026-10-05T03:00:00+00:00'
T4 = '2026-10-05T04:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _subject(digit: str = 'a') -> AuthorityRef:
    return AuthorityRef(
        kind='installed_equipment_instance',
        ref_id='inst-avr-1',
        ref_sha256=digit * 64,
    )


def _baseline(digit: str = 'b') -> AuthorityRef:
    return AuthorityRef(
        kind='device_known_good_baseline',
        ref_id='kgb-avr-1',
        ref_sha256=digit * 64,
    )


def _declaration(**overrides):
    kwargs = dict(
        document_id=DOC,
        subject_ref=_subject(),
        capabilities=('pollable_readback', 'event_log_export'),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_monitoring_declaration(**kwargs)


def _obs(**overrides):
    kwargs = dict(
        document_id=DOC,
        subject_ref=_subject(),
        domain='device_config',
        kind='config_hash_state',
        source_tool='pytest',
        observed_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_observation(**kwargs)


# ---------------------------------------------------------------------------
# Monitoring declaration + observation sealing
# ---------------------------------------------------------------------------


def test_declaration_roundtrip_and_append_only(tmp_path: Path):
    repository = CadHealthDriftRepository(_scene_repo(tmp_path))
    declaration = _declaration()
    repository.save_declaration(declaration)
    assert repository.get_declaration(declaration.declaration_id) == (
        declaration
    )
    assert repository.list_declarations(DOC) == (declaration,)
    assert repository.declaration_for_subject(
        DOC, 'installed_equipment_instance', 'inst-avr-1'
    ) == declaration
    # Identical re-save is idempotent; a divergent payload under the same
    # id is a conflict, never an update.
    repository.save_declaration(declaration)
    forged = declaration.model_copy(
        update={'telemetry_scope_note': 'tampered'}
    )
    with pytest.raises(HealthDriftIntegrityError):
        repository.save_declaration(forged)


def test_declaration_unobservable_is_exclusive():
    with pytest.raises(ValueError):
        _declaration(capabilities=('unobservable', 'pollable_readback'))


def test_observation_requires_comparison_key_for_baseline_pin():
    with pytest.raises(ValueError):
        _obs(compared_to_repr='"abc"')


def test_observation_roundtrip_and_filters(tmp_path: Path):
    repository = CadHealthDriftRepository(_scene_repo(tmp_path))
    observation = _obs(
        comparison_key='state_content_sha256',
        compared_to_repr='"deadbeef"',
        observed_repr='"deadbeef"',
        collection_mode='local_automatic',
    )
    repository.save_observation(observation)
    assert repository.get_observation(observation.observation_id) == (
        observation
    )
    assert repository.list_observations(DOC, domain='device_config') == (
        observation,
    )
    assert repository.list_observations(DOC, domain='acoustic') == ()
    assert repository.list_observations(
        DOC, subject_ref_id='inst-avr-1', kind='config_hash_state'
    ) == (observation,)


def test_remote_service_collection_needs_authorization_seam():
    """The authorization boundary is explicit metadata — remote mode is
    recordable but the declaration flags the allowed scope (#595 §15)."""

    declaration = _declaration(
        authorization=MonitoringAuthorization(
            authorized_by='operator-a',
            remote_collection_allowed=True,
            minimum_scope_note='fault counters only',
        )
    )
    assert declaration.authorization.remote_collection_allowed is True
    unscoped = _declaration()
    assert unscoped.authorization is None


# ---------------------------------------------------------------------------
# Classification ladder (#595 §4)
# ---------------------------------------------------------------------------


def test_classify_no_observations_is_insufficient_observability():
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=None,
        observations=(),
    )
    assert component.classification == 'insufficient_observability'


def test_classify_unobservable_declaration_is_not_a_failure():
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(capabilities=('unobservable',)),
        observations=(),
    )
    assert component.classification == 'insufficient_observability'
    assert 'unobservable' in component.reason


def test_classify_observation_without_baseline_is_not_comparable():
    observation = _obs()  # no comparison_key
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
    )
    assert component.classification == 'not_comparable'


def test_classify_matched_baseline_is_no_material_change():
    observation = _obs(
        comparison_key='state_content_sha256',
        compared_to_repr='"deadbeef"',
        observed_repr='"deadbeef"',
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
    )
    assert component.classification == 'no_material_change'


def test_classify_config_mismatch_is_configuration_drift():
    observation = _obs(
        comparison_key='state_content_sha256',
        compared_to_repr='"deadbeef"',
        observed_repr='"cafebabe"',
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
    )
    assert component.classification == 'configuration_drift'
    assert component.severity == 'medium'


def test_classify_mismatch_with_change_event_is_expected_change():
    observation = _obs(
        comparison_key='state_content_sha256',
        compared_to_repr='"deadbeef"',
        observed_repr='"cafebabe"',
    )
    event = build_change_event(
        document_id=DOC,
        kind='configuration_change',
        subject_refs=(_subject(),),
        detail='operator EQ update',
        occurred_at_utc=T1,
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
        change_events=(event,),
        expected_change_kinds=('configuration_change',),
    )
    assert component.classification == 'expected_change'
    assert component.matched_change_event_id == event.event_id


def test_classify_mismatch_unrelated_event_stays_drift():
    observation = _obs(
        comparison_key='state_content_sha256',
        compared_to_repr='"deadbeef"',
        observed_repr='"cafebabe"',
    )
    event = build_change_event(
        document_id=DOC,
        kind='service_visit',
        subject_refs=(_subject(),),
        occurred_at_utc=T1,
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
        change_events=(event,),
        expected_change_kinds=('configuration_change',),
    )
    assert component.classification == 'configuration_drift'


def test_classify_performance_mismatch_is_performance_drift():
    observation = _obs(
        domain='acoustic',
        kind='field_measurement_check',
        comparison_key='reference_sweep',
        compared_to_repr='{"spl_db": 75.0}',
        observed_repr='{"spl_db": 68.0}',
    )
    component = classify_drift_component(
        domain='acoustic',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(observation,),
    )
    assert component.classification == 'performance_drift'


def test_classify_hard_failure_and_intermittent_fault():
    offline = _obs(
        kind='device_online_state',
        observed_repr='{"online": false}',
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=(offline,),
    )
    assert component.classification == 'hard_failure'
    assert component.severity == 'high'

    reboots = tuple(
        _obs(kind='reboot_uptime_event', observed_at_utc=t)
        for t in (T1, T2, T3)
    )
    component = classify_drift_component(
        domain='device_config',
        subject_ref=_subject(),
        declaration=_declaration(),
        observations=reboots,
    )
    assert component.classification == 'intermittent_fault'


# ---------------------------------------------------------------------------
# Dependency-aware staleness (#595 §5)
# ---------------------------------------------------------------------------


def _dep_rule(
    domain: str = 'device_config',
    classifications=('configuration_drift', 'performance_drift'),
    ref: AuthorityRef | None = None,
    disposition: str = 'stale',
) -> DependencyImpactRule:
    return DependencyImpactRule(
        rule_id=f'rule-{domain}',
        trigger_domain=domain,
        trigger_classifications=tuple(classifications),
        affected_ref=ref
        or AuthorityRef(kind='commissioning_result', ref_id='comm-1'),
        disposition=disposition,
    )


def test_stale_marks_fire_rules_and_unmapped_dependents_review():
    components = (
        DriftComponent(
            domain='device_config',
            classification='configuration_drift',
            reason='x',
        ),
    )
    dependent = AuthorityRef(kind='latency_qualification', ref_id='lat-1')
    marks, status = derive_stale_marks(
        components,
        (_dep_rule(),),
        unmapped_dependents=(dependent,),
    )
    assert status == 'partially_mapped'
    by_kind = {m.affected_ref.kind: m for m in marks}
    assert by_kind['commissioning_result'].disposition == 'stale'
    assert by_kind['latency_qualification'].disposition == 'review_required'
    assert by_kind['latency_qualification'].via_rule_id is None


def test_stale_marks_clean_components_produce_none():
    component = DriftComponent(
        domain='acoustic',
        classification='no_material_change',
        reason='x',
    )
    marks, status = derive_stale_marks((component,), (_dep_rule(),))
    assert marks == ()
    assert status == 'mapped'


# ---------------------------------------------------------------------------
# Drift assessment (#595 §14)
# ---------------------------------------------------------------------------


def _component(
    classification: str, domain: str = 'device_config', severity='none'
) -> DriftComponent:
    return DriftComponent(
        domain=domain,
        classification=classification,
        reason='test',
        severity=severity,
    )


def test_assessment_rejects_severity_below_worst_component():
    with pytest.raises(ValueError):
        build_drift_assessment(
            document_id=DOC,
            subject_ref=_subject(),
            components=(_component('hard_failure', severity='high'),),
            operational_severity='low',
            assessed_at_utc=T2,
        )


def test_assessment_rejects_confirmed_certainty_with_dirty_component():
    with pytest.raises(ValueError):
        build_drift_assessment(
            document_id=DOC,
            subject_ref=_subject(),
            components=(_component('configuration_drift'),),
            operational_severity='medium',
            evidence_certainty='confirmed',
            assessed_at_utc=T2,
        )


def test_assessment_requires_non_none_severity_when_dirty():
    with pytest.raises(ValueError):
        build_drift_assessment(
            document_id=DOC,
            subject_ref=_subject(),
            components=(_component('configuration_drift'),),
            operational_severity='none',
            assessed_at_utc=T2,
        )


def test_assessment_roundtrip_and_defaults(tmp_path: Path):
    repository = CadHealthDriftRepository(_scene_repo(tmp_path))
    assessment = build_drift_assessment(
        document_id=DOC,
        subject_ref=_subject(),
        baseline_refs=(_baseline(),),
        components=(
            _component('no_material_change', 'acoustic'),
            _component('configuration_drift', 'device_config', 'medium'),
        ),
        dependency_rules=(_dep_rule(),),
        assessed_at_utc=T2,
    )
    assert assessment.operational_severity == 'medium'
    assert assessment.evidence_certainty == 'suspected'
    assert assessment.dependency_status == 'mapped'
    assert len(assessment.stale_marks) == 1
    repository.save_drift_assessment(assessment)
    assert repository.get_drift_assessment(assessment.assessment_id) == (
        assessment
    )
    assert repository.list_drift_assessments(
        DOC, subject_ref_id='inst-avr-1'
    ) == (assessment,)
    forged = assessment.model_copy(update={'operational_severity': 'none'})
    with pytest.raises(HealthDriftIntegrityError):
        repository.save_drift_assessment(forged)
    other = build_drift_assessment(
        document_id=DOC,
        subject_ref=_subject(),
        components=(_component('hard_failure', 'power_thermal', 'high'),),
        assessed_at_utc=T3,
    )
    conflict = other.model_copy(
        update={'assessment_id': assessment.assessment_id}
    )
    with pytest.raises(HealthDriftConflictError):
        repository.save_drift_assessment(conflict)


# ---------------------------------------------------------------------------
# Re-verification triggers (#595 §10)
# ---------------------------------------------------------------------------


def _assessment_with(*components: DriftComponent) -> object:
    return build_drift_assessment(
        document_id=DOC,
        subject_ref=_subject(),
        components=components,
        assessed_at_utc=T2,
    )


def test_compose_no_action_when_clean():
    assessment = _assessment_with(_component('no_material_change'))
    trigger = compose_reverification(assessment, decided_at_utc=T3)
    assert trigger.action == 'no_action'
    assert trigger.tasks == ()


def test_compose_restore_config_when_known_good_exists():
    assessment = _assessment_with(
        _component('configuration_drift', 'device_config', 'medium')
    )
    trigger = compose_reverification(
        assessment, known_good_refs=(_baseline(),), decided_at_utc=T3
    )
    assert trigger.action == 'restore_known_good_config'
    assert trigger.tasks[0].task_kind == 'restore_config'
    assert trigger.tasks[0].target_refs == (_baseline(),)


def test_compose_domain_reverification_without_known_good():
    assessment = _assessment_with(
        _component('configuration_drift', 'device_config', 'medium')
    )
    trigger = compose_reverification(assessment, decided_at_utc=T3)
    assert trigger.action == 'reverify_domain'
    assert trigger.tasks[0].task_kind == 'domain_reverification'


def test_compose_service_review_for_faults():
    assessment = _assessment_with(
        _component('hard_failure', 'power_thermal', 'high')
    )
    trigger = compose_reverification(assessment, decided_at_utc=T3)
    assert trigger.action == 'service_review'
    assert trigger.tasks[0].task_kind == 'service_investigation'


def test_compose_observe_for_insufficient_observability():
    assessment = _assessment_with(
        _component('insufficient_observability', 'network')
    )
    trigger = compose_reverification(assessment, decided_at_utc=T3)
    assert trigger.action == 'observe'
    assert trigger.tasks[0].task_kind == 'manual_observation'


def test_compose_full_recommission_is_explicit_only():
    assessment = _assessment_with(
        _component('configuration_drift', 'device_config', 'medium')
    )
    with pytest.raises(ValueError):
        compose_reverification(
            assessment, require_full_recommission=True, decided_at_utc=T3
        )
    trigger = compose_reverification(
        assessment,
        require_full_recommission=True,
        full_recommission_reason='site gut renovation requires full scope',
        decided_at_utc=T3,
    )
    assert trigger.action == 'full_recommission_required'
    assert 'full' in trigger.summary.lower()


def test_trigger_repository_roundtrip(tmp_path: Path):
    repository = CadHealthDriftRepository(_scene_repo(tmp_path))
    assessment = build_drift_assessment(
        document_id=DOC,
        subject_ref=_subject(),
        components=(
            _component('performance_drift', 'acoustic', 'medium'),
        ),
        assessed_at_utc=T2,
    )
    repository.save_drift_assessment(assessment)
    trigger = compose_reverification(assessment, decided_at_utc=T3)
    assert trigger.action == 'run_reference_check'
    repository.save_trigger(trigger)
    assert repository.get_trigger(trigger.trigger_id) == trigger
    assert repository.list_triggers(
        DOC, assessment_id=assessment.assessment_id
    ) == (trigger,)


# ---------------------------------------------------------------------------
# Trend + symptom semantics (#595 §7, §8)
# ---------------------------------------------------------------------------


def _spec(**overrides) -> TrendThresholdSpec:
    kwargs = dict(
        spec_id='spec-spl',
        metric_key='output_spl_db',
        unit='dB',
        center=75.0,
        band=1.0,
        step=3.0,
        min_samples=3,
    )
    kwargs.update(overrides)
    return TrendThresholdSpec(**kwargs)


def _sample(value: float, when: str) -> TrendSample:
    return TrendSample(value=value, observed_at_utc=when)


def test_trend_states():
    subject = _subject()
    few = assess_trend(
        document_id=DOC,
        subject_ref=subject,
        spec=_spec(),
        samples=(_sample(75.0, T0),),
        assessed_at_utc=T4,
    )
    assert few.state == 'insufficient_samples'

    unbounded = assess_trend(
        document_id=DOC,
        subject_ref=subject,
        spec=_spec(band=None),
        samples=tuple(
            _sample(75.0 + i, t)
            for i, t in enumerate((T0, T1, T2, T3))
        ),
        assessed_at_utc=T4,
    )
    assert unbounded.state == 'no_threshold'

    within = assess_trend(
        document_id=DOC,
        subject_ref=subject,
        spec=_spec(),
        samples=(
            _sample(75.0, T0),
            _sample(75.2, T1),
            _sample(75.4, T2),
        ),
        assessed_at_utc=T4,
    )
    assert within.state == 'within_band'
    assert within.slope_per_day is not None

    drifted = assess_trend(
        document_id=DOC,
        subject_ref=subject,
        spec=_spec(),
        samples=(
            _sample(76.0, T0),
            _sample(77.0, T1),
            _sample(77.0, T2),
        ),
        assessed_at_utc=T4,
    )
    assert drifted.state == 'drift_detected'

    stepped = assess_trend(
        document_id=DOC,
        subject_ref=subject,
        spec=_spec(),
        samples=(
            _sample(75.0, T0),
            _sample(75.1, T1),
            _sample(79.0, T2),
        ),
        assessed_at_utc=T4,
    )
    assert stepped.state == 'step_detected'
    assert stepped.step_change is True


def test_symptom_confirmed_needs_confirming_refs():
    with pytest.raises(ValueError):
        build_symptom_episode(
            document_id=DOC,
            symptom_observation_ids=('hlobs-x',),
            reason_state='confirmed',
            recorded_at_utc=T2,
        )
    episode = build_symptom_episode(
        document_id=DOC,
        symptom_observation_ids=('hlobs-x',),
        reason_state='suspected',
        cause_note='correlation only — causality unproven',
        recorded_at_utc=T2,
    )
    assert episode.reason_state == 'suspected'
    with pytest.raises(ValueError):
        build_symptom_episode(
            document_id=DOC,
            resolved=True,
            recorded_at_utc=T2,
        )


# ---------------------------------------------------------------------------
# Restore-and-confirm loop (#595 §11)
# ---------------------------------------------------------------------------


def _check(state: str) -> RestoreCheckEvidence:
    return RestoreCheckEvidence(
        check_ref=AuthorityRef(
            kind='reference_check', ref_id=f'chk-{state}'
        ),
        state=state,
    )


def test_restore_confirmation_verdicts():
    restore_ref = AuthorityRef(
        kind='device_restore_record', ref_id='rst-1'
    )
    inconclusive = evaluate_restore_confirmation(
        document_id=DOC,
        restore_ref=restore_ref,
        checks=(),
        confirmed_at_utc=T3,
    )
    assert inconclusive.verdict == 'confirmation_inconclusive'

    restored = evaluate_restore_confirmation(
        document_id=DOC,
        restore_ref=restore_ref,
        checks=(_check('passed'), _check('passed')),
        confirmed_at_utc=T3,
    )
    assert restored.verdict == 'confidence_restored'

    failed = evaluate_restore_confirmation(
        document_id=DOC,
        restore_ref=restore_ref,
        checks=(_check('passed'), _check('failed')),
        confirmed_at_utc=T3,
    )
    assert failed.verdict == 'differences_remain'

    escalated = evaluate_restore_confirmation(
        document_id=DOC,
        restore_ref=restore_ref,
        checks=(_check('inconclusive'),),
        escalate=True,
        confirmed_at_utc=T3,
    )
    assert escalated.verdict == 'escalate_service'


def test_restore_confirmation_repository(tmp_path: Path):
    repository = CadHealthDriftRepository(_scene_repo(tmp_path))
    confirmation = evaluate_restore_confirmation(
        document_id=DOC,
        restore_ref=AuthorityRef(
            kind='device_restore_record', ref_id='rst-9'
        ),
        checks=(_check('passed'),),
        confirmed_at_utc=T3,
    )
    repository.save_restore_confirmation(confirmation)
    assert repository.get_restore_confirmation(
        confirmation.confirmation_id
    ) == confirmation
    assert repository.list_restore_confirmations(DOC) == (confirmation,)
