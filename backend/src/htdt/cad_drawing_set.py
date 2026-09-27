"""Installation drawing set renderer (#644).

:class:`~htdt.report.InstallationDimensionSheet` carries view bounds and
projected entity *points* — canonical data whose rendering is explicitly
downstream. This module is that downstream: a deterministic vector
(SVG) drawing-set renderer over exact ``InstallationOutput`` plus declared
field datums.

Contract properties:

- ``InstallationOutput`` stays the engineering authority; a
  :class:`DrawingSetSpec` is a versioned *presentation* request (page,
  orientation, scale policy, datum policy, label density, renderer
  version) — changing paper size never changes engineering truth, and both
  hashes are pinned on every sheet;
- the initial set covers floor plan, front elevation, side elevation and
  the reflected ceiling plan (RCP shows only entities whose height
  places them on the ceiling plane — nothing is fabricated from
  non-spatial records);
- object outlines render the entity snapshot's hash-bound per-view
  projections (:class:`~htdt.report.InstallationViewOutline`) — a
  ``bounding_envelope`` basis draws dashed and the sheet states its basis;
  entities without an outline get a point symbol that is visually distinct
  from acoustic-reference/lens/image-center markers — outlines are never
  fabricated;
- dimensions are datum-relative (:class:`DrawingDatum` —
  finished floor, front wall, room/screen centerline, user datum) and every
  dimension names the *referenced point* (body center, acoustic reference,
  lens, image aperture) so no raw global XYZ transcription is required;
- proposed / current / as-built lifecycle states get distinct line styles
  with a legend; a dashed hidden style marks unobserved content;
- page composition is deterministic: fit-to-page output is labelled NTS —
  never a fake 1:50 — and when a fixed scale cannot contain the content the
  sheet is flagged for review rather than silently rescaled;
- label collision uses a bounded deterministic placement pass; unresolvable
  overlap sets ``needs_review`` instead of dropping labels;
- ``sheet_content_sha256`` covers the canonical vector payload only —
  ``generated_at_utc`` is metadata, never part of drawing identity.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .report import InstallationEntityOutput, InstallationOutput
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


DRAWING_SPEC_SCHEMA_VERSION = 1
DRAWING_SPEC_AUTHORITY_VERSION = 'drawing-set-spec-1'
DRAWING_SET_AUTHORITY_VERSION = 'installation-drawing-set-1'
DRAWING_RENDERER_VERSION = 'drawing-renderer-1'

SheetKind = Literal['floor_plan', 'front_elevation', 'side_elevation', 'rcp']
PageSize = Literal['a4', 'a3']
Orientation = Literal['portrait', 'landscape']
ScalePolicy = Literal['fit', 'fixed']
LabelDensity = Literal['minimal', 'normal', 'full']
LifecycleState = Literal['proposed', 'current', 'as_built', 'reference']

#: Which physical/reference point a dimension or callout names — a bare dot
#: is never ambiguous about what was dimensioned.
ReferencePoint = Literal[
    'body_center',
    'acoustic_reference',
    'lens_center',
    'image_center',
    'mount_point',
    'entity_origin',
]

#: Deterministic layer names. Layer visibility is presentation state.
DRAWING_LAYERS: tuple[str, ...] = (
    'room',
    'equipment',
    'speakers',
    'seating',
    'video',
    'treatment',
    'infrastructure',
    'dimensions',
    'datums',
    'callouts',
)

_PAGE_MM: dict[str, tuple[float, float]] = {
    'a4': (210.0, 297.0),
    'a3': (297.0, 420.0),
}
_MARGIN_MM = 15.0
_TITLE_BLOCK_MM = 28.0






def _finite(value: object, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise ValueError(f'{field_name} must be a finite number')
    return float(value)


def _fmt_m(value: float) -> str:
    return f'{value:.3f}'.rstrip('0').rstrip('.')


class DrawingDatum(BaseModel):
    """One declared field datum a drawing can dimension from (#537 feed)."""

    model_config = ConfigDict(frozen=True)

    datum_id: str = Field(min_length=1)
    kind: Literal[
        'finished_floor',
        'front_wall',
        'rear_wall',
        'room_centerline',
        'screen_centerline',
        'side_wall',
        'custom',
    ]
    #: Axis the datum measures along: 'x' (right), 'y' (rear), 'z' (up).
    axis: Literal['x', 'y', 'z']
    #: World offset the datum sits at; finished_floor is typically z=0.
    offset_m: float = 0.0
    label: str = Field(min_length=1)

    @field_validator('offset_m')
    @classmethod
    def finite_offset(cls, value: float) -> float:
        return _finite(value, field_name='datum offset')


class DrawingSetSpec(BaseModel):
    """Versioned presentation request — never engineering authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DRAWING_SPEC_SCHEMA_VERSION
    authority_version: Literal['drawing-set-spec-1'] = (
        DRAWING_SPEC_AUTHORITY_VERSION
    )
    spec_id: str = Field(min_length=1)
    spec_version: str = Field(min_length=1)
    sheets: tuple[SheetKind, ...] = Field(min_length=1)
    page_size: PageSize = 'a4'
    orientation: Orientation = 'landscape'
    scale_policy: ScalePolicy = 'fit'
    #: Only meaningful under ``fixed`` — denominator N of a 1:N drawing.
    fixed_scale_denominator: int | None = Field(default=None, gt=0)
    unit: Literal['m', 'mm'] = 'm'
    label_density: LabelDensity = 'normal'
    datum_ids: tuple[str, ...] = ()
    #: Entities with z at/above (ceiling − inset) render on the RCP.
    rcp_ceiling_inset_m: float = Field(default=0.6, ge=0.0)
    renderer_version: str = Field(
        default=DRAWING_RENDERER_VERSION, min_length=1
    )
    spec_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_spec(self) -> 'DrawingSetSpec':
        if len(set(self.sheets)) != len(self.sheets):
            raise ValueError('drawing spec sheets must be unique')
        if len(set(self.datum_ids)) != len(self.datum_ids):
            raise ValueError('drawing spec datum ids must be unique')
        if self.scale_policy == 'fixed' and self.fixed_scale_denominator is None:
            raise ValueError('fixed scale policy requires a denominator')
        if self.spec_semantic_hash != _digest(self.semantic_payload()):
            raise ValueError('DrawingSetSpec semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'spec_id': self.spec_id,
            'spec_version': self.spec_version,
            'sheets': list(self.sheets),
            'page_size': self.page_size,
            'orientation': self.orientation,
            'scale_policy': self.scale_policy,
            'fixed_scale_denominator': self.fixed_scale_denominator,
            'unit': self.unit,
            'label_density': self.label_density,
            'datum_ids': list(self.datum_ids),
            'rcp_ceiling_inset_m': self.rcp_ceiling_inset_m,
            'renderer_version': self.renderer_version,
        }


def build_drawing_set_spec(**kwargs: Any) -> DrawingSetSpec:
    provisional = DrawingSetSpec.model_construct(
        **kwargs, spec_semantic_hash='0' * 64
    )
    return DrawingSetSpec(
        **kwargs, spec_semantic_hash=_digest(provisional.semantic_payload())
    )


class VectorPrimitive(BaseModel):
    """One canonical drawing element on one layer."""

    model_config = ConfigDict(frozen=True)

    # 'polygon' data is a flat (x, y) page-mm sequence — one closed ring
    # per primitive (#893 body outlines).
    kind: Literal['line', 'rect', 'circle', 'text', 'dimension', 'polygon']
    layer: str = Field(min_length=1)
    #: Layer-order canonical geometry payload (mm in page space).
    data: tuple[float, ...] = ()
    text: str | None = None
    state: LifecycleState = 'current'
    dashed: bool = False


class TitleBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    sheet_title: str = Field(min_length=1)
    project_label: str = Field(min_length=1)
    units: Literal['m', 'mm']
    #: 'NTS' or '1:N'. Fit output is always NTS.
    scale_label: str = Field(min_length=1)
    authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    generated_at_utc: str = Field(min_length=1)


class DrawingSheet(BaseModel):
    """One deterministic vector sheet. ``needs_review`` surfaces unmanaged
    collisions or fixed-scale overflow — labels are never silently dropped."""

    model_config = ConfigDict(frozen=True)

    sheet_id: str = Field(min_length=1)
    kind: SheetKind
    page_width_mm: float = Field(gt=0.0)
    page_height_mm: float = Field(gt=0.0)
    primitives: tuple[VectorPrimitive, ...] = ()
    title_block: TitleBlock
    needs_review: bool = False
    review_reasons: tuple[str, ...] = ()
    sheet_content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_sheet(self) -> 'DrawingSheet':
        if self.sheet_content_sha256 != _digest(self.content_payload()):
            raise ValueError('DrawingSheet content hash mismatch')
        return self

    def content_payload(self) -> dict[str, Any]:
        """Canonical content — generated_at stays out of identity."""
        return {
            'sheet_id': self.sheet_id,
            'kind': self.kind,
            'page_width_mm': self.page_width_mm,
            'page_height_mm': self.page_height_mm,
            'primitives': [item.model_dump(mode='json') for item in self.primitives],
            'title_block': {
                k: v for k, v in self.title_block.model_dump(mode='json').items()
                if k != 'generated_at_utc'
            },
            'needs_review': self.needs_review,
            'review_reasons': list(self.review_reasons),
        }

    def to_svg(self) -> str:
        """Deterministic SVG render of the canonical vector payload."""
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{_fmt_m(self.page_width_mm)}mm" '
            f'height="{_fmt_m(self.page_height_mm)}mm" '
            f'viewBox="0 0 {_fmt_m(self.page_width_mm)} '
            f'{_fmt_m(self.page_height_mm)}">',
        ]
        for layer in DRAWING_LAYERS:
            layer_items = [p for p in self.primitives if p.layer == layer]
            if not layer_items:
                continue
            parts.append(f'<g id="layer-{layer}">')
            for item in layer_items:
                parts.append(_primitive_svg(item))
            parts.append('</g>')
        parts.append(self._title_svg())
        parts.append('</svg>')
        return ''.join(parts)

    def _title_svg(self) -> str:
        tb = self.title_block
        top = self.page_height_mm - _TITLE_BLOCK_MM
        left = _MARGIN_MM
        lines = [
            tb.sheet_title,
            tb.project_label,
            f'units={tb.units} scale={tb.scale_label}',
            f'authority={tb.authority_sha256[:12]} spec={tb.spec_sha256[:12]}',
            f'generated={tb.generated_at_utc}',
        ]
        out = [f'<g id="title-block"><rect x="{left}" y="{top}" '
               f'width="{self.page_width_mm - 2 * _MARGIN_MM}" '
               f'height="{_TITLE_BLOCK_MM}" fill="none" '
               f'stroke="#000" stroke-width="0.3"/>']
        for i, line in enumerate(lines):
            out.append(
                f'<text x="{left + 2}" y="{top + 5 + i * 5}" '
                f'font-size="3.5" fill="#000">{_xml_escape(line)}</text>'
            )
        return ''.join(out) + '</g>'


def _xml_escape(text: str) -> str:
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def _state_style(state: LifecycleState, dashed: bool) -> str:
    dash = {
        'current': '',
        'as_built': '',
        'proposed': ' stroke-dasharray="2,1.5"',
        'reference': ' stroke-dasharray="4,1,1,1"',
    }[state]
    if dashed:
        dash = ' stroke-dasharray="1.5,1.5"'
    return dash


def _primitive_svg(item: VectorPrimitive) -> str:
    style = _state_style(item.state, item.dashed)
    stroke = '#666' if item.state in {'proposed', 'reference'} else '#000'
    if item.kind == 'line':
        x1, y1, x2, y2 = item.data
        return (
            f'<line x1="{_fmt_m(x1)}" y1="{_fmt_m(y1)}" x2="{_fmt_m(x2)}" '
            f'y2="{_fmt_m(y2)}" stroke="{stroke}" stroke-width="0.35"{style}/>'
        )
    if item.kind == 'rect':
        x, y, w, h = item.data
        return (
            f'<rect x="{_fmt_m(x)}" y="{_fmt_m(y)}" width="{_fmt_m(w)}" '
            f'height="{_fmt_m(h)}" fill="none" stroke="{stroke}" '
            f'stroke-width="0.35"{style}/>'
        )
    if item.kind == 'circle':
        cx, cy, r = item.data
        return (
            f'<circle cx="{_fmt_m(cx)}" cy="{_fmt_m(cy)}" r="{_fmt_m(r)}" '
            f'fill="none" stroke="{stroke}" stroke-width="0.35"{style}/>'
        )
    if item.kind == 'polygon':
        points = ' '.join(
            f'{_fmt_m(item.data[i])},{_fmt_m(item.data[i + 1])}'
            for i in range(0, len(item.data) - 1, 2)
        )
        return (
            f'<polygon points="{points}" fill="none" stroke="{stroke}" '
            f'stroke-width="0.35"{style}/>'
        )
    if item.kind == 'dimension':
        x1, y1, x2, y2 = item.data[:4]
        tx, ty = item.data[4], item.data[5]
        label = _xml_escape(item.text or '')
        return (
            f'<line x1="{_fmt_m(x1)}" y1="{_fmt_m(y1)}" x2="{_fmt_m(x2)}" '
            f'y2="{_fmt_m(y2)}" stroke="#444" stroke-width="0.2"/>'
            f'<text x="{_fmt_m(tx)}" y="{_fmt_m(ty)}" font-size="2.8" '
            f'fill="#222">{label}</text>'
        )
    # text
    x, y = item.data[:2]
    label = _xml_escape(item.text or '')
    return (
        f'<text x="{_fmt_m(x)}" y="{_fmt_m(y)}" font-size="3" '
        f'fill="#000">{label}</text>'
    )


def _reference_point(entity: InstallationEntityOutput) -> ReferencePoint:
    kind = entity.entity_kind
    if kind == 'speaker' or kind == 'subwoofer':
        return 'acoustic_reference'
    if kind == 'projector':
        return 'lens_center'
    if kind in {'screen', 'display'}:
        return 'image_center'
    return 'body_center' if entity.body_geometry_kind else 'entity_origin'


_POINT_LABEL = {
    'body_center': 'body center',
    'acoustic_reference': 'acoustic reference',
    'lens_center': 'lens center',
    'image_center': 'image center',
    'mount_point': 'mount point',
    'entity_origin': 'entity origin',
}

_ENTITY_LAYER = {
    'speaker': 'speakers',
    'subwoofer': 'speakers',
    'seat': 'seating',
    'row': 'seating',
    'riser': 'seating',
    'projector': 'video',
    'screen': 'video',
    'display': 'video',
    'treatment': 'treatment',
    'rack': 'infrastructure',
    'cable': 'infrastructure',
}


def _view_axes(kind: SheetKind) -> tuple[str, str]:
    return {
        'floor_plan': ('x', 'y'),
        'front_elevation': ('x', 'z'),
        'side_elevation': ('y', 'z'),
        'rcp': ('x', 'y'),
    }[kind]


#: Which ``InstallationViewOutline.view`` each sheet draws — the floor plan
#: and the reflected ceiling plan share the same top-view geometry basis.
_SHEET_VIEW: dict[str, str] = {
    'floor_plan': 'top',
    'front_elevation': 'front',
    'side_elevation': 'side',
    'rcp': 'top',
}


def _entity_outline(entity: InstallationEntityOutput, view: str):
    """The snapshot-bound outline for this view, or ``None``."""
    return next(
        (outline for outline in entity.outlines if outline.view == view),
        None,
    )


def _format_dimension_offset(
    offset_m: float,
    unit: Literal['m', 'mm'],
) -> str:
    """Datum-relative offset text in the sheet's declared unit (#893).

    The offset is computed in meters; a ``mm`` sheet multiplies by 1000
    instead of mislabeling the meter value.
    """
    if unit == 'mm':
        return f'{_fmt_m(offset_m * 1000.0)} mm'
    return f'{_fmt_m(offset_m)} m'


def _entity_xy(
    entity: InstallationEntityOutput, axes: tuple[str, str]
) -> tuple[float, float]:
    coord = {'x': entity.x_m, 'y': entity.y_m, 'z': entity.z_m}
    return coord[axes[0]], coord[axes[1]]


class _Layout:
    """View transform and label bookkeeping for one sheet."""

    def __init__(
        self,
        *,
        page_w: float,
        page_h: float,
        view_min: tuple[float, float],
        view_max: tuple[float, float],
        scale: float,
    ) -> None:
        self.page_w = page_w
        self.page_h = page_h
        self.view_min = view_min
        self.scale = scale
        self.label_boxes: list[tuple[float, float, float, float]] = []

    def to_page(self, h: float, v: float) -> tuple[float, float]:
        """Map view meters to page mm (vertical axis flips up→down)."""
        x = _MARGIN_MM + (h - self.view_min[0]) * self.scale
        draw_bottom = self.page_h - _TITLE_BLOCK_MM - 2.0
        y = draw_bottom - (v - self.view_min[1]) * self.scale
        return x, y

    def place_label(self, x: float, y: float, text: str) -> bool:
        """Deterministic label slot check; returns False on collision."""
        w = max(6.0, 2.1 * len(text))
        box = (x, y - 3.0, x + w, y + 0.5)
        for other in self.label_boxes:
            if not (
                box[2] < other[0] or box[0] > other[2]
                or box[3] < other[1] or box[1] > other[3]
            ):
                return False
        self.label_boxes.append(box)
        return True


def _compute_scale(
    *,
    spec: DrawingSetSpec,
    page_w: float,
    page_h: float,
    span_h: float,
    span_v: float,
) -> tuple[float, str, list[str]]:
    """Return (mm-per-meter, scale_label, review_reasons)."""
    usable_w = page_w - 2 * _MARGIN_MM
    usable_h = page_h - _TITLE_BLOCK_MM - _MARGIN_MM
    if span_h <= 0 or span_v <= 0:
        return 20.0, 'NTS', ['view span is degenerate']
    if spec.scale_policy == 'fit':
        return min(usable_w / span_h, usable_h / span_v), 'NTS', []
    assert spec.fixed_scale_denominator is not None
    # The declared scale is the truth — when the content does not fit the
    # page at 1:N the sheet keeps the fixed scale (content may clip) and is
    # flagged for review; it is never silently rescaled under a 1:N label.
    scale = 1000.0 / spec.fixed_scale_denominator
    reasons: list[str] = []
    if span_h * scale > usable_w or span_v * scale > usable_h:
        reasons.append(
            f'content exceeds page at fixed 1:{spec.fixed_scale_denominator}'
        )
    return scale, f'1:{spec.fixed_scale_denominator}', reasons


def _datums_for_axes(
    datums: Sequence[DrawingDatum], axes: tuple[str, str]
) -> tuple[DrawingDatum | None, DrawingDatum | None]:
    horizontal = next((d for d in datums if d.axis == axes[0]), None)
    vertical = next((d for d in datums if d.axis == axes[1]), None)
    return horizontal, vertical


def _render_sheet(
    *,
    kind: SheetKind,
    spec: DrawingSetSpec,
    output: InstallationOutput,
    entities: Sequence[InstallationEntityOutput],
    datums: Sequence[DrawingDatum],
    bounds: tuple[float, float, float, float],
    project_label: str,
    generated_at_utc: str,
    states: dict[str, LifecycleState],
) -> DrawingSheet:
    page_w, page_h = _PAGE_MM[spec.page_size]
    if spec.orientation == 'landscape':
        page_w, page_h = page_h, page_w
    axes = _view_axes(kind)
    h_min, v_min, h_max, v_max = bounds
    span_h = h_max - h_min
    span_v = v_max - v_min
    scale, scale_label, reasons = _compute_scale(
        spec=spec, page_w=page_w, page_h=page_h,
        span_h=span_h, span_v=span_v,
    )
    layout = _Layout(
        page_w=page_w, page_h=page_h,
        view_min=(h_min, v_min), view_max=(h_max, v_max),
        scale=scale,
    )
    primitives: list[VectorPrimitive] = []

    # Room outline.
    x0, y0 = layout.to_page(h_min, v_min)
    x1, y1 = layout.to_page(h_max, v_max)
    primitives.append(VectorPrimitive(
        kind='rect', layer='room',
        data=(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)),
    ))

    # Datum lines.
    for datum in datums:
        if datum.axis == axes[0]:
            px, _ = layout.to_page(datum.offset_m, v_min)
            primitives.append(VectorPrimitive(
                kind='line', layer='datums',
                data=(px, y0, px, y1), dashed=True,
            ))
            primitives.append(VectorPrimitive(
                kind='text', layer='datums',
                data=(px + 0.8, max(y0, y1) + 3.0), text=datum.label,
            ))
        elif datum.axis == axes[1]:
            _, py = layout.to_page(h_min, datum.offset_m)
            primitives.append(VectorPrimitive(
                kind='line', layer='datums',
                data=(x0, py, x1, py), dashed=True,
            ))
            primitives.append(VectorPrimitive(
                kind='text', layer='datums',
                data=(x0 + 0.8, py - 0.8), text=datum.label,
            ))

    datum_h, datum_v = _datums_for_axes(datums, axes)
    density = spec.label_density

    outline_view = _SHEET_VIEW[kind]
    envelope_basis_seen = False

    for entity in entities:
        h, v = _entity_xy(entity, axes)
        px, py = layout.to_page(h, v)
        layer = _ENTITY_LAYER.get(entity.entity_kind, 'equipment')
        state = states.get(entity.entity_id, 'current')
        ref_point = _reference_point(entity)

        # Geometry: the snapshot's real projected outline where it exists —
        # never a fabricated square; distinct point symbols otherwise.
        outline = _entity_outline(entity, outline_view)
        if outline is not None and outline.polygons_m:
            envelope_basis_seen = (
                envelope_basis_seen or outline.basis == 'bounding_envelope'
            )
            for ring in outline.polygons_m:
                points: list[float] = []
                for ring_h, ring_v in ring:
                    rx, ry = layout.to_page(ring_h, ring_v)
                    points.extend((rx, ry))
                primitives.append(VectorPrimitive(
                    kind='polygon', layer=layer, data=tuple(points),
                    state=state,
                    dashed=outline.basis == 'bounding_envelope',
                ))
        elif ref_point == 'acoustic_reference':
            primitives.append(VectorPrimitive(
                kind='circle', layer=layer,
                data=(px, py, 1.6), state=state,
            ))
        elif ref_point in {'lens_center', 'image_center'}:
            primitives.append(VectorPrimitive(
                kind='line', layer=layer,
                data=(px - 2.0, py, px + 2.0, py), state=state,
            ))
            primitives.append(VectorPrimitive(
                kind='line', layer=layer,
                data=(px, py - 2.0, px, py + 2.0), state=state,
            ))
        else:
            primitives.append(VectorPrimitive(
                kind='circle', layer=layer,
                data=(px, py, 1.0), state=state, dashed=True,
            ))

        if density != 'minimal':
            label = entity.name
            if density == 'full':
                label = f'{entity.name} [{_POINT_LABEL[ref_point]}]'
            tx, ty = px + 2.5, py - 1.5
            placed = layout.place_label(tx, ty, label)
            while not placed and ty < page_h - _TITLE_BLOCK_MM - 6.0:
                ty += 4.5
                placed = layout.place_label(tx, ty, label)
            if placed:
                primitives.append(VectorPrimitive(
                    kind='text', layer='callouts',
                    data=(tx, ty), text=label, state=state,
                ))
            else:
                reasons.append(f'label for {entity.entity_id!r} overlaps')

        # Datum-relative dimensions naming the referenced point.
        for datum, value, axis_name in (
            (datum_h, h, axes[0]),
            (datum_v, v, axes[1]),
        ):
            if datum is None:
                continue
            offset = value - datum.offset_m
            dim_text = (
                f'{_format_dimension_offset(offset, spec.unit)} from '
                f'{datum.label} ({_POINT_LABEL[ref_point]})'
            )
            if datum.axis == axes[0]:
                primitives.append(VectorPrimitive(
                    kind='dimension', layer='dimensions',
                    data=(px, py, px, py - 8.0, px + 1.0, py - 9.0),
                    text=dim_text,
                ))
            else:
                primitives.append(VectorPrimitive(
                    kind='dimension', layer='dimensions',
                    data=(px, py, px - 8.0, py, px - 9.0, py + 1.0),
                    text=dim_text,
                ))

    if envelope_basis_seen:
        primitives.append(VectorPrimitive(
            kind='text', layer='callouts',
            data=(_MARGIN_MM, page_h - _TITLE_BLOCK_MM - 4.0),
            text=(
                'dashed outline = bounding-envelope basis '
                '(exact body geometry unverified)'
            ),
        ))

    title = {
        'floor_plan': 'Floor plan',
        'front_elevation': 'Front elevation',
        'side_elevation': 'Side elevation',
        'rcp': 'Reflected ceiling plan',
    }[kind]
    title_block = TitleBlock(
        sheet_title=title,
        project_label=project_label,
        units=spec.unit,
        scale_label=scale_label,
        authority_sha256=output.semantic_sha256,
        spec_sha256=spec.spec_semantic_hash,
        generated_at_utc=generated_at_utc,
    )
    provisional = DrawingSheet.model_construct(
        sheet_id=f'{spec.spec_id}:{kind}',
        kind=kind,
        page_width_mm=page_w,
        page_height_mm=page_h,
        primitives=tuple(primitives),
        title_block=title_block,
        needs_review=bool(reasons),
        review_reasons=tuple(sorted(set(reasons))),
        sheet_content_sha256='0' * 64,
    )
    return DrawingSheet(
        sheet_id=provisional.sheet_id,
        kind=kind,
        page_width_mm=page_w,
        page_height_mm=page_h,
        primitives=tuple(primitives),
        title_block=title_block,
        needs_review=provisional.needs_review,
        review_reasons=provisional.review_reasons,
        sheet_content_sha256=_digest(provisional.content_payload()),
    )


class InstallationDrawingSet(BaseModel):
    """The rendered set — presentation output bound to exact authority."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['installation-drawing-set-1'] = (
        DRAWING_SET_AUTHORITY_VERSION
    )
    drawing_set_id: str = Field(min_length=1)
    installation_output_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sheets: tuple[DrawingSheet, ...]
    generated_at_utc: str = Field(min_length=1)

    def sheet(self, kind: SheetKind) -> DrawingSheet | None:
        return next((s for s in self.sheets if s.kind == kind), None)


