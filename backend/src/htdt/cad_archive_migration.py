"""Project archival / schema-migration authority (#718, REV59-DEPS).

Schema migrations must not silently destroy the ability to re-read —
or the meaning of — earlier digital-twin snapshots. This module treats
archival and migration as authority records:

- :class:`ArchiveSnapshot` — the sealed archival unit: schema version at
  capture, content hash, the export/write procedure and version that
  produced it, an optional prior-archive lineage link, the declared
  readback procedure pin (serializer/reader + versions) and a snapshot
  semantic checksum over the canonical identity payload (#718 §1/§10).

- :class:`ArchiveVerification` — a sealed re-read verdict on one
  snapshot: the checker independently re-read the archive and compared
  content hash and semantic checksum; a check that was not run is
  ``not_checked`` and never contributes a PASS (#718 §11).

- :class:`MigrationRecord` — the sealed migration identity: source
  archive pin, target archive pin, from/to schema versions, migration
  procedure and version, declared field-level change intents, and the
  verification status (#718 §3/§13).

- :class:`MigrationVerification` — the sealed post-migration verdict:
  the target archive is re-readable (declared procedure re-reads it and
  the content hash matches), semantic checksum matches (or diffs were
  explicitly declared and accepted as intentional), required pins still
  resolve, and the declared preservation scope is complete under a
  pinned comparison policy (#718 §4/§13). An unverified migration can
  never claim preservation.

Composition: #710 owns generic project export, #720 owns report package
output — this module supplies the archivability/migration layer they
compose with; #564 glossary semantics are addressed by explicit
transformation declarations inside the migration record (#718 §15).

Literature basis
----------------
- ISO 14721 (OAIS reference model) — preservation description
  information: provenance, fixity and representation information are
  first-class metadata carried with the object, not implied by bytes.
- OAIS PDI fixity + representation information — a stored object needs
  both "these are the same bytes" (fixity) and "this is how to read
  them" (representation); HTDT pins the declared readback procedure.
- Library/archive migration vs emulation literature (e.g. Bearman;
  CAMiLEON) — migration changes the carrier under verification;
  semantic preservation, not byte identity, is the claim.

The authority uses declared verification evidence: HTDT does not embed a
schema-migration engine; independent checkers supply results under
pinned policies, and unproven steps fail closed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


ARCHIVE_SCHEMA_VERSION = 'archive-migration-1'
ARCHIVE_VERIFICATION_VERSION = 'archive-verify-1'
MIGRATION_VERIFICATION_VERSION = 'migration-verify-1'

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

PreservationScope = Literal[
    'full_semantics_and_provenance',
    'evidence_provenance_only',
    'measurement_data_only',
    'project_structure_only',
    'other_declared',
]
"""#718 §4 — the archive must declare what it is supposed to preserve:
a full-semantics-and-provenance snapshot differs from a raw measurement
dump; they never silently claim each other."""

ArchiveVerificationCheck = Literal[
    'container_legible',
    'content_hash_match',
    'semantic_checksum_match',
    'readback_procedure_rerun',
    'provenance_complete',
    'schema_version_legible',
]
"""The re-read checks an archive verification may report."""

CheckOutcome = Literal['pass', 'fail', 'not_checked', 'not_applicable']
"""``not_checked`` contributes no PASS — a check that was not run never
silently verifies (#718 §11)."""

MigrationVerificationCheck = Literal[
    'target_readable',
    'content_hash_match',
    'semantic_checksum_match',
    'semantic_diff_declared',
    'semantic_diff_accepted',
    'referenced_pins_resolve',
    'preservation_scope_complete',
    'declared_transformations_applied',
]
"""#718 §13 — the fixed post-migration check list."""

ArchiveVerificationStatus = Literal[
    'verified_legible',
    'partially_verified',
    'verification_failed',
    'unverified',
]
"""``unverified`` = declared legibility only, no check ran."""

MigrationStatus = Literal[
    'declared',
    'verified_equivalent',
    'verified_with_declared_differences',
    'verification_failed',
    'unverified',
]
"""#718 §2/§13 — a migration never derives authority from 'it ran';
equivalent vs declared-differences vs failed vs unverified are distinct
claims."""

MigrationKind = Literal[
    'schema_upgrade',
    'format_transform',
    'storage_transform',
    'other_declared',
]
"""#718 §2 — schema upgrade, format transform and storage transform are
distinct migrations; they must never be silently conflated."""


class ProcedurePin(BaseModel):
    """A pinned procedure: what code produced / reads this object.

    ``procedure_id`` names the reader/writer; ``procedure_version`` is
    the exact code version; ``config`` pins any options that affect
    output (#718 §10 pins the declared readback procedure).
    """

    model_config = ConfigDict(frozen=True)

    procedure_id: str = Field(min_length=1)
    procedure_version: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class ArchiveSnapshot(BaseModel):
    """One sealed project-archive unit (#718 §1).

    Always contains fixity (content hash) + representation information
    (schema version, declared readback procedure) + the semantic
    checksum over the snapshot's canonical identity payload. An optional
    ``prior_archive_ref`` links successive archives of the same
    project lineage.
    """

    model_config = ConfigDict(frozen=True)

    archive_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    archive_label: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    """The schema version the snapshot was written under — needed to
    interpret the content correctly (#718 §10)."""
    content_hash: str = Field(pattern=_SHA256_PATTERN)
    snapshot_semantic_checksum: str = Field(pattern=_SHA256_PATTERN)
    """Semantic checksum over the snapshot's canonical content —
    distinguishes 'bytes differ' from 'meaning differs' (#718 §13)."""
    preservation_scope: PreservationScope
    write_procedure: ProcedurePin
    declared_readback_procedure: ProcedurePin
    """The procedure declared capable of re-reading this archive —
    the OAIS representation-information pin."""
    prior_archive_ref: AuthorityRef | None = None
    captured_at_utc: str = Field(min_length=1)
    archive_schema_version: str = Field(
        default=ARCHIVE_SCHEMA_VERSION, min_length=1
    )
    archive_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'archive_label': self.archive_label,
            'schema_version': self.schema_version,
            'content_hash': self.content_hash,
            'snapshot_semantic_checksum': self.snapshot_semantic_checksum,
            'preservation_scope': self.preservation_scope,
            'write_procedure': self.write_procedure.model_dump(mode='json'),
            'declared_readback_procedure': (
                self.declared_readback_procedure.model_dump(mode='json')
            ),
            'prior_archive_ref': (
                self.prior_archive_ref.model_dump(mode='json')
                if self.prior_archive_ref is not None
                else None
            ),
            'captured_at_utc': self.captured_at_utc,
            'archive_schema_version': self.archive_schema_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ArchiveSnapshot':
        _require_iso8601(self.captured_at_utc, 'archive captured_at_utc')
        _require_ref_sha(self.prior_archive_ref, 'prior_archive_ref')
        expected = _hash(self.identity_payload())
        if self.archive_sha256 != expected:
            raise ValueError('archive snapshot hash mismatch')
        if self.archive_id != _semantic_id('arc', expected):
            raise ValueError(
                'archive snapshot id does not match its hash'
            )
        return self


def build_archive_snapshot(
    *,
    document_id: str,
    archive_label: str,
    schema_version: str,
    content_hash: str,
    snapshot_semantic_checksum: str,
    preservation_scope: PreservationScope,
    write_procedure: ProcedurePin,
    declared_readback_procedure: ProcedurePin,
    prior_archive_ref: AuthorityRef | None = None,
    captured_at_utc: str | None = None,
) -> ArchiveSnapshot:
    """Seal one archive snapshot identity."""
    return _seal(
        ArchiveSnapshot,
        {
            'document_id': document_id,
            'archive_label': archive_label,
            'schema_version': schema_version,
            'content_hash': content_hash,
            'snapshot_semantic_checksum': snapshot_semantic_checksum,
            'preservation_scope': preservation_scope,
            'write_procedure': write_procedure.model_dump(mode='json'),
            'declared_readback_procedure': (
                declared_readback_procedure.model_dump(mode='json')
            ),
            'prior_archive_ref': (
                prior_archive_ref.model_dump(mode='json')
                if prior_archive_ref is not None
                else None
            ),
            'captured_at_utc': captured_at_utc or _utc_now(),
        },
        'archive_id',
        'archive_sha256',
        'arc',
    )


def archive_binding(archive: ArchiveSnapshot) -> AuthorityRef:
    return AuthorityRef(
        kind='archive_snapshot',
        ref_id=archive.archive_id,
        ref_sha256=archive.archive_sha256,
    )


class ArchiveCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: ArchiveVerificationCheck
    outcome: CheckOutcome
    detail: str | None = None


class ArchiveVerification(BaseModel):
    """Sealed re-read verdict on one archive snapshot (#718 §11)."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    archive_ref: AuthorityRef
    checks: tuple[ArchiveCheckResult, ...] = ()
    status: ArchiveVerificationStatus
    verification_policy_id: str = Field(min_length=1)
    verification_policy_version: str = Field(min_length=1)
    verified_at_utc: str = Field(min_length=1)
    verification_version: str = Field(
        default=ARCHIVE_VERIFICATION_VERSION, min_length=1
    )
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'archive_ref': self.archive_ref.model_dump(mode='json'),
            'checks': [c.model_dump(mode='json') for c in self.checks],
            'status': self.status,
            'verification_policy_id': self.verification_policy_id,
            'verification_policy_version': self.verification_policy_version,
            'verified_at_utc': self.verified_at_utc,
            'verification_version': self.verification_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ArchiveVerification':
        _require_iso8601(
            self.verified_at_utc, 'archive verification verified_at_utc'
        )
        _require_ref_sha(self.archive_ref, 'archive_ref')
        expected = _hash(self.identity_payload())
        if self.verification_sha256 != expected:
            raise ValueError('archive verification hash mismatch')
        if self.verification_id != _semantic_id('arcver', expected):
            raise ValueError(
                'archive verification id does not match its hash'
            )
        return self


def evaluate_archive(
    document_id: str,
    archive: ArchiveSnapshot,
    checks: Sequence[ArchiveCheckResult],
    *,
    verification_policy_id: str,
    verification_policy_version: str,
    verified_at_utc: str | None = None,
) -> ArchiveVerification:
    """Fail-closed archive re-read (#718 §11).

    Any ``fail`` → ``verification_failed``. Otherwise ``verified_legible``
    only when the core legibility checks all passed; any
    ``not_checked`` on a required check downgrades to
    ``partially_verified``; an empty check list is ``unverified`` —
    declared legibility, nothing more.
    """

    verified_at_utc = verified_at_utc or _utc_now()
    _require_iso8601(verified_at_utc, 'verified_at_utc')

    required: tuple[ArchiveVerificationCheck, ...] = (
        'container_legible',
        'content_hash_match',
        'semantic_checksum_match',
        'schema_version_legible',
    )
    by_check = {c.check: c.outcome for c in checks}
    if not checks:
        status: ArchiveVerificationStatus = 'unverified'
    elif any(o == 'fail' for o in by_check.values()):
        status = 'verification_failed'
    elif all(by_check.get(c) == 'pass' for c in required):
        status = 'verified_legible'
    else:
        status = 'partially_verified'

    return _seal(
        ArchiveVerification,
        {
            'document_id': document_id,
            'archive_ref': archive_binding(archive).model_dump(mode='json'),
            'checks': [c.model_dump(mode='json') for c in checks],
            'status': status,
            'verification_policy_id': verification_policy_id,
            'verification_policy_version': verification_policy_version,
            'verified_at_utc': verified_at_utc,
        },
        'verification_id',
        'verification_sha256',
        'arcver',
    )


class FieldChangeIntent(BaseModel):
    """One declared field-level change intent in a migration (#718 §13).

    A schema field renamed, re-typed or dropped must be declared —
    undeclared semantic drift is caught at verification, not absorbed.
    """

    model_config = ConfigDict(frozen=True)

    field_path: str = Field(min_length=1)
    change_kind: Literal[
        'renamed', 'retyped', 'dropped', 'added', 'rescaled',
        'other_declared',
    ]
    justification: str | None = None


class MigrationRecord(BaseModel):
    """One sealed migration identity (#718 §3/§13).

    Pins the source archive, the target archive produced, from/to
    schema versions, the migration procedure + version, the declared
    field-level change intents and the comparison policy — a source and
    a target byte-diff alone cannot distinguish 'renamed field' from
    'lost data'.
    """

    model_config = ConfigDict(frozen=True)

    migration_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: MigrationKind
    source_archive_ref: AuthorityRef
    target_archive_ref: AuthorityRef
    from_schema_version: str = Field(min_length=1)
    to_schema_version: str = Field(min_length=1)
    migration_procedure: ProcedurePin
    comparison_policy_id: str = Field(min_length=1)
    comparison_policy_version: str = Field(min_length=1)
    declared_field_changes: tuple[FieldChangeIntent, ...] = ()
    migration_status_at_write: MigrationStatus
    """The claimed status at write time — 'declared' until verification
    lands; never upgraded silently."""
    migrated_at_utc: str = Field(min_length=1)
    migration_schema_version: str = Field(
        default=ARCHIVE_SCHEMA_VERSION, min_length=1
    )
    migration_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'kind': self.kind,
            'source_archive_ref': self.source_archive_ref.model_dump(
                mode='json'
            ),
            'target_archive_ref': self.target_archive_ref.model_dump(
                mode='json'
            ),
            'from_schema_version': self.from_schema_version,
            'to_schema_version': self.to_schema_version,
            'migration_procedure': self.migration_procedure.model_dump(
                mode='json'
            ),
            'comparison_policy_id': self.comparison_policy_id,
            'comparison_policy_version': self.comparison_policy_version,
            'declared_field_changes': [
                c.model_dump(mode='json')
                for c in self.declared_field_changes
            ],
            'migration_status_at_write': self.migration_status_at_write,
            'migrated_at_utc': self.migrated_at_utc,
            'migration_schema_version': self.migration_schema_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'MigrationRecord':
        _require_iso8601(
            self.migrated_at_utc, 'migration migrated_at_utc'
        )
        _require_ref_sha(self.source_archive_ref, 'source_archive_ref')
        _require_ref_sha(self.target_archive_ref, 'target_archive_ref')
        expected = _hash(self.identity_payload())
        if self.migration_sha256 != expected:
            raise ValueError('migration record hash mismatch')
        if self.migration_id != _semantic_id('mig', expected):
            raise ValueError(
                'migration record id does not match its hash'
            )
        return self


def build_migration_record(
    *,
    document_id: str,
    kind: MigrationKind,
    source_archive_ref: AuthorityRef,
    target_archive_ref: AuthorityRef,
    from_schema_version: str,
    to_schema_version: str,
    migration_procedure: ProcedurePin,
    comparison_policy_id: str,
    comparison_policy_version: str,
    declared_field_changes: Sequence[FieldChangeIntent] = (),
    migrated_at_utc: str | None = None,
) -> MigrationRecord:
    """Seal one migration record — always written 'declared'."""
    return _seal(
        MigrationRecord,
        {
            'document_id': document_id,
            'kind': kind,
            'source_archive_ref': source_archive_ref.model_dump(mode='json'),
            'target_archive_ref': target_archive_ref.model_dump(mode='json'),
            'from_schema_version': from_schema_version,
            'to_schema_version': to_schema_version,
            'migration_procedure': migration_procedure.model_dump(
                mode='json'
            ),
            'comparison_policy_id': comparison_policy_id,
            'comparison_policy_version': comparison_policy_version,
            'declared_field_changes': [
                c.model_dump(mode='json') for c in declared_field_changes
            ],
            'migration_status_at_write': 'declared',
            'migrated_at_utc': migrated_at_utc or _utc_now(),
        },
        'migration_id',
        'migration_sha256',
        'mig',
    )


def migration_binding(migration: MigrationRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='migration_record',
        ref_id=migration.migration_id,
        ref_sha256=migration.migration_sha256,
    )


class MigrationCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: MigrationVerificationCheck
    outcome: CheckOutcome
    detail: str | None = None


class MigrationVerification(BaseModel):
    """Sealed post-migration preservation verdict (#718 §4/§13)."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    migration_ref: AuthorityRef
    source_archive_ref: AuthorityRef
    target_archive_ref: AuthorityRef
    checks: tuple[MigrationCheckResult, ...] = ()
    status: MigrationStatus
    comparison_policy_id: str = Field(min_length=1)
    comparison_policy_version: str = Field(min_length=1)
    verified_at_utc: str = Field(min_length=1)
    verification_version: str = Field(
        default=MIGRATION_VERIFICATION_VERSION, min_length=1
    )
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'migration_ref': self.migration_ref.model_dump(mode='json'),
            'source_archive_ref': self.source_archive_ref.model_dump(
                mode='json'
            ),
            'target_archive_ref': self.target_archive_ref.model_dump(
                mode='json'
            ),
            'checks': [c.model_dump(mode='json') for c in self.checks],
            'status': self.status,
            'comparison_policy_id': self.comparison_policy_id,
            'comparison_policy_version': self.comparison_policy_version,
            'verified_at_utc': self.verified_at_utc,
            'verification_version': self.verification_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'MigrationVerification':
        _require_iso8601(
            self.verified_at_utc,
            'migration verification verified_at_utc',
        )
        _require_ref_sha(self.migration_ref, 'migration_ref')
        _require_ref_sha(self.source_archive_ref, 'source_archive_ref')
        _require_ref_sha(self.target_archive_ref, 'target_archive_ref')
        expected = _hash(self.identity_payload())
        if self.verification_sha256 != expected:
            raise ValueError('migration verification hash mismatch')
        if self.verification_id != _semantic_id('migver', expected):
            raise ValueError(
                'migration verification id does not match its hash'
            )
        return self


def evaluate_migration(
    document_id: str,
    migration: MigrationRecord,
    checks: Sequence[MigrationCheckResult],
    *,
    verified_at_utc: str | None = None,
) -> MigrationVerification:
    """Fail-closed migration verification (#718 §4/§13).

    - Any ``fail`` → ``verification_failed`` (independent of declared
      field changes).
    - ``verified_equivalent`` requires the full required set passed
      with ``semantic_checksum_match`` — meaning survived byte-for-
      byte-equivalent semantics under the comparison policy.
    - ``verified_with_declared_differences`` requires
      ``semantic_diff_declared`` + ``semantic_diff_accepted`` passed
      (checksum may differ — every drift was declared and accepted),
      plus all other required checks.
    - Any required check ``not_checked`` / ``not_applicable`` →
      ``unverified`` — an unproven migration can never claim
      preservation.
    """

    verified_at_utc = verified_at_utc or _utc_now()
    _require_iso8601(verified_at_utc, 'verified_at_utc')

    required: tuple[MigrationVerificationCheck, ...] = (
        'target_readable',
        'content_hash_match',
        'referenced_pins_resolve',
        'preservation_scope_complete',
        'declared_transformations_applied',
    )
    by_check = {c.check: c.outcome for c in checks}
    checksum = by_check.get('semantic_checksum_match')
    diff_accepted = (
        by_check.get('semantic_diff_declared') == 'pass'
        and by_check.get('semantic_diff_accepted') == 'pass'
    )
    if not checks:
        status: MigrationStatus = 'unverified'
    elif any(
        # A failing semantic checksum is *explained* by a declared,
        # accepted diff — it does not by itself fail the migration;
        # every other check must pass.
        outcome == 'fail'
        and not (check == 'semantic_checksum_match' and diff_accepted)
        for check, outcome in by_check.items()
    ):
        status = 'verification_failed'
    elif not all(by_check.get(c) == 'pass' for c in required):
        status = 'unverified'
    elif checksum == 'pass':
        status = 'verified_equivalent'
    elif checksum in ('fail', 'not_applicable') and diff_accepted:
        # Semantics changed — but every difference was declared in
        # declared_field_changes and accepted under the policy.
        status = 'verified_with_declared_differences'
    else:
        status = 'unverified'

    return _seal(
        MigrationVerification,
        {
            'document_id': document_id,
            'migration_ref': migration_binding(migration).model_dump(
                mode='json'
            ),
            'source_archive_ref': migration.source_archive_ref.model_dump(
                mode='json'
            ),
            'target_archive_ref': migration.target_archive_ref.model_dump(
                mode='json'
            ),
            'checks': [c.model_dump(mode='json') for c in checks],
            'status': status,
            'comparison_policy_id': migration.comparison_policy_id,
            'comparison_policy_version': (
                migration.comparison_policy_version
            ),
            'verified_at_utc': verified_at_utc,
        },
        'verification_id',
        'verification_sha256',
        'migver',
    )


__all__ = [
    'ARCHIVE_SCHEMA_VERSION',
    'ARCHIVE_VERIFICATION_VERSION',
    'MIGRATION_VERIFICATION_VERSION',
    'PreservationScope',
    'ArchiveVerificationCheck',
    'CheckOutcome',
    'MigrationVerificationCheck',
    'ArchiveVerificationStatus',
    'MigrationStatus',
    'MigrationKind',
    'ProcedurePin',
    'ArchiveSnapshot',
    'build_archive_snapshot',
    'archive_binding',
    'ArchiveCheckResult',
    'ArchiveVerification',
    'evaluate_archive',
    'FieldChangeIntent',
    'MigrationRecord',
    'build_migration_record',
    'migration_binding',
    'MigrationCheckResult',
    'MigrationVerification',
    'evaluate_migration',
]
