from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_hybrid_prediction_provider import (
    SPL_REFERENCE_PA,
    HybridPredictionProviderRef,
)
from htdt.cad_mixed_fidelity_provider_integration import (
    MIXED_FIDELITY_MODEL_ID,
    assess_mixed_matrix_coherent_compatibility,
    bind_mixed_fidelity_validation_providers,
    bind_provider_to_aim_evaluation,
    bind_provider_to_radiator_model,
    build_mixed_fidelity_measurement_validation,
    build_provider_multi_seat_binding,
    build_provider_multi_seat_member,
    collect_mixed_fidelity_matrix_results,
    execute_mixed_fidelity_prediction_matrix,
    plan_mixed_fidelity_matrix_execution,
    provider_radiator_transfer_evidence,
    resolve_provider_seat_responses,
)
from htdt.cad_bass_management import FrequencyBand
from htdt.cad_multi_seat_analysis import build_multi_seat_set
from htdt.cad_multi_radiator_source import (
    SourceRadiatorElement,
    build_multi_radiator_source_model,
)
from htdt.cad_scene import Offset3
from htdt.cad_prediction_matrix import (
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixSourceRef,
    build_prediction_matrix_spec,
)
from htdt.cad_prediction_provider import PredictionProviderRef
from htdt.cad_scene import Position3
from htdt.placement_constraints import (
    ConstraintSetCreate,
    PlacementEvaluationRequest,
    evaluate_constraint_set,
    validate_constraint_set_for_context,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

from test_cad_hybrid_prediction_provider import _build_bundle


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _solver_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='solver:fixture',
        authority_version='1',
        semantic_hash_sha256=_hash('solver'),
    )


DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)
GRID = (20.0, 100.0, 200.0)


def _spec(*, grid: tuple[float, ...] = GRID):
    return build_prediction_matrix_spec(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        solver_implementation_ref=_solver_ref(),
        valid_frequency_domain=DOMAIN,
        sources=(
            MatrixSourceRef(
                matrix_source_id='src-low',
                source_entity_id='entity-low',
                source_binding_sha256=_hash('low'),
            ),
            MatrixSourceRef(
                matrix_source_id='src-hybrid',
                source_entity_id='entity-hybrid',
                source_binding_sha256=_hash('hybrid'),
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id='seat-a',
                receiver_id='seat-a',
                receiver_entity_id='entity-a',
                receiver_binding_sha256=_hash('a'),
            ),
        ),
        observable_contract=MatrixObservableContract(frequency_axis_hz=grid),
    )


def _low_band_provider(
    spec,
    *,
    receiver_ids: tuple[str, ...] = ('seat-a',),
    capability_state: str = 'READY',
    phase_ready: bool = True,
    label: str = 'low',
) -> SimpleNamespace:
    """Duck-typed low-band lane: only the attributes the gating reads."""

    provider_id = 'r170a-provider:' + _hash(label)
    responses = tuple(
        SimpleNamespace(
            receiver_id=receiver_id,
            receiver_entity_id=f'entity-{receiver_id}',
            frequency_hz=GRID,
            magnitude_pa=(1.0, 0.5, 0.25),
            phase_deg=(0.0, -10.0, -20.0) if phase_ready else None,
            pressure_reference_pa=SPL_REFERENCE_PA,
            phase_convention='exp(+i*omega*t)',
        )
        for receiver_id in receiver_ids
    )
    return SimpleNamespace(
        provider_id=provider_id,
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
        magnitude_capability=capability_state,
        receiver_responses=responses,
        ref=lambda: PredictionProviderRef(
            provider_id=provider_id,
            semantic_sha256=_hash(label + '-sha'),
        ),
        result_artifact_ref=_solver_ref(),
        source_normalization_id='normalization/uniform',
        timing_authority='absolute_propagation_time',
    )


