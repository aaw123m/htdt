from __future__ import annotations

from math import exp
from pathlib import Path

import pytest

from htdt.cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
)
from htdt.cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    DeterministicAcousticPath,
    DeterministicPathArtifact,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_hybrid_grid_reconciliation import (
    HybridNumericalCompositionError,
    HybridNumericalFailureCode,
    build_hybrid_crossover_configuration_authority,
)
from htdt.cad_hybrid_late_energy import (
    LateFieldBandDecay,
    LateFieldSurfaceCapability,
    R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF,
    build_late_energy_decay_law,
    build_late_field_input_authority,
    late_energy_decay_observable_manifest,
    solve_late_energy_decay,
    surface_scattering_evidence_ref,
)
from htdt.cad_late_field_energy import LATE_FIELD_ARTIFACT_SCHEMA_REF
from htdt.cad_hybrid_numerical_composition import (
    build_hybrid_convention_normalization_authority,
    compute_native_wave_transfer,
)
from htdt.cad_hybrid_stitching import (
    build_automatic_crossover_selection,
    build_hybrid_band_stitch_plan,
    build_stitched_hybrid_composition_spec,
    compose_stitched_hybrid_response,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Position3
from htdt.cad_surface_scattering import build_surface_scattering_evidence
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

from test_cad_hybrid_numerical_composition import (  # noqa: E402
    _candidate_input,
    _digest,
    _domain,
    _excitation,
    _hash,
    _r130_payload,
    _r150_response,
    _ref,
    _solver_result,
)
from htdt.acoustics.persistence.cad_late_energy_decay_repository import CadLateEnergyDecayRepository
from htdt.acoustics.persistence.cad_stitched_hybrid_response_repository import CadStitchedHybridResponseRepository


def _union_fixture():
    """Wave grid [40,60,80], GA grid [60,80,100,120] — overlapping bands."""

    wave_grid = (40.0, 60.0, 80.0)
    ga_grid = (60.0, 80.0, 100.0, 120.0)
    wave_values = (1.0 + 1.0j, 2.0 + 2.0j, 3.0 + 1.0j)
    ga_values = (2.0 + 2.0j, 3.0 + 1.0j, 4.0 + 0.5j, 5.0 + 1.5j)

    excitation = _excitation(wave_grid, (1.0e-4, 7.0e-5 + 2.0e-5j, 1.0e-4j))
    candidate = _candidate_input(
        frequencies=wave_grid, excitation=excitation
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
    response = _r150_response(
        frequencies=ga_grid,
        values=ga_values,
    )
    normalization = build_hybrid_convention_normalization_authority()
    return {
        'wave_grid': wave_grid,
        'ga_grid': ga_grid,
        'wave_values': wave_values,
        'ga_values': ga_values,
        'excitation': excitation,
        'candidate': candidate,
        'payload': payload,
        'result': result,
        'responses': (response,),
        'normalization': normalization,
    }


def _stitched_spec(
    fixture,
    *,
    output_grid,
    crossover=None,
    selection=None,
    responses=None,
):
    return build_stitched_hybrid_composition_spec(
        r130_result=fixture['result'],
        r130_artifact_payload=fixture['payload'],
        r130_candidate_input=fixture['candidate'],
        wave_excitation=fixture['excitation'],
        r150_responses=fixture['responses'] if responses is None else responses,
        receiver_id='receiver-1',
        exact_frequency_grid_hz=output_grid,
        crossover_configuration=crossover,
        automatic_crossover_selection=selection,
        normalization_authority=fixture['normalization'],
    )


def _compose(fixture, spec, *, responses=None):
    return compose_stitched_hybrid_response(
        spec=spec,
        r130_result=fixture['result'],
        r130_artifact_payload=fixture['payload'],
        r130_candidate_input=fixture['candidate'],
        wave_excitation=fixture['excitation'],
        r150_responses=fixture['responses'] if responses is None else responses,
        normalization_authority=fixture['normalization'],
    )


def test_stitch_plan_partitions_union_band_with_crossover() -> None:
    crossover = build_hybrid_crossover_configuration_authority(
        overlap_lower_hz=60.0,
        overlap_upper_hz=80.0,
        wave_validity_band_hz=(40.0, 80.0),
        ga_validity_band_hz=(60.0, 120.0),
    )
    plan = build_hybrid_band_stitch_plan(
        wave_valid_input_band_hz=(40.0, 80.0),
        ga_valid_input_band_hz=(60.0, 120.0),
        requested_output_frequency_grid_hz=(40.0, 50.0, 60.0, 70.0, 80.0, 100.0, 120.0),
        crossover_configuration=crossover,
    )
    assert plan.stitch_state == 'CONTINUOUS'
    assert plan.gap_domains == ()
    assert [region.region for region in plan.regions] == [
        'wave_only',
        'crossover_blend',
        'ga_only',
    ]
    assert plan.regions[0].output_grid_hz == (40.0, 50.0)
    assert plan.regions[1].output_grid_hz == (60.0, 70.0, 80.0)
    assert plan.regions[2].output_grid_hz == (100.0, 120.0)
    assert plan.crossover_configuration == crossover


def test_stitch_plan_records_disjoint_band_gap() -> None:
    plan = build_hybrid_band_stitch_plan(
        wave_valid_input_band_hz=(40.0, 80.0),
        ga_valid_input_band_hz=(100.0, 120.0),
        requested_output_frequency_grid_hz=(40.0, 60.0, 80.0, 100.0, 120.0),
    )
    assert plan.stitch_state == 'GAP_PRESERVED'
    assert [region.region for region in plan.regions] == [
        'wave_only',
        'ga_only',
    ]
    assert len(plan.gap_domains) == 1
    gap = plan.gap_domains[0]
    assert gap.lower_hz == 80.0
    assert gap.upper_hz == 100.0
    assert 'no validity band covers' in gap.reason


def test_stitch_plan_rejects_grid_points_inside_gap() -> None:
    with pytest.raises(HybridNumericalCompositionError) as caught:
        build_hybrid_band_stitch_plan(
            wave_valid_input_band_hz=(40.0, 80.0),
            ga_valid_input_band_hz=(100.0, 120.0),
            requested_output_frequency_grid_hz=(40.0, 90.0, 120.0),
        )
    assert caught.value.code == (
        HybridNumericalFailureCode.OUT_OF_VALID_BAND
    )


def test_stitch_plan_keeps_ambiguous_overlap_unjoined() -> None:
    plan = build_hybrid_band_stitch_plan(
        wave_valid_input_band_hz=(40.0, 100.0),
        ga_valid_input_band_hz=(60.0, 120.0),
        requested_output_frequency_grid_hz=(40.0, 50.0, 110.0, 120.0),
    )
    assert plan.stitch_state == 'GAP_PRESERVED'
    assert [region.region for region in plan.regions] == [
        'wave_only',
        'ga_only',
    ]
    assert len(plan.gap_domains) == 1
    gap = plan.gap_domains[0]
    assert gap.lower_hz == 60.0
    assert gap.upper_hz == 100.0
    assert 'ambiguous' in gap.reason

    with pytest.raises(HybridNumericalCompositionError) as caught:
        build_hybrid_band_stitch_plan(
            wave_valid_input_band_hz=(40.0, 100.0),
            ga_valid_input_band_hz=(60.0, 120.0),
            requested_output_frequency_grid_hz=(40.0, 70.0, 120.0),
        )
    assert caught.value.code == (
        HybridNumericalFailureCode.OUT_OF_VALID_BAND
    )


def test_automatic_crossover_selects_widest_agreement_band() -> None:
    grid = (60.0, 70.0, 80.0, 90.0)
    wave_values = {
        60.0: 1.0 + 0.1j,
        70.0: 2.0 - 0.2j,
        80.0: 3.0 + 0.3j,
        90.0: 4.0,
    }
    ga_values = dict(wave_values)
    selection = build_automatic_crossover_selection(
        wave_values=wave_values,
        ga_values=ga_values,
        wave_validity_band_hz=(40.0, 100.0),
        ga_validity_band_hz=(60.0, 120.0),
        max_relative_discrepancy=0.05,
        discrepancy_floor_pa_per_m3_s=1.0e-12,
    )
    assert selection.selection_state == 'SELECTED'
    assert selection.selected_lower_hz == 60.0
    assert selection.selected_upper_hz == 90.0
    assert selection.crossover_configuration is not None
    assert len(selection.candidates) == 1
    assert selection.candidates[0].accepted
    assert all(item.within_tolerance for item in selection.samples)


def test_automatic_crossover_rejects_equal_width_tie() -> None:
    # Two equal-width agreement runs separated by a violation: ambiguous.
    grid = (60.0, 70.0, 80.0, 90.0, 100.0)
    wave_values = {
        60.0: 1.0 + 0.0j,
        70.0: 2.0 + 0.0j,
        80.0: 3.0 + 0.0j,
        90.0: 4.0 + 0.0j,
        100.0: 5.0 + 0.0j,
    }
    ga_values = {
        60.0: 1.0 + 0.0j,
        70.0: 2.0 + 0.0j,
        80.0: 30.0 + 0.0j,  # violating
        90.0: 4.0 + 0.0j,
        100.0: 5.0 + 0.0j,
    }
    selection = build_automatic_crossover_selection(
        wave_values=wave_values,
        ga_values=ga_values,
        wave_validity_band_hz=(40.0, 100.0),
        ga_validity_band_hz=(60.0, 120.0),
        max_relative_discrepancy=0.05,
        discrepancy_floor_pa_per_m3_s=1.0e-12,
    )
    assert selection.selection_state == 'UNSUPPORTED'
    assert selection.crossover_configuration is None
    assert any('ambiguous' in reason for reason in selection.unsupported_reasons)
    assert len(selection.candidates) == 2
    assert all(not item.accepted for item in selection.candidates)
    assert not selection.samples[2].within_tolerance


def test_automatic_crossover_unsupported_without_agreement() -> None:
    grid = (60.0, 80.0)
    selection = build_automatic_crossover_selection(
        wave_values={60.0: 1.0 + 0.0j, 80.0: 2.0 + 0.0j},
        ga_values={60.0: 10.0 + 0.0j, 80.0: 20.0 + 0.0j},
        wave_validity_band_hz=(40.0, 100.0),
        ga_validity_band_hz=(60.0, 120.0),
        max_relative_discrepancy=0.05,
        discrepancy_floor_pa_per_m3_s=1.0e-12,
    )
    assert selection.selection_state == 'UNSUPPORTED'
    assert selection.candidates == ()
    assert selection.unsupported_reasons


def test_automatic_crossover_unsupported_on_disjoint_bands() -> None:
    selection = build_automatic_crossover_selection(
        wave_values={40.0: 1.0 + 0.0j},
        ga_values={40.0: 1.0 + 0.0j},
        wave_validity_band_hz=(40.0, 60.0),
        ga_validity_band_hz=(80.0, 120.0),
        max_relative_discrepancy=0.05,
        discrepancy_floor_pa_per_m3_s=1.0e-12,
    )
    assert selection.selection_state == 'UNSUPPORTED'
    assert any('disjoint' in reason for reason in selection.unsupported_reasons)


def test_stitched_compose_blends_overlap_and_extends_union() -> None:
    fixture = _union_fixture()
    crossover = build_hybrid_crossover_configuration_authority(
        overlap_lower_hz=60.0,
        overlap_upper_hz=80.0,
        wave_validity_band_hz=(40.0, 80.0),
        ga_validity_band_hz=(60.0, 120.0),
    )
    spec = _stitched_spec(
        fixture,
        output_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
        crossover=crossover,
    )
    artifact = _compose(fixture, spec)
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'
    assert artifact.stitch_state == 'CONTINUOUS'
    assert artifact.gap_domains == ()
    assert len(artifact.samples) == 5
    by_frequency = {
        item.frequency_hz: item for item in artifact.samples
    }
    wave40 = fixture['wave_values'][0]
    ga100 = fixture['ga_values'][2]
    assert by_frequency[40.0].region == 'wave_only'
    assert by_frequency[40.0].low_weight == 1.0
    assert by_frequency[40.0].high_weight == 0.0
    assert by_frequency[40.0].ga_complex_real_pa_per_m3_s is None
    assert complex(
        by_frequency[40.0].complex_real_pa_per_m3_s,
        by_frequency[40.0].complex_imag_pa_per_m3_s,
    ) == wave40
    assert by_frequency[100.0].region == 'ga_only'
    assert by_frequency[100.0].low_weight == 0.0
    assert by_frequency[100.0].high_weight == 1.0
    assert by_frequency[100.0].wave_complex_real_pa_per_m3_s is None
    assert complex(
        by_frequency[100.0].complex_real_pa_per_m3_s,
        by_frequency[100.0].complex_imag_pa_per_m3_s,
    ) == ga100
    blend = by_frequency[60.0]
    assert blend.region == 'crossover_blend'
    assert blend.low_weight == 1.0
    assert blend.high_weight == 0.0
    assert blend.wave_complex_real_pa_per_m3_s is not None
    assert blend.ga_complex_real_pa_per_m3_s is not None
    end_blend = by_frequency[80.0]
    assert end_blend.region == 'crossover_blend'
    assert end_blend.low_weight == 0.0
    assert end_blend.high_weight == 1.0


def test_stitched_compose_with_automatic_crossover_selection() -> None:
    fixture = _union_fixture()
    native_grid, native_wave = compute_native_wave_transfer(
        receiver_id='receiver-1',
        payload=fixture['payload'],
        excitation=fixture['excitation'],
    )
    wave_map = dict(zip(native_grid, native_wave))
    selection = build_automatic_crossover_selection(
        wave_values={60.0: wave_map[60.0], 80.0: wave_map[80.0]},
        ga_values={60.0: wave_map[60.0], 80.0: wave_map[80.0]},
        wave_validity_band_hz=(40.0, 80.0),
        ga_validity_band_hz=(60.0, 120.0),
        max_relative_discrepancy=0.05,
        discrepancy_floor_pa_per_m3_s=1.0e-12,
    )
    assert selection.selection_state == 'SELECTED'
    spec = _stitched_spec(
        fixture,
        output_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
        selection=selection,
    )
    artifact = _compose(fixture, spec)
    assert artifact.capability_state == 'COMPLEX_SUPPORTED'
    assert artifact.crossover_selection == selection
    assert artifact.stitch_plan.automatic_crossover_selection_ref == (
        selection.as_external_ref()
    )
    assert [
        item.region for item in artifact.samples
    ] == ['wave_only', 'crossover_blend', 'crossover_blend', 'ga_only', 'ga_only']


def test_stitched_compose_rejects_unsupported_ga_inputs() -> None:
    fixture = _union_fixture()
    unsupported = _r150_response(
        frequencies=fixture['ga_grid'],
        values=(0.0j,) * 4,
        capability='UNSUPPORTED',
    )
    spec = _stitched_spec(
        fixture,
        output_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
        crossover=build_hybrid_crossover_configuration_authority(
            overlap_lower_hz=60.0,
            overlap_upper_hz=80.0,
            wave_validity_band_hz=(40.0, 80.0),
            ga_validity_band_hz=(60.0, 120.0),
        ),
        responses=(unsupported,),
    )
    artifact = _compose(fixture, spec, responses=(unsupported,))
    assert artifact.capability_state == 'UNSUPPORTED'
    assert artifact.samples == ()
    assert HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH in (
        artifact.failure_codes
    )
    assert artifact.unsupported_reasons


def test_stale_stitched_spec_is_rejected() -> None:
    fixture = _union_fixture()
    crossover = build_hybrid_crossover_configuration_authority(
        overlap_lower_hz=60.0,
        overlap_upper_hz=80.0,
        wave_validity_band_hz=(40.0, 80.0),
        ga_validity_band_hz=(60.0, 120.0),
    )
    spec = _stitched_spec(
        fixture,
        output_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
        crossover=crossover,
    )
    other_response = _r150_response(
        frequencies=fixture['ga_grid'],
        values=(9.0 + 9.0j,) * 4,
        path_label='other',
    )
    with pytest.raises(ValueError, match='stale'):
        compose_stitched_hybrid_response(
            spec=spec,
            r130_result=fixture['result'],
            r130_artifact_payload=fixture['payload'],
            r130_candidate_input=fixture['candidate'],
            wave_excitation=fixture['excitation'],
            r150_responses=(other_response,),
            normalization_authority=fixture['normalization'],
        )


def test_stitched_repository_roundtrip_and_stale_rejection(
    tmp_path: Path,
) -> None:
    fixture = _union_fixture()
    crossover = build_hybrid_crossover_configuration_authority(
        overlap_lower_hz=60.0,
        overlap_upper_hz=80.0,
        wave_validity_band_hz=(40.0, 80.0),
        ga_validity_band_hz=(60.0, 120.0),
    )
    spec = _stitched_spec(
        fixture,
        output_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
        crossover=crossover,
    )
    artifact = _compose(fixture, spec)

    results = {fixture['result'].result_id: fixture['result']}
    payloads = {
        spec.r130_complex_pressure_artifact_ref.authority_id: fixture['payload']
    }
    candidates = {fixture['candidate'].execution_input_id: fixture['candidate']}
    excitations = {fixture['excitation'].excitation_id: fixture['excitation']}
    responses = {item.artifact_id: item for item in fixture['responses']}
    specs = {spec.composition_spec_id: spec}
    normalizations = {
        fixture['normalization'].authority_id: fixture['normalization']
    }

    repository = CadStitchedHybridResponseRepository(
        SceneRepository(tmp_path / 'r160-stitch.sqlite3'),
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
    repository.save(artifact)
    reopened = repository.get(artifact.artifact_id)
    assert reopened == artifact
    assert reopened is not None
    assert reopened.stitch_plan == artifact.stitch_plan

    saved_result = results.pop(fixture['result'].result_id)
    with pytest.raises(ValueError, match='missing/stale'):
        repository.get(artifact.artifact_id)
    results[saved_result.result_id] = saved_result

    saved_spec = specs.pop(spec.composition_spec_id)
    with pytest.raises(ValueError, match='missing/stale'):
        repository.get(artifact.artifact_id)
    specs[saved_spec.composition_spec_id] = saved_spec
    assert repository.get(artifact.artifact_id) == artifact


def _directivity(center_hz: float) -> SourceDirectivityContribution:
    return SourceDirectivityContribution(
        dataset_id='dataset:r160-late',
        dataset_version='1',
        dataset_semantic_sha256=_hash('late-dataset'),
        evaluation_semantic_sha256=_hash(f'late-eval-{center_hz}'),
        frequency_hz=center_hz,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
        magnitude_db=0.0,
        magnitude_linear=1.0,
        energy_factor=1.0,
    )


def _material(
    surface_id: str,
    center_hz: float,
    *,
    absorption: float,
    scattering: float,
) -> BoundaryMaterialContribution:
    return BoundaryMaterialContribution(
        source_surface_id=surface_id,
        material_authority=_ref(f'material-{surface_id}'),
        frequency_hz=center_hz,
        absorption=absorption,
        scattering=scattering,
        specular_energy_factor=(1.0 - absorption) * (1.0 - scattering),
    )


def _path(
    *,
    path_type: str,
    surface_ids: tuple[str, ...],
    bands_hz: tuple[float, ...],
    delay_s: float,
    material_scattering: float = 0.2,
) -> DeterministicAcousticPath:
    bands = []
    for center_hz in bands_hz:
        if not surface_ids:
            materials = ()
            boundary_material = None
            boundary_materials = None
        elif len(surface_ids) == 1:
            boundary_material = _material(
                surface_ids[0],
                center_hz,
                absorption=0.1,
                scattering=material_scattering,
            )
            boundary_materials = None
            materials = (boundary_material,)
        else:
            boundary_material = None
            boundary_materials = tuple(
                _material(
                    surface_id,
                    center_hz,
                    absorption=0.1,
                    scattering=material_scattering,
                )
                for surface_id in surface_ids
            )
            materials = boundary_materials
        arriving = 1.0
        for material in materials:
            arriving *= material.specular_energy_factor
        bands.append(
            DeterministicPathBandQuantity(
                center_hz=center_hz,
                spreading_factor_per_m2=1.0,
                source_directivity=_directivity(center_hz),
                boundary_material=boundary_material,
                boundary_materials=boundary_materials,
                relative_energy_transport_per_m2=arriving,
            )
        )
    placeholder = DeterministicAcousticPath.model_construct(
        path_id=f'deterministic-acoustic-path:{"0" * 64}',
        semantic_sha256='0' * 64,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        receiver_entity_id='receiver-entity-1',
        path_type=path_type,
        ordered_interaction_surface_ids=surface_ids,
        ordered_interaction_points=tuple(
            Position3(x_m=1.0, y_m=0.0, z_m=0.0) for _ in surface_ids
        ),
        ordered_interactions=None,
        ordered_region_ids=None,
        region_segment_evidence=None,
        geometric_path_length_m=2.0,
        propagation_delay_s=delay_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=-1.0, y=0.0, z=0.0),
        bands=tuple(bands),
        solver_implementation_ref=_ref('ga-solver'),
        execution_input_semantic_sha256=_hash('ga-execution-input'),
    )
    core = placeholder.semantic_payload()
    digest = _digest(core)
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _path_artifact(
    *,
    bands_hz: tuple[float, ...] = (500.0, 1000.0),
    reflection_surface: str = 'surface:wall-1',
    scattering: float = 0.2,
) -> DeterministicPathArtifact:
    paths = (
        _path(
            path_type='direct',
            surface_ids=(),
            bands_hz=bands_hz,
            delay_s=0.005,
        ),
        _path(
            path_type='specular_reflection',
            surface_ids=(reflection_surface,),
            bands_hz=bands_hz,
            delay_s=0.01,
            material_scattering=scattering,
        ),
    )
    placeholder = DeterministicPathArtifact.model_construct(
        schema_version=1,
        authority_version='r150-deterministic-ga-1',
        artifact_id=f'deterministic-path-artifact:{"0" * 64}',
        semantic_sha256='0' * 64,
        execution_id=f'r150-ga-execution:{_hash("execution")}',
        execution_input_id='r150-ga-execution-input:test',
        execution_input_sha256=_hash('execution-input'),
        snapshot_id='snapshot:r160-late',
        snapshot_sha256=_hash('snapshot'),
        prediction_request_id='request:r160-late',
        prediction_request_sha256=_hash('request'),
        dispatch_binding_id='dispatch:r160-late',
        dispatch_binding_sha256=_hash('dispatch'),
        adapter_descriptor_id='adapter:r160-late',
        adapter_descriptor_sha256=_hash('adapter'),
        solver_implementation_ref=_ref('ga-solver'),
        solver_configuration_ref=_ref('ga-config'),
        r120_compiled_geometry_id='r120-compiled-geometry:r160-late',
        r120_compiled_geometry_sha256=_hash('geometry'),
        topology_identity_sha256=_hash('topology'),
        engine_id='htdt.r150.test-engine',
        engine_version='1',
        candidate_source_commit=None,
        numeric_comparison_tolerance_m=1.0e-6,
        identity_decimal_places=9,
        frequency_domain=_domain(bands_hz),
        path_scope='direct_and_first_order_specular',
        paths=paths,
        rejected_candidates=(),
    )
    core = placeholder.semantic_payload()
    digest = _digest(core)
    return DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _scattering_evidence(
    surface_id: str,
    bands_hz: tuple[float, ...] = (500.0, 1000.0),
    values: tuple[float, ...] = (0.2, 0.25),
):
    return build_surface_scattering_evidence(
        quantity_kind='random_incidence_scattering_coefficient',
        surface_id=surface_id,
        band_center_hz=bands_hz,
        values=values,
        source='synthetic test evidence',
        provenance='r160 late-energy test',
    )


def _decay_law(
    bands_hz: tuple[float, ...] = (500.0, 1000.0),
    time_grid_s: tuple[float, ...] = (0.01, 0.05, 0.1),
):
    return build_late_energy_decay_law(
        bands=tuple(
            LateFieldBandDecay(
                center_hz=center,
                decay_time_s=0.5,
                provenance='declared_model',
            )
            for center in bands_hz
        ),
        decay_time_grid_s=time_grid_s,
        rationale='synthetic bounded decay law for regression tests',
    )


def test_late_energy_supported_bounded_decay() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    evidence = _scattering_evidence('surface:wall-1', bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            ),
        ),
    )
    law = _decay_law(bands_hz)
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=law,
        scattering_evidence={evidence.evidence_id: evidence},
    )
    assert artifact.capability_state == 'SUPPORTED'
    assert artifact.late_field_start_s == 0.01
    assert len(artifact.bands) == 2
    band = artifact.bands[0]
    # E0=1.0; single reflection -> injected (1-absorption)*scattering =
    # 0.9*0.2 = 0.18 per band. The scattered fraction of the reflected
    # energy leaves the specular channel; absorbed energy cannot scatter.
    assert band.injected_late_energy_per_m2 == pytest.approx(0.18)
    assert band.specular_path_energy_per_m2 == pytest.approx(0.72 + 1.0)
    assert band.diffraction_declared_energy_per_m2 == 0.0
    assert [item.time_s for item in band.samples] == [0.01, 0.05, 0.1]
    assert band.samples[0].late_energy_per_m2 == pytest.approx(0.18)
    assert band.samples[1].late_energy_per_m2 == pytest.approx(
        0.18 * exp(-(0.05 - 0.01) / 0.5)
    )
    assert len(artifact.path_contributions) == 2
    observable = artifact.as_solver_observable(
        _ref('late-energy-encoding')
    )
    assert observable.observable == 'late_energy_decay'


