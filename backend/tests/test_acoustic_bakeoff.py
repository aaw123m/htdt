from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_bakeoff import (
    BakeoffAdoptionProfile,
    BakeoffCandidateManifest,
    BakeoffDecision,
    BakeoffFixtureEvidence,
    BakeoffHardGateEvidence,
    BakeoffObservableEvidence,
    BakeoffPlatform,
    BakeoffRun,
    applicable_fixture_ids,
    load_bakeoff_adoption_profile,
    load_bakeoff_candidate_manifest,
    preflight_summary,
    validate_bakeoff_decision,
    validate_bakeoff_run,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


ROOT = Path(__file__).resolve().parents[2]
R100A_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'
CANDIDATE_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_candidates.json'
ADOPTION_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100b_wave_adoption_profile.json'


def _authorities():
    return (
        load_acoustic_benchmark_manifest(R100A_PATH),
        load_bakeoff_candidate_manifest(CANDIDATE_PATH),
    )


def _adoption_profile() -> BakeoffAdoptionProfile:
    return load_bakeoff_adoption_profile(ADOPTION_PATH)


def _candidate(candidates: BakeoffCandidateManifest, candidate_id: str):
    return next(item for item in candidates.candidates if item.candidate_id == candidate_id)


def _not_run_fixture(fixture_id: str) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=fixture_id,
        status='not_run',
        adapter_id='htdt-r100b-probe',
        adapter_version='0',
        backend_version='not-run',
        precision='float64',
        diagnostics=('probe not executed yet',),
    )


def _gates(categories: list[str], status: str = 'not_run'):
    return tuple(
        BakeoffHardGateEvidence(
            category=category,
            status=status,
            summary='not evaluated by this schema test',
        )
        for category in categories
    )


def _run(candidate_id: str, fixture_ids: tuple[str, ...]) -> BakeoffRun:
    benchmark, candidates = _authorities()
    candidate = _candidate(candidates, candidate_id)
    if candidate.evaluation_scope == 'shipping_candidate':
        categories = [
            'physics_correctness',
            'cpu_baseline',
            'windows_packaging',
            'license_redistribution',
            'required_capability',
            'reproducible_authority',
        ]
    else:
        categories = ['physics_correctness', 'reproducible_authority']

    return BakeoffRun(
        run_id=f'test-{candidate_id}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=BakeoffPlatform(
            os='GitHub Actions Windows',
            architecture='x86_64',
            python_version='3.12',
            cpu='CI runner',
            logical_threads=4,
            thread_budget=4,
            gpu=None,
            device_notes='schema fixture only',
        ),
        fixture_evidence=tuple(_not_run_fixture(item) for item in fixture_ids),
        hard_gates=_gates(categories),
    )


def test_candidate_manifest_is_version_pinned_and_covers_primary_roles() -> None:
    benchmark, candidates = _authorities()

    assert candidates.schema_version == 'r100b-candidates-1'
    assert {item.role for item in candidates.candidates} == {
        'wave_primary_evaluation',
        'wave_reference',
        'geometric_reference',
    }

    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')
    assert pffdtd.source_commit_sha == 'aa319f6c86517cb95aabfae8656277da62c3ead5'
    assert pffdtd.windows_packaging_status == 'linux_upstream_port_required'

    mfem = _candidate(candidates, 'mfem-v4.10-d964264')
    assert mfem.source_ref == 'v4.10'
    assert mfem.source_commit_sha == 'd964264cdb9a13e94a201b6c236c7721e0c8765f'

    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    assert pyroom.source_ref == 'v0.10.1'
    assert pyroom.evaluation_scope == 'reference_only'

    profile = _adoption_profile()
    summary = preflight_summary(benchmark, candidates, profile)
    assert set(summary['uncovered_fixture_ids']) == {
        'hybrid-overlap-continuity-v1',
    }
    assert summary['adoption_profile']['profile_id'] == profile.profile_id
    pffdtd_summary = next(
        item for item in summary['candidates'] if item['candidate_id'] == pffdtd.candidate_id
    )
    assert pffdtd_summary['adoption_missing_capabilities'] == [
        'portal_continuity',
        'wave_radiation_termination',
    ]
    mfem_summary = next(
        item for item in summary['candidates'] if item['candidate_id'] == mfem.candidate_id
    )
    assert mfem_summary['adoption_missing_capabilities'] == []
    assert 'wave-portal-split-room-v1' in mfem_summary['applicable_fixture_ids']
    assert (
        'wave-explicit-radiation-termination-v1'
        in mfem_summary['applicable_fixture_ids']
    )


