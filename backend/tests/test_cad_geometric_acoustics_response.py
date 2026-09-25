from __future__ import annotations

import cmath
from math import pi
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import (
    AcousticMaterial,
    GeometricAcousticBand,
    SpecificImpedancePoint,
)
from htdt.cad_directivity import (
    DirectivityCoordinateConvention,
    DirectivityDataset,
    DirectivityNormalization,
    DirectivitySample,
)
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
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
    DeterministicPathBandQuantity,
    DeterministicPathInteraction,
    GeometricSurfacePlane,
    SourceDirectivityContribution,
)
from htdt.cad_geometric_acoustics_response import (
    ANALYTIC_OMNI_DIRECTIVITY_MODEL,
    CadPathFrequencyResponseRepository,
    ComplexTransferSample,
    build_acoustic_environment_authority,
    build_deterministic_path_frequency_response,
    build_explicit_complex_surface_reflection_authority,
    build_frequency_grid_authority,
    build_magnitude_only_surface_reflection_authority,
    build_path_response_configuration,
    build_point_source_normalization_authority,
    build_portal_acoustic_transfer_authority,
    build_receiver_response_authority,
    build_rigid_surface_reflection_authority,
    build_source_response_authority,
    build_specific_impedance_surface_reflection_authority,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Position3
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


H = '1' * 64
H2 = '2' * 64
H3 = '3' * 64
H4 = '4' * 64
H5 = '5' * 64
SURFACE_A = f'semantic-surface:{"a" * 64}'
SURFACE_B = f'semantic-surface:{"b" * 64}'


def _ref(authority_id: str, digest: str = H, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority_id,
        authority_version=version,
        semantic_hash_sha256=digest,
    )


def _domain(frequencies: tuple[float, ...]) -> FrequencyDomain:
    if len(frequencies) == 1:
        value = frequencies[0]
        return FrequencyDomain(
            minimum_hz=value * (1.0 - 1e-12),
            maximum_hz=value * (1.0 + 1e-12),
        )
    return FrequencyDomain(minimum_hz=frequencies[0], maximum_hz=frequencies[-1])


def _directivity_domain(frequencies: tuple[float, ...]) -> DirectivityDomain:
    return DirectivityDomain(
        frequency=_domain(frequencies),
        horizontal=AngleDomain(minimum_deg=-180.0, maximum_deg=180.0),
        vertical=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
    )


def _equipment(
    frequencies: tuple[float, ...],
    *,
    tier: str = 'analytic',
    analytic_model: str | None = ANALYTIC_OMNI_DIRECTIVITY_MODEL,
) -> EquipmentDefinition:
    if tier == 'analytic':
        capability = DirectivityCapability.model_construct(
            tier='analytic',
            data_format='analytic_model',
            provenance=None,
            data_asset_sha256=None,
            valid_domain=_directivity_domain(frequencies),
            interpolation=None,
            coherent_phase=True,
            phase_reference='source_volume_velocity_t0',
            analytic_model=analytic_model,
        )
    elif tier == 'magnitude_only':
        capability = DirectivityCapability.model_construct(
            tier='magnitude_only',
            data_format='custom',
            provenance=None,
            data_asset_sha256=H3,
            valid_domain=_directivity_domain(frequencies),
            interpolation=None,
            coherent_phase=False,
            phase_reference=None,
            analytic_model=None,
        )
    else:
        capability = DirectivityCapability.model_construct(
            tier='unknown',
            data_format='unknown',
            provenance=None,
            data_asset_sha256=None,
            valid_domain=None,
            interpolation=None,
            coherent_phase=False,
            phase_reference=None,
            analytic_model=None,
        )
    return EquipmentDefinition.model_construct(
        definition_id='equipment:test-speaker',
        version='1',
        semantic_sha256=H2,
        directivity=capability,
    )


def _dataset(
    frequencies: tuple[float, ...],
    *,
    kind: str = 'magnitude_only',
    normalization_reference: str = 'on_axis_per_frequency',
) -> DirectivityDataset:
    normalization = (
        DirectivityNormalization(
            source_magnitude_unit='db',
            reference='on_axis_per_frequency',
        )
        if normalization_reference == 'on_axis_per_frequency'
        else DirectivityNormalization(
            source_magnitude_unit='db',
            reference='explicit_reference_level',
            reference_level_db=90.0,
        )
    )
    return DirectivityDataset.model_construct(
        dataset_id='directivity:test-speaker',
        version='1',
        semantic_sha256=H3,
        equipment_definition_id='equipment:test-speaker',
        equipment_definition_version='1',
        equipment_definition_sha256=H2,
        valid_domain=_directivity_domain(frequencies),
        kind=kind,
        normalization=normalization,
    )


def _band(
    frequency: float,
    *,
    magnitude_linear: float,
    dataset: DirectivityDataset,
) -> DeterministicPathBandQuantity:
    directivity = SourceDirectivityContribution(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        evaluation_semantic_sha256=H4,
        frequency_hz=frequency,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
        magnitude_db=20.0,
        magnitude_linear=magnitude_linear,
        energy_factor=magnitude_linear * magnitude_linear,
    )
    return DeterministicPathBandQuantity(
        center_hz=frequency,
        spreading_factor_per_m2=1.0,
        source_directivity=directivity,
        relative_energy_transport_per_m2=1.0,
    )


def _path(
    *,
    length_m: float,
    sound_speed_m_s: float,
    path_id_seed: str,
    surfaces: tuple[str, ...] = (),
    interactions: tuple[DeterministicPathInteraction, ...] | None = None,
    bands: tuple[DeterministicPathBandQuantity, ...] = (),
) -> DeterministicAcousticPath:
    path_type = 'direct' if not surfaces else 'specular_reflection'
    points = tuple(
        Position3(x_m=float(index + 1), y_m=0.0, z_m=0.0)
        for index in range(len(surfaces))
    )
    return DeterministicAcousticPath.model_construct(
        path_id=f'deterministic-acoustic-path:{path_id_seed * 64}',
        semantic_sha256=path_id_seed * 64,
        source_entity_id='source-1',
        receiver_id='receiver-1',
        receiver_entity_id='receiver-entity-1',
        path_type=path_type,
        ordered_interaction_surface_ids=surfaces,
        ordered_interaction_points=points,
        ordered_interactions=interactions,
        ordered_region_ids=None,
        region_segment_evidence=None,
        geometric_path_length_m=length_m,
        propagation_delay_s=length_m / sound_speed_m_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=1.0, y=0.0, z=0.0),
        bands=bands,
        adapter_id='htdt.r150.deterministic-path',
        adapter_version='1',
        solver_implementation_ref=_ref('solver:test'),
    )


