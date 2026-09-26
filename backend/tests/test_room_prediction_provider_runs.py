from __future__ import annotations

from threading import Event

import pytest

from htdt.cad_prediction_models import CadPredictionResult
from htdt.cad_provider_response import provider_response_request_identity
from htdt.room_prediction import RoomPredictionController
from htdt.room_prediction_options import (
    HYBRID_MODEL_KEY,
    hybrid_model_key,
    provider_model_key,
)
from htdt.room_workspace import RoomWorkspaceController

from test_cad_prediction_provider import _fixture as _r170a_fixture
from test_cad_hybrid_prediction_provider import _build_bundle


R170A_RECEIVER_ENTITY = 'receiver-1-entity'


def _r170a_controller(tmp_path):
    fixture = _r170a_fixture(tmp_path)
    provider = fixture.build_provider()
    provider_repository = fixture.repository()
    provider_repository.save_provider(provider)
    room = RoomWorkspaceController(fixture.scene_repository, 'r170a-fixture')
    prediction = RoomPredictionController(
        fixture.scene_repository,
        room,
        provider_repository=provider_repository,
    )
    return fixture, provider, prediction


def test_wave_provider_lane_is_runnable_and_persists_exact_response(tmp_path) -> None:
    # #938: a READY R170A provider lane is an executable Room prediction —
    # the run binds the exact sealed provider authority into the canonical
    # request and persists the stored frequency response.
    fixture, provider, prediction = _r170a_controller(tmp_path)

    options = prediction.prediction_options(
        R170A_RECEIVER_ENTITY, max_mode_hz=80.0
    )
    wave = next(
        option
        for option in options
        if option.model_key == provider_model_key(provider.provider_id)
    )
    assert wave.state == 'READY'
    assert wave.runnable is True

    spec = prediction.prepare_run(
        R170A_RECEIVER_ENTITY,
        model_key=wave.model_key,
        max_mode_hz=80.0,
    )
    results = prediction._analyze(spec, Event())
    assert results is not None
    assert len(results) == 1
    result = results[0]
    assert result.result_kind == 'provider_frequency_response'
    assert result.provider_response is not None
    assert result.provider_response.provider_id == provider.provider_id
    assert result.provider_response.provider_semantic_sha256 == (
        provider.semantic_sha256
    )
    assert result.modes == () and result.reflections == ()

    expected = provider.frequency_response('receiver-1')
    assert result.provider_response.frequency_hz == expected.frequency_hz
    assert result.provider_response.level_db == expected.level_db

    accepted = prediction.accept_results(spec, results)
    assert accepted == results
    stored = prediction.prediction_repository.list_run(result.run_id)
    assert stored == results
    assert prediction.result_is_current(stored[0]) is True

    interpretation = prediction.interpretation_for(stored)
    assert interpretation is not None
    assert interpretation.reliability.provider_stale_state == 'CURRENT'

    prediction.dispose()


def test_hybrid_provider_lane_is_runnable_and_interpreted(tmp_path, monkeypatch) -> None:
    # #938: R170B providers make the hybrid lane a real runnable option —
    # the placeholder UNSUPPORTED row only remains when none exist.
    bundle = _build_bundle(tmp_path / 'hybrid', monkeypatch)
    provider = bundle['provider']
    bundle['hybrid_repository'].save(provider)
    fixture = bundle['fixture']
    document_id = fixture['revision'].document_id
    receiver_entity_id = bundle['candidate'].receivers[0].entity_id

    room = RoomWorkspaceController(fixture['scene_repository'], document_id)
    prediction = RoomPredictionController(
        fixture['scene_repository'],
        room,
        hybrid_provider_repository=bundle['hybrid_repository'],
    )

    options = prediction.prediction_options(receiver_entity_id, max_mode_hz=80.0)
    keys = {option.model_key for option in options}
    assert HYBRID_MODEL_KEY not in keys
    hybrid = next(
        option
        for option in options
        if option.model_key == hybrid_model_key(provider.provider_id)
    )
    assert hybrid.state == 'READY'
    assert hybrid.runnable is True

    spec = prediction.prepare_run(
        receiver_entity_id,
        model_key=hybrid.model_key,
        max_mode_hz=80.0,
    )
    results = prediction._analyze(spec, Event())
    assert results is not None and len(results) == 1
    result = results[0]
    assert result.result_kind == 'provider_frequency_response'
    assert result.provider_response is not None
    assert result.provider_response.provider_semantic_sha256 == (
        provider.semantic_sha256
    )

    accepted = prediction.accept_results(spec, results)
    assert accepted == results
    assert prediction.prediction_repository.list_run(result.run_id) == results
    assert prediction.result_is_current(accepted[0]) is True

    interpretation = prediction.interpretation_for(accepted)
    assert interpretation is not None
    assert interpretation.reliability.provider_stale_state == 'CURRENT'

    prediction.dispose()


