from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_acoustic_snapshot import AcousticPredictionRequest
from htdt.cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from htdt.cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverObservableArtifact,
    build_acoustic_solver_result_envelope,
)
from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    ExactJsonAuthorityStore,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_prediction_models import (
    canonical_prediction_json,
    prediction_input_hash,
)
from htdt.cad_repository import SceneRepository
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.acoustics.persistence.cad_acoustic_solver_result_repository import CadAcousticSolverResultRepository


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
    minimum_hz: float = 100.0,
    maximum_hz: float = 200.0,
) -> AcousticPredictionRequest:
    fidelity = _ref('fixture-fidelity-policy', '1')
    domain = FrequencyDomain(minimum_hz=minimum_hz, maximum_hz=maximum_hz)
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
        'numerical_fidelity_policy_ref': (
            request.numerical_fidelity_policy_ref.model_dump(mode='json')
        ),
    }
    solver_input_hash = _digest(solver_input)
    reasons = () if state == 'READY' else ('fixture_dispatch_blocked',)
    core = {
        'schema_version': 2,
        'authority_version': '2',
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
        'numerical_fidelity_policy_ref': (
            request.numerical_fidelity_policy_ref.model_dump(mode='json')
        ),
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
        numerical_fidelity_policy_ref=request.numerical_fidelity_policy_ref,
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


def _manifest(
    artifact: AcousticSolverObservableArtifact,
    *,
    observable: str | None = None,
    encoding_schema_ref: ExactExternalAuthorityRef | None = None,
    artifact_ref: ExactExternalAuthorityRef | None = None,
    minimum_hz: float | None = None,
    maximum_hz: float | None = None,
) -> AcousticSolverArtifactManifest:
    return AcousticSolverArtifactManifest(
        artifact_ref=(
            artifact_ref
            if artifact_ref is not None
            else artifact.artifact_authority
        ),
        observable=(
            observable if observable is not None else artifact.observable
        ),
        encoding_schema_ref=(
            encoding_schema_ref
            if encoding_schema_ref is not None
            else artifact.encoding_schema_ref
        ),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=(
                minimum_hz
                if minimum_hz is not None
                else artifact.valid_frequency_domain.minimum_hz
            ),
            maximum_hz=(
                maximum_hz
                if maximum_hz is not None
                else artifact.valid_frequency_domain.maximum_hz
            ),
        ),
    )


def _manifest_registry(*manifests: AcousticSolverArtifactManifest):
    values = {
        (
            manifest.artifact_ref.authority_id,
            manifest.artifact_ref.authority_version,
            manifest.artifact_ref.semantic_hash_sha256,
        ): manifest
        for manifest in manifests
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


def test_envelope_build_with_manifest_resolver_matches_unresolved_build() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(
        _manifest(pressure),
        _manifest(field),
    )

    resolved = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-resolved',
        execution_provenance_ref=_ref('fixture-execution-provenance', 'a'),
        artifacts=(field, pressure),
        completed_at_utc=NOW,
        artifact_manifest_resolver=manifest_resolver,
    )
    unresolved = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-resolved',
        execution_provenance_ref=_ref('fixture-execution-provenance', 'a'),
        artifacts=(field, pressure),
        completed_at_utc=NOW,
    )
    assert resolved == unresolved


