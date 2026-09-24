from __future__ import annotations

from hashlib import sha256
import json
from math import atan2, degrees, hypot, log10
from pathlib import Path

import pytest

from htdt.cad_candidate_wave_execution import CandidateNumericalOutput
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentDefinition,
    FrequencyDomain,
    InterpolationProvenance,
)
from htdt.cad_geometric_acoustics_adapter import (
    DeterministicAcousticPath,
    DeterministicGaExecutionInput,
    DeterministicGaReceiverInput,
    DeterministicGaSourceInput,
    DeterministicPathArtifact,
)
from htdt.cad_geometric_acoustics_response import (
    ANALYTIC_OMNI_DIRECTIVITY_MODEL,
    build_acoustic_environment_authority,
    build_deterministic_path_frequency_response,
    build_frequency_grid_authority,
    build_path_response_configuration,
    build_point_source_normalization_authority,
    build_receiver_response_authority,
    build_source_response_authority,
)
from htdt.cad_hybrid_grid_reconciliation import HybridNumericalFailureCode
from htdt.cad_hybrid_numerical_composition import (
    CadNumericalHybridResponseRepository,
    NumericalHybridResponseArtifact,
    build_hybrid_convention_normalization_authority,
    build_numerical_hybrid_composition_spec,
    compose_numerical_hybrid_response,
)
from htdt.cad_hybrid_prediction_provider import (
    CadHybridPredictionProviderRepository,
    HybridPredictionProvider,
    build_hybrid_prediction_provider,
    build_hybrid_provider_binding,
    evaluate_excitation_volume_velocity,
    hybrid_provider_frequency_response,
    require_hybrid_binding_current,
)
from htdt.cad_hybrid_prediction_provider_integration import (
    CadHybridPredictionProviderObjectiveRepository,
    bind_hybrid_provider_to_adaptive_validation,
    bind_hybrid_provider_to_validation,
    bind_measurement_plan_hybrid_prediction,
    build_hybrid_provider_measurement_validation,
    build_hybrid_provider_objective_connection,
    target_objective_evaluation_from_hybrid_provider,
)
from htdt.cad_measurement_loop import build_measurement_plan
from htdt.cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementRecord,
)
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
    build_r130_low_band_prediction_provider,
)
from htdt.cad_document import WorkingDocument
from htdt.cad_scene import Direction3, Position3, RoomPrism
from htdt.cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_wave_excitation import (
    ComplexVolumeVelocitySample,
    WaveExcitationEvidenceSubject,
    WaveExcitationManualDerivation,
    build_acoustic_wave_excitation_authority,
    build_wave_excitation_evidence_authority,
)
from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import ResponseObjectiveSpec
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from scripts.run_r130a_candidate_wave_execution import _fixture as r130_fixture


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


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _ref(label: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version=version,
        semantic_hash_sha256=_hash(label),
    )


def _domain(frequencies: tuple[float, ...]) -> FrequencyDomain:
    return FrequencyDomain(
        minimum_hz=float(frequencies[0]),
        maximum_hz=float(frequencies[-1]),
    )