def _execution_input(
    path: DeterministicAcousticPath,
    common: dict,
    *,
    semantic_sha256: str = 'e' * 64,
    frequency_domain: FrequencyDomain | None = None,
    portal_authority_ref: ExactExternalAuthorityRef | None = None,
    boundary_planes: tuple[GeometricSurfacePlane, ...] | None = None,
) -> DeterministicGaExecutionInput:
    directivity_ref = common['source'].directivity_dataset_ref
    return DeterministicGaExecutionInput.model_construct(
        authority_version='r150-deterministic-ga-1',
        execution_input_id=f'r150-ga-execution-input:{semantic_sha256}',
        semantic_sha256=semantic_sha256,
        r120_compiled_geometry_id=common['r120'].authority_id,
        r120_compiled_geometry_sha256=common['r120'].semantic_hash_sha256,
        portal_authority_ref=(
            portal_authority_ref
            if portal_authority_ref is not None
            else _ref('r120-portals:' + H4, H4)
        ),
        sources=(
            DeterministicGaSourceInput(
                source_entity_id=path.source_entity_id,
                r110_compiled_source_sha256=common['source'].r110_source_ref.semantic_hash_sha256,
                source_reference_point=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
                source_axis=Direction3(x=1.0, y=0.0, z=0.0),
                directivity_dataset_id=(
                    directivity_ref.authority_id
                    if directivity_ref is not None
                    else 'directivity:geometry-only-placeholder'
                ),
                directivity_dataset_version=(
                    directivity_ref.authority_version
                    if directivity_ref is not None
                    else '1'
                ),
                directivity_dataset_sha256=(
                    directivity_ref.semantic_hash_sha256
                    if directivity_ref is not None
                    else H3
                ),
            ),
        ),
        receivers=(
            DeterministicGaReceiverInput(
                receiver_id=path.receiver_id,
                entity_id=path.receiver_entity_id,
                world_position=common['receiver'].world_position,
            ),
        ),
        boundary_planes=(
            boundary_planes if boundary_planes is not None else ()
        ),
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        frequency_domain=(
            frequency_domain
            if frequency_domain is not None
            else FrequencyDomain(minimum_hz=20.0, maximum_hz=20000.0)
        ),
    )


def _artifact(
    path: DeterministicAcousticPath,
    execution_input: DeterministicGaExecutionInput,
    *,
    frequency_domain: FrequencyDomain | None = None,
) -> DeterministicPathArtifact:
    return DeterministicPathArtifact.model_construct(
        authority_version='r150-deterministic-ga-1',
        artifact_id=f'deterministic-path-artifact:{H5}',
        semantic_sha256=H5,
        execution_input_id=execution_input.execution_input_id,
        execution_input_sha256=execution_input.semantic_sha256,
        r120_compiled_geometry_id='r120-compiled-geometry:' + H,
        r120_compiled_geometry_sha256=H,
        frequency_domain=(
            frequency_domain
            if frequency_domain is not None
            else FrequencyDomain(minimum_hz=20.0, maximum_hz=20000.0)
        ),
        paths=(path,),
    )


def _common(
    frequencies: tuple[float, ...],
    *,
    sound_speed_m_s: float = 343.0,
    density_kg_m3: float = 1.2,
    source_tier: str = 'analytic',
):
    frequency_grid = build_frequency_grid_authority(frequencies)
    environment = build_acoustic_environment_authority(
        density_kg_m3=density_kg_m3,
        sound_speed_m_s=sound_speed_m_s,
        valid_frequency_domain=_domain(frequencies),
    )
    configuration = build_path_response_configuration(minimum_path_length_m=1e-6)
    normalization = build_point_source_normalization_authority(
        valid_frequency_domain=_domain(frequencies),
    )
    equipment = _equipment(frequencies, tier=source_tier)
    dataset = _dataset(frequencies) if source_tier == 'magnitude_only' else None
    source = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1', H2),
        equipment_definition=equipment,
        directivity_dataset=dataset,
        point_source_normalization=normalization,
    )
    receiver = build_receiver_response_authority(
        receiver_id='receiver-1',
        receiver_entity_id='receiver-entity-1',
        receiver_authority_ref=_ref('r110-receiver:receiver-1', H4),
        world_position=Position3(x_m=2.0, y_m=0.0, z_m=0.0),
    )
    r120 = _ref('r120-compiled-geometry:' + H, H)
    return {
        'frequency_grid': frequency_grid,
        'environment': environment,
        'configuration': configuration,
        'normalization': normalization,
        'source': source,
        'receiver': receiver,
        'r120': r120,
        'dataset': dataset,
    }


