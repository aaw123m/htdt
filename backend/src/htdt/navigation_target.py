"""Typed navigation targets shared across the workflow shell.

NavigationTarget is the single transport object for "show this thing" requests:
it carries a stable, closed-vocabulary kind, the exact stable object references,
optional authority context (project, scene revision, system variant), a
preferred surface, and presentation intent — never a bare string that a
workspace could resolve by name or newest-match.

Workspaces declare which target kinds they can focus; resolution maps a target
onto a concrete WorkspaceDeepLink (exact revision/variant context preserved) or
returns an explicit, actionable failure. ``htdt://nav/v1/...`` is the versioned
URI grammar; legacy ``htdt://workspace/...`` links parse into targets unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from .workflow_navigation import (
    ApplicationDestinationId,
    DestinationId,
    NavigationScope,
    WorkspaceDeepLink,
    WorkspaceId,
    normalize_destination_id,
)


class NavigationTargetKind(StrEnum):
    """Closed vocabulary of addressable domain objects and destinations."""

    WORKSPACE = "workspace"
    PROJECT = "project"
    SCENE_ENTITY = "scene_entity"
    SCENE_REVISION = "scene_revision"
    SYSTEM_VARIANT = "system_variant"
    MEASUREMENT = "measurement"
    MEASUREMENT_CAMPAIGN = "measurement_campaign"
    PREDICTION_RESULT = "prediction_result"
    OPTIMIZATION_CANDIDATE = "optimization_candidate"
    OPTIMIZATION_COMPARISON = "optimization_comparison"
    COMMISSIONING_EVALUATION = "commissioning_evaluation"
    CAPTURE_DELIVERY = "capture_delivery"
    CAPTURE_INBOX_ITEM = "capture_inbox_item"
    EQUIPMENT_DEFINITION = "equipment_definition"
    INSTALLED_EQUIPMENT_INSTANCE = "installed_equipment_instance"
    TREATMENT_DEFINITION = "treatment_definition"
    PROJECT_CHECKPOINT = "project_checkpoint"
    ACTIVITY_JOB = "activity_job"
    HELP_TOPIC = "help_topic"
    OPERATING_PRESET = "operating_preset"
    HEALTH_BASELINE = "health_baseline"
    HEALTH_CHECK_PLAN = "health_check_plan"
    PROJECT_NOTE = "project_note"
    CALIBRATION_PLAN = "calibration_plan"
    AV_SYNC_CONDITION = "av_sync_condition"


class NavigationIntent(StrEnum):
    """How the destination should present the resolved object."""

    INSPECT = "inspect"
    EDIT = "edit"
    COMPARE = "compare"
    PROVENANCE = "provenance"
    LOCATE_SOURCE = "locate_source"


_NAV_URI_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """Exact, transport-only description of a navigation request.

    ``object_ids`` are the stable authority references (entity ids, variant ids,
    revision ids, job ids) — names and "newest" lookups are never substitutes.
    ``revision_id``/``system_variant_id`` pin explicit authority context so a
    target still resolves to the same evidence after later saves. Bounded
    multi-select sets are expressed by supplying more than one object id.
    """

    kind: NavigationTargetKind
    object_ids: tuple[str, ...] = ()
    scope: NavigationScope = NavigationScope.PROJECT
    project_id: str | None = None
    revision_id: str | None = None
    system_variant_id: str | None = None
    preferred_destination: DestinationId | None = None
    preferred_section: str | None = None
    intent: NavigationIntent = NavigationIntent.INSPECT
    referrer: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NavigationTargetKind):
            object.__setattr__(self, "kind", NavigationTargetKind(self.kind))
        if not isinstance(self.scope, NavigationScope):
            object.__setattr__(self, "scope", NavigationScope(self.scope))
        if not isinstance(self.intent, NavigationIntent):
            object.__setattr__(self, "intent", NavigationIntent(self.intent))
        if self.preferred_destination is not None:
            object.__setattr__(
                self,
                "preferred_destination",
                normalize_destination_id(self.preferred_destination),
            )
        object.__setattr__(self, "object_ids", tuple(self.object_ids))

    @property
    def primary_id(self) -> str | None:
        return self.object_ids[0] if self.object_ids else None

    def as_uri(self) -> str:
        params: list[tuple[str, str]] = [("id", item) for item in self.object_ids]
        params.append(("scope", self.scope.value))
        if self.project_id is not None:
            params.append(("project", self.project_id))
        if self.revision_id is not None:
            params.append(("revision", self.revision_id))
        if self.system_variant_id is not None:
            params.append(("variant", self.system_variant_id))
        if self.preferred_destination is not None:
            params.append(("destination", self.preferred_destination.value))
        if self.preferred_section is not None:
            params.append(("section", self.preferred_section))
        if self.intent != NavigationIntent.INSPECT:
            params.append(("intent", self.intent.value))
        if self.referrer is not None:
            params.append(("referrer", self.referrer))
        query = urlencode(params)
        return f"htdt://nav/{_NAV_URI_VERSION}/{self.kind.value}?{query}"


#: Route table: kind -> owning destinations in preference order. Application
#: destinations take priority where they exist; project-scope fallbacks keep
#: deep links resolvable when an application surface is not registered.
TARGET_KIND_ROUTES: dict[
    NavigationTargetKind, tuple[tuple[DestinationId, str | None], ...]
] = {
    NavigationTargetKind.WORKSPACE: (),
    NavigationTargetKind.PROJECT: ((ApplicationDestinationId.PROJECTS, None),),
    NavigationTargetKind.SCENE_ENTITY: ((WorkspaceId.ROOM, "placement"),),
    NavigationTargetKind.SCENE_REVISION: ((WorkspaceId.ROOM, "history"),),
    NavigationTargetKind.SYSTEM_VARIANT: (
        (WorkspaceId.OPTIMIZATION, "comparison"),
    ),
    NavigationTargetKind.MEASUREMENT: (
        (WorkspaceId.MEASUREMENT, "assignment"),
    ),
    NavigationTargetKind.MEASUREMENT_CAMPAIGN: (
        (WorkspaceId.MEASUREMENT, "campaign"),
    ),
    NavigationTargetKind.PREDICTION_RESULT: (
        (WorkspaceId.MEASUREMENT, "comparison"),
    ),
    NavigationTargetKind.OPTIMIZATION_CANDIDATE: (
        (WorkspaceId.OPTIMIZATION, "candidates"),
    ),
    NavigationTargetKind.OPTIMIZATION_COMPARISON: (
        (WorkspaceId.OPTIMIZATION, "comparison"),
    ),
    NavigationTargetKind.COMMISSIONING_EVALUATION: (
        (WorkspaceId.OPTIMIZATION, "validation"),
    ),
    NavigationTargetKind.CAPTURE_DELIVERY: (
        (ApplicationDestinationId.INBOX, None),
        (WorkspaceId.MEASUREMENT, "import"),
    ),
    NavigationTargetKind.CAPTURE_INBOX_ITEM: (
        (ApplicationDestinationId.INBOX, None),
        (WorkspaceId.MEASUREMENT, "import"),
    ),
    NavigationTargetKind.EQUIPMENT_DEFINITION: (
        (ApplicationDestinationId.LIBRARY, None),
        (WorkspaceId.ROOM, "objects"),
    ),
    NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE: (
        (WorkspaceId.ROOM, "objects"),
    ),
    NavigationTargetKind.TREATMENT_DEFINITION: (
        (WorkspaceId.ROOM, "acoustics"),
    ),
    NavigationTargetKind.PROJECT_CHECKPOINT: (
        (ApplicationDestinationId.ACTIVITY, None),
        (WorkspaceId.ROOM, "history"),
    ),
    NavigationTargetKind.ACTIVITY_JOB: (
        (ApplicationDestinationId.ACTIVITY, None),
    ),
    NavigationTargetKind.HELP_TOPIC: (
        (ApplicationDestinationId.SUPPORT, None),
    ),
    NavigationTargetKind.OPERATING_PRESET: (),
    NavigationTargetKind.HEALTH_BASELINE: (),
    NavigationTargetKind.HEALTH_CHECK_PLAN: (),
    NavigationTargetKind.PROJECT_NOTE: (),
    NavigationTargetKind.CALIBRATION_PLAN: (
        # No dedicated calibration surface yet: land on the Measurement
        # workspace default rather than a section the mount cannot select.
        (WorkspaceId.MEASUREMENT, None),
    ),
    NavigationTargetKind.AV_SYNC_CONDITION: (
        (WorkspaceId.MEASUREMENT, "quality"),
    ),
}

#: User-facing Japanese name per target kind — surfaced in resolution
#: messages so the enum id never renders into a sentence. A kind that
#: arrives from a future/persisted link falls back to its raw id.
_NAVIGATION_KIND_LABELS: dict[NavigationTargetKind, str] = {
    NavigationTargetKind.WORKSPACE: "ワークスペース",
    NavigationTargetKind.PROJECT: "プロジェクト",
    NavigationTargetKind.SCENE_ENTITY: "シーンオブジェクト",
    NavigationTargetKind.SCENE_REVISION: "シーンリビジョン",
    NavigationTargetKind.SYSTEM_VARIANT: "システムバリアント",
    NavigationTargetKind.MEASUREMENT: "測定",
    NavigationTargetKind.MEASUREMENT_CAMPAIGN: "測定キャンペーン",
    NavigationTargetKind.PREDICTION_RESULT: "予測結果",
    NavigationTargetKind.OPTIMIZATION_CANDIDATE: "最適化候補",
    NavigationTargetKind.OPTIMIZATION_COMPARISON: "最適化比較",
    NavigationTargetKind.COMMISSIONING_EVALUATION: "コミッショニング評価",
    NavigationTargetKind.CAPTURE_DELIVERY: "Capture配送",
    NavigationTargetKind.CAPTURE_INBOX_ITEM: "取り込み項目",
    NavigationTargetKind.EQUIPMENT_DEFINITION: "機材定義",
    NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE: "設置済み機材",
    NavigationTargetKind.TREATMENT_DEFINITION: "処理材定義",
    NavigationTargetKind.PROJECT_CHECKPOINT: "プロジェクトチェックポイント",
    NavigationTargetKind.ACTIVITY_JOB: "アクティビティジョブ",
    NavigationTargetKind.HELP_TOPIC: "ヘルプ",
    NavigationTargetKind.OPERATING_PRESET: "運用プリセット",
    NavigationTargetKind.HEALTH_BASELINE: "健全性ベースライン",
    NavigationTargetKind.HEALTH_CHECK_PLAN: "健全性チェック計画",
    NavigationTargetKind.PROJECT_NOTE: "プロジェクトメモ",
    NavigationTargetKind.CALIBRATION_PLAN: "校正プラン",
    NavigationTargetKind.AV_SYNC_CONDITION: "AV同期条件",
}


def navigation_kind_label(kind: NavigationTargetKind) -> str:
    """Japanese display label for a target kind (raw id as last resort)."""
    return _NAVIGATION_KIND_LABELS.get(kind, kind.value)


_TARGET_UNSUPPORTED_REASON = (
    "この対象（{kind}）を表示できる画面がありません。"
    "ワークスペースの登録を確認してください。"
)


@dataclass(frozen=True, slots=True)
class NavigationResolution:
    """Outcome of resolving a NavigationTarget against registered surfaces."""

    target: NavigationTarget
    link: WorkspaceDeepLink | None
    status: str  # "focused" | "navigated" | "unsupported"
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "unsupported"


@dataclass(frozen=True, slots=True)
class NavigationContextEntry:
    """One recorded navigation: the typed target, resolved link and referrer."""

    target: NavigationTarget
    link: WorkspaceDeepLink
    referrer: str | None
    seq: int


class NavigationHistory:
    """Bounded application Back/Forward history, independent of Scene Undo.

    Entries are typed targets plus their resolved links, so going back replays
    the same authority context (project / revision / variant) — it never
    re-derives from newest state.
    """

    CAPACITY = 64

    def __init__(self) -> None:
        self._entries: list[NavigationContextEntry] = []
        self._cursor = -1
        self._seq = 0

    def record(
        self,
        target: NavigationTarget,
        link: WorkspaceDeepLink,
        *,
        referrer: str | None = None,
    ) -> NavigationContextEntry:
        del self._entries[self._cursor + 1 :]
        self._seq += 1
        entry = NavigationContextEntry(
            target=target,
            link=link,
            referrer=referrer if referrer is not None else target.referrer,
            seq=self._seq,
        )
        self._entries.append(entry)
        if len(self._entries) > self.CAPACITY:
            del self._entries[0]
        self._cursor = len(self._entries) - 1
        return entry

    @property
    def current(self) -> NavigationContextEntry | None:
        if 0 <= self._cursor < len(self._entries):
            return self._entries[self._cursor]
        return None

    @property
    def can_go_back(self) -> bool:
        return self._cursor > 0

    @property
    def can_go_forward(self) -> bool:
        return 0 <= self._cursor < len(self._entries) - 1

    def back(self) -> NavigationContextEntry | None:
        if self.can_go_back:
            self._cursor -= 1
        return self.current

    def forward(self) -> NavigationContextEntry | None:
        if self.can_go_forward:
            self._cursor += 1
        return self.current

    def entries(self) -> tuple[NavigationContextEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        """Drop every entry — establishes a new navigation epoch.

        Whole-data restore/reset replaces the data universe; every recorded
        target addresses authority that may not exist (or may mean something
        different) in the new generation and must never be replayed.
        """
        self._entries.clear()
        self._cursor = -1

    def drop_unscoped_project_entries(self) -> None:
        """Drop project-scope entries recorded without project identity.

        Called when the active project changes: a legacy target carrying no
        ``project_id`` could otherwise replay against the wrong project's
        namespace (colliding object ids). Entries pinned to a canonical
        project id survive — replaying them performs a guarded switch back.
        Application-scope entries are valid independently of the project.
        """

        retained = [
            entry
            for entry in self._entries
            if entry.target.scope is not NavigationScope.PROJECT
            or entry.target.project_id is not None
        ]
        if len(retained) == len(self._entries):
            return
        self._entries = retained
        self._cursor = min(self._cursor, len(self._entries) - 1)


class NavigationResolver:
    """Maps typed targets onto registered destinations — fail-closed."""

    def __init__(
        self,
        routes: Mapping[
            NavigationTargetKind, Sequence[tuple[DestinationId, str | None]]
        ] = TARGET_KIND_ROUTES,
    ) -> None:
        self._routes = routes

    def resolve(
        self,
        target: NavigationTarget,
        *,
        registered: set[DestinationId],
        capabilities: Mapping[DestinationId, frozenset[NavigationTargetKind]],
    ) -> NavigationResolution:
        """Pick the destination for ``target`` and build its deep link.

        The preferred surface is honored when it is registered; otherwise the
        kind's route chain is consulted in order. A kind no registered
        destination owns yields an explicit unsupported failure — never a
        name/newest substitution.
        """
        candidates: list[tuple[DestinationId, str | None]] = []
        if target.preferred_destination is not None:
            candidates.append(
                (target.preferred_destination, target.preferred_section)
            )
        candidates.extend(self._routes.get(target.kind, ()))

        for destination, default_section in candidates:
            if destination not in registered:
                continue
            link = WorkspaceDeepLink(
                workspace=destination,
                section=target.preferred_section or default_section,
                entity_id=target.primary_id,
                revision_id=target.revision_id,
                system_variant_id=target.system_variant_id,
                kind=target.kind,
                intent=target.intent,
            )
            focusable = target.kind in capabilities.get(destination, frozenset())
            return NavigationResolution(
                target=target,
                link=link,
                status="focused" if focusable else "navigated",
            )

        return NavigationResolution(
            target=target,
            link=None,
            status="unsupported",
            message=_TARGET_UNSUPPORTED_REASON.format(
                kind=navigation_kind_label(target.kind)
            ),
        )


def navigation_target_from_uri(uri: str) -> NavigationTarget:
    """Parse ``htdt://nav/v1/...`` and legacy ``htdt://workspace|app/...`` URIs."""

    parsed = urlparse(uri)
    if parsed.scheme != "htdt":
        raise ValueError(f"unsupported navigation URI scheme: {uri!r}")

    if parsed.netloc == "nav":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != _NAV_URI_VERSION:
            raise ValueError(f"unsupported navigation URI version/path: {uri!r}")
        kind = NavigationTargetKind(parts[1])
        params = dict(parse_qsl(parsed.query, keep_blank_values=False))
        ids = tuple(
            value for key, value in parse_qsl(parsed.query) if key == "id"
        )
        return NavigationTarget(
            kind=kind,
            object_ids=ids,
            scope=NavigationScope(params.get("scope", "project")),
            project_id=params.get("project"),
            revision_id=params.get("revision"),
            system_variant_id=params.get("variant"),
            preferred_destination=(
                normalize_destination_id(params["destination"])
                if params.get("destination")
                else None
            ),
            preferred_section=params.get("section"),
            intent=NavigationIntent(params.get("intent", "inspect")),
            referrer=params.get("referrer"),
        )

    if parsed.netloc in ("workspace", "app"):
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError(f"missing destination in navigation URI: {uri!r}")
        destination = (
            ApplicationDestinationId(parts[0])
            if parsed.netloc == "app"
            else WorkspaceId(parts[0])
        )
        params = dict(parse_qsl(parsed.query))
        entity = params.get("entity")
        if entity:
            return NavigationTarget(
                kind=NavigationTargetKind.SCENE_ENTITY,
                object_ids=(entity,),
                preferred_destination=destination,
                preferred_section=parts[1] if len(parts) > 1 else None,
                revision_id=params.get("revision"),
                system_variant_id=params.get("variant"),
            )
        return NavigationTarget(
            kind=NavigationTargetKind.WORKSPACE,
            object_ids=(destination.value,),
            preferred_destination=destination,
            preferred_section=parts[1] if len(parts) > 1 else None,
        )

    raise ValueError(f"unsupported navigation URI host: {uri!r}")


__all__ = [
    "NavigationContextEntry",
    "NavigationHistory",
    "NavigationIntent",
    "NavigationResolution",
    "NavigationResolver",
    "NavigationTarget",
    "NavigationTargetKind",
    "TARGET_KIND_ROUTES",
    "navigation_target_from_uri",
]
