"""#1003 screen quality map — ANSI 9-point luminance/chromaticity/focus
evidence overlaid on the real image surface in the Room 3D viewport."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_direct_view import (
    DisplayGeometryBinding,
    build_direct_view_geometry_request,
    evaluate_direct_view_geometry,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    domain_to_render,
)
from htdt.cad_spatial_image_authority import (
    CadMeasurementViewpoint,
    CadProjectorOpticalState,
    CadScreenStateSnapshot,
    CadSpatialObservation,
    CadSpatialSamplePoint,
    ansi_9_point_grid,
    build_spatial_derived_map,
    build_spatial_measurement_plan,
    build_spatial_measurement_set,
)
from htdt.cad_spatial_image_repository import CadSpatialImageRepository
from htdt.cad_video_geometry import (
    PROJECTOR_SPEC_EVIDENCED_FIELDS,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecificationProvenance,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    build_projector_spec_document_evidence,
    build_projector_spec_field_assertions,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
    projector_spec_optical_values,
)
from htdt.managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore
from htdt.room_screen_quality_map import (
    HEATMAP_ARTIFACT_KIND,
    QualityMapSelection,
    ScreenQualityMapController,
    resolve_screen_quality_map,
    scale_color_for,
)

DOC = 'doc-issue-1003'
T0 = '2026-10-08T00:00:00+00:00'
T1 = '2026-10-08T01:00:00+00:00'
SHA_A = 'a' * 64


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


def _screen_entity(**overrides) -> SceneEntity:
    kwargs = dict(
        entity_id='screen-main',
        kind='screen',
        name='Main Screen',
        position=Position3(x_m=3.0, y_m=0.2, z_m=1.5),
        size_m=Size3(x_m=3.2, y_m=0.1, z_m=1.8),
    )
    kwargs.update(overrides)
    return SceneEntity(**kwargs)


def _projector_entity() -> SceneEntity:
    return SceneEntity(
        entity_id='projector-main',
        kind='projector',
        name='Projector',
        position=Position3(x_m=3.0, y_m=4.3, z_m=2.25),
        size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
    )


def _seat_entity() -> SceneEntity:
    return SceneEntity(
        entity_id='seat-front',
        kind='seat',
        name='Front',
        position=Position3(x_m=3.0, y_m=2.2, z_m=0.5),
        size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
    )


def _display_entity(**overrides) -> SceneEntity:
    kwargs = dict(
        entity_id='display-main',
        kind='display',
        name='Display',
        position=Position3(x_m=3.0, y_m=0.2, z_m=1.5),
        size_m=Size3(x_m=2.2, y_m=0.08, z_m=1.25),
    )
    kwargs.update(overrides)
    return SceneEntity(**kwargs)


def _scene(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id=DOC,
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=3.0),
        entities=entities,
    )


def _repos(tmp_path: Path, document=None):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        document or _scene(_screen_entity(), _projector_entity(), _seat_entity()),
        parent_revision_id=None,
    ).revision
    spatial = CadSpatialImageRepository(scene_repository)
    assets = ManagedAssetStore(tmp_path / MANAGED_ASSETS_DIRNAME)
    return scene_repository, spatial, assets, revision


def _spec_optical_values() -> dict:
    return projector_spec_optical_values(
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=3.0,
        optical_zoom_ratio=1.4,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-0.25, maximum_fraction=0.25
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-0.65, maximum_fraction=0.65
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _spec():
    optical = _spec_optical_values()
    source_bytes = json.dumps(
        {
            'schema': 'example.projector-spec-sheet.v1',
            'manufacturer': 'Example Projection Co.',
            'model': 'P',
            'document_version': '2026.1',
            'optical': optical,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    evidence = build_projector_spec_document_evidence(
        evidence_kind='manufacturer_document',
        manufacturer='Example Projection Co.',
        model='P',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=sha256(source_bytes).hexdigest(),
        extractor_id='htdt-test-spec-table',
        extractor_version='1.0',
        field_assertions=build_projector_spec_field_assertions(
            optical_values=optical,
            field_locators={
                field: f'datasheet p.4, optical table, row "{field}"'
                for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
            },
        ),
    )
    provenance = ProjectorSpecificationProvenance(
        source_kind='manufacturer',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=evidence.source_sha256,
        evidence=evidence.ref(),
    )
    return build_projector_specification(
        specification_id='spec-p',
        version='2026.1',
        manufacturer='Example Projection Co.',
        model='P',
        provenance=provenance,
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=3.0,
        optical_zoom_ratio=1.4,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-0.25, maximum_fraction=0.25
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-0.65, maximum_fraction=0.65
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _policy() -> VideoGeometryPolicy:
    return VideoGeometryPolicy(
        sightline_samples=(
            SightlineSample(
                sample_id='center', horizontal_fraction=0.5, vertical_fraction=0.5
            ),
        ),
        sightline_clearance_m=0.1,
        riser_support_tolerance_m=0.05,
        max_optical_axis_deviation_deg=45.0,
        collision_clearance_m=0.05,
    )


def _seat_binding() -> SeatGeometryBinding:
    return SeatGeometryBinding(
        geometry_source='manual',
        entity_id='seat-front',
        row_id='row-1',
        eye_reference_offset_local_m=Offset3(z_m=0.65),
        head_center_offset_local_m=Offset3(z_m=0.65),
        head_radius_m=0.16,
    )


def _evaluate(revision, spec, **screen_overrides):
    request = build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=spec,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=screen_overrides.get('width', 8.0 / 3.0),
            visible_height_m=screen_overrides.get('height', 1.5),
            frame_clearance_m=0.05,
        ),
        seats=(_seat_binding(),),
        policy=_policy(),
        collision_entity_ids=(),
    )
    return evaluate_video_geometry(
        baseline=revision,
        variant=None,
        projector_specification=spec,
        request=request,
    )


def _evaluate_direct_view(revision, width=2.0, height=1.125):
    request = build_direct_view_geometry_request(
        display=DisplayGeometryBinding(
            entity_id='display-main',
            visible_width_m=width,
            visible_height_m=height,
            frame_clearance_m=0.02,
        ),
        seats=(_seat_binding(),),
        policy=_policy(),
        collision_entity_ids=(),
    )
    return evaluate_direct_view_geometry(
        baseline=revision, variant=None, request=request
    )


def _plan(document_id=DOC, **overrides):
    kwargs = dict(
        document_id=document_id,
        layout='ansi_9_point',
        points=ansi_9_point_grid(),
        quantities=('white_luminance',),
        viewpoints=(CadMeasurementViewpoint(viewpoint_id='seat-primary'),),
        screen_state=CadScreenStateSnapshot(
            screen_ref=_ref('screen', 'screen-main'),
        ),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_spatial_measurement_plan(**kwargs)


def _lum_observations(plan, *, value=80.0, viewpoint='seat-primary', quantity='white_luminance'):
    return tuple(
        CadSpatialObservation(
            point_id=point.point_id,
            viewpoint_id=viewpoint,
            quantity=quantity,
            value=value + idx,
            units='nits',
            stimulus_profile='sdr_reference',
            observed_at_utc=T1,
        )
        for idx, point in enumerate(plan.points)
    )


def _save_set(spatial, plan, observations, document_id=DOC, **overrides):
    kwargs = dict(
        document_id=document_id,
        plan=plan,
        observations=observations,
        evidence_kind='field_measured',
        stimulus_profile='sdr_reference',
        declared_at_utc=T1,
    )
    kwargs.update(overrides)
    measurement_set = build_spatial_measurement_set(**kwargs)
    spatial.save_plan(plan)
    spatial.save_measurement_set(measurement_set)
    return measurement_set


def test_ansi9_maps_exact_image_plane_positions(tmp_path):
    """Every ANSI-9 point lands on the evaluated quad at its declared
    fractions — y_fraction measured from the top edge."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository,
        spatial,
        DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene is not None
    assert not scene.read_only
    assert len(scene.markers) == 9
    corners = evaluation.projection.image_plane_corners
    # center point = bilinear center of the quad.
    center = next(m for m in scene.markers if m.point_id == 'center')
    cx = sum(c.x_m for c in corners) / 4.0
    cz = sum(c.z_m for c in corners) / 4.0
    assert center.domain_xyz == pytest.approx((cx, corners[0].y_m, cz))
    # top-left (x=1/6, y=1/6-from-top): sits left+up of center.
    top_left = next(m for m in scene.markers if m.point_id == 'top-left')
    width = corners[1].x_m - corners[0].x_m
    height = corners[3].z_m - corners[0].z_m
    assert top_left.domain_xyz[0] == pytest.approx(
        corners[0].x_m + (1.0 / 6.0) * width
    )
    assert top_left.domain_xyz[2] == pytest.approx(
        corners[0].z_m + (5.0 / 6.0) * height
    )
    bottom_right = next(
        m for m in scene.markers if m.point_id == 'bottom-right'
    )
    assert bottom_right.domain_xyz[0] == pytest.approx(
        corners[0].x_m + (5.0 / 6.0) * width
    )
    assert bottom_right.domain_xyz[2] == pytest.approx(
        corners[0].z_m + (1.0 / 6.0) * height
    )
    assert all(m.state == 'measured' for m in scene.markers)