def _response(path: DeterministicAcousticPath, common: dict, **kwargs):
    portal_ref = kwargs.get('portal_geometry_authority_ref')
    execution_input = kwargs.pop(
        'execution_input',
        _execution_input(
            path,
            common,
            portal_authority_ref=portal_ref,
        ),
    )
    path_artifact = kwargs.pop(
        'path_artifact',
        _artifact(path, execution_input),
    )
    return build_deterministic_path_frequency_response(
        path_artifact=path_artifact,
        execution_input=execution_input,
        path_id=path.path_id,
        r120_geometry_ref=common['r120'],
        source_authority=kwargs.pop('source_authority', common['source']),
        point_source_normalization=common['normalization'],
        receiver_authority=kwargs.pop('receiver_authority', common['receiver']),
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        configuration=common['configuration'],
        directivity_dataset=common['dataset'],
        **kwargs,
    )


def _expected_monopole(
    *,
    frequency_hz: float,
    density_kg_m3: float,
    sound_speed_m_s: float,
    distance_m: float,
) -> complex:
    omega = 2.0 * pi * frequency_hz
    k = omega / sound_speed_m_s
    return (
        1j
        * omega
        * density_kg_m3
        * cmath.exp(-1j * k * distance_m)
        / (4.0 * pi * distance_m)
    )


def test_free_field_direct_matches_independent_point_monopole_reference() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='6',
    )

    response = _response(path, common)

    assert response.capability == 'COMPLEX_SUPPORTED'
    sample = response.samples[0]
    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=2.0,
    )
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)
    assert sample.magnitude_pa_per_m3_s == pytest.approx(abs(expected), rel=1e-12)


def test_rigid_plane_first_reflection_matches_image_source_reference() -> None:
    frequency = 800.0
    common = _common((frequency,))
    path = _path(
        length_m=4.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='7',
        surfaces=(SURFACE_A,),
    )
    material = AcousticMaterial(
        material_id='rigid-wall',
        provenance='analytic rigid fixture',
        version='1',
        wave_model='rigid',
    )
    reflection = build_rigid_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:rigid', H2),
        material=material,
        frequency_grid=common['frequency_grid'],
    )

    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
    )

    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=4.0,
    )
    sample = response.samples[0]
    assert response.capability == 'COMPLEX_SUPPORTED'
    assert response.path_length_m == 4.0
    assert sample.magnitude_pa_per_m3_s == pytest.approx(abs(expected), rel=1e-12)
    assert sample.phase_rad == pytest.approx(cmath.phase(expected), abs=1e-12)


def test_specific_impedance_reflection_uses_complex_local_reaction_coefficient() -> None:
    frequency = 500.0
    common = _common((frequency,), density_kg_m3=1.25, sound_speed_m_s=340.0)
    characteristic = (
        common['environment'].density_kg_m3
        * common['environment'].sound_speed_m_s
    )
    material = AcousticMaterial(
        material_id='z-wall',
        provenance='explicit impedance fixture',
        version='1',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=frequency,
                resistance_pa_s_m=2.0 * characteristic,
                reactance_pa_s_m=characteristic,
            ),
        ),
    )
    reflection = build_specific_impedance_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:z-wall', H2),
        material=material,
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        incidence_cosine=1.0,
    )
    path = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='8',
        surfaces=(SURFACE_A,),
        interactions=(
            DeterministicPathInteraction(
                kind='reflection',
                point=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
                surface_id=SURFACE_A,
            ),
        ),
    )
    plane = GeometricSurfacePlane(
        source_surface_id=SURFACE_A,
        point_m=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
        normal=Direction3(x=1.0, y=0.0, z=0.0),
        compiled_triangle_indices=(0,),
    )

    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
        execution_input=_execution_input(
            path,
            common,
            boundary_planes=(plane,),
        ),
    )

    z = complex(2.0 * characteristic, characteristic)
    expected_r = (z - characteristic) / (z + characteristic)
    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=3.0,
    ) * expected_r
    sample = response.samples[0]
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)


def test_second_order_reflection_preserves_ordered_complex_coefficient_product() -> None:
    frequency = 1200.0
    common = _common((frequency,))
    path = _path(
        length_m=5.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='9',
        surfaces=(SURFACE_A, SURFACE_B),
    )
    r1 = 0.5 + 0.25j
    r2 = -0.2 + 0.1j
    reflection_a = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:a', H2),
        frequency_coefficients={frequency: r1},
        provenance='explicit fixture A',
    )
    reflection_b = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_B,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:b', H3),
        frequency_coefficients={frequency: r2},
        provenance='explicit fixture B',
    )

    response = _response(
        path,
        common,
        surface_reflections={
            SURFACE_A: reflection_a,
            SURFACE_B: reflection_b,
        },
    )

    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=5.0,
    ) * r1 * r2
    sample = response.samples[0]
    assert response.ordered_surface_interactions == (SURFACE_A, SURFACE_B)
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)


def test_magnitude_only_source_is_not_promoted_to_complex_phase() -> None:
    frequency = 1000.0
    common = _common((frequency,), source_tier='magnitude_only')
    dataset = common['dataset']
    assert dataset is not None
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='c',
        bands=(_band(frequency, magnitude_linear=2.0, dataset=dataset),),
    )

    response = _response(path, common)

    assert response.capability == 'MAGNITUDE_ONLY'
    assert response.samples[0].phase_rad is None
    assert response.samples[0].complex_real_pa_per_m3_s is None
    expected = abs(
        _expected_monopole(
            frequency_hz=frequency,
            density_kg_m3=common['environment'].density_kg_m3,
            sound_speed_m_s=common['environment'].sound_speed_m_s,
            distance_m=2.0,
        )
    ) * 2.0
    assert response.samples[0].magnitude_pa_per_m3_s == pytest.approx(expected)


