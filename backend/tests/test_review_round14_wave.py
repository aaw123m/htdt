"""Round-14 review: solver / prediction-lane consistency.

Instrumented proof that the lane a user selects dispatches the
implementation that was selected — and that the persisted result's
authority keys name the solver that actually ran, never the requested
label. These tests record calls into the two analyzers behind
``RoomPredictionController._analyze`` rather than trusting the
``model_key`` label.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

import htdt.room_prediction as room_prediction
from htdt.cad_acoustic_solver_result import CadAcousticSolverResultRepository
from htdt.cad_candidate_wave_execution import (
    CandidateWaveExecutionError,
    ExactJsonAuthorityStore,
    PffdtdCandidateWaveExecutor,
)
from htdt.cad_provider_response import (
    HYBRID_RESPONSE_MODEL_ID,
    PROVIDER_RESPONSE_MODEL_ID,
)
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_wave_excitation import CadWaveExcitationRepository
from htdt.cad_predictions import RECTANGULAR_GEOMETRY_MODEL_ID
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.r120_geometry_compiler_repository import R120GeometryCompilerRepository
from htdt.room_prediction import RoomPredictionController
from htdt.room_prediction_options import (
    HYBRID_MODEL_KEY,
    RECTANGULAR_MODEL_KEY,
    WAVE_MODEL_KEY_PREFIX,
    hybrid_model_key,
    provider_model_key,
)
from htdt.room_workspace import RoomWorkspaceController

from test_cad_geometric_acoustics_adapter import _fixture as _ga_fixture  # noqa: E402
from test_cad_hybrid_prediction_provider import _build_bundle  # noqa: E402


RECEIVER_ENTITY = 'receiver-1'


def _all_lane_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One scene exposing all three READY lanes on the same receiver."""
    bundle = _build_bundle(tmp_path / 'hybrid', monkeypatch)
    provider = bundle['provider']
    bundle['hybrid_repository'].save(provider)
    fixture = bundle['fixture']
    room = RoomWorkspaceController(
        fixture['scene_repository'],
        fixture['revision'].document_id,
    )
    prediction = RoomPredictionController(
        fixture['scene_repository'],
        room,
        provider_repository=bundle['base_repository'],
        hybrid_provider_repository=bundle['hybrid_repository'],
    )
    return bundle, provider, prediction


def _recording_analyzers(monkeypatch: pytest.MonkeyPatch):
    """Patch both analyzers behind `_analyze` with call-recorders."""
    calls: dict[str, list[object]] = {'rectangular': [], 'provider': []}
    real_rectangular = room_prediction.analyze_native_rectangular_geometry
    real_provider = room_prediction.analyze_provider_frequency_response

    def record_rectangular(revision, receiver_entity_id, **kwargs):
        calls['rectangular'].append(receiver_entity_id)
        return real_rectangular(revision, receiver_entity_id, **kwargs)

    def record_provider(provider, revision, receiver_entity_id, **kwargs):
        calls['provider'].append(provider.provider_id)
        return real_provider(provider, revision, receiver_entity_id, **kwargs)

    monkeypatch.setattr(
        room_prediction,
        'analyze_native_rectangular_geometry',
        record_rectangular,
    )
    monkeypatch.setattr(
        room_prediction,
        'analyze_provider_frequency_response',
        record_provider,
    )
    return calls


