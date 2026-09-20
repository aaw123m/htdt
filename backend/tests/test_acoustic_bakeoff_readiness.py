from __future__ import annotations

from pathlib import Path

from htdt.acoustic_bakeoff import (
    BakeoffCandidateManifest,
    BakeoffFixtureEvidence,
    BakeoffHardGateEvidence,
    BakeoffObservableEvidence,
    BakeoffPlatform,
    load_bakeoff_adoption_profile,
    load_bakeoff_candidate_manifest,
)
from htdt.acoustic_bakeoff_readiness import (
    BakeoffReadinessEvidenceLedger,
    BakeoffReadinessEvidenceRecord,
    build_production_adoption_readiness_report,
    candidate_semantic_hash,
    load_readiness_evidence_ledger,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


ROOT = Path(__file__).resolve().parents[2]
R100A_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'
CANDIDATE_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_candidates.json'
ADOPTION_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_wave_adoption_profile.json'
READINESS_LEDGER_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_production_adoption_evidence.json'


def _authorities():
    return (
        load_acoustic_benchmark_manifest(R100A_PATH),
        load_bakeoff_candidate_manifest(CANDIDATE_PATH),
        load_bakeoff_adoption_profile(ADOPTION_PATH),
    )


def _candidate(candidates, candidate_id):
    return next(item for item in candidates.candidates if item.candidate_id == candidate_id)


def _platform() -> BakeoffPlatform:
    return BakeoffPlatform(
        os='GitHub Actions Windows 11',
        architecture='x86_64',
        python_version='3.12',
        cpu='CI CPU',
        logical_threads=4,
        thread_budget=4,
        gpu=None,
        device_notes='focused readiness test',
    )


def _passing_fixture(fixture) -> BakeoffFixtureEvidence:
    observables = []
    for expected in fixture.observables:
        kwargs = {
            'observable_id': expected.observable_id,
            'status': 'pass',
            'summary': 'synthetic current-authority readiness evidence',
        }
        if expected.acceptance_relation == 'must_differ_from_peer':
            kwargs['difference_from_peer'] = float(
                expected.tolerance.minimum_difference or 0.001
            )
        elif expected.kind == 'transfer_phase_deg':
            kwargs['phase_error_deg'] = 0.0
        elif expected.kind == 'complex_reflection_coefficient':
            kwargs.update(
                absolute_error=0.0,
                relative_error=0.0,
                phase_error_deg=0.0,
            )
        else:
            if expected.tolerance.absolute is not None:
                kwargs['absolute_error'] = 0.0
            if expected.tolerance.relative is not None:
                kwargs['relative_error'] = 0.0
            if expected.tolerance.phase_deg is not None:
                kwargs['phase_error_deg'] = 0.0
            if expected.tolerance.statistical_stddev_max is not None:
                kwargs['statistical_stddev'] = 0.0
        observables.append(BakeoffObservableEvidence(**kwargs))

    return BakeoffFixtureEvidence(
        fixture_id=fixture.fixture_id,
        status='pass',
        evidence_ref=f'artifact:test/{fixture.fixture_id}.json',
        adapter_id='synthetic-readiness',
        adapter_version='1',
        backend_version='synthetic',
        precision='float64',
        compile_s=0.1,
        solve_s=0.1,
        postprocess_s=0.1,
        peak_ram_mb=1.0,
        disk_mb=1.0,
        output_mb=1.0,
        observables=tuple(observables),
    )


def _current_fixture_record(
    benchmark,
    candidates,
    candidate,
    fixture,
    *,
    evidence_id: str | None = None,
) -> BakeoffReadinessEvidenceRecord:
    return BakeoffReadinessEvidenceRecord(
        evidence_id=evidence_id or f'fixture-{fixture.fixture_id}',
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        candidate_semantic_hash=candidate_semantic_hash(candidate),
        candidate_manifest_hash=candidates.semantic_hash(),
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        target_kind='fixture',
        fixture_id=fixture.fixture_id,
        reported_status='pass',
        evidence_ref=f'artifact:test/{fixture.fixture_id}.json',
        fixture_evidence=_passing_fixture(fixture),
        platform=_platform(),
    )


def _current_hard_gate_record(
    benchmark,
    candidates,
    candidate,
    category: str,
) -> BakeoffReadinessEvidenceRecord:
    gate = BakeoffHardGateEvidence(
        category=category,
        status='pass',
        evidence_ref=f'artifact:test/gate-{category}.json',
        summary='synthetic current-authority hard-gate evidence',
    )
    return BakeoffReadinessEvidenceRecord(
        evidence_id=f'gate-{category}',
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        candidate_semantic_hash=candidate_semantic_hash(candidate),
        candidate_manifest_hash=candidates.semantic_hash(),
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        target_kind='hard_gate',
        hard_gate_category=category,
        reported_status='pass',
        evidence_ref=gate.evidence_ref,
        hard_gate_evidence=gate,
        platform=_platform() if category in {'cpu_baseline', 'windows_packaging'} else None,
    )


def _fully_qualified():
    benchmark, candidates, profile = _authorities()
    base = _candidate(candidates, 'pffdtd-main-aa319f6')
    upgraded = base.model_copy(
        update={
            'probe_capabilities': tuple(
                sorted(set(base.probe_capabilities) | set(profile.required_capabilities))
            ),
            'windows_packaging_status': 'documented_native',
        }
    )
    payload = candidates.model_dump(mode='python')
    payload['candidates'] = [
        upgraded.model_dump(mode='python')
        if item['candidate_id'] == base.candidate_id
        else item
        for item in payload['candidates']
    ]
    upgraded_candidates = BakeoffCandidateManifest.model_validate(payload)
    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    records = [
        _current_fixture_record(
            benchmark,
            upgraded_candidates,
            upgraded,
            fixture_by_id[fixture_id],
        )
        for fixture_id in profile.required_fixture_ids
    ]
    records.extend(
        _current_hard_gate_record(
            benchmark,
            upgraded_candidates,
            upgraded,
            category,
        )
        for category in (
            'cpu_baseline',
            'windows_packaging',
            'reproducible_authority',
        )
    )
    ledger = BakeoffReadinessEvidenceLedger(
        ledger_id='synthetic-fully-qualified',
        records=tuple(records),
    )
    return benchmark, upgraded_candidates, profile, upgraded, ledger


def _candidate_report(report, candidate_id):
    return next(item for item in report.candidates if item.candidate_id == candidate_id)


def _check(checks, check_id):
    return next(item for item in checks if item.check_id == check_id)


def test_fully_qualified_candidate_is_ready() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )

    candidate_report = _candidate_report(report, candidate.candidate_id)
    assert candidate_report.decision == 'READY'
    assert report.decision == 'READY'
    assert report.ready_candidate_ids == (candidate.candidate_id,)
    assert report.production_solver_selected is False
    assert all(
        item.status == 'PASS'
        for item in candidate_report.audit_checks
        if item.mandatory
    )


