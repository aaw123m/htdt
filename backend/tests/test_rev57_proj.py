"""REV57-PROJ regression tests — #619 spatial projection-image
qualification, #622 projection image-geometry / masking, #624 projector
hush-box / enclosure co-design, #627 projector optical-radiation
safety."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_spatial_image_authority import (
    CadMeasurementViewpoint,
    CadProjectorOpticalState,
    CadScreenStateSnapshot,
    CadSpatialCriterion,
    CadSpatialObservation,
    CadSpatialSamplePoint,
    ansi_9_point_grid,
    build_spatial_derived_map,
    build_spatial_measurement_plan,
    build_spatial_measurement_set,
    evaluate_spatial_uniformity,
)
from htdt.cad_spatial_image_repository import (
    CadSpatialImageRepository,
    SpatialImageIntegrityError,
)
from htdt.cad_projection_geometry_authority import (
    CadCropAttribution,
    CadGeometryQuantityReading,
    CadMaskingEdgeState,
    CadRasterStageDeclaration,
    build_image_geometry_measurement,
    build_lens_memory_recall,
    build_presentation_geometry_binding,
    evaluate_presentation_geometry,
)
from htdt.cad_projection_geometry_repository import (
    CadProjectionGeometryRepository,
    ProjectionGeometryIntegrityError,
)
from htdt.cad_hushbox_authority import (
    CadEnclosureAirPath,
    CadEnclosureFan,
    CadOpticalPort,
    build_acoustic_observation,
    build_enclosure_plan,
    build_install_constraints,
    build_operating_observation,
    evaluate_enclosure,
)
from htdt.cad_hushbox_repository import (
    CadHushboxRepository,
    HushboxIntegrityError,
)
from htdt.cad_optical_safety_authority import (
    CadAccessiblePosition,
    CadHazardDistanceRule,
    build_placement,
    build_safety_constraints,
    build_safety_identity,
    evaluate_optical_safety,
)
from htdt.cad_optical_safety_repository import (
    CadOpticalSafetyRepository,
    OpticalSafetyIntegrityError,
)


DOC = 'doc-rev57-proj'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #619 spatial projection-image qualification
# ---------------------------------------------------------------------------


def _viewpoint(**overrides):
    kwargs = dict(viewpoint_id='seat-primary')
    kwargs.update(overrides)
    return CadMeasurementViewpoint(**kwargs)


def _lum_obs(point_id: str, value: float,
             viewpoint_id: str = 'seat-primary') -> CadSpatialObservation:
    return CadSpatialObservation(
        point_id=point_id,
        viewpoint_id=viewpoint_id,
        quantity='white_luminance',
        value=value,
        units='nits',
        stimulus_profile='sdr_reference',
        observed_at_utc=T1,
    )


def _spatial_plan(**overrides):
    kwargs = dict(
        document_id=DOC,
        layout='ansi_9_point',
        points=ansi_9_point_grid(),
        quantities=('white_luminance',),
        viewpoints=(_viewpoint(),),
        screen_state=CadScreenStateSnapshot(material='matte-white-1.0'),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_spatial_measurement_plan(**kwargs)


def test_ansi_9_point_grid_roles() -> None:
    points = ansi_9_point_grid()
    assert len(points) == 9
    roles = {point.point_id: point.role for point in points}
    assert roles['center'] == 'center'
    assert roles['top-left'] == 'corner'
    assert roles['top-center'] == 'edge'
    for point in points:
        assert 0.0 <= point.x_fraction <= 1.0
        assert 0.0 <= point.y_fraction <= 1.0


def test_spatial_plan_center_only_must_be_single_center() -> None:
    with pytest.raises(ValueError, match='center_only'):
        _spatial_plan(
            layout='center_only',
            points=ansi_9_point_grid(),
        )
    _spatial_plan(
        layout='center_only',
        points=(
            CadSpatialSamplePoint(
                point_id='c', role='center',
                x_fraction=0.5, y_fraction=0.5,
            ),
        ),
    )


def test_spatial_set_rejects_duplicate_point_viewpoint_quantity() -> None:
    plan = _spatial_plan()
    obs = _lum_obs('center', 100.0)
    with pytest.raises(ValueError, match='duplicate'):
        build_spatial_measurement_set(
            document_id=DOC,
            plan=plan,
            observations=(obs, obs),
            declared_at_utc=T1,
        )


def test_chromaticity_observation_requires_pair() -> None:
    with pytest.raises(ValueError, match='x and y'):
        CadSpatialObservation(
            point_id='center', viewpoint_id='seat-primary',
            quantity='white_chromaticity',
            chromaticity_x=0.3127,
        )


def test_spatial_observation_requires_units() -> None:
    with pytest.raises(ValueError, match='units'):
        CadSpatialObservation(
            point_id='center', viewpoint_id='seat-primary',
            quantity='white_luminance', value=100.0,
        )


def test_full_9point_within_profile() -> None:
    plan = _spatial_plan()
    observations = tuple(
        _lum_obs(point.point_id, 100.0 - i)
        for i, point in enumerate(plan.points)
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=observations,
        stimulus_profile='sdr_reference',
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        criteria={
            'white_luminance': CadSpatialCriterion(
                quantity='white_luminance',
                metric='min_over_max_ratio',
                operator='>=',
                limit=0.8,
            ),
        },
        evaluated_at_utc=T2,
    )
    assert evaluation.coverage_state == 'full_spatial_coverage'
    assert evaluation.verdict_for('white_luminance') == 'within_profile'
    verdict = evaluation.quantity_verdicts[0]
    assert verdict.expected_points == 9
    assert verdict.observed_points == 9
    assert verdict.missing_roles == ()
    assert verdict.min_value == pytest.approx(92.0)
    assert verdict.max_value == pytest.approx(100.0)


def test_center_only_never_claims_uniformity() -> None:
    plan = _spatial_plan(
        layout='center_only',
        points=(
            CadSpatialSamplePoint(
                point_id='c', role='center',
                x_fraction=0.5, y_fraction=0.5,
            ),
        ),
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=(_lum_obs('c', 100.0),),
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        criteria={
            'white_luminance': CadSpatialCriterion(
                quantity='white_luminance',
                metric='min_over_max_ratio',
                operator='>=',
                limit=0.8,
            ),
        },
        evaluated_at_utc=T2,
    )
    assert evaluation.coverage_state == 'center_only'
    assert any('center-only' in reason for reason in evaluation.reasons)


def test_partial_coverage_reported_not_filled() -> None:
    plan = _spatial_plan()
    observations = tuple(
        _lum_obs(point.point_id, 100.0)
        for point in plan.points
        if point.role != 'corner'
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=observations,
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        criteria={
            'white_luminance': CadSpatialCriterion(
                quantity='white_luminance',
                metric='min_over_max_ratio',
                operator='>=',
                limit=0.8,
            ),
        },
        evaluated_at_utc=T2,
    )
    assert evaluation.coverage_state == 'partial_spatial_coverage'
    verdict = evaluation.quantity_verdicts[0]
    assert verdict.state == 'insufficient_coverage'
    assert 'corner' in verdict.missing_roles


def test_unstabilized_projector_blocks_verdict() -> None:
    plan = _spatial_plan(stabilization_required=True)
    observations = tuple(
        _lum_obs(point.point_id, 100.0) for point in plan.points
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=observations,
        projector_state=CadProjectorOpticalState(
            stabilization_state='warming',
        ),
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        criteria={
            'white_luminance': CadSpatialCriterion(
                quantity='white_luminance',
                metric='min_over_max_ratio',
                operator='>=',
                limit=0.8,
            ),
        },
        evaluated_at_utc=T2,
    )
    assert evaluation.quantity_verdicts[0].state == 'insufficient_evidence'
    assert any('stabiliz' in r for r in evaluation.reasons)


def test_unbound_criterion_never_invents_threshold() -> None:
    plan = _spatial_plan()
    observations = tuple(
        _lum_obs(point.point_id, 100.0) for point in plan.points
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=observations,
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        evaluated_at_utc=T2,
    )
    assert evaluation.quantity_verdicts[0].state == 'criterion_unbound'


def test_evaluation_rejects_plan_mismatch() -> None:
    plan = _spatial_plan()
    other_plan = _spatial_plan(declared_at_utc=T1)
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=(_lum_obs('center', 100.0),),
        declared_at_utc=T1,
    )
    with pytest.raises(ValueError, match='does not bind'):
        evaluate_spatial_uniformity(
            document_id=DOC,
            plan=other_plan,
            measurement_set=measurement_set,
            evaluated_at_utc=T2,
        )


def test_predicted_evidence_flagged() -> None:
    plan = _spatial_plan()
    observations = tuple(
        _lum_obs(point.point_id, 100.0) for point in plan.points
    )
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=observations,
        evidence_kind='predicted',
        declared_at_utc=T1,
    )
    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        evaluated_at_utc=T2,
    )
    assert evaluation.evidence_kind == 'predicted'
    assert any('predicted' in r for r in evaluation.reasons)


def test_derived_map_requires_provenance() -> None:
    plan = _spatial_plan()
    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=(_lum_obs('center', 100.0),),
        declared_at_utc=T1,
    )
    derived = build_spatial_derived_map(
        document_id=DOC,
        source_set=measurement_set,
        quantity='white_luminance',
        interpolation_algorithm='nearest',
        algorithm_version='1.0',
        grid_resolution='32x18',
        declared_at_utc=T2,
    )
    assert derived.source_set_ref.ref_sha256 == measurement_set.set_sha256


def test_spatial_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadSpatialImageRepository(scene)
    plan = _spatial_plan()
    repo.save_plan(plan)
    assert repo.get_plan(plan.plan_id) == plan
    repo.save_plan(plan)  # idempotent

    measurement_set = build_spatial_measurement_set(
        document_id=DOC,
        plan=plan,
        observations=(_lum_obs('center', 100.0),),
        declared_at_utc=T1,
    )
    repo.save_measurement_set(measurement_set)
    assert repo.get_measurement_set(measurement_set.set_id) == (
        measurement_set
    )

    derived = build_spatial_derived_map(
        document_id=DOC,
        source_set=measurement_set,
        quantity='white_luminance',
        interpolation_algorithm='nearest',
        algorithm_version='1.0',
        grid_resolution='32x18',
        declared_at_utc=T2,
    )
    repo.save_derived_map(derived)
    assert repo.get_derived_map(derived.map_id) == derived

    evaluation = evaluate_spatial_uniformity(
        document_id=DOC,
        plan=plan,
        measurement_set=measurement_set,
        evaluated_at_utc=T2,
    )
    repo.save_evaluation(evaluation)
    assert repo.get_evaluation(evaluation.evaluation_id) == evaluation

    assert len(repo.list_plans(DOC)) == 1
    assert len(repo.list_measurement_sets(DOC)) == 1
    assert len(repo.list_derived_maps(DOC)) == 1
    assert len(repo.list_evaluations(DOC)) == 1


def test_spatial_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadSpatialImageRepository(scene)
    plan = _spatial_plan()
    repo.save_plan(plan)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_spatial_measurement_plans SET layout='center_only' "
            'WHERE plan_id=?',
            (plan.plan_id,),
        )
        connection.commit()
    with pytest.raises(SpatialImageIntegrityError):
        repo.get_plan(plan.plan_id)


# ---------------------------------------------------------------------------
# #622 projection image-geometry / masking
# ---------------------------------------------------------------------------


def _geometry_binding(**overrides):
    kwargs = dict(
        document_id=DOC,
        presentation_profile_ref=_ref('presentation_profile', 'pp-1'),
        screen_ref=_ref('screen', 'scr-1'),
        projector_ref=_ref('projector_specification', 'pj-1'),
        content_aspect=2.39,
        projected_aspect=2.39,
        anamorphic_state='lens_inserted',
        keystone_state='none',
        warp_state='none',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_presentation_geometry_binding(**kwargs)


def _raster_chain() -> tuple[CadRasterStageDeclaration, ...]:
    return (
        CadRasterStageDeclaration(
            stage='source_active_raster',
            width=4096, height=1716, units='px', source='declared',
        ),
        CadRasterStageDeclaration(
            stage='processed_output_raster',
            width=3840, height=1608, units='px', source='readback',
        ),
        CadRasterStageDeclaration(
            stage='projector_imaging_raster',
            width=3840, height=2160, units='px', source='readback',
        ),
        CadRasterStageDeclaration(
            stage='projected_image_boundary',
            width=3.0, height=1.256, units='m', source='measured',
        ),
        CadRasterStageDeclaration(
            stage='visible_screen_boundary',
            width=3.0, height=1.256, units='m', source='measured',
        ),
    )


def _measurement(binding, **overrides):
    kwargs = dict(
        document_id=DOC,
        binding=binding,
        method='camera_based_image_geometry',
        test_pattern_identity='SMPTE RP40 framing grid',
        physical_alignment='physically_aligned',
        raster_chain=_raster_chain(),
        observed_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_image_geometry_measurement(**kwargs)


def test_geometry_measurement_requires_test_pattern() -> None:
    binding = _geometry_binding()
    with pytest.raises(ValueError):
        build_image_geometry_measurement(
            document_id=DOC,
            binding=binding,
            method='manual_grid_visual_inspection',
            test_pattern_identity='',
            observed_at_utc=T1,
        )


def test_geometry_no_measurement_is_insufficient() -> None:
    binding = _geometry_binding()
    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'insufficient_evidence'
    assert any('nominal throw' in r for r in evaluation.reasons)


def test_geometry_all_pass_verified() -> None:
    binding = _geometry_binding()
    measurement = _measurement(
        binding,
        readings=(
            CadGeometryQuantityReading(
                quantity='aspect_ratio',
                observed_state='PASS',
            ),
            CadGeometryQuantityReading(
                quantity='crop_per_edge',
                edge='left', value=0.0, units='px',
            ),
        ),
        masking_edges=(
            CadMaskingEdgeState(edge='left', observed_state='PASS'),
            CadMaskingEdgeState(edge='right', observed_state='PASS'),
        ),
    )
    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        measurement=measurement,
        tolerances={'crop_per_edge': 0.5},
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'verified_with_limitations'
    assert evaluation.digital_correction_state == 'none'
    states = dict(evaluation.quantity_states)
    assert states['aspect_ratio'] == 'PASS'
    assert states['crop_per_edge'] == 'PASS'
    assert states['masking_overlap_gap'] == 'PASS'
    assert states['keystone_trapezoid'] == 'UNKNOWN'


def test_geometry_digital_correction_carries_cost() -> None:
    binding = _geometry_binding(keystone_state='active')
    measurement = _measurement(
        binding,
        physical_alignment='physically_misaligned',
        active_corrections=('digital_keystone',),
    )
    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        measurement=measurement,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'verified_with_digital_correction'
    assert evaluation.physical_alignment == 'physically_misaligned'
    assert evaluation.correction_costs


def test_geometry_masking_intrusion_fails() -> None:
    binding = _geometry_binding()
    measurement = _measurement(
        binding,
        masking_edges=(
            CadMaskingEdgeState(
                edge='left', overlap_m=0.02, observed_state='FAIL',
            ),
        ),
    )
    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        measurement=measurement,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'failed'
    states = dict(evaluation.quantity_states)
    assert states['masking_overlap_gap'] == 'FAIL'


def test_geometry_anamorphic_conflict_fails() -> None:
    binding = _geometry_binding(
        anamorphic_state='electronic_stretch',
        content_aspect=2.39,
        projected_aspect=2.39,
    )
    measurement = _measurement(binding)
    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        measurement=measurement,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'failed'
    assert any('anamorphic' in r for r in evaluation.reasons)


def test_geometry_rejects_binding_mismatch() -> None:
    binding = _geometry_binding()
    other = _geometry_binding(projected_aspect=2.35)
    measurement = _measurement(other)
    with pytest.raises(ValueError, match='does not bind'):
        evaluate_presentation_geometry(
            document_id=DOC,
            binding=binding,
            measurement=measurement,
            evaluated_at_utc=T2,
        )


def test_lens_recall_records() -> None:
    recall = build_lens_memory_recall(
        document_id=DOC,
        memory_id='scope-position',
        cycle_index=1,
        position_error=1.5,
        position_error_units='px',
        repeatable=True,
        observed_at_utc=T1,
    )
    assert recall.repeatable is True
    with pytest.raises(ValueError, match='units'):
        build_lens_memory_recall(
            document_id=DOC,
            memory_id='scope-position',
            cycle_index=2,
            position_error=1.0,
            observed_at_utc=T1,
        )


def test_geometry_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadProjectionGeometryRepository(scene)
    binding = _geometry_binding()
    repo.save_binding(binding)
    assert repo.get_binding(binding.binding_id) == binding
    repo.save_binding(binding)

    measurement = _measurement(binding)
    repo.save_measurement(measurement)
    assert repo.get_measurement(measurement.measurement_id) == measurement

    recall = build_lens_memory_recall(
        document_id=DOC,
        memory_id='mem-1',
        cycle_index=1,
        observed_at_utc=T1,
    )
    repo.save_lens_recall(recall)
    assert repo.get_lens_recall(recall.recall_id) == recall

    evaluation = evaluate_presentation_geometry(
        document_id=DOC,
        binding=binding,
        measurement=measurement,
        evaluated_at_utc=T2,
    )
    repo.save_evaluation(evaluation)
    assert repo.get_evaluation(evaluation.evaluation_id) == evaluation

    assert len(repo.list_bindings(DOC)) == 1
    assert len(repo.list_measurements(DOC)) == 1
    assert len(repo.list_lens_recalls(DOC)) == 1
    assert len(repo.list_evaluations(DOC)) == 1


def test_geometry_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadProjectionGeometryRepository(scene)
    binding = _geometry_binding()
    repo.save_binding(binding)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_presentation_geometry_bindings "
            "SET keystone_state='active' WHERE binding_id=?",
            (binding.binding_id,),
        )
        connection.commit()
    with pytest.raises(ProjectionGeometryIntegrityError):
        repo.get_binding(binding.binding_id)


# ---------------------------------------------------------------------------
# #624 hush-box / enclosure co-design
# ---------------------------------------------------------------------------


def _install_constraints(**overrides):
    kwargs = dict(
        document_id=DOC,
        manufacturer='JVC',
        model='DLA-NZ900',
        operating_temp_min_c=5.0,
        operating_temp_max_c=35.0,
        humidity_min_pct=20.0,
        humidity_max_pct=80.0,
        clearance_rear_m=0.3,
        intake_locations='rear',
        exhaust_locations='front',
        source_document='DLA-NZ900 installation manual',
        source_revision='rev-A',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_install_constraints(**kwargs)


def _enclosure_plan(constraints=None, **overrides):
    kwargs = dict(
        document_id=DOC,
        constraint_ref=constraints,
        internal_volume_m3=0.5,
        air_path=CadEnclosureAirPath(
            intake_source='room',
            exhaust_destination='ducted',
            recirculation='none',
        ),
        fans=(
            CadEnclosureFan(
                fan_id='exhaust-1',
                role='exhaust',
                airflow_basis='installed_measured',
                airflow_cfm=60.0,
            ),
        ),
        access_panels='front and side panels',
        mount_assembly='suspended cradle',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_enclosure_plan(**kwargs)


def test_enclosure_remote_projection_requires_port() -> None:
    with pytest.raises(ValueError, match='optical port'):
        _enclosure_plan(remote_projection=True)


def test_fan_free_air_rating_kept_distinct() -> None:
    fan = CadEnclosureFan(
        fan_id='f1',
        role='intake',
        airflow_basis='free_air_rating',
        airflow_cfm=80.0,
    )
    assert fan.airflow_basis == 'free_air_rating'
    with pytest.raises(ValueError, match='basis'):
        CadEnclosureFan(
            fan_id='f2', role='intake', airflow_cfm=80.0,
        )


def test_enclosure_no_constraints_insufficient() -> None:
    plan = _enclosure_plan()
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'
    assert qualification.thermal_state == (
        'ventilation_requirement_unknown'
    )


def test_enclosure_cold_short_test_not_evidence() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(constraints=constraints)
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'
    assert qualification.thermal_state == 'insufficient_evidence'


def test_enclosure_qualified_full_stack() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(
        constraints=constraints,
        optical_port=CadOpticalPort(
            material='AR-coated low-iron glass',
            transmission_loss_pct=1.5,
            ghosting_flare='none_observed',
            contrast_impact='none_observed',
            focus_impact='none_observed',
        ),
    )
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='long_movie_warmed',
        duration_s=7200.0,
        time_to_stability_s=1800.0,
        inlet_temp_c=24.0,
        ambient_temp_c=22.0,
        projector_fan_state='normal',
        protection_event='none',
        measured_at_utc=T1,
    )
    acoustic = build_acoustic_observation(
        document_id=DOC,
        plan=plan,
        comparability='same_state',
        pre_spl_db=30.0,
        post_spl_db=20.0,
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        acoustic=acoustic,
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'qualified'
    assert qualification.thermal_state == 'thermally_measured_stable'
    assert qualification.acoustic_state == 'net_reduction_documented'
    assert qualification.optical_state == 'port_within_limits'


def test_enclosure_protection_event_fails() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(constraints=constraints)
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='hdr_high_output',
        duration_s=3600.0,
        projector_fan_state='max',
        protection_event='shutdown',
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'failed'
    assert qualification.thermal_state == 'over_temperature_event'


def test_enclosure_temp_violation_fails() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(constraints=constraints)
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='high_ambient',
        duration_s=3600.0,
        inlet_temp_c=40.0,
        time_to_stability_s=900.0,
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'failed'
    assert qualification.thermal_state == (
        'manufacturer_constraint_violated'
    )


def test_enclosure_acoustic_across_states_not_comparable() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(constraints=constraints)
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='long_movie_warmed',
        duration_s=7200.0,
        time_to_stability_s=1800.0,
        measured_at_utc=T1,
    )
    acoustic = build_acoustic_observation(
        document_id=DOC,
        plan=plan,
        comparability='different_state',
        pre_spl_db=30.0,
        post_spl_db=18.0,
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        acoustic=acoustic,
        evaluated_at_utc=T2,
    )
    assert qualification.acoustic_state == 'not_comparable'
    assert qualification.verdict == 'qualified_with_limitations'


def test_enclosure_fan_escalation_negates() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(constraints=constraints)
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='long_movie_warmed',
        duration_s=7200.0,
        time_to_stability_s=1800.0,
        inlet_temp_c=30.0,
        projector_fan_state='escalated',
        measured_at_utc=T1,
    )
    acoustic = build_acoustic_observation(
        document_id=DOC,
        plan=plan,
        comparability='same_state',
        pre_spl_db=30.0,
        post_spl_db=31.0,
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        acoustic=acoustic,
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'failed'
    assert qualification.thermal_state == 'thermally_limited'
    assert qualification.acoustic_state == 'fan_escalation_negates'


def test_enclosure_uncharacterized_port_limits() -> None:
    constraints = _install_constraints()
    plan = _enclosure_plan(
        constraints=constraints,
        optical_port=CadOpticalPort(material='window glass'),
    )
    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='long_movie_warmed',
        duration_s=7200.0,
        time_to_stability_s=1800.0,
        measured_at_utc=T1,
    )
    acoustic = build_acoustic_observation(
        document_id=DOC,
        plan=plan,
        comparability='same_state',
        pre_spl_db=30.0,
        post_spl_db=20.0,
        measured_at_utc=T1,
    )
    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        acoustic=acoustic,
        evaluated_at_utc=T2,
    )
    assert qualification.optical_state == 'port_uncharacterized'
    assert qualification.verdict == 'qualified_with_limitations'


def test_hushbox_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadHushboxRepository(scene)
    constraints = _install_constraints()
    repo.save_constraints(constraints)
    assert repo.get_constraints(constraints.constraint_id) == constraints
    repo.save_constraints(constraints)

    plan = _enclosure_plan(constraints=constraints)
    repo.save_plan(plan)
    assert repo.get_plan(plan.plan_id) == plan

    observation = build_operating_observation(
        document_id=DOC,
        plan=plan,
        scenario='sdr_low_power',
        duration_s=1800.0,
        measured_at_utc=T1,
    )
    repo.save_operating_observation(observation)
    assert repo.get_operating_observation(
        observation.observation_id
    ) == observation

    acoustic = build_acoustic_observation(
        document_id=DOC,
        plan=plan,
        comparability='same_state',
        pre_spl_db=30.0,
        post_spl_db=21.0,
        measured_at_utc=T1,
    )
    repo.save_acoustic_observation(acoustic)
    assert repo.get_acoustic_observation(
        acoustic.acoustic_id
    ) == acoustic

    qualification = evaluate_enclosure(
        document_id=DOC,
        plan=plan,
        constraints=constraints,
        observations=(observation,),
        acoustic=acoustic,
        evaluated_at_utc=T2,
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(
        qualification.qualification_id
    ) == qualification

    assert len(repo.list_constraints(DOC)) == 1
    assert len(repo.list_plans(DOC)) == 1
    assert len(repo.list_operating_observations(DOC)) == 1
    assert len(repo.list_acoustic_observations(DOC)) == 1
    assert len(repo.list_qualifications(DOC)) == 1


def test_hushbox_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadHushboxRepository(scene)
    constraints = _install_constraints()
    repo.save_constraints(constraints)
    with repo._connect() as connection:
        connection.execute(
            'UPDATE cad_projector_install_constraints '
            "SET source_document='tampered' WHERE constraint_id=?",
            (constraints.constraint_id,),
        )
        connection.commit()
    with pytest.raises(HushboxIntegrityError):
        repo.get_constraints(constraints.constraint_id)


# ---------------------------------------------------------------------------
# #627 optical-radiation safety
# ---------------------------------------------------------------------------


def _safety_identity(**overrides):
    kwargs = dict(
        document_id=DOC,
        manufacturer='Barco',
        model='Njord',
        illumination_source='laser',
        risk_group='rg2',
        risk_group_source='manufacturer datasheet rev C',
        laser_class='not_applicable',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_safety_identity(**kwargs)


def _safety_constraints(identity=None, **overrides):
    kwargs = dict(
        document_id=DOC,
        safety_identity_ref=(
            None if identity is None else AuthorityRef(
                kind='projector_safety_identity',
                ref_id=identity.identity_id,
                ref_sha256=identity.identity_sha256,
            )
        ),
        hazard_distance_rules=(
            CadHazardDistanceRule(
                rule_id='base-rule',
                applies_to='base',
                hazard_distance_m=1.5,
                restricted_below_m=2.0,
            ),
        ),
        source_document='safety manual rev 2',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_safety_constraints(**kwargs)


def _placement(identity, constraints=None, **overrides):
    kwargs = dict(
        document_id=DOC,
        safety_identity=identity,
        constraints=constraints,
        operating_state='normal_operation',
        throw_distance_m=4.0,
        viewer_position='no_audience_in_hazard_zone',
        accessible_positions=(
            CadAccessiblePosition(
                position_id='seat-1',
                kind='seated',
                distance_from_lens_m=3.0,
                height_m=1.1,
                in_beam_path=True,
            ),
        ),
        declared_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_placement(**kwargs)


def test_safety_identity_requires_source_for_classification() -> None:
    with pytest.raises(ValueError, match='source'):
        _safety_identity(risk_group_source=None)


def test_safety_service_state_never_user_safe() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement = _placement(
        identity, constraints,
        operating_state='service_interlock_defeat',
    )
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'service_state_not_user_safe'


def test_safety_no_constraints_local_review() -> None:
    identity = _safety_identity()
    placement = _placement(identity, constraints=None)
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'local_review_required'
    assert any('review' in r for r in evaluation.reasons)


def test_safety_within_documented_constraints() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement = _placement(identity, constraints)
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == (
        'installation_within_documented_constraints'
    )
    assert evaluation.zone_results[0]['result'] == 'clear'


def test_safety_zone_conflict() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement = _placement(
        identity, constraints,
        accessible_positions=(
            CadAccessiblePosition(
                position_id='child-low',
                kind='floor_standing',
                distance_from_lens_m=0.8,
                height_m=0.9,
                in_beam_path=True,
            ),
        ),
        viewer_position='audience_within_hazard_zone',
    )
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'safety_zone_conflict'


def test_safety_lens_accessory_unscoped() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement = _placement(
        identity, constraints,
        lens_accessory='anamorphic-adapter-x',
    )
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'lens_accessory_applicability_unknown'


def test_safety_no_identity_insufficient() -> None:
    identity = _safety_identity()
    placement = _placement(identity)
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=None,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'insufficient_evidence'


def test_safety_unknown_classification_insufficient() -> None:
    identity = _safety_identity(
        risk_group='unknown', risk_group_source=None,
        laser_class='unknown', laser_class_source=None,
    )
    constraints = _safety_constraints(identity)
    placement = _placement(identity, constraints)
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == 'insufficient_evidence'


def test_safety_stale_after_placement_change() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement_a = _placement(identity, constraints)
    evaluation_a = evaluate_optical_safety(
        document_id=DOC,
        placement=placement_a,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T1,
    )
    placement_b = _placement(
        identity, constraints, throw_distance_m=3.5,
    )
    evaluation_b = evaluate_optical_safety(
        document_id=DOC,
        placement=placement_b,
        safety_identity=identity,
        constraints=constraints,
        prior_evaluation=evaluation_a,
        evaluated_at_utc=T2,
    )
    assert evaluation_b.verdict == 'stale_after_change'


def test_safety_remote_power_not_a_control() -> None:
    identity = _safety_identity()
    constraints = _safety_constraints(identity)
    placement = _placement(
        identity, constraints,
        remote_power_capable=True,
        accessible_positions=(
            CadAccessiblePosition(
                position_id='pos-1',
                kind='seated',
                distance_from_lens_m=3.0,
                height_m=1.1,
                in_beam_path=True,
            ),
        ),
    )
    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    assert evaluation.verdict == (
        'installation_within_documented_constraints'
    )
    assert any('not a safety control' in r for r in evaluation.reasons)


def test_optical_safety_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadOpticalSafetyRepository(scene)
    identity = _safety_identity()
    repo.save_safety_identity(identity)
    assert repo.get_safety_identity(identity.identity_id) == identity
    repo.save_safety_identity(identity)

    constraints = _safety_constraints(identity)
    repo.save_safety_constraints(constraints)
    assert repo.get_safety_constraints(
        constraints.constraint_id
    ) == constraints

    placement = _placement(identity, constraints)
    repo.save_placement(placement)
    assert repo.get_placement(placement.placement_id) == placement

    evaluation = evaluate_optical_safety(
        document_id=DOC,
        placement=placement,
        safety_identity=identity,
        constraints=constraints,
        evaluated_at_utc=T2,
    )
    repo.save_evaluation(evaluation)
    assert repo.get_evaluation(evaluation.evaluation_id) == evaluation

    assert len(repo.list_safety_identities(DOC)) == 1
    assert len(repo.list_safety_constraints(DOC)) == 1
    assert len(repo.list_placements(DOC)) == 1
    assert len(repo.list_evaluations(DOC)) == 1


def test_optical_safety_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadOpticalSafetyRepository(scene)
    identity = _safety_identity()
    repo.save_safety_identity(identity)
    with repo._connect() as connection:
        connection.execute(
            'UPDATE cad_projector_safety_identities '
            "SET risk_group='rg0' WHERE identity_id=?",
            (identity.identity_id,),
        )
        connection.commit()
    with pytest.raises(OpticalSafetyIntegrityError):
        repo.get_safety_identity(identity.identity_id)
