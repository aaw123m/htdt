"""HDMI transport capability & Latency Indication Protocol authority
(#1041).

Feature-based, never version-label based: "HDMI 2.2" on a spec sheet is
not authority that 96 Gbps + LIP + every feature is supported — each
transport feature is a separate evidence record.

Authorities:

- :class:`HDMITransportCapability` — per-port/device feature evidence
  (FRL/TMDS/U96 rate classes, eARC, VRR, ALLM, QFT, LIP) plus exact
  cable-run capability (certification class is bound to the as-built
  cable, not a marketing name). Rate fields carry explicit evidence
  class and stay informational/fail-closed unless that evidence is
  certified or measured.
- :class:`HDMILatencyIndicationEvidence` — LIP-reported per-device
  latency components with protocol/version/readback method. LIP is
  device-reported metadata: it is never presented as a measured A/V-sync
  residual, and negotiated-mode vs LIP-state evidence stays
  independent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash




HdmiFeature = Literal[
    'frl',
    'u96_link',
    'tmds',
    'earc',
    'vrr',
    'allm',
    'qft',
    'lip',
    'hdr_dynamic',
    'other',
]
FeatureState = Literal['supported', 'unsupported', 'unknown']
RateEvidenceClass = Literal[
    'certified', 'measured', 'manufacturer', 'marketing', 'unknown'
]
CableClass = Literal[
    'ultra96', 'ultra_high_speed', 'premium_high_speed', 'standard',
    'other', 'unknown',
]
LipState = Literal['supported', 'active', 'inactive', 'unknown']
LipReadbackMethod = Literal[
    'edid_readback', 'hdmi_forum_report', 'vendor_api', 'cdd', 'other',
    'unknown',
]


class HdmiFeatureEvidence(BaseModel):
    """One HDMI transport feature on one port/device (#1041 §1)."""

    model_config = ConfigDict(frozen=True)

    feature: HdmiFeature
    state: FeatureState = 'unknown'
    max_rate_gbps: float | None = Field(default=None, gt=0.0)
    evidence_class: RateEvidenceClass = 'unknown'
    note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'HdmiFeatureEvidence':
        if (
            self.state == 'supported'
            and self.evidence_class in ('unknown', 'marketing')
        ):
            raise ValueError(
                f"feature {self.feature!r} cannot be 'supported' on "
                f'{self.evidence_class!r} evidence — marketing copy and '
                'missing evidence are not capability'
            )
        return self


class HDMITransportCapability(BaseModel):
    """Exact transport capability of one port/device/cable run."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['hdmi-transport-capability-1'] = (
        'hdmi-transport-capability-1'
    )
    capability_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    device_id: str | None = None
    port_label: str | None = None
    features: tuple[HdmiFeatureEvidence, ...] = ()
    max_link_rate_gbps: float | None = Field(default=None, gt=0.0)
    max_rate_evidence_class: RateEvidenceClass = 'unknown'
    cable_class: CableClass = 'unknown'
    cable_run_id: str | None = None
    cable_certification: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    capability_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'capability_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'HDMITransportCapability':
        if (
            self.max_link_rate_gbps is not None
            and self.max_rate_evidence_class == 'unknown'
        ):
            raise ValueError(
                'max_link_rate_gbps requires an evidence class — a rate '
                'is never inferred from marketing terminology'
            )
        if self.cable_class in ('ultra96', 'ultra_high_speed') and (
            self.cable_certification is None
        ):
            raise ValueError(
                f'cable class {self.cable_class!r} requires a '
                'certification reference bound to the cable run'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.capability_sha256:
            raise ValueError('hdmi transport capability hash mismatch')
        return self

    @property
    def rate_is_authoritative(self) -> bool:
        """Whether the rate figure may authorize a transport decision —
        only certified or measured evidence (#820 dependency)."""
        return self.max_rate_evidence_class in ('certified', 'measured')


class LipLatencyComponent(BaseModel):
    """One device's LIP-reported latency component — reported metadata,
    not measured sync."""

    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1)
    component_kind: str | None = None
    reported_latency_seconds: float = Field(ge=0.0)
    note: str | None = None


class HDMILatencyIndicationEvidence(BaseModel):
    """LIP exchange evidence for one link (#1041 §4)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['hdmi-lip-evidence-1'] = (
        'hdmi-lip-evidence-1'
    )
    evidence_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    signal_path_id: str | None = None
    signal_path_version: str | None = None
    signal_path_sha256: str | None = None
    negotiated_mode_label: str | None = None
    negotiated_mode_evidence_id: str | None = None
    lip_state: LipState = 'unknown'
    lip_protocol_version: str | None = None
    readback_method: LipReadbackMethod = 'unknown'
    components: tuple[LipLatencyComponent, ...] = ()
    observed_at_utc: str | None = None
    stale_after_firmware_change: bool = False
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    evidence_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'evidence_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'HDMILatencyIndicationEvidence':
        triple = (
            self.signal_path_id,
            self.signal_path_version,
            self.signal_path_sha256,
        )
        if (None in triple) and any(v is not None for v in triple):
            raise ValueError(
                'signal path id/version/sha256 must be supplied together '
                'or not at all'
            )
        if self.lip_state == 'active' and (
            self.lip_protocol_version is None
            or self.readback_method == 'unknown'
        ):
            raise ValueError(
                "lip_state 'active' requires protocol version and a real "
                'readback method — a handshake alone does not prove LIP'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.evidence_sha256:
            raise ValueError('hdmi lip evidence hash mismatch')
        return self


def build_hdmi_transport_capability(**kwargs) -> HDMITransportCapability:
    probe = HDMITransportCapability.model_construct(
        capability_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return HDMITransportCapability(
        **probe.model_dump(
            mode='python', exclude={'capability_sha256', 'schema_version'}
        ),
        capability_sha256=digest,
    )


def build_hdmi_lip_evidence(**kwargs) -> HDMILatencyIndicationEvidence:
    probe = HDMILatencyIndicationEvidence.model_construct(
        evidence_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return HDMILatencyIndicationEvidence(
        **probe.model_dump(
            mode='python', exclude={'evidence_sha256', 'schema_version'}
        ),
        evidence_sha256=digest,
    )
