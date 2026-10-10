"""R170B extended integration (#101).

A bounded software slice on top of the solver-neutral provider contracts:

- **mixed-fidelity batch**: one ``PredictionMatrixSpec`` drives each
  (source, receiver) cell through the provider lane that is actually bound
  to it — the R170A multi-receiver low-band lane (exact R130 complex-pressure
  artifacts) or the R170B hybrid lane (exact R160 composed results). Every
  lane re-checks the spec's scene/snapshot/solver pins, the provider's
  declared capabilities, and the observable contract's frequency grid; a cell
  with no admissible lane fails closed into BLOCKED/UNSUPPORTED instead of
  being silently dropped or fabricated.
- **O70 residual/adaptive**: a mixed result set feeds the existing O60
  residual authority as one ``CadModelValidationRecord`` — one sample per
  READY/CACHED cell with an explicitly bound measurement — and each
  referenced provider binds into the O70 adaptive path through the existing
  typed binding records (no solver payloads cross this boundary).
- **O80 multi-seat / multi-radiator / aim**: predicted members of a
  ``MultiSeatAnalysisSet`` resolve through the typed provider reads; a
  ``MultiRadiatorSourceModel`` binding records which provider the element
  transfer evidence rests on, gated on the equipment-identity pins and the
  provider's phase capability; aim evaluation binds only when the provider
  declares ``spatial_pressure_field`` — every current provider reports it
  UNSUPPORTED, so the binding fails closed rather than inventing directivity.

Every carried artifact keeps typed provenance (provider refs, result
authority refs, scene/snapshot pins); no binding makes a production claim.
"""

from __future__ import annotations

from math import isfinite, log10
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_hybrid_prediction_provider import (
    SPL_REFERENCE_PA,
    HybridPredictionProvider,
    HybridPredictionProviderBinding,
    HybridPredictionProviderRef,
    build_hybrid_provider_binding,
    hybrid_provider_frequency_response,
)
from .cad_hybrid_prediction_provider_integration import (
    bind_hybrid_provider_to_adaptive_validation,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation import (
    CadModelValidationRecord,
    EvidenceScope,
    build_model_validation,
)
from .cad_multi_radiator_source import (
    MultiRadiatorSourceModel,
    RadiatorTransferEvidence,
)
from .cad_multi_seat_analysis import MultiSeatAnalysisSet, MultiSeatMember
from .cad_prediction_matrix import (
    PREDICTION_MATRIX_RESULT_AUTHORITY_VERSION,
    PREDICTION_MATRIX_SCHEMA_VERSION,
    MatrixCell,
    MatrixCellTransfer,
    MatrixExecutionRun,
    PredictionMatrixSpec,
    TransferMatrixResultSet,
    build_matrix_cell,
    build_matrix_execution_run,
    cached_result_sha256_map,
)
from .cad_prediction_provider import (
    LowBandPredictionProvider,
    PredictionProviderBinding,
    PredictionProviderRef,
    build_prediction_provider_binding,
)
from .cad_prediction_provider_integration import (
    bind_provider_to_adaptive_validation,
    provider_frequency_response,
)
from .comparison import FrequencyResponse
from .canonical_json import canonical_sha256 as _digest


R170B_EXTENDED_INTEGRATION_AUTHORITY_VERSION = (
    'r170b-extended-integration-1'
)
MIXED_FIDELITY_MODEL_ID = 'htdt.r170b.mixed_fidelity_matrix'
MIXED_FIDELITY_MODEL_VERSION = '1'


MixedCellLane = Literal['low_band', 'hybrid', 'unbound']


class MixedFidelityCellAssignment(BaseModel):
    """Per-cell provider lane resolution recorded in the plan."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    matrix_source_id: str = Field(min_length=1)
    matrix_receiver_id: str = Field(min_length=1)
    lane: MixedCellLane
    provider_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_assignment(self) -> 'MixedFidelityCellAssignment':
        if (self.lane == 'unbound') != (self.provider_id is None):
            raise ValueError(
                'unbound cells cannot carry a provider identity; bound '
                'cells require one'
            )
        return self


class MixedFidelityMatrixPlan(BaseModel):
    """Batched provider execution plan over mixed fidelity lanes.

    ``batching_semantics`` declares that low-band columns still batch all
    their receivers into one provider task while hybrid cells resolve one
    provider task per (source, receiver) pair — the R160 hybrid provider is
    exactly single-pair by contract.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170b-extended-integration-1'
    ] = R170B_EXTENDED_INTEGRATION_AUTHORITY_VERSION
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    assignments: tuple[MixedFidelityCellAssignment, ...] = Field(min_length=1)
    batching_semantics: Literal[
        'mixed_fidelity_per_cell_provider_batch'
    ] = 'mixed_fidelity_per_cell_provider_batch'


def _low_band_column_gate(
    spec: PredictionMatrixSpec,
    provider: LowBandPredictionProvider | None,
) -> tuple[Literal['BLOCKED', 'UNSUPPORTED'] | None, str | None]:
    """Column-level gate for the R170A lane; None when the lane is admissible."""

    if provider is None:
        return 'BLOCKED', 'no provider run bound for this matrix source'
    if (
        provider.current_authority.scene_content_hash
        != spec.scene_content_hash
        or provider.current_authority.acoustic_scene_snapshot_sha256
        != spec.acoustic_scene_snapshot_sha256
        or provider.current_authority.solver_implementation_ref
        != spec.solver_implementation_ref
    ):
        return 'BLOCKED', 'provider authority does not match matrix spec'
    capability = provider.capability('frequency_response_magnitude')
    if capability.state != 'READY':
        return 'UNSUPPORTED', (
            capability.reason or 'provider observable unsupported'
        )
    return None, None