def test_scalar_absorption_scattering_surface_is_magnitude_only_not_complex() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    material = AcousticMaterial(
        material_id='banded-only',
        provenance='scalar band fixture',
        version='1',
        wave_model='unsupported',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=frequency,
                absorption=0.36,
                scattering=0.0,
            ),
        ),
    )
    reflection = build_magnitude_only_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:banded', H2),
        material=material,
        frequency_grid=common['frequency_grid'],
    )
    path = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='d',
        surfaces=(SURFACE_A,),
    )

    response = _response(path, common, surface_reflections={SURFACE_A: reflection})

    assert response.capability == 'MAGNITUDE_ONLY'
    assert response.samples[0].phase_rad is None
    assert response.samples[0].magnitude_pa_per_m3_s == pytest.approx(
        abs(
            _expected_monopole(
                frequency_hz=frequency,
                density_kg_m3=common['environment'].density_kg_m3,
                sound_speed_m_s=common['environment'].sound_speed_m_s,
                distance_m=3.0,
            )
        )
        * 0.8
    )


def test_out_of_band_reflection_and_unknown_directivity_fail_closed() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='e',
        surfaces=(SURFACE_A,),
    )
    reflection = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:a', H2),
        frequency_coefficients={500.0: 0.5 + 0.0j},
        provenance='out-of-band fixture',
    )

    response = _response(path, common, surface_reflections={SURFACE_A: reflection})
    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert any('REFLECTION_VALID_BAND_MISMATCH' in item for item in response.unsupported_reasons)

    unknown = _common((frequency,))
    equipment = _equipment((frequency,), tier='unknown')
    unknown['source'] = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1', H2),
        equipment_definition=equipment,
        point_source_normalization=unknown['normalization'],
    )
    direct_path = _path(
        length_m=2.0,
        sound_speed_m_s=unknown['environment'].sound_speed_m_s,
        path_id_seed='f',
    )
    rejected = _response(direct_path, unknown)
    assert rejected.capability == 'UNSUPPORTED'
    assert rejected.samples == ()


def _portal_path(
    *,
    sound_speed_m_s: float,
    length_m: float = 6.0,
) -> DeterministicAcousticPath:
    interactions = (
        DeterministicPathInteraction(
            kind='portal_crossing',
            point=Position3(x_m=2.0, y_m=0.0, z_m=0.0),
            portal_id='portal-ab',
            from_region_id='A',
            to_region_id='B',
        ),
        DeterministicPathInteraction(
            kind='portal_crossing',
            point=Position3(x_m=4.0, y_m=0.0, z_m=0.0),
            portal_id='portal-bc',
            from_region_id='B',
            to_region_id='C',
        ),
    )
    path = _path(
        length_m=length_m,
        sound_speed_m_s=sound_speed_m_s,
        path_id_seed='a',
        interactions=interactions,
    )
    return DeterministicAcousticPath.model_construct(
        **{
            **path.__dict__,
            'ordered_region_ids': ('A', 'B', 'C'),
        }
    )



def test_one_portal_first_order_reflection_reuses_complex_response_authorities() -> None:
    frequency = 700.0
    common = _common((frequency,))
    reflection_event = DeterministicPathInteraction(
        kind='reflection',
        point=Position3(x_m=0.0, y_m=1.25, z_m=1.0),
        surface_id=SURFACE_A,
    )
    portal_event = DeterministicPathInteraction(
        kind='portal_crossing',
        point=Position3(x_m=2.0, y_m=1.75, z_m=1.0),
        portal_id='portal-ab',
        from_region_id='A',
        to_region_id='B',
    )
    base = _path(
        length_m=17.0 ** 0.5,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='b',
        surfaces=(SURFACE_A,),
        interactions=(reflection_event, portal_event),
    )
    path = DeterministicAcousticPath.model_construct(
        **{
            **base.__dict__,
            'ordered_region_ids': ('A', 'B'),
        }
    )

    portal_ref = _ref('r120-portals:' + H2, H2)
    reflection_coefficient = 0.6 + 0.2j
    portal_coefficient = 0.8 - 0.1j
    reflection = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:a', H3),
        frequency_coefficients={frequency: reflection_coefficient},
        provenance='one-Portal reflection fixture',
    )
    transfer = build_portal_acoustic_transfer_authority(
        portal_id='portal-ab',
        from_region_id='A',
        to_region_id='B',
        portal_geometry_authority_ref=portal_ref,
        frequency_coefficients={frequency: portal_coefficient},
        provenance='one-Portal transmission fixture',
        provenance_state='measured',
        uncertainty='fixture exact coefficient',
    )

    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
        portal_geometry_authority_ref=portal_ref,
        portal_transfers={('portal-ab', 'A', 'B'): transfer},
    )
    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=path.geometric_path_length_m,
    ) * reflection_coefficient * portal_coefficient

    assert response.capability == 'COMPLEX_SUPPORTED'
    assert response.ordered_surface_interactions == (SURFACE_A,)
    assert response.ordered_portal_interactions == ('portal-ab',)
    sample = response.samples[0]
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)



def test_missing_portal_transfer_is_unsupported_not_unity() -> None:
    frequency = 700.0
    common = _common((frequency,))
    path = _portal_path(sound_speed_m_s=common['environment'].sound_speed_m_s)
    portal_ref = _ref('r120-portals:' + H2, H2)

    response = _response(
        path,
        common,
        portal_geometry_authority_ref=portal_ref,
        portal_transfers={},
    )

    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert any(
        item.startswith('MISSING_PORTAL_TRANSFER_AUTHORITY:')
        for item in response.unsupported_reasons
    )