def test_center_only_layout_and_partial_coverage(tmp_path):
    """center_only draws one point; sparse sets mark unmeasured points."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(
        layout='center_only',
        points=(
            CadSpatialSamplePoint(
                point_id='center', role='center',
                x_fraction=0.5, y_fraction=0.5,
            ),
        ),
    )
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert len(scene.markers) == 1
    assert scene.markers[0].state == 'measured'

    # Partial corners: only corners measured on a 9-point plan.
    plan9 = _plan()
    partial = _save_set(
        spatial,
        plan9,
        tuple(
            CadSpatialObservation(
                point_id=p.point_id,
                viewpoint_id='seat-primary',
                quantity='white_luminance',
                value=50.0,
                units='nits',
                stimulus_profile='sdr_reference',
                observed_at_utc=T1,
            )
            for p in plan9.points
            if p.role == 'corner'
        ),
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=partial.set_id),
        evaluation,
    )
    assert sum(1 for m in scene.markers if m.state == 'measured') == 4
    assert sum(1 for m in scene.markers if m.state == 'unmeasured') == 5
    coverage = next(
        c for c in scene.coverage if c.quantity == 'white_luminance'
    )
    assert coverage.observed_points == 4
    assert coverage.verdict_state in (
        'insufficient_coverage',
        'criterion_unbound',
    )


def test_quantity_dropdown_only_offers_observed_quantities(tmp_path):
    """The quantity selector lists only quantities the set actually has."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(quantities=('white_luminance', 'focus_sharpness'))
    observations = _lum_observations(plan) + (
        CadSpatialObservation(
            point_id='center',
            viewpoint_id='seat-primary',
            quantity='focus_sharpness',
            value=87.0,
            units='%',
            stimulus_profile='unknown',
            observed_at_utc=T1,
        ),
    )
    measurement_set = _save_set(spatial, plan, observations)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene is not None
    assert set(scene.available_quantities) == {
        'white_luminance', 'focus_sharpness',
    }
    # Selecting focus_sharpness binds that quantity's units + values.
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(
            set_id=measurement_set.set_id, quantity='focus_sharpness'
        ),
        evaluation,
    )
    assert scene.quantity == 'focus_sharpness'
    measured = [m for m in scene.markers if m.state == 'measured']
    assert len(measured) == 1
    assert measured[0].observation.value == 87.0
    # One point's value is never reused for another point.
    assert all(
        m.state != 'measured' or m.point_id == 'center'
        for m in scene.markers
    )


