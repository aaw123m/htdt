from __future__ import annotations

import cmath
from hashlib import sha256
import json
from math import pi
from pathlib import Path

import pytest

from htdt.cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
)
from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    CandidateNumericalOutput,
    CandidateReceiverBinding,
    CandidateResourceConfiguration,
    CandidateRuntimeIdentity,
    CandidateWaveExecutionError,
    CandidateWaveExecutionInput,
)
from htdt.cad_acoustic_snapshot import (
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
)
from htdt.cad_acoustic_solver_adapter import (
    bind_prediction_request_to_solver_adapter,
)
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
    DeterministicPathFrequencyResponseArtifact,
    PathFrequencyResponseSample,
    build_acoustic_environment_authority,
    build_deterministic_path_frequency_response,
    build_frequency_grid_authority,
    build_path_response_configuration,
    build_point_source_normalization_authority,
    build_receiver_response_authority,
    build_source_response_authority,
)
from htdt.cad_hybrid_grid_reconciliation import (
    HybridNumericalCompositionError,
    HybridNumericalFailureCode,
    build_frequency_grid_reconciliation_authority,
    build_hybrid_crossover_configuration_authority,
)
from htdt.cad_hybrid_numerical_composition import (
    COMMON_PHASOR_CONVENTION,
    R130_ANALYSIS_FOURIER_KERNEL,
    R130_PHASOR_CONVENTION,
    R130_PRESSURE_REFERENCE,
    CadNumericalHybridResponseRepository,
    aggregate_r150_complex_paths,
    build_hybrid_convention_normalization_authority,
    build_numerical_hybrid_composition_spec,
    compose_numerical_hybrid_response,
    convert_complex_phasor,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Position3
from htdt.cad_wave_excitation import (
    ComplexVolumeVelocitySample,
    WaveExcitationEvidenceSubject,
    WaveExcitationManualDerivation,
    build_acoustic_wave_excitation_authority,
    build_wave_excitation_evidence_authority,
)
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


def _ref(label: str, *, version: str = '1') -> ExactExternalAuthorityRef:
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


def _excitation(
    frequencies: tuple[float, ...],
    q_values: tuple[complex, ...],
):
    samples = tuple(
        ComplexVolumeVelocitySample(
            frequency_hz=frequency,
            real_m3_s=value.real,
            imag_m3_s=value.imag,
        )
        for frequency, value in zip(frequencies, q_values, strict=True)
    )
    evidence = build_wave_excitation_evidence_authority(
        evidence_kind='user_defined',
        source_name='R160 numerical composition synthetic excitation',
        source_version='1',
        source_reference='synthetic algebra fixture; not physical validation',
        derivation=WaveExcitationManualDerivation(
            author='r160-fixture',
            authored_at_utc='2026-09-20T00:00:00+00:00',
        ),
        subject=WaveExcitationEvidenceSubject(
            definition_id='equipment:r160-synthetic-source',
            definition_version='1',
            definition_sha256=_hash('r160-synthetic-equipment'),
            samples=samples,
        ),
    )
    return build_acoustic_wave_excitation_authority(
        definition_id='equipment:r160-synthetic-source',
        definition_version='1',
        definition_sha256=_hash('r160-synthetic-equipment'),
        samples=samples,
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='unused-exact-bin-fixture',
            implementation_version='1',
            provenance=evidence.provenance,
        ),
        provenance=(evidence.provenance,),
        evidence=(evidence,),
        approximation_note=(
            'R160 tests use only exact samples; interpolation is never invoked.'
        ),
    )


