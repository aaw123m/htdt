"""#838 slice B — CamillaDSP deploy/read-back/rollback + miniDSP target registry.

Machine-verifiable deploy (CamillaDSP WebSocket) and manual file handoff
(miniDSP biquad text) land as different evidence strengths:
read-back can reach deployment_verified, operator import attestation can
never — and runtime telemetry stays distinct from acoustic verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadCalibrationExportSnapshot,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    build_biquad_filter,
    build_generic_biquad_export,
)
from htdt.cad_calibration_deployment import (
    CalibrationDeployment,
    DeploymentCapabilityDeclaration,
    DeploymentEffectivenessReport,
    EffectivenessMetricDelta,
    derive_deployment_state,
    evaluate_deployment_gate,
)
from htdt.cad_camilladsp import FixtureCamillaDSPTransport
from htdt.cad_camilladsp_deploy import (
    CAMILLADSP_DEPLOY_ADAPTER_ID,
    CamillaDSPCalibrationAdapter,
    CamillaDSPDeploymentSession,
    CamillaDSPEndpointError,
    CamillaDSPRollbackEvidence,
    CamillaDSPRuntimeObservation,
    compile_camilladsp_config,
    config_sha256,
    derive_camilladsp_stage,
    normalize_camilladsp_config,
)
from htdt.cad_camilladsp_deployment_repository import (
    CadCamillaDSPDeploymentRepository,
    DeploymentConflictError,
    DeploymentIntegrityError,
)
from htdt.cad_deployment_target import (
    DSPTargetProfile,
    DSPTargetTopology,
    UnknownTargetProfileError,
    evaluate_export_target_fit,
    find_dsp_target_profile,
    get_dsp_target_profile,
    list_dsp_target_profiles,
)
from htdt.cad_device_adapter import (
    AdapterCapabilityError,
    CalibrationAdapterService,
    _hash,
)
from htdt.cad_device_adapter import build_device_binding
from htdt.cad_minidsp_export import (
    MINIDSP_EXPORT_ADAPTER_ID,
    MiniDSPBiquadExportAdapter,
    derive_minidsp_handoff_stage,
)
from htdt.cad_repository import SceneRepository
from htdt.canonical_json import canonical_json, canonical_sha256


NOW = '2026-10-07T00:00:00Z'
DOC = 'doc-838'
_SHA = canonical_sha256({'fixture': 'sha'})
ENDPOINT = 'camilladsp://127.0.0.1:1234'


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-838',
        capability_version='1',
        supported_sample_rates_hz=(48000, 96000),
        supported_filter_types=('peaking', 'low_pass', 'high_pass'),
        channel_gain_resolution_db=0.5,
    )


def _plan(*, rate: int = 48000, peq=(), crossovers=(), delay_s=0.0,
          gain_db=1.0, polarity='normal') -> CadCalibrationPlan:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='0',
        sample_rate_hz=rate,
        gain_db=gain_db,
        delay_s=delay_s,
        polarity=polarity,
        crossovers=crossovers,
        peq=peq,
        routing=('0',),
    )
    payload = {
        'plan_id': 'plan-838',
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
        'sample_rate_hz': rate,
        'channels': (channel,),
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': _constraints(),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': canonical_sha256(
                provisional.semantic_payload()
            ),
        }
    )


def _peq() -> tuple:
    return (
        build_biquad_filter(
            filter_id='peq-1',
            filter_type='peaking',
            frequency_hz=105.0,
            q=1.1,
            gain_db=-4.5,
            sample_rate_hz=48000,
        ),
        build_biquad_filter(
            filter_id='peq-2',
            filter_type='peaking',
            frequency_hz=3200.0,
            q=2.0,
            gain_db=2.0,
            sample_rate_hz=48000,
        ),
    )


def _export(**plan_kw) -> CadCalibrationExportSnapshot:
    return build_generic_biquad_export(
        plan=_plan(**plan_kw), created_at_utc=NOW)


def _camilladsp_binding(routing=(('ch-1', '0'),)) -> object:
    return build_device_binding(
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        binding_id='adb-test-camilladsp',
        device_family='camilladsp',
        device_model='CamillaDSP',
        device_serial=ENDPOINT,
        firmware_version='3.0.0',
        routing=routing,
        bound_at_utc=NOW,
    )


def _minidsp_binding(routing=(('ch-1', 'out-1'),)) -> object:
    return build_device_binding(
        adapter_id=MINIDSP_EXPORT_ADAPTER_ID,
        binding_id='adb-test-minidsp',
        device_family='minidsp',
        device_model='miniDSP 2x4 HD',
        device_serial='MS-838',
        firmware_version='1.0',
        routing=routing,
        bound_at_utc=NOW,
    )


def _adapter(
    config: dict | None = None, **fixture_kw
) -> tuple[CamillaDSPCalibrationAdapter, FixtureCamillaDSPTransport]:
    transport = FixtureCamillaDSPTransport(
        config=config if config is not None else {
            'devices': {'samplerate': 48000, 'playback': {'channels': 2}},
        },
        **fixture_kw,
    )
    return CamillaDSPCalibrationAdapter({ENDPOINT: transport}), transport


def _declaration(**kw) -> DeploymentCapabilityDeclaration:
    payload = dict(
        document_id=DOC,
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        adapter_version='1',
        adapter_kind='network_api',
        device_family='camilladsp',
        supports_apply=True,
        supports_read_back=True,
        supports_materialization=True,
        supported_features=(
            'peq', 'gain', 'delay', 'polarity', 'crossover', 'routing'),
        declared_limit_notes=(),
        declared_at_utc=NOW,
    )
    payload.update(kw)
    return DeploymentCapabilityDeclaration.create(**payload)


def _deployment(**kw) -> CalibrationDeployment:
    declaration = _declaration()
    payload = dict(
        document_id=DOC,
        calibration_plan_id='plan-838',
        calibration_plan_sha256='a' * 64,
        export_id='exp-1',
        export_sha256='b' * 64,
        materialization_id='mat:1',
        materialization_sha256='c' * 64,
        binding_id='bind-1',
        binding_sha256='d' * 64,
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        adapter_version='1',
        capability_declaration_sha256=declaration.declaration_sha256,
        target_class='camilladsp_device',
        deployed_at_utc=NOW,
    )
    payload.update(kw)
    return CalibrationDeployment.create(**payload)


def _dep_ref(dep: CalibrationDeployment) -> AuthorityRef:
    return AuthorityRef(
        kind='calibration_deployment',
        ref_id=dep.deployment_id,
        ref_sha256=dep.deployment_sha256,
    )


class _CalibrationAuthority:
    """Minimal persisted-authority stub for observation document
    resolution (#865) — the same shape test_cad_device_adapter uses."""

    def __init__(self, plan, export) -> None:
        self._plan = plan
        self._export = export

    def get_export(self, export_id):
        return self._export if export_id == self._export.export_id else None

    def get_plan(self, plan_id):
        return self._plan if plan_id == self._plan.plan_id else None


# -- target registry ---------------------------------------------------------


def test_registry_has_exact_minidsp_profiles() -> None:
    profiles = list_dsp_target_profiles(
        target_family='minidsp_biquad_export')
    models = {p.device_model for p in profiles}
    assert {
        'miniDSP 2x4', 'miniDSP 2x4 HD', 'miniDSP 4x10 HD',
        'miniDSP C-DSP 8x12', 'miniDSP C-DSP 8x12 DL',
        'miniDSP DDRC-24', 'miniDSP SHD Series', 'miniDSP nanoAVR',
    } <= models


def test_minidsp_profiles_pin_exact_rate_and_peq() -> None:
    profile_2x4 = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    profile_2x4hd = find_dsp_target_profile('minidsp', 'miniDSP 2x4 HD')
    assert profile_2x4.supported_sample_rates_hz == (48000,)
    assert profile_2x4.topology.peq_slots_per_output == 6
    assert profile_2x4hd.supported_sample_rates_hz == (96000,)
    assert profile_2x4hd.topology.peq_slots_per_output == 10
    assert (
        profile_2x4.biquad_coefficient_convention
        == 'denominator=1-a1*z^-1-a2*z^-2'
    )


def test_flex_dirac_and_non_dirac_are_distinct_profiles() -> None:
    plain = find_dsp_target_profile(
        'minidsp', 'miniDSP Flex', 'non_dirac')
    dirac = find_dsp_target_profile(
        'minidsp', 'miniDSP Flex', 'dirac')
    assert plain.profile_id != dirac.profile_id
    assert plain.supported_sample_rates_hz == (96000,)
    assert dirac.supported_sample_rates_hz == (48000,)


def test_camilladsp_profile_declares_machine_mechanisms() -> None:
    profiles = list_dsp_target_profiles(
        target_family='camilladsp_websocket')
    (profile,) = profiles
    assert profile.deploy_mechanism == 'machine_write'
    assert profile.readback_mechanism == 'machine_exact'
    assert profile.runtime_attestation == 'telemetry'
    assert profile.rollback_mechanism == 'previous_config'


def test_minidsp_profiles_declare_file_handoff_only() -> None:
    for profile in list_dsp_target_profiles(
            target_family='minidsp_biquad_export'):
        assert profile.deploy_mechanism == 'file_export'
        assert profile.readback_mechanism == 'none'
        assert profile.runtime_attestation == 'none'
        assert profile.rollback_mechanism == 'operator_only'


def test_profile_lookup_fails_closed() -> None:
    with pytest.raises(UnknownTargetProfileError):
        get_dsp_target_profile('dtp-does-not-exist')


def test_profile_requires_pinned_sources() -> None:
    with pytest.raises(ValidationError):
        DSPTargetProfile.create(
            target_family='minidsp_biquad_export',
            device_family='minidsp',
            device_model='miniDSP 2x4',
            profile_variant=None,
            profile_revision='test',
            supported_sample_rates_hz=(48000,),
            supported_filter_classes=('peq',),
            topology=DSPTargetTopology(peq_slots_per_output=6),
            biquad_coefficient_convention=(
                'denominator=1-a1*z^-1-a2*z^-2'),
            deploy_mechanism='file_export',
            readback_mechanism='none',
            runtime_attestation='none',
            rollback_mechanism='operator_only',
            source_refs=(),
        )


def test_profiles_are_sealed_and_stable() -> None:
    profile = get_dsp_target_profile(
        find_dsp_target_profile('minidsp', 'miniDSP 2x4 HD').profile_id)
    assert profile.profile_id.startswith('dtp-')
    assert profile.profile_sha256 == canonical_sha256(
        profile.identity_payload())
    with pytest.raises(ValidationError):
        DSPTargetProfile(
            **{**profile.model_dump(), 'profile_sha256': '0' * 64})


# -- fit evaluation ----------------------------------------------------------


def test_fit_passes_within_profile() -> None:
    # 2x4 original: 48 kHz / 6 PEQ — matches the fixture export.
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    verdict, reasons = evaluate_export_target_fit(
        _export(gain_db=0.0, peq=_peq()), profile)
    assert verdict == 'fits'
    assert reasons == ()


def test_fit_rejects_wrong_sample_rate() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4 HD')
    verdict, reasons = evaluate_export_target_fit(
        _export(rate=44100, gain_db=0.0), profile)
    assert verdict == 'rate_unsupported'
    assert any('44100' in reason for reason in reasons)


def test_fit_rejects_peq_overflow() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    many = tuple(
        build_biquad_filter(
            filter_id=f'peq-{i}', filter_type='peaking',
            frequency_hz=100.0 * (i + 1), q=1.0, gain_db=-1.0,
            sample_rate_hz=48000)
        for i in range(7)
    )
    verdict, reasons = evaluate_export_target_fit(
        _export(gain_db=0.0, peq=many), profile)
    assert verdict == 'exceeds_profile'
    assert any('peq_count' in reason for reason in reasons)


def test_fit_fails_closed_on_undeclared_classes() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4 HD')
    verdict, _ = evaluate_export_target_fit(
        _export(delay_s=0.001), profile)
    assert verdict == 'unsupported_parameters'
    verdict, _ = evaluate_export_target_fit(
        _export(
            crossovers=(CadCrossoverSetting(
                crossover_type='low_pass', frequency_hz=80.0,
                filter_order=2),)),
        profile)
    assert verdict == 'unsupported_parameters'


# -- miniDSP export adapter ---------------------------------------------------


def test_minidsp_adapter_requires_exact_profile() -> None:
    generic = list_dsp_target_profiles(
        target_family='generic_peq_fir_file')[0]
    with pytest.raises(ValueError, match='minidsp_biquad_export'):
        MiniDSPBiquadExportAdapter(generic)


def test_minidsp_materialize_renders_sign_flipped_biquads() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    adapter = MiniDSPBiquadExportAdapter(profile)
    export = _export(gain_db=0.0, peq=_peq())
    materialization = adapter.materialize(
        export, _minidsp_binding(), created_at_utc=NOW)
    files = json.loads(materialization.payload_text)['files']
    text = files['ch-1']
    assert 'biquad1,' in text
    assert 'b0=' in text and 'a2=' in text
    b0, b1, b2, a1, a2 = export.channels[0].peq[0].coefficients
    assert f'b0={b0!r}' in text
    # miniDSP convention negates a1/a2 relative to the HTDT denominator.
    assert f'a1={-a1!r}' in text
    assert f'a2={-a2!r}' in text
    assert materialization.unsupported_items == ()


def test_minidsp_materialize_fails_closed_on_unrepresentable() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    adapter = MiniDSPBiquadExportAdapter(profile)
    export = _export(delay_s=0.002, polarity='inverted')
    materialization = adapter.materialize(
        export, _minidsp_binding(), created_at_utc=NOW)
    assert materialization.unsupported_items
    assert any('delay' in item for item in materialization.unsupported_items)
    assert any(
        'polarity' in item for item in materialization.unsupported_items)
    # The #806 gate must block this materialization.
    verdict, reason = evaluate_deployment_gate(
        materialization, _declaration(
            adapter_id=MINIDSP_EXPORT_ADAPTER_ID,
            adapter_kind='offline_file', device_family='minidsp',
            supports_apply=False, supports_read_back=False,
            supported_features=('peq',)))
    assert verdict == 'deployment_blocked'


def test_minidsp_apply_and_readback_never_claimed() -> None:
    profile = find_dsp_target_profile('minidsp', 'miniDSP 2x4')
    adapter = MiniDSPBiquadExportAdapter(profile)
    binding = _minidsp_binding()
    materialization = adapter.materialize(
        _export(), binding, created_at_utc=NOW)
    with pytest.raises(AdapterCapabilityError):
        adapter.apply(
            materialization, binding,
            operator_confirmed=True, applied_at_utc=NOW)
    with pytest.raises(AdapterCapabilityError):
        adapter.read_back(binding, observed_at_utc=NOW)
    report = adapter.capability()
    assert report.supports_apply is False
    assert report.supports_read_back is False


def test_minidsp_handoff_stages() -> None:
    # No deployment: device state unknown.
    assert derive_minidsp_handoff_stage(None) == 'device_state_unknown'
    unverified = _deployment(
        target_class='minidsp_file_export',
        deployment_state='deployment_unverified',
        evidence_mode='unverified_export')
    assert derive_minidsp_handoff_stage(unverified) == (
        'exported_for_minidsp')
    attested = _deployment(
        target_class='minidsp_file_export',
        deployment_state='deployment_attested',
        evidence_mode='operator_attestation',
        observed_snapshot_id='snap-1',
        observed_snapshot_sha256='f' * 64,
        operator_attestor='op-1')
    assert derive_minidsp_handoff_stage(attested) == (
        'operator_import_attested')
    report = DeploymentEffectivenessReport.create(
        document_id=DOC,
        deployment_ref=_dep_ref(attested),
        post_measurement_refs=(
            AuthorityRef(kind='measurement', ref_id='m-2',
                         ref_sha256='a' * 64),),
        deltas=(EffectivenessMetricDelta(
            metric_id='m1', before_value_repr='-12',
            after_value_repr='-3', outcome='improved'),),
        verdict='improvement_verified',
        evaluated_at_utc=NOW,
    )
    assert derive_minidsp_handoff_stage(attested, report) == (
        'post_measurement_verified')


# -- CamillaDSP compile/materialize -------------------------------------------


def test_materialize_compiles_owned_region() -> None:
    adapter, transport = _adapter()
    export = _export(peq=_peq(), delay_s=0.0015)
    binding = _camilladsp_binding()
    materialization = adapter.materialize(
        export, binding, created_at_utc=NOW)
    config = json.loads(materialization.payload_text)
    filters = config['filters']
    tag = export.exported_settings_semantic_sha256[:12]
    prefix = f'htdt_{tag}_ch-1'
    gain = filters[f'{prefix}_gain']
    assert gain['type'] == 'Gain'
    assert gain['parameters']['gain'] == export.channels[0].gain_db
    delay = filters[f'{prefix}_delay']
    assert delay['parameters']['unit'] == 'ms'
    assert delay['parameters']['delay'] == pytest.approx(1.5)
    peq1 = filters[f'{prefix}_peq1']
    assert peq1['type'] == 'Biquad'
    assert peq1['parameters']['type'] == 'Peaking'
    assert peq1['parameters']['freq'] == 105.0
    assert peq1['parameters']['gain'] == -4.5
    assert config['devices']['playback']['channels'] == 2
    assert materialization.unsupported_items == ()
    # Pipeline step targets the routed output index.
    step = config['pipeline'][-1]
    assert step['type'] == 'Filter'
    assert step['channels'] == [0]
    # The description pins channel identity for read-back normalization.
    blob = json.loads(config['description'])['htdt-deployment']
    assert blob['export_id'] == export.export_id
    assert blob['channels'][0]['channel_id'] == 'ch-1'


def test_materialize_deterministic() -> None:
    adapter, _ = _adapter()
    export = _export(peq=_peq())
    binding = _camilladsp_binding()
    first = adapter.materialize(export, binding, created_at_utc=NOW)
    second = adapter.materialize(export, binding, created_at_utc=NOW)
    assert first.payload_text == second.payload_text
    assert first.materialization_sha256 == second.materialization_sha256


def test_compile_fails_closed_on_rate_mismatch_and_unrouted() -> None:
    export = _export(peq=_peq())
    config, _notes, unsupported = compile_camilladsp_config(
        export, _camilladsp_binding(),
        {'devices': {'samplerate': 96000, 'playback': {'channels': 2}}})
    assert any('samplerate' in item for item in unsupported)
    config, _notes, unsupported = compile_camilladsp_config(
        export, _camilladsp_binding(routing=()),
        {'devices': {'samplerate': 48000}})
    assert any('no routing' in item for item in unsupported)


def test_compile_strips_stale_htdt_region() -> None:
    base = {
        'devices': {'samplerate': 48000, 'playback': {'channels': 2}},
        'filters': {
            'htdt_deadbeef_ch-1_peq1': {
                'type': 'Biquad',
                'parameters': {'type': 'Peaking', 'freq': 100.0,
                               'q': 1.0, 'gain': -1.0},
            },
            'operator_eq': {
                'type': 'Biquad',
                'parameters': {'type': 'Peaking', 'freq': 60.0,
                               'q': 1.0, 'gain': 1.0},
            },
        },
        'pipeline': [
            {'type': 'Filter', 'channels': [0],
             'names': ['htdt_deadbeef_ch-1_peq1']},
            {'type': 'Filter', 'channels': [1],
             'names': ['operator_eq', 'htdt_deadbeef_extra']},
        ],
    }
    config, notes, _ = compile_camilladsp_config(
        _export(), _camilladsp_binding(), base)
    # Stale htdt_-owned names from the prior deploy tag are stripped;
    # new htdt_ names for this export's tag are written fresh.
    assert not any('deadbeef' in name for name in config['filters'])
    assert any(name.startswith('htdt_') for name in config['filters'])
    # foreign filter and its mixed pipeline step survive, minus htdt name.
    assert 'operator_eq' in config['filters']
    remaining = config['pipeline']
    assert any(
        step.get('names') == ['operator_eq'] for step in remaining)
    assert any('stale' in note for note in notes)


def test_crossover_compile_orders() -> None:
    order2 = compile_camilladsp_config(
        _export(crossovers=(CadCrossoverSetting(
            crossover_type='low_pass', frequency_hz=80.0,
            filter_order=2),)),
        _camilladsp_binding(),
        {'devices': {'samplerate': 48000, 'playback': {'channels': 2}}},
    )
    config, _, unsupported = order2
    assert unsupported == ()
    xo = [f for f in config['filters'].values()
          if f.get('parameters', {}).get('type') == 'Lowpass']
    assert xo and xo[0]['parameters']['q'] == pytest.approx(0.70710678)
    # order-3 crossover exceeds the deployable cascade — fail closed.
    _, _, unsupported = compile_camilladsp_config(
        _export(crossovers=(CadCrossoverSetting(
            crossover_type='low_pass', frequency_hz=80.0,
            filter_order=3),)),
        _camilladsp_binding(),
        {'devices': {'samplerate': 48000}},
    )
    assert any('order 3' in item for item in unsupported)


# -- CamillaDSP apply / read-back ---------------------------------------------


def test_apply_sends_validate_then_set() -> None:
    adapter, transport = _adapter()
    service = CalibrationAdapterService(adapter)
    export = _export(peq=_peq())
    binding = _camilladsp_binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW)
    ack = service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW)
    assert ack.materialization_id == materialization.materialization_id
    commands = [list(request)[0] for request in transport.requests]
    validate_index = commands.index('ValidateConfigJson')
    set_index = commands.index('SetConfigJson')
    assert validate_index < set_index
    # device config now carries the candidate verbatim.
    assert transport._config == json.loads(materialization.payload_text)