def test_late_energy_injection_conserves_reflection_energy() -> None:
    # Physics regression: the injected late energy per reflection is the
    # scattered fraction of the *reflected* energy, (1-alpha)*s — the exact
    # complement of the specular factor (1-alpha)*(1-s). Injecting s alone
    # counted the absorbed share twice and created energy per bounce.
    bands_hz = (500.0, 1000.0)
    paths = (
        _path(
            path_type='specular_reflection',
            surface_ids=('surface:wall-1', 'surface:wall-2'),
            bands_hz=bands_hz,
            delay_s=0.01,
            material_scattering=0.2,
        ),
    )
    placeholder = DeterministicPathArtifact.model_construct(
        schema_version=1,
        authority_version='r150-deterministic-ga-1',
        artifact_id=f'deterministic-path-artifact:{"0" * 64}',
        semantic_sha256='0' * 64,
        execution_id=f'r150-ga-execution:{_hash("execution2")}',
        execution_input_id='r150-ga-execution-input:test2',
        execution_input_sha256=_hash('execution-input2'),
        snapshot_id='snapshot:r160-late',
        snapshot_sha256=_hash('snapshot'),
        prediction_request_id='request:r160-late',
        prediction_request_sha256=_hash('request'),
        dispatch_binding_id='dispatch:r160-late',
        dispatch_binding_sha256=_hash('dispatch'),
        adapter_descriptor_id='adapter:r160-late',
        adapter_descriptor_sha256=_hash('adapter'),
        solver_implementation_ref=_ref('ga-solver'),
        solver_configuration_ref=_ref('ga-config'),
        r120_compiled_geometry_id='r120-compiled-geometry:r160-late',
        r120_compiled_geometry_sha256=_hash('geometry'),
        topology_identity_sha256=_hash('topology'),
        engine_id='htdt.r150.test-engine',
        engine_version='1',
        candidate_source_commit=None,
        numeric_comparison_tolerance_m=1.0e-6,
        identity_decimal_places=9,
        frequency_domain=_domain(bands_hz),
        path_scope='direct_and_first_order_specular',
        paths=paths,
        rejected_candidates=(),
    )
    core = placeholder.semantic_payload()
    digest = _digest(core)
    path_artifact = DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **core,
    )
    evidences = {}
    capabilities = []
    for surface_id in ('surface:wall-1', 'surface:wall-2'):
        evidence = _scattering_evidence(surface_id, bands_hz)
        evidences[evidence.evidence_id] = evidence
        capabilities.append(
            LateFieldSurfaceCapability(
                surface_id=surface_id,
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            )
        )
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=tuple(capabilities),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence=evidences,
    )
    assert artifact.capability_state == 'SUPPORTED'
    band = artifact.bands[0]
    # E0=1.0, alpha=0.1, s=0.2 per bounce:
    #   late_1 = (1-a)*s          = 0.18
    #   late_2 = spec_1*(1-a)*s   = 0.72*0.18 = 0.1296
    #   spec_end = 0.72*0.72      = 0.5184
    assert band.injected_late_energy_per_m2 == pytest.approx(
        0.18 + 0.72 * 0.18
    )
    assert band.specular_path_energy_per_m2 == pytest.approx(0.5184)
    # Conservation: specular arrival + injected late + absorbed = E0.
    absorbed = 0.1 + 0.72 * 0.1
    assert (
        band.specular_path_energy_per_m2
        + band.injected_late_energy_per_m2
        + absorbed
    ) == pytest.approx(1.0)


