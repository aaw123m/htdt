"""#966: R130 source-model compatibility authority and R160 wiring.

The wave solver collapses every loudspeaker to one equivalent monopole.
These tests cover the evaluated authority that states whether that
collapse is native, supported, unpinned or falsified — and that an R160
composition now honestly records the evaluation instead of implying
source-model compatibility it never proved.
"""

from __future__ import annotations

from hashlib import sha256
import json

import pytest

from htdt.cad_bass_management import FrequencyBand
from htdt.cad_hybrid_numerical_composition import (
    build_numerical_hybrid_composition_spec,
    compose_numerical_hybrid_response,
)
from htdt.cad_multi_radiator_source import (
    MultiRadiatorSourceModel,
    SourceRadiatorElement,
    SourceReferencePoints,
    SourceTransferEdge,
    build_multi_radiator_source_model,
)
from htdt.cad_scene import Offset3
from htdt.cad_wave_excitation import WaveSourceExcitationBinding
from htdt.cad_wave_source_model import (
    WaveSourceModelCompatibility,
    evaluate_wave_source_model_compatibility,
)

from test_cad_hybrid_numerical_composition import (
    _bundle,
    _candidate_input,
    _digest,
    _excitation,
    _hash,
    _r130_payload,
    _r150_response,
    _solver_result,
)


def _binding(excitation) -> WaveSourceExcitationBinding:
    probe = WaveSourceExcitationBinding.model_construct(
        authority_version='r110-wave-source-binding-1',
        r110_compiled_source_sha256=_hash('r110-source'),
        source_entity_id='source-1',
        equipment_definition_id=excitation.definition_id,
        equipment_definition_version=excitation.definition_version,
        equipment_definition_sha256=excitation.definition_sha256,
        excitation_id=excitation.excitation_id,
        excitation_authority_version=excitation.authority_version,
        excitation_semantic_sha256=excitation.semantic_sha256,
        valid_frequency_domain=excitation.valid_frequency_domain,
        source_model=excitation.excitation_model,
    )
    digest = _digest(probe.semantic_payload())
    return WaveSourceExcitationBinding(
        binding_id=f'wave-source-excitation-binding:{digest}',
        semantic_sha256=digest,
        **probe.model_dump(mode='python'),
    )


def _candidate_with_binding(*, excitation, binding):
    candidate = _candidate_input(
        frequencies=(40.0, 60.0, 80.0),
        excitation=excitation,
    )
    core = candidate.semantic_payload()
    core['wave_excitation_binding_id'] = binding.binding_id
    core['wave_excitation_binding_sha256'] = binding.semantic_sha256
    digest = _digest(core)
    from htdt.cad_candidate_wave_execution import CandidateWaveExecutionInput

    return CandidateWaveExecutionInput(
        execution_input_id=f'candidate-wave-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _element(
    element_id: str,
    *,
    evidence: str = 'complex',
    band: FrequencyBand | None,
) -> SourceRadiatorElement:
    return SourceRadiatorElement(
        element_id=element_id,
        kind='driver',
        local_position_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.0),
        valid_band=band,
        transfer_evidence=evidence,
        transfer_dataset_ref=(
            f'dataset:{element_id}' if evidence != 'none' else None
        ),
    )


def _two_element_model(
    definition_sha256: str,
    *,
    pinned_point: bool = True,
) -> MultiRadiatorSourceModel:
    reference_points = SourceReferencePoints(
        geometric_reference_m=Offset3(),
        dataset_phase_origin_m=Offset3(),
        dataset_phase_origin_ref='dataset:phase-origin',
        effective_acoustic_center_m=(
            Offset3(x_m=0.0, y_m=0.02, z_m=0.0) if pinned_point else None
        ),
        effective_acoustic_center_ref=(
            'dataset:acoustic-center' if pinned_point else None
        ),
        solver_equivalent_point_m=(
            Offset3(x_m=0.0, y_m=0.02, z_m=0.0) if pinned_point else None
        ),
    )
    return build_multi_radiator_source_model(
        model_id='model:two-way',
        version='1',
        equipment_definition_id='equipment:r160-synthetic-source',
        equipment_definition_version='1',
        equipment_definition_sha256=definition_sha256,
        elements=(
            _element(
                'woofer', band=FrequencyBand(low_hz=30.0, high_hz=200.0)
            ),
            _element(
                'tweeter', band=FrequencyBand(low_hz=2000.0, high_hz=20000.0)
            ),
        ),
        transfer_edges=(
            SourceTransferEdge(
                element_id='woofer', polarity='normal', gain_db=0.0
            ),
            SourceTransferEdge(
                element_id='tweeter', polarity='normal', gain_db=0.0
            ),
        ),
        reference_points=reference_points,
    )


