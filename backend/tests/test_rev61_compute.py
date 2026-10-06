"""REV61-COMPUTE regression tests — solver/prediction/optimization
numerics slice.

Fixture map:

- CMP61 (#581, cad_spatial_campaign): coverage metrics and design
  evaluation must run on designs with zero spatial-coverage points
  (repeatability/diagnostic-only) instead of crashing on the density
  share; a lone spatial point has no nearest neighbour — no fabricated
  0 m spacing must surface a spacing warning; template outside-area
  points must land outside the declared area on BOTH sides of the
  aggregate bounds.
- AAI61 (#661, cad_adaptive_identification): a partially-converged
  adaptive estimate is not qualified evidence, and a declared
  band-insufficient stimulus caps the claim at band_limited_estimate.
- BMQ61 (#574, cad_bass_management_qualification): splice margins and
  the predicted-vs-measured check are restricted to the frequency range
  BOTH isolated paths actually measured — ``np.interp`` flat-clamps
  outside the measured domain, which would fabricate evidence.
- ISO61 (#576, cad_isolation_authority): a ``relative_reduction_target``
  criterion compares the measured level difference (not an SPL), a
  direct ``receiving_spl`` band feeds the receiving-SPL estimate, and
  ``level_difference_db`` never carries a non-difference metric value.
- RT61 (#588, cad_response_target): ``band_average`` normalization
  averages the response and the target over the SAME sample pairs.
"""

from __future__ import annotations

import pytest

