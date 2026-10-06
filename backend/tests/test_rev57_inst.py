"""REV57-INST regression tests: #616 HVAC co-design, #618 playback
reference calibration, #631 as-built treatment, #612 tactile/seat
vibration authorities."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_hvac_authority import (
    CadHvacPathLeg,
    CadHvacPathNode,
    build_component_evidence,
    build_field_observation,
    build_hvac_path,
    build_ventilation_scenario,
    evaluate_hvac_path,
)
from htdt.cad_hvac_repository import (
    CadHvacRepository,
    HvacConflictError,
    HvacIntegrityError,
)
from htdt.cad_playback_reference_authority import (
    build_calibration_stimulus,
    build_channel_observation,
    build_reference_profile,
    evaluate_reference_calibration,
)
from htdt.cad_playback_reference_repository import (
    CadPlaybackReferenceRepository,
    RefCalConflictError,
    RefCalIntegrityError,
)
from htdt.cad_treatment_asbuilt_authority import (
    CadParameterObservation,
    build_asbuilt_observation,
    build_inspection,
    build_install_spec,
    evaluate_treatment_asbuilt,
)
from htdt.cad_treatment_asbuilt_repository import (
    CadTreatmentAsBuiltRepository,
    TreatmentAsBuiltConflictError,
    TreatmentAsBuiltIntegrityError,
)
from htdt.cad_tactile_vibration_authority import (
    CadTactilePathLeg,
    CadTactilePathNode,
    build_tactile_path,
    build_tactile_profile,
    build_vibration_measurement,
    evaluate_tactile_vibration,
)
from htdt.cad_tactile_vibration_repository import (
    CadTactileVibrationRepository,
    TactileVibrationConflictError,
    TactileVibrationIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

DOC = 'doc-rev57-inst'
_TS = '2026-10-06T00:00:00+00:00'
_SHA = 'a' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str = 'ext-1') -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=_SHA)


# ---------------------------------------------------------------------------
# #616 HVAC co-design
# ---------------------------------------------------------------------------


def _hvac_nodes(component_id: str | None = None):
    return (
        CadHvacPathNode(node_id='fan', kind='source_fan'),
        CadHvacPathNode(
            node_id='sil',
            kind='silencer',
            component_evidence_id=component_id,
        ),
        CadHvacPathNode(node_id='diff', kind='diffuser'),
    )


def _hvac_legs():
    return (
        CadHvacPathLeg(
            leg_id='l1', from_node='fan', to_node='sil',
            leg_kind='duct_borne_internal',
        ),
        CadHvacPathLeg(
            leg_id='l2', from_node='sil', to_node='diff',
            leg_kind='duct_borne_internal',
        ),
    )


def _hvac_path(**kw):
    kw.setdefault('nodes', _hvac_nodes())
    kw.setdefault('legs', _hvac_legs())
    return build_hvac_path(
        document_id=DOC, path_kind='supply', label='supply A',
        serves_room='theater', declared_at_utc=_TS, **kw,
    )


def _hvac_scenario(required_lps: float | None = 200.0):
    return build_ventilation_scenario(
        document_id=DOC, label='occupied evening',
        occupancy=4, required_supply_flow_lps=required_lps,
        operating_state='normal_occupied',
        requirement_source='project requirement doc',
        declared_at_utc=_TS,
    )


def _hvac_evidence(**kw):
    kw.setdefault('component_kind', 'silencer')
    return build_component_evidence(
        document_id=DOC, declared_at_utc=_TS, **kw
    )


def test_hvac10_quiet_but_underventilated_is_ineligible() -> None:
    """A path can be acoustically quiet and still fail: required
    airflow is a separate eligibility axis (#616)."""
    scenario = _hvac_scenario(200.0)
    path = _hvac_path(scenario=scenario)
    obs = build_field_observation(
        document_id=DOC, path=path, operating_state='normal_occupied',
        measured_supply_flow_lps=80.0, room_noise_db=22.0,
        room_noise_weighting='a', measured_at_utc=_TS,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        observations=(obs,), evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'ineligible_airflow'
    assert qual.airflow_eligibility == 'under_ventilated'


def test_hvac20_iso7235_lab_only_not_installed_truth() -> None:
    """ISO 7235 silencer data is laboratory evidence, never installed
    in-situ truth (#616 / ISO 11820 distinction)."""
    scenario = _hvac_scenario(50.0)
    ev = _hvac_evidence(
        component_kind='silencer', method='iso_7235_laboratory',
        insertion_loss_band_db_json='{"250": 12}',
        regenerated_noise_band_db_json='{"250": 30}',
        pressure_loss_pa=40.0, flow_rate_lps=120.0,
        airflow_evidence_class='manufacturer_declared',
    )
    path = _hvac_path(
        nodes=_hvac_nodes(ev.evidence_id), scenario=scenario,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        evidence=(ev,), evaluated_at_utc=_TS,
    )
    assert qual.acoustic_state == 'lab_evidence_not_installed_truth'
    assert qual.verdict == 'qualified_with_limitations'


def test_hvac30_in_situ_evidence_with_measured_flow_qualifies() -> None:
    scenario = _hvac_scenario(50.0)
    ev = _hvac_evidence(
        component_kind='silencer', method='iso_11820_in_situ',
        insertion_loss_band_db_json='{"250": 10}',
        regenerated_noise_band_db_json='{"250": 28}',
        pressure_loss_pa=45.0, flow_rate_lps=120.0,
        airflow_evidence_class='field_measured',
    )
    path = _hvac_path(
        nodes=_hvac_nodes(ev.evidence_id), scenario=scenario,
        flanking_role='none',
    )
    obs = build_field_observation(
        document_id=DOC, path=path, operating_state='normal_occupied',
        measured_supply_flow_lps=110.0,
        room_noise_spectrum_json='{"125": 35.0}',
        measured_at_utc=_TS,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        evidence=(ev,), observations=(obs,), evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'qualified'
    assert qual.acoustic_state == 'contributions_documented'


def test_hvac40_confirmed_cross_talk_flanking_fails() -> None:
    """A confirmed shared-duct cross-talk route defeats the isolation
    claim regardless of room treatment (#616, #576 boundary)."""
    ev = _hvac_evidence(
        component_kind='duct_element', method='iso_11820_in_situ',
        flow_rate_lps=120.0, airflow_evidence_class='field_measured',
    )
    path = _hvac_path(
        nodes=_hvac_nodes(ev.evidence_id),
        flanking_role='confirmed_path',
        flanking_target_rooms=('bedroom',),
    )
    scenario = _hvac_scenario(50.0)
    obs = build_field_observation(
        document_id=DOC, path=path, measured_supply_flow_lps=110.0,
        measured_at_utc=_TS,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        evidence=(ev,), observations=(obs,), evaluated_at_utc=_TS,
    )
    assert qual.flanking_state == 'flanking_path_open'
    assert qual.verdict == 'failed'


def test_hvac50_tonal_flags_stay_visible() -> None:
    scenario = _hvac_scenario(50.0)
    ev = _hvac_evidence(
        component_kind='silencer', method='iso_11820_in_situ',
        regenerated_noise_band_db_json='{"500": 40}',
        pressure_loss_pa=30.0, tonal_flags=('blade_pass_tone',),
        flow_rate_lps=120.0, airflow_evidence_class='field_measured',
    )
    path = _hvac_path(
        nodes=_hvac_nodes(ev.evidence_id), scenario=scenario,
    )
    obs = build_field_observation(
        document_id=DOC, path=path, measured_supply_flow_lps=110.0,
        room_noise_spectrum_json='{"500": 44.0}', measured_at_utc=_TS,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        evidence=(ev,), observations=(obs,), evaluated_at_utc=_TS,
    )
    assert qual.tonal_state == 'tonal_flags_present'
    assert qual.verdict != 'qualified'


def test_hvac60_unbound_airflow_requirement() -> None:
    """HTDT never invents a ventilation requirement — without a
    scenario the eligibility axis is honestly unbound."""
    path = _hvac_path()
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, evaluated_at_utc=_TS,
    )
    assert qual.airflow_eligibility == 'airflow_requirement_unbound'
    assert qual.verdict == 'insufficient_evidence'


def test_hvac_silencer_not_reduced_to_insertion_loss() -> None:
    """A silencer with only insertion-loss data and no regenerated-
    noise or pressure-loss terms is not valid evidence."""
    with pytest.raises(ValueError, match='regenerated noise'):
        _hvac_evidence(
            component_kind='silencer', method='iso_7235_laboratory',
            insertion_loss_band_db_json='{"250": 12}',
        )


def test_hvac_in_situ_flow_requires_field_measured_class() -> None:
    with pytest.raises(ValueError, match='field_measured'):
        _hvac_evidence(
            component_kind='terminal_device',
            method='iso_11820_in_situ',
            flow_rate_lps=100.0, airflow_evidence_class='design',
        )


def test_hvac_flanking_path_requires_target_rooms() -> None:
    with pytest.raises(ValueError, match='target rooms'):
        _hvac_path(flanking_role='suspected_path')


def test_hvac_broadband_scalar_leg_rejected() -> None:
    """A single broadband duct-loss scalar is not a quantitative
    path — band detail or 'unsupported' is required."""
    with pytest.raises(ValueError, match='band detail|unsupported'):
        build_hvac_path(
            document_id=DOC, path_kind='supply',
            nodes=_hvac_nodes(),
            legs=(
                CadHvacPathLeg(
                    leg_id='l1', from_node='fan', to_node='sil',
                    leg_kind='duct_borne_internal',
                    broadband_loss_db=8.0,
                ),
            ),
        )


def test_hvac_repository_full_chain(tmp_path) -> None:
    repo = CadHvacRepository(_scene_repo(tmp_path))
    scenario = _hvac_scenario(50.0)
    ev = _hvac_evidence(
        component_kind='silencer', method='iso_11820_in_situ',
        regenerated_noise_band_db_json='{"250": 28}',
        pressure_loss_pa=45.0, flow_rate_lps=120.0,
        airflow_evidence_class='field_measured',
    )
    path = _hvac_path(
        nodes=_hvac_nodes(ev.evidence_id), scenario=scenario,
    )
    obs = build_field_observation(
        document_id=DOC, path=path, measured_supply_flow_lps=110.0,
        measured_at_utc=_TS,
    )
    qual = evaluate_hvac_path(
        document_id=DOC, path=path, scenario=scenario,
        evidence=(ev,), observations=(obs,), evaluated_at_utc=_TS,
    )
    repo.save_scenario(scenario)
    repo.save_path(path)
    repo.save_component_evidence(ev)
    repo.save_observation(obs)
    repo.save_qualification(qual)
    assert repo.get_scenario(scenario.scenario_id) == scenario
    assert repo.get_path(path.path_id) == path
    assert repo.get_component_evidence(ev.evidence_id) == ev
    assert repo.get_observation(obs.observation_id) == obs
    assert repo.get_qualification(qual.qualification_id) == qual
    repo.save_observation(obs)  # idempotent


def test_hvac_repository_rejects_unpersisted_path(tmp_path) -> None:
    repo = CadHvacRepository(_scene_repo(tmp_path))
    obs = build_field_observation(
        document_id=DOC, path=_hvac_path(), measured_at_utc=_TS,
    )
    with pytest.raises(HvacConflictError, match='persist'):
        repo.save_observation(obs)


def test_hvac_repository_rejects_forged_record(tmp_path) -> None:
    repo = CadHvacRepository(_scene_repo(tmp_path))
    scenario = _hvac_scenario()
    forged = scenario.model_copy(update={'occupancy': 99})
    with pytest.raises(HvacIntegrityError):
        repo.save_scenario(forged)


# ---------------------------------------------------------------------------
# #618 Playback reference calibration
# ---------------------------------------------------------------------------


def _refcal_profile(**kw):
    kw.setdefault('profile_kind', 'smpte_cinema_reference')
    kw.setdefault(
        'profile_document', 'SMPTE RP 2096-1 pink-noise calibration'
    )
    kw.setdefault('publisher', 'SMPTE')
    kw.setdefault('lifecycle_state', 'published_current')
    kw.setdefault('target_spl_dbc', 85.0)
    kw.setdefault('lfe_in_band_gain_db', 10.0)
    return build_reference_profile(
        document_id=DOC, declared_at_utc=_TS, **kw,
    )


def _refcal_stimulus(**kw):
    return build_calibration_stimulus(
        document_id=DOC, source_kind='external_file',
        signal_class='main_channel', digital_level_dbfs=-20.0,
        spectrum_description='band-limited pink noise 500 Hz–2 kHz',
        declared_at_utc=_TS, **kw,
    )


def _refcal_obs(channel: str, spl: float | None = 85.0, **kw):
    kw.setdefault('measured_spl_db', spl)
    kw.setdefault('weighting', 'c')
    kw.setdefault('time_weighting', 'slow')
    kw.setdefault('integration_quantity', 'leq')
    kw.setdefault('instrument_ref', _ref('instrument', 'slm-1'))
    kw.setdefault('position_ref', _ref('position', 'mlp'))
    return build_channel_observation(
        document_id=DOC, channel_role=channel,
        measured_at_utc=_TS, **kw,
    )


def test_refcal10_internal_undocumented_caps_device_profile() -> None:
    """An AVR-internal tone with undocumented level can never be a
    standardized reference proof (#618)."""
    stimulus = build_calibration_stimulus(
        document_id=DOC, source_kind='device_internal_test',
        signal_class='main_channel', device_identity='avr-x4800h',
        declared_at_utc=_TS,
    )
    obs = _refcal_obs('front_left', stimulus=stimulus)
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=_refcal_profile(),
        stimulus=stimulus, observations=(obs,),
        tolerance_db=0.5, evaluated_at_utc=_TS,
    )
    assert qual.stimulus_state == 'device_internal_undocumented'
    assert qual.verdict == 'device_profile_only'


def test_refcal20_exact_stimulus_complete_semantics_calibrates() -> None:
    profile = _refcal_profile()
    stimulus = _refcal_stimulus()
    observations = (
        _refcal_obs('front_left', 85.0, stimulus=stimulus),
        _refcal_obs('front_right', 85.2, stimulus=stimulus),
        _refcal_obs(
            'lfe', 95.0, stimulus=stimulus,
            signal_class='native_lfe',
            quantity='lfe_in_band_reproduction_gain',
            lfe_in_band_gain_db=10.0,
        ),
    )
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=profile, stimulus=stimulus,
        observations=observations, tolerance_db=0.5,
        evaluated_at_utc=_TS,
    )
    assert qual.stimulus_state == 'exact_stimulus_bound'
    assert qual.measurement_state == 'semantics_complete'
    assert qual.lfe_state == 'in_band_gain_verified'
    assert qual.alignment_state == 'aligned'
    assert qual.verdict == 'reference_calibrated'