def _actual_r150_direct_response(
    *,
    source_entity_id: str,
    r110_sha256: str,
    receiver_id: str,
    receiver_entity_id: str,
    receiver_position: tuple[float, float, float],
    frequencies: tuple[float, ...],
    sound_speed_m_s: float,
    compiled_geometry_id: str,
    compiled_geometry_sha256: str,
):
    domain = _domain(frequencies)
    grid = build_frequency_grid_authority(frequencies)
    environment = build_acoustic_environment_authority(
        density_kg_m3=1.2,
        sound_speed_m_s=sound_speed_m_s,
        valid_frequency_domain=domain,
    )
    configuration = build_path_response_configuration(
        minimum_path_length_m=1e-6
    )
    normalization = build_point_source_normalization_authority(
        valid_frequency_domain=domain
    )
    directivity_domain = DirectivityDomain(
        frequency=domain,
        horizontal=AngleDomain(minimum_deg=-180.0, maximum_deg=180.0),
        vertical=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
    )
    equipment = EquipmentDefinition.model_construct(
        definition_id='equipment:r170b-integration-source',
        version='1',
        semantic_sha256=_hash('r170b-integration-equipment'),
        directivity=DirectivityCapability.model_construct(
            tier='analytic',
            data_format='analytic_model',
            provenance=None,
            data_asset_sha256=None,
            valid_domain=directivity_domain,
            interpolation=None,
            coherent_phase=True,
            phase_reference='source_volume_velocity_t0',
            analytic_model=ANALYTIC_OMNI_DIRECTIVITY_MODEL,
        ),
    )
    r110_ref = ExactExternalAuthorityRef(
        authority_id='r110-source:r170b-integration',
        authority_version='1',
        semantic_hash_sha256=r110_sha256,
    )
    source = build_source_response_authority(
        source_entity_id=source_entity_id,
        r110_source_ref=r110_ref,
        equipment_definition=equipment,
        point_source_normalization=normalization,
    )
    world_position = Position3(
        x_m=receiver_position[0],
        y_m=receiver_position[1],
        z_m=receiver_position[2],
    )
    receiver = build_receiver_response_authority(
        receiver_id=receiver_id,
        receiver_entity_id=receiver_entity_id,
        receiver_authority_ref=_ref('integration-receiver'),
        world_position=world_position,
    )
    r120_ref = ExactExternalAuthorityRef(
        authority_id=compiled_geometry_id,
        authority_version='1',
        semantic_hash_sha256=compiled_geometry_sha256,
    )
    path = DeterministicAcousticPath.model_construct(
        path_id=f'deterministic-acoustic-path:{_hash("integration-path")}',
        semantic_sha256=_hash('integration-path'),
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        receiver_entity_id=receiver_entity_id,
        path_type='direct',
        ordered_interaction_surface_ids=(),
        ordered_interaction_points=(),
        ordered_interactions=None,
        ordered_region_ids=None,
        region_segment_evidence=None,
        geometric_path_length_m=2.0,
        propagation_delay_s=2.0 / sound_speed_m_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=1.0, y=0.0, z=0.0),
        bands=(),
        adapter_id='htdt.r150.deterministic-path',
        adapter_version='1',
        solver_implementation_ref=_ref('r150-solver'),
    )
    execution = DeterministicGaExecutionInput.model_construct(
        authority_version='r150-deterministic-ga-1',
        execution_input_id=f'r150-ga-execution-input:{_hash("integration-execution")}',
        semantic_sha256=_hash('integration-execution'),
        r120_compiled_geometry_id=r120_ref.authority_id,
        r120_compiled_geometry_sha256=r120_ref.semantic_hash_sha256,
        portal_authority_ref=_ref('integration-portals'),
        sources=(
            DeterministicGaSourceInput(
                source_entity_id=source_entity_id,
                r110_compiled_source_sha256=r110_sha256,
                source_reference_point=Position3(
                    x_m=0.0,
                    y_m=0.0,
                    z_m=0.0,
                ),
                source_axis=Direction3(x=1.0, y=0.0, z=0.0),
                directivity_dataset_id='analytic:omnidirectional',
                directivity_dataset_version='1',
                directivity_dataset_sha256=_hash('analytic-directivity'),
            ),
        ),
        receivers=(
            DeterministicGaReceiverInput(
                receiver_id=receiver_id,
                entity_id=receiver_entity_id,
                world_position=world_position,
            ),
        ),
        sound_speed_m_s=sound_speed_m_s,
        frequency_domain=domain,
    )
    path_artifact = DeterministicPathArtifact.model_construct(
        authority_version='r150-deterministic-ga-1',
        artifact_id=f'deterministic-path-artifact:{_hash("integration-path-artifact")}',
        semantic_sha256=_hash('integration-path-artifact'),
        execution_input_id=execution.execution_input_id,
        execution_input_sha256=execution.semantic_sha256,
        r120_compiled_geometry_id=r120_ref.authority_id,
        r120_compiled_geometry_sha256=r120_ref.semantic_hash_sha256,
        frequency_domain=domain,
        paths=(path,),
        rejected_candidates=(),
    )
    response = build_deterministic_path_frequency_response(
        path_artifact=path_artifact,
        execution_input=execution,
        path_id=path.path_id,
        r120_geometry_ref=r120_ref,
        source_authority=source,
        point_source_normalization=normalization,
        receiver_authority=receiver,
        environment=environment,
        frequency_grid=grid,
        configuration=configuration,
    )
    assert response.capability == 'COMPLEX_SUPPORTED'
    return response


