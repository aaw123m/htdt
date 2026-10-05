"""REV56-TRANSPORT regression tests: #582 A/V latency authority,
#583 HDMI design & verification authority, #591 networked AV
qualification authority.

Fixture map (issue-suggested ids):
- AVL10/20/30/40/50/60/70: sign convention, sealed profiles, composite
  fail-closed, verdict lifecycle, LIP/protocol-vs-measured split,
  staleness, persistence round-trip.
- HDMI10/20/30/40/50/60/70/80: signal profile seal, EDID artifact,
  HDCP observation, link observation, verification-record guards,
  qualification ladder, RP28 honest profile, persistence round-trip.
- NAV10/20/30/40/50/60/70/80: topology declaration, flow binding,
  observation kind guards, qualification checks + media ladder,
  staleness, persistence round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_av_latency import (
    AVLatencyStage,
    AVLatencyCompensationEntry,
    AVLatencyObservation,
    build_atsc_is191_profile,
    build_av_latency_path,
    build_av_latency_path_measurement,
    build_itu_bt1359_profile,
    evaluate_av_latency_path,
)
from htdt.cad_av_latency_repository import (
    AVLatencyConflictError,
    CadAVLatencyRepository,
)
from htdt.cad_hdmi_verification import (
    DiagnosticBypassTest,
    HopVerification,
    build_edid_artifact,
    build_hdcp_observation,
    build_hdmi_signal_profile,
    build_hdmi_verification_record,
    build_link_observation,
    evaluate_hdmi_qualification,
    seed_rp28_profile,
)
from htdt.cad_hdmi_verification_repository import (
    CadHDMIVerificationRepository,
)
from htdt.cad_network_av import (
    NetworkLinkDecl,
    NetworkNodeDecl,
    NetworkPortDecl,
    build_network_av_path,
    build_network_media_flow,
    build_network_timing_observation,
    build_network_transport_observation,
    evaluate_network_av_qualification,
)
from htdt.cad_network_av_repository import (
    CadNetworkAVRepository,
    NetworkAVConflictError,
    NetworkAVIntegrityError,
)
from htdt.cad_repository import SceneRepository

TS = '2026-10-05T00:00:00+00:00'
TS2 = '2026-10-05T00:10:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64


# ---------------------------------------------------------------------------
# AVL — #582 A/V latency authority
# ---------------------------------------------------------------------------


def _av_path(**kwargs):
    params = dict(
        path_id='avp-main',
        version='v1',
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        audio_stages=(
            AVLatencyStage(
                stage_id='avr-dsp',
                role='audio',
                component_kind='filter_implementation_delay',
                device_ref='avr-1',
                nominal_latency_seconds=0.080,
                evidence_class='derived_from_configuration',
            ),
        ),
        video_stages=(
            AVLatencyStage(
                stage_id='tv-scanout',
                role='video',
                component_kind='display_scanout_emission',
                device_ref='disp-1',
                measured_latency_seconds=0.020,
                evidence_class='electrically_measured',
            ),
        ),
    )
    params.update(kwargs)
    return build_av_latency_path(**params)


def test_avl10_profiles_seal_and_bounds() -> None:
    itu = build_itu_bt1359_profile()
    assert itu.profile_id.startswith('avlatprof:')
    # BT.1359 sound-advance-positive bounds converted to
    # audio_late_positive: lead 45ms / lag 125ms detectable,
    # 90ms/185ms acceptable.
    assert itu.detectability_min_seconds == pytest.approx(-0.045)
    assert itu.detectability_max_seconds == pytest.approx(0.125)
    assert itu.acceptability_min_seconds == pytest.approx(-0.090)
    assert itu.acceptability_max_seconds == pytest.approx(0.185)
    atsc = build_atsc_is191_profile()
    assert atsc.acceptability_min_seconds == pytest.approx(-0.015)
    assert atsc.acceptability_max_seconds == pytest.approx(0.045)


def test_avl20_composite_fail_closed() -> None:
    path = _av_path(
        audio_stages=(
            AVLatencyStage(
                stage_id='avr-dsp',
                role='audio',
                component_kind='filter_implementation_delay',
                nominal_latency_seconds=0.080,
                evidence_class='derived_from_configuration',
            ),
            AVLatencyStage(
                stage_id='avr-buffer',
                role='audio',
                component_kind='buffering',
                # no latency figure -> UNKNOWN
            ),
        )
    )
    assert path.relative_offset_seconds is None
    assert path.composite_latency_seconds('video') is not None


def test_avl30_stage_guards() -> None:
    with pytest.raises(ValueError, match='evidence'):
        AVLatencyStage(
            stage_id='x',
            role='audio',
            component_kind='device_processing',
            nominal_latency_seconds=0.01,
            evidence_class='unknown',
        )
    with pytest.raises(ValueError, match='min exceeds max'):
        AVLatencyStage(
            stage_id='x',
            role='audio',
            component_kind='buffering',
            latency_min_seconds=0.02,
            latency_max_seconds=0.01,
        )
    with pytest.raises(ValueError):
        AVLatencyCompensationEntry(
            location='avr_audio_delay',
            requested_seconds=0.05,
            applied_seconds=0.09,
            range_min_seconds=0.0,
            range_max_seconds=0.06,
        )


def _measured_offset(path, offset=0.062):
    return build_av_latency_path_measurement(
        document_id=path.document_id,
        path_id=path.path_id,
        path_version=path.version,
        path_sha256=path.path_sha256,
        method='acoustic_optical_measurement',
        method_version='avsync-flash-1',
        stimulus_asset_ref='assets/avsync.mp4',
        stimulus_sha256=SHA_A,
        observations=(
            AVLatencyObservation(
                observed_at_utc=TS, offset_seconds=offset - 0.002
            ),
            AVLatencyObservation(
                observed_at_utc=TS2, offset_seconds=offset + 0.002
            ),
        ),
        mean_offset_seconds=offset,
        min_offset_seconds=offset - 0.002,
        max_offset_seconds=offset + 0.002,
        measured_at_utc=TS2,
    )


def test_avl40_verdict_lifecycle() -> None:
    profile = build_itu_bt1359_profile()
    path = _av_path()
    assert path.relative_offset_seconds == pytest.approx(0.060)

    # no measurement -> insufficient evidence, never a verdict
    q0 = evaluate_av_latency_path(path=path, profile=profile, measurement=None)
    assert q0.verdict == 'insufficient_evidence'

    measurement = _measured_offset(path)
    q1 = evaluate_av_latency_path(
        path=path, profile=profile, measurement=measurement
    )
    assert q1.verdict == 'qualified_within_profile'
    assert q1.residual_offset_seconds == pytest.approx(0.062)
    assert q1.measurement_sha256 == measurement.measurement_sha256

    # an offset beyond acceptability -> exceeds_profile_bounds
    beyond = _measured_offset(path, offset=0.250)
    q2 = evaluate_av_latency_path(
        path=path, profile=profile, measurement=beyond
    )
    assert q2.verdict == 'exceeds_profile_bounds'

    # ATSC bound is tighter: 62ms lag exceeds +45ms
    q3 = evaluate_av_latency_path(
        path=path, profile=build_atsc_is191_profile(),
        measurement=measurement,
    )
    assert q3.verdict == 'exceeds_profile_bounds'


def test_avl50_manual_estimate_cannot_qualify() -> None:
    profile = build_itu_bt1359_profile()
    path = _av_path()
    manual = build_av_latency_path_measurement(
        document_id=path.document_id,
        path_id=path.path_id,
        path_version=path.version,
        path_sha256=path.path_sha256,
        method='manual_estimate',
        observations=(
            AVLatencyObservation(
                observed_at_utc=TS, offset_seconds=0.05
            ),
        ),
        mean_offset_seconds=0.05,
        min_offset_seconds=0.05,
        max_offset_seconds=0.05,
        measured_at_utc=TS,
    )
    q = evaluate_av_latency_path(
        path=path, profile=profile, measurement=manual
    )
    assert q.verdict == 'insufficient_evidence'


def test_avl60_lip_is_not_measured_and_mismatch_is_flagged() -> None:
    path = _av_path()
    # LIP method cannot mint observations at all
    with pytest.raises(ValueError, match='LIP'):
        build_av_latency_path_measurement(
            document_id=path.document_id,
            path_id=path.path_id,
            path_version=path.version,
            path_sha256=path.path_sha256,
            method='lip_protocol_reported',
            lip_evidence_ref='lip-ev-1',
            observations=(
                AVLatencyObservation(
                    observed_at_utc=TS, offset_seconds=0.05
                ),
            ),
            mean_offset_seconds=0.05,
            measured_at_utc=TS,
        )
    measured = _measured_offset(path)
    assert measured.protocol_physical_mismatch is None
    with_lip = build_av_latency_path_measurement(
        document_id=path.document_id,
        path_id=path.path_id,
        path_version=path.version,
        path_sha256=path.path_sha256,
        method='acoustic_optical_measurement',
        stimulus_asset_ref='assets/avsync.mp4',
        stimulus_sha256=SHA_A,
        observations=measured.observations,
        mean_offset_seconds=0.062,
        min_offset_seconds=0.060,
        max_offset_seconds=0.064,
        uncertainty_seconds=0.002,
        lip_reported_offset_seconds=0.200,
        lip_evidence_ref='lip-ev-1',
        measured_at_utc=TS2,
    )
    assert with_lip.protocol_physical_mismatch is True


def test_avl70_stale_on_path_revision() -> None:
    profile = build_itu_bt1359_profile()
    path = _av_path()
    measurement = _measured_offset(path)
    # any mode/route change rehashes the path -> old measurement stale
    changed = _av_path(version='v2', display_picture_mode='game')
    assert changed.path_sha256 != path.path_sha256
    q = evaluate_av_latency_path(
        path=changed, profile=profile, measurement=measurement
    )
    assert q.verdict == 'stale'

    # compensation insertion points exclude speaker-distance trims by
    # construction (literal): compensation math never double counts
    compensated = _av_path(
        compensations=(
            AVLatencyCompensationEntry(
                location='avr_audio_delay',
                requested_seconds=0.05,
                applied_seconds=0.05,
            ),
        )
    )
    assert compensated.applied_compensation_seconds == pytest.approx(
        0.05
    )


def test_avl_repository_round_trip(tmp_path: Path) -> None:
    repository = CadAVLatencyRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = build_itu_bt1359_profile()
    repository.save_profile(profile)
    path = _av_path()
    repository.save_path(path)
    measurement = _measured_offset(path)
    repository.save_measurement(measurement)
    qualification = evaluate_av_latency_path(
        path=path, profile=profile, measurement=measurement
    )
    repository.save_qualification(qualification)

    assert repository.get_path('avp-main', 'v1') == path
    assert repository.get_path_revision(
        'avp-main', path.path_sha256
    ) == path
    assert (
        repository.get_measurement(measurement.measurement_id)
        == measurement
    )
    assert (
        repository.get_qualification(qualification.qualification_id)
        == qualification
    )
    assert len(repository.list_paths('doc-1')) == 1

    # append-only: a different payload under the same id conflicts
    changed = _av_path(display_picture_mode='game')
    with pytest.raises(AVLatencyConflictError):
        repository.save_path(changed)
    # idempotent resave is a no-op
    repository.save_path(path)
    repository.save_measurement(measurement)


# ---------------------------------------------------------------------------
# HDMI — #583 HDMI design & verification authority
# ---------------------------------------------------------------------------


def _hdmi_profile():
    return build_hdmi_signal_profile(
        document_id='doc-1',
        label='4K120 HDR10 VRR',
        width_px=3840,
        height_px=2160,
        refresh_hz=120.0,
        hdr_format='hdr10',
        required_bandwidth_gbps=40.0,
        required_features=('vrr', 'allm', 'earc'),
        requires_earc=True,
        hdcp_required='hdcp_2_3',
    )


def _hdmi_record(profile, verdict='pass_stable', **kwargs):
    params = dict(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        required_profile_id=profile.profile_id,
        required_profile_sha256=profile.profile_sha256,
        verdict=verdict,
        observed_at_utc=TS2,
    )
    params.update(kwargs)
    return build_hdmi_verification_record(**params)


def test_hdmi10_signal_profile_seal_and_earc_consistency() -> None:
    profile = _hdmi_profile()
    assert profile.profile_id.startswith('hdmiprof:')
    with pytest.raises(ValueError, match='earc'):
        build_hdmi_signal_profile(
            document_id='doc-1',
            label='bad',
            width_px=3840,
            height_px=2160,
            refresh_hz=60.0,
            requires_earc=True,
            required_features=(),
        )


def test_hdmi20_edid_artifact_parser_versioned() -> None:
    artifact = build_edid_artifact(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        raw_sha256=SHA_A,
        capture_method='edid_readback',
        interception_kind='synthesized',
        parser_id='htdt-edid',
        parser_version='1.0',
        interpreted_fields=('cta:vsdb:max_tmds=600',),
        captured_at_utc=TS,
    )
    assert artifact.interception_kind == 'synthesized'
    with pytest.raises(ValueError, match='parser'):
        build_edid_artifact(
            document_id='doc-1',
            signal_path_id='sp-1',
            signal_path_version='v1',
            signal_path_sha256=SHA_B,
            raw_sha256=SHA_A,
            interpreted_fields=('x',),
            captured_at_utc=TS,
        )


def test_hdmi30_hdcp_observation_honest() -> None:
    obs = build_hdcp_observation(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        negotiated_version='hdcp_2_3',
        auth_state='authenticated',
        observed_at_utc=TS,
    )
    assert obs.auth_state == 'authenticated'
    with pytest.raises(ValueError, match='version'):
        build_hdcp_observation(
            document_id='doc-1',
            signal_path_id='sp-1',
            signal_path_version='v1',
            signal_path_sha256=SHA_B,
            negotiated_version='unknown',
            auth_state='authenticated',
            observed_at_utc=TS,
        )


def test_hdmi40_link_observation_rate_evidence_guard() -> None:
    obs = build_link_observation(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        link_mode='frl',
        negotiated_rate_gbps=48.0,
        rate_evidence_class='measured',
        tmds_error_count=0,
        observed_at_utc=TS,
    )
    assert obs.link_mode == 'frl'
    with pytest.raises(ValueError, match='evidence'):
        build_link_observation(
            document_id='doc-1',
            signal_path_id='sp-1',
            signal_path_version='v1',
            signal_path_sha256=SHA_B,
            negotiated_rate_gbps=48.0,
            rate_evidence_class='marketing',
            observed_at_utc=TS,
        )


def test_hdmi50_verification_record_guards() -> None:
    profile = _hdmi_profile()
    record = _hdmi_record(
        profile,
        hop_verifications=(
            HopVerification(edge_id='e1', status='verified'),
        ),
        stress_scenarios=('sustained_playback',),
        stress_duration_seconds=7200.0,
        bypass_tests=(
            DiagnosticBypassTest(
                bypass_label='direct source->display',
                removed_elements=('avr',),
                result='worked',
            ),
        ),
    )
    assert record.verdict == 'pass_stable'
    with pytest.raises(ValueError, match='failure_reason'):
        _hdmi_record(profile, verdict='fail_link')
    with pytest.raises(ValueError, match='limitation'):
        _hdmi_record(profile, verdict='pass_with_limitations')
    with pytest.raises(ValueError, match='stable pass'):
        _hdmi_record(
            profile,
            verdict='pass_stable',
            failure_reasons=('cable',),
        )


def test_hdmi60_qualification_ladder() -> None:
    profile = _hdmi_profile()
    record = _hdmi_record(profile)
    q = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v1',
        path_sha256=SHA_B,
        profile=profile,
        theoretical_status='supported',
        record=record,
    )
    assert q.verdict == 'verified'

    # theoretical support without field evidence is not verification
    q2 = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v1',
        path_sha256=SHA_B,
        profile=profile,
        theoretical_status='supported',
        record=None,
    )
    assert q2.verdict == 'theoretically_supported_unverified'

    # unsupported theoretical -> failed regardless
    q3 = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v1',
        path_sha256=SHA_B,
        profile=profile,
        theoretical_status='unsupported',
        record=record,
    )
    assert q3.verdict == 'failed'

    # record bound to an old path revision -> stale
    q4 = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v2',
        path_sha256='c' * 64,
        profile=profile,
        theoretical_status='supported',
        record=record,
    )
    assert q4.verdict == 'stale'

    # intermittent record -> failed with reasons preserved
    flapping = _hdmi_record(
        profile,
        verdict='intermittent',
        failure_reasons=('intermittent', 'cable'),
        stress_scenarios=('source_switching',),
    )
    q5 = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v1',
        path_sha256=SHA_B,
        profile=profile,
        theoretical_status='supported',
        record=flapping,
    )
    assert q5.verdict == 'failed'
    assert 'intermittent' in q5.reasons[0]


def test_hdmi70_rp28_profile_honest_unpopulated() -> None:
    profile = seed_rp28_profile()
    assert profile.profile_id.startswith('rp28:')
    assert profile.standard_id == 'cedia-cta-rp28'
    assert all(
        d.mapping_status == 'unpopulated_pending_lawful_source'
        for d in profile.requirement_domains
    )


def test_hdmi_repository_round_trip(tmp_path: Path) -> None:
    repository = CadHDMIVerificationRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _hdmi_profile()
    repository.save_signal_profile(profile)
    artifact = build_edid_artifact(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        raw_sha256=SHA_A,
        captured_at_utc=TS,
    )
    repository.save_edid_artifact(artifact)
    hdcp = build_hdcp_observation(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        negotiated_version='hdcp_2_3',
        auth_state='authenticated',
        observed_at_utc=TS,
    )
    repository.save_hdcp_observation(hdcp)
    link = build_link_observation(
        document_id='doc-1',
        signal_path_id='sp-1',
        signal_path_version='v1',
        signal_path_sha256=SHA_B,
        link_mode='frl',
        negotiated_rate_gbps=48.0,
        rate_evidence_class='measured',
        observed_at_utc=TS,
    )
    repository.save_link_observation(link)
    record = _hdmi_record(profile)
    repository.save_verification_record(record)
    qualification = evaluate_hdmi_qualification(
        document_id='doc-1',
        path_id='sp-1',
        path_version='v1',
        path_sha256=SHA_B,
        profile=profile,
        theoretical_status='supported',
        record=record,
    )
    repository.save_qualification(qualification)
    rp28 = seed_rp28_profile()
    repository.save_rp28_profile(rp28)

    assert repository.get_signal_profile(profile.profile_id) == profile
    assert repository.get_edid_artifact(artifact.artifact_id) == artifact
    assert (
        repository.get_verification_record(record.record_id) == record
    )
    assert (
        repository.get_qualification(qualification.qualification_id)
        == qualification
    )
    assert repository.get_rp28_profile(rp28.profile_id) == rp28

    # orphan verification record rejected (unknown profile)
    orphan_repo = CadHDMIVerificationRepository(
        SceneRepository(tmp_path / 'o.sqlite3')
    )
    with pytest.raises(Exception, match='persisted'):
        orphan_repo.save_verification_record(record)
    # identical resave is a no-op (sha-derived ids can't conflict — a
    # different payload is a different identity by construction)
    repository.save_signal_profile(
        build_hdmi_signal_profile(
            document_id='doc-1',
            label='4K120 HDR10 VRR',
            width_px=3840,
            height_px=2160,
            refresh_hz=120.0,
            hdr_format='hdr10',
            required_bandwidth_gbps=40.0,
            required_features=('vrr', 'allm', 'earc'),
            requires_earc=True,
            hdcp_required='hdcp_2_3',
        )
    )


# ---------------------------------------------------------------------------
# NAV — #591 networked AV qualification authority
# ---------------------------------------------------------------------------


def _net_path(**kwargs):
    params = dict(
        path_id='net-1',
        version='v1',
        document_id='doc-1',
        nodes=(
            NetworkNodeDecl(
                node_id='tx',
                kind='endpoint',
                ports=(
                    NetworkPortDecl(
                        port_id='p1', speed_gbps=1.0,
                        medium='copper', vlan_ids=(10,),
                    ),
                ),
            ),
            NetworkNodeDecl(
                node_id='sw',
                kind='switch',
                ports=(
                    NetworkPortDecl(port_id='p1', speed_gbps=1.0),
                    NetworkPortDecl(port_id='p2', speed_gbps=10.0),
                ),
            ),
            NetworkNodeDecl(
                node_id='rx',
                kind='endpoint',
                ports=(
                    NetworkPortDecl(port_id='p1', speed_gbps=1.0),
                ),
            ),
        ),
        links=(
            NetworkLinkDecl(
                link_id='l1', from_node_id='tx', from_port_id='p1',
                to_node_id='sw', to_port_id='p1',
                nominal_capacity_gbps=1.0,
            ),
            NetworkLinkDecl(
                link_id='l2', from_node_id='sw', from_port_id='p2',
                to_node_id='rx', to_port_id='p1',
                nominal_capacity_gbps=10.0, is_uplink=True,
            ),
        ),
    )
    params.update(kwargs)
    return build_network_av_path(**params)


def _flow(path, **kwargs):
    params = dict(
        document_id='doc-1',
        path_id=path.path_id,
        path_version=path.version,
        path_sha256=path.path_sha256,
        provider_profile='aes67',
        source_node_id='tx',
        sink_node_ids=('rx',),
        delivery='unicast',
        audio_channels=8,
        sample_rate_hz=48000.0,
        required_bitrate_mbps=10.0,
        clock_requirement='ptp_required',
        hop_link_ids=('l1', 'l2'),
    )
    params.update(kwargs)
    return build_network_media_flow(**params)


def test_nav10_topology_validates_refs() -> None:
    path = _net_path()
    assert path.path_sha256
    with pytest.raises(ValueError, match='unknown'):
        build_network_av_path(
            path_id='bad',
            version='v1',
            document_id='doc-1',
            nodes=(
                NetworkNodeDecl(
                    node_id='n', kind='switch',
                    ports=(NetworkPortDecl(port_id='x'),),
                ),
            ),
            links=(
                NetworkLinkDecl(
                    link_id='l', from_node_id='n', from_port_id='zz',
                    to_node_id='n', to_port_id='x',
                ),
            ),
        )
    with pytest.raises(ValueError, match='duplicate'):
        build_network_av_path(
            path_id='bad',
            version='v1',
            document_id='doc-1',
            nodes=(
                NetworkNodeDecl(
                    node_id='n', kind='switch',
                    ports=(
                        NetworkPortDecl(port_id='x'),
                        NetworkPortDecl(port_id='x'),
                    ),
                ),
            ),
        )


def test_nav20_flow_binding_guards() -> None:
    path = _net_path()
    flow = _flow(path)
    assert flow.flow_id.startswith('netflow:')
    with pytest.raises(ValueError, match='multicast_group'):
        build_network_media_flow(
            document_id='doc-1',
            path_id=path.path_id,
            path_version=path.version,
            path_sha256=path.path_sha256,
            provider_profile='dante',
            source_node_id='tx',
            sink_node_ids=('rx',),
            delivery='multicast',
        )
    with pytest.raises(ValueError, match='multicast_group'):
        build_network_media_flow(
            document_id='doc-1',
            path_id=path.path_id,
            path_version=path.version,
            path_sha256=path.path_sha256,
            provider_profile='dante',
            source_node_id='tx',
            sink_node_ids=('rx',),
            delivery='unicast',
            multicast_group='239.1.1.1',
        )


def test_nav30_observation_kind_guards() -> None:
    path = _net_path()
    with pytest.raises(ValueError, match='qos_evidence_level'):
        build_network_transport_observation(
            document_id='doc-1', path_id='net-1', path_version='v1',
            path_sha256=path.path_sha256, kind='qos_state',
            observed_at_utc=TS,
        )
    with pytest.raises(ValueError, match='duration'):
        build_network_transport_observation(
            document_id='doc-1', path_id='net-1', path_version='v1',
            path_sha256=path.path_sha256, kind='stress_soak',
            observed_at_utc=TS,
        )
    with pytest.raises(ValueError, match='units_note'):
        build_network_timing_observation(
            document_id='doc-1', path_id='net-1', path_version='v1',
            path_sha256=path.path_sha256,
            offset_from_leader_seconds=0.000001,
            observed_at_utc=TS,
        )


def test_nav40_qualification_full_evidence() -> None:
    path = _net_path()
    flow = _flow(path)
    pq = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='packet_quality',
        flow_id=flow.flow_id, interface_ref='sw:p1',
        packets_total=1_000_000, packets_lost=0, loss_ratio=0.0,
        observed_at_utc=TS,
    )
    qos = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='qos_state',
        qos_evidence_level='end_to_end_behavior_verified',
        observed_at_utc=TS,
    )
    ptp = build_network_timing_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, ptp_domain=0,
        ptp_profile='aes67', node_ref='rx:p1',
        node_state='follower', lock_state='locked',
        observed_at_utc=TS,
    )
    q = evaluate_network_av_qualification(
        path=path, flow=flow,
        transport_observations=(pq, qos),
        timing_observations=(ptp,),
    )
    assert q.verdict == 'qualified'
    assert q.media_state == 'media_stream_qualified'
    assert dict(q.checks)['ptp_timing'] == 'verified'
    assert q.evidence_refs


def test_nav50_no_evidence_is_insufficient_not_qualified() -> None:
    path = _net_path()
    flow = _flow(path)
    q = evaluate_network_av_qualification(path=path, flow=flow)
    assert q.verdict == 'insufficient_evidence'
    assert q.media_state == 'discovered'


def test_nav60_multicast_requires_igmp_evidence() -> None:
    path = _net_path()
    flow = _flow(
        path,
        provider_profile='dante',
        delivery='multicast',
        multicast_group='239.69.1.1',
        clock_requirement='none',
    )
    pq = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='packet_quality',
        packets_total=100, packets_lost=0, observed_at_utc=TS,
    )
    q = evaluate_network_av_qualification(
        path=path, flow=flow, transport_observations=(pq,)
    )
    assert dict(q.checks)['multicast'] == 'not_verified'

    flood = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='multicast_state',
        igmp_querier_present=True, igmp_snooping_enabled=True,
        flooding_observed=True, observed_at_utc=TS,
    )
    q2 = evaluate_network_av_qualification(
        path=path, flow=flow, transport_observations=(pq, flood)
    )
    assert dict(q2.checks)['multicast'] == 'not_verified'
    assert 'flooding' in ' '.join(q2.reasons)


def test_nav70_ptp_and_redundancy_fail_closed() -> None:
    path = _net_path()
    flow = _flow(path, requires_redundancy=True)
    pq = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='packet_quality',
        packets_total=100, packets_lost=0, observed_at_utc=TS,
    )
    qos = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='qos_state',
        qos_evidence_level='end_to_end_behavior_verified',
        observed_at_utc=TS,
    )
    # PTP required but no lock + redundancy required but no event
    q = evaluate_network_av_qualification(
        path=path, flow=flow, transport_observations=(pq, qos)
    )
    checks = dict(q.checks)
    assert checks['ptp_timing'] == 'not_verified'
    assert checks['redundancy'] == 'not_verified'
    assert q.verdict == 'failed'

    ptp = build_network_timing_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, lock_state='locked',
        node_state='follower', observed_at_utc=TS,
    )
    failover = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='redundancy_event',
        redundancy_event='primary link pulled', failover_detected=True,
        failover_recovery_seconds=0.0,
        media_interruption_observed=False,
        observed_at_utc=TS,
    )
    q2 = evaluate_network_av_qualification(
        path=path, flow=flow,
        transport_observations=(pq, qos, failover),
        timing_observations=(ptp,),
    )
    assert q2.verdict == 'qualified'


def test_nav80_stale_on_topology_revision() -> None:
    path = _net_path()
    flow = _flow(path)
    changed = _net_path(version='v2', label='rewired')
    q = evaluate_network_av_qualification(
        path=changed, flow=flow
    )
    assert q.verdict == 'stale'
    assert q.media_state == 'not_evaluated'


def test_net_repository_round_trip(tmp_path: Path) -> None:
    repository = CadNetworkAVRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    path = _net_path()
    repository.save_path(path)
    flow = _flow(path)
    repository.save_flow(flow)
    pq = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='packet_quality',
        flow_id=flow.flow_id, packets_total=10, packets_lost=0,
        observed_at_utc=TS,
    )
    repository.save_transport_observation(pq)
    ptp = build_network_timing_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, lock_state='locked',
        node_state='follower', observed_at_utc=TS,
    )
    repository.save_timing_observation(ptp)
    qos = build_network_transport_observation(
        document_id='doc-1', path_id='net-1', path_version='v1',
        path_sha256=path.path_sha256, kind='qos_state',
        qos_evidence_level='end_to_end_behavior_verified',
        observed_at_utc=TS,
    )
    repository.save_transport_observation(qos)
    q = evaluate_network_av_qualification(
        path=path, flow=flow,
        transport_observations=(pq, qos),
        timing_observations=(ptp,),
    )
    repository.save_qualification(q)

    assert repository.get_path('net-1', 'v1') == path
    assert repository.get_flow(flow.flow_id) == flow
    assert (
        repository.get_transport_observation(pq.observation_id) == pq
    )
    assert (
        repository.get_timing_observation(ptp.observation_id) == ptp
    )
    assert repository.get_qualification(q.qualification_id) == q
    assert len(
        repository.list_transport_observations('net-1', 'v1')
    ) == 2

    # flow bound to an unpersisted path revision is rejected
    orphan_repo = CadNetworkAVRepository(
        SceneRepository(tmp_path / 'o.sqlite3')
    )
    with pytest.raises(NetworkAVIntegrityError):
        orphan_repo.save_flow(flow)
    # append-only conflict: same path_id+version, different payload
    with pytest.raises(NetworkAVConflictError):
        repository.save_path(
            _net_path(label='rewired')
        )
    repository.save_path(path)  # idempotent no-op
