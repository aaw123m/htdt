"""REV59-CADREF regression tests — #777 video cadence delivery,
#778 BS.1116/EBU reference listening room profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_cadence_reference import (
    CadenceDeliveryEvidence,
    ReferenceRoomProfile,
    evaluate_cadence_claim,
    evaluate_reference_claim,
)
from htdt.cad_cadref_repository import (
    CadCadRefRepository,
    CadRefAuthorityIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadCadRefRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadCadRefRepository(SceneRepository(db))


def _cax(**kw) -> CadenceDeliveryEvidence:
    payload = dict(
        document_id='doc-1',
        content_cadence_kind='film_24',
        refresh_relationship='integer_multiple',
        timing_evidence_ref=_ref('timing-1'),
    )
    payload.update(kw)
    return CadenceDeliveryEvidence.create(payload)


def _rrx(**kw) -> ReferenceRoomProfile:
    payload = dict(
        document_id='doc-1',
        framework='itu_bs1116',
        room_criteria_ref=_ref('room-1'),
        loudspeaker_criteria_ref=_ref('spk-1'),
        listener_position_ref=_ref('pos-1'),
    )
    payload.update(kw)
    return ReferenceRoomProfile.create(payload)


def test_cadence_requires_kinds():
    with pytest.raises(ValueError):
        _cax(content_cadence_kind='unknown')
    with pytest.raises(ValueError):
        _cax(refresh_relationship='unknown')


def test_reference_requires_framework():
    with pytest.raises(ValueError):
        _rrx(framework='unknown')


def test_cadence_ladder():
    assert evaluate_cadence_claim(
        None, mode_negotiated=True)[0] == 'negotiated_is_not_motion'
    assert evaluate_cadence_claim(None)[0] == 'cadence_unqualified'
    c_mp = _cax(motion_processing_declared=True)
    assert evaluate_cadence_claim(c_mp)[0] == 'processing_active'
    c_conv = _cax(refresh_relationship='fixed_conversion')
    assert evaluate_cadence_claim(c_conv)[0] == 'conversion_declared'
    c_drop = _cax(repeat_drop_observed=True)
    assert evaluate_cadence_claim(c_drop)[0] == 'cadence_unqualified'
    c_no = _cax(timing_evidence_ref=None)
    assert evaluate_cadence_claim(c_no)[0] == 'cadence_unqualified'
    assert evaluate_cadence_claim(_cax())[0] == 'qualified_cadence'


def test_reference_ladder():
    assert evaluate_reference_claim(None)[0] == 'profile_unqualified'
    p = _rrx()
    assert evaluate_reference_claim(
        p, cited_as_design_target=True)[0] == (
        'reference_is_not_design_target')
    p2 = _rrx(claimed_as_design_target=True)
    assert evaluate_reference_claim(p2)[0] == (
        'reference_is_not_design_target')
    p3 = _rrx(listener_position_ref=None)
    assert evaluate_reference_claim(p3)[0] == 'profile_unqualified'
    assert evaluate_reference_claim(p)[0] == 'qualified_reference'


def test_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    c, p = _cax(), _rrx()
    repo.save_cadence_evidence(c)
    repo.save_reference_profile(p)
    assert repo.get_cadence_evidence(c.evidence_id) == c
    assert repo.get_reference_profile(p.profile_id) == p


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    p = _rrx()
    repo.save_reference_profile(p)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_reference_room_profiles SET '
            'framework=? WHERE profile_id=?',
            ('ebu_tech3276', p.profile_id),
        )
    with pytest.raises(CadRefAuthorityIntegrityError):
        repo.get_reference_profile(p.profile_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadCadRefRepository(SceneRepository(db))
    c = _cax()
    repo.save_cadence_evidence(c)
    assert repo.get_cadence_evidence(c.evidence_id) == c
