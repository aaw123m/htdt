"""#1008: read-only 3D fabrication preview for sealed packages (REV74).

Honesty contract under test: only issued/sealed
``TreatmentFabricationPackage`` objects resolve; the QRD well order/depth
is drawn from ``well_table`` 1:1; selection highlights are keyed to the
exact cut-list entry with material/tolerance/quantity; mount orientation
and clearance draw as REFERENCE geometry only while a canonical
placement binds the definition to the current head; unsupported
families fall back to the table view with no forced 3D; the export
report shares the preview's numbering/dimensions by construction.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_acoustic_treatment import (
    TreatmentCoverage,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_scene import Position3
from htdt.cad_treatment_fabrication import (
    FabricationSpec,
    FabricationToleranceProfile,
    generate_panel_fabrication,
    generate_qrd_fabrication,
)
from htdt.fabrication_preview import (
    UNSPECIFIED,
    FabricationPreviewController,
    build_fabrication_report,
)

from test_treatment_boundary_overlay import (
    _definition,
    _fixture,
    _second_revision_and_compiled,
)


_NOW = '2026-10-09T00:00:00Z'


def _spec(**overrides):
    kwargs = dict(
        spec_version='spec-1',
        panel_count=2,
        kerf_mm=0.5,
        tolerances=FabricationToleranceProfile(
            overall_dimension_mm=0.5,
        ),
        material_ref=None,
    )
    kwargs.update(overrides)
    return FabricationSpec(**kwargs)


def _panel_package(fixture, suffix=''):
    definition, evidence = _definition('geometric', suffix)
    repository = fixture['treatment_repository']
    for item in evidence:
        repository.save_evidence(item)
    definition = repository.save_definition(definition)
    package = generate_panel_fabrication(
        definition, _spec(), created_at_utc=_NOW
    )
    return definition, package


def _qrd_definition():
    # A diffuser-flavoured variant of the shared fixture definition;
    # it is never persisted, so authority validation does not apply.
    definition, _evidence = _definition('geometric', '-qrd')
    return definition.model_copy(
        update={'treatment_type': 'diffuser_scattering_element'}
    )


def _qrd_package():
    return generate_qrd_fabrication(
        _qrd_definition(),
        _spec(
            tolerances=FabricationToleranceProfile(
                overall_dimension_mm=0.5,
                well_depth_mm=0.25,
            )
        ),
        qrd_prime=7,
        design_frequency_hz=1000.0,
        well_width_m=0.05,
        fin_thickness_m=0.005,
        periods=1,
        back_thickness_m=0.012,
        created_at_utc=_NOW,
    )


def _controller(fixture):
    return FabricationPreviewController(
        fixture['scene_repository'],
        fixture['treatment_repository'],
        fixture['revision'].document.document_id,
    )


def scene_anchor(scene):
    return scene.anchor


def _project(center, anchor) -> float:
    """Center position measured along the anchor's stack axis."""

    return sum(
        (center[i] - anchor.origin[i]) * anchor.stack_axis[i]
        for i in range(3)
    )


def _place(
    fixture,
    definition,
    *,
    instance_id: str,
    position: Position3,
    width: float,
    height: float,
    installed: bool = False,
):
    repository = fixture['treatment_repository']
    proposed = build_treatment_placement(
        definition=definition,
        revision=fixture['revision'],
        instance_id=instance_id,
        position=position,
        coverage=TreatmentCoverage(width_m=width, height_m=height),
        host_surface_id=fixture['surface_id'],
    )
    repository.save_placement(proposed)
    if not installed:
        return proposed
    placed = revise_treatment_placement(
        proposed, revision=fixture['revision'], lifecycle='installed'
    )
    repository.save_placement(placed)
    return placed


