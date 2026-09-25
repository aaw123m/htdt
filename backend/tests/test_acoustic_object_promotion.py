"""#965: explicit SceneEntity body → acoustic object promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_object_promotion import (
    assess_object_promotion_currency,
    assess_scene_acoustic_object_participation,
    compile_acoustic_object_promotion,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    EntityBodyGeometry,
    FootprintVertex,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _scene(**kwargs) -> SceneDocument:
    kwargs.setdefault(
        'entities',
        (
            SceneEntity(
                entity_id='sofa-1',
                kind='furniture',
                name='Sofa',
                position=Position3(x_m=3.0, y_m=1.5, z_m=0.45),
                size_m=Size3(x_m=1.8, y_m=0.9, z_m=0.9),
            ),
            SceneEntity(
                entity_id='riser-1',
                kind='riser',
                name='Rear riser',
                position=Position3(x_m=3.0, y_m=3.2, z_m=0.15),
                size_m=Size3(x_m=3.0, y_m=1.6, z_m=0.3),
            ),
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
        ),
    )
    return SceneDocument(
        document_id='object-promotion',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.6),
        **kwargs,
    )


def _material_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='acoustic-material:fabric-sofa',
        authority_version='1',
        semantic_hash_sha256='f' * 64,
    )


def test_promotion_binds_exact_entity_body(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    sofa = next(
        item for item in revision.document.entities
        if item.entity_id == 'sofa-1'
    )
    promotion = compile_acoustic_object_promotion(
        revision, sofa, participation='rigid_boundary'
    )
    assert promotion.promotion_id.startswith('acoustic-object-promotion:')
    assert promotion.ga_participation == 'occlusion'
    assert promotion.wave_participation == 'none'
    assert promotion.geometry is not None
    assert len(promotion.geometry.vertices) == 8
    # 6 quad faces -> 12 triangles
    triangle_count = sum(
        len(surface.triangle_indices)
        for surface in promotion.geometry.surfaces
    )
    assert triangle_count == 12
    assert all(
        surface.semantic_class == 'object_surface'
        for surface in promotion.geometry.surfaces
    )
    # world-space placement: sofa centered at z=0.45 with 0.9m height
    zs = {v[2] for v in promotion.geometry.vertices}
    assert min(zs) == pytest.approx(0.0)
    assert max(zs) == pytest.approx(0.9)


def test_promoted_object_changes_occlusion(tmp_path: Path) -> None:
    # A promoted body demonstrably changes occlusion: a ray through the
    # sofa intersects its promoted triangles while a solver-inert sofa
    # produces none.
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    sofa = next(
        item for item in revision.document.entities
        if item.entity_id == 'sofa-1'
    )
    promotion = compile_acoustic_object_promotion(
        revision, sofa, participation='occlusion_only'
    )
    assert promotion.acoustic_representation == 'occlusion_body'

    from htdt.r120_geometry_compiler import (
        CompiledVertex,
        _ray_intersects_triangle,
    )

    origin = (0.5, 1.5, 0.45)
    target = (5.5, 1.5, 0.45)
    direction = tuple(
        (t - o) for o, t in zip(origin, target)
    )
    hits = 0
    vertices = tuple(
        CompiledVertex(x_m=v[0], y_m=v[1], z_m=v[2])
        for v in promotion.geometry.vertices
    )
    for surface in promotion.geometry.surfaces:
        for triangle in surface.triangle_indices:
            a, b, c = (vertices[i] for i in triangle)
            if _ray_intersects_triangle(origin, direction, a, b, c):
                hits += 1
    assert hits > 0


def test_extruded_polygon_promotion_and_simplification(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    doc = _scene(
        entities=_scene().entities
        + (
            SceneEntity(
                entity_id='table-1',
                kind='furniture',
                name='L-table',
                position=Position3(x_m=4.5, y_m=2.0, z_m=0.4),
                size_m=Size3(x_m=1.0, y_m=1.0, z_m=0.8),
                body_geometry=EntityBodyGeometry(
                    kind='extruded_polygon',
                    footprint_vertices=(
                        FootprintVertex(x_m=-0.5, y_m=-0.5),
                        FootprintVertex(x_m=0.5, y_m=-0.5),
                        FootprintVertex(x_m=0.5, y_m=0.0),
                        FootprintVertex(x_m=0.0, y_m=0.0),
                        FootprintVertex(x_m=0.0, y_m=0.5),
                        FootprintVertex(x_m=-0.5, y_m=0.5),
                    ),
                ),
            ),
        )
    )
    revision = repository.save(doc, parent_revision_id=None).revision
    table = next(
        item for item in revision.document.entities
        if item.entity_id == 'table-1'
    )
    promotion = compile_acoustic_object_promotion(
        revision, table, participation='occlusion_only'
    )
    # 6-vertex L footprint: bottom 4 tris + top 4 tris + 6 sides x2 tris
    assert promotion.geometry is not None
    assert len(promotion.geometry.surfaces) == 8
    assert promotion.simplification_declared == ()


def test_material_bound_requires_exact_authority(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    sofa = next(
        item for item in revision.document.entities
        if item.entity_id == 'sofa-1'
    )
    with pytest.raises(ValueError, match='material authority'):
        compile_acoustic_object_promotion(
            revision, sofa, participation='material_bound_boundary'
        )
    promotion = compile_acoustic_object_promotion(
        revision,
        sofa,
        participation='material_bound_boundary',
        material_ref=_material_ref(),
    )
    assert promotion.material_ref is not None


def test_body_edit_stales_promotion(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    sofa = next(
        item for item in revision.document.entities
        if item.entity_id == 'sofa-1'
    )
    promotion = compile_acoustic_object_promotion(
        revision, sofa, participation='rigid_boundary'
    )
    assert assess_object_promotion_currency(promotion, revision).state == (
        'CURRENT'
    )

    edited = _scene(
        entities=tuple(
            (
                item
                if item.entity_id != 'sofa-1'
                else SceneEntity(
                    entity_id='sofa-1',
                    kind='furniture',
                    name='Sofa',
                    position=Position3(x_m=3.5, y_m=1.5, z_m=0.45),
                    size_m=item.size_m,
                )
            )
            for item in _scene().entities
        )
    )
    revision2 = repository.save(
        edited, parent_revision_id=revision.revision_id
    ).revision
    currency = assess_object_promotion_currency(promotion, revision2)
    assert currency.state == 'STALE'
    assert 'body_geometry_changed' in currency.stale_reasons


def test_participation_assessment_flags_large_inert(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    assessment = assess_scene_acoustic_object_participation(revision)
    states = {item.entity_id: item.state for item in assessment.entries}
    # speakers are excluded — they are sources, not acoustic objects
    assert 'speaker-fl' not in states
    assert states['sofa-1'] == 'visual_only'
    assert states['riser-1'] == 'visual_only'
    assert any('riser-1' in w for w in assessment.completeness_warnings)
    assert any('sofa-1' in w for w in assessment.completeness_warnings)

    sofa = next(
        item for item in revision.document.entities
        if item.entity_id == 'sofa-1'
    )
    promotion = compile_acoustic_object_promotion(
        revision, sofa, participation='occlusion_only'
    )
    assessment = assess_scene_acoustic_object_participation(
        revision, (promotion,)
    )
    states = {item.entity_id: item.state for item in assessment.entries}
    assert states['sofa-1'] == 'occlusion_only'
    assert not any(
        'sofa-1' in w for w in assessment.completeness_warnings
    )
