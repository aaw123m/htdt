"""Embedded Help & Glossary registry (#623).

One canonical, structured, versioned source for offline product help:
authority states, workflows and "why unavailable" explanations. Widgets,
readiness surfaces and the Command Palette's help search (#585) index this
registry instead of embedding long documentation strings.

Contents:

* :class:`HelpTopic` — stable topic ID, localized title/summary/sections,
  keywords/aliases (Japanese, English and acronyms like REW/SPL/Pareto),
  related commands, workspace deep links, related topics, optional
  reason-code bindings and a content version;
* :class:`GlossaryTerm` — plain-language meaning, "what it is not", and the
  provenance implication, sharing the frozen terminology from
  :mod:`htdt.localization` (#624);
* :class:`HelpRegistry` — canonical lookup plus integrity validation:
  duplicate topic IDs, broken related-topic / command / deep-link
  references, missing localization, duplicate reason-code bindings;
* :func:`build_help_registry` — the shipped topic set;
* :func:`shortcut_reference` — keyboard/mouse reference generated from the
  :class:`~htdt.command_registry.CommandRegistry` so shortcut documentation
  cannot drift from registered commands.

Help is user documentation: it never mutates project authority, seeds no
example data, and never exports logs or private project data (that boundary
belongs to Support & Diagnostics, #604).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .command_registry import CommandRegistry, default_command_definitions
from .localization import HTDT_TERMINOLOGY, PresentationLocale, TermId
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


HELP_SCHEMA_VERSION = 1
HELP_CONTENT_VERSION = 'htdt-help-1'


class HelpSection(BaseModel):
    """One localized section of a topic body."""

    model_config = ConfigDict(frozen=True)

    heading: str = Field(min_length=1)
    body: str = Field(min_length=1)


class LocalizedTopicContent(BaseModel):
    """The per-locale rendering of a help topic."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    sections: tuple[HelpSection, ...] = ()


class HelpTopic(BaseModel):
    """A canonical help topic.

    ``reason_codes`` bind stable domain reason codes (e.g. a readiness or
    disabled-command code) to this topic so "Why?" actions resolve without
    matching rendered Japanese/English text.
    """

    model_config = ConfigDict(frozen=True)

    topic_id: str = Field(min_length=1, pattern=r'^[a-z0-9]+(\.[a-z0-9_]+)*$')
    content: Mapping[PresentationLocale, LocalizedTopicContent]
    keywords: tuple[str, ...] = ()
    related_commands: tuple[str, ...] = ()
    deep_links: tuple[WorkspaceDeepLink, ...] = ()
    related_topics: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    content_version: str = HELP_CONTENT_VERSION

    @model_validator(mode='after')
    def valid_topic(self) -> 'HelpTopic':
        normalized = {
            PresentationLocale(locale): content
            for locale, content in self.content.items()
        }
        object.__setattr__(self, 'content', normalized)
        for name, values in (
            ('keywords', self.keywords),
            ('related_commands', self.related_commands),
            ('related_topics', self.related_topics),
            ('reason_codes', self.reason_codes),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'help topic {self.topic_id!r} has duplicate {name}')
        for related in self.related_topics:
            if related == self.topic_id:
                raise ValueError(
                    f'help topic {self.topic_id!r} cannot relate to itself'
                )
        return self

    def localized(self, locale: PresentationLocale) -> LocalizedTopicContent:
        content = self.content.get(locale) or self.content.get(
            PresentationLocale.JAPANESE
        )
        if content is None:
            content = next(iter(self.content.values()))
        return content


class GlossaryTerm(BaseModel):
    """One glossary entry; terminology wording is shared with #624."""

    model_config = ConfigDict(frozen=True)

    term_id: TermId
    meaning: Mapping[PresentationLocale, str]
    not_this: Mapping[PresentationLocale, str] = Field(default_factory=dict)
    provenance_implication: Mapping[PresentationLocale, str] = Field(
        default_factory=dict
    )
    related_topics: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_term(self) -> 'GlossaryTerm':
        for name in ('meaning', 'not_this', 'provenance_implication'):
            object.__setattr__(
                self,
                name,
                {PresentationLocale(k): v for k, v in getattr(self, name).items()},
            )
        return self

    def label(self, locale: PresentationLocale) -> str:
        entry = HTDT_TERMINOLOGY[self.term_id]
        return entry.preferred.get(locale) or entry.preferred[
            PresentationLocale.JAPANESE
        ]


