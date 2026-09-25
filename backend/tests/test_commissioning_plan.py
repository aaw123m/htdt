"""Commissioning plan persistence and live status derivation (#588)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_empty_scene, make_f1_scene
from htdt.commissioning_plan import (
    CommissioningIntent,
    CommissioningPlanRepository,
    CommissioningService,
    CommissioningStage,
    new_plan,
)


def _intent() -> CommissioningIntent:
    return CommissioningIntent(
        is_new_project=True,
        has_existing_room=False,
        audio_only=False,
        rew_available=True,
        wants_hybrid_prediction=True,
        planned_speaker_count=5,
        goals=('迫力', '定位'),
    )


def test_plan_repository_roundtrip_and_latest(tmp_path) -> None:
    store = CommissioningPlanRepository(tmp_path)
    plan = new_plan('doc-a', 'theater-a', _intent())
    store.save(plan)

    loaded = store.get('doc-a')
    assert loaded is not None
    assert loaded.plan_id == plan.plan_id
    assert loaded.schema_version == 1
    assert loaded.intent.goals == ('迫力', '定位')
    assert loaded.stage == CommissioningStage.ROOM
    assert loaded.document_id == 'doc-a'
    assert not loaded.finished

    # latest() returns the most recently updated unfinished plan
    assert store.latest().document_id == 'doc-a'

    finished = dataclasses.replace(plan, finished=True)
    store.save(finished)
    assert store.latest() is None


def test_plan_schema_version_rejects_unreadable(tmp_path) -> None:
    store = CommissioningPlanRepository(tmp_path)
    (tmp_path / 'commissioning-plans.json').write_text(
        '{"plans": {"doc": {"plan_id": 123}}}', encoding='utf-8'
    )
    with pytest.raises(Exception):
        store.get('doc')


def test_requirements_derived_from_live_state(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)

    overview = SimpleNamespace(
        read=lambda document_id: SimpleNamespace(
            blockers=(
                SimpleNamespace(
                    code='room.missing', message='部屋がありません', action=None
                ),
                SimpleNamespace(
                    code='speaker.missing',
                    message='スピーカーがありません',
                    action=None,
                ),
            ),
            warnings=(
                SimpleNamespace(
                    code='measurement.missing',
                    message='測定がありません',
                    action=None,
                ),
            ),
        )
    )
    service = CommissioningService(repository, overview)
    plan = new_plan('doc-a', 'theater-a', _intent())

    requirements = {r.requirement_id: r for r in service.requirements(plan)}
    assert requirements['room.geometry'].status == 'pending'
    assert requirements['system.speakers'].status == 'pending'
    assert requirements['measurement.data'].status == 'pending'
    assert requirements['readiness.summary'].status == 'pending'


def test_skipped_requirements_stay_skipped(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    overview = SimpleNamespace(
        read=lambda document_id: SimpleNamespace(
            blockers=(), warnings=()
        )
    )
    service = CommissioningService(repository, overview)
    plan = dataclasses.replace(
        new_plan('doc-a', 'theater-a', _intent()),
        skipped_requirements=frozenset({'measurement.data'}),
    )
    requirements = {r.requirement_id: r for r in service.requirements(plan)}
    assert requirements['measurement.data'].status == 'skipped'
    assert requirements['room.geometry'].status == 'satisfied'
    assert requirements['readiness.summary'].status == 'satisfied'


# -- #899: intent-derived requirements ----------------------------------------


def _overview(blockers=(), warnings=()):
    return SimpleNamespace(
        read=lambda document_id: SimpleNamespace(
            blockers=blockers, warnings=warnings
        )
    )


def test_goals_and_flags_materially_change_requirement_sets(tmp_path) -> None:
    """Different declared intents produce different requirement graphs."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    service = CommissioningService(repository, _overview())

    quiet = new_plan(
        'doc-a',
        'archive',
        CommissioningIntent(
            is_new_project=True,
            has_existing_room=False,
            audio_only=True,
            rew_available=False,
            wants_hybrid_prediction=False,
            planned_speaker_count=0,
            goals=(),
        ),
    )
    requirements = {r.requirement_id: r for r in service.requirements(quiet)}
    # A document-only project can reach a useful state: no fabricated
    # measurement dependency, hybrid evidence step absent, listening point
    # not forced.
    assert 'prediction.hybrid_evidence' not in requirements
    assert requirements['measurement.data'].level == 'optional'
    assert requirements['system.listening'].level == 'optional'
    assert 'video' not in ''.join(requirements)

    ambitious = new_plan(
        'doc-a',
        'theater',
        CommissioningIntent(
            is_new_project=True,
            has_existing_room=False,
            audio_only=False,
            rew_available=True,
            wants_hybrid_prediction=True,
            planned_speaker_count=5,
            goals=('迫力',),
        ),
    )
    requirements = {
        r.requirement_id: r for r in service.requirements(ambitious)
    }
    assert requirements['measurement.data'].level == 'required'
    assert requirements['system.listening'].level == 'required'
    assert requirements['prediction.hybrid_evidence'].level == 'required'

    rew_only = new_plan(
        'doc-a',
        'room',
        CommissioningIntent(
            is_new_project=True,
            has_existing_room=False,
            audio_only=False,
            rew_available=True,
            wants_hybrid_prediction=False,
            planned_speaker_count=0,
            goals=(),
        ),
    )
    requirements = {r.requirement_id: r for r in service.requirements(rew_only)}
    assert requirements['measurement.data'].level == 'recommended'