def _candidate_input(
    *,
    frequencies: tuple[float, ...],
    excitation,
    source_entity_id: str = 'source-1',
    receiver_id: str = 'receiver-1',
    receiver_entity_id: str = 'receiver-entity-1',
) -> CandidateWaveExecutionInput:
    placeholder = CandidateWaveExecutionInput.model_construct(
        authority_version='r130a-candidate-wave-input-1',
        execution_input_id=f'candidate-wave-input:{"0" * 64}',
        semantic_sha256='0' * 64,
        snapshot_id='snapshot:r160-test',
        snapshot_sha256=_hash('snapshot'),
        prediction_request_id='request:r160-test',
        prediction_request_sha256=_hash('request'),
        prediction_deterministic_input_hash=_hash('request-input'),
        dispatch_binding_id='dispatch:r160-test',
        dispatch_binding_sha256=_hash('dispatch'),
        dispatch_deterministic_solver_input_hash=_hash('dispatch-input'),
        compiled_geometry_id='r120-compiled-geometry:r160-test',
        compiled_geometry_sha256=_hash('geometry'),
        compiled_topology_sha256=_hash('topology'),
        material_boundary_configuration_sha256=_hash('materials'),
        boundary_bindings=(),
        treatment_boundary_composition_sha256=_hash('treatments'),
        acoustic_region_authority_ref=_ref('regions'),
        portal_authority_ref=_ref('portals'),
        boundary_termination_authority_ref=None,
        source_entity_id=source_entity_id,
        r110_compiled_source_sha256=_hash('r110-source'),
        wave_excitation_binding_id='wave-binding:r160-test',
        wave_excitation_binding_sha256=_hash('wave-binding'),
        wave_excitation_id=excitation.excitation_id,
        wave_excitation_sha256=excitation.semantic_sha256,
        receivers=(
            CandidateReceiverBinding(
                receiver_id=receiver_id,
                entity_id=receiver_entity_id,
                position_m=(2.0, 0.0, 0.0),
            ),
        ),
        requested_frequency_domain=_domain(frequencies),
        frequency_samples_hz=frequencies,
        observation_time_s=0.1,
        solver_implementation_ref=_ref('solver-implementation'),
        solver_configuration_ref=_ref('solver-configuration'),
        adapter_descriptor_id='adapter:r160-test',
        adapter_descriptor_sha256=_hash('adapter'),
        adapter_compiler_id='htdt.r130a.pffdtd_candidate_input_compiler',
        adapter_compiler_version='1',
        solver_model_sha256=_hash('solver-model'),
        runtime_identity=CandidateRuntimeIdentity(
            operating_system='test',
            architecture='test',
            python_version='test',
            cpu_identity='test',
            logical_threads=1,
            package_versions=(),
        ),
        resource_configuration=CandidateResourceConfiguration(
            solver_threads=1,
            setup_processes=1,
            max_grid_cells=1,
            max_time_steps=1,
            max_output_bytes=1,
            max_solver_wall_seconds=1.0,
        ),
        polyhedral_geometry_binding=None,
    )
    core = placeholder.semantic_payload()
    digest = _digest(core)
    return CandidateWaveExecutionInput(
        execution_input_id=f'candidate-wave-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _solver_result(
    *,
    candidate: CandidateWaveExecutionInput,
    artifact_ref: ExactExternalAuthorityRef,
    frequencies: tuple[float, ...],
) -> AcousticSolverResultEnvelope:
    manifest = AcousticSolverObservableArtifact(
        observable='complex_pressure',
        artifact_authority=artifact_ref,
        encoding_schema_ref=_ref(
            'r130-complex-pressure-schema',
            version=COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        ),
        valid_frequency_domain=_domain(frequencies),
    )
    placeholder = AcousticSolverResultEnvelope.model_construct(
        schema_version=1,
        authority_version='acoustic-solver-result-1',
        result_id=f'acoustic-solver-result:{"0" * 64}',
        semantic_sha256='0' * 64,
        execution_id='r130:r160-test',
        result_state='COMPLETED',
        dispatch_binding_id=f'acoustic-solver-dispatch:{_hash("binding")}',
        dispatch_binding_sha256=_hash('binding'),
        prediction_request_id=f'acoustic-prediction-request:{_hash("prediction")}',
        prediction_request_semantic_sha256=_hash('prediction'),
        prediction_deterministic_input_hash=_hash('prediction-input'),
        acoustic_scene_snapshot_id=f'acoustic-scene-snapshot:{_hash("scene")}',
        acoustic_scene_snapshot_sha256=_hash('scene'),
        adapter_descriptor_id=f'acoustic-solver-adapter:{_hash("adapter")}',
        adapter_descriptor_semantic_sha256=_hash('adapter'),
        deterministic_solver_input_hash=_hash('solver-input'),
        solver_implementation_ref=candidate.solver_implementation_ref,
        solver_configuration_ref=candidate.solver_configuration_ref,
        execution_provenance_ref=_ref('execution-provenance'),
        artifacts=(manifest,),
        completed_at_utc='2026-09-20T00:00:00+00:00',
    )
    core = placeholder.semantic_payload()
    digest = _digest(core)
    return AcousticSolverResultEnvelope(
        result_id=f'acoustic-solver-result:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _r130_payload(
    *,
    candidate: CandidateWaveExecutionInput,
    excitation,
    frequencies: tuple[float, ...],
    physical_transfer_plus: tuple[complex, ...],
) -> dict[str, object]:
    q_values = {
        float(item.frequency_hz): complex(item.real_m3_s, item.imag_m3_s)
        for item in excitation.samples
    }
    pressure_minus = []
    for frequency, transfer_plus in zip(
        frequencies,
        physical_transfer_plus,
        strict=True,
    ):
        transfer_minus = transfer_plus.conjugate()
        pressure_minus.append(transfer_minus * q_values[frequency])

    receiver = candidate.receivers[0]
    return {
        'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        'quantity_type': 'complex_pressure',
        'complex_representation': {
            'form': 'cartesian_real_imag',
            'phasor_convention': R130_PHASOR_CONVENTION,
            'analysis_fourier_kernel': R130_ANALYSIS_FOURIER_KERNEL,
        },
        'receiver_identity_order': [
            {
                'receiver_id': receiver.receiver_id,
                'entity_id': receiver.entity_id,
                'position_m': list(receiver.position_m),
            }
        ],
        'frequency_axis_hz': list(frequencies),
        'time_sampling': {
            'time_step_s': 1.0e-4,
            'sample_count': 1024,
            'finite_record_interval': '[0,T)',
            'requested_duration_s': 0.1,
        },
        'units': 'Pa',
        'reference': R130_PRESSURE_REFERENCE,
        'candidate_execution_input_id': candidate.execution_input_id,
        'candidate_execution_input_sha256': candidate.semantic_sha256,
        'source_authority': {
            'r110_compiled_source_sha256': (
                candidate.r110_compiled_source_sha256
            ),
            'wave_excitation_binding_sha256': (
                candidate.wave_excitation_binding_sha256
            ),
            'wave_excitation_sha256': candidate.wave_excitation_sha256,
        },
        'pressure_real_pa': [[item.real for item in pressure_minus]],
        'pressure_imag_pa': [[item.imag for item in pressure_minus]],
    }