def test_each_ready_model_key_dispatches_the_selected_implementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every READY model_key must run its own analyzer and stamp the
    authority of the solver that actually produced the result."""
    bundle, hybrid_provider, prediction = _all_lane_controller(tmp_path, monkeypatch)
    wave_provider = bundle['base_provider']
    calls = _recording_analyzers(monkeypatch)

    options = prediction.prediction_options(RECEIVER_ENTITY, max_mode_hz=80.0)
    ready_keys = [
        option.model_key for option in options if option.state == 'READY'
    ]
    assert RECTANGULAR_MODEL_KEY in ready_keys
    wave_key = provider_model_key(wave_provider.provider_id)
    hybrid_key = hybrid_model_key(hybrid_provider.provider_id)
    assert wave_key in ready_keys
    assert hybrid_key in ready_keys

    expected = {
        RECTANGULAR_MODEL_KEY: RECTANGULAR_GEOMETRY_MODEL_ID,
        wave_key: PROVIDER_RESPONSE_MODEL_ID,
        hybrid_key: HYBRID_RESPONSE_MODEL_ID,
    }
    for model_key in ready_keys:
        calls['rectangular'].clear()
        calls['provider'].clear()
        spec = prediction.prepare_run(
            RECEIVER_ENTITY,
            model_key=model_key,
            max_mode_hz=80.0,
        )
        results = prediction._analyze(spec, Event())
        assert results is not None

        # The token, the request identity and the produced result all name
        # one model — the solver whose analyzer actually ran.
        result = results[0]
        assert result.model_id == expected[model_key]
        assert spec.identity.model_id == result.model_id
        assert spec.token.model_id == result.model_id
        assert spec.token.model_version == result.model_version
        assert spec.token.input_hash == result.input_hash

        if model_key == RECTANGULAR_MODEL_KEY:
            assert calls['rectangular'] == [RECEIVER_ENTITY]
            assert calls['provider'] == []
        else:
            provider = (
                wave_provider
                if model_key == wave_key
                else hybrid_provider
            )
            assert calls['provider'] == [provider.provider_id]
            assert calls['rectangular'] == []
            # Result authority names the producing provider authority,
            # not the requested model_key string.
            response = result.provider_response
            assert response is not None
            assert response.provider_id == provider.provider_id
            assert response.provider_semantic_sha256 == (
                provider.semantic_sha256
            )
            assert response.provider_adapter_id == provider.adapter_id
            assert provider.provider_id == model_key.split(':', 1)[1]

        accepted = prediction.accept_results(spec, results)
        assert accepted == results
        stored = prediction.prediction_repository.list_run(result.run_id)
        assert stored == results

    prediction.dispose()


def test_cross_lane_model_key_confusion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wave model_key pointing at a hybrid provider (and vice versa),
    an unknown provider id, and the bare placeholder keys must all be
    refused — never resolved to a different lane's implementation."""
    bundle, hybrid_provider, prediction = _all_lane_controller(
        tmp_path, monkeypatch
    )
    wave_provider = bundle['base_provider']

    confused_keys = (
        # Wave key carrying a hybrid provider id.
        provider_model_key(hybrid_provider.provider_id),
        # Hybrid key carrying a wave provider id.
        hybrid_model_key(wave_provider.provider_id),
        # Unknown provider id.
        f'{WAVE_MODEL_KEY_PREFIX}r170a-provider:{"0" * 64}',
        # Bare placeholders have no registered provider behind them.
        'low-band-wave',
        HYBRID_MODEL_KEY,
    )
    for model_key in confused_keys:
        with pytest.raises(ValueError):
            prediction.prepare_run(
                RECEIVER_ENTITY,
                model_key=model_key,
                max_mode_hz=80.0,
            )

    prediction.dispose()


def test_wrong_lane_result_cannot_apply_to_another_lanes_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result computed by solver B must not be appliable to a job
    token minted for solver A — the guard compares model identity."""
    bundle, hybrid_provider, prediction = _all_lane_controller(
        tmp_path, monkeypatch
    )
    wave_provider = bundle['base_provider']

    # One outstanding prediction job per operation: the wave token minted
    # last supersedes the hybrid token.
    hybrid_spec = prediction.prepare_run(
        RECEIVER_ENTITY,
        model_key=hybrid_model_key(hybrid_provider.provider_id),
        max_mode_hz=80.0,
    )
    hybrid_results = prediction._analyze(hybrid_spec, Event())
    assert hybrid_results is not None
    wave_spec = prediction.prepare_run(
        RECEIVER_ENTITY,
        model_key=provider_model_key(wave_provider.provider_id),
        max_mode_hz=80.0,
    )

    # The hybrid lane's result carries a different model_id/input_hash and
    # must not land on the wave lane's outstanding token — fail closed.
    assert not prediction._result_matches_token(
        hybrid_results[0], wave_spec.token
    )
    with pytest.raises(ValueError, match='immutable input identity'):
        prediction.accept_results(wave_spec, hybrid_results)

    # The wave lane's own result applies to its own token; the superseded
    # hybrid token can no longer apply (latest-per-operation semantics).
    wave_results = prediction._analyze(wave_spec, Event())
    assert wave_results is not None
    assert prediction.accept_results(wave_spec, wave_results) == wave_results
    assert prediction.accept_results(hybrid_spec, hybrid_results) is None

    prediction.dispose()


def test_identical_selection_produces_identical_request_and_result_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Determinism: the same selection on the same revision yields the
    same canonical request identity and the same result_sha256."""
    bundle, hybrid_provider, prediction = _all_lane_controller(
        tmp_path, monkeypatch
    )
    wave_provider = bundle['base_provider']

    for model_key in (
        RECTANGULAR_MODEL_KEY,
        provider_model_key(wave_provider.provider_id),
        hybrid_model_key(hybrid_provider.provider_id),
    ):
        first = prediction.prepare_run(
            RECEIVER_ENTITY, model_key=model_key, max_mode_hz=80.0
        )
        second = prediction.prepare_run(
            RECEIVER_ENTITY, model_key=model_key, max_mode_hz=80.0
        )
        assert first.identity.model_id == second.identity.model_id
        assert first.identity.model_version == second.identity.model_version
        assert first.identity.parameters_json == second.identity.parameters_json
        assert (
            first.identity.input_snapshot_json
            == second.identity.input_snapshot_json
        )
        assert first.identity.input_hash == second.identity.input_hash

        run_a = prediction._analyze(first, Event())
        run_b = prediction._analyze(second, Event())
        assert run_a is not None and run_b is not None
        assert tuple(r.result_sha256 for r in run_a) == tuple(
            r.result_sha256 for r in run_b
        )

    prediction.dispose()


