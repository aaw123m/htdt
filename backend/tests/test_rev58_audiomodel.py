"""REV58-AUDIOMODEL regression tests — #654 acoustic-reference origin,
#655 source near/far-field applicability, #656 directivity angular
resolution, #690 multi-source coherence, #684 geometric scattering
model, #681 edge diffraction model. Sealed records, append-only
repositories, and fail-closed verdicts: no capability claim without a
declared, evidence-backed domain.
"""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_bass_management import FrequencyBand
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, make_empty_scene

from htdt.cad_source_origin_authority import (
    AcousticCenterEstimate,
    DatasetReferenceFrame,
    DeclaredReferencePoint,
    build_source_reference_origin_profile,
    evaluate_source_origin,
)
from htdt.cad_source_origin_repository import (
    CadSourceOriginRepository,
    SourceOriginIntegrityError,
)
from htdt.cad_source_field_applicability_authority import (
    BandFieldRegime,
    DistanceRange,
    MeasurementGeometrySpec,
    NearFarDerivation,
    build_source_field_profile,
    evaluate_source_field_applicability,
)
from htdt.cad_source_field_applicability_repository import (
    CadSourceFieldApplicabilityRepository,
    SourceFieldIntegrityError,
)
from htdt.cad_directivity_resolution_authority import (
    AngularInterpolationSpec,
    AngularSamplingSpec,
    BandAngularCapability,
    HeldoutAngleStudy,
    ShRepresentation,
    build_directivity_interpolation_record,
    build_directivity_sampling_profile,
    evaluate_direction_query,
)
from htdt.cad_directivity_resolution_repository import (
    CadDirectivityResolutionRepository,
    DirectivityResolutionIntegrityError,
)
from htdt.cad_source_coherence_authority import (
    CorrelationRelation,
    CsdEvidence,
    SourceSignalPin,
    build_source_coherence_profile,
    evaluate_source_combination,
)
from htdt.cad_source_coherence_repository import (
    CadSourceCoherenceRepository,
    SourceCoherenceIntegrityError,
)
from htdt.cad_scattering_model_authority import (
    CoefficientDistributionMapping,
    ScatteringValidation,
    build_surface_reflection_model_profile,
    evaluate_scattering_model,
)
from htdt.cad_scattering_model_repository import (
    CadScatteringModelRepository,
    ScatteringModelIntegrityError,
)
from htdt.cad_edge_diffraction_authority import (
    DiffractionNumericsSpec,
    EdgeGeometryPin,
    WedgeMaterialSemantics,
    build_diffraction_benchmark_result,
    build_edge_diffraction_profile,
    evaluate_diffraction_model,
)
from htdt.cad_edge_diffraction_repository import (
    CadEdgeDiffractionRepository,
    EdgeDiffractionIntegrityError,
)


DOC = 'doc-rev58-audiomodel'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #654 — acoustic-reference origin / phase center
# ---------------------------------------------------------------------------


def _origin_estimate(**overrides) -> AcousticCenterEstimate:
    params = dict(
        model_kind='band_limited_center',
        evidence_class='directly_measured',
        method='laser+mic survey',
        position_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.15),
        valid_band_hz=FrequencyBand(low_hz=80.0, high_hz=800.0),
    )
    params.update(overrides)
    return AcousticCenterEstimate(**params)


