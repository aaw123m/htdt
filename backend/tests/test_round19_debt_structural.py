"""Regression coverage for the structural deferreds implemented in round 19.

- ``scene_document_heads`` dangling head pointer fails closed instead of
  reading the document as head-less.
- The executor progress sink has an explicit lease lifecycle; successor
  controllers neither starve nor keep a stale sink.
- ``capture_disposition_transitions`` is created by schema v10→v11 and
  every disposition write path appends one audit row.
- Legacy API list endpoints declare response models and bounded
  ``offset``/``limit`` paging with unchanged wire shapes.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import (  # noqa: E402
    SceneDocumentHeadIntegrityError,
    SceneRepository,
)
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene  # noqa: E402
from htdt.cad_schema import (  # noqa: E402
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
    read_native_schema_version,
)
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.capture_retention import CaptureRetentionService  # noqa: E402

SERIES_ID = '10000000-0000-4000-8000-000000000001'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'


def _plan_and_payloads(
    tmp_path: Path,
    *,
    revision_id: str | None = None,
    parent_revision_id: str | None = None,
) -> tuple[dict, dict[str, bytes]]:
    workdir = Path(tempfile.mkdtemp(dir=tmp_path))
    revision_id = revision_id or str(uuid.uuid4())
    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    manifest_overrides = {
        'capture_series_id': SERIES_ID,
        'capture_revision_id': revision_id,
        'parent_revision_id': parent_revision_id,
        'capture_session_ids': [SESSION_ID],
        'coordinate_space_ids': [SPACE_ID],
        'created_at': '2026-09-20T00:00:00Z',
        'finalized_at': '2026-09-20T00:00:00Z',
    }
    plan, payloads, _manifest = support.plan_and_payloads(
        workdir,
        files=support.mesh_specs_files(
            ((ANCHOR_ID, geometry_path, 3, 1),),
            anchor_transform=(
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ),
            session_id=SESSION_ID,
            space_id=SPACE_ID,
        ),
        manifest_overrides=manifest_overrides,
        id_map={
            support.SESSION_ID: SESSION_ID,
            support.SPACE_ID: SPACE_ID,
        },
    )
    return plan, payloads


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return scene, ingestion, inbox


def _stage(ingestion, inbox, tmp_path, **kwargs):
    plan, payloads = _plan_and_payloads(tmp_path, **kwargs)
    ingestion.ingest(plan, payloads)
    typed = CaptureIngestionPlan.model_validate(plan)
    inbox.stage(typed, arrival_source='file_import')
    return typed


class TestHeadPointerIntegrity:
    def test_missing_head_revision_fails_closed(self, tmp_path: Path) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        saved = scene.save(make_f1_scene(), parent_revision_id=None)
        assert scene.current_head(F1_DOCUMENT_ID) is not None

        # A head row pointing at a revision that does not exist cannot
        # arise through the write path — only through corruption or a
        # foreign_keys-off rebuild. Simulate that damage directly.
        with sqlite3.connect(scene.path) as connection:
            connection.execute(
                'DELETE FROM scene_revisions WHERE revision_id=?',
                (saved.revision.revision_id,),
            )

        with pytest.raises(SceneDocumentHeadIntegrityError):
            scene.current_head(F1_DOCUMENT_ID)
        with pytest.raises(SceneDocumentHeadIntegrityError):
            scene.latest(F1_DOCUMENT_ID)

    def test_document_without_head_row_is_still_headless(
        self, tmp_path: Path
    ) -> None:
        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        assert scene.current_head(F1_DOCUMENT_ID) is None


class TestProgressSinkLease:
    @staticmethod
    def _executor(tmp_path: Path):
        from htdt.cad_multifidelity import CadMultiFidelityRepository
        from htdt.cad_multifidelity_execution import (
            CadMultiFidelityExecutionRepository,
        )
        from htdt.cad_r140_executor import (
            BoundedR140Executor,
            CadR140ExecutorRepository,
        )

        scene = SceneRepository(tmp_path / 'cad.sqlite3')
        resolve = lambda ref, context=None: None  # noqa: E731
        multifidelity = CadMultiFidelityRepository(
            scene,
            authority_resolver=resolve,
            stage_evidence_resolver=resolve,
        )
        execution = CadMultiFidelityExecutionRepository(
            scene,
            multifidelity_repository=multifidelity,
            external_authority_resolver=resolve,
        )
        runtime = CadR140ExecutorRepository(
            scene, execution_repository=execution
        )
        return BoundedR140Executor(
            execution_repository=execution,
            runtime_repository=runtime,
            worker_port=lambda task, context: None,
            max_workers=1,
        )

    def test_lease_stacks_and_unwinds(self, tmp_path: Path) -> None:
        executor = self._executor(tmp_path)
        try:
            assert executor.progress_sink is None

            first = executor.acquire_progress_sink(
                lambda task_id, fraction, message: None
            )
            assert executor.progress_sink is first.sink
            second = executor.acquire_progress_sink(
                lambda task_id, fraction, message: 'x'
            )
            assert executor.progress_sink is second.sink

            second.release()
            assert executor.progress_sink is first.sink
            second.release()  # idempotent
            first.release()
            assert executor.progress_sink is None
        finally:
            executor.close()

    def test_lease_on_closed_executor_is_refused(
        self, tmp_path: Path
    ) -> None:
        executor = self._executor(tmp_path)
        executor.close()
        with pytest.raises(RuntimeError, match='closed'):
            executor.acquire_progress_sink(
                lambda task_id, fraction, message: None
            )

    def test_successor_controller_takes_over_and_release_restores(
        self, tmp_path: Path
    ) -> None:
        from htdt.cad_prediction_execution import (
            PredictionExecutionController,
        )

        executor = self._executor(tmp_path)
        try:
            controller_a = PredictionExecutionController(executor)
            assert executor.progress_sink == controller_a._on_progress

            controller_b = PredictionExecutionController(executor)
            # The successor reports immediately — it cannot starve behind
            # a sink a prior controller left bound.
            assert executor.progress_sink == controller_b._on_progress

            controller_b.close()
            assert executor.progress_sink == controller_a._on_progress
            controller_a.close()
            assert executor.progress_sink is None
        finally:
            executor.close()

    def test_close_clears_outstanding_leases(self, tmp_path: Path) -> None:
        from htdt.cad_prediction_execution import (
            PredictionExecutionController,
        )

        executor = self._executor(tmp_path)
        controller = PredictionExecutionController(executor)
        executor.close()
        controller.close()  # releasing after executor close is safe
        assert executor.progress_sink is None


class TestDispositionTransitions:
    def test_schema_v11_creates_and_requires_the_ledger(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / 'cad.sqlite3'
        assert ensure_native_schema(path) == NATIVE_SCHEMA_VERSION == 11
        with sqlite3.connect(path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    'PRAGMA table_info(capture_disposition_transitions)'
                )
            }
        assert columns == {
            'transition_id',
            'lineage_digest',
            'from_disposition',
            'to_disposition',
            'reason',
            'actor',
            'changed_at_utc',
        }

    def test_upgrade_from_v10_preserves_items_and_adds_ledger(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        inbox.defer(typed.lineage_digest, 'parked before upgrade')

        # Rewind the marker and drop the ledger to reconstruct the exact
        # on-disk shape a v10 database has (items present, table absent).
        with sqlite3.connect(scene.path) as connection:
            connection.execute(
                'UPDATE native_schema_metadata SET schema_version=10 '
                'WHERE singleton=1'
            )
            connection.execute(
                'DELETE FROM native_schema_migrations '
                'WHERE schema_version=11'
            )
            connection.execute(
                'DROP TABLE capture_disposition_transitions'
            )

        assert ensure_native_schema(scene.path) == 11
        assert read_native_schema_version(scene.path) == 11

        item = inbox.get(typed.lineage_digest)
        assert item is not None
        assert item.disposition == 'deferred'
        # The v10 item had no ledger; its trail starts with the first
        # post-upgrade change.
        assert inbox.disposition_transitions(typed.lineage_digest) == ()
        inbox.resume(typed.lineage_digest)
        transitions = inbox.disposition_transitions(typed.lineage_digest)
        assert [t.to_disposition for t in transitions] == ['pending']
        assert transitions[0].from_disposition == 'deferred'
        assert transitions[0].actor == 'resume'

    def test_every_disposition_change_appends_one_row(
        self, tmp_path: Path
    ) -> None:
        _, ingestion, inbox = _rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        digest = typed.lineage_digest

        inbox.defer(digest, 'waiting on rescan')
        inbox.resume(digest)
        inbox.reject(digest, 'wrong room')
        inbox.resume(digest)
        inbox.promote(
            digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='full promote',
            executor=lambda plan, kind: f'promoted-{kind}',
        )

        transitions = inbox.disposition_transitions(digest)
        assert [(t.from_disposition, t.to_disposition, t.actor) for t in transitions] == [
            (None, 'pending', 'stage'),
            ('pending', 'deferred', 'defer'),
            ('deferred', 'pending', 'resume'),
            ('pending', 'rejected', 'reject'),
            ('rejected', 'pending', 'resume'),
            ('pending', 'partially_promoted', 'promotion_outcome'),
            ('partially_promoted', 'promoted', 'promotion_outcome'),
        ]
        assert all(
            t.transition_id.startswith('capture-disposition-transition:')
            for t in transitions
        )
        assert len({t.transition_id for t in transitions}) == len(transitions)
        assert transitions[1].reason == 'waiting on rescan'

    def test_unchanged_outcome_records_no_transition(
        self, tmp_path: Path
    ) -> None:
        _, ingestion, inbox = _rig(tmp_path)
        typed = _stage(ingestion, inbox, tmp_path)
        digest = typed.lineage_digest

        # Partial promotion: pending -> partially_promoted once; recording
        # the second kind's block must not mint another transition.
        inbox.promote(
            digest,
            ['raw_visual_evidence'],
            reason='first kind',
            executor=lambda plan, kind: f'promoted-{kind}',
        )
        inbox.record_blocked(digest, 'semantic_geometry', 'mesh too dense')

        transitions = inbox.disposition_transitions(digest)
        assert [(t.from_disposition, t.to_disposition) for t in transitions] == [
            (None, 'pending'),
            ('pending', 'partially_promoted'),
        ]

    def test_supersede_flip_appends_transition(self, tmp_path: Path) -> None:
        _, ingestion, inbox = _rig(tmp_path)
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
            executor=lambda plan, kind: f'auth-{kind}',
        )
        inbox.supersede(
            a.lineage_digest,
            b.lineage_digest,
            'raw_visual_evidence',
            reason='rescan replaces evidence',
        )
        inbox.supersede(
            a.lineage_digest,
            b.lineage_digest,
            'semantic_geometry',
            reason='rescan replaces geometry',
        )

        transitions = inbox.disposition_transitions(a.lineage_digest)
        last = transitions[-1]
        assert (last.from_disposition, last.to_disposition, last.actor) == (
            'promoted',
            'superseded',
            'supersede',
        )

    def test_purged_lineage_drops_ledger_and_revert_records_row(
        self, tmp_path: Path
    ) -> None:
        scene, ingestion, inbox = _rig(tmp_path)
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
            executor=lambda plan, kind: f'auth-{kind}',
        )
        inbox.supersede(
            a.lineage_digest, b.lineage_digest,
            'raw_visual_evidence', reason='replaced',
        )
        inbox.supersede(
            a.lineage_digest, b.lineage_digest,
            'semantic_geometry', reason='replaced',
        )
        assert inbox.get(a.lineage_digest).disposition == 'superseded'

        retention = CaptureRetentionService(scene)
        retention.purge_capture_revision(rev_b)

        # The purged revision's item and its ledger rows are gone.
        assert inbox.get(b.lineage_digest) is None
        assert inbox.disposition_transitions(b.lineage_digest) == ()

        # The superseded item reverted to its promoted disposition, and
        # the ledger records that revert instead of hiding it.
        assert inbox.get(a.lineage_digest).disposition == 'promoted'
        last = inbox.disposition_transitions(a.lineage_digest)[-1]
        assert (last.from_disposition, last.to_disposition, last.actor) == (
            'superseded',
            'promoted',
            'retention_purge',
        )


class TestApiContracts:
    @staticmethod
    def _client(tmp_path: Path):
        from fastapi.testclient import TestClient
        from htdt.main import create_app

        return TestClient(create_app(tmp_path))

    def test_list_endpoints_page_without_shape_change(
        self, tmp_path: Path
    ) -> None:
        client = self._client(tmp_path)
        ids = [
            client.post('/api/projects', json={'name': f'p{i}'}).json()['id']
            for i in range(3)
        ]

        everything = client.get('/api/projects')
        assert everything.status_code == 200
        assert [row['id'] for row in everything.json()] == ids[::-1]

        page = client.get('/api/projects?offset=1&limit=1')
        assert page.status_code == 200
        assert [row['id'] for row in page.json()] == [ids[::-1][1]]

        assert client.get('/api/projects?limit=0').status_code == 422
        assert client.get('/api/projects?offset=-1').status_code == 422
        too_big = client.get(f'/api/projects?limit={10_001}')
        assert too_big.status_code == 422

    def test_list_endpoints_declare_response_models(
        self, tmp_path: Path
    ) -> None:
        from htdt.main import (
            AttachmentRow,
            ComparisonRow,
            ConstraintSetRow,
            ContextRow,
            MeasurementRow,
            ProjectRow,
            SearchSpecRow,
            SessionRow,
            create_app,
        )

        routes = {
            route.path: route
            for route in create_app(tmp_path).routes
            if getattr(route, 'methods', None) == {'GET'}
        }
        expected = {
            '/api/projects': list[ProjectRow],
            '/api/projects/{project_id}/sessions': list[SessionRow],
            '/api/projects/{project_id}/contexts': list[ContextRow],
            '/api/projects/{project_id}/constraint-sets': list[
                ConstraintSetRow
            ],
            '/api/projects/{project_id}/search-specs': list[SearchSpecRow],
            '/api/projects/{project_id}/measurements': list[MeasurementRow],
            '/api/projects/{project_id}/attachments': list[AttachmentRow],
            '/api/projects/{project_id}/comparisons': list[ComparisonRow],
        }
        for path, model in expected.items():
            assert routes[path].response_model == model, (
                path,
                routes[path].response_model,
            )

    def test_sessions_pagination_bounds_apply(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        project = client.post('/api/projects', json={'name': 'p'}).json()
        for i in range(3):
            client.post(
                f"/api/projects/{project['id']}/sessions",
                json={'purpose': f's{i}'},
            )
        listed = client.get(
            f"/api/projects/{project['id']}/sessions?offset=0&limit=2"
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 2
        assert (
            client.get(
                f"/api/projects/{project['id']}/sessions?limit=0"
            ).status_code
            == 422
        )