def _hybrid_provider(
    spec,
    *,
    source_entity_id: str = 'entity-hybrid',
    receiver_id: str = 'seat-a',
    label: str = 'hybrid',
    grid: tuple[float, ...] = GRID,
    stale_authority: bool = False,
) -> SimpleNamespace:
    """Duck-typed hybrid lane: exactly one source×receiver pair."""

    provider_id = 'r170b-hybrid-provider:' + _hash(label)
    samples = tuple(
        SimpleNamespace(
            frequency_hz=frequency,
            magnitude_pa=magnitude,
            phase_deg=phase,
        )
        for frequency, magnitude, phase in zip(
            grid,
            (0.8, 0.4, 0.2)[: len(grid)],
            (5.0, -15.0, -30.0)[: len(grid)],
            strict=True,
        )
    )
    scene_hash = _hash('other-scene') if stale_authority else spec.scene_content_hash
    return SimpleNamespace(
        provider_id=provider_id,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        base_current_authority=SimpleNamespace(
            scene_content_hash=scene_hash,
            acoustic_scene_snapshot_sha256=spec.acoustic_scene_snapshot_sha256,
            solver_implementation_ref=spec.solver_implementation_ref,
        ),
        capability=lambda observable: SimpleNamespace(
            state='READY', reason=None,
        ),
        phase_capability='READY',
        magnitude_capability='READY',
        output_frequency_grid_hz=grid,
        absolute_pressure_samples=samples,
        ref=lambda: HybridPredictionProviderRef(
            provider_id=provider_id,
            semantic_sha256=_hash(label + '-sha'),
        ),
        r160_artifact_ref=_solver_ref(),
        normalization_authority_ref=ExactExternalAuthorityRef(
            authority_id='normalization/uniform',
            authority_version='1',
            semantic_hash_sha256=_hash('norm'),
        ),
        phasor_convention='exp(+i*omega*t)',
    )


def _cell(result_set, matrix_source_id: str, matrix_receiver_id: str):
    return next(
        item
        for item in result_set.cells
        if item.matrix_source_id == matrix_source_id
        and item.matrix_receiver_id == matrix_receiver_id
    )


def test_mixed_plan_assigns_provider_lanes_per_cell() -> None:
    spec = _spec()
    low = _low_band_provider(spec)
    hybrid = _hybrid_provider(spec)

    plan = plan_mixed_fidelity_matrix_execution(
        spec,
        low_band_providers={'src-low': low},
        hybrid_providers={('entity-hybrid', 'seat-a'): hybrid},
    )
    lanes = {
        (item.matrix_source_id, item.matrix_receiver_id): item
        for item in plan.assignments
    }
    assert lanes[('src-hybrid', 'seat-a')].lane == 'hybrid'
    assert lanes[('src-hybrid', 'seat-a')].provider_id == hybrid.provider_id
    assert lanes[('src-low', 'seat-a')].lane == 'low_band'
    assert lanes[('src-low', 'seat-a')].provider_id == low.provider_id
    assert plan.batching_semantics == 'mixed_fidelity_per_cell_provider_batch'


def test_mixed_collect_resolves_each_lane_with_typed_provenance() -> None:
    spec = _spec()
    low = _low_band_provider(spec)
    hybrid = _hybrid_provider(spec)

    result_set = collect_mixed_fidelity_matrix_results(
        spec,
        low_band_providers={'src-low': low},
        hybrid_providers={('entity-hybrid', 'seat-a'): hybrid},
    )
    assert _cell(result_set, 'src-low', 'seat-a').state == 'READY'
    assert _cell(result_set, 'src-hybrid', 'seat-a').state == 'READY'

    low_transfer = result_set.transfer_for_cell(
        _cell(result_set, 'src-low', 'seat-a').cell_id
    )
    hybrid_transfer = result_set.transfer_for_cell(
        _cell(result_set, 'src-hybrid', 'seat-a').cell_id
    )
    assert isinstance(low_transfer.provider_ref, PredictionProviderRef)
    assert isinstance(hybrid_transfer.provider_ref, HybridPredictionProviderRef)
    assert hybrid_transfer.magnitude_pa == (0.8, 0.4, 0.2)
    assert hybrid_transfer.phase_deg == (5.0, -15.0, -30.0)
    assert hybrid_transfer.timing_authority == 'unavailable'
    assert hybrid_transfer.result_authority_ref == hybrid.r160_artifact_ref
    # The hybrid lane declares no timing authority: the batch honestly stays
    # ineligible for a coherent sum instead of silently losing the metadata.
    assert result_set.coherent_sum_eligible is False
    assert any('timing' in reason for reason in result_set.coherent_compatibility_reasons)


