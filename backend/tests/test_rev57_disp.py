"""REV57-DISP regression suite — #625/#626/#633.

#625 direct-view display authority: a single 0% APL window never
supports an across-content peak-luminance claim; transient peaks are
never promoted to sustained-full-field; contrast kinds are never
conflated; instrument-floor blacks are bounded, never infinite;
observed temporal dimming attaches to sustained claims; seat-dependent
claims need per-seat off-axis evidence.

#626 observer-metamerism authority: instrument-matched chromaticity
never implies a perceptual match for every observer; x/y alone is
INSUFFICIENT_SPECTRAL_EVIDENCE; IEC TS 61966-13 / ANSI CTA-6035
profiles never extend to reflected projection; a user white offset
never redefines the nominal target.

#633 viewing-environment authority: display output state and viewing
environment are separate authorities; surround luminance is
first-class; incident illuminance is never general room lux; CCT alone
never claims a D65 spectral surround; broadcast profiles are method
profiles, not residential defect criteria.
"""

from __future__ import annotations

import pytest

from htdt.cad_direct_view_display import (
    DisplayClaim,
    SpatialPoint,
    build_angle_measurement,
    build_display_state,
    build_photometric_measurement,
    build_spatial_measurement,
    build_stimulus_context,
    build_temporal_observation,
    evaluate_direct_view_qualification,
)
from htdt.cad_direct_view_display_repository import (
    CadDirectViewDisplayRepository,
    DirectViewDisplayConflictError,
    DirectViewDisplayIntegrityError,
)
from htdt.cad_observer_metamerism import (
    ChromaticityPoint,
    SpdBlock,
    SpdSample,
    build_metamerism_evaluation,
    build_observer_profile,
    build_perceptual_match,
    build_spectral_state,
    evaluate_observer_metamerism,
)
from htdt.cad_observer_metamerism_repository import (
    CadObserverMetamerismRepository,
)
from htdt.cad_viewing_environment import (
    IncidentLightState,
    SurroundState,
    build_environment_observation,
    build_lighting_scene,
    build_viewing_geometry,
    compare_viewing_environments,
    evaluate_viewing_environment,
)
from htdt.cad_viewing_environment_repository import (
    CadViewingEnvironmentRepository,
    ViewingEnvironmentIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

DOC = 'doc-rev57-disp'
_TS = '2026-10-05T12:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# #625 fixtures
# ---------------------------------------------------------------------------


def _clean_display(**over):
    """A fully declared OLED state — no unknown controls."""
    fields = dict(
        manufacturer='Acme',
        model='OLED-55',
        hardware_revision='1.0',
        panel_technology='woled',
        firmware='fw-1.2.3',
        picture_mode='filmmaker',
        content_mode='hdr10',
        local_dimming='off',
        contrast_enhancer='off',
        ambient_light_adaptation='off',
        eco_power_state='off',
        refresh_rate_hz=120.0,
        oled_light_setting=80.0,
    )
    fields.update(over)
    return build_display_state(
        document_id=DOC, captured_at_utc=_TS, **fields
    )


def _window(apl: float, window_pct: float = 10.0, **over):
    fields = dict(
        field_kind='windowed',
        window_size_percent=window_pct,
        apl_percent=apl,
        patch_position='center',
        content_kind='static',
        eotf='pq',
        sequence_id='seq-1',
        sequence_order=1,
        hold_duration_s=2.0,
    )
    fields.update(over)
    return build_stimulus_context(document_id=DOC, **fields)


def _full_field(apl: float = 100.0, **over):
    fields = dict(
        field_kind='full_field',
        window_size_percent=None,
        apl_percent=apl,
        content_kind='static',
        eotf='pq',
        sequence_id='seq-1',
        sequence_order=2,
    )
    fields.update(over)
    return build_stimulus_context(document_id=DOC, **fields)


def _peak_claim(**over):
    return DisplayClaim(kind='peak_luminance', **over)


def _claim(kind: str, **over) -> DisplayClaim:
    return DisplayClaim(kind=kind, **over)


def _measure(state, ctx, quantity, value, **over):
    fields = dict(
        value_cd_m2=value,
        warmup_minutes=45.0,
        instrument_ref='spec-1',
    )
    fields.update(over)
    return build_photometric_measurement(
        document_id=DOC,
        display_state=state,
        stimulus_context=ctx,
        quantity=quantity,
        measured_at_utc=_TS,
        **fields,
    )


def test_dtv10_single_apl_peak_is_limited() -> None:
    """A single 0% APL window never supports an across-content peak
    claim — only the measured APL state is qualified."""
    state = _clean_display()
    ctx = _window(apl=2.0, window_pct=10.0)
    meas = _measure(
        state, ctx, 'stabilized_window_luminance', 812.0
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_peak_claim()],
        stimulus_contexts=[ctx],
        measurements=[meas],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported_with_limitations'
    assert 'APL_COVERAGE_MISSING' in v.reasons
    assert q.state == 'qualified_with_limitations'


def test_dtv10_apl_sweep_qualifies_peak() -> None:
    """Windowed peaks measured across an APL span qualify without the
    coverage caveat."""
    state = _clean_display()
    ctxs = [
        _window(apl=a, window_pct=10.0, sequence_order=i)
        for i, a in enumerate((2.0, 10.0, 25.0, 50.0), start=1)
    ]
    meas = [
        _measure(state, c, 'stabilized_window_luminance', v)
        for c, v in zip(ctxs, (1100.0, 950.0, 820.0, 640.0))
    ]
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_peak_claim()],
        stimulus_contexts=ctxs,
        measurements=meas,
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported'
    assert 'APL_COVERAGE_MISSING' not in v.reasons


