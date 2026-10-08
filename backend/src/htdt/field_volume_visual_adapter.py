"""Pure display adapter: sealed SpatialFieldResult -> 3D CAD viewport (#999).

Everything in this module is a *derived display product*: it reads the
sealed :class:`SpatialFieldResult` / :class:`FieldExplorerSession`
authorities, re-expresses their canonical samples in the viewport's render
coordinate frame, and returns plain dataclasses a VTK layer can consume.
It never writes back, never recomputes physics, and never imports Qt or
VTK — so the whole transformation is headless-testable.

Contracts honoured here:

- Canonical sample order ``(iz * ny + iy) * nx + ix`` equals NumPy
  ``reshape((nx, ny, nz), order='F')`` and VTK ``ImageData`` point order
  (x fastest). The adapter exposes ``scalars_xyz`` — a ``(nx, ny, nz)``
  array in *render* axis order — which the viewport flattens with
  ``ravel(order='F')`` into ``point_data``.
- HTDT domain coordinates (+X right / +Y rear / +Z up) map to VTK render
  coordinates ``(x, -y, z)`` (:func:`cad_scene.domain_to_render`). Because
  ``ImageData`` spacing must be positive, the Y axis is *reversed in the
  sample array* and the render origin is moved to
  ``-(origin_y + (ny - 1) * dy)`` instead of emitting negative spacing.
- Display scalars are ``float32`` (display only — the sealed canonical
  samples are never rounded or thinned; ``clim`` is computed on the
  full-resolution quantity values so decimation cannot hide an extremum).
- ``MAX_FIELD_SAMPLES`` is the *data-authority* bound, not a render-safety
  bound: the adapter applies its own :data:`FIELD_DISPLAY_POINT_BUDGET`
  and reports ``display_stride``/``decimated`` honestly.
- Staleness is fail-closed: the view is built only when the session's
  pinned SceneRevision IS the repository's current head (id AND content
  hash AND document id). A missing head is UNKNOWN and also blocked —
  an old field must never paint onto a scene it no longer describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

import numpy as np

from .cad_field_explorer import (
    FieldExplorerSession,
    field_explorer_session_currency,
)
from .cad_repository import SceneRevision
from .cad_scene import Position3
from .cad_spatial_field import (
    FieldQuantity,
    FieldProbeValue,
    RegularGridAxis,
    SpatialFieldResult,
    _QUANTITY_UNIT,
    probe_field,
)

#: Display-only point budget for the volume grid handed to VTK — separate
#: from the canonical ``MAX_FIELD_SAMPLES`` contract. 1M float32 scalars is
#: ~4 MB before VTK/GPU staging; above this the adapter decimates the
#: display grid by a uniform integer stride instead of touching samples.
FIELD_DISPLAY_POINT_BUDGET = 1_000_000

#: Quantities the 3D viewport can show. ``complex_pressure`` stays 2D-panel
#: only — a single colour channel cannot carry it honestly.
FieldDisplayQuantity = Literal['pressure_magnitude_pa', 'spl_db', 'phase_deg']

FieldOverlayCurrencyState = Literal['CURRENT', 'STALE', 'UNKNOWN']

#: Scalar-bar titles must name scale *and* honesty state, never just a unit.
_QUANTITY_BAR_TITLES: dict[str, str] = {
    'pressure_magnitude_pa': 'モード圧力振幅 [Pa] (規格化)',
    'spl_db': 'SPL [dB]',
    'phase_deg': '位相 [deg] (循環)',
}


class FieldDisplayBlocked(ValueError):
    """Fail-closed refusal: no display model for a non-current/unsupported field."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__('; '.join(reasons))
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class FieldOverlayCurrency:
    state: FieldOverlayCurrencyState
    reasons: tuple[str, ...]


def field_overlay_currency(
    session: FieldExplorerSession,
    head: SceneRevision | None,
) -> FieldOverlayCurrency:
    """Currency of one session against the document's CURRENT head.

    The pinned revision itself is never the arbiter — an old session stays
    'CURRENT' against its own stored revision even after the head moves on
    (the #999 blocker). Only the live head decides.
    """

    if session.document_id != (head.document_id if head is not None else session.document_id):
        return FieldOverlayCurrency('STALE', ('document mismatch',))
    if head is None:
        return FieldOverlayCurrency(
            'UNKNOWN', ('current head revision unavailable',)
        )
    currency = field_explorer_session_currency(session, head)
    if currency.state == 'CURRENT':
        return FieldOverlayCurrency('CURRENT', ())
    return FieldOverlayCurrency('STALE', tuple(currency.stale_reasons))


