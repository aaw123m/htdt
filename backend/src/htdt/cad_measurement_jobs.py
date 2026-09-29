from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
from typing import Any
from uuid import uuid4

from .cad_measurement_models import CadMeasurementRecord


#: Bound on remembered cancelled job ids — generous enough that a
#: completion still in flight after this many cancels is already stale.
CANCELLED_JOB_LIMIT = 4096


@dataclass(frozen=True)
class MeasurementJobToken:
    job_id: str
    sequence: int
    operation_key: str
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    measurement_entity_id: str
    measurement_position_json: str
    external_source_id: str
    query_json: str
    constraint_workspace_hash: str | None = None


@dataclass(frozen=True)
class MeasurementJobApplyContext:
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    # ``None`` marks a constraint-independent read; a constraint-bound token
    # only applies while the constraint workspace hash is unchanged (the same
    # stale-result contract as PredictionJobApplyContext).
    constraint_workspace_hash: str | None = None


class MeasurementJobGuard:
    """Reject cancelled, superseded, or stale external-read completions."""

    def __init__(self) -> None:
        self._sequence = 0
        self._latest_by_operation: dict[str, str] = {}
        # Cancelled job ids must outlive arbitrary late completions, but a
        # session that cancels thousands of jobs cannot retain one set
        # entry per id forever — keep the newest ids, since a completion
        # still pending after that many cancels is beyond any live window.
        self._cancelled: OrderedDict[str, None] = OrderedDict()

    def submit(
        self,
        measurement: CadMeasurementRecord,
        *,
        external_source_id: str,
        query: dict[str, Any],
        operation_key: str = 'rew-read',
        constraint_workspace_hash: str | None = None,
    ) -> MeasurementJobToken:
        if not external_source_id:
            raise ValueError('external_source_id must not be empty')
        if not operation_key:
            raise ValueError('operation_key must not be empty')
        self._sequence += 1
        token = MeasurementJobToken(
            job_id=str(uuid4()),
            sequence=self._sequence,
            operation_key=operation_key,
            document_id=measurement.document_id,
            scene_revision_id=measurement.scene_revision_id,
            scene_content_hash=measurement.scene_content_hash,
            measurement_entity_id=measurement.measurement_entity_id,
            measurement_position_json=measurement.measurement_position.model_dump_json(),
            external_source_id=external_source_id,
            query_json=json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False),
            constraint_workspace_hash=constraint_workspace_hash,
        )
        self._latest_by_operation[operation_key] = token.job_id
        return token

    def cancel(self, token: MeasurementJobToken) -> None:
        self._cancelled[token.job_id] = None
        self._cancelled.move_to_end(token.job_id)
        while len(self._cancelled) > CANCELLED_JOB_LIMIT:
            self._cancelled.popitem(last=False)

    def is_cancelled(self, token: MeasurementJobToken) -> bool:
        return token.job_id in self._cancelled

    def can_apply(self, token: MeasurementJobToken, context: MeasurementJobApplyContext) -> bool:
        if token.job_id in self._cancelled:
            return False
        if self._latest_by_operation.get(token.operation_key) != token.job_id:
            return False
        if context.constraint_workspace_hash != token.constraint_workspace_hash:
            return False
        return (
            context.document_id == token.document_id
            and context.scene_revision_id == token.scene_revision_id
            and context.scene_content_hash == token.scene_content_hash
        )
