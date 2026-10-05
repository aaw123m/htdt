"""Regression tests for the material-evidence compatibility gate (#570)."""

from __future__ import annotations

import pytest

from htdt.cad_material_evidence_compatibility import (
    COMPATIBILITY_MATRIX_VERSION,
    MaterialBuildUp,
    boundary_evidence_from_material_acoustic,
    boundary_evidence_from_surface_scattering,
    build_boundary_evidence,
    build_conversion_artifact,
    build_up_matches,
    evaluate_material_evidence_compatibility,
    evaluate_surface_scattering_for_ga,
    list_compatibility_matrix,
)
from htdt.cad_material_library import build_material_evidence
from htdt.cad_surface_scattering import build_surface_scattering_evidence


def _iso354(**overrides):
    """Diffuse-field ISO 354 absorption evidence (mineral wool class)."""

    base = dict(
        evidence_id='EV-354',
        method_class='iso_354_reverberation_room',
        standard_revision='ISO 354:2003',
        laboratory='Lab A',
        quantity='absorption_coefficient_diffuse_field',
        incidence='diffuse_reverberant',
        phase='magnitude_energy_only',
        band_center_hz=(125.0, 250.0, 500.0, 1000.0),
    )
    base.update(overrides)
    return build_boundary_evidence(**base)


def _iso10534(**overrides):
    """Normal-incidence ISO 10534-2 impedance-tube evidence."""

    base = dict(
        evidence_id='EV-10534',
        method_class='iso_10534_2_impedance_tube',
        standard_revision='ISO 10534-2:2023',
        laboratory='Lab B',
        quantity='absorption_coefficient_normal_incidence',
        incidence='normal',
        phase='magnitude_energy_only',
        band_center_hz=(125.0, 250.0, 500.0, 1000.0),
    )
    base.update(overrides)
    return build_boundary_evidence(**base)


def _impedance(**overrides):
    """Complex surface-impedance evidence (tube-measured, phase-bearing)."""

    base = dict(
        evidence_id='EV-Z',
        method_class='iso_10534_2_impedance_tube',
        standard_revision='ISO 10534-2:2023',
        laboratory='Lab B',
        quantity='surface_impedance',
        incidence='normal',
        phase='complex_impedance',
        band_center_hz=(63.0, 125.0, 250.0),
    )
    base.update(overrides)
    return build_boundary_evidence(**base)


# ---------------------------------------------------------------------------
# The canonical incompatibilities (issue §7/§11)
# ---------------------------------------------------------------------------


class TestCanonicalIncompatibilities:
    def test_iso354_vs_tube_not_equatable_through_same_boundary(self):
        # ISO 354 diffuse α must not feed a normal-incidence path, and ISO
        # 10534-2 αn must not feed a diffuse/arbitrary-incidence boundary.
        diffuse = _iso354()
        decision = evaluate_material_evidence_compatibility(
            diffuse, 'wave_local_reaction_boundary'
        )
        # diffuse data into a specific-incidence wave boundary: not direct
        assert decision.eligibility in (
            'INCOMPATIBLE',
            'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS',
        )
        assert decision.eligibility != 'DIRECTLY_COMPATIBLE'

        tube = _iso10534()
        decision = evaluate_material_evidence_compatibility(
            tube, 'geometric_arbitrary_incidence'
        )
        assert decision.eligibility == 'INCOMPATIBLE'
        assert (
            'NORMAL_INCIDENCE_DATA_USED_FOR_ARBITRARY_ANGLE_BOUNDARY'
            in decision.reasons
        )

    def test_tube_absorption_into_statistical_model_needs_conversion(self):
        tube = _iso10534()
        decision = evaluate_material_evidence_compatibility(
            tube, 'statistical_energy_model'
        )
        assert decision.eligibility != 'DIRECTLY_COMPATIBLE'

    def test_iso354_into_statistical_model_is_declared_assumptions(self):
        decision = evaluate_material_evidence_compatibility(
            _iso354(), 'statistical_energy_model'
        )
        assert decision.eligibility == 'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS'

    def test_diffusion_coefficient_never_a_scatter_fraction(self):
        evidence = build_boundary_evidence(
            evidence_id='EV-DIFF',
            method_class='iso_17497_2_directional_diffusion',
            standard_revision='ISO 17497-2:2012',
            quantity='directional_diffusion_coefficient',
            incidence='diffuse_reverberant',
            phase='magnitude_energy_only',
            band_center_hz=(250.0, 500.0),
        )
        decision = evaluate_material_evidence_compatibility(
            evidence, 'geometric_scatter_fraction'
        )
        assert decision.eligibility == 'INCOMPATIBLE'
        assert (
            'DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER'
            in decision.reasons
        )

    def test_energy_coefficient_has_no_phase_authority(self):
        decision = evaluate_material_evidence_compatibility(
            _iso354(), 'wave_complex_boundary'
        )
        assert decision.eligibility == 'INCOMPATIBLE'
        assert 'ENERGY_COEFFICIENT_HAS_NO_PHASE_AUTHORITY' in decision.reasons

    def test_complex_impedance_feeds_wave_boundary(self):
        decision = evaluate_material_evidence_compatibility(
            _impedance(), 'wave_complex_boundary'
        )
        assert decision.eligibility == 'DIRECTLY_COMPATIBLE'

    def test_derived_complex_needs_artifact(self):
        evidence = build_boundary_evidence(
            evidence_id='EV-DER',
            method_class='derived_conversion',
            quantity='surface_impedance',
            incidence='diffuse_reverberant',
            phase='derived_complex_model',
            band_center_hz=(63.0, 125.0),
        )
        decision = evaluate_material_evidence_compatibility(
            evidence, 'hybrid_lf_wave_boundary'
        )
        assert decision.eligibility == 'INSUFFICIENT_EVIDENCE'
        assert 'CONVERSION_ARTIFACT_MISSING' in decision.reasons

        artifact = build_conversion_artifact(
            source_evidence_id=evidence.evidence_id,
            source_evidence_sha256=evidence.evidence_sha256,
            source_quantity='surface_impedance',
            source_method='derived_conversion',
            target_consumer='hybrid_lf_wave_boundary',
            target_representation='complex impedance table',
            conversion_model='local-reaction fit',
            conversion_version='1.0',
            created_at_utc='2026-10-05T00:00:00Z',
        )
        decision = evaluate_material_evidence_compatibility(
            evidence, 'hybrid_lf_wave_boundary', conversion=artifact
        )
        assert decision.eligibility == 'DERIVED_WITH_UNCERTAINTY'
        assert decision.conversion_artifact_id == artifact.artifact_id


