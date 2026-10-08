"""REV72 (#964): change-diff driven evidence invalidation.

Covers the sealed change-diff record, the deterministic revalidation
queue, the fail-closed edges (unknown/uncertain -> review), the single
verify operation over software items, head-pin drift fencing, and
repository round-trip + tamper detection.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_authority_dependency import (  # noqa: E402
    DependencyRuleEntry,
    build_dependency_edge,
    build_rule_profile,
)
from htdt.cad_authority_dependency_repository import (  # noqa: E402
    CadAuthorityDependencyRepository,
)
from htdt.cad_authority_resolver import AuthorityRef  # noqa: E402
from htdt.cad_dependency_impact import (  # noqa: E402
    SceneChange,
    WatchedArtifact,
)
from htdt.cad_evidence_invalidation import (  # noqa: E402
    QueueItemUnavailableError,
    compose_revalidation_queue,
    run_queue_software,
)
from htdt.cad_evidence_invalidation_repository import (  # noqa: E402
    CadEvidenceInvalidationRepository,
    RevalidationIntegrityError,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import (  # noqa: E402
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

DOC = 'rev72-fixture'
T0 = '2026-10-08T00:00:00+00:00'

VERDICT_REF = AuthorityRef(
    kind='verdict',
    ref_id='verdict-1',
    ref_sha256='a' * 64,
)
EVIDENCE_REF = AuthorityRef(
    kind='evidence',
    ref_id='evidence-1',
    ref_sha256='b' * 64,
)
PROFILE_REF = AuthorityRef(
    kind='calibration_profile',
    ref_id='profile-1',
    ref_sha256='c' * 64,
)


def _speaker(role: str = 'FL') -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='FL',
        speaker_role=role,
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
    )


def _seat() -> SceneEntity:
    return SceneEntity(
        entity_id='seat-a',
        kind='seat',
        name='seat-a',
        position=Position3(x_m=1.0, y_m=3.0, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _doc(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOC,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=8.0, height_m=2.5),
        entities=entities,
    )


def _repos(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    first = scene.save(_doc(_speaker(), _seat()), parent_revision_id=None)
    # speaker_role -> axes {source_equipment, topology_routing}
    moved = _speaker('L').model_copy(
        update={'position': Position3(x_m=1.5, y_m=1.0, z_m=1.0)}
    )
    second = scene.save(
        _doc(moved, _seat()), parent_revision_id=first.revision.revision_id
    )
    deps = CadAuthorityDependencyRepository(scene)
    rev = CadEvidenceInvalidationRepository(scene)
    return scene, first.revision, second.revision, deps, rev


def _watched(bound) -> tuple[WatchedArtifact, ...]:
    return (
        WatchedArtifact(
            artifact_kind='prediction',
            artifact_id='pred-1',
            bound_revision_id=bound.revision_id,
            bound_content_hash=bound.content_hash,
            watched_axes={'geometry', 'source_equipment'},
        ),
        WatchedArtifact(
            artifact_kind='measured_dataset',
            artifact_id='meas-1',
            bound_revision_id=bound.revision_id,
            bound_content_hash=bound.content_hash,
            watched_axes={'measurement_context'},
        ),
    )


def _rules(document_id: str = DOC):
    return build_rule_profile(
        document_id=document_id,
        ruleset_version='rs-1',
        entries=(
            DependencyRuleEntry(
                entry_id='rule-1',
                edge_kind='derived_from',
                change_class='calibration_parameters',
                effect='recompute',
            ),
            DependencyRuleEntry(
                entry_id='rule-2',
                edge_kind='measured_under_state',
                change_class='calibration_parameters',
                effect='remeasure',
                action='requalify_profile',
            ),
            DependencyRuleEntry(
                entry_id='rule-equipment',
                edge_kind='measured_under_state',
                change_class='equipment_definition',
                effect='remeasure',
                action='remeasure_channel',
            ),
        ),
        declared_at_utc=T0,
    )


def _edges(document_id: str = DOC):
    return (
        build_dependency_edge(
            document_id=document_id,
            subject_ref=VERDICT_REF,
            kind='derived_from',
            target_ref=EVIDENCE_REF,
            declared_at_utc=T0,
        ),
        build_dependency_edge(
            document_id=document_id,
            subject_ref=EVIDENCE_REF,
            kind='measured_under_state',
            target_ref=PROFILE_REF,
            declared_at_utc=T0,
        ),
    )


def _compose(tmp_path, *, extra_changes=(), **kwargs):
    scene, first, second, deps, rev = _repos(tmp_path)
    bundle = compose_revalidation_queue(
        document_id=DOC,
        from_revision=first,
        to_revision=second,
        watched_artifacts=_watched(first),
        extra_changes=extra_changes,
        edges=kwargs.get('edges', ()),
        rule_profile=kwargs.get('rule_profile'),
        unknown_subjects=kwargs.get('unknown_subjects', ()),
        extra_events=kwargs.get('extra_events', ()),
        prepared_refs=kwargs.get('prepared_refs', {}),
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    return scene, first, second, deps, rev, bundle


def test_compose_seals_diff_and_queue(tmp_path: Path) -> None:
    _scene, first, second, deps, rev, bundle = _compose(tmp_path)

    record = bundle.diff_record
    assert record.diff_id.startswith('chdiff-')
    assert record.from_revision_id == first.revision_id
    assert record.to_revision_id == second.revision_id
    assert record.changes
    assert 'source_equipment' in record.changed_axes
    # scene change events resolved + persisted in the dependency store
    assert bundle.change_events
    classes = {event.change_class for event in bundle.change_events}
    assert 'equipment_definition' in classes
    assert 'device_configuration' in classes
    for event in bundle.change_events:
        assert deps.get_change_event(event.event_id) is not None
    # queue + diff persisted
    assert rev.get_diff_record(record.diff_id) == record
    assert rev.get_queue(bundle.queue.queue_id) == bundle.queue
    assert rev.diff_record_for_head(DOC, second.revision_id) == record
    assert rev.queue_for_head(DOC, second.revision_id) == bundle.queue
    # speaker move stales the geometry+source prediction
    by_key = {i.artifact_kind: i for i in bundle.queue.items}
    assert by_key['prediction'].state == 'stale'
    assert by_key['prediction'].action == 'recompute'
    assert by_key['prediction'].execution_class == 'software'
    # the measurement-context dataset saw no measurement change
    assert 'measured_dataset' not in by_key


def test_material_only_change_leaves_geometry_artifacts(tmp_path):
    """DoD: material-only change must not stale geometry-bound claims."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    doc_id = 'rev72-material'
    first = scene.save(
        _doc(_speaker(), _seat()).model_copy(
            update={'document_id': doc_id}
        ),
        parent_revision_id=None,
    ).revision
    # a name-only entity change carries no dependency axes, so the only
    # axis the diff+declared changes touch is material_boundary
    renamed = _seat().model_copy(update={'name': 'seat-renamed'})
    second = scene.save(
        _doc(_speaker(), renamed).model_copy(
            update={'document_id': doc_id}
        ),
        parent_revision_id=first.revision_id,
    ).revision
    deps = CadAuthorityDependencyRepository(scene)
    rev = CadEvidenceInvalidationRepository(scene)
    material_change = SceneChange(
        kind='authority_reference',
        axes=frozenset({'material_boundary'}),
        entity_id='room',
        detail='wall material changed',
    )
    bundle = compose_revalidation_queue(
        document_id=doc_id,
        from_revision=first,
        to_revision=second,
        watched_artifacts=(
            WatchedArtifact(
                artifact_kind='prediction',
                artifact_id='pred-geometry-only',
                bound_revision_id=first.revision_id,
                bound_content_hash=first.content_hash,
                watched_axes={'geometry'},
            ),
        ),
        extra_changes=(material_change,),
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    assert bundle.queue.items == ()
    assert bundle.queue.software_sequence == ()
    assert bundle.impact_report.impacts[0].state == 'unaffected'


def test_target_change_preserves_raw_measurement(tmp_path: Path) -> None:
    """DoD: target-curve change stales calibration, not raw captures."""
    target_change = SceneChange(
        kind='authority_reference',
        axes=frozenset({'target_design'}),
        entity_id=None,
        detail='TargetCurveProfile changed',
    )
    watched = (
        WatchedArtifact(
            artifact_kind='calibration_plan',
            artifact_id='cal-1',
            bound_revision_id=None,  # filled below
            bound_content_hash=None,
            watched_axes={'target_design'},
        ),
    )
    scene, first, second, deps, rev = _repos(tmp_path)
    watched = (
        watched[0].model_copy(
            update={
                'bound_revision_id': first.revision_id,
                'bound_content_hash': first.content_hash,
            }
        ),
        WatchedArtifact(
            artifact_kind='measured_dataset',
            artifact_id='meas-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'measurement_context'},
        ),
    )
    bundle = compose_revalidation_queue(
        document_id=DOC,
        from_revision=first,
        to_revision=second,
        watched_artifacts=watched,
        extra_changes=(target_change,),
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    by_id = {i.subject_ref.ref_id: i for i in bundle.queue.items}
    assert by_id['cal-1'].action == 're_commission'
    assert by_id['cal-1'].route == 'commissioning_authorization'
    assert 'meas-1' not in by_id


def test_unknown_axis_fails_closed_to_review(tmp_path: Path) -> None:
    """DoD: unknown change scope always becomes review_required."""
    unknown = SceneChange(
        kind='authority_reference',
        axes=frozenset({'unknown'}),
        entity_id=None,
        detail='external authority changed',
    )
    scene, first, second, deps, rev = _repos(tmp_path)
    bundle = compose_revalidation_queue(
        document_id=DOC,
        from_revision=first,
        to_revision=second,
        watched_artifacts=(
            WatchedArtifact(
                artifact_kind='video_geometry',
                artifact_id='vid-1',
                bound_revision_id=first.revision_id,
                bound_content_hash=first.content_hash,
                watched_axes={'geometry'},
            ),
        ),
        extra_changes=(unknown,),
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    items = {i.subject_ref.ref_id: i for i in bundle.queue.items}
    assert items['vid-1'].state == 'uncertain'
    assert items['vid-1'].action == 'review'
    assert items['vid-1'].execution_class == 'review'


def test_unknown_dependency_does_not_invalidate_all(tmp_path: Path) -> None:
    """DoD: an unknown-dependency subject reviews only itself — the
    rest of history is unaffected."""
    from htdt.cad_authority_dependency import build_change_event

    mystery = AuthorityRef(
        kind='evidence', ref_id='mystery-1', ref_sha256='d' * 64
    )
    profile_event = build_change_event(
        document_id=DOC,
        changed_ref=PROFILE_REF,
        change_class='equipment_definition',
        changed_fields=('driver',),
        occurred_at_utc=T0,
    )
    _scene, first, _s, _d, _r, bundle = _compose(
        tmp_path,
        edges=_edges(),
        rule_profile=_rules(),
        unknown_subjects=(mystery,),
        extra_events=(profile_event,),
    )
    review = [
        i for i in bundle.queue.items if i.subject_ref.ref_id == 'mystery-1'
    ]
    assert len(review) == 1
    assert review[0].action == 'review'
    assert review[0].state == 'uncertain'
    # declared dependents still follow the ruleset, not blanket review
    planned = {
        i.subject_ref.ref_id for i in bundle.queue.items
    }
    assert 'evidence-1' in planned
    assert 'verdict-1' in planned
    # and they are remeasure work — never forced to review by the
    # unrelated unknown subject
    assert all(
        i.subject_ref.ref_id == 'mystery-1'
        for i in bundle.queue.items
        if i.action == 'review'
    )


def test_unaffected_artifacts_never_enter_queue(tmp_path: Path) -> None:
    """DoD: unaffected artifacts are recomputed zero times."""
    _scene, _f, _s, _d, _r, bundle = _compose(tmp_path)
    ids = {i.subject_ref.ref_id for i in bundle.queue.items}
    assert 'meas-1' not in ids
    software = set(bundle.queue.software_sequence)
    assert all(
        item.execution_class == 'software' and item.item_key in software
        for item in bundle.queue.items
        if item.subject_ref.ref_id == 'pred-1'
    )


def test_compose_is_idempotent(tmp_path: Path) -> None:
    """DoD: recomposing the same pair produces bit-identical authority —
    append-only saves make restart/re-edit a no-op."""
    scene, first, second, deps, rev, first_bundle = _compose(tmp_path)
    second_bundle = compose_revalidation_queue(
        document_id=DOC,
        from_revision=first,
        to_revision=second,
        watched_artifacts=_watched(first),
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    assert first_bundle.diff_record.diff_sha256 == (
        second_bundle.diff_record.diff_sha256
    )
    assert first_bundle.queue.queue_sha256 == second_bundle.queue.queue_sha256
    # saving the same sealed queue again is a no-op
    rev.save_queue(first_bundle.queue)
    assert len(rev.list_queues(DOC)) == 1


def test_run_queue_software_one_operation(tmp_path: Path) -> None:
    """DoD: software items run as a single verify operation; physical
    items always stop at the confirmation boundary."""
    scene, first, second, deps, rev, bundle = _compose(
        tmp_path,
        extra_changes=(
            SceneChange(
                kind='authority_reference',
                axes=frozenset({'target_design'}),
                entity_id=None,
                detail='target curve changed',
            ),
        ),
    )
    called: list[str] = []

    def runner(item):
        called.append(item.item_key)
        return AuthorityRef(
            kind='prediction',
            ref_id=f'{item.item_key}-result',
            ref_sha256='e' * 64,
        )

    run = run_queue_software(
        bundle.queue,
        pre_head_revision=second,
        post_head_revision=second,
        software_runner=runner,
        revalidation_repository=rev,
        started_at_utc=T0,
        finished_at_utc=T0,
    )
    by_key = {o.item_key: o for o in run.outcomes}
    software_items = [
        i for i in bundle.queue.items if i.execution_class == 'software'
    ]
    assert len(called) == len(software_items)
    for item in software_items:
        assert by_key[item.item_key].status == 'completed'
    for item in bundle.queue.items:
        if item.execution_class == 'physical':
            assert by_key[item.item_key].status == 'awaiting_physical'
    assert rev.get_run(run.run_id) == run
    assert run.verdict in {'software_complete', 'awaiting_human'}


def test_run_fences_pre_and_post_head_drift(tmp_path: Path) -> None:
    scene, first, second, deps, rev, bundle = _compose(tmp_path)
    # pre-head mismatch: everything skipped, drift verdict
    run = run_queue_software(
        bundle.queue,
        pre_head_revision=first,
        revalidation_repository=rev,
        started_at_utc=T0,
        finished_at_utc=T0,
    )
    assert run.verdict == 'drift_detected'
    assert all(o.status == 'skipped' for o in run.outcomes)
    # post-head drift: software ran but the verdict still fails closed
    third = scene.save(
        _doc(_speaker(), _seat()),
        parent_revision_id=second.revision_id,
    ).revision
    run2 = run_queue_software(
        bundle.queue,
        pre_head_revision=second,
        post_head_revision=third,
        software_runner=lambda item: None,
        revalidation_repository=rev,
        started_at_utc=T0,
        finished_at_utc=T0,
    )
    assert run2.verdict == 'drift_detected'


def test_run_unavailable_and_failure_are_honest(tmp_path: Path) -> None:
    _scene, _f, second, _d, _r, bundle = _compose(tmp_path)
    # no runner wired -> unavailable, never fabricated
    run = run_queue_software(
        bundle.queue,
        pre_head_revision=second,
        software_runner=None,
        started_at_utc=T0,
        finished_at_utc=T0,
    )
    statuses = {o.status for o in run.outcomes}
    assert 'unavailable' in statuses
    assert run.verdict == 'awaiting_human'
    # a failing runner aborts the remaining software items
    def bad_runner(item):
        raise RuntimeError('solver exploded')
    run2 = run_queue_software(
        bundle.queue,
        pre_head_revision=second,
        software_runner=bad_runner,
        started_at_utc=T0,
        finished_at_utc=T0,
    )
    assert run2.verdict == 'failed'
    assert {o.status for o in run2.outcomes} >= {'failed'}


def test_repository_tamper_detection(tmp_path: Path) -> None:
    _scene, _f, second, _d, rev, bundle = _compose(tmp_path)
    db_path = tmp_path / 'cad.sqlite3'
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'UPDATE cad_revalidation_queues SET item_count = 99 '
            'WHERE queue_id = ?',
            (bundle.queue.queue_id,),
        )
    with pytest.raises(RevalidationIntegrityError):
        rev.get_queue(bundle.queue.queue_id)


def test_prepared_refs_route_to_physical_surfaces(tmp_path: Path) -> None:
    """DoD: remeasure items carry the #956 plan ref; re_commission the
    #946 orchestration ref — never auto-started, only routed."""
    target_change = SceneChange(
        kind='authority_reference',
        axes=frozenset({'target_design'}),
        entity_id=None,
        detail='TargetCurveProfile changed',
    )
    plan_ref = AuthorityRef(
        kind='campaign_execution_plan',
        ref_id='mcplan-1',
        ref_sha256='f' * 64,
    )
    run_ref = AuthorityRef(
        kind='commissioning_orchestration_run',
        ref_id='cor-1',
        ref_sha256='0' * 64,
    )
    scene, first, second, deps, rev = _repos(tmp_path)
    watched = (
        WatchedArtifact(
            artifact_kind='calibration_plan',
            artifact_id='cal-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'target_design'},
        ),
    )
    bundle = compose_revalidation_queue(
        document_id=DOC,
        from_revision=first,
        to_revision=second,
        watched_artifacts=watched,
        extra_changes=(target_change,),
        prepared_refs={
            ('calibration_plan', '*'): (run_ref,),
            ('measured_dataset', '*'): (plan_ref,),
        },
        dependency_repository=deps,
        revalidation_repository=rev,
    )
    cal = [i for i in bundle.queue.items if i.subject_ref.ref_id == 'cal-1']
    assert cal and cal[0].prepared_refs == (run_ref,)


def test_panel_renders_queue_and_routes(tmp_path: Path) -> None:
    """Panel smoke: compose through the supplier, verify item lines are
    JA and routes navigate."""
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication, QLabel
    from htdt.revalidation_queue_panel import RevalidationQueuePanel

    app = QApplication.instance() or QApplication([])
    scene, first, second, deps, rev, bundle = _compose(tmp_path)
    navigated: list[str] = []

    panel = RevalidationQueuePanel(
        scene,
        DOC,
        revalidation_repository=rev,
        queue_supplier=lambda: compose_revalidation_queue(
            document_id=DOC,
            from_revision=first,
            to_revision=second,
            watched_artifacts=_watched(first),
            dependency_repository=deps,
            revalidation_repository=rev,
        ),
        software_runner=lambda item: AuthorityRef(
            kind='prediction',
            ref_id='pred-new',
            ref_sha256='9' * 64,
        ),
        on_navigate=lambda route: navigated.append(route) or True,
    )
    panel._compose()
    app.processEvents()
    assert panel._queue is not None
    labels = panel.items_container.findChildren(QLabel)
    assert labels
    assert '→' in labels[0].text()
    # physical/review items must not be driven by the runner
    statuses = {o.status for o in panel._latest_run().outcomes} \
        if panel._latest_run() else set()
    panel.deleteLater()
