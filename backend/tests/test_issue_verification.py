"""Tests for scripts/verify_open_issues.py (issue-verification runner).

Covers manifest parsing/validation (fail-closed), verdict computation, the
offline runner path, --dry-run, and --post-summary-from error handling.
Everything is offline; no GitHub API calls are made.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / 'scripts' / 'verify_open_issues.py'
SPEC = importlib.util.spec_from_file_location('verify_open_issues', RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
# register before exec: dataclass field introspection reads sys.modules
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)

REAL_MANIFEST = ROOT / 'scripts' / 'issue_verification_manifest.yaml'

PREFLIGHT_PATH = ROOT / 'scripts' / 'golden_path_preflight.py'
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    'golden_path_preflight', PREFLIGHT_PATH
)
assert PREFLIGHT_SPEC is not None and PREFLIGHT_SPEC.loader is not None
preflight = importlib.util.module_from_spec(PREFLIGHT_SPEC)
sys.modules[PREFLIGHT_SPEC.name] = preflight
PREFLIGHT_SPEC.loader.exec_module(preflight)

PY = sys.executable.replace('\\', '/')


def _write_manifest(tmp_path: Path, text: str, name: str = 'manifest.yaml') -> Path:
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


def _manifest_yaml(checks_block: str, issue: int = 10) -> str:
    return f"""version: 1
