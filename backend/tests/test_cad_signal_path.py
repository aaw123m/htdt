"""A/V signal-path compatibility tests (#570)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_signal_path import (
    AudioCapabilitySet,
    AVSignalPath,
    MediaPlaybackCondition,
    NegotiatedModeEvidence,
    SignalPathEdge,
    SignalPathNode,
    SignalPort,
    VideoCapabilitySet,
    build_av_signal_path,
    evaluate_signal_path,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='vendor docs',
            source_version='1',
            source_reference='spec sheets',
            source_sha256='2' * 64,
        ),
    )


HDMI21_VIDEO = VideoCapabilitySet(
    spec_label='hdmi_2_1',
    max_width_px=3840,
    max_height_px=2160,
    max_refresh_hz=120.0,
    chroma_subsampling=('444', '422', '420'),
    bit_depths=(8, 10, 12),
    hdr_formats=('hdr10', 'dolby_vision'),
    vrr=True,
    bandwidth_gbps=48.0,
)

HDMI20_VIDEO = VideoCapabilitySet(
    spec_label='hdmi_2_0',
    max_width_px=3840,
    max_height_px=2160,
    max_refresh_hz=60.0,
    chroma_subsampling=('444', '422', '420'),
    bit_depths=(8, 10, 12),
    hdr_formats=('hdr10',),
    vrr=False,
    bandwidth_gbps=18.0,
)

EARC_AUDIO = AudioCapabilitySet(
    spec_label='earc',
    max_pcm_channels=8,
    pcm_formats=('lpcm',),
    bitstream_families=('dolby_truehd', 'dts_hd_ma', 'dolby_atmos'),
    earc=True,
    arc=True,
)


def _path(video_cap=HDMI21_VIDEO, cable_cap=HDMI21_VIDEO):
    source = SignalPathNode(
        node_id='src',
        kind='source',
        label='AppleTV',
        ports=(
            SignalPort(
                port_id='out1',
                direction='out',
                medium='hdmi',
                video=video_cap,
                audio=AudioCapabilitySet(
                    max_pcm_channels=8,
                    pcm_formats=('lpcm',),
                    bitstream_families=('dolby_atmos',),
                ),
            ),
        ),
    )
    avr = SignalPathNode(
        node_id='avr',
        kind='avr',
        label='AVR',
        ports=(
            SignalPort(
                port_id='in1', direction='in', medium='hdmi',
                video=video_cap,
                audio=AudioCapabilitySet(
                    max_pcm_channels=8,
                    pcm_formats=('lpcm',),
                    bitstream_families=('dolby_atmos', 'dts_x'),
                ),
            ),
            SignalPort(
                port_id='out1', direction='out', medium='hdmi',
                video=video_cap,
                audio=AudioCapabilitySet(max_pcm_channels=8),
            ),
        ),
    )
    display = SignalPathNode(
        node_id='disp',
        kind='display',
        label='OLED',
        ports=(
            SignalPort(
                port_id='hdmi2', direction='in', medium='hdmi',
                video=video_cap,
                audio=AudioCapabilitySet(
                    max_pcm_channels=2, pcm_formats=('lpcm',)
                ),
            ),
            SignalPort(
                port_id='earc1', direction='bidirectional',
                medium='earc', audio=EARC_AUDIO,
            ),
        ),
    )
    return build_av_signal_path(
        path_id='path-atv-avr-tv',
        version='1',
        nodes=(source, avr, display),
        edges=(
            SignalPathEdge(
                edge_id='e1',
                from_node_id='src', from_port_id='out1',
                to_node_id='avr', to_port_id='in1',
                medium='hdmi',
                cable_capability=cable_cap,
                cable_audio_capability=AudioCapabilitySet(
                    spec_label='hdmi-audio',
                    max_pcm_channels=8,
                    pcm_formats=('lpcm',),
                    bitstream_families=('dolby_atmos', 'dts_x'),
                ),
                cable_run_ref='cable-run-3',
            ),
            SignalPathEdge(
                edge_id='e2',
                from_node_id='avr', from_port_id='out1',
                to_node_id='disp', to_port_id='hdmi2',
                medium='hdmi',
                cable_capability=cable_cap,
                cable_audio_capability=AudioCapabilitySet(
                    spec_label='hdmi-audio',
                    max_pcm_channels=8,
                    pcm_formats=('lpcm',),
                ),
            ),
        ),
        provenance=_provenance(),
    )


def _condition(**overrides):
    kwargs = dict(
        width_px=3840, height_px=2160, refresh_hz=120.0,
        chroma_subsampling='422', bit_depth=10,
        hdr_format='dolby_vision', vrr=True,
        audio_format='dolby_atmos', audio_channels=6,
    )
    kwargs.update(overrides)
    return MediaPlaybackCondition(**kwargs)


def test_supported_path():
    evaluation = evaluate_signal_path(
        path=_path(), condition=_condition()
    )
    assert evaluation.status == 'SUPPORTED'
    assert evaluation.limiting_component is None
    assert evaluation.evaluation_id.startswith('spe-')


def test_unsupported_names_limiting_component():
    # cable rated HDMI 2.0 — refresh 120 Hz exceeds it
    evaluation = evaluate_signal_path(
        path=_path(cable_cap=HDMI20_VIDEO),
        condition=_condition(),
    )
    assert evaluation.status == 'UNSUPPORTED'
    assert evaluation.limiting_component == 'cable:e1'


def test_unknown_when_capability_absent():
    path = _path(cable_cap=None)
    # rebuild edges without cable capability
    edges = tuple(
        e.model_copy(update={'cable_capability': None})
        for e in path.edges
    )
    path = build_av_signal_path(
        path_id=path.path_id, version=path.version,
        nodes=path.nodes, edges=edges,
    )
    evaluation = evaluate_signal_path(path=path, condition=_condition())
    assert evaluation.status == 'UNKNOWN'


def test_negotiated_evidence_upgrades_and_binds():
    path = _path()
    evidence = NegotiatedModeEvidence(
        evidence_id='neg-1',
        path_id=path.path_id,
        path_sha256=path.path_sha256,
        observed_at_utc='2026-09-23T04:00:00+00:00',
        method='link_training',
        worked=True,
        negotiated=_condition(),
    )
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=evidence
    )
    assert evaluation.status == 'VERIFIED_WORKING'

    failed = NegotiatedModeEvidence(
        evidence_id='neg-2',
        path_id=path.path_id,
        path_sha256=path.path_sha256,
        observed_at_utc='2026-09-23T04:05:00+00:00',
        method='osd_status',
        worked=False,
        note='fell back to 60Hz',
    )
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=failed
    )
    assert evaluation.status == 'VERIFIED_FAILURE'

    stale = evidence.model_copy(update={'path_sha256': '0' * 64})
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=stale
    )
    assert evaluation.status == 'UNKNOWN'


def test_missing_port_rejected():
    with pytest.raises(ValueError, match='missing port'):
        build_av_signal_path(
            path_id='bad',
            version='1',
            nodes=(
                SignalPathNode(
                    node_id='a', kind='source',
                    ports=(
                        SignalPort(
                            port_id='o', direction='out',
                            medium='hdmi',
                        ),
                    ),
                ),
                SignalPathNode(node_id='b', kind='display', ports=()),
            ),
            edges=(
                SignalPathEdge(
                    edge_id='e', from_node_id='a', from_port_id='o',
                    to_node_id='b', to_port_id='nope',
                ),
            ),
        )


def test_earc_direction_is_legitimate_source():
    path = _path()
    earc_port = next(
        p for n in path.nodes for p in n.ports if p.medium == 'earc'
    )
    assert earc_port.direction == 'bidirectional'
    assert earc_port.audio.earc is True


def test_no_screen_node_kind():
    # 'screen' is not a valid SignalNodeKind — passive surfaces are never
    # signal endpoints; pydantic enforces at construction.
    with pytest.raises(ValidationError):
        SignalPathNode(node_id='scr', kind='screen', ports=())


def test_direction_violation_fails():
    path = _path()
    bad_edges = path.edges[:1] + (
        SignalPathEdge(
            edge_id='e2-rev',
            from_node_id='avr', from_port_id='in1',   # in port as source
            to_node_id='disp', to_port_id='hdmi2',
            medium='hdmi',
            cable_capability=HDMI21_VIDEO,
        ),
    )
    path = build_av_signal_path(
        path_id='p2', version='1',
        nodes=path.nodes, edges=bad_edges,
    )
    evaluation = evaluate_signal_path(path=path, condition=_condition())
    assert evaluation.status == 'UNSUPPORTED'
    assert evaluation.limiting_component == 'e2-rev:direction'


def _nodes(*specs):
    return tuple(
        SignalPathNode(
            node_id=node_id,
            kind=kind,
            ports=tuple(
                SignalPort(
                    port_id=port_id,
                    direction=direction,
                    medium=medium,
                    video=HDMI21_VIDEO,
                    audio=EARC_AUDIO,
                )
                for port_id, direction, medium in ports
            ),
        )
        for node_id, kind, ports in specs
    )


def test_disconnected_edge_bag_rejected():
    # two individually valid HDMI edges that do not form one route
    nodes = _nodes(
        ('src-a', 'source', (('o', 'out', 'hdmi'),)),
        ('disp-a', 'display', (('i', 'in', 'hdmi'),)),
        ('src-b', 'source', (('o', 'out', 'hdmi'),)),
        ('disp-b', 'display', (('i', 'in', 'hdmi'),)),
    )
    with pytest.raises(ValueError, match='unique start and end'):
        build_av_signal_path(
            path_id='bag', version='1', nodes=nodes,
            edges=(
                SignalPathEdge(
                    edge_id='e1', from_node_id='src-a', from_port_id='o',
                    to_node_id='disp-a', to_port_id='i', medium='hdmi',
                ),
                SignalPathEdge(
                    edge_id='e2', from_node_id='src-b', from_port_id='o',
                    to_node_id='disp-b', to_port_id='i', medium='hdmi',
                ),
            ),
        )


def test_cycle_rejected():
    nodes = _nodes(
        ('a', 'source', (('o', 'out', 'hdmi'), ('i', 'in', 'hdmi'))),
        ('b', 'switch', (('o', 'out', 'hdmi'), ('i', 'in', 'hdmi'))),
        ('c', 'switch', (('o', 'out', 'hdmi'), ('i', 'in', 'hdmi'))),
    )
    with pytest.raises(ValueError, match='unique start and end'):
        build_av_signal_path(
            path_id='cyc', version='1', nodes=nodes,
            edges=(
                SignalPathEdge(
                    edge_id='e1', from_node_id='a', from_port_id='o',
                    to_node_id='b', to_port_id='i', medium='hdmi',
                ),
                SignalPathEdge(
                    edge_id='e2', from_node_id='b', from_port_id='o',
                    to_node_id='c', to_port_id='i', medium='hdmi',
                ),
                SignalPathEdge(
                    edge_id='e3', from_node_id='c', from_port_id='o',
                    to_node_id='a', to_port_id='i', medium='hdmi',
                ),
            ),
        )


def test_medium_mismatch_requires_adapter_node():
    nodes = _nodes(
        ('src', 'source', (('o', 'out', 'hdmi'),)),
        ('amp', 'other', (('i', 'in', 'analog'),)),
    )
    with pytest.raises(ValueError, match='cannot mate'):
        build_av_signal_path(
            path_id='mismatch', version='1', nodes=nodes,
            edges=(
                SignalPathEdge(
                    edge_id='e1', from_node_id='src', from_port_id='o',
                    to_node_id='amp', to_port_id='i', medium='hdmi',
                ),
            ),
        )


def test_unknown_medium_is_not_implicit_compatibility():
    nodes = _nodes(
        ('src', 'source', (('o', 'out', 'hdmi'),)),
        ('disp', 'display', (('i', 'in', 'hdmi'),)),
    )
    path = build_av_signal_path(
        path_id='unknown-medium', version='1', nodes=nodes,
        edges=(
            SignalPathEdge(
                edge_id='e1', from_node_id='src', from_port_id='o',
                to_node_id='disp', to_port_id='i', medium='unknown',
                cable_capability=HDMI21_VIDEO,
            ),
        ),
    )
    evaluation = evaluate_signal_path(path=path, condition=_condition())
    assert evaluation.status == 'UNKNOWN'


def test_requested_height_unknown_when_max_height_absent():
    cap = VideoCapabilitySet(
        spec_label='partial',
        max_width_px=3840,
        max_height_px=None,
        max_refresh_hz=120.0,
        chroma_subsampling=('422',),
        bit_depths=(10,),
        hdr_formats=('hdr10',),
        vrr=True,
        bandwidth_gbps=48.0,
    )
    path = _path(video_cap=cap, cable_cap=cap)
    evaluation = evaluate_signal_path(path=path, condition=_condition())
    assert evaluation.status == 'UNKNOWN'


def test_negotiated_degraded_mode_is_not_requested_success():
    path = _path()
    negotiated = _condition(refresh_hz=60.0, hdr_format='hdr10')
    evidence = NegotiatedModeEvidence(
        evidence_id='neg-deg',
        path_id=path.path_id,
        path_sha256=path.path_sha256,
        observed_at_utc='2026-09-24T00:00:00+00:00',
        method='link_training',
        worked=True,
        negotiated=negotiated,
    )
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=evidence
    )
    assert evaluation.status == 'VERIFIED_DIFFERENT_MODE'


def test_negotiated_exact_mode_verifies():
    path = _path()
    evidence = NegotiatedModeEvidence(
        evidence_id='neg-exact',
        path_id=path.path_id,
        path_sha256=path.path_sha256,
        observed_at_utc='2026-09-24T00:00:00+00:00',
        method='link_training',
        worked=True,
        negotiated=_condition(),
    )
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=evidence
    )
    assert evaluation.status == 'VERIFIED_WORKING'
    # method/provenance stays visible on the result
    assert evaluation.negotiation.method == 'link_training'


def test_missing_negotiated_mode_cannot_over_verify():
    path = _path()
    evidence = NegotiatedModeEvidence(
        evidence_id='neg-nomode',
        path_id=path.path_id,
        path_sha256=path.path_sha256,
        observed_at_utc='2026-09-24T00:00:00+00:00',
        method='user_confirmed',
        worked=True,
        negotiated=None,
    )
    evaluation = evaluate_signal_path(
        path=path, condition=_condition(), negotiation=evidence
    )
    assert evaluation.status == 'UNKNOWN'


def test_forward_path_must_terminate_at_display():
    nodes = _nodes(
        ('src', 'source', (('o', 'out', 'hdmi'),)),
        ('avr', 'avr', (('i', 'in', 'hdmi'),)),
    )
    path = build_av_signal_path(
        path_id='ends-at-avr', version='1', nodes=nodes,
        edges=(
            SignalPathEdge(
                edge_id='e1', from_node_id='src', from_port_id='o',
                to_node_id='avr', to_port_id='i', medium='hdmi',
                cable_capability=HDMI21_VIDEO,
                cable_audio_capability=EARC_AUDIO,
            ),
        ),
    )
    evaluation = evaluate_signal_path(path=path, condition=_condition())
    assert evaluation.status == 'UNSUPPORTED'
    assert evaluation.limiting_component == 'route:endpoint'


def test_earc_return_route_accepted():
    nodes = _nodes(
        ('disp', 'display', (('earc1', 'bidirectional', 'earc'),)),
        ('avr', 'avr', (('earc1', 'bidirectional', 'earc'),)),
    )
    path = build_av_signal_path(
        path_id='earc-return', version='1', nodes=nodes,
        edges=(
            SignalPathEdge(
                edge_id='r1', from_node_id='disp', from_port_id='earc1',
                to_node_id='avr', to_port_id='earc1', medium='earc',
                cable_capability=HDMI21_VIDEO,
                cable_audio_capability=EARC_AUDIO,
            ),
        ),
    )
    evaluation = evaluate_signal_path(
        path=path,
        condition=MediaPlaybackCondition(
            audio_format='dolby_atmos', audio_channels=8,
        ),
    )
    assert evaluation.status == 'SUPPORTED'


def test_required_bandwidth_evaluated_exactly():
    path = _path(cable_cap=HDMI20_VIDEO)
    evaluation = evaluate_signal_path(
        path=path,
        condition=_condition(
            required_bandwidth_gbps=24.0,
            refresh_hz=None, hdr_format=None, vrr=None,
        ),
    )
    assert evaluation.status == 'UNSUPPORTED'
    assert evaluation.limiting_component == 'cable:e1'

    evaluation = evaluate_signal_path(
        path=path,
        condition=_condition(
            required_bandwidth_gbps=18.0,
            refresh_hz=None, hdr_format=None, vrr=None,
        ),
    )
    assert evaluation.status == 'SUPPORTED'
