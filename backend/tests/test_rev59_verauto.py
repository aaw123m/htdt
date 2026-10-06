"""REV59-VERAUTO regression tests — device-verification
requirement/closure automation."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_verification_automation import (
    RequiredCell,
    VerificationClosure,
    VerificationRequirement,
    evaluate_verification_claim,
)
from htdt.cad_verification_repository import (
    CadVerAutoRepository,
    VerAutoAuthorityIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadVerAutoRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadVerAutoRepository(SceneRepository(db))


def _cell(**kw) -> RequiredCell:
    payload = dict(
        channel_role='subwoofer_1',
        source_speaker_ids=('sub1',),
        target_entity_id='seat_a',
        repeats=1,
    )
    payload.update(kw)
    return RequiredCell(**payload)


def _vrq(**kw) -> VerificationRequirement:
    payload = dict(
        document_id='doc-1',
        issue_ref='#652',
        gate_label='panning continuity',
        required_cells=(_cell(),),
        evaluator_kind='authority_evaluator',
        evaluator_ref=_ref('eval-1'),
    )
    payload.update(kw)
    return VerificationRequirement.create(payload)


def _vcl(**kw) -> VerificationClosure:
    payload = dict(
        document_id='doc-1',
        requirement_ref=_ref('req-1'),
        runner_plan_ref=_ref('plan-1'),
        committed_cell_count=1,
        bound_evidence_refs=(_ref('ev-1'),),
        evaluator_verdict='passed',
        closed=True,
    )
    payload.update(kw)
    return VerificationClosure.create(payload)


def test_cell_requires_parts():
    with pytest.raises(ValueError):
        _cell(channel_role='')
    with pytest.raises(ValueError):
        _cell(source_speaker_ids=())
    with pytest.raises(ValueError):
        _cell(repeats=0)


def test_requirement_requires_cells_and_evaluator():
    with pytest.raises(ValueError):
        _vrq(issue_ref='')
    with pytest.raises(ValueError):
        _vrq(required_cells=())
    with pytest.raises(ValueError):
        _vrq(evaluator_kind='unknown')
    with pytest.raises(ValueError):
        _vrq(evaluator_ref=None)


def test_closure_requires_pass_to_close():
    with pytest.raises(ValueError):
        _vcl(requirement_ref=None)
    with pytest.raises(ValueError, match='pass'):
        _vcl(evaluator_verdict='inconclusive', closed=True)
    ok = _vcl(evaluator_verdict='failed', closed=False)
    assert ok.closed is False


def test_verification_ladder():
    assert evaluate_verification_claim(None, None)[0] == (
        'plan_underived')
    req = _vrq()
    assert evaluate_verification_claim(req, None)[0] == (
        'cells_incomplete')
    c_noplan = _vcl(runner_plan_ref=None, closed=False,
                  evaluator_verdict='inconclusive')
    assert evaluate_verification_claim(req, c_noplan)[0] == (
        'plan_underived')
    c_short = _vcl(committed_cell_count=0, closed=False,
                   evaluator_verdict='inconclusive')
    assert evaluate_verification_claim(req, c_short)[0] == (
        'cells_incomplete')
    c_noev = _vcl(bound_evidence_refs=(), closed=False,
                  evaluator_verdict='inconclusive')
    assert evaluate_verification_claim(req, c_noev)[0] == (
        'evidence_unbound')
    c_fail = _vcl(evaluator_verdict='failed', closed=False)
    assert evaluate_verification_claim(req, c_fail)[0] == (
        'verdict_not_passed')
    assert evaluate_verification_claim(req, _vcl())[0] == 'closable'


def test_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    r, c = _vrq(), _vcl()
    repo.save_verification_requirement(r)
    repo.save_verification_closure(c)
    assert repo.get_verification_requirement(
        r.requirement_id) == r
    assert repo.get_verification_closure(c.closure_id) == c


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    c = _vcl()
    repo.save_verification_closure(c)
    import sqlite3
    with sqlite3.connect(repo.path) as conn:
        conn.execute(
            'UPDATE cad_verification_closures SET '
            'evaluator_verdict=? WHERE closure_id=?',
            ('failed', c.closure_id),
        )
    with pytest.raises(VerAutoAuthorityIntegrityError):
        repo.get_verification_closure(c.closure_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadVerAutoRepository(SceneRepository(db))
    r = _vrq()
    repo.save_verification_requirement(r)
    assert repo.get_verification_requirement(
        r.requirement_id) == r
