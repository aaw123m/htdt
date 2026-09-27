"""Analysis export (#512).

Native workflows produce measurement, prediction and comparison data that
must be exportable *reproducibly*: the same authorities must always produce
byte-identical content, every value must carry its class (raw measurement,
derived, predicted, display-transformed), and exports must state when the
underlying evidence is historical rather than current.

This module renders deterministic CSV, JSON, and a self-contained HTML
report (one inline SVG plot per incompatible unit group + embedded
machine-readable JSON payload). It never recomputes source numerics — it
packages already-persisted analysis series with their authority pins.

Identity model:

- ``spec_sha256`` seals the *reproducible spec* — document, title, series
  and metadata. Volatile record identity (``export_id``,
  ``generated_at_utc``) is deliberately outside the spec so two exports of
  identical evidence share one spec hash.
- ``export_sha256`` seals the full record (spec + volatile fields) so a
  persisted/exported record is self-verifying end to end.

Series provenance is typed: every source-backed value class requires an
exact ``source_kind``/``source_id``/``source_sha256`` pin, and derived or
display-transformed series additionally pin the processing operation
identity that produced them. ``historical`` is never a caller flag — the
typed adapters (:func:`series_from_measurement_dataset`,
:func:`series_from_comparison`, :func:`series_from_prediction`) derive it
from the authority's scene binding versus the declared current head.
Point order inside a series is the source authority's canonical order and
is never rewritten for byte determinism.
"""

from __future__ import annotations

import csv
import html
import io
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementComparison,
    CadMeasurementRecord,
)
from .csv_export import csv_safe_row
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


ANALYSIS_EXPORT_SCHEMA_VERSION = 1
ANALYSIS_EXPORT_AUTHORITY_VERSION = 'analysis-export-1'

#: What a series' values physically are.
AnalysisValueClass = Literal[
    'raw',
    'derived',
    'predicted',
    'display_transformed',
]

_SERIES_COLORS = (
    '#1f77b4',
    '#d62728',
    '#2ca02c',
    '#9467bd',
    '#ff7f0e',
    '#17becf',
)






def _format_number(value: float) -> str:
    return format(value, '.12g')


