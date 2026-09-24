from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_room_operating_state import (
    CurtainState,
    HvacState,
    MovableConfiguration,
    OperatingOpeningState,
    RoomOperatingState,
    build_operating_state,
    evaluate_operating_state_freshness,
)
from htdt.cad_room_operating_state_repository import (
    CadRoomOperatingStateRepository,
    OperatingStateConflictError,
)
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.cad_wall_models import WallOpening
from htdt.cad_walls import make_wall_topology, add_opening

NOW = '2026-09-24T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    room = RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4)
    topology = make_wall_topology(room)
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id='door-1',
            wall_id='wall:rear-right->rear-left',
            offset_m=1.0,
            width_m=0.9,
            sill_m=0.0,
            height_m=2.1,
        ),
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=3,
        room=room,
        wall_topology=topology,
        entities=(),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    return scene_repository, revision, CadRoomOperatingStateRepository(
        scene_repository
    )


def _state(revision, **overrides) -> RoomOperatingState:
    payload = {
        'document_id': revision.document_id,
        'name': 'Listening session',
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'curtains': (
            CurtainState(
                curtain_id='curtain-left',
                label='Left absorptive curtain',
                coverage_fraction=1.0,
                opening_id='window-1',
            ),
        ),
        'openings': (OperatingOpeningState(opening_id='door-1', is_open=False),),
        'hvac': HvacState(is_on=False, description='mini-split off'),
        'movable_configurations': (
            MovableConfiguration(
                configuration_id='chairs-folded',
                label='Folding chairs against wall',
                in_effect=True,
            ),
        ),
        'observed_at_utc': NOW,
    }
    payload.update(overrides)
    return build_operating_state(**payload)


def test_state_records_declared_conditions(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    state = _state(revision)
    repository.save_state(state)
    assert repository.get_state(state.state_id, state.version) == state
    assert repository.get_state_by_hash(state.semantic_sha256) == state
    stored = repository.list_states(revision.document_id)[0]
    assert stored.openings[0].is_open is False
    assert stored.hvac.is_on is False
    assert stored.movable_configurations[0].in_effect is True


def test_versions_append_old_states_never_rewritten(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    v1 = _state(revision, state_id='session', version='1')
    repository.save_state(v1)
    v2 = _state(
        revision,
        state_id='session',
        version='2',
        openings=(OperatingOpeningState(opening_id='door-1', is_open=True),),
    )
    repository.save_state(v2)
    with pytest.raises(OperatingStateConflictError):
        repository.save_state(v1)
    assert [item.version for item in repository.list_state_versions('session')] == [
        '1', '2',
    ]
    assert repository.get_state('session', '1').openings[0].is_open is False


def test_save_requires_the_exact_pinned_revision(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    foreign = _state(revision, scene_content_hash='0' * 64)
    with pytest.raises(ValueError, match='content hash mismatch'):
        repository.save_state(foreign)


def test_freshness_tracks_scene_and_opening_anchors() -> None:
    state = build_operating_state(
        document_id='doc-1',
        name='state',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        openings=(OperatingOpeningState(opening_id='door-1', is_open=True),),
        observed_at_utc=NOW,
    )
    current = evaluate_operating_state_freshness(
        state, scene_content_hash='a' * 64, present_opening_ids=('door-1',)
    )
    assert current.status == 'current'
    stale = evaluate_operating_state_freshness(
        state, scene_content_hash='b' * 64, present_opening_ids=('door-1',)
    )
    assert stale.status == 'stale'
    missing = evaluate_operating_state_freshness(
        state, scene_content_hash='a' * 64, present_opening_ids=('window-1',)
    )
    assert missing.status == 'missing'
    assert any('opening' in reason for reason in missing.reasons)


def test_coverage_fraction_is_bounded() -> None:
    with pytest.raises(ValidationError):
        CurtainState(
            curtain_id='c1', label='c', coverage_fraction=1.5
        )
