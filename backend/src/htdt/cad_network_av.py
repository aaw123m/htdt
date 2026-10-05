"""Networked AV transport qualification authority (issue #591).

This module owns the answer to "can this IP network demonstrably carry
this AV workload" — and it answers fail-closed: a network that has
never been observed carrying the flow is UNKNOWN, never "probably fine".

Three layers, all sealed:

1. :class:`NetworkAVPath` — the *physical/administrative* topology
   claim: endpoints, switches, ports, uplinks, VLANs, link speeds,
   media, PoE roles, LAG groups. This is a declaration of what the
   design intends, with provenance — it is not evidence the network
   works.
2. :class:`NetworkMediaFlow` — the *logical workload*: provider profile
   (AES67/Dante/ST 2110/AVB/Ravenna/vendor/generic), endpoints,
   unicast/multicast delivery, codec, channel count, rates, bitrate,
   latency budget, clock requirement, redundancy requirement. A flow
   names its ordered hop path inside the topology.
3. Evidence + verdict — :class:`NetworkTransportObservation` (packet
   quality, utilization, QoS state, multicast state, redundancy events,
   stress/soak runs, diagnostic isolations) and
   :class:`NetworkTimingObservation` (PTP domain/grandmaster/leader-
   follower/offset/path-delay/lock/clock-class/failover) bound to the
   topology; :func:`evaluate_network_av_qualification` derives a sealed
   :class:`NetworkAVQualification` with the media-state ladder
   ``discovered -> discovery_available -> control_established ->
   media_stream_qualified`` and per-check results.

Mechanically enforced rules:

- Every numeric counter (loss, jitter, CRC, reorder) is optional —
  ``None`` = UNKNOWN, and a fabricated zero is structurally identical
  to a real zero only if a real observation produced it.
- ``qos_evidence_level`` distinguishes ``endpoint_marking_declared``
  from ``switch_policy_observed`` from
  ``end_to_end_behavior_verified`` — a DSCP marking on an endpoint is
  never presented as verified end-to-end QoS.
- Multicast evidence (IGMP querier/snooping/flooding observations) is
  *conditional* on the flow actually using multicast — a unicast flow
  cannot be failed for missing IGMP evidence.
- Redundancy qualification requires real failover *event* evidence —
  a declared redundant topology is not a proven one.
- Security boundary: observations are data records. There is no
  scanning, probing, or auth capability in this authority.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


NETWORK_AV_PATH_AUTHORITY_VERSION = 'network-av-path-1'
NETWORK_MEDIA_FLOW_AUTHORITY_VERSION = 'network-media-flow-1'
NETWORK_TRANSPORT_OBSERVATION_AUTHORITY_VERSION = (
    'network-transport-observation-1'
)
NETWORK_TIMING_OBSERVATION_AUTHORITY_VERSION = (
    'network-timing-observation-1'
)
NETWORK_AV_QUALIFICATION_AUTHORITY_VERSION = (
    'network-av-qualification-1'
)
NETWORK_AV_EVALUATION_VERSION = 'network-av-eval-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Topology declaration
# ---------------------------------------------------------------------------

NetworkNodeKind = Literal[
    'endpoint', 'switch', 'router', 'wireless_ap', 'other'
]
NetworkMedium = Literal[
    'copper', 'fiber', 'wireless', 'other', 'unknown'
]
PoERole = Literal['none', 'pse', 'pd', 'unknown']
"""PoE *roles* only — electrical power qualification (budget, classes,
draw) is #587's scope and is deliberately not modeled here."""