def test_dtv10_window_size_undeclared() -> None:
    state = _clean_display()
    ctx2 = build_stimulus_context(
        document_id=DOC,
        field_kind='windowed',
        window_size_percent=None,
        apl_percent=2.0,
    )
    meas = _measure(state, ctx2, 'transient_peak_luminance', 1000.0)
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_peak_claim()],
        stimulus_contexts=[ctx2],
        measurements=[meas],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert 'WINDOW_SIZE_UNDECLARED' in v.reasons
    assert v.verdict == 'supported_with_limitations'


def test_dtv20_temporal_dimming_attaches_to_sustained() -> None:
    """Static bright pattern dims over time — the observation must
    attach to every sustained claim, never dismissed as drift."""
    state = _clean_display()
    ctx = _full_field()
    meas = _measure(
        state, ctx, 'sustained_full_field_luminance', 240.0
    )
    obs = build_temporal_observation(
        document_id=DOC,
        display_state=state,
        stimulus_context=ctx,
        state='temporal_dimming_observed',
        elapsed_s=1800.0,
        luminance_drop_percent=12.0,
        region='whole_screen',
        recovery='not_observed',
        observed_at_utc=_TS,
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('sustained_full_field_luminance')],
        stimulus_contexts=[ctx],
        measurements=[meas],
        temporal_observations=[obs],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported_with_limitations'
    assert 'TEMPORAL_DIMMING_OBSERVED' in v.reasons
    assert 'TEMPORAL_DIMMING_OBSERVED' in q.reasons


def test_dtv20_provider_protection_requires_doc_ref() -> None:
    state = _clean_display()
    with pytest.raises(ValueError, match='document ref'):
        build_temporal_observation(
            document_id=DOC,
            display_state=state,
            state='provider_documented_protection',
            observed_at_utc=_TS,
        )


def test_dtv30_local_dimming_limits_black_claim() -> None:
    """Full-screen black with local dimming active never proves real-
    content black."""
    state = _clean_display(local_dimming='high')
    ctx = _full_field(apl=0.0)
    black = _measure(
        state, ctx, 'black_luminance', 0.0002, instrument_floor_cd_m2=0.0001
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('black_level')],
        stimulus_contexts=[ctx],
        measurements=[black],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported_with_limitations'
    assert 'LOCAL_DIMMING_ACTIVE_UNMEASURED' in v.reasons


def test_dtv30_local_dimming_unknown_honest() -> None:
    state = _clean_display(local_dimming='unknown')
    ctx = _full_field(apl=0.0)
    black = _measure(state, ctx, 'black_luminance', 0.001)
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('black_level')],
        stimulus_contexts=[ctx],
        measurements=[black],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert 'LOCAL_DIMMING_STATE_UNKNOWN' in v.reasons
    assert 'DISPLAY_STATE_INCOMPLETE' in q.reasons


def test_dtv30_contrast_kind_never_conflated() -> None:
    """An ANSI measurement cannot support a full-field contrast claim."""
    state = _clean_display()
    ctx = _full_field(apl=50.0)
    ansi = _measure(
        state, ctx, 'eotf_tracking_point', 200.0,
        contrast_kind='ansi_checkerboard_contrast', contrast_value=1200.0,
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('contrast', contrast_kind='full_field_contrast')],
        stimulus_contexts=[ctx],
        measurements=[ansi],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'insufficient_evidence'
    assert 'CONTRAST_KIND_CONFLATED' in v.reasons


def test_dtv30_contrast_claim_requires_kind() -> None:
    with pytest.raises(ValueError, match='contrast kind'):
        DisplayClaim(kind='contrast')


def test_dtv40_side_seat_needs_per_seat_evidence() -> None:
    """Multi-seat claims never average away a bad seat."""
    state = _clean_display()
    ctx = _window(apl=10.0)
    angles = [
        build_angle_measurement(
            document_id=DOC,
            display_state=state,
            stimulus_context=ctx,
            horizontal_angle_deg=0.0,
            vertical_angle_deg=0.0,
            seat_ref='mlp',
            measured_at_utc=_TS,
        ),
        build_angle_measurement(
            document_id=DOC,
            display_state=state,
            stimulus_context=ctx,
            horizontal_angle_deg=35.0,
            vertical_angle_deg=0.0,
            seat_ref='seat-left',
            luminance_cd_m2=340.0,
            chromaticity_x=0.32,
            chromaticity_y=0.33,
            measured_at_utc=_TS,
        ),
    ]
    claim = _claim(
        'multi_seat_consistency',
        scope='declared_seats',
        seat_refs=('mlp', 'seat-left', 'seat-right'),
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[claim],
        stimulus_contexts=[ctx],
        angle_measurements=angles,
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'insufficient_evidence'
    assert 'SEAT_COVERAGE_INCOMPLETE' in v.reasons
    assert v.unverified_seat_refs == ('seat-right',)


def test_dtv50_uniformity_grid_vs_sparse() -> None:
    state = _clean_display()
    ctx = _full_field(apl=100.0)
    grid = build_spatial_measurement(
        document_id=DOC,
        display_state=state,
        stimulus_context=ctx,
        observable='luminance',
        points=tuple(
            SpatialPoint(x_frac=x / 4.0, y_frac=y / 4.0, value=250.0)
            for x in (0, 1, 2, 3, 4)
            for y in (0, 1, 2, 3, 4)
        ),
        measured_at_utc=_TS,
    )
    assert grid.coverage == 'grid'
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('uniformity')],
        stimulus_contexts=[ctx],
        spatial_measurements=[grid],
        evaluated_at_utc=_TS,
    )
    assert q.claim_verdicts[0].verdict == 'supported'

    sparse = build_spatial_measurement(
        document_id=DOC,
        display_state=state,
        stimulus_context=ctx,
        observable='luminance',
        points=(
            SpatialPoint(x_frac=0.5, y_frac=0.5, value=250.0),
            SpatialPoint(x_frac=0.1, y_frac=0.1, value=241.0),
            SpatialPoint(x_frac=0.9, y_frac=0.9, value=238.0),
        ),
        measured_at_utc=_TS,
    )
    assert sparse.coverage == 'sparse'
    q2 = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('uniformity')],
        stimulus_contexts=[ctx],
        spatial_measurements=[sparse],
        evaluated_at_utc=_TS,
    )
    v2 = q2.claim_verdicts[0]
    assert v2.verdict == 'supported_with_limitations'
    assert 'UNIFORMITY_PARTIAL' in v2.reasons


