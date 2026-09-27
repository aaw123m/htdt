"""Ruler/measure-tool geometry authority (#491) — round 2 coverage.

``cad_measure`` is display-only math, but it feeds the UI with numbers the
user trusts (distances, azimuth, elevation, vertex angles). These tests pin
the canonical coordinate convention (+Y rear = 0° azimuth, +X right = +90°),
the degenerate-input behavior, and the result formatting contract.
"""

from __future__ import annotations

import pytest
from math import sqrt

from htdt.cad_measure import (
    MeasureEndpoint,
    build_angle_result,
    build_distance_result,
    format_measure_result,
    measure_angle_deg,
    measure_components,
)
from htdt.cad_scene import Position3


def _point(x: float, y: float, z: float) -> Position3:
    return Position3(x_m=x, y_m=y, z_m=z)


def _endpoint(
    x: float,
    y: float,
    z: float,
    *,
    entity_id: str | None = None,
    entity_name: str | None = None,
    reference_kind: str = 'free_point',
    reference_label: str = '自由点',
) -> MeasureEndpoint:
    return MeasureEndpoint(
        position=_point(x, y, z),
        entity_id=entity_id,
        entity_name=entity_name,
        reference_kind=reference_kind,
        reference_label=reference_label,
    )


def test_measure_components_axis_distances() -> None:
    result = measure_components(_point(0, 0, 0), _point(3, 4, 12))
    assert result.mode == 'distance'
    assert result.distance_m == pytest.approx(13.0)
    assert result.horizontal_m == pytest.approx(5.0)
    assert result.dx_m == pytest.approx(3.0)
    assert result.dy_m == pytest.approx(4.0)
    assert result.dz_m == pytest.approx(12.0)


def test_measure_components_negative_deltas_preserved() -> None:
    result = measure_components(_point(2, 2, 2), _point(0, 0, 0))
    assert result.distance_m == pytest.approx(sqrt(12.0))
    assert result.dx_m == pytest.approx(-2.0)
    assert result.dy_m == pytest.approx(-2.0)
    assert result.dz_m == pytest.approx(-2.0)


def test_measure_components_coincident_points_are_zero_not_nan() -> None:
    result = measure_components(_point(1, 1, 1), _point(1, 1, 1))
    assert result.distance_m == pytest.approx(0.0)
    assert result.horizontal_m == pytest.approx(0.0)
    # Degenerate direction must still yield finite, defined angles.
    assert result.azimuth_deg == pytest.approx(0.0)
    assert result.elevation_deg == pytest.approx(0.0)


@pytest.mark.parametrize(
    'end, expected_azimuth',
    (
        ((0, 1, 0), 0.0),      # +Y rear: canonical zero
        ((1, 0, 0), 90.0),     # +X right
        ((0, -1, 0), 180.0),   # -Y front (atan2 wraps to +180)
        ((-1, 0, 0), -90.0),   # -X left
        ((1, 1, 0), 45.0),
    ),
)
def test_measure_components_azimuth_convention(end, expected_azimuth) -> None:
    result = measure_components(_point(0, 0, 0), _point(*end))
    assert result.azimuth_deg == pytest.approx(expected_azimuth)


def test_measure_components_elevation_up_down() -> None:
    up = measure_components(_point(0, 0, 0), _point(0, 0, 2))
    assert up.elevation_deg == pytest.approx(90.0)
    down = measure_components(_point(0, 0, 0), _point(0, 0, -2))
    assert down.elevation_deg == pytest.approx(-90.0)
    diagonal = measure_components(_point(0, 0, 0), _point(0, 1, 1))
    assert diagonal.elevation_deg == pytest.approx(45.0)


