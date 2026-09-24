from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_installation_datum import (
    DatumFrameSemantics,
    DatumReferencePoint,
    InstallationDatum,
    build_installation_datum,
    evaluate_datum_freshness,
    reproject_datum_coordinates,
)
from htdt.cad_installation_datum_repository import (
    CadInstallationDatumRepository,
    InstallationDatumConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument, room_vertices
from htdt.cad_wall_models import WallTopology
from htdt.cad_walls import make_wall_topology

NOW = '2026-09-24T00:00:00+00:00'
X_WALL = 'wall:front-left->front-right'
Y_WALL = 'wall:rear-left->front-left'


def _document(document_id: str = 'doc-1') -> SceneDocument:
    room = RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4)
    return SceneDocument(
        document_id=document_id,
        schema_version=3,
        room=room,
        wall_topology=make_wall_topology(room),
        entities=(),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _document(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadInstallationDatumRepository(
        scene_repository
    )


def _datum(revision, **overrides) -> InstallationDatum:
    payload = {
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'frame_semantics': DatumFrameSemantics(
            origin_label='front-left corner',
            x_label='along front wall (room width)',
            y_label='along left wall (room depth)',
            z_label='up',
        ),
        'primary_anchor': DatumReferencePoint(
            kind='room_vertex', vertex_id='front-left', label='front-left corner'
        ),
        'x_direction_wall_id': X_WALL,
        'y_direction_wall_id': Y_WALL,
        'evidence_refs': ('site-survey-2026-09', 'floorplan-v2'),
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_installation_datum(**payload)


def test_reprojection_reproduces_the_declared_frame(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    datum = _datum(revision)
    document = revision.document
    result = reproject_datum_coordinates(
        datum, room=document.room, walls=document.wall_topology.walls
    )
    assert result.status == 'AVAILABLE'
    assert result.origin_x_m == 0.0 and result.origin_y_m == 0.0
    # +X runs along the front wall (canonical +X); +Y runs along the left
    # wall toward the rear (canonical +Y) — no rotation.
    assert result.x_axis_dx == pytest.approx(1.0)
    assert result.x_axis_dy == pytest.approx(0.0)
    assert result.y_axis_dx == pytest.approx(0.0)
    assert result.y_axis_dy == pytest.approx(1.0)
    assert result.rotation_deg == pytest.approx(0.0)
    assert result.evidence['x_direction_wall_id'] == X_WALL
    assert result.evidence['scene_revision_id'] == revision.revision_id


def test_reprojection_reports_the_frame_rotation(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    # Anchor at rear-left: +X wall along the rear, +Y wall toward the front.
    datum = _datum(
        revision,
        primary_anchor=DatumReferencePoint(
            kind='room_vertex', vertex_id='rear-left', label='rear-left corner'
        ),
        x_direction_wall_id='wall:rear-right->rear-left',
        y_direction_wall_id='wall:rear-left->front-left',
    )
    document = revision.document
    result = reproject_datum_coordinates(
        datum, room=document.room, walls=document.wall_topology.walls
    )
    assert result.status == 'AVAILABLE'
    assert result.origin_x_m == 0.0
    assert result.origin_y_m == pytest.approx(4.5)
    assert result.y_axis_dy == pytest.approx(-1.0)


def test_non_perpendicular_direction_walls_fail_closed(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    # Two parallel walls can never define a room frame.
    datum = _datum(
        revision,
        x_direction_wall_id=X_WALL,
        y_direction_wall_id='wall:rear-right->rear-left',
    )
    document = revision.document
    result = reproject_datum_coordinates(
        datum, room=document.room, walls=document.wall_topology.walls
    )
    assert result.status == 'UNKNOWN'
    assert 'not perpendicular' in result.reason


def test_repository_is_append_only_and_revision_verified(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    datum = _datum(revision, datum_id='datum-main', version='1')
    repository.save_datum(datum)
    assert repository.get_datum('datum-main', '1') == datum
    assert repository.get_datum_by_hash(datum.semantic_sha256) == datum
    with pytest.raises(InstallationDatumConflictError):
        repository.save_datum(datum)

    foreign = _datum(revision, scene_content_hash='0' * 64, datum_id='bad')
    with pytest.raises(ValueError, match='content hash mismatch'):
        repository.save_datum(foreign)


def test_freshness_flags_stale_pin_and_missing_references() -> None:
    # Freshness is a pure evaluator — no repository needed.
    revision_doc = _document()
    datum = build_installation_datum(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        frame_semantics=DatumFrameSemantics(
            origin_label='corner', x_label='x', y_label='y', z_label='z'
        ),
        primary_anchor=DatumReferencePoint(
            kind='room_vertex', vertex_id='front-left', label='front-left'
        ),
        x_direction_wall_id=X_WALL,
        y_direction_wall_id=Y_WALL,
        created_at_utc=NOW,
    )
    walls = make_wall_topology(revision_doc.room)
    vertices = room_vertices(revision_doc.room)
    current = evaluate_datum_freshness(
        datum,
        scene_content_hash='a' * 64,
        present_vertex_ids=[v.vertex_id for v in vertices],
        present_wall_ids=[w.wall_id for w in walls.walls],
    )
    assert current.status == 'current'

    stale = evaluate_datum_freshness(
        datum,
        scene_content_hash='b' * 64,
        present_vertex_ids=[v.vertex_id for v in vertices],
        present_wall_ids=[w.wall_id for w in walls.walls],
    )
    assert stale.status == 'stale'

    missing = evaluate_datum_freshness(
        datum,
        scene_content_hash='a' * 64,
        present_vertex_ids=['rear-right'],
        present_wall_ids=['wall:rear-right->rear-left'],
    )
    assert missing.status == 'missing'
    assert any('anchor vertex absent' in r for r in missing.reasons)


def test_datum_rejects_identical_direction_walls() -> None:
    with pytest.raises(ValidationError):
        build_installation_datum(
            document_id='doc-1',
            scene_revision_id='rev-1',
            scene_content_hash='a' * 64,
            frame_semantics=DatumFrameSemantics(
                origin_label='corner', x_label='x', y_label='y', z_label='z'
            ),
            primary_anchor=DatumReferencePoint(
                kind='room_vertex', vertex_id='front-left', label='corner'
            ),
            x_direction_wall_id=X_WALL,
            y_direction_wall_id=X_WALL,
            created_at_utc=NOW,
        )
