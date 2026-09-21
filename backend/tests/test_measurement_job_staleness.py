from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from htdt.cad_constraint_models import CadConstraintSet, CadWallClearanceConstraint
from htdt.cad_measurement_loop import CadMeasurementPlan
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID
from htdt.cad_search_models import constraint_workspace_snapshot
from htdt.measurement_editor import MeasurementEditorWindow
from htdt.optimization_workflow_controller import OptimizationWorkflowController


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _constraint_set(document_id: str) -> CadConstraintSet:
    return CadConstraintSet(
        document_id=document_id,
        constraints=(
            CadWallClearanceConstraint(
                constraint_id='clearance-1',
                name='壁離隔',
                entity_ids=('speaker-fl',),
                wall_id='wall-left',
                min_m=0.5,
            ),
        ),
    )


def _planned_measurement_plan(revision, *, plan_id: str = 'plan-1') -> CadMeasurementPlan:
    payload = {
        'document_id': revision.document_id,
        'search_spec_id': 'spec-1',
        'search_spec_sha256': 'a' * 64,
        'candidate_id': 'cand-1',
        'candidate_set_sha256': 'b' * 64,
        'applied_scene_revision_id': revision.revision_id,
        'applied_scene_content_hash': revision.content_hash,
        'status': 'planned',
        'measurement_ids': [],
    }
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    return CadMeasurementPlan(plan_id=plan_id, plan_sha256=digest, **payload)


def test_editor_rew_read_binds_current_constraint_workspace(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    window = MeasurementEditorWindow(repository, F1_DOCUMENT_ID)
    window.selected_id = 'point-mlp'
    window.rew_combo.addItem('REW A', 'rew-uuid-1')
    monkeypatch.setattr(window, '_start_rew_task', lambda *args, **kwargs: None)

    window.read_selected_rew_async()

    expected = constraint_workspace_snapshot(window.constraint_set)[1]
    token = next(iter(window._rew_tokens.values()))
    assert token.constraint_workspace_hash == expected
    context = window._current_job_apply_context()
    assert context is not None
    assert context.constraint_workspace_hash == expected
    assert window.rew_job_guard.can_apply(token, context)

    # Editing constraints while the external read is in flight stales the token.
    window.constraint_set = _constraint_set(window.document_id)
    stale = window._current_job_apply_context()
    assert stale is not None
    assert stale.constraint_workspace_hash != expected
    assert not window.rew_job_guard.can_apply(token, stale)

    window.close()
    window.deleteLater()
    app.processEvents()


def test_optimization_rew_read_binds_current_constraint_workspace(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    revision = repository.latest(F1_DOCUMENT_ID)
    assert revision is not None

    plan = _planned_measurement_plan(revision)
    controller.measurement_repository.save_measurement_plan(plan)
    controller.search_selected_spec_id = plan.search_spec_id
    controller.refresh_measurement_plans()
    controller.measurement_plan_tree.setCurrentItem(controller.measurement_plan_tree.topLevelItem(0))

    controller.rew_combo.addItem('REW A', 'rew-uuid-1')
    point_index = controller.campaign_measurement_point_combo.findData('point-mlp')
    assert point_index >= 0
    controller.campaign_measurement_point_combo.setCurrentIndex(point_index)
    monkeypatch.setattr(controller, '_start_rew_task', lambda *args, **kwargs: None)

    controller._start_selected_rew_read(
        validation_scope='owned_room',
        validation_campaign_id='campaign-1',
        evidence_type_override='measured',
    )

    expected = constraint_workspace_snapshot(controller.constraint_set)[1]
    token = next(iter(controller._rew_tokens.values()))
    assert token.constraint_workspace_hash == expected
    context = controller._current_job_apply_context()
    assert context is not None
    assert context.constraint_workspace_hash == expected
    assert controller.rew_job_guard.can_apply(token, context)

    # Validation/measurement reads are constraint-bound: a workspace change
    # while the read is in flight makes the delayed result stale.
    controller.constraint_set = _constraint_set(controller.document_id)
    stale = controller._current_job_apply_context()
    assert stale is not None
    assert stale.constraint_workspace_hash != expected
    assert not controller.rew_job_guard.can_apply(token, stale)

    controller.dispose()
    app.processEvents()