def test_refcal30_meter_delta_is_not_inband_gain_proof() -> None:
    """A broadband sub-out meter delta never proves the +10 dB
    in-band LFE reproduction gain (#618 BS.775-4 semantics)."""
    stimulus = _refcal_stimulus()
    obs = _refcal_obs(
        'sub_out', None, stimulus=stimulus, signal_class='native_lfe',
        weighting='unknown',
        time_weighting='unknown', integration_quantity='unknown',
        broadband_meter_delta_db=10.0, instrument_ref=None,
    )
    main = _refcal_obs('front_left', 85.0, stimulus=stimulus)
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=_refcal_profile(),
        stimulus=stimulus, observations=(main, obs),
        tolerance_db=0.5, evaluated_at_utc=_TS,
    )
    assert qual.lfe_state == 'meter_delta_only_not_proof'
    assert qual.verdict != 'reference_calibrated'


def test_refcal40_redirected_bass_cannot_carry_lfe_gain() -> None:
    with pytest.raises(ValueError, match='redirected bass'):
        _refcal_obs(
            'sub_out', signal_class='redirected_bass',
            lfe_in_band_gain_db=10.0,
        )


def test_refcal50_spl_measurement_requires_meter_semantics() -> None:
    with pytest.raises(ValueError, match='weighting'):
        _refcal_obs('front_left', 85.0, weighting='unknown')