def test_explicit_multi_portal_transfer_product_is_deterministic_and_identity_bound() -> None:
    frequency = 700.0
    common = _common((frequency,))
    path = _portal_path(sound_speed_m_s=common['environment'].sound_speed_m_s)
    portal_ref = _ref('r120-portals:' + H2, H2)
    t1 = 0.7 + 0.1j
    t2 = 0.8 - 0.2j
    transfer_ab = build_portal_acoustic_transfer_authority(
        portal_id='portal-ab',
        from_region_id='A',
        to_region_id='B',
        portal_geometry_authority_ref=portal_ref,
        frequency_coefficients={frequency: t1},
        provenance='measured fixture AB',
        provenance_state='measured',
        uncertainty='fixture uncertainty ±0.01 complex magnitude components',
    )
    transfer_bc = build_portal_acoustic_transfer_authority(
        portal_id='portal-bc',
        from_region_id='B',
        to_region_id='C',
        portal_geometry_authority_ref=portal_ref,
        frequency_coefficients={frequency: t2},
        provenance='manufacturer fixture BC',
        provenance_state='manufacturer',
        uncertainty='manufacturer tabulation uncertainty unspecified',
    )
    transfers = {
        ('portal-ab', 'A', 'B'): transfer_ab,
        ('portal-bc', 'B', 'C'): transfer_bc,
    }

    response = _response(
        path,
        common,
        portal_geometry_authority_ref=portal_ref,
        portal_transfers=transfers,
    )

    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=path.geometric_path_length_m,
    ) * t1 * t2
    sample = response.samples[0]
    assert response.capability == 'COMPLEX_SUPPORTED'
    assert response.ordered_portal_interactions == ('portal-ab', 'portal-bc')
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)

    changed_bc = build_portal_acoustic_transfer_authority(
        portal_id='portal-bc',
        from_region_id='B',
        to_region_id='C',
        portal_geometry_authority_ref=portal_ref,
        frequency_coefficients={frequency: 0.9 - 0.2j},
        provenance='manufacturer fixture BC revision 2',
        provenance_state='manufacturer',
        uncertainty='manufacturer tabulation uncertainty unspecified',
    )
    changed = _response(
        path,
        common,
        portal_geometry_authority_ref=portal_ref,
        portal_transfers={
            ('portal-ab', 'A', 'B'): transfer_ab,
            ('portal-bc', 'B', 'C'): changed_bc,
        },
    )
    assert changed.artifact_id != response.artifact_id


def test_environment_delay_mismatch_and_near_singularity_fail_closed() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    mismatched = _path(
        length_m=2.0,
        sound_speed_m_s=300.0,
        path_id_seed='b',
    )
    response = _response(mismatched, common)
    assert response.capability == 'UNSUPPORTED'
    assert 'ENVIRONMENT_PATH_DELAY_MISMATCH' in response.unsupported_reasons

    near = _path(
        length_m=1e-8,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='0',
    )
    singular = _response(near, common)
    assert singular.capability == 'UNSUPPORTED'
    assert 'ZERO_OR_NEAR_SINGULAR_PATH_LENGTH' in singular.unsupported_reasons


def test_save_reopen_exact_and_stale_dependency_rejection(tmp_path: Path) -> None:
    frequency = 1000.0
    common = _common((frequency,))
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='6',
    )
    execution_input = _execution_input(path, common)
    path_artifact = _artifact(path, execution_input)
    response = build_deterministic_path_frequency_response(
        path_artifact=path_artifact,
        execution_input=execution_input,
        path_id=path.path_id,
        r120_geometry_ref=common['r120'],
        source_authority=common['source'],
        point_source_normalization=common['normalization'],
        receiver_authority=common['receiver'],
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        configuration=common['configuration'],
    )

    dependency_map = {}
    typed = (
        execution_input,
        common['source'],
        common['normalization'],
        common['receiver'],
        common['environment'],
        common['frequency_grid'],
        common['configuration'],
    )
    for item in typed:
        if isinstance(item, DeterministicGaExecutionInput):
            ref = ExactExternalAuthorityRef(
                authority_id=item.execution_input_id,
                authority_version=item.authority_version,
                semantic_hash_sha256=item.semantic_sha256,
            )
        else:
            ref = item.as_external_ref()
        dependency_map[(ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)] = item
    for ref in response.dependency_refs:
        dependency_map.setdefault(
            (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256),
            ref,
        )

    scene = SceneRepository(tmp_path / 'scene.sqlite3')

    def resolve_path(artifact_id: str):
        return path_artifact if artifact_id == path_artifact.artifact_id else None

    def resolve_dependency(ref: ExactExternalAuthorityRef):
        return dependency_map.get(
            (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)
        )

    repository = CadPathFrequencyResponseRepository(
        scene,
        path_artifact_resolver=resolve_path,
        dependency_resolver=resolve_dependency,
    )
    repository.save(response)
    assert repository.get(response.artifact_id) == response

    env_ref = common['environment'].as_external_ref()
    dependency_map.pop(
        (env_ref.authority_id, env_ref.authority_version, env_ref.semantic_hash_sha256)
    )
    with pytest.raises(ValueError, match='missing/stale'):
        repository.get(response.artifact_id)


