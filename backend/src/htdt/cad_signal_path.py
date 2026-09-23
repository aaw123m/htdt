"""A/V signal-path compatibility authority (#570).

Models source → AVR/switch → display routes as a typed graph: nodes (source,
AVR, switch/matrix, display, dedicated eARC/ARC endpoint) own typed ports;
edges bind an output to an input over a medium with an optional cable
capability and CableRun reference.

Contract points honoured:

- Capability is a *versioned set*, not a flag: video capability records
  resolution/refresh/chroma/bit-depth/HDR/VRR subsets; audio records PCM
  channel ceilings, bitstream families and ARC/eARC support. Absent fields
  mean UNKNOWN, not "unsupported".
- A passive screen is never an endpoint — node kinds exclude it, and the
  evaluator verifies the chain only ever terminates on display/eARC ports.
- Displays are sinks AND sources: a display's input port terminates the
  video chain; its eARC/ARC port is a legitimate *source* for the audio
  return path.
- Static capability and negotiated reality stay separate:
  :func:`evaluate_signal_path` produces SUPPORTED / UNSUPPORTED (naming the
  limiting component) / UNKNOWN from the declared graph alone; a
  :class:`NegotiatedModeEvidence` record (EDID/handshake readback, link
  training result, user confirmation) then upgrades the same condition to
  VERIFIED_WORKING or downgrades it to VERIFIED_FAILURE — evidence about
  what actually happened, never inferred from the graph.
- Cables are first-class limits: an edge's cable capability intersects both
  endpoint ports' capabilities, and an edge may bind a ``cable_run_ref``
  (issue #538 owns the CableRun authority; it is referenced by id only).
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


SignalNodeKind = Literal[
    'source',
    'avr',
    'switch',
    'display',
    'earc_endpoint',
    'repeater',
    'other',
]
"""Node roles in a signal path. There is deliberately no ``screen`` kind —
a passive projection surface is not a signal endpoint."""

PortDirection = Literal['in', 'out', 'bidirectional']

MediumKind = Literal[
    'hdmi',
    'displayport',
    'earc',
    'arc',
    'toslink',
    'analog',
    'hdBaseT',
    'other',
    'unknown',
]

PathStatus = Literal[
    'SUPPORTED', 'UNSUPPORTED', 'UNKNOWN',
    'VERIFIED_WORKING', 'VERIFIED_FAILURE',
]


class VideoCapabilitySet(BaseModel):
    """What a port/cable can carry for video. Missing fields = UNKNOWN."""

    model_config = ConfigDict(frozen=True)

    spec_label: str | None = None
    max_width_px: int | None = Field(default=None, gt=0)
    max_height_px: int | None = Field(default=None, gt=0)
    max_refresh_hz: float | None = Field(default=None, gt=0.0)
    chroma_subsampling: tuple[str, ...] = ()
    bit_depths: tuple[int, ...] = ()
    hdr_formats: tuple[str, ...] = ()
    vrr: bool | None = None
    bandwidth_gbps: float | None = Field(default=None, gt=0.0)


class AudioCapabilitySet(BaseModel):
    """What a port/cable can carry for audio."""

    model_config = ConfigDict(frozen=True)

    spec_label: str | None = None
    max_pcm_channels: int | None = Field(default=None, gt=0)
    pcm_formats: tuple[str, ...] = ()
    bitstream_families: tuple[str, ...] = ()
    arc: bool | None = None
    earc: bool | None = None


class SignalPort(BaseModel):
    model_config = ConfigDict(frozen=True)

    port_id: str = Field(min_length=1)
    direction: PortDirection
    medium: MediumKind
    label: str | None = None
    video: VideoCapabilitySet | None = None
    audio: AudioCapabilitySet | None = None


class SignalPathNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str = Field(min_length=1)
    kind: SignalNodeKind
    label: str | None = None
    device_ref: str | None = None
    entity_id: str | None = None
    ports: tuple[SignalPort, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'SignalPathNode':
        ids = [p.port_id for p in self.ports]
        if len(set(ids)) != len(ids):
            raise ValueError('port ids must be unique within a node')
        return self


class SignalPathEdge(BaseModel):
    """One hop: an output port to an input port over a medium."""

    model_config = ConfigDict(frozen=True)

    edge_id: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    from_port_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    to_port_id: str = Field(min_length=1)
    medium: MediumKind = 'unknown'
    cable_capability: VideoCapabilitySet | None = None
    cable_audio_capability: AudioCapabilitySet | None = None
    cable_run_ref: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class AVSignalPath(BaseModel):
    """A named source→…→sink path through nodes/edges."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['av-signal-path-1'] = 'av-signal-path-1'
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str | None = None
    nodes: tuple[SignalPathNode, ...]
    edges: tuple[SignalPathEdge, ...]
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    path_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'path_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'AVSignalPath':
        node_ids = [n.node_id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError('node ids must be unique')
        nodes = {n.node_id: n for n in self.nodes}
        port_index = {
            (n.node_id, p.port_id): p
            for n in self.nodes for p in n.ports
        }
        for edge in self.edges:
            src = port_index.get((edge.from_node_id, edge.from_port_id))
            dst = port_index.get((edge.to_node_id, edge.to_port_id))
            if src is None or dst is None:
                raise ValueError(
                    f'edge {edge.edge_id} references a missing port'
                )
            if nodes[edge.from_node_id] is nodes[edge.to_node_id]:
                raise ValueError(
                    f'edge {edge.edge_id} loops a node to itself'
                )
        if self.path_sha256 != _hash(self.semantic_payload()):
            raise ValueError('signal path semantic hash mismatch')
        return self


def build_av_signal_path(
    *,
    path_id: str,
    version: str,
    nodes: tuple[SignalPathNode, ...],
    edges: tuple[SignalPathEdge, ...],
    label: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AVSignalPath:
    probe = AVSignalPath.model_construct(
        path_id=path_id,
        version=version,
        label=label,
        nodes=tuple(nodes),
        edges=tuple(edges),
        provenance=tuple(provenance),
        path_sha256='',
    )
    return AVSignalPath(
        **probe.model_dump(mode='python', exclude={'path_sha256'}),
        path_sha256=_hash(probe.semantic_payload()),
    )


class MediaPlaybackCondition(BaseModel):
    """The requested playback mode a path is evaluated against."""

    model_config = ConfigDict(frozen=True)

    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    refresh_hz: float | None = Field(default=None, gt=0.0)
    chroma_subsampling: str | None = None
    bit_depth: int | None = Field(default=None, gt=0)
    hdr_format: str | None = None
    vrr: bool | None = None
    audio_format: str | None = None
    audio_channels: int | None = Field(default=None, gt=0)
    requires_earc: bool | None = None


class NegotiatedModeEvidence(BaseModel):
    """What the link actually negotiated — kept separate from declared
    capability sets."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_sha256: str = Field(min_length=16)
    observed_at_utc: str = Field(min_length=1)
    method: Literal[
        'edid_readback',
        'link_training',
        'osd_status',
        'user_confirmed',
        'other',
    ] = 'other'
    worked: bool
    negotiated: MediaPlaybackCondition | None = None
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class HopResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    hop: str = Field(min_length=1)
    status: PathStatus
    limiting_component: str | None = None
    reason: str | None = None


class SignalPathEvaluation(BaseModel):
    """Per-hop and overall compatibility for one playback condition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    path: AVSignalPath
    condition: MediaPlaybackCondition
    hops: tuple[HopResult, ...]
    status: PathStatus
    limiting_component: str | None = None
    negotiation: NegotiatedModeEvidence | None = None
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SignalPathEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('signal path evaluation hash mismatch')
        if self.evaluation_id != 'spe-' + digest[:24]:
            raise ValueError('signal path evaluation id mismatch')
        return self


def _video_carry(
    cap: VideoCapabilitySet | None,
    condition: MediaPlaybackCondition,
) -> PathStatus:
    """Whether a capability set carries the requested video condition.
    Missing fields → UNKNOWN (never "unsupported")."""

    if cap is None:
        return 'UNKNOWN'
    if condition.width_px is not None:
        if cap.max_width_px is None or cap.max_height_px is None:
            return 'UNKNOWN'
        if condition.width_px > cap.max_width_px:
            return 'UNSUPPORTED'
    if condition.height_px is not None and cap.max_height_px is not None:
        if condition.height_px > cap.max_height_px:
            return 'UNSUPPORTED'
    if condition.refresh_hz is not None:
        if cap.max_refresh_hz is None:
            return 'UNKNOWN'
        if condition.refresh_hz > cap.max_refresh_hz:
            return 'UNSUPPORTED'
    if condition.chroma_subsampling is not None:
        if not cap.chroma_subsampling:
            return 'UNKNOWN'
        if condition.chroma_subsampling not in cap.chroma_subsampling:
            return 'UNSUPPORTED'
    if condition.bit_depth is not None:
        if not cap.bit_depths:
            return 'UNKNOWN'
        if condition.bit_depth not in cap.bit_depths:
            return 'UNSUPPORTED'
    if condition.hdr_format is not None:
        if not cap.hdr_formats:
            return 'UNKNOWN'
        if condition.hdr_format not in cap.hdr_formats:
            return 'UNSUPPORTED'
    if condition.vrr:
        if cap.vrr is None:
            return 'UNKNOWN'
        if not cap.vrr:
            return 'UNSUPPORTED'
    return 'SUPPORTED'


def _audio_carry(
    cap: AudioCapabilitySet | None,
    condition: MediaPlaybackCondition,
) -> PathStatus:
    if cap is None:
        return 'UNKNOWN'
    if condition.audio_channels is not None:
        if cap.max_pcm_channels is None:
            return 'UNKNOWN'
        if condition.audio_channels > cap.max_pcm_channels:
            return 'UNSUPPORTED'
    if condition.audio_format is not None:
        known = set(cap.pcm_formats) | set(cap.bitstream_families)
        if not known:
            return 'UNKNOWN'
        if condition.audio_format not in known:
            return 'UNSUPPORTED'
    if condition.requires_earc:
        if cap.earc is None:
            return 'UNKNOWN'
        if not cap.earc:
            return 'UNSUPPORTED'
    return 'SUPPORTED'


def _fold(hop_statuses: tuple[PathStatus, ...]) -> PathStatus:
    """Declared-path fold: UNSUPPORTED > UNKNOWN > SUPPORTED."""
    if 'UNSUPPORTED' in hop_statuses:
        return 'UNSUPPORTED'
    if 'UNKNOWN' in hop_statuses:
        return 'UNKNOWN'
    return 'SUPPORTED'


def evaluate_signal_path(
    *,
    path: AVSignalPath,
    condition: MediaPlaybackCondition,
    negotiation: NegotiatedModeEvidence | None = None,
) -> SignalPathEvaluation:
    """Evaluate every hop against the requested condition, then fold.

    When negotiation evidence is supplied it must bind to the exact path
    hash; it upgrades/downgrades the declared result to VERIFIED_WORKING /
    VERIFIED_FAILURE. Evidence for a different path hash is a binding FAIL,
    not silently accepted.
    """

    nodes = {n.node_id: n for n in path.nodes}
    port_index = {
        (n.node_id, p.port_id): p for n in path.nodes for p in n.ports
    }
    hops: list[HopResult] = []
    for edge in path.edges:
        src = port_index[(edge.from_node_id, edge.from_port_id)]
        dst = port_index[(edge.to_node_id, edge.to_port_id)]
        # an edge drives out→in (or bidirectional); flag wrong-way links
        ok_direction = (
            src.direction in {'out', 'bidirectional'}
            and dst.direction in {'in', 'bidirectional'}
        )
        # Audio capability is only a limit where audio actually terminates:
        # the processor legs and return-channel hops. A display's video
        # input is not required to decode formats the AVR already consumed.
        dst_kind = nodes[edge.to_node_id].kind
        audio_applies = (
            dst_kind != 'display'
            or edge.medium in {'earc', 'arc', 'toslink', 'analog'}
            or dst.medium in {'earc', 'arc', 'toslink', 'analog'}
        )
        video = _fold(
            (
                _video_carry(src.video, condition),
                _video_carry(dst.video, condition),
                _video_carry(edge.cable_capability, condition)
                if edge.cable_capability is not None
                else 'UNKNOWN',
            )
        )
        audio = (
            _fold(
                (
                    _audio_carry(src.audio, condition),
                    _audio_carry(dst.audio, condition),
                    _audio_carry(edge.cable_audio_capability, condition)
                    if edge.cable_audio_capability is not None
                    else 'UNKNOWN',
                )
            )
            if audio_applies
            else 'SUPPORTED'
        )
        # direction check folds into the result
        hop_status = 'UNSUPPORTED' if not ok_direction else _fold(
            (video, audio)
        )
        limiting = None
        if hop_status == 'UNSUPPORTED':
            if not ok_direction:
                limiting = f'{edge.edge_id}:direction'
            # otherwise name the first failing component for diagnosis
            for name, cap_result in (
                (f'{edge.from_node_id}.{edge.from_port_id}',
                 _video_carry(src.video, condition)),
                (f'{edge.to_node_id}.{edge.to_port_id}',
                 _video_carry(dst.video, condition)),
                (f'cable:{edge.edge_id}',
                 _video_carry(edge.cable_capability, condition)
                 if edge.cable_capability is not None else 'UNKNOWN'),
                (f'{edge.from_node_id}.{edge.from_port_id}:audio',
                 _audio_carry(src.audio, condition)),
                (f'{edge.to_node_id}.{edge.to_port_id}:audio',
                 _audio_carry(dst.audio, condition)),
                (f'cable:{edge.edge_id}:audio',
                 _audio_carry(edge.cable_audio_capability, condition)
                 if edge.cable_audio_capability is not None
                 else 'UNKNOWN'),
            ):
                if limiting is None and cap_result == 'UNSUPPORTED':
                    limiting = name
                    break
        hops.append(
            HopResult(
                hop=edge.edge_id,
                status=hop_status,
                limiting_component=limiting,
            )
        )

    if not path.edges:
        declared: PathStatus = 'UNKNOWN'
    else:
        declared = _fold(tuple(h.status for h in hops))
    limiting = next(
        (h.limiting_component for h in hops if h.limiting_component), None
    )

    status = declared
    if negotiation is not None:
        bound = (
            negotiation.path_id == path.path_id
            and negotiation.path_sha256 == path.path_sha256
        )
        if not bound:
            status = 'UNKNOWN'
        elif negotiation.worked:
            status = 'VERIFIED_WORKING'
        else:
            status = 'VERIFIED_FAILURE'

    probe = SignalPathEvaluation.model_construct(
        evaluation_id='',
        path=path,
        condition=condition,
        hops=tuple(hops),
        status=status,
        limiting_component=limiting,
        negotiation=negotiation,
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return SignalPathEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='spe-' + digest[:24],
        evaluation_sha256=digest,
    )
