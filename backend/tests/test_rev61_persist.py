"""REV61-PERSIST regression tests.

Covers the persistence/recovery defects fixed in REV61:

* ``data_relocation._verify_staged_root`` verified only two of the eight
  ``_ASSET_TABLES`` managed-byte specs — a staged root whose other
  registry/digest-only rows dangled passed verification and got
  promoted. The check now iterates the shared registry.
* ``cad_device_snapshot_repository``, ``cad_project_template_repository``
  and ``cad_project_activity_repository`` inserted sealed records without
  re-verifying the payload digest — a ``model_copy`` forge landed a row
  every read path then rejected.
* ``cad_acceptance_repository._insert_evidence`` used bare
  ``INSERT OR IGNORE``, masking a same-id evidence row asserting
  different bytes and accepting malformed digest/path/size claims into
  the managed-byte registry.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from htdt.acceptance_gates import get_gate
from htdt.cad_acceptance import (
    AcceptanceEvidenceRef,
    build_acceptance_run,
)
from htdt.cad_acceptance_repository import AcceptanceRunRepository
from htdt.cad_device_backup import build_backup_artifact
from htdt.cad_device_snapshot import (
    ObservedDeviceField,
    build_device_snapshot,
)
from htdt.cad_device_snapshot_repository import (
    CadDeviceSnapshotRepository,
    DeviceSnapshotIntegrityError,
)
from htdt.cad_project_activity import build_activity_note
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_project_template import build_project_template
from htdt.cad_project_template_repository import (
    CadProjectTemplateRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_schema import NativeSchemaError
from htdt.data_relocation import DataRelocationError, _verify_staged_root
from htdt.managed_assets import MANAGED_ASSETS_DIRNAME
from htdt.native_backup import DATABASE_NAME


SHA_A = hashlib.sha256(b'asset-a').hexdigest()
SHA_B = hashlib.sha256(b'asset-b').hexdigest()


def _seed_staged(staged: Path) -> Path:
    staged.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(staged / DATABASE_NAME)
    repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    return staged / DATABASE_NAME


def _scene_repo(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    return repository


def _snapshot(**overrides):
    kwargs = dict(
        document_id='doc-1',
        instance_id='avr-1',
        evidence_class='device_readback',
        transition_kind='initial_configuration',
        manufacturer='Trinnov',
        model='Altitude 16',
        firmware_version='4.3.0',
        serial_number='SN-1',
        fields=(
            ObservedDeviceField(field='preset_slot', state='observed', value='A'),
        ),
    )
    kwargs.update(overrides)
    return build_device_snapshot(**kwargs)


# ----------------------------------------------------------------------
# _verify_staged_root — full _ASSET_TABLES coverage
# ----------------------------------------------------------------------


def test_staged_root_rejects_missing_registry_asset(tmp_path: Path) -> None:
    """An htdt_acceptance_evidence row claiming absent bytes must fail."""
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO htdt_acceptance_evidence("
            "evidence_id, run_id, step_id, kind, filename, sha256, "
            "relative_path, size_bytes, recorded_at_utc"
            ") VALUES('ev-1','r','s','note','f',?,?,1,'t')",
            (SHA_A, f'{MANAGED_ASSETS_DIRNAME}/{SHA_A}'),
        )
    with pytest.raises(DataRelocationError, match='staged asset missing'):
        _verify_staged_root(staged)


def test_staged_root_rejects_missing_digest_only_asset(
    tmp_path: Path,
) -> None:
    """Digest-only rows resolve to measurement-assets/<sha> — a row whose
    content address holds no bytes must fail verification. The parent
    registry row points at a real file elsewhere so only the digest-only
    leg can fail."""
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    elsewhere = staged / 'imports'
    elsewhere.mkdir()
    (elsewhere / 'source.bin').write_bytes(b'asset-a')
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cad_measurement_assets("
            "sha256, filename, relative_path, size_bytes"
            ") VALUES(?,'f','imports/source.bin',7)",
            (SHA_A,),
        )
        connection.execute(
            "INSERT INTO cad_directivity_source_assets("
            "source_asset_sha256, filename, media_type, source_format, "
            "declared_schema, recorded_at_utc"
            ") VALUES(?,'f','manual','fmt','schema','t')",
            (SHA_A,),
        )
    with pytest.raises(DataRelocationError, match='staged asset missing'):
        _verify_staged_root(staged)


def test_staged_root_rejects_malformed_digest(tmp_path: Path) -> None:
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cad_measurement_assets("
            "sha256, filename, relative_path, size_bytes"
            ") VALUES('not-hex','f','imports/x',1)",
        )
    with pytest.raises(DataRelocationError, match='malformed'):
        _verify_staged_root(staged)


def test_staged_root_rejects_unsafe_relative_path(tmp_path: Path) -> None:
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cad_measurement_assets("
            "sha256, filename, relative_path, size_bytes"
            ") VALUES(?,'f',?,1)",
            (SHA_A, '../../etc/passwd'),
        )
    with pytest.raises(DataRelocationError):
        _verify_staged_root(staged)


def test_staged_root_rejects_size_mismatch(tmp_path: Path) -> None:
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    assets = staged / MANAGED_ASSETS_DIRNAME
    assets.mkdir()
    (assets / SHA_A).write_bytes(b'asset-a')
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cad_measurement_assets("
            "sha256, filename, relative_path, size_bytes"
            ") VALUES(?,'f',?,999)",
            (SHA_A, f'{MANAGED_ASSETS_DIRNAME}/{SHA_A}'),
        )
    with pytest.raises(DataRelocationError, match='size mismatch'):
        _verify_staged_root(staged)


def test_staged_root_accepts_honest_asset_coverage(tmp_path: Path) -> None:
    """Every registry spec covered: path-declaring rows, digest-only rows
    and both ``where`` predicate forms (skipped when they don't match)."""
    staged = tmp_path / 'staged'
    database = _seed_staged(staged)
    assets = staged / MANAGED_ASSETS_DIRNAME
    assets.mkdir()
    (assets / SHA_A).write_bytes(b'asset-a')
    (assets / SHA_B).write_bytes(b'asset-b')
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cad_measurement_assets("
            "sha256, filename, relative_path, size_bytes"
            ") VALUES(?,'f',?,7)",
            (SHA_A, f'{MANAGED_ASSETS_DIRNAME}/{SHA_A}'),
        )
        connection.execute(
            "INSERT INTO htdt_acceptance_evidence("
            "evidence_id, run_id, step_id, kind, filename, sha256, "
            "relative_path, size_bytes, recorded_at_utc"
            ") VALUES('ev-1','r','s','note','f',?,?,7,'t')",
            (SHA_B, f'{MANAGED_ASSETS_DIRNAME}/{SHA_B}'),
        )
        # Managed-source-asset authority rows bind bytes too.
        connection.execute(
            "INSERT INTO cad_equipment_evidence_authorities("
            "evidence_id, evidence_sha256, equipment_definition_sha256, "
            "provenance_sha256, source_sha256, authority_kind, "
            "subject_sha256, field_groups_json, payload_json, "
            "recorded_at_utc"
            ") VALUES('eq-1','e','d','p',?,'managed_source_asset','s',"
            "'[]','{}','t')",
            (SHA_A,),
        )
        # Same table, different authority_kind: not an asset claim — the
        # where predicate must skip it (source_sha256 here is dangling).
        connection.execute(
            "INSERT INTO cad_equipment_evidence_authorities("
            "evidence_id, evidence_sha256, equipment_definition_sha256, "
            "provenance_sha256, source_sha256, authority_kind, "
            "subject_sha256, field_groups_json, payload_json, "
            "recorded_at_utc"
            ") VALUES('eq-2','e2','d2','p2',?,'upstream_catalog','s',"
            "'[]','{}','t')",
            ('9' * 64,),
        )
        # NULL source_sha256 on the IS NOT NULL table is skipped.
        connection.execute(
            "INSERT INTO cad_treatment_evidence_authorities("
            "evidence_id, evidence_sha256, source_kind, source_id, "
            "source_version, source_sha256, subject_sha256, payload_json"
            ") VALUES('tr-1','e','k','i','v',NULL,'s','{}')",
        )
    _verify_staged_root(staged)


# ----------------------------------------------------------------------
# Sealed-record write re-verification
# ----------------------------------------------------------------------


def test_device_snapshot_save_rejects_forged_seal(tmp_path: Path) -> None:
    repository = CadDeviceSnapshotRepository(_scene_repo(tmp_path))
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    forged = snapshot.model_copy(
        update={
            'snapshot_id': 'devsnap-' + 'e' * 24,
            'snapshot_sha256': 'e' * 64,
        }
    )
    with pytest.raises(DeviceSnapshotIntegrityError, match='sealed sha256'):
        repository.save_snapshot(forged)


def test_device_snapshot_save_rejects_forged_id(tmp_path: Path) -> None:
    """Real sha + an id that isn't its derived address is equally forged."""
    repository = CadDeviceSnapshotRepository(_scene_repo(tmp_path))
    snapshot = _snapshot()
    forged = snapshot.model_copy(
        update={'snapshot_id': 'devsnap-' + 'f' * 24}
    )
    with pytest.raises(DeviceSnapshotIntegrityError, match='sealed sha256'):
        repository.save_snapshot(forged)


def test_device_baseline_save_rejects_forged_seal(tmp_path: Path) -> None:
    from htdt.cad_device_snapshot import build_known_good_baseline

    repository = CadDeviceSnapshotRepository(_scene_repo(tmp_path))
    snapshot = _snapshot()
    repository.save_snapshot(snapshot)
    baseline = build_known_good_baseline(
        snapshot=snapshot,
        promoted_at_utc='2026-10-06T00:00:00Z',
    )
    repository.save_baseline(baseline)
    forged = baseline.model_copy(
        update={
            'baseline_id': 'devkg-' + 'e' * 24,
            'baseline_sha256': 'e' * 64,
        }
    )
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_baseline(forged)


def test_device_artifact_save_rejects_forged_sha(tmp_path: Path) -> None:
    repository = CadDeviceSnapshotRepository(_scene_repo(tmp_path))
    artifact = build_backup_artifact(
        artifact_id='art-1',
        device_equipment_id='avr-1',
        content_sha256=SHA_A,
        captured_at_utc='2026-10-06T00:00:00Z',
    )
    repository.save_backup_artifact(artifact)
    forged = artifact.model_copy(update={'artifact_sha256': 'e' * 64})
    with pytest.raises(DeviceSnapshotIntegrityError):
        repository.save_backup_artifact(forged)


def test_project_template_save_rejects_forged_sha(tmp_path: Path) -> None:
    repository = CadProjectTemplateRepository(_scene_repo(tmp_path))
    template = build_project_template(
        template_id='t-1', version='1', name='five-one', kind='user'
    )
    repository.save_template(template)
    repository.save_template(template)  # identical re-save is a no-op
    forged = template.model_copy(update={'template_sha256': 'e' * 64})
    with pytest.raises(ValueError, match='sealed sha256'):
        repository.save_template(forged)


def test_activity_note_save_rejects_forged_sha(tmp_path: Path) -> None:
    scenes = _scene_repo(tmp_path)
    notes = CadProjectActivityNoteRepository(scenes)
    note = build_activity_note(
        document_id='doc-1',
        title='milestone',
        created_at_utc='2026-10-06T00:00:00Z',
    )
    notes.save_note(note)
    forged = note.model_copy(update={'note_sha256': 'e' * 64})
    with pytest.raises(ValueError, match='sealed sha256'):
        notes.save_note(forged)


# ----------------------------------------------------------------------
# Acceptance evidence — same-id conflict + write-side shape checks
# ----------------------------------------------------------------------


def _evidence_ref(evidence_id: str, sha: str, path: str | None = None):
    return AcceptanceEvidenceRef(
        evidence_id=evidence_id,
        kind='gate_log',
        filename='gate.log',
        sha256=sha,
        relative_path=path or f'{MANAGED_ASSETS_DIRNAME}/{sha}',
        size_bytes=4,
        recorded_at_utc='2026-10-06T00:00:00Z',
    )


def _attach_via_run(repo, run, ref, *, status='passed'):
    steps = [
        step.model_copy(
            update={
                'status': status,
                'verdict_source': 'human_confirm',
                'evidence': (ref,),
            }
        )
        if index == 0
        else step
        for index, step in enumerate(run.steps)
    ]
    return repo.commit(run, steps)


def test_acceptance_evidence_same_id_conflict_rejected(tmp_path: Path):
    """Same evidence id re-asserted with different bytes is a conflict —
    before this fix the OR IGNORE silently kept the first row while the
    run payload claimed the new digest."""
    repo = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-evconf', environment={}
        )
    )
    ref = _evidence_ref('ev-fixed', SHA_A)
    rev2 = _attach_via_run(repo, run, ref)
    forged = _evidence_ref('ev-fixed', SHA_B)
    steps = [
        step.model_copy(update={'evidence': (ref, forged)})
        if index == 0
        else step
        for index, step in enumerate(rev2.steps)
    ]
    with pytest.raises(NativeSchemaError, match='different bytes'):
        repo.commit(rev2, steps)


