"""Desktop localization architecture (#624).

The workflow-first native UI historically rendered hard-coded Japanese
literals with no shared presentation layer. This module introduces the
canonical HTDT localization service:

* :class:`MessageCatalog` — one version-controlled source of stable
  presentation keys with Japanese + English templates;
* :class:`LocalizationService` — the single ``tr`` entry point plus the
  language-preference policy (日本語 / English / system default);
* locale formatting helpers for dates, numbers, byte sizes, percentages,
  durations and list conjunctions — engineering unit display stays governed
  by the separate units preference (#496), and locale formatting never
  changes canonical SI values or semantic hashes;
* catalog integrity checks for CI (missing locale entries, placeholder
  mismatches, keys accidentally equal to rendered text).

Domain enums, command IDs, workspace IDs, reason codes, authority values and
report semantic keys are *never* localized in persistence: this layer only
produces rendered presentation text at the UI boundary.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


LOCALIZATION_SCHEMA_VERSION = 1
LOCALIZATION_AUTHORITY_VERSION = 'htdt-localization-1'


class PresentationLocale(StrEnum):
    """Supported product presentation locales (v1: Japanese + English)."""

    JAPANESE = 'ja'
    ENGLISH = 'en'


class LanguagePolicy(StrEnum):
    """User-facing language preference stored in application preferences."""

    SYSTEM_DEFAULT = 'system_default'
    JAPANESE = 'ja'
    ENGLISH = 'en'


#: Japanese remains the primary/default product locale; English is supported.
DEFAULT_LOCALE = PresentationLocale.JAPANESE
SUPPORTED_LOCALES: tuple[PresentationLocale, ...] = (
    PresentationLocale.JAPANESE,
    PresentationLocale.ENGLISH,
)


def detect_system_locale(env: Mapping[str, str] | None = None) -> PresentationLocale:
    """Best-effort OS-locale detection.

    Injectable for tests; the real UI passes the process environment. Any
    ``ja*`` locale selects Japanese; an explicitly English locale selects
    English; anything else falls back to the product default rather than
    presenting an untranslated mix.
    """

    env = os.environ if env is None else env
    raw = (
        env.get('HTDT_LANGUAGE')
        or env.get('LC_ALL')
        or env.get('LC_MESSAGES')
        or env.get('LANG')
        or ''
    ).split('.')[0].split('-')[0].split('_')[0].lower()
    if raw in {'ja', 'jp', 'jpn'}:
        return PresentationLocale.JAPANESE
    if raw in {'en', 'eng'}:
        return PresentationLocale.ENGLISH
    return DEFAULT_LOCALE


def resolve_locale(
    policy: LanguagePolicy,
    *,
    system_locale: PresentationLocale | None = None,
) -> PresentationLocale:
    """Resolve the effective presentation locale for a language policy."""

    if policy == LanguagePolicy.SYSTEM_DEFAULT:
        return system_locale or detect_system_locale()
    return PresentationLocale(policy.value)


_PLACEHOLDER_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


def template_placeholders(template: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER_RE.findall(template))


@dataclass(frozen=True, slots=True)
class MessageDefinition:
    """One stable presentation key with per-locale templates.

    ``context`` carries translator-facing disambiguation notes; it never
    appears in rendered output. ``production`` keys are user-facing in the
    supported product and require every supported locale; development-only
    keys may keep a reduced locale set while stating so explicitly.
    """

    key: str
    templates: Mapping[PresentationLocale, str]
    context: str = ''
    production: bool = True

    def __post_init__(self) -> None:
        if not self.key or self.key.strip() != self.key:
            raise ValueError('message key must be non-empty and trimmed')
        if not self.templates:
            raise ValueError(f'message {self.key!r} has no templates')
        for locale in self.templates:
            if not isinstance(locale, PresentationLocale):
                raise ValueError(
                    f'message {self.key!r} has unsupported locale {locale!r}'
                )
        normalized = {PresentationLocale(k): v for k, v in self.templates.items()}
        object.__setattr__(self, 'templates', normalized)
        reference: frozenset[str] | None = None
        for template in normalized.values():
            placeholders = template_placeholders(template)
            if reference is None:
                reference = placeholders
            elif placeholders != reference:
                raise ValueError(
                    f'message {self.key!r} has mismatched placeholders across '
                    f'locales: {sorted(reference ^ placeholders)}'
                )


class CatalogIntegrityError(ValueError):
    """Raised when a message catalog fails structural integrity checks."""


@dataclass(frozen=True, slots=True)
class CatalogReport:
    """Result of catalog integrity checks; ``errors`` must be empty to ship."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class MessageCatalog:
    """Canonical registry of localized message definitions.

    Constructing the catalog fails closed on duplicate keys — a duplicate
    presentation key is a source bug, not something to merge silently.
    """

    def __init__(self, messages: Iterable[MessageDefinition]) -> None:
        by_key: dict[str, MessageDefinition] = {}
        for message in messages:
            if message.key in by_key:
                raise CatalogIntegrityError(f'duplicate message key: {message.key}')
            by_key[message.key] = message
        self._messages = by_key

    def __contains__(self, key: object) -> bool:
        return key in self._messages

    def __len__(self) -> int:
        return len(self._messages)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._messages)

    def get(self, key: str) -> MessageDefinition | None:
        return self._messages.get(key)

    def all(self) -> tuple[MessageDefinition, ...]:
        return tuple(self._messages.values())

    def validate(
        self,
        *,
        required_locales: Iterable[PresentationLocale] = SUPPORTED_LOCALES,
    ) -> CatalogReport:
        """CI-oriented integrity checks over the whole catalog.

        * every ``production`` key has templates for all required locales;
        * placeholder names already match by construction, re-verified here;
        * a rendered template must never equal its own key (a tell-tale sign
          that an untranslated stable code leaked into the catalog).
        """

        errors: list[str] = []
        warnings: list[str] = []
        required = tuple(required_locales)
        for message in self._messages.values():
            missing = [l.value for l in required if l not in message.templates]
            if message.production and missing:
                errors.append(
                    f'production key {message.key!r} missing locales: {missing}'
                )
            if not message.production and missing:
                warnings.append(
                    f'development key {message.key!r} missing locales: {missing}'
                )
            for locale, template in message.templates.items():
                if template.strip() == message.key:
                    errors.append(
                        f'key {message.key!r} renders itself for locale {locale.value}'
                    )
                reference = template_placeholders(template)
                for other in message.templates.values():
                    if template_placeholders(other) != reference:
                        errors.append(
                            f'key {message.key!r} placeholder mismatch across locales'
                        )
                        break
        return CatalogReport(tuple(errors), tuple(warnings))


