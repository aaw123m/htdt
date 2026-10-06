"""Evidence attestation / trusted timestamp authority (#725, REV59-DEPS).

A cryptographic hash can detect that bytes changed relative to a known
hash — it cannot by itself establish *who* attested to an artifact,
*when* the attestation existed, or whether the signing credential was
valid at that time. This module separates those claims:

- :class:`SignedManifestRecord` — the sealed canonical manifest identity:
  exactly which artifact bytes were signed (``manifest_sha256`` over the
  canonical serialization), the canonicalization procedure and version
  it depends on, the subject refs it binds (project/scene/variant/
  evidence/profile revisions), and the exact approval scope. A rendered
  PDF may be a signed artifact but is never the only machine-readable
  authority (#725 §2/§3).

- :class:`EvidenceAttestation` — the sealed attestation record binding a
  manifest to one signer under one :class:`AttestationKind`:
  hash-only, MAC-authenticated, digitally signed (with or without a
  certificate chain), trusted-timestamp-only, signed-and-timestamped,
  or an external signature reference. The taxonomy is deliberately not
  a generic ``signed = true`` flag (#725 §1); private key material is
  never persisted — only verification-side metadata (#725 §20).

- :func:`evaluate_attestation` → :class:`AttestationVerification` — the
  sealed verification verdict. Verification is itself a derived evidence
  record (#725 §10): it consumes explicit check results (manifest hash,
  signature, timestamp token, key state at signing time) under a pinned
  policy identity and reports a time-authority ladder —
  trusted timestamp > declared time > unknown (#725 §8). A hash-only
  bundle can never claim signer identity; a signature whose manifest no
  longer matches (e.g. after redaction) fails rather than silently
  inheriting coverage (#725 §12).

Composition: #610 owns evidence-bundle hashing and packaging, #721 owns
project approval semantics, #722 owns privacy/redaction policy — this
module supplies the attestation/timestamp layer they compose with.

Literature basis
----------------
- NIST FIPS 186-5 (2023) — digital signatures authenticate the signatory
  *subject to* the surrounding key/identity assurance system; HTDT never
  equates cryptographic validity with legal enforceability (#725 §19).
- RFC 3161 — the Time-Stamp Protocol: a TSA token evidences that a datum
  existed before a stated time, distinct from any local ``created_at``.
- RFC 7515 (JWS) — one possible signature serialization; the format is a
  pinned field, not an architecture mandate.
- in-toto / SLSA supply-chain attestation — an attestation binds a
  subject, a predicate and a signer identity; chains of attestations
  (an attestation over an attestation) preserve provenance (#725 §15).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


ATTESTATION_SCHEMA_VERSION = 'evidence-attestation-1'
ATTESTATION_EVALUATION_VERSION = 'attestation-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


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
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

AttestationKind = Literal[
    'hash_only',
    'mac_authenticated',
    'digitally_signed',
    'digitally_signed_with_certificate_chain',
    'trusted_timestamp_only',
    'signed_and_trusted_timestamped',
    'external_signature_reference',
    'unknown',
]
"""#725 §1 — hash-only evidence must never be described as authenticated
by a person or organization; the taxonomy keeps integrity, MAC,
signature, certificate and trusted-timestamp classes distinct."""

ApprovalScope = Literal[
    'integrity_only',
    'design_revision',
    'as_built_observation',
    'measurement_campaign',
    'verification_result',
    'equipment_substitution',
    'commissioning_handoff',
    'exception_acceptance',
    'other_declared',
]
"""#725 §7 — what the signer actually attested to. A signature on the
overall project never implies technical approval of every contained
artifact; the scope binds exactly one claim class."""

SignerType = Literal[
    'human',
    'organizational_service',
    'automated_pipeline',
    'measurement_appliance',
    'external_provider',
    'unknown',
]
"""#725 §16 — an automated pipeline signature proves artifact
pipeline identity/integrity, never human engineering approval."""

SignatureFormat = Literal[
    'jws_compact',
    'jws_json',
    'detached_cms',
    'detached_cbor',
    'raw_algorithm_signature',
    'external_reference',
    'other_declared',
]

TimeAuthority = Literal[
    'trusted_timestamp',
    'declared_time',
    'unknown_time',
]
"""#725 §8 — the time-authority ladder. A verified RFC3161-style token
outranks any declared/local timestamp; no token means the attestation
can claim at most the signer's own declared time."""

