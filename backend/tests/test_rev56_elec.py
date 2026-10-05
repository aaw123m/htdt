"""REV56-ELEC regression tests (#593 electrical qualification, #597 wiring)."""

from __future__ import annotations

from math import log10, sqrt
from pathlib import Path

import pytest

from htdt.cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    ElectricalValue,
    build_amplifier_output_capability,
)
from htdt.cad_direct_level import DirectLevelFrequencyBand
from htdt.cad_electrical_compatibility import (
    build_electrical_scenario,
    evaluate_electrical_compatibility,
)
from htdt.cad_electrical_compatibility_repository import (
    CadElectricalCompatibilityRepository,
    ElectricalQualificationConflictError,
)
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    FrequencyDomain,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
)
from htdt.cad_physical_interconnect import (
    CableTermination,
    InterconnectHop,
    applicable_test_kinds,
    build_logical_physical_binding,
    build_physical_interconnect,
    build_wiring_verification,
    candidate_fault_segments,
    derive_cable_schedule,
    evaluate_logical_physical_binding,
    evaluate_physical_path_state,
    record_service_change,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Size3, make_empty_scene
from htdt.cad_speaker_impedance import (
    ImpedanceSample,
    build_amplifier_electrical_limit,
    build_speaker_impedance_authority,
)
from htdt.cad_speaker_level_transfer import (
    build_amplifier_output_impedance,
    build_speaker_cable_electrical_profile,
    build_speaker_electrical_path,
)
from htdt.cad_wiring_trace_repository import (
    CadWiringTraceRepository,
    WiringTraceConflictError,
)


DOC = 'doc-rev56-elec'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T00:01:00+00:00'
T2 = '2026-10-05T00:02:00+00:00'
T3 = '2026-10-05T00:03:00+00:00'
T4 = '2026-10-05T00:04:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _provenance(
    name: str,
    digit: str = '1',
    *,
    kind: str = 'manufacturer',
) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind=kind,
        source_name=name,
        source_version='2026-10-05',
        source_reference='rev56-elec-fixture',
        source_sha256=digit * 64,
    )


_BAND = DirectLevelFrequencyBand(low_hz=100.0, high_hz=10000.0)
_DOMAIN = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)


def _speaker(
    *,
    definition_id: str = 'spk-fl',
    sensitivity_db_spl: float | None = 88.0,
    sensitivity_quantity: str = 'voltage_v_rms',
    sensitivity_input_value: float = 2.83,
    continuous_db_spl: float | None = 112.0,
    peak_db_spl: float | None = 118.0,
    spl_provenance_kind: str = 'manufacturer',
    declared_headroom_db: float | None = None,
    headroom_reference_level_db_spl: float | None = None,
):
    provenance = _provenance(definition_id, '1')
    spl_provenance = _provenance(
        f'{definition_id}-spl', '2', kind=spl_provenance_kind
    )
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='manufacturer',
        manufacturer='Fixture Acoustics',
        model=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
        acoustic_reference_point_m=Offset3(),
        sensitivity=(
            None
            if sensitivity_db_spl is None
            else SensitivityReference(
                level_db_spl=sensitivity_db_spl,
                input_quantity=sensitivity_quantity,
                input_value=sensitivity_input_value,
                distance_m=1.0,
                valid_frequency_domain=_DOMAIN,
                weighting=None,
                provenance=provenance,
            )
        ),
        spl_capability=(
            None
            if continuous_db_spl is None and peak_db_spl is None
            else SplCapability(
                continuous_db_spl=continuous_db_spl,
                peak_db_spl=peak_db_spl,
                reference_distance_m=1.0,
                valid_frequency_domain=_DOMAIN,
                continuous_duration_s=(
                    None if continuous_db_spl is None else 60.0
                ),
                peak_duration_s=None if peak_db_spl is None else 0.1,
                declared_headroom_db=declared_headroom_db,
                headroom_reference_level_db_spl=(
                    headroom_reference_level_db_spl
                ),
                provenance=spl_provenance,
            )
        ),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _amplifier(
    *,
    capability_id: str = 'amp-a',
    continuous_v: float | None = 20.0,
    peak_v: float | None = None,
    continuous_w: float | None = None,
    simultaneous_channel_count: int = 1,
    minimum_load_ohm: float = 4.0,
    maximum_load_ohm: float = 16.0,
):
    continuous = None
    if continuous_v is not None:
        continuous = ElectricalValue(
            quantity='voltage_v_rms', value=continuous_v
        )
    elif continuous_w is not None:
        continuous = ElectricalValue(
            quantity='power_w', value=continuous_w
        )
    peak = (
        None
        if peak_v is None
        else ElectricalValue(quantity='voltage_v_rms', value=peak_v)
    )
    return build_amplifier_output_capability(
        capability_id=capability_id,
        version='1',
        identity_kind='manufacturer',
        manufacturer='Fixture Amplification',
        model=capability_id,
        output_id='main',
        provenance=(_provenance(capability_id, '3'),),
        supported_load=AmplifierLoadDomain(
            reference_load_ohm=8.0,
            minimum_load_ohm=minimum_load_ohm,
            maximum_load_ohm=maximum_load_ohm,
        ),
        continuous_capability=continuous,
        peak_capability=peak,
        continuous_duration_s=(
            None if continuous is None else 60.0
        ),
        peak_duration_s=None if peak is None else 0.05,
        clipping_reference_definition='rated power +0.5 dB THD',
        valid_frequency_band=_DOMAIN,
        weighting='unweighted',
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=simultaneous_channel_count,
            shared_supply_evidence=simultaneous_channel_count > 1,
            condition_description='fixture channel condition',
        ),
    )