def _hybrid_cell_gate(
    spec: PredictionMatrixSpec,
    source_entity_id: str,
    receiver_id: str,
    provider: HybridPredictionProvider,
) -> tuple[Literal['BLOCKED', 'UNSUPPORTED'] | None, str | None]:
    """Per-cell gate for the R170B hybrid lane (exactly one pair/provider)."""

    if provider.source_entity_id != source_entity_id:
        return 'BLOCKED', 'hybrid provider source identity does not match the matrix spec'
    if provider.receiver_id != receiver_id:
        return 'BLOCKED', 'hybrid provider receiver identity does not match the matrix spec'
    authority = provider.base_current_authority
    if (
        authority.scene_content_hash != spec.scene_content_hash
        or authority.acoustic_scene_snapshot_sha256
        != spec.acoustic_scene_snapshot_sha256
        or authority.solver_implementation_ref
        != spec.solver_implementation_ref
    ):
        return (
            'BLOCKED',
            'hybrid provider base authority does not match the matrix spec',
        )
    capability = provider.capability('frequency_response_magnitude')
    if capability.state != 'READY':
        return 'UNSUPPORTED', (
            capability.reason or 'hybrid provider observable unsupported'
        )
    if tuple(provider.output_frequency_grid_hz) != tuple(
        spec.observable_contract.frequency_axis_hz
    ):
        return (
            'BLOCKED',
            'hybrid provider output grid does not match the matrix '
            'observable contract',
        )
    return None, None


def _resolve_low_band_provider(
    spec: PredictionMatrixSpec,
    matrix_source_id: str,
    providers: Mapping[str, LowBandPredictionProvider],
) -> LowBandPredictionProvider | None:
    provider = providers.get(matrix_source_id)
    if provider is None:
        for source in spec.sources:
            if source.matrix_source_id == matrix_source_id:
                provider = providers.get(source.source_entity_id)
                break
    return provider


def _low_band_cell_transfer(
    spec: PredictionMatrixSpec,
    provider: LowBandPredictionProvider,
    receiver,
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...] | None, float, str]:
    response = next(
        (
            item
            for item in provider.receiver_responses
            if item.receiver_id == receiver.receiver_id
        ),
        None,
    )
    if response is None:
        raise LookupError('provider run does not cover receiver')
    if tuple(response.frequency_hz) != tuple(
        spec.observable_contract.frequency_axis_hz
    ):
        raise LookupError(
            'provider response frequency grid does not match the matrix '
            'observable contract'
        )
    return (
        response.frequency_hz,
        response.magnitude_pa,
        response.phase_deg,
        float(response.pressure_reference_pa),
        response.phase_convention,
    )