@dataclass(frozen=True, slots=True)
class HelpIntegrityReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class HelpSearchResult:
    topic: HelpTopic
    score: int


@dataclass(frozen=True, slots=True)
class ShortcutDoc:
    """Generated keyboard/mouse reference row."""

    command_id: str
    shortcut: str
    context: str
    display_name: str


class HelpRegistry:
    """Canonical help/glossary registry with integrity validation."""

    def __init__(
        self,
        topics: Iterable[HelpTopic],
        glossary: Iterable[GlossaryTerm] = (),
    ) -> None:
        topics_by_id: dict[str, HelpTopic] = {}
        for topic in topics:
            if topic.topic_id in topics_by_id:
                raise ValueError(f'duplicate help topic id: {topic.topic_id}')
            topics_by_id[topic.topic_id] = topic
        glossary_by_term: dict[TermId, GlossaryTerm] = {}
        for term in glossary:
            if term.term_id in glossary_by_term:
                raise ValueError(f'duplicate glossary term: {term.term_id}')
            glossary_by_term[term.term_id] = term
        self._topics = topics_by_id
        self._glossary = glossary_by_term
        self._reason_index: dict[str, str] = {}
        for topic in topics_by_id.values():
            for code in topic.reason_codes:
                if code in self._reason_index:
                    raise ValueError(
                        f'reason code {code!r} bound to multiple topics: '
                        f'{self._reason_index[code]} and {topic.topic_id}'
                    )
                self._reason_index[code] = topic.topic_id

    def __len__(self) -> int:
        return len(self._topics)

    def topic_ids(self) -> tuple[str, ...]:
        return tuple(self._topics)

    def get(self, topic_id: str) -> HelpTopic | None:
        return self._topics.get(topic_id)

    def require(self, topic_id: str) -> HelpTopic:
        topic = self._topics.get(topic_id)
        if topic is None:
            raise KeyError(f'unknown help topic: {topic_id}')
        return topic

    def glossary(self) -> tuple[GlossaryTerm, ...]:
        return tuple(self._glossary.values())

    def glossary_term(self, term_id: TermId) -> GlossaryTerm | None:
        return self._glossary.get(term_id)

    def topic_for_reason(self, reason_code: str) -> HelpTopic | None:
        """Contextual-help lookup by stable domain reason code."""

        topic_id = self._reason_index.get(reason_code)
        return None if topic_id is None else self._topics.get(topic_id)

    def search(self, query: str, *, limit: int = 10) -> tuple[HelpSearchResult, ...]:
        """Alias search powering the Command Palette help provider (#585).

        Matches topic ids, localized titles/summaries and keyword aliases in
        both supported languages — the user can type ``speaker`` or
        ``スピーカー`` without switching the whole UI locale.
        """

        normalized = query.strip().lower()
        if not normalized:
            return ()
        results: list[HelpSearchResult] = []
        for topic in self._topics.values():
            score = 0
            if topic.topic_id.lower() == normalized:
                score = 100
            else:
                if normalized in topic.topic_id.lower():
                    score = max(score, 40)
                for locale, content in topic.content.items():
                    if normalized in content.title.lower():
                        score = max(score, 60)
                    if normalized in content.summary.lower():
                        score = max(score, 30)
                for keyword in topic.keywords:
                    kw = keyword.lower()
                    if kw == normalized:
                        score = max(score, 80)
                    elif normalized in kw or kw in normalized:
                        score = max(score, 45)
            if score:
                results.append(HelpSearchResult(topic=topic, score=score))
        results.sort(key=lambda item: (-item.score, item.topic.topic_id))
        return tuple(results[:limit])

    def validate(
        self,
        *,
        command_ids: Iterable[str] | None = None,
        required_locales: Iterable[PresentationLocale] = (
            PresentationLocale.JAPANESE,
            PresentationLocale.ENGLISH,
        ),
    ) -> HelpIntegrityReport:
        """CI integrity checks over the shipped registry.

        * related-topic references resolve;
        * related command references resolve (against the command registry
          when provided);
        * deep-link workspace ids resolve;
        * production topics carry every required locale;
        * glossary terms reference real topics.
        """

        errors: list[str] = []
        warnings: list[str] = []
        known_commands = (
            set(command_ids) if command_ids is not None else None
        )
        required = tuple(required_locales)
        for topic in self._topics.values():
            for related in topic.related_topics:
                if related not in self._topics:
                    errors.append(
                        f'topic {topic.topic_id!r} links unknown related topic {related!r}'
                    )
            if known_commands is not None:
                for command_id in topic.related_commands:
                    if command_id not in known_commands:
                        errors.append(
                            f'topic {topic.topic_id!r} references unknown command {command_id!r}'
                        )
            for link in topic.deep_links:
                if not isinstance(link.workspace, WorkspaceId):
                    errors.append(
                        f'topic {topic.topic_id!r} has invalid deep link {link!r}'
                    )
            missing = [l.value for l in required if l not in topic.content]
            if missing:
                errors.append(
                    f'topic {topic.topic_id!r} missing locales: {missing}'
                )
        for term in self._glossary.values():
            for related in term.related_topics:
                if related not in self._topics:
                    errors.append(
                        f'glossary term {term.term_id.value!r} links unknown topic {related!r}'
                    )
        return HelpIntegrityReport(tuple(errors), tuple(warnings))

    def fingerprint(self) -> str:
        """Stable digest over topic ids + content versions."""

        import json as _json

        payload = {
            topic_id: topic.content_version
            for topic_id, topic in sorted(self._topics.items())
        }
        return sha256(
            _json.dumps(payload, sort_keys=True).encode('utf-8')
        ).hexdigest()


