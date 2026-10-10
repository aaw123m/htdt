"""#955 follow-up: typed error boundaries in the capture/persistence lanes.

Sequel to ``test_issue_955_error_boundaries.py`` (the measurement/
optimization UI tranche). This file locks the same contract for the
capture/ingestion/watch/persistence + worker lanes:

* every broad catch in the converted modules carries an
  ``error-boundary:`` marker naming the boundary;
* injected expected operation failures degrade honestly (logged, sealed
  as the per-item problem, or crossed as the completion payload) and
  never abort the surrounding operation;
* sealed-authority failures (``*IntegrityError`` / ``*ConflictError``
  leaf-suffix classes, hard sqlite codes) propagate — never swallowed,
  never falsified.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.capture_ingestion_transaction import (
    CaptureIngestionRepository,
    PersistedIngestionIntegrityError,
)
from htdt.capture_inbox import CaptureInboxRepository
from htdt.native_worker import NativeWorker

SRC = Path(__file__).resolve().parents[1] / "src" / "htdt"

# Modules converted in the capture/persistence tranche — every broad catch
# inside them must carry an error-boundary marker (extends the #955 guard).
BOUNDARY_MODULES = (
    "capture_connected_space.py",
    "capture_import.py",
    "capture_inbox.py",
    "capture_ingestion_transaction.py",
    "capture_receiver.py",
    "capture_receiver_controller.py",
    "capture_receiver_settings.py",
    "capture_retention.py",
    "capture_retention_ui.py",
    "capture_semantic_promotion.py",
    "capture_watch_failures.py",
    "capture_watch_runner.py",
    "native_worker.py",
    "storage_watch_runner.py",
    "cad_acceptance_repository.py",
    "cad_acoustic_treatment_repository.py",
    "cad_apply_transaction_repository.py",
    "cad_objective_repository.py",
    "cad_project_template_repository.py",
    "cad_repository.py",
    "cad_robustness_repository.py",
    "cad_roomsim_repository.py",
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


def test_no_unmarked_broad_catches_in_capture_scope() -> None:
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
def app():
    return QApplication.instance() or QApplication([])


# -- native_worker ------------------------------------------------------------


def test_worker_operation_failure_crosses_as_error_payload(app):
    """The dispatch boundary is legitimately broad: ANY operation failure —
    including bug-class errors — must cross the thread boundary as the
    completion payload object (consumers map by exception class)."""
    failure = RuntimeError("device exploded")
    worker = NativeWorker("k", lambda _ev: (_ for _ in ()).throw(failure))
    seen: list[tuple] = []
    worker.completed.connect(lambda *a: seen.append(a))
    worker.run()
    assert seen == [("k", None, failure)]
    assert type(seen[0][2]) is RuntimeError


def test_worker_bug_class_failure_also_crosses_as_error_payload(app):
    failure = TypeError("contract violated")
    worker = NativeWorker("k", lambda _ev: (_ for _ in ()).throw(failure))
    seen: list[tuple] = []
    worker.completed.connect(lambda *a: seen.append(a))
    worker.run()
    assert seen == [("k", None, failure)]


# -- capture_inbox reconcile_orphaned_ingestions ------------------------------


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return ingestion, inbox


def _orphan_ingestion(ingestion, tmp_path: Path) -> None:
    """Persist an ingestion run WITHOUT staging it — the orphan the
    reconcile sweep exists to restage."""
    plan, payloads, _manifest = support.plan_and_payloads(
        tmp_path,
        files=support.mesh_specs_files(
            (('10000000-0000-4000-8000-000000000005',
              'mesh/geometry/10000000-0000-4000-8000-000000000005.meshbin',
              3, 1),),
            anchor_transform=(
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ),
            session_id='10000000-0000-4000-8000-000000000003',
            space_id='10000000-0000-4000-8000-000000000004',
        ),
        manifest_overrides={
            'capture_series_id': '10000000-0000-4000-8000-000000000001',
            'capture_revision_id': '10000000-0000-4000-8000-000000000099',
            'parent_revision_id': None,
            'capture_session_ids': ['10000000-0000-4000-8000-000000000003'],
            'coordinate_space_ids': ['10000000-0000-4000-8000-000000000004'],
            'created_at': '2026-09-20T00:00:00Z',
            'finalized_at': '2026-09-20T00:00:00Z',
        },
        id_map={
            support.SESSION_ID: '10000000-0000-4000-8000-000000000003',
            support.SPACE_ID: '10000000-0000-4000-8000-000000000004',
        },
    )
    ingestion.ingest(plan, payloads)


def test_orphaned_ingestion_is_restaged(tmp_path):
    ingestion, inbox = _rig(tmp_path)
    _orphan_ingestion(ingestion, tmp_path)
    recovered = inbox.reconcile_orphaned_ingestions()
    assert len(recovered) == 1
    assert recovered[0].arrival_source == 'restart_recovery'


def test_orphan_restage_expected_failure_is_logged_and_continues(
    tmp_path, monkeypatch, caplog
):
    """A per-digest expected failure (e.g. locked read) is logged with the
    digest and the sweep continues — never aborting the recovery."""
    ingestion, inbox = _rig(tmp_path)
    _orphan_ingestion(ingestion, tmp_path)
    monkeypatch.setattr(
        inbox.ingestion_repository,
        "get_ingestion",
        Mock(side_effect=OSError("db locked")),
    )
    with caplog.at_level(logging.ERROR, logger="htdt.capture_inbox"):
        recovered = inbox.reconcile_orphaned_ingestions()
    assert recovered == ()
    assert any(
        "could not restage orphaned ingestion" in record.getMessage()
        for record in caplog.records
    )


def test_orphan_restage_authority_failure_propagates(tmp_path, monkeypatch):
    """A sealed-store integrity failure must NOT degrade to an empty
    recovery — it propagates so the caller sees the sweep never ran."""
    ingestion, inbox = _rig(tmp_path)
    _orphan_ingestion(ingestion, tmp_path)
    monkeypatch.setattr(
        inbox.ingestion_repository,
        "get_ingestion",
        Mock(
            side_effect=PersistedIngestionIntegrityError(
                "persisted materialization corrupt"
            )
        ),
    )
    with pytest.raises(PersistedIngestionIntegrityError):
        inbox.reconcile_orphaned_ingestions()


# -- cad_repository journal write-throughs ------------------------------------


def test_clear_recovery_journal_failure_is_logged_not_raised(
    tmp_path, monkeypatch, caplog
):
    """The #883 draft-cleared journal is best-effort: a journaling failure
    must be logged (never a silent pass) and must not gate the canonical
    recovery-snapshot delete."""
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    import htdt.session_recovery as session_recovery

    monkeypatch.setattr(
        session_recovery,
        "declare_scene_draft_cleared",
        Mock(side_effect=OSError("journal locked")),
    )
    with caplog.at_level(logging.ERROR, logger="htdt.native"):
        repository.clear_recovery("doc-1")
    assert any(
        "scene-draft-cleared journaling failed" in record.getMessage()
        for record in caplog.records
    )