# ---------------------------------------------------------------------------
# Build-up identity and resonant-material scope (issue §7/§9)
# ---------------------------------------------------------------------------


class TestBuildUp:
    def test_exact_identity_required(self):
        a = MaterialBuildUp(product='wool', thickness_mm=50.0)
        b = MaterialBuildUp(product='wool', thickness_mm=50.0)
        assert build_up_matches(a, b)
        c = MaterialBuildUp(product='wool', thickness_mm=100.0)
        assert not build_up_matches(a, c)
        # A field absent on either side never silently matches.
        d = MaterialBuildUp(product='wool')
        assert not build_up_matches(a, d)

    def test_build_up_mismatch_is_incompatible(self):
        evidence = _iso354(
            build_up=MaterialBuildUp(product='wool', thickness_mm=50.0)
        )
        decision = evaluate_material_evidence_compatibility(
            evidence,
            'statistical_energy_model',
            required_build_up=MaterialBuildUp(
                product='wool', thickness_mm=100.0
            ),
        )
        assert decision.eligibility == 'INCOMPATIBLE'
        assert 'BUILD_UP_MISMATCH' in decision.reasons

    def test_resonant_iso354_outside_method_scope(self):
        evidence = _iso354(
            build_up=MaterialBuildUp(
                product='membrane absorber', is_resonant=True
            )
        )
        # ISO 354 excludes weakly damped resonators — the coefficient must
        # not silently become a wave boundary input.
        decision = evaluate_material_evidence_compatibility(
            evidence, 'wave_complex_boundary'
        )
        assert decision.eligibility == 'INCOMPATIBLE'
        assert 'RESONANT_MATERIAL_OUTSIDE_METHOD_SCOPE' in decision.reasons


