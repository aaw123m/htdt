from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

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
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_measurement_loop import (
    bind_measurement_plan_prediction,
    build_measurement_plan,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
)
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_objectives import build_pareto_set
from htdt.cad_prediction_provider import (
    CadPredictionProviderRepository,
    build_prediction_provider_binding,
    build_r130_low_band_prediction_provider,
    require_provider_current,
)
from htdt.cad_prediction_provider_integration import (
    CadPredictionProviderObjectiveRepository,
    bind_provider_to_adaptive_validation,
    bind_provider_to_validation,
    build_provider_measurement_validation,
    build_provider_objective_connection,
    provider_frequency_response,
    target_objective_evaluation_from_provider,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_document import WorkingDocument
from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import ResponseObjectiveSpec
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


@dataclass
class SnapshotRequestStore:
    path: Path
    snapshots: dict[str, AcousticSceneSnapshot]
    requests: dict[str, object]

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
class PayloadStore:
    payloads: dict[str, object]

    def __call__(self, ref: ExactExternalAuthorityRef):
        payload = self.payloads.get(ref.authority_id)
        if payload is None:
            raise ValueError('missing test external authority')
        if _digest(payload) != ref.semantic_hash_sha256:
            raise ValueError('modified test external authority')
        return payload


@dataclass
class ProviderFixture:
    scene_repository: SceneRepository
    revision: object
    snapshot: AcousticSceneSnapshot
    request: object
    result: AcousticSolverResultEnvelope
    payload_store: PayloadStore
    snapshot_store: SnapshotRequestStore
    result_store: ResultStore

    def build_provider(self):
        return build_r130_low_band_prediction_provider(
            revision=self.revision,
            snapshot=self.snapshot,
            request=self.request,
            result=self.result,
            external_payload_resolver=self.payload_store,
        )

    def repository(self) -> CadPredictionProviderRepository:
        return CadPredictionProviderRepository(
            self.scene_repository,
            snapshot_request_resolver=self.snapshot_store,
            solver_result_resolver=self.result_store,
            external_payload_resolver=self.payload_store,
        )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='r170a-fixture',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='source-1',
                kind='speaker',
                name='Source',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='receiver-1-entity',
                kind='measurement_point',
                name='Receiver',
                position=Position3(x_m=2.5, y_m=2.5, z_m=1.0),
            ),
        ),
    )


