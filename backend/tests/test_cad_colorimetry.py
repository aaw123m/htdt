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
    assert evaluation.evaluation_id.startswith('vce-')
    assert evaluation.metric_family == 'dE2000'
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
        metric_version='htdt-xy-screen-1',
    )
    b = evaluate_video_color(
        target=_target(),
        measurement_set=_measurement(),
        metric_family='dEICtCp',
        metric_version='htdt-xy-screen-1',
    )
    assert a.evaluation_sha256 != b.evaluation_sha256