class _SafeFormat(dict):
    """Missing parameters stay visible as ``{name}`` instead of crashing UI."""

    def __missing__(self, key: str) -> str:
        return '{' + key + '}'


class LocalizationService:
    """Single entry point that converts stable keys into rendered text.

    The service owns language resolution (explicit user override beats the
    system default) and the fallback chain: requested locale → product
    default locale → the raw key. Falling back to the key keeps development
    builds usable while CI catalog checks still gate production coverage.
    """

    def __init__(
        self,
        catalog: MessageCatalog,
        *,
        policy: LanguagePolicy = LanguagePolicy.SYSTEM_DEFAULT,
        system_locale: PresentationLocale | None = None,
    ) -> None:
        self.catalog = catalog
        self.policy = policy
        self.locale = resolve_locale(policy, system_locale=system_locale)

    def tr(self, key: str, /, **params: object) -> str:
        """Render ``key`` in the active locale with ``{name}`` substitution."""

        message = self.catalog.get(key)
        if message is None:
            return key
        template = message.templates.get(self.locale) or message.templates.get(
            DEFAULT_LOCALE
        )
        if template is None:
            template = next(iter(message.templates.values()))
        return template.format_map(_SafeFormat(params))

    def available(self, key: str) -> bool:
        return key in self.catalog