repo: ka0923s-a11y/HTDT
issues:
  - issue: {issue}
    checks:
{checks_block}
"""


def _script_check(cid: str, body: str, **extra) -> str:
    lines = [
        f'      - id: {cid}',
        '        kind: script',
        '        command:',
        f'          - "{{python}}"',
        '          - "-c"',
        f'          - "{body}"',
        '        description: test check',
    ]
    for key, value in extra.items():
        lines.append(f'        {key}: {value}')
    return '\n'.join(lines)


def _manual_check(cid: str = 'manual-item') -> str:
    return (
        f'      - id: {cid}\n'
        '        kind: manual\n'
        '        description: physical evidence needed\n'
    )


def _pytest_check(cid: str = 'pt') -> str:
    return (
        f'      - id: {cid}\n'
        '        kind: pytest\n'
        '        tests:\n'
        '          - backend/tests/test_dependency_lock.py\n'
        '        description: real pytest file\n'
    )


class TestManifestParse:
    def test_real_manifest_parses_and_covers_issues(self):
        manifest = verify.load_manifest(REAL_MANIFEST)
        assert manifest.repo == 'ka0923s-a11y/HTDT'
        assert len(manifest.issues) >= 14
        for entry in manifest.issues.values():
            assert entry.checks, f'#{entry.issue} has no checks'
            for check in entry.checks:
                assert check.kind in verify.CHECK_KINDS
                if check.kind == 'pytest':
                    assert check.tests, check.id
                    for node in check.tests:
                        file_part = node.split('::')[0]
                        assert (ROOT / file_part).is_file(), (
                            f'#{entry.issue} check {check.id}: '
                            f'missing test file {file_part}'
                        )
                elif check.kind == 'script':
                    assert check.command, check.id
                    script_args = [
                        a for a in check.command if a.endswith('.py')
                    ]
                    for a in script_args:
                        assert (ROOT / a).is_file(), (
                            f'#{entry.issue} check {check.id}: '
                            f'missing script {a}'
                        )
                else:
                    assert check.description.strip(), check.id

    def test_missing_manifest_fails(self, tmp_path):
        with pytest.raises(verify.ManifestError, match='not found'):
            verify.load_manifest(tmp_path / 'nope.yaml')

    def test_bad_version_fails(self, tmp_path):
        path = _write_manifest(
            tmp_path, 'version: 2\nrepo: a/b\nissues: []\n'
        )
        with pytest.raises(verify.ManifestError, match='version'):
            verify.load_manifest(path)

    @pytest.mark.parametrize(
        'checks_block, match',
        [
            ('      - id: x\n        kind: bogus\n        description: d', 'kind'),
            ('      - id: x\n        kind: pytest\n        description: d', 'tests'),
            ('      - id: x\n        kind: script\n        description: d', 'command'),
            ('      - id: x\n        kind: manual', 'description'),
            (
                '      - id: x\n        kind: manual\n        description: d\n'
                '      - id: x\n        kind: manual\n        description: e',
                'duplicate check id',
            ),
        ],
    )
    def test_malformed_checks_fail_closed(self, tmp_path, checks_block, match):
        path = _write_manifest(tmp_path, _manifest_yaml(checks_block))
        with pytest.raises(verify.ManifestError, match=match):
            verify.load_manifest(path)

    def test_duplicate_issue_fails(self, tmp_path):
        text = _manifest_yaml(_manual_check()) + (
            '  - issue: 10\n'
            '    checks:\n      - id: y\n        kind: manual\n'
            '        description: dup\n'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='duplicate issue'):
            verify.load_manifest(path)

    def test_unknown_verdict_strategy_fails(self, tmp_path):
        text = _manifest_yaml(_manual_check()).replace(
            '    checks:\n',
            '    verdict_rules:\n      strategy: always_green\n    checks:\n',
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='strategy'):
            verify.load_manifest(path)

    def test_empty_checks_fails(self, tmp_path):
        text = (
            'version: 1\nrepo: a/b\nissues:\n'
            '  - issue: 1\n    checks: []\n'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='must not be empty'):
            verify.load_manifest(path)


class TestVerdictLogic:
    def _entry(self, checks) -> verify.IssueEntry:
        return verify.IssueEntry(issue=1, title=None, notes=None, checks=checks)

    def _result(self, check, status):
        return verify.CheckResult(check=check, status=status)

    def _mk(self, kind, cid='c'):
        return verify.Check(id=cid, kind=kind, description='d')

    def test_verified_when_all_pass_no_manual(self):
        c = self._mk('pytest')
        verdict = verify.compute_verdict(
            self._entry([c]), [self._result(c, 'passed')]
        )
        assert verdict == 'verified'

    def test_partially_when_manual_remains(self):
        c = self._mk('pytest')
        m = self._mk('manual', 'm')
        verdict = verify.compute_verdict(
            self._entry([c, m]),
            [self._result(c, 'passed'), self._result(m, 'manual')],
        )
        assert verdict == 'partially_verified'

    def test_manual_required_when_no_automated(self):
        m = self._mk('manual')
        verdict = verify.compute_verdict(
            self._entry([m]), [self._result(m, 'manual')]
        )
        assert verdict == 'manual_required'

    @pytest.mark.parametrize('status', ['failed', 'timeout', 'error'])
    def test_failing_on_any_nonpass(self, status):
        c = self._mk('pytest')
        verdict = verify.compute_verdict(
            self._entry([c]), [self._result(c, status)]
        )
        assert verdict == 'failing'


class TestOfflineRun:
    def test_offline_report_verdicts(self, tmp_path):
        checks = (
            _script_check('ok', 'import sys; sys.exit(0)')
            + '\n'
            + _script_check('bad', 'import sys; sys.exit(3)')
        )
        text = (
            'version: 1\nrepo: a/b\ndefaults:\n  timeout_seconds: 60\n'
            'issues:\n'
            '  - issue: 1\n    checks:\n'
            + checks + '\n'
            '  - issue: 2\n    checks:\n'
            + _script_check('ok2', 'import sys; sys.exit(0)') + '\n'
            + _manual_check() +
            '  - issue: 3\n    checks:\n'
            + _manual_check('only-manual')
        )
        manifest_path = _write_manifest(tmp_path, text)
        report_dir = tmp_path / 'report'
        code = verify.main([
            '--manifest', str(manifest_path),
            '--offline', '--issues', '1,2,3',
            '--report-dir', str(report_dir),
        ])
        assert code == 0
        report = json.loads(
            (report_dir / 'issue_verification_report.json')
            .read_text(encoding='utf-8')
        )
        verdicts = {i['issue']: i['verdict'] for i in report['issues']}
        assert verdicts == {
            1: 'failing', 2: 'partially_verified', 3: 'manual_required',
        }
        assert report['summary']['failing'] == 1
        assert report['summary']['partially_verified'] == 1
        assert report['summary']['manual_required'] == 1
        md = list(report_dir.glob('issue-verification-*.md'))
        assert md, 'markdown report not written'
        logs = list((report_dir / 'logs').glob('*.log'))
        assert {p.name for p in logs} >= {'ok.log', 'bad.log', 'ok2.log'}

    def test_offline_unmapped_issue(self, tmp_path):
        text = (
            'version: 1\nrepo: a/b\nissues:\n'
            '  - issue: 1\n    checks:\n' + _manual_check()
        )
        manifest_path = _write_manifest(tmp_path, text)
        report_dir = tmp_path / 'report'
        code = verify.main([
            '--manifest', str(manifest_path),
            '--offline', '--issues', '1,99',
            '--report-dir', str(report_dir),
        ])
        assert code == 0
        report = json.loads(
            (report_dir / 'issue_verification_report.json')
            .read_text(encoding='utf-8')
        )
        verdicts = {i['issue']: i['verdict'] for i in report['issues']}
        assert verdicts[99] == 'unmapped'

    def test_timeout_marks_check(self, tmp_path):
        checks = _script_check(
            'slow', 'import time; time.sleep(30)', timeout_seconds=1
        )
        manifest_path = _write_manifest(tmp_path, _manifest_yaml(checks))
        report_dir = tmp_path / 'report'
        code = verify.main([
            '--manifest', str(manifest_path),
            '--offline', '--issues', '10',
            '--report-dir', str(report_dir),
        ])
        assert code == 0
        report = json.loads(
            (report_dir / 'issue_verification_report.json')
            .read_text(encoding='utf-8')
        )
        assert report['issues'][0]['verdict'] == 'failing'
        assert report['issues'][0]['checks'][0]['status'] == 'timeout'

    def test_dry_run_exits_zero(self, tmp_path, capsys):
        text = (
            'version: 1\nrepo: a/b\nissues:\n'
            '  - issue: 1\n    checks:\n' + _manual_check()
        )
        manifest_path = _write_manifest(tmp_path, text)
        code = verify.main([
            '--manifest', str(manifest_path),
            '--offline', '--issues', '1',
            '--report-dir', str(tmp_path / 'report'),
            '--dry-run',
        ])
        assert code == 0
        out = capsys.readouterr().out
        assert '#1' in out and 'manual' in out

    def test_malformed_manifest_exits_2(self, tmp_path):
        path = _write_manifest(tmp_path, 'version: 9\n')
        code = verify.main([
            '--manifest', str(path), '--offline', '--issues', '1',
        ])
        assert code == 2

    def test_post_summary_from_requires_token(self, tmp_path, monkeypatch):
        for name in verify.TOKEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        report_path = tmp_path / 'report.json'
        report_path.write_text(json.dumps({'issues': []}), encoding='utf-8')
        code = verify.main([
            '--manifest', str(REAL_MANIFEST),
            '--post-summary-from', str(report_path),
        ])
        assert code == 2

    def test_post_summary_from_missing_report(self, tmp_path):
        code = verify.main([
            '--manifest', str(REAL_MANIFEST),
            '--post-summary-from', str(tmp_path / 'missing.json'),
        ])
        assert code == 2


class TestPreflightCleanup:
    """golden_path_preflight --clean-work-dir-on-success must be best-effort:
    a still-open file handle inside the work dir must not turn a green run
    into exit 1 (observed: WinError 32 on data-main/cad-scenes.sqlite3)."""

    def test_removes_artifacts_when_unlocked(self, tmp_path):
        work = tmp_path / 'work'
        for name in ('data-main', 'data-restored'):
            (work / name).mkdir(parents=True)
            (work / name / 'scratch.bin').write_bytes(b'x')
        (work / 'preflight.htdt-backup').write_bytes(b'x')
        preflight.clean_work_dir_on_success(work)
        assert not (work / 'data-main').exists()
        assert not (work / 'data-restored').exists()
        assert not (work / 'preflight.htdt-backup').exists()

    def test_locked_file_degrades_to_warning(self, tmp_path, capsys):
        work = tmp_path / 'work'
        target = work / 'data-main'
        target.mkdir(parents=True)
        locked = target / 'cad-scenes.sqlite3'
        handle = locked.open('wb')
        try:
            # on Windows the open handle blocks deletion; on POSIX the unlink
            # succeeds — either way the helper must return without raising.
            preflight.clean_work_dir_on_success(work)
        finally:
            handle.close()
        out = capsys.readouterr().out
        assert 'preflight' in out


# ---------------------------------------------------------------------------
# REV45 additions: process-tree kill, retry/flaky surfacing, verdict cache,
# upserted status comments, closed-issue handling, schema/report hardening.
# ---------------------------------------------------------------------------


def _script_argv_check(cid: str, script: Path, **extra) -> str:
    lines = [
        f'      - id: {cid}',
        '        kind: script',
        '        command:',
        '          - "{python}"',
        f'          - "{script.as_posix()}"',
        '        description: test check',
    ]
    for key, value in extra.items():
        lines.append(f'        {key}: {value}')
    return '\n'.join(lines)


def _run_offline(tmp_path: Path, manifest_text: str, *extra_args) -> tuple:
    manifest_path = _write_manifest(tmp_path, manifest_text)
    report_dir = tmp_path / 'report'
    argv = [
        '--manifest', str(manifest_path),
        '--offline', '--issues', '10',
        '--report-dir', str(report_dir),
        *extra_args,
    ]
    code = verify.main(argv)
    report = json.loads(
        (report_dir / 'issue_verification_report.json')
        .read_text(encoding='utf-8')
    )
    return code, report, report_dir


class TestSchemaHardening:
    def test_check_id_must_be_filename_safe(self, tmp_path):
        text = _manifest_yaml(
            '      - id: a/b\n        kind: manual\n        description: d'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='filename-safe'):
            verify.load_manifest(path)

    @pytest.mark.parametrize('cid', ['con', 'NUL', 'aux', 'com1', 'lpt9'])
    def test_check_id_windows_reserved_name_fails(self, tmp_path, cid):
        """logs/<id>.log cannot be created for DOS device names — the CI
        lane runs windows-latest, so these ids are schema errors."""
        text = _manifest_yaml(
            f'      - id: {cid}\n        kind: manual\n        description: d'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='reserved'):
            verify.load_manifest(path)

    def test_check_id_leading_dot_fails(self, tmp_path):
        text = _manifest_yaml(
            '      - id: .hidden\n        kind: manual\n        description: d'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='dot'):
            verify.load_manifest(path)

    def test_check_id_must_be_unique_across_issues(self, tmp_path):
        """ids name logs/<id>.log in one shared dir: a duplicate across
        issues would silently overwrite the earlier check's log."""
        block = _manual_check('dup')
        text = (
            'version: 1\nrepo: a/b\nissues:\n'
            '  - issue: 10\n    checks:\n' + block + '\n'
            '  - issue: 11\n    checks:\n' + block
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='issue #10'):
            verify.load_manifest(path)

    def test_title_must_be_a_string(self, tmp_path):
        text = _manifest_yaml(_manual_check()).replace(
            '    checks:\n', '    title: [not, a, string]\n    checks:\n'
        )
        path = _write_manifest(tmp_path, text)
        with pytest.raises(verify.ManifestError, match='title'):
            verify.load_manifest(path)