from htdt.cad_adaptive_identification import (
    AdaptiveIdentificationProfile,
    AdaptiveTransferEstimate,
    ArbitraryStimulusMeasurement,
    evaluate_adaptive_claim,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_bass_management_qualification import (
    ResponseCurve,
    evaluate_splice,
)
from htdt.cad_calibration import CadTargetNormalizationCondition
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_isolation_authority import (
    IsolationBandEvidence,
    IsolationElementEvidence,
    IsolationNormalizationEvidence,
    IsolationSourceStressProfile,
    IsolationTransmissionPath,
    ReceivingRoomCriterion,
    build_field_measurement,
    build_interroom_scenario,
    build_isolation_element,
    evaluate_isolation_qualification,
)
from htdt.cad_response_target import _normalization_offset
from htdt.cad_scene import Position3
from htdt.cad_spatial_campaign import (
    CampaignPoint,
    CaptureOrderPlan,
    ListeningAreaSpec,
    ListeningZone,
    SpatialCampaignTemplate,
    build_spatial_campaign_design,
    compute_coverage_metrics,
    evaluate_campaign_design,
    instantiate_campaign_template,
)


_DOC = 'doc-rev61-compute'
_TS = '2026-10-05T00:00:00+00:00'
_TS1 = '2026-10-05T00:00:01+00:00'


# ---------------------------------------------------------------------------
# CMP61 — spatial campaign coverage metrics (#581)
# ---------------------------------------------------------------------------


def _area() -> ListeningAreaSpec:
    return ListeningAreaSpec(
        area_label='main row',
        zones=(
            ListeningZone(
                zone_id='main-row',
                kind='row',
                declared_weight=1.0,
                bounds_min=Position3(x_m=1.0, y_m=1.0, z_m=0.9),
                bounds_max=Position3(x_m=3.0, y_m=3.0, z_m=1.4),
            ),
        ),
    )


def _design(points, **overrides):
    kwargs = dict(
        document_id=_DOC,
        listening_area=_area(),
        points=points,
        declared_at_utc=_TS,
        claim_kinds=(),
        order_plan=CaptureOrderPlan(
            strategy='declared_sequence',
            sequence=tuple(point.point_id for point in points),
        ),
    )
    kwargs.update(overrides)
    return build_spatial_campaign_design(**kwargs)


def test_cmp61_coverage_metrics_without_spatial_points() -> None:
    """A repeatability/diagnostic-only design has zero spatial points:
    the density share must not divide by zero, and the evaluator's
    intended fail-closed verdict must be reachable."""
    design = _design(
        points=(
            CampaignPoint(
                point_id='p-rep',
                position=Position3(x_m=2.0, y_m=2.0, z_m=1.1),
                roles=('repeatability',),
                weight=0.0,
            ),
            CampaignPoint(
                point_id='p-diag',
                position=Position3(x_m=2.2, y_m=2.0, z_m=1.1),
                roles=('diagnostic',),
                weight=0.0,
            ),
        ),
    )
    metrics = compute_coverage_metrics(design)
    assert metrics.spatial_point_count == 0
    evaluation = evaluate_campaign_design(design, evaluated_at_utc=_TS1)
    assert evaluation.state == 'invalid'
    assert 'no_spatial_coverage_points' in evaluation.reasons


def test_cmp61_single_point_has_no_spacing_artifact() -> None:
    """A lone spatial point has no neighbour pair — spacing statistics
    stay absent rather than a fabricated 0 m spacing tripping the
    declared-spacing warning."""
    design = _design(
        points=(
            CampaignPoint(
                point_id='p-opt',
                position=Position3(x_m=2.0, y_m=2.0, z_m=1.1),
                roles=('optimization',),
            ),
        ),
    )
    metrics = compute_coverage_metrics(design)
    assert metrics.min_spacing_m is None
    assert metrics.max_spacing_m is None
    assert metrics.median_spacing_m is None
    assert metrics.nearest_neighbor_m == {}
    evaluation = evaluate_campaign_design(
        design,
        evaluated_at_utc=_TS1,
    )
    assert evaluation.state == 'valid'
    assert 'points_below_declared_spacing' not in evaluation.warnings


def test_cmp61_template_outside_points_land_outside() -> None:
    """Outside-area probe points must clear the zone on the +Y side too —
    previously a zone with y-extent >= spacing swallowed them."""
    template = SpatialCampaignTemplate(
        template_id='probe-outside',
        template_version='1.0',
        name='probe',
        source_label='rev61 probe',
        compatibility_note='probe',
        optimization_count=2,
        holdout_count=0,
        repeatability_count=0,
        include_reference=True,
        outside_area_count=2,
        spacing_m=0.45,
    )
    design = instantiate_campaign_template(
        template,
        _area(),
        document_id=_DOC,
        declared_at_utc=_TS,
    )
    metrics = compute_coverage_metrics(design)
    out_ids = set(metrics.outside_area_point_ids)
    assert 'p-out-00' in out_ids
    assert 'p-out-01' in out_ids
    out_points = {
        point.point_id: point
        for point in design.points
        if point.point_id.startswith('p-out-')
    }
    assert len(out_points) == 2
    for point in out_points.values():
        assert _area().zone_for(point.position) is None


# ---------------------------------------------------------------------------
# AAI61 — adaptive identification claim gates (#661)
# ---------------------------------------------------------------------------


def _ref(rid: str) -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _aam(**kw) -> AdaptiveIdentificationProfile:
    payload = dict(
        document_id='doc-1',
        method='subband_adaptive_identifier',
        method_reference='fsaf-reference-impl-x',
        license_review_status='reviewed_permitted',
        model_length_samples=8192,
    )
    payload.update(kw)
    return AdaptiveIdentificationProfile.create(payload)


def _asm(**kw) -> ArbitraryStimulusMeasurement:
    payload = dict(
        document_id='doc-1',
        stimulus_kind='file_segment',
        defect_state='stimulus_validated',
        asset_sha256='b' * 64,
        segment_start_s=0.0,
        segment_end_s=30.0,
        crest_factor_db=12.0,
    )
    payload.update(kw)
    return ArbitraryStimulusMeasurement.create(payload)


def _ate(**kw) -> AdaptiveTransferEstimate:
    payload = dict(
        document_id='doc-1',
        profile_ref=_ref('aam-1'),
        stimulus_ref=_ref('asm-1'),
        clock_qualification='common_clock',
        convergence_state='converged',
        excitation_support=(
            dict(band_hz=(80.0, 8000.0), state='identified'),
        ),
    )
    payload.update(kw)
    return AdaptiveTransferEstimate.create(payload)


def test_aai61_partially_converged_is_not_qualified() -> None:
    """An adaptive estimate that only partially converged is not
    converged evidence — it must not reach qualified_adaptive_estimate."""
    profile, stimulus = _aam(), _asm()
    estimate = _ate(convergence_state='partially_converged')
    state, _note = evaluate_adaptive_claim(profile, stimulus, estimate)
    assert state == 'not_converged'


def test_aai61_band_insufficient_stimulus_caps_the_claim() -> None:
    """A stimulus declared band-insufficient cannot yield a qualified
    estimate even when the estimate claims full identification."""
    profile, stimulus = _aam(), _asm(
        defect_state='stimulus_band_insufficient'
    )
    estimate = _ate()
    state, _note = evaluate_adaptive_claim(profile, stimulus, estimate)
    assert state == 'band_limited_estimate'


# ---------------------------------------------------------------------------
# BMQ61 — splice evaluation measured-domain restriction (#574)
# ---------------------------------------------------------------------------


def _curve(
    freqs: tuple[float, ...],
    level_db: float,
    *,
    levels: tuple[float, ...] | None = None,
    phase_deg: float = 0.0,
) -> ResponseCurve:
    mags = levels if levels is not None else tuple(
        level_db for _freq in freqs
    )
    return ResponseCurve(
        frequencies_hz=freqs,
        magnitude_db=mags,
        phase_deg=tuple(phase_deg for _freq in freqs),
    )


def test_bmq61_splice_ignores_samples_outside_shared_domain() -> None:
    """Summed samples beyond an isolated path's measured range must not
    contribute margins — the interpolator clamps flat at the edge and
    would report a fabricated cancellation."""
    # Main measured 40-80 Hz only; sub measured 70-160 Hz.
    main = _curve((40.0, 56.0, 70.0, 80.0), -12.0)
    sub = _curve((70.0, 80.0, 90.0, 113.0, 160.0), -12.0)
    # The summed curve has a deep dip at 90/113 Hz — but the main path
    # never measured there, so those samples are not splice evidence.
    summed = _curve(
        (40.0, 56.0, 70.0, 80.0, 90.0, 113.0, 160.0),
        -6.0,
        levels=(-6.0, -6.0, -6.0, -6.0, -30.0, -30.0, -6.0),
    )
    verdict = evaluate_splice(
        main_curve=main,
        sub_curve=sub,
        summed_curve=summed,
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'constructive'
    assert 'phase_cancellation_at_splice' not in verdict.failure_reasons
    assert verdict.metrics.worst_splice_margin_db == pytest.approx(6.0)
    # The fabricated margin frequency must not be reported.
    assert verdict.metrics.worst_margin_frequency_hz != pytest.approx(90.0)


def test_bmq61_splice_without_shared_domain_is_unknown() -> None:
    """No summed sample inside the shared measured domain → unknown, not
    a verdict on clamped edge values."""
    main = _curve((40.0, 56.0, 70.0), -12.0)
    sub = _curve((90.0, 113.0, 160.0), -12.0)
    summed = _curve(
        (40.0, 56.0, 70.0, 80.0, 90.0, 113.0, 160.0), -6.0
    )
    verdict = evaluate_splice(
        main_curve=main,
        sub_curve=sub,
        summed_curve=summed,
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'unknown'


# ---------------------------------------------------------------------------
# ISO61 — inter-room isolation criterion quantities (#576)
# ---------------------------------------------------------------------------


def _iso_element():
    return build_isolation_element(
        document_id=_DOC,
        label='shared wall',
        construction_class='double_leaf_decoupled',
        evidence=(
            IsolationElementEvidence(
                evidence_kind='lab_tl_spectrum',
                standard_ref='ISO 10140-2',
                edition_or_revision='2021',
                tl_values_db={'125': 41.0, '250': 52.0, '500': 61.0},
                frequency_domain=FrequencyDomain(
                    minimum_hz=100.0, maximum_hz=3150.0
                ),
            ),
        ),
    )


def _iso_scenario(criteria=()):
    element = _iso_element()
    return build_interroom_scenario(
        document_id=_DOC,
        label='theater -> bedroom',
        source_region_id='theater',
        receiving_region_id='bedroom',
        source_profile=IsolationSourceStressProfile(
            content_class='movie_lfe_stress',
            lfe_active=True,
            band_levels_db={'125': 95.0, '250': 88.0},
            subwoofer_output_level_db=112.0,
            bass_management_state='on',
        ),
        paths=(
            IsolationTransmissionPath(
                path_id='partition',
                kind='direct_partition',
                element_id=element.element_id,
                element_sha256=element.element_sha256,
                area_m2=12.0,
            ),
        ),
        criteria=criteria,
        construction_state='design_prediction',
        created_at_utc=_TS,
    )


def _iso_measurement(scenario, bands):
    return build_field_measurement(
        document_id=_DOC,
        scenario=scenario,
        method_profile='iso_16283_1',
        method_standard_ref='ISO 16283-1:2014',
        method_edition='2014',
        source_position_ids=('src-1', 'src-2'),
        receiver_position_ids=('rcv-1', 'rcv-2', 'rcv-3'),
        bands=bands,
        normalization=IsolationNormalizationEvidence(
            normalization='reverberation_time',
            reference='reference_rt_0p5s',
            receiving_rt_s={'125': 0.45, '250': 0.4},
        ),
        source_room_volume_m3=35.0,
        receiving_room_volume_m3=18.0,
        partition_area_m2=12.0,
        opening_state='closed',
        measured_at_utc='2026-10-05T01:00:00+00:00',
    )


def test_iso61_relative_reduction_target_compares_level_difference() -> None:
    """A reduction target caps the measured level difference — not the
    estimated receiving SPL (units mismatch otherwise)."""
    criterion = ReceivingRoomCriterion(
        criterion_id='reduction-45',
        kind='relative_reduction_target',
        frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=300.0
        ),
        limit_db=45.0,
    )
    scenario = _iso_scenario(criteria=(criterion,))
    measurement = _iso_measurement(
        scenario,
        bands=(
            IsolationBandEvidence(
                band_hz=125.0,
                metric='standardized_level_difference_dnt',
                value_db=52.0,
                source_level_db=95.0,
                receiving_level_db=43.0,
                background_level_db=20.0,
            ),
            IsolationBandEvidence(
                band_hz=250.0,
                metric='standardized_level_difference_dnt',
                value_db=42.0,
                source_level_db=88.0,
                receiving_level_db=46.0,
                background_level_db=18.0,
            ),
        ),
    )
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(measurement,), evaluated_at_utc=_TS
    )
    result = qualification.criterion_results[0]
    # Worst difference 42 dB vs a 45 dB target → fail by -3 dB. An SPL
    # margin (45 - 46) could not express this at all.
    assert result.verdict == 'fail'
    assert result.worst_margin_db == pytest.approx(-3.0)
    assert result.evaluated_bands_hz == (125.0, 250.0)


def test_iso61_relative_reduction_target_passes() -> None:
    criterion = ReceivingRoomCriterion(
        criterion_id='reduction-40',
        kind='relative_reduction_target',
        frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=300.0
        ),
        limit_db=40.0,
    )
    scenario = _iso_scenario(criteria=(criterion,))
    measurement = _iso_measurement(
        scenario,
        bands=(
            IsolationBandEvidence(
                band_hz=125.0,
                metric='standardized_level_difference_dnt',
                value_db=52.0,
                source_level_db=95.0,
                receiving_level_db=43.0,
                background_level_db=20.0,
            ),
            IsolationBandEvidence(
                band_hz=250.0,
                metric='standardized_level_difference_dnt',
                value_db=42.0,
                source_level_db=88.0,
                receiving_level_db=46.0,
                background_level_db=18.0,
            ),
        ),
    )
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(measurement,), evaluated_at_utc=_TS
    )
    result = qualification.criterion_results[0]
    assert result.verdict == 'pass'
    assert result.worst_margin_db == pytest.approx(2.0)


