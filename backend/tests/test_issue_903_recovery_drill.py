"""Tests for the #903 fault-injection recovery drill harness.

Coverage: manifest integrity/enumerability, every drill verdict path
(recovered_as_designed / loss_detected / false_success / harness_error),
fail-closed evaluator rules, report sealing + tamper detection, and the
``htdt drill`` CLI family (list, dry-run, --arm gating, sealed records).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt import cad_recovery_drill as drill_mod
from htdt.cad_recovery_drill import (
    FAILING_DRILL_VERDICTS,
    RECOVERY_DRILL_MANIFEST_FORMAT,
    RECOVERY_DRILL_REPORT_FORMAT,
    DrillCheck,
    DrillContext,
    RecoveryDrillExpectation,
    DrillObservation,
    RecoveryDrillSpec,
    builtin_drill_manifest,
    evaluate_drill,
    run_drill,
    run_drill_manifest,
    select_drills,
)
from htdt.canonical_json import canonical_sha256
from htdt.headless_cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_FILE = REPO_ROOT / 'scripts' / 'recovery_drill_manifest.json'

EXPECTED_DRILL_IDS = {
    'session-crash-mid-write',
    'session-journal-torn-tail',
    'update-crash-mid-swap',
    'update-crash-mid-rollback',
    'update-health-unverifiable',
    'diagnostics-bundle-collector-failure',
    'vault-locked-denies-material',
    'vault-unavailable-fails-closed',
    'error-boundary-authority-honesty',
}


def _ctx(tmp_path: Path) -> DrillContext:
    return DrillContext(
        work_root=tmp_path / 'drills', document_id='doc-test')


def _spec(**overrides) -> RecoveryDrillSpec:
    base = dict(
        drill_id='t-drill',
        title='test drill',
        issue_refs=('#903',),
        fault='process_kill_mid_write',
        fault_summary='kill the process mid-write',
        recovery_expectation='journal reconciles, no silent loss',
        mutating=True,
        implementation='_impl_noop',
        expectation=RecoveryDrillExpectation(
            expected_terminal='done',
            expected_records=('session_recovery_journal',),
            forbidden_terminals=('silent_ok',),
            required_checks=('landing', 'honest'),
        ),
    )
    base.update(overrides)
    return RecoveryDrillSpec(**base)


def _check(check_id: str, kind: str, passed: bool,
           detail: str = '') -> DrillCheck:
    return DrillCheck(
        check_id=check_id, kind=kind, passed=passed, detail=detail)


def _obs(terminal: str = 'done',
         checks=(_check('landing', 'expectation', True),
                 _check('honest', 'honesty', True)),
         records=()) -> DrillObservation:
    return DrillObservation(
        terminal=terminal, records=records, checks=checks)


def _journal_ref() -> drill_mod.AuthorityRef:
    return drill_mod.AuthorityRef(
        kind='session_recovery_journal',
        ref_id='srjrn-x', ref_sha256='0' * 64)


# --- manifest -----------------------------------------------------------


def test_builtin_manifest_sealed_deterministic_and_complete() -> None:
    manifest = builtin_drill_manifest()
    assert manifest.format == RECOVERY_DRILL_MANIFEST_FORMAT
    assert builtin_drill_manifest().manifest_sha256 == (
        manifest.manifest_sha256)
    payload = manifest.model_dump(mode='json')
    payload['manifest_sha256'] = ''
    assert manifest.manifest_sha256 == canonical_sha256(payload)
    ids = [d.drill_id for d in manifest.drills]
    assert len(ids) == len(set(ids)) == 9
    assert set(ids) == EXPECTED_DRILL_IDS
    for spec in manifest.drills:
        assert spec.issue_refs
        assert spec.implementation in drill_mod._IMPLEMENTATIONS
        assert spec.expectation.expected_terminal
        assert spec.expectation.required_checks


def test_manifest_file_mirrors_builtin() -> None:
    payload = json.loads(MANIFEST_FILE.read_text(encoding='utf-8'))
    assert payload == builtin_drill_manifest().model_dump(mode='json')


def test_select_drills_subset_and_unknown_id() -> None:
    manifest = builtin_drill_manifest()
    picked = select_drills(manifest, ('vault-locked-denies-material',))
    assert [s.drill_id for s in picked] == [
        'vault-locked-denies-material']
    assert len(select_drills(manifest, None)) == 9
    with pytest.raises(ValueError):
        select_drills(manifest, ('no-such-drill',))


# --- evaluator verdict paths --------------------------------------------


def test_evaluate_recovered_as_designed() -> None:
    result = evaluate_drill(
        _spec(), _obs(records=(_journal_ref(),)))
    assert result.verdict == 'recovered_as_designed'


def test_evaluate_terminal_mismatch_is_loss_detected() -> None:
    result = evaluate_drill(_spec(), _obs(terminal='crashed'))
    assert result.verdict == 'loss_detected'
    assert 'crashed' in result.detail


def test_evaluate_forbidden_terminal_is_false_success() -> None:
    result = evaluate_drill(_spec(), _obs(terminal='silent_ok'))
    assert result.verdict == 'false_success'


def test_evaluate_honesty_failure_is_false_success() -> None:
    obs = _obs(checks=(
        _check('landing', 'expectation', True),
        _check('honest', 'honesty', False, 'reported ok'),
    ))
    result = evaluate_drill(_spec(), obs)
    assert result.verdict == 'false_success'
    assert 'honest' in result.detail


def test_evaluate_preservation_failure_is_loss_detected() -> None:
    obs = _obs(checks=(
        _check('landing', 'expectation', True),
        _check('honest', 'preservation', False, 'bytes diverged'),
    ))
    result = evaluate_drill(_spec(), obs)
    assert result.verdict == 'loss_detected'


def test_evaluate_expectation_failure_is_loss_detected() -> None:
    obs = _obs(checks=(
        _check('landing', 'expectation', False, 'not restored'),
        _check('honest', 'honesty', True),
    ))
    result = evaluate_drill(_spec(), obs)
    assert result.verdict == 'loss_detected'


def test_evaluate_missing_expected_record_is_loss_detected() -> None:
    result = evaluate_drill(_spec(), _obs())
    assert result.verdict == 'loss_detected'
    assert 'session_recovery_journal' in result.detail


def test_evaluate_missing_required_check_is_harness_error() -> None:
    obs = _obs(checks=(_check('landing', 'expectation', True),))
    result = evaluate_drill(_spec(), obs)
    assert result.verdict == 'harness_error'
    assert 'honest' in result.detail


def test_run_drill_unknown_implementation_is_harness_error(
        tmp_path: Path) -> None:
    result = run_drill(
        _spec(implementation='no-such-impl'), _ctx(tmp_path))
    assert result.verdict == 'harness_error'


def test_drill_check_kind_is_literal_closed() -> None:
    with pytest.raises(Exception):
        _check('mystery', 'bogus', True)


def test_run_drill_impl_exception_is_harness_error(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(ctx, spec):
        raise RuntimeError('impl exploded')

    monkeypatch.setitem(
        drill_mod._IMPLEMENTATIONS, '_impl_noop', _boom)
    result = run_drill(_spec(), _ctx(tmp_path))
    assert result.verdict == 'harness_error'
    assert 'impl exploded' in result.detail


# --- builtin drills end-to-end ------------------------------------------


@pytest.mark.parametrize('drill_id', sorted(EXPECTED_DRILL_IDS))
def test_each_builtin_drill_recovers_as_designed(
        tmp_path: Path, drill_id: str) -> None:
    manifest = builtin_drill_manifest()
    spec = select_drills(manifest, (drill_id,))[0]
    result = run_drill(spec, _ctx(tmp_path))
    assert result.verdict == 'recovered_as_designed', (
        f'{drill_id}: {result.verdict} — {result.detail} — '
        f'failed checks: '
        f'{[c.check_id for c in result.checks if not c.passed]}')
    assert result.observed_terminal == (
        spec.expectation.expected_terminal)
    emitted = {c.check_id for c in result.checks}
    assert set(spec.expectation.required_checks) <= emitted


def test_run_manifest_report_sealed_and_overall(
        tmp_path: Path) -> None:
    manifest = builtin_drill_manifest()
    report = run_drill_manifest(
        manifest, _ctx(tmp_path),
        started_at_utc='2026-10-08T00:00:00Z',
        finished_at_utc='2026-10-08T00:01:00Z',
        elapsed_ms=60000)
    assert report.format == RECOVERY_DRILL_REPORT_FORMAT
    assert report.overall_verdict == 'all_recovered'
    assert len(report.results) == 9
    payload = report.model_dump(mode='json')
    payload['report_sha256'] = ''
    assert report.report_sha256 == canonical_sha256(payload)


def test_report_tamper_detected_by_sha(tmp_path: Path) -> None:
    manifest = builtin_drill_manifest()
    report = run_drill_manifest(
        manifest, _ctx(tmp_path),
        started_at_utc='a', finished_at_utc='b', elapsed_ms=1)
    tampered = report.model_copy(update={
        'results': (
            report.results[0].model_copy(
                update={'verdict': 'loss_detected'}),
        ) + report.results[1:]})
    payload = tampered.model_dump(mode='json')
    payload['report_sha256'] = ''
    assert canonical_sha256(payload) != report.report_sha256


# --- CLI ----------------------------------------------------------------


def _run(capsys, *argv):
    code = cli_main([str(a) for a in argv])
    out = capsys.readouterr().out.strip().splitlines()
    env = json.loads(out[-1])
    return code, env


def test_cli_drill_list(tmp_path, capsys) -> None:
    code, env = _run(
        capsys, 'drill', 'list', '--json',
        '--data-dir', tmp_path / 'd')
    assert code == 0
    assert env['outcome'] == 'succeeded'
    assert env['data']['count'] == 9
    assert env['data']['manifest_sha256'] == (
        builtin_drill_manifest().manifest_sha256)


def test_cli_drill_run_requires_arm(tmp_path, capsys) -> None:
    code, env = _run(
        capsys, 'drill', 'run', '--json',
        '--data-dir', tmp_path / 'd')
    assert code == 4
    assert env['outcome'] == 'unauthorized'
    assert env['verdict'] == 'arm_required'


def test_cli_drill_run_dry_run_never_injects(tmp_path, capsys) -> None:
    work = tmp_path / 'drills-work'
    code, env = _run(
        capsys, 'drill', 'run', '--dry-run', '--work-root', work,
        '--json', '--data-dir', tmp_path / 'd')
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['verdict'] == 'planned'
    assert len(env['data']['planned']) == 9
    assert not work.exists() or not any(work.iterdir())


def test_cli_drill_run_unknown_id_is_missing_evidence(
        tmp_path, capsys) -> None:
    code, env = _run(
        capsys, 'drill', 'run', '--drill-id', 'nope', '--arm',
        '--json', '--data-dir', tmp_path / 'd')
    assert code == 5
    assert env['outcome'] == 'missing_evidence'
    assert env['verdict'] == 'unknown_drill'


def test_cli_drill_run_armed_seals_and_reports(
        tmp_path, capsys) -> None:
    data_dir = tmp_path / 'd'
    report_path = tmp_path / 'report.json'
    code, env = _run(
        capsys, 'drill', 'run',
        '--drill-id', 'vault-locked-denies-material',
        '--drill-id', 'vault-unavailable-fails-closed',
        '--arm', '--work-root', tmp_path / 'w',
        '--out', report_path,
        '--document-id', 'doc-cli', '--json',
        '--data-dir', data_dir)
    assert code == 0, env
    assert env['outcome'] == 'succeeded'
    assert env['verdict'] == 'all_recovered'
    assert env['run_record_id'] and env['run_sha256']
    assert {r['drill_id'] for r in env['data']['results']} == {
        'vault-locked-denies-material',
        'vault-unavailable-fails-closed'}
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['format'] == RECOVERY_DRILL_REPORT_FORMAT
    payload = dict(report)
    payload['report_sha256'] = ''
    assert canonical_sha256(payload) == report['report_sha256']

    code2, env2 = _run(
        capsys, 'records', 'list', '--kind', 'headless_run',
        '--verb', 'drill.run', '--json', '--data-dir', data_dir)
    assert code2 == 0
    assert env2['data']['records'][0]['id'] == env['run_record_id']