def test_envelope_build_rejects_manifest_with_narrower_actual_band() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact(
        'complex_pressure',
        char='6',
        minimum_hz=20.0,
        maximum_hz=300.0,
    )
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(
        _manifest(pressure, minimum_hz=40.0, maximum_hz=200.0),
        _manifest(field),
    )

    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest valid frequency domain mismatch',
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-narrow-band',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_envelope_build_rejects_manifest_observable_rebind() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(
        _manifest(pressure, observable='spatial_field'),
        _manifest(field),
    )

    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest observable mismatch',
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-observable-rebind',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_envelope_build_rejects_manifest_encoding_schema_mismatch() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(
        _manifest(
            pressure,
            encoding_schema_ref=_ref('other-schema', 'c'),
        ),
        _manifest(field),
    )

    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest encoding schema mismatch',
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-schema-mismatch',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_envelope_build_rejects_manifest_for_different_artifact() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')

    def manifest_resolver(ref: ExactExternalAuthorityRef):
        if ref == pressure.artifact_authority:
            return _manifest(
                pressure,
                artifact_ref=_ref('other-artifact', 'e'),
            )
        if ref == field.artifact_authority:
            return _manifest(field)
        return None

    with pytest.raises(
        ValueError,
        match='artifact manifest resolves a different exact artifact authority',
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-confused-deputy',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_envelope_build_rejects_unresolvable_manifest() -> None:
    request = _request()
    dispatch = _dispatch(request)
    pressure = _artifact('complex_pressure', char='6')
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(_manifest(field))

    with pytest.raises(
        ValueError,
        match=(
            'complex_pressure artifact manifest exact external authority '
            'does not exist'
        ),
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-missing-manifest',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_envelope_build_checks_requested_domain_against_resolved_coverage() -> None:
    request = _request()
    dispatch = _dispatch(request)
    # Honest binding that exactly reproduces a manifest covering only
    # 120..180 Hz while the prediction request needs 100..200 Hz.
    pressure = _artifact(
        'complex_pressure',
        char='6',
        minimum_hz=120.0,
        maximum_hz=180.0,
    )
    field = _artifact('spatial_field', char='8')
    _, manifest_resolver = _manifest_registry(
        _manifest(pressure),
        _manifest(field),
    )

    with pytest.raises(
        ValueError,
        match='does not cover requested frequency domain',
    ):
        build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id='fixture-execution-short-coverage',
            execution_provenance_ref=_ref(
                'fixture-execution-provenance',
                'a',
            ),
            artifacts=(pressure, field),
            completed_at_utc=NOW,
            artifact_manifest_resolver=manifest_resolver,
        )


def test_repository_reresolves_manifests_on_save_and_read(tmp_path: Path) -> None:
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
    _, resolver = _registry(*all_refs)
    manifest_values, manifest_resolver = _manifest_registry(
        _manifest(pressure),
        _manifest(field),
    )
    repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=_DispatchResolver(scene_repository.path, dispatch),
        request_resolver=_RequestResolver(scene_repository.path, request),
        external_authority_resolver=resolver,
        artifact_manifest_resolver=manifest_resolver,
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
        artifact_manifest_resolver=manifest_resolver,
    )
    assert reopened.get(result.result_id) == result

    # Read-side repeats manifest resolution: once the artifact manifest stops
    # resolving, the persisted envelope no longer reopens.
    stale = pressure.artifact_authority
    manifest_values.pop(
        (
            stale.authority_id,
            stale.authority_version,
            stale.semantic_hash_sha256,
        )
    )
    with pytest.raises(
        ValueError,
        match=(
            'complex_pressure artifact manifest exact external authority '
            'does not exist'
        ),
    ):
        reopened.get(result.result_id)


def test_repository_rejects_envelope_claiming_wider_band_than_manifest(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    request = _request()
    dispatch = _dispatch(request)
    # The caller points at a real artifact whose manifest only covers
    # 100..200 Hz but declares 20..300 Hz in the submitted binding.
    pressure = _artifact(
        'complex_pressure',
        char='6',
        minimum_hz=20.0,
        maximum_hz=300.0,
    )
    field = _artifact('spatial_field', char='8')
    execution = _ref('fixture-execution-provenance', 'a')
    result = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-false-band',
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
    _, resolver = _registry(*all_refs)
    manifest_values, manifest_resolver = _manifest_registry(
        _manifest(pressure, minimum_hz=100.0, maximum_hz=200.0),
        _manifest(field),
    )
    repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=_DispatchResolver(scene_repository.path, dispatch),
        request_resolver=_RequestResolver(scene_repository.path, request),
        external_authority_resolver=resolver,
        artifact_manifest_resolver=manifest_resolver,
    )
    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest valid frequency domain mismatch',
    ):
        repository.save(result)

    # A manifest that later reports a different observable also fails reads.
    manifest_values[
        (
            pressure.artifact_authority.authority_id,
            pressure.artifact_authority.authority_version,
            pressure.artifact_authority.semantic_hash_sha256,
        )
    ] = _manifest(pressure, observable='spatial_field')
    with pytest.raises(
        ValueError,
        match='artifact manifest observable mismatch',
    ):
        repository.save(result)


def _store_fixture(tmp_path: Path):
    store = ExactJsonAuthorityStore(tmp_path / 'authorities')
    schema_ref = store.put_json(
        'fixture-complex-pressure-schema',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        {
            'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            'quantity_type': 'complex_pressure',
            'quantity_unit': 'Pa',
        },
    )
    return store, schema_ref


def _complex_pressure_payload(domain: FrequencyDomain) -> dict:
    return {
        'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        'quantity_type': 'complex_pressure',
        'receiver_identity_order': [
            {'receiver_id': 'receiver-a', 'entity_id': 'entity-a'}
        ],
        'frequency_axis_hz': [100.0, 150.0, 200.0],
        'units': 'Pa',
        'valid_domain': domain.model_dump(mode='json'),
        'solver_execution_id': 'fixture-execution-1',
        'candidate_execution_input_id': 'fixture-input-1',
        'candidate_execution_input_sha256': 'f' * 64,
    }


