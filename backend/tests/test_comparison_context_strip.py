"""Offscreen Qt coverage for ComparisonContextStrip (#584) — round 2.

The strip is pure presentation: labels must fall back to the em-dash
placeholder and the status value must carry the semantic warning state only
for the two attention states (プレビュー / 選択済み).
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from htdt.comparison_context_strip import ComparisonContextStrip


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_set_context_fills_all_labels() -> None:
    _app()
    strip = ComparisonContextStrip()
    strip.set_context(
        baseline_label='ベースライン v3',
        selected_label='候補 B',
        scene_label='リビジョン 12',
        status_label='比較中',
    )
    assert strip.baseline_value.text() == 'ベースライン v3'
    assert strip.selected_value.text() == '候補 B'
    assert strip.scene_value.text() == 'リビジョン 12'
    assert strip.status_value.text() == '比較中'


def test_set_context_none_falls_back_to_dash() -> None:
    _app()
    strip = ComparisonContextStrip()
    strip.set_context(
        baseline_label='ベース',
        selected_label=None,
        scene_label=None,
        status_label=None,
    )
    assert strip.selected_value.text() == '—'
    assert strip.scene_value.text() == '—'
    assert strip.status_value.text() == '—'


def test_status_warning_state_only_for_attention_labels() -> None:
    _app()
    strip = ComparisonContextStrip()
    for label in ('プレビュー', '選択済み'):
        strip.set_context(
            baseline_label='b',
            selected_label=None,
            scene_label=None,
            status_label=label,
        )
        assert strip.status_value.property('semanticState') == 'warning'
    strip.set_context(
        baseline_label='b',
        selected_label=None,
        scene_label=None,
        status_label='比較中',
    )
    assert strip.status_value.property('semanticState') is None
