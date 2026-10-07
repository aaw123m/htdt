"""Lifecycle / supportability authority (#792).

A system can be perfectly commissioned today and still become
unrecoverable later: a manufacturer ends support, firmware disappears,
a cloud service is discontinued, a licence or entitlement expires, a
driver/API is abandoned, or replacement hardware is unavailable. This
module records the *dependency and lifecycle evidence* for those
questions — function-level external dependencies, empirically tested
offline continuity, licence/entitlement state (identity only, never key
material), time-stamped manufacturer support and serviceability
observations, and replacement-readiness criteria.

Everything here is evidence, never inference or prediction: evaluators
fail closed to ``unknown``/degraded verdicts when evidence is missing,
stale, or contradictory. There is deliberately no aggregate
"future-proof" or vendor-viability score. Composition is by reference
only: known-good backups (#592), live health monitoring (#595),
substitution requalification (#596), security risk decisions (#598),
supersession of changing evidence (#765).

Basis: issue #792 scope; AVIXA lifecycle-planning guidance (June 2026);
CEDIA controller/service-documentation guidance (2026).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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


def _validate_instant(value: str, field: str) -> None:
    """Enforce strict, explicitly-UTC ISO 8601 timestamps.

    Supportability evidence is time-sensitive (#792 section 15): naive or
    non-UTC strings are rejected so a local-time quirk can never silently
    extend or shorten an evidence horizon.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} is not a valid ISO 8601 instant') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f'{field} must carry an explicit UTC offset')


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


ExternalDependencyKind = Literal[
    'local_only', 'cloud_control', 'cloud_authentication',
    'cloud_configuration', 'cloud_backup', 'cloud_media_service',
    'vendor_activation', 'subscription_licence_entitlement',
    'driver_plugin_api', 'mobile_desktop_app', 'os_platform_service',
    'third_party_identity_provider', 'third_party_automation_service',
    'external_update_server', 'other', 'unknown',
]

EndpointClass = Literal[
    'local', 'lan', 'vendor_cloud', 'third_party_service', 'unknown',
]

FunctionCriticality = Literal[
    'essential', 'important', 'optional', 'service_only', 'unknown',
]

OfflineCondition = Literal[
    'internet_unavailable', 'vendor_cloud_unreachable',
    'dns_unavailable', 'account_signed_out', 'remote_app_unavailable',
    'local_lan_available', 'other', 'unknown',
]

ContinuityOutcome = Literal[
    'continue_local', 'degraded', 'unavailable', 'unknown',
]

ManufacturerSupportState = Literal[
    'currently_supported', 'limited_security_only', 'end_of_sale',
    'end_of_software_updates', 'end_of_security_support',
    'end_of_service', 'discontinued_cloud_dependency', 'unknown',
]

LicenceModel = Literal[
    'perpetual', 'subscription', 'trial', 'bundled', 'unknown',
]

LicenceState = Literal[
    'active', 'grace_period', 'renewal_pending', 'expired', 'revoked',
    'unknown',
]

LicenceBinding = Literal['account', 'device', 'none', 'unknown']

LicenceTransferability = Literal[
    'transferable', 'not_transferable', 'requires_vendor_action',
    'unknown',
]

ActivationDependency = Literal[
    'none', 'vendor_activation_server', 'account_sign_in',
    'network_service', 'unknown',
]

Availability = Literal['yes', 'no', 'unknown']

PortabilityState = Literal[
    'confirmed', 'requires_work', 'not_possible', 'unknown',
]

ControlMigrationState = Literal[
    'compatible', 'migration_required', 'incompatible', 'unknown',
]

WarrantyState = Literal[
    'in_warranty', 'expired', 'extended', 'unknown',
]

LifecycleObservationKind = Literal[
    'manufacturer_support_status', 'licence_entitlement_state',
    'firmware_availability', 'software_availability', 'driver_api_status',
    'warranty_service_state', 'replacement_lead_time',
    'update_channel_status', 'security_maintenance_state', 'other',
]

