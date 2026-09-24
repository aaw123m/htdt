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
    HealthCheckItem,
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
