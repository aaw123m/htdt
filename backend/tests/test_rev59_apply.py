"""REV59-APPLY regression tests — device apply transaction authority (#723)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_apply_transaction import (
    ApplyCapabilityProfile,
    ApplyVerificationRecord,
    ApplyWriteRecord,
    DeviceApplyPlan,
    DeviceApplyTransaction,
    DesiredSettingEntry,
    RollbackExecutionRecord,
    RollbackPlan,
    build_apply_transaction,
    evaluate_apply_state,
    evaluate_rollback_claim,
    evaluate_rollback_outcome,
)
from htdt.cad_apply_transaction_repository import (
    ApplyTransactionIntegrityError,
    CadApplyTransactionRepository,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-apply'

_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _profile(**kwargs) -> ApplyCapabilityProfile:
    payload = dict(
        document_id=DOC,
        device_ref=_ref('device_instance', 'dev-avr-1'),
        adapter_kind='vendor_avr_api',
        declared_capabilities=('sequential_write_with_readback',),
        capability_evidence='api_documented',
        declared_at_utc='2026-01-01T00:00:00+00:00',
    )
    payload.update(kwargs)
    return ApplyCapabilityProfile.create(**payload)


def _plan(
    capability: ApplyCapabilityProfile | None = None, **kwargs
) -> DeviceApplyPlan:
    if capability is None:
        capability = _profile()
    payload = dict(
        document_id=DOC,
        device_ref=_ref('device_instance', 'dev-avr-1'),
        capability_ref=AuthorityRef(
            kind='apply_capability',
            ref_id=capability.profile_id,
            ref_sha256=capability.profile_sha256,
        ),
        pre_state_evidence='full_readback_captured',
        pre_state_snapshot_ref=_ref('device_snapshot', 'snap-1'),
        desired_state_repr_sha256=_SHA,
        delta_entries=(
            DesiredSettingEntry(
                field_path='dsp.eq.band1.gain_db',
                desired_value_repr='-3.0',
            ),
            DesiredSettingEntry(
                field_path='dsp.eq.band2.gain_db',
                desired_value_repr='+1.5',
            ),
        ),
        write_order=('dsp.eq.band1.gain_db', 'dsp.eq.band2.gain_db'),
        exclusive_access_required=True,
        rollback_strategy='full_restore',
        planned_at_utc='2026-01-02T00:00:00+00:00',
    )
    payload.update(kwargs)
    return DeviceApplyPlan.create(**payload)


def _write(
    plan: DeviceApplyPlan, seq: int, field: str, outcome: str = 'acknowledged',
    **kwargs,
) -> ApplyWriteRecord:
    payload = dict(
        document_id=DOC,
        plan_ref=AuthorityRef(
            kind='apply_plan', ref_id=plan.plan_id,
            ref_sha256=plan.plan_sha256,
        ),
        sequence_index=seq,
        write_kind='write_field',
        target_field_path=field,
        outcome=outcome,
        ack_payload_sha256=_SHA if outcome == 'acknowledged' else None,
        observed_at_utc='2026-01-02T00:01:0%d+00:00' % seq,
    )
    payload.update(kwargs)
    return ApplyWriteRecord.create(**payload)


def _verification(
    plan: DeviceApplyPlan, **kwargs
) -> ApplyVerificationRecord:
    payload = dict(
        document_id=DOC,
        plan_ref=AuthorityRef(
            kind='apply_plan', ref_id=plan.plan_id,
            ref_sha256=plan.plan_sha256,
        ),
        post_state_snapshot_ref=_ref('device_snapshot', 'snap-2'),
        fields_verified=tuple(plan.write_order),
        fields_failed=(),
        readback_means='device_readback',
        verified_at_utc='2026-01-02T00:05:00+00:00',
    )
    payload.update(kwargs)
    return ApplyVerificationRecord.create(**payload)


def _rollback_plan(plan: DeviceApplyPlan, **kwargs) -> RollbackPlan:
    payload = dict(
        document_id=DOC,
        plan_ref=AuthorityRef(
            kind='apply_plan', ref_id=plan.plan_id,
            ref_sha256=plan.plan_sha256,
        ),
        pre_state_evidence='full_readback_captured',
        restorable_fields=tuple(plan.write_order),
        restore_steps=('restore snapshot snap-1',),
        claim='exact_rollback_claimable',
    )
    payload.update(kwargs)
    return RollbackPlan.create(**payload)


def _rollback_exec(
    rbplan: RollbackPlan, **kwargs
) -> RollbackExecutionRecord:
    payload = dict(
        document_id=DOC,
        rollback_plan_ref=AuthorityRef(
            kind='rollback_plan', ref_id=rbplan.rollback_plan_id,
            ref_sha256=rbplan.rollback_plan_sha256,
        ),
        post_rollback_snapshot_ref=_ref('device_snapshot', 'snap-1'),
        restored_fields=('dsp.eq.band1.gain_db', 'dsp.eq.band2.gain_db'),
        outcome='exact_prior_state_restored',
        executed_at_utc='2026-01-02T00:10:00+00:00',
    )
    payload.update(kwargs)
    return RollbackExecutionRecord.create(**payload)


# ---------------------------------------------------------------------------
# capability profile
# ---------------------------------------------------------------------------


def test_atomic_capability_requires_observed_evidence() -> None:
    with pytest.raises(ValidationError):
        _profile(
            declared_capabilities=('stage_validate_commit_atomic',),
            capability_evidence='assumed',
        )


def test_atomic_capability_ok_with_documented_api() -> None:
    profile = _profile(
        declared_capabilities=('stage_validate_commit_atomic',),
        capability_evidence='api_documented',
    )
    assert profile.profile_id.startswith('apcap-')


def test_capability_evidence_unknown_rejects_atomic() -> None:
    with pytest.raises(ValidationError):
        _profile(
            declared_capabilities=('atomic_bulk_apply',),
            capability_evidence='unknown',
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_requires_full_write_order_coverage() -> None:
    with pytest.raises(ValidationError):
        _plan(write_order=('dsp.eq.band1.gain_db',))


def test_plan_identity_changes_with_desired_state() -> None:
    plan_a = _plan()
    plan_b = _plan(
        delta_entries=(
            DesiredSettingEntry(
                field_path='dsp.eq.band1.gain_db',
                desired_value_repr='-3.0',
            ),
            DesiredSettingEntry(
                field_path='dsp.eq.band2.gain_db',
                desired_value_repr='+2.0',
            ),
        )
    )
    assert plan_a.plan_id != plan_b.plan_id


def test_plan_sealed_fields() -> None:
    plan = _plan()
    digest = canonical_sha256(plan.identity_payload())
    assert plan.plan_sha256 == digest
    assert plan.plan_id == f'applan-{digest[:24]}'


# ---------------------------------------------------------------------------
# write records
# ---------------------------------------------------------------------------


def test_acknowledged_write_requires_ack_payload() -> None:
    with pytest.raises(ValidationError):
        _write(_plan(), 0, 'dsp.eq.band1.gain_db',
               outcome='acknowledged', ack_payload_sha256=None)


def test_write_field_requires_target() -> None:
    with pytest.raises(ValidationError):
        _write(_plan(), 0, '', target_field_path=None)


# ---------------------------------------------------------------------------
# evaluate_apply_state ladder
# ---------------------------------------------------------------------------


def test_verdict_not_started_without_writes() -> None:
    plan = _plan()
    assert evaluate_apply_state(plan, (), None) == 'not_started'


def test_verdict_apply_failed_on_rejected_write() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db', outcome='rejected'),
    )
    assert evaluate_apply_state(plan, writes, None) == 'apply_failed'


def test_verdict_unknown_state_on_interruption() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(
            plan, 1, 'dsp.eq.band2.gain_db',
            outcome='communication_error',
        ),
    )
    assert evaluate_apply_state(plan, writes, None) == 'unknown_state'


def test_verdict_partially_applied() -> None:
    plan = _plan()
    writes = (_write(plan, 0, 'dsp.eq.band1.gain_db'),)
    assert (
        evaluate_apply_state(plan, writes, None) == 'partially_applied'
    )


def test_write_success_alone_is_insufficient_evidence() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db'),
    )
    assert (
        evaluate_apply_state(plan, writes, None)
        == 'insufficient_evidence'
    )


def test_fully_applied_only_with_readback_verification() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db'),
    )
    verification = _verification(plan)
    assert (
        evaluate_apply_state(plan, writes, verification)
        == 'fully_applied'
    )


def test_verification_with_failed_field_is_apply_failed() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db'),
    )
    verification = _verification(
        plan,
        fields_verified=('dsp.eq.band1.gain_db',),
        fields_failed=('dsp.eq.band2.gain_db',),
    )
    assert (
        evaluate_apply_state(plan, writes, verification)
        == 'apply_failed'
    )


def test_verification_with_unknown_field_is_insufficient() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db'),
    )
    verification = _verification(
        plan,
        fields_verified=('dsp.eq.band1.gain_db',),
        fields_unknown=('dsp.eq.band2.gain_db',),
    )
    assert (
        evaluate_apply_state(plan, writes, verification)
        == 'insufficient_evidence'
    )


def test_writes_from_other_plan_do_not_count() -> None:
    plan = _plan()
    other = _plan(pre_state_evidence='partial_readback_captured')
    writes = (
        _write(other, 0, 'dsp.eq.band1.gain_db'),
        _write(other, 1, 'dsp.eq.band2.gain_db'),
    )
    assert evaluate_apply_state(plan, writes, None) == 'not_started'


def test_rolled_back_verdict() -> None:
    plan = _plan()
    writes = (
        _write(plan, 0, 'dsp.eq.band1.gain_db'),
        _write(plan, 1, 'dsp.eq.band2.gain_db'),
    )
    rbplan = _rollback_plan(plan)
    execution = _rollback_exec(rbplan)
    assert (
        evaluate_apply_state(
            plan, writes, None, rollback_execution=execution
        )
        == 'rolled_back'
    )


# ---------------------------------------------------------------------------
# rollback claim gate
# ---------------------------------------------------------------------------


def test_exact_claim_from_full_evidence() -> None:
    plan = _plan()
    assert evaluate_rollback_claim(plan) == 'exact_rollback_claimable'


def test_partial_evidence_caps_claim() -> None:
    plan = _plan(
        pre_state_evidence='partial_readback_captured',
        rollback_strategy='full_restore',
    )
    assert evaluate_rollback_claim(plan) == 'partial_rollback_only'


def test_user_recorded_only_cannot_claim() -> None:
    plan = _plan(
        pre_state_evidence='user_recorded_only',
        pre_state_snapshot_ref=None,
        rollback_strategy='partial_restore',
    )
    assert evaluate_rollback_claim(plan) == 'claim_unknown'


def test_rollback_plan_rejects_exact_from_partial() -> None:
    plan = _plan(pre_state_evidence='partial_readback_captured')
    with pytest.raises(ValidationError):
        _rollback_plan(
            plan, pre_state_evidence='partial_readback_captured'
        )


def test_rollback_plan_rejects_claim_without_evidence() -> None:
    plan = _plan()
    with pytest.raises(ValidationError):
        _rollback_plan(
            plan,
            pre_state_evidence='user_recorded_only',
            claim='partial_rollback_only',
        )


# ---------------------------------------------------------------------------
# rollback outcome
# ---------------------------------------------------------------------------


def test_rollback_outcome_not_attempted() -> None:
    rbplan = _rollback_plan(_plan())
    assert evaluate_rollback_outcome(rbplan, None) == 'not_attempted'


def test_exact_restore_verdict() -> None:
    rbplan = _rollback_plan(_plan())
    execution = _rollback_exec(rbplan)
    assert (
        evaluate_rollback_outcome(rbplan, execution)
        == 'exact_prior_state_restored'
    )


def test_exact_restore_requires_post_snapshot() -> None:
    rbplan = _rollback_plan(_plan())
    with pytest.raises(ValidationError):
        _rollback_exec(rbplan, post_rollback_snapshot_ref=None)


def test_exact_restore_rejects_failed_fields() -> None:
    rbplan = _rollback_plan(_plan())
    with pytest.raises(ValidationError):
        _rollback_exec(
            rbplan,
            restored_fields=('dsp.eq.band1.gain_db',),
            failed_fields=('dsp.eq.band2.gain_db',),
        )


def test_exact_restore_without_claim_is_unknown() -> None:
    plan = _plan(pre_state_evidence='partial_readback_captured')
    rbplan = _rollback_plan(
        plan,
        pre_state_evidence='partial_readback_captured',
        claim='partial_rollback_only',
        non_restorable_fields=('dsp.eq.band2.gain_db',),
        restorable_fields=('dsp.eq.band1.gain_db',),
    )
    execution = _rollback_exec(
        rbplan, restored_fields=('dsp.eq.band1.gain_db',)
    )
    assert evaluate_rollback_outcome(rbplan, execution) == 'unknown'


# ---------------------------------------------------------------------------
# transaction lifecycle
# ---------------------------------------------------------------------------


def test_transaction_requires_verification_for_fully_applied() -> None:
    plan = _plan()
    capability = _profile()
    with pytest.raises(ValidationError):
        build_apply_transaction(
            document_id=DOC,
            plan=plan,
            capability=capability,
            state_verdict='fully_applied',
            opened_at_utc='2026-01-02T00:00:30+00:00',
        )


def test_rolled_back_requires_execution_ref() -> None:
    plan = _plan()
    capability = _profile()
    with pytest.raises(ValidationError):
        build_apply_transaction(
            document_id=DOC,
            plan=plan,
            capability=capability,
            state_verdict='rolled_back',
            opened_at_utc='2026-01-02T00:00:30+00:00',
        )


def test_transaction_rejects_cross_device() -> None:
    plan = _plan()
    capability = _profile(
        device_ref=_ref(
            'device_instance', 'dev-other',
            sha=canonical_sha256({'fixture': 'other-device'}),
        )
    )
    with pytest.raises(ValueError, match='different devices'):
        build_apply_transaction(
            document_id=DOC,
            plan=plan,
            capability=capability,
            state_verdict='not_started',
            opened_at_utc='2026-01-02T00:00:30+00:00',
        )


def test_build_transaction_seals() -> None:
    plan = _plan()
    capability = _profile()
    txn = build_apply_transaction(
        document_id=DOC,
        plan=plan,
        capability=capability,
        state_verdict='fully_applied',
        verification_ref=_ref('apply_verification', 'ver-1'),
        opened_at_utc='2026-01-02T00:00:30+00:00',
    )
    assert txn.transaction_id.startswith('aptxn-')
    assert txn.plan_ref.ref_sha256 == plan.plan_sha256


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> CadApplyTransactionRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadApplyTransactionRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    profile = _profile()
    plan = _plan(profile)
    write = _write(plan, 0, 'dsp.eq.band1.gain_db')
    verification = _verification(plan)
    rbplan = _rollback_plan(plan)
    execution = _rollback_exec(rbplan)
    txn = build_apply_transaction(
        document_id=DOC,
        plan=plan,
        capability=profile,
        state_verdict='fully_applied',
        verification_ref=_ref('apply_verification', 'ver-1'),
        opened_at_utc='2026-01-02T00:00:30+00:00',
    )
    repo.save_profile(profile)
    repo.save_plan(plan)
    repo.save_write(write)
    repo.save_verification(verification)
    repo.save_rollback_plan(rbplan)
    repo.save_rollback_execution(execution)
    repo.save_transaction(txn)

    assert repo.get_profile(profile.profile_id) == profile
    assert repo.get_plan(plan.plan_id) == plan
    assert repo.get_write(write.write_id) == write
    assert repo.get_verification(
        verification.verification_id
    ) == verification
    assert repo.get_rollback_plan(
        rbplan.rollback_plan_id
    ) == rbplan
    assert repo.get_rollback_execution(
        execution.execution_id
    ) == execution
    assert repo.get_transaction(txn.transaction_id) == txn


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan()
    repo.save_plan(plan)
    repo.save_plan(plan)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan()
    repo.save_plan(plan)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_apply_plans SET rollback_strategy=? '
            'WHERE plan_id=?',
            ('none', plan.plan_id),
        )
        connection.commit()
    with pytest.raises(ApplyTransactionIntegrityError):
        repo.get_plan(plan.plan_id)


def test_apply_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with sqlite3.connect(db) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_apply_capability_profiles',
        'cad_apply_plans',
        'cad_apply_write_records',
        'cad_apply_verifications',
        'cad_apply_rollback_plans',
        'cad_apply_rollback_executions',
        'cad_apply_transactions',
    }
    assert expected <= names
