"""Playback sample-rate-conversion / clock-domain authority (issue #739).

A chain can be clock-synchronized and still alter audio through
sample-rate conversion. Asynchronous SRC is an intentional clock-domain
crossing that changes latency, filter response, alias/image rejection
and exact sample identity — hidden SRC must remain measurable chain
state, never an implicit identity.

Basis: AES17-2020 measurement framework; Midya/Roeckner/Schooler AES
121 #6863 (ASRC synchronizes input data to a different low-jitter system
clock — a clock-domain crossing, not merely file conversion); Adams &
Kwan AES 93 #3355; `Measuring Audio when Clocks Differ` (differing
clocks act as differing sample rates).
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


SrcAlgorithm = Literal[
    'synchronous_fixed_ratio', 'asynchronous_src', 'polyphase',
    'os_mixer_builtin', 'integer_decimation', 'unknown',
]
ClockCrossingKind = Literal[
    'same_domain', 'asrc_crossing', 'buffered_sync', 'drift_compensated',
    'unknown',
]
SrcClaimVerdict = Literal[
    'sample_exact', 'converted_qualified', 'converted_unqualified',
    'hidden_conversion', 'insufficient_evidence',
]


SRC_ALGORITHM_LABELS: dict[str, str] = {
    'synchronous_fixed_ratio': '同期固定比変換',
    'asynchronous_src': '非同期 SRC',
    'polyphase': 'ポリフェーズ変換',
    'os_mixer_builtin': 'OS ミキサ内蔵変換',
    'integer_decimation': '整数間引き',
    'unknown': '不明',
}
CROSSING_LABELS: dict[str, str] = {
    'same_domain': '同一クロックドメイン',
    'asrc_crossing': 'ASRC クロックドメイン横断',
    'buffered_sync': 'バッファ同期',
    'drift_compensated': 'ドリフト補償',
    'unknown': '不明',
}
SRC_VERDICT_LABELS: dict[str, str] = {
    'sample_exact': 'サンプル同一性あり',
    'converted_qualified': '変換あり・適格',
    'converted_unqualified': '変換あり・未適格',
    'hidden_conversion': '隠れた変換を検出',
    'insufficient_evidence': '証拠不足',
}


class PlaybackSrcProfile(BaseModel):
    """Declared SRC stage in a playback chain.

    Rates and algorithm must be declared; `os_mixer_builtin` marks a
    conversion performed by the OS mixer whose parameters are external
    to HTDT control.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    input_rate_hz: float | None = None
    output_rate_hz: float | None = None
    algorithm: SrcAlgorithm = 'unknown'
    crossing_kind: ClockCrossingKind = 'unknown'
    latency_samples: float | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'PlaybackSrcProfile':
        if self.algorithm == 'unknown':
            raise ValueError(
                'SRC algorithm must be declared — an unknown conversion '
                'cannot anchor chain state'
            )
        for name, value in (
            ('input_rate_hz', self.input_rate_hz),
            ('output_rate_hz', self.output_rate_hz),
        ):
            if value is not None and value <= 0:
                raise ValueError(f'{name} must be positive')
        if (
            self.input_rate_hz is not None
            and self.output_rate_hz is not None
            and self.input_rate_hz == self.output_rate_hz
            and self.algorithm != 'synchronous_fixed_ratio'
        ):
            raise ValueError(
                'equal rates with a non-synchronous algorithm is not a '
                'pass-through declaration'
            )
        if self.latency_samples is not None and self.latency_samples < 0:
            raise ValueError('latency_samples cannot be negative')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'PlaybackSrcProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'srcp'
        )


class SrcQualificationRecord(BaseModel):
    """Measured SRC performance: alias/image rejection, bandwidth."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    src_profile_ref: AuthorityRef
    alias_rejection_db: float | None = None
    passband_ripple_db: float | None = None
    measurement_ref: AuthorityRef
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SrcQualificationRecord':
        _require_refs(self.src_profile_ref, self.measurement_ref)
        if (
            self.alias_rejection_db is None
            and self.passband_ripple_db is None
        ):
            raise ValueError(
                'a qualification needs at least one measured figure'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'SrcQualificationRecord':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256', 'srcq'
        )


class ClockDomainCrossingRecord(BaseModel):
    """Declared-vs-observed clock-domain crossing observation."""

    model_config = ConfigDict(frozen=True)

    crossing_id: str
    crossing_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    input_domain: str
    output_domain: str
    declared_kind: ClockCrossingKind = 'unknown'
    observed_kind: ClockCrossingKind = 'unknown'
    observation_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ClockDomainCrossingRecord':
        if self.observation_ref is not None:
            _require_refs(self.observation_ref)
        if not self.input_domain or not self.output_domain:
            raise ValueError('crossing needs declared domains')
        if (
            self.observed_kind != 'unknown'
            and self.observation_ref is None
        ):
            raise ValueError(
                'an observed crossing kind needs pinned observation '
                'evidence'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'crossing_id', 'crossing_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ClockDomainCrossingRecord':
        return _seal(
            cls, payload, 'crossing_id', 'crossing_sha256', 'cdc'
        )


def evaluate_src_claim(
    profiles: tuple[PlaybackSrcProfile, ...],
    qualifications: tuple[SrcQualificationRecord, ...],
    crossings: tuple[ClockDomainCrossingRecord, ...] = (),
    exact_sample_claim: bool = False,
) -> tuple[SrcClaimVerdict, str]:
    """Fail-closed playback-path SRC gate.

    An exact-sample-identity claim fails while any conversion stage is
    present; hidden conversions (declared same-domain but observed
    crossing, or rate mismatch without a declared profile) surface as
    `hidden_conversion`.
    """
    for c in crossings:
        if (
            c.declared_kind == 'same_domain'
            and c.observed_kind
            in ('asrc_crossing', 'buffered_sync', 'drift_compensated')
        ):
            return 'hidden_conversion', 'declared_same_observed_crossing'
    if not profiles:
        if crossings:
            return 'insufficient_evidence', 'crossing_without_profile'
        return ('insufficient_evidence', 'no_src_profile') \
            if exact_sample_claim else ('sample_exact', 'no_conversion')
    qualified_refs = {q.src_profile_ref.ref_id for q in qualifications}
    for p in profiles:
        if p.profile_id not in qualified_refs:
            return 'converted_unqualified', 'unqualified_conversion_stage'
    if exact_sample_claim:
        return 'converted_qualified', 'identity_claim_impossible_with_src'
    return 'converted_qualified', 'all_stages_qualified'