def test_mixed_collect_unbound_column_fails_closed() -> None:
    spec = _spec()
    result_set = collect_mixed_fidelity_matrix_results(
        spec, low_band_providers={},
    )
    for cell in result_set.cells:
        assert cell.state == 'BLOCKED'
        assert 'no provider run bound' in (cell.blocked_reason or '')


def test_mixed_collect_unsupported_capability_fails_closed() -> None:
    spec = _spec()
    low = _low_band_provider(spec, capability_state='UNSUPPORTED')
    result_set = collect_mixed_fidelity_matrix_results(
        spec, low_band_providers={'src-low': low},
    )
    cell = _cell(result_set, 'src-low', 'seat-a')
    assert cell.state == 'UNSUPPORTED'
    assert 'unsupported' in (cell.blocked_reason or '').lower()


def test_mixed_collect_hybrid_gate_rejects_mismatched_authority_and_grid() -> None:
    spec = _spec()
    stale = _hybrid_provider(spec, stale_authority=True)
    result_set = collect_mixed_fidelity_matrix_results(
        spec,
        hybrid_providers={('entity-hybrid', 'seat-a'): stale},
    )
    cell = _cell(result_set, 'src-hybrid', 'seat-a')
    assert cell.state == 'BLOCKED'
    assert 'base authority' in (cell.blocked_reason or '')

    off_grid = _hybrid_provider(spec, grid=(40.0, 80.0))
    result_set = collect_mixed_fidelity_matrix_results(
        spec,
        hybrid_providers={('entity-hybrid', 'seat-a'): off_grid},
    )
    cell = _cell(result_set, 'src-hybrid', 'seat-a')
    assert cell.state == 'BLOCKED'
    assert 'output grid' in (cell.blocked_reason or '')

    wrong_pair = _hybrid_provider(spec, receiver_id='seat-b')
    result_set = collect_mixed_fidelity_matrix_results(
        spec,
        # Keyed to the spec pair while the provider itself pins seat-b: the
        # key alone can never smuggle a mismatched provider into a cell.
        hybrid_providers={('entity-hybrid', 'seat-a'): wrong_pair},
    )
    cell = _cell(result_set, 'src-hybrid', 'seat-a')
    assert cell.state == 'BLOCKED'
    assert 'receiver identity' in (cell.blocked_reason or '')


def test_mixed_collect_cached_cells_verify_result_authority() -> None:
    spec = _spec()
    low = _low_band_provider(spec)
    first = collect_mixed_fidelity_matrix_results(
        spec, low_band_providers={'src-low': low},
    )
    cell = _cell(first, 'src-low', 'seat-a')
    second = collect_mixed_fidelity_matrix_results(
        spec,
        low_band_providers={'src-low': low},
        cached_result_sha256={('src-low', 'seat-a'): cell.result_sha256},
    )
    assert _cell(second, 'src-low', 'seat-a').state == 'CACHED'
    with pytest.raises(ValueError, match='cached result hash'):
        collect_mixed_fidelity_matrix_results(
            spec,
            low_band_providers={'src-low': low},
            cached_result_sha256={('src-low', 'seat-a'): _hash('tampered')},
        )


def test_mixed_execute_reports_terminal_state_without_repository() -> None:
    spec = _spec()
    low = _low_band_provider(spec)
    run = execute_mixed_fidelity_prediction_matrix(
        spec,
        low_band_providers={'src-low': low},
        hybrid_providers={('entity-hybrid', 'seat-a'): _hybrid_provider(spec)},
        started_at_utc='2026-09-30T00:00:00Z',
        finished_at_utc='2026-09-30T00:00:01Z',
    )
    assert run.state == 'READY'
    assert run.attempt == 1

    # #986 semantics: the run is READY when the batch executed to its
    # terminal state; per-cell BLOCKED/UNSUPPORTED verdicts live on the
    # result set, not the run state.
    blocked = execute_mixed_fidelity_prediction_matrix(
        spec,
        low_band_providers={},
        started_at_utc='2026-09-30T00:00:00Z',
        finished_at_utc='2026-09-30T00:00:01Z',
    )
    assert blocked.state == 'READY'
    assert blocked.cell_state_counts == {'BLOCKED': 2}


