"""#1032: scattering vs diffusion quantity-kind authority."""

from __future__ import annotations

import pytest

from htdt.cad_surface_scattering import (
    SurfaceScatteringEvidence,
    build_directional_scattering_kernel,
    build_surface_scattering_evidence,
    ga_scatter_fraction,
    solver_scattering_representation,
    StochasticScatteringModel,
)


def _evidence(**overrides):
    kwargs = {
        'surface_id': 'surface-1',
        'quantity_kind': 'random_incidence_scattering_coefficient',
        'band_center_hz': (500.0, 1000.0),
        'values': (0.2, 0.4),
        'standard': 'iso_17497_1',
        'source': 'lab-report-7',
        'provenance': 'reverberation-room measurement',
    }
    kwargs.update(overrides)
    return build_surface_scattering_evidence(**kwargs)


def test_scalar_scattering_kind_feeds_ga_solver():
    evidence = _evidence()
    assert ga_scatter_fraction(evidence) == pytest.approx(0.3)
    assert solver_scattering_representation(evidence) == 'scalar_coefficient'
    assert evidence.evidence_sha256 == evidence.evidence_sha256


def test_iso_17497_2_diffusion_is_never_a_ga_scatter_fraction():
    diffusion = _evidence(
        quantity_kind='directional_diffusion_coefficient',
        standard='iso_17497_2',
        method='polar uniformity',
    )
    assert ga_scatter_fraction(diffusion) is None
    assert solver_scattering_representation(diffusion) == 'display_only'


def test_iso_17497_2_cannot_be_labelled_random_incidence_scattering():
    with pytest.raises(ValueError, match='ISO 17497-2'):
        _evidence(
            quantity_kind='random_incidence_scattering_coefficient',
            standard='iso_17497_2',
        )


def test_explicit_geometry_only_never_downgrades_to_scalar():
    evidence = _evidence(
        quantity_kind='explicit_geometry_only',
        values=(),
        band_center_hz=(),
        standard='declared_no_standard',
    )
    assert ga_scatter_fraction(evidence) is None
    # No explicit solve requested -> unknown, not a fabricated scalar.
    assert solver_scattering_representation(evidence) == 'unknown'
    # Explicit geometry wins over the scalar — no double counting.
    assert (
        solver_scattering_representation(evidence, explicit_geometry=True)
        == 'explicit_geometry'
    )


def test_directional_kernel_requires_directional_evidence_and_owner():
    directional = _evidence(
        quantity_kind='directional_scattering_distribution',
        values=(),
        band_center_hz=(),
        incidence_semantics='angle_specific',
        incidence_angle_deg=45.0,
    )
    kernel = build_directional_scattering_kernel(
        evidence_id=directional.evidence_id,
        frequency_hz=1000.0,
        incidence_azimuth_deg=0.0,
        incidence_elevation_deg=45.0,
        orientation_deg=90.0,
        outgoing_samples=((0.0, 0.0, 0.8), (30.0, 0.0, 0.2)),
        source='polar measurement run 3',
    )
    assert (
        solver_scattering_representation(directional, kernel=kernel)
        == 'directional_kernel'
    )
    # A kernel cannot drive scattering for scalar-only evidence.
    with pytest.raises(ValueError, match='directional_scattering'):
        solver_scattering_representation(_evidence(), kernel=kernel)
    # Or belong to different evidence.
    with pytest.raises(ValueError, match='belong'):
        solver_scattering_representation(
            _evidence(
                quantity_kind='directional_scattering_distribution',
                values=(),
                band_center_hz=(),
                incidence_semantics='angle_specific',
                incidence_angle_deg=45.0,
            ),
            kernel=kernel,
        )


def test_stochastic_model_is_seeded_and_kernel_bound():
    with pytest.raises(ValueError, match='kernel_id'):
        StochasticScatteringModel(
            distribution='measured_polar_kernel',
            seed=7,
            sample_count=1000,
        )
    model = StochasticScatteringModel(
        distribution='lambertian', seed=7, sample_count=1000
    )
    assert model.seed == 7 and model.model_version == 'stochastic_scattering_v1'


def test_evidence_requires_a_bound_subject_and_aligned_bands():
    with pytest.raises(ValueError, match='surface/material/treatment'):
        _evidence(surface_id=None)
    with pytest.raises(ValueError, match='align'):
        _evidence(values=(0.5,))
    with pytest.raises(ValueError, match='hash mismatch'):
        SurfaceScatteringEvidence(
            evidence_id='ev-tampered',
            surface_id='surface-1',
            quantity_kind='random_incidence_scattering_coefficient',
            band_center_hz=(500.0,),
            values=(0.5,),
            source='lab-report-7',
            provenance='test',
            evidence_sha256='0' * 64,
        )
