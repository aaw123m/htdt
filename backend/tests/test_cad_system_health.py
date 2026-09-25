from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_health import (
    ChangeDetectionPolicy,
    HealthAuthorityRef,
    HealthCheckAssessment,
    HealthCheckItem,
    HealthCheckPlan,
    HealthCheckRun,
    HealthMetricPin,
    HealthObservation,
    assess_health_observation,
    build_health_baseline,
    build_health_check_plan,
    run_health_check,
)
from htdt.cad_system_health_repository import CadSystemHealthRepository

NOW = '2026-09-23T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    repository = CadSystemHealthRepository(scene_repository)
    return revision, repository


def _level_pin(pin_id: str = 'pin-fl-level', expected_db: float = 82.0) -> HealthMetricPin:
    return HealthMetricPin(
        pin_id=pin_id,
        domain='acoustic',
        metric_key='mlp_level_db',
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-baseline', ref_sha256='a' * 64
        ),
        expected_repr='{"value_db": %s}' % expected_db,
        tolerance_repr='{"tolerance_db": 0.5}',
    )


def _check(
    check_id: str = 'check-level',
    *,
    pins=('pin-fl-level',),
    context=(),
) -> HealthCheckItem:
    return HealthCheckItem(
        check_id=check_id,
        domain='acoustic',
        description='FL channel level at MLP',
        related_pin_ids=tuple(pins),
        required_context=tuple(context),
    )


def _observation(
    check_id: str = 'check-level',
    *,
    observed: str | None = '{"value_db": 82.0}',
    context=(),
    repeatability: bool = True,
) -> HealthObservation:
    return HealthObservation(
        check_id=check_id,
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-run', ref_sha256='b' * 64
        ),
        context_refs=tuple(context),
        observed_repr=observed,
        has_repeatability_evidence=repeatability,
    )


