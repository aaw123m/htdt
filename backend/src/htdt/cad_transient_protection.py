"""Surge / lightning transient-protection evidence authority (#789).

Adequate steady-state voltage/current capacity, stable mains quality and
UPS ride-through do not prove protection against lightning-related or
switching transient overvoltage, and a consumer ``surge protector``
label does not prove correct SPD type, ratings, coordination,
installation or project applicability. This module owns exactly that
evidence question: which exact electrical or conductive signal path
needs protection, which declared plan covers it, what product/listing/
install evidence is bound to it, what its current health readback says,
and what protection verdict a path may carry.

Layers stay separate and fail closed:

- the protected path binds the real topology stages
  (utility/service -> panel/distribution -> branch circuit ->
  receptacle/PDU/UPS -> AV device, or external conductive path ->
  surge-protection interface -> device); a protected rack outlet never
  marks the whole theater protected;
- the plan declares which protection domains apply and the pinned
  jurisdiction/code context — NFPA 70 is not global, IEC standards do
  not automatically establish local compliance;
- SPD evidence keeps method-specific ratings (type/class, modes of
  protection, VPR, MCOV, nominal/maximum discharge current, SCCR) as
  exact optional fields — unlike IEC/UL quantities are never normalized
  into one hidden ``surge rating``;
- health observations carry manufacturer-defined states
  (ok/replace/fault/remote-alarm) with their readback provenance; an
  SPD life estimate is never derived from elapsed time alone;
- transient/lightning/service events stale the affected evidence until
  a targeted inspection/requalification record exists;
- the assessment verdict is composed per path and remains fail-closed:
  absent evidence is ``no_protection_evidence``, never inferred.

Boundaries this module does not own: steady-state power quality /
sags/swells / harmonics / ride-through (#738), power topology and UPS
capability (#587/#736), grounding/EMC diagnosis (#606/#752), product
safety certification (#751), revision lifecycle (#599), impact/staleness
propagation mechanics (#729), and any lightning-risk design, conductor
routing, air-terminal placement, earth-electrode or SPD-coordination
calculation — structure-level lightning protection always stays an
external professional authority and unresolved requirements surface as
``review_required``.

Basis: issue #789 scope; IEC 61643-11:2025 (SPDs on AC low-voltage
systems; IEC 61643-11:2011 withdrawn 2025-06-18); IEC 62305-1..4:2024
and the IEC 62305:2026 series package (external lightning-protection
scope); NFPA 70 (NEC) 2026 (jurisdiction-pinned only); UL 1449
certification record fields (Type, VPR, MCOV, In, SCCR).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

# Identity/quantity fields the method-specific SPD ratings use. They stay
# separate because IEC 61643-11 and UL 1449 do not define the same
# quantities on the same basis — normalizing them would fabricate an
# equivalence the standards do not make.


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_standard_pin(reference: str) -> None:
    if '@' not in reference:
        raise ValueError(
            'standard/listing references must pin exact editions '
            '(standard_id@edition)')


class TransientProtectionIntegrityError(ValueError):
    """A sealed transient-protection record failed integrity checks."""


ProtectionDomain = Literal[
    'service_entrance_spd',
    'distribution_panel_spd',
    'branch_point_of_use_spd',
    'signal_data_line_protection',
    'external_lightning_protection_system',
    'ups_power_conditioner_transient_feature',
    'device_internal_protection',
    'unknown',
]

PathKind = Literal[
    'electrical_supply',
    'signal_data',
    'antenna_feed',
    'control_bus',
    'other_external_copper',
]

PathStage = Literal[
    'utility_service',
    'panel_distribution',
    'branch_circuit',
    'receptacle_pdu_ups',
    'av_device',
    'external_conductive_path',
    'surge_protection_interface',
    'network_or_signal_device',
]

StandardProfile = Literal[
    'iec_61643_11_2025',
    'iec_61643_11_2011_withdrawn',
    'ul_1449',
    'local_national_spd_standard',
    'iec_62305_lightning_profile',
    'project_defined',
    'unknown',
]

SPDTypeClass = Literal[
    'type_1',
    'type_2',
    'type_3',
    'type_1_component',
    'type_2_component',
    'type_3_component',
    'type_4_component',
    'type_5_component',
    'class_i',
    'class_ii',
    'class_iii',
    'signal_line_spd',
    'non_spd_transient_feature',
    'unknown',
]

InstallState = Literal[
    'designed_only',
    'installed_verified',
    'installed_unverified',
    'unknown',
]

EvidenceBasis = Literal[
    'listing_document',
    'installed_record',
    'field_observation',
    'manufacturer_document',
    'vendor_marketing',
]

HealthStatus = Literal[
    'status_ok',
    'replace_required',
    'fault',
    'remote_alarm',
    'unknown',
]

StatusSource = Literal[
    'local_indicator',
    'remote_contact',
    'telemetry',
    'manual_inspection',
    'unknown',
]

EventKind = Literal[
    'lightning_strike',
    'severe_transient',
    'utility_fault',
    'spd_alarm',
    'service_panel_work',
    'external_path_added',
    'topology_change',
    'other',
]

CoordinationState = Literal[
    'coordinated_with_reference',
    'single_device',
    'multiple_uncoordinated_unknown',
    'unknown',
]

PathProtectionVerdict = Literal[
    'protected_with_evidence',
    'partial_point_of_use_only',
    'designed_not_installed',
    'status_degraded',
    'stale_after_event',
    'coordination_unverified',
    'review_required',
    'no_protection_evidence',
]

TRANSIENT_PROTECTION_LABELS: dict[str, str] = {
    'protected_with_evidence': '経路保護（エビデンスあり）',
    'partial_point_of_use_only': '使用点のみ保護（上流未確認）',
    'designed_not_installed': '設計意図のみ（設置未検証）',
    'status_degraded': 'SPD劣化/交換要（電源全体の故障ではない）',
    'stale_after_event': 'イベント後の点検待ち（保護陳腐化）',
    'coordination_unverified': 'SPD協調未検証',
    'review_required': '有資格電気/雷保護レビュー要',
    'no_protection_evidence': '保護エビデンスなし',
    'service_entrance_spd': 'サービス入口SPD',
    'distribution_panel_spd': '分電盤SPD',
    'branch_point_of_use_spd': '分岐/使用点SPD',
    'signal_data_line_protection': '信号/データ線保護',
    'external_lightning_protection_system': '外部雷保護システム',
    'ups_power_conditioner_transient_feature': 'UPS/電源コンディショナ過渡機能',
    'device_internal_protection': '機器内部保護',
    'status_ok': 'SPD状態: 正常',
    'replace_required': 'SPD状態: 交換要',
    'fault': 'SPD状態: 故障',
    'remote_alarm': 'SPD状態: リモート警報',
    'unknown': '不明',
}


class ProtectedPath(BaseModel):
    """One exact electrical or conductive signal path that may carry
    transient energy into AV equipment (ppath- prefix).

    ``stage_sequence`` is ordered upstream-to-load using the fixed
    vocabulary so a path is a topology claim, not a zone name. The path
    binds the protected load (``load_ref`` — equipment/asset authority)
    so protection evidence is evaluated per actual device path, never
    per theater. A path is the thing evidence is bound to: declaring it
    is not protection, only the identity protection records hang off.
    """

    model_config = ConfigDict(frozen=True)

    path_id: str
    path_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_kind: PathKind
    stage_sequence: tuple[PathStage, ...]
    load_ref: AuthorityRef
    load_label: str
    requires_protection: bool = True
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ProtectedPath':
        if not self.stage_sequence:
            raise ValueError('the ordered path stages are required')
        if not self.load_label:
            raise ValueError('load_label is required')
        _require_refs(self.load_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'path_id', 'path_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ProtectedPath':
        return _seal(cls, payload, 'path_id', 'path_sha256', 'ppath')


class TransientProtectionPlan(BaseModel):
    """Declared protection strategy for a set of paths (tpplan- prefix).

    The plan is design intent, not evidence: it records which protection
    domains apply to which paths, the pinned jurisdiction/code context
    and whether qualified electrical/lightning-protection review is
    required. ``declared_domains`` is the completeness contract the
    assessment checks evidence against — a path whose plan declares
    ``service_entrance_spd`` but carries only point-of-use evidence is
    partial, never 'protected'.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_refs: tuple[AuthorityRef, ...]
    declared_domains: tuple[ProtectionDomain, ...]
    jurisdiction_country: str | None = None
    code_family: str | None = None
    code_edition: str | None = None
    ahj_record_ref: AuthorityRef | None = None
    professional_review_ref: AuthorityRef | None = None
    requires_qualified_review: bool = False
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'TransientProtectionPlan':
        if not self.path_refs:
            raise ValueError('at least one protected path is required')
        if not self.declared_domains:
            raise ValueError('declared protection domains are required')
        _require_refs(*self.path_refs)
        for ref in (self.ahj_record_ref, self.professional_review_ref):
            if ref is not None:
                _require_refs(ref)
        if 'external_lightning_protection_system' in self.declared_domains \
                and self.professional_review_ref is None:
            # Not a rejection — a plan may legitimately require an LPS
            # it does not yet have review for — but force the flag so
            # the plan can never read as self-contained.
            object.__setattr__(self, 'requires_qualified_review', True)
        if self.code_family is not None and self.code_edition is None:
            raise ValueError(
                'code_edition is required when code_family is set — '
                'a code family without an edition is not a pinned '
                'requirement')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'TransientProtectionPlan':
        return _seal(cls, payload, 'plan_id', 'plan_sha256', 'tpplan')


