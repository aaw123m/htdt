"""#806 calibration deployment/verification-loop authority tests.

designed != exported != deployed != verified: an export or apply ack can
never produce a verified deployment — only a machine read-back snapshot
can, and manual attestation stays visibly weaker. Unsupported
materialized parameters fail closed, and rollback invalidates only
deployment-bound evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration_deployment import (
    CalibrationDeployment,
    DeploymentCapabilityDeclaration,
    DeploymentEffectivenessReport,
    DeploymentRollbackRecord,
    EffectivenessMetricDelta,
    deployment_state_with_rollbacks,
    derive_deployment_state,
    evaluate_deployment_effectiveness,
    evaluate_deployment_gate,
    rollback_invalidates_evidence,
)
from htdt.cad_calibration_deployment_repository import (
    CadCalibrationDeploymentRepository,
    DeploymentIntegrityError,
)
from htdt.cad_calibration import CadExportedChannelSettings
from htdt.cad_device_adapter import MaterializedCalibrationSettings
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-806'
_SHA = canonical_sha256({'fixture': 'sha'})
_TS = '2026-10-06T00:00:00Z'


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _dep_ref(dep: CalibrationDeployment) -> AuthorityRef:
    return AuthorityRef(
        kind='calibration_deployment',
        ref_id=dep.deployment_id,
        ref_sha256=dep.deployment_sha256,
    )


def _declaration(**kw) -> DeploymentCapabilityDeclaration:
    payload = dict(
        document_id=DOC,
        adapter_id='htdt-generic-biquad',
        adapter_version='1',
        adapter_kind='offline_file',
        device_family='generic',
        supports_apply=True,
        supports_read_back=True,
        supported_features=('peq', 'gain', 'delay'),
        declared_at_utc=_TS,
    )
    payload.update(kw)
    return DeploymentCapabilityDeclaration.create(**payload)


def _materialization(**kw) -> MaterializedCalibrationSettings:
    channel = CadExportedChannelSettings(
        channel_id='c1', role_id='fl', source_entity_id='src-1',
        physical_output_id='out-1', gain_db=-3.0, delay_s=0.004,
        polarity='normal', crossovers=(), peq=(), routing=('out-1',),
    )
    payload = dict(
        materialization_id='mat-1',
        created_at_utc=_TS,
        adapter_id='htdt-generic-biquad',
        adapter_version='1',
        export_id='exp-1',
        exported_settings_semantic_sha256=_SHA,
        binding_id='bind-1',
        binding_sha256=_SHA,
        payload_text='{"filters": []}',
        channel_settings=(channel,),
        quantization_applied=False,
        quantization_notes=(),
        unsupported_items=(),
    )
    payload.update(kw)
    probe = MaterializedCalibrationSettings.model_construct(**payload)
    return MaterializedCalibrationSettings(
        **payload,
        materialization_sha256=canonical_sha256(probe.semantic_payload()),
    )


def _deployment(**kw) -> CalibrationDeployment:
    payload = dict(
        document_id=DOC,
        calibration_plan_id='plan-1',
        calibration_plan_sha256=_SHA,
        export_id='exp-1',
        export_sha256=_SHA,
        materialization_id='mat-1',
        materialization_sha256=_SHA,
        binding_id='bind-1',
        binding_sha256=_SHA,
        adapter_id='htdt-generic-biquad',
        adapter_version='1',
        capability_declaration_sha256=_SHA,
        target_class='generic_peq_fir_file',
        deployed_at_utc=_TS,
    )
    payload.update(kw)
    return CalibrationDeployment.create(**payload)


def _verified_deployment(**kw) -> CalibrationDeployment:
    kw.setdefault('observed_snapshot_id', 'snap-1')
    kw.setdefault('observed_snapshot_sha256', _SHA)
    return _deployment(
        evidence_mode='machine_readback',
        deployment_state='deployment_verified',
        **kw,
    )


def _effectiveness(dep: CalibrationDeployment, **kw) -> DeploymentEffectivenessReport:
    payload = dict(
        document_id=DOC,
        deployment_ref=_dep_ref(dep),
        pre_measurement_refs=(_ref('measurement', 'm-pre'),),
        post_measurement_refs=(_ref('measurement', 'm-post'),),
        deltas=(
            EffectivenessMetricDelta(
                metric_id='seat_spl_deviation',
                before_value_repr='6.1 dB', after_value_repr='2.3 dB',
                outcome='improved',
            ),
        ),
        verdict='improvement_verified',
        evaluated_at_utc=_TS,
    )
    payload.update(kw)
    return DeploymentEffectivenessReport.create(**payload)


def _rollback(dep: CalibrationDeployment, **kw) -> DeploymentRollbackRecord:
    payload = dict(
        document_id=DOC,
        deployment_ref=_dep_ref(dep),
        rolled_back_at_utc=_TS,
        reason='filter reassigned to a different target',
        affected_evidence_refs=(_dep_ref(dep),),
    )
    payload.update(kw)
    return DeploymentRollbackRecord.create(**payload)


def _repo(tmp_path: Path) -> CadCalibrationDeploymentRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadCalibrationDeploymentRepository(SceneRepository(db))


# ---------------------------------------------------------------------------
# sealing + model validators


class TestSealing:
    def test_sealed_create(self) -> None:
        dep = _verified_deployment()
        assert dep.deployment_id.startswith('cald-')
        assert dep.deployment_id[5:] == dep.deployment_sha256[:24]
        assert dep.deployment_state == 'deployment_verified'
        assert dep.evidence_mode == 'machine_readback'

    def test_verified_requires_readback_mode(self) -> None:
        with pytest.raises(ValidationError):
            _deployment(
                deployment_state='deployment_verified',
                evidence_mode='operator_attestation',
                observed_snapshot_id='snap-1',
                observed_snapshot_sha256=_SHA,
            )

    def test_verified_requires_snapshot(self) -> None:
        with pytest.raises(ValidationError):
            _deployment(
                deployment_state='deployment_verified',
                evidence_mode='machine_readback',
            )

    def test_attested_requires_attestor(self) -> None:
        with pytest.raises(ValidationError):
            _deployment(
                deployment_state='deployment_attested',
                evidence_mode='operator_attestation',
                observed_snapshot_id='snap-1',
                observed_snapshot_sha256=_SHA,
            )

    def test_unverified_forbids_snapshot(self) -> None:
        with pytest.raises(ValidationError):
            _deployment(observed_snapshot_id='snap-1')

    def test_superseded_never_stored(self) -> None:
        with pytest.raises(ValidationError):
            _deployment(deployment_state='deployment_superseded')

    def test_forged_sha_rejected_at_model(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            CalibrationDeployment.model_validate({
                **dep.model_dump(mode='python'),
                'deployment_sha256': _SHA,
            })


# ---------------------------------------------------------------------------
# deployment gate


class TestDeploymentGate:
    def test_clean_materialization_deployable(self) -> None:
        verdict, reason = evaluate_deployment_gate(
            _materialization(), _declaration())
        assert verdict == 'deployable'
        assert reason == 'clean'

    def test_unsupported_items_fail_closed(self) -> None:
        verdict, reason = evaluate_deployment_gate(
            _materialization(unsupported_items=('peq:node7',)),
            _declaration())
        assert verdict == 'deployment_blocked'
        assert 'peq:node7' in reason

    def test_missing_declaration_blocks(self) -> None:
        verdict, reason = evaluate_deployment_gate(_materialization())
        assert verdict == 'deployment_blocked'
        assert reason == 'no_capability_declaration'

    def test_no_apply_support_blocks(self) -> None:
        verdict, _ = evaluate_deployment_gate(
            _materialization(), _declaration(supports_apply=False))
        assert verdict == 'deployment_blocked'

    def test_no_readback_still_deployable_but_never_verified(self) -> None:
        verdict, _ = evaluate_deployment_gate(
            _materialization(), _declaration(supports_read_back=False))
        assert verdict == 'deployable'
        state, mode = derive_deployment_state('operator_entered')
        assert state == 'deployment_attested'
        assert mode == 'operator_attestation'


# ---------------------------------------------------------------------------
# state derivation — evidence classes


class TestStateDerivation:
    def test_no_snapshot_is_unverified(self) -> None:
        state, mode = derive_deployment_state(None)
        assert (state, mode) == ('deployment_unverified', 'unverified_export')

    def test_ack_only_is_unverified(self) -> None:
        # an apply ack alone is never deployment evidence
        dep = _deployment(apply_ack_id='ack-1')
        assert dep.deployment_state == 'deployment_unverified'

    def test_readback_verifies(self) -> None:
        state, mode = derive_deployment_state('read_back')
        assert (state, mode) == ('deployment_verified', 'machine_readback')

    def test_operator_attestation_is_weaker(self) -> None:
        state, mode = derive_deployment_state('operator_entered')
        assert (state, mode) == ('deployment_attested', 'operator_attestation')

    def test_divergent_readback_is_mismatch(self) -> None:
        state, mode = derive_deployment_state('read_back', True)
        assert state == 'deployment_mismatch'
        assert mode == 'machine_readback'

    def test_divergent_attestation_is_mismatch(self) -> None:
        state, _ = derive_deployment_state('operator_entered', True)
        assert state == 'deployment_mismatch'


# ---------------------------------------------------------------------------
# effectiveness


class TestEffectiveness:
    def test_improvement_requires_post_measurement(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            _effectiveness(dep, post_measurement_refs=())

    def test_improvement_requires_improved_delta(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            _effectiveness(dep, deltas=())

    def test_regressed_delta_blocks_improvement_claim(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            _effectiveness(
                dep,
                deltas=(
                    EffectivenessMetricDelta(
                        metric_id='a', outcome='improved'),
                    EffectivenessMetricDelta(
                        metric_id='b', outcome='regressed'),
                ),
            )

    def test_unmeasured_forbids_post_refs(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            _effectiveness(dep, verdict='unmeasured')

    def test_evaluator_passes_through_on_verified_deployment(self) -> None:
        dep = _verified_deployment()
        report = _effectiveness(dep)
        verdict, reason = evaluate_deployment_effectiveness(dep, report)
        assert verdict == 'improvement_verified'
        assert 'improvement_verified' in reason

    def test_unverified_deployment_is_inconclusive(self) -> None:
        dep = _deployment()  # unverified_export
        report = _effectiveness(dep)
        verdict, _ = evaluate_deployment_effectiveness(dep, report)
        assert verdict == 'inconclusive'

    def test_mismatch_deployment_is_inconclusive(self) -> None:
        dep = _deployment(
            deployment_state='deployment_mismatch',
            evidence_mode='machine_readback',
            observed_snapshot_id='snap-1',
            observed_snapshot_sha256=_SHA,
        )
        report = _effectiveness(dep)
        verdict, _ = evaluate_deployment_effectiveness(dep, report)
        assert verdict == 'inconclusive'

    def test_no_report_is_unmeasured(self) -> None:
        dep = _verified_deployment()
        verdict, _ = evaluate_deployment_effectiveness(dep, None)
        assert verdict == 'unmeasured'

    def test_report_for_other_deployment_inconclusive(self) -> None:
        dep = _verified_deployment()
        other = _verified_deployment(calibration_plan_id='plan-2')
        report = _effectiveness(dep)
        verdict, _ = evaluate_deployment_effectiveness(other, report)
        assert verdict == 'inconclusive'


# ---------------------------------------------------------------------------
# rollback scoping


class TestRollback:
    def test_rollback_supersedes_only_its_deployment(self) -> None:
        dep = _verified_deployment()
        other = _verified_deployment(calibration_plan_id='plan-2')
        rb = _rollback(dep)
        assert deployment_state_with_rollbacks(dep, (rb,)) \
            == 'deployment_superseded'
        assert deployment_state_with_rollbacks(other, (rb,)) \
            == 'deployment_verified'

    def test_rollback_invalidates_only_bound_evidence(self) -> None:
        dep = _verified_deployment()
        rb = _rollback(dep, affected_evidence_refs=(
            _ref('measurement', 'm-bound'),
        ))
        assert rollback_invalidates_evidence(
            rb, _ref('measurement', 'm-bound'))
        assert not rollback_invalidates_evidence(
            rb, _ref('measurement', 'm-unrelated'))
        assert not rollback_invalidates_evidence(
            rb, _ref('room_geometry', 'room-1'))

    def test_replacement_ref_must_be_deployment(self) -> None:
        dep = _verified_deployment()
        with pytest.raises(ValidationError):
            _rollback(dep, replacement_deployment_ref=_ref('measurement'))

    def test_superseded_deployment_effectiveness_inconclusive(self) -> None:
        dep = _verified_deployment()
        rb = _rollback(dep)
        superseded = deployment_state_with_rollbacks(dep, (rb,))
        report = _effectiveness(dep)
        stale = dep.model_copy(
            update={'deployment_state': superseded},
        )
        verdict, _ = evaluate_deployment_effectiveness(stale, report)
        assert verdict == 'inconclusive'


# ---------------------------------------------------------------------------
# repository


class TestRepository:
    def test_roundtrip_all_stores(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        decl = _declaration()
        dep = _verified_deployment()
        report = _effectiveness(dep)
        rb = _rollback(dep)
        repo.save_capability_declaration(decl)
        repo.save_deployment(dep)
        repo.save_effectiveness_report(report)
        repo.save_rollback(rb)
        assert repo.get_capability_declaration(
            decl.declaration_id) == decl
        assert repo.get_deployment(dep.deployment_id) == dep
        assert repo.get_effectiveness_report(report.report_id) == report
        assert repo.get_rollback(rb.rollback_id) == rb
        assert repo.list_deployments(DOC) == (dep,)
        assert repo.list_deployments('other-doc') == ()

    def test_append_only_idempotent_resave(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        dep = _verified_deployment()
        repo.save_deployment(dep)
        repo.save_deployment(dep)  # identical sealed record is a no-op
        assert len(repo.list_deployments(DOC)) == 1
        # any record sharing the id but not the payload is impossible to
        # construct under sealing (id = sha[:24]); divergence is caught
        # by _assert_sealed before the append-only check.
        with pytest.raises(DeploymentIntegrityError):
            repo.save_deployment(
                CalibrationDeployment.model_construct(
                    **{**dep.model_dump(mode='python'),
                       'notes': 'tampered'}))

    def test_forged_sha_rejected_at_save(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        dep = _verified_deployment()
        forged = CalibrationDeployment.model_construct(
            **{**dep.model_dump(mode='python'),
               'deployment_sha256': _SHA})
        with pytest.raises(DeploymentIntegrityError):
            repo.save_deployment(forged)

    def test_column_tamper_detected_on_get(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        dep = _verified_deployment()
        repo.save_deployment(dep)
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_calibration_deployments '
                'SET deployment_state=? WHERE deployment_id=?',
                ('deployment_unverified', dep.deployment_id))
        with pytest.raises(DeploymentIntegrityError):
            repo.get_deployment(dep.deployment_id)

    def test_document_id_drift_detected_on_list(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        dep = _verified_deployment()
        repo.save_deployment(dep)
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_calibration_deployments '
                'SET document_id=? WHERE deployment_id=?',
                ('other-doc', dep.deployment_id))
        with pytest.raises(DeploymentIntegrityError):
            repo.list_deployments('other-doc')

    def test_effectiveness_roundtrip_by_document(
            self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        dep = _verified_deployment()
        report = _effectiveness(dep)
        repo.save_deployment(dep)
        repo.save_effectiveness_report(report)
        assert repo.list_effectiveness_reports(DOC) == (report,)
        assert repo.list_effectiveness_reports('other-doc') == ()
