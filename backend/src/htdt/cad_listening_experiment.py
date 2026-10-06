"""Subjective listening-experiment authority (issue #696).

Storing an ABX result or subjective rating is not enough to make a
listening experiment methodologically valid, reproducible or
statistically interpretable. ITU-R families differ: BS.1116-3 (small
impairments), BS.1534-3 MUSHRA (intermediate quality), BS.1284-2
(general methodology — in-force 2019 edition; a draft revision exists
but drafts are not production profiles).

Basis: ITU-R BS.1116-3 (2015), BS.1534-3 (2015), BS.1284-2 (2019).
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


_METHOD_KINDS = (
    'bs1116_3', 'mushra_bs1534_3', 'bs1284_2', 'abx',
    'paired_comparison', 'other', 'unknown',
)

ExperimentVerdict = Literal[
    'qualified_experiment',
    'unqualified_listeners',
    'unrandomized_trials',
    'method_mismatch',
    'insufficient_trials',
    'unverified_result',
]


class ListeningExperimentPlan(BaseModel):
    """Declared subjective experiment plan (#696) — method family,
    impairment regime and trial statistics. A 'listening test' claim
    must pin its method family; an unknown method is not a claim."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method_kind: Literal[
        'bs1116_3', 'mushra_bs1534_3', 'bs1284_2', 'abx',
        'paired_comparison', 'other', 'unknown',
    ]
    impairment_regime: Literal[
        'small_impairment', 'intermediate_quality', 'large_difference',
        'other', 'unknown',
    ] = 'unknown'
    listener_count: int | None = None
    trials_per_listener: int | None = None
    randomization_plan_ref: AuthorityRef | None = None
    stimulus_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('method_kind') not in _METHOD_KINDS:
                raise ValueError('unknown experiment method')
            if data.get('method_kind') == 'unknown':
                raise ValueError(
                    'an experiment plan must declare its method '
                    'family — an unclaimed method is not a claim'
                )
            regime = data.get('impairment_regime')
            if regime == 'small_impairment' and (
                data.get('method_kind') == 'mushra_bs1534_3'
            ):
                raise ValueError(
                    'MUSHRA is for intermediate quality, not small '
                    'impairments (BS.1116 regime)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ListeningExperimentPlan':
        return _seal(
            cls, payload, 'plan_id', 'plan_sha256', 'lexp'
        )


class ListenerQualification(BaseModel):
    """Panel qualification evidence (#696) — screening, training and
    test-retest quality of the listeners actually used."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    screening_ref: AuthorityRef | None = None
    training_completed: bool = False
    test_retest_ref: AuthorityRef | None = None
    panel_size: int | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            panel = data.get('panel_size')
            if panel is not None and panel < 1:
                raise ValueError('panel size must be positive')
        return data

    @property
    def qualified(self) -> bool:
        return (
            self.screening_ref is not None
            and self.training_completed
            and self.test_retest_ref is not None
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ListenerQualification':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256',
            'lqual',
        )


class SubjectiveInferenceRecord(BaseModel):
    """One inference drawn from a listening experiment (#696) —
    statistical honesty: effect, interval, power and whether the
    verdict is sealed to the recorded analysis."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    plan_ref: AuthorityRef
    qualification_ref: AuthorityRef | None = None
    effect_estimate: float | None = None
    confidence_interval: tuple[float, float] | None = None
    statistical_power: float | None = None
    verdict: Literal[
        'significant', 'not_significant', 'underpowered',
        'inconclusive',
    ]

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('plan_ref') is None:
                raise ValueError(
                    'an inference requires a pinned experiment plan'
                )
            if data.get('verdict') == 'significant' and (
                data.get('confidence_interval') is None
            ):
                raise ValueError(
                    'a significant verdict requires a confidence '
                    'interval'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'SubjectiveInferenceRecord':
        return _seal(
            cls, payload, 'record_id', 'record_sha256', 'sinf'
        )


def evaluate_experiment_claim(
    plan: ListeningExperimentPlan | None,
    qualification: ListenerQualification | None,
    inference: SubjectiveInferenceRecord | None,
) -> tuple[ExperimentVerdict, str]:
    """Judge a listening-experiment claim (#696)."""
    if plan is None:
        return (
            'unverified_result',
            'no pinned experiment plan — ratings without a method '
            'family are not evidence',
        )
    if plan.randomization_plan_ref is None:
        return (
            'unrandomized_trials',
            'no pinned randomization plan — ordering effects are '
            'uncontrolled',
        )
    if qualification is None or not qualification.qualified:
        return (
            'unqualified_listeners',
            'listener screening/training/test-retest evidence is '
            'incomplete',
        )
    if (
        plan.listener_count is not None
        and plan.trials_per_listener is not None
        and plan.listener_count * plan.trials_per_listener < 20
    ):
        return (
            'insufficient_trials',
            'declared trial volume cannot support the claimed '
            'inference',
        )
    if inference is None:
        return (
            'unverified_result',
            'experiment declared but no inference record pinned',
        )
    if inference.verdict in ('underpowered', 'inconclusive'):
        return (
            'insufficient_trials',
            f'inference verdict: {inference.verdict}',
        )
    return (
        'qualified_experiment',
        f'{plan.method_kind} experiment with qualified panel and '
        'pinned inference',
    )


EXPERIMENT_LABELS: dict[str, str] = {
    'qualified_experiment': '適格聴取実験',
    'unqualified_listeners': '聴取者未適格',
    'unrandomized_trials': '試行順序未ランダム化',
    'method_mismatch': '手法不一致',
    'insufficient_trials': '試行数不足',
    'unverified_result': '結果未検証',
}
