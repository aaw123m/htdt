"""Round-8 regression: format_measure_result honours the interactive
LengthDisplayPolicy (round-6 D3) while keeping SI when no policy is given;
RoomMeasurePanel reads the live provider for both the label and clipboard."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.cad_display_units import display_length_policy
from htdt.cad_measure import (
    MeasureEndpoint,
    build_distance_result,
    build_angle_result,
    format_measure_result,
)
from htdt.cad_scene import Position3


def _point(x: float, y: float, z: float) -> MeasureEndpoint:
    return MeasureEndpoint(
        position=Position3(x_m=x, y_m=y, z_m=z),
        entity_id=None,
        entity_name=None,
        reference_kind="free_point",
        reference_label="自由点",
    )


@pytest.fixture()
def distance_result():
    return build_distance_result(_point(0, 0, 0), _point(1.5, 2.0, 0.25))


def test_si_default_is_unchanged(distance_result) -> None:
    text = format_measure_result(distance_result)
    assert "2.500 m" in text  # distance
    assert "ΔX +1.500" in text
    assert "水平 2.500 m" in text
    # Byte-identical with an explicit metres policy at default precision.
    assert format_measure_result(
        distance_result, policy=display_length_policy("m")
    ) == text


def test_mm_policy_converts_lengths(distance_result) -> None:
    text = format_measure_result(
        distance_result, policy=display_length_policy("mm")
    )
    assert "2512.5 mm" in text  # distance sqrt(1.5²+2²+0.25²)
    assert "ΔX +1500.0" in text
    assert "水平 2500.0 mm" in text
    # Angles stay degrees — the length policy never governs them.
    assert "方位" in text and "°" in text


def test_inch_policy_uses_in_suffix(distance_result) -> None:
    text = format_measure_result(
        distance_result, policy=display_length_policy("inch")
    )
    assert "in" in text and " mm" not in text


def test_angle_result_ignores_policy() -> None:
    result = build_angle_result(
        _point(1, 0, 0), _point(0, 0, 0), _point(0, 1, 0)
    )
    assert format_measure_result(
        result, policy=display_length_policy("mm")
    ) == format_measure_result(result)


def test_panel_follows_live_policy_provider() -> None:
    from htdt.room_measure_input import RoomMeasurePanel

    app = QApplication.instance() or QApplication([])

    class _Controller:
        class _Signal:
            def connect(self, *_a):  # noqa: D102
                return None

        is_active = False
        result = None
        measurementChanged = _Signal()
        stateChanged = _Signal()

        def begin(self) -> None:
            return None

        def cancel(self) -> None:
            return None

        def set_mode(self, _mode) -> None:
            return None

        def set_reference_kind(self, _kind) -> None:
            return None

    controller = _Controller()
    policy = {"current": display_length_policy("m")}
    panel = RoomMeasurePanel(
        controller,
        display_policy_provider=lambda: policy["current"],
    )
    try:
        result = build_distance_result(_point(0, 0, 0), _point(0.012, 0, 0))
        panel._on_result(result)
        assert "0.012 m" in panel.result_label.text()
        # A later preference commit is picked up without re-construction.
        policy["current"] = display_length_policy("mm")
        panel._on_result(result)
        assert "12.0 mm" in panel.result_label.text()
    finally:
        panel.close()
        panel.deleteLater()
