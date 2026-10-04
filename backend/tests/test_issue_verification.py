"""Tests for scripts/verify_open_issues.py (issue-verification runner).

Covers manifest parsing/validation (fail-closed), verdict computation, the
offline runner path, --dry-run, and --post-summary-from error handling.
Everything is offline; no GitHub API calls are made.
"""

from __future__ import annotations

import importlib.util
import json
import sys
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