def test_source_receiver_and_stale_surface_identity_mismatches_fail_closed() -> None:
    frequency = 900.0
    common = _common((frequency,))
    path = _path(
        length_m=2.5,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='1',
    )
    wrong_source = build_source_response_authority(
        source_entity_id='source-other',
        r110_source_ref=_ref('r110-source:source-other', H2),
        equipment_definition=_equipment((frequency,)),
        point_source_normalization=common['normalization'],
    )
    source_mismatch = _response(
        path,
        common,
        source_authority=wrong_source,
    )
    assert source_mismatch.capability == 'UNSUPPORTED'
    assert 'SOURCE_IDENTITY_MISMATCH' in source_mismatch.unsupported_reasons

    wrong_receiver = build_receiver_response_authority(
        receiver_id='receiver-1',
        receiver_entity_id='receiver-entity-other',
        receiver_authority_ref=_ref('r110-receiver:receiver-other', H4),
        world_position=common['receiver'].world_position,
    )
    receiver_mismatch = _response(
        path,
        common,
        receiver_authority=wrong_receiver,
    )
    assert receiver_mismatch.capability == 'UNSUPPORTED'
    assert 'RECEIVER_IDENTITY_MISMATCH' in receiver_mismatch.unsupported_reasons

    moved_receiver = build_receiver_response_authority(
        receiver_id='receiver-1',
        receiver_entity_id='receiver-entity-1',
        receiver_authority_ref=_ref('r110-receiver:receiver-1', H4),
        world_position=Position3(x_m=2.1, y_m=0.0, z_m=0.0),
    )
    moved_receiver_response = _response(
        path,
        common,
        receiver_authority=moved_receiver,
    )
    assert moved_receiver_response.capability == 'UNSUPPORTED'
    assert 'RECEIVER_POSITION_MISMATCH' in moved_receiver_response.unsupported_reasons

    reflected = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='2',
        surfaces=(SURFACE_A,),
    )
    stale_reflection = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=_ref('r120-compiled-geometry:' + H2, H2),
        material_authority_ref=_ref('material:a', H3),
        frequency_coefficients={frequency: 0.8 + 0.1j},
        provenance='stale geometry fixture',
    )
    stale = _response(
        reflected,
        common,
        surface_reflections={SURFACE_A: stale_reflection},
    )
    assert stale.capability == 'UNSUPPORTED'
    assert f'STALE_REFLECTION_SURFACE_AUTHORITY:{SURFACE_A}' in stale.unsupported_reasons

    wrong_surface_reflection = build_explicit_complex_surface_reflection_authority(
        source_surface_id=SURFACE_B,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:b', H3),
        frequency_coefficients={frequency: 0.8 + 0.1j},
        provenance='wrong exact surface fixture',
    )
    wrong_surface = _response(
        reflected,
        common,
        surface_reflections={SURFACE_A: wrong_surface_reflection},
    )
    assert wrong_surface.capability == 'UNSUPPORTED'
    assert (
        f'REFLECTION_SURFACE_IDENTITY_MISMATCH:{SURFACE_A}'
        in wrong_surface.unsupported_reasons
    )


def test_stale_portal_transfer_authority_fails_closed() -> None:
    frequency = 650.0
    common = _common((frequency,))
    path = _portal_path(sound_speed_m_s=common['environment'].sound_speed_m_s)
    current_portal_ref = _ref('r120-portals:' + H2, H2)
    stale_portal_ref = _ref('r120-portals:' + H3, H3)
    transfer_ab = build_portal_acoustic_transfer_authority(
        portal_id='portal-ab',
        from_region_id='A',
        to_region_id='B',
        portal_geometry_authority_ref=stale_portal_ref,
        frequency_coefficients={frequency: 0.9 + 0.0j},
        provenance='stale portal fixture AB',
        provenance_state='measured',
        uncertainty='fixture',
    )
    transfer_bc = build_portal_acoustic_transfer_authority(
        portal_id='portal-bc',
        from_region_id='B',
        to_region_id='C',
        portal_geometry_authority_ref=stale_portal_ref,
        frequency_coefficients={frequency: 0.9 + 0.0j},
        provenance='stale portal fixture BC',
        provenance_state='measured',
        uncertainty='fixture',
    )

    response = _response(
        path,
        common,
        portal_geometry_authority_ref=current_portal_ref,
        portal_transfers={
            ('portal-ab', 'A', 'B'): transfer_ab,
            ('portal-bc', 'B', 'C'): transfer_bc,
        },
    )

    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert any(
        item.startswith('STALE_PORTAL_TRANSFER_AUTHORITY:')
        for item in response.unsupported_reasons
    )

    current_ab = build_portal_acoustic_transfer_authority(
        portal_id='portal-ab',
        from_region_id='A',
        to_region_id='B',
        portal_geometry_authority_ref=current_portal_ref,
        frequency_coefficients={frequency: 0.9 + 0.0j},
        provenance='current portal fixture AB',
        provenance_state='measured',
        uncertainty='fixture',
    )
    current_bc = build_portal_acoustic_transfer_authority(
        portal_id='portal-bc',
        from_region_id='B',
        to_region_id='C',
        portal_geometry_authority_ref=current_portal_ref,
        frequency_coefficients={frequency: 0.9 + 0.0j},
        provenance='current portal fixture BC',
        provenance_state='measured',
        uncertainty='fixture',
    )
    stale_execution_input = _execution_input(
        path,
        common,
        portal_authority_ref=stale_portal_ref,
    )
    execution_mismatch = _response(
        path,
        common,
        execution_input=stale_execution_input,
        path_artifact=_artifact(path, stale_execution_input),
        portal_geometry_authority_ref=current_portal_ref,
        portal_transfers={
            ('portal-ab', 'A', 'B'): current_ab,
            ('portal-bc', 'B', 'C'): current_bc,
        },
    )
    assert execution_mismatch.capability == 'UNSUPPORTED'
    assert (
        'EXECUTION_INPUT_PORTAL_AUTHORITY_MISMATCH'
        in execution_mismatch.unsupported_reasons
    )


