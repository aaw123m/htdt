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
- A path is exactly one connected, directed chain (#820): every node sits on
  the route, exactly one start and one end exist, and cycles/merges/
  branches/disconnected edge bags are rejected at construction. Edge media
  must be structurally compatible with both endpoint port media — a medium
  conversion is modelled as an explicit adapter node, never implied by an
  edge label; ``unknown``/``other`` assert nothing and evaluate UNKNOWN.
- Negotiated evidence verifies the *requested* condition (#820):
  ``worked=True`` upgrades to VERIFIED_WORKING only when the recorded
  negotiated mode meets every requested requirement; a degraded negotiated
  mode reports VERIFIED_DIFFERENT_MODE, and a missing negotiated mode stays
  UNKNOWN. The evidence object (method, note, provenance) is preserved on
  the evaluation verbatim so Result Trust keeps method strength visible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash




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
    'VERIFIED_WORKING', 'VERIFIED_FAILURE', 'VERIFIED_DIFFERENT_MODE',
]

# Media carried over the same physical HDMI connector family. eARC/ARC are
# HDMI-connector transports, so a port labelled 'hdmi' is link-compatible
# with an 'earc'/'arc' edge (feature support stays a capability-set check).
_HDMI_FAMILY = frozenset({'hdmi', 'earc', 'arc'})

# Media that can only ever terminate an audio return/auxiliary channel — a
# route consisting solely of these is an audio-return route, not a forward
# video route.
_RETURN_MEDIA = frozenset({'earc', 'arc', 'toslink', 'analog'})

# Media that assert a concrete electrical/protocol link. 'other'/'unknown'
# assert nothing and must evaluate UNKNOWN, never implicit compatibility.
_PROVEN_MEDIA = frozenset(
    {'hdmi', 'displayport', 'earc', 'arc', 'toslink', 'analog', 'hdBaseT'}
)


def _media_link_compatible(a: MediumKind, b: MediumKind) -> bool | None:
    """Structural link compatibility between two media.

    ``True`` = same link family; ``False`` = two proven media that cannot
    mate; ``None`` = either side asserts nothing (unknown/other) — UNKNOWN,
    never implicit compatibility.
    """
    if a not in _PROVEN_MEDIA or b not in _PROVEN_MEDIA:
        return None
    if a == b:
        return True
    if a in _HDMI_FAMILY and b in _HDMI_FAMILY:
        return True
    return False


def _route_role(path: 'AVSignalPath') -> Literal['forward', 'audio_return']:
    """Whether the route is a forward path or a pure audio-return path."""
    media = {edge.medium for edge in path.edges}
    if media and media <= _RETURN_MEDIA:
        return 'audio_return'
    return 'forward'


def _route_endpoints(
    path: 'AVSignalPath',
) -> tuple[str, str] | None:
    """Directed (start, end) node ids of a chain route, or None.

    A route is a linear chain: exactly one node out-degree 1/in-degree 0
    (start), exactly one in-degree 1/out-degree 0 (end), every other node
    1-in/1-out. Cycles, merges, branches and disconnected edge bags have no
    such pair.
    """
    out_deg: dict[str, int] = {n.node_id: 0 for n in path.nodes}
    in_deg: dict[str, int] = {n.node_id: 0 for n in path.nodes}
    for edge in path.edges:
        out_deg[edge.from_node_id] += 1
        in_deg[edge.to_node_id] += 1
    start = [n for n in out_deg if out_deg[n] == 1 and in_deg[n] == 0]
    end = [n for n in in_deg if in_deg[n] == 1 and out_deg[n] == 0]
    if len(start) != 1 or len(end) != 1:
        return None
    if any(
        not (0 <= out_deg[n] <= 1 and 0 <= in_deg[n] <= 1)
        for n in out_deg
    ):
        return None
    # connectivity: walking forward from the unique start must reach every
    # edge exactly once and stop at the unique end.
    edges_from = {edge.from_node_id: edge for edge in path.edges}
    seen: set[str] = set()
    node = start[0]
    while node in edges_from:
        edge = edges_from[node]
        if edge.edge_id in seen:
            return None
        seen.add(edge.edge_id)
        node = edge.to_node_id
    if node != end[0] or len(seen) != len(path.edges):
        return None
    return start[0], end[0]


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
            for endpoint, port in (
                (edge.from_node_id, src),
                (edge.to_node_id, dst),
            ):
                link = _media_link_compatible(edge.medium, port.medium)
                if link is False:
                    raise ValueError(
                        f'edge {edge.edge_id} medium {edge.medium!r} cannot '
                        f'mate {port.medium!r} port {endpoint}.'
                        f'{port.port_id} — a medium conversion requires an '
                        'explicit adapter node'
                    )
        if self.edges:
            # One connected, coherent route — not an arbitrary edge bag:
            # every node must sit on the route and the directed edges must
            # form a single linear chain (rejects cycles, merges, branches
            # and disconnected pairs of valid edges).
            incident = {
                edge.from_node_id for edge in self.edges
            } | {
                edge.to_node_id for edge in self.edges
            }
            stray = set(nodes) - incident
            if stray:
                raise ValueError(
                    f'signal path node(s) {sorted(stray)} sit outside the '
                    'route'
                )
            if _route_endpoints(self) is None:
                raise ValueError(
                    'signal path edges do not form one connected chain with '
                    'a unique start and end'
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
    required_bandwidth_gbps: float | None = Field(default=None, gt=0.0)
    """Caller-declared transport rate for the requested mode. Compared
    exactly against each ``VideoCapabilitySet.bandwidth_gbps`` — the
    condition carries the rate authority (e.g. the mode's CTA-861 required
    TMDS/FRL rate); this module never derives one."""


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
        if cap.max_width_px is None:
            return 'UNKNOWN'
        if condition.width_px > cap.max_width_px:
            return 'UNSUPPORTED'
    if condition.height_px is not None:
        if cap.max_height_px is None:
            return 'UNKNOWN'
        if condition.height_px > cap.max_height_px:
            return 'UNSUPPORTED'
    if condition.required_bandwidth_gbps is not None:
        if cap.bandwidth_gbps is None:
            return 'UNKNOWN'
        if condition.required_bandwidth_gbps > cap.bandwidth_gbps:
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


_NUMERIC_REQUEST_FIELDS = (
    'width_px',
    'height_px',
    'refresh_hz',
    'bit_depth',
    'audio_channels',
    'required_bandwidth_gbps',
)
_CATEGORICAL_REQUEST_FIELDS = (
    'chroma_subsampling',
    'hdr_format',
    'audio_format',
)


def _negotiated_meets_request(
    negotiated: MediaPlaybackCondition | None,
    requested: MediaPlaybackCondition,
) -> bool | None:
    """Whether the observed negotiated mode meets every requested
    requirement. ``None`` = no negotiated mode recorded (cannot verify).

    Numeric capabilities must meet or exceed the request; categorical
    formats must match exactly. Unset request fields assert nothing.
    """
    if negotiated is None:
        return None
    for field in _NUMERIC_REQUEST_FIELDS:
        req = getattr(requested, field)
        if req is None:
            continue
        got = getattr(negotiated, field)
        if got is None or got < req:
            return False
    for field in _CATEGORICAL_REQUEST_FIELDS:
        req = getattr(requested, field)
        if req is not None and getattr(negotiated, field) != req:
            return False
    if requested.vrr and negotiated.vrr is not True:
        return False
    if requested.requires_earc and negotiated.requires_earc is not True:
        return False
    return True


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
        # Link-media compatibility: a proven mismatch is impossible to
        # persist (model validation), so a non-proven medium asserts
        # nothing and stays UNKNOWN — never implicit compatibility.
        link = _media_link_compatible(src.medium, dst.medium)
        edge_link = _media_link_compatible(edge.medium, src.medium)
        edge_link_dst = _media_link_compatible(edge.medium, dst.medium)
        medium_status: PathStatus = 'SUPPORTED'
        if (
            edge.medium not in _PROVEN_MEDIA
            or src.medium not in _PROVEN_MEDIA
            or dst.medium not in _PROVEN_MEDIA
        ):
            medium_status = 'UNKNOWN'
        elif (
            link is False or edge_link is False or edge_link_dst is False
        ):
            medium_status = 'UNSUPPORTED'
        # direction check folds into the result
        hop_status = 'UNSUPPORTED' if not ok_direction else _fold(
            (video, audio, medium_status)
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

    # Endpoint semantics: a forward route carrying a requested video mode
    # must terminate at a display/eARC endpoint input; a pure audio-return
    # route may legitimately end at the processor/AVR.
    video_requested = any(
        getattr(condition, field) is not None
        for field in (
            'width_px',
            'height_px',
            'refresh_hz',
            'chroma_subsampling',
            'bit_depth',
            'hdr_format',
            'required_bandwidth_gbps',
        )
    ) or bool(condition.vrr)
    if path.edges and video_requested and _route_role(path) == 'forward':
        endpoints = _route_endpoints(path)
        end_kind = nodes[endpoints[1]].kind if endpoints else None
        if end_kind not in {'display', 'earc_endpoint'}:
            hops.append(
                HopResult(
                    hop='route',
                    status='UNSUPPORTED',
                    limiting_component='route:endpoint',
                    reason=(
                        'forward path terminates at '
                        f'{end_kind or "no unique end"}, not a display/eARC '
                        'endpoint input'
                    ),
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
        elif not negotiation.worked:
            status = 'VERIFIED_FAILURE'
        else:
            meets = _negotiated_meets_request(negotiation.negotiated, condition)
            if meets is True:
                status = 'VERIFIED_WORKING'
            elif meets is False:
                # The link ran, but under a degraded/different mode than the
                # requested condition — distinct from success.
                status = 'VERIFIED_DIFFERENT_MODE'
            else:
                # worked=True without a recorded negotiated mode cannot prove
                # the requested condition — partially verified at best.
                status = 'UNKNOWN'

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
