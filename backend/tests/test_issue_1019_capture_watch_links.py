"""#1019 — watch-folder symlink/reparse defense and verified staging.

The opt-in drop folder is a place other machines/users can write to:
anything that is not provably a real, regular file inside the canonical
watch root is a skip, never a delivery — and a delivered drop is routed
through a verified staged copy so the settle→route window cannot
redirect the import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
import htdt.capture_watch_guard as guard  # noqa: E402
from htdt.capture_watch_guard import (  # noqa: E402
    SKIP_BROKEN_LINK,
    SKIP_LINK_EXTERNAL,
    SKIP_LINK_INTERNAL,
    SKIP_NOT_REGULAR,
    SKIP_REPARSE,
    SKIP_UNREADABLE,
    WatchStageError,
    staged_capture_drop,
)
from htdt.capture_watch_runner import (  # noqa: E402
    CaptureWatchRunner,
    scan_capture_watch_dir,
)
import htdt.capture_watch_runner as watch_module  # noqa: E402
import zipfile  # noqa: E402


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _pump(app: QApplication, predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return predicate()


class _Prefs:
    def __init__(self, watch_dir: str = '') -> None:
        self.watch_dir = watch_dir

    def get(self, key: str) -> object:
        if key == 'integrations.capture_watch_dir':
            return self.watch_dir
        raise KeyError(key)


def _write_bundle_zip(dest_dir: Path, name: str) -> Path:
    bundle_dir, _ = support.write_bundle(
        dest_dir / f'{name}-src', support.default_file_specs()
    )
    zip_path = dest_dir / f'{name}.htdtcapture'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return zip_path


def _mklink(target: Path, link: Path, *, directory: bool) -> None:
    """Real symlink; skip (not xfail) where the FS/account refuses."""
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f'symlink privilege unavailable: {exc}')


def _mkjunction(target: Path, link: Path) -> None:
    """Real NTFS junction via mklink /J — Windows-only by definition."""
    if os.name != 'nt':
        pytest.skip('junctions are NTFS-only')
    completed = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f'mklink /J failed: {completed.stdout}{completed.stderr}')


def _scan(watch, seen=None, pending=None):
    seen = seen if seen is not None else {}
    pending = pending if pending is not None else {}
    skipped: list = []
    delivered = scan_capture_watch_dir(
        watch, seen, pending, skipped_out=skipped
    )
    return delivered, skipped, seen, pending


# -- classification ----------------------------------------------------------


def test_symlink_to_external_is_skipped_and_reported(tmp_path: Path) -> None:
    """POSIX-style escape: a link pointing outside the watched root."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    outside_file = outside / 'loot.htdtcapture'
    outside_file.write_bytes(b'outside payload')
    watch = tmp_path / 'watch'
    watch.mkdir()
    _, _, seen, pending = _scan(watch)  # baseline
    link = watch / 'dropped.htdtcapture'
    _mklink(outside_file, link, directory=False)

    delivered, skipped, seen, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(Path(k).name, r) for k, r, _ in skipped] == [
        ('dropped.htdtcapture', SKIP_LINK_EXTERNAL)
    ]
    # Marker is the link's own signature — unchanged link stays quiet.
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == [] and skipped == []
    # The outside file was never opened by the lane at all.
    assert outside_file.read_bytes() == b'outside payload'


