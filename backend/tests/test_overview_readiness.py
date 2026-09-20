from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from htdt.cad_repository import SceneRevision
from htdt.overview_readiness import OverviewReadinessService


@dataclass
class _SceneSource:
    revision: SceneRevision | None

    def latest(self, document_id: str) -> SceneRevision | None:
        if self.revision is None or self.revision.document_id != document_id:
            return None
        return self.revision


@dataclass
class _MeasurementSource:
    measurements: tuple = ()
    datasets: dict | None = None

    def list_measurements(self, document_id: str) -> tuple:
        return self.measurements

    def dataset_for_measurement(self, measurement_id: str):
        return (self.datasets or {}).get(measurement_id)


@dataclass
class _PredictionSource:
    results: tuple = ()

    def list_results(self, document_id: str) -> tuple:
        return self.results


@dataclass
class _SearchSource:
    specs: tuple = ()

    def list_specs(self, document_id: str) -> tuple:
        return self.specs


@dataclass
class _ValidationSource:
    records: dict | None = None

    def list_for_search_spec(self, search_spec_id: str) -> tuple:
        return (self.records or {}).get(search_spec_id, ())


def _revision(
    *,
    room: bool = True,
    speakers: tuple[tuple[str, str | None], ...] = (('speaker-fl', 'FL'),),
    content_hash: str = 'a' * 64,
) -> SceneRevision:
    entities = tuple(
        SimpleNamespace(entity_id=entity_id, kind='speaker', speaker_role=role)
        for entity_id, role in speakers
    )
    document = SimpleNamespace(
        room=SimpleNamespace(room_id='room') if room else None,
        entities=entities,
    )
    return SceneRevision(
        revision_id='revision-current',
        document_id='project-1',
        parent_revision_id=None,
        created_at_utc='2026-09-18T00:00:00+00:00',
        content_hash=content_hash,
        document=document,
    )


def _service(
    revision: SceneRevision | None,
    *,
    measurements: tuple = (),
    datasets: dict | None = None,
    predictions: tuple = (),
    specs: tuple = (),
    validations: dict | None = None,
) -> OverviewReadinessService:
    return OverviewReadinessService(
        _SceneSource(revision),
        _MeasurementSource(measurements, datasets),
        _PredictionSource(predictions),
        _SearchSource(specs),
        _ValidationSource(validations),
    )


def _current_prediction(revision: SceneRevision):
    return SimpleNamespace(
        status='completed',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )


def test_missing_room_is_primary_blocker() -> None:
    view = _service(_revision(room=False)).read('project-1')

    assert view.summary == '部屋形状を完成させてください。'
    assert view.blockers[0].code == 'room.geometry_incomplete'
    assert view.next_action is not None
    assert view.next_action.label == '部屋を完成させる'
    assert view.next_action.target.workspace == 'room'
    assert view.next_action.target.subsection == 'geometry'
    assert view.optimization_ready is False


def test_missing_speaker_role_deep_links_to_entity_without_showing_internal_id() -> None:
    view = _service(
        _revision(speakers=(('speaker-internal-uuid-like-id', None),))
    ).read('project-1')

    blocker = next(item for item in view.blockers if item.code == 'speaker.role_missing')
    assert blocker.message == '役割が未設定のスピーカーがあります。'
    assert 'speaker-internal-uuid-like-id' not in blocker.message
    assert blocker.action is not None
    assert blocker.action.target.workspace == 'room'
    assert blocker.action.target.subsection == 'placement'
    assert blocker.action.target.entity_id == 'speaker-internal-uuid-like-id'
    assert view.next_action == blocker.action


@pytest.mark.parametrize('phase_status', ['absent', 'unknown'])
def test_measurement_phase_capability_uses_authority_status(phase_status: str) -> None:
    revision = _revision()
    measurement = SimpleNamespace(measurement_id='measurement-1')
    dataset = SimpleNamespace(phase_status=phase_status)

    view = _service(
        revision,
        measurements=(measurement,),
        datasets={'measurement-1': dataset},
        predictions=(_current_prediction(revision),),
    ).read('project-1')

    warning = next(
        item for item in view.warnings if item.code == 'measurement.phase_timing_unavailable'
    )
    assert warning.message == 'タイミング/位相比較に使える測定が確認できません。'
    assert warning.action is not None
    assert warning.action.target.workspace == 'measurement'
    assert warning.action.target.subsection == 'quality'