def _build_bundle(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_grid: tuple[float, ...] = (40.0, 60.0, 80.0),
    wave_transfer_plus: tuple[complex, complex] = (
        2.0 + 3.0j,
        4.0 + 0.5j,
    ),
):
    fixture = r130_fixture(
        root / 'r130-fixture',
        root / 'unused-pffdtd-upstream',
        fixture_id=f'r170b-{root.name}',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=3.0),
    )
    candidate, _ = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    wave_grid = tuple(float(item) for item in fixture['configuration'].frequency_samples_hz)
    assert wave_grid == (40.0, 80.0)
    q_minus = tuple(
        complex(item.real_m3_s, item.imag_m3_s)
        for item in fixture['excitation'].samples
    )
    pressure_minus = tuple(
        transfer.conjugate() * q
        for transfer, q in zip(wave_transfer_plus, q_minus, strict=True)
    )
    numerical = CandidateNumericalOutput(
        receiver_ids=tuple(item.receiver_id for item in candidate.receivers),
        frequency_hz=wave_grid,
        pressure_real_pa=(tuple(item.real for item in pressure_minus),),
        pressure_imag_pa=(tuple(item.imag for item in pressure_minus),),
        raw_solver_asset_sha256=_hash(f'r170b-raw-{root.name}'),
        raw_solver_asset_name='mocked-sim-outs.h5',
        time_step_s=1.0e-4,
        time_step_count=128,
        grid_shape=(8, 8, 8),
        sound_speed_m_s=float(fixture['environment'].sound_speed_m_s),
        compile_seconds=0.0,
        solve_seconds=0.0,
        postprocess_seconds=0.0,
        compatibility_patch={'r170b_fixture': True},
    )
    monkeypatch.setattr(
        fixture['executor'],
        '_run_pffdtd',
        lambda **_: numerical,
    )
    result = fixture['executor'].execute(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    payload = fixture['store'].read_payload(
        result.artifacts[0].artifact_authority
    )
    candidate_after, _ = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    assert candidate_after == candidate

    base_provider = build_r130_low_band_prediction_provider(
        revision=fixture['revision'],
        snapshot=fixture['snapshot'],
        request=fixture['request'],
        result=result,
        external_payload_resolver=fixture['store'].read_payload,
    )
    base_repository = CadPredictionProviderRepository(
        fixture['scene_repository'],
        snapshot_request_resolver=fixture['snapshot_repository'],
        solver_result_resolver=fixture['result_repository'],
        external_payload_resolver=fixture['store'].read_payload,
    )
    base_repository.save_provider(base_provider)

    receiver = candidate.receivers[0]
    r150_response = _actual_r150_direct_response(
        source_entity_id=candidate.source_entity_id,
        r110_sha256=candidate.r110_compiled_source_sha256,
        receiver_id=receiver.receiver_id,
        receiver_entity_id=receiver.entity_id,
        receiver_position=receiver.position_m,
        frequencies=wave_grid,
        sound_speed_m_s=float(fixture['environment'].sound_speed_m_s),
        compiled_geometry_id=candidate.compiled_geometry_id,
        compiled_geometry_sha256=candidate.compiled_geometry_sha256,
    )
    normalization = build_hybrid_convention_normalization_authority()
    reconciliation_method = (
        'exact_bin_identity_v1'
        if output_grid == wave_grid
        else 'cartesian_linear_v1'
    )
    spec = build_numerical_hybrid_composition_spec(
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=fixture['excitation'],
        r150_responses=(r150_response,),
        receiver_id=receiver.receiver_id,
        exact_frequency_grid_hz=output_grid,
        transition_start_hz=40.0,
        transition_end_hz=80.0,
        normalization_authority=normalization,
        reconciliation_method=reconciliation_method,
    )
    artifact = compose_numerical_hybrid_response(
        spec=spec,
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=fixture['excitation'],
        r150_responses=(r150_response,),
        normalization_authority=normalization,
    )
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'

    specs = {spec.composition_spec_id: spec}
    excitations = {
        fixture['excitation'].excitation_id: fixture['excitation']
    }
    responses = {r150_response.artifact_id: r150_response}
    normalizations = {normalization.authority_id: normalization}
    r160_repository = CadNumericalHybridResponseRepository(
        fixture['scene_repository'],
        wave_result_resolver=lambda result_id: (
            result if result_id == result.result_id else None
        ),
        wave_artifact_payload_resolver=lambda ref: (
            payload
            if ref == spec.r130_complex_pressure_artifact_ref
            else None
        ),
        candidate_input_resolver=lambda input_id: (
            candidate if input_id == candidate.execution_input_id else None
        ),
        wave_excitation_resolver=lambda excitation_id: excitations.get(
            excitation_id
        ),
        r150_response_resolver=lambda artifact_id: responses.get(artifact_id),
        composition_spec_resolver=lambda spec_id: specs.get(spec_id),
        convention_authority_resolver=lambda ref: normalizations.get(
            ref.authority_id
        ),
    )
    r160_repository.save(artifact)

    hybrid_repository = CadHybridPredictionProviderRepository(
        fixture['scene_repository'],
        base_provider_repository=base_repository,
        r160_repository=r160_repository,
        composition_spec_resolver=lambda spec_id: specs.get(spec_id),
        wave_excitation_resolver=lambda excitation_id: excitations.get(
            excitation_id
        ),
    )
    provider = hybrid_repository.build_current(
        base_provider_id=base_provider.provider_id,
        r160_artifact_id=artifact.artifact_id,
    )
    return {
        'fixture': fixture,
        'candidate': candidate,
        'result': result,
        'payload': payload,
        'base_provider': base_provider,
        'base_repository': base_repository,
        'r150_response': r150_response,
        'normalization': normalization,
        'spec': spec,
        'artifact': artifact,
        'provider': provider,
        'r160_repository': r160_repository,
        'hybrid_repository': hybrid_repository,
        'specs': specs,
        'excitations': excitations,
        'responses': responses,
        'normalizations': normalizations,
    }


def _search_fixture(bundle):
    """Minimal exact SearchSpec authority; O40 regression does not exercise placement."""

    fixture = bundle['fixture']
    revision = fixture['revision']
    constraint_set = CadConstraintSet(
        document_id=revision.document_id,
        constraints=(),
    )
    axis = CadSearchAxis(
        entity_id='speaker-source',
        axis='x',
        min_m=1.0,
        max_m=2.0,
        step_m=1.0,
    )
    spec, _estimate = build_cad_search_spec(
        revision,
        constraint_set,
        (axis,),
        candidate_limit=10,
        name='r170b objective-only integration fixture',
    )
    search_repository = CadSearchRepository(fixture['scene_repository'])
    search_repository.save(spec)
    page = generate_cad_candidates(
        fixture['scene_repository'],
        spec,
        limit=10,
    )
    assert page.candidates, 'objective fixture SearchSpec must produce candidates'
    return spec, search_repository, page.candidates[0].candidate_id


def test_exact_identity_absolute_pressure_db_phase_and_phasor_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'primary', monkeypatch)
    first = bundle['provider']
    second = build_hybrid_prediction_provider(
        base_provider=bundle['base_provider'],
        r160_artifact=bundle['artifact'],
        composition_spec=bundle['spec'],
        wave_excitation=bundle['fixture']['excitation'],
    )
    assert first == second
    assert first.provider_id == second.provider_id
    assert first.base_provider_ref == bundle['base_provider'].ref()
    assert first.r160_artifact_ref == bundle['artifact'].as_external_ref()
    assert first.r160_composition_spec_ref == bundle['spec'].as_external_ref()
    assert first.exact_r130_result.result_id == bundle['result'].result_id
    assert first.exact_r150_response_refs == (
        bundle['r150_response'].as_external_ref(),
    )
    assert first.evidence_state == 'candidate'
    assert first.evidence_scope == 'unvalidated'
    assert first.production_adoption is False

    artifact_sample = bundle['artifact'].samples[0]
    q_minus = evaluate_excitation_volume_velocity(
        bundle['fixture']['excitation'],
        artifact_sample.frequency_hz,
    )
    q_plus = q_minus.conjugate()
    transfer = complex(
        artifact_sample.complex_real_pa_per_m3_s,
        artifact_sample.complex_imag_pa_per_m3_s,
    )
    expected = transfer * q_plus
    sample = first.absolute_pressure_samples[0]
    assert complex(sample.complex_real_pa, sample.complex_imag_pa) == pytest.approx(
        expected
    )
    assert sample.magnitude_pa == pytest.approx(abs(expected))
    assert sample.magnitude_db_spl == pytest.approx(
        20.0 * log10(abs(expected) / 20.0e-6)
    )
    assert sample.phase_deg == pytest.approx(
        degrees(atan2(expected.imag, expected.real))
    )
    assert first.phasor_convention == 'exp(+i*omega*t)'
    assert first.excitation_phasor_convention == 'exp(-i*omega*t)'


