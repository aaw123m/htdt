from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest

from htdt.cad_acoustic_snapshot import (
    AcousticReceiverBinding,
    AcousticSceneReadiness,
    AcousticSceneSnapshot,
    AcousticSceneSourceBinding,
    ObservableReadiness,
    SnapshotEnvironmentAuthorityRef,
    build_acoustic_prediction_request,
)
from htdt.cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
)
from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_geometric_acoustics_adapter import (
    DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
    BoundaryMaterialContribution,
    DeterministicAcousticPath,
    DeterministicPathArtifact,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_hybrid_acoustic_result import (
    CadHybridAcousticResultRepository,
    HybridAcousticResult,
    HybridCrossoverPolicy,
    HybridDoubleCountExclusionPolicy,
    HybridFrequencyPartition,
    HybridObservableValidity,
    LateEnergyDecay,
    build_hybrid_acoustic_result,
    build_hybrid_composition_spec,
    build_hybrid_stitching_policy,
    compose_hybrid_acoustic_result,
)
from htdt.cad_hybrid_late_energy import R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
from htdt.cad_late_field_energy import LATE_FIELD_ARTIFACT_SCHEMA_REF
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Position3
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _ref(label: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test:{label}',
        authority_version=version,
        semantic_hash_sha256=_hash(label),
    )


def _payload_ref(
    prefix: str,
    version: str,
    payload: object,
) -> ExactExternalAuthorityRef:
    digest = _digest(payload)
    return ExactExternalAuthorityRef(
        authority_id=f'{prefix}:{digest}',
        authority_version=version,
        semantic_hash_sha256=digest,
    )


def _snapshot(
    tag: str = 'a',
    *,
    receiver_ids: tuple[str, ...] = ('receiver-1',),
    observables: tuple[str, ...] = ('complex_pressure', 'deterministic_paths'),
) -> AcousticSceneSnapshot:
    source = AcousticSceneSourceBinding(
        source_entity_id='source-1',
        source_entity_sha256=_hash(f'{tag}:source-entity'),
        equipment_definition_id='equipment-1',
        equipment_definition_version='1',
        equipment_definition_sha256=_hash(f'{tag}:equipment'),
        r110_compiled_source_sha256=_hash(f'{tag}:r110'),
        source_reference_point=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        source_reference_semantics='acoustic_reference_point',
        source_axis=Direction3(x=1.0, y=0.0, z=0.0),
        source_axis_semantics='explicit_acoustic_axis',
        directivity_capability='complex',
        geometric_directivity_state='AVAILABLE',
        complex_directivity_state='AVAILABLE',
        wave_excitation_state='AVAILABLE',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=500.0,
        ),
    )
    receivers = tuple(
        AcousticReceiverBinding(
            receiver_id=receiver_id,
            entity_id=f'{receiver_id}-entity',
            world_position=Position3(
                x_m=1.0 + index,
                y_m=1.5,
                z_m=1.0,
            ),
            acoustic_reference_semantics='scene_acoustic_reference_position',
            requested_output_capabilities=observables,
        )
        for index, receiver_id in enumerate(receiver_ids)
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref(f'{tag}:environment'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref(f'{tag}:sound-speed'),
    )
    readiness = AcousticSceneReadiness(
        geometry_ready=True,
        geometric_directivity_ready=True,
        wave_source_ready=True,
        wave_boundary_ready=True,
        geometric_boundary_ready=True,
        environment_ready=True,
        receiver_ready=True,
        requested_observable_ready=True,
        observable_readiness=tuple(
            ObservableReadiness(
                observable=observable,
                state='READY',
                reasons=(),
            )
            for observable in observables
        ),
    )
    probe = AcousticSceneSnapshot.model_construct(
        schema_version=3,
        authority_version='3',
        compiler_id='htdt.acoustic_scene_snapshot',
        compiler_version='3',
        snapshot_id=f'acoustic-scene-snapshot:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        document_id=f'document-{tag}',
        scene_revision_id=f'scene-revision-{tag}',
        scene_content_hash=_hash(f'{tag}:scene-content'),
        system_variant_id=None,
        system_variant_sha256=None,
        semantic_geometry_id=(
            f'semantic-acoustic-geometry:{_hash(f"{tag}:semantic-geometry")}'
        ),
        semantic_geometry_sha256=_hash(f'{tag}:semantic-geometry'),
        r120_compiled_geometry_id=(
            f'r120-compiled-geometry:{_hash(f"{tag}:r120-geometry")}'
        ),
        r120_compiled_geometry_sha256=_hash(f'{tag}:r120-geometry'),
        compiled_topology_sha256=_hash(f'{tag}:topology'),
        geometric_tolerance_m=1.0e-6,
        approximation_error_bound_m=0.0,
        approximation_error_status='EXACT',
        maximum_dropped_feature_extent_m=0.0,
        acoustic_region_authority_ref=None,
        portal_authority_ref=None,
        boundary_termination_authority_ref=None,
        surface_boundary_configuration=(),
        material_boundary_configuration_sha256=_digest([]),
        treatment_boundary_bindings=(),
        wave_source_excitation_bindings=(),
        sources=(source,),
        receivers=receivers,
        environment=environment,
        valid_frequency_domain=None,
        valid_frequency_domain_authority_ref=None,
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=500.0,
        ),
        requested_observables=observables,
        readiness=readiness,
        unresolved_conditions=(),
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['snapshot_id'] = f'acoustic-scene-snapshot:{digest}'
    payload['semantic_sha256'] = digest
    return AcousticSceneSnapshot.model_validate(payload)


def _request(
    snapshot: AcousticSceneSnapshot,
    *,
    observable: str,
    domain: FrequencyDomain,
    tag: str,
):
    return build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=f'test-{tag}',
        requested_frequency_domain=domain,
        requested_observables=(observable,),
        numerical_fidelity_policy_ref=_ref(f'{tag}:fidelity'),
    )