def _impedance(
    equipment,
    *,
    impedance_id: str = 'spk-fl-z',
    tier: str = 'minimum_impedance',
    nominal_ohm: float = 8.0,
    minimum_ohm: float | None = 6.4,
    minimum_hz: float | None = 500.0,
    samples=(),
):
    return build_speaker_impedance_authority(
        impedance_id=impedance_id,
        version='1',
        equipment_definition=equipment,
        tier=tier,
        nominal_impedance_ohm=nominal_ohm,
        minimum_impedance_ohm=minimum_ohm,
        minimum_frequency_hz=minimum_hz,
        samples=samples,
        interpolation=None if not samples else 'linear',
        valid_frequency_domain=_DOMAIN,
        provenance=_provenance(f'{impedance_id}-prov', '4'),
    )


def _scenario(speaker, amplifier, impedance=None, **kwargs):
    options = {
        'document_id': DOC,
        'equipment': speaker,
        'target_spl_db_spl': 85.0,
        'listening_distance_m': 3.0,
        'frequency_band': _BAND,
        'weighting': 'unweighted',
        'amplifier_capability': amplifier,
        'impedance': impedance,
        'simultaneous_channel_count': 1,
        'stress_profile': 'sustained',
    }
    options.update(kwargs)
    return build_electrical_scenario(**options)


# ---------------------------------------------------------------------------
# #593 — ALC10..ALC80 electrical qualification fixtures
# ---------------------------------------------------------------------------


def test_alc10_qualified_baseline_electrically_qualified_model():
    """ALC10 — a well-evidenced chain qualifies end-to-end."""
    speaker = _speaker()
    amplifier = _amplifier(continuous_v=20.0)
    impedance = _impedance(speaker)
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.verdict == 'qualified'
    assert result.failure_codes == ()
    assert result.capability_class == 'electrically_qualified_model'
    # 85 dB @ 3 m -> 94.54 dB @ 1 m -> required V = 2.83*10^((94.54-88)/20)
    expected_level = 85.0 + 20.0 * log10(3.0)
    expected_v = 2.83 * 10.0 ** ((expected_level - 88.0) / 20.0)
    assert result.required_input.state == 'available'
    assert result.required_input.value == pytest.approx(expected_v)
    assert result.minimum_load_ohm.state == 'available'
    assert result.minimum_load_ohm.value == pytest.approx(6.4)


def test_alc20_amplifier_clipping_when_ceiling_below_required():
    """ALC20 — required input above the evidenced ceiling → clipping."""
    speaker = _speaker()
    amplifier = _amplifier(continuous_v=4.0)
    impedance = _impedance(speaker)
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.verdict == 'unqualified'
    assert 'amplifier_clipping' in result.failure_codes
    assert result.dominant_limiter == 'voltage'