def test_mixed_coherent_compatibility_flags_missing_metadata() -> None:
    spec = _spec()
    hybrid = _hybrid_provider(spec)
    reasons = assess_mixed_matrix_coherent_compatibility(
        spec,
        hybrid_providers={('entity-hybrid', 'seat-a'): hybrid},
    )
    assert any('timing' in reason for reason in reasons)
    assert not any('grid' in reason for reason in reasons)

    low = _low_band_provider(spec)
    reasons = assess_mixed_matrix_coherent_compatibility(
        spec,
        low_band_providers={'src-low': low},
        hybrid_providers={('entity-hybrid', 'seat-a'): hybrid},
    )
    # Hybrid participant has no declared timing authority; the low-band one
    # does — only the hybrid cell is flagged.
    assert any('entity-hybrid/seat-a' in reason for reason in reasons)


def _bundle_spec(bundle):
    """Matrix spec pinned to the real provider authority from the bundle."""

    base = bundle['base_provider']
    hybrid = bundle['provider']
    source = base.source_identity
    receiver_identity = next(
        item
        for item in base.receiver_identities
        if item.receiver_binding.receiver_id == hybrid.receiver_id
    )
    return build_prediction_matrix_spec(
        document_id=base.current_authority.document_id,
        scene_revision_id=base.current_authority.scene_revision_id,
        scene_content_hash=base.current_authority.scene_content_hash,
        acoustic_scene_snapshot_id=(
            base.current_authority.acoustic_scene_snapshot_id
        ),
        acoustic_scene_snapshot_sha256=(
            base.current_authority.acoustic_scene_snapshot_sha256
        ),
        solver_implementation_ref=(
            base.current_authority.solver_implementation_ref
        ),
        valid_frequency_domain=base.current_authority.valid_frequency_domain,
        sources=(
            MatrixSourceRef(
                matrix_source_id='src-real',
                source_entity_id=source.source_binding.source_entity_id,
                source_binding_sha256=source.source_binding_sha256,
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id='seat-real',
                receiver_id=hybrid.receiver_id,
                receiver_entity_id=(
                    receiver_identity.receiver_binding.entity_id
                ),
                receiver_binding_sha256=(
                    receiver_identity.receiver_binding_sha256
                ),
            ),
        ),
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=tuple(
                float(item) for item in hybrid.output_frequency_grid_hz
            )
        ),
    )


def test_mixed_collect_over_real_providers_routes_hybrid_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    spec = _bundle_spec(bundle)
    result_set = collect_mixed_fidelity_matrix_results(
        spec,
        low_band_providers={'src-real': bundle['base_provider']},
        hybrid_providers={
            (
                bundle['provider'].source_entity_id,
                bundle['provider'].receiver_id,
            ): bundle['provider']
        },
    )
    cell = _cell(result_set, 'src-real', 'seat-real')
    assert cell.state == 'READY'
    transfer = result_set.transfer_for_cell(cell.cell_id)
    assert isinstance(transfer.provider_ref, HybridPredictionProviderRef)
    assert transfer.magnitude_pa == tuple(
        item.magnitude_pa for item in bundle['provider'].absolute_pressure_samples
    )

    # Without the hybrid binding the same cell resolves to the low-band lane.
    fallback = collect_mixed_fidelity_matrix_results(
        spec,
        low_band_providers={'src-real': bundle['base_provider']},
    )
    fallback_cell = _cell(fallback, 'src-real', 'seat-real')
    assert fallback_cell.state == 'READY'
    fallback_transfer = fallback.transfer_for_cell(fallback_cell.cell_id)
    assert isinstance(fallback_transfer.provider_ref, PredictionProviderRef)


def _fake_measurement_repository(
    spec, *, measurement_id: str, dataset_level_db: tuple[float, ...],
):
    measurement = SimpleNamespace(
        measurement_id=measurement_id,
        document_id=spec.document_id,
        scene_revision_id=spec.scene_revision_id,
        scene_content_hash=spec.scene_content_hash,
    )
    dataset = SimpleNamespace(
        frequency_hz=GRID,
        level_db=dataset_level_db,
    )
    return SimpleNamespace(
        get_measurement=lambda item: measurement if item == measurement_id else None,
        dataset_for_measurement=lambda item: dataset if item == measurement_id else None,
    )


