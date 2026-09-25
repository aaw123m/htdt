"""Installation output visual QA (#785).

#644 owns the drawing authority — canonical engineering data stays
correct even when the rendered artifact is unusable. This module is the
deterministic QA layer over SVG/PDF layout: label overlap, text clipping,
minimum font size, grayscale-safe encodings, NTS-vs-datum consistency and
page-fit checks over the canonical fixtures (VQ-small / VQ-dense /
VQ-edge). It is acceptance infrastructure — not a second report renderer.

A QA run produces an immutable verdict record pinned to the artifact
hash; failures enumerate findings, never one aggregate score.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


VisualQAFixtureId = Literal['vq_small', 'vq_dense', 'vq_edge']
PageSize = Literal['a4', 'a3']
Orientation = Literal['portrait', 'landscape']
Severity = Literal['error', 'warning']

# Page bounds in mm (print area, margins excluded by convention).
PAGE_BOUNDS_MM: dict[tuple[PageSize, Orientation], tuple[float, float]] = {
    ('a4', 'portrait'): (190.0, 277.0),
    ('a4', 'landscape'): (277.0, 190.0),
    ('a3', 'portrait'): (277.0, 400.0),
    ('a3', 'landscape'): (400.0, 277.0),
}

_MIN_FONT_PT = 4.0
_WARN_FONT_PT = 5.0


class LabelBox(BaseModel):
    """One text label's placed rect on the page (mm from print origin)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    label_id: str = Field(min_length=1)
    text: str
    x_mm: float
    y_mm: float
    w_mm: float = Field(gt=0.0)
    h_mm: float = Field(gt=0.0)
    font_pt: float = Field(gt=0.0)

    def intersects(self, other: 'LabelBox') -> bool:
        return (
            self.x_mm < other.x_mm + other.w_mm
            and other.x_mm < self.x_mm + self.w_mm
            and self.y_mm < other.y_mm + other.h_mm
            and other.y_mm < self.y_mm + self.h_mm
        )


class SemanticEncoding(BaseModel):
    """One visual channel the drawing uses to encode semantics.

    ``grayscale_safe`` false means the meaning disappears when printed or
    viewed without hue — a #579-class defect even when the data is right.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    channel: str = Field(min_length=1)
    grayscale_safe: bool = True


class DrawingLayout(BaseModel):
    """The QA-visible subset of a rendered installation sheet."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    fixture_id: VisualQAFixtureId
    artifact_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    page_size: PageSize
    orientation: Orientation
    fixed_scale: bool = True
    carries_datum_dimensions: bool = False
    labels: tuple[LabelBox, ...] = ()
    encodings: tuple[SemanticEncoding, ...] = ()


class VisualQAFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    check: str = Field(min_length=1)
    severity: Severity
    subject_ref: str | None = None
    detail: str = Field(min_length=1)


