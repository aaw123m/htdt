"""Video colorimetry commissioning tests (#647)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_colorimetry import (
    ChromaticityPoint,
    ColorTolerances,
    ColorimeterCorrectionProfile,
    StimulusDefinition,
    TristimulusSample,
    VideoColorTargetProfile,
    build_video_color_measurement_set,
    build_video_color_target_profile,
    color_evaluation_status,
    evaluate_video_color,
)
from htdt.cad_equipment import EquipmentDataProvenance


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='klein-k10',
            source_version='fw 2.1',
            source_reference='serial 1234',
            source_sha256='9' * 64,
        ),
    )


def _target() -> VideoColorTargetProfile:
    return build_video_color_target_profile(
        target_id='rec709-d65',
        version='1',
        label='Rec.709 gamma 2.4',
        eotf='gamma_2_4',
        white_point=ChromaticityPoint(x=0.3127, y=0.3290),
        primary_red=ChromaticityPoint(x=0.640, y=0.330),
        primary_green=ChromaticityPoint(x=0.300, y=0.600),
        primary_blue=ChromaticityPoint(x=0.150, y=0.060),
        stimulus_levels=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        tolerances=ColorTolerances(
            white_point_delta_e=2.0,
            grayscale_delta_e=3.0,
            gamut_delta_e=3.0,
        ),
        provenance=_provenance(),
    )


def _xyz_for_xy(x: float, y: float, lum: float) -> TristimulusSample:
    return (x / y * lum, lum, (1 - x - y) / y * lum)


def _sample(stimulus_id: str, x: float, y: float, lum: float, **kw):
    xs, ys, zs = _xyz_for_xy(x, y, lum)
    return TristimulusSample(
        stimulus_id=stimulus_id, x=xs, y_luminance=ys, z=zs, **kw
    )


def _measurement(samples=None):
    return build_video_color_measurement_set(
        measurement_set_id='set-1',
        measured_at_utc='2026-09-23T01:00:00+00:00',
        surface_entity_id='screen-main',
        meter='klein-k10',
        meter_correction=ColorimeterCorrectionProfile(
            correction_id='k10-oled-1',
            version='1',
            base_meter='klein-k10',
            correction_kind='four_color_matrix',
            applies_to_display_class='oled',
        ),
        stimulus=StimulusDefinition(
            encoding='rgb_limited',
            bit_depth=10,
            patch_size_percent=10.0,
            pattern_generator='murideo-g7',
            signal_path='gen → avr → display hdmi2',
        ),
        samples=samples
        if samples is not None
        else (
            _sample('white_100', 0.3127, 0.3290, 120.0),
            _sample('gray_50', 0.3130, 0.3295, 18.0),
            _sample('gray_20', 0.3135, 0.3300, 2.4),
            _sample('r', 0.640, 0.330, 25.0),
            _sample('g', 0.300, 0.600, 80.0),
            _sample('b', 0.150, 0.060, 6.0),
        ),
        import_source='colourspace-csv',
        import_app_version='1.4',
        import_asset_sha256='8' * 64,
        import_parser_id='csv-xyz-v1',
        provenance=_provenance(),
    )


def test_target_hash_and_eotf_label_rule():
    target = _target()
    assert target.target_sha256
    payload = target.model_dump(mode='python')
    payload['white_point'] = {'x': 0.31, 'y': 0.33}
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        VideoColorTargetProfile(**payload)
    with pytest.raises(ValueError, match='eotf_label'):
        build_video_color_target_profile(
            target_id='t', version='1', eotf='other'
        )


def test_measurement_set_import_triple():
    with pytest.raises(ValueError, match='supplied together'):
        build_video_color_measurement_set(
            measurement_set_id='bad',
            measured_at_utc='2026-09-23T00:00:00+00:00',
            surface_entity_id='screen-main',
            meter='m',
            samples=(_sample('white', 0.31, 0.33, 100.0),),
            import_source='csv',  # missing asset hash + parser
        )


def test_duplicate_stimulus_ids_rejected():
    s = _sample('white', 0.31, 0.33, 100.0)
    with pytest.raises(ValueError, match='unique'):
        build_video_color_measurement_set(
            measurement_set_id='dup',
            measured_at_utc='2026-09-23T00:00:00+00:00',
            surface_entity_id='screen-main',
            meter='m',
            samples=(s, s),
        )


def test_evaluation_groups():
    evaluation = evaluate_video_color(
        target=_target(), measurement_set=_measurement()
    )
    by_group = {g.group: g for g in evaluation.groups}
    assert by_group['white_point'].status == 'PASS'
    assert by_group['grayscale'].status == 'PASS'
    assert by_group['gamut'].status == 'PASS'
    # target declares an EOTF but no eotf tolerance / stimulus-level
    # samples → tracking group is NOT_APPLICABLE, peak NOT_APPLICABLE
    assert by_group['eotf_tracking'].status == 'NOT_APPLICABLE'
    assert by_group['peak_luminance'].status == 'NOT_APPLICABLE'
    assert evaluation.evaluation_id.startswith('vce-')
    assert evaluation.metric_family == 'dE2000'
    assert evaluation.metric_version == 'ciede2000-1'
    assert color_evaluation_status(evaluation) == 'PASS'


def test_evaluation_flags_off_target_gamut():
    samples = (
        _sample('white_100', 0.3127, 0.3290, 120.0),
        _sample('r', 0.60, 0.30, 25.0),  # badly off-red
        _sample('g', 0.300, 0.600, 80.0),
        _sample('b', 0.150, 0.060, 6.0),
    )
    evaluation = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(samples=samples),
    )
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['gamut'] == 'FAIL'


def test_meter_floor_samples_excluded_not_failed():
    samples = (
        _sample('white_100', 0.3127, 0.3290, 120.0),
        _sample('gray_5', 0.40, 0.45, 0.02, at_meter_floor=True),
    )
    evaluation = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(samples=samples),
    )
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['grayscale'] == 'UNKNOWN'  # only meter-floor data
    assert by_group['white_point'] == 'PASS'


def test_unconstrained_group_not_applicable():
    target = build_video_color_target_profile(
        target_id='wp-only',
        version='1',
        eotf='gamma_2_4',
        white_point=ChromaticityPoint(x=0.3127, y=0.3290),
        tolerances=ColorTolerances(white_point_delta_e=2.0),
    )
    evaluation = evaluate_video_color(
        target=target, measurement_set=_measurement()
    )
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['gamut'] == 'NOT_APPLICABLE'
    assert by_group['grayscale'] == 'NOT_APPLICABLE'


def test_different_metric_family_produces_different_hash():
    a = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(),
        metric_family='dE2000',
    )
    b = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(),
        metric_family='dEuv1976',
    )
    assert a.evaluation_sha256 != b.evaluation_sha256
    assert a.metric_version == 'ciede2000-1'
    assert b.metric_version == 'cie1976-uv-1'


def test_unimplemented_metric_fails_closed():
    # #1018: dEICtCp has no verified implementation — reject, never relabel.
    with pytest.raises(ValueError, match='no implemented algorithm'):
        evaluate_video_color(
            target=_target(),
            measurement_set=_measurement(),
            metric_family='dEICtCp',
        )
    with pytest.raises(ValueError, match='not implemented'):
        evaluate_video_color(
            target=_target(),
            measurement_set=_measurement(),
            metric_family='dE2000',
            metric_version='htdt-xy-screen-1',
        )


def test_other_family_uses_legacy_xy_metric():
    evaluation = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(),
        metric_family='other',
    )
    assert evaluation.metric_version == 'xy_euclidean_scaled_v1'
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['white_point'] == 'PASS'


def test_tolerance_bound_to_other_metric_gates_groups():
    # #1018: tolerances authored for one metric cannot be silently
    # consumed under another.
    target = build_video_color_target_profile(
        target_id='bound-tol',
        version='1',
        eotf='gamma_2_4',
        white_point=ChromaticityPoint(x=0.3127, y=0.3290),
        tolerances=ColorTolerances(
            white_point_delta_e=2.0,
            metric_family='dE2000',
            metric_version='ciede2000-1',
        ),
        provenance=_provenance(),
    )
    ok = evaluate_video_color(
        target=target,
        measurement_set=_measurement(),
        metric_family='dE2000',
    )
    by_group = {g.group: g.status for g in ok.groups}
    assert by_group['white_point'] == 'PASS'
    mismatched = evaluate_video_color(
        target=target,
        measurement_set=_measurement(),
        metric_family='dEuv1976',
    )
    by_group = {g.group: g.status for g in mismatched.groups}
    assert by_group['white_point'] == 'UNKNOWN'
    assert color_evaluation_status(mismatched) != 'PASS'


def test_ciede2000_reference_vectors():
    # Sharma et al. (2005) reference pairs — the implementation must
    # reproduce the published numbers exactly.
    from htdt.cad_colorimetry import _delta_e_2000_lab

    for lab1, lab2, expected in (
        ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
        ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
        ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
        ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1795),
        ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
        ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
        ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ):
        # symmetric pairs land within ~3e-4 of the published rounded
        # values depending on float evaluation order
        assert _delta_e_2000_lab(lab1, lab2) == pytest.approx(
            expected, abs=0.0005
        )


def test_eotf_tracking_group():
    # #1019: gamma-2.4 tracking evaluated from explicit stimulus_level.
    peak = 100.0
    samples = tuple(
        _sample(
            f'eotf_{int(level * 100)}',
            0.3127,
            0.3290,
            peak * level ** 2.4,
            stimulus_level=level,
        )
        for level in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    )
    target = build_video_color_target_profile(
        target_id='gamma-24',
        version='1',
        eotf='gamma_2_4',
        stimulus_levels=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        tolerances=ColorTolerances(eotf_deviation_fraction=0.05),
        provenance=_provenance(),
    )
    evaluation = evaluate_video_color(
        target=target, measurement_set=_measurement(samples=samples)
    )
    by_group = {g.group: g for g in evaluation.groups}
    assert by_group['eotf_tracking'].status == 'PASS'
    assert by_group['eotf_tracking'].worst_delta_e == pytest.approx(0.0)

    # gamma-2.2 tracking data against a 2.4 target must FAIL.
    bad_samples = tuple(
        _sample(
            f'eotf_{int(level * 100)}',
            0.3127,
            0.3290,
            peak * level ** 2.2,
            stimulus_level=level,
        )
        for level in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    )
    bad = evaluate_video_color(
        target=target, measurement_set=_measurement(samples=bad_samples)
    )
    by_group = {g.group: g.status for g in bad.groups}
    assert by_group['eotf_tracking'] == 'FAIL'


def test_eotf_missing_required_levels_is_unknown():
    # #1019: coverage accounting — a declared level with no sample cannot
    # fold into PASS.
    samples = (
        _sample('eotf_50', 0.3127, 0.3290, 18.9, stimulus_level=0.5),
    )
    target = build_video_color_target_profile(
        target_id='gamma-24-cov',
        version='1',
        eotf='gamma_2_4',
        stimulus_levels=(0.0, 0.5, 1.0),
        tolerances=ColorTolerances(eotf_deviation_fraction=0.1),
        provenance=_provenance(),
    )
    evaluation = evaluate_video_color(
        target=target, measurement_set=_measurement(samples=samples)
    )
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['eotf_tracking'] == 'UNKNOWN'
    # and a configured-but-unmeasured criterion keeps the fold honest
    assert color_evaluation_status(evaluation) != 'PASS'


def test_eotf_samples_without_level_semantics_unknown():
    # #1019: levels must come from stimulus_level, never guessed from ids.
    target = build_video_color_target_profile(
        target_id='gamma-24-nolvl',
        version='1',
        eotf='gamma_2_4',
        tolerances=ColorTolerances(eotf_deviation_fraction=0.1),
        provenance=_provenance(),
    )
    evaluation = evaluate_video_color(
        target=target, measurement_set=_measurement()
    )
    by_group = {g.group: g for g in evaluation.groups}
    assert by_group['eotf_tracking'].status == 'UNKNOWN'
    assert 'stimulus_level' in (by_group['eotf_tracking'].note or '')


def test_peak_luminance_group():
    # #1019: declared peak is evaluated, not descriptive metadata.
    samples = (
        _sample('white_100', 0.3127, 0.3290, 118.0),
    )
    target = build_video_color_target_profile(
        target_id='hdr-peak',
        version='1',
        eotf='pq_st2084',
        peak_luminance_cd_m2=120.0,
        tolerances=ColorTolerances(
            peak_luminance_tolerance_fraction=0.10
        ),
        provenance=_provenance(),
    )
    evaluation = evaluate_video_color(
        target=target, measurement_set=_measurement(samples=samples)
    )
    by_group = {g.group: g.status for g in evaluation.groups}
    assert by_group['peak_luminance'] == 'PASS'

    # declared peak without tolerance → visibly UNKNOWN, never ignored
    unconstrained = build_video_color_target_profile(
        target_id='hdr-peak-notol',
        version='1',
        eotf='pq_st2084',
        peak_luminance_cd_m2=120.0,
        provenance=_provenance(),
    )
    result = evaluate_video_color(
        target=unconstrained,
        measurement_set=_measurement(samples=samples),
    )
    by_group = {g.group: g.status for g in result.groups}
    assert by_group['peak_luminance'] == 'UNKNOWN'