def test_apply_requires_operator_confirmation() -> None:
    adapter, transport = _adapter()
    service = CalibrationAdapterService(adapter)
    export = _export()
    binding = _camilladsp_binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW)
    with pytest.raises(PermissionError):
        service.apply_materialization(
            materialization, binding,
            operator_confirmed=False, applied_at_utc=NOW)
    assert not any(
        'SetConfigJson' in request for request in transport.requests)


def test_apply_validate_rejection_never_sets() -> None:
    adapter, transport = _adapter(reject_config='invalid biquad')
    service = CalibrationAdapterService(adapter)
    materialization = service.materialize_export(
        _export(), _camilladsp_binding(), created_at_utc=NOW)
    # reject_config makes the fixture reject materialize's GetConfigJson?
    # No — GetConfigJson is a read, unaffected; the reject hits
    # ValidateConfigJson on apply.
    with pytest.raises(Exception, match='device_error'):
        service.apply_materialization(
            materialization, _camilladsp_binding(),
            operator_confirmed=True, applied_at_utc=NOW)
    assert not any(
        'SetConfigJson' in request for request in transport.requests)


def test_remote_endpoint_requires_operator_approval() -> None:
    remote = 'camilladsp://192.168.1.40:1234'
    adapter = CamillaDSPCalibrationAdapter(
        {remote: FixtureCamillaDSPTransport()})
    binding = build_device_binding(
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        device_family='camilladsp', device_model='CamillaDSP',
        device_serial=remote, firmware_version='3.0.0',
        routing=(('ch-1', '0'),), bound_at_utc=NOW)
    with pytest.raises(CamillaDSPEndpointError):
        adapter.materialize(_export(), binding, created_at_utc=NOW)
    approved = CamillaDSPCalibrationAdapter(
        {remote: FixtureCamillaDSPTransport()},
        approved_remote_endpoints=(remote,))
    materialization = approved.materialize(
        _export(), binding, created_at_utc=NOW)
    assert materialization.materialization_id


