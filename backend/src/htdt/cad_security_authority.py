"""Networked AV security authority (issue #598).

The #591 transport authority (:mod:`cad_network_av`) proves a networked AV
system can *carry* its workloads. This authority is the parallel
lifecycle dimension: a system can be transport-qualified while remaining
insecure, over-privileged or operationally unrecoverable — credentials,
management surfaces, firmware/patch state, remote-service exposure,
backup sensitivity and decommissioning are versioned here, separately
from any packet-performance evidence.

Standards basis (see docs/reviews/rev56-ops.md): AVIXA RP-C303.01:2018
*Recommended Practices for Security in Networked Audiovisual Systems* —
inventory, responsibility assignment, updates/patches/decommissioning,
documentation and regular authorized testing — registered through the
#599 external-standards registry as ``avixa-rp-c303-01@2018`` (the 2025
revision task group is tracked as lifecycle context, never as content).

Contract properties:

- this module stores **metadata and status only** — account identities,
  storage *references*, rotation state. Passwords, tokens, private keys
  and bearer material have no fields here; ``account_ref`` is a label,
  and values carrying embedded credentials (``scheme://user:pw@host``)
  are rejected outright;
- "network reachable" never implies "secure" and "latest firmware" never
  implies "safe" — every state is an explicit reviewed value or UNKNOWN;
- privilege is a *taxonomy*, not a flag — a read-only telemetry token and
  a firmware-capable API key are different records
  (:data:`AccessCapability`);
- remote monitoring/control requires a sealed
  :class:`RemoteServiceAuthorization` naming provider, scope and consent
  — remote collection never becomes an implied grant;
- risks are qualitative per-record
  (:class:`SecurityRiskRecord`) — no fabricated breach probabilities;
  every mitigation carries an independent *functional AV effect* field so
  a "more secure" change is never applied blind to AV requirements
  (#598 §15);
- security testing evidence is scope- and authorization-bound
  (:class:`SecurityTestEvidence`) — HTDT never performs intrusive
  scanning itself;
- decommissioning is first-class: an asset whose credentials were never
  revoked stays ``removed_pending_credential_revocation``, not
  ``decommissioned``;
- the review verdict is fail-closed: :func:`evaluate_security_review`
  requires bound declarations/observations — an unassessed system reads
  ``unknown``, not "probably fine".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SECURITY_AUTHORITY_SCHEMA_VERSION = 1

SECURITY_ASSET_AUTHORITY_VERSION = 'rev56-security-asset-1'
CREDENTIAL_AUTHORITY_VERSION = 'rev56-credential-1'
SURFACE_AUTHORITY_VERSION = 'rev56-surface-1'
SECURITY_OBSERVATION_AUTHORITY_VERSION = 'rev56-security-observation-1'
SECURITY_RISK_AUTHORITY_VERSION = 'rev56-security-risk-1'
REMOTE_AUTHORITY_VERSION = 'rev56-remote-service-1'
SECURITY_TEST_AUTHORITY_VERSION = 'rev56-security-test-1'
ACCESS_REVIEW_AUTHORITY_VERSION = 'rev56-access-review-1'
SECURITY_REVIEW_AUTHORITY_VERSION = 'rev56-security-review-1'
SECURITY_EVALUATION_VERSION = 'rev56-security-eval-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _reject_embedded_secret(value: str, label: str) -> None:
    """Credential/identity fields are references, never secret material."""
    lowered = value.lower()
    if '://' in value and '@' in value:
        raise ValueError(
            f'{label} must be a reference, not a credential-bearing URI'
        )
    for marker in ('private key', 'password=', 'secret=', 'bearer '):
        if marker in lowered:
            raise ValueError(
                f'{label} must never carry secret material '
                f'(marker {marker!r} found) — store references only'
            )


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

AccessCapability = Literal[
    'view_telemetry',
    'measure_diagnose',
    'change_av_settings',
    'apply_configuration',
    'firmware_update',
    'network_admin',
    'backup_restore',
    'security_admin',
    'unknown',
]
"""Least-privilege capability taxonomy (#598 §3). An integration key able
to change firmware or network policy is never equivalent to a read-only
telemetry token; ``unknown`` is the honest "not classified" state."""

ManagementReachability = Literal[
    'device_local',
    'management_vlan',
    'lan',
    'wan_internet',
    'vendor_cloud',
    'mixed',
    'unknown',
]
"""Where the asset's management plane is reachable from (#598 §5). This
describes *exposure composition* — the topology claim itself belongs to
#591's ``NetworkAVPath`` and is pinned by reference only."""

VendorSupportStatus = Literal[
    'supported', 'limited', 'eol_announced', 'eol', 'unknown'
]
"""Vendor security/support status for the installed firmware/software
(#598 §6). ``eol`` is not automatically vulnerable, and ``supported`` is
not automatically safe — it is context, always paired with review
evidence."""

SecurityLifecycleState = Literal[
    'active',
    'quarantined',
    'removed_pending_credential_revocation',
    'decommissioned',
    'unknown',
]
"""Access-lifecycle state (#598 §14). A removed device whose accounts or
tokens were never revoked is ``removed_pending_credential_revocation`` —
it may not claim ``decommissioned``."""

BackupSensitivity = Literal[
    'public', 'local_private', 'secret_bearing', 'unknown'
]
"""Sensitivity class of associated backup/config artifacts (#598 §10) —
aligned with the #592 artifact sensitivity classes; ``secret_bearing``
artifacts must never enter normal report exports."""

SecurityObservationKind = Literal[
    'credential_state',
    'surface_state',
    'firmware_state',
    'service_inventory',
    'certificate_state',
    'backup_sensitivity',
    'access_state',
    'manual_review',
    'other',
]

SecurityEvidenceClass = Literal[
    'device_readback',
    'configuration_audit',
    'vendor_documentation',
    'authorized_scan',
    'user_recorded',
    'inferred',
    'unknown',
]

ImpactDomain = Literal[
    'availability',
    'confidentiality',
    'integrity',
    'safety',
    'privacy',
    'operational',
]

LikelihoodClass = Literal['low', 'medium', 'high', 'unknown']
"""Qualitative likelihood only — no fabricated breach probabilities."""

RiskStatus = Literal['open', 'mitigated', 'accepted', 'transferred', 'unknown']

FunctionalAVEffect = Literal[
    'none',
    'degrades_feature',
    'breaks_dependency',
    'requires_reverification',
    'unknown',
]
"""What a mitigation does to AV functionality (#598 §15): disabling
discovery can break commissioning, strict ACLs can block control paths,
firmware updates can change HDMI/DSP behaviour. Independent axis."""

RemoteServiceMethod = Literal[
    'vendor_cloud',
    'vpn',
    'support_tunnel',
    'remote_desktop',
    'on_premises_only',
    'none',
    'unknown',
]

SecurityTestKind = Literal[
    'configuration_audit',
    'known_service_inventory',
    'vendor_security_assessment',
    'vulnerability_scan_report',
    'penetration_test_summary',
    'manual_review',
    'other',
]

AccessReviewTrigger = Literal[
    'handoff', 'provider_change', 'decommission', 'periodic',
    'incident', 'other',
]

AccessReviewOutcome = Literal['retained', 'disabled', 'revoked', 'unknown']

SecurityCheck = Literal[
    'inventory',
    'credentials',
    'surfaces',
    'firmware',
    'remote_access',
    'backup_sensitivity',
    'access_review',
    'decommission',
]

SecurityCheckResult = Literal[
    'verified', 'limited', 'failed', 'not_applicable'
]

SecurityReviewState = Literal[
    'reviewed',
    'reviewed_with_limitations',
    'risk_accepted',
    'mitigation_required',
    'high_risk_exposure',
    'unknown',
    'not_applicable',
]
"""Profile states (#598 §7) — no opaque single cybersecurity score; the
exact checks/reasons carry the meaning."""


# ---------------------------------------------------------------------------
# Security asset declaration (#598 §1/§13/§14)
# ---------------------------------------------------------------------------


class SecurityAssetDeclaration(BaseModel):
    """Security-relevant metadata overlay for one networked AV resource.

    ``subject_ref`` binds an existing authority identity (installed
    equipment instance, network node, service) — this record never
    duplicates the general equipment inventory; it carries only the
    security dimensions the equipment authority does not own.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-security-asset-1'
    ] = SECURITY_ASSET_AUTHORITY_VERSION
    asset_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    security_owner: str | None = None
    """Responsible party for this asset's security state; None = unowned
    (which the review records, never assumes)."""
    management_reachability: ManagementReachability = 'unknown'
    vendor_support_status: VendorSupportStatus = 'unknown'
    current_firmware_label: str | None = None
    newer_version_known: bool | None = None
    last_firmware_review_at_utc: str | None = None
    update_policy_note: str | None = None
    rollback_capability_refs: tuple[AuthorityRef, ...] = ()
    """Pins into #592 snapshot/firmware-transition evidence when a
    rollback path is claimed — the label alone is not evidence."""
    firmware_evidence_refs: tuple[AuthorityRef, ...] = ()
    cloud_dependency: Literal[
        'none', 'optional', 'required', 'unknown'
    ] = 'unknown'
    cloud_provider_label: str | None = None
    certificate_dependency: Literal[
        'none', 'tls_required', 'expiry_tracked', 'unknown'
    ] = 'unknown'
    time_dependency_note: str | None = None
    """Where device clock correctness gates certificates/auth (#598
    §13) — declared, never invented for devices exposing none."""
    lifecycle_state: SecurityLifecycleState = 'unknown'
    backup_sensitivity: BackupSensitivity = 'unknown'
    backup_redaction_state: Literal[
        'redacted', 'unredacted', 'not_applicable', 'unknown'
    ] = 'unknown'
    segmentation_note: str | None = None
    notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    asset_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'asset_id', 'asset_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SecurityAssetDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.last_firmware_review_at_utc is not None:
            _require_iso8601(
                self.last_firmware_review_at_utc,
                'last_firmware_review_at_utc',
            )
        if self.lifecycle_state == 'decommissioned':
            raise ValueError(
                "'decommissioned' is only reachable through an "
                "AccessReviewRecord showing revoked access — declare "
                "'removed_pending_credential_revocation' until then"
            )
        if (
            self.backup_sensitivity == 'secret_bearing'
            and self.backup_redaction_state == 'unredacted'
        ):
            raise ValueError(
                'a secret-bearing backup may not be declared unredacted '
                'without review evidence — use a SecurityObservation '
                'first'
            )
        digest = _hash(self.identity_payload())
        if self.asset_sha256 != digest:
            raise ValueError('security asset hash mismatch')
        if self.asset_id != _semantic_id('secasset', digest):
            raise ValueError('security asset id mismatch')
        return self


# ---------------------------------------------------------------------------
# Credential / account authority (#598 §2/§3/§12)
# ---------------------------------------------------------------------------

CredentialKind = Literal[
    'named_account',
    'shared_account',
    'service_account',
    'vendor_support_account',
    'api_token',
    'certificate',
    'unknown',
]

CredentialScope = Literal[
    'device_local', 'local_network', 'vendor_cloud', 'provider', 'unknown'
]

CredentialStorageRef = Literal[
    'htdt_managed',
    'external_secret_store',
    'vendor_cloud_account',
    'not_stored_ephemeral',
    'documented_elsewhere',
    'unknown',
]
"""Where the *secret* lives. This authority stores the reference class —
never the secret. There is intentionally no field that could hold a
password, token or key."""

MFAState = Literal[
    'enabled', 'available_not_enabled', 'unsupported', 'unknown'
]

DefaultCredentialState = Literal[
    'default_changed', 'default_still_active', 'never_default', 'unknown'
]

CredentialState = Literal['active', 'disabled', 'revoked', 'expired', 'unknown']

RotationState = Literal['current', 'overdue', 'no_policy', 'unknown']


class CredentialRecord(BaseModel):
    """Metadata about one account/credential — references and status
    only (#598 §2/§12).

    The model has no field capable of holding secret material by
    construction, and ``account_ref`` rejects credential-bearing strings.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-credential-1'
    ] = CREDENTIAL_AUTHORITY_VERSION
    credential_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    account_ref: str = Field(min_length=1)
    """Human/audit reference for the account (label or external id) —
    never the secret itself."""
    kind: CredentialKind = 'unknown'
    capabilities: tuple[AccessCapability, ...] = ()
    """Least-privilege classification of what this account can do."""
    scope: CredentialScope = 'unknown'
    storage_reference: CredentialStorageRef = 'unknown'
    mfa_state: MFAState = 'unknown'
    default_credential_state: DefaultCredentialState = 'unknown'
    rotation_state: RotationState = 'unknown'
    rotation_policy_note: str | None = None
    former_holder: bool = False
    """Marks accounts held by former staff/integrators — reviewable and
    revocable on handoff or provider change (#598 §12)."""
    recovery_dependency_note: str | None = None
    state: CredentialState = 'unknown'
    declared_at_utc: str = Field(min_length=1)
    credential_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'credential_id', 'credential_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'CredentialRecord':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _reject_embedded_secret(self.account_ref, 'account_ref')
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('credential capabilities must be unique')
        if (
            self.state in ('revoked', 'expired')
            and self.former_holder
            and self.default_credential_state == 'default_still_active'
        ):
            # A revoked default account is a normal healthy state; the
            # guard exists so a revoked record cannot simultaneously
            # claim the default is still live on the device.
            raise ValueError(
                'a revoked/expired credential cannot also claim '
                'default_still_active'
            )
        digest = _hash(self.identity_payload())
        if self.credential_sha256 != digest:
            raise ValueError('credential record hash mismatch')
        if self.credential_id != _semantic_id('seccred', digest):
            raise ValueError('credential record id mismatch')
        return self


# ---------------------------------------------------------------------------
# Management-surface declaration (#598 §4)
# ---------------------------------------------------------------------------

ManagementSurfaceKind = Literal[
    'web_ui',
    'ssh_shell',
    'snmp_telemetry',
    'vendor_api',
    'mobile_cloud_app',
    'remote_desktop',
    'support_tunnel',
    'serial_control',
    'mdns_discovery',
    'other_network_service',
    'unknown',
]

SurfaceRequirement = Literal['required', 'optional', 'unknown']
"""Whether the project needs this surface. Unused enabled surfaces are
risk candidates — but HTDT never disables them itself (#598 §4)."""

SurfaceState = Literal['enabled', 'disabled', 'unknown']

ExposureScope = Literal[
    'device_local',
    'management_vlan',
    'lan',
    'wan_internet',
    'vendor_cloud',
    'unknown',
]

AuthenticationState = Literal[
    'authenticated', 'unauthenticated', 'inherited', 'unknown'
]


class ManagementSurfaceDeclaration(BaseModel):
    """One enabled/available management surface on an asset (#598 §4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-surface-1'
    ] = SURFACE_AUTHORITY_VERSION
    surface_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    kind: ManagementSurfaceKind = 'unknown'
    requirement: SurfaceRequirement = 'unknown'
    state: SurfaceState = 'unknown'
    exposure_scope: ExposureScope = 'unknown'
    authentication_state: AuthenticationState = 'unknown'
    provider_label: str | None = None
    version_label: str | None = None
    project_dependency_note: str | None = None
    declared_at_utc: str = Field(min_length=1)
    surface_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'surface_id', 'surface_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ManagementSurfaceDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        digest = _hash(self.identity_payload())
        if self.surface_sha256 != digest:
            raise ValueError('management surface hash mismatch')
        if self.surface_id != _semantic_id('secsurf', digest):
            raise ValueError('management surface id mismatch')
        return self


# ---------------------------------------------------------------------------
# Security observation (#598 §6/§11 evidence)
# ---------------------------------------------------------------------------

SecurityObservationOutcome = Literal[
    'observed', 'observed_concern', 'inconclusive'
]


class SecurityObservation(BaseModel):
    """One bound security-evidence record (#598 §6/§11).

    Observations are evidence, not enforcement: this authority has no
    scanning or probing capability — ``authorized_scan`` evidence records
    the *result reference* of a scan someone else was authorized to run.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-security-observation-1'
    ] = SECURITY_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    kind: SecurityObservationKind
    outcome: SecurityObservationOutcome = 'observed'
    credential_id: str | None = None
    surface_id: str | None = None
    observed_value_repr: str | None = None
    detail_note: str | None = None
    evidence_class: SecurityEvidenceClass = 'unknown'
    evidence_refs: tuple[AuthorityRef, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'observation_id', 'observation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SecurityObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        digest = _hash(self.identity_payload())
        if self.observation_sha256 != digest:
            raise ValueError('security observation hash mismatch')
        if self.observation_id != _semantic_id('secobs', digest):
            raise ValueError('security observation id mismatch')
        return self


# ---------------------------------------------------------------------------
# Risk record (#598 §8/§15)
# ---------------------------------------------------------------------------


class SecurityRiskRecord(BaseModel):
    """One qualitative risk record — never a numeric breach probability
    (#598 §8). ``functional_av_effect`` keeps the security-vs-availability
    trade-off explicit (#598 §15)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-security-risk-1'
    ] = SECURITY_RISK_AUTHORITY_VERSION
    risk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef | None = None
    title: str = Field(min_length=1)
    threat_description: str | None = None
    impact_domains: tuple[ImpactDomain, ...] = ()
    likelihood_class: LikelihoodClass = 'unknown'
    evidence_refs: tuple[AuthorityRef, ...] = ()
    existing_controls_note: str | None = None
    mitigation_plan_note: str | None = None
    functional_av_effect: FunctionalAVEffect = 'unknown'
    risk_owner: str | None = None
    status: RiskStatus = 'unknown'
    review_at_utc: str | None = None
    source_profile_ref: AuthorityRef | None = None
    """Pin to the #599 external standard/profile whose guidance frames
    this risk (e.g. ``avixa-rp-c303-01@2018``)."""
    raised_at_utc: str = Field(min_length=1)
    risk_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'risk_id', 'risk_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SecurityRiskRecord':
        _require_iso8601(self.raised_at_utc, 'raised_at_utc')
        if self.review_at_utc is not None:
            _require_iso8601(self.review_at_utc, 'review_at_utc')
        if len(set(self.impact_domains)) != len(self.impact_domains):
            raise ValueError('impact domains must be unique')
        if self.status == 'accepted' and (
            self.risk_owner is None or self.review_at_utc is None
        ):
            raise ValueError(
                "an 'accepted' risk requires a named owner and a review "
                'date — acceptance is a decision, not a default'
            )
        if self.status == 'mitigated' and not self.mitigation_plan_note:
            raise ValueError(
                "a 'mitigated' risk requires the mitigation that was "
                'applied'
            )
        digest = _hash(self.identity_payload())
        if self.risk_sha256 != digest:
            raise ValueError('security risk hash mismatch')
        if self.risk_id != _semantic_id('secrisk', digest):
            raise ValueError('security risk id mismatch')
        return self


# ---------------------------------------------------------------------------
# Remote service / monitoring authorization (#598 §9)
# ---------------------------------------------------------------------------

SessionLoggingState = Literal[
    'required', 'available', 'unavailable', 'unknown'
]

RemoteAuthorizationState = Literal[
    'authorized', 'expired', 'revoked', 'unknown'
]


class RemoteServiceAuthorization(BaseModel):
    """Explicit authorization boundary for remote support/monitoring
    (#598 §9). Remote monitoring never implies unrestricted device
    control — the allowed capability set is the record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-remote-service-1'
    ] = REMOTE_AUTHORITY_VERSION
    authorization_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    method: RemoteServiceMethod = 'unknown'
    provider_account_ref: str | None = None
    allowed_capabilities: tuple[AccessCapability, ...] = ()
    allowed_actions_note: str | None = None
    session_logging: SessionLoggingState = 'unknown'
    consent_ref: str = Field(min_length=1)
    """Owner consent / policy reference — required, because remote access
    without documented consent is not authorizable."""
    valid_from_utc: str | None = None
    valid_until_utc: str | None = None
    state: RemoteAuthorizationState = 'authorized'
    declared_at_utc: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authorization_id', 'authorization_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'RemoteServiceAuthorization':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, value in (
            ('valid_from_utc', self.valid_from_utc),
            ('valid_until_utc', self.valid_until_utc),
        ):
            if value is not None:
                _require_iso8601(value, label)
        if (
            self.valid_from_utc is not None
            and self.valid_until_utc is not None
            and self.valid_until_utc <= self.valid_from_utc
        ):
            raise ValueError('valid_until before valid_from')
        if self.provider_account_ref is not None:
            _reject_embedded_secret(
                self.provider_account_ref, 'provider_account_ref'
            )
        if len(set(self.allowed_capabilities)) != len(
            self.allowed_capabilities
        ):
            raise ValueError('allowed capabilities must be unique')
        if self.state == 'authorized' and self.method == 'none':
            raise ValueError(
                "an authorized remote service cannot declare method "
                "'none' — 'on_premises_only' is the local-only claim"
            )
        digest = _hash(self.identity_payload())
        if self.authorization_sha256 != digest:
            raise ValueError('remote authorization hash mismatch')
        if self.authorization_id != _semantic_id('secrem', digest):
            raise ValueError('remote authorization id mismatch')
        return self


# ---------------------------------------------------------------------------
# Authorized security testing evidence (#598 §11)
# ---------------------------------------------------------------------------


class SecurityTestEvidence(BaseModel):
    """Record of an *authorized* security review performed by someone
    else (#598 §11). HTDT performs no intrusive scanning itself — this
    record pins scope, authorization, tool identity, findings and
    remediation of an external review."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-security-test-1'
    ] = SECURITY_TEST_AUTHORITY_VERSION
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: SecurityTestKind
    scope_note: str = Field(min_length=1)
    authorization_ref: str = Field(min_length=1)
    """Owner authorization reference — a test recorded without it is not
    admissible evidence."""
    tool_provider: str | None = None
    tool_version: str | None = None
    performed_at_utc: str = Field(min_length=1)
    finding_refs: tuple[str, ...] = ()
    """Risk/observation ids this evidence produced."""
    remediation_note: str | None = None
    report_ref: AuthorityRef | None = None
    evidence_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'evidence_id', 'evidence_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SecurityTestEvidence':
        _require_iso8601(self.performed_at_utc, 'performed_at_utc')
        digest = _hash(self.identity_payload())
        if self.evidence_sha256 != digest:
            raise ValueError('security test evidence hash mismatch')
        if self.evidence_id != _semantic_id('sectest', digest):
            raise ValueError('security test evidence id mismatch')
        return self


# ---------------------------------------------------------------------------
# Access review / revocation (#598 §12/§14)
# ---------------------------------------------------------------------------


class AccessReviewOutcomeEntry(BaseModel):
    """Per-credential outcome inside one access review."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    credential_id: str = Field(min_length=1)
    outcome: AccessReviewOutcome
    note: str | None = None


class AccessReviewRecord(BaseModel):
    """A review/revocation pass over credentials at handoff, provider
    change, decommission or periodic audit (#598 §12/§14)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-access-review-1'
    ] = ACCESS_REVIEW_AUTHORITY_VERSION
    review_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    trigger: AccessReviewTrigger
    reviewer: str = Field(min_length=1)
    performed_at_utc: str = Field(min_length=1)
    outcomes: tuple[AccessReviewOutcomeEntry, ...] = ()
    open_item_refs: tuple[str, ...] = ()
    """Credential/risk ids still needing action after this review —
    non-empty means the review did not close."""
    notes: str | None = None
    review_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'review_id', 'review_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'AccessReviewRecord':
        _require_iso8601(self.performed_at_utc, 'performed_at_utc')
        ids = [o.credential_id for o in self.outcomes]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate credential outcome in review')
        digest = _hash(self.identity_payload())
        if self.review_sha256 != digest:
            raise ValueError('access review hash mismatch')
        if self.review_id != _semantic_id('secacc', digest):
            raise ValueError('access review id mismatch')
        return self


# ---------------------------------------------------------------------------
# Security review (evaluation verdict)
# ---------------------------------------------------------------------------


class SecurityReview(BaseModel):
    """Sealed review verdict for the security posture of a document's
    networked AV assets (#598 §7). Produced only by
    :func:`evaluate_security_review`."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SECURITY_AUTHORITY_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-security-review-1'
    ] = SECURITY_REVIEW_AUTHORITY_VERSION
    review_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef | None = None
    """Versioned external profile pin — e.g. the #599 registry key for
    ``avixa-rp-c303-01@2018``. The exact bound edition is part of the
    review identity; a future revision never rewrites this record."""
    evaluation_version: str = Field(min_length=1)
    state: SecurityReviewState
    checks: tuple[tuple[SecurityCheck, SecurityCheckResult], ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    risk_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    review_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'review_id', 'review_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SecurityReview':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.state == 'reviewed' and not self.evidence_refs:
            raise ValueError(
                "a 'reviewed' verdict requires bound evidence — "
                'security never floats free of observations'
            )
        digest = _hash(self.identity_payload())
        if self.review_sha256 != digest:
            raise ValueError('security review hash mismatch')
        if self.review_id != _semantic_id('secrev', digest):
            raise ValueError('security review id mismatch')
        return self


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_sealed(model: type[BaseModel], sha_field: str,
                  id_field: str, prefix: str,
                  authority_version: str,
                  **kwargs: Any) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(
            model,
            dict(
                schema_version=SECURITY_AUTHORITY_SCHEMA_VERSION,
                authority_version=authority_version,
                **{sha_field: '0' * 64, id_field: ''},
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={sha_field, id_field}),
        **{sha_field: sha, id_field: _semantic_id(prefix, sha)},
    )


def build_security_asset(**kwargs: Any) -> SecurityAssetDeclaration:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        SecurityAssetDeclaration, 'asset_sha256', 'asset_id',
        'secasset', SECURITY_ASSET_AUTHORITY_VERSION, **kwargs,
    )


def build_credential(**kwargs: Any) -> CredentialRecord:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        CredentialRecord, 'credential_sha256', 'credential_id',
        'seccred', CREDENTIAL_AUTHORITY_VERSION, **kwargs,
    )


def build_management_surface(**kwargs: Any) -> ManagementSurfaceDeclaration:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        ManagementSurfaceDeclaration, 'surface_sha256', 'surface_id',
        'secsurf', SURFACE_AUTHORITY_VERSION, **kwargs,
    )