def test_dtv60_warmup_and_order_compose() -> None:
    """Warm-up state and measurement order (#573 composition) attach to
    sustained claims when unrecorded."""
    state = _clean_display()
    ctx = _full_field()
    meas = _measure(
        state, ctx, 'sustained_full_field_luminance', 240.0,
        warmup_minutes=None,
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('sustained_full_field_luminance')],
        stimulus_contexts=[ctx],
        measurements=[meas],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert 'WARMUP_STATE_UNKNOWN' in v.reasons
    # _full_field records a sequence → no order caveat here.
    assert 'MEASUREMENT_ORDER_UNRECORDED' not in v.reasons


def test_dtv70_adaptive_ambient_mode_limits_reference() -> None:
    state = _clean_display(ambient_light_adaptation='on')
    ctx = _window(apl=10.0)
    meas = _measure(state, ctx, 'chromaticity', None,
                    chromaticity_x=0.3127, chromaticity_y=0.3290)
    claim = _claim('color_performance', requires_reference_environment=True)
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[claim],
        stimulus_contexts=[ctx],
        measurements=[meas],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert 'ADAPTIVE_BEHAVIOR_UNDECLARED' in v.reasons
    assert v.verdict == 'supported_with_limitations'


def test_dtv80_instrument_floor_bounds_black() -> None:
    """A reading at/below the instrument floor is a bounded claim —
    never infinite contrast."""
    state = _clean_display()
    ctx = _full_field(apl=0.0)
    black = _measure(
        state, ctx, 'black_luminance', 0.00005,
        instrument_floor_cd_m2=0.0001, below_instrument_floor=True,
    )
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('black_level')],
        stimulus_contexts=[ctx],
        measurements=[black],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported_with_limitations'
    assert 'INSTRUMENT_FLOOR_LIMITED' in v.reasons


def test_dtv90_static_sweep_never_proves_dynamic() -> None:
    state = _clean_display()
    ctx = _window(apl=10.0, content_kind='static')
    pts = [
        _measure(state, ctx, 'eotf_tracking_point', v)
        for v in (10.0, 50.0, 100.0)
    ]
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('eotf_tracking')],
        stimulus_contexts=[ctx],
        measurements=pts,
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'supported_with_limitations'
    assert 'CONTENT_STATE_MISMATCH' in v.reasons


def test_dtv_transient_never_promoted_to_sustained() -> None:
    state = _clean_display()
    ctx = _window(apl=5.0)
    meas = _measure(state, ctx, 'transient_peak_luminance', 1400.0)
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('sustained_full_field_luminance')],
        stimulus_contexts=[ctx],
        measurements=[meas],
        evaluated_at_utc=_TS,
    )
    v = q.claim_verdicts[0]
    assert v.verdict == 'unsupported'
    assert 'PEAK_PROMOTED_TO_SUSTAINED' in v.reasons


def test_dtv_content_independent_claim_unsupported() -> None:
    state = _clean_display()
    q = evaluate_direct_view_qualification(
        document_id=DOC,
        display_state=state,
        claims=[_claim('content_independent_performance')],
        evaluated_at_utc=_TS,
    )
    assert q.claim_verdicts[0].verdict == 'unsupported'
    assert q.state == 'unsupported'


def test_dtv_unknown_never_defaults_off() -> None:
    state = build_display_state(document_id=DOC, captured_at_utc=_TS)
    assert 'local_dimming' in state.unknown_fields
    assert 'ambient_light_adaptation' in state.unknown_fields
    assert state.adaptive_behavior_risk


def test_dtv_context_window_rules() -> None:
    with pytest.raises(ValueError, match='full-field'):
        build_stimulus_context(
            document_id=DOC, field_kind='full_field',
            window_size_percent=80.0,
        )
    with pytest.raises(ValueError, match='full-field'):
        build_stimulus_context(
            document_id=DOC, field_kind='windowed',
            window_size_percent=100.0,
        )


def test_dtv_spatial_unique_points() -> None:
    state = _clean_display()
    ctx = _full_field()
    with pytest.raises(ValueError, match='unique'):
        build_spatial_measurement(
            document_id=DOC,
            display_state=state,
            stimulus_context=ctx,
            observable='luminance',
            points=(
                SpatialPoint(x_frac=0.5, y_frac=0.5, value=250.0),
                SpatialPoint(x_frac=0.5, y_frac=0.5, value=251.0),
            ),
            measured_at_utc=_TS,
        )


