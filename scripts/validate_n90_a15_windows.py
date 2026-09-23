from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


DOCUMENT_ID = 'fixture-f1'
DATABASE_NAME = 'cad-scenes.sqlite3'
WM_CLOSE = 0x0010


if sys.platform != 'win32':
    raise SystemExit('N90 A15 acceptance requires Windows.')


def _run(arguments: list[str], *, timeout: float = 240.0) -> None:
    completed = subprocess.run(
        arguments,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f'command failed with exit code {completed.returncode}: {arguments!r}'
        )


def _install(installer: Path, install_root: Path) -> Path:
    _run([
        str(installer),
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        f'/DIR={install_root}',
    ])
    executable = install_root / 'HTDT' / 'HTDT.exe'
    if not executable.is_file():
        raise AssertionError(f'installed executable is missing: {executable}')
    _run([str(executable), '--version'], timeout=60.0)
    return executable


def _uninstall(install_root: Path) -> None:
    uninstaller = install_root / 'unins000.exe'
    if not uninstaller.is_file():
        raise AssertionError(f'uninstaller is missing: {uninstaller}')
    _run([
        str(uninstaller),
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
    ])
    executable = install_root / 'HTDT' / 'HTDT.exe'
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and (executable.exists() or uninstaller.exists()):
        time.sleep(0.1)
    if executable.exists():
        raise AssertionError('application executable remains after uninstall')
    if uninstaller.exists():
        raise AssertionError('uninstaller remains after uninstall cleanup window')


def _user32():
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def _visible_window_for_pid(process_id: int) -> int | None:
    user32 = _user32()
    found: list[int] = []

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    @callback_type
    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process_id and user32.IsWindowVisible(hwnd):
            title_length = user32.GetWindowTextLengthW(hwnd)
            if title_length > 0:
                found.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def _launch_and_close(executable: Path, data_dir: Path) -> tuple[str, str]:
    process = subprocess.Popen([
        str(executable),
        '--data-dir',
        str(data_dir),
        '--document-id',
        DOCUMENT_ID,
    ])
    database = data_dir / DATABASE_NAME
    hwnd: int | None = None
    deadline = time.monotonic() + 45.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f'GUI exited before acceptance could close it: {process.returncode}'
                )
            if database.is_file():
                hwnd = _visible_window_for_pid(process.pid)
                if hwnd is not None:
                    break
            time.sleep(0.1)
        if hwnd is None:
            raise AssertionError('visible HTDT GUI window did not appear')

        user32 = _user32()
        if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            exit_code = process.wait(timeout=15.0)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError('HTDT GUI did not exit after WM_CLOSE') from exc
        if exit_code != 0:
            raise AssertionError(f'HTDT GUI exited with code {exit_code}')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)

    return _latest_revision(data_dir)


def _database_health(data_dir: Path) -> None:
    database = data_dir / DATABASE_NAME
    if not database.is_file():
        raise AssertionError(f'native database is missing: {database}')
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise AssertionError('native database integrity_check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise AssertionError('native database foreign_key_check failed')


def _latest_revision(data_dir: Path) -> tuple[str, str]:
    _database_health(data_dir)
    database = data_dir / DATABASE_NAME
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            'SELECT revision_id, content_hash FROM scene_revisions '
            'WHERE document_id=? ORDER BY seq DESC LIMIT 1',
            (DOCUMENT_ID,),
        ).fetchone()
    if row is None:
        raise AssertionError('F1 SceneRevision is missing')
    return str(row[0]), str(row[1])


def _add_post_backup_marker(data_dir: Path) -> None:
    database = data_dir / DATABASE_NAME
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'CREATE TABLE n90_acceptance_marker(value TEXT NOT NULL)'
        )
        connection.execute(
            'INSERT INTO n90_acceptance_marker(value) VALUES (?)',
            ('created-after-backup',),
        )


def _marker_exists(data_dir: Path) -> bool:
    database = data_dir / DATABASE_NAME
    with closing(sqlite3.connect(database)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='n90_acceptance_marker'"
        ).fetchone()
    return table is not None


