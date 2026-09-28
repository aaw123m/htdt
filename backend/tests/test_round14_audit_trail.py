"""Round-14 audit-trail & authority-record truth checks.

Every test performs a known action, reads the recorded trail back, and
asserts on CONTENT — actor, inputs, outcome, ordering — rather than on
the mere presence of a row. A trail that exists but lies about what
happened is worse than no trail.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import capture_fixture_support as support  # noqa: E402
from test_cad_hybrid_prediction_provider import (  # noqa: E402
    _build_bundle,
    _digest,
    _ref,
)

from htdt.activity_center import (  # noqa: E402
    ActivityCenter,
    NavigationPolicy,
    OperationClass,
)
from htdt.cad_hybrid_prediction_provider import (  # noqa: E402
    CadHybridPredictionProviderRepository,
    HybridValidatedObservable,
    promote_hybrid_provider_evidence,
)
from htdt.cad_project_activity import (  # noqa: E402
    CadProjectActivityService,
)
from htdt.cad_project_activity_repository import (  # noqa: E402
    CadProjectActivityNoteRepository,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import make_f1_scene  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.data_management import (  # noqa: E402
    ApplicationDataLifecycle,
    DataManagementBackend,
    DataManagementController,
    DataOperationKind,
    _ActiveOperation,
    _OperationWorker,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef  # noqa: E402


SERIES_ID = '10000000-0000-4000-8000-000000000101'
SESSION_ID = '10000000-0000-4000-8000-000000000103'
SPACE_ID = '10000000-0000-4000-8000-000000000104'
ANCHOR_ID = '10000000-0000-4000-8000-000000000105'


def _stage_one(ingestion, inbox, tmp_path, **kwargs):
    """Stage one contract-valid delivery (mirrors test_capture_inbox)."""
    revision_id = kwargs.pop('revision_id', None) or str(uuid.uuid4())
    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    created_at = kwargs.pop('created_at', '2026-09-20T00:00:00Z')
    manifest_overrides = {
        'capture_series_id': kwargs.pop('series_id', SERIES_ID),
        'capture_revision_id': revision_id,
        'parent_revision_id': kwargs.pop('parent_revision_id', None),
        'capture_session_ids': [SESSION_ID],
        'coordinate_space_ids': [SPACE_ID],
        'created_at': created_at,
        'finalized_at': created_at,
    }
    plan, payloads, _manifest = support.plan_and_payloads(
        tmp_path,
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
    ingestion.ingest(plan, payloads)
    typed = CaptureIngestionPlan.model_validate(plan)
    result = inbox.stage(typed, arrival_source='file_import')
    return result, typed


def _resolvable_ref(
    label: str, payloads: dict[str, object]
) -> ExactExternalAuthorityRef:
    """An exact ref whose payload an external resolver can actually serve."""
    payload = {'authority_payload': label}
    ref = ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version='1',
        semantic_hash_sha256=_digest(payload),
    )
    payloads[ref.authority_id] = payload
    return ref


# ---------------------------------------------------------------------
# Ordering truth: the timeline must order by true time, not by string.
# ---------------------------------------------------------------------


def test_timeline_orders_mixed_iso_timestamp_forms_by_true_time(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document_id = repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision.document_id
    notes = CadProjectActivityNoteRepository(repository)
    service = CadProjectActivityService(
        scene_repository=repository, notes_repository=notes
    )

    # Same second, different writers' ISO forms: bare 'Z' sorts AFTER
    # '.500+00:00' lexically, so a string sort puts the later event first.
    service.add_note(
        document_id=document_id,
        title='first',
        created_at_utc='2026-09-23T09:00:00Z',
    )
    service.add_note(
        document_id=document_id,
        title='second',
        created_at_utc='2026-09-23T09:00:00.500+00:00',
    )

    note_titles = [
        event.title
        for event in service.events(document_id)
        if event.kind == 'project_note'
    ]
    assert note_titles == ['メモ「first」', 'メモ「second」']


# ---------------------------------------------------------------------
# Completeness: every real disposition transition must be on the trail.
# ---------------------------------------------------------------------


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    service = CadProjectActivityService(
        scene_repository=scene, capture_inbox=inbox
    )
    return scene, ingestion, inbox, service


def test_capture_dispositions_are_projected_to_the_timeline(
    tmp_path: Path,
) -> None:
    scene, ingestion, inbox, service = _rig(tmp_path)
    document_id = scene.save(
        make_f1_scene(), parent_revision_id=None
    ).revision.document_id

    staged, typed = _stage_one(ingestion, inbox, tmp_path)
    inbox.assign_scope(staged.lineage_digest, document_id)

    # defer is a real disposition transition and must be visible.
    deferred = inbox.defer(staged.lineage_digest, 'waiting on rescan')
    assert deferred.disposition == 'deferred'
    kinds = {e.kind for e in service.events(document_id)}
    assert 'capture_deferred' in kinds

    # resume returns to pending: the item was never disposed, so the
    # "disposed at" stamp must be empty rather than the resume time.
    resumed = inbox.resume(staged.lineage_digest)
    assert resumed.disposition == 'pending'
    assert resumed.disposition_at_utc is None

    # A blocked outcome that does not change the disposition must not
    # stamp a disposal time onto the item.
    inbox.record_blocked(
        staged.lineage_digest, 'connected_space', 'no connected doc'
    )
    item = inbox.get(staged.lineage_digest)
    assert item.disposition == 'pending'
    assert item.disposition_at_utc is None

    # Promotion is recorded per authority kind: the trail must show WHICH
    # kind was promoted, not just that the item "was promoted".
    inbox.promote(
        staged.lineage_digest,
        ['raw_visual_evidence'],
        reason='evidence first',
        executor=lambda plan_, kind: f'promoted-{kind}',
    )
    item = inbox.get(staged.lineage_digest)
    assert item.disposition == 'partially_promoted'
    promoted_titles = [
        e.title
        for e in service.events(document_id)
        if e.kind == 'capture_promoted'
    ]
    assert promoted_titles == [
        f'キャプチャ {item.capture_revision_id} を昇格'
        '（raw_visual_evidence）'
    ]


def test_capture_supersession_is_projected_to_the_timeline(
    tmp_path: Path,
) -> None:
    scene, ingestion, inbox, service = _rig(tmp_path)
    document_id = scene.save(
        make_f1_scene(), parent_revision_id=None
    ).revision.document_id

    rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
    a, _typed_a = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
    b, _typed_b = _stage_one(
        ingestion,
        inbox,
        tmp_path,
        revision_id=rev_b,
        parent_revision_id=rev_a,
    )
    inbox.assign_scope(a.lineage_digest, document_id)
    inbox.promote(
        a.lineage_digest,
        ['raw_visual_evidence', 'semantic_geometry'],
        reason='v1',
        executor=lambda p, k: f'auth-a-{k}',
    )
    assert inbox.get(a.lineage_digest).disposition == 'promoted'

    for kind in ('semantic_geometry', 'raw_visual_evidence'):
        inbox.supersede(
            a.lineage_digest,
            b.lineage_digest,
            kind,
            reason='rescan replaces it',
        )
    assert inbox.get(a.lineage_digest).disposition == 'superseded'

    kinds = [e.kind for e in service.events(document_id)]
    assert 'capture_superseded' in kinds
    # The promotion facts stay on the trail: the item was really promoted
    # before it was superseded, and both must be readable.
    assert 'capture_promoted' in kinds


# ---------------------------------------------------------------------
# Lifecycle truth: promoted-provider refs must resolve, not just look
# like refs. (R170A enforced this at build time; R170B never did.)
# ---------------------------------------------------------------------


def _promotion_claim(bundle):
    return HybridValidatedObservable(
        observable='frequency_response_magnitude',
        evidence_scope='synthetic_fixture',
        frequency_domain=bundle['provider'].valid_frequency_domain,
    )


def test_r170b_promotion_requires_resolving_authority_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _build_bundle(tmp_path / 'refs', monkeypatch)
    candidate = bundle['provider']
    claim = _promotion_claim(bundle)

    payloads: dict[str, object] = {}

    def resolver(ref):
        return payloads.get(ref.authority_id)

    fabricated = _ref('unbacked-authority')
    with pytest.raises(ValueError, match='exact external authority'):
        promote_hybrid_provider_evidence(
            candidate,
            evidence_state='validated',
            evidence_scope='synthetic_fixture',
            validation_authority_ref=fabricated,
            validated_observables=(claim,),
            external_payload_resolver=resolver,
        )

    # A resolver serving bytes that do not match the ref hash fails too.
    payloads[fabricated.authority_id] = {'not': 'the pinned payload'}
    with pytest.raises(ValueError, match='exact external authority'):
        promote_hybrid_provider_evidence(
            candidate,
            evidence_state='validated',
            evidence_scope='synthetic_fixture',
            validation_authority_ref=fabricated,
            validated_observables=(claim,),
            external_payload_resolver=resolver,
        )

    # A ref whose payload resolves promotes and round-trips through the
    # repository, which re-resolves the refs during reopen validation.
    promoted = promote_hybrid_provider_evidence(
        candidate,
        evidence_state='validated',
        evidence_scope='synthetic_fixture',
        validation_authority_ref=_resolvable_ref(
            'hybrid-validation', bundle['external_payloads']
        ),
        validated_observables=(claim,),
        external_payload_resolver=lambda ref: bundle[
            'external_payloads'
        ].get(ref.authority_id),
    )
    assert bundle['hybrid_repository'].save(promoted) == promoted
    assert bundle['hybrid_repository'].get(promoted.provider_id) == promoted


def test_r170b_repository_rejects_rows_whose_refs_stop_resolving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promoted row minted under a trusting resolver must fail the
    authoritative read once its authority ref no longer resolves."""
    bundle = _build_bundle(tmp_path / 'refs-strict', monkeypatch)
    candidate = bundle['provider']
    claim = _promotion_claim(bundle)

    payloads = bundle['external_payloads']

    def resolver(ref):
        return payloads.get(ref.authority_id)

    promoted = promote_hybrid_provider_evidence(
        candidate,
        evidence_state='validated',
        evidence_scope='synthetic_fixture',
        validation_authority_ref=_resolvable_ref('v', payloads),
        validated_observables=(claim,),
        external_payload_resolver=resolver,
    )
    assert bundle['hybrid_repository'].save(promoted) == promoted

    # A second repository over the same store with a resolver that no
    # longer knows the ref must refuse to replay the row rather than
    # trusting the recorded (now unverifiable) evidence claim.
    strict_repository = CadHybridPredictionProviderRepository(
        bundle['fixture']['scene_repository'],
        base_provider_repository=bundle['base_repository'],
        r160_repository=bundle['r160_repository'],
        composition_spec_resolver=lambda spec_id: bundle['specs'].get(
            spec_id
        ),
        wave_excitation_resolver=lambda excitation_id: bundle[
            'excitations'
        ].get(excitation_id),
        external_payload_resolver=lambda ref: None,
    )
    with pytest.raises(ValueError, match='exact external authority'):
        strict_repository.get(promoted.provider_id)