def _manual_excitation_evidence(
    *,
    definition_id: str,
    definition_sha256: str,
    samples: tuple[ComplexVolumeVelocitySample, ...],
):
    return build_wave_excitation_evidence_authority(
        evidence_kind='user_defined',
        source_name='R170B Q evaluator',
        source_version='1',
        source_reference='synthetic unit test',
        derivation=WaveExcitationManualDerivation(
            author='r170b-fixture',
            authored_at_utc='2026-09-20T00:00:00+00:00',
        ),
        subject=WaveExcitationEvidenceSubject(
            definition_id=definition_id,
            definition_version='1',
            definition_sha256=definition_sha256,
            samples=samples,
        ),
    )


def test_q_linear_evaluation_and_unsupported_missing_frequency_rejection() -> None:
    linear_samples = (
        ComplexVolumeVelocitySample(
            frequency_hz=40.0,
            real_m3_s=1.0,
            imag_m3_s=1.0,
        ),
        ComplexVolumeVelocitySample(
            frequency_hz=80.0,
            real_m3_s=3.0,
            imag_m3_s=-1.0,
        ),
    )
    linear_evidence = _manual_excitation_evidence(
        definition_id='equipment:q',
        definition_sha256=_hash('equipment-q'),
        samples=linear_samples,
    )
    linear = build_acoustic_wave_excitation_authority(
        definition_id='equipment:q',
        definition_version='1',
        definition_sha256=_hash('equipment-q'),
        samples=linear_samples,
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='cartesian-linear-test',
            implementation_version='1',
            provenance=linear_evidence.provenance,
        ),
        provenance=(linear_evidence.provenance,),
        evidence=(linear_evidence,),
        approximation_note='explicit complex Cartesian linear interpolation test',
    )
    assert evaluate_excitation_volume_velocity(linear, 60.0) == pytest.approx(
        2.0 + 0.0j
    )
    with pytest.raises(ValueError, match='extrapolation'):
        evaluate_excitation_volume_velocity(linear, 100.0)

    custom_evidence = _manual_excitation_evidence(
        definition_id='equipment:q-custom',
        definition_sha256=_hash('equipment-q-custom'),
        samples=linear.samples,
    )
    custom = build_acoustic_wave_excitation_authority(
        definition_id='equipment:q-custom',
        definition_version='1',
        definition_sha256=_hash('equipment-q-custom'),
        samples=linear.samples,
        interpolation=InterpolationProvenance(
            method='custom',
            implementation='not-supported-by-r170b',
            implementation_version='1',
            provenance=custom_evidence.provenance,
        ),
        provenance=(custom_evidence.provenance,),
        evidence=(custom_evidence,),
        approximation_note='custom method intentionally unsupported by R170B',
    )
    with pytest.raises(ValueError, match='not exactly evaluable'):
        evaluate_excitation_volume_velocity(custom, 60.0)


