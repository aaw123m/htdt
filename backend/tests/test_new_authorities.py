"""Tests for the slice-10 authority modules: #1022 #1023 #1036 #1037
#1038 #1045 #1048 #1052 #1053 #1055."""

from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_spatial_uncertainty import (
    SpatialObservationUncertainty,
    UncertaintyBound,
    build_spatial_observation_uncertainty,
    evaluate_dimension_decision,
)
from htdt.cad_renderer_topology import (
    LogicalRoleBinding,
    SpeakerActivity,
    active_source_entities,
    build_renderer_output_topology,
    evaluate_renderer_topology,
    renderer_topology_status,
)
from htdt.cad_program_dynamics import (
    DynamicsMechanismProfile,
    EffectiveDynamicsState,
    build_effective_dynamics_state,
    build_program_dynamics_profile,
    dynamics_evaluation_status,
    evaluate_dynamics_state,
)
from htdt.cad_audio_transform_lineage import (
    AudioSignalFormat,
    build_audio_format_transform,
    evaluate_transform_lineage,
)
from htdt.cad_occupancy_acoustics import (
    OccupantAcousticProxy,
    SeatOccupancyBinding,
    build_room_occupancy_state,
    evaluate_occupancy_compatibility,
    evaluate_occupancy_state,
)
from htdt.cad_media_source_capability import (
    ContentProfile,
    ObservedSourceOutput,
    SourceCapabilityObservation,
    build_media_source_condition,
    evaluate_source_capability,
)
from htdt.cad_personal_listening import (
    PersonalAudioEndpoint,
    build_personal_listening_route,
    evaluate_personal_route,
)
from htdt.cad_streaming_qoe import (
    StreamingPlaybackEvent,
    build_streaming_session,
    derive_qoe_metrics,
)
from htdt.cad_av_session_reliability import (
    AVSessionEvent,
    build_av_playback_session,
    summarize_reliability,
)
from htdt.cad_device_backup import (
    BackupScopeCoverage,
    RestoreCompatibilityDecision,
    build_backup_artifact,
    evaluate_restore_compatibility,
)