def test_refcal60_draft_profile_cannot_qualify() -> None:
    draft = _refcal_profile(
        profile_kind='project_defined',
        lifecycle_state='draft_research_only',
        target_spl_dbc=None, lfe_in_band_gain_db=None,
    )
    with pytest.raises(ValueError, match='research-only'):
        evaluate_reference_calibration(
            document_id=DOC, profile=draft,
            observations=(_refcal_obs('front_left'),),
            evaluated_at_utc=_TS,
        )


def test_refcal70_misaligned_channel_fails() -> None:
    stimulus = _refcal_stimulus()
    observations = (
        _refcal_obs('front_left', 85.0, stimulus=stimulus),
        _refcal_obs('front_right', 91.0, stimulus=stimulus),
    )
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=_refcal_profile(),
        stimulus=stimulus, observations=observations,
        tolerance_db=0.5, evaluated_at_utc=_TS,
    )
    assert qual.alignment_state == 'misaligned'
    assert qual.verdict == 'failed'


def test_refcal80_no_invented_tolerance() -> None:
    """Without a declared tolerance, alignment is honestly
    unverifiable — HTDT does not invent one."""
    stimulus = _refcal_stimulus()
    obs = _refcal_obs('front_left', 85.0, stimulus=stimulus)
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=_refcal_profile(),
        stimulus=stimulus, observations=(obs,),
        evaluated_at_utc=_TS,
    )
    assert qual.alignment_state == 'unverifiable'


