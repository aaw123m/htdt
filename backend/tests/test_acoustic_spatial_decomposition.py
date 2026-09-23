from __future__ import annotations

from cmath import exp
from hashlib import sha256
from math import pi

import pytest

from htdt.acoustic_spatial_decomposition import (
    ComplexPressureValue,
    ExactDecompositionAuthorityRef,
    SpatialDecompositionConditioningPolicy,
    SpatialPressureSamplePoint,
    TwoPointFrequencyPressure,
    build_spatial_field_decomposition_spec,
    build_two_point_complex_pressure_evidence,
    decompose_planar_normal_incidence_two_point,
)
from htdt.cad_scene import Direction3, Position3


C_M_S = 343.0


def _ref(kind: str, identity: str) -> ExactDecompositionAuthorityRef:
    digest = sha256(f'{kind}:{identity}'.encode('utf-8')).hexdigest()
    return ExactDecompositionAuthorityRef(
        authority_kind=kind,
        authority_id=f'{kind}:{identity}',
        authority_version='1',
        semantic_sha256=digest,
    )


def _value(value: complex) -> ComplexPressureValue:
    return ComplexPressureValue(real=value.real, imag=value.imag)


def _synthetic_pressures(
    *,
    frequencies_hz: tuple[float, ...],
    d1_m: float,
    d2_m: float,
    incident: tuple[complex, ...],
    reflected: tuple[complex, ...],
) -> tuple[TwoPointFrequencyPressure, ...]:
    assert len(frequencies_hz) == len(incident) == len(reflected)
    samples = []
    for frequency_hz, incident_value, reflected_value in zip(
        frequencies_hz,
        incident,
        reflected,
        strict=True,
    ):
        k = 2.0 * pi * frequency_hz / C_M_S

        def pressure(distance_m: float) -> complex:
            return (
                incident_value * exp(-1j * k * distance_m)
                + reflected_value * exp(+1j * k * distance_m)
            )

        samples.append(
            TwoPointFrequencyPressure(
                frequency_hz=frequency_hz,
                sample_1_pressure=_value(pressure(d1_m)),
                sample_2_pressure=_value(pressure(d2_m)),
            )
        )
    return tuple(samples)


def _case(
    *,
    frequencies_hz: tuple[float, ...] = (100.0,),
    d1_m: float = 0.1,
    d2_m: float = 0.3,
    incident: tuple[complex, ...] = (1.0 + 0.0j,),
    reflected: tuple[complex, ...] = (0.0 + 0.0j,),
    outward_normal: Direction3 | None = None,
    sample_sign: float = 1.0,
    evidence_phasor: str = 'exp(-i*omega*t)',
    analysis_kernel: str = 'exp(+i*omega*t)',
    policy: SpatialDecompositionConditioningPolicy | None = None,
):
    sample_ids = ('p-near', 'p-far')
    evidence = build_two_point_complex_pressure_evidence(
        sample_ids=sample_ids,
        frequencies=_synthetic_pressures(
            frequencies_hz=frequencies_hz,
            d1_m=d1_m,
            d2_m=d2_m,
            incident=incident,
            reflected=reflected,
        ),
    )
    raw_ref = ExactDecompositionAuthorityRef(
        authority_kind='raw_complex_pressure_evidence',
        authority_id=evidence.evidence_id,
        authority_version=evidence.authority_version,
        semantic_sha256=evidence.semantic_sha256,
    )
    normal = outward_normal or Direction3(x=-1.0, y=0.0, z=0.0)
    spec = build_spatial_field_decomposition_spec(
        fixture_authority_ref=_ref('analytic_fixture', 'two-point-v1'),
        prediction_authority_ref=None,
        raw_complex_pressure_evidence_ref=raw_ref,
        timing_fourier_authority_ref=_ref(
            'timing_fourier',
            f'{evidence_phasor}:{analysis_kernel}',
        ),
        normalization_authority_ref=_ref(
            'pressure_normalization',
            'analytic-pa-v1',
        ),
        sound_speed_authority_ref=_ref('sound_speed', '343mps-v1'),
        pressure_evidence_source='analytic_synthetic',
        solver_backend_provenance_ref=None,
        boundary_plane_point=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        surface_outward_normal=normal,
        sample_points=(
            SpatialPressureSamplePoint(
                sample_id=sample_ids[0],
                position=Position3(
                    x_m=sample_sign * d1_m,
                    y_m=0.0,
                    z_m=0.0,
                ),
                inward_distance_m=d1_m,
            ),
            SpatialPressureSamplePoint(
                sample_id=sample_ids[1],
                position=Position3(
                    x_m=sample_sign * d2_m,
                    y_m=0.0,
                    z_m=0.0,
                ),
                inward_distance_m=d2_m,
            ),
        ),
        frequency_grid_hz=frequencies_hz,
        sound_speed_m_s=C_M_S,
        pressure_evidence_phasor_convention=evidence_phasor,
        pressure_analysis_kernel=analysis_kernel,
        complex_pressure_normalization='Pa, analytic synthetic amplitude',
        conditioning_policy=policy,
    )
    return evidence, spec, decompose_planar_normal_incidence_two_point(
        spec=spec,
        pressure_evidence=evidence,
    )


