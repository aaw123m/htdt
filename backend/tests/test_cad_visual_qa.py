"""#785: installation output visual QA — deterministic layout checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_visual_qa import (
    DrawingLayout,
    LabelBox,
    SemanticEncoding,
    VisualQARepository,
    inspect_layout,
    run_visual_qa,
)


def _layout(labels=(), encodings=(), **kw) -> DrawingLayout:
    return DrawingLayout(
        fixture_id='vq_small',
        page_size='a4',
        orientation='portrait',
        labels=labels,
        encodings=encodings,
        **kw,
    )


def test_clean_layout_passes() -> None:
    layout = _layout(
        labels=(
            LabelBox(
                label_id='l1', text='Subwoofer', x_mm=10, y_mm=10,
                w_mm=30, h_mm=6, font_pt=9.0,
            ),
            LabelBox(
                label_id='l2', text='Mains L/R', x_mm=10, y_mm=30,
                w_mm=30, h_mm=6, font_pt=9.0,
            ),
        )
    )
    verdict = run_visual_qa(layout)
    assert verdict.passed
    assert verdict.error_count == 0


def test_label_overlap_is_error() -> None:
    layout = _layout(
        labels=(
            LabelBox(
                label_id='l1', text='A', x_mm=10, y_mm=10,
                w_mm=30, h_mm=8, font_pt=9.0,
            ),
            LabelBox(
                label_id='l2', text='B', x_mm=20, y_mm=12,
                w_mm=30, h_mm=8, font_pt=9.0,
            ),
        )
    )
    findings = inspect_layout(layout)
    assert any(
        f.check == 'label_overlap' and f.severity == 'error'
        for f in findings
    )
    verdict = run_visual_qa(layout)
    assert not verdict.passed


def test_label_clipping_and_font_floor() -> None:
    layout = _layout(
        labels=(
            LabelBox(
                label_id='clipped', text='X', x_mm=180, y_mm=270,
                w_mm=30, h_mm=10, font_pt=9.0,
            ),
            LabelBox(
                label_id='tiny', text='Y', x_mm=10, y_mm=10,
                w_mm=20, h_mm=4, font_pt=3.0,
            ),
            LabelBox(
                label_id='marginal', text='Z', x_mm=10, y_mm=20,
                w_mm=20, h_mm=4, font_pt=4.5,
            ),
        )
    )
    findings = inspect_layout(layout)
    checks = {(f.subject_ref, f.check, f.severity) for f in findings}
    assert ('clipped', 'label_clipping', 'error') in checks
    assert ('tiny', 'min_font_size', 'error') in checks
    assert ('marginal', 'min_font_size', 'warning') in checks


def test_hue_only_encoding_is_error() -> None:
    layout = _layout(
        encodings=(
            SemanticEncoding(channel='speaker_zone', grayscale_safe=False),
            SemanticEncoding(channel='datum_axis', grayscale_safe=True),
        )
    )
    findings = inspect_layout(layout)
    assert any(
        f.check == 'grayscale_semantics' and f.subject_ref == 'speaker_zone'
        for f in findings
    )


def test_nts_with_datum_warns() -> None:
    layout = _layout(fixed_scale=False, carries_datum_dimensions=True)
    findings = inspect_layout(layout)
    assert any(f.check == 'nts_with_datum' for f in findings)


def test_verdict_repository(tmp_path: Path) -> None:
    repo = VisualQARepository(tmp_path / 'cad.sqlite3')
    verdict = run_visual_qa(_layout())
    repo.save_verdict(verdict)
    repo.save_verdict(verdict)
    loaded = repo.list_verdicts_for_fixture('vq_small')
    assert [v.verdict_id for v in loaded] == [verdict.verdict_id]
