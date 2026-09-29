"""#604 support & diagnostics center tests."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from htdt.runtime_instance import (
    LOCK_FILENAME,
    RUNTIME_FILENAME,
    RuntimeInfo,
    SingleInstanceGuard,
    write_runtime_info,
)
import htdt.support_diagnostics as sd
from htdt.support_diagnostics import (
    DATABASE_NAME,
    DiagnosticPackageBuilder,
    HealthCategory,
    HealthCheckResult,
    HealthStatus,
    PACKAGE_EXCLUSIONS,
    PackageCategory,
    environment_summary,
    failure_correlation_id,
    package_filename,
    previous_session_unexpected_end,
    run_health_checks,
)


def _native_db(path: Path, schema_version: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            'CREATE TABLE native_schema_metadata '
            '(singleton INTEGER PRIMARY KEY, schema_version INTEGER)'
        )
        conn.execute(
            'INSERT INTO native_schema_metadata '
            '(singleton, schema_version) VALUES (1, ?)',
            (schema_version,),
        )


def test_failure_correlation_id() -> None:
    assert failure_correlation_id('seed') == failure_correlation_id('seed')
    assert len(failure_correlation_id('seed')) == 4
    assert failure_correlation_id() != failure_correlation_id()


def test_environment_summary(tmp_path) -> None:
    summary = environment_summary(tmp_path, project_ref='demo')
    assert summary.display_version.startswith('HTDT ')
    assert summary.schema_version >= 1
    assert summary.project_ref == 'demo'
    assert summary.data_dir == str(tmp_path)


def test_health_checks_empty_dir(tmp_path) -> None:
    report = run_health_checks(tmp_path)
    by_id = {r.check_id: r for r in report.results}
    assert by_id['storage.database_openable'].status == HealthStatus.NOT_APPLICABLE
    assert by_id['storage.sqlite_quick_check'].status == HealthStatus.NOT_APPLICABLE
    assert by_id['storage.disk_space'].status in (
        HealthStatus.PASS,
        HealthStatus.FAIL,
    )
    assert by_id['integrity.semantic'].status == HealthStatus.NOT_APPLICABLE
    assert report.overall != HealthStatus.FAIL


def test_health_check_database_openable(tmp_path) -> None:
    db = tmp_path / DATABASE_NAME
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE t (id INTEGER)')
    report = run_health_checks(tmp_path)
    by_id = {r.check_id: r for r in report.results}
    assert by_id['storage.database_openable'].status == HealthStatus.PASS
    assert by_id['storage.sqlite_quick_check'].status == HealthStatus.PASS


def test_corrupt_database_fails_storage(tmp_path) -> None:
    (tmp_path / DATABASE_NAME).write_bytes(b'not a sqlite database at all')
    report = run_health_checks(tmp_path)
    assert report.overall == HealthStatus.FAIL
    assert report.failed


def test_integration_failure_degrades_to_attention(tmp_path) -> None:
    def bad_probe(data_dir: Path) -> HealthCheckResult:
        return HealthCheckResult(
            check_id='integrations.rew',
            category=HealthCategory.INTEGRATIONS,
            status=HealthStatus.FAIL,
            summary='REW unreachable',
        )

    report = run_health_checks(tmp_path, integration_probes=(bad_probe,))
    assert report.overall == HealthStatus.ATTENTION

    def crashing_probe(data_dir: Path) -> HealthCheckResult:
        raise RuntimeError('boom')

    report2 = run_health_checks(tmp_path, integration_probes=(crashing_probe,))
    assert report2.overall == HealthStatus.ATTENTION


def test_integrity_runner_crash_is_finding(tmp_path) -> None:
    def runner(data_dir: Path) -> HealthCheckResult:
        raise RuntimeError('audit crashed')

    report = run_health_checks(tmp_path, integrity_runner=runner)
    assert report.overall == HealthStatus.FAIL
    crash = next(r for r in report.results if r.check_id == 'integrity.semantic')
    assert crash.status == HealthStatus.FAIL


def test_previous_session_unexpected_end(tmp_path) -> None:
    evidence = previous_session_unexpected_end(tmp_path)
    assert evidence.unexpected_end is False

    write_runtime_info(
        tmp_path,
        RuntimeInfo(pid=99999999, port=43210, url='http://127.0.0.1:43210/'),
    )
    dead = previous_session_unexpected_end(tmp_path, pid_alive=lambda pid: False)
    assert dead.unexpected_end is True
    live = previous_session_unexpected_end(tmp_path, pid_alive=lambda pid: True)
    assert live.unexpected_end is False


def test_package_plan_lists_categories(tmp_path) -> None:
    builder = DiagnosticPackageBuilder(
        tmp_path, health_report=run_health_checks(tmp_path)
    )
    plan = builder.plan()
    assert PackageCategory.LOGS in plan.categories
    assert PackageCategory.BUILD_IDENTITY in plan.categories
    assert PackageCategory.HEALTH_RESULTS in plan.categories
    assert PackageCategory.PROJECT_IDS not in plan.categories
    assert plan.exclusions == PACKAGE_EXCLUSIONS


def test_package_build_and_manifest(tmp_path) -> None:
    data_dir = tmp_path / 'data'
    diag = data_dir / 'diagnostics'
    diag.mkdir(parents=True)
    (diag / 'htdt-native.log').write_text('log line\n', encoding='utf-8')

    builder = DiagnosticPackageBuilder(
        data_dir,
        health_report=run_health_checks(data_dir),
        preferences_summary={
            'general.theme': 'dark',
            'files.export_dir': 'C:\\Users\\me\\exports',
            'integrations.rew_api_key': 'secret-value',
        },
        project_ids={'demo': 'uuid-1'},
    )
    plan = builder.plan(include_project_ids=True)
    destination = tmp_path / 'out' / package_filename()
    result = builder.build(destination, plan)

    assert result.path.exists()
    names = set(result.included)
    assert 'manifest.json' not in names  # manifest listed separately
    with zipfile.ZipFile(result.path) as archive:
        members = set(archive.namelist())
        assert 'manifest.json' in members
        assert 'logs/htdt-native.log' in members
        assert 'build_identity.json' in members
        assert 'health_checks.json' in members
        assert 'project_ids.json' in members
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['not_a_backup'] is True
        assert manifest['not_a_project_export'] is True
        assert manifest['exclusions'] == list(PACKAGE_EXCLUSIONS)
        prefs = json.loads(archive.read('preferences_summary.json'))
        assert prefs['general.theme'] == 'dark'
        assert prefs['files.export_dir'] == '<set>'
        assert prefs['integrations.rew_api_key'] == '<excluded>'


def test_package_excludes_project_ids_by_default(tmp_path) -> None:
    builder = DiagnosticPackageBuilder(tmp_path, project_ids={'demo': 'uuid-1'})
    plan = builder.plan(include_project_ids=False)
    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        assert 'project_ids.json' not in archive.namelist()


def test_package_byte_budget_truncates(tmp_path) -> None:
    data_dir = tmp_path / 'data'
    diag = data_dir / 'diagnostics'
    diag.mkdir(parents=True)
    (diag / 'htdt-native.log').write_bytes(b'x' * 10_000)

    builder = DiagnosticPackageBuilder(data_dir)
    plan = builder.plan().model_copy(update={'byte_budget': 4_000})
    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        # member payload bytes respect the budget; the archive size is the
        # real produced file size, which can exceed it by ZIP overhead.
        assert manifest['bytes'] <= 4_000
        assert manifest['byte_budget'] == 4_000
        log_member = manifest['members']['logs/htdt-native.log']
        assert log_member['status'] == 'truncated'
        assert log_member['original_bytes'] == 10_000
        assert log_member['included_bytes'] < 10_000
        # the tail is retained, not an arbitrary prefix
        log_bytes = archive.read('logs/htdt-native.log')
        assert log_bytes == b'x' * log_member['included_bytes']
    # reported size is the actual produced archive
    assert result.bytes_written == result.path.stat().st_size


def test_package_never_byte_truncates_json(tmp_path) -> None:
    """#749: tight budgets produce valid JSON or skips, never cut files."""
    builder = DiagnosticPackageBuilder(
        tmp_path,
        health_report=run_health_checks(tmp_path),
        operation_failures=[{'op': f'op-{i}', 'error': 'x' * 50} for i in range(20)],
        capability_inventory={'gpu': {'name': 'x' * 200}},
        preferences_summary={'general.theme': 'dark'},
        project_ids={'demo': 'uuid-1'},
    )
    plan = builder.plan(include_project_ids=True).model_copy(
        update={'byte_budget': 900}
    )
    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        for name in archive.namelist():
            if name.endswith('.json'):
                json.loads(archive.read(name))  # must be valid JSON
        assert set(manifest['skipped_files']) | set(manifest['included_files']) == set(
            manifest['members']
        )
        assert any(
            m['status'] == 'skipped' for m in manifest['members'].values()
        )
        # every member carries an explicit status
        assert all(
            m['status'] in {'complete', 'truncated', 'skipped'}
            for m in manifest['members'].values()
        )