def test_stale_prediction_becomes_primary_recompute_action() -> None:
    revision = _revision(content_hash='b' * 64)
    stale = SimpleNamespace(
        status='completed',
        scene_revision_id='revision-old',
        scene_content_hash='a' * 64,
    )

    view = _service(revision, predictions=(stale,)).read('project-1')

    warning = next(item for item in view.warnings if item.code == 'prediction.stale')
    assert warning.message == '条件が変更されています。予測を再計算してください。'
    assert view.next_action == warning.action
    assert view.next_action is not None
    assert view.next_action.target.workspace == 'room'
    assert view.next_action.target.subsection == 'acoustics'
    assert view.optimization_ready is False


def test_validation_gate_is_reported_without_recomputing_or_leaking_gate_ids() -> None:
    revision = _revision()
    spec = SimpleNamespace(
        search_spec_id='search-current',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='c' * 64,
    )
    validation = SimpleNamespace(
        recommendation_gate='disabled',
        gate_reasons=('objective trend internal-objective-123 is fail',),
    )

    view = _service(
        revision,
        predictions=(_current_prediction(revision),),
        specs=(spec,),
        validations={'search-current': (validation,)},
    ).read('project-1', constraint_workspace_hash='c' * 64)

    blocker = next(
        item for item in view.blockers if item.code == 'validation.recommendation_blocked'
    )
    assert blocker.message == (
        '自動推薦はまだ利用できません。目的指標の傾向が検証条件を満たしていません。'
    )
    assert 'internal-objective-123' not in blocker.message
    assert view.optimization_ready is True
    assert view.next_action == blocker.action
    assert view.next_action is not None
    assert view.next_action.target.workspace == 'optimization'
    assert view.next_action.target.subsection == 'validation'


def test_constraint_hash_filters_validation_status_when_context_is_supplied() -> None:
    revision = _revision()
    old_spec = SimpleNamespace(
        search_spec_id='search-old-constraints',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='1' * 64,
    )
    current_spec = SimpleNamespace(
        search_spec_id='search-current-constraints',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='2' * 64,
    )
    blocked = SimpleNamespace(
        recommendation_gate='disabled',
        gate_reasons=('independent holdout evidence is required',),
    )

    view = _service(
        revision,
        predictions=(_current_prediction(revision),),
        specs=(old_spec, current_spec),
        validations={'search-old-constraints': (blocked,)},
    ).read('project-1', constraint_workspace_hash='2' * 64)

    assert all(item.code != 'validation.recommendation_blocked' for item in view.blockers)
    assert view.optimization_ready is True
    assert view.next_action is not None
    assert view.next_action.action_id == 'optimization.open_setup'


def test_ready_state_can_recommend_optimization_even_without_optional_measurement() -> None:
    revision = _revision()

    view = _service(
        revision,
        predictions=(_current_prediction(revision),),
    ).read('project-1')

    assert view.summary == '最適化の準備ができています。'
    assert view.optimization_ready is True
    assert any(item.code == 'measurement.missing' for item in view.warnings)
    assert view.next_action is not None
    assert view.next_action.label == '最適化を始める'
    assert view.next_action.target.workspace == 'optimization'
    assert view.next_action.target.subsection == 'setup'


def test_unsupported_current_prediction_does_not_unlock_optimization() -> None:
    revision = _revision()
    unsupported = SimpleNamespace(
        status='completed',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        geometry_compatibility='unsupported',
    )

    view = _service(revision, predictions=(unsupported,)).read('project-1')

    warning = next(
        item for item in view.warnings if item.code == 'prediction.unsupported_geometry'
    )
    assert warning.message == (
        '現在の部屋形状は予測モデルに対応していません。'
        '矩形の部屋に変更すると予測できます。'
    )
    assert view.summary == '現在の部屋形状では予測できません。'
    assert view.optimization_ready is False
    assert view.next_action == warning.action
    assert view.next_action is not None
    assert view.next_action.target.workspace == 'room'
    assert view.next_action.target.subsection == 'geometry'