SUPPORTABILITY_LABELS: dict[str, str] = {
    'continues_local': 'ローカルで継続',
    'continues_degraded': '縮退して継続',
    'unavailable': '利用不可',
    'offline_recovery_verified': 'オフライン復旧が検証済み',
    'recovery_requires_external_service': '復旧に外部サービスが必要',
    'recovery_requires_active_entitlement': '復旧に有効な権利が必要',
    'recovery_untested': '復旧は未検証',
    'recovery_impossible_with_current_evidence':
        '現在の証拠では復旧不可',
    'replacement_ready': '代替準備完了',
    'replacement_requires_work': '代替に追加作業が必要',
    'replacement_blocked': '代替不可',
    'replacement_unverified': '代替可否は未検証',
    'security_maintenance_available': 'セキュリティ保守あり',
    'security_maintenance_limited': 'セキュリティ保守は限定',
    'security_maintenance_ended': 'セキュリティ保守終了',
    'current': '証拠は再確認期限内',
    'review_due': '再確認期限を経過',
    'superseded': '新しい証拠に置換済み',
    'active': '有効',
    'renewal_due': '更新期限到来',
    'expired': '期限切れ',
    'insufficient_evidence': '証拠不足',
    'unknown': '不明',
}


class DeclaredFunction(BaseModel):
    """One system function the project declares (#792 sections 2/12).

    Criticality is project-defined — ``essential``/``important``/
    ``optional``/``service_only`` — with no hidden universal weighting.
    ``requires_offline_continuity`` records the project's decision that
    this function must keep working without external services; a
    cloud-rich product is not thereby inferior.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    function_label: str = Field(min_length=1)
    criticality: FunctionCriticality = 'unknown'
    device_ref: AuthorityRef | None = None
    requires_offline_continuity: bool = False
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DeclaredFunction':
        if self.device_ref is not None:
            _require_refs(self.device_ref)
        return self


class LicenceEntitlement(BaseModel):
    """Licence/entitlement identity and state (#792 section 4).

    Carries feature identity, commercial model, binding, lifecycle dates,
    offline grace and transferability. It deliberately has no field for
    key material, tokens, passwords, or account credentials — those never
    belong in an authority record or report.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    feature_label: str = Field(min_length=1)
    licence_model: LicenceModel = 'unknown'
    licence_state: LicenceState = 'unknown'
    binding: LicenceBinding = 'unknown'
    issued_at_utc: str | None = None
    expires_at_utc: str | None = None
    renewal_due_utc: str | None = None
    offline_grace_days: int | None = None
    activation_dependency: ActivationDependency = 'unknown'
    transferable: LicenceTransferability = 'unknown'
    source_label: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'LicenceEntitlement':
        for field in ('issued_at_utc', 'expires_at_utc', 'renewal_due_utc'):
            value = getattr(self, field)
            if value is not None:
                _validate_instant(value, field)
        if (
            self.issued_at_utc is not None
            and self.expires_at_utc is not None
            and _parse_instant(self.expires_at_utc)
            < _parse_instant(self.issued_at_utc)
        ):
            raise ValueError('expires_at_utc is before issued_at_utc')
        if self.offline_grace_days is not None \
                and self.offline_grace_days < 0:
            raise ValueError('offline_grace_days must be >= 0')
        return self


class SoftwareAvailability(BaseModel):
    """Whether the software/firmware needed to keep or restore the
    known-good state is still obtainable (#792 section 6).

    A binary backup alone is not disaster-recovery evidence: the record
    separately tracks installer availability, lawful retention of
    known-good versions, vendor-service dependence of the restore path,
    factory-reset re-provisioning, and host-OS compatibility of required
    drivers/plugins.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    component_label: str = Field(min_length=1)
    installer_obtainable: Availability = 'unknown'
    known_good_retained: Availability = 'unknown'
    restore_requires_vendor_service: Availability = 'unknown'
    re_provisionable_after_reset: Availability = 'unknown'
    host_os_compatible: Availability = 'unknown'
    notes: str | None = None


class IntegrationDependency(BaseModel):
    """One control/integration component's lifecycle (#792 section 7).

    ``auth_method`` is the method's identity only (e.g. 'oauth2',
    'local_pairing_token') — never a credential. A device can keep
    playing audio/video while becoming unmaintainable because the
    supported control integration disappeared; that state composes with
    #601/#598 and is evaluated elsewhere.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    component_label: str = Field(min_length=1)
    api_sdk_version: str | None = None
    driver_plugin_version: str | None = None
    host_controller_version: str | None = None
    auth_method: str | None = None
    vendor_support_state: ManufacturerSupportState = 'unknown'
    endpoint_class: EndpointClass = 'unknown'
    compatibility_evidence_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'IntegrationDependency':
        if self.compatibility_evidence_ref is not None:
            _require_refs(self.compatibility_evidence_ref)
        return self