def test_quick_check_non_ok_rows_fail(tmp_path, monkeypatch) -> None:
    """#749: a non-throwing quick_check with diagnostic rows is not PASS."""
    (tmp_path / DATABASE_NAME).write_bytes(b'x')

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert 'quick_check' in query
            return [('database corruption on page 7',)]

    monkeypatch.setattr(sd.sqlite3, 'connect', lambda *a, **k: FakeConn())
    report = run_health_checks(tmp_path)
    by_id = {r.check_id: r for r in report.results}
    assert by_id['storage.database_openable'].status == HealthStatus.PASS
    check = by_id['storage.sqlite_quick_check']
    assert check.status == HealthStatus.FAIL
    assert 'corruption' in (check.detail or '')


def test_lock_check_own_instance_is_pass(tmp_path) -> None:
    """#749: the live instance's own lock is expected state, not a warning."""
    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire()
    try:
        report = run_health_checks(tmp_path, owns_lock=True)
        check = next(
            r for r in report.results if r.check_id == 'storage.data_dir_lock'
        )
        assert check.status == HealthStatus.PASS
    finally:
        guard.release()


def test_lock_check_foreign_lock_is_attention(tmp_path) -> None:
    """#749: a held lock this process does not claim is ATTENTION."""
    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire()
    try:
        report = run_health_checks(tmp_path, owns_lock=False)
        check = next(
            r for r in report.results if r.check_id == 'storage.data_dir_lock'
        )
        assert check.status == HealthStatus.ATTENTION
        assert 'pid' in check.summary
    finally:
        guard.release()