def test_refcal_repository_full_chain(tmp_path) -> None:
    repo = CadPlaybackReferenceRepository(_scene_repo(tmp_path))
    profile = _refcal_profile()
    stimulus = _refcal_stimulus()
    obs = _refcal_obs('front_left', 85.0, stimulus=stimulus)
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=profile, stimulus=stimulus,
        observations=(obs,), tolerance_db=0.5, evaluated_at_utc=_TS,
    )
    repo.save_profile(profile)
    repo.save_stimulus(stimulus)
    repo.save_observation(obs)
    repo.save_qualification(qual)
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.get_stimulus(stimulus.stimulus_id) == stimulus
    assert repo.get_observation(obs.observation_id) == obs
    assert repo.get_qualification(qual.qualification_id) == qual
    repo.save_observation(obs)  # idempotent


def test_refcal_repository_rejects_unpersisted_profile(tmp_path) -> None:
    repo = CadPlaybackReferenceRepository(_scene_repo(tmp_path))
    qual = evaluate_reference_calibration(
        document_id=DOC, profile=_refcal_profile(),
        observations=(_refcal_obs('front_left'),),
        evaluated_at_utc=_TS,
    )
    with pytest.raises(RefCalConflictError, match='persist'):
        repo.save_qualification(qual)


def test_refcal_repository_rejects_forged_record(tmp_path) -> None:
    repo = CadPlaybackReferenceRepository(_scene_repo(tmp_path))
    profile = _refcal_profile()
    forged = profile.model_copy(update={'target_spl_dbc': 75.0})
    with pytest.raises(RefCalIntegrityError):
        repo.save_profile(forged)


