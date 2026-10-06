"""REV58-NUMERIC regression tests — #683 wave-solver numerical
fidelity, #685 geometrical-acoustics numerical fidelity, #687
wave↔geometrical hybrid-handoff qualification. Sealed records,
append-only repositories, and fail-closed verdicts: no accuracy claim
without a declared configuration, no convergence claim without a study,
no qualified handoff without declared bands.
"""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_wave_fidelity_authority import (
    ArtificialBoundarySpec,
    AdjacentTerminationSpec,
    BemNumerics,
    FdtdTimeStepping,
    FemNumerics,
    LinearSolveEvidence,
    WaveBoundaryReflectionEvidence,
    WaveDiscretization,
    WaveDispersionEvidence,
    WaveFixtureResult,
    WaveReceiverDiscretization,
    WaveRefinementComparison,
    WaveRefinementLevel,
    WaveSolverFormulation,
    WaveSourceDiscretization,
    build_numerical_convergence_record,
    build_wave_fidelity_profile,
    evaluate_wave_fidelity,
)
from htdt.cad_wave_fidelity_repository import (
    CadWaveFidelityRepository,
    WaveFidelityIntegrityError,
)
from htdt.cad_geometric_fidelity_authority import (
    EnergyAccountingRecord,
    GaAlgorithmIdentity,
    GaConvergenceEvidence,
    GaFixtureResult,
    GaVisibilityCaseResult,
    PathTruncationSpec,
    RayLaunchSpec,
    ReceiverEstimatorSpec,
    ReceiverRadiusStudy,
    SeedSpreadStudy,
    VisibilitySpec,
    build_geometric_fidelity_profile,
    build_path_enumeration_qualification,
    build_ray_sampling_convergence,
    evaluate_geometric_fidelity,
)
from htdt.cad_geometric_fidelity_repository import (
    CadGeometricFidelityRepository,
    GeometricFidelityIntegrityError,
)
from htdt.cad_hybrid_handoff_authority import (
    CalibrationDiscipline,
    ContinuityEvidence,
    CrossoverFilterSpec,
    HybridComponentPin,
    ObservableValidity,
    PhenomenonDeclaration,
    SourceNormalizationSpec,
    TimeAlignmentSpec,
    TransitionBandSpec,
    build_hybrid_composition_profile,
    evaluate_hybrid_handoff,
)
from htdt.cad_hybrid_handoff_repository import (
    CadHybridHandoffRepository,
    HybridHandoffIntegrityError,
)


DOC = 'doc-rev58-numeric'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #683 — wave-solver numerical fidelity


def _fdtd_formulation() -> WaveSolverFormulation:
    return WaveSolverFormulation(
        solver_family='fdtd',
        equation_formulation='leapfrog_velocity_potential',
        solver_domain='time_domain',
        implementation='pffdtd',
        implementation_version='1.2.3',
        element_or_basis='cartesian_staggered_yee',
        precision='float64',
        boundary_update_scheme='velocity_impedance',
        source_discretization='soft_source_volume_velocity',
        receiver_interpolation='grid_node_nearest',
    )


def _discretization() -> WaveDiscretization:
    return WaveDiscretization(
        mesh_identity='grid:5mm:uniform',
        mesh_sha256=SHA_B,
        resolution_min_m=0.005,
        resolution_max_m=0.005,
        element_or_voxel_size_m=0.005,
        basis_order=2,
        geometry_approximation='staircased_voxel',
        boundary_representation='impedance_face',
        points_per_wavelength_rule='10_ppwl_at_f_max',
        target_frequency_hz=4000.0,
    )


def _fdtd_stepping(**overrides) -> FdtdTimeStepping:
    params = dict(
        dt_seconds=8.0e-6,
        grid_spacing_m=0.005,
        cfl_rule='c*dt/dx <= 1/sqrt(3)',
        cfl_number=0.55,
        time_integration='leapfrog',
        boundary_update='velocity_impedance',
        duration_s=0.5,
        stability_state='within_declared_stability',
    )
    params.update(overrides)
    return FdtdTimeStepping(**params)