def test_unbound_endpoint_and_foreign_binding_rejected() -> None:
    adapter, _ = _adapter()
    missing = build_device_binding(
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        device_family='camilladsp', device_model='CamillaDSP',
        device_serial='camilladsp://127.0.0.1:9999',
        firmware_version='3.0.0',
        routing=(('ch-1', '0'),), bound_at_utc=NOW)
    with pytest.raises(AdapterCapabilityError, match='not bound'):
        adapter.materialize(_export(), missing, created_at_utc=NOW)
    foreign = build_device_binding(
        adapter_id='htdt-file-adapter',
        device_family='avr-family', device_model='AVR-X',
        device_serial='SN-1', firmware_version='1',
        routing=(('ch-1', 'out-1'),), bound_at_utc=NOW)
    with pytest.raises(Exception, match='for adapter htdt-file-adapter'):
        adapter.materialize(_export(), foreign, created_at_utc=NOW)


def test_read_back_round_trip_verifies() -> None:
    adapter, _ = _adapter()
    plan = _plan(peq=_peq(), delay_s=0.001)
    export = build_generic_biquad_export(plan=plan, created_at_utc=NOW)
    binding = _camilladsp_binding()
    service = CalibrationAdapterService(
        adapter, calibration_repository=_CalibrationAuthority(plan, export))
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW)
    service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW)
    snapshot = service.read_back_snapshot(
        binding, export, observed_at_utc=NOW)
    assert snapshot.source == 'read_back'
    assert snapshot.deviations == ()
    state, mode = derive_deployment_state('read_back', False)
    assert state == 'deployment_verified'
    assert mode == 'machine_readback'