# ---------------------------------------------------------------------------
# #631 As-built treatment
# ---------------------------------------------------------------------------


def _tai_spec(**kw):
    kw.setdefault('treatment_class', 'porous_absorber')
    kw.setdefault('product_identity', 'AcmePanel 50')
    kw.setdefault('lab_evidence_class', 'iso_354_specimen')
    kw.setdefault(
        'lab_mounting_condition', 'Type A mounting, direct on wall'
    )
    kw.setdefault('acoustic_role', 'early_reflection_control')
    kw.setdefault('surface_ref', 'wall-left')
    kw.setdefault('width_m', 1.2)
    kw.setdefault('height_m', 2.4)
    kw.setdefault('area_m2', 2.88)
    kw.setdefault('thickness_m', 0.05)
    kw.setdefault('air_gap_m', 0.0)
    kw.setdefault('backing', 'drywall')
    kw.setdefault('facing', 'fabric wrap')
    kw.setdefault('mounting_type', 'impaling clips')
    return build_install_spec(
        document_id=DOC, declared_at_utc=_TS, **kw,
    )


def _param(parameter: str, state: str, numeric=None, value=None):
    return CadParameterObservation(
        parameter=parameter, state=state,
        observed_numeric=numeric, observed_value=value,
    )


def test_tai10_matching_asbuilt_qualifies() -> None:
    spec = _tai_spec()
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        installed_product_identity='AcmePanel 50',
        parameters=(
            _param('thickness', 'field_measured', numeric=0.05),
            _param('air_gap', 'field_measured', numeric=0.0),
            _param('area', 'field_measured', numeric=2.88),
            _param('facing', 'field_observed', value='fabric wrap'),
            _param('orientation', 'field_observed', value=spec.pose_json),
            _param('placement', 'field_observed', value='wall-left'),
            _param('backing', 'field_observed', value='drywall'),
        ),
        observed_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'qualified_as_built'
    assert qual.prediction_validity == 'remains_eligible'


def test_tai20_buildup_deviation_stales_prediction() -> None:
    """Installed thickness differing from the spec makes the assumed
    boundary stale — the old prediction never survives (#631)."""
    spec = _tai_spec()
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        parameters=(
            _param('thickness', 'field_measured', numeric=0.10),
            _param('air_gap', 'field_measured', numeric=0.0),
            _param('area', 'field_measured', numeric=2.88),
            _param('facing', 'field_observed', value='fabric wrap'),
            _param('orientation', 'field_observed', value=spec.pose_json),
            _param('placement', 'field_observed', value='wall-left'),
            _param('backing', 'field_observed', value='drywall'),
        ),
        observed_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'prediction_stale'
    assert qual.prediction_validity == 'stale'
    import json as _json

    verdicts = _json.loads(qual.parameter_verdicts_json)
    assert verdicts['thickness'] == 'deviation'
    assert verdicts['area'] == 'match'