def _result(
    request,
    *,
    observable: str,
    artifact_ref: ExactExternalAuthorityRef,
    schema_ref: ExactExternalAuthorityRef,
    domain: FrequencyDomain,
    tag: str,
    execution_id: str | None = None,
    dispatch_id: str | None = None,
    dispatch_sha256: str | None = None,
    adapter_id: str | None = None,
    adapter_sha256: str | None = None,
    solver_implementation_ref: ExactExternalAuthorityRef | None = None,
    solver_configuration_ref: ExactExternalAuthorityRef | None = None,
) -> AcousticSolverResultEnvelope:
    manifest = AcousticSolverObservableArtifact(
        observable=observable,
        artifact_authority=artifact_ref,
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=domain,
    )
    dispatch_hash = dispatch_sha256 or _hash(f'{tag}:dispatch')
    adapter_hash = adapter_sha256 or _hash(f'{tag}:adapter')
    probe = AcousticSolverResultEnvelope.model_construct(
        schema_version=1,
        authority_version='acoustic-solver-result-1',
        result_id=f'acoustic-solver-result:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        execution_id=execution_id or f'execution-{tag}',
        result_state='COMPLETED',
        dispatch_binding_id=(
            dispatch_id or f'acoustic-solver-dispatch:{dispatch_hash}'
        ),
        dispatch_binding_sha256=dispatch_hash,
        prediction_request_id=request.request_id,
        prediction_request_semantic_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        acoustic_scene_snapshot_id=request.acoustic_scene_snapshot_id,
        acoustic_scene_snapshot_sha256=request.acoustic_scene_snapshot_sha256,
        adapter_descriptor_id=(
            adapter_id or f'acoustic-solver-adapter:{adapter_hash}'
        ),
        adapter_descriptor_semantic_sha256=adapter_hash,
        deterministic_solver_input_hash=_hash(f'{tag}:solver-input'),
        solver_implementation_ref=(
            solver_implementation_ref or _ref(f'{tag}:implementation')
        ),
        solver_configuration_ref=(
            solver_configuration_ref or _ref(f'{tag}:configuration')
        ),
        execution_provenance_ref=_ref(f'{tag}:provenance'),
        artifacts=(manifest,),
        completed_at_utc='2026-09-20T02:00:00+00:00',
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['result_id'] = f'acoustic-solver-result:{digest}'
    payload['semantic_sha256'] = digest
    return AcousticSolverResultEnvelope.model_validate(payload)


def _wave_pair(
    snapshot: AcousticSceneSnapshot,
    *,
    domain: FrequencyDomain,
    tag: str = 'wave',
):
    request = _request(
        snapshot,
        observable='complex_pressure',
        domain=domain,
        tag=tag,
    )
    solver_execution_id = (
        f'r130a-candidate-wave:{_hash(tag)[:20]}:fixture'
    )
    payload = {
        'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        'quantity_type': 'complex_pressure',
        'complex_representation': {
            'form': 'cartesian_real_imag',
            'phasor_convention': 'exp(-i*omega*t)',
            'analysis_fourier_kernel': 'exp(+i*omega*t)',
        },
        'receiver_identity_order': [
            {
                'receiver_id': item.receiver_id,
                'entity_id': item.entity_id,
                'position_m': [
                    float(item.world_position.x_m),
                    float(item.world_position.y_m),
                    float(item.world_position.z_m),
                ],
            }
            for item in snapshot.receivers
        ],
        'frequency_axis_hz': [
            float(domain.minimum_hz),
            float(domain.maximum_hz),
        ],
        'time_sampling': {
            'time_step_s': 1.0e-4,
            'sample_count': 1024,
            'finite_record_interval': '[0,T)',
            'requested_duration_s': 0.1024,
        },
        'units': 'Pa',
        'reference': (
            'absolute complex acoustic pressure from finite-record P/Q '
            'transfer multiplied by exact AcousticWaveExcitationAuthority Q(f)'
        ),
        'valid_domain': domain.model_dump(mode='json'),
        'solver_execution_id': solver_execution_id,
        'candidate_execution_input_id': (
            f'candidate-wave-input:{_hash(f"{tag}:candidate-input")}'
        ),
        'candidate_execution_input_sha256': _hash(f'{tag}:candidate-input'),
        'source_authority': {
            'r110_compiled_source_sha256': (
                snapshot.sources[0].r110_compiled_source_sha256
            ),
            'wave_excitation_binding_sha256': _hash(
                f'{tag}:wave-excitation-binding'
            ),
            'wave_excitation_sha256': _hash(f'{tag}:wave-excitation'),
        },
        'solver_raw_asset': {
            'name': 'sim_outs.h5',
            'sha256': _hash(f'{tag}:raw-pffdtd-output'),
        },
        'pressure_real_pa': [
            [1.0, 0.5] for _ in snapshot.receivers
        ],
        'pressure_imag_pa': [
            [0.0, -0.25] for _ in snapshot.receivers
        ],
    }
    artifact_ref = _payload_ref(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        payload,
    )
    schema_payload = {
        'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        'quantity_type': 'complex_pressure',
    }
    result = _result(
        request,
        observable='complex_pressure',
        artifact_ref=artifact_ref,
        schema_ref=_payload_ref(
            'complex-pressure-artifact-schema',
            '1',
            schema_payload,
        ),
        domain=domain,
        tag=tag,
        execution_id=solver_execution_id,
    )
    return request, result, payload


def _direct_path(
    *,
    solver_implementation_ref: ExactExternalAuthorityRef,
    tag: str,
    frequency_hz: float,
) -> DeterministicAcousticPath:
    directivity = SourceDirectivityContribution(
        dataset_id='dataset-1',
        dataset_version='1',
        dataset_semantic_sha256=_hash(f'{tag}:dataset'),
        evaluation_semantic_sha256=_hash(f'{tag}:directivity-eval'),
        frequency_hz=frequency_hz,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
        magnitude_db=0.0,
        magnitude_linear=1.0,
        energy_factor=1.0,
    )
    band = DeterministicPathBandQuantity(
        center_hz=frequency_hz,
        spreading_factor_per_m2=0.25,
        source_directivity=directivity,
        relative_energy_transport_per_m2=0.25,
    )
    probe = DeterministicAcousticPath.model_construct(
        path_id=f'deterministic-acoustic-path:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        source_entity_id='source-1',
        receiver_id='receiver-1',
        receiver_entity_id='receiver-1-entity',
        path_type='direct',
        ordered_interaction_surface_ids=(),
        ordered_interaction_points=(),
        geometric_path_length_m=2.0,
        propagation_delay_s=2.0 / 343.0,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=1.0, y=0.0, z=0.0),
        direction_semantics=(
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        bands=(band,),
        adapter_id='htdt.r150.deterministic-path',
        adapter_version='1',
        solver_implementation_ref=solver_implementation_ref,
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['path_id'] = f'deterministic-acoustic-path:{digest}'
    payload['semantic_sha256'] = digest
    return DeterministicAcousticPath.model_validate(payload)


def _ga_pair(
    snapshot: AcousticSceneSnapshot,
    *,
    domain: FrequencyDomain,
    tag: str = 'ga',
):
    request = _request(
        snapshot,
        observable='deterministic_paths',
        domain=domain,
        tag=tag,
    )
    dispatch_hash = _hash(f'{tag}:dispatch')
    adapter_hash = _hash(f'{tag}:adapter')
    dispatch_id = f'acoustic-solver-dispatch:{dispatch_hash}'
    adapter_id = f'acoustic-solver-adapter:{adapter_hash}'
    implementation_ref = _ref(f'{tag}:implementation')
    configuration_ref = _ref(f'{tag}:configuration')
    execution_id = f'r150-ga-execution:{_hash(f"{tag}:execution")}'
    path = _direct_path(
        solver_implementation_ref=implementation_ref,
        tag=tag,
        frequency_hz=float(domain.minimum_hz),
    )
    probe = DeterministicPathArtifact.model_construct(
        schema_version=1,
        authority_version='r150-deterministic-ga-1',
        artifact_id=f'deterministic-path-artifact:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        execution_id=execution_id,
        execution_input_id=f'r150-ga-execution-input:{_hash(f"{tag}:input")}',
        execution_input_sha256=_hash(f'{tag}:input'),
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.semantic_sha256,
        prediction_request_id=request.request_id,
        prediction_request_sha256=request.request_semantic_sha256,
        dispatch_binding_id=dispatch_id,
        dispatch_binding_sha256=dispatch_hash,
        adapter_descriptor_id=adapter_id,
        adapter_descriptor_sha256=adapter_hash,
        solver_implementation_ref=implementation_ref,
        solver_configuration_ref=configuration_ref,
        r120_compiled_geometry_id=snapshot.r120_compiled_geometry_id,
        r120_compiled_geometry_sha256=snapshot.r120_compiled_geometry_sha256,
        topology_identity_sha256=snapshot.compiled_topology_sha256,
        engine_id='test.image-source',
        engine_version='1',
        candidate_source_commit=None,
        numeric_comparison_tolerance_m=1.0e-6,
        identity_decimal_places=9,
        frequency_domain=domain,
        path_scope='direct_and_first_order_specular',
        coherent_phase_authority='UNAVAILABLE_NOT_SYNTHESIZED',
        paths=(path,),
        rejected_candidates=(),
    )
    digest = _digest(probe.semantic_payload())
    artifact_payload = probe.model_dump(mode='python')
    artifact_payload['artifact_id'] = f'deterministic-path-artifact:{digest}'
    artifact_payload['semantic_sha256'] = digest
    artifact = DeterministicPathArtifact.model_validate(artifact_payload)
    result = _result(
        request,
        observable='deterministic_paths',
        artifact_ref=artifact.as_external_ref(),
        schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
        domain=domain,
        tag=tag,
        execution_id=execution_id,
        dispatch_id=dispatch_id,
        dispatch_sha256=dispatch_hash,
        adapter_id=adapter_id,
        adapter_sha256=adapter_hash,
        solver_implementation_ref=implementation_ref,
        solver_configuration_ref=configuration_ref,
    )
    return request, result, artifact


@dataclass
class SnapshotRequestStore:
    path: Path
    snapshots: dict[str, AcousticSceneSnapshot]
    requests: dict[str, Any]

    def get_snapshot(self, snapshot_id: str):
        return self.snapshots.get(snapshot_id)

    def get_prediction_request(self, request_id: str):
        return self.requests.get(request_id)


@dataclass
class ResultStore:
    path: Path
    results: dict[str, AcousticSolverResultEnvelope]

    def get(self, result_id: str):
        return self.results.get(result_id)


@dataclass
class PathStore:
    path: Path
    artifacts: dict[str, DeterministicPathArtifact]

    def get(self, artifact_id: str):
        return self.artifacts.get(artifact_id)


@dataclass
class PayloadStore:
    payloads: dict[str, object]

    def __call__(self, ref: ExactExternalAuthorityRef):
        payload = self.payloads.get(ref.authority_id)
        if payload is None:
            raise ValueError('missing test artifact')
        if _digest(payload) != ref.semantic_hash_sha256:
            raise ValueError('modified test artifact')
        return payload


@dataclass
class RepositoryFixture:
    scene_repository: SceneRepository
    snapshot_store: SnapshotRequestStore
    result_store: ResultStore
    path_store: PathStore
    payload_store: PayloadStore

    def repository(self) -> CadHybridAcousticResultRepository:
        return CadHybridAcousticResultRepository(
            self.scene_repository,
            snapshot_request_resolver=self.snapshot_store,
            solver_result_resolver=self.result_store,
            deterministic_path_resolver=self.path_store,
            external_payload_resolver=self.payload_store,
        )


def _repository_fixture(
    tmp_path: Path,
    *,
    snapshot: AcousticSceneSnapshot,
    requests: tuple[Any, ...],
    results: tuple[AcousticSolverResultEnvelope, ...],
    paths: tuple[DeterministicPathArtifact, ...] = (),
    payloads: tuple[tuple[ExactExternalAuthorityRef, object], ...] = (),
) -> RepositoryFixture:
    db_path = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(db_path)
    return RepositoryFixture(
        scene_repository=scene_repository,
        snapshot_store=SnapshotRequestStore(
            path=db_path,
            snapshots={snapshot.snapshot_id: snapshot},
            requests={item.request_id: item for item in requests},
        ),
        result_store=ResultStore(
            path=db_path,
            results={item.result_id: item for item in results},
        ),
        path_store=PathStore(
            path=db_path,
            artifacts={item.artifact_id: item for item in paths},
        ),
        payload_store=PayloadStore(
            payloads={
                ref.authority_id: payload for ref, payload in payloads
            }
        ),
    )


def test_wave_only_coherent_component() -> None:
    snapshot = _snapshot()
    domain = FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0)
    request, result, payload = _wave_pair(snapshot, domain=domain)
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        external_payload_resolver=lambda ref: payload,
    )

    assert hybrid.has_frequency_response()
    assert hybrid.has_phase()
    assert not hybrid.has_direct_or_early_paths()
    assert hybrid.coherent_transfer is not None
    assert hybrid.coherent_transfer.coherent is True
    assert hybrid.valid_frequency_domain_for('coherent_transfer') == domain