def _embed_json(payload: Any) -> str:
    """JSON for an HTML ``application/json`` block that round-trips exactly.

    Markup-active characters are escaped *inside the JSON encoding* itself
    (``\\uXXXX``), not via HTML escaping, so ``JSON.parse`` on the script
    contents reproduces the payload byte-for-byte.
    """

    canonical = _canonical_json(payload)
    return (
        canonical
        .replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


class AnalysisSeriesPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float


class AnalysisSeries(BaseModel):
    """One exportable series pinned to its source authority."""

    model_config = ConfigDict(frozen=True)

    series_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value_class: AnalysisValueClass
    x_label: str | None = None
    y_label: str | None = None
    unit: str | None = None
    points: tuple[AnalysisSeriesPoint, ...] = ()
    source_kind: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: The exact processing operation that produced the values for
    #: ``derived``/``display_transformed`` series — required there, and
    #: forbidden on ``raw``/``predicted`` where the source authority
    #: already carries the full provenance.
    operation: str | None = Field(default=None, min_length=1)
    operation_version: str | None = Field(default=None, min_length=1)
    operation_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: Derived by the typed adapters (source revision vs current head) —
    #: never a caller-claimed flag.
    historical: bool = False

    @model_validator(mode='after')
    def valid_series(self) -> 'AnalysisSeries':
        if self.source_sha256 is not None and (
            self.source_kind is None or self.source_id is None
        ):
            raise ValueError('source_sha256 requires source_kind+source_id')
        if self.source_id is not None and self.source_kind is None:
            raise ValueError('source_id requires source_kind')
        # Every exported series binds the exact authority its values came
        # from — a series with no provenance cannot be reproduced.
        if self.source_sha256 is None:
            raise ValueError(
                f"value_class {self.value_class!r} requires an exact "
                'source authority pin (source_kind+source_id+source_sha256)'
            )
        processed = self.value_class in ('derived', 'display_transformed')
        if processed and (
            self.operation is None or self.operation_version is None
        ):
            raise ValueError(
                f"value_class {self.value_class!r} requires the processing "
                'operation+operation_version that produced its values'
            )
        if not processed and (
            self.operation is not None
            or self.operation_version is not None
            or self.operation_sha256 is not None
        ):
            raise ValueError(
                f"value_class {self.value_class!r} cannot carry processing "
                'operation provenance — the source authority pin is its '
                'full identity'
            )
        if self.operation_version is not None and self.operation is None:
            raise ValueError('operation_version requires operation')
        if self.operation_sha256 is not None and (
            self.operation_version is None
        ):
            raise ValueError(
                'operation_sha256 requires operation+operation_version'
            )
        return self


class AnalysisExportMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class AnalysisExportBundle(BaseModel):
    """A deterministic, hashed export of analysis series + metadata."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ANALYSIS_EXPORT_SCHEMA_VERSION
    authority_version: Literal['analysis-export-1'] = (
        ANALYSIS_EXPORT_AUTHORITY_VERSION
    )
    export_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    series: tuple[AnalysisSeries, ...] = ()
    metadata: tuple[AnalysisExportMeta, ...] = ()
    generated_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    export_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_bundle(self) -> 'AnalysisExportBundle':
        ids = [series.series_id for series in self.series]
        if len(ids) != len(set(ids)):
            raise ValueError('export series ids must be unique')
        keys = [meta.key for meta in self.metadata]
        if len(keys) != len(set(keys)):
            raise ValueError('export metadata keys must be unique')
        if self.spec_sha256 != _hash(self.spec_payload()):
            raise ValueError('AnalysisExportBundle spec hash mismatch')
        if self.export_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AnalysisExportBundle hash mismatch')
        return self

    def spec_payload(self) -> dict[str, Any]:
        """The reproducible spec — identical evidence → identical hash.

        ``export_id`` and ``generated_at_utc`` are volatile record identity
        and intentionally excluded.
        """

        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'title': self.title,
            'series': [s.model_dump(mode='json') for s in self.series],
            'metadata': [m.model_dump(mode='json') for m in self.metadata],
        }

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.spec_payload()
        payload['export_id'] = self.export_id
        payload['generated_at_utc'] = self.generated_at_utc
        return payload


def build_analysis_export(
    *,
    document_id: str,
    title: str,
    generated_at_utc: str,
    series: tuple[AnalysisSeries, ...] = (),
    metadata: tuple[AnalysisExportMeta, ...] = (),
    export_id: str | None = None,
) -> AnalysisExportBundle:
    """Build a bundle with canonical ordering.

    Series — a keyed set with unique ``series_id`` — are sorted by id, and
    metadata by key, so equal inputs always produce equal ``spec_sha256``
    and equal bytes in every renderer. Each series' points keep the
    source authority's canonical order: an impulse, trajectory,
    repeated-x or parametric series must never be resampled by an
    exporter's idea of sorting.
    """

    ordered = tuple(
        sorted(series, key=lambda item: item.series_id)
    )
    payload: dict[str, Any] = {
        'export_id': export_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'series': ordered,
        'metadata': tuple(sorted(metadata, key=lambda m: m.key)),
        'generated_at_utc': generated_at_utc,
    }
    provisional = AnalysisExportBundle.model_construct(
        **payload,
        spec_sha256='0' * 64,
        export_sha256='0' * 64,
    )
    return AnalysisExportBundle(
        **payload,
        spec_sha256=_hash(provisional.spec_payload()),
        export_sha256=_hash(provisional.semantic_payload()),
    )


def series_from_measurement_dataset(
    dataset: CadFrequencyResponseDataset,
    measurement: CadMeasurementRecord,
    *,
    label: str | None = None,
    series_id: str | None = None,
    current_scene_revision_id: str | None = None,
) -> AnalysisSeries:
    """Derive a raw-measurement series from a persisted dataset.

    Provenance comes only from the real authorities: the dataset's own
    ``dataset_sha256`` pins identity, and ``historical`` is derived by
    comparing the measurement's pinned SceneRevision to the supplied
    current head — not from a caller flag.
    """

    return AnalysisSeries(
        series_id=series_id or f'dataset:{dataset.dataset_id}',
        label=label or f'測定 {dataset.dataset_id}',
        value_class='raw',
        x_label='Frequency',
        y_label='Level',
        unit='db',
        points=tuple(
            AnalysisSeriesPoint(x=x, y=y)
            for x, y in zip(dataset.frequency_hz, dataset.level_db)
        ),
        source_kind='measurement_dataset',
        source_id=dataset.dataset_id,
        source_sha256=dataset.dataset_sha256,
        historical=(
            current_scene_revision_id is not None
            and measurement.scene_revision_id != current_scene_revision_id
        ),
    )


def series_from_comparison(
    comparison: CadMeasurementComparison,
    *,
    label: str | None = None,
    series_id: str | None = None,
    current_scene_revision_id: str | None = None,
) -> AnalysisSeries:
    """Derive a comparison (difference) series from a persisted A/B record.

    The processing operation identity comes from the comparison record
    itself (``algorithm_version``/``algorithm_sha256``); ``historical`` is
    derived from the revisions the comparison binds — it is historical
    once the declared current head is neither side of the A/B.
    """

    return AnalysisSeries(
        series_id=series_id or f'comparison:{comparison.comparison_id}',
        label=label or f'比較 {comparison.comparison_id}',
        value_class='derived',
        x_label='Frequency',
        y_label='Difference',
        unit='db',
        points=tuple(
            AnalysisSeriesPoint(x=x, y=y)
            for x, y in zip(comparison.grid_hz, comparison.difference_db)
        ),
        source_kind='measurement_comparison',
        source_id=comparison.comparison_id,
        source_sha256=comparison.comparison_sha256,
        operation='measurement_comparison',
        operation_version=comparison.algorithm_version,
        operation_sha256=comparison.algorithm_sha256,
        historical=(
            current_scene_revision_id is not None
            and current_scene_revision_id
            not in (
                comparison.scene_revision_a_id,
                comparison.scene_revision_b_id,
            )
        ),
    )


def _band_text(band: tuple[float, float] | None) -> str | None:
    if band is None:
        return None
    return f'{_format_number(band[0])}-{_format_number(band[1])} Hz'


def comparison_metadata_entries(
    comparison: CadMeasurementComparison,
) -> tuple[AnalysisExportMeta, ...]:
    """Scalar verdict + spec context of a persisted A/B comparison.

    The difference curve travels as a series, but the headline verdict the
    workspace shows (metrics, band spec, side labels, level compatibility)
    is metadata — without it an export carries the trace but not the
    comparison's actual result. Keys are namespaced by comparison id so
    every entry is unique within a bundle.
    """

    prefix = f'comparison.{comparison.comparison_id}'
    entries: list[AnalysisExportMeta] = [
        AnalysisExportMeta(
            key=f'{prefix}.algorithm_version',
            value=comparison.algorithm_version,
        ),
        AnalysisExportMeta(
            key=f'{prefix}.requested_band_hz',
            value=_band_text(comparison.requested_band_hz) or 'unknown',
        ),
        AnalysisExportMeta(
            key=f'{prefix}.actual_band_hz',
            value=_band_text(comparison.actual_band_hz) or 'unknown',
        ),
        AnalysisExportMeta(
            key=f'{prefix}.valid_points', value=str(comparison.valid_points)
        ),
        AnalysisExportMeta(
            key=f'{prefix}.total_grid_points',
            value=str(comparison.total_grid_points),
        ),
    ]
    optional: tuple[tuple[str, Any], ...] = (
        ('label_a', comparison.label_a),
        ('label_b', comparison.label_b),
        ('level_compatibility', comparison.level_compatibility),
        ('reference_band_hz', _band_text(comparison.reference_band_hz)),
        ('excluded_bands_hz', ' / '.join(
            _band_text(band) or '' for band in comparison.excluded_bands
        ) or None),
        ('mean_difference_db', comparison.mean_difference_db),
        ('rms_difference_db', comparison.rms_difference_db),
        ('level_offset_db', comparison.level_offset_db),
        ('shape_rms_db', comparison.shape_rms_db),
    )
    for name, value in optional:
        if value is None:
            continue
        entries.append(
            AnalysisExportMeta(
                key=f'{prefix}.{name}',
                value=(
                    _format_number(value)
                    if isinstance(value, float)
                    else str(value)
                ),
            )
        )
    return tuple(entries)


def series_from_prediction(
    points: tuple[tuple[float, float], ...],
    *,
    label: str,
    prediction_ref: str,
    prediction_sha256: str,
    unit: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    source_scene_revision_id: str | None = None,
    current_scene_revision_id: str | None = None,
    series_id: str | None = None,
) -> AnalysisSeries:
    """Derive a predicted series pinned to an exact prediction authority.

    ``historical`` is derived from authority context: the prediction's
    scene binding (``source_scene_revision_id``) versus the declared
    current head — never a caller-supplied boolean.
    """

    return AnalysisSeries(
        series_id=series_id or f'prediction:{prediction_ref}',
        label=label,
        value_class='predicted',
        x_label=x_label,
        y_label=y_label,
        unit=unit,
        points=tuple(
            AnalysisSeriesPoint(x=x, y=y) for x, y in points
        ),
        source_kind='prediction',
        source_id=prediction_ref,
        source_sha256=prediction_sha256,
        historical=(
            current_scene_revision_id is not None
            and source_scene_revision_id is not None
            and source_scene_revision_id != current_scene_revision_id
        ),
    )


def render_analysis_csv(bundle: AnalysisExportBundle) -> str:
    """Deterministic CSV: one metadata header block, then series rows."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(csv_safe_row(('section', 'key', 'value')))
    writer.writerow(csv_safe_row(('export', 'export_id', bundle.export_id)))
    writer.writerow(csv_safe_row(('export', 'title', bundle.title)))
    writer.writerow(
        csv_safe_row(('export', 'generated_at_utc', bundle.generated_at_utc))
    )
    writer.writerow(
        csv_safe_row(('export', 'spec_sha256', bundle.spec_sha256))
    )
    writer.writerow(
        csv_safe_row(('export', 'export_sha256', bundle.export_sha256))
    )
    for meta in bundle.metadata:
        writer.writerow(csv_safe_row(('metadata', meta.key, meta.value)))
    writer.writerow(
        csv_safe_row(
            (
                'series_id',
                'label',
                'value_class',
                'x',
                'y',
                'unit',
                'source_kind',
                'source_id',
                'source_sha256',
                'operation',
                'operation_version',
                'operation_sha256',
                'historical',
            )
        )
    )
    for series in bundle.series:
        for point in series.points:
            writer.writerow(
                csv_safe_row(
                    (
                        series.series_id,
                        series.label,
                        series.value_class,
                        _format_number(point.x),
                        _format_number(point.y),
                        series.unit,
                        series.source_kind,
                        series.source_id,
                        series.source_sha256,
                        series.operation,
                        series.operation_version,
                        series.operation_sha256,
                        str(series.historical).lower(),
                    )
                )
            )
    return buffer.getvalue()


def render_analysis_json(bundle: AnalysisExportBundle) -> str:
    """Canonical JSON — byte-identical for equal inputs."""

    return _canonical_json(bundle.semantic_payload()) + '\n'


def _plot_svg(
    series: tuple[AnalysisSeries, ...], unit_label: str
) -> str:
    width, height, pad = 720, 360, 48
    xs = [p.x for s in series for p in s.points]
    ys = [p.y for s in series for p in s.points]
    if not xs or not ys:
        return ''
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min:
        x_max = x_min + 1.0
    if y_max == y_min:
        y_max = y_min + 1.0

    def px(x: float) -> float:
        return pad + (x - x_min) / (x_max - x_min) * (width - 2 * pad)

    def py(y: float) -> float:
        return height - pad - (y - y_min) / (y_max - y_min) * (
            height - 2 * pad
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} '
        f'{height}" role="img">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>',
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" '
        f'y2="{height - pad}" stroke="#333"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" '
        f'stroke="#333"/>',
        f'<text x="{width - pad}" y="{pad - 8}" font-size="11" '
        f'text-anchor="end">y unit: {html.escape(unit_label)}</text>',
        f'<text x="{pad}" y="{height - pad + 16}" font-size="11">'
        f'{html.escape(_format_number(x_min))}</text>',
        f'<text x="{width - pad}" y="{height - pad + 16}" font-size="11" '
        f'text-anchor="end">{html.escape(_format_number(x_max))}</text>',
        f'<text x="{pad - 6}" y="{height - pad}" font-size="11" '
        f'text-anchor="end">{html.escape(_format_number(y_min))}</text>',
        f'<text x="{pad - 6}" y="{pad}" font-size="11" text-anchor="end">'
        f'{html.escape(_format_number(y_max))}</text>',
    ]
    for index, item in enumerate(series):
        color = _SERIES_COLORS[index % len(_SERIES_COLORS)]
        points = ' '.join(
            f'{px(p.x):.2f},{py(p.y):.2f}' for p in item.points
        )
        parts.append(
            f'<polyline points="{points}" fill="none" '
            f'stroke="{color}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{pad + 8}" y="{pad + 14 + 14 * index}" '
            f'font-size="12" fill="{color}">'
            f'{html.escape(item.label)} '
            f'({html.escape(item.value_class)})</text>'
        )
    parts.append('</svg>')
    return ''.join(parts)


