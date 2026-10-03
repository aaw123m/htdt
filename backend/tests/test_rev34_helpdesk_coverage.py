"""REV34-HELPDESK: coverage regression for help/glossary/explanation surfaces.

Every code or sentence the backend emits onto a user-facing surface must
resolve to a non-empty Japanese rendering and a real help topic — a new
emitted code that escapes localization fails here instead of leaking raw
English to the operator. The emitted vocabularies are enumerated
mechanically (AST walks over the producer modules + runtime Literal
members), so this file does not drift when emitters add codes.
"""

from __future__ import annotations

import ast
import errno as _errno
import sqlite3
import typing
from pathlib import Path

import pytest

from htdt.availability_reasons import AVAILABILITY_REASONS
from htdt.cad_display_labels import measurement_reason_label
from htdt.cad_measurement_quality import (
    MEASUREMENT_QUALITY_CHECKS,
    QualityDecision,
    RetakeRecommendation,
)
from htdt.help_registry import build_help_registry, default_command_ids
from htdt.localization import (
    HTDT_TERMINOLOGY,
    PresentationLocale,
    TermId,
    term_text,
)
from htdt.measurement_explanations import (
    FIELD_EXPLANATIONS,
    METRIC_EXPLANATIONS,
    STATUS_EXPLANATIONS,
    _STATUS_DOMAINS,
    status_explanation,
)
from htdt.measurement_page_workspace import (
    _check_label,
    _check_status_label,
    _evidence_label,
    _missing_evidence_label,
    _mismatch_label,
    _remeasure_label,
    _retake_recommendation_label,
)
from htdt.user_facing_error import (
    _NAME_PATTERNS,
    _SUFFIX_PATTERNS,
    to_user_facing_error,
)

SRC = Path(__file__).resolve().parents[1] / 'src' / 'htdt'
JA = PresentationLocale.JAPANESE
EN = PresentationLocale.ENGLISH
ERROR_FALLBACK_TOPIC = 'trouble.operation_error'


def _ja(text: str) -> bool:
    """Non-empty and containing at least one Japanese character."""
    return bool(text) and any(ord(c) >= 0x3000 for c in text)


def _tree(module: str) -> ast.Module:
    return ast.parse((SRC / module).read_text(encoding='utf-8'))


def _kwarg_literals(tree: ast.AST, keyword: str) -> list[str]:
    """All string-literal values of ``<keyword>=`` keyword arguments."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == keyword and isinstance(
            node.value, ast.Constant
        ) and isinstance(node.value.value, str):
            found.append(node.value.value)
    return found


def _call_arg_literals(tree: ast.AST, func_name: str, position: int) -> list[ast.expr]:
    """Argument nodes at ``position`` of every ``func_name(...)`` call."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
            and len(node.args) > position
        ):
            found.append(node.args[position])
    return found