def _complex(model: ComplexPressureValue | None) -> complex:
    assert model is not None
    return model.as_complex()


def test_pure_incident_recovers_incident_and_zero_reflection() -> None:
    incident = 1.7 * exp(0.31j)
    _, _, result = _case(
        incident=(incident,),
        reflected=(0.0 + 0.0j,),
    )

    item = result.frequency_results[0]
    assert result.overall_status == 'AVAILABLE'
    assert item.status == 'AVAILABLE'
    assert _complex(item.incident_pressure) == pytest.approx(incident, abs=1.0e-12)
    assert _complex(item.reflected_pressure) == pytest.approx(0.0j, abs=1.0e-12)
    assert _complex(item.reflection_coefficient) == pytest.approx(0.0j, abs=1.0e-12)


def test_known_nonzero_reflection_recovers_both_components() -> None:
    _, _, result = _case(
        incident=(2.0 + 0.0j,),
        reflected=(0.5 + 0.0j,),
    )

    item = result.frequency_results[0]
    assert _complex(item.incident_pressure) == pytest.approx(2.0 + 0.0j, abs=1.0e-12)
    assert _complex(item.reflected_pressure) == pytest.approx(0.5 + 0.0j, abs=1.0e-12)
    assert _complex(item.reflection_coefficient) == pytest.approx(
        0.25 + 0.0j,
        abs=1.0e-12,
    )


def test_phase_bearing_reflection_is_recovered_without_conjugation() -> None:
    incident = 1.3 * exp(0.42j)
    reflected = 0.48 * exp(-0.73j)
    _, _, result = _case(
        incident=(incident,),
        reflected=(reflected,),
    )

    item = result.frequency_results[0]
    assert _complex(item.incident_pressure) == pytest.approx(incident, abs=1.0e-12)
    assert _complex(item.reflected_pressure) == pytest.approx(reflected, abs=1.0e-12)
    assert _complex(item.reflection_coefficient) == pytest.approx(
        reflected / incident,
        abs=1.0e-12,
    )


def test_multiple_frequencies_use_exact_frequency_grid_and_sound_speed() -> None:
    frequencies = (63.0, 137.0, 251.0)
    incident = (
        1.0 + 0.2j,
        0.7 - 0.4j,
        1.8 + 0.1j,
    )
    reflected = (
        0.1 - 0.05j,
        -0.2 + 0.3j,
        0.4 - 0.6j,
    )
    _, spec, result = _case(
        frequencies_hz=frequencies,
        incident=incident,
        reflected=reflected,
    )

    assert spec.frequency_grid_hz == frequencies
    assert spec.sound_speed_m_s == C_M_S
    assert result.overall_status == 'AVAILABLE'
    for item, expected_i, expected_r in zip(
        result.frequency_results,
        incident,
        reflected,
        strict=True,
    ):
        assert _complex(item.incident_pressure) == pytest.approx(
            expected_i,
            abs=1.0e-12,
        )
        assert _complex(item.reflected_pressure) == pytest.approx(
            expected_r,
            abs=1.0e-12,
        )