def _r150_response(
    *,
    frequencies: tuple[float, ...],
    values: tuple[complex, ...],
    source_entity_id: str = 'source-1',
    receiver_id: str = 'receiver-1',
    receiver_entity_id: str = 'receiver-entity-1',
    path_label: str = 'direct',
    capability: str = 'COMPLEX_SUPPORTED',
) -> DeterministicPathFrequencyResponseArtifact:
    execution_ref = _ref(f'r150-execution-{path_label}')
    geometry_ref = ExactExternalAuthorityRef(
        authority_id='r120-compiled-geometry:r160-test',
        authority_version='1',
        semantic_hash_sha256=_hash('geometry'),
    )
    r110_ref = ExactExternalAuthorityRef(
        authority_id='r110-source:r160-test',
        authority_version='1',
        semantic_hash_sha256=_hash('r110-source'),
    )
    if capability == 'COMPLEX_SUPPORTED':
        samples = tuple(
            PathFrequencyResponseSample(
                frequency_hz=frequency,
                propagation_phase_rad=0.0,
                geometric_spreading_per_m=1.0,
                magnitude_pa_per_m3_s=abs(value),
                phase_rad=cmath.phase(value),
                complex_real_pa_per_m3_s=value.real,
                complex_imag_pa_per_m3_s=value.imag,
            )
            for frequency, value in zip(frequencies, values, strict=True)
        )
        reasons: tuple[str, ...] = ()
    elif capability == 'MAGNITUDE_ONLY':
        samples = tuple(
            PathFrequencyResponseSample(
                frequency_hz=frequency,
                propagation_phase_rad=0.0,
                geometric_spreading_per_m=1.0,
                magnitude_pa_per_m3_s=abs(value),
            )
            for frequency, value in zip(frequencies, values, strict=True)
        )
        reasons = ()
    else:
        samples = ()
        reasons = ('synthetic unsupported path',)

    path_hash = _hash(f'path-{path_label}')
    path_artifact_hash = _hash('shared-r150-path-artifact')
    core = {
        'schema_version': 1,
        'authority_version': 'r150-complex-path-response-1',
        'deterministic_path_artifact_id': (
            f'deterministic-path-artifact:{path_artifact_hash}'
        ),
        'deterministic_path_artifact_sha256': path_artifact_hash,
        'execution_input_ref': execution_ref.model_dump(mode='json'),
        'deterministic_path_id': f'deterministic-acoustic-path:{path_hash}',
        'deterministic_path_sha256': path_hash,
        'source_entity_id': source_entity_id,
        'receiver_id': receiver_id,
        'receiver_entity_id': receiver_entity_id,
        'ordered_surface_interactions': (
            () if path_label == 'direct' else (f'surface:{path_label}',)
        ),
        'ordered_portal_interactions': (),
        'path_length_m': 1.0 if path_label == 'direct' else 2.0,
        'quantity': 'complex_acoustic_pressure_per_volume_velocity',
        'unit': 'Pa/(m3/s)',
        'source_normalization': 'unit_volume_velocity_m3_s',
        'phasor_convention': COMMON_PHASOR_CONVENTION,
        'time_origin': 'source_t0',
        'sound_speed_m_s': 343.0,
        'density_kg_m3': 1.2,
        'exact_frequency_grid_hz': list(frequencies),
        'valid_frequency_domain': _domain(frequencies).model_dump(mode='json'),
        'capability': capability,
        'unsupported_reasons': list(reasons),
        'samples': [item.model_dump(mode='json') for item in samples],
        'dependency_refs': [
            item.model_dump(mode='json')
            for item in sorted(
                (execution_ref, geometry_ref, r110_ref),
                key=lambda ref: (
                    ref.authority_id,
                    ref.authority_version,
                    ref.semantic_hash_sha256,
                ),
            )
        ],
    }
    digest = _digest(core)
    return DeterministicPathFrequencyResponseArtifact(
        artifact_id=f'r150-path-frequency-response:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _bundle(
    *,
    physical_transfer_plus: tuple[complex, ...],
    responses: tuple[DeterministicPathFrequencyResponseArtifact, ...] | None = None,
):
    frequencies = (40.0, 60.0, 80.0)
    q_values = (1.0e-4 + 0.0j, 7.0e-5 + 2.0e-5j, 0.0 + 1.0e-4j)
    excitation = _excitation(frequencies, q_values)
    candidate = _candidate_input(
        frequencies=frequencies,
        excitation=excitation,
    )
    payload = _r130_payload(
        candidate=candidate,
        excitation=excitation,
        frequencies=frequencies,
        physical_transfer_plus=physical_transfer_plus,
    )
    payload_hash = _digest(payload)
    artifact_ref = ExactExternalAuthorityRef(
        authority_id=f'acoustic-solver-artifact:{payload_hash}',
        authority_version=COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        semantic_hash_sha256=payload_hash,
    )
    result = _solver_result(
        candidate=candidate,
        artifact_ref=artifact_ref,
        frequencies=frequencies,
    )
    if responses is None:
        responses = (
            _r150_response(
                frequencies=frequencies,
                values=physical_transfer_plus,
            ),
        )
    normalization = build_hybrid_convention_normalization_authority()
    spec = build_numerical_hybrid_composition_spec(
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=excitation,
        r150_responses=responses,
        receiver_id='receiver-1',
        exact_frequency_grid_hz=frequencies,
        transition_start_hz=40.0,
        transition_end_hz=80.0,
        normalization_authority=normalization,
    )
    return {
        'frequencies': frequencies,
        'excitation': excitation,
        'candidate': candidate,
        'result': result,
        'payload': payload,
        'responses': responses,
        'normalization': normalization,
        'spec': spec,
    }


