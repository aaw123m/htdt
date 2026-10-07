"""Issue #792 lifecycle/supportability authority tests.

Covers the sealed domain models, every fail-closed evaluator verdict
path, and the ``_SealedStore`` repository contract for
``cad_supportability.py`` / ``cad_supportability_repository.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_supportability import (
    ExternalDependency,
    LicenceEntitlement,
    LifecycleRiskObservation,
    OfflineContinuityEvidence,
    ReplacementReadiness,
    ServiceabilitySnapshot,
    SoftwareAvailability,
    IntegrationDependency,
    SupportabilityProfile,
    DeclaredFunction,
    evaluate_evidence_freshness,
    evaluate_licence_deadline,
    evaluate_offline_continuity,
    evaluate_recovery_capability,
    evaluate_replacement_readiness,
    evaluate_security_maintenance,
)
from htdt.cad_supportability_repository import (
    CadSupportabilityRepository,
    SupportabilityConflictError,
    SupportabilityIntegrityError,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-792'
_SHA = canonical_sha256({'fixture': 'sha'})
_TS = '2026-10-07T00:00:00+00:00'
_LATER = '2027-01-01T00:00:00+00:00'


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _profile(**kw) -> SupportabilityProfile:
    payload = dict(
        document_id=DOC,
        profile_label='installed AVR system',
        declared_at_utc=_TS,
        source_label='commissioning survey',
    )
    payload.update(kw)
    return SupportabilityProfile.create(**payload)


def _dependency(**kw) -> ExternalDependency:
    payload = dict(
        document_id=DOC,
        function_label='remote_app_control',
        dependency_kind='cloud_control',
        provider_label='vendor cloud',
        endpoint_class='vendor_cloud',
        requires_account=True,
        source_label='vendor datasheet',
        observed_at_utc=_TS,
    )
    payload.update(kw)
    return ExternalDependency.create(**payload)


def _observation(**kw) -> LifecycleRiskObservation:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('device', 'avr-1'),
        kind='manufacturer_support_status',
        support_state='currently_supported',
        source_label='vendor support page',
        observed_at_utc=_TS,
    )
    payload.update(kw)
    return LifecycleRiskObservation.create(**payload)


def _continuity(**kw) -> OfflineContinuityEvidence:
    payload = dict(
        document_id=DOC,
        function_label='local_playback',
        condition='internet_unavailable',
        outcome='continue_local',
        test_method='WAN uplink pulled, playback observed',
        authorized=True,
        destructive=False,
        observed_at_utc=_TS,
    )
    payload.update(kw)
    return OfflineContinuityEvidence.create(**payload)


def _readiness(**kw) -> ReplacementReadiness:
    payload = dict(
        document_id=DOC,
        subject_ref=_ref('device', 'avr-1'),
        candidate_label='AVR successor model',
        assessed_at_utc=_TS,
    )
    payload.update(kw)
    return ReplacementReadiness.create(**payload)


def _licence(**kw) -> LicenceEntitlement:
    payload = dict(
        feature_label='room-correction DSP',
        licence_model='subscription',
        licence_state='active',
        binding='account',
        activation_dependency='account_sign_in',
        source_label='licence portal export',
    )
    payload.update(kw)
    return LicenceEntitlement(**payload)


def _software(**kw) -> SoftwareAvailability:
    payload = dict(
        component_label='calibration desktop app',
        installer_obtainable='yes',
        known_good_retained='yes',
        restore_requires_vendor_service='no',
        re_provisionable_after_reset='yes',
        host_os_compatible='yes',
    )
    payload.update(kw)
    return SoftwareAvailability(**payload)


# ---------------------------------------------------------------------------
# seal + validator integrity
# ---------------------------------------------------------------------------

class TestSealIntegrity:
    def test_ids_derive_from_sha(self) -> None:
        for rec, id_f, sha_f, prefix in (
            (_profile(), 'profile_id', 'profile_sha256', 'spro'),
            (_dependency(), 'dependency_id', 'dependency_sha256',
             'exdep'),
            (_observation(), 'observation_id', 'observation_sha256',
             'lro'),
            (_continuity(), 'evidence_id', 'evidence_sha256', 'oce'),
            (_readiness(), 'readiness_id', 'readiness_sha256', 'rpr'),
        ):
            sha = getattr(rec, sha_f)
            assert sha == canonical_sha256(rec.identity_payload())
            assert getattr(rec, id_f) == f'{prefix}-{sha[:24]}'

    def test_payload_change_reseals(self) -> None:
        a = _dependency(dependency_kind='cloud_control')
        b = _dependency(dependency_kind='cloud_authentication')
        assert a.dependency_id != b.dependency_id
        assert a.dependency_sha256 != b.dependency_sha256

    def test_forged_sha_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SupportabilityProfile(
                profile_id='spro-forged',
                profile_sha256='z' * 64,
                document_id=DOC,
                profile_label='x',
                declared_at_utc=_TS,
                source_label='s',
            )

    def test_ref_without_sha_rejected(self) -> None:
        bare = AuthorityRef(kind='device', ref_id='avr-1')
        with pytest.raises(ValueError, match='sha256'):
            _observation(subject_ref=bare)
        with pytest.raises(ValueError, match='sha256'):
            _readiness(subject_ref=bare)
        with pytest.raises(ValueError, match='sha256'):
            DeclaredFunction(function_label='f', device_ref=bare)

    def test_duplicate_function_labels_rejected(self) -> None:
        with pytest.raises(ValueError, match='unique'):
            _profile(functions=(
                DeclaredFunction(function_label='playback'),
                DeclaredFunction(function_label='playback'),
            ))

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match='UTC'):
            _dependency(observed_at_utc='2026-10-07T00:00:00')

    def test_review_by_before_observed_rejected(self) -> None:
        with pytest.raises(ValueError, match='review_by'):
            _observation(review_by_utc='2026-01-01T00:00:00+00:00')

    def test_licence_expiry_before_issue_rejected(self) -> None:
        with pytest.raises(ValueError, match='expires'):
            _licence(
                issued_at_utc=_LATER,
                expires_at_utc=_TS,
            )

    def test_licence_carries_no_secret_fields(self) -> None:
        # #792: identity/state only — no key material field exists.
        assert not any(
            'key' in f or 'secret' in f or 'token' in f
            or 'credential' in f or 'password' in f
            for f in LicenceEntitlement.model_fields)

    def test_observation_kind_requires_detail_section(self) -> None:
        with pytest.raises(ValueError, match='detail'):
            _observation(kind='licence_entitlement_state')
        with pytest.raises(ValueError, match='detail'):
            _observation(kind='driver_api_status')
        with pytest.raises(ValueError, match='detail'):
            _observation(kind='warranty_service_state')
        ok = _observation(
            kind='licence_entitlement_state', licence=_licence())
        assert ok.licence is not None

    def test_unauthorized_continuity_evidence_rejected(self) -> None:
        with pytest.raises(ValueError, match='authorized'):
            _continuity(authorized=False)

    def test_destructive_continuity_evidence_rejected(self) -> None:
        with pytest.raises(ValueError, match='non-destructive'):
            _continuity(destructive=True)


# ---------------------------------------------------------------------------
# evaluate_offline_continuity — DEPENDENCY IMPACT IF UNAVAILABLE
# ---------------------------------------------------------------------------

class TestOfflineContinuity:
    def test_undeclared_function_unknown(self) -> None:
        profile = _profile(functions=(
            DeclaredFunction(function_label='local_playback'),))
        verdict, detail = evaluate_offline_continuity(
            'remote_app', (_continuity(),), profile=profile)
        assert verdict == 'unknown'
        assert 'undeclared_function' in detail

    def test_no_evidence_unknown(self) -> None:
        verdict, detail = evaluate_offline_continuity(
            'local_playback', ())
        assert verdict == 'unknown'
        assert detail == 'no_authorized_continuity_evidence'

    def test_evidence_for_other_function_unknown(self) -> None:
        verdict, _ = evaluate_offline_continuity(
            'voice_assistant', (_continuity(),))
        assert verdict == 'unknown'

    def test_continue_local(self) -> None:
        verdict, detail = evaluate_offline_continuity(
            'local_playback', (_continuity(),))
        assert verdict == 'continues_local'
        assert 'tested:1' in detail

    def test_unavailable_dominates(self) -> None:
        down = _continuity(
            function_label='remote_app_control',
            outcome='unavailable',
            condition='vendor_cloud_unreachable')
        ok = _continuity(function_label='remote_app_control')
        verdict, _ = evaluate_offline_continuity(
            'remote_app_control', (ok, down))
        assert verdict == 'unavailable'

    def test_degraded_beats_unknown(self) -> None:
        deg = _continuity(outcome='degraded')
        unk = _continuity(outcome='unknown')
        verdict, _ = evaluate_offline_continuity(
            'local_playback', (unk, deg))
        assert verdict == 'continues_degraded'

    def test_inconclusive_unknown(self) -> None:
        verdict, detail = evaluate_offline_continuity(
            'local_playback', (_continuity(outcome='unknown'),))
        assert verdict == 'unknown'
        assert 'inconclusive' in detail

    def test_condition_filter(self) -> None:
        wan = _continuity(
            condition='internet_unavailable', outcome='continue_local')
        dns = _continuity(condition='dns_unavailable', outcome='degraded')
        verdict, _ = evaluate_offline_continuity(
            'local_playback', (wan, dns), condition='dns_unavailable')
        assert verdict == 'continues_degraded'

    def test_lfc10_partial_impact(self) -> None:
        # LFC10: internet removed — essential playback continues,
        # remote app unavailable; function-level, never device-global.
        playback = _continuity(function_label='local_playback')
        app_down = _continuity(
            function_label='remote_app_control',
            outcome='unavailable')
        assert evaluate_offline_continuity(
            'local_playback', (playback, app_down))[0] \
            == 'continues_local'
        assert evaluate_offline_continuity(
            'remote_app_control', (playback, app_down))[0] \
            == 'unavailable'


# ---------------------------------------------------------------------------
# evaluate_recovery_capability — known-good composition (#592+)
# ---------------------------------------------------------------------------

class TestRecoveryCapability:
    def test_no_backup_impossible(self) -> None:
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=None, software=_software())
        assert verdict == 'recovery_impossible_with_current_evidence'
        assert detail == 'no_known_good_backup_evidence'

    def test_external_restore_dependency(self) -> None:
        dep = _dependency(
            required_for_restore=True, endpoint_class='vendor_cloud')
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            restore_dependencies=(dep,))
        assert verdict == 'recovery_requires_external_service'
        assert 'vendor cloud' in detail

    def test_software_vendor_service_restore(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(restore_requires_vendor_service='yes'))
        assert verdict == 'recovery_requires_external_service'

    def test_lfc20_cloud_provisioning_no_offline_recovery(self) -> None:
        # LFC20: works now but factory-reset recovery needs the vendor
        # cloud — a backup file alone cannot prove offline recovery.
        dep = _dependency(
            dependency_kind='vendor_activation',
            endpoint_class='vendor_cloud', required_for_restore=True)
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            restore_dependencies=(dep,))
        assert verdict == 'recovery_requires_external_service'

    def test_declared_licence_unevidenced_unknown(self) -> None:
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            licence_required=True)
        assert verdict == 'unknown'
        assert 'unevidenced' in detail

    def test_licence_activation_requires_entitlement(self) -> None:
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            licence=_licence(
                licence_model='perpetual',
                activation_dependency='vendor_activation_server'))
        assert verdict == 'recovery_requires_active_entitlement'
        assert 'room-correction DSP' in detail

    def test_subscription_requires_entitlement(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            licence=_licence(activation_dependency='none'))
        assert verdict == 'recovery_requires_active_entitlement'

    def test_no_software_evidence_unknown(self) -> None:
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'), software=None)
        assert verdict == 'unknown'
        assert detail == 'no_software_availability_evidence'

    def test_software_gap_impossible(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(installer_obtainable='no'))
        assert verdict == 'recovery_impossible_with_current_evidence'

    def test_software_unknown_fails_closed(self) -> None:
        verdict, detail = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(installer_obtainable='unknown'))
        assert verdict == 'unknown'
        assert 'incomplete' in detail

    def test_restore_tested_verified(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            restore_test=_continuity(
                condition='vendor_cloud_unreachable'))
        assert verdict == 'offline_recovery_verified'

    def test_inconclusive_restore_test_unknown(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software(),
            restore_test=_continuity(outcome='degraded'))
        assert verdict == 'unknown'

    def test_untested_when_all_evidenced(self) -> None:
        verdict, _ = evaluate_recovery_capability(
            backup_restore_path_ref=_ref('backup', 'b1'),
            software=_software())
        assert verdict == 'recovery_untested'


# ---------------------------------------------------------------------------
# evaluate_evidence_freshness — staleness / supersession (#765)
# ---------------------------------------------------------------------------

class TestEvidenceFreshness:
    def test_superseded(self) -> None:
        obs = _observation(superseded_by_ref=_ref('observation', 'new-1'))
        verdict, detail = evaluate_evidence_freshness(obs, _TS)
        assert verdict == 'superseded'
        assert 'new-1' in detail

    def test_no_horizon_unknown(self) -> None:
        verdict, _ = evaluate_evidence_freshness(_observation(), _TS)
        assert verdict == 'unknown'

    def test_review_due(self) -> None:
        obs = _observation(review_by_utc=_TS)
        verdict, _ = evaluate_evidence_freshness(obs, _LATER)
        assert verdict == 'review_due'

    def test_current(self) -> None:
        obs = _observation(review_by_utc=_LATER)
        verdict, _ = evaluate_evidence_freshness(obs, _TS)
        assert verdict == 'current'

    def test_lfc70_support_source_staleness(self) -> None:
        # LFC70: an old 'currently supported' page is superseded by a
        # later EOL notice (#765) — stale pages are not eternal truth.
        stale = _observation(review_by_utc='2026-10-07T12:00:00+00:00')
        assert evaluate_evidence_freshness(stale, _LATER)[0] \
            == 'review_due'
        newer = _observation(support_state='end_of_software_updates')
        superseded = _observation(superseded_by_ref=_ref(
            'observation', newer.observation_id))
        assert evaluate_evidence_freshness(superseded, _TS)[0] \
            == 'superseded'


# ---------------------------------------------------------------------------
# evaluate_security_maintenance — routes #598, never "compromised"
# ---------------------------------------------------------------------------

class TestSecurityMaintenance:
    def test_no_observation_unknown(self) -> None:
        assert evaluate_security_maintenance(None) == (
            'unknown', 'no_support_observation')

    def test_superseded_unknown(self) -> None:
        obs = _observation(superseded_by_ref=_ref('observation', 'n1'))
        assert evaluate_security_maintenance(obs)[0] == 'unknown'

    def test_currently_supported(self) -> None:
        assert evaluate_security_maintenance(_observation()) == (
            'security_maintenance_available',
            'support_state:currently_supported')

    @pytest.mark.parametrize('state', (
        'limited_security_only', 'end_of_sale', 'end_of_software_updates'))
    def test_limited(self, state: str) -> None:
        verdict, _ = evaluate_security_maintenance(
            _observation(support_state=state))
        assert verdict == 'security_maintenance_limited'

    @pytest.mark.parametrize('state', (
        'end_of_security_support', 'end_of_service',
        'discontinued_cloud_dependency'))
    def test_ended_routes_598(self, state: str) -> None:
        verdict, detail = evaluate_security_maintenance(
            _observation(support_state=state))
        assert verdict == 'security_maintenance_ended'
        assert '#598' in detail
        # never a compromise claim
        assert 'compromis' not in detail

    def test_lfc40_eol_but_functional(self) -> None:
        # LFC40: updates ended, hardware still operates — record the
        # limited state, no automatic compromise claim.
        obs = _observation(support_state='end_of_software_updates')
        assert evaluate_security_maintenance(obs)[0] \
            == 'security_maintenance_limited'

    def test_unknown_state(self) -> None:
        assert evaluate_security_maintenance(
            _observation(support_state='unknown')) == (
                'unknown', 'support_state:unknown')


# ---------------------------------------------------------------------------
# evaluate_licence_deadline — LFC30
# ---------------------------------------------------------------------------

class TestLicenceDeadline:
    def test_no_licence_unknown(self) -> None:
        assert evaluate_licence_deadline(None, _TS) == (
            'unknown', 'no_licence_evidence')

    def test_expired_state(self) -> None:
        assert evaluate_licence_deadline(
            _licence(licence_state='expired'), _TS)[0] == 'expired'

    def test_expired_by_date(self) -> None:
        lic = _licence(expires_at_utc=_TS)
        assert evaluate_licence_deadline(lic, _LATER)[0] == 'expired'

    def test_renewal_pending(self) -> None:
        lic = _licence(licence_state='renewal_pending')
        assert evaluate_licence_deadline(lic, _TS)[0] == 'renewal_due'

    def test_renewal_due_date_passed(self) -> None:
        lic = _licence(renewal_due_utc=_TS)
        assert evaluate_licence_deadline(lic, _LATER)[0] == 'renewal_due'

    def test_grace_period(self) -> None:
        lic = _licence(licence_state='grace_period')
        assert evaluate_licence_deadline(lic, _TS)[0] == 'renewal_due'

    def test_active(self) -> None:
        lic = _licence(expires_at_utc=_LATER)
        assert evaluate_licence_deadline(lic, _TS)[0] == 'active'

    def test_unknown_state(self) -> None:
        lic = _licence(licence_state='unknown')
        assert evaluate_licence_deadline(lic, _TS)[0] == 'unknown'


# ---------------------------------------------------------------------------
# evaluate_replacement_readiness — LFC60
# ---------------------------------------------------------------------------

def _ready_readiness(**kw) -> ReplacementReadiness:
    payload = dict(
        control_migration_state='compatible',
        config_portability='confirmed',
        licence_portability='confirmed',
        physical_fit='confirmed',
        requalification_scope_ref=_ref('requalification', 'rq-1'),
        backup_restore_path_ref=_ref('backup', 'b1'),
    )
    payload.update(kw)
    return _readiness(**payload)


class TestReplacementReadiness:
    def test_no_record_unverified(self) -> None:
        assert evaluate_replacement_readiness(None) == (
            'replacement_unverified', 'no_readiness_record')

    def test_not_possible_blocked(self) -> None:
        verdict, detail = evaluate_replacement_readiness(
            _ready_readiness(physical_fit='not_possible'))
        assert verdict == 'replacement_blocked'
        assert 'physical_fit' in detail

    def test_incompatible_migration_blocked(self) -> None:
        verdict, _ = evaluate_replacement_readiness(
            _ready_readiness(control_migration_state='incompatible'))
        assert verdict == 'replacement_blocked'

    def test_missing_requalification_unverified(self) -> None:
        verdict, detail = evaluate_replacement_readiness(
            _ready_readiness(requalification_scope_ref=None))
        assert verdict == 'replacement_unverified'
        assert '#596' in detail

    def test_missing_backup_path_unverified(self) -> None:
        verdict, detail = evaluate_replacement_readiness(
            _ready_readiness(backup_restore_path_ref=None))
        assert verdict == 'replacement_unverified'
        assert '#592' in detail

    def test_ready(self) -> None:
        verdict, detail = evaluate_replacement_readiness(
            _ready_readiness())
        assert verdict == 'replacement_ready'
        assert 'AVR successor model' in detail

    def test_requires_work(self) -> None:
        verdict, _ = evaluate_replacement_readiness(
            _ready_readiness(config_portability='requires_work'))
        assert verdict == 'replacement_requires_work'

    def test_migration_required_requires_work(self) -> None:
        verdict, _ = evaluate_replacement_readiness(
            _ready_readiness(
                control_migration_state='migration_required'))
        assert verdict == 'replacement_requires_work'

    def test_unresolved_criteria_unverified(self) -> None:
        # Criteria present-but-unknown can never read as ready.
        verdict, _ = evaluate_replacement_readiness(
            _ready_readiness(licence_portability='unknown'))
        assert verdict == 'replacement_unverified'

    def test_lfc50_driver_abandonment(self) -> None:
        # LFC50: core AV works but the control integration is
        # unmaintainable — an integration-layer observation, not a
        # playback-capability verdict.
        obs = _observation(
            kind='driver_api_status',
            support_state='discontinued_cloud_dependency',
            integration=IntegrationDependency(
                component_label='control driver',
                api_sdk_version='2.1',
                vendor_support_state='discontinued_cloud_dependency',
            ),
        )
        assert obs.kind == 'driver_api_status'
        verdict, detail = evaluate_security_maintenance(obs)
        assert verdict == 'security_maintenance_ended'
        assert '#598' in detail


# ---------------------------------------------------------------------------
# persistence — _SealedStore contract
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path) -> CadSupportabilityRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadSupportabilityRepository(SceneRepository(db))


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    records = (
        ('spro', _profile(), repo.save_supportability_profile,
         repo.get_supportability_profile, 'profile_id'),
        ('exdep', _dependency(), repo.save_external_dependency,
         repo.get_external_dependency, 'dependency_id'),
        ('lro', _observation(), repo.save_lifecycle_observation,
         repo.get_lifecycle_observation, 'observation_id'),
        ('oce', _continuity(), repo.save_offline_continuity,
         repo.get_offline_continuity, 'evidence_id'),
        ('rpr', _readiness(), repo.save_replacement_readiness,
         repo.get_replacement_readiness, 'readiness_id'),
    )
    for _name, record, save, get, id_field in records:
        save(record)
        rid = getattr(record, id_field)
        assert get(rid) == record


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dep = _dependency()
    repo.save_external_dependency(dep)
    repo.save_external_dependency(dep)
    assert repo.list_external_dependencies(DOC) == (dep,)


def test_repository_get_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.get_supportability_profile('spro-missing') is None


def test_repository_unsealed_record_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    forged = SupportabilityProfile.model_construct(
        profile_id='spro-forged',
        profile_sha256='f' * 64,
        document_id=DOC,
        profile_label='x',
        declared_at_utc=_TS,
        source_label='s',
    )
    with pytest.raises(SupportabilityIntegrityError):
        repo.save_supportability_profile(forged)


def test_repository_column_tamper_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dep = _dependency()
    repo.save_external_dependency(dep)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_supportability_dependencies "
            "SET dependency_kind='local_only' WHERE dependency_id=?",
            (dep.dependency_id,))
        conn.commit()
    with pytest.raises(SupportabilityIntegrityError):
        repo.get_external_dependency(dep.dependency_id)


def test_repository_document_id_tamper_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    profile = _profile()
    repo.save_supportability_profile(profile)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_supportability_profiles "
            "SET document_id='other-doc' WHERE profile_id=?",
            (profile.profile_id,))
        conn.commit()
    with pytest.raises(SupportabilityIntegrityError):
        repo.get_supportability_profile(profile.profile_id)


def test_repository_nullable_ref_column_tamper_detected(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dep = _dependency()  # profile_ref=None → NULL profile_ref_id
    repo.save_external_dependency(dep)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_supportability_dependencies "
            "SET profile_ref_id='forged-profile' WHERE dependency_id=?",
            (dep.dependency_id,))
        conn.commit()
    with pytest.raises(SupportabilityIntegrityError):
        repo.get_external_dependency(dep.dependency_id)


def test_repository_list_document_id_tamper_detected(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    obs = _observation()
    repo.save_lifecycle_observation(obs)
    assert repo.list_lifecycle_observations(DOC) == (obs,)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_lifecycle_risk_observations "
            "SET document_id='other-doc' WHERE observation_id=?",
            (obs.observation_id,))
        conn.commit()
    with pytest.raises(SupportabilityIntegrityError):
        repo.list_lifecycle_observations()
    assert repo.list_lifecycle_observations(DOC) == ()


def test_repository_document_scoping(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = _dependency()
    b = _dependency(document_id='doc-other', dependency_kind='other')
    repo.save_external_dependency(a)
    repo.save_external_dependency(b)
    assert repo.list_external_dependencies(DOC) == (a,)
    assert repo.list_external_dependencies('doc-other') == (b,)


def test_supportability_tables_exist_after_fresh_migrate(
        tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        'cad_supportability_profiles',
        'cad_supportability_dependencies',
        'cad_lifecycle_risk_observations',
        'cad_offline_continuity_evidence',
        'cad_replacement_readiness',
    } <= tables


def test_lfc30_licence_expiry_without_secrets(tmp_path: Path) -> None:
    # LFC30: DSP feature depends on an entitlement; expiry is visible;
    # no secret material is stored anywhere in the record.
    lic = _licence(
        expires_at_utc=_TS,
        renewal_due_utc=_TS,
    )
    obs = _observation(
        kind='licence_entitlement_state', licence=lic)
    repo = _repo(tmp_path)
    repo.save_lifecycle_observation(obs)
    stored = repo.get_lifecycle_observation(obs.observation_id)
    assert stored == obs
    assert evaluate_licence_deadline(
        stored.licence, _LATER)[0] == 'expired'
    assert 'key' not in stored.model_dump_json().lower()


def test_lfc80_external_outage_tested(tmp_path: Path) -> None:
    # LFC80: a temporary outage test verifies the essential fallback —
    # the record carries no permanence claim.
    evidence = _continuity(
        function_label='local_playback',
        condition='vendor_cloud_unreachable',
        outcome='continue_local')
    repo = _repo(tmp_path)
    repo.save_offline_continuity(evidence)
    assert repo.get_offline_continuity(
        evidence.evidence_id) == evidence
    verdict, _ = evaluate_offline_continuity(
        'local_playback', (evidence,),
        condition='vendor_cloud_unreachable')
    assert verdict == 'continues_local'


def test_lfc60_replacement_readiness_composition(
        tmp_path: Path) -> None:
    # LFC60: successor identified; readiness requires the pinned #596
    # requalification scope and #592 backup/restore path.
    readiness = _ready_readiness()
    repo = _repo(tmp_path)
    repo.save_replacement_readiness(readiness)
    stored = repo.get_replacement_readiness(readiness.readiness_id)
    assert stored == readiness
    assert evaluate_replacement_readiness(stored)[0] \
        == 'replacement_ready'


def test_serviceability_snapshot_is_time_sensitive() -> None:
    # Warranty/service facts are observed state, not product truth —
    # expiry is never equated with failure.
    snap = ServiceabilitySnapshot(
        warranty_state='expired',
        authorized_service_available='no',
        lead_time_days_observed=14.0,
        rma_policy_source='vendor rma page',
    )
    obs = _observation(
        kind='warranty_service_state', serviceability=snap,
        support_state='unknown')
    assert obs.serviceability.warranty_state == 'expired'
    assert evaluate_security_maintenance(obs)[0] == 'unknown'
