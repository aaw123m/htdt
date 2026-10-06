"""Mixing-time / echo-density authority (issue #764).

Reverberation time does not identify when a room impulse response
transitions from sparse, individually structured reflections to a dense
late field. The transition affects early↔late handoff (#687 hybrid),
stochastic late-tail rendering, and whether a late response may be
treated as sufficiently mixed for a chosen model.

Basis: normalized echo-density estimators for measured/predicted RIRs
(Abel & Huang), early-transition studies (Defrance & Polack), and
diffuseness-adjacent estimators; position dependence and estimator
parameters are part of the estimate, never an implied constant. No
single universal mixing-time formula or home-theater threshold exists —
the authority pins the estimator, not a constant.
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


EchoEstimatorKind = Literal[
    'normalized_echo_density', 'echo_density_kurtosis',
    'gaussian_model_fit', 'schroeder_slope_transition',
    'diffuseness_threshold', 'temporal_kurtosis',
    'user_declared', 'unknown',
]
MixingBasis = Literal[
    'measured_rir', 'predicted_rir', 'spatial_array',
    'simulated_tail', 'unknown',
]
TransitionVerdict = Literal[
    'sufficiently_mixed', 'in_transition', 'sparse_early_field',
    'insufficient_evidence', 'incomparable',
]
LateHandoffVerdict = Literal[
    'handoff_supported', 'handoff_unsupported', 'insufficient_evidence',
]

ESTIMATOR_LABELS: dict[str, str] = {
    'normalized_echo_density': '正規化エコー密度',
    'echo_density_kurtosis': 'エコー密度（尖度推定）',
    'gaussian_model_fit': 'ガウスモデル適合',
    'schroeder_slope_transition': 'シュレーダー傾斜遷移',
    'diffuseness_threshold': '拡散度閾値',
    'temporal_kurtosis': '時間尖度',
    'user_declared': '利用者宣言値',
    'unknown': '不明',
}
BASIS_LABELS: dict[str, str] = {
    'measured_rir': '実測インパルス応答',
    'predicted_rir': '予測インパルス応答',
    'spatial_array': '空間アレイ',
    'simulated_tail': 'シミュレーション残響尾部',
    'unknown': '不明',
}
TRANSITION_LABELS: dict[str, str] = {
    'sufficiently_mixed': '十分に混合',
    'in_transition': '遷移中',
    'sparse_early_field': '疎な初期音場',
    'insufficient_evidence': '証拠不足',
    'incomparable': '比較不能',
}
HANDOFF_LABELS: dict[str, str] = {
    'handoff_supported': '後期遷移の根拠あり',
    'handoff_unsupported': '後期遷移を支持しない',
    'insufficient_evidence': '証拠不足',
}


class EchoDensityProfile(BaseModel):
    """Declared echo-density/mixing estimator configuration."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    estimator_kind: EchoEstimatorKind
    window_ms: float | None = Field(default=None, gt=0)
    threshold: float | None = None
    directional_capability: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    method_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'EchoDensityProfile':
        if self.method_ref is not None:
            _require_refs(self.method_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'EchoDensityProfile':
        return _seal(
            cls, kwargs, 'profile_id', 'profile_sha256', 'edp'
        )


class MixingTimeEstimate(BaseModel):
    """One position-bound mixing-time estimate.

    Estimates are bound to a basis (measured vs predicted RIR), a
    position ref and the estimator profile — a single room-wide mixing
    time is not implied.
    """

    model_config = ConfigDict(frozen=True)

    estimate_id: str
    estimate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    basis: MixingBasis = 'unknown'
    source_ref: AuthorityRef | None = None
    position_ref: AuthorityRef | None = None
    estimate_ms: float | None = Field(default=None, gt=0)
    uncertainty_ms: float | None = Field(default=None, ge=0)
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'MixingTimeEstimate':
        _require_refs(self.profile_ref)
        if self.source_ref is not None:
            _require_refs(self.source_ref)
        if self.position_ref is not None:
            _require_refs(self.position_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'estimate_id', 'estimate_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'MixingTimeEstimate':
        return _seal(
            cls, kwargs, 'estimate_id', 'estimate_sha256', 'mixt'
        )


class LateFieldTransitionAssessment(BaseModel):
    """Verdict on the early→late transition for one handoff question."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    estimate_refs: tuple[AuthorityRef, ...] = ()
    verdict: TransitionVerdict = 'insufficient_evidence'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'LateFieldTransitionAssessment':
        for ref in self.estimate_refs:
            _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'assessment_id', 'assessment_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'LateFieldTransitionAssessment':
        return _seal(
            cls, kwargs, 'assessment_id', 'assessment_sha256', 'lft'
        )


def evaluate_transition(
    profile: EchoDensityProfile,
    estimate: MixingTimeEstimate | None,
) -> tuple[TransitionVerdict, tuple[str, ...]]:
    """Fail-closed transition verdict.

    No estimate → ``insufficient_evidence``. An unnamed estimator or a
    user-declared value without method evidence cannot establish
    ``sufficiently_mixed``.
    """
    if estimate is None or estimate.estimate_ms is None:
        return 'insufficient_evidence', ('no_estimate',)
    if estimate.basis == 'unknown':
        return 'insufficient_evidence', ('basis_unknown',)
    if profile.estimator_kind == 'unknown':
        return 'insufficient_evidence', ('estimator_unknown',)
    if estimate.profile_ref.ref_sha256 != profile.profile_sha256:
        return 'incomparable', ('profile_mismatch',)
    if profile.estimator_kind == 'user_declared':
        return 'in_transition', ('declared_only',)
    if profile.threshold is None:
        return 'in_transition', ('threshold_undeclared',)
    return 'sufficiently_mixed', ()


def late_handoff_gate(
    rt_seconds: float | None,
    assessment: LateFieldTransitionAssessment | None,
) -> tuple[LateHandoffVerdict, tuple[str, ...]]:
    """RT alone is not mixing evidence.

    Even with a valid RT, the early→late handoff needs an explicit
    transition assessment; otherwise the claim is
    ``insufficient_evidence`` — never defaulted benign.
    """
    if assessment is None:
        reasons = ['transition_assessment_missing']
        if rt_seconds is None:
            reasons.append('rt_unknown')
        else:
            reasons.append('rt_alone_not_evidence')
        return 'insufficient_evidence', tuple(reasons)
    if assessment.verdict == 'sufficiently_mixed':
        return 'handoff_supported', ()
    if assessment.verdict in ('in_transition', 'sparse_early_field'):
        return 'handoff_unsupported', (assessment.verdict,)
    return 'insufficient_evidence', (assessment.verdict,)
