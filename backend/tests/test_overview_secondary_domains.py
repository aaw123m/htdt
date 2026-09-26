"""IA v2 project-secondary domain exposure (issue #887).

Decisions, installation, commissioning and operating health each keep
their own authority. The Overview renders one status card per domain
under a dedicated secondary section so the four primary workspaces
stay compact (#649 tier C) while the domains stay discoverable.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from htdt.cad_repository import SceneRevision
from htdt.overview_readiness import OverviewReadinessService
from htdt.workflow_navigation import WorkspaceId


@dataclass
class _SceneSource:
    revision: SceneRevision | None

    def current_head(self, document_id: str) -> SceneRevision | None:
        if self.revision is None or self.revision.document_id != document_id:
            return None
        return self.revision


@dataclass
class _MeasurementSource:
    def list_measurements(self, document_id: str) -> tuple:
        return ()

    def dataset_for_measurement(self, measurement_id: str):
        return None


@dataclass
class _PredictionSource:
    def list_results(self, document_id: str) -> tuple:
        return ()


@dataclass
class _SearchSource:
    def list_specs(self, document_id: str) -> tuple:
        return ()


@dataclass
class _ValidationSource:
    def inspect_for_search_spec(self, search_spec_id: str) -> tuple:
        return ()


@dataclass
class _DecisionSource:
    decisions: tuple = ()

    def list_decisions(self, document_id: str) -> tuple:
        return self.decisions


@dataclass
class _InstallationSource:
    context_entity_ids: frozenset = frozenset()

    def get_context_for_entity(self, document_id: str, entity_id: str):
        if entity_id in self.context_entity_ids:
            return SimpleNamespace(entity_id=entity_id)
        return None


@dataclass
class _CommissioningSource:
    plan: object | None = None

    def get(self, document_id: str):
        return self.plan


@dataclass
class _HealthSource:
    runs: tuple = ()

    def list_document_runs(self, document_id: str) -> tuple:
        return self.runs


def _revision(
    speakers: tuple[tuple[str, str | None], ...] = (('speaker-fl', 'FL'),),
) -> SceneRevision:
    entities = tuple(
        SimpleNamespace(entity_id=entity_id, kind='speaker', speaker_role=role)
        for entity_id, role in speakers
    )
    document = SimpleNamespace(
        room=SimpleNamespace(room_id='room'),
        entities=entities,
    )
    return SceneRevision(
        revision_id='revision-current',
        document_id='project-1',
        parent_revision_id=None,
        created_at_utc='2026-09-18T00:00:00+00:00',
        content_hash='a' * 64,
        document=document,
    )


def _service(
    revision: SceneRevision | None = None,
    **kwargs,
) -> OverviewReadinessService:
    return OverviewReadinessService(
        _SceneSource(revision if revision is not None else _revision()),
        _MeasurementSource(),
        _PredictionSource(),
        _SearchSource(),
        _ValidationSource(),
        **kwargs,
    )


def _domain(view, domain_id: str):
    return next(
        (d for d in view.secondary_domains if d.domain_id == domain_id), None
    )


def test_no_sources_no_domains() -> None:
    """No secondary section when no domain authority is wired (#887)."""
    view = _service().read('project-1')
    assert view.secondary_domains == ()


def test_decisions_domain_counts_records_and_flags() -> None:
    view = _service(
        assumption_decision_source=_DecisionSource(
            (
                SimpleNamespace(expires_at_utc='2020-01-01T00:00:00+00:00'),
                SimpleNamespace(expires_at_utc=None),
            )
        ),
        design_decision_source=_DecisionSource(
            (
                SimpleNamespace(applied_action_ref=None),
                SimpleNamespace(applied_action_ref=SimpleNamespace(kind='scene')),
            )
        ),
    ).read('project-1')

    domain = _domain(view, 'decisions')
    assert domain is not None
    assert domain.title == '決定'
    assert domain.state_label == '4件の決定'
    assert '前提 2件' in domain.detail
    assert '設計 2件' in domain.detail
    assert '未適用 1件' in domain.detail
    assert '期限切れ前提 1件' in domain.detail


def test_decisions_domain_empty() -> None:
    view = _service(
        assumption_decision_source=_DecisionSource(),
        design_decision_source=_DecisionSource(),
    ).read('project-1')

    domain = _domain(view, 'decisions')
    assert domain is not None
    assert domain.state_label == '記録なし'


def test_installation_domain_counts_contexts_and_links() -> None:
    revision = _revision(
        speakers=(('speaker-fl', 'FL'), ('speaker-fr', 'FR')),
    )
    view = _service(
        revision,
        installation_source=_InstallationSource(frozenset({'speaker-fl'})),
    ).read('project-1')

    domain = _domain(view, 'installation')
    assert domain is not None
    assert domain.state_label == '1/2 スピーカー'
    assert '未記録' in domain.detail
    assert domain.action is not None
    assert domain.action.target.workspace == WorkspaceId.OPTIMIZATION
    assert domain.action.target.section == 'interventions'


def test_installation_domain_complete() -> None:
    revision = _revision()
    view = _service(
        revision,
        installation_source=_InstallationSource(frozenset({'speaker-fl'})),
    ).read('project-1')

    domain = _domain(view, 'installation')
    assert domain.state_label == '1/1 スピーカー'
    assert 'すべて' in domain.detail


def test_commissioning_domain_states() -> None:
    """Plan absent/in-progress/finished map to distinct labels (#887)."""
    view = _service(commissioning_source=_CommissioningSource(None)).read(
        'project-1'
    )
    assert _domain(view, 'commissioning').state_label == '未開始'

    plan = SimpleNamespace(
        stage='measurement', skipped_requirements=frozenset(), finished=False
    )
    view = _service(commissioning_source=_CommissioningSource(plan)).read(
        'project-1'
    )
    domain = _domain(view, 'commissioning')
    assert domain.state_label == '進行中 · 測定'

    finished = SimpleNamespace(
        stage='readiness', skipped_requirements=frozenset(), finished=True
    )
    view = _service(commissioning_source=_CommissioningSource(finished)).read(
        'project-1'
    )
    assert _domain(view, 'commissioning').state_label == '完了'


def test_health_domain_reports_latest_run() -> None:
    view = _service(health_source=_HealthSource()).read('project-1')
    assert _domain(view, 'health').state_label == '未チェック'

    run = SimpleNamespace(
        assessments=(
            SimpleNamespace(state='within_baseline'),
            SimpleNamespace(state='within_baseline'),
            SimpleNamespace(state='changed'),
            SimpleNamespace(state='indeterminate'),
        )
    )
    view = _service(health_source=_HealthSource((run,))).read('project-1')
    domain = _domain(view, 'health')
    assert domain.state_label == '変化あり'
    assert '変化 1件' in domain.detail
    assert '判定不能 1件' in domain.detail


def test_health_domain_within_baseline() -> None:
    run = SimpleNamespace(
        assessments=(SimpleNamespace(state='within_baseline'),)
    )
    view = _service(health_source=_HealthSource((run,))).read('project-1')
    assert _domain(view, 'health').state_label == 'ベースライン内'


def test_domains_present_without_room() -> None:
    """Secondary domains are project-scoped — they must surface even when
    the primary workflow is blocked on a missing room (#887)."""
    empty = SceneRevision(
        revision_id='revision-empty',
        document_id='project-1',
        parent_revision_id=None,
        created_at_utc='2026-09-18T00:00:00+00:00',
        content_hash='b' * 64,
        document=SimpleNamespace(room=None, entities=()),
    )
    view = _service(
        empty,
        commissioning_source=_CommissioningSource(None),
        health_source=_HealthSource(),
    ).read('project-1')

    domain_ids = {d.domain_id for d in view.secondary_domains}
    assert domain_ids == {'commissioning', 'health'}
    # The missing-room blocker still dominates the primary summary.
    assert '部屋' in view.summary