class TestTimeoutTreeKill:
    """A timed-out check must kill the whole process tree — subprocess.run's
    timeout kills only the direct child, orphaning e.g. xdist workers."""

    def test_timeout_kills_grandchildren(self, tmp_path):
        heartbeat = tmp_path / 'grandchild-hb.txt'
        grandchild = tmp_path / 'grandchild.py'
        grandchild.write_text(
            'import time, pathlib\n'
            f'p = pathlib.Path(r"{heartbeat.as_posix()}")\n'
            'while True:\n'
            '    p.write_text(str(time.time()))\n'
            '    time.sleep(0.25)\n',
            encoding='utf-8',
        )
        parent = tmp_path / 'parent.py'
        parent.write_text(
            'import subprocess, sys, time\n'
            f'subprocess.Popen([sys.executable, r"{grandchild.as_posix()}"])\n'
            'time.sleep(60)\n',
            encoding='utf-8',
        )
        checks = _script_argv_check('spawner', parent, timeout_seconds=2)
        code, report, _ = _run_offline(tmp_path, _manifest_yaml(checks))
        assert code == 0
        check = report['issues'][0]['checks'][0]
        assert check['status'] == 'timeout'
        # The grandchild heartbeats ~4x/s while alive; give it a beat to have
        # started, then prove it stopped writing after the tree-kill.
        time.sleep(1.0)
        assert heartbeat.exists(), 'grandchild never started — test vacuous'
        first = heartbeat.read_text(encoding='utf-8')
        time.sleep(1.5)
        assert heartbeat.read_text(encoding='utf-8') == first


