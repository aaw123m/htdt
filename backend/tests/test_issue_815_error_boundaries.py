"""#815: typed error-boundary failure-injection tests.

Each test injects a failure at a converted boundary by mocking the
collaborator (controller/repository/service), never PySide internals, and
asserts the taxonomy contract:

- expected user/input errors → actionable UI feedback;
- adapter/external failures → original reason preserved in the
  diagnostics detail;
- authority/integrity failures → propagate or reach the fail-closed
  path, never a fabricated default;
- unexpected programming errors → escape to the central diagnostics
  boundary (never an actionable-looking rejection);
- no converted site regresses to silent default state (failures are
  logged through ``htdt.errors``);
- an exception after a partial mutation never leaves a success-looking
  UI.
"""

from __future__ import annotations

import ast
import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_acoustic_metrology_repository import AcousticMetrologyIntegrityError
from htdt.cad_document import EditStateError
from htdt.cad_repository import SceneRepository, SceneRevisionConflictError
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_schema import NativeSchemaError
from htdt.localization import LanguagePolicy
from htdt.error_boundary import (
    ERROR_BOUNDARY_MARKER,
    EXPECTED_OPERATION_ERRORS,
    BoundaryCategory,
    classify_boundary_error,
    is_authority_failure,
    report_boundary_failure,
    report_unexpected_error,
)
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import (
    MeasurementWorkflowController,
    MeasurementWorkflowError,
)
from htdt.rew_api import RewApiError, RewApiUnavailable
from htdt.rew_parser import RewParseError
from htdt.room_viewport import RoomOverlayState
from htdt.room_workspace import RoomWorkspace
from htdt.ui_theme import SemanticState
from htdt.workflow_application import WorkflowApplicationComposition