def test_adoption_profile_must_classify_every_r100a_fixture() -> None:
    benchmark, candidates = _authorities()
    profile = _adoption_profile()
    incomplete = profile.model_copy(
        update={'deferred_fixture_ids': profile.deferred_fixture_ids[:-1]}
    )

    with pytest.raises(ValueError, match='leaves benchmark fixtures unclassified'):
        preflight_summary(benchmark, candidates, incomplete)


def test_probe_capabilities_are_not_accepted_capabilities() -> None:
    benchmark, candidates = _authorities()
    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')

    applicable = set(applicable_fixture_ids(benchmark, pffdtd))
    assert 'wave-rigid-rectangular-modes-v1' in applicable
    assert 'wave-normal-incidence-impedance-v1' in applicable
    assert 'wave-portal-split-room-v1' not in applicable

    run = _run(pffdtd.candidate_id, tuple(sorted(applicable)))
    validate_bakeoff_run(benchmark, candidates, run)

    assert all(item.status == 'not_run' for item in run.fixture_evidence)
    assert all(item.status == 'not_run' for item in run.hard_gates)


def test_mfem_radiation_probe_permission_is_not_pass_evidence() -> None:
    benchmark, candidates = _authorities()
    mfem = _candidate(candidates, 'mfem-v4.10-d964264')
    fixture_id = 'wave-explicit-radiation-termination-v1'

    assert 'wave_radiation_termination' in mfem.probe_capabilities
    assert fixture_id in applicable_fixture_ids(benchmark, mfem)

    run = _run(mfem.candidate_id, (fixture_id,))
    validate_bakeoff_run(benchmark, candidates, run)
    assert run.fixture_evidence[0].status == 'not_run'


def test_r100a_semantic_hash_mismatch_is_rejected() -> None:
    benchmark, candidates = _authorities()
    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')
    run = _run(pffdtd.candidate_id, ('wave-rigid-rectangular-modes-v1',))
    payload = run.model_dump(mode='python')
    payload['r100a_semantic_hash'] = '0' * 64

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='semantic hash'):
        validate_bakeoff_run(benchmark, candidates, altered)


def test_unknown_fixture_evidence_is_rejected() -> None:
    benchmark, candidates = _authorities()
    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')
    run = _run(pffdtd.candidate_id, ('wave-rigid-rectangular-modes-v1',))
    payload = run.model_dump(mode='python')
    payload['fixture_evidence'][0]['fixture_id'] = 'not-an-r100a-fixture'

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='unknown R100A fixture'):
        validate_bakeoff_run(benchmark, candidates, altered)


def test_candidate_cannot_execute_fixture_without_probe_capability() -> None:
    benchmark, candidates = _authorities()
    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')
    run = _run(pffdtd.candidate_id, ('wave-portal-split-room-v1',))
    payload = run.model_dump(mode='python')
    payload['fixture_evidence'][0].update(
        status='fail',
        evidence_ref='artifacts/r100b/pffdtd/portal.json',
        backend_version='pffdtd-pinned',
        compile_s=1.0,
        solve_s=1.0,
        postprocess_s=1.0,
        peak_ram_mb=1.0,
        output_mb=1.0,
    )

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='without all required probe capabilities'):
        validate_bakeoff_run(benchmark, candidates, altered)


def test_production_selection_is_blocked_when_required_capability_is_omitted() -> None:
    benchmark, candidates = _authorities()
    profile = _adoption_profile()
    pffdtd = _candidate(candidates, 'pffdtd-main-aa319f6')
    run = _run(pffdtd.candidate_id, applicable_fixture_ids(benchmark, pffdtd))

    decision = BakeoffDecision(
        status='selected',
        selected_candidate_id=pffdtd.candidate_id,
        accepted_run_id=run.run_id,
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256=profile.semantic_hash(),
        rationale='portal capability must not disappear from production adoption',
    )

    with pytest.raises(ValueError, match='missing required capabilities.*portal_continuity'):
        validate_bakeoff_decision(
            benchmark, candidates, (run,), decision, profile
        )


