"""Device configuration apply-transaction / rollback authority (#723).

A successful write command does **not** prove that a complete
configuration was applied atomically. A communication or power failure
mid-sequence can leave a device in a partially changed state that
matches neither the old nor the intended digital twin. This module owns
the sealed records that make a device-apply lifecycle auditable:

- :class:`ApplyCapabilityProfile` — the declared mutation semantics of
  one device/adapter/firmware (stage+validate+commit, atomic bulk,
  sequential writes, backup-only, …). Atomicity is never inferred from
  an API accepting a multi-field JSON object.

- :class:`DeviceApplyPlan` — the immutable pre-mutation plan: target
  device, pre-state evidence, desired delta, write order, validation
  prerequisites, exclusive-access requirement, rollback strategy and
  post-apply checks. Any change to the desired set is a different plan.

- :class:`ApplyWriteRecord` — one record per attempted write/stage/
  validate/commit step with an honest outcome (acknowledged / rejected /
  timeout / communication_error / unknown).

- :class:`ApplyVerificationRecord` — post-apply readback comparison:
  which planned fields were verified, failed or are unknown. Only a
  readback comparison can claim ``fully_applied``.

- :class:`RollbackPlan` / :class:`RollbackExecutionRecord` — what can
  be restored, what cannot, and what actually happened. Exact rollback
  is never promised from a partial snapshot (Trinnov precedent: preset
  backups exclude source configuration).

- :class:`DeviceApplyTransaction` — the sealed lifecycle binding plan +
  capability profile + lock evidence + writes + verification +
  optional rollback, carrying the final state verdict.

Verdicts are fail-closed: ``deployed = true`` is never derived from
write success alone; unknown state stays ``unknown_state``; a missing
verification stage yields ``insufficient_evidence``.

External precedent: NETCONF RFC 6241 (candidate datastore, locking,
validated commit, rollback-on-error, confirmed-commit auto-revert).
HTDT models equivalent capability classes — it never assumes AV
products implement NETCONF.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


APPLY_SCHEMA_VERSION = 'apply-transaction-1'
APPLY_EVALUATION_VERSION = 'apply-transaction-eval-1'

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


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

ApplyCapability = Literal[
    'stage_validate_commit_atomic',
    'atomic_bulk_apply',
    'transaction_with_rollback',
    'sequential_write_with_readback',
    'sequential_write_no_readback',
    'backup_restore_only',
    'partial_backup_restore',
    'no_rollback',
    'unknown',
]

CapabilityEvidenceMeans = Literal[
    'api_documented',
    'behavior_observed',
    'vendor_declared',
    'assumed',
    'unknown',
]

PreStateEvidenceKind = Literal[
    'full_readback_captured',
    'partial_readback_captured',
    'backup_captured',
    'readback_plus_backup',
    'user_recorded_only',
    'unknown',
]

ApplyWriteKind = Literal[
    'stage',
    'validate',
    'commit',
    'write_field',
    'restore_backup',
    'confirm_commit',
    'rollback_step',
    'acquire_lock',
    'release_lock',
]

ApplyWriteOutcome = Literal[
    'acknowledged',
    'rejected',
    'timeout',
    'communication_error',
    'unknown',
]

ApplyStateVerdict = Literal[
    'not_started',
    'fully_applied',
    'partially_applied',
    'rolled_back',
    'apply_failed',
    'unknown_state',
    'insufficient_evidence',
]

RollbackOutcomeVerdict = Literal[
    'exact_prior_state_restored',
    'partial_prior_state_restored',
    'nothing_restorable',
    'rollback_failed',
    'not_attempted',
    'unknown',
]

RollbackClaim = Literal[
    'exact_rollback_claimable',
    'partial_rollback_only',
    'no_rollback_claim',
    'claim_unknown',
]

_ATOMIC_CAPABILITIES = frozenset(
    {
        'stage_validate_commit_atomic',
        'atomic_bulk_apply',
        'transaction_with_rollback',
    }
)

_OBSERVED_MEANS = frozenset({'api_documented', 'behavior_observed'})

CAPABILITY_LABELS: dict[str, str] = {
    'stage_validate_commit_atomic': '段階・検証・原子コミット',
    'atomic_bulk_apply': '一括アトミック適用',
    'transaction_with_rollback': 'ロールバック付きトランザクション',
    'sequential_write_with_readback': '逐次書込み（読戻しあり）',
    'sequential_write_no_readback': '逐次書込み（読戻しなし）',
    'backup_restore_only': 'バックアップ復元のみ',
    'partial_backup_restore': '部分バックアップ復元',
    'no_rollback': 'ロールバック不可',
    'unknown': '不明',
}

STATE_VERDICT_LABELS: dict[str, str] = {
    'not_started': '未着手',
    'fully_applied': '完全適用済み',
    'partially_applied': '部分的に適用',
    'rolled_back': 'ロールバック済み',
    'apply_failed': '適用失敗',
    'unknown_state': '状態不明',
    'insufficient_evidence': '証拠不足',
}

OUTCOME_LABELS: dict[str, str] = {
    'acknowledged': '受理確認',
    'rejected': '拒否',
    'timeout': 'タイムアウト',
    'communication_error': '通信エラー',
    'unknown': '不明',
}

CLAIM_LABELS: dict[str, str] = {
    'exact_rollback_claimable': '完全復元を主張可能',
    'partial_rollback_only': '部分復元のみ',
    'no_rollback_claim': '復元不能',
    'claim_unknown': '復元可否不明',
}


# ---------------------------------------------------------------------------
# Capability profile
# ---------------------------------------------------------------------------


class ApplyCapabilityProfile(BaseModel):
    """Declared apply/rollback semantics for one device+adapter+firmware."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    adapter_kind: str = 'unknown'
    firmware_ref: AuthorityRef | None = None
    declared_capabilities: tuple[ApplyCapability, ...]
    capability_evidence: CapabilityEvidenceMeans
    external_lock_supported: bool | None = None
    notes: str = ''
    declared_at_utc: str

    @model_validator(mode='after')
    def _validate(self) -> 'ApplyCapabilityProfile':
        _require_refs(self.device_ref)
        if self.firmware_ref is not None:
            _require_refs(self.firmware_ref)
        if not self.declared_capabilities:
            raise ValueError('declared_capabilities must not be empty')
        if (
            _ATOMIC_CAPABILITIES & set(self.declared_capabilities)
            and self.capability_evidence not in _OBSERVED_MEANS
        ):
            raise ValueError(
                'atomic apply capabilities require api_documented or '
                'behavior_observed evidence'
            )
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ApplyCapabilityProfile':
        return _seal(
            cls, kwargs, 'profile_id', 'profile_sha256', 'apcap'
        )


