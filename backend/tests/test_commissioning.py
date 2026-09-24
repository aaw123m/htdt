from pathlib import Path

import pytest

from htdt.cad_commissioning import (
    AcceptedDeviation,
    CheckSubject,
    CommissioningCheck,
    CommissioningObservation,
    ToleranceSpec,
    build_commissioning_plan,
    build_commissioning_run,
    build_tolerance_profile,
    evaluate_commissioning_check,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_commissioning_repository import (
    CadCommissioningRepository,
    CommissioningConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


DOC = F1_DOCUMENT_ID


def _profile(document_id: str = DOC):
    return build_tolerance_profile(
        document_id=document_id,
        name='theater-acceptance',
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
                key='amp-channel',
                operator='equals',
                limit_text='FL',
            ),
            ToleranceSpec(
                key='spl-min',
                operator='min',
                limit=85.0,
                unit='db',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )


def _check(
    check_id: str = 'check-position',
    *,
    subject: CheckSubject | None = None,
    subject_kind: str = 'position',
    tolerance_key: str = 'speaker-position-m',
    **kwargs,
) -> CommissioningCheck:
    return CommissioningCheck(
        check_id=check_id,
        subject_kind=subject_kind,
        subject=subject
        or CheckSubject(
            scene_entity_id='speaker-fl', property='position.x'
        ),
        tolerance_key=tolerance_key,
        **kwargs,
    )


def _seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=saved.revision,
        name='install variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    variant_repository.save_variant(variant)
    return scene_repository, saved, variant, variant_repository


def _repository(
    tmp_path: Path,
) -> tuple[
    CadCommissioningRepository,
    object,
    object,
]:
    scene_repository, saved, variant, variant_repository = _seed(tmp_path)
    repository = CadCommissioningRepository(
        scene_repository,
        system_variant_repository=variant_repository,
    )
    return repository, saved, variant


def test_tolerance_profile_unique_keys_and_hash() -> None:
    profile = _profile()
    assert len(profile.profile_sha256) == 64
    with pytest.raises(ValueError):
        build_tolerance_profile(
            document_id=DOC,
            name='dup',
            version='v1',
            specs=(
                ToleranceSpec(
                    key='k', operator='min', limit=1, unit='db'
                ),
                ToleranceSpec(
                    key='k', operator='min', limit=2, unit='db'
                ),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_uncertainty_aware_decision_rule() -> None:
    check = _check()
    tolerance = _profile().spec('speaker-position-m')
    base = dict(
        check_id='check-position', observed_at_utc='2026-09-24T00:00:00+00:00'
    )
    passing = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o1', value=3.02, unit='m', uncertainty=0.01,
            **base,
        ),
    )
    assert passing.status == 'pass'

    unknown = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o2', value=3.04, unit='m', uncertainty=0.02,
            **base,
        ),
    )
    assert unknown.status == 'unknown'

    failing = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o3', value=3.10, unit='m', uncertainty=0.01,
            **base,
        ),
    )
    assert failing.status == 'fail'

    not_quantified = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o4', value=3.02, unit='m', uncertainty=None,
            **base,
        ),
    )
    assert not_quantified.status == 'unknown'

    no_observation = evaluate_commissioning_check(check, tolerance, None)
    assert no_observation.status == 'unknown'

    na = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o5', not_applicable=True, **base
        ),
    )
    assert na.status == 'not_applicable'


def test_numeric_observations_require_explicit_unit() -> None:
    with pytest.raises(ValueError):
        CommissioningObservation(
            observation_id='o1',
            check_id='check-position',
            value=3.02,
            uncertainty=0.01,
        )
    with pytest.raises(ValueError):
        CommissioningObservation(
            observation_id='o1',
            check_id='check-position',
            value_text='FL',
            unit='m',
        )