# ---------------------------------------------------------------------
# Outcome truth: a restore whose reload fails is not a clean success.
# ---------------------------------------------------------------------


def test_restore_reload_failure_records_an_honest_outcome(
    tmp_path: Path,
) -> None:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)

    events: list[str] = []

    def fail_reopen() -> None:
        events.append('reopen')
        raise RuntimeError('injected reload failure')

    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: events.append('freeze'),
        release_data_handles=lambda: events.append('release'),
        reopen_data_handles=fail_reopen,
        thaw_mutations=lambda: events.append('thaw'),
    )
    backend = DataManagementBackend(data_dir)
    center = ActivityCenter()
    controller = DataManagementController(
        backend, lifecycle, activity_center=center
    )

    operation_id = 'op-restore-1'
    center.submit(
        operation_id=operation_id,
        operation_kind=DataOperationKind.RESTORE.value,
        operation_class=OperationClass.DATA_MANAGEMENT,
        title='restore',
        navigation_policy=NavigationPolicy.EXCLUSIVE,
        navigation_block_reason='busy',
    )
    center.mark_running(operation_id)
    lifecycle.begin_restore()

    active = _ActiveOperation(
        operation_id=operation_id,
        kind=DataOperationKind.RESTORE,
        thread=QThread(app),
        worker=_OperationWorker(
            operation_id=operation_id,
            kind=DataOperationKind.RESTORE,
            job=lambda emit: None,
        ),
        lifecycle_mode='restore',
        result=None,
    )
    controller._active = active
    controller._complete_success(active)

    operation = center.get(operation_id)
    assert operation is not None
    assert operation.state.value == 'completed'
    # The data was restored but the reload failed; the recorded outcome
    # must carry that failure, not a bare 'restore completed' claim.
    assert '再読み込みに失敗' in (operation.result_summary or '')


# ---------------------------------------------------------------------
# Tamper surface: a hand-edited history row must not kill the whole
# diagnostics read — the corrupt row is dropped, the honest rows load.
# ---------------------------------------------------------------------


def test_activity_history_load_drops_a_corrupt_row(tmp_path: Path) -> None:
    center = ActivityCenter()
    for index in range(2):
        operation_id = f'op-{index}'
        center.submit(
            operation_id=operation_id,
            operation_kind='restore',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title=f'restore {index}',
        )
        center.mark_running(operation_id)
        center.complete(operation_id, result_summary='done')

    path = tmp_path / 'activity-history.json'
    center.persist_history(path)

    payload = json.loads(path.read_text(encoding='utf-8'))
    # Corrupt one stored row so it no longer validates.
    payload['operations'][0]['state'] = 'definitely-not-a-state'
    path.write_text(json.dumps(payload), encoding='utf-8')

    loaded = ActivityCenter.load_history(path)
    assert [op.operation_id for op in loaded] == ['op-1']
    assert loaded[0].result_summary == 'done'
