"""Issue #789 — transient/surge-protection evidence authority.

Covers the sealed records, the fail-closed evaluator ordering, the
cross-domain guards (power quality / UPS ride-through / vendor
marketing never satisfy SPD domains) and repository round-trip +
tamper detection.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.cad_transient_protection import (
    ProtectedPath,
    SPDEvidence,
    TransientProtectionAssessment,
    TransientProtectionEvent,
    TransientProtectionIntegrityError,
    TransientProtectionObservation,
    TransientProtectionPlan,
    evaluate_path_protection,
)
from htdt.cad_transient_protection_repository import (
    CadTransientProtectionRepository,
    TransientProtectionConflictError,
)
from htdt.measurement_evidence_display import (
    transient_protection_label,
    transient_protection_verdict_line,
)


def _ref(kind: str, rid: str = 'x') -> AuthorityRef:
    return AuthorityRef(
        kind=kind, ref_id=rid, ref_sha256='a' * 64)


def _path(document_id: str = 'doc-1', rid: str = 'p1') -> ProtectedPath:
    return ProtectedPath.create(
        document_id=document_id,
        path_kind='electrical_supply',
        stage_sequence=(
            'utility_service', 'panel_distribution',
            'branch_circuit', 'receptacle_pdu_ups', 'av_device'),
        load_ref=_ref('equipment', rid),
        load_label='AVR-1',
    )


def _plan(
    path: ProtectedPath,
    domains: tuple[str, ...] = ('branch_point_of_use_spd',),
    **kw,
) -> TransientProtectionPlan:
    return TransientProtectionPlan.create(
        document_id=path.document_id,
        path_refs=(AuthorityRef(
            kind='protected_path', ref_id=path.path_id,
            ref_sha256=path.path_sha256),),
        declared_domains=domains,
        **kw)


def _spd(
    path: ProtectedPath,
    domain: str = 'branch_point_of_use_spd',
    **kw,
) -> SPDEvidence:
    payload = dict(
        document_id=path.document_id,
        path_ref=AuthorityRef(
            kind='protected_path', ref_id=path.path_id,
            ref_sha256=path.path_sha256),
        domain=domain,
        spd_type_class='type_2',
        standard_profile='iec_61643_11_2025',
        standard_reference='IEC 61643-11@2025',
        manufacturer='Acme',
        model='SPD-240',
        vpr_volts=800.0,
        mcov_volts=275.0,
        nominal_discharge_current_ka=20.0,
        sccr_ka=100.0,
        install_state='installed_verified',
        panel_circuit_association='panel-A ckt-7',
        evidence_basis='installed_record',
    )
    payload.update(kw)
    return SPDEvidence.create(**payload)


def _obs(spd: SPDEvidence, status: str, ts: str) -> TransientProtectionObservation:
    return TransientProtectionObservation.create(
        document_id=spd.document_id,
        spd_ref=AuthorityRef(
            kind='spd_evidence', ref_id=spd.spd_id,
            ref_sha256=spd.spd_sha256),
        observed_at_utc=ts,
        status=status,
        status_source='local_indicator',
    )


def _event(
    path: ProtectedPath, inspected: bool = False,
    kind: str = 'lightning_strike',
) -> TransientProtectionEvent:
    return TransientProtectionEvent.create(
        document_id=path.document_id,
        event_kind=kind,
        observed_at_utc='2026-10-01T00:00:00Z',
        affected_path_refs=(AuthorityRef(
            kind='protected_path', ref_id=path.path_id,
            ref_sha256=path.path_sha256),),
        inspection_record_ref=_ref('inspection', 'insp-1')
        if inspected else None,
    )


def _repo(tmp_path):
    db = tmp_path / 'scene.sqlite3'
    ensure_native_schema(db)
    return CadTransientProtectionRepository(SceneRepository(db))


# ------------------------------------------------------------------ paths


def test_path_requires_stages_and_pinned_load() -> None:
    with pytest.raises(ValueError):
        ProtectedPath.create(
            document_id='d', path_kind='electrical_supply',
            stage_sequence=(), load_ref=_ref('equipment'),
            load_label='x')
    with pytest.raises(ValueError):
        ProtectedPath.create(
            document_id='d', path_kind='electrical_supply',
            stage_sequence=('branch_circuit',),
            load_ref=AuthorityRef(
                kind='equipment', ref_id='e', ref_sha256=None),
            load_label='x')


def test_path_seal_is_deterministic() -> None:
    a = _path()
    b = _path()
    assert a.path_sha256 == b.path_sha256
    assert a.path_id == b.path_id


# ------------------------------------------------------------------ plan


def test_plan_forces_review_flag_for_external_lps() -> None:
    plan = _plan(_path(), domains=(
        'branch_point_of_use_spd',
        'external_lightning_protection_system'))
    assert plan.requires_qualified_review is True


def test_plan_rejects_unpinned_refs_and_code_without_edition() -> None:
    path = _path()
    with pytest.raises(ValueError):
        TransientProtectionPlan.create(
            document_id='d',
            path_refs=(AuthorityRef(
                kind='protected_path', ref_id='p', ref_sha256=None),),
            declared_domains=('branch_point_of_use_spd',))
    with pytest.raises(ValueError):
        _plan(path, code_family='nfpa_70')


# ------------------------------------------------------------------ SPD evidence


def test_spd_requires_standard_pin() -> None:
    with pytest.raises(ValueError):
        _spd(_path(), standard_reference='IEC 61643-11')


def test_spd_installed_verified_requires_install_record() -> None:
    with pytest.raises(ValueError):
        _spd(
            _path(), install_state='installed_verified',
            panel_circuit_association=None)


def test_spd_ratings_must_be_positive() -> None:
    for field in ('vpr_volts', 'mcov_volts',
                  'nominal_discharge_current_ka', 'sccr_ka'):
        with pytest.raises(ValueError):
            _spd(_path(), **{field: 0.0})


# ------------------------------------------------------------------ evaluator


def test_no_evidence_is_fail_closed() -> None:
    path = _path()
    result = evaluate_path_protection(path, _plan(path), (), (), ())
    assert result.verdict == 'no_protection_evidence'
    assert result.domains_covered == ()


def test_evidence_without_plan_never_claims_protection() -> None:
    path = _path()
    result = evaluate_path_protection(
        path, None, (_spd(path),), (), ())
    assert result.verdict == 'no_protection_evidence'


def test_installed_verified_covering_declared_domains_is_protected() -> None:
    path = _path()
    result = evaluate_path_protection(
        path, _plan(path), (_spd(path),), (), ())
    assert result.verdict == 'protected_with_evidence'
    assert result.coordination_state == 'single_device'
    assert result.domains_uncovered == ()


def test_uncovered_declared_domain_is_partial() -> None:
    path = _path()
    plan = _plan(path, domains=(
        'branch_point_of_use_spd', 'service_entrance_spd'))
    result = evaluate_path_protection(
        path, plan, (_spd(path),), (), ())
    assert result.verdict == 'partial_point_of_use_only'
    assert 'service_entrance_spd' in result.domains_uncovered


def test_designed_only_is_not_installed() -> None:
    path = _path()
    result = evaluate_path_protection(
        path, _plan(path),
        (_spd(path, install_state='designed_only',
              panel_circuit_association=None),),
        (), ())
    assert result.verdict == 'designed_not_installed'


def test_two_spds_without_coordination_reference() -> None:
    path = _path()
    plan = _plan(path, domains=(
        'branch_point_of_use_spd', 'distribution_panel_spd'))
    ev = (
        _spd(path),
        _spd(path, domain='distribution_panel_spd', model='SPD-Main'),
    )
    result = evaluate_path_protection(path, plan, ev, (), ())
    assert result.verdict == 'coordination_unverified'
    assert result.coordination_state == 'multiple_uncoordinated_unknown'


def test_coordinated_spds_reach_protected() -> None:
    path = _path()
    plan = _plan(path, domains=(
        'branch_point_of_use_spd', 'distribution_panel_spd'))
    ev = (
        _spd(path, coordination_ref=_ref('coordination', 'c1')),
        _spd(path, domain='distribution_panel_spd', model='SPD-Main',
             coordination_ref=_ref('coordination', 'c1')),
    )
    result = evaluate_path_protection(path, plan, ev, (), ())
    assert result.verdict == 'protected_with_evidence'
    assert result.coordination_state == 'coordinated_with_reference'


def test_vendor_marketing_never_satisfies() -> None:
    path = _path()
    ev = _spd(path, evidence_basis='vendor_marketing')
    result = evaluate_path_protection(path, _plan(path), (ev,), (), ())
    assert result.verdict == 'no_protection_evidence'


def test_non_spd_feature_never_satisfies_power_domain() -> None:
    path = _path()
    ev = _spd(path, spd_type_class='non_spd_transient_feature',
              standard_profile='unknown', standard_reference='')
    result = evaluate_path_protection(path, _plan(path), (ev,), (), ())
    assert result.verdict == 'no_protection_evidence'


def test_degraded_status_stales_protection_not_power() -> None:
    path = _path()
    spd = _spd(path)
    result = evaluate_path_protection(
        path, _plan(path), (spd,),
        (_obs(spd, 'replace_required', '2026-10-02T00:00:00Z'),), ())
    assert result.verdict == 'status_degraded'


def test_latest_observation_wins_over_earlier_alarm() -> None:
    path = _path()
    spd = _spd(path)
    result = evaluate_path_protection(
        path, _plan(path), (spd,),
        (
            _obs(spd, 'fault', '2026-09-01T00:00:00Z'),
            _obs(spd, 'status_ok', '2026-10-01T00:00:00Z'),
        ), ())
    assert result.verdict == 'protected_with_evidence'


def test_uninspected_event_stales_everything() -> None:
    path = _path()
    spd = _spd(path)
    result = evaluate_path_protection(
        path, _plan(path), (spd,), (), (_event(path),))
    assert result.verdict == 'stale_after_event'
    assert result.open_event_refs


def test_inspected_event_releases_stale() -> None:
    path = _path()
    spd = _spd(path)
    result = evaluate_path_protection(
        path, _plan(path), (spd,), (),
        (_event(path, inspected=True),))
    assert result.verdict == 'protected_with_evidence'


def test_event_on_other_path_does_not_stale() -> None:
    path = _path()
    other = ProtectedPath.create(
        document_id=path.document_id,
        path_kind='signal_data',
        stage_sequence=(
            'external_conductive_path',
            'surge_protection_interface',
            'network_or_signal_device'),
        load_ref=_ref('equipment', 'switch-1'),
        load_label='SW-1')
    ev = TransientProtectionEvent.create(
        document_id=path.document_id,
        event_kind='external_path_added',
        observed_at_utc='2026-10-01T00:00:00Z',
        affected_path_refs=(AuthorityRef(
            kind='protected_path', ref_id=other.path_id,
            ref_sha256=other.path_sha256),))
    result = evaluate_path_protection(
        path, _plan(path), (_spd(path),), (), (ev,))
    assert result.verdict == 'protected_with_evidence'


def test_lps_plan_without_review_is_review_required() -> None:
    path = _path()
    plan = _plan(path, domains=(
        'branch_point_of_use_spd',
        'external_lightning_protection_system'))
    result = evaluate_path_protection(
        path, plan, (_spd(path),), (), ())
    assert result.verdict == 'review_required'
    assert result.professional_review_required is True


def test_lps_plan_with_review_ref_can_be_partial_not_protected() -> None:
    path = _path()
    plan = _plan(
        path,
        domains=(
            'branch_point_of_use_spd',
            'external_lightning_protection_system'),
        professional_review_ref=_ref('professional_review', 'rev-1'))
    assert plan.requires_qualified_review is False
    result = evaluate_path_protection(
        path, plan, (_spd(path),), (), ())
    # LPS domain is declared but uncovered — review exists, coverage not.
    assert result.verdict == 'partial_point_of_use_only'


def test_evidence_for_other_path_does_not_satisfy() -> None:
    path = _path()
    other = ProtectedPath.create(
        document_id=path.document_id,
        path_kind='signal_data',
        stage_sequence=(
            'external_conductive_path',
            'surge_protection_interface',
            'network_or_signal_device'),
        load_ref=_ref('equipment', 'switch-1'),
        load_label='SW-1')
    ev = _spd(other, domain='signal_data_line_protection')
    result = evaluate_path_protection(
        path, _plan(path), (ev,), (), ())
    assert result.verdict == 'no_protection_evidence'


# ------------------------------------------------------------------ assessment model


def test_assessment_rejects_unpinned_refs_and_inconsistent_verdict() -> None:
    path = _path()
    with pytest.raises(ValueError):
        TransientProtectionAssessment.create(
            document_id='d',
            path_ref=AuthorityRef(
                kind='protected_path', ref_id='p', ref_sha256=None),
            plan_ref=None,
            spd_evidence_refs=(),
            verdict='no_protection_evidence',
            coordination_state='unknown',
            rationale='x')
    with pytest.raises(ValueError):
        TransientProtectionAssessment.create(
            document_id='d',
            path_ref=_ref('protected_path', 'p'),
            plan_ref=None,
            spd_evidence_refs=(),
            verdict='protected_with_evidence',
            coordination_state='unknown',
            rationale='x')


# ------------------------------------------------------------------ repository


def test_repository_round_trip_all_records(tmp_path) -> None:
    repo = _repo(tmp_path)
    path = _path()
    plan = _plan(path)
    spd = _spd(path)
    obs = _obs(spd, 'status_ok', '2026-10-01T00:00:00Z')
    ev = _event(path, inspected=True)
    assessment = evaluate_path_protection(
        path, plan, (spd,), (obs,), (ev,))
    repo.protected_paths.save(path)
    repo.plans.save(plan)
    repo.spd_evidence.save(spd)
    repo.observations.save(obs)
    repo.events.save(ev)
    repo.assessments.save(assessment)
    assert repo.get_path(path.path_id) == path
    assert repo.get_plan(plan.plan_id) == plan
    assert repo.get_spd(spd.spd_id) == spd
    assert repo.get_observation(obs.observation_id) == obs
    assert repo.get_event(ev.event_id) == ev
    assert repo.get_assessment(assessment.assessment_id) == assessment


def test_repository_append_only_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    path = _path()
    repo.protected_paths.save(path)
    tampered = ProtectedPath.create(
        document_id=path.document_id,
        path_kind='signal_data',
        stage_sequence=('external_conductive_path',),
        load_ref=_ref('equipment', 'other'),
        load_label='X')
    object.__setattr__(tampered, 'path_id', path.path_id)
    object.__setattr__(tampered, 'path_sha256', path.path_sha256)
    # The tampered copy is rejected at the seal check before the
    # append-only conflict check can see it.
    with pytest.raises(
            (TransientProtectionConflictError,
             TransientProtectionIntegrityError)):
        repo.protected_paths.save(tampered)
    repo.protected_paths.save(path)  # idempotent re-save


def test_repository_detects_column_tampering(tmp_path) -> None:
    repo = _repo(tmp_path)
    spd = _spd(_path())
    repo.spd_evidence.save(spd)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_spd_evidence SET domain=? WHERE spd_id=?',
            ('service_entrance_spd', spd.spd_id))
    with pytest.raises(TransientProtectionIntegrityError):
        repo.get_spd(spd.spd_id)


def test_repository_rejects_unsealed_save(tmp_path) -> None:
    repo = _repo(tmp_path)
    path = _path()
    broken = path.model_copy(update={'path_sha256': 'b' * 64})
    with pytest.raises(TransientProtectionIntegrityError):
        repo.protected_paths.save(broken)


def test_repository_list_filters_by_document(tmp_path) -> None:
    repo = _repo(tmp_path)
    a = _path(document_id='doc-a')
    b = _path(document_id='doc-b')
    repo.protected_paths.save(a)
    repo.protected_paths.save(b)
    assert repo.protected_paths.list('doc-a') == (a,)
    assert repo.protected_paths.list() == (a, b)


# ------------------------------------------------------------------ labels


def test_ja_label_coverage() -> None:
    assert transient_protection_verdict_line(
        'stale_after_event').startswith('過渡保護:')
    assert transient_protection_label(
        'service_entrance_spd') == 'サービス入口SPD'
    assert transient_protection_label('__none__') == '__none__'