def test_late_energy_missing_surface_capability_fails_closed() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence={},
    )
    assert artifact.capability_state == 'UNSUPPORTED'
    assert artifact.bands == ()
    assert artifact.path_contributions == ()
    assert any(
        'surface:wall-1' in reason for reason in artifact.unsupported_reasons
    )


def test_late_energy_specular_only_with_scattering_fails_closed() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='specular_only_declared',
                detail='declared specular-only',
            ),
        ),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence={},
    )
    assert artifact.capability_state == 'UNSUPPORTED'
    assert any(
        'specular-only' in reason for reason in artifact.unsupported_reasons
    )


def test_late_energy_missing_evidence_fails_closed() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    evidence = _scattering_evidence('surface:wall-1', bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            ),
        ),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence={},  # evidence authority absent
    )
    assert artifact.capability_state == 'UNSUPPORTED'
    assert any(
        'missing' in reason for reason in artifact.unsupported_reasons
    )


def test_late_energy_decay_grid_before_last_arrival_raises() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    evidence = _scattering_evidence('surface:wall-1', bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            ),
        ),
    )
    with pytest.raises(HybridNumericalCompositionError) as caught:
        solve_late_energy_decay(
            path_artifact=path_artifact,
            late_field_input=late_input,
            decay_law=_decay_law(bands_hz, time_grid_s=(0.001, 0.05)),
            scattering_evidence={evidence.evidence_id: evidence},
        )
    assert caught.value.code == (
        HybridNumericalFailureCode.INVALID_GRID
    )


