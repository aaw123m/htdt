"""#937 Decision Brief — optimization candidates → sealed next action.

Covers the fail-closed evidence contract (missing pins block, never
promote), deterministic seal (same inputs → byte-identical record),
honest tiers (ready / conditional / not_ready), the verdict ladder,
stale freshness, seal forgery rejection and repository round-trip with
tamper detection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_decision_brief import (
    DECISION_GATE_ORDER,
    CadDecisionBrief,
    DecisionAction,
    DecisionCost,
    DecisionDelta,
    DecisionEvidencePin,
    DecisionGate,
    DecisionRecommendation,
    brief_freshness,
    build_decision_action,
    build_decision_brief,
    collect_gate_pins,
    decision_brief_ref,
)
from htdt.cad_decision_brief_repository import (
    CadDecisionBriefRepository,
    DecisionBriefIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-23T00:00:00+00:00'
GATE_KINDS = DECISION_GATE_ORDER


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _revisions(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision_a = scene_repository.save(
        _scene(fl_x=1.2), parent_revision_id=None
    ).revision
    revision_b = scene_repository.save(
        _scene(fl_x=1.6), parent_revision_id=revision_a.revision_id
    ).revision
    revision_c = scene_repository.save(
        _scene(fl_x=2.0), parent_revision_id=revision_b.revision_id
    ).revision
    return scene_repository, revision_a, revision_b, revision_c


def _revision_ref(revision) -> AuthorityRef:
    return AuthorityRef(
        kind='scene_revision',
        ref_id=revision.revision_id,
        ref_sha256=revision.content_hash,
    )


def _pin(revision, evidence_class: str = 'measured') -> DecisionEvidencePin:
    return DecisionEvidencePin(
        kind='scene_revision',
        ref_id=revision.revision_id,
        ref_sha256=revision.content_hash,
        evidence_class=evidence_class,
        freshness='current',
    )


def _verified_gates(revision, **overrides) -> tuple[DecisionGate, ...]:
    gates = []
    for kind in GATE_KINDS:
        gates.append(
            overrides.get(
                kind,
                DecisionGate(gate=kind, pin=_pin(revision), verdict='verified'),
            )
        )
    return tuple(gates)


def _apply_recommendation() -> DecisionRecommendation:
    return DecisionRecommendation(
        kind='apply_candidate',
        why='証拠チェーンが完結しているため候補を適用できます',
        actor='operator',
        verify_by='対象座席で再測定してIR差分を確認してください',
    )


def _verify_recommendation() -> DecisionRecommendation:
    return DecisionRecommendation(
        kind='collect_evidence',
        why='証拠チェーンが未提出のため採否を判断できません',
        actor='operator',
        verify_by='欠落した検証レコードをこの候補へピンしてください',
    )


def _action(revision, label: str, gates, **overrides) -> DecisionAction:
    recommendation = overrides.pop('recommendation', None)
    return build_decision_action(
        candidate_ref=_revision_ref(revision),
        label=label,
        gates=gates,
        comparability='comparable',
        recommendation=(
            _apply_recommendation() if recommendation is None else recommendation
        ),
        **overrides,
    )


def _brief(rev_a, rev_b, actions) -> CadDecisionBrief:
    return build_decision_brief(
        document_id=rev_a.document_id,
        scene_revision_id=rev_a.revision_id,
        scene_content_hash=rev_a.content_hash,
        baseline_ref=_revision_ref(rev_a),
        baseline_label='案A: 基準',
        actions=actions,
        provenance=('test',),
        created_at_utc=NOW,
    )


# -- tier / gate ladder -----------------------------------------------------


def test_ready_requires_every_gate_verified(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    action = _action(rev_b, '案B', _verified_gates(rev_a))
    assert action.tier == 'ready'
    assert action.disposition == 'recommended'
    assert action.gaps == ()
    assert action.recommendation.kind == 'apply_candidate'


def test_missing_pin_blocks_never_promotes(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    gates = _verified_gates(
        rev_a,
        deployment=DecisionGate(gate='deployment'),
    )
    action = _action(
        rev_b, '案B', gates, recommendation=_verify_recommendation()
    )
    assert action.tier == 'not_ready'
    assert action.disposition == 'requires_verification'
    assert [gap.code for gap in action.gaps] == ['evidence_missing']
    assert action.gaps[0].gate == 'deployment'


def test_conditional_verdict_lands_conditional_not_blocked(
    tmp_path: Path,
) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    gates = _verified_gates(
        rev_a,
        campaign=DecisionGate(
            gate='campaign', pin=_pin(rev_a), verdict='conditional'
        ),
    )
    action = _action(
        rev_b, '案B', gates, recommendation=_verify_recommendation()
    )
    assert action.tier == 'conditional'
    assert action.gaps == ()


def test_failed_verdict_blocks_with_failed_gap(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    gates = _verified_gates(
        rev_a,
        solver_gate=DecisionGate(
            gate='solver_gate', pin=_pin(rev_a), verdict='failed'
        ),
    )
    action = _action(
        rev_b, '案B', gates, recommendation=_verify_recommendation()
    )
    assert action.tier == 'not_ready'
    assert [gap.code for gap in action.gaps] == ['verdict_failed']


def test_stale_pin_blocks_with_stale_gap(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    stale_pin = DecisionEvidencePin(
        kind='scene_revision',
        ref_id=rev_a.revision_id,
        ref_sha256=rev_a.content_hash,
        evidence_class='predicted',
        freshness='stale',
    )
    gates = _verified_gates(
        rev_a,
        channel_verify=DecisionGate(
            gate='channel_verify', pin=stale_pin, verdict='verified'
        ),
    )
    action = _action(
        rev_b, '案B', gates, recommendation=_verify_recommendation()
    )
    assert action.tier == 'not_ready'
    assert [gap.code for gap in action.gaps] == ['evidence_stale']


def test_incompatible_comparability_blocks(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    action = build_decision_action(
        candidate_ref=_revision_ref(rev_b),
        label='案B',
        gates=_verified_gates(rev_a),
        comparability='incompatible_fidelity',
        recommendation=_verify_recommendation(),
    )
    assert action.tier == 'not_ready'
    assert [gap.code for gap in action.gaps] == ['not_comparable']
    assert action.gaps[0].gate == 'comparison'


def test_no_evidence_is_fail_closed_not_ready(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    action = _action(
        rev_b, '案B', (), recommendation=_verify_recommendation()
    )
    assert action.tier == 'not_ready'
    assert action.satisfied_gate_count == 0
    assert len(action.gaps) == len(GATE_KINDS)
    assert {gap.code for gap in action.gaps} == {'evidence_missing'}


def test_ready_rejects_verification_recommendation(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    with pytest.raises(ValueError):
        _action(
            rev_b,
            '案B',
            _verified_gates(rev_a),
            recommendation=_verify_recommendation(),
        )


def test_non_ready_rejects_apply_candidate(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    with pytest.raises(ValueError):
        _action(rev_b, '案B', (), recommendation=_apply_recommendation())


def test_forged_tier_cannot_survive_construction(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    action = _action(
        rev_b, '案B', (), recommendation=_verify_recommendation()
    )
    payload = action.model_dump(mode='python')
    payload['tier'] = 'ready'
    with pytest.raises(ValueError):
        DecisionAction(**payload)


def test_unknown_gate_kind_rejected(tmp_path: Path) -> None:
    _repo, _rev_a, rev_b, _rev_c = _revisions(tmp_path)
    with pytest.raises(ValueError):
        build_decision_action(
            candidate_ref=_revision_ref(rev_b),
            label='案B',
            gates=(
                DecisionGate(
                    gate='marketing_gate',  # type: ignore[arg-type]
                ),
            ),
            comparability='comparable',
            recommendation=_verify_recommendation(),
        )


def test_gate_verdict_requires_pin_and_pin_requires_verdict() -> None:
    pin = DecisionEvidencePin(
        kind='scene_revision',
        ref_id='rev-1',
        ref_sha256='a' * 64,
        evidence_class='measured',
        freshness='current',
    )
    with pytest.raises(ValueError):
        DecisionGate(gate='solver_gate', verdict='verified')
    with pytest.raises(ValueError):
        DecisionGate(gate='solver_gate', pin=pin)


# -- determinism / ranking ---------------------------------------------------


def test_brief_is_byte_identical_for_same_inputs(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, rev_c = _revisions(tmp_path)
    actions = (
        _action(rev_b, '案B', _verified_gates(rev_a)),
        _action(rev_c, '案C', (), recommendation=_verify_recommendation()),
    )
    first = _brief(rev_a, rev_b, actions)
    second = _brief(rev_a, rev_b, tuple(reversed(actions)))
    assert first.brief_sha256 == second.brief_sha256
    assert first.brief_id == second.brief_id
    assert first.model_dump(mode='json') == second.model_dump(mode='json')
    assert first.actions[0].rank == 1
    assert first.actions[0].tier == 'ready'
    assert first.top_tier == 'ready'
    assert first.ready_count == 1
    assert first.action_count == 2


def test_ranking_explanation_names_winner_and_gap(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, rev_c = _revisions(tmp_path)
    brief = _brief(
        rev_a,
        rev_b,
        (
            _action(rev_b, '案B', _verified_gates(rev_a)),
            _action(rev_c, '案C', (), recommendation=_verify_recommendation()),
        ),
    )
    assert '案B' in brief.ranking_explanation
    assert '案C' in brief.ranking_explanation


def test_ranking_prefers_more_verified_gates(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, rev_c = _revisions(tmp_path)
    partial = _verified_gates(
        rev_a,
        solver_gate=DecisionGate(
            gate='solver_gate', pin=_pin(rev_a), verdict='conditional'
        ),
    )
    brief = _brief(
        rev_a,
        rev_b,
        (
            _action(rev_c, '案C', (), recommendation=_verify_recommendation()),
            _action(
                rev_b, '案B', partial, recommendation=_verify_recommendation()
            ),
        ),
    )
    assert brief.actions[0].label == '案B'
    assert brief.top_tier == 'conditional'


def test_delta_improvements_break_ties_deterministically(
    tmp_path: Path,
) -> None:
    _repo, rev_a, rev_b, rev_c = _revisions(tmp_path)
    delta = DecisionDelta(
        objective_id='spl_uniformity',
        direction='improved',
        basis='measured',
    )
    action_a = _action(
        rev_b, '案A', _verified_gates(rev_a), deltas=(delta,)
    )
    action_b = _action(rev_c, '案Z', _verified_gates(rev_a))
    brief = _brief(rev_a, rev_b, (action_b, action_a))
    assert brief.actions[0].label == '案A'


def test_brief_seal_for_forgery_rejected(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    brief = _brief(
        rev_a,
        rev_b,
        (_action(rev_b, '案B', _verified_gates(rev_a)),),
    )
    payload = brief.model_dump(mode='python')
    payload['top_tier'] = 'not_ready'
    with pytest.raises(ValueError):
        CadDecisionBrief(**payload)


def test_decision_brief_ref_pins_seal(tmp_path: Path) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    ref = decision_brief_ref(brief)
    assert ref.kind == 'decision_brief'
    assert ref.ref_id == brief.brief_id
    assert ref.ref_sha256 == brief.brief_sha256


def test_collect_gate_pins_covers_identity_and_gate_refs(
    tmp_path: Path,
) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    refs = collect_gate_pins(brief)
    assert brief.baseline_ref in refs
    ids = {(ref.kind, ref.ref_id) for ref in refs}
    assert ('scene_revision', rev_a.revision_id) in ids
    assert ('scene_revision', rev_b.revision_id) in ids
    # baseline + candidate + five gate pins
    assert len(refs) == 1 + 1 + len(GATE_KINDS)


def test_freshness_reports_stale_for_superseded_revision(
    tmp_path: Path,
) -> None:
    _repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    state, reason = brief_freshness(
        brief, current_scene_revision_id=rev_b.revision_id
    )
    assert state == 'stale'
    assert 'revision' in reason
    state, _reason = brief_freshness(
        brief,
        current_scene_revision_id=rev_a.revision_id,
        current_scene_content_hash='f' * 64,
    )
    assert state == 'stale'
    state, _reason = brief_freshness(
        brief,
        current_scene_revision_id=rev_a.revision_id,
        current_scene_content_hash=rev_a.content_hash,
    )
    assert state == 'current'


def test_cost_unknown_rejects_provenance_and_known_requires_it() -> None:
    with pytest.raises(ValueError):
        DecisionCost(state='unknown', amount=100.0, currency='JPY')
    with pytest.raises(ValueError):
        DecisionCost(state='known', amount=100.0, currency='JPY')
    cost = DecisionCost(
        state='known',
        amount=12000.0,
        currency='JPY',
        source='見積書 2026-09',
        quoted_on='2026-09-01',
        quantity='1台',
        assumptions='搬入費込み',
    )
    assert cost.amount == 12000.0


def test_created_at_requires_utc(tmp_path: Path) -> None:
    _repo, rev_a, _rev_b, _rev_c = _revisions(tmp_path)
    with pytest.raises(ValueError):
        build_decision_brief(
            document_id=rev_a.document_id,
            scene_revision_id=rev_a.revision_id,
            scene_content_hash=rev_a.content_hash,
            baseline_ref=_revision_ref(rev_a),
            baseline_label='案A',
            actions=(),
            created_at_utc='2026-09-23 00:00:00',
        )


# -- repository round-trip ----------------------------------------------------


def test_repository_round_trip(tmp_path: Path) -> None:
    scene_repo, rev_a, rev_b, rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    brief = _brief(
        rev_a,
        rev_b,
        (
            _action(rev_b, '案B', _verified_gates(rev_a)),
            _action(rev_c, '案C', (), recommendation=_verify_recommendation()),
        ),
    )
    repository.save_brief(brief)
    loaded = repository.get_brief(brief.brief_id)
    assert loaded is not None
    assert loaded.brief_sha256 == brief.brief_sha256
    assert loaded.model_dump(mode='json') == brief.model_dump(mode='json')
    assert repository.latest_brief(rev_a.document_id).brief_id == brief.brief_id
    listed = repository.list_briefs(rev_a.document_id)
    assert [item.brief_id for item in listed] == [brief.brief_id]


def test_repository_save_rejects_unresolvable_identity_ref(
    tmp_path: Path,
) -> None:
    scene_repo, rev_a, _rev_b, _rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    brief = _brief(rev_a, rev_a, ())
    forged = brief.model_dump(mode='python')
    forged['baseline_ref'] = AuthorityRef(
        kind='scene_revision',
        ref_id='nonexistent-revision',
        ref_sha256='a' * 64,
    )
    forged_brief = build_decision_brief(
        document_id=forged['document_id'],
        scene_revision_id=forged['scene_revision_id'],
        scene_content_hash=forged['scene_content_hash'],
        baseline_ref=forged['baseline_ref'],
        baseline_label=forged['baseline_label'],
        actions=(),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError):
        repository.save_brief(forged_brief)


def test_repository_detects_payload_tamper(tmp_path: Path) -> None:
    scene_repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    repository.save_brief(brief)
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'UPDATE cad_decision_briefs SET top_tier = ? WHERE brief_id = ?',
            ('not_ready', brief.brief_id),
        )
        connection.commit()
    with pytest.raises((ValueError, DecisionBriefIntegrityError)):
        repository.get_brief(brief.brief_id)


def test_repository_detects_row_payload_json_tamper(tmp_path: Path) -> None:
    scene_repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    repository.save_brief(brief)
    payload = brief.model_dump(mode='json')
    payload['actions'] = []
    with sqlite3.connect(scene_repo.path) as connection:
        import json

        connection.execute(
            'UPDATE cad_decision_briefs SET payload_json = ?, '
            'action_count = ? WHERE brief_id = ?',
            (json.dumps(payload), 0, brief.brief_id),
        )
        connection.commit()
    with pytest.raises((ValueError, DecisionBriefIntegrityError)):
        repository.get_brief(brief.brief_id)


def test_repository_detects_deleted_identity_ref(tmp_path: Path) -> None:
    scene_repo, rev_a, rev_b, _rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    brief = _brief(
        rev_a, rev_b, (_action(rev_b, '案B', _verified_gates(rev_a)),)
    )
    repository.save_brief(brief)
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'DELETE FROM scene_revisions WHERE revision_id = ?',
            (rev_b.revision_id,),
        )
        connection.commit()
    with pytest.raises(DecisionBriefIntegrityError):
        repository.get_brief(brief.brief_id)


def test_latest_brief_is_none_without_rows(tmp_path: Path) -> None:
    scene_repo, rev_a, _rev_b, _rev_c = _revisions(tmp_path)
    repository = CadDecisionBriefRepository(scene_repo)
    assert repository.latest_brief(rev_a.document_id) is None
    assert repository.get_brief('dbrief-missing') is None
    assert repository.list_briefs('doc-1') == ()
