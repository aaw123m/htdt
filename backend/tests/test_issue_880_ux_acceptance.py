"""Tests for issue #880 — owned-Windows UX acceptance evidence runner.

Covers the sealed ``cad_ux_acceptance_bundle_records`` authority
(seal/id integrity, repository round-trip, tamper detection, every
verdict path — fail-closed especially), the runner's bundle assembly
(deterministic attempt naming, artifact hashing, manual-checklist
separation, baseline comparison), and an offscreen-capable self-test
that produces a real sample bundle through the in-process driver.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))
# the runner imports its sibling check library (`import ux160_driver`)
sys.path.insert(0, str(ROOT / 'scripts'))

from htdt.cad_authority_resolver import AuthorityRef  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_schema import connect_sqlite  # noqa: E402
from htdt.cad_ux_acceptance_evidence import (  # noqa: E402
    CadUxAcceptanceBundleRecord,
    UxArtifactRef,
    UxCheckpointOutcome,
    UxEnvironmentBinding,
    UxManualChecklistItem,
    build_ux_acceptance_bundle_record,
    derive_bundle_verdict,
)
from htdt.cad_ux_acceptance_evidence_repository import (  # noqa: E402
    CadUxAcceptanceEvidenceRepository,
    DeploymentIntegrityError,
)
from htdt.native_authority_audit import audit_table_modes  # noqa: E402
from htdt.native_row_integrity import (  # noqa: E402
    assert_row_integrity_registry_complete,
    scan_native_row_integrity,
)


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / 'scripts' / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # register before exec: dataclass/annotation introspection reads
    # sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


uxrun = _load_script('ux160_acceptance_run', 'ux160_acceptance_run.py')

UTC = '2026-10-08T01:00:00+00:00'


def _env() -> dict:
    return {
        'build_version': '0.2.0.dev0',
        'build_display_version': '0.2.0.dev0+gdeadbee5',
        'build_commit_sha': 'd' * 40,
        'build_dirty': False,
        'build_source': 'checkout',
        'python_version': '3.12.10',
        'python_implementation': 'CPython',
        'platform': 'Windows-2022Server',
        'machine': 'AMD64',
        'os_version': '10.0.20348',
        'qt_version': '6.10.0',
        'pyside_version': '6.10.0',
        'qpa_platform': 'windows',
        'qt_scale_factor_env': '1.25',
        'renderer_id': 'pyvista-0.46/vtk-9.5/Mesa 26',
        'screens': [{
            'name': 'DISPLAY1',
            'geometry': [0, 0, 1920, 1080],
            'available_geometry': [0, 0, 1920, 1040],
            'logical_dpi': 120.0,
            'device_pixel_ratio': 1.25,
        }],
    }


def _checkpoint(cid: str, status: str = 'pass',
                kind: str = 'navigate') -> UxCheckpointOutcome:
    return UxCheckpointOutcome(
        checkpoint_id=cid, kind=kind, status=status,
        target=cid.split(':', 1)[-1],
        detail=None if status == 'pass' else f'{status} detail',
        evidence=(),
    )


def _record(**overrides) -> CadUxAcceptanceBundleRecord:
    fields = dict(
        document_id='document-1',
        matrix_id='ux160',
        row_id='dpi-125-seeded',
        run_attempt=1,
        scenario='seeded',
        scale_factor='1.25',
        capture_mode='owned_windows',
        environment=_env(),
        verdict='evidence_captured',
        checkpoints=(
            _checkpoint('launch:initial_state', 'pass', 'launch'),
            _checkpoint('navigate:measurement'),
            _checkpoint('navigate:support'),
        ),
        bundle_ref='ux160/dpi-125-seeded/attempt-001',
        manifest_sha256='a' * 64,
        report_sha256='b' * 64,
        artifacts=(
            UxArtifactRef(
                name='shots/measurement.png', kind='screenshot',
                sha256='c' * 64, byte_count=1234),
        ),
        manual_items=(
            UxManualChecklistItem(
                item_id='manual:pointer-vtk-gestures',
                description='pointer gestures'),
        ),
        non_claims=('automated evidence is not acceptance',),
        started_at_utc=UTC,
        finished_at_utc='2026-10-08T01:05:00+00:00',
    )
    fields.update(overrides)
    return build_ux_acceptance_bundle_record(**fields)


def _repo(tmp_path: Path) -> CadUxAcceptanceEvidenceRepository:
    return CadUxAcceptanceEvidenceRepository(
        SceneRepository(tmp_path / 'ux-evidence.sqlite3'))


def _evidence_repo(work_dir: Path) -> CadUxAcceptanceEvidenceRepository:
    """Repository on the runner's real evidence-db location."""

    return CadUxAcceptanceEvidenceRepository(
        SceneRepository(
            work_dir / 'ux-acceptance-evidence' / uxrun.EVIDENCE_DB_NAME))