def test_exact_r130_r150_and_unsupported_artifact_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'identity-a', monkeypatch)
    other = _build_bundle(
        tmp_path / 'identity-b',
        monkeypatch,
        wave_transfer_plus=(7.0 + 1.0j, 8.0 - 2.0j),
    )
    with pytest.raises(ValueError, match='R130 result identity'):
        build_hybrid_prediction_provider(
            base_provider=other['base_provider'],
            r160_artifact=bundle['artifact'],
            composition_spec=bundle['spec'],
            wave_excitation=bundle['fixture']['excitation'],
        )

    wrong_ref = _ref('wrong-r150-response')
    malformed = bundle['artifact'].model_copy(
        update={'exact_r150_response_refs': (wrong_ref,)}
    )
    with pytest.raises(ValueError, match='R150 response identity'):
        build_hybrid_prediction_provider(
            base_provider=bundle['base_provider'],
            r160_artifact=malformed,
            composition_spec=bundle['spec'],
            wave_excitation=bundle['fixture']['excitation'],
        )

    unsupported_probe = bundle['artifact'].model_copy(
        update={
            'capability_state': 'UNSUPPORTED',
            'failure_codes': (
                HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
            ),
            'unsupported_reasons': ('synthetic unsupported fixture',),
            'samples': (),
        }
    )
    unsupported_core = unsupported_probe.model_dump(
        mode='json',
        exclude={'artifact_id', 'semantic_sha256'},
    )
    unsupported_digest = _digest(unsupported_core)
    unsupported = NumericalHybridResponseArtifact.model_validate(
        unsupported_probe.model_dump(mode='python')
        | {
            'artifact_id': (
                f'r160-numerical-hybrid-response:{unsupported_digest}'
            ),
            'semantic_sha256': unsupported_digest,
        }
    )
    with pytest.raises(ValueError, match='COMPLEX_SUPPORTED'):
        build_hybrid_prediction_provider(
            base_provider=bundle['base_provider'],
            r160_artifact=unsupported,
            composition_spec=bundle['spec'],
            wave_excitation=bundle['fixture']['excitation'],
        )