def test_ga_only_deterministic_path_component() -> None:
    snapshot = _snapshot()
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    request, result, artifact = _ga_pair(snapshot, domain=domain)
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        deterministic_path_artifacts=(artifact,),
        external_payload_resolver=lambda ref: {},
    )

    assert not hybrid.has_frequency_response()
    assert not hybrid.has_phase()
    assert hybrid.has_direct_or_early_paths()
    assert hybrid.deterministic_path_set is not None
    assert hybrid.deterministic_path_set.artifact_authority == artifact.as_external_ref()
    assert hybrid.valid_frequency_domain_for('deterministic_paths') == domain


def test_wave_and_ga_same_snapshot_hybrid() -> None:
    snapshot = _snapshot()
    wave_request, wave_result, wave_payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0),
    )
    ga_request, ga_result, path = _ga_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: wave_payload,
    )

    assert hybrid.has_frequency_response()
    assert hybrid.has_phase()
    assert hybrid.has_direct_or_early_paths()
    assert hybrid.frequency_relationship() == 'OVERLAPPING_COMPONENT_VALIDITY'
    assert not hybrid.numerical_blend_permitted()


def test_different_scene_revision_rejected() -> None:
    first = _snapshot('a')
    second = _snapshot('b')
    wave_request, wave_result, wave_payload = _wave_pair(
        first,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
    )
    ga_request, ga_result, path = _ga_pair(
        second,
        domain=FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    with pytest.raises(ValueError, match='same exact snapshot'):
        build_hybrid_acoustic_result(
            snapshot=first,
            prediction_requests=(wave_request, ga_request),
            solver_results=(wave_result, ga_result),
            stitching_policy=policy,
            deterministic_path_artifacts=(path,),
            external_payload_resolver=lambda ref: wave_payload,
        )


def test_different_receiver_set_rejected() -> None:
    snapshot = _snapshot('a')
    different_receivers = _snapshot(
        'receiver-variant',
        receiver_ids=('receiver-1', 'receiver-2'),
    )
    domain = FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0)
    request, result, _ = _wave_pair(snapshot, domain=domain)
    _, _, wrong_payload = _wave_pair(
        different_receivers,
        domain=domain,
        tag='other-wave',
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    with pytest.raises(ValueError, match='receiver identity/order'):
        build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=(request,),
            solver_results=(result,),
            stitching_policy=policy,
            external_payload_resolver=lambda ref: wrong_payload,
        )


