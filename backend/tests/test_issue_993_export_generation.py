"""Issue #993 — multi-file export as one generation: no overwrite, no
partial publication, concurrent-writer safety.

The generation contract: members are staged inside ``<stem>.export-staging``
(the atomic ``mkdir`` IS the reservation), hashed into ``manifest.json``,
then the staging directory is renamed to ``<stem>/`` — a single
publication point. A crashed writer leaves a diagnosable staging dir;
a published generation is never overwritten.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from htdt import export_io
from htdt.export_io import (
    cleanup_export_staging,
    find_export_staging,
    iter_export_generations,
    verify_export_generation,
    write_export_generation,
)


_FILES = {'a.csv': 'a1\n', 'b.json': '{}\n', 'c.html': '<html/>\n'}


def test_generation_publishes_as_single_directory(tmp_path) -> None:
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    assert generation.stem == 'analysis'
    assert generation.directory == tmp_path / 'analysis'
    for name, content in _FILES.items():
        assert (generation.directory / name).read_text(
            encoding='utf-8'
        ) == content
    manifest = json.loads(
        (generation.directory / 'manifest.json').read_text(
            encoding='utf-8'
        )
    )
    assert manifest['schema'] == 'export-generation/v1'
    assert manifest['member_count'] == 3
    assert set(manifest['members']) == set(_FILES)
    # No staging residue after a successful publish.
    assert find_export_staging(tmp_path) == ()
    verify_export_generation(generation.directory)


def test_existing_generation_never_overwritten(tmp_path) -> None:
    first = write_export_generation(tmp_path, 'analysis', _FILES)
    second = write_export_generation(
        tmp_path, 'analysis', {'a.csv': 'different\n'}
    )
    assert second.stem == 'analysis-2'
    # The first generation's bytes are untouched.
    assert (first.directory / 'a.csv').read_text(encoding='utf-8') == 'a1\n'


def test_legacy_flat_member_bumps_stem(tmp_path) -> None:
    # Pre-change layout: flat ``<stem>_<member>`` files still mark a stem
    # occupied, so numbering stays monotonic and old artifacts survive.
    (tmp_path / 'analysis_export.json').write_text('{}', encoding='utf-8')
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    assert generation.stem == 'analysis-2'


def test_concurrent_writers_get_distinct_stems(tmp_path) -> None:
    barrier = threading.Barrier(5)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                write_export_generation(tmp_path, 'analysis', _FILES)
            )
        except BaseException as exc:  # pragma: no cover - assertion path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stems = sorted(g.stem for g in results)
    assert stems == [
        'analysis',
        'analysis-2',
        'analysis-3',
        'analysis-4',
        'analysis-5',
    ]
    # Every published generation verifies; nothing partially published.
    assert find_export_staging(tmp_path) == ()
    for generation in results:
        verify_export_generation(generation.directory)


def test_failure_mid_staging_leaves_no_partial_publication(
    tmp_path, monkeypatch
) -> None:
    real = export_io.write_text_atomic
    calls = {'n': 0}

    def die_on_second(path, content, *, encoding='utf-8'):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('simulated kill mid-generation')
        return real(path, content, encoding=encoding)

    monkeypatch.setattr(
        export_io, 'write_text_atomic', die_on_second
    )
    with pytest.raises(RuntimeError):
        write_export_generation(tmp_path, 'analysis', _FILES)
    assert not (tmp_path / 'analysis').exists()
    # The staging dir was cleaned by the failing call itself.
    assert find_export_staging(tmp_path) == ()


def test_failure_at_publication_point_leaves_no_partial(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        export_io.os,
        'rename',
        lambda *a, **k: (_ for _ in ()).throw(
            OSError('simulated kill at rename')
        ),
    )
    with pytest.raises(OSError):
        write_export_generation(tmp_path, 'analysis', _FILES)
    assert not (tmp_path / 'analysis').exists()
    assert find_export_staging(tmp_path) == ()


def test_crashed_staging_blocks_stem_and_cleans_up(tmp_path) -> None:
    # A killed writer leaves its staging dir behind (except-path never ran).
    orphan = tmp_path / 'analysis.export-staging'
    orphan.mkdir()
    (orphan / 'a.csv').write_text('partial\n', encoding='utf-8')

    assert find_export_staging(tmp_path) == (orphan,)
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    # The crashed generation's stem is not silently reused.
    assert generation.stem == 'analysis-2'
    # Orphan evidence is still there until explicitly cleaned.
    assert (orphan / 'a.csv').exists()
    removed = cleanup_export_staging(tmp_path)
    assert removed == (orphan,)
    assert not orphan.exists()
    verify_export_generation(generation.directory)


def test_squatted_final_name_fails_closed(tmp_path, monkeypatch) -> None:
    # Another actor creates the final name between our reservation and
    # publication — the write must fail rather than merge into it.
    real_rename = os.rename

    def squat(src, dst):
        Path(dst).mkdir()
        (Path(dst) / 'foreign.txt').write_text('x', encoding='utf-8')
        return real_rename(src, dst)

    calls = {'n': 0}

    def once(src, dst):
        calls['n'] += 1
        if calls['n'] == 1:
            return squat(src, dst)
        return real_rename(src, dst)

    monkeypatch.setattr(export_io.os, 'rename', once)
    with pytest.raises(OSError):
        write_export_generation(tmp_path, 'analysis', _FILES)
    # Foreign dir contents are untouched; our staging is cleaned.
    assert (tmp_path / 'analysis' / 'foreign.txt').exists()
    assert not (tmp_path / 'analysis' / 'a.csv').exists()
    assert find_export_staging(tmp_path) == ()


def test_unwritable_directory_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        export_io.os,
        'mkdir',
        lambda *a, **k: (_ for _ in ()).throw(
            PermissionError('read-only directory')
        ),
    )
    with pytest.raises(PermissionError):
        write_export_generation(tmp_path, 'analysis', _FILES)


def test_verify_rejects_corrupt_member(tmp_path) -> None:
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    member = generation.directory / 'a.csv'
    member.write_bytes(member.read_bytes() + b'x')
    with pytest.raises(ValueError, match='corrupt'):
        verify_export_generation(generation.directory)


def test_verify_rejects_missing_member(tmp_path) -> None:
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    (generation.directory / 'b.json').unlink()
    with pytest.raises(ValueError, match='missing'):
        verify_export_generation(generation.directory)


def test_verify_rejects_unmanifested_extra(tmp_path) -> None:
    generation = write_export_generation(tmp_path, 'analysis', _FILES)
    (generation.directory / 'sneaky.csv').write_text('x', encoding='utf-8')
    with pytest.raises(ValueError, match='unmanifested'):
        verify_export_generation(generation.directory)


def test_verify_rejects_foreign_directory(tmp_path) -> None:
    foreign = tmp_path / 'random'
    foreign.mkdir()
    with pytest.raises(ValueError, match='manifest missing'):
        verify_export_generation(foreign)


def test_iter_export_generations_lists_published(tmp_path) -> None:
    write_export_generation(tmp_path, 'analysis', {'a.csv': '1'})
    write_export_generation(tmp_path, 'analysis', {'a.csv': '2'})
    write_export_generation(tmp_path, 'calibration', {'s.json': '{}'})
    (tmp_path / 'unrelated-dir').mkdir()
    stems = [
        g.stem for g in iter_export_generations(tmp_path, 'analysis')
    ]
    assert stems == ['analysis', 'analysis-2']


def test_member_name_validation(tmp_path) -> None:
    for bad in ('../x.csv', 'a/b.csv', 'a\\b.csv', 'manifest.json', ''):
        with pytest.raises(ValueError, match='member name'):
            write_export_generation(tmp_path, 'analysis', {bad: 'x'})


def test_empty_file_set_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        write_export_generation(tmp_path, 'analysis', {})


def test_manifest_extra_pins_source_identity(tmp_path) -> None:
    generation = write_export_generation(
        tmp_path,
        'calibration',
        {'settings.json': '{}'},
        manifest_extra={'export_id': 'exp-1'},
    )
    assert generation.manifest['extra'] == {'export_id': 'exp-1'}


def test_bom_suffix_member_encoding(tmp_path) -> None:
    generation = write_export_generation(
        tmp_path,
        'analysis',
        {'export.csv': 'héllo\n', 'export.json': '{}'},
        bom_suffixes=('.csv',),
    )
    assert (generation.directory / 'export.csv').read_bytes().startswith(
        b'\xef\xbb\xbf'
    )
    assert not (
        generation.directory / 'export.json'
    ).read_bytes().startswith(b'\xef\xbb\xbf')


def test_write_denied_temp_creation_fails_fast(tmp_path, monkeypatch) -> None:
    # ACL-denied dir: mkstemp retried ~10000x (minutes); ours fails once.
    attempts = {'n': 0}

    def denied(*args, **kwargs):
        attempts['n'] += 1
        raise PermissionError('access denied')

    monkeypatch.setattr(export_io.os, 'open', denied)
    with pytest.raises(PermissionError):
        export_io.write_text_atomic(tmp_path / 'out.txt', 'x')
    assert attempts['n'] == 1


def test_temp_name_collision_retries_then_succeeds(tmp_path, monkeypatch) -> None:
    real_open = os.open
    attempts = {'n': 0}

    def collide_once(*args, **kwargs):
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise FileExistsError('name taken')
        return real_open(*args, **kwargs)

    monkeypatch.setattr(export_io.os, 'open', collide_once)
    path = export_io.write_text_atomic(tmp_path / 'out.txt', 'ok')
    assert attempts['n'] == 2
    assert path.read_text(encoding='utf-8') == 'ok'