def shortcut_reference(
    registry: CommandRegistry,
) -> tuple[ShortcutDoc, ...]:
    """Generate the keyboard/mouse reference from registered commands.

    Only semantics not represented in the CommandRegistry (viewport mouse
    navigation, numeric-edit modifiers) are documented manually elsewhere;
    shortcut rows always come from the registry so docs cannot drift.
    """

    docs: list[ShortcutDoc] = []
    for definition in registry.definitions():
        shortcuts: tuple[str, ...] = ()
        if definition.shortcut:
            shortcuts = (definition.shortcut,)
        shortcuts += tuple(definition.shortcut_aliases)
        context = ','.join(sorted(c.value for c in definition.contexts))
        for shortcut in shortcuts:
            docs.append(
                ShortcutDoc(
                    command_id=definition.command_id,
                    shortcut=shortcut,
                    context=context,
                    display_name=definition.display_name,
                )
            )
    return tuple(docs)


def default_command_ids() -> frozenset[str]:
    """Command ids of the shipped command registry, for CI validation."""

    return frozenset(d.command_id for d in default_command_definitions())


# ---------------------------------------------------------------------------
# Builtin content.


def _topic(
    topic_id: str,
    ja_title: str,
    en_title: str,
    ja_summary: str,
    en_summary: str,
    *,
    keywords: tuple[str, ...] = (),
    related_commands: tuple[str, ...] = (),
    deep_links: tuple[WorkspaceDeepLink, ...] = (),
    related_topics: tuple[str, ...] = (),
    reason_codes: tuple[str, ...] = (),
    ja_sections: tuple[tuple[str, str], ...] = (),
    en_sections: tuple[tuple[str, str], ...] = (),
) -> HelpTopic:
    return HelpTopic(
        topic_id=topic_id,
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(
                title=ja_title,
                summary=ja_summary,
                sections=tuple(
                    HelpSection(heading=h, body=b) for h, b in ja_sections
                ),
            ),
            PresentationLocale.ENGLISH: LocalizedTopicContent(
                title=en_title,
                summary=en_summary,
                sections=tuple(
                    HelpSection(heading=h, body=b) for h, b in en_sections
                ),
            ),
        },
        keywords=keywords,
        related_commands=related_commands,
        deep_links=deep_links,
        related_topics=related_topics,
        reason_codes=reason_codes,
    )


