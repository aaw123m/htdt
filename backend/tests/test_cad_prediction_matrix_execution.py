from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_prediction_matrix import (
    build_prediction_matrix_spec,
    collect_matrix_results,
)
from htdt.cad_prediction_matrix_repository import (
    CadPredictionMatrixRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.prediction_matrix_service import PredictionMatrixService
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


DOCUMENT_ID = 'o986-matrix-fixture'
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


def _create(service, revision) -> str:
    providers = {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }
    spec = service.create_matrix(
        source_entity_ids=('speaker-fl', 'speaker-fr'),
        receiver_ids=('seat-a', 'seat-b'),
        providers=providers,
        frequency_axis_hz=AXIS,
        solver_implementation_ref=_solver_ref(),
        valid_frequency_domain=DOMAIN,
    )
    return spec.spec_id


def test_create_matrix_persists_spec(tmp_path):
    _, _, service = _fixture(tmp_path)
    revision = service.scene_repository.current_head(DOCUMENT_ID)
    spec_id = _create(service, revision)

    repo = CadPredictionMatrixRepository(service.scene_repository)
    spec = repo.get_spec(spec_id)
    assert spec is not None
    assert len(spec.sources) == 2
    assert len(spec.receivers) == 2
    assert {s.source_entity_id for s in spec.sources} == {
        'speaker-fl', 'speaker-fr'
    }
    assert {r.receiver_id for r in spec.receivers} == {'seat-a', 'seat-b'}


def test_run_matrix_executes_and_persists(tmp_path):
    _, _, service = _fixture(tmp_path)
    revision = service.scene_repository.current_head(DOCUMENT_ID)
    spec_id = _create(service, revision)
    providers = {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }

    run = service.run_matrix(spec_id, providers)
    assert run.state == 'READY'
    assert run.attempt == 1

    repo = CadPredictionMatrixRepository(service.scene_repository)
    result = repo.latest_result_set(spec_id)
    assert result is not None
    states = {cell.state for cell in result.cells}
    assert states == {'READY'}
    assert len(result.transfers) == 4
    assert len(repo.run_history(spec_id)) == 1


def test_reopen_and_reexecute_reuses_cached_cells(tmp_path):
    scene_repository, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    providers = {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }
    service.run_matrix(spec_id, providers)

    # Reopen through a fresh service: authorities replay from the store.
    reopened = PredictionMatrixService(scene_repository, DOCUMENT_ID)
    view = reopened.matrix_presentation()
    assert view.spec_id == spec_id
    assert view.currency_state == 'CURRENT'
    assert len(view.cells) == 4
    assert all(cell.state == 'READY' for cell in view.cells)
    assert view.source_labels == ('speaker-fl', 'speaker-fr')
    assert view.receiver_labels == ('seat-a', 'seat-b')

    second = reopened.run_matrix(spec_id, providers)
    assert second.attempt == 2
    result = reopened.repository.latest_result_set(spec_id)
    assert all(cell.state == 'CACHED' for cell in result.cells)


def test_dependency_aware_currency_stales_one_column(tmp_path):
    scene_repository, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    providers = {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }
    service.run_matrix(spec_id, providers)
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)

    # speaker-fl's equipment/pose moved: only its column goes stale.
    moved = _provider(
        revision,
        'speaker-fl',
        source_binding_sha256=_hash('bind:moved'),
    )
    currency = service.assess_currency(
        spec,
        result_set=result,
        providers={'speaker-fl': moved, 'speaker-fr': providers['speaker-fr']},
    )
    assert currency.state == 'STALE'
    stale_cells = set(currency.stale_cell_ids)
    fl_cells = {
        cell.cell_id
        for cell in result.cells
        if cell.matrix_source_id == 'source:speaker-fl'
    }
    fr_cells = {
        cell.cell_id
        for cell in result.cells
        if cell.matrix_source_id == 'source:speaker-fr'
    }
    assert stale_cells == fl_cells
    assert stale_cells.isdisjoint(fr_cells)


def test_shared_scene_change_stales_everything(tmp_path):
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    providers = {
        'speaker-fl': _provider(revision, 'speaker-fl'),
        'speaker-fr': _provider(revision, 'speaker-fr'),
    }
    service.run_matrix(spec_id, providers)
    spec = service.repository.get_spec(spec_id)
    result = service.repository.latest_result_set(spec_id)

    # A new scene revision moves the shared scene hash: every cell stale.
    document = revision.document.model_copy(
        update={'room': RoomPrism(width_m=7.0, depth_m=6.0, height_m=2.5)}
    )
    service.scene_repository.save(
        document, parent_revision_id=revision.revision_id
    )
    currency = service.assess_currency(spec, result_set=result)
    assert currency.state == 'STALE'
    assert len(currency.stale_cell_ids) == 4


def test_blocked_provider_marks_explicit_cells(tmp_path):
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    providers = {
        # speaker-fr has no provider run bound: its column is BLOCKED.
        'speaker-fl': _provider(revision, 'speaker-fl'),
    }
    run = service.run_matrix(spec_id, providers)
    result = service.repository.latest_result_set(spec_id)
    states = {
        cell.matrix_source_id: cell.state
        for cell in result.cells
        if cell.matrix_receiver_id == 'receiver:seat-a'
    }
    assert states['source:speaker-fl'] == 'READY'
    assert states['source:speaker-fr'] == 'BLOCKED'
    blocked = next(
        cell
        for cell in result.cells
        if cell.state == 'BLOCKED'
    )
    assert blocked.blocked_reason
    assert run.state == 'READY'


def test_presentation_without_results_shows_queued(tmp_path):
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    view = service.matrix_presentation(spec_id)
    assert all(cell.state == 'QUEUED' for cell in view.cells)
    assert view.run_state is None


def test_run_history_and_failures(tmp_path):
    _, revision, service = _fixture(tmp_path)
    spec_id = _create(service, revision)
    provider = _provider(revision, 'speaker-fl')
    provider.receiver_responses = ()  # covers no receiver -> blocked cells
    run = service.run_matrix(
        spec_id,
        {'speaker-fl': provider, 'speaker-fr': _provider(revision, 'speaker-fr')},
    )
    assert run.attempt == 1
    assert len(service.run_history(spec_id)) == 1

    # Running again always appends a new attempt, never rewrites history.
    second = service.run_matrix(
        spec_id,
        {
            'speaker-fl': _provider(revision, 'speaker-fl'),
            'speaker-fr': _provider(revision, 'speaker-fr'),
        },
    )
    assert second.attempt == 2
    assert len(service.run_history(spec_id)) == 2