def test_n70_exact_source_receiver_grid_and_candidate_only_enforcement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'n70', monkeypatch)
    provider = bundle['provider']
    response = hybrid_provider_frequency_response(
        provider,
        source_entity_id=provider.source_entity_id,
        receiver_id=provider.receiver_id,
        low_hz=40.0,
        high_hz=80.0,
    )
    assert response.frequency_hz == (40.0, 60.0, 80.0)

    with pytest.raises(ValueError, match='source identity'):
        hybrid_provider_frequency_response(
            provider,
            source_entity_id='wrong-source',
            receiver_id=provider.receiver_id,
            low_hz=40.0,
            high_hz=80.0,
        )
    with pytest.raises(ValueError, match='receiver identity'):
        hybrid_provider_frequency_response(
            provider,
            source_entity_id=provider.source_entity_id,
            receiver_id='wrong-receiver',
            low_hz=40.0,
            high_hz=80.0,
        )
    with pytest.raises(ValueError, match='exceeds exact output domain'):
        hybrid_provider_frequency_response(
            provider,
            source_entity_id=provider.source_entity_id,
            receiver_id=provider.receiver_id,
            low_hz=20.0,
            high_hz=80.0,
        )
    for update in (
        {'evidence_state': 'validated'},
        {'evidence_state': 'production'},
        {'evidence_scope': 'owned_room'},
        {'production_adoption': True},
    ):
        with pytest.raises(Exception):
            HybridPredictionProvider.model_validate(
                provider.model_dump(mode='python') | update
            )

    assert provider.capability('frequency_response_magnitude').state == 'READY'
    assert provider.capability('frequency_response_phase').state == 'READY'
    assert provider.capability('broadband_hybrid').state == 'READY'
    for observable in (
        'impulse_response',
        'rt60',
        'edt',
        'c50',
        'c80',
        'arrival_timing',
        'late_decay',
        'diffraction_completeness',
        'spatial_pressure_field',
    ):
        assert provider.capability(observable).state == 'UNSUPPORTED'


def test_save_reopen_and_exact_stale_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'persistence', monkeypatch)
    provider = bundle['provider']
    repository = bundle['hybrid_repository']
    assert repository.save(provider) == provider
    assert repository.get(provider.provider_id) == provider

    original_r160_get = bundle['r160_repository'].get
    monkeypatch.setattr(bundle['r160_repository'], 'get', lambda artifact_id: None)
    with pytest.raises(ValueError, match='R160 numerical artifact'):
        repository.get(provider.provider_id)
    monkeypatch.setattr(bundle['r160_repository'], 'get', original_r160_get)
    assert repository.get(provider.provider_id) == provider

    saved_spec = bundle['specs'].pop(bundle['spec'].composition_spec_id)
    with pytest.raises(ValueError, match='R160 numerical composition authority|composition spec'):
        repository.get(provider.provider_id)
    bundle['specs'][saved_spec.composition_spec_id] = saved_spec
    assert repository.get(provider.provider_id) == provider

    saved_response = bundle['responses'].pop(bundle['r150_response'].artifact_id)
    with pytest.raises(ValueError, match='R150 response'):
        repository.get(provider.provider_id)
    bundle['responses'][saved_response.artifact_id] = saved_response
    assert repository.get(provider.provider_id) == provider

    saved_excitation = bundle['excitations'].pop(
        bundle['fixture']['excitation'].excitation_id
    )
    with pytest.raises(ValueError, match='wave excitation|source excitation'):
        repository.get(provider.provider_id)
    bundle['excitations'][saved_excitation.excitation_id] = saved_excitation
    assert repository.get(provider.provider_id) == provider

    original_get = bundle['base_repository'].solver_result_resolver.get
    monkeypatch.setattr(
        bundle['base_repository'].solver_result_resolver,
        'get',
        lambda result_id: None,
    )
    with pytest.raises(ValueError, match='underlying solver result|base R170A'):
        repository.get(provider.provider_id)
    monkeypatch.setattr(
        bundle['base_repository'].solver_result_resolver,
        'get',
        original_get,
    )