def build_security_observation(**kwargs: Any) -> SecurityObservation:
    kwargs.setdefault('observed_at_utc', _utc_now())
    return _build_sealed(
        SecurityObservation, 'observation_sha256', 'observation_id',
        'secobs', SECURITY_OBSERVATION_AUTHORITY_VERSION, **kwargs,
    )


def build_security_risk(**kwargs: Any) -> SecurityRiskRecord:
    kwargs.setdefault('raised_at_utc', _utc_now())
    return _build_sealed(
        SecurityRiskRecord, 'risk_sha256', 'risk_id',
        'secrisk', SECURITY_RISK_AUTHORITY_VERSION, **kwargs,
    )


def build_remote_authorization(
    **kwargs: Any,
) -> RemoteServiceAuthorization:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        RemoteServiceAuthorization,
        'authorization_sha256', 'authorization_id',
        'secrem', REMOTE_AUTHORITY_VERSION, **kwargs,
    )


def build_security_test_evidence(**kwargs: Any) -> SecurityTestEvidence:
    return _build_sealed(
        SecurityTestEvidence, 'evidence_sha256', 'evidence_id',
        'sectest', SECURITY_TEST_AUTHORITY_VERSION, **kwargs,
    )


def build_access_review(**kwargs: Any) -> AccessReviewRecord:
    return _build_sealed(
        AccessReviewRecord, 'review_sha256', 'review_id',
        'secacc', ACCESS_REVIEW_AUTHORITY_VERSION, **kwargs,
    )