def test_stale_or_mismatched_solver_result_rejected(tmp_path: Path) -> None:
    snapshot = _snapshot()
    domain = FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0)
    request, result, payload = _wave_pair(snapshot, domain=domain)
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        external_payload_resolver=lambda ref: payload,
    )
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(request,),
        results=(result,),
        payloads=((result.artifacts[0].artifact_authority, payload),),
    )
    repository = fixture.repository()
    repository.save(hybrid, policy=policy)

    other_request, other_result, other_payload = _wave_pair(
        snapshot,
        domain=domain,
        tag='wave-other',
    )
    fixture.snapshot_store.requests[other_request.request_id] = other_request
    fixture.payload_store.payloads[
        other_result.artifacts[0].artifact_authority.authority_id
    ] = other_payload
    fixture.result_store.results[result.result_id] = other_result

    with pytest.raises(ValueError, match='identity/provenance mismatch'):
        repository.get(hybrid.hybrid_result_id)


def test_phase_free_ga_never_promotes_to_coherent() -> None:
    snapshot = _snapshot()
    request, result, artifact = _ga_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        deterministic_path_artifacts=(artifact,),
        external_payload_resolver=lambda ref: {},
    )

    assert hybrid.coherent_transfer is None
    assert not hybrid.has_phase()
    assert hybrid.deterministic_path_set is not None
    assert (
        hybrid.deterministic_path_set.validity.phase_capability
        == 'UNAVAILABLE_NOT_SYNTHESIZED'
    )


def test_explicit_non_overlapping_frequency_validity() -> None:
    snapshot = _snapshot()
    wave_domain = FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0)
    ga_domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=wave_domain,
    )
    ga_request, ga_result, path = _ga_pair(snapshot, domain=ga_domain)
    policy = build_hybrid_stitching_policy(
        mode='frequency_partition_no_blend',
        frequency_partitions=(
            HybridFrequencyPartition(
                component='coherent_transfer',
                frequency_domain=wave_domain,
            ),
            HybridFrequencyPartition(
                component='deterministic_paths',
                frequency_domain=ga_domain,
            ),
        ),
    )

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )

    assert hybrid.frequency_relationship() == 'DISJOINT_COMPONENT_VALIDITY'
    assert hybrid.valid_frequency_domain_for('coherent_transfer') == wave_domain
    assert hybrid.valid_frequency_domain_for('deterministic_paths') == ga_domain
    assert not hybrid.numerical_blend_permitted()


