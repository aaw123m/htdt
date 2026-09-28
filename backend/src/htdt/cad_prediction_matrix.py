"""Prediction matrix authority (#531).

A matrix run requests the batch of exact source x seat transfer functions the
product needs for system diagnosis and reuse: one typed spec pins scene and
variant identity, the source axis, the seat/receiver axis, the observable
contract, and the frequency grid. Each cell is an independent authority slot
with an explicit state (QUEUED/RUNNING/READY/CACHED/STALE/FAILED/CANCELLED/
UNSUPPORTED/BLOCKED) — a cell that could not run is never silently faked.

The planner batches receivers under a single source task where the bound
provider supports multi-receiver runs (R170A low-band providers already carry
N receivers per solve), so an M-source x N-receiver matrix requires at most M
provider executions instead of M*N.
"""

from __future__ import annotations

from math import cos, pi, sin
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_serializer,
    model_validator,
)

from .cad_equipment import FrequencyDomain
from .cad_multi_channel_excitation import (
    ScenarioSourceTransfer,
)
from .cad_prediction_provider import (
    LowBandPredictionProvider,
    PredictionProviderRef,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_sha256 as _digest, canonicalize_payload


PREDICTION_MATRIX_SCHEMA_VERSION = 1
PREDICTION_MATRIX_SPEC_AUTHORITY_VERSION = 'prediction-matrix-spec-1'
PREDICTION_MATRIX_RESULT_AUTHORITY_VERSION = 'prediction-matrix-result-1'
PREDICTION_MATRIX_RUN_AUTHORITY_VERSION = 'prediction-matrix-run-1'

MatrixCellState = Literal[
    'QUEUED',
    'RUNNING',
    'READY',
    'CACHED',
    'STALE',
    'FAILED',
    'CANCELLED',
    'UNSUPPORTED',
    'BLOCKED',
]
TERMINAL_CELL_STATES = frozenset(
    {'READY', 'CACHED', 'FAILED', 'CANCELLED', 'UNSUPPORTED'}
)






class MatrixSourceRef(BaseModel):
    """One source column of the matrix, pinned by scene source identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    matrix_source_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    source_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    logical_channel_id: str | None = Field(default=None, min_length=1)


class MatrixReceiverRef(BaseModel):
    """One seat/receiver row of the matrix."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    matrix_receiver_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    receiver_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    seat_label: str | None = Field(default=None, min_length=1)


class MatrixObservableContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: Literal['frequency_response_transfer'] = (
        'frequency_response_transfer'
    )
    frequency_axis_hz: tuple[float, ...] = Field(min_length=2)
    response_unit: Literal['Pa'] = 'Pa'
    level_reference: Literal['20_uPa'] = '20_uPa'
    require_coherent_sum_eligible: bool = False

    @model_validator(mode='after')
    def validate_axis(self) -> 'MatrixObservableContract':
        if len(set(self.frequency_axis_hz)) != len(self.frequency_axis_hz):
            raise ValueError('matrix frequency axis must be unique')
        if tuple(self.frequency_axis_hz) != tuple(sorted(self.frequency_axis_hz)):
            raise ValueError('matrix frequency axis must be sorted')
        if any(float(v) <= 0.0 for v in self.frequency_axis_hz):
            raise ValueError('matrix frequencies must be positive')
        return self


class PredictionMatrixSpec(BaseModel):
    """Immutable batch specification for a source x seat matrix run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PREDICTION_MATRIX_SCHEMA_VERSION
    authority_version: Literal[
        'prediction-matrix-spec-1'
    ] = PREDICTION_MATRIX_SPEC_AUTHORITY_VERSION
    spec_id: str = Field(pattern=r'^prediction-matrix-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str | None = Field(default=None, min_length=1)
    variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    solver_implementation_ref: ExactExternalAuthorityRef
    valid_frequency_domain: FrequencyDomain
    sources: tuple[MatrixSourceRef, ...] = Field(min_length=1)
    receivers: tuple[MatrixReceiverRef, ...] = Field(min_length=1)
    observable_contract: MatrixObservableContract

    @model_validator(mode='after')
    def validate_spec(self) -> 'PredictionMatrixSpec':
        source_ids = [item.matrix_source_id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError('matrix source ids must be unique')
        receiver_ids = [item.matrix_receiver_id for item in self.receivers]
        if len(receiver_ids) != len(set(receiver_ids)):
            raise ValueError('matrix receiver ids must be unique')
        entity_ids = [item.source_entity_id for item in self.sources]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('a source entity may appear only once per matrix')
        if (self.variant_id is None) != (self.variant_sha256 is None):
            raise ValueError('variant id/hash must be supplied together')
        domain = self.valid_frequency_domain
        axis = self.observable_contract.frequency_axis_hz
        if (
            float(axis[0]) < float(domain.minimum_hz)
            or float(axis[-1]) > float(domain.maximum_hz)
        ):
            raise ValueError('matrix frequency axis exceeds the valid domain')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('prediction matrix spec semantic hash mismatch')
        if self.spec_id != f'prediction-matrix-spec:{expected}':
            raise ValueError('prediction matrix spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )


def build_prediction_matrix_spec(**kwargs: Any) -> PredictionMatrixSpec:
    probe = PredictionMatrixSpec.model_construct(**canonicalize_payload(PredictionMatrixSpec, dict(
        spec_id='prediction-matrix-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )))
    digest = _digest(probe.semantic_payload())
    return PredictionMatrixSpec(
        spec_id=f'prediction-matrix-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class MatrixCell(BaseModel):
    """One (source, receiver) authority slot; state is explicit."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    cell_id: str = Field(pattern=r'^matrix-cell:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    matrix_source_id: str = Field(min_length=1)
    matrix_receiver_id: str = Field(min_length=1)
    state: MatrixCellState
    result_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    blocked_reason: str | None = None

    @model_validator(mode='after')
    def validate_cell(self) -> 'MatrixCell':
        ready = self.state in ('READY', 'CACHED')
        if ready != (self.result_sha256 is not None):
            raise ValueError(
                'READY/CACHED cells require a result hash; other states forbid one'
            )
        if self.state in ('UNSUPPORTED', 'BLOCKED', 'FAILED') and not self.blocked_reason:
            raise ValueError('non-runnable terminal cells require an explicit reason')
        if self.state not in ('UNSUPPORTED', 'BLOCKED', 'FAILED') and self.blocked_reason:
            raise ValueError('only UNSUPPORTED/BLOCKED/FAILED cells carry a reason')
        expected = _digest(
            {
                'kind': 'matrix-cell',
                'spec_semantic_sha256': self.spec_semantic_sha256,
                'matrix_source_id': self.matrix_source_id,
                'matrix_receiver_id': self.matrix_receiver_id,
            }
        )
        if self.cell_id != f'matrix-cell:{expected}':
            raise ValueError('matrix cell id mismatch')
        return self


def build_matrix_cell(
    spec: PredictionMatrixSpec,
    *,
    matrix_source_id: str,
    matrix_receiver_id: str,
    state: MatrixCellState = 'QUEUED',
    result_sha256: str | None = None,
    blocked_reason: str | None = None,
) -> MatrixCell:
    source_ids = {item.matrix_source_id for item in spec.sources}
    receiver_ids = {item.matrix_receiver_id for item in spec.receivers}
    if matrix_source_id not in source_ids:
        raise ValueError(f'unknown matrix source: {matrix_source_id}')
    if matrix_receiver_id not in receiver_ids:
        raise ValueError(f'unknown matrix receiver: {matrix_receiver_id}')
    digest = _digest(
        {
            'kind': 'matrix-cell',
            'spec_semantic_sha256': spec.semantic_sha256,
            'matrix_source_id': matrix_source_id,
            'matrix_receiver_id': matrix_receiver_id,
        }
    )
    return MatrixCell(
        cell_id=f'matrix-cell:{digest}',
        spec_semantic_sha256=spec.semantic_sha256,
        matrix_source_id=matrix_source_id,
        matrix_receiver_id=matrix_receiver_id,
        state=state,
        result_sha256=result_sha256,
        blocked_reason=blocked_reason,
    )


class MatrixCellTransfer(BaseModel):
    """Exact per-cell transfer function bound to its matrix coordinates.

    #942: the cell also carries the exact provider/result authority plus the
    coherent-composition semantics (normalization, phasor convention,
    timing) #492 requires — copied arrays alone cannot prove a coherent sum.
    All are serialized only when declared so pre-composition cells keep
    their canonical shape.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    cell_id: str = Field(pattern=r'^matrix-cell:[0-9a-f]{64}$')
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    magnitude_pa: tuple[float, ...] = Field(min_length=2)
    phase_deg: tuple[float, ...] | None = None
    pressure_reference_pa: float = Field(default=20.0e-6, gt=0.0)
    provider_ref: PredictionProviderRef | None = None
    result_authority_ref: ExactExternalAuthorityRef | None = None
    source_normalization_id: str | None = Field(
        default=None, min_length=1
    )
    phasor_convention: str | None = Field(default=None, min_length=1)
    timing_authority: Literal[
        'absolute_propagation_time', 'relative_delay', 'unavailable'
    ] | None = None

    @model_serializer(mode='wrap')
    def _serialize(self, handler):
        data = handler(self)
        for key in (
            'provider_ref',
            'result_authority_ref',
            'source_normalization_id',
            'phasor_convention',
            'timing_authority',
        ):
            if getattr(self, key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode='after')
    def validate_transfer(self) -> 'MatrixCellTransfer':
        count = len(self.frequency_hz)
        if len(self.magnitude_pa) != count:
            raise ValueError('cell transfer magnitude axis mismatch')
        if self.phase_deg is not None and len(self.phase_deg) != count:
            raise ValueError('cell transfer phase axis mismatch')
        return self


class TransferMatrixResultSet(BaseModel):
    """Row-major (receiver-major) result set over the spec's cells."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PREDICTION_MATRIX_SCHEMA_VERSION
    authority_version: Literal[
        'prediction-matrix-result-1'
    ] = PREDICTION_MATRIX_RESULT_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^prediction-matrix-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^prediction-matrix-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    cells: tuple[MatrixCell, ...] = Field(min_length=1)
    transfers: tuple[MatrixCellTransfer, ...] = ()
    coherent_sum_eligible: bool
    # #942: typed reasons coherent composition is unavailable; present only
    # when non-empty so legacy result digests are preserved.
    coherent_compatibility_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_result(self) -> 'TransferMatrixResultSet':
        cell_ids = [item.cell_id for item in self.cells]
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError('matrix result cells must be unique')
        transfer_ids = [item.cell_id for item in self.transfers]
        if len(transfer_ids) != len(set(transfer_ids)):
            raise ValueError('matrix result transfers must be unique per cell')
        ready_ids = {
            item.cell_id
            for item in self.cells
            if item.state in ('READY', 'CACHED')
        }
        if set(transfer_ids) != ready_ids:
            raise ValueError(
                'transfers must exist exactly for READY/CACHED cells'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('prediction matrix result semantic hash mismatch')
        if self.result_id != f'prediction-matrix-result:{expected}':
            raise ValueError('prediction matrix result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )
        if not self.coherent_compatibility_reasons:
            payload.pop('coherent_compatibility_reasons', None)
        return payload

    def cell(self, matrix_source_id: str, matrix_receiver_id: str) -> MatrixCell:
        for item in self.cells:
            if (
                item.matrix_source_id == matrix_source_id
                and item.matrix_receiver_id == matrix_receiver_id
            ):
                return item
        raise ValueError('matrix cell coordinates not in result set')

    def transfer(self, matrix_source_id: str, matrix_receiver_id: str) -> MatrixCellTransfer:
        return self.transfer_for_cell(
            self.cell(matrix_source_id, matrix_receiver_id).cell_id
        )

    def transfer_for_cell(self, cell_id: str) -> MatrixCellTransfer:
        for item in self.transfers:
            if item.cell_id == cell_id:
                return item
        raise ValueError(f'no transfer for cell: {cell_id}')


class MatrixProviderPlan(BaseModel):
    """Batched provider execution plan: one task per source column."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    batched_tasks: tuple[tuple[str, tuple[str, ...]], ...] = Field(min_length=1)
    cell_ids: tuple[str, ...] = Field(min_length=1)
    batching_semantics: Literal[
        'provider_multi_receiver_batch'
    ] = 'provider_multi_receiver_batch'


def plan_matrix_execution(
    spec: PredictionMatrixSpec,
) -> MatrixProviderPlan:
    """Group all receivers under each source: at most M provider tasks."""
    tasks = tuple(
        (source.matrix_source_id, tuple(
            receiver.matrix_receiver_id for receiver in spec.receivers
        ))
        for source in spec.sources
    )
    cells = tuple(
        build_matrix_cell(
            spec,
            matrix_source_id=source.matrix_source_id,
            matrix_receiver_id=receiver.matrix_receiver_id,
        )
        for source in spec.sources
        for receiver in spec.receivers
    )
    return MatrixProviderPlan(
        spec_semantic_sha256=spec.semantic_sha256,
        batched_tasks=tasks,
        cell_ids=tuple(item.cell_id for item in cells),
    )


def collect_matrix_results(
    spec: PredictionMatrixSpec,
    providers: dict[str, LowBandPredictionProvider],
    *,
    cached_result_sha256: dict[tuple[str, str], str] | None = None,
) -> TransferMatrixResultSet:
    """Assemble the result set from one multi-receiver provider per source.

    Providers must pin the same scene snapshot + solver implementation as the
    spec and cover every requested receiver; otherwise affected cells become
    BLOCKED/UNSUPPORTED rather than silently empty.
    """
    cached_result_sha256 = cached_result_sha256 or {}
    cells: list[MatrixCell] = []
    transfers: list[MatrixCellTransfer] = []
    coherent_ok = True
    for source in spec.sources:
        provider = providers.get(source.matrix_source_id)
        if provider is None:
            for receiver in spec.receivers:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='BLOCKED',
                        blocked_reason='no provider run bound for this matrix source',
                    )
                )
            coherent_ok = False
            continue
        if (
            provider.current_authority.scene_content_hash
            != spec.scene_content_hash
            or provider.current_authority.acoustic_scene_snapshot_sha256
            != spec.acoustic_scene_snapshot_sha256
            or provider.current_authority.solver_implementation_ref
            != spec.solver_implementation_ref
        ):
            for receiver in spec.receivers:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='BLOCKED',
                        blocked_reason='provider authority does not match matrix spec',
                    )
                )
            coherent_ok = False
            continue
        capability = provider.capability('frequency_response_magnitude')
        if capability.state != 'READY':
            for receiver in spec.receivers:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='UNSUPPORTED',
                        blocked_reason=capability.reason
                        or 'provider observable unsupported',
                    )
                )
            coherent_ok = False
            continue
        phase_ok = provider.phase_capability == 'READY'
        if not phase_ok:
            coherent_ok = False
        for receiver in spec.receivers:
            response = next(
                (
                    item
                    for item in provider.receiver_responses
                    if item.receiver_id == receiver.receiver_id
                ),
                None,
            )
            if response is None:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='BLOCKED',
                        blocked_reason='provider run does not cover receiver',
                    )
                )
                continue
            if tuple(response.frequency_hz) != tuple(
                spec.observable_contract.frequency_axis_hz
            ):
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='BLOCKED',
                        blocked_reason=(
                            'provider response frequency grid does not match '
                            'the matrix observable contract'
                        ),
                    )
                )
                continue
            result_sha = _digest(
                {
                    'kind': 'matrix-cell-transfer',
                    'spec_semantic_sha256': spec.semantic_sha256,
                    'provider_id': provider.provider_id,
                    'receiver_id': receiver.receiver_id,
                    'frequency_hz': list(response.frequency_hz),
                    'magnitude_pa': list(response.magnitude_pa),
                    'phase_deg': (
                        list(response.phase_deg)
                        if response.phase_deg is not None
                        else None
                    ),
                    'pressure_reference_pa': float(response.pressure_reference_pa),
                }
            )
            cell_key = (source.matrix_source_id, receiver.matrix_receiver_id)
            if cell_key in cached_result_sha256:
                cached_sha = cached_result_sha256[cell_key]
                if cached_sha != result_sha:
                    raise ValueError(
                        'cached result hash does not match the result '
                        'authority of cell '
                        f'{source.matrix_source_id}x{receiver.matrix_receiver_id}'
                    )
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='CACHED',
                        result_sha256=result_sha,
                    )
                )
                transfers.append(
                    MatrixCellTransfer(
                        cell_id=cells[-1].cell_id,
                        frequency_hz=response.frequency_hz,
                        magnitude_pa=response.magnitude_pa,
                        phase_deg=response.phase_deg,
                        pressure_reference_pa=response.pressure_reference_pa,
                        provider_ref=provider.ref(),
                        result_authority_ref=provider.result_artifact_ref,
                        source_normalization_id=(
                            provider.source_normalization_id
                        ),
                        phasor_convention=response.phase_convention,
                        timing_authority=provider.timing_authority,
                    )
                )
                continue
            cells.append(
                build_matrix_cell(
                    spec,
                    matrix_source_id=source.matrix_source_id,
                    matrix_receiver_id=receiver.matrix_receiver_id,
                    state='READY',
                    result_sha256=result_sha,
                )
            )
            transfers.append(
                MatrixCellTransfer(
                    cell_id=cells[-1].cell_id,
                    frequency_hz=response.frequency_hz,
                    magnitude_pa=response.magnitude_pa,
                    phase_deg=response.phase_deg,
                    pressure_reference_pa=response.pressure_reference_pa,
                    provider_ref=provider.ref(),
                    result_authority_ref=provider.result_artifact_ref,
                    source_normalization_id=provider.source_normalization_id,
                    phasor_convention=response.phase_convention,
                    timing_authority=provider.timing_authority,
                )
            )
    # #942: phase availability alone cannot prove coherent-sum eligibility —
    # timing, normalization and phasor compatibility are machine-checked.
    coherence_reasons = assess_matrix_coherent_compatibility(spec, providers)
    coherent_ok = coherent_ok and not coherence_reasons
    eligible = coherent_ok and spec.observable_contract.require_coherent_sum_eligible
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


def assess_matrix_coherent_compatibility(
    spec: PredictionMatrixSpec,
    providers: dict[str, LowBandPredictionProvider],
) -> tuple[str, ...]:
    """Machine-checked coherent-composition compatibility (#942).

    Returns typed reasons coherent composition is unavailable; an empty
    tuple means every participating source carries the declared semantics
    #492 requires — common frequency grid, complex pressure with a common
    phasor convention, a common explicit source normalization, and declared
    timing authority — on top of the per-cell authority checks
    ``collect_matrix_results`` already performs.
    """

    reasons: list[str] = []
    ready_providers: list[tuple[str, LowBandPredictionProvider]] = []
    for source in spec.sources:
        provider = providers.get(source.matrix_source_id)
        if provider is None:
            reasons.append(
                f'source {source.source_entity_id} has no bound provider run'
            )
            continue
        if (
            provider.current_authority.scene_content_hash
            != spec.scene_content_hash
            or provider.current_authority.acoustic_scene_snapshot_sha256
            != spec.acoustic_scene_snapshot_sha256
            or provider.current_authority.solver_implementation_ref
            != spec.solver_implementation_ref
        ):
            reasons.append(
                f'source {source.source_entity_id} provider authority does '
                'not match the matrix spec'
            )
            continue
        ready_providers.append((source.source_entity_id, provider))

    requested_receivers = tuple(
        receiver.receiver_id for receiver in spec.receivers
    )
    for entity_id, provider in ready_providers:
        if provider.phase_capability != 'READY':
            reasons.append(
                f'source {entity_id} has no complex-pressure capability'
            )
            continue
        responses = {
            item.receiver_id: item for item in provider.receiver_responses
        }
        for receiver_id in requested_receivers:
            response = responses.get(receiver_id)
            if response is None:
                reasons.append(
                    f'source {entity_id} does not cover receiver '
                    f'{receiver_id}'
                )
                continue
            if response.phase_deg is None:
                reasons.append(
                    f'source {entity_id} receiver {receiver_id} carries '
                    'magnitude-only data; it cannot enter a coherent sum'
                )

    if not ready_providers:
        return tuple(sorted(set(reasons))) or (
            'no coherent-compatible sources were collected',
        )

    grid_variants = {
        tuple(item.frequency_hz)
        for _entity, provider in ready_providers
        for item in provider.receiver_responses
        if item.receiver_id in requested_receivers
    }
    if len(grid_variants) > 1:
        reasons.append(
            'sources do not share a common frequency grid'
        )

    conventions = {
        provider.receiver_responses[0].phase_convention
        for _entity, provider in ready_providers
        if provider.receiver_responses
    }
    if len(conventions) > 1:
        reasons.append(
            'sources declare different phasor conventions: '
            + ','.join(sorted(conventions))
        )

    normalizations = {
        provider.source_normalization_id
        for _entity, provider in ready_providers
    }
    if None in normalizations:
        reasons.append(
            'at least one source has no declared source normalization'
        )
    elif len(normalizations) > 1:
        reasons.append(
            'sources declare different source normalizations: '
            + ','.join(sorted(item for item in normalizations if item))
        )

    for entity_id, provider in ready_providers:
        if provider.timing_authority in (None, 'unavailable'):
            reasons.append(
                f'source {entity_id} lacks declared timing authority'
            )

    return tuple(sorted(set(reasons)))


def matrix_scenario_source_transfers(
    result_set: TransferMatrixResultSet,
    spec: PredictionMatrixSpec,
    matrix_receiver_id: str,
) -> tuple[ScenarioSourceTransfer, ...]:
    """Canonical Matrix → #492 adapter (#942).

    Materializes ``ScenarioSourceTransfer`` entries for one receiver row
    from the exact per-cell authorities the matrix preserved — no caller-
    invented metadata. Fails closed on cells that lack complex data or the
    declared normalization/phasor semantics #492 requires.
    """

    if result_set.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('result set does not belong to the supplied spec')
    receiver_ids = {
        item.matrix_receiver_id for item in spec.receivers
    }
    if matrix_receiver_id not in receiver_ids:
        raise ValueError(f'unknown matrix receiver: {matrix_receiver_id}')

    source_by_id = {item.matrix_source_id: item for item in spec.sources}
    transfers: list[ScenarioSourceTransfer] = []
    for cell in result_set.cells:
        if cell.matrix_receiver_id != matrix_receiver_id:
            continue
        if cell.state not in ('READY', 'CACHED'):
            raise ValueError(
                f'cell {cell.cell_id} is {cell.state}; only READY/CACHED '
                'cells can materialize a scenario transfer'
            )
        transfer = result_set.transfer_for_cell(cell.cell_id)
        source = source_by_id[cell.matrix_source_id]
        if transfer.phase_deg is None:
            raise ValueError(
                f'cell {cell.cell_id} carries magnitude-only data'
            )
        if transfer.source_normalization_id is None:
            raise ValueError(
                f'cell {cell.cell_id} declares no source normalization'
            )
        if transfer.phasor_convention is None:
            raise ValueError(
                f'cell {cell.cell_id} declares no phasor convention'
            )
        real: list[float] = []
        imag: list[float] = []
        for magnitude, phase in zip(
            transfer.magnitude_pa, transfer.phase_deg, strict=True
        ):
            radians = phase * pi / 180.0
            real.append(magnitude * cos(radians))
            imag.append(magnitude * sin(radians))
        transfers.append(
            ScenarioSourceTransfer(
                source_entity_id=source.source_entity_id,
                frequency_hz=transfer.frequency_hz,
                pressure_real=tuple(real),
                pressure_imag=tuple(imag),
                phasor_convention=transfer.phasor_convention,
                source_normalization_id=transfer.source_normalization_id,
                timing_authority=(
                    'unavailable'
                    if transfer.timing_authority is None
                    else transfer.timing_authority
                ),
                result_authority_ref=transfer.result_authority_ref,
            )
        )
    return tuple(transfers)


MatrixRunState = Literal[
    'QUEUED',
    'RUNNING',
    'READY',
    'BLOCKED',
    'FAILED',
    'CANCELLED',
]


class MatrixExecutionRun(BaseModel):
    """One persisted execution attempt for a PredictionMatrixSpec (#986).

    The run record is the replayable evidence that a matrix was actually
    executed: every attempt carries its own semantic identity, terminal
    state, per-cell state snapshot, and the result set it produced.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PREDICTION_MATRIX_SCHEMA_VERSION
    authority_version: Literal[
        'prediction-matrix-run-1'
    ] = PREDICTION_MATRIX_RUN_AUTHORITY_VERSION
    run_id: str = Field(pattern=r'^prediction-matrix-run:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^prediction-matrix-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    attempt: int = Field(ge=1)
    state: MatrixRunState
    result_set_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    cell_state_counts: dict[str, int] = Field(default_factory=dict)
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str | None = Field(default=None, min_length=1)
    failure_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_run(self) -> 'MatrixExecutionRun':
        if self.state in ('READY', 'BLOCKED') and (
            self.result_set_sha256 is None
        ):
            raise ValueError(
                'terminal matrix run requires the result set it produced'
            )
        if self.state in ('QUEUED', 'RUNNING') and (
            self.result_set_sha256 is not None
        ):
            raise ValueError(
                'pending matrix run cannot carry a result set hash'
            )
        if self.state == 'FAILED' and not self.failure_reason:
            raise ValueError('FAILED matrix run requires a reason')
        if self.state != 'FAILED' and self.failure_reason:
            raise ValueError('only FAILED matrix runs carry a reason')
        if self.state in ('READY', 'BLOCKED', 'FAILED', 'CANCELLED') and (
            self.finished_at_utc is None
        ):
            raise ValueError('terminal matrix run requires finished_at_utc')
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('matrix run semantic hash mismatch')
        if self.run_id != f'prediction-matrix-run:{digest}':
            raise ValueError('matrix run id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'run_id', 'semantic_sha256'},
        )


def build_matrix_execution_run(
    *,
    spec: PredictionMatrixSpec,
    attempt: int,
    state: MatrixRunState,
    started_at_utc: str,
    finished_at_utc: str | None = None,
    result_set: TransferMatrixResultSet | None = None,
    cell_state_counts: dict[str, int] | None = None,
    failure_reason: str | None = None,
) -> MatrixExecutionRun:
    counts = dict(cell_state_counts or {})
    if result_set is not None:
        for cell in result_set.cells:
            counts[cell.state] = counts.get(cell.state, 0) + 1
    payload: dict[str, Any] = {
        'schema_version': PREDICTION_MATRIX_SCHEMA_VERSION,
        'authority_version': PREDICTION_MATRIX_RUN_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'attempt': int(attempt),
        'state': state,
        'result_set_sha256': (
            None if result_set is None else result_set.semantic_sha256
        ),
        'cell_state_counts': counts,
        'started_at_utc': started_at_utc,
        'finished_at_utc': finished_at_utc,
        'failure_reason': failure_reason,
    }
    digest = _digest(payload)
    return MatrixExecutionRun(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        attempt=int(attempt),
        state=state,
        result_set_sha256=(
            None if result_set is None else result_set.semantic_sha256
        ),
        cell_state_counts=counts,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        failure_reason=failure_reason,
        run_id=f'prediction-matrix-run:{digest}',
        semantic_sha256=digest,
    )


def cached_result_sha256_map(
    result_set: TransferMatrixResultSet,
) -> dict[tuple[str, str], str]:
    """Exact per-cell reuse map for re-runs of the same spec."""
    return {
        (cell.matrix_source_id, cell.matrix_receiver_id): cell.result_sha256
        for cell in result_set.cells
        if cell.state in ('READY', 'CACHED')
        and cell.result_sha256 is not None
    }


def execute_prediction_matrix(
    spec: PredictionMatrixSpec,
    providers: dict[str, LowBandPredictionProvider],
    *,
    repository=None,
    attempt: int | None = None,
    started_at_utc: str,
    finished_at_utc: str | None = None,
) -> MatrixExecutionRun:
    """Drive one matrix execution and persist spec/run/result set (#986).

    Cells whose prior persisted result already carries the exact result
    authority (same spec, same provider output identity) are marked CACHED
    and reused verbatim — nothing is recomputed or fabricated.
    """
    if repository is not None:
        repository.save_spec(spec)
    plan = plan_matrix_execution(spec)
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
        result_set = collect_matrix_results(
            spec,
            providers,
            cached_result_sha256=cached,
        )
    except Exception as exc:
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


class MatrixCurrency(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    spec_id: str = Field(min_length=1)
    state: Literal['CURRENT', 'STALE']
    stale_reasons: tuple[str, ...]
    stale_cell_ids: tuple[str, ...]

    @model_validator(mode='after')
    def validate_state(self) -> 'MatrixCurrency':
        if self.state == 'CURRENT' and (self.stale_reasons or self.stale_cell_ids):
            raise ValueError('CURRENT matrix cannot carry stale detail')
        if self.state == 'STALE' and not self.stale_reasons:
            raise ValueError('STALE matrix requires reasons')
        return self


def assess_matrix_currency(
    spec: PredictionMatrixSpec,
    result_set: TransferMatrixResultSet,
    *,
    current_scene_content_hash: str,
    current_snapshot_sha256: str,
    current_source_bindings: dict[str, str] | None = None,
    current_receiver_bindings: dict[str, str] | None = None,
) -> MatrixCurrency:
    """Dependency-aware currency assessment (#986).

    Shared dependencies (scene content, acoustic snapshot, solver) stale the
    whole matrix; a changed source binding stales only that source's column,
    a changed receiver binding only that receiver's row. Exact cells keep
    running until their own pinned dependency moves.
    """
    if result_set.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('result set does not belong to the supplied spec')
    shared_reasons: list[str] = []
    if spec.scene_content_hash != current_scene_content_hash:
        shared_reasons.append('scene content changed since the matrix ran')
    if spec.acoustic_scene_snapshot_sha256 != current_snapshot_sha256:
        shared_reasons.append(
            'acoustic scene snapshot changed since the matrix ran'
        )

    stale_cells: list[str] = []
    reasons = list(shared_reasons)
    if shared_reasons:
        stale_cells = [item.cell_id for item in result_set.cells]
    else:
        source_bindings = dict(current_source_bindings or {})
        receiver_bindings = dict(current_receiver_bindings or {})
        stale_sources: set[str] = set()
        stale_receivers: set[str] = set()
        if source_bindings:
            for source in spec.sources:
                current = source_bindings.get(
                    source.source_entity_id,
                    source_bindings.get(source.matrix_source_id),
                )
                if (
                    current is not None
                    and current != source.source_binding_sha256
                ):
                    stale_sources.add(source.matrix_source_id)
            if stale_sources:
                reasons.append(
                    'matrix source binding changed for: '
                    + ', '.join(sorted(stale_sources))
                )
        if receiver_bindings:
            for receiver in spec.receivers:
                current = receiver_bindings.get(
                    receiver.receiver_entity_id,
                    receiver_bindings.get(receiver.matrix_receiver_id),
                )
                if (
                    current is not None
                    and current != receiver.receiver_binding_sha256
                ):
                    stale_receivers.add(receiver.matrix_receiver_id)
            if stale_receivers:
                reasons.append(
                    'matrix receiver binding changed for: '
                    + ', '.join(sorted(stale_receivers))
                )
        for cell in result_set.cells:
            if (
                cell.matrix_source_id in stale_sources
                or cell.matrix_receiver_id in stale_receivers
            ):
                stale_cells.append(cell.cell_id)

    return MatrixCurrency(
        spec_id=spec.spec_id,
        state='STALE' if reasons else 'CURRENT',
        stale_reasons=tuple(reasons),
        stale_cell_ids=tuple(stale_cells),
    )