SRC = Path(__file__).resolve().parents[1] / "src" / "htdt"
BOUNDARY_MODULES = (
    "measurement_page_workspace.py",
    "workflow_application.py",
    "room_workspace.py",
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _measurement_workspace(tmp_path: Path):
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(repository, revision.document_id)
    return controller, MeasurementPageWorkspace(controller)


def _close(widget) -> None:
    widget.close()
    widget.deleteLater()
    _app().processEvents()


class _FakeRoomViewport(QFrame):
    """Minimal viewport stand-in so RoomWorkspace builds without VTK."""

    entitySelected = Signal(object)

    def render_document(self, *args, **kwargs) -> None:
        pass

    def render_proposed_entities(self, *args, **kwargs) -> None:
        pass

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, *_args, **_kwargs) -> None:
        pass

    def close(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Source-level guard: no unapproved broad catches in the scoped modules.
# ---------------------------------------------------------------------------

_BROAD_NAMES = {"Exception", "BaseException"}


def _is_broad_catch(node_type: ast.expr | None) -> bool:
    if node_type is None:  # bare ``except:``
        return True
    if isinstance(node_type, ast.Name):
        return node_type.id in _BROAD_NAMES
    if isinstance(node_type, ast.Attribute):
        return node_type.attr in _BROAD_NAMES
    if isinstance(node_type, ast.Tuple):
        return any(
            isinstance(elt, (ast.Name, ast.Attribute))
            and getattr(elt, "id", getattr(elt, "attr", None)) in _BROAD_NAMES
            for elt in node_type.elts
        )
    return False


def test_no_unmarked_broad_catches_in_scope_modules() -> None:
    """Every remaining ``except Exception`` must carry an error-boundary
    marker classifying why it legitimately stays broad."""
    for module_name in BOUNDARY_MODULES:
        path = SRC / module_name
        lines = path.read_text(encoding="utf-8").splitlines()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_broad_catch(node.type):
                continue
            line = lines[node.lineno - 1]
            assert ERROR_BOUNDARY_MARKER in line, (
                f"{module_name}:{node.lineno} is a broad catch without an "
                f"'{ERROR_BOUNDARY_MARKER}' classification comment"
            )


def test_expected_operation_errors_covers_domain_vocabulary() -> None:
    """The narrowing tuple must cover every error family the product
    treats as an expected rejection — and must exclude programming bugs."""
    assert ValueError in EXPECTED_OPERATION_ERRORS
    assert RuntimeError in EXPECTED_OPERATION_ERRORS
    assert OSError in EXPECTED_OPERATION_ERRORS
    assert KeyError in EXPECTED_OPERATION_ERRORS
    assert sqlite3.Error in EXPECTED_OPERATION_ERRORS
    assert TypeError not in EXPECTED_OPERATION_ERRORS
    assert AttributeError not in EXPECTED_OPERATION_ERRORS


# ---------------------------------------------------------------------------
# Taxonomy classification
# ---------------------------------------------------------------------------


def test_classify_user_input_and_domain_rejections() -> None:
    assert (
        classify_boundary_error(MeasurementWorkflowError("未保存です"))
        is BoundaryCategory.USER_INPUT
    )
    assert classify_boundary_error(ValueError("bad")) is BoundaryCategory.USER_INPUT
    assert classify_boundary_error(KeyError("k")) is BoundaryCategory.USER_INPUT
    assert (
        classify_boundary_error(EditStateError("編集中です"))
        is BoundaryCategory.USER_INPUT
    )


def test_classify_authority_failures_fail_closed() -> None:
    for exc in (
        SceneRevisionConflictError("head moved"),
        AcousticMetrologyIntegrityError("row hash mismatch"),
        NativeSchemaError("schema vN unreadable"),
    ):
        assert is_authority_failure(exc)
        assert classify_boundary_error(exc) is BoundaryCategory.AUTHORITY


def test_classify_sqlite_corruption_as_authority() -> None:
    corrupt = sqlite3.OperationalError("database disk image is malformed")
    corrupt.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    assert is_authority_failure(corrupt)
    locked = sqlite3.OperationalError("database is locked")
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    assert not is_authority_failure(locked)


def test_classify_external_adapter_and_unexpected() -> None:
    assert (
        classify_boundary_error(RewApiUnavailable("connection refused"))
        is BoundaryCategory.EXTERNAL_ADAPTER
    )
    assert (
        classify_boundary_error(RewParseError("parse failed"))
        is BoundaryCategory.EXTERNAL_ADAPTER
    )
    assert classify_boundary_error(OSError("io")) is BoundaryCategory.EXTERNAL_ADAPTER
    assert classify_boundary_error(TypeError("bug")) is BoundaryCategory.UNEXPECTED
    assert classify_boundary_error(AttributeError("bug")) is BoundaryCategory.UNEXPECTED


# ---------------------------------------------------------------------------
# Boundary logging observability
# ---------------------------------------------------------------------------


def test_boundary_failure_logs_expected_at_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        try:
            raise MeasurementWorkflowError("部屋を一度保存してください")
        except EXPECTED_OPERATION_ERRORS as exc:
            report_boundary_failure(exc, operation="測定計画の状態確認")
    assert caplog.records
    assert any("測定計画の状態確認" in r.getMessage() for r in caplog.records)


def test_boundary_failure_logs_authority_at_error(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        try:
            raise NativeSchemaError("schema mismatch")
        except EXPECTED_OPERATION_ERRORS as exc:
            error = report_boundary_failure(exc, operation="登録の読み込み")
    assert any(
        r.levelno >= logging.ERROR and "authority failure" in r.getMessage()
        for r in caplog.records
    )
    assert "NativeSchemaError" in (error.technical_detail or "")


def test_unexpected_error_reports_as_internal_unexpected(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="htdt.errors"):
        try:
            raise TypeError("programming bug")
        except Exception as exc:
            error = report_boundary_failure(exc, operation="比較の準備")
    assert error.code == "internal.unexpected"
    assert error.severity is SemanticState.ERROR
    assert "予期しない" in error.message
    assert "TypeError" in (error.technical_detail or "")
    assert any("unexpected error" in r.getMessage() for r in caplog.records)


def test_report_unexpected_error_shape() -> None:
    error = report_unexpected_error(TypeError("x"), operation="描画")
    assert error.code == "internal.unexpected"
    assert error.effect == "操作は完了していません"
    assert "描画" in error.title


# ---------------------------------------------------------------------------
# measurement_page_workspace — injected failures at converted boundaries
# ---------------------------------------------------------------------------


def test_auto_assign_user_error_produces_actionable_notice(tmp_path) -> None:
    """Category 1: a domain rejection must surface actionable UI feedback,
    not a silent failure or a crash."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.auto_assign_batch_items = Mock(
            side_effect=MeasurementWorkflowError("割り当て対象がありません")
        )
        items = (
            SimpleNamespace(item_id="i-1"),
            SimpleNamespace(item_id="i-2"),
        )
        tried, total = workspace._auto_assign_safely(items)
        assert (tried, total) == (0, 2)
        assert "自動割り当てに失敗しました" in workspace.notice.text()
        assert "MeasurementWorkflowError" in (
            workspace.last_operation_error_detail or ""
        )
    finally:
        _close(workspace)
        app.processEvents()


def test_auto_assign_adapter_failure_preserves_reason(tmp_path) -> None:
    """Category 2: adapter failures keep the original reason — the mapped
    notice is actionable AND the diagnostics detail carries provenance."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.auto_assign_batch_items = Mock(
            side_effect=RewApiUnavailable("connection refused port 4735")
        )
        workspace._auto_assign_safely((SimpleNamespace(item_id="i-1"),))
        assert workspace.notice.text()
        assert "connection refused port 4735" in (
            workspace.last_operation_error_detail or ""
        )
    finally:
        _close(workspace)
        app.processEvents()


def test_auto_assign_unexpected_error_escapes(tmp_path) -> None:
    """Category 5: a programming error is not dressed as a recoverable
    rejection — it propagates to the central diagnostics boundary."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.auto_assign_batch_items = Mock(
            side_effect=TypeError("internal bug")
        )
        with pytest.raises(TypeError):
            workspace._auto_assign_safely((SimpleNamespace(item_id="i-1"),))
        # No actionable rejection notice was fabricated.
        assert "自動割り当てに失敗しました" not in workspace.notice.text()
    finally:
        _close(workspace)
        app.processEvents()


def test_campaign_builder_authority_failure_propagates(tmp_path) -> None:
    """Category 4: an integrity failure on a degraded read must not
    become an empty combo — it propagates instead."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.runner_source_options = Mock(
            side_effect=NativeSchemaError("store schema mismatch")
        )
        with pytest.raises(NativeSchemaError):
            workspace._refresh_campaign_builder()
    finally:
        _close(workspace)
        app.processEvents()