def test_late_energy_decay_law_band_mismatch_raises() -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(),
    )
    law = _decay_law((500.0,))
    with pytest.raises(HybridNumericalCompositionError) as caught:
        solve_late_energy_decay(
            path_artifact=path_artifact,
            late_field_input=late_input,
            decay_law=law,
            scattering_evidence={},
        )
    assert caught.value.code == (
        HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH
    )


def test_late_energy_repository_roundtrip_and_stale_rejection(
    tmp_path: Path,
) -> None:
    bands_hz = (500.0, 1000.0)
    path_artifact = _path_artifact(bands_hz=bands_hz)
    evidence = _scattering_evidence('surface:wall-1', bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            ),
        ),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence={evidence.evidence_id: evidence},
    )
    assert artifact.capability_state == 'SUPPORTED'

    path_artifacts = {path_artifact.artifact_id: path_artifact}
    evidences = {evidence.evidence_id: evidence}
    repository = CadLateEnergyDecayRepository(
        SceneRepository(tmp_path / 'r160-late.sqlite3'),
        path_artifact_resolver=lambda artifact_id: path_artifacts.get(
            artifact_id
        ),
        scattering_evidence_resolver=lambda evidence_id: evidences.get(
            evidence_id
        ),
    )
    repository.save(artifact)
    reopened = repository.get(artifact.artifact_id)
    assert reopened == artifact

    path_artifacts.pop(path_artifact.artifact_id)
    with pytest.raises(ValueError, match='missing/stale'):
        repository.get(artifact.artifact_id)
    path_artifacts[path_artifact.artifact_id] = path_artifact

    evidences.pop(evidence.evidence_id)
    with pytest.raises(ValueError, match='missing/stale'):
        repository.get(artifact.artifact_id)
    evidences[evidence.evidence_id] = evidence
    assert repository.get(artifact.artifact_id) == artifact