def _compose(bundle):
    return compose_numerical_hybrid_response(
        spec=bundle['spec'],
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        normalization_authority=bundle['normalization'],
    )


def _stitch_fixture() -> dict[str, object]:
    path = (
        Path(__file__).parent
        / 'fixtures'
        / 'r160_frequency_grid_stitch_fixture.json'
    )
    return json.loads(path.read_text(encoding='utf-8'))


def _fixture_transfer(
    frequency_hz: float,
    transfer: dict[str, float],
) -> complex:
    return complex(
        transfer['real_intercept']
        + transfer['real_slope_per_hz'] * frequency_hz,
        transfer['imag_intercept']
        + transfer['imag_slope_per_hz'] * frequency_hz,
    )


def _unequal_grid_bundle(case: dict[str, object]):
    fixture = _stitch_fixture()
    transfer = fixture['transfer']
    assert isinstance(transfer, dict)
    wave_grid = tuple(float(item) for item in case['wave_grid_hz'])
    ga_grid = tuple(float(item) for item in case['ga_grid_hz'])
    output_grid = tuple(float(item) for item in case['output_grid_hz'])
    wave_values = tuple(
        _fixture_transfer(frequency, transfer) for frequency in wave_grid
    )
    ga_values = tuple(
        _fixture_transfer(frequency, transfer) for frequency in ga_grid
    )
    q_values = tuple(
        complex(1.0e-4 + index * 1.0e-6, index * 5.0e-7)
        for index, _ in enumerate(wave_grid)
    )
    excitation = _excitation(wave_grid, q_values)
    candidate = _candidate_input(
        frequencies=wave_grid,
        excitation=excitation,
    )
    payload = _r130_payload(
        candidate=candidate,
        excitation=excitation,
        frequencies=wave_grid,
        physical_transfer_plus=wave_values,
    )
    payload_hash = _digest(payload)
    artifact_ref = ExactExternalAuthorityRef(
        authority_id=f'acoustic-solver-artifact:{payload_hash}',
        authority_version=COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        semantic_hash_sha256=payload_hash,
    )
    result = _solver_result(
        candidate=candidate,
        artifact_ref=artifact_ref,
        frequencies=wave_grid,
    )
    responses = (
        _r150_response(
            frequencies=ga_grid,
            values=ga_values,
        ),
    )
    normalization = build_hybrid_convention_normalization_authority()
    spec = build_numerical_hybrid_composition_spec(
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=excitation,
        r150_responses=responses,
        receiver_id='receiver-1',
        exact_frequency_grid_hz=output_grid,
        transition_start_hz=float(case['overlap_lower_hz']),
        transition_end_hz=float(case['overlap_upper_hz']),
        normalization_authority=normalization,
        reconciliation_method='cartesian_linear_v1',
    )
    return {
        'fixture': fixture,
        'transfer': transfer,
        'wave_grid': wave_grid,
        'ga_grid': ga_grid,
        'output_grid': output_grid,
        'excitation': excitation,
        'candidate': candidate,
        'payload': payload,
        'result': result,
        'responses': responses,
        'normalization': normalization,
        'spec': spec,
    }


def test_identity_fixture_is_invariant_under_complementary_crossover_weights() -> None:
    expected = (
        2.0 + 3.0j,
        -1.5 + 0.25j,
        0.2 - 4.0j,
    )
    output = _compose(_bundle(physical_transfer_plus=expected))

    assert output.capability_state == 'COMPLEX_SUPPORTED'
    assert output.grid_reconciliation.reconciliation_method == 'exact_bin_identity_v1'
    assert tuple(
        (sample.low_weight, sample.high_weight) for sample in output.samples
    ) == ((1.0, 0.0), (0.5, 0.5), (0.0, 1.0))
    for sample, value in zip(output.samples, expected, strict=True):
        assert complex(
            sample.complex_real_pa_per_m3_s,
            sample.complex_imag_pa_per_m3_s,
        ) == pytest.approx(value)
        assert complex(
            sample.wave_complex_real_pa_per_m3_s,
            sample.wave_complex_imag_pa_per_m3_s,
        ) == pytest.approx(value)
        assert complex(
            sample.ga_complex_real_pa_per_m3_s,
            sample.ga_complex_imag_pa_per_m3_s,
        ) == pytest.approx(value)