def test_chromaticity_never_merges_into_luminance_scalar(tmp_path):
    """white_chromaticity renders the measured x,y pair as its own channel."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(quantities=('white_chromaticity',))
    observations = tuple(
        CadSpatialObservation(
            point_id=point.point_id,
            viewpoint_id='seat-primary',
            quantity='white_chromaticity',
            chromaticity_x=0.31 + (0.008 * idx),
            chromaticity_y=0.33,
            stimulus_profile='sdr_reference',
            observed_at_utc=T1,
        )
        for idx, point in enumerate(plan.points)
    )
    measurement_set = _save_set(spatial, plan, observations)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene.quantity == 'white_chromaticity'
    assert all(m.state == 'measured' for m in scene.markers)
    colors = {m.fill_color for m in scene.markers}
    assert len(colors) > 1  # real chromaticity values rendered, not a scalar
    first = scene.markers[0].observation
    assert first.chromaticity_x is not None
    assert first.value is None  # never folded into a scalar


def test_offaxis_and_rotated_screen_follow_evaluated_quad(tmp_path):
    """A rotated screen still maps fractions onto the real quad — off-axis
    placement is handled because corners are world-space."""
    rotated = _screen_entity(
        orientation=Quaternion4(w=0.70710678, x=0.0, y=0.0, z=0.70710678),
    )
    scene_repository, spatial, _assets, revision = _repos(
        tmp_path,
        document=_scene(rotated, _projector_entity(), _seat_entity()),
    )
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene is not None
    corners = evaluation.projection.image_plane_corners
    top_left = next(m for m in scene.markers if m.point_id == 'top-left')
    # For a 90° yaw rotation the aperture right axis points +Y: the marker
    # must land on the rotated quad, not the identity quad.
    expected_x = corners[0].x_m + (1.0 / 6.0) * (
        corners[1].x_m - corners[0].x_m
    ) + (5.0 / 6.0) * (corners[3].x_m - corners[0].x_m)
    expected_y = corners[0].y_m + (1.0 / 6.0) * (
        corners[1].y_m - corners[0].y_m
    ) + (5.0 / 6.0) * (corners[3].y_m - corners[0].y_m)
    expected_z = corners[0].z_m + (1.0 / 6.0) * (
        corners[1].z_m - corners[0].z_m
    ) + (5.0 / 6.0) * (corners[3].z_m - corners[0].z_m)
    assert top_left.domain_xyz == pytest.approx(
        (expected_x, expected_y, expected_z)
    )


def test_portrait_direct_view_surface(tmp_path):
    """DirectView surface.image_plane_corners maps portrait layouts too."""
    document = _scene(
        _display_entity(),
        _seat_entity(),
        SceneEntity(
            entity_id='projector-main', kind='projector', name='P',
            position=Position3(x_m=3.0, y_m=4.3, z_m=2.25),
            size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
        ),
    )
    scene_repository, spatial, _assets, revision = _repos(
        tmp_path, document=document
    )
    evaluation = _evaluate_direct_view(revision, width=1.0, height=1.8)
    plan = _plan(
        screen_state=CadScreenStateSnapshot(
            screen_ref=_ref('screen', 'display-main'),
        ),
    )
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene is not None
    assert scene.target_kind == 'direct_view'
    corners = evaluation.surface.image_plane_corners
    top_center = next(m for m in scene.markers if m.point_id == 'top-center')
    assert top_center.domain_xyz[2] == pytest.approx(
        corners[0].z_m + (5.0 / 6.0) * (corners[3].z_m - corners[0].z_m)
    )


def test_stale_evaluation_is_read_only(tmp_path):
    """A scene edit after evaluation clears markers — never stale paint."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    # Scene edit: move the screen -> new head revision.
    document = scene_repository.current_head(DOC).document
    moved = document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(update={'name': 'Screen moved'})
                if entity.entity_id == 'screen-main' else entity
                for entity in document.entities
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=revision.revision_id)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene.read_only
    assert not scene.markers
    assert 'stale' in scene.read_only_reason_viewport