class NetworkPortDecl(BaseModel):
    """One declared port on a topology node."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    port_id: str = Field(min_length=1)
    speed_gbps: float | None = Field(default=None, gt=0.0)
    """Declared link speed — None = unknown."""
    medium: NetworkMedium = 'unknown'
    vlan_ids: tuple[int, ...] = ()
    poe_role: PoERole = 'unknown'
    lag_group: str | None = None
    label: str | None = None


class NetworkNodeDecl(BaseModel):
    """One node of the declared topology (endpoint, switch, …)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    node_id: str = Field(min_length=1)
    kind: NetworkNodeKind
    label: str | None = None
    device_ref: str | None = None
    """Entity/device registry reference when the node is managed gear."""
    mgmt_vlan_id: int | None = None
    ports: tuple[NetworkPortDecl, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'NetworkNodeDecl':
        ids = [p.port_id for p in self.ports]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate port_id in node')
        return self


class NetworkLinkDecl(BaseModel):
    """One declared link (edge) between two node ports."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    link_id: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    from_port_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    to_port_id: str = Field(min_length=1)
    nominal_capacity_gbps: float | None = Field(default=None, gt=0.0)
    medium: NetworkMedium = 'unknown'
    is_uplink: bool = False
    """Uplink flag marks aggregation links for oversubscription checks."""
    vlan_ids: tuple[int, ...] = ()
    label: str | None = None


class NetworkAVPath(BaseModel):
    """Sealed declared topology for networked AV (#591 §2).

    This is the *design claim*: nodes, ports, links, VLANs, capacities.
    Saving it asserts nothing about whether the network actually works —
    that is what the observation/qualification layer is for.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'network-av-path-1'
    ] = NETWORK_AV_PATH_AUTHORITY_VERSION
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    label: str | None = None
    nodes: tuple[NetworkNodeDecl, ...] = ()
    links: tuple[NetworkLinkDecl, ...] = ()
    segmentation_note: str | None = None
    """VLAN/segmentation plan description (project-driven)."""
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'path_id', 'version', 'path_sha256'},
        )

    def node(self, node_id: str) -> NetworkNodeDecl | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    @model_validator(mode='after')
    def _check(self) -> 'NetworkAVPath':
        node_ids = {n.node_id for n in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError('duplicate node_id in topology')
        port_keys = {
            (n.node_id, p.port_id) for n in self.nodes for p in n.ports
        }
        link_ids = set()
        for link in self.links:
            if link.link_id in link_ids:
                raise ValueError('duplicate link_id')
            link_ids.add(link.link_id)
            if (link.from_node_id, link.from_port_id) not in port_keys:
                raise ValueError(
                    f'link {link.link_id} references unknown from-port'
                )
            if (link.to_node_id, link.to_port_id) not in port_keys:
                raise ValueError(
                    f'link {link.link_id} references unknown to-port'
                )
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        expected = _hash(self.identity_payload())
        if self.path_sha256 != expected:
            raise ValueError('network av path hash mismatch')
        return self


def build_network_av_path(
    *,
    path_id: str,
    version: str,
    document_id: str,
    created_at_utc: str | None = None,
    **kwargs: Any,
) -> NetworkAVPath:
    probe = NetworkAVPath.model_construct(
        **canonicalize_payload(
            NetworkAVPath,
            dict(
                schema_version=1,
                authority_version=NETWORK_AV_PATH_AUTHORITY_VERSION,
                path_id=path_id,
                version=version,
                path_sha256='0' * 64,
                document_id=document_id,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    return NetworkAVPath(
        **probe.model_dump(mode='python', exclude={'path_sha256'}),
        path_sha256=_hash(probe.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Media flow declaration
# ---------------------------------------------------------------------------

MediaProviderProfile = Literal[
    'aes67',
    'dante',
    'st2110',
    'avb',
    'ravenna',
    'vendor_other',
    'generic',
    'control',
    'unknown',
]
"""Provider/transport profile family (#591 §3). ``control`` marks
non-media control flows (device discovery/control) which have no
bandwidth/PTP requirements."""

DeliveryMode = Literal['unicast', 'multicast']
ClockRequirement = Literal[
    'ptp_required', 'ptp_preferred', 'none', 'unknown'
]
"""Whether the flow's media clock rides IEEE 1588 PTP (AES67/ST 2110/
AVB/PTP-profiled vendor systems) — drives which timing observations
the qualification demands."""


class NetworkMediaFlow(BaseModel):
    """Sealed workload declaration for one media or control flow
    (#591 §3)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'network-media-flow-1'
    ] = NETWORK_MEDIA_FLOW_AUTHORITY_VERSION
    flow_id: str = Field(min_length=1)
    flow_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)

    provider_profile: MediaProviderProfile
    label: str | None = None
    source_node_id: str = Field(min_length=1)
    sink_node_ids: tuple[str, ...] = Field(min_length=1)
    delivery: DeliveryMode
    multicast_group: str | None = None
    codec_label: str | None = None
    audio_channels: int | None = Field(default=None, gt=0)
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    frame_rate_hz: float | None = Field(default=None, gt=0.0)
    required_bitrate_mbps: float | None = Field(default=None, gt=0.0)
    bitrate_max_mbps: float | None = Field(default=None, gt=0.0)
    """Sustained vs peak/burst bandwidth needs."""
    latency_budget_seconds: float | None = Field(default=None, gt=0.0)
    jitter_budget_seconds: float | None = Field(default=None, gt=0.0)
    clock_requirement: ClockRequirement = 'unknown'
    requires_redundancy: bool = False
    redundancy_scheme: str | None = None
    """e.g. ST 2022-7 seamless pair — declared here, *evidenced* via
    redundancy observations."""
    hop_link_ids: tuple[str, ...] = ()
    """Ordered link ids of the flow's route inside the bound topology —
    empty until the path is designed."""
    vlan_id: int | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'flow_id', 'flow_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'NetworkMediaFlow':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if self.delivery == 'multicast' and not self.multicast_group:
            raise ValueError(
                'a multicast flow requires a multicast_group'
            )
        if self.delivery == 'unicast' and self.multicast_group:
            raise ValueError(
                'a unicast flow cannot claim a multicast_group'
            )
        if (
            self.required_bitrate_mbps is not None
            and self.bitrate_max_mbps is not None
            and self.bitrate_max_mbps < self.required_bitrate_mbps
        ):
            raise ValueError('burst bitrate below sustained bitrate')
        expected = _hash(self.identity_payload())
        if self.flow_sha256 != expected:
            raise ValueError('network media flow hash mismatch')
        if self.flow_id != f'netflow:{expected}':
            raise ValueError('network media flow id mismatch')
        return self


def build_network_media_flow(
    *, created_at_utc: str | None = None, **kwargs: Any
) -> NetworkMediaFlow:
    probe = NetworkMediaFlow.model_construct(
        **canonicalize_payload(
            NetworkMediaFlow,
            dict(
                schema_version=1,
                authority_version=NETWORK_MEDIA_FLOW_AUTHORITY_VERSION,
                flow_id='',
                flow_sha256='0' * 64,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NetworkMediaFlow(
        **probe.model_dump(mode='python', exclude={'flow_id', 'flow_sha256'}),
        flow_id=f'netflow:{sha}',
        flow_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Transport + timing evidence
# ---------------------------------------------------------------------------

TransportObservationKind = Literal[
    'packet_quality',
    'link_utilization',
    'qos_state',
    'multicast_state',
    'redundancy_event',
    'stress_soak',
    'diagnostic_isolation',
]
"""Observation classes (#591 §5-§9). ``diagnostic_isolation`` records an
isolated bench/test-segment run — kept separate so it never masquerades
as production-path evidence."""

QoSEvidenceLevel = Literal[
    'endpoint_marking_declared',
    'switch_policy_observed',
    'end_to_end_behavior_verified',
]
"""QoS evidence ladder (#591 §6): a declared DSCP mark on an endpoint,
an observed switch queuing policy, and verified end-to-end behavior
are three different claims and are never collapsed."""


class NetworkTransportObservation(BaseModel):
    """One bound packet/link/QoS/multicast/stress observation
    (#591 §5-§9).

    Every counter is optional: ``None`` = the interface could not or did
    not report it — UNKNOWN, not zero.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'network-transport-observation-1'
    ] = NETWORK_TRANSPORT_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    kind: TransportObservationKind
    flow_id: str | None = None
    """Bound media flow when the observation is flow-scoped."""
    interface_ref: str | None = None
    """Node/port identity (``node:port``) the observation was taken on."""
    interval_start_utc: str | None = None
    interval_end_utc: str | None = None
    scenario: str | None = None
    """What the network was doing (idle / full production load /
    failover drill …)."""
    method: str | None = None
    """How it was observed (switch counters / mirror port / analyzer /
    vendor report)."""
    units_note: str | None = None
    uncertainty_note: str | None = None

    # packet quality
    packets_total: int | None = Field(default=None, ge=0)
    packets_lost: int | None = Field(default=None, ge=0)
    loss_ratio: float | None = Field(default=None, ge=0.0)
    jitter_seconds: float | None = Field(default=None, ge=0.0)
    out_of_order: int | None = Field(default=None, ge=0)
    crc_errors: int | None = Field(default=None, ge=0)
    buffer_underruns: int | None = Field(default=None, ge=0)

    # link utilization / capacity
    utilization_ratio: float | None = Field(default=None, ge=0.0)
    observed_bitrate_mbps: float | None = Field(default=None, ge=0.0)

    # QoS
    qos_evidence_level: QoSEvidenceLevel | None = None
    qos_detail: str | None = None
    """Declared marks (DSCP/CoS values) or observed queue policy."""

    # multicast
    igmp_querier_present: bool | None = None
    igmp_snooping_enabled: bool | None = None
    flooding_observed: bool | None = None
    multicast_groups_observed: tuple[str, ...] = ()

    # redundancy / failover
    redundancy_event: str | None = None
    """What was exercised (link pull, power fail, grandmaster loss)."""
    failover_detected: bool | None = None
    failover_recovery_seconds: float | None = Field(
        default=None, ge=0.0
    )
    media_interruption_observed: bool | None = None

    # stress / soak
    stress_duration_seconds: float | None = Field(default=None, gt=0.0)
    stress_load_description: str | None = None

    # diagnostic isolation
    isolation_scope: str | None = None
    """What was bypassed/isolated for the diagnostic run."""

    observed_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'NetworkTransportObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        for when in (self.interval_start_utc, self.interval_end_utc):
            if when is not None:
                _require_iso8601(when, 'interval_*_utc')
        if (
            self.interval_start_utc
            and self.interval_end_utc
            and self.interval_end_utc <= self.interval_start_utc
        ):
            raise ValueError('observation interval end before start')
        if self.kind == 'qos_state' and self.qos_evidence_level is None:
            raise ValueError(
                'a qos_state observation requires an explicit '
                'qos_evidence_level'
            )
        if self.kind == 'stress_soak' and (
            self.stress_duration_seconds is None
        ):
            raise ValueError(
                'a stress_soak observation requires its duration — '
                'an undated soak is not evidence'
            )
        if self.kind == 'redundancy_event' and not self.redundancy_event:
            raise ValueError(
                'a redundancy_event observation requires what was '
                'exercised'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('transport observation hash mismatch')
        if self.observation_id != f'netobs:{expected}':
            raise ValueError('transport observation id mismatch')
        return self


def build_network_transport_observation(**kwargs: Any) -> NetworkTransportObservation:
    probe = NetworkTransportObservation.model_construct(
        **canonicalize_payload(
            NetworkTransportObservation,
            dict(
                schema_version=1,
                authority_version=(
                    NETWORK_TRANSPORT_OBSERVATION_AUTHORITY_VERSION
                ),
                observation_id='',
                observation_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NetworkTransportObservation(
        **probe.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        ),
        observation_id=f'netobs:{sha}',
        observation_sha256=sha,
    )


PTPNodeState = Literal[
    'grandmaster', 'leader', 'follower', 'unlocked', 'unknown'
]
"""PTP role/lock state (IEEE 1588): ``leader``/``follower`` cover
master/slave roles neutrally."""

PTPProfile = Literal[
    'ieee1588_default', 'aes67', 'st2059', 'avb', 'vendor', 'unknown'
]


class NetworkTimingObservation(BaseModel):
    """One bound PTP/timing observation (#591 §4): the clocking evidence
    AES67/ST 2110/AVB media depends on."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'network-timing-observation-1'
    ] = NETWORK_TIMING_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    ptp_domain: int | None = Field(default=None, ge=0)
    ptp_profile: PTPProfile = 'unknown'
    grandmaster_ref: str | None = None
    node_ref: str | None = None
    """Node this observation describes (``node:port``)."""
    node_state: PTPNodeState = 'unknown'
    offset_from_leader_seconds: float | None = None
    """Signed offset — sign convention documented in ``units_note``."""
    mean_path_delay_seconds: float | None = Field(default=None, ge=0.0)
    lock_state: Literal['locked', 'locking', 'unlocked', 'unknown'] = (
        'unknown'
    )
    clock_class: int | None = Field(default=None, ge=0)
    failover_events: int | None = Field(default=None, ge=0)
    """Grandmaster/failover events observed during the interval."""
    interval_start_utc: str | None = None
    interval_end_utc: str | None = None
    units_note: str | None = None
    uncertainty_note: str | None = None
    observed_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'NetworkTimingObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        for when in (self.interval_start_utc, self.interval_end_utc):
            if when is not None:
                _require_iso8601(when, 'interval_*_utc')
        if self.offset_from_leader_seconds is not None and (
            self.units_note is None
        ):
            raise ValueError(
                'a signed PTP offset requires a units_note stating the '
                'sign convention'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('timing observation hash mismatch')
        if self.observation_id != f'ptpobs:{expected}':
            raise ValueError('timing observation id mismatch')
        return self


def build_network_timing_observation(**kwargs: Any) -> NetworkTimingObservation:
    probe = NetworkTimingObservation.model_construct(
        **canonicalize_payload(
            NetworkTimingObservation,
            dict(
                schema_version=1,
                authority_version=(
                    NETWORK_TIMING_OBSERVATION_AUTHORITY_VERSION
                ),
                observation_id='',
                observation_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NetworkTimingObservation(
        **probe.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        ),
        observation_id=f'ptpobs:{sha}',
        observation_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

MediaState = Literal[
    'discovered',
    'discovery_available',
    'control_established',
    'media_stream_qualified',
    'not_evaluated',
]
"""The commissioning ladder (#591 §10): discovery → control → media.
A system that discovers endpoints but has no media evidence sits at
``discovery_available`` — never silently higher."""

QualificationCheck = Literal[
    'capacity',
    'oversubscription',
    'packet_quality',
    'ptp_timing',
    'qos',
    'multicast',
    'redundancy',
]
CheckResult = Literal['verified', 'not_verified', 'not_applicable']

NetworkAVVerdict = Literal[
    'qualified',
    'qualified_with_limitations',
    'failed',
    'insufficient_evidence',
    'stale',
]


class NetworkAVQualification(BaseModel):
    """Sealed qualification verdict for one flow on one path revision
    (#591 §10-§11). Produced by :func:`evaluate_network_av_qualification`.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'network-av-qualification-1'
    ] = NETWORK_AV_QUALIFICATION_AUTHORITY_VERSION
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    path_sha256: str = Field(pattern=_SHA256)
    flow_id: str = Field(min_length=1)
    flow_sha256: str = Field(pattern=_SHA256)
    evaluation_version: str = Field(min_length=1)
    media_state: MediaState
    checks: tuple[tuple[QualificationCheck, CheckResult], ...] = ()
    verdict: NetworkAVVerdict
    limitations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    """Observation ids the verdict rests on — auditable basis."""
    evaluated_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'NetworkAVQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.verdict == 'qualified' and not self.evidence_refs:
            raise ValueError(
                'a qualified verdict requires bound evidence refs — '
                'qualification never floats free of observations'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('network av qualification hash mismatch')
        if self.qualification_id != f'netq:{expected}':
            raise ValueError('network av qualification id mismatch')
        return self


def _flow_observations(
    flow: NetworkMediaFlow,
    observations: tuple[NetworkTransportObservation, ...],
) -> tuple[NetworkTransportObservation, ...]:
    return tuple(
        o for o in observations
        if o.flow_id in (None, flow.flow_id)
    )


def evaluate_network_av_qualification(
    *,
    path: NetworkAVPath,
    flow: NetworkMediaFlow,
    transport_observations: tuple[NetworkTransportObservation, ...] = (),
    timing_observations: tuple[NetworkTimingObservation, ...] = (),
    evaluated_at_utc: str | None = None,
) -> NetworkAVQualification:
    """Derive the qualification verdict for one flow on one path
    revision (#591 §10-§11). Fail-closed per check:

    - ``capacity``: every hop link's declared capacity must cover the
      flow's required bandwidth — an undeclared capacity is UNKNOWN and
      fails the check.
    - ``oversubscription``: for each uplink on the flow's hop path,
      the sum of *all* flows' required bitrates routed through it (as
      supplied via ``all_flows`` — this function sees only the bound
      flow, so the check uses the flow's own share plus the uplink's
      declared capacity, and marks not_verified when either is
      undeclared).
    - ``packet_quality``: requires at least one packet_quality or
      stress_soak observation bound to this flow or path with a real
      loss/counter field populated.
    - ``ptp_timing``: required only when ``clock_requirement`` is
      ``ptp_required``; needs a timing observation showing a locked
      follower state.
    - ``qos``: satisfied by an ``end_to_end_behavior_verified`` QoS
      observation, or ``switch_policy_observed`` downgraded into a
      limitation; ``endpoint_marking_declared`` alone does not satisfy.
    - ``multicast``: applies only when the flow is multicast; needs an
      observation with IGMP/querier/snooping fields populated and no
      flooding observed.
    - ``redundancy``: applies only when ``requires_redundancy``; needs
      a redundancy_event observation with a detected failover.
    """
    checks: list[tuple[QualificationCheck, CheckResult]] = []
    reasons: list[str] = []
    limitations: list[str] = []
    evidence: list[str] = []

    stale = flow.path_sha256 != path.path_sha256 or (
        flow.path_id != path.path_id
        or flow.path_version != path.version
    )

    obs = tuple(
        o for o in transport_observations
        if o.path_sha256 == path.path_sha256
    )
    tobs = tuple(
        o for o in timing_observations
        if o.path_sha256 == path.path_sha256
    )
    flow_obs = _flow_observations(flow, obs)

    if stale:
        verdict: NetworkAVVerdict = 'stale'
        reasons.append(
            'the flow predates the current topology revision — '
            're-evaluate after the path change'
        )
        media_state: MediaState = 'not_evaluated'
    else:
        # capacity: every declared link on the hop path must cover the
        # flow's sustained bitrate; undeclared capacity -> UNKNOWN ->
        # check fails (fail-closed).
        capacity_ok = True
        if flow.required_bitrate_mbps is None:
            capacity_ok = False
            reasons.append('flow declares no required bitrate')
        elif not flow.hop_link_ids:
            capacity_ok = False
            reasons.append('flow has no designed hop path')
        else:
            link_by_id = {l.link_id: l for l in path.links}
            for link_id in flow.hop_link_ids:
                link = link_by_id.get(link_id)
                if link is None or link.nominal_capacity_gbps is None:
                    capacity_ok = False
                    reasons.append(
                        f'link {link_id} has undeclared capacity'
                    )
                    break
                if link.nominal_capacity_gbps * 1000.0 < (
                    flow.required_bitrate_mbps
                ):
                    capacity_ok = False
                    reasons.append(
                        f'link {link_id} capacity '
                        f'{link.nominal_capacity_gbps}G < required '
                        f'{flow.required_bitrate_mbps}M'
                    )
                    break
        checks.append(
            ('capacity', 'verified' if capacity_ok else 'not_verified')
        )

        # oversubscription on uplinks in the hop path: a bound
        # utilization observation below 1.0 with the flow running, or
        # declared headroom >= peak bitrate. Utilization evidence is
        # preferred over arithmetic.
        uplinks = [
            link_by_id[lid]
            for lid in flow.hop_link_ids
            if (lid in link_by_id and link_by_id[lid].is_uplink)
        ]
        oversub_ok = True
        for link in uplinks:
            uobs = [
                o for o in obs
                if o.interface_ref and link.link_id in o.interface_ref
                and o.kind == 'link_utilization'
                and o.utilization_ratio is not None
            ]
            if uobs:
                if any(o.utilization_ratio and o.utilization_ratio >= 0.95
                       for o in uobs):
                    oversub_ok = False
                    reasons.append(
                        f'uplink {link.link_id} observed at/near '
                        'saturation'
                    )
                else:
                    evidence.extend(o.observation_id for o in uobs[:1])
            else:
                peak = flow.bitrate_max_mbps or flow.required_bitrate_mbps
                if (
                    peak is not None
                    and link.nominal_capacity_gbps is not None
                    and link.nominal_capacity_gbps * 1000.0 >= peak
                ):
                    continue
                oversub_ok = False
                reasons.append(
                    f'uplink {link.link_id}: no utilization evidence '
                    'and no declared headroom for the flow peak'
                )
        checks.append(
            (
                'oversubscription',
                'verified' if oversub_ok else 'not_verified',
            )
        )

        # packet quality: needs real bound evidence.
        pq = [
            o for o in flow_obs
            if o.kind in ('packet_quality', 'stress_soak')
            and (
                o.packets_lost is not None
                or o.loss_ratio is not None
                or o.packets_total is not None
            )
        ]
        if pq:
            checks.append(('packet_quality', 'verified'))
            evidence.extend(o.observation_id for o in pq[:2])
        else:
            checks.append(('packet_quality', 'not_verified'))
            reasons.append('no bound packet-quality evidence')

        # PTP timing
        if flow.clock_requirement == 'ptp_required':
            locked = [
                t for t in tobs if t.lock_state == 'locked'
            ]
            if locked:
                checks.append(('ptp_timing', 'verified'))
                evidence.append(locked[0].observation_id)
            else:
                checks.append(('ptp_timing', 'not_verified'))
                reasons.append(
                    'flow requires PTP but no locked timing observation '
                    'is bound'
                )
        else:
            checks.append(('ptp_timing', 'not_applicable'))

        # QoS — only when the flow is real media (not control).
        if flow.provider_profile == 'control':
            checks.append(('qos', 'not_applicable'))
        else:
            qos_obs = [
                o for o in obs if o.kind == 'qos_state'
            ]
            levels = {o.qos_evidence_level for o in qos_obs}
            if 'end_to_end_behavior_verified' in levels:
                checks.append(('qos', 'verified'))
                evidence.extend(
                    o.observation_id for o in qos_obs
                    if o.qos_evidence_level
                    == 'end_to_end_behavior_verified'
                )
            elif 'switch_policy_observed' in levels:
                checks.append(('qos', 'verified'))
                limitations.append(
                    'QoS verified at switch policy level only — '
                    'end-to-end behavior unverified'
                )
            elif qos_obs:
                checks.append(('qos', 'not_verified'))
                reasons.append(
                    'only endpoint-marking QoS declared — no switch '
                    'policy or end-to-end evidence'
                )
            else:
                checks.append(('qos', 'not_verified'))
                reasons.append('no QoS evidence bound')

        # multicast — conditional on actual multicast use.
        if flow.delivery == 'multicast':
            mc = [o for o in obs if o.kind == 'multicast_state']
            usable = [
                o for o in mc
                if o.igmp_querier_present is not None
                or o.igmp_snooping_enabled is not None
            ]
            flooding = [o for o in mc if o.flooding_observed]
            if usable and not flooding:
                checks.append(('multicast', 'verified'))
                evidence.append(usable[0].observation_id)
            else:
                checks.append(('multicast', 'not_verified'))
                if flooding:
                    reasons.append('multicast flooding observed')
                else:
                    reasons.append(
                        'multicast flow lacks IGMP/querier evidence'
                    )
        else:
            checks.append(('multicast', 'not_applicable'))

        # redundancy — conditional on the flow's declaration.
        if flow.requires_redundancy:
            red = [
                o for o in obs
                if o.kind == 'redundancy_event' and o.failover_detected
            ]
            if red:
                checks.append(('redundancy', 'verified'))
                evidence.append(red[0].observation_id)
            else:
                checks.append(('redundancy', 'not_verified'))
                reasons.append(
                    'flow requires redundancy but no failover event '
                    'evidence exists'
                )
        else:
            checks.append(('redundancy', 'not_applicable'))

        # Media state ladder.
        discovery_obs = [
            o for o in obs if o.kind == 'diagnostic_isolation'
        ]
        check_map = dict(checks)
        core_verified = (
            check_map.get('capacity') == 'verified'
            and check_map.get('packet_quality') == 'verified'
        )
        if (
            core_verified
            and all(
                r in ('verified', 'not_applicable') for _, r in checks
            )
        ):
            media_state = 'media_stream_qualified'
        elif any(
            c == 'verified'
            for c, r in checks if c == 'packet_quality'
        ) or discovery_obs:
            media_state = 'control_established'
        elif obs or tobs:
            media_state = 'discovery_available'
        else:
            media_state = 'discovered'

        failed = [c for c, r in checks if r == 'not_verified']
        if not failed:
            verdict = (
                'qualified_with_limitations' if limitations
                else 'qualified'
            )
        elif 'packet_quality' in failed or 'capacity' in failed:
            verdict = 'insufficient_evidence' if (
                set(failed) <= {'packet_quality', 'capacity',
                                'oversubscription'}
            ) else 'failed'
        else:
            verdict = 'failed' if any(
                c in failed
                for c in ('oversubscription', 'ptp_timing',
                          'redundancy', 'multicast')
            ) else 'insufficient_evidence'
        # No evidence at all is honest insufficient_evidence.
        if not obs and not tobs:
            verdict = 'insufficient_evidence'
            reasons.append('no bound observations exist for this path')

    probe = NetworkAVQualification.model_construct(
        **canonicalize_payload(
            NetworkAVQualification,
            dict(
                schema_version=1,
                authority_version=(
                    NETWORK_AV_QUALIFICATION_AUTHORITY_VERSION
                ),
                qualification_id='',
                qualification_sha256='0' * 64,
                document_id=path.document_id,
                path_id=path.path_id,
                path_version=path.version,
                path_sha256=path.path_sha256,
                flow_id=flow.flow_id,
                flow_sha256=flow.flow_sha256,
                evaluation_version=NETWORK_AV_EVALUATION_VERSION,
                media_state=media_state,
                checks=tuple(checks),
                verdict=verdict,
                limitations=tuple(limitations),
                reasons=tuple(reasons),
                evidence_refs=tuple(dict.fromkeys(evidence)),
                evaluated_at_utc=evaluated_at_utc or _utc_now(),
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return NetworkAVQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        ),
        qualification_id=f'netq:{sha}',
        qualification_sha256=sha,
    )


__all__ = [
    'CheckResult',
    'ClockRequirement',
    'DeliveryMode',
    'MediaProviderProfile',
    'MediaState',
    'NetworkAVPath',
    'NetworkAVQualification',
    'NetworkAVVerdict',
    'NetworkLinkDecl',
    'NetworkMediaFlow',
    'NetworkMedium',
    'NetworkNodeDecl',
    'NetworkNodeKind',
    'NetworkPortDecl',
    'NetworkTimingObservation',
    'NetworkTransportObservation',
    'PoERole',
    'PTPNodeState',
    'PTPProfile',
    'QoSEvidenceLevel',
    'QualificationCheck',
    'TransportObservationKind',
    'NETWORK_AV_EVALUATION_VERSION',
    'NETWORK_AV_PATH_AUTHORITY_VERSION',
    'NETWORK_AV_QUALIFICATION_AUTHORITY_VERSION',
    'NETWORK_MEDIA_FLOW_AUTHORITY_VERSION',
    'NETWORK_TIMING_OBSERVATION_AUTHORITY_VERSION',
    'NETWORK_TRANSPORT_OBSERVATION_AUTHORITY_VERSION',
    'build_network_av_path',
    'build_network_media_flow',
    'build_network_timing_observation',
    'build_network_transport_observation',
    'evaluate_network_av_qualification',
]
