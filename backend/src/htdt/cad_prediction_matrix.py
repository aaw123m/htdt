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

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .cad_prediction_provider import (
    LowBandPredictionProvider,
    PredictionProviderReceiverIdentity,
    PredictionProviderSourceIdentity,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


PREDICTION_MATRIX_SCHEMA_VERSION = 1
PREDICTION_MATRIX_SPEC_AUTHORITY_VERSION = 'prediction-matrix-spec-1'
PREDICTION_MATRIX_RESULT_AUTHORITY_VERSION = 'prediction-matrix-result-1'

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


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


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
    probe = PredictionMatrixSpec.model_construct(
        spec_id='prediction-matrix-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
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
    """Exact per-cell transfer function bound to its matrix coordinates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    cell_id: str = Field(pattern=r'^matrix-cell:[0-9a-f]{64}$')
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    magnitude_pa: tuple[float, ...] = Field(min_length=2)
    phase_deg: tuple[float, ...] | None = None
    pressure_reference_pa: float = Field(default=20.0e-6, gt=0.0)

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
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )

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
            cell_key = (source.matrix_source_id, receiver.matrix_receiver_id)
            if cell_key in cached_result_sha256:
                cells.append(
                    build_matrix_cell(
                        spec,
                        matrix_source_id=source.matrix_source_id,
                        matrix_receiver_id=receiver.matrix_receiver_id,
                        state='CACHED',
                        result_sha256=cached_result_sha256[cell_key],
                    )
                )
                transfers.append(
                    MatrixCellTransfer(
                        cell_id=cells[-1].cell_id,
                        frequency_hz=response.frequency_hz,
                        magnitude_pa=response.magnitude_pa,
                        phase_deg=response.phase_deg,
                        pressure_reference_pa=response.pressure_reference_pa,
                    )
                )
                continue
            result_sha = _digest(
                {
                    'kind': 'matrix-cell-transfer',
                    'spec_semantic_sha256': spec.semantic_sha256,
                    'provider_id': provider.provider_id,
                    'receiver_id': receiver.receiver_id,
                    'magnitude_pa': list(response.magnitude_pa),
                    'phase_deg': (
                        list(response.phase_deg)
                        if response.phase_deg is not None
                        else None
                    ),
                }
            )
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
                )
            )
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
    digest = _digest(payload)
    return TransferMatrixResultSet(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        cells=tuple(cells),
        transfers=tuple(transfers),
        coherent_sum_eligible=eligible,
        result_id=f'prediction-matrix-result:{digest}',
        semantic_sha256=digest,
    )


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
) -> MatrixCurrency:
    """A matrix goes stale when the pinned scene or snapshot moves."""
    if result_set.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('result set does not belong to the supplied spec')
    reasons: list[str] = []
    if spec.scene_content_hash != current_scene_content_hash:
        reasons.append('scene content changed since the matrix ran')
    if spec.acoustic_scene_snapshot_sha256 != current_snapshot_sha256:
        reasons.append('acoustic scene snapshot changed since the matrix ran')
    stale_cells = (
        tuple(item.cell_id for item in result_set.cells)
        if reasons
        else ()
    )
    return MatrixCurrency(
        spec_id=spec.spec_id,
        state='STALE' if reasons else 'CURRENT',
        stale_reasons=tuple(reasons),
        stale_cell_ids=stale_cells,
    )