def test_missing_mandatory_evidence_is_blocked() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    missing = ledger.model_copy(update={'records': ledger.records[:-4]})

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, missing
    )
    candidate_report = _candidate_report(report, candidate.candidate_id)

    assert candidate_report.decision == 'NO_GO'
    assert candidate_report.fixture_results[-1].status == 'BLOCKED'


def test_explicit_failed_evidence_is_fail_not_missing() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    records = list(ledger.records)
    target = records[0]
    failed_fixture = target.fixture_evidence.model_copy(
        update={'status': 'fail', 'observables': ()}
    )
    records[0] = target.model_copy(
        update={
            'reported_status': 'fail',
            'fixture_evidence': failed_fixture,
        }
    )
    failed = ledger.model_copy(update={'records': tuple(records)})

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, failed
    )
    result = _candidate_report(report, candidate.candidate_id).fixture_results[0]

    assert result.status == 'FAIL'
    assert result.evidence_ids == (target.evidence_id,)


def test_blocked_evidence_remains_blocked() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    records = list(ledger.records)
    target = records[1]
    blocked_fixture = target.fixture_evidence.model_copy(
        update={'status': 'blocked', 'observables': ()}
    )
    records[1] = target.model_copy(
        update={
            'reported_status': 'blocked',
            'fixture_evidence': blocked_fixture,
        }
    )

    report = build_production_adoption_readiness_report(
        benchmark,
        candidates,
        profile,
        ledger.model_copy(update={'records': tuple(records)}),
    )
    result = _candidate_report(report, candidate.candidate_id).fixture_results[1]

    assert result.status == 'BLOCKED'
    assert result.evidence_ids == (target.evidence_id,)