def test_selected_decision_requires_current_adoption_profile_binding() -> None:
    profile = _adoption_profile()

    with pytest.raises(ValueError, match='exact adoption profile binding'):
        BakeoffDecision(
            status='selected',
            selected_candidate_id='candidate',
            accepted_run_id='run',
            rationale='missing binding',
        )

    decision = BakeoffDecision(
        status='selected',
        selected_candidate_id='candidate',
        accepted_run_id='run',
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256='0' * 64,
        rationale='stale profile binding',
    )
    benchmark, candidates = _authorities()
    with pytest.raises(ValueError, match='profile binding is stale'):
        validate_bakeoff_decision(
            benchmark, candidates, (), decision, profile
        )


def test_reference_only_candidate_cannot_be_selected_for_production() -> None:
    benchmark, candidates = _authorities()
    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    run = _run(pyroom.candidate_id, applicable_fixture_ids(benchmark, pyroom))

    profile = _adoption_profile()
    decision = BakeoffDecision(
        status='selected',
        selected_candidate_id=pyroom.candidate_id,
        accepted_run_id=run.run_id,
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256=profile.semantic_hash(),
        rationale='schema guard test',
    )

    with pytest.raises(ValueError, match='reference-only'):
        validate_bakeoff_decision(
            benchmark, candidates, (run,), decision, profile
        )


def _passing_wave_fixture(fixture) -> BakeoffFixtureEvidence:
    observables = []
    for expected in fixture.observables:
        kwargs = {
            'observable_id': expected.observable_id,
            'status': 'pass',
            'summary': 'synthetic adoption-gate coverage evidence',
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
        adapter_id='synthetic-adoption-gate',
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


def _passing_gates(categories: list[str]) -> tuple[BakeoffHardGateEvidence, ...]:
    return tuple(
        BakeoffHardGateEvidence(
            category=category,
            status='pass',
            evidence_ref=f'artifact:test/gate-{category}.json',
            summary='synthetic adoption-gate PASS',
        )
        for category in categories
    )


def _fully_covered_wave_authority():
    benchmark, candidates = _authorities()
    profile = _adoption_profile()
    base = _candidate(candidates, 'pffdtd-main-aa319f6')
    upgraded = base.model_copy(
        update={
            'probe_capabilities': tuple(
                sorted(
                    set(base.probe_capabilities)
                    | set(profile.required_capabilities)
                )
            )
        }
    )
    candidate_payload = candidates.model_dump(mode='python')
    candidate_payload['candidates'] = [
        upgraded.model_dump(mode='python')
        if item['candidate_id'] == base.candidate_id
        else item
        for item in candidate_payload['candidates']
    ]
    upgraded_manifest = BakeoffCandidateManifest.model_validate(candidate_payload)

    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    categories = [
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    ]
    run = BakeoffRun(
        run_id='synthetic-fully-covered-wave-run',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=upgraded_manifest.semantic_hash(),
        candidate_id=upgraded.candidate_id,
        candidate_source_commit_sha=upgraded.source_commit_sha,
        platform=BakeoffPlatform(
            os='test',
            architecture='x86_64',
            python_version='3.12',
            cpu='test',
            logical_threads=4,
            thread_budget=4,
            gpu=None,
            device_notes='synthetic adoption validator test',
        ),
        fixture_evidence=tuple(
            _passing_wave_fixture(fixture_by_id[fixture_id])
            for fixture_id in profile.required_fixture_ids
        ),
        hard_gates=_passing_gates(categories),
    )
    return benchmark, upgraded_manifest, profile, upgraded, run


def test_fully_covered_shipping_wave_candidate_can_pass_adoption_gate() -> None:
    benchmark, candidates, profile, candidate, run = _fully_covered_wave_authority()
    decision = BakeoffDecision(
        status='selected',
        selected_candidate_id=candidate.candidate_id,
        accepted_run_id=run.run_id,
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256=profile.semantic_hash(),
        rationale='synthetic complete adoption evidence',
    )

    validate_bakeoff_decision(
        benchmark, candidates, (run,), decision, profile
    )


def test_adoption_gate_rejects_missing_or_nonpassing_required_fixture() -> None:
    benchmark, candidates, profile, candidate, run = _fully_covered_wave_authority()
    decision = BakeoffDecision(
        status='selected',
        selected_candidate_id=candidate.candidate_id,
        accepted_run_id=run.run_id,
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256=profile.semantic_hash(),
        rationale='fixture completeness guard',
    )

    missing = run.model_copy(update={'fixture_evidence': run.fixture_evidence[:-1]})
    with pytest.raises(ValueError, match='required adoption fixtures'):
        validate_bakeoff_decision(
            benchmark, candidates, (missing,), decision.model_copy(
                update={'accepted_run_id': missing.run_id}
            ), profile
        )

    blocked_items = list(run.fixture_evidence)
    target = blocked_items[-1]
    blocked_items[-1] = target.model_copy(
        update={
            'status': 'blocked',
            'observables': (),
            'diagnostics': ('synthetic blocked fixture',),
        }
    )
    blocked = run.model_copy(update={'run_id': 'synthetic-blocked-wave-run', 'fixture_evidence': tuple(blocked_items)})
    blocked_decision = decision.model_copy(update={'accepted_run_id': blocked.run_id})
    with pytest.raises(ValueError, match='required adoption fixtures'):
        validate_bakeoff_decision(
            benchmark, candidates, (blocked,), blocked_decision, profile
        )


def _passing_geometric_evidence(*, direct_length_absolute_error: float = 0.0, solve_s: float = 1.0):
    return BakeoffFixtureEvidence(
        fixture_id='geometric-direct-first-reflection-v1',
        status='pass',
        evidence_ref='artifacts/r100b/pyroom/direct-first-reflection.json',
        adapter_id='htdt-r100b-pyroom-reference',
        adapter_version='0',
        backend_version='0.10.1',
        precision='float64',
        compile_s=1.0,
        solve_s=solve_s,
        postprocess_s=1.0,
        peak_ram_mb=128.0,
        disk_mb=2.0,
        output_mb=1.0,
        observables=(
            BakeoffObservableEvidence(
                observable_id='direct-length',
                status='pass',
                summary='closed-form comparison',
                absolute_error=direct_length_absolute_error,
                relative_error=0.0,
            ),
            BakeoffObservableEvidence(
                observable_id='direct-delay',
                status='pass',
                summary='closed-form comparison',
                absolute_error=0.0,
                relative_error=0.0,
            ),
            BakeoffObservableEvidence(
                observable_id='first-reflection-point-ymin',
                status='pass',
                summary='closed-form comparison',
                absolute_error=0.0,
                relative_error=0.0,
            ),
            BakeoffObservableEvidence(
                observable_id='first-reflection-length-ymin',
                status='pass',
                summary='closed-form comparison',
                absolute_error=0.0,
                relative_error=0.0,
            ),
        ),
    )


def test_passing_observable_cannot_exceed_r100a_tolerance() -> None:
    benchmark, candidates = _authorities()
    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    run = _run(pyroom.candidate_id, ('geometric-direct-first-reflection-v1',))
    payload = run.model_dump(mode='python')
    payload['fixture_evidence'] = list(payload['fixture_evidence'])
    payload['fixture_evidence'][0] = _passing_geometric_evidence(
        direct_length_absolute_error=2e-6
    ).model_dump(mode='python')

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='exceeds absolute_error tolerance'):
        validate_bakeoff_run(benchmark, candidates, altered)