def test_o30_exact_binding_and_o40_regression_without_algorithm_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'objective', monkeypatch)
    provider = bundle['hybrid_repository'].save(bundle['provider'])
    spec, search_repository, candidate_id = _search_fixture(bundle)
    target = FrequencyResponse(
        frequency_hz=(40.0, 60.0, 80.0),
        level_db=(80.0, 80.0, 80.0),
    )
    objective_spec = ResponseObjectiveSpec(low_hz=40.0, high_hz=80.0)
    evaluation = target_objective_evaluation_from_hybrid_provider(
        revision=bundle['fixture']['revision'],
        search_spec=spec,
        candidate_id=candidate_id,
        provider=provider,
        source_entity_id=provider.source_entity_id,
        receiver_id=provider.receiver_id,
        target=target,
        objective_spec=objective_spec,
    )
    assert any(
        ref.source_kind == 'r170b_hybrid_prediction_provider'
        and ref.source_id.startswith('r170b-hybrid-objective-input:')
        for ref in evaluation.input_refs
    )

    objective_repository = CadObjectiveRepository(
        bundle['fixture']['scene_repository'],
        search_repository,
        hybrid_provider_repository=bundle['hybrid_repository'],
    )
    objective_repository.save_evaluation(evaluation)
    connection = build_hybrid_provider_objective_connection(
        provider,
        evaluation,
        source_entity_id=provider.source_entity_id,
        receiver_id=provider.receiver_id,
        low_hz=40.0,
        high_hz=80.0,
    )
    assert connection.provider_ref == provider.ref()
    assert connection.receiver_id == provider.receiver_id
    assert connection.requested_low_hz == 40.0
    assert connection.requested_high_hz == 80.0
    assert connection.evaluation_id == evaluation.evaluation_id
    connection_repository = CadHybridPredictionProviderObjectiveRepository(
        bundle['hybrid_repository'],
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


def test_o50_plan_o60_residual_and_o70_bind_exact_hybrid_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'validation', monkeypatch)
    provider = bundle['hybrid_repository'].save(bundle['provider'])
    fixture = bundle['fixture']
    spec, search_repository, _first_candidate_id = _search_fixture(bundle)

    page = generate_cad_candidates(fixture['scene_repository'], spec, limit=10)
    candidate = page.candidates[0]
    receiver = bundle['candidate'].receivers[0]
    constraints = CadConstraintSet(
        document_id=fixture['revision'].document_id,
        constraints=(),
    )
    working = WorkingDocument(
        fixture['revision'].document,
        source_revision_id=fixture['revision'].revision_id,
    )
    apply_candidate_positions(
        working,
        candidate,
        spec=spec,
        current_constraint_set=constraints,
    )
    applied = fixture['scene_repository'].save(
        working.committed_document,
        parent_revision_id=fixture['revision'].revision_id,
    ).revision
    plan = build_measurement_plan(
        fixture['scene_repository'],
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate.candidate_id,
        applied_scene_revision_id=applied.revision_id,
    )
    plan_binding = build_hybrid_provider_binding(
        provider,
        consumer_kind='O50_MEASUREMENT_PLAN',
        consumer_id=plan.plan_id,
        required_observables=('frequency_response_magnitude',),
    )
    bundle['hybrid_repository'].save_binding(plan_binding)
    bound_plan = bind_measurement_plan_hybrid_prediction(plan, plan_binding)
    assert bound_plan.prediction_provider_binding_id == plan_binding.binding_id
    assert bound_plan.supersedes_plan_sha256 == plan.plan_sha256

    measurement_repository = CadMeasurementRepository(fixture['scene_repository'])
    measurement_repository.save_measurement_plan(plan)
    measurement_repository.save_measurement_plan(bound_plan)
    assert measurement_repository.latest_measurement_plans(
        spec.search_spec_id
    ) == (bound_plan,)

    raw = declared_fr_raw(
        frequency_hz=(40.0, 60.0, 80.0),
        level_db=(94.0, 92.0, 91.0),
        phase_status='absent',
        processing={'fixture_raw': 'r170b-o60-measurement'},
    )
    measurement = CadMeasurementRecord(
        measurement_id='r170b-measurement',
        document_id=fixture['revision'].document_id,
        scene_revision_id=fixture['revision'].revision_id,
        scene_content_hash=fixture['revision'].content_hash,
        measurement_entity_id=receiver.entity_id,
        measurement_position=Position3(
            x_m=receiver.position_m[0],
            y_m=receiver.position_m[1],
            z_m=receiver.position_m[2],
        ),
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=(provider.source_entity_id,),
        radiation_scope='single',
        routing_evidence='manual',
        imported_at='2026-09-24T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='r170b-dataset',
        measurement_id=measurement.measurement_id,
        frequency_hz=(40.0, 60.0, 80.0),
        level_db=(94.0, 92.0, 91.0),
        phase_deg=None,
        phase_status='absent',
        processing_json=canonical_json({'fixture_raw': 'r170b-o60-measurement'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        measurement,
        dataset,
        raw_filename='r170b.txt',
        raw_bytes=raw,
    )

    validation = build_hybrid_provider_measurement_validation(
        provider=provider,
        source_entity_id=provider.source_entity_id,
        receiver_id=provider.receiver_id,
        measurement_repository=measurement_repository,
        measurement_id=measurement.measurement_id,
        document_id=fixture['revision'].document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        candidate_id=candidate.candidate_id,
        split='holdout',
        low_hz=40.0,
        high_hz=80.0,
        max_holdout_rms_db=30.0,
        evidence_scope='synthetic_fixture',
    )
    assert validation.pairs[0].prediction_source_id == provider.provider_id
    assert validation.model_id == provider.adapter_id

    with pytest.raises(ValueError, match='document identity'):
        build_hybrid_provider_measurement_validation(
            provider=provider,
            source_entity_id=provider.source_entity_id,
            receiver_id=provider.receiver_id,
            measurement_repository=measurement_repository,
            measurement_id=measurement.measurement_id,
            document_id='other-document',
            search_spec_id=spec.search_spec_id,
            search_spec_sha256=spec.search_spec_sha256,
            candidate_set_sha256=page.candidate_set_sha256,
            candidate_id=candidate.candidate_id,
            split='holdout',
            low_hz=40.0,
            high_hz=80.0,
            max_holdout_rms_db=30.0,
        )

    o60_binding = bind_hybrid_provider_to_validation(provider, validation)
    o70_binding = bind_hybrid_provider_to_adaptive_validation(provider, validation)
    assert o60_binding.consumer_kind == 'O60_VALIDATION'
    assert o60_binding.consumer_id == validation.validation_id
    assert o60_binding.provider_ref == provider.ref()
    assert o60_binding.base_provider_ref == provider.base_provider_ref
    assert o70_binding.consumer_kind == 'O70_ADAPTIVE'
    assert o70_binding.consumer_semantic_sha256 == validation.validation_sha256
    bundle['hybrid_repository'].save_binding(o60_binding)
    bundle['hybrid_repository'].save_binding(o70_binding)
    assert bundle['hybrid_repository'].get_binding(o60_binding.binding_id) == o60_binding
    assert bundle['hybrid_repository'].bindings_for_consumer(
        consumer_kind='O60_VALIDATION',
        consumer_id=validation.validation_id,
    ) == (o60_binding,)

    require_hybrid_binding_current(
        o60_binding,
        provider,
        provider.base_current_authority,
    )
    foreign = build_hybrid_provider_measurement_validation(
        provider=provider,
        source_entity_id=provider.source_entity_id,
        receiver_id=provider.receiver_id,
        measurement_repository=measurement_repository,
        measurement_id=measurement.measurement_id,
        document_id=fixture['revision'].document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        candidate_id='unrelated-candidate',
        split='calibration',
        low_hz=40.0,
        high_hz=80.0,
        max_holdout_rms_db=30.0,
    )
    with pytest.raises(ValueError, match='does not reference this hybrid provider'):
        bind_hybrid_provider_to_validation(
            provider,
            foreign.model_copy(
                update={
                    'pairs': tuple(
                        pair.model_copy(update={'prediction_source_id': 'other'})
                        for pair in foreign.pairs
                    )
                }
            ),
        )
    assert foreign.pairs[0].candidate_id == 'unrelated-candidate'

    with pytest.raises(ValueError, match='authority is stale'):
        require_hybrid_binding_current(
            o60_binding,
            provider,
            provider.base_current_authority.model_copy(
                update={'scene_revision_id': 'stale-revision'}
            ),
        )
    with pytest.raises(ValueError, match='observable is unsupported'):
        build_hybrid_provider_binding(
            provider,
            consumer_kind='O60_VALIDATION',
            consumer_id='v-unsupported',
            required_observables=('impulse_response',),
        )
    with pytest.raises(ValueError, match='O50 hybrid-provider binding'):
        bind_measurement_plan_hybrid_prediction(plan, o60_binding)
