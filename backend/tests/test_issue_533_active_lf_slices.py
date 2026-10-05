"""Issue #533: remaining active-LF control software slices — ALF fixtures.

Vendor-neutral fixtures covering the prescribed slice list:
- ALF10  two subwoofers / one seat — multi-sub optimization vs independent EQ
- ALF20  two subs / multiple seats — average FR and seat variance stay separate
- ALF30  support-speaker graph — one speaker excluded for band/headroom
- ALF40  DSP resource limit — physically valid graph rejected by the target
- ALF50  front/rear array geometry — boundary/spacing changes flip eligibility
- design -> exported -> applied -> read-back lifecycle integration
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from htdt.cad_active_lf_control import (
    ControlMatrixEntry,
    VendorControllerBinding,
    build_control_plan,
)
from htdt.cad_active_lf_design import (
    ACTIVE_CONTROL_LABEL_JA,
    DspResourceEnvelope,
    FilterPathDemand,
    SourceCapabilityEvidence,
    SpeakerGroupDecl,
    classify_lf_control_strategy,
    classify_matrix_path,
    evaluate_dsp_feasibility,
    evaluate_plan_eligibility,
    evaluate_source_group_eligibility,
    lf_control_objectives,
)
from htdt.cad_active_lf_control_repository import (
    ActiveLfIntegrityError,
    CadActiveLfControlRepository,
    LfLifecycleTransitionError,
    transition_plan,
)
from htdt.cad_scene import make_f1_scene
from htdt.cad_repository import SceneRepository
from htdt.comparison import FrequencyResponse
from htdt.native_authority_audit import audit_table_modes

_H = 'e' * 64
_BAND = (20.0, 150.0)


def _entry(input_group='LFE', output_group='SUBS', **overrides):
    kwargs = dict(
        input_group=input_group,
        output_group=output_group,
        gain_db=-4.0,
        delay_s=0.005,
        filter_kind='fir',
        filter_ref='fir-1',
        valid_band_hz=(20.0, 120.0),
        latency_s=0.012,
    )
    kwargs.update(overrides)
    return ControlMatrixEntry(**kwargs)


def _plan(**overrides):
    kwargs = dict(
        plan_id='alf-1',
        schema_version='alfc_v1',
        document_id='doc-533',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        logical_input_groups=('FL', 'FR', 'LFE'),
        physical_output_groups=('FL', 'FR', 'SUBS'),
        control_band_hz=_BAND,
        representation='transfer_matrix',
        matrix=(_entry('LFE', 'SUBS'),),
        objectives=('seat_consistency',),
        total_latency_s=0.018,
        lifecycle='proposed',
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_control_plan(**kwargs)


def _group(group_id, members, positions=None, boundary='unknown'):
    return SpeakerGroupDecl(
        group_id=group_id,
        member_entity_ids=tuple(members),
        member_positions_m=tuple(positions or ()),
        boundary=boundary,
    )


def _ev(band=(18.0, 250.0), headroom=6.0):
    return SourceCapabilityEvidence(
        usable_band_hz=band,
        declared_headroom_db=headroom,
        capability_ref='cap-' + str(band[0]),
    )


def _response(level: float) -> FrequencyResponse:
    freqs = tuple(float(f) for f in range(20, 151, 5))
    return FrequencyResponse(
        frequency_hz=freqs,
        level_db=tuple(level for _ in freqs),
    )


# ---------------------------------------------------------------------------
# ALF10 — two subwoofers / one seat
# ---------------------------------------------------------------------------


def test_alf10_multi_sub_group_is_optimization_not_support():
    subs = _group(
        'SUBS',
        ['sub-front', 'sub-rear'],
        positions=[(1.0, 0.5, 0.4), (1.0, 3.5, 0.4)],
    )
    evidence = {m: _ev() for m in subs.member_entity_ids}
    result = evaluate_source_group_eligibility(
        subs, evidence, _BAND, strategy='multi_sub_optimization'
    )
    assert result.verdict == 'eligible'

    plan = _plan(matrix=(_entry('LFE', 'SUBS'),))
    kind = classify_matrix_path(
        plan.matrix[0], _group('LFE', []), subs
    )
    assert kind == 'content_feed'
    lfe = _group('LFE', [])  # pure content channel: no member emitters
    assert (
        classify_lf_control_strategy(control_plan=plan, groups=(subs, lfe))
        == 'multi_sub_sum_optimization'
    )
    # a pure content feed is NOT cross-channel support
    assert result.strategy == 'multi_sub_optimization'


def test_alf10_single_seat_objectives_no_pairwise():
    plan = _plan()
    vector = lf_control_objectives(
        plan,
        'cand-1',
        [_response(-3.0)],
        seat_entity_ids=('mlp',),
        target_response=_response(0.0),
    )
    ids = {m.objective_id for m in vector.metrics}
    assert 'lf.seat.mlp.rms_difference_db' in ids
    assert 'lf.mean_seat_rms_difference_db' in ids
    assert not any('pairwise' in i for i in ids)
    # every metric is bound to the exact plan
    for metric in vector.metrics:
        assert metric.definition is not None
        assert metric.definition.comparison_model_id == 'lf-control-plan-objective-1'


def test_alf10_single_member_group_is_independent_eq():
    subs = _group('SUBS', ['sub-front'], positions=[(1.0, 0.5, 0.4)])
    result = evaluate_source_group_eligibility(
        subs, {'sub-front': _ev()}, _BAND, strategy='multi_sub_optimization'
    )
    assert result.verdict == 'ineligible'
    solo = evaluate_source_group_eligibility(
        subs, {'sub-front': _ev()}, _BAND, strategy='independent_eq'
    )
    assert solo.verdict == 'eligible'


# ---------------------------------------------------------------------------
# ALF20 — two subs / multiple seats: FR and seat variance stay separate
# ---------------------------------------------------------------------------


def test_alf20_multi_seat_objectives_keep_fr_and_variance_separate():
    plan = _plan()
    responses = [_response(-2.0), _response(-5.0), _response(-8.0)]
    seats = ('seat-a', 'seat-b', 'seat-c')
    vector = lf_control_objectives(
        plan,
        'cand-2',
        responses,
        seat_entity_ids=seats,
        target_response=_response(0.0),
    )
    ids = {m.objective_id for m in vector.metrics}
    assert 'lf.seat.pairwise_rms_difference_max_db' in ids
    assert 'lf.seat.pairwise_rms_difference_rms_db' in ids
    assert 'lf.mean_seat_rms_difference_db' in ids
    assert 'lf.max_seat_rms_difference_db' in ids
    for seat in seats:
        assert f'lf.seat.{seat}.rms_difference_db' in ids
    # variance and mean stay distinct quantities
    variance = vector.metric('lf.seat.pairwise_rms_difference_rms_db').value
    mean = vector.metric('lf.mean_seat_rms_difference_db').value
    assert variance != mean
    # every metric traces to the plan hash via its definition
    spec_sha = vector.metric('lf.mean_seat_rms_difference_db').definition.comparison_model_version
    assert len(spec_sha) == 64


def test_alf20_objectives_restrict_to_control_band():
    plan = _plan(
        control_band_hz=(20.0, 80.0),
        matrix=(_entry('LFE', 'SUBS', valid_band_hz=(20.0, 80.0)),),
    )
    # identical inside 20-80, divergent above
    a = FrequencyResponse(
        frequency_hz=tuple(float(f) for f in range(20, 151, 5)),
        level_db=tuple(-4.0 for _ in range(20, 151, 5)),
    )
    b = FrequencyResponse(
        frequency_hz=tuple(float(f) for f in range(20, 151, 5)),
        level_db=tuple(-4.0 if f <= 80 else -20.0 for f in range(20, 151, 5)),
    )
    vector = lf_control_objectives(
        plan, 'cand-3', [a, b], seat_entity_ids=('s1', 's2')
    )
    assert vector.metric('lf.seat.pairwise_rms_difference_max_db').value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ALF30 — support-speaker graph with one ineligible member
# ---------------------------------------------------------------------------


def _alf30_scene():
    """Mains support each other; SUBS support the mains; one wide speaker is
    too bandwidth-limited to support in the control band."""
    groups = (
        _group('FL', ['sp-fl'], positions=[(1.35, 0.75, 1.05)]),
        _group('FR', ['sp-fr'], positions=[(4.65, 0.75, 1.05)]),
        _group('SUBS', ['sub-f', 'sub-r'], positions=[(1.0, 0.5, 0.4), (1.0, 3.5, 0.4)]),
        _group('WIDE', ['sp-wide'], positions=[(0.5, 2.2, 1.0)]),
    )
    evidence = {
        'sp-fl': _ev(),
        'sp-fr': _ev(),
        'sub-f': _ev(),
        'sub-r': _ev(),
        # usable band starts at 60 Hz — cannot cover the 20-150 control band
        'sp-wide': _ev(band=(60.0, 250.0), headroom=3.0),
    }
    return groups, evidence


def test_alf30_support_graph_eligibility_excludes_weak_speaker():
    groups, evidence = _alf30_scene()
    plan = _plan(
        logical_input_groups=('FL', 'FR', 'WIDE', 'LFE'),
        physical_output_groups=('FL', 'FR', 'SUBS', 'WIDE'),
        matrix=(
            _entry('FL', 'SUBS', filter_kind='iir'),          # subs support FL
            _entry('FL', 'FR', gain_db=-10.0, filter_kind='gain_delay', filter_ref=None),  # FR supports FL
            _entry('FL', 'WIDE', gain_db=-6.0),               # wide speaker asked to support
        ),
        objectives=('support_speaker',),
        support_band_upper_limit_hz=150.0,
    )
    report = evaluate_plan_eligibility(plan, groups, evidence)
    by_out = {p.output_group: p for p in report.paths}
    assert by_out['SUBS'].path_kind == 'cross_channel_support'
    assert by_out['SUBS'].verdict == 'eligible'
    assert by_out['FR'].verdict == 'eligible'
    assert by_out['WIDE'].verdict == 'ineligible'
    assert report.overall == 'ineligible'


def test_alf30_missing_capability_evidence_is_unknown_not_pass():
    groups, _evidence = _alf30_scene()
    # headroom/band evidence absent for the support group entirely
    evidence = {'sp-fl': _ev(), 'sp-fr': _ev()}
    plan = _plan(
        logical_input_groups=('FL', 'LFE'),
        physical_output_groups=('SUBS',),
        matrix=(_entry('FL', 'SUBS'),),
        objectives=('support_speaker',),
    )
    report = evaluate_plan_eligibility(plan, groups, evidence)
    assert report.paths[0].verdict == 'unknown'
    assert report.overall == 'unknown'
    kinds = {c.check: c.status for c in report.paths[0].checks}
    assert kinds['usable_band_covers_control_band'] == 'UNKNOWN'
    assert kinds['headroom_declared'] == 'UNKNOWN'


def test_alf30_self_support_is_not_a_support_path():
    groups, evidence = _alf30_scene()
    plan = _plan(
        logical_input_groups=('FL', 'LFE'),
        physical_output_groups=('FL',),
        matrix=(_entry('FL', 'FL', filter_kind='peq', filter_ref=None),),
    )
    report = evaluate_plan_eligibility(plan, groups, evidence)
    assert report.paths[0].path_kind == 'diagonal'


# ---------------------------------------------------------------------------
# ALF40 — DSP resource limits
# ---------------------------------------------------------------------------


def test_alf40_cross_term_limit_rejects_physically_valid_graph():
    plan = _plan(
        logical_input_groups=('FL', 'FR', 'C', 'LFE'),
        physical_output_groups=('SUBS', 'FL', 'FR'),
        matrix=(
            _entry('LFE', 'SUBS'),
            _entry('FL', 'SUBS'),
            _entry('FR', 'SUBS'),
            _entry('FL', 'FR', filter_kind='gain_delay', filter_ref=None),
        ),
    )
    # 4 enabled paths, all with input != output ids -> 4 cross terms
    envelope = DspResourceEnvelope(
        envelope_id='denon-class',
        available_output_count=4,
        max_cross_terms=2,
        max_processing_paths=8,
        max_simultaneous_routes=4,
        max_delay_s=0.05,
        latency_budget_s=0.05,
    )
    report = evaluate_dsp_feasibility(plan, envelope)
    assert report.verdict == 'incompatible'
    cross = next(c for c in report.checks if c.check == 'cross_terms')
    assert cross.status == 'FAIL'
    assert '4 cross term(s) vs 2' in cross.detail


def test_alf40_undeclared_limits_are_unknown_not_unlimited():
    plan = _plan(
        matrix=(
            _entry('LFE', 'SUBS'),
            _entry('FL', 'SUBS'),
        ),
        logical_input_groups=('FL', 'LFE'),
        physical_output_groups=('SUBS',),
    )
    envelope = DspResourceEnvelope(envelope_id='opaque-dsp')  # nothing declared
    report = evaluate_dsp_feasibility(plan, envelope)
    assert report.verdict == 'unknown'
    statuses = {c.check: c.status for c in report.checks}
    assert statuses['cross_terms'] == 'UNKNOWN'
    assert statuses['output_count'] == 'UNKNOWN'
    assert statuses['fir_budget'] == 'UNKNOWN'


def test_alf40_fir_budget_checked_only_with_declared_demand():
    plan = _plan(matrix=(_entry('LFE', 'SUBS', filter_kind='fir'),))
    envelope = DspResourceEnvelope(
        envelope_id='dsp-x',
        available_output_count=8,
        max_cross_terms=8,
        max_processing_paths=16,
        max_simultaneous_routes=8,
        max_fir_taps_total=512,
        max_delay_s=0.05,
        latency_budget_s=0.05,
        min_gain_db=-20.0,
        max_gain_db=0.0,
    )
    # budget declared but demand undeclared -> UNKNOWN
    report = evaluate_dsp_feasibility(plan, envelope)
    assert report.checks and report.verdict == 'unknown'
    fir = next(c for c in report.checks if c.check == 'fir_budget')
    assert fir.status == 'UNKNOWN'
    # with declared demand under the budget -> compatible
    demand = FilterPathDemand(input_group='LFE', output_group='SUBS', fir_taps=256)
    report = evaluate_dsp_feasibility(plan, envelope, filter_demands=(demand,))
    assert report.verdict == 'compatible'
    # demand over the budget -> incompatible
    heavy = FilterPathDemand(input_group='LFE', output_group='SUBS', fir_taps=1024)
    report = evaluate_dsp_feasibility(plan, envelope, filter_demands=(heavy,))
    assert report.verdict == 'incompatible'


def test_alf40_output_group_outside_declared_set_fails():
    plan = _plan(physical_output_groups=('SUBS', 'TACTILE'))
    envelope = DspResourceEnvelope(
        envelope_id='avr',
        allowed_output_groups=('SUBS',),
        available_output_count=8,
        max_cross_terms=8,
        max_processing_paths=8,
        max_simultaneous_routes=8,
        max_fir_taps_total=4096,
        max_delay_s=0.05,
        latency_budget_s=0.05,
    )
    report = evaluate_dsp_feasibility(
        plan,
        envelope,
        filter_demands=(
            FilterPathDemand(input_group='LFE', output_group='SUBS', fir_taps=256),
        ),
    )
    outputs = next(c for c in report.checks if c.check == 'allowed_outputs')
    assert outputs.status == 'FAIL'
    assert report.verdict == 'incompatible'


# ---------------------------------------------------------------------------
# ALF50 — front/rear array geometry
# ---------------------------------------------------------------------------


def test_alf50_wavefront_array_requires_geometry_and_boundary():
    front = _group(
        'SUBS_FRONT',
        ['sub-fl', 'sub-fr'],
        positions=[(1.0, 0.4, 0.4), (5.0, 0.4, 0.4)],
        boundary='front',
    )
    evidence = {m: _ev() for m in front.member_entity_ids}
    ok = evaluate_source_group_eligibility(
        front, evidence, _BAND, strategy='wavefront_array'
    )
    assert ok.verdict == 'eligible'

    # boundary unrecorded -> honest unknown, not assumed front
    no_boundary = SpeakerGroupDecl(
        group_id='SUBS_FRONT',
        member_entity_ids=front.member_entity_ids,
        member_positions_m=front.member_positions_m,
        boundary='unknown',
    )
    result = evaluate_source_group_eligibility(
        no_boundary, evidence, _BAND, strategy='wavefront_array'
    )
    assert result.verdict == 'unknown'

    # positions unresolved -> unknown
    no_pos = SpeakerGroupDecl(
        group_id='SUBS_FRONT',
        member_entity_ids=front.member_entity_ids,
        boundary='front',
    )
    result = evaluate_source_group_eligibility(
        no_pos, evidence, _BAND, strategy='wavefront_array'
    )
    assert result.verdict == 'unknown'

    # single emitter -> ineligible
    solo = _group('SUBS_FRONT', ['sub-fl'], positions=[(1.0, 0.4, 0.4)], boundary='front')
    result = evaluate_source_group_eligibility(
        solo, evidence, _BAND, strategy='wavefront_array'
    )
    assert result.verdict == 'ineligible'


def test_alf50_geometry_delta_is_explicit():
    """Spacing/span changes appear in the recorded check detail — the
    robustness delta is explicit, not hidden in a score."""
    narrow = _group(
        'SUBS_FRONT', ['a', 'b'], positions=[(1.0, 0.4, 0.4), (2.0, 0.4, 0.4)],
        boundary='front',
    )
    wide = _group(
        'SUBS_FRONT', ['a', 'b'], positions=[(1.0, 0.4, 0.4), (5.0, 0.4, 0.4)],
        boundary='front',
    )
    evidence = {'a': _ev(), 'b': _ev()}
    r_narrow = evaluate_source_group_eligibility(
        narrow, evidence, _BAND, strategy='wavefront_array'
    )
    r_wide = evaluate_source_group_eligibility(
        wide, evidence, _BAND, strategy='wavefront_array'
    )
    d_narrow = next(c.detail for c in r_narrow.checks if c.check == 'array_geometry')
    d_wide = next(c.detail for c in r_wide.checks if c.check == 'array_geometry')
    assert d_narrow != d_wide
    assert '1 m' in d_narrow and '4 m' in d_wide


# ---------------------------------------------------------------------------
# Strategy capability labels
# ---------------------------------------------------------------------------


def test_capability_labels_keep_strategies_distinct():
    groups, evidence = _alf30_scene()

    # bass management only
    assert classify_lf_control_strategy(bass_management_present=True) == 'bass_management_only'
    assert classify_lf_control_strategy() == 'none'

    # multi-sub optimization: content feed into a multi-emitter group
    lfe = _group('LFE', [])
    multi = _plan(matrix=(_entry('LFE', 'SUBS'),))
    assert (
        classify_lf_control_strategy(control_plan=multi, groups=(*groups, lfe))
        == 'multi_sub_sum_optimization'
    )

    # cross-channel support
    support = _plan(
        logical_input_groups=('FL', 'LFE'),
        physical_output_groups=('SUBS',),
        matrix=(_entry('FL', 'SUBS'),),
        objectives=('support_speaker',),
    )
    assert (
        classify_lf_control_strategy(control_plan=support, groups=groups)
        == 'cross_channel_support_control'
    )

    # external proprietary: vendor-opaque never claims HTDT generation
    ext = _plan(
        representation='vendor_opaque',
        matrix=(),
        vendor_binding=VendorControllerBinding(
            vendor='Dirac', product='ART', profile_name='p',
            declared_control_band_hz=_BAND,
        ),
    )
    assert (
        classify_lf_control_strategy(control_plan=ext, groups=groups)
        == 'external_proprietary_control'
    )

    # wavefront requires support paths + wavefront objective + eligible array
    wave = _plan(
        logical_input_groups=('FL', 'LFE'),
        physical_output_groups=('SUBS',),
        matrix=(_entry('FL', 'SUBS'),),
        objectives=('support_speaker', 'wavefront_control'),
    )
    # the support array must actually qualify: boundary-declared members
    front_groups = tuple(
        SpeakerGroupDecl(
            group_id=g.group_id,
            member_entity_ids=g.member_entity_ids,
            member_positions_m=g.member_positions_m,
            boundary='front' if g.group_id == 'SUBS' else g.boundary,
        )
        for g in groups
    )
    assert (
        classify_lf_control_strategy(
            control_plan=wave, groups=front_groups, evidence=evidence
        )
        == 'wavefront_active_control'
    )
    # same plan without an eligible array stays cross-channel support
    assert (
        classify_lf_control_strategy(
            control_plan=wave, groups=groups, evidence={'sub-f': _ev(), 'sub-r': _ev()}
        )
        == 'cross_channel_support_control'
    )

    # every label carries JA display text
    for label in (
        'bass_management_only',
        'multi_sub_sum_optimization',
        'cross_channel_support_control',
        'wavefront_active_control',
        'external_proprietary_control',
    ):
        assert ACTIVE_CONTROL_LABEL_JA[label]


# ---------------------------------------------------------------------------
# Integration: design -> exported -> applied -> read-back
# ---------------------------------------------------------------------------


def _repo(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    saved = scene_repo.save(make_f1_scene(), parent_revision_id=None)
    return scene_repo, saved.revision


def test_plan_persistence_and_lifecycle_journal(tmp_path):
    scene_repo, revision = _repo(tmp_path)
    repo = CadActiveLfControlRepository(scene_repo)
    plan = _plan(
        document_id='fixture-f1',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )

    repo.save_plan(plan, 'fixture-f1')
    repo.save_plan(plan, 'fixture-f1')  # identical re-save is a no-op

    exported = transition_plan(plan, lifecycle='exported')
    applied = transition_plan(plan, lifecycle='applied')
    read_back = transition_plan(plan, lifecycle='read_back')
    verified = transition_plan(plan, lifecycle='verified')
    for state in (exported, applied, read_back, verified):
        repo.save_plan(state, 'fixture-f1')

    repo.record_transition(
        'fixture-f1', from_plan=plan, to_plan=exported, event_kind='export',
        evidence_ref='export-package-sha',
    )
    repo.record_transition(
        'fixture-f1', from_plan=exported, to_plan=applied, event_kind='apply',
        evidence_ref='device-apply-ack-1',
    )
    repo.record_transition(
        'fixture-f1', from_plan=applied, to_plan=read_back,
        event_kind='read_back', evidence_ref='readback-snapshot-1',
    )
    repo.record_transition(
        'fixture-f1', from_plan=read_back, to_plan=verified,
        event_kind='verify', evidence_ref='post-measurement-campaign-1',
    )

    history = repo.plans_for('fixture-f1', plan.plan_id)
    assert [p.lifecycle for p in history] == [
        'proposed', 'exported', 'applied', 'read_back', 'verified',
    ]
    assert repo.current_plan('fixture-f1', plan.plan_id).lifecycle == 'verified'

    events = repo.lifecycle_events('fixture-f1', plan.plan_id)
    assert [e.event_kind for e in events] == ['export', 'apply', 'read_back', 'verify']
    assert events[0].from_plan_sha256 == plan.plan_sha256
    assert events[-1].to_plan_sha256 == verified.plan_sha256


def test_lifecycle_journal_is_fail_closed(tmp_path):
    scene_repo, revision = _repo(tmp_path)
    repo = CadActiveLfControlRepository(scene_repo)
    plan = _plan(document_id='fixture-f1', scene_revision_id=revision.revision_id)
    repo.save_plan(plan, 'fixture-f1')

    # cannot jump proposed -> verified
    verified = transition_plan(plan, lifecycle='verified')
    repo.save_plan(verified, 'fixture-f1')
    with pytest.raises(LfLifecycleTransitionError, match='illegal'):
        repo.record_transition(
            'fixture-f1', from_plan=plan, to_plan=verified, event_kind='verify',
            evidence_ref='x',
        )

    # applied requires evidence
    exported = transition_plan(plan, lifecycle='exported')
    applied = transition_plan(plan, lifecycle='applied')
    repo.save_plan(exported, 'fixture-f1')
    repo.save_plan(applied, 'fixture-f1')
    repo.record_transition(
        'fixture-f1', from_plan=plan, to_plan=exported, event_kind='export',
    )
    with pytest.raises(LfLifecycleTransitionError, match='evidence'):
        repo.record_transition(
            'fixture-f1', from_plan=exported, to_plan=applied, event_kind='apply',
        )

    # endpoints must be persisted
    ghost = transition_plan(plan, lifecycle='read_back')
    with pytest.raises(ActiveLfIntegrityError, match='persisted'):
        repo.record_transition(
            'fixture-f1', from_plan=applied, to_plan=ghost,
            event_kind='read_back', evidence_ref='rb',
        )


def test_persisted_plan_integrity_fails_closed(tmp_path):
    scene_repo, revision = _repo(tmp_path)
    repo = CadActiveLfControlRepository(scene_repo)
    plan = _plan(document_id='fixture-f1', scene_revision_id=revision.revision_id)
    repo.save_plan(plan, 'fixture-f1')

    # a tampered payload row must fail on read
    tampered = plan.model_dump(mode='json')
    tampered['control_band_hz'] = [10.0, 150.0]
    import json
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'UPDATE cad_active_lf_control_plans SET payload_json=? WHERE plan_id=?',
            (json.dumps(tampered), plan.plan_id),
        )
    with pytest.raises(ValidationError):
        repo.get_plan('fixture-f1', plan.plan_id, plan.plan_sha256)


def test_new_tables_registered_for_authority_audit():
    modes = audit_table_modes()
    assert modes['cad_active_lf_control_plans'] == 'structural_only'
    assert modes['cad_active_lf_control_events'] == 'structural_only'


def test_vendor_opaque_plan_persists_without_claiming_generation(tmp_path):
    scene_repo, revision = _repo(tmp_path)
    repo = CadActiveLfControlRepository(scene_repo)
    plan = _plan(
        document_id='fixture-f1',
        scene_revision_id=revision.revision_id,
        representation='vendor_opaque',
        matrix=(),
        vendor_binding=VendorControllerBinding(
            vendor='Trinnov', product='WaveForming', profile_name='array-2f',
            declared_control_band_hz=_BAND,
        ),
    )
    repo.save_plan(plan, 'fixture-f1')
    loaded = repo.get_plan('fixture-f1', plan.plan_id, plan.plan_sha256)
    assert loaded.representation == 'vendor_opaque'
    assert loaded.vendor_binding.vendor == 'Trinnov'
    exported = transition_plan(plan, lifecycle='exported')
    repo.save_plan(exported, 'fixture-f1')
    repo.record_transition(
        'fixture-f1', from_plan=plan, to_plan=exported, event_kind='export',
        evidence_ref='vendor-export-file-sha',
    )
    assert repo.current_plan('fixture-f1', plan.plan_id).lifecycle == 'exported'