def test_stale_fixture_authority_is_excluded_fail_closed() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    records = list(ledger.records)
    target = records[0]
    records[0] = target.model_copy(update={'r100a_semantic_hash': '0' * 64})

    report = build_production_adoption_readiness_report(
        benchmark,
        candidates,
        profile,
        ledger.model_copy(update={'records': tuple(records)}),
    )
    candidate_report = _candidate_report(report, candidate.candidate_id)
    result = candidate_report.fixture_results[0]

    assert result.status == 'BLOCKED'
    excluded = {item.evidence_id: item.reason for item in candidate_report.excluded_evidence}
    assert excluded[target.evidence_id] == 'stale_r100a_authority'


def test_stale_candidate_implementation_is_excluded() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    records = list(ledger.records)
    target = records[0]
    records[0] = target.model_copy(
        update={'candidate_source_commit_sha': '1' * 40}
    )

    report = build_production_adoption_readiness_report(
        benchmark,
        candidates,
        profile,
        ledger.model_copy(update={'records': tuple(records)}),
    )
    candidate_report = _candidate_report(report, candidate.candidate_id)
    excluded = {item.evidence_id: item.reason for item in candidate_report.excluded_evidence}

    assert candidate_report.fixture_results[0].status == 'BLOCKED'
    assert excluded[target.evidence_id] == 'stale_candidate_implementation'


def test_wrong_candidate_evidence_binding_is_excluded() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    mfem = _candidate(candidates, 'mfem-v4.10-d964264')
    records = list(ledger.records)
    target = records[0]
    records[0] = target.model_copy(
        update={'candidate_semantic_hash': candidate_semantic_hash(mfem)}
    )

    report = build_production_adoption_readiness_report(
        benchmark,
        candidates,
        profile,
        ledger.model_copy(update={'records': tuple(records)}),
    )
    candidate_report = _candidate_report(report, candidate.candidate_id)
    excluded = {item.evidence_id: item.reason for item in candidate_report.excluded_evidence}

    assert candidate_report.fixture_results[0].status == 'BLOCKED'
    assert excluded[target.evidence_id] == 'stale_candidate_authority'


def test_reference_only_candidate_is_rejected_for_production() -> None:
    benchmark, candidates, profile = _authorities()
    ledger = BakeoffReadinessEvidenceLedger(ledger_id='empty')

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    pyroom = _candidate_report(report, 'pyroomacoustics-v0.10.1-f02b01d')

    assert pyroom.decision == 'NO_GO'
    assert _check(pyroom.audit_checks, 'production_role_scope').status == 'FAIL'


def test_unsupported_required_capability_is_blocked() -> None:
    benchmark, candidates, profile = _authorities()
    ledger = BakeoffReadinessEvidenceLedger(ledger_id='empty')

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    pffdtd = _candidate_report(report, 'pffdtd-main-aa319f6')

    assert _check(pffdtd.hard_gate_results, 'required_capability').status == 'BLOCKED'
    portal = _check(pffdtd.fixture_results, 'wave-portal-split-room-v1')
    radiation = _check(
        pffdtd.fixture_results,
        'wave-explicit-radiation-termination-v1',
    )
    assert portal.status == 'BLOCKED'
    assert radiation.status == 'BLOCKED'


def test_license_and_platform_hard_gate_failures_are_explicit() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    broken = candidate.model_copy(
        update={
            'redistribution_status': 'fail',
            'windows_packaging_status': 'unsupported',
        }
    )
    payload = candidates.model_dump(mode='python')
    payload['candidates'] = [
        broken.model_dump(mode='python')
        if item['candidate_id'] == candidate.candidate_id
        else item
        for item in payload['candidates']
    ]
    broken_candidates = BakeoffCandidateManifest.model_validate(payload)

    report = build_production_adoption_readiness_report(
        benchmark,
        broken_candidates,
        profile,
        BakeoffReadinessEvidenceLedger(ledger_id='no-rebound-evidence'),
    )
    candidate_report = _candidate_report(report, broken.candidate_id)

    assert _check(
        candidate_report.hard_gate_results,
        'license_redistribution',
    ).status == 'FAIL'
    assert _check(
        candidate_report.hard_gate_results,
        'windows_packaging',
    ).status == 'FAIL'
    assert candidate_report.decision == 'NO_GO'


