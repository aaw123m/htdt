"""REV56-OPS regression tests — #598 networked AV security authority,
#601 control/automation scenario qualification, #602 safe-listening /
test-exposure authority."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_security_authority import (
    build_access_review,
    build_credential,
    build_management_surface,
    build_remote_authorization,
    build_security_risk,
    build_security_asset,
    build_security_observation,
    build_security_test_evidence,
    evaluate_security_review,
)
from htdt.cad_security_authority_repository import (
    CadSecurityAuthorityRepository,
    SecurityAuthorityConflictError,
    SecurityAuthorityIntegrityError,
)
from htdt.cad_control_scenario import (
    ControlRunStepRecord,
    ControlScenarioStep,
    build_control_surface,
    build_scenario,
    build_scenario_run,
    evaluate_scenario_qualification,
)
from htdt.cad_control_scenario_repository import (
    CadControlScenarioRepository,
    ControlScenarioConflictError,
)
from htdt.cad_safe_listening import (
    build_exposure_gate,
    build_exposure_limit,
    build_spl_capability,
    build_test_exposure_plan,
    evaluate_exposure_assessment,
)
from htdt.cad_safe_listening_repository import (
    CadSafeListeningRepository,
    SafeListeningConflictError,
)


DOC = 'doc-rev56-ops'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _subject() -> AuthorityRef:
    return AuthorityRef(
        kind='installed_equipment_instance',
        ref_id='inst-avr-1',
        ref_sha256='a' * 64,
    )


# ---------------------------------------------------------------------------
# #598 — security authority
# ---------------------------------------------------------------------------

def _asset(document_id: str = DOC):
    return build_security_asset(
        document_id=document_id,
        subject_ref=_subject(),
        management_reachability='lan',
        lifecycle_state='active',
        declared_at_utc=T0,
    )


def _credential(state: str = 'active', default: str = 'default_changed'):
    return build_credential(
        document_id=DOC,
        subject_ref=_subject(),
        account_ref='installer-svc',
        kind='named_account',
        capabilities=('view_telemetry',),
        scope='device_local',
        storage_reference='external_secret_store',
        default_credential_state=default,
        state=state,
        declared_at_utc=T0,
    )


def test_security_unassessed_is_unknown() -> None:
    review = evaluate_security_review(
        document_id=DOC, assets=(_asset(),), evaluated_at_utc=T1
    )
    assert review.state == 'unknown'
    assert 'unassessed' in review.reasons[-1]


def test_security_default_credential_is_high_risk() -> None:
    review = evaluate_security_review(
        document_id=DOC,
        assets=(_asset(),),
        credentials=(
            _credential(default='default_still_active'),
        ),
        evaluated_at_utc=T1,
    )
    assert review.state == 'high_risk_exposure'


def test_security_reviewed_with_limitations() -> None:
    review = evaluate_security_review(
        document_id=DOC,
        assets=(_asset(),),
        credentials=(_credential(),),
        surfaces=(
            build_management_surface(
                document_id=DOC,
                subject_ref=_subject(),
                kind='web_ui',
                requirement='required',
                state='enabled',
                exposure_scope='lan',
                authentication_state='authenticated',
                declared_at_utc=T0,
            ),
        ),
        observations=(
            build_security_observation(
                document_id=DOC,
                subject_ref=_subject(),
                kind='credential_state',
                outcome='observed',
                evidence_class='device_readback',
                observed_at_utc=T0,
            ),
        ),
        evaluated_at_utc=T1,
    )
    assert review.state == 'reviewed_with_limitations'
    assert dict(review.checks)['credentials'] == 'verified'
    assert dict(review.checks)['firmware'] == 'limited'


def test_security_credential_rejects_secret_material() -> None:
    with pytest.raises(ValueError):
        build_credential(
            document_id=DOC,
            subject_ref=_subject(),
            account_ref='postgres://user:password@host/db',
            kind='named_account',
            capabilities=('view_telemetry',),
            declared_at_utc=T0,
        )


def test_security_revoked_default_credential_rejected() -> None:
    # A revoked former-holder credential may not still claim the vendor
    # default is live on the device.
    with pytest.raises(ValueError, match='default_still_active'):
        build_credential(
            document_id=DOC,
            subject_ref=_subject(),
            account_ref='admin',
            kind='named_account',
            capabilities=('security_admin',),
            default_credential_state='default_still_active',
            former_holder=True,
            state='revoked',
            declared_at_utc=T0,
        )


def test_security_asset_rejects_direct_decommissioned() -> None:
    with pytest.raises(ValueError, match='decommission'):
        build_security_asset(
            document_id=DOC,
            subject_ref=_subject(),
            management_reachability='lan',
            lifecycle_state='decommissioned',
            declared_at_utc=T0,
        )


def test_security_repository_roundtrip(tmp_path) -> None:
    repo = CadSecurityAuthorityRepository(_scene_repo(tmp_path))
    asset = _asset()
    credential = _credential()
    observation = build_security_observation(
        document_id=DOC,
        subject_ref=_subject(),
        kind='firmware_state',
        outcome='observed',
        evidence_class='vendor_documentation',
        observed_at_utc=T0,
    )
    risk = build_security_risk(
        document_id=DOC,
        title='Internet-exposed management UI',
        likelihood_class='high',
        impact_domains=('operational', 'privacy'),
        status='accepted',
        risk_owner='integrator',
        review_at_utc=T2,
        subject_ref=_subject(),
        mitigation_plan_note='VLAN isolation applied',
        functional_av_effect='degrades_feature',
        raised_at_utc=T0,
    )
    remote = build_remote_authorization(
        document_id=DOC,
        subject_ref=_subject(),
        method='vendor_cloud',
        state='authorized',
        allowed_actions_note='read-only telemetry for support case 1234',
        consent_ref='ticket-1234',
        valid_until_utc=T2,
        declared_at_utc=T0,
    )
    evidence = build_security_test_evidence(
        document_id=DOC,
        kind='configuration_audit',
        tool_provider='manual',
        scope_note='config export review only — no active probing',
        authorization_ref='ticket-1234',
        performed_at_utc=T0,
    )
    access = build_access_review(
        document_id=DOC,
        trigger='periodic',
        reviewer='it-lead',
        outcomes=(),
        performed_at_utc=T0,
    )
    review = evaluate_security_review(
        document_id=DOC,
        assets=(asset,),
        credentials=(credential,),
        observations=(observation,),
        evaluated_at_utc=T1,
    )
    for saver, record in (
        (repo.save_asset, asset),
        (repo.save_credential, credential),
        (repo.save_observation, observation),
        (repo.save_risk, risk),
        (repo.save_remote_authorization, remote),
        (repo.save_test_evidence, evidence),
        (repo.save_access_review, access),
        (repo.save_review, review),
    ):
        saver(record)

    assert repo.get_asset(asset.asset_id) == asset
    assert repo.get_credential(credential.credential_id) == credential
    assert repo.get_observation(observation.observation_id) == observation
    assert repo.get_risk(risk.risk_id) == risk
    assert (
        repo.get_remote_authorization(remote.authorization_id) == remote
    )
    assert repo.get_test_evidence(evidence.evidence_id) == evidence
    assert repo.get_access_review(access.review_id) == access
    assert repo.get_review(review.review_id) == review

    # Idempotent re-save.
    repo.save_asset(asset)
    repo.save_review(review)

    assert len(repo.list_assets(DOC)) == 1
    assert len(repo.list_reviews(DOC)) == 1


def test_security_repository_append_only(tmp_path) -> None:
    # Semantic ids are sha-derived: a different payload is a new record,
    # re-saving the same record is a no-op, and the earlier revision
    # remains retrievable.
    repo = CadSecurityAuthorityRepository(_scene_repo(tmp_path))
    first = _credential(state='active')
    second = _credential(state='active', default='never_default')
    repo.save_credential(first)
    repo.save_credential(second)
    repo.save_credential(first)
    assert first.credential_id != second.credential_id
    assert repo.get_credential(first.credential_id) == first
    assert repo.get_credential(second.credential_id) == second
    assert len(repo.list_credentials(DOC)) == 2


def test_security_repository_integrity(tmp_path) -> None:
    repo = CadSecurityAuthorityRepository(_scene_repo(tmp_path))
    asset = _asset()
    repo.save_asset(asset)
    import sqlite3

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_security_assets SET lifecycle_state=? '
            'WHERE asset_id=?',
            ('decommissioned', asset.asset_id),
        )
    with pytest.raises(SecurityAuthorityIntegrityError):
        repo.get_asset(asset.asset_id)


def test_security_remote_requires_consent() -> None:
    with pytest.raises(ValueError, match='consent'):
        build_remote_authorization(
            document_id=DOC,
            subject_ref=_subject(),
            method='vpn',
            state='authorized',
            allowed_actions_note='full network',
            declared_at_utc=T0,
        )


def test_security_risk_accepted_requires_owner_and_review() -> None:
    with pytest.raises(ValueError):
        build_security_risk(
            document_id=DOC,
            title='unowned acceptance',
            likelihood_class='medium',
            impact_domains=('operational',),
            status='accepted',
            subject_ref=_subject(),
            raised_at_utc=T0,
        )


# ---------------------------------------------------------------------------
# #601 — control-scenario qualification
# ---------------------------------------------------------------------------

def _surface():
    return build_control_surface(
        document_id=DOC,
        controller_family='crestron',
        program_identity='rack-prog',
        program_version='1.4.2',
        program_sha256='c' * 64,
        declared_at_utc=T0,
    )


def _scenario() -> 'object':
    return build_scenario(
        document_id=DOC,
        kind='startup',
        name='System On',
        steps=(
            ControlScenarioStep(
                position=1,
                action_kind='power_on',
                target_label='amp-rack',
                expected_feedback='amp reports on',
                timeout_ms=8000,
            ),
            ControlScenarioStep(
                position=2,
                action_kind='route_set',
                target_label='hdmi-matrix',
                expected_feedback='matrix reports map',
                timeout_ms=3000,
            ),
        ),
        failure_notification_required=True,
        failure_notification_channel='panel-banner',
        declared_at_utc=T0,
    )


def test_scenario_never_run_is_not_executed() -> None:
    scenario = _scenario()
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(), evaluated_at_utc=T1
    )
    assert qualification.state == 'not_executed'


def test_scenario_empty_steps_is_draft() -> None:
    scenario = build_scenario(
        document_id=DOC,
        kind='custom',
        name='Empty',
        steps=(),
        declared_at_utc=T0,
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, evaluated_at_utc=T1
    )
    assert qualification.state == 'draft'


def test_scenario_stale_when_run_pins_other_revision() -> None:
    scenario = _scenario()
    # A run pinning the same scenario_id but a different revision sha is
    # evidence for a different revision — the current one stays
    # fail-closed at 'stale' (verification is revision-scoped).
    run = build_scenario_run(
        document_id=DOC,
        scenario=scenario,
        outcome='completed',
        step_records=(
            ControlRunStepRecord(
                step_position=1,
                outcome='verified',
                elapsed_ms=4000,
                observed_feedback='amp on',
            ),
            ControlRunStepRecord(
                step_position=2,
                outcome='verified',
                elapsed_ms=900,
                observed_feedback='map ok',
            ),
        ),
        started_at_utc=T1,
        finished_at_utc=T1,
    ).model_copy(
        update={
            'scenario_ref': AuthorityRef(
                kind='control_scenario',
                ref_id=scenario.scenario_id,
                ref_sha256='f' * 64,
            )
        }
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(run,),
        evaluated_at_utc=T2,
    )
    assert qualification.state == 'stale'


def test_scenario_run_on_other_scenario_is_not_evidence() -> None:
    # A run executed against a different scenario record does not
    # qualify this scenario — it is ignored, not credited.
    scenario = _scenario()
    other = build_scenario(
        document_id=DOC,
        kind='shutdown',
        name='System Off',
        steps=scenario.steps,
        declared_at_utc=T0,
    )
    run = build_scenario_run(
        document_id=DOC,
        scenario=other,
        outcome='completed',
        step_records=(
            ControlRunStepRecord(
                step_position=1,
                outcome='verified',
                elapsed_ms=4000,
                observed_feedback='amp off',
            ),
            ControlRunStepRecord(
                step_position=2,
                outcome='verified',
                elapsed_ms=900,
                observed_feedback='map cleared',
            ),
        ),
        started_at_utc=T1,
        finished_at_utc=T1,
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(run,),
        evaluated_at_utc=T2,
    )
    assert qualification.state == 'not_executed'


def test_scenario_failed_on_timeout() -> None:
    scenario = _scenario()
    run = build_scenario_run(
        document_id=DOC,
        scenario=scenario,
        outcome='aborted_on_failure',
        step_records=(
            ControlRunStepRecord(
                step_position=1,
                outcome='verified',
                elapsed_ms=4000,
                observed_feedback='amp on',
            ),
            ControlRunStepRecord(
                step_position=2,
                outcome='timeout',
                elapsed_ms=3000,
                observed_feedback='no reply',
            ),
        ),
        failure_notification_outcome='missed',
        started_at_utc=T1,
        finished_at_utc=T1,
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(run,),
        evaluated_at_utc=T2,
    )
    assert qualification.state == 'failed'
    assert dict(qualification.checks)['failure_notification'] == 'failed'


def test_scenario_qualified() -> None:
    scenario = _scenario()
    run = build_scenario_run(
        document_id=DOC,
        scenario=scenario,
        outcome='completed',
        step_records=(
            ControlRunStepRecord(
                step_position=1,
                outcome='verified',
                elapsed_ms=4000,
                observed_feedback='amp on',
            ),
            ControlRunStepRecord(
                step_position=2,
                outcome='verified',
                elapsed_ms=900,
                observed_feedback='map ok',
            ),
        ),
        failure_notification_outcome='notified',
        started_at_utc=T1,
        finished_at_utc=T1,
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(run,),
        evaluated_at_utc=T2,
    )
    assert qualification.state == 'qualified'


def test_scenario_step_requires_target() -> None:
    with pytest.raises(ValueError, match='target'):
        ControlScenarioStep(
            position=1, action_kind='power_on', timeout_ms=1000
        )


def test_scenario_verified_step_requires_feedback_and_time() -> None:
    with pytest.raises(ValueError):
        ControlRunStepRecord(step_position=1, outcome='verified')
    with pytest.raises(ValueError):
        ControlRunStepRecord(
            step_position=1, outcome='verified', elapsed_ms=100
        )


def test_control_repository_roundtrip(tmp_path) -> None:
    repo = CadControlScenarioRepository(_scene_repo(tmp_path))
    surface = _surface()
    scenario = _scenario()
    run = build_scenario_run(
        document_id=DOC,
        scenario=scenario,
        outcome='completed',
        step_records=(
            ControlRunStepRecord(
                step_position=1,
                outcome='verified',
                elapsed_ms=4000,
                observed_feedback='amp on',
            ),
            ControlRunStepRecord(
                step_position=2,
                outcome='verified',
                elapsed_ms=900,
                observed_feedback='map ok',
            ),
        ),
        failure_notification_outcome='notified',
        started_at_utc=T1,
        finished_at_utc=T1,
    )
    qualification = evaluate_scenario_qualification(
        document_id=DOC, scenario=scenario, runs=(run,),
        evaluated_at_utc=T2,
    )
    repo.save_surface(surface)
    repo.save_scenario(scenario)
    repo.save_run(run)
    repo.save_qualification(qualification)

    assert repo.get_surface(surface.surface_id) == surface
    assert repo.get_scenario(scenario.scenario_id) == scenario
    assert repo.get_run(run.run_id) == run
    assert (
        repo.get_qualification(qualification.qualification_id)
        == qualification
    )
    assert len(repo.list_scenarios(DOC)) == 1
    assert len(repo.list_qualifications(DOC)) == 1

    repo.save_scenario(scenario)


def test_control_repository_append_only(tmp_path) -> None:
    repo = CadControlScenarioRepository(_scene_repo(tmp_path))
    first = _scenario()
    second = build_scenario(
        document_id=DOC,
        kind='shutdown',
        name='System Off',
        steps=first.steps,
        declared_at_utc=T0,
    )
    repo.save_scenario(first)
    repo.save_scenario(second)
    repo.save_scenario(first)
    assert repo.get_scenario(first.scenario_id) == first
    assert repo.get_scenario(second.scenario_id) == second
    assert len(repo.list_scenarios(DOC)) == 2


# ---------------------------------------------------------------------------
# #602 — safe-listening / test-exposure authority
# ---------------------------------------------------------------------------

def _niosh_limit():
    return build_exposure_limit(
        document_id=DOC,
        label='NIOSH REL 85 dBA / 8 h',
        basis='niosh_rel',
        criterion='laeq_twa',
        limit_level_db=85.0,
        reference_window_s=28800,
        exchange_rate_db=3.0,
        declared_at_utc=T0,
    )


def _who_limit():
    return build_exposure_limit(
        document_id=DOC,
        label='WHO venue 100 dB LAeq,15min',
        basis='who_safe_listening_venue',
        criterion='laeq_windowed',
        limit_level_db=100.0,
        reference_window_s=900,
        declared_at_utc=T0,
    )


def _capability():
    return build_spl_capability(
        document_id=DOC,
        scope='reference_seat',
        capability_source='rp22_parameter',
        source_ref=AuthorityRef(kind='rp22_profile', ref_id='rp22-1'),
        max_continuous_db_spl=105.0,
        max_peak_db_spl=115.0,
        declared_at_utc=T0,
    )


def test_exposure_no_limit_is_unknown() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='quick sweep',
        planned_level_db_spl=80.0,
        planned_duration_s=600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, evaluated_at_utc=T1
    )
    assert assessment.state == 'unknown'


def test_exposure_within_limit() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='low-level check',
        planned_level_db_spl=80.0,
        planned_duration_s=600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        capability=_capability(), evaluated_at_utc=T1,
    )
    assert assessment.state == 'within_limit'
    assert assessment.projected_dose_pct is not None
    assert assessment.projected_dose_pct < 100


def test_exposure_exceeds_requires_gate() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='full-level sweep',
        planned_level_db_spl=100.0,
        planned_duration_s=3600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        capability=_capability(), evaluated_at_utc=T1,
    )
    assert assessment.state == 'gate_required'
    # 100 dB under NIOSH 3 dB exchange: 28800 * 2^(-15/3) = 900 s.
    assert assessment.allowable_duration_s == pytest.approx(900.0)


def test_exposure_gate_approve_with_controls() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='full-level sweep',
        planned_level_db_spl=100.0,
        planned_duration_s=3600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        evaluated_at_utc=T1,
    )
    gate = build_exposure_gate(
        document_id=DOC,
        assessment=assessment,
        decision='allow_with_controls',
        decided_by='operator-1',
        controls=('hearing_protection', 'reduced_duration'),
        decided_at_utc=T1,
    )
    gated = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        gate=gate, evaluated_at_utc=T2,
    )
    assert gated.state == 'approved_with_controls'


def test_exposure_gate_block() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='full-level sweep',
        planned_level_db_spl=100.0,
        planned_duration_s=3600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        evaluated_at_utc=T1,
    )
    gate = build_exposure_gate(
        document_id=DOC,
        assessment=assessment,
        decision='block',
        decided_by='operator-1',
        reason='exceeds occupational basis for planned audience',
        decided_at_utc=T1,
    )
    gated = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        gate=gate, evaluated_at_utc=T2,
    )
    assert gated.state == 'blocked'


def test_exposure_who_windowed() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='pink noise run',
        planned_level_db_spl=95.0,
        planned_duration_s=900,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_who_limit(),
        capability=_capability(), evaluated_at_utc=T1,
    )
    assert assessment.state == 'within_limit'


def test_exposure_peak_ceiling_blocks_quiet_plan() -> None:
    limit = build_exposure_limit(
        document_id=DOC,
        label='project limit with peak ceiling',
        basis='declared_project_policy',
        criterion='laeq_windowed',
        limit_level_db=95.0,
        reference_window_s=900,
        peak_ceiling_db=110.0,
        notes='declared by integrator for occupied commissioning',
        declared_at_utc=T0,
    )
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='quiet check',
        planned_level_db_spl=70.0,
        planned_duration_s=60,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=limit,
        capability=_capability(), evaluated_at_utc=T1,
    )
    # capability peak 115 > ceiling 110 → gate required even though
    # the planned LAeq is well inside the limit.
    assert assessment.state == 'gate_required'


def test_exposure_capability_requires_source_or_basis() -> None:
    with pytest.raises(ValueError, match='source_ref'):
        build_spl_capability(
            document_id=DOC,
            scope='room_wide',
            capability_source='in_room_measured',
            max_continuous_db_spl=100.0,
            declared_at_utc=T0,
        )
    with pytest.raises(ValueError, match='basis_note'):
        build_spl_capability(
            document_id=DOC,
            scope='room_wide',
            capability_source='declared_estimate',
            max_continuous_db_spl=100.0,
            declared_at_utc=T0,
        )


def test_exposure_limit_requires_window_and_exchange() -> None:
    with pytest.raises(ValueError, match='reference_window_s'):
        build_exposure_limit(
            document_id=DOC,
            label='bad',
            basis='niosh_rel',
            criterion='laeq_twa',
            limit_level_db=85.0,
            declared_at_utc=T0,
        )
    with pytest.raises(ValueError, match='exchange'):
        build_exposure_limit(
            document_id=DOC,
            label='bad',
            basis='niosh_rel',
            criterion='laeq_twa',
            limit_level_db=85.0,
            reference_window_s=28800,
            declared_at_utc=T0,
        )


def test_gate_decision_validation() -> None:
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='x',
        planned_level_db_spl=100.0,
        planned_duration_s=10,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=_niosh_limit(),
        evaluated_at_utc=T1,
    )
    with pytest.raises(ValueError, match='controls'):
        build_exposure_gate(
            document_id=DOC,
            assessment=assessment,
            decision='allow_with_controls',
            decided_by='op',
            decided_at_utc=T1,
        )
    with pytest.raises(ValueError, match='reason'):
        build_exposure_gate(
            document_id=DOC,
            assessment=assessment,
            decision='block',
            decided_by='op',
            decided_at_utc=T1,
        )


def test_safe_listening_repository_roundtrip(tmp_path) -> None:
    repo = CadSafeListeningRepository(_scene_repo(tmp_path))
    limit = _niosh_limit()
    capability = _capability()
    plan = build_test_exposure_plan(
        document_id=DOC,
        label='full-level sweep',
        planned_level_db_spl=100.0,
        planned_duration_s=3600,
        declared_at_utc=T0,
    )
    assessment = evaluate_exposure_assessment(
        document_id=DOC, plan=plan, limit=limit,
        capability=capability, evaluated_at_utc=T1,
    )
    gate = build_exposure_gate(
        document_id=DOC,
        assessment=assessment,
        decision='allow_with_controls',
        decided_by='op',
        controls=('hearing_protection',),
        decided_at_utc=T1,
    )
    repo.save_limit(limit)
    repo.save_capability(capability)
    repo.save_plan(plan)
    repo.save_assessment(assessment)
    repo.save_gate(gate)

    assert repo.get_limit(limit.limit_id) == limit
    assert repo.get_capability(capability.capability_id) == capability
    assert repo.get_plan(plan.plan_id) == plan
    assert repo.get_gate(gate.gate_id) == gate
    assert repo.get_assessment(assessment.assessment_id) == assessment
    assert len(repo.list_limits(DOC)) == 1
    assert len(repo.list_assessments(DOC)) == 1

    repo.save_limit(limit)


def test_safe_listening_repository_append_only(tmp_path) -> None:
    repo = CadSafeListeningRepository(_scene_repo(tmp_path))
    first = _niosh_limit()
    second = _who_limit()
    repo.save_limit(first)
    repo.save_limit(second)
    repo.save_limit(first)
    assert repo.get_limit(first.limit_id) == first
    assert repo.get_limit(second.limit_id) == second
    assert len(repo.list_limits(DOC)) == 2


# ---------------------------------------------------------------------------
# Cross-cutting: sealed-model invariants shared by all three authorities
# ---------------------------------------------------------------------------

def test_record_ids_are_sha_derived() -> None:
    asset = _asset()
    assert asset.asset_id.startswith('secasset-')
    assert asset.asset_sha256.startswith(asset.asset_id[len('secasset-'):])
    scenario = _scenario()
    assert scenario.scenario_id.startswith('ctrlscn-')
    limit = _niosh_limit()
    assert limit.limit_id.startswith('explim-')


def test_external_standards_seed_includes_ops_references() -> None:
    from htdt.cad_external_standards import seed_standard_documents

    keys = {
        (doc.standard_id, doc.edition)
        for doc in seed_standard_documents()
    }
    assert ('avixa-rp-c303-01', '2018') in keys
    assert ('ansi-avixa-d402-02', '2013-r2024') in keys
    assert ('avixa-tr-111', '2019') in keys
    assert ('avixa-ux-701-01', 'in-development') in keys
    assert ('who-safe-listening-venues', '2022') in keys
    assert ('niosh-noise-rel', '98-126') in keys
    assert ('cedia-spl-capability-wp', '2025') in keys