def test_tai30_hidden_parameter_never_promoted() -> None:
    """A concealed air gap reads UNKNOWN — presence of a panel is
    not proof of hidden build-up."""
    spec = _tai_spec(air_gap_m=0.05)
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        parameters=(
            _param('thickness', 'field_measured', numeric=0.05),
            _param('air_gap', 'hidden_unverified'),
            _param('area', 'field_measured', numeric=2.88),
            _param('facing', 'field_observed', value='fabric wrap'),
            _param('orientation', 'field_observed', value=spec.pose_json),
            _param('placement', 'field_observed', value='wall-left'),
            _param('backing', 'field_observed', value='drywall'),
        ),
        observed_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'qualified_with_limitations'
    assert qual.prediction_validity == 'limited'


def test_tai40_substitution_is_not_equivalence() -> None:
    spec = _tai_spec()
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        installed_product_identity='GenericPanel X',
        substituted=True,
        substitution_evidence='same alpha-w rating per datasheet',
        parameters=(
            _param('thickness', 'field_measured', numeric=0.05),
            _param('area', 'field_measured', numeric=2.88),
        ),
        observed_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        evaluated_at_utc=_TS,
    )
    assert qual.prediction_validity == 'stale'


def test_tai50_no_observation_is_insufficient() -> None:
    spec = _tai_spec()
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'insufficient_evidence'


def test_tai60_inconsistent_before_after_incompatible() -> None:
    spec = _tai_spec()
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        parameters=(
            _param('thickness', 'field_measured', numeric=0.05),
        ),
        observed_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        before_after_result='inconsistent_with_expected',
        evaluated_at_utc=_TS,
    )
    assert qual.verdict == 'incompatible'


def test_tai_lab_evidence_requires_mounting_condition() -> None:
    with pytest.raises(ValueError, match='mounting'):
        build_install_spec(
            document_id=DOC, treatment_class='porous_absorber',
            lab_evidence_class='iso_354_specimen',
            declared_at_utc=_TS,
        )


def test_tai_custom_assembly_requires_layers() -> None:
    with pytest.raises(ValueError, match='construction'):
        build_install_spec(
            document_id=DOC,
            treatment_class='custom_built_in_assembly',
            declared_at_utc=_TS,
        )


def test_tai_substitution_requires_evidence() -> None:
    spec = _tai_spec()
    with pytest.raises(ValueError, match='evidence'):
        build_asbuilt_observation(
            document_id=DOC, spec=spec, substituted=True,
            observed_at_utc=_TS,
        )


def test_tai_repository_full_chain(tmp_path) -> None:
    repo = CadTreatmentAsBuiltRepository(_scene_repo(tmp_path))
    spec = _tai_spec()
    obs = build_asbuilt_observation(
        document_id=DOC, spec=spec,
        parameters=(
            _param('thickness', 'field_measured', numeric=0.05),
        ),
        observed_at_utc=_TS,
    )
    insp = build_inspection(
        document_id=DOC, observations=(obs,),
        photo_artifact_refs=('sha256:abc',),
        hidden_parameters=('air_gap',), operator='tech-1',
        inspected_at_utc=_TS,
    )
    qual = evaluate_treatment_asbuilt(
        document_id=DOC, spec=spec, observation=obs,
        inspection=insp, evaluated_at_utc=_TS,
    )
    repo.save_spec(spec)
    repo.save_observation(obs)
    repo.save_inspection(insp)
    repo.save_qualification(qual)
    assert repo.get_spec(spec.spec_id) == spec
    assert repo.get_observation(obs.observation_id) == obs
    assert repo.get_inspection(insp.inspection_id) == insp
    assert repo.get_qualification(qual.qualification_id) == qual
    repo.save_spec(spec)  # idempotent


def test_tai_repository_rejects_unpersisted_spec(tmp_path) -> None:
    repo = CadTreatmentAsBuiltRepository(_scene_repo(tmp_path))
    obs = build_asbuilt_observation(
        document_id=DOC, spec=_tai_spec(), observed_at_utc=_TS,
    )
    with pytest.raises(TreatmentAsBuiltConflictError, match='persist'):
        repo.save_observation(obs)


