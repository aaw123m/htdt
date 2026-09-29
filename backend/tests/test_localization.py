"""#624 localization architecture tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from htdt.localization import (
    CatalogIntegrityError,
    DEFAULT_LOCALE,
    LanguagePolicy,
    LocalizationService,
    MessageCatalog,
    MessageDefinition,
    PresentationLocale,
    TermId,
    build_workflow_catalog,
    detect_system_locale,
    format_bytes,
    format_datetime,
    format_decimal,
    format_duration,
    format_list,
    format_percent,
    resolve_locale,
    term_text,
    terminology_fingerprint,
)


def _msg(key: str, ja: str, en: str) -> MessageDefinition:
    return MessageDefinition(
        key=key,
        templates={
            PresentationLocale.JAPANESE: ja,
            PresentationLocale.ENGLISH: en,
        },
    )


def test_builtin_catalog_validates_clean() -> None:
    catalog = build_workflow_catalog()
    report = catalog.validate()
    assert report.ok, report.errors


def test_tr_renders_requested_locale() -> None:
    catalog = build_workflow_catalog()
    ja = LocalizationService(catalog, policy=LanguagePolicy.JAPANESE)
    en = LocalizationService(catalog, policy=LanguagePolicy.ENGLISH)
    assert ja.tr('workspace.room') == '部屋'
    assert en.tr('workspace.room') == 'Room'


def test_tr_parameterized_and_fallback() -> None:
    catalog = build_workflow_catalog()
    svc = LocalizationService(catalog, policy=LanguagePolicy.ENGLISH)
    text = svc.tr(
        'activity.completed_for_historical', name='Prediction', revision='R10'
    )
    assert 'Prediction' in text and 'R10' in text
    # Unknown keys degrade to the stable key, never crash rendering.
    assert svc.tr('missing.key') == 'missing.key'


def test_duplicate_key_fails_closed() -> None:
    with pytest.raises(CatalogIntegrityError):
        MessageCatalog([_msg('a.b', 'x', 'y'), _msg('a.b', 'p', 'q')])


def test_placeholder_parity_enforced() -> None:
    with pytest.raises(ValueError):
        MessageDefinition(
            key='bad.key',
            templates={
                PresentationLocale.JAPANESE: '{name} の結果',
                PresentationLocale.ENGLISH: 'result',  # missing {name}
            },
        )


def test_missing_production_locale_flagged() -> None:
    catalog = MessageCatalog(
        [
            MessageDefinition(
                key='only.ja',
                templates={PresentationLocale.JAPANESE: '日本語だけ'},
            )
        ]
    )
    report = catalog.validate()
    assert not report.ok
    assert any('only.ja' in e for e in report.errors)


def test_key_must_not_render_itself() -> None:
    catalog = MessageCatalog(
        [
            MessageDefinition(
                key='echo.key',
                templates={
                    PresentationLocale.JAPANESE: 'echo.key',
                    PresentationLocale.ENGLISH: 'echo.key',
                },
            )
        ]
    )
    assert not catalog.validate().ok


def test_language_policy_resolution() -> None:
    assert resolve_locale(LanguagePolicy.JAPANESE) == PresentationLocale.JAPANESE
    assert resolve_locale(LanguagePolicy.ENGLISH) == PresentationLocale.ENGLISH
    assert (
        resolve_locale(
            LanguagePolicy.SYSTEM_DEFAULT,
            system_locale=PresentationLocale.ENGLISH,
        )
        == PresentationLocale.ENGLISH
    )
    assert detect_system_locale({'LANG': 'ja_JP.UTF-8'}) == PresentationLocale.JAPANESE
    assert detect_system_locale({'LANG': 'en_US.UTF-8'}) == PresentationLocale.ENGLISH
    assert detect_system_locale({'LANG': 'de_DE.UTF-8'}) == DEFAULT_LOCALE


def test_locale_formatting() -> None:
    moment = datetime(2026, 9, 23, 22, 17, tzinfo=timezone.utc)
    assert format_datetime(moment, PresentationLocale.JAPANESE) == '2026年9月23日 22:17 UTC'
    assert format_datetime(moment, PresentationLocale.ENGLISH) == '2026-09-23 22:17 UTC'
    assert format_decimal(12345.6, places=1, locale=PresentationLocale.ENGLISH) == '12,345.6'
    assert format_bytes(1536) == '1.5 KB'
    assert format_percent(0.823, places=0) == '82%'
    assert format_duration(4980, PresentationLocale.JAPANESE) == '1時間23分'
    assert format_duration(4980, PresentationLocale.ENGLISH) == '1h 23m'
    assert format_duration(45, PresentationLocale.ENGLISH) == '45s'
    assert format_list(['A', 'B'], PresentationLocale.ENGLISH) == 'A and B'
    assert format_list(['A', 'B'], PresentationLocale.JAPANESE) == 'AとB'


def test_terminology_is_frozen_and_localized() -> None:
    assert term_text(TermId.PROPOSED_SYSTEM, PresentationLocale.JAPANESE) == '提案'
    assert term_text(TermId.PROPOSED_SYSTEM, PresentationLocale.ENGLISH) == 'Proposed'
    assert terminology_fingerprint() == terminology_fingerprint()
    # Acronyms stay recognizable in English presentation.
    assert term_text(TermId.UNKNOWN, PresentationLocale.ENGLISH) == 'UNKNOWN'
