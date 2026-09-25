from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias
from urllib.parse import parse_qsl, quote, urlparse


class NavigationScope(StrEnum):
    """Which IA domain owns a destination (UX160 IA v2)."""

    PROJECT = "project"
    APPLICATION = "application"


class WorkspaceId(StrEnum):
    """Stable user-facing project workflow destinations."""

    OVERVIEW = "overview"
    ROOM = "room"
    MEASUREMENT = "measurement"
    OPTIMIZATION = "optimization"


class ApplicationDestinationId(StrEnum):
    """Application-scope destinations that exist outside any one project."""

    PROJECTS = "projects"
    INBOX = "inbox"
    ACTIVITY = "activity"
    LIBRARY = "library"
    SUPPORT = "support"


DestinationId: TypeAlias = WorkspaceId | ApplicationDestinationId

#: The four compact project workspaces that every composition must register.
#: Additional registered destinations are allowed — the shell no longer
#: assumes every user-facing destination is one of exactly these four.
PROJECT_WORKSPACE_IDS: frozenset[WorkspaceId] = frozenset(WorkspaceId)


def destination_scope(destination: DestinationId | str) -> NavigationScope:
    if isinstance(destination, ApplicationDestinationId):
        return NavigationScope.APPLICATION
    if isinstance(destination, WorkspaceId):
        return NavigationScope.PROJECT
    value = str(destination)
    try:
        ApplicationDestinationId(value)
        return NavigationScope.APPLICATION
    except ValueError:
        pass
    WorkspaceId(value)  # raises for unknown destinations
    return NavigationScope.PROJECT


def normalize_destination_id(value: DestinationId | str) -> DestinationId:
    if isinstance(value, (WorkspaceId, ApplicationDestinationId)):
        return value
    text = str(value)
    for enum in (WorkspaceId, ApplicationDestinationId):
        try:
            return enum(text)
        except ValueError:
            continue
    raise ValueError(f"unknown navigation destination: {text!r}")


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    context_id: str
    label: str


@dataclass(frozen=True, slots=True)
class WorkspaceDeepLink:
    """Transport-only navigation target shared by shell, commands and Overview.

    ``revision_id``/``system_variant_id`` carry explicit authority context so a
    link resolves to the same evidence after later saves; ``kind``/``intent``
    describe the typed target the link was resolved from (when known).
    """

    workspace: DestinationId
    section: str | None = None
    entity_id: str | None = None
    revision_id: str | None = None
    system_variant_id: str | None = None
    kind: str | None = None
    intent: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, (WorkspaceId, ApplicationDestinationId)):
            object.__setattr__(self, "workspace", normalize_destination_id(self.workspace))

    @property
    def scope(self) -> NavigationScope:
        return destination_scope(self.workspace)

    @property
    def subsection(self) -> str | None:
        """Overview/readiness spelling for the canonical command-layer section."""
        return self.section

    def as_uri(self) -> str:
        if self.scope == NavigationScope.APPLICATION:
            base = f"htdt://app/{self.workspace.value}"
        else:
            base = f"htdt://workspace/{self.workspace.value}"
        if self.section is not None:
            base += f"/{quote(self.section, safe='')}"
        params: list[tuple[str, str]] = []
        if self.entity_id is not None:
            params.append(("entity", self.entity_id))
        if self.revision_id is not None:
            params.append(("revision", self.revision_id))
        if self.system_variant_id is not None:
            params.append(("variant", self.system_variant_id))
        if self.kind is not None:
            params.append(("kind", self.kind))
        if self.intent is not None:
            params.append(("intent", self.intent))
        if params:
            base += "?" + "&".join(
                f"{key}={quote(value, safe='')}" for key, value in params
            )
        return base

    @classmethod
    def from_uri(cls, uri: str) -> "WorkspaceDeepLink":
        """Parse ``htdt://workspace/...`` and ``htdt://app/...`` deep links."""

        parsed = urlparse(uri)
        if parsed.scheme != "htdt" or parsed.netloc not in ("workspace", "app"):
            raise ValueError(f"unsupported deep link URI: {uri!r}")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError(f"missing destination in deep link URI: {uri!r}")
        destination: DestinationId = (
            ApplicationDestinationId(parts[0])
            if parsed.netloc == "app"
            else WorkspaceId(parts[0])
        )
        params = dict(parse_qsl(parsed.query))
        return cls(
            workspace=destination,
            section=parts[1] if len(parts) > 1 else None,
            entity_id=params.get("entity"),
            revision_id=params.get("revision"),
            system_variant_id=params.get("variant"),
            kind=params.get("kind"),
            intent=params.get("intent"),
        )