AttestationState = Literal[
    'integrity_confirmed',
    'timestamp_confirmed',
    'attested_verified',
    'attested_verified_declared_time',
    'attested_verified_historical',
    'attested_unprovened',
    'unverifiable',
    'verification_failed',
]
"""Fail-closed verification states. ``attested_verified_historical``
records that the key was valid at a trusted signing time even though it
has since expired or been revoked (#725 §9); ``attested_unprovened``
means a signature is present but the required checks were not run —
never silently PASS."""

CheckResult = Literal['not_checked', 'verified', 'failed']

KeyStateAtTime = Literal['valid', 'expired', 'revoked', 'unknown']

HashAlgorithmPolicy = Literal[
    'currently_acceptable',
    'legacy_verify_only',
    'deprecated',
    'unsupported',
]
"""#725 §4 — algorithm agility: policy comes from the deployment's
security profile; ``unsupported`` digest identities fail closed."""


class CanonicalizationSpec(BaseModel):
    """How the signed bytes were produced (#725 §3).

    Signature validity depends on exact bytes: serialization format and
    version, canonicalization procedure and version, excluded/non-signed
    fields and content encoding are all pinned. Re-serializing a signed
    object under a different procedure is a different manifest.
    """

    model_config = ConfigDict(frozen=True)

    serialization_format: str = Field(min_length=1)
    canonicalization_id: str = Field(min_length=1)
    canonicalization_version: str = Field(min_length=1)
    excluded_fields: tuple[str, ...] = ()
    content_encoding: str | None = None


class SignedManifestRecord(BaseModel):
    """The sealed canonical manifest identity (#725 §2/§3)."""

    model_config = ConfigDict(frozen=True)

    manifest_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    manifest_label: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    """SHA-256 of the canonical manifest bytes — the exact signed
    object, never a rendered-UI proxy."""
    canonicalization: CanonicalizationSpec
    subject_refs: tuple[AuthorityRef, ...] = ()
    approval_scope: ApprovalScope
    referenced_artifacts: tuple[str, ...] = ()
    """Content digests of referenced artifacts the manifest covers —
    large raw files are referenced by digest, never embedded (#725 §13)."""
    schema_version: str = Field(
        default=ATTESTATION_SCHEMA_VERSION, min_length=1
    )
    created_at_utc: str = Field(min_length=1)
    manifest_record_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'manifest_label': self.manifest_label,
            'manifest_sha256': self.manifest_sha256,
            'canonicalization': self.canonicalization.model_dump(
                mode='json'
            ),
            'subject_refs': [
                r.model_dump(mode='json') for r in self.subject_refs
            ],
            'approval_scope': self.approval_scope,
            'referenced_artifacts': list(self.referenced_artifacts),
            'schema_version': self.schema_version,
            'created_at_utc': self.created_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SignedManifestRecord':
        _require_iso8601(self.created_at_utc, 'manifest created_at_utc')
        for ref in self.subject_refs:
            _require_ref_sha(ref, 'manifest subject_ref')
        expected = _hash(self.identity_payload())
        if self.manifest_record_sha256 != expected:
            raise ValueError('signed manifest record hash mismatch')
        if self.manifest_id != _semantic_id('sigman', expected):
            raise ValueError(
                'signed manifest id does not match its hash'
            )
        return self