def test_projector_spec_swap_marks_read_only(tmp_path):
    """A set bound to a different projector spec never masquerades as
    current evidence."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan),
        projector_state=CadProjectorOpticalState(
            projector_ref=_ref('projector_spec', 'spec-other', 'b' * 64),
            picture_mode='cinema',
        ),
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene.read_only
    assert 'projector' in scene.read_only_reason_viewport
    assert any(
        'プロジェクター' in line for line in scene.summary_ja
    )


def test_screen_swap_marks_read_only(tmp_path):
    """Plan-bound screen authority differs from the bound surface."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(
        screen_state=CadScreenStateSnapshot(
            screen_ref=_ref('screen', 'screen-other'),
        ),
    )
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene.read_only
    assert 'screen mismatch' in scene.read_only_reason_viewport


def test_viewpoint_selection_never_reuses_other_viewpoint(tmp_path):
    """A point unmeasured at viewpoint B draws unmeasured, even when
    viewpoint A has a reading."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(
        viewpoints=(
            CadMeasurementViewpoint(viewpoint_id='seat-primary'),
            CadMeasurementViewpoint(viewpoint_id='seat-rear'),
        ),
    )
    observations = _lum_observations(plan, viewpoint='seat-primary')
    measurement_set = _save_set(spatial, plan, observations)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(
            set_id=measurement_set.set_id, viewpoint_id='seat-rear'
        ),
        evaluation,
    )
    assert scene.viewpoint_id == 'seat-rear'
    assert all(m.state == 'unmeasured' for m in scene.markers)


def test_physical_xyz_mismatch_requires_manual_alignment(tmp_path):
    """Declared physical_xyz_m disagreeing with the surface position is
    flagged — never silently auto-corrected."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    points = tuple(
        CadSpatialSamplePoint(
            point_id=p.point_id,
            role=p.role,
            x_fraction=p.x_fraction,
            y_fraction=p.y_fraction,
            physical_xyz_m=(0.0, 0.0, 0.0) if p.point_id == 'center' else None,
        )
        for p in ansi_9_point_grid()
    )
    plan = _plan(points=points)
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    center = next(m for m in scene.markers if m.point_id == 'center')
    assert center.state == 'misaligned'
    assert center.physical_mismatch_m is not None
    assert center.physical_mismatch_m > 0.05
    others = [m for m in scene.markers if m.point_id != 'center']
    assert all(m.state == 'measured' for m in others)