def _wave_profile(**overrides) -> object:
    params = dict(
        document_id=DOC,
        formulation=_fdtd_formulation(),
        discretization=_discretization(),
        fdtd=_fdtd_stepping(),
        solver_result_ref=_ref('solver_result', 'res-1', SHA_C),
        artificial_boundary=ArtificialBoundarySpec(
            method='pml',
            layer_thickness_m=0.1,
            damping_profile='polynomial_order_3',
            distance_from_interest_m=0.3,
            measured_reflection=WaveBoundaryReflectionEvidence(
                fixture_description='WNV70 outgoing-plane-wave',
                reflection_coefficient_max=0.01,
                reference_kind='analytic',
            ),
        ),
        adjacent_termination=AdjacentTerminationSpec(
            kind='open_domain',
            detail='free-field reference scene',
        ),
        source_discretization=WaveSourceDiscretization(
            injection_method='soft_source_volume_velocity',
            regularization='grid_smeared_2voxel',
        ),
        receiver_discretization=WaveReceiverDiscretization(
            method='grid_node',
        ),
        dispersion_evidence=WaveDispersionEvidence(
            fixture_id='WNV20',
            phase_velocity_error_rel=0.004,
            arrival_time_error_s=1.2e-5,
            anisotropy_observed='yes',
        ),
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_wave_fidelity_profile(**params)


def _refinement(
    profile=None, document_id: str = DOC, levels: int = 3, **overrides
):
    profile = profile if profile is not None else _wave_profile()
    names = ('coarse', 'medium', 'fine')[:levels]
    level_records = [
        WaveRefinementLevel(
            level_name=name,
            discretization=_discretization(),
            result_ref=_ref('solver_result', f'res-{name}', SHA_D),
        )
        for name in names
    ]
    comparisons = [
        WaveRefinementComparison(
            observable='pressure_magnitude_spectrum',
            level_pair=(names[0], names[-1]),
            max_relative_change=0.02,
        ),
        WaveRefinementComparison(
            observable='arrival_time',
            level_pair=(names[-2], names[-1]),
            max_relative_change=0.005,
        ),
    ]
    params = dict(
        document_id=document_id,
        profile_ref=AuthorityRef(
            kind='wave_fidelity_profile',
            ref_id=profile.profile_id,
            ref_sha256=profile.profile_sha256,
        ),
        study_kind='refinement',
        refinement_levels=level_records,
        comparisons=comparisons,
        declared_at_utc=T1,
    )
    params.update(overrides)
    return build_numerical_convergence_record(**params)


def test_wave_profile_seals_and_roundtrips(tmp_path):
    repo = CadWaveFidelityRepository(_scene_repo(tmp_path))
    profile = _wave_profile()
    assert profile.profile_id.startswith('wnfprof-')
    repo.save_profile(profile)
    loaded = repo.get_profile(profile.profile_id)
    assert loaded == profile
    assert repo.list_profiles(DOC) == (profile,)


def test_wave_profile_family_block_consistency():
    # fdtd family without the fdtd block cannot seal.
    with pytest.raises(ValueError, match='fdtd'):
        _wave_profile(fdtd=None)
    # An fem block on an fdtd family is rejected.
    with pytest.raises(ValueError, match='fem'):
        _wave_profile(
            fem=FemNumerics(
                element_family='tet',
                element_order=2,
                mesh_density_descriptor='8 ppwl',
            )
        )


def test_wave_profile_requires_pinned_solver_ref():
    with pytest.raises(ValueError, match='sha256'):
        _wave_profile(
            solver_result_ref=AuthorityRef(
                kind='solver_result', ref_id='res-1', ref_sha256=None
            )
        )


def test_wave_profile_pml_is_not_anechoic():
    # A computational PML without measured leakage can be declared, but
    # the artificial-boundary error class stays unresolved and the
    # profile cannot claim qualified_for_declared_domain.
    profile = _wave_profile(
        artificial_boundary=ArtificialBoundarySpec(
            method='pml',
            layer_thickness_m=0.1,
        )
    )
    qualification = evaluate_wave_fidelity(
        DOC,
        profile,
        convergences=(_refinement(profile),),
        evaluated_at_utc=T1,
    )
    assert qualification.fidelity_state == 'insufficient_evidence'
    boundary = next(
        entry
        for entry in qualification.error_class_states
        if entry.error_class == 'artificial_boundary'
    )
    assert boundary.state == 'unresolved'


def test_wave_qualification_qualified_with_full_evidence(tmp_path):
    profile = _wave_profile()
    convergence = _refinement(profile)
    qualification = evaluate_wave_fidelity(
        DOC,
        profile,
        convergences=(convergence,),
        evaluated_at_utc=T1,
    )
    assert qualification.fidelity_state == 'qualified_for_declared_domain'
    states = {
        entry.error_class: entry.state
        for entry in qualification.error_class_states
    }
    assert states['numerical_dispersion'] == 'qualified'
    assert states['cfl_stability'] == 'qualified'
    assert states['artificial_boundary'] == 'qualified'
    assert states['fem_pollution'] == 'not_applicable'
    assert states['algebraic_tolerance'] == 'not_applicable'


def test_wave_qualification_no_convergence_is_insufficient():
    profile = _wave_profile()
    qualification = evaluate_wave_fidelity(
        DOC, profile, convergences=(), evaluated_at_utc=T1
    )
    assert qualification.fidelity_state == 'insufficient_evidence'


def test_wave_qualification_two_level_refinement_is_limited():
    profile = _wave_profile()
    convergence = _refinement(profile, levels=2)
    qualification = evaluate_wave_fidelity(
        DOC,
        profile,
        convergences=(convergence,),
        evaluated_at_utc=T1,
    )
    assert qualification.fidelity_state == 'qualified_with_limitations'
    assert any(
        'two-level' in limitation
        for limitation in qualification.limitations
    )


def test_wave_qualification_outside_stability_is_not_qualified():
    profile = _wave_profile(
        fdtd=_fdtd_stepping(stability_state='outside_declared_stability')
    )
    qualification = evaluate_wave_fidelity(
        DOC,
        profile,
        convergences=(_refinement(profile),),
        evaluated_at_utc=T1,
    )
    assert qualification.fidelity_state == 'not_qualified'


def test_wave_qualification_fixture_fail_is_not_qualified():
    profile = _wave_profile()
    convergence = build_numerical_convergence_record(
        document_id=DOC,
        profile_ref=AuthorityRef(
            kind='wave_fidelity_profile',
            ref_id=profile.profile_id,
            ref_sha256=profile.profile_sha256,
        ),
        study_kind='fixture',
        fixture_results=(
            WaveFixtureResult(
                fixture_id='WNV20',
                verdict='fail',
                reference_kind='analytic',
            ),
        ),
        declared_at_utc=T1,
    )
    qualification = evaluate_wave_fidelity(
        DOC,
        profile,
        convergences=(convergence,),
        evaluated_at_utc=T1,
    )
    assert qualification.fidelity_state == 'not_qualified'


def test_wave_convergence_requires_independence_limitation():
    profile = _wave_profile()
    with pytest.raises(ValueError, match='independence'):
        build_numerical_convergence_record(
            document_id=DOC,
            profile_ref=AuthorityRef(
                kind='wave_fidelity_profile',
                ref_id=profile.profile_id,
                ref_sha256=profile.profile_sha256,
            ),
            study_kind='cross_solver',
            cross_solver_detail='pffdtd vs comsol',
            declared_at_utc=T1,
        )


def test_wave_profile_repository_append_only(tmp_path):
    repo = CadWaveFidelityRepository(_scene_repo(tmp_path))
    profile = _wave_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)  # same id + same sha is a no-op
    other = _wave_profile(declared_at_utc=T1)
    assert other.profile_id != profile.profile_id