def test_read_back_divergence_reported() -> None:
    adapter, transport = _adapter()
    plan = _plan(peq=_peq())
    export = build_generic_biquad_export(plan=plan, created_at_utc=NOW)
    binding = _camilladsp_binding()
    service = CalibrationAdapterService(
        adapter, calibration_repository=_CalibrationAuthority(plan, export))
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW)
    service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW)
    # Device-side drift: operator edits one deployed PEQ gain.
    tag = export.exported_settings_semantic_sha256[:12]
    transport._config['filters'][f'htdt_{tag}_ch-1_peq1'][
        'parameters']['gain'] = -9.9
    snapshot = service.read_back_snapshot(
        binding, export, observed_at_utc=NOW)
    assert snapshot.deviations
    state, _mode = derive_deployment_state('read_back', True)
    assert state == 'deployment_mismatch'


def test_read_back_requires_htdt_region() -> None:
    adapter, _ = _adapter()
    binding = _camilladsp_binding()
    with pytest.raises(AdapterCapabilityError, match='no htdt deployment'):
        adapter.read_back(binding, observed_at_utc=NOW)


def test_normalize_ignores_foreign_config() -> None:
    assert normalize_camilladsp_config({'devices': {}}) is None
    assert normalize_camilladsp_config(
        {'description': 'plain text', 'filters': {}}) is None


