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
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_geometric_acoustics_adapter import (
    DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
    DeterministicAcousticPath,
    DeterministicPathArtifact,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_hybrid_acoustic_result import (
    CadHybridAcousticResultRepository,
    HybridFrequencyPartition,
    build_hybrid_acoustic_result,
    build_hybrid_stitching_policy,
)
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
            requested_output_capabilities=(
                'complex_pressure',
                'deterministic_paths',
            ),
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
        observable_readiness=(
            ObservableReadiness(
                observable='complex_pressure',
                state='READY',
                reasons=(),
            ),
            ObservableReadiness(
                observable='deterministic_paths',
                state='READY',
                reasons=(),
            ),
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
        requested_observables=(
            'complex_pressure',
            'deterministic_paths',
        ),
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
    payload = {
        'schema_version': 'test-complex-pressure-1',
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
        'units': 'Pa',
        'reference': 'absolute complex acoustic pressure',
        'valid_domain': domain.model_dump(mode='json'),
        'source_authority': {
            'r110_compiled_source_sha256': (
                snapshot.sources[0].r110_compiled_source_sha256
            ),
        },
        'pressure_real_pa': [
            [1.0, 0.5] for _ in snapshot.receivers
        ],
        'pressure_imag_pa': [
            [0.0, -0.25] for _ in snapshot.receivers
        ],
    }
    artifact_ref = _payload_ref(
        'test-complex-pressure',
        'test-complex-pressure-1',
        payload,
    )
    result = _result(
        request,
        observable='complex_pressure',
        artifact_ref=artifact_ref,
        schema_ref=_ref(f'{tag}:complex-schema'),
        domain=domain,
        tag=tag,
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