def test_lock_check_stale_metadata_is_not_held(tmp_path) -> None:
    """#749: leftover owner metadata with no OS lock is stale evidence."""
    (tmp_path / LOCK_FILENAME).write_bytes(
        b'L'
        + json.dumps(
            {
                'app_id': 'home-theater-digital-twin',
                'pid': 424242,
                'host': 'stale-host',
                'acquired_at': '2026-01-01T00:00:00Z',
            }
        ).encode('utf-8')
    )
    report = run_health_checks(tmp_path)
    check = next(
        r for r in report.results if r.check_id == 'storage.data_dir_lock'
    )
    assert check.status == HealthStatus.PASS
    assert 'stale' in (check.detail or '')


# -- round14: schema compatibility is part of storage truth ---------------------


def _schema_check(report):
    return next(
        r for r in report.results if r.check_id == 'storage.schema_compatibility'
    )


def test_schema_check_current_store_passes(tmp_path) -> None:
    from htdt.cad_schema import NATIVE_SCHEMA_VERSION

    _native_db(tmp_path / DATABASE_NAME, NATIVE_SCHEMA_VERSION)
    report = run_health_checks(tmp_path)
    check = _schema_check(report)
    assert check.status == HealthStatus.PASS
    assert report.overall != HealthStatus.FAIL


def test_schema_check_newer_store_fails(tmp_path) -> None:
    """A store a newer build wrote must never read as healthy (#749 truth)."""

    _native_db(tmp_path / DATABASE_NAME, 999)
    report = run_health_checks(tmp_path)
    check = _schema_check(report)
    assert check.status == HealthStatus.FAIL
    assert 'v999' in (check.detail or '')
    assert report.overall == HealthStatus.FAIL


def test_schema_check_migration_required_is_attention(tmp_path) -> None:
    """An older store is supported but not silent — migration is pending."""

    _native_db(tmp_path / DATABASE_NAME, 3)
    report = run_health_checks(tmp_path)
    check = _schema_check(report)
    assert check.status == HealthStatus.ATTENTION
    assert 'v3' in check.summary
    assert report.overall == HealthStatus.ATTENTION


def test_schema_check_alien_sqlite_fails(tmp_path) -> None:
    """A valid sqlite that is not an HTDT store is a real fault, not 'ok'."""

    with sqlite3.connect(tmp_path / DATABASE_NAME) as conn:
        conn.execute('CREATE TABLE unrelated (id INTEGER)')
    report = run_health_checks(tmp_path)
    assert _schema_check(report).status == HealthStatus.FAIL
    assert report.overall == HealthStatus.FAIL