# -- runtime observation + stage ladder ---------------------------------------


def test_observe_runtime_seals_telemetry() -> None:
    adapter, _ = _adapter(
        clipped_samples=7, processing_load_pct=42.5,
        signal_levels={
            'playback_peak': [-3.0, -3.2],
            'playback_rms': [-18.0, -18.1],
            'capture_peak': [-9.0, -9.1],
            'capture_rms': [-24.0, -24.2],
        })
    observation = adapter.observe_runtime(
        _camilladsp_binding(),
        document_id=DOC, observed_at_utc=NOW)
    assert observation.observation_id.startswith('crun-')
    assert observation.clipped_samples == 7
    assert observation.processing_state == 'Running'
    assert observation.playback_peak_dbfs == (-3.0, -3.2)
    assert observation.limitations == ()
    assert observation.observation_sha256 == canonical_sha256(
        observation.identity_payload())


def test_observe_runtime_records_limitations() -> None:
    class _Partial(FixtureCamillaDSPTransport):
        def request(self, command):
            if 'GetClippedSamples' in command or 'GetSignalLevels' in command:
                return {
                    next(iter(command)): {
                        'result': 'Error', 'value': 'unsupported'}}
            return super().request(command)

    partial = _Partial(config={'devices': {}})
    adapter = CamillaDSPCalibrationAdapter({ENDPOINT: partial})
    observation = adapter.observe_runtime(
        _camilladsp_binding(),
        document_id=DOC, observed_at_utc=NOW)
    assert observation.clipped_samples is None
    assert 'GetClippedSamples:device_error' in observation.limitations
    assert observation.playback_peak_dbfs is None