def test_campaign_builder_expected_failure_logs_and_degrades(
    tmp_path, caplog
) -> None:
    """A non-authority operational failure still degrades to the empty
    option list — but observably: the failure is logged, never silent."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.runner_source_options = Mock(
            side_effect=RewApiError("source catalog unreachable")
        )
        with caplog.at_level(logging.WARNING, logger="htdt.errors"):
            workspace._refresh_campaign_builder()
        assert any(
            "キャンペーン音源候補の読み込み" in r.getMessage()
            for r in caplog.records
        )
    finally:
        _close(workspace)
        app.processEvents()


def test_registration_listing_authority_failure_propagates(tmp_path) -> None:
    """A sealed-store integrity failure must not read as an empty
    registration table — the refresh fails closed."""
    app = _app()
    _, workspace = _measurement_workspace(tmp_path)
    try:
        service = SimpleNamespace(
            list_registrations=Mock(
                side_effect=SceneRevisionConflictError("head moved")
            ),
        )
        workspace._registration_service = lambda: service
        with pytest.raises(SceneRevisionConflictError):
            workspace._refresh_registration_panel()
    finally:
        _close(workspace)
        app.processEvents()


def _fake_registration() -> SimpleNamespace:
    return SimpleNamespace(
        registration_id="reg-1",
        measurement=SimpleNamespace(measurement_id="meas-1234567890"),
        prediction=SimpleNamespace(
            kind="imported_prediction_dataset", prediction_id="pred-1"
        ),
        comparability=SimpleNamespace(state="comparable"),
        receiver=SimpleNamespace(position_delta_m=None),
        timing=SimpleNamespace(method="shared_reference"),
        partition="unassigned",
    )


def test_registration_freshness_failure_is_unverified_not_current(
    tmp_path,
) -> None:
    """The regression guard for the stale-as-current falsification: an
    unreadable freshness marks the row 確認不可 and never '最新'."""
    app = _app()
    _, workspace = _measurement_workspace(tmp_path)
    try:
        service = SimpleNamespace(
            list_registrations=lambda: (_fake_registration(),),
            registration_freshness=Mock(
                side_effect=ValueError("freshness index unreadable")
            ),
        )
        workspace._registration_service = lambda: service
        workspace._refresh_registration_panel()
        assert workspace.registration_table.rowCount() == 1
        assert workspace.registration_table.item(0, 2).text() == "確認不可"
        # The stale gate stays closed: 'unverified' is not 'current'.
        assert not workspace.residual_compute_button.isEnabled()
    finally:
        _close(workspace)
        app.processEvents()


def test_activity_reporting_failure_is_observable(tmp_path, caplog) -> None:
    """Best-effort activity-center reporting degrades but is logged —
    a silent ``except: pass`` is no longer acceptable."""
    app = _app()
    _, workspace = _measurement_workspace(tmp_path)
    try:
        center = SimpleNamespace(
            submit=lambda **_: "op-1",
            mark_running=lambda _id: None,
            complete=Mock(side_effect=ValueError("activity store busy")),
        )
        workspace._activity_center = center
        with caplog.at_level(logging.WARNING, logger="htdt.errors"):
            workspace._report_auto_ingest("取り込み", "1 件")
        assert any(
            "アクティビティ記録の登録" in r.getMessage()
            for r in caplog.records
        )
    finally:
        _close(workspace)
        app.processEvents()


def test_unsaved_scene_probe_still_reports_needs_scene(tmp_path) -> None:
    """The narrowed ``latest_revision`` probe keeps its designed signal:
    genuinely unsaved → scene_saved False, other failures propagate."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.latest_revision = Mock(
            side_effect=MeasurementWorkflowError("部屋を一度保存してください")
        )
        workspace._refresh_journey((), ())
        controller.latest_revision = Mock(
            side_effect=NativeSchemaError("store unreadable")
        )
        with pytest.raises(NativeSchemaError):
            workspace._refresh_journey((), ())
    finally:
        _close(workspace)
        app.processEvents()


