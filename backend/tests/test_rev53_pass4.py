"""REV53-PASS4 — fourth-pass findings on REV52's merged changes.

A. Relocation rollback (REV52-DATA): recovery must return the parked
   generation even when a post-crash EMPTY directory stands at the source
   name — ``os.replace`` cannot move a directory over an existing
   directory on Windows, so the rollback site needs the same
   ``_promote_directory`` treatment the forward path got.

B. Restore rollback (REV52-DATA): ``pre_restore_live`` must gate the
   whole rollback, not just the aux leg. When the journal says nothing
   was live before the swap, the interrupted swap's own restored
   generation — database AND measurement assets — is parked, never
   blessed as recovered pre-restore state.

C. Bundle asset closure (REV52-DATA): export pins registry rows to
   ``measurement-assets/<digest>`` exactly like import does, so a corrupt
   row cannot produce a bundle the importer then refuses.

D. Matrix verification (REV52-GAEMIT): ``coverage_complete`` requires
   the provider's receiver set to be a subset of the verified receivers —
   a matrix that ran only part of the provider's coverage cannot stamp
   complete coverage.

E. Managed-data fingerprint (REV52-DATA): backup-included directory
   components fold their subtree contents (count/bytes/newest mtime), so
   an inner-file edit inside a directory component cannot keep a stale
   fingerprint.
"""

from __future__ import annotations

from contextlib import closing
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts.run_r130a_candidate_wave_execution import (  # noqa: E402
    _fixture as r130_fixture,
)
from test_rev44_surfaces import _registered_provider  # noqa: E402
from test_data_relocation_cutover import (  # noqa: E402
    _journal,
    _seed_data_dir,
    _write_journal,
)
from test_native_backup import (  # noqa: E402
    _SimulatedCrash,
    _inject_swap_crash,
    _seed_data,
    _stage_dirs,
)
from test_project_bundle import _seed_project  # noqa: E402

import htdt.automatic_backup as automatic_backup  # noqa: E402
from htdt.automatic_backup import (  # noqa: E402
    managed_data_fingerprint,
)
from htdt.cad_prediction_matrix import (  # noqa: E402
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixSourceRef,
    build_matrix_run_verification,
    build_prediction_matrix_spec,
    execute_prediction_matrix,
)
from htdt.data_relocation import (  # noqa: E402
    DataRelocationError,
    recover_interrupted_relocation,
)
from htdt.native_backup import (  # noqa: E402
    DATABASE_NAME,
    MEASUREMENT_ASSETS_NAME,
    create_backup,
    recover_interrupted_restore,
    restore_backup,
)
from htdt.persisted_data import (  # noqa: E402
    BackupPolicy,
    PersistedDataComponent,
    PersistedDataLifecycle,
    PortableProjectPolicy,
    RelocationPolicy,
)
from htdt.project_bundle import (  # noqa: E402
    ProjectBundleError,
    export_project_bundle,
)


# ---------------------------------------------------------------------------
# A. relocation rollback: parked generation vs an empty source dir
# ---------------------------------------------------------------------------


def test_recovery_restores_parked_over_empty_source_dir(
    tmp_path: Path,
) -> None:
    """Journal says DESTINATION_PROMOTED; the staged dir is gone, the
    destination never got a database, and a post-crash EMPTY directory
    stands at the source name. ``os.replace(parked, source)`` raises on
    Windows even for an empty destination — the parked generation must be
    promoted over it, not stranded."""
    source = tmp_path / 'source'
    _seed_data_dir(source)
    parked = tmp_path / 'source.relocated-x'
    bootstrap = tmp_path / 'boot.json'
    shutil.move(source, parked)
    source.mkdir()  # crash residue: an empty directory at the source name
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ),
        bootstrap,
    )

    with pytest.raises(DataRelocationError):
        recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert (source / DATABASE_NAME).is_file()
    assert not parked.exists()