def test_opposite_phasor_is_explicitly_converted_and_implicit_mismatch_fails() -> None:
    physical = (
        1.0 + 2.0j,
        -3.0 + 4.0j,
        5.0 - 6.0j,
    )
    bundle = _bundle(physical_transfer_plus=physical)
    output = _compose(bundle)

    assert convert_complex_phasor(
        (1.0 - 2.0j),
        input_convention=R130_PHASOR_CONVENTION,
        output_convention=COMMON_PHASOR_CONVENTION,
    ) == 1.0 + 2.0j
    for sample, expected in zip(output.samples, physical, strict=True):
        assert complex(
            sample.wave_complex_real_pa_per_m3_s,
            sample.wave_complex_imag_pa_per_m3_s,
        ) == pytest.approx(expected)

    tampered = json.loads(json.dumps(bundle['payload']))
    tampered['complex_representation']['phasor_convention'] = (
        COMMON_PHASOR_CONVENTION
    )
    tampered_hash = _digest(tampered)
    tampered_ref = ExactExternalAuthorityRef(
        authority_id=f'acoustic-solver-artifact:{tampered_hash}',
        authority_version=COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        semantic_hash_sha256=tampered_hash,
    )
    tampered_result = _solver_result(
        candidate=bundle['candidate'],
        artifact_ref=tampered_ref,
        frequencies=bundle['frequencies'],
    )
    with pytest.raises(ValueError, match='complex/Fourier convention'):
        build_numerical_hybrid_composition_spec(
            r130_result=tampered_result,
            r130_artifact_payload=tampered,
            r130_candidate_input=bundle['candidate'],
            wave_excitation=bundle['excitation'],
            r150_responses=bundle['responses'],
            receiver_id='receiver-1',
            exact_frequency_grid_hz=bundle['frequencies'],
            transition_start_hz=40.0,
            transition_end_hz=80.0,
            normalization_authority=bundle['normalization'],
        )


def test_no_double_count_transition_does_not_double_identical_signal() -> None:
    physical = (3.0 + 0.0j, 3.0 + 4.0j, -3.0 + 0.0j)
    output = _compose(_bundle(physical_transfer_plus=physical))

    midpoint = output.samples[1]
    assert midpoint.low_weight == pytest.approx(0.5)
    assert midpoint.high_weight == pytest.approx(0.5)
    assert midpoint.magnitude_pa_per_m3_s == pytest.approx(5.0)
    assert midpoint.magnitude_pa_per_m3_s != pytest.approx(10.0)


def test_coherent_r150_path_sum_matches_independent_complex_sum() -> None:
    frequencies = (40.0, 60.0, 80.0)
    direct = (1.0 + 1.0j, 2.0 + 0.0j, 1.0 - 1.0j)
    reflected = (0.5 - 0.25j, -0.25 + 0.5j, 1.25 + 0.75j)
    responses = (
        _r150_response(
            frequencies=frequencies,
            values=direct,
            path_label='direct',
        ),
        _r150_response(
            frequencies=frequencies,
            values=reflected,
            path_label='reflection',
        ),
    )
    bundle = _bundle(
        physical_transfer_plus=tuple(
            a + b for a, b in zip(direct, reflected, strict=True)
        ),
        responses=responses,
    )
    aggregate = aggregate_r150_complex_paths(
        spec=bundle['spec'],
        responses=responses,
    )

    assert aggregate.capability_state == 'COMPLEX_SUPPORTED'
    for sample, first, second in zip(
        aggregate.samples,
        direct,
        reflected,
        strict=True,
    ):
        assert complex(
            sample.complex_real_pa_per_m3_s,
            sample.complex_imag_pa_per_m3_s,
        ) == pytest.approx(first + second)


def test_magnitude_only_required_path_fails_closed_without_fabricated_complex_output() -> None:
    frequencies = (40.0, 60.0, 80.0)
    physical = (1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j)
    responses = (
        _r150_response(
            frequencies=frequencies,
            values=physical,
            path_label='direct',
        ),
        _r150_response(
            frequencies=frequencies,
            values=(0.5 + 0.0j, 0.5 + 0.0j, 0.5 + 0.0j),
            path_label='magnitude-only-reflection',
            capability='MAGNITUDE_ONLY',
        ),
    )
    output = _compose(
        _bundle(
            physical_transfer_plus=physical,
            responses=responses,
        )
    )

    assert output.capability_state == 'UNSUPPORTED'
    assert output.failure_codes == (
        HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
    )
    assert output.samples == ()
    assert output.unsupported_reasons
    assert 'MAGNITUDE_ONLY' in output.unsupported_reasons[0]



def test_r130_payload_must_match_exact_result_artifact_hash() -> None:
    bundle = _bundle(
        physical_transfer_plus=(
            1.0 + 0.0j,
            2.0 + 0.0j,
            3.0 + 0.0j,
        )
    )
    tampered = json.loads(json.dumps(bundle['payload']))
    tampered['pressure_real_pa'][0][0] += 0.125
    with pytest.raises(HybridNumericalCompositionError) as error:
        build_numerical_hybrid_composition_spec(
            r130_result=bundle['result'],
            r130_artifact_payload=tampered,
            r130_candidate_input=bundle['candidate'],
            wave_excitation=bundle['excitation'],
            r150_responses=bundle['responses'],
            receiver_id='receiver-1',
            exact_frequency_grid_hz=bundle['frequencies'],
            transition_start_hz=40.0,
            transition_end_hz=80.0,
            normalization_authority=bundle['normalization'],
        )
    assert error.value.code == HybridNumericalFailureCode.ARTIFACT_HASH_MISMATCH


def test_duplicate_r150_deterministic_path_identity_is_rejected() -> None:
    frequencies = (40.0, 60.0, 80.0)
    values = (1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j)
    first = _r150_response(
        frequencies=frequencies,
        values=values,
        path_label='direct',
    )
    duplicate = _r150_response(
        frequencies=frequencies,
        values=(1.1 + 0.0j, 2.1 + 0.0j, 3.1 + 0.0j),
        path_label='direct',
    )
    assert duplicate.deterministic_path_id == first.deterministic_path_id
    assert duplicate.artifact_id != first.artifact_id
    bundle = _bundle(
        physical_transfer_plus=values,
        responses=(first, duplicate),
    )
    with pytest.raises(ValueError, match='duplicate deterministic path identity'):
        aggregate_r150_complex_paths(
            spec=bundle['spec'],
            responses=(first, duplicate),
        )