def test_explicit_overlap_preserves_components_without_silent_blend() -> None:
    snapshot = _snapshot()
    wave_domain = FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0)
    ga_domain = FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0)
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=wave_domain,
    )
    ga_request, ga_result, path = _ga_pair(snapshot, domain=ga_domain)
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )

    assert hybrid.frequency_relationship() == 'OVERLAPPING_COMPONENT_VALIDITY'
    assert not hybrid.numerical_blend_permitted()
    assert policy.numerical_blend_permitted is False
    assert policy.unsupported_phase_generation_permitted is False
    assert hybrid.coherent_transfer is not None
    assert hybrid.deterministic_path_set is not None


def test_unavailable_late_energy_decay_state() -> None:
    snapshot = _snapshot()
    request, result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        external_payload_resolver=lambda ref: payload,
        late_energy_decay_state='UNAVAILABLE',
        late_energy_decay_reason='no late-tail solver evidence exists',
    )

    assert not hybrid.has_late_energy_decay()
    assert hybrid.late_energy_decay_state() == 'UNAVAILABLE'
    assert hybrid.valid_frequency_domain_for('late_energy_decay') is None


def test_deterministic_identity_is_input_order_independent() -> None:
    snapshot = _snapshot()
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0),
    )
    ga_request, ga_result, path = _ga_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')

    first = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )
    second = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(ga_request, wave_request),
        solver_results=(ga_result, wave_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )

    assert first == second
    assert first.hybrid_result_id == second.hybrid_result_id


def test_persistence_reopen_equality(tmp_path: Path) -> None:
    snapshot = _snapshot()
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0),
    )
    ga_request, ga_result, path = _ga_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(wave_request, ga_request),
        results=(wave_result, ga_result),
        paths=(path,),
        payloads=((wave_result.artifacts[0].artifact_authority, payload),),
    )
    repository = fixture.repository()

    assert repository.save(hybrid, policy=policy) == hybrid

    reopened = fixture.repository()
    assert reopened.get_policy(policy.policy_id) == policy
    assert reopened.get(hybrid.hybrid_result_id) == hybrid


def test_missing_external_artifact_fails_closed_after_save(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    request, result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        external_payload_resolver=lambda ref: payload,
    )
    artifact_ref = result.artifacts[0].artifact_authority
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(request,),
        results=(result,),
        payloads=((artifact_ref, payload),),
    )
    repository = fixture.repository()
    repository.save(hybrid, policy=policy)

    del fixture.payload_store.payloads[artifact_ref.authority_id]

    with pytest.raises(ValueError, match='external artifact is unavailable'):
        fixture.repository().get(hybrid.hybrid_result_id)



def test_r160_v1_cannot_claim_validated_without_exact_validation_authority() -> None:
    with pytest.raises(ValueError):
        HybridObservableValidity(
            frequency_domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
            observable_type='coherent_transfer',
            source_observable='complex_pressure',
            phase_capability='COMPLEX_EXPLICIT_REFERENCE',
            solver_result_id=f'acoustic-solver-result:{_hash("validation-result-id")}',
            solver_result_sha256=_hash('validation-result'),
            adapter_descriptor_id=f'acoustic-solver-adapter:{_hash("validation-adapter-id")}',
            adapter_descriptor_sha256=_hash('validation-adapter'),
            solver_implementation_ref=_ref('validation-solver'),
            solver_configuration_ref=_ref('validation-config'),
            evidence_state='VALIDATED',
        )


def _bounded_composition_fixture(
    *,
    wave_domain: FrequencyDomain | None = None,
    ga_domain: FrequencyDomain | None = None,
):
    snapshot = _snapshot()
    wave_domain = wave_domain or FrequencyDomain(
        minimum_hz=20.0,
        maximum_hz=120.0,
    )
    ga_domain = ga_domain or FrequencyDomain(
        minimum_hz=80.0,
        maximum_hz=300.0,
    )
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=wave_domain,
    )
    ga_request, ga_result, path = _ga_pair(
        snapshot,
        domain=ga_domain,
    )
    policy = build_hybrid_stitching_policy(
        mode=(
            'overlap_preserve_components'
            if wave_domain.maximum_hz > ga_domain.minimum_hz
            else 'disjoint_by_observable'
        )
    )
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )
    return (
        snapshot,
        wave_request,
        wave_result,
        payload,
        ga_request,
        ga_result,
        path,
        policy,
        hybrid,
    )