def test_unverified_without_source_model() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    evaluated = evaluate_wave_source_model_compatibility(
        binding=binding, excitation=excitation
    )
    assert evaluated.collapse_state == 'unverified'
    assert evaluated.compatibility_state == 'unverified'
    assert evaluated.multi_radiator_model_sha256 is None


def test_single_element_model_is_monopole_native() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    model = build_multi_radiator_source_model(
        model_id='model:single',
        version='1',
        equipment_definition_id='equipment:r160-synthetic-source',
        equipment_definition_version='1',
        equipment_definition_sha256=excitation.definition_sha256,
        elements=(
            _element(
                'driver', band=FrequencyBand(low_hz=30.0, high_hz=200.0)
            ),
        ),
        reference_points=SourceReferencePoints(
            geometric_reference_m=Offset3()
        ),
    )
    evaluated = evaluate_wave_source_model_compatibility(
        binding=binding,
        excitation=excitation,
        multi_radiator_model=model,
    )
    assert evaluated.collapse_state == 'monopole_native'
    assert evaluated.compatibility_state == 'compatible'
    assert evaluated.point_basis == 'equipment_acoustic_reference'


def test_coherent_multi_radiator_collapse_supported() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    model = _two_element_model(excitation.definition_sha256)
    evaluated = evaluate_wave_source_model_compatibility(
        binding=binding,
        excitation=excitation,
        multi_radiator_model=model,
    )
    assert evaluated.collapse_state == 'collapse_supported'
    assert evaluated.compatibility_state == 'compatible'
    assert evaluated.point_basis == 'solver_equivalent_point'
    assert evaluated.coherence_evaluation_id is not None


def test_coherent_collapse_without_evidenced_point_is_limitation() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    model = _two_element_model(
        excitation.definition_sha256, pinned_point=False
    )
    evaluated = evaluate_wave_source_model_compatibility(
        binding=binding,
        excitation=excitation,
        multi_radiator_model=model,
    )
    assert evaluated.collapse_state == 'collapse_supported_unpinned'
    assert evaluated.compatibility_state == 'compatible_with_limitations'


def test_non_summable_model_falsifies_collapse() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    model = build_multi_radiator_source_model(
        model_id='model:mixed',
        version='1',
        equipment_definition_id='equipment:r160-synthetic-source',
        equipment_definition_version='1',
        equipment_definition_sha256=excitation.definition_sha256,
        elements=(
            _element(
                'woofer',
                evidence='complex',
                band=FrequencyBand(low_hz=30.0, high_hz=200.0),
            ),
            _element(
                'port',
                evidence='magnitude_only',
                band=FrequencyBand(low_hz=30.0, high_hz=80.0),
            ),
        ),
        reference_points=SourceReferencePoints(
            geometric_reference_m=Offset3()
        ),
    )
    evaluated = evaluate_wave_source_model_compatibility(
        binding=binding,
        excitation=excitation,
        multi_radiator_model=model,
    )
    assert evaluated.collapse_state == 'collapse_unsupported'
    assert evaluated.compatibility_state == 'unsupported'


def test_evaluation_rejects_binding_excitation_mismatch() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    other = _excitation(
        (40.0, 60.0, 80.0), (9.0e-4 + 0.0j, 8.0e-4 + 0.0j, 7.0e-4 + 0.0j)
    )
    binding = _binding(other)
    with pytest.raises(ValueError, match='does not pin this excitation'):
        evaluate_wave_source_model_compatibility(
            binding=binding, excitation=excitation
        )


