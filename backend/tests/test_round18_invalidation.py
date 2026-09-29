"""Round-18 derived-data invalidation regression tests.

* ``PredictionMatrixService.assess_currency`` must compare the spec's
  pinned ``acoustic_scene_snapshot_sha256`` against the providers'
  *current* snapshot authority — not against the spec's own pinned value
  (which made the snapshot-drift check tautological and dead).
* ``assess_matrix_currency`` must fail closed when a spec source or
  receiver has no re-verifiable binding in the supplied binding map —
  an absent key means "cannot prove current", not "unchanged".
* ``ActivityCenter`` staleness wiring: data-management operations pin the
  managed-data fingerprint at submit, and a mutating operation (restore /
  relocate / storage GC) marks results bound to the superseded
  fingerprint ``COMPLETED_FOR_HISTORICAL_INPUT`` instead of leaving the
  machinery unwired.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from htdt.activity_center import (
    ActivityCenter,
    OperationState,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.data_management import (
    ApplicationDataLifecycle,
    DataManagementBackend,
    DataManagementController,
    DataOperationKind,
)
from htdt.prediction_matrix_service import PredictionMatrixService
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _lifecycle() -> ApplicationDataLifecycle:
    return ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )


# -- prediction matrix harness (mirrors test_cad_prediction_matrix_execution)


DOCUMENT_ID = 'r18-matrix-fixture'
AXIS = (20.0, 100.0, 200.0)
DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _solver_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='solver:fixture',
        authority_version='1',
        semantic_hash_sha256=_hash('solver'),
    )


def _speaker(entity_id: str, x_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        speaker_role=entity_id.upper(),
        position=Position3(x_m=x_m, y_m=0.0, z_m=1.0),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
    )


def _seat(entity_id: str, y_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=0.0, y_m=y_m, z_m=0.5),
        size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
        acoustic_reference_offset_m=Offset3(z_m=0.5),
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(
            _speaker('speaker-fl', -1.0),
            _speaker('speaker-fr', 1.0),
            _seat('seat-a', 2.0),
            _seat('seat-b', 3.0),
        ),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    service = PredictionMatrixService(scene_repository, DOCUMENT_ID)
    return scene_repository, revision, service


def _provider(
    revision,
    source_entity_id: str,
    *,
    receiver_ids: tuple[str, ...] = ('seat-a', 'seat-b'),
    source_binding_sha256: str | None = None,
    snapshot: tuple[str, str] = ('snapshot-1', ''),
) -> SimpleNamespace:
    snapshot_id, snapshot_sha = snapshot
    snapshot_sha = snapshot_sha or _hash('snapshot')
    entity_by_receiver = {'seat-a': 'seat-a', 'seat-b': 'seat-b'}
    return SimpleNamespace(
        provider_id='r170a-provider:' + _hash(source_entity_id),
        current_authority=SimpleNamespace(
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            acoustic_scene_snapshot_id=snapshot_id,
            acoustic_scene_snapshot_sha256=snapshot_sha,
            solver_implementation_ref=_solver_ref(),
        ),
        source_identity=SimpleNamespace(
            source_binding=SimpleNamespace(source_entity_id=source_entity_id),
            source_binding_sha256=(
                source_binding_sha256 or _hash(f'bind:{source_entity_id}')
            ),
        ),
        receiver_identities=tuple(
            SimpleNamespace(
                receiver_binding=SimpleNamespace(
                    receiver_id=receiver_id,
                    entity_id=entity_by_receiver[receiver_id],
                ),
                receiver_binding_sha256=_hash(f'rbind:{receiver_id}'),
            )
            for receiver_id in receiver_ids
        ),
        capability=lambda observable: SimpleNamespace(
            state='READY', reason=None
        ),
        phase_capability='READY',
        ref=lambda: None,
        result_artifact_ref=_solver_ref(),
        source_normalization_id='norm-shared',
        timing_authority='absolute_propagation_time',
        receiver_responses=tuple(
            SimpleNamespace(
                receiver_id=receiver_id,
                frequency_hz=AXIS,
                magnitude_pa=(1.0, 0.5, 0.25),
                phase_deg=(0.0, -10.0, -20.0),
                pressure_reference_pa=20.0e-6,
                phase_convention='exp(-iwt)',
            )
            for receiver_id in receiver_ids
        ),
    )


def _providers(revision) -> dict[str, SimpleNamespace]:
    return {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }


def _create(service) -> str:
    spec = service.create_matrix(
        source_entity_ids=('speaker-fl', 'speaker-fr'),
        receiver_ids=('seat-a', 'seat-b'),
        providers=_providers(service.scene_repository.current_head(DOCUMENT_ID)),
        frequency_axis_hz=AXIS,
        solver_implementation_ref=_solver_ref(),
        valid_frequency_domain=DOMAIN,
    )
    return spec.spec_id


def _cell_ids(result, *, source=None, receiver=None) -> set[str]:
    return {
        cell.cell_id
        for cell in result.cells
        if (source is None or cell.matrix_source_id == source)
        and (receiver is None or cell.matrix_receiver_id == receiver)
    }


# -- finding 1: snapshot drift must stale the matrix -------------------------


def test_matrix_snapshot_drift_marks_stale(tmp_path: Path) -> None:
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service)
    service.run_matrix(spec_id, _providers(revision))
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)
    assert spec is not None and result is not None

    unchanged = service.assess_currency(
        spec, result_set=result, providers=_providers(revision)
    )
    assert unchanged is not None
    assert unchanged.state == 'CURRENT'

    # The scene content hash is identical — only the acoustic snapshot
    # rotated (e.g. a boundary/treatment rebinding under the same scene
    # revision authority). The spec's pinned snapshot sha must be compared
    # against the providers' current authority, not itself.
    drifted = {
        entity_id: _provider(
            revision,
            entity_id,
            snapshot=('snapshot-2', _hash('snapshot-2')),
        )
        for entity_id in ('speaker-fl', 'speaker-fr')
    }
    currency = service.assess_currency(
        spec, result_set=result, providers=drifted
    )
    assert currency is not None
    assert currency.state == 'STALE'
    assert any('snapshot' in reason for reason in currency.stale_reasons)
    assert set(currency.stale_cell_ids) == _cell_ids(result)


def test_matrix_divergent_provider_snapshots_fail_closed(tmp_path) -> None:
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service)
    service.run_matrix(spec_id, _providers(revision))
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)
    assert spec is not None and result is not None

    # Providers bound to different snapshots: no single current authority
    # exists, so currency cannot be claimed — fail closed.
    divergent = _providers(revision)
    divergent['speaker-fr'] = _provider(
        revision,
        'speaker-fr',
        snapshot=('snapshot-2', _hash('snapshot-2')),
    )
    currency = service.assess_currency(
        spec, result_set=result, providers=divergent
    )
    assert currency is not None
    assert currency.state == 'STALE'


# -- finding 2: unverifiable bindings must not read as exact -----------------


def test_matrix_missing_source_binding_marks_column_stale(
    tmp_path: Path,
) -> None:
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service)
    service.run_matrix(spec_id, _providers(revision))
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)
    assert spec is not None and result is not None

    # Only speaker-fl's provider is supplied: speaker-fr's pinned binding
    # cannot be re-verified, so its column must not report current.
    currency = service.assess_currency(
        spec,
        result_set=result,
        providers={'speaker-fl': _provider(revision, 'speaker-fl')},
    )
    assert currency is not None
    assert currency.state == 'STALE'
    assert _cell_ids(result, source='source:speaker-fr') == set(
        currency.stale_cell_ids
    )
    assert _cell_ids(result, source='source:speaker-fl').isdisjoint(
        currency.stale_cell_ids
    )


def test_matrix_missing_receiver_binding_marks_row_stale(
    tmp_path: Path,
) -> None:
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service)
    service.run_matrix(spec_id, _providers(revision))
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)
    assert spec is not None and result is not None

    # The live providers only cover seat-a now: seat-b's row cannot be
    # re-verified and must stale instead of staying silently exact.
    partial = {
        entity_id: _provider(
            revision, entity_id, receiver_ids=('seat-a',)
        )
        for entity_id in ('speaker-fl', 'speaker-fr')
    }
    currency = service.assess_currency(
        spec, result_set=result, providers=partial
    )
    assert currency is not None
    assert currency.state == 'STALE'
    assert _cell_ids(result, receiver='receiver:seat-b') == set(
        currency.stale_cell_ids
    )
    assert _cell_ids(result, receiver='receiver:seat-a').isdisjoint(
        currency.stale_cell_ids
    )


# -- finding 3: activity-center staleness wiring ------------------------------


def test_data_op_result_marks_superseded_after_mutation(tmp_path) -> None:
    _app()
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    revision = repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision

    center = ActivityCenter()
    controller = DataManagementController(
        DataManagementBackend(data_dir),
        _lifecycle(),
        activity_center=center,
    )
    try:
        controller._start(
            operation_id='op-scan',
            kind=DataOperationKind.SCAN_STORAGE,
            job=lambda emit: object(),
            lifecycle_mode='none',
        )
        assert _pump_until(
            lambda: center.get('op-scan') is not None
            and center.get('op-scan').state is OperationState.COMPLETED
        )
        scan = center.get('op-scan')
        assert scan is not None
        assert scan.current_for_input is True
        assert scan.input_authority_refs, (
            'the operation must pin the managed-data fingerprint'
        )

        # Mutating op: the job replaces the managed data (here via a scene
        # write on the same database — the fingerprint is stat-level).
        document = revision.document.model_copy(
            update={
                'room': RoomPrism(width_m=7.0, depth_m=6.0, height_m=2.5)
            }
        )

        def mutating_job(emit):
            repository.save(
                document, parent_revision_id=revision.revision_id
            )
            return object()

        controller._start(
            operation_id='op-gc',
            kind=DataOperationKind.GC_STORAGE,
            job=mutating_job,
            lifecycle_mode='none',
        )
        assert _pump_until(
            lambda: center.get('op-gc') is not None
            and center.get('op-gc').state is OperationState.COMPLETED
        )

        superseded = center.get('op-scan')
        assert superseded is not None
        assert (
            superseded.state
            is OperationState.COMPLETED_FOR_HISTORICAL_INPUT
        )
        assert superseded.current_for_input is False

        # The mutating op pinned the pre-mutation fingerprint, so it too is
        # honestly not-current-for-input — but its own state stays
        # COMPLETED: the mutation is its result, not a stale artifact.
        mutating = center.get('op-gc')
        assert mutating is not None
        assert mutating.state is OperationState.COMPLETED
    finally:
        controller.deleteLater()


def test_data_op_result_stays_current_without_mutation(tmp_path) -> None:
    _app()
    data_dir = tmp_path / 'data'
    SceneRepository(data_dir / 'cad-scenes.sqlite3').save(
        make_f1_scene(), parent_revision_id=None
    )

    center = ActivityCenter()
    controller = DataManagementController(
        DataManagementBackend(data_dir),
        _lifecycle(),
        activity_center=center,
    )
    try:
        controller._start(
            operation_id='op-scan-1',
            kind=DataOperationKind.SCAN_STORAGE,
            job=lambda emit: object(),
            lifecycle_mode='none',
        )
        assert _pump_until(
            lambda: center.get('op-scan-1') is not None
            and center.get('op-scan-1').state is OperationState.COMPLETED
        )
        # A second read-only op does not mark the first stale.
        controller._start(
            operation_id='op-scan-2',
            kind=DataOperationKind.SCAN_STORAGE,
            job=lambda emit: object(),
            lifecycle_mode='none',
        )
        assert _pump_until(
            lambda: center.get('op-scan-2') is not None
            and center.get('op-scan-2').state is OperationState.COMPLETED
        )
        first = center.get('op-scan-1')
        assert first is not None
        assert first.state is OperationState.COMPLETED
        assert first.current_for_input is True
    finally:
        controller.deleteLater()
