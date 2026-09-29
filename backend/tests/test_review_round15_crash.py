"""Round-15 deep review: crash / power-loss safety depth.

Simulates process death and torn writes around each commit point — an
uncommitted transaction killed mid-flight, an ingestion committed before
its inbox staging, an interrupted restore swap, a migration chain cut
between steps, stale locks and leftover temp residue — and asserts the
restart behavior is honest: whole or absent, never half-state.
"""

from contextlib import closing
from hashlib import sha256
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402
import test_cad_schema as schema_tests  # noqa: E402

from htdt import cad_schema
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
    read_native_schema_version,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import (
    CAPTURE_INBOX_RECOVERY_SOURCE,
    CaptureInboxRepository,
)
from htdt.capture_receiver import CaptureReceiverService
from htdt.native_backup import (
    create_backup,
    recover_interrupted_restore,
)
from htdt.runtime_instance import SingleInstanceGuard


SERIES_ID = '40000000-0000-4000-8000-000000000001'
SESSION_ID = support.SESSION_ID
SPACE_ID = support.SPACE_ID
ANCHOR_ID = '40000000-0000-4000-8000-000000000005'
IDENTITY_TRANSFORM = (
    1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
)


def _files() -> dict:
    return support.mesh_specs_files(
        ((ANCHOR_ID, f'mesh/geometry/{ANCHOR_ID}.meshbin', 3, 1),),
        anchor_transform=IDENTITY_TRANSFORM,
        session_id=SESSION_ID,
        space_id=SPACE_ID,
    )


def _plan_and_payloads(
    *, revision_id: str | None = None
) -> tuple[dict, dict[str, bytes]]:
    plan, payloads, _manifest = support.plan_and_payloads(
        Path(tempfile.mkdtemp()),
        files=_files(),
        manifest_overrides={
            'capture_series_id': SERIES_ID,
            'capture_revision_id': revision_id or str(uuid.uuid4()),
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
            'created_at': '2026-09-20T00:00:00Z',
            'finalized_at': '2026-09-20T00:00:00Z',
        },
    )
    return plan, payloads


