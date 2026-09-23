"""Floor-plan underlay domain (#534).

A floor-plan underlay is a *reference* drawing (PNG/JPEG raster, rasterized
PDF page, or a bounded simple-DXF line set) rendered beneath the room plan so
the editor can trace geometry — it is NEVER treated as Scene truth, never
writes SceneRevisions, and never feeds measurement/solver pipelines.

Scale authority: an underlay only acquires real-world scale through an
explicit calibration — either a two-point calibration (two picked source
points plus their true distance in meters) or a manual units-per-meter
entry. No scale is ever inferred from image DPI/EXIF metadata.

Persistence: one row per underlay in ``floor_plan_underlays`` (payload =
``FloorPlanUnderlay`` JSON), original/raster bytes in the content-addressed
blob store, plus provenance (file name, source format, import timestamp).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import cos, hypot, radians, sin
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DXF_SEGMENTS = 20_000
MAX_SNAP_HINTS = 4_000
MAX_NAME_LENGTH = 64

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg'}
DXF_SUFFIXES = {'.dxf'}
PDF_SUFFIXES = {'.pdf'}

#: Display-only scale used to draw an *uncalibrated* underlay (1 source unit
#: = 1 cm). Purely a preview until the user calibrates — never consumed by
#: tracing snap hints or measurements.
UNCALIBRATED_UNITS_PER_METER = 100.0


class UnderlayImportError(ValueError):
    """A rejected floor-plan underlay import."""


class UnderlaySourceFormat(str, Enum):
    IMAGE = 'image'
    PDF_PAGE = 'pdf_page'
    DXF = 'dxf'


class UnderlayCalibrationMethod(str, Enum):
    UNCALIBRATED = 'uncalibrated'
    TWO_POINT = 'two_point'
    MANUAL = 'manual'


class FloorPlanUnderlay(BaseModel):
    """One persisted floor-plan underlay record (document-scoped).

    Source space: raster images use pixel coordinates (u→right, v→down);
    DXF files use the drawing's raw model-space units. ``units_per_meter``
    maps source units to meters; ``origin_*`` + ``rotation_deg`` form the
    editor-authority placement transform:

        p_domain = R(rotation_deg) · (p_source / units_per_meter) + origin
    """

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    underlay_id: str
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    source_format: UnderlaySourceFormat
    source_file_name: str | None = None
    imported_at_utc: str
    # Blob store digests — the original file bytes plus (for PDF pages) the
    # rasterized render actually drawn.
    source_blob_sha256: str
    render_blob_sha256: str | None = None
    source_page: int = 0
    # Source-space extents (px or raw DXF units); informational.
    source_width: float | None = None
    source_height: float | None = None
    # Calibration — None until an explicit two-point/manual calibration.
    units_per_meter: float | None = None
    calibration: UnderlayCalibrationMethod = UnderlayCalibrationMethod.UNCALIBRATED
    calibration_point_a: tuple[float, float] | None = None
    calibration_point_b: tuple[float, float] | None = None
    calibration_distance_m: float | None = None
    # Editor-authority placement (domain coordinates).
    origin_x_m: float = 0.0
    origin_y_m: float = 0.0
    rotation_deg: float = 0.0
    visible: bool = True
    locked: bool = False
    opacity: float = 0.6
    elevation_m: float = 0.002
    # Source-space snap hints (DXF endpoints/vertices, calibration points).
    snap_hints: tuple[tuple[float, float], ...] = ()
    # Source-space line segments (DXF vector content).
    segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = ()


def _scale(underlay: FloorPlanUnderlay) -> float:
    upm = underlay.units_per_meter
    if upm is None or upm <= 0.0:
        return UNCALIBRATED_UNITS_PER_METER
    return upm


def is_calibrated(underlay: FloorPlanUnderlay) -> bool:
    return (
        underlay.units_per_meter is not None
        and underlay.units_per_meter > 0.0
        and underlay.calibration is not UnderlayCalibrationMethod.UNCALIBRATED
    )


def source_to_domain(
    underlay: FloorPlanUnderlay,
    u: float,
    v: float,
) -> tuple[float, float]:
    """Map a source-space point to a domain XY position (meters)."""

    scale = _scale(underlay)
    x = float(u) / scale
    y = float(v) / scale
    angle = radians(underlay.rotation_deg)
    rotated_x = x * cos(angle) - y * sin(angle)
    rotated_y = x * sin(angle) + y * cos(angle)
    return (
        underlay.origin_x_m + rotated_x,
        underlay.origin_y_m + rotated_y,
    )


def domain_to_source(
    underlay: FloorPlanUnderlay,
    x_m: float,
    y_m: float,
) -> tuple[float, float]:
    """Inverse of :func:`source_to_domain`."""

    angle = radians(-underlay.rotation_deg)
    dx = float(x_m) - underlay.origin_x_m
    dy = float(y_m) - underlay.origin_y_m
    x = dx * cos(angle) - dy * sin(angle)
    y = dx * sin(angle) + dy * cos(angle)
    scale = _scale(underlay)
    return (x * scale, y * scale)


def underlay_quad_domain(
    underlay: FloorPlanUnderlay,
) -> tuple[tuple[float, float, float], ...] | None:
    """Domain-space corners of the raster quad (None for vector-only)."""

    if underlay.source_format is UnderlaySourceFormat.DXF:
        return None
    if underlay.source_width is None or underlay.source_height is None:
        return None
    z = underlay.elevation_m
    corners = (
        (0.0, 0.0),
        (underlay.source_width, 0.0),
        (underlay.source_width, underlay.source_height),
        (0.0, underlay.source_height),
    )
    return tuple(
        (x, y, z) for (x, y) in (source_to_domain(underlay, u, v) for u, v in corners)
    )


def underlay_segments_domain(
    underlay: FloorPlanUnderlay,
) -> tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...]:
    """Domain-space line segments (DXF vector content)."""

    z = underlay.elevation_m
    result: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for (a, b) in underlay.segments[:MAX_DXF_SEGMENTS]:
        ax, ay = source_to_domain(underlay, a[0], a[1])
        bx, by = source_to_domain(underlay, b[0], b[1])
        result.append(((ax, ay, z), (bx, by, z)))
    return tuple(result)


def underlay_snap_points(
    underlay: FloorPlanUnderlay,
) -> tuple[tuple[float, float], ...]:
    """Domain-space snap hint points — active ONLY once calibrated."""

    if not is_calibrated(underlay) or not underlay.visible:
        return ()
    points: list[tuple[float, float]] = []
    for u, v in underlay.snap_hints[:MAX_SNAP_HINTS]:
        points.append(source_to_domain(underlay, u, v))
    return tuple(points)


def calibrate_two_point(
    underlay: FloorPlanUnderlay,
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    distance_m: float,
) -> FloorPlanUnderlay:
    """Return a copy with scale set from two picked source points + their
    true distance in meters. Never derives scale from DPI metadata."""

    if distance_m <= 0.0:
        raise UnderlayImportError('キャリブレーション距離は正の値が必要です')
    source_distance = hypot(point_b[0] - point_a[0], point_b[1] - point_a[1])
    if source_distance <= 1e-9:
        raise UnderlayImportError('2点が近すぎます。離れた2点を選んでください')
    units_per_meter = source_distance / float(distance_m)
    hints = list(underlay.snap_hints)
    for point in (point_a, point_b):
        if point not in hints:
            hints.append(point)
    return underlay.model_copy(
        update={
            'units_per_meter': units_per_meter,
            'calibration': UnderlayCalibrationMethod.TWO_POINT,
            'calibration_point_a': tuple(point_a),
            'calibration_point_b': tuple(point_b),
            'calibration_distance_m': float(distance_m),
            'snap_hints': tuple(hints[:MAX_SNAP_HINTS]),
        }
    )


def calibrate_manual(
    underlay: FloorPlanUnderlay,
    units_per_meter: float,
) -> FloorPlanUnderlay:
    if units_per_meter <= 0.0:
        raise UnderlayImportError('スケールは正の値が必要です')
    return underlay.model_copy(
        update={
            'units_per_meter': float(units_per_meter),
            'calibration': UnderlayCalibrationMethod.MANUAL,
            'calibration_point_a': None,
            'calibration_point_b': None,
            'calibration_distance_m': None,
        }
    )


# ---------------------------------------------------------------------------
# DXF parsing (bounded: LINE / LWPOLYLINE / POINT entities only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DxfContents:
    segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...]
    points: tuple[tuple[float, float], ...]
    truncated: bool


def parse_dxf(data: bytes) -> DxfContents:
    """Extract 2D segments/points from a simple ASCII DXF.

    Bounded on purpose: only LINE, LWPOLYLINE and POINT entities are read,
    model-space only, and counts are capped — this is a tracing reference,
    not a CAD kernel.
    """

    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = data.decode('utf-8', errors='replace')
    lines = [line.rstrip('\r') for line in text.split('\n')]
    # Group-code pairs: (int code, raw value string)
    pairs: list[tuple[int, str]] = []
    index = 0
    while index + 1 < len(lines):
        try:
            code = int(lines[index].strip())
        except ValueError:
            index += 1
            continue
        pairs.append((code, lines[index + 1].strip()))
        index += 2

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    points: list[tuple[float, float]] = []
    truncated = False

    index = 0
    while index < len(pairs):
        code, value = pairs[index]
        if code != 0:
            index += 1
            continue
        entity_type = value.upper()
        index += 1
        if entity_type == 'LINE':
            start: tuple[float, float] | None = None
            end: tuple[float, float] | None = None
            sx = sy = ex = ey = None
            while index < len(pairs) and pairs[index][0] != 0:
                group, raw = pairs[index]
                if group == 10:
                    sx = _to_float(raw)
                elif group == 20:
                    sy = _to_float(raw)
                elif group == 11:
                    ex = _to_float(raw)
                elif group == 21:
                    ey = _to_float(raw)
                index += 1
            if None not in (sx, sy, ex, ey):
                start = (float(sx), float(sy))
                end = (float(ex), float(ey))
                segments.append((start, end))
                points.extend((start, end))
        elif entity_type == 'LWPOLYLINE':
            closed = False
            vertices: list[tuple[float, float]] = []
            current_x: float | None = None
            while index < len(pairs) and pairs[index][0] != 0:
                group, raw = pairs[index]
                if group == 70:
                    try:
                        closed = bool(int(raw) & 1)
                    except ValueError:
                        closed = False
                elif group == 10:
                    current_x = _to_float(raw)
                elif group == 20 and current_x is not None:
                    y = _to_float(raw)
                    if y is not None:
                        vertices.append((float(current_x), float(y)))
                        current_x = None
                index += 1
            for a, b in zip(vertices, vertices[1:]):
                segments.append((a, b))
            if closed and len(vertices) > 2:
                segments.append((vertices[-1], vertices[0]))
            points.extend(vertices)
        elif entity_type == 'POINT':
            px = py = None
            while index < len(pairs) and pairs[index][0] != 0:
                group, raw = pairs[index]
                if group == 10:
                    px = _to_float(raw)
                elif group == 20:
                    py = _to_float(raw)
                index += 1
            if px is not None and py is not None:
                points.append((float(px), float(py)))
        else:
            # Skip unknown entities.
            while index < len(pairs) and pairs[index][0] != 0:
                index += 1
        if len(segments) > MAX_DXF_SEGMENTS:
            truncated = True
            segments = segments[:MAX_DXF_SEGMENTS]
            break

    # Deduplicate + cap hint points.
    seen: set[tuple[float, float]] = set()
    unique_points: list[tuple[float, float]] = []
    for point in points:
        if point in seen:
            continue
        seen.add(point)
        unique_points.append(point)
        if len(unique_points) >= MAX_SNAP_HINTS:
            truncated = True
            break
    return DxfContents(
        segments=tuple(segments),
        points=tuple(unique_points),
        truncated=truncated,
    )


def _to_float(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Raster helpers (lazy Qt/PyMuPDF imports so headless tests stay light)
# ---------------------------------------------------------------------------

def decode_image_bytes(data: bytes):
    """Decode PNG/JPEG bytes to a uint8 RGBA ndarray (lazy PySide6 import)."""

    import numpy as np
    from PySide6.QtGui import QImage

    image = QImage.fromData(data)
    if image.isNull():
        raise UnderlayImportError('画像をデコードできませんでした (PNG/JPEG のみ対応)')
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = image.width(), image.height()
    buffer = bytes(image.bits())[: width * height * 4]
    array = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
    return array


def render_pdf_page(data: bytes, page: int = 0, zoom: float = 2.0) -> bytes:
    """Rasterize one PDF page to PNG bytes via PyMuPDF (lazy import).

    Raises :class:`UnderlayImportError` when PyMuPDF is unavailable — PDF
    import is optional; images and DXF work without it.
    """

    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnderlayImportError(
            'PDF 下図には PyMuPDF が必要です (未インストール)'
        ) from exc
    try:
        document = pymupdf.open(stream=data, filetype='pdf')
    except Exception as exc:  # pymupdf raises FileDataError et al.
        raise UnderlayImportError(f'PDF を開けませんでした: {exc}') from exc
    try:
        if document.page_count < 1:
            raise UnderlayImportError('PDF にページがありません')
        page_index = max(0, min(int(page), document.page_count - 1))
        pixmap = document.load_page(page_index).get_pixmap(
            matrix=pymupdf.Matrix(zoom, zoom),
            alpha=False,
        )
        return pixmap.tobytes('png')
    finally:
        document.close()


def new_underlay_id() -> str:
    return f'underlay-{uuid4().hex[:10]}'


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    'DXF_SUFFIXES',
    'IMAGE_SUFFIXES',
    'MAX_DXF_SEGMENTS',
    'MAX_SNAP_HINTS',
    'MAX_SOURCE_BYTES',
    'PDF_SUFFIXES',
    'UNCALIBRATED_UNITS_PER_METER',
    'DxfContents',
    'FloorPlanUnderlay',
    'UnderlayCalibrationMethod',
    'UnderlayImportError',
    'UnderlaySourceFormat',
    'calibrate_manual',
    'calibrate_two_point',
    'decode_image_bytes',
    'domain_to_source',
    'is_calibrated',
    'new_underlay_id',
    'parse_dxf',
    'render_pdf_page',
    'source_to_domain',
    'underlay_quad_domain',
    'underlay_segments_domain',
    'underlay_snap_points',
    'utc_now_iso',
]