def _second_order_path(
    direct: DeterministicAcousticPath,
    *,
    solver_implementation_ref: ExactExternalAuthorityRef,
) -> DeterministicAcousticPath:
    source_directivity = direct.bands[0].source_directivity
    materials = (
        BoundaryMaterialContribution(
            source_surface_id='surface-a',
            material_authority=_ref('second-order-material-a'),
            frequency_hz=source_directivity.frequency_hz,
            absorption=0.1,
            scattering=0.0,
            specular_energy_factor=0.9,
        ),
        BoundaryMaterialContribution(
            source_surface_id='surface-b',
            material_authority=_ref('second-order-material-b'),
            frequency_hz=source_directivity.frequency_hz,
            absorption=0.2,
            scattering=0.0,
            specular_energy_factor=0.8,
        ),
    )
    band = DeterministicPathBandQuantity(
        center_hz=source_directivity.frequency_hz,
        spreading_factor_per_m2=0.1,
        source_directivity=source_directivity,
        boundary_materials=materials,
        relative_energy_transport_per_m2=0.072,
    )
    probe = DeterministicAcousticPath.model_construct(
        path_id=f'deterministic-acoustic-path:{_hash("placeholder-second")}',
        semantic_sha256=_hash('placeholder-second'),
        source_entity_id=direct.source_entity_id,
        receiver_id=direct.receiver_id,
        receiver_entity_id=direct.receiver_entity_id,
        path_type='specular_reflection',
        ordered_interaction_surface_ids=('surface-a', 'surface-b'),
        ordered_interaction_points=(
            Position3(x_m=0.5, y_m=0.0, z_m=0.0),
            Position3(x_m=1.5, y_m=0.0, z_m=0.0),
        ),
        geometric_path_length_m=3.0,
        propagation_delay_s=3.0 / 343.0,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=1.0, y=0.0, z=0.0),
        direction_semantics=direct.direction_semantics,
        bands=(band,),
        adapter_id=direct.adapter_id,
        adapter_version=direct.adapter_version,
        solver_implementation_ref=solver_implementation_ref,
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['path_id'] = f'deterministic-acoustic-path:{digest}'
    payload['semantic_sha256'] = digest
    return DeterministicAcousticPath.model_validate(payload)


def _ga_pair_second_order(
    snapshot: AcousticSceneSnapshot,
    *,
    domain: FrequencyDomain,
):
    request, original_result, original_artifact = _ga_pair(
        snapshot,
        domain=domain,
        tag='ga-second',
    )
    second = _second_order_path(
        original_artifact.paths[0],
        solver_implementation_ref=original_artifact.solver_implementation_ref,
    )
    probe = original_artifact.model_copy(
        update={
            'artifact_id': (
                'deterministic-path-artifact:'
                f'{_hash("placeholder-second-artifact")}'
            ),
            'semantic_sha256': _hash('placeholder-second-artifact'),
            'path_scope': 'direct_through_second_order_specular',
            'paths': (original_artifact.paths[0], second),
        }
    )
    digest = _digest(probe.semantic_payload())
    artifact_payload = probe.model_dump(mode='python')
    artifact_payload['artifact_id'] = f'deterministic-path-artifact:{digest}'
    artifact_payload['semantic_sha256'] = digest
    artifact = DeterministicPathArtifact.model_validate(artifact_payload)
    result = _result(
        request,
        observable='deterministic_paths',
        artifact_ref=artifact.as_external_ref(),
        schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
        domain=domain,
        tag='ga-second-result',
        execution_id=artifact.execution_id,
        dispatch_id=artifact.dispatch_binding_id,
        dispatch_sha256=artifact.dispatch_binding_sha256,
        adapter_id=artifact.adapter_descriptor_id,
        adapter_sha256=artifact.adapter_descriptor_sha256,
        solver_implementation_ref=artifact.solver_implementation_ref,
        solver_configuration_ref=artifact.solver_configuration_ref,
    )
    return request, result, artifact


def test_bounded_composition_binds_compatible_actual_artifacts() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=300.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )

    assert composed.composition_spec == spec
    assert composed.composition_decision is not None
    assert composed.composition_decision.status == (
        'ELIGIBLE_BOUNDED_NO_NUMERIC_BLEND'
    )
    assert composed.composition_decision.supported_observables == (
        'deterministic_path_identity',
    )
    assert composed.composition_decision.approximation_error.extrapolation_performed is False
    assert composed.hybrid_result_id != hybrid.hybrid_result_id


def test_bounded_composition_rejects_incompatible_scene_artifact() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    other = _snapshot('composition-other-scene')
    _, _, other_path = _ga_pair(
        other,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
        tag='other-scene-ga',
    )

    with pytest.raises(ValueError, match='GA artifact identity|incompatible scene'):
        build_hybrid_composition_spec(
            hybrid=hybrid,
            deterministic_path_artifact=other_path,
            observable='deterministic_path_identity',
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=20.0,
                maximum_hz=300.0,
            ),
            crossover_policy=HybridCrossoverPolicy(
                mode='preserve_overlap_no_blend'
            ),
            double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
                mode='preserve_components_no_numeric_sum'
            ),
        )


def test_bounded_composition_rejects_receiver_set_mismatch() -> None:
    snapshot = _snapshot(receiver_ids=('receiver-1', 'receiver-2'))
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0),
        tag='wave-receiver-mismatch',
    )
    ga_request, ga_result, path = _ga_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
        tag='ga-receiver-mismatch',
    )
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )

    with pytest.raises(ValueError, match='receiver identity mismatch'):
        build_hybrid_composition_spec(
            hybrid=hybrid,
            deterministic_path_artifact=path,
            observable='deterministic_path_identity',
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=80.0,
                maximum_hz=120.0,
            ),
            crossover_policy=HybridCrossoverPolicy(
                mode='preserve_overlap_no_blend'
            ),
            double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
                mode='preserve_components_no_numeric_sum'
            ),
        )


def test_bounded_composition_preserves_no_overlap_gap() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture(
        wave_domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=80.0),
        ga_domain=FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0),
    )
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=20.0,
            maximum_hz=300.0,
        ),
        crossover_policy=HybridCrossoverPolicy(mode='preserve_gap_no_fill'),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    assert spec.overlap_domain is None
    assert composed.composition_decision is not None
    assert composed.composition_decision.gap_domains == (
        FrequencyDomain(minimum_hz=80.0, maximum_hz=100.0),
    )
    assert composed.composition_decision.valid_domains == (
        FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0),
    )


def test_bounded_composition_missing_coherent_phase_rejected() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='coherent_phase',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    with pytest.raises(ValueError, match='missing coherent phase authority'):
        compose_hybrid_acoustic_result(
            hybrid=hybrid,
            composition_spec=spec,
            deterministic_path_artifact=path,
        )