def test_measure_angle_right_straight_and_zero() -> None:
    vertex = _point(0, 0, 0)
    assert measure_angle_deg(_point(1, 0, 0), vertex, _point(0, 1, 0)) == pytest.approx(90.0)
    assert measure_angle_deg(_point(1, 0, 0), vertex, _point(-1, 0, 0)) == pytest.approx(180.0)
    assert measure_angle_deg(_point(1, 0, 0), vertex, _point(2, 0, 0)) == pytest.approx(0.0)


def test_measure_angle_degenerate_endpoint_raises() -> None:
    vertex = _point(0, 0, 0)
    with pytest.raises(ValueError, match='distinct endpoints'):
        measure_angle_deg(vertex, vertex, _point(1, 0, 0))
    with pytest.raises(ValueError, match='distinct endpoints'):
        measure_angle_deg(_point(1, 0, 0), vertex, vertex)


def test_measure_angle_uses_3d_vectors_not_plan_projection() -> None:
    vertex = _point(0, 0, 0)
    # A is up, B is horizontal: 90° even though their XY projections coincide.
    assert measure_angle_deg(_point(0, 0, 1), vertex, _point(1, 0, 0)) == pytest.approx(90.0)


def test_build_distance_result_carries_endpoints() -> None:
    a = _endpoint(0, 0, 0, entity_id='e1', entity_name='MLP', reference_kind='position', reference_label='基準位置（本体原点）')
    b = _endpoint(1, 0, 0, entity_id='e2', entity_name='Speaker', reference_kind='snap_point', reference_label='スナップ点')
    result = build_distance_result(a, b)
    assert result.endpoints == (a, b)
    assert result.distance_m == pytest.approx(1.0)
    # The carried endpoints keep semantic provenance for the display.
    assert result.endpoints[0].reference_kind == 'position'


def test_build_angle_result_vertex_order() -> None:
    a = _endpoint(1, 0, 0)
    vertex = _endpoint(0, 0, 0)
    b = _endpoint(0, 1, 0)
    result = build_angle_result(a, vertex, b)
    assert result.mode == 'angle'
    assert result.endpoints == (a, vertex, b)
    assert result.angle_deg == pytest.approx(90.0)


def test_endpoint_describe_falls_back_to_free_point() -> None:
    named = _endpoint(0, 0, 0, entity_name='MLP', reference_label='スナップ点')
    assert named.describe() == 'MLP · スナップ点'
    anonymous = _endpoint(0, 0, 0)
    assert anonymous.describe() == '（自由点） · 自由点'


def test_format_measure_result_distance_includes_all_fields() -> None:
    result = build_distance_result(_endpoint(0, 0, 0), _endpoint(1, 0, 0))
    text = format_measure_result(result)
    assert text.startswith('距離 1.000 m')
    assert 'ΔX +1.000' in text
    assert '水平 1.000' in text
    assert '方位 +90.0°' in text


def test_format_measure_result_angle() -> None:
    result = build_angle_result(_endpoint(1, 0, 0), _endpoint(0, 0, 0), _endpoint(0, 1, 0))
    assert format_measure_result(result) == '角度 90.0°'


def test_format_measure_result_follows_display_length_policy() -> None:
    # Round8: the measure panel/copy text honors the #496 display-unit policy
    # instead of hardcoding SI metres.
    from htdt.cad_display_units import display_length_policy

    result = build_distance_result(_endpoint(0, 0, 0), _endpoint(1, 0, 0))
    text = format_measure_result(result, display_length_policy('mm'))
    assert text.startswith('距離 1000.0 mm')
    assert 'ΔX +1000.0 mm' in text
    assert '水平 1000.0 mm' in text
    # Angles stay degrees regardless of the length policy.
    assert '方位 +90.0°' in text


def test_measure_result_is_display_only_document() -> None:
    # MeasureResult carries no mutation hooks; endpoints tuple is immutable
    # ordering metadata only.
    a = _endpoint(0, 0, 0)
    b = _endpoint(1, 2, 2)
    result = build_distance_result(a, b)
    assert isinstance(result.endpoints, tuple)
    assert result.angle_deg is None
