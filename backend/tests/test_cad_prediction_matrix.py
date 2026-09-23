from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_prediction_matrix import (
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixSourceRef,
    assess_matrix_currency,
    build_matrix_cell,
    build_prediction_matrix_spec,
    collect_matrix_results,
    plan_matrix_execution,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _solver_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='solver:fixture',
        authority_version='1',
        semantic_hash_sha256=_hash('solver'),
    )


DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)


def _spec(**overrides):
    kwargs = dict(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        solver_implementation_ref=_solver_ref(),
        valid_frequency_domain=DOMAIN,
        sources=(
            MatrixSourceRef(
                matrix_source_id='source-fl',
                source_entity_id='entity-fl',
                source_binding_sha256=_hash('fl'),
            ),
            MatrixSourceRef(
                matrix_source_id='source-fr',
                source_entity_id='entity-fr',
                source_binding_sha256=_hash('fr'),
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id='seat-a',
                receiver_id='seat-a',
                receiver_entity_id='entity-a',
                receiver_binding_sha256=_hash('a'),
            ),
            MatrixReceiverRef(
                matrix_receiver_id='seat-b',
                receiver_id='seat-b',
                receiver_entity_id='entity-b',
                receiver_binding_sha256=_hash('b'),
            ),
        ),
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=(20.0, 100.0, 200.0)
        ),
    )
    kwargs.update(overrides)
    return build_prediction_matrix_spec(**kwargs)


def _provider(
    spec,
    *,
    receiver_ids: tuple[str, ...] = ('seat-a', 'seat-b'),
    capability_state: str = 'READY',
    phase_ready: bool = True,
) -> SimpleNamespace:
    responses = tuple(
        SimpleNamespace(
            receiver_id=receiver_id,
            frequency_hz=(20.0, 100.0, 200.0),
            magnitude_pa=(1.0, 0.5, 0.25),
            phase_deg=(0.0, -10.0, -20.0) if phase_ready else None,
            pressure_reference_pa=20.0e-6,
        )
        for receiver_id in receiver_ids
    )
    return SimpleNamespace(
        provider_id='r170a-provider:' + _hash('provider'),
        current_authority=SimpleNamespace(
            scene_content_hash=spec.scene_content_hash,
            acoustic_scene_snapshot_sha256=spec.acoustic_scene_snapshot_sha256,
            solver_implementation_ref=spec.solver_implementation_ref,
        ),
        capability=lambda observable: SimpleNamespace(
            state=capability_state,
            reason='fixture unsupported' if capability_state != 'READY' else None,
        ),
        phase_capability='READY' if phase_ready else 'UNSUPPORTED',
        receiver_responses=responses,
    )


def test_spec_identity_and_membership_rules() -> None:
    spec = _spec()
    assert spec.spec_id.startswith('prediction-matrix-spec:')
    with pytest.raises(ValueError, match='unique'):
        _spec(
            sources=(
                MatrixSourceRef(
                    matrix_source_id='s',
                    source_entity_id='e',
                    source_binding_sha256=_hash('s'),
                ),
                MatrixSourceRef(
                    matrix_source_id='s',
                    source_entity_id='e2',
                    source_binding_sha256=_hash('s2'),
                ),
            )
        )
    with pytest.raises(ValueError, match='exceeds the valid domain'):
        _spec(
            observable_contract=MatrixObservableContract(
                frequency_axis_hz=(5.0, 100.0)
            )
        )


def test_planner_batches_receivers_per_source() -> None:
    spec = _spec()
    plan = plan_matrix_execution(spec)
    # 2 sources x 2 receivers -> at most 2 provider tasks
    assert len(plan.batched_tasks) == 2
    for source_id, receiver_ids in plan.batched_tasks:
        assert receiver_ids == ('seat-a', 'seat-b')
    assert len(plan.cell_ids) == 4


