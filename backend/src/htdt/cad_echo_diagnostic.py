"""Flutter / focused-echo objective diagnostics authority
(issue #773).

A room can have acceptable broadband FR, RT/EDT and reasonable
C50/C80 while still containing late discrete reflections, focused
echoes or flutter-echo sequences that are perceptually objectionable.
The Dietsch & Kraak (1986) echo criterion estimates disturbing
echoes for speech and music but is not universal truth — thresholds
depend on signal class, listener, reflection pattern and measurement
quality.

Basis: Dietsch & Kraak 1986 (echo criterion, implemented in
ODEON/EASERA); later evaluations documenting its limits.
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


_SIGNAL_CLASSES = ('speech', 'music', 'other', 'unknown')
_PERIODICITIES = ('discrete_single', 'periodic_train', 'focused', 'other')

EchoDiagVerdict = Literal[
    'echo_flagged',
    'no_echo_detected',
    'diagnostic_inconclusive',
    'broadband_metrics_are_not_echo_evidence',
]


class DiscreteReflectionEvent(BaseModel):
    """One late discrete reflection or flutter component (#773) —
    delay, level, periodicity and the ETC evidence it was detected
    from."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    delay_ms: float
    relative_level_db: float
    periodicity: Literal[
        'discrete_single', 'periodic_train', 'focused', 'other',
    ]
    repetition_interval_ms: float | None = None
    etc_ref: AuthorityRef

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('etc_ref') is None:
                raise ValueError(
                    'a discrete reflection event requires pinned ETC '
                    'evidence'
                )
            if data.get('periodicity') not in _PERIODICITIES:
                raise ValueError('unknown periodicity kind')
            if data.get('periodicity') == 'periodic_train' and (
                data.get('repetition_interval_ms') is None
            ):
                raise ValueError(
                    'a periodic train requires its repetition '
                    'interval'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'event_id', 'event_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'DiscreteReflectionEvent':
        return _seal(
            cls, payload, 'event_id', 'event_sha256', 'dre'
        )


class EchoDiagnostic(BaseModel):
    """Objective echo diagnosis over detected events (#773) —
    signal-class-scoped Dietsch-style criterion; 'unknown' signal
    class fails closed."""

    model_config = ConfigDict(frozen=True)

    diagnostic_id: str
    diagnostic_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    signal_class: Literal['speech', 'music', 'other', 'unknown']
    event_refs: tuple[AuthorityRef, ...]
    criterion_value: float | None = None
    threshold_ref: AuthorityRef | None = None
    verdict: Literal[
        'disturbing_echo_likely', 'no_disturbing_echo',
        'inconclusive',
    ]

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('signal_class') not in _SIGNAL_CLASSES:
                raise ValueError('unknown signal class')
            if data.get('signal_class') == 'unknown':
                raise ValueError(
                    'echo diagnosis must declare its signal class — '
                    'thresholds are signal-dependent'
                )
            if data.get('verdict') == 'disturbing_echo_likely' and (
                data.get('criterion_value') is None
                or data.get('threshold_ref') is None
            ):
                raise ValueError(
                    'a disturbing-echo verdict requires the criterion '
                    'value and its threshold reference'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'diagnostic_id', 'diagnostic_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'EchoDiagnostic':
        return _seal(
            cls, payload, 'diagnostic_id', 'diagnostic_sha256', 'edia'
        )


def evaluate_echo_diagnostic_claim(
    diagnostic: EchoDiagnostic | None,
    *,
    broadband_metrics_clean: bool = False,
) -> tuple[EchoDiagVerdict, str]:
    """Judge an objective echo-disturbance claim (#773)."""
    if diagnostic is None:
        if broadband_metrics_clean:
            return (
                'broadband_metrics_are_not_echo_evidence',
                'clean FR/RT/C50 does not rule out late discrete '
                'echoes — echo diagnosis is a separate observable',
            )
        return (
            'diagnostic_inconclusive',
            'no echo diagnostic pinned',
        )
    if diagnostic.verdict == 'inconclusive':
        return (
            'diagnostic_inconclusive',
            'diagnostic inconclusive',
        )
    if diagnostic.verdict == 'disturbing_echo_likely':
        return (
            'echo_flagged',
            f"{diagnostic.signal_class}: disturbing echo likely",
        )
    return (
        'no_echo_detected',
        'no disturbing echo detected under the declared signal '
        'class',
    )


ECHO_DIAG_LABELS: dict[str, str] = {
    'echo_flagged': 'エコー疑いあり',
    'no_echo_detected': 'エコー未検出',
    'diagnostic_inconclusive': '診断不能',
    'broadband_metrics_are_not_echo_evidence': '広帯域指標はエコー証拠ではない',
}