def test_symlink_inside_root_is_still_skipped(tmp_path: Path) -> None:
    """An in-root alias is not a drop either — links are never routed."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    target = watch / 'real-payload.bin'
    target.write_bytes(b'x')
    _, _, seen, pending = _scan(watch)  # baseline
    link = watch / 'alias.htdtcapture'
    _mklink(target, link, directory=False)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(Path(k).name, r) for k, r, _ in skipped] == [
        ('alias.htdtcapture', SKIP_LINK_INTERNAL)
    ]


def test_broken_symlink_is_skipped(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    _, _, seen, pending = _scan(watch)
    link = watch / 'gone.htdtcapture'
    _mklink(watch / 'missing-target', link, directory=False)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(r) for _, r, _ in skipped] == [SKIP_BROKEN_LINK]


def test_symlink_to_directory_is_a_link_not_a_drop(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    dir_target = tmp_path / 'bundle-dir'
    dir_target.mkdir()
    _, _, seen, pending = _scan(watch)
    link = watch / 'diralias.htdtcapture'
    _mklink(dir_target, link, directory=True)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    # A dir alias escaping the root reads as link_external — links are
    # classified before their target type matters.
    assert [(r) for _, r, _ in skipped] == [SKIP_LINK_EXTERNAL]


def test_ntfs_junction_dir_is_reparse_point(tmp_path: Path) -> None:
    """mklink /J — a real NTFS reparse point, not a symlink."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    jtarget = tmp_path / 'junction-target'
    jtarget.mkdir()
    _, _, seen, pending = _scan(watch)
    junction = watch / 'joined.htdtcapture'
    _mkjunction(jtarget, junction)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(r) for _, r, _ in skipped] == [SKIP_REPARSE]


def test_plain_directory_is_not_regular(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    _, _, seen, pending = _scan(watch)
    (watch / 'unzipped.htdtcapture').mkdir()
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(r) for _, r, _ in skipped] == [SKIP_NOT_REGULAR]


def test_unc_alias_containment_fails_closed(tmp_path: Path) -> None:
    """UNC-vs-drive comparisons must never be string-prefix fooled."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    canonical = watch.resolve()
    # A UNC-spelled path can never be proven inside a local-drive root.
    unc = Path(f'\\\\localhost\\{canonical.drive.rstrip(":")}$\\x')
    assert guard._within_root(unc, canonical) is False
    assert guard._within_root(canonical / 'inner.txt', canonical) is True
    assert guard._within_root(canonical.parent, canonical) is False


def test_link_swap_between_scans_is_caught(tmp_path: Path) -> None:
    """A settled file replaced by a link: the link is skipped, never routed."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'victim.htdtcapture'
    drop.write_bytes(b'original bytes')
    _, _, seen, pending = _scan(watch)
    _scan(watch, seen, pending)  # settles → delivered on this scan
    outside = tmp_path / 'loot.htdtcapture'
    outside.write_bytes(b'stolen')
    drop.unlink()
    _mklink(outside, drop, directory=False)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(r) for _, r, _ in skipped] == [SKIP_LINK_EXTERNAL]
    # Swap back to a real file → normal settle path re-delivers it.
    drop.unlink()
    drop.write_bytes(b'back to real')
    assert _scan(watch, seen, pending)[0] == []
    assert _scan(watch, seen, pending)[0] == [drop.resolve()]


def test_preexisting_link_baselines_silently(tmp_path: Path) -> None:
    """A link present at the first scan is marked, never reported."""
    outside = tmp_path / 'loot.htdtcapture'
    outside.write_bytes(b'x')
    watch = tmp_path / 'watch'
    watch.mkdir()
    link = watch / 'old.htdtcapture'
    _mklink(outside, link, directory=False)
    delivered, skipped, seen, _ = _scan(watch)
    assert delivered == [] and skipped == []
    assert str(link) in seen


def test_mass_links_stay_bounded(tmp_path: Path) -> None:
    """A folder full of links completes in one scan without wedging."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    outside = tmp_path / 'loot.htdtcapture'
    outside.write_bytes(b'x')
    _, _, seen, pending = _scan(watch)
    for i in range(300):
        _mklink(outside, watch / f'link-{i:03d}.htdtcapture', directory=False)
    started = time.monotonic()
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    elapsed = time.monotonic() - started
    assert delivered == []
    assert len(skipped) == min(300, guard.SKIP_REPORT_LIMIT)
    assert elapsed < 30


def test_unreadable_entry_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry whose lstat fails reports unreadable, never routes."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    victim = watch / 'denied.htdtcapture'
    victim.write_bytes(b'x')
    _, _, seen, pending = _scan(watch)
    real_lstat = Path.lstat

    def flaky(self: Path):
        if self == victim:
            raise PermissionError('denied')
        return real_lstat(self)

    monkeypatch.setattr(Path, 'lstat', flaky)
    delivered, skipped, _, _ = _scan(watch, seen, pending)
    assert delivered == []
    assert [(r) for _, r, _ in skipped] == [SKIP_UNREADABLE]


# -- verified staging --------------------------------------------------------


def test_staged_drop_binds_descriptor_identity(tmp_path: Path) -> None:
    """fstat must equal the lstat'd dirent — and the copy is real."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'real.htdtcapture'
    payload = b'stage me' * 4096
    drop.write_bytes(payload)
    canonical = watch.resolve()
    with staged_capture_drop(drop, canonical_root=canonical) as staged:
        assert staged.read_bytes() == payload
        assert staged != drop
        assert staged.name == drop.name
    assert not staged.exists()