def test_collect_ready_cells_and_transfers() -> None:
    spec = _spec(
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=(20.0, 100.0, 200.0),
            require_coherent_sum_eligible=True,
        )
    )
    providers = {
        'source-fl': _provider(spec),
        'source-fr': _provider(spec),
    }
    result = collect_matrix_results(spec, providers)
    assert result.result_id.startswith('prediction-matrix-result:')
    assert len(result.cells) == 4
    assert all(cell.state == 'READY' for cell in result.cells)
    assert len(result.transfers) == 4
    assert result.coherent_sum_eligible is True
    transfer = result.transfer('source-fl', 'seat-a')
    assert transfer.magnitude_pa == (1.0, 0.5, 0.25)
    assert transfer.phase_deg is not None


def test_missing_provider_blocks_cells_not_fakes() -> None:
    spec = _spec()
    result = collect_matrix_results(spec, {'source-fl': _provider(spec)})
    states = {
        (cell.matrix_source_id, cell.state) for cell in result.cells
    }
    assert ('source-fl', 'READY') in states
    assert ('source-fr', 'BLOCKED') in states
    blocked = [
        cell for cell in result.cells if cell.state == 'BLOCKED'
    ]
    assert all(cell.blocked_reason for cell in blocked)


def test_unsupported_capability_and_missing_receiver() -> None:
    spec = _spec()
    providers = {
        'source-fl': _provider(spec, capability_state='UNSUPPORTED'),
        'source-fr': _provider(spec, receiver_ids=('seat-a',)),
    }
    result = collect_matrix_results(spec, providers)
    cell_states = {
        (cell.matrix_source_id, cell.matrix_receiver_id): cell.state
        for cell in result.cells
    }
    assert cell_states[('source-fl', 'seat-a')] == 'UNSUPPORTED'
    assert cell_states[('source-fr', 'seat-b')] == 'BLOCKED'
    assert result.coherent_sum_eligible is False


def test_authority_mismatch_blocks() -> None:
    spec = _spec()
    provider = _provider(spec)
    provider.current_authority = SimpleNamespace(
        scene_content_hash=_hash('other-scene'),
        acoustic_scene_snapshot_sha256=spec.acoustic_scene_snapshot_sha256,
        solver_implementation_ref=spec.solver_implementation_ref,
    )
    result = collect_matrix_results(
        spec, {'source-fl': provider, 'source-fr': _provider(spec)}
    )
    cell_states = {
        (cell.matrix_source_id, cell.matrix_receiver_id): cell.state
        for cell in result.cells
    }
    assert cell_states[('source-fl', 'seat-a')] == 'BLOCKED'


def test_cached_cells_reuse_exact_hash() -> None:
    spec = _spec()
    providers = {'source-fl': _provider(spec), 'source-fr': _provider(spec)}
    cached = {('source-fl', 'seat-a'): _hash('cached')}
    result = collect_matrix_results(
        spec, providers, cached_result_sha256=cached
    )
    cell = result.cell('source-fl', 'seat-a')
    assert cell.state == 'CACHED'
    assert cell.result_sha256 == _hash('cached')


def test_currency_marks_stale_scene() -> None:
    spec = _spec()
    providers = {'source-fl': _provider(spec), 'source-fr': _provider(spec)}
    result = collect_matrix_results(spec, providers)
    current = assess_matrix_currency(
        spec,
        result,
        current_scene_content_hash=_hash('scene'),
        current_snapshot_sha256=_hash('snapshot'),
    )
    assert current.state == 'CURRENT'
    stale = assess_matrix_currency(
        spec,
        result,
        current_scene_content_hash=_hash('new-scene'),
        current_snapshot_sha256=_hash('snapshot'),
    )
    assert stale.state == 'STALE'
    assert stale.stale_cell_ids == tuple(c.cell_id for c in result.cells)


def test_cell_identity_and_state_invariants() -> None:
    spec = _spec()
    cell = build_matrix_cell(
        spec,
        matrix_source_id='source-fl',
        matrix_receiver_id='seat-a',
        state='QUEUED',
    )
    assert cell.cell_id.startswith('matrix-cell:')
    with pytest.raises(ValueError):
        build_matrix_cell(
            spec,
            matrix_source_id='ghost',
            matrix_receiver_id='seat-a',
        )
    with pytest.raises(ValueError):
        build_matrix_cell(
            spec,
            matrix_source_id='source-fl',
            matrix_receiver_id='seat-a',
            state='READY',  # READY without result hash
        )