def collect_mixed_fidelity_matrix_results(
    spec: PredictionMatrixSpec,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[tuple[str, str], HybridPredictionProvider] | None = None,
    cached_result_sha256: Mapping[tuple[str, str], str] | None = None,
) -> TransferMatrixResultSet:
    """Assemble the result set over mixed provider lanes.

    A cell resolves to the hybrid lane when a hybrid provider is bound to
    its exact ``(source_entity_id, receiver_id)`` pair; otherwise it falls
    back to the column's low-band provider. Every admissible lane re-verifies
    authority pins, capability and the observable-contract grid — a cell that
    admits no lane is BLOCKED/UNSUPPORTED with an explicit reason, never
    silently skipped.
    """
    low_band_providers = low_band_providers or {}
    hybrid_providers = hybrid_providers or {}
    cached_result_sha256 = cached_result_sha256 or {}
    cells: list[MatrixCell] = []
    transfers: list[MatrixCellTransfer] = []
    coherent_ok = True

    for source in spec.sources:
        column_provider = _resolve_low_band_provider(
            spec, source.matrix_source_id, low_band_providers
        )
        column_state, column_reason = _low_band_column_gate(
            spec, column_provider
        )
        for receiver in spec.receivers:
            cell_key = (source.matrix_source_id, receiver.matrix_receiver_id)
            hybrid = hybrid_providers.get(
                (source.source_entity_id, receiver.receiver_id)
            )
            if hybrid is not None:
                gate_state, gate_reason = _hybrid_cell_gate(
                    spec, source.source_entity_id, receiver.receiver_id, hybrid
                )
                if gate_state is not None:
                    cells.append(
                        build_matrix_cell(
                            spec,
                            matrix_source_id=source.matrix_source_id,
                            matrix_receiver_id=receiver.matrix_receiver_id,
                            state=gate_state,
                            blocked_reason=gate_reason,
                        )
                    )
                    coherent_ok = False
                    continue
                samples = hybrid.absolute_pressure_samples
                frequency_hz = tuple(
                    float(item.frequency_hz) for item in samples
                )
                magnitude_pa = tuple(
                    float(item.magnitude_pa) for item in samples
                )
                phase_deg = tuple(
                    float(item.phase_deg) for item in samples
                )
                result_sha = _digest(
                    {
                        'kind': 'matrix-cell-transfer',
                        'spec_semantic_sha256': spec.semantic_sha256,
                        'provider_id': hybrid.provider_id,
                        'receiver_id': receiver.receiver_id,
                        'frequency_hz': list(frequency_hz),
                        'magnitude_pa': list(magnitude_pa),
                        'phase_deg': list(phase_deg),
                        'pressure_reference_pa': SPL_REFERENCE_PA,
                    }
                )
                state: Literal['READY', 'CACHED'] = 'READY'
                if cell_key in cached_result_sha256:
                    if cached_result_sha256[cell_key] != result_sha:
                        raise ValueError(
                            'cached result hash does not match the result '
                            'authority of cell '
                            f'{source.matrix_source_id}x{receiver.matrix_receiver_id}'
                        )
                    state = 'CACHED'
                cell = build_matrix_cell(
                    spec,
                    matrix_source_id=source.matrix_source_id,
                    matrix_receiver_id=receiver.matrix_receiver_id,
                    state=state,
                    result_sha256=result_sha,
                )
                cells.append(cell)
                transfers.append(
                    MatrixCellTransfer(
                        cell_id=cell.cell_id,
                        frequency_hz=frequency_hz,
                        magnitude_pa=magnitude_pa,
                        phase_deg=phase_deg,
                        pressure_reference_pa=SPL_REFERENCE_PA,
                        provider_ref=hybrid.ref(),
                        result_authority_ref=hybrid.r160_artifact_ref,
                        source_normalization_id=(
                            hybrid.normalization_authority_ref.authority_id
                        ),
                        phasor_convention=hybrid.phasor_convention,
                        # R160 hybrid pressure is a steady-state phasor stack;
                        # no timing authority is declared, so a coherent sum
                        # honestly stays ineligible.
                        timing_authority='unavailable',
                    )
                )
                continue
            if column_state is not None:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state=column_state,
                        blocked_reason=column_reason,
                    )
                )
                coherent_ok = False
                continue
            assert column_provider is not None
            if column_provider.phase_capability != 'READY':
                coherent_ok = False
            try:
                (
                    frequency_hz,
                    magnitude_pa,
                    phase_deg,
                    pressure_reference_pa,
                    phase_convention,
                ) = _low_band_cell_transfer(
                    spec, column_provider, receiver
                )
            except LookupError as exc:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='BLOCKED',
                        blocked_reason=str(exc),
                    )
                )
                continue
            result_sha = _digest(
                {
                    'kind': 'matrix-cell-transfer',
                    'spec_semantic_sha256': spec.semantic_sha256,
                    'provider_id': column_provider.provider_id,
                    'receiver_id': receiver.receiver_id,
                    'frequency_hz': list(frequency_hz),
                    'magnitude_pa': list(magnitude_pa),
                    'phase_deg': (
                        list(phase_deg) if phase_deg is not None else None
                    ),
                    'pressure_reference_pa': pressure_reference_pa,
                }
            )
            state = 'READY'
            if cell_key in cached_result_sha256:
                if cached_result_sha256[cell_key] != result_sha:
                    raise ValueError(
                        'cached result hash does not match the result '
                        'authority of cell '
                        f'{source.matrix_source_id}x{receiver.matrix_receiver_id}'
                    )
                state = 'CACHED'
            cell = build_matrix_cell(
                spec,
                matrix_source_id=source.matrix_source_id,
                matrix_receiver_id=receiver.matrix_receiver_id,
                state=state,
                result_sha256=result_sha,
            )
            cells.append(cell)
            transfers.append(
                MatrixCellTransfer(
                    cell_id=cell.cell_id,
                    frequency_hz=frequency_hz,
                    magnitude_pa=magnitude_pa,
                    phase_deg=phase_deg,
                    pressure_reference_pa=pressure_reference_pa,
                    provider_ref=column_provider.ref(),
                    result_authority_ref=column_provider.result_artifact_ref,
                    source_normalization_id=(
                        column_provider.source_normalization_id
                    ),
                    phasor_convention=phase_convention,
                    timing_authority=column_provider.timing_authority,
                )
            )

    coherence_reasons = assess_mixed_matrix_coherent_compatibility(
        spec,
        low_band_providers=low_band_providers,
        hybrid_providers=hybrid_providers,
    )
    coherent_ok = coherent_ok and not coherence_reasons
    eligible = (
        coherent_ok
        and spec.observable_contract.require_coherent_sum_eligible
    )
    payload = {
        'schema_version': PREDICTION_MATRIX_SCHEMA_VERSION,
        'authority_version': PREDICTION_MATRIX_RESULT_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'cells': [item.model_dump(mode='json') for item in cells],
        'transfers': [item.model_dump(mode='json') for item in transfers],
        'coherent_sum_eligible': eligible,
    }
    if coherence_reasons:
        payload['coherent_compatibility_reasons'] = list(coherence_reasons)
    digest = _digest(payload)
    return TransferMatrixResultSet(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        cells=tuple(cells),
        transfers=tuple(transfers),
        coherent_sum_eligible=eligible,
        coherent_compatibility_reasons=tuple(coherence_reasons),
        result_id=f'prediction-matrix-result:{digest}',
        semantic_sha256=digest,
    )