def test_mixed_o70_residual_records_one_sample_per_bound_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    base = bundle['base_provider']
    spec = _bundle_spec(bundle)
    result_set = collect_mixed_fidelity_matrix_results(
        spec, low_band_providers={'src-real': base},
    )
    repository = _fake_measurement_repository(
        spec, measurement_id='meas-1', dataset_level_db=(80.0, 75.0, 70.0),
    )
    record = build_mixed_fidelity_measurement_validation(
        result_set=result_set,
        spec=spec,
        measurement_repository=repository,
        cell_measurements={('src-real', 'seat-real'): 'meas-1'},
        document_id=spec.document_id,
        search_spec_id='search-1',
        search_spec_sha256=_hash('search'),
        candidate_set_sha256=_hash('candidates'),
        split='holdout',
        low_hz=20.0,
        high_hz=200.0,
        max_holdout_rms_db=3.0,
    )
    assert record.model_id == MIXED_FIDELITY_MODEL_ID
    assert len(record.pairs) == 1
    pair = record.pairs[0]
    assert pair.prediction_source_id == base.provider_id
    assert pair.measurement_id == 'meas-1'

    bindings = bind_mixed_fidelity_validation_providers(
        record, low_band_providers=(base,),
    )
    assert len(bindings) == 1
    assert bindings[0].consumer_kind == 'O70_ADAPTIVE'
    assert bindings[0].provider_ref.provider_id == base.provider_id

    # A provider the record never references cannot bind (fail closed).
    with pytest.raises(ValueError, match='does not reference'):
        bind_mixed_fidelity_validation_providers(
            record, hybrid_providers=(bundle['provider'],),
        )


def test_mixed_o70_rejects_unbound_or_revision_mismatched_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    spec = _bundle_spec(bundle)
    result_set = collect_mixed_fidelity_matrix_results(
        spec, low_band_providers={'src-real': bundle['base_provider']},
    )

    # No cell carries a bound measurement -> fail closed, never emit an
    # empty residual record.
    with pytest.raises(ValueError, match='no READY/CACHED matrix cell'):
        build_mixed_fidelity_measurement_validation(
            result_set=result_set,
            spec=spec,
            measurement_repository=_fake_measurement_repository(
                spec, measurement_id='meas-1', dataset_level_db=(1.0, 1.0, 1.0),
            ),
            cell_measurements={},
            document_id=spec.document_id,
            search_spec_id='search-1',
            search_spec_sha256=_hash('search'),
            candidate_set_sha256=_hash('candidates'),
            split='holdout',
            low_hz=20.0,
            high_hz=200.0,
            max_holdout_rms_db=3.0,
        )

    # A measurement pinned to a different revision is refused.
    stale = SimpleNamespace(
        measurement_id='meas-1',
        document_id=spec.document_id,
        scene_revision_id='other-revision',
        scene_content_hash=_hash('other'),
    )
    repository = SimpleNamespace(
        get_measurement=lambda item: stale,
        dataset_for_measurement=lambda item: None,
    )
    with pytest.raises(ValueError, match='SceneRevision'):
        build_mixed_fidelity_measurement_validation(
            result_set=result_set,
            spec=spec,
            measurement_repository=repository,
            cell_measurements={('src-real', 'seat-real'): 'meas-1'},
            document_id=spec.document_id,
            search_spec_id='search-1',
            search_spec_sha256=_hash('search'),
            candidate_set_sha256=_hash('candidates'),
            split='holdout',
            low_hz=20.0,
            high_hz=200.0,
            max_holdout_rms_db=3.0,
        )


