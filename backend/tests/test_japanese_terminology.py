"""Canonical Japanese UI terminology: forbidden legacy spellings.

The REV25 terminology pass normalized user-facing Japanese onto one
spelling per concept (evidence → 証拠, calibration → 校正,
recommendation → 推奨, load → 読み込み, import → 取り込み, and so on).
A legacy form slipping back into a string literal surfaces as
inconsistent UI copy, so this check scans every string constant under
``backend/src/htdt`` for the retired spellings. Docstrings are skipped —
they are developer documentation, not UI text.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Each entry: retired spelling -> canonical spelling (for the failure
# message). Only spellings that must never appear in any user-facing
# string belong here; domain enums and stored values are not scanned.
_FORBIDDEN = {
    '証跡': '証拠',
    'エビデンス': '証拠',
    'キャリブレーション': '校正',
    '推薦': '推奨',
    '読込': '読み込み',
    '取込': '取り込み',
    '着座席': '座席',
    '実部屋': '実室',
    '受信箱': '受信ボックス',
    'プロベナンス': '出典',
    '編集履歴': 'アンドゥ履歴',
    '方向照準': '指向性照準',
    '音響の向き': '音響照準',
    '筐体の向き': '筐体ヨー',
    '治療': 'トリートメント',
}


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if (
            not isinstance(body, list)
            or not body
            or not isinstance(body[0], ast.Expr)
        ):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            ids.add(id(value))
    return ids


def _string_constants(tree: ast.AST) -> list[str]:
    docstring_ids = _docstring_constant_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]


def test_japanese_terminology_uses_canonical_spellings() -> None:
    src_root = Path(__file__).resolve().parents[1] / 'src' / 'htdt'
    offenders: list[str] = []
    for path in sorted(src_root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for text in _string_constants(tree):
            for retired, canonical in _FORBIDDEN.items():
                if retired in text:
                    offenders.append(
                        f'{path.name}: {retired!r} (use {canonical!r}) '
                        f'in {text[:60]!r}'
                    )
    assert not offenders, 'retired terminology spellings found:\n' + '\n'.join(
        offenders
    )