# ---------------------------------------------------------------------------
# Evaluation (#598 §7)
# ---------------------------------------------------------------------------


def evaluate_security_review(
    *,
    document_id: str,
    profile_ref: AuthorityRef | None = None,
    assets: tuple[SecurityAssetDeclaration, ...] = (),
    credentials: tuple[CredentialRecord, ...] = (),
    surfaces: tuple[ManagementSurfaceDeclaration, ...] = (),
    observations: tuple[SecurityObservation, ...] = (),
    risks: tuple[SecurityRiskRecord, ...] = (),
    remote_authorizations: tuple[RemoteServiceAuthorization, ...] = (),
    access_reviews: tuple[AccessReviewRecord, ...] = (),
    evaluated_at_utc: str | None = None,
) -> SecurityReview:
    """Derive the fail-closed security review state (#598 §7).

    Per-dimension checks; every check is ``verified`` / ``limited`` /
    ``failed`` / ``not_applicable`` with explicit reasons — an unassessed
    dimension is a limitation, never a silent pass.
    """
    checks: list[tuple[SecurityCheck, SecurityCheckResult]] = []
    reasons: list[str] = []
    limitations: list[str] = []
    evidence: list[str] = []

    subject_keys = {
        (a.subject_ref.kind, a.subject_ref.ref_id) for a in assets
    }
    cred_by_subject = {
        (c.subject_ref.kind, c.subject_ref.ref_id) for c in credentials
    }
    surf_by_subject = {
        (s.subject_ref.kind, s.subject_ref.ref_id) for s in surfaces
    }

    if not assets:
        checks = [
            ('inventory', 'not_applicable'),
            ('credentials', 'not_applicable'),
            ('surfaces', 'not_applicable'),
            ('firmware', 'not_applicable'),
            ('remote_access', 'not_applicable'),
            ('backup_sensitivity', 'not_applicable'),
            ('access_review', 'not_applicable'),
            ('decommission', 'not_applicable'),
        ]
        reasons.append('no networked AV security assets are declared')
        state: SecurityReviewState = 'not_applicable'
        return _seal_review(
            document_id=document_id,
            profile_ref=profile_ref,
            state=state,
            checks=tuple(checks),
            reasons=tuple(reasons),
            limitations=(),
            risk_refs=(),
            evidence_refs=(),
            evaluated_at_utc=evaluated_at_utc,
        )

    # --- inventory -----------------------------------------------------
    unknowns = [
        a for a in assets
        if a.management_reachability == 'unknown'
        or a.lifecycle_state == 'unknown'
    ]
    if unknowns:
        checks.append(('inventory', 'limited'))
        limitations.append(
            f'{len(unknowns)} asset(s) lack declared reachability/'
            'lifecycle state'
        )
    else:
        checks.append(('inventory', 'verified'))
    evidence.extend(a.asset_id for a in assets[:4])

    # --- credentials ---------------------------------------------------
    active = [c for c in credentials if c.state == 'active']
    default_live = [
        c for c in active
        if c.default_credential_state == 'default_still_active'
    ]
    former_live = [c for c in active if c.former_holder]
    shared = [c for c in active if c.kind == 'shared_account']
    unknown_creds = [
        c for c in active if c.default_credential_state == 'unknown'
    ]
    if default_live:
        checks.append(('credentials', 'failed'))
        reasons.append(
            f'{len(default_live)} active credential(s) still on vendor '
            'defaults'
        )
    elif former_live or shared or unknown_creds:
        checks.append(('credentials', 'limited'))
        if former_live:
            limitations.append(
                f'{len(former_live)} active credential(s) belong to '
                'former holders — revocation review outstanding'
            )
        if shared:
            limitations.append(
                f'{len(shared)} active shared account(s) — no individual '
                'accountability'
            )
        if unknown_creds:
            limitations.append(
                f'{len(unknown_creds)} active credential(s) with unknown '
                'default state'
            )
    elif credentials:
        checks.append(('credentials', 'verified'))
        evidence.extend(c.credential_id for c in credentials[:4])
    else:
        checks.append(('credentials', 'limited'))
        limitations.append('no credential records declared')
    if 'unknown' in {
        c.default_credential_state for c in credentials
        if c.state != 'active'
    }:
        pass  # non-active unknowns carry no live risk

    # --- surfaces ------------------------------------------------------
    enabled = [s for s in surfaces if s.state == 'enabled']
    exposed = [
        s for s in enabled
        if s.exposure_scope in ('wan_internet', 'vendor_cloud')
    ]
    unauth_exposed = [
        s for s in exposed
        if s.authentication_state == 'unauthenticated'
    ]
    optional_enabled = [
        s for s in enabled if s.requirement in ('optional', 'unknown')
    ]
    unknown_surface_state = [
        s for s in surfaces if s.state == 'unknown'
    ]
    if unauth_exposed:
        checks.append(('surfaces', 'failed'))
        reasons.append(
            f'{len(unauth_exposed)} unauthenticated management '
            'surface(s) reachable beyond the site'
        )
    elif optional_enabled or unknown_surface_state or not surfaces:
        checks.append(('surfaces', 'limited'))
        if optional_enabled:
            limitations.append(
                f'{len(optional_enabled)} enabled surface(s) are not a '
                'declared project requirement — risk candidates'
            )
        if unknown_surface_state:
            limitations.append(
                f'{len(unknown_surface_state)} surface(s) with unknown '
                'enable state'
            )
        if not surfaces:
            limitations.append('no management surfaces declared')
    else:
        checks.append(('surfaces', 'verified'))
        evidence.extend(s.surface_id for s in surfaces[:4])

    # --- firmware ------------------------------------------------------
    eol = [
        a for a in assets
        if a.vendor_support_status in ('eol', 'eol_announced')
    ]
    unknown_fw = [
        a for a in assets
        if a.vendor_support_status == 'unknown'
        or a.current_firmware_label is None
    ]
    if eol or unknown_fw:
        checks.append(('firmware', 'limited'))
        if eol:
            limitations.append(
                f'{len(eol)} asset(s) at/near vendor end-of-support'
            )
        if unknown_fw:
            limitations.append(
                f'{len(unknown_fw)} asset(s) without reviewed firmware/'
                'support state'
            )
    else:
        checks.append(('firmware', 'verified'))

    # --- remote access -------------------------------------------------
    remote_assets = [
        a for a in assets
        if a.management_reachability
        in ('wan_internet', 'vendor_cloud', 'mixed')
        or a.cloud_dependency in ('required', 'optional')
    ]
    authorized = [
        r for r in remote_authorizations if r.state == 'authorized'
    ]
    remote_subjects = {
        (a.subject_ref.kind, a.subject_ref.ref_id) for a in remote_assets
    }
    auth_subjects = {
        (r.subject_ref.kind, r.subject_ref.ref_id) for r in authorized
    }
    unauthorized_remote = remote_subjects - auth_subjects
    if unauthorized_remote:
        checks.append(('remote_access', 'failed'))
        reasons.append(
            f'{len(unauthorized_remote)} remote-reachable asset(s) lack '
            'a valid RemoteServiceAuthorization'
        )
    elif remote_assets:
        checks.append(('remote_access', 'verified'))
        evidence.extend(r.authorization_id for r in authorized[:4])
    else:
        checks.append(('remote_access', 'not_applicable'))

    # --- backup sensitivity --------------------------------------------
    secret_bearing = [
        a for a in assets if a.backup_sensitivity == 'secret_bearing'
    ]
    unredacted = [
        a for a in secret_bearing
        if a.backup_redaction_state != 'redacted'
    ]
    unknown_backup = [
        a for a in assets if a.backup_sensitivity == 'unknown'
    ]
    if unredacted:
        checks.append(('backup_sensitivity', 'failed'))
        reasons.append(
            f'{len(unredacted)} secret-bearing backup(s) without '
            'redaction evidence'
        )
    elif unknown_backup or (
        secret_bearing
        and any(a.backup_redaction_state == 'unknown' for a in secret_bearing)
    ):
        checks.append(('backup_sensitivity', 'limited'))
        limitations.append(
            'backup sensitivity/redaction incompletely reviewed'
        )
    else:
        checks.append(('backup_sensitivity', 'verified'))

    # --- access review ---------------------------------------------------
    reviewed_credential_ids = {
        o.credential_id for r in access_reviews for o in r.outcomes
    }
    unclosed_reviews = [r for r in access_reviews if r.open_item_refs]
    if former_live and not (
        reviewed_credential_ids & {c.credential_id for c in former_live}
    ):
        checks.append(('access_review', 'failed'))
        reasons.append(
            'former-holder credential(s) active with no access review '
            'covering them'
        )
    elif unclosed_reviews:
        checks.append(('access_review', 'limited'))
        limitations.append(
            f'{len(unclosed_reviews)} access review(s) still have open '
            'items'
        )
    elif access_reviews:
        checks.append(('access_review', 'verified'))
        evidence.extend(r.review_id for r in access_reviews[:2])
    else:
        checks.append(('access_review', 'limited'))
        limitations.append('no access review recorded')

    # --- decommission ----------------------------------------------------
    pending_revocation = [
        a for a in assets
        if a.lifecycle_state == 'removed_pending_credential_revocation'
    ]
    active_creds_on_removed = [
        c for c in credentials
        if c.state == 'active'
        and (c.subject_ref.kind, c.subject_ref.ref_id)
        in {
            (a.subject_ref.kind, a.subject_ref.ref_id)
            for a in pending_revocation
        }
    ]
    if pending_revocation:
        checks.append(('decommission', 'limited'))
        limitations.append(
            f'{len(pending_revocation)} removed asset(s) pending '
            'credential revocation'
        )
        if active_creds_on_removed:
            limitations.append(
                f'{len(active_creds_on_removed)} credential(s) still '
                'active on removed assets'
            )
    else:
        checks.append(('decommission', 'verified'))

    # An unassessed system is UNKNOWN — assets declared without any
    # security evidence is not "probably fine" (#598 goal).
    if (
        not credentials
        and not surfaces
        and not observations
        and not risks
        and not remote_authorizations
        and not access_reviews
    ):
        return _seal_review(
            document_id=document_id,
            profile_ref=profile_ref,
            state='unknown',
            checks=tuple(checks),
            reasons=tuple(
                list(reasons)
                + [
                    'no credential, surface, observation, risk, '
                    'authorization or access-review records exist — '
                    'the system is unassessed',
                ]
            ),
            limitations=tuple(limitations),
            risk_refs=(),
            evidence_refs=tuple(a.asset_id for a in assets[:4]),
            evaluated_at_utc=evaluated_at_utc,
        )

    # --- verdict ----------------------------------------------------------
    failed_checks = {c for c, r in checks if r == 'failed'}
    limited_checks = {c for c, r in checks if r == 'limited'}

    high_risk_open = [
        r for r in risks
        if r.status == 'open'
        and r.likelihood_class == 'high'
    ]
    open_risks = [r for r in risks if r.status == 'open']
    accepted_risks = [r for r in risks if r.status == 'accepted']

    if high_risk_open or {'credentials', 'remote_access',
                          'backup_sensitivity'} & failed_checks:
        state = 'high_risk_exposure'
        if high_risk_open:
            reasons.append(
                f'{len(high_risk_open)} open high-likelihood risk(s)'
            )
    elif failed_checks or open_risks:
        state = 'mitigation_required'
        if open_risks:
            reasons.append(
                f'{len(open_risks)} open risk(s) without mitigation or '
                'acceptance'
            )
    elif accepted_risks and not open_risks:
        state = 'risk_accepted'
        reasons.append(
            'all residual risk(s) are explicitly accepted with owner '
            'and review date'
        )
    elif limited_checks:
        state = 'reviewed_with_limitations'
    else:
        state = 'reviewed'

    evidence.extend(o.observation_id for o in observations[:4])
    risk_refs = tuple(r.risk_id for r in risks)

    return _seal_review(
        document_id=document_id,
        profile_ref=profile_ref,
        state=state,
        checks=tuple(checks),
        reasons=tuple(reasons),
        limitations=tuple(limitations),
        risk_refs=risk_refs,
        evidence_refs=tuple(dict.fromkeys(evidence)),
        evaluated_at_utc=evaluated_at_utc,
    )