def test_physical_xyz_within_tolerance_not_flagged(tmp_path):
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    corners = evaluation.projection.image_plane_corners
    cx = sum(c.x_m for c in corners) / 4.0
    cy = sum(c.y_m for c in corners) / 4.0
    cz = sum(c.z_m for c in corners) / 4.0
    points = tuple(
        CadSpatialSamplePoint(
            point_id=p.point_id, role=p.role,
            x_fraction=p.x_fraction, y_fraction=p.y_fraction,
            physical_xyz_m=(cx + 0.005, cy, cz) if p.point_id == 'center' else None,
        )
        for p in ansi_9_point_grid()
    )
    plan = _plan(points=points)
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    center = next(m for m in scene.markers if m.point_id == 'center')
    assert center.state == 'measured'


def test_no_evaluation_is_read_only(tmp_path):
    """Without a current video evaluation there is no surface to draw on."""
    scene_repository, spatial, _assets, _rev = _repos(tmp_path)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        None,
    )
    assert scene.read_only
    assert scene.surface_render_quad is None
    assert 'evaluation' in scene.read_only_reason_viewport


def test_heatmap_requires_verified_artifact(tmp_path):
    """A derived map only draws when its rendered artifact's sha verifies."""
    scene_repository, spatial, assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )

    payload = {
        'kind': HEATMAP_ARTIFACT_KIND,
        'quantity': 'white_luminance',
        'units': 'nits',
        'values': [
            [80.0, 81.0, None],
            [82.0, 83.0, 84.0],
        ],
    }
    raw = json.dumps(payload, sort_keys=True).encode('utf-8')
    digest = sha256(raw).hexdigest()
    assets.install(digest, raw)
    derived_map = build_spatial_derived_map(
        document_id=DOC,
        source_set=measurement_set,
        quantity='white_luminance',
        interpolation_algorithm='bilinear',
        algorithm_version='1.0',
        grid_resolution='3x2',
        rendered_artifact_ref=_ref('managed_asset', digest, digest),
        declared_at_utc=T1,
    )
    spatial.save_derived_map(derived_map)

    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(
            set_id=measurement_set.set_id, heatmap_enabled=True
        ),
        evaluation,
        asset_store=assets,
    )
    assert scene.heatmap is not None
    assert len(scene.heatmap.cells) == 5  # null cell stays transparent
    assert scene.heatmap.interpolation_algorithm == 'bilinear'

    # Tampered artifact -> no heatmap, explicit reason.
    bad = _save_set(
        spatial, plan,
        (
            CadSpatialObservation(
                point_id='center', viewpoint_id='seat-primary',
                quantity='white_luminance', value=99.0, units='nits',
                stimulus_profile='sdr_reference', observed_at_utc=T1,
            ),
        ),
    )
    bad_map = build_spatial_derived_map(
        document_id=DOC,
        source_set=bad,
        quantity='white_luminance',
        interpolation_algorithm='nearest',
        algorithm_version='1.0',
        grid_resolution='1x1',
        rendered_artifact_ref=_ref('managed_asset', 'c' * 64, 'c' * 64),
        declared_at_utc=T1,
    )
    spatial.save_derived_map(bad_map)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=bad.set_id, heatmap_enabled=True),
        evaluation,
        asset_store=assets,
    )
    assert scene.heatmap is None
    assert any('ヒートマップ非表示' in n for n in scene.notices)


