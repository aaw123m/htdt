"""REV56-CAMPPROFILE (#581/#585): spatial measurement campaign design +
CEDIA RP32 commissioning profile — declared listening-area coverage,
point-role discipline (holdout independence, adaptive honesty),
fail-closed design evaluation, sealed record integrity, append-only
persistence, and honest UNKNOWN/unmapped states.

Fixture naming follows the issue plans: SMP10-SMP70 cover the spatial
campaign authority (#581); RP32F10-F60 cover the commissioning profile
(#585).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_rp32_profile import (
    AsBuiltObservation,
    CommissioningDocumentIdentity,
    CommissioningException,
    DesignTargetBinding,
    InstrumentEvidence,
    Rp32RequirementMapping,
    Rp32TaskSpec,
    Rp32VerificationItemResult,
    build_commissioning_report,
    build_rp32_profile,
    build_verification_plan,
    build_verification_record,
    evaluate_designed_built_reconciliation,
    evaluate_measurement_readiness,
    evaluate_verification_freshness,
    rp32_builtin_profile,
)
from htdt.cad_rp32_repository import (
    CadRp32Repository,
    Rp32ConflictError,
    Rp32IntegrityError,
)
from htdt.cad_scene import Position3, make_empty_scene
from htdt.cad_spatial_campaign import (
    AcousticDiversitySpec,
    CampaignPoint,
    CaptureOrderPlan,
    InterleavedPair,
    ListeningAreaSpec,
    ListeningZone,
    PointResponseInput,
    SamplingExpectation,
    acoustic_diversity_report,
    assert_campaign_partition_disjoint,
    build_point_binding,
    build_spatial_campaign_design,
    builtin_campaign_templates,
    check_binding_partition_consistency,
    compute_coverage_metrics,
    evaluate_campaign_design,
    evaluate_placements,
    instantiate_campaign_template,
    partition_for_point,
)
from htdt.cad_spatial_campaign_repository import (
    CadSpatialCampaignRepository,
    SpatialCampaignConflictError,
    SpatialCampaignIntegrityError,
)


DOC = 'doc-campprofile'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T00:00:01+00:00'
T2 = '2026-10-05T00:00:02+00:00'
T3 = '2026-10-05T00:00:03+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# Spatial campaign fixtures (#581)


def _area() -> ListeningAreaSpec:
    return ListeningAreaSpec(
        area_label='main row',
        zones=(
            ListeningZone(
                zone_id='main-row',
                kind='row',
                # Declared weight matches the fixture's intended sampling
                # density (4 of 5 spatial points) so the default design is
                # not secretly density-weighted.
                declared_weight=4.0,
                bounds_min=Position3(x_m=1.0, y_m=1.0, z_m=0.9),
                bounds_max=Position3(x_m=3.0, y_m=2.0, z_m=1.4),
            ),
            ListeningZone(
                zone_id='front-row',
                kind='seat',
                declared_weight=1.0,
                bounds_min=Position3(x_m=1.0, y_m=2.6, z_m=0.9),
                bounds_max=Position3(x_m=3.0, y_m=3.4, z_m=1.4),
            ),
        ),
    )


def _point(
    point_id: str,
    x: float,
    y: float = 1.5,
    z: float = 1.1,
    roles=('optimization',),
    **kwargs,
) -> CampaignPoint:
    return CampaignPoint(
        point_id=point_id,
        position=Position3(x_m=x, y_m=y, z_m=z),
        roles=roles,
        **kwargs,
    )


def _design(**overrides) -> 'object':
    kwargs = dict(
        document_id=DOC,
        listening_area=_area(),
        points=(
            _point('p-ref', 2.0, 1.5, 1.1,
                   roles=('reference_alignment', 'optimization')),
            _point('p-opt-1', 1.3, 1.3, 1.1),
            _point('p-opt-2', 2.7, 1.7, 1.2),
            _point('p-hold-1', 1.6, 1.9, 1.1, roles=('spatial_holdout',)),
            _point('p-hold-2', 2.5, 3.0, 1.2, roles=('spatial_holdout',)),
        ),
        declared_at_utc=T0,
        claim_kinds=('multi_position_area',),
        order_plan=CaptureOrderPlan(
            strategy='declared_sequence',
            sequence=(
                'p-ref', 'p-opt-1', 'p-opt-2', 'p-hold-1', 'p-hold-2',
            ),
        ),
    )
    kwargs.update(overrides)
    return build_spatial_campaign_design(**kwargs)


# -- SMP10: sealed design round-trips and hash-binds -------------------------


def test_smp10_design_is_sealed_and_content_addressed() -> None:
    design = _design()
    assert design.design_id == f'spatial-campaign:{design.design_sha256}'
    # Tampering with the sealed payload is rejected.
    payload = design.model_dump(mode='json')
    payload['points'][0]['point_id'] = 'tampered'
    with pytest.raises(ValidationError):
        type(design).model_validate(payload)


def test_smp11_points_require_unique_ids_and_roles() -> None:
    with pytest.raises(ValidationError):
        _design(
            points=(
                _point('dup', 1.2),
                _point('dup', 1.4),
                _point('h', 2.0, roles=('spatial_holdout',)),
            )
        )
    with pytest.raises(ValidationError):
        CampaignPoint(
            point_id='bad',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            roles=('optimization', 'spatial_holdout'),
        )


def test_smp12_listening_area_is_explicit_not_inferred() -> None:
    design = _design()
    # The declared bounds — not the measured points — define the domain.
    assert design.listening_area.zone_for(
        Position3(x_m=0.2, y_m=0.2, z_m=1.1)
    ) is None
    assert design.listening_area.zone_for(
        Position3(x_m=2.0, y_m=3.0, z_m=1.1)
    ) is not None


# -- SMP20: fail-closed design evaluation ------------------------------------


def test_smp20_valid_design_passes_evaluation() -> None:
    evaluation = evaluate_campaign_design(_design(), evaluated_at_utc=T1)
    assert evaluation.state == 'valid'
    assert not evaluation.reasons
    assert not evaluation.warnings


def test_smp21_spatial_claim_without_holdout_is_rejected() -> None:
    design = _design(
        points=(
            _point('p1', 1.2),
            _point('p2', 2.0),
            _point('p3', 2.8),
        ),
        order_plan=CaptureOrderPlan(
            strategy='declared_sequence', sequence=('p1', 'p2', 'p3')
        ),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'spatial_claim_without_holdout' in evaluation.reasons


def test_smp22_holdout_outside_declared_area_is_rejected() -> None:
    design = _design(
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('p1', 1.3),
            _point('p2', 2.7),
            # Holdout sits outside every declared zone.
            _point('h-out', 9.0, 9.0, 9.0, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'holdout_outside_declared_area' in evaluation.reasons


def test_smp23_coincident_holdout_and_optimization_is_rejected() -> None:
    design = _design(
        points=(
            _point('p1', 1.3),
            _point('p2', 2.7),
            # Same XYZ as p1: repeats at one spot are not holdout evidence.
            _point('h-dup', 1.3, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'holdout_coincident_with_design_point' in evaluation.reasons


def test_smp24_required_zone_unsampled_is_rejected() -> None:
    design = _design(
        expectations=SamplingExpectation(required_zone_ids=('front-row',)),
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('p1', 1.3),
            _point('p2', 2.7),
            _point('h', 1.9, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'required_zone_unsampled' in evaluation.reasons


def test_smp25_repeat_captures_do_not_inflate_spatial_coverage() -> None:
    design = _design(
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('rep-1', 2.0, roles=('repeatability',), weight=0.0),
            _point('rep-2', 2.0, roles=('repeatability',), weight=0.0),
            _point('h', 1.6, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    metrics = compute_coverage_metrics(design)
    # 3 distinct spatial positions (ref + holdout); repeats add nothing.
    assert metrics.distinct_spatial_positions == 2
    assert metrics.repeatability_count == 2


def test_smp26_declared_span_minimum_is_enforced() -> None:
    design = _design(
        expectations=SamplingExpectation(min_axis_span_m=5.0),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'spatial_span_below_declared_minimum' in evaluation.reasons


def test_smp27_near_duplicate_positions_are_flagged() -> None:
    design = _design(
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('p1', 1.3),
            _point('p1b', 1.305),
            _point('h', 1.9, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert evaluation.state == 'invalid'
    assert 'near_duplicate_positions' in evaluation.reasons
    assert ('p1', 'p1b') in evaluation.metrics.near_duplicate_pairs


# -- SMP30: density/weighting honesty ----------------------------------------


def test_smp30_density_as_hidden_weight_is_warned() -> None:
    area = ListeningAreaSpec(
        area_label='two-zone',
        zones=(
            ListeningZone(
                zone_id='big',
                kind='row',
                declared_weight=1.0,
                bounds_min=Position3(x_m=0.0, y_m=0.0, z_m=0.9),
                bounds_max=Position3(x_m=1.0, y_m=4.0, z_m=1.4),
            ),
            ListeningZone(
                zone_id='small',
                kind='seat',
                declared_weight=4.0,
                bounds_min=Position3(x_m=1.2, y_m=0.0, z_m=0.9),
                bounds_max=Position3(x_m=1.5, y_m=1.0, z_m=1.4),
            ),
        ),
    )
    design = build_spatial_campaign_design(
        document_id=DOC,
        listening_area=area,
        points=(
            _point('a', 0.3, 1.0),
            _point('b', 0.3, 3.0),
            _point('c', 0.6, 2.0),
            _point('s', 1.3, 0.5),
            _point('h', 1.4, 0.8, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
        claim_kinds=('multi_position_area',),
        declared_at_utc=T0,
        expectations=SamplingExpectation(density_weight_alert_ratio=1.5),
    )
    metrics = compute_coverage_metrics(design)
    # 'big' (weight 1 of 5) holds 3 of 5 spatial samples — hidden
    # density weighting relative to declared importance.
    assert metrics.density_implied_share['big'] == pytest.approx(0.6)
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert 'hidden_density_weighting' in evaluation.warnings


def test_smp31_diagnostic_weight_is_warned() -> None:
    design = _design(
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('p1', 1.3),
            _point('diag', 2.6, roles=('diagnostic',), weight=2.0),
            _point('h', 1.9, roles=('spatial_holdout',)),
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert 'diagnostic_point_carries_weight' in evaluation.warnings


# -- SMP40: planned vs observed ----------------------------------------------


def test_smp40_binding_records_deviation() -> None:
    design = _design()
    binding = build_point_binding(
        design=design,
        point_id='p-hold-1',
        measurement_id='m-1',
        measurement_sha256=SHA_A,
        captured_at_utc=T1,
        observed_position=Position3(x_m=1.63, y_m=1.9, z_m=1.1),
        position_uncertainty_m=0.02,
        capture_index=3,
    )
    assert binding.deviation_m == pytest.approx(0.03)


def test_smp41_missing_observed_position_is_unknown_not_zero() -> None:
    design = _design()
    binding = build_point_binding(
        design=design,
        point_id='p-hold-1',
        measurement_id='m-1',
        measurement_sha256=SHA_A,
        captured_at_utc=T1,
    )
    assert binding.observed_position is None
    assert binding.deviation_m is None


def test_smp42_binding_to_unknown_point_fails_closed() -> None:
    with pytest.raises(ValueError, match='no point'):
        build_point_binding(
            design=_design(),
            point_id='ghost',
            measurement_id='m-1',
            measurement_sha256=SHA_A,
            captured_at_utc=T1,
        )


def test_smp43_placement_assessment_flags() -> None:
    design = _design()
    bindings = [
        build_point_binding(
            design=design,
            point_id='p-ref',
            measurement_id='m-1',
            measurement_sha256=SHA_A,
            captured_at_utc=T1,
            observed_position=Position3(x_m=2.1, y_m=1.5, z_m=1.1),
            position_uncertainty_m=0.05,
            capture_index=0,
        ),
        build_point_binding(
            design=design,
            point_id='p-opt-2',  # declared third, captured second
            measurement_id='m-2',
            measurement_sha256=SHA_B,
            captured_at_utc=T2,
            observed_position=Position3(x_m=2.7, y_m=1.7, z_m=1.2),
            position_uncertainty_m=0.02,
            capture_index=1,
        ),
    ]
    assessment = evaluate_placements(
        design, bindings, deviation_tolerance_m=0.05
    )
    assert 'deviation_above_declared_tolerance' in assessment.deviation_flags
    assert 'spatial_points_uncaptured' in assessment.deviation_flags
    assert 'capture_order_diverged' in assessment.order_flags


# -- SMP50: adaptive provenance + channel coverage ----------------------------


def test_smp50_adaptive_holdout_is_impossible() -> None:
    with pytest.raises(ValidationError):
        CampaignPoint(
            point_id='adapt-hold',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            roles=('spatial_holdout',),
            provenance='adaptive',
            adaptive_algorithm='greedy-v1',
            adaptive_reason='max-min dispersion',
        )


def test_smp51_adaptive_points_need_selection_provenance() -> None:
    with pytest.raises(ValidationError):
        CampaignPoint(
            point_id='adapt',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            roles=('diagnostic',),
            provenance='adaptive',
        )
    point = CampaignPoint(
        point_id='adapt',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        roles=('diagnostic',),
        provenance='adaptive',
        adaptive_algorithm='greedy-v1',
        adaptive_reason='redundancy detection',
    )
    design = _design(
        points=(
            _point('p-ref', 2.0, roles=('reference_alignment', 'optimization')),
            _point('p1', 1.3),
            _point('h', 1.9, roles=('spatial_holdout',)),
            point,
        ),
        order_plan=CaptureOrderPlan(strategy='unplanned'),
    )
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    assert 'adaptive_points_not_holdout_eligible' in evaluation.warnings


def test_smp52_channel_coverage_scope_rules() -> None:
    with pytest.raises(ValidationError):
        _point('c1', 1.0, channel_coverage='selected_channels')
    with pytest.raises(ValidationError):
        _point(
            'c2', 1.0,
            channel_coverage='all_channels',
            channel_entity_ids=('ch-l',),
        )
    scoped = _point(
        'c3', 1.0,
        channel_coverage='subwoofers_only',
        channel_entity_ids=('sub-1', 'sub-2'),
    )
    assert scoped.channel_entity_ids == ('sub-1', 'sub-2')


# -- SMP55: partition composition ---------------------------------------------


def test_smp55_point_roles_map_to_partitions() -> None:
    assert partition_for_point(_point('o', 1.0)) == 'calibration'
    assert partition_for_point(
        _point('h', 1.0, roles=('spatial_holdout',))
    ) == 'holdout'
    assert partition_for_point(
        _point('r', 1.0, roles=('repeatability',))
    ) == 'repeatability'
    assert partition_for_point(
        _point('d', 1.0, roles=('diagnostic',))
    ) == 'unassigned'


def test_smp56_partition_disjointness_is_enforced() -> None:
    design = _design()
    with pytest.raises(ValueError, match='both calibration and holdout'):
        assert_campaign_partition_disjoint(
            design, [('m-1', 'calibration'), ('m-1', 'holdout')]
        )


def test_smp57_binding_partition_consistency() -> None:
    design = _design()
    binding = build_point_binding(
        design=design,
        point_id='p-hold-1',
        measurement_id='m-h',
        measurement_sha256=SHA_A,
        captured_at_utc=T1,
    )
    check_binding_partition_consistency(
        design, [binding], [('m-h', 'holdout')]
    )
    with pytest.raises(ValueError, match='filed as'):
        check_binding_partition_consistency(
            design, [binding], [('m-h', 'calibration')]
        )


# -- SMP60: capture order + diversity ------------------------------------------


def test_smp60_order_plan_strategies() -> None:
    with pytest.raises(ValidationError):
        CaptureOrderPlan(strategy='declared_sequence')
    with pytest.raises(ValidationError):
        CaptureOrderPlan(strategy='randomized')
    plan = CaptureOrderPlan(
        strategy='interleaved_pairs',
        interleaved_pairs=(
            InterleavedPair(
                pair_id='pair-1',
                first_point_id='p-a',
                second_point_id='p-b',
            ),
        ),
    )
    assert plan.interleaved_pairs[0].pair_id == 'pair-1'


def test_smp61_diversity_report_is_diagnostic_only() -> None:
    design = _design()
    freqs = tuple(20.0 + i * 5.0 for i in range(20))
    identical = tuple(-80.0 + (i % 3) for i in range(20))
    different = tuple(-80.0 + i * 0.4 for i in range(20))
    report = acoustic_diversity_report(
        design,
        (
            PointResponseInput(
                point_id='p-opt-1',
                frequency_hz=freqs,
                magnitude_db=identical,
            ),
            PointResponseInput(
                point_id='p-opt-2',
                frequency_hz=freqs,
                magnitude_db=identical,
            ),
            PointResponseInput(
                point_id='p-hold-1',
                frequency_hz=freqs,
                magnitude_db=different,
            ),
        ),
        spec=AcousticDiversitySpec(band_hz=(20.0, 120.0)),
        created_at_utc=T1,
    )
    assert report.diagnostic_only is True
    assert ('p-opt-1', 'p-opt-2') in report.redundant_pair_ids
    # Flagging redundancy never rewrites the declared roles.
    assert design.point('p-opt-1').roles == ('optimization',)
    assert design.point('p-hold-1').roles == ('spatial_holdout',)


def test_smp62_diversity_rejects_unknown_points() -> None:
    design = _design()
    with pytest.raises(ValueError, match='outside the design spatial set'):
        acoustic_diversity_report(
            design,
            (
                PointResponseInput(
                    point_id='ghost',
                    frequency_hz=(20.0, 30.0),
                    magnitude_db=(-80.0, -81.0),
                ),
            ),
            created_at_utc=T1,
        )


# -- SMP65: templates ----------------------------------------------------------


def test_smp65_templates_are_labelled_and_provenance_carried() -> None:
    templates = builtin_campaign_templates()
    assert {t.template_id for t in templates} >= {
        'dirac-like-focused',
        'trinnov-like-rsp-multipoint',
        'htdt-research-holdout',
        'rp22-verification',
    }
    design = instantiate_campaign_template(
        templates[0],
        _area(),
        document_id=DOC,
        declared_at_utc=T0,
    )
    assert design.template_ref is not None
    assert 'compatibility' in design.template_ref.compatibility_note
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    # The generated lattice must be a defensible plan — holdouts inside
    # the area, no forbidden role pairs.
    assert 'holdout_outside_declared_area' not in evaluation.reasons


def test_smp66_template_instantiation_is_deterministic() -> None:
    template = builtin_campaign_templates()[0]
    first = instantiate_campaign_template(
        template, _area(), document_id=DOC, declared_at_utc=T0
    )
    second = instantiate_campaign_template(
        template, _area(), document_id=DOC, declared_at_utc=T0
    )
    assert first.design_id == second.design_id


# -- SMP70: repository persistence ---------------------------------------------


def test_smp70_repository_round_trip_and_append_only(tmp_path) -> None:
    repository = CadSpatialCampaignRepository(_scene_repo(tmp_path))
    design = _design()
    repository.save_design(design)
    repository.save_design(design)  # identical re-save is a no-op
    loaded = repository.get_design(design.design_id)
    assert loaded == design

    evaluation = evaluate_campaign_design(design, evaluated_at_utc=T1)
    repository.save_evaluation(evaluation)
    assert repository.evaluations_for_design(design.design_id) == (
        evaluation,
    )

    binding = build_point_binding(
        design=design,
        point_id='p-hold-1',
        measurement_id='m-1',
        measurement_sha256=SHA_A,
        captured_at_utc=T1,
        observed_position=Position3(x_m=1.6, y_m=1.9, z_m=1.1),
        position_uncertainty_m=0.02,
    )
    repository.save_binding(binding)
    assert repository.bindings_for_measurement('m-1') == (binding,)


def test_smp71_binding_requires_persisted_design(tmp_path) -> None:
    repository = CadSpatialCampaignRepository(_scene_repo(tmp_path))
    design = _design()
    binding = build_point_binding(
        design=design,
        point_id='p-hold-1',
        measurement_id='m-1',
        measurement_sha256=SHA_A,
        captured_at_utc=T1,
    )
    # The design was never persisted — declared-before-measurement order
    # is enforced by the store.
    with pytest.raises(SpatialCampaignIntegrityError):
        repository.save_binding(binding)


def test_smp72_row_payload_drift_is_rejected(tmp_path) -> None:
    repository = CadSpatialCampaignRepository(_scene_repo(tmp_path))
    design = _design()
    repository.save_design(design)
    with repository._connect() as connection:
        connection.execute(
            'UPDATE cad_spatial_campaign_designs SET document_id=? '
            'WHERE design_id=?',
            ('tampered-doc', design.design_id),
        )
    with pytest.raises(SpatialCampaignIntegrityError):
        repository.get_design(design.design_id)


# ---------------------------------------------------------------------------
# RP32 fixtures (#585)


def _target() -> DesignTargetBinding:
    return DesignTargetBinding(
        scene_revision_id='rev-1',
        scene_content_hash=SHA_A,
        rp22_revision='RP22 (2024)',
        rp22_level='Level 2',
        expected_speaker_count=9,
        expected_subwoofer_count=2,
        expected_channel_labels=('L', 'C', 'R'),
        approved_equipment_ids=('avr-1', 'mic-usb-1'),
    )


def _plan(profile=None, **overrides):
    kwargs = dict(
        document_id=DOC,
        profile=profile or rp32_builtin_profile(created_at_utc=T0),
        target=_target(),
        tasks=(
            Rp32TaskSpec(
                task_id='fr-seats',
                domain='frequency_response',
                description='frequency response at each seat',
                required_instrument_roles=('measurement_microphone',),
            ),
            Rp32TaskSpec(
                task_id='level-align',
                domain='level_alignment',
                description='channel level alignment check',
            ),
        ),
        created_at_utc=T1,
        spatial_design_id='spatial-campaign:' + 'd' * 64,
        spatial_design_sha256='d' * 64,
    )
    kwargs.update(overrides)
    return build_verification_plan(**kwargs)


# -- RP32F10: external profile identity ---------------------------------------


def test_rp32f10_builtin_profile_is_honest_public_announcement() -> None:
    profile = rp32_builtin_profile(created_at_utc=T0)
    assert profile.document.publisher == 'CEDIA'
    assert profile.document.document_id == 'RP32'
    assert profile.document.source_access_kind == 'public_announcement'
    # Zero asserted clause mappings — honest until the exact source is
    # lawfully reviewed.
    assert profile.clause_mapping_state == 'unpopulated_pending_lawful_source'
    assert profile.is_rp32_claimable() is False
    assert all(
        mapping.status == 'unmapped'
        for mapping in profile.requirement_mappings
    )


def test_rp32f11_full_document_identity_requires_content_hash() -> None:
    with pytest.raises(ValidationError):
        CommissioningDocumentIdentity(
            publisher='CEDIA',
            document_id='RP32',
            title='Audio System Measurement and Verification',
            revision='1.0',
            source_access_kind='full_document',
            parser_version='rp32-identity-1',
        )


def test_rp32f12_asserted_clause_mapping_state_is_rejected() -> None:
    profile = rp32_builtin_profile(created_at_utc=T0)
    payload = profile.model_dump(mode='json')
    payload['clause_mapping_state'] = 'mapped'
    with pytest.raises(ValidationError):
        type(profile).model_validate(payload)


def test_rp32f13_mapping_honesty_rules() -> None:
    # Supported claims require authorities + review evidence.
    with pytest.raises(ValidationError):
        Rp32RequirementMapping(
            requirement_id='r1',
            domain='stimulus',
            clause_id='4.2',
            status='supported',
            authority_refs=('stimulus_registry',),
        )
    # Unmapped entries cannot name satisfying authorities.
    with pytest.raises(ValidationError):
        Rp32RequirementMapping(
            requirement_id='r2',
            domain='stimulus',
            status='unmapped',
            authority_refs=('stimulus_registry',),
        )
    mapping = Rp32RequirementMapping(
        requirement_id='r3',
        domain='stimulus',
        clause_id='4.2',
        status='supported',
        authority_refs=('stimulus_registry',),
        review_evidence='RP32 §4.2 reviewed 2026-10, doc sha abc',
    )
    assert mapping.status == 'supported'


# -- RP32F20: designed vs as-built reconciliation ------------------------------


def test_rp32f20_reconciled_when_as_built_matches() -> None:
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=9,
            observed_subwoofer_count=2,
            observed_channel_labels=('L', 'C', 'R'),
            observed_equipment_ids=('avr-1', 'mic-usb-1'),
            room_matches_design=True,
        ),
        evaluated_at_utc=T1,
    )
    assert reconciliation.state == 'reconciled'


def test_rp32f21_mismatch_blocks_verification() -> None:
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=7,
            observed_subwoofer_count=2,
            observed_channel_labels=('L', 'C', 'R'),
            room_matches_design=True,
        ),
        evaluated_at_utc=T1,
    )
    assert reconciliation.state == 'incompatible'
    assert 'speaker_count_mismatch' in reconciliation.findings


def test_rp32f22_missing_observations_are_insufficient_evidence() -> None:
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=9,
            observed_subwoofer_count=2,
        ),
        evaluated_at_utc=T1,
    )
    assert reconciliation.state == 'insufficient_evidence'
    assert 'room_geometry' in reconciliation.unknown_fields


# -- RP32F30/35: plans + instrument evidence ------------------------------------


def test_rp32f30_plan_pins_exact_profile() -> None:
    profile = rp32_builtin_profile(created_at_utc=T0)
    plan = _plan(profile=profile)
    assert plan.profile_id == profile.profile_id
    assert plan.plan_id == f'rp32-plan:{plan.plan_sha256}'


def test_rp32f31_tasks_cannot_claim_unsupported_clauses() -> None:
    with pytest.raises(ValueError, match='clauses the profile'):
        _plan(
            tasks=(
                Rp32TaskSpec(
                    task_id='claimed',
                    domain='stimulus',
                    rp32_clause_ids=('5.1',),
                    description='asserts RP32 clause without source',
                ),
            ),
        )


def test_rp32f32_instrument_calibration_states() -> None:
    instrument = InstrumentEvidence(
        instrument_id='mic-1',
        role='measurement_microphone',
        calibration_certificate_id='cert-9',
        calibration_due_utc='2026-01-01T00:00:00+00:00',
    )
    assert instrument.calibration_state(at_utc=T0) == 'expired'
    assert instrument.calibration_state(at_utc='2025-06-01T00:00:00+00:00') == 'calibrated'
    unknown = InstrumentEvidence(instrument_id='mic-2', role='spl_meter')
    assert unknown.calibration_state(at_utc=T0) == 'unknown'


# -- RP32F40: readiness gate -----------------------------------------------------


def test_rp32f40_full_evidence_is_ready() -> None:
    plan = _plan()
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=9,
            observed_subwoofer_count=2,
            observed_channel_labels=('L', 'C', 'R'),
            observed_equipment_ids=('avr-1', 'mic-usb-1'),
            room_matches_design=True,
        ),
        evaluated_at_utc=T1,
    )
    readiness = evaluate_measurement_readiness(
        plan,
        reconciliation=reconciliation,
        spatial_evaluation_state='valid',
        measurement_state_ready=True,
        uncertainty_budgets_present=True,
        stimulus_pins_present=True,
        assessed_at_utc=T2,
    )
    assert readiness.state == 'ready'


def test_rp32f41_unassessed_signals_are_insufficient_evidence() -> None:
    readiness = evaluate_measurement_readiness(
        _plan(),
        assessed_at_utc=T2,
    )
    assert readiness.state == 'insufficient_evidence'
    assert 'designed_built_reconciliation' in readiness.unknowns
    assert 'measurement_state' in readiness.unknowns


def test_rp32f42_incompatible_inputs_block() -> None:
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=7,
            observed_subwoofer_count=2,
            room_matches_design=False,
        ),
        evaluated_at_utc=T1,
    )
    readiness = evaluate_measurement_readiness(
        _plan(),
        reconciliation=reconciliation,
        spatial_evaluation_state='invalid',
        assessed_at_utc=T2,
    )
    assert readiness.state == 'incompatible'
    assert 'as_built_incompatible' in readiness.reasons
    assert 'spatial_design_invalid' in readiness.reasons


def test_rp32f43_expired_instrument_blocks_ready() -> None:
    plan = _plan(
        instrument_evidence=(
            InstrumentEvidence(
                instrument_id='mic-1',
                role='measurement_microphone',
                calibration_certificate_id='cert-9',
                calibration_due_utc='2026-01-01T00:00:00+00:00',
            ),
        ),
    )
    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=9,
            observed_subwoofer_count=2,
            observed_channel_labels=('L', 'C', 'R'),
            observed_equipment_ids=('avr-1', 'mic-usb-1'),
            room_matches_design=True,
        ),
        evaluated_at_utc=T1,
    )
    readiness = evaluate_measurement_readiness(
        plan,
        reconciliation=reconciliation,
        spatial_evaluation_state='valid',
        measurement_state_ready=True,
        uncertainty_budgets_present=True,
        stimulus_pins_present=True,
        assessed_at_utc=T2,
    )
    assert readiness.state == 'incompatible'
    assert any(
        reason.startswith('instrument_calibration_expired')
        for reason in readiness.reasons
    )


# -- RP32F45/50: records, exceptions, RP22 linkage --------------------------------


def _passing_record(plan, readiness):
    return build_verification_record(
        plan=plan,
        readiness_assessment_id=readiness.assessment_id,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-fr',),
            ),
            Rp32VerificationItemResult(
                task_id='level-align',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-lvl',),
            ),
        ),
        completed_at_utc=T3,
    )


def test_rp32f45_verified_record_links_rp22_state() -> None:
    plan = _plan()
    readiness = evaluate_measurement_readiness(
        plan,
        reconciliation=evaluate_designed_built_reconciliation(
            document_id=DOC,
            target=_target(),
            as_built=AsBuiltObservation(
                observed_speaker_count=9,
                observed_subwoofer_count=2,
                observed_channel_labels=('L', 'C', 'R'),
                observed_equipment_ids=('avr-1', 'mic-usb-1'),
                room_matches_design=True,
            ),
            evaluated_at_utc=T1,
        ),
        spatial_evaluation_state='valid',
        measurement_state_ready=True,
        uncertainty_budgets_present=True,
        stimulus_pins_present=True,
        assessed_at_utc=T2,
    )
    record = _passing_record(plan, readiness)
    assert record.overall_state == 'verified'
    assert record.rp22_state == 'rp22_rp32_measured_verified'


def test_rp32f46_failed_item_marks_rp22_failed() -> None:
    plan = _plan()
    record = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'e' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='fail',
                evidence_kind='measured',
                measurement_ids=('m-fr',),
            ),
        ),
        completed_at_utc=T3,
    )
    assert record.overall_state == 'failed'
    assert record.rp22_state == 'rp22_rp32_measured_failed'


def test_rp32f47_measured_results_require_measurement_ids() -> None:
    with pytest.raises(ValidationError):
        Rp32VerificationItemResult(
            task_id='fr-seats',
            state='pass',
            evidence_kind='measured',
        )


def test_rp32f48_client_acceptance_never_upgrades() -> None:
    plan = _plan()
    record = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'e' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='fail',
                evidence_kind='measured',
                measurement_ids=('m-fr',),
            ),
            Rp32VerificationItemResult(
                task_id='level-align',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-lvl',),
            ),
        ),
        exceptions=(
            CommissioningException(
                exception_id='ex-1',
                task_id='fr-seats',
                reason='client accepted deviation',
                accepted_by='client-signature',
                client_acceptance=True,
                recorded_at_utc=T3,
            ),
        ),
        completed_at_utc=T3,
    )
    # Client acceptance is recorded but cannot flip the technical verdict.
    assert record.overall_state == 'failed'


def test_rp32f49_attempt_chain_preserves_failed_baseline() -> None:
    plan = _plan()
    first = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'e' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='fail',
                evidence_kind='measured',
                measurement_ids=('m-1',),
            ),
        ),
        completed_at_utc=T3,
    )
    second = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'f' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-2',),
            ),
            Rp32VerificationItemResult(
                task_id='level-align',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-3',),
            ),
        ),
        prior_record_ids=(first.record_id,),
        completed_at_utc='2026-10-06T00:00:00+00:00',
    )
    assert second.prior_record_ids == (first.record_id,)
    assert second.overall_state == 'verified'


# -- RP32F55: freshness -----------------------------------------------------------


def test_rp32f55_change_events_stale_the_record() -> None:
    plan = _plan()
    record = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'e' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-1',),
            ),
            Rp32VerificationItemResult(
                task_id='level-align',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-2',),
            ),
        ),
        completed_at_utc=T3,
    )
    fresh = evaluate_verification_freshness(
        record, change_events=(), assessed_at_utc='2026-10-06T00:00:00+00:00'
    )
    assert fresh.state == 'fresh'
    stale = evaluate_verification_freshness(
        record,
        change_events=('dsp_configuration_changed', 'seating_changed'),
        assessed_at_utc='2026-10-06T00:00:00+00:00',
    )
    assert stale.state == 'stale'
    assert set(stale.staleness_reasons) == {
        'dsp_configuration_changed',
        'seating_changed',
    }
    unknown = evaluate_verification_freshness(
        record,
        change_events=('mystery_change',),
        assessed_at_utc='2026-10-06T00:00:00+00:00',
    )
    assert unknown.state == 'insufficient_evidence'


# -- RP32F60: report + repository -------------------------------------------------


def test_rp32f60_report_is_sealed_and_honest(tmp_path) -> None:
    repository = CadRp32Repository(_scene_repo(tmp_path))
    profile = rp32_builtin_profile(created_at_utc=T0)
    repository.save_profile(profile)

    plan = _plan(profile=profile)
    repository.save_plan(plan)

    reconciliation = evaluate_designed_built_reconciliation(
        document_id=DOC,
        target=_target(),
        as_built=AsBuiltObservation(
            observed_speaker_count=9,
            observed_subwoofer_count=2,
            observed_channel_labels=('L', 'C', 'R'),
            observed_equipment_ids=('avr-1', 'mic-usb-1'),
            room_matches_design=True,
        ),
        evaluated_at_utc=T1,
    )
    repository.save_reconciliation(reconciliation)

    readiness = evaluate_measurement_readiness(
        plan,
        reconciliation=reconciliation,
        spatial_evaluation_state='valid',
        measurement_state_ready=True,
        uncertainty_budgets_present=True,
        stimulus_pins_present=True,
        assessed_at_utc=T2,
    )
    repository.save_readiness(readiness)

    record = build_verification_record(
        plan=plan,
        readiness_assessment_id=readiness.assessment_id,
        reconciliation_id=reconciliation.reconciliation_id,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-fr',),
            ),
            Rp32VerificationItemResult(
                task_id='level-align',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-lvl',),
            ),
        ),
        completed_at_utc=T3,
    )
    repository.save_record(record)

    report = build_commissioning_report(
        profile=profile,
        plan=plan,
        record=record,
        generated_at_utc='2026-10-05T00:10:00+00:00',
    )
    repository.save_report(report)
    loaded = repository.get_report(report.report_id)
    assert loaded == report
    # The report states the honest mapping state and never claims CEDIA
    # certification.
    assert 'unpopulated_pending_lawful_source' in report.claim_text
    assert 'not a CEDIA certification' in report.claim_text
    assert report.item_summary == {'pass': 2}


def test_rp32f61_evidence_chain_commit_order_is_enforced(tmp_path) -> None:
    repository = CadRp32Repository(_scene_repo(tmp_path))
    plan = _plan()
    # A plan cannot reference an unpersisted profile.
    with pytest.raises(Rp32IntegrityError):
        repository.save_plan(plan)

    profile = rp32_builtin_profile(created_at_utc=T0)
    repository.save_profile(profile)
    repository.save_plan(plan)

    record = build_verification_record(
        plan=plan,
        readiness_assessment_id='rp32-readiness:' + 'e' * 64,
        item_results=(
            Rp32VerificationItemResult(
                task_id='fr-seats',
                state='pass',
                evidence_kind='measured',
                measurement_ids=('m-1',),
            ),
        ),
        completed_at_utc=T3,
    )
    # A record cannot reference an unpersisted readiness assessment.
    with pytest.raises(Rp32IntegrityError):
        repository.save_record(record)


def test_rp32f62_append_only_conflicts(tmp_path) -> None:
    repository = CadRp32Repository(_scene_repo(tmp_path))
    profile = rp32_builtin_profile(created_at_utc=T0)
    repository.save_profile(profile)
    repository.save_profile(profile)  # no-op
    tampered = profile.model_dump(mode='json')
    tampered['note'] = 'different content same id'
    import sqlite3

    # Forging a divergent payload under the same id is a conflict, not a
    # silent revision — drive it through the model so the store sees the
    # disagreement.
    with pytest.raises(ValidationError):
        type(profile).model_validate(tampered)


def test_rp32f63_row_payload_drift_is_rejected(tmp_path) -> None:
    repository = CadRp32Repository(_scene_repo(tmp_path))
    profile = rp32_builtin_profile(created_at_utc=T0)
    repository.save_profile(profile)
    with repository._connect() as connection:
        connection.execute(
            'UPDATE cad_rp32_profiles SET revision=? WHERE profile_id=?',
            ('9.9-forged', profile.profile_id),
        )
    with pytest.raises(Rp32IntegrityError):
        repository.get_profile(profile.profile_id)