def test_observation_unit_converted_across_quantity_family() -> None:
    check = _check()
    tolerance = _profile().spec('speaker-position-m')
    converted = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o1',
            check_id='check-position',
            value=302.0,
            unit='cm',
            uncertainty=1.0,
        ),
    )
    assert converted.status == 'pass'
    assert converted.observed_value == pytest.approx(3.02)

    incompatible = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o2',
            check_id='check-position',
            value=3.02,
            unit='deg',
            uncertainty=0.01,
        ),
    )
    assert incompatible.status == 'unknown'


def test_equals_check_requires_matching_text() -> None:
    check = _check(
        'check-amp',
        subject=CheckSubject(channel_role_id='FL', property='routing'),
        subject_kind='setting',
        tolerance_key='amp-channel',
    )
    tolerance = _profile().spec('amp-channel')
    match = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o1',
            check_id='check-amp',
            value_text='FL',
        ),
    )
    assert match.status == 'pass'
    mismatch = evaluate_commissioning_check(
        check,
        tolerance,
        CommissioningObservation(
            observation_id='o2',
            check_id='check-amp',
            value_text='SUB1',
        ),
    )
    assert mismatch.status == 'fail'


def test_commissioning_run_evaluation_and_deviation(tmp_path: Path) -> None:
    _repository, saved, _variant = _seed(tmp_path)[:3]
    profile = _profile()
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        tolerance_profile=profile,
        checks=(
            _check(),
            _check(
                'check-amp',
                subject=CheckSubject(
                    channel_role_id='FL', property='routing'
                ),
                subject_kind='setting',
                tolerance_key='amp-channel',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    observations = (
        CommissioningObservation(
            observation_id='o-pos',
            check_id='check-position',
            value=3.02,
            unit='m',
            uncertainty=0.01,
        ),
        CommissioningObservation(
            observation_id='o-amp',
            check_id='check-amp',
            value_text='SUB1',
        ),
    )
    deviation = AcceptedDeviation(
        deviation_id='dev-1',
        check_id='check-amp',
        rationale='temporary wiring pending amplifier swap',
        approved_by='installer-lead',
        decided_at_utc='2026-09-24T01:00:00+00:00',
    )
    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=observations,
        accepted_deviations=(deviation,),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert run.result('check-position').status == 'pass'
    assert run.result('check-amp').status == 'fail'
    assert run.effective_outcome('check-amp') == 'accepted_deviation'
    assert run.effective_outcome('check-position') == 'pass'


def test_run_rejects_mismatched_profile_and_orphan_observation(
    tmp_path: Path,
) -> None:
    _repository, saved, _variant = _seed(tmp_path)[:3]
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        tolerance_profile=_profile(),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    other_profile = build_tolerance_profile(
        document_id=DOC,
        name='other',
        version='v1',
        specs=(
            ToleranceSpec(
                key='k', operator='min', limit=1.0, unit='db'
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    with pytest.raises(ValueError):
        build_commissioning_run(
            plan=plan,
            tolerance_profile=other_profile,
            observations=(),
            created_at_utc='2026-09-24T01:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_commissioning_plan(
            document_id=DOC,
            scene_revision=saved.revision,
            tolerance_profile=_profile(),
            checks=(_check(tolerance_key='missing'),),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_plan_requires_exact_design_authority(tmp_path: Path) -> None:
    scene_repository, saved, variant, _ = _seed(tmp_path)
    profile = _profile()
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        system_variant=variant,
        tolerance_profile=profile,
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert plan.system_variant_sha256 == variant.variant_sha256

    with pytest.raises(ValueError):
        build_commissioning_plan(
            document_id='different-doc',
            scene_revision=saved.revision,
            tolerance_profile=profile,
            created_at_utc='2026-09-24T00:00:00+00:00',
        )

    later = scene_repository.save(
        saved.revision.document.model_copy(update={'entities': ()}),
        parent_revision_id=saved.revision.revision_id,
    )
    with pytest.raises(ValueError):
        build_commissioning_plan(
            document_id=DOC,
            scene_revision=later.revision,
            system_variant=variant,
            tolerance_profile=profile,
            created_at_utc='2026-09-24T00:00:00+00:00',
        )

    with pytest.raises(ValueError):
        build_commissioning_plan(
            document_id=DOC,
            scene_revision=saved.revision,
            tolerance_profile=profile,
            checks=(
                _check(
                    subject=CheckSubject(
                        channel_role_id='FL', property='dimension'
                    ),
                    subject_kind='dimension',
                ),
            ),
            created_at_utc='2026-09-24T00:00:00+00:00',
        )


def test_commissioning_repository_append_only_and_replay(
    tmp_path: Path,
) -> None:
    repository, saved, variant = _repository(tmp_path)
    profile = _profile()
    repository.save_tolerance_profile(profile)
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        system_variant=variant,
        tolerance_profile=profile,
        checks=(_check(),),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    repository.save_plan(plan)
    assert repository.get_plan(plan.plan_id) == plan
    with pytest.raises(CommissioningConflictError):
        repository.save_plan(plan)

    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(
            CommissioningObservation(
                observation_id='o1',
                check_id='check-position',
                value=3.02,
                unit='m',
                uncertainty=0.01,
            ),
        ),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save_run(run)
    assert repository.get_run(run.run_id) == run
    assert repository.list_runs(plan.plan_id) == (run,)
    with pytest.raises(CommissioningConflictError):
        repository.save_run(run)


def test_repository_replays_results_forged_run_cannot_persist(
    tmp_path: Path,
) -> None:
    repository, saved, _variant = _repository(tmp_path)
    profile = _profile()
    repository.save_tolerance_profile(profile)
    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        tolerance_profile=profile,
        checks=(_check(),),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    repository.save_plan(plan)

    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(
            CommissioningObservation(
                observation_id='o1',
                check_id='check-position',
                value=9.99,
                unit='m',
                uncertainty=0.01,
            ),
        ),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert run.result('check-position').status == 'fail'

    forged = run.model_copy(
        update={
            'results': (
                run.result('check-position').model_copy(
                    update={'status': 'pass'}
                ),
            )
        }
    )
    with pytest.raises(ValueError):
        repository.save_run(forged)


def test_repository_resolves_plan_and_evidence_refs(tmp_path: Path) -> None:
    repository, saved, variant = _repository(tmp_path)
    profile = _profile()
    repository.save_tolerance_profile(profile)

    plan = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        system_variant=variant,
        tolerance_profile=profile,
        checks=(_check(),),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    forged_plan = plan.model_copy(
        update={'scene_content_hash': 'f' * 64}
    )
    with pytest.raises(ValueError):
        repository.save_plan(forged_plan)

    ghost_subject = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        tolerance_profile=profile,
        checks=(
            _check(
                subject=CheckSubject(
                    scene_entity_id='ghost', property='position.x'
                )
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    with pytest.raises(ValueError):
        repository.save_plan(ghost_subject)

    ghost_role = build_commissioning_plan(
        document_id=DOC,
        scene_revision=saved.revision,
        system_variant=variant,
        tolerance_profile=profile,
        checks=(
            _check(
                subject=CheckSubject(
                    channel_role_id='SUB9', property='routing'
                ),
                subject_kind='setting',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    with pytest.raises(ValueError):
        repository.save_plan(ghost_role)

    repository.save_plan(plan)
    run = build_commissioning_run(
        plan=plan,
        tolerance_profile=profile,
        observations=(
            CommissioningObservation(
                observation_id='o1',
                check_id='check-position',
                value=3.02,
                unit='m',
                uncertainty=0.01,
                evidence_ref=AuthorityRef(
                    kind='scene_revision',
                    ref_id=saved.revision.revision_id,
                    ref_sha256=saved.revision.content_hash,
                ),
            ),
        ),
        created_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save_run(run)

    forged_evidence = run.observations[0].model_copy(
        update={
            'evidence_ref': run.observations[0].evidence_ref.model_copy(
                update={'ref_sha256': 'b' * 64}
            )
        }
    )
    forged_run = run.model_copy(
        update={'observations': (forged_evidence,)}
    )
    with pytest.raises(ValueError):
        repository.save_run(forged_run)