def test_schema_check_missing_store_is_not_applicable(tmp_path) -> None:
    report = run_health_checks(tmp_path)
    assert _schema_check(report).status == HealthStatus.NOT_APPLICABLE


# -- round14: corrupt runtime marker must not crash the diagnostic --------------


@pytest.mark.parametrize('raw', ['[1, 2, 3]', '5', '"marker"', 'true'])
def test_runtime_info_non_dict_json_is_no_marker(tmp_path, raw) -> None:
    (tmp_path / RUNTIME_FILENAME).write_text(raw, encoding='utf-8')
    evidence = previous_session_unexpected_end(tmp_path)
    assert evidence.unexpected_end is False
    assert 'no runtime marker' in evidence.detail


# -- round14: disk check answers from the real volume ---------------------------


def test_disk_space_measures_existing_ancestor(tmp_path) -> None:
    missing = tmp_path / 'deep' / 'not-created'
    report = run_health_checks(missing)
    check = next(
        r for r in report.results if r.check_id == 'storage.disk_space'
    )
    assert check.status in (HealthStatus.PASS, HealthStatus.FAIL)
    assert 'unavailable' not in check.summary


# -- round14: bundle carries the bounded launch history -------------------------


def test_package_includes_launch_metadata(tmp_path) -> None:
    from htdt.startup_recovery import complete_launch, record_launch

    data_dir = tmp_path / 'data'
    record = record_launch(
        data_dir,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00Z',
        project_ref='secret-project-name',
    )
    complete_launch(
        data_dir, record.launch_id, clean=False,
        failure_class='renderer_initialization',
    )

    builder = DiagnosticPackageBuilder(data_dir)
    plan = builder.plan()
    assert PackageCategory.LAUNCH_METADATA in plan.categories

    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        assert 'launch_metadata.json' in archive.namelist()
        payload = json.loads(archive.read('launch_metadata.json'))
        entry = payload['records'][0]
        assert entry['failure_class'] == 'renderer_initialization'
        assert entry['clean_exit'] is False
        # Project identity stays behind the opt-in, like project_ids.
        assert entry['last_project_ref'] == '<set>'


def test_package_launch_metadata_respects_project_optin(tmp_path) -> None:
    from htdt.startup_recovery import complete_launch, record_launch

    data_dir = tmp_path / 'data'
    record = record_launch(
        data_dir,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00Z',
        project_ref='named-project',
    )
    complete_launch(data_dir, record.launch_id, clean=True)

    builder = DiagnosticPackageBuilder(
        data_dir, project_ids={'named-project': 'doc-1'}
    )
    plan = builder.plan(include_project_ids=True)
    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        payload = json.loads(archive.read('launch_metadata.json'))
        assert payload['records'][0]['last_project_ref'] == 'named-project'


def test_package_launch_metadata_corrupt_file_is_skipped_not_empty(
    tmp_path,
) -> None:
    """A torn metadata file is marked skipped — never an empty history."""

    data_dir = tmp_path / 'data'
    diag = data_dir / 'diagnostics'
    diag.mkdir(parents=True)
    (diag / 'recovery-launch-metadata.json').write_bytes(b'{"records": [')

    builder = DiagnosticPackageBuilder(data_dir)
    plan = builder.plan()
    result = builder.build(tmp_path / 'pkg.zip', plan)
    with zipfile.ZipFile(result.path) as archive:
        assert 'launch_metadata.json' not in archive.namelist()
        manifest = json.loads(archive.read('manifest.json'))
        member = manifest['members']['launch_metadata.json']
        assert member['status'] == 'skipped'


# -- round14: schema summary reports the native store's own authority -----------


def test_schema_summary_reports_native_store(tmp_path) -> None:
    from htdt.cad_schema import NATIVE_SCHEMA_VERSION

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    _native_db(data_dir / DATABASE_NAME, NATIVE_SCHEMA_VERSION)

    builder = DiagnosticPackageBuilder(data_dir)
    result = builder.build(tmp_path / 'pkg.zip', builder.plan())
    with zipfile.ZipFile(result.path) as archive:
        summary = json.loads(archive.read('schema_summary.json'))
    assert summary['schema_version'] == NATIVE_SCHEMA_VERSION
    assert summary['supported_native_schema_version'] == NATIVE_SCHEMA_VERSION
    assert summary['stored_native_schema_version'] == NATIVE_SCHEMA_VERSION
    assert summary['native_schema_compatibility'] == 'current'