def test_wave_repository_rejects_forged_record(tmp_path):
    repo = CadWaveFidelityRepository(_scene_repo(tmp_path))
    profile = _wave_profile()
    forged = profile.model_copy(update={'declared_at_utc': T1})
    with pytest.raises(WaveFidelityIntegrityError):
        repo.save_profile(forged)


def test_wave_repository_integrity_on_mirrored_tamper(tmp_path):
    repo = CadWaveFidelityRepository(_scene_repo(tmp_path))
    profile = _wave_profile()
    repo.save_profile(profile)
    import sqlite3

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE cad_wave_fidelity_profiles SET solver_family='fem' "
            'WHERE profile_id=?',
            (profile.profile_id,),
        )
        connection.commit()
    with pytest.raises(WaveFidelityIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #685 — geometrical-acoustics numerical fidelity


def _ga_algorithm(family: str = 'image_source') -> GaAlgorithmIdentity:
    classes = (
        ('deterministic_path_enumeration',)
        if family
        in ('image_source', 'beam_tracing', 'pyramid_cone_frustum')
        else ('stochastic_ray_sample', 'statistical_late_field')
    )
    return GaAlgorithmIdentity(
        family=family,
        implementation='htdt.planar_image_source',
        implementation_version='r150-1',
        path_classes=classes,
    )


def _truncation() -> PathTruncationSpec:
    return PathTruncationSpec(
        max_reflection_order=6,
        max_path_time_s=0.2,
        visibility_algorithm='exact_polygon_visibility:1.0',
        order_sensitivity_evaluated='evaluated',
    )


def _visibility() -> VisibilitySpec:
    return VisibilitySpec(
        intersection_tolerance_m=1.0e-9,
        geometry_version='scene-rev-1',
        case_results=(
            GaVisibilityCaseResult(
                case='concave_polygon', state='tested', detail='GAN30'
            ),
            GaVisibilityCaseResult(
                case='portal_opening', state='tested', detail='GAN30'
            ),
        ),
    )


def _receiver(**overrides) -> ReceiverEstimatorSpec:
    params = dict(
        model='path_intersection_exact',
        normalization='per_path_energy',
        duplicate_hit_policy='count_once_per_path',
        estimator_version='r150-1',
    )
    params.update(overrides)
    return ReceiverEstimatorSpec(**params)


def _ga_profile(**overrides):
    params = dict(
        document_id=DOC,
        algorithm=_ga_algorithm(),
        receiver=_receiver(),
        truncation=_truncation(),
        visibility=_visibility(),
        solver_result_ref=_ref('solver_result', 'ga-res-1', SHA_C),
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_geometric_fidelity_profile(**params)


def test_ga_profile_seals_and_roundtrips(tmp_path):
    repo = CadGeometricFidelityRepository(_scene_repo(tmp_path))
    profile = _ga_profile()
    assert profile.profile_id.startswith('gnfprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)


def test_ga_profile_deterministic_family_requires_blocks():
    with pytest.raises(ValueError, match='truncation'):
        _ga_profile(truncation=None)
    with pytest.raises(ValueError, match='visibility'):
        _ga_profile(visibility=None)


def test_ga_profile_stochastic_family_requires_launch():
    params = dict(
        document_id=DOC,
        algorithm=GaAlgorithmIdentity(
            family='ray_tracing',
            implementation='custom',
            implementation_version='1.0',
            path_classes=('stochastic_ray_sample',),
        ),
        receiver=_receiver(model='spherical_capture', radius_m=0.1),
        declared_at_utc=T0,
    )
    with pytest.raises(ValueError, match='ray'):
        build_geometric_fidelity_profile(**params)


def test_ga_receiver_radius_required_for_spherical():
    with pytest.raises(ValueError, match='radius'):
        _receiver(model='spherical_capture')


def test_ga_seed_spread_requires_distinct_seeds():
    with pytest.raises(ValueError, match='distinct'):
        SeedSpreadStudy(observable='edc_shape', seeds=(7, 7))


def test_ga_converged_status_requires_evidence():
    with pytest.raises(ValueError, match='evidence'):
        GaConvergenceEvidence(observable='edc_shape', status='converged')


def test_ga_qualification_deterministic_qualified(tmp_path):
    profile = _ga_profile()
    enumeration = build_path_enumeration_qualification(
        document_id=DOC,
        profile_ref=AuthorityRef(
            kind='geometric_fidelity_profile',
            ref_id=profile.profile_id,
            ref_sha256=profile.profile_sha256,
        ),
        deterministic_state='qualified',
        named_path_evidence_class='exact_ga_path_eligible',
        max_qualified_order=6,
        covered_cases=('concave_polygon', 'portal_opening'),
        exactness_fixtures=(
            GaFixtureResult(fixture_id='GAN20', verdict='pass'),
        ),
        declared_at_utc=T1,
    )
    qualification = evaluate_geometric_fidelity(
        DOC,
        profile,
        sampling=None,
        enumeration=enumeration,
        evaluated_at_utc=T1,
    )
    assert (
        qualification.fidelity_state == 'qualified_for_declared_domain'
    )
    assert qualification.deterministic_state == 'qualified'
    assert qualification.stochastic_state == 'not_applicable'


def test_ga_qualification_missing_enumeration_is_insufficient():
    profile = _ga_profile()
    qualification = evaluate_geometric_fidelity(
        DOC, profile, sampling=None, enumeration=None, evaluated_at_utc=T1
    )
    assert qualification.deterministic_state == 'unqualified'
    assert qualification.fidelity_state == 'insufficient_evidence'


def test_ga_receiver_radius_unevaluated_is_limited():
    profile = _ga_profile(
        algorithm=GaAlgorithmIdentity(
            family='ray_tracing',
            implementation='custom',
            implementation_version='1.0',
            path_classes=(
                'stochastic_ray_sample', 'statistical_late_field'
            ),
        ),
        receiver=ReceiverEstimatorSpec(
            model='spherical_capture',
            radius_m=0.1,
            normalization='per_ray_energy',
            duplicate_hit_policy='count_once_per_path',
            estimator_version='1.0',
        ),
        ray_launch=RayLaunchSpec(
            launched_ray_count=100000,
            launch_distribution='uniform_sphere',
            rng_name='pcg64',
            seed=42,
        ),
        truncation=None,
        visibility=None,
    )
    sampling = build_ray_sampling_convergence(
        document_id=DOC,
        profile_ref=AuthorityRef(
            kind='geometric_fidelity_profile',
            ref_id=profile.profile_id,
            ref_sha256=profile.profile_sha256,
        ),
        evidence=(
            GaConvergenceEvidence(
                observable='edc_shape',
                status='converged',
                ray_counts_tested=(10000, 50000, 100000),
            ),
        ),
        declared_at_utc=T1,
    )
    qualification = evaluate_geometric_fidelity(
        DOC,
        profile,
        sampling=sampling,
        enumeration=None,
        evaluated_at_utc=T1,
    )
    assert qualification.receiver_domain_state == 'radius_unevaluated'
    assert qualification.fidelity_state == 'qualified_with_limitations'


def test_ga_stochastic_without_sampling_is_insufficient():
    profile = _ga_profile(
        algorithm=GaAlgorithmIdentity(
            family='ray_tracing',
            implementation='custom',
            implementation_version='1.0',
            path_classes=(
                'stochastic_ray_sample', 'statistical_late_field'
            ),
        ),
        receiver=ReceiverEstimatorSpec(
            model='spherical_capture',
            radius_m=0.1,
            normalization='per_ray_energy',
            duplicate_hit_policy='count_once_per_path',
            estimator_version='1.0',
            radius_sensitivity=ReceiverRadiusStudy(
                swept_radii_m=(0.05, 0.1, 0.2),
                observable='edc_shape',
                stable_band_m=(0.08, 0.15),
                state='stable_domain_found',
            ),
        ),
        ray_launch=RayLaunchSpec(
            launched_ray_count=100000,
            launch_distribution='uniform_sphere',
            rng_name='pcg64',
            seed=42,
        ),
    )
    qualification = evaluate_geometric_fidelity(
        DOC, profile, sampling=None, evaluated_at_utc=T1
    )
    assert qualification.stochastic_state == 'unqualified'
    assert qualification.fidelity_state == 'insufficient_evidence'


def test_ga_enumeration_case_overlap_rejected():
    profile = _ga_profile()
    with pytest.raises(ValueError, match='covered and untested'):
        build_path_enumeration_qualification(
            document_id=DOC,
            profile_ref=AuthorityRef(
                kind='geometric_fidelity_profile',
                ref_id=profile.profile_id,
                ref_sha256=profile.profile_sha256,
            ),
            deterministic_state='limited',
            named_path_evidence_class='monte_carlo_only',
            covered_cases=('concave_polygon',),
            untested_cases=('concave_polygon',),
            declared_at_utc=T1,
        )


def test_ga_exact_path_requires_qualified_enumeration():
    profile = _ga_profile()
    with pytest.raises(ValueError, match='qualified'):
        build_path_enumeration_qualification(
            document_id=DOC,
            profile_ref=AuthorityRef(
                kind='geometric_fidelity_profile',
                ref_id=profile.profile_id,
                ref_sha256=profile.profile_sha256,
            ),
            deterministic_state='limited',
            named_path_evidence_class='exact_ga_path_eligible',
            declared_at_utc=T1,
        )


def test_ga_repository_append_only(tmp_path):
    repo = CadGeometricFidelityRepository(_scene_repo(tmp_path))
    profile = _ga_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)
    qualification = evaluate_geometric_fidelity(
        DOC, profile, evaluated_at_utc=T1
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(qualification.qualification_id) == (
        qualification
    )
    forged = profile.model_copy(update={'declared_at_utc': T1})
    with pytest.raises(GeometricFidelityIntegrityError):
        repo.save_profile(forged)


def test_ga_repository_integrity_on_mirrored_tamper(tmp_path):
    repo = CadGeometricFidelityRepository(_scene_repo(tmp_path))
    profile = _ga_profile()
    repo.save_profile(profile)
    import sqlite3

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE cad_geometric_fidelity_profiles "
            "SET receiver_model='spherical_capture' WHERE profile_id=?",
            (profile.profile_id,),
        )
        connection.commit()
    with pytest.raises(GeometricFidelityIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #687 — wave↔geometrical hybrid handoff


def _component(
    ref_id: str, band: tuple[float, float] | None, semantics: str,
    fidelity_kind: str = 'wave_fidelity_profile',
) -> HybridComponentPin:
    return HybridComponentPin(
        prediction_ref=_ref('solver_result', ref_id, SHA_C),
        fidelity_profile_ref=_ref(fidelity_kind, f'{ref_id}-prof', SHA_D),
        qualified_band_hz=band,
        semantics=semantics,
    )


def _hybrid_profile(**overrides):
    params = dict(
        document_id=DOC,
        wave_component=_component(
            'wave-1', (20.0, 500.0), 'complex_pressure',
        ),
        ga_component=_component(
            'ga-1', (400.0, 8000.0), 'energy_histogram',
            fidelity_kind='geometric_fidelity_profile',
        ),
        scene_revision_ref=_ref('scene_revision', 'scene-1', SHA_B),
        source_normalization=SourceNormalizationSpec(
            source_strength_basis='unit_volume_velocity_m3_s',
            reference_condition='common_free_field_reference',
            quantity='mixed_declared',
            phase_reference='source_t0',
            spl_state='absolute',
        ),
        time_alignment=TimeAlignmentSpec(
            common_t0='source_emission',
            direct_path_delay_handling='subsample_fractional_delay',
            filter_group_delay_policy='zero_phase',
        ),
        transition=TransitionBandSpec(
            kind='overlap_blend',
            low_edge_hz=400.0,
            nominal_hz=450.0,
            high_edge_hz=500.0,
            blend_law='raised_cosine',
            selection_basis='component_envelope_intersection',
        ),
        crossover_filter=CrossoverFilterSpec(
            family='linkwitz_riley',
            order=4,
            corner_hz=(400.0, 500.0),
            phase_characteristic='linear_phase_fir',
            zero_phase=True,
            sample_rate_hz=48000.0,
            implementation='scipy.signal.firls',
            implementation_version='1.14',
            complementary_declared=True,
        ),
        output_capability='magnitude_energy_only',
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave',
                phenomena=(
                    'direct_path',
                    'specular_reflection',
                    'modal_response',
                    'diffraction',
                ),
            ),
            PhenomenonDeclaration(
                branch='geometric',
                phenomena=('specular_reflection', 'scattering_late'),
            ),
        ),
        composition_axes=('frequency_domain',),
        declared_at_utc=T0,
    )
    params.update(overrides)
    return build_hybrid_composition_profile(**params)


def _continuity(**overrides) -> ContinuityEvidence:
    params = dict(
        magnitude_step_db=0.4,
        magnitude_ripple_db=0.8,
        phase_discontinuity_rad=0.05,
        direct_arrival_continuity='continuous',
        reflection_timing_continuity='continuous',
        edc_continuity='continuous',
        band_energy_conservation='continuous',
        spatial_continuity='continuous',
    )
    params.update(overrides)
    return ContinuityEvidence(**params)


def test_hybrid_profile_seals_and_roundtrips(tmp_path):
    repo = CadHybridHandoffRepository(_scene_repo(tmp_path))
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave',
                phenomena=('direct_path', 'modal_response'),
            ),
            PhenomenonDeclaration(
                branch='geometric',
                phenomena=('scattering_late',),
            ),
        ),
    )
    assert profile.profile_id.startswith('hybprof-')
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile


def test_hybrid_handoff_overlap_qualified():
    profile = _hybrid_profile()
    qualification = evaluate_hybrid_handoff(
        DOC, profile, continuity=_continuity(), evaluated_at_utc=T1
    )
    # specular_reflection is declared in both branches without a
    # decomposition note → double_count_risk
    assert qualification.handoff_state == 'double_count_risk'
    assert any(
        'specular_reflection' in finding
        for finding in qualification.double_count_findings
    )


def test_hybrid_handoff_qualified_with_clean_split():
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave',
                phenomena=('direct_path', 'modal_response'),
            ),
            PhenomenonDeclaration(
                branch='geometric',
                phenomena=('scattering_late',),
            ),
        ),
    )
    qualification = evaluate_hybrid_handoff(
        DOC, profile, continuity=_continuity(), evaluated_at_utc=T1
    )
    assert qualification.handoff_state == 'overlap_qualified'


def test_hybrid_handoff_decomposition_note_avoids_double_count():
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave',
                phenomena=('direct_path', 'diffraction'),
                decomposition_note='wave carries diffraction below 450 Hz',
            ),
            PhenomenonDeclaration(
                branch='geometric',
                phenomena=('diffraction', 'scattering_late'),
                decomposition_note='ga carries diffraction above 450 Hz',
            ),
        ),
    )
    qualification = evaluate_hybrid_handoff(
        DOC, profile, continuity=_continuity(), evaluated_at_utc=T1
    )
    assert qualification.handoff_state == 'overlap_qualified'
    assert any(
        'declared decomposition' in limitation
        for limitation in qualification.limitations
    )