# ---------------------------------------------------------------------------
# Apply plan
# ---------------------------------------------------------------------------


class DesiredSettingEntry(BaseModel):
    """One intended field change inside an apply plan."""

    model_config = ConfigDict(frozen=True)

    field_path: str
    desired_value_repr: str
    atomic_group: str | None = None


class DeviceApplyPlan(BaseModel):
    """Immutable pre-mutation plan; any desired change is a new plan."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    capability_ref: AuthorityRef
    pre_state_snapshot_ref: AuthorityRef | None = None
    pre_state_evidence: PreStateEvidenceKind = 'unknown'
    desired_state_repr_sha256: str = Field(pattern=_SHA256_PATTERN)
    delta_entries: tuple[DesiredSettingEntry, ...]
    write_order: tuple[str, ...]
    validation_prerequisites: tuple[str, ...] = ()
    exclusive_access_required: bool = True
    rollback_strategy: Literal[
        'full_restore', 'partial_restore', 'recreate_manually',
        'none', 'unknown',
    ] = 'unknown'
    timeout_seconds: float | None = None
    retry_policy: str = 'none'
    post_apply_checks: tuple[str, ...] = ()
    re_verification_requirements: tuple[str, ...] = ()
    planned_at_utc: str

    @model_validator(mode='after')
    def _validate(self) -> 'DeviceApplyPlan':
        _require_refs(self.device_ref, self.capability_ref)
        if self.pre_state_snapshot_ref is not None:
            _require_refs(self.pre_state_snapshot_ref)
        if not self.delta_entries:
            raise ValueError('delta_entries must not be empty')
        paths = {entry.field_path for entry in self.delta_entries}
        missing = paths - set(self.write_order)
        if missing:
            raise ValueError(
                'write_order must cover every delta field: '
                + ', '.join(sorted(missing))
            )
        if self.timeout_seconds is not None:
            if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
                raise ValueError('timeout_seconds must be positive')
        _require_iso8601(self.planned_at_utc, 'planned_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'DeviceApplyPlan':
        return _seal(cls, kwargs, 'plan_id', 'plan_sha256', 'applan')


# ---------------------------------------------------------------------------
# Write records
# ---------------------------------------------------------------------------


class ApplyWriteRecord(BaseModel):
    """One attempted mutation step; ``acknowledged`` needs ack evidence."""

    model_config = ConfigDict(frozen=True)

    write_id: str
    write_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    sequence_index: int = Field(ge=0)
    write_kind: ApplyWriteKind
    target_field_path: str | None = None
    outcome: ApplyWriteOutcome
    ack_payload_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    observed_at_utc: str

    @model_validator(mode='after')
    def _validate(self) -> 'ApplyWriteRecord':
        _require_refs(self.plan_ref)
        if (
            self.write_kind == 'write_field'
            and not self.target_field_path
        ):
            raise ValueError(
                'write_field records must name target_field_path'
            )
        if self.outcome == 'acknowledged' and self.ack_payload_sha256 is None:
            raise ValueError(
                'acknowledged outcomes require ack_payload_sha256'
            )
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'write_id', 'write_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ApplyWriteRecord':
        return _seal(cls, kwargs, 'write_id', 'write_sha256', 'apwrite')


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class ApplyVerificationRecord(BaseModel):
    """Post-apply readback comparison against the plan's delta."""

    model_config = ConfigDict(frozen=True)

    verification_id: str
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    post_state_snapshot_ref: AuthorityRef
    fields_verified: tuple[str, ...]
    fields_failed: tuple[str, ...]
    fields_unknown: tuple[str, ...] = ()
    readback_means: Literal[
        'device_readback', 'backup_compare', 'user_observed', 'unknown'
    ]
    verified_at_utc: str

    @model_validator(mode='after')
    def _validate(self) -> 'ApplyVerificationRecord':
        _require_refs(self.plan_ref, self.post_state_snapshot_ref)
        overlap = (
            set(self.fields_verified)
            & set(self.fields_failed)
            | set(self.fields_verified) & set(self.fields_unknown)
            | set(self.fields_failed) & set(self.fields_unknown)
        )
        if overlap:
            raise ValueError(
                'a field can only appear in one verification bucket'
            )
        _require_iso8601(self.verified_at_utc, 'verified_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'verification_id', 'verification_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ApplyVerificationRecord':
        return _seal(
            cls,
            kwargs,
            'verification_id',
            'verification_sha256',
            'apver',
        )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class RollbackPlan(BaseModel):
    """What a rollback could restore — and what it provably cannot."""

    model_config = ConfigDict(frozen=True)

    rollback_plan_id: str
    rollback_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    pre_state_evidence: PreStateEvidenceKind
    restorable_fields: tuple[str, ...]
    non_restorable_fields: tuple[str, ...] = ()
    restore_steps: tuple[str, ...] = ()
    claim: RollbackClaim

    @model_validator(mode='after')
    def _validate(self) -> 'RollbackPlan':
        _require_refs(self.plan_ref)
        overlap = set(self.restorable_fields) & set(
            self.non_restorable_fields
        )
        if overlap:
            raise ValueError(
                'a field cannot be both restorable and non-restorable'
            )
        if self.claim == 'exact_rollback_claimable':
            if self.pre_state_evidence not in (
                'full_readback_captured',
                'readback_plus_backup',
            ):
                raise ValueError(
                    'exact rollback requires full pre-state evidence'
                )
            if self.non_restorable_fields:
                raise ValueError(
                    'exact rollback claim conflicts with '
                    'non-restorable fields'
                )
        if (
            self.pre_state_evidence in ('user_recorded_only', 'unknown')
            and self.claim in (
                'exact_rollback_claimable',
                'partial_rollback_only',
            )
        ):
            raise ValueError(
                'rollback claims require captured pre-state evidence'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'rollback_plan_id', 'rollback_plan_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'RollbackPlan':
        return _seal(
            cls,
            kwargs,
            'rollback_plan_id',
            'rollback_plan_sha256',
            'rbplan',
        )


class RollbackExecutionRecord(BaseModel):
    """The executed rollback and its honest outcome."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    execution_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    rollback_plan_ref: AuthorityRef
    post_rollback_snapshot_ref: AuthorityRef | None = None
    restored_fields: tuple[str, ...] = ()
    failed_fields: tuple[str, ...] = ()
    outcome: RollbackOutcomeVerdict
    executed_at_utc: str

    @model_validator(mode='after')
    def _validate(self) -> 'RollbackExecutionRecord':
        _require_refs(self.rollback_plan_ref)
        if self.post_rollback_snapshot_ref is not None:
            _require_refs(self.post_rollback_snapshot_ref)
        if self.outcome == 'exact_prior_state_restored':
            if self.post_rollback_snapshot_ref is None:
                raise ValueError(
                    'exact restore requires post-rollback readback'
                )
            if self.failed_fields:
                raise ValueError(
                    'exact restore conflicts with failed fields'
                )
        _require_iso8601(self.executed_at_utc, 'executed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'execution_id', 'execution_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'RollbackExecutionRecord':
        return _seal(
            cls, kwargs, 'execution_id', 'execution_sha256', 'rbexec'
        )


# ---------------------------------------------------------------------------
# Transaction lifecycle
# ---------------------------------------------------------------------------


class DeviceApplyTransaction(BaseModel):
    """The sealed apply lifecycle: plan → lock → writes → verify."""

    model_config = ConfigDict(frozen=True)

    transaction_id: str
    transaction_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    capability_ref: AuthorityRef
    mutation_lock_ref: AuthorityRef | None = None
    external_lock_obtained: bool | None = None
    write_refs: tuple[AuthorityRef, ...] = ()
    verification_ref: AuthorityRef | None = None
    rollback_plan_ref: AuthorityRef | None = None
    rollback_execution_ref: AuthorityRef | None = None
    state_verdict: ApplyStateVerdict
    opened_at_utc: str
    closed_at_utc: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DeviceApplyTransaction':
        _require_refs(self.plan_ref, self.capability_ref)
        for ref in (
            self.mutation_lock_ref,
            self.verification_ref,
            self.rollback_plan_ref,
            self.rollback_execution_ref,
            *self.write_refs,
        ):
            if ref is not None:
                _require_refs(ref)
        if self.state_verdict == 'fully_applied':
            if self.verification_ref is None:
                raise ValueError(
                    'fully_applied requires a verification record'
                )
        if self.state_verdict == 'rolled_back':
            if self.rollback_execution_ref is None:
                raise ValueError(
                    'rolled_back requires a rollback execution record'
                )
        _require_iso8601(self.opened_at_utc, 'opened_at_utc')
        if self.closed_at_utc is not None:
            _require_iso8601(self.closed_at_utc, 'closed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transaction_id', 'transaction_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'DeviceApplyTransaction':
        return _seal(
            cls, kwargs, 'transaction_id', 'transaction_sha256', 'aptxn'
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_apply_state(
    plan: DeviceApplyPlan,
    writes: tuple[ApplyWriteRecord, ...],
    verification: ApplyVerificationRecord | None,
    *,
    rollback_execution: RollbackExecutionRecord | None = None,
) -> ApplyStateVerdict:
    """Fail-closed state verdict for one apply plan.

    - ``rolled_back`` when a confirmed rollback execution is pinned.
    - ``fully_applied`` only when a readback verification confirms every
      planned field — write acknowledgements alone never qualify.
    - ``apply_failed`` when any write was rejected.
    - ``unknown_state`` when a communication error or timeout interrupts
      the sequence before all writes were attempted.
    - ``partially_applied`` when acknowledged writes exist but the plan
      is incomplete and no failure interrupted it.
    - ``insufficient_evidence`` when verification is absent or leaves
      planned fields unknown, or when write records are missing.
    """
    plan_writes = [
        w
        for w in writes
        if w.plan_ref.ref_sha256 == plan.plan_sha256
        and w.write_kind == 'write_field'
    ]
    acked = {w.target_field_path for w in plan_writes
             if w.outcome == 'acknowledged'}
    failed = [w for w in plan_writes if w.outcome == 'rejected']
    interrupted = [
        w
        for w in plan_writes
        if w.outcome in ('timeout', 'communication_error')
    ]

    if rollback_execution is not None and rollback_execution.outcome in (
        'exact_prior_state_restored',
        'partial_prior_state_restored',
    ):
        return 'rolled_back'

    if not plan_writes:
        return 'not_started'

    if failed:
        return 'apply_failed'

    planned_fields = {e.field_path for e in plan.delta_entries}
    if interrupted and acked != planned_fields:
        return 'unknown_state'

    if acked != planned_fields:
        return 'partially_applied'

    if verification is None:
        return 'insufficient_evidence'
    if verification.plan_ref.ref_sha256 != plan.plan_sha256:
        return 'insufficient_evidence'
    verified = set(verification.fields_verified)
    failed_v = set(verification.fields_failed)
    unknown_v = set(verification.fields_unknown)
    if failed_v:
        return 'apply_failed'
    if planned_fields - verified or unknown_v:
        return 'insufficient_evidence'
    return 'fully_applied'


def evaluate_rollback_claim(plan: DeviceApplyPlan) -> RollbackClaim:
    """Pre-apply gate: how much rollback can honestly be promised."""
    if plan.pre_state_evidence in (
        'full_readback_captured',
        'readback_plus_backup',
    ):
        if plan.rollback_strategy == 'full_restore':
            return 'exact_rollback_claimable'
        if plan.rollback_strategy == 'partial_restore':
            return 'partial_rollback_only'
        return 'claim_unknown'
    if plan.pre_state_evidence in (
        'partial_readback_captured',
        'backup_captured',
    ):
        if plan.rollback_strategy in ('partial_restore', 'full_restore'):
            return 'partial_rollback_only'
        if plan.rollback_strategy == 'none':
            return 'no_rollback_claim'
        return 'claim_unknown'
    if plan.rollback_strategy == 'none':
        return 'no_rollback_claim'
    return 'claim_unknown'


def evaluate_rollback_outcome(
    plan: RollbackPlan,
    execution: RollbackExecutionRecord | None,
) -> RollbackOutcomeVerdict:
    """Verdict on an executed rollback, fail-closed."""
    if execution is None:
        return 'not_attempted'
    if execution.rollback_plan_ref.ref_sha256 != plan.rollback_plan_sha256:
        return 'unknown'
    if execution.outcome == 'exact_prior_state_restored':
        if plan.claim != 'exact_rollback_claimable':
            return 'unknown'
        missing = set(plan.restorable_fields) - set(
            execution.restored_fields
        )
        if missing:
            return 'unknown'
    return execution.outcome


def build_apply_transaction(
    *,
    document_id: str,
    plan: DeviceApplyPlan,
    capability: ApplyCapabilityProfile,
    mutation_lock_ref: AuthorityRef | None = None,
    external_lock_obtained: bool | None = None,
    write_refs: tuple[AuthorityRef, ...] = (),
    verification_ref: AuthorityRef | None = None,
    rollback_plan_ref: AuthorityRef | None = None,
    rollback_execution_ref: AuthorityRef | None = None,
    state_verdict: ApplyStateVerdict,
    opened_at_utc: str | None = None,
    closed_at_utc: str | None = None,
) -> DeviceApplyTransaction:
    """Seal a transaction lifecycle record for ``plan``."""
    if capability.device_ref.ref_sha256 != plan.device_ref.ref_sha256:
        raise ValueError(
            'capability profile and plan target different devices'
        )
    return DeviceApplyTransaction.create(
        document_id=document_id,
        plan_ref=AuthorityRef(kind='apply_plan', ref_id=plan.plan_id,
                              ref_sha256=plan.plan_sha256),
        capability_ref=AuthorityRef(
            kind='apply_capability', ref_id=capability.profile_id,
            ref_sha256=capability.profile_sha256
        ),
        mutation_lock_ref=mutation_lock_ref,
        external_lock_obtained=external_lock_obtained,
        write_refs=write_refs,
        verification_ref=verification_ref,
        rollback_plan_ref=rollback_plan_ref,
        rollback_execution_ref=rollback_execution_ref,
        state_verdict=state_verdict,
        opened_at_utc=opened_at_utc or _utc_now(),
        closed_at_utc=closed_at_utc,
    )
