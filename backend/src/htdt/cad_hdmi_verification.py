"""HDMI system design & field-verification authority (issue #583).

This module owns the gap between *advertised capability* and
*demonstrated behavior* for one HDMI route in one required mode:

- the route/port topology itself is NOT re-modeled — it is pinned by
  the #570 ``AVSignalPath`` triple (id/version/sha256), and the
  required mode is pinned by an explicit :class:`HDMISignalProfile`;
- *advertised* per-port capability already lives in
  ``HDMITransportCapability`` (#1041) and negotiated-mode observations
  in ``NegotiatedModeEvidence`` (#570); this module adds the remaining
  evidence classes the issue requires — raw EDID artifacts with
  versioned interpretation, HDCP observations (no circumvention),
  link-mode/rate/error observations, per-hop verification, stress
  results — and seals them into :class:`HDMIVerificationRecord`;
- :func:`evaluate_hdmi_qualification` then derives a fail-closed
  verdict that keeps *theoretical support* (capability intersection)
  permanently distinct from *field verification* (observed evidence).
  A route that looks fine on paper is ``theoretically_supported`` —
  never ``verified`` — until bound observations say otherwise.

Mechanically enforced rules:

- An EDID artifact is the raw bytes' hash plus a versioned parser
  identity; a synthesized/spoofed EDID flag is first-class evidence,
  not a footnote.
- HDCP observations record only what is observable at the interface —
  negotiated version, authentication state, repeater depth, failure
  counters. No keys, no content, no circumvention paths exist in the
  schema.
- Error counters/retraining events stay ``None`` when the port cannot
  expose them — an unobservable counter is UNKNOWN, never zero.
- A diagnostic bypass test (e.g. source directly to display) is its
  own sealed record and can never mutate the failed-route evidence —
  both are preserved.
- ``rp28`` is recorded as an external profile reference with honest
  ``unpopulated`` requirement mappings until the licensed document
  text is lawfully sourced — no implied CEDIA/CTA certification.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_hdmi_transport import (
    CableClass,
    HdmiFeature,
    RateEvidenceClass,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


HDMI_SIGNAL_PROFILE_AUTHORITY_VERSION = 'hdmi-signal-profile-1'
EDID_ARTIFACT_AUTHORITY_VERSION = 'hdmi-edid-artifact-1'
HDCP_OBSERVATION_AUTHORITY_VERSION = 'hdmi-hdcp-observation-1'
LINK_OBSERVATION_AUTHORITY_VERSION = 'hdmi-link-observation-1'
HDMI_VERIFICATION_AUTHORITY_VERSION = 'hdmi-verification-1'
HDMI_QUALIFICATION_AUTHORITY_VERSION = 'hdmi-qualification-1'
RP28_PROFILE_AUTHORITY_VERSION = 'rp28-profile-1'

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
# Required signal profile (what the route must demonstrably carry)
# ---------------------------------------------------------------------------


class HDMISignalProfile(BaseModel):
    """Sealed *required* signal profile for verification (#583 §3).

    ``MediaPlaybackCondition`` models the playback request; this profile
    is the *verification contract*: the exact mode + feature set the
    route must demonstrably sustain, including requirements the playback
    model does not carry (HDCP level, LIP, QMS, eARC layout, link-rate
    floor). Verification without a bound profile is impossible — a route
    is never judged "OK in general".
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-signal-profile-1'
    ] = HDMI_SIGNAL_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)

    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    refresh_hz: float = Field(gt=0.0)
    chroma_subsampling: str | None = None
    bit_depth: int | None = Field(default=None, gt=0)
    hdr_format: str | None = None
    required_bandwidth_gbps: float | None = Field(default=None, gt=0.0)
    """Payload bandwidth the mode requires — compared against link
    rate evidence, never against marketing labels."""
    required_features: tuple[HdmiFeature, ...] = ()
    """Features this verification requires (vrr/allm/qms/qft/lip/earc
    …) — a mode that needs them cannot verify on ports without
    declared support."""
    hdcp_required: Literal['none', 'hdcp_1_4', 'hdcp_2_2', 'hdcp_2_3'] = (
        'none'
    )
    audio_format: str | None = None
    audio_channels: int | None = Field(default=None, gt=0)
    requires_earc: bool = False
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'profile_id', 'profile_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HDMISignalProfile':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if self.requires_earc and 'earc' not in self.required_features:
            raise ValueError(
                'requires_earc must be reflected in required_features'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('hdmi signal profile hash mismatch')
        if self.profile_id != f'hdmiprof:{expected}':
            raise ValueError('hdmi signal profile id mismatch')
        return self


def build_hdmi_signal_profile(
    *, created_at_utc: str | None = None, **kwargs: Any
) -> HDMISignalProfile:
    probe = HDMISignalProfile.model_construct(
        **canonicalize_payload(
            HDMISignalProfile,
            dict(
                schema_version=1,
                authority_version=HDMI_SIGNAL_PROFILE_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='0' * 64,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return HDMISignalProfile(
        **probe.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        ),
        profile_id=f'hdmiprof:{sha}',
        profile_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Evidence artifacts: EDID / HDCP / link state
# ---------------------------------------------------------------------------

EDIDInterceptionKind = Literal[
    'native_sink', 'repeated', 'synthesized', 'emulated', 'unknown'
]
"""How the EDID presented to the source was produced (#583 §5). A
synthesized/emulated EDID (EDID management, matrix, capture device)
is recorded, not hidden — it changes which capabilities are real."""


class EDIDArtifact(BaseModel):
    """Raw EDID bytes bound to a route, with versioned interpretation.

    The artifact is the *bytes* (sha256) plus the parser identity that
    interpreted them — the same bytes re-parsed by a newer parser is a
    different interpretation record, never a silent overwrite.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-edid-artifact-1'
    ] = EDID_ARTIFACT_AUTHORITY_VERSION
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    signal_path_version: str = Field(min_length=1)
    signal_path_sha256: str = Field(pattern=_SHA256)
    device_ref: str | None = None
    """Device whose EDID this is (or the intermediate that presents it)."""
    raw_sha256: str = Field(pattern=_SHA256)
    """SHA-256 of the raw EDID block as captured — the artifact of
    record; the bytes themselves are stored out-of-band."""
    capture_method: str | None = None
    interception_kind: EDIDInterceptionKind = 'unknown'
    parser_id: str | None = None
    parser_version: str | None = None
    """Parser identity+version that produced ``interpreted_fields`` —
    interpretation is versioned, not canonical."""
    interpreted_fields: tuple[str, ...] = ()
    """Compact normalized summary (e.g. ``cta:vsdb:max_tmds=600``);
    detailed decode lives with the parser, not here."""
    notes: str | None = None
    captured_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'artifact_id', 'artifact_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'EDIDArtifact':
        _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        if self.interpreted_fields and self.parser_version is None:
            raise ValueError(
                'interpreted EDID fields require a parser_version — '
                'decode is never anonymous'
            )
        expected = _hash(self.identity_payload())
        if self.artifact_sha256 != expected:
            raise ValueError('edid artifact hash mismatch')
        if self.artifact_id != f'edid:{expected}':
            raise ValueError('edid artifact id mismatch')
        return self


def build_edid_artifact(**kwargs: Any) -> EDIDArtifact:
    probe = EDIDArtifact.model_construct(
        **canonicalize_payload(
            EDIDArtifact,
            dict(
                schema_version=1,
                authority_version=EDID_ARTIFACT_AUTHORITY_VERSION,
                artifact_id='',
                artifact_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return EDIDArtifact(
        **probe.model_dump(
            mode='python', exclude={'artifact_id', 'artifact_sha256'}
        ),
        artifact_id=f'edid:{sha}',
        artifact_sha256=sha,
    )


HDCPAuthState = Literal[
    'not_required',
    'unauthenticated',
    'authenticated',
    'failed',
    'unknown',
]
"""Observable HDCP link state only (#583 §6). This authority records
interoperability evidence — version negotiated, authentication success/
failure, topology depth — and nothing else: no keys, no content paths,
no circumvention data exist in the schema."""


class HDCPStateObservation(BaseModel):
    """One HDCP interoperability observation on a bound path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-hdcp-observation-1'
    ] = HDCP_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    signal_path_version: str = Field(min_length=1)
    signal_path_sha256: str = Field(pattern=_SHA256)
    negotiated_version: Literal[
        'none', 'hdcp_1_4', 'hdcp_2_2', 'hdcp_2_3', 'unknown'
    ] = 'unknown'
    auth_state: HDCPAuthState = 'unknown'
    repeater_depth: int | None = Field(default=None, ge=0)
    authentication_failures: int | None = Field(default=None, ge=0)
    """Observed failure counter — None means unobservable, never 0."""
    observed_at_utc: str = Field(min_length=1)
    method: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'observation_id', 'observation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HDCPStateObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.auth_state == 'authenticated' and (
            self.negotiated_version in ('none', 'unknown')
        ):
            raise ValueError(
                'an authenticated HDCP state requires a negotiated '
                'version — do not fabricate the version'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('hdcp observation hash mismatch')
        if self.observation_id != f'hdcpobs:{expected}':
            raise ValueError('hdcp observation id mismatch')
        return self


def build_hdcp_observation(**kwargs: Any) -> HDCPStateObservation:
    probe = HDCPStateObservation.model_construct(
        **canonicalize_payload(
            HDCPStateObservation,
            dict(
                schema_version=1,
                authority_version=HDCP_OBSERVATION_AUTHORITY_VERSION,
                observation_id='',
                observation_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return HDCPStateObservation(
        **probe.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        ),
        observation_id=f'hdcpobs:{sha}',
        observation_sha256=sha,
    )


LinkMode = Literal['tmds', 'frl', 'unknown']
"""Electrical link mode actually trained (TMDS vs FRL lanes)."""


class LinkStateObservation(BaseModel):
    """One link-mode/rate/error observation on a bound path (#583 §7).

    Every numeric field is optional-by-design: when the port cannot
    report it the field is None = UNKNOWN. Fabricated zeros are not
    representable.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-link-observation-1'
    ] = LINK_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    signal_path_version: str = Field(min_length=1)
    signal_path_sha256: str = Field(pattern=_SHA256)
    link_mode: LinkMode = 'unknown'
    negotiated_rate_gbps: float | None = Field(default=None, gt=0.0)
    rate_evidence_class: RateEvidenceClass = 'unknown'
    """Evidence class of the reported rate — certified/measured vs
    marketing vs unknown (#1041 convention)."""
    dsc_active: Literal['yes', 'no', 'unknown'] = 'unknown'
    tmds_error_count: int | None = Field(default=None, ge=0)
    frl_error_count: int | None = Field(default=None, ge=0)
    retraining_events: int | None = Field(default=None, ge=0)
    hotplug_events: int | None = Field(default=None, ge=0)
    dropout_events: int | None = Field(default=None, ge=0)
    observed_duration_seconds: float | None = Field(
        default=None, gt=0.0
    )
    observed_at_utc: str = Field(min_length=1)
    method: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'observation_id', 'observation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'LinkStateObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.negotiated_rate_gbps is not None and (
            self.rate_evidence_class in ('unknown', 'marketing')
        ):
            raise ValueError(
                'a negotiated link rate requires real evidence — '
                'marketing/unknown cannot assert a rate'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('link observation hash mismatch')
        if self.observation_id != f'linkobs:{expected}':
            raise ValueError('link observation id mismatch')
        return self


def build_link_observation(**kwargs: Any) -> LinkStateObservation:
    probe = LinkStateObservation.model_construct(
        **canonicalize_payload(
            LinkStateObservation,
            dict(
                schema_version=1,
                authority_version=LINK_OBSERVATION_AUTHORITY_VERSION,
                observation_id='',
                observation_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return LinkStateObservation(
        **probe.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        ),
        observation_id=f'linkobs:{sha}',
        observation_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Verification record (field verification of one route+profile)
# ---------------------------------------------------------------------------

HDMIVerificationVerdict = Literal[
    'pass_stable',
    'pass_with_limitations',
    'intermittent',
    'fail_negotiation',
    'fail_link',
    'fail_feature',
    'insufficient_evidence',
]
"""Field-verification verdicts (#583 §9). ``intermittent`` is a
first-class outcome (dropouts/retraining under stress), not collapsed
into pass/fail."""

HDMIFailureReason = Literal[
    'source',
    'sink',
    'intermediate_device',
    'cable',
    'edid',
    'hdcp',
    'firmware',
    'bandwidth',
    'intermittent',
    'unknown',
]
"""Cause taxonomy for non-passing verdicts (#583 §10)."""

StressScenario = Literal[
    'sustained_playback',
    'thermal_so',
    'source_switching',
    'mode_switching',
    'power_cycle',
    'cable_flex',
    'other',
]


class HopVerification(BaseModel):
    """Per-hop verification note: which hop passed/failed and by what
    evidence — keeps blame attached to the hop, not the route."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    edge_id: str = Field(min_length=1)
    """Edge id inside the bound ``AVSignalPath``."""
    status: Literal['verified', 'failed', 'not_observed'] = 'not_observed'
    cable_class_observed: CableClass | None = None
    cable_certification_ref: str | None = None
    """Bound ``InstalledCableCertification`` (#1078) where present."""
    evidence_refs: tuple[str, ...] = ()
    note: str | None = None


class DiagnosticBypassTest(BaseModel):
    """A diagnostic isolation run (e.g. source→display direct) — its own
    sealed record that never mutates the failed route's evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    bypass_label: str = Field(min_length=1)
    removed_elements: tuple[str, ...] = ()
    """Route elements bypassed (edge ids / device refs)."""
    result: Literal['worked', 'failed', 'inconclusive']
    evidence_refs: tuple[str, ...] = ()
    note: str | None = None


class HDMIVerificationRecord(BaseModel):
    """Sealed field-verification record for one route under one required
    profile (#583 §8-§10).

    Binds the path triple, the required-mode profile, and every artifact
    the verdict rests on (EDID, HDCP, link observations, negotiated-mode
    evidence, per-hop results, stress runs, bypass tests). The verdict
    is asserted evidence — ``insufficient_evidence`` is the honest floor
    and requires no further justification.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-verification-1'
    ] = HDMI_VERIFICATION_AUTHORITY_VERSION
    record_id: str = Field(min_length=1)
    record_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    signal_path_version: str = Field(min_length=1)
    signal_path_sha256: str = Field(pattern=_SHA256)
    required_profile_id: str = Field(min_length=1)
    required_profile_sha256: str = Field(pattern=_SHA256)

    verdict: HDMIVerificationVerdict
    failure_reasons: tuple[HDMIFailureReason, ...] = ()
    reason_detail: str | None = None

    edid_artifact_id: str | None = None
    edid_artifact_sha256: str | None = Field(default=None, pattern=_SHA256)
    hdcp_observation_id: str | None = None
    hdcp_observation_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    link_observation_id: str | None = None
    link_observation_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    negotiated_mode_evidence_id: str | None = None
    """Bound ``NegotiatedModeEvidence`` (#570) where the negotiated
    mode was observed."""

    hop_verifications: tuple[HopVerification, ...] = ()
    stress_scenarios: tuple[StressScenario, ...] = ()
    stress_duration_seconds: float | None = Field(default=None, gt=0.0)
    stress_note: str | None = None
    bypass_tests: tuple[DiagnosticBypassTest, ...] = ()
    limitations: tuple[str, ...] = ()
    """What limits a ``pass_with_limitations`` (e.g. 'only at 4:2:0',
    'no VRR')."""

    observed_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'record_id', 'record_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HDMIVerificationRecord':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        for pair in (
            ('edid_artifact_id', 'edid_artifact_sha256'),
            ('hdcp_observation_id', 'hdcp_observation_sha256'),
            ('link_observation_id', 'link_observation_sha256'),
        ):
            left, right = getattr(self, pair[0]), getattr(self, pair[1])
            if (left is None) != (right is None):
                raise ValueError(
                    f'{pair[0]} and {pair[1]} must be supplied together '
                    'or not at all'
                )
        if self.verdict in (
            'fail_negotiation', 'fail_link', 'fail_feature',
            'intermittent',
        ) and not self.failure_reasons:
            raise ValueError(
                'a failing/intermittent verdict requires at least one '
                'failure_reason from the taxonomy'
            )
        if self.verdict == 'pass_with_limitations' and (
            not self.limitations
        ):
            raise ValueError(
                'pass_with_limitations requires the limitation list'
            )
        if self.verdict == 'pass_stable' and self.failure_reasons:
            raise ValueError(
                'a stable pass cannot carry failure reasons'
            )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('hdmi verification record hash mismatch')
        if self.record_id != f'hdmi-ver:{expected}':
            raise ValueError('hdmi verification record id mismatch')
        return self


def build_hdmi_verification_record(**kwargs: Any) -> HDMIVerificationRecord:
    probe = HDMIVerificationRecord.model_construct(
        **canonicalize_payload(
            HDMIVerificationRecord,
            dict(
                schema_version=1,
                authority_version=HDMI_VERIFICATION_AUTHORITY_VERSION,
                record_id='',
                record_sha256='0' * 64,
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return HDMIVerificationRecord(
        **probe.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'}
        ),
        record_id=f'hdmi-ver:{sha}',
        record_sha256=sha,
    )


# ---------------------------------------------------------------------------
# RP28 external profile (#583 §14)
# ---------------------------------------------------------------------------


class Rp28RequirementDomain(BaseModel):
    """One RP28 requirement domain (e.g. cable selection, equalization,
    verification procedure). ``mapping_status`` honestly records whether
    the requirement text was lawfully populated — CEDIA/CTA RP28 is
    licensed material: until sourced, domains stay ``unpopulated`` and
    no conformance is implied.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    domain_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    mapping_status: Literal[
        'unpopulated_pending_lawful_source',
        'mapped',
        'not_applicable',
    ] = 'unpopulated_pending_lawful_source'
    requirement_text: str | None = None
    local_evidence_refs: tuple[str, ...] = ()
    note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'Rp28RequirementDomain':
        if self.mapping_status == 'mapped' and not self.requirement_text:
            raise ValueError(
                'a mapped RP28 domain requires the sourced requirement '
                'text'
            )
        return self


class Rp28VerificationProfile(BaseModel):
    """Sealed pointer to CEDIA/CTA RP28 (HDMI System Design &
    Verification Recommended Practice) with per-domain requirement
    mappings (#583 §14).

    Referencing RP28 is never a certification claim — the profile
    records which requirement domains exist and which are populated.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'rp28-profile-1'
    ] = RP28_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    document_id: str | None = None
    label: str = Field(min_length=1)
    standard_id: str = 'cedia-cta-rp28'
    """ExternalStandardDocument id in the #599 registry."""
    publisher: str = 'CEDIA/CTA'
    document_designation: str = 'CTA-RP28'
    edition: str | None = None
    requirement_domains: tuple[Rp28RequirementDomain, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'profile_id', 'profile_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'Rp28VerificationProfile':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('rp28 profile hash mismatch')
        if self.profile_id != f'rp28:{expected}':
            raise ValueError('rp28 profile id mismatch')
        return self


def build_rp28_profile(
    *, created_at_utc: str | None = None, **kwargs: Any
) -> Rp28VerificationProfile:
    probe = Rp28VerificationProfile.model_construct(
        **canonicalize_payload(
            Rp28VerificationProfile,
            dict(
                schema_version=1,
                authority_version=RP28_PROFILE_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='0' * 64,
                created_at_utc=created_at_utc or _utc_now(),
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return Rp28VerificationProfile(
        **probe.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        ),
        profile_id=f'rp28:{sha}',
        profile_sha256=sha,
    )


def seed_rp28_profile(
    *, created_at_utc: str | None = None
) -> Rp28VerificationProfile:
    """RP28 shell profile with the known requirement domains honestly
    ``unpopulated`` — the licensed text is not reproduced; each domain
    becomes ``mapped`` only when requirement text is lawfully sourced.
    """
    domains = tuple(
        Rp28RequirementDomain(domain_id=d, label=label)
        for d, label in (
            ('rp28-system-design', 'HDMI system design requirements'),
            ('rp28-cable-selection', 'Cable type/length selection'),
            ('rp28-link-budget', 'Link budget & equalization'),
            ('rp28-edid-hdcp', 'EDID/HDCP interoperability checks'),
            ('rp28-field-verification', 'Field verification procedure'),
            ('rp28-documentation', 'Verification documentation'),
        )
    )
    return build_rp28_profile(
        label='CEDIA/CTA RP28 HDMI system design & verification',
        edition=None,
        requirement_domains=domains,
        created_at_utc=created_at_utc,
    )


# ---------------------------------------------------------------------------
# Derived qualification — theoretical vs verified
# ---------------------------------------------------------------------------

HDMIQualificationVerdict = Literal[
    'verified',
    'verified_with_limitations',
    'failed',
    'insufficient_evidence',
    'stale',
    'theoretically_supported_unverified',
]
"""System-level verdicts (#583 §9/§13). ``theoretically_supported_unverified``
is the honest state when capability intersection says possible but no
field-verification record exists — it is never collapsed into
``verified``."""


class HDMIQualification(BaseModel):
    """Sealed system-level qualification: route × required profile.

    Binds the exact path revision, the required profile, the theoretical
    evaluation result, and the verification record it rests on.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'hdmi-qualification-1'
    ] = HDMI_QUALIFICATION_AUTHORITY_VERSION
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    signal_path_id: str = Field(min_length=1)
    signal_path_version: str = Field(min_length=1)
    signal_path_sha256: str = Field(pattern=_SHA256)
    required_profile_id: str = Field(min_length=1)
    required_profile_sha256: str = Field(pattern=_SHA256)
    theoretical_status: Literal[
        'supported', 'unsupported', 'unknown'
    ]
    """Theoretical capability-intersection result (#570 evaluation) —
    kept as its own field so "supported on paper" is never confused
    with "verified in the field"."""
    verification_record_id: str | None = None
    verification_record_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    verdict: HDMIQualificationVerdict
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'HDMIQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if (
            self.verification_record_id is None
        ) != (self.verification_record_sha256 is None):
            raise ValueError(
                'verification record id/sha256 must be supplied together'
            )
        if self.verdict in ('verified', 'verified_with_limitations') and (
            self.verification_record_id is None
        ):
            raise ValueError(
                'a verified verdict requires a bound verification '
                'record — theoretical support alone never verifies'
            )
        if self.verdict == 'theoretically_supported_unverified' and (
            self.theoretical_status != 'supported'
        ):
            raise ValueError(
                'theoretically_supported_unverified requires a '
                'supported theoretical intersection'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('hdmi qualification hash mismatch')
        if self.qualification_id != f'hdmiq:{expected}':
            raise ValueError('hdmi qualification id mismatch')
        return self


def evaluate_hdmi_qualification(
    *,
    document_id: str,
    path_id: str,
    path_version: str,
    path_sha256: str,
    profile: HDMISignalProfile,
    theoretical_status: Literal['supported', 'unsupported', 'unknown'],
    record: HDMIVerificationRecord | None = None,
    evaluated_at_utc: str | None = None,
) -> HDMIQualification:
    """Derive the system verdict for route × profile.

    Fail-closed ordering (#583 §13):

    1. theoretical intersection ``unsupported`` -> ``failed`` (the
       declared/estimated stack cannot carry the required profile);
    2. no verification record -> ``theoretically_supported_unverified``
       if the intersection says supported, else ``insufficient_evidence``;
    3. record pins a different path revision -> ``stale``;
    4. record verdict maps: ``pass_stable`` -> ``verified``;
       ``pass_with_limitations`` -> ``verified_with_limitations``;
       ``intermittent``/``fail_*`` -> ``failed`` (with reasons);
       ``insufficient_evidence`` -> ``insufficient_evidence``.
    """
    reasons: list[str] = []
    verdict: HDMIQualificationVerdict

    if theoretical_status == 'unsupported':
        verdict = 'failed'
        reasons.append(
            'theoretical capability intersection does not support the '
            'required profile — no field test can rescue it'
        )
    elif record is None:
        if theoretical_status == 'supported':
            verdict = 'theoretically_supported_unverified'
            reasons.append(
                'declared capability supports the profile but no field '
                'verification record exists'
            )
        else:
            verdict = 'insufficient_evidence'
            reasons.append(
                'capability is unknown and no verification record '
                'exists'
            )
    elif record.signal_path_sha256 != path_sha256 or (
        record.signal_path_id != path_id
        or record.signal_path_version != path_version
    ):
        verdict = 'stale'
        reasons.append(
            'the verification record predates the current path revision'
        )
    elif record.required_profile_sha256 != profile.profile_sha256:
        verdict = 'insufficient_evidence'
        reasons.append(
            'the verification record was produced under a different '
            'required profile'
        )
    elif record.verdict == 'pass_stable':
        verdict = 'verified'
        reasons.append('field verification passed stably')
    elif record.verdict == 'pass_with_limitations':
        verdict = 'verified_with_limitations'
        reasons.extend(f'limitation: {l}' for l in record.limitations)
    elif record.verdict == 'insufficient_evidence':
        verdict = 'insufficient_evidence'
        reasons.append('verification record reports insufficient evidence')
    else:
        verdict = 'failed'
        reasons.append(
            f'field verification {record.verdict}: '
            + ', '.join(record.failure_reasons)
        )

    probe = HDMIQualification.model_construct(
        **canonicalize_payload(
            HDMIQualification,
            dict(
                schema_version=1,
                authority_version=HDMI_QUALIFICATION_AUTHORITY_VERSION,
                qualification_id='',
                qualification_sha256='0' * 64,
                document_id=document_id,
                signal_path_id=path_id,
                signal_path_version=path_version,
                signal_path_sha256=path_sha256,
                required_profile_id=profile.profile_id,
                required_profile_sha256=profile.profile_sha256,
                theoretical_status=theoretical_status,
                verification_record_id=(
                    record.record_id if record else None
                ),
                verification_record_sha256=(
                    record.record_sha256 if record else None
                ),
                verdict=verdict,
                reasons=tuple(reasons),
                evaluated_at_utc=evaluated_at_utc or _utc_now(),
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return HDMIQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        ),
        qualification_id=f'hdmiq:{sha}',
        qualification_sha256=sha,
    )


__all__ = [
    'DiagnosticBypassTest',
    'EDIDArtifact',
    'EDIDInterceptionKind',
    'HDCPAuthState',
    'HDCPStateObservation',
    'HDMIFailureReason',
    'HDMIQualification',
    'HDMIQualificationVerdict',
    'HDMISignalProfile',
    'HDMIVerificationRecord',
    'HDMIVerificationVerdict',
    'HopVerification',
    'LinkMode',
    'LinkStateObservation',
    'Rp28RequirementDomain',
    'Rp28VerificationProfile',
    'StressScenario',
    'EDID_ARTIFACT_AUTHORITY_VERSION',
    'HDCP_OBSERVATION_AUTHORITY_VERSION',
    'HDMI_QUALIFICATION_AUTHORITY_VERSION',
    'HDMI_SIGNAL_PROFILE_AUTHORITY_VERSION',
    'HDMI_VERIFICATION_AUTHORITY_VERSION',
    'LINK_OBSERVATION_AUTHORITY_VERSION',
    'RP28_PROFILE_AUTHORITY_VERSION',
    'build_edid_artifact',
    'build_hdcp_observation',
    'build_hdmi_signal_profile',
    'build_hdmi_verification_record',
    'build_link_observation',
    'build_rp28_profile',
    'evaluate_hdmi_qualification',
    'seed_rp28_profile',
]
