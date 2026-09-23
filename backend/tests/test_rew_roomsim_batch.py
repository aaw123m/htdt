from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request

import pytest

from htdt.rew_api import RewFrequencyResponse, RewRoomSimFrequencyResponse, RewRoomSimSnapshot
from htdt.rew_roomsim_batch import (
    RewRoomSimBatchError,
    RewRoomSimConcurrentChange,
    RewRoomSimControlClient,
    RewRoomSimPositionBatchRequest,
    RewRoomSimRestoreError,
    roomsim_state_sha256,
    run_roomsim_position_batch,
)


FIXTURE = Path(__file__).parent / 'fixtures' / 'rew_5_40_beta135_roomsim_state.json'


def _snapshot() -> RewRoomSimSnapshot:
    state = json.loads(FIXTURE.read_text(encoding='utf-8'))
    sources = {
        name: {
            'position_rew': deepcopy(detail['position']),
            'position_htdt': {
                'x_m': float(detail['position']['fromLeft']),
                'y_m': float(state['room_size']['length']) - float(detail['position']['fromRear']),
                'z_m': float(detail['position']['fromFloor']),
            },
            'configuration': deepcopy(detail['configuration']),
        }
        for name, detail in state['sources']['details'].items()
    }
    head = state['head_position']
    return RewRoomSimSnapshot(
        rew_version=state['version']['message'],
        room_size=deepcopy(state['room_size']),
        room_is_sealed=state['room_is_sealed'],
        absorptions=deepcopy(state['absorptions']),
        options=deepcopy(state['options']),
        head_position_rew=deepcopy(head),
        head_position_htdt={
            'x_m': float(head['fromLeft']),
            'y_m': float(state['room_size']['length']) - float(head['fromRear']),
            'z_m': float(head['fromFloor']),
        },
        mic_position_offsets=deepcopy(state['mic_posn_offsets']),
        active_sources=tuple(state['sources']['active']),
        recognized_sources=tuple(state['sources']['recognized']),
        mic_positions=tuple(state['mic_positions']),
        sources=sources,
    )


class FakeControl:
    def __init__(self) -> None:
        self.state = _snapshot()
        self.response_reads = 0
        self.external_change_after_response = False
        self.paired_source_side_effect = False
        self.fail_restore = False

    def get_roomsim_snapshot(self) -> RewRoomSimSnapshot:
        return deepcopy(self.state)

    def set_roomsim_head_position(self, position_rew) -> None:
        if self.fail_restore and position_rew == _snapshot().head_position_rew:
            return
        payload = self.state.__dict__.copy()
        payload['head_position_rew'] = dict(position_rew)
        payload['head_position_htdt'] = {
            'x_m': float(position_rew['fromLeft']),
            'y_m': float(self.state.room_size['length']) - float(position_rew['fromRear']),
            'z_m': float(position_rew['fromFloor']),
        }
        self.state = RewRoomSimSnapshot(**payload)

    def set_roomsim_source_position(self, source_name: str, position_rew) -> None:
        sources = deepcopy(self.state.sources)
        sources[source_name]['position_rew'] = dict(position_rew)
        sources[source_name]['position_htdt'] = {
            'x_m': float(position_rew['fromLeft']),
            'y_m': float(self.state.room_size['length']) - float(position_rew['fromRear']),
            'z_m': float(position_rew['fromFloor']),
        }
        if self.paired_source_side_effect and source_name == 'Left':
            paired = sources['Right']['position_rew'].copy()
            paired['fromLeft'] = 2.8
            sources['Right']['position_rew'] = paired
            sources['Right']['position_htdt'] = {
                'x_m': 2.8,
                'y_m': float(self.state.room_size['length']) - float(paired['fromRear']),
                'z_m': float(paired['fromFloor']),
            }
        payload = self.state.__dict__.copy()
        payload['sources'] = sources
        self.state = RewRoomSimSnapshot(**payload)

    def get_roomsim_frequency_response(self, *, mic_position='Main', source_name=None):
        self.response_reads += 1
        result = RewRoomSimFrequencyResponse(
            source_name=source_name,
            mic_position=mic_position,
            message='fixture',
            unit='SPL',
            smoothing='None',
            start_frequency_hz=20.0,
            points_per_octave=96.0,
            frequency_step_hz=None,
            frequency_hz=(20.0, 40.0, 80.0),
            magnitude=(80.0, 81.0, 79.0),
            phase_deg=(0.0, 1.0, 2.0),
        )
        if self.external_change_after_response:
            payload = self.state.__dict__.copy()
            options = deepcopy(self.state.options)
            options['crossoverFrequencyHz'] = 90
            payload['options'] = options
            self.state = RewRoomSimSnapshot(**payload)
        return result


def _request() -> RewRoomSimPositionBatchRequest:
    return RewRoomSimPositionBatchRequest(
        candidate_id='candidate-a',
        room_width_m=4.0,
        room_depth_m=5.0,
        room_height_m=2.4,
        head_position_htdt={'x_m': 2.0, 'y_m': 3.2, 'z_m': 1.0},
        source_positions_htdt={
            'Left': {'x_m': 1.1, 'y_m': 1.0, 'z_m': 1.0},
            'Right': {'x_m': 2.9, 'y_m': 1.0, 'z_m': 1.0},
            'Sub1': {'x_m': 0.15, 'y_m': 0.3, 'z_m': 0.15},
        },
    )


