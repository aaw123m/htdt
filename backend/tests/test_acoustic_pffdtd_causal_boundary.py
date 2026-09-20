from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from htdt.acoustic_pffdtd_causal_boundary import (
    CausalAdmittanceBranch,
    CausalBoundaryFitProvenance,
    build_causal_frequency_dependent_boundary_authority,
    compile_causal_boundary_to_pffdtd,
    evaluate_normalized_admittance,
    pffdtd_causal_boundary_mapping_authority_payload,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _ref(seed: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'fixture:{seed}',
        authority_version=version,
        semantic_hash_sha256=seed * 64,
    )


def _authority():
    return build_causal_frequency_dependent_boundary_authority(
        source_scene_revision_id='scene-revision-1',
        source_scene_content_hash='a' * 64,
        source_surface_id='semantic-surface:' + 'b' * 64,
        material_id='analytic-r130c-rlc',
        material_version='1',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        branches=(
            CausalAdmittanceBranch(
                d_seconds=8.0e-4,
                e_dimensionless=1.5,
                f_per_second=120.0,
            ),
        ),
        evidence_state='analytic',
        provenance={
            'basis': 'closed_form_positive_real_series_RLC',
            'fixture': 'r130c-unit-v1',
        },
        uncertainty=None,
    )


def test_causal_boundary_identity_and_frequency_response_are_deterministic() -> None:
    first = _authority()
    second = _authority()

    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.as_external_ref().semantic_hash_sha256 == first.semantic_sha256
    assert first.interpolation_semantics == 'analytic_rational_evaluation_no_interpolation'
    assert first.extrapolation_rule == 'forbidden'
    assert first.causality_status == 'CAUSAL'
    assert first.passivity_status == 'PASSIVE'
    assert first.stability_status == 'STABLE'

    values = evaluate_normalized_admittance(first, (40.0, 80.0))
    assert values[0] != values[1]
    assert all(value.real > 0.0 for value in values)
    reflections = tuple((1.0 - value) / (1.0 + value) for value in values)
    assert all(abs(value) <= 1.0 for value in reflections)
    assert not math.isclose(
        math.atan2(reflections[0].imag, reflections[0].real),
        math.atan2(reflections[1].imag, reflections[1].real),
    )


def test_causal_boundary_rejects_nonpassive_or_frequency_independent_contracts() -> None:
    with pytest.raises(ValidationError):
        CausalAdmittanceBranch(
            d_seconds=-1.0e-3,
            e_dimensionless=1.0,
            f_per_second=0.0,
        )
    with pytest.raises(ValidationError):
        CausalAdmittanceBranch(
            d_seconds=1.0e-3,
            e_dimensionless=0.0,
            f_per_second=0.0,
        )

    with pytest.raises(ValidationError, match='frequency-dependent'):
        build_causal_frequency_dependent_boundary_authority(
            source_scene_revision_id='scene-revision-1',
            source_scene_content_hash='a' * 64,
            source_surface_id='semantic-surface:' + 'b' * 64,
            material_id='r130b-belongs-elsewhere',
            material_version='1',
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=40.0,
                maximum_hz=80.0,
            ),
            branches=(
                CausalAdmittanceBranch(
                    d_seconds=0.0,
                    e_dimensionless=2.0,
                    f_per_second=0.0,
                ),
            ),
            evidence_state='analytic',
            provenance={'basis': 'negative-fixture'},
        )


