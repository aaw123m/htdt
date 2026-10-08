"""#955: typed error boundaries in the measurement/optimization UI layer.

The #815 Phase B exit audit found broad ``except Exception`` sites that
swallowed failures silently — a measurement-session failure could return a
falsified ``None``, and a diagram-click recovery could pass without logging
the failure identity. This file locks the audited contract:

* every broad catch in the scoped modules carries an ``error-boundary:``
  marker naming the boundary;
* injected expected operation failures report through the existing
  honest-surface mechanisms (status line / error label / log) and degrade;
* sealed-authority failures and unexpected (non-EXPECTED) errors propagate —
  never swallowed, never falsified.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from htdt.cad_repository import SceneRepository, SceneRevisionConflictError
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_schema import NativeSchemaError
from htdt.measurement_authority_dialogs import (
    RoutingProfileDialog,
    _RecordDialog,
)
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.optimization_workflow_controller import OptimizationWorkflowController
from htdt.revalidation_queue_panel import RevalidationQueuePanel
from htdt.rew_api import RewApiError

SRC = Path(__file__).resolve().parents[1] / "src" / "htdt"

# Modules converted in the #955 pass — every broad catch inside them must
# carry an error-boundary marker (extends the #815 guard).
BOUNDARY_MODULES = (
    "measurement_workflow.py",
    "measurement_record_surfaces.py",
    "measurement_authority_dialogs.py",
    "optimization_workflow_workspace.py",
    "workflow_shell.py",
    "optimization_workflow_controller.py",
    "optimization_measurement_controller.py",
    "optimization_search_controller.py",
    "optimization_validation_controller.py",
    "optimization_extended_controller.py",
    "optimization_adaptive_controller.py",
    "optimization_adaptive_extended_controller.py",
    "optimization_robustness_controller.py",
    "optimization_search_domain.py",
    "joint_optimization_panel.py",
    "robustness_authoring_panel.py",
    "revalidation_queue_panel.py",
    "commissioning_panel.py",
    "optimization_robustness.py",
    "optimization_robustness_multidimensional.py",
)

_BROAD_NAMES = {"Exception", "BaseException"}


def _broad(handler_type) -> bool:
    if handler_type is None:
        return True
    if isinstance(handler_type, ast.Name):
        return handler_type.id in _BROAD_NAMES
    if isinstance(handler_type, ast.Attribute):
        return handler_type.attr in _BROAD_NAMES
    if isinstance(handler_type, ast.Tuple):
        return any(
            isinstance(elt, (ast.Name, ast.Attribute))
            and getattr(elt, "id", getattr(elt, "attr", None)) in _BROAD_NAMES
            for elt in handler_type.elts
        )
    return False


def test_no_unmarked_broad_catches_in_955_scope() -> None:
    unmarked: list[str] = []
    for name in BOUNDARY_MODULES:
        source = SRC / name
        text = source.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ExceptHandler) and _broad(node.type):
                if "error-boundary:" not in lines[node.lineno - 1]:
                    unmarked.append(f"{name}:{node.lineno}")
    assert not unmarked, f"unmarked broad catches remain: {unmarked}"


@pytest.fixture()
def repository(tmp_path):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


# -- measurement_workflow -----------------------------------------------------


def _measurement_controller(repository):
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    controller.auto_assign_batch_items = Mock(return_value=())
    return controller


def test_pending_import_journaling_failure_is_logged(monkeypatch, repository, caplog):
    """The pending-import journal is best-effort: a journaling failure must
    be logged (never a silent pass) and must not break the caller."""
    import htdt.session_recovery as session_recovery

    monkeypatch.setattr(
        session_recovery,
        "declare_pending_import",
        Mock(side_effect=OSError("journal locked")),
    )
    controller = _measurement_controller(repository)
    with caplog.at_level(logging.WARNING, logger="htdt.measurement_workflow"):
        controller._journal_pending()
    assert any(
        "pending-import journaling failed" in record.getMessage()
        for record in caplog.records
    )


def test_engine_session_payload_expected_failure_reports_none(repository, caplog):
    controller = _measurement_controller(repository)
    controller.rew_client = SimpleNamespace(
        engine_session=Mock(side_effect=OSError("socket refused"))
    )
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        assert controller._engine_session_payload() is None
    assert any(
        "REWセッション証跡の確認" in record.getMessage()
        for record in caplog.records
    )


def test_engine_session_payload_authority_failure_propagates(repository):
    controller = _measurement_controller(repository)
    controller.rew_client = SimpleNamespace(
        engine_session=Mock(
            side_effect=SceneRevisionConflictError("conflict")
        )
    )
    with pytest.raises(SceneRevisionConflictError):
        controller._engine_session_payload()


def test_engine_session_payload_unexpected_failure_propagates(repository):
    controller = _measurement_controller(repository)
    controller.rew_client = SimpleNamespace(
        engine_session=Mock(side_effect=TypeError("unexpected"))
    )
    with pytest.raises(TypeError):
        controller._engine_session_payload()


def test_stage_teardown_unstages_and_propagates_unexpected(repository):
    """A mid-stage failure (past the per-item parse boundary) aborts the
    batch atomically: appended entries are unstaged so a retry cannot
    duplicate queue rows, and the unexpected error propagates."""
    controller = _measurement_controller(repository)
    controller._duplicate_names_for = Mock(side_effect=TypeError("boom"))
    with pytest.raises(TypeError):
        controller.stage_rew_text_files(
            [(b"20 70\n40 71\n80 69\n", "mic.txt")]
        )
    assert controller._batch == {}


def test_quality_report_expected_failure_warns_and_returns_none(
    repository, caplog
):
    controller = _measurement_controller(repository)
    producer = SimpleNamespace(
        produce_report=Mock(side_effect=OSError("producer failed"))
    )
    controller._quality_producer = Mock(return_value=producer)
    with caplog.at_level(logging.WARNING, logger="htdt.measurement_workflow"):
        assert controller._produce_quality_report("m-1") is None
    assert any(
        "quality report production failed for m-1" in record.getMessage()
        for record in caplog.records
    )


def test_quality_report_unexpected_failure_propagates(repository):
    controller = _measurement_controller(repository)
    producer = SimpleNamespace(
        produce_report=Mock(side_effect=TypeError("unexpected"))
    )
    controller._quality_producer = Mock(return_value=producer)
    with pytest.raises(TypeError):
        controller._produce_quality_report("m-1")


# -- measurement_authority_dialogs --------------------------------------------


class _FailingRecordDialog(_RecordDialog):
    """Minimal _RecordDialog: build_record injected per test."""

    build_error: Exception | None = None

    def build_record(self):  # noqa: D102 - test double
        if self.build_error is not None:
            raise self.build_error
        return object()


def test_record_dialog_accept_expected_failure_surfaces(app):
    dialog = _FailingRecordDialog(None, "t")
    dialog.build_error = ValueError("invalid shape")
    dialog.accept()
    assert "確定できません" in dialog.error_label.text()
    assert dialog.result() == 0  # stays open — the failure is surfaced


def test_record_dialog_accept_unexpected_failure_propagates(app):
    dialog = _FailingRecordDialog(None, "t")
    dialog.build_error = TypeError("unexpected")
    with pytest.raises(TypeError):
        dialog.accept()


def test_latest_output_device_label_expected_degrades(repository, caplog):
    """The label probe must degrade to no label on expected failures —
    never silently (the failure is reported through htdt.errors)."""
    fake = SimpleNamespace(
        controller=SimpleNamespace(
            quality_repository=SimpleNamespace(
                list_acquisition_contexts=Mock(
                    side_effect=OSError("locked")
                )
            )
        )
    )
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        result = RoutingProfileDialog._latest_output_device_label(fake)
    assert result is None
    assert any(
        "出力デバイス表示の確認" in record.getMessage()
        for record in caplog.records
    )


def test_latest_output_device_label_authority_propagates(repository):
    fake = SimpleNamespace(
        controller=SimpleNamespace(
            quality_repository=SimpleNamespace(
                list_acquisition_contexts=Mock(
                    side_effect=NativeSchemaError("store unreadable")
                )
            )
        )
    )
    with pytest.raises(NativeSchemaError):
        RoutingProfileDialog._latest_output_device_label(fake)


# -- revalidation_queue_panel -------------------------------------------------


def test_queue_compose_expected_failure_surfaces(app, repository):
    statuses: list[str] = []
    panel = RevalidationQueuePanel(
        repository,
        F1_DOCUMENT_ID,
        queue_supplier=Mock(side_effect=OSError("repository read failed")),
        on_status=statuses.append,
    )
    panel._compose()
    assert panel.status_label.text().startswith("再検証キューを作成できません")
    assert statuses and statuses[-1].startswith("再検証キューを作成できません")


def test_queue_compose_unexpected_failure_propagates(app, repository):
    panel = RevalidationQueuePanel(
        repository,
        F1_DOCUMENT_ID,
        queue_supplier=Mock(side_effect=TypeError("unexpected")),
    )
    with pytest.raises(TypeError):
        panel._compose()


# -- optimization controllers -------------------------------------------------


def test_decision_verdicts_expected_failure_surfaces(repository):
    """The decision-verdicts wrapper reports expected failures on the
    verdict label instead of swallowing."""
    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    controller.decision_verdict_label = QLabel()
    controller._apply_decision_verdicts = Mock(
        side_effect=OSError("read failed")
    )
    controller._refresh_decision_verdicts("spec-1", (), ())
    assert "証拠判定を計算できません" in controller.decision_verdict_label.text()


def test_decision_verdicts_unexpected_failure_propagates(repository):
    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    controller.decision_verdict_label = QLabel()
    controller._apply_decision_verdicts = Mock(
        side_effect=TypeError("unexpected")
    )
    with pytest.raises(TypeError):
        controller._refresh_decision_verdicts("spec-1", (), ())


def _seed_plan_selection(controller):
    controller.search_selected_spec_id = "s-1"
    controller.search_selected_candidate_id = "c-1"


def test_measurement_plan_save_expected_failure_surfaces(
    repository, monkeypatch
):
    """Plan save reports through the status surface; the controller's
    statusChanged signal carries the honest message."""
    import htdt.optimization_measurement_controller as plan_controller

    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    messages: list[str] = []
    controller.statusChanged.connect(messages.append)
    _seed_plan_selection(controller)
    monkeypatch.setattr(
        plan_controller,
        "build_measurement_plan",
        Mock(return_value=SimpleNamespace()),
    )
    controller.measurement_repository = SimpleNamespace(
        save_measurement_plan=Mock(side_effect=OSError("store busy"))
    )
    controller.create_measurement_plan_for_selected_candidate()
    assert any("実測候補を記録できません" in m for m in messages)


def test_measurement_plan_save_unexpected_failure_propagates(
    repository, monkeypatch
):
    import htdt.optimization_measurement_controller as plan_controller

    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    _seed_plan_selection(controller)
    monkeypatch.setattr(
        plan_controller,
        "build_measurement_plan",
        Mock(return_value=SimpleNamespace()),
    )
    controller.measurement_repository = SimpleNamespace(
        save_measurement_plan=Mock(side_effect=TypeError("unexpected"))
    )
    with pytest.raises(TypeError):
        controller.create_measurement_plan_for_selected_candidate()


# -- optimization journey (workspace degraded reads) --------------------------


def _workspace(repository, app):
    from htdt.optimization_workflow_workspace import OptimizationWorkflowWorkspace

    class _FakePlotter:
        def add_mesh(self, *a, **k):
            return object()

        def remove_actor(self, *a, **k) -> None:
            pass

        def add_text(self, *a, **k) -> None:
            pass

        def render(self) -> None:
            pass

    class _Viewport(QWidget):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.plotter = _FakePlotter()

        def render_document(self, *a, **k) -> None:
            pass

        def set_focus_ring(self, *a, **k) -> None:
            pass

    return OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _Viewport(parent),
    )


def test_journey_head_read_authority_failure_propagates(app, repository):
    workspace = _workspace(repository, app)
    workspace.controller.repository = SimpleNamespace(
        latest=Mock(side_effect=NativeSchemaError("store unreadable"))
    )
    with pytest.raises(NativeSchemaError):
        workspace._refresh_journey()


def test_journey_head_read_expected_failure_logs_and_degrades(
    app, repository, caplog
):
    """A locked/read-failed head degrades to step 1 — with the failure
    reported, never a silent falsified default."""
    workspace = _workspace(repository, app)
    workspace.controller.repository = SimpleNamespace(
        latest=Mock(side_effect=OSError("locked"))
    )
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        workspace._refresh_journey()
    assert any(
        "最適化の手順の部屋状態確認" in record.getMessage()
        for record in caplog.records
    )
    # Journey still evaluates — scene_saved=False → step strip renders.
    assert workspace._journey_steps
    assert workspace.journey_progress.text().endswith("/6")


def test_journey_spec_count_authority_failure_propagates(app, repository):
    """A sealed-store conflict during the per-spec count walk must
    propagate — counting zero would falsify the guide."""
    workspace = _workspace(repository, app)
    fake_spec = SimpleNamespace(
        search_spec_id="s-1", scene_revision_id="rev-1"
    )
    workspace.controller.search_repository = SimpleNamespace(
        list_specs=Mock(return_value=(fake_spec,))
    )
    workspace.controller.constraint_set = None  # skip the freshness probe
    workspace.controller.measurement_repository = SimpleNamespace(
        latest_measurement_plans=Mock(
            side_effect=SceneRevisionConflictError("conflict")
        )
    )
    with pytest.raises(SceneRevisionConflictError):
        workspace._refresh_journey()


def test_journey_unexpected_failure_propagates(app, repository):
    workspace = _workspace(repository, app)
    workspace.controller.repository = SimpleNamespace(
        latest=Mock(side_effect=TypeError("unexpected"))
    )
    with pytest.raises(TypeError):
        workspace._refresh_journey()


def test_journey_success_path_unchanged(app, repository):
    """Happy path still populates the strip — regression guard that the
    narrowed catches did not change success behavior."""
    workspace = _workspace(repository, app)
    workspace._refresh_journey()
    assert len(workspace._journey_steps) == 6
    assert workspace.journey_progress.text().endswith("/6")