def _supported_late_energy_artifact(
    bands_hz: tuple[float, ...] = (500.0, 1000.0),
):
    path_artifact = _path_artifact(bands_hz=bands_hz)
    evidence = _scattering_evidence('surface:wall-1', bands_hz)
    late_input = build_late_field_input_authority(
        path_artifact=path_artifact,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        surface_capabilities=(
            LateFieldSurfaceCapability(
                surface_id='surface:wall-1',
                capability='scattering_modeled',
                evidence_ref=surface_scattering_evidence_ref(evidence),
                detail='synthetic modeled scattering',
            ),
        ),
    )
    artifact = solve_late_energy_decay(
        path_artifact=path_artifact,
        late_field_input=late_input,
        decay_law=_decay_law(bands_hz),
        scattering_evidence={evidence.evidence_id: evidence},
    )
    assert artifact.capability_state == 'SUPPORTED'
    return artifact


def test_late_energy_observable_defaults_to_canonical_schema_ref() -> None:
    artifact = _supported_late_energy_artifact()
    observable = artifact.as_solver_observable()
    assert observable.observable == 'late_energy_decay'
    assert observable.artifact_authority == artifact.as_external_ref()
    assert observable.encoding_schema_ref == (
        R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
    )
    assert observable.valid_frequency_domain == (
        artifact.valid_frequency_domain
    )
    assert late_energy_decay_observable_manifest(artifact) == observable


def test_late_energy_observable_explicit_schema_ref_preserved() -> None:
    artifact = _supported_late_energy_artifact()
    observable = artifact.as_solver_observable(
        _ref('late-energy-encoding')
    )
    assert observable.encoding_schema_ref == _ref('late-energy-encoding')


def test_late_energy_observable_schema_refs_are_distinct() -> None:
    # Both slices bind observable 'late_energy_decay'; envelopes disambiguate
    # the two encodings by encoding_schema_ref.
    assert R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF != (
        LATE_FIELD_ARTIFACT_SCHEMA_REF
    )
    assert (
        R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF.authority_id
        != LATE_FIELD_ARTIFACT_SCHEMA_REF.authority_id
    )