def test_position_batch_returns_response_only_after_exact_restore() -> None:
    control = FakeControl()
    before = control.get_roomsim_snapshot()
    result = run_roomsim_position_batch(control, _request())

    assert result.candidate_id == 'candidate-a'
    assert result.model_id == 'rew-room-simulator'
    assert result.model_version == '5.40 Beta 135 API 0.9.8'
    assert result.pre_state_sha256 == result.restored_state_sha256
    assert result.applied_state_sha256 != result.pre_state_sha256
    assert result.response.magnitude == (80.0, 81.0, 79.0)
    assert control.response_reads == 1
    assert roomsim_state_sha256(control.get_roomsim_snapshot()) == roomsim_state_sha256(before)


def test_position_batch_detects_external_change_and_does_not_return_result() -> None:
    control = FakeControl()
    control.external_change_after_response = True

    with pytest.raises(RewRoomSimRestoreError, match='transaction failed and restore was not clean'):
        run_roomsim_position_batch(control, _request())


def test_position_batch_rejects_inactive_source_before_writes() -> None:
    control = FakeControl()
    request = RewRoomSimPositionBatchRequest(
        candidate_id='candidate-a',
        room_width_m=4.0,
        room_depth_m=5.0,
        room_height_m=2.4,
        head_position_htdt={'x_m': 2.0, 'y_m': 3.2, 'z_m': 1.0},
        source_positions_htdt={'Sub8': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 0.2}},
        source_name='Sub8',
    )

    with pytest.raises(RewRoomSimBatchError, match='not active'):
        run_roomsim_position_batch(control, request)
    assert control.response_reads == 0


def test_position_batch_restores_unrequested_source_side_effect() -> None:
    control = FakeControl()
    before_hash = roomsim_state_sha256(control.get_roomsim_snapshot())
    control.paired_source_side_effect = True
    request = RewRoomSimPositionBatchRequest(
        candidate_id='candidate-a',
        room_width_m=4.0,
        room_depth_m=5.0,
        room_height_m=2.4,
        head_position_htdt={'x_m': 2.0, 'y_m': 3.2, 'z_m': 1.0},
        source_positions_htdt={'Left': {'x_m': 1.1, 'y_m': 1.0, 'z_m': 1.0}},
        source_name='Left',
    )

    with pytest.raises(RewRoomSimConcurrentChange, match='exact candidate state'):
        run_roomsim_position_batch(control, request)

    assert roomsim_state_sha256(control.get_roomsim_snapshot()) == before_hash


def test_combined_response_requires_exact_active_source_coverage() -> None:
    control = FakeControl()
    request = RewRoomSimPositionBatchRequest(
        candidate_id='candidate-a',
        room_width_m=4.0,
        room_depth_m=5.0,
        room_height_m=2.4,
        head_position_htdt={'x_m': 2.0, 'y_m': 3.2, 'z_m': 1.0},
        source_positions_htdt={
            'Left': {'x_m': 1.1, 'y_m': 1.0, 'z_m': 1.0},
            'Right': {'x_m': 2.9, 'y_m': 1.0, 'z_m': 1.0},
        },
    )

    with pytest.raises(RewRoomSimBatchError, match='every active source'):
        run_roomsim_position_batch(control, request)
    assert control.response_reads == 0


def test_position_batch_rejects_room_dimension_mismatch() -> None:
    control = FakeControl()
    request = _request()
    wrong = RewRoomSimPositionBatchRequest(
        candidate_id=request.candidate_id,
        room_width_m=6.0,
        room_depth_m=request.room_depth_m,
        room_height_m=request.room_height_m,
        head_position_htdt=request.head_position_htdt,
        source_positions_htdt=request.source_positions_htdt,
    )
    with pytest.raises(RewRoomSimBatchError, match='dimensions do not match'):
        run_roomsim_position_batch(control, wrong)
    assert control.response_reads == 0


def test_position_batch_restore_failure_is_hard_error() -> None:
    control = FakeControl()
    control.fail_restore = True

    with pytest.raises(RewRoomSimRestoreError, match='did not restore'):
        run_roomsim_position_batch(control, _request())


class FakeResponse:
    def __init__(self, payload=None) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, amt: int = -1) -> bytes:
        raw = b'' if self.payload is None else json.dumps(self.payload).encode('utf-8')
        return raw if amt is None or amt < 0 else raw[:amt]


def test_write_client_uses_documented_roomsim_post_routes() -> None:
    calls: list[tuple[str, str, dict]] = []

    def opener(request: Request, timeout: float):
        calls.append((
            urlparse(request.full_url).path,
            request.get_method(),
            json.loads(request.data.decode('utf-8')),
        ))
        return FakeResponse()

    client = RewRoomSimControlClient(opener=opener)
    client.set_roomsim_head_position({
        'unit': 'metres',
        'fromRear': 1.8,
        'fromLeft': 2.0,
        'fromFloor': 1.0,
    })
    client.set_roomsim_source_position('Left', {
        'unit': 'metres',
        'fromRear': 4.0,
        'fromLeft': 1.1,
        'fromFloor': 1.0,
    })

    assert calls == [
        (
            '/roomsim/head-position',
            'POST',
            {'unit': 'metres', 'fromRear': 1.8, 'fromLeft': 2.0, 'fromFloor': 1.0},
        ),
        (
            '/roomsim/Left/position',
            'POST',
            {'unit': 'metres', 'fromRear': 4.0, 'fromLeft': 1.1, 'fromFloor': 1.0},
        ),
    ]