def build_signed_manifest(
    *,
    document_id: str,
    manifest_label: str,
    manifest_sha256: str,
    canonicalization: CanonicalizationSpec,
    approval_scope: ApprovalScope,
    subject_refs: Sequence[AuthorityRef] = (),
    referenced_artifacts: Sequence[str] = (),
    created_at_utc: str | None = None,
) -> SignedManifestRecord:
    """Seal one canonical signed-manifest identity."""
    return _seal(
        SignedManifestRecord,
        {
            'document_id': document_id,
            'manifest_label': manifest_label,
            'manifest_sha256': manifest_sha256,
            'canonicalization': canonicalization.model_dump(mode='json'),
            'subject_refs': [
                r.model_dump(mode='json') for r in subject_refs
            ],
            'approval_scope': approval_scope,
            'referenced_artifacts': list(referenced_artifacts),
            'created_at_utc': created_at_utc or _utc_now(),
        },
        'manifest_id',
        'manifest_record_sha256',
        'sigman',
    )


def manifest_binding(record: SignedManifestRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='signed_manifest_record',
        ref_id=record.manifest_id,
        ref_sha256=record.manifest_record_sha256,
    )


class SignatureDescriptor(BaseModel):
    """Verification-side metadata for one signature (#725 §5/§6/§20).

    Carries algorithm, format, key identity and the hash of the
    signature bytes — never the private key and never the raw signature
    bytes as authority. ``signer_label`` is a display identity; the
    cryptographic identity is ``key_id``.
    """

    model_config = ConfigDict(frozen=True)

    algorithm: str = Field(min_length=1)
    signature_format: SignatureFormat
    key_id: str = Field(min_length=1)
    signer_type: SignerType
    signer_label: str | None = None
    certificate_chain_ref: AuthorityRef | None = None
    signature_sha256: str = Field(pattern=_SHA256_PATTERN)
    """Hash of the signature bytes — proves which signature was applied
    without storing key material."""
    creation_time_claim_utc: str | None = None
    """The signer's own claimed signing time — declared time, not
    trusted time (#725 §8)."""

    @model_validator(mode='after')
    def _check(self) -> 'SignatureDescriptor':
        _require_ref_sha(
            self.certificate_chain_ref, 'certificate_chain_ref'
        )
        if self.creation_time_claim_utc is not None:
            _require_iso8601(
                self.creation_time_claim_utc,
                'signature creation_time_claim_utc',
            )
        return self


class TrustedTimestampPin(BaseModel):
    """An RFC3161-style trusted timestamp token pin (#725 §8).

    The token evidences that the manifest imprint existed before the
    stated time; it does not make every timestamp inside the payload
    independently accurate.
    """

    model_config = ConfigDict(frozen=True)

    tsa_identity: str = Field(min_length=1)
    token_sha256: str = Field(pattern=_SHA256_PATTERN)
    imprint_algorithm: str = Field(min_length=1)
    claimed_time_utc: str = Field(min_length=1)
    token_source_ref: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'TrustedTimestampPin':
        _require_iso8601(
            self.claimed_time_utc, 'timestamp claimed_time_utc'
        )
        return self