def test_iso61_receiving_spl_metric_feeds_spl_estimate() -> None:
    """A directly measured receiving SPL is the strongest evidence for an
    SPL criterion — it is not derivable evidence, it IS the quantity."""
    criterion = ReceivingRoomCriterion(
        criterion_id='bedroom-limit',
        kind='max_receiving_spl_band',
        frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=300.0
        ),
        limit_db=40.0,
    )
    scenario = _iso_scenario(criteria=(criterion,))
    measurement = _iso_measurement(
        scenario,
        bands=(
            IsolationBandEvidence(
                band_hz=125.0,
                metric='receiving_spl',
                value_db=38.0,
                source_level_db=None,
                receiving_level_db=38.0,
                background_level_db=15.0,
            ),
        ),
    )
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(measurement,), evaluated_at_utc=_TS
    )
    verdicts = {v.band_hz: v for v in qualification.band_verdicts}
    assert verdicts[125.0].estimated_receiving_spl_db == pytest.approx(
        38.0
    )
    # The SPL value is an absolute level — it must not surface under the
    # level-difference field name.
    assert verdicts[125.0].level_difference_db is None
    result = qualification.criterion_results[0]
    assert result.verdict == 'pass'
    assert result.worst_margin_db == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# RT61 — response-target band_average normalization (#588)
# ---------------------------------------------------------------------------


def test_rt61_band_average_pairs_sample_sets() -> None:
    """The response mean and target mean must run over the same
    frequencies — a response point the target does not cover is not part
    of the offset."""
    normalization = CadTargetNormalizationCondition(
        method='band_average',
        reference_band_hz=(50.0, 300.0),
    )
    # Target covers only 100-200 Hz.
    target_points = ((100.0, -20.0), (200.0, -20.0))
    response_points = (
        (50.0, -40.0),
        (100.0, -60.0),
        (150.0, -60.0),
        (200.0, -60.0),
        (250.0, -40.0),
    )
    offset = _normalization_offset(
        response_points,
        target_points,
        normalization,
        'linear_db_hz',
    )
    # Covered response mean = -60 dB over (100,150,200); target -20 dB.
    assert offset == pytest.approx(40.0)
