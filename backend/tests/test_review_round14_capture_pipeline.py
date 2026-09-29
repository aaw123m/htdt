"""Round-14 deep review: capture -> inbox -> measurement pipeline depth.

Drives real payload transitions — real ``.htdtcapture`` archives through
the paired receiver, direct ingest, inbox disposition changes, semantic
promotion, supersession and retention purges — and asserts the record's
identity, payload hash, labels and timestamps stay true at every hop.
"""

from contextlib import closing
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.capture_ingestion_transaction import (
    SUPPORTED_INGESTOR_IDENTITIES,
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import (
    CAPTURE_INBOX_UNASSIGNED_SCOPE,
    CaptureInboxError,
    CaptureInboxRepository,
)
from htdt.capture_receiver import CaptureReceiverService
from htdt.capture_retention import (
    CaptureRetentionError,
    CaptureRetentionService,
)
from htdt.capture_semantic_promotion import (
    CaptureSemanticPromotionError,
    CaptureSemanticPromotionRepository,
    make_capture_semantic_promotion_request,
    make_capture_world_to_scene_authority,
)
from htdt.semantic_geometry import SemanticCoordinateTransform


SERIES_ID = '40000000-0000-4000-8000-000000000001'
REVISION_ID = '40000000-0000-4000-8000-000000000002'
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
    *,
    revision_id: str | None = None,
    series_id: str = SERIES_ID,
    parent_revision_id: str | None = None,
    created_at: str = '2026-09-20T00:00:00Z',
    json_overrides: dict | None = None,
) -> tuple[dict, dict[str, bytes]]:
    plan, payloads, _manifest = support.plan_and_payloads(
        Path(tempfile.mkdtemp()),
        files=_files(),
        manifest_overrides={
            'capture_series_id': series_id,
            'capture_revision_id': revision_id or str(uuid.uuid4()),
            'parent_revision_id': parent_revision_id,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
            'created_at': created_at,
            'finalized_at': created_at,
        },
        json_overrides=json_overrides,
    )
    return plan, payloads