def test_complex_sample_phase_and_path_artifact_band_are_fail_closed() -> None:
    with pytest.raises(ValueError, match='phase does not match'):
        ComplexTransferSample(
            frequency_hz=1000.0,
            magnitude=1.0,
            phase_rad=0.0,
            real=0.0,
            imag=1.0,
        )

    frequency = 1000.0
    common = _common((frequency,))
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='3',
    )
    execution_input = _execution_input(path, common)
    path_artifact = _artifact(
        path,
        execution_input,
        frequency_domain=FrequencyDomain(minimum_hz=100.0, maximum_hz=800.0),
    )
    response = build_deterministic_path_frequency_response(
        path_artifact=path_artifact,
        execution_input=execution_input,
        path_id=path.path_id,
        r120_geometry_ref=common['r120'],
        source_authority=common['source'],
        point_source_normalization=common['normalization'],
        receiver_authority=common['receiver'],
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        configuration=common['configuration'],
    )
    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert 'PATH_ARTIFACT_VALID_BAND_MISMATCH' in response.unsupported_reasons


def test_coherent_source_phase_reference_must_bind_volume_velocity_t0() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    capability = DirectivityCapability.model_construct(
        tier='analytic',
        data_format='analytic_model',
        provenance=None,
        data_asset_sha256=None,
        valid_domain=_directivity_domain((frequency,)),
        interpolation=None,
        coherent_phase=True,
        phase_reference='manufacturer_unaligned_phase_reference',
        analytic_model=ANALYTIC_OMNI_DIRECTIVITY_MODEL,
    )
    equipment = EquipmentDefinition.model_construct(
        definition_id='equipment:test-speaker',
        version='1',
        semantic_sha256=H2,
        directivity=capability,
    )
    common['source'] = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1', H2),
        equipment_definition=equipment,
        point_source_normalization=common['normalization'],
    )
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='4',
    )
    response = _response(path, common)
    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert 'SOURCE_PHASE_REFERENCE_MISMATCH' in response.unsupported_reasons


def test_explicit_reference_directivity_is_not_assumed_to_be_point_source_ratio() -> None:
    frequency = 1000.0
    common = _common((frequency,), source_tier='magnitude_only')
    dataset = _dataset(
        (frequency,),
        kind='magnitude_only',
        normalization_reference='explicit_reference_level',
    )
    common['dataset'] = dataset
    common['source'] = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1', H2),
        equipment_definition=_equipment((frequency,), tier='magnitude_only'),
        directivity_dataset=dataset,
        point_source_normalization=common['normalization'],
    )
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='5',
        bands=(_band(frequency, magnitude_linear=1.0, dataset=dataset),),
    )
    response = _response(path, common)
    assert response.capability == 'UNSUPPORTED'
    assert response.samples == ()
    assert 'DIRECTIVITY_NORMALIZATION_NOT_POINT_SOURCE_RATIO' in response.unsupported_reasons


def test_complex_directional_dataset_phase_multiplies_point_source_transfer() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    interpolation = InterpolationProvenance.model_construct(
        method='none',
        implementation='response-fixture-exact-grid',
        implementation_version='1',
        provenance=None,
    )
    directivity_capability = DirectivityCapability.model_construct(
        tier='complex',
        data_format='custom',
        provenance=None,
        data_asset_sha256=H3,
        valid_domain=_directivity_domain((frequency,)),
        interpolation=interpolation,
        coherent_phase=True,
        phase_reference='source_volume_velocity_t0',
        analytic_model=None,
    )
    equipment = EquipmentDefinition.model_construct(
        definition_id='equipment:test-speaker',
        version='1',
        semantic_sha256=H2,
        directivity=directivity_capability,
    )
    dataset = DirectivityDataset.model_construct(
        dataset_id='directivity:test-speaker',
        version='1',
        semantic_sha256=H3,
        equipment_definition_id='equipment:test-speaker',
        equipment_definition_version='1',
        equipment_definition_sha256=H2,
        kind='complex',
        coordinate_convention=DirectivityCoordinateConvention(
            angle_semantics='spherical_azimuth_elevation',
            horizontal_wrap='signed_180',
        ),
        normalization=DirectivityNormalization(
            source_magnitude_unit='db',
            reference='on_axis_per_frequency',
        ),
        phase_reference='source_volume_velocity_t0',
        valid_domain=_directivity_domain((frequency,)),
        interpolation=interpolation,
        samples=(
            DirectivitySample(
                frequency_hz=frequency,
                horizontal_angle_deg=0.0,
                vertical_angle_deg=0.0,
                magnitude_db=0.0,
                phase_deg=30.0,
            ),
        ),
    )
    common['dataset'] = dataset
    common['source'] = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1', H2),
        equipment_definition=equipment,
        directivity_dataset=dataset,
        point_source_normalization=common['normalization'],
    )
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='c',
        bands=(_band(frequency, magnitude_linear=1.0, dataset=dataset),),
    )

    response = _response(path, common)

    expected = _expected_monopole(
        frequency_hz=frequency,
        density_kg_m3=common['environment'].density_kg_m3,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        distance_m=2.0,
    ) * cmath.exp(1j * pi / 6.0)
    sample = response.samples[0]
    assert response.capability == 'COMPLEX_SUPPORTED'
    assert sample.complex_real_pa_per_m3_s == pytest.approx(expected.real, rel=1e-12)
    assert sample.complex_imag_pa_per_m3_s == pytest.approx(expected.imag, rel=1e-12)