def _real_archive(tmp_path: Path, *, revision_id: str | None = None) -> bytes:
    bundle_dir, _manifest = support.write_bundle(
        tmp_path / f'bundle-{uuid.uuid4().hex[:8]}',
        _files(),
        manifest_overrides={
            'capture_series_id': SERIES_ID,
            'capture_revision_id': revision_id or str(uuid.uuid4()),
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
            'created_at': '2026-09-20T00:00:00Z',
            'finalized_at': '2026-09-20T00:00:00Z',
        },
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return buffer.getvalue()


def _inbox_rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return scene, ingestion, inbox


class _Reader:
    def __init__(self) -> None:
        self.plans: dict[str, tuple[dict, dict]] = {}

    def register(self, archive: bytes, plan: dict, payloads: dict) -> None:
        self.plans[sha256(archive).hexdigest()] = (plan, payloads)

    def __call__(self, body: bytes):
        key = sha256(body).hexdigest()
        if key not in self.plans:
            raise ValueError('unreadable archive')
        return self.plans[key]


def _stale(path: Path) -> None:
    """Age a residue path past the sweep's age floor."""
    os.utime(path, (0, 0))


# ---------------------------------------------------------------------
# Crash between ingest commit and inbox staging leaves an invisible
# orphan — reconcile_orphaned_ingestions() surfaces it honestly.
# ---------------------------------------------------------------------


class TestOrphanedIngestion:
    def test_unstaged_ingestion_is_reconciled_into_inbox(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        plan, payloads = _plan_and_payloads()
        ingestion.ingest(plan, payloads)
        # the ingest commit landed; the process died before stage()
        assert inbox.list_items() == ()

        recovered = inbox.reconcile_orphaned_ingestions()

        assert len(recovered) == 1
        item = inbox.list_items()[0]
        assert item.lineage_digest == recovered[0].lineage_digest
        assert item.arrival_source == CAPTURE_INBOX_RECOVERY_SOURCE
        assert item.source_detail

    def test_reconcile_is_idempotent_and_leaves_staged_items_untouched(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        plan, payloads = _plan_and_payloads()
        ingestion.ingest(plan, payloads)
        staged = inbox.stage(
            CaptureIngestionPlan.model_validate(plan),
            arrival_source='paired_receiver',
        )
        assert staged.created

        assert inbox.reconcile_orphaned_ingestions() == ()
        items = inbox.list_items()
        assert len(items) == 1
        assert items[0].arrival_source == 'paired_receiver'
        assert items[0].first_arrived_at_utc == staged.item.first_arrived_at_utc

    def test_receiver_delivery_reconciles_a_prior_crash_orphan(
        self, tmp_path: Path
    ) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        ingestion = CaptureIngestionRepository(scene)
        inbox = CaptureInboxRepository(scene, ingestion)
        reader = _Reader()
        service = CaptureReceiverService(
            scene, inbox, ingestion,
            bundle_reader=reader,
            data_dir=tmp_path / 'receiver',
        )
        pairing, _payload = service.begin_pairing(project_ref='doc-1')
        service.confirm_pairing(pairing.pairing_id)

        # earlier run: ingest committed, process died before stage
        orphan_plan, orphan_payloads = _plan_and_payloads()
        ingestion.ingest(orphan_plan, orphan_payloads)
        orphan_digest = CaptureIngestionPlan.model_validate(
            orphan_plan
        ).lineage_digest
        assert inbox.list_items() == ()

        # the next delivery heals the orphan on its way through
        archive = _real_archive(tmp_path)
        plan, payloads = _plan_and_payloads()
        reader.register(archive, plan, payloads)
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            {
                'Content-Type': 'application/vnd.htdt.capture-bundle',
                'X-HTDT-Artifact-Kind': 'capture_bundle',
                'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
                'X-HTDT-Archive-Bytes': str(len(archive)),
            },
            archive,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'

        digests = {item.lineage_digest for item in inbox.list_items()}
        assert digests == {
            orphan_digest,
            CaptureIngestionPlan.model_validate(plan).lineage_digest,
        }


# ---------------------------------------------------------------------
# Crash residue: staging dirs and atomic-writer temp files must not
# accumulate silently or linger as unreadable junk.
# ---------------------------------------------------------------------


class TestCrashResidue:
    def _seeded_data_dir(self, tmp_path: Path) -> Path:
        data_dir = tmp_path / 'data'
        scene = SceneRepository(data_dir / 'cad-scenes.sqlite3')
        scene.save(
            SceneDocument(
                document_id='doc-1',
                schema_version=4,
                room=None,
                entities=(),
            ),
            parent_revision_id=None,
        )
        return data_dir

    def test_open_sweeps_stale_restore_stage_and_tmp_residue(
        self, tmp_path: Path
    ) -> None:
        data_dir = self._seeded_data_dir(tmp_path)
        parent = tmp_path
        stage_dir = parent / 'htdt-restore-stage-deadbeef'
        (stage_dir / 'snapshot').mkdir(parents=True)
        (stage_dir / 'snapshot' / 'junk.bin').write_bytes(b'partial')
        _stale(stage_dir)
        tmp_file = data_dir / '.htdt-journal.json.1234.tmp'
        tmp_file.write_text('{"half"')
        _stale(tmp_file)
        fresh_tmp = data_dir / '.htdt-journal.json.9999.tmp'
        fresh_tmp.write_text('{"live write in progress"')

        recover_interrupted_restore(data_dir)

        assert not stage_dir.exists()
        assert not tmp_file.exists()
        # a temp file younger than the age floor is an in-flight write,
        # not crash residue — it must survive the sweep
        assert fresh_tmp.is_file()
        assert fresh_tmp.read_text() == '{"live write in progress"'
        assert read_native_schema_version(
            data_dir / 'cad-scenes.sqlite3'
        ) == NATIVE_SCHEMA_VERSION

    def test_open_sweeps_stale_incoming_intent_temp(
        self, tmp_path: Path
    ) -> None:
        data_dir = self._seeded_data_dir(tmp_path)
        incoming = data_dir / 'launch-intents' / 'incoming'
        incoming.mkdir(parents=True)
        leftover = incoming / '.999-abc.42.tmp'
        leftover.write_text('{"partial"')
        _stale(leftover)

        recover_interrupted_restore(data_dir)

        assert not leftover.exists()

    def test_backup_staging_dir_swept_on_next_backup(
        self, tmp_path: Path
    ) -> None:
        data_dir = self._seeded_data_dir(tmp_path)
        backups = tmp_path / 'backups'
        leftover = backups / 'htdt-backup-deadbeef'
        (leftover / 'snapshot').mkdir(parents=True)
        (leftover / 'snapshot' / 'cad-scenes.sqlite3').write_bytes(b'torn')
        _stale(leftover)

        manifest = create_backup(data_dir, backups / 'b.htdt-backup')

        assert manifest.manifest_sha256
        assert not leftover.exists()
        assert (backups / 'b.htdt-backup').is_file()


# ---------------------------------------------------------------------
# Commit-boundary honesty: killed transactions roll back whole,
# migrations resume from the last committed boundary, stale locks free.
# ---------------------------------------------------------------------


def _child_env() -> dict:
    src = str(Path(__file__).resolve().parents[1] / 'src')
    env = dict(os.environ)
    env['PYTHONPATH'] = src + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


class TestCrashBoundaries:
    def test_killed_mid_transaction_leaves_no_partial_rows(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / 'mid-txn.sqlite3'
        script = (
            "import sys, time\n"
            "from htdt.cad_schema import connect_sqlite\n"
            f"c = connect_sqlite({str(db)!r})\n"
            "c.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY)')\n"
            "c.commit()\n"
            "c.execute('BEGIN IMMEDIATE')\n"
            "c.execute('INSERT INTO probe VALUES (1)')\n"
            "c.execute('INSERT INTO probe VALUES (2)')\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, '-c', script],
            stdout=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        try:
            assert proc.stdout.readline().strip() == 'ready'
        finally:
            proc.kill()
            proc.wait(timeout=15)

        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute(
                'SELECT COUNT(*) FROM probe'
            ).fetchone()[0] == 0
            assert connection.execute(
                'PRAGMA integrity_check'
            ).fetchone()[0] == 'ok'

    def test_interrupted_migration_resumes_from_committed_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / 'migrating.sqlite3'
        schema_tests._create_database(path, *schema_tests._LEGACY_DDL)
        with sqlite3.connect(path) as connection, connection:
            for statement in schema_tests._SCHEMA_AUTHORITY_DDL:
                connection.execute(statement)
            connection.execute(
                'INSERT INTO native_schema_metadata VALUES (1, 1)'
            )
            connection.execute(
                'INSERT INTO native_schema_migrations VALUES (1, ?, ?)',
                ('2026-09-18T00:00:00+00:00', 'adopted as baseline v1'),
            )

        crashed_step = 7

        def die(connection) -> None:
            raise RuntimeError('simulated process death inside step 7')

        monkeypatch.setitem(cad_schema._MIGRATIONS, crashed_step, die)
        with pytest.raises(RuntimeError):
            ensure_native_schema(path)
        monkeypatch.undo()

        # the marker is written last inside each step, and convergence
        # helpers commit at internal boundaries — the stored version sits
        # at a committed boundary, never inside a half-applied step
        stored = read_native_schema_version(path)
        assert 1 <= stored < crashed_step

        assert ensure_native_schema(path) == NATIVE_SCHEMA_VERSION
        assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
        with closing(sqlite3.connect(path)) as connection:
            versions = [
                row[0]
                for row in connection.execute(
                    'SELECT schema_version FROM native_schema_migrations '
                    'ORDER BY schema_version'
                )
            ]
            assert len(versions) == len(set(versions))
            assert versions[-1] == NATIVE_SCHEMA_VERSION
            assert connection.execute(
                'PRAGMA integrity_check'
            ).fetchone()[0] == 'ok'

    def test_dead_holder_instance_lock_does_not_deadlock(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / 'instance'
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from htdt.runtime_instance import SingleInstanceGuard\n"
            f"guard = SingleInstanceGuard(Path({str(root)!r}))\n"
            "assert guard.acquire()\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, '-c', script],
            stdout=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        try:
            assert proc.stdout.readline().strip() == 'held'
        finally:
            proc.kill()
            proc.wait(timeout=15)

        # the OS byte-range lock died with the holder — no stale-lock
        # deadloop, and no manual cleanup was needed
        assert SingleInstanceGuard(root).acquire()

    def test_torn_archive_is_rejected_not_staged(self, tmp_path: Path) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        ingestion = CaptureIngestionRepository(scene)
        inbox = CaptureInboxRepository(scene, ingestion)
        reader = _Reader()
        service = CaptureReceiverService(
            scene, inbox, ingestion,
            bundle_reader=reader,
            data_dir=tmp_path / 'receiver',
        )
        pairing, _payload = service.begin_pairing(project_ref='doc-1')
        service.confirm_pairing(pairing.pairing_id)

        archive = _real_archive(tmp_path)
        plan, payloads = _plan_and_payloads()
        reader.register(archive, plan, payloads)
        torn = archive[: len(archive) // 2]
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            {
                'Content-Type': 'application/vnd.htdt.capture-bundle',
                'X-HTDT-Artifact-Kind': 'capture_bundle',
                'X-HTDT-Archive-SHA256': sha256(torn).hexdigest(),
                'X-HTDT-Archive-Bytes': str(len(torn)),
            },
            torn,
        )

        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        assert inbox.list_items() == ()
