"""REV61-UI regression tests.

Locks in the confirmed defects this round fixed:

- The measurement workspace's 予測↔実測 registration table now carries an
  accessibleName — it was the one remaining unlabeled control in the
  mounted-destinations sweep (test_accessible_labels was red on HEAD).
- Operator-facing surfaces route caught exceptions through
  ``operation_error_message`` instead of interpolating the raw exception.
  English internals (``[Errno 2]``, pydantic noise, exception class names)
  must never reach a label, dialog, or the persisted ``detail_ja``
  evidence field; the raw text stays in diagnostic/evidence fields only.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# -- registration table a11y ---------------------------------------------------


def test_registration_table_has_accessible_name(tmp_path: Path) -> None:
    """The 予測↔実測 table must be named for screen readers (#564 card)."""
    _app()
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    workspace = MeasurementPageWorkspace(
        MeasurementWorkflowController(repository, revision.document_id)
    )
    assert workspace.registration_table.accessibleName().strip()


# -- installation panel: mapped save errors -------------------------------------


def _installation_panel(tmp_path: Path):
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
    from htdt.installation_panel import InstallationPanel
    from htdt.system_expansion_widgets import SystemExpansionWorkflowService

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = SystemExpansionWorkflowService(repository, F1_DOCUMENT_ID)
    return InstallationPanel(
        repository, service.equipment_repository, F1_DOCUMENT_ID
    )


def test_installation_context_error_maps_exception(tmp_path: Path) -> None:
    """An internal ValueError must surface as operator JA text, not raw."""
    _app()
    from htdt import installation_panel

    panel = _installation_panel(tmp_path)

    def _fail(*_args, **_kwargs):
        raise ValueError("raw internal field detail")

    panel._selected_speaker_id = lambda: "speaker-fl"  # type: ignore[method-assign]
    panel._selected_definition = lambda: SimpleNamespace()  # type: ignore[method-assign]
    panel.actor_edit.setText("ops")
    original_builder = installation_panel.build_installation_context
    installation_panel.build_installation_context = _fail  # type: ignore[assignment]
    try:
        panel._save_context()
    finally:
        installation_panel.build_installation_context = original_builder  # type: ignore[assignment]

    text = panel.context_error_label.text()
    assert text
    assert "raw internal field detail" not in text


def test_installation_datum_error_maps_exception(tmp_path: Path) -> None:
    _app()
    panel = _installation_panel(tmp_path)
    panel.revision_combo.addItem("rev", "rev-missing")
    panel.revision_combo.setCurrentIndex(0)

    def _fail(_revision_id):
        raise ValueError("connection is closed: english internals")

    panel.scene_repository.get = _fail  # type: ignore[method-assign]
    panel._save_datum()

    text = panel.datum_error_label.text()
    assert text
    assert "english internals" not in text


# -- room acoustics: source file pick failure -----------------------------------


def test_treatment_source_file_error_maps_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    from PySide6.QtWidgets import QMessageBox
    from htdt import room_acoustics_panel
    from htdt.room_acoustics_panel import TreatmentDefinitionDialog

    dialog = TreatmentDefinitionDialog()
    missing = tmp_path / "gone.txt"
    monkeypatch.setattr(
        room_acoustics_panel.file_dialog_memory,
        "get_open_file_name",
        lambda *a, **k: (str(missing), ""),
    )
    captured: dict[str, str] = {}

    def _warning(_parent, _title, text, *a, **k):
        captured["text"] = text
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_warning))
    dialog._pick_source_file()

    assert captured.get("text")
    assert "No such file" not in captured["text"]
    assert "Errno" not in captured["text"]


# -- reflection guidance: load failure -------------------------------------------


def test_reflection_guidance_load_error_maps_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    from htdt import reflection_guidance_ui
    from htdt.reflection_guidance_ui import ReflectionGuidancePanel

    repository = SimpleNamespace(
        path=tmp_path / "cad.sqlite3",
        list_revisions=lambda *_a, **_k: (),
        revision_labels=lambda *_a, **_k: {},
    )
    controller = SimpleNamespace(
        repository=repository,
        document_id="doc-1",
    )
    panel = ReflectionGuidancePanel(controller)

    def _boom(*_a, **_k):
        raise OSError("[Errno 2] raw os detail")

    monkeypatch.setattr(
        reflection_guidance_ui, "load_reflection_guidance_view", _boom
    )
    panel.refresh()

    text = panel.summary_label.text()
    # The failure must be visible — not masked as an empty-but-healthy view.
    assert "失敗" in text
    assert "Errno" not in text
    assert "raw os detail" not in text


# -- acceptance checks: fail-closed detail_ja -------------------------------------


def test_auto_check_fail_closed_maps_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htdt import acceptance_checks

    def _boom(_ctx, _arg):
        raise ZeroDivisionError("division by zero internals")

    monkeypatch.setitem(acceptance_checks.AUTO_CHECKS, "boom", _boom)
    ctx = acceptance_checks.CheckContext(
        data_dir=tmp_path,
        db_path=tmp_path / "x.sqlite3",
        rew_base_url="http://localhost",
        run_id="r1",
        step_id="s1",
    )
    result = acceptance_checks.run_auto_check("boom", ctx)
    assert result.verdict == "unavailable"
    # Operator detail carries the mapped JA text…
    assert "ZeroDivisionError" not in result.detail_ja
    assert "division by zero internals" not in result.detail_ja
    # …while the evidence field keeps the raw diagnostics.
    assert "ZeroDivisionError" in result.evidence["error"]


def test_preflight_unavailable_maps_exception(tmp_path: Path) -> None:
    from htdt import acceptance_checks

    class _Client:
        def get_audio_preflight(self):
            raise ConnectionError("connection refused: [Errno 111]")

    ctx = acceptance_checks.CheckContext(
        data_dir=tmp_path,
        db_path=tmp_path / "x.sqlite3",
        rew_base_url="http://localhost",
        run_id="r1",
        step_id="s1",
        rew_client=_Client(),
    )
    _preflight, failure = acceptance_checks._preflight_or_unavailable(ctx)
    assert failure is not None
    assert "Errno" not in failure.detail_ja
    assert "connection refused" not in failure.detail_ja


# -- verification wizard: manifest + evidence file failures ----------------------


def test_manifest_failure_maps_exception(tmp_path: Path) -> None:
    _app()
    from htdt.verification_wizard_page import VerificationWizardPage

    page = VerificationWizardPage(tmp_path, manifest_path=tmp_path / "m.yaml")
    page._on_manifest_failed(OSError("[Errno 2] raw manifest detail"))

    text = page.verdict_reason.text()
    assert "Errno" not in text
    assert "raw manifest detail" not in text
    assert "OSError" not in text


def test_evidence_file_failure_maps_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    from PySide6.QtWidgets import QMessageBox
    from htdt.verification_wizard_page import VerificationWizardPage

    page = VerificationWizardPage(tmp_path, manifest_path=tmp_path / "m.yaml")
    page._check = SimpleNamespace(kind="manual", check_id="chk-1")
    page.attest_edit.setPlainText("確認済み")
    page._pending_files = [tmp_path / "missing.bin"]

    captured: dict[str, str] = {}

    def _warning(_parent, _title, text, *a, **k):
        captured["text"] = text
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_warning))
    page._commit_manual_evidence()

    assert captured.get("text")
    assert "No such file" not in captured["text"]
    assert "Errno" not in captured["text"]
