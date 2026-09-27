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
    RoomViewport3D,
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


class _CountingPlotter:
    def __init__(self) -> None:
        self.renders = 0

    def render(self) -> None:
        self.renders += 1


def _bare_viewport():
    # Method-semantics harness: bypass the real plotter so the deferred
    # render bookkeeping is exercised without a GPU draw.
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["htdt-test"])
    vp = RoomViewport3D()
    vp.plotter = _CountingPlotter()
    return vp


def test_deferred_render_coalesces_to_one_draw() -> None:
    vp = _bare_viewport()
    with vp.deferred_render():
        vp._render()
        vp._render()
        vp._render()
    assert vp.plotter.renders == 1


def test_deferred_render_nested_scope_flushes_once() -> None:
    vp = _bare_viewport()
    with vp.deferred_render():
        with vp.deferred_render():
            vp._render()
        assert vp.plotter.renders == 0
        vp._render()
    assert vp.plotter.renders == 1


def test_deferred_render_flushes_pending_on_exception() -> None:
    vp = _bare_viewport()
    with pytest.raises(RuntimeError):
        with vp.deferred_render():
            vp._render()
            raise RuntimeError("build aborted")
    assert vp.plotter.renders == 1
    assert vp._defer_render_depth == 0
    assert vp._render_pending is False


def test_render_unbatched_still_draws_immediately() -> None:
    vp = _bare_viewport()
    vp._render()
    vp._render()
    assert vp.plotter.renders == 2


class _MeshRecordingPlotter:
    """GPU-free plotter stand-in that captures ``add_mesh`` calls."""

    def __init__(self) -> None:
        self.renders = 0
        self.meshes: list = []
        self.kwargs: list[dict] = []

    def add_mesh(self, mesh, **kwargs):
        self.meshes.append(mesh)
        self.kwargs.append(kwargs)
        return object()

    def render(self) -> None:
        self.renders += 1


def test_render_underlays_registers_tcoords_and_actors() -> None:
    # Regression for the pyvista-0.49 pin: ``active_t_coords`` was removed
    # upstream and assigning it raised PyVistaAttributeError the moment a
    # raster underlay was imported. The supported API must leave a real VTK
    # TCoords array on the quad for ``texture=`` mapping to consume.
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import UnderlayRenderItem

    QApplication.instance() or QApplication(["htdt-test"])
    vp = RoomViewport3D()
    vp.plotter = _MeshRecordingPlotter()

    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[..., 0] = 128
    vp.set_aux_render_state(
        underlays=(
            UnderlayRenderItem(
                underlay_id="u-1",
                name="floor plan",
                quad_domain=((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 3.0, 0.0), (0.0, 3.0, 0.0)),
                image=image,
                segments_domain=(((0.0, 0.0, 0.01), (1.0, 1.0, 0.01)),),
                opacity=0.5,
                elevation_m=0.0,
            ),
        )
    )
    vp._render_underlays()

    assert len(vp.plotter.meshes) == 2
    quad = vp.plotter.meshes[0]
    assert quad.GetPointData().GetTCoords() is not None
    assert quad.active_texture_coordinates.shape == (4, 2)
    assert vp.plotter.kwargs[0]["texture"] is not None
    assert vp.plotter.kwargs[0]["name"] == "underlay-u-1"
    assert set(vp._actor_underlay_ids.values()) == {"u-1"}
