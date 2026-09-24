"""Composable command-palette / feature-search architecture (UX160).

``PaletteSearchProvider`` implementations each own one result domain —
commands/actions, scene entities, settings, help topics. ``PaletteResult``
is the unified row contract (stable UI-only result id, kind, title, location
subtitle, keywords, icon key, shortcut badge, availability, activation
target) so the palette renders every domain through one structured row.

Japanese labels ship with English/acronym alias keywords so the same provider
surface answers "スピーカー" and "speaker" / "FL" alike.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import unicodedata

from .command_registry import (
    CommandAvailability,
    CommandContext,
    CommandRegistry,
)
from .workflow_navigation import WorkspaceDeepLink


class PaletteResultKind(StrEnum):
    NAVIGATION = "navigation"
    ACTION = "action"
    ENTITY = "entity"
    DATA = "data"
    SETTINGS = "settings"
    HELP = "help"


COMMAND_CONTEXT_LABELS: dict[CommandContext, str] = {
    CommandContext.GLOBAL: "共通",
    CommandContext.OVERVIEW: "概要",
    CommandContext.ROOM: "部屋",
    CommandContext.MEASUREMENT: "測定",
    CommandContext.OPTIMIZATION: "最適化",
}


#: Group header labels for empty-query curated sections.
PALETTE_GROUP_LABELS: dict[PaletteResultKind, str] = {
    PaletteResultKind.NAVIGATION: "ナビゲーション",
    PaletteResultKind.ACTION: "操作",
    PaletteResultKind.ENTITY: "シーン項目",
    PaletteResultKind.DATA: "データ",
    PaletteResultKind.SETTINGS: "設定",
    PaletteResultKind.HELP: "ヘルプ",
}


@dataclass(frozen=True, slots=True)
class PaletteResult:
    """Unified palette row. ``result_id`` is stable per UI session only."""

    result_id: str
    kind: PaletteResultKind
    title: str
    subtitle: str | None = None
    keywords: tuple[str, ...] = ()
    icon_key: str | None = None
    shortcut: str | None = None
    available: bool = True
    disabled_reason: str | None = None
    command_id: str | None = None
    deep_link: WorkspaceDeepLink | None = None
    score: int = 0
    group: str | None = None


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _score_text(query: str, text: str, *, exact: int, prefix: int, substring: int) -> int:
    normalized_text = _normalized(text)
    if not query:
        return 0
    if normalized_text == query:
        return exact
    if normalized_text.startswith(query):
        return prefix
    if query in normalized_text:
        return substring
    return 0


class PaletteSearchProvider:
    """Provider contract: search + curated empty-query entries + activation."""

    name: str = "provider"

    def search(
        self,
        query: str,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        raise NotImplementedError

    def suggested(
        self,
        *,
        context: CommandContext | None = None,
        limit: int = 8,
    ) -> tuple[PaletteResult, ...]:
        return ()

    def activate(self, result: PaletteResult) -> bool:  # noqa: ARG002
        return False


class CommandPaletteProvider(PaletteSearchProvider):
    """Wraps the command registry: actions and navigation destinations."""

    name = "commands"

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    def _result_for(self, item) -> PaletteResult:
        definition = item.definition
        contexts = " / ".join(
            COMMAND_CONTEXT_LABELS[context] for context in definition.contexts
        )
        return PaletteResult(
            result_id=f"command:{definition.command_id}",
            kind=(
                PaletteResultKind.NAVIGATION
                if definition.deep_link is not None
                else PaletteResultKind.ACTION
            ),
            title=definition.display_name,
            subtitle=f"利用場所 · {contexts}",
            keywords=tuple(definition.keywords),
            icon_key="navigation" if definition.deep_link is not None else "action",
            shortcut=definition.shortcut,
            available=item.availability.enabled,
            disabled_reason=item.availability.disabled_reason,
            command_id=definition.command_id,
            deep_link=definition.deep_link,
            score=item.score,
        )

    def search(
        self,
        query: str,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        hits = self.registry.search(query, context=context)[:limit]
        return tuple(self._result_for(item) for item in hits)

    def suggested(
        self,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        """Curated navigation candidates: commands that carry a deep link."""
        results: list[PaletteResult] = []
        for definition in self.registry.definitions():
            if definition.deep_link is None:
                continue
            availability = self.registry.availability(definition.command_id)
            results.append(
                PaletteResult(
                    result_id=f"command:{definition.command_id}",
                    kind=PaletteResultKind.NAVIGATION,
                    title=definition.display_name,
                    subtitle="利用場所 · " + " / ".join(
                        COMMAND_CONTEXT_LABELS[context_]
                        for context_ in definition.contexts
                    ),
                    keywords=tuple(definition.keywords),
                    icon_key="navigation",
                    shortcut=definition.shortcut,
                    available=availability.enabled,
                    disabled_reason=availability.disabled_reason,
                    command_id=definition.command_id,
                    deep_link=definition.deep_link,
                    score=1.0,
                )
            )
        return tuple(results[:limit])

    def activate(self, result: PaletteResult) -> bool:
        if result.command_id is None or not result.available:
            return False
        return self.registry.execute(result.command_id)


class SceneEntityPaletteProvider(PaletteSearchProvider):
    """Scene entities (speakers, seats, measurement points) as focus targets."""

    name = "entity"

    KIND_ALIASES: Mapping[str, tuple[str, ...]] = {
        "speaker": ("speaker", "スピーカー", "ch"),
        "seat": ("seat", "座席", "seat"),
        "screen": ("screen", "スクリーン", "画面"),
        "projector": ("projector", "プロジェクター", "pj"),
        "display": ("display", "ディスプレイ", "tv"),
        "riser": ("riser", "ライザー", "段"),
        "furniture": ("furniture", "家具"),
        "av_equipment": ("av", "av機器", "receiver", "amplifier", "amp", "avr"),
        "measurement_point": ("測定点", "mic", "マイク", "mlp", "point", "測定位置"),
    }

    def __init__(
        self,
        entity_source: Callable[[], tuple],
        deep_link_builder: Callable[[object], WorkspaceDeepLink | None],
    ) -> None:
        self._entity_source = entity_source
        self._deep_link_builder = deep_link_builder

    def search(
        self,
        query: str,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        query = _normalized(query)
        if not query:
            return ()
        results: list[PaletteResult] = []
        for entity in self._entity_source():
            name = getattr(entity, "name", "")
            role = getattr(entity, "speaker_role", None) or ""
            entity_id = getattr(entity, "entity_id", "")
            kind = getattr(entity, "kind", "")
            score = max(
                _score_text(query, name, exact=100, prefix=80, substring=60),
                _score_text(query, role, exact=95, prefix=75, substring=55),
                _score_text(query, entity_id, exact=70, prefix=50, substring=30),
                max(
                    (
                        _score_text(query, alias, exact=55, prefix=45, substring=30)
                        for alias in self.KIND_ALIASES.get(kind, ())
                    ),
                    default=0,
                ),
            )
            if score == 0:
                continue
            link = self._deep_link_builder(entity)
            results.append(
                PaletteResult(
                    result_id=f"entity:{entity_id}",
                    kind=PaletteResultKind.ENTITY,
                    title=name,
                    subtitle=f"{_KIND_LABELS.get(kind, kind)} · 部屋",
                    keywords=(role, entity_id, kind),
                    icon_key="entity",
                    available=link is not None,
                    disabled_reason=(
                        None if link is not None else "部屋ワークスペースで開けません"
                    ),
                    deep_link=link,
                    score=score,
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return tuple(results[:limit])

    def suggested(
        self,
        *,
        context: CommandContext | None = None,
        limit: int = 8,
    ) -> tuple[PaletteResult, ...]:
        return ()


_KIND_LABELS: Mapping[str, str] = {
    "speaker": "スピーカー",
    "seat": "座席",
    "screen": "スクリーン",
    "projector": "プロジェクター",
    "display": "ディスプレイ",
    "riser": "ライザー",
    "furniture": "家具",
    "av_equipment": "AV機器",
    "measurement_point": "測定点",
}


@dataclass(frozen=True, slots=True)
class _StaticDestination:
    destination_id: str
    title: str
    subtitle: str
    keywords: tuple[str, ...]
    icon_key: str


class StaticPaletteProvider(PaletteSearchProvider):
    """Settings and help destinations: a small curated, keyword-aliased list."""

    def __init__(
        self,
        name: str,
        kind: PaletteResultKind,
        destinations: Iterable[_StaticDestination],
        on_open: Callable[[str], bool],
    ) -> None:
        self.name = name
        self._kind = kind
        self._destinations = tuple(destinations)
        self._on_open = on_open

    def search(
        self,
        query: str,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        normalized = _normalized(query)
        results: list[PaletteResult] = []
        for destination in self._destinations:
            score = max(
                _score_text(normalized, destination.title, exact=100, prefix=80, substring=60),
                max(
                    (
                        _score_text(normalized, kw, exact=70, prefix=55, substring=35)
                        for kw in destination.keywords
                    ),
                    default=0,
                ),
            )
            if score == 0:
                continue
            results.append(
                PaletteResult(
                    result_id=f"{self.name}:{destination.destination_id}",
                    kind=self._kind,
                    title=destination.title,
                    subtitle=destination.subtitle,
                    keywords=destination.keywords,
                    icon_key=destination.icon_key,
                    score=score,
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return tuple(results[:limit])

    def suggested(
        self,
        *,
        context: CommandContext | None = None,
        limit: int = 12,
    ) -> tuple[PaletteResult, ...]:
        """All curated static destinations are valid empty-query suggestions."""
        return tuple(
            PaletteResult(
                result_id=f"{self.name}:{destination.destination_id}",
                kind=self._kind,
                title=destination.title,
                subtitle=destination.subtitle,
                keywords=destination.keywords,
                icon_key=destination.icon_key,
                score=1.0,
            )
            for destination in self._destinations[:limit]
        )

    def activate(self, result: PaletteResult) -> bool:
        destination_id = result.result_id.split(":", 1)[1]
        return self._on_open(destination_id)


class PaletteSearchService:
    """Fuses providers; owns recent-result memory for the Suggested group."""

    def __init__(
        self,
        providers: Sequence[PaletteSearchProvider],
        *,
        on_deep_link: Callable[[WorkspaceDeepLink], bool] | None = None,
        recent_capacity: int = 8,
    ) -> None:
        self._providers = tuple(providers)
        self._on_deep_link = on_deep_link
        self._recent: list[str] = []
        self._recent_capacity = recent_capacity

    def providers(self) -> tuple[PaletteSearchProvider, ...]:
        return self._providers

    def search(
        self,
        query: str,
        *,
        context: CommandContext | None = None,
        limit: int = 24,
    ) -> tuple[PaletteResult, ...]:
        if not _normalized(query):
            return self.suggested(context=context)
        results: list[PaletteResult] = []
        for provider in self._providers:
            results.extend(provider.search(query, context=context, limit=limit))
        deduped: dict[str, PaletteResult] = {}
        for result in sorted(results, key=lambda item: item.score, reverse=True):
            deduped.setdefault(result.result_id, result)
        return tuple(deduped.values())[:limit]

    def suggested(
        self,
        *,
        context: CommandContext | None = None,
    ) -> tuple[PaletteResult, ...]:
        """Curated empty-query groups: Recent → Suggested → Navigation."""

        by_id: dict[str, PaletteResult] = {}
        for provider in self._providers:
            for result in provider.search("", context=context, limit=40):
                by_id.setdefault(result.result_id, result)

        groups: list[PaletteResult] = []
        for provider in self._providers:
            for result in provider.suggested(context=context):
                if result.result_id not in by_id:
                    by_id[result.result_id] = result

        seen: set[str] = set()
        for item in (
            by_id[result_id]
            for result_id in self._recent
            if result_id in by_id and by_id[result_id].available
        ):
            groups.append(_with_group(item, "最近使用"))
            seen.add(item.result_id)

        for item in by_id.values():
            if item.kind != PaletteResultKind.NAVIGATION or not item.available:
                continue
            if item.result_id in seen:
                continue
            groups.append(_with_group(item, "ナビゲーション"))
            seen.add(item.result_id)
        return tuple(groups)

    def activate(self, result: PaletteResult) -> bool:
        if not result.available:
            return False
        activated = False
        if result.command_id is not None:
            for provider in self._providers:
                if isinstance(provider, CommandPaletteProvider):
                    activated = provider.activate(result)
                    break
        if not activated and result.deep_link is not None and self._on_deep_link is not None:
            activated = bool(self._on_deep_link(result.deep_link))
        if not activated:
            for provider in self._providers:
                if result.result_id.startswith(f"{provider.name}:"):
                    activated = provider.activate(result)
                    break
        if activated:
            self._remember(result.result_id)
        return activated

    def _remember(self, result_id: str) -> None:
        if result_id in self._recent:
            self._recent.remove(result_id)
        self._recent.insert(0, result_id)
        del self._recent[self._recent_capacity :]


def _with_group(item: PaletteResult, group: str) -> PaletteResult:
    return PaletteResult(
        result_id=item.result_id,
        kind=item.kind,
        title=item.title,
        subtitle=item.subtitle,
        keywords=item.keywords,
        icon_key=item.icon_key,
        shortcut=item.shortcut,
        available=item.available,
        disabled_reason=item.disabled_reason,
        command_id=item.command_id,
        deep_link=item.deep_link,
        score=item.score,
        group=group,
    )


def settings_destinations() -> tuple[_StaticDestination, ...]:
    return (
        _StaticDestination(
            "settings.data",
            "データとバックアップの設定",
            "設定 · データ管理",
            ("設定", "データ", "バックアップ", "backup", "preferences", "管理"),
            "settings",
        ),
    )


def help_destinations() -> tuple[_StaticDestination, ...]:
    return (
        _StaticDestination(
            "help.shortcuts",
            "キーボードショートカット一覧",
            "ヘルプ",
            ("shortcut", "キーボード", "shortcuts", "ヘルプ", "help", "ショートカット"),
            "help",
        ),
        _StaticDestination(
            "help.palette",
            "コマンドパレットの使い方",
            "ヘルプ",
            ("palette", "パレット", "検索", "search", "使い方", "help"),
            "help",
        ),
    )


__all__ = [
    "CommandPaletteProvider",
    "PaletteResult",
    "PaletteResultKind",
    "PaletteSearchProvider",
    "PaletteSearchService",
    "PALETTE_GROUP_LABELS",
    "SceneEntityPaletteProvider",
    "StaticPaletteProvider",
    "help_destinations",
    "settings_destinations",
]