def _display_stride(
    dims: tuple[int, int, int], point_budget: int
) -> int:
    """Smallest uniform integer stride keeping the display grid in budget.

    A stride that collapses a multi-sample axis to one point is not a
    bounded grid — it is a useless one — so it fails closed.
    """

    total = dims[0] * dims[1] * dims[2]
    if total <= point_budget:
        return 1
    stride = 1
    while stride <= max(dims):
        stride += 1
        bounded = tuple((count + stride - 1) // stride for count in dims)
        if bounded[0] * bounded[1] * bounded[2] <= point_budget:
            if all(
                src == 1 or dst >= 2 for src, dst in zip(dims, bounded)
            ):
                return stride
            raise FieldDisplayBlocked(
                ('field cannot be bounded within the display budget',)
            )
    raise FieldDisplayBlocked(('field cannot be bounded within the display budget',))


def _scalars_display(
    result: SpatialFieldResult,
    quantity: FieldDisplayQuantity,
    stride: int,
    phase_mask_min_magnitude_pa: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """(scalar, mask) arrays shaped (nx, ny, nz) in DOMAIN axis order.

    Returns float32 display scalars and a bool mask (True = visible);
    masked values are NaN in the scalar array.
    """

    nx, ny, nz = (axis.count for axis in result.axes)
    if result.is_complex:
        real = np.asarray(result.pressure_real, dtype=np.float64)
        imag = np.asarray(result.pressure_imag, dtype=np.float64)
        magnitude = np.hypot(real, imag)
        if quantity == 'phase_deg':
            values = np.degrees(np.arctan2(imag, real))
        elif quantity == 'spl_db':
            safe = np.maximum(magnitude, 1e-300)
            values = 20.0 * np.log10(safe / result.pressure_reference_pa)
            values[magnitude <= 0.0] = -400.0
        else:
            values = magnitude
    else:
        magnitude = np.asarray(result.pressure_magnitude_pa, dtype=np.float64)
        if quantity == 'spl_db':
            safe = np.maximum(magnitude, 1e-300)
            values = 20.0 * np.log10(safe / result.pressure_reference_pa)
            values[magnitude <= 0.0] = -400.0
        else:
            values = magnitude

    # Full-resolution range BEFORE decimation: the colour legend must cover
    # the field's real extrema, not just the strided subset's.
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise FieldDisplayBlocked(('field has no finite display values',))
    clim = (float(finite.min()), float(finite.max()))

    # Canonical flat order (iz*ny+iy)*nx+ix == F-order reshape of (nx,ny,nz).
    grid = values.reshape((nz, ny, nx)).transpose(2, 1, 0)  # (nx,ny,nz) domain
    mask = np.ones(grid.shape, dtype=bool)
    if quantity == 'phase_deg' and phase_mask_min_magnitude_pa is not None:
        mask &= (
            magnitude.reshape((nz, ny, nx)).transpose(2, 1, 0)
            >= phase_mask_min_magnitude_pa
        )
    if stride > 1:
        grid = grid[::stride, ::stride, ::stride]
        mask = mask[::stride, ::stride, ::stride]
    out = np.ascontiguousarray(grid, dtype=np.float32)
    out[~mask] = np.nan
    return out, clim


def _render_axes(
    axes: tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis],
    stride: int,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[int, int, int]]:
    """(render_origin, render_spacing, dims) after stride + Y flip."""

    dims = tuple(
        (axis.count + stride - 1) // stride for axis in axes
    )
    ox = axes[0].origin_m
    oy = axes[1].origin_m
    oz = axes[2].origin_m
    dx = axes[0].spacing_m * stride
    dy = axes[1].spacing_m * stride
    dz = axes[2].spacing_m * stride
    ny_render = dims[1]
    # Y reversal: the render grid runs from the far (max domain y) end back.
    oy_render = -(oy + (axes[1].count - 1) * axes[1].spacing_m)
    return (ox, oy_render, oz), (dx, dy, dz), (dims[0], ny_render, dims[2])


@dataclass(frozen=True, slots=True)
class FieldDisplayView:
    """Bounded display grid + provenance; VTK layer consumes verbatim."""

    # provenance pins
    session_id: str
    result_semantic_sha256: str
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    prediction_run_id: str
    producer: str
    model_id: str
    model_version: str
    mode_indices: tuple[int, int, int]
    frequency_hz: float
    coordinate_frame_id: str | None
    coordinate_frame_version: str | None
    absolute_pressure_reference: bool

    quantity: FieldDisplayQuantity
    unit: str
    # render-space grid (Y already flipped in the array and origin)
    dims: tuple[int, int, int]
    render_origin: tuple[float, float, float]
    render_spacing: tuple[float, float, float]
    scalars_xyz: np.ndarray  # (nx,ny,nz) float32, render order, NaN = masked
    clim: tuple[float, float]
    masked_count: int
    cyclic_colormap: bool  # phase -> cyclic hue ramp
    sample_state: Literal['exact', 'decimated']
    display_stride: int
    source_point_count: int
    domain_axes: tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis]

    @property
    def scalar_bar_title(self) -> str:
        title = _QUANTITY_BAR_TITLES.get(self.quantity, self.quantity)
        if self.sample_state == 'decimated':
            title += f' (表示間引き x{self.display_stride})'
        return title


