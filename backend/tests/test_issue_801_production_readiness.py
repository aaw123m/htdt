"""Issue #801 — production-readiness gate.

Covers the adoption-decision chain (no_go / limited / production_ready),
hybrid-path extra gate, stale-authority rejection, surface enablement
derivation + replay verification, seal/id integrity and repository
round-trip + tamper detection.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_production_readiness import (
    ProductionReadinessDecision,
    ProductionReadinessIntegrityError,
    RecommendationSurfaceDecision,
    SurfaceEnablement,
    evaluate_surface_enablement,
    issue_surface_decision,
    verify_surface_decision,
)
from htdt.cad_production_readiness_repository import (
    CadProductionReadinessRepository,
    ProductionReadinessConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.measurement_evidence_display import (
    production_readiness_label,
    production_readiness_line,
)


def _ref(kind: str, rid: str = 'x') -> AuthorityRef:
    return AuthorityRef(
        kind=kind, ref_id=rid, ref_sha256='a' * 64)


def _decision(
    document_id: str = 'doc-1',
    outcome: str = 'no_go',
    **kw,
) -> ProductionReadinessDecision:
    payload = dict(
        document_id=document_id,
        scene_ref=_ref('scene', 'scene-1'),
        system_variant_ref=_ref('system_variant', 'sv-1'),
        equipment_authority_refs=(_ref('equipment', 'avr-1'),),
        solver_path_kind='wave_only',
        solver_version_ref=_ref('solver', 'r130d-1.0'),
        outcome=outcome,
        outcome_rationale='owned-room campaign not yet run',
        decided_at_utc='2026-10-06T00:00:00Z',
    )
    payload.update(kw)
    return ProductionReadinessDecision.create(**payload)


def _full_chain(**extra):
    return dict(
        campaign_ref=_ref('campaign_verdict', 'crv-1'),
        solver_qualification_ref=_ref('benchmark_qualification'),
        residual_evaluation_ref=_ref('uncertainty_evaluation'),
        applicability_ref=_ref('applicability_envelope'),
        **extra,
    )


def _repo(tmp_path):
    db = tmp_path / 'scene.sqlite3'
    ensure_native_schema(db)
    return CadProductionReadinessRepository(SceneRepository(db))


# ------------------------------------------------------- decision model


def test_decision_requires_equipment_authority() -> None:
    with pytest.raises(ValueError):
        _decision(equipment_authority_refs=())


def test_production_ready_requires_full_chain() -> None:
    for missing in ('campaign_ref', 'solver_qualification_ref',
                    'residual_evaluation_ref', 'applicability_ref'):
        chain = _full_chain()
        chain[missing] = None
        with pytest.raises(ValueError):
            _decision(outcome='production_ready', **chain)
    ok = _decision(outcome='production_ready', **_full_chain())
    assert ok.outcome == 'production_ready'


def test_limited_requires_campaign_and_qualification() -> None:
    with pytest.raises(ValueError):
        _decision(outcome='limited')
    with pytest.raises(ValueError):
        _decision(outcome='limited', campaign_ref=_ref('cv'))
    ok = _decision(
        outcome='limited',
        campaign_ref=_ref('cv'),
        solver_qualification_ref=_ref('bq'))
    assert ok.outcome == 'limited'


def test_hybrid_path_requires_hybrid_validation() -> None:
    with pytest.raises(ValueError):
        _decision(solver_path_kind='hybrid')
    with pytest.raises(ValueError):
        _decision(hybrid_validation_ref=_ref('hybrid'))
    ok = _decision(
        solver_path_kind='hybrid',
        hybrid_validation_ref=_ref('hybrid'))
    assert ok.solver_path_kind == 'hybrid'


def test_stale_authority_rejects_production_ready() -> None:
    with pytest.raises(ValueError):
        _decision(outcome='production_ready',
                  stale_authority_refs=(_ref('old'),), **_full_chain())


def test_no_go_needs_no_gates_but_needs_rationale() -> None:
    with pytest.raises(ValueError):
        _decision(outcome_rationale='')
    ok = _decision()
    assert ok.outcome == 'no_go'


def test_refs_must_be_pinned() -> None:
    with pytest.raises(ValueError):
        _decision(scene_ref=AuthorityRef(
            kind='scene', ref_id='s', ref_sha256=None))


# ------------------------------------------------------- surfaces


def test_surface_enablement_by_outcome() -> None:
    ready = _decision(outcome='production_ready', **_full_chain())
    limited = _decision(
        outcome='limited', campaign_ref=_ref('cv'),
        solver_qualification_ref=_ref('bq'))
    nogo = _decision()
    assert evaluate_surface_enablement(ready) == {
        'inspect_prediction': 'enabled',
        'compare_candidates': 'enabled',
        'automatic_recommendation': 'enabled'}
    lim = evaluate_surface_enablement(limited)
    assert lim['automatic_recommendation'] == 'disabled'
    assert lim['compare_candidates'] == 'limited'
    assert evaluate_surface_enablement(nogo) == {
        'inspect_prediction': 'disabled',
        'compare_candidates': 'disabled',
        'automatic_recommendation': 'disabled'}


def test_surface_decision_auto_enable_only_when_ready() -> None:
    limited = _decision(
        outcome='limited', campaign_ref=_ref('cv'),
        solver_qualification_ref=_ref('bq'))
    with pytest.raises(ValueError):
        RecommendationSurfaceDecision.create(
            document_id='doc-1',
            decision_ref=AuthorityRef(
                kind='production_readiness_decision',
                ref_id=limited.decision_id,
                ref_sha256=limited.decision_sha256),
            outcome_at_issue='limited',
            surfaces=(
                SurfaceEnablement(surface='inspect_prediction',
                                  state='enabled', reason='r'),
                SurfaceEnablement(surface='compare_candidates',
                                  state='limited', reason='r'),
                SurfaceEnablement(surface='automatic_recommendation',
                                  state='enabled', reason='forged'),
            ),
            issued_at_utc='2026-10-06T00:00:00Z')


def test_surface_decision_covers_all_surfaces() -> None:
    d = _decision()
    with pytest.raises(ValueError):
        RecommendationSurfaceDecision.create(
            document_id='doc-1',
            decision_ref=AuthorityRef(
                kind='production_readiness_decision',
                ref_id=d.decision_id,
                ref_sha256=d.decision_sha256),
            outcome_at_issue='no_go',
            surfaces=(SurfaceEnablement(
                surface='inspect_prediction', state='disabled',
                reason='r'),),
            issued_at_utc='2026-10-06T00:00:00Z')


def test_issue_surface_decision_matches_evaluation() -> None:
    d = _decision(outcome='limited', campaign_ref=_ref('cv'),
                  solver_qualification_ref=_ref('bq'))
    sd = issue_surface_decision(d, '2026-10-06T00:00:00Z')
    verify_surface_decision(d, sd)
    assert {s.surface for s in sd.surfaces} == {
        'inspect_prediction', 'compare_candidates',
        'automatic_recommendation'}


def test_verify_surface_decision_detects_forgery() -> None:
    d = _decision()
    sd = issue_surface_decision(d, '2026-10-06T00:00:00Z')
    forged = sd.model_copy(update={
        'surfaces': (
            SurfaceEnablement(surface='inspect_prediction',
                              state='enabled', reason='forged'),
            SurfaceEnablement(surface='compare_candidates',
                              state='enabled', reason='forged'),
            SurfaceEnablement(surface='automatic_recommendation',
                              state='disabled', reason='forged'),
        )})
    with pytest.raises(ProductionReadinessIntegrityError):
        verify_surface_decision(d, forged)


def test_verify_surface_decision_detects_outcome_drift() -> None:
    d = _decision()
    sd = issue_surface_decision(d, '2026-10-06T00:00:00Z')
    drifted = sd.model_copy(update={'outcome_at_issue': 'limited'})
    with pytest.raises(ProductionReadinessIntegrityError):
        verify_surface_decision(d, drifted)


# ------------------------------------------------------- repository


def test_repository_round_trip(tmp_path) -> None:
    repo = _repo(tmp_path)
    d = _decision(outcome='limited', campaign_ref=_ref('cv'),
                  solver_qualification_ref=_ref('bq'))
    sd = issue_surface_decision(d, '2026-10-06T00:00:00Z')
    repo.decisions.save(d)
    repo.surface_decisions.save(sd)
    assert repo.get_decision(d.decision_id) == d
    assert repo.get_surface_decision(sd.surface_decision_id) == sd


def test_repository_append_only_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    d = _decision()
    repo.decisions.save(d)
    tampered = _decision(outcome_rationale='different')
    object.__setattr__(tampered, 'decision_id', d.decision_id)
    object.__setattr__(tampered, 'decision_sha256', d.decision_sha256)
    with pytest.raises(
            (ProductionReadinessConflictError,
             ProductionReadinessIntegrityError)):
        repo.decisions.save(tampered)
    repo.decisions.save(d)


def test_repository_detects_column_tampering(tmp_path) -> None:
    repo = _repo(tmp_path)
    d = _decision()
    repo.decisions.save(d)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_production_readiness_decisions SET outcome=? '
            'WHERE decision_id=?', ('production_ready', d.decision_id))
    with pytest.raises(ProductionReadinessIntegrityError):
        repo.get_decision(d.decision_id)


# ------------------------------------------------------- labels


def test_ja_label_coverage() -> None:
    assert production_readiness_line(
        'no_go', 'automatic_recommendation', 'disabled') \
        == '自動推奨: 無効（採用判定: NO_GO（採用不可））'
    assert production_readiness_label('hybrid') == 'ハイブリッド'
    assert production_readiness_label('__none__') == '__none__'
