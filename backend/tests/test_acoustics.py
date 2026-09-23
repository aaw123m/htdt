import math

from htdt.acoustics import analyze_rectangular_context, first_order_reflections, rectangular_room_modes


def test_cube_first_axial_mode() -> None:
    modes = rectangular_room_modes(5.0, 5.0, 5.0, max_hz=40.0, sound_speed_m_s=343.0)
    axial = next(mode for mode in modes if (mode.n_x, mode.n_y, mode.n_z) == (1, 0, 0))
    assert axial.mode_class == 'axial'
    assert math.isclose(axial.frequency_hz, 34.3, abs_tol=1e-12)


def test_first_order_front_reflection_geometry() -> None:
    results = first_order_reflections(
        4.0,
        5.0,
        2.4,
        (1.0, 1.0, 1.0),
        (3.0, 3.0, 1.0),
        speaker_id='FL',
        speaker_role='front_left',
    )
    assert len(results) == 6
    front = next(result for result in results if result.surface == 'front_y0')
    assert all(math.isclose(value, expected, abs_tol=1e-12) for value, expected in zip(front.reflection_point_m, (1.5, 0.0, 1.0), strict=True))
    assert front.reflected_length_m > front.direct_length_m
    assert front.excess_delay_ms > 0
    assert front.first_destructive_hz is not None


def test_context_analysis_labels_geometry_as_prediction() -> None:
    result = analyze_rectangular_context({
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4, 'geometry_kind': 'rectangular'},
        'measurement_point': {'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'C', 'role': 'front_center', 'position': None},
        ],
    }, max_hz=100.0)
    assert result['classification'] == 'predicted_geometry_candidate'
    assert result['room_modes']
    assert len(result['first_order_reflections']) == 6
    assert result['skipped_speaker_ids'] == ['C']