def test_hybrid_handoff_gap_detection():
    profile = _hybrid_profile(
        wave_component=_component(
            'wave-1', (20.0, 300.0), 'complex_pressure'
        ),
        ga_component=_component(
            'ga-1', (600.0, 8000.0), 'energy_histogram',
            fidelity_kind='geometric_fidelity_profile',
        ),
    )
    qualification = evaluate_hybrid_handoff(
        DOC, profile, continuity=_continuity(), evaluated_at_utc=T1
    )
    assert qualification.handoff_state == 'gap_in_capability'
    assert qualification.gap_band_hz == (300.0, 600.0)


def test_hybrid_handoff_missing_bands_is_insufficient():
    profile = _hybrid_profile(
        wave_component=_component('wave-1', None, 'complex_pressure'),
    )
    qualification = evaluate_hybrid_handoff(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qualification.handoff_state == 'insufficient_evidence'


def test_hybrid_handoff_no_continuity_is_unqualified():
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave', phenomena=('direct_path',)
            ),
            PhenomenonDeclaration(
                branch='geometric', phenomena=('scattering_late',)
            ),
        ),
    )
    qualification = evaluate_hybrid_handoff(
        DOC, profile, continuity=None, evaluated_at_utc=T1
    )
    assert qualification.handoff_state == 'transition_unqualified'