def test_o80_multi_seat_binds_predicted_members_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    base = bundle['base_provider']
    hybrid = bundle['provider']
    document_id = base.current_authority.document_id

    low_member = build_provider_multi_seat_member(
        base, receiver_id=base.receiver_responses[0].receiver_id,
        seat_label='MLP', is_mlp=True,
    )
    hybrid_member = build_provider_multi_seat_member(
        hybrid, receiver_id=hybrid.receiver_id, seat_label='Left seat',
    )
    assert low_member.evidence_type == 'predicted'
    assert low_member.dataset_id.startswith(base.provider_id + '/')
    assert hybrid_member.dataset_id.startswith(hybrid.provider_id + '/')
    assert low_member.measurement_id == base.provider_id

    analysis_set = build_multi_seat_set(
        (low_member, hybrid_member),
        document_id=document_id,
        created_at='2026-09-30T00:00:00Z',
    )
    binding = build_provider_multi_seat_binding(
        analysis_set,
        low_band_providers={base.provider_id: base},
        hybrid_providers={hybrid.provider_id: hybrid},
    )
    assert binding.set_sha256 == analysis_set.set_sha256
    assert len(binding.member_bindings) == 2
    assert {item.consumer_kind for item in binding.seat_providers} == {
        'O80_MULTI_SEAT'
    }
    assert {
        item.provider_ref.provider_id for item in binding.seat_providers
    } == {base.provider_id, hybrid.provider_id}

    resolved = resolve_provider_seat_responses(
        binding,
        analysis_set,
        low_band_providers={base.provider_id: base},
        hybrid_providers={hybrid.provider_id: hybrid},
        low_hz=40.0,
        high_hz=80.0,
    )
    assert set(resolved) == {0, 1}
    assert all(len(item.frequency_hz) >= 2 for item in resolved.values())

    # A tampered member dataset hash fails closed at bind time.
    tampered = low_member.model_copy(
        update={'dataset_sha256': _hash('tampered')}
    )
    tampered_set = build_multi_seat_set(
        (tampered,), document_id=document_id,
        created_at='2026-09-30T00:00:00Z',
    )
    with pytest.raises(ValueError, match='dataset hash'):
        build_provider_multi_seat_binding(
            tampered_set, low_band_providers={base.provider_id: base},
        )

    # A predicted member with no bound provider fails closed.
    with pytest.raises(ValueError, match='no bound prediction provider'):
        build_provider_multi_seat_binding(
            analysis_set, low_band_providers={}, hybrid_providers={},
        )


def test_o80_radiator_binding_gates_on_equipment_and_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    base = bundle['base_provider']
    source_binding = base.source_identity.source_binding

    model = build_multi_radiator_source_model(
        model_id='model-1',
        version='1',
        equipment_definition_id=source_binding.equipment_definition_id,
        equipment_definition_version=(
            source_binding.equipment_definition_version
        ),
        equipment_definition_sha256=(
            source_binding.equipment_definition_sha256
        ),
        elements=(
            SourceRadiatorElement(
                element_id='driver-1',
                kind='driver',
                local_position_m=Offset3(),
                valid_band=FrequencyBand(low_hz=20.0, high_hz=200.0),
                transfer_evidence='none',
            ),
        ),
    )
    assert provider_radiator_transfer_evidence(base) == 'complex'
    binding = bind_provider_to_radiator_model(base, model)
    assert binding.consumer_kind == 'O80_MULTI_RADIATOR'
    assert 'frequency_response_phase' in binding.required_observables

    # A radiator model pinned to different equipment cannot borrow this
    # provider's transfer authority.
    other = model.model_copy(
        update={'equipment_definition_sha256': _hash('other-equipment')}
    )
    with pytest.raises(ValueError, match='equipment identity'):
        bind_provider_to_radiator_model(base, other)


def test_o80_aim_binding_fails_closed_without_spatial_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build_bundle(tmp_path / 'real', monkeypatch, output_grid=(40.0, 80.0))
    for provider in (bundle['base_provider'], bundle['provider']):
        with pytest.raises(ValueError):
            bind_provider_to_aim_evaluation(
                provider,
                evaluation_id='aim-1',
                evaluation_sha256=_hash('aim'),
            )