class TestStragglerSweep:
    """A check whose main process exits but leaves a child behind must have
    that child reaped — surviving children hold basetemp handles, write into
    the shared log, and burn CPU through the rest of the run."""

    def test_passed_check_cannot_leave_a_child_running(self, tmp_path):
        heartbeat = tmp_path / 'straggler-hb.txt'
        child = tmp_path / 'child.py'
        child.write_text(
            'import time, pathlib\n'
            f'p = pathlib.Path(r"{heartbeat.as_posix()}")\n'
            'while True:\n'
            '    p.write_text(str(time.time()))\n'
            '    time.sleep(0.25)\n',
            encoding='utf-8',
        )
        parent = tmp_path / 'parent.py'
        # Spawns a heartbeat child, waits so it provably starts, then exits 0 —
        # 'passed' on the surface, straggler underneath. The sweep must kill it.
        parent.write_text(
            'import subprocess, sys, time\n'
            f'subprocess.Popen([sys.executable, r"{child.as_posix()}"])\n'
            'time.sleep(1.5)\n',
            encoding='utf-8',
        )
        checks = _script_argv_check('spawner', parent, timeout_seconds=30)
        code, report, _ = _run_offline(tmp_path, _manifest_yaml(checks))
        assert code == 0
        assert report['issues'][0]['checks'][0]['status'] == 'passed'
        assert heartbeat.exists(), 'straggler never started — test vacuous'
        frozen = heartbeat.read_text(encoding='utf-8')
        time.sleep(1.5)
        assert heartbeat.read_text(encoding='utf-8') == frozen

    @pytest.mark.skipif(
        sys.platform != 'win32', reason='job-object kill-on-close is Windows'
    )
    def test_runner_death_reaps_the_check_tree(self, tmp_path):
        """Kill-on-close job semantics: when the *runner* dies mid-check the
        kernel kills every job member — no orphaned check processes."""
        heartbeat = tmp_path / 'orphan-hb.txt'
        child = tmp_path / 'orphan-child.py'
        child.write_text(
            'import time, pathlib\n'
            f'p = pathlib.Path(r"{heartbeat.as_posix()}")\n'
            'while True:\n'
            '    p.write_text(str(time.time()))\n'
            '    time.sleep(0.25)\n',
            encoding='utf-8',
        )
        mini_runner = tmp_path / 'mini_runner.py'
        mini_runner.write_text(
            'import subprocess, sys, time\n'
            f'sys.path.insert(0, r"{ROOT.as_posix()}/scripts")\n'
            'import verify_open_issues as verify\n'
            'job = verify._create_kill_job()\n'
            'assert job is not None\n'
            'proc = subprocess.Popen('
            f'    [sys.executable, r"{child.as_posix()}"])\n'
            'assert verify._kernel32.AssignProcessToJobObject('
            '        job, proc._handle)\n'
            'print(proc.pid, flush=True)\n'
            'time.sleep(60)\n',
            encoding='utf-8',
        )
        runner = subprocess.Popen(
            [sys.executable, str(mini_runner)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            child_pid = int(runner.stdout.readline().strip())
            time.sleep(1.0)
            assert heartbeat.exists(), 'child never started — test vacuous'
            # Kill the "runner" itself; the job's kill-on-close must reap
            # the check child with it.
            subprocess.run(
                ['taskkill', '/F', '/PID', str(runner.pid)],
                capture_output=True, timeout=10,
            )
            runner.wait(timeout=10)
            time.sleep(1.5)
            frozen = heartbeat.read_text(encoding='utf-8')
            time.sleep(1.5)
            assert heartbeat.read_text(encoding='utf-8') == frozen, (
                f'child pid {child_pid} survived runner death — '
                'kill-on-close job did not reap it'
            )
        finally:
            if runner.poll() is None:
                runner.kill()
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(runner.pid)],
                capture_output=True, timeout=10,
            )


