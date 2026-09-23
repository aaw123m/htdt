from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.cad_dependency_impact import (
    SceneChange,
    WatchedArtifact,
    build_dependency_impact_report,
    diff_scene_documents,
)
from htdt.cad_repository import SceneRepository
from htdt.overview_readiness import OverviewReadinessService
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)


DOCUMENT_ID = 'impact-fixture'


def _speaker() -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='FL',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
    )


def _seat(entity_id: str = 'seat-a') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=1.0, y_m=3.0, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _document(*entities: SceneEntity, width: float = 6.0) -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=width, depth_m=8.0, height_m=2.5),
        entities=entities,
    )


def _two_revisions(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(
        _document(_speaker(), _seat()),
        parent_revision_id=None,
    ).revision
    moved = _speaker().model_copy(
        update={'position': Position3(x_m=1.5, y_m=1.0, z_m=1.0)}
    )
    second = repository.save(
        _document(moved, _seat()),
        parent_revision_id=first.revision_id,
    ).revision
    return repository, first, second


def test_diff_reports_exact_axes_per_change(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(
        _document(_speaker(), _seat()),
        parent_revision_id=None,
    ).revision
    moved = _speaker().model_copy(
        update={'position': Position3(x_m=1.5, y_m=1.0, z_m=1.0)}
    )
    second_doc = _document(
        moved,
        _seat(),
        SceneEntity(
            entity_id='mp-1',
            kind='measurement_point',
            name='mp-1',
            position=Position3(x_m=1.0, y_m=2.0, z_m=1.0),
        ),
    )
    second = repository.save(second_doc, parent_revision_id=first.revision_id)

    changes = diff_scene_documents(first.document, second_doc)
    by_entity = {change.entity_id: change for change in changes}
    assert by_entity['speaker-fl'].axes == {'geometry', 'source_equipment'}
    assert by_entity['mp-1'].axes == {'measurement_context'}
    assert by_entity['mp-1'].kind == 'entity_added'

    widened = repository.save(
        _document(moved, _seat(), width=7.0),
        parent_revision_id=second.revision.revision_id,
    )
    room_changes = diff_scene_documents(second_doc, widened.revision.document)
    assert room_changes[0].kind == 'room_geometry'
    assert room_changes[0].axes == {'geometry', 'material_boundary'}


def test_impact_report_stales_only_watched_axes(tmp_path: Path) -> None:
    _repo, first, second = _two_revisions(tmp_path)
    artifacts = (
        WatchedArtifact(
            artifact_kind='prediction',
            artifact_id='pred-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'geometry', 'source_equipment'},
        ),
        WatchedArtifact(
            artifact_kind='measured_dataset',
            artifact_id='meas-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'measurement_context'},
        ),
        WatchedArtifact(
            artifact_kind='seat_priority_profile',
            artifact_id='spp-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'geometry'},
            watched_entity_ids=('seat-a',),
        ),
        WatchedArtifact(
            artifact_kind='calibration_plan',
            artifact_id='cal-1',
            bound_revision_id=second.revision_id,
            bound_content_hash=second.content_hash,
            watched_axes={'geometry', 'target_design'},
        ),
    )

    report = build_dependency_impact_report(
        from_revision=first,
        to_revision=second,
        artifacts=artifacts,
    )
    by_id = {item.artifact_id: item for item in report.impacts}
    assert by_id['pred-1'].state == 'stale'
    assert by_id['pred-1'].action == 'recompute'
    assert by_id['pred-1'].dependency_edges
    assert by_id['meas-1'].state == 'unaffected'
    assert by_id['spp-1'].state == 'unaffected'
    assert by_id['cal-1'].state == 'unaffected'
    assert report.stale_count == 1
    assert report.unaffected_count == 3
    assert 'geometry' in report.changed_axes
    assert 'source_equipment' in report.changed_axes


def test_target_change_stales_calibration_not_geometry_artifacts(
    tmp_path: Path,
) -> None:
    _repo, first, second = _two_revisions(tmp_path)
    target_change = SceneChange(
        kind='authority_reference',
        axes=frozenset({'target_design'}),
        entity_id=None,
        detail='TargetCurveProfile changed',
    )
    artifacts = (
        WatchedArtifact(
            artifact_kind='calibration_plan',
            artifact_id='cal-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'target_design', 'calibration_settings'},
        ),
        WatchedArtifact(
            artifact_kind='prediction',
            artifact_id='pred-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'geometry', 'source_equipment'},
        ),
    )
    # Re-diff a no-change pair via an added entity to keep the revision pair.
    report = build_dependency_impact_report(
        from_revision=first,
        to_revision=second,
        artifacts=artifacts,
        extra_changes=(target_change,),
    )
    by_id = {item.artifact_id: item for item in report.impacts}
    # Target change stales calibration; the speaker move stales prediction.
    assert by_id['cal-1'].action == 're_commission'
    assert by_id['pred-1'].state == 'stale'


def test_unknown_axis_change_is_conservatively_uncertain(tmp_path: Path) -> None:
    _repo, first, second = _two_revisions(tmp_path)
    unknown = SceneChange(
        kind='authority_reference',
        axes=frozenset({'unknown'}),
        entity_id=None,
        detail='external authority changed without dependency precision',
    )
    artifacts = (
        WatchedArtifact(
            artifact_kind='video_geometry',
            artifact_id='video-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'geometry'},
        ),
    )
    report = build_dependency_impact_report(
        from_revision=first,
        to_revision=second,
        artifacts=artifacts,
        extra_changes=(unknown,),
    )
    impact = report.impacts[0]
    assert impact.state == 'uncertain'
    assert 'conservative' in impact.reason


def test_report_is_deterministic_and_hash_bound(tmp_path: Path) -> None:
    _repo, first, second = _two_revisions(tmp_path)
    artifacts = (
        WatchedArtifact(
            artifact_kind='prediction',
            artifact_id='pred-1',
            bound_revision_id=first.revision_id,
            bound_content_hash=first.content_hash,
            watched_axes={'geometry'},
        ),
    )
    one = build_dependency_impact_report(
        from_revision=first,
        to_revision=second,
        artifacts=artifacts,
    )
    two = build_dependency_impact_report(
        from_revision=first,
        to_revision=second,
        artifacts=artifacts,
    )
    assert one.report_sha256 == two.report_sha256

    with pytest.raises(ValueError, match='distinct revisions'):
        build_dependency_impact_report(
            from_revision=first,
            to_revision=first,
            artifacts=artifacts,
        )


def test_overview_warns_when_bound_prediction_goes_stale(
    tmp_path: Path,
) -> None:
    repository, first, second = _two_revisions(tmp_path)

    class _Source:
        def current_head(self, document_id):
            return second if document_id == DOCUMENT_ID else None

        def get(self, revision_id):
            return repository.get(revision_id)

    stale_prediction = SimpleNamespace(
        status='completed',
        result_id='pred-stale',
        scene_revision_id=first.revision_id,
        scene_content_hash=first.content_hash,
        geometry_compatibility='supported',
    )
    service = OverviewReadinessService(
        _Source(),
        SimpleNamespace(list_measurements=lambda document_id: ()),
        SimpleNamespace(list_results=lambda document_id: (stale_prediction,)),
        SimpleNamespace(list_specs=lambda document_id: ()),
        SimpleNamespace(inspect_for_search_spec=lambda spec_id: ()),
        impact_source=repository,
    )
    view = service.read(DOCUMENT_ID)
    impact = [n for n in view.warnings if n.code == 'impact.stale']
    assert len(impact) == 1
    assert '予測' in impact[0].message
    assert '再計算が必要' in impact[0].message