def _z_context() -> dict:
    vertices = [
        {'vertex_id': 'v0', 'x_m': 0.0, 'y_m': 0.0},
        {'vertex_id': 'v1', 'x_m': 4.0, 'y_m': 0.0},
        {'vertex_id': 'v2', 'x_m': 4.0, 'y_m': 5.0},
        {'vertex_id': 'v3', 'x_m': 0.0, 'y_m': 5.0},
    ]
    return {
        'room': {
            'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4,
            'geometry_kind': 'polygon_prism', 'footprint_vertices': vertices,
        },
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left',
             'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'FR', 'role': 'front_right',
             'position': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0},
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def _z_spec(*, collision: bool = True):
    request = {
        'context_id': 'ctx',
        'name': 'z-envelope containment',
        'entity_profiles': [
            {
                'entity_id': 'FL',
                'footprint_vertices_xy_m': [
                    {'x_m': -0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': 0.2},
                    {'x_m': -0.2, 'y_m': 0.2},
                ],
                'z_extent_m': 0.2,
            },
            {
                'entity_id': 'FR',
                'footprint_vertices_xy_m': [
                    {'x_m': -0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': 0.2},
                    {'x_m': -0.2, 'y_m': 0.2},
                ],
                'z_extent_m': 0.2,
            },
        ],
        'constraints': (
            [
                {
                    'constraint_id': 'speaker-collision',
                    'kind': 'entity_collision',
                    'entity_a': 'FL',
                    'entity_b': 'FR',
                }
            ]
            if collision
            else []
        ),
    }
    return validate_constraint_set_for_context(
        ConstraintSetCreate.model_validate(request), _z_context()
    )


def _evaluate(spec, positions: dict[str, dict[str, float]]):
    return evaluate_constraint_set(
        _z_context(), spec,
        PlacementEvaluationRequest.model_validate({'positions': positions}),
    )


def test_r120b_ceiling_envelope_overflow_is_rejected() -> None:
    spec = _z_spec(collision=False)
    # XY fully in bounds but the declared envelope top (2.6m) exceeds the
    # 2.4m room height -> rejected by the built-in room boundary check.
    result = _evaluate(spec, {
        'FL': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 2.4},
        'FR': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0},
    })
    rejected = {item['constraint_id'] for item in result['rejections']}
    assert '__room_boundary__:FL' in rejected
    assert '__room_boundary__:FR' not in rejected

    inside = _evaluate(spec, {
        'FL': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 2.0},
        'FR': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0},
    })
    assert inside['feasible'] is True


def test_r120b_underfloor_envelope_keeps_origin_semantics() -> None:
    spec = _z_spec(collision=False)
    # Envelope bottom dips below the floor while the origin stays at z>=0:
    # the floor side keeps the legacy origin-point check (documented
    # asymmetric semantics — seat cushions legitimately hang below their
    # origin point).
    result = _evaluate(spec, {
        'FL': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 0.1},
        'FR': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0},
    })
    assert result['feasible'] is True


def test_r120b_entity_collision_is_a_declared_constraint() -> None:
    spec = _z_spec()
    # Overlapping XY footprints with overlapping z intervals -> collision.
    result = _evaluate(spec, {
        'FL': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 1.0},
        'FR': {'x_m': 2.1, 'y_m': 2.0, 'z_m': 1.1},
    })
    rejected = {item['constraint_id']: item for item in result['rejections']}
    assert 'speaker-collision' in rejected
    assert rejected['speaker-collision']['kind'] == 'entity_collision'

    # Same XY overlap but disjoint z intervals -> no 3D collision.
    clear_z = _evaluate(spec, {
        'FL': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 0.5},
        'FR': {'x_m': 2.1, 'y_m': 2.0, 'z_m': 1.5},
    })
    assert clear_z['feasible'] is True

    # XY-separated envelopes -> no collision even at the same height.
    clear_xy = _evaluate(spec, {
        'FL': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0},
        'FR': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0},
    })
    assert clear_xy['feasible'] is True


def test_r120b_entity_collision_requires_declared_envelopes() -> None:
    request = {
        'context_id': 'ctx',
        'name': 'collision without declared z extent',
        'entity_profiles': [
            {
                'entity_id': 'FL',
                'footprint_vertices_xy_m': [
                    {'x_m': -0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': 0.2},
                    {'x_m': -0.2, 'y_m': 0.2},
                ],
            },
            {
                'entity_id': 'FR',
                'footprint_vertices_xy_m': [
                    {'x_m': -0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': -0.2},
                    {'x_m': 0.2, 'y_m': 0.2},
                    {'x_m': -0.2, 'y_m': 0.2},
                ],
                'z_extent_m': 0.2,
            },
        ],
        'constraints': [
            {
                'constraint_id': 'speaker-collision',
                'kind': 'entity_collision',
                'entity_a': 'FL',
                'entity_b': 'FR',
            }
        ],
    }
    with pytest.raises(ValueError, match='Z extent'):
        validate_constraint_set_for_context(
            ConstraintSetCreate.model_validate(request), _z_context()
        )

    # Undeclared collision pairs never fail silently either — two identical
    # entities or unknown entities are rejected at validation.
    with pytest.raises(ValueError):
        ConstraintSetCreate.model_validate({
            'context_id': 'ctx',
            'constraints': [
                {
                    'constraint_id': 'self-collision',
                    'kind': 'entity_collision',
                    'entity_a': 'FL',
                    'entity_b': 'FL',
                }
            ],
        })
