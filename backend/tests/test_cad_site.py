"""#729: site/space hierarchy — theater + adjacent spaces, exact frames."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_site import (
    FrameTransform4x4,
    IDENTITY_TRANSFORM,
    SiteRepository,
    link_spaces,
    register_space,
    retarget_space_transform,
)


def _spaces():
    theater = register_space(
        document_id='doc-1',
        kind='theater',
        label='Dedicated Theater',
        geometry_ref='geom:theater-1',
        T_site_from_space=IDENTITY_TRANSFORM,
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    closet = register_space(
        document_id='doc-1',
        kind='equipment',
        label='Rack Closet',
        geometry_ref='geom:closet-1',
        T_site_from_space=FrameTransform4x4(
            column_major_4x4=(
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                6.0, 0.0, 0.0, 1.0,
            )
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    return theater, closet


def test_space_with_exact_transform() -> None:
    theater, closet = _spaces()
    assert theater.kind == 'theater'
    assert closet.T_site_from_space.column_major_4x4[12:] == (6.0, 0.0, 0.0, 1.0)
    assert closet.authority_ref().semantic_hash_sha256 == closet.semantic_sha256


def test_unknown_geometry_is_explicit() -> None:
    space = register_space(
        document_id='doc-1',
        kind='adjacent',
        label='Bedroom',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    assert space.geometry_ref is None
    assert space.T_site_from_space is None


def test_transform_must_be_affine() -> None:
    with pytest.raises(ValueError, match='affine'):
        FrameTransform4x4(column_major_4x4=(1.0,) * 15 + (2.0,))
    with pytest.raises(ValueError, match='16 values'):
        FrameTransform4x4(column_major_4x4=(1.0,) * 9)


def test_retarget_mints_new_authority_version() -> None:
    theater, _ = _spaces()
    moved = retarget_space_transform(
        theater,
        T_site_from_space=FrameTransform4x4(
            column_major_4x4=(
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                1.0, 0.0, 0.0, 1.0,
            )
        ),
        authority_version='2',
    )
    assert moved.authority_version == '2'
    assert moved.space_id == theater.space_id
    assert moved.semantic_sha256 != theater.semantic_sha256


def test_relationship_requires_registered_endpoints(tmp_path: Path) -> None:
    repo = SiteRepository(tmp_path / 'cad.sqlite3')
    theater, closet = _spaces()
    repo.save_space(theater)
    repo.save_space(closet)
    rel = link_spaces(
        document_id='doc-1',
        kind='cable_route',
        space_a_id=theater.space_id,
        space_b_id=closet.space_id,
        detail='speaker + network runs',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    repo.save_relationship(rel)
    assert repo.get_relationship(rel.relationship_id) == rel
    assert [
        r.relationship_id
        for r in repo.list_relationships_for_space('doc-1', closet.space_id)
    ] == [rel.relationship_id]


def test_relationship_rejects_unregistered_endpoint(tmp_path: Path) -> None:
    repo = SiteRepository(tmp_path / 'cad.sqlite3')
    theater, _ = _spaces()
    repo.save_space(theater)
    rel = link_spaces(
        document_id='doc-1',
        kind='opening_portal',
        space_a_id=theater.space_id,
        space_b_id='space:not-registered',
        created_at_utc='2026-09-24T00:00:00+00:00',
    )
    with pytest.raises(ValueError, match='not a registered space'):
        repo.save_relationship(rel)


def test_single_room_project_still_valid(tmp_path: Path) -> None:
    repo = SiteRepository(tmp_path / 'cad.sqlite3')
    theater, _ = _spaces()
    repo.save_space(theater)
    assert [s.space_id for s in repo.list_spaces('doc-1')] == [
        theater.space_id
    ]
    assert [
        s.space_id for s in repo.list_spaces('doc-1', kind='theater')
    ] == [theater.space_id]


def test_self_relationship_rejected() -> None:
    with pytest.raises(ValueError, match='cannot relate to itself'):
        link_spaces(
            document_id='doc-1',
            kind='adjacent_boundary',
            space_a_id='space:x',
            space_b_id='space:x',
        )


def test_relationship_kinds_closed() -> None:
    theater, closet = _spaces()
    for kind in (
        'adjacent_boundary',
        'opening_portal',
        'cable_route',
        'equipment_service',
        'isolation_source_receiver',
    ):
        rel = link_spaces(
            document_id='doc-1',
            kind=kind,
            space_a_id=theater.space_id,
            space_b_id=closet.space_id,
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
        assert rel.kind == kind
    with pytest.raises(ValueError):
        link_spaces(
            document_id='doc-1',
            kind='inferred_mesh_overlap',
            space_a_id=theater.space_id,
            space_b_id=closet.space_id,
        )