def test_ga_ready_dispatch_refused_by_pffdtd_candidate_executor(
    tmp_path: Path,
) -> None:
    """Execution lanes: a READY geometric-acoustics dispatch fed to the
    bounded PFFDTD candidate executor fails closed at the adapter check —
    the executor never substitutes a dispatch from a different lane."""
    fixture = _ga_fixture(tmp_path / 'ga')
    scene_repository = fixture['scene_repository']

    result_repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=fixture['dispatch_repository'],
        request_resolver=fixture['snapshot_repository'],
        external_authority_resolver=fixture['external_resolver'],
        artifact_manifest_resolver=lambda ref: None,
    )
    executor = PffdtdCandidateWaveExecutor(
        snapshot_repository=fixture['snapshot_repository'],
        dispatch_repository=fixture['dispatch_repository'],
        r120_repository=R120GeometryCompilerRepository(scene_repository),
        r110_repository=CadR110SourceRepository(scene_repository),
        wave_excitation_repository=CadWaveExcitationRepository(
            scene_repository
        ),
        result_repository=result_repository,
        authority_store=ExactJsonAuthorityStore(tmp_path / 'authorities'),
        output_schema_ref=ExactExternalAuthorityRef(
            authority_id='test-output-schema',
            authority_version='1',
            semantic_hash_sha256='0' * 64,
        ),
        upstream_root=tmp_path,
        work_root=tmp_path,
    )

    with pytest.raises(
        CandidateWaveExecutionError,
        match='does not target the bounded PFFDTD candidate adapter',
    ):
        executor.compile_input(
            dispatch_binding_id=fixture['dispatch'].binding_id,
            configuration=fixture['configuration'],
        )


def test_provider_lane_below_domain_blocks_at_resolve_not_at_analysis(
    tmp_path: Path,
) -> None:
    """Capability matrix: a requested band entirely below the provider
    domain is declared BLOCKED at option resolution — not resolved READY
    and failed later inside the analyzer."""
    from test_cad_prediction_provider import _fixture as _r170a_fixture

    fixture = _r170a_fixture(tmp_path / 'wave')
    provider = fixture.build_provider()
    provider_repository = fixture.repository()
    provider_repository.save_provider(provider)
    room = RoomWorkspaceController(
        fixture.scene_repository, 'r170a-fixture'
    )
    prediction = RoomPredictionController(
        fixture.scene_repository,
        room,
        provider_repository=provider_repository,
    )

    # Fixture domain is 40–80 Hz; a 30 Hz request selects an empty band.
    options = prediction.prediction_options(
        'receiver-1-entity', max_mode_hz=30.0
    )
    wave = next(
        option
        for option in options
        if option.model_key == provider_model_key(provider.provider_id)
    )
    assert wave.state == 'BLOCKED'
    assert wave.runnable is False
    assert wave.reasons

    with pytest.raises(ValueError):
        prediction.prepare_run(
            'receiver-1-entity',
            model_key=wave.model_key,
            max_mode_hz=30.0,
        )

    prediction.dispose()
