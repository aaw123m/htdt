from __future__ import annotations

from hashlib import sha256
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / 'scripts' / 'audit_o60_owned_room.py'
SPEC = importlib.util.spec_from_file_location('audit_o60_owned_room', AUDIT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_readonly_snapshot_includes_committed_wal_state_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / 'source.sqlite3'
    snapshot = tmp_path / 'snapshot.sqlite3'

    connection = sqlite3.connect(source)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('INSERT INTO evidence(value) VALUES (?)', ('owned-room',))
        connection.commit()

        before_main = _digest(source)
        wal = source.with_name(source.name + '-wal')
        before_wal = None if not wal.exists() else _digest(wal)

        audit_module._readonly_snapshot(source, snapshot)

        assert _digest(source) == before_main
        if before_wal is not None and wal.exists():
            assert _digest(wal) == before_wal
        with sqlite3.connect(snapshot) as copied:
            assert copied.execute('SELECT value FROM evidence').fetchall() == [('owned-room',)]
            assert copied.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        connection.close()


def test_inventory_cli_closes_initialized_snapshot_before_temp_cleanup(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    sqlite3.connect(data_dir / audit_module.DATABASE_NAME).close()

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / 'scripts' / 'inventory_o60_owned_room.py'),
            '--data-dir',
            str(data_dir),
            '--skip-rew',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'O60R_DATABASE_EXISTS=True' in result.stdout
    assert 'O60R_CAMPAIGN_COUNT=0' in result.stdout
    assert 'O60R_INVENTORY_ERROR=' not in result.stdout


def _passing_record():
    return SimpleNamespace(
        evidence_scope='owned_room',
        recommendation_gate='eligible',
        gate_reasons=(),
        residual_gate='pass',
        trend_checks=(
            SimpleNamespace(objective_id='response.shape_rms_db', gate='pass'),
        ),
        sensitivity_checks=(
            SimpleNamespace(
                objective_id='response.shape_rms_db',
                candidate_a_id='a',
                candidate_b_id='b',
                gate='pass',
            ),
        ),
        repeatability_checks=(
            SimpleNamespace(scene_revision_id='revision-a', gate='pass'),
        ),
        separation_checks=(
            SimpleNamespace(candidate_a_id='a', candidate_b_id='b', gate='pass'),
        ),
        applicability_checks=(
            SimpleNamespace(code='geometry', passed=True, detail='verified'),
            SimpleNamespace(code='band', passed=True, detail='verified'),
            SimpleNamespace(code='routing', passed=True, detail='verified'),
        ),
    )


def test_audit_gate_summary_fails_closed_on_any_validation_gate() -> None:
    record = _passing_record()
    passed, reasons = audit_module._check_gate(record)
    assert passed
    assert reasons == ()

    record.separation_checks = (
        SimpleNamespace(candidate_a_id='a', candidate_b_id='b', gate='fail'),
    )
    passed, reasons = audit_module._check_gate(record)

    assert not passed
    assert any('separation a/b is fail' in reason for reason in reasons)