def test_exact_store_manifest_resolver_derives_typed_manifest(
    tmp_path: Path,
) -> None:
    store, schema_ref = _store_fixture(tmp_path)
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=200.0)
    artifact_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        _complex_pressure_payload(domain),
    )
    resolver = store.solver_artifact_manifest_resolver(
        encoding_schema_ref=schema_ref,
    )

    manifest = resolver(artifact_ref)
    assert manifest == AcousticSolverArtifactManifest(
        artifact_ref=artifact_ref,
        observable='complex_pressure',
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=domain,
        channel_identity={
            'receiver_identity_order': [
                {'receiver_id': 'receiver-a', 'entity_id': 'entity-a'}
            ],
            'frequency_axis_hz': [100.0, 150.0, 200.0],
        },
        solver_lineage={
            'solver_execution_id': 'fixture-execution-1',
            'candidate_execution_input_id': 'fixture-input-1',
            'candidate_execution_input_sha256': 'f' * 64,
        },
    )

    # Missing artifact, schema-confused payload, missing observable and an
    # invalid domain all fail closed.
    assert resolver(_ref('missing-artifact', '9')) is None

    other_schema_ref = ExactExternalAuthorityRef(
        authority_id=schema_ref.authority_id,
        authority_version='other-schema-version',
        semantic_hash_sha256=schema_ref.semantic_hash_sha256,
    )
    assert store.solver_artifact_manifest_resolver(
        encoding_schema_ref=other_schema_ref,
    )(artifact_ref) is None

    no_observable = dict(_complex_pressure_payload(domain))
    no_observable.pop('quantity_type')
    no_observable_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        no_observable,
    )
    assert resolver(no_observable_ref) is None

    bad_domain = dict(_complex_pressure_payload(domain))
    bad_domain['valid_domain'] = {'minimum_hz': 'not-a-frequency'}
    bad_domain_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        bad_domain,
    )
    assert resolver(bad_domain_ref) is None


def test_store_backed_result_repository_binds_exact_manifest(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    store, schema_ref = _store_fixture(tmp_path)
    request = _request(observables=('complex_pressure',))
    dispatch = _dispatch(request)
    execution = _ref('fixture-execution-provenance', 'a')
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=200.0)
    artifact_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        _complex_pressure_payload(domain),
    )
    honest = AcousticSolverObservableArtifact(
        observable='complex_pressure',
        artifact_authority=artifact_ref,
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=domain,
    )

    _, fixture_resolver = _registry(
        dispatch.solver_implementation_ref,
        dispatch.solver_configuration_ref,
        execution,
    )

    def external_resolver(ref: ExactExternalAuthorityRef):
        return store.resolve(ref) or fixture_resolver(ref)

    manifest_resolver = store.solver_artifact_manifest_resolver(
        encoding_schema_ref=schema_ref,
    )
    repository = CadAcousticSolverResultRepository(
        scene_repository,
        dispatch_resolver=_DispatchResolver(scene_repository.path, dispatch),
        request_resolver=_RequestResolver(scene_repository.path, request),
        external_authority_resolver=external_resolver,
        artifact_manifest_resolver=manifest_resolver,
    )

    result = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-store',
        execution_provenance_ref=execution,
        artifacts=(honest,),
        completed_at_utc=NOW,
        artifact_manifest_resolver=manifest_resolver,
    )
    repository.save(result)
    assert repository.get(result.result_id) == result

    # The issue scenario: the real artifact covers only 100..200 Hz but the
    # submitted binding claims 20..300 Hz.
    wider = AcousticSolverObservableArtifact(
        observable='complex_pressure',
        artifact_authority=artifact_ref,
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=300.0,
        ),
    )
    dishonest = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-store',
        execution_provenance_ref=execution,
        artifacts=(wider,),
        completed_at_utc=NOW,
    )
    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest valid frequency domain mismatch',
    ):
        repository.save(dishonest)

    # A payload for a different observable cannot be rebound as
    # complex_pressure.
    other_domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=200.0)
    other_payload = _complex_pressure_payload(other_domain)
    other_payload['quantity_type'] = 'spatial_field'
    other_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        other_payload,
    )
    rebound = AcousticSolverObservableArtifact(
        observable='complex_pressure',
        artifact_authority=other_ref,
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=other_domain,
    )
    rebound_result = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id='fixture-execution-store',
        execution_provenance_ref=execution,
        artifacts=(rebound,),
        completed_at_utc=NOW,
    )
    with pytest.raises(
        ValueError,
        match='complex_pressure artifact manifest observable mismatch',
    ):
        repository.save(rebound_result)
