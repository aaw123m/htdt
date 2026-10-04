"""Guided acceptance wizard tests (REV48-HWGUIDE)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.acceptance_checks import (
    AUTO_CHECKS,
    CheckContext,
    check_campaign_registered,
    check_comparison_present,
    check_env_snapshot,
    check_gate_run_completed,
    check_persistence_probe,
    check_rew_engine_probe,
    check_rew_input_ready,
    run_auto_check,
)
from htdt.acceptance_gates import GATE_REGISTRY, collect_auto_checks, get_gate
from htdt.cad_acceptance import (
    AcceptanceStepRecord,
    build_acceptance_run,
    build_evidence_bundle,
    compute_run_status,
)
from htdt.cad_acceptance_repository import AcceptanceRunRepository
from htdt.cad_schema import NativeSchemaError


@pytest.fixture()
def repo(tmp_path: Path) -> AcceptanceRunRepository:
    return AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')


@pytest.fixture()
def ctx(tmp_path: Path, repo: AcceptanceRunRepository) -> CheckContext:
    return CheckContext(
        data_dir=tmp_path,
        db_path=tmp_path / 'cad-scenes.sqlite3',
        rew_base_url='http://127.0.0.1:4735',
        run_id='ac-test',
        step_id='step-1',
        repository=repo,
    )


class _FakeRew:
    def __init__(
        self, *, connected=True, count=0, version='REW 5.40', preflight=None
    ) -> None:
        self._status = {
            'connected': connected,
            'read_only': True,
            'base_url': 'http://127.0.0.1:4735',
            'measurement_count': count,
            'rew_version': version,
            'capabilities': [],
            'adapter_versions': {},
            'error': None if connected else 'refused',
        }
        self._preflight = preflight

    def status(self):
        return dict(self._status)

    def get_audio_preflight(self):
        if self._preflight is None:
            raise RuntimeError('no audio preflight')
        return self._preflight


def _umik_preflight(**java_overrides):
    java = {
        'input_device': 'UMIK-1 gain 18dB',
        'input_cal_file': 'C:/cal/umik90.txt',
        'input_cal_file_present': True,
        'input_endpoint_ready': True,
        'multichannel_ready': True,
        'output_channel_mapping': ['L', 'R', 'C'],
        'output_device': 'EXCL: RX-A4A',
    }
    java.update(java_overrides)
    return {
        'audio_ready': True,
        'driver': 'Java',
        'sample_rate_hz': 48000.0,
        'java': java,
        'warnings': [],
    }


# ----------------------------------------------------------------------
# gate manifest


def test_every_manifest_auto_check_is_registered():
    missing = collect_auto_checks() - set(AUTO_CHECKS)
    assert not missing


def test_gate_ids_unique_and_steps_have_ja_text():
    ids = [g.gate_id for g in GATE_REGISTRY]
    assert len(ids) == len(set(ids))
    for gate in GATE_REGISTRY:
        assert gate.title_ja
        assert gate.source_ref
        for step in gate.steps:
            assert step.title_ja
            assert step.instruction_ja
            assert step.source_ref, (
                f'{gate.gate_id}/{step.step_id} lacks doc provenance'
            )
        assert len(gate.manifest_sha256()) == 64


def test_windows_m10_covers_doc_items():
    gate = get_gate('windows-m10')
    assert len(gate.steps) == 12
    kinds = [s.kind for s in gate.steps]
    assert kinds.count('auto') == 4
    assert 'rew-version-record' in [s.step_id for s in gate.steps]


# ----------------------------------------------------------------------
# model


def test_build_run_all_pending_and_status_rollup():
    run = build_acceptance_run(
        GATE_REGISTRY[0], run_id='ac-1', environment={'hostname': 'h'}
    )
    assert run.status == 'in_progress'
    assert all(s.status == 'pending' for s in run.steps)
    steps = [
        s.model_copy(update={'status': 'passed', 'verdict_source': 'auto_check'})
        if s.auto_check
        else s.model_copy(
            update={'status': 'passed', 'verdict_source': 'human_confirm'}
        )
        for s in run.steps
    ]
    assert compute_run_status(steps) == 'passed'
    steps[0] = steps[0].model_copy(update={'status': 'skipped'})
    assert compute_run_status(steps) == 'partial'
    steps[0] = steps[0].model_copy(update={'status': 'failed'})
    assert compute_run_status(steps) == 'failed'


def test_resolved_step_requires_verdict_source():
    with pytest.raises(Exception):
        AcceptanceStepRecord(
            step_id='s', kind='auto', status='passed', auto_check='x'
        )


def test_bundle_labels_auto_vs_attested(repo: AcceptanceRunRepository):
    gate = get_gate('golden-path')
    run = repo.save(
        build_acceptance_run(gate, run_id='ac-b1', environment={})
    )
    steps = [
        run.steps[0].model_copy(
            update={'status': 'passed', 'verdict_source': 'auto_check'}
        ),
        run.steps[1].model_copy(
            update={
                'status': 'passed',
                'verdict_source': 'attestation',
                'attestation': 'UIのみで完走を確認',
            }
        ),
    ]
    run = repo.commit(run, steps)
    bundle = build_evidence_bundle(run, gate)
    assert bundle['status'] == 'passed'
    assert bundle['summary']['auto_verified'] == 1
    assert bundle['summary']['attested'] == 1
    assert bundle['schema'] == 'htdt.acceptance-evidence-bundle'


# ----------------------------------------------------------------------
# repository


def test_run_persists_and_resumes(repo: AcceptanceRunRepository, tmp_path: Path):
    gate = GATE_REGISTRY[0]
    run = repo.save(
        build_acceptance_run(gate, run_id='ac-resume', environment={})
    )
    # Simulate an app restart: a fresh repository over the same db file.
    reopened = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    latest = reopened.latest(run.run_id)
    assert latest is not None
    assert latest.status == 'in_progress'
    assert len(latest.steps) == len(gate.steps)
    # Advance one step on the reopened instance — the resume path.
    steps = list(latest.steps)
    steps[0] = steps[0].model_copy(
        update={'status': 'passed', 'verdict_source': 'auto_check'}
    )
    latest = reopened.commit(latest, steps)
    assert latest.revision == 2
    assert reopened.latest(run.run_id).steps[0].status == 'passed'


def test_chain_verification_and_tamper_detection(
    repo: AcceptanceRunRepository,
):
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-chain', environment={}
        )
    )
    steps = list(run.steps)
    steps[0] = steps[0].model_copy(
        update={'status': 'skipped', 'verdict_source': 'human_confirm'}
    )
    repo.commit(run, steps)
    head = repo.verify_run_chain('ac-chain')
    assert head.revision == 2

    # Tamper with an index column: the payload/index divergence trips the
    # replay probe.
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE htdt_acceptance_runs SET status='passed' "
            "WHERE run_id='ac-chain' AND revision=1"
        )
        connection.commit()
    with pytest.raises(NativeSchemaError):
        repo.verify_run_chain('ac-chain')
    # Tamper with the payload itself: the chained hash rejects it.
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE htdt_acceptance_runs SET status='in_progress', "
            "payload_json=REPLACE(payload_json, '\"in_progress\"', "
            "'\"passed\"') WHERE run_id='ac-chain' AND revision=1"
        )
        connection.commit()
    with pytest.raises(NativeSchemaError):
        repo.verify_run_chain('ac-chain')


def test_out_of_order_revision_rejected(repo: AcceptanceRunRepository):
    run = build_acceptance_run(
        get_gate('golden-path'), run_id='ac-seq', environment={}
    )
    bad = run.model_copy(update={'revision': 7})
    with pytest.raises(ValueError, match='revision'):
        repo.save(bad)


def test_evidence_attach_and_integrity(repo: AcceptanceRunRepository):
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-ev', environment={}
        )
    )
    ref = repo.attach_evidence(
        run.run_id,
        'preflight',
        kind='gate_log',
        filename='gate.log',
        payload=b'PASS everything',
    )
    assert ref.sha256 and ref.size_bytes == 15
    evidence = repo.evidence_for(run.run_id, 'preflight')
    assert len(evidence) == 1
    assert repo.verify_evidence_integrity(run.run_id) == []
    # Asset is byte-identical readable through the digest store.
    assert (
        repo.asset_store.read_verified(ref.sha256) == b'PASS everything'
    )
    # Corrupt the asset → integrity check reports it.
    repo.asset_store.asset_path(ref.sha256).write_bytes(b'corrupted')
    problems = repo.verify_evidence_integrity(run.run_id)
    assert problems and problems[0][0] == ref.evidence_id


# ----------------------------------------------------------------------
# auto-check executors


def test_env_snapshot_records_machine(ctx: CheckContext):
    result = check_env_snapshot(ctx, '')
    assert result.verdict == 'pass'
    assert result.evidence['environment']['hostname']


def test_rew_engine_probe_connected(ctx: CheckContext):
    ctx = CheckContext(
        **{**ctx.__dict__, 'rew_client': _FakeRew(count=3)}
    )
    result = check_rew_engine_probe(ctx, '')
    assert result.verdict == 'pass'
    assert result.evidence['status']['measurement_count'] == 3


def test_rew_engine_probe_offline_is_unavailable(ctx: CheckContext):
    ctx = CheckContext(
        **{**ctx.__dict__, 'rew_client': _FakeRew(connected=False)}
    )
    result = check_rew_engine_probe(ctx, '')
    assert result.verdict == 'unavailable'


def test_rew_input_ready_checks_umik_48k_cal(ctx: CheckContext):
    ctx = CheckContext(
        **{
            **ctx.__dict__,
            'rew_client': _FakeRew(preflight=_umik_preflight()),
        }
    )
    assert check_rew_input_ready(ctx, '').verdict == 'pass'
    ctx = CheckContext(
        **{
            **ctx.__dict__,
            'rew_client': _FakeRew(
                preflight=_umik_preflight(input_device='Built-in mic')
            ),
        }
    )
    result = check_rew_input_ready(ctx, '')
    assert result.verdict == 'fail'
    assert 'UMIK' in result.detail_ja


def test_persistence_probe_two_phase(
    repo: AcceptanceRunRepository, ctx: CheckContext
):
    # Phase 1: records the marker, asks for a restart.
    result = check_persistence_probe(ctx, '')
    assert result.verdict == 'deferred'
    # Phase 2 (post-restart, fresh repository view): marker verifies.
    reopened = AcceptanceRunRepository(ctx.db_path)
    ctx2 = CheckContext(
        **{**ctx.__dict__, 'repository': reopened}
    )
    result = check_persistence_probe(ctx2, '')
    assert result.verdict == 'pass'
    assert result.evidence['verified_after_restart'] is True


def test_campaign_and_comparison_checks_on_empty_db(ctx: CheckContext):
    assert check_campaign_registered(ctx, '').verdict == 'fail'
    assert check_comparison_present(ctx, '').verdict == 'fail'


def test_gate_run_completed_cross_reference(
    repo: AcceptanceRunRepository, ctx: CheckContext
):
    ctx = CheckContext(**{**ctx.__dict__, 'repository': repo})
    assert check_gate_run_completed(ctx, 'ux160').verdict == 'fail'
    gate = get_gate('golden-path')
    run = repo.save(
        build_acceptance_run(gate, run_id='ac-gp', environment={})
    )
    repo.commit(
        run,
        [
            s.model_copy(
                update={
                    'status': 'passed',
                    'verdict_source': 'auto_check'
                    if s.auto_check
                    else 'attestation',
                }
            )
            for s in run.steps
        ],
    )
    assert check_gate_run_completed(ctx, 'golden-path').verdict == 'pass'


def test_unknown_check_fails_closed(ctx: CheckContext):
    result = run_auto_check('does_not_exist', ctx)
    assert result.verdict == 'unavailable'


def test_check_exception_fails_closed(ctx: CheckContext, monkeypatch):
    def boom(_ctx, _arg):
        raise RuntimeError('kaboom')

    monkeypatch.setitem(AUTO_CHECKS, 'env_snapshot', boom)
    result = run_auto_check('env_snapshot', ctx)
    assert result.verdict == 'unavailable'
    assert 'kaboom' in result.detail_ja


def test_repo_scripts_resolve_from_repo_root():
    """Regression: _REPO_ROOT must point at the repository root so the
    gate-manifest script checks can find their scripts — parents[2]
    resolves to backend/ and every subprocess check was 'unavailable'."""
    from htdt.acceptance_checks import _REPO_ROOT

    for script in (
        'golden_path_preflight.py',
        'check_dependency_lock.py',
        'audit_o60_owned_room.py',
    ):
        assert (_REPO_ROOT / 'scripts' / script).is_file(), script


def test_finished_runs_stay_listed(repo: AcceptanceRunRepository, ctx: CheckContext):
    """A finished run must remain listed (replayable + re-exportable);
    in_progress_runs() still filters to resumable runs only."""
    gate = get_gate('golden-path')
    run = build_acceptance_run(gate, run_id='ac-finished', environment={})
    run = repo.save(run)
    repo.commit(
        run,
        [
            s.model_copy(
                update={'status': 'passed', 'verdict_source': 'attestation'}
            )
            for s in run.steps
        ],
    )
    assert repo.list_runs()[0].status == 'passed'
    assert repo.in_progress_runs() == []
    # The run list the page shows uses list_runs() — finished runs included.
    assert [r.run_id for r in repo.list_runs()] == ['ac-finished']