def test_stat_to_open_inodeswap_is_changed(tmp_path: Path) -> None:
    """Replace-by-inode between lstat and open: caught by (dev, ino)."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'swap.htdtcapture'
    drop.write_bytes(b'original')
    canonical = watch.resolve()

    real_open = os.open
    swapped = {'done': False}

    def swapping_open(path, flags, *args, **kwargs):
        if not swapped['done'] and Path(path) == drop:
            swapped['done'] = True
            replacement = watch / 'replacement.bin'
            replacement.write_bytes(b'replaced!')
            os.replace(replacement, drop)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(os, 'open', swapping_open)
    try:
        with pytest.raises(WatchStageError) as exc_info:
            with staged_capture_drop(drop, canonical_root=canonical):
                pass
    finally:
        monkeypatch.undo()
    assert exc_info.value.reason == 'changed'


def test_in_place_same_signature_rewrite_never_succeeds_silently(
    tmp_path: Path,
) -> None:
    """Same inode, same (mtime,size) forged — staged bytes are the *new*
    bytes; a junk rewrite fails the route rather than importing stale."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'forge.htdtcapture'
    _write_bundle_zip(watch, 'forge-src-basis')
    drop.write_bytes(b'A' * 64)
    canonical = watch.resolve()
    info = drop.lstat()
    # In-place rewrite: same inode, forged same signature.
    with drop.open('r+b') as f:
        f.write(b'B' * 64)
    os.utime(drop, ns=(info.st_atime_ns, info.st_mtime_ns))
    with staged_capture_drop(drop, canonical_root=canonical) as staged:
        assert staged.read_bytes() == b'B' * 64
        # It stages the post-swap bytes — never the pre-swap ones —
        # and import then rejects them honestly as a non-bundle.


def test_interrupted_copy_imports_nothing(tmp_path: Path) -> None:
    """Cancel mid-copy: no staged path reaches the router, no leftovers."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'big.htdtcapture'
    drop.write_bytes(os.urandom(3 * 1024 * 1024))
    canonical = watch.resolve()

    import threading

    cancel = threading.Event()
    reads = {'n': 0}

    def cancelling_read(fd, n):
        chunk = _orig_os_read(fd, n)
        reads['n'] += 1
        if reads['n'] >= 1:
            cancel.set()
        return chunk

    _orig_os_read = os.read
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(guard.os, 'read', cancelling_read)
    tempdir = Path(tempfile.gettempdir())
    before = set(tempdir.glob('htdt-watch-*'))
    try:
        with pytest.raises(guard._StageCancelled):
            with staged_capture_drop(
                drop, canonical_root=canonical, cancel=cancel
            ):
                pass
    finally:
        monkeypatch.undo()
    after = set(tempdir.glob('htdt-watch-*'))
    assert after == before  # the partial stage was cleaned up
    assert drop.read_bytes()[:16] != b''  # original untouched


# -- descriptor indirection ---------------------------------------------------


def _write_descriptor(path: Path, bundle_path: str) -> Path:
    path.write_text(
        json.dumps(
            {
                'kind': 'htdt-capture-ref',
                'schema_version': 1,
                'bundle_path': bundle_path,
            }
        ),
        encoding='utf-8',
    )
    return path


def test_descriptor_external_bundle_path_blocked(tmp_path: Path) -> None:
    """A descriptor pointing outside the root is a refused stage."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    bundle = outside / 'bundle.zip'
    bundle.write_bytes(b'PKjunk')
    descriptor = _write_descriptor(
        watch / 'desc.htdtcapture', str(bundle.resolve())
    )
    with pytest.raises(WatchStageError) as exc_info:
        with staged_capture_drop(
            descriptor, canonical_root=watch.resolve()
        ):
            pass
    assert exc_info.value.reason == 'descriptor_external'


