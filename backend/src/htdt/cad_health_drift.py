"""Post-commissioning health / drift monitoring authority (#595).

Commissioning and the #568 ``cad_system_health`` baseline prove the
theater was correct at handover. The operational question that follows is
*does the real system still match the state we designed and verified?* —
and answering it needs lifecycle evidence the acoustic health checks alone
do not cover: device telemetry, firmware/config transitions, service
events, room changes and recurring symptom episodes.

This module owns **baseline-relative health/drift semantics and
re-verification orchestration**. It never creates a second truth store:
every baseline component is an exact pin into the existing authorities —
the #592 ``DeviceKnownGoodBaseline`` / ``DeviceConfigurationSnapshot``
state digest, the #568 ``SystemHealthBaseline`` measurement envelope, the
SceneRevision as-built identity and commissioning record refs.

Contract points honoured:

- ``device online`` is never ``system healthy`` — online state is one
  observation kind among many and carries no performance semantics;
- devices differ in observability: the monitoring-capability taxonomy
  (``active_telemetry`` / ``pollable_readback`` / ``event_log_export`` /
  ``manual_service_observation`` / ``periodic_measurement_only`` /
  ``unobservable``) is declared per device, and an unobservable device is
  classified ``insufficient_observability``, never ``hard_failure``;
- drift, intermittent fault, hard failure and stale evidence are
  separate classifications — no single opaque ``health score`` exists;
- ``no_material_change`` is claimable only when a comparison actually
  ran against a comparable pin — incomparable inputs classify
  ``not_comparable``, and missing capability classifies
  ``insufficient_observability``;
- a state change with no matching logged change event is never assigned
  a fabricated cause — it produces ``configuration_drift`` and an
  investigation trigger; a change with a matching authorised
  :class:`ChangeEventRecord` is ``expected_change``;
- staleness is dependency-aware: a :class:`DependencyImpactRule` maps the
  changed component domain to exactly the authority refs it invalidates;
  anything changed without covering dependency knowledge becomes
  ``review_required`` rather than silently staying valid;
- trend semantics are descriptive (baseline descriptor, recent series,
  drift slope, step change, outlier count, uncertainty) with
  versioned/profile-scoped thresholds — no Gaussian/iid assumption is
  made and no degradation is declared inside measurement variability;
- a :class:`SymptomEpisode` correlates evidence streams for diagnosis;
  ``reason_state='confirmed'`` requires confirming evidence refs —
  correlation never upgrades itself into proven causality;
- operational severity (``operational_severity``) and evidence certainty
  (``evidence_certainty``) are independent fields;
- remote monitoring is authorization/privacy bounded — a
  :class:`MonitoringAuthorization` is required for ``remote_service``
  collection and payload exclusion/redaction is first-class;
- history is append-only: observations, change events, episodes,
  assessments, triggers and confirmations are retained after resolution.

Literature basis (see docs/reviews/rev56-lifecycle.md):

- AVIXA post-handover guidance: remote monitoring exposes online state,
  temperature, network, firmware, peripherals, error logs, uptime,
  licence, storage and recurring faults — none of which proves
  commissioned performance by itself;
- NIST SP 800-53 CM-2(2): an approved baseline is a versioned artifact;
  drift = the difference between approved baseline and actual state,
  reconciled against authorised change records, with evidence retained;
- ILAC-G24 / OIML D 10 + ISO/IEC 17025: intermediate checks between full
  calibrations confirm "no alteration which could introduce doubt"; a
  light reference check is deliberately not a full requalification.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import (
    canonical_json as _canonical,
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


LIFECYCLE_SCHEMA_VERSION = 1

MONITORING_DECLARATION_AUTHORITY_VERSION = 'rev56-monitoring-declaration-1'
LIFECYCLE_OBSERVATION_AUTHORITY_VERSION = 'rev56-lifecycle-observation-1'
CHANGE_EVENT_AUTHORITY_VERSION = 'rev56-change-event-1'
TREND_ASSESSMENT_AUTHORITY_VERSION = 'rev56-trend-assessment-1'
SYMPTOM_EPISODE_AUTHORITY_VERSION = 'rev56-symptom-episode-1'
DRIFT_ASSESSMENT_AUTHORITY_VERSION = 'rev56-drift-assessment-1'
REVERIFICATION_TRIGGER_AUTHORITY_VERSION = 'rev56-reverification-trigger-1'
RESTORE_CONFIRMATION_AUTHORITY_VERSION = 'rev56-restore-confirmation-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomy (#595 §2, §3, §4, §10, §14)
# ---------------------------------------------------------------------------

#: What a lifecycle observation reports (#595 §2). Online/telemetry/
#: config/performance/measurement observations are distinct kinds —
#: they never collapse into one "healthy" signal.
ObservationKind = Literal[
    'device_online_state',
    'firmware_version',
    'config_hash_state',
    'device_error_warning',
    'temperature_fan_state',
    'power_ups_state',
    'network_link_quality',
    'clock_ptp_state',
    'hdmi_link_state',
    'license_feature_state',
    'storage_resource_state',
    'reboot_uptime_event',
    'av_self_test_result',
    'user_reported_symptom',
    'field_measurement_check',
    'room_as_built_change',
]

#: How observable one device/subject is (#595 §3). Coverage and
#: qualification are separate concepts: ``unobservable`` never means
#: failed.
MonitoringCapabilityClass = Literal[
    'active_telemetry',
    'pollable_readback',
    'event_log_export',
    'manual_service_observation',
    'periodic_measurement_only',
    'unobservable',
]

#: Where an observation came from — remote collection is the one path
#: that must be authorization-bounded (#595 §15).
CollectionMode = Literal[
    'local_automatic',
    'local_manual',
    'remote_service',
]

#: Drift vs failure classification per subject component (#595 §4).
#: ``not_comparable`` is separate: "no change" may only be claimed when a
#: comparison actually ran — the prompt's fail-closed rule.
DriftClassification = Literal[
    'no_material_change',
    'expected_change',
    'configuration_drift',
    'performance_drift',
    'intermittent_fault',
    'hard_failure',
    'evidence_stale',
    'insufficient_observability',
    'not_comparable',
]

_CLEAN_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'no_material_change', 'expected_change'}
)
_FAULT_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'intermittent_fault', 'hard_failure'}
)
_DRIFT_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'configuration_drift', 'performance_drift'}
)
#: Classifications where no usable comparison/evidence exists — a
#: "confirmed" certainty claim is impossible while any of these appear.
_NONCONFIRMING_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'insufficient_observability', 'not_comparable', 'evidence_stale'}
)

#: Which lifecycle domain a drifted component belongs to — used by
#: dependency rules so only the affected claims go stale (#595 §5).
LifecycleDomain = Literal[
    'acoustic',
    'video',
    'audio_transport',
    'video_transport',
    'network',
    'dsp_config',
    'control',
    'device_config',
    'power_thermal',
    'physical_room',
    'licensing',
    'other',
]

#: What happens to evidence that depended on a changed component.
ImpactDisposition = Literal[
    'stale',
    'recompute',
    'remeasure',
    'review_required',
    'unaffected',
]

#: Targeted re-verification actions (#595 §10). The recommended scope is
#: the smallest defensible campaign needed to restore confidence.
ReverificationAction = Literal[
    'no_action',
    'observe',
    'service_review',
    'restore_known_good_config',
    'run_reference_check',
    'reverify_domain',
    'full_recommission_required',
]

ReverificationTaskKind = Literal[
    'manual_observation',
    'service_investigation',
    'restore_config',
    'reference_check',
    'domain_reverification',
    'full_recommission',
]

#: Operational urgency — deliberately orthogonal to evidence certainty
#: (#595 §14). HIGH severity with SUSPECTED evidence is a valid state.
OperationalSeverity = Literal[
    'none',
    'low',
    'medium',
    'high',
    'critical',
]
_SEVERITY_ORDER: tuple[str, ...] = (
    'none', 'low', 'medium', 'high', 'critical',
)

EvidenceCertainty = Literal['confirmed', 'suspected', 'unknown']

#: Why a change event occurred / what kind of change it records
#: (#595 §9).
ChangeEventKind = Literal[
    'firmware_transition',
    'configuration_change',
    'service_visit',
    'equipment_replacement',
    'room_furniture_change',
    'network_policy_change',
    'power_rack_change',
    'calibration_filter_change',
    'other',
]

SymptomReasonState = Literal['suspected', 'confirmed', 'unknown']

RestoreConfirmationVerdict = Literal[
    'confidence_restored',
    'differences_remain',
    'confirmation_inconclusive',
    'escalate_service',
]


# ---------------------------------------------------------------------------
# Monitoring declaration (#595 §3, §15)
# ---------------------------------------------------------------------------


class MonitoringAuthorization(BaseModel):
    """Explicit authorization boundary for telemetry collection (#595 §15).

    Remote-service collection requires ``authorized_by`` and a positive
    ``minimum_scope`` statement; program/content payloads are excluded by
    construction — an observation can only claim ``payload_excluded`` or
    carry a ``redaction_note``, never the payload itself.
    """

    model_config = ConfigDict(frozen=True)

    authorized_by: str = Field(min_length=1)
    remote_collection_allowed: bool = False
    minimum_scope_note: str = Field(min_length=1)
    secrets_redacted: bool = True
    content_payloads_excluded: bool = True


class DeviceMonitoringDeclaration(BaseModel):
    """Per-device observability + collection authorization (#595 §3/§15).

    ``capabilities=()`` or ``('unobservable',)`` is an honest statement of
    zero observability — downstream assessment classifies such subjects
    ``insufficient_observability`` rather than penalizing them as failed.
    ``authorization`` is required before any ``remote_service``
    observation may reference this subject.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-monitoring-declaration-1'
    ] = MONITORING_DECLARATION_AUTHORITY_VERSION
    declaration_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    capabilities: tuple[MonitoringCapabilityClass, ...] = ()
    authorization: MonitoringAuthorization | None = None
    telemetry_scope_note: str | None = None
    declared_at_utc: str = Field(min_length=1)
    declaration_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'declaration_sha256', 'declaration_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DeviceMonitoringDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('monitoring capabilities must be unique')
        if 'unobservable' in self.capabilities and len(self.capabilities) > 1:
            raise ValueError(
                "'unobservable' may not be combined with other capabilities"
            )
        digest = _hash(self.semantic_payload())
        if self.declaration_sha256 != digest:
            raise ValueError('monitoring declaration hash mismatch')
        if self.declaration_id != _semantic_id('hlmon', digest):
            raise ValueError('monitoring declaration id mismatch')
        return self

    def observable(self) -> bool:
        return bool(self.capabilities) and 'unobservable' not in self.capabilities


def build_monitoring_declaration(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    capabilities: Sequence[MonitoringCapabilityClass] = (),
    authorization: MonitoringAuthorization | None = None,
    telemetry_scope_note: str | None = None,
    declared_at_utc: str | None = None,
) -> DeviceMonitoringDeclaration:
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=MONITORING_DECLARATION_AUTHORITY_VERSION,
        declaration_id='',
        document_id=document_id,
        subject_ref=subject_ref,
        capabilities=tuple(capabilities),
        authorization=authorization,
        telemetry_scope_note=telemetry_scope_note,
        declared_at_utc=declared_at_utc or _utc_now(),
        declaration_sha256='0' * 64,
    )
    probe = DeviceMonitoringDeclaration.model_construct(
        **canonicalize_payload(DeviceMonitoringDeclaration, payload)
    )
    digest = _hash(probe.semantic_payload())
    return DeviceMonitoringDeclaration(
        **probe.model_dump(
            mode='python', exclude={'declaration_sha256', 'declaration_id'}
        ),
        declaration_id=_semantic_id('hlmon', digest),
        declaration_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Lifecycle observation (#595 §2)
# ---------------------------------------------------------------------------


class LifecycleObservation(BaseModel):
    """One sealed lifecycle observation against one subject.

    ``observed_repr`` is the canonical-JSON rendering of what was observed
    (e.g. ``{"online": true}`` or ``{"state_content_sha256": "..."}``).
    ``comparison_key`` names the baseline pin this observation compares
    against (for example ``device_state`` → a snapshot's
    ``state_content_sha256``); an observation that cannot name what it
    compares to can be recorded but is never used to claim change or
    no-change.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-lifecycle-observation-1'
    ] = LIFECYCLE_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    domain: LifecycleDomain
    kind: ObservationKind
    observed_repr: str | None = None
    comparison_key: str | None = None
    compared_to_repr: str | None = None
    source_tool: str = Field(min_length=1)
    collection_mode: CollectionMode = 'local_manual'
    confidence: EvidenceCertainty = 'unknown'
    payload_excluded: bool = True
    redaction_note: str | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'observation_sha256', 'observation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'LifecycleObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.compared_to_repr is not None and self.comparison_key is None:
            raise ValueError(
                'a compared baseline value requires a comparison_key'
            )
        ref_keys = {(r.kind, r.ref_id) for r in self.evidence_refs}
        if len(ref_keys) != len(self.evidence_refs):
            raise ValueError('observation evidence refs must be unique')
        digest = _hash(self.semantic_payload())
        if self.observation_sha256 != digest:
            raise ValueError('lifecycle observation hash mismatch')
        if self.observation_id != _semantic_id('hlobs', digest):
            raise ValueError('lifecycle observation id mismatch')
        return self


def build_observation(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    domain: LifecycleDomain,
    kind: ObservationKind,
    source_tool: str,
    observed_repr: str | None = None,
    comparison_key: str | None = None,
    compared_to_repr: str | None = None,
    collection_mode: CollectionMode = 'local_manual',
    confidence: EvidenceCertainty = 'unknown',
    payload_excluded: bool = True,
    redaction_note: str | None = None,
    evidence_refs: Sequence[AuthorityRef] = (),
    observed_at_utc: str | None = None,
) -> LifecycleObservation:
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=LIFECYCLE_OBSERVATION_AUTHORITY_VERSION,
        observation_id='',
        document_id=document_id,
        subject_ref=subject_ref,
        domain=domain,
        kind=kind,
        observed_repr=observed_repr,
        comparison_key=comparison_key,
        compared_to_repr=compared_to_repr,
        source_tool=source_tool,
        collection_mode=collection_mode,
        confidence=confidence,
        payload_excluded=payload_excluded,
        redaction_note=redaction_note,
        evidence_refs=tuple(evidence_refs),
        observed_at_utc=observed_at_utc or _utc_now(),
        observation_sha256='0' * 64,
    )
    probe = LifecycleObservation.model_construct(
        **canonicalize_payload(LifecycleObservation, payload)
    )
    digest = _hash(probe.semantic_payload())
    return LifecycleObservation(
        **probe.model_dump(
            mode='python', exclude={'observation_sha256', 'observation_id'}
        ),
        observation_id=_semantic_id('hlobs', digest),
        observation_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Change event log (#595 §9)
# ---------------------------------------------------------------------------


class ChangeEventRecord(BaseModel):
    """One auditable system change (firmware, service, equipment, room,
    network, power, calibration).

    A drift discovered without a matching change event is valid evidence
    for investigation — the event log never invents a cause, it only
    records what actually happened and what produced the record
    (``evidence_refs`` pin e.g. the #592 ``DeviceFirmwareTransition``).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-change-event-1'
    ] = CHANGE_EVENT_AUTHORITY_VERSION
    event_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: ChangeEventKind
    subject_refs: tuple[AuthorityRef, ...] = ()
    detail: str | None = None
    actor: str | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    occurred_at_utc: str = Field(min_length=1)
    event_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_sha256', 'event_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ChangeEventRecord':
        _require_iso8601(self.occurred_at_utc, 'occurred_at_utc')
        for name, refs in (
            ('subject', self.subject_refs),
            ('evidence', self.evidence_refs),
        ):
            keys = {(r.kind, r.ref_id) for r in refs}
            if len(keys) != len(refs):
                raise ValueError(f'change event {name} refs must be unique')
        digest = _hash(self.semantic_payload())
        if self.event_sha256 != digest:
            raise ValueError('change event hash mismatch')
        if self.event_id != _semantic_id('hlchg', digest):
            raise ValueError('change event id mismatch')
        return self


def build_change_event(
    *,
    document_id: str,
    kind: ChangeEventKind,
    subject_refs: Sequence[AuthorityRef] = (),
    detail: str | None = None,
    actor: str | None = None,
    evidence_refs: Sequence[AuthorityRef] = (),
    occurred_at_utc: str | None = None,
) -> ChangeEventRecord:
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=CHANGE_EVENT_AUTHORITY_VERSION,
        event_id='',
        document_id=document_id,
        kind=kind,
        subject_refs=tuple(subject_refs),
        detail=detail,
        actor=actor,
        evidence_refs=tuple(evidence_refs),
        occurred_at_utc=occurred_at_utc or _utc_now(),
        event_sha256='0' * 64,
    )
    probe = ChangeEventRecord.model_construct(
        **canonicalize_payload(ChangeEventRecord, payload)
    )
    digest = _hash(probe.semantic_payload())
    return ChangeEventRecord(
        **probe.model_dump(mode='python', exclude={'event_sha256', 'event_id'}),
        event_id=_semantic_id('hlchg', digest),
        event_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Trend semantics (#595 §7) — descriptive, never distributional.
# ---------------------------------------------------------------------------


class TrendSample(BaseModel):
    """One numeric sample in a repeated observation series."""

    model_config = ConfigDict(frozen=True)

    value: float
    observed_at_utc: str = Field(min_length=1)
    source: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'TrendSample':
        if not isfinite(self.value):
            raise ValueError('trend sample value must be finite')
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        return self


class TrendThresholdSpec(BaseModel):
    """Versioned, profile-scoped thresholds for one metric.

    ``band`` (absolute deviation from ``center``) is what may be called
    acceptable variation; ``step`` is the magnitude of an abrupt shift.
    A metric with no declared band cannot be declared "in drift" — the
    assessment reports ``no_threshold`` rather than guessing one.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    metric_key: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    center: float
    band: float | None = Field(default=None, gt=0.0)
    step: float | None = Field(default=None, gt=0.0)
    min_samples: int = Field(default=3, ge=1)

    @model_validator(mode='after')
    def _check(self) -> 'TrendThresholdSpec':
        for value in (self.center, self.band, self.step):
            if value is not None and not isfinite(float(value)):
                raise ValueError('trend threshold values must be finite')
        return self


class TrendAssessment(BaseModel):
    """Descriptive control-chart assessment of one metric series.

    Reports what the series did (median, deviation from declared center,
    least-squares slope per day, step flag, outliers beyond the band)
    under a named threshold spec — never a Gaussian claim. ``state`` is
    honest about missing data or missing thresholds.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-trend-assessment-1'
    ] = TREND_ASSESSMENT_AUTHORITY_VERSION
    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    spec: TrendThresholdSpec
    state: Literal[
        'within_band',
        'drift_detected',
        'step_detected',
        'insufficient_samples',
        'no_threshold',
    ]
    sample_count: int = Field(ge=0)
    median_recent: float | None = None
    deviation_from_center: float | None = None
    slope_per_day: float | None = None
    step_change: bool = False
    outlier_count: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1)
    observation_ids: tuple[str, ...] = ()
    assessed_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'TrendAssessment':
        _require_iso8601(self.assessed_at_utc, 'assessed_at_utc')
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError('trend observation ids must be unique')
        digest = _hash(self.semantic_payload())
        if self.assessment_sha256 != digest:
            raise ValueError('trend assessment hash mismatch')
        if self.assessment_id != _semantic_id('hltnd', digest):
            raise ValueError('trend assessment id mismatch')
        return self


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def assess_trend(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    spec: TrendThresholdSpec,
    samples: Sequence[TrendSample],
    observation_ids: Sequence[str] = (),
    assessed_at_utc: str | None = None,
) -> TrendAssessment:
    """Classify a numeric series against declared thresholds.

    No distribution assumption is made: the descriptor set (median,
    deviation, slope, step flag, outliers) is reported and the caller's
    declared band/step decide whether it counts as drift.
    """

    ordered = sorted(samples, key=lambda s: s.observed_at_utc)
    n = len(ordered)
    values = [s.value for s in ordered]
    median = _median(values) if n else None
    deviation = (
        None if median is None else abs(median - spec.center)
    )
    outliers = (
        0
        if spec.band is None
        else sum(1 for v in values if abs(v - spec.center) > spec.band)
    )
    slope: float | None = None
    step_change = False
    if n >= 2:
        t0 = datetime.fromisoformat(ordered[0].observed_at_utc)
        times = [
            (datetime.fromisoformat(s.observed_at_utc) - t0).total_seconds()
            / 86400.0
            for s in ordered
        ]
        mean_t = sum(times) / n
        mean_v = sum(values) / n
        denom = sum((t - mean_t) ** 2 for t in times)
        if denom > 0:
            slope = sum(
                (t - mean_t) * (v - mean_v) for t, v in zip(times, values)
            ) / denom
        if spec.step is not None:
            base = _median(values[:-1]) if n > 2 else values[0]
            step_change = abs(values[-1] - base) > spec.step

    if n < spec.min_samples:
        state: str = 'insufficient_samples'
        reason = (
            f'{n} samples below declared minimum {spec.min_samples}'
        )
    elif spec.band is None:
        state = 'no_threshold'
        reason = 'no declared tolerance band — drift cannot be asserted'
    elif step_change:
        state = 'step_detected'
        reason = 'latest sample shifted beyond declared step threshold'
    elif deviation is not None and deviation > spec.band:
        state = 'drift_detected'
        reason = (
            f'median deviation {deviation:g} exceeds declared band '
            f'{spec.band:g} {spec.unit}'
        )
    else:
        state = 'within_band'
        reason = 'series within declared tolerance band'

    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=TREND_ASSESSMENT_AUTHORITY_VERSION,
        assessment_id='',
        document_id=document_id,
        subject_ref=subject_ref,
        spec=spec,
        state=state,
        sample_count=n,
        median_recent=median,
        deviation_from_center=deviation,
        slope_per_day=slope,
        step_change=step_change,
        outlier_count=outliers,
        reason=reason,
        observation_ids=tuple(observation_ids),
        assessed_at_utc=assessed_at_utc or _utc_now(),
        assessment_sha256='0' * 64,
    )
    probe = TrendAssessment.model_construct(
        **canonicalize_payload(TrendAssessment, payload)
    )
    digest = _hash(probe.semantic_payload())
    return TrendAssessment(
        **probe.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        ),
        assessment_id=_semantic_id('hltnd', digest),
        assessment_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Symptom correlation (#595 §8)
# ---------------------------------------------------------------------------


class SymptomEpisode(BaseModel):
    """One fault episode correlating independent evidence streams.

    Correlation supports diagnosis but never proves causality:
    ``reason_state='confirmed'`` requires ``confirming_refs`` —
    post-hoc reasoning stays ``suspected`` until an evidence ref can
    attest it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-symptom-episode-1'
    ] = SYMPTOM_EPISODE_AUTHORITY_VERSION
    episode_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    symptom_observation_ids: tuple[str, ...] = ()
    correlated_observation_ids: tuple[str, ...] = ()
    reason_state: SymptomReasonState = 'unknown'
    cause_note: str | None = None
    confirming_refs: tuple[AuthorityRef, ...] = ()
    resolved: bool = False
    resolution_note: str | None = None
    recorded_at_utc: str = Field(min_length=1)
    episode_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'episode_sha256', 'episode_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SymptomEpisode':
        _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        if len(set(self.symptom_observation_ids)) != len(
            self.symptom_observation_ids
        ):
            raise ValueError('symptom observation ids must be unique')
        if len(set(self.correlated_observation_ids)) != len(
            self.correlated_observation_ids
        ):
            raise ValueError('correlated observation ids must be unique')
        if self.reason_state == 'confirmed' and not self.confirming_refs:
            raise ValueError(
                "a 'confirmed' reason requires confirming evidence refs — "
                'correlation alone never proves causality'
            )
        if self.resolved and not self.resolution_note:
            raise ValueError('a resolved episode keeps a resolution note')
        digest = _hash(self.semantic_payload())
        if self.episode_sha256 != digest:
            raise ValueError('symptom episode hash mismatch')
        if self.episode_id != _semantic_id('hlsym', digest):
            raise ValueError('symptom episode id mismatch')
        return self


def build_symptom_episode(
    *,
    document_id: str,
    symptom_observation_ids: Sequence[str] = (),
    correlated_observation_ids: Sequence[str] = (),
    reason_state: SymptomReasonState = 'unknown',
    cause_note: str | None = None,
    confirming_refs: Sequence[AuthorityRef] = (),
    resolved: bool = False,
    resolution_note: str | None = None,
    recorded_at_utc: str | None = None,
) -> SymptomEpisode:
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=SYMPTOM_EPISODE_AUTHORITY_VERSION,
        episode_id='',
        document_id=document_id,
        symptom_observation_ids=tuple(symptom_observation_ids),
        correlated_observation_ids=tuple(correlated_observation_ids),
        reason_state=reason_state,
        cause_note=cause_note,
        confirming_refs=tuple(confirming_refs),
        resolved=resolved,
        resolution_note=resolution_note,
        recorded_at_utc=recorded_at_utc or _utc_now(),
        episode_sha256='0' * 64,
    )
    probe = SymptomEpisode.model_construct(
        **canonicalize_payload(SymptomEpisode, payload)
    )
    digest = _hash(probe.semantic_payload())
    return SymptomEpisode(
        **probe.model_dump(
            mode='python', exclude={'episode_sha256', 'episode_id'}
        ),
        episode_id=_semantic_id('hlsym', digest),
        episode_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Dependency-aware staleness (#595 §5)
# ---------------------------------------------------------------------------


class DependencyImpactRule(BaseModel):
    """Declared dependency knowledge: when a component in
    ``trigger_domain`` classifies as drift/fault, exactly the listed
    authority refs carry ``disposition``.

    Rules are the project's declared dependency knowledge — the source of
    truth is wherever the dependency was authored (equipment bindings,
    qualification scenarios); a rule only names what to invalidate, never
    re-decides the evidence itself.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(min_length=1)
    trigger_domain: LifecycleDomain
    trigger_classifications: tuple[DriftClassification, ...] = Field(
        min_length=1
    )
    affected_ref: AuthorityRef
    disposition: ImpactDisposition
    note: str | None = None


class StaleEvidenceMark(BaseModel):
    """One authority ref marked by a fired dependency rule (or by the
    unmapped-dependency fallback)."""

    model_config = ConfigDict(frozen=True)

    affected_ref: AuthorityRef
    disposition: ImpactDisposition
    reason: str = Field(min_length=1)
    via_rule_id: str | None = None


class DriftComponent(BaseModel):
    """Classification of one observed domain component for the subject."""

    model_config = ConfigDict(frozen=True)

    domain: LifecycleDomain
    classification: DriftClassification
    observation_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1)
    matched_change_event_id: str | None = None
    severity: OperationalSeverity = 'none'