def test_passing_fixture_cannot_exceed_r100a_resource_budget() -> None:
    benchmark, candidates = _authorities()
    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    run = _run(pyroom.candidate_id, ('geometric-direct-first-reflection-v1',))
    payload = run.model_dump(mode='python')
    payload['fixture_evidence'] = list(payload['fixture_evidence'])
    payload['fixture_evidence'][0] = _passing_geometric_evidence(
        solve_s=61.0
    ).model_dump(mode='python')

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='exceeds resource budget'):
        validate_bakeoff_run(benchmark, candidates, altered)


def test_passing_fixture_cannot_exceed_r100a_disk_budget() -> None:
    benchmark, candidates = _authorities()
    pyroom = _candidate(candidates, 'pyroomacoustics-v0.10.1-f02b01d')
    run = _run(pyroom.candidate_id, ('geometric-direct-first-reflection-v1',))
    payload = run.model_dump(mode='python')
    payload['fixture_evidence'] = list(payload['fixture_evidence'])
    evidence = _passing_geometric_evidence().model_dump(mode='python')
    evidence['disk_mb'] = 2049.0
    payload['fixture_evidence'][0] = evidence

    altered = BakeoffRun.model_validate(payload)
    with pytest.raises(ValueError, match='disk_mb'):
        validate_bakeoff_run(benchmark, candidates, altered)
