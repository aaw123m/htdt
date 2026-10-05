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
    # Issue #534: client presentation/review surface — read-only replay
    # of exact scene authority; never an engineering edit surface.
    PRESENTATION = "presentation"
    # Issue #541: guided video commissioning journey — import → diagnose →
    # act → verify for a display the user is calibrating.
    VIDEO = "video"


class ApplicationDestinationId(StrEnum):
    """Application-scope destinations that exist outside any one project."""

    PROJECTS = "projects"
    INBOX = "inbox"
    ACTIVITY = "activity"
    LIBRARY = "library"
    SUPPORT = "support"
    ACCEPTANCE = "acceptance"


DestinationId: TypeAlias = WorkspaceId | ApplicationDestinationId

#: The compact project workspaces that every composition must register.
#: Additional registered destinations are allowed — the shell no longer
#: assumes every user-facing destination is one of exactly these.
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
    #: One-line Japanese explanation of what the context is for; rendered as
    #: the context tab's tooltip. Empty means no tooltip.
    hint: str = ""


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
    WorkspaceId.PRESENTATION: "プレゼン",
    WorkspaceId.VIDEO: "映像調整",
}


#: One-line Japanese purpose of each workspace/destination; rendered as the
#: rail button's tooltip so navigation explains itself.
CANONICAL_WORKSPACE_HINTS: dict[WorkspaceId, str] = {
    WorkspaceId.OVERVIEW: "プロジェクト全体の進行状況と、次にやるべきことを確認します",
    WorkspaceId.ROOM: "部屋の形状・配置・壁材を設定し、音響予測を行います",
    WorkspaceId.MEASUREMENT: "REW等の測定の計画・取り込み・品質確認を行います",
    WorkspaceId.OPTIMIZATION: "スピーカー配置や設定の探索候補を生成し、比較・検証します",
    WorkspaceId.PRESENTATION: "クライアント向けのプレゼン・レビュー。確定した設計権威のビューポイント再生、A/B比較、オフライン共有パッケージを扱います",
    WorkspaceId.VIDEO: "TV/プロジェクターの色調整 — 測定の取り込み・診断・対策・再測定を手順どおりに進めます",
}


APPLICATION_DESTINATION_HINTS: dict[ApplicationDestinationId, str] = {
    ApplicationDestinationId.PROJECTS: "保存済みプロジェクトの一覧・切替・管理を行います",
    ApplicationDestinationId.INBOX: "外部から受け取った測定・ファイルの割り当て待ち一覧です",
    ApplicationDestinationId.ACTIVITY: "アプリ内で行われた操作・処理の記録を確認します",
    ApplicationDestinationId.LIBRARY: "機材・素材などの参照データ（マスタ情報）を管理します",
    ApplicationDestinationId.SUPPORT: "診断情報の出力や、権威グラフなどの内部確認を行います",
    ApplicationDestinationId.ACCEPTANCE: "実機での受入ゲートを手順どおりに実行し、証跡を記録します",
}


APPLICATION_DESTINATION_LABELS: dict[ApplicationDestinationId, str] = {
    ApplicationDestinationId.PROJECTS: "プロジェクト",
    ApplicationDestinationId.INBOX: "取り込み",
    ApplicationDestinationId.ACTIVITY: "アクティビティ",
    ApplicationDestinationId.LIBRARY: "ライブラリ",
    ApplicationDestinationId.SUPPORT: "サポート",
    ApplicationDestinationId.ACCEPTANCE: "受入検証",
}