def test_fitted_boundary_requires_full_fit_provenance() -> None:
    with pytest.raises(ValidationError, match='fit provenance'):
        build_causal_frequency_dependent_boundary_authority(
            source_scene_revision_id='scene-revision-1',
            source_scene_content_hash='a' * 64,
            source_surface_id='semantic-surface:' + 'b' * 64,
            material_id='fit-missing-audit',
            material_version='1',
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=40.0,
                maximum_hz=80.0,
            ),
            branches=(
                CausalAdmittanceBranch(
                    d_seconds=8.0e-4,
                    e_dimensionless=1.5,
                    f_per_second=120.0,
                ),
            ),
            evidence_state='fitted',
            provenance={'basis': 'negative-fixture'},
        )

    fit = CausalBoundaryFitProvenance(
        source_data_sha256='c' * 64,
        fitting_algorithm='bounded-vector-fit',
        fitting_algorithm_version='1',
        order=1,
        error_metric='complex_admittance_l2',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        passivity_correction_applied=False,
        residual=0.002,
        residual_unit='normalized_admittance_rms',
    )
    fitted = build_causal_frequency_dependent_boundary_authority(
        source_scene_revision_id='scene-revision-1',
        source_scene_content_hash='a' * 64,
        source_surface_id='semantic-surface:' + 'b' * 64,
        material_id='audited-fit',
        material_version='1',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=40.0,
            maximum_hz=80.0,
        ),
        branches=(
            CausalAdmittanceBranch(
                d_seconds=8.0e-4,
                e_dimensionless=1.5,
                f_per_second=120.0,
            ),
        ),
        evidence_state='fitted',
        provenance={'basis': 'bounded-fit-fixture'},
        fit_provenance=fit,
    )
    assert fitted.fit_provenance == fit


def test_causal_pffdtd_compilation_is_exact_deterministic_and_band_limited() -> None:
    authority = _authority()
    mapping_ref = _ref('d', version='1')
    source_ref = authority.as_external_ref()
    kwargs = {
        'authority': authority,
        'source_boundary_authority_ref': source_ref,
        'mapping_authority_ref': mapping_ref,
        'requested_frequency_hz': (40.0, 80.0),
        'density_kg_m3': 1.2,
        'density_authority_ref': _ref('e'),
        'sound_speed_m_s': 343.0,
        'sound_speed_authority_ref': _ref('f'),
        'expected_scene_revision_id': 'scene-revision-1',
        'expected_scene_content_hash': 'a' * 64,
        'expected_surface_id': 'semantic-surface:' + 'b' * 64,
    }
    first = compile_causal_boundary_to_pffdtd(**kwargs)
    second = compile_causal_boundary_to_pffdtd(**kwargs)

    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.semantic_sha256 != authority.semantic_sha256
    assert first.def_coefficients == ((8.0e-4, 1.5, 120.0),)
    assert first.mapping_id == pffdtd_causal_boundary_mapping_authority_payload()['mapping_id']
    assert first.normalized_admittance_samples[0].imag != first.normalized_admittance_samples[1].imag
    assert all(
        sample.real > 0.0
        for sample in first.normalized_admittance_samples
    )

    with pytest.raises(ValueError, match='outside causal boundary valid band'):
        compile_causal_boundary_to_pffdtd(
            **{**kwargs, 'requested_frequency_hz': (40.0, 100.0)}
        )
    with pytest.raises(ValueError, match='SceneRevision/surface'):
        compile_causal_boundary_to_pffdtd(
            **{**kwargs, 'expected_scene_revision_id': 'stale-scene'}
        )


def test_causal_compilation_hash_changes_when_boundary_changes() -> None:
    authority = _authority()
    changed = build_causal_frequency_dependent_boundary_authority(
        source_scene_revision_id=authority.source_scene_revision_id,
        source_scene_content_hash=authority.source_scene_content_hash,
        source_surface_id=authority.source_surface_id,
        material_id=authority.material_id,
        material_version='2',
        valid_frequency_domain=authority.valid_frequency_domain,
        branches=(
            CausalAdmittanceBranch(
                d_seconds=9.0e-4,
                e_dimensionless=1.5,
                f_per_second=120.0,
            ),
        ),
        evidence_state='analytic',
        provenance=authority.provenance,
    )
    common = {
        'mapping_authority_ref': _ref('d', version='1'),
        'requested_frequency_hz': (40.0, 80.0),
        'density_kg_m3': 1.2,
        'density_authority_ref': _ref('e'),
        'sound_speed_m_s': 343.0,
        'sound_speed_authority_ref': _ref('f'),
        'expected_scene_revision_id': 'scene-revision-1',
        'expected_scene_content_hash': 'a' * 64,
        'expected_surface_id': 'semantic-surface:' + 'b' * 64,
    }
    first = compile_causal_boundary_to_pffdtd(
        authority=authority,
        source_boundary_authority_ref=authority.as_external_ref(),
        **common,
    )
    second = compile_causal_boundary_to_pffdtd(
        authority=changed,
        source_boundary_authority_ref=changed.as_external_ref(),
        **common,
    )
    assert first.semantic_sha256 != second.semantic_sha256