def test_sample_spacing_is_authority_and_both_well_conditioned_cases_recover() -> None:
    incident = 0.9 + 0.6j
    reflected = -0.2 + 0.35j
    _, spec_a, result_a = _case(
        d1_m=0.08,
        d2_m=0.21,
        incident=(incident,),
        reflected=(reflected,),
    )
    _, spec_b, result_b = _case(
        d1_m=0.08,
        d2_m=0.29,
        incident=(incident,),
        reflected=(reflected,),
    )

    assert spec_a.semantic_sha256 != spec_b.semantic_sha256
    assert result_a.overall_status == 'AVAILABLE'
    assert result_b.overall_status == 'AVAILABLE'
    assert _complex(result_a.frequency_results[0].incident_pressure) == pytest.approx(
        incident,
        abs=1.0e-12,
    )
    assert _complex(result_b.frequency_results[0].reflected_pressure) == pytest.approx(
        reflected,
        abs=1.0e-12,
    )


def test_half_wavelength_spacing_is_blocked_as_singular_without_epsilon_substitution() -> None:
    _, _, result = _case(
        frequencies_hz=(343.0,),
        d1_m=0.0,
        d2_m=0.5,
        incident=(1.0 + 0.0j,),
        reflected=(0.25 + 0.1j,),
    )

    item = result.frequency_results[0]
    assert result.overall_status == 'BLOCKED'
    assert item.status == 'BLOCKED'
    assert item.conditioning.state == 'SINGULAR'
    assert item.conditioning.abs_determinant < item.conditioning.minimum_abs_determinant
    assert item.conditioning.condition_number_2 is None
    assert item.conditioning.condition_number_is_infinite is True
    assert item.incident_pressure is None
    assert item.reflected_pressure is None
    assert item.reflection_coefficient is None
    assert 'determinant' in item.reasons[0]


@pytest.mark.parametrize('incident', [0.0 + 0.0j, 1.0e-14 + 0.0j])
def test_zero_or_near_zero_incident_blocks_reflection_coefficient(
    incident: complex,
) -> None:
    _, _, result = _case(
        incident=(incident,),
        reflected=(0.4 - 0.2j,),
    )

    item = result.frequency_results[0]
    assert result.overall_status == 'BLOCKED'
    assert item.status == 'BLOCKED'
    assert item.conditioning.state == 'INCIDENT_UNDEFINED'
    assert item.reflection_coefficient is None
    assert _complex(item.reflected_pressure) == pytest.approx(
        0.4 - 0.2j,
        abs=1.0e-12,
    )
    assert 'undefined' in item.reasons[0]