def test_acceptance_evidence_identical_resave_tolerated(tmp_path: Path):
    """The OR IGNORE path remains legal for the row-identical re-save:
    same evidence id, same run/step binding, same bytes claim."""
    repo = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-evsame', environment={}
        )
    )
    ref = _evidence_ref('ev-fixed', SHA_A)
    rev2 = _attach_via_run(repo, run, ref)
    steps = list(rev2.steps)
    steps[1] = steps[1].model_copy(
        update={
            'status': 'skipped',
            'verdict_source': 'human_confirm',
        }
    )
    repo.commit(rev2, steps)
    assert repo.evidence_for('ac-evsame')


def test_acceptance_evidence_malformed_digest_rejected(tmp_path: Path):
    repo = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-evsha', environment={}
        )
    )
    bad = _evidence_ref('ev-bad', 'zzzz', path='measurement-assets/x')
    with pytest.raises(NativeSchemaError, match='malformed digest'):
        _attach_via_run(repo, run, bad)


def test_acceptance_evidence_unsafe_path_rejected(tmp_path: Path):
    repo = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-evpath', environment={}
        )
    )
    bad = _evidence_ref('ev-bad2', SHA_A, path='../../outside')
    with pytest.raises(NativeSchemaError, match='unsafe path'):
        _attach_via_run(repo, run, bad)


def test_acceptance_evidence_negative_size_rejected(tmp_path: Path):
    repo = AcceptanceRunRepository(tmp_path / 'cad-scenes.sqlite3')
    run = repo.save(
        build_acceptance_run(
            get_gate('golden-path'), run_id='ac-evsize', environment={}
        )
    )
    bad = AcceptanceEvidenceRef(
        evidence_id='ev-bad3',
        kind='gate_log',
        filename='g',
        sha256=SHA_A,
        relative_path=f'{MANAGED_ASSETS_DIRNAME}/{SHA_A}',
        size_bytes=-1,
        recorded_at_utc='2026-10-06T00:00:00Z',
    )
    with pytest.raises(NativeSchemaError, match='negative size'):
        _attach_via_run(repo, run, bad)
