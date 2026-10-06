"""Acoustic-solver compute-budget / scaling authority (issue #770).

Fidelity choices must expose runtime, memory and hardware cost — a
requested fidelity is not reproducible or reviewable without cost
evidence, and predicted cost must stay distinct from observed cost.

Basis: Treble documentation on time-domain DG room-solver cost scaling
(room volume, IR duration, maximum resolved frequency; hybrid
wave+geometrical with GPU/HPC execution), Melander et al. 2024
(doi:10.1177/10943420231208948), Okuzono et al. 2022
(doi:10.1016/j.apacoust.2021.108212).
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


FidelityAxis = Literal[
    'max_frequency', 'mesh_density', 'ir_duration', 'ray_count',
    'room_volume', 'basis_order', 'time_step', 'element_order',
    'batch_size', 'unknown',
]
CostMetricKind = Literal[
    'runtime_s', 'peak_memory_bytes', 'gpu_seconds', 'cpu_seconds',
    'storage_bytes', 'throughput_runs_per_hour', 'energy_wh', 'unknown',
]
BudgetEvidenceMeans = Literal[
    'observed_run', 'vendor_documented', 'extrapolated_model',
    'assumed', 'unknown',
]
BudgetVerdict = Literal[
    'within_budget', 'over_budget', 'budget_unknown', 'incomparable',
]
FidelityClaimVerdict = Literal[
    'cost_evidence_present', 'cost_evidence_partial',
    'cost_evidence_missing',
]

AXIS_LABELS: dict[str, str] = {
    'max_frequency': '最大解析周波数',
    'mesh_density': 'メッシュ密度',
    'ir_duration': 'IR 長',
    'ray_count': 'レイ数',
    'room_volume': '室容積',
    'basis_order': '基底次数',
    'time_step': '時間刻み',
    'element_order': '要素次数',
    'batch_size': 'バッチ数',
    'unknown': '不明',
}
COST_LABELS: dict[str, str] = {
    'runtime_s': '実行時間(s)',
    'peak_memory_bytes': 'ピークメモリ(B)',
    'gpu_seconds': 'GPU 秒',
    'cpu_seconds': 'CPU 秒',
    'storage_bytes': 'ストレージ(B)',
    'throughput_runs_per_hour': 'スループット(回/時)',
    'energy_wh': 'エネルギー(Wh)',
    'unknown': '不明',
}
MEANS_LABELS: dict[str, str] = {
    'observed_run': '実測ラン',
    'vendor_documented': 'ベンダー文書',
    'extrapolated_model': '外挿モデル',
    'assumed': '仮定値',
    'unknown': '不明',
}
BUDGET_LABELS: dict[str, str] = {
    'within_budget': '予算内',
    'over_budget': '予算超過',
    'budget_unknown': 'コスト不明',
    'incomparable': '比較不能',
}
FIDELITY_CLAIM_LABELS: dict[str, str] = {
    'cost_evidence_present': 'コスト証拠あり',
    'cost_evidence_partial': 'コスト証拠一部',
    'cost_evidence_missing': 'コスト証拠なし',
}


class PredictedCost(BaseModel):
    """One predicted cost figure with its evidence tier."""

    model_config = ConfigDict(frozen=True)

    metric: CostMetricKind
    value: float = Field(ge=0)
    means: BudgetEvidenceMeans = 'unknown'
    run_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'PredictedCost':
        if self.metric == 'unknown':
            raise ValueError('cost metric kind is required')
        if self.means == 'observed_run' and self.run_ref is None:
            raise ValueError(
                'observed_run predicted cost requires a run reference'
            )
        if self.run_ref is not None:
            _require_refs(self.run_ref)
        return self


class SolverBudgetProfile(BaseModel):
    """Declared fidelity → predicted cost for one solver config."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    solver_ref: AuthorityRef | None = None
    fidelity_axes: dict[str, Any] = Field(default_factory=dict)
    predicted_costs: tuple[PredictedCost, ...] = ()
    hardware_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SolverBudgetProfile':
        if self.solver_ref is not None:
            _require_refs(self.solver_ref)
        if self.hardware_ref is not None:
            _require_refs(self.hardware_ref)
        unknown_axes = [k for k in self.fidelity_axes if k == 'unknown']
        if unknown_axes:
            raise ValueError(
                'fidelity_axes cannot be keyed by unknown axis'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SolverBudgetProfile':
        return _seal(
            cls, kwargs, 'profile_id', 'profile_sha256', 'cbp'
        )


class ComputeObservation(BaseModel):
    """Observed resource cost of one actual run — kept distinct from
    predicted cost so extrapolation cannot launder itself as measured."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    run_ref: AuthorityRef | None = None
    profile_ref: AuthorityRef | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    hardware_label: str = ''
    observed_at_utc: str = ''
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ComputeObservation':
        if self.run_ref is not None:
            _require_refs(self.run_ref)
        if self.profile_ref is not None:
            _require_refs(self.profile_ref)
        if 'unknown' in self.metrics:
            raise ValueError(
                'observed metrics cannot be keyed by unknown kind'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ComputeObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'cobs'
        )


class AccuracyCostEnvelope(BaseModel):
    """Accuracy-vs-cost envelope binding fidelity, cost observations
    and the accuracy evidence (#566) they were measured under."""

    model_config = ConfigDict(frozen=True)

    envelope_id: str
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_refs: tuple[AuthorityRef, ...] = ()
    observation_refs: tuple[AuthorityRef, ...] = ()
    accuracy_envelope_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'AccuracyCostEnvelope':
        for ref in self.profile_refs:
            _require_refs(ref)
        for ref in self.observation_refs:
            _require_refs(ref)
        if self.accuracy_envelope_ref is not None:
            _require_refs(self.accuracy_envelope_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'envelope_id', 'envelope_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'AccuracyCostEnvelope':
        return _seal(
            cls, kwargs, 'envelope_id', 'envelope_sha256', 'cenv'
        )


def evaluate_budget(
    observation: ComputeObservation | None,
    budget_limits: dict[str, float],
) -> tuple[BudgetVerdict, tuple[str, ...]]:
    """Compare observed cost against declared limits.

    Missing observation or missing limit → ``budget_unknown``; an
    observed metric above its limit → ``over_budget``.
    """
    if observation is None:
        return 'budget_unknown', ('no_observation',)
    if not budget_limits:
        return 'budget_unknown', ('no_limits',)
    over: list[str] = []
    unknown: list[str] = []
    for kind, limit in budget_limits.items():
        value = observation.metrics.get(kind)
        if value is None:
            unknown.append(f'unobserved_{kind}')
        elif value > limit:
            over.append(f'over_{kind}')
    if over:
        return 'over_budget', tuple(over + unknown)
    if unknown:
        return 'budget_unknown', tuple(unknown)
    return 'within_budget', ()


def fidelity_cost_gate(
    profile: SolverBudgetProfile,
) -> FidelityClaimVerdict:
    """A declared fidelity must expose its cost story.

    Predicted costs resting on ``assumed``/``unknown`` means count as
    partial evidence only — fidelity claims may not silently omit
    runtime/memory/hardware cost.
    """
    if not profile.predicted_costs:
        return 'cost_evidence_missing'
    weak = [
        c for c in profile.predicted_costs
        if c.means in ('assumed', 'unknown')
    ]
    if weak:
        return 'cost_evidence_partial'
    return 'cost_evidence_present'
