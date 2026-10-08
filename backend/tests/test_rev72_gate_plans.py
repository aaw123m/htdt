"""#1030 — lifecycle-gate operator plans + sealed acceptance runs.

Covers: manifest → minimal-step plan derivation (every physical/manual
gate, deterministic seal), honest verdicts (no attestation → blocked,
never an implied pass), evidence sha-pinning, sealed-store round-trip,
export/import re-verification, and the ``htdt gates`` CLI exit lattice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.cad_lifecycle_gates import (
    DEFAULT_DOCUMENT_ID,
    GATE_RUN_EXPORT_FORMAT,
    CadGateAcceptanceRun,
    GateEvidenceFile,
    GateOperatorPlan,
    LifecycleGate,
    build_gate_operator_plan,
    build_gate_plans,
    derive_run_verdict,
    export_run_envelope,
    load_lifecycle_gates,
    parse_run_envelope,
    resolve_evidence_ref,
    run_gate_plan,
)
from htdt.cad_lifecycle_gates_repository import (
    CadLifecycleGateRepository,
)
from htdt.project_library_repository import SceneRepository
from htdt.headless_cli import _REPO_ROOT, main as cli_main


NOW = '2026-10-08T00:00:00Z'
LATER = '2026-10-08T00:01:00Z'

_MANIFEST = _REPO_ROOT / 'scripts' / 'issue_lifecycle_manifest.yaml'


def _gate(**overrides) -> LifecycleGate:
    params = {
        'issue': 4242,
        'lifecycle': 'physical_evidence_remaining',
        'gate_index': 0,
        'gate_kind': 'physical',
        'description': '実機でサブウーファーを鳴らして検証する',
        'evidence_refs': (),
        'landed_prs': (),
    }
    params.update(overrides)
    return LifecycleGate(**params)


def _plan(**overrides) -> GateOperatorPlan:
    return build_gate_operator_plan(
        _gate(**overrides),
        manifest_sha256='a' * 64,
        manifest_path='scripts/issue_lifecycle_manifest.yaml',
        document_id=DEFAULT_DOCUMENT_ID,
    )


def _repo(tmp_path: Path) -> CadLifecycleGateRepository:
    return CadLifecycleGateRepository(
        SceneRepository(tmp_path / 'cad-scenes.sqlite3'))


def _run(capsys, *argv):
    code = cli_main([str(a) for a in argv])
    out = capsys.readouterr().out.strip().splitlines()
    assert out, f'no stdout for {argv}'
    return code, json.loads(out[-1])


def test_plans_cover_every_operator_gate_in_real_manifest() -> None:
    plans = build_gate_plans(_MANIFEST)
    sha, gates = load_lifecycle_gates(_MANIFEST)
    assert plans
    assert len(plans) == len(gates)
    for plan in plans:
        auto = [s for s in plan.steps if s.kind == 'auto']
        operator = [s for s in plan.steps if s.kind == 'operator']
        assert len(auto) == 3
        assert len(operator) == 1
        assert plan.operator_step_count == 1
        assert operator[0].instruction_ja
        # deterministic seal — re-planning must reproduce the id
        again = build_gate_plans(
            _MANIFEST, issues=(plan.issue,))[0]
        if again.issue == plan.issue and (
                again.gate_index == plan.gate_index):
            assert again.plan_id == plan.plan_id


def test_plan_is_pure_derivation_and_filtered() -> None:
    plans = build_gate_plans(_MANIFEST, issues=(801,))
    assert all(p.issue == 801 for p in plans)
    manual = build_gate_plans(_MANIFEST, kinds=('manual',))
    physical = build_gate_plans(_MANIFEST, kinds=('physical',))
    assert all(p.gate_kind == 'manual' for p in manual)
    assert all(p.gate_kind == 'physical' for p in physical)
    both = build_gate_plans(_MANIFEST)
    assert len(manual) + len(physical) == len(both)


def test_run_without_attestation_never_passes() -> None:
    run = run_gate_plan(
        _plan(),
        environment={'platform': 'test'},
        repo_root=_REPO_ROOT,
        started_at_utc=NOW,
        finished_at_utc=LATER,
    )
    assert run.verdict == 'awaiting_attestation'
    operator = [s for s in run.steps if s.kind == 'operator'][0]
    assert operator.status == 'pending'
    assert operator.verdict_source == 'none'
    assert run.non_claims


def test_run_with_attestation_and_evidence_satisfies() -> None:
    run = run_gate_plan(
        _plan(),
        environment={'platform': 'test'},
        repo_root=_REPO_ROOT,
        operator_attestation='実機で確認した',
        operator_id='maintainer-1',
        evidence=[GateEvidenceFile(
            filename='clip.wav', sha256='b' * 64, size_bytes=4)],
        started_at_utc=NOW,
        finished_at_utc=LATER,
    )
    assert run.verdict == 'gate_satisfied'
    assert run.operator_attestation == '実機で確認した'
    # seal re-derives from payload
    CadGateAcceptanceRun.model_validate(run.model_dump(mode='json'))


def test_unresolvable_evidence_ref_fails_the_run() -> None:
    plan = _plan(evidence_refs=('missing/does-not-exist.wav',))
    run = run_gate_plan(
        plan,
        environment={},
        repo_root=_REPO_ROOT,
        operator_attestation='done',
        started_at_utc=NOW,
        finished_at_utc=LATER,
    )
    assert run.verdict == 'steps_failed'
    step = [s for s in run.steps if s.auto_action
            == 'evidence_ref_integrity'][0]
    assert step.status == 'failed'
    # absolute/escaped refs can never resolve
    for bad in ('C:/x/ev.wav', '../outside.wav', '/abs.wav', '\\\\srv\\x'):
        plan = _plan(evidence_refs=(bad,))
        run = run_gate_plan(
            plan, environment={}, repo_root=_REPO_ROOT,
            operator_attestation='done',
            started_at_utc=NOW, finished_at_utc=LATER)
        assert run.verdict == 'steps_failed'


def test_verdict_derivation_is_fail_closed() -> None:
    assert derive_run_verdict(()) == 'awaiting_attestation'


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan(issue=7777)
    repo.save_plan(plan)
    assert repo.get_plan(plan.plan_id) == plan
    assert repo.list_plans(plan.document_id)[0].plan_id == plan.plan_id
    run = run_gate_plan(
        plan, environment={}, repo_root=_REPO_ROOT,
        operator_attestation='実施した',
        started_at_utc=NOW, finished_at_utc=LATER)
    repo.save_run(run)
    assert repo.get_run(run.run_id) == run
    listed = repo.list_runs(
        plan.document_id, issue=7777, gate_index=0)
    assert [r.run_id for r in listed] == [run.run_id]


def test_export_import_envelope_re_verifies() -> None:
    run = run_gate_plan(
        _plan(), environment={}, repo_root=_REPO_ROOT,
        operator_attestation='実施した',
        started_at_utc=NOW, finished_at_utc=LATER)
    envelope = export_run_envelope(run, exported_at_utc=LATER)
    assert envelope['format'] == GATE_RUN_EXPORT_FORMAT
    assert parse_run_envelope(envelope) == run
    # tampered payloads and foreign formats are rejected
    tampered = json.loads(json.dumps(envelope))
    tampered['record']['verdict'] = 'gate_satisfied'
    tampered['record']['operator_attestation'] = 'forged'
    with pytest.raises(Exception):
        parse_run_envelope(tampered)
    with pytest.raises(ValueError):
        parse_run_envelope({'format': 'other', 'record': {}})
    with pytest.raises(ValueError):
        parse_run_envelope({'format': GATE_RUN_EXPORT_FORMAT})
    with pytest.raises(ValueError):
        parse_run_envelope('not-a-dict')


def test_cli_gates_plan_run_import(capsys, tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    out_path = tmp_path / 'plans.json'
    # pick a gate whose declared evidence refs actually resolve on this
    # checkout so the attested run can reach gate_satisfied
    good = None
    for plan in build_gate_plans(_MANIFEST):
        if all(resolve_evidence_ref(r, _REPO_ROOT)[0]
               for r in plan.evidence_refs):
            good = plan
            break
    assert good is not None
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'issues': [good.issue]}),
                    encoding='utf-8')
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'plan', '--spec', spec, '--out', out_path)
    assert code == 0, env
    assert env['outcome'] == 'succeeded'
    plans = env['data']['plans']
    assert plans
    plan_id = good.plan_id
    assert any(p['plan_id'] == plan_id for p in plans)
    # a run without attestation blocks — never an implied pass
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'run', '--plan-id', plan_id)
    assert code == 3, env
    assert env['data']['run']['verdict'] == 'awaiting_attestation'
    # attested run satisfies the gate and exports
    run_out = tmp_path / 'run-export.json'
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'run', '--plan-id', plan_id,
        '--attest', '実機で確認した', '--operator', 'op-1',
        '--out', run_out)
    assert code == 0, env
    assert env['data']['run']['verdict'] == 'gate_satisfied'
    run_id = env['data']['run']['run_id']
    assert run_out.is_file()
    # import into a second data dir — imported once, idempotent twice
    other_dir = tmp_path / 'data2'
    code, env = _run(
        capsys, '--data-dir', other_dir, '--json',
        'gates', 'import', '--run-file', run_out)
    assert code == 0, env
    assert env['verdict'] == 'imported'
    code, env = _run(
        capsys, '--data-dir', other_dir, '--json',
        'gates', 'import', '--run-file', run_out)
    assert code == 0, env
    assert env['verdict'] == 'already_imported'
    # tampered exports never import
    bad = json.loads(run_out.read_text(encoding='utf-8'))
    bad['record']['operator_attestation'] = 'forged'
    bad_path = tmp_path / 'bad.json'
    bad_path.write_text(json.dumps(bad), encoding='utf-8')
    code, env = _run(
        capsys, '--data-dir', other_dir, '--json',
        'gates', 'import', '--run-file', bad_path)
    assert code == 5, env
    # records surface both tables
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'records', 'list', '--kind', 'gate_plan')
    assert code == 0
    assert any(r['id'] == plan_id for r in env['data']['records'])
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'records', 'list', '--kind', 'gate_acceptance_run')
    assert any(r['id'] == run_id for r in env['data']['records'])
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'records', 'export', '--kind', 'gate_acceptance_run',
        '--id', run_id)
    assert code == 0
    assert env['data']['payload']['run_id'] == run_id


def test_cli_plan_dry_run_does_not_persist(capsys, tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json', '--dry-run',
        'gates', 'plan')
    assert code == 0, env
    assert env['data']['persisted'] is False
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'records', 'list', '--kind', 'gate_plan')
    assert code == 0
    assert env['data']['records'] == []


def test_cli_plan_spec_filters(capsys, tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps(
        {'issues': [801], 'kinds': ['physical']}), encoding='utf-8')
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'plan', '--spec', spec)
    assert code == 0, env
    plans = env['data']['plans']
    assert all(p['issue'] == 801 for p in plans)
    assert all(p['gate_kind'] == 'physical' for p in plans)


def test_cli_run_from_plan_bundle_with_gate_selector(
        capsys, tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    bundle = tmp_path / 'bundle.json'
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'plan', '--out', bundle)
    assert code == 0
    first = env['data']['plans'][0]
    selector = f"{first['issue']}:{first['gate_index']}"
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'run', '--plan', bundle, '--gate', selector)
    assert code == 3, env
    assert env['data']['run']['run_id']


def test_cli_gates_list(capsys, tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json', 'gates', 'plan')
    assert code == 0
    issue = env['data']['plans'][0]['issue']
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'gates', 'list', '--issue', issue)
    assert code == 0
    assert all(p['issue'] == issue for p in env['data']['plans'])
