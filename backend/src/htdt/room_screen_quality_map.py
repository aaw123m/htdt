"""Room-viewport screen quality map overlay (#1003 / REV73).

Overlays a persisted :class:`CadSpatialMeasurementSet` — ANSI 9-point
(ansi_9_point) luminance/chromaticity/focus evidence, or any other spatial
plan layout — onto the *exact* image surface the current video geometry
evaluation resolved (``VideoGeometryEvaluation.projection.image_plane_corners``
for projection, ``DirectViewGeometryEvaluation.surface.image_plane_corners``
for direct view).

Honesty contract:

- Sample positions are bilinear on the evaluation's declared image-plane
  quad, so rotation/flips/off-axis projection all follow the evaluated
  surface — nothing is re-derived from scratch.
- Only measured cells draw as filled markers; plan points with no
  observation for the selected quantity+viewpoint draw as transparent
  outline markers (hatched-equivalent). Sparse points are never
  interpolated on screen.
- A heatmap only draws when a :class:`CadSpatialDerivedMap` for the
  selected set+quantity carries ``rendered_artifact_ref`` AND that artifact
  resolves to real bytes in the managed-asset store whose SHA-256 matches
  the pinned digest — colors are never synthesized from the record alone.
- ``physical_xyz_m`` on a sample point is *verified* against the mapped
  surface position; a large mismatch marks the point
  ``manual_alignment_required`` — it is never auto-corrected.
- The surface comes only from a non-stale video/direct-view evaluation
  (``target.scene_content_hash == current head content hash``); stale or
  absent evaluation -> read-only overlay with the reason stated.
- Evidence identity mismatches (projector spec swap, screen swap,
  plan-requirement drift) downgrade to read-only markers plus a named
  reason — they never paint current-surface colors from foreign evidence.

No Qt imports here — the workspace owns the widgets; this module owns the
resolution so a late render cannot resurrect a superseded measurement map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from math import sqrt
from typing import Any, Iterable

from .cad_authority_resolver import AuthorityRef
from .cad_repository import SceneRepository
from .cad_scene import Position3, domain_to_render, scene_content_hash
from .cad_spatial_image_authority import (
    CadImageUniformityEvaluation,
    CadSpatialDerivedMap,
    CadSpatialMeasurementPlan,
    CadSpatialMeasurementSet,
    CadSpatialObservation,
    SpatialQuantity,
    evaluate_spatial_uniformity,
)
from .cad_spatial_image_repository import (
    CadSpatialImageRepository,
    SpatialImageIntegrityError,
)
from .managed_assets import MANAGED_ASSETS_DIRNAME, ManagedAssetStore

#: Actor-name prefix registered in ``_OVERLAY_ACTOR_PREFIXES``.
SCREEN_QUALITY_ACTOR_PREFIX = 'quality-map-'

#: physical_xyz_m tolerance before a point requires manual alignment.
#: max(5 cm absolute, 2% of the smaller image dimension).
PHYSICAL_ALIGNMENT_MIN_TOLERANCE_M = 0.05
PHYSICAL_ALIGNMENT_REL_TOLERANCE = 0.02

#: Expected JSON payload kind for a rendered derived-map artifact.
HEATMAP_ARTIFACT_KIND = 'screen-quality-map-artifact'


# ---------------------------------------------------------------------------
# Operator-facing labels
# ---------------------------------------------------------------------------

QUANTITY_LABELS: dict[str, str] = {
    'white_luminance': '白輝度',
    'black_luminance': '黒輝度',
    'contrast': 'コントラスト',
    'white_chromaticity': '白色度 (x,y)',
    'color_error': '色誤差',
    'eotf_gamma_tracking': 'EOTF/ガンマ追従',
    'focus_sharpness': 'フォーカスシャープネス',
    'convergence_fringe': '色ずれ (収差)',
    'project_defined': 'プロジェクト定義量',
}

QUANTITY_VIEWPORT: dict[str, str] = {
    'white_luminance': 'white luminance',
    'black_luminance': 'black luminance',
    'contrast': 'contrast',
    'white_chromaticity': 'white chromaticity (x,y)',
    'color_error': 'color error',
    'eotf_gamma_tracking': 'EOTF/gamma tracking',
    'focus_sharpness': 'focus sharpness',
    'convergence_fringe': 'convergence fringe',
    'project_defined': 'project-defined quantity',
}

ROLE_LABELS = {
    'center': '中央',
    'edge': 'エッジ',
    'corner': 'コーナー',
    'auxiliary': '補助',
    'custom': '任意',
}

COVERAGE_STATE_LABELS = {
    'full_spatial_coverage': '全空間カバー',
    'partial_spatial_coverage': '部分カバー',
    'center_only': '中央のみ',
    'empty': '測定点なし',
}

QUANTITY_STATE_LABELS = {
    'within_profile': 'プロファイル内',
    'outside_profile': 'プロファイル外',
    'criterion_unbound': '基準未バインド',
    'insufficient_coverage': 'カバー不足',
    'insufficient_evidence': '証拠不足',
    'not_evaluable': '評価不能',
}

#: ASCII verdict/labels for the VTK overlay text block.
COVERAGE_STATE_VIEWPORT = {
    'full_spatial_coverage': 'FULL spatial coverage',
    'partial_spatial_coverage': 'PARTIAL spatial coverage',
    'center_only': 'CENTER only',
    'empty': 'EMPTY (no measured points)',
}

QUANTITY_STATE_VIEWPORT = {
    'within_profile': 'within_profile',
    'outside_profile': 'outside_profile',
    'criterion_unbound': 'criterion_unbound',
    'insufficient_coverage': 'insufficient_coverage',
    'insufficient_evidence': 'insufficient_evidence',
    'not_evaluable': 'not_evaluable',
}

TARGET_KIND_LABELS = {
    'projection': 'プロジェクター＋スクリーン',
    'direct_view': '直視ディスプレイ',
}

EVIDENCE_KIND_LABELS = {
    'field_measured': '実測',
    'predicted': '予測',
}


# ---------------------------------------------------------------------------
# Absolute color scales (per quantity + unit — never normalized to data)
# ---------------------------------------------------------------------------

# Each entry: ordered (upper_bound_inclusive, hex) bands; the last band is
# the overflow color. An absolute scale is fixed for the declared unit —
# the same measured value always lands in the same color regardless of
# which set is loaded or how sparse it is.
_BAND_COLORS = (
    '#313695',  # deep blue
    '#4575b4',  # blue
    '#74add1',  # light blue
    '#a6d96a',  # green
    '#fdae61',  # orange
    '#d73027',  # red
)

# (quantity, canonical unit) -> band edges. Unit aliases fold to a single
# canonical key ('cd/m²' == 'nits', 'fL'/'ft-L' is a distinct physical unit).
_ABSOLUTE_SCALES: dict[str, dict[str, tuple[float, ...]]] = {
    'white_luminance': {
        'cd/m²': (15.0, 30.0, 60.0, 120.0, 240.0),
        'fL': (4.0, 9.0, 18.0, 35.0, 70.0),
    },
    'black_luminance': {
        'cd/m²': (0.005, 0.02, 0.08, 0.3, 1.0),
        'fL': (0.0015, 0.006, 0.025, 0.09, 0.3),
    },
    'contrast': {
        ':1': (200.0, 800.0, 2000.0, 8000.0, 20000.0),
        '1': (200.0, 800.0, 2000.0, 8000.0, 20000.0),
    },
    'color_error': {
        'ΔE': (0.5, 1.0, 2.0, 3.0, 6.0),
        'dE': (0.5, 1.0, 2.0, 3.0, 6.0),
        'deltaE': (0.5, 1.0, 2.0, 3.0, 6.0),
    },
    'eotf_gamma_tracking': {
        'gamma': (1.8, 2.0, 2.2, 2.4, 2.6),
    },
    'focus_sharpness': {
        '%': (30.0, 50.0, 70.0, 85.0, 95.0),
        'percent': (30.0, 50.0, 70.0, 85.0, 95.0),
    },
    'convergence_fringe': {
        'px': (0.2, 0.5, 1.0, 2.0, 4.0),
        'pixels': (0.2, 0.5, 1.0, 2.0, 4.0),
        'arcmin': (2.0, 5.0, 10.0, 20.0, 40.0),
    },
}

_UNIT_ALIASES = {
    'nits': 'cd/m²',
    'nit': 'cd/m²',
    'cd/m2': 'cd/m²',
    'cd/m^2': 'cd/m²',
    'ft-L': 'fL',
    'ft-l': 'fL',
    'fL': 'fL',
    'ratio': ':1',
    'x:1': ':1',
    'pixels': 'px',
    'pixel': 'px',
    'pct': '%',
}


def _canonical_unit(units: str | None) -> str | None:
    if units is None:
        return None
    return _UNIT_ALIASES.get(units, units)


def scale_color_for(
    quantity: str, units: str | None, value: float
) -> str | None:
    """Absolute band color for (quantity, unit, value).

    Returns ``None`` when no absolute scale is defined for the
    quantity+unit pair — the caller must then draw the point unscaled
    (neutral outline), never inventing a scale.
    """

    bands = _ABSOLUTE_SCALES.get(quantity, {}).get(_canonical_unit(units) or '')
    if bands is None:
        return None
    for index, upper in enumerate(bands):
        if value <= upper:
            return _BAND_COLORS[index]
    return _BAND_COLORS[-1]


def _chromaticity_to_rgb(x: float, y: float) -> str:
    """Approximate sRGB swatch for a measured white-point x,y pair.

    xyY->XYZ->linear sRGB->gamma, with Y normalized to 1.0. This renders
    the measured chromaticity itself — it is an absolute function of the
    measured pair, not a judgment against a reference white.
    """

    if y <= 0.0:
        return '#404040'
    x_v = x / y
    y_v = 1.0
    z_v = (1.0 - x - y) / y
    r_lin = 3.2406 * x_v - 1.5372 * y_v - 0.4986 * z_v
    g_lin = -0.9689 * x_v + 1.8758 * y_v + 0.0415 * z_v
    b_lin = 0.0557 * x_v - 0.2040 * y_v + 1.0570 * z_v

    # Normalize by the peak channel so different measured white points stay
    # distinguishable — clipping at Y=1.0 would white-out real spreads.
    peak = max(r_lin, g_lin, b_lin)
    if peak <= 0.0:
        return '#404040'
    r_lin, g_lin, b_lin = r_lin / peak, g_lin / peak, b_lin / peak

    def _channel(c: float) -> int:
        c = max(0.0, c)
        gamma = min(1.0, 1.055 * (c ** (1.0 / 2.4)) - 0.055)
        return int(round(gamma * 255))

    return '#{:02x}{:02x}{:02x}'.format(
        _channel(r_lin), _channel(g_lin), _channel(b_lin)
    )


def marker_color(observation: CadSpatialObservation) -> str | None:
    """Fill color for a measured point, or None for unscaleable."""

    if observation.quantity == 'white_chromaticity':
        if (
            observation.chromaticity_x is None
            or observation.chromaticity_y is None
        ):
            return None
        return _chromaticity_to_rgb(
            observation.chromaticity_x, observation.chromaticity_y
        )
    if observation.value is None:
        return None
    return scale_color_for(
        observation.quantity, observation.units, observation.value
    )


# ---------------------------------------------------------------------------
# Resolved scene model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityMapSelection:
    """Explicit operator selection for the map (R1)."""

    set_id: str | None = None
    quantity: str | None = None
    viewpoint_id: str | None = None
    heatmap_enabled: bool = False


@dataclass(frozen=True, slots=True)
class QualityMapMarker:
    """One drawable sample point resolved onto the current surface."""

    point_id: str
    role: str
    x_fraction: float
    y_fraction: float
    #: Domain-space position on the image plane (None = no surface).
    domain_xyz: tuple[float, float, float] | None
    #: VTK render-space position (domain_to_render applied).
    render_xyz: tuple[float, float, float] | None
    #: 'measured' | 'unmeasured' | 'misaligned' | 'unscaled'
    state: str
    observation: CadSpatialObservation | None
    fill_color: str | None
    #: |physical_xyz_m - mapped position| when the point declares one.
    physical_mismatch_m: float | None
    label: str


@dataclass(frozen=True, slots=True)
class QualityMapHeatmapCell:
    """One verified derived-map cell — real values only."""

    row: int
    column: int
    value: float
    fill_color: str | None
    #: Render-space quad corners (BL, BR, TR, TL order).
    render_quad: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True, slots=True)
class QualityMapHeatmap:
    """A verified CadSpatialDerivedMap + its real rendered artifact."""

    map_id: str
    quantity: str
    units: str | None
    viewpoint_id: str | None
    interpolation_algorithm: str
    algorithm_version: str
    grid_resolution: str
    extrapolation: bool
    smoothing: str | None
    uncertainty: float | None
    cells: tuple[QualityMapHeatmapCell, ...]


@dataclass(frozen=True, slots=True)
class QualityMapCoverage:
    """Per-quantity coverage summary for the panel (no invented verdicts)."""

    quantity: str
    observed_points: int
    expected_points: int
    missing_roles: tuple[str, ...]
    verdict_state: str | None  # None when no sealed/live evaluation exists


@dataclass(frozen=True, slots=True)
class ScreenQualityMapScene:
    """Everything one render pass draws/announces for the current head."""

    document_id: str
    revision_id: str
    revision_content_hash: str
    target_kind: str  # 'projection' | 'direct_view' | 'none'
    surface_entity_id: str | None
    evaluation_id: str | None
    measurement_set: CadSpatialMeasurementSet | None
    plan: CadSpatialMeasurementPlan | None
    quantity: str | None
    quantity_units: str | None
    viewpoint_id: str | None
    #: Quantities actually observed in the set (dropdown content).
    available_quantities: tuple[str, ...]
    #: Viewpoints declared by the plan (dropdown content).
    available_viewpoints: tuple[str, ...]
    markers: tuple[QualityMapMarker, ...]
    coverage: tuple[QualityMapCoverage, ...]
    coverage_state: str | None
    heatmap: QualityMapHeatmap | None
    heatmap_available: bool
    #: Surface quad in render space for the marker frame (None when the
    #: surface is unresolved — scene is then read-only text only).
    surface_render_quad: tuple[tuple[float, float, float], ...] | None
    read_only: bool
    read_only_reason: str | None  # JA reason for the panel
    read_only_reason_viewport: str | None  # ASCII reason
    notices: tuple[str, ...]  # JA honesty lines
    viewport_lines: tuple[str, ...]
    summary_ja: tuple[str, ...]

    @property
    def drawable(self) -> bool:
        return self.surface_render_quad is not None


# ---------------------------------------------------------------------------
# Surface mapping
# ---------------------------------------------------------------------------


def _bilinear_on_quad(
    corners: tuple[Position3, Position3, Position3, Position3],
    x_fraction: float,
    y_fraction: float,
) -> tuple[float, float, float]:
    """Map plan (x_fraction, y_fraction) onto the image-plane quad.

    ``image_plane_corners`` are ordered BL, BR, TR, TL in the aperture's
    local right/up frame (``_plane_frame`` signs
    ``((-1,-1),(1,-1),(1,1),(-1,1))``). ``x_fraction`` runs along the
    aperture's local right axis; ``y_fraction`` runs downward from the top
    edge (ANSI 9-point rows: top=1/6, bottom=5/6). Bilinear on the real
    quad keeps rotation, flips, portrait/landscape and off-axis geometry
    exact — nothing is re-projected.
    """

    bl, br, tr, tl = corners
    u = x_fraction
    s = 1.0 - y_fraction  # weight measured up from the bottom edge

    def _mix(p: Position3, w: float) -> tuple[float, float, float]:
        return (p.x_m * w, p.y_m * w, p.z_m * w)

    def _add(a, b):
        return (a[0] + b[0], a[1] + b[1], a[2] + b[2])

    position = _add(
        _add(_mix(bl, (1.0 - u) * (1.0 - s)), _mix(br, u * (1.0 - s))),
        _add(_mix(tr, u * s), _mix(tl, (1.0 - u) * s)),
    )
    return position


def _surface_axes(
    corners: tuple[Position3, Position3, Position3, Position3],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """(width axis, height axis) vectors of the surface quad."""

    bl, br, tr, tl = corners
    width = (br.x_m - bl.x_m, br.y_m - bl.y_m, br.z_m - bl.z_m)
    height = (tl.x_m - bl.x_m, tl.y_m - bl.y_m, tl.z_m - bl.z_m)
    return width, height


def _eval_surface(evaluation) -> tuple[
    str | None,
    tuple[Position3, Position3, Position3, Position3] | None,
    str | None,
    str | None,
]:
    """(target_kind, corners, surface_entity_id, spec_sha) from an eval."""

    if evaluation is None:
        return None, None, None, None
    projection = getattr(evaluation, 'projection', None)
    if projection is not None and getattr(projection, 'image_plane_corners', None):
        return (
            'projection',
            tuple(projection.image_plane_corners),
            evaluation.request.screen.entity_id,
            getattr(evaluation, 'projector_specification_sha256', None),
        )
    surface = getattr(evaluation, 'surface', None)
    if surface is not None and getattr(surface, 'image_plane_corners', None):
        return (
            'direct_view',
            tuple(surface.image_plane_corners),
            evaluation.request.display.entity_id,
            getattr(evaluation, 'display_specification_sha256', None),
        )
    return None, None, None, None


def _heatmap_payload(raw: bytes) -> dict[str, Any] | None:
    """Parse + shape-check a rendered-artifact payload; None when invalid."""

    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('kind') != HEATMAP_ARTIFACT_KIND:
        return None
    values = payload.get('values')
    if not isinstance(values, list) or not values:
        return None
    width = len(values[0]) if isinstance(values[0], list) else 0
    if width == 0:
        return None
    for row in values:
        if not isinstance(row, list) or len(row) != width:
            return None
        for cell in row:
            if cell is not None and not isinstance(cell, (int, float)):
                return None
    return payload


def _resolve_heatmap(
    derived_map: CadSpatialDerivedMap,
    asset_store: ManagedAssetStore | None,
    corners: tuple[Position3, Position3, Position3, Position3],
    quantity: str,
) -> tuple[QualityMapHeatmap | None, str | None]:
    """(heatmap, reason) — draws only on a sha-verified real artifact."""

    ref = derived_map.rendered_artifact_ref
    if ref is None:
        return None, '派生マップに描画済みアーティファクトがありません'
    if asset_store is None or ref.ref_sha256 is None:
        return None, 'アーティファクトストアを利用できません'
    path = asset_store.asset_path(ref.ref_sha256)
    try:
        raw = asset_store.read_file(path)
    except OSError:
        return None, '描画済みアーティファクトが見つかりません'
    if sha256(raw).hexdigest() != ref.ref_sha256:
        return None, 'アーティファクトのSHAが不一致です'
    payload = _heatmap_payload(raw)
    if payload is None:
        return None, 'アーティファクト形式を検証できません'
    if payload.get('quantity') != derived_map.quantity:
        return None, 'アーティファクトの量がマップと一致しません'
    values = payload['values']
    units = payload.get('units')
    rows = len(values)
    columns = len(values[0])
    cells: list[QualityMapHeatmapCell] = []
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            if value is None:
                continue  # transparent cell — never synthesized
            x0 = column_index / columns
            x1 = (column_index + 1) / columns
            y0 = row_index / rows
            y1 = (row_index + 1) / rows
            quad = tuple(
                domain_to_render(
                    Position3(
                        x_m=p[0],
                        y_m=p[1],
                        z_m=p[2],
                    )
                )
                for p in (
                    _bilinear_on_quad(corners, x0, y0),
                    _bilinear_on_quad(corners, x1, y0),
                    _bilinear_on_quad(corners, x1, y1),
                    _bilinear_on_quad(corners, x0, y1),
                )
            )
            cells.append(
                QualityMapHeatmapCell(
                    row=row_index,
                    column=column_index,
                    value=float(value),
                    fill_color=scale_color_for(quantity, units, float(value)),
                    render_quad=quad,
                )
            )
    return (
        QualityMapHeatmap(
            map_id=derived_map.map_id,
            quantity=derived_map.quantity,
            units=units,
            viewpoint_id=derived_map.viewpoint_id,
            interpolation_algorithm=derived_map.interpolation_algorithm,
            algorithm_version=derived_map.algorithm_version,
            grid_resolution=derived_map.grid_resolution,
            extrapolation=derived_map.extrapolation,
            smoothing=derived_map.smoothing,
            uncertainty=derived_map.uncertainty,
            cells=tuple(cells),
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def resolve_screen_quality_map(
    scene_repository: SceneRepository,
    spatial_repository: CadSpatialImageRepository,
    document_id: str,
    selection: QualityMapSelection,
    evaluation,
    *,
    asset_store: ManagedAssetStore | None = None,
) -> ScreenQualityMapScene | None:
    """Resolve the quality-map overlay for the document's CURRENT head.

    Re-reads live authority on every call — a scene edit, screen swap,
    projector-spec change or new measurement set is picked up the same
    render pass, and a stale surface can never stay painted.
    """

    head = scene_repository.current_head(document_id)
    if head is None:
        return None
    head_hash = scene_content_hash(head.document)

    def _text_scene(
        *,
        read_only_reason: str,
        read_only_reason_viewport: str,
        notices: tuple[str, ...] = (),
        viewport_lines: tuple[str, ...] = (),
    ) -> ScreenQualityMapScene:
        return ScreenQualityMapScene(
            document_id=document_id,
            revision_id=head.revision_id,
            revision_content_hash=head_hash,
            target_kind='none',
            surface_entity_id=None,
            evaluation_id=None,
            measurement_set=None,
            plan=None,
            quantity=None,
            quantity_units=None,
            viewpoint_id=None,
            available_quantities=(),
            available_viewpoints=(),
            markers=(),
            coverage=(),
            coverage_state=None,
            heatmap=None,
            heatmap_available=False,
            surface_render_quad=None,
            read_only=True,
            read_only_reason=read_only_reason,
            read_only_reason_viewport=read_only_reason_viewport,
            notices=notices,
            viewport_lines=(
                viewport_lines
                or (f'screen quality map: {read_only_reason_viewport}',)
            ),
            summary_ja=(read_only_reason, *notices),
        )

    # -- measurement set -----------------------------------------------------
    measurement_set: CadSpatialMeasurementSet | None = None
    if selection.set_id:
        try:
            measurement_set = spatial_repository.get_measurement_set(
                selection.set_id
            )
        except (SpatialImageIntegrityError, ValueError) as exc:
            return _text_scene(
                read_only_reason=f'測定セットを再検証できません: {exc}',
                read_only_reason_viewport='set unreadable: revalidation failed',
            )
        if measurement_set is not None and (
            measurement_set.document_id != document_id
        ):
            measurement_set = None
    if measurement_set is None:
        return _text_scene(
            read_only_reason='測定セットを選択してください',
            read_only_reason_viewport='select a measurement set',
        )

    # -- plan (set must pin the exact plan sha) --------------------------------
    try:
        plan = spatial_repository.get_plan(measurement_set.plan_ref.ref_id)
    except (SpatialImageIntegrityError, ValueError) as exc:
        return _text_scene(
            read_only_reason=f'測定計画を再検証できません: {exc}',
            read_only_reason_viewport='plan unreadable: revalidation failed',
        )
    if plan is None or plan.plan_sha256 != measurement_set.plan_ref.ref_sha256:
        return _text_scene(
            read_only_reason='測定セットの計画バインドが一致しません',
            read_only_reason_viewport='plan binding mismatch',
        )

    # -- surface from a non-stale evaluation ---------------------------------
    target_kind, corners, surface_entity_id, spec_sha = _eval_surface(
        evaluation
    )
    if evaluation is None:
        return _text_scene(
            read_only_reason=(
                '映像ジオメトリ評価を実行してください'
                '（画面面の確定が必要です）'
            ),
            read_only_reason_viewport='run video geometry evaluation first',
        )
    eval_hash = getattr(
        getattr(evaluation, 'target', None), 'scene_content_hash', None
    )
    evaluation_id = getattr(evaluation, 'evaluation_id', None)
    if eval_hash != head_hash:
        return _text_scene(
            read_only_reason=(
                '映像評価が現在のシーン版より古いです'
                ' — 再評価してください'
            ),
            read_only_reason_viewport='stale evaluation: re-run video evaluation',
        )
    if corners is None or surface_entity_id is None or target_kind is None:
        return _text_scene(
            read_only_reason='画像面を解決できません（評価結果に画域がありません）',
            read_only_reason_viewport='no image surface in evaluation',
        )

    surface_render_quad = tuple(
        domain_to_render(corner) for corner in corners
    )
    width_axis, height_axis = _surface_axes(corners)
    width_m = sqrt(sum(c * c for c in width_axis))
    height_m = sqrt(sum(c * c for c in height_axis))
    physical_tolerance = max(
        PHYSICAL_ALIGNMENT_MIN_TOLERANCE_M,
        PHYSICAL_ALIGNMENT_REL_TOLERANCE * min(width_m, height_m),
    )

    notices: list[str] = []
    read_only = False
    read_only_reason: str | None = None
    read_only_reason_viewport: str | None = None

    # -- evidence identity checks (no stale reuse) ----------------------------
    bound_projector_sha = None
    if measurement_set.projector_state is not None and (
        measurement_set.projector_state.projector_ref is not None
    ):
        bound_projector_sha = (
            measurement_set.projector_state.projector_ref.ref_sha256
        )
    if (
        bound_projector_sha is not None
        and spec_sha is not None
        and bound_projector_sha != spec_sha
    ):
        read_only = True
        read_only_reason = (
            '測定セットがバインドするプロジェクター仕様と'
            '現在の評価が異なります（プロジェクターモード不一致）'
        )
        read_only_reason_viewport = (
            'projector spec mismatch: measured under a different spec'
        )
        notices.append(read_only_reason)

    # Screen-swap check: the plan may pin a screen authority; its ref id
    # names the measured screen — it must be the surface we draw on.
    bound_screen_ref = None
    if plan.screen_state is not None:
        bound_screen_ref = plan.screen_state.screen_ref
    if bound_screen_ref is not None and bound_screen_ref.ref_id != surface_entity_id:
        read_only = True
        read_only_reason = (
            '測定がバインドするスクリーンと現在の対象面が異なります'
            '（スクリーン交換）'
        )
        read_only_reason_viewport = (
            'screen mismatch: set bound to a different surface'
        )
        notices.append(read_only_reason)

    # Plan's projector-state requirement vs the set's recorded state —
    # field-level drift is flagged, not hidden.
    requirement = plan.projector_state_requirement
    observed_state = measurement_set.projector_state
    if requirement is not None and observed_state is not None:
        diffs: list[str] = []
        for field_name, label_ja in (
            ('picture_mode', 'ピクチャーモード'),
            ('light_source_mode', '光源モード'),
            ('lens_memory_id', 'レンズメモリ'),
            ('zoom_ratio', 'ズーム比'),
            ('lens_shift_h', '水平レンズシフト'),
            ('lens_shift_v', '垂直レンズシフト'),
            ('keystone_correction', 'キーストン補正'),
            ('anamorphic_state', 'アナモルフィック状態'),
        ):
            required_value = getattr(requirement, field_name, None)
            observed_value = getattr(observed_state, field_name, None)
            if (
                required_value is not None
                and observed_value is not None
                and required_value != observed_value
            ):
                diffs.append(
                    f'{label_ja}: 計画={required_value} / 実測={observed_value}'
                )
        if diffs:
            notices.append(
                'プロジェクター状態が計画要求と異なります: ' + '; '.join(diffs)
            )

    if measurement_set.evidence_kind == 'predicted':
        notices.append(
            'このセットは予測値です（実測ではありません）— '
            '設計時の予想マップとして表示します'
        )

    # -- quantity/viewpoint selection ----------------------------------------
    observed_quantities = {obs.quantity for obs in measurement_set.observations}
    available_quantities = tuple(
        quantity
        for quantity in plan.quantities
        if quantity in observed_quantities
    ) + tuple(
        sorted(observed_quantities - set(plan.quantities))
    )
    quantity = selection.quantity
    if quantity is None or quantity not in observed_quantities:
        quantity = available_quantities[0] if available_quantities else None

    available_viewpoints = tuple(v.viewpoint_id for v in plan.viewpoints)
    viewpoint_id = selection.viewpoint_id
    if viewpoint_id is None or viewpoint_id not in available_viewpoints:
        viewpoint_id = available_viewpoints[0] if available_viewpoints else None

    # Observations keyed strictly (point, viewpoint, quantity) inside THIS
    # set — a point never borrows another point's or viewpoint's reading.
    observation_by_key = {
        (obs.point_id, obs.viewpoint_id, obs.quantity): obs
        for obs in measurement_set.observations
    }

    quantity_units: str | None = None
    markers: list[QualityMapMarker] = []
    all_unscaled = True
    any_measured = False
    for point in plan.points:
        domain_xyz = _bilinear_on_quad(
            corners, point.x_fraction, point.y_fraction
        )
        observation = (
            observation_by_key.get((point.point_id, viewpoint_id, quantity))
            if quantity is not None
            else None
        )
        if quantity is not None and observation is not None and (
            observation.units is not None
        ):
            quantity_units = observation.units
        mismatch_m: float | None = None
        if point.physical_xyz_m is not None:
            mismatch_m = sqrt(
                sum(
                    (a - b) ** 2
                    for a, b in zip(domain_xyz, point.physical_xyz_m)
                )
            )
        if mismatch_m is not None and mismatch_m > physical_tolerance:
            markers.append(
                QualityMapMarker(
                    point_id=point.point_id,
                    role=point.role,
                    x_fraction=point.x_fraction,
                    y_fraction=point.y_fraction,
                    domain_xyz=domain_xyz,
                    render_xyz=domain_to_render(Position3(
                        x_m=domain_xyz[0], y_m=domain_xyz[1], z_m=domain_xyz[2]
                    )),
                    state='misaligned',
                    observation=observation,
                    fill_color=None,
                    physical_mismatch_m=mismatch_m,
                    label=point.point_id,
                )
            )
            notices.append(
                f'{point.point_id}: 物理座標と画面面が '
                f'{mismatch_m * 1000.0:.0f} mm 不一致 — 手動アライメントが必要です'
            )
            continue
        if observation is None:
            markers.append(
                QualityMapMarker(
                    point_id=point.point_id,
                    role=point.role,
                    x_fraction=point.x_fraction,
                    y_fraction=point.y_fraction,
                    domain_xyz=domain_xyz,
                    render_xyz=domain_to_render(Position3(
                        x_m=domain_xyz[0], y_m=domain_xyz[1], z_m=domain_xyz[2]
                    )),
                    state='unmeasured',
                    observation=None,
                    fill_color=None,
                    physical_mismatch_m=mismatch_m,
                    label=point.point_id,
                )
            )
            continue
        any_measured = True
        color = marker_color(observation)
        if color is None:
            markers.append(
                QualityMapMarker(
                    point_id=point.point_id,
                    role=point.role,
                    x_fraction=point.x_fraction,
                    y_fraction=point.y_fraction,
                    domain_xyz=domain_xyz,
                    render_xyz=domain_to_render(Position3(
                        x_m=domain_xyz[0], y_m=domain_xyz[1], z_m=domain_xyz[2]
                    )),
                    state='unscaled',
                    observation=observation,
                    fill_color=None,
                    physical_mismatch_m=mismatch_m,
                    label=point.point_id,
                )
            )
            continue
        all_unscaled = False
        markers.append(
            QualityMapMarker(
                point_id=point.point_id,
                role=point.role,
                x_fraction=point.x_fraction,
                y_fraction=point.y_fraction,
                domain_xyz=domain_xyz,
                render_xyz=domain_to_render(Position3(
                    x_m=domain_xyz[0], y_m=domain_xyz[1], z_m=domain_xyz[2]
                )),
                state='measured',
                observation=observation,
                fill_color=color,
                physical_mismatch_m=mismatch_m,
                label=point.point_id,
            )
        )
    if any_measured and all_unscaled:
        read_only = True
        read_only_reason = (
            f'量 {QUANTITY_LABELS.get(quantity or "", quantity)} の単位 '
            f'({quantity_units or "不明"}) には絶対スケールが定義されていません'
        )
        read_only_reason_viewport = (
            'no absolute color scale for this quantity+unit'
        )
        notices.append(read_only_reason)

    # -- coverage / uniformity verdicts ---------------------------------------
    coverage_state: str | None = None
    coverage: list[QualityMapCoverage] = []
    evaluation_for_verdicts: CadImageUniformityEvaluation | None = None
    try:
        persisted = tuple(
            item
            for item in spatial_repository.list_evaluations(document_id)
            if item.set_ref.ref_id == measurement_set.set_id
            and item.set_ref.ref_sha256 == measurement_set.set_sha256
            and item.plan_ref.ref_sha256 == plan.plan_sha256
        )
    except (SpatialImageIntegrityError, ValueError):
        persisted = ()
    if persisted:
        evaluation_for_verdicts = persisted[-1]
    else:
        try:
            evaluation_for_verdicts = evaluate_spatial_uniformity(
                document_id=document_id,
                plan=plan,
                measurement_set=measurement_set,
            )
        except ValueError:
            evaluation_for_verdicts = None
    if evaluation_for_verdicts is not None:
        coverage_state = evaluation_for_verdicts.coverage_state
        for verdict in evaluation_for_verdicts.quantity_verdicts:
            coverage.append(
                QualityMapCoverage(
                    quantity=verdict.quantity,
                    observed_points=verdict.observed_points,
                    expected_points=verdict.expected_points,
                    missing_roles=tuple(verdict.missing_roles),
                    verdict_state=verdict.state,
                )
            )
    else:
        for q in available_quantities:
            observed = sum(
                1
                for obs in measurement_set.observations
                if obs.quantity == q and obs.viewpoint_id in available_viewpoints
            )
            coverage.append(
                QualityMapCoverage(
                    quantity=q,
                    observed_points=observed,
                    expected_points=len(plan.points),
                    missing_roles=(),
                    verdict_state=None,
                )
            )

    # -- derived heatmap (verified artifact only) ------------------------------
    heatmap: QualityMapHeatmap | None = None
    heatmap_available = False
    if selection.heatmap_enabled and quantity is not None:
        candidates: tuple[CadSpatialDerivedMap, ...] = ()
        try:
            candidates = tuple(
                item
                for item in spatial_repository.list_derived_maps(document_id)
                if item.source_set_ref.ref_id == measurement_set.set_id
                and item.source_set_ref.ref_sha256 == measurement_set.set_sha256
                and item.quantity == quantity
                and (
                    item.viewpoint_id is None
                    or item.viewpoint_id == viewpoint_id
                )
            )
        except (SpatialImageIntegrityError, ValueError):
            candidates = ()
        if not candidates:
            notices.append('選択した量の派生マップが登録されていません')
        else:
            heatmap, reason = _resolve_heatmap(
                candidates[-1], asset_store, corners, quantity
            )
            heatmap_available = heatmap is not None
            if heatmap is None and reason is not None:
                notices.append(f'ヒートマップ非表示: {reason}')
    else:
        heatmap_available = False
        if quantity is not None:
            try:
                heatmap_available = any(
                    item.source_set_ref.ref_id == measurement_set.set_id
                    and item.source_set_ref.ref_sha256
                    == measurement_set.set_sha256
                    and item.quantity == quantity
                    and item.rendered_artifact_ref is not None
                    for item in spatial_repository.list_derived_maps(
                        document_id
                    )
                )
            except (SpatialImageIntegrityError, ValueError):
                heatmap_available = False

    # -- summary lines ---------------------------------------------------------
    measured_count = sum(1 for m in markers if m.state == 'measured')
    misaligned_count = sum(1 for m in markers if m.state == 'misaligned')
    unmeasured_count = sum(1 for m in markers if m.state == 'unmeasured')
    quantity_label_vp = QUANTITY_VIEWPORT.get(
        quantity or '', quantity or 'no quantity'
    )
    lines = [
        f'screen quality map: {quantity_label_vp} | '
        f'{measured_count}/{len(markers)} measured '
        f'({unmeasured_count} unmeasured, {misaligned_count} misaligned) | '
        f'{target_kind} surface {surface_entity_id} | '
        f'rev {head.revision_id[:8]} CURRENT',
        f'set {measurement_set.set_id} [{measurement_set.evidence_kind}] '
        f'declared {measurement_set.declared_at_utc} | viewpoint {viewpoint_id}',
    ]
    if coverage_state is not None:
        lines.append(
            'coverage: '
            + COVERAGE_STATE_VIEWPORT.get(coverage_state, coverage_state)
        )
    if read_only_reason_viewport:
        lines.append(f'READ-ONLY: {read_only_reason_viewport}')
    if heatmap is not None:
        lines.append(
            f'heatmap {heatmap.map_id}: {heatmap.interpolation_algorithm} '
            f'v{heatmap.algorithm_version} grid={heatmap.grid_resolution} '
            f'smoothing={heatmap.smoothing or "none"} '
            f'extrapolation={heatmap.extrapolation} '
            f'uncertainty={heatmap.uncertainty}'
        )

    summary: list[str] = [
        f'スクリーン品質マップ: {QUANTITY_LABELS.get(quantity or "", quantity or "—")} '
        f'· {TARGET_KIND_LABELS.get(target_kind, target_kind)} '
        f'({surface_entity_id}) · 視点 {viewpoint_id}',
        f'測定点 {measured_count}/{len(markers)} '
        f'(未測定 {unmeasured_count}, 不一致 {misaligned_count}) · '
        f'{EVIDENCE_KIND_LABELS.get(measurement_set.evidence_kind, measurement_set.evidence_kind)} '
        f'· {measurement_set.declared_at_utc}',
    ]
    if coverage_state is not None:
        summary.append(
            'カバレッジ: '
            + COVERAGE_STATE_LABELS.get(coverage_state, coverage_state)
        )
    for item in coverage:
        missing = (
            ' / 不足ロール: '
            + ', '.join(ROLE_LABELS.get(r, r) for r in item.missing_roles)
            if item.missing_roles
            else ''
        )
        verdict = (
            QUANTITY_STATE_LABELS.get(item.verdict_state, item.verdict_state)
            if item.verdict_state
            else '評価なし'
        )
        summary.append(
            f'{QUANTITY_LABELS.get(item.quantity, item.quantity)}: '
            f'{item.observed_points}/{item.expected_points} 点 · {verdict}{missing}'
        )
    if heatmap is not None:
        summary.append(
            f'ヒートマップ {heatmap.map_id}: {heatmap.interpolation_algorithm} '
            f'v{heatmap.algorithm_version} · grid {heatmap.grid_resolution} · '
            f'平滑化={heatmap.smoothing or "なし"} · '
            f'外挿={heatmap.extrapolation} · 誤差={heatmap.uncertainty}'
        )
    if read_only and read_only_reason:
        summary.append(f'読み取り専用: {read_only_reason}')

    return ScreenQualityMapScene(
        document_id=document_id,
        revision_id=head.revision_id,
        revision_content_hash=head_hash,
        target_kind=target_kind,
        surface_entity_id=surface_entity_id,
        evaluation_id=evaluation_id,
        measurement_set=measurement_set,
        plan=plan,
        quantity=quantity,
        quantity_units=quantity_units,
        viewpoint_id=viewpoint_id,
        available_quantities=available_quantities,
        available_viewpoints=available_viewpoints,
        markers=tuple(markers),
        coverage=tuple(coverage),
        coverage_state=coverage_state,
        heatmap=heatmap,
        heatmap_available=heatmap_available,
        surface_render_quad=surface_render_quad,
        read_only=read_only,
        read_only_reason=read_only_reason,
        read_only_reason_viewport=read_only_reason_viewport,
        notices=tuple(notices),
        viewport_lines=tuple(lines),
        summary_ja=tuple(summary),
    )


# ---------------------------------------------------------------------------
# Controller — re-resolve per render, cached on live authority keys
# ---------------------------------------------------------------------------


class ScreenQualityMapController:
    """Selection + per-render resolve() for the screen quality map (#1003).

    The cache key is the live head (revision id + content hash), the exact
    set sha, the selected quantity/viewpoint, and the surface evaluation's
    identity — so a scene edit, screen swap, projector-mode change or new
    set never replays stale markers.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        spatial_repository: CadSpatialImageRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.spatial_repository = spatial_repository
        self.document_id = document_id
        self.selection = QualityMapSelection()
        self._cache_key: tuple | None = None
        self._cache: ScreenQualityMapScene | None = None

    @property
    def asset_store(self) -> ManagedAssetStore:
        return ManagedAssetStore(
            self.scene_repository.path.parent / MANAGED_ASSETS_DIRNAME
        )

    def available_sets(self) -> tuple[CadSpatialMeasurementSet, ...]:
        try:
            return self.spatial_repository.list_measurement_sets(
                self.document_id
            )
        except (SpatialImageIntegrityError, ValueError):
            return ()

    def select_set(self, set_id: str | None) -> None:
        if set_id != self.selection.set_id:
            self.selection = QualityMapSelection(
                set_id=set_id,
                quantity=None,
                viewpoint_id=None,
                heatmap_enabled=self.selection.heatmap_enabled,
            )
            self.invalidate()

    def select_quantity(self, quantity: str | None) -> None:
        if quantity != self.selection.quantity:
            self.selection = QualityMapSelection(
                set_id=self.selection.set_id,
                quantity=quantity,
                viewpoint_id=self.selection.viewpoint_id,
                heatmap_enabled=self.selection.heatmap_enabled,
            )
            self.invalidate()

    def select_viewpoint(self, viewpoint_id: str | None) -> None:
        if viewpoint_id != self.selection.viewpoint_id:
            self.selection = QualityMapSelection(
                set_id=self.selection.set_id,
                quantity=self.selection.quantity,
                viewpoint_id=viewpoint_id,
                heatmap_enabled=self.selection.heatmap_enabled,
            )
            self.invalidate()

    def set_heatmap_enabled(self, enabled: bool) -> None:
        if enabled != self.selection.heatmap_enabled:
            self.selection = QualityMapSelection(
                set_id=self.selection.set_id,
                quantity=self.selection.quantity,
                viewpoint_id=self.selection.viewpoint_id,
                heatmap_enabled=enabled,
            )
            self.invalidate()

    def invalidate(self) -> None:
        self._cache_key = None
        self._cache = None

    def resolve(self, evaluation) -> ScreenQualityMapScene | None:
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            self._cache_key = None
            self._cache = None
            return None
        eval_identity = None
        if evaluation is not None:
            eval_identity = (
                getattr(evaluation, 'evaluation_id', None),
                getattr(
                    getattr(evaluation, 'target', None),
                    'scene_content_hash',
                    None,
                ),
            )
        set_sha = None
        if self.selection.set_id:
            try:
                bound = self.spatial_repository.get_measurement_set(
                    self.selection.set_id
                )
            except (SpatialImageIntegrityError, ValueError):
                bound = None
            set_sha = bound.set_sha256 if bound is not None else '<missing>'
        key = (
            head.revision_id,
            head.content_hash,
            eval_identity,
            self.selection,
            set_sha,
        )
        if key != self._cache_key:
            self._cache = resolve_screen_quality_map(
                self.scene_repository,
                self.spatial_repository,
                self.document_id,
                self.selection,
                evaluation,
                asset_store=self.asset_store,
            )
            self._cache_key = key
        return self._cache


__all__ = [
    'HEATMAP_ARTIFACT_KIND',
    'PHYSICAL_ALIGNMENT_MIN_TOLERANCE_M',
    'PHYSICAL_ALIGNMENT_REL_TOLERANCE',
    'QualityMapCoverage',
    'QualityMapHeatmap',
    'QualityMapHeatmapCell',
    'QualityMapMarker',
    'QualityMapSelection',
    'SCREEN_QUALITY_ACTOR_PREFIX',
    'ScreenQualityMapController',
    'ScreenQualityMapScene',
    'marker_color',
    'resolve_screen_quality_map',
    'scale_color_for',
]