def test_hybrid_handoff_holdout_tuning_caps_verdict():
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave', phenomena=('direct_path',)
            ),
            PhenomenonDeclaration(
                branch='geometric', phenomena=('scattering_late',)
            ),
        ),
    )
    calibration = CalibrationDiscipline(
        tuned_parameters=('crossover_nominal_hz',),
        tuned_on_validation_holdout=True,
    )
    qualification = evaluate_hybrid_handoff(
        DOC,
        profile,
        continuity=_continuity(),
        calibration=calibration,
        evaluated_at_utc=T1,
    )
    assert qualification.handoff_state == 'qualified_with_limitations'
    assert any(
        'holdout' in limitation
        for limitation in qualification.limitations
    )


def test_hybrid_profile_filtered_kind_requires_filter():
    with pytest.raises(ValueError, match='crossover filter'):
        _hybrid_profile(crossover_filter=None)


def test_hybrid_profile_pressure_energy_requires_normalization():
    with pytest.raises(ValueError, match='normalization'):
        _hybrid_profile(
            source_normalization=SourceNormalizationSpec(
                source_strength_basis='unit_volume_velocity_m3_s',
                reference_condition='common_free_field_reference',
                quantity='pressure',
            )
        )


def test_hybrid_profile_requires_both_branches():
    with pytest.raises(ValueError, match='wave'):
        _hybrid_profile(
            phenomenon_declarations=(
                PhenomenonDeclaration(
                    branch='wave', phenomena=('direct_path',)
                ),
                PhenomenonDeclaration(
                    branch='wave', phenomena=('modal_response',)
                ),
            ),
        )