def test_planned_speaker_count_is_a_delta_not_an_absolute(tmp_path) -> None:
    """Declared topology intent: satisfied only when roles match the plan."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = CommissioningService(
        repository, _overview(blockers=(), warnings=())
    )
    # F1 defines FL/C/FR — three role-defined speakers.
    intent = CommissioningIntent(
        is_new_project=True,
        has_existing_room=True,
        audio_only=False,
        rew_available=True,
        wants_hybrid_prediction=False,
        planned_speaker_count=5,
        goals=('迫力',),
    )
    plan = new_plan(F1_DOCUMENT_ID, 'f1', intent)
    requirements = {r.requirement_id: r for r in service.requirements(plan)}
    speaker_req = requirements['system.speakers']
    assert speaker_req.status == 'pending'
    assert '5' in speaker_req.reason and '3' in speaker_req.reason
    assert speaker_req.link is not None

    matching = new_plan(
        F1_DOCUMENT_ID,
        'f1',
        dataclasses.replace(intent, planned_speaker_count=3),
    )
    requirements = {r.requirement_id: r for r in service.requirements(matching)}
    assert requirements['system.speakers'].status == 'satisfied'


def test_rew_availability_changes_guidance_not_verdict(tmp_path) -> None:
    """REW availability steers the copy; it cannot fabricate readiness."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    warning = SimpleNamespace(
        code='measurement.missing', message='測定がありません', action=None
    )
    service = CommissioningService(repository, _overview(warnings=(warning,)))

    with_rew = new_plan('doc-a', 'a', _intent())
    without_rew = new_plan(
        'doc-a',
        'a',
        dataclasses.replace(_intent(), rew_available=False),
    )
    req_rew = {
        r.requirement_id: r for r in service.requirements(with_rew)
    }['measurement.data']
    req_no_rew = {
        r.requirement_id: r for r in service.requirements(without_rew)
    }['measurement.data']
    assert req_rew.status == 'pending' and req_no_rew.status == 'pending'
    assert req_rew.reason != req_no_rew.reason


def test_hybrid_evidence_tracks_measurement_authority(tmp_path) -> None:
    """The hybrid opt-in adds a real evidence requirement, not a no-op."""

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    warning = SimpleNamespace(
        code='measurement.missing', message='測定がありません', action=None
    )
    service = CommissioningService(repository, _overview(warnings=(warning,)))
    plan = new_plan('doc-a', 'a', _intent())  # wants_hybrid_prediction=True
    requirements = {r.requirement_id: r for r in service.requirements(plan)}
    hybrid = requirements['prediction.hybrid_evidence']
    assert hybrid.status == 'pending'
    assert hybrid.link is not None

    measured = CommissioningService(repository, _overview())
    requirements = {
        r.requirement_id: r for r in measured.requirements(plan)
    }
    assert requirements['prediction.hybrid_evidence'].status == 'satisfied'