def test_bounded_composition_observable_capability_is_specific() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=100.0,
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    assert composed.composition_decision is not None
    unsupported = {
        item.observable: item.reason_code
        for item in composed.composition_decision.unsupported_observables
    }
    assert unsupported['magnitude_energy'] == 'INCOMPATIBLE_QUANTITY_REFERENCE'
    assert unsupported['coherent_phase'] == 'MISSING_COHERENT_PHASE_AUTHORITY'
    assert unsupported['arrival_timing'] == 'MISSING_SHARED_TIME_ORIGIN'
    assert unsupported['late_decay'] == 'MISSING_LATE_DECAY_AUTHORITY'


def test_bounded_composition_double_count_ambiguity_rejected() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='magnitude_energy',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='require_disjoint_component_ownership'
        ),
    )
    with pytest.raises(ValueError, match='double-count ambiguity'):
        compose_hybrid_acoustic_result(
            hybrid=hybrid,
            composition_spec=spec,
            deterministic_path_artifact=path,
        )


def test_bounded_composition_identity_is_deterministic_and_policy_bound() -> None:
    *_, path, _, hybrid = _bounded_composition_fixture()
    common = dict(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    first_spec = build_hybrid_composition_spec(
        **common,
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=100.0,
        ),
    )
    second_spec = build_hybrid_composition_spec(
        **common,
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=100.0,
        ),
    )
    changed_spec = build_hybrid_composition_spec(
        **common,
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=110.0,
        ),
    )
    first = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=first_spec,
        deterministic_path_artifact=path,
    )
    second = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=second_spec,
        deterministic_path_artifact=path,
    )
    changed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=changed_spec,
        deterministic_path_artifact=path,
    )
    assert first_spec == second_spec
    assert first.hybrid_result_id == second.hybrid_result_id
    assert changed_spec.composition_spec_id != first_spec.composition_spec_id
    assert changed.hybrid_result_id != first.hybrid_result_id


def test_bounded_composition_save_reopen_and_policy_stale(tmp_path: Path) -> None:
    (
        snapshot,
        wave_request,
        wave_result,
        payload,
        ga_request,
        ga_result,
        path,
        policy,
        hybrid,
    ) = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=100.0,
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(wave_request, ga_request),
        results=(wave_result, ga_result),
        paths=(path,),
        payloads=((wave_result.artifacts[0].artifact_authority, payload),),
    )
    repository = fixture.repository()
    repository.save(composed, policy=policy)

    reopened = fixture.repository().get(
        composed.hybrid_result_id,
        expected_composition_spec=spec,
    )
    assert reopened == composed

    changed_spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='explicit_partition_no_blend',
            crossover_hz=110.0,
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    with pytest.raises(ValueError, match='stale for expected spec'):
        fixture.repository().get(
            composed.hybrid_result_id,
            expected_composition_spec=changed_spec,
        )


def test_bounded_composition_wave_artifact_stale_after_reopen(tmp_path: Path) -> None:
    (
        snapshot,
        wave_request,
        wave_result,
        payload,
        ga_request,
        ga_result,
        path,
        policy,
        hybrid,
    ) = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(wave_request, ga_request),
        results=(wave_result, ga_result),
        paths=(path,),
        payloads=((wave_result.artifacts[0].artifact_authority, payload),),
    )
    fixture.repository().save(composed, policy=policy)
    fixture.payload_store.payloads[
        wave_result.artifacts[0].artifact_authority.authority_id
    ] = {**payload, 'reference': 'changed reference'}

    with pytest.raises(ValueError):
        fixture.repository().get(composed.hybrid_result_id)


def test_bounded_composition_ga_and_snapshot_stale_after_reopen(
    tmp_path: Path,
) -> None:
    (
        snapshot,
        wave_request,
        wave_result,
        payload,
        ga_request,
        ga_result,
        path,
        policy,
        hybrid,
    ) = _bounded_composition_fixture()
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    fixture = _repository_fixture(
        tmp_path,
        snapshot=snapshot,
        requests=(wave_request, ga_request),
        results=(wave_result, ga_result),
        paths=(path,),
        payloads=((wave_result.artifacts[0].artifact_authority, payload),),
    )
    fixture.repository().save(composed, policy=policy)

    _, _, stale_path = _ga_pair(
        snapshot,
        domain=path.frequency_domain,
        tag='ga-stale-replacement',
    )
    fixture.path_store.artifacts[path.artifact_id] = stale_path
    with pytest.raises(ValueError, match='missing/stale R150'):
        fixture.repository().get(composed.hybrid_result_id)

    fixture.path_store.artifacts[path.artifact_id] = path
    fixture.snapshot_store.snapshots[snapshot.snapshot_id] = _snapshot(
        'receiver-stale',
        receiver_ids=('receiver-1', 'receiver-2'),
    )
    with pytest.raises(
        ValueError,
        match='snapshot/geometry/source/receiver/environment compatibility',
    ):
        fixture.repository().get(composed.hybrid_result_id)


def test_bounded_composition_accepts_second_order_path_identity() -> None:
    snapshot = _snapshot()
    wave_request, wave_result, payload = _wave_pair(
        snapshot,
        domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=120.0),
        tag='wave-second-order',
    )
    ga_request, ga_result, path = _ga_pair_second_order(
        snapshot,
        domain=FrequencyDomain(minimum_hz=80.0, maximum_hz=300.0),
    )
    policy = build_hybrid_stitching_policy(mode='overlap_preserve_components')
    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(wave_request, ga_request),
        solver_results=(wave_result, ga_result),
        stitching_policy=policy,
        deterministic_path_artifacts=(path,),
        external_payload_resolver=lambda ref: payload,
    )
    spec = build_hybrid_composition_spec(
        hybrid=hybrid,
        deterministic_path_artifact=path,
        observable='deterministic_path_identity',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=80.0,
            maximum_hz=120.0,
        ),
        crossover_policy=HybridCrossoverPolicy(
            mode='preserve_overlap_no_blend'
        ),
        double_count_exclusion_policy=HybridDoubleCountExclusionPolicy(
            mode='preserve_components_no_numeric_sum'
        ),
    )
    composed = compose_hybrid_acoustic_result(
        hybrid=hybrid,
        composition_spec=spec,
        deterministic_path_artifact=path,
    )
    assert composed.composition_decision is not None
    assert composed.composition_decision.path_reflection_orders_present == (0, 2)