def assess_mixed_matrix_coherent_compatibility(
    spec: PredictionMatrixSpec,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[tuple[str, str], HybridPredictionProvider] | None = None,
) -> tuple[str, ...]:
    """Coherent-composition compatibility over mixed provider lanes (#942).

    Same machine checks as the same-fidelity assessor — a common frequency
    grid, complex pressure with a common phasor convention, a common explicit
    source normalization, and declared timing authority — evaluated per bound
    (source, receiver) participant rather than per source column.
    """
    low_band_providers = low_band_providers or {}
    hybrid_providers = hybrid_providers or {}
    reasons: list[str] = []
    participants: list[tuple[str, dict[str, Any]]] = []

    for source in spec.sources:
        column_provider = _resolve_low_band_provider(
            spec, source.matrix_source_id, low_band_providers
        )
        column_authority_ok = (
            column_provider is not None
            and column_provider.current_authority.scene_content_hash
            == spec.scene_content_hash
            and column_provider.current_authority.acoustic_scene_snapshot_sha256
            == spec.acoustic_scene_snapshot_sha256
            and column_provider.current_authority.solver_implementation_ref
            == spec.solver_implementation_ref
        )
        for receiver in spec.receivers:
            hybrid = hybrid_providers.get(
                (source.source_entity_id, receiver.receiver_id)
            )
            if hybrid is not None:
                label = f'{source.source_entity_id}/{receiver.receiver_id}'
                authority = hybrid.base_current_authority
                if (
                    authority.scene_content_hash != spec.scene_content_hash
                    or authority.acoustic_scene_snapshot_sha256
                    != spec.acoustic_scene_snapshot_sha256
                    or authority.solver_implementation_ref
                    != spec.solver_implementation_ref
                ):
                    reasons.append(
                        f'cell {label} hybrid provider authority does not '
                        'match the matrix spec'
                    )
                    continue
                participants.append(
                    (
                        label,
                        {
                            'grid': tuple(
                                hybrid.output_frequency_grid_hz
                            ),
                            'phase_ready': (
                                hybrid.phase_capability == 'READY'
                            ),
                            'convention': hybrid.phasor_convention,
                            'normalization_id': (
                                hybrid.normalization_authority_ref.authority_id
                            ),
                            'timing': 'unavailable',
                        },
                    )
                )
                continue
            if column_provider is None:
                reasons.append(
                    f'source {source.source_entity_id} receiver '
                    f'{receiver.receiver_id} has no bound provider run'
                )
                continue
            if not column_authority_ok:
                reasons.append(
                    f'source {source.source_entity_id} provider authority '
                    'does not match the matrix spec'
                )
                continue
            response = next(
                (
                    item
                    for item in column_provider.receiver_responses
                    if item.receiver_id == receiver.receiver_id
                ),
                None,
            )
            if response is None:
                reasons.append(
                    f'source {source.source_entity_id} does not cover '
                    f'receiver {receiver.receiver_id}'
                )
                continue
            participants.append(
                (
                    f'{source.source_entity_id}/{receiver.receiver_id}',
                    {
                        'grid': tuple(response.frequency_hz),
                        'phase_ready': (
                            column_provider.phase_capability == 'READY'
                            and response.phase_deg is not None
                        ),
                        'convention': response.phase_convention,
                        'normalization_id': (
                            column_provider.source_normalization_id
                        ),
                        'timing': column_provider.timing_authority,
                    },
                )
            )

    if not participants:
        return tuple(sorted(set(reasons))) or (
            'no coherent-compatible cells were collected',
        )

    for label, participant in participants:
        if not participant['phase_ready']:
            reasons.append(
                f'cell {label} carries magnitude-only data; it cannot '
                'enter a coherent sum'
            )

    grids = {participant['grid'] for _label, participant in participants}
    if len(grids) > 1:
        reasons.append('cells do not share a common frequency grid')

    conventions = {
        participant['convention'] for _label, participant in participants
    }
    if len(conventions) > 1:
        reasons.append(
            'cells declare different phasor conventions: '
            + ','.join(sorted(str(item) for item in conventions))
        )

    normalizations = {
        participant['normalization_id'] for _label, participant in participants
    }
    if None in normalizations:
        reasons.append(
            'at least one cell has no declared source normalization'
        )
    elif len(normalizations) > 1:
        reasons.append(
            'cells declare different source normalizations: '
            + ','.join(sorted(str(item) for item in normalizations if item))
        )

    for label, participant in participants:
        if participant['timing'] in (None, 'unavailable'):
            reasons.append(f'cell {label} lacks declared timing authority')

    return tuple(sorted(set(reasons)))


def plan_mixed_fidelity_matrix_execution(
    spec: PredictionMatrixSpec,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[tuple[str, str], HybridPredictionProvider] | None = None,
) -> MixedFidelityMatrixPlan:
    """Record the per-cell provider lane resolution for one mixed matrix."""
    low_band_providers = low_band_providers or {}
    hybrid_providers = hybrid_providers or {}
    assignments: list[MixedFidelityCellAssignment] = []
    for source in spec.sources:
        column_provider = _resolve_low_band_provider(
            spec, source.matrix_source_id, low_band_providers
        )
        for receiver in spec.receivers:
            hybrid = hybrid_providers.get(
                (source.source_entity_id, receiver.receiver_id)
            )
            if hybrid is not None:
                lane: MixedCellLane = 'hybrid'
                provider_id: str | None = hybrid.provider_id
            elif column_provider is not None:
                lane = 'low_band'
                provider_id = column_provider.provider_id
            else:
                lane = 'unbound'
                provider_id = None
            assignments.append(
                MixedFidelityCellAssignment(
                    matrix_source_id=source.matrix_source_id,
                    matrix_receiver_id=receiver.matrix_receiver_id,
                    lane=lane,
                    provider_id=provider_id,
                )
            )
    return MixedFidelityMatrixPlan(
        spec_semantic_sha256=spec.semantic_sha256,
        assignments=tuple(assignments),
    )


