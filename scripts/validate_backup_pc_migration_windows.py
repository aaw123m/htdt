from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from typing import Any


DATABASE_NAME = "cad-scenes.sqlite3"
SYNTHETIC_DOCUMENT_ID = "htdt-synthetic-o70-o80-demo-v1"

_REQUIRED_POPULATED_TABLES = (
    "scene_revisions",
    "cad_measurement_assets",
    "cad_measurements",
    "cad_frequency_responses",
    "cad_search_specs",
    "cad_model_validations",
    "cad_adaptive_plans",
    "cad_extended_search_specs",
    "cad_adaptive_extended_plans",
)


def _run(executable: Path, *arguments: str | Path) -> subprocess.CompletedProcess[str]:
    command = [str(executable), *(str(argument) for argument in arguments)]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def _normalized_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_sha256": sha256(value).hexdigest(), "size_bytes": len(value)}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return repr(value)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_snapshot(path: Path) -> tuple[str, dict[str, int]]:
    if not path.is_file():
        raise FileNotFoundError(f"database missing: {path}")

    payload: list[dict[str, Any]] = []
    row_counts: dict[str, int] = {}
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"database integrity failure: {integrity!r}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"database foreign-key failure: {foreign_keys!r}")

        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            quoted = _quote_identifier(table)
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            schema_sql = None if schema_row is None else schema_row[0]
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            rows = [
                [_normalized_value(value) for value in row]
                for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()
            ]
            rows.sort(
                key=lambda row: json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            row_counts[table] = len(rows)
            payload.append(
                {
                    "table": table,
                    "schema_sql": schema_sql,
                    "columns": columns,
                    "rows": rows,
                }
            )

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest(), row_counts


def _asset_snapshot(data_dir: Path) -> dict[str, str]:
    root = data_dir / "measurement-assets"
    if not root.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(data_dir).as_posix()
        snapshot[relative] = sha256(path.read_bytes()).hexdigest()
    return snapshot


def _assert_fixture_coverage(row_counts: dict[str, int]) -> None:
    missing = [
        table
        for table in _REQUIRED_POPULATED_TABLES
        if row_counts.get(table, 0) <= 0
    ]
    if missing:
        raise RuntimeError(
            "synthetic migration fixture did not populate required HTDT authorities: "
            + ", ".join(missing)
        )


def validate_pc_migration(executable: Path, work_dir: Path) -> dict[str, Any]:
    executable = executable.resolve()
    work_dir = work_dir.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"packaged HTDT executable missing: {executable}")

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    old_pc = work_dir / "old-pc-data"
    new_pc = work_dir / "new-pc-data"
    backup = work_dir / "pc-migration.htdt-backup"

    _run(
        executable,
        "--data-dir",
        old_pc,
        "--seed-synthetic-demo",
    )

    old_database = old_pc / DATABASE_NAME
    old_database_sha, old_counts = _database_snapshot(old_database)
    _assert_fixture_coverage(old_counts)
    old_assets = _asset_snapshot(old_pc)
    if not old_assets:
        raise RuntimeError(
            "synthetic migration fixture did not create measurement assets"
        )

    backup_result = _run(
        executable,
        "--data-dir",
        old_pc,
        "--backup",
        backup,
    )
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise RuntimeError("packaged backup command did not create a migration archive")

    if new_pc.exists():
        raise RuntimeError("new-PC data root must be absent before restore acceptance")

    restore_result = _run(
        executable,
        "--data-dir",
        new_pc,
        "--restore",
        backup,
    )
    if "pre-restore backup:" in restore_result.stdout:
        raise RuntimeError(
            "fresh new-PC restore unexpectedly created a pre-restore backup"
        )

    new_database = new_pc / DATABASE_NAME
    new_database_sha, new_counts = _database_snapshot(new_database)
    new_assets = _asset_snapshot(new_pc)

    if new_database_sha != old_database_sha:
        raise RuntimeError(
            "new-PC logical database content differs from old-PC source "
            f"({old_database_sha} != {new_database_sha})"
        )
    if new_counts != old_counts:
        raise RuntimeError("new-PC table row counts differ from old-PC source")
    if new_assets != old_assets:
        raise RuntimeError("new-PC measurement asset hashes differ from old-PC source")

    with sqlite3.connect(new_database) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM scene_revisions WHERE document_id=?",
            (SYNTHETIC_DOCUMENT_ID,),
        ).fetchone()
        if row is None or int(row[0]) <= 0:
            raise RuntimeError("restored new-PC database is missing the synthetic project")

    report = {
        "acceptance": "issue-102-windows-pc-migration",
        "status": "pass",
        "executable": str(executable),
        "old_pc_data_root": str(old_pc),
        "new_pc_data_root": str(new_pc),
        "backup_path": str(backup),
        "backup_size_bytes": backup.stat().st_size,
        "logical_database_sha256": old_database_sha,
        "measurement_asset_count": len(old_assets),
        "required_populated_tables": {
            table: old_counts[table] for table in _REQUIRED_POPULATED_TABLES
        },
        "old_pc_backup_stdout": backup_result.stdout.strip(),
        "new_pc_restore_stdout": restore_result.stdout.strip(),
    }
    report_path = work_dir / "pc-migration-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Issue #102 old-PC -> backup -> fresh-new-PC migration "
            "using only the packaged Windows executable."
        )
    )
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    report = validate_pc_migration(args.exe, args.work_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