def test_alc30_load_below_amplifier_rating():
    """ALC30 — minimum |Z| dips below the evidenced amplifier load domain."""
    speaker = _speaker()
    amplifier = _amplifier(continuous_v=20.0, minimum_load_ohm=7.0)
    impedance = _impedance(speaker, minimum_ohm=6.4)
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.verdict == 'unqualified'
    assert 'load_below_amplifier_rating' in result.failure_codes
    assert result.dominant_limiter == 'load_domain'


def test_alc40_current_margin_insufficient():
    """ALC40 — worst-case current demand exceeds the evidenced ceiling."""
    speaker = _speaker()
    # 40 V ceiling keeps the clipping gate out so current dominates.
    amplifier = _amplifier(continuous_v=40.0)
    impedance = _impedance(speaker)
    limit = build_amplifier_electrical_limit(
        limit_id='amp-a-limit',
        version='1',
        amplifier_capability=amplifier,
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=1,
            shared_supply_evidence=False,
            condition_description='limit fixture',
        ),
        rms_current_ceiling_a=1.0,
        valid_frequency_domain=_DOMAIN,
        provenance=_provenance('amp-a-limit-prov', '5'),
    )
    # ~33.8 V required at 100 dB/3 m → ~5.3 A worst case over 6.4 ohm.
    scenario = _scenario(
        speaker,
        amplifier,
        impedance,
        amplifier_limit=limit,
        target_spl_db_spl=100.0,
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        amplifier_limit=limit,
        evaluated_at_utc=T0,
    )
    assert result.worst_current_demand_a.state == 'available'
    assert 'current_margin_insufficient' in result.failure_codes
    assert result.dominant_limiter == 'current'


