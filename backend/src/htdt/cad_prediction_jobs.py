from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from .cad_repository import SceneRevision


#: Bound on remembered cancelled job ids — generous enough that a
#: completion still in flight after this many cancels is already stale.
CANCELLED_JOB_LIMIT = 4096


@dataclass(frozen=True)
class PredictionJobToken:
    job_id: str
    sequence: int
    operation_key: str
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    constraint_workspace_hash: str | None
    model_id: str
    model_version: str
    parameters_json: str
    input_hash: str


@dataclass(frozen=True)
class PredictionJobApplyContext:
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    # ``None`` marks a constraint-independent job; a constraint-bound token
    # only applies while the constraint workspace hash is unchanged (the same
    # stale-result contract as MeasurementJobApplyContext).
    constraint_workspace_hash: str | None = None


class PredictionJobGuard:
    """Reject cancelled, superseded or stale prediction completions."""

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
        revision: SceneRevision,
        *,
        model_id: str,
        model_version: str,
        parameters_json: str,
        input_hash: str,
        operation_key: str = 'prediction',
        constraint_workspace_hash: str | None = None,
    ) -> PredictionJobToken:
        if not model_id or not model_version:
            raise ValueError('model identity must not be empty')
        if not parameters_json or not input_hash:
            raise ValueError('prediction parameters/input hash must not be empty')
        if not operation_key:
            raise ValueError('operation_key must not be empty')
        self._sequence += 1
        token = PredictionJobToken(
            job_id=str(uuid4()),
            sequence=self._sequence,
            operation_key=operation_key,
            document_id=revision.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            constraint_workspace_hash=constraint_workspace_hash,
            model_id=model_id,
            model_version=model_version,
            parameters_json=parameters_json,
            input_hash=input_hash,
        )
        self._latest_by_operation[operation_key] = token.job_id
        return token

    def cancel(self, token: PredictionJobToken) -> None:
        self._cancelled[token.job_id] = None
        self._cancelled.move_to_end(token.job_id)
        while len(self._cancelled) > CANCELLED_JOB_LIMIT:
            self._cancelled.popitem(last=False)

    def is_cancelled(self, token: PredictionJobToken) -> bool:
        return token.job_id in self._cancelled

    def can_apply(self, token: PredictionJobToken, context: PredictionJobApplyContext) -> bool:
        if token.job_id in self._cancelled:
            return False
        if self._latest_by_operation.get(token.operation_key) != token.job_id:
            return False
        if token.constraint_workspace_hash != context.constraint_workspace_hash:
            return False
        return (
            token.document_id == context.document_id
            and token.scene_revision_id == context.scene_revision_id
            and token.scene_content_hash == context.scene_content_hash
        )