def build_help_registry() -> HelpRegistry:
    """The canonical offline help/glossary content shipped with the build."""

    topics = [
        _topic(
            'start.getting_started',
            'はじめに',
            'Getting started',
            'プロジェクトの作成から検証までの基本の流れ。',
            'The basic flow from creating a project to verification.',
            keywords=('getting started', 'はじめ', 'チュートリアル', 'プロジェクト'),
            deep_links=(WorkspaceDeepLink(WorkspaceId.OVERVIEW),),
            related_topics=('concept.project_identity', 'workflow.room_cad'),
            ja_sections=(
                ('流れ', 'プロジェクトを作成し、部屋を定義し、機器を配置し、測定・予測・比較を行います。'),
            ),
            en_sections=(
                ('Flow', 'Create a project, define the room, place equipment, then measure, predict and compare.'),
            ),
        ),
        _topic(
            'concept.scene_vs_revision',
            'シーンとシーンリビジョン',
            'Scene vs SceneRevision',
            'シーンは現在の作業状態、シーンリビジョンは不変の履歴スナップショットです。',
            'A scene is the live working state; a SceneRevision is an immutable historical snapshot.',
            keywords=('scene', 'revision', 'シーン', 'リビジョン', '履歴'),
            deep_links=(WorkspaceDeepLink(WorkspaceId.ROOM, section='history'),),
            related_topics=('concept.lifecycle_states', 'concept.stale_historical'),
            ja_sections=(
                ('不変性', 'リビジョンは保存後に変化しません。編集は新しいリビジョンを作ります。'),
            ),
            en_sections=(
                ('Immutability', 'Revisions never change after saving; editing creates a new revision.'),
            ),
        ),
        _topic(
            'concept.lifecycle_states',
            '現在・提案・設置済み・実測済み',
            'Current vs Proposed vs As-built vs Measured',
            'authorityのライフサイクル状態の違い。',
            'The difference between lifecycle states of an authority.',
            keywords=('current', 'proposed', 'as-built', 'measured', '現在', '提案', '設置済み', '実測'),
            related_topics=('concept.scene_vs_revision', 'concept.evidence_vs_assumption'),
            ja_sections=(
                ('意味', '現在は設計の現行状態、提案は代替案、設置済みは実際の構成、実測済みは計測エビデンスがある状態です。実測済みは検証済みを意味しません。'),
            ),
            en_sections=(
                ('Meaning', 'Current is the live design, Proposed is an alternative, As-built is the installed configuration, Measured has measurement evidence. Measured does not imply validated.'),
            ),
        ),
        _topic(
            'concept.evidence_vs_assumption',
            'エビデンスと仮定',
            'Evidence vs assumption',
            'UNKNOWN・仮定・推定・未検証の扱い。暗黙補完は行いません。',
            'How UNKNOWN, assumed, inferred and unverified are treated; nothing is silently filled in.',
            keywords=('evidence', 'assumption', 'UNKNOWN', '不明', '仮定', '推定', '未検証', '証拠'),
            related_topics=('concept.lifecycle_states', 'trouble.unknown_value'),
            ja_sections=(
                ('原則', 'UNKNOWNは0や推定値で補完されません。仮定・推定は出所が明示されます。'),
            ),
            en_sections=(
                ('Principle', 'UNKNOWN is never filled with 0 or a guess; assumptions and inferences carry their provenance.'),
            ),
        ),
        _topic(
            'concept.staged_vs_promoted',
            'Captureのステージと昇格',
            'Capture staged vs promoted',
            'Capture受信データは検証されるまでステージ状態です。',
            'Received capture data stays staged until validated and promoted.',
            keywords=('capture', 'staged', 'promoted', 'ステージ', '昇格', 'RoomPlan'),
            related_topics=('trouble.capture_staged',),
            ja_sections=(
                ('ステージ', '受信直後のCaptureはステージに留まり、検証・昇格で初めてプロジェクトauthorityになります。'),
            ),
            en_sections=(
                ('Staged', 'A fresh capture stays staged; only validation and promotion turn it into project authority.'),
            ),
        ),
        _topic(
            'concept.stale_historical',
            '現在・履歴・古い',
            'Current vs historical vs stale',
            '古い結果は無効ではなく、過去のリビジョンに対して有効です。',
            'A stale result is not invalid; it remains valid evidence for a past revision.',
            keywords=('stale', 'historical', 'current', '古い', '履歴', '現在'),
            related_topics=('concept.scene_vs_revision', 'trouble.stale_result'),
            ja_sections=(
                ('stale', '依存が変わると結果は「古い」になります。旧リビジョンへの証拠としては有効です。'),
            ),
            en_sections=(
                ('Stale', 'When a dependency changes, results become stale; they remain valid evidence for the old revision.'),
            ),
        ),
        _topic(
            'concept.project_identity',
            'プロジェクト識別と表示名',
            'Project identity vs display name',
            'プロジェクトの同一性は内部IDで決まり、表示名は自由に変えられます。',
            'Project identity is the internal ID; the display name is free to change.',
            keywords=('project', 'identity', 'プロジェクト', '表示名', '名前'),
            related_topics=('start.getting_started', 'concept.reusable_vs_project'),
            ja_sections=(
                ('識別', '同じ表示名でもauthorityが異なれば別物です。衝突時に表示名で上書きしません。'),
            ),
            en_sections=(
                ('Identity', 'Identical display names do not mean the same authority; nothing is overwritten by name.'),
            ),
        ),
        _topic(
            'concept.reusable_vs_project',
            '再利用定義とプロジェクト内実体',
            'Reusable definition vs project instance',
            '機器・処理・目標などの定義は複数プロジェクトで再利用できます。',
            'Equipment, treatment, target and other definitions can be reused across projects.',
            keywords=('library', 'equipment', 'definition', 'speaker', 'ライブラリ', '機器', '定義', '再利用', 'スピーカー'),
            related_topics=('concept.project_identity',),
            ja_sections=(
                ('スコープ', '定義は内蔵・ユーザーライブラリ・プロジェクト内・インポート済みのスコープを持ちます。'),
            ),
            en_sections=(
                ('Scope', 'Definitions carry a scope: built-in, user library, project-local or imported.'),
            ),
        ),
        _topic(
            'workflow.room_cad',
            '部屋 / CAD編集',
            'Room / CAD editing',
            '3D CADでの選択・移動・スナップ・数値編集・Undo/Redo。',
            'Selection, gizmo, snapping, numeric edit and Undo/Redo in the 3D CAD editor.',
            keywords=('room', 'cad', '部屋', '3D', 'スナップ', 'gizmo', '壁', 'opening'),
            deep_links=(WorkspaceDeepLink(WorkspaceId.ROOM),),
            related_commands=('navigation.room', 'project.save'),
            related_topics=('concept.scene_vs_revision',),
            ja_sections=(
                ('基本操作', 'ビューポートでオブジェクトを選択し、gizmoや数値入力で配置します。'),
            ),
            en_sections=(
                ('Basics', 'Select objects in the viewport and place them with the gizmo or numeric fields.'),
            ),
        ),
        _topic(
            'workflow.measurements',
            '測定とREW',
            'Measurements and REW',
            'REW実測の取り込み、取得コンテキスト、品質と繰り返し。',
            'REW imports, acquisition context, quality and repeats.',
            keywords=('measurement', 'REW', 'SPL', 'IR', '実測', '測定', '品質', 'mic'),
            deep_links=(WorkspaceDeepLink(WorkspaceId.MEASUREMENT),),
            related_commands=('navigation.measurements',),
            related_topics=('concept.acquisition_context', 'concept.lifecycle_states'),
            ja_sections=(
                ('取り込み', 'REW APIまたはファイルから実測を取り込み、取得コンテキストと品質を保持します。'),
            ),
            en_sections=(
                ('Import', 'Import measurements via the REW API or files, keeping acquisition context and quality.'),
            ),
        ),
        _topic(
            'concept.acquisition_context',
            '取得コンテキスト',
            'AcquisitionContext',
            '測定の機器・設定・条件を束ねたauthority。',
            'The authority bundling instrument, settings and conditions of a measurement.',
            keywords=('acquisition', 'context', '取得', 'コンテキスト', 'REW', 'mic', 'calibration'),
            related_topics=('workflow.measurements',),
            ja_sections=(
                ('内容', 'マイク・校正・レベル・タイミング参照など、測定の解釈に必要な条件を記録します。'),
            ),
            en_sections=(
                ('Contents', 'Microphone, calibration, level and timing reference — everything needed to interpret the measurement.'),
            ),
        ),
        _topic(
            'workflow.prediction_optimize',
            '予測と最適化',
            'Prediction and optimization',
            '予測の前提、capability限界、Pareto、そしてstaleになる理由。',
            'Prediction prerequisites, capability limits, Pareto and why results go stale.',
            keywords=('prediction', 'optimize', 'Pareto', '予測', '最適化', '候補', 'solver'),
            deep_links=(WorkspaceDeepLink(WorkspaceId.OPTIMIZATION),),
            related_commands=('navigation.optimization',),
            related_topics=('concept.stale_historical', 'trouble.prediction_unavailable'),
            ja_sections=(
                ('前提', '予測には機器のsource model等の入力authorityが必要です。結果はリビジョンに紐づきます。'),
            ),
            en_sections=(
                ('Prerequisites', 'Prediction needs input authorities such as the speaker source model; results bind to a revision.'),
            ),
        ),
        _topic(
            'workflow.data_recovery',
            '保存・復元・バックアップ',
            'Save, recovery and backup',
            '保存とrecovery draft、バックアップ、プロジェクトエクスポートの違い。',
            'The difference between save, recovery drafts, backups and project export.',
            keywords=('save', 'backup', 'restore', 'export', '保存', 'バックアップ', '復元', 'エクスポート'),
            related_commands=('project.save',),
            related_topics=('concept.project_identity',),
            ja_sections=(
                ('違い', 'バックアップは災害復旧、エクスポートは移行、診断パッケージはデバッグ用です。混同しません。'),
            ),
            en_sections=(
                ('Distinction', 'Backups are disaster recovery, exports are portability, diagnostic packages are for debugging.'),
            ),
        ),
        _topic(
            'trouble.prediction_unavailable',
            '予測が利用できない理由',
            'Why prediction is unavailable',
            'source model未設定など、前提authorityの欠落を説明します。',
            'Explains missing prerequisites such as a speaker source model.',
            keywords=('unavailable', 'prediction', '利用不可', '予測', 'why', 'なぜ'),
            reason_codes=(
                'prediction.unavailable.source_model_missing',
                'prediction.unavailable.geometry_unsupported',
                'prediction.unavailable.provider_not_ready',
            ),
            deep_links=(WorkspaceDeepLink(WorkspaceId.ROOM, section='placement'),),
            related_topics=('workflow.prediction_optimize', 'concept.evidence_vs_assumption'),
            ja_sections=(
                ('原因', '予測は入力authorityが揃わないと実行できません。欠落している項目を確認してください。'),
            ),
            en_sections=(
                ('Cause', 'Prediction cannot run until required input authorities exist. Check the listed gaps.'),
            ),
        ),
        _topic(
            'trouble.unknown_value',
            '値がUNKNOWNの理由',
            'Why a value is UNKNOWN',
            'UNKNOWNは欠落を明示する状態であり、0や推定値の代替ではありません。',
            'UNKNOWN explicitly marks absence; it is not a stand-in for 0 or a guess.',
            keywords=('UNKNOWN', '不明', 'missing', 'なし'),
            reason_codes=('value.unknown',),
            related_topics=('concept.evidence_vs_assumption',),
            ja_sections=(
                ('意味', '測定・推定・既定値が存在しないためUNKNOWNと表示されます。'),
            ),
            en_sections=(
                ('Meaning', 'No measurement, inference or default exists, so the value is shown as UNKNOWN.'),
            ),
        ),
        _topic(
            'trouble.capture_staged',
            'Captureがステージのままの理由',
            'Why a capture is only staged',
            'ステージは未検証の保留状態。昇格すると初めてauthorityになります。',
            'Staged means held for verification; promotion is what creates authority.',
            keywords=('capture', 'staged', 'ステージ', '昇格'),
            reason_codes=('capture.staged_pending',),
            related_topics=('concept.staged_vs_promoted',),
            ja_sections=(
                ('昇格', '内容を検証し昇格させると、プロジェクトauthorityとして利用できます。'),
            ),
            en_sections=(
                ('Promotion', 'Verify and promote the capture to use it as project authority.'),
            ),
        ),
        _topic(
            'trouble.revisions_incomparable',
            'リビジョンを空間比較できない理由',
            'Why revisions cannot be spatially compared',
            '比較は同一Digital Twin上のexact authority比較に限られます。',
            'Comparison is limited to exact authorities on the same digital twin.',
            keywords=('compare', 'revision', '比較', 'リビジョン'),
            reason_codes=('comparison.incomparable_revisions',),
            related_topics=('concept.scene_vs_revision',),
            ja_sections=(
                ('制限', '別プロジェクトや座標系の異なるrevision同士は空間的に比較できません。'),
            ),
            en_sections=(
                ('Limit', 'Revisions from different projects or coordinate spaces cannot be compared spatially.'),
            ),
        ),
        _topic(
            'trouble.stale_result',
            '結果がstaleになる理由',
            'Why results become stale',
            '依存するscene・機器・測定が変わると結果は「古い」になります。',
            'Results become stale when their scene, equipment or measurement dependencies change.',
            keywords=('stale', '古い', 'invalidated', '再評価'),
            reason_codes=('result.stale', 'result.completed_for_historical_input'),
            related_topics=('concept.stale_historical', 'workflow.prediction_optimize'),
            ja_sections=(
                ('再評価', 'staleな結果は旧リビジョンの証拠として残ります。現行値は再実行で得られます。'),
            ),
            en_sections=(
                ('Re-evaluate', 'A stale result stays valid for its old revision; re-run to get the current value.'),
            ),
        ),
    ]

    glossary = [
        GlossaryTerm(term_id=term, meaning=entry.gloss, related_topics=())
        for term, entry in HTDT_TERMINOLOGY.items()
    ]
    # Attach cross-references after construction (pydantic frozen models).
    glossary = [
        term.model_copy(update={'related_topics': _glossary_links(term.term_id)})
        for term in glossary
    ]
    return HelpRegistry(topics, glossary)


