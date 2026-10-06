"""REV59-CODEPOLICY regression tests — seating circulation / egress /
accessibility evidence (#746), lighting temporal modulation (#748),
project data privacy & sharing (#722)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_code_policy_repository import (
    CadCodePolicyRepository,
    CodePolicyIntegrityError,
)
from htdt.cad_life_safety import (
    CirculationRoute,
    EgressEvidence,
    ProfessionalApprovalReference,
    ProjectLifeSafetyProfile,
    SeatingAccessibilityRequirement,
    evaluate_egress_claim,
)
from htdt.cad_lighting_tlm import (
    DimmingTemporalProfile,
    LightingTLAAssessment,
    LightingTLMObservation,
    TemporalLightWaveform,
    evaluate_tla_claim,
)
from htdt.cad_project_data_privacy import (
    ExportRedactionManifest,
    ProjectDataClassification,
    RetentionPolicyRecord,
    SensitiveArtifactPolicy,
    evaluate_export_eligibility,
    evaluate_reproducibility_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-codepolicy'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #746 helpers
# ---------------------------------------------------------------------------

def _lsp(**kw) -> ProjectLifeSafetyProfile:
    payload = dict(
        document_id=DOC,
        jurisdiction='Example City, JP',
        project_kind='private_residential',
        adopted_references=('ISO 21542@2021',),
        applicability_decision='applies',
        applicability_basis='declared_by_project_authority',
    )
    payload.update(kw)
    return ProjectLifeSafetyProfile.create(**payload)


def _rte(**kw) -> CirculationRoute:
    p = _lsp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('life_safety', p.profile_id, p.profile_sha256),
        origin_kind='seat',
        origin_ref=_ref('seat', 'seat-1'),
        destination_kind='door',
        destination_ref=_ref('door', 'door-1'),
        segments=('aisle_accessway', 'aisle', 'door_opening', 'exit'),
        furniture_state='footrest_extended',
        min_clear_width_m=0.9,
        door_clear_opening_m=0.8,
        travel_length_m=6.0,
    )
    payload.update(kw)
    return CirculationRoute.create(**payload)


def _acr(**kw) -> SeatingAccessibilityRequirement:
    p = _lsp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('life_safety', p.profile_id, p.profile_sha256),
        required_items=('wheelchair_space', 'companion_seat',
                        'equivalent_sightline'),
        sightline_equivalence_required=True,
    )
    payload.update(kw)
    return SeatingAccessibilityRequirement.create(**payload)


def _egx(**kw) -> EgressEvidence:
    r = _rte()
    payload = dict(
        document_id=DOC,
        route_refs=(_ref('route', r.route_id, r.route_sha256),),
        evidence_class='field_verified',
        observation_basis='field_observed',
    )
    payload.update(kw)
    return EgressEvidence.create(**payload)


def _appr(**kw) -> ProfessionalApprovalReference:
    p = _lsp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('life_safety', p.profile_id, p.profile_sha256),
        approver_role='licensed_architect',
        approver_name='A. Example',
        approval_scope='seating egress review',
        verdict='approved',
        support_ref=_ref('approval_doc', 'doc-9'),
    )
    payload.update(kw)
    return ProfessionalApprovalReference.create(**payload)


# ---------------------------------------------------------------------------
# #748 helpers
# ---------------------------------------------------------------------------

def _dtp(**kw) -> DimmingTemporalProfile:
    payload = dict(
        document_id=DOC,
        luminaire_ref=_ref('luminaire', 'led-1'),
        dimmer_ref=_ref('dimmer', 'dim-1'),
        control_scene='movie',
        commanded_dim_level_pct=10.0,
        warm_up_state='warmed',
    )
    payload.update(kw)
    return DimmingTemporalProfile.create(**payload)


def _tlw(**kw) -> TemporalLightWaveform:
    p = _dtp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('dimming', p.profile_id, p.profile_sha256),
        artifact_ref=_ref('waveform_file', 'wf-1'),
        instrument_ref=_ref('photometer', 'inst-1'),
        sample_rate_hz=20000.0,
        bandwidth_hz=2000.0,
        duration_s=5.0,
        illuminance_lx=150.0,
    )
    payload.update(kw)
    return TemporalLightWaveform.create(**payload)


def _tlmo(**kw) -> LightingTLMObservation:
    w = _tlw()
    payload = dict(
        document_id=DOC,
        waveform_ref=_ref('waveform', w.waveform_id, w.waveform_sha256),
        phenomenon='flicker',
        modulation_source='intrinsic',
        dominant_frequency_hz=120.0,
    )
    payload.update(kw)
    return LightingTLMObservation.create(**payload)


def _tlaa(**kw) -> LightingTLAAssessment:
    o = _tlmo()
    payload = dict(
        document_id=DOC,
        observation_refs=(
            _ref('tlm_obs', o.observation_id, o.observation_sha256),),
        metric_id='PstLM',
        metric_source='IEC TR 61547-1:2020',
        applicability_domain='flickermeter_61547',
        verdict='within_domain_pass',
    )
    payload.update(kw)
    return LightingTLAAssessment.create(**payload)


# ---------------------------------------------------------------------------
# #722 helpers
# ---------------------------------------------------------------------------

def _pdc(**kw) -> ProjectDataClassification:
    payload = dict(
        document_id=DOC,
        artifact_ref=_ref('artifact', 'scan-1'),
        data_class='client_confidential',
        review_state='reviewed',
    )
    payload.update(kw)
    return ProjectDataClassification.create(**payload)


def _sap(**kw) -> SensitiveArtifactPolicy:
    payload = dict(
        document_id=DOC,
        target_classes=('client_confidential',),
        allow_view_roles=('owner', 'installer'),
        allow_export_roles=('owner',),
        allow_share_roles=(),
    )
    payload.update(kw)
    return SensitiveArtifactPolicy.create(**payload)


def _erm(**kw) -> ExportRedactionManifest:
    payload = dict(
        document_id=DOC,
        bundle_kind='client_package',
        included_refs=(_ref('artifact', 'scan-1'),),
    )
    payload.update(kw)
    return ExportRedactionManifest.create(**payload)


def _rtn(**kw) -> RetentionPolicyRecord:
    payload = dict(
        document_id=DOC,
        artifact_ref=_ref('artifact', 'scan-1'),
        retention_class='retain_until_handover',
    )
    payload.update(kw)
    return RetentionPolicyRecord.create(**payload)


class TestLifeSafety:
    def test_sealed_create(self) -> None:
        p = _lsp()
        assert p.profile_id.startswith('lsp-')
        assert _rte().route_id.startswith('rte-')
        assert _acr().requirement_id.startswith('acr-')
        assert _egx().evidence_id.startswith('egx-')
        assert _appr().approval_id.startswith('appr-')

    def test_room_label_inference_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _lsp(applicability_basis='inferred_from_room_label')

    def test_applies_needs_pinned_editions(self) -> None:
        with pytest.raises(ValidationError):
            _lsp(adopted_references=())
        with pytest.raises(ValidationError):
            _lsp(adopted_references=('IBC',))

    def test_route_must_terminate_at_egress(self) -> None:
        with pytest.raises(ValidationError):
            _rte(segments=('aisle', 'seat'))
        with pytest.raises(ValidationError):
            _rte(segments=())

    def test_stale_evidence_needs_staling_ref(self) -> None:
        with pytest.raises(ValidationError):
            _egx(evidence_class='stale_after_change')
        ok = _egx(evidence_class='stale_after_change',
                  staling_ref=_ref('change', 'chg-1'))
        assert ok.evidence_class == 'stale_after_change'

    def test_decided_approval_needs_support(self) -> None:
        with pytest.raises(ValidationError):
            _appr(support_ref=None)
        pending = _appr(verdict='pending', support_ref=None)
        assert pending.verdict == 'pending'

    def test_fit_not_compliance(self) -> None:
        verdict, _ = evaluate_egress_claim(
            None, (), None, None, cad_fit_ok=True)
        assert verdict == 'geometry_only_not_compliance'

    def test_undecided_applicability(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(applicability_decision='undecided'),
            (_rte(),), _egx(), _appr())
        assert verdict == 'insufficient_evidence'

    def test_stale_evidence_wins(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(), (_rte(),),
            _egx(evidence_class='stale_after_change',
                 staling_ref=_ref('change', 'chg-1')),
            _appr())
        assert verdict == 'stale_after_change'

    def test_reviewed_without_approval(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(), (_rte(),), _egx(), None)
        assert verdict == 'professional_review_required'

    def test_professional_rejection(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(), (_rte(),), _egx(),
            _appr(verdict='rejected'))
        assert verdict == 'professional_rejected'

    def test_approved_path(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(), (_rte(),), _egx(), _appr())
        assert verdict == 'compliance_professionally_approved'

    def test_approved_with_limitations(self) -> None:
        verdict, _ = evaluate_egress_claim(
            _lsp(), (_rte(),), _egx(),
            _appr(verdict='approved_with_limitations',
                  deviations=('egress width waiver',)))
        assert verdict == 'approved_with_limitations'


class TestLightingTLM:
    def test_sealed_create(self) -> None:
        assert _dtp().profile_id.startswith('dtp-')
        assert _tlw().waveform_id.startswith('tlw-')
        assert _tlmo().observation_id.startswith('tlmo-')
        assert _tlaa().assessment_id.startswith('tlaa-')

    def test_dim_level_range(self) -> None:
        with pytest.raises(ValidationError):
            _dtp(commanded_dim_level_pct=120.0)

    def test_power_induced_needs_event(self) -> None:
        with pytest.raises(ValidationError):
            _tlmo(modulation_source='power_quality_induced')
        ok = _tlmo(modulation_source='power_quality_induced',
                   power_event_ref=_ref('power_event', 'pq-1'))
        assert ok.modulation_source == 'power_quality_induced'

    def test_superseded_cie249_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _tlaa(metric_source='CIE 249:2022')

    def test_ieee1789_historical_only(self) -> None:
        with pytest.raises(ValidationError):
            _tlaa(metric_source='IEEE 1789-2015',
                  applicability_domain='general')
        ok = _tlaa(metric_source='IEEE 1789-2015',
                   metric_id='other',
                   applicability_domain='historical_ieee1789')
        assert ok.applicability_domain == 'historical_ieee1789'

    def test_svm_domain_binding(self) -> None:
        with pytest.raises(ValidationError):
            _tlaa(metric_source='IEC TR 63158:2018',
                  metric_id='SVM',
                  applicability_domain='general')

    def test_mp_research_scoped(self) -> None:
        with pytest.raises(ValidationError):
            _tlaa(metric_id='Mp', metric_source='CIE 249:2022-Cor1',
                  applicability_domain='general')

    def test_photometry_is_not_tla(self) -> None:
        verdict, _ = evaluate_tla_claim(
            None, None, None, None, photometry_ok=True)
        assert verdict == 'illuminance_is_not_tla'

    def test_no_waveform_insufficient(self) -> None:
        verdict, _ = evaluate_tla_claim(
            _dtp(), None, None, None)
        assert verdict == 'insufficient_evidence'

    def test_bandwidth_gate(self) -> None:
        obs = _tlmo(dominant_frequency_hz=8000.0)
        verdict, _ = evaluate_tla_claim(
            _dtp(), _tlw(bandwidth_hz=2000.0), obs, _tlaa())
        assert verdict == 'instrument_bandwidth_insufficient'

    def test_low_light_svm_outside_domain(self) -> None:
        assessment = _tlaa(
            metric_source='IEC TR 63158:2018', metric_id='SVM',
            applicability_domain='svm_indoor_gt100lx')
        verdict, reason = evaluate_tla_claim(
            _dtp(), _tlw(illuminance_lx=40.0), _tlmo(), assessment)
        assert verdict == 'outside_profile_domain'
        assert '100lx' in reason

    def test_cor1_svm_caveat(self) -> None:
        assessment = _tlaa(
            metric_source='CIE 249:2022-Cor1', metric_id='SVM',
            applicability_domain='guidance_only')
        verdict, _ = evaluate_tla_claim(
            _dtp(), _tlw(), _tlmo(), assessment)
        assert verdict == 'limited_by_corrigendum'

    def test_ieee1789_eval_historical(self) -> None:
        assessment = _tlaa(
            metric_source='IEEE 1789-2015', metric_id='other',
            applicability_domain='historical_ieee1789')
        verdict, _ = evaluate_tla_claim(
            _dtp(), _tlw(), _tlmo(), assessment)
        assert verdict == 'historical_reference_only'

    def test_power_induced(self) -> None:
        obs = _tlmo(modulation_source='power_quality_induced',
                    power_event_ref=_ref('power_event', 'pq-1'))
        verdict, _ = evaluate_tla_claim(
            _dtp(), _tlw(), obs, _tlaa())
        assert verdict == 'power_quality_induced'

    def test_within_domain_pass(self) -> None:
        verdict, _ = evaluate_tla_claim(
            _dtp(), _tlw(), _tlmo(), _tlaa())
        assert verdict == 'tla_within_domain'


class TestDataPrivacy:
    def test_sealed_create(self) -> None:
        assert _pdc().classification_id.startswith('pdc-')
        assert _sap().policy_id.startswith('sap-')
        assert _erm().manifest_id.startswith('erm-')
        assert _rtn().record_id.startswith('rtn-')

    def test_unknown_class_not_approvable(self) -> None:
        with pytest.raises(ValidationError):
            _pdc(data_class='unknown_classification',
                 review_state='approved')

    def test_secret_class_needs_rationale(self) -> None:
        with pytest.raises(ValidationError):
            _pdc(data_class='credential_or_secret')
        ok = _pdc(data_class='credential_or_secret',
                  rationale='secret-store reference only')
        assert ok.data_class == 'credential_or_secret'

    def test_secret_never_exportable(self) -> None:
        with pytest.raises(ValidationError):
            _sap(target_classes=('credential_or_secret',),
                 allow_export_roles=('owner',))

    def test_manifest_overlap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _erm(excluded_refs=(_ref('artifact', 'scan-1'),))

    def test_dangling_redaction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _erm(redactions=(('other-art', 'mask', 'reason'),))

    def test_retention_validation(self) -> None:
        with pytest.raises(ValidationError):
            _rtn(retention_class='retain_n_days')
        with pytest.raises(ValidationError):
            _rtn(retention_class='legal_hold')
        with pytest.raises(ValidationError):
            _rtn(reproducibility_state='raw_deleted_hash_retained')

    def test_export_denied_unclassified(self) -> None:
        verdict, _ = evaluate_export_eligibility(
            None, _sap(), _erm(), _ref('artifact', 'scan-1'))
        assert verdict == 'export_denied_unclassified'
        verdict, _ = evaluate_export_eligibility(
            _pdc(data_class='unknown_classification'),
            _sap(), _erm(), _ref('artifact', 'scan-1'))
        assert verdict == 'export_denied_unclassified'

    def test_export_denied_secret(self) -> None:
        verdict, _ = evaluate_export_eligibility(
            _pdc(data_class='credential_or_secret',
                 rationale='secret-store ref'),
            _sap(), _erm(), _ref('artifact', 'scan-1'))
        assert verdict == 'export_denied_secret'

    def test_export_denied_license(self) -> None:
        verdict, _ = evaluate_export_eligibility(
            _pdc(rights_class='licensed_no_redistribution'),
            _sap(), _erm(), _ref('artifact', 'scan-1'))
        assert verdict == 'export_denied_license_restricted'

    def test_export_denied_not_manifested(self) -> None:
        verdict, _ = evaluate_export_eligibility(
            _pdc(), _sap(), _erm(), _ref('artifact', 'other-art'))
        assert verdict == 'export_denied_not_manifested'

    def test_export_requires_redaction(self) -> None:
        verdict, _ = evaluate_export_eligibility(
            _pdc(), _sap(redaction_required_for_export=True),
            _erm(), _ref('artifact', 'scan-1'))
        assert verdict == 'export_requires_redaction'
        verdict, _ = evaluate_export_eligibility(
            _pdc(), _sap(redaction_required_for_export=True),
            _erm(redactions=(('scan-1', 'crop', 'faces'),)),
            _ref('artifact', 'scan-1'))
        assert verdict == 'export_allowed_within_manifest'

    def test_reproducibility_states(self) -> None:
        verdict, _ = evaluate_reproducibility_claim(_rtn())
        assert verdict == 'reproducibility_intact'
        verdict, _ = evaluate_reproducibility_claim(
            _rtn(reproducibility_state='raw_deleted_hash_retained',
                 deleted_utc='2026-10-06T00:00:00+00:00'))
        assert verdict == 'reproducibility_limited'


def _repo(tmp_path: Path) -> CadCodePolicyRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadCodePolicyRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    records = (
        ('lsp', _lsp(), repo.save_life_safety_profile,
         repo.get_life_safety_profile, 'profile_id'),
        ('rte', _rte(), repo.save_circulation_route,
         repo.get_circulation_route, 'route_id'),
        ('acr', _acr(), repo.save_accessibility_requirement,
         repo.get_accessibility_requirement, 'requirement_id'),
        ('egx', _egx(), repo.save_egress_evidence,
         repo.get_egress_evidence, 'evidence_id'),
        ('appr', _appr(), repo.save_professional_approval,
         repo.get_professional_approval, 'approval_id'),
        ('dtp', _dtp(), repo.save_dimming_profile,
         repo.get_dimming_profile, 'profile_id'),
        ('tlw', _tlw(), repo.save_light_waveform,
         repo.get_light_waveform, 'waveform_id'),
        ('tlmo', _tlmo(), repo.save_tlm_observation,
         repo.get_tlm_observation, 'observation_id'),
        ('tlaa', _tlaa(), repo.save_tla_assessment,
         repo.get_tla_assessment, 'assessment_id'),
        ('pdc', _pdc(), repo.save_data_classification,
         repo.get_data_classification, 'classification_id'),
        ('sap', _sap(), repo.save_artifact_policy,
         repo.get_artifact_policy, 'policy_id'),
        ('erm', _erm(), repo.save_export_manifest,
         repo.get_export_manifest, 'manifest_id'),
        ('rtn', _rtn(), repo.save_retention_record,
         repo.get_retention_record, 'record_id'),
    )
    for _name, record, save, get, id_field in records:
        save(record)
        rid = getattr(record, id_field)
        assert get(rid) == record


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _pdc()
    repo.save_data_classification(record)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_project_data_classifications '
            "SET data_class='public_shareable' WHERE classification_id=?",
            (record.classification_id,),
        )
        connection.commit()
    with pytest.raises(CodePolicyIntegrityError):
        repo.get_data_classification(record.classification_id)


def test_codepolicy_tables_exist_after_fresh_migrate(
        tmp_path: Path) -> None:
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
        'cad_life_safety_profiles',
        'cad_circulation_routes',
        'cad_seating_accessibility_requirements',
        'cad_egress_evidence_records',
        'cad_professional_approval_refs',
        'cad_dimming_temporal_profiles',
        'cad_temporal_light_waveforms',
        'cad_lighting_tlm_observations',
        'cad_lighting_tla_assessments',
        'cad_project_data_classifications',
        'cad_sensitive_artifact_policies',
        'cad_export_redaction_manifests',
        'cad_retention_policy_records',
    }
    assert expected <= tables