# ---------------------------------------------------------------------------
# Locale-sensitive presentation formatting.
#
# These helpers centralize the handful of locale differences the desktop UI
# needs (grouping/decimal separators, date ordering, unit words, list
# conjunctions). Engineering display units remain governed by the separate
# units preference (#496); formatting never alters canonical SI values.


@dataclass(frozen=True, slots=True)
class LocaleFormatRules:
    decimal_separator: str
    thousands_separator: str
    date_order: Literal['ymd', 'mdy', 'dmy']
    year_suffix: str
    month_suffix: str
    day_suffix: str
    hour_suffix: str
    minute_suffix: str
    second_suffix: str
    list_conjunction: str
    list_separator: str


LOCALE_FORMAT_RULES: dict[PresentationLocale, LocaleFormatRules] = {
    PresentationLocale.JAPANESE: LocaleFormatRules(
        decimal_separator='.',
        thousands_separator=',',
        date_order='ymd',
        year_suffix='年',
        month_suffix='月',
        day_suffix='日',
        hour_suffix='時間',
        minute_suffix='分',
        second_suffix='秒',
        list_conjunction='・',
        list_separator='、',
    ),
    PresentationLocale.ENGLISH: LocaleFormatRules(
        decimal_separator='.',
        thousands_separator=',',
        date_order='ymd',
        year_suffix='-',
        month_suffix='-',
        day_suffix='',
        hour_suffix='h',
        minute_suffix='m',
        second_suffix='s',
        list_conjunction='and',
        list_separator=', ',
    ),
}


def _rules(locale: PresentationLocale) -> LocaleFormatRules:
    return LOCALE_FORMAT_RULES.get(locale, LOCALE_FORMAT_RULES[DEFAULT_LOCALE])


def format_datetime(
    value: datetime,
    locale: PresentationLocale = DEFAULT_LOCALE,
) -> str:
    """Locale-aware timestamp (``2026-09-23 22:17`` vs ``2026年9月23日 22:17``)."""

    local = value.astimezone(timezone.utc) if value.tzinfo else value
    rules = _rules(locale)
    if locale == PresentationLocale.JAPANESE:
        return (
            f'{local.year}{rules.year_suffix}{local.month}{rules.month_suffix}'
            f'{local.day}{rules.day_suffix} {local.hour:02d}:{local.minute:02d}'
        )
    return f'{local.year:04d}-{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}'


def format_date(value: date, locale: PresentationLocale = DEFAULT_LOCALE) -> str:
    rules = _rules(locale)
    if locale == PresentationLocale.JAPANESE:
        return (
            f'{value.year}{rules.year_suffix}{value.month}{rules.month_suffix}'
            f'{value.day}{rules.day_suffix}'
        )
    return f'{value.year:04d}-{value.month:02d}-{value.day:02d}'


def format_decimal(
    value: float,
    *,
    places: int = 1,
    locale: PresentationLocale = DEFAULT_LOCALE,
    group: bool = True,
) -> str:
    rules = _rules(locale)
    text = f'{value:,.{places}f}' if group else f'{value:.{places}f}'
    if rules.thousands_separator != ',':
        text = text.replace(',', '\u0000')
    if rules.decimal_separator != '.':
        text = text.replace('.', rules.decimal_separator)
    return text.replace('\u0000', rules.thousands_separator)


def format_bytes(
    size: int,
    locale: PresentationLocale = DEFAULT_LOCALE,
) -> str:
    """Byte sizes use SI/IEC unit words that stay recognizable in both locales."""

    units = ('B', 'KB', 'MB', 'GB', 'TB')
    amount = float(size)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == 'B':
        return f'{int(amount)} {unit}'
    return f'{format_decimal(amount, places=1, locale=locale, group=False)} {unit}'


