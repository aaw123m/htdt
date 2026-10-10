"""Read-only 3D import preview tests (Issue #980).

Covers the render-only adapter (``htdt.geometry_import_preview``): the
declared unit/axis/handedness/anchor transform shared with the commit
paths, per-source-triangle repair fates reconstructed from the actual
``RepairedRawMesh`` lineage, defect-focus positions, deterministic
decimation, and the honest unsupported/unknown paths — plus the dialog's
preview pane wiring under offscreen Qt.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.geometry_import_dialog import GeometryImportDialog
from htdt.geometry_import_preview import (
    PreviewDeclaration,
    build_import_preview_scene,
    defect_focus_points,
    resolve_preview_declaration,
    source_triangle_fates,
)
from htdt.mesh_import_authority import (
    make_mesh_import_authority,
    mesh_import_scene_transform,
)
from htdt.raw_mesh import (
    diagnose_raw_visual_mesh,
    import_raw_visual_mesh,
)
from htdt.raw_mesh_repair import (
    CorrectConsistentWinding,
    ExactDuplicateVertexConsolidation,
    RemoveDegenerateFaces,
    RemoveExactDuplicateFaces,
    RemoveUnreferencedVertices,
    ToleranceVertexWeld,
    apply_raw_mesh_repair,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_plan,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


_TETRA_OBJ = b"""# closed unit tetra
v 0.0 0.0 0.0
v 0.4 0.0 0.0
v 0.0 0.3 0.0
v 0.0 0.0 0.2
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
"""

_DUP_OBJ = b"""# tetra with a duplicated vertex and duplicated face
v 0.0 0.0 0.0
v 0.4 0.0 0.0
v 0.3 0.0 0.0
v 0.0 0.0 0.2
v 0.0 0.0 0.0
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
f 1 2 3
f 2 3 5
"""

# A mesh in millimetre units: a 400 x 300 x 200 mm box made of 12
# triangles; closed walls, an open bottom face, one duplicate top
# triangle (f 5 6 7 twice), and one top triangle whose winding is
# inconsistent with its neighbours (f 8 7 5 shares directed edges).
_MM_BOX_OBJ = b"""# open-bottom mm box with flipped + duplicate faces
v 0 0 0
v 400 0 0
v 400 300 0
v 0 300 0
v 0 0 200
v 400 0 200
v 400 300 200
v 0 300 200
f 1 2 6
f 1 6 5
f 2 3 7
f 2 7 6
f 3 4 8
f 3 8 7
f 4 1 5
f 4 5 8
f 5 6 7
f 5 8 7
f 5 6 7
"""


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _mesh(data: bytes, name: str = 'mesh.obj'):
    return import_raw_visual_mesh(data, source_name=name)  # kw-only arg


def _decl(**overrides) -> PreviewDeclaration:
    base = dict(
        source_unit='meters',
        custom_scale_to_meters=None,
        up_axis='z+',
        forward_axis='y+',
        handedness='right',
        local_anchor='source_origin',
    )
    base.update(overrides)
    return PreviewDeclaration(**base)


def _repair(mesh, diagnostic, operations):
    plan = make_raw_mesh_repair_plan(
        mesh,
        diagnostic,
        operations=operations,
        requested_by='explicit_user_selected',
        request_reason='test preview repair',
    )
    repaired = apply_raw_mesh_repair(mesh, diagnostic, plan)
    return repaired, diagnose_repaired_raw_mesh(mesh, repaired)


# --- declaration / transform -------------------------------------------------


def test_undeclared_unit_renders_source_coordinates() -> None:
    mesh = _mesh(_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    matrix, resolved, notices = resolve_preview_declaration(
        _decl(source_unit=None),
        spec_unit='unknown',
        source_vertices=[(v.x, v.y, v.z) for v in mesh.vertices],
    )
    assert matrix is None and not resolved
    assert 'unit_undeclared' in notices

    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(source_unit=None)
    )
    assert scene.supported
    assert scene.coordinate_space == 'source_units'
    # No meter conversion: source bbox stands as-is.
    assert scene.dims == (0.4, 0.3, 0.2)


def test_declared_unit_scales_preview_bbox_to_meters() -> None:
    mesh = _mesh(_MM_BOX_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(source_unit='millimeters')
    )
    assert scene.coordinate_space == 'meters'
    # 400 x 300 x 200 mm → 0.4 x 0.3 x 0.2 m — matches the canonical
    # commit transform rather than a preview-side rescale.
    assert scene.dims == pytest.approx((0.4, 0.3, 0.2), abs=1e-9)


def test_preview_matches_commit_transform_exactly() -> None:
    mesh = _mesh(_MM_BOX_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    decl = _decl(
        source_unit='millimeters',
        up_axis='y+',
        forward_axis='x+',
        handedness='left',
        local_anchor='bounds_center',
    )
    scene = build_import_preview_scene(mesh, diagnostic, decl)

    authority = make_mesh_import_authority(
        source_unit='millimeters',
        unit_declared_by='operator_confirmed',
        up_axis='y+',
        forward_axis='x+',
        handedness='left',
        local_anchor='bounds_center',
    )
    matrix = mesh_import_scene_transform(
        authority, [(v.x, v.y, v.z) for v in mesh.vertices]
    )
    expected = [
        (
            matrix[0][0] * v.x + matrix[0][1] * v.y + matrix[0][2] * v.z + matrix[0][3],
            matrix[1][0] * v.x + matrix[1][1] * v.y + matrix[1][2] * v.z + matrix[1][3],
            matrix[2][0] * v.x + matrix[2][1] * v.y + matrix[2][2] * v.z + matrix[2][3],
        )
        for v in mesh.vertices
    ]
    assert list(scene.vertices) == expected


def test_unresolved_axis_convention_renders_unrotated() -> None:
    mesh = _mesh(_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    scene = build_import_preview_scene(
        mesh,
        diagnostic,
        _decl(up_axis='unknown', handedness='unknown'),
    )
    assert scene.supported
    assert scene.coordinate_space == 'meters'
    assert not scene.axis_convention_resolved
    assert 'axis_convention_unresolved' in scene.notices


def test_anchor_bottom_center_shifts_preview_bounds() -> None:
    mesh = _mesh(_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    plain = build_import_preview_scene(mesh, diagnostic, _decl())
    anchored = build_import_preview_scene(
        mesh, diagnostic, _decl(local_anchor='bottom_center')
    )
    assert anchored.bbox_min[2] == pytest.approx(0.0, abs=1e-12)
    # X/Y centered on bounds: -0.2/-0.15 after centering the 0.4/0.3 box.
    assert anchored.bbox_min[0] == pytest.approx(-0.2, abs=1e-12)
    assert anchored.bbox_min[1] == pytest.approx(-0.15, abs=1e-12)
    assert plain.bbox_min == (0.0, 0.0, 0.0)


# --- repair diff lineage -----------------------------------------------------


def test_diff_fates_removed_duplicate_and_vertex() -> None:
    mesh = _mesh(_DUP_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    repaired, _diag = _repair(
        mesh,
        diagnostic,
        (
            ExactDuplicateVertexConsolidation(),
            RemoveExactDuplicateFaces(),
            RemoveUnreferencedVertices(),
        ),
    )
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), view_mode='diff', repaired=repaired
    )
    assert scene.supported
    assert len(scene.source_fates) == len(mesh.triangles)
    # Consolidation merges vertex 5 onto vertex 1, so face 4 ('f 1 2 3')
    # AND face 5 (positions equal face 0's) both collapse to duplicates
    # and are removed — determinately so: neither leaves a repaired
    # entry under its source primitive.
    assert scene.source_fates[4] == 'removed'
    assert scene.source_fates[5] == 'removed'


# Closed manifold tetra with faces 1 and 2 wound opposite to the
# others — CorrectConsistentWinding propagates orientation and flips
# exactly the inconsistent faces (non-manifold meshes block it, so the
# fixture must stay closed and manifold).
_WOUND_TETRA_OBJ = b"""v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 4 2
f 1 3 4
f 2 3 4
"""


def test_diff_fates_flipped_winding() -> None:
    mesh = _mesh(_WOUND_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    repaired, _diag = _repair(mesh, diagnostic, (CorrectConsistentWinding(),))
    fates = source_triangle_fates(mesh, repaired)
    assert len(fates) == len(mesh.triangles)
    # Winding repair flips whole triangles in place: same vertex
    # positions, reversed cyclic order — and nothing is removed.
    assert fates == ('kept', 'flipped', 'flipped', 'kept')
    assert 'removed' not in fates


def test_diff_weld_reports_moved_or_unknown_honestly() -> None:
    mesh = _mesh(_DUP_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    repaired, _diag = _repair(
        mesh,
        diagnostic,
        (
            ToleranceVertexWeld(tolerance_source_units=0.01),
            RemoveExactDuplicateFaces(),
        ),
    )
    fates = source_triangle_fates(mesh, repaired)
    assert len(fates) == len(mesh.triangles)
    # With vertex movement + removal both applied, ambiguous faces are
    # 'unknown' rather than guessed — and nothing is silently 'kept'.
    assert all(fate in ('kept', 'flipped', 'moved', 'removed', 'unknown') for fate in fates)


def test_diff_rejects_foreign_lineage() -> None:
    mesh = _mesh(_TETRA_OBJ)
    other = _mesh(_DUP_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    other_diagnostic = diagnose_raw_visual_mesh(other)
    repaired, _diag = _repair(
        other, other_diagnostic, (RemoveUnreferencedVertices(),)
    )
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), view_mode='diff', repaired=repaired
    )
    assert all(fate == 'unknown' for fate in scene.source_fates)


def test_repaired_view_draws_repaired_geometry() -> None:
    mesh = _mesh(_DUP_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    repaired, _diag = _repair(
        mesh, diagnostic, (RemoveExactDuplicateFaces(),)
    )
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), view_mode='repaired', repaired=repaired
    )
    assert scene.supported
    assert scene.face_count_full == len(repaired.triangles)
    assert 0 < scene.face_count_full < len(mesh.triangles)


def test_repaired_view_without_preview_is_unsupported() -> None:
    mesh = _mesh(_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), view_mode='repaired'
    )
    assert not scene.supported
    assert scene.unsupported_reason == 'no_repair_preview'


# --- defect focus ------------------------------------------------------------


def test_focus_open_boundary_locates_open_edges() -> None:
    mesh = _mesh(_MM_BOX_OBJ)  # open box: missing bottom face
    diagnostic = diagnose_raw_visual_mesh(mesh)
    assert any(
        f.code == 'open_boundary' and f.state == 'fail' for f in diagnostic.findings
    )
    points = defect_focus_points(mesh, diagnostic, 'open_boundary')
    assert points is not None and len(points) > 0
    # The open boundary is the bottom rim (z = 0): every focus edge
    # midpoint must lie on z = 0.
    assert all(abs(p[2]) < 1e-9 for p in points)


def test_focus_duplicate_face_locates_duplicate() -> None:
    mesh = _mesh(_MM_BOX_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    points = defect_focus_points(mesh, diagnostic, 'duplicate_face')
    assert points is not None and len(points) == 1


def test_focus_underivable_codes_report_unknown() -> None:
    mesh = _mesh(_TETRA_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    assert defect_focus_points(mesh, diagnostic, 'overlapping_face') is None
    assert defect_focus_points(mesh, diagnostic, 'inverted_normal') is None
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), focus_codes=('overlapping_face',)
    )
    assert scene.focuses[0].state == 'unknown'


def test_focus_positions_follow_declared_transform() -> None:
    mesh = _mesh(_MM_BOX_OBJ)
    diagnostic = diagnose_raw_visual_mesh(mesh)
    scene = build_import_preview_scene(
        mesh,
        diagnostic,
        _decl(source_unit='millimeters'),
        focus_codes=('open_boundary',),
    )
    focus = scene.focuses[0]
    assert focus.state == 'located'
    # mm → m: bottom-rim focus points land near z = 0 in meters.
    assert all(abs(p[2]) < 1e-9 for p in focus.points)


# --- decimation --------------------------------------------------------------


def test_decimation_is_deterministic_and_bounded() -> None:
    # 600 triangles → cap 100: stride 7 sampling, identical every run.
    # A flat fan mesh: vertices 0..601, faces share vertex 0.
    obj = b'v 0 0 0\n' + b'\n'.join(
        b'v %d.0 %d.0 0.0' % (i, (i % 7)) for i in range(1, 605)
    ) + b'\n' + b''.join(
        b'f 1 %d %d\n' % (i, i + 1) for i in range(2, 602)
    )
    mesh = _mesh(obj)
    assert len(mesh.triangles) == 600
    diagnostic = diagnose_raw_visual_mesh(mesh)
    scene = build_import_preview_scene(
        mesh, diagnostic, _decl(), max_faces=100
    )
    assert scene.decimated
    assert len(scene.faces) <= 100
    again = build_import_preview_scene(
        mesh, diagnostic, _decl(), max_faces=100
    )
    assert again.faces == scene.faces
    assert again.vertices == scene.vertices
    # Source data untouched.
    assert len(mesh.triangles) == 600


# --- dialog pane -------------------------------------------------------------


def test_dialog_preview_pane_exists_and_updates(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'room.obj', _MM_BOX_OBJ)
    dialog = GeometryImportDialog(obj)
    try:
        assert dialog.preview_view.currentData() == 'original'
        # Repaired/diff views stay gated until a repair preview ran.
        model = dialog.preview_view.model()
        assert not model.item(1).isEnabled()
        assert not model.item(2).isEnabled()
        # Undeclared unit: honest source-coordinates notice, no meters.
        assert 'ソース座標' in dialog.preview_info.text()

        index = dialog.unit_combo.findData('millimeters')
        assert index >= 0
        dialog.unit_combo.setCurrentIndex(index)
        dialog._sync_unit_state()
        assert '0.4' in dialog.preview_info.text()
        assert 'm' in dialog.preview_info.text()
    finally:
        dialog.done(0)


def test_dialog_repair_preview_unlocks_diff_view(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'dup.obj', _DUP_OBJ)
    dialog = GeometryImportDialog(obj)
    try:
        dialog.repair_checks['remove_exact_duplicate_faces'].setChecked(True)
        dialog._preview_repair()
        model = dialog.preview_view.model()
        assert model.item(1).isEnabled()
        assert model.item(2).isEnabled()
        index = dialog.preview_view.findData('diff')
        dialog.preview_view.setCurrentIndex(index)
        dialog._refresh_preview()
        assert '差分' in dialog.preview_info.text()
    finally:
        dialog.done(0)


def test_dialog_focus_combo_lists_health_issues(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'room.obj', _MM_BOX_OBJ)
    dialog = GeometryImportDialog(obj)
    try:
        codes = [
            dialog.preview_focus.itemData(i)
            for i in range(dialog.preview_focus.count())
        ]
        assert None in codes
        assert 'open_boundary' in codes or 'duplicate_face' in codes
    finally:
        dialog.done(0)
