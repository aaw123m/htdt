"""Sound-strength G authority (issue #761).

Sound strength G is a standardized room-acoustic quantity: the sound
energy level at a receiver relative to the same source measured at
10 m in a free field. It is NOT ordinary SPL, NOT room gain by an
arbitrary installed speaker, and NOT a normalized frequency-response
curve. A G claim must pin the source-power reference and the
measurement/derivation method; installed-system level says nothing
about G by itself.

Basis: issue #761 scope; ISO 3382-1/ISO 3741 source-power
references; #668 excitation authority; #734 source normalization;
#618 absolute SPL calibration; #564/#566 solver validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


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


GMethod = Literal[
    'measured_with_reference_source', 'derived_from_source_power',
    'computed_from_impulse_response', 'declared_only', 'unknown',
]

G_LABELS: dict[str, str] = {
    'g_established': 'G は確立済み',
    'g_relative_only': '相対評価のみ（絶対 G ではない）',
    'not_sound_strength': 'これは G ではない',
    'insufficient_evidence': '証拠不足',
}


class SoundStrengthObservation(BaseModel):
    """A G observation bound to source-power reference + method
    (gobs- prefix)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    method: GMethod
    band: str
    value_db: float | None = None
    source_power_ref: AuthorityRef | None = None
    receiver_position_id: str | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SoundStrengthObservation':
        if not self.band:
            raise ValueError('band must be declared')
        if self.value_db is None and self.result_data_ref is None:
            raise ValueError('G observation needs a value or pinned data')
        if self.method == 'measured_with_reference_source' \
                and self.source_power_ref is None:
            raise ValueError(
                'reference-source method must pin source_power_ref')
        if self.method == 'derived_from_source_power' \
                and self.source_power_ref is None:
            raise ValueError(
                'source-power method must pin source_power_ref')
        if self.method == 'unknown' and self.value_db is not None:
            raise ValueError('unknown method cannot carry a G value')
        for ref in (self.source_power_ref, self.result_data_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SoundStrengthObservation':
        return _seal(
            cls, payload, 'observation_id',
            'observation_sha256', 'gobs')


class SoundStrengthQualification(BaseModel):
    """A G verdict bound to pinned observations (gqual- prefix)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    observation_refs: tuple[AuthorityRef, ...]
    verdict: str
    comparability_note: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SoundStrengthQualification':
        if not self.observation_refs:
            raise ValueError('G qualification needs observation refs')
        _require_refs(*self.observation_refs)
        if self.verdict not in G_LABELS:
            raise ValueError(f'unknown G verdict {self.verdict!r}')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SoundStrengthQualification':
        return _seal(
            cls, payload, 'qualification_id',
            'qualification_sha256', 'gqual')


def evaluate_g_claim(
    observation: SoundStrengthObservation | None,
    claim_kind: str,
) -> tuple[str, str]:
    """Ordinary SPL / room gain / normalized FR are never G."""
    if claim_kind in ('installed_spl', 'room_gain', 'normalized_fr'):
        return ('not_sound_strength',
                f'{claim_kind}_is_not_g')
    if observation is None:
        return ('insufficient_evidence', 'no_g_observation')
    if observation.method == 'declared_only' \
            or observation.method == 'unknown':
        return ('insufficient_evidence', 'method_not_evidence_grade')
    if observation.source_power_ref is None:
        return ('g_relative_only', 'no_absolute_source_reference')
    return ('g_established', 'source_power_pinned')
