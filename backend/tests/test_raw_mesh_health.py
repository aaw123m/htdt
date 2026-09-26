from __future__ import annotations

import pytest

from htdt.raw_mesh import (
    diagnose_raw_visual_mesh,
    import_raw_visual_mesh,
)
from htdt.raw_mesh_health import (
    build_mesh_health_summary,
    classify_repair_operation,
    mesh_component_inventory,
)

NOW = '2026-09-24T00:00:00+00:00'

# closed tetrahedron -> watertight
TETRA_OBJ = b"""v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 2 3 4
f 1 4 3
"""

# single triangle -> open boundary on every edge
TRIANGLE_OBJ = b"""v 0 0 0
v 1 0 0
v 0 1 0
f 1 2 3
"""

# two disconnected triangles -> 2 components
TWO_PARTS_OBJ = b"""v 0 0 0
v 1 0 0
v 0 1 0
v 5 5 5
v 6 5 5
v 5 6 5
f 1 2 3
f 4 5 6
"""


def _import(asset: bytes, name: str):
    return import_raw_visual_mesh(asset, source_name=name)


def test_summary_on_watertight_mesh_has_no_opaque_score() -> None:
    mesh = _import(TETRA_OBJ, 'tetra.obj')
    diagnostics = diagnose_raw_visual_mesh(mesh)
    summary = build_mesh_health_summary(
        diagnostics, mesh=mesh, created_at_utc=NOW
    )
    assert summary.issues == ()  # all findings pass -> no issue rows
    states = {row.target: row.state for row in summary.readiness}
    assert states['visual_mesh'] == 'ready'
    assert states['wave_closed_volume'] == 'ready'
    assert states['general_3d_production'] == 'not_validated'
    assert summary.component_count == 1


def test_open_mesh_reports_blocker_with_impact_and_action() -> None:
    mesh = _import(TRIANGLE_OBJ, 'tri.obj')
    diagnostics = diagnose_raw_visual_mesh(mesh)
    summary = build_mesh_health_summary(
        diagnostics, mesh=mesh, created_at_utc=NOW
    )
    by_code = {issue.code: issue for issue in summary.issues}
    assert 'open_boundary' in by_code
    issue = by_code['open_boundary']
    assert issue.category == 'topology'
    assert issue.severity == 'blocker'
    assert issue.count == 3
    assert 'wave' in issue.impact or 'closed' in issue.impact
    assert issue.action
    watertight = by_code['watertightness']
    assert watertight.category == 'acoustic_model'
    assert watertight.severity == 'blocker'
    states = {row.target: row.state for row in summary.readiness}
    assert states['wave_closed_volume'] == 'blocked'
    wave_row = next(
        row for row in summary.readiness if row.target == 'wave_closed_volume'
    )
    assert 'open_boundary' in wave_row.blocking_codes


def test_component_inventory_never_auto_discards() -> None:
    mesh = _import(TWO_PARTS_OBJ, 'two.obj')
    inventory = mesh_component_inventory(mesh)
    assert len(inventory.components) == 2
    assert all(c.face_count == 1 for c in inventory.components)
    assert inventory.components[0].surface_area_m2 == pytest.approx(0.5)


def test_summary_is_deterministic() -> None:
    mesh = _import(TRIANGLE_OBJ, 'tri.obj')
    diagnostics = diagnose_raw_visual_mesh(mesh)
    first = build_mesh_health_summary(diagnostics, created_at_utc=NOW)
    second = build_mesh_health_summary(diagnostics, created_at_utc=NOW)
    assert first.summary_sha256 == second.summary_sha256


def test_repair_risk_taxonomy() -> None:
    assert (
        classify_repair_operation('remove_unreferenced_vertices')
        == 'A_deterministic'
    )
    assert classify_repair_operation('tolerance_vertex_weld') == 'B_bounded'
    assert classify_repair_operation('fill_hole') == 'B_bounded'
    assert (
        classify_repair_operation('non_manifold_surgery') == 'unsupported'
    )
    assert (
        classify_repair_operation('acoustic_room_inference') == 'C_semantic'
    )
    assert classify_repair_operation('invented_op') == 'unsupported'


def test_ga_limited_but_visual_ready_on_open_mesh() -> None:
    mesh = _import(TRIANGLE_OBJ, 'tri.obj')
    diagnostics = diagnose_raw_visual_mesh(mesh)
    summary = build_mesh_health_summary(diagnostics, created_at_utc=NOW)
    ga = next(
        row for row in summary.readiness if row.target == 'ga_direct_early'
    )
    assert ga.state in ('ready_with_limitations', 'blocked')
    visual = next(
        row for row in summary.readiness if row.target == 'visual_mesh'
    )
    assert visual.state == 'ready'
    production = next(
        row
        for row in summary.readiness
        if row.target == 'general_3d_production'
    )
    assert production.state == 'not_validated'