CANONICAL_WORKSPACE_CONTEXTS: dict[WorkspaceId, tuple[WorkspaceContext, ...]] = {
    WorkspaceId.OVERVIEW: (),
    WorkspaceId.ROOM: (
        WorkspaceContext("geometry", "形状", "部屋の外形を描き、天井高・壁・開口を設定します"),
        WorkspaceContext("objects", "物体", "部屋に置く物体（スピーカー・座席・家具など）を追加・編集します"),
        WorkspaceContext("placement", "スピーカー・座席", "スピーカーと座席の位置・向きを調整し、配置制約や提案を扱います"),
        WorkspaceContext("acoustics", "音響", "壁材・吸音処理を設定し、音響予測を実行します"),
        WorkspaceContext("history", "履歴", "保存した版の履歴を確認し、差分比較や復元を行います"),
    ),
    WorkspaceId.MEASUREMENT: (
        WorkspaceContext("import", "読み込み", "REWなどの測定ファイルを取り込みます"),
        WorkspaceContext("assignment", "割り当て", "取り込んだ測定を座席や計画の測定点に割り当てます"),
        WorkspaceContext("campaign", "キャンペーン", "どこを・何を測るかの測定計画を立てます"),
        WorkspaceContext("quality", "品質", "測定結果の品質（SN比・残差など）を確認します"),
        WorkspaceContext("comparison", "比較", "複数の測定や候補を並べて比較します"),
        # Instrument onboarding/checklist page; reached last in the bar since
        # it guides the first capture rather than describing a workflow stage.
        WorkspaceContext("calibration", "機器の準備", "測定機器の準備手順と確認項目を案内します"),
    ),
    WorkspaceId.OPTIMIZATION: (
        WorkspaceContext("setup", "探索設定", "どのパラメータをどの範囲で動かすか（探索軸）を設定します"),
        WorkspaceContext("candidates", "候補", "生成された配置・設定の候補一覧を確認します"),
        WorkspaceContext("comparison", "比較", "候補どうしの性能を並べて比較します"),
        WorkspaceContext("interventions", "介入計画", "物理的な変更案（スピーカー移動・吸音材追加など）を計画します"),
        WorkspaceContext("robustness", "ばらつき耐性", "候補が実際のばらつき（測定誤差・個体差）に耐えるかを評価します"),
        WorkspaceContext("validation", "測定・検証", "選んだ候補を実測で検証する計画を立てます"),
    ),
    WorkspaceId.PRESENTATION: (
        WorkspaceContext("session", "セッション", "プレゼンセッションとビューポイントを組み立て・再生します"),
        WorkspaceContext("compare", "A/B比較", "2つの権威を同期カメラで並べてレビューします"),
        WorkspaceContext("decisions", "決定・提案", "レビュー中の選択・提案・コメントを権威に紐付けて記録します"),
        WorkspaceContext("export", "出力", "オフラインのレビュー／提案パッケージを生成します"),
    ),
    WorkspaceId.VIDEO: (
        WorkspaceContext("session", "セッション", "対象の画面・測定条件・ターゲットを束縛し、準備状況を評価します"),
        WorkspaceContext("import", "読み込み", "測定ファイル（HTDT JSON / HCFR CSV）を証拠セットとして取り込みます"),
        WorkspaceContext("diagnose", "診断", "測定セットをターゲットと照合し、項目ごとの判定と説明を確認します"),
        WorkspaceContext("actions", "対策", "診断から導かれる調整案を確認し、実施した調整を記録します"),
        WorkspaceContext("verify", "再測定・比較", "調整前後の測定を比較し、改善・悪化を確認します"),
        WorkspaceContext("report", "結果", "セッションの証拠チェーンと状態を確認・完了します"),
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


def destination_label(destination: DestinationId) -> str:
    """Japanese display label for a shell destination (raw id as fallback)."""
    if isinstance(destination, WorkspaceId):
        return CANONICAL_WORKSPACE_LABELS.get(destination, destination.value)
    return APPLICATION_DESTINATION_LABELS.get(destination, destination.value)


def workspace_context_label(
    workspace: WorkspaceId | str, context_id: str
) -> str:
    """Japanese display label for a workspace context id.

    A stale or foreign deep link can name a context the target workspace
    does not expose — fall back across every workspace's contexts so the
    message still names it in Japanese, then to the raw id.
    """
    canonical = normalize_workspace_context(workspace, context_id)
    for contexts in CANONICAL_WORKSPACE_CONTEXTS.values():
        for context in contexts:
            if context.context_id == canonical:
                return context.label
    return context_id


def deeplink_hint(link: WorkspaceDeepLink) -> str:
    """Japanese explanation of where a deep link leads, for action tooltips.

    Section-scoped links describe the target context (then the workspace);
    unscoped links describe the destination itself. Unknown targets fall
    back to a generic "opens X" phrase so the tooltip is never empty.
    """
    workspace = link.workspace
    section = link.section
    if isinstance(workspace, WorkspaceId):
        workspace_label = CANONICAL_WORKSPACE_LABELS.get(workspace, workspace.value)
        if section is not None:
            canonical = normalize_workspace_context(workspace, section)
            for context in CANONICAL_WORKSPACE_CONTEXTS.get(workspace, ()):
                if context.context_id == canonical:
                    if context.hint:
                        return f"「{workspace_label}」の「{context.label}」ページを開きます。{context.hint}"
                    return f"「{workspace_label}」の「{context.label}」ページを開きます"
        hint = CANONICAL_WORKSPACE_HINTS.get(workspace, "")
        return f"「{workspace_label}」を開きます。{hint}" if hint else f"「{workspace_label}」を開きます"
    label = APPLICATION_DESTINATION_LABELS.get(workspace, workspace.value)
    hint = APPLICATION_DESTINATION_HINTS.get(workspace, "")
    return f"「{label}」を開きます。{hint}" if hint else f"「{label}」を開きます"


__all__ = [
    "APPLICATION_DESTINATION_HINTS",
    "APPLICATION_DESTINATION_LABELS",
    "ApplicationDestinationId",
    "CANONICAL_WORKSPACE_CONTEXTS",
    "CANONICAL_WORKSPACE_HINTS",
    "CANONICAL_WORKSPACE_LABELS",
    "DestinationId",
    "NavigationScope",
    "PROJECT_WORKSPACE_IDS",
    "WORKSPACE_CONTEXT_ALIASES",
    "WorkspaceContext",
    "WorkspaceDeepLink",
    "deeplink_hint",
    "WorkspaceId",
    "destination_label",
    "destination_scope",
    "normalize_destination_id",
    "normalize_workspace_context",
    "normalize_workspace_id",
    "workspace_context_label",
]
