from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_acoustic_snapshot import AcousticPredictionRequest
from htdt.cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from htdt.cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    CadAcousticSolverResultRepository,
    build_acoustic_solver_result_envelope,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_prediction_models import (
    canonical_prediction_json,
    prediction_input_hash,
)
from htdt.cad_repository import SceneRepository
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


NOW = '2026-09-20T00:00:00+00:00'


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _ref(name: str, char: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=char * 64,
    )


def _request(
    *,
    observables: tuple[str, ...] = ('complex_pressure', 'spatial_field'),
) -> AcousticPredictionRequest:
    fidelity = _ref('fixture-fidelity-policy', '1')
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=200.0)
    core = {
        'schema_version': 1,
        'acoustic_scene_snapshot_id': 'acoustic-scene-snapshot:' + '2' * 64,
        'acoustic_scene_snapshot_sha256': '2' * 64,
        'model_solver_role_id': 'fixture-wave-role',
        'requested_frequency_domain': domain.model_dump(mode='json'),
        'requested_observables': list(observables),
        'numerical_fidelity_policy_ref': fidelity.model_dump(mode='json'),
    }
    canonical = canonical_prediction_json(core)
    semantic = _digest(core)
    return AcousticPredictionRequest(
        request_id=f'acoustic-prediction-request:{semantic}',
        request_semantic_sha256=semantic,
        acoustic_scene_snapshot_id=core['acoustic_scene_snapshot_id'],
        acoustic_scene_snapshot_sha256=core[
            'acoustic_scene_snapshot_sha256'
        ],
        model_solver_role_id='fixture-wave-role',
        requested_frequency_domain=domain,
        requested_observables=observables,
        numerical_fidelity_policy_ref=fidelity,
        deterministic_input_hash=prediction_input_hash(canonical),
    )


def _dispatch(
    request: AcousticPredictionRequest,
    *,
    state: str = 'READY',
) -> AcousticSolverDispatchBinding:
    implementation = _ref('fixture-solver-build', '3')
    configuration = _ref('fixture-solver-config', '4')
    descriptor_id = 'acoustic-solver-adapter:' + '5' * 64
    descriptor_hash = '5' * 64
    solver_input = {
        'prediction_request_id': request.request_id,
        'prediction_request_semantic_sha256': (
            request.request_semantic_sha256
        ),
        'prediction_deterministic_input_hash': (
            request.deterministic_input_hash
        ),
        'adapter_descriptor_id': descriptor_id,
        'adapter_descriptor_semantic_sha256': descriptor_hash,
        'solver_implementation_ref': implementation.model_dump(mode='json'),
        'solver_configuration_ref': configuration.model_dump(mode='json'),
    }
    solver_input_hash = _digest(solver_input)
    reasons = () if state == 'READY' else ('fixture_dispatch_blocked',)
    core = {
        'schema_version': 1,
        'authority_version': '1',
        'acoustic_scene_snapshot_id': request.acoustic_scene_snapshot_id,
        'acoustic_scene_snapshot_sha256': (
            request.acoustic_scene_snapshot_sha256
        ),
        'prediction_request_id': request.request_id,
        'prediction_request_semantic_sha256': (
            request.request_semantic_sha256
        ),
        'prediction_deterministic_input_hash': (
            request.deterministic_input_hash
        ),
        'adapter_descriptor_id': descriptor_id,
        'adapter_descriptor_semantic_sha256': descriptor_hash,
        'solver_implementation_ref': implementation.model_dump(mode='json'),
        'solver_configuration_ref': configuration.model_dump(mode='json'),
        'state': state,
        'reasons': list(reasons),
        'deterministic_solver_input_hash': solver_input_hash,
    }
    semantic = _digest(core)
    return AcousticSolverDispatchBinding(
        binding_id=f'acoustic-solver-dispatch:{semantic}',
        semantic_sha256=semantic,
        acoustic_scene_snapshot_id=request.acoustic_scene_snapshot_id,
        acoustic_scene_snapshot_sha256=request.acoustic_scene_snapshot_sha256,
        prediction_request_id=request.request_id,
        prediction_request_semantic_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        adapter_descriptor_id=descriptor_id,
        adapter_descriptor_semantic_sha256=descriptor_hash,
        solver_implementation_ref=implementation,
        solver_configuration_ref=configuration,
        state=state,
        reasons=reasons,
        deterministic_solver_input_hash=solver_input_hash,
    )