def test_tai_repository_rejects_forged_record(tmp_path) -> None:
    repo = CadTreatmentAsBuiltRepository(_scene_repo(tmp_path))
    spec = _tai_spec()
    forged = spec.model_copy(update={'thickness_m': 0.99})
    with pytest.raises(TreatmentAsBuiltIntegrityError):
        repo.save_spec(forged)


# ---------------------------------------------------------------------------
# #612 Tactile / seat vibration
# ---------------------------------------------------------------------------


def _tv_path(**kw):
    return build_tactile_path(
        document_id=DOC, label='seat-1 tactile',
        seat_ref='seat-1',
        nodes=(
            CadTactilePathNode(
                node_id='src', node_kind='bass_signal_source',
                label='LFE out',
            ),
            CadTactilePathNode(
                node_id='amp', node_kind='amplifier', label='amp',
            ),
            CadTactilePathNode(
                node_id='bk', node_kind='transducer',
                label='BassShaker BST-1',
                device_ref=_ref('tactile_actuator', 'bst-1'),
            ),
            CadTactilePathNode(
                node_id='seat', node_kind='seat_platform',
                label='seat-1',
            ),
            CadTactilePathNode(
                node_id='pan', node_kind='contact_surface',
                label='seat pan',
            ),
        ),
        legs=(
            CadTactilePathLeg(
                leg_id='tl1', from_node_id='src', to_node_id='amp',
                content_mapping='lfe_derived',
                filter_description='LPF 80 Hz',
            ),
            CadTactilePathLeg(
                leg_id='tl2', from_node_id='amp', to_node_id='bk',
            ),
            CadTactilePathLeg(
                leg_id='tl3', from_node_id='bk', to_node_id='seat',
            ),
            CadTactilePathLeg(
                leg_id='tl4', from_node_id='seat', to_node_id='pan',
            ),
        ),
        declared_at_utc=_TS, **kw,
    )


def _tv_meas(path, **kw):
    kw.setdefault('quantity', 'acceleration')
    kw.setdefault('axis', 'z')
    kw.setdefault('occupancy_state', 'occupied_measured')
    kw.setdefault('unit', 'm/s2')
    kw.setdefault('metric', 'rms')
    kw.setdefault('sensor_evidence_class', 'traceable_calibrated')
    kw.setdefault('sensor_ref', _ref('instrument', 'accel-1'))
    return build_vibration_measurement(
        document_id=DOC, path=path, contact_point='seat pan',
        measured_at_utc=_TS, **kw,
    )


def test_tac10_measured_transfer_qualifies() -> None:
    path = _tv_path()
    meas = _tv_meas(
        path, value=0.8, transfer_json='{"20": 0.7, "40": 0.9}',
        tactile_delay_ms=12.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    assert qual.transfer_state == 'measured'
    assert qual.timing_state == 'physically_measured'
    assert qual.verdict == 'qualified'


def test_tac20_unmeasured_never_inferred() -> None:
    """No seat measurement → insufficient evidence; transducer watts
    are never a proxy for seat acceleration (#612)."""
    path = _tv_path()
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, evaluated_at_utc=_TS,
    )
    assert qual.transfer_state == 'unmeasured'
    assert qual.verdict == 'insufficient_evidence'


def test_tac30_dsp_delay_is_not_physical_sync() -> None:
    path = _tv_path()
    meas = _tv_meas(path, value=0.5, tactile_delay_ms=12.0)
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        evaluated_at_utc=_TS,
    )
    assert qual.timing_state == 'dsp_setting_only'
    assert qual.verdict != 'qualified'