def _seal_review(
    *,
    document_id: str,
    profile_ref: AuthorityRef | None,
    state: SecurityReviewState,
    checks: tuple[tuple[SecurityCheck, SecurityCheckResult], ...],
    reasons: tuple[str, ...],
    limitations: tuple[str, ...],
    risk_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    evaluated_at_utc: str | None,
) -> SecurityReview:
    probe = SecurityReview.model_construct(
        **canonicalize_payload(
            SecurityReview,
            dict(
                schema_version=SECURITY_AUTHORITY_SCHEMA_VERSION,
                authority_version=SECURITY_REVIEW_AUTHORITY_VERSION,
                review_id='',
                review_sha256='0' * 64,
                document_id=document_id,
                profile_ref=profile_ref,
                evaluation_version=SECURITY_EVALUATION_VERSION,
                state=state,
                checks=checks,
                reasons=reasons,
                limitations=limitations,
                risk_refs=risk_refs,
                evidence_refs=evidence_refs,
                evaluated_at_utc=evaluated_at_utc or _utc_now(),
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return SecurityReview(
        **probe.model_dump(
            mode='python', exclude={'review_id', 'review_sha256'}
        ),
        review_id=_semantic_id('secrev', sha),
        review_sha256=sha,
    )


__all__ = [
    'AccessCapability',
    'AccessReviewOutcome',
    'AccessReviewOutcomeEntry',
    'AccessReviewRecord',
    'AccessReviewTrigger',
    'AuthenticationState',
    'BackupSensitivity',
    'CredentialKind',
    'CredentialRecord',
    'CredentialScope',
    'CredentialState',
    'CredentialStorageRef',
    'DefaultCredentialState',
    'ExposureScope',
    'FunctionalAVEffect',
    'ImpactDomain',
    'LikelihoodClass',
    'MFAState',
    'ManagementReachability',
    'ManagementSurfaceDeclaration',
    'ManagementSurfaceKind',
    'RemoteAuthorizationState',
    'RemoteServiceAuthorization',
    'RemoteServiceMethod',
    'RotationState',
    'SecurityAssetDeclaration',
    'SecurityCheck',
    'SecurityCheckResult',
    'SecurityEvidenceClass',
    'SecurityLifecycleState',
    'SecurityObservation',
    'SecurityObservationKind',
    'SecurityObservationOutcome',
    'SecurityReview',
    'SecurityReviewState',
    'SecurityRiskRecord',
    'SecurityTestEvidence',
    'SecurityTestKind',
    'SessionLoggingState',
    'SurfaceRequirement',
    'SurfaceState',
    'VendorSupportStatus',
    'SECURITY_AUTHORITY_SCHEMA_VERSION',
    'SECURITY_EVALUATION_VERSION',
    'build_access_review',
    'build_credential',
    'build_management_surface',
    'build_remote_authorization',
    'build_security_asset',
    'build_security_observation',
    'build_security_risk',
    'build_security_test_evidence',
    'evaluate_security_review',
]