def test_evaluation_rejects_foreign_source_model() -> None:
    excitation = _excitation(
        (40.0, 60.0, 80.0), (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    model = build_multi_radiator_source_model(
        model_id='model:foreign',
        version='1',
        equipment_definition_id='equipment:other',
        equipment_definition_version='1',
        equipment_definition_sha256=_hash('other-equipment'),
    )
    with pytest.raises(ValueError, match='equipment definition'):
        evaluate_wave_source_model_compatibility(
            binding=binding,
            excitation=excitation,
            multi_radiator_model=model,
        )


def _bound_spec_bundle(*, collapse: str):
    frequencies = (40.0, 60.0, 80.0)
    q_values = (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    excitation = _excitation(frequencies, q_values)
    binding = _binding(excitation)
    model = None
    if collapse != 'unverified':
        model = _two_element_model(
            excitation.definition_sha256,
            pinned_point=(collapse == 'pinned'),
        )
    compatibility = evaluate_wave_source_model_compatibility(
        binding=binding,
        excitation=excitation,
        multi_radiator_model=model,
    )
    candidate = _candidate_with_binding(
        excitation=excitation, binding=binding
    )
    payload = _r130_payload(
        candidate=candidate,
        excitation=excitation,
        frequencies=frequencies,
        physical_transfer_plus=(1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j),
    )
    payload_hash = _digest(payload)
    artifact_ref = _solver_result_ref(payload_hash)
    result = _solver_result(
        candidate=candidate,
        artifact_ref=artifact_ref,
        frequencies=frequencies,
    )
    responses = (_r150_response(
        frequencies=frequencies, values=(1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j)
    ),)
    from htdt.cad_hybrid_numerical_composition import (
        build_hybrid_convention_normalization_authority,
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
        wave_source_model=compatibility,
    )
    return {
        'spec': spec,
        'result': result,
        'payload': payload,
        'candidate': candidate,
        'excitation': excitation,
        'responses': responses,
        'normalization': normalization,
    }


def _solver_result_ref(payload_hash: str):
    from htdt.cad_candidate_wave_execution import (
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    )
    from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

    return ExactExternalAuthorityRef(
        authority_id=f'acoustic-solver-artifact:{payload_hash}',
        authority_version=COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        semantic_hash_sha256=payload_hash,
    )


def test_r160_records_compatible_source_model_state() -> None:
    bundle = _bound_spec_bundle(collapse='pinned')
    artifact = compose_numerical_hybrid_response(
        spec=bundle['spec'],
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        normalization_authority=bundle['normalization'],
    )
    assert artifact.source_model_state == 'compatible'
    assert artifact.wave_source_model is not None
    assert artifact.wave_source_model.collapse_state == 'collapse_supported'
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'


def test_r160_records_unverified_without_source_model() -> None:
    bundle = _bundle(physical_transfer_plus=(1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j))
    artifact = compose_numerical_hybrid_response(
        spec=bundle['spec'],
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        normalization_authority=bundle['normalization'],
    )
    assert artifact.source_model_state == 'unverified'
    assert artifact.wave_source_model is None
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'


def test_r160_records_limitations_for_unpinned_collapse() -> None:
    bundle = _bound_spec_bundle(collapse='unpinned')
    artifact = compose_numerical_hybrid_response(
        spec=bundle['spec'],
        r130_result=bundle['result'],
        r130_artifact_payload=bundle['payload'],
        r130_candidate_input=bundle['candidate'],
        wave_excitation=bundle['excitation'],
        r150_responses=bundle['responses'],
        normalization_authority=bundle['normalization'],
    )
    assert artifact.source_model_state == 'compatible_with_limitations'
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'


def test_r160_spec_rejects_stale_wave_source_model() -> None:
    frequencies = (40.0, 60.0, 80.0)
    excitation = _excitation(
        frequencies, (1.0e-4 + 0.0j, 2.0e-4 + 0.0j, 3.0e-4 + 0.0j)
    )
    binding = _binding(excitation)
    compatibility = evaluate_wave_source_model_compatibility(
        binding=binding, excitation=excitation
    )
    bundle = _bundle(
        physical_transfer_plus=(1.0 + 0.0j, 2.0 + 0.0j, 3.0 + 0.0j)
    )
    with pytest.raises(ValueError, match='does not pin'):
        build_numerical_hybrid_composition_spec(
            r130_result=bundle['result'],
            r130_artifact_payload=bundle['payload'],
            r130_candidate_input=bundle['candidate'],
            wave_excitation=bundle['excitation'],
            r150_responses=bundle['responses'],
            receiver_id='receiver-1',
            exact_frequency_grid_hz=frequencies,
            transition_start_hz=40.0,
            transition_end_hz=80.0,
            normalization_authority=bundle['normalization'],
            wave_source_model=compatibility,
        )
