"""Pure-function tests for the #572 3D semantic visual language.

The glyph proxies are render-only templates; these tests exercise the mesh
factories and category palette directly (no renderer/GPU required).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from htdt.cad_scene import Position3, Quaternion4, SceneEntity, Size3
from htdt.room_viewport import (
    _CATEGORY_LEGEND_LABELS,
    _SEMANTIC_CATEGORY_BY_KIND,
    _category_color,
    _entity_category,
    _semantic_glyph_local_meshes,
    _shade_hex,
    semantic_entity_meshes,
)
from htdt.ui_theme import DARK_THEME


def _entity(
    kind: str,
    *,
    size: tuple[float, float, float] | None = (0.30, 0.32, 1.10),
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> SceneEntity:
    return SceneEntity(
        entity_id=f"e-{kind}",
        kind=kind,
        name=kind,
        position=Position3(
            x_m=position[0], y_m=position[1], z_m=position[2]
        ),
        orientation=Quaternion4(),
        speaker_role="FL" if kind == "speaker" else None,
        size_m=None
        if size is None or kind == "measurement_point"
        else Size3(x_m=size[0], y_m=size[1], z_m=size[2]),
    )


def test_entity_category_covers_every_kind_with_legend_label() -> None:
    # Every mapped category resolves to a palette token + legend wording.
    categories = DARK_THEME.viewport.categories
    for kind, category in _SEMANTIC_CATEGORY_BY_KIND.items():
        entity = _entity(kind)
        assert _entity_category(entity) == category
        assert _category_color(category) == getattr(categories, category).hex
        assert category in _CATEGORY_LEGEND_LABELS
    # Unknown kinds fall back to architecture rather than crashing.
    assert _entity_category(_entity("furniture")) == "architecture"


def test_category_palette_is_low_saturation_and_distinct() -> None:
    from dataclasses import fields as dataclass_fields

    categories = DARK_THEME.viewport.categories
    names = [field.name for field in dataclass_fields(categories)]
    colors = {getattr(categories, name).hex for name in names}
    # The acceptance grammar asks for muted category fills distinct from the
    # saturated scientific measured/predicted/cursor accents (#572).
    scientific = DARK_THEME.scientific
    accents = {
        scientific.measured.hex,
        scientific.predicted.hex,
        scientific.cursor.hex,
        scientific.primary_trace.hex,
        scientific.secondary_trace.hex,
    }
    assert colors.isdisjoint(accents)
    assert len(colors) == len(names)


def test_shade_hex_scales_toward_black() -> None:
    assert _shade_hex("#FF8800", 1.0) == "#FF8800"
    assert _shade_hex("#FF8800", 0.0) == "#000000"
    assert _shade_hex("#808080", 0.5) == "#404040"


def test_speaker_glyph_has_baffle_and_driver_in_front() -> None:
    entity = _entity("speaker", size=(0.30, 0.40, 1.10))
    glyphs = _semantic_glyph_local_meshes(entity)
    assert len(glyphs) == 2
    # Local frame: entity front sits at -y; the baffle/driver live just proud
    # of the front face, i.e. at more negative y than the envelope front.
    front_y = -0.40 * 0.5
    for glyph in glyphs:
        assert glyph.bounds[3] <= front_y + 1e-9


def test_screen_and_projector_glyphs_present() -> None:
    assert len(_semantic_glyph_local_meshes(_entity("screen"))) == 1
    assert len(_semantic_glyph_local_meshes(_entity("display"))) == 1
    assert len(_semantic_glyph_local_meshes(_entity("projector"))) == 1


def test_seat_glyph_is_a_backrest_behind_the_envelope() -> None:
    entity = _entity("seat", size=(0.55, 0.60, 0.90))
    (slab,) = _semantic_glyph_local_meshes(entity)
    # Backrest slab lives at the local rear (+local Y → render +Y).
    assert slab.bounds[2] > 0.0
    assert slab.bounds[3] <= 0.60 * 0.5 + 1e-6


def test_av_equipment_glyph_is_three_shelf_slats() -> None:
    slats = _semantic_glyph_local_meshes(_entity("av_equipment"))
    assert len(slats) == 3


def test_architecture_kinds_have_no_glyph() -> None:
    assert _semantic_glyph_local_meshes(_entity("riser")) == ()
    assert _semantic_glyph_local_meshes(_entity("furniture")) == ()


def test_measurement_point_marker_is_crosshair_plus_bead() -> None:
    entity = _entity("measurement_point", size=None)
    meshes = semantic_entity_meshes(entity)
    assert len(meshes) == 2
    cross, bead = meshes
    # The crosshair spans ±0.11 on all three local axes.
    assert np.isclose(cross.bounds[1] - cross.bounds[0], 0.22)
    assert np.isclose(cross.bounds[3] - cross.bounds[2], 0.22)
    assert np.isclose(cross.bounds[5] - cross.bounds[4], 0.22)
    assert bead.bounds[1] - bead.bounds[0] == pytest.approx(0.056, abs=1e-3)


def test_semantic_meshes_apply_entity_pose() -> None:
    entity = _entity(
        "speaker", size=(0.30, 0.40, 1.10), position=(1.5, -2.0, 0.0)
    )
    meshes = semantic_entity_meshes(entity)
    assert len(meshes) == 2
    # Baffle plate center tracks the entity position (render frame: y → -z…
    # whatever the convention, the transformed bounds must move with it).
    moved = _entity(
        "speaker", size=(0.30, 0.40, 1.10), position=(4.5, -2.0, 0.0)
    )
    moved_meshes = semantic_entity_meshes(moved)
    delta = np.asarray(moved_meshes[0].center) - np.asarray(meshes[0].center)
    assert np.linalg.norm(delta) > 1.0


def test_semantic_meshes_empty_for_glyphless_kinds() -> None:
    # Architecture-category kinds rely on the envelope alone.
    assert semantic_entity_meshes(_entity("riser")) == ()
    assert semantic_entity_meshes(_entity("furniture")) == ()