def test_dtv_repository_full_chain(tmp_path) -> None:
    repo = CadDirectViewDisplayRepository(_scene_repo(tmp_path))
    state = _clean_display()
    ctx = _window(apl=10.0)
    repo.save_display_state(state)
    repo.save_stimulus_context(ctx)
    meas = _measure(state, ctx, 'stabilized_window_luminance', 900.0)
    repo.save_measurement(meas)
    obs = build_temporal_observation(
        document_id=DOC, display_state=state,
        state='no_observed_dimming', observed_at_utc=_TS,
    )
    repo.save_temporal_observation(obs)
    spatial = build_spatial_measurement(
        document_id=DOC, display_state=state, stimulus_context=ctx,
        observable='luminance',
        points=(SpatialPoint(x_frac=0.5, y_frac=0.5, value=250.0),),
        measured_at_utc=_TS,
    )
    repo.save_spatial_measurement(spatial)
    angle = build_angle_measurement(
        document_id=DOC, display_state=state, stimulus_context=ctx,
        horizontal_angle_deg=30.0, vertical_angle_deg=0.0,
        measured_at_utc=_TS,
    )
    repo.save_angle_measurement(angle)
    qual = evaluate_direct_view_qualification(
        document_id=DOC, display_state=state,
        claims=[_peak_claim()], stimulus_contexts=[ctx],
        measurements=[meas], temporal_observations=[obs],
        spatial_measurements=[spatial], angle_measurements=[angle],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual
    assert repo.get_measurement(meas.measurement_id) == meas
    # Idempotent re-save.
    repo.save_measurement(meas)


def test_dtv_repository_rejects_unpersisted_parent(tmp_path) -> None:
    repo = CadDirectViewDisplayRepository(_scene_repo(tmp_path))
    state = _clean_display()
    ctx = _window(apl=10.0)
    meas = _measure(state, ctx, 'black_luminance', 0.001)
    with pytest.raises(DirectViewDisplayIntegrityError, match='persist'):
        repo.save_measurement(meas)


def test_dtv_repository_conflict_on_divergent_save(tmp_path) -> None:
    repo = CadDirectViewDisplayRepository(_scene_repo(tmp_path))
    state = _clean_display()
    repo.save_display_state(state)
    forged = state.model_copy(
        update={'panel_technology': 'qd_oled'}
    )
    with pytest.raises(DirectViewDisplayIntegrityError):
        repo.save_display_state(forged)


# ---------------------------------------------------------------------------
# #626 fixtures
# ---------------------------------------------------------------------------


def _spd(peak_shift: float = 0.0, narrowband: bool = False) -> SpdBlock:
    """Two synthetically distinct SPD families with the same xy."""
    if narrowband:
        samples = [
            SpdSample(wavelength_nm=450.0, value=0.9),
            SpdSample(wavelength_nm=530.0, value=0.4),
            SpdSample(wavelength_nm=610.0, value=0.85),
        ]
    else:
        samples = [
            SpdSample(wavelength_nm=w, value=0.5 + 0.2 * peak_shift)
            for w in (420.0, 500.0, 580.0, 660.0)
        ]
    return SpdBlock(
        samples=tuple(samples),
        wavelength_min_nm=380.0,
        wavelength_max_nm=780.0,
        normalization='peak_normalized',
    )


def _spectral_state(
    display_ref: str,
    system_kind: str = 'direct_view_emissive',
    spd: SpdBlock | None = None,
    evidence: str = 'spectroradiometer_measured',
    **over,
):
    fields = dict(
        system_kind=system_kind,
        evidence_class=evidence,
        spd=spd,
        instrument_ref=(
            'spec-1'
            if evidence == 'spectroradiometer_measured' else None
        ),
        white_point=ChromaticityPoint(x=0.3127, y=0.3290),
        picture_mode='filmmaker',
        firmware='fw-1.0',
    )
    fields.update(over)
    return build_spectral_state(
        document_id=DOC,
        display_ref=display_ref,
        measured_at_utc=_TS,
        **fields,
    )


def _iec_profile(**over):
    fields = dict(
        kind='iec_ts_61966_13_2023_cor1_2025',
        revision='2023+COR1:2025',
        observer_set='iec_61966_13_observer_set',
        applicable_system_kinds=(
            'direct_view_emissive', 'direct_view_backlit',
        ),
        external_standard_ref='IEC TS 61966-13:2023+COR1:2025',
    )
    fields.update(over)
    return build_observer_profile(
        document_id=DOC, label='IEC TS 61966-13', **fields
    )


def test_omf10_identical_spd_pair_verified() -> None:
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd())
    assert ref.spd == dut.spd
    assert ref.spectral_state_sha256 != dut.spectral_state_sha256
    profile = _iec_profile()
    ev = build_metamerism_evaluation(
        document_id=DOC,
        reference_state=ref, dut_state=dut, profile=profile,
        result_class='observer_metameric_failure',
        metric_value=0.0, metric_units='deltaI',
        metric_semantics='IEC TS 61966-13 observer-metamerism index',
        evaluated_at_utc=_TS,
    )
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        profile=profile, evaluation=ev,
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'verified_within_profile'
    assert 'SAME_SPD_PAIR' in q.reasons
    assert 'METER_CORRECTION_NOT_OBSERVER_PROOF' in q.reasons


def test_omf20_same_xy_different_spd() -> None:
    """Identical x/y with different SPDs — the classical metameric
    pair the authority exists for."""
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    assert ref.white_point == dut.white_point
    assert ref.spectral_state_sha256 != dut.spectral_state_sha256
    profile = _iec_profile()
    ev = build_metamerism_evaluation(
        document_id=DOC,
        reference_state=ref, dut_state=dut, profile=profile,
        result_class='standard_observer_colorimetric_difference',
        metric_value=0.4, metric_units='deltaI',
        metric_semantics='IEC TS 61966-13 observer-metamerism index',
        evaluated_at_utc=_TS,
    )
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        profile=profile, evaluation=ev,
        evaluated_at_utc=_TS,
    )
    assert q.verdict in (
        'verified_within_profile', 'verified_with_limitations'
    )
    assert 'VERIFIED_WITHIN_PROFILE' in q.reasons


