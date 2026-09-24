from pathlib import Path

import pytest

from htdt.cad_commissioning import (
    AcceptedDeviation,
    CommissioningCheck,
    CommissioningObservation,
    ToleranceSpec,
    build_commissioning_plan,
    build_commissioning_run,
    build_tolerance_profile,
    evaluate_commissioning_check,
)
from htdt.cad_commissioning_repository import (
    CadCommissioningRepository,
    CommissioningConflictError,
)
from htdt.cad_repository import SceneRepository


def _profile(document_id: str = 'doc-1', **kwargs):
    return build_tolerance_profile(
        document_id=document_id,
        name=kwargs.pop('name', 'Install tolerances'),
        version='v1',
        specs=(
            ToleranceSpec(
                key='speaker-position-m',
                operator='abs_error',
                target=3.0,
                limit=0.05,
                unit='m',
            ),
            ToleranceSpec(
                key='room-width-m',
                operator='range',
                limit=4.0,
                limit_high=5.0,
                unit='m',
            ),
            ToleranceSpec(
                key='amp-channel',
                operator='equals',
                limit_text='out-1',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def _plan(profile, **kwargs):
    return build_commissioning_plan(
        document_id='doc-1',
        scene_revision_id='rev-1',
        system_variant_id='var-1',
        tolerance_profile=profile,
        checks=(
            CommissioningCheck(
                check_id='check-position',
                subject_kind='position',
                subject_ref='speaker-fl',
                tolerance_key='speaker-position-m',
            ),
            CommissioningCheck(
                check_id='check-amp',
                subject_kind='setting',
                subject_ref='amp-1',
                tolerance_key='amp-channel',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_tolerance_profile_unique_keys_and_hash() -> None:
    profile = _profile()
    assert len(profile.profile_sha256) == 64
    assert profile.spec('speaker-position-m') is not None
    with pytest.raises(ValueError):
        build_tolerance_profile(
            document_id='doc-1',
            name='dup',
            version='v1',
            specs=(
                ToleranceSpec(key='k', operator='max', limit=1.0),
                ToleranceSpec(key='k', operator='min', limit=0.0),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_uncertainty_aware_decision_rule() -> None:
    check = CommissioningCheck(
        check_id='c',
        subject_kind='position',
        subject_ref='e',
        tolerance_key='speaker-position-m',
    )
    tolerance = ToleranceSpec(
        key='speaker-position-m', operator='abs_error', target=3.0, limit=0.05
    )

    def observation(value, uncertainty):
        return CommissioningObservation(
            observation_id='o1',
            check_id='c',
            value=value,
            uncertainty=uncertainty,
        )

    assert (
        evaluate_commissioning_check(
            check, tolerance, observation(3.02, 0.01)
        ).status
        == 'pass'
    )
    assert (
        evaluate_commissioning_check(
            check, tolerance, observation(3.20, 0.01)
        ).status
        == 'fail'
    )
    assert (
        evaluate_commissioning_check(
            check, tolerance, observation(3.04, 0.03)
        ).status
        == 'unknown'
    )
    assert (
        evaluate_commissioning_check(
            check, tolerance, observation(3.02, None)
        ).status
        == 'unknown'
    )
    assert (
        evaluate_commissioning_check(check, tolerance, None).status
        == 'unknown'
    )
    assert (
        evaluate_commissioning_check(
            check,
            tolerance,
            CommissioningObservation(
                observation_id='o2', check_id='c', not_applicable=True
            ),
        ).status
        == 'not_applicable'
    )


def test_equals_check_text() -> None:
    check = CommissioningCheck(
        check_id='c2',
        subject_kind='setting',
        subject_ref='amp-1',
        tolerance_key='amp-channel',
    )
    tolerance = ToleranceSpec(
        key='amp-channel', operator='equals', limit_text='out-1'
    )
    assert (
        evaluate_commissioning_check(
            check,
            tolerance,
            CommissioningObservation(
                observation_id='o', check_id='c2', value_text='out-1'
            ),
        ).status
        == 'pass'
    )
    assert (
        evaluate_commissioning_check(
            check,
            tolerance,
            CommissioningObservation(
                observation_id='o', check_id='c2', value_text='out-2'
            ),
        ).status
        == 'fail'
    )


def test_commissioning_run_evaluation_and_deviation() -> None:
    profile = _profile()
    plan = _plan(profile)
    observations = (
        CommissioningObservation(
            observation_id='o1',
            check_id='check-position',
            value=3.02,
            uncertainty=0.01,
        ),
        CommissioningObservation(
            observation_id='o2',
            check_id='check-amp',
            value_text='out-2',
        ),
    )
    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=observations,
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert run.result('check-position').status == 'pass'
    assert run.result('check-amp').status == 'fail'
    assert run.effective_outcome('check-amp') == 'fail'

    deviation = AcceptedDeviation(
        deviation_id='d1',
        check_id='check-amp',
        rationale='Client approved reroute to out-2',
        approved_by='pm-1',
        decided_at_utc='2026-09-24T02:00:00+00:00',
    )
    run2 = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=observations,
        accepted_deviations=(deviation,),
        created_at_utc='2026-09-24T03:00:00+00:00',
    )
    assert run2.result('check-amp').status == 'fail'
    assert run2.effective_outcome('check-amp') == 'accepted_deviation'
    assert run2.effective_outcome('check-amp') != 'pass'


def test_run_rejects_mismatched_profile_and_orphan_observation() -> None:
    profile = _profile()
    plan = _plan(profile)
    other = _profile(name='Install tolerances v2')
    with pytest.raises(ValueError):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=other,
            observations=(),
            created_at_utc='2026-09-24T01:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=(
                CommissioningObservation(
                    observation_id='ghost',
                    check_id='no-such-check',
                    value=1.0,
                ),
            ),
            created_at_utc='2026-09-24T01:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_commissioning_plan(
            document_id='doc-1',
            scene_revision_id='rev-1',
            tolerance_profile=profile,
            checks=(
                CommissioningCheck(
                    check_id='x',
                    subject_kind='other',
                    subject_ref='e',
                    tolerance_key='missing-key',
                ),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_commissioning_repository_append_only(tmp_path: Path) -> None:
    repository = CadCommissioningRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )
    profile = _profile()
    repository.save_tolerance_profile(profile)
    assert repository.get_tolerance_profile(profile.profile_id) == profile
    with pytest.raises(CommissioningConflictError):
        repository.save_tolerance_profile(profile)

    plan = _plan(profile)
    repository.save_plan(plan)
    assert repository.get_plan(plan.plan_id) == plan

    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(
            CommissioningObservation(
                observation_id='o1',
                check_id='check-position',
                value=3.0,
                uncertainty=0.0,
            ),
            CommissioningObservation(
                observation_id='o2',
                check_id='check-amp',
                value_text='out-1',
            ),
        ),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save_run(run)
    assert repository.get_run(run.run_id) == run
    assert repository.list_runs(plan.plan_id) == (run,)

    unsaved = _profile(name='Never persisted')
    with pytest.raises(ValueError):
        repository.save_plan(_plan(unsaved))
