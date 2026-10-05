"""Regression tests for the REV56-BUILDING authorities.

Fixture map:

- ISO10-70 (#576): sealed element/scenario/measurement/calibration/
  qualification construction; generic labels cannot carry TL data;
  per-band evidence states and LF-domain classification; receiving-SPL
  estimates only where both sides are declared; declared-but-unmodelled
  flanking/bypass paths demote modelled bands; lifecycle is fail-closed;
  append-only repository integrity.
- MRT10-60 (#589): stress-stimulus identity required; FR sweeps cannot
  qualify a room as rattle-free; remediation resolution requires a
  retest at stress level; tested-level caps the claim below the project
  target; append-only repository integrity.
- SOA10-70 (#590): seat acoustic models keep geometry and acoustics as
  separate evidence layers; generic/assumed evidence can never become
  measurement-grade; occupancy comparability is content-derived;
  geometric clearance is an eligibility gate with enumerable failure
  reasons; commissioning verdicts combine as-built evidence fail-closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_isolation_authority import (
    InterRoomIsolationScenario,
    IsolationBandEvidence,
    IsolationElementEvidence,
    IsolationNormalizationEvidence,
    IsolationSourceStressProfile,
    IsolationTransmissionPath,
    ReceivingRoomCriterion,
    build_calibration_record,
    build_field_measurement,
    build_interroom_scenario,
    build_isolation_element,
    classify_band_lf_domain,
    evaluate_isolation_qualification,
)
from htdt.cad_isolation_authority_repository import (
    CadIsolationAuthorityRepository,
    IsolationAuthorityConflictError,
    IsolationAuthorityIntegrityError,
)
from htdt.cad_mechanical_noise import (
    NoiseStressStimulus,
    VibrationSensorEvidence,
    build_noise_test,
    build_rattle_event,
    build_remediation_action,
    evaluate_mechanical_noise,
)
from htdt.cad_mechanical_noise_repository import (
    CadMechanicalNoiseRepository,
    MechanicalNoiseIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Position3
from htdt.cad_seating_acoustics import (
    ListenerEarAuthority,
    PathOccluder,
    SeatAcousticEvidence,
    SeatGeometryEvidence,
    SeatingZoneBlock,
    build_clearance_evaluation,
    build_occupancy_scenario,
    build_seat_model,
    evaluate_clearance_path,
    evaluate_commissioning_verdict,
    evaluate_occupancy_comparability,
)
from htdt.cad_seating_acoustics_repository import (
    CadSeatingAcousticsRepository,
    SeatingAcousticsIntegrityError,
)


_DOC = 'doc-rev56-building'
_TS = '2026-10-05T00:00:00+00:00'


def _element() -> object:
    return build_isolation_element(
        document_id=_DOC,
        label='shared wall',
        construction_class='double_leaf_decoupled',
        stud_frame='double',
        insulation_fill='mineral_fibre',
        resilient_decoupling='isolation_clips',
        surface_area_m2=12.0,
        evidence=(
            IsolationElementEvidence(
                evidence_kind='lab_tl_spectrum',
                standard_ref='ISO 10140-2',
                edition_or_revision='2021',
                tl_values_db={'125': 41.0, '250': 52.0, '500': 61.0},
                frequency_domain=FrequencyDomain(
                    minimum_hz=100.0, maximum_hz=3150.0
                ),
            ),
        ),
    )


def _profile(**fields) -> IsolationSourceStressProfile:
    base = dict(
        content_class='movie_lfe_stress',
        lfe_active=True,
        band_levels_db={'125': 95.0, '250': 88.0},
        subwoofer_output_level_db=112.0,
        bass_management_state='on',
    )
    base.update(fields)
    return IsolationSourceStressProfile(**base)


def _scenario(
    paths=None,
    state: str = 'design_prediction',
    criteria=None,
    **fields,
) -> InterRoomIsolationScenario:
    if paths is None:
        el = _element()
        paths = (
            IsolationTransmissionPath(
                path_id='partition',
                kind='direct_partition',
                element_id=el.element_id,
                element_sha256=el.element_sha256,
                area_m2=12.0,
            ),
        )
    if criteria is None:
        criteria = ()
    return build_interroom_scenario(
        document_id=_DOC,
        label='theater -> bedroom',
        source_region_id='theater',
        receiving_region_id='bedroom',
        source_profile=_profile(),
        paths=paths,
        criteria=criteria,
        construction_state=state,
        created_at_utc=_TS,
        **fields,
    )


def _measurement(
    scenario: InterRoomIsolationScenario,
    *,
    bands=None,
    conformant: bool = True,
):
    if bands is None:
        bands = (
            IsolationBandEvidence(
                band_hz=125.0,
                metric='standardized_level_difference_dnt',
                value_db=52.0,
                source_level_db=95.0,
                receiving_level_db=43.0,
                background_level_db=20.0,
            ),
            IsolationBandEvidence(
                band_hz=250.0,
                metric='standardized_level_difference_dnt',
                value_db=58.0,
                source_level_db=88.0,
                receiving_level_db=30.0,
                background_level_db=18.0,
            ),
        )
    if conformant:
        return build_field_measurement(
            document_id=_DOC,
            scenario=scenario,
            method_profile='iso_16283_1',
            method_standard_ref='ISO 16283-1:2014',
            method_edition='2014',
            source_position_ids=('src-1', 'src-2'),
            receiver_position_ids=('rcv-1', 'rcv-2', 'rcv-3'),
            bands=bands,
            normalization=IsolationNormalizationEvidence(
                normalization='reverberation_time',
                reference='reference_rt_0p5s',
                receiving_rt_s={'125': 0.45, '250': 0.4},
            ),
            source_room_volume_m3=35.0,
            receiving_room_volume_m3=18.0,
            partition_area_m2=12.0,
            opening_state='closed',
            measured_at_utc='2026-10-05T01:00:00+00:00',
        )
    return build_field_measurement(
        document_id=_DOC,
        scenario=scenario,
        method_profile='informal',
        bands=bands,
        measured_at_utc='2026-10-05T01:00:00+00:00',
    )


def _test(
    signal_type: str = 'slow_sine_sweep',
    level: float | None = 105.0,
    target: float | None = None,
):
    stimulus = NoiseStressStimulus(
        signal_type=signal_type,
        frequency_domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0),
        sweep_rate_oct_s=0.5,
        level_db_spl=level,
        duration_s=45.0,
        channel_ids=('sub-1', 'sub-2'),
        subwoofer_group='subs',
        bass_management_state='on',
    )
    return build_noise_test(
        document_id=_DOC,
        label='LF stress sweep',
        stimulus=stimulus,
        project_target_level_db=target,
        captured_at_utc=_TS,
    )


def _seat_model(source: str = 'measured_product', seat: str = 'seat-1'):
    return build_seat_model(
        document_id=_DOC,
        seat_entity_id=seat,
        geometry=SeatGeometryEvidence(
            seat_entity_id=seat,
            source=source,
            backrest_height_m=1.02,
            backrest_width_m=0.62,
            backrest_thickness_m=0.18,
            recline_deg=12.0,
            upholstery_class='heavily_upholstered',
            envelope_origin=Position3(x_m=1.9, y_m=0.6, z_m=0.0),
            envelope_size=Offset3(x_m=0.8, y_m=0.7, z_m=1.05),
        ),
        acoustics=(
            SeatAcousticEvidence(
                evidence_class='literature_generic',
                applies_to_state='occupied',
                upholstery_class='heavily_upholstered',
                band_spec='octave',
                band_coefficients={'125': 0.4, '250': 0.6, '500': 0.85},
                publication_ref='Beranek JASA 99(4) 1996',
            ),
        ),
    )


def _occupancy(state: str, occupied=(), unoccupied=()):
    return build_occupancy_scenario(
        document_id=_DOC,
        label=f'occupancy-{state}',
        state=state,
        occupied_seat_ids=tuple(occupied),
        unoccupied_seat_ids=tuple(unoccupied),
        zone_block=SeatingZoneBlock(
            seat_count=4,
            row_spacing_m=1.4,
            perimeter_m=7.2,
            occupied_area_m2=6.5,
        ),
        occupant_model='generic_occupant_model',
        created_at_utc=_TS,
    )


# ---------------------------------------------------------------------------
# ISO10 — sealed construction elements and scenarios
# ---------------------------------------------------------------------------


def test_iso10_element_is_content_derived_and_sealed() -> None:
    a = _element()
    b = _element()
    assert a.element_id == b.element_id == a.element_id
    assert a.element_sha256 == b.element_sha256
    changed = build_isolation_element(
        document_id=_DOC, label='shared wall variant',
        construction_class='double_leaf_decoupled',
        evidence=a.evidence,
    )
    assert changed.element_id != a.element_id


def test_iso10_generic_label_cannot_carry_tl() -> None:
    with pytest.raises(ValueError, match='generic_label'):
        IsolationElementEvidence(
            evidence_kind='generic_label',
            tl_values_db={'125': 40.0},
        )
    with pytest.raises(ValueError, match='generic_label'):
        IsolationElementEvidence(
            evidence_kind='generic_label',
            single_number_rating='STC 60',
        )
    # A plain marketing name is allowed as evidence — just honestly.
    evidence = IsolationElementEvidence(
        evidence_kind='generic_label', note='double stud wall per builder'
    )
    assert evidence.evidence_kind == 'generic_label'


def test_iso10_lab_evidence_requires_data() -> None:
    with pytest.raises(ValueError, match='band TL data'):
        IsolationElementEvidence(evidence_kind='lab_tl_spectrum')


def test_iso10_scenario_seal_and_region_distinctness() -> None:
    scenario = _scenario()
    assert scenario.scenario_id.startswith('irs:')
    with pytest.raises(ValueError):
        build_interroom_scenario(
            document_id=_DOC,
            label='self',
            source_region_id='room',
            receiving_region_id='room',
            source_profile=_profile(),
            created_at_utc=_TS,
        )


# ---------------------------------------------------------------------------
# ISO20/30 — per-band verdicts, LF domains, receiving-SPL estimates
# ---------------------------------------------------------------------------


def test_iso20_band_evidence_states_and_lf_domains() -> None:
    scenario = _scenario()
    measurement = _measurement(scenario)
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(measurement,), evaluated_at_utc=_TS
    )
    verdicts = {v.band_hz: v for v in qualification.band_verdicts}
    assert verdicts[125.0].evidence_state == 'measured'
    assert verdicts[125.0].lf_domain == 'standard_rating_domain'
    # Inside the standard rating domain without measured coverage:
    # honest 'declared' — the rating domain says nothing about this band.
    assert verdicts[500.0].evidence_state == 'declared'
    # Below 100 Hz with nothing: below validated domain, never
    # extrapolated from Rw/STC.
    assert verdicts[63.0].evidence_state == 'unknown'
    assert verdicts[63.0].lf_domain == 'below_validated_domain'
    assert 'low_frequency_below_validated_domain' in (
        qualification.limitations
    )


def test_iso20_lf_domain_classification() -> None:
    assert classify_band_lf_domain(
        125.0, has_measured_evidence=False, has_lf_model=False
    ) == 'standard_rating_domain'
    assert classify_band_lf_domain(
        63.0, has_measured_evidence=True, has_lf_model=False
    ) == 'extended_low_frequency_measured'
    assert classify_band_lf_domain(
        63.0, has_measured_evidence=False, has_lf_model=True
    ) == 'extended_low_frequency_modelled'
    assert classify_band_lf_domain(
        63.0, has_measured_evidence=False, has_lf_model=False
    ) == 'below_validated_domain'


def test_iso30_receiving_spl_requires_both_sides() -> None:
    criterion = ReceivingRoomCriterion(
        criterion_id='bedroom-limit',
        kind='max_receiving_spl_band',
        frequency_domain=FrequencyDomain(minimum_hz=100.0, maximum_hz=300.0),
        limit_db=40.0,
    )
    scenario = _scenario(criteria=(criterion,))
    qualification = evaluate_isolation_qualification(
        scenario,
        measurements=(_measurement(scenario),),
        evaluated_at_utc=_TS,
    )
    verdicts = {v.band_hz: v for v in qualification.band_verdicts}
    # 95 dB source - 52 dB DnT = 43 dB estimated receiving level.
    assert verdicts[125.0].estimated_receiving_spl_db == pytest.approx(43.0)
    assert verdicts[250.0].estimated_receiving_spl_db == pytest.approx(30.0)
    # A band without a declared source level yields no estimate.
    assert verdicts[500.0].estimated_receiving_spl_db is None
    result = qualification.criterion_results[0]
    # Worst band (125 Hz: 43 dB vs 40 dB limit) fails by -3 dB.
    assert result.verdict == 'fail'
    assert result.worst_margin_db == pytest.approx(-3.0)


def test_iso30_no_source_spectrum_no_estimate() -> None:
    scenario = _scenario()
    quiet = build_interroom_scenario(
        document_id=_DOC,
        label='no spectrum',
        source_region_id='theater',
        receiving_region_id='bedroom',
        source_profile=IsolationSourceStressProfile(
            content_class='speech'
        ),
        paths=scenario.paths,
        created_at_utc=_TS,
    )
    qualification = evaluate_isolation_qualification(
        quiet,
        measurements=(_measurement(quiet),),
        evaluated_at_utc=_TS,
    )
    assert all(
        v.estimated_receiving_spl_db is None
        for v in qualification.band_verdicts
    )


# ---------------------------------------------------------------------------
# ISO40/50 — fail-closed lifecycle and unmodelled-path handling
# ---------------------------------------------------------------------------


def test_iso40_measured_claim_without_measurement_fails_closed() -> None:
    scenario = _scenario(state='field_measured')
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(), evaluated_at_utc=_TS
    )
    assert qualification.lifecycle_state == 'as_built_unverified'
    assert 'claimed_measurement_not_bound' in qualification.limitations


def test_iso40_informal_measurement_cannot_qualify() -> None:
    scenario = _scenario(state='field_measured')
    informal = _measurement(scenario, conformant=False)
    assert not informal.standard_conformant
    qualification = evaluate_isolation_qualification(
        scenario, measurements=(informal,), evaluated_at_utc=_TS
    )
    assert qualification.lifecycle_state == 'as_built_unverified'
    assert 'no_standard_conformant_field_measurement' in (
        qualification.limitations
    )
    # Diagnostic evidence only — no band reaches 'measured'.
    assert all(
        v.evidence_state != 'measured' for v in qualification.band_verdicts
    )


def test_iso50_declared_flanking_demotes_modelled_bands() -> None:
    el = _element()
    scenario = _scenario(
        paths=(
            IsolationTransmissionPath(
                path_id='partition',
                kind='direct_partition',
                element_id=el.element_id,
                element_sha256=el.element_sha256,
            ),
            IsolationTransmissionPath(
                path_id='duct', kind='duct_hvac',
            ),
            IsolationTransmissionPath(
                path_id='slab', kind='floor_ceiling_structure',
            ),
        )
    )
    qualification = evaluate_isolation_qualification(
        scenario,
        lf_modelled_bands_hz=(63.0, 500.0),
        evaluated_at_utc=_TS,
    )
    assert set(qualification.unmodelled_path_ids) == {'duct', 'slab'}
    verdicts = {v.band_hz: v for v in qualification.band_verdicts}
    # The modelled band cannot claim 'modelled' while a duct path is
    # declared but not carried.
    assert verdicts[500.0].evidence_state == 'declared'
    assert set(verdicts[500.0].limiting_path_ids) == {'duct', 'slab'}
    assert any(
        lim.startswith('declared_paths_unmodelled')
        for lim in qualification.limitations
    )


def test_iso50_measured_band_survives_unmodelled_path() -> None:
    """A conformant field measurement captures the real combined
    transmission — declared-but-unmodelled paths stay listed as
    attribution diagnostics without demoting measured truth."""
    el = _element()
    scenario = _scenario(
        paths=(
            IsolationTransmissionPath(
                path_id='partition', kind='direct_partition',
                element_id=el.element_id, element_sha256=el.element_sha256,
            ),
            IsolationTransmissionPath(path_id='duct', kind='duct_hvac'),
        ),
        criteria=(
            ReceivingRoomCriterion(
                criterion_id='c', kind='max_receiving_spl_band',
                frequency_domain=FrequencyDomain(
                    minimum_hz=100.0, maximum_hz=300.0
                ),
                limit_db=50.0,
            ),
        ),
    )
    qualification = evaluate_isolation_qualification(
        scenario,
        measurements=(_measurement(scenario),),
        evaluated_at_utc=_TS,
    )
    verdicts = {v.band_hz: v for v in qualification.band_verdicts}
    assert verdicts[125.0].evidence_state == 'measured'
    assert verdicts[125.0].limiting_path_ids == ('duct',)
    # Criteria evaluate on measured evidence — 43 dB vs 50 dB -> pass.
    assert qualification.criterion_results[0].verdict == 'pass'


# ---------------------------------------------------------------------------
# ISO60 — repository integrity
# ---------------------------------------------------------------------------


def test_iso60_repository_round_trip_and_seal(tmp_path: Path) -> None:
    repository = CadIsolationAuthorityRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    element = _element()
    repository.save_element(element)
    repository.save_element(element)  # identical re-save is a no-op
    assert repository.get_element(element.element_id) == element

    scenario = _scenario()
    repository.save_scenario(scenario)
    assert repository.get_scenario(scenario.scenario_id) == scenario

    measurement = _measurement(scenario)
    repository.save_field_measurement(measurement)
    assert (
        repository.get_field_measurement(measurement.measurement_id)
        == measurement
    )

    calibration = build_calibration_record(
        document_id=_DOC,
        scenario_id=scenario.scenario_id,
        model_ref='iso_12354_1_detailed',
        bounded_refinement='junction Kij adjusted to measured DnT',
        fit_measurement_ids=(measurement.measurement_id,),
        holdout_measurement_ids=(),
    )
    repository.save_calibration(calibration)
    assert repository.get_calibration(calibration.calibration_id) == (
        calibration
    )

    qualification = evaluate_isolation_qualification(
        scenario, measurements=(measurement,), evaluated_at_utc=_TS
    )
    repository.save_qualification(qualification)
    assert (
        repository.get_qualification(qualification.qualification_id)
        == qualification
    )

    # Commit order: measurements cannot precede their scenario.
    orphan_repo = CadIsolationAuthorityRepository(
        SceneRepository(tmp_path / 'other.sqlite3')
    )
    with pytest.raises(IsolationAuthorityIntegrityError):
        orphan_repo.save_field_measurement(measurement)

    # Forged seal (model_copy bypasses validators) is rejected.
    forged = measurement.model_copy(
        update={'method_profile': 'astm_e336'}
    )
    with pytest.raises(IsolationAuthorityIntegrityError):
        repository.save_field_measurement(forged)

    # A divergent scenario under the same id is a conflict — but ids are
    # content-derived, so a same-id different-sha save is unreachable
    # through the model. Conflict surfaces via forged content ids only
    # through direct payload edits, which _assert_sealed already blocks.
    # Re-saving identical content stays a no-op.
    repository.save_scenario(scenario)


# ---------------------------------------------------------------------------
# ISO70 — calibration holdout separation
# ---------------------------------------------------------------------------


def test_iso70_fit_and_holdout_must_be_disjoint() -> None:
    scenario = _scenario()
    measurement = _measurement(scenario)
    with pytest.raises(ValueError, match='disjoint'):
        build_calibration_record(
            document_id=_DOC,
            scenario_id=scenario.scenario_id,
            model_ref='iso_12354_1',
            bounded_refinement='x',
            fit_measurement_ids=(measurement.measurement_id,),
            holdout_measurement_ids=(measurement.measurement_id,),
        )


# ---------------------------------------------------------------------------
# MRT10/20 — stress stimulus identity and event binding
# ---------------------------------------------------------------------------


def test_mrt10_fr_sweep_is_not_a_rattle_stressor() -> None:
    fr_test = _test(signal_type='frequency_response_measurement')
    assert not fr_test.stimulus.is_rattle_stress
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(fr_test,),
        events=(),
        evaluated_at_utc=_TS,
    )
    assert qualification.overall_verdict == 'insufficient_evidence'
    assert 'no_rattle_stress_test' in qualification.limitations


def test_mrt10_stress_test_with_no_events_is_free_at_tested() -> None:
    test = _test(level=105.0, target=105.0)
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(),
        evaluated_at_utc=_TS,
    )
    assert qualification.overall_verdict == 'rattle_free_at_tested_level'
    assert qualification.tested_max_level_db == 105.0


def test_mrt10_stress_below_target_is_limited() -> None:
    test = _test(level=95.0, target=105.0)
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(),
        evaluated_at_utc=_TS,
    )
    assert qualification.overall_verdict == 'qualified_with_limitations'
    assert any(
        lim.startswith('project_target_exceeds_tested_level')
        for lim in qualification.limitations
    )


def test_mrt20_vibration_sensor_cannot_self_calibrate() -> None:
    with pytest.raises(ValueError, match='calibration_ref'):
        VibrationSensorEvidence(
            device_id='phone-1', calibration_class='calibrated'
        )
    ok = VibrationSensorEvidence(
        device_id='acc-1', calibration_class='calibrated',
        calibration_ref='cal-sheet-2026-09'
    )
    assert ok.calibration_class == 'calibrated'


def test_mrt20_event_binds_exact_test() -> None:
    test = _test()
    event = build_rattle_event(
        document_id=_DOC,
        test=test,
        kind='panel_resonance',
        first_onset_level_db=92.0,
        repeatability='repeatable',
        localization_state='correlated_with_object',
        suspected_object_refs=('wall-panel-3',),
        detected_at_utc=_TS,
    )
    assert event.test_id == test.test_id
    assert event.test_sha256 == test.test_sha256


# ---------------------------------------------------------------------------
# MRT30/40 — remediation resolution requires retest; level caps
# ---------------------------------------------------------------------------


def test_mrt30_remediation_without_retest_is_insufficient() -> None:
    test = _test()
    event = build_rattle_event(
        document_id=_DOC, test=test, kind='door_hardware_vibration',
        localization_state='correlated_with_object',
        suspected_object_refs=('door-handle',),
        detected_at_utc=_TS,
    )
    action = build_remediation_action(
        document_id=_DOC,
        action_kind='fastener_tightened',
        affected_object_ref='door-handle',
        event_refs=((event.event_id, event.event_sha256),),
        performed_at_utc='2026-10-05T02:00:00+00:00',
    )
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(event,),
        remediation_actions=(action,),
        retest_test_ids=(),
        evaluated_at_utc=_TS,
    )
    assert (
        qualification.event_results[0].verdict == 'insufficient_evidence'
    )
    assert qualification.overall_verdict == 'qualified_with_limitations'


def test_mrt30_retest_and_resolved_reaches_resolved_verdict() -> None:
    test = _test()
    event = build_rattle_event(
        document_id=_DOC, test=test, kind='panel_resonance',
        localization_state='resolved',
        suspected_object_refs=('wall-panel-3',),
        detected_at_utc=_TS,
    )
    action = build_remediation_action(
        document_id=_DOC, action_kind='panel_braced',
        affected_object_ref='wall-panel-3',
        event_refs=((event.event_id, event.event_sha256),),
        performed_at_utc='2026-10-05T02:00:00+00:00',
    )
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(event,),
        remediation_actions=(action,),
        retest_test_ids=(test.test_id,),
        evaluated_at_utc=_TS,
    )
    assert qualification.event_results[0].verdict == (
        'resolved_at_tested_level'
    )
    assert qualification.overall_verdict == 'rattle_free_at_tested_level'


def test_mrt30_unremediated_event_is_not_resolved() -> None:
    test = _test()
    event = build_rattle_event(
        document_id=_DOC, test=test, kind='fixture_resonance',
        localization_state='unresolved',
        detected_at_utc=_TS,
    )
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(event,),
        evaluated_at_utc=_TS,
    )
    assert qualification.event_results[0].verdict == 'not_resolved'
    assert qualification.overall_verdict == 'not_qualified'


def test_mrt40_resolved_degrades_when_target_exceeds_test() -> None:
    test = _test(level=95.0)
    event = build_rattle_event(
        document_id=_DOC, test=test, kind='panel_resonance',
        localization_state='resolved',
        suspected_object_refs=('panel',),
        detected_at_utc=_TS,
    )
    action = build_remediation_action(
        document_id=_DOC, action_kind='damping_added',
        affected_object_ref='panel',
        event_refs=((event.event_id, event.event_sha256),),
        performed_at_utc='2026-10-05T02:00:00+00:00',
    )
    qualification = evaluate_mechanical_noise(
        document_id=_DOC,
        tests=(test,),
        events=(event,),
        remediation_actions=(action,),
        retest_test_ids=(test.test_id,),
        project_target_level_db=105.0,
        evaluated_at_utc=_TS,
    )
    assert qualification.event_results[0].verdict == (
        'resolved_with_limitations'
    )
    assert qualification.overall_verdict == 'qualified_with_limitations'


# ---------------------------------------------------------------------------
# MRT50/60 — repository integrity
# ---------------------------------------------------------------------------


def test_mrt50_repository_round_trip_and_seal(tmp_path: Path) -> None:
    repository = CadMechanicalNoiseRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    test = _test()
    repository.save_test(test)
    repository.save_test(test)
    assert repository.get_test(test.test_id) == test

    event = build_rattle_event(
        document_id=_DOC, test=test, kind='duct_grille_vibration',
        localization_state='correlated_with_object',
        suspected_object_refs=('hvac-grille',),
        detected_at_utc=_TS,
    )
    repository.save_event(event)
    assert repository.get_event(event.event_id) == event

    action = build_remediation_action(
        document_id=_DOC, action_kind='grille_treated',
        affected_object_ref='hvac-grille',
        event_refs=((event.event_id, event.event_sha256),),
        performed_at_utc='2026-10-05T02:00:00+00:00',
    )
    repository.save_remediation(action)
    assert repository.get_remediation(action.action_id) == action

    qualification = evaluate_mechanical_noise(
        document_id=_DOC, tests=(test,), events=(event,),
        evaluated_at_utc=_TS,
    )
    repository.save_qualification(qualification)
    assert repository.get_qualification(
        qualification.qualification_id
    ) == qualification

    orphan_repo = CadMechanicalNoiseRepository(
        SceneRepository(tmp_path / 'other.sqlite3')
    )
    with pytest.raises(MechanicalNoiseIntegrityError):
        orphan_repo.save_event(event)
    forged = event.model_copy(update={'kind': 'electrical_lighting_buzz'})
    with pytest.raises(MechanicalNoiseIntegrityError):
        repository.save_event(forged)


# ---------------------------------------------------------------------------
# SOA10/20 — seat model evidence layers stay honest
# ---------------------------------------------------------------------------


def test_soa10_geometry_and_acoustics_are_separate_layers() -> None:
    model = _seat_model(source='measured_product')
    assert model.geometric_model_state == 'evidenced'
    # literature_generic acoustics can never read as measured.
    assert model.acoustic_model_state == 'assumed'
    assumed = _seat_model(source='generic_assumed')
    assert assumed.geometric_model_state == 'assumed'
    bare = build_seat_model(
        document_id=_DOC, seat_entity_id='seat-x',
        geometry=SeatGeometryEvidence(
            seat_entity_id='seat-x', source='unknown'
        ),
    )
    assert bare.geometric_model_state == 'unknown'
    assert bare.acoustic_model_state == 'unknown'


def test_soa10_generic_evidence_cannot_be_measurement_grade() -> None:
    for cls in ('literature_generic', 'user_assumed', 'unknown'):
        with pytest.raises(ValueError, match='measurement_grade'):
            SeatAcousticEvidence(
                evidence_class=cls, measurement_grade=True
            )
    ok = SeatAcousticEvidence(
        evidence_class='lab_measured_seating_block',
        measurement_grade=True,
        method_ref='ISO 354 reverberation room',
        band_spec='octave',
        band_coefficients={'500': 0.9},
    )
    assert ok.measurement_grade


# ---------------------------------------------------------------------------
# SOA20 — geometric clearance as eligibility gate
# ---------------------------------------------------------------------------


def _listener(z: float = 1.1) -> ListenerEarAuthority:
    return ListenerEarAuthority(
        listener_ref='listener-1',
        seat_entity_id='seat-1',
        nominal_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=z),
        source='design_nominal',
    )


_FRONT_ROW_BACKREST = PathOccluder(
    occluder_id='front-row-backrest', kind='seat_back',
    origin=Position3(x_m=1.0, y_m=0.8, z_m=0.9),
    size=Offset3(x_m=0.3, y_m=0.5, z_m=0.45),
)


def test_soa20_occluded_path_fails_closed() -> None:
    # The front-row backrest tops out at z=1.35 — a low surround at
    # z=1.2 aims straight through it toward the ear at z=1.1.
    speaker = Position3(x_m=0.0, y_m=1.0, z_m=1.2)
    result = evaluate_clearance_path(
        speaker_id='surround-left',
        speaker_position=speaker,
        listener=_listener(),
        occluders=(_FRONT_ROW_BACKREST,),
    )
    assert result.status == 'occluded'
    assert result.failure_reason == 'seat_back_occlusion'
    assert result.obstructor_ids == ('front-row-backrest',)


def test_soa20_clear_and_tolerance_risk_states() -> None:
    # At z=1.8 the descending ray clears the backrest top by ~5 cm at the
    # box's far edge — inside the 10 cm position-tolerance margin, so a
    # posture/ear-height drift could block it.
    result = evaluate_clearance_path(
        speaker_id='front-left',
        speaker_position=Position3(x_m=0.0, y_m=1.0, z_m=1.8),
        listener=_listener(),
        occluders=(_FRONT_ROW_BACKREST,),
    )
    assert result.status == 'clear_with_position_tolerance_risk'
    assert result.obstructor_ids == ('front-row-backrest',)
    far = evaluate_clearance_path(
        speaker_id='front-left',
        speaker_position=Position3(x_m=0.0, y_m=1.0, z_m=3.5),
        listener=_listener(),
        occluders=(_FRONT_ROW_BACKREST,),
    )
    assert far.status == 'clear'


def test_soa20_unknown_geometry_is_honest() -> None:
    result = evaluate_clearance_path(
        speaker_id='front-left',
        speaker_position=Position3(x_m=0.0, y_m=1.0, z_m=1.2),
        listener=ListenerEarAuthority(listener_ref='listener-1'),
        occluders=(),
    )
    assert result.status == 'unknown_geometry'
    assert result.method == 'not_evaluated'


# ---------------------------------------------------------------------------
# SOA30 — occupancy comparability
# ---------------------------------------------------------------------------


def test_soa30_occupancy_comparability_is_content_derived() -> None:
    full = _occupancy('full_occupancy', occupied=('seat-1', 'seat-2'))
    full_copy = _occupancy('full_occupancy', occupied=('seat-1', 'seat-2'))
    assert evaluate_occupancy_comparability(full, full_copy) == 'compatible'
    assert full.comparability_key == full_copy.comparability_key

    partial = _occupancy('partial_occupancy', occupied=('seat-1',))
    assert evaluate_occupancy_comparability(full, partial) == 'incompatible'
    unknown = _occupancy('unknown')
    assert evaluate_occupancy_comparability(full, unknown) == 'unknown'


# ---------------------------------------------------------------------------
# SOA40 — commissioning verdicts carry enumerable failure reasons
# ---------------------------------------------------------------------------


def test_soa40_commissioning_reports_specific_reasons() -> None:
    model = _seat_model()
    result = evaluate_commissioning_verdict(
        seat_models=(model,),
        measured_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=0.75),
        design_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=1.15),
        occupancy_matches=False,
        clearance_failure_reasons=('seat_back_occlusion',),
        document_id=_DOC,
        measured_at_utc=_TS,
    )
    assert result.verdict == 'deviated'
    assert set(result.failure_reasons) == {
        'seat_back_occlusion',
        'ear_height_outside_design_range',
        'occupancy_state_mismatch',
    }


def test_soa40_matching_as_built_is_clean() -> None:
    model = _seat_model()
    result = evaluate_commissioning_verdict(
        seat_models=(model,),
        measured_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=1.15),
        design_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=1.15),
        occupancy_matches=True,
        clearance_failure_reasons=(),
        document_id=_DOC,
        measured_at_utc=_TS,
    )
    assert result.verdict == 'matches_design'
    assert result.failure_reasons == ()


def test_soa40_unknown_seat_model_is_reported() -> None:
    bare = build_seat_model(
        document_id=_DOC, seat_entity_id='seat-x',
        geometry=SeatGeometryEvidence(
            seat_entity_id='seat-x', source='unknown'
        ),
    )
    result = evaluate_commissioning_verdict(
        seat_models=(bare,),
        measured_ear_position=None,
        design_ear_position=None,
        occupancy_matches=None,
        clearance_failure_reasons=(),
        document_id=_DOC,
        measured_at_utc=_TS,
    )
    assert 'seating_acoustic_model_unknown' in result.failure_reasons


# ---------------------------------------------------------------------------
# SOA50/60 — repository integrity
# ---------------------------------------------------------------------------


def test_soa50_repository_round_trip_and_seal(tmp_path: Path) -> None:
    repository = CadSeatingAcousticsRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    model = _seat_model()
    repository.save_seat_model(model)
    assert repository.get_seat_model(model.seat_model_id) == model
    assert repository.seat_models_for_entity('seat-1') == (model,)

    occupancy = _occupancy('full_occupancy', occupied=('seat-1',))
    repository.save_occupancy_scenario(occupancy)
    assert repository.get_occupancy_scenario(
        occupancy.occupancy_scenario_id
    ) == occupancy

    listener = _listener()
    clearance = build_clearance_evaluation(
        document_id=_DOC,
        occupancy_scenario=occupancy,
        listener_ref=listener.listener_ref,
        results=(
            evaluate_clearance_path(
                speaker_id='front-left',
                speaker_position=Position3(x_m=0.0, y_m=1.0, z_m=3.5),
                listener=listener,
                occluders=(),
            ),
        ),
        evaluated_at_utc=_TS,
    )
    repository.save_clearance_evaluation(clearance)
    assert repository.get_clearance_evaluation(
        clearance.evaluation_id
    ) == clearance

    result = evaluate_commissioning_verdict(
        seat_models=(model,),
        measured_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=1.15),
        design_ear_position=Position3(x_m=2.3, y_m=1.0, z_m=1.15),
        occupancy_matches=True,
        clearance_failure_reasons=(),
        document_id=_DOC,
        measured_at_utc=_TS,
    )
    repository.save_commissioning_result(result)
    assert repository.get_commissioning_result(result.result_id) == result

    orphan_repo = CadSeatingAcousticsRepository(
        SceneRepository(tmp_path / 'other.sqlite3')
    )
    with pytest.raises(SeatingAcousticsIntegrityError):
        orphan_repo.save_clearance_evaluation(clearance)
    forged = occupancy.model_copy(update={'state': 'empty'})
    with pytest.raises(SeatingAcousticsIntegrityError):
        repository.save_occupancy_scenario(forged)


def test_soa60_occupancy_rejects_double_booked_seat() -> None:
    with pytest.raises(ValueError, match='occupied and unoccupied'):
        build_occupancy_scenario(
            document_id=_DOC, label='dup', state='custom',
            occupied_seat_ids=('seat-1',),
            unoccupied_seat_ids=('seat-1',),
            created_at_utc=_TS,
        )


def test_sealed_ids_are_content_derived() -> None:
    a = _test()
    b = _test()
    assert a.test_id == b.test_id
    c = _test(level=100.0)
    assert c.test_id != a.test_id
    m1 = _seat_model()
    m2 = _seat_model(source='generic_assumed')
    assert m1.seat_model_id != m2.seat_model_id