def test_exact_frequency_grid_mismatch_rejects_nearest_neighbor_guessing() -> None:
    bundle = _bundle(
        physical_transfer_plus=(
            1.0 + 0.0j,
            2.0 + 0.0j,
            3.0 + 0.0j,
        )
    )
    with pytest.raises(HybridNumericalCompositionError) as error:
        build_numerical_hybrid_composition_spec(
            r130_result=bundle['result'],
            r130_artifact_payload=bundle['payload'],
            r130_candidate_input=bundle['candidate'],
            wave_excitation=bundle['excitation'],
            r150_responses=bundle['responses'],
            receiver_id='receiver-1',
            exact_frequency_grid_hz=(40.0, 50.0, 80.0),
            transition_start_hz=40.0,
            transition_end_hz=80.0,
            normalization_authority=bundle['normalization'],
        )
    assert error.value.code == HybridNumericalFailureCode.INVALID_GRID


@pytest.mark.parametrize(
    'case_name',
    ('unequal_regular', 'unequal_irregular'),
)
def test_frequency_grid_stitch_fixture_reconstructs_smooth_complex_response(
    case_name: str,
) -> None:
    fixture = _stitch_fixture()
    cases = {
        item['name']: item
        for item in fixture['cases']
    }
    bundle = _unequal_grid_bundle(cases[case_name])
    output = _compose(bundle)
    transfer = bundle['transfer']

    assert output.capability_state == 'COMPLEX_SUPPORTED'
    assert output.grid_reconciliation.reconciliation_method == 'cartesian_linear_v1'
    assert output.grid_reconciliation.extrapolation_policy == 'forbidden'
    assert output.time_origin == 'source_t0'
    assert output.common_phasor_convention == COMMON_PHASOR_CONVENTION

    phases = []
    for sample in output.samples:
        expected = _fixture_transfer(sample.frequency_hz, transfer)
        wave = complex(
            sample.wave_complex_real_pa_per_m3_s,
            sample.wave_complex_imag_pa_per_m3_s,
        )
        ga = complex(
            sample.ga_complex_real_pa_per_m3_s,
            sample.ga_complex_imag_pa_per_m3_s,
        )
        hybrid = complex(
            sample.complex_real_pa_per_m3_s,
            sample.complex_imag_pa_per_m3_s,
        )
        assert wave == pytest.approx(expected)
        assert ga == pytest.approx(expected)
        assert hybrid == pytest.approx(expected)
        assert sample.low_weight + sample.high_weight == pytest.approx(1.0)
        phases.append(sample.phase_rad)

    for previous, current in zip(phases, phases[1:]):
        wrapped_delta = cmath.phase(cmath.exp(1j * (current - previous)))
        assert abs(wrapped_delta) < 0.1

    lower = output.crossover_configuration.overlap_lower_hz
    upper = output.crossover_configuration.overlap_upper_hz
    by_frequency = {sample.frequency_hz: sample for sample in output.samples}
    if lower in by_frequency:
        assert by_frequency[lower].low_weight == 1.0
        assert by_frequency[lower].high_weight == 0.0
    if upper in by_frequency:
        assert by_frequency[upper].low_weight == 0.0
        assert by_frequency[upper].high_weight == 1.0


def test_grid_reconciliation_failures_are_typed_and_fail_closed() -> None:
    common = {
        'original_wave_frequency_grid_hz': (40.0, 60.0, 80.0),
        'original_ga_frequency_grid_hz': (40.0, 60.0, 80.0),
        'requested_output_frequency_grid_hz': (40.0, 60.0, 80.0),
    }

    with pytest.raises(HybridNumericalCompositionError) as unsorted:
        build_frequency_grid_reconciliation_authority(
            **{
                **common,
                'original_wave_frequency_grid_hz': (40.0, 80.0, 60.0),
            }
        )
    assert unsorted.value.code == HybridNumericalFailureCode.NON_MONOTONIC_GRID

    with pytest.raises(HybridNumericalCompositionError) as duplicate:
        build_frequency_grid_reconciliation_authority(
            **{
                **common,
                'original_ga_frequency_grid_hz': (40.0, 40.0, 80.0),
            }
        )
    assert duplicate.value.code == HybridNumericalFailureCode.DUPLICATE_FREQUENCY

    with pytest.raises(HybridNumericalCompositionError) as outside:
        build_frequency_grid_reconciliation_authority(
            **{
                **common,
                'requested_output_frequency_grid_hz': (30.0, 40.0, 60.0),
            },
            reconciliation_method='cartesian_linear_v1',
        )
    assert outside.value.code == HybridNumericalFailureCode.OUT_OF_VALID_BAND

    with pytest.raises(HybridNumericalCompositionError) as phase_method:
        build_frequency_grid_reconciliation_authority(
            **common,
            reconciliation_method='magnitude_unwrapped_phase_linear_v1',
        )
    assert (
        phase_method.value.code
        == HybridNumericalFailureCode.PHASE_INTERPOLATION_UNSUPPORTED
    )

    with pytest.raises(HybridNumericalCompositionError) as overlap:
        build_hybrid_crossover_configuration_authority(
            overlap_lower_hz=30.0,
            overlap_upper_hz=70.0,
            wave_validity_band_hz=(40.0, 80.0),
            ga_validity_band_hz=(40.0, 80.0),
        )
    assert overlap.value.code == HybridNumericalFailureCode.OVERLAP_INVALID


