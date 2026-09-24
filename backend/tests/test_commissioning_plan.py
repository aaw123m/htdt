"""Commissioning plan persistence and live status derivation (#588)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
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