def test_omf30_instrument_mismatch_classified_separately() -> None:
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        instrument_mismatch_suspected=True,
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'instrument_mismatch_explains'
    assert 'INSTRUMENT_MISMATCH_SUSPECTED' in q.reasons


def test_omf40_single_observer_match_is_limitation() -> None:
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    profile = _iec_profile()
    ev = build_metamerism_evaluation(
        document_id=DOC,
        reference_state=ref, dut_state=dut, profile=profile,
        result_class='standard_observer_colorimetric_difference',
        metric_value=0.3, metric_units='deltaI',
        metric_semantics='IEC TS 61966-13 observer-metamerism index',
        evaluated_at_utc=_TS,
    )
    match = build_perceptual_match(
        document_id=DOC,
        reference_state_id=ref.spectral_state_id,
        dut_state_id=dut.spectral_state_id,
        nominal_target=ChromaticityPoint(x=0.3127, y=0.3290),
        selected_target=ChromaticityPoint(x=0.314, y=0.331),
        observer_identity_class='single_calibrator',
        observer_count=1,
        adaptation_environment='d65_surround',
        recorded_at_utc=_TS,
    )
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        profile=profile, evaluation=ev,
        perceptual_matches=[match],
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'verified_with_limitations'
    assert 'SINGLE_OBSERVER_NOT_UNIVERSAL' in q.reasons
    assert 'PERCEPTUAL_OFFSET_RECORDED' in q.reasons


def test_omf50_offset_never_mutates_nominal_target() -> None:
    """The user-chosen perceptual white is a delta against an immutable
    nominal target — never a redefinition of D65."""
    nominal = ChromaticityPoint(x=0.3127, y=0.3290)
    match = build_perceptual_match(
        document_id=DOC,
        reference_state_id='om:x',
        dut_state_id='om:y',
        nominal_target=nominal,
        selected_target=ChromaticityPoint(x=0.31, y=0.335),
        recorded_at_utc=_TS,
    )
    assert match.nominal_target == nominal
    with pytest.raises(Exception):
        match.nominal_target = ChromaticityPoint(x=0.3, y=0.3)


def test_omf60_projection_out_of_profile_scope() -> None:
    """IEC TS 61966-13 / ANSI CTA-6035 never apply to reflected
    projection systems."""
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state(
        'proj-1', system_kind='projected_reflected', spd=_spd()
    )
    profile = _iec_profile()
    with pytest.raises(ValueError, match='not applicable'):
        build_metamerism_evaluation(
            document_id=DOC,
            reference_state=ref, dut_state=dut, profile=profile,
            result_class='standard_observer_colorimetric_difference',
            metric_value=0.2, metric_units='deltaI',
            metric_semantics='test', evaluated_at_utc=_TS,
        )
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        profile=profile,
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'profile_inapplicable'
    assert 'PROJECTION_OUT_OF_PROFILE_SCOPE' in q.reasons


def test_omf60_profile_construction_rejects_projection_scope() -> None:
    with pytest.raises(ValueError, match='projection'):
        _iec_profile(
            applicable_system_kinds=(
                'direct_view_emissive', 'projected_reflected',
            )
        )


def test_omf_tristimulus_only_insufficient() -> None:
    ref = _spectral_state(
        'disp-a', spd=None, evidence='tristimulus_only',
        instrument_ref=None,
    )
    dut = _spectral_state('disp-b', spd=_spd())
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'insufficient_spectral_evidence'
    assert 'TRISTIMULUS_ONLY_EVIDENCE' in q.reasons


def test_omf_spd_requires_instrument_binding() -> None:
    with pytest.raises(ValueError, match='instrument'):
        _spectral_state(
            'disp-a', spd=_spd(),
            evidence='spectroradiometer_measured', instrument_ref=None,
        )


def test_omf_tristimulus_cannot_carry_spd() -> None:
    with pytest.raises(ValueError, match='cannot carry an SPD'):
        _spectral_state(
            'disp-a', spd=_spd(), evidence='tristimulus_only',
            instrument_ref=None,
        )


def test_omf_metric_requires_units() -> None:
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    profile = _iec_profile()
    with pytest.raises(ValueError, match='units'):
        build_metamerism_evaluation(
            document_id=DOC,
            reference_state=ref, dut_state=dut, profile=profile,
            result_class='standard_observer_colorimetric_difference',
            metric_value=0.2, metric_units=None,
            evaluated_at_utc=_TS,
        )


def test_omf_multi_observer_needs_panel_class() -> None:
    with pytest.raises(ValueError, match='identity'):
        build_perceptual_match(
            document_id=DOC,
            reference_state_id='om:x', dut_state_id='om:y',
            nominal_target=ChromaticityPoint(x=0.3127, y=0.3290),
            observer_identity_class='single_calibrator',
            observer_count=4,
            recorded_at_utc=_TS,
        )


def test_omf_unpaired_evaluation_conflicts() -> None:
    """An evaluation bound to different states is never evidence for
    this pair."""
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    other = _spectral_state('disp-c', spd=_spd(peak_shift=1.0))
    profile = _iec_profile()
    ev = build_metamerism_evaluation(
        document_id=DOC,
        reference_state=ref, dut_state=other, profile=profile,
        result_class='standard_observer_colorimetric_difference',
        metric_value=0.2, metric_units='deltaI',
        metric_semantics='test', evaluated_at_utc=_TS,
    )
    q = evaluate_observer_metamerism(
        document_id=DOC,
        goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut,
        profile=profile, evaluation=ev,
        evaluated_at_utc=_TS,
    )
    assert q.verdict == 'conflicting_evidence'
    assert 'UNPAIRED_SCALAR_REJECTED' in q.reasons


