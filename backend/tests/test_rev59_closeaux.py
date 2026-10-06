"""REV59-CLOSEAUX: manifest-to-verification bridge tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_manifest_gate_repository import (
    CadManifestGateRepository,
    ManifestGateIntegrityError,
)
from htdt.cad_manifest_verification import (
    GateRunResult,
    ManifestGate,
    RequiredCell,
    derive_verification_requirement,
    evaluate_gate,
    evaluate_issue_verdict,
    load_manifest_gates,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import NATIVE_SCHEMA_VERSION, ensure_native_schema


DOC = 'doc-manifest-gate'


def _repo(tmp_path: Path) -> CadManifestGateRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadManifestGateRepository(SceneRepository(db))


def _ref(rid: str = 'x' * 64) -> AuthorityRef:
    return AuthorityRef(kind='evidence', ref_id=rid, ref_sha256='a' * 64)


def _gate(tmp_path: Path, kind: str = 'pytest', **kw) -> ManifestGate:
    payload = {
        'document_id': DOC,
        'issue_ref': 'issue-99',
        'check_id': 'chk-1',
        'check_kind': kind,
        'manifest_sha256': 'b' * 64,
        'description': 'desc',
    }
    if kind == 'pytest':
        payload['tests'] = ('backend/tests/test_x.py',)
    if kind == 'script':
        payload['argv'] = ('script.py',)
    payload.update(kw)
    return ManifestGate.create(payload)


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / 'manifest.yaml'
    p.write_text(
        'version: 1\nissues:\n'
        '  - issue: 42\n    checks:\n'
        '      - id: auto-1\n        kind: pytest\n'
        '        tests: [backend/tests/test_a.py]\n'
        '        description: auto check\n'
        '      - id: phys-1\n        kind: manual\n'
        '        description: 実機測定\n',
        encoding='utf-8',
    )
    return p


def test_gate_requires_test_targets_for_pytest(tmp_path):
    with pytest.raises(ValueError):
        _gate(tmp_path, 'pytest', tests=())


def test_gate_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        _gate(tmp_path, 'bogus')


def test_cells_only_on_manual_gates(tmp_path):
    cell = RequiredCell(
        channel_role='sub', source_speaker_ids=('s1',),
        target_entity_id='room', repeats=2,
    )
    with pytest.raises(ValueError):
        _gate(tmp_path, 'pytest', cells=(cell,))
    g = _gate(tmp_path, 'manual', cells=(cell,))
    assert g.cells == (cell,)


def test_manual_gate_derives_requirement(tmp_path):
    cell = RequiredCell(
        channel_role='sub', source_speaker_ids=('s1',),
        target_entity_id='room',
    )
    g = _gate(tmp_path, 'manual', cells=(cell,))
    req = derive_verification_requirement(g)
    assert req is not None and req.issue_ref == 'issue-99'
    assert req.gate_label == 'chk-1'
    assert len(req.required_cells) == 1


def test_auto_gate_does_not_derive_requirement(tmp_path):
    g = _gate(tmp_path, 'pytest')
    assert derive_verification_requirement(g) is None


def test_load_manifest_gates_pins_file_sha(tmp_path):
    p = _manifest(tmp_path)
    gates = load_manifest_gates(DOC, p)
    assert len(gates) == 2
    kinds = {g.check_id: g.check_kind for g in gates}
    assert kinds == {'auto-1': 'pytest', 'phys-1': 'manual'}
    assert all(len(g.manifest_sha256) == 64 for g in gates)


def test_load_manifest_rejects_bad_kind(tmp_path):
    p = tmp_path / 'bad.yaml'
    p.write_text(
        'issues:\n  - issue: 1\n    checks:\n'
        '      - id: x\n        kind: telepathy\n'
        '        description: d\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError):
        load_manifest_gates(DOC, p)


def test_manual_gate_only_satisfied_by_committed_evidence(tmp_path):
    g = _gate(tmp_path, 'manual')
    passed = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(g.gate_id),
        'outcome': 'passed',
    })
    v, _ = evaluate_gate(g, passed)
    assert v == 'unsatisfied'
    committed = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(g.gate_id),
        'outcome': 'evidence_committed', 'evidence_ref': _ref('e' * 64),
    })
    v, _ = evaluate_gate(g, committed)
    assert v == 'satisfied'


def test_evidence_committed_requires_ref(tmp_path):
    with pytest.raises(ValueError):
        GateRunResult.create({
            'document_id': DOC, 'gate_ref': _ref(),
            'outcome': 'evidence_committed',
        })


def test_issue_verdict_ladder(tmp_path):
    auto = _gate(tmp_path, 'pytest', check_id='a1')
    manual = _gate(tmp_path, 'manual', check_id='m1')
    # manual_required: no auto gates
    v, _ = evaluate_issue_verdict((manual,), {})
    assert v == 'manual_required'
    # unevaluated: gates exist but nothing has run yet
    v, _ = evaluate_issue_verdict((auto, manual), {})
    assert v == 'unevaluated'
    # failing: auto failed
    bad = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(auto.gate_id),
        'outcome': 'failed',
    })
    v, _ = evaluate_issue_verdict((auto, manual), {auto.gate_id: bad})
    assert v == 'failing'
    # partially: auto passed, manual open
    ok = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(auto.gate_id),
        'outcome': 'passed',
    })
    v, _ = evaluate_issue_verdict((auto, manual), {auto.gate_id: ok})
    assert v == 'partially_verified'
    # verified: auto passed + manual evidence committed
    ev = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(manual.gate_id),
        'outcome': 'evidence_committed', 'evidence_ref': _ref('f' * 64),
    })
    v, _ = evaluate_issue_verdict(
        (auto, manual), {auto.gate_id: ok, manual.gate_id: ev}
    )
    assert v == 'verified'


def test_store_roundtrip_and_tamper_detection(tmp_path):
    repo = _repo(tmp_path)
    g = _gate(tmp_path)
    repo.save_manifest_gate(g)
    back = repo.get_manifest_gate(g.gate_id)
    assert back.gate_sha256 == g.gate_sha256
    r = GateRunResult.create({
        'document_id': DOC, 'gate_ref': _ref(g.gate_id),
        'outcome': 'passed', 'finished_at_utc': '2026-10-06T00:00:00Z',
    })
    repo.save_gate_run_result(r)
    assert repo.get_gate_run_result(r.result_id).outcome == 'passed'
    with sqlite3.connect(repo.path) as con:
        con.execute(
            "UPDATE cad_manifest_gates SET issue_ref='issue-0' "
            'WHERE gate_id=?', (g.gate_id,))
    with pytest.raises(ManifestGateIntegrityError):
        repo.get_manifest_gate(g.gate_id)


def test_store_idempotent_and_conflict(tmp_path):
    repo = _repo(tmp_path)
    g = _gate(tmp_path)
    repo.save_manifest_gate(g)
    repo.save_manifest_gate(g)
    other = ManifestGate.create({
        'document_id': DOC, 'issue_ref': 'issue-100',
        'check_id': 'c2', 'check_kind': 'manual',
        'manifest_sha256': 'c' * 64, 'description': 'd',
    })
    repo.save_manifest_gate(other)
    # a row carrying a foreign sha under an existing id is caught
    # by the full row-vs-payload compare before any conflict check
    with sqlite3.connect(repo.path) as con:
        con.execute(
            "UPDATE cad_manifest_gates SET gate_sha256='0' || "
            'substr(gate_sha256, 3) WHERE gate_id=?', (g.gate_id,))
    with pytest.raises(ManifestGateIntegrityError):
        repo.get_manifest_gate(g.gate_id)


def test_commit_script_turns_report_into_results(tmp_path):
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location(
        'commit_manifest_verification',
        Path(__file__).resolve().parents[2]
        / 'scripts' / 'commit_manifest_verification.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    manifest = _manifest(tmp_path)
    report = {
        'issues': [{
            'issue': 42,
            'checks': [{
                'id': 'auto-1', 'kind': 'pytest',
                'status': 'passed', 'detail': '',
            }],
            'manual_required': [{'id': 'phys-1', 'description': 'x'}],
        }],
    }
    rp = tmp_path / 'report.json'
    rp.write_text(_json.dumps(report), encoding='utf-8')
    scene = tmp_path / 'scene.htdtscene'
    rc = mod.main([
        '--scene', str(scene), '--document-id', DOC,
        '--manifest', str(manifest), '--report', str(rp),
    ])
    assert rc == 0
    repo = CadManifestGateRepository(SceneRepository(scene))
    results = repo.list_gate_run_results()
    assert len(results) == 1
    assert results[0].outcome == 'passed'
    gates = repo.list_manifest_gates()
    assert len(gates) == 2


def test_fresh_migrate_creates_tables(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadManifestGateRepository(SceneRepository(db))
    g = _gate(tmp_path)
    repo.save_manifest_gate(g)
    assert repo.get_manifest_gate(g.gate_id) == g
    with sqlite3.connect(repo.path) as con:
        for table in ('cad_manifest_gates', 'cad_gate_run_results'):
            con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