class SPDEvidence(BaseModel):
    """One protection device's product/listing/install evidence bound to
    a protected path (spd- prefix).

    Method-specific ratings stay exact optional fields: ``vpr_volts``,
    ``mcov_volts``, ``nominal_discharge_current_ka``,
    ``max_discharge_current_ka``, ``sccr_ka`` are recorded under the
    standard/profile they were measured or certified by — HTDT never
    normalizes unlike quantities into one surge rating and never derives
    coordination from two datasheets. ``evidence_basis='vendor_marketing'``
    records the claim but can never satisfy a protection domain. A
    non-SPD feature (UPS transient feature, device-internal protection)
    is represented via ``spd_type_class='non_spd_transient_feature'`` and
    counts only for its own declared domain.
    """

    model_config = ConfigDict(frozen=True)

    spd_id: str
    spd_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_ref: AuthorityRef
    domain: ProtectionDomain
    spd_type_class: SPDTypeClass
    standard_profile: StandardProfile
    standard_reference: str
    manufacturer: str | None = None
    model: str | None = None
    revision: str | None = None
    nominal_voltage_v: float | None = None
    nominal_frequency_hz: float | None = None
    modes_of_protection: tuple[str, ...] = ()
    vpr_volts: float | None = None
    mcov_volts: float | None = None
    nominal_discharge_current_ka: float | None = None
    max_discharge_current_ka: float | None = None
    sccr_ka: float | None = None
    listing_report_id: str | None = None
    certification_ref: AuthorityRef | None = None
    coordination_ref: AuthorityRef | None = None
    install_state: InstallState = 'unknown'
    panel_circuit_association: str | None = None
    install_location: str | None = None
    installer_report_ref: AuthorityRef | None = None
    photo_ref: AuthorityRef | None = None
    commissioning_date: str | None = None
    status_indicator_kind: Literal[
        'none', 'local_led', 'remote_contact', 'telemetry', 'unknown',
    ] = 'unknown'
    evidence_basis: EvidenceBasis
    upstream_spd_ref: AuthorityRef | None = None
    downstream_spd_ref: AuthorityRef | None = None
    known_limitations: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SPDEvidence':
        _require_refs(self.path_ref)
        for ref in (
            self.certification_ref,
            self.coordination_ref,
            self.installer_report_ref,
            self.photo_ref,
            self.upstream_spd_ref,
            self.downstream_spd_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        if self.standard_profile not in (
                'project_defined', 'unknown') \
                and not self.standard_reference:
            raise ValueError(
                'standard_reference is required for a declared '
                'standard profile')
        if self.standard_reference:
            _require_standard_pin(self.standard_reference)
        for name in ('vpr_volts', 'mcov_volts'):
            value = getattr(self, name)
            if value is not None and value <= 0.0:
                raise ValueError(f'{name} must be > 0')
        for name in (
                'nominal_discharge_current_ka',
                'max_discharge_current_ka',
                'sccr_ka'):
            value = getattr(self, name)
            if value is not None and value <= 0.0:
                raise ValueError(f'{name} must be > 0')
        if self.nominal_voltage_v is not None \
                and self.nominal_voltage_v <= 0.0:
            raise ValueError('nominal_voltage_v must be > 0')
        if self.nominal_frequency_hz is not None \
                and self.nominal_frequency_hz <= 0.0:
            raise ValueError('nominal_frequency_hz must be > 0')
        if self.install_state == 'installed_verified' \
                and self.installer_report_ref is None \
                and self.photo_ref is None \
                and self.panel_circuit_association is None:
            raise ValueError(
                "install_state='installed_verified' requires an "
                'installer report, photo/label record or exact '
                'panel/circuit association — design intent is not '
                'installed evidence')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'spd_id', 'spd_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SPDEvidence':
        return _seal(cls, payload, 'spd_id', 'spd_sha256', 'spd')


class TransientProtectionObservation(BaseModel):
    """One health/status readback for a recorded SPD (tpo- prefix).

    The status vocabulary mirrors manufacturer-supported states; the
    ``status_source`` pins where the readback came from so a
    ``remote_alarm`` never becomes a silent local ``status_ok``. Elapsed
    time alone never degrades a status — only an observation does.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    spd_ref: AuthorityRef
    observed_at_utc: str
    status: HealthStatus
    status_source: StatusSource
    manufacturer_semantics_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'TransientProtectionObservation':
        _require_refs(self.spd_ref)
        if self.manufacturer_semantics_ref is not None:
            _require_refs(self.manufacturer_semantics_ref)
        if not self.observed_at_utc:
            raise ValueError('observed_at_utc is required')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(
            cls, **payload: Any) -> 'TransientProtectionObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'tpo')


class TransientProtectionEvent(BaseModel):
    """One lightning/severe-transient/service event that stales
    protection evidence until inspected (tpe- prefix).

    The event does not declare damage — it suspends confidence. The
    post-event workflow (inspection -> equipment health check ->
    targeted verification -> requalification) is closed by
    ``inspection_record_ref``; until then every assessment touching an
    ``affected_path_refs`` entry stays ``stale_after_event``.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    event_kind: EventKind
    observed_at_utc: str
    affected_path_refs: tuple[AuthorityRef, ...]
    inspection_record_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'TransientProtectionEvent':
        if not self.affected_path_refs:
            raise ValueError('affected_path_refs is required')
        _require_refs(*self.affected_path_refs)
        if self.inspection_record_ref is not None:
            _require_refs(self.inspection_record_ref)
        if not self.observed_at_utc:
            raise ValueError('observed_at_utc is required')
        return self

    @property
    def inspection_completed(self) -> bool:
        return self.inspection_record_ref is not None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_id', 'event_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'TransientProtectionEvent':
        return _seal(cls, payload, 'event_id', 'event_sha256', 'tpe')


class TransientProtectionAssessment(BaseModel):
    """The sealed per-path protection verdict (tpa- prefix).

    Composed by :func:`evaluate_path_protection`; ``verdict`` plus the
    exact evidence refs it rested on are sealed together so a stored
    verdict can never be silently re-based onto different evidence.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_ref: AuthorityRef
    plan_ref: AuthorityRef | None
    spd_evidence_refs: tuple[AuthorityRef, ...]
    observation_refs: tuple[AuthorityRef, ...] = ()
    open_event_refs: tuple[AuthorityRef, ...] = ()
    verdict: PathProtectionVerdict
    coordination_state: CoordinationState
    domains_covered: tuple[ProtectionDomain, ...] = ()
    domains_uncovered: tuple[ProtectionDomain, ...] = ()
    professional_review_required: bool = False
    rationale: str

    @model_validator(mode='after')
    def _validate(self) -> 'TransientProtectionAssessment':
        _require_refs(self.path_ref)
        if self.plan_ref is not None:
            _require_refs(self.plan_ref)
        _require_refs(
            *self.spd_evidence_refs,
            *self.observation_refs,
            *self.open_event_refs,
        )
        if not self.rationale:
            raise ValueError('rationale is required')
        if self.verdict == 'protected_with_evidence' and (
                self.domains_uncovered or not self.spd_evidence_refs):
            raise ValueError(
                "'protected_with_evidence' requires satisfying SPD "
                'evidence and no uncovered declared domains')
        if self.verdict == 'no_protection_evidence' \
                and self.domains_covered:
            raise ValueError(
                "'no_protection_evidence' cannot carry covered "
                'domains')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'assessment_id', 'assessment_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'TransientProtectionAssessment':
        return _seal(
            cls, payload,
            'assessment_id', 'assessment_sha256', 'tpa')


_DEGRADED_STATUSES = ('replace_required', 'fault', 'remote_alarm')

# Evidence that can satisfy a protection domain at all — vendor marketing
# and unclassified devices never do.
_SATISFYING_BASIS = (
    'listing_document',
    'installed_record',
    'field_observation',
    'manufacturer_document',
)


def _satisfies(evidence: SPDEvidence) -> bool:
    """True when an SPD record may count as protection evidence.

    A non-SPD transient feature or unknown device class never satisfies a
    power-path domain, and vendor marketing never satisfies anything —
    UPS ride-through or 'clean power' copy is not transient-protection
    evidence.
    """
    if evidence.evidence_basis == 'vendor_marketing':
        return False
    if evidence.evidence_basis not in _SATISFYING_BASIS:
        return False
    if evidence.spd_type_class in (
            'non_spd_transient_feature', 'unknown') \
            and evidence.domain not in (
                'ups_power_conditioner_transient_feature',
                'device_internal_protection'):
        return False
    return evidence.domain != 'unknown'


def evaluate_path_protection(
    path: ProtectedPath,
    plan: TransientProtectionPlan | None,
    evidence: tuple[SPDEvidence, ...],
    observations: tuple[TransientProtectionObservation, ...],
    events: tuple[TransientProtectionEvent, ...],
) -> TransientProtectionAssessment:
    """Compose the fail-closed protection verdict for one path.

    Ordering: an un-inspected event stales everything; a degraded SPD
    health readback degrades its path; a qualified-review requirement
    never disappears because product records exist; multiple SPDs
    without a coordination reference never count as coordinated;
    uncovered declared domains cap the verdict at partial.
    """
    path_evidence = tuple(
        e for e in evidence
        if e.path_ref.ref_id == path.path_id and _satisfies(e)
    )
    open_events = tuple(
        ev for ev in events
        if not ev.inspection_completed
        and any(r.ref_id == path.path_id for r in ev.affected_path_refs)
    )
    latest_status: dict[str, TransientProtectionObservation] = {}
    for obs in observations:
        key = obs.spd_ref.ref_id
        current = latest_status.get(key)
        if current is None or current.observed_at_utc <= obs.observed_at_utc:
            latest_status[key] = obs
    degraded = any(
        obs.status in _DEGRADED_STATUSES
        for obs in latest_status.values()
    )

    declared: tuple[ProtectionDomain, ...] = ()
    plan_ref = None
    review_required = False
    if plan is not None \
            and any(r.ref_id == path.path_id for r in plan.path_refs):
        declared = plan.declared_domains
        plan_ref = AuthorityRef(
            kind='transient_protection_plan',
            ref_id=plan.plan_id,
            ref_sha256=plan.plan_sha256,
        )
        review_required = plan.requires_qualified_review
        if 'external_lightning_protection_system' in declared:
            review_required = plan.professional_review_ref is None

    # Coverage counts against the DECLARED domain set only — evidence
    # for a domain nobody required does not make the path protected.
    evidence_domains = {e.domain for e in path_evidence}
    covered = tuple(d for d in declared if d in evidence_domains)
    uncovered = tuple(d for d in declared if d not in evidence_domains)

    satisfying = [e for e in path_evidence]
    has_verified_install = any(
        e.install_state == 'installed_verified' for e in satisfying)
    all_designed = bool(satisfying) and all(
        e.install_state == 'designed_only' for e in satisfying)
    multi_uncoordinated = len(satisfying) >= 2 and all(
        e.coordination_ref is None for e in satisfying)

    if open_events:
        verdict: PathProtectionVerdict = 'stale_after_event'
        rationale = (
            'post-event inspection pending for an event affecting '
            'this path')
    elif degraded:
        verdict = 'status_degraded'
        rationale = (
            'latest SPD health readback reports replace/fault/alarm — '
            'protection degraded, not total power failure')
    elif not satisfying:
        verdict = 'no_protection_evidence'
        rationale = 'no satisfying SPD evidence bound to this path'
    elif review_required:
        verdict = 'review_required'
        rationale = (
            'plan requires qualified electrical/lightning-protection '
            'review that is not yet recorded')
    elif all_designed:
        verdict = 'designed_not_installed'
        rationale = 'protection is design intent without installed evidence'
    elif multi_uncoordinated:
        verdict = 'coordination_unverified'
        rationale = (
            'multiple SPDs present without a coordination reference — '
            'two devices never imply coordination')
    elif uncovered:
        verdict = 'partial_point_of_use_only'
        rationale = (
            'declared protection domains remain uncovered by installed '
            'evidence')
    elif declared and not uncovered and has_verified_install:
        verdict = 'protected_with_evidence'
        rationale = 'declared domains covered by installed-verified evidence'
    elif declared and not uncovered:
        verdict = 'partial_point_of_use_only'
        rationale = (
            'declared domains covered but no installed-verified '
            'evidence')
    elif not declared:
        # No plan declared: evidence exists but coverage is undefined —
        # never claim protection without knowing what should be covered.
        verdict = 'no_protection_evidence'
        rationale = (
            'no declared plan for this path — coverage undefined, '
            'evidence cannot complete an unspecified requirement')
    else:
        verdict = 'partial_point_of_use_only'
        rationale = 'evidence present but not verified-installed'

    if len(satisfying) >= 2:
        coordination_state: CoordinationState = (
            'coordinated_with_reference'
            if all(e.coordination_ref is not None for e in satisfying)
            else 'multiple_uncoordinated_unknown')
    elif len(satisfying) == 1:
        coordination_state = 'single_device'
    else:
        coordination_state = 'unknown'

    def _ref(kind: str, rid: str, sha: str) -> AuthorityRef:
        return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)

    return TransientProtectionAssessment.create(
        document_id=path.document_id,
        path_ref=_ref('protected_path', path.path_id, path.path_sha256),
        plan_ref=plan_ref,
        spd_evidence_refs=tuple(
            _ref('spd_evidence', e.spd_id, e.spd_sha256)
            for e in satisfying),
        observation_refs=tuple(
            _ref('transient_protection_observation',
                 o.observation_id, o.observation_sha256)
            for o in latest_status.values()),
        open_event_refs=tuple(
            _ref('transient_protection_event',
                 ev.event_id, ev.event_sha256)
            for ev in open_events),
        verdict=verdict,
        coordination_state=coordination_state,
        domains_covered=covered,
        domains_uncovered=uncovered,
        professional_review_required=review_required,
        rationale=rationale,
    )
