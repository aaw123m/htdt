"""Project / evidence data privacy & sharing authority (#722).

HTDT holds room scans, photos, floor plans, device serials, network
topology, customer identity, service notes, measurement recordings and
proprietary design data — sensitive even when the AV network itself is
technically secure (#598 owns that side). This module pins an
artifact-level classification taxonomy, least-privilege per-class
policies, allowlist-based export manifests and retention/deletion
semantics. Unknown classification fails closed for external sharing;
deleting raw evidence truthfully downgrades reproducibility.

Basis: issue #722 scope; ISO 19650-5:2020 (security-minded information
management — architecture guidance, not a certification claim); ISO/IEC
27701:2025 (PIMS principles for controllers/processors). GDPR/CCPA
concepts inform consent/minimum-necessary semantics; jurisdiction/legal
programs stay out of scope. Cybersecurity controls remain #598,
reproducibility #610, archival/migration #718, collaboration roles #721,
licensing/provenance #599/#586/#608.
"""

from __future__ import annotations

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


DataClass = Literal[
    'public_shareable', 'internal_project', 'client_confidential',
    'personal_pii', 'security_sensitive', 'credential_or_secret',
    'proprietary_license_restricted', 'safety_engineering_restricted',
    'unknown_classification',
]

RightsClass = Literal[
    'open', 'reference_only', 'licensed_no_redistribution',
    'proprietary', 'undeclared',
]

ClassificationReviewState = Literal[
    'declared', 'reviewed', 'approved', 'contested',
]

BundleProfile = Literal[
    'safe_diagnostic_metadata_only',
    'include_device_config_with_redaction',
    'include_raw_measurement',
    'project_owner_approved_extended_bundle',
    'none',
]

RetentionClass = Literal[
    'retain_indefinitely', 'retain_until_handover', 'retain_n_days',
    'delete_after_support_case', 'user_requested_deletion',
    'legal_hold',
]

ReproducibilityState = Literal[
    'raw_available', 'raw_redacted_derivative_only',
    'raw_deleted_hash_retained', 'raw_external_reference_only',
    'reproducibility_limited_by_privacy_policy',
]

# Classes that may never leave the project via an external export.
_NON_EXPORTABLE_CLASSES = frozenset({
    'credential_or_secret',
    'unknown_classification',
})

PRIVACY_LABELS: dict[str, str] = {
    'export_denied_unclassified': '未分類のため外部出力不可',
    'export_denied_secret': '秘密情報は出力不可',
    'export_denied_license_restricted': '再配布権限なし',
    'export_denied_no_policy': '共有ポリシー未設定',
    'export_denied_not_manifested': '出力マニフェスト外',
    'export_requires_redaction': 'リダクション必須',
    'export_allowed_within_manifest': 'マニフェスト内で出力可',
    'reproducibility_intact': '再現性は保持',
    'reproducibility_limited': 'プライバシー方針により再現性限定',
}


class ProjectDataClassification(BaseModel):
    """Artifact/field-level sensitivity classification (pdc- prefix).

    Classification attaches to the exact artifact, not the whole
    project; it is separate from technical evidence quality and from
    redistribution rights. ``unknown_classification`` exists so
    unreviewed artifacts stay honest — and fail closed on sharing.
    """

    model_config = ConfigDict(frozen=True)

    classification_id: str
    classification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    artifact_ref: AuthorityRef
    data_class: DataClass
    rights_class: RightsClass = 'undeclared'
    review_state: ClassificationReviewState = 'declared'
    # Derived artifacts inherit sensitivity; declassification is an
    # explicit reviewed act, never an automatic side effect of a
    # transform. ``derived_from_refs`` keeps that lineage visible.
    derived_from_refs: tuple[AuthorityRef, ...] = ()
    rationale: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ProjectDataClassification':
        _require_refs(self.artifact_ref)
        for ref in self.derived_from_refs:
            _require_refs(ref)
        if self.data_class == 'unknown_classification' \
                and self.review_state == 'approved':
            raise ValueError(
                'unknown_classification cannot be approved')
        if self.data_class == 'credential_or_secret' \
                and not self.rationale:
            raise ValueError(
                'credential_or_secret requires a rationale '
                '(a reference/state record — plaintext secrets do not '
                'belong in project fields)')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'classification_id', 'classification_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ProjectDataClassification':
        return _seal(
            cls, payload, 'classification_id',
            'classification_sha256', 'pdc')


class SensitiveArtifactPolicy(BaseModel):
    """Least-privilege policy over one or more data classes
    (sap- prefix). View/edit/export/share/delete are separate grants —
    'project member' never implies access to every artifact."""

    model_config = ConfigDict(frozen=True)

    policy_id: str
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    target_classes: tuple[DataClass, ...]
    allow_view_roles: tuple[str, ...] = ()
    allow_edit_roles: tuple[str, ...] = ()
    allow_export_roles: tuple[str, ...] = ()
    allow_share_roles: tuple[str, ...] = ()
    allow_delete_roles: tuple[str, ...] = ()
    redaction_required_for_export: bool = False
    derived_inherits: bool = True
    min_bundle_profile: BundleProfile = 'safe_diagnostic_metadata_only'

    @model_validator(mode='after')
    def _validate(self) -> 'SensitiveArtifactPolicy':
        if not self.target_classes:
            raise ValueError('target_classes must not be empty')
        for cls_ in self.target_classes:
            if cls_ in _NON_EXPORTABLE_CLASSES and (
                    self.allow_export_roles or self.allow_share_roles):
                raise ValueError(
                    f'{cls_} may never be granted export/share roles')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'policy_id', 'policy_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SensitiveArtifactPolicy':
        return _seal(cls, payload, 'policy_id', 'policy_sha256', 'sap')