def test_pr254_legacy_typed_artifact_identity_remains_compatible() -> None:
    *_, hybrid = _bounded_composition_fixture()
    legacy_payload = hybrid.model_dump(
        mode='python',
        exclude={'composition_spec', 'composition_decision'},
    )
    restored = HybridAcousticResult.model_validate(legacy_payload)
    assert restored == hybrid
    assert restored.hybrid_result_id == hybrid.hybrid_result_id
    assert 'composition_spec' not in restored.semantic_payload()
    assert 'composition_decision' not in restored.semantic_payload()


def _late_pair(
    snapshot: AcousticSceneSnapshot,
    *,
    domain: FrequencyDomain,
    schema_ref: ExactExternalAuthorityRef,
    tag: str = 'late',
):
    request = _request(
        snapshot,
        observable='late_energy_decay',
        domain=domain,
        tag=tag,
    )
    artifact_ref = _payload_ref(
        'late-energy-decay-artifact',
        'r160-late-energy-decay-2',
        {'tag': tag},
    )
    result = _result(
        request,
        observable='late_energy_decay',
        artifact_ref=artifact_ref,
        schema_ref=schema_ref,
        domain=domain,
        tag=tag,
    )
    return request, result


def test_late_component_binds_canonical_decay_encoding() -> None:
    snapshot = _snapshot(observables=('late_energy_decay',))
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    request, result = _late_pair(
        snapshot,
        domain=domain,
        schema_ref=R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF,
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    hybrid = build_hybrid_acoustic_result(
        snapshot=snapshot,
        prediction_requests=(request,),
        solver_results=(result,),
        stitching_policy=policy,
        external_payload_resolver=lambda ref: {},
    )

    assert hybrid.has_late_energy_decay()
    assert hybrid.late_energy_decay.state == 'AVAILABLE'
    assert hybrid.late_energy_decay.encoding_schema_ref == (
        R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
    )
    assert hybrid.valid_frequency_domain_for('late_energy_decay') == domain


def test_late_component_rejects_late_field_upper_bound_encoding() -> None:
    # The R150 late-field artifact shares the 'late_energy_decay' observable
    # name but encodes per-band energy upper bounds, not decay samples — it
    # stays a resolvable solver-result row and must not fill the typed slot.
    snapshot = _snapshot(observables=('late_energy_decay',))
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    request, result = _late_pair(
        snapshot,
        domain=domain,
        schema_ref=LATE_FIELD_ARTIFACT_SCHEMA_REF,
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    with pytest.raises(
        ValueError,
        match='canonical late-energy decay encoding',
    ):
        build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=(request,),
            solver_results=(result,),
            stitching_policy=policy,
            external_payload_resolver=lambda ref: {},
        )


def test_late_component_rejects_foreign_encoding() -> None:
    snapshot = _snapshot(observables=('late_energy_decay',))
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    request, result = _late_pair(
        snapshot,
        domain=domain,
        schema_ref=_ref('late-energy-encoding-foreign'),
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    with pytest.raises(
        ValueError,
        match='canonical late-energy decay encoding',
    ):
        build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=(request,),
            solver_results=(result,),
            stitching_policy=policy,
            external_payload_resolver=lambda ref: {},
        )


def test_late_component_rejects_canonical_id_with_tampered_hash() -> None:
    # Encoding validation compares the whole ref, not just the authority id.
    snapshot = _snapshot(observables=('late_energy_decay',))
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    tampered = ExactExternalAuthorityRef(
        authority_id=R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF.authority_id,
        authority_version=(
            R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF.authority_version
        ),
        semantic_hash_sha256=_hash('tampered-schema-hash'),
    )
    request, result = _late_pair(
        snapshot,
        domain=domain,
        schema_ref=tampered,
    )
    policy = build_hybrid_stitching_policy(mode='disjoint_by_observable')

    with pytest.raises(
        ValueError,
        match='canonical late-energy decay encoding',
    ):
        build_hybrid_acoustic_result(
            snapshot=snapshot,
            prediction_requests=(request,),
            solver_results=(result,),
            stitching_policy=policy,
            external_payload_resolver=lambda ref: {},
        )


def test_available_late_decay_model_rejects_noncanonical_encoding() -> None:
    # The component model itself is fail-closed: a hand-built AVAILABLE
    # LateEnergyDecay cannot bind a non-canonical encoding schema.
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0)
    validity = HybridObservableValidity(
        frequency_domain=domain,
        observable_type='late_energy_decay',
        source_observable='late_energy_decay',
        phase_capability='NOT_APPLICABLE',
        solver_result_id=f'acoustic-solver-result:{_hash("late-result")}',
        solver_result_sha256=_hash('late-result'),
        adapter_descriptor_id=f'acoustic-solver-adapter:{_hash("late-adapter")}',
        adapter_descriptor_sha256=_hash('late-adapter'),
        solver_implementation_ref=_ref('late-impl'),
        solver_configuration_ref=_ref('late-conf'),
        evidence_state='EXECUTED_UNVALIDATED',
    )
    with pytest.raises(
        ValueError,
        match='canonical',
    ):
        LateEnergyDecay(
            state='AVAILABLE',
            reason='foreign encoding must not promote',
            artifact_authority=_ref('late-artifact'),
            encoding_schema_ref=_ref('late-encoding-foreign'),
            validity=validity,
        )

    bound = LateEnergyDecay(
        state='AVAILABLE',
        reason='canonical encoding promotes',
        artifact_authority=_ref('late-artifact'),
        encoding_schema_ref=R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF,
        validity=validity,
    )
    assert bound.state == 'AVAILABLE'