class TestRerunFailed:
    def _flaky_manifest(self) -> str:
        body = (
            'import sys,pathlib;'
            "p=pathlib.Path(r'{work_dir}')/'seen';"
            'sys.exit(0) if p.exists() else '
            "(lambda:(p.write_text('1'),sys.exit(1)))()"
        )
        checks = _script_check('flaky', body)
        return _manifest_yaml(checks)

    def test_retry_recovers_and_marks_flaky(self, tmp_path):
        code, report, _ = _run_offline(tmp_path, self._flaky_manifest())
        assert code == 0
        item = report['issues'][0]
        assert item['verdict'] == 'verified'
        check = item['checks'][0]
        assert [a['status'] for a in check['attempts']] == [
            'failed', 'passed',
        ]
        assert check['flaky'] is True

    def test_rerun_failed_zero_is_single_attempt(self, tmp_path):
        code, report, _ = _run_offline(
            tmp_path, self._flaky_manifest(), '--rerun-failed', '0'
        )
        assert code == 0
        item = report['issues'][0]
        assert item['verdict'] == 'failing'
        check = item['checks'][0]
        assert [a['status'] for a in check['attempts']] == ['failed']
        assert check['flaky'] is False


class TestVerdictCache:
    _COUNTER_BODY = (
        "import pathlib;"
        "p=pathlib.Path(r'{report_dir}')/'counter.txt';"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')"
    )

    def _manifest(self) -> str:
        return _manifest_yaml(_script_check('counted', self._COUNTER_BODY))

    def _patch_clean_env(self, monkeypatch):
        monkeypatch.setattr(
            verify, '_git_head', lambda root: ('f' * 40, False)
        )
        monkeypatch.setattr(
            verify, '_env_fingerprint', lambda python: 'envfp'
        )

    def _counter(self, report_dir: Path) -> str:
        path = report_dir / 'counter.txt'
        return path.read_text() if path.exists() else ''

    def test_cache_hit_skips_reexecution(self, tmp_path, monkeypatch):
        self._patch_clean_env(monkeypatch)
        cache_file = tmp_path / 'cache.json'
        args = ('--use-cache', '--cache-file', str(cache_file))
        code, report, report_dir = _run_offline(
            tmp_path, self._manifest(), *args
        )
        assert code == 0
        assert self._counter(report_dir) == 'x'
        check = report['issues'][0]['checks'][0]
        assert check['cached'] is False
        assert cache_file.exists()

        code, report, _ = _run_offline(tmp_path, self._manifest(), *args)
        assert code == 0
        # Second run served the verdict from the cache: no re-execution.
        assert self._counter(report_dir) == 'x'
        check = report['issues'][0]['checks'][0]
        assert check['cached'] is True
        assert check['status'] == 'passed'
        assert report['cache_hits'] == 1

    def test_env_change_invalidates(self, tmp_path, monkeypatch):
        cache_file = tmp_path / 'cache.json'
        args = ('--use-cache', '--cache-file', str(cache_file))
        monkeypatch.setattr(
            verify, '_git_head', lambda root: ('f' * 40, False)
        )
        monkeypatch.setattr(verify, '_env_fingerprint', lambda python: 'env-a')
        _, _, report_dir = _run_offline(tmp_path, self._manifest(), *args)
        monkeypatch.setattr(verify, '_env_fingerprint', lambda python: 'env-b')
        _run_offline(tmp_path, self._manifest(), *args)
        assert self._counter(report_dir) == 'xx'

    def test_managed_env_change_invalidates(self, tmp_path, monkeypatch):
        """Same sha + same interpreter fingerprint, but the env the runner
        injects changed (QT_QPA_PLATFORM offscreen -> windows): the verdict
        could differ, so the cache must not serve the old one."""
        self._patch_clean_env(monkeypatch)
        cache_file = tmp_path / 'cache.json'
        args = ('--use-cache', '--cache-file', str(cache_file))
        monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
        _, _, report_dir = _run_offline(tmp_path, self._manifest(), *args)
        assert self._counter(report_dir) == 'x'
        monkeypatch.setenv('QT_QPA_PLATFORM', 'windows')
        _, _, report_dir = _run_offline(tmp_path, self._manifest(), *args)
        assert self._counter(report_dir) == 'xx'

    def test_dirty_worktree_disables_cache(self, tmp_path, monkeypatch):
        self._patch_clean_env(monkeypatch)
        monkeypatch.setattr(
            verify, '_git_head', lambda root: ('f' * 40, True)
        )
        cache_file = tmp_path / 'cache.json'
        args = ('--use-cache', '--cache-file', str(cache_file))
        code, report, report_dir = _run_offline(
            tmp_path, self._manifest(), *args
        )
        assert code == 0
        assert report['cache'] == 'disabled'
        _run_offline(tmp_path, self._manifest(), *args)
        assert self._counter(report_dir) == 'xx'  # ran twice: no cache

    def test_corrupt_cache_is_ignored(self, tmp_path, monkeypatch):
        self._patch_clean_env(monkeypatch)
        cache_file = tmp_path / 'cache.json'
        cache_file.write_text('{not json', encoding='utf-8')
        code, report, _ = _run_offline(
            tmp_path, self._manifest(),
            '--use-cache', '--cache-file', str(cache_file),
        )
        assert code == 0
        assert json.loads(cache_file.read_text())['version'] == 1