def build_field_display_view(
    session: FieldExplorerSession,
    *,
    quantity: FieldDisplayQuantity,
    head: SceneRevision | None,
    point_budget: int = FIELD_DISPLAY_POINT_BUDGET,
    phase_mask_min_magnitude_pa: float | None = None,
) -> FieldDisplayView:
    """Build the bounded display grid for one CURRENT session's field.

    Raises :class:`FieldDisplayBlocked` for any non-current, unsupported,
    corrupt or unverifiable input — the caller clears the overlay instead
    of showing a partial or stale field.
    """

    currency = field_overlay_currency(session, head)
    if currency.state != 'CURRENT':
        raise FieldDisplayBlocked(
            tuple(f'field overlay is {currency.state}: {r}' for r in currency.reasons)
            or (f'field overlay is {currency.state}',)
        )
    result = session.result
    supported, reason = result.supports_quantity(quantity)
    if not supported:
        raise FieldDisplayBlocked((f'quantity unsupported: {reason}',))
    if quantity == 'phase_deg' and not result.is_complex:
        raise FieldDisplayBlocked(('phase requires complex pressure authority',))
    axes = result.axes
    if [axis.name for axis in axes] != ['x_m', 'y_m', 'z_m']:
        raise FieldDisplayBlocked(('field axes are not x_m/y_m/z_m ordered',))
    frame = (
        result.coordinate_frame_id,
        result.coordinate_frame_version,
    )
    if None in frame:
        raise FieldDisplayBlocked(('field has no coordinate-frame authority',))
    dims = tuple(axis.count for axis in axes)
    if any(count < 1 for count in dims) or not all(
        isfinite(axis.origin_m) and isfinite(axis.spacing_m) and axis.spacing_m > 0
        for axis in axes
    ):
        raise FieldDisplayBlocked(('field grid axes are degenerate',))

    stride = _display_stride(dims, point_budget)
    scalars, clim = _scalars_display(
        result, quantity, stride, phase_mask_min_magnitude_pa
    )
    scalars_render = scalars[:, ::-1, :]  # domain +Y -> render -Y
    render_origin, render_spacing, render_dims = _render_axes(axes, stride)
    masked = int(np.isnan(scalars_render).sum())
    if masked == scalars_render.size:
        raise FieldDisplayBlocked(('every display sample is masked',))
    if clim[0] == clim[1]:
        # A uniform field renders as one colour — expand slightly so VTK's
        # lookup table never sees a degenerate range.
        pad = abs(clim[0]) * 1e-6 or 1e-6
        clim = (clim[0] - pad, clim[1] + pad)

    return FieldDisplayView(
        session_id=session.session_id,
        result_semantic_sha256=result.semantic_sha256,
        document_id=session.document_id,
        scene_revision_id=session.scene_revision_id,
        scene_content_hash=session.scene_content_hash,
        prediction_run_id=session.prediction_run_id,
        producer=session.producer,
        model_id=session.model_id,
        model_version=session.model_version,
        mode_indices=(session.mode_n_x, session.mode_n_y, session.mode_n_z),
        frequency_hz=session.result.frequency_hz,
        coordinate_frame_id=result.coordinate_frame_id,
        coordinate_frame_version=result.coordinate_frame_version,
        absolute_pressure_reference=result.absolute_pressure_reference,
        quantity=quantity,
        unit=_QUANTITY_UNIT[quantity],
        dims=render_dims,
        render_origin=render_origin,
        render_spacing=render_spacing,
        scalars_xyz=np.ascontiguousarray(scalars_render),
        clim=clim,
        masked_count=masked,
        cyclic_colormap=quantity == 'phase_deg',
        sample_state='exact' if stride == 1 else 'decimated',
        display_stride=stride,
        source_point_count=int(np.prod(dims)),
        domain_axes=axes,
    )


_FIXED_AXIS = {'xy': 2, 'xz': 1, 'yz': 0}