def execute_mixed_fidelity_prediction_matrix(
    spec: PredictionMatrixSpec,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[tuple[str, str], HybridPredictionProvider] | None = None,
    repository=None,
    attempt: int | None = None,
    started_at_utc: str,
    finished_at_utc: str | None = None,
) -> MatrixExecutionRun:
    """Drive one mixed-fidelity matrix execution (#986 semantics).

    Same persisted run/result authority as the same-fidelity path: cells
    whose prior persisted result already carries the exact result authority
    are marked CACHED and reused verbatim.
    """
    if repository is not None:
        repository.save_spec(spec)
    plan = plan_mixed_fidelity_matrix_execution(
        spec,
        low_band_providers=low_band_providers,
        hybrid_providers=hybrid_providers,
    )
    _ = plan  # batching semantics are carried by provider composition

    if attempt is None:
        attempt = 1
        if repository is not None:
            attempt = len(repository.run_history(spec.spec_id)) + 1

    cached: dict[tuple[str, str], str] = {}
    if repository is not None:
        latest = repository.latest_result_set(spec.spec_id)
        if latest is not None:
            cached = cached_result_sha256_map(latest)

    try:
        result_set = collect_mixed_fidelity_matrix_results(
            spec,
            low_band_providers=low_band_providers,
            hybrid_providers=hybrid_providers,
            cached_result_sha256=cached,
        )
    except Exception as exc:  # error-boundary: run boundary — any collection failure records a FAILED execution run with the exception identity and re-raises; the run ledger is never silent (noqa: BLE001)
        run = build_matrix_execution_run(
            spec=spec,
            attempt=attempt,
            state='FAILED',
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            failure_reason=str(exc),
        )
        if repository is not None:
            repository.save_run(run)
        raise
    if repository is not None:
        repository.save_result_set(result_set)
    terminal = (
        'READY'
        if all(cell.state != 'FAILED' for cell in result_set.cells)
        else 'BLOCKED'
    )
    run = build_matrix_execution_run(
        spec=spec,
        attempt=attempt,
        state=terminal,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        result_set=result_set,
    )
    if repository is not None:
        repository.save_run(run)
    return run


def _transfer_band_response(
    transfer: MatrixCellTransfer,
    *,
    low_hz: float,
    high_hz: float,
) -> FrequencyResponse:
    """Project a cell transfer onto the requested band in level units."""

    low = float(low_hz)
    high = float(high_hz)
    if not isfinite(low) or not isfinite(high) or high <= low:
        raise ValueError('mixed-fidelity validation band is invalid')
    selected = [
        (float(frequency), float(magnitude))
        for frequency, magnitude in zip(
            transfer.frequency_hz, transfer.magnitude_pa, strict=True
        )
        if low <= float(frequency) <= high
    ]
    if len(selected) < 2:
        raise ValueError(
            'matrix cell transfer has fewer than two points in requested band'
        )
    reference = float(transfer.pressure_reference_pa)
    return FrequencyResponse(
        frequency_hz=tuple(item[0] for item in selected),
        level_db=tuple(
            20.0 * log10(item[1] / reference) for item in selected
        ),
    )


def build_mixed_fidelity_measurement_validation(
    *,
    result_set: TransferMatrixResultSet,
    spec: PredictionMatrixSpec,
    measurement_repository: CadMeasurementRepository,
    cell_measurements: Mapping[tuple[str, str], str],
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    split: Literal['calibration', 'holdout'],
    low_hz: float,
    high_hz: float,
    max_holdout_rms_db: float,
    evidence_scope: EvidenceScope = 'synthetic_fixture',
    campaign_id: str | None = None,
    campaign_sha256: str | None = None,
) -> CadModelValidationRecord:
    """O60 residual record over a mixed-fidelity result set.

    One sample per READY/CACHED cell whose ``(matrix_source_id,
    matrix_receiver_id)`` carries an explicitly bound measurement. The
    predicted side is the cell transfer's own exact authority projected into
    level units at its pinned pressure reference; the measured side must be
    bound to the spec's SceneRevision. Cells without a bound measurement are
    omitted — a residual cannot be computed for them — and when no cell is
    admissible the builder fails closed instead of emitting an empty record.
    """
    if result_set.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('result set does not belong to the supplied spec')
    if document_id != spec.document_id:
        raise ValueError('mixed-fidelity validation document identity mismatch')

    samples: list[tuple] = []
    for cell in result_set.cells:
        if cell.state not in ('READY', 'CACHED'):
            continue
        measurement_id = cell_measurements.get(
            (cell.matrix_source_id, cell.matrix_receiver_id)
        )
        if measurement_id is None:
            continue
        measurement = measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise ValueError(
                f'matrix cell measurement does not exist: {measurement_id}'
            )
        if (
            measurement.document_id != spec.document_id
            or measurement.scene_revision_id != spec.scene_revision_id
            or measurement.scene_content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'matrix cell measurement is not bound to the spec '
                'SceneRevision'
            )
        dataset = measurement_repository.dataset_for_measurement(
            measurement_id
        )
        if dataset is None:
            raise ValueError(
                f'matrix cell measurement has no frequency response: '
                f'{measurement_id}'
            )
        transfer = result_set.transfer_for_cell(cell.cell_id)
        if transfer.provider_ref is None:
            raise ValueError(
                f'matrix cell {cell.cell_id} carries no provider provenance'
            )
        predicted = _transfer_band_response(
            transfer, low_hz=low_hz, high_hz=high_hz
        )
        measured = FrequencyResponse(
            frequency_hz=dataset.frequency_hz,
            level_db=dataset.level_db,
        )
        samples.append(
            (
                cell.cell_id,
                split,
                transfer.provider_ref.provider_id,
                measurement_id,
                predicted,
                measured,
            )
        )
    if not samples:
        raise ValueError(
            'no READY/CACHED matrix cell carries a bound measurement'
        )
    return build_model_validation(
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        # The record-level model identity is the batch lane itself; the exact
        # per-sample solver/provider identity stays on each pair's
        # prediction_source_id.
        model_id=MIXED_FIDELITY_MODEL_ID,
        model_version=MIXED_FIDELITY_MODEL_VERSION,
        samples=tuple(samples),
        low_hz=low_hz,
        high_hz=high_hz,
        max_holdout_rms_db=max_holdout_rms_db,
        evidence_scope=evidence_scope,
    )