def _h(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


# ---- #1023 spatial uncertainty ----

def test_uncertainty_unknown_has_no_quantities():
    with pytest.raises(ValueError, match='at least one recorded quantity'):
        UncertaintyBound(
            representation='bounded_interval', component='random'
        )


def test_classification_confidence_is_not_metric_accuracy():
    # RoomPlan-style HIGH classification says nothing metric -> UNKNOWN.
    u = build_spatial_observation_uncertainty(
        uncertainty_id='u1',
        version='1',
        observation_ref_kind='roomplan_capture',
        observation_ref_id='cap-1',
        domain='position',
        bound=UncertaintyBound(
            representation='unknown', component='unknown'
        ),
        classification_confidence='high',
    )
    r = evaluate_dimension_decision(
        decision='install within 3mm',
        allowed_tolerance_m=0.003,
        uncertainty=u,
    )
    assert r.status == 'UNKNOWN'
    assert 'confidence' in r.reason


def test_uncertainty_gate_fail_and_pass():
    big = build_spatial_observation_uncertainty(
        uncertainty_id='u2',
        version='1',
        observation_ref_kind='laser_dim',
        observation_ref_id='d1',
        domain='dimension',
        bound=UncertaintyBound(
            representation='manufacturer_accuracy_statement',
            dimensional_bound_m=0.02,
            component='combined',
        ),
    )
    assert evaluate_dimension_decision(
        decision='d', allowed_tolerance_m=0.003, uncertainty=big
    ).status == 'FAIL'
    assert evaluate_dimension_decision(
        decision='d', allowed_tolerance_m=0.05, uncertainty=big
    ).status == 'PASS'


# ---- #1022 renderer topology ----

def _topology(**overrides):
    kwargs = dict(
        topology_id='topo-1',
        version='1',
        processor_equipment_id='avr-1',
        firmware_version='2.1.0',
        renderer_family='auro_3d',
        role_bindings=(
            LogicalRoleBinding(
                requested_role_id='role:FL',
                configured_role_id='role:FL',
                output_port_id='PREOUT:FL',
                installed_equipment_id='spk-fl',
                activity='active',
            ),
            LogicalRoleBinding(
                requested_role_id='role:SurroundHeightL',
                configured_role_id='role:RearHeightL',
                installed_equipment_id='spk-rhl',
                activity='substituted',
                substitution_rule='auro:rear_height_substitute',
            ),
        ),
        speaker_activities=(
            SpeakerActivity(
                installed_equipment_id='spk-top-mid',
                activity='inactive',
            ),
        ),
        evidence_tier='hardware_readback_verified',
    )
    kwargs.update(overrides)
    return build_renderer_output_topology(**kwargs)


def test_topology_active_sources_exclude_inactive():
    topo = _topology()
    assert set(active_source_entities(topo)) == {'spk-fl', 'spk-rhl'}


def test_substitution_requires_rule():
    with pytest.raises(ValueError, match='substitution_rule'):
        LogicalRoleBinding(
            requested_role_id='role:X',
            configured_role_id='role:Y',
        )


def test_topology_evaluation_flags_unaccounted_speakers():
    topo = _topology()
    ok = evaluate_renderer_topology(
        topology=topo,
        installed_equipment_ids=('spk-fl', 'spk-rhl', 'spk-top-mid'),
    )
    assert renderer_topology_status(ok) == 'PASS'
    bad = evaluate_renderer_topology(
        topology=topo,
        installed_equipment_ids=(
            'spk-fl', 'spk-rhl', 'spk-top-mid', 'spk-extra'
        ),
    )
    assert renderer_topology_status(bad) == 'FAIL'


# ---- #1036 program dynamics ----

def test_dynamics_metadata_dependent_stays_unknown():
    profile = build_program_dynamics_profile(
        profile_id='p1',
        version='1',
        processor_equipment_id='avr-1',
        firmware_version='1.0',
        mechanisms=(
            DynamicsMechanismProfile(
                mechanism='drc',
                metadata_dependent=True,
                opaque_behavior=True,
            ),
            DynamicsMechanismProfile(
                mechanism='limiter_protection',
                opaque_behavior=False,
            ),
        ),
    )
    state = build_effective_dynamics_state(
        state_id='s1',
        device_equipment_id='avr-1',
        firmware_version='1.0',
        input_codec_family='ddp',
        states=(
            EffectiveDynamicsState(mechanism='drc', state='on'),
            EffectiveDynamicsState(
                mechanism='limiter_protection',
                state='enabled',
                evidence_tier='device_readback_verified',
                transfer_effective_ref='meas-1',
            ),
        ),
    )
    ev = evaluate_dynamics_state(profile=profile, state=state)
    checks = {c.check: c.status for c in ev.checks}
    assert checks['context_binding'] == 'PASS'
    assert checks['mechanism:drc'] == 'UNKNOWN'
    assert checks['mechanism:limiter_protection'] == 'PASS'
    assert dynamics_evaluation_status(ev) == 'UNKNOWN'


# ---- #1037 transform lineage ----

def _fmt(fid: str, **kw) -> AudioSignalFormat:
    return AudioSignalFormat(format_id=fid, **kw)


def test_lineage_opaque_downmix_and_object_loss():
    bitstream = _fmt(
        'f1', codec='ddp', signal_kind='bitstream',
        channel_count=None, carries_object_metadata=True,
    )
    pcm51 = _fmt(
        'f2', codec='pcm', signal_kind='pcm', channel_count=6,
        carries_object_metadata=False,
    )
    stereo = _fmt(
        'f3', codec='pcm', signal_kind='pcm', channel_count=2,
        carries_object_metadata=False,
    )
    decode = build_audio_format_transform(
        transform_id='t1', device_node_id='tv',
        input_format=bitstream, output_format=pcm51, kind='decode',
    )
    downmix = build_audio_format_transform(
        transform_id='t2', device_node_id='avr',
        input_format=pcm51, output_format=stereo, kind='downmix',
    )
    ev = evaluate_transform_lineage((decode, downmix))
    checks = {c.check: c.status for c in ev.checks}
    assert checks['chain_contiguous'] == 'PASS'
    assert checks['downmix_coefficients'] == 'UNKNOWN'
    assert checks['object_metadata_survival'] == 'UNKNOWN'


def test_lineage_gap_fails():
    a = _fmt('a1', codec='pcm', signal_kind='pcm')
    b = _fmt('a2', codec='pcm', signal_kind='pcm')
    c = _fmt('a3', codec='pcm', signal_kind='pcm')
    t1 = build_audio_format_transform(
        transform_id='t1', device_node_id='n1',
        input_format=a, output_format=b, kind='pcm_passthrough',
    ) if a == b else None
    # passthrough requires identical in/out — use decode instead
    t1 = build_audio_format_transform(
        transform_id='t1', device_node_id='n1',
        input_format=a, output_format=b, kind='decode',
    )
    t2 = build_audio_format_transform(
        transform_id='t2', device_node_id='n2',
        input_format=c, output_format=a, kind='decode',
    )
    ev = evaluate_transform_lineage((t1, t2))
    checks = {ch.check: ch.status for ch in ev.checks}
    assert checks['chain_contiguous'] == 'FAIL'


# ---- #1038 occupancy ----

def _occupancy(state_id: str, seats):
    return build_room_occupancy_state(
        state_id=state_id,
        version='1',
        scene_revision_id='rev-1',
        scene_content_hash=_h('scene'),
        seats=seats,
    )


def test_occupancy_compatibility():
    empty = _occupancy(
        'o1', (SeatOccupancyBinding(seat_entity_id='seat-1', occupancy='empty'),)
    )
    occupied = _occupancy(
        'o2',
        (SeatOccupancyBinding(seat_entity_id='seat-1', occupancy='occupied'),),
    )
    assert evaluate_occupancy_compatibility(
        prediction_occupancy=occupied, measurement_occupancy=occupied
    ).status == 'compatible'
    assert evaluate_occupancy_compatibility(
        prediction_occupancy=occupied, measurement_occupancy=empty
    ).status == 'incompatible'
    assert evaluate_occupancy_compatibility(
        prediction_occupancy=occupied,
        measurement_occupancy=empty,
        observable_declared_insensitive=True,
        insensitivity_policy_id='policy-x',
    ).status == 'limited'
    # insensitivity claim without a policy is not honored
    assert evaluate_occupancy_compatibility(
        prediction_occupancy=occupied,
        measurement_occupancy=empty,
        observable_declared_insensitive=True,
    ).status == 'incompatible'


def test_occupancy_proxy_requires_occupied():
    with pytest.raises(ValueError, match='occupancy=occupied'):
        SeatOccupancyBinding(
            seat_entity_id='seat-1',
            occupancy='empty',
            occupant_proxy=OccupantAcousticProxy(
                representation='bounded_acoustic_proxy',
                absorption_profile_ref='abs-1',
                validity_domain='250-4000Hz',
            ),
        )
    checks = evaluate_occupancy_state(
        _occupancy(
            'o9',
            (SeatOccupancyBinding(seat_entity_id='s', occupancy='occupied'),),
        ),
        solver_supports_occupancy=False,
    )
    assert {c.check: c.status for c in checks}[
        'solver_participation'
    ] == 'UNKNOWN'


# ---- #1045 media source capability ----

def test_source_fallback_visible_even_if_transport_fine():
    condition = build_media_source_condition(
        condition_id='c1',
        version='1',
        device_equipment_id='atv-1',
        app_identity='svc-app',
        app_version='3.2',
        content_profile=ContentProfile(
            profile_id='cp-1',
            video_codec='hevc',
            hdr_family='dolby_vision',
            audio_codec='ddp_atmos',
            audio_layout='5.1.4',
        ),
        capabilities=(
            SourceCapabilityObservation(
                aspect='audio:ddp_atmos', state='observed_fallback'
            ),
        ),
        observed_output=ObservedSourceOutput(
            video_codec='hevc',
            resolution='2160p',
            hdr_family='hdr10',
            audio_format='pcm',
            audio_layout='5.1',
            observed_at_utc='2026-09-25T00:00:00+00:00',
        ),
    )
    checks = {c.check: c.status for c in evaluate_source_capability(condition)}
    assert checks['requested_profile_preserved'] == 'FAIL'
    assert checks['capability_evidence'] == 'UNKNOWN'


# ---- #1048 personal listening ----

def test_auracast_requires_explicit_capability():
    endpoint = PersonalAudioEndpoint(
        endpoint_id='ep-1', endpoint_kind='earbud'
    )
    route = build_personal_listening_route(
        route_id='r1',
        version='1',
        transport_family='auracast_broadcast',
        endpoint_id='ep-1',
        auracast_broadcast_id='bc-1',
        coexistence='both_active',
    )
    checks = {
        c.check: c.status
        for c in evaluate_personal_route(route=route, endpoint=endpoint)
    }
    assert checks['auracast_capability'] == 'UNKNOWN'
    assert checks['endpoint_bound'] == 'PASS'

    capable = PersonalAudioEndpoint(
        endpoint_id='ep-1', endpoint_kind='earbud',
        supports_auracast_pbp=True, supports_le_audio=True,
    )
    checks = {
        c.check: c.status
        for c in evaluate_personal_route(route=route, endpoint=capable)
    }
    assert checks['auracast_capability'] == 'PASS'


def test_room_prediction_inheritance_flagged():
    route = build_personal_listening_route(
        route_id='r2',
        version='1',
        transport_family='wired_headphone',
        claims_room_acoustic_prediction=True,
    )
    checks = {
        c.check: c.status
        for c in evaluate_personal_route(route=route, endpoint=None)
    }
    assert checks['room_prediction_boundary'] == 'FAIL'


# ---- #1052 streaming QoE ----

def test_qoe_metrics_derive_and_unknown_not_zero():
    session = build_streaming_session(
        session_id='s1',
        version='1',
        device_equipment_id='tv-1',
        duration_s=100.0,
        events=(
            StreamingPlaybackEvent(kind='playback_requested', occurred_at_s=0.0),
            StreamingPlaybackEvent(kind='first_frame', occurred_at_s=2.5),
            StreamingPlaybackEvent(kind='buffering_start', occurred_at_s=10.0),
            StreamingPlaybackEvent(kind='buffering_end', occurred_at_s=13.0),
            StreamingPlaybackEvent(kind='error', occurred_at_s=20.0),
            StreamingPlaybackEvent(kind='playback_end', occurred_at_s=100.0),
        ),
    )
    m = derive_qoe_metrics(session)
    assert m.startup_delay_s == 2.5
    assert m.rebuffer_count == 1
    assert m.total_rebuffer_s == 3.0
    assert m.rebuffer_ratio == 0.03
    assert m.longest_stall_s == 3.0
    assert m.playback_error_count == 1
    assert m.representation_switch_count is None  # UNKNOWN, not 0
    assert m.audio_format_change_count is None

    empty = build_streaming_session(
        session_id='s2', version='1', device_equipment_id='tv-1'
    )
    m2 = derive_qoe_metrics(empty)
    assert m2.startup_delay_s is None
    assert m2.rebuffer_count is None


# ---- #1053 AV session reliability ----

def test_reliability_summary_scoped_and_counted():
    s1 = build_av_playback_session(
        session_id='s1', version='1', signal_path_id='p1',
        trigger='cold_start', outcome='success',
        events=(AVSessionEvent(kind='negotiation_completed', occurred_at_s=1.0),),
    )
    s2 = build_av_playback_session(
        session_id='s2', version='1', signal_path_id='p1',
        trigger='cold_start', outcome='failure',
        events=(
            AVSessionEvent(kind='hdcp_auth_failure', occurred_at_s=1.0),
            AVSessionEvent(kind='dropout', occurred_at_s=2.0),
        ),
    )
    s3 = build_av_playback_session(
        session_id='s3', version='1', signal_path_id='p2',
        trigger='resume', outcome='success',
    )
    summary = summarize_reliability(
        (s1, s2, s3), signal_path_id='p1', trigger='cold_start'
    )
    assert summary.sample_count == 2
    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert summary.success_ratio == 0.5
    assert summary.dropout_event_count == 1
    assert summary.negotiation_failure_count == 1
    empty = summarize_reliability((), signal_path_id='p9')
    assert empty.sample_count == 0
    assert empty.success_ratio is None


# ---- #1055 device backup ----

def test_backup_restore_gate():
    artifact = build_backup_artifact(
        artifact_id='b1',
        device_equipment_id='avr-1',
        content_sha256=_h('backup-bytes'),
        manufacturer='Denon',
        model='X3800',
        firmware_version='2.1.0',
        scope=(
            BackupScopeCoverage(family='peq_calibration', state='included'),
            BackupScopeCoverage(family='account_credentials', state='excluded'),
        ),
    )
    # unknown when no decision recorded
    checks = {
        c.check: c.status
        for c in evaluate_restore_compatibility(
            artifact=artifact,
            target_equipment_id='avr-1',
            target_firmware_version='2.1.0',
            decisions=(),
        )
    }
    assert checks['compatibility_decision'] == 'UNKNOWN'
    assert checks['device_identity'] == 'PASS'
    assert checks['scope_coverage'] == 'UNKNOWN'

    decision = RestoreCompatibilityDecision(
        artifact_sha256=artifact.content_sha256,
        target_equipment_id='avr-1',
        target_firmware_version='2.1.0',
        state='documented_compatible',
        rule_source='vendor manual',
    )
    checks = {
        c.check: c.status
        for c in evaluate_restore_compatibility(
            artifact=artifact,
            target_equipment_id='avr-1',
            target_firmware_version='2.1.0',
            decisions=(decision,),
        )
    }
    assert checks['compatibility_decision'] == 'PASS'

    bad = RestoreCompatibilityDecision(
        artifact_sha256=artifact.content_sha256,
        target_equipment_id='avr-9',
        state='documented_incompatible',
    )
    checks = {
        c.check: c.status
        for c in evaluate_restore_compatibility(
            artifact=artifact,
            target_equipment_id='avr-9',
            target_firmware_version=None,
            decisions=(bad,),
        )
    }
    assert checks['compatibility_decision'] == 'FAIL'
    assert checks['device_identity'] == 'UNKNOWN'