def format_percent(
    fraction: float,
    *,
    places: int = 0,
    locale: PresentationLocale = DEFAULT_LOCALE,
) -> str:
    return f'{format_decimal(fraction * 100.0, places=places, locale=locale, group=False)}%'


def format_duration(
    seconds: float,
    locale: PresentationLocale = DEFAULT_LOCALE,
) -> str:
    """Human duration: ``1時間23分`` vs ``1h 23m``."""

    rules = _rules(locale)
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}{rules.hour_suffix}' if locale == PresentationLocale.JAPANESE else f'{hours}h')
    if minutes:
        parts.append(f'{minutes}{rules.minute_suffix}' if locale == PresentationLocale.JAPANESE else f'{minutes}m')
    if not hours and not minutes:
        parts.append(f'{secs}{rules.second_suffix}' if locale == PresentationLocale.JAPANESE else f'{secs}s')
    joiner = '' if locale == PresentationLocale.JAPANESE else ' '
    return joiner.join(parts)


def format_list(
    items: Iterable[str],
    locale: PresentationLocale = DEFAULT_LOCALE,
    *,
    conjunction: bool = True,
) -> str:
    """Locale list rendering: ``A、BとC``-style conjunction vs ``A, B, and C``."""

    rules = _rules(locale)
    values = list(items)
    if len(values) < 2 or not conjunction:
        return rules.list_separator.join(values)
    if locale == PresentationLocale.JAPANESE:
        return f'{rules.list_separator.join(values[:-1])}と{values[-1]}'
    if len(values) == 2:
        return f'{values[0]} {rules.list_conjunction} {values[1]}'
    return f'{rules.list_separator.join(values[:-1])}{rules.list_separator.rstrip()} {rules.list_conjunction} {values[-1]}'


# ---------------------------------------------------------------------------
# Canonical HTDT terminology (#624 §3 / #623 glossary).
#
# Preferred translations for intentionally-precise domain terms are frozen
# here so every widget and the embedded glossary share one wording. Technical
# acronyms (REW, SPL, IR, MLP, FR) stay recognizable and untranslated.


class TermId(StrEnum):
    CURRENT_SYSTEM = 'current_system'
    PROPOSED_SYSTEM = 'proposed_system'
    AS_BUILT = 'as_built'
    MEASURED = 'measured'
    SCENE_REVISION = 'scene_revision'
    CAPTURE_STAGED = 'capture_staged'
    CAPTURE_PROMOTED = 'capture_promoted'
    UNKNOWN = 'unknown'
    ASSUMED = 'assumed'
    INFERRED = 'inferred'
    UNVERIFIED = 'unverified'
    STALE = 'stale'
    HISTORICAL = 'historical'
    SYSTEM_VARIANT = 'system_variant'
    ACQUISITION_CONTEXT = 'acquisition_context'


class TerminologyEntry(BaseModel):
    """One frozen domain term with its preferred presentation per locale."""

    model_config = ConfigDict(frozen=True)

    term_id: TermId
    preferred: dict[PresentationLocale, str] = Field(min_length=1)
    # Plain-language gloss used by the embedded help glossary (#623).
    gloss: dict[PresentationLocale, str] = Field(default_factory=dict)