def _render_joinedstr(node: ast.JoinedStr, sample: str = '1.000') -> str:
    """Concrete sample of an f-string template (formats are dropped)."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
        elif isinstance(value, ast.FormattedValue):
            parts.append(sample)
    return ''.join(parts)


# ---------------------------------------------------------------------------
# Registry integrity — every link resolves, every topic ships JA + EN content
# ---------------------------------------------------------------------------

def test_builtin_registry_integrity_is_clean() -> None:
    registry = build_help_registry()
    report = registry.validate(
        command_ids=default_command_ids(),
        command_reason_codes=set(AVAILABILITY_REASONS),
    )
    assert report.ok, report.errors


def test_every_topic_and_term_has_nonempty_ja_content() -> None:
    registry = build_help_registry()
    for topic_id in registry.topic_ids():
        topic = registry.require(topic_id)
        content = topic.content[JA]
        assert _ja(content.title), topic_id
        assert _ja(content.summary), topic_id
    for term in registry.glossary():
        # preferred labels may be proper acronyms (REW, RT60, SPL) —
        # non-emptiness is required, Japanese-ness is not.
        assert term.label(JA), term.term_id
        assert _ja(term.meaning[JA]), term.term_id


# ---------------------------------------------------------------------------
# Terminology ↔ glossary ↔ field explanations
# ---------------------------------------------------------------------------

def test_every_terminology_term_is_glossed_in_both_locales() -> None:
    registry = build_help_registry()
    assert set(TermId) == set(HTDT_TERMINOLOGY), (
        'TermId members without a HTDT_TERMINOLOGY entry: '
        f'{set(TermId) - set(HTDT_TERMINOLOGY)}'
    )
    for term_id, entry in HTDT_TERMINOLOGY.items():
        assert entry.preferred[JA], term_id
        assert entry.preferred.get(EN), term_id
        assert _ja(entry.gloss[JA]), term_id
        assert entry.gloss.get(EN), term_id
        glossary_term = registry.glossary_term(term_id)
        assert glossary_term is not None, (
            f'TermId.{term_id.name} has a terminology entry but no glossary '
            'term — the 用語集 dialog cannot show it'
        )
        assert term_text(term_id, JA), term_id


def test_field_explanations_resolve_terms_and_topics() -> None:
    registry = build_help_registry()
    for key, explanation in FIELD_EXPLANATIONS.items():
        assert _ja(explanation.meaning), key
        if explanation.term is not None:
            assert explanation.term in HTDT_TERMINOLOGY, (
                f'{key}: dangling term link {explanation.term}'
            )
        if explanation.topic is not None:
            assert registry.get(explanation.topic) is not None, (
                f'{key}: dangling topic link {explanation.topic!r}'
            )


def test_status_explanations_cover_every_emitted_status_code() -> None:
    # The flat registry: every code resolves to non-empty JA.
    for code, text in STATUS_EXPLANATIONS.items():
        assert _ja(text), code
        assert status_explanation(code) == text
    # Domain vocabularies: the emitted code list of each domain resolves
    # through the same entry point the tables call.
    for domain, vocab in _STATUS_DOMAINS.items():
        assert vocab, domain
        for code, text in vocab.items():
            assert _ja(text), f'{domain}:{code}'
            assert status_explanation(code, domain=domain) == text
    for name, text in METRIC_EXPLANATIONS.items():
        assert _ja(text), name


# ---------------------------------------------------------------------------
# Emitted reason/status codes → help topics and JA labels
# ---------------------------------------------------------------------------

def test_overview_readiness_codes_bind_help_topics() -> None:
    """Every ``code=`` the overview emits must resolve a "Why?" topic."""
    registry = build_help_registry()
    tree = _tree('overview_readiness.py')
    emitted = set(_kwarg_literals(tree, 'code'))
    # also `code = 'literal'` assignments feeding the same payload
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == 'code' for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            emitted.add(node.value.value)
    assert emitted, 'no reason codes found — emitter shape changed?'
    unbound = [c for c in emitted if registry.topic_for_reason(c) is None]
    assert not unbound, (
        'overview reason codes with no help topic (dead-end "Why?" button): '
        f'{unbound}'
    )


def test_availability_catalog_specs_have_ja_and_resolvable_topic() -> None:
    registry = build_help_registry()
    for code, spec in AVAILABILITY_REASONS.items():
        assert _ja(spec.message_ja), code
        assert spec.message_en, code
        assert spec.help_topic_id is not None, code
        assert registry.get(spec.help_topic_id) is not None, (
            f'{code}: bound help topic {spec.help_topic_id!r} does not exist'
        )


def _emitted_error_codes() -> set[str]:
    """Every ``code`` the exception mapper can emit (pattern slots + literal
    returns in ``_map_exception``)."""
    codes = {entry[1] for entry in _NAME_PATTERNS}
    codes.update(entry[1] for entry in _SUFFIX_PATTERNS)
    tree = _tree('user_facing_error.py')
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != '_map_exception':
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(
                    sub.value, (ast.Tuple, ast.Name)
                ):
                    target = (
                        sub.value.elts[0]
                        if isinstance(sub.value, ast.Tuple)
                        else sub.value
                    )
                    if isinstance(target, ast.Constant) and isinstance(
                        target.value, str
                    ):
                        codes.add(target.value)
    return codes


def test_every_emitted_error_code_resolves_a_help_topic() -> None:
    """The error dialog's ヘルプ button must never dead-end: each emitted
    code resolves via its bound topic or the designed operation-error
    fallback — both must exist with JA content."""
    registry = build_help_registry()
    fallback = registry.get(ERROR_FALLBACK_TOPIC)
    assert fallback is not None
    assert _ja(fallback.content[JA].title)
    codes = _emitted_error_codes()
    assert len(codes) >= 30, f'mapper enumeration drifted: {sorted(codes)}'
    for code in codes:
        topic = registry.topic_for_reason(code) or fallback
        assert _ja(topic.content[JA].title), code
        assert _ja(topic.content[JA].summary), code


def test_exception_mapper_emits_ja_messages_for_every_pattern() -> None:
    for type_name, code, _message, _recovery in _NAME_PATTERNS:
        exc = type(type_name, (Exception,), {})()
        error = to_user_facing_error(exc, title='テスト')
        assert error.code == code, (type_name, error.code)
        assert _ja(error.message), code
    for suffix, code, _message, _recovery in _SUFFIX_PATTERNS:
        exc = type(f'Fake{suffix}', (Exception,), {})()
        error = to_user_facing_error(exc, title='テスト')
        assert error.code == code, (suffix, error.code)
        assert _ja(error.message), code
    # Builtin-exception fallbacks (each branch of _map_exception).
    builtins = [
        FileNotFoundError('x'),
        PermissionError('x'),
        OSError(_errno.ENOSPC, 'x'),
        OSError(_errno.EADDRINUSE, 'x'),
        OSError('x'),
        KeyError('x'),
        ValueError('plain english'),
        sqlite3.Error('x'),
        RuntimeError('x'),
    ]
    for exc in builtins:
        error = to_user_facing_error(exc, title='テスト')
        assert error.code in _emitted_error_codes(), error.code
        assert _ja(error.message), (type(exc).__name__, error.code)


# ---------------------------------------------------------------------------
# Emitted measurement vocabularies → JA labels (fail-open passthrough must
# never fire for a code the backend actually produces)
# ---------------------------------------------------------------------------

def test_quality_check_reason_sentences_are_glossed() -> None:
    """Every reason literal a ``_check(...)`` emits has a JA label."""
    tree = _tree('cad_measurement_quality.py')
    raw: list[str] = []
    rendered: list[str] = []
    for arg in _call_arg_literals(tree, '_check', 1):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            raw.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            rendered.append(_render_joinedstr(arg))
    assert len(raw) + len(rendered) >= 20, 'reason enumeration drifted'
    unglossed = [r for r in raw if measurement_reason_label(r) == r]
    unglossed += [r for r in rendered if measurement_reason_label(r) == r]
    assert not unglossed, (
        'emitted quality-check reasons that would render raw English: '
        f'{unglossed}'
    )


def test_retake_guidance_codes_are_glossed() -> None:
    """``missing_evidence``/``remeasure``/recommendation vocab emitted by
    ``measurement_retake_guidance`` all have JA display labels."""
    tree = _tree('cad_measurement_quality.py')
    appended: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'append'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ('missing', 'remeasure')
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            appended.add(node.args[0].value)
    # the `code in missing` tuple literals also emit missing-codes — union
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(c, ast.Constant) and isinstance(c.value, str)
            for c in node.comparators
        ):
            for comp in ast.walk(node):
                if isinstance(comp, ast.Tuple):
                    for elt in comp.elts:
                        if isinstance(elt, ast.Constant) and isinstance(
                            elt.value, str
                        ):
                            appended.add(elt.value)
    missing_codes = {
        c for c in appended
        if c
        in (
            'clipping_metadata',
            'snr_evidence',
            'usable_band_evidence',
            'timing_reference_evidence',
            'polarity_evidence',
            'impulse_response',
            'ir_window_evidence',
            'calibration_provenance',
            'repeat_measurements',
            'acquisition_context',
        )
    }
    remeasure_codes = appended - missing_codes
    assert missing_codes and remeasure_codes, 'code enumeration drifted'
    for code in missing_codes:
        assert _missing_evidence_label(code) != code, code
    for code in remeasure_codes:
        assert _remeasure_label(code) != code, code
    for member in typing.get_args(RetakeRecommendation):
        assert _retake_recommendation_label(member) != member, member
    for member in typing.get_args(QualityDecision):
        assert _check_status_label(member) != member, member
    for check in MEASUREMENT_QUALITY_CHECKS:
        assert _check_label(check) != check, check


def test_comparison_mismatch_codes_are_glossed() -> None:
    tree = _tree('cad_comparison_semantics.py')
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'append'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'mismatches'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            emitted.add(node.args[0].value)
    assert len(emitted) >= 5, f'mismatch enumeration drifted: {emitted}'
    unglossed = [c for c in emitted if _mismatch_label(c) == c]
    assert not unglossed, f'mismatch codes rendering raw: {unglossed}'


def test_evidence_type_labels_cover_stored_vocabulary() -> None:
    """Stored evidence_type tokens shown in batch/comparison surfaces."""
    for token in ('measured', 'derived', 'predicted', 'unknown'):
        label = _evidence_label(token)
        assert _ja(label), token