def test_reconciliation_config_changes_spec_and_artifact_identity_only() -> None:
    bundle = _bundle(
        physical_transfer_plus=(
            1.0 + 0.5j,
            2.0 + 0.75j,
            3.0 + 1.0j,
        )
    )
    baseline = _compose(bundle)
    alternate_spec = build_numerical_hybrid_composition_spec(
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        receiver_id='receiver-1',
        exact_frequency_grid_hz=bundle['frequencies'],
        transition_start_hz=40.0,
        transition_end_hz=80.0,
        normalization_authority=bundle['normalization'],
        frequency_tolerance_hz=1.0e-9,
    )
    alternate = compose_numerical_hybrid_response(
        spec=alternate_spec,
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        normalization_authority=bundle['normalization'],
    )

    assert alternate_spec.composition_spec_id != bundle['spec'].composition_spec_id
    assert (
        alternate_spec.grid_reconciliation.semantic_sha256
        != bundle['spec'].grid_reconciliation.semantic_sha256
    )
    assert alternate.artifact_id != baseline.artifact_id
    assert tuple(
        complex(
            item.complex_real_pa_per_m3_s,
            item.complex_imag_pa_per_m3_s,
        )
        for item in alternate.samples
    ) == pytest.approx(
        tuple(
            complex(
                item.complex_real_pa_per_m3_s,
                item.complex_imag_pa_per_m3_s,
            )
            for item in baseline.samples
        )
    )


def test_save_reopen_exact_and_r130_r150_composition_stale_rejection(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        physical_transfer_plus=(
            1.0 + 1.0j,
            2.0 + 2.0j,
            3.0 + 3.0j,
        )
    )
    output = _compose(bundle)

    results = {bundle['result'].result_id: bundle['result']}
    payloads = {
        bundle['spec'].r130_complex_pressure_artifact_ref.authority_id: (
            bundle['payload']
        )
    }
    candidates = {
        bundle['candidate'].execution_input_id: bundle['candidate']
    }
    excitations = {
        bundle['excitation'].excitation_id: bundle['excitation']
    }
    responses = {
        item.artifact_id: item for item in bundle['responses']
    }
    specs = {
        bundle['spec'].composition_spec_id: bundle['spec']
    }
    normalizations = {
        bundle['normalization'].authority_id: bundle['normalization']
    }

    repository = CadNumericalHybridResponseRepository(
        SceneRepository(tmp_path / 'r160.sqlite3'),
        wave_result_resolver=lambda result_id: results.get(result_id),
        wave_artifact_payload_resolver=lambda ref: payloads.get(
            ref.authority_id
        ),
        candidate_input_resolver=lambda input_id: candidates.get(input_id),
        wave_excitation_resolver=lambda excitation_id: excitations.get(
            excitation_id
        ),
        r150_response_resolver=lambda artifact_id: responses.get(artifact_id),
        composition_spec_resolver=lambda spec_id: specs.get(spec_id),
        convention_authority_resolver=lambda ref: normalizations.get(
            ref.authority_id
        ),
    )
    repository.save(output)
    reopened = repository.get(output.artifact_id)
    assert reopened == output
    assert reopened is not None
    assert reopened.grid_reconciliation == output.grid_reconciliation
    assert reopened.crossover_configuration == output.crossover_configuration

    saved_result = results.pop(bundle['result'].result_id)
    with pytest.raises(ValueError, match='R130 result dependency'):
        repository.get(output.artifact_id)
    results[saved_result.result_id] = saved_result

    response = bundle['responses'][0]
    responses.pop(response.artifact_id)
    with pytest.raises(ValueError, match='R150 response'):
        repository.get(output.artifact_id)
    responses[response.artifact_id] = response

    saved_spec = specs.pop(bundle['spec'].composition_spec_id)
    with pytest.raises(ValueError, match='composition authority'):
        repository.get(output.artifact_id)
    specs[saved_spec.composition_spec_id] = saved_spec
    assert repository.get(output.artifact_id) == output


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
        definition_id='equipment:r160-integration-source',
        version='1',
        semantic_sha256=_hash('r160-integration-equipment'),
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
        authority_id='r110-source:r160-integration',
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


