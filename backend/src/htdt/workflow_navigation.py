from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote


class WorkspaceId(StrEnum):
    """Stable user-facing workflow destinations."""

    OVERVIEW = "overview"
    ROOM = "room"
    MEASUREMENT = "measurement"
    OPTIMIZATION = "optimization"


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    context_id: str
    label: str


@dataclass(frozen=True, slots=True)
class WorkspaceDeepLink:
    """Transport-only navigation target shared by shell, commands and Overview."""

    workspace: WorkspaceId
    section: str | None = None
    entity_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, WorkspaceId):
            object.__setattr__(self, "workspace", WorkspaceId(self.workspace))

    @property
    def subsection(self) -> str | None:
        """Overview/readiness spelling for the canonical command-layer section."""
        return self.section

    def as_uri(self) -> str:
        base = f"htdt://workspace/{self.workspace.value}"
        if self.section is not None:
            base += f"/{quote(self.section, safe='')}"
        if self.entity_id is not None:
            base += f"?entity={quote(self.entity_id, safe='')}"
        return base


CANONICAL_WORKSPACE_LABELS: dict[WorkspaceId, str] = {
    WorkspaceId.OVERVIEW: "概要",
    WorkspaceId.ROOM: "部屋",
    WorkspaceId.MEASUREMENT: "測定",
    WorkspaceId.OPTIMIZATION: "最適化",
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
    ),
    WorkspaceId.OPTIMIZATION: (
        WorkspaceContext("setup", "探索設定"),
        WorkspaceContext("candidates", "候補"),
        WorkspaceContext("comparison", "比較"),
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
    },
}



def normalize_workspace_id(value: WorkspaceId | str) -> WorkspaceId:
    return value if isinstance(value, WorkspaceId) else WorkspaceId(value)


def normalize_workspace_context(workspace: WorkspaceId | str, context_id: str) -> str:
    workspace_id = normalize_workspace_id(workspace)
    return WORKSPACE_CONTEXT_ALIASES.get(workspace_id, {}).get(context_id, context_id)


__all__ = [
    "CANONICAL_WORKSPACE_CONTEXTS",
    "CANONICAL_WORKSPACE_LABELS",
    "WORKSPACE_CONTEXT_ALIASES",
    "WorkspaceContext",
    "WorkspaceDeepLink",
    "WorkspaceId",
    "normalize_workspace_context",
    "normalize_workspace_id",
]
