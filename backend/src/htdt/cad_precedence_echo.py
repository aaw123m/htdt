"""Precedence / discrete-echo perceptual-risk authority (issue #646).

An early/later reflection with a known delay and level is not
automatically an audible echo, an image shift, or harmless fused
energy. Fusion, localization dominance, discrimination suppression
and echo segregation are related but distinct phenomena, and echo
threshold depends on stimulus, delay, relative level, spatial
separation/direction, listener/task and build-up — a fixed 20 ms
rule is not honest.

Basis: precedence-effect literature (Haas; Litovsky et al. 1999
review; echo-threshold dependence on stimulus/delay/level/direction).
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


_STIMULUS_KINDS = (
    'speech', 'music', 'impulsive', 'noise', 'sine', 'other', 'unknown',
)
_PHENOMENA = (
    'fusion', 'localization_dominance', 'discrimination_suppression',
    'echo_segregation', 'image_shift', 'unknown',
)

EchoVerdict = Literal[
    'risk_profile_declared',
    'fixed_rule_misapplied',
    'stimulus_unqualified',
    'unresolved_risk',
    'evidence_declared',
]


class PrecedenceProfile(BaseModel):
    """Psychoacoustic interpretation context for reflection
    evaluation (#646) — stimulus class, task and listener context that
    any echo-risk claim must pin."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    stimulus_kind: Literal[
        'speech', 'music', 'impulsive', 'noise', 'sine', 'other',
        'unknown',
    ]
    listening_task: Literal[
        'critical_listening', 'casual_viewing', 'speech_intelligibility',
        'other', 'unknown',
    ] = 'unknown'
    adaptation_context: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('stimulus_kind') not in _STIMULUS_KINDS:
                raise ValueError('unknown stimulus kind')
            if data.get('stimulus_kind') == 'unknown':
                raise ValueError(
                    'a precedence profile must declare its stimulus '
                    'class — echo threshold is stimulus-dependent'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'PrecedenceProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'prec'
        )


class EchoRiskObservation(BaseModel):
    """One reflection's perceptual-risk assessment under a pinned
    precedence profile (#646) — delay/level/direction/stimulus bound
    to a phenomenon classification, never a fixed threshold rule."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    reflection_ref: AuthorityRef | None = None
    delay_ms: float
    relative_level_db: float
    direction_separation_deg: float | None = None
    assessed_phenomenon: Literal[
        'fusion', 'localization_dominance', 'discrimination_suppression',
        'echo_segregation', 'image_shift', 'unknown',
    ]
    risk_verdict: Literal[
        'echo_risk', 'image_shift_risk', 'fused', 'inconclusive',
    ]
    listening_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('profile_ref') is None:
                raise ValueError(
                    'an echo-risk observation requires a pinned '
                    'profile'
                )
            if data.get('assessed_phenomenon') not in _PHENOMENA:
                raise ValueError('unknown phenomenon')
            if data.get('assessed_phenomenon') == 'unknown':
                raise ValueError(
                    'a risk observation must classify the phenomenon — '
                    'fusion is not segregation'
                )
            verdict = data.get('risk_verdict')
            if verdict == 'echo_risk' and data.get(
                'delay_ms'
            ) is None:
                raise ValueError('echo risk requires a delay')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'EchoRiskObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'erk'
        )


def evaluate_echo_risk_claim(
    delay_ms: float | None,
    relative_level_db: float | None,
    profile: PrecedenceProfile | None,
    observation: EchoRiskObservation | None,
    *,
    used_fixed_rule: bool = False,
) -> tuple[EchoVerdict, str]:
    """Judge a perceptual echo-risk claim (#646).

    - a fixed delay rule (e.g. '>20ms = echo') → 'fixed_rule_misapplied'
    - no stimulus-declared profile → 'stimulus_unqualified'
    - no observation → 'unresolved_risk' (physical data alone cannot
      resolve the perceptual classification)
    - classified observation → 'evidence_declared' / profile-declared
    """
    if used_fixed_rule:
        return (
            'fixed_rule_misapplied',
            'a fixed delay threshold is not an echo verdict — fusion, '
            'segregation and image shift are distinct phenomena',
        )
    if profile is None:
        return (
            'stimulus_unqualified',
            'no precedence profile — echo threshold is stimulus/'
            'task-dependent and cannot be claimed generically',
        )
    if observation is None:
        return (
            'unresolved_risk',
            'physical delay/level exist but no perceptual observation '
            'is pinned',
        )
    if observation.risk_verdict == 'inconclusive':
        return (
            'unresolved_risk',
            'observation inconclusive — risk remains unresolved',
        )
    return (
        'evidence_declared',
        f'{observation.assessed_phenomenon}: '
        f'{observation.risk_verdict}',
    )


ECHO_LABELS: dict[str, str] = {
    'risk_profile_declared': '知覚プロファイル宣言済み',
    'fixed_rule_misapplied': '固定閾値ルールの誤適用',
    'stimulus_unqualified': '刺激未適格',
    'unresolved_risk': '知覚リスク未解決',
    'evidence_declared': '知覚証拠宣言済み',
}
