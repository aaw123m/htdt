from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_cable_run import (
    CableRun,
    CableRunEndpoint,
    CableRunSegment,
    build_cable_run,
    evaluate_cable_run_freshness,
)
from htdt.cad_cable_run_repository import (
    CadCableRunRepository,
    CableRunConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-24T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='rack-avr',
                kind='av_equipment',
                name='AVR rack',
                position=Position3(x_m=5.5, y_m=0.4, z_m=0.9),
                size_m=Size3(x_m=0.5, y_m=0.4, z_m=0.8),
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    return scene_repository, revision, CadCableRunRepository(scene_repository)


def _run(revision, **overrides) -> CableRun:
    payload = {
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'label': 'FL speaker cable',
        'kind': 'speaker_signal',
        'gauge': '14 AWG',
        'from_endpoint': CableRunEndpoint(
            label='AVR FL output', entity_id='rack-avr'
        ),
        'to_endpoint': CableRunEndpoint(
            label='FL speaker terminals', entity_id='speaker-fl'
        ),
        'segments': (
            CableRunSegment(
                sequence=0, path_kind='conduit', length_m=1.5,
                description='rack to floor box',
            ),
            CableRunSegment(
                sequence=1, path_kind='under_floor', length_m=3.2,
            ),
            CableRunSegment(
                sequence=2, path_kind='in_wall', length_m=1.1,
            ),
        ),
        'service_loop_m': 0.5,
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_cable_run(**payload)


def test_run_records_explicit_segments_and_derives_total(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    run = _run(revision)
    # Total is deterministic: segments + service loop — never re-inferred
    # from placement distance.
    assert run.total_length_m == pytest.approx(6.3)
    assert [item.path_kind for item in run.segments] == [
        'conduit', 'under_floor', 'in_wall',
    ]
    repository.save_run(run)
    stored = repository.get_run(run.run_id, run.version)
    assert stored == run
    assert repository.get_run_by_hash(run.semantic_sha256) == run


def test_total_length_is_checked_against_segments() -> None:
    with pytest.raises(ValidationError, match='segments plus service loop'):
        run = _run_for_total_check()
        CableRun.model_validate(
            {**run.model_dump(mode='json'), 'total_length_m': 9.9,
             'semantic_sha256': '0' * 64}
        )


def _run_for_total_check() -> CableRun:
    return build_cable_run(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        label='run',
        kind='power',
        from_endpoint=CableRunEndpoint(label='panel'),
        to_endpoint=CableRunEndpoint(label='subwoofer'),
        segments=(CableRunSegment(sequence=0, path_kind='exposed', length_m=2.0),),
        created_at_utc=NOW,
    )


def test_repository_is_append_only_per_version(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    v1 = _run(revision, run_id='run-fl', version='1')
    repository.save_run(v1)
    v2 = _run(revision, run_id='run-fl', version='2', service_loop_m=1.0)
    repository.save_run(v2)
    with pytest.raises(CableRunConflictError):
        repository.save_run(v1)
    versions = repository.list_run_versions('run-fl')
    assert [item.version for item in versions] == ['1', '2']


def test_save_requires_the_exact_pinned_revision(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    foreign = _run(revision, scene_content_hash='0' * 64)
    with pytest.raises(ValueError, match='content hash mismatch'):
        repository.save_run(foreign)


def test_freshness_reports_stale_and_missing_anchors() -> None:
    run = _run_for_total_check()
    current = evaluate_cable_run_freshness(
        run,
        scene_content_hash='a' * 64,
        present_entity_ids=(),
        present_signal_path_edge_ids=(),
    )
    assert current.status == 'current'

    stale = evaluate_cable_run_freshness(
        run,
        scene_content_hash='b' * 64,
        present_entity_ids=(),
        present_signal_path_edge_ids=(),
    )
    assert stale.status == 'stale'

    missing = evaluate_cable_run_freshness(
        run.model_copy(update={
            'from_endpoint': CableRunEndpoint(
                label='avr', entity_id='gone-entity'
            ),
            'signal_path_edge_id': 'edge-9',
        }),
        scene_content_hash='a' * 64,
        present_entity_ids=('speaker-fl',),
        present_signal_path_edge_ids=('edge-1',),
    )
    assert missing.status == 'missing'
    assert any('endpoint entity absent' in r for r in missing.reasons)
    assert any('signal path edge absent' in r for r in missing.reasons)


def test_edge_and_entity_bindings_are_optional_but_explicit() -> None:
    run = build_cable_run(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        label='subwoofer RCA',
        kind='analog_audio',
        medium='copper',
        from_endpoint=CableRunEndpoint(label='AVR sub out', entity_id='rack-avr'),
        to_endpoint=CableRunEndpoint(label='Sub LFE in', entity_id='sub-1'),
        segments=(
            CableRunSegment(sequence=0, path_kind='surface_raceway', length_m=2.0),
        ),
        signal_path_edge_id='edge-avr-sub',
        created_at_utc=NOW,
    )
    assert run.signal_path_edge_id == 'edge-avr-sub'
    assert run.from_endpoint.entity_id == 'rack-avr'