def test_provider_lane_blocked_when_evidence_is_stale(tmp_path) -> None:
    _fixture, provider, prediction = _r170a_controller(tmp_path)

    # A committed edit yields a new SceneRevision; the provider's pinned
    # authority no longer binds it, so the lane must fail closed.
    room = prediction.room_controller
    room.add_object('furniture')
    assert room.save() is True

    options = prediction.prediction_options(
        R170A_RECEIVER_ENTITY, max_mode_hz=80.0
    )
    wave = next(
        option
        for option in options
        if option.model_key == provider_model_key(provider.provider_id)
    )
    assert wave.state == 'BLOCKED'
    assert wave.runnable is False

    with pytest.raises(ValueError):
        prediction.prepare_run(
            R170A_RECEIVER_ENTITY,
            model_key=wave.model_key,
            max_mode_hz=80.0,
        )

    prediction.dispose()


def test_provider_lane_rejects_non_member_receiver_and_wide_band(tmp_path) -> None:
    fixture, provider, prediction = _r170a_controller(tmp_path)

    # A receiver ineligible as a listener (e.g. a speaker) fails closed at
    # target resolution: only the BLOCKED rectangular option is returned.
    options = prediction.prediction_options('source-1', max_mode_hz=80.0)
    assert [option.model_key for option in options] == ['rectangular']
    assert options[0].state == 'BLOCKED'

    # The provider receiver set is enforced inside the canonical request too:
    # an entity the provider never covered can never mint a lane identity.
    with pytest.raises(ValueError, match='not part of the provider'):
        provider_response_request_identity(
            provider,
            fixture.scene_repository.latest('r170a-fixture'),
            'source-1',
            max_mode_hz=80.0,
        )

    options = prediction.prediction_options(
        R170A_RECEIVER_ENTITY, max_mode_hz=200.0
    )
    wave = next(
        option
        for option in options
        if option.model_key == provider_model_key(provider.provider_id)
    )
    assert wave.state == 'BLOCKED'
    assert wave.runnable is False

    prediction.dispose()


def test_hybrid_placeholder_stays_unsupported_without_providers(tmp_path) -> None:
    fixture = _r170a_fixture(tmp_path)
    room = RoomWorkspaceController(fixture.scene_repository, 'r170a-fixture')
    prediction = RoomPredictionController(
        fixture.scene_repository, room
    )

    options = prediction.prediction_options(
        R170A_RECEIVER_ENTITY, max_mode_hz=80.0
    )
    hybrid = next(
        option for option in options if option.model_key == HYBRID_MODEL_KEY
    )
    assert hybrid.state == 'UNSUPPORTED'
    assert hybrid.runnable is False

    prediction.dispose()


def test_tampered_provider_request_fails_closed_on_persist(tmp_path) -> None:
    # The provider lane replays like any other model identity: mutating the
    # canonical request snapshot after the run must reject persistence.
    _fixture, provider, prediction = _r170a_controller(tmp_path)

    spec = prediction.prepare_run(
        R170A_RECEIVER_ENTITY,
        model_key=provider_model_key(provider.provider_id),
        max_mode_hz=80.0,
    )
    results = prediction._analyze(spec, Event())
    assert results is not None

    tampered = results[0].model_copy(
        update={
            'input_snapshot_json': results[0].input_snapshot_json.replace(
                R170A_RECEIVER_ENTITY, 'receiver-2-entity'
            )
        }
    )
    assert isinstance(tampered, CadPredictionResult)
    with pytest.raises(ValueError):
        prediction.prediction_repository.save_run((tampered,))

    prediction.dispose()
