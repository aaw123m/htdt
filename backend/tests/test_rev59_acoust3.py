"""REV59-ACOUST3 regression tests — #694 finite absorber edge/size,
#646 precedence/echo perceptual risk, #648 fire/finish evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_finite_absorber import (
    FiniteAbsorberGeometry,
    FiniteTreatmentBoundaryModel,
    evaluate_finite_absorber_claim,
)
from htdt.cad_fire_evidence import (
    FinishAssemblySafetyEvidence,
    ReactionToFireEvidence,
    evaluate_fire_eligibility_claim,
)
from htdt.cad_precedence_echo import (
    EchoRiskObservation,
    PrecedenceProfile,
    evaluate_echo_risk_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.cad_treatment_safety_repository import (
    CadTreatmentSafetyRepository,
    TreatmentSafetyConflictError,
    TreatmentSafetyIntegrityError,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadTreatmentSafetyRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadTreatmentSafetyRepository(SceneRepository(db))


def _geom(**kw) -> FiniteAbsorberGeometry:
    payload = dict(
        document_id='doc-1',
        width_m=1.2,
        height_m=0.6,
        edge_state='exposed',
        mounting_kind='wall_patch',
    )
    payload.update(kw)
    return FiniteAbsorberGeometry.create(payload)


def _model(**kw) -> FiniteTreatmentBoundaryModel:
    payload = dict(
        document_id='doc-1',
        geometry_ref=_ref('g-1'),
        reaction_kind='locally_reacting',
    )
    payload.update(kw)
    return FiniteTreatmentBoundaryModel.create(payload)


def _profile(**kw) -> PrecedenceProfile:
    payload = dict(document_id='doc-1', stimulus_kind='speech')
    payload.update(kw)
    return PrecedenceProfile.create(payload)


def _obs(**kw) -> EchoRiskObservation:
    payload = dict(
        document_id='doc-1',
        profile_ref=_ref('p-1'),
        delay_ms=32.0,
        relative_level_db=-3.0,
        assessed_phenomenon='echo_segregation',
        risk_verdict='echo_risk',
    )
    payload.update(kw)
    return EchoRiskObservation.create(payload)


def _fire(**kw) -> ReactionToFireEvidence:
    payload = dict(
        document_id='doc-1',
        test_standard='astm_e84',
        tested_assembly_descriptor='fabric-wrapped 50mm panel on 25mm standoff',
        result_descriptor='FSI 20 / SDI 350',
        report_ref=_ref('rep-1'),
    )
    payload.update(kw)
    return ReactionToFireEvidence.create(payload)


def _assembly(**kw) -> FinishAssemblySafetyEvidence:
    payload = dict(
        document_id='doc-1',
        assembly_parts=('absorber', 'fabric_facing'),
        evidence_refs=(_ref('e-1'),),
        installation_context='wall_finish',
    )
    payload.update(kw)
    return FinishAssemblySafetyEvidence.create(payload)


# -- model validation -------------------------------------------------

def test_geometry_requires_positive_extents():
    with pytest.raises(ValueError, match='positive extent'):
        _geom(width_m=0.0)
    with pytest.raises(ValueError, match='positive extent'):
        _geom(height_m=-1.0)


def test_boundary_model_requires_geometry_ref():
    with pytest.raises(ValueError):
        _model(geometry_ref=None)


def test_infinite_plane_equivalence_needs_edge_evidence():
    with pytest.raises(ValueError, match='edge-effect'):
        _model(
            claims_infinite_plane_equivalence=True,
            edge_effect_model_ref=None,
        )
    ok = _model(
        claims_infinite_plane_equivalence=True,
        edge_effect_model_ref=_ref('edge-1'),
    )
    assert ok.claims_infinite_plane_equivalence


def test_profile_rejects_unknown_stimulus():
    with pytest.raises(ValueError, match='stimulus'):
        _profile(stimulus_kind='unknown')


def test_observation_requires_profile_and_phenomenon():
    with pytest.raises(ValueError):
        _obs(profile_ref=None)
    with pytest.raises(ValueError, match='phenomenon'):
        _obs(assessed_phenomenon='unknown')


def test_fire_evidence_requires_standard_assembly_report():
    with pytest.raises(ValueError, match='test standard'):
        _fire(test_standard='unknown')
    with pytest.raises(ValueError, match='tested assembly'):
        _fire(tested_assembly_descriptor='')
    with pytest.raises(ValueError):
        _fire(report_ref=None)


def test_assembly_requires_parts_and_evidence():
    with pytest.raises(ValueError, match='parts'):
        _assembly(assembly_parts=())
    with pytest.raises(ValueError, match='evidence'):
        _assembly(evidence_refs=())
    with pytest.raises(ValueError, match='unknown assembly parts'):
        _assembly(assembly_parts=('steel_beam',))


# -- evaluators --------------------------------------------------------

def test_finite_claim_ladder():
    g, m = _geom(), _model()
    assert evaluate_finite_absorber_claim(None, m)[0] == (
        'insufficient_geometry'
    )
    assert evaluate_finite_absorber_claim(g, None)[0] == (
        'infinite_plane_misapplied'
    )
    m_bad = _model(claims_infinite_plane_equivalence=True,
                   edge_effect_model_ref=_ref('x'))
    assert evaluate_finite_absorber_claim(
        g, m_bad, measurement_sample_is_finite=False
    )[0] == 'sample_size_unaccounted'
    g_unknown = _geom(edge_state='unknown')
    assert evaluate_finite_absorber_claim(g_unknown, m)[0] == (
        'edge_effect_unqualified'
    )
    assert evaluate_finite_absorber_claim(g, m)[0] == (
        'finite_model_declared'
    )


def test_echo_claim_ladder():
    p, o = _profile(), _obs()
    assert evaluate_echo_risk_claim(32.0, -3.0, p, o,
                                    used_fixed_rule=True)[0] == (
        'fixed_rule_misapplied'
    )
    assert evaluate_echo_risk_claim(32.0, -3.0, None, o)[0] == (
        'stimulus_unqualified'
    )
    assert evaluate_echo_risk_claim(32.0, -3.0, p, None)[0] == (
        'unresolved_risk'
    )
    o_inc = _obs(risk_verdict='inconclusive')
    assert evaluate_echo_risk_claim(32.0, -3.0, p, o_inc)[0] == (
        'unresolved_risk'
    )
    assert evaluate_echo_risk_claim(32.0, -3.0, p, o)[0] == (
        'evidence_declared'
    )


def test_fire_claim_ladder():
    e, a = _fire(), _assembly()
    assert evaluate_fire_eligibility_claim(
        None, a, acoustic_qualified=True
    )[0] == 'acoustic_not_safety'
    assert evaluate_fire_eligibility_claim(None, a)[0] == (
        'unclaimed_fire_evidence'
    )
    assert evaluate_fire_eligibility_claim(
        e, a, required_standard='nfpa_286'
    )[0] == 'test_scope_mismatch'
    assert evaluate_fire_eligibility_claim(e, None)[0] == (
        'lab_result_is_not_installation'
    )
    assert evaluate_fire_eligibility_claim(e, a)[0] == (
        'eligible_assembly'
    )


# -- repository --------------------------------------------------------

def test_roundtrip_all_six(tmp_path):
    repo = _repo(tmp_path)
    g, m, p, o, e, a = (
        _geom(), _model(), _profile(), _obs(), _fire(), _assembly()
    )
    repo.save_finite_geometry(g)
    repo.save_boundary_model(m)
    repo.save_precedence_profile(p)
    repo.save_echo_observation(o)
    repo.save_fire_evidence(e)
    repo.save_assembly_evidence(a)
    assert repo.get_finite_geometry(g.geometry_id) == g
    assert repo.get_boundary_model(m.model_id) == m
    assert repo.get_precedence_profile(p.profile_id) == p
    assert repo.get_echo_observation(o.observation_id) == o
    assert repo.get_fire_evidence(e.evidence_id) == e
    assert repo.get_assembly_evidence(a.assembly_id) == a


def test_append_only_conflict(tmp_path):
    repo = _repo(tmp_path)
    g = _geom()
    repo.save_finite_geometry(g)
    repo.save_finite_geometry(g)  # idempotent
    g2 = FiniteAbsorberGeometry.create(dict(
        document_id='doc-1', width_m=1.2, height_m=0.6,
        edge_state='sealed', mounting_kind='wall_patch',
    ))
    forged = FiniteAbsorberGeometry.model_construct(
        **dict(g2.model_dump(), geometry_id=g.geometry_id)
    )
    with pytest.raises(TreatmentSafetyIntegrityError):
        repo.save_finite_geometry(forged)


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    p = _profile()
    repo.save_precedence_profile(p)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_precedence_profiles SET stimulus_kind=? '
            'WHERE profile_id=?', ('music', p.profile_id)
        )
    with pytest.raises(TreatmentSafetyIntegrityError):
        repo.get_precedence_profile(p.profile_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == 62
    repo = CadTreatmentSafetyRepository(SceneRepository(db))
    g = _geom()
    repo.save_finite_geometry(g)
    assert repo.get_finite_geometry(g.geometry_id) == g