def test_panel_layers_stack_with_derived_air_gap(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    scene = controller.resolve()
    assert scene is not None and scene.supported
    # Air gap = overall_depth − Σ part depth: derived, honest, index 0,
    # never a package part.
    gap = package.overall_depth_m - sum(
        p.finished_depth_m for p in package.parts
    )
    derived = [p for p in scene.parts if p.derived]
    assert len(derived) == 1
    assert derived[0].index == 0 and derived[0].material_label == UNSPECIFIED
    assert derived[0].dims_whs[2] == pytest.approx(gap)
    # Every package part draws in package order with its P-number.
    drawn = [p for p in scene.parts if not p.derived]
    assert [p.index for p in drawn] == list(
        range(1, len(package.parts) + 1)
    )
    first = drawn[0]
    part = package.parts[0]
    assert first.part_id == part.part_id == 'layer-core'
    assert first.dims_whs == pytest.approx(
        (
            part.finished_width_m,
            part.finished_height_m,
            part.finished_depth_m,
        )
    )
    assert first.label.startswith(f'P1 {part.part_id}')
    # No canonical placement → neutral anchor, no reference geometry.
    assert scene.anchor.source == 'neutral'
    assert scene.reference is None
    # Stacked centers advance along the stack axis (neutral = -Y).
    assert len(drawn) == 1
    stack_pos = (
        scene.anchor.origin[1] - gap - part.finished_depth_m / 2
    )
    assert first.center[1] == pytest.approx(stack_pos)
    for line in scene.viewport_lines:
        assert line.isascii()
    assert 'DESIGN VALUES ONLY' in '\n'.join(scene.viewport_lines)


def test_qrd_wells_and_fins_match_well_table_1to1(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    package = _qrd_package()
    controller = _controller(fixture)
    controller.arm(package)
    scene = controller.resolve()
    assert scene is not None and scene.supported
    # 7 wells, ordered, depths straight from well_table — 1:1 labels.
    labels = {w.well_index: w.depth_m for w in scene.well_labels}
    assert labels == {
        row.well_index: float(row.depth_m) for row in package.well_table
    }
    assert [w.well_index for w in scene.well_labels] == list(range(7))
    # Backing + fin instances; fin count = well_count + 1 positions.
    fins = [p for p in scene.parts if p.part_kind == 'well_fin']
    backing = [p for p in scene.parts if p.part_kind == 'backing_panel']
    assert len(backing) == 1 and backing[0].index == 1
    # Well 0 has residue 0 → depth 0; the edge fin at j=0 has no part
    # (the generator never issues a zero-depth fin) so 7 fins draw.
    assert len(fins) == 7
    # Fin j depth = max adjacent wells, matched to its depth-grouped part.
    depths = [float(r.depth_m) for r in package.well_table]
    for box in fins:
        j = int(box.part_id.rsplit('@', 1)[-1])
        left = depths[j - 1] if j > 0 else depths[0]
        right = depths[j] if j < len(depths) else depths[-1]
        assert box.dims_whs[2] == pytest.approx(max(left, right))
    # Shared depth groups reuse the part's P-number — export parity.
    indices = {box.index for box in fins}
    assert indices == {
        i + 1 for i in range(len(package.parts)) if i > 0
    }


def test_unsupported_family_falls_back_without_parts(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    package = _qrd_package().model_copy(
        update={'fabrication_family': 'qrd_2d'}
    )
    controller = _controller(fixture)
    controller.arm(package)
    scene = controller.resolve()
    assert scene is not None
    assert not scene.supported
    assert scene.parts == () and scene.well_labels == ()
    assert scene.reference is None
    assert 'UNSUPPORTED' in '\n'.join(scene.viewport_lines)
    assert 'qrd_2d' in (scene.fallback_reason or '')


def test_canonical_placement_draws_reference_geometry(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    definition, package = _panel_package(fixture)
    placement = _place(
        fixture,
        definition,
        instance_id='panel-ref',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=1.0,
        height=1.0,
        installed=True,
    )
    controller = _controller(fixture)
    controller.arm(package, placement_instance_id=placement.instance_id)
    scene = controller.resolve()
    assert scene is not None and scene.supported
    assert scene.anchor.source == 'placement'
    assert scene.anchor.placement_instance_id == 'panel-ref'
    reference = scene.reference
    assert reference is not None
    # Mount ring: authored 1.0×1.0 rect projected on the top face z=1.
    assert len(reference.mount_ring) == 4
    for x, y, z in reference.mount_ring:
        assert z == pytest.approx(1.0, abs=1e-9)
        assert 0.0 - 1e-9 <= x <= 1.0 + 1e-9
        assert 0.0 - 1e-9 <= y <= 1.0 + 1e-9
    # Clearance envelope = authored rect extruded by overall_depth
    # along the stack axis — reference only, never a boundary input.
    assert reference.clearance_dims == pytest.approx(
        (1.0, 1.0, package.overall_depth_m)
    )
    assert reference.clearance_center[2] == pytest.approx(
        1.0 + package.overall_depth_m / 2.0
    )
    text = '\n'.join(scene.viewport_lines)
    assert 'REF mount+clearance' in text
    assert 'panel-ref' in text
    assert any('取付向き' in line for line in scene.summary_ja)


def test_lapsed_placement_falls_back_to_neutral(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    definition, package = _panel_package(fixture)
    placement = _place(
        fixture,
        definition,
        instance_id='panel-stale',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        width=0.5,
        height=0.5,
    )
    controller = _controller(fixture)
    controller.arm(package, placement_instance_id=placement.instance_id)
    _second_revision_and_compiled(fixture)
    scene = controller.resolve()
    assert scene is not None
    assert scene.anchor.source == 'neutral'
    assert scene.reference is None
    assert any('not canonical' in n for n in scene.notices)


def test_selection_resolves_cut_list_detail(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    controller.select('absorber-core')  # cut_group, not part_id
    scene = controller.resolve()
    assert scene is not None
    selected = [p for p in scene.parts if p.selected]
    assert len(selected) == 1
    assert selected[0].part_id == 'layer-core'
    detail = '\n'.join(scene.selected_detail)
    assert 'layer-core' in detail and 'absorber-core' in detail
    assert 'material fixture core' in detail
    assert '×2' in detail  # panel_count from the spec
    # Panel packages carry no cut tolerance — the preview must say
    # unspecified rather than invent one (QRD fin groups carry the
    # well_depth tolerance; see the export test).
    assert 'unspecified mm' in detail
    # Selecting a missing key is explicit, not silent.
    controller.select('no-such-part')
    scene = controller.resolve()
    assert scene is not None
    assert any(
        'matches no drawn part' in n for n in scene.notices
    )


def test_export_report_matches_preview_numbering(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    report = build_fabrication_report(package)
    assert package.package_sha256 in report
    assert package.definition_sha256 in report
    assert 'spec v' not in report  # uses 'spec_version' label instead
    for index, part in enumerate(package.parts, start=1):
        assert f'| P{index} | {part.part_id} |' in report
    for entry in package.cut_list:
        assert entry.cut_group in report
        if entry.tolerance_mm is None:
            continue
        assert f'{entry.tolerance_mm:g}' in report
    assert 'unspecified' in report
    assert '設計値由来' in report
    qrd = _qrd_package()
    qrd_report = build_fabrication_report(qrd)
    assert '## Well table' in qrd_report
    for row in qrd.well_table:
        assert f'| {row.well_index} |' in qrd_report
        assert f'{row.depth_m * 1000.0:.0f} |' in qrd_report
    # Kerf/depth tolerances show exactly where the package grounds them:
    # fin cut groups carry well_depth_mm, the backing carries overall.
    for entry in qrd.cut_list:
        if entry.cut_group.startswith('fin-'):
            assert entry.tolerance_mm == pytest.approx(0.25)
        else:
            assert entry.tolerance_mm == pytest.approx(0.5)
        assert f'{entry.tolerance_mm:g}' in qrd_report


def test_view_controls_section_and_explode(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    flat = controller.resolve()
    assert flat is not None and flat.section_fraction is None
    flat_proj = [
        _project(p.center, scene_anchor(flat)) for p in flat.parts
        if not p.derived
    ]
    controller.set_view(section_fraction=0.5, exploded_fraction=1.0)
    scene = controller.resolve()
    assert scene is not None
    assert scene.section_fraction == pytest.approx(0.5)
    assert scene.section_position is not None
    # Section plane sits at the width midpoint of the anchor frame.
    assert scene.section_position[0] == pytest.approx(
        scene.anchor.origin[0]
    )
    moved = [
        _project(p.center, scene_anchor(scene)) for p in scene.parts
        if not p.derived
    ]
    assert all(m > f for m, f in zip(moved, flat_proj))


def test_disarm_returns_none_and_arm_revalidates(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    assert controller.resolve() is not None
    controller.disarm()
    assert controller.resolve() is None
    # Re-arm with a re-issued package draws the NEW sha, never the old.
    other = generate_panel_fabrication(
        _definition_obj,
        _spec(
            spec_version='spec-2',
            supersedes_package_sha256=package.package_sha256,
        ),
        created_at_utc=_NOW,
    )
    assert other.package_sha256 != package.package_sha256
    controller.arm(other)
    scene = controller.resolve()
    assert scene is not None
    assert scene.package.package_sha256 == other.package_sha256
    assert scene.package.supersedes_package_sha256 == (
        package.package_sha256
    )


# -- viewport + dialog (offscreen) -------------------------------------------


def _offscreen_scene(tmp_path: Path):
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    return controller.resolve()


def test_viewport_actors_named_unpickable_and_swept(
    tmp_path: Path,
) -> None:
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    scene = _offscreen_scene(tmp_path)
    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_fabrication_preview(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('fabrication-')
    ]
    assert any('fabrication-part-1-layer-core' in n for n in names)
    assert any('fabrication-part-0-air-gap' in n for n in names)
    assert 'fabrication-status' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        if name != 'fabrication-status':
            assert not actor.GetPickable()
    assert 'fabrication-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES
    viewport._remove_overlay_actors()
    assert not [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('fabrication-')
    ]


def test_viewport_section_draws_cut_actors(tmp_path: Path) -> None:
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    _definition_obj, package = _panel_package(fixture)
    controller = _controller(fixture)
    controller.arm(package)
    controller.set_view(section_fraction=0.5, exploded_fraction=0.0)
    scene = controller.resolve()
    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_fabrication_preview(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('fabrication-section-')
    ]
    assert names
    # Reference-geometry naming only appears with a canonical placement.
    assert not [
        n
        for n in viewport.plotter.renderer.actors
        if isinstance(n, str) and n.startswith('fabrication-ref-')
    ]
    viewport.clear_fabrication_preview()
    assert not [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('fabrication-')
    ]


class _RecordingHost:
    def __init__(self) -> None:
        self.calls: list = []

    def show_fabrication_preview(self, package, *, placement_instance_id):
        self.calls.append(('show', package, placement_instance_id))

    def update_fabrication_preview_view(
        self, *, section_fraction, exploded_fraction
    ):
        self.calls.append(('view', section_fraction, exploded_fraction))

    def select_fabrication_part(self, key):
        self.calls.append(('select', key))

    def clear_fabrication_preview(self):
        self.calls.append(('clear',))


class _DialogController:
    def __init__(self, fixture) -> None:
        self.repository = fixture['scene_repository']
        self.treatment_repository = fixture['treatment_repository']
        self.document_id = fixture['revision'].document.document_id


def test_dialog_issues_and_reports_parity(tmp_path: Path) -> None:
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from htdt.room_fabrication_panel import FabricationPreviewDialog

    app = QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    definition, _pkg = _panel_package(fixture)
    host = _RecordingHost()
    dialog = FabricationPreviewDialog(_DialogController(fixture), host=host)
    try:
        assert dialog.definition_combo.count() == 1
        assert dialog.issue_button.isEnabled()
        dialog.issue()
        package = dialog._package
        assert package is not None
        # Issuance arms the preview through the host facade.
        assert host.calls[0][0] == 'show'
        assert host.calls[0][1].package_sha256 == package.package_sha256
        assert host.calls[0][2] is None  # no placement pinned
        # Tables mirror the sealed package 1:1.
        assert dialog.parts_table.rowCount() == len(package.parts)
        assert dialog.cut_table.rowCount() == len(package.cut_list)
        assert not dialog.well_table.isVisible()
        # Part row carries the preview's P-number + cut-group key.
        first_id = dialog.parts_table.item(0, 1).text()
        assert first_id == 'layer-core'
        assert dialog.parts_table.item(0, 5).text() == 'fixture core'
        report = dialog.report_text()
        assert f'| P1 | {first_id} |' in report
        # Selecting a row selects by part_id through to the host.
        dialog.parts_table.selectRow(0)
        app.processEvents()
        assert ('select', 'layer-core') in host.calls
        # Section/exploded sliders forward as fractions (None when off).
        dialog.section_enabled.setChecked(True)
        dialog.section_slider.setValue(40)
        dialog.exploded_slider.setValue(70)
        app.processEvents()
        assert ('view', 0.4, 0.7) in host.calls
        # Closing disarms the overlay — actor cleanup rides the same path.
        dialog.close()
        assert ('clear',) in host.calls
    finally:
        dialog.close()
        dialog.deleteLater()


def test_dialog_rejects_unsupported_definition_type(
    tmp_path: Path,
) -> None:
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from htdt.room_fabrication_panel import FabricationPreviewDialog

    QApplication.instance() or QApplication(['htdt-test'])
    fixture = _fixture(tmp_path)
    _panel_package(fixture)  # registers a porous definition
    dialog = FabricationPreviewDialog(
        _DialogController(fixture), host=_RecordingHost()
    )
    try:
        # porous_absorber → panel path; qrd group hidden.
        assert dialog.qrd_box.isHidden()
        assert dialog.issue_button.isEnabled()
    finally:
        dialog.deleteLater()
