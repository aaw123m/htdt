from __future__ import annotations

import json
from pathlib import Path

from htdt.launch_intents import (
    build_activation_intent,
    build_launch_intent,
    classify_launch_path,
    complete_queued_intent,
    describe_launch_intent,
    drain_launch_intents,
    forward_launch_intent,
    intents_dir,
)


def test_classify_by_extension(tmp_path: Path) -> None:
    assert classify_launch_path(Path('a/b.htdtproject')) == 'open_project'
    assert classify_launch_path(Path('b/c.htdtcapture')) == 'preview_capture'
    assert classify_launch_path(Path('c/d.htdt-backup')) == 'preview_backup'
    assert classify_launch_path(Path('e/f.zip')) == 'unknown'
    assert classify_launch_path(Path('g/H.BACKUP')) == 'unknown'


def test_project_descriptor_document_id_extracted(tmp_path: Path) -> None:
    ref = tmp_path / 'room.htdtproject'
    ref.write_text(
        json.dumps({
            'kind': 'htdt-project-ref',
            'schema_version': 1,
            'project_id': 'p-1',
            'document_id': 'doc-1',
            'display_name': 'Living Room',
        }),
        encoding='utf-8',
    )
    intent = build_launch_intent(ref)
    assert intent.kind == 'open_project'
    assert intent.document_id == 'doc-1'
    assert intent.detail == 'Living Room'
    assert intent.source == 'command_line'


def test_invalid_descriptor_still_classifies_by_extension(tmp_path: Path) -> None:
    ref = tmp_path / 'broken.htdtproject'
    ref.write_bytes(b'\x00\x01\xff not json')
    intent = build_launch_intent(ref)
    assert intent.kind == 'open_project'
    assert intent.document_id is None


def test_forward_and_drain_roundtrip(tmp_path: Path) -> None:
    intent = build_launch_intent(
        Path('data.htdt-backup'), source='forwarded'
    )
    dropped = forward_launch_intent(tmp_path, intent)
    assert dropped.is_file()
    assert dropped.parent == intents_dir(tmp_path) / 'incoming'

    drained = drain_launch_intents(tmp_path)
    assert [q.intent for q in drained] == [intent]
    # #736: the queue file survives until the dispatch completes — a
    # second drain before completion redelivers the intent.
    assert drain_launch_intents(tmp_path) == drained
    for queued in drained:
        complete_queued_intent(queued, succeeded=True)
    assert drain_launch_intents(tmp_path) == ()
    assert (intents_dir(tmp_path) / 'done' / dropped.name).is_file()


def test_failed_dispatch_moves_to_failed(tmp_path: Path) -> None:
    intent = build_launch_intent(
        Path('broken.htdtcapture'), source='forwarded'
    )
    dropped = forward_launch_intent(tmp_path, intent)
    (queued,) = drain_launch_intents(tmp_path)
    complete_queued_intent(queued, succeeded=False)
    assert not dropped.exists()
    assert (intents_dir(tmp_path) / 'failed' / dropped.name).is_file()
    assert drain_launch_intents(tmp_path) == ()


def test_malformed_intent_moves_to_dead(tmp_path: Path) -> None:
    incoming = intents_dir(tmp_path) / 'incoming'
    incoming.mkdir(parents=True)
    bad = incoming / 'bad.json'
    bad.write_text('{"nope": 1}', encoding='utf-8')

    assert drain_launch_intents(tmp_path) == ()
    assert not bad.exists()
    assert (intents_dir(tmp_path) / 'dead' / 'bad.json').is_file()


def test_multiple_intents_drain_in_order(tmp_path: Path) -> None:
    paths = ['a.htdtproject', 'b.htdtcapture', 'c.htdt-backup']
    for path in paths:
        forward_launch_intent(
            tmp_path, build_launch_intent(Path(path), source='forwarded')
        )
    drained = drain_launch_intents(tmp_path)
    assert [Path(q.intent.path).name for q in drained] == sorted(paths)


def test_describe_intent_is_human_readable(tmp_path: Path) -> None:
    intent = build_launch_intent(Path('archives/x.htdt-backup'))
    text = describe_launch_intent(intent)
    assert 'x.htdt-backup' in text
    assert 'バックアップ' in text


def test_activation_intent_roundtrip(tmp_path: Path) -> None:
    """A bare second launch queues 'activate' for the running instance."""

    intent = build_activation_intent(tmp_path)
    assert intent.kind == 'activate'
    assert intent.path == str(tmp_path)
    assert intent.source == 'forwarded'

    forward_launch_intent(tmp_path, intent)
    (queued,) = drain_launch_intents(tmp_path)
    assert queued.intent == intent
    assert 'HTDT' in describe_launch_intent(queued.intent)
    complete_queued_intent(queued, succeeded=True)
    assert drain_launch_intents(tmp_path) == ()


def test_deeply_nested_descriptor_still_classifies_by_extension(
    tmp_path: Path,
) -> None:
    # json.loads reports a deeply nested descriptor as RecursionError;
    # it must take the same unreadable-descriptor path as torn JSON.
    ref = tmp_path / 'deep.htdtproject'
    ref.write_text('[' * 3000 + ']' * 3000, encoding='utf-8')
    intent = build_launch_intent(ref)
    assert intent.kind == 'open_project'
    assert intent.document_id is None