def test_repository_native_actual_r130_artifact_path_and_actual_r150_response_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = r130_fixture(
        tmp_path / 'r130-fixture',
        tmp_path / 'unused-pffdtd-upstream',
    )
    candidate, _ = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    frequencies = tuple(fixture['configuration'].frequency_samples_hz)
    assert frequencies == (40.0, 80.0)

    numerical = CandidateNumericalOutput(
        receiver_ids=tuple(item.receiver_id for item in candidate.receivers),
        frequency_hz=frequencies,
        pressure_real_pa=((1.0, -2.0),),
        pressure_imag_pa=((0.5, 1.0),),
        raw_solver_asset_sha256=_hash('mocked-r130-raw-solver-asset'),
        raw_solver_asset_name='mocked-sim-outs.h5',
        time_step_s=1.0e-4,
        time_step_count=128,
        grid_shape=(8, 8, 8),
        sound_speed_m_s=float(fixture['environment'].sound_speed_m_s),
        compile_seconds=0.0,
        solve_seconds=0.0,
        postprocess_seconds=0.0,
        compatibility_patch={'repository_native_fixture': True},
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

    receiver_binding = candidate.receivers[0]
    r150_response = _actual_r150_direct_response(
        source_entity_id=candidate.source_entity_id,
        r110_sha256=candidate.r110_compiled_source_sha256,
        receiver_id=receiver_binding.receiver_id,
        receiver_entity_id=receiver_binding.entity_id,
        receiver_position=receiver_binding.position_m,
        frequencies=frequencies,
        sound_speed_m_s=float(fixture['environment'].sound_speed_m_s),
        compiled_geometry_id=candidate.compiled_geometry_id,
        compiled_geometry_sha256=candidate.compiled_geometry_sha256,
    )
    normalization = build_hybrid_convention_normalization_authority()
    spec = build_numerical_hybrid_composition_spec(
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=fixture['excitation'],
        r150_responses=(r150_response,),
        receiver_id=receiver_binding.receiver_id,
        exact_frequency_grid_hz=frequencies,
        transition_start_hz=40.0,
        transition_end_hz=80.0,
        normalization_authority=normalization,
    )
    output = compose_numerical_hybrid_response(
        spec=spec,
        r130_result=result,
        r130_artifact_payload=payload,
        r130_candidate_input=candidate,
        wave_excitation=fixture['excitation'],
        r150_responses=(r150_response,),
        normalization_authority=normalization,
    )

    assert payload['schema_version'] == COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION
    assert output.capability_state == 'COMPLEX_SUPPORTED'
    assert output.exact_r130_result.result_id == result.result_id
    assert output.exact_r150_response_refs == (
        r150_response.as_external_ref(),
    )
    assert output.samples[0].low_weight == 1.0
    assert output.samples[-1].high_weight == 1.0
    assert output.samples[-1].ga_complex_real_pa_per_m3_s == pytest.approx(
        r150_response.samples[-1].complex_real_pa_per_m3_s
    )
    assert output.samples[-1].ga_complex_imag_pa_per_m3_s == pytest.approx(
        r150_response.samples[-1].complex_imag_pa_per_m3_s
    )


# ---------------------------------------------------------------------------
# #932 — Air-state authority unification across snapshot and R130
# ---------------------------------------------------------------------------


def test_r130_candidate_rejects_air_state_from_unrelated_authority(
    tmp_path: Path,
) -> None:
    fixture = r130_fixture(
        tmp_path / 'r130-air-state',
        tmp_path / 'unused-pffdtd-upstream',
    )
    store = fixture['store']
    snapshot_repository = fixture['snapshot_repository']
    configuration = fixture['configuration']

    # The snapshot environment authority now declares an air-state density of
    # 9.9 while the candidate configuration still binds density 1.2 from its
    # own (unrelated) density authority — the mix must fail closed.
    air_density_ref = store.put_json(
        'r130-air-state-density',
        '1',
        {'quantity': 'air_density_kg_m3', 'value': 9.9},
    )
    environment = fixture['environment'].model_copy(
        update={
            'air_density_kg_m3': 9.9,
            'air_density_source_authority': air_density_ref,
        }
    )
    def _scalar(ref, quantity):
        if store.resolve(ref) is None:
            return None
        payload = store.read_payload(ref)
        if not isinstance(payload, dict) or payload.get('quantity') != quantity:
            return None
        return float(payload['value'])

    resolvers = fixture['snapshot_authority_resolvers']._replace(
        environment=lambda ref: (
            environment
            if ref == environment.authority
            and store.resolve(ref) is not None
            else None
        ),
        air_density_source=lambda ref: _scalar(ref, 'air_density_kg_m3'),
    )
    snapshot_repository.authority_resolvers = resolvers
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fixture['revision'],
        compiled_geometry=fixture['compiled'],
        source_models=(fixture['source'],),
        receivers=fixture['snapshot'].receivers,
        requested_frequency_domain=fixture['snapshot'].requested_frequency_domain,
        requested_observables=('complex_pressure',),
        system_variant=fixture['variant'],
        environment=environment,
        valid_frequency_domain=fixture['snapshot'].requested_frequency_domain,
        valid_frequency_domain_authority_ref=(
            fixture['snapshot'].valid_frequency_domain_authority_ref
        ),
        wave_source_excitation_bindings=(
            fixture['snapshot'].wave_source_excitation_bindings
        ),
    )
    snapshot_repository.save_snapshot(snapshot)
    request = fixture['request']
    band = snapshot.requested_frequency_domain
    air_request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=request.model_solver_role_id,
        requested_frequency_domain=band,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=(
            request.numerical_fidelity_policy_ref
        ),
    )
    snapshot_repository.save_prediction_request(air_request)
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=air_request,
        adapter=fixture['descriptor'],
        solver_configuration_ref=configuration.as_external_ref(),
        numerical_fidelity_policy=fixture['fidelity_policy'],
    )
    assert dispatch.state == 'READY'
    fixture['dispatch_repository'].save_dispatch(dispatch)

    with pytest.raises(
        CandidateWaveExecutionError,
        match='density must match the exact environment air-state authority',
    ):
        fixture['executor'].compile_input(
            dispatch_binding_id=dispatch.binding_id,
            configuration=configuration,
        )