def bind_mixed_fidelity_validation_providers(
    validation: CadModelValidationRecord,
    *,
    low_band_providers=(),
    hybrid_providers=(),
) -> tuple[
    PredictionProviderBinding | HybridPredictionProviderBinding, ...
]:
    """O70 adaptive bindings: every listed provider must be referenced."""

    bindings: list[
        PredictionProviderBinding | HybridPredictionProviderBinding
    ] = []
    for provider in low_band_providers:
        bindings.append(
            bind_provider_to_adaptive_validation(provider, validation)
        )
    for provider in hybrid_providers:
        bindings.append(
            bind_hybrid_provider_to_adaptive_validation(provider, validation)
        )
    return tuple(bindings)


def _provider_seat_dataset_sha(
    provider_id: str,
    provider_sha: str,
    receiver_id: str,
    frequency_hz: tuple[float, ...],
    magnitude_pa: tuple[float, ...],
    phase_deg: tuple[float, ...] | None,
    pressure_reference_pa: float,
) -> str:
    return _digest(
        {
            'kind': 'r170b-provider-seat-dataset',
            'provider_id': provider_id,
            'provider_semantic_sha256': provider_sha,
            'receiver_id': receiver_id,
            'frequency_hz': list(frequency_hz),
            'magnitude_pa': list(magnitude_pa),
            'phase_deg': (
                list(phase_deg) if phase_deg is not None else None
            ),
            'pressure_reference_pa': pressure_reference_pa,
        }
    )


def _low_band_seat_payload(
    provider: LowBandPredictionProvider,
    receiver_id: str,
) -> tuple[str, str, tuple[float, ...], tuple[float, ...], tuple[float, ...] | None, float]:
    identity = next(
        (
            item
            for item in provider.receiver_identities
            if item.receiver_binding.receiver_id == receiver_id
        ),
        None,
    )
    if identity is None:
        raise ValueError(
            f'provider does not declare receiver identity: {receiver_id}'
        )
    response = next(
        (
            item
            for item in provider.receiver_responses
            if item.receiver_id == receiver_id
        ),
        None,
    )
    if response is None:
        raise ValueError(
            f'provider run does not cover receiver: {receiver_id}'
        )
    return (
        identity.receiver_binding.entity_id,
        response.receiver_entity_id,
        response.frequency_hz,
        response.magnitude_pa,
        response.phase_deg,
        float(response.pressure_reference_pa),
    )


def build_provider_multi_seat_member(
    provider: LowBandPredictionProvider | HybridPredictionProvider,
    *,
    receiver_id: str,
    seat_label: str,
    channel_role: str = 'unknown',
    is_mlp: bool = False,
) -> MultiSeatMember:
    """Project one provider receiver into a 'predicted' multi-seat member.

    ``evidence_type`` is pinned to ``'predicted'`` so the existing mixed-
    evidence warning machinery marks any seat set that blends measured and
    predicted members; the member dataset identity is the exact hash of the
    provider's per-receiver response, never a fabricated measurement id.
    """
    provider.require_observable('frequency_response_magnitude')
    if isinstance(provider, HybridPredictionProvider):
        if receiver_id != provider.receiver_id:
            raise ValueError(
                'hybrid provider receiver identity mismatch'
            )
        entity_id = provider.receiver_identity.receiver_binding.entity_id
        frequency_hz = tuple(
            float(item.frequency_hz)
            for item in provider.absolute_pressure_samples
        )
        magnitude_pa = tuple(
            float(item.magnitude_pa)
            for item in provider.absolute_pressure_samples
        )
        phase_deg = tuple(
            float(item.phase_deg)
            for item in provider.absolute_pressure_samples
        )
        pressure_reference_pa = SPL_REFERENCE_PA
        authority = provider.base_current_authority
    else:
        (
            entity_id,
            _response_entity_id,
            frequency_hz,
            magnitude_pa,
            phase_deg,
            pressure_reference_pa,
        ) = _low_band_seat_payload(provider, receiver_id)
        authority = provider.current_authority
    dataset_sha = _provider_seat_dataset_sha(
        provider.provider_id,
        provider.semantic_sha256,
        receiver_id,
        frequency_hz,
        magnitude_pa,
        phase_deg,
        pressure_reference_pa,
    )
    return MultiSeatMember(
        measurement_id=provider.provider_id,
        dataset_id=f'{provider.provider_id}/{receiver_id}',
        dataset_sha256=dataset_sha,
        target_entity_id=entity_id,
        seat_label=seat_label,
        channel_role=channel_role,
        evidence_type='predicted',
        scene_revision_id=authority.scene_revision_id,
        scene_content_hash=authority.scene_content_hash,
        is_mlp=is_mlp,
        level_reference='20_uPa',
    )