def test_tac40_flagged_acoustic_side_effect_fails() -> None:
    path = _tv_path()
    meas = _tv_meas(
        path, value=0.8, transfer_json='{"40": 0.9}',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        acoustic_side_effect_evaluated=True,
        acoustic_side_effect_flagged=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    assert qual.acoustic_side_effect_state == 'evaluated_flagged'
    assert qual.verdict == 'failed'


def test_tac50_building_coupling_independent_outcome() -> None:
    """Desired seat vibration and unwanted building vibration stay
    independent — a good seat never hides structural coupling."""
    path = _tv_path()
    meas = _tv_meas(
        path, value=0.8, transfer_json='{"40": 0.9}',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        building_coupling_excessive=True,
        evaluated_at_utc=_TS,
    )
    assert qual.building_coupling_state == 'evaluated_excessive'
    assert qual.verdict == 'failed'


def test_tac60_fdis_profile_is_research_only() -> None:
    """ISO/FDIS 2631-1 Ed.3 stays research-only until published —
    the 1997+Amd edition is the current production profile."""
    path = _tv_path()
    profile = build_tactile_profile(
        document_id=DOC,
        profile_kind='iso_2631_1_ed3_fdis_draft_research_only',
        label='FDIS Ed.3 draft', declared_at_utc=_TS,
    )
    meas = _tv_meas(
        path, value=0.8, transfer_json='{"40": 0.9}',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        profile=profile,
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    assert qual.profile_verdict == 'research_only'
    assert qual.verdict == 'research_only'


def test_tac70_exposure_limit_exceeded_fails() -> None:
    path = _tv_path()
    profile = build_tactile_profile(
        document_id=DOC, profile_kind='exposure_comfort_limit',
        label='site comfort limit', limit_value=0.5,
        limit_unit='m/s2', declared_at_utc=_TS,
    )
    meas = _tv_meas(
        path, value=0.9, transfer_json='{"40": 0.9}',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        profile=profile,
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    assert qual.profile_verdict == 'exceeds_profile'
    assert qual.verdict == 'failed'


def test_tac80_empty_seat_not_occupied_truth() -> None:
    path = _tv_path()
    meas = _tv_meas(
        path, value=0.6, transfer_json='{"40": 0.8}',
        occupancy_state='empty_seat',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    assert qual.occupancy_state == 'empty_seat'
    assert qual.verdict == 'qualified_with_limitations'


def test_tac_path_requires_transducer() -> None:
    with pytest.raises(ValueError, match='transducer'):
        build_tactile_path(
            document_id=DOC, label='no transducer',
            nodes=(
                CadTactilePathNode(
                    node_id='seat', node_kind='seat_platform',
                    label='seat',
                ),
            ),
            legs=(),
        )


def test_tac_measurement_value_requires_unit() -> None:
    path = _tv_path()
    with pytest.raises(ValueError, match='unit'):
        build_vibration_measurement(
            document_id=DOC, path=path, contact_point='seat pan',
            value=0.5, unit=None, measured_at_utc=_TS,
        )


def test_tac_traceable_sensor_requires_calibration_ref() -> None:
    path = _tv_path()
    with pytest.raises(ValueError, match='sensor'):
        _tv_meas(path, value=0.5, sensor_ref=None)


def test_tac_fdis_profile_cannot_carry_targets() -> None:
    with pytest.raises(ValueError, match='research-only'):
        build_tactile_profile(
            document_id=DOC,
            profile_kind='iso_2631_1_ed3_fdis_draft_research_only',
            label='FDIS draft', target_value=0.5,
            declared_at_utc=_TS,
        )


def test_tac_repository_full_chain(tmp_path) -> None:
    repo = CadTactileVibrationRepository(_scene_repo(tmp_path))
    path = _tv_path()
    profile = build_tactile_profile(
        document_id=DOC, profile_kind='project_defined',
        label='project target', target_value=0.8,
        target_unit='m/s2', declared_at_utc=_TS,
    )
    meas = _tv_meas(
        path, value=0.8, transfer_json='{"40": 0.9}',
        tactile_delay_ms=5.0,
        timebase_ref=_ref('timebase', 'tb-1'),
    )
    qual = evaluate_tactile_vibration(
        document_id=DOC, path=path, measurements=(meas,),
        profile=profile,
        acoustic_side_effect_evaluated=True,
        building_coupling_evaluated=True,
        evaluated_at_utc=_TS,
    )
    repo.save_path(path)
    repo.save_profile(profile)
    repo.save_measurement(meas)
    repo.save_qualification(qual)
    assert repo.get_path(path.path_id) == path
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.get_measurement(meas.measurement_id) == meas
    assert repo.get_qualification(qual.qualification_id) == qual
    repo.save_measurement(meas)  # idempotent


def test_tac_repository_rejects_unpersisted_path(tmp_path) -> None:
    repo = CadTactileVibrationRepository(_scene_repo(tmp_path))
    meas = _tv_meas(_tv_path(), value=0.5)
    with pytest.raises(TactileVibrationConflictError, match='persist'):
        repo.save_measurement(meas)


def test_tac_repository_rejects_forged_record(tmp_path) -> None:
    repo = CadTactileVibrationRepository(_scene_repo(tmp_path))
    path = _tv_path()
    forged = path.model_copy(update={'seat_ref': 'seat-99'})
    with pytest.raises(TactileVibrationIntegrityError):
        repo.save_path(forged)