# ---------------------------------------------------------------------------
# B. restore rollback: journal-gated empty pre-restore root
# ---------------------------------------------------------------------------


def test_crashed_restore_into_empty_root_does_not_bless_restored_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty pre-restore root + crash after the staged database/assets
    landed + swept stage dir. ``pre_restore_live`` says nothing was live
    before, so the rollback must leave the root EMPTY — the staged
    generation is the payload the restore failed to commit, not recovered
    data, and the journaled-absent assets must not be adopted either."""
    seed_dir = tmp_path / 'seed'
    _seed_data(seed_dir)
    # An aux member in the manifest keeps the swap incomplete after the
    # crash: the staged aux file never landed and the stage dir is gone.
    (seed_dir / 'commissioning-plans.json').write_text(
        json.dumps({'plans': ['x']}), encoding='utf-8'
    )
    baseline = tmp_path / 'baseline.htdt-backup'
    create_backup(seed_dir, baseline)

    data_dir = tmp_path / 'data'  # never seeded: EMPTY pre-restore root
    # On an empty root there are no live evacuations: the staged database
    # is swap move 1 and the staged assets dir is move 2 — crash before
    # the aux install so the swap cannot complete after the stage sweep.
    _inject_swap_crash(monkeypatch, after_move=2)
    with pytest.raises(_SimulatedCrash):
        restore_backup(data_dir, baseline)
    monkeypatch.undo()
    assert _stage_dirs(data_dir) == []

    events = recover_interrupted_restore(data_dir)
    assert any(event.action == 'rolled_back' for event in events)

    assert not (data_dir / DATABASE_NAME).exists()
    assert not (data_dir / MEASUREMENT_ASSETS_NAME).exists()


# ---------------------------------------------------------------------------
# C. bundle asset closure: export pins rows to the content address
# ---------------------------------------------------------------------------


def test_export_rejects_asset_row_pointing_outside_store(
    tmp_path: Path,
) -> None:
    """Import requires ``relative_path == measurement-assets/<digest>``;
    export used to accept any managed path under the data root, shipping
    a bundle the importer refuses. A corrupt row must fail the export."""
    repository, _head, _record, dataset = _seed_project(tmp_path)
    data_dir = tmp_path / 'source'
    digest = dataset.source_sha256
    # The asset's real bytes at a wrong path — size AND hash both pass the
    # write-time checks, so an un-pinned export ships a manifest the
    # importer then refuses: a bundle no import can accept.
    good = data_dir / 'measurement-assets' / digest
    stray = data_dir / 'stray-asset.bin'
    stray.write_bytes(good.read_bytes())

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''UPDATE cad_measurement_assets
               SET relative_path=?
               WHERE sha256=?''',
            ('stray-asset.bin', digest),
        )

    with pytest.raises(ProjectBundleError, match='content address'):
        export_project_bundle(
            repository,
            'doc-a',
            tmp_path / 'out.htdtproject',
        )


# ---------------------------------------------------------------------------
# D. matrix verification: provider receiver coverage subset check
# ---------------------------------------------------------------------------


def test_matrix_coverage_incomplete_when_provider_receivers_unrun(
    tmp_path: Path,
) -> None:
    """A provider that covers receivers the matrix never ran must not get
    ``coverage_complete`` — promotion would then claim every receiver the
    provider covers was verified. The provider here claims one extra
    receiver response (``model_copy`` bypasses identity validators; the
    coverage gate reads only ``receiver_responses``)."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, lane, provider = _registered_provider(fixture)
    revision = fixture['revision']
    snapshot = fixture['snapshot']
    source_entity_id = (
        provider.source_identity.source_binding.source_entity_id
    )
    receiver_id = provider.receiver_responses[0].receiver_id
    receiver_identity = provider.receiver_identities[0]

    spec = build_prediction_matrix_spec(
        document_id=snapshot.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        acoustic_scene_snapshot_id=snapshot.snapshot_id,
        acoustic_scene_snapshot_sha256=snapshot.semantic_sha256,
        solver_implementation_ref=(
            provider.current_authority.solver_implementation_ref
        ),
        valid_frequency_domain=provider.valid_frequency_domain,
        sources=(
            MatrixSourceRef(
                matrix_source_id=f'source:{source_entity_id}',
                source_entity_id=source_entity_id,
                source_binding_sha256=(
                    provider.source_identity.source_binding_sha256
                ),
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id=f'receiver:{receiver_id}',
                receiver_id=receiver_id,
                receiver_entity_id=(
                    receiver_identity.receiver_binding.entity_id
                ),
                receiver_binding_sha256=(
                    receiver_identity.receiver_binding_sha256
                ),
            ),
        ),
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=tuple(
                provider.receiver_responses[0].frequency_hz
            ),
        ),
    )
    run = execute_prediction_matrix(
        spec,
        {f'source:{source_entity_id}': provider},
        repository=lane.matrix_repository,
        started_at_utc='2026-01-01T00:00:00Z',
        finished_at_utc='2026-01-01T00:00:01Z',
    )
    assert run.state == 'READY'
    result_set = next(
        item
        for item in lane.matrix_repository.list_result_sets(spec.spec_id)
        if item.semantic_sha256 == run.result_set_sha256
    )

    # Same provider claiming one additional receiver response.
    extra_response = provider.receiver_responses[0].model_copy(
        update={
            'receiver_id': 'receiver-unrun',
            'receiver_entity_id': 'entity-unrun',
        }
    )
    wide_provider = provider.model_copy(
        update={
            'receiver_responses': (
                provider.receiver_responses + (extra_response,)
            )
        }
    )
    verification = build_matrix_run_verification(
        spec=spec,
        result_set=result_set,
        run=run,
        providers={f'source:{source_entity_id}': wide_provider},
    )
    entry = verification.provider_entry(provider.provider_id)
    assert entry is not None
    assert entry.unverified_cells == ()
    # Every run cell verified — but not every receiver the provider
    # covers, so coverage is NOT complete.
    assert entry.coverage_complete is False


