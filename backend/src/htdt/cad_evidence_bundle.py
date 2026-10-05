"""Reproducible evidence bundle / integrity manifest authority (#610,
REV57-METRO).

Individually hashed artifacts are not enough: HTDT must prove *which
exact set* of inputs, mappings, software versions and derivations
produced a commissioning result or report. This module provides the
sealed package-level authority:

- :class:`CadEvidenceArtifactEntry` — one manifest entry: logical role,
  package path or record URI, media/schema type, digest, inclusion
  class (embedded vs. protected/external/reference-only), artifact
  class (raw vs. derived vs. decision vs. presentation) and
  rights/sensitivity state.
- :class:`CadDerivationEdge` — one provenance-DAG edge: operation,
  software/algorithm identity, parameters, ordered inputs, output —
  every derived result/chart/verdict traces to exact inputs.
- :class:`CadEvidenceBundle` — the finalized package identity: purpose,
  producer software/version, status, optional parent (supersede) link,
  declared reproducibility level and the manifest root hash covering
  every artifact/edge entry.
- :class:`CadBundleAttestation` — an optional signed attestation over
  the pinned manifest root: signer identity/key reference, algorithm,
  role and trust context. An attestation attests identity, integrity
  and actor under policy — never scientific truth.
- :class:`CadBundleValidationVerdict` — the validator's sealed verdict:
  completeness/integrity state kept strictly separate from scientific
  PASS/FAIL.

External-specification basis (borrowed patterns, domain-native schema):

- RFC 8493 BagIt 1.0 — payload/tag manifests, completeness vs. digest
  validity, SHA-256 minimum for new packages;
- RO-Crate 1.3 (2026-06-22, current long-term release) — packaging
  data + software + equipment + workflows + provenance;
- W3C PROV — entity/activity/agent provenance model;
- in-toto v1.0 — material→product attestation per workflow step.

Honesty rules baked in:

- A finalized bundle is immutable — corrections produce a new bundle
  linked by ``supersedes_bundle_ref``; nothing is silently repaired.
- ``sha256``/``sha512`` digests are required for newly manifested
  embedded payloads; historical weaker digests may persist as source
  metadata but never as the only integrity mechanism of a new entry.
- A mutable external URL is never counted as immutable evidence.
- Presentation/report artifacts never substitute for required raw
  evidence — a screenshot cannot complete a package whose profile
  requires the underlying numeric/raw record.
- ``checksum-valid`` never implies ``scientifically sufficient``;
  integrity and content correctness stay separate verdicts.
- Omitted/redacted/externalized items are explicit and degrade
  completeness/reproducibility — a stripped package never reports
  ``complete``.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


EVIDENCE_BUNDLE_SCHEMA_VERSION = 'metro-evb-1'
BUNDLE_VALIDATION_VERSION = 'metro-evb-val-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'
_SHA512_PATTERN = r'^[0-9a-f]{128}$'


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
# Taxonomies (#610)
# ---------------------------------------------------------------------------

BundlePurpose = Literal[
    'design_review',
    'simulation_validation',
    'commissioning',
    'rp22_rp32_verification',
    'service_baseline',
    'fault_diagnostic',
    'before_after_comparison',
    'archival_handoff',
    'other',
]

BundleStatus = Literal['draft', 'finalized', 'superseded', 'withdrawn']

ArtifactInclusion = Literal[
    'embedded',
    'project_local_reference',
    'external_immutable_reference',
    'external_mutable_url',
    'licensed_not_embedded',
    'sensitive_not_embedded',
    'unavailable_reference',
]

#: Inclusions whose bytes live inside the package or in verifiable
#: project/immutable storage — digest verification applies.
_EMBEDDED_LIKE: frozenset[ArtifactInclusion] = frozenset(
    {'embedded', 'project_local_reference', 'external_immutable_reference'}
)

#: Inclusions that are declarations only — nothing verifiable travels.
_DECLARED_ONLY: frozenset[ArtifactInclusion] = frozenset(
    {
        'external_mutable_url',
        'licensed_not_embedded',
        'sensitive_not_embedded',
        'unavailable_reference',
    }
)

ArtifactClass = Literal[
    'raw_acquired',
    'source_external',
    'derived_numeric',
    'derived_media',
    'decision_verdict',
    'presentation_report',
]

#: Classes that can satisfy a required evidence role — presentation and
#: decision artifacts may accompany raw evidence, never replace it.
_EVIDENCE_CLASSES: frozenset[ArtifactClass] = frozenset(
    {'raw_acquired', 'source_external', 'derived_numeric'}
)

RightsSensitivity = Literal[
    'open',
    'protected',
    'licensed',
    'sensitive',
    'unknown',
]

DigestAlgorithm = Literal['sha256', 'sha512']

ReproducibilityLevel = Literal[
    'bit_reproducible',
    'deterministic_with_same_runtime',
    'numerically_reproducible_with_tolerance',
    'recomputable_from_retained_inputs',
    'auditable_but_not_recomputable',
    'presentation_only',
]

CompletenessProfile = Literal[
    'commissioning_minimum',
    'rp32_profile',
    'simulation_validation',
    'service_handoff',
    'legal_client_deliverable_custom',
    'none',
]

AttestationRole = Literal[
    'measurement_operator',
    'system_designer',
    'commissioning_engineer',
    'client_acceptance',
    'qa_reviewer',
]

BundleValidationState = Literal[
    'complete_valid',
    'complete_but_external_dependencies',
    'incomplete',
    'integrity_failure',
    'profile_mismatch',
    'unresolved_reference',
]

BundleCheck = Literal[
    'payload_presence',
    'digest_match',
    'manifest_root',
    'required_roles',
    'external_dependencies',
    'derivation_integrity',
    'reproducibility_vs_content',
    'status_finalized',
]

BundleCheckResult = Literal['verified', 'limited', 'failed', 'not_applicable']

#: Roles every completeness profile requires — the profile selects a
#: vocabulary of logical roles, never a byte layout.
_PROFILE_REQUIRED_ROLES: Mapping[CompletenessProfile, frozenset[str]] = {
    'commissioning_minimum': frozenset(
        {
            'measurement_raw',
            'measurement_derived',
            'device_configuration',
            'standards_profile',
            'report',
        }
    ),
    'rp32_profile': frozenset(
        {
            'measurement_raw',
            'measurement_derived',
            'standards_profile',
            'verification_result',
            'report',
        }
    ),
    'simulation_validation': frozenset(
        {
            'scene_revision',
            'solver_settings',
            'simulation_result',
            'validation_result',
            'report',
        }
    ),
    'service_handoff': frozenset(
        {
            'measurement_raw',
            'device_configuration',
            'report',
        }
    ),
    'legal_client_deliverable_custom': frozenset({'report'}),
    'none': frozenset(),
}


def required_roles_for_profile(
    profile: CompletenessProfile,
) -> frozenset[str]:
    return _PROFILE_REQUIRED_ROLES[profile]


# ---------------------------------------------------------------------------
# Artifact entries
# ---------------------------------------------------------------------------


class CadEvidenceArtifactEntry(BaseModel):
    """One manifest entry in an evidence bundle.

    ``logical_role`` is the package vocabulary slot the artifact fills
    (e.g. ``measurement_raw``, ``standards_profile``, ``report``);
    completeness profiles require roles, never filenames.
    ``package_path`` is a relative POSIX path inside the package for
    embedded payloads; ``record_uri`` names database/external records.

    The digest pins the exact payload bytes (or the canonical record
    hash for database-backed items). ``unavailable_reference`` entries
    carry no verifiable content — they exist so omissions are explicit.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    logical_role: str = Field(min_length=1)
    artifact_class: ArtifactClass
    inclusion: ArtifactInclusion
    package_path: str | None = None
    record_uri: str | None = None
    media_type: str | None = None
    schema_type: str | None = None
    byte_length: int | None = None
    digest_algorithm: DigestAlgorithm | None = None
    digest: str | None = None
    source_authority: AuthorityRef | None = None
    required: bool = True
    rights_sensitivity: RightsSensitivity = 'unknown'
    captured_at_utc: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_entry(self) -> 'CadEvidenceArtifactEntry':
        _require_iso8601(
            self.declared_at_utc, 'artifact declared_at_utc'
        )
        if self.captured_at_utc is not None:
            _require_iso8601(
                self.captured_at_utc, 'artifact captured_at_utc'
            )
        if self.package_path is not None:
            path = self.package_path
            if (
                path.startswith('/')
                or '\\' in path
                or ':' in path
                or '..' in path.split('/')
                or not path
            ):
                raise ValueError(
                    'package_path must be a relative POSIX path'
                )
        if self.byte_length is not None and self.byte_length < 0:
            raise ValueError('byte_length must be non-negative')
        if self.digest is not None:
            if self.digest_algorithm is None:
                raise ValueError('a digest requires its algorithm')
            expected_len = {'sha256': 64, 'sha512': 128}[
                self.digest_algorithm
            ]
            if (
                len(self.digest) != expected_len
                or any(c not in '0123456789abcdef' for c in self.digest)
            ):
                raise ValueError(
                    f'digest must be a {self.digest_algorithm} hex '
                    'value'
                )
        if self.inclusion in _EMBEDDED_LIKE:
            if self.digest is None:
                raise ValueError(
                    f'{self.inclusion} artifacts require a content '
                    'digest — unverifiable payloads are declared as '
                    'external/unavailable instead'
                )
            if self.inclusion == 'embedded' and (
                self.package_path is None
            ):
                raise ValueError(
                    'embedded artifacts require a package_path'
                )
        else:
            if self.package_path is not None:
                raise ValueError(
                    f'{self.inclusion} artifacts carry no embedded '
                    'package_path'
                )
            if self.inclusion == 'external_mutable_url':
                if self.record_uri is None:
                    raise ValueError(
                        'a mutable external reference must name its '
                        'URL/URI'
                    )
                if self.captured_at_utc is None:
                    raise ValueError(
                        'a mutable external reference must record when '
                        'it was captured'
                    )
            if (
                self.inclusion == 'unavailable_reference'
                and self.digest is not None
            ):
                raise ValueError(
                    'unavailable references carry no digest — record '
                    'the omission honestly'
                )
        if self.source_authority is not None and (
            self.source_authority.ref_sha256 is None
        ):
            raise ValueError(
                'source authority refs must pin the authority sha256'
            )
        expected = _hash(self.identity_payload())
        if self.artifact_sha256 != expected:
            raise ValueError('artifact entry hash mismatch')
        if self.artifact_id != _semantic_id('evart', expected):
            raise ValueError('artifact id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'bundle_id': self.bundle_id,
            'logical_role': self.logical_role,
            'artifact_class': self.artifact_class,
            'inclusion': self.inclusion,
            'package_path': self.package_path,
            'record_uri': self.record_uri,
            'media_type': self.media_type,
            'schema_type': self.schema_type,
            'byte_length': self.byte_length,
            'digest_algorithm': self.digest_algorithm,
            'digest': self.digest,
            'source_authority': (
                self.source_authority.model_dump(mode='json')
                if self.source_authority is not None
                else None
            ),
            'required': self.required,
            'rights_sensitivity': self.rights_sensitivity,
            'captured_at_utc': self.captured_at_utc,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @property
    def is_embedded_like(self) -> bool:
        return self.inclusion in _EMBEDDED_LIKE

    @property
    def satisfies_evidence_role(self) -> bool:
        """Whether this entry can fill a required evidence role — raw
        or derived-numeric content only; presentation/decision
        artifacts never substitute for underlying evidence."""
        return self.artifact_class in _EVIDENCE_CLASSES


# ---------------------------------------------------------------------------
# Derivation edges
# ---------------------------------------------------------------------------


class CadDerivationEdge(BaseModel):
    """One provenance-DAG edge: inputs + operation → output artifact.

    ``input_artifact_ids`` names the exact manifest entries consumed
    (ordered where order matters); ``output_artifact_id`` the entry the
    run produced. ``operation``, ``software_identity`` and
    ``parameters_json`` pin *how* — a chart must be traceable to its
    exact numeric source.
    """

    model_config = ConfigDict(frozen=True)

    edge_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    software_identity: str = Field(min_length=1)
    algorithm_profile_version: str | None = None
    parameters_json: str = '{}'
    input_artifact_ids: tuple[str, ...] = Field(min_length=1)
    output_artifact_id: str = Field(min_length=1)
    operator: str | None = None
    performed_at_utc: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    edge_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_edge(self) -> 'CadDerivationEdge':
        _require_iso8601(self.declared_at_utc, 'edge declared_at_utc')
        if self.performed_at_utc is not None:
            _require_iso8601(
                self.performed_at_utc, 'edge performed_at_utc'
            )
        if any(not item for item in self.input_artifact_ids):
            raise ValueError('edge inputs must be non-empty ids')
        if self.output_artifact_id in self.input_artifact_ids:
            raise ValueError(
                'an edge output cannot be its own input'
            )
        expected = _hash(self.identity_payload())
        if self.edge_sha256 != expected:
            raise ValueError('derivation edge hash mismatch')
        if self.edge_id != _semantic_id('evedge', expected):
            raise ValueError('edge id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'bundle_id': self.bundle_id,
            'operation': self.operation,
            'software_identity': self.software_identity,
            'algorithm_profile_version': self.algorithm_profile_version,
            'parameters_json': self.parameters_json,
            'input_artifact_ids': list(self.input_artifact_ids),
            'output_artifact_id': self.output_artifact_id,
            'operator': self.operator,
            'performed_at_utc': self.performed_at_utc,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }


# ---------------------------------------------------------------------------
# Bundle manifest
# ---------------------------------------------------------------------------


def compute_manifest_root(
    artifact_shas: tuple[str, ...] | list[str],
    edge_shas: tuple[str, ...] | list[str],
) -> str:
    """Tamper-evident manifest root over sorted member digests."""
    return _hash(
        {
            'artifacts': sorted(artifact_shas),
            'edges': sorted(edge_shas),
        }
    )


class CadEvidenceBundle(BaseModel):
    """The sealed evidence-package identity.

    ``manifest_root_sha256`` covers every artifact entry and derivation
    edge: change any member and the root no longer matches — tampering
    is detectable without trusting a rendered report. ``status`` is the
    lifecycle state; ``finalized`` requires the manifest root plus
    ``finalized_at_utc``. A corrected package is a *new* bundle whose
    ``supersedes_bundle_ref`` points at the old one.
    """

    model_config = ConfigDict(frozen=True)

    bundle_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    purpose: BundlePurpose
    status: BundleStatus = 'draft'
    scene_revision_id: str | None = None
    system_variant_id: str | None = None
    completeness_profile: CompletenessProfile = 'none'
    reproducibility_level: ReproducibilityLevel = (
        'auditable_but_not_recomputable'
    )
    producer_software: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    producer_build: str | None = None
    runtime_environment: str | None = None
    manifest_root_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    supersedes_bundle_ref: AuthorityRef | None = None
    authority_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    finalized_at_utc: str | None = None
    provenance_json: str = '{}'
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_bundle(self) -> 'CadEvidenceBundle':
        _require_iso8601(self.created_at_utc, 'bundle created_at_utc')
        if self.finalized_at_utc is not None:
            _require_iso8601(
                self.finalized_at_utc, 'bundle finalized_at_utc'
            )
        if self.status == 'finalized':
            if self.manifest_root_sha256 is None:
                raise ValueError(
                    'a finalized bundle requires its manifest root hash'
                )
            if self.finalized_at_utc is None:
                raise ValueError(
                    'a finalized bundle requires finalized_at_utc'
                )
        if self.status == 'draft' and (
            self.manifest_root_sha256 is not None
        ):
            raise ValueError(
                'a draft bundle has no manifest root — finalize it '
                'before pinning the root'
            )
        if self.supersedes_bundle_ref is not None and (
            self.supersedes_bundle_ref.ref_sha256 is None
        ):
            raise ValueError(
                'a supersede link must pin the parent bundle sha256'
            )
        expected = _hash(self.identity_payload())
        if self.bundle_sha256 != expected:
            raise ValueError('bundle hash mismatch')
        if self.bundle_id != _semantic_id('evbun', expected):
            raise ValueError('bundle id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'purpose': self.purpose,
            'status': self.status,
            'scene_revision_id': self.scene_revision_id,
            'system_variant_id': self.system_variant_id,
            'completeness_profile': self.completeness_profile,
            'reproducibility_level': self.reproducibility_level,
            'producer_software': self.producer_software,
            'producer_version': self.producer_version,
            'producer_build': self.producer_build,
            'runtime_environment': self.runtime_environment,
            'manifest_root_sha256': self.manifest_root_sha256,
            'supersedes_bundle_ref': (
                self.supersedes_bundle_ref.model_dump(mode='json')
                if self.supersedes_bundle_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'created_at_utc': self.created_at_utc,
            'finalized_at_utc': self.finalized_at_utc,
            'provenance_json': self.provenance_json,
        }


def bundle_binding(bundle: CadEvidenceBundle) -> AuthorityRef:
    return AuthorityRef(
        kind='evidence_bundle',
        ref_id=bundle.bundle_id,
        ref_sha256=bundle.bundle_sha256,
    )


# ---------------------------------------------------------------------------
# Attestations
# ---------------------------------------------------------------------------


class CadBundleAttestation(BaseModel):
    """An optional signed attestation over a pinned bundle.

    The signature blob is recorded opaquely — this record proves that a
    signer attested *this* manifest root at a time under a policy; it
    never verifies a key itself, and it never attests scientific truth.
    """

    model_config = ConfigDict(frozen=True)

    attestation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    bundle_ref: AuthorityRef
    signer_identity: str = Field(min_length=1)
    signer_key_ref: str | None = None
    signature_algorithm: str = Field(min_length=1)
    signature_value: str = Field(min_length=1)
    role: AttestationRole
    trust_context: str | None = None
    authority_version: str = Field(min_length=1)
    signed_at_utc: str = Field(min_length=1)
    attestation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_attestation(self) -> 'CadBundleAttestation':
        _require_iso8601(self.signed_at_utc, 'attestation signed_at_utc')
        if self.bundle_ref.ref_sha256 is None:
            raise ValueError(
                'attestations must pin the bundle root hash — a '
                'signature over an unnamed package attests nothing'
            )
        expected = _hash(self.identity_payload())
        if self.attestation_sha256 != expected:
            raise ValueError('attestation hash mismatch')
        if self.attestation_id != _semantic_id('evatt', expected):
            raise ValueError('attestation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'bundle_ref': self.bundle_ref.model_dump(mode='json'),
            'signer_identity': self.signer_identity,
            'signer_key_ref': self.signer_key_ref,
            'signature_algorithm': self.signature_algorithm,
            'signature_value': self.signature_value,
            'role': self.role,
            'trust_context': self.trust_context,
            'authority_version': self.authority_version,
            'signed_at_utc': self.signed_at_utc,
        }


# ---------------------------------------------------------------------------
# Validation verdicts
# ---------------------------------------------------------------------------


class CadBundleValidationVerdict(BaseModel):
    """Sealed validator output for one bundle + profile pair.

    ``state`` is package integrity/completeness — never the scientific
    verdict of the contents. ``missing_roles``, ``external_dependencies``
    and ``failed_digests`` make the shortfall enumerable.
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    bundle_ref: AuthorityRef
    profile: CompletenessProfile
    state: BundleValidationState
    checks: tuple[tuple[BundleCheck, BundleCheckResult], ...]
    missing_roles: tuple[str, ...] = ()
    external_dependencies: tuple[str, ...] = ()
    failed_digests: tuple[str, ...] = ()
    unresolved_references: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    validation_version: str = Field(min_length=1)
    validated_at_utc: str = Field(min_length=1)
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_verdict(self) -> 'CadBundleValidationVerdict':
        _require_iso8601(
            self.validated_at_utc, 'verdict validated_at_utc'
        )
        if self.bundle_ref.ref_sha256 is None:
            raise ValueError(
                'verdicts must pin the bundle sha256'
            )
        if len({check for check, _ in self.checks}) != len(self.checks):
            raise ValueError('duplicate validation check entries')
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('validation verdict hash mismatch')
        if self.verdict_id != _semantic_id('evval', expected):
            raise ValueError('verdict id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'bundle_ref': self.bundle_ref.model_dump(mode='json'),
            'profile': self.profile,
            'state': self.state,
            'checks': [list(item) for item in self.checks],
            'missing_roles': list(self.missing_roles),
            'external_dependencies': list(self.external_dependencies),
            'failed_digests': list(self.failed_digests),
            'unresolved_references': list(self.unresolved_references),
            'reasons': list(self.reasons),
            'validation_version': self.validation_version,
            'validated_at_utc': self.validated_at_utc,
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_artifact_entry(
    *,
    document_id: str,
    bundle_id: str,
    logical_role: str,
    artifact_class: ArtifactClass,
    inclusion: ArtifactInclusion,
    package_path: str | None = None,
    record_uri: str | None = None,
    media_type: str | None = None,
    schema_type: str | None = None,
    byte_length: int | None = None,
    digest_algorithm: DigestAlgorithm | None = None,
    digest: str | None = None,
    source_authority: AuthorityRef | None = None,
    required: bool = True,
    rights_sensitivity: RightsSensitivity = 'unknown',
    captured_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadEvidenceArtifactEntry:
    """Seal one manifest entry."""
    payload = dict(
        document_id=document_id,
        bundle_id=bundle_id,
        logical_role=logical_role,
        artifact_class=artifact_class,
        inclusion=inclusion,
        package_path=package_path,
        record_uri=record_uri,
        media_type=media_type,
        schema_type=schema_type,
        byte_length=byte_length,
        digest_algorithm=digest_algorithm,
        digest=digest,
        source_authority=source_authority,
        required=required,
        rights_sensitivity=rights_sensitivity,
        captured_at_utc=captured_at_utc,
        authority_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadEvidenceArtifactEntry, payload,
        'artifact_id', 'artifact_sha256', 'evart',
    )


def build_derivation_edge(
    *,
    document_id: str,
    bundle_id: str,
    operation: str,
    software_identity: str,
    input_artifact_ids: tuple[str, ...],
    output_artifact_id: str,
    algorithm_profile_version: str | None = None,
    parameters_json: str = '{}',
    operator: str | None = None,
    performed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadDerivationEdge:
    """Seal one provenance-DAG edge."""
    payload = dict(
        document_id=document_id,
        bundle_id=bundle_id,
        operation=operation,
        software_identity=software_identity,
        algorithm_profile_version=algorithm_profile_version,
        parameters_json=parameters_json,
        input_artifact_ids=tuple(input_artifact_ids),
        output_artifact_id=output_artifact_id,
        operator=operator,
        performed_at_utc=performed_at_utc,
        authority_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadDerivationEdge, payload,
        'edge_id', 'edge_sha256', 'evedge',
    )


def build_evidence_bundle(
    *,
    document_id: str,
    purpose: BundlePurpose,
    producer_software: str,
    producer_version: str,
    status: BundleStatus = 'draft',
    scene_revision_id: str | None = None,
    system_variant_id: str | None = None,
    completeness_profile: CompletenessProfile = 'none',
    reproducibility_level: ReproducibilityLevel = (
        'auditable_but_not_recomputable'
    ),
    producer_build: str | None = None,
    runtime_environment: str | None = None,
    manifest_root_sha256: str | None = None,
    supersedes_bundle_ref: AuthorityRef | CadEvidenceBundle | None = None,
    created_at_utc: str | None = None,
    finalized_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadEvidenceBundle:
    """Seal the bundle identity (manifest root covers its members)."""
    if isinstance(supersedes_bundle_ref, CadEvidenceBundle):
        supersedes_bundle_ref = bundle_binding(supersedes_bundle_ref)
    payload = dict(
        document_id=document_id,
        purpose=purpose,
        status=status,
        scene_revision_id=scene_revision_id,
        system_variant_id=system_variant_id,
        completeness_profile=completeness_profile,
        reproducibility_level=reproducibility_level,
        producer_software=producer_software,
        producer_version=producer_version,
        producer_build=producer_build,
        runtime_environment=runtime_environment,
        manifest_root_sha256=manifest_root_sha256,
        supersedes_bundle_ref=supersedes_bundle_ref,
        authority_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        created_at_utc=created_at_utc or _utc_now(),
        finalized_at_utc=finalized_at_utc,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadEvidenceBundle, payload,
        'bundle_id', 'bundle_sha256', 'evbun',
    )


def finalize_evidence_bundle(
    *,
    document_id: str,
    draft: CadEvidenceBundle,
    entries: tuple[CadEvidenceArtifactEntry, ...] | list[CadEvidenceArtifactEntry],
    edges: tuple[CadDerivationEdge, ...] | list[CadDerivationEdge] = (),
    finalized_at_utc: str | None = None,
) -> CadEvidenceBundle:
    """Seal a draft into its finalized, tamper-evident record.

    Entries/edges bind to the draft's bundle id (they exist before the
    root hash is computable — the manifest root covers their sealed
    digests, and the finalized record supersedes the draft, so
    validation still resolves membership. The finalized bundle is a
    NEW sealed record, never an in-place edit.
    """
    if draft.status != 'draft':
        raise ValueError(
            'finalize_evidence_bundle takes a draft bundle'
        )
    members = [
        entry for entry in entries if entry.bundle_id == draft.bundle_id
    ]
    member_edges = [
        edge for edge in edges if edge.bundle_id == draft.bundle_id
    ]
    return build_evidence_bundle(
        document_id=document_id,
        purpose=draft.purpose,
        producer_software=draft.producer_software,
        producer_version=draft.producer_version,
        status='finalized',
        scene_revision_id=draft.scene_revision_id,
        system_variant_id=draft.system_variant_id,
        completeness_profile=draft.completeness_profile,
        reproducibility_level=draft.reproducibility_level,
        producer_build=draft.producer_build,
        runtime_environment=draft.runtime_environment,
        manifest_root_sha256=compute_manifest_root(
            [entry.artifact_sha256 for entry in members],
            [edge.edge_sha256 for edge in member_edges],
        ),
        supersedes_bundle_ref=bundle_binding(draft),
        created_at_utc=draft.created_at_utc,
        finalized_at_utc=finalized_at_utc,
        provenance_json=draft.provenance_json,
    )


def build_attestation(
    *,
    document_id: str,
    bundle_ref: AuthorityRef | CadEvidenceBundle,
    signer_identity: str,
    signature_algorithm: str,
    signature_value: str,
    role: AttestationRole,
    signer_key_ref: str | None = None,
    trust_context: str | None = None,
    signed_at_utc: str | None = None,
) -> CadBundleAttestation:
    """Seal an attestation over a pinned bundle root."""
    if isinstance(bundle_ref, CadEvidenceBundle):
        bundle_ref = bundle_binding(bundle_ref)
    payload = dict(
        document_id=document_id,
        bundle_ref=bundle_ref,
        signer_identity=signer_identity,
        signer_key_ref=signer_key_ref,
        signature_algorithm=signature_algorithm,
        signature_value=signature_value,
        role=role,
        trust_context=trust_context,
        authority_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        signed_at_utc=signed_at_utc or _utc_now(),
    )
    return _seal_model(
        CadBundleAttestation, payload,
        'attestation_id', 'attestation_sha256', 'evatt',
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_evidence_bundle(
    *,
    document_id: str,
    bundle: CadEvidenceBundle,
    entries: tuple[CadEvidenceArtifactEntry, ...] | list[CadEvidenceArtifactEntry],
    edges: tuple[CadDerivationEdge, ...] | list[CadDerivationEdge] = (),
    digest_resolver: Callable[[CadEvidenceArtifactEntry], str | None] | None = None,
    profile: CompletenessProfile | None = None,
    validated_at_utc: str | None = None,
) -> CadBundleValidationVerdict:
    """Fail-closed package validation.

    ``digest_resolver`` returns the *current* content digest for an
    embedded-like entry (the bytes now, not the manifest's claim) or
    ``None`` when the payload cannot be resolved. Integrity proves
    package consistency only — never content correctness.
    """
    validated_at_utc = validated_at_utc or _utc_now()
    _require_iso8601(validated_at_utc, 'validated_at_utc')
    profile = profile or bundle.completeness_profile
    checks: dict[BundleCheck, BundleCheckResult] = {}
    reasons: list[str] = []

    # Members bind to the bundle they were assembled under. A finalized
    # package seals over entries created while it was a draft, so
    # membership follows the bundle id plus its immediate supersedes
    # predecessor (the sha-pinned draft this record corrects/seals).
    member_ids = {bundle.bundle_id}
    if bundle.supersedes_bundle_ref is not None:
        member_ids.add(bundle.supersedes_bundle_ref.ref_id)
    bundle_entries = [
        entry for entry in entries if entry.bundle_id in member_ids
    ]
    bundle_edges = [
        edge for edge in edges if edge.bundle_id in member_ids
    ]
    entry_ids = {entry.artifact_id for entry in bundle_entries}

    # --- status ---------------------------------------------------------
    if bundle.status == 'finalized':
        checks['status_finalized'] = 'verified'
    else:
        checks['status_finalized'] = 'failed'
        reasons.append(
            f'bundle is {bundle.status} — only finalized packages '
            'validate as sealed evidence'
        )

    # --- manifest root --------------------------------------------------
    if bundle.manifest_root_sha256 is None:
        checks['manifest_root'] = 'failed'
        reasons.append('no manifest root hash — package identity open')
    else:
        recomputed = compute_manifest_root(
            [entry.artifact_sha256 for entry in bundle_entries],
            [edge.edge_sha256 for edge in bundle_edges],
        )
        if recomputed == bundle.manifest_root_sha256:
            checks['manifest_root'] = 'verified'
        else:
            checks['manifest_root'] = 'failed'
            reasons.append(
                'manifest root mismatch — member set changed since '
                'finalization'
            )

    # --- payload presence + digests --------------------------------------
    failed_digests: list[str] = []
    unresolved: list[str] = []
    external: list[str] = []
    presence_ok = True
    digest_ok = True
    for entry in bundle_entries:
        if entry.inclusion in _DECLARED_ONLY:
            external.append(entry.artifact_id)
            continue
        if digest_resolver is None:
            unresolved.append(entry.artifact_id)
            continue
        current = digest_resolver(entry)
        if current is None:
            unresolved.append(entry.artifact_id)
            presence_ok = False
        elif current != entry.digest:
            failed_digests.append(entry.artifact_id)
            digest_ok = False
            reasons.append(
                f'digest mismatch on {entry.logical_role} — payload '
                'changed since manifest'
            )
    if unresolved:
        checks['payload_presence'] = 'failed'
        if digest_resolver is None:
            reasons.append(
                'no digest resolver — payload presence unverifiable'
            )
        else:
            reasons.append(
                f'{len(unresolved)} payload(s) could not be resolved'
            )
    else:
        checks['payload_presence'] = 'verified'
    checks['digest_match'] = 'verified' if digest_ok else 'failed'

    # --- external dependencies -------------------------------------------
    if external:
        checks['external_dependencies'] = 'limited'
        reasons.append(
            f'{len(external)} artifact(s) are external/reference-only '
            '— reproducibility bounded by them'
        )
    else:
        checks['external_dependencies'] = 'verified'

    # --- required roles ---------------------------------------------------
    missing: list[str] = []
    if profile != 'none':
        required_roles = required_roles_for_profile(profile)
        # A role is satisfied by any evidence-class entry that claims
        # it — presentation/decision artifacts accompany evidence but
        # never fill the slot.
        covered = {
            entry.logical_role
            for entry in bundle_entries
            if entry.satisfies_evidence_role
        }
        # The report role is filled by the report itself.
        covered |= {
            entry.logical_role
            for entry in bundle_entries
            if entry.logical_role == 'report'
            and entry.artifact_class == 'presentation_report'
        }
        # Standards/verification results may live in verdict class.
        covered |= {
            entry.logical_role
            for entry in bundle_entries
            if entry.artifact_class == 'decision_verdict'
            and entry.logical_role
            in {'standards_profile', 'verification_result',
                'validation_result'}
        }
        missing = sorted(required_roles - covered)
        if missing:
            checks['required_roles'] = 'failed'
            reasons.append(
                'missing required roles: ' + ', '.join(missing)
            )
        else:
            checks['required_roles'] = 'verified'
    else:
        checks['required_roles'] = 'not_applicable'

    # --- derivation integrity ---------------------------------------------
    if bundle_edges:
        broken = False
        for edge in bundle_edges:
            if edge.output_artifact_id not in entry_ids:
                broken = True
            for input_id in edge.input_artifact_ids:
                if input_id not in entry_ids:
                    broken = True
        checks['derivation_integrity'] = (
            'failed' if broken else 'verified'
        )
        if broken:
            reasons.append(
                'derivation edges reference artifacts outside the '
                'manifest — provenance chain incomplete'
            )
    else:
        checks['derivation_integrity'] = 'not_applicable'

    # --- reproducibility vs content ---------------------------------------
    if bundle.reproducibility_level == 'presentation_only':
        checks['reproducibility_vs_content'] = 'verified'
    elif bundle.reproducibility_level in (
        'bit_reproducible',
        'deterministic_with_same_runtime',
        'numerically_reproducible_with_tolerance',
        'recomputable_from_retained_inputs',
    ):
        # A recomputability claim needs the derivation chain present.
        if not bundle_edges:
            checks['reproducibility_vs_content'] = 'limited'
            reasons.append(
                f'{bundle.reproducibility_level} claimed without a '
                'recorded derivation chain'
            )
        else:
            checks['reproducibility_vs_content'] = 'verified'
    else:
        checks['reproducibility_vs_content'] = 'verified'

    # --- state ------------------------------------------------------------
    if (
        not digest_ok
        or checks['manifest_root'] == 'failed'
        or checks['derivation_integrity'] == 'failed'
        or (not presence_ok and digest_resolver is not None)
    ):
        state: BundleValidationState = 'integrity_failure'
    elif unresolved:
        state = 'unresolved_reference'
    elif checks['required_roles'] == 'failed':
        if bundle.completeness_profile != profile:
            state = 'profile_mismatch'
        else:
            state = 'incomplete'
    elif bundle.status != 'finalized':
        state = 'incomplete'
    elif external:
        state = 'complete_but_external_dependencies'
    else:
        state = 'complete_valid'

    payload = dict(
        document_id=document_id,
        bundle_ref=bundle_binding(bundle),
        profile=profile,
        state=state,
        checks=tuple(checks.items()),
        missing_roles=tuple(missing),
        external_dependencies=tuple(sorted(external)),
        failed_digests=tuple(sorted(failed_digests)),
        unresolved_references=tuple(sorted(unresolved)),
        reasons=tuple(reasons),
        validation_version=BUNDLE_VALIDATION_VERSION,
        validated_at_utc=validated_at_utc,
    )
    return _seal_model(
        CadBundleValidationVerdict, payload,
        'verdict_id', 'verdict_sha256', 'evval',
    )


__all__ = [
    'ArtifactClass',
    'ArtifactInclusion',
    'AttestationRole',
    'BUNDLE_VALIDATION_VERSION',
    'BundleCheck',
    'BundleCheckResult',
    'BundlePurpose',
    'BundleStatus',
    'BundleValidationState',
    'CadBundleAttestation',
    'CadBundleValidationVerdict',
    'CadDerivationEdge',
    'CadEvidenceArtifactEntry',
    'CadEvidenceBundle',
    'CompletenessProfile',
    'DigestAlgorithm',
    'EVIDENCE_BUNDLE_SCHEMA_VERSION',
    'ReproducibilityLevel',
    'RightsSensitivity',
    'build_artifact_entry',
    'build_attestation',
    'build_derivation_edge',
    'build_evidence_bundle',
    'bundle_binding',
    'compute_manifest_root',
    'finalize_evidence_bundle',
    'required_roles_for_profile',
    'validate_evidence_bundle',
]