def test_omf_repository_chain(tmp_path) -> None:
    repo = CadObserverMetamerismRepository(_scene_repo(tmp_path))
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    repo.save_spectral_state(ref)
    repo.save_spectral_state(dut)
    profile = _iec_profile()
    repo.save_profile(profile)
    ev = build_metamerism_evaluation(
        document_id=DOC,
        reference_state=ref, dut_state=dut, profile=profile,
        result_class='standard_observer_colorimetric_difference',
        metric_value=0.3, metric_units='deltaI',
        metric_semantics='test', evaluated_at_utc=_TS,
    )
    repo.save_evaluation(ev)
    match = build_perceptual_match(
        document_id=DOC,
        reference_state_id=ref.spectral_state_id,
        dut_state_id=dut.spectral_state_id,
        nominal_target=ChromaticityPoint(x=0.3127, y=0.3290),
        recorded_at_utc=_TS,
    )
    repo.save_perceptual_match(match)
    qual = evaluate_observer_metamerism(
        document_id=DOC, goal='cross_display_perceptual_match',
        reference_state=ref, dut_state=dut, profile=profile,
        evaluation=ev, perceptual_matches=[match],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual
    assert len(repo.list_spectral_states(DOC)) == 2


# ---------------------------------------------------------------------------
# #633 fixtures
# ---------------------------------------------------------------------------


def _dark_theater_observation(**over):
    fields = dict(
        room_ref='room-1',
        ambient_illuminance_lx=1.0,
        incident_light=IncidentLightState(
            incident_illuminance_lx=0.2,
            measurement_direction='screen_normal',
            specular_reflection_observed=False,
        ),
        surround=SurroundState(
            surround_luminance_cd_m2=5.0,
            periphery_luminance_cd_m2=4.5,
            surround_chromaticity_x=0.3127,
            surround_chromaticity_y=0.3290,
            spectral_evidence='spectral_measured',
            bias_light_state='on',
        ),
        wall_reflectance=0.1,
        stray_light_sources=(),
        blind_curtain_state='closed',
        instrument_ref='lux-1',
    )
    fields.update(over)
    return build_environment_observation(
        document_id=DOC, captured_at_utc=_TS, **fields
    )


def _bound_geometry(obs=None, **over):
    fields = dict(
        viewing_distance_m=2.4,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
        screen_angular_size_deg=45.0,
        seat_ref='mlp',
    )
    fields.update(over)
    return build_viewing_geometry(
        document_id=DOC, measured_at_utc=_TS, observation=obs, **fields
    )


def test_vve10_dark_reference_theater_meets_bt2035() -> None:
    obs = _dark_theater_observation()
    geo = _bound_geometry(obs)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        external_standard_ref='ITU-R BT.2035',
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_met'
    assert 'PROFILE_MET' in q.reasons


def test_vve20_bright_room_fails_dark_room() -> None:
    """The same display in a bright room is a different environment
    qualification — environment state never rides on display output."""
    obs = _dark_theater_observation(
        ambient_illuminance_lx=180.0,
        incident_light=IncidentLightState(
            incident_illuminance_lx=30.0,
        ),
        blind_curtain_state='open',
    )
    geo = _bound_geometry(obs)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2166_hdr_sdr_critical_monitoring',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_not_met'
    dark = [
        v for v in q.requirement_verdicts
        if v.requirement == 'dark_room'
    ][0]
    assert dark.state == 'unmet'
    assert dark.reason == 'STRAY_LIGHT_PRESENT'


def test_vve30_coloured_bias_light_fails_neutral() -> None:
    obs = _dark_theater_observation(
        surround=SurroundState(
            surround_luminance_cd_m2=5.0,
            periphery_luminance_cd_m2=4.0,
            surround_chromaticity_x=0.45,  # warm-tinted
            surround_chromaticity_y=0.40,
            spectral_evidence='spectral_measured',
            bias_light_state='on',
        ),
    )
    geo = _bound_geometry(obs)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2166_hdr_sdr_critical_monitoring',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_not_met'
    chroma = [
        v for v in q.requirement_verdicts
        if v.requirement == 'surround_neutral_chromaticity'
    ][0]
    assert chroma.state == 'unmet'
    assert chroma.reason == 'SURROUND_CHROMATICITY_MISMATCH'


def test_vve40_stray_light_fails() -> None:
    obs = _dark_theater_observation(
        stray_light_sources=('window-leak', 'led-strip'),
    )
    geo = _bound_geometry(obs)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_not_met'
    stray = [
        v for v in q.requirement_verdicts
        if v.requirement == 'no_stray_light'
    ][0]
    assert stray.state == 'unmet'


def test_vve50_environment_distinct_from_display_state() -> None:
    """The environment qualification binds the display state by ref
    only — a calibrated display never implies a calibrated room."""
    obs = _dark_theater_observation()
    geo = _bound_geometry(obs)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[geo],
        display_state_ref='dvs:abc',
        evaluated_at_utc=_TS,
    )
    assert q.display_state_ref == 'dvs:abc'
    # No display display_sha needed — environment stands alone.
    assert q.state == 'profile_met'


def test_vve60_side_seat_geometry_bound() -> None:
    obs = _dark_theater_observation()
    geo = _bound_geometry(obs, seat_ref='seat-right',
                          horizontal_angle_deg=35.0)
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt500_subjective_home',
        profile_scope='project_normal_use',
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_met'


