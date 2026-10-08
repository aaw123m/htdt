"""#878: capability-negotiated deployment — common manifest, staged
pipeline transaction, strongest-path evaluation, AVR documented-LAN
adapter, Equalizer APO installed-identity verification, miniDSP
deployability registry and the assisted fallback contract.

Fail-closed coverage: unknown capability is unavailable, undeclared
control surfaces are never claimed, simulated transports are marked
non-production, evidence strength is structurally capped, and
machine-read-back evidence is never claimable from file writes.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationExportSnapshot,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_calibration_deployment import build_capability_declaration
from htdt.cad_commissioning_orchestrator import CommissioningOrchestrator
from htdt.cad_avr_lan_adapter import (
    AVR_LAN_ADAPTER_ID,
    AVR_LAN_DEVICE_FAMILY,
    AvrLanApplyError,
    AvrLanCalibrationAdapter,
    FakeAvrLanTransport,
)
from htdt.cad_camilladsp_deploy import CamillaDSPCalibrationAdapter
from htdt.cad_deployment_pipeline import (
    ApoInstallRecord,
    AssistedStepManifest,
    DeploymentPathCandidate,
    DeploymentPipelineService,
    EqualizerApoInstaller,
    MINIDSP_DEPLOYABILITY,
    PipelineAuthorizationError,
    PipelineStageError,
    UnknownMinidspModelError,
    build_assisted_attestation,
    build_assisted_manifest,
    build_operator_authorization,
    build_pipeline_record,
    derive_pipeline_stage,
    evaluate_deployment_path,
    minidsp_deploy_class,
    rank_deployment_paths,
    select_strongest_deployment_path,
    select_strongest_production_path,
)
from htdt.cad_deployment_pipeline_repository import (
    CadDeploymentPipelineRepository,
    DeploymentIntegrityError,
)
from htdt.cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    build_device_binding,
)
from htdt.cad_device_adapter_file import FileCalibrationAdapter
from htdt.cad_deployment_target import (
    UnknownTargetProfileError,
    get_dsp_target_profile,
)
from htdt.cad_equalizer_apo_export import (
    ApoExportChannel,
    render_equalizer_apo_config,
)
from htdt.cad_minidsp_export import MiniDSPBiquadExportAdapter
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256

NOW = '2026-10-08T00:00:00+00:00'
DOC = 'doc-878'
_SHA = 'f' * 64


def _channel(channel_id: str, gain_db: float) -> CadCalibrationChannel:
    return CadCalibrationChannel(
        channel_id=channel_id,
        role_id=channel_id.upper(),
        source_entity_id=f'spk-{channel_id}',
        physical_output_id=f'out-{channel_id}',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=0.0,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=(f'out-{channel_id}',),
    )


def _plan(
    channels: tuple[CadCalibrationChannel, ...] = (_channel('fl', 1.5),),
) -> CadCalibrationPlan:
    payload = {
        'plan_id': 'plan-878',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': DOC,
        'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000,
        'channels': channels,
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': CadDeviceCapabilityConstraints(
            capability_id='test-device-1',
            capability_version='1',
            supported_sample_rates_hz=(48000,),
            supported_filter_types=('peaking',),
            channel_gain_resolution_db=0.5,
        ),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': sha256(
                json.dumps(
                    provisional.semantic_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )


def _export(
    channels: tuple[CadCalibrationChannel, ...] = (_channel('fl', 1.5),),
) -> CadCalibrationExportSnapshot:
    return build_generic_biquad_export(plan=_plan(channels), created_at_utc=NOW)


def _avr_binding(
    routing: tuple[tuple[str, str], ...] = (('fl', 'out-fl'),),
    serial: str = 'localhost:23',
) -> AdapterDeviceBinding:
    return build_device_binding(
        adapter_id=AVR_LAN_ADAPTER_ID,
        device_family=AVR_LAN_DEVICE_FAMILY,
        device_model='AVR-X3800H',
        device_serial=serial,
        firmware_version='1.4.0',
        routing=routing,
        bound_at_utc=NOW,
    )


def _avr_adapter(
    transport: FakeAvrLanTransport | None = None,
    *,
    simulated: bool = True,
    approved: tuple[str, ...] = (),
) -> AvrLanCalibrationAdapter:
    transport = transport or FakeAvrLanTransport()
    return AvrLanCalibrationAdapter(
        {'localhost:23': transport, 'localhost': transport, **({
            'avr.local:23': transport, 'avr.local': transport,
        } if approved else {})},
        approved_remote_endpoints=approved,
        simulated=simulated,
    )


class _StubAvrTransport:
    """Canned-response transport — returns a fixed tuple for any query."""

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._responses = responses

    def send(self, command: str) -> None:
        pass

    def query(self, command: str) -> tuple[str, ...]:
        return self._responses


def _report(**kw) -> AdapterCapabilityReport:
    payload = dict(
        adapter_id='test-adapter',
        adapter_version='1',
        adapter_kind='network_api',
        device_family='test-family',
        supports_apply=False,
        supports_read_back=False,
    )
    payload.update(kw)
    return AdapterCapabilityReport(**payload)


def _repo(tmp_path: Path) -> CadDeploymentPipelineRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadDeploymentPipelineRepository(SceneRepository(db))


# ---------------------------------------------------------------------------
# common capability manifest


class TestCapabilityManifest:
    def test_camilladsp_report_publishes_manifest(self) -> None:
        adapter = CamillaDSPCalibrationAdapter({})
        report = adapter.capability()
        assert report.deploy_mechanism == 'machine_write'
        assert report.readback_mechanism == 'machine_exact'
        assert report.rollback_mechanism == 'previous_config'
        assert report.runtime_observation == 'telemetry'
        assert report.protocol_authority == 'open_source'
        assert 'peq' in report.supported_features

    def test_file_adapter_report_publishes_manifest(self) -> None:
        adapter = FileCalibrationAdapter(Path('/nonexistent'))
        report = adapter.capability()
        assert report.deploy_mechanism == 'file_export'
        assert report.readback_mechanism == 'operator_captured_file'
        assert report.protocol_authority == 'none'

    def test_minidsp_report_publishes_manifest(self) -> None:
        profile = get_dsp_target_profile(
            sorted(MINIDSP_DEPLOYABILITY)[0])
        adapter = MiniDSPBiquadExportAdapter(profile)
        report = adapter.capability()
        assert report.deploy_mechanism == 'file_export'
        assert report.readback_mechanism == 'none'
        assert report.rollback_mechanism == 'operator_only'
        assert report.applicability is not None
        assert 'miniDSP' in report.applicability

    def test_declaration_pins_manifest_verbatim(self) -> None:
        report = _report(
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            protocol_authority='documented',
            supported_features=('gain',),
            auth_requirements=('operator_confirmation',),
            applicability='test-api-v1',
        )
        declaration = build_capability_declaration(
            report, document_id=DOC, declared_at_utc=NOW)
        assert declaration.deploy_mechanism == 'machine_write'
        assert declaration.readback_mechanism == 'machine_exact'
        assert declaration.protocol_authority == 'documented'
        assert declaration.supported_features == ('gain',)
        assert declaration.declaration_id.startswith('dcd-')

    def test_defaults_stay_fail_closed(self) -> None:
        report = _report()
        assert report.deploy_mechanism == 'none'
        assert report.readback_mechanism == 'none'
        assert report.protocol_authority == 'unknown'


# ---------------------------------------------------------------------------
# strongest-path evaluator


class TestPathEvaluation:
    def _candidate(self, report: AdapterCapabilityReport, **kw):
        payload = dict(
            candidate_id=f'cand-{report.adapter_id}',
            adapter_id=report.adapter_id,
            target_ref='target-1',
            capability=report,
            reachability='confirmed',
        )
        payload.update(kw)
        return DeploymentPathCandidate(**payload)

    def test_machine_readback_is_strongest(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report(
            supports_apply=True,
            supports_read_back=True,
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            protocol_authority='documented',
        )))
        assert verdict.evidence_strength == 'machine_readback'
        assert verdict.production_eligible
        assert verdict.availability == 'available'

    def test_applied_ack_without_readback(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report(
            supports_apply=True,
            deploy_mechanism='machine_write',
            protocol_authority='documented',
        )))
        assert verdict.evidence_strength == 'applied_ack'

    def test_file_verified_lane(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report(
            deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file',
            protocol_authority='none',
        )))
        assert verdict.evidence_strength == 'file_verified'
        assert verdict.production_eligible  # file lanes are eligible

    def test_assisted_only_with_manifest(self) -> None:
        from htdt.cad_authority_resolver import AuthorityRef
        verdict = evaluate_deployment_path(self._candidate(
            _report(deploy_mechanism='manual_entry'),
            assisted_manifest_ref=AuthorityRef(
                kind='assisted_instruction_manifest',
                ref_id='aim-x', ref_sha256=_SHA)))
        assert verdict.evidence_strength == 'assisted_attestation'
        assert verdict.availability == 'available'

    def test_unknown_capability_unavailable(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report()))
        assert verdict.evidence_strength == 'none'
        assert verdict.availability == 'unavailable'

    def test_simulated_ranks_but_not_production(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report(
            adapter_kind='simulated',
            supports_apply=True,
            supports_read_back=True,
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            protocol_authority='simulated',
        )))
        assert verdict.evidence_strength == 'machine_readback'
        assert not verdict.production_eligible

    def test_unknown_reachability_is_conditional(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(
            _report(
                supports_apply=True, supports_read_back=True,
                deploy_mechanism='machine_write',
                readback_mechanism='machine_exact',
                protocol_authority='documented'),
            reachability='unknown'))
        assert verdict.availability == 'conditional'

    def test_unconfirmed_reachability_is_conditional(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(
            _report(
                supports_apply=True, supports_read_back=True,
                deploy_mechanism='machine_write',
                readback_mechanism='machine_exact',
                protocol_authority='documented'),
            reachability='unconfirmed'))
        assert verdict.availability == 'conditional'

    def test_simulated_file_lane_not_production(self) -> None:
        verdict = evaluate_deployment_path(self._candidate(_report(
            adapter_kind='simulated',
            deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file',
            protocol_authority='simulated')))
        assert verdict.evidence_strength == 'file_verified'
        assert not verdict.production_eligible

    def test_production_selection_skips_simulated_file(self) -> None:
        simulated_file = self._candidate(_report(
            adapter_id='sim-file', adapter_kind='simulated',
            deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file',
            protocol_authority='simulated'))
        real_file = self._candidate(_report(
            adapter_id='real-file',
            deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file',
            protocol_authority='none'))
        verdict = select_strongest_production_path(
            (simulated_file, real_file))
        assert verdict is not None
        assert verdict.candidate_id == 'cand-real-file'

    def test_ranking_order(self) -> None:
        from htdt.cad_authority_resolver import AuthorityRef
        machine = self._candidate(_report(
            adapter_id='machine', supports_apply=True,
            supports_read_back=True, deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            protocol_authority='documented'))
        file_lane = self._candidate(_report(
            adapter_id='file', deploy_mechanism='file_export',
            readback_mechanism='operator_captured_file'))
        assisted = self._candidate(
            _report(adapter_id='assist'),
            assisted_manifest_ref=AuthorityRef(
                kind='assisted_instruction_manifest',
                ref_id='aim-x', ref_sha256=_SHA))
        nothing = self._candidate(_report(adapter_id='none'))
        ranked = rank_deployment_paths((nothing, assisted, file_lane, machine))
        strengths = [v.evidence_strength for v in ranked]
        assert strengths == [
            'machine_readback', 'file_verified',
            'assisted_attestation', 'none',
        ]

    def test_select_fail_closed_when_none(self) -> None:
        assert select_strongest_deployment_path((
            self._candidate(_report(adapter_id='n1')),
            self._candidate(_report(adapter_id='n2')),
        )) is None

    def test_production_selection_skips_simulated(self) -> None:
        simulated = self._candidate(_report(
            adapter_id='sim', adapter_kind='simulated',
            supports_apply=True, supports_read_back=True,
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            protocol_authority='simulated'))
        real = self._candidate(_report(
            adapter_id='real', supports_apply=True,
            deploy_mechanism='machine_write',
            protocol_authority='documented'))
        verdict = select_strongest_production_path((simulated, real))
        assert verdict is not None
        assert verdict.candidate_id == 'cand-real'

    def test_orchestrator_delegates(self, tmp_path: Path) -> None:
        db = tmp_path / 'cad.sqlite3'
        ensure_native_schema(db)
        orchestrator = CommissioningOrchestrator(SceneRepository(db))
        candidates = (self._candidate(_report()),)
        assert orchestrator.evaluate_deployment_paths(candidates)[
            0].availability == 'unavailable'
        assert orchestrator.select_deployment_path(candidates) is None


# ---------------------------------------------------------------------------
# miniDSP deployability registry


class TestMinidspRegistry:
    def test_registry_covers_minidsp_profiles(self) -> None:
        assert len(MINIDSP_DEPLOYABILITY) > 15

    def test_every_current_model_is_assisted_only(self) -> None:
        for entry in MINIDSP_DEPLOYABILITY.values():
            assert entry.deploy_class == 'assisted_only'
            assert entry.machine_interface == 'none'
            assert entry.deployability_refs

    def test_lookup_by_exact_profile(self) -> None:
        profile_id = sorted(MINIDSP_DEPLOYABILITY)[0]
        assert minidsp_deploy_class(profile_id) == 'assisted_only'

    def test_unknown_profile_fails_closed(self) -> None:
        with pytest.raises(UnknownTargetProfileError):
            minidsp_deploy_class('dtp-does-not-exist')

    def test_machine_deployable_requires_interface(self) -> None:
        from htdt.cad_deployment_pipeline import MinidspModelDeployEntry
        with pytest.raises(ValidationError):
            MinidspModelDeployEntry(
                entry_id='mmr-x', entry_sha256=_SHA,
                profile_id='dtp-x', device_model='X',
                deploy_class='machine_deployable',
                machine_interface='none',
                deployability_refs=('doc',))


# ---------------------------------------------------------------------------
# AVR documented-LAN adapter


class TestAvrAdapter:
    def test_capability_marked_simulated_for_fake(self) -> None:
        report = _avr_adapter().capability()
        assert report.adapter_kind == 'simulated'
        assert report.protocol_authority == 'simulated'
        assert report.deploy_mechanism == 'machine_write'
        assert report.readback_mechanism == 'machine_exact'

    def test_capability_documented_for_real_transport(self) -> None:
        report = _avr_adapter(simulated=False).capability()
        assert report.adapter_kind == 'network_api'
        assert report.protocol_authority == 'documented'

    def test_materialize_renders_documented_cv_commands(self) -> None:
        export = _export((_channel('fl', 1.5), _channel('fr', -2.0)))
        binding = _avr_binding((('fl', 'o1'), ('fr', 'o2')))
        materialization = _avr_adapter().materialize(
            export, binding, created_at_utc=NOW)
        assert materialization.payload_text == 'CVFL 53\r\nCVFR 46'
        assert materialization.unsupported_items == ()

    def test_materialize_fail_closed_on_undeclared(self) -> None:
        channel = _channel('fl', 1.5).model_copy(update={
            'delay_s': 0.002, 'peq': (_fake_biquad(),),
        })
        export = _export((channel, _channel('mystery', 0.0)))
        binding = _avr_binding((('fl', 'o1'), ('mystery', 'o2')))
        materialization = _avr_adapter().materialize(
            export, binding, created_at_utc=NOW)
        problems = ' | '.join(materialization.unsupported_items)
        assert 'delay' in problems
        assert 'PEQ' in problems
        assert 'no documented AVR channel code' in problems

    def test_apply_and_readback_roundtrip(self) -> None:
        transport = FakeAvrLanTransport()
        adapter = _avr_adapter(transport)
        export = _export((_channel('fl', 1.5),))
        binding = _avr_binding()
        materialization = adapter.materialize(
            export, binding, created_at_utc=NOW)
        ack = adapter.apply(
            materialization, binding,
            operator_confirmed=True, applied_at_utc=NOW)
        assert ack.applied_units == ack.total_units == 1
        observed = adapter.read_back(binding, observed_at_utc=NOW)
        assert observed[0].gain_db == 1.5

    def test_apply_partial_write_raises_with_counts(self) -> None:
        transport = FakeAvrLanTransport(fail_on_send_index=1)
        adapter = _avr_adapter(transport)
        export = _export((_channel('fl', 1.0), _channel('fr', 2.0)))
        binding = _avr_binding((('fl', 'o1'), ('fr', 'o2')))
        materialization = adapter.materialize(
            export, binding, created_at_utc=NOW)
        with pytest.raises(AvrLanApplyError) as excinfo:
            adapter.apply(
                materialization, binding,
                operator_confirmed=True, applied_at_utc=NOW)
        assert excinfo.value.applied_units == 1
        assert excinfo.value.total_units == 2

    def test_apply_requires_operator_confirmation(self) -> None:
        adapter = _avr_adapter()
        materialization = adapter.materialize(
            _export(), _avr_binding(), created_at_utc=NOW)
        with pytest.raises(AdapterCapabilityError):
            adapter.apply(
                materialization, _avr_binding(),
                operator_confirmed=False, applied_at_utc=NOW)

    def test_remote_endpoint_requires_approval(self) -> None:
        adapter = _avr_adapter()  # no approval, remote endpoint
        binding = _avr_binding(serial='avr.local:23')
        with pytest.raises(AdapterCapabilityError):
            adapter.read_back(binding, observed_at_utc=NOW)

    def test_remote_endpoint_approved(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.5})
        adapter = _avr_adapter(transport, approved=('avr.local:23',))
        binding = _avr_binding(serial='avr.local:23')
        observed = adapter.read_back(binding, observed_at_utc=NOW)
        assert observed[0].gain_db == 0.5

    def test_baseline_and_verified_rollback(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        adapter = _avr_adapter(transport)
        binding = _avr_binding()
        payload, baseline_sha = adapter.capture_baseline(binding)
        assert baseline_sha == canonical_sha256(payload)
        export = _export((_channel('fl', 2.0),))
        materialization = adapter.materialize(
            export, binding, created_at_utc=NOW)
        adapter.apply(
            materialization, binding,
            operator_confirmed=True, applied_at_utc=NOW)
        result = adapter.rollback_previous(binding)
        assert result.outcome == 'restored_verified'
        assert transport.state['FL'] == 50

    def test_readback_rejects_prefix_collision(self) -> None:
        # 'CVFLX 50' must never be read as the FL trim — a garbage
        # response producing 'readback_matched' would fabricate
        # machine-read-back evidence.
        adapter = _avr_adapter(_StubAvrTransport(('CVFLX 50',)))
        with pytest.raises(AdapterCapabilityError):
            adapter.read_back(_avr_binding(), observed_at_utc=NOW)

    def test_readback_rejects_out_of_range_value(self) -> None:
        adapter = _avr_adapter(_StubAvrTransport(('CVFL 99',)))
        with pytest.raises(AdapterCapabilityError):
            adapter.read_back(_avr_binding(), observed_at_utc=NOW)

    def test_readback_rejects_non_numeric_response(self) -> None:
        adapter = _avr_adapter(_StubAvrTransport(('CVFL END',)))
        with pytest.raises(AdapterCapabilityError):
            adapter.read_back(_avr_binding(), observed_at_utc=NOW)

    def test_readback_skips_malformed_before_valid(self) -> None:
        adapter = _avr_adapter(
            _StubAvrTransport(('CVFL?', 'noise', 'CVFL 52')))
        observed = adapter.read_back(_avr_binding(), observed_at_utc=NOW)
        assert observed[0].gain_db == 1.0


def _fake_biquad():
    from htdt.cad_calibration import CadBiquadFilter
    return CadBiquadFilter.model_construct(
        filter_id='bq-1', filter_type='peaking', frequency_hz=80.0,
        gain_db=2.0, q=1.0, sample_rate_hz=48000,
        coefficients=(1.0, 0.0, 0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# staged pipeline transaction


class TestPipelineService:
    def _service(self, transport=None, **kw):
        adapter = _avr_adapter(transport, **kw)
        binding = _avr_binding()
        return DeploymentPipelineService(
            adapter, binding,
            target_ref='avr-target', document_id=DOC), adapter, binding

    def test_full_machine_readback_lifecycle(self) -> None:
        service, adapter, binding = self._service(
            FakeAvrLanTransport({'fl': 0.0}))
        record = service.open(at=NOW)
        assert record.stage == 'baseline_captured'
        assert record.baseline_source == 'machine_capture'
        service.compile(_export(), at=NOW)
        entries = service.preview(at=NOW)
        assert any(
            e.channel_id == 'fl' and e.field_name == 'gain_db'
            and e.before_repr == '0.0' and e.after_repr == '1.5'
            for e in entries)
        authorization = service.authorize(operator_id='op-1', at=NOW)
        assert authorization.scope == 'apply'
        ack = service.apply(authorization, at=NOW)
        assert ack.applied_units == 1
        snapshot = service.verify_readback(at=NOW)
        latest = service.latest
        assert latest is not None
        assert latest.stage == 'readback_matched'
        assert latest.evidence_strength == 'machine_readback'
        assert latest.readback_sha256 == snapshot.snapshot_sha256

    def test_readback_diverged_caps_evidence(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        service, adapter, binding = self._service(transport)
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        service.apply(service.authorize(operator_id='op-1', at=NOW), at=NOW)
        transport.state['FL'] = 51  # device diverges after apply
        service.verify_readback(at=NOW)
        assert service.latest.stage == 'readback_diverged'
        assert service.latest.evidence_strength == 'applied_ack'

    def test_partial_write_state(self) -> None:
        transport = FakeAvrLanTransport(fail_on_send_index=1)
        adapter = _avr_adapter(transport)
        binding = _avr_binding((('fl', 'o1'), ('fr', 'o2')))
        service = DeploymentPipelineService(
            adapter, binding, target_ref='avr', document_id=DOC)
        service.open(at=NOW)
        service.compile(
            _export((_channel('fl', 1.0), _channel('fr', 2.0))), at=NOW)
        service.preview(at=NOW)
        with pytest.raises(AvrLanApplyError):
            service.apply(
                service.authorize(operator_id='op-1', at=NOW), at=NOW)
        latest = service.latest
        assert latest.stage == 'partial_write'
        assert latest.partial_write == 'partial'
        assert latest.applied_units == 1 and latest.total_units == 2
        assert latest.evidence_strength == 'unverified'

    def test_stage_order_enforced(self) -> None:
        service, _adapter, _binding = self._service()
        with pytest.raises(PipelineStageError):
            service.compile(_export(), at=NOW)
        service.open(at=NOW)
        with pytest.raises(PipelineStageError):
            service.preview(at=NOW)

    def test_abort_never_masks_terminal_stage(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        service, _a, _b = self._service(transport)
        service.open(at=NOW)
        record = service.abort(at=NOW, reason='operator cancel')
        assert record.stage == 'aborted'
        # already-aborted runs are terminal
        with pytest.raises(PipelineStageError):
            service.abort(at=NOW, reason='again')

    def test_abort_rejected_after_verified_rollback(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        service, _a, _b = self._service(transport)
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        service.apply(service.authorize(operator_id='op-1', at=NOW), at=NOW)
        service.verify_readback(at=NOW)
        service.rollback(
            service.authorize(operator_id='op-1', scope='rollback', at=NOW),
            at=NOW)
        assert service.latest.stage == 'rollback_verified'
        with pytest.raises(PipelineStageError):
            service.abort(at=NOW, reason='late cancel')
        assert service.latest.stage == 'rollback_verified'

    def test_abort_rejected_after_failed_apply(self) -> None:
        transport = FakeAvrLanTransport(fail_on_send_index=0)
        service, _a, _b = self._service(transport)
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        with pytest.raises(AvrLanApplyError):
            service.apply(
                service.authorize(operator_id='op-1', at=NOW), at=NOW)
        assert service.latest.stage == 'failed'
        with pytest.raises(PipelineStageError):
            service.abort(at=NOW, reason='late cancel')

    def test_apply_without_authorization_fails(self) -> None:
        service, _a, _b = self._service()
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        bogus = build_operator_authorization(
            document_id=DOC, pipeline_id='dpl-other',
            scope='apply', operator_id='op-1', authorized_at_utc=NOW,
            candidate_materialization_sha256=_SHA)
        with pytest.raises(PipelineAuthorizationError):
            service.apply(bogus, at=NOW)

    def test_authorization_pins_materialization(self) -> None:
        service, _a, _b = self._service()
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        wrong = build_operator_authorization(
            document_id=DOC, pipeline_id=service.pipeline_id,
            scope='apply', operator_id='op-1', authorized_at_utc=NOW,
            candidate_materialization_sha256=_SHA)
        with pytest.raises(PipelineAuthorizationError):
            service.apply(wrong, at=NOW)

    def test_authorization_one_shot(self) -> None:
        service, _a, _b = self._service()
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        authorization = service.authorize(operator_id='op-1', at=NOW)
        service.apply(authorization, at=NOW)
        consumed = build_operator_authorization(
            document_id=DOC, pipeline_id=service.pipeline_id,
            scope='apply', operator_id='op-1', authorized_at_utc=NOW,
            candidate_materialization_sha256=(
                authorization.candidate_materialization_sha256),
            consumed=True, consumed_by_record_id='dplr-x')
        assert consumed.consumed
        with pytest.raises(PipelineAuthorizationError):
            service._assert_authorization(consumed, scope='apply', at=NOW)

    def test_expired_authorization_rejected_at_apply(self) -> None:
        service, _a, _b = self._service()
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        authorization = service.authorize(
            operator_id='op-1', at=NOW, expires_at_utc='2026-10-08T00:00:01+00:00')
        with pytest.raises(PipelineAuthorizationError):
            service.apply(authorization, at='2026-10-08T00:00:02+00:00')
        # a still-valid authorization remains acceptable
        service.apply(authorization, at='2026-10-08T00:00:00+00:00')

    def test_expired_authorization_rejected_at_rollback(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        service, _a, _b = self._service(transport)
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        service.apply(service.authorize(operator_id='op-1', at=NOW), at=NOW)
        rollback_auth = service.authorize(
            operator_id='op-1', scope='rollback', at=NOW,
            expires_at_utc='2026-10-08T00:00:01+00:00')
        with pytest.raises(PipelineAuthorizationError):
            service.rollback(
                rollback_auth, at='2026-10-08T00:00:02+00:00')

    def test_expiry_before_authorized_rejected_at_mint(self) -> None:
        with pytest.raises(ValidationError):
            build_operator_authorization(
                document_id=DOC, pipeline_id='dpl-1', scope='rollback',
                operator_id='op-1', authorized_at_utc=NOW,
                expires_at_utc=NOW)

    def test_rollback_verified(self) -> None:
        transport = FakeAvrLanTransport({'fl': 0.0})
        service, adapter, binding = self._service(transport)
        service.open(at=NOW)
        service.compile(_export(), at=NOW)
        service.preview(at=NOW)
        service.apply(service.authorize(operator_id='op-1', at=NOW), at=NOW)
        service.verify_readback(at=NOW)
        rollback_auth = service.authorize(
            operator_id='op-1', scope='rollback', at=NOW)
        record = service.rollback(rollback_auth, at=NOW)
        assert record.stage == 'rollback_verified'
        assert record.rollback_outcome == 'verified'
        assert transport.state['FL'] == 50

    def test_baseline_unavailable_path(self) -> None:
        adapter = FileCalibrationAdapter(Path('/nonexistent'))
        binding = build_device_binding(
            adapter_id=adapter.capability().adapter_id,
            device_family='generic', device_model='x',
            device_serial='n/a', firmware_version='1',
            routing=(), bound_at_utc=NOW)
        service = DeploymentPipelineService(
            adapter, binding, target_ref='file', document_id=DOC)
        record = service.open(at=NOW)
        assert record.stage == 'baseline_unavailable'
        assert record.baseline_sha256 is None

    def test_records_persisted_when_repository(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        adapter = _avr_adapter(FakeAvrLanTransport({'fl': 0.0}))
        service = DeploymentPipelineService(
            adapter, _avr_binding(), target_ref='avr',
            document_id=DOC, repository=repo)
        service.open(at=NOW)
        assert len(repo.list_pipeline_records(DOC)) == 1


# ---------------------------------------------------------------------------
# assisted fallback contract


class TestAssistedContract:
    def _manifest(self):
        return build_assisted_manifest(
            document_id=DOC, target_ref='minidsp-2x4hd',
            title='manual import via Device Console',
            steps=(
                AssistedStepManifest(
                    step_index=1, action='produce_artifact',
                    instruction='export biquad file'),
                AssistedStepManifest(
                    step_index=2, action='import_file',
                    instruction='import in Device Console'),
                AssistedStepManifest(
                    step_index=3, action='confirm',
                    instruction='confirm applied preset'),
            ))

    def test_manifest_seals_ordered_steps(self) -> None:
        manifest = self._manifest()
        assert manifest.manifest_id.startswith('aim-')
        assert manifest.post_measurement_required

    def test_manifest_rejects_unordered_steps(self) -> None:
        from htdt.cad_deployment_pipeline import (
            AssistedInstructionManifest)
        with pytest.raises(ValidationError):
            AssistedInstructionManifest(
                manifest_id='aim-x', manifest_sha256=_SHA,
                document_id=DOC, target_ref='t', title='x',
                steps=(
                    AssistedStepManifest(
                        step_index=2, action='confirm', instruction='b'),
                    AssistedStepManifest(
                        step_index=1, action='confirm', instruction='a'),
                ))

    def test_attestation_requires_all_steps_for_attested(self) -> None:
        manifest = self._manifest()
        with pytest.raises(Exception):
            build_assisted_attestation(
                document_id=DOC, manifest=manifest,
                operator_attestor='op-1', attested_at_utc=NOW,
                completed_step_indexes=(1, 2), outcome='attested')

    def test_attestation_rejects_undeclared_steps(self) -> None:
        manifest = self._manifest()
        with pytest.raises(Exception):
            build_assisted_attestation(
                document_id=DOC, manifest=manifest,
                operator_attestor='op-1', attested_at_utc=NOW,
                completed_step_indexes=(1, 2, 3, 9), outcome='attested')

    def test_attested_carries_weaker_evidence(self) -> None:
        attestation = build_assisted_attestation(
            document_id=DOC, manifest=self._manifest(),
            operator_attestor='op-1', attested_at_utc=NOW,
            completed_step_indexes=(1, 2, 3), outcome='attested')
        assert attestation.evidence_strength == 'assisted_attestation'
        assert attestation.post_measurement_required
        assert attestation.manifest_ref.ref_id.startswith('aim-')

    def test_partial_attestation_allowed(self) -> None:
        attestation = build_assisted_attestation(
            document_id=DOC, manifest=self._manifest(),
            operator_attestor='op-1', attested_at_utc=NOW,
            completed_step_indexes=(1,), outcome='partial')
        assert attestation.outcome == 'partial'


# ---------------------------------------------------------------------------
# Equalizer APO installed-identity verification


class TestApoInstaller:
    def _rendered(self) -> tuple[str, tuple[ApoExportChannel, ...]]:
        channels = (
            ApoExportChannel(
                channel_labels=('L',), preamp_db=-1.0,
                bands=()),
        )
        return render_equalizer_apo_config(channels=channels), channels

    def test_install_verified_content_sha(self, tmp_path: Path) -> None:
        rendered, channels = self._rendered()
        record = EqualizerApoInstaller().install(
            document_id=DOC, rendered_text=rendered,
            target_path=tmp_path / 'apo.txt', created_at_utc=NOW,
            channels=channels)
        assert record.verification == 'content_sha_matched'
        assert record.installed_sha256 == record.rendered_sha256
        assert record.semantic_verdict == 'matched'
        assert record.evidence_strength == 'file_verified'

    def test_file_written_is_not_identity(self, tmp_path: Path) -> None:
        rendered, _channels = self._rendered()
        target = tmp_path / 'apo.txt'
        record = EqualizerApoInstaller().install(
            document_id=DOC, rendered_text=rendered,
            target_path=target, created_at_utc=NOW)
        assert record.verification == 'content_sha_matched'
        assert record.semantic_verdict == 'not_checked'
        # a foreign write afterwards is detectable through the sha pin
        target.write_text('tampered', encoding='utf-8')
        assert record.rendered_sha256 != sha256(b'tampered').hexdigest()

    def test_install_failure_recorded(self, tmp_path: Path) -> None:
        record = EqualizerApoInstaller().install(
            document_id=DOC, rendered_text='x',
            target_path=tmp_path / 'missing-dir' / 'apo.txt',
            created_at_utc=NOW)
        assert record.verification == 'install_failed'
        assert record.evidence_strength == 'none'

    def test_install_record_sealed(self, tmp_path: Path) -> None:
        rendered, _ = self._rendered()
        record = EqualizerApoInstaller().install(
            document_id=DOC, rendered_text=rendered,
            target_path=tmp_path / 'apo.txt', created_at_utc=NOW)
        assert record.record_id.startswith('eap-')
        assert record.record_id[4:] == record.record_sha256[:24]


# ---------------------------------------------------------------------------
# sealing + integrity


class TestSealing:
    def _record(self, **kw) -> object:
        payload = dict(
            pipeline_id='dpl-x', document_id=DOC,
            binding_sha256=_SHA, adapter_id='a', target_ref='t',
            stage='applied', created_at_utc=NOW, ack_id='ack-1',
        )
        payload.update(kw)
        return build_pipeline_record(**payload)

    def test_seal_and_id_from_sha(self) -> None:
        record = self._record()
        assert record.record_id.startswith('dplr-')
        assert record.record_id[5:] == record.record_sha256[:24]

    def test_evidence_overclaim_rejected(self) -> None:
        with pytest.raises(ValidationError):
            # applied-ack evidence fields but claiming machine read-back
            from htdt.cad_deployment_pipeline import DeploymentPipelineRecord
            record = self._record()
            DeploymentPipelineRecord.model_validate({
                **record.model_dump(mode='python'),
                'evidence_strength': 'machine_readback',
            })

    def test_machine_readback_requires_machine_mechanism(self) -> None:
        # file-mechanism matched read-back can only reach file_verified
        record = self._record(
            readback_mechanism='operator_captured_file',
            readback_sha256=_SHA, readback_verdict='matched')
        assert record.evidence_strength == 'file_verified'

    def test_authorization_scope_enforced(self) -> None:
        with pytest.raises(ValidationError):
            build_operator_authorization(
                document_id=DOC, pipeline_id='p', scope='apply',
                operator_id='op', authorized_at_utc=NOW)  # no pin

    def test_stage_derivation_from_chain(self) -> None:
        record = self._record()
        assert derive_pipeline_stage((record,)) == 'applied'
        assert derive_pipeline_stage(()) == 'opened'


# ---------------------------------------------------------------------------
# repository round-trip + tamper detection


class TestRepository:
    def test_pipeline_record_roundtrip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        record = build_pipeline_record(
            pipeline_id='dpl-1', document_id=DOC,
            binding_sha256=_SHA, adapter_id='a', target_ref='t',
            stage='readback_matched', created_at_utc=NOW,
            readback_mechanism='machine_exact', readback_sha256=_SHA,
            readback_verdict='matched', ack_id='ack-1')
        repo.save_pipeline_record(record)
        loaded = repo.get_pipeline_record(record.record_id)
        assert loaded == record
        assert repo.list_pipeline_records_for_pipeline('dpl-1') == (record,)

    def test_authorization_roundtrip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        authorization = build_operator_authorization(
            document_id=DOC, pipeline_id='dpl-1', scope='apply',
            operator_id='op-1', authorized_at_utc=NOW,
            candidate_materialization_sha256=_SHA)
        repo.save_operator_authorization(authorization)
        assert repo.get_operator_authorization(
            authorization.authorization_id) == authorization

    def test_manifest_and_attestation_roundtrip(
            self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        manifest = build_assisted_manifest(
            document_id=DOC, target_ref='t', title='x',
            steps=(AssistedStepManifest(
                step_index=1, action='confirm', instruction='do it'),))
        repo.save_assisted_manifest(manifest)
        attestation = build_assisted_attestation(
            document_id=DOC, manifest=manifest,
            operator_attestor='op-1', attested_at_utc=NOW,
            completed_step_indexes=(1,), outcome='attested')
        repo.save_assisted_attestation(attestation)
        assert repo.get_assisted_manifest(manifest.manifest_id) == manifest
        assert repo.get_assisted_attestation(
            attestation.attestation_id) == attestation

    def test_apo_install_roundtrip_and_tamper(
            self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        rendered, _ = TestApoInstaller()._rendered()
        record = EqualizerApoInstaller().install(
            document_id=DOC, rendered_text=rendered,
            target_path=tmp_path / 'apo.txt', created_at_utc=NOW)
        repo.save_apo_install(record)
        assert repo.get_apo_install(record.record_id) == record
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_apo_install_records SET verification=? '
                'WHERE record_id=?', ('content_sha_mismatch', record.record_id))
        with pytest.raises(DeploymentIntegrityError):
            repo.get_apo_install(record.record_id)

    def test_pipeline_column_tamper_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        record = build_pipeline_record(
            pipeline_id='dpl-1', document_id=DOC,
            binding_sha256=_SHA, adapter_id='a', target_ref='t',
            stage='applied', created_at_utc=NOW, ack_id='ack-1')
        repo.save_pipeline_record(record)
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_deployment_pipeline_records '
                'SET evidence_strength=? WHERE record_id=?',
                ('machine_readback', record.record_id))
        with pytest.raises(DeploymentIntegrityError):
            repo.get_pipeline_record(record.record_id)

    def test_append_only_resave(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        record = build_pipeline_record(
            pipeline_id='dpl-1', document_id=DOC,
            binding_sha256=_SHA, adapter_id='a', target_ref='t',
            stage='applied', created_at_utc=NOW, ack_id='ack-1')
        repo.save_pipeline_record(record)
        repo.save_pipeline_record(record)
        assert len(repo.list_pipeline_records(DOC)) == 1