class ServiceabilitySnapshot(BaseModel):
    """Time-sensitive warranty/serviceability facts (#792 section 9).

    These are observations at a point in time, not permanent product
    facts — warranty expiry is not equated with expected failure.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    warranty_state: WarrantyState = 'unknown'
    warranty_expires_utc: str | None = None
    authorized_service_available: Availability = 'unknown'
    replaceable_modules: tuple[str, ...] = ()
    spare_part_refs: tuple[AuthorityRef, ...] = ()
    lead_time_days_observed: float | None = None
    rma_policy_source: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ServiceabilitySnapshot':
        if self.warranty_expires_utc is not None:
            _validate_instant(self.warranty_expires_utc,
                              'warranty_expires_utc')
        for ref in self.spare_part_refs:
            _require_refs(ref)
        if self.lead_time_days_observed is not None \
                and self.lead_time_days_observed < 0:
            raise ValueError('lead_time_days_observed must be >= 0')
        return self


class SupportabilityProfile(BaseModel):
    """Declared lifecycle/supportability scope for one document
    (spro- prefix).

    Pins the project's function inventory — each function with its
    project-defined criticality and whether the project requires it to
    survive external-service loss. Nothing here asserts that any
    function actually was tested offline.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    profile_label: str = Field(min_length=1)
    functions: tuple[DeclaredFunction, ...] = ()
    declared_at_utc: str
    source_label: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SupportabilityProfile':
        _validate_instant(self.declared_at_utc, 'declared_at_utc')
        labels = [f.function_label for f in self.functions]
        if len(set(labels)) != len(labels):
            raise ValueError('functions must have unique function_labels')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SupportabilityProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'spro')


class ExternalDependency(BaseModel):
    """One external dependency bound to an exact function (exdep-
    prefix) — never to a device as a whole (#792 sections 1/2).

    ``required_for_restore`` marks dependencies the known-good recovery
    path (#592) needs — e.g. an activation server reachable at restore
    time. ``observed_at_utc`` + ``source_label`` are mandatory: a
    dependency declaration is time-sensitive evidence, not an eternal
    product truth.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    dependency_id: str
    dependency_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef | None = None
    function_label: str = Field(min_length=1)
    dependency_kind: ExternalDependencyKind
    provider_label: str | None = None
    endpoint_class: EndpointClass = 'unknown'
    requires_account: bool = False
    requires_entitlement: bool = False
    required_for_restore: bool = False
    source_label: str = Field(min_length=1)
    observed_at_utc: str
    evidence_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ExternalDependency':
        if self.profile_ref is not None:
            _require_refs(self.profile_ref)
        if self.evidence_ref is not None:
            _require_refs(self.evidence_ref)
        _validate_instant(self.observed_at_utc, 'observed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'dependency_id', 'dependency_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ExternalDependency':
        return _seal(
            cls, payload, 'dependency_id', 'dependency_sha256', 'exdep')


class LifecycleRiskObservation(BaseModel):
    """One time-stamped lifecycle fact about a subject (lro- prefix).

    Manufacturer support states are *evidenced* states bound to a source
    and observation date — never predictions from product age (#792
    section 5). ``review_by_utc`` bounds the evidence horizon (#792
    section 15): observations past it must be rechecked before
    procurement/replacement decisions, and ``superseded_by_ref`` composes
    with #765 so a newer notice retires an older page.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    kind: LifecycleObservationKind
    support_state: ManufacturerSupportState = 'unknown'
    licence: LicenceEntitlement | None = None
    software: SoftwareAvailability | None = None
    integration: IntegrationDependency | None = None
    serviceability: ServiceabilitySnapshot | None = None
    source_label: str = Field(min_length=1)
    observed_at_utc: str
    review_by_utc: str | None = None
    superseded_by_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'LifecycleRiskObservation':
        _require_refs(self.subject_ref)
        if self.superseded_by_ref is not None:
            _require_refs(self.superseded_by_ref)
        _validate_instant(self.observed_at_utc, 'observed_at_utc')
        if self.review_by_utc is not None:
            _validate_instant(self.review_by_utc, 'review_by_utc')
            if _parse_instant(self.review_by_utc) < _parse_instant(
                    self.observed_at_utc):
                raise ValueError(
                    'review_by_utc is before observed_at_utc')
        detail: tuple[tuple[str, Any], ...] = (
            ('licence_entitlement_state', self.licence),
            ('firmware_availability', self.software),
            ('software_availability', self.software),
            ('update_channel_status', self.software),
            ('driver_api_status', self.integration),
            ('warranty_service_state', self.serviceability),
            ('replacement_lead_time', self.serviceability),
        )
        for kind, section in detail:
            if self.kind == kind and section is None:
                raise ValueError(
                    f'{kind} observation requires its detail section')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'LifecycleRiskObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'lro')


