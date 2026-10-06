"""REV60-EDGE regression tests — sub-20 Hz / infrasonic acoustics
(#779), external noise ingress / façade isolation (#781), material
fire-safety evidence (#782), accessible media playback (#783)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_edge_repository import (
    CadEdgeAuthorityRepository,
    EdgeIntegrityError,
)
from htdt.cad_ulf_acoustics import (
    InfrasonicMeasurementCapability,
    ULFAcousticObservation,
    ULFSystemQualification,
    UltraLowFrequencyAcousticProfile,
    evaluate_ulf_claim,
)
from htdt.cad_noise_ingress import (
    ExternalNoiseIngressMeasurement,
    ExternalNoiseIngressScenario,
    FacadeElement,
    FacadeTransmissionModel,
    IndoorNoiseIngressQualification,
    NoiseBand,
    evaluate_ingress_claim,
)
from htdt.cad_material_fire_safety import (
    FireSafetyApprovalReference,
    FireSafetyEvidenceProfile,
    InstalledMaterialSafetyRequirement,
    MaterialBuildUp,
    MaterialReactionToFireEvidence,
    evaluate_material_deployability,
)
from htdt.cad_accessible_media import (
    AccessibleMediaProfile,
    AccessiblePlaybackQualification,
    AudioDescriptionPlaybackObservation,
    CaptionPresentationObservation,
    ComponentTrack,
    FallbackResult,
    PlaybackStackIdentity,
    evaluate_accessible_playback,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-edge'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #779 helpers
# ---------------------------------------------------------------------------

def _ulfap(**kw) -> UltraLowFrequencyAcousticProfile:
    payload = dict(
        document_id=DOC,
        request_low_hz=5.0,
        request_high_hz=20.0,
        frequency_resolution_hz=0.5,
        banding='narrowband_fixed',
        door_state='closed',
        window_state='closed',
        hvac_state='off',
        solver_ref=_ref('solver', 'sol-1'),
        solver_validated_low_hz=1.0,
        source_refs=(_ref('subwoofer', 'sub-1'),),
        bass_management_semantics='declared',
    )
    payload.update(kw)
    return UltraLowFrequencyAcousticProfile.create(**payload)


def _imc(**kw) -> InfrasonicMeasurementCapability:
    payload = dict(
        document_id=DOC,
        chain_label='Earthworks M23R + Focusrite',
        microphone_ref=_ref('mic', 'mic-1'),
        calibration_file_ref=_ref('cal_file', 'cal-1'),
        calibration_standard='IEC TR 61094-10:2022',
        valid_low_hz=2.0,
        capability_state='ulf_calibrated',
        interface_states=('none_detected',),
        adc_sample_rate_hz=96000.0,
    )
    payload.update(kw)
    return InfrasonicMeasurementCapability.create(**payload)


def _ulfo(**kw) -> ULFAcousticObservation:
    p = _ulfap()
    c = _imc()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('ulf_profile', p.profile_id, p.profile_sha256),
        capability_ref=_ref(
            'capability', c.capability_id, c.capability_sha256),
        quantity_kind='narrowband_spectrum',
        band_low_hz=4.0,
        band_high_hz=20.0,
        resolution_hz=0.5,
        level_db=94.0,
        uncertainty_db=2.5,
        seat_ref=_ref('seat', 'seat-1'),
        acquisition_duration_s=30.0,
        cycles_observed=120,
        window_kind='hann',
    )
    payload.update(kw)
    return ULFAcousticObservation.create(**payload)


def _ulfq(**kw) -> ULFSystemQualification:
    p = _ulfap()
    c = _imc()
    o = _ulfo()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('ulf_profile', p.profile_id, p.profile_sha256),
        capability_ref=_ref(
            'capability', c.capability_id, c.capability_sha256),
        observation_refs=(
            _ref('ulf_obs', o.observation_id, o.observation_sha256),),
        requested_low_hz=5.0,
        requested_high_hz=20.0,
        qualification_state='measured',
        measured_low_hz=4.0,
        exposure_interpretation='delegated_to_issue_602',
    )
    payload.update(kw)
    return ULFSystemQualification.create(**payload)


class TestUltraLowFrequency:
    def test_sealed_create(self) -> None:
        assert _ulfap().profile_id.startswith('ulfap-')
        assert _imc().capability_id.startswith('imc-')
        assert _ulfo().observation_id.startswith('ulfo-')
        assert _ulfq().qualification_id.startswith('ulfq-')

    def test_profile_band_bounds(self) -> None:
        with pytest.raises(ValidationError):
            _ulfap(request_low_hz=25.0)
        with pytest.raises(ValidationError):
            _ulfap(request_low_hz=10.0, request_high_hz=8.0)
        with pytest.raises(ValidationError):
            _ulfap(request_low_hz=10.0, request_high_hz=25.0)

    def test_calibrated_capability_needs_calibration(self) -> None:
        with pytest.raises(ValidationError):
            _imc(calibration_file_ref=None)
        with pytest.raises(ValidationError):
            _imc(calibration_standard=None)
        ok = _imc(capability_state='audible_band_only',
                  calibration_file_ref=None, calibration_standard=None,
                  valid_low_hz=20.0)
        assert ok.capability_state == 'audible_band_only'

    def test_g_weighted_needs_iso7196(self) -> None:
        with pytest.raises(ValidationError):
            _ulfo(quantity_kind='g_weighted_level')
        ok = _ulfo(quantity_kind='g_weighted_level',
                   method_references=('ISO 7196@1995',))
        assert ok.quantity_kind == 'g_weighted_level'

    def test_measured_state_needs_coverage(self) -> None:
        with pytest.raises(ValidationError):
            _ulfq(measured_low_hz=None)
        with pytest.raises(ValidationError):
            _ulfq(measured_low_hz=10.0)  # above requested low bound
        with pytest.raises(ValidationError):
            _ulfq(observation_refs=())

    def test_no_profile_fails_closed(self) -> None:
        verdict, _ = evaluate_ulf_claim(None, None, (), None)
        assert verdict == 'insufficient_evidence'

    def test_solver_floor_fails_closed(self) -> None:
        verdict, reason = evaluate_ulf_claim(
            _ulfap(solver_validated_low_hz=8.0), _imc(), (_ulfo(),),
            _ulfq())
        assert verdict == 'below_validated_domain'
        assert '8.0' in reason

    def test_audible_only_chain_rejected(self) -> None:
        verdict, _ = evaluate_ulf_claim(
            _ulfap(),
            _imc(capability_state='audible_band_only',
                 calibration_file_ref=None, calibration_standard=None,
                 valid_low_hz=20.0),
            (_ulfo(),), _ulfq())
        assert verdict == 'measurement_chain_inadequate'

    def test_chain_floor_above_request(self) -> None:
        verdict, _ = evaluate_ulf_claim(
            _ulfap(), _imc(valid_low_hz=10.0), (_ulfo(),), _ulfq())
        assert verdict == 'below_measurement_capability'

    def test_verified_path(self) -> None:
        verdict, _ = evaluate_ulf_claim(
            _ulfap(), _imc(), (_ulfo(),), _ulfq())
        assert verdict == 'ulf_capability_verified'

    def test_limited_path(self) -> None:
        verdict, _ = evaluate_ulf_claim(
            _ulfap(), _imc(), (_ulfo(),),
            _ulfq(qualification_state='measured_with_limitations',
                  limitation_reasons=('hvac cycling noted',)))
        assert verdict == 'ulf_capability_verified_limited'


# ---------------------------------------------------------------------------
# #781 helpers
# ---------------------------------------------------------------------------

def _enis(**kw) -> ExternalNoiseIngressScenario:
    payload = dict(
        document_id=DOC,
        source_kind='road_traffic',
        spectrum_bands=(
            NoiseBand(center_hz=63.0, level_db=82.0),
            NoiseBand(center_hz=125.0, level_db=78.0),
        ),
        time_statistic='la_eq',
        duration_s=300.0,
        facade_incidence='oblique',
        source_evidence_ref=_ref('noise_survey', 'ns-1'),
    )
    payload.update(kw)
    return ExternalNoiseIngressScenario.create(**payload)


def _ftm(**kw) -> FacadeTransmissionModel:
    payload = dict(
        document_id=DOC,
        envelope_state='field_observed_state',
        elements=(
            FacadeElement(
                element_kind='window_glazing',
                label='east glazing',
                area_m2=4.0,
                banded_transmission=(
                    NoiseBand(center_hz=125.0, level_db=30.0),),
                transmission_method='field_measurement',
                standard_references=('ISO 16283-3@2016',),
                installation_state='as_built',
            ),),
        ingress_paths=('window_door', 'direct_facade_element'),
        window_state='closed',
        door_state='closed',
        vent_state='closed',
        as_built_ref=_ref('as_built', 'ab-1'),
    )
    payload.update(kw)
    return FacadeTransmissionModel.create(**payload)


def _enim(**kw) -> ExternalNoiseIngressMeasurement:
    s = _enis()
    m = _ftm()
    payload = dict(
        document_id=DOC,
        scenario_ref=_ref('scenario', s.scenario_id, s.scenario_sha256),
        model_ref=_ref('facade_model', m.model_id, m.model_sha256),
        measurement_class='facade_insulation',
        method_standard='ISO 16283-3@2016',
        method_status='current_standard',
        result_bands=(
            NoiseBand(center_hz=125.0, level_db=32.0),),
        domain_state='standard_method_domain',
        window_state='closed',
        door_state='closed',
        receiver_positions=('rp-1', 'rp-2'),
    )
    payload.update(kw)
    return ExternalNoiseIngressMeasurement.create(**payload)


def _iniq(**kw) -> IndoorNoiseIngressQualification:
    s = _enis()
    m = _ftm()
    me = _enim()
    payload = dict(
        document_id=DOC,
        scenario_ref=_ref('scenario', s.scenario_id, s.scenario_sha256),
        model_ref=_ref('facade_model', m.model_id, m.model_sha256),
        measurement_refs=(
            _ref('ingress_meas', me.measurement_id,
                 me.measurement_sha256),),
        predicted_bands=(NoiseBand(center_hz=125.0, level_db=46.0),),
        main_limiting_path='window_door',
        criterion_ref=_ref('room_noise_profile', 'rnp-1'),
        verdict='qualified',
    )
    payload.update(kw)
    return IndoorNoiseIngressQualification.create(**payload)


class TestNoiseIngress:
    def test_sealed_create(self) -> None:
        assert _enis().scenario_id.startswith('enis-')
        assert _ftm().model_id.startswith('ftm-')
        assert _enim().measurement_id.startswith('enim-')
        assert _iniq().qualification_id.startswith('iniq-')

    def test_scenario_needs_bands(self) -> None:
        with pytest.raises(ValidationError):
            _enis(spectrum_bands=())

    def test_field_envelope_needs_as_built(self) -> None:
        with pytest.raises(ValidationError):
            _ftm(envelope_state='qualified_state', as_built_ref=None)

    def test_sub50hz_not_standard_domain(self) -> None:
        with pytest.raises(ValidationError):
            _enim(
                result_bands=(NoiseBand(center_hz=31.5, level_db=40.0),),
                domain_state='standard_method_domain')
        ok = _enim(
            result_bands=(NoiseBand(center_hz=31.5, level_db=40.0),),
            domain_state='extended_lf_diagnostic')
        assert ok.domain_state == 'extended_lf_diagnostic'

    def test_draft_method_rejected_for_standard_domain(self) -> None:
        with pytest.raises(ValidationError):
            _enim(method_standard='ISO/CD 16283-3@Ed2',
                  method_status='draft_research_only',
                  domain_state='standard_method_domain')
        ok = _enim(method_standard='ISO/CD 16283-3@Ed2',
                   method_status='draft_research_only',
                   domain_state='extended_lf_diagnostic')
        assert ok.method_status == 'draft_research_only'

    def test_stale_needs_staling_ref(self) -> None:
        with pytest.raises(ValidationError):
            _iniq(verdict='stale_after_envelope_change')
        ok = _iniq(verdict='stale_after_envelope_change',
                   staling_ref=_ref('envelope_change', 'ec-1'))
        assert ok.verdict == 'stale_after_envelope_change'

    def test_design_envelope_is_not_qualified(self) -> None:
        model = _ftm(envelope_state='design_envelope',
                     as_built_ref=None)
        qual = _iniq(measurement_refs=(),
                     predicted_bands=(
                         NoiseBand(center_hz=125.0, level_db=46.0),))
        verdict, _ = evaluate_ingress_claim(
            _enis(), model, (), qual)
        assert verdict == 'design_model_only'

    def test_below_domain_measurement_fails(self) -> None:
        meas = _enim(
            result_bands=(NoiseBand(center_hz=31.5, level_db=40.0),),
            domain_state='below_validated_domain')
        verdict, _ = evaluate_ingress_claim(
            _enis(), _ftm(), (meas,), _iniq())
        assert verdict == 'below_validated_domain'

    def test_qualified_path(self) -> None:
        verdict, _ = evaluate_ingress_claim(
            _enis(), _ftm(), (_enim(),), _iniq())
        assert verdict == 'ingress_qualified'

    def test_fails_criterion(self) -> None:
        verdict, _ = evaluate_ingress_claim(
            _enis(), _ftm(), (_enim(),),
            _iniq(verdict='fails_project_criterion'))
        assert verdict == 'ingress_fails_criterion'


# ---------------------------------------------------------------------------
# #782 helpers
# ---------------------------------------------------------------------------

def _buildup(**kw) -> MaterialBuildUp:
    payload = dict(
        base_material='melamine foam panel',
        facing='fabric wrap',
        adhesive_or_fastener='clips',
        thickness_mm=50.0,
        density_kg_m3=9.0,
        mounting_method='wall-mounted cleats',
    )
    payload.update(kw)
    return MaterialBuildUp(**payload)


def _fsep(**kw) -> FireSafetyEvidenceProfile:
    payload = dict(
        document_id=DOC,
        jurisdiction='Example City, JP',
        project_class='private_residential',
        applicable_codes=('NFPA 701@2023', 'ISO 11925-2@2026'),
        approval_status='approved',
        reviewer_ref=_ref('ahj', 'ahj-1'),
    )
    payload.update(kw)
    return FireSafetyEvidenceProfile.create(**payload)


def _mrfe(**kw) -> MaterialReactionToFireEvidence:
    payload = dict(
        document_id=DOC,
        material_label='acoustic panel A',
        installed_buildup=_buildup(),
        specimen_buildup=_buildup(),
        evidence_kind='ignitability_test',
        standard_reference='ISO 11925-2@2026',
        standard_status='current',
        evidence_maturity='accredited_lab_report',
        report_ref=_ref('lab_report', 'rep-1'),
        specimen_applicability='directly_applicable',
    )
    payload.update(kw)
    return MaterialReactionToFireEvidence.create(**payload)


def _imsr(**kw) -> InstalledMaterialSafetyRequirement:
    p = _fsep()
    e = _mrfe()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('fire_profile', p.profile_id, p.profile_sha256),
        material_ref=_ref('material', 'mat-1'),
        material_label='acoustic panel A',
        required_evidence_kinds=('ignitability_test',),
        state='required_evidence_present',
        satisfied_by_refs=(
            _ref('fire_evidence', e.evidence_id, e.evidence_sha256),),
    )
    payload.update(kw)
    return InstalledMaterialSafetyRequirement.create(**payload)


def _fsar(**kw) -> FireSafetyApprovalReference:
    r = _imsr()
    payload = dict(
        document_id=DOC,
        requirement_ref=_ref(
            'requirement', r.requirement_id, r.requirement_sha256),
        approver_role='fire_engineer',
        approver_name='B. Example',
        approval_scope='panel installation review',
        verdict='approved',
        support_ref=_ref('approval_doc', 'doc-2'),
    )
    payload.update(kw)
    return FireSafetyApprovalReference.create(**payload)


class TestMaterialFireSafety:
    def test_sealed_create(self) -> None:
        assert _fsep().profile_id.startswith('fsep-')
        assert _mrfe().evidence_id.startswith('mrfe-')
        assert _imsr().requirement_id.startswith('imsr-')
        assert _fsar().approval_id.startswith('fsar-')

    def test_codes_pin_editions(self) -> None:
        with pytest.raises(ValidationError):
            _fsep(applicable_codes=('NFPA 701',))

    def test_decided_status_needs_reviewer(self) -> None:
        with pytest.raises(ValidationError):
            _fsep(reviewer_ref=None)
        ok = _fsep(approval_status='undecided', reviewer_ref=None)
        assert ok.approval_status == 'undecided'

    def test_datasheet_not_directly_applicable(self) -> None:
        with pytest.raises(ValidationError):
            _mrfe(evidence_kind='manufacturer_declaration',
                  evidence_maturity='declaration_only',
                  standard_reference=None, report_ref=None)
        ok = _mrfe(evidence_kind='manufacturer_declaration',
                   evidence_maturity='declaration_only',
                   standard_reference=None, report_ref=None,
                   specimen_applicability='insufficient_evidence')
        assert ok.specimen_applicability == 'insufficient_evidence'

    def test_draft_standard_not_applicable(self) -> None:
        with pytest.raises(ValidationError):
            _mrfe(standard_status='draft_research_only')

    def test_test_kind_needs_report(self) -> None:
        with pytest.raises(ValidationError):
            _mrfe(report_ref=None)

    def test_stale_state_needs_ref(self) -> None:
        with pytest.raises(ValidationError):
            _imsr(state='stale_after_substitution',
                  satisfied_by_refs=())
        ok = _imsr(
            state='stale_after_substitution',
            satisfied_by_refs=(),
            staling_ref=_ref('substitution', 'sub-1'))
        assert ok.state == 'stale_after_substitution'

    def test_missing_evidence_fails_closed(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(state='fire_safety_evidence_required',
                  satisfied_by_refs=()),
            (), None)
        assert verdict == 'fire_safety_evidence_required'

    def test_assembly_mismatch_blocks(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(),
            (_mrfe(specimen_applicability='assembly_differs'),),
            _fsar())
        assert verdict == 'test_assembly_mismatch'

    def test_approval_pending(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(state='ahj_review_required'), (_mrfe(),), None)
        assert verdict == 'approval_pending'

    def test_deployable_path(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(), (_mrfe(),), _fsar())
        assert verdict == 'deployable'


# ---------------------------------------------------------------------------
# #783 helpers
# ---------------------------------------------------------------------------

def _amp(**kw) -> AccessibleMediaProfile:
    payload = dict(
        document_id=DOC,
        content_label='Feature film — night mix',
        edition_or_service='streaming-edition-7',
        language='ja',
        component_tracks=(
            ComponentTrack(
                component_kind='sdh_subtitle',
                track_id='sub-ja-sdh',
                language='ja',
                format_profile='imsc_text_1_3',
                format_revision='IMSC Text Profile@1.3',
                selection_kind='selectable',
            ),
            ComponentTrack(
                component_kind='audio_description',
                track_id='ad-ja',
                language='ja',
                selection_kind='selectable',
            ),
        ),
        captions_enabled=True,
        ad_enabled=True,
        profile_source='user_selected',
    )
    payload.update(kw)
    return AccessibleMediaProfile.create(**payload)


def _cpo(**kw) -> CaptionPresentationObservation:
    p = _amp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'media_profile', p.profile_id, p.profile_sha256),
        component_kind='sdh_subtitle',
        track_id='sub-ja-sdh',
        language='ja',
        format_profile='imsc_text_1_3',
        format_revision='IMSC Text Profile@1.3',
        selection_kind='selectable',
        reached_stage='presentation_verified',
        display_profile_ref=_ref('display_profile', 'dp-1'),
        readability_state='fully_visible',
        cue_time_s=12.5,
        observed_onset_s=12.52,
        observed_offset_s=15.1,
        av_sync_state='in_sync',
        measurement_method='frame_capture_review',
        placement='lower_third',
    )
    payload.update(kw)
    return CaptionPresentationObservation.create(**payload)


def _adpo(**kw) -> AudioDescriptionPlaybackObservation:
    p = _amp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'media_profile', p.profile_id, p.profile_sha256),
        track_id='ad-ja',
        language='ja',
        codec='eac3',
        channel_layout='stereo',
        mix_semantics='separate_narration_mixed_by_player',
        output_path_ref=_ref('output_path', 'hdmi-arc-1'),
        output_state='verified_at_output',
        measurement_method='loopback_capture',
    )
    payload.update(kw)
    return AudioDescriptionPlaybackObservation.create(**payload)


def _apq(**kw) -> AccessiblePlaybackQualification:
    p = _amp()
    c = _cpo()
    a = _adpo()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'media_profile', p.profile_id, p.profile_sha256),
        caption_observation_refs=(
            _ref('caption_obs', c.observation_id, c.observation_sha256),),
        ad_observation_refs=(
            _ref('ad_obs', a.observation_id, a.observation_sha256),),
        stack_identity=PlaybackStackIdentity(
            source_device='streamer-X', os_or_firmware='fw-9.1',
            app_version='app-4.2', display_mode='projector-4k'),
        fallback_results=(
            FallbackResult(
                scenario='standby_resume', preference_survived=True),),
        verdict='verified_accessible_playback',
    )
    payload.update(kw)
    return AccessiblePlaybackQualification.create(**payload)


class TestAccessibleMedia:
    def test_sealed_create(self) -> None:
        assert _amp().profile_id.startswith('amp-')
        assert _cpo().observation_id.startswith('cpo-')
        assert _adpo().observation_id.startswith('adpo-')
        assert _apq().qualification_id.startswith('apq-')

    def test_verified_needs_display_binding(self) -> None:
        with pytest.raises(ValidationError):
            _cpo(display_profile_ref=None)
        with pytest.raises(ValidationError):
            _cpo(readability_state='clipped')
        with pytest.raises(ValidationError):
            _cpo(observed_onset_s=None)

    def test_clipped_cannot_be_verified(self) -> None:
        ok = _cpo(reached_stage='rendered_played',
                  display_profile_ref=_ref('display_profile', 'dp-1'),
                  readability_state='clipped',
                  observed_onset_s=None, observed_offset_s=None)
        assert ok.readability_state == 'clipped'

    def test_ad_verified_needs_output_path(self) -> None:
        with pytest.raises(ValidationError):
            _adpo(output_path_ref=None)
        with pytest.raises(ValidationError):
            _adpo(measurement_method=None)

    def test_verified_qualification_needs_observations(self) -> None:
        with pytest.raises(ValidationError):
            _apq(caption_observation_refs=(), ad_observation_refs=())

    def test_preference_not_persistent_needs_fallback(self) -> None:
        with pytest.raises(ValidationError):
            _apq(verdict='preference_not_persistent',
                 fallback_results=(
                     FallbackResult(scenario='cold_start',
                                    preference_survived=True),))

    def test_presence_is_not_presentation(self) -> None:
        obs = _cpo(reached_stage='content_component_present',
                   display_profile_ref=None,
                   readability_state='unknown',
                   observed_onset_s=None, observed_offset_s=None)
        verdict, reason = evaluate_accessible_playback(
            _amp(), (obs,), (_adpo(),),
            _apq(caption_observation_refs=(
                _ref('caption_obs', obs.observation_id,
                     obs.observation_sha256),)))
        assert verdict == 'presentation_failed'
        assert 'content_component_present' in reason

    def test_routing_failure(self) -> None:
        ad = _adpo(output_state='downmix_lost',
                   output_path_ref=None, measurement_method=None)
        verdict, _ = evaluate_accessible_playback(
            _amp(), (_cpo(),), (ad,), _apq())
        assert verdict == 'routing_failed'

    def test_verified_path(self) -> None:
        verdict, _ = evaluate_accessible_playback(
            _amp(), (_cpo(),), (_adpo(),), _apq())
        assert verdict == 'verified_accessible_playback'

    def test_no_components_not_applicable(self) -> None:
        verdict, _ = evaluate_accessible_playback(
            _amp(component_tracks=()), (), (), None)
        assert verdict == 'not_applicable'


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path) -> CadEdgeAuthorityRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadEdgeAuthorityRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    records = (
        ('ulfap', _ulfap(), repo.save_ulf_profile,
         repo.get_ulf_profile, 'profile_id'),
        ('imc', _imc(), repo.save_infrasonic_capability,
         repo.get_infrasonic_capability, 'capability_id'),
        ('ulfo', _ulfo(), repo.save_ulf_observation,
         repo.get_ulf_observation, 'observation_id'),
        ('ulfq', _ulfq(), repo.save_ulf_qualification,
         repo.get_ulf_qualification, 'qualification_id'),
        ('enis', _enis(), repo.save_ingress_scenario,
         repo.get_ingress_scenario, 'scenario_id'),
        ('ftm', _ftm(), repo.save_facade_model,
         repo.get_facade_model, 'model_id'),
        ('enim', _enim(), repo.save_ingress_measurement,
         repo.get_ingress_measurement, 'measurement_id'),
        ('iniq', _iniq(), repo.save_ingress_qualification,
         repo.get_ingress_qualification, 'qualification_id'),
        ('fsep', _fsep(), repo.save_fire_safety_profile,
         repo.get_fire_safety_profile, 'profile_id'),
        ('mrfe', _mrfe(), repo.save_fire_evidence,
         repo.get_fire_evidence, 'evidence_id'),
        ('imsr', _imsr(), repo.save_material_requirement,
         repo.get_material_requirement, 'requirement_id'),
        ('fsar', _fsar(), repo.save_fire_safety_approval,
         repo.get_fire_safety_approval, 'approval_id'),
        ('amp', _amp(), repo.save_accessible_media_profile,
         repo.get_accessible_media_profile, 'profile_id'),
        ('cpo', _cpo(), repo.save_caption_observation,
         repo.get_caption_observation, 'observation_id'),
        ('adpo', _adpo(), repo.save_ad_observation,
         repo.get_ad_observation, 'observation_id'),
        ('apq', _apq(), repo.save_accessible_qualification,
         repo.get_accessible_qualification, 'qualification_id'),
    )
    for _name, record, save, get, id_field in records:
        save(record)
        rid = getattr(record, id_field)
        assert get(rid) == record


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _imsr()
    repo.save_material_requirement(record)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_installed_material_safety_requirements '
            "SET state='ahj_review_required' "
            'WHERE requirement_id=?',
            (record.requirement_id,),
        )
        connection.commit()
    with pytest.raises(EdgeIntegrityError):
        repo.get_material_requirement(record.requirement_id)


def test_edge_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_ulf_acoustic_profiles',
        'cad_infrasonic_measurement_capabilities',
        'cad_ulf_acoustic_observations',
        'cad_ulf_system_qualifications',
        'cad_external_noise_ingress_scenarios',
        'cad_facade_transmission_models',
        'cad_external_noise_ingress_measurements',
        'cad_indoor_noise_ingress_qualifications',
        'cad_fire_safety_evidence_profiles',
        'cad_material_reaction_to_fire_evidence',
        'cad_installed_material_safety_requirements',
        'cad_fire_safety_approval_refs',
        'cad_accessible_media_profiles',
        'cad_caption_presentation_observations',
        'cad_audio_description_playback_observations',
        'cad_accessible_playback_qualifications',
    }
    assert expected <= tables