def test_normal_and_fourier_convention_are_identity_and_no_implicit_conversion_occurs() -> None:
    evidence, canonical_spec, canonical_result = _case(
        incident=(1.0 + 0.2j,),
        reflected=(0.3 - 0.1j,),
    )
    assert canonical_result.overall_status == 'AVAILABLE'

    # Flipping the normal while also moving the samples to the corresponding
    # inward side creates a distinct, valid mathematical authority.
    _, flipped_spec, flipped_result = _case(
        incident=(1.0 + 0.2j,),
        reflected=(0.3 - 0.1j,),
        outward_normal=Direction3(x=1.0, y=0.0, z=0.0),
        sample_sign=-1.0,
    )
    assert flipped_result.overall_status == 'AVAILABLE'
    assert flipped_spec.semantic_sha256 != canonical_spec.semantic_sha256

    # The same physical sample positions with the wrong outward normal are not
    # silently reinterpreted.
    with pytest.raises(ValueError, match='outside canonical acoustic domain'):
        _case(
            incident=(1.0 + 0.2j,),
            reflected=(0.3 - 0.1j,),
            outward_normal=Direction3(x=1.0, y=0.0, z=0.0),
            sample_sign=1.0,
        )

    # A backend/evidence convention mismatch has a different identity and is
    # explicitly unsupported. No complex conjugation is applied implicitly.
    mismatched_spec = build_spatial_field_decomposition_spec(
        fixture_authority_ref=canonical_spec.fixture_authority_ref,
        prediction_authority_ref=None,
        raw_complex_pressure_evidence_ref=canonical_spec.raw_complex_pressure_evidence_ref,
        timing_fourier_authority_ref=_ref(
            'timing_fourier',
            'exp(+i*omega*t):exp(-i*omega*t)',
        ),
        normalization_authority_ref=canonical_spec.normalization_authority_ref,
        sound_speed_authority_ref=canonical_spec.sound_speed_authority_ref,
        pressure_evidence_source='analytic_synthetic',
        solver_backend_provenance_ref=None,
        boundary_plane_point=canonical_spec.boundary_plane_point,
        surface_outward_normal=canonical_spec.surface_outward_normal,
        sample_points=canonical_spec.sample_points,
        frequency_grid_hz=canonical_spec.frequency_grid_hz,
        sound_speed_m_s=canonical_spec.sound_speed_m_s,
        pressure_evidence_phasor_convention='exp(+i*omega*t)',
        pressure_analysis_kernel='exp(-i*omega*t)',
        complex_pressure_normalization=canonical_spec.complex_pressure_normalization,
        conditioning_policy=canonical_spec.conditioning_policy,
    )
    mismatch_result = decompose_planar_normal_incidence_two_point(
        spec=mismatched_spec,
        pressure_evidence=evidence,
    )
    assert mismatched_spec.semantic_sha256 != canonical_spec.semantic_sha256
    assert mismatch_result.overall_status == 'UNSUPPORTED'
    item = mismatch_result.frequency_results[0]
    assert item.status == 'UNSUPPORTED'
    assert item.conditioning.state == 'UNSUPPORTED_CONVENTION'
    assert item.incident_pressure is None
    assert 'explicit adapter conversion' in item.reasons[0]


def test_same_input_produces_same_spec_evidence_and_result_hashes() -> None:
    args = {
        'frequencies_hz': (80.0, 160.0),
        'd1_m': 0.05,
        'd2_m': 0.19,
        'incident': (1.0 + 0.2j, 0.8 - 0.1j),
        'reflected': (0.3 - 0.1j, -0.2 + 0.4j),
    }
    evidence_a, spec_a, result_a = _case(**args)
    evidence_b, spec_b, result_b = _case(**args)

    assert evidence_a.evidence_id == evidence_b.evidence_id
    assert evidence_a.semantic_sha256 == evidence_b.semantic_sha256
    assert spec_a.spec_id == spec_b.spec_id
    assert spec_a.semantic_sha256 == spec_b.semantic_sha256
    assert result_a.result_id == result_b.result_id
    assert result_a.semantic_sha256 == result_b.semantic_sha256


def test_reconstruction_residual_and_reconstructed_pressures_are_explicit() -> None:
    evidence, _, result = _case(
        frequencies_hz=(91.0, 203.0),
        incident=(1.1 + 0.3j, 0.6 - 0.7j),
        reflected=(-0.15 + 0.2j, 0.4 + 0.1j),
    )

    assert result.overall_status == 'AVAILABLE'
    for source, item in zip(
        evidence.frequencies,
        result.frequency_results,
        strict=True,
    ):
        assert item.reconstruction_residual_max_abs is not None
        assert item.reconstruction_residual_max_abs <= 1.0e-12
        assert _complex(item.reconstructed_sample_1_pressure) == pytest.approx(
            source.sample_1_pressure.as_complex(),
            abs=1.0e-12,
        )
        assert _complex(item.reconstructed_sample_2_pressure) == pytest.approx(
            source.sample_2_pressure.as_complex(),
            abs=1.0e-12,
        )