class _FakeApi:
    """In-memory GitHub comments API for urlopen monkeypatching."""

    def __init__(self):
        self.comments: dict[int, dict] = {}
        self.next_id = 1
        self.calls: list = []
        self.fail = False

    @staticmethod
    def _issue_of(url: str) -> int:
        return int(url.split('/issues/')[1].split('/')[0])

    def urlopen(self, request, timeout=0):
        self.calls.append((request.get_method(), request.full_url))
        if self.fail:
            raise urllib.error.HTTPError(
                request.full_url, 500, 'boom', None, None
            )
        method = request.get_method()
        url = request.full_url
        if method == 'GET' and '/comments' in url:
            issue = self._issue_of(url)
            return _FakeResponse([
                c for c in self.comments.values() if c['issue'] == issue
            ])
        if method == 'POST':
            import json as _json
            body = _json.loads(request.data.decode('utf-8'))
            cid = self.next_id
            self.next_id += 1
            self.comments[cid] = {
                'id': cid, 'body': body['body'],
                'issue': self._issue_of(url),
            }
            return _FakeResponse(self.comments[cid])
        if method == 'PATCH':
            cid = int(url.rstrip('/').split('/')[-1])
            import json as _json
            self.comments[cid]['body'] = _json.loads(
                request.data.decode('utf-8')
            )['body']
            return _FakeResponse(self.comments[cid])
        if method == 'DELETE':
            cid = int(url.rstrip('/').split('/')[-1])
            del self.comments[cid]
            return _FakeResponse(None)
        raise AssertionError(f'unexpected request {method} {url}')

    def comment_count(self, issue: int) -> int:
        return sum(
            1 for c in self.comments.values() if c['issue'] == issue
        )