def test_stage_ladder_never_exceeds_evidence() -> None:
    dep = _deployment()
    ref = _dep_ref(dep)
    session = CamillaDSPDeploymentSession.create(
        document_id=DOC,
        deployment_ref=ref,
        binding_sha256='a' * 64,
        materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        previous_config_sha256='d' * 64,
        deployed_at_utc=NOW,
    )
    assert derive_camilladsp_stage(session) == 'compiled_for_target'
    with_ack = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=ref, binding_sha256='a' * 64,
        materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        previous_config_sha256='d' * 64, deploy_ack_id='ack:1',
        deployed_at_utc=NOW)
    assert derive_camilladsp_stage(with_ack) == 'deploy_acknowledged'
    matched = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=ref, binding_sha256='a' * 64,
        materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        previous_config_sha256='d' * 64, deploy_ack_id='ack:1',
        readback_config_sha256='c' * 64, readback_matched=True,
        deployed_at_utc=NOW)
    assert derive_camilladsp_stage(matched) == 'config_readback_matched'
    # runtime telemetry must never reach post-measurement verification.
    observed = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=ref, binding_sha256='a' * 64,
        materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        previous_config_sha256='d' * 64, deploy_ack_id='ack:1',
        readback_config_sha256='c' * 64, readback_matched=True,
        runtime_observation_ref=AuthorityRef(
            kind='camilladsp_runtime_observation', ref_id='crun-1',
            ref_sha256='e' * 64),
        deployed_at_utc=NOW)
    assert derive_camilladsp_stage(observed) == 'runtime_observed'
    with pytest.raises(ValidationError):
        # runtime ref without a matched read-back cannot seal.
        CamillaDSPDeploymentSession.create(
            document_id=DOC, deployment_ref=ref,
            binding_sha256='a' * 64,
            materialization_sha256='b' * 64,
            candidate_config_sha256='c' * 64,
            runtime_observation_ref=AuthorityRef(
                kind='camilladsp_runtime_observation', ref_id='crun-1',
                ref_sha256='e' * 64),
            deployed_at_utc=NOW)


