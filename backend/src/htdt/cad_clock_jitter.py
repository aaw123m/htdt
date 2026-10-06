"""Sample-clock jitter metrology authority (issue #745).

Synchronization lock state and sample-clock jitter performance are
related but NOT the same measurand: a chain can be locked and still
carry degraded jitter. Ordinary audio measurements are not sufficient
to characterize converter jitter susceptibility — dedicated evidence is
required before claiming jitter tolerance.

Basis: AES-12id-2020 (jitter performance specifications — wideband,
baseband, period, long-term jitter; jitter spectra/signatures; PLL
jitter transfer; converter susceptibility), Dunn AES UK 9th Conf. 1994
(sampling jitter ↔ modulation products; ordinary audio measurements
insufficient for susceptibility). AES-12id-R is RESEARCH_ONLY until a
revision publishes — the 2020 edition stays production-current.
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


JitterKind = Literal[
    'wideband', 'baseband', 'period', 'long_term', 'spectrum',
    'signature', 'unknown',
]
JitterClaimKind = Literal[
    'locked_operation', 'low_jitter', 'converter_immune', 'unknown',
]
JitterClaimVerdict = Literal[
    'supported', 'supported_with_limitations', 'not_supported',
    'insufficient_evidence',
]


JITTER_KIND_LABELS: dict[str, str] = {
    'wideband': 'ワイドバンドジッタ',
    'baseband': 'ベースバンドジッタ',
    'period': '周期ジッタ',
    'long_term': '長期ジッタ',
    'spectrum': 'ジッタスペクトル',
    'signature': 'ジッタシグネチャ',
    'unknown': '不明',
}
CLAIM_VERDICT_LABELS: dict[str, str] = {
    'supported': '裏付けあり',
    'supported_with_limitations': '限定付きで裏付けあり',
    'not_supported': '裏付けなし',
    'insufficient_evidence': '証拠不足',
}


class SampleClockJitterProfile(BaseModel):
    """Declared jitter-measurement profile: instrument, covered kinds.

    The measurand set an instrument actually covers must be pinned —
    a lock indicator is not a jitter instrument.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    covered_kinds: tuple[JitterKind, ...] = ()
    instrument_ref: AuthorityRef | None = None
    spectrum_capable: bool = False
    uncertainty_ps: float | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SampleClockJitterProfile':
        if self.instrument_ref is not None:
            _require_refs(self.instrument_ref)
        if 'unknown' in self.covered_kinds:
            raise ValueError(
                'covered_kinds cannot include unknown — pin what the '
                'instrument actually measures'
            )
        if self.spectrum_capable and 'spectrum' not in self.covered_kinds:
            raise ValueError(
                'spectrum_capable profiles must cover the spectrum kind'
            )
        if self.uncertainty_ps is not None and self.uncertainty_ps < 0:
            raise ValueError('uncertainty_ps cannot be negative')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'SampleClockJitterProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'jmp'
        )


class SampleClockJitterObservation(BaseModel):
    """One jitter observation bound to a profile and clock domain."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    clock_domain: str
    jitter_kind: JitterKind
    value_ps: float | None = None
    measurement_data_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SampleClockJitterObservation':
        _require_refs(self.profile_ref)
        if self.measurement_data_ref is not None:
            _require_refs(self.measurement_data_ref)
        if self.jitter_kind == 'unknown':
            raise ValueError(
                'jitter_kind must be declared — an unkind measurement '
                'cannot anchor jitter evidence'
            )
        if self.value_ps is not None and self.value_ps < 0:
            raise ValueError('value_ps cannot be negative')
        if self.value_ps is None and self.measurement_data_ref is None:
            raise ValueError(
                'an observation needs a value or pinned measurement data'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'SampleClockJitterObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'job'
        )


class JitterTransferMeasurement(BaseModel):
    """Jitter transfer through a clock chain (PLL / distribution)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    input_domain: str
    output_domain: str
    transfer_data_ref: AuthorityRef
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'JitterTransferMeasurement':
        _require_refs(self.profile_ref, self.transfer_data_ref)
        if not self.input_domain or not self.output_domain:
            raise ValueError('transfer needs declared input/output domains')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'JitterTransferMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'jtf'
        )


class ConverterJitterSusceptibility(BaseModel):
    """Converter jitter-susceptibility evidence.

    Ordinary audio measurements do not characterize susceptibility —
    a dedicated susceptibility test (modulation-product or equivalent)
    is required before claiming converter immunity.
    """

    model_config = ConfigDict(frozen=True)

    susceptibility_id: str
    susceptibility_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    converter_ref: AuthorityRef
    test_method_ref: AuthorityRef
    result_data_ref: AuthorityRef | None = None
    susceptible: bool | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ConverterJitterSusceptibility':
        _require_refs(self.converter_ref, self.test_method_ref)
        if self.result_data_ref is not None:
            _require_refs(self.result_data_ref)
        if self.susceptible is not None and self.result_data_ref is None:
            raise ValueError(
                'a susceptibility verdict needs pinned result data'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'susceptibility_id', 'susceptibility_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ConverterJitterSusceptibility':
        return _seal(
            cls,
            payload,
            'susceptibility_id',
            'susceptibility_sha256',
            'cjs',
        )


def evaluate_jitter_claim(
    claim: JitterClaimKind,
    observations: tuple[SampleClockJitterObservation, ...],
    susceptibility: ConverterJitterSusceptibility | None,
) -> tuple[JitterClaimVerdict, str]:
    """Fail-closed jitter-claim gate.

    Lock state alone never supports a low-jitter or converter-immunity
    claim; immunity additionally requires dedicated susceptibility
    evidence (Dunn: ordinary measurements are insufficient).
    """
    if claim == 'locked_operation':
        return 'supported_with_limitations', 'lock_only_not_jitter'
    if not observations:
        return 'insufficient_evidence', 'no_jitter_observation'
    if claim == 'low_jitter':
        if any(o.jitter_kind == 'spectrum' for o in observations):
            return 'supported', 'spectral_evidence'
        return 'supported_with_limitations', 'scalar_only'
    if claim == 'converter_immune':
        if susceptibility is None:
            return 'insufficient_evidence', 'no_susceptibility_test'
        if susceptibility.susceptible is False:
            return 'supported', 'susceptibility_tested'
        if susceptibility.susceptible is True:
            return 'not_supported', 'converter_susceptible'
        return 'insufficient_evidence', 'susceptibility_result_absent'
    return 'insufficient_evidence', 'claim_kind_unknown'