def _baseline(revision, **overrides):
    payload = {
        'document_id': revision.document_id,
        'name': 'Commissioning baseline',
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'metric_pins': (_level_pin(),),
        'policy': ChangeDetectionPolicy(level_tolerance_db=0.5),
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_health_baseline(**payload)


def _reforge(model, **overrides):
    """Re-hash a model's semantic payload with forged fields.

    ``model_copy`` skips validation, so the copy's hash is recomputed over
    the tampered fields; the result is internally consistent yet must
    still fail the repository's chain/derivation checks.
    """
    import htdt.cad_system_health as sh

    forged = model.model_copy(update=overrides)
    sha_key = next(
        k
        for k in reversed(list(type(forged).model_fields))
        if k.endswith('_sha256')
    )
    return forged.model_copy(
        update={sha_key: sh._hash(forged.semantic_payload())}
    )


def test_baseline_pins_exact_refs_not_latest(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    baseline = _baseline(
        revision,
        source_refs=(
            HealthAuthorityRef(
                kind='system_variant_as_built', ref_id='ab-1', ref_sha256='c' * 64
            ),
        ),
        instrument_refs=(
            HealthAuthorityRef(kind='microphone', ref_id='umik-1'),
        ),
    )
    repository.save_baseline(baseline)
    stored = repository.get_baseline(baseline.baseline_id)
    assert stored == baseline
    assert stored.scene_content_hash == revision.content_hash


def test_plan_rejects_pins_outside_the_baseline(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    with pytest.raises(ValueError, match='unknown baseline pins'):
        build_health_check_plan(
            baseline,
            checks=(_check(pins=('ghost-pin',)),),
            created_at_utc=NOW,
        )


def test_run_binds_exact_plan_and_baseline_hashes(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    baseline = _baseline(revision)
    repository.save_baseline(baseline)
    plan = build_health_check_plan(
        baseline, checks=(_check(),), created_at_utc=NOW
    )
    repository.save_plan(plan)
    other = _baseline(revision, name='Other baseline')
    with pytest.raises(ValueError, match='another baseline'):
        run_health_check(plan, other, created_at_utc=NOW)


def test_no_observation_is_not_run(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check()
    assessment = assess_health_observation(baseline, check, None)
    assert assessment.state == 'not_run'


def test_context_mismatch_is_not_comparable(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    context = HealthAuthorityRef(
        kind='operating_preset', ref_id='preset-1', ref_sha256='d' * 64
    )
    check = _check(context=(context,))
    observation = _observation(
        context=(
            HealthAuthorityRef(
                kind='operating_preset', ref_id='preset-2', ref_sha256='e' * 64
            ),
        )
    )
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'not_comparable'
    assert 'operating_preset' in assessment.reason


def test_check_without_pinned_metric_is_indeterminate(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check(pins=())
    assessment = assess_health_observation(baseline, check, _observation())
    assert assessment.state == 'indeterminate'


def test_change_beyond_tolerance_without_repeatability_is_indeterminate(
    tmp_path: Path,
) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check()
    observation = _observation(observed='{"value_db": 84.0}', repeatability=False)
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'indeterminate'
    assert 'repeatability' in assessment.reason


def test_change_with_repeatability_is_changed(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check()
    observation = _observation(observed='{"value_db": 84.0}', repeatability=True)
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'changed'


def test_within_tolerance_is_within_baseline(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check()
    observation = _observation(observed='{"value_db": 82.2}')
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'within_baseline'


def test_preferred_repeatability_still_reports_changed(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(
        revision,
        policy=ChangeDetectionPolicy(
            level_tolerance_db=0.5, min_repeatability_evidence='preferred'
        ),
    )
    check = _check()
    observation = _observation(observed='{"value_db": 84.0}', repeatability=False)
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'changed'


def test_run_history_is_append_only(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    baseline = _baseline(revision)
    repository.save_baseline(baseline)
    plan = build_health_check_plan(
        baseline, checks=(_check(),), created_at_utc=NOW
    )
    repository.save_plan(plan)
    run1 = run_health_check(
        plan, baseline, observations=(), created_at_utc=NOW
    )
    repository.save_run(run1)
    run2 = run_health_check(
        plan,
        baseline,
        observations=(_observation(observed='{"value_db": 84.0}'),),
        created_at_utc='2026-09-23T01:00:00+00:00',
    )
    repository.save_run(run2)
    runs = repository.list_document_runs(revision.document_id)
    assert [run.run_id for run in runs] == [run1.run_id, run2.run_id]
    assert runs[1].assessments[0].state == 'changed'


def test_health_check_never_invents_a_cause(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    plan = build_health_check_plan(
        baseline, checks=(_check(),), created_at_utc=NOW
    )
    run = run_health_check(
        plan,
        baseline,
        observations=(_observation(observed='{"value_db": 84.0}'),),
        cause_hypothesis='speaker may have moved',
        created_at_utc=NOW,
    )
    assert run.assessments[0].state == 'changed'
    assert run.cause_hypothesis == 'speaker may have moved'


def test_cross_document_baseline_rejected(tmp_path: Path) -> None:
    """#744: a baseline cannot pin a SceneRevision from another document."""
    revision_a, repository = _repositories(tmp_path)
    repo_b = SceneRepository(tmp_path / 'cad.sqlite3')
    revision_b = repo_b.save(_scene('doc-2'), parent_revision_id=None).revision
    baseline = _baseline(
        revision_b,
        scene_revision_id=revision_a.revision_id,
        scene_content_hash=revision_a.content_hash,
    )
    with pytest.raises(ValueError, match='another document'):
        repository.save_baseline(baseline)


def test_cross_document_plan_and_run_rejected(tmp_path: Path) -> None:
    """#744: plan/run must stay inside the baseline's document."""
    revision, repository = _repositories(tmp_path)
    baseline = _baseline(revision)
    repository.save_baseline(baseline)
    plan = build_health_check_plan(
        baseline, checks=(_check(),), created_at_utc=NOW
    )

    forged = _reforge(plan, document_id='doc-2')
    with pytest.raises(ValueError, match='different document'):
        repository.save_plan(forged)


def test_forged_run_assessments_rejected(tmp_path: Path) -> None:
    """#744: a self-hashed run whose assessments do not reproduce is refused."""
    revision, repository = _repositories(tmp_path)
    baseline = _baseline(revision)
    repository.save_baseline(baseline)
    plan = build_health_check_plan(
        baseline, checks=(_check(),), created_at_utc=NOW
    )
    repository.save_plan(plan)

    run = run_health_check(
        plan,
        baseline,
        observations=(_observation(observed='{"value_db": 82.0}'),),
        created_at_utc=NOW,
    )
    repository.save_run(run)
    assert repository.list_document_runs(revision.document_id)

    # Forge assessments that claim 'changed' without any observation.
    forged = _reforge(
        run,
        run_id='forged-run',
        observations=(),
        assessments=(
            HealthCheckAssessment(
                check_id='check-level',
                state='changed',
                observed_delta_repr=(
                    '[{"pin_id": "pin-fl-level", "delta": 9.0}]'
                ),
                reason='invented drift',
            ),
        ),
    )
    with pytest.raises(ValueError, match='canonical'):
        repository.save_run(forged)

    # Observations for checks outside the plan are rejected too.
    ghost = _reforge(
        run,
        run_id='ghost-observation-run',
        observations=(_observation(check_id='ghost-check'),),
    )
    with pytest.raises(ValueError, match='outside the plan'):
        repository.save_run(ghost)


def test_frequency_metric_uses_hz_tolerance(tmp_path: Path) -> None:
    """#745: a Hz metric can never be thresholded with a dB tolerance."""
    revision, _repo = _repositories(tmp_path)
    freq_pin = HealthMetricPin(
        pin_id='pin-xo',
        domain='acoustic',
        metric_key='crossover_hz',
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-baseline', ref_sha256='a' * 64
        ),
        expected_repr='{"value_hz": 80.0}',
    )
    baseline = _baseline(
        revision,
        metric_pins=(freq_pin,),
        policy=ChangeDetectionPolicy(
            level_tolerance_db=0.5, frequency_tolerance_hz=2.0
        ),
    )
    check = _check(check_id='check-xo', pins=('pin-xo',))
    # 1 Hz drift is inside the 2 Hz frequency tolerance — the dB policy
    # must not be applied to a frequency metric.
    observation = _observation(
        check_id='check-xo', observed='{"value_hz": 81.0}'
    )
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'within_baseline'
    assert '"unit":"hz"' in (assessment.observed_delta_repr or '')

    observation = _observation(
        check_id='check-xo', observed='{"value_hz": 85.0}'
    )
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'changed'


def test_position_metric_uses_m_tolerance(tmp_path: Path) -> None:
    revision, _repo = _repositories(tmp_path)
    pos_pin = HealthMetricPin(
        pin_id='pin-mlp',
        domain='physical',
        metric_key='mlp_offset_m',
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-baseline', ref_sha256='a' * 64
        ),
        expected_repr='{"value_m": 0.0}',
    )
    baseline = _baseline(
        revision,
        metric_pins=(pos_pin,),
        policy=ChangeDetectionPolicy(
            level_tolerance_db=0.5, position_tolerance_m=0.3
        ),
    )
    check = _check(check_id='check-mlp', pins=('pin-mlp',))
    observation = _observation(
        check_id='check-mlp', observed='{"value_m": 0.2}'
    )
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'within_baseline'


def test_pin_tolerance_overrides_policy(tmp_path: Path) -> None:
    """#745: a compatible pin-specific tolerance wins over the policy."""
    revision, _repo = _repositories(tmp_path)
    pin = HealthMetricPin(
        pin_id='pin-strict',
        domain='acoustic',
        metric_key='mlp_level_db',
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-baseline', ref_sha256='a' * 64
        ),
        expected_repr='{"value_db": 82.0}',
        tolerance_repr='{"tolerance_db": 3.0}',
    )
    baseline = _baseline(
        revision,
        metric_pins=(pin,),
        policy=ChangeDetectionPolicy(level_tolerance_db=0.5),
    )
    check = _check(check_id='check-strict', pins=('pin-strict',))
    observation = _observation(
        check_id='check-strict', observed='{"value_db": 84.0}'
    )
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'within_baseline'


def test_unitless_metric_is_indeterminate(tmp_path: Path) -> None:
    """#745: unknown comparison semantics fail to indeterminate, never guess."""
    revision, _repo = _repositories(tmp_path)
    pin = HealthMetricPin(
        pin_id='pin-raw',
        domain='settings',
        metric_key='dsp_gain',
        evidence_ref=HealthAuthorityRef(
            kind='measurement', ref_id='m-baseline', ref_sha256='a' * 64
        ),
        expected_repr='{"value": 5.0}',
    )
    baseline = _baseline(
        revision,
        metric_pins=(pin,),
        policy=ChangeDetectionPolicy(level_tolerance_db=0.5),
    )
    check = _check(check_id='check-raw', pins=('pin-raw',))
    observation = _observation(check_id='check-raw', observed='{"value": 9.0}')
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'indeterminate'


def test_unit_mismatch_is_indeterminate(tmp_path: Path) -> None:
    """#745: an Hz observation can never be compared to a dB pin."""
    revision, _repo = _repositories(tmp_path)
    baseline = _baseline(revision)
    check = _check()
    observation = _observation(observed='{"value_hz": 84.0}')
    assessment = assess_health_observation(baseline, check, observation)
    assert assessment.state == 'indeterminate'
    assert 'unit mismatch' in (assessment.observed_delta_repr or '')