def test_alc50_multichannel_power_limit_single_channel_spec():
    """ALC50 — a 1ch spec can never qualify simultaneous multichannel."""
    speaker = _speaker()
    amplifier = _amplifier(
        continuous_v=30.0, simultaneous_channel_count=1
    )
    impedance = _impedance(speaker)
    scenario = _scenario(
        speaker,
        amplifier,
        impedance,
        simultaneous_channel_count=5,
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.verdict == 'unqualified'
    assert 'multichannel_power_limit' in result.failure_codes
    assert result.dominant_limiter == 'multichannel'


def test_alc60_nominal_only_impedance_is_insufficient_evidence():
    """ALC60 — nominal impedance alone can never prove the load domain."""
    speaker = _speaker()
    amplifier = _amplifier(continuous_v=20.0)
    impedance = _impedance(
        speaker, tier='nominal_impedance_only', minimum_ohm=None,
        minimum_hz=None,
    )
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.verdict == 'indeterminate'
    assert 'insufficient_evidence' in result.failure_codes


def test_alc65_thermal_derating_burst_holds_sustained_fails():
    """ALC65 — peak ceiling covers the demand; sustained does not."""
    speaker = _speaker()
    # required V ≈ 5.36 V at 85 dB/3 m for 88 dB/2.83 V sensitivity
    amplifier = _amplifier(continuous_v=5.0, peak_v=9.0)
    impedance = _impedance(speaker)
    scenario = _scenario(
        speaker, amplifier, impedance, stress_profile='sustained'
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert 'amplifier_clipping' in result.failure_codes
    assert 'thermal_derating' in result.failure_codes
    assert result.dominant_limiter == 'thermal'


def test_alc70_loudspeaker_compression_limit():
    """ALC70 — acoustic ceiling at distance below the requested SPL."""
    speaker = _speaker(continuous_db_spl=100.0, peak_db_spl=104.0)
    amplifier = _amplifier(continuous_v=30.0)
    impedance = _impedance(speaker)
    scenario = _scenario(
        speaker, amplifier, impedance, target_spl_db_spl=95.0,
        listening_distance_m=4.0,
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert 'loudspeaker_compression_limit' in result.failure_codes
    assert result.dominant_limiter == 'speaker_compression'


def test_alc75_cable_loss_excessive_pushes_source_over_ceiling():
    """ALC75 — cable loss requires a source voltage beyond the ceiling."""
    speaker = _speaker()
    # amp ceiling 7.0 V — terminal requirement ~6.0 V passes, but the
    # cable must deliver it through a lossy run.
    amplifier = _amplifier(continuous_v=7.0)
    impedance = _impedance(speaker)
    amp_out_z = build_amplifier_output_impedance(
        impedance_id='amp-a-outz',
        amplifier_ref='amp-a',
        tier='scalar',
        scalar_ohm=0.1,
        provenance=(_provenance('amp-outz', '6'),),
    )
    # 30 m of 0.5 ohm/m cable → 15 ohm series — huge loss vs 6.4 ohm min.
    cable = build_speaker_cable_electrical_profile(
        cable_profile_id='lossy-cable',
        series_resistance_ohm_per_m=0.5,
        provenance=(_provenance('cable-prov', '7'),),
    )
    path = build_speaker_electrical_path(
        path_id='path-fl',
        amplifier_impedance_id='amp-a-outz',
        cable_profile_id='lossy-cable',
        cable_length_m=30.0,
        load_topology='single',
        load_impedance_ids=('spk-fl-z',),
        provenance=(_provenance('path-prov', '8'),),
    )
    scenario = _scenario(
        speaker, amplifier, impedance, electrical_path=path
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        electrical_path=path,
        amplifier_output_impedance=amp_out_z,
        cable_profile=cable,
        evaluated_at_utc=T0,
    )
    assert result.cable_loss_db.state == 'available'
    assert result.cable_loss_db.value < 0.0
    assert 'cable_loss_excessive' in result.failure_codes
    assert result.dominant_limiter == 'cable'


def test_alc78_digital_headroom_limit_eq_boost():
    """ALC78 — declared EQ boost exceeds declared digital headroom."""
    speaker = _speaker()
    amplifier = _amplifier(continuous_v=20.0)
    impedance = _impedance(speaker)
    scenario = _scenario(
        speaker,
        amplifier,
        impedance,
        eq_boost_db=8.0,
        eq_boost_band=_BAND,
        digital_headroom_db=4.0,
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert 'digital_headroom_limit' in result.failure_codes
    assert result.dominant_limiter == 'digital'


def test_alc80_active_lane_qualifies_on_acoustic_evidence():
    """ALC80 — an active loudspeaker never fabricates an amp model."""
    speaker = _speaker(
        continuous_db_spl=104.0,
        peak_db_spl=110.0,
        spl_provenance_kind='measured',
    )
    scenario = build_electrical_scenario(
        document_id=DOC,
        equipment=speaker,
        drive_kind='active_system',
        target_spl_db_spl=92.0,
        listening_distance_m=3.0,
        frequency_band=_BAND,
        weighting='unweighted',
        stress_profile='sustained',
    )
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        evaluated_at_utc=T0,
    )
    # 104 dB @ 1 m → ~94.5 dB @ 3 m — target 92 dB qualifies.
    assert result.verdict == 'qualified'
    assert result.capability_class == 'acoustic_output_measured'
    assert result.speaker_spl_ceiling_db_spl.state == 'available'
    assert result.spl_margin_db.value > 0.0


def test_alc90_qualified_repository_roundtrip(tmp_path: Path):
    """Qualifications persist append-only with exact payload."""
    scene_repository = _scene_repo(tmp_path)
    repository = CadElectricalCompatibilityRepository(scene_repository)
    speaker = _speaker()
    amplifier = _amplifier()
    impedance = _impedance(speaker)
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    repository.save_qualification(result)
    loaded = repository.get_qualification(result.qualification_id)
    assert loaded == result
    by_hash = repository.get_qualification_by_hash(
        result.qualification_sha256
    )
    assert by_hash == result
    assert repository.list_qualifications(DOC) == (result,)
    assert repository.list_for_scenario(scenario.scenario_sha256) == (
        result,
    )
    with pytest.raises(ElectricalQualificationConflictError):
        repository.save_qualification(result)


def test_alc95_scenario_authority_drift_rejected():
    """A swapped authority under the same scenario id fails closed."""
    speaker = _speaker()
    amplifier = _amplifier()
    impedance = _impedance(speaker)
    scenario = _scenario(speaker, amplifier, impedance)
    other_speaker = _speaker(definition_id='spk-other')
    with pytest.raises(ValueError):
        evaluate_electrical_compatibility(
            scenario=scenario,
            equipment=other_speaker,
            amplifier_capability=amplifier,
            impedance=impedance,
            evaluated_at_utc=T0,
        )


def test_alc98_missing_sensitivity_is_indeterminate_not_qualified():
    """No sensitivity → required input stays missing → indeterminate."""
    speaker = _speaker(sensitivity_db_spl=None)
    amplifier = _amplifier()
    impedance = _impedance(speaker)
    scenario = _scenario(speaker, amplifier, impedance)
    result = evaluate_electrical_compatibility(
        scenario=scenario,
        equipment=speaker,
        amplifier_capability=amplifier,
        impedance=impedance,
        evaluated_at_utc=T0,
    )
    assert result.required_input.state == 'missing'
    assert result.verdict == 'indeterminate'
    assert 'insufficient_evidence' in result.failure_codes


# ---------------------------------------------------------------------------
# #597 — CAB10..CAB70 as-built wiring traceability fixtures
# ---------------------------------------------------------------------------


def _from_port():
    return CableTermination(
        role='from',
        device_instance_ref='avr-1',
        port_identity='pre-out-fl',
        connector_type='rca',
        observed_label='AVR1-PO-FL',
    )


def _to_speaker():
    return CableTermination(
        role='to',
        device_instance_ref='spk-fl',
        port_identity='terminal+',
        connector_type='binding-post',
        polarity='normal',
        observed_label='SPK-FL-IN',
    )


def _path(**kwargs):
    options = {
        'path_id': 'run-fl',
        'version': '1',
        'document_id': DOC,
        'path_class': 'speaker_level',
        'evidence_state': 'designed_path',
        'from_termination': _from_port(),
        'to_termination': _to_speaker(),
        'created_at_utc': T0,
    }
    options.update(kwargs)
    return build_physical_interconnect(**options)


def test_cab10_interconnect_roundtrip_with_labels_and_hops(tmp_path: Path):
    """CAB10 — sealed path roundtrip keeps hops, labels, terminations."""
    scene_repository = _scene_repo(tmp_path)
    repository = CadWiringTraceRepository(scene_repository)
    path = _path(
        hops=(
            InterconnectHop(
                sequence=0, kind='device_port', ref_id='avr-1',
                label='pre-out FL',
            ),
            InterconnectHop(
                sequence=1, kind='permanent_run', ref_id='in-wall-3',
                label='IW-3', length_m=12.0,
            ),
            InterconnectHop(
                sequence=2, kind='wall_plate', ref_id='plate-b',
                label='WP-B',
            ),
            InterconnectHop(
                sequence=3,
                kind='destination_termination',
                ref_id='spk-fl',
                label='terminal +',
            ),
        ),
        label_end_a='AVR1-PO-FL',
        label_end_b='SPK-FL-IN',
        intermediate_labels=('IW-3', 'WP-B'),
        label_profile_standard_ref='avixa-f501.01@2015',
        cable_manufacturer='Fixture Cables',
        cable_model='CL3-162',
        conductor_count=2,
        conductor_gauge='16 AWG',
        length_designed_m=12.0,
        observation_state='observed_both_ends',
    )
    repository.save_interconnect(path)
    loaded = repository.get_interconnect(path.path_id, path.version)
    assert loaded == path
    assert loaded.labels() == ('AVR1-PO-FL', 'IW-3', 'WP-B', 'SPK-FL-IN')
    assert len(loaded.hops) == 4
    assert repository.list_interconnects(DOC) == (path,)
    history = repository.list_interconnect_history('run-fl')
    assert history == (path,)


def test_cab20_interconnect_append_only(tmp_path: Path):
    """CAB20 — (path_id, version) is saved exactly once."""
    scene_repository = _scene_repo(tmp_path)
    repository = CadWiringTraceRepository(scene_repository)
    path = _path()
    repository.save_interconnect(path)
    with pytest.raises(WiringTraceConflictError):
        repository.save_interconnect(path)


def test_cab25_unbound_logical_route_is_unverified_routing(tmp_path: Path):
    """CAB25 — a logical route with no physical path is unverified."""
    path = _path()
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        recorded_at_utc=T1,
    )
    # Binding evaluated without its path — the association cannot be proven.
    assert (
        evaluate_logical_physical_binding(binding, path=None)
        == 'unverified_routing'
    )
    # A path id that was never recorded resolves the same way.
    assert (
        evaluate_logical_physical_binding(
            binding, path=_path(path_id='other-run', version='9')
        )
        == 'unverified_routing'
    )


def test_cab30_verification_promotes_path_state(tmp_path: Path):
    """CAB30 — passing domain tests evaluate designed → verified."""
    path = _path()
    assessment = evaluate_physical_path_state(path, ())
    assert assessment.evaluated_state == 'designed_path'
    record = build_wiring_verification(
        path=path,
        test_kind='continuity',
        result='pass',
        provenance=_provenance('field-tech', 'a', kind='measured'),
        verified_at_utc=T1,
    )
    assessment = evaluate_physical_path_state(path, (record,))
    assert assessment.evaluated_state == 'verified_path'
    assert assessment.passing_tests == ('continuity',)
    assert assessment.promotion_gap is not None
    assert 'designed_path' in assessment.promotion_gap


def test_cab35_declared_verified_without_test_shows_gap():
    """CAB35 — declared verified_path alone can never promote itself."""
    path = _path(evidence_state='verified_path')
    assessment = evaluate_physical_path_state(path, ())
    assert assessment.evaluated_state == 'unverified_declaration'
    assert assessment.promotion_gap is not None
    assert 'without a passing applicable' in assessment.promotion_gap


def test_cab40_failing_test_yields_failed_binding(tmp_path: Path):
    """CAB40 — a failing applicable test fails the path and binding."""
    path = _path()
    record = build_wiring_verification(
        path=path,
        test_kind='loop_resistance',
        result='fail',
        measured_quantity='loop_resistance_ohm',
        measured_value=45.0,
        measured_unit='ohm',
        provenance=_provenance('field-tech', 'b', kind='measured'),
        verified_at_utc=T1,
    )
    assessment = evaluate_physical_path_state(path, (record,))
    assert assessment.evaluated_state == 'failed_path'
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        recorded_at_utc=T1,
    )
    assert (
        evaluate_logical_physical_binding(
            binding, path=path, verifications=(record,)
        )
        == 'failed_path'
    )


def test_cab50_endpoint_mismatch_detected():
    """CAB50 — logical endpoints disagreeing with the path are flagged."""
    path = _path()
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        # Logical source port is `pre-out-fr`, physical lands on `pre-out-fl`.
        logical_from_identity='avr-1|pre-out-fr',
        logical_to_identity='spk-fl|terminal+',
        recorded_at_utc=T1,
    )
    assert (
        evaluate_logical_physical_binding(binding, path=path)
        == 'mismatched'
    )


