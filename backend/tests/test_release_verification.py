"""Tests for the release-candidate verification gate (issue #833).

Covers scripts/release_verification_manifest.yaml (schema validity, the
declared classes stay runnable), scripts/run_release_verification.py
(manifest parsing, fail-closed verdict computation, capability skipping,
scoped-vs-full coverage, evidence schema), scripts/package_smoke.py exit
contract, and the build-installer.ps1 / workflow provenance binding that
references the evidence identity. Everything is offline and local.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # register before exec: dataclass field introspection reads sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rv = _load(
    'run_release_verification',
    ROOT / 'scripts' / 'run_release_verification.py',
)
smoke = _load('package_smoke', ROOT / 'scripts' / 'package_smoke.py')

REAL_MANIFEST = ROOT / 'scripts' / 'release_verification_manifest.yaml'
PY = sys.executable.replace('\\', '/')


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'manifest.yaml'
    path.write_text(body, encoding='utf-8')
    return path


def _script_check(cid: str, *argv_tail: str, **extra) -> str:
    lines = [
        f'      - id: {cid}',
        '        kind: script',
        '        command:',
        f'          - "{{python}}"',
    ]
    lines += [f'          - "{a}"' for a in argv_tail]
    lines.append('        description: test check')
    for key, value in extra.items():
        lines.append(f'        {key}: {value}')
    return '\n'.join(lines)


def _manifest_yaml(
    checks_block: str,
    *,
    class_attrs: str = '',
    external: bool = True,
) -> str:
    ext = (
        'external_gates:\n'
        '  - id: owned-room\n'
        '    title: physical gate\n'
        '    evidence: manual evidence description\n'
        if external else ''
    )
    return (
        'version: 1\n'
        'repo: ka0923s-a11y/HTDT\n'
        + ext
        + 'classes:\n'
        + '  - id: test-class\n'
        + '    title: test class\n'
        + '    description: synthetic class\n'
        + class_attrs
        + '    checks:\n'
        + checks_block
        + '\n'
    )


class TestManifestParse:
    def test_real_manifest_parses(self):
        manifest = rv.load_manifest(REAL_MANIFEST)
        assert manifest.repo == 'ka0923s-a11y/HTDT'
        assert manifest.version == 1
        assert len(manifest.sha256) == 64
        ids = [c.id for c in manifest.classes]
        assert len(ids) == len(set(ids)), 'duplicate class ids'
        check_ids = [
            chk.id for c in manifest.classes for chk in c.checks
        ]
        assert len(check_ids) == len(set(check_ids)), (
            'check ids must be unique manifest-wide (they name log files)'
        )
        # The required set is what release-candidate evidence must cover —
        # every issue-#833 class is present.
        required_ids = {c.id for c in manifest.classes if c.required}
        assert {
            'dependency-lock',
            'schema-migration',
            'authority-invariants',
            'persistence',
            'backup-restore',
            'measurement-evidence',
            'recommendation-gating',
            'solver-provenance',
            'native-startup-smoke',
        } <= required_ids

    def test_real_manifest_targets_exist(self):
        """Every pytest path and .py script in the manifest must exist —
        the release gate is deterministic only when it cannot rot silently."""
        manifest = rv.load_manifest(REAL_MANIFEST)
        for cls in manifest.classes:
            for chk in cls.checks:
                if chk.kind == 'pytest':
                    for node in chk.tests:
                        file_part = node.split('::')[0]
                        assert (ROOT / file_part).is_file(), (
                            f'{cls.id}/{chk.id}: missing test file '
                            f'{file_part}'
                        )
                else:
                    script_args = [
                        a for a in chk.command
                        if a.endswith('.py') and not a.startswith('{')
                    ]
                    for a in script_args:
                        assert (ROOT / a).is_file(), (
                            f'{cls.id}/{chk.id}: missing script {a}'
                        )

    def test_real_manifest_declares_external_gates(self):
        """Owned-room/GPU/UX160 stay separately typed gates — never folded
        into the software PASS."""
        manifest = rv.load_manifest(REAL_MANIFEST)
        gate_ids = {g.id for g in manifest.external_gates}
        assert {
            'owned-room-physical',
            'gpu-solver-validation',
            'ux160-windows-acceptance',
        } <= gate_ids

    def test_required_classes_cannot_be_capability_gated(self, tmp_path):
        """A required class that silently skips when the environment lacks
        a capability would be a fail-open hole — the schema forbids it."""
        body = _manifest_yaml(
            _script_check('c1', '-c', 'pass'),
            class_attrs='    required: true\n'
                        '    requires_capability: inno_setup\n',
        )
        with pytest.raises(rv.ManifestError, match='required'):
            rv.load_manifest(_write_manifest(tmp_path, body))

    def test_unknown_capability_fails_closed(self, tmp_path):
        body = _manifest_yaml(
            _script_check('c1', '-c', 'pass'),
            class_attrs='    required: false\n'
                        '    requires_capability: teleportation\n',
        )
        with pytest.raises(rv.ManifestError, match='unknown'):
            rv.load_manifest(_write_manifest(tmp_path, body))

    def test_duplicate_check_ids_fail_closed(self, tmp_path):
        body = _manifest_yaml(
            _script_check('dup', '-c', 'pass')
            + '\n'
            + _script_check('dup', '-c', 'pass'),
        )
        with pytest.raises(rv.ManifestError, match='unique'):
            rv.load_manifest(_write_manifest(tmp_path, body))

    def test_bad_version_fails(self, tmp_path):
        path = _write_manifest(
            tmp_path,
            'version: 2\nrepo: a/b\nclasses: []\n',
        )
        with pytest.raises(rv.ManifestError, match='version'):
            rv.load_manifest(path)

    def test_missing_manifest_fails(self, tmp_path):
        with pytest.raises(rv.ManifestError, match='not found'):
            rv.load_manifest(tmp_path / 'nope.yaml')


class TestSelection:
    def _manifest(self, tmp_path: Path) -> rv.ReleaseManifest:
        body = (
            'version: 1\n'
            'repo: ka0923s-a11y/HTDT\n'
            'classes:\n'
            '  - id: fast\n'
            '    title: fast\n'
            '    description: d\n'
            '    include_in_dev: true\n'
            '    checks:\n'
            + _script_check('c1', '-c', 'pass')
            + '\n'
            + '  - id: slow\n'
            '    title: slow\n'
            '    description: d\n'
            '    checks:\n'
            + _script_check('c2', '-c', 'pass')
            + '\n'
        )
        return rv.load_manifest(_write_manifest(tmp_path, body))

    def test_dev_profile_is_bounded_subset(self, tmp_path):
        manifest = self._manifest(tmp_path)
        picked = rv.select_classes(manifest, 'dev', None)
        assert [c.id for c in picked] == ['fast']

    def test_release_profile_runs_everything(self, tmp_path):
        manifest = self._manifest(tmp_path)
        picked = rv.select_classes(manifest, 'release', None)
        assert [c.id for c in picked] == ['fast', 'slow']

    def test_unknown_class_selection_fails(self, tmp_path):
        manifest = self._manifest(tmp_path)
        with pytest.raises(rv.ManifestError, match='unknown ids'):
            rv.select_classes(manifest, 'release', ['nope'])


def _outcome(cid: str, status: str, skip_reason: str | None = None):
    chk = rv.ReleaseCheck(
        id=cid, kind='script', description='d', command=['x'],
    )
    return rv.CheckOutcome(
        check=chk, status=status, skip_reason=skip_reason,
    )


def _cls(cid: str, required: bool) -> rv.CheckClass:
    return rv.CheckClass(
        id=cid, title=cid, description='d', required=required,
        include_in_dev=False, requires_capability=None, checks=[],
    )


class TestVerdict:
    def test_all_green_passes(self):
        verdict, reasons = rv.compute_verdict(
            [(_cls('a', True), [_outcome('c', 'passed')])],
            run_completed=True,
        )
        assert verdict == 'passed' and not reasons

    def test_timeout_is_never_pass(self):
        verdict, _ = rv.compute_verdict(
            [(_cls('a', True), [_outcome('c', 'timeout')])],
            run_completed=True,
        )
        assert verdict == 'failed'

    def test_optional_skip_with_reason_passes(self):
        verdict, _ = rv.compute_verdict(
            [(_cls('opt', False), [
                _outcome('c', 'skipped', 'capability x unsupported: y'),
            ])],
            run_completed=True,
        )
        assert verdict == 'passed'

    def test_required_skip_fails(self):
        verdict, reasons = rv.compute_verdict(
            [(_cls('req', True), [
                _outcome('c', 'skipped', 'somehow skipped'),
            ])],
            run_completed=True,
        )
        assert verdict == 'failed'
        assert any('req/c' in r for r in reasons)

    def test_optional_executed_failure_still_fails(self):
        """An optional check that ran and found breakage is red evidence —
        'optional' governs whether absence is excused, not whether a
        failure can be ignored."""
        verdict, _ = rv.compute_verdict(
            [(_cls('opt', False), [_outcome('c', 'failed')])],
            run_completed=True,
        )
        assert verdict == 'failed'

    def test_interrupted_run_is_incomplete(self):
        verdict, _ = rv.compute_verdict(
            [(_cls('a', True), [_outcome('c', 'passed')])],
            run_completed=False,
        )
        assert verdict == 'incomplete'


class TestRunnerEndToEnd:
    """End-to-end over a tiny synthetic manifest — real subprocesses,
    real evidence files, no network."""

    def test_green_run_writes_evidence(self, tmp_path):
        manifest = _write_manifest(
            tmp_path,
            _manifest_yaml(_script_check('ok', '-c', 'pass')),
        )
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
            '--rerun-failed', '0',
        ])
        assert code == 0
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        assert evidence['schema'] == rv.SCHEMA_ID
        assert evidence['verdict'] == 'passed'
        assert evidence['coverage'] == 'full'
        assert evidence['run_completed'] is True
        # evidence is bound to the exact revision and toolchain
        assert evidence['revision']['commit_sha']
        assert isinstance(evidence['revision']['dirty'], bool)
        assert len(evidence['manifest']['sha256']) == 64
        assert evidence['toolchain']['python_version']
        assert evidence['toolchain']['environment_fingerprint']
        assert evidence['external_gates']
        chk = evidence['classes'][0]['checks'][0]
        assert chk['status'] == 'passed'
        assert (report_dir / chk['log_path']).is_file()
        mds = list(report_dir.glob('release-verification-*.md'))
        assert mds, 'markdown evidence missing'

    def test_failing_check_exits_1_and_never_marks_pass(self, tmp_path):
        manifest = _write_manifest(
            tmp_path,
            _manifest_yaml(_script_check('boom', '-c', 'import sys; sys.exit(1)')),
        )
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
            '--rerun-failed', '0',
        ])
        assert code == 1
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        assert evidence['verdict'] == 'failed'
        assert evidence['verdict_reasons']

    def test_capability_skip_records_reason(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            rv.CAPABILITY_PROBES,
            'inno_setup',
            lambda root: (False, 'probe says no'),
        )
        body = _manifest_yaml(
            _script_check('c1', '-c', 'pass'),
            class_attrs='    required: false\n'
                        '    requires_capability: inno_setup\n',
        )
        manifest = _write_manifest(tmp_path, body)
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
        ])
        assert code == 0
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        chk = evidence['classes'][0]['checks'][0]
        assert chk['status'] == 'skipped'
        assert chk['skip_reason'] == (
            'capability inno_setup unsupported: probe says no'
        )

    def test_interrupted_run_never_reports_pass(self, tmp_path, monkeypatch):
        manifest = _write_manifest(
            tmp_path,
            _manifest_yaml(
                _script_check('first', '-c', 'pass')
                + '\n'
                + _script_check('second', '-c', 'pass'),
            ),
        )
        calls = {'n': 0}

        def _interrupt(*a, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                raise KeyboardInterrupt
            raise AssertionError('unreachable')

        monkeypatch.setattr(rv, 'run_check', _interrupt)
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
        ])
        assert code == 3
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        assert evidence['verdict'] == 'incomplete'
        assert evidence['run_completed'] is False
        statuses = {
            c['status'] for c in evidence['classes'][0]['checks']
        }
        assert 'not_executed' in statuses

    def test_scoped_run_records_scoped_coverage(self, tmp_path):
        manifest = _write_manifest(
            tmp_path,
            _manifest_yaml(_script_check('ok', '-c', 'pass')),
        )
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
            '--classes', 'test-class',
        ])
        assert code == 0
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        assert evidence['verdict'] == 'passed'
        # a scoped run is honest about what it did not cover
        assert evidence['coverage'] == 'scoped'
        assert evidence['selection']['requested_classes'] == ['test-class']

    def test_empty_selection_is_tool_error(self, tmp_path):
        body = (
            'version: 1\nrepo: a/b\nclasses:\n'
            '  - id: only-release\n'
            '    title: t\n'
            '    description: d\n'
            '    checks:\n'
            + _script_check('c1', '-c', 'pass')
            + '\n'
        )
        manifest = _write_manifest(tmp_path, body)
        # --classes names a real class, but the dev profile excludes it —
        # an empty effective selection must not produce a verdict.
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--profile', 'dev',
            '--classes', 'only-release',
        ])
        assert code == 2

    def test_missing_test_path_is_check_error(self, tmp_path):
        body = _manifest_yaml(
            '      - id: pt\n'
            '        kind: pytest\n'
            '        tests:\n'
            '          - backend/tests/does_not_exist_xyz.py\n'
            '        description: missing file\n',
        )
        manifest = _write_manifest(tmp_path, body)
        report_dir = tmp_path / 'report'
        code = rv.main([
            '--manifest', str(manifest),
            '--repo-root', str(ROOT),
            '--report-dir', str(report_dir),
            '--python', PY,
        ])
        assert code == 1
        evidence = json.loads(
            (report_dir / rv.EVIDENCE_JSON).read_text(encoding='utf-8')
        )
        chk = evidence['classes'][0]['checks'][0]
        assert chk['status'] == 'error'
        assert 'not found' in chk['detail']


class TestPackageSmoke:
    def test_missing_package_exits_2(self, tmp_path):
        code = smoke.main([
            '--package-dir', str(tmp_path / 'no-such-package'),
            '--repo-root', str(ROOT),
        ])
        assert code == 2

    def test_unreadable_build_info_exits_2(self, tmp_path):
        pkg = tmp_path / 'HTDT'
        info = pkg / smoke.BUILD_INFO_REL
        info.parent.mkdir(parents=True)
        (pkg / 'HTDT.exe').write_bytes(b'x')
        info.write_text('not json', encoding='utf-8')
        code = smoke.main([
            '--package-dir', str(pkg),
            '--repo-root', str(ROOT),
        ])
        assert code == 2


class TestReleaseBinding:
    """The packaged-artifact side of the gate: manifests must be able to
    reference the evidence identity and unverified builds must be visibly
    distinguishable."""

    def test_installer_script_exposes_evidence_params(self):
        text = (ROOT / 'scripts' / 'build-installer.ps1').read_text(
            encoding='utf-8'
        )
        assert 'VerificationEvidence' in text
        assert 'RequireVerification' in text
        assert 'verification' in text
        assert 'verification_evidence.ps1' in text

    def test_evidence_helper_enforces_full_green_same_commit(self):
        text = (ROOT / 'scripts' / 'verification_evidence.ps1').read_text(
            encoding='utf-8'
        )
        for needle in (
            'htdt-release-verification/1',
            'passed',
            'release',
            'full',
            'unverified',
            'rejected',
        ):
            assert needle in text, needle

    def test_workflow_runs_gate_and_binds_evidence(self):
        text = (
            ROOT / '.github' / 'workflows' / 'build-windows-artifacts.yml'
        ).read_text(encoding='utf-8')
        assert 'run_software_verification' in text
        assert 'run_release_verification.py --profile release' in text
        assert 'VerificationEvidence' in text
        assert 'RequireVerification' in text
        # evidence is uploaded even when the gate is red
        assert 'always()' in text