# ---------------------------------------------------------------------------
# Sealed model integrity
# ---------------------------------------------------------------------------


def test_seal_produces_stable_id_and_sha() -> None:
    record = _record()
    assert record.bundle_id.startswith('uxbnd-')
    assert len(record.bundle_sha256) == 64
    # Identity is content-derived: identical inputs seal identically.
    assert _record().bundle_id == record.bundle_id
    assert _record().bundle_sha256 == record.bundle_sha256
    # ...and a different row produces a different identity.
    assert _record(row_id='dpi-150-seeded').bundle_id != record.bundle_id


def test_forged_sha_rejected() -> None:
    record = _record()
    forged = record.model_copy(update={'bundle_sha256': '0' * 64})
    with pytest.raises(ValueError):
        CadUxAcceptanceBundleRecord.model_validate_json(
            forged.model_dump_json())


def test_forged_payload_rejected_on_save(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _record()
    # model_copy skips validators: verdict flipped without resealing.
    forged = record.model_copy(update={'verdict': 'capture_incomplete'})
    with pytest.raises(DeploymentIntegrityError):
        repo.save_bundle(forged)


def test_validator_fail_closed_paths() -> None:
    # evidence_captured cannot coexist with unexecuted checkpoints.
    with pytest.raises(ValueError, match='blocked'):
        _record(checkpoints=(
            _checkpoint('launch:initial_state'),
            _checkpoint('navigate:room', 'blocked'),
        ))
    with pytest.raises(ValueError, match='skipped'):
        _record(checkpoints=(
            _checkpoint('launch:initial_state'),
            _checkpoint('focus:room', 'skipped', 'focus'),
        ))
    # capture_failed must say why.
    with pytest.raises(ValueError, match='failure_detail'):
        _record(verdict='capture_failed')
    # Checkpoint tallies must agree with the embedded outcomes.
    fields = _record().model_dump(mode='python')
    fields['checkpoints_total'] = 99
    with pytest.raises(ValueError, match='checkpoints_total'):
        CadUxAcceptanceBundleRecord(**fields)
    # Manual items outstanding can never read as review-complete.
    fields = _record().model_dump(mode='python')
    fields['review_state'] = 'manual_review_none'
    fields['bundle_sha256'] = '0' * 64
    fields['bundle_id'] = 'uxbnd-' + '0' * 24
    with pytest.raises(ValueError, match='manual_review_remaining'):
        CadUxAcceptanceBundleRecord(**fields)
    # Fixture captures must carry the offscreen non-claim.
    with pytest.raises(ValueError, match='offscreen'):
        _record(capture_mode='offscreen_fixture')


def test_derive_bundle_verdict_vocabulary() -> None:
    ok = (_checkpoint('a'), _checkpoint('b', 'finding'))
    assert derive_bundle_verdict(ok, driver_completed=True) == (
        'evidence_captured')
    # findings are captured evidence, not a downgrade
    assert derive_bundle_verdict(
        (_checkpoint('a', 'finding'),), driver_completed=True) == (
        'evidence_captured')
    assert derive_bundle_verdict(
        (_checkpoint('a'), _checkpoint('b', 'blocked')),
        driver_completed=True) == 'capture_incomplete'
    assert derive_bundle_verdict(
        (_checkpoint('a'), _checkpoint('b', 'skipped')),
        driver_completed=True) == 'capture_incomplete'
    # a dead driver is failed even if earlier checkpoints all passed
    assert derive_bundle_verdict(
        ok, driver_completed=False,
        failure_detail='crash') == 'capture_failed'
    # no checkpoints at all is still honest captured-evidence of an
    # empty sweep — the run completed and recorded its emptiness
    assert derive_bundle_verdict(
        (), driver_completed=True) == 'evidence_captured'


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def test_repository_roundtrip_and_attempt_allocation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _record()
    repo.save_bundle(first)
    # idempotent re-save of the identical seal
    repo.save_bundle(first)
    got = repo.get_bundle(first.bundle_id)
    assert got is not None and got.bundle_sha256 == first.bundle_sha256
    assert got == first

    second = _record(
        run_attempt=2,
        bundle_ref='ux160/dpi-125-seeded/attempt-002',
        finished_at_utc='2026-10-08T01:10:00+00:00',
    )
    repo.save_bundle(second)

    assert [r.run_attempt for r in repo.list_bundles()] == [1, 2]
    rows = repo.list_bundles_for_row('ux160', 'dpi-125-seeded')
    assert len(rows) == 2
    assert repo.list_bundles_for_row('ux160', 'dpi-150-seeded') == ()
    assert repo.list_bundles('other-document') == ()
    assert repo.next_run_attempt('ux160', 'dpi-125-seeded') == 3
    assert repo.next_run_attempt('ux160', 'unseen-row') == 1

    # An id forged onto a different payload is rejected before any
    # append happens — ids are sha-derived so a re-used id can only
    # ever name the identical seal (idempotent re-save above).
    forged = _record(
        run_attempt=2,
        finished_at_utc='2026-10-08T01:11:00+00:00',
    ).model_copy(update={'bundle_id': second.bundle_id})
    with pytest.raises(DeploymentIntegrityError):
        repo.save_bundle(forged)


def test_repository_tamper_detection(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _record()
    repo.save_bundle(record)
    with connect_sqlite(repo.path) as connection, connection:
        connection.execute(
            'UPDATE cad_ux_acceptance_bundle_records '
            "SET verdict='capture_failed' WHERE bundle_id=?",
            (record.bundle_id,))
    with pytest.raises(DeploymentIntegrityError):
        repo.get_bundle(record.bundle_id)


def test_audit_and_row_integrity_wiring(tmp_path: Path) -> None:
    assert_row_integrity_registry_complete()
    assert audit_table_modes()['cad_ux_acceptance_bundle_records'] == (
        'replay_canonical')
    repo = _repo(tmp_path)
    repo.save_bundle(_record())
    with connect_sqlite(repo.path) as connection:
        drifts = scan_native_row_integrity(connection)
    assert drifts == ()


# ---------------------------------------------------------------------------
# Runner bundle assembly (no Qt needed)
# ---------------------------------------------------------------------------


def _assemble(tmp_path: Path, **overrides) -> dict:
    fields = dict(
        work_dir=tmp_path,
        matrix_id='ux160',
        row_id='dpi-125-seeded',
        scenario='seeded',
        scale_factor='1.25',
        capture_mode='owned_windows',
        document_id='document-1',
        checkpoints=[
            {'checkpoint_id': 'launch:initial_state', 'kind': 'launch',
             'status': 'pass', 'target': 'document-1', 'detail': None,
             'evidence': []},
            {'checkpoint_id': 'navigate:measurement', 'kind': 'navigate',
             'status': 'pass', 'target': 'measurement', 'detail': None,
             'evidence': []},
        ],
        metrics={'window_allocated': [1366, 860], 'destinations': {
            'measurement': {'navigated': True,
                            'page_size': [1200, 800],
                            'overflow_count': 0}}},
        environment=_env(),
        driver_results=[{'phase': 'main', 'clean_exit': True,
                         'finished_utc': UTC}],
        driver_completed=True,
        failure_detail=None,
        baseline_manifest=None,
        started_at_utc=UTC,
    )
    fields.update(overrides)
    return uxrun.assemble_bundle(**fields)


def test_bundle_layout_manifest_and_pointer(tmp_path: Path) -> None:
    # a canonical screenshot exists on disk before assembly
    attempt_dir = tmp_path / 'ux160' / 'dpi-125-seeded' / 'attempt-001'
    shots = attempt_dir / 'shots'
    shots.mkdir(parents=True)
    png = b'\x89PNG\r\n\x1a\n' + b'evidence' * 16
    (shots / 'measurement.png').write_bytes(png)

    manifest = _assemble(tmp_path, attempt_dir=attempt_dir, run_attempt=1)

    assert manifest['format'] == 'htdt-ux-acceptance-bundle-1'
    assert manifest['verdict'] == 'evidence_captured'
    # manual items always remain — automated evidence never accepts a row
    assert manifest['review_state'] == 'manual_review_remaining'
    assert all(i['requires_human'] for i in manifest['manual_items'])
    assert manifest['baseline']['verdict'] == 'not_run'
    names = {a['name'] for a in manifest['artifacts']}
    assert {'shots/measurement.png', 'report.md'} <= names
    shot = next(a for a in manifest['artifacts']
                if a['name'] == 'shots/measurement.png')
    assert shot['byte_count'] == len(png) and len(shot['sha256']) == 64

    bundle_dir = tmp_path / 'ux160' / 'dpi-125-seeded' / 'attempt-001'
    assert (bundle_dir / 'manifest.json').is_file()
    assert (bundle_dir / 'report.md').read_text(
        encoding='utf-8').find('manual:pointer-vtk-gestures') >= 0

    latest = json.loads(io.open(
        tmp_path / 'ux160' / 'dpi-125-seeded' / 'latest.json',
        encoding='utf-8').read())
    assert latest['latest_attempt'] == 1
    assert latest['bundle_ref'] == 'ux160/dpi-125-seeded/attempt-001'
    assert latest['record']['table'] == 'cad_ux_acceptance_bundle_records'

    # the sealed record round-trips and binds the manifest bytes
    repo = _evidence_repo(tmp_path)
    record = repo.get_bundle(latest['record']['bundle_id'])
    assert record is not None
    assert record.bundle_sha256 == latest['record']['bundle_sha256']
    assert record.manifest_sha256 == uxrun._sha256_file(
        bundle_dir / 'manifest.json')
    assert record.verdict == 'evidence_captured'
    assert record.review_state == 'manual_review_remaining'


def test_attempt_numbering_is_monotonic(tmp_path: Path) -> None:
    first = _assemble(tmp_path)
    second = _assemble(tmp_path)
    assert (first['run_attempt'], second['run_attempt']) == (1, 2)
    assert second['bundle_ref'].endswith('attempt-002')
    # a third attempt compared against the latest manifest
    third = _assemble(tmp_path, baseline_manifest=second)
    assert third['baseline']['verdict'] in ('matched', 'diverged')
    assert third['baseline']['ref'] == second['bundle_ref']


def test_failed_and_incomplete_bundles(tmp_path: Path) -> None:
    failed = _assemble(
        tmp_path, row_id='dpi-150-seeded', driver_completed=False,
        checkpoints=[],
        failure_detail='main driver crashed: rc=-1073740791')
    assert failed['verdict'] == 'capture_failed'
    assert failed['failure_detail'].startswith('main driver crashed')

    incomplete = _assemble(
        tmp_path, row_id='dpi-200-seeded',
        checkpoints=[
            {'checkpoint_id': 'navigate:room', 'kind': 'navigate',
             'status': 'blocked', 'target': 'room',
             'detail': 'blocked: dialog open', 'evidence': []},
        ])
    assert incomplete['verdict'] == 'capture_incomplete'


def test_baseline_comparison(tmp_path: Path) -> None:
    base_metrics = {'window_allocated': [1366, 860], 'destinations': {
        'measurement': {'navigated': True, 'page_size': [1200, 800],
                        'overflow_count': 0}}}
    same = dict(base_metrics)
    verdict, divergences = uxrun.compare_baseline(base_metrics, same)
    assert verdict == 'matched' and divergences == []
    changed = {'window_allocated': [1366, 860], 'destinations': {
        'measurement': {'navigated': True, 'page_size': [800, 800],
                        'overflow_count': 2}}}
    verdict, divergences = uxrun.compare_baseline(base_metrics, changed)
    assert verdict == 'diverged'
    assert any('page_size' in d for d in divergences)
    assert any('overflow_count' in d for d in divergences)


def test_fixture_bundle_requires_offscreen_nonclaim(tmp_path: Path) -> None:
    manifest = _assemble(tmp_path, row_id='fixture-selftest',
                       capture_mode='offscreen_fixture')
    assert manifest['capture_mode'] == 'offscreen_fixture'
    assert any('offscreen' in c for c in manifest['non_claims'])


# ---------------------------------------------------------------------------
# Offscreen-capable self-test: the in-process driver produces a real
# sample bundle on this box (the owned-Windows run stays a physical gate).
# ---------------------------------------------------------------------------


def test_offscreen_fixture_selftest_bundle(tmp_path: Path) -> None:
    pytest.importorskip('PySide6')
    data_dir = tmp_path / 'data-seeded'
    data_dir.mkdir()
    driver_out = tmp_path / 'driver'
    result = uxrun.run_driver_phase(
        data_dir, driver_out, 'seeded',
        ('measurement', 'support'),
        ('launch', 'navigate', 'geometry'),
    )
    assert result['clean_exit'] is True
    assert (driver_out / 'driver_result.json').is_file()
    assert result['document_id']
    assert result['environment']['qpa_platform'] == 'offscreen'
    checkpoints = result['checkpoints']
    assert any(c['kind'] == 'launch' for c in checkpoints)
    assert any(c['checkpoint_id'] == 'navigate:measurement'
               for c in checkpoints)

    manifest = _assemble(
        tmp_path,
        row_id='fixture-offscreen-selftest',
        capture_mode='offscreen_fixture',
        document_id=result['document_id'],
        checkpoints=checkpoints,
        metrics=result['metrics'],
        environment={**_env(), **result['environment']},
        driver_results=[result],
        driver_completed=True,
    )
    assert manifest['verdict'] in (
        'evidence_captured', 'capture_incomplete')
    assert manifest['capture_mode'] == 'offscreen_fixture'
    # the sealed fixture record exists and stays honest
    repo = _evidence_repo(tmp_path)
    records = repo.list_bundles_for_row(
        'ux160', 'fixture-offscreen-selftest')
    assert len(records) == 1
    assert records[0].capture_mode == 'offscreen_fixture'
    assert any('offscreen' in c for c in records[0].non_claims)
