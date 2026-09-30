"""Round 20: corrupted-state honesty for persisted artifacts.

Every persisted artifact must degrade honestly when a real user/disk
corrupts it: a clear error naming the artifact, or the documented clean
fallback for convenience stores — never a crash, never silently loading
wrong data, never a bare low-level decode error escaping a tolerant read.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from htdt.activity_center import ActivityCenter, OperationClass
from htdt.cad_repository import SceneRepository, SceneRevisionIntegrityError
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    NativeSchemaError,
    check_native_schema_compatibility,
    ensure_native_schema,
    read_native_schema_version,
)
from htdt.commissioning_plan import (
    CommissioningIntent,
    CommissioningPlanError,
    CommissioningPlanRepository,
    new_plan,
)
from htdt.native_backup import DATABASE_NAME
from htdt.native_upgrade import (
    NativeUpgradeQuarantineError,
    UpgradeStateRecord,
    execute_native_upgrade,
    list_upgrade_events,
    read_upgrade_state,
    upgrade_journal_path,
    upgrade_state_path,
    write_upgrade_state,
)


def _intent() -> CommissioningIntent:
    return CommissioningIntent(
        is_new_project=True,
        has_existing_room=False,
        audio_only=False,
        rew_available=True,
        wants_hybrid_prediction=True,
        planned_speaker_count=5,
        goals=('迫力', '定位'),
    )


def _plan_repository_with_plan(data_dir: Path) -> CommissioningPlanRepository:
    store = CommissioningPlanRepository(data_dir)
    store.save(new_plan('doc-a', 'theater-a', _intent()))
    return store


def _plans_file(data_dir: Path) -> Path:
    return data_dir / 'commissioning-plans.json'


# ---------------------------------------------------------------------------
# commissioning-plans.json — auxiliary project registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'payload',
    [
        b'{"plans": {"doc-a": {"plan_id": "plan-x"',  # truncated
        b'[1, 2, 3]',  # valid JSON, not an object
        b'"a string"',
        b'5',
        b'{"plans": 5}',  # object, wrong plans shape
        b'\xff\xfe\xaa\x88',  # undecodable bytes
        b'',  # zero-length
    ],
)
def test_commissioning_registry_corrupt_degrades_to_empty(
    tmp_path: Path, payload: bytes
) -> None:
    store = _plan_repository_with_plan(tmp_path)
    _plans_file(tmp_path).write_bytes(payload)

    # Every reader degrades to the documented empty registry instead of
    # crashing on .get/.setdefault/.values of a non-dict payload.
    assert store.get('doc-a') is None
    assert store.latest() is None
    assert store.list_plans() == ()

    # A save over corrupt state replaces it with a valid registry.
    store.save(new_plan('doc-b', 'theater-b', _intent()))
    assert store.get('doc-b') is not None


def test_commissioning_registry_corrupt_row_fails_honestly(
    tmp_path: Path,
) -> None:
    store = _plan_repository_with_plan(tmp_path)
    _plans_file(tmp_path).write_text(
        '{"plans": {"doc-a": 42}}',
        encoding='utf-8',
    )
    # A corrupt row fails closed with the named typed error on get(),
    # and the listing/latest readers degrade instead of crashing.
    with pytest.raises(CommissioningPlanError):
        store.get('doc-a')
    assert store.latest() is None
    assert store.list_plans() == ()


# ---------------------------------------------------------------------------
# activity_history.json — diagnostics-only sidecar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'payload',
    [
        b'{"schema_version": 1, "operations": [{"operation_id": "op-1"',  # truncated
        b'[1, 2, 3]',
        b'5',
        b'{"schema_version": 1, "operations": 42}',  # non-list items
        b'{"schema_version": 1, "active_operations": {"a": 1}}',
        b'\xff\xfe\xaa\x88',
        b'',
    ],
)
def test_activity_history_corrupt_is_tolerated(
    tmp_path: Path, payload: bytes
) -> None:
    history = tmp_path / 'activity_history.json'
    history.write_bytes(payload)
    # Diagnostics reads must never crash the export that contains them.
    assert ActivityCenter.load_history(history) == ()
    assert ActivityCenter.load_active_operations(history) == ()


def test_activity_history_valid_payload_still_loads(tmp_path: Path) -> None:
    center = ActivityCenter()
    op_id = center.submit(
        operation_kind='scan',
        operation_class=OperationClass.COMPUTE,
        title='op',
    )
    center.mark_running(op_id)
    center.complete(op_id, result_summary='done')
    history = tmp_path / 'activity_history.json'
    center.persist_history(history)
    assert len(ActivityCenter.load_history(history)) == 1


# ---------------------------------------------------------------------------
# .native-upgrade-state.json — the durable quarantine marker
# ---------------------------------------------------------------------------


def _create_current_database(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_f1_scene(), parent_revision_id=None)


@pytest.mark.parametrize(
    'payload',
    [
        b'{"state": "committed_pending_verification", "upg',
        b'[1, 2, 3]',
        b'{"totally": "foreign"}',
        b'\xff\xfe\xaa\x88',
        b'',
    ],
)
def test_corrupt_upgrade_marker_quarantines_instead_of_silent_skip(
    tmp_path: Path, payload: bytes
) -> None:
    _create_current_database(tmp_path)
    write_upgrade_state(
        tmp_path,
        UpgradeStateRecord(
            state='committed_pending_verification',
            upgrade_id='up-1',
            from_schema=NATIVE_SCHEMA_VERSION - 1,
            to_schema=NATIVE_SCHEMA_VERSION,
            started_at_utc='2026-01-01T00:00:00+00:00',
            updated_at_utc='2026-01-01T00:00:00+00:00',
        ),
    )
    upgrade_state_path(tmp_path).write_bytes(payload)

    # The marker exists but is unreadable: the live generation might be
    # committed-but-unverified, so the launch gate must fail closed —
    # never a silent 'no_upgrade' that opens the unproven database.
    assert read_upgrade_state(tmp_path) is None
    with pytest.raises(NativeUpgradeQuarantineError):
        execute_native_upgrade(tmp_path)


def test_absent_upgrade_marker_stays_a_silent_noop(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    assert read_upgrade_state(tmp_path) is None
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'no_upgrade'


def test_unreadable_upgrade_journal_is_tolerated(tmp_path: Path) -> None:
    journal = upgrade_journal_path(tmp_path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b'\xff\xfe\xaa\x88')
    assert list_upgrade_events(tmp_path) == ()


# ---------------------------------------------------------------------------
# cad-scenes.sqlite3 — zero-byte existing file is torn state, not fresh
# ---------------------------------------------------------------------------


def test_zero_byte_database_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / DATABASE_NAME
    db.write_bytes(b'')

    with pytest.raises(NativeSchemaError):
        read_native_schema_version(db)
    with pytest.raises(NativeSchemaError):
        check_native_schema_compatibility(db)
    with pytest.raises(NativeSchemaError):
        ensure_native_schema(db)
    with pytest.raises(NativeSchemaError):
        SceneRepository(db)


def test_missing_database_still_initializes_fresh(tmp_path: Path) -> None:
    db = tmp_path / DATABASE_NAME
    assert read_native_schema_version(db) == 0
    assert check_native_schema_compatibility(db) == 0
    ensure_native_schema(db)
    assert read_native_schema_version(db) == NATIVE_SCHEMA_VERSION


def test_upgrade_plan_rejects_zero_byte_database(tmp_path: Path) -> None:
    (tmp_path / DATABASE_NAME).write_bytes(b'')
    with pytest.raises(NativeSchemaError):
        execute_native_upgrade(tmp_path)


# ---------------------------------------------------------------------------
# scene revision row payload — typed integrity error, never a bare decode
# ---------------------------------------------------------------------------


def test_corrupt_scene_revision_payload_raises_named_integrity_error(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    with sqlite3.connect(tmp_path / 'cad.sqlite3') as connection, connection:
        connection.execute(
            "UPDATE scene_revisions SET payload_json='{\"broken\": '"
            ' WHERE document_id=?',
            (F1_DOCUMENT_ID,),
        )
    with pytest.raises(SceneRevisionIntegrityError):
        repository.current_head(F1_DOCUMENT_ID)


def test_scene_revision_hash_mismatch_raises_named_integrity_error(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    with sqlite3.connect(tmp_path / 'cad.sqlite3') as connection, connection:
        # A well-formed payload whose bytes no longer hash to the stored
        # content hash is corruption, not a valid alternate document.
        connection.execute(
            "UPDATE scene_revisions SET content_hash='0' "
            ' WHERE document_id=?',
            (F1_DOCUMENT_ID,),
        )
    with pytest.raises(SceneRevisionIntegrityError):
        repository.current_head(F1_DOCUMENT_ID)