class OfflineContinuityEvidence(BaseModel):
    """One authorized, non-destructive offline-continuity test (oce-
    prefix) — #792 section 3.

    The record cannot be sealed unless ``authorized`` is true and
    ``destructive`` is false: evidence gathered by unauthorized or
    destructive actions is not admissible authority. Marketing terms such
    as 'local control' are declarations, not evidence — only tested
    outcomes record CONTINUE_LOCAL/DEGRADED/UNAVAILABLE, and anything
    untested stays ``unknown``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    function_label: str = Field(min_length=1)
    condition: OfflineCondition
    outcome: ContinuityOutcome
    test_method: str = Field(min_length=1)
    authorized: bool
    destructive: bool
    observed_at_utc: str
    tester_label: str | None = None
    dependency_refs: tuple[AuthorityRef, ...] = ()
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'OfflineContinuityEvidence':
        if not self.authorized:
            raise ValueError(
                'continuity evidence requires an authorized test')
        if self.destructive:
            raise ValueError(
                'continuity evidence requires a non-destructive test')
        _validate_instant(self.observed_at_utc, 'observed_at_utc')
        for ref in self.dependency_refs:
            _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'OfflineContinuityEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'oce')


class ReplacementReadiness(BaseModel):
    """Replacement-candidate readiness evidence for one component
    (rpr- prefix) — #792 section 10.

    Records the criteria a replacement must satisfy: adapters/cabling,
    control/API migration, configuration portability, licence
    portability, physical fit, and the composed #596 requalification
    scope plus #592 backup/restore path. A readiness claim can never be
    'ready' while the requalification scope or restore path is unbound —
    the evaluator fails closed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    readiness_id: str
    readiness_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    candidate_label: str = Field(min_length=1)
    required_adapters: tuple[str, ...] = ()
    control_migration_state: ControlMigrationState = 'unknown'
    config_portability: PortabilityState = 'unknown'
    licence_portability: PortabilityState = 'unknown'
    physical_fit: PortabilityState = 'unknown'
    requalification_scope_ref: AuthorityRef | None = None
    backup_restore_path_ref: AuthorityRef | None = None
    assessed_at_utc: str
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ReplacementReadiness':
        _require_refs(self.subject_ref)
        if self.requalification_scope_ref is not None:
            _require_refs(self.requalification_scope_ref)
        if self.backup_restore_path_ref is not None:
            _require_refs(self.backup_restore_path_ref)
        _validate_instant(self.assessed_at_utc, 'assessed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'readiness_id', 'readiness_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ReplacementReadiness':
        return _seal(
            cls, payload, 'readiness_id', 'readiness_sha256', 'rpr')


def evaluate_offline_continuity(
    function_label: str,
    continuity: tuple[OfflineContinuityEvidence, ...],
    profile: SupportabilityProfile | None = None,
    condition: OfflineCondition | None = None,
) -> tuple[str, str]:
    """DEPENDENCY IMPACT IF UNAVAILABLE for one function (#792
    sections 3/8).

    The verdict comes only from authorized, non-destructive test
    evidence bound to the exact function — a ``local_only`` dependency
    declaration or a 'local control' datasheet claim never infers
    continuity. Any ``unavailable`` dominates ``degraded``, which
    dominates ``unknown``; only evidence that uniformly reports
    ``continue_local`` yields ``continues_local``.
    """
    if profile is not None and function_label not in {
            f.function_label for f in profile.functions}:
        return ('unknown', 'undeclared_function:' + function_label)
    evidence = [
        e for e in continuity
        if e.function_label == function_label
        and (condition is None or e.condition == condition)
    ]
    if not evidence:
        return ('unknown', 'no_authorized_continuity_evidence')
    outcomes = {e.outcome for e in evidence}
    if 'unavailable' in outcomes:
        conditions = sorted(
            {e.condition for e in evidence
             if e.outcome == 'unavailable'})
        return ('unavailable',
                'tested_unavailable_under:' + ','.join(conditions))
    if 'degraded' in outcomes:
        conditions = sorted(
            {e.condition for e in evidence if e.outcome == 'degraded'})
        return ('continues_degraded',
                'tested_degraded_under:' + ','.join(conditions))
    if 'unknown' in outcomes:
        return ('unknown', 'inconclusive_continuity_evidence')
    return ('continues_local',
            'tested:' + str(len(evidence)) + ' evidence record(s)')


def evaluate_recovery_capability(
    *,
    backup_restore_path_ref: AuthorityRef | None,
    software: SoftwareAvailability | None,
    licence: LicenceEntitlement | None = None,
    licence_required: bool = False,
    restore_test: OfflineContinuityEvidence | None = None,
    restore_dependencies: tuple[ExternalDependency, ...] = (),
) -> tuple[str, str]:
    """Known-good recovery composition (#792 section 13).

    RECOVERY CAPABILITY = known-good backup evidence (#592) + obtainable
    tools + licence/account path + firmware availability + target
    compatibility. A backup file alone is never full recovery evidence:
    every required layer must be evidenced, an actual restore test marks
    ``offline_recovery_verified``, and declared-but-unevidenced licence
    or software layers fail closed to ``unknown``.
    """
    if backup_restore_path_ref is None:
        return ('recovery_impossible_with_current_evidence',
                'no_known_good_backup_evidence')
    external = [
        d for d in restore_dependencies
        if d.required_for_restore
        and d.endpoint_class in ('vendor_cloud', 'third_party_service')
    ]
    if external:
        return (
            'recovery_requires_external_service',
            'restore_dependencies:'
            + ','.join(sorted(d.provider_label or d.dependency_kind
                              for d in external)))
    if software is not None \
            and software.restore_requires_vendor_service == 'yes':
        return ('recovery_requires_external_service',
                'restore_requires_vendor_service:'
                + software.component_label)
    if licence_required and licence is None:
        return ('unknown',
                'licence_dependency_declared_but_unevidenced')
    if licence is not None and (
            licence.activation_dependency in (
                'vendor_activation_server', 'account_sign_in',
                'network_service')
            or licence.licence_model in ('subscription', 'trial')):
        return ('recovery_requires_active_entitlement',
                'licence:' + licence.feature_label)
    if software is None:
        return ('unknown', 'no_software_availability_evidence')
    for field in ('installer_obtainable', 're_provisionable_after_reset',
                  'host_os_compatible'):
        if getattr(software, field) == 'no':
            return ('recovery_impossible_with_current_evidence',
                    field + ':no')
    for field in ('installer_obtainable', 're_provisionable_after_reset'):
        if getattr(software, field) == 'unknown':
            return ('unknown', 'incomplete_software_evidence:' + field)
    if restore_test is not None:
        if restore_test.outcome == 'continue_local':
            return ('offline_recovery_verified',
                    'restore_tested_under:' + restore_test.condition)
        return ('unknown',
                'restore_test_inconclusive:' + restore_test.outcome)
    return ('recovery_untested',
            'backup_and_tools_evidenced_no_restore_test')


def evaluate_evidence_freshness(
    observation: LifecycleRiskObservation,
    evaluated_at_utc: str,
) -> tuple[str, str]:
    """Evidence-horizon verdict for one observation (#792 section 15).

    ``superseded`` when #765-style change evidence replaced it,
    ``review_due`` once ``review_by_utc`` has passed (recheck before
    procurement/replacement decisions), ``current`` inside the horizon.
    No declared horizon fails closed to ``unknown``.
    """
    if observation.superseded_by_ref is not None:
        return ('superseded',
                'replaced_by:' + observation.superseded_by_ref.ref_id)
    if observation.review_by_utc is None:
        return ('unknown', 'no_review_horizon_declared')
    _validate_instant(evaluated_at_utc, 'evaluated_at_utc')
    if _parse_instant(observation.review_by_utc) \
            <= _parse_instant(evaluated_at_utc):
        return ('review_due',
                'review_by:' + observation.review_by_utc)
    return ('current', 'evidence_within_review_horizon')


def evaluate_security_maintenance(
    observation: LifecycleRiskObservation | None,
) -> tuple[str, str]:
    """Security-maintenance lifecycle state (#792 section 14).

    End of security support marks the maintenance state and routes the
    risk/mitigation decision through #598 — this evaluator never calls a
    device compromised and never produces a scare score. A superseded
    observation fails closed to ``unknown``.
    """
    if observation is None:
        return ('unknown', 'no_support_observation')
    if observation.superseded_by_ref is not None:
        return ('unknown', 'superseded_observation')
    state = observation.support_state
    if state == 'currently_supported':
        return ('security_maintenance_available',
                'support_state:currently_supported')
    if state in ('limited_security_only', 'end_of_sale',
                 'end_of_software_updates'):
        return ('security_maintenance_limited',
                'support_state:' + state)
    if state in ('end_of_security_support', 'end_of_service',
                 'discontinued_cloud_dependency'):
        return ('security_maintenance_ended',
                'support_state:' + state
                + ' — route risk/mitigation decision via #598')
    return ('unknown', 'support_state:' + state)


def evaluate_licence_deadline(
    licence: LicenceEntitlement | None,
    evaluated_at_utc: str,
) -> tuple[str, str]:
    """Licence/entitlement deadline state (#792 sections 4/15).

    Reports the evidenced lifecycle position only — expiry visibility
    without ever carrying key material.
    """
    if licence is None:
        return ('unknown', 'no_licence_evidence')
    _validate_instant(evaluated_at_utc, 'evaluated_at_utc')
    now = _parse_instant(evaluated_at_utc)
    if licence.licence_state in ('expired', 'revoked'):
        return ('expired', 'licence_state:' + licence.licence_state)
    if licence.expires_at_utc is not None \
            and _parse_instant(licence.expires_at_utc) <= now:
        return ('expired', 'expires_at_utc:' + licence.expires_at_utc)
    if licence.licence_state == 'renewal_pending' or (
            licence.renewal_due_utc is not None
            and _parse_instant(licence.renewal_due_utc) <= now):
        return ('renewal_due', 'renewal_due_utc:'
                + str(licence.renewal_due_utc))
    if licence.licence_state == 'grace_period':
        return ('renewal_due', 'licence_state:grace_period')
    if licence.licence_state == 'active':
        return ('active', 'licence_state:active')
    return ('unknown', 'licence_state:' + licence.licence_state)


def evaluate_replacement_readiness(
    readiness: ReplacementReadiness | None,
) -> tuple[str, str]:
    """Replacement-readiness verdict (#792 section 10).

    ``replacement_ready`` requires every portability criterion confirmed,
    a compatible control migration, AND the composed #596
    requalification scope plus #592 backup/restore path pinned — a
    replacement can never be ready while requalification is unbound.
    ``not_possible``/``incompatible`` criteria block outright; anything
    unresolved fails closed to ``replacement_unverified``.
    """
    if readiness is None:
        return ('replacement_unverified', 'no_readiness_record')
    blocking = []
    for field in ('config_portability', 'licence_portability',
                  'physical_fit'):
        if getattr(readiness, field) == 'not_possible':
            blocking.append(field)
    if readiness.control_migration_state == 'incompatible':
        blocking.append('control_migration_state')
    if blocking:
        return ('replacement_blocked', 'not_possible:' + ','.join(
            blocking))
    if readiness.requalification_scope_ref is None:
        return ('replacement_unverified',
                'no_requalification_scope_ref (#596 not satisfied)')
    if readiness.backup_restore_path_ref is None:
        return ('replacement_unverified',
                'no_backup_restore_path_ref (#592 path unbound)')
    criteria = (readiness.config_portability,
                readiness.licence_portability, readiness.physical_fit)
    if all(c == 'confirmed' for c in criteria) \
            and readiness.control_migration_state == 'compatible':
        return ('replacement_ready',
                'candidate:' + readiness.candidate_label)
    if any(c == 'requires_work' for c in criteria) \
            or readiness.control_migration_state == 'migration_required':
        return ('replacement_requires_work',
                'open_criteria_require_work')
    return ('replacement_unverified', 'unresolved_criteria')
