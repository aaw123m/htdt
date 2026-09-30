"""Round-23 review: error-message actionability & in-app guidance.

Two gap classes verified in this round:

* Raw backend exception text (``str(exc)`` / ``f"{exc}"``) leaking onto
  user surfaces that bypass the #903 ``operation_error_message`` /
  ``warn_user`` contract — dirty-state resolution failures, the data
  management revalidation status card, the move-commit gate, and the
  pre-registration warning.
* The reason → help-topic bridge was built
  (``AvailabilityReasonSpec.help_topic_id`` → ``reason_help_topic_id`` →
  ``HelpRegistry.topic_for_reason``) but never reached the UI: a disabled
  palette result dead-ended on its reason sentence. ``PaletteResult`` now
  carries ``help_topic_id`` and the palette opens the bound topic when the
  operator presses Enter on a row it cannot run.

Every test drives the real surface (dialog, status text, palette list)
rather than asserting on source text.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.application_pages import CaptureInboxPage
from htdt.availability_reasons import availability_reason
from htdt.cad_constraint_models import (
    CadConstraintSet,
    CadWallClearanceConstraint,
)
from htdt.cad_document import EditStateError
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.capture_inbox import CaptureInboxRepository
from htdt.command_palette import CommandPalette
from htdt.command_registry import (
    CommandAvailability,
    CommandContext,
    CommandDefinition,
    CommandRegistry,
)
from htdt.data_management_ui import DataManagementWidget
from htdt.dirty_state_dialog import _apply as _apply_dirty_resolution
from htdt.palette_search import (
    CommandPaletteProvider,
    PaletteSearchService,
)
from htdt.room_workspace import RoomWorkspace
from htdt.user_facing_error import operation_error_message


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def qapp():
    return _app()


@pytest.fixture()
def msgboxes(monkeypatch):
    """Neutralize modal QMessageBox entry points and record what surfaced."""
    calls: list[tuple[str, str, str, str]] = []

    def _exec(self):
        calls.append(
            ("exec", self.windowTitle(), self.text(), self.informativeText())
        )
        return QMessageBox.StandardButton.Cancel

    def _static(kind):
        def _fn(parent, title, text, *args, **kwargs):
            calls.append((kind, str(title), str(text), ""))
            return QMessageBox.StandardButton.Cancel

        return staticmethod(_fn)

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    monkeypatch.setattr(QMessageBox, "warning", _static("warning"))
    monkeypatch.setattr(QMessageBox, "information", _static("information"))
    monkeypatch.setattr(QMessageBox, "question", _static("question"))
    monkeypatch.setattr(QMessageBox, "critical", _static("critical"))
    return calls


def _f1_repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _room_workspace(tmp_path):
    from test_room_workspace import FakeRoomViewport

    workspace = RoomWorkspace(
        _f1_repository(tmp_path),
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return workspace


# ---------------------------------------------------------------------------
# Palette "why → help" bridge


def _blocked_registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.set_deep_link_handler(lambda link: True)
    registry.register(
        CommandDefinition(
            command_id='mutate.thing',
            display_name='変更する',
            contexts=(CommandContext.GLOBAL,),
            keywords=('mutate',),
        ),
        availability=lambda: CommandAvailability.blocked(
            availability_reason('command.blocked.data_mutation_frozen')
        ),
    )
    return registry


def test_palette_result_carries_help_topic_for_catalog_reason() -> None:
    service = PaletteSearchService((CommandPaletteProvider(_blocked_registry()),))
    result = next(r for r in service.search('変更') if not r.available)
    assert result.disabled_reason == 'データ処理中はデータを変更できません'
    assert result.help_topic_id == 'trouble.command_unavailable'


def test_palette_result_without_catalog_reason_has_no_help_topic() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='mutate.thing',
            display_name='変更する',
            contexts=(CommandContext.GLOBAL,),
        ),
        availability=lambda: CommandAvailability.unavailable('凍結中です'),
    )
    service = PaletteSearchService((CommandPaletteProvider(registry),))
    result = next(r for r in service.search('変更') if not r.available)
    assert result.disabled_reason == '凍結中です'
    assert result.help_topic_id is None


def test_palette_activate_unavailable_opens_bound_help_topic(qapp) -> None:
    opened: list[str] = []
    service = PaletteSearchService((CommandPaletteProvider(_blocked_registry()),))
    palette = CommandPalette(
        service,
        on_help_topic=lambda topic_id: opened.append(topic_id) or True,
    )
    palette.refresh_results('変更')
    current = palette.results_list.currentItem()
    assert current is not None
    palette._activate_item(current)
    assert opened == ['trouble.command_unavailable']
    # The disabled reason still lands in the detail line — the operator
    # keeps the "why" sentence even after the help dialog opens.
    assert 'データ処理中はデータを変更できません' in palette.detail_label.text()
    palette.deleteLater()


def test_palette_selection_hints_help_for_bound_reason(qapp) -> None:
    service = PaletteSearchService((CommandPaletteProvider(_blocked_registry()),))
    palette = CommandPalette(service, on_help_topic=lambda topic_id: True)
    palette.refresh_results('変更')
    assert 'ヘルプ' in palette.detail_label.text()
    palette.deleteLater()


def test_palette_unavailable_without_topic_still_shows_reason(qapp) -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='mutate.thing',
            display_name='変更する',
            contexts=(CommandContext.GLOBAL,),
        ),
        availability=lambda: CommandAvailability.unavailable('凍結中です'),
    )
    service = PaletteSearchService((CommandPaletteProvider(registry),))
    palette = CommandPalette(service, on_help_topic=lambda topic_id: True)
    palette.refresh_results('変更')
    palette._activate_item(palette.results_list.currentItem())
    assert palette.detail_label.text() == '凍結中です'
    palette.deleteLater()


# ---------------------------------------------------------------------------
# Dirty-state resolution failures never surface raw exception text


def test_dirty_resolution_failure_maps_exception_text(qapp, msgboxes) -> None:
    mount = SimpleNamespace(
        resolve_dirty_state=Mock(
            side_effect=EditStateError('cannot remove unknown entities: e9')
        ),
        before_deactivate=None,
    )
    resolved = _apply_dirty_resolution(mount, 'save', '画面の切り替え', None)
    assert resolved is False
    warnings = [call for call in msgboxes if call[0] == 'warning']
    assert len(warnings) == 1
    text = warnings[0][2]
    assert '処理を完了できませんでした' in text
    assert 'cannot remove unknown entities' not in text
    assert '現在の編集状態では完了できませんでした' in text


def test_room_workspace_resolution_failure_maps_exception(tmp_path, qapp) -> None:
    workspace = _room_workspace(tmp_path)
    workspace.controller.working.commit_preview = Mock(
        side_effect=EditStateError('document replacement after state does not match')
    )
    resolved, message = workspace.controller.resolve_dirty_state('commit_preview')
    assert resolved is False
    assert message == '現在の編集状態では完了できませんでした'
    assert 'does not match' not in message


def test_move_commit_gate_maps_adapter_error(tmp_path, qapp) -> None:
    workspace = _room_workspace(tmp_path)
    workspace.controller.constraint_set = CadConstraintSet(
        document_id=F1_DOCUMENT_ID,
        constraints=(
            CadWallClearanceConstraint(
                constraint_id='ghost-clearance',
                name='幽霊壁離隔',
                entity_ids=('ghost-entity',),
                wall_id='wall:missing',
                min_m=0.4,
            ),
        ),
    )
    message = workspace.controller.move_commit_gate(('ghost-entity',))
    assert message is not None
    assert message.startswith('配置制約が参照先を失っています')
    assert 'requires' not in message and 'unknown' not in message
    assert 'データを処理できませんでした' in message


# ---------------------------------------------------------------------------
# Data-management revalidation status card uses the mapped message


def test_revalidation_failure_status_uses_mapped_message(qapp) -> None:
    page = DataManagementWidget.__new__(DataManagementWidget)
    page._busy = False
    page._restart_required = False
    page.controller = SimpleNamespace(
        revalidate=Mock(side_effect=RuntimeError('SQLITE_CANTOPEN raw dump'))
    )
    shown: dict[str, str] = {}
    page._show_status = lambda title, detail, state: shown.update(
        title=title, detail=detail, state=state
    )
    DataManagementWidget._run_revalidation(page)
    assert shown['title'] == '再検証を完了できませんでした'
    assert 'SQLITE_CANTOPEN' not in shown['detail']
    assert shown['detail'] == operation_error_message(
        RuntimeError('SQLITE_CANTOPEN raw dump')
    )


# ---------------------------------------------------------------------------
# Capture inbox: list row and detail localize the same authority codes


def _staged_inbox(tmp_path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    ingestion.ingest(plan, payloads)
    staged = inbox.stage(
        CaptureIngestionPlan.model_validate(plan),
        arrival_source='file_import',
    )
    return inbox, staged


def test_capture_inbox_surfaces_localize_authority_codes(tmp_path, qapp) -> None:
    inbox, staged = _staged_inbox(tmp_path)
    page = CaptureInboxPage(
        inbox.list_items,
        on_navigate=lambda link: True,
        inspect_item=inbox.inspect,
    )
    try:
        # Row cells use the same Japanese labels as the detail pane — the
        # table is the primary reading surface, not a raw enum dump.
        disposition_cell = page.table.item(0, 3).text()
        classification_cell = page.table.item(0, 2).text()
        scope_cell = page.table.item(0, 0).text()
        assert disposition_cell == '保留中'
        assert classification_cell == '新しい系列'
        assert scope_cell == '（未割り当て）'

        page.table.selectRow(0)
        _app().processEvents()
        text = page.detail.text()
        assert '新しい系列' in text
        assert 'new_series' not in text
        assert '（未割り当て）' in text
        assert '保留中' in text
        # Gate facet values are localized too.
        assert '検証済' in text
        assert 'validated' not in text
    finally:
        page.deleteLater()
