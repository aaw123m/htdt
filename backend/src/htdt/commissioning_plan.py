"""First-run commissioning plan — resumable orchestration metadata (#588).

The plan is intentionally *not* a source of evidence: it never manufactures
measurement data, validation records, or lifecycle transitions. Persisted
state is limited to the user's declared intent, explicit skip marks, and a
stage cursor; every requirement's real status is re-derived from the
authoritative workspace reads (OverviewReadinessService / repositories)
each time the plan is opened, so resuming never replays stale checkmarks.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

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
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
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


#: Declarative requirement list. Each entry is re-derived from live reads;
#: ``skipped_requirements`` on the plan is the only durable per-item state.
_REQUIREMENTS: tuple[tuple[str, CommissioningStage, str], ...] = (
    ('room.geometry', CommissioningStage.ROOM, '部屋形状を決定する'),
    ('system.speakers', CommissioningStage.SYSTEM, 'スピーカーを配置する'),
    ('system.listening', CommissioningStage.SYSTEM, 'リスニングポイントを設定する'),
    ('measurement.data', CommissioningStage.MEASUREMENT, '測定データを登録する'),
    ('readiness.summary', CommissioningStage.READINESS, '準備状況を確認する'),
)


def _requirement_status(
    requirement_id: str,
    view,
    revision,
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
            return 'pending', notice.message, link
        return 'satisfied', '測定データは登録済みです', None
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
        view = self._overview.read(plan.document_id)
        revision = self._repository.latest(plan.document_id)
        results: list[CommissioningRequirement] = []
        for requirement_id, stage, title in _REQUIREMENTS:
            if requirement_id in plan.skipped_requirements:
                results.append(
                    CommissioningRequirement(
                        requirement_id, stage, title,
                        status='skipped', reason='スキップ済み', link=None,
                    )
                )
                continue
            status, reason, link = _requirement_status(
                requirement_id, view, revision
            )
            results.append(
                CommissioningRequirement(
                    requirement_id, stage, title,
                    status=status, reason=reason, link=link,
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
    'new_plan',
]