class DriftAssessment(BaseModel):
    """Sealed drift verdict for one subject over one observation window.

    Per-component classifications are the record — there is deliberately
    no aggregate health score. ``dependency_status`` reports how much of
    the change was covered by declared dependency rules; unmapped changes
    mark their declared dependents ``review_required`` instead of
    pretending continued validity.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-drift-assessment-1'
    ] = DRIFT_ASSESSMENT_AUTHORITY_VERSION
    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    baseline_refs: tuple[AuthorityRef, ...] = ()
    components: tuple[DriftComponent, ...] = ()
    stale_marks: tuple[StaleEvidenceMark, ...] = ()
    dependency_status: Literal['mapped', 'partially_mapped', 'unmapped']
    observation_ids: tuple[str, ...] = ()
    change_event_ids: tuple[str, ...] = ()
    operational_severity: OperationalSeverity
    evidence_certainty: EvidenceCertainty
    assessed_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DriftAssessment':
        _require_iso8601(self.assessed_at_utc, 'assessed_at_utc')
        if not self.components:
            raise ValueError('drift assessment requires ≥1 component')
        for name, refs in (('baseline', self.baseline_refs),):
            keys = {(r.kind, r.ref_id) for r in refs}
            if len(keys) != len(refs):
                raise ValueError(f'drift {name} refs must be unique')
        seen_domains: set[str] = set()
        for component in self.components:
            if component.domain in seen_domains:
                raise ValueError('drift components must be unique per domain')
            seen_domains.add(component.domain)
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError('assessed observation ids must be unique')
        if len(set(self.change_event_ids)) != len(self.change_event_ids):
            raise ValueError('referenced change event ids must be unique')
        mark_keys = {(m.affected_ref.kind, m.affected_ref.ref_id) for m in self.stale_marks}
        if len(mark_keys) != len(self.stale_marks):
            raise ValueError('stale marks must be unique per authority ref')
        # Severity may never be lower than the worst component severity —
        # and 'none' is impossible while any component is non-clean.
        if any(
            c.classification not in _CLEAN_CLASSIFICATIONS
            for c in self.components
        ) and self.operational_severity == 'none':
            raise ValueError(
                'non-clean components require a non-none operational severity'
            )
        worst_component = max(
            (_SEVERITY_ORDER.index(c.severity) for c in self.components),
            default=0,
        )
        if _SEVERITY_ORDER.index(self.operational_severity) < worst_component:
            raise ValueError(
                'operational severity must be at least the highest '
                'component severity'
            )
        # 'confirmed' certainty is unreachable while any component lacks
        # confirming evidence (#595 §14 severity ≠ certainty).
        if self.evidence_certainty == 'confirmed' and any(
            c.classification in _NONCONFIRMING_CLASSIFICATIONS
            or c.classification in _DRIFT_CLASSIFICATIONS
            or c.classification in _FAULT_CLASSIFICATIONS
            for c in self.components
        ):
            raise ValueError(
                "'confirmed' certainty requires every component to be "
                'clean and evidence-backed'
            )
        digest = _hash(self.semantic_payload())
        if self.assessment_sha256 != digest:
            raise ValueError('drift assessment hash mismatch')
        if self.assessment_id != _semantic_id('hldrf', digest):
            raise ValueError('drift assessment id mismatch')
        return self


#: Observation kinds that describe configuration-identity state — a
#: mismatch against the pinned value is ``configuration_drift`` unless a
#: matching change event authorizes it.
_CONFIG_KINDS: frozenset[str] = frozenset(
    {'config_hash_state', 'firmware_version'}
)
#: Kinds that describe measured/performance state — a beyond-tolerance
#: value is ``performance_drift``.
_PERFORMANCE_KINDS: frozenset[str] = frozenset(
    {
        'field_measurement_check',
        'av_self_test_result',
        'temperature_fan_state',
        'power_ups_state',
        'network_link_quality',
        'clock_ptp_state',
        'hdmi_link_state',
        'storage_resource_state',
        'license_feature_state',
        'room_as_built_change',
    }
)
#: Kinds that describe faults/events — recurring occurrences classify
#: ``intermittent_fault``; a hard-fault observation is ``hard_failure``.
_EVENT_KINDS: frozenset[str] = frozenset(
    {
        'device_error_warning',
        'reboot_uptime_event',
        'user_reported_symptom',
    }
)


def _parse_repr_value(repr_text: str | None) -> Any:
    if repr_text is None:
        return None
    import json

    try:
        return json.loads(repr_text)
    except ValueError:
        return None


def _reprs_equal(left: str | None, right: str | None) -> bool | None:
    """Canonical-equality check; ``None`` = not decidable."""

    if left is None or right is None:
        return None
    lval = _parse_repr_value(left)
    rval = _parse_repr_value(right)
    if lval is None or rval is None:
        return left == right
    return lval == rval


def _change_event_matches(
    event: ChangeEventRecord,
    subject_ref: AuthorityRef,
    domain: LifecycleDomain,
    kinds: tuple[str, ...],
    window: tuple[str | None, str | None],
) -> bool:
    if subject_ref not in event.subject_refs and event.subject_refs:
        return False
    lower, upper = window
    if lower is not None and event.occurred_at_utc < lower:
        return False
    if upper is not None and event.occurred_at_utc > upper:
        return False
    if kinds and event.kind not in kinds:
        return False
    return True


def classify_drift_component(
    *,
    domain: LifecycleDomain,
    subject_ref: AuthorityRef,
    declaration: DeviceMonitoringDeclaration | None,
    observations: Sequence[LifecycleObservation],
    change_events: Sequence[ChangeEventRecord] = (),
    baseline_window: tuple[str | None, str | None] = (None, None),
    intermittent_min_events: int = 2,
    expected_change_kinds: tuple[ChangeEventKind, ...] = (),
) -> DriftComponent:
    """Classify one domain component — never invent change or cause.

    The classification ladder is strict:
    ``insufficient_observability`` (no capability and nothing observed) →
    ``not_comparable`` (observations exist but none can name a baseline
    comparison) → ``hard_failure``/``intermittent_fault`` (fault events) →
    ``configuration_drift``/``performance_drift``/``evidence_stale`` →
    ``expected_change`` (matching logged change) →
    ``no_material_change`` (a comparison ran and matched).
    """

    obs = [
        item
        for item in observations
        if item.domain == domain and item.subject_ref == subject_ref
    ]
    obs_ids = tuple(item.observation_id for item in obs)

    if not obs:
        if declaration is not None and not declaration.observable():
            return DriftComponent(
                domain=domain,
                classification='insufficient_observability',
                observation_ids=(),
                reason='subject declared unobservable — no telemetry expected',
            )
        return DriftComponent(
            domain=domain,
            classification='insufficient_observability',
            observation_ids=(),
            reason='no observations captured for this domain component',
        )

    fault_events = [
        item for item in obs if item.kind in _EVENT_KINDS
    ]

    def _hard_failure(item: LifecycleObservation) -> bool:
        parsed = _parse_repr_value(item.observed_repr)
        if item.kind == 'device_error_warning':
            return parsed in {'failed', 'offline_hard', 'fault'} or (
                isinstance(parsed, dict)
                and parsed.get('severity') in {'hard_failure', 'fault'}
            )
        if item.kind == 'device_online_state':
            if parsed is False:
                return True
            return (
                isinstance(parsed, dict)
                and parsed.get('online') is False
            )
        return False

    hard = [item for item in obs if _hard_failure(item)]
    hard_ids = tuple(item.observation_id for item in hard)
    if hard:
        return DriftComponent(
            domain=domain,
            classification='hard_failure',
            observation_ids=hard_ids,
            reason='hard fault reported by observation evidence',
            severity='high',
        )

    comparable: list[LifecycleObservation] = []
    changed: list[LifecycleObservation] = []
    unmatched_changed = False
    matched_event_id: str | None = None
    for item in obs:
        if item.kind in _EVENT_KINDS:
            continue
        if item.comparison_key is None:
            continue
        if item.compared_to_repr is None:
            continue
        comparable.append(item)
        equal = _reprs_equal(item.observed_repr, item.compared_to_repr)
        if equal is False:
            changed.append(item)

    if not comparable:
        if fault_events and len(fault_events) >= intermittent_min_events:
            ids = tuple(i.observation_id for i in fault_events)
            return DriftComponent(
                domain=domain,
                classification='intermittent_fault',
                observation_ids=ids,
                reason=(
                    f'{len(fault_events)} recurring fault events '
                    f'(≥{intermittent_min_events} declared threshold)'
                ),
                severity='medium',
            )
        return DriftComponent(
            domain=domain,
            classification='not_comparable',
            observation_ids=obs_ids,
            reason=(
                'observations exist but none carries a baseline '
                'comparison — "no change" cannot be claimed'
            ),
        )

    if changed:
        matched = None
        for item in changed:
            for event in change_events:
                if _change_event_matches(
                    event,
                    subject_ref,
                    domain,
                    expected_change_kinds,
                    baseline_window,
                ):
                    matched = event
                    break
            if matched is not None:
                break
        changed_ids = tuple(item.observation_id for item in changed)
        if matched is not None:
            return DriftComponent(
                domain=domain,
                classification='expected_change',
                observation_ids=changed_ids,
                reason='change matches a logged authorized change event',
                matched_change_event_id=matched.event_id,
            )
        drift_kind = (
            'configuration_drift'
            if any(item.kind in _CONFIG_KINDS for item in changed)
            else 'performance_drift'
        )
        if not any(
            item.kind in _CONFIG_KINDS | _PERFORMANCE_KINDS for item in changed
        ):
            drift_kind = 'configuration_drift'
        return DriftComponent(
            domain=domain,
            classification=drift_kind,
            observation_ids=changed_ids,
            reason=(
                'observed state differs from the pinned baseline with no '
                'matching change event — cause is not asserted'
            ),
            severity='medium',
        )

    # All comparable observations matched their baseline pin — a real
    # comparison ran, so "no change" is an earned claim.
    if fault_events and len(fault_events) >= intermittent_min_events:
        ids = tuple(i.observation_id for i in fault_events)
        return DriftComponent(
            domain=domain,
            classification='intermittent_fault',
            observation_ids=ids,
            reason=(
                f'{len(fault_events)} recurring fault events '
                f'(≥{intermittent_min_events} declared threshold)'
            ),
            severity='medium',
        )
    return DriftComponent(
        domain=domain,
        classification='no_material_change',
        observation_ids=obs_ids,
        reason='comparable observations match the pinned baseline',
    )


def derive_stale_marks(
    components: Sequence[DriftComponent],
    dependency_rules: Sequence[DependencyImpactRule],
    unmapped_dependents: Sequence[AuthorityRef] = (),
) -> tuple[tuple[StaleEvidenceMark, ...], Literal['mapped', 'partially_mapped', 'unmapped']]:
    """Fire dependency rules for non-clean components.

    Returns the marks plus a coverage status: every non-clean component
    must be covered by at least one rule for 'mapped'; declared dependents
    with no covering rule are marked ``review_required`` — unknown
    dependency never means continued validity (#595 §5).
    """

    marks: dict[tuple[str, str], StaleEvidenceMark] = {}
    covered_domains: set[str] = set()
    dirty = [
        c for c in components if c.classification not in _CLEAN_CLASSIFICATIONS
    ]
    for component in dirty:
        for rule in dependency_rules:
            if rule.trigger_domain != component.domain:
                continue
            if component.classification not in rule.trigger_classifications:
                continue
            key = (rule.affected_ref.kind, rule.affected_ref.ref_id)
            covered_domains.add(component.domain)
            marks[key] = StaleEvidenceMark(
                affected_ref=rule.affected_ref,
                disposition=rule.disposition,
                reason=(
                    f'{component.domain} component classified '
                    f'{component.classification}'
                    + (f' — {rule.note}' if rule.note else '')
                ),
                via_rule_id=rule.rule_id,
            )
    for ref in unmapped_dependents:
        key = (ref.kind, ref.ref_id)
        if key in marks:
            continue
        marks[key] = StaleEvidenceMark(
            affected_ref=ref,
            disposition='review_required',
            reason=(
                'dependency coverage is not declared for the changed '
                'components — continued validity is unproven'
            ),
        )
    if not dirty:
        status = 'mapped'
    elif not covered_domains:
        status = 'unmapped'
    elif len(covered_domains) == len({c.domain for c in dirty}):
        status = 'mapped' if not unmapped_dependents else 'partially_mapped'
    else:
        status = 'partially_mapped'
    return tuple(marks.values()), status


def build_drift_assessment(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    baseline_refs: Sequence[AuthorityRef] = (),
    components: Sequence[DriftComponent],
    dependency_rules: Sequence[DependencyImpactRule] = (),
    unmapped_dependents: Sequence[AuthorityRef] = (),
    observations: Sequence[LifecycleObservation] = (),
    change_events: Sequence[ChangeEventRecord] = (),
    operational_severity: OperationalSeverity | None = None,
    evidence_certainty: EvidenceCertainty | None = None,
    assessed_at_utc: str | None = None,
) -> DriftAssessment:
    """Seal a drift assessment; severity/certainty default to honest
    derivations, and caller assertions are validated by the model."""

    items = tuple(components)
    marks, status = derive_stale_marks(
        items, dependency_rules, unmapped_dependents
    )
    worst = max(
        (_SEVERITY_ORDER.index(c.severity) for c in items),
        default=0,
    )
    any_nonclean = any(
        c.classification not in _CLEAN_CLASSIFICATIONS for c in items
    )
    if operational_severity is None:
        # A non-clean component always means at least 'low' operational
        # attention — even when the component itself carries 'none'
        # (e.g. an observability gap is unproven, not harmless).
        operational_severity = (
            _SEVERITY_ORDER[max(worst, 1)] if any_nonclean else 'none'
        )
    if evidence_certainty is None:
        if not any_nonclean:
            evidence_certainty = 'confirmed'
        elif all(
            c.classification
            in _CLEAN_CLASSIFICATIONS | _DRIFT_CLASSIFICATIONS | _FAULT_CLASSIFICATIONS
            for c in items
        ):
            evidence_certainty = 'suspected'
        else:
            evidence_certainty = 'unknown'
    obs_ids = tuple(
        dict.fromkeys(
            oid
            for c in items
            for oid in c.observation_ids
        )
        or [item.observation_id for item in observations]
    )
    event_ids = tuple(item.event_id for item in change_events)
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=DRIFT_ASSESSMENT_AUTHORITY_VERSION,
        assessment_id='',
        document_id=document_id,
        subject_ref=subject_ref,
        baseline_refs=tuple(baseline_refs),
        components=items,
        stale_marks=marks,
        dependency_status=status,
        observation_ids=obs_ids,
        change_event_ids=event_ids,
        operational_severity=operational_severity,
        evidence_certainty=evidence_certainty,
        assessed_at_utc=assessed_at_utc or _utc_now(),
        assessment_sha256='0' * 64,
    )
    probe = DriftAssessment.model_construct(
        **canonicalize_payload(DriftAssessment, payload)
    )
    digest = _hash(probe.semantic_payload())
    return DriftAssessment(
        **probe.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        ),
        assessment_id=_semantic_id('hldrf', digest),
        assessment_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Re-verification trigger (#595 §10) — smallest defensible campaign.
# ---------------------------------------------------------------------------


class ReverificationTask(BaseModel):
    """One bounded re-verification action with its reasons and the exact
    evidence it restores confidence in."""

    model_config = ConfigDict(frozen=True)

    task_kind: ReverificationTaskKind
    domain: LifecycleDomain | None = None
    target_refs: tuple[AuthorityRef, ...] = ()
    reason: str = Field(min_length=1)


class ReverificationTrigger(BaseModel):
    """The sealed output of a drift assessment: what to do next.

    ``full_recommission_required`` is never emitted by
    :func:`compose_reverification` on its own — it requires an explicit
    caller declaration because a small health check is not equivalent to
    full requalification (#595 §6).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-reverification-trigger-1'
    ] = REVERIFICATION_TRIGGER_AUTHORITY_VERSION
    trigger_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assessment_id: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)
    action: ReverificationAction
    tasks: tuple[ReverificationTask, ...] = ()
    summary: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)
    trigger_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'trigger_sha256', 'trigger_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ReverificationTrigger':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        if self.action == 'no_action' and self.tasks:
            raise ValueError("'no_action' may not carry tasks")
        if self.action != 'no_action' and not self.tasks:
            raise ValueError('a trigger action requires ≥1 task')
        if (
            self.action == 'full_recommission_required'
            and 'full' not in self.summary.lower()
        ):
            raise ValueError(
                'full recommission requires an explicit summary justification'
            )
        digest = _hash(self.semantic_payload())
        if self.trigger_sha256 != digest:
            raise ValueError('reverification trigger hash mismatch')
        if self.trigger_id != _semantic_id('hlrev', digest):
            raise ValueError('reverification trigger id mismatch')
        return self


