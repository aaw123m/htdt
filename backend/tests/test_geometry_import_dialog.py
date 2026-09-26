"""Guided geometry import UX tests (Issue #762 / current #35).

Covers the native wiring that was missing while the import backend was
already complete: the guided dialog (declaration → QA → bounded-repair
preview → destination), the declared-authority entity attach on the
controller, and the ``r120_semantic_geometry`` room commit as one undoable
``replace_document`` step.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from htdt.cad_document import EditStateError
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.geometry_import_dialog import GeometryImportDialog
from htdt.raw_mesh import import_raw_visual_mesh, diagnose_raw_visual_mesh
from htdt.raw_mesh_repair import (
    ExactDuplicateVertexConsolidation,
    apply_raw_mesh_repair,
    make_raw_mesh_repair_plan,
)
from htdt.room_workspace import RoomWorkspaceController
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    raw_triangle_ids,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


DOCUMENT_ID = 'geometry-import-fixture'

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
v 0.0 0.3 0.0
v 0.0 0.0 0.2
v 0.0 0.0 0.0
f 1 2 3
f 1 2 4
f 1 3 4
f 2 3 4
f 1 2 3
f 2 3 5
"""


def _furniture(
    entity_id: str = 'table',
    *,
    size: Size3 | None = None,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=entity_id,
        position=Position3(x_m=2.0, y_m=2.0, z_m=0.4),
        orientation=Quaternion4(),
        size_m=size or Size3(x_m=1.0, y_m=1.0, z_m=0.8),
    )


def _scene(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
        entities=entities,
    )


def _controller(tmp_path: Path, *entities: SceneEntity) -> RoomWorkspaceController:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(_scene(*entities), parent_revision_id=None)
    return RoomWorkspaceController(repository, DOCUMENT_ID)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _select_unit(dialog: GeometryImportDialog, unit: str) -> None:
    index = dialog.unit_combo.findData(unit)
    assert index >= 0
    dialog.unit_combo.setCurrentIndex(index)


# --- Dialog ------------------------------------------------------------------


def test_dialog_requires_operator_unit_declaration(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'room.obj', _TETRA_OBJ)
    dialog = GeometryImportDialog(obj)

    # OBJ is unitless: the declaration is unlocked and starts unset.
    assert dialog.unit_combo.isEnabled()
    assert dialog.unit_combo.currentData() is None
    assert dialog.diagnostic_summary.text()

    # Accepting without a declared unit is refused.
    dialog._accept()
    assert dialog.result() != QDialog.DialogCode.Accepted

    _select_unit(dialog, 'meters')
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted

    request = dialog.import_request()
    assert request.destination == 'room_geometry'
    assert request.source_unit == 'meters'
    assert request.repaired_mesh is None


def test_dialog_entity_destination_and_repair_preview(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'body.obj', _DUP_OBJ)
    dialog = GeometryImportDialog(obj, entity_target='table')

    assert dialog.entity_radio.isEnabled()
    assert dialog.entity_radio.isChecked()
    assert dialog.issues_table.rowCount() > 0
    assert not dialog.use_repaired.isEnabled()

    dialog.repair_checks['exact_duplicate_vertex_consolidation'].setChecked(True)
    dialog.repair_checks['remove_exact_duplicate_faces'].setChecked(True)
    dialog._preview_repair()

    assert dialog.use_repaired.isEnabled()
    assert dialog.use_repaired.isChecked()
    assert dialog._repaired_mesh is not None
    assert dialog._repaired_mesh.vertex_count_after < dialog._repaired_mesh.vertex_count_before
    assert dialog._repaired_mesh.triangle_count_after < dialog._repaired_mesh.triangle_count_before

    _select_unit(dialog, 'millimeters')
    request = dialog.import_request()
    assert request.destination == 'entity_body'
    assert request.source_unit == 'millimeters'
    assert request.repaired_mesh is not None
    assert request.repaired_diagnostic is not None


def test_dialog_room_destination_surface_assignment(tmp_path: Path) -> None:
    _app()
    obj = _write(tmp_path, 'room.obj', _TETRA_OBJ)
    dialog = GeometryImportDialog(obj)
    _select_unit(dialog, 'meters')
    dialog.room_radio.setChecked(True)

    # Whole-mesh surface classification feeds the conversion request.
    index = dialog.surface_class.findData('room_boundary')
    assert index >= 0
    dialog.surface_class.setCurrentIndex(index)
    geometry = dialog._evaluate_readiness()
    assert geometry is not None
    assert geometry.geometry_compiler_readiness in (
        'ready_for_r120_geometry_compiler_contract',
        'blocked_by_geometry',
        'blocked_by_surface_semantics',
    )

    request = dialog.import_request()
    assert request.surface_assignments
    assert request.surface_assignments[0].semantic_class == 'room_boundary'


def test_dialog_rejects_unparseable_source(tmp_path: Path) -> None:
    _app()
    bad = _write(tmp_path, 'bad.obj', b'not a mesh\n')
    with pytest.raises(Exception):
        GeometryImportDialog(bad)


# --- Controller: declared-authority entity attach -----------------------------