def test_vve70_scenes_qualify_independently() -> None:
    """MOVIE_DARK meets the project profile; INTERMISSION is captured
    as a different condition — never folded into one badge."""
    obs_dark = _dark_theater_observation()
    obs_lit = _dark_theater_observation(
        ambient_illuminance_lx=120.0,
        surround=SurroundState(
            surround_luminance_cd_m2=60.0,
            periphery_luminance_cd_m2=55.0,
            spectral_evidence='photometric_only',
            bias_light_state='off',
        ),
        blind_curtain_state='open',
    )
    scene_dark = build_lighting_scene(
        document_id=DOC, name='MOVIE_DARK', kind='movie_dark',
        bound_observation=obs_dark, declared_at_utc=_TS,
    )
    scene_lit = build_lighting_scene(
        document_id=DOC, name='INTERMISSION', kind='intermission',
        bound_observation=obs_lit, declared_at_utc=_TS,
    )
    q_dark = evaluate_viewing_environment(
        document_id=DOC, observation=obs_dark,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[_bound_geometry(obs_dark)],
        evaluated_at_utc=_TS,
    )
    q_lit = evaluate_viewing_environment(
        document_id=DOC, observation=obs_lit,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[_bound_geometry(obs_lit)],
        evaluated_at_utc=_TS,
    )
    assert q_dark.state == 'profile_met'
    assert q_lit.state in (
        'profile_not_met', 'profile_met_with_limitations',
        'insufficient_evidence',
    )
    assert scene_dark.bound_observation_id != scene_lit.bound_observation_id


def test_vve80_wall_repaint_stales_prior_evidence() -> None:
    before = _dark_theater_observation(wall_colour='neutral-grey')
    after = _dark_theater_observation(
        wall_colour='burgundy', wall_reflectance=0.25,
    )
    cmp_ = compare_viewing_environments(before, after)
    assert cmp_.verdict == 'not_comparable'
    assert 'wall_reflectance_or_colour' in cmp_.changed_axes

    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=after,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[_bound_geometry(after)],
        prior_change_axes=cmp_.changed_axes,
        evaluated_at_utc=_TS,
    )
    assert 'ENVIRONMENT_STALE' in q.reasons
    assert q.stale_after_change == ('wall_reflectance_or_colour',)


def test_vve_cct_alone_never_d65_spectral() -> None:
    """CCT/chromaticity alone never claims a D65 spectral surround."""
    obs = _dark_theater_observation(
        surround=SurroundState(
            surround_luminance_cd_m2=5.0,
            periphery_luminance_cd_m2=4.5,
            surround_chromaticity_x=0.3127,
            surround_chromaticity_y=0.3290,
            surround_cct_k=6500.0,
            spectral_evidence='cct_chromaticity_measured',
            bias_light_state='on',
        ),
    )
    q = evaluate_viewing_environment(
        document_id=DOC,
        observation=obs,
        profile_kind='itu_bt2166_hdr_sdr_critical_monitoring',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[_bound_geometry(obs)],
        evaluated_at_utc=_TS,
    )
    spectral = [
        v for v in q.requirement_verdicts
        if v.requirement == 'surround_spectral_evidence'
    ][0]
    assert spectral.state == 'unknown'
    assert spectral.reason == 'SPECTRAL_EVIDENCE_INSUFFICIENT'
    assert q.state != 'profile_met'


def test_vve_incident_never_room_lux() -> None:
    """Incident illuminance and ambient room lux are separate fields —
    one never substitutes for the other."""
    obs = build_environment_observation(
        document_id=DOC, captured_at_utc=_TS,
        ambient_illuminance_lx=None,
        incident_light=IncidentLightState(
            incident_illuminance_lx=0.2,
        ),
    )
    q = evaluate_viewing_environment(
        document_id=DOC, observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[_bound_geometry(obs)],
        evaluated_at_utc=_TS,
    )
    ambient = [
        v for v in q.requirement_verdicts
        if v.requirement == 'ambient_illuminance_bounded'
    ][0]
    assert ambient.state == 'unknown'
    assert ambient.reason == 'AMBIENT_ILLUMINANCE_UNMEASURED'


def test_vve_broadcast_profile_not_residential_law() -> None:
    """A non-selected broadcast profile reports scope notes — a
    residential room is never defective for missing BT.2166."""
    obs = _dark_theater_observation(
        ambient_illuminance_lx=40.0,
        incident_light=IncidentLightState(
            incident_illuminance_lx=8.0,
        ),
    )
    q = evaluate_viewing_environment(
        document_id=DOC, observation=obs,
        profile_kind='itu_bt2166_hdr_sdr_critical_monitoring',
        profile_scope='project_normal_use',
        selected_as_requirement=False,
        geometry=[_bound_geometry(obs)],
        evaluated_at_utc=_TS,
    )
    assert 'NOT_REQUESTED_PROFILE' in q.reasons
    assert 'PROFILE_SCOPE_NOTE' in q.reasons


def test_vve_normal_use_capture_only() -> None:
    obs = _dark_theater_observation()
    q = evaluate_viewing_environment(
        document_id=DOC, observation=obs,
        profile_kind='normal_use_scenario',
        profile_scope='project_normal_use',
        evaluated_at_utc=_TS,
    )
    assert q.state == 'profile_met_with_limitations'


def test_vve_geometry_unbound_unknown() -> None:
    obs = _dark_theater_observation()
    q = evaluate_viewing_environment(
        document_id=DOC, observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=(),
        evaluated_at_utc=_TS,
    )
    geo_req = [
        v for v in q.requirement_verdicts
        if v.requirement == 'viewing_geometry_bound'
    ][0]
    assert geo_req.state == 'unknown'
    assert geo_req.reason == 'GEOMETRY_UNVERIFIED'
    assert q.state == 'insufficient_evidence'