def test_cab55_service_change_stales_old_binding(tmp_path: Path):
    """CAB55 — a re-terminated path stales bindings to the old hash."""
    scene_repository = _scene_repo(tmp_path)
    repository = CadWiringTraceRepository(scene_repository)
    path = _path()
    repository.save_interconnect(path)
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        recorded_at_utc=T1,
    )
    repository.save_binding(binding)
    assert (
        evaluate_logical_physical_binding(binding, path=path)
        == 'designed_binding'
    )
    moved = record_service_change(
        path,
        version='2',
        created_at_utc=T2,
        service_action='reterminated-to-wall-plate-B',
        to_termination=CableTermination(
            role='to',
            device_instance_ref='spk-fl',
            port_identity='terminal-',
            connector_type='binding-post',
            polarity='reversed',
            observed_label='SPK-FL-IN-R',
        ),
        evidence_state='installed_reported_path',
        observation_state='observed_one_end',
    )
    repository.save_interconnect(moved)
    assert (
        evaluate_logical_physical_binding(binding, path=moved)
        == 'stale'
    )
    history = repository.list_interconnect_history('run-fl')
    assert len(history) == 2
    assert history[1].supersedes_path_sha256 == path.semantic_sha256


def test_cab60_derived_cable_schedule(tmp_path: Path):
    """CAB60 — schedule derives from sealed paths, listing unbound routes."""
    path = _path(
        label_end_a='AVR1-PO-FL',
        label_end_b='SPK-FL-IN',
        conductor_gauge='16 AWG',
        length_measured_m=12.4,
    )
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        recorded_at_utc=T1,
    )
    schedule = derive_cable_schedule(
        document_id=DOC,
        paths=(path,),
        bindings=(binding,),
        generated_at_utc=T3,
    )
    assert len(schedule.entries) == 1
    entry = schedule.entries[0]
    assert entry.path_id == 'run-fl'
    assert entry.length_semantics == 'measured'
    assert entry.length_m == pytest.approx(12.4)
    assert entry.logical_refs == ('edge-avr-to-fl',)
    assert entry.evaluated_state == 'designed_path'
    # An unbound logical ref surfaces as unverified_routing.
    phantom = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-ghost',
        path=path,
        recorded_at_utc=T1,
    )
    schedule = derive_cable_schedule(
        document_id=DOC,
        paths=(),
        bindings=(phantom,),
        generated_at_utc=T3,
    )
    assert schedule.unbound_logical_refs == ('edge-ghost',)