def _run_gate(
    baseline_installer: Path,
    update_installer: Path,
    work_root: Path,
) -> bool:
    install_root = work_root / 'program'
    data_dir = work_root / 'user-data'
    backup_dir = work_root / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / 'baseline.htdt-backup'
    sentinel = data_dir / 'a15-user-data-retention.txt'

    gate_error: BaseException | None = None
    try:
        baseline_exe = _install(baseline_installer, install_root)
        baseline_revision, baseline_hash = _launch_and_close(baseline_exe, data_dir)
        print('A15_BASELINE_GUI_SEEDED', True, flush=True)
        print('A15_BASELINE_REVISION', baseline_revision, flush=True)

        _run([
            str(baseline_exe),
            '--data-dir',
            str(data_dir),
            '--backup',
            str(backup_path),
        ])
        backup_ok = backup_path.is_file() and backup_path.stat().st_size > 0
        print('A15_BACKUP_CREATED', backup_ok, flush=True)
        if not backup_ok:
            return False

        _add_post_backup_marker(data_dir)
        sentinel.write_text('retain-me', encoding='utf-8')
        if not _marker_exists(data_dir):
            return False

        update_exe = _install(update_installer, install_root)
        update_preserved = (
            _marker_exists(data_dir)
            and sentinel.is_file()
            and _latest_revision(data_dir) == (baseline_revision, baseline_hash)
        )
        print('A15_UPDATE_PRESERVED_DATA', update_preserved, flush=True)
        if not update_preserved:
            return False

        _run([
            str(update_exe),
            '--data-dir',
            str(data_dir),
            '--restore',
            str(backup_path),
        ])
        restore_ok = (
            not _marker_exists(data_dir)
            and sentinel.is_file()
            and _latest_revision(data_dir) == (baseline_revision, baseline_hash)
        )
        print('A15_RESTORE_EXACT', restore_ok, flush=True)
        if not restore_ok:
            return False

        reopened_revision = _launch_and_close(update_exe, data_dir)
        reopen_ok = reopened_revision == (baseline_revision, baseline_hash)
        print('A15_REOPEN_AFTER_RESTORE', reopen_ok, flush=True)
        if not reopen_ok:
            return False

        _uninstall(install_root)
        uninstall_retained = (
            not (install_root / 'HTDT' / 'HTDT.exe').exists()
            and (data_dir / DATABASE_NAME).is_file()
            and sentinel.is_file()
        )
        print('A15_UNINSTALL_RETAINED_USER_DATA', uninstall_retained, flush=True)
        if not uninstall_retained:
            return False

        reinstalled_exe = _install(update_installer, install_root)
        reinstall_revision = _launch_and_close(reinstalled_exe, data_dir)
        reinstall_ok = (
            reinstall_revision == (baseline_revision, baseline_hash)
            and sentinel.is_file()
        )
        print('A15_REINSTALL_OPENED_RETAINED_DATA', reinstall_ok, flush=True)
        if not reinstall_ok:
            return False

        _uninstall(install_root)
        final_retention = (
            (data_dir / DATABASE_NAME).is_file()
            and sentinel.is_file()
            and _latest_revision(data_dir) == (baseline_revision, baseline_hash)
        )
        print('A15_FINAL_DATA_RETENTION', final_retention, flush=True)
        return final_retention

    except BaseException as exc:
        gate_error = exc
        raise
    finally:
        uninstaller = install_root / 'unins000.exe'
        if uninstaller.is_file():
            try:
                _uninstall(install_root)
            except Exception as cleanup_error:
                if gate_error is None:
                    raise
                print(
                    f'A15_CLEANUP_WARNING {cleanup_error}',
                    file=sys.stderr,
                    flush=True,
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Run N90 A15 installer/update/backup/restore acceptance.'
    )
    parser.add_argument('--baseline-installer', type=Path, required=True)
    parser.add_argument('--update-installer', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, default=None)
    parser.add_argument('--keep-work', action='store_true')
    args = parser.parse_args()

    baseline = args.baseline_installer.resolve()
    update = args.update_installer.resolve()
    if not baseline.is_file() or not update.is_file():
        raise SystemExit('Both baseline and update installer paths must exist.')

    if args.work_root is not None:
        root = args.work_root.resolve()
        if root.exists():
            raise SystemExit(f'work root must not already exist: {root}')
        root.mkdir(parents=True)
        cleanup = not args.keep_work
        try:
            passed = _run_gate(baseline, update, root)
        finally:
            if cleanup:
                shutil.rmtree(root, ignore_errors=True)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n90-a15-') as temp_name:
            passed = _run_gate(baseline, update, Path(temp_name))

    print('N90_A15_WINDOWS_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