def test_negative_evidence_is_preserved_when_stale_and_superseded() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    fresh = ledger.records[0]
    stale_fail = BakeoffReadinessEvidenceRecord(
        evidence_id='historical-negative',
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        candidate_semantic_hash=None,
        r100a_manifest_id='historical-r100a',
        r100a_semantic_hash='2' * 64,
        target_kind='fixture',
        fixture_id=fresh.fixture_id,
        reported_status='fail',
        evidence_ref='docs/historical-negative.md',
    )
    ledger_with_history = ledger.model_copy(
        update={'records': ledger.records + (stale_fail,)}
    )

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger_with_history
    )
    candidate_report = _candidate_report(report, candidate.candidate_id)

    assert candidate_report.decision == 'READY'
    assert 'historical-negative' in candidate_report.negative_evidence_ids
    excluded = {item.evidence_id: item.reason for item in candidate_report.excluded_evidence}
    assert excluded['historical-negative'] == 'stale_r100a_manifest_id'


def test_adding_candidate_does_not_mutate_existing_candidate_result() -> None:
    benchmark, candidates, profile, candidate, ledger = _fully_qualified()
    before = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    before_candidate = _candidate_report(before, candidate.candidate_id)

    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    extra = pyroom.model_copy(
        update={
            'candidate_id': 'pyroomacoustics-extra-reference',
            'source_commit_sha': '3' * 40,
        }
    )
    payload = candidates.model_dump(mode='python')
    payload['candidates'] = list(payload['candidates'])
    payload['candidates'].append(extra.model_dump(mode='python'))
    expanded = BakeoffCandidateManifest.model_validate(payload)

    after = build_production_adoption_readiness_report(
        benchmark, expanded, profile, ledger
    )
    after_candidate = _candidate_report(after, candidate.candidate_id)

    assert after_candidate.decision == before_candidate.decision == 'READY'
    assert after_candidate.fixture_results == before_candidate.fixture_results
    assert after_candidate.hard_gate_results == before_candidate.hard_gate_results


def test_readiness_report_identity_is_deterministic() -> None:
    benchmark, candidates, profile, _, ledger = _fully_qualified()

    first = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    second = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )

    assert first == second
    assert first.semantic_hash() == second.semantic_hash()


def test_ledger_save_reopen_preserves_same_decision(
    tmp_path: Path,
) -> None:
    benchmark, candidates, profile, _, ledger = _fully_qualified()
    path = tmp_path / 'ledger.json'
    path.write_text(
        ledger.model_dump_json(indent=2),
        encoding='utf-8',
    )

    reopened = load_readiness_evidence_ledger(path)
    first = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    second = build_production_adoption_readiness_report(
        benchmark, candidates, profile, reopened
    )

    assert second.decision == first.decision
    assert second.semantic_hash() == first.semantic_hash()


def test_checked_in_readiness_is_no_go_with_current_mfem_fail() -> None:
    benchmark, candidates, profile = _authorities()
    ledger = load_readiness_evidence_ledger(READINESS_LEDGER_PATH)

    report = build_production_adoption_readiness_report(
        benchmark, candidates, profile, ledger
    )
    mfem = _candidate_report(report, 'mfem-v4.10-d964264')
    pffdtd = _candidate_report(report, 'pffdtd-main-aa319f6')

    assert report.decision == 'NO_GO'
    assert report.ready_candidate_ids == ()
    assert report.production_solver_selected is False

    mfem_concave = _check(mfem.fixture_results, 'wave-concave-l-room-v1')
    assert mfem_concave.status == 'FAIL'
    assert mfem_concave.evidence_ids == (
        'mfem-finite-record-concave-2026-09-19',
    )
    assert _check(
        mfem.hard_gate_results,
        'reproducible_authority',
    ).status == 'PASS'
    assert 'mfem-finite-record-concave-2026-09-19' in mfem.negative_evidence_ids

    pffdtd_concave = _check(pffdtd.fixture_results, 'wave-concave-l-room-v1')
    assert pffdtd_concave.status == 'BLOCKED'
    excluded = {item.evidence_id: item.reason for item in pffdtd.excluded_evidence}
    assert excluded['pffdtd-concave-known-negative'] == 'stale_r100a_authority'
    assert 'pffdtd-concave-known-negative' in pffdtd.negative_evidence_ids