def inspect_layout(layout: DrawingLayout) -> tuple[VisualQAFinding, ...]:
    """Run the deterministic check set over one layout."""
    findings: list[VisualQAFinding] = []
    page_w, page_h = PAGE_BOUNDS_MM[
        (layout.page_size, layout.orientation)
    ]

    for i, label in enumerate(layout.labels):
        if label.font_pt < _MIN_FONT_PT:
            findings.append(
                VisualQAFinding(
                    check='min_font_size',
                    severity='error',
                    subject_ref=label.label_id,
                    detail=(
                        f'label "{label.label_id}" renders at '
                        f'{label.font_pt:.1f}pt — below {_MIN_FONT_PT}pt '
                        'floor'
                    ),
                )
            )
        elif label.font_pt < _WARN_FONT_PT:
            findings.append(
                VisualQAFinding(
                    check='min_font_size',
                    severity='warning',
                    subject_ref=label.label_id,
                    detail=(
                        f'label "{label.label_id}" renders at '
                        f'{label.font_pt:.1f}pt — marginal'
                    ),
                )
            )
        if (
            label.x_mm < 0
            or label.y_mm < 0
            or label.x_mm + label.w_mm > page_w
            or label.y_mm + label.h_mm > page_h
        ):
            findings.append(
                VisualQAFinding(
                    check='label_clipping',
                    severity='error',
                    subject_ref=label.label_id,
                    detail=(
                        f'label "{label.label_id}" extends outside the '
                        f'{layout.page_size}/{layout.orientation} print '
                        'area'
                    ),
                )
            )
        for other in layout.labels[i + 1 :]:
            if label.intersects(other):
                findings.append(
                    VisualQAFinding(
                        check='label_overlap',
                        severity='error',
                        subject_ref=f'{label.label_id}+{other.label_id}',
                        detail=(
                            f'labels "{label.label_id}" and '
                            f'"{other.label_id}" overlap'
                        ),
                    )
                )

    for encoding in layout.encodings:
        if not encoding.grayscale_safe:
            findings.append(
                VisualQAFinding(
                    check='grayscale_semantics',
                    severity='error',
                    subject_ref=encoding.channel,
                    detail=(
                        f'semantic channel "{encoding.channel}" is '
                        'hue-only — unreadable in grayscale/print'
                    ),
                )
            )

    if not layout.fixed_scale and layout.carries_datum_dimensions:
        findings.append(
            VisualQAFinding(
                check='nts_with_datum',
                severity='warning',
                detail=(
                    'sheet carries datum dimensions under an NTS '
                    'declaration — reconcile or restate the scale basis'
                ),
            )
        )
    return tuple(findings)


class VisualQAVerdict(BaseModel):
    """Immutable QA verdict over one rendered artifact (#785)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict_id: str = Field(min_length=1)
    fixture_id: VisualQAFixtureId
    artifact_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    passed: bool
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    findings: tuple[VisualQAFinding, ...] = ()
    detail: str = ''


def run_visual_qa(layout: DrawingLayout) -> VisualQAVerdict:
    """Evaluate a layout; a pass requires zero error-severity findings."""
    findings = inspect_layout(layout)
    errors = sum(1 for f in findings if f.severity == 'error')
    warnings = len(findings) - errors
    return VisualQAVerdict(
        verdict_id=f'vqa-{uuid4()}',
        fixture_id=layout.fixture_id,
        artifact_sha256=layout.artifact_sha256,
        passed=errors == 0,
        error_count=errors,
        warning_count=warnings,
        findings=findings,
    )


class VisualQARepository:
    """Append-only QA verdict store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_visual_qa_verdicts (
                    verdict_id TEXT PRIMARY KEY,
                    fixture_id TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def save_verdict(self, verdict: VisualQAVerdict) -> VisualQAVerdict:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_visual_qa_verdicts('
                'verdict_id, fixture_id, passed, error_count, '
                'warning_count, payload_json) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(verdict_id) DO NOTHING',
                (
                    verdict.verdict_id,
                    verdict.fixture_id,
                    1 if verdict.passed else 0,
                    verdict.error_count,
                    verdict.warning_count,
                    verdict.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_visual_qa_verdicts '
                'WHERE verdict_id=?',
                (verdict.verdict_id,),
            ).fetchone()
            if row['payload_json'] != verdict.model_dump_json():
                raise ValueError(
                    f'visual QA verdict {verdict.verdict_id} already '
                    'persisted with different content'
                )
        return verdict

    def list_verdicts_for_fixture(
        self, fixture_id: VisualQAFixtureId
    ) -> tuple[VisualQAVerdict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_visual_qa_verdicts '
                'WHERE fixture_id=? ORDER BY verdict_id ASC',
                (fixture_id,),
            ).fetchall()
        return tuple(
            VisualQAVerdict.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'DrawingLayout',
    'LabelBox',
    'Orientation',
    'PAGE_BOUNDS_MM',
    'PageSize',
    'SemanticEncoding',
    'Severity',
    'VisualQAFinding',
    'VisualQAFixtureId',
    'VisualQARepository',
    'VisualQAVerdict',
    'inspect_layout',
    'run_visual_qa',
]
