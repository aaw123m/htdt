"""REV56-INFRA regression tests (issues #587/#603/#606).

Fixture map — issue #587 rack/power/thermal:
  RPT10 measured sustained scenario qualified   -> test_rpt10_*
  RPT20 passive-only ventilation                -> test_rpt20_*
  RPT30 still-rising measurement                -> test_rpt30_*
  RPT40 measured over-temperature               -> test_rpt40_*
  RPT50 continuous-derated circuit overload     -> test_rpt50_*
  RPT60 'power conditioner' is not a UPS        -> test_rpt60_*
  RPT70 UPS runtime below scenario requirement  -> test_rpt70_*
  RPT80 repository append-only + ref integrity  -> test_rpt80_*

Fixture map — issue #603 render path:
  RND10 native render qualification             -> test_rnd10_*
  RND20 transport fallback                      -> test_rnd20_*
  RND30 installed outputs unaddressed           -> test_rnd30_*
  RND40 configured > installed mismatch         -> test_rnd40_*
  RND50 upmixed provenance is never 'native'    -> test_rnd50_*
  RND60 unbound capability stays insufficient   -> test_rnd60_*
  RND70 evidence-basis rejections               -> test_rnd70_*

Fixture map — issue #606 hum/buzz diagnosis:
  HUM10 building-earthing -> electrician only    -> test_hum10_*
  HUM20 protective earth cannot be defeated      -> test_hum20_*
  HUM30 evaluated mitigation needs repeat capture-> test_hum30_*
  HUM40 confirmed/external states need anchors   -> test_hum40_*
  HUM50 shield-termination basis honesty         -> test_hum50_*
  HUM60 resolved via measured safe intervention  -> test_hum60_*
  HUM70 insufficient / external classification   -> test_hum70_*
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_electrical_noise import (
    AudioInterconnectEvidence,
    CorrelationRecord,
    ElectricalNoiseObservation,
    HumBuzzDiagnostic,
    IsolationStep,
    NoiseIsolationTest,
    NoiseMitigationAttempt,
    SpectralComponent,
    build_diagnostic,
    build_interconnect,
    build_isolation_test,
    build_mitigation_attempt,
    build_noise_observation,
    evaluate_humbuzz,
)
from htdt.cad_electrical_noise_repository import (
    CadElectricalNoiseRepository,
    ElectricalNoiseIntegrityError,
)
from htdt.cad_infrastructure import (
    BranchCircuit,
    CoolingDevice,
    InfrastructureScenario,
    PoEBudget,
    PoEPortAllocation,
    PowerProtectionDevice,
    RackThermalMeasurement,
    ScenarioDeviceLoad,
    TemperatureReading,
    build_circuit,
    build_poe_budget,
    build_protection_device,
    build_rack,
    build_rack_device,
    build_scenario,
    build_thermal_measurement,
    evaluate_infrastructure,
)
from htdt.cad_infrastructure_repository import (
    CadInfrastructureRepository,
    InfrastructureIntegrityError,
)
from htdt.cad_render_path import (
    ImmersiveContentProfile,
    ObservedOutput,
    RenderSession,
    RenderedOutputObservation,
    RendererCapabilityProfile,
    SpeakerLayoutDeclaration,
    build_content_profile,
    build_layout,
    build_output_observation,
    build_render_session,
    build_renderer_capability,
    evaluate_render_path,
)
from htdt.cad_render_path_repository import (
    CadRenderPathRepository,
    RenderPathIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

DOC = 'doc-rev56-infra'
_TS = '2026-10-05T12:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _rack_ref(rack) -> AuthorityRef:
    return AuthorityRef(
        kind='rack_enclosure',
        ref_id=rack.rack_id,
        ref_sha256=rack.rack_sha256,
    )


def _scenario_ref(scenario) -> AuthorityRef:
    return AuthorityRef(
        kind='infrastructure_scenario',
        ref_id=scenario.scenario_id,
        ref_sha256=scenario.scenario_sha256,
    )


def _content_ref(content) -> AuthorityRef:
    return AuthorityRef(
        kind='immersive_content_profile',
        ref_id=content.content_id,
        ref_sha256=content.content_sha256,
    )


def _session_ref(session) -> AuthorityRef:
    return AuthorityRef(
        kind='render_session',
        ref_id=session.session_id,
        ref_sha256=session.session_sha256,
    )


def _observation_ref(observation) -> AuthorityRef:
    return AuthorityRef(
        kind='electrical_noise_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


# ---------------------------------------------------------------------------
# #587 fixtures
# ---------------------------------------------------------------------------


def _rack(**kwargs):
    fields = dict(
        document_id=DOC,
        label='機器ラック',
        rack_unit_capacity=20,
        enclosure_kind='closed_cabinet',
        service_access='front_and_rear',
        clearance_front_mm=900.0,
        clearance_rear_mm=800.0,
        vent_intake='front',
        vent_exhaust='rear',
        blanking_panels='full',
        design_ambient_c=22.0,
    )
    fields.update(kwargs)
    return build_rack(**fields)


def _amp(rack, **kwargs):
    fields = dict(
        document_id=DOC,
        label='パワーアンプ',
        rack_ref=_rack_ref(rack),
        role='amplifier',
        power_idle_w=45.0,
        power_nominal_w=400.0,
        power_max_w=1200.0,
        ambient_limit_c=40.0,
        heat_evidence_class='derived_from_input_power',
        thermal_protection='declared',
        fan_behavior='variable_speed',
    )
    fields.update(kwargs)
    return build_rack_device(**fields)


def _circuit(**kwargs):
    fields = dict(
        document_id=DOC,
        label='専用回路 1',
        nominal_voltage_v=100.0,
        frequency_hz=50.0,
        breaker_rating_a=20.0,
        continuous_load_policy='continuous_derated',
    )
    fields.update(kwargs)
    return build_circuit(**fields)


def _scenario(devices, **kwargs):
    return build_scenario(
        document_id=DOC,
        kind='movie_typical',
        name='映画再生シナリオ',
        device_loads=tuple(
            ScenarioDeviceLoad(
                device_ref=d.device_id, load_w=350.0,
                load_source='measured',
            )
            for d in devices
        ),
        **kwargs,
    )


def _measurement(rack, scenario, **kwargs):
    fields = dict(
        document_id=DOC,
        rack_ref=_rack_ref(rack),
        scenario_ref=_scenario_ref(scenario),
        readings=(
            TemperatureReading(point='ambient_room', temp_c=23.0),
            TemperatureReading(point='rack_inlet', temp_c=24.5),
            TemperatureReading(point='rack_internal', temp_c=33.0),
            TemperatureReading(point='device_exhaust', temp_c=38.0),
        ),
        duration_s=3600,
        warm_up_complete=True,
        still_rising=False,
        measured_power_w=355.0,
        measured_at_utc=_TS,
    )
    fields.update(kwargs)
    return build_thermal_measurement(**fields)


def test_rpt10_measured_sustained_scenario_qualified() -> None:
    rack = _rack(
        cooling_devices=(
            CoolingDevice(
                label='排気ファン', kind='thermostatic_exhaust',
                airflow_cfm=200.0, control='thermostatic',
                location='rear',
            ),
        ),
    )
    circuit = _circuit()
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp])
    measurement = _measurement(rack, scenario)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    assert verdict.thermal_state == 'thermally_measured'
    assert verdict.electrical_state == 'power_measured'
    assert verdict.noise_state == 'not_applicable' or (
        verdict.noise_state == 'insufficient_evidence'
    )
    # measured thermal + measured electrical must qualify; noise may
    # limit to qualified_with_limitations when active cooling has no
    # #580 evidence
    if verdict.noise_state == 'insufficient_evidence':
        assert verdict.overall_state == 'qualified_with_limitations'
    else:
        assert verdict.overall_state == 'qualified'


def test_rpt20_passive_only_never_claims_calculated_capacity() -> None:
    rack = _rack(
        cooling_devices=(
            CoolingDevice(
                label='ベントパネル', kind='passive_vent',
                location='rear',
            ),
        ),
    )
    circuit = _circuit()
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp])
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        evaluated_at_utc=_TS,
    )
    assert verdict.thermal_state == 'manufacturer_guidance_only'
    assert verdict.overall_state in (
        'qualified_with_limitations', 'design_only',
    )
    assert verdict.overall_state != 'qualified'


def test_rpt30_still_rising_measurement_is_insufficient_evidence() -> None:
    rack = _rack()
    circuit = _circuit()
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp])
    measurement = _measurement(
        rack, scenario, warm_up_complete=False, still_rising=True,
    )
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    assert verdict.thermal_state == 'insufficient_evidence'
    assert verdict.overall_state != 'qualified'


def test_rpt40_measured_over_limit_is_over_capacity() -> None:
    rack = _rack()
    circuit = _circuit()
    amp = _amp(rack, circuit_ref=circuit.circuit_id, ambient_limit_c=30.0)
    scenario = _scenario([amp])
    measurement = _measurement(rack, scenario)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    assert verdict.thermal_state == 'over_capacity'
    assert verdict.overall_state == 'over_capacity'


def test_rpt50_continuous_derated_circuit_overload() -> None:
    rack = _rack()
    circuit = _circuit()
    amps = [
        _amp(rack, circuit_ref=circuit.circuit_id, label=f'アンプ {i}')
        for i in range(5)
    ]
    # usable = 100 V * 20 A * 0.8 = 1600 W; 5 * 350 W = 1750 W exceeds it
    scenario = _scenario(amps)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=amps,
        circuits=[circuit],
        evaluated_at_utc=_TS,
    )
    assert verdict.electrical_state == 'over_capacity'
    assert verdict.overall_state == 'over_capacity'


def test_rpt60_conditioner_is_never_a_ups() -> None:
    rack = _rack()
    conditioner = build_protection_device(
        document_id=DOC,
        label='パワーコンディショナ',
        capabilities=('power_conditioning', 'surge_protection'),
        observed_state='in_service',
    )
    circuit = _circuit(ups_ref=conditioner.protection_id)
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp], required_runtime_minutes=10.0)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        protections=[conditioner],
        evaluated_at_utc=_TS,
    )
    assert verdict.protection_state == 'insufficient_evidence'
    assert verdict.overall_state != 'qualified'


def test_rpt70_ups_runtime_below_requirement_is_not_qualified() -> None:
    rack = _rack()
    ups = build_protection_device(
        document_id=DOC,
        label='UPS',
        capabilities=('ups_backup',),
        watt_rating=1500.0,
        runtime_minutes_at_load=4.0,
        observed_state='in_service',
    )
    circuit = _circuit(ups_ref=ups.protection_id)
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp], required_runtime_minutes=15.0)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        protections=[ups],
        evaluated_at_utc=_TS,
    )
    assert verdict.protection_state == 'ups_insufficient'
    assert verdict.overall_state == 'not_qualified'


def test_rpt70b_va_only_ups_cannot_verify_watt_margin() -> None:
    rack = _rack()
    ups = build_protection_device(
        document_id=DOC,
        label='UPS (VA のみ)',
        capabilities=('ups_backup',),
        va_rating=1500.0,
        observed_state='in_service',
    )
    circuit = _circuit(ups_ref=ups.protection_id)
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    scenario = _scenario([amp])
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=[amp],
        circuits=[circuit],
        protections=[ups],
        evaluated_at_utc=_TS,
    )
    assert verdict.protection_state == 'insufficient_evidence'
    assert verdict.overall_state != 'qualified'


def test_rpt80_repository_roundtrip_and_append_only(tmp_path) -> None:
    repo = CadInfrastructureRepository(_scene_repo(tmp_path))
    rack = _rack()
    repo.save_rack(rack)
    assert repo.get_rack(rack.rack_id) == rack

    circuit = _circuit()
    repo.save_circuit(circuit)
    amp = _amp(rack, circuit_ref=circuit.circuit_id)
    repo.save_rack_device(amp)
    assert repo.get_rack_device(amp.device_id) == amp
    scenario = _scenario([amp])
    repo.save_scenario(scenario)
    measurement = _measurement(rack, scenario)
    repo.save_thermal_measurement(measurement)
    verdict = evaluate_infrastructure(
        scenario=scenario, racks=[rack], devices=[amp],
        circuits=[circuit], measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict
    assert len(repo.list_racks(DOC)) == 1

    # idempotent re-save is a no-op
    repo.save_rack(rack)
    # a different rack is a distinct record, not a conflict (semantic
    # ids are sha-derived — OPS convention)
    different = _rack(label='別ラック')
    repo.save_rack(different)
    assert repo.get_rack(different.rack_id) == different
    assert len(repo.list_racks(DOC)) == 2
    # a forged seal is rejected
    forged = rack.model_copy(update={'label': '改竄'})
    with pytest.raises(InfrastructureIntegrityError):
        repo.save_rack(forged)
    forged_id = rack.model_copy(update={'rack_id': 'rack-forged'})
    with pytest.raises(InfrastructureIntegrityError):
        repo.save_rack(forged_id)
    # device against an unpersisted rack rejected
    ghost_rack = _rack(label='未保存ラック')
    orphan = _amp(ghost_rack)
    with pytest.raises(InfrastructureIntegrityError):
        repo.save_rack_device(orphan)


def test_rpt80b_poe_port_ceiling_enforced() -> None:
    with pytest.raises(ValidationError):
        build_poe_budget(
            document_id=DOC,
            label='スイッチ',
            standard='ieee802_3at',
            port_power_limit_w=45.0,
        )
    budget = build_poe_budget(
        document_id=DOC,
        label='スイッチ',
        standard='ieee802_3bt_type4',
        port_power_limit_w=60.0,
        aggregate_budget_w=600.0,
        ports=(
            PoEPortAllocation(port_label='p1', allocated_w=25.0),
            PoEPortAllocation(port_label='p2', allocated_w=25.0),
        ),
    )
    assert budget.port_ceiling_w() == 60.0


def test_rpt80c_device_ordering_and_evidence_rules() -> None:
    rack = _rack()
    # power figures must be ordered idle <= nominal <= max
    with pytest.raises(ValidationError):
        _amp(rack, power_idle_w=500.0, power_max_w=400.0)
    # manufacturer thermal claim needs the dissipation figure
    with pytest.raises(ValidationError):
        build_rack_device(
            document_id=DOC,
            label='x',
            rack_ref=_rack_ref(rack),
            heat_evidence_class='manufacturer_thermal_data',
        )
    # device must pin a rack_enclosure ref kind
    with pytest.raises(ValidationError):
        build_rack_device(
            document_id=DOC,
            label='x',
            rack_ref=AuthorityRef(
                kind='installed_equipment_instance',
                ref_id='eq-1', ref_sha256='a' * 64,
            ),
        )


# ---------------------------------------------------------------------------
# #603 fixtures
# ---------------------------------------------------------------------------


def _atmos_content(**kwargs):
    fields = dict(
        document_id=DOC,
        label='Atmos テスト素材',
        container='bitstream_hdmi',
        format_label='atmos',
        metadata_class='object',
        declared_layout_label='7.1.4',
        object_count_declared=12,
        metadata_source='container_parser',
    )
    fields.update(kwargs)
    return build_content_profile(**fields)


def _atmos_capability(**kwargs):
    fields = dict(
        document_id=DOC,
        device_ref=AuthorityRef(
            kind='installed_equipment_instance',
            ref_id='avr-1', ref_sha256='b' * 64,
        ),
        model_label='AVR-X',
        firmware_label='1.2.3',
        license_state='licensed',
        supported_input_formats=('atmos', 'dts_x', 'lpcm_5_1'),
        supported_layouts=('5.1.4', '7.1.4'),
        native_renderer_formats=('atmos', 'dts_x'),
        capability_source='manufacturer_document',
    )
    fields.update(kwargs)
    return build_renderer_capability(**fields)


def _installed_layout(**kwargs):
    roles = (
        'L', 'C', 'R', 'LS', 'RS', 'LB', 'RB',
        'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
    )
    fields = dict(
        document_id=DOC,
        kind='physically_installed',
        label='7.1.4',
        ear_level_count=7,
        top_count=4,
        lfe_channel_count=1,
        physical_subwoofer_count=2,
        speaker_roles=roles,
        evidence='physical_inventory',
    )
    fields.update(kwargs)
    return build_layout(**fields)


def _configured_layout(**kwargs):
    fields = dict(
        document_id=DOC,
        kind='processor_configured',
        label='7.1.4',
        ear_level_count=7,
        top_count=4,
        lfe_channel_count=1,
        speaker_roles=(
            'L', 'C', 'R', 'LS', 'RS', 'LB', 'RB',
            'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
        ),
        evidence='processor_readback',
    )
    fields.update(kwargs)
    return build_layout(**fields)


def _session(content, **kwargs):
    fields = dict(
        document_id=DOC,
        content_ref=_content_ref(content),
        decoder_mode='native_decode',
        upmixer_state='off',
        source_format_label='atmos',
        transported_format_label='atmos',
        started_at_utc=_TS,
    )
    fields.update(kwargs)
    return build_render_session(**fields)


def _observation(session, **kwargs):
    outputs = kwargs.pop(
        'outputs',
        tuple(
            ObservedOutput(
                output_label=label, signal_present=True,
                activity='active',
            )
            for label in (
                'L', 'C', 'R', 'LS', 'RS', 'LB', 'RB',
                'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
            )
        ),
    )
    fields = dict(
        document_id=DOC,
        session_ref=_session_ref(session),
        capture_method='test_signal_asset',
        outputs=outputs,
        observed_at_utc=_TS,
    )
    fields.update(kwargs)
    return build_output_observation(**fields)


def test_rnd10_native_render_qualified() -> None:
    content = _atmos_content()
    capability = _atmos_capability()
    installed = _installed_layout()
    configured = _configured_layout()
    session = _session(content)
    observation = _observation(session)
    verdict = evaluate_render_path(
        session=session,
        content=content,
        capability=capability,
        configured_layout=configured,
        installed_layout=installed,
        observations=[observation],
        expected_outputs=[
            'L', 'C', 'R', 'LS', 'RS', 'LB', 'RB',
            'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
        ],
        evaluated_at_utc=_TS,
    )
    assert verdict.overall_state == 'qualified'
    assert verdict.render_state == 'native_render'
    assert verdict.provenance_state == 'native_confirmed'
    assert verdict.axis_result('format_render_eligible') == 'pass'
    assert verdict.axis_result('output_channel_active') == 'pass'


def test_rnd20_transport_fallback_is_stated() -> None:
    content = _atmos_content()
    session = _session(
        content, transported_format_label='lpcm_5_1',
    )
    verdict = evaluate_render_path(
        session=session,
        content=content,
        capability=_atmos_capability(),
        configured_layout=_configured_layout(),
        installed_layout=_installed_layout(),
        evaluated_at_utc=_TS,
    )
    assert verdict.overall_state == 'fallback_only'
    assert verdict.render_state == 'fallback_transport'
    assert verdict.transport_fallback
    assert verdict.fallback_detail is not None


def test_rnd30_installed_outputs_unaddressed_by_configuration() -> None:
    content = _atmos_content()
    configured = _configured_layout(
        label='7.1',
        speaker_roles=('L', 'C', 'R', 'LS', 'RS', 'LB', 'RB', 'LFE'),
        top_count=0,
    )
    session = _session(content)
    verdict = evaluate_render_path(
        session=session,
        content=content,
        capability=_atmos_capability(
            supported_layouts=('5.1.4', '7.1.4', '7.1'),
        ),
        configured_layout=configured,
        installed_layout=_installed_layout(),
        observations=[_observation(session)],
        evaluated_at_utc=_TS,
    )
    assert verdict.layout_match_state == 'partial_unused_outputs'
    assert 'TFL' in verdict.unused_outputs
    assert verdict.axis_result('layout_geometry_eligible') == 'limited'


def test_rnd40_configured_over_installed_is_mismatch() -> None:
    content = _atmos_content()
    installed = _installed_layout(
        speaker_roles=('L', 'C', 'R', 'LS', 'RS', 'LFE'),
        ear_level_count=5, top_count=0,
    )
    session = _session(content)
    verdict = evaluate_render_path(
        session=session,
        content=content,
        capability=_atmos_capability(),
        configured_layout=_configured_layout(),
        installed_layout=installed,
        evaluated_at_utc=_TS,
    )
    assert verdict.layout_match_state == 'configured_differs_from_installed'
    assert verdict.overall_state == 'mismatch'


def test_rnd50_upmixer_provenance_never_native() -> None:
    bed_content = build_content_profile(
        document_id=DOC,
        label='5.1 PCM',
        container='pcm_multichannel',
        format_label='lpcm_5_1',
        metadata_class='channel_bed',
        channel_count_declared=6,
        metadata_source='container_parser',
    )
    session = _session(
        bed_content,
        decoder_mode='surround_upmixer',
        upmixer_state='on',
        source_format_label='lpcm_5_1',
        transported_format_label='lpcm_5_1',
    )
    verdict = evaluate_render_path(
        session=session,
        content=bed_content,
        capability=_atmos_capability(),
        evaluated_at_utc=_TS,
    )
    assert verdict.render_state == 'upmixed'
    assert verdict.provenance_state == 'upmixer_confirmed'


def test_rnd60_unbound_capability_is_insufficient() -> None:
    content = _atmos_content()
    session = _session(content)
    verdict = evaluate_render_path(
        session=session,
        content=content,
        evaluated_at_utc=_TS,
    )
    assert verdict.axis_result('format_render_eligible') == 'unknown'
    assert verdict.overall_state == 'insufficient_evidence'


def test_rnd70_evidence_basis_rejections() -> None:
    # structural counts cannot come from listening impressions
    with pytest.raises(ValidationError):
        build_content_profile(
            document_id=DOC,
            label='聴感ベース',
            metadata_class='object',
            object_count_declared=4,
            metadata_source='listening_observation',
        )
    # rendered-output layout requires observed evidence
    with pytest.raises(ValidationError):
        build_layout(
            document_id=DOC,
            kind='rendered_output',
            speaker_roles=('L', 'R'),
            evidence='user_declared',
        )
    # a native-format claim needs documented evidence
    with pytest.raises(ValidationError):
        _atmos_capability(capability_source='user_declared')
    # x.y.z middle figure is the LFE channel, not physical subs
    layout = _installed_layout()
    assert layout.lfe_channel_count == 1
    assert layout.physical_subwoofer_count == 2


def test_rnd_repository_roundtrip_and_integrity(tmp_path) -> None:
    repo = CadRenderPathRepository(_scene_repo(tmp_path))
    content = _atmos_content()
    repo.save_content(content)
    capability = _atmos_capability()
    repo.save_capability(capability)
    layout = _installed_layout()
    repo.save_layout(layout)
    session = _session(content)
    repo.save_session(session)
    observation = _observation(session)
    repo.save_observation(observation)
    verdict = evaluate_render_path(
        session=session, content=content, capability=capability,
        configured_layout=_configured_layout(),
        installed_layout=layout,
        observations=[observation],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(verdict)
    assert repo.get_session(session.session_id) == session
    assert repo.observations_for_session(session.session_id) == (
        observation,
    )

    # session requires a persisted content profile
    ghost = _atmos_content(label='未保存コンテンツ')
    orphan = _session(ghost)
    with pytest.raises(RenderPathIntegrityError):
        repo.save_session(orphan)
    # a different record is a new semantic id — never a conflict
    different = _atmos_content(label='別素材')
    repo.save_content(different)
    assert repo.get_content(different.content_id) == different
    # forged seal rejected
    forged = content.model_copy(update={'label': '改竄'})
    with pytest.raises(RenderPathIntegrityError):
        repo.save_content(forged)


# ---------------------------------------------------------------------------
# #606 fixtures
# ---------------------------------------------------------------------------


def _observation_noise(**kwargs):
    fields = dict(
        document_id=DOC,
        affected_refs=('アンプ Lch',),
        symptom='hum',
        components=(
            SpectralComponent(freq_hz=50.0, kind='mains_fundamental'),
            SpectralComponent(freq_hz=100.0, kind='harmonic'),
            SpectralComponent(freq_hz=150.0, kind='harmonic'),
        ),
        instrument='acoustic_microphone_spectrum',
        mute_state='unmuted',
        captured_at_utc=_TS,
    )
    fields.update(kwargs)
    return build_noise_observation(**fields)


def test_hum10_building_earthing_forces_electrician_referral() -> None:
    observation = _observation_noise()
    with pytest.raises(ValidationError):
        build_diagnostic(
            document_id=DOC,
            observation_refs=(observation.observation_id,),
            suspect_factors=('building_earthing',),
            recommendation='apply_safe_mitigation',
        )
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(observation.observation_id,),
        suspect_factors=('building_earthing',),
        recommendation='refer_to_qualified_electrician',
    )
    verdict = evaluate_humbuzz(
        diagnostic=diagnostic,
        observations=[observation],
        evaluated_at_utc=_TS,
    )
    assert verdict.state == 'referred_to_electrician'


def test_hum20_protective_earth_cannot_be_defeated() -> None:
    # protective_earth_preserved is a required True literal — a step
    # that defeats PE cannot be expressed as diagnostic evidence at all
    with pytest.raises(ValidationError):
        IsolationStep.model_validate({
            'kind': 'disconnect_authorized_input',
            'target_label': 'x',
            'protective_earth_preserved': False,
        })
    with pytest.raises(ValidationError):
        NoiseMitigationAttempt.model_validate({
            'document_id': DOC,
            'diagnostic_ref': 'd',
            'kind': 'other',
            'detail': 'cheater plug',
            'protective_earth_preserved': False,
            'before_ref': 'obs',
            'performed_at_utc': _TS,
            'attempt_id': 'a' * 40,
            'attempt_sha256': '0' * 64,
        })


def test_hum30_evaluated_mitigation_requires_repeat_capture() -> None:
    observation = _observation_noise()
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(observation.observation_id,),
        suspect_factors=('ground_loop',),
        hypothesis_state='suspected',
    )
    # an evaluated outcome without the identical-capture repeat is rejected
    with pytest.raises(ValidationError):
        build_mitigation_attempt(
            document_id=DOC,
            diagnostic_ref=diagnostic.diagnostic_id,
            kind='balanced_interface',
            detail='XLR化した',
            before_ref=observation.observation_id,
            outcome='noise_resolved',
            performed_at_utc=_TS,
        )
    # 'not_evaluated' cannot carry an after observation
    after = _observation_noise(symptom='unknown', components=())
    with pytest.raises(ValidationError):
        build_mitigation_attempt(
            document_id=DOC,
            diagnostic_ref=diagnostic.diagnostic_id,
            kind='balanced_interface',
            detail='XLR化した',
            before_ref=observation.observation_id,
            after_ref=after.observation_id,
            outcome='not_evaluated',
            performed_at_utc=_TS,
        )


def test_hum40_confirmed_and_external_states_need_anchors() -> None:
    observation = _observation_noise()
    with pytest.raises(ValidationError):
        build_diagnostic(
            document_id=DOC,
            observation_refs=(observation.observation_id,),
            hypothesis_state='confirmed_by_safe_intervention',
        )
    with pytest.raises(ValidationError):
        build_diagnostic(
            document_id=DOC,
            observation_refs=(observation.observation_id,),
            hypothesis_state='classified_external',
        )
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(observation.observation_id,),
        hypothesis_state='classified_external',
        external_authority_ref=AuthorityRef(
            kind='mechanical_noise_test',
            ref_id='mnt:' + 'c' * 64,
            ref_sha256='c' * 64,
        ),
        classification='mechanical_suspected',
    )
    verdict = evaluate_humbuzz(
        diagnostic=diagnostic,
        observations=[observation],
        evaluated_at_utc=_TS,
    )
    assert verdict.state == 'classified_external'


def test_hum50_shield_termination_basis_honesty() -> None:
    # connector shape can never evidence a termination
    with pytest.raises(ValidationError):
        build_interconnect(
            document_id=DOC,
            label='XLRケーブル',
            interface_class='balanced_declared',
            connector_form='xlr',
            shield_termination='chassis_at_entry',
            shield_termination_basis='assumed_from_connector',
        )
    with pytest.raises(ValidationError):
        build_interconnect(
            document_id=DOC,
            label='XLRケーブル',
            interface_class='balanced_declared',
            connector_form='xlr',
            shield_termination='floating',
            shield_termination_basis='assumed_from_connector',
        )
    ok = build_interconnect(
        document_id=DOC,
        label='AES48準拠リンク',
        interface_class='balanced_confirmed',
        connector_form='xlr',
        shield_termination='chassis_at_entry',
        shield_termination_basis='inspected',
        aes_profile_refs=('aes48@2019', 'aes54-2@2008-r2019'),
    )
    assert ok.shield_termination == 'chassis_at_entry'


def test_hum60_resolved_via_measured_safe_intervention(tmp_path) -> None:
    repo = CadElectricalNoiseRepository(_scene_repo(tmp_path))
    before = _observation_noise()
    repo.save_observation(before)
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(before.observation_id,),
        classification='ground_loop_suspected',
        suspect_factors=('ground_loop',),
        hypothesis_state='confirmed_by_safe_intervention',
        confirmed_at_utc=_TS,
    )
    repo.save_diagnostic(diagnostic)
    test = build_isolation_test(
        document_id=DOC,
        observation_ref=_observation_ref(before),
        steps=(
            IsolationStep(
                kind='substitute_balanced_path',
                target_label='アンプ入力リンク',
                outcome='noise_absent',
            ),
        ),
        performed_at_utc=_TS,
    )
    repo.save_isolation_test(test)
    after = _observation_noise(
        symptom='unknown', components=(),
    )
    repo.save_observation(after)
    attempt = build_mitigation_attempt(
        document_id=DOC,
        diagnostic_ref=diagnostic.diagnostic_id,
        kind='balanced_interface',
        detail='バランス化を実施',
        before_ref=before.observation_id,
        after_ref=after.observation_id,
        outcome='noise_resolved',
        performed_at_utc=_TS,
    )
    repo.save_mitigation(attempt)
    verdict = evaluate_humbuzz(
        diagnostic=diagnostic,
        observations=[before, after],
        mitigations=[attempt],
        evaluated_at_utc=_TS,
    )
    repo.save_verdict(verdict)
    assert verdict.state == 'resolved_confirmed'
    assert repo.get_verdict(verdict.verdict_id) == verdict

    # mitigation against an unpersisted diagnostic is rejected
    ghost_diag = build_diagnostic(
        document_id=DOC,
        observation_refs=(before.observation_id,),
        suspect_factors=('emi_pickup',),
    )
    orphan = build_mitigation_attempt(
        document_id=DOC,
        diagnostic_ref=ghost_diag.diagnostic_id,
        kind='cable_connector_repair',
        detail='修繕',
        before_ref=before.observation_id,
        performed_at_utc=_TS,
    )
    with pytest.raises(ElectricalNoiseIntegrityError):
        repo.save_mitigation(orphan)
    # forged seal rejected
    forged = before.model_copy(update={'symptom': 'buzz'})
    with pytest.raises(ElectricalNoiseIntegrityError):
        repo.save_observation(forged)
    # a different payload is a new semantic id — re-save is a no-op
    other = _observation_noise(symptom='buzz')
    repo.save_observation(other)
    repo.save_observation(before)
    assert other.observation_id != before.observation_id


def test_hum70_no_isolation_is_insufficient() -> None:
    observation = _observation_noise()
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(observation.observation_id,),
        classification='mains_fundamental_hum',
        suspect_factors=('ground_loop', 'emi_pickup'),
    )
    verdict = evaluate_humbuzz(
        diagnostic=diagnostic,
        observations=[observation],
        evaluated_at_utc=_TS,
    )
    assert verdict.state == 'insufficient_evidence'


def test_hum70b_spectrum_suggests_hypothesis_not_cause() -> None:
    observation = _observation_noise()
    diagnostic = build_diagnostic(
        document_id=DOC,
        observation_refs=(observation.observation_id,),
        suspect_factors=('dimmer_lighting',),
        frequency_pattern_note='50 Hz + harmonics observed',
    )
    verdict = evaluate_humbuzz(
        diagnostic=diagnostic,
        observations=[observation],
        evaluated_at_utc=_TS,
    )
    assert verdict.state == 'insufficient_evidence'
    assert any('hypothesis' in r or 'suggest' in r for r in verdict.reasons)


def test_hum70c_correlation_is_recorded_not_claimed() -> None:
    observation = _observation_noise(
        correlations=(
            CorrelationRecord(
                factor='lighting_dimmer',
                state_label='調光 40%',
                result='changes',
            ),
        ),
    )
    assert observation.correlations[0].result == 'changes'


# ---------------------------------------------------------------------------
# Audit-chain coverage (OPS factory fix + new repos)
# ---------------------------------------------------------------------------


def test_authority_chain_covers_rev56_infra_and_ops(tmp_path) -> None:
    """The audit repository chain must resolve every authority that
    ``_REPLAY_PROBES`` names — REV56-OPS left three factories
    unregistered, which would KeyError on populated tables."""
    from htdt.native_authority_audit import _RepositoryChain

    scene = _scene_repo(tmp_path)
    chain = _RepositoryChain(tmp_path / 'cad.sqlite3')
    try:
        for name in (
            'infrastructure', 'render_path', 'electrical_noise',
            'security_authority', 'control_scenario', 'safe_listening',
        ):
            assert chain.repo(name) is not None, name
    finally:
        chain.close()


def test_native_audit_replays_infra_rows(tmp_path) -> None:
    """A populated #587 table replays through the audit chain without
    integrity findings."""
    from htdt.native_authority_audit import (
        assert_native_authority_graph,
    )

    repo = CadInfrastructureRepository(_scene_repo(tmp_path))
    rack = _rack()
    repo.save_rack(rack)
    circuit = _circuit()
    repo.save_circuit(circuit)
    repo.save_rack_device(_amp(rack, circuit_ref=circuit.circuit_id))
    scenario = _scenario(repo.list_rack_devices(DOC))
    repo.save_scenario(scenario)
    measurement = _measurement(rack, scenario)
    repo.save_thermal_measurement(measurement)
    verdict = evaluate_infrastructure(
        scenario=scenario,
        racks=[rack],
        devices=list(repo.list_rack_devices(DOC)),
        circuits=list(repo.list_circuits(DOC)),
        measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(verdict)
    assert_native_authority_graph(tmp_path / 'cad.sqlite3')
