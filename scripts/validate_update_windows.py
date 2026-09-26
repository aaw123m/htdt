"""Issue #811: validate an actual OLD-installed -> NEW-installer in-place
update using only packaged Windows artifacts.

Exercises the real user update path:

    install OLD (pinned) per-user
    -> launch OLD and seed a real data root
    -> add user sentinel content
    -> run NEW installer over the same install root/AppId
    -> launch NEW (schema/upgrade lifecycle runs on data-dir open)
    -> verify data/revision/asset preservation
    -> verify packaged-file replacement (no stale active files)
    -> verify file associations resolve to the installed executable
    -> uninstall NEW and confirm user data remains

The interactive Inno close prompt for a running HTDT instance is not
scriptable under /VERYSILENT; that subset stays an owned-Windows
acceptance case (issue #811 section C) and is recorded as such in the
report rather than silently skipped.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any


DATABASE_NAME = "cad-scenes.sqlite3"
SYNTHETIC_DOCUMENT_ID = "htdt-synthetic-o70-o80-demo-v1"
INSTALLER_NAME_RE = re.compile(r"^HTDT-Setup-(?P<version>.+)\.exe$")

_SILENT_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")


def _run(executable: Path, *arguments: str | Path) -> subprocess.CompletedProcess[str]:
    command = [str(executable), *(str(argument) for argument in arguments)]
    # The packaged app is windowed: an unhandled failure surfaces as a fatal
    # dialog on the invisible desktop instead of an exit, so a bare run()
    # would block forever. Bound each invocation to keep failures fast and
    # visible.
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "packaged HTDT command failed\n"
            f"command={command!r}\n"
            f"exit={completed.returncode}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    return completed


def _install(installer: Path, install_root: Path) -> None:
    completed = subprocess.run(
        [
            str(installer),
            *_SILENT_FLAGS,
            f"/DIR={install_root}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"installer {installer.name} failed\n"
            f"exit={completed.returncode}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )


def _uninstall(install_root: Path) -> None:
    uninstaller = install_root / "unins000.exe"
    if not uninstaller.is_file():
        raise RuntimeError(f"uninstaller missing: {uninstaller}")
    completed = subprocess.run(
        [str(uninstaller), *_SILENT_FLAGS],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"uninstall failed with exit code {completed.returncode}"
        )


def _file_manifest(root: Path) -> dict[str, str]:
    """sha256 of every file under root, keyed by POSIX relative path."""
    snapshot: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        snapshot[path.relative_to(root).as_posix()] = sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


def _installer_identity(path: Path) -> dict[str, str]:
    match = INSTALLER_NAME_RE.match(path.name)
    if match is None:
        raise RuntimeError(
            f"installer name does not carry the HTDT-Setup-<display> "
            f"convention: {path.name}"
        )
    return {"file": path.name, "display_version": match.group("version")}


def _integrity_check(database: Path) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"database missing: {database}")
    with sqlite3.connect(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"database integrity failure: {integrity!r}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(
                f"database foreign-key failure: {foreign_keys!r}"
            )


def _synthetic_document_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM scene_revisions WHERE document_id=?",
            (SYNTHETIC_DOCUMENT_ID,),
        ).fetchone()
    return 0 if row is None else int(row[0])


def _file_associations_resolve_to(executable: Path) -> dict[str, Any]:
    """Check HKCU class registrations for the three HTDT associations."""
    if sys.platform != "win32":
        return {"checked": False, "reason": "not a Windows host"}
    import winreg

    results: dict[str, Any] = {"checked": True}
    expected = str(executable.resolve()).lower()
    for extension, prog_id in (
        (".htdtproject", "HTDT.Project"),
        (".htdtcapture", "HTDT.Capture"),
        (".htdt-backup", "HTDT.Backup"),
    ):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{extension}",
            ) as key:
                registered_prog = winreg.QueryValue(key, None)
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{prog_id}\\shell\\open\\command",
            ) as key:
                command = winreg.QueryValue(key, None)
        except OSError as error:
            results[extension] = {
                "registered": False,
                "error": str(error),
            }
            continue
        results[extension] = {
            "registered": True,
            "prog_id": registered_prog,
            "command": command,
            "resolves_to_install": expected in command.lower(),
        }
    return results


def validate_update(
    old_installer: Path,
    new_installer: Path,
    work_dir: Path,
) -> dict[str, Any]:
    old_installer = old_installer.resolve()
    new_installer = new_installer.resolve()
    work_dir = work_dir.resolve()
    for installer in (old_installer, new_installer):
        if not installer.is_file():
            raise FileNotFoundError(f"installer missing: {installer}")

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    install_root = work_dir / "installed"
    app_root = install_root / "HTDT"
    executable = app_root / "HTDT.exe"
    data_root = work_dir / "user-data"

    # 1. Install the pinned OLD build per-user and launch it once.
    _install(old_installer, install_root)
    if not executable.is_file():
        raise RuntimeError(f"installed OLD executable missing: {executable}")
    old_manifest = _file_manifest(app_root)

    # 2. Seed a real data root and add user sentinel content.
    _run(executable, "--data-dir", data_root, "--seed-synthetic-demo")
    database = data_root / DATABASE_NAME
    _integrity_check(database)
    if _synthetic_document_count(database) <= 0:
        raise RuntimeError("OLD build did not seed the synthetic project")
    sentinel_text = "update-acceptance-user-sentinel"
    sentinel = data_root / "user-sentinel.txt"
    sentinel.write_text(sentinel_text, encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS update_sentinel(value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO update_sentinel(value) VALUES (?)",
            (sentinel_text,),
        )
        connection.commit()

    # 3. Install NEW into a clean reference root, then run it again over
    # the same install root and AppId: the updated tree must equal a clean
    # NEW install exactly (issue #811 B). The in-place update runs last so
    # the HKCU file associations point at the updated install — the same
    # end state a real user is left in.
    clean_root = work_dir / "clean-new-install"
    _install(new_installer, clean_root)
    new_payload = _file_manifest(clean_root / "HTDT")

    _install(new_installer, install_root)
    post_manifest = _file_manifest(app_root)
    if not executable.is_file():
        raise RuntimeError("NEW install removed the application executable")

    lingering = sorted(set(post_manifest) - set(new_payload))
    if lingering:
        raise RuntimeError(
            "obsolete OLD package files survived the in-place update: "
            + ", ".join(lingering[:20])
        )
    missing = sorted(set(new_payload) - set(post_manifest))
    if missing:
        raise RuntimeError(
            "NEW package files missing after the in-place update: "
            + ", ".join(missing[:20])
        )
    diverged = sorted(
        path
        for path in set(post_manifest) & set(new_payload)
        if post_manifest[path] != new_payload[path]
    )
    if diverged:
        raise RuntimeError(
            "updated files diverge from a clean NEW install: "
            + ", ".join(diverged[:20])
        )
    added_files = sorted(set(new_payload) - set(old_manifest))
    removed_files = sorted(set(old_manifest) - set(new_payload))

    # 5. Launch NEW on the OLD-seeded data root; the canonical upgrade
    # lifecycle runs when the data dir opens, then exercise it end to end.
    _run(executable, "--data-dir", data_root, "--version")
    _integrity_check(database)
    if _synthetic_document_count(database) <= 0:
        raise RuntimeError(
            "synthetic project missing after in-place update"
        )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT value FROM update_sentinel"
        ).fetchone()
    if row is None or row[0] != sentinel_text:
        raise RuntimeError("user sentinel row lost across the update")
    if sentinel.read_text(encoding="utf-8") != sentinel_text:
        raise RuntimeError("user sentinel file lost across the update")
    # The sentinel row already proved arbitrary user content survives the
    # update; the lane's probe table is not a managed authority table, so it
    # must not linger into --backup, whose authority audit fails closed on
    # unclassified persistent tables.
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE update_sentinel")
        connection.commit()
    backup = work_dir / "post-update.htdt-backup"
    _run(executable, "--data-dir", data_root, "--backup", backup)
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise RuntimeError("post-update packaged backup did not complete")

    # 6. File associations still resolve to the installed executable.
    associations = _file_associations_resolve_to(executable)
    unresolved = [
        extension
        for extension, entry in associations.items()
        if extension.startswith(".")
        and not entry.get("resolves_to_install")
    ]
    if unresolved:
        raise RuntimeError(
            "file associations do not resolve to the installed NEW "
            f"executable: {', '.join(unresolved)}"
        )

    # 7. Uninstall NEW and confirm user data remains.
    _uninstall(install_root)
    if executable.is_file():
        raise RuntimeError("application executable remains after uninstall")
    if not sentinel.is_file() or not database.is_file():
        raise RuntimeError("user data was removed by uninstall")

    report = {
        "acceptance": "issue-811-windows-in-place-update",
        "status": "pass",
        "old": _installer_identity(old_installer),
        "new": _installer_identity(new_installer),
        "install_root": str(install_root),
        "data_root": str(data_root),
        "old_file_count": len(old_manifest),
        "new_file_count": len(new_payload),
        "added_files": len(added_files),
        "obsolete_files_removed": len(removed_files),
        "updated_tree_equals_clean_install": True,
        "synthetic_document": SYNTHETIC_DOCUMENT_ID,
        "post_update_backup_size_bytes": backup.stat().st_size,
        "file_associations": associations,
        # Section C of the issue: an interactive Inno close prompt under a
        # running HTDT instance is not scriptable in silent CI; the
        # owned-Windows acceptance case covers it.
        "running_app_close": "covered by owned-Windows acceptance case",
    }
    report_path = work_dir / "update-acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Issue #811 in-place installer update acceptance: "
            "OLD-installed -> NEW-installer over one install root."
        )
    )
    parser.add_argument("--old-installer", type=Path, required=True)
    parser.add_argument("--new-installer", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    report = validate_update(
        args.old_installer,
        args.new_installer,
        args.work_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