# ---------------------------------------------------------------------------
# Fail-closed unknowns and method honesty (issue §1/§2)
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_unknown_method_is_insufficient(self):
        evidence = build_boundary_evidence(
            evidence_id='EV-UNK',
            method_class='unknown_method',
            quantity='absorption_coefficient_diffuse_field',
            incidence='diffuse_reverberant',
            phase='magnitude_energy_only',
            band_center_hz=(500.0,),
        )
        decision = evaluate_material_evidence_compatibility(
            evidence, 'statistical_energy_model'
        )
        assert decision.eligibility == 'INSUFFICIENT_EVIDENCE'
        assert 'METHOD_UNKNOWN' in decision.reasons

    def test_method_quantity_honesty(self):
        # ISO 354 cannot produce a normal-incidence coefficient.
        with pytest.raises(ValueError, match='cannot produce'):
            build_boundary_evidence(
                evidence_id='EV-BAD',
                method_class='iso_354_reverberation_room',
                quantity='absorption_coefficient_normal_incidence',
                incidence='diffuse_reverberant',
                phase='magnitude_energy_only',
                band_center_hz=(500.0,),
            )

    def test_iso354_cannot_claim_complex_phase(self):
        with pytest.raises(ValueError, match='complex phase'):
            build_boundary_evidence(
                evidence_id='EV-BAD2',
                method_class='iso_354_reverberation_room',
                quantity='absorption_coefficient_diffuse_field',
                incidence='diffuse_reverberant',
                phase='complex_reflection',
                band_center_hz=(500.0,),
            )

    def test_single_number_rating_never_frequency_input(self):
        evidence = build_boundary_evidence(
            evidence_id='EV-NRC',
            method_class='manufacturer_declared',
            quantity='single_number_rating',
            incidence='unknown',
            phase='unknown_phase',
            band_center_hz=(500.0,),
        )
        for consumer in (
            'wave_complex_boundary',
            'geometric_arbitrary_incidence',
            'statistical_energy_model',
        ):
            decision = evaluate_material_evidence_compatibility(
                evidence, consumer
            )
            assert decision.eligibility == 'INCOMPATIBLE'
            assert (
                'SINGLE_NUMBER_RATING_NOT_FREQUENCY_DEPENDENT'
                in decision.reasons
            )

    def test_manufacturer_declared_never_direct(self):
        evidence = build_boundary_evidence(
            evidence_id='EV-MFG',
            method_class='manufacturer_declared',
            quantity='scattering_coefficient_random_incidence',
            incidence='diffuse_reverberant',
            phase='magnitude_energy_only',
            band_center_hz=(250.0, 500.0),
        )
        decision = evaluate_material_evidence_compatibility(
            evidence, 'geometric_scatter_fraction'
        )
        assert decision.eligibility == 'COMPATIBLE_WITH_DECLARED_ASSUMPTIONS'
        assert 'DECLARED_METHOD_NOT_MEASURED' in decision.reasons

    def test_standard_revision_missing_is_flagged(self):
        evidence = _iso354(standard_revision=None)
        decision = evaluate_material_evidence_compatibility(
            evidence, 'statistical_energy_model'
        )
        assert 'STANDARD_REVISION_UNKNOWN' in decision.reasons


# ---------------------------------------------------------------------------
# Matrix is inspectable and versioned (issue §3)
# ---------------------------------------------------------------------------


def test_compatibility_matrix_is_inspectable():
    matrix = list_compatibility_matrix()
    assert matrix['matrix_version'] == COMPATIBILITY_MATRIX_VERSION
    assert any(
        rule['consumer'] == 'geometric_scatter_fraction'
        for rule in matrix['rules']
    )


# ---------------------------------------------------------------------------
# Composition with #771 and #1032 authorities
# ---------------------------------------------------------------------------


class TestAuthorityComposition:
    def test_material_library_mapping(self):
        evidence = build_material_evidence(
            material_id='wool-50',
            quantity='random_incidence_absorption_coefficient',
            frequency_hz=(125.0, 250.0, 500.0),
            values=(0.3, 0.6, 0.8),
            unit_label='αs',
            incidence='random',
            provenance_class='laboratory_measured',
            source_label='Lab A report 1',
            created_at_utc='2026-01-01T00:00:00Z',
        )
        boundary = boundary_evidence_from_material_acoustic(
            evidence,
            method_class='iso_354_reverberation_room',
            standard_revision='ISO 354:2003',
        )
        assert boundary.quantity == 'absorption_coefficient_diffuse_field'
        assert boundary.incidence == 'diffuse_reverberant'
        assert boundary.phase == 'magnitude_energy_only'

    def test_scattering_ga_fraction_direct(self):
        evidence = build_surface_scattering_evidence(
            evidence_id='EV-SCAT',
            material_id='panel-1',
            quantity_kind='random_incidence_scattering_coefficient',
            values=(0.4, 0.5),
            band_center_hz=(250.0, 500.0),
            standard='iso_17497_1',
            source='Lab S',
            provenance='measured per ISO 17497-1',
            incidence_semantics='random_or_diffuse_incidence',
        )
        decision = evaluate_surface_scattering_for_ga(evidence)
        assert decision.eligibility == 'DIRECTLY_COMPATIBLE'

    def test_diffusion_evidence_refused_for_ga(self):
        evidence = build_surface_scattering_evidence(
            evidence_id='EV-DIF',
            material_id='panel-1',
            quantity_kind='directional_diffusion_coefficient',
            values=(0.7, 0.8),
            band_center_hz=(250.0, 500.0),
            standard='iso_17497_2',
            source='Lab S',
            provenance='measured per ISO 17497-2',
            incidence_semantics='angle_specific',
            incidence_angle_deg=30.0,
        )
        decision = evaluate_surface_scattering_for_ga(evidence)
        assert decision.eligibility == 'INCOMPATIBLE'
        assert (
            'DIFFUSION_COEFFICIENT_NOT_SCATTERING_PARAMETER'
            in decision.reasons
        )