class EvidenceAttestation(BaseModel):
    """One sealed attestation: manifest + signer + optional trusted
    timestamp (#725 §1/§15).

    Multiple attestations over one manifest are separate records — each
    signer/scope/chain gets its own sealed identity; actors are never
    merged into a shared signing claim.
    """

    model_config = ConfigDict(frozen=True)

    attestation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    manifest_ref: AuthorityRef
    kind: AttestationKind
    signature: SignatureDescriptor | None = None
    trusted_timestamp: TrustedTimestampPin | None = None
    attests_attestation_ref: AuthorityRef | None = None
    """Optional attestation-of-attestation chaining (#725 §15) — an
    attestation whose subject is a prior attestation."""
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=ATTESTATION_SCHEMA_VERSION, min_length=1
    )
    attestation_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'manifest_ref': self.manifest_ref.model_dump(mode='json'),
            'kind': self.kind,
            'signature': (
                self.signature.model_dump(mode='json')
                if self.signature is not None
                else None
            ),
            'trusted_timestamp': (
                self.trusted_timestamp.model_dump(mode='json')
                if self.trusted_timestamp is not None
                else None
            ),
            'attests_attestation_ref': (
                self.attests_attestation_ref.model_dump(mode='json')
                if self.attests_attestation_ref is not None
                else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'EvidenceAttestation':
        _require_iso8601(
            self.declared_at_utc, 'attestation declared_at_utc'
        )
        _require_ref_sha(self.manifest_ref, 'manifest_ref')
        _require_ref_sha(
            self.attests_attestation_ref, 'attests_attestation_ref'
        )
        has_sig = self.signature is not None
        has_ts = self.trusted_timestamp is not None
        if self.kind in (
            'digitally_signed',
            'digitally_signed_with_certificate_chain',
            'mac_authenticated',
        ) and not has_sig:
            raise ValueError(
                f'attestation kind {self.kind} requires a signature '
                'descriptor'
            )
        if self.kind == 'digitally_signed_with_certificate_chain' and (
            self.signature is not None
            and self.signature.certificate_chain_ref is None
        ):
            raise ValueError(
                'certificate-chain attestation requires a '
                'certificate_chain_ref'
            )
        if self.kind == 'signed_and_trusted_timestamped' and not (
            has_sig and has_ts
        ):
            raise ValueError(
                'signed_and_trusted_timestamped requires signature and '
                'trusted timestamp'
            )
        if self.kind == 'trusted_timestamp_only' and not has_ts:
            raise ValueError(
                'trusted_timestamp_only requires a timestamp pin'
            )
        if self.kind in ('hash_only', 'trusted_timestamp_only') and has_sig:
            raise ValueError(
                f'attestation kind {self.kind} cannot carry a signature'
            )
        expected = _hash(self.identity_payload())
        if self.attestation_sha256 != expected:
            raise ValueError('evidence attestation hash mismatch')
        if self.attestation_id != _semantic_id('attest', expected):
            raise ValueError(
                'evidence attestation id does not match its hash'
            )
        return self


def build_attestation(
    *,
    document_id: str,
    manifest_ref: AuthorityRef,
    kind: AttestationKind,
    signature: SignatureDescriptor | None = None,
    trusted_timestamp: TrustedTimestampPin | None = None,
    attests_attestation_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> EvidenceAttestation:
    """Seal one evidence attestation."""
    return _seal(
        EvidenceAttestation,
        {
            'document_id': document_id,
            'manifest_ref': manifest_ref.model_dump(mode='json'),
            'kind': kind,
            'signature': (
                signature.model_dump(mode='json')
                if signature is not None
                else None
            ),
            'trusted_timestamp': (
                trusted_timestamp.model_dump(mode='json')
                if trusted_timestamp is not None
                else None
            ),
            'attests_attestation_ref': (
                attests_attestation_ref.model_dump(mode='json')
                if attests_attestation_ref is not None
                else None
            ),
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'attestation_id',
        'attestation_sha256',
        'attest',
    )


def attestation_binding(
    attestation: EvidenceAttestation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='evidence_attestation',
        ref_id=attestation.attestation_id,
        ref_sha256=attestation.attestation_sha256,
    )


# ----------------------------------------------------------------------
# Verification

class VerificationInputs(BaseModel):
    """The declared check evidence one verification pass ran (#725 §10).

    HTDT's authority evaluates *declared verification evidence* — the
    cryptographic primitive results are supplied by the verifier that ran
    them, with its implementation and policy identities pinned. A check
    that was not run is ``not_checked`` and caps the verdict; it is never
    silently assumed.
    """

    model_config = ConfigDict(frozen=True)

    manifest_hash_match: bool | None = None
    signature_check: CheckResult = 'not_checked'
    timestamp_check: CheckResult = 'not_checked'
    """``not_checked`` also covers "no token present"."""
    key_state_at_signing_time: KeyStateAtTime = 'unknown'
    revocation_evidence_available: bool = False
    hash_algorithm_policy: HashAlgorithmPolicy = 'currently_acceptable'
    verifier_implementation: str | None = None
    verification_policy_id: str = Field(min_length=1)
    verification_policy_version: str = Field(min_length=1)


class AttestationVerification(BaseModel):
    """The sealed verification verdict — a derived evidence record, not
    a mutation of the signed artifact (#725 §10)."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    attestation_ref: AuthorityRef
    manifest_ref: AuthorityRef
    state: AttestationState
    time_authority: TimeAuthority
    verification_policy_id: str = Field(min_length=1)
    verification_policy_version: str = Field(min_length=1)
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=ATTESTATION_EVALUATION_VERSION, min_length=1
    )
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'attestation_ref': self.attestation_ref.model_dump(mode='json'),
            'manifest_ref': self.manifest_ref.model_dump(mode='json'),
            'state': self.state,
            'time_authority': self.time_authority,
            'verification_policy_id': self.verification_policy_id,
            'verification_policy_version': (
                self.verification_policy_version
            ),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'AttestationVerification':
        _require_iso8601(
            self.evaluated_at_utc, 'verification evaluated_at_utc'
        )
        _require_ref_sha(self.attestation_ref, 'attestation_ref')
        _require_ref_sha(self.manifest_ref, 'manifest_ref')
        expected = _hash(self.identity_payload())
        if self.verification_sha256 != expected:
            raise ValueError('attestation verification hash mismatch')
        if self.verification_id != _semantic_id('attver', expected):
            raise ValueError(
                'attestation verification id does not match its hash'
            )
        return self


def evaluate_attestation(
    document_id: str,
    manifest: SignedManifestRecord,
    attestation: EvidenceAttestation,
    inputs: VerificationInputs,
    *,
    evaluated_at_utc: str | None = None,
) -> AttestationVerification:
    """Fail-closed attestation verification (#725).

    Rules, in order:

    - An attestation bound to a different manifest id, or a manifest
      whose content hash no longer matches, fails — a redacted or
      re-exported artifact never inherits the original signature
      (#725 §12).
    - An ``unsupported`` hash-algorithm policy fails closed (#725 §4).
    - ``hash_only`` confirms integrity only — no signer claim, ever.
    - ``trusted_timestamp_only`` confirms existence-before-time only.
    - Signed kinds: a failed signature check or a failed required
      timestamp check fails; an unrun signature check is
      ``attested_unprovened``; a verified signature over a verified
      trusted timestamp is ``attested_verified``, over declared time
      only ``attested_verified_declared_time``; a key that has since
      expired/been revoked but was valid at a trusted signing time is
      ``attested_verified_historical`` under an explicit policy
      (#725 §9).
    - ``external_signature_reference``/``unknown`` kinds are
      ``unverifiable`` — imported attestations do not silently pass.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []
    state: AttestationState
    time_authority: TimeAuthority = 'unknown_time'

    manifest_ref = manifest_binding(manifest)
    attestation_ref = attestation_binding(attestation)
    mref = attestation.manifest_ref
    if (
        mref.ref_id != manifest.manifest_id
        or mref.ref_sha256 != manifest.manifest_record_sha256
    ):
        # The attestation pins a different manifest identity — e.g. a
        # redacted derivative presenting the original's signature.
        return _seal(
            AttestationVerification,
            {
                'document_id': document_id,
                'attestation_ref': attestation_ref.model_dump(mode='json'),
                'manifest_ref': manifest_ref.model_dump(mode='json'),
                'state': 'verification_failed',
                'time_authority': 'unknown_time',
                'verification_policy_id': inputs.verification_policy_id,
                'verification_policy_version': (
                    inputs.verification_policy_version
                ),
                'reasons': [
                    'attestation does not bind this manifest identity — '
                    'a modified or substituted artifact cannot inherit '
                    'the signature'
                ],
                'limitations': [],
                'evaluated_at_utc': evaluated_at_utc,
            },
            'verification_id',
            'verification_sha256',
            'attver',
        )

    if inputs.manifest_hash_match is False:
        return _seal(
            AttestationVerification,
            {
                'document_id': document_id,
                'attestation_ref': attestation_ref.model_dump(mode='json'),
                'manifest_ref': manifest_ref.model_dump(mode='json'),
                'state': 'verification_failed',
                'time_authority': 'unknown_time',
                'verification_policy_id': inputs.verification_policy_id,
                'verification_policy_version': (
                    inputs.verification_policy_version
                ),
                'reasons': [
                    'manifest bytes no longer match the signed digest — '
                    'the artifact was modified after signing'
                ],
                'limitations': [],
                'evaluated_at_utc': evaluated_at_utc,
            },
            'verification_id',
            'verification_sha256',
            'attver',
        )

    if inputs.hash_algorithm_policy == 'unsupported':
        reasons.append('digest algorithm is unsupported under policy')
        return _seal(
            AttestationVerification,
            {
                'document_id': document_id,
                'attestation_ref': attestation_ref.model_dump(mode='json'),
                'manifest_ref': manifest_ref.model_dump(mode='json'),
                'state': 'verification_failed',
                'time_authority': 'unknown_time',
                'verification_policy_id': inputs.verification_policy_id,
                'verification_policy_version': (
                    inputs.verification_policy_version
                ),
                'reasons': reasons,
                'limitations': limitations,
                'evaluated_at_utc': evaluated_at_utc,
            },
            'verification_id',
            'verification_sha256',
            'attver',
        )

    kind = attestation.kind
    ts_check = inputs.timestamp_check
    if kind == 'trusted_timestamp_only':
        if ts_check == 'verified' and inputs.manifest_hash_match:
            state = 'timestamp_confirmed'
            time_authority = 'trusted_timestamp'
            reasons.append(
                'trusted timestamp token verified — proof of existence '
                'before the stated time, no signer claim'
            )
        elif ts_check == 'failed':
            state = 'verification_failed'
            reasons.append('trusted timestamp token failed verification')
        else:
            state = 'attested_unprovened'
            reasons.append(
                'timestamp token was not verified — existence-before-'
                'time is not proven'
            )
    elif kind == 'hash_only':
        if inputs.manifest_hash_match:
            state = 'integrity_confirmed'
            reasons.append(
                'content hash matches — integrity evidence only, no '
                'signer identity is claimed'
            )
        else:
            state = 'attested_unprovened'
            reasons.append(
                'manifest hash was not verified — integrity is not '
                'proven'
            )
    elif kind == 'mac_authenticated':
        if inputs.signature_check == 'verified' and (
            inputs.manifest_hash_match
        ):
            state = 'integrity_confirmed'
            limitations.append(
                'MAC authenticates under a shared key — no third-party '
                'signer attribution'
            )
        elif inputs.signature_check == 'failed':
            state = 'verification_failed'
            reasons.append('MAC verification failed')
        else:
            state = 'attested_unprovened'
            reasons.append('MAC was not verified')
    elif kind in (
        'digitally_signed',
        'digitally_signed_with_certificate_chain',
        'signed_and_trusted_timestamped',
    ):
        sig = inputs.signature_check
        if sig == 'failed':
            state = 'verification_failed'
            reasons.append('signature verification failed')
        elif sig == 'not_checked':
            state = 'attested_unprovened'
            reasons.append(
                'signature is present but was not verified'
            )
        elif not inputs.manifest_hash_match:
            state = 'attested_unprovened'
            reasons.append(
                'signature verified but manifest hash was not checked '
                '— coverage is not proven'
            )
        elif (
            kind == 'signed_and_trusted_timestamped'
            and ts_check == 'failed'
        ):
            state = 'verification_failed'
            reasons.append(
                'required trusted timestamp failed verification'
            )
        elif (
            kind == 'signed_and_trusted_timestamped'
            and ts_check == 'not_checked'
        ):
            state = 'attested_unprovened'
            reasons.append(
                'declared signed-and-timestamped, but the token was '
                'not verified'
            )
        else:
            # A declared check result only counts when the attestation
            # actually carries a token — a verified nothing is not a
            # trusted timestamp.
            ts_verified = (
                ts_check == 'verified'
                and attestation.trusted_timestamp is not None
            )
            key_state = inputs.key_state_at_signing_time
            if ts_verified:
                time_authority = 'trusted_timestamp'
            elif attestation.signature is not None and (
                attestation.signature.creation_time_claim_utc is not None
            ):
                time_authority = 'declared_time'
            if key_state in ('expired', 'revoked'):
                if ts_verified:
                    state = 'attested_verified_historical'
                    limitations.append(
                        f'signing key is {key_state} today — valid at '
                        'the trusted signing time under policy '
                        'retention'
                    )
                else:
                    state = 'attested_unprovened'
                    reasons.append(
                        f'signing key is {key_state} and no trusted '
                        'timestamp proves the signature predates it'
                    )
            elif key_state == 'unknown':
                if ts_verified:
                    state = 'attested_verified_declared_time'
                    limitations.append(
                        'key state at signing time is unknown — '
                        'trusted timestamp proves existence only'
                    )
                else:
                    state = 'attested_unprovened'
                    reasons.append(
                        'key state at signing time is unknown and no '
                        'trusted timestamp anchors it'
                    )
            elif ts_verified:
                state = 'attested_verified'
                reasons.append(
                    'signature verified over a trusted timestamp'
                )
            else:
                state = 'attested_verified_declared_time'
                limitations.append(
                    'no trusted timestamp — only the signer\'s declared '
                    'time is evidenced'
                )
    else:  # external_signature_reference / unknown
        state = 'unverifiable'
        reasons.append(
            f'attestation kind {kind} cannot be verified by this '
            'authority — imported claims do not silently pass'
        )

    return _seal(
        AttestationVerification,
        {
            'document_id': document_id,
            'attestation_ref': attestation_ref.model_dump(mode='json'),
            'manifest_ref': manifest_ref.model_dump(mode='json'),
            'state': state,
            'time_authority': time_authority,
            'verification_policy_id': inputs.verification_policy_id,
            'verification_policy_version': (
                inputs.verification_policy_version
            ),
            'reasons': reasons,
            'limitations': limitations,
            'evaluated_at_utc': evaluated_at_utc,
        },
        'verification_id',
        'verification_sha256',
        'attver',
    )


def verification_binding(
    verification: AttestationVerification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='attestation_verification',
        ref_id=verification.verification_id,
        ref_sha256=verification.verification_sha256,
    )


__all__ = [
    'ATTESTATION_SCHEMA_VERSION',
    'ATTESTATION_EVALUATION_VERSION',
    'AttestationKind',
    'ApprovalScope',
    'SignerType',
    'SignatureFormat',
    'TimeAuthority',
    'AttestationState',
    'CheckResult',
    'KeyStateAtTime',
    'HashAlgorithmPolicy',
    'CanonicalizationSpec',
    'SignedManifestRecord',
    'build_signed_manifest',
    'manifest_binding',
    'SignatureDescriptor',
    'TrustedTimestampPin',
    'EvidenceAttestation',
    'build_attestation',
    'attestation_binding',
    'VerificationInputs',
    'AttestationVerification',
    'evaluate_attestation',
    'verification_binding',
]