def test_attach_mesh_asset_declared_records_operator_authority(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _furniture())
    obj = _write(tmp_path, 'body.obj', _TETRA_OBJ)

    entity = controller.attach_mesh_asset_declared(
        'table', obj, source_unit='meters'
    )
    mesh = entity.body_geometry.mesh
    assert entity.body_geometry.kind == 'mesh_asset'
    assert mesh.asset_sha256 == sha256(_TETRA_OBJ).hexdigest()
    assert mesh.import_authority.source_unit == 'meters'
    assert mesh.import_authority.unit_declared_by == 'operator_confirmed'

    # Undo restores the plain envelope body.
    controller.working.undo()
    assert controller.document.entity('table').body_geometry is None


def test_attach_mesh_asset_declared_scales_declared_units(tmp_path: Path) -> None:
    controller = _controller(
        tmp_path, _furniture(size=Size3(x_m=2.0, y_m=2.0, z_m=2.0))
    )
    # Same tetra authored in millimetres.
    mm_obj = _TETRA_OBJ.replace(b'0.4', b'400.0').replace(b'0.3', b'300.0').replace(
        b'0.2', b'200.0'
    )
    obj = _write(tmp_path, 'body_mm.obj', mm_obj)
    entity = controller.attach_mesh_asset_declared(
        'table', obj, source_unit='millimeters'
    )
    xs = [v.x_m for v in entity.body_geometry.mesh.vertices]
    assert max(xs) == pytest.approx(0.4)


def test_attach_mesh_asset_declared_rejects_oversize_then_adopts(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path, _furniture(size=Size3(x_m=0.1, y_m=0.1, z_m=0.1))
    )
    obj = _write(tmp_path, 'big.obj', _TETRA_OBJ)
    with pytest.raises(ValueError):
        controller.attach_mesh_asset_declared(
            'table', obj, source_unit='meters', oversize_decision='cancel'
        )
    entity = controller.attach_mesh_asset_declared(
        'table', obj, source_unit='meters', oversize_decision='adopt'
    )
    assert entity.size_m.x_m >= 0.4


def test_attach_mesh_asset_declared_uses_repaired_mesh(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _furniture())
    obj = _write(tmp_path, 'dup.obj', _DUP_OBJ)
    raw = import_raw_visual_mesh(_DUP_OBJ, source_name='dup.obj')
    diagnostics = diagnose_raw_visual_mesh(raw)
    plan = make_raw_mesh_repair_plan(
        raw,
        diagnostics,
        operations=(ExactDuplicateVertexConsolidation(),),
        requested_by='explicit_user_selected',
        request_reason='test',
    )
    repaired = apply_raw_mesh_repair(raw, diagnostics, plan)

    entity = controller.attach_mesh_asset_declared(
        'table', obj, source_unit='meters', repaired_mesh=repaired
    )
    assert len(entity.body_geometry.mesh.vertices) == repaired.vertex_count_after
    assert len(entity.body_geometry.mesh.vertices) == 4


# --- Controller: room acoustic geometry import --------------------------------


def _room_assignments(mesh, surface_class: str = 'room_boundary'):
    return (
        SurfaceSemanticAssignment(
            surface_key='imported',
            triangle_ids=raw_triangle_ids(mesh),
            semantic_class=surface_class,
        ),
    )


def test_import_room_mesh_geometry_commits_undoable_document(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _furniture())
    obj = _write(tmp_path, 'room.obj', _TETRA_OBJ)
    raw = import_raw_visual_mesh(_TETRA_OBJ, source_name='room.obj')

    assert controller.document.r120_semantic_geometry is None
    geometry = controller.import_room_mesh_geometry(
        obj,
        source_unit='meters',
        surface_assignments=_room_assignments(raw),
    )
    committed = controller.committed_document
    assert committed.schema_version >= 4
    assert committed.r120_semantic_geometry is not None
    assert (
        committed.r120_semantic_geometry.geometry_compiler_readiness
        == geometry.geometry_compiler_readiness
    )
    assert (
        geometry.source_to_scene_transform.provenance
        == 'explicit_import_metadata'
    )

    # The commit is a single undoable replace_document step.
    controller.working.undo()
    assert controller.committed_document.r120_semantic_geometry is None
    controller.working.redo()
    assert controller.committed_document.r120_semantic_geometry is not None


def test_import_room_mesh_geometry_unit_scale_in_transform(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    mm_obj = _TETRA_OBJ.replace(b'0.4', b'400.0').replace(b'0.3', b'300.0').replace(
        b'0.2', b'200.0'
    )
    obj = _write(tmp_path, 'room_mm.obj', mm_obj)
    geometry = controller.import_room_mesh_geometry(
        obj, source_unit='millimeters'
    )
    matrix = geometry.source_to_scene_transform.matrix_source_to_scene_m
    assert matrix[0][0] == pytest.approx(0.001)


def test_import_room_mesh_geometry_rejects_undeclared_unit(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    obj = _write(tmp_path, 'room.obj', _TETRA_OBJ)
    with pytest.raises((ValueError, EditStateError)):
        controller.import_room_mesh_geometry(
            obj, source_unit='unknown'
        )