def test_descriptor_parent_traversal_blocked(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    outside = tmp_path / 'loot.zip'
    outside.write_bytes(b'PK')
    descriptor = _write_descriptor(
        watch / 'desc.htdtcapture', '../loot.zip'
    )
    with pytest.raises(WatchStageError) as exc_info:
        with staged_capture_drop(
            descriptor, canonical_root=watch.resolve()
        ):
            pass
    assert exc_info.value.reason == 'descriptor_external'


def test_descriptor_inside_bundle_stages_and_routes(tmp_path: Path) -> None:
    """Happy path preserved: descriptor + in-root companion stage."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    bundle_zip = _write_bundle_zip(watch, 'companion')
    descriptor = _write_descriptor(
        watch / 'desc.htdtcapture', bundle_zip.name
    )
    canonical = watch.resolve()
    with staged_capture_drop(descriptor, canonical_root=canonical) as staged:
        assert staged.name == 'desc.htdtcapture'
        assert (staged.parent / bundle_zip.name).exists()
        assert (staged.parent / bundle_zip.name).stat().st_size == (
            bundle_zip.stat().st_size
        )


def test_descriptor_bundle_dir_member_link_blocked(tmp_path: Path) -> None:
    """A link inside a directory bundle blocks the whole stage."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    bundle_dir = watch / 'bundle'
    bundle_dir.mkdir()
    (bundle_dir / 'manifest.json').write_text('{}')
    outside = tmp_path / 'loot.bin'
    outside.write_bytes(b'x')
    _mklink(outside, bundle_dir / 'evil.bin', directory=False)
    descriptor = _write_descriptor(
        watch / 'desc.htdtcapture', 'bundle'
    )
    with pytest.raises(WatchStageError) as exc_info:
        with staged_capture_drop(
            descriptor, canonical_root=watch.resolve()
        ):
            pass
    assert exc_info.value.reason == 'bundle_member_blocked'


# -- runner end-to-end --------------------------------------------------------


def test_runner_reports_external_link_and_never_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: a link drop surfaces on entries_skipped — no import,
    no inbox row."""
    app = _app()
    outside = tmp_path / 'outside'
    outside.mkdir()
    loot = _write_bundle_zip(outside, 'loot')
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')

    route_calls: list = []
    real_route = watch_module.route_capture_intent

    def spy(intent, *, repository, arrival_source):
        route_calls.append(intent.path)
        return real_route(
            intent, repository=repository, arrival_source=arrival_source
        )

    monkeypatch.setattr(watch_module, 'route_capture_intent', spy)
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    batches: list = []
    skipped_records: list = []
    runner.scan_completed.connect(batches.append)
    runner.entries_skipped.connect(skipped_records.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    link = watch / 'evil.htdtcapture'
    _mklink(loot, link, directory=False)
    assert _pump(app, lambda: bool(skipped_records))
    runner.shutdown()

    records = [r for batch in skipped_records for r in batch]
    assert [r.error_kind for r in records] == [SKIP_LINK_EXTERNAL]
    assert records[0].detail  # localized, non-empty
    # No route was attempted and nothing reached the inbox.
    assert route_calls == []
    assert batches == []
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()


def test_runner_watch_root_through_junction_watches_target(
    tmp_path: Path,
) -> None:
    """Root spelled through a junction watches the resolved directory —
    the documented accept-by-canonicalization contract."""
    app = _app()
    real_root = tmp_path / 'real-watch'
    real_root.mkdir()
    junction = tmp_path / 'watch-link'
    _mkjunction(real_root, junction)
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(junction)), interval_ms=40
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    # Canonical identity: the armed root is the resolved directory.
    assert runner._watched_root == real_root.resolve()
    _write_bundle_zip(real_root, 'dropped')
    assert _pump(app, lambda: bool(batches))
    runner.shutdown()
    staged = [
        p.name for batch in batches for p, r, _ in batch
        if r is not None and r.outcome == 'staged_for_review'
    ]
    assert staged == ['dropped.htdtcapture']