def test_execution_input_source_and_environment_bindings_fail_closed() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    path = _path(
        length_m=2.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='7',
    )
    execution_input = _execution_input(path, common)
    path_artifact = _artifact(path, execution_input)

    stale_execution_input = _execution_input(
        path,
        common,
        semantic_sha256='f' * 64,
    )
    stale = _response(
        path,
        common,
        execution_input=stale_execution_input,
        path_artifact=path_artifact,
    )
    assert stale.capability == 'UNSUPPORTED'
    assert 'STALE_OR_MISMATCHED_EXECUTION_INPUT' in stale.unsupported_reasons

    mismatched_source = build_source_response_authority(
        source_entity_id='source-1',
        r110_source_ref=_ref('r110-source:source-1-revision', H3),
        equipment_definition=_equipment((frequency,)),
        point_source_normalization=common['normalization'],
    )
    source_mismatch = _response(
        path,
        common,
        source_authority=mismatched_source,
    )
    assert source_mismatch.capability == 'UNSUPPORTED'
    assert 'SOURCE_R110_AUTHORITY_MISMATCH' in source_mismatch.unsupported_reasons

    environment_mismatch_input = execution_input.model_copy(
        update={'sound_speed_m_s': 330.0}
    )
    environment_mismatch = _response(
        path,
        common,
        execution_input=environment_mismatch_input,
        path_artifact=_artifact(path, environment_mismatch_input),
    )
    assert environment_mismatch.capability == 'UNSUPPORTED'
    assert 'EXECUTION_INPUT_ENVIRONMENT_MISMATCH' in environment_mismatch.unsupported_reasons


def _z_wall(frequency: float, characteristic: float) -> AcousticMaterial:
    return AcousticMaterial(
        material_id='z-wall',
        provenance='explicit impedance fixture',
        version='1',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=frequency,
                resistance_pa_s_m=2.0 * characteristic,
                reactance_pa_s_m=characteristic,
            ),
        ),
    )


def test_impedance_authority_rejects_mismatched_evaluated_incidence() -> None:
    frequency = 500.0
    common = _common((frequency,), density_kg_m3=1.25, sound_speed_m_s=340.0)
    characteristic = (
        common['environment'].density_kg_m3
        * common['environment'].sound_speed_m_s
    )
    material = _z_wall(frequency, characteristic)
    # The authority was derived for cosine 0.5 (60 deg) but the path evaluates
    # normal incidence (cosine 1.0): one surface-global impedance authority
    # must never serve a different reflection angle silently.
    reflection = build_specific_impedance_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:z-wall', H2),
        material=material,
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        incidence_cosine=0.5,
    )
    path = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='9',
        surfaces=(SURFACE_A,),
        interactions=(
            DeterministicPathInteraction(
                kind='reflection',
                point=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
                surface_id=SURFACE_A,
            ),
        ),
    )
    plane = GeometricSurfacePlane(
        source_surface_id=SURFACE_A,
        point_m=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
        normal=Direction3(x=1.0, y=0.0, z=0.0),
        compiled_triangle_indices=(0,),
    )
    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
        execution_input=_execution_input(
            path,
            common,
            boundary_planes=(plane,),
        ),
    )
    assert response.capability == 'UNSUPPORTED'
    assert (
        f'REFLECTION_INCIDENCE_COSINE_MISMATCH:{SURFACE_A}'
        in response.unsupported_reasons
    )
    assert not response.samples
    # Evaluated incidence is still persisted on the rejected artifact.
    assert response.ordered_reflection_incidence_cosines == pytest.approx((1.0,))
    assert response.ordered_reflection_incidence_angles_deg == pytest.approx((0.0,))


def test_impedance_authority_fails_closed_when_incidence_underivable() -> None:
    frequency = 500.0
    common = _common((frequency,), density_kg_m3=1.25, sound_speed_m_s=340.0)
    characteristic = (
        common['environment'].density_kg_m3
        * common['environment'].sound_speed_m_s
    )
    material = _z_wall(frequency, characteristic)
    reflection = build_specific_impedance_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:z-wall', H2),
        material=material,
        environment=common['environment'],
        frequency_grid=common['frequency_grid'],
        incidence_cosine=1.0,
    )
    # No boundary planes in the execution input: incidence cannot be
    # re-evaluated, so the impedance authority must fail closed.
    path = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='a',
        surfaces=(SURFACE_A,),
    )
    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
        execution_input=_execution_input(path, common),
    )
    assert response.capability == 'UNSUPPORTED'
    assert (
        f'REFLECTION_INCIDENCE_UNDERIVABLE:{SURFACE_A}'
        in response.unsupported_reasons
    )


def test_evaluated_reflection_incidence_is_persisted_on_response() -> None:
    frequency = 1000.0
    common = _common((frequency,))
    material = AcousticMaterial(
        material_id='rigid-wall',
        provenance='analytic rigid fixture',
        version='1',
        wave_model='rigid',
    )
    reflection = build_rigid_surface_reflection_authority(
        source_surface_id=SURFACE_A,
        r120_geometry_ref=common['r120'],
        material_authority_ref=_ref('material:rigid', H2),
        material=material,
        frequency_grid=common['frequency_grid'],
    )
    sqrt2 = 1.0 / (2.0 ** 0.5)
    path = _path(
        length_m=3.0,
        sound_speed_m_s=common['environment'].sound_speed_m_s,
        path_id_seed='b',
        surfaces=(SURFACE_A,),
        interactions=(
            DeterministicPathInteraction(
                kind='reflection',
                point=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
                surface_id=SURFACE_A,
            ),
        ),
    )
    # Departure (1,0,0) against a (1/sqrt2, -1/sqrt2, 0) normal: 45 deg
    # oblique incidence.
    plane = GeometricSurfacePlane(
        source_surface_id=SURFACE_A,
        point_m=Position3(x_m=1.0, y_m=0.0, z_m=0.0),
        normal=Direction3(x=sqrt2, y=-sqrt2, z=0.0),
        compiled_triangle_indices=(0,),
    )
    response = _response(
        path,
        common,
        surface_reflections={SURFACE_A: reflection},
        execution_input=_execution_input(
            path,
            common,
            boundary_planes=(plane,),
        ),
    )
    assert response.capability == 'COMPLEX_SUPPORTED'
    assert response.ordered_reflection_incidence_cosines == pytest.approx(
        (sqrt2,), abs=1e-9
    )
    assert response.ordered_reflection_incidence_angles_deg == pytest.approx(
        (45.0,), abs=1e-9
    )
