"""REV61-AUTHORITY regression tests — fail-closed authority verdicts
and sealed-store column verification.

Findings fixed here (all probe-verified against unfixed code):
  F1  cad_ulf_acoustics.evaluate_ulf_claim — partial band coverage
      (obs.band_high_hz never checked) reached 'ulf_capability_verified';
      'ulf_characterized_with_limitations' chains reached unqualified
      'ulf_capability_verified'.
  F2  cad_noise_ingress.evaluate_ingress_claim — 'installed_envelope'
      models with only predicted_bands reached 'ingress_qualified',
      defeating the documented design_model_only gate.
  F3  cad_material_fire_safety.evaluate_material_deployability —
      an evidence-present requirement state with zero resolved evidence
      items (or unresolved kinds) reached 'deployable'; evidence graded
      'applicable_with_limitations' did not propagate to the verdict.
  F4  cad_accessible_media.evaluate_accessible_playback — AD
      observations stuck at 'not_verified'/'unknown' output reached
      'verified_accessible_playback'.
  F5  _SealedStore.get — columns whose first payload segment is None
      were skipped, so a tampered column value went undetected; the
      '__document_id__' pseudo-path never matched a field, so the
      document_id column (which scopes list(document_id)) was never
      verified in any of the 27 repositories sharing the pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_collaboration import CollaborationEvent, RevisionAuthorship
from htdt.cad_collaboration_repository import (
    CadCollaborationRepository,
    CollaborationIntegrityError,
)
from htdt.cad_edge_repository import (
    CadEdgeAuthorityRepository,
    EdgeIntegrityError,
)
from htdt.cad_material_fire_safety import (
    FireSafetyApprovalReference,
    FireSafetyEvidenceProfile,
    InstalledMaterialSafetyRequirement,
    MaterialBuildUp,
    MaterialReactionToFireEvidence,
    evaluate_material_deployability,
)
from htdt.cad_noise_ingress import (
    ExternalNoiseIngressScenario,
    FacadeElement,
    FacadeTransmissionModel,
    IndoorNoiseIngressQualification,
    NoiseBand,
    evaluate_ingress_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_ulf_acoustics import (
    InfrasonicMeasurementCapability,
    ULFAcousticObservation,
    ULFSystemQualification,
    UltraLowFrequencyAcousticProfile,
    evaluate_ulf_claim,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-rev61'
_TS = '2026-10-06T00:00:00Z'
_SHA = canonical_sha256({'fixture': 'rev61'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


# ---------------------------------------------------------------------------
# ULF helpers
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


class TestUlfClaimBandCoverage:
    def test_partial_high_band_coverage_fails_closed(self) -> None:
        # Observation stops at 10 Hz while the request runs to 20 Hz:
        # the unfixed code only checked band_low_hz.
        verdict, reason = evaluate_ulf_claim(
            _ulfap(), _imc(), (_ulfo(band_high_hz=10.0),), _ulfq())
        assert verdict == 'insufficient_evidence'
        assert reason == 'no_observation_covers_requested_band'

    def test_full_band_coverage_still_verifies(self) -> None:
        verdict, _ = evaluate_ulf_claim(
            _ulfap(), _imc(), (_ulfo(),), _ulfq())
        assert verdict == 'ulf_capability_verified'

    def test_limited_chain_propagates_limitations(self) -> None:
        verdict, reason = evaluate_ulf_claim(
            _ulfap(),
            _imc(capability_state='ulf_characterized_with_limitations',
                 limitation_notes=('mic roll-off below 3 Hz',)),
            (_ulfo(),), _ulfq())
        assert verdict == 'ulf_capability_verified_limited'
        assert 'mic roll-off' in reason


# ---------------------------------------------------------------------------
# Noise ingress helpers
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


def _iniq(**kw) -> IndoorNoiseIngressQualification:
    s = _enis()
    m = _ftm()
    payload = dict(
        document_id=DOC,
        scenario_ref=_ref('scenario', s.scenario_id, s.scenario_sha256),
        model_ref=_ref('facade_model', m.model_id, m.model_sha256),
        measurement_refs=(_ref('ingress_meas', 'im-1'),),
        predicted_bands=(NoiseBand(center_hz=125.0, level_db=46.0),),
        main_limiting_path='window_door',
        criterion_ref=_ref('room_noise_profile', 'rnp-1'),
        verdict='qualified',
    )
    payload.update(kw)
    return IndoorNoiseIngressQualification.create(**payload)


class TestIngressDesignModelGate:
    def test_installed_envelope_without_measurements_is_model_only(
            self) -> None:
        # 'installed_envelope' is still a model — the gate must apply to
        # every non-field-observed envelope state.
        model = _ftm(envelope_state='installed_envelope')
        qual = _iniq(measurement_refs=())
        verdict, _ = evaluate_ingress_claim(
            _enis(), model, (), qual)
        assert verdict == 'design_model_only'

    def test_installed_envelope_with_measurements_can_qualify(
            self) -> None:
        model = _ftm(envelope_state='installed_envelope')
        verdict, _ = evaluate_ingress_claim(
            _enis(), model, (), _iniq())
        assert verdict == 'ingress_qualified'

    def test_field_observed_without_measurements_can_qualify(
            self) -> None:
        # Regression guard: the widened gate must not block the
        # documented field-observed/qualified states.
        qual = _iniq(measurement_refs=())
        verdict, _ = evaluate_ingress_claim(
            _enis(), _ftm(), (), qual)
        assert verdict == 'ingress_qualified'


# ---------------------------------------------------------------------------
# Material fire safety helpers
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


class TestMaterialDeployabilityEvidence:
    def test_empty_evidence_fails_closed(self) -> None:
        # state claims evidence present; nothing resolved — the unfixed
        # code fell straight through to 'deployable'.
        verdict, reason = evaluate_material_deployability(
            _imsr(), (), _fsar())
        assert verdict == 'fire_safety_evidence_required'
        assert reason == 'required_evidence_kinds_unresolved'

    def test_wrong_evidence_kind_fails_closed(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(),
            (_mrfe(evidence_kind='flame_propagation_test',
                   standard_reference='NFPA 701@2023'),),
            _fsar())
        assert verdict == 'fire_safety_evidence_required'

    def test_limited_specimen_propagates_to_verdict(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(),
            (_mrfe(specimen_applicability='applicable_with_limitations'),),
            _fsar())
        assert verdict == 'deployable_with_limitations'

    def test_fully_applicable_still_deployable(self) -> None:
        verdict, _ = evaluate_material_deployability(
            _imsr(), (_mrfe(),), _fsar())
        assert verdict == 'deployable'


# ---------------------------------------------------------------------------
# Accessible media helpers
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


class TestAccessiblePlaybackAdVerification:
    def test_ad_not_verified_fails_closed(self) -> None:
        ad = _adpo(output_state='not_verified',
                   output_path_ref=None, measurement_method=None)
        verdict, reason = evaluate_accessible_playback(
            _amp(), (_cpo(),), (ad,), _apq())
        assert verdict == 'routing_failed'
        assert reason == 'ad_output_unverified'

    def test_ad_unknown_fails_closed(self) -> None:
        ad = _adpo(output_state='unknown',
                   output_path_ref=None, measurement_method=None)
        verdict, _ = evaluate_accessible_playback(
            _amp(), (_cpo(),), (ad,), _apq())
        assert verdict == 'routing_failed'

    def test_verified_output_still_passes(self) -> None:
        verdict, _ = evaluate_accessible_playback(
            _amp(), (_cpo(),), (_adpo(),), _apq())
        assert verdict == 'verified_accessible_playback'


# ---------------------------------------------------------------------------
# _SealedStore column verification
# ---------------------------------------------------------------------------

def _collab_repo(tmp_path: Path) -> CadCollaborationRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadCollaborationRepository(SceneRepository(db))


def _edge_repo(tmp_path: Path) -> CadEdgeAuthorityRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadEdgeAuthorityRepository(SceneRepository(db))


def _authorship(**kw):
    payload = dict(
        document_id=DOC,
        author_ref=_ref('collaboration_actor', 'a1'),
        parent_revision_id=None,
        result_revision_id='rev-1',
        changed_refs=(_ref('room', 'r1'),),
        recorded_at_utc=_TS,
    )
    payload.update(kw)
    return RevisionAuthorship.create(**payload)


class TestSealedStoreColumnVerification:
    def test_optional_field_column_tamper_detected(
            self, tmp_path: Path) -> None:
        repo = _collab_repo(tmp_path)
        a = _authorship()
        repo.save_authorship(a)
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                "UPDATE cad_revision_authorship "
                "SET parent_revision_id='forged-rev' "
                "WHERE authorship_id=?", (a.authorship_id,))
            conn.commit()
        with pytest.raises(CollaborationIntegrityError):
            repo.get_authorship(a.authorship_id)

    def test_optional_ref_column_tamper_detected(
            self, tmp_path: Path) -> None:
        repo = _collab_repo(tmp_path)
        e = CollaborationEvent.create(
            document_id=DOC, kind='import', actor_ref=None,
            subject_refs=(_ref('room', 'r1'),), recorded_at_utc=_TS)
        repo.save_event(e)
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                "UPDATE cad_collaboration_events "
                "SET actor_ref_id='forged-actor' WHERE event_id=?",
                (e.event_id,))
            conn.commit()
        with pytest.raises(CollaborationIntegrityError):
            repo.get_event(e.event_id)

    def test_document_id_column_tamper_detected(
            self, tmp_path: Path) -> None:
        # '__document_id__' never matched a field, so this column was
        # skipped in every repository binding it (all 27).
        repo = _collab_repo(tmp_path)
        a = _authorship()
        repo.save_authorship(a)
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                "UPDATE cad_revision_authorship "
                "SET document_id='other-doc' WHERE authorship_id=?",
                (a.authorship_id,))
            conn.commit()
        with pytest.raises(CollaborationIntegrityError):
            repo.get_authorship(a.authorship_id)

    def test_edge_store_document_id_tamper_detected(
            self, tmp_path: Path) -> None:
        repo = _edge_repo(tmp_path)
        s = _enis()
        repo.save_ingress_scenario(s)
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                "UPDATE cad_external_noise_ingress_scenarios "
                "SET document_id='other-doc' WHERE scenario_id=?",
                (s.scenario_id,))
            conn.commit()
        with pytest.raises(EdgeIntegrityError):
            repo.get_ingress_scenario(s.scenario_id)

    def test_list_path_document_id_tamper_detected(
            self, tmp_path: Path) -> None:
        # list() filters on the document_id column — the sibling query
        # path verifies it too, or a tampered row silently rescopes.
        repo = _collab_repo(tmp_path)
        a = _authorship()
        repo.save_authorship(a)
        assert repo.authorship.list(DOC) == (a,)
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                "UPDATE cad_revision_authorship "
                "SET document_id='other-doc' WHERE authorship_id=?",
                (a.authorship_id,))
            conn.commit()
        with pytest.raises(CollaborationIntegrityError):
            repo.authorship.list()
        with pytest.raises(CollaborationIntegrityError):
            repo.authorship.list('other-doc')
        assert repo.authorship.list(DOC) == ()

    def test_null_bound_columns_still_read_back(
            self, tmp_path: Path) -> None:
        # Removing the skip must not break legitimate NULL columns.
        repo = _collab_repo(tmp_path)
        a = _authorship()
        repo.save_authorship(a)
        assert repo.get_authorship(a.authorship_id) == a
        e = CollaborationEvent.create(
            document_id=DOC, kind='import', actor_ref=None,
            subject_refs=(_ref('room', 'r1'),), recorded_at_utc=_TS)
        repo.save_event(e)
        assert repo.get_event(e.event_id) == e