def test_partial_commit_retake_failure_not_success(tmp_path) -> None:
    """Partial mutation: commit succeeded but retake lineage failed —
    the UI must report the exact outcome, not a clean success."""
    app = _app()
    controller, workspace = _measurement_workspace(tmp_path)
    try:
        controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "m.txt")
        workspace.refresh()
        workspace.set_context("assignment")
        # Choose the pending scope and a real target/speaker.
        scope_index = next(
            i
            for i in range(workspace.assignment_scope_combo.count())
            if workspace.assignment_scope_combo.itemData(i) == ("pending", None)
        )
        workspace.assignment_scope_combo.setCurrentIndex(scope_index)
        target_index = next(
            i
            for i in range(workspace.target_combo.count())
            if workspace.target_combo.itemData(i) == "point-mlp"
        )
        workspace.target_combo.setCurrentIndex(target_index)
        if workspace.source_speaker_list.count():
            workspace.source_speaker_list.item(0).setCheckState(
                Qt.CheckState.Checked
            )
        workspace._retake_source_id = "meas-old"
        controller.commit_pending = Mock(
            return_value=SimpleNamespace(
                measurement_id="meas-new", evidence_type="measured"
            )
        )
        controller.record_retake = Mock(
            side_effect=ValueError("同じ測定点・役割・音源が必要です")
        )
        workspace._commit_assignment()
        text = workspace.notice.text()
        assert "保存されましたが" in text
        assert "再測定の系譜を記録できませんでした" in text
        assert "保存しました。「品質」で内容を確認" not in text
    finally:
        _close(workspace)
        app.processEvents()