class ExportRedactionManifest(BaseModel):
    """Allowlist-based external export manifest (erm- prefix). Every
    shareable bundle is *generated from* the manifest — included
    artifacts, excluded artifacts, redaction transforms and the
    resulting package hash are all pinned, so a leak is detectable."""

    model_config = ConfigDict(frozen=True)

    manifest_id: str
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    bundle_kind: Literal[
        'client_package', 'support_bundle', 'share_link',
        'archive_export', 'other',
    ]
    included_refs: tuple[AuthorityRef, ...]
    excluded_refs: tuple[AuthorityRef, ...] = ()
    # (artifact_ref_id, transform, reason) — e.g. serial digits masked.
    redactions: tuple[tuple[str, str, str], ...] = ()
    policy_ref: AuthorityRef | None = None
    resulting_bundle_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _validate(self) -> 'ExportRedactionManifest':
        if not self.included_refs:
            raise ValueError('included_refs must not be empty')
        for ref in self.included_refs + self.excluded_refs:
            _require_refs(ref)
        included_ids = {r.ref_id for r in self.included_refs}
        excluded_ids = {r.ref_id for r in self.excluded_refs}
        overlap = included_ids & excluded_ids
        if overlap:
            raise ValueError(
                'refs cannot be both included and excluded: '
                f'{sorted(overlap)}')
        dangling = {
            entry[0] for entry in self.redactions
        } - included_ids
        if dangling:
            raise ValueError(
                'redaction entries must reference included refs: '
                f'{sorted(dangling)}')
        if self.policy_ref is not None:
            _require_refs(self.policy_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'manifest_id', 'manifest_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ExportRedactionManifest':
        return _seal(
            cls, payload, 'manifest_id', 'manifest_sha256', 'erm')


class RetentionPolicyRecord(BaseModel):
    """Retention/deletion policy for one artifact or class
    (rtn- prefix). HTDT defines semantics only — jurisdiction-specific
    periods stay project policy. Deleting canonical raw evidence
    truthfully downgrades reproducibility."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    artifact_ref: AuthorityRef
    retention_class: RetentionClass
    retention_days: int | None = None
    legal_hold_ref: AuthorityRef | None = None
    reproducibility_state: ReproducibilityState = 'raw_available'
    deleted_utc: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'RetentionPolicyRecord':
        _require_refs(self.artifact_ref)
        if self.retention_class == 'retain_n_days' \
                and (self.retention_days is None
                     or self.retention_days <= 0):
            raise ValueError(
                'retain_n_days requires a positive retention_days')
        if self.retention_class == 'legal_hold' \
                and self.legal_hold_ref is None:
            raise ValueError('legal_hold requires its hold reference')
        if self.legal_hold_ref is not None:
            _require_refs(self.legal_hold_ref)
        if self.reproducibility_state == 'raw_deleted_hash_retained' \
                and self.deleted_utc is None:
            raise ValueError(
                'raw_deleted_hash_retained records the deletion time')
        if self.reproducibility_state == 'raw_available' \
                and self.deleted_utc is not None:
            raise ValueError(
                'raw_available cannot carry a deletion timestamp')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'RetentionPolicyRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'rtn')


def evaluate_export_eligibility(
    classification: ProjectDataClassification | None,
    policy: SensitiveArtifactPolicy | None,
    manifest: ExportRedactionManifest | None,
    artifact_ref: AuthorityRef,
) -> tuple[str, str]:
    """Fail-closed external-export gate (#722).

    Unclassified or secret artifacts never leave the project; an export
    must be manifest-allowlisted, policy-governed, and redacted when the
    policy requires it. Rights restrictions are checked independently of
    the privacy class.
    """
    if classification is None \
            or classification.data_class == 'unknown_classification':
        return ('export_denied_unclassified',
                'artifact_has_no_reviewed_classification')
    if classification.data_class == 'credential_or_secret':
        return ('export_denied_secret',
                'secret_bearing_artifacts_never_export')
    if classification.rights_class == 'licensed_no_redistribution':
        return ('export_denied_license_restricted',
                'licensed_no_redistribution')
    if policy is None:
        return ('export_denied_no_policy', 'no_sensitive_artifact_policy')
    if classification.data_class not in policy.target_classes:
        return ('export_denied_no_policy',
                'policy_does_not_cover_class:'
                + classification.data_class)
    if manifest is None:
        return ('export_denied_not_manifested',
                'no_export_manifest')
    if artifact_ref.ref_id in {
            r.ref_id for r in manifest.excluded_refs}:
        return ('export_denied_not_manifested',
                'artifact_explicitly_excluded')
    if artifact_ref.ref_id not in {
            r.ref_id for r in manifest.included_refs}:
        return ('export_denied_not_manifested',
                'artifact_not_in_manifest_allowlist')
    if policy.redaction_required_for_export \
            and artifact_ref.ref_id not in {
                entry[0] for entry in manifest.redactions}:
        return ('export_requires_redaction',
                'policy_requires_redaction_transform')
    return ('export_allowed_within_manifest',
            'manifest:' + manifest.manifest_id)


def evaluate_reproducibility_claim(
    retention: RetentionPolicyRecord | None,
) -> tuple[str, str]:
    """Privacy deletions truthfully cap reproducibility (#722 §11) —
    full reproduction is never claimed once the canonical raw evidence
    is gone or external."""
    if retention is None:
        return ('reproducibility_intact', 'no_retention_policy')
    if retention.reproducibility_state == 'raw_available':
        return ('reproducibility_intact', 'raw_available')
    return ('reproducibility_limited',
            'state:' + retention.reproducibility_state)