#: Preferred translations shared by UI labels and the glossary. The Japanese
#: wording matches the existing lifecycle badge (現在 / 提案 / 設置済み / 実測済み)
#: so retrofit does not change rendered product text.
HTDT_TERMINOLOGY: dict[TermId, TerminologyEntry] = {
    TermId.CURRENT_SYSTEM: TerminologyEntry(
        term_id=TermId.CURRENT_SYSTEM,
        preferred={
            PresentationLocale.JAPANESE: '現在',
            PresentationLocale.ENGLISH: 'Current',
        },
        gloss={
            PresentationLocale.JAPANESE: '設計の現行状態。提案でも実測でもない。',
            PresentationLocale.ENGLISH: 'The present design state; neither proposed nor measured.',
        },
    ),
    TermId.PROPOSED_SYSTEM: TerminologyEntry(
        term_id=TermId.PROPOSED_SYSTEM,
        preferred={
            PresentationLocale.JAPANESE: '提案',
            PresentationLocale.ENGLISH: 'Proposed',
        },
        gloss={
            PresentationLocale.JAPANESE: '代替案。設置済みでも実測済みでもない。',
            PresentationLocale.ENGLISH: 'A design alternative; not installed, measured or current.',
        },
    ),
    TermId.AS_BUILT: TerminologyEntry(
        term_id=TermId.AS_BUILT,
        preferred={
            PresentationLocale.JAPANESE: '設置済み',
            PresentationLocale.ENGLISH: 'As-built',
        },
        gloss={
            PresentationLocale.JAPANESE: '実際に設置された構成。',
            PresentationLocale.ENGLISH: 'The physically installed configuration.',
        },
    ),
    TermId.MEASURED: TerminologyEntry(
        term_id=TermId.MEASURED,
        preferred={
            PresentationLocale.JAPANESE: '実測済み',
            PresentationLocale.ENGLISH: 'Measured',
        },
        gloss={
            PresentationLocale.JAPANESE: '計測エビデンスが存在する。検証済みとは限らない。',
            PresentationLocale.ENGLISH: 'Backed by measurement evidence; not necessarily validated.',
        },
    ),
    TermId.SCENE_REVISION: TerminologyEntry(
        term_id=TermId.SCENE_REVISION,
        preferred={
            PresentationLocale.JAPANESE: 'シーンリビジョン',
            PresentationLocale.ENGLISH: 'SceneRevision',
        },
        gloss={
            PresentationLocale.JAPANESE: '部屋と配置の不変スナップショット。',
            PresentationLocale.ENGLISH: 'An immutable snapshot of room geometry and placement.',
        },
    ),
    TermId.CAPTURE_STAGED: TerminologyEntry(
        term_id=TermId.CAPTURE_STAGED,
        preferred={
            PresentationLocale.JAPANESE: 'ステージ済み',
            PresentationLocale.ENGLISH: 'Staged',
        },
        gloss={
            PresentationLocale.JAPANESE: '取り込み済みだが未昇格のCaptureデータ。',
            PresentationLocale.ENGLISH: 'Imported capture data not yet promoted.',
        },
    ),
    TermId.CAPTURE_PROMOTED: TerminologyEntry(
        term_id=TermId.CAPTURE_PROMOTED,
        preferred={
            PresentationLocale.JAPANESE: '昇格済み',
            PresentationLocale.ENGLISH: 'Promoted',
        },
        gloss={
            PresentationLocale.JAPANESE: 'プロジェクトauthorityへ昇格したCaptureデータ。',
            PresentationLocale.ENGLISH: 'Capture data promoted into project authority.',
        },
    ),
    TermId.UNKNOWN: TerminologyEntry(
        term_id=TermId.UNKNOWN,
        preferred={
            PresentationLocale.JAPANESE: '不明',
            PresentationLocale.ENGLISH: 'UNKNOWN',
        },
        gloss={
            PresentationLocale.JAPANESE: '値が存在しない。0や推定値で補完しない。',
            PresentationLocale.ENGLISH: 'The value is absent; never filled with 0 or a guess.',
        },
    ),
    TermId.ASSUMED: TerminologyEntry(
        term_id=TermId.ASSUMED,
        preferred={
            PresentationLocale.JAPANESE: '仮定',
            PresentationLocale.ENGLISH: 'Assumed',
        },
        gloss={
            PresentationLocale.JAPANESE: '明示的に仮定された値。証拠ではない。',
            PresentationLocale.ENGLISH: 'An explicitly assumed value; not evidence.',
        },
    ),
    TermId.INFERRED: TerminologyEntry(
        term_id=TermId.INFERRED,
        preferred={
            PresentationLocale.JAPANESE: '推定',
            PresentationLocale.ENGLISH: 'Inferred',
        },
        gloss={
            PresentationLocale.JAPANESE: '他のauthorityから推定された値。',
            PresentationLocale.ENGLISH: 'A value inferred from other authorities.',
        },
    ),
    TermId.UNVERIFIED: TerminologyEntry(
        term_id=TermId.UNVERIFIED,
        preferred={
            PresentationLocale.JAPANESE: '未検証',
            PresentationLocale.ENGLISH: 'Unverified',
        },
        gloss={
            PresentationLocale.JAPANESE: '検証エビデンスがまだ無い状態。',
            PresentationLocale.ENGLISH: 'No validation evidence exists yet.',
        },
    ),
    TermId.STALE: TerminologyEntry(
        term_id=TermId.STALE,
        preferred={
            PresentationLocale.JAPANESE: '古い',
            PresentationLocale.ENGLISH: 'Stale',
        },
        gloss={
            PresentationLocale.JAPANESE: '依存authorityが変わり現行ではない。無効とは限らない。',
            PresentationLocale.ENGLISH: 'Not current because a dependency changed; not necessarily invalid.',
        },
    ),
    TermId.HISTORICAL: TerminologyEntry(
        term_id=TermId.HISTORICAL,
        preferred={
            PresentationLocale.JAPANESE: '履歴',
            PresentationLocale.ENGLISH: 'Historical',
        },
        gloss={
            PresentationLocale.JAPANESE: '過去のrevisionに対する有効な記録。',
            PresentationLocale.ENGLISH: 'A record still valid for a past revision.',
        },
    ),
    TermId.SYSTEM_VARIANT: TerminologyEntry(
        term_id=TermId.SYSTEM_VARIANT,
        preferred={
            PresentationLocale.JAPANESE: 'システムバリアント',
            PresentationLocale.ENGLISH: 'SystemVariant',
        },
        gloss={
            PresentationLocale.JAPANESE: '同一Digital Twin上の代替トポロジー提案。',
            PresentationLocale.ENGLISH: 'An alternative topology proposal on the same digital twin.',
        },
    ),
    TermId.ACQUISITION_CONTEXT: TerminologyEntry(
        term_id=TermId.ACQUISITION_CONTEXT,
        preferred={
            PresentationLocale.JAPANESE: '取得コンテキスト',
            PresentationLocale.ENGLISH: 'AcquisitionContext',
        },
        gloss={
            PresentationLocale.JAPANESE: '測定の機器・設定・環境条件。',
            PresentationLocale.ENGLISH: 'Instrument, settings and conditions of a measurement.',
        },
    ),
}