@dataclass(frozen=True, slots=True)
class FieldSliceItem:
    """One grid-aligned orthogonal slice — exact canonical samples."""

    axis_plane: Literal['xy', 'xz', 'yz']
    display_index: int
    coordinate_m: float  # domain coordinate of the plane
    dims: tuple[int, int, int]
    render_origin: tuple[float, float, float]
    render_spacing: tuple[float, float, float]
    scalars: np.ndarray  # shaped `dims` (one axis == 1), render order


def build_slice_item(
    view: FieldDisplayView,
    axis_plane: Literal['xy', 'xz', 'yz'],
    display_index: int,
) -> FieldSliceItem:
    fixed = _FIXED_AXIS[axis_plane]
    if not (0 <= display_index < view.dims[fixed]):
        raise FieldDisplayBlocked(('slice index outside the display grid',))
    if axis_plane == 'xy':
        scalars = view.scalars_xyz[:, :, display_index : display_index + 1]
        origin = (
            view.render_origin[0],
            view.render_origin[1],
            view.render_origin[2] + display_index * view.render_spacing[2],
        )
        coordinate = _display_axis_coordinate(view, 2, display_index)
    elif axis_plane == 'xz':
        scalars = view.scalars_xyz[:, display_index : display_index + 1, :]
        origin = (
            view.render_origin[0],
            view.render_origin[1] + display_index * view.render_spacing[1],
            view.render_origin[2],
        )
        coordinate = _display_axis_coordinate(view, 1, display_index)
    else:
        scalars = view.scalars_xyz[display_index : display_index + 1, :, :]
        origin = (
            view.render_origin[0] + display_index * view.render_spacing[0],
            view.render_origin[1],
            view.render_origin[2],
        )
        coordinate = _display_axis_coordinate(view, 0, display_index)
    return FieldSliceItem(
        axis_plane=axis_plane,
        display_index=display_index,
        coordinate_m=coordinate,
        dims=scalars.shape,
        render_origin=origin,
        render_spacing=view.render_spacing,
        scalars=scalars,
    )


def _display_axis_coordinate(
    view: FieldDisplayView, axis: int, display_index: int
) -> float:
    """DOMAIN coordinate of display-grid index ``display_index``."""

    src_axis = view.domain_axes[axis]
    return src_axis.coordinate(display_index * view.display_stride)


def nearest_slice_index(
    view: FieldDisplayView,
    axis_plane: Literal['xy', 'xz', 'yz'],
    coordinate_m: float,
) -> int:
    """Snap a domain coordinate to the nearest DISPLAY-grid index."""

    fixed = _FIXED_AXIS[axis_plane]
    axis = view.domain_axes[fixed]
    best = min(
        range(view.dims[fixed]),
        key=lambda i: abs(
            axis.coordinate(i * view.display_stride) - coordinate_m
        ),
    )
    return int(best)


def iso_values_for(view: FieldDisplayView, fraction: float) -> tuple[float, ...]:
    """One isovalue at ``fraction`` of the clim span (magnitude/SPL only).

    Phase is cyclic — a single isosurface of wrapped phase is meaningless,
    so iso requests on phase views fail closed.
    """

    if view.cyclic_colormap:
        raise FieldDisplayBlocked(('iso surfaces are not defined for phase',))
    if view.dims[0] < 2 or view.dims[1] < 2 or view.dims[2] < 2:
        raise FieldDisplayBlocked(('iso surfaces need a 3D-extent grid',))
    lo, hi = view.clim
    if not isfinite(lo) or not isfinite(hi) or hi <= lo:
        raise FieldDisplayBlocked(('colour range is degenerate',))
    fraction = min(1.0, max(0.0, float(fraction)))
    return (lo + fraction * (hi - lo),)


@dataclass(frozen=True, slots=True)
class FieldOverlayScene:
    """Everything the viewport must draw for one frame — or not draw."""

    view: FieldDisplayView
    slices: tuple[FieldSliceItem, ...]
    iso_values: tuple[float, ...]
    volume_enabled: bool
    probe: FieldProbeValue | None
    status_lines: tuple[str, ...]


def probe_display_position(view: FieldDisplayView, probe: FieldProbeValue) -> tuple[float, float, float]:
    """Render-space position of a probe's sampled domain position."""

    position = probe.sampled_position
    return (position.x_m, -position.y_m, position.z_m)


def probe_field_at(
    session: FieldExplorerSession,
    position_domain: Position3,
    quantity: FieldDisplayQuantity,
    *,
    interpolation: Literal['exact_samples', 'trilinear'] = 'exact_samples',
) -> FieldProbeValue:
    """Thin wrapper over the canonical probe so the viewport never re-implements it."""

    return probe_field(
        session.result, position_domain, quantity, interpolation=interpolation
    )