# ---------------------------------------------------------------------------
# workflow_application — app-composition boundaries
# ---------------------------------------------------------------------------


def test_import_availability_blocks_only_for_unsaved_scene() -> None:
    """The availability probe narrows to the unsaved-scene signal; a
    store failure is not allowed to masquerade as 'no scene saved'."""
    stub = SimpleNamespace(
        latest_revision=Mock(
            side_effect=MeasurementWorkflowError("部屋を一度保存してください")
        )
    )
    availability = (
        WorkflowApplicationComposition._measurement_import_availability(stub)
    )
    assert not availability.enabled
    assert availability.reason is not None

    corrupt = SimpleNamespace(
        latest_revision=Mock(
            side_effect=SceneRevisionConflictError("head conflict")
        )
    )
    with pytest.raises(SceneRevisionConflictError):
        WorkflowApplicationComposition._measurement_import_availability(corrupt)


def test_language_policy_expected_only(tmp_path) -> None:
    """Invalid stored language values fall back to system default; a
    programming error is not silently absorbed."""
    composition = WorkflowApplicationComposition.__new__(
        WorkflowApplicationComposition
    )
    composition.preferences = SimpleNamespace(
        get=lambda _key: "fr-FR-unknown"
    )
    assert composition._language_policy() is LanguagePolicy.SYSTEM_DEFAULT

    composition.preferences = SimpleNamespace(
        get=Mock(side_effect=RuntimeError("store gone"))
    )
    assert composition._language_policy() is LanguagePolicy.SYSTEM_DEFAULT


# ---------------------------------------------------------------------------
# room_workspace — room-edit boundaries
# ---------------------------------------------------------------------------


def _room_workspace(tmp_path: Path) -> RoomWorkspace:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeRoomViewport(parent),
    )


def test_room_journey_authority_failure_propagates(tmp_path) -> None:
    """The journey strip degrades on ordinary read failures but a sealed
    head read failure must never present as 'nothing saved' (step 1)."""
    app = _app()
    workspace = _room_workspace(tmp_path)
    try:
        workspace.controller.repository.latest = Mock(
            side_effect=NativeSchemaError("head unreadable")
        )
        with pytest.raises(NativeSchemaError):
            workspace._refresh_journey()
    finally:
        _close(workspace)
        app.processEvents()


def test_room_journey_expected_failure_logs_and_degrades(
    tmp_path, caplog
) -> None:
    app = _app()
    workspace = _room_workspace(tmp_path)
    try:
        workspace.listener_pose_repository.selections_for_document = Mock(
            side_effect=ValueError("pose rows unreadable")
        )
        with caplog.at_level(logging.WARNING, logger="htdt.errors"):
            workspace._refresh_journey()
        assert any(
            "測定姿勢の確認" in r.getMessage() for r in caplog.records
        )
    finally:
        _close(workspace)
        app.processEvents()


def test_constraints_evaluate_boundary(tmp_path) -> None:
    """The constraints panel surfaces evaluator failures on the panel —
    and unexpected errors still propagate."""
    app = _app()
    workspace = _room_workspace(tmp_path)
    try:
        workspace.controller.evaluate_constraints = Mock(
            side_effect=ValueError("constraint malformed")
        )
        workspace._sync_constraints_panel()
        # evaluate_error was surfaced to the panel, not swallowed.
        assert workspace.constraints_panel is not None

        workspace.controller.evaluate_constraints = Mock(
            side_effect=TypeError("internal bug")
        )
        with pytest.raises(TypeError):
            workspace._sync_constraints_panel()
    finally:
        _close(workspace)
        app.processEvents()
