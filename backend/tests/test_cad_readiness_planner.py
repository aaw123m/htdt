"""#724 Next Best Action / Readiness Planner tests."""

from __future__ import annotations

from htdt.cad_readiness_planner import (
    ReadinessSignals,
    plan_next_actions,
)

NOW = '2026-09-23T00:00:00+00:00'
DOC = 'doc-1'


def _signals(**over) -> ReadinessSignals:
    payload = {
        'document_id': DOC,
        'scene_exists': True,
        'equipment_inventory_exists': True,
        'source_directivity_bound': True,
        'routing_resolved': True,
        'measurement_plan_exists': True,
        'measurement_count': 4,
        'holdout_measurement_count': 3,
        'stale_derived_results': 0,
        'calibration_plan_exists': True,
        'applied_settings_verified': True,
        'owned_room_validation_eligible': True,
        'system_variant_exists': True,
    }
    payload.update(over)
    return ReadinessSignals(**payload)


def test_identical_state_gives_identical_ordering() -> None:
    signals = _signals(
        source_directivity_bound=False,
        stale_derived_results=2,
        applied_settings_verified=False,
    )
    first = plan_next_actions(signals, intent='general', generated_at_utc=NOW)
    second = plan_next_actions(signals, intent='general', generated_at_utc=NOW)
    assert [a.action_id for a in first.actions] == [
        a.action_id for a in second.actions
    ]
    # Action ids are content-derived — stable across processes.
    kinds = [a.action_kind for a in first.actions]
    assert 'recompute_stale_results' in kinds
    assert 'bind_source_directivity' in kinds
    assert 'verify_applied_settings' in kinds


def test_no_scene_is_the_only_blocker() -> None:
    plan = plan_next_actions(
        _signals(scene_exists=False, equipment_inventory_exists=False),
        intent='general',
        generated_at_utc=NOW,
    )
    assert [a.action_kind for a in plan.actions] == ['capture_room_geometry']
    assert plan.actions[0].urgency == 'blocker'
    assert plan.actions[0].blocking_reason_codes == ('scene_missing',)


def test_intent_reorders_without_changing_truth() -> None:
    signals = _signals(
        source_directivity_bound=False,
        owned_room_validation_eligible=False,
        holdout_measurement_count=1,
        applied_settings_verified=False,
        system_variant_exists=False,
    )
    general = plan_next_actions(signals, intent='general', generated_at_utc=NOW)
    commissioning = plan_next_actions(
        signals, intent='commission_installed', generated_at_utc=NOW
    )
    general_kinds = [a.action_kind for a in general.actions]
    commissioning_kinds = [a.action_kind for a in commissioning.actions]
    # Same action set — domain truth unchanged.
    assert sorted(general_kinds) == sorted(commissioning_kinds)
    # But ordering differs: commissioning elevates its goal actions.
    assert commissioning_kinds.index('add_holdout_measurements') < (
        commissioning_kinds.index('bind_source_directivity')
    )


def test_urgency_tiers_blocker_first() -> None:
    signals = _signals(
        routing_resolved=False,
        source_directivity_bound=False,
        system_variant_exists=False,
    )
    plan = plan_next_actions(signals, intent='general', generated_at_utc=NOW)
    urgencies = [a.urgency for a in plan.actions]
    assert urgencies == sorted(
        urgencies, key={'blocker': 0, 'recommended': 1, 'optional': 2}.get
    )
    assert urgencies[0] == 'blocker'


def test_stale_results_recommend_recompute_not_remeasure() -> None:
    plan = plan_next_actions(
        _signals(stale_derived_results=3, measurement_count=0,
                 measurement_plan_exists=True),
        intent='general',
        generated_at_utc=NOW,
    )
    kinds = [a.action_kind for a in plan.actions]
    assert 'recompute_stale_results' in kinds
    # No remeasurement is suggested just because derived results are stale.
    assert 'plan_measurement_campaign' not in kinds


def test_unknown_counts_are_not_zero() -> None:
    # measurement_count=None (unknown) must not fire 'plan campaign'.
    plan = plan_next_actions(
        _signals(measurement_count=None, measurement_plan_exists=False),
        intent='general',
        generated_at_utc=NOW,
    )
    assert 'plan_measurement_campaign' not in [
        a.action_kind for a in plan.actions
    ]


def test_every_action_has_reason_deeplink_unlocks() -> None:
    plan = plan_next_actions(
        _signals(
            equipment_inventory_exists=False,
            source_directivity_bound=False,
            stale_derived_results=1,
            applied_settings_verified=False,
            system_variant_exists=False,
        ),
        intent='general',
        generated_at_utc=NOW,
    )
    assert len(plan.actions) >= 4
    for action in plan.actions:
        assert action.reason
        assert action.deep_link.startswith('cad.')
        assert action.unlocks
        assert action.action_id.startswith('readiness-action:')


def test_owned_room_validation_gap_is_explicit() -> None:
    plan = plan_next_actions(
        _signals(
            owned_room_validation_eligible=False,
            holdout_measurement_count=0,
        ),
        intent='commission_installed',
        generated_at_utc=NOW,
    )
    action = next(
        a for a in plan.actions
        if a.action_kind == 'add_holdout_measurements'
    )
    assert action.blocking_reason_codes == ('holdout_evidence_missing',)
    assert 'owned_room_validation_eligibility' in action.unlocks


def test_no_hidden_composite_score() -> None:
    plan = plan_next_actions(
        _signals(source_directivity_bound=False),
        intent='general',
        generated_at_utc=NOW,
    )
    assert not hasattr(plan, 'score')
    assert not hasattr(plan, 'readiness_score')


def test_fully_ready_project_has_no_actions() -> None:
    plan = plan_next_actions(
        _signals(), intent='general', generated_at_utc=NOW
    )
    assert plan.actions == ()