class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self):
        return json.dumps(self._payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(verify.urllib.request, 'urlopen', api.urlopen)
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    return api


def _verdict_report(*verdicts) -> dict:
    return {
        'generated_at_utc': '2026-10-04T00:00:00Z',
        'git_ref': 'abc1234',
        'issues': [
            {
                'issue': i + 1,
                'verdict': v,
                'checks': [],
                'manual_required': [],
            }
            for i, v in enumerate(verdicts)
        ],
    }


class TestPostSummariesUpsert:
    def test_creates_then_edits_single_comment(self, monkeypatch):
        api = _fake_api(monkeypatch)
        report = _verdict_report('verified', 'failing')
        assert verify._post_summaries('a/b', report) == 0
        assert api.comment_count(1) == 1
        first_body = api.comments[1]['body']
        assert verify.COMMENT_MARKER in first_body
        assert 'verified' in first_body

        # A second run must EDIT the existing comment, not add another.
        assert verify._post_summaries('a/b', report) == 0
        assert api.comment_count(1) == 1
        methods = [m for m, _ in api.calls]
        assert 'PATCH' in methods

    def test_unmapped_and_not_open_are_skipped(self, monkeypatch):
        api = _fake_api(monkeypatch)
        report = _verdict_report('unmapped', 'not_open')
        assert verify._post_summaries('a/b', report) == 0
        assert api.calls == []

    def test_duplicate_status_comments_are_deduped(self, monkeypatch):
        """A past posting race (two runners both POSTed) leaves duplicate
        marked comments; the next upsert edits the oldest and deletes the
        rest so the stale copy stops shadowing the verdict."""
        api = _fake_api(monkeypatch)
        for cid in (1, 2):
            api.comments[cid] = {
                'id': cid, 'issue': 1,
                'body': verify.COMMENT_MARKER
                + '\n自動検証: **failing** (old run)',
            }
        api.next_id = 3
        report = _verdict_report('verified')
        assert verify._post_summaries('a/b', report) == 0
        assert api.comment_count(1) == 1
        remaining = next(iter(api.comments.values()))
        assert remaining['id'] == 1  # oldest edited, duplicate deleted
        assert 'verified' in remaining['body']
        methods = [m for m, _ in api.calls]
        assert 'PATCH' in methods and 'DELETE' in methods

    def test_marker_quoted_by_a_human_is_not_clobbered(self, monkeypatch):
        """A comment merely containing the marker (e.g. a human quoting it
        mid-body) is not ours — PATCHing it would destroy their text."""
        api = _fake_api(monkeypatch)
        api.comments[1] = {
            'id': 1, 'issue': 1,
            'body': 'see the <!-- verify-open-issues --> comment above',
        }
        api.next_id = 2
        report = _verdict_report('verified')
        assert verify._post_summaries('a/b', report) == 0
        assert api.comment_count(1) == 2  # human comment kept, ours added
        assert api.comments[1]['body'].startswith('see the')
        assert api.comments[2]['body'].startswith(verify.COMMENT_MARKER)

    def test_comment_failure_exits_nonzero(self, monkeypatch):
        api = _fake_api(monkeypatch)
        api.fail = True
        report = _verdict_report('verified')
        assert verify._post_summaries('a/b', report) == 2


class TestClosedRequestedIssue:
    def test_issues_on_closed_issue_reports_not_open(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(verify, 'find_token', lambda: 'tok')
        monkeypatch.setattr(
            verify, 'fetch_open_issues', lambda repo, token: []
        )
        manifest_path = _write_manifest(tmp_path, _manifest_yaml(
            _script_check('ok', 'import sys; sys.exit(0)')
        ))
        report_dir = tmp_path / 'report'
        code = verify.main([
            '--manifest', str(manifest_path),
            '--issues', '10',
            '--report-dir', str(report_dir),
        ])
        assert code == 0
        report = json.loads(
            (report_dir / 'issue_verification_report.json')
            .read_text(encoding='utf-8')
        )
        item = report['issues'][0]
        assert item['verdict'] == 'not_open'
        assert item['checks'] == []  # closed issue: checks must not run
        assert report['summary']['not_open'] == 1
        assert 10 in report['not_open_issues']


class TestCliHardening:
    def test_issues_rejects_garbage(self, tmp_path, capsys):
        code = verify.main([
            '--manifest', str(REAL_MANIFEST),
            '--offline', '--issues', 'abc',
        ])
        assert code == 2
        assert '--issues' in capsys.readouterr().err

    def test_issues_rejects_non_positive(self, tmp_path, capsys):
        code = verify.main([
            '--manifest', str(REAL_MANIFEST),
            '--offline', '--issues', '3,-1',
        ])
        assert code == 2
        assert 'positive' in capsys.readouterr().err

    def test_issues_fetch_non_list_page_fails(self, monkeypatch):
        """A malformed API page must be a clean tool failure (exit 2 / a
        RuntimeError surfaced), not a TypeError deep in the loop."""
        class _Resp:
            headers = {}

            def read(self):
                return b'{"message": "abuse detection"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            verify.urllib.request, 'urlopen', lambda req, timeout=0: _Resp()
        )
        monkeypatch.setenv('GITHUB_TOKEN', 't')
        with pytest.raises(RuntimeError, match='non-list'):
            verify.fetch_open_issues('a/b', 't')

    def test_fail_on_verdict_exits_3(self, tmp_path):
        checks = _script_check('bad', 'import sys; sys.exit(3)')
        code, report, _ = _run_offline(
            tmp_path, _manifest_yaml(checks),
            '--rerun-failed', '0', '--fail-on-verdict', 'failing',
        )
        assert code == 3
        assert report['issues'][0]['verdict'] == 'failing'

    def test_fail_on_verdict_rejects_unknown(self):
        assert verify.main(['--fail-on-verdict', 'bogus']) == 2

    def test_log_path_includes_logs_dir(self, tmp_path):
        checks = _script_check('ok', 'import sys; sys.exit(0)')
        code, report, _ = _run_offline(tmp_path, _manifest_yaml(checks))
        assert code == 0
        assert report['issues'][0]['checks'][0]['log_path'].startswith(
            'logs/'
        )


class TestReportUx:
    def test_pipe_in_title_is_escaped(self):
        report = {
            'repo': 'a/b',
            'generated_at_utc': '2026-10-04T00:00:00Z',
            'mode': 'offline',
            'git_ref': 'abc',
            'python': 'x',
            'summary': {'verified': 1},
            'issues': [{
                'issue': 1, 'title': 'a | b', 'verdict': 'verified',
                'checks': [], 'manual_required': [],
            }],
            'unmapped_issues': [],
            'not_open_issues': [],
        }
        md = verify.render_markdown(report)
        assert 'a \\| b' in md

    def test_at_a_glance_groups_verdicts(self):
        report = {
            'repo': 'a/b',
            'generated_at_utc': '2026-10-04T00:00:00Z',
            'mode': 'offline',
            'git_ref': 'abc',
            'python': 'x',
            'summary': {'verified': 1, 'failing': 1},
            'issues': [
                {'issue': 1, 'title': 't', 'verdict': 'verified',
                 'checks': [], 'manual_required': []},
                {'issue': 2, 'title': 't', 'verdict': 'failing',
                 'checks': [
                     {'id': 'c', 'kind': 'script', 'status': 'failed',
                      'attempts': [{'status': 'failed'}, {'status': 'failed'}],
                      'flaky': False, 'cached': False},
                 ], 'manual_required': []},
            ],
            'unmapped_issues': [],
            'not_open_issues': [],
        }
        md = verify.render_markdown(report)
        assert '## At a glance' in md
        assert 'Close candidates' in md and '#1' in md
        assert 'Needs attention' in md and '#2' in md
        assert 'failed→failed' in md

    def test_unrequested_manifest_entries_not_flagged_stale(
        self, tmp_path, monkeypatch
    ):
        """--issues 10 must not mark open-but-unrequested #11 as not_open."""
        monkeypatch.setattr(verify, 'find_token', lambda: 'tok')
        monkeypatch.setattr(
            verify, 'fetch_open_issues',
            lambda repo, token: [
                {'number': 10, 'title': 'ten'},
                {'number': 11, 'title': 'eleven'},
            ],
        )
        text = (
            'version: 1\nrepo: a/b\nissues:\n'
            '  - issue: 10\n    checks:\n'
            + _script_check('a', 'import sys; sys.exit(0)') + '\n'
            '  - issue: 11\n    checks:\n'
            + _script_check('b', 'import sys; sys.exit(0)')
        )
        manifest_path = _write_manifest(tmp_path, text)
        report_dir = tmp_path / 'report'
        code = verify.main([
            '--manifest', str(manifest_path),
            '--issues', '10',
            '--report-dir', str(report_dir),
        ])
        assert code == 0
        report = json.loads(
            (report_dir / 'issue_verification_report.json')
            .read_text(encoding='utf-8')
        )
        assert [i['issue'] for i in report['issues']] == [10]
        assert report['not_open_issues'] == []
