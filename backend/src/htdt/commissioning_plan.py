"""First-run commissioning plan — resumable orchestration metadata (#588).

The plan is intentionally *not* a source of evidence: it never manufactures
measurement data, validation records, or lifecycle transitions. Persisted
state is limited to the user's declared intent, explicit skip marks, and a
stage cursor; every requirement's real status is re-derived from the
authoritative workspace reads (OverviewReadinessService / repositories)
each time the plan is opened, so resuming never replays stale checkmarks.

The requirement list itself is *derived* from the declared intent (#899):
audio-only suppresses video-scope work, REW availability changes
measurement guidance, a declared speaker count produces a current-vs-
planned topology delta, the hybrid-prediction opt-in adds its measured-
evidence prerequisite, and performance goals decide whether measurement
is required or optional — no wizard answer is passive metadata.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from .cad_scene import is_unassigned_speaker_role
from .overview_readiness import OverviewReadinessService
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


class CommissioningStage(StrEnum):
    INTENT = 'intent'
    ROOM = 'room'
    SYSTEM = 'system'
    MEASUREMENT = 'measurement'
    READINESS = 'readiness'


class CommissioningPlanError(Exception):
    pass


RequirementStatus = Literal['pending', 'satisfied', 'skipped']

#: How mandatory a requirement is for *this* project's declared path —
#: derived from intent (#899), never a global score.
RequirementLevel = Literal['required', 'recommended', 'optional']


@dataclass(frozen=True, slots=True)
class CommissioningIntent:
    """What the user declared at first run — immutable context for stages."""

    is_new_project: bool
    has_existing_room: bool
    audio_only: bool
    rew_available: bool
    wants_hybrid_prediction: bool
    planned_speaker_count: int
    goals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommissioningRequirement:
    requirement_id: str
    stage: CommissioningStage
    title: str
    status: RequirementStatus
    reason: str
    #: Why this requirement exists on this project's path (#899).
    level: RequirementLevel = 'required'
    link: WorkspaceDeepLink | None = None


@dataclass(frozen=True, slots=True)
class CommissioningPlan:
    """Persisted plan state — explicit skips + stage cursor, never evidence."""

    plan_id: str
    schema_version: int
    document_id: str
    name: str
    intent: CommissioningIntent
    stage: CommissioningStage
    skipped_requirements: frozenset[str] = field(default_factory=frozenset)
    finished: bool = False
    created_at_utc: str = ''
    updated_at_utc: str = ''

    SCHEMA_VERSION = 1

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def new_plan(
    document_id: str,
    name: str,
    intent: CommissioningIntent,
) -> CommissioningPlan:
    now = CommissioningPlan._now()
    return CommissioningPlan(
        plan_id=f'plan-{uuid.uuid4().hex[:12]}',
        schema_version=CommissioningPlan.SCHEMA_VERSION,
        document_id=document_id,
        name=name,
        intent=intent,
        stage=CommissioningStage.ROOM,
        created_at_utc=now,
        updated_at_utc=now,
    )


class CommissioningPlanRepository:
    """JSON-file store under the app data directory — orchestration metadata."""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / 'commissioning-plans.json'

    def _load(self) -> dict:
        if not self._path.exists():
            return {'plans': {}}
        try:
            return json.loads(self._path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return {'plans': {}}

    def _store(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8'
        )

    def save(self, plan: CommissioningPlan) -> CommissioningPlan:
        plan = replace(plan, updated_at_utc=CommissioningPlan._now())
        data = self._load()
        plans = data.setdefault('plans', {})
        serialized = asdict(plan)
        serialized['stage'] = plan.stage.value
        serialized['skipped_requirements'] = sorted(plan.skipped_requirements)
        plans[plan.document_id] = serialized
        data['active_document_id'] = plan.document_id
        self._store(data)
        return plan

    def get(self, document_id: str) -> CommissioningPlan | None:
        raw = self._load().get('plans', {}).get(document_id)
        if raw is None:
            return None
        try:
            intent_raw = dict(raw['intent'])
            intent_raw['goals'] = tuple(intent_raw.get('goals', ()))
            return CommissioningPlan(
                plan_id=raw['plan_id'],
                schema_version=int(raw['schema_version']),
                document_id=raw['document_id'],
                name=raw['name'],
                intent=CommissioningIntent(**intent_raw),
                stage=CommissioningStage(raw['stage']),
                skipped_requirements=frozenset(
                    raw.get('skipped_requirements', ())
                ),
                finished=bool(raw.get('finished', False)),
                created_at_utc=raw.get('created_at_utc', ''),
                updated_at_utc=raw.get('updated_at_utc', ''),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CommissioningPlanError(
                f'commissioning plan for {document_id!r} is unreadable: {exc}'
            ) from exc

    def latest(self) -> CommissioningPlan | None:
        data = self._load()
        plans = data.get('plans', {})
        active = data.get('active_document_id')
        ordered = sorted(
            plans.values(), key=lambda raw: raw.get('updated_at_utc', '')
        )
        for raw in reversed(ordered):
            if raw.get('document_id') == active or active is None:
                plan = self.get(raw['document_id'])
                if plan is not None and not plan.finished:
                    return plan
        return None

    def list_plans(self) -> tuple[CommissioningPlan, ...]:
        plans = []
        for raw in self._load().get('plans', {}).values():
            try:
                plans.append(self.get(raw['document_id']))
            except CommissioningPlanError:
                continue
        return tuple(p for p in plans if p is not None)


def _derive_requirements(
    intent: CommissioningIntent,
) -> tuple[tuple[str, CommissioningStage, RequirementLevel, str], ...]:
    """Requirement graph derived from declared intent (#899).

    Every wizard answer has a documented effect: existing-room intent
    switches the room requirement to acquisition/import guidance, the
    planned speaker count turns the speaker requirement into a
    current-vs-planned delta, performance goals and the hybrid-prediction
    opt-in make measured data required, and an audio-only project never
    receives video-scope tasks. A goal-less document/inventory project can
    reach a useful state without being told measurement is mandatory.
    """
    measurement_level: RequirementLevel = 'required'
    if not intent.goals and not intent.wants_hybrid_prediction:
        measurement_level = (
            'recommended' if intent.rew_available else 'optional'
        )
    listening_level: RequirementLevel = (
        'required'
        if (
            intent.goals
            or intent.rew_available
            or intent.wants_hybrid_prediction
        )
        else 'optional'
    )
    requirements: list[tuple[str, CommissioningStage, RequirementLevel, str]] = [
        (
            'room.geometry',
            CommissioningStage.ROOM,
            'required',
            (
                '部屋の既存データを取り込む・確認する'
                if intent.has_existing_room
                else '部屋形状を決定する'
            ),
        ),
        (
            'system.speakers',
            CommissioningStage.SYSTEM,
            'required',
            (
                f'スピーカーを配置する（計画 {intent.planned_speaker_count} 台）'
                if intent.planned_speaker_count > 0
                else 'スピーカーを配置する'
            ),
        ),
        (
            'system.listening',
            CommissioningStage.SYSTEM,
            listening_level,
            'リスニングポイントを設定する',
        ),
        (
            'measurement.data',
            CommissioningStage.MEASUREMENT,
            measurement_level,
            '測定データを登録する',
        ),
    ]
    if intent.wants_hybrid_prediction:
        # The checkbox has a real downstream effect: hybrid prediction is
        # only meaningful with measured evidence, so the plan carries that
        # prerequisite explicitly instead of an inert preference (#899).
        requirements.append((
            'prediction.hybrid_evidence',
            CommissioningStage.MEASUREMENT,
            'required',
            'ハイブリッド予測用の測定エビデンスを揃える',
        ))
    requirements.append((
        'readiness.summary',
        CommissioningStage.READINESS,
        'required',
        '準備状況を確認する',
    ))
    return tuple(requirements)


def _requirement_status(
    requirement_id: str,
    view,
    revision,
    intent: CommissioningIntent,
    level: RequirementLevel = 'required',
) -> tuple[RequirementStatus, str, WorkspaceDeepLink | None]:
    """Map an Overview readiness view onto a requirement verdict."""
    blockers = {notice.code: notice for notice in view.blockers}
    warnings = {notice.code: notice for notice in view.warnings}
    entities = () if revision is None else revision.document.entities

    if requirement_id == 'room.geometry':
        for code in ('room.missing', 'room.geometry_incomplete'):
            if code in blockers:
                notice = blockers[code]
                link = notice.action.target if notice.action else None
                return 'pending', notice.message, link
        return 'satisfied', '部屋形状は定義済みです', None
    if requirement_id == 'system.speakers':
        speaker_codes = (
            'speaker.missing',
            'speaker.role_missing',
            'speaker.role_duplicate',
        )
        for code in speaker_codes:
            if code in blockers:
                notice = blockers[code]
                link = notice.action.target if notice.action else None
                return 'pending', notice.message, link
        # Declared topology intent makes the step meaningful (#899-D): a
        # single valid speaker no longer satisfies a plan that asked for N.
        planned = intent.planned_speaker_count
        defined = sum(
            1
            for entity in entities
            if str(entity.kind) == 'speaker'
            and not is_unassigned_speaker_role(entity.speaker_role)
        )
        if planned > 0 and defined < planned:
            return (
                'pending',
                f'計画 {planned} 台のうち {defined} 台がロール定義済みです',
                WorkspaceDeepLink(WorkspaceId.ROOM, 'placement'),
            )
        return 'satisfied', 'スピーカー構成は定義済みです', None
    if requirement_id == 'system.listening':
        seat = any(entity.kind == 'seat' for entity in entities)
        point = any(
            entity.kind == 'measurement_point' for entity in entities
        )
        if seat or point:
            return 'satisfied', 'リスニング位置は定義済みです', None
        return (
            'pending',
            '座席または測定ポイントが未登録です',
            WorkspaceDeepLink(WorkspaceId.ROOM, 'placement'),
        )
    if requirement_id == 'measurement.data':
        if 'measurement.missing' in warnings:
            notice = warnings['measurement.missing']
            link = notice.action.target if notice.action else None
            # REW availability changes the guidance, never the verdict —
            # wizard answers can steer the route but cannot fabricate
            # measurement readiness (#899-B).
            if intent.rew_available:
                return (
                    'pending',
                    f'{notice.message} REW などの実測データをインポートしてください。',
                    link,
                )
            return (
                'pending',
                f'{notice.message} REW・HTDT-Capture・既存ファイルなどの'
                '取得経路を選んでください。',
                link,
            )
        if level == 'optional':
            # An optional measurement with no 'missing' warning is a no-op
            # state — the reason must not claim data was registered (#899).
            return 'satisfied', '測定はこのプロジェクトでは任意です', None
        return 'satisfied', '測定データは登録済みです', None
    if requirement_id == 'prediction.hybrid_evidence':
        if 'measurement.missing' in warnings:
            notice = warnings['measurement.missing']
            link = notice.action.target if notice.action else None
            return (
                'pending',
                'ハイブリッド予測には測定エビデンスが必要です。'
                'まず測定データを登録してください。',
                link or WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'import'),
            )
        return 'satisfied', 'ハイブリッド予測に必要な測定エビデンスは揃っています', None
    if requirement_id == 'readiness.summary':
        if view.blockers:
            return (
                'pending',
                '未解決のブロッカーがあります',
                WorkspaceDeepLink(WorkspaceId.OVERVIEW),
            )
        return 'satisfied', '準備状況を確認しました', None
    return 'pending', '', None


class CommissioningService:
    """Derives live requirement status for a persisted plan (#588)."""

    def __init__(
        self,
        repository,
        overview_service: OverviewReadinessService,
    ) -> None:
        self._repository = repository
        self._overview = overview_service

    def requirements(self, plan: CommissioningPlan) -> tuple[CommissioningRequirement, ...]:
        """Intent-derived requirements against live authority reads (#899).

        The set, classification and guidance all come from ``plan.intent``;
        manual skips stay explicit and never mean "satisfied".
        """
        view = self._overview.read(plan.document_id)
        revision = self._repository.latest(plan.document_id)
        results: list[CommissioningRequirement] = []
        for requirement_id, stage, level, title in _derive_requirements(plan.intent):
            if requirement_id in plan.skipped_requirements:
                results.append(
                    CommissioningRequirement(
                        requirement_id, stage, title,
                        status='skipped', reason='スキップ済み',
                        level=level, link=None,
                    )
                )
                continue
            status, reason, link = _requirement_status(
                requirement_id, view, revision, plan.intent, level
            )
            results.append(
                CommissioningRequirement(
                    requirement_id, stage, title,
                    status=status, reason=reason,
                    level=level, link=link,
                )
            )
        return tuple(results)


__all__ = [
    'CommissioningIntent',
    'CommissioningPlan',
    'CommissioningPlanError',
    'CommissioningPlanRepository',
    'CommissioningRequirement',
    'CommissioningService',
    'CommissioningStage',
    'RequirementLevel',
    'new_plan',
]