def test_hybrid_transition_edge_order_enforced():
    with pytest.raises(ValueError, match='low edge'):
        _hybrid_profile(
            transition=TransitionBandSpec(
                kind='hard_split',
                low_edge_hz=500.0,
                high_edge_hz=400.0,
                blend_law='none',
                selection_basis='declared_other',
            ),
            crossover_filter=None,
        )


def test_hybrid_repository_integrity_on_tamper(tmp_path):
    repo = CadHybridHandoffRepository(_scene_repo(tmp_path))
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave', phenomena=('direct_path',)
            ),
            PhenomenonDeclaration(
                branch='geometric', phenomena=('scattering_late',)
            ),
        ),
    )
    repo.save_profile(profile)
    import sqlite3

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE cad_hybrid_composition_profiles "
            "SET output_capability='complex_phase_bearing' "
            'WHERE profile_id=?',
            (profile.profile_id,),
        )
        connection.commit()
    with pytest.raises(HybridHandoffIntegrityError):
        repo.get_profile(profile.profile_id)


def test_hybrid_qualification_roundtrip(tmp_path):
    repo = CadHybridHandoffRepository(_scene_repo(tmp_path))
    profile = _hybrid_profile(
        phenomenon_declarations=(
            PhenomenonDeclaration(
                branch='wave', phenomena=('direct_path',)
            ),
            PhenomenonDeclaration(
                branch='geometric', phenomena=('scattering_late',)
            ),
        ),
    )
    repo.save_profile(profile)
    qualification = evaluate_hybrid_handoff(
        DOC,
        profile,
        continuity=_continuity(),
        observable_validity=(
            ObservableValidity(
                observable='magnitude_fr',
                valid_band_hz=(20.0, 8000.0),
                state='qualified',
            ),
            ObservableValidity(
                observable='complex_phase',
                valid_band_hz=(20.0, 500.0),
                state='limited',
                detail='GA branch carries no phase authority',
            ),
        ),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(qualification)
    loaded = repo.get_qualification(qualification.qualification_id)
    assert loaded == qualification
    assert loaded.handoff_state == 'overlap_qualified'


def test_hybrid_component_requires_sha_pin():
    with pytest.raises(ValueError, match='sha256'):
        HybridComponentPin(
            prediction_ref=AuthorityRef(
                kind='solver_result', ref_id='x', ref_sha256=None
            ),
            semantics='complex_pressure',
        )
