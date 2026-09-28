"""Round-11 cross-feature seam regressions.

Each test pins a seam where two features merged in different rounds meet —
and where the audit found the join was lying (stale copy, discarded
reasons, English leakage, dead refresh paths).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.application_pages import (  # noqa: E402
    _deletion_blocker_line,
    _OPERATION_STATE_LABELS,
)
from htdt.automatic_backup import (  # noqa: E402
    AutomaticBackupPolicy,
    AutomaticBackupScheduler,
    managed_data_fingerprint,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import make_empty_scene  # noqa: E402
from htdt.data_management import DataManagementBackend  # noqa: E402
from htdt.data_management_ui import (  # noqa: E402
    _excluded_component_label,
    _EXCLUDED_COMPONENT_LABELS,
    _STORAGE_CATEGORY_LABELS,
)
from htdt.launch_intents import (  # noqa: E402
    build_launch_intent,
    drain_launch_intents,
    forward_launch_intent,
    intents_dir,
)
from htdt.native_backup import DATABASE_NAME  # noqa: E402
from htdt.persisted_data import backup_excluded_names  # noqa: E402
from htdt.project_lifecycle import ProjectLibrary  # noqa: E402


def _seed_data(tmp_path: Path, document_id: str = 'doc-1') -> SceneRepository:
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene(document_id), parent_revision_id=None)
    return repository


def _scheduler(tmp_path: Path, **policy_kwargs) -> AutomaticBackupScheduler:
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        interval_hours=24.0,
        keep_generations=5,
        keep_daily_generations=14,
    )
    kwargs.update(policy_kwargs)
    return AutomaticBackupScheduler(data_dir, AutomaticBackupPolicy(**kwargs))


# -- seam 1: forwarded intents must not land on a later launch ---------


def test_stale_launch_intent_expires_to_dead(tmp_path: Path) -> None:
    intent = build_launch_intent(Path('room.htdtproject'), source='forwarded')
    dropped = forward_launch_intent(tmp_path, intent)
    stale = time.time() - 3600.0
    os.utime(dropped, (stale, stale))

    assert drain_launch_intents(tmp_path) == ()
    assert not dropped.exists()
    assert (intents_dir(tmp_path) / 'dead' / dropped.name).is_file()


def test_fresh_launch_intent_survives_expiry_check(tmp_path: Path) -> None:
    intent = build_launch_intent(Path('room.htdtproject'), source='forwarded')
    forward_launch_intent(tmp_path, intent)
    assert [q.intent for q in drain_launch_intents(tmp_path)] == [intent]


def test_intent_expiry_can_be_disabled(tmp_path: Path) -> None:
    intent = build_launch_intent(Path('room.htdtproject'), source='forwarded')
    dropped = forward_launch_intent(tmp_path, intent)
    stale = time.time() - 3600.0
    os.utime(dropped, (stale, stale))
    drained = drain_launch_intents(tmp_path, max_age_seconds=None)
    assert [q.intent for q in drained] == [intent]


# -- seam 2/5: manual backups and the periodic fingerprint -------------


def test_record_external_generation_marks_fingerprint(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    should, _ = scheduler.evaluate('periodic')
    assert should  # never backed up — a generation is due

    scheduler.record_external_generation()
    should, reason = scheduler.evaluate('periodic')
    assert not should
    assert 'unchanged' in reason
    # The periodic interval clock must NOT advance — a manual archive
    # postpones nothing (#752).
    assert 'last_automatic_at_utc' not in scheduler._load_state()


def test_manual_create_backup_marks_coverage(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    data_dir = tmp_path / 'data'
    backend = DataManagementBackend(data_dir)
    backend.create_backup(tmp_path / 'manual.htdt-backup')

    scheduler = _scheduler(tmp_path)
    should, reason = scheduler.evaluate('periodic')
    assert not should
    assert 'unchanged' in reason


# -- seam 5/6: deletion blockers localize honestly ----------------------


def test_deletion_blocker_carries_subject_count(tmp_path: Path) -> None:
    _seed_data(tmp_path, 'doc-parent')
    _seed_data(tmp_path, 'doc-child')
    library = ProjectLibrary(tmp_path / 'data' / DATABASE_NAME)
    parent = library.register_project('doc-parent', 'Parent')
    library.register_project(
        'doc-child', 'Child', cloned_from_project_id=parent.project_id
    )
    library.archive_project(parent.project_id)

    plan = library.plan_project_deletion(parent.project_id)
    (blocker,) = [
        b for b in plan.hard_blockers if b.kind == 'active_descendants'
    ]
    assert blocker.count == 1


def test_deletion_blocker_line_renders_japanese() -> None:
    from htdt.project_lifecycle import DeletionBlocker

    blocker = DeletionBlocker(
        kind='active_descendants',
        detail='2 project(s) were cloned from this project; retire them first',
        count=2,
    )
    line = _deletion_blocker_line(blocker)
    assert '2 件' in line
    assert 'project' not in line

    state = DeletionBlocker(kind='project_not_archived', detail='diagnostic')
    assert 'アーカイブ' in _deletion_blocker_line(state)


def test_cancelled_operation_state_is_past_tense() -> None:
    assert _OPERATION_STATE_LABELS['cancelled'] == 'キャンセル済み'


# -- seam 6: no internal identifiers leak into JP surfaces --------------


def test_excluded_component_labels_cover_registry() -> None:
    missing = [
        name
        for name in backup_excluded_names()
        if name not in _EXCLUDED_COMPONENT_LABELS
    ]
    assert missing == []
    assert _excluded_component_label('diagnostics') == '診断データ'


def test_storage_category_labels_cover_report_ids() -> None:
    for category_id in ('native-database', 'managed-assets', 'diagnostics'):
        assert category_id in _STORAGE_CATEGORY_LABELS


# -- seam 4: the joint lane refreshes when setup is shown ---------------


def test_setup_section_refreshes_joint_optimization_panel(tmp_path) -> None:
    from PySide6.QtWidgets import QApplication, QWidget

    from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
    from htdt.optimization_workflow_workspace import (
        OptimizationWorkflowWorkspace,
    )

    app = QApplication.instance() or QApplication([])

    class _FakePlotter:
        def add_mesh(self, *_a, **_k):
            return object()

        def remove_actor(self, *_a, **_k) -> None:
            pass

        def add_text(self, *_a, **_k) -> None:
            pass

        def render(self) -> None:
            pass

    class _FakeViewport(QWidget):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.plotter = _FakePlotter()

        def render_document(self, *_, **__) -> None:
            pass

    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )
    calls = {'count': 0}
    panel = workspace.joint_optimization_panel
    original = panel.refresh

    def _counting_refresh() -> None:
        calls['count'] += 1
        original()

    panel.refresh = _counting_refresh
    try:
        workspace.select_section('setup')
        assert calls['count'] >= 1
        workspace.refresh_from_authorities()
        assert calls['count'] >= 2
    finally:
        panel.refresh = original
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