def test_cab65_inapplicable_test_kind_rejected():
    """CAB65 — a speaker-polarity test cannot attach to a network run."""
    network_path = _path(
        path_id='run-net',
        path_class='network_copper',
        to_termination=CableTermination(
            role='to',
            device_instance_ref='proj-1',
            port_identity='rj45-1',
            connector_type='rj45',
            observed_label='PROJ-NET-1',
        ),
    )
    assert 'link_negotiation' in applicable_test_kinds('network_copper')
    assert 'speaker_polarity' not in applicable_test_kinds(
        'network_copper'
    )
    with pytest.raises(ValueError):
        build_wiring_verification(
            path=network_path,
            test_kind='speaker_polarity',
            result='pass',
            provenance=_provenance('field-tech', 'c', kind='measured'),
            verified_at_utc=T1,
        )


def test_cab70_observation_and_orphan_rules(tmp_path: Path):
    """CAB70 — observed labels and orphan verifications fail closed."""
    # observed_both_ends without observed labels cannot be declared.
    from_unobserved = CableTermination(
        role='from',
        device_instance_ref='avr-1',
        port_identity='pre-out-fr',
        connector_type='rca',
    )
    with pytest.raises(ValueError):
        _path(
            observation_state='observed_both_ends',
            from_termination=from_unobserved,
        )
    # Verification against a path the store has never seen is an orphan.
    scene_repository = _scene_repo(tmp_path)
    repository = CadWiringTraceRepository(scene_repository)
    path = _path()
    record = build_wiring_verification(
        path=path,
        test_kind='continuity',
        result='pass',
        provenance=_provenance('field-tech', 'd', kind='measured'),
        verified_at_utc=T1,
    )
    with pytest.raises(ValueError):
        repository.save_verification(record)
    repository.save_interconnect(path)
    repository.save_verification(record)
    assert repository.list_verifications('run-fl') == (record,)


