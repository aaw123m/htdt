"""Spreadsheet-safe CSV cell encoding for human-facing exports.

Python's :mod:`csv` quoting protects CSV *structure* only. Spreadsheet
applications additionally interpret a cell whose first character is a formula
prefix as a live formula, so a caller/imported value such as
``=HYPERLINK("https://example.invalid","speaker")`` stays executable even when
the cell is perfectly valid RFC 4180 CSV.

Every textual cell written to a human-facing CSV export is routed through
:func:`csv_safe_cell` (uniformly, via :func:`csv_safe_row`) so fields added
later are covered automatically. Rendering safety lives outside semantic
identity: neutralization happens at render time and never alters the exported
model or its semantic hash.

Escaping convention: a textual cell whose first non-whitespace character is
``=``, ``+``, ``-`` or ``@`` — or that begins with a tab or carriage return,
which some importers strip before evaluating the remainder — is prefixed with
a single quote ``'``. Spreadsheet software renders such cells as inert literal
text. Finite numeric literals such as ``'-4.0'`` are already inert and pass
through unchanged so numeric columns remain machine-readable. Machine-facing
exact payloads remain available through the semantic JSON outputs.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


_FORMULA_LEADING_CHARACTERS = frozenset('=+-@')
_FORMULA_BYPASS_CHARACTERS = frozenset('\t\r')


def _is_finite_numeric_literal(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def csv_safe_cell(value: Any) -> Any:
    """Return ``value`` neutralized against spreadsheet formula interpretation.

    Non-string cells pass through unchanged. Strings whose first
    non-whitespace character is ``=``, ``+``, ``-`` or ``@`` — or that begin
    with a tab or carriage return — are prefixed with a single quote so the
    cell is rendered as literal text. Finite numeric literals are already
    inert and are returned unchanged.
    """
    if not isinstance(value, str):
        return value
    candidate = value.lstrip()
    if (
        candidate
        and candidate[0] in _FORMULA_LEADING_CHARACTERS
        and not _is_finite_numeric_literal(candidate)
    ):
        return "'" + value
    if value and value[0] in _FORMULA_BYPASS_CHARACTERS:
        return "'" + value
    return value


def csv_safe_row(row: Iterable[Any]) -> tuple[Any, ...]:
    """Apply :func:`csv_safe_cell` to every cell of one CSV row."""
    return tuple(csv_safe_cell(cell) for cell in row)
