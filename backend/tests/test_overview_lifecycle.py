"""#443: variant lifecycle coverage and equipment readiness on Overview."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from htdt.cad_repository import SceneRevision
from htdt.overview_readiness import (
    OverviewNotice,
    OverviewReadinessService,
    notice_area,
)


@dataclass
class _SceneSource:
    revision: SceneRevision | None

    def current_head(self, document_id: str) -> SceneRevision | None:
        if self.revision is None or self.revision.document_id != document_id:
            return None
        return self.revision


@dataclass
class _EmptyMeasurements:
    def list_measurements(self, document_id: str) -> tuple:
        return ()

    def dataset_for_measurement(self, measurement_id: str):
        return None


class _EmptyPredictions:
    def list_results(self, document_id: str) -> tuple:
        return ()


class _EmptySearch:
    def list_specs(self, document_id: str) -> tuple:
        return ()


class _EmptyValidation:
    def inspect_for_search_spec(self, search_spec_id: str) -> tuple:
        return ()


def _revision(speakers=()) -> SceneRevision:
    entities = tuple(
        SimpleNamespace(entity_id=entity_id, kind='speaker', speaker_role=role)
        for entity_id, role in speakers
    )
    return SceneRevision(
        revision_id='rev-1',
        document_id='doc-1',
        parent_revision_id=None,
        created_at_utc='2026-09-23T00:00:00+00:00',
        content_hash='a' * 64,
        document=SimpleNamespace(
            room=SimpleNamespace(room_id='room'), entities=entities
        ),
    )


def _service(**kwargs) -> OverviewReadinessService:
    return OverviewReadinessService(
        _SceneSource(kwargs.get('revision', _revision())),
        _EmptyMeasurements(),
        _EmptyPredictions(),
        _EmptySearch(),
        _EmptyValidation(),
        variant_source=kwargs.get('variant_source'),
        equipment_source=kwargs.get('equipment_source'),
    )


class _VariantSource:
    def __init__(
        self,
        lifecycle_state: str,
        measurement_state: str,
        *,
        application_exists: bool = False,
    ) -> None:
        self._lifecycle = SimpleNamespace(
            state=lifecycle_state,
            label='設置済み',
            validation_label='未検証',
            application_exists=application_exists,
        )
        self._measurement = SimpleNamespace(
            state=measurement_state, state_label='未計画'
        )

    def variants(self) -> tuple:
        return (SimpleNamespace(variant_id='var-1', name='シアター案A'),)

    def lifecycle(self, variant_id: str):
        return self._lifecycle

    def measurement(self, variant_id: str):
        return self._measurement


def test_variant_states_cover_lifecycle() -> None:
    service = _service(
        variant_source=_VariantSource('as_built', 'campaign_preregistered')
    )
    view = service.read('doc-1')
    assert view.variant_states
    state = view.variant_states[0]
    assert state.variant_id == 'var-1'
    assert state.stage == 'campaign_preregistered'
    assert state.stage_label
    assert state.action is not None
    assert state.action.target.workspace.value == 'measurement'


def test_current_variant_maps_to_current() -> None:
    """#914: a current baseline is the live system, not an applied proposal."""
    service = _service(variant_source=_VariantSource('current', 'unplanned'))
    view = service.read('doc-1')
    state = view.variant_states[0]
    assert state.stage == 'current'
    assert state.stage_label == '現在のシステム'
    assert state.action is None


def test_applied_proposal_maps_to_applied_with_as_built_action() -> None:
    """#914: proposed + application authority ⇒ applied, routes to as-built."""
    service = _service(
        variant_source=_VariantSource(
            'proposed', 'unplanned', application_exists=True
        )
    )
    view = service.read('doc-1')
    state = view.variant_states[0]
    assert state.stage == 'applied'
    assert state.stage_label == '適用済み（設置記録なし）'
    assert state.action is not None
    assert state.action.target.workspace.value == 'optimization'
    assert state.action.target.section == 'validation'


def test_unapplied_proposal_stays_proposed() -> None:
    service = _service(variant_source=_VariantSource('proposed', 'unplanned'))
    view = service.read('doc-1')
    state = view.variant_states[0]
    assert state.stage == 'proposed'
    assert state.action is not None
    assert state.action.target.section == 'comparison'


def test_measured_variant_maps_to_measured_unvalidated() -> None:
    service = _service(
        variant_source=_VariantSource('measured', 'validation_pending')
    )
    view = service.read('doc-1')
    assert view.variant_states[0].stage == 'measured_unvalidated'


def test_equipment_binding_warning_only_when_unbound() -> None:
    class _Bindings:
        def __init__(self, bound: set[str]) -> None:
            self._bound = bound

        def get_binding_for_entity(self, document_id, entity_id):
            return object() if entity_id in self._bound else None

    revision = _revision(speakers=(('sp-fl', 'FL'), ('sp-fr', 'FR')))
    service = _service(revision=revision, equipment_source=_Bindings({'sp-fl'}))
    view = service.read('doc-1')
    warnings = {n.code: n for n in view.warnings}
    assert 'equipment.binding_missing' in warnings
    assert warnings['equipment.binding_missing'].action is not None

    bound = _service(revision=revision, equipment_source=_Bindings({'sp-fl', 'sp-fr'}))
    view = bound.read('doc-1')
    assert 'equipment.binding_missing' not in {n.code for n in view.warnings}


def test_notice_area_grouping() -> None:
    assert notice_area('room.geometry_incomplete') == 'room'
    assert notice_area('measurement.missing') == 'measurement'
    assert notice_area('prediction.missing') == 'prediction'
    assert notice_area('impact.stale') == 'optimization'
    notice = OverviewNotice(code='room.missing', severity='blocker', message='x')
    assert notice.area == 'room'
    assert notice.state_label == '要対応'