def test_cab75_fault_localization_is_diagnostic_only():
    """candidate_fault_segments narrows — it never asserts the fault."""
    path = _path(
        hops=(
            InterconnectHop(sequence=0, kind='device_port'),
            InterconnectHop(sequence=1, kind='permanent_run', ref_id='r1'),
            InterconnectHop(sequence=2, kind='wall_plate', ref_id='wp'),
            InterconnectHop(
                sequence=3, kind='destination_termination'
            ),
        ),
    )
    assert candidate_fault_segments(path) == path.hops
    narrowed = candidate_fault_segments(
        path, upstream_verified_sequence=1
    )
    assert [hop.sequence for hop in narrowed] == [2, 3]


def test_cab80_binding_repository_roundtrip(tmp_path: Path):
    """Bindings persist append-only; latest lookup returns the newest."""
    scene_repository = _scene_repo(tmp_path)
    repository = CadWiringTraceRepository(scene_repository)
    path = _path()
    repository.save_interconnect(path)
    binding = build_logical_physical_binding(
        document_id=DOC,
        logical_ref_kind='signal_path_edge',
        logical_ref_id='edge-avr-to-fl',
        path=path,
        recorded_at_utc=T1,
    )
    repository.save_binding(binding)
    assert repository.get_binding(binding.binding_id) == binding
    assert repository.latest_binding_for('edge-avr-to-fl') == binding
    with pytest.raises(WiringTraceConflictError):
        repository.save_binding(binding)


def test_cab90_service_change_identical_path_rejected():
    """A 'service change' producing an identical path is meaningless."""
    path = _path()
    with pytest.raises(ValueError):
        record_service_change(
            path,
            version='2',
            created_at_utc=T2,
            service_action='no-op',
        )
