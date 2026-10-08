"""#891 — Reference Theater self-test fixture + deterministic lane.

Covers: versioned manifest/payload pins, drift fail-closed on every
comparison, materialization + reopen idempotence + head-drift rejection,
the seven-step self-test lane verdict lattice (verified/diverged/
unauthorized/skipped → passed/failed/unauthorized/fixture_drift/blocked),
seal/id integrity + repository round-trip with tamper detection, the
``htdt selftest run`` exit-code lattice, the #886 wizard offer wiring and
the #867 benchmark manifest entry.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import htdt.cad_reference_theater as rt
from htdt.cad_calibration_deployment_repository import (
    DeploymentIntegrityError,
)
from htdt.cad_reference_theater import CadReferenceTheaterRun
from htdt.cad_scene import scene_content_hash
from htdt.cad_schema import NATIVE_SCHEMA_VERSION
from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES
from htdt.headless_cli import _Repositories, main as cli_main
from htdt.native_authority_audit import audit_table_modes
from htdt.first_run_wizard_state import (
    FirstRunWizardFacts,
    WizardStage,
    derive_wizard_progress,
)


DOC = 'doc-reference-theater-v1'
NOW = '2026-10-08T00:00:00Z'


def _repos(tmp_path: Path) -> _Repositories:
    return _Repositories(tmp_path / 'data')


def _run(capsys, *argv):
    code = cli_main([str(a) for a in argv])
    out = capsys.readouterr().out.strip().splitlines()
    assert out, f'no stdout for {argv}'
    return code, json.loads(out[-1])


# ---------------------------------------------------------------------------
# Fixture manifest + payload pins
# ---------------------------------------------------------------------------


def test_schema_registered() -> None:
    assert 'cad_reference_theater_runs' in NATIVE_SCHEMA_TABLES
    assert NATIVE_SCHEMA_VERSION >= 111
    assert audit_table_modes()['cad_reference_theater_runs'] == (
        'replay_canonical')


def test_manifest_sealed_and_stable() -> None:
    manifest = rt.REFERENCE_THEATER_MANIFEST
    assert manifest.manifest_id.startswith('rtman-')
    assert manifest.fixture_id == (
        f'reference-theater:{rt.REFERENCE_THEATER_FIXTURE_VERSION}')
    assert manifest.payload_sha256 == rt.reference_theater_payload_sha256()
    assert tuple(manifest.lane) == rt.REFERENCE_THEATER_STEPS
    assert manifest.provenance.private_data == 'none'
    assert manifest.provenance.proprietary_assets == ()
    assert manifest.benchmark_fixture_id == 'htdt-reference-theater-v1'
    rebuilt = rt.build_reference_theater_manifest(
        **{k: v for k, v in manifest.model_dump(mode='json').items()
           if k not in ('manifest_id', 'manifest_sha256')})
    assert rebuilt.manifest_id == manifest.manifest_id
    assert rebuilt.manifest_sha256 == manifest.manifest_sha256


def test_payload_loads_and_validates() -> None:
    payload = rt.load_reference_theater_payload()
    assert payload.document_id == DOC
    assert payload.fixture_version == (
        rt.REFERENCE_THEATER_FIXTURE_VERSION)
    assert payload.provenance is not None
    assert rt.reference_theater_available()


def test_payload_bytes_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tampered = tmp_path / 'tampered.json'
    tampered.write_text(
        rt.reference_theater_payload_path().read_text(
            encoding='utf-8').replace('rt-v1', 'rt-v2', 1),
        encoding='utf-8')
    monkeypatch.setattr(
        rt, 'reference_theater_payload_path', lambda: tampered)
    with pytest.raises(rt.ReferenceTheaterDriftError):
        rt.load_reference_theater_payload()
    assert not rt.reference_theater_available()


def test_payload_missing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rt, 'reference_theater_payload_path',
        lambda: tmp_path / 'absent.json')
    with pytest.raises(rt.ReferenceTheaterDriftError):
        rt.load_reference_theater_payload()


# ---------------------------------------------------------------------------
# Materialization — open the fixture as a real project
# ---------------------------------------------------------------------------


def test_materialize_creates_project(tmp_path: Path) -> None:
    repos = _repos(tmp_path)
    opened = rt.materialize_reference_theater(repos.scene, repos.library)
    assert opened.created
    assert opened.document_id == DOC
    assert opened.content_sha256 == (
        rt.REFERENCE_THEATER_MANIFEST.expected_scene_sha256)
    head = repos.scene.current_head(DOC)
    assert head is not None
    entry = repos.library.get_by_document_id(DOC)
    assert entry is not None and entry.project_id == opened.project_id


def test_materialize_reopen_is_idempotent(tmp_path: Path) -> None:
    repos = _repos(tmp_path)
    first = rt.materialize_reference_theater(repos.scene, repos.library)
    second = rt.materialize_reference_theater(repos.scene, repos.library)
    assert not second.created
    assert second.revision_id == first.revision_id
    assert second.project_id == first.project_id
    assert repos.scene.current_head(DOC) is not None
    revisions = repos.scene.list_revisions(DOC)
    assert len(revisions) == 1


def test_materialize_head_drift_fails_closed(tmp_path: Path) -> None:
    repos = _repos(tmp_path)
    rt.materialize_reference_theater(repos.scene, repos.library)
    head = repos.scene.current_head(DOC)
    assert head is not None
    # A divergent head under the same document id is user drift — the
    # fixture refuses to reopen it rather than silently accept it.
    drifted = head.document.model_copy(update={
        'room': head.document.room.model_copy(
            update={'width_m': head.document.room.width_m + 0.5})})
    assert scene_content_hash(drifted) != (
        rt.REFERENCE_THEATER_MANIFEST.expected_scene_sha256)
    repos.scene.save(drifted, parent_revision_id=head.revision_id)
    with pytest.raises(rt.ReferenceTheaterDriftError):
        rt.materialize_reference_theater(repos.scene, repos.library)


# ---------------------------------------------------------------------------
# The deterministic self-test lane
# ---------------------------------------------------------------------------


def test_selftest_lane_passes_with_authorized_simulated_deploy(
    tmp_path: Path,
) -> None:
    repos = _repos(tmp_path)
    result = rt.run_reference_theater_self_test(
        repos, authorize_apply='test-op')
    run = result.run
    assert run.outcome == 'passed'
    assert run.verdict == 'all_verified'
    assert all(s.verdict == 'verified' for s in run.steps)
    assert [s.step for s in run.steps] == list(
        rt.REFERENCE_THEATER_STEPS)
    assert run.deploy_is_simulated
    assert run.deploy_evidence_strength == 'machine_readback'
    assert run.deploy_record_ref is not None
    assert run.project_ref is not None
    assert run.report_sha256 is not None
    # Sealed + persisted: round-trip through the store re-verifies.
    stored = repos.reference_theater.get_run(run.run_id)
    assert stored is not None and stored.run_sha256 == run.run_sha256
    # The report artifact exists and carries the lane evidence.
    report = (
        repos.data_dir / 'exports' / 'reference-theater'
        / 'selftest-report.json')
    assert report.exists()


def test_selftest_lane_unauthorized_without_apply_flag(
    tmp_path: Path,
) -> None:
    repos = _repos(tmp_path)
    result = rt.run_reference_theater_self_test(repos)
    run = result.run
    assert run.outcome == 'unauthorized'
    assert run.verdict == 'authorize_apply_required'
    steps = {s.step: s for s in run.steps}
    assert steps['open'].verdict == 'verified'
    assert steps['deploy'].verdict == 'unauthorized'
    assert steps['verify'].verdict == 'skipped'
    assert steps['export'].verdict == 'verified'
    # The negative result is still sealed + persisted.
    assert repos.reference_theater.get_run(run.run_id) is not None


def test_selftest_lane_fixture_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = tmp_path / 'drifted.json'
    drifted.write_text(
        rt.reference_theater_payload_path().read_text(
            encoding='utf-8').replace('"screen_count"', '"scrn_count"'),
        encoding='utf-8')
    monkeypatch.setattr(
        rt, 'reference_theater_payload_path', lambda: drifted)
    repos = _repos(tmp_path)
    result = rt.run_reference_theater_self_test(
        repos, authorize_apply='test-op')
    run = result.run
    assert run.outcome == 'fixture_drift'
    assert {s.step: s.verdict for s in run.steps}['open'] == 'diverged'
    assert all(
        s.verdict == 'skipped'
        for s in run.steps if s.step != 'open')


def test_selftest_second_run_reuses_project(tmp_path: Path) -> None:
    repos = _repos(tmp_path)
    first = rt.run_reference_theater_self_test(
        repos, authorize_apply='op')
    second = rt.run_reference_theater_self_test(
        repos, authorize_apply='op')
    assert first.run.outcome == second.run.outcome == 'passed'
    runs = repos.reference_theater.list_runs(DOC)
    assert len(runs) == 2


def test_run_record_seal_integrity(tmp_path: Path) -> None:
    run = rt.run_reference_theater_self_test(
        _repos(tmp_path), authorize_apply='op').run
    # identity excludes the seal pair; sealed values verify on re-parse.
    identity = run.identity_payload()
    assert 'run_id' not in identity and 'run_sha256' not in identity
    rebuilt = rt.build_reference_theater_run(
        **run.model_dump(mode='json'))
    assert rebuilt.run_id == run.run_id
    assert rebuilt.run_sha256 == run.run_sha256


def test_run_record_rejects_inconsistent_passed(
    tmp_path: Path,
) -> None:
    """A 'passed' run requires every step verified — fail closed."""
    run = rt.run_reference_theater_self_test(
        _repos(tmp_path), authorize_apply='op').run
    payload = run.model_dump(mode='json')
    payload.pop('run_id')
    payload.pop('run_sha256')
    payload['steps'][0]['verdict'] = 'skipped'
    with pytest.raises(Exception):
        rt.build_reference_theater_run(**payload)


def test_run_repository_tamper_detection(tmp_path: Path) -> None:
    repos = _repos(tmp_path)
    run = rt.run_reference_theater_self_test(
        repos, authorize_apply='op').run
    with sqlite3.connect(repos.scene.path) as connection:
        connection.execute(
            'UPDATE cad_reference_theater_runs SET verdict = ? '
            'WHERE run_id = ?',
            ('all_verified_TAMPERED', run.run_id))
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repos.reference_theater.get_run(run.run_id)


def test_run_repository_rejects_forged_seal(tmp_path: Path) -> None:
    """A record whose stored sha doesn't match its payload can't be saved."""
    repos = _repos(tmp_path)
    run = rt.run_reference_theater_self_test(
        repos, authorize_apply='op').run
    forged = CadReferenceTheaterRun.model_construct(**{
        **run.model_dump(mode='json'),
        'verdict': 'forged',
        'run_sha256': 'f' * 64,
    })
    assert forged.run_id == run.run_id
    assert forged.run_sha256 != run.run_sha256
    with pytest.raises(DeploymentIntegrityError):
        repos.reference_theater.save_run(forged)


# ---------------------------------------------------------------------------
# Headless CLI integration — ``htdt selftest run``
# ---------------------------------------------------------------------------


def test_cli_selftest_run_succeeds(capsys, tmp_path: Path) -> None:
    code, env = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'selftest', 'run', '--authorize-apply', 'op')
    assert code == 0
    assert env['outcome'] == 'succeeded'
    assert env['verdict'] == 'all_verified'
    assert env['data']['fixture_version'] == (
        rt.REFERENCE_THEATER_FIXTURE_VERSION)
    assert env['data']['deploy_is_simulated'] is True
    assert env['data']['deploy_evidence_strength'] == 'machine_readback'
    steps = env['data']['steps']
    assert [s['step'] for s in steps] == list(
        rt.REFERENCE_THEATER_STEPS)
    assert all(s['verdict'] == 'verified' for s in steps)
    # The sealed headless run record persisted too.
    code2, env2 = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'records', 'list', '--kind', 'headless_run',
        '--verb', 'selftest.run')
    assert code2 == 0 and env2['data']['count'] == 1


def test_cli_selftest_unauthorized(capsys, tmp_path: Path) -> None:
    code, env = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'selftest', 'run')
    assert code == 4
    assert env['outcome'] == 'unauthorized'
    assert env['verdict'] == 'authorize_apply_required'


def test_cli_selftest_dry_run_writes_nothing(
    capsys, tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'd'
    code, env = _run(
        capsys, '--data-dir', data_dir, '--json',
        'selftest', 'run', '--dry-run')
    assert code == 0
    assert env['outcome'] == 'dry_run'
    assert env['verdict'] == 'planned'
    assert env['data']['lane'] == list(rt.REFERENCE_THEATER_STEPS)
    # --dry-run must not create the database or any sealed record.
    assert not (data_dir / 'cad-scenes.sqlite3').exists()


def test_cli_selftest_unknown_fixture_version(
    capsys, tmp_path: Path,
) -> None:
    code, env = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'selftest', 'run', '--fixture-version', 'rt-v0')
    assert code == 5
    assert env['outcome'] == 'missing_evidence'
    assert env['verdict'] == 'version_mismatch'


def test_cli_records_list_and_export(capsys, tmp_path: Path) -> None:
    _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'selftest', 'run', '--authorize-apply', 'op')
    code, env = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'records', 'list', '--kind', 'reference_theater_run')
    assert code == 0
    assert env['data']['count'] == 1
    run_id = env['data']['records'][0]['id']
    code, env = _run(
        capsys, '--data-dir', tmp_path / 'd', '--json',
        'records', 'export', '--kind', 'reference_theater_run',
        '--id', run_id)
    assert code == 0
    payload = env['data']['payload']
    assert payload['outcome'] == 'passed'
    assert payload['deploy_is_simulated'] is True


# ---------------------------------------------------------------------------
# #886 onboarding offer + #867 benchmark manifest entry
# ---------------------------------------------------------------------------


def test_wizard_offers_theater_only_when_needed() -> None:
    views = derive_wizard_progress(FirstRunWizardFacts(
        reference_theater_available=True))
    stage1 = next(v for v in views if v.stage is WizardStage.PROJECT_ROOM)
    assert stage1.offer_reference_theater
    # A project with a saved room no longer needs the offer.
    views = derive_wizard_progress(FirstRunWizardFacts(
        project_exists=True, room_saved=True,
        reference_theater_available=True))
    stage1 = next(v for v in views if v.stage is WizardStage.PROJECT_ROOM)
    assert not stage1.offer_reference_theater
    # Fixture drift (availability False) suppresses the offer entirely.
    views = derive_wizard_progress(FirstRunWizardFacts())
    stage1 = next(v for v in views if v.stage is WizardStage.PROJECT_ROOM)
    assert not stage1.offer_reference_theater


def test_benchmark_entry_shape_and_pins() -> None:
    entry = rt.reference_theater_benchmark_entry()
    assert entry['fixture_id'] == 'htdt-reference-theater-v1'
    assert entry['size'] == 'small'
    assert entry['entity_count'] == 13
    assert entry['document_id'] == DOC
    assert entry['document_sha256'] == (
        rt.REFERENCE_THEATER_MANIFEST.expected_scene_sha256)
    assert entry['generator_version'] == (
        rt.REFERENCE_THEATER_AUTHORITY_VERSION)


# ---------------------------------------------------------------------------
# Qt-free import guard (headless surfaces must never import Qt)
# ---------------------------------------------------------------------------


def test_headless_lane_imports_no_qt(tmp_path: Path) -> None:
    probe = tmp_path / 'probe.py'
    probe.write_text(
        'import sys\n'
        'import htdt.cad_reference_theater\n'
        'import htdt.cad_reference_theater_repository\n'
        'bad = [m for m in sys.modules if m.startswith(("PySide6", "PyQt"))]\n'
        'sys.exit(1 if bad else 0)\n',
        encoding='utf-8')
    src = str(Path(rt.__file__).resolve().parent.parent)
    result = subprocess.run(
        [sys.executable, str(probe)],
        env={**__import__('os').environ, 'PYTHONPATH': src},
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-500:]
