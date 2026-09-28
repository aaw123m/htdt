"""#736: the document-open semantic contract, exercised headless.

Every route returns exactly one LaunchIntentOutcome; success outcomes are
asserted only where the underlying domain action ran (project import,
inbox staging, backup validation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_repository import SceneRepository
from htdt.launch_intents import build_activation_intent, build_launch_intent
from htdt.launch_router import route_launch_intent
from htdt.native_backup import create_backup
from htdt.project_bundle import export_project_bundle
from htdt.project_library_repository import ProjectLibraryRepository

from capture_fixture_support import plan_and_payloads


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
        ),
    )


def _repository(tmp_path: Path) -> SceneRepository:
    return SceneRepository(tmp_path / 'cad-scenes.sqlite3')


def _write_descriptor(path: Path, **fields: object) -> Path:
    path.write_text(json.dumps(fields), encoding='utf-8')
    return path


def test_descriptor_routes_to_registered_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save(_scene('doc-a'), parent_revision_id=None)
    library = ProjectLibraryRepository(repository)
    entry = library.ensure_document_registered('doc-a', 'Living Room')

    ref = _write_descriptor(
        tmp_path / 'room.htdtproject',
        kind='htdt-project-ref',
        schema_version=1,
        project_id=entry.project_id,
        document_id='doc-a',
        display_name='Living Room',
    )
    result = route_launch_intent(
        build_launch_intent(ref, source='file_association'),
        repository=repository,
    )
    assert result.outcome == 'routed_and_opened'
    assert result.document_id == 'doc-a'


def test_descriptor_unknown_document_requires_user_action(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    ref = _write_descriptor(
        tmp_path / 'ghost.htdtproject',
        kind='htdt-project-ref',
        schema_version=1,
        document_id='doc-missing',
    )
    result = route_launch_intent(
        build_launch_intent(ref), repository=repository
    )
    assert result.outcome == 'user_action_required'
    assert result.document_id == 'doc-missing'
    # The descriptor path must never auto-register the document.
    assert (
        ProjectLibraryRepository(repository).get_by_document_id(
            'doc-missing'
        )
        is None
    )


def test_descriptor_without_identity_is_invalid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    ref = _write_descriptor(
        tmp_path / 'empty.htdtproject',
        kind='htdt-project-ref',
        schema_version=1,
    )
    result = route_launch_intent(
        build_launch_intent(ref), repository=repository
    )
    assert result.outcome == 'invalid_or_unsupported'


def test_non_descriptor_non_zip_project_file_is_invalid(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    bogus = tmp_path / 'bogus.htdtproject'
    bogus.write_bytes(b'\x00\x01\xff definitely not a bundle')
    result = route_launch_intent(
        build_launch_intent(bogus), repository=repository
    )
    assert result.outcome == 'invalid_or_unsupported'


def test_project_bundle_zip_imports_and_routes(tmp_path: Path) -> None:
    source = SceneRepository(tmp_path / 'source' / 'cad-scenes.sqlite3')
    source.save(_scene('doc-a'), parent_revision_id=None)
    archive = tmp_path / 'out' / 'doc-a.htdtproject'
    export_project_bundle(source, 'doc-a', archive)

    target = _repository(tmp_path / 'target-data')
    result = route_launch_intent(
        build_launch_intent(archive), repository=target
    )
    assert result.outcome == 'routed_and_opened'
    assert result.document_id == 'doc-a'
    # Import is the semantic action: the document must be registered and
    # its revisions readable.
    assert (
        ProjectLibraryRepository(target).get_by_document_id('doc-a')
        is not None
    )
    assert target.current_head('doc-a') is not None


def test_missing_file_is_invalid_not_crashed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    missing = tmp_path / 'gone.htdtproject'
    intent = build_launch_intent(missing)
    result = route_launch_intent(intent, repository=repository)
    assert result.outcome == 'invalid_or_unsupported'


def test_capture_bundle_stages_into_inbox(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan_and_payloads(tmp_path / 'bundle-src')
    bundle_dir = tmp_path / 'bundle-src' / 'bundle'

    intent = build_launch_intent(bundle_dir.with_suffix('.htdtcapture'))
    # The file does not exist yet -> invalid, not a crash.
    result = route_launch_intent(intent, repository=repository)
    assert result.outcome == 'invalid_or_unsupported'

    # Point the intent at the real bundle directory.
    intent = intent.model_copy(update={'path': str(bundle_dir)})
    result = route_launch_intent(intent, repository=repository)
    assert result.outcome == 'staged_for_review'
    assert result.inbox_item_id is not None

    # Re-delivery of the identical bundle reports already_staged.
    again = route_launch_intent(intent, repository=repository)
    assert again.outcome == 'already_staged'
    assert again.inbox_item_id == result.inbox_item_id


def test_capture_descriptor_resolves_bundle_and_mismatch(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plan, _payloads, manifest_bytes = plan_and_payloads(tmp_path / 'b')
    bundle_dir = tmp_path / 'b' / 'bundle'
    manifest = json.loads(manifest_bytes)

    descriptor = _write_descriptor(
        tmp_path / 'drop.htdtcapture',
        kind='htdt-capture-ref',
        schema_version=1,
        capture_revision_id=manifest['capture_revision_id'],
        bundle_path=str(bundle_dir),
    )
    result = route_launch_intent(
        build_launch_intent(descriptor), repository=repository
    )
    assert result.outcome == 'staged_for_review'
    del plan

    # A descriptor naming a different revision must not silently stage.
    wrong = _write_descriptor(
        tmp_path / 'wrong.htdtcapture',
        kind='htdt-capture-ref',
        schema_version=1,
        capture_revision_id='00000000-0000-4000-8000-0000000000ff',
        bundle_path=str(bundle_dir),
    )
    result = route_launch_intent(
        build_launch_intent(wrong), repository=repository
    )
    assert result.outcome == 'invalid_or_unsupported'


def test_capture_descriptor_missing_bundle_is_user_action(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    descriptor = _write_descriptor(
        tmp_path / 'unplaced.htdtcapture',
        kind='htdt-capture-ref',
        schema_version=1,
    )
    result = route_launch_intent(
        build_launch_intent(descriptor), repository=repository
    )
    assert result.outcome == 'user_action_required'


def test_invalid_capture_file_fails_not_succeeds(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bad = tmp_path / 'junk.htdtcapture'
    bad.write_bytes(b'{"kind": "htdt-capture-ref", "bundle_path": "/nope"}')
    result = route_launch_intent(
        build_launch_intent(bad), repository=repository
    )
    # Descriptor parses but its bundle does not exist -> invalid.
    assert result.outcome in {
        'invalid_or_unsupported',
        'user_action_required',
        'failed',
    }


def test_backup_archive_routes_to_preview(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(_scene('doc-a'), parent_revision_id=None)
    backup = tmp_path / 'backups' / 'snapshot.htdt-backup'
    create_backup(data_dir, backup)

    result = route_launch_intent(
        build_launch_intent(backup), repository=repository
    )
    assert result.outcome == 'preview_opened'


def test_non_backup_file_named_backup_is_invalid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fake = tmp_path / 'fake.htdt-backup'
    fake.write_text('{"nope": true}', encoding='utf-8')
    result = route_launch_intent(
        build_launch_intent(fake), repository=repository
    )
    assert result.outcome == 'invalid_or_unsupported'


def test_unknown_suffix_is_invalid_or_unsupported(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    other = tmp_path / 'notes.txt'
    other.write_text('hello', encoding='utf-8')
    result = route_launch_intent(
        build_launch_intent(other), repository=repository
    )
    assert result.outcome == 'invalid_or_unsupported'


def test_activation_intent_reports_activated(tmp_path: Path) -> None:
    """'activate' carries no file work; the shell already raised the window."""

    repository = _repository(tmp_path)
    result = route_launch_intent(
        build_activation_intent(tmp_path), repository=repository
    )
    assert result.outcome == 'activated'
    assert result.document_id is None


@pytest.mark.parametrize(
    'outcome',
    [
        'routed_and_opened',
        'staged_for_review',
        'already_staged',
        'preview_opened',
        'activated',
        'user_action_required',
        'blocked_dirty_state',
        'invalid_or_unsupported',
        'failed',
    ],
)
def test_outcome_contract_is_closed_vocabulary(outcome: str) -> None:
    from htdt.launch_intents import LaunchIntentResult

    result = LaunchIntentResult(
        intent_id='i', kind='open_project', path='p', outcome=outcome
    )
    assert result.outcome == outcome