def render_analysis_html(bundle: AnalysisExportBundle) -> str:
    """Self-contained HTML report with embedded machine-readable payload.

    Series are plotted in separate groups per y-unit — incompatible units
    are never plotted on a shared axis.
    """

    metadata_rows = ''.join(
        '<tr><td>'
        + html.escape(meta.key)
        + '</td><td>'
        + html.escape(meta.value)
        + '</td></tr>'
        for meta in bundle.metadata
    )
    series_rows = ''.join(
        '<tr><td>'
        + html.escape(series.series_id)
        + '</td><td>'
        + html.escape(series.label)
        + '</td><td>'
        + html.escape(series.value_class)
        + '</td><td>'
        + str(len(series.points))
        + '</td><td>'
        + html.escape(series.source_kind or '')
        + '</td><td>'
        + html.escape(series.source_id or '')
        + '</td><td>'
        + html.escape(series.source_sha256 or '')
        + '</td><td>'
        + ('historical' if series.historical else 'current')
        + '</td></tr>'
        for series in bundle.series
    )
    plots: list[str] = []
    unit_groups: dict[str, list[AnalysisSeries]] = {}
    for series in bundle.series:
        unit_groups.setdefault(series.unit or 'unlabeled', []).append(series)
    for unit_label in sorted(unit_groups):
        svg = _plot_svg(tuple(unit_groups[unit_label]), unit_label)
        if svg:
            plots.append(svg)
    payload_json = _embed_json(bundle.semantic_payload())
    return (
        '<!doctype html>\n'
        '<html lang="en"><head><meta charset="utf-8">'
        f'<title>{html.escape(bundle.title)}</title>'
        '<style>'
        'body{font-family:system-ui,sans-serif;margin:2em;color:#222}'
        'table{border-collapse:collapse;margin:1em 0}'
        'td,th{border:1px solid #ccc;padding:4px 10px;font-size:13px}'
        'h1{font-size:20px}h2{font-size:15px;margin-top:1.6em}'
        'script{display:none}'
        '</style></head><body>'
        f'<h1>{html.escape(bundle.title)}</h1>'
        f'<p>Export <code>{html.escape(bundle.export_id)}</code> · '
        f'generated {html.escape(bundle.generated_at_utc)} · '
        f'spec sha256 <code>{html.escape(bundle.spec_sha256)}</code> · '
        f'export sha256 <code>{html.escape(bundle.export_sha256)}</code></p>'
        '<h2>Series</h2>'
        '<table><tr><th>id</th><th>label</th><th>value class</th>'
        '<th>points</th><th>source kind</th><th>source id</th>'
        '<th>source sha256</th><th>currency</th></tr>'
        f'{series_rows}</table>'
        + ''.join(plots)
        + '<h2>Metadata</h2><table>'
        + f'{metadata_rows}</table>'
        + '<h2>Machine-readable payload</h2>'
        + '<script type="application/json" id="analysis-export">'
        + payload_json
        + '</script>'
        + '</body></html>\n'
    )


__all__ = [
    'ANALYSIS_EXPORT_AUTHORITY_VERSION',
    'ANALYSIS_EXPORT_SCHEMA_VERSION',
    'AnalysisExportBundle',
    'AnalysisExportMeta',
    'AnalysisSeries',
    'AnalysisSeriesPoint',
    'AnalysisValueClass',
    'build_analysis_export',
    'render_analysis_csv',
    'render_analysis_html',
    'render_analysis_json',
    'series_from_comparison',
    'series_from_measurement_dataset',
    'series_from_prediction',
]
