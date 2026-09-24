"""Analysis export (#512).

Native workflows produce measurement, prediction and comparison data that
must be exportable *reproducibly*: the same authorities must always produce
byte-identical files, every value must carry its class (raw measurement,
derived, predicted, display-transformed), and exports must state when the
underlying evidence is historical rather than current.

This module renders deterministic CSV, JSON, and a self-contained HTML
report (inline SVG plot + embedded machine-readable JSON payload). It never
recomputes source numerics — it packages already-persisted analysis series
with their authority pins.
"""

from __future__ import annotations

import csv
from hashlib import sha256
import html
import io
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .csv_export import csv_safe_row


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


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _format_number(value: float) -> str:
    return format(value, '.12g')


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
        default=None, min_length=8, max_length=64
    )
    historical: bool = False


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
    export_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_bundle(self) -> 'AnalysisExportBundle':
        ids = [series.series_id for series in self.series]
        if len(ids) != len(set(ids)):
            raise ValueError('export series ids must be unique')
        keys = [meta.key for meta in self.metadata]
        if len(keys) != len(set(keys)):
            raise ValueError('export metadata keys must be unique')
        if self.export_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AnalysisExportBundle hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'export_id': self.export_id,
            'document_id': self.document_id,
            'title': self.title,
            'series': [s.model_dump(mode='json') for s in self.series],
            'metadata': [m.model_dump(mode='json') for m in self.metadata],
            'generated_at_utc': self.generated_at_utc,
        }


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

    Series are sorted by ``series_id`` and each series' points by ``(x, y)``
    so equal inputs always produce equal bytes in every renderer.
    """

    ordered = tuple(
        sorted(
            (
                item.model_copy(
                    update={
                        'points': tuple(
                            sorted(item.points, key=lambda p: (p.x, p.y))
                        )
                    }
                )
                for item in series
            ),
            key=lambda item: item.series_id,
        )
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
        **payload, export_sha256='0' * 64
    )
    return AnalysisExportBundle(
        **payload,
        export_sha256=_hash(provisional.semantic_payload()),
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
                        str(series.historical).lower(),
                    )
                )
            )
    return buffer.getvalue()


def render_analysis_json(bundle: AnalysisExportBundle) -> str:
    """Canonical JSON — byte-identical for equal inputs."""

    return _canonical_json(bundle.semantic_payload()) + '\n'


def _plot_svg(bundle: AnalysisExportBundle) -> str:
    width, height, pad = 720, 360, 48
    xs = [p.x for s in bundle.series for p in s.points]
    ys = [p.y for s in bundle.series for p in s.points]
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
        f'<text x="{pad}" y="{height - pad + 16}" font-size="11">'
        f'{html.escape(_format_number(x_min))}</text>',
        f'<text x="{width - pad}" y="{height - pad + 16}" font-size="11" '
        f'text-anchor="end">{html.escape(_format_number(x_max))}</text>',
        f'<text x="{pad - 6}" y="{height - pad}" font-size="11" '
        f'text-anchor="end">{html.escape(_format_number(y_min))}</text>',
        f'<text x="{pad - 6}" y="{pad}" font-size="11" text-anchor="end">'
        f'{html.escape(_format_number(y_max))}</text>',
    ]
    for index, series in enumerate(bundle.series):
        color = _SERIES_COLORS[index % len(_SERIES_COLORS)]
        points = ' '.join(
            f'{px(p.x):.2f},{py(p.y):.2f}' for p in series.points
        )
        parts.append(
            f'<polyline points="{points}" fill="none" '
            f'stroke="{color}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{pad + 8}" y="{pad + 14 + 14 * index}" '
            f'font-size="12" fill="{color}">'
            f'{html.escape(series.label)} '
            f'({html.escape(series.value_class)})</text>'
        )
    parts.append('</svg>')
    return ''.join(parts)


def render_analysis_html(bundle: AnalysisExportBundle) -> str:
    """Self-contained HTML report with embedded machine-readable payload."""

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
    payload_json = html.escape(
        _canonical_json(bundle.semantic_payload()), quote=False
    )
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
        f'sha256 <code>{html.escape(bundle.export_sha256)}</code></p>'
        '<h2>Series</h2>'
        '<table><tr><th>id</th><th>label</th><th>value class</th>'
        '<th>points</th><th>source kind</th><th>source id</th>'
        '<th>source sha256</th><th>currency</th></tr>'
        f'{series_rows}</table>'
        + (_plot_svg(bundle))
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
]