def _glossary_links(term_id: TermId) -> tuple[str, ...]:
    mapping: dict[TermId, tuple[str, ...]] = {
        TermId.CURRENT_SYSTEM: ('concept.lifecycle_states',),
        TermId.PROPOSED_SYSTEM: ('concept.lifecycle_states',),
        TermId.AS_BUILT: ('concept.lifecycle_states',),
        TermId.MEASURED: ('concept.lifecycle_states',),
        TermId.SCENE_REVISION: ('concept.scene_vs_revision',),
        TermId.CAPTURE_STAGED: ('concept.staged_vs_promoted',),
        TermId.CAPTURE_PROMOTED: ('concept.staged_vs_promoted',),
        TermId.UNKNOWN: ('concept.evidence_vs_assumption', 'trouble.unknown_value'),
        TermId.ASSUMED: ('concept.evidence_vs_assumption',),
        TermId.INFERRED: ('concept.evidence_vs_assumption',),
        TermId.UNVERIFIED: ('concept.evidence_vs_assumption',),
        TermId.STALE: ('concept.stale_historical', 'trouble.stale_result'),
        TermId.HISTORICAL: ('concept.stale_historical',),
        TermId.SYSTEM_VARIANT: ('concept.lifecycle_states',),
        TermId.ACQUISITION_CONTEXT: ('concept.acquisition_context',),
    }
    return mapping.get(term_id, ())


__all__ = [
    'GlossaryTerm',
    'HELP_CONTENT_VERSION',
    'HELP_SCHEMA_VERSION',
    'HelpIntegrityReport',
    'HelpRegistry',
    'HelpSearchResult',
    'HelpSection',
    'HelpTopic',
    'LocalizedTopicContent',
    'ShortcutDoc',
    'build_help_registry',
    'default_command_ids',
    'shortcut_reference',
]