CANONICAL_WORKSPACE_LABELS: dict[WorkspaceId, str] = {
    WorkspaceId.OVERVIEW: "概要",
    WorkspaceId.ROOM: "部屋",
    WorkspaceId.MEASUREMENT: "測定",
    WorkspaceId.OPTIMIZATION: "最適化",
}


APPLICATION_DESTINATION_LABELS: dict[ApplicationDestinationId, str] = {
    ApplicationDestinationId.PROJECTS: "プロジェクト",
    ApplicationDestinationId.INBOX: "取り込み",
    ApplicationDestinationId.ACTIVITY: "アクティビティ",
    ApplicationDestinationId.LIBRARY: "ライブラリ",
    ApplicationDestinationId.SUPPORT: "サポート",
}


CANONICAL_WORKSPACE_CONTEXTS: dict[WorkspaceId, tuple[WorkspaceContext, ...]] = {
    WorkspaceId.OVERVIEW: (),
    WorkspaceId.ROOM: (
        WorkspaceContext("geometry", "形状"),
        WorkspaceContext("objects", "物体"),
        WorkspaceContext("placement", "スピーカー・座席"),
        WorkspaceContext("acoustics", "音響"),
        WorkspaceContext("history", "履歴"),
    ),
    WorkspaceId.MEASUREMENT: (
        WorkspaceContext("import", "読み込み"),
        WorkspaceContext("assignment", "割り当て"),
        WorkspaceContext("campaign", "キャンペーン"),
        WorkspaceContext("quality", "品質"),
        WorkspaceContext("comparison", "比較"),
        WorkspaceContext("calibration", "キャリブレーション"),
    ),
    WorkspaceId.OPTIMIZATION: (
        WorkspaceContext("setup", "探索設定"),
        WorkspaceContext("candidates", "候補"),
        WorkspaceContext("comparison", "比較"),
        WorkspaceContext("interventions", "介入計画"),
        WorkspaceContext("robustness", "ばらつき耐性"),
        WorkspaceContext("validation", "測定・検証"),
    ),
}

WORKSPACE_CONTEXT_ALIASES: dict[WorkspaceId, dict[str, str]] = {
    WorkspaceId.ROOM: {
        "system-proposal": "placement",
    },
    WorkspaceId.OPTIMIZATION: {
        "objectives": "comparison",
        "measurement-plan": "validation",
        "topology-comparison": "comparison",
        "variant-robustness": "robustness",
        "variant-measurement": "validation",
        "intervention-planner": "interventions",
    },
}



def normalize_workspace_id(value: WorkspaceId | str) -> WorkspaceId:
    return value if isinstance(value, WorkspaceId) else WorkspaceId(value)


def normalize_workspace_context(workspace: WorkspaceId | str, context_id: str) -> str:
    workspace_id = normalize_workspace_id(workspace)
    return WORKSPACE_CONTEXT_ALIASES.get(workspace_id, {}).get(context_id, context_id)


__all__ = [
    "APPLICATION_DESTINATION_LABELS",
    "ApplicationDestinationId",
    "CANONICAL_WORKSPACE_CONTEXTS",
    "CANONICAL_WORKSPACE_LABELS",
    "DestinationId",
    "NavigationScope",
    "PROJECT_WORKSPACE_IDS",
    "WORKSPACE_CONTEXT_ALIASES",
    "WorkspaceContext",
    "WorkspaceDeepLink",
    "WorkspaceId",
    "destination_scope",
    "normalize_destination_id",
    "normalize_workspace_context",
    "normalize_workspace_id",
]