def _origin_profile(**overrides) :
    params = dict(
        document_id=DOC,
        source_ref=_ref('excitation_source_profile', 'exc-1'),
        acoustic_center_estimates=[_origin_estimate()],
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_source_reference_origin_profile(**params)


def test_origin_profile_seals_and_roundtrips(tmp_path):
    repo = CadSourceOriginRepository(_scene_repo(tmp_path))
    profile = _origin_profile()
    assert profile.profile_id.startswith('sorprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)


def test_origin_profile_requires_pinned_source_ref():
    with pytest.raises(ValueError, match='sha256'):
        _origin_profile(
            source_ref=AuthorityRef(
                kind='excitation_source_profile',
                ref_id='exc-1',
                ref_sha256=None,
            )
        )


def test_origin_band_limited_estimate_requires_band():
    with pytest.raises(ValueError, match='valid_band'):
        _origin_estimate(valid_band_hz=None)


def test_origin_no_single_center_cannot_assert_position():
    with pytest.raises(ValueError, match='no_single_center_model'):
        _origin_estimate(model_kind='no_single_center_model')


def test_origin_measured_estimate_requires_method():
    with pytest.raises(ValueError, match='method'):
        _origin_estimate(method='')


def test_origin_absolute_capability_requires_phase_reference():
    # Only a geometry-inferred estimate — no measured delay and no
    # declared phase-time reference — cannot carry an absolute-phase
    # capability.
    with pytest.raises(ValueError, match='absolute'):
        _origin_profile(
            capability='absolute_phase_reference',
            acoustic_center_estimates=[
                _origin_estimate(
                    evidence_class='inferred_from_geometry',
                    method='',
                )
            ],
        )


def test_origin_evaluate_qualified_in_band():
    profile = _origin_profile()
    q = evaluate_source_origin(
        DOC, profile,
        requested_band_hz=FrequencyBand(low_hz=100.0, high_hz=400.0),
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'origin_qualified'
    assert (
        q.effective_origin_kind
        == 'band_limited_effective_acoustic_center'
    )


def test_origin_evaluate_limited_outside_band():
    profile = _origin_profile()
    q = evaluate_source_origin(
        DOC, profile,
        requested_band_hz=FrequencyBand(low_hz=2000.0, high_hz=8000.0),
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'origin_limited'
    assert any('requested band' in l for l in q.limitations)


def test_origin_undeclared_is_insufficient_evidence():
    # No estimates and no dataset origin — the cabinet pose must not be
    # silently promoted to the propagation origin.
    profile = _origin_profile(acoustic_center_estimates=[])
    q = evaluate_source_origin(DOC, profile, evaluated_at_utc=T1)
    assert q.verdict == 'insufficient_evidence'


def test_origin_frame_origin_without_estimate_is_unverified():
    profile = _origin_profile(
        acoustic_center_estimates=[],
        dataset_frame=DatasetReferenceFrame(
            origin_kind='dataset_measurement_origin',
            measurement_distance_definition='mic to baffle plane',
        ),
    )
    q = evaluate_source_origin(DOC, profile, evaluated_at_utc=T1)
    assert q.verdict == 'origin_unverified'


def test_origin_boundary_mismatch_limits():
    profile = _origin_profile(boundary_state='installed_boundary')
    q = evaluate_source_origin(DOC, profile, evaluated_at_utc=T1)
    assert q.verdict == 'origin_limited'
    assert any('boundary' in l for l in q.limitations)


def test_origin_assumed_estimate_limits():
    profile = _origin_profile(
        acoustic_center_estimates=[
            _origin_estimate(
                evidence_class='assumed_at_driver_aperture',
                method='',
            )
        ]
    )
    q = evaluate_source_origin(DOC, profile, evaluated_at_utc=T1)
    assert q.verdict == 'origin_limited'


def test_origin_repository_rejects_tampered_row(tmp_path):
    repo = CadSourceOriginRepository(_scene_repo(tmp_path))
    profile = _origin_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_source_origin_profiles SET capability=? '
        'WHERE profile_id=?',
        ('absolute_phase_reference', profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SourceOriginIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #655 — source near/far-field applicability
# ---------------------------------------------------------------------------


def _field_profile(**overrides):
    params = dict(
        document_id=DOC,
        source_ref=_ref('excitation_source_profile', 'exc-1'),
        origin_ref=_ref('source_reference_origin_profile', 'sorprof-x'),
        measurement_geometry=MeasurementGeometrySpec(
            mic_distance_m=2.0,
            distance_reference_kind='dataset_phase_origin',
            environment='anechoic_far_field',
        ),
        band_regimes=[
            BandFieldRegime(
                band_hz=FrequencyBand(low_hz=80.0, high_hz=16000.0),
                regime='anechoic_far_field',
                applicable_distance_m=DistanceRange(
                    min_m=1.0, max_m=8.0
                ),
                transition_distance_m=1.2,
                transition_distance_basis='D^2/lambda',
            )
        ],
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_source_field_profile(**params)


def test_field_profile_seals_and_roundtrips(tmp_path):
    repo = CadSourceFieldApplicabilityRepository(_scene_repo(tmp_path))
    profile = _field_profile()
    assert profile.profile_id.startswith('sfldprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_field_profile_origin_ref_must_be_origin_authority():
    with pytest.raises(ValueError, match='origin_ref'):
        _field_profile(
            origin_ref=_ref('excitation_source_profile', 'exc-9')
        )


def test_field_gated_environment_requires_gate_window():
    with pytest.raises(ValueError, match='gate_window'):
        MeasurementGeometrySpec(
            mic_distance_m=1.0,
            environment='quasi_anechoic_gated',
        )


def test_field_transition_distance_requires_basis():
    with pytest.raises(ValueError, match='basis'):
        BandFieldRegime(
            band_hz=FrequencyBand(low_hz=80.0, high_hz=16000.0),
            regime='anechoic_far_field',
            transition_distance_m=1.2,
        )


def test_field_too_close_refused():
    profile = _field_profile()
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=0.5, evaluated_at_utc=T1
    )
    assert (
        q.verdict
        == 'distance_too_close_for_selected_far_field_model'
    )


def test_field_directly_applicable_in_domain():
    profile = _field_profile()
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=3.0, evaluated_at_utc=T1
    )
    assert q.verdict == 'directly_applicable'
    assert q.effective_regime == 'anechoic_far_field'


def test_field_multi_radiator_semantics_refused_for_polar_only():
    profile = _field_profile()
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=3.0,
        requested_semantics='multi_radiator_time_domain',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'requires_explicit_multi_radiator_model'


def test_field_band_outside_regimes_is_insufficient_evidence():
    profile = _field_profile()
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=3.0,
        requested_band_hz=FrequencyBand(low_hz=16000.0, high_hz=40000.0),
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'insufficient_evidence'


def test_field_nearfield_only_without_derivation():
    profile = _field_profile(
        measurement_geometry=MeasurementGeometrySpec(
            mic_distance_m=0.05,
            distance_reference_kind='enclosure_front_baffle_center',
            environment='near_field_scan',
        ),
        band_regimes=[
            BandFieldRegime(
                band_hz=FrequencyBand(low_hz=80.0, high_hz=16000.0),
                regime='near_field_scan',
                applicable_distance_m=DistanceRange(
                    min_m=0.02, max_m=0.3
                ),
            )
        ],
    )
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=2.0, evaluated_at_utc=T1
    )
    assert q.verdict == 'nearfield_only'


def test_field_nearfield_with_derivation_approximates():
    profile = _field_profile(
        measurement_geometry=MeasurementGeometrySpec(
            mic_distance_m=0.05,
            distance_reference_kind='enclosure_front_baffle_center',
            environment='near_field_scan',
        ),
        band_regimes=[
            BandFieldRegime(
                band_hz=FrequencyBand(low_hz=80.0, high_hz=16000.0),
                regime='near_field_scan',
                applicable_distance_m=DistanceRange(
                    min_m=0.02, max_m=0.3
                ),
            )
        ],
        near_far_derivation=NearFarDerivation(
            source_measurement_refs=[_ref('measurement_run', 'nf-1')],
            algorithm='near-field acoustical holography',
        ),
    )
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=2.0, evaluated_at_utc=T1
    )
    assert q.verdict == 'applicable_with_approximation'
    assert any('derivation' in l for l in q.limitations)


def test_field_gate_limits_low_band():
    profile = _field_profile(
        measurement_geometry=MeasurementGeometrySpec(
            mic_distance_m=2.0,
            distance_reference_kind='dataset_phase_origin',
            environment='quasi_anechoic_gated',
            gate_window_s=0.005,  # 5 ms gate → ~200 Hz resolution
        ),
        band_regimes=[
            BandFieldRegime(
                band_hz=FrequencyBand(low_hz=20.0, high_hz=20000.0),
                regime='quasi_anechoic_gated',
            )
        ],
    )
    q = evaluate_source_field_applicability(
        DOC, profile, requested_distance_m=2.0,
        requested_band_hz=FrequencyBand(low_hz=40.0, high_hz=120.0),
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'insufficient_evidence'


def test_field_repository_rejects_tampered_row(tmp_path):
    repo = CadSourceFieldApplicabilityRepository(_scene_repo(tmp_path))
    profile = _field_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_source_field_profiles SET mic_distance_m=? '
        'WHERE profile_id=?',
        (0.05, profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SourceFieldIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #656 — directivity angular resolution
# ---------------------------------------------------------------------------


def _sampling(**overrides) -> AngularSamplingSpec:
    params = dict(
        coverage_class='full_sphere_sampled',
        measured_direction_count=72,
        nominal_step_deg=30.0,
    )
    params.update(overrides)
    return AngularSamplingSpec(**params)


def _dir_profile(**overrides):
    params = dict(
        document_id=DOC,
        dataset_ref=_ref('directivity_dataset', 'ds-1'),
        dataset_kind='magnitude_only',
        sampling=_sampling(),
        interpolation=AngularInterpolationSpec(
            method='linear_angular', domain='magnitude_db'
        ),
        presentation_step_deg=1.0,
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_directivity_sampling_profile(**params)


def test_dir_profile_seals_and_roundtrips(tmp_path):
    repo = CadDirectivityResolutionRepository(_scene_repo(tmp_path))
    profile = _dir_profile()
    assert profile.profile_id.startswith('drsprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_dir_complex_interp_rejected_on_magnitude_dataset():
    with pytest.raises(ValueError, match='complex'):
        _dir_profile(
            interpolation=AngularInterpolationSpec(
                method='complex_tf_interpolation',
                domain='complex_tf',
            )
        )


def test_dir_sh_method_requires_representation():
    with pytest.raises(ValueError, match='ShRepresentation|sh'):
        AngularInterpolationSpec(
            method='spherical_harmonic_reconstruction',
            domain='sh_coefficients',
        )


def test_dir_method_domain_mismatch_rejected():
    with pytest.raises(ValueError, match='domain'):
        AngularInterpolationSpec(
            method='linear_angular', domain='sh_coefficients'
        )


def test_dir_measured_direction():
    profile = _dir_profile()
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=30.0, elevation_deg=0.0,
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'measured_direction'


def test_dir_interpolated_eligible():
    profile = _dir_profile()
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=45.0, elevation_deg=15.0,
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'interpolated_eligible'


def test_dir_no_interpolation_is_outside_coverage():
    profile = _dir_profile(interpolation=None)
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=45.0, elevation_deg=15.0,
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'outside_coverage'


def test_dir_hv_cuts_off_plane_is_outside_coverage():
    profile = _dir_profile(
        sampling=_sampling(
            coverage_class='hv_cuts',
            measured_direction_count=36,
            nominal_step_deg=30.0,
        )
    )
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=45.0, elevation_deg=30.0,
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'outside_coverage'


def test_dir_sh_order_unsupported():
    profile = _dir_profile(
        interpolation=AngularInterpolationSpec(
            method='spherical_harmonic_reconstruction',
            domain='sh_coefficients',
            sh=ShRepresentation(
                supported_order=4, convention='ambix'
            ),
        )
    )
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=45.0, elevation_deg=15.0,
        requested_sh_order=8, evaluated_at_utc=T1,
    )
    assert q.verdict == 'sh_order_unsupported'


def test_dir_limited_band_capability_downgrades():
    profile = _dir_profile(
        band_capabilities=[
            BandAngularCapability(
                band_hz=FrequencyBand(low_hz=4000.0, high_hz=16000.0),
                state='spatial_aliasing_risk',
            )
        ]
    )
    q = evaluate_direction_query(
        DOC, profile, azimuth_deg=45.0, elevation_deg=15.0,
        frequency_hz=8000.0, evaluated_at_utc=T1,
    )
    assert q.verdict == 'interpolated_limited'
    assert any('aliasing' in l for l in q.limitations)


def test_dir_interpolation_record_seals(tmp_path):
    repo = CadDirectivityResolutionRepository(_scene_repo(tmp_path))
    profile = _dir_profile()
    repo.save_profile(profile)
    record = build_directivity_interpolation_record(
        DOC, profile,
        method='linear_angular', domain='magnitude_db',
        output_grid=AngularSamplingSpec(
            coverage_class='full_sphere_derived',
            measured_direction_count=0,
            nominal_step_deg=1.0,
        ),
        declared_at_utc=T0,
    )
    assert record.record_id.startswith('dinterp-')
    repo.save_interpolation_record(record)
    assert repo.get_interpolation_record(record.record_id) == record


def test_dir_repository_rejects_tampered_row(tmp_path):
    repo = CadDirectivityResolutionRepository(_scene_repo(tmp_path))
    profile = _dir_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_directivity_sampling_profiles '
        'SET measured_direction_count=? WHERE profile_id=?',
        (64800, profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(DirectivityResolutionIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #690 — multi-source correlation / coherence
# ---------------------------------------------------------------------------


def _member(member_id: str, **overrides) -> SourceSignalPin:
    params = dict(
        member_id=member_id,
        source_ref=_ref('excitation_source_profile', member_id),
        stimulus_ref=_ref('stimulus', 'stim-1'),
    )
    params.update(overrides)
    return SourceSignalPin(**params)


def _coherence_profile(**overrides):
    params = dict(
        document_id=DOC,
        members=[_member('L'), _member('R')],
        relations=[
            CorrelationRelation(
                member_a_id='L', member_b_id='R',
                relation='deterministic_identical',
                basis='analytic_signal_path',
            )
        ],
        default_relation='deterministic_identical',
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_source_coherence_profile(**params)


def test_coherence_profile_seals_and_roundtrips(tmp_path):
    repo = CadSourceCoherenceRepository(_scene_repo(tmp_path))
    profile = _coherence_profile()
    assert profile.profile_id.startswith('mscprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_coherence_deterministic_requires_stimulus_pin():
    with pytest.raises(ValueError, match='stimulus'):
        _coherence_profile(
            members=[
                _member('L'),
                _member('R', stimulus_ref=None),
            ]
        )


def test_coherence_relation_requires_basis():
    with pytest.raises(ValueError, match='basis'):
        CorrelationRelation(
            member_a_id='L', member_b_id='R',
            relation='uncorrelated_independent',
            basis='unknown',
        )


def test_coherence_partial_requires_csd_or_coherence_basis():
    with pytest.raises(ValueError, match='partially_coherent'):
        CorrelationRelation(
            member_a_id='L', member_b_id='R',
            relation='partially_coherent',
            basis='analytic_signal_path',
        )


def test_coherence_csd_must_declare_hermitian():
    with pytest.raises(ValueError, match='Hermitian'):
        CsdEvidence(
            frequency_grid_hz=[125.0, 250.0],
            hermitian_declared=False,
        )


def test_coherence_energy_sum_on_deterministic_is_incompatible():
    profile = _coherence_profile()
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='incoherent_expected_energy',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'incompatible_combination'
    assert q.effective_mode is None


def test_coherence_complex_sum_qualified():
    profile = _coherence_profile()
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='complex_deterministic',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'combination_qualified'
    assert q.effective_mode == 'complex_deterministic'


def test_coherence_independent_energy_sum_qualified():
    profile = _coherence_profile(
        relations=[
            CorrelationRelation(
                member_a_id='L', member_b_id='R',
                relation='uncorrelated_independent',
                basis='measured_cross_spectrum',
            )
        ],
        default_relation='uncorrelated_independent',
    )
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='incoherent_expected_energy',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'combination_qualified'


def test_coherence_partial_energy_sum_incompatible():
    profile = _coherence_profile(
        relations=[
            CorrelationRelation(
                member_a_id='L', member_b_id='R',
                relation='partially_coherent',
                basis='measured_coherence_function',
            )
        ],
        default_relation='partially_coherent',
    )
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='incoherent_expected_energy',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'incompatible_combination'


def test_coherence_undeclared_only_allows_bounds():
    profile = _coherence_profile(
        relations=[],
        default_relation='unknown',
    )
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='complex_deterministic',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'insufficient_evidence'
    q2 = evaluate_source_combination(
        DOC, profile,
        requested_mode='bounded_scenarios',
        evaluated_at_utc=T1,
    )
    assert q2.verdict == 'insufficient_evidence'


def test_coherence_undeclared_with_declared_bounds():
    profile = _coherence_profile(
        relations=[
            CorrelationRelation(
                member_a_id='L', member_b_id='R',
                relation='unknown',
                basis='bounded_scenarios_declared',
            )
        ],
        default_relation='unknown',
    )
    q = evaluate_source_combination(
        DOC, profile,
        requested_mode='bounded_scenarios',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'qualified_with_scenario_bounds'


def test_coherence_repository_rejects_tampered_row(tmp_path):
    repo = CadSourceCoherenceRepository(_scene_repo(tmp_path))
    profile = _coherence_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_source_coherence_profiles SET default_relation=? '
        'WHERE profile_id=?',
        ('uncorrelated_independent', profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SourceCoherenceIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #684 — geometric surface-scattering model
# ---------------------------------------------------------------------------


def _scat_mapping(**overrides) -> CoefficientDistributionMapping:
    params = dict(
        input_quantity_kind='random_incidence_scattering_coefficient',
        distribution_law='lambert',
        specular_fraction=0.7,
        diffuse_fraction=0.3,
    )
    params.update(overrides)
    return CoefficientDistributionMapping(**params)


def _scat_profile(**overrides):
    params = dict(
        document_id=DOC,
        solver_model='specular_plus_lambert_diffuse',
        coefficient_mapping=_scat_mapping(),
        incidence_domain='random_incidence_scalar',
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_surface_reflection_model_profile(**params)


def test_scat_profile_seals_and_roundtrips(tmp_path):
    repo = CadScatteringModelRepository(_scene_repo(tmp_path))
    profile = _scat_profile()
    assert profile.profile_id.startswith('scatprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_scat_iso17497_2_cannot_feed_solver_input():
    # ISO 17497-2 diffusion coefficient is not a scatter fraction; the
    # mapping must reject it as a solver input (documentation only).
    with pytest.raises(ValueError, match='solver scalar input'):
        _scat_mapping(
            input_quantity_kind='directional_diffusion_coefficient'
        )


def test_scat_diffusion_kind_as_documentation_role_ok():
    mapping = _scat_mapping(
        input_quantity_kind='directional_diffusion_coefficient',
        mapping_role='display_documentation',
    )
    assert mapping.mapping_role == 'display_documentation'


def test_scat_coefficient_model_requires_mapping():
    with pytest.raises(ValueError, match='Coefficient'):
        _scat_profile(coefficient_mapping=None)


def test_scat_unit_energy_requires_fraction_sum():
    with pytest.raises(ValueError, match='sum to 1'):
        _scat_mapping(specular_fraction=0.5, diffuse_fraction=0.6)


def test_scat_redirection_required_refused():
    profile = _scat_profile()
    q = evaluate_scattering_model(
        DOC, profile, requires_redirection=True, evaluated_at_utc=T1
    )
    assert q.verdict == 'directional_redirection_unsupported'


def test_scat_redirecting_model_qualified():
    profile = _scat_profile(
        solver_model='specular_plus_measured_directional',
        coefficient_mapping=_scat_mapping(
            distribution_law='measured_kernel',
            incidence_handling='multi_incidence_measured',
        ),
        incidence_domain='multi_incidence_measured',
        validation=ScatteringValidation(
            tier='benchmark_measured',
            evidence_refs=[_ref('benchmark_run', 'scat-1')],
        ),
    )
    q = evaluate_scattering_model(
        DOC, profile, requires_redirection=True, evaluated_at_utc=T1
    )
    assert q.verdict == 'qualified_for_declared_domain'


def test_scat_early_reflection_on_late_only_model_limits():
    profile = _scat_profile(
        early_late_applicability='late_diffuse_approximation_only'
    )
    q = evaluate_scattering_model(
        DOC, profile, reflection_order='early_reflection',
        evaluated_at_utc=T1,
    )
    assert q.verdict == 'qualified_with_limitations'


def test_scat_repository_rejects_tampered_row(tmp_path):
    repo = CadScatteringModelRepository(_scene_repo(tmp_path))
    profile = _scat_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_scattering_model_profiles SET solver_model=? '
        'WHERE profile_id=?',
        ('specular_plus_measured_directional', profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ScatteringModelIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #681 — edge diffraction model
# ---------------------------------------------------------------------------


def _dif_profile(**overrides):
    params = dict(
        document_id=DOC,
        model_family='btm_finite_edge',
        edge_geometry=EdgeGeometryPin(
            edge_kind='finite_straight_edge',
            edge_length_m=0.8,
            wedge_angle_deg=90.0,
        ),
        implementation='pytafm',
        numerics=DiffractionNumericsSpec(
            kind='series_summation',
            convergence_evidence_ref=_ref('convergence_run', 'cv-1'),
        ),
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_edge_diffraction_profile(**params)


def _dif_benchmark(profile, **overrides):
    params = dict(
        document_id=DOC,
        profile=profile,
        fixture_id='DIF20',
        fixture_kind='finite_edge',
        reference_class='bras',
        result='pass',
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_diffraction_benchmark_result(**params)


def test_dif_profile_seals_and_roundtrips(tmp_path):
    repo = CadEdgeDiffractionRepository(_scene_repo(tmp_path))
    profile = _dif_profile()
    assert profile.profile_id.startswith('edfprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_dif_finite_edge_requires_length():
    with pytest.raises(ValueError, match='edge_length'):
        EdgeGeometryPin(edge_kind='finite_straight_edge')


def test_dif_unknown_family_not_declarable():
    with pytest.raises(ValueError, match='model_family'):
        _dif_profile(model_family='unknown')


def test_dif_pass_fixture_cannot_mismatch():
    with pytest.raises(ValueError, match='mismatch'):
        _dif_benchmark(
            _dif_profile(),
            observables=[('arrival_time', 'mismatches')],
        )


def test_dif_benchmark_seals(tmp_path):
    repo = CadEdgeDiffractionRepository(_scene_repo(tmp_path))
    profile = _dif_profile()
    repo.save_profile(profile)
    result = _dif_benchmark(profile)
    assert result.result_id.startswith('difbench-')
    repo.save_benchmark_result(result)
    assert repo.get_benchmark_result(result.result_id) == result


def test_dif_no_benchmarks_is_insufficient_evidence():
    profile = _dif_profile()
    q = evaluate_diffraction_model(DOC, profile, evaluated_at_utc=T1)
    assert q.capability == 'insufficient_evidence'


def test_dif_strong_fixture_gives_reference_capability():
    profile = _dif_profile()
    q = evaluate_diffraction_model(
        DOC, profile,
        benchmarks=[_dif_benchmark(profile)],
        evaluated_at_utc=T1,
    )
    assert q.capability == 'physical_reference_capability'


def test_dif_failing_fixture_is_unqualified():
    profile = _dif_profile()
    failing = _dif_benchmark(
        profile, fixture_id='DIF30', result='fail',
    )
    q = evaluate_diffraction_model(
        DOC, profile, benchmarks=[failing], evaluated_at_utc=T1,
    )
    assert q.capability == 'unqualified'


def test_dif_iir_approximation_capped_perceptual():
    profile = _dif_profile(model_family='iir_reduced_approximation')
    weak = _dif_benchmark(
        profile,
        fixture_kind='infinite_wedge_reference',
        result='pass',
    )
    q = evaluate_diffraction_model(
        DOC, profile, benchmarks=[weak], evaluated_at_utc=T1,
    )
    assert q.capability == 'perceptual_approximation_capability'


def test_dif_rigid_model_on_nonrigid_wedge_limited():
    profile = _dif_profile(
        wedge_material=WedgeMaterialSemantics(
            boundary='rigid_reference'
        )
    )
    q = evaluate_diffraction_model(
        DOC, profile,
        benchmarks=[_dif_benchmark(profile)],
        wedge_nonrigid=True,
        evaluated_at_utc=T1,
    )
    assert q.capability == 'physical_approximation_capability'
    assert q.boundary_limited


def test_dif_missing_convergence_caps_reference():
    profile = _dif_profile(
        numerics=DiffractionNumericsSpec(kind='series_summation')
    )
    q = evaluate_diffraction_model(
        DOC, profile,
        benchmarks=[_dif_benchmark(profile)],
        evaluated_at_utc=T1,
    )
    assert q.capability == 'physical_approximation_capability'
    assert any('convergence' in l for l in q.limitations)


def test_dif_repository_rejects_tampered_row(tmp_path):
    repo = CadEdgeDiffractionRepository(_scene_repo(tmp_path))
    profile = _dif_profile()
    repo.save_profile(profile)
    import sqlite3
    connection = sqlite3.connect(repo.path)
    connection.execute(
        'UPDATE cad_diffraction_model_profiles SET model_family=? '
        'WHERE profile_id=?',
        ('utd', profile.profile_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(EdgeDiffractionIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# Append-only semantics (all authorities)
# ---------------------------------------------------------------------------


def test_append_only_rejects_conflicting_resave(tmp_path):
    repo = CadSourceOriginRepository(_scene_repo(tmp_path))
    profile = _origin_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)  # identical resave is a no-op

    import sqlite3
    connection = sqlite3.connect(repo.path)
    count = connection.execute(
        'SELECT COUNT(*) FROM cad_source_origin_profiles'
    ).fetchone()[0]
    connection.close()
    assert count == 1