# ---------------------------------------------------------------------------
# E. fingerprint: directory components fold their subtree
# ---------------------------------------------------------------------------


def test_fingerprint_tracks_directory_component_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today every backup-included component is a flat file, but a
    registry entry with ``is_directory=True`` must contribute its
    subtree's contents — an inner-file edit otherwise leaves the
    directory stat unchanged and the fingerprint stale."""
    component = PersistedDataComponent(
        name='aux-store',
        path='aux-store',
        lifecycle=PersistedDataLifecycle.PROJECT_AUXILIARY,
        scope='root',
        backup=BackupPolicy.INCLUDE,
        relocation=RelocationPolicy.CARRY,
        portable=PortableProjectPolicy.NOT_PROJECT_DATA,
        is_directory=True,
    )
    monkeypatch.setattr(
        automatic_backup,
        'backup_included_components',
        lambda: (component,),
    )
    data_dir = tmp_path / 'data'
    store_dir = data_dir / 'aux-store'
    store_dir.mkdir(parents=True)
    member = store_dir / 'a.json'
    member.write_text('{"a": 1}', encoding='utf-8')

    before = managed_data_fingerprint(data_dir)
    member.write_text('{"a": 22}', encoding='utf-8')
    assert managed_data_fingerprint(data_dir) != before

    after = managed_data_fingerprint(data_dir)
    (store_dir / 'b.json').write_text('{"b": 3}', encoding='utf-8')
    assert managed_data_fingerprint(data_dir) != after

    # A bare rename must invalidate too — same bytes, different member set.
    renamed = managed_data_fingerprint(data_dir)
    member.rename(store_dir / 'c.json')
    assert managed_data_fingerprint(data_dir) != renamed