# -- rollback ------------------------------------------------------------------


def test_rollback_restores_previous_config() -> None:
    previous = {
        'devices': {'samplerate': 48000, 'playback': {'channels': 2}},
        'description': 'operator baseline',
    }
    adapter, transport = _adapter(config=dict(previous))
    binding = _camilladsp_binding()
    service = CalibrationAdapterService(adapter)
    export = _export(peq=_peq())
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW)
    dep = _deployment()
    ref = _dep_ref(dep)
    baseline_text, baseline_sha = adapter.capture_baseline(binding)
    session = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=ref,
        binding_sha256=binding.binding_sha256,
        materialization_sha256=materialization.materialization_sha256,
        candidate_config_sha256=config_sha256(
            json.loads(materialization.payload_text)),
        previous_config_sha256=baseline_sha,
        deploy_ack_id='ack:1', deployed_at_utc=NOW)
    service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW)
    evidence = adapter.rollback_previous(
        binding, document_id=DOC, deployment_ref=ref,
        session=session, requested_at_utc=NOW)
    assert evidence.evidence_id.startswith('crbk-')
    assert evidence.outcome == 'restored_verified'
    assert evidence.previous_config_sha256 == baseline_sha
    assert evidence.post_rollback_readback_sha256 == baseline_sha
    # The device now carries the previous config verbatim.
    assert transport._config['description'] == 'operator baseline'
    assert not any(
        name.startswith('htdt_')
        for name in transport._config.get('filters', {}))