def term_text(term: TermId, locale: PresentationLocale) -> str:
    entry = HTDT_TERMINOLOGY.get(term)
    if entry is None:
        return term.value
    return entry.preferred.get(locale) or entry.preferred[DEFAULT_LOCALE]


def terminology_fingerprint() -> str:
    """Stable digest of the terminology set for glossary/version checks."""

    payload = {
        entry.term_id.value: dict(sorted(entry.preferred.items(), key=lambda kv: kv[0].value))
        for entry in sorted(HTDT_TERMINOLOGY.values(), key=lambda e: e.term_id.value)
    }
    import json as _json

    return sha256(_json.dumps(payload, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Builtin workflow-first catalog (migration step 2 of #624).
#
# Keys are stable, dotted and semantic. The Japanese templates mirror the
# literals already shipped in the workflow shell so adopting the service does
# not change rendered Japanese text.


def _m(
    key: str,
    ja: str,
    en: str,
    *,
    context: str = '',
    production: bool = True,
) -> MessageDefinition:
    return MessageDefinition(
        key=key,
        templates={
            PresentationLocale.JAPANESE: ja,
            PresentationLocale.ENGLISH: en,
        },
        context=context,
        production=production,
    )


def build_workflow_catalog() -> MessageCatalog:
    """Core keys covering shell/workspaces/common actions/status."""

    messages = [
        _m('workspace.overview', '概要', 'Overview'),
        _m('workspace.room', '部屋', 'Room'),
        _m('workspace.measurement', '測定', 'Measurements'),
        _m('workspace.optimization', '最適化', 'Optimize'),
        _m('action.open', '開く', 'Open'),
        _m('action.save', '保存', 'Save'),
        _m('action.cancel', 'キャンセル', 'Cancel'),
        _m('action.retry', '再試行', 'Retry'),
        _m('action.close', '閉じる', 'Close'),
        _m('action.apply', '適用', 'Apply'),
        _m('action.delete', '削除', 'Delete'),
        _m('action.export', 'エクスポート', 'Export'),
        _m('action.import', 'インポート', 'Import'),
        _m('action.back', '戻る', 'Back'),
        _m('action.settings', '設定', 'Settings'),
        _m('action.help', 'ヘルプ', 'Help'),
        _m('status.loading', '読み込み中…', 'Loading…'),
        _m('status.working', '処理中…', 'Working…'),
        _m('status.completed', '完了', 'Completed'),
        _m('status.failed', '失敗', 'Failed'),
        _m('status.cancelled', 'キャンセル済み', 'Cancelled'),
        _m('status.no_data', 'データなし', 'No data'),
        _m('status.needs_reevaluation', '要再評価', 'Needs re-evaluation'),
        _m('status.not_configured', '未設定', 'Not configured'),
        _m('status.unavailable', '利用不可', 'Unavailable'),
        _m('lifecycle.current', '現在', 'Current'),
        _m('lifecycle.proposed', '提案', 'Proposed'),
        _m('lifecycle.as_built', '設置済み', 'As-built'),
        _m('lifecycle.measured', '実測済み', 'Measured'),
        _m('lifecycle.measured_unverified', '実測済み・未検証', 'Measured · unverified'),
        _m('common.project', 'プロジェクト', 'Project'),
        _m('common.revision', 'リビジョン', 'Revision'),
        _m('common.settings.title', '設定', 'Settings'),
        _m('common.settings.data_management', 'データ管理', 'Data management'),
        _m('common.settings.preferences', 'アプリケーション設定', 'Preferences'),
        _m('common.settings.support_diagnostics', 'サポートと診断', 'Support & diagnostics'),
        _m('common.settings.reference_libraries', '参照ライブラリ', 'Reference libraries'),
        _m('common.help.title', 'ヘルプと概念', 'Help & concepts'),
        _m('common.help.why', 'なぜ？', 'Why?'),
        _m('common.activity.title', 'アクティビティ', 'Activity'),
        _m('activity.running', '実行中', 'Running'),
        _m('activity.recent', '最近', 'Recent'),
        _m('activity.failed', '失敗・要対応', 'Failed · attention'),
        _m('activity.completed_for_historical', '{name} は過去のリビジョン {revision} 向けに完了', '{name} completed for historical revision {revision}'),
        _m('prefs.language', '言語', 'Language'),
        _m('prefs.language.system_default', 'システム既定', 'System default'),
        _m('prefs.restart_required', '言語の変更は再起動後に適用されます', 'Language changes apply after restart'),
    ]
    return MessageCatalog(messages)


__all__ = [
    'CatalogIntegrityError',
    'CatalogReport',
    'DEFAULT_LOCALE',
    'HTDT_TERMINOLOGY',
    'LanguagePolicy',
    'LocaleFormatRules',
    'LOCALE_FORMAT_RULES',
    'LOCALIZATION_AUTHORITY_VERSION',
    'LOCALIZATION_SCHEMA_VERSION',
    'LocalizationService',
    'MessageCatalog',
    'MessageDefinition',
    'PresentationLocale',
    'SUPPORTED_LOCALES',
    'TermId',
    'TerminologyEntry',
    'build_workflow_catalog',
    'detect_system_locale',
    'format_bytes',
    'format_date',
    'format_datetime',
    'format_decimal',
    'format_duration',
    'format_list',
    'format_percent',
    'resolve_locale',
    'template_placeholders',
    'term_text',
    'terminology_fingerprint',
]