def _real_archive(
    tmp_path: Path,
    *,
    revision_id: str | None = None,
    created_at: str = '2026-09-20T00:00:00Z',
) -> tuple[bytes, bytes]:
    """A real .htdtcapture zip + its canonical manifest bytes."""
    bundle_dir, manifest = support.write_bundle(
        tmp_path / f'bundle-{uuid.uuid4().hex[:8]}',
        _files(),
        manifest_overrides={
            'capture_series_id': SERIES_ID,
            'capture_revision_id': revision_id or str(uuid.uuid4()),
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
            'created_at': created_at,
            'finalized_at': created_at,
        },
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return buffer.getvalue(), manifest


class _Reader:
    """Reader double: maps archive bytes to a prebuilt plan."""

    def __init__(self) -> None:
        self.plans: dict[str, tuple[dict, dict]] = {}

    def register(self, archive: bytes, plan: dict, payloads: dict) -> None:
        self.plans[sha256(archive).hexdigest()] = (plan, payloads)

    def __call__(self, body: bytes):
        key = sha256(body).hexdigest()
        if key not in self.plans:
            raise ValueError('unreadable archive')
        return self.plans[key]


def _receiver_rig(tmp_path: Path, *, project_ref: str | None = 'doc-1'):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    reader = _Reader()
    service = CaptureReceiverService(
        scene,
        inbox,
        ingestion,
        bundle_reader=reader,
        data_dir=tmp_path / 'receiver',
    )
    pairing, _payload = service.begin_pairing(project_ref=project_ref)
    service.confirm_pairing(pairing.pairing_id)
    return scene, ingestion, inbox, reader, service, pairing


def _headers(archive: bytes, **extra) -> dict:
    headers = {
        'Content-Type': 'application/vnd.htdt.capture-bundle',
        'X-HTDT-Artifact-Kind': 'capture_bundle',
        'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
        'X-HTDT-Archive-Bytes': str(len(archive)),
    }
    for key, value in extra.items():
        header = 'X-HTDT-' + key.replace('_', '-').upper()
        if value is None:
            headers.pop(header, None)
        else:
            headers[header] = value
    return headers


def _deliver(service, pairing, reader, archive, plan, payloads, **headers):
    reader.register(archive, plan, payloads)
    return service.handle_delivery(
        pairing.pairing_token, _headers(archive, **headers), archive
    )


def _inbox_rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return scene, ingestion, inbox


def _stage(ingestion, inbox, tmp_path, **kwargs):
    plan, payloads = _plan_and_payloads(**kwargs)
    ingestion.ingest(plan, payloads)
    typed = CaptureIngestionPlan.model_validate(plan)
    inbox.stage(typed, arrival_source='paired_receiver')
    return typed


def _scene_doc(scene: SceneRepository) -> str:
    return scene.save(
        SceneDocument(
            document_id='capture-doc',
            schema_version=4,
            room=None,
            entities=(),
        ),
        parent_revision_id=None,
    ).revision.revision_id


def _promotion_repo(scene, capture):
    return CaptureSemanticPromotionRepository(scene, capture)


def _world_authority(capture: CaptureIngestionRepository, bundle_digest: str):
    authority = capture.coordinate_authority_for(bundle_digest, SPACE_ID)
    assert authority is not None
    transform = SemanticCoordinateTransform(
        matrix_source_to_scene_m=(
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        provenance='explicit_user_authority',
        reason='fixture world-to-scene alignment',
    )
    return make_capture_world_to_scene_authority(
        coordinate_authority=authority, transform=transform
    )


# ---------------------------------------------------------------------
# End-to-end identity: real archive bytes through the real reader
# ---------------------------------------------------------------------


class TestEndToEndIdentity:
    def test_real_archive_delivery_preserves_identity_at_every_hop(
        self, tmp_path: Path
    ) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        ingestion = CaptureIngestionRepository(scene)
        inbox = CaptureInboxRepository(scene, ingestion)
        service = CaptureReceiverService(
            scene, inbox, ingestion, data_dir=tmp_path / 'receiver'
        )
        pairing, _payload = service.begin_pairing(project_ref='doc-9')
        service.confirm_pairing(pairing.pairing_id)

        archive, manifest = _real_archive(tmp_path)
        bundle_digest = sha256(manifest).hexdigest()
        manifest_doc = json.loads(manifest)
        revision_id = manifest_doc['capture_revision_id']

        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                capture_revision_id=revision_id,
                bundle_digest=bundle_digest,
            ),
            archive,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'
        assert receipt['capture_revision_id'] == revision_id
        assert receipt['bundle_digest'] == bundle_digest

        # Hop 1 -> ingestion run: revision/digest/lineage all true.
        runs = ingestion.list_ingestion_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.bundle_digest == bundle_digest
        assert run.capture_revision_id == revision_id
        assert run.recorded_at_utc  # stamped at ingest, not at capture

        # Hop 2 -> inbox item: same lineage/digest, honest labels.
        item = inbox.get(run.lineage_digest)
        assert item is not None
        assert item.bundle_digest == bundle_digest
        assert item.capture_revision_id == revision_id
        assert item.arrival_source == 'paired_receiver'
        assert item.scope == 'doc-9'
        assert item.disposition == 'pending'
        assert item.arrival_count == 1
        # Arrival time is now-ish, never the producer's created_at.
        assert item.first_arrived_at_utc != manifest_doc['created_at']

        # Hop 3 -> source evidence rows: sha and size match real bytes.
        plan = ingestion.get_ingestion(run.lineage_digest)
        assert plan is not None
        for record in plan.source_evidence:
            evidence = ingestion.get_source_evidence(
                record.source_evidence_id
            )
            assert evidence is not None
            # The retained bytes themselves hash to the declared sha.
            assert sha256(evidence.payload).hexdigest() == (
                record.payload_sha256
            )
            assert evidence.record.payload_sha256 == (
                record.payload_sha256
            )
            assert evidence.record.bytes == record.bytes
            declared = next(
                entry
                for entry in manifest_doc['files']
                if entry['path'] == record.path
            )
            assert declared['sha256'] == record.payload_sha256
            assert declared['bytes'] == record.bytes
            assert evidence.record.media_type == declared['media_type']
            assert (
                evidence.record.provenance_class
                == declared['provenance_class']
            )

        # The canonical manifest — the document that authorized every
        # row above — must be retained for wire deliveries too.
        bundle = ingestion.get_capture_bundle(bundle_digest)
        assert bundle is not None
        assert sha256(bundle.manifest_bytes).hexdigest() == bundle_digest
        assert bundle.manifest_bytes == manifest

    def test_second_pairing_redelivery_dedupes_and_counts(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox, reader, service, pairing = _receiver_rig(
            tmp_path
        )
        plan, payloads = _plan_and_payloads()
        archive = b'archive-dup'

        first = _deliver(
            service, pairing, reader, archive, plan, payloads
        )
        assert first[0] == 200
        # Same delivery id replayed: deduped at the delivery ledger.
        second = _deliver(
            service, pairing, reader, archive, plan, payloads
        )
        assert second[1]['ingestion_outcome'] == 'already_staged'

        typed = CaptureIngestionPlan.model_validate(plan)
        item = inbox.get(typed.lineage_digest)
        assert item.arrival_count == 1
        assert len(inbox.list_items()) == 1
        assert len(ingestion.list_ingestion_runs()) == 1

        # A different pairing delivering the same bytes bumps arrival
        # count honestly (arrival_source stays the first arrival's).
        pairing2, _p2 = service.begin_pairing(project_ref='doc-2')
        service.confirm_pairing(pairing2.pairing_id)
        third = _deliver(
            service, pairing2, reader, archive, plan, payloads
        )
        assert third[1]['ingestion_outcome'] == 'already_staged'
        item = inbox.get(typed.lineage_digest)
        assert item.arrival_count == 2
        assert item.scope == 'doc-1'  # first arrival's routing kept
        assert len(inbox.list_items()) == 1
        assert len(ingestion.list_ingestion_runs()) == 1

    def test_revision_conflict_receipt_tells_the_truth(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox, reader, service, pairing = _receiver_rig(
            tmp_path
        )
        revision = str(uuid.uuid4())
        plan1, payloads1 = _plan_and_payloads(revision_id=revision)
        plan2, payloads2 = _plan_and_payloads(
            revision_id=revision, created_at='2026-09-20T00:00:02Z'
        )
        digest2 = CaptureIngestionPlan.model_validate(
            plan2
        ).bundle.bundle_digest

        ok = _deliver(service, pairing, reader, b'arc-1', plan1, payloads1)
        assert ok[0] == 200

        status, receipt = _deliver(
            service,
            pairing,
            reader,
            b'arc-2',
            plan2,
            payloads2,
            capture_revision_id=revision,
            bundle_digest=digest2,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        # The receipt must not claim staging that never happened.
        assert 'inbox' not in (receipt['detail'] or '')

        # The conflicting bundle is nowhere in the inbox and the
        # rejection is still auditable on the delivery ledger.
        assert len(inbox.list_items()) == 1
        with closing(
            sqlite3.connect(tmp_path / 'cad.sqlite3')
        ) as connection:
            rejected = connection.execute(
                "SELECT outcome, detail FROM capture_receiver_deliveries "
                "WHERE bundle_digest=?",
                (digest2,),
            ).fetchone()
        assert rejected is not None
        assert rejected[0] == 'rejected'


# ---------------------------------------------------------------------
# Disposition coherence: stranded / contradictory states must be
# impossible at repository level, not just at the button level
# ---------------------------------------------------------------------


class TestDispositionCoherence:
    def test_promoted_item_cannot_be_rejected_or_deferred(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        inbox.promote(
            typed.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='v1',
            executor=lambda p, k: f'auth-{k}',
        )
        assert inbox.get(typed.lineage_digest).disposition == 'promoted'
        with pytest.raises(CaptureInboxError, match='reject'):
            inbox.reject(typed.lineage_digest, 'changed my mind')
        with pytest.raises(CaptureInboxError, match='defer'):
            inbox.defer(typed.lineage_digest, 'later')
        # Nothing silently re-disposed the live promotions.
        assert inbox.get(typed.lineage_digest).disposition == 'promoted'

    def test_deferred_item_cannot_record_promotion(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        inbox.defer(typed.lineage_digest, 'waiting on rescan')
        with pytest.raises(CaptureInboxError, match='deferred'):
            inbox.record_promotion(
                typed.lineage_digest, 'raw_visual_evidence', 'auth:x'
            )
        with pytest.raises(CaptureInboxError, match='deferred'):
            inbox.record_blocked(
                typed.lineage_digest, 'raw_visual_evidence', 'trying'
            )
        # Resume restores honest pending state.
        resumed = inbox.resume(typed.lineage_digest)
        assert resumed.disposition == 'pending'
        assert resumed.disposition_at_utc is None
        inbox.record_promotion(
            typed.lineage_digest, 'raw_visual_evidence', 'auth:x'
        )

    def test_superseded_item_cannot_be_resurrected_by_disposition_ops(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        a = _stage(ingestion, inbox, tmp_path, revision_id=rev_a)
        b = _stage(
            ingestion,
            inbox,
            tmp_path,
            revision_id=rev_b,
            parent_revision_id=rev_a,
        )
        inbox.promote(
            a.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='v1',
            executor=lambda p, k: f'auth-a-{k}',
        )
        for kind in ('raw_visual_evidence', 'semantic_geometry'):
            inbox.supersede(
                a.lineage_digest, b.lineage_digest, kind,
                reason='rescan replaces it',
            )
        assert inbox.get(a.lineage_digest).disposition == 'superseded'
        with pytest.raises(CaptureInboxError, match='reject'):
            inbox.reject(a.lineage_digest, 'un-supersede')
        with pytest.raises(CaptureInboxError, match='defer'):
            inbox.defer(a.lineage_digest, 'un-supersede')
        with pytest.raises(CaptureInboxError, match='resume'):
            inbox.resume(a.lineage_digest)
        assert inbox.get(a.lineage_digest).disposition == 'superseded'
        # The supersession records are untouched.
        inspection = inbox.inspect(a.lineage_digest)
        assert len(inspection.supersessions_of_this) == 2

    def test_superseded_kind_cannot_be_recorded_promoted(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        a = _stage(ingestion, inbox, tmp_path, revision_id=rev_a)
        b = _stage(
            ingestion,
            inbox,
            tmp_path,
            revision_id=rev_b,
            parent_revision_id=rev_a,
        )
        inbox.promote(
            a.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='v1',
            executor=lambda p, k: f'auth-a-{k}',
        )
        inbox.supersede(
            a.lineage_digest, b.lineage_digest,
            'semantic_geometry', reason='rescan replaces geometry',
        )
        # The superseded kind must not silently regain a live promotion.
        with pytest.raises(CaptureInboxError, match='superseded'):
            inbox.record_promotion(
                a.lineage_digest, 'semantic_geometry', 'auth-a-late'
            )
        # The still-current kind keeps recording normally.
        inbox.record_promotion(
            a.lineage_digest, 'raw_visual_evidence', 'auth-a-rve-2'
        )


# ---------------------------------------------------------------------
# Supersession truth downstream: scene geometry stays honestly current
# ---------------------------------------------------------------------


class TestSupersessionTruth:
    def test_inbox_supersession_does_not_mutate_scene_geometry(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _inbox_rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        _scene_doc(scene)
        promotion = _promotion_repo(scene, ingestion)
        run = ingestion.get_ingestion_run(
            ingestion.list_ingestion_runs()[0].ingestion_run_id
        )
        binding_id = ingestion.mesh_binding_ids_for_run(
            run.ingestion_run_id
        )[0]
        source = scene.latest('capture-doc')
        request = make_capture_semantic_promotion_request(
            ingestion_run_id=run.ingestion_run_id,
            raw_mesh_binding_id=binding_id,
            target_document_id='capture-doc',
            source_scene_revision_id=source.revision_id,
            world_to_scene_authority=_world_authority(
                ingestion, run.bundle_digest
            ),
            readiness_policy='allow_blocked_semantic_authority',
            reason='retain captured mesh',
        )
        promoted = promotion.promote(request)
        assert promoted.promotion_created
        geometry_before = scene.get(
            promoted.scene_revision_id
        ).document.r120_semantic_geometry
        assert geometry_before is not None

        inbox.record_promotion(
            typed.lineage_digest, 'semantic_geometry', promoted.promotion_id
        )
        rev_b = str(uuid.uuid4())
        b = _stage(
            ingestion, inbox, tmp_path,
            revision_id=rev_b,
            parent_revision_id=typed.bundle.capture_revision_id,
        )
        inbox.supersede(
            typed.lineage_digest, b.lineage_digest,
            'semantic_geometry', reason='rescan replaces geometry',
        )

        # Nothing downstream was silently rewritten: the promoted
        # geometry is still the document's honest current authority —
        # replacing it requires an explicit replace_exact promotion.
        head = scene.latest('capture-doc')
        assert head.document.r120_semantic_geometry is not None
        assert (
            head.document.r120_semantic_geometry.geometry_id
            == geometry_before.geometry_id
        )
        record = promotion.get_promotion(promoted.promotion_id)
        assert record is not None
        assert record.scene_revision_id == promoted.scene_revision_id


# ---------------------------------------------------------------------
# Retention truth: 'purge' must mean purge — payload gone, bookkeeping
# follows, nothing stranded
# ---------------------------------------------------------------------


class TestRetentionTruth:
    def test_purge_removes_review_bookkeeping_and_payload(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _inbox_rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        retention = CaptureRetentionService(scene)

        plan = retention.plan_capture_revision_purge(
            typed.bundle.capture_revision_id
        )
        assert plan.status == 'ready'
        purged = retention.purge_capture_revision(
            typed.bundle.capture_revision_id
        )
        assert purged.status == 'ready'

        # The review queue entry followed the payload — no ghost row,
        # no inspect() corruption error, no dangling promotions.
        assert inbox.get(typed.lineage_digest) is None
        assert inbox.list_items() == ()
        assert inbox.inspect(typed.lineage_digest) is None
        assert inbox.promotions_for(typed.lineage_digest) == ()
        assert ingestion.list_ingestion_runs() == ()

        # Payloads are actually gone, not merely hidden.
        with closing(
            sqlite3.connect(tmp_path / 'cad.sqlite3')
        ) as connection:
            assert (
                connection.execute(
                    'SELECT COUNT(*) FROM capture_source_evidence'
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    'SELECT COUNT(*) FROM htdt_content_blobs'
                ).fetchone()[0]
                == 0
            )
            for table in (
                'capture_inbox_items',
                'capture_inbox_promotions',
                'capture_inbox_supersessions',
                'capture_inbox_registrations',
            ):
                assert (
                    connection.execute(
                        f'SELECT COUNT(*) FROM {table}'
                    ).fetchone()[0]
                    == 0
                )

    def test_purge_blocked_by_live_semantic_promotion(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _inbox_rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        _scene_doc(scene)
        promotion = _promotion_repo(scene, ingestion)
        run = ingestion.list_ingestion_runs()[0]
        binding_id = ingestion.mesh_binding_ids_for_run(
            run.ingestion_run_id
        )[0]
        source = scene.latest('capture-doc')
        promoted = promotion.promote(
            make_capture_semantic_promotion_request(
                ingestion_run_id=run.ingestion_run_id,
                raw_mesh_binding_id=binding_id,
                target_document_id='capture-doc',
                source_scene_revision_id=source.revision_id,
                world_to_scene_authority=_world_authority(
                    ingestion, run.bundle_digest
                ),
                readiness_policy='allow_blocked_semantic_authority',
                reason='retain captured mesh',
            )
        )
        inbox.record_promotion(
            typed.lineage_digest, 'semantic_geometry', promoted.promotion_id
        )

        retention = CaptureRetentionService(scene)
        plan = retention.plan_capture_revision_purge(
            typed.bundle.capture_revision_id
        )
        assert plan.status == 'blocked'
        assert any(
            dep.kind == 'semantic_promotion'
            for dep in plan.blocking_dependents
        )
        with pytest.raises(CaptureRetentionError, match='dependents'):
            retention.purge_capture_revision(typed.bundle.capture_revision_id)
        # Inbox item and promoted payload survive untouched.
        assert inbox.get(typed.lineage_digest) is not None
        assert len(ingestion.list_ingestion_runs()) == 1

    def test_purging_superseding_revision_revives_superseded_item(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _inbox_rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        a = _stage(ingestion, inbox, tmp_path, revision_id=rev_a)
        b = _stage(
            ingestion, inbox, tmp_path,
            revision_id=rev_b, parent_revision_id=rev_a,
        )
        inbox.promote(
            a.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='v1',
            executor=lambda p, k: f'auth-a-{k}',
        )
        for kind in ('raw_visual_evidence', 'semantic_geometry'):
            inbox.supersede(
                a.lineage_digest, b.lineage_digest, kind,
                reason='rescan replaces it',
            )
        assert inbox.get(a.lineage_digest).disposition == 'superseded'

        retention = CaptureRetentionService(scene)
        retention.purge_capture_revision(rev_b)

        # The 'superseded by <purged lineage>' claim can no longer stand:
        # A honestly reverts to its promoted state.
        revived = inbox.get(a.lineage_digest)
        assert revived is not None
        assert revived.disposition == 'promoted'
        assert inbox.inspect(a.lineage_digest).promotability == 'complete'


# ---------------------------------------------------------------------
# Partial failures: nothing invisible, everything recoverable
# ---------------------------------------------------------------------


class TestPartialFailures:
    def test_failed_ingest_leaves_no_rows_and_recovers(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        plan, payloads = _plan_and_payloads()
        broken = dict(payloads)
        path = next(iter(broken))
        broken[path] = b'\x00' + broken[path]
        with pytest.raises(Exception):
            ingestion.ingest(plan, broken)
        assert ingestion.list_ingestion_runs() == ()
        typed = CaptureIngestionPlan.model_validate(plan)
        assert inbox.get(typed.lineage_digest) is None
        # The same valid bundle ingests cleanly afterwards.
        result = ingestion.ingest(plan, payloads)
        assert result.created

    def test_failed_staging_keeps_run_visible_and_redelivery_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene, ingestion, inbox, reader, service, pairing = _receiver_rig(
            tmp_path
        )
        plan, payloads = _plan_and_payloads()
        archive = b'arc-partial'
        reader.register(archive, plan, payloads)

        calls = {'count': 0}
        real_stage = inbox.stage

        def flaky_stage(*args, **kwargs):
            calls['count'] += 1
            if calls['count'] == 1:
                raise RuntimeError('injected staging crash')
            return real_stage(*args, **kwargs)

        monkeypatch.setattr(inbox, 'stage', flaky_stage)

        status, receipt = service.handle_delivery(
            pairing.pairing_token, _headers(archive), archive
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        # The ingested run is still visible in the library — the partial
        # state is honest, not invisible corruption.
        assert len(ingestion.list_ingestion_runs()) == 1
        typed = CaptureIngestionPlan.model_validate(plan)
        assert inbox.get(typed.lineage_digest) is None

        # Re-delivery completes the pipeline and stages the item.
        status, receipt = service.handle_delivery(
            pairing.pairing_token, _headers(archive), archive
        )
        assert status == 200
        assert inbox.get(typed.lineage_digest) is not None


# ---------------------------------------------------------------------
# Quality gate truth: unresolved quality really blocks promotion, and
# multi-run lineages read the latest run deterministically
# ---------------------------------------------------------------------


class TestQualityTruth:
    def test_multi_run_lineage_quality_reads_latest_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        capture = CaptureIngestionRepository(scene)
        monkeypatch.setitem(
            SUPPORTED_INGESTOR_IDENTITIES,
            ('htdt-capture-reference-ingestor', '2.0.0'),
            'f' * 64,
        )
        revision = str(uuid.uuid4())
        plan_v1, payloads_v1 = _plan_and_payloads(revision_id=revision)
        plan_v2, payloads_v2 = _plan_and_payloads(revision_id=revision)
        plan_v2['ingestor']['version'] = '2.0.0'
        plan_v2['ingestor']['configuration_digest'] = 'f' * 64
        first = capture.ingest(plan_v1, payloads_v1)
        second = capture.ingest(plan_v2, payloads_v2)
        assert first.lineage_digest == second.lineage_digest

        # Simulate a pre-gate legacy row on the OLDER run: quality reads
        # must still resolve to the latest run's state deterministically.
        with closing(
            sqlite3.connect(tmp_path / 'cad.sqlite3')
        ) as connection:
            connection.execute(
                "UPDATE capture_ingestion_runs SET quality_state='unresolved' "
                'WHERE ingestion_run_id=?',
                (first.ingestion_run_id,),
            )
            connection.commit()

        state = capture.get_capture_quality_state(first.lineage_digest)
        assert state is not None
        assert state.state == 'validated'
        # The promote gate resolves the same run deterministically.
        assert (
            capture.require_quality_state(first.lineage_digest).state
            == 'validated'
        )


# ---------------------------------------------------------------------
# UI truth: listing counts / filters / labels match persisted rows
# ---------------------------------------------------------------------


class TestUiTruth:
    def test_inbox_counts_and_filters_match_persisted_rows(
        self, tmp_path: Path
    ) -> None:
        _scene, ingestion, inbox = _inbox_rig(tmp_path)
        pending = _stage(ingestion, inbox, tmp_path)
        deferred = _stage(ingestion, inbox, tmp_path)
        rejected = _stage(ingestion, inbox, tmp_path)
        inbox.defer(deferred.lineage_digest, 'waiting on rescan')
        inbox.reject(rejected.lineage_digest, 'wrong room')

        items = inbox.list_items()
        assert len(items) == 3
        assert {i.disposition for i in items} == {
            'pending', 'deferred', 'rejected',
        }
        for disposition in ('pending', 'deferred', 'rejected'):
            filtered = inbox.list_items(disposition=disposition)
            assert len(filtered) == 1
            assert filtered[0].disposition == disposition

        pending_items = inbox.list_items(disposition='pending')
        assert pending_items[0].lineage_digest == pending.lineage_digest
        inspection = inbox.inspect(pending.lineage_digest)
        assert inspection.promotability == 'promotable'
        assert inspection.item.arrival_count == 1