def generate_drawing_set(
    *,
    output: InstallationOutput,
    spec: DrawingSetSpec,
    datums: Sequence[DrawingDatum] = (),
    project_label: str,
    generated_at_utc: str,
    entity_states: dict[str, LifecycleState] | None = None,
) -> InstallationDrawingSet:
    """Render the requested sheets from exact installation authority.

    Only entities present on ``output`` are drawn; the RCP includes only
    entities at the ceiling plane (within ``rcp_ceiling_inset_m``), so no
    ceiling-plane object is fabricated from non-spatial records.
    """

    states = entity_states or {}
    top = next((s for s in output.dimensions if s.view == 'top'), None)
    front = next((s for s in output.dimensions if s.view == 'front'), None)
    if top is None or front is None:
        raise ValueError(
            'installation output has no top/front dimension sheets to render'
        )
    ceiling_z = front.vertical_max_m

    view_entities: dict[SheetKind, list[InstallationEntityOutput]] = {
        'floor_plan': list(output.entities),
        'front_elevation': list(output.entities),
        'side_elevation': list(output.entities),
        'rcp': [
            entity
            for entity in output.entities
            if entity.z_m >= ceiling_z - spec.rcp_ceiling_inset_m
        ],
    }
    bounds_by_kind: dict[SheetKind, tuple[float, float, float, float]] = {
        'floor_plan': (
            top.horizontal_min_m, top.vertical_min_m,
            top.horizontal_max_m, top.vertical_max_m,
        ),
        'front_elevation': (
            front.horizontal_min_m, front.vertical_min_m,
            front.horizontal_max_m, front.vertical_max_m,
        ),
        'side_elevation': (
            next(s for s in output.dimensions if s.view == 'side')
            .horizontal_min_m,
            next(s for s in output.dimensions if s.view == 'side')
            .vertical_min_m,
            next(s for s in output.dimensions if s.view == 'side')
            .horizontal_max_m,
            next(s for s in output.dimensions if s.view == 'side')
            .vertical_max_m,
        ),
        'rcp': (
            top.horizontal_min_m, top.vertical_min_m,
            top.horizontal_max_m, top.vertical_max_m,
        ),
    }

    wanted = set(spec.datum_ids)
    active_datums = tuple(
        datum for datum in datums if not wanted or datum.datum_id in wanted
    )

    sheets = tuple(
        _render_sheet(
            kind=kind,
            spec=spec,
            output=output,
            entities=view_entities[kind],
            datums=active_datums,
            bounds=bounds_by_kind[kind],
            project_label=project_label,
            generated_at_utc=generated_at_utc,
            states=states,
        )
        for kind in spec.sheets
    )
    return InstallationDrawingSet(
        drawing_set_id=f'drawing-set-{output.semantic_sha256[:16]}-{_digest(spec.semantic_payload())[:8]}',
        installation_output_sha256=output.semantic_sha256,
        spec_sha256=spec.spec_semantic_hash,
        sheets=sheets,
        generated_at_utc=generated_at_utc,
    )


__all__ = [
    'DRAWING_LAYERS',
    'DRAWING_RENDERER_VERSION',
    'DRAWING_SET_AUTHORITY_VERSION',
    'DRAWING_SPEC_AUTHORITY_VERSION',
    'DRAWING_SPEC_SCHEMA_VERSION',
    'DrawingDatum',
    'DrawingSetSpec',
    'DrawingSheet',
    'InstallationDrawingSet',
    'LabelDensity',
    'LifecycleState',
    'Orientation',
    'PageSize',
    'ReferencePoint',
    'ScalePolicy',
    'SheetKind',
    'TitleBlock',
    'VectorPrimitive',
    'build_drawing_set_spec',
    'generate_drawing_set',
]
