"""Device-verification automation authority — derive runner plans
from issue-gate requirements and record closure verdicts.

Issues whose closure needs real-device verification get a sealed
`VerificationRequirement`: which acquisition cells the gate needs
(channel role × source × target × repeats) and which evaluator
decides. The automation derives a MeasurementRunnerPlan from that
declaration — the manual step is reduced to executing the generated
checklist — and a `VerificationClosure` binds the committed cells
and evaluator verdict so closure is evidence-backed, never
asserted.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


class RequiredCell(BaseModel):
    """One acquisition cell a gate needs — mirrors the runner's
    cell spec so a plan can be derived mechanically."""

    model_config = ConfigDict(frozen=True)

    channel_role: str
    source_speaker_ids: tuple[str, ...]
    target_entity_id: str
    repeats: int = 1
    purpose: str = 'measurement'

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('channel_role'):
                raise ValueError('a required cell needs a channel role')
            if not data.get('source_speaker_ids'):
                raise ValueError('a required cell needs source speakers')
            if not data.get('target_entity_id'):
                raise ValueError('a required cell needs a target')
            if data.get('repeats', 1) < 1:
                raise ValueError('repeats must be >= 1')
        return data


class VerificationRequirement(BaseModel):
    """Sealed declaration of the device evidence an issue gate
    needs: the required cells plus the deciding evaluator."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    issue_ref: str
    gate_label: str
    required_cells: tuple[RequiredCell, ...]
    evaluator_kind: Literal[
        'pytest_check', 'authority_evaluator', 'manual_review',
        'unknown',
    ]
    evaluator_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('issue_ref'):
                raise ValueError('a requirement needs its issue ref')
            if not data.get('required_cells'):
                raise ValueError(
                    'a requirement needs at least one required '
                    'cell — else the gate needs no device run'
                )
            if data.get('evaluator_kind') not in (
                'pytest_check', 'authority_evaluator',
                'manual_review', 'unknown',
            ):
                raise ValueError('unknown evaluator kind')
            if data.get('evaluator_kind') == 'unknown':
                raise ValueError(
                    'declare how the gate is decided'
                )
            if data.get('evaluator_kind') == 'authority_evaluator' and (
                data.get('evaluator_ref') is None
            ):
                raise ValueError(
                    'an authority evaluator gate needs its '
                    'evaluator ref'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'requirement_id', 'requirement_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'VerificationRequirement':
        return _seal(
            cls, payload, 'requirement_id', 'requirement_sha256', 'vrq'
        )


class VerificationClosure(BaseModel):
    """Sealed closure record — which plan ran, which cells bound
    evidence, the evaluator verdict; a gate only closes when the
    evidence it required is actually committed."""

    model_config = ConfigDict(frozen=True)

    closure_id: str
    closure_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    requirement_ref: AuthorityRef
    runner_plan_ref: AuthorityRef | None = None
    committed_cell_count: int | None = None
    bound_evidence_refs: tuple[AuthorityRef, ...] = ()
    evaluator_verdict: Literal[
        'passed', 'failed', 'inconclusive', 'unknown',
    ]
    closed: bool = False

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('requirement_ref') is None:
                raise ValueError('a closure needs its requirement ref')
            if data.get('evaluator_verdict') not in (
                'passed', 'failed', 'inconclusive', 'unknown',
            ):
                raise ValueError('unknown evaluator verdict')
            if data.get('closed') and (
                data.get('evaluator_verdict') != 'passed'
            ):
                raise ValueError(
                    'a gate only closes on a passed verdict — '
                    'failed/inconclusive stays open'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'closure_id', 'closure_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'VerificationClosure':
        return _seal(
            cls, payload, 'closure_id', 'closure_sha256', 'vcl'
        )


VerificationVerdict = Literal[
    'closable',
    'plan_underived',
    'cells_incomplete',
    'evidence_unbound',
    'verdict_not_passed',
]


def evaluate_verification_claim(
    requirement: VerificationRequirement | None,
    closure: VerificationClosure | None,
) -> tuple[VerificationVerdict, str]:
    """Judge whether a device-gated issue may close."""
    if requirement is None:
        return (
            'plan_underived',
            'no verification requirement — nothing derives a plan',
        )
    if closure is None:
        return (
            'cells_incomplete',
            'requirement pinned but no closure record — the '
            'derived checklist has not run',
        )
    if closure.runner_plan_ref is None:
        return (
            'plan_underived',
            'closure lacks the runner-plan ref',
        )
    needed = sum(c.repeats for c in requirement.required_cells)
    if (
        closure.committed_cell_count is None
        or closure.committed_cell_count < needed
    ):
        return (
            'cells_incomplete',
            f'{closure.committed_cell_count or 0}/{needed} '
            'required cells committed',
        )
    if not closure.bound_evidence_refs:
        return (
            'evidence_unbound',
            'cells committed but no evidence refs bound',
        )
    if closure.evaluator_verdict != 'passed' or not closure.closed:
        return (
            'verdict_not_passed',
            'evaluator did not pass / closure flag not set — '
            'issue stays open',
        )
    return (
        'closable',
        'required evidence committed and evaluator passed',
    )


VERIFICATION_LABELS: dict[str, str] = {
    'closable': 'クローズ可能',
    'plan_underived': '計画未生成',
    'cells_incomplete': 'セル未完了',
    'evidence_unbound': '証拠未束縛',
    'verdict_not_passed': '判定未合格',
}
