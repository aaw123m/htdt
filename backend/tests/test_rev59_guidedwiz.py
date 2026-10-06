"""REV59-GUIDEDWIZ: guided verification wizard data-path tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.cad_manifest_verification import (  # noqa: E402
    evaluate_gate,
    evaluate_issue_verdict,
    load_manifest_gates,
)
from htdt.verification_wizard import (  # noqa: E402
    ManifestGateStore,
    WizardCheck,
    load_wizard_issues,
    resolve_check_argv,
    run_check,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST = REPO_ROOT / 'scripts' / 'issue_verification_manifest.yaml'

DOC = 'application'


def _manifest(tmp_path: Path, body: str = '') -> Path:
    p = tmp_path / 'manifest.yaml'
    p.write_text(
        'version: 1\n'
        'issues:\n'
        '  - issue: 42\n'
        '    title: サンプル課題\n'
        '    checks:\n'
        '      - id: auto-1\n'
        '        kind: pytest\n'
        '        tests: [backend/tests/test_rev59_guidedwiz.py]\n'
        '        timeout_seconds: 300\n'
        '        pytest_workers: 2\n'
        '        description: 自動チェック\n'
        '      - id: script-1\n'
        '        kind: script\n'
        '        command: ["{python}", "-c", "import sys; sys.exit(0)"]\n'
        '        description: スクリプトチェック\n'
        '      - id: phys-1\n'
        '        kind: manual\n'
        '        description: 実機で測定音を確認\n'
        + body,
        encoding='utf-8',
    )
    return p


def _store(tmp_path: Path) -> ManifestGateStore:
    return ManifestGateStore(tmp_path / 'cad-scenes.sqlite3', DOC)


# ---------------------------------------------------------------------------
# manifest loading — including the real manifest


def test_load_wizard_issues_synthetic(tmp_path):
    issues = load_wizard_issues(_manifest(tmp_path))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.issue_ref == 'issue-42' and issue.title == 'サンプル課題'
    kinds = {c.check_id: c.kind for c in issue.checks}
    assert kinds == {
        'auto-1': 'pytest',
        'script-1': 'script',
        'phys-1': 'manual',
    }
    auto = next(c for c in issue.checks if c.check_id == 'auto-1')
    assert auto.tests == ('backend/tests/test_rev59_guidedwiz.py',)
    assert auto.timeout_seconds == 300 and auto.pytest_workers == 2
    assert auto.issue_ref == 'issue-42'
    script = next(c for c in issue.checks if c.check_id == 'script-1')
    assert script.command[0] == '{python}'


def test_load_wizard_issues_rejects_bad_kind(tmp_path):
    p = tmp_path / 'bad.yaml'
    p.write_text(
        'issues:\n  - issue: 1\n    checks:\n'
        '      - id: x\n        kind: telepathy\n        description: d\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='unknown check kind'):
        load_wizard_issues(p)


@pytest.mark.skipif(
    not REAL_MANIFEST.is_file(), reason='repo manifest not present'
)
def test_real_manifest_loads_end_to_end(tmp_path):
    """The bridge seals the real manifest: the script check uses
    ``command:`` (not ``argv:``), and every issue entry is unique."""
    issues = load_wizard_issues(REAL_MANIFEST)
    assert len(issues) == 188
    kinds = [c.kind for i in issues for c in i.checks]
    assert kinds.count('pytest') == 204
    assert kinds.count('script') == 1
    assert kinds.count('manual') == 115

    gates = load_manifest_gates(DOC, REAL_MANIFEST)
    assert len(gates) == len(kinds) == 320
    script_gate = next(g for g in gates if g.check_kind == 'script')
    assert script_gate.argv[0] == '{python}'
    assert 'golden_path_preflight.py' in script_gate.argv[1]


# ---------------------------------------------------------------------------
# argv construction + bounded run


def test_resolve_argv_pytest(tmp_path):
    check = WizardCheck(
        check_id='c', kind='pytest', description='',
        tests=('backend/tests/test_rev59_guidedwiz.py',), pytest_workers=1,
    )
    argv = resolve_check_argv(
        check,
        python='PY', work_dir=tmp_path / 'w', report_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert argv[:3] == ['PY', '-m', 'pytest']
    assert 'backend/tests/test_rev59_guidedwiz.py' in argv
    assert '-n' in argv and '1' in argv
    assert '--basetemp=' in argv[-1]


def test_resolve_argv_script_substitutes(tmp_path):
    check = WizardCheck(
        check_id='c', kind='script', description='',
        command=('{python}', 's.py', '--out', '{report_dir}/r.json'),
    )
    argv = resolve_check_argv(
        check, python='PY', work_dir=tmp_path / 'w',
        report_dir=tmp_path, repo_root=REPO_ROOT,
    )
    assert argv[0] == 'PY'
    assert argv[3].endswith('r.json') and '{report_dir}' not in argv[3]


def test_resolve_argv_missing_tests_fail_closed(tmp_path):
    check = WizardCheck(
        check_id='c', kind='pytest', description='',
        tests=('no/such/test_file.py',),
    )
    with pytest.raises(FileNotFoundError):
        resolve_check_argv(
            check, python='PY', work_dir=tmp_path,
            report_dir=tmp_path, repo_root=REPO_ROOT,
        )


def test_resolve_argv_manual_rejected(tmp_path):
    check = WizardCheck(check_id='c', kind='manual', description='')
    with pytest.raises(ValueError):
        resolve_check_argv(
            check, python='PY', work_dir=tmp_path,
            report_dir=tmp_path, repo_root=REPO_ROOT,
        )


def test_run_check_pass_fail_timeout(tmp_path):
    ok = WizardCheck(
        check_id='ok', kind='script', description='',
        command=(sys.executable, '-c', 'import sys; sys.exit(0)'),
    )
    outcome = run_check(ok, repo_root=REPO_ROOT, report_dir=tmp_path)
    assert outcome.status == 'passed'
    assert outcome.log_path is not None and outcome.log_path.is_file()

    bad = WizardCheck(
        check_id='bad', kind='script', description='',
        command=(sys.executable, '-c', 'import sys; sys.exit(3)'),
    )
    outcome = run_check(bad, repo_root=REPO_ROOT, report_dir=tmp_path)
    assert outcome.status == 'failed' and '3' in outcome.detail

    slow = WizardCheck(
        check_id='slow', kind='script', description='',
        command=(sys.executable, '-c', 'import time; time.sleep(30)'),
        timeout_seconds=2,
    )
    outcome = run_check(slow, repo_root=REPO_ROOT, report_dir=tmp_path)
    assert outcome.status == 'timeout'

    missing = WizardCheck(
        check_id='gone', kind='pytest', description='',
        tests=('no/such/file.py',),
    )
    outcome = run_check(missing, repo_root=REPO_ROOT, report_dir=tmp_path)
    assert outcome.status == 'error'


# ---------------------------------------------------------------------------
# store: outcomes, evidence commits, verdicts


def test_store_records_outcome_and_binds_log(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(tmp_path)
    gates = store.load_gates(manifest)
    auto = next(g for g in gates if g.check_id == 'auto-1')
    from htdt.verification_wizard import CheckRunOutcome

    outcome = CheckRunOutcome(
        status='passed', detail='', duration_s=1.5,
        log_path=tmp_path / 'auto-1.log',
    )
    outcome.log_path.write_bytes(b'argv: pytest\nok\n')
    result = store.record_check_outcome(gate=auto, outcome=outcome)
    assert result.outcome == 'passed'
    assert 'log sha256=' in result.detail
    # stored result replay
    latest = store.latest_results()
    assert latest[auto.gate_id].result_id == result.result_id


def test_manual_gate_satisfied_only_by_committed_evidence(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(tmp_path)
    gates = store.load_gates(manifest)
    manual = next(g for g in gates if g.check_id == 'phys-1')
    auto = next(g for g in gates if g.check_id == 'auto-1')
    script = next(g for g in gates if g.check_id == 'script-1')

    from htdt.verification_wizard import CheckRunOutcome

    # auto + script pass → issue partially_verified (manual gate still open)
    store.record_check_outcome(
        gate=auto,
        outcome=CheckRunOutcome(
            status='passed', detail='', duration_s=0.1, log_path=None,
        ),
    )
    store.record_check_outcome(
        gate=script,
        outcome=CheckRunOutcome(
            status='passed', detail='', duration_s=0.1, log_path=None,
        ),
    )
    v, _ = store.issue_verdict(gates)
    assert v == 'partially_verified'

    # a 'passed' outcome on the manual gate does NOT satisfy it
    store.record_check_outcome(
        gate=manual,
        outcome=CheckRunOutcome(
            status='passed', detail='', duration_s=0.1, log_path=None,
        ),
    )
    v, reason = evaluate_gate(
        manual, store.latest_results()[manual.gate_id]
    )
    assert v == 'unsatisfied'

    # committed evidence does
    committed = store.commit_evidence(
        gate=manual,
        files=(('note.txt', b'observed output'),),
        note='実機で確認しました',
    )
    assert committed.outcome == 'evidence_committed'
    assert committed.evidence_ref is not None
    digest = committed.evidence_ref.ref_sha256
    bundle = store.assets.read_verified(digest)
    assert bundle is not None and b'note.txt' in bundle
    v, _ = store.issue_verdict(gates)
    assert v == 'verified'


def test_evidence_bundle_replayable(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(tmp_path)
    gates = store.load_gates(manifest)
    manual = next(g for g in gates if g.check_id == 'phys-1')
    committed = store.commit_evidence(
        gate=manual, note='波形を確認', files=(),
    )
    ref = committed.evidence_ref
    assert ref.kind == 'evidence'
    bundle = store.assets.read_verified(ref.ref_sha256)
    import json

    parsed = json.loads(bundle.decode('utf-8'))
    assert parsed['note'] == '波形を確認'
    assert parsed['check_id'] == 'phys-1'


def test_latest_results_picks_newest(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(tmp_path)
    gates = store.load_gates(manifest)
    auto = next(g for g in gates if g.check_id == 'auto-1')
    from htdt.verification_wizard import CheckRunOutcome

    store.record_check_outcome(
        gate=auto,
        outcome=CheckRunOutcome(
            status='failed', detail='x', duration_s=0.1, log_path=None,
        ),
    )
    v, _ = evaluate_issue_verdict(gates, store.latest_results())
    assert v == 'failing'
    store.record_check_outcome(
        gate=auto,
        outcome=CheckRunOutcome(
            status='passed', detail='', duration_s=0.1, log_path=None,
        ),
    )
    v, _ = evaluate_issue_verdict(gates, store.latest_results())
    # auto green, manual open → partially_verified (script gate still open)
    assert v == 'partially_verified'


# ---------------------------------------------------------------------------
# page smoke (offscreen)


def test_page_lists_issues_and_commits_evidence(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from htdt.verification_wizard_page import VerificationWizardPage

    manifest = _manifest(tmp_path)
    page = VerificationWizardPage(
        tmp_path / 'data', repo_root=REPO_ROOT, manifest_path=manifest
    )
    assert page.issue_list.count() == 1
    item = page.issue_list.item(0)
    assert '一部検証済み' in item.text()

    page.issue_list.setCurrentRow(0)
    assert page.check_list.count() == 3
    # auto gates exist but nothing has run → partially_verified ladder
    assert '一部検証済み' in page.verdict_label.text()

    # select the manual check → guided controls enabled
    page.check_list.setCurrentRow(2)
    assert page.commit_button.isEnabled()
    assert page.attest_edit.isEnabled()
    page.attest_edit.setPlainText('実機で再生を確認しました')
    page._commit_manual_evidence()
    # the manual gate now has committed evidence
    manual_gate = next(
        g for g in page.store.repository.list_manifest_gates()
        if g.check_id == 'phys-1'
    )
    latest = page.store.latest_results()[manual_gate.gate_id]
    assert latest.outcome == 'evidence_committed'
    assert '一部検証済み' in page.verdict_label.text()
    page.close()
    page.deleteLater()


def test_page_surfaces_missing_manifest(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from htdt.verification_wizard_page import VerificationWizardPage

    page = VerificationWizardPage(
        tmp_path / 'data',
        repo_root=tmp_path,
        manifest_path=tmp_path / 'absent.yaml',
    )
    assert page.issue_list.count() == 0
    assert '読み込めません' in page.issue_title.text()
    page.close()
    page.deleteLater()