def _artifact(
    observable: str,
    *,
    char: str,
    minimum_hz: float = 100.0,
    maximum_hz: float = 200.0,
) -> AcousticSolverObservableArtifact:
    return AcousticSolverObservableArtifact(
        observable=observable,
        artifact_authority=_ref(f'{observable}-artifact', char),
        encoding_schema_ref=_ref(f'{observable}-schema', chr(ord(char) + 1)),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=minimum_hz,
            maximum_hz=maximum_hz,
        ),
    )


class _DispatchResolver:
    def __init__(self, path: Path, dispatch: AcousticSolverDispatchBinding):
        self.path = Path(path)
        self.dispatch = dispatch

    def get_dispatch(self, binding_id: str):
        return self.dispatch if binding_id == self.dispatch.binding_id else None


class _RequestResolver:
    def __init__(self, path: Path, request: AcousticPredictionRequest):
        self.path = Path(path)
        self.request = request

    def get_prediction_request(self, request_id: str):
        return self.request if request_id == self.request.request_id else None


def _registry(*refs: ExactExternalAuthorityRef):
    values = {
        (
            ref.authority_id,
            ref.authority_version,
            ref.semantic_hash_sha256,
        ): ref
        for ref in refs
    }

    def resolve(ref: ExactExternalAuthorityRef):
        return values.get(
            (
                ref.authority_id,
                ref.authority_version,
                ref.semantic_hash_sha256,
            )
        )

    return values, resolve


def test_result_envelope_binds_ready_dispatch_and_exact_requested_observables() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')

    result = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-1',
        execution_provenance_ref=_ref('fixture-execution-provenance', 'a'),
        artifacts=(field, pressure),
        completed_at_utc=NOW,
    )

    assert result.dispatch_binding_id == dispatch.binding_id
    assert result.deterministic_solver_input_hash == (
        dispatch.deterministic_solver_input_hash
    )
    assert [item.observable for item in result.artifacts] == [
        'complex_pressure',
        'spatial_field',
    ]
    assert len(result.semantic_sha256) == 64


def test_result_envelope_rejects_blocked_dispatch() -> None:
    request = _request(observables=('complex_pressure',))
    dispatch = _dispatch(request, state='BLOCKED')

    with pytest.raises(ValueError, match='only bind a READY'):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-blocked',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(_artifact('complex_pressure', char='6'),),
            completed_at_utc=NOW,
        )


def test_result_envelope_requires_exact_observable_set_and_frequency_coverage() -> None:
    request = _request()
    dispatch = _dispatch(request)

    with pytest.raises(ValueError, match='observable set must exactly match'):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-missing-output',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(_artifact('complex_pressure', char='6'),),
            completed_at_utc=NOW,
        )

    with pytest.raises(ValueError, match='does not cover requested frequency domain'):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-short-band',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(
                _artifact(
                    'complex_pressure',
                    char='6',
                    minimum_hz=120.0,
                    maximum_hz=180.0,
                ),
                _artifact('spatial_field', char='8'),
            ),
            completed_at_utc=NOW,
        )


def test_result_repository_reopens_exact_artifacts_and_fails_when_artifact_stales(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')
    execution = _ref('fixture-execution-provenance', 'a')
    result = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-persisted',
        execution_provenance_ref=execution,
        artifacts=(pressure, field),
        completed_at_utc=NOW,
    )

    all_refs = [
        dispatch.solver_implementation_ref,
        dispatch.solver_configuration_ref,
        execution,
        pressure.artifact_authority,
        pressure.encoding_schema_ref,
        field.artifact_authority,
        field.encoding_schema_ref,
    ]
    values, resolver = _registry(*all_refs)
    dispatch_resolver = _DispatchResolver(scene_repository.path, dispatch)
    request_resolver = _RequestResolver(scene_repository.path, request)
    repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=dispatch_resolver,
        request_resolver=request_resolver,
        external_authority_resolver=resolver,
    )
    repository.save(result)

    reopened = CadAcousticSolverResultRepository(
        SceneRepository(scene_repository.path),
        dispatch_resolver=_DispatchResolver(
            scene_repository.path,
            dispatch,
        ),
        request_resolver=_RequestResolver(
            scene_repository.path,
            request,
        ),
        external_authority_resolver=resolver,
    )
    assert reopened.get(result.result_id) == result

    stale = pressure.artifact_authority
    values.pop(
        (
            stale.authority_id,
            stale.authority_version,
            stale.semantic_hash_sha256,
        )
    )
    with pytest.raises(
        ValueError,
        match='complex_pressure artifact exact external authority does not exist',
    ):
        reopened.get(result.result_id)
