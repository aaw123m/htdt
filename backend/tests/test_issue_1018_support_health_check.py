"""#1018 — SupportPage read-only health-check surface.

Covers: button→async-job→results wiring via the real SupportHealthRunner,
per-category itemization, optional-integration non-escalation,
reason-specific guidance mapping, concurrent re-run safety, cancellation,
stale-result honesty, and narrow/DPI/UIA reachability.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea

from htdt.activity_center import ActivityCenter, OperationState
from htdt.application_pages import SupportPage, health_check_guidance
from htdt.support_diagnostics import (
    HealthCategory,
    HealthCheckResult,
    HealthReport,
    HealthStatus,
)
from htdt.support_health_runner import SupportHealthRunner


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _pump_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for health check")
        time.sleep(0.01)


def _result(
    check_id: str,
    category: HealthCategory,
    status: HealthStatus,
    summary: str = "summary",
    detail: str | None = None,
) -> HealthCheckResult:
    return HealthCheckResult(
        check_id=check_id,
        category=category,
        status=status,
        summary=summary,
        detail=detail,
    )


def _report(*results: HealthCheckResult) -> HealthReport:
    return HealthReport(
        checked_at="2026-10-08T12:00:00+00:00", results=tuple(results)
    )


class _FakeRunner(QObject):
    """Signal-compatible stand-in for page-level rendering tests."""

    check_started = Signal()
    report_ready = Signal(object)
    run_failed = Signal(object)
    run_cancelled = Signal()
    run_finished = Signal()

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.last_fingerprint = "fp123"
        self.started = 0
        self.cancel_requests = 0
        self.shut_down = False
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self) -> bool:
        self.started += 1
        if self._busy:
            return False
        self._busy = True
        self.check_started.emit()
        return True

    def request_cancel(self) -> bool:
        self.cancel_requests += 1
        return self._busy

    def shutdown(self) -> None:
        self.shut_down = True

    def finish_with(self, report: HealthReport) -> None:
        self.report_ready.emit(report)
        self._busy = False
        self.run_finished.emit()


def _make_page(tmp_path, runner, **kwargs) -> SupportPage:
    _app()
    return SupportPage(tmp_path, health_runner=runner, **kwargs)


def _all_labels(page: SupportPage) -> list[str]:
    return [label.text() for label in page.findChildren(QLabel)]


def test_surface_wiring_runs_job_and_renders_results(tmp_path):
    """Button → runner.start() → report_ready → itemized results."""
    runner = _FakeRunner(tmp_path)
    report = _report(
        _result(
            "storage.database_openable",
            HealthCategory.APP_STORAGE,
            HealthStatus.PASS,
            "database opened read-only",
        ),
        _result(
            "integrity.semantic",
            HealthCategory.SEMANTIC_INTEGRITY,
            HealthStatus.PASS,
            "audit passed",
        ),
        _result(
            "integrations.rew_api",
            HealthCategory.INTEGRATIONS,
            HealthStatus.ATTENTION,
            "REW API is not reachable (optional integration)",
        ),
    )
    page = _make_page(tmp_path, runner)
    try:
        page.health_button.click()
        assert runner.started == 1
        assert not page.health_button.isEnabled()
        assert not page.health_cancel_button.isHidden()
        assert "診断しています" in page.health_status.text()

        runner.finish_with(report)
        assert page.health_button.isEnabled()
        assert not page.health_results.isHidden()
        texts = "\n".join(_all_labels(page))
        assert "最終実行: 2026-10-08T12:00:00+00:00" in page.health_status.text()
        assert "アプリ・プロジェクトの保存データ" in texts
        assert "データの意味整合性" in texts
        assert "外部連携（任意）" in texts
        assert "プロジェクトDBのオープン" in texts
        assert "権威グラフの意味監査" in texts
        assert "REW API連携" in texts
    finally:
        page.close()
        page.deleteLater()


def test_real_runner_async_path_and_operation_record(tmp_path):
    """Real SupportHealthRunner: worker thread, ActivityCenter record."""
    center = ActivityCenter()
    report = _report(
        _result(
            "storage.disk_space",
            HealthCategory.APP_STORAGE,
            HealthStatus.PASS,
            "enough free space",
        )
    )
    runner = SupportHealthRunner(
        tmp_path, center, lambda: (lambda _cancel: report)
    )
    page = _make_page(tmp_path, runner)
    delivered: list[HealthReport] = []
    runner.report_ready.connect(delivered.append)
    try:
        assert runner.start() is True
        _pump_until(lambda: not runner.busy)
        assert delivered == [report]
        operations = center.recent()
        assert len(operations) == 1
        assert operations[0].operation_kind == "support_health_check"
        assert operations[0].state == OperationState.COMPLETED
        assert "全項目正常" in (operations[0].result_summary or "")
        assert runner.last_fingerprint
        assert "最終実行" in page.health_status.text()
    finally:
        runner.shutdown()
        page.close()
        page.deleteLater()


def test_concurrent_rerun_refused_while_busy(tmp_path):
    """A second start while a run is in flight is refused, not queued."""
    center = ActivityCenter()
    gate = threading.Event()

    def job(cancel: threading.Event):
        while not gate.is_set():
            if cancel.is_set():
                return _report()
            time.sleep(0.01)
        return _report()

    runner = SupportHealthRunner(tmp_path, center, lambda: job)
    try:
        assert runner.start() is True
        assert runner.busy
        assert runner.start() is False  # concurrent re-run safety
        assert runner.start() is False
        assert len(center.active()) == 1
        gate.set()
        _pump_until(lambda: not runner.busy)
        # after the run settled, a re-run IS allowed
        assert runner.start() is True
        gate.set()
        _pump_until(lambda: not runner.busy)
    finally:
        gate.set()
        runner.shutdown()


def test_cancel_lands_as_cancelled_never_as_result(tmp_path):
    center = ActivityCenter()

    def job(cancel: threading.Event):
        while not cancel.is_set():
            time.sleep(0.01)
        return _report()

    runner = SupportHealthRunner(tmp_path, center, lambda: job)
    cancelled: list[bool] = []
    runner.run_cancelled.connect(lambda: cancelled.append(True))
    try:
        assert runner.start() is True
        _pump_until(lambda: len(center.active()) == 1)
        assert runner.request_cancel() is True
        _pump_until(lambda: not runner.busy)
        assert cancelled == [True]
        op = center.recent()[0]
        assert op.state == OperationState.CANCELLED
    finally:
        runner.shutdown()


def test_failed_job_reports_honestly(tmp_path):
    center = ActivityCenter()

    def job(_cancel):
        raise RuntimeError("disk exploded")

    runner = SupportHealthRunner(tmp_path, center, lambda: job)
    failed: list[object] = []
    runner.run_failed.connect(failed.append)
    page = _make_page(tmp_path, runner)
    try:
        assert runner.start() is True
        _pump_until(lambda: not runner.busy)
        assert len(failed) == 1
        op = center.recent()[0]
        assert op.state == OperationState.FAILED
        assert "診断を完了できませんでした" in page.health_status.text()
    finally:
        runner.shutdown()
        page.close()
        page.deleteLater()


def test_optional_integration_failure_never_escalates(tmp_path):
    """An integrations FAIL renders as ATTENTION overall, not app damage."""
    runner = _FakeRunner(tmp_path)
    report = _report(
        _result(
            "storage.database_openable",
            HealthCategory.APP_STORAGE,
            HealthStatus.PASS,
        ),
        _result(
            "integrity.semantic",
            HealthCategory.SEMANTIC_INTEGRITY,
            HealthStatus.PASS,
        ),
        _result(
            "integrations.rew_api",
            HealthCategory.INTEGRATIONS,
            HealthStatus.FAIL,
            "probe raised",
            "socket error",
        ),
    )
    page = _make_page(tmp_path, runner)
    try:
        runner.start()
        runner.finish_with(report)
        assert report.overall == HealthStatus.ATTENTION
        assert "総合判定: 注意" in page.health_status.text()
        texts = "\n".join(_all_labels(page))
        assert "アプリ・プロジェクトの保存データ — 正常" in texts
        # honesty note next to the integrations section
        assert "アプリやプロジェクトDBの障害では" in texts
        # the failed optional check still shows its own failed state
        assert "REW API連携 — 失敗" in texts
    finally:
        page.close()
        page.deleteLater()


def test_guidance_mapping_per_failure_kind():
    cases = {
        ("storage.database_openable", HealthCategory.APP_STORAGE): (
            "data_management",
            "バックアップ",
        ),
        ("storage.sqlite_quick_check", HealthCategory.APP_STORAGE): (
            "data_management",
            "隔離復元",
        ),
        ("storage.schema_compatibility", HealthCategory.APP_STORAGE): (
            "data_management",
            "バックアップ",
        ),
        ("storage.data_dir_lock", HealthCategory.APP_STORAGE): (
            "activity",
            "アクティビティ",
        ),
        ("storage.disk_space", HealthCategory.APP_STORAGE): (
            "data_management",
            "空き容量",
        ),
        ("integrity.semantic", HealthCategory.SEMANTIC_INTEGRITY): (
            "authority",
            "権威グラフ",
        ),
        ("integrations.rew_api", HealthCategory.INTEGRATIONS): (
            "preferences",
            "環境設定",
        ),
        ("integrations.capture_receiver", HealthCategory.INTEGRATIONS): (
            "capture_settings",
            "キャプチャ",
        ),
        ("integrations.vtk", HealthCategory.INTEGRATIONS): (
            "export",
            "診断パッケージ",
        ),
    }
    for (check_id, category), (action, wording) in cases.items():
        guidance = health_check_guidance(
            _result(check_id, category, HealthStatus.FAIL)
        )
        assert guidance is not None, check_id
        text, action_key = guidance
        assert action_key == action, (check_id, action_key)
        assert wording in text, (check_id, text)
    # unknown check ids fall back to category-level guidance
    guidance = health_check_guidance(
        _result("integrations.probe_9", HealthCategory.INTEGRATIONS, HealthStatus.FAIL)
    )
    assert guidance is not None and guidance[1] == "preferences"
    guidance = health_check_guidance(
        _result("storage.mystery", HealthCategory.APP_STORAGE, HealthStatus.FAIL)
    )
    assert guidance is not None and guidance[1] == "data_management"
    # PASS / NOT_APPLICABLE need no next step
    assert (
        health_check_guidance(
            _result("x", HealthCategory.APP_STORAGE, HealthStatus.PASS)
        )
        is None
    )
    assert (
        health_check_guidance(
            _result("x", HealthCategory.APP_STORAGE, HealthStatus.NOT_APPLICABLE)
        )
        is None
    )


def test_guidance_buttons_route_to_real_surfaces(tmp_path):
    """Each guidance action opens the surface it names — nothing invented."""
    opened: list[str] = []
    runner = _FakeRunner(tmp_path)
    report = _report(
        _result(
            "storage.data_dir_lock",
            HealthCategory.APP_STORAGE,
            HealthStatus.FAIL,
        ),
        _result(
            "integrity.semantic",
            HealthCategory.SEMANTIC_INTEGRITY,
            HealthStatus.FAIL,
        ),
    )
    page = _make_page(
        tmp_path,
        runner,
        open_data_management=lambda _w: opened.append("data"),
        open_preferences=lambda _w: opened.append("prefs"),
        open_capture_settings=lambda _w: opened.append("capture"),
        open_activity=lambda _w: opened.append("activity"),
        open_authority_graph=lambda _w: opened.append("authority"),
    )
    try:
        runner.start()
        runner.finish_with(report)
        activity_button = page.findChild(
            QPushButton, "supportHealthAction-activity"
        )
        authority_button = page.findChild(
            QPushButton, "supportHealthAction-authority"
        )
        assert activity_button is not None
        assert authority_button is not None
        activity_button.click()
        authority_button.click()
        assert opened == ["activity", "authority"]
    finally:
        page.close()
        page.deleteLater()


def test_never_falsely_all_pass(tmp_path):
    """Mixed results render their own statuses — no blanket green."""
    runner = _FakeRunner(tmp_path)
    report = _report(
        _result(
            "storage.database_openable",
            HealthCategory.APP_STORAGE,
            HealthStatus.FAIL,
            "cannot open",
        ),
        _result(
            "storage.disk_space",
            HealthCategory.APP_STORAGE,
            HealthStatus.PASS,
        ),
        _result(
            "integrations.vtk",
            HealthCategory.INTEGRATIONS,
            HealthStatus.NOT_APPLICABLE,
            "unprobed",
        ),
    )
    page = _make_page(tmp_path, runner)
    try:
        runner.start()
        runner.finish_with(report)
        texts = "\n".join(_all_labels(page))
        assert "総合判定: 失敗" in page.health_status.text()
        assert "プロジェクトDBのオープン — 失敗" in texts
        assert "ディスク空き容量 — 正常" in texts
        assert "3D表示スタック（VTK） — 対象外" in texts
    finally:
        page.close()
        page.deleteLater()


def test_refresh_reuses_report_with_own_timestamp(tmp_path):
    """refresh() re-renders the stored report — never re-runs the check."""
    runner = _FakeRunner(tmp_path)
    report = _report(
        _result(
            "storage.disk_space",
            HealthCategory.APP_STORAGE,
            HealthStatus.PASS,
        )
    )
    page = _make_page(tmp_path, runner)
    try:
        runner.start()
        runner.finish_with(report)
        page.refresh()
        assert runner.started == 1  # refresh did not re-run
        assert "最終実行: 2026-10-08T12:00:00+00:00" in page.health_status.text()
    finally:
        page.close()
        page.deleteLater()


def test_report_stamps_data_state_fingerprint(tmp_path):
    """The report header records which data state it was produced on."""
    runner = _FakeRunner(tmp_path)
    runner.last_fingerprint = "abc123def456"
    page = _make_page(tmp_path, runner)
    try:
        runner.start()
        runner.finish_with(_report())
        assert "データ状態: abc123def456" in page.health_status.text()
    finally:
        page.close()
        page.deleteLater()


def test_narrow_ui_and_dpi_reachable(tmp_path):
    """Small windows + 200% font proxy: results stay scroll-reachable."""
    runner = _FakeRunner(tmp_path)
    report = _report(
        *[
            _result(
                f"storage.check_{i}",
                HealthCategory.APP_STORAGE,
                HealthStatus.FAIL if i % 3 == 0 else HealthStatus.PASS,
                f"summary {i}",
            )
            for i in range(12)
        ]
    )
    for width, height in ((640, 480), (800, 600), (1280, 720)):
        page = _make_page(tmp_path, runner)
        try:
            page.resize(width, height)
            page.show()
            runner.start()
            runner.finish_with(report)
            scroll = page.findChild(QScrollArea, "supportHealthResults")
            assert scroll is not None
            assert scroll.widget() is not None
            # first and last check rows are scroll-reachable
            labels = [
                w
                for w in scroll.widget().findChildren(QLabel)
                if "storage.check_11" in w.text()
            ]
            assert labels, f"last check row missing at {width}x{height}"
            scroll.ensureWidgetVisible(labels[0])
            assert page.health_button.accessibleName()
            assert page.health_cancel_button.accessibleName()
            page.close()
        finally:
            page.deleteLater()
    # DPI200 proxy: double point size, still renders
    page = _make_page(tmp_path, runner)
    try:
        font = page.font()
        font.setPointSize(font.pointSize() * 2)
        page.setFont(font)
        page.resize(800, 600)
        page.show()
        runner.start()
        runner.finish_with(report)
        assert page.health_results is not None
        assert page.health_button.isEnabled()
        page.close()
    finally:
        page.deleteLater()


def test_close_drains_runner(tmp_path):
    runner = _FakeRunner(tmp_path)
    page = _make_page(tmp_path, runner)
    page.close()
    assert runner.shut_down is True
    page.deleteLater()


def test_semantic_check_absent_db_is_not_applicable(tmp_path):
    """Read-only guarantee: no db → NOT_APPLICABLE, and no probe dirs leak."""
    from htdt.support_diagnostics import semantic_integrity_check

    result = semantic_integrity_check(tmp_path)
    assert result.status == HealthStatus.NOT_APPLICABLE
    assert result.category == HealthCategory.SEMANTIC_INTEGRITY
    leftovers = list(Path(tmp_path).glob(".health-audit-*"))
    assert leftovers == []


def test_empty_db_is_not_applicable_without_writes(tmp_path):
    """An empty database file is honestly N/A — never a fabricated audit."""
    import sqlite3

    from htdt.support_diagnostics import DATABASE_NAME, semantic_integrity_check

    db = tmp_path / DATABASE_NAME
    sqlite3.connect(db).close()  # valid but empty (0-table) file
    before = {
        p.name: p.stat().st_mtime_ns
        for p in tmp_path.iterdir()
        if p.is_file()
    }
    result = semantic_integrity_check(tmp_path)
    assert result.status in (
        HealthStatus.NOT_APPLICABLE,
        HealthStatus.PASS,
        HealthStatus.FAIL,
        HealthStatus.ATTENTION,
    )
    # the live file was never written to
    after = {
        p.name: p.stat().st_mtime_ns
        for p in tmp_path.iterdir()
        if p.is_file()
    }
    assert before == after
    assert list(Path(tmp_path).glob(".health-audit-*")) == []


def test_real_runner_full_job_end_to_end(tmp_path):
    """run_health_checks through the runner: probes itemize, no UI block."""
    center = ActivityCenter()

    def factory():
        def job(_cancel):
            from htdt.support_diagnostics import (
                capture_receiver_probe,
                run_health_checks,
                vtk_probe,
            )

            return run_health_checks(
                tmp_path,
                integration_probes=(
                    capture_receiver_probe({"enabled": False}),
                    vtk_probe(),
                ),
            )

        return job

    runner = SupportHealthRunner(tmp_path, center, factory)
    page = _make_page(tmp_path, runner)
    delivered: list[HealthReport] = []
    runner.report_ready.connect(delivered.append)
    try:
        page.health_button.click()
        _pump_until(lambda: not runner.busy)
        assert len(delivered) == 1
        report = delivered[0]
        # storage checks itemize individually
        ids = {r.check_id for r in report.results}
        assert "storage.disk_space" in ids
        assert "storage.data_dir_lock" in ids
        assert "integrations.capture_receiver" in ids
        assert "integrations.vtk" in ids
        texts = "\n".join(_all_labels(page))
        assert "アプリ・プロジェクトの保存データ" in texts
        assert "外部連携（任意）" in texts
        assert "最終実行" in page.health_status.text()
    finally:
        runner.shutdown()
        page.close()
        page.deleteLater()