def test_controller_re_resolves_on_new_head(tmp_path):
    """Cache key binds the live head — a scene edit re-resolves, never
    replays the superseded map."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan()
    measurement_set = _save_set(
        spatial, plan, _lum_observations(plan)
    )
    controller = ScreenQualityMapController(
        scene_repository, spatial, DOC
    )
    controller.select_set(measurement_set.set_id)
    first = controller.resolve(evaluation)
    assert first is not None and not first.read_only
    # Edit the scene -> new head; the same (stale) evaluation now resolves
    # read-only instead of painting stale geometry.
    document = scene_repository.current_head(DOC).document
    moved = document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(update={'name': 'Screen moved'})
                if entity.entity_id == 'screen-main' else entity
                for entity in document.entities
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=revision.revision_id)
    second = controller.resolve(evaluation)
    assert second is not None
    assert second.revision_id != first.revision_id
    assert second.read_only


def test_scale_is_absolute_not_normalized(tmp_path):
    """Same value -> same band color regardless of dataset range."""
    low = scale_color_for('white_luminance', 'nits', 20.0)
    assert low == scale_color_for('white_luminance', 'cd/m²', 20.0)
    assert low != scale_color_for('white_luminance', 'nits', 400.0)
    # Unknown unit -> no invented scale.
    assert scale_color_for('white_luminance', 'lumens', 20.0) is None


def test_unscaled_quantity_is_read_only_with_reason(tmp_path):
    """A quantity+unit with no absolute scale draws unscaled markers plus
    a stated reason — never guessed colors."""
    scene_repository, spatial, _assets, revision = _repos(tmp_path)
    spec = _spec()
    evaluation = _evaluate(revision, spec)
    plan = _plan(quantities=('project_defined',))
    observations = tuple(
        CadSpatialObservation(
            point_id=p.point_id,
            viewpoint_id='seat-primary',
            quantity='project_defined',
            value=1.0,
            units='custom-unit',
            stimulus_profile='unknown',
            observed_at_utc=T1,
        )
        for p in plan.points
    )
    measurement_set = _save_set(spatial, plan, observations)
    scene = resolve_screen_quality_map(
        scene_repository, spatial, DOC,
        QualityMapSelection(set_id=measurement_set.set_id),
        evaluation,
    )
    assert scene.read_only
    assert all(m.state == 'unscaled' for m in scene.markers)
    assert 'no absolute color scale' in scene.read_only_reason_viewport