def test_vve_comparability_identical_is_comparable() -> None:
    a = _dark_theater_observation()
    b = _dark_theater_observation()
    cmp_ = compare_viewing_environments(a, b)
    assert cmp_.verdict == 'comparable'
    assert not cmp_.changed_axes


def test_vve_repository_chain(tmp_path) -> None:
    repo = CadViewingEnvironmentRepository(_scene_repo(tmp_path))
    obs = _dark_theater_observation()
    repo.save_observation(obs)
    geo = _bound_geometry(obs)
    repo.save_geometry(geo)
    scene = build_lighting_scene(
        document_id=DOC, name='MOVIE_DARK', kind='movie_dark',
        bound_observation=obs, declared_at_utc=_TS,
    )
    repo.save_scene(scene)
    qual = evaluate_viewing_environment(
        document_id=DOC, observation=obs,
        profile_kind='itu_bt2035_reference_hdtv',
        profile_scope='reference_evaluation',
        selected_as_requirement=True,
        geometry=[geo],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual
    assert repo.get_scene(scene.scene_id) == scene


def test_vve_repository_rejects_unpersisted_parent(tmp_path) -> None:
    repo = CadViewingEnvironmentRepository(_scene_repo(tmp_path))
    obs = _dark_theater_observation()
    geo = _bound_geometry(obs)
    with pytest.raises(ViewingEnvironmentIntegrityError, match='persist'):
        repo.save_geometry(geo)


# ---------------------------------------------------------------------------
# Cross-cutting: audit chain + row replay
# ---------------------------------------------------------------------------


def test_authority_chain_covers_rev57_disp(tmp_path) -> None:
    """Every probe table must resolve through the lazy repository
    factory — REV57-DISP added three repositories to the audit chain."""
    from htdt.native_authority_audit import _RepositoryChain

    _scene_repo(tmp_path)
    chain = _RepositoryChain(tmp_path / 'cad.sqlite3')
    try:
        for name in (
            'direct_view_display', 'observer_metamerism',
            'viewing_environment',
        ):
            assert chain.repo(name) is not None, name
    finally:
        chain.close()


def test_native_audit_replays_disp_rows(tmp_path) -> None:
    """Populated REV57-DISP tables replay through the audit without
    integrity findings."""
    from htdt.native_authority_audit import assert_native_authority_graph

    scene = _scene_repo(tmp_path)

    dv = CadDirectViewDisplayRepository(scene)
    state = _clean_display()
    ctx = _window(apl=10.0)
    dv.save_display_state(state)
    dv.save_stimulus_context(ctx)
    meas = _measure(state, ctx, 'stabilized_window_luminance', 900.0)
    dv.save_measurement(meas)
    dv.save_temporal_observation(
        build_temporal_observation(
            document_id=DOC, display_state=state,
            state='no_observed_dimming', observed_at_utc=_TS,
        )
    )
    dv.save_spatial_measurement(
        build_spatial_measurement(
            document_id=DOC, display_state=state, stimulus_context=ctx,
            observable='luminance',
            points=(SpatialPoint(x_frac=0.5, y_frac=0.5, value=250.0),),
            measured_at_utc=_TS,
        )
    )
    dv.save_angle_measurement(
        build_angle_measurement(
            document_id=DOC, display_state=state, stimulus_context=ctx,
            horizontal_angle_deg=0.0, vertical_angle_deg=0.0,
            measured_at_utc=_TS,
        )
    )
    dv.save_qualification(
        evaluate_direct_view_qualification(
            document_id=DOC, display_state=state,
            claims=[_peak_claim()], stimulus_contexts=[ctx],
            measurements=[meas], evaluated_at_utc=_TS,
        )
    )

    om = CadObserverMetamerismRepository(scene)
    ref = _spectral_state('disp-a', spd=_spd())
    dut = _spectral_state('disp-b', spd=_spd(narrowband=True))
    om.save_spectral_state(ref)
    om.save_spectral_state(dut)
    profile = _iec_profile()
    om.save_profile(profile)
    ev = build_metamerism_evaluation(
        document_id=DOC, reference_state=ref, dut_state=dut,
        profile=profile,
        result_class='standard_observer_colorimetric_difference',
        metric_value=0.3, metric_units='deltaI',
        metric_semantics='test', evaluated_at_utc=_TS,
    )
    om.save_evaluation(ev)
    om.save_perceptual_match(
        build_perceptual_match(
            document_id=DOC,
            reference_state_id=ref.spectral_state_id,
            dut_state_id=dut.spectral_state_id,
            nominal_target=ChromaticityPoint(x=0.3127, y=0.3290),
            recorded_at_utc=_TS,
        )
    )
    om.save_qualification(
        evaluate_observer_metamerism(
            document_id=DOC, goal='cross_display_perceptual_match',
            reference_state=ref, dut_state=dut, profile=profile,
            evaluation=ev, evaluated_at_utc=_TS,
        )
    )

    ve = CadViewingEnvironmentRepository(scene)
    obs = _dark_theater_observation()
    ve.save_observation(obs)
    geo = _bound_geometry(obs)
    ve.save_geometry(geo)
    ve.save_scene(
        build_lighting_scene(
            document_id=DOC, name='MOVIE_DARK', kind='movie_dark',
            bound_observation=obs, declared_at_utc=_TS,
        )
    )
    ve.save_qualification(
        evaluate_viewing_environment(
            document_id=DOC, observation=obs,
            profile_kind='itu_bt2035_reference_hdtv',
            profile_scope='reference_evaluation',
            selected_as_requirement=True,
            geometry=[geo], evaluated_at_utc=_TS,
        )
    )

    assert_native_authority_graph(tmp_path / 'cad.sqlite3')