class ProviderSeatMemberBinding(BaseModel):
    """Sealed member→provider reference inside one multi-seat set."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    dataset_id: str = Field(min_length=1)
    provider_ref: PredictionProviderRef | HybridPredictionProviderRef
    receiver_id: str = Field(min_length=1)


class ProviderMultiSeatBinding(BaseModel):
    """Immutable O80 multi-seat binding over predicted members.

    Records, for every ``evidence_type='predicted'`` member, which exact
    provider and receiver the member dataset resolves from, plus the
    capability-gated per-provider consumer bindings. Measured members are
    untouched — they keep their own measurement provenance.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170b-extended-integration-1'
    ] = R170B_EXTENDED_INTEGRATION_AUTHORITY_VERSION
    binding_id: str = Field(
        pattern=r'^r170b-multi-seat-binding:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    set_id: str = Field(min_length=1)
    set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    member_bindings: tuple[ProviderSeatMemberBinding, ...] = ()
    seat_providers: tuple[
        PredictionProviderBinding | HybridPredictionProviderBinding, ...
    ] = ()

    @model_validator(mode='after')
    def validate_binding(self) -> 'ProviderMultiSeatBinding':
        payload = self.model_dump(
            mode='json',
            exclude={'binding_id', 'semantic_sha256'},
        )
        digest = _digest(payload)
        if self.semantic_sha256 != digest:
            raise ValueError('multi-seat provider binding hash mismatch')
        if self.binding_id != f'r170b-multi-seat-binding:{digest}':
            raise ValueError('multi-seat provider binding id mismatch')
        return self


def _provider_seat_binding(
    provider: LowBandPredictionProvider | HybridPredictionProvider,
    analysis_set: MultiSeatAnalysisSet,
) -> PredictionProviderBinding | HybridPredictionProviderBinding:
    if isinstance(provider, HybridPredictionProvider):
        return build_hybrid_provider_binding(
            provider,
            consumer_kind='O80_MULTI_SEAT',
            consumer_id=analysis_set.set_id,
            consumer_semantic_sha256=analysis_set.set_sha256,
            required_observables=('frequency_response_magnitude',),
        )
    return build_prediction_provider_binding(
        provider,
        consumer_kind='O80_MULTI_SEAT',
        consumer_id=analysis_set.set_id,
        consumer_semantic_sha256=analysis_set.set_sha256,
        required_observables=('frequency_response_magnitude',),
    )


def _seat_member_receiver_id(
    member: MultiSeatMember,
    provider: LowBandPredictionProvider | HybridPredictionProvider,
) -> str:
    prefix = f'{provider.provider_id}/'
    if not member.dataset_id.startswith(prefix):
        raise ValueError(
            f'predicted member {member.dataset_id} is not bound to provider '
            f'{provider.provider_id}'
        )
    receiver_id = member.dataset_id[len(prefix):]
    if not receiver_id:
        raise ValueError(
            f'predicted member {member.dataset_id} carries no receiver identity'
        )
    if isinstance(provider, HybridPredictionProvider):
        if receiver_id != provider.receiver_id:
            raise ValueError(
                'predicted member receiver identity does not match the '
                'hybrid provider'
            )
        frequency_hz = tuple(
            float(item.frequency_hz)
            for item in provider.absolute_pressure_samples
        )
        magnitude_pa = tuple(
            float(item.magnitude_pa)
            for item in provider.absolute_pressure_samples
        )
        phase_deg = tuple(
            float(item.phase_deg)
            for item in provider.absolute_pressure_samples
        )
        expected_sha = _provider_seat_dataset_sha(
            provider.provider_id,
            provider.semantic_sha256,
            receiver_id,
            frequency_hz,
            magnitude_pa,
            phase_deg,
            SPL_REFERENCE_PA,
        )
    else:
        (
            _entity_id,
            _response_entity_id,
            frequency_hz,
            magnitude_pa,
            phase_deg,
            pressure_reference_pa,
        ) = _low_band_seat_payload(provider, receiver_id)
        expected_sha = _provider_seat_dataset_sha(
            provider.provider_id,
            provider.semantic_sha256,
            receiver_id,
            frequency_hz,
            magnitude_pa,
            phase_deg,
            pressure_reference_pa,
        )
    if member.dataset_sha256 != expected_sha:
        raise ValueError(
            f'predicted member {member.dataset_id} dataset hash does not '
            'match the bound provider response'
        )
    return receiver_id


def build_provider_multi_seat_binding(
    analysis_set: MultiSeatAnalysisSet,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[str, HybridPredictionProvider] | None = None,
) -> ProviderMultiSeatBinding:
    """Bind every 'predicted' member of a seat set to its provider.

    Fails closed when a predicted member resolves to no provider, when the
    provider's receiver/dataset identity does not reproduce the member's
    recorded dataset hash, or when the provider lacks the magnitude
    observable the seat lane requires.
    """
    low_band_providers = low_band_providers or {}
    hybrid_providers = hybrid_providers or {}
    member_bindings: list[ProviderSeatMemberBinding] = []
    seat_providers: list[
        PredictionProviderBinding | HybridPredictionProviderBinding
    ] = []
    seen: set[str] = set()
    for member in analysis_set.members:
        if member.evidence_type != 'predicted':
            continue
        provider = low_band_providers.get(member.measurement_id)
        if provider is None:
            provider = hybrid_providers.get(member.measurement_id)
        if provider is None:
            raise ValueError(
                f'predicted member {member.dataset_id} has no bound '
                'prediction provider'
            )
        receiver_id = _seat_member_receiver_id(member, provider)
        member_bindings.append(
            ProviderSeatMemberBinding(
                dataset_id=member.dataset_id,
                provider_ref=provider.ref(),
                receiver_id=receiver_id,
            )
        )
        if provider.provider_id in seen:
            continue
        seen.add(provider.provider_id)
        seat_providers.append(
            _provider_seat_binding(provider, analysis_set)
        )
    payload = {
        'authority_version': R170B_EXTENDED_INTEGRATION_AUTHORITY_VERSION,
        'set_id': analysis_set.set_id,
        'set_sha256': analysis_set.set_sha256,
        'member_bindings': [
            item.model_dump(mode='json') for item in member_bindings
        ],
        'seat_providers': [
            item.model_dump(mode='json') for item in seat_providers
        ],
    }
    digest = _digest(payload)
    return ProviderMultiSeatBinding(
        set_id=analysis_set.set_id,
        set_sha256=analysis_set.set_sha256,
        member_bindings=tuple(member_bindings),
        seat_providers=tuple(seat_providers),
        binding_id=f'r170b-multi-seat-binding:{digest}',
        semantic_sha256=digest,
    )


def resolve_provider_seat_responses(
    binding: ProviderMultiSeatBinding,
    analysis_set: MultiSeatAnalysisSet,
    *,
    low_band_providers: Mapping[str, LowBandPredictionProvider] | None = None,
    hybrid_providers: Mapping[str, HybridPredictionProvider] | None = None,
    low_hz: float,
    high_hz: float,
) -> dict[int, FrequencyResponse]:
    """Resolve predicted member responses in member order.

    Returns ``{member_index: FrequencyResponse}`` for the predicted members
    only — measured members keep resolving through their own measurement
    repository. The band-checked typed read is the same one N70 uses.
    """
    if binding.set_sha256 != analysis_set.set_sha256:
        raise ValueError(
            'multi-seat provider binding is stale for this analysis set'
        )
    low_band_providers = low_band_providers or {}
    hybrid_providers = hybrid_providers or {}
    resolved: dict[int, FrequencyResponse] = {}
    for index, member in enumerate(analysis_set.members):
        if member.evidence_type != 'predicted':
            continue
        provider = low_band_providers.get(member.measurement_id)
        if provider is None:
            provider = hybrid_providers.get(member.measurement_id)
        if provider is None:
            raise ValueError(
                f'predicted member {member.dataset_id} has no bound '
                'prediction provider'
            )
        receiver_id = _seat_member_receiver_id(member, provider)
        if isinstance(provider, HybridPredictionProvider):
            resolved[index] = hybrid_provider_frequency_response(
                provider,
                source_entity_id=provider.source_entity_id,
                receiver_id=receiver_id,
                low_hz=low_hz,
                high_hz=high_hz,
            )
        else:
            resolved[index] = provider_frequency_response(
                provider,
                receiver_id=receiver_id,
                low_hz=low_hz,
                high_hz=high_hz,
            )
    return resolved


def provider_radiator_transfer_evidence(
    provider: LowBandPredictionProvider | HybridPredictionProvider,
) -> RadiatorTransferEvidence:
    """Declare the transfer evidence a provider's response can back.

    Only a READY phase capability can back 'complex' element evidence; a
    magnitude-only provider backs 'magnitude_only'; anything else is 'none'.
    """
    if provider.phase_capability == 'READY':
        return 'complex'
    if provider.magnitude_capability == 'READY':
        return 'magnitude_only'
    return 'none'


def bind_provider_to_radiator_model(
    provider: LowBandPredictionProvider | HybridPredictionProvider,
    model: MultiRadiatorSourceModel,
) -> PredictionProviderBinding | HybridPredictionProviderBinding:
    """O80 radiator binding: provider ↔ multi-element source model.

    The provider's source binding must pin the same exact equipment
    definition the radiator model supplements — a provider built from
    another equipment definition can never stand in for this model's
    transfer evidence.
    """
    binding = provider.source_identity.source_binding
    if (
        model.equipment_definition_id != binding.equipment_definition_id
        or model.equipment_definition_version
        != binding.equipment_definition_version
        or model.equipment_definition_sha256
        != binding.equipment_definition_sha256
    ):
        raise ValueError(
            'provider source equipment identity does not match the '
            'multi-radiator model'
        )
    required: tuple[str, ...] = ('frequency_response_magnitude',)
    if provider.phase_capability == 'READY':
        required = (
            'frequency_response_magnitude',
            'frequency_response_phase',
        )
    if isinstance(provider, HybridPredictionProvider):
        return build_hybrid_provider_binding(
            provider,
            consumer_kind='O80_MULTI_RADIATOR',
            consumer_id=model.model_id,
            consumer_semantic_sha256=model.semantic_sha256,
            required_observables=required,
        )
    return build_prediction_provider_binding(
        provider,
        consumer_kind='O80_MULTI_RADIATOR',
        consumer_id=model.model_id,
        consumer_semantic_sha256=model.semantic_sha256,
        required_observables=required,
    )


def bind_provider_to_aim_evaluation(
    provider: LowBandPredictionProvider | HybridPredictionProvider,
    *,
    evaluation_id: str,
    evaluation_sha256: str,
) -> PredictionProviderBinding | HybridPredictionProviderBinding:
    """O80 aim-analysis binding — requires a declared spatial field.

    Aim evaluation needs ``spatial_pressure_field`` coverage over the aim
    directions being compared; every current provider reports it UNSUPPORTED
    (the hybrid lane is a single exact source×receiver pair, the low-band
    lane carries per-receiver point responses), so this binding fails closed
    today rather than admitting a lane that cannot honour the observable.
    """
    required = (
        'frequency_response_magnitude',
        'spatial_pressure_field',
    )
    if isinstance(provider, HybridPredictionProvider):
        return build_hybrid_provider_binding(
            provider,
            consumer_kind='O80_AIM_ANALYSIS',
            consumer_id=evaluation_id,
            consumer_semantic_sha256=evaluation_sha256,
            required_observables=required,
        )
    return build_prediction_provider_binding(
        provider,
        consumer_kind='O80_AIM_ANALYSIS',
        consumer_id=evaluation_id,
        consumer_semantic_sha256=evaluation_sha256,
        required_observables=required,
    )