def test_rollback_flags_diverged_previous() -> None:
    baseline = {
        'devices': {'samplerate': 48000, 'playback': {'channels': 2}},
    }
    adapter, transport = _adapter(config=dict(baseline))
    binding = _camilladsp_binding()
    service = CalibrationAdapterService(adapter)
    materialization = service.materialize_export(
        _export(), binding, created_at_utc=NOW)
    service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW)
    # Someone applied yet another config between deploy and rollback —
    # GetPreviousConfig no longer equals the pinned baseline.
    transport.request({'SetConfigJson': json.dumps(
        {'devices': {'samplerate': 48000}, 'note': 'manual edit'})})
    dep = _deployment()
    session = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=_dep_ref(dep),
        binding_sha256=binding.binding_sha256,
        materialization_sha256=materialization.materialization_sha256,
        candidate_config_sha256=config_sha256(
            json.loads(materialization.payload_text)),
        previous_config_sha256=config_sha256(baseline),
        deploy_ack_id='ack:1', deployed_at_utc=NOW)
    evidence = adapter.rollback_previous(
        binding, document_id=DOC, deployment_ref=_dep_ref(dep),
        session=session, requested_at_utc=NOW)
    assert evidence.outcome == 'restored_previous_diverged'


def test_rollback_without_previous_fails_closed() -> None:
    adapter, _transport = _adapter()
    dep = _deployment()
    evidence = adapter.rollback_previous(
        _camilladsp_binding(), document_id=DOC,
        deployment_ref=_dep_ref(dep), requested_at_utc=NOW)
    assert evidence.outcome == 'failed'
    assert evidence.post_rollback_readback_sha256 is None
    assert evidence.error_detail


# -- persistence ---------------------------------------------------------------


def _scene(tmp_path: Path) -> SceneRepository:
    from htdt.cad_scene import make_f1_scene
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def test_repository_round_trip(tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    repo = CadCamillaDSPDeploymentRepository(scene)
    dep = _deployment()
    session = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=_dep_ref(dep),
        binding_sha256='a' * 64, materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        previous_config_sha256='d' * 64,
        deploy_ack_id='ack:1', deployed_at_utc=NOW)
    repo.save_deployment_session(session)
    assert repo.get_deployment_session(session.session_id) == session
    observation = CamillaDSPRuntimeObservation.create(
        document_id=DOC, binding_sha256='a' * 64,
        deployment_ref=_dep_ref(dep), observed_at_utc=NOW,
        processing_state='Running', clipped_samples=0)
    repo.save_runtime_observation(observation)
    assert repo.get_runtime_observation(
        observation.observation_id) == observation
    evidence = CamillaDSPRollbackEvidence.create(
        document_id=DOC, deployment_ref=_dep_ref(dep),
        previous_config_sha256='d' * 64, requested_at_utc=NOW,
        restored_config_sha256='d' * 64,
        post_rollback_readback_sha256='d' * 64,
        outcome='restored_verified')
    repo.save_rollback_evidence(evidence)
    assert repo.get_rollback_evidence(evidence.evidence_id) == evidence
    # listing scopes to document
    assert repo.list_deployment_sessions(DOC) == (session,)
    assert repo.list_runtime_observations('other-doc') == ()


def test_repository_rejects_tampered_rows(tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    repo = CadCamillaDSPDeploymentRepository(scene)
    dep = _deployment()
    session = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=_dep_ref(dep),
        binding_sha256='a' * 64, materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64, deployed_at_utc=NOW)
    repo.save_deployment_session(session)
    from htdt.cad_schema import connect_sqlite
    from contextlib import closing
    with closing(connect_sqlite(scene.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_camilladsp_deployment_sessions '
            'SET candidate_config_sha256=? WHERE session_id=?',
            ('f' * 64, session.session_id))
    with pytest.raises(DeploymentIntegrityError):
        repo.get_deployment_session(session.session_id)
    # A record whose id is not bound to its sealed payload cannot save.
    forged = CamillaDSPDeploymentSession.create(
        document_id=DOC, deployment_ref=_dep_ref(dep),
        binding_sha256='a' * 64, materialization_sha256='b' * 64,
        candidate_config_sha256='e' * 64, deployed_at_utc=NOW)
    forged = type(forged).model_construct(
        **{**forged.model_dump(mode='python'),
           'session_id': session.session_id})
    with pytest.raises(DeploymentIntegrityError):
        repo.save_deployment_session(forged)