def _snapshot(revision) -> AcousticSceneSnapshot:
    source = AcousticSceneSourceBinding(
        source_entity_id='source-1',
        source_entity_sha256=_hash('source-entity'),
        equipment_definition_id='equipment-1',
        equipment_definition_version='1',
        equipment_definition_sha256=_hash('equipment'),
        r110_compiled_source_sha256=_hash('r110'),
        source_reference_point=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
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
    receiver = AcousticReceiverBinding(
        receiver_id='receiver-1',
        entity_id='receiver-1-entity',
        world_position=Position3(x_m=2.5, y_m=2.5, z_m=1.0),
        acoustic_reference_semantics='scene_acoustic_reference_position',
        requested_output_capabilities=('complex_pressure',),
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref('environment'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref('sound-speed'),
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
        ),
    )
    probe = AcousticSceneSnapshot.model_construct(
        schema_version=3,
        authority_version='3',
        compiler_id='htdt.acoustic_scene_snapshot',
        compiler_version='3',
        snapshot_id=f'acoustic-scene-snapshot:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None,
        system_variant_sha256=None,
        semantic_geometry_id=(
            f'semantic-acoustic-geometry:{_hash("semantic-geometry")}'
        ),
        semantic_geometry_sha256=_hash('semantic-geometry'),
        r120_compiled_geometry_id=(
            f'r120-compiled-geometry:{_hash("r120-geometry")}'
        ),
        r120_compiled_geometry_sha256=_hash('r120-geometry'),
        compiled_topology_sha256=_hash('topology'),
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
        receivers=(receiver,),
        environment=environment,
        valid_frequency_domain=None,
        valid_frequency_domain_authority_ref=None,
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        requested_observables=('complex_pressure',),
        readiness=readiness,
        unresolved_conditions=(),
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['snapshot_id'] = f'acoustic-scene-snapshot:{digest}'
    payload['semantic_sha256'] = digest
    return AcousticSceneSnapshot.model_validate(payload)


def _result(
    request,
    *,
    artifact_ref: ExactExternalAuthorityRef,
    schema_ref: ExactExternalAuthorityRef,
    provenance_ref: ExactExternalAuthorityRef,
    solver_implementation_ref: ExactExternalAuthorityRef,
    solver_configuration_ref: ExactExternalAuthorityRef,
) -> AcousticSolverResultEnvelope:
    manifest = AcousticSolverObservableArtifact(
        observable='complex_pressure',
        artifact_authority=artifact_ref,
        encoding_schema_ref=schema_ref,
        valid_frequency_domain=request.requested_frequency_domain,
    )
    dispatch_hash = _hash('dispatch')
    adapter_hash = _hash('adapter')
    probe = AcousticSolverResultEnvelope.model_construct(
        schema_version=1,
        authority_version='acoustic-solver-result-1',
        result_id=f'acoustic-solver-result:{_hash("placeholder-result")}',
        semantic_sha256=_hash('placeholder-result'),
        execution_id='r130a-candidate:test',
        result_state='COMPLETED',
        dispatch_binding_id=f'acoustic-solver-dispatch:{dispatch_hash}',
        dispatch_binding_sha256=dispatch_hash,
        prediction_request_id=request.request_id,
        prediction_request_semantic_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        acoustic_scene_snapshot_id=request.acoustic_scene_snapshot_id,
        acoustic_scene_snapshot_sha256=request.acoustic_scene_snapshot_sha256,
        adapter_descriptor_id=f'acoustic-solver-adapter:{adapter_hash}',
        adapter_descriptor_semantic_sha256=adapter_hash,
        deterministic_solver_input_hash=_hash('solver-input'),
        solver_implementation_ref=solver_implementation_ref,
        solver_configuration_ref=solver_configuration_ref,
        execution_provenance_ref=provenance_ref,
        artifacts=(manifest,),
        completed_at_utc='2026-09-20T08:30:00+00:00',
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['result_id'] = f'acoustic-solver-result:{digest}'
    payload['semantic_sha256'] = digest
    return AcousticSolverResultEnvelope.model_validate(payload)


def _fixture(tmp_path: Path) -> ProviderFixture:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    snapshot = _snapshot(revision)
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='test-r130-low-band',
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_ref('fidelity'),
    )
    implementation_ref = _ref('pffdtd-implementation')
    configuration_ref = _ref('pffdtd-configuration')
    execution_input_sha = _hash('execution-input')
    artifact_payload = {
        'schema_version': 'r130a-complex-pressure-artifact-1',
        'quantity_type': 'complex_pressure',
        'complex_representation': {
            'form': 'cartesian_real_imag',
            'phasor_convention': 'exp(-i*omega*t)',
            'analysis_fourier_kernel': 'exp(+i*omega*t)',
        },
        'receiver_identity_order': [
            {
                'receiver_id': 'receiver-1',
                'entity_id': 'receiver-1-entity',
                'position_m': [2.5, 2.5, 1.0],
            },
        ],
        'frequency_axis_hz': [40.0, 80.0],
        'valid_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'candidate_execution_input_sha256': execution_input_sha,
        'source_authority': {
            'r110_compiled_source_sha256': snapshot.sources[0].r110_compiled_source_sha256,
        },
        'pressure_real_pa': [[1.0, 0.5]],
        'pressure_imag_pa': [[0.0, -0.5]],
    }
    schema_payload = {
        'schema_version': 'r130a-complex-pressure-artifact-1',
        'quantity_type': 'complex_pressure',
    }
    provenance_payload = {
        'schema_version': 'htdt.r130a.candidate-execution-provenance-1',
        'candidate_only': True,
        'production_solver_selected': False,
        'owned_room_evidence': False,
        'execution_input_sha256': execution_input_sha,
        'solver_implementation_ref': implementation_ref.model_dump(mode='json'),
        'solver_configuration_ref': configuration_ref.model_dump(mode='json'),
    }
    artifact_ref = _payload_ref('artifact', '1', artifact_payload)
    schema_ref = _payload_ref('schema', '1', schema_payload)
    provenance_ref = _payload_ref('provenance', '1', provenance_payload)
    result = _result(
        request,
        artifact_ref=artifact_ref,
        schema_ref=schema_ref,
        provenance_ref=provenance_ref,
        solver_implementation_ref=implementation_ref,
        solver_configuration_ref=configuration_ref,
    )
    payload_store = PayloadStore(
        payloads={
            artifact_ref.authority_id: artifact_payload,
            schema_ref.authority_id: schema_payload,
            provenance_ref.authority_id: provenance_payload,
        }
    )
    path = Path(scene_repository.path)
    return ProviderFixture(
        scene_repository=scene_repository,
        revision=revision,
        snapshot=snapshot,
        request=request,
        result=result,
        payload_store=payload_store,
        snapshot_store=SnapshotRequestStore(
            path=path,
            snapshots={snapshot.snapshot_id: snapshot},
            requests={request.request_id: request},
        ),
        result_store=ResultStore(
            path=path,
            results={result.result_id: result},
        ),
    )


def test_provider_identity_exact_r130_binding_and_typed_n70_read(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = fixture.build_provider()
    second = fixture.build_provider()

    assert first == second
    assert first.provider_id == second.provider_id
    assert first.result_envelope_id == fixture.result.result_id
    assert first.result_envelope_sha256 == fixture.result.semantic_sha256
    assert first.current_authority.scene_revision_id == fixture.revision.revision_id
    assert first.evidence_state == 'candidate'
    assert first.evidence_scope == 'unvalidated'
    assert first.magnitude_capability == 'READY'
    assert first.phase_capability == 'READY'
    assert first.timing_capability == 'UNSUPPORTED'
    assert first.spatial_field_capability == 'UNSUPPORTED'

    response = provider_frequency_response(
        first,
        receiver_id='receiver-1',
        low_hz=40.0,
        high_hz=80.0,
    )
    assert response.frequency_hz == (40.0, 80.0)
    assert len(response.level_db) == 2


def test_provider_save_reopen_binding_and_external_tamper_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    provider = fixture.build_provider()
    repository = fixture.repository()

    assert repository.save_provider(provider) == provider
    assert repository.get_provider(provider.provider_id) == provider

    binding = build_prediction_provider_binding(
        provider,
        consumer_kind='N70_PRODUCT_PREDICTION',
        consumer_id='n70:test',
        required_observables=('frequency_response_magnitude',),
    )
    assert repository.save_binding(binding) == binding
    assert repository.get_binding(binding.binding_id) == binding

    fixture.payload_store.payloads[provider.result_artifact_ref.authority_id] = {
        'tampered': True,
    }
    with pytest.raises(ValueError, match='unavailable|hash mismatch'):
        repository.get_provider(provider.provider_id)


def test_stale_scene_revision_and_solver_input_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    provider = fixture.build_provider()

    stale_scene = provider.current_authority.model_copy(
        update={'scene_revision_id': 'scene-revision:changed'}
    )
    with pytest.raises(ValueError, match='stale.*SceneRevision'):
        require_provider_current(provider, stale_scene)

    stale_solver = provider.current_authority.model_copy(
        update={'deterministic_solver_input_hash': '0' * 64}
    )
    with pytest.raises(ValueError, match='stale.*solver input'):
        require_provider_current(provider, stale_solver)


def test_receiver_mismatch_is_rejected_before_provider_publication(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original_ref = fixture.result.artifacts[0].artifact_authority
    artifact_payload = dict(fixture.payload_store.payloads[original_ref.authority_id])
    artifact_payload['receiver_identity_order'] = [
        {
            'receiver_id': 'receiver-wrong',
            'entity_id': 'receiver-1-entity',
            'position_m': [2.5, 2.5, 1.0],
        },
    ]
    bad_ref = _payload_ref('artifact', '1', artifact_payload)
    fixture.payload_store.payloads[bad_ref.authority_id] = artifact_payload
    manifest = fixture.result.artifacts[0].model_copy(
        update={'artifact_authority': bad_ref}
    )
    probe = fixture.result.model_copy(
        update={
            'artifacts': (manifest,),
            'result_id': f'acoustic-solver-result:{_hash("placeholder-bad")}',
            'semantic_sha256': _hash('placeholder-bad'),
        }
    )
    digest = _digest(probe.semantic_payload())
    bad_result = AcousticSolverResultEnvelope.model_validate(
        probe.model_dump(mode='python')
        | {
            'result_id': f'acoustic-solver-result:{digest}',
            'semantic_sha256': digest,
        }
    )

    with pytest.raises(ValueError, match='receiver identity/set mismatch'):
        build_r130_low_band_prediction_provider(
            revision=fixture.revision,
            snapshot=fixture.snapshot,
            request=fixture.request,
            result=bad_result,
            external_payload_resolver=fixture.payload_store,
        )


def test_unsupported_observable_is_not_converted_to_bad_numeric_score(tmp_path: Path) -> None:
    provider = _fixture(tmp_path).build_provider()

    with pytest.raises(ValueError, match='unsupported.*rt60'):
        provider.require_observable('rt60')
    with pytest.raises(ValueError, match='exceeds exact valid band'):
        provider_frequency_response(
            provider,
            receiver_id='receiver-1',
            low_hz=20.0,
            high_hz=80.0,
        )


def test_candidate_result_cannot_be_promoted_to_production_owned_room(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    validation_payload = {
        'authority': 'test-validation',
        'evidence_scope': 'owned_room',
    }
    adoption_payload = {
        'authority': 'test-production-adoption',
        'decision': 'GO',
    }
    validation_ref = _payload_ref('validation', '1', validation_payload)
    adoption_ref = _payload_ref('adoption', '1', adoption_payload)
    fixture.payload_store.payloads[validation_ref.authority_id] = validation_payload
    fixture.payload_store.payloads[adoption_ref.authority_id] = adoption_payload

    with pytest.raises(ValueError, match='cannot be promoted to production'):
        build_r130_low_band_prediction_provider(
            revision=fixture.revision,
            snapshot=fixture.snapshot,
            request=fixture.request,
            result=fixture.result,
            external_payload_resolver=fixture.payload_store,
            evidence_state='production',
            evidence_scope='owned_room',
            validation_authority_ref=validation_ref,
            production_adoption_authority_ref=adoption_ref,
        )


def _search_fixture(fixture: ProviderFixture):
    constraint_set = CadConstraintSet(
        document_id=fixture.revision.document_id,
        constraints=(),
    )
    spec, _estimate = build_cad_search_spec(
        fixture.revision,
        constraint_set,
        (
            CadSearchAxis(
                entity_id='source-1',
                axis='x',
                min_m=1.0,
                max_m=2.0,
                step_m=1.0,
            ),
        ),
        candidate_limit=10,
        name='r170a integration fixture',
    )
    search_repository = CadSearchRepository(fixture.scene_repository)
    search_repository.save(spec)
    candidates = generate_cad_candidates(fixture.scene_repository, spec).candidates
    return constraint_set, spec, search_repository, candidates


def test_o30_o40_objective_connection_persists_exact_provider_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    provider_repository = fixture.repository()
    provider = provider_repository.save_provider(fixture.build_provider())
    _constraints, spec, search_repository, candidates = _search_fixture(fixture)
    candidate = candidates[0]
    target = FrequencyResponse(
        frequency_hz=(40.0, 80.0),
        level_db=(94.0, 94.0),
    )
    evaluation = target_objective_evaluation_from_provider(
        revision=fixture.revision,
        search_spec=spec,
        candidate_id=candidate.candidate_id,
        provider=provider,
        receiver_id='receiver-1',
        target=target,
        objective_spec=ResponseObjectiveSpec(low_hz=40.0, high_hz=80.0),
    )
    assert any(
        ref.source_kind == 'r170a_prediction_provider'
        and ref.source_id == provider.provider_id
        for ref in evaluation.input_refs
    )

    objective_repository = CadObjectiveRepository(
        fixture.scene_repository,
        search_repository,
    )
    objective_repository.save_evaluation(evaluation)
    binding, connection = build_provider_objective_connection(
        provider,
        evaluation,
        receiver_id='receiver-1',
    )
    provider_repository.save_binding(binding)
    connection_repository = CadPredictionProviderObjectiveRepository(
        provider_repository,
        objective_repository,
    )
    assert connection_repository.save(connection) == connection
    assert connection_repository.get(connection.connection_id) == connection

    pareto = build_pareto_set(
        (evaluation,),
        ('response.rms_difference_db',),
    )
    objective_repository.save_pareto_set(pareto)
    assert objective_repository.get_pareto_set(pareto.pareto_set_id) == pareto


def test_o50_plan_o60_residual_and_o70_bind_exact_provider_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    provider_repository = fixture.repository()
    provider = provider_repository.save_provider(fixture.build_provider())
    constraints, spec, search_repository, candidates = _search_fixture(fixture)
    candidate = candidates[1]

    working = WorkingDocument(
        fixture.revision.document,
        source_revision_id=fixture.revision.revision_id,
    )
    apply_candidate_positions(
        working,
        candidate,
        spec=spec,
        current_constraint_set=constraints,
    )
    applied = fixture.scene_repository.save(
        working.committed_document,
        parent_revision_id=fixture.revision.revision_id,
    ).revision
    plan = build_measurement_plan(
        fixture.scene_repository,
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate.candidate_id,
        applied_scene_revision_id=applied.revision_id,
    )
    plan_binding = build_prediction_provider_binding(
        provider,
        consumer_kind='O50_MEASUREMENT_PLAN',
        consumer_id=plan.plan_id,
        required_observables=('frequency_response_magnitude',),
    )
    provider_repository.save_binding(plan_binding)
    bound_plan = bind_measurement_plan_prediction(plan, plan_binding)
    assert bound_plan.prediction_provider_binding_id == plan_binding.binding_id

    measurement_repository = CadMeasurementRepository(fixture.scene_repository)
    # The bound plan supersedes the unbound snapshot, so the lifecycle head can
    # only advance once the claimed predecessor is itself persisted.
    measurement_repository.save_measurement_plan(plan)
    measurement_repository.save_measurement_plan(bound_plan)
    assert measurement_repository.latest_measurement_plans(
        spec.search_spec_id
    ) == (bound_plan,)

    raw = declared_fr_raw(
        frequency_hz=(40.0, 80.0),
        level_db=(94.0, 91.0),
        phase_status='absent',
        processing={'fixture_raw': 'r170a-o60-measurement'},
    )
    measurement = CadMeasurementRecord(
        measurement_id='r170a-measurement',
        document_id=fixture.revision.document_id,
        scene_revision_id=fixture.revision.revision_id,
        scene_content_hash=fixture.revision.content_hash,
        measurement_entity_id='receiver-1-entity',
        measurement_position=fixture.revision.document.entity(
            'receiver-1-entity'
        ).position,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('source-1',),
        radiation_scope='single',
        routing_evidence='manual',
        imported_at='2026-09-20T08:45:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='r170a-dataset',
        measurement_id=measurement.measurement_id,
        frequency_hz=(40.0, 80.0),
        level_db=(94.0, 91.0),
        phase_deg=None,
        phase_status='absent',
        processing_json=canonical_json({'fixture_raw': 'r170a-o60-measurement'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        measurement,
        dataset,
        raw_filename='r170a.txt',
        raw_bytes=raw,
    )

    validation = build_provider_measurement_validation(
        provider=provider,
        receiver_id='receiver-1',
        measurement_repository=measurement_repository,
        measurement_id=measurement.measurement_id,
        document_id=fixture.revision.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=generate_cad_candidates(
            fixture.scene_repository,
            spec,
        ).candidate_set_sha256,
        candidate_id=candidate.candidate_id,
        split='holdout',
        low_hz=40.0,
        high_hz=80.0,
        max_holdout_rms_db=20.0,
        evidence_scope='synthetic_fixture',
    )
    assert validation.pairs[0].prediction_source_id == provider.provider_id

    o60_binding = bind_provider_to_validation(provider, validation)
    o70_binding = bind_provider_to_adaptive_validation(provider, validation)
    assert o60_binding.consumer_kind == 'O60_VALIDATION'
    assert o60_binding.consumer_id == validation.validation_id
    assert o70_binding.consumer_kind == 'O70_ADAPTIVE'
    assert o70_binding.consumer_semantic_sha256 == validation.validation_sha256
    provider_repository.save_binding(o60_binding)
    provider_repository.save_binding(o70_binding)
