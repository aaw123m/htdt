"""REV30: sub-millimetre feature-scale spec and fail-closed gating.

Pins the supported occluder feature regime (``MIN_SUPPORTED_FEATURE_M``):
both deterministic verdict entry points must refuse compiled geometry that
contains finer triangles instead of emitting an unreliable verdict, and the
point-in-triangle degenerate check must use the same ``tolerance²``
comparison the Portal module already proved out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_geometric_acoustics_adapter import (
    MIN_SUPPORTED_FEATURE_M,
    DeterministicGaUnsupportedError,
    _point_in_triangle,
    _point_in_triangle_prepared,
    _require_supported_feature_scale,
)
from htdt.r120_geometry_compiler import CompiledTriangle, CompiledVertex

from test_cad_geometric_acoustics_adapter import (  # noqa: E402  (shared fixtures)
    _execute,
    _fixture,
)
from test_cad_late_field_energy import _execute_late  # noqa: E402


def _compiled_with_tiny_triangle(fx, max_edge_m: float):
    """Return ``fx['compiled']`` plus one tiny out-of-band triangle.

    Identity fields (``compiled_geometry_id``/hashes) are preserved so the
    execution-input identity checks still pass and the feature-scale gate is
    the behaviour under test.
    """
    compiled = fx['compiled']
    base = len(compiled.vertices)
    # Vertex B sits exactly ``max_edge_m`` from A on one axis, and C's edges
    # stay shorter, so ``max_edge_m`` is the triangle's true maximum edge.
    vertices = compiled.vertices + (
        CompiledVertex(x_m=0.0, y_m=0.0, z_m=0.0),
        CompiledVertex(x_m=max_edge_m, y_m=0.0, z_m=0.0),
        CompiledVertex(x_m=max_edge_m / 2.0, y_m=max_edge_m / 10.0, z_m=0.0),
    )
    triangles = compiled.triangles + (
        CompiledTriangle(
            a=base,
            b=base + 1,
            c=base + 2,
            source_triangle_id='rev30-tiny-0',
            source_surface_id=compiled.triangles[0].source_surface_id,
        ),
    )
    return compiled.model_copy(
        update={'vertices': vertices, 'triangles': triangles}
    )


def _prepared(triangle):
    """``_IndexedOccluderRows``-equivalent prepared tuple for one triangle."""
    from math import sqrt

    vertex_a, vertex_b, vertex_c = triangle
    edge1 = tuple(vertex_b[i] - vertex_a[i] for i in range(3))
    edge2 = tuple(vertex_c[i] - vertex_a[i] for i in range(3))
    edge1_sq = sum(component * component for component in edge1)
    edge2_sq = sum(component * component for component in edge2)
    normal = (
        edge1[1] * edge2[2] - edge1[2] * edge2[1],
        edge1[2] * edge2[0] - edge1[0] * edge2[2],
        edge1[0] * edge2[1] - edge1[1] * edge2[0],
    )
    normal_sq = sum(component * component for component in normal)
    return (
        edge1,
        edge2,
        sqrt(edge1_sq),
        sqrt(edge2_sq),
        edge1_sq,
        edge2_sq,
        normal,
        sqrt(normal_sq),
    )


def test_feature_scale_gate_accepts_fixture_geometry(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    # The shared fixture compiles a metre-scale shoebox — well inside the
    # supported regime, so the gate must not fire.
    assert _require_supported_feature_scale(fx['compiled']) is None


def test_feature_scale_gate_rejects_sub_supported_triangle(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    compiled = _compiled_with_tiny_triangle(fx, MIN_SUPPORTED_FEATURE_M / 2)
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _require_supported_feature_scale(compiled)
    assert error.value.reason_code == 'UNSUPPORTED_GEOMETRY'
    assert 'supported minimum feature scale' in str(error.value)


def test_feature_scale_gate_boundary_is_inclusive(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    # Exactly at the supported scale the gate passes; anything below fails.
    at_limit = _compiled_with_tiny_triangle(fx, MIN_SUPPORTED_FEATURE_M)
    assert _require_supported_feature_scale(at_limit) is None
    just_below = _compiled_with_tiny_triangle(fx, MIN_SUPPORTED_FEATURE_M - 1e-9)
    with pytest.raises(DeterministicGaUnsupportedError):
        _require_supported_feature_scale(just_below)


def test_deterministic_ga_fails_closed_on_sub_supported_triangle(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    fx['compiled'] = _compiled_with_tiny_triangle(fx, 1.0e-3)
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _execute(fx)
    assert error.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_late_field_energy_fails_closed_on_sub_supported_triangle(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    fx['compiled'] = _compiled_with_tiny_triangle(fx, 1.0e-3)
    with pytest.raises(DeterministicGaUnsupportedError) as error:
        _execute_late(fx)
    assert error.value.reason_code == 'UNSUPPORTED_GEOMETRY'


def test_point_in_triangle_honours_supported_scale_triangles() -> None:
    # A 5 mm right triangle (area ~12.5 mm²) is inside the supported regime
    # but has a barycentric denominator of ~6.25e-10 m⁴ — below the bare
    # 1e-9 tolerance the degenerate check used to compare against, so the
    # centroid was previously reported as outside.
    triangle = (
        (0.0, 0.0, 0.0),
        (5.0e-3, 0.0, 0.0),
        (0.0, 5.0e-3, 0.0),
    )
    centroid = (5.0e-3 / 3.0, 5.0e-3 / 3.0, 0.0)
    outside = (5.0e-3, 5.0e-3, 0.0)
    tolerance = 1.0e-9
    assert _point_in_triangle(centroid, triangle, tolerance=tolerance) is True
    assert _point_in_triangle(outside, triangle, tolerance=tolerance) is False
    prepared = _prepared(triangle)
    assert (
        _point_in_triangle_prepared(
            centroid, triangle[0], prepared, tolerance=tolerance
        )
        is True
    )
    assert (
        _point_in_triangle_prepared(
            outside, triangle[0], prepared, tolerance=tolerance
        )
        is False
    )


def test_point_in_triangle_still_rejects_degenerate_triangles() -> None:
    # A collapsed (zero-area) triangle must still be treated as degenerate —
    # the squared comparison widened the cutoff, it did not remove it.
    triangle = (
        (0.0, 0.0, 0.0),
        (1.0e-9, 0.0, 0.0),
        (0.0, 1.0e-9, 0.0),
    )
    centroid = (1.0e-9 / 3.0, 1.0e-9 / 3.0, 0.0)
    tolerance = 1.0e-9
    assert _point_in_triangle(centroid, triangle, tolerance=tolerance) is False
    assert (
        _point_in_triangle_prepared(
            centroid, triangle[0], _prepared(triangle), tolerance=tolerance
        )
        is False
    )