def compose_reverification(
    assessment: DriftAssessment,
    *,
    known_good_refs: Sequence[AuthorityRef] = (),
    require_full_recommission: bool = False,
    full_recommission_reason: str | None = None,
    decided_at_utc: str | None = None,
) -> ReverificationTrigger:
    """Derive the smallest defensible re-verification campaign.

    Task synthesis mirrors the component classifications:
    ``hard_failure``/``intermittent_fault`` → service investigation;
    ``configuration_drift`` → known-good restore (when a baseline pin
    exists) else a domain reverification; ``performance_drift`` /
    ``evidence_stale`` → reference check / domain reverification;
    ``insufficient_observability`` → a manual observation task;
    ``not_comparable`` → a reference check that establishes comparability.
    Stale marks with ``remeasure``/``recompute``/``stale`` dispositions
    produce domain reverification tasks naming their refs.
    """

    tasks: list[ReverificationTask] = []
    seen: set[tuple[str, str | None]] = set()

    def add(kind: ReverificationTaskKind, domain, refs, reason: str) -> None:
        key = (kind, domain)
        if key in seen:
            return
        seen.add(key)
        tasks.append(
            ReverificationTask(
                task_kind=kind,
                domain=domain,
                target_refs=tuple(refs),
                reason=reason,
            )
        )

    dirty = [
        c for c in assessment.components
        if c.classification not in _CLEAN_CLASSIFICATIONS
    ]
    for component in dirty:
        cls = component.classification
        if cls in _FAULT_CLASSIFICATIONS:
            add(
                'service_investigation',
                component.domain,
                (),
                f'{cls} in {component.domain}: {component.reason}',
            )
        elif cls == 'configuration_drift':
            if known_good_refs:
                add(
                    'restore_config',
                    component.domain,
                    known_good_refs,
                    'restore the pinned known-good configuration, then '
                    'confirm by readback and targeted measurement',
                )
            else:
                add(
                    'domain_reverification',
                    component.domain,
                    (),
                    'configuration differs from baseline and no known-good '
                    'reference exists to restore',
                )
        elif cls in {'performance_drift', 'evidence_stale'}:
            add(
                'reference_check',
                component.domain,
                (),
                f'{cls} in {component.domain}: run the bounded reference '
                'check against the pinned baseline envelope',
            )
        elif cls == 'insufficient_observability':
            add(
                'manual_observation',
                component.domain,
                (),
                'no telemetry path — capture a manual service observation '
                'before any verdict',
            )
        elif cls == 'not_comparable':
            add(
                'reference_check',
                component.domain,
                (),
                'observation could not be compared to the baseline — '
                'recapture under the pinned acquisition context',
            )
    for mark in assessment.stale_marks:
        if mark.disposition in {'stale', 'recompute', 'remeasure'}:
            add(
                'domain_reverification',
                None,
                (mark.affected_ref,),
                f'evidence marked {mark.disposition}: {mark.reason}',
            )
        elif mark.disposition == 'review_required':
            add(
                'domain_reverification',
                None,
                (mark.affected_ref,),
                f'dependency unmapped — review required: {mark.reason}',
            )

    if require_full_recommission:
        if not full_recommission_reason:
            raise ValueError(
                'full recommission requires an explicit declared reason'
            )
        action: ReverificationAction = 'full_recommission_required'
        tasks = [
            ReverificationTask(
                task_kind='full_recommission',
                domain=None,
                target_refs=tuple(known_good_refs),
                reason=full_recommission_reason,
            )
        ]
        summary = f'full recommission required: {full_recommission_reason}'
    elif not tasks:
        action = 'no_action'
        summary = 'all components clean — no re-verification required'
    elif any(t.task_kind == 'service_investigation' for t in tasks):
        action = 'service_review'
        summary = (
            'fault evidence requires service review plus targeted checks'
        )
    elif any(t.task_kind == 'restore_config' for t in tasks):
        action = 'restore_known_good_config'
        summary = (
            'restore known-good configuration then confirm by readback '
            'and targeted measurement'
        )
    elif any(t.task_kind == 'manual_observation' for t in tasks):
        action = 'observe'
        summary = 'insufficient observability — capture observations first'
    elif any(t.task_kind == 'domain_reverification' for t in tasks):
        action = 'reverify_domain'
        summary = 'targeted domain re-verification of affected evidence'
    else:
        action = 'run_reference_check'
        summary = 'bounded reference checks against the baseline envelope'

    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=REVERIFICATION_TRIGGER_AUTHORITY_VERSION,
        trigger_id='',
        document_id=assessment.document_id,
        assessment_id=assessment.assessment_id,
        assessment_sha256=assessment.assessment_sha256,
        action=action,
        tasks=tuple(tasks),
        summary=summary,
        decided_at_utc=decided_at_utc or _utc_now(),
        trigger_sha256='0' * 64,
    )
    probe = ReverificationTrigger.model_construct(
        **canonicalize_payload(ReverificationTrigger, payload)
    )
    digest = _hash(probe.semantic_payload())
    return ReverificationTrigger(
        **probe.model_dump(
            mode='python', exclude={'trigger_sha256', 'trigger_id'}
        ),
        trigger_id=_semantic_id('hlrev', digest),
        trigger_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Restore-and-confirm loop (#595 §11)
# ---------------------------------------------------------------------------


class RestoreCheckEvidence(BaseModel):
    """One post-restore confirmation check — a readback diff, a targeted
    measurement, a reference-check run."""

    model_config = ConfigDict(frozen=True)

    check_ref: AuthorityRef
    state: Literal['passed', 'failed', 'inconclusive']
    note: str | None = None


class RestoreConfirmation(BaseModel):
    """Whether a known-good restore actually restored confidence.

    A configuration-hash match alone does not restore physical
    performance when hardware or room state changed — the verdict derives
    from the confirmation checks, and ``escalate_service`` is the honest
    exit when restoration cannot be confirmed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LIFECYCLE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-restore-confirmation-1'
    ] = RESTORE_CONFIRMATION_AUTHORITY_VERSION
    confirmation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    restore_ref: AuthorityRef
    checks: tuple[RestoreCheckEvidence, ...] = ()
    verdict: RestoreConfirmationVerdict
    note: str | None = None
    confirmed_at_utc: str = Field(min_length=1)
    confirmation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'confirmation_sha256', 'confirmation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RestoreConfirmation':
        _require_iso8601(self.confirmed_at_utc, 'confirmed_at_utc')
        states = {item.state for item in self.checks}
        if self.verdict == 'confidence_restored':
            if not self.checks or 'passed' not in states:
                raise ValueError(
                    "'confidence_restored' requires at least one passed check"
                )
            if states - {'passed'}:
                raise ValueError(
                    "'confidence_restored' cannot coexist with failed or "
                    'inconclusive checks'
                )
        if self.verdict == 'differences_remain' and 'failed' not in states:
            raise ValueError(
                "'differences_remain' requires at least one failed check"
            )
        if self.verdict == 'confirmation_inconclusive' and (
            self.checks and 'inconclusive' not in states
        ):
            raise ValueError(
                "'confirmation_inconclusive' requires ≥1 inconclusive "
                'check or no completed checks'
            )
        digest = _hash(self.semantic_payload())
        if self.confirmation_sha256 != digest:
            raise ValueError('restore confirmation hash mismatch')
        if self.confirmation_id != _semantic_id('hlrst', digest):
            raise ValueError('restore confirmation id mismatch')
        return self


def evaluate_restore_confirmation(
    *,
    document_id: str,
    restore_ref: AuthorityRef,
    checks: Sequence[RestoreCheckEvidence],
    note: str | None = None,
    escalate: bool = False,
    confirmed_at_utc: str | None = None,
) -> RestoreConfirmation:
    """Derive the honest verdict from the recorded checks."""

    states = {item.state for item in checks}
    if escalate:
        verdict: RestoreConfirmationVerdict = 'escalate_service'
    elif 'failed' in states:
        verdict = 'differences_remain'
    elif 'inconclusive' in states or not checks:
        verdict = 'confirmation_inconclusive'
    else:
        verdict = 'confidence_restored'
    payload = dict(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        authority_version=RESTORE_CONFIRMATION_AUTHORITY_VERSION,
        confirmation_id='',
        document_id=document_id,
        restore_ref=restore_ref,
        checks=tuple(checks),
        verdict=verdict,
        note=note,
        confirmed_at_utc=confirmed_at_utc or _utc_now(),
        confirmation_sha256='0' * 64,
    )
    probe = RestoreConfirmation.model_construct(
        **canonicalize_payload(RestoreConfirmation, payload)
    )
    digest = _hash(probe.semantic_payload())
    return RestoreConfirmation(
        **probe.model_dump(
            mode='python', exclude={'confirmation_sha256', 'confirmation_id'}
        ),
        confirmation_id=_semantic_id('hlrst', digest),
        confirmation_sha256=digest,
    )


__all__ = [
    'CHANGE_EVENT_AUTHORITY_VERSION',
    'ChangeEventKind',
    'ChangeEventRecord',
    'CollectionMode',
    'DependencyImpactRule',
    'DeviceMonitoringDeclaration',
    'DRIFT_ASSESSMENT_AUTHORITY_VERSION',
    'DriftAssessment',
    'DriftClassification',
    'DriftComponent',
    'EvidenceCertainty',
    'ImpactDisposition',
    'LIFECYCLE_OBSERVATION_AUTHORITY_VERSION',
    'LIFECYCLE_SCHEMA_VERSION',
    'LifecycleDomain',
    'LifecycleObservation',
    'MONITORING_DECLARATION_AUTHORITY_VERSION',
    'MonitoringAuthorization',
    'MonitoringCapabilityClass',
    'ObservationKind',
    'OperationalSeverity',
    'RESTORE_CONFIRMATION_AUTHORITY_VERSION',
    'REVERIFICATION_TRIGGER_AUTHORITY_VERSION',
    'RestoreCheckEvidence',
    'RestoreConfirmation',
    'RestoreConfirmationVerdict',
    'ReverificationAction',
    'ReverificationTask',
    'ReverificationTaskKind',
    'ReverificationTrigger',
    'SYMPTOM_EPISODE_AUTHORITY_VERSION',
    'StaleEvidenceMark',
    'SymptomEpisode',
    'SymptomReasonState',
    'TREND_ASSESSMENT_AUTHORITY_VERSION',
    'TrendAssessment',
    'TrendSample',
    'TrendThresholdSpec',
    'assess_trend',
    'build_change_event',
    'build_drift_assessment',
    'build_monitoring_declaration',
    'build_observation',
    'build_symptom_episode',
    'classify_drift_component',
    'compose_reverification',
    'derive_stale_marks',
    'evaluate_restore_confirmation',
]
