"""Interchannel crosstalk / separation qualification authority (issue #650).

Proving logical channel X reaches the intended loudspeaker (#621) does
not prove X is isolated from every other channel. Low-level leakage
contaminates per-channel calibration, blurs localization, biases
spatial tests and makes a correct routing graph look fully correct
while unintended channels remain active.

Basis: IEC 60268-3:2018 (amplifier measurements — crosstalk/separation
in multi-channel amplifiers, interchannel gain/phase differences),
AES17-2020 (digital audio measurement — production-current until
AES17-R publishes). Declared intended mixing (upmix/downmix/render) is
a separate declarand from unintended leakage and must not be conflated.
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


LeakageMethod = Literal[
    'iec60268_3', 'aes17', 'swept_sine', 'stimulus_specific', 'unknown',
]
LeakageStage = Literal[
    'decoder_output', 'dsp_matrix', 'dac_stage', 'preamp_path',
    'power_amplifier', 'active_speaker_route', 'full_chain', 'unknown',
]
SeparationVerdict = Literal[
    'qualified', 'qualified_with_limitations', 'unqualified',
    'insufficient_evidence',
]


METHOD_LABELS: dict[str, str] = {
    'iec60268_3': 'IEC 60268-3 方式',
    'aes17': 'AES17 方式',
    'swept_sine': 'スイープ正弦波',
    'stimulus_specific': '刺激固有方式',
    'unknown': '不明',
}
STAGE_LABELS: dict[str, str] = {
    'decoder_output': 'デコーダ出力',
    'dsp_matrix': 'DSP マトリクス',
    'dac_stage': 'DAC 段',
    'preamp_path': 'プリアンプ経路',
    'power_amplifier': 'パワーアンプ',
    'active_speaker_route': 'アクティブスピーカー経路',
    'full_chain': '全チェーン',
    'unknown': '不明',
}
SEPARATION_LABELS: dict[str, str] = {
    'qualified': '分離適格',
    'qualified_with_limitations': '限定付き分離適格',
    'unqualified': '分離不適格',
    'insufficient_evidence': '証拠不足',
}


class InterchannelLeakageMeasurement(BaseModel):
    """One driven→observed channel-pair leakage measurement."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    driven_channel: str
    observed_channel: str
    stage: LeakageStage = 'unknown'
    method: LeakageMethod = 'unknown'
    stimulus_ref: AuthorityRef | None = None
    level_db: float | None = None
    frequency_limited: bool = False
    measurement_data_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'InterchannelLeakageMeasurement':
        if self.stimulus_ref is not None:
            _require_refs(self.stimulus_ref)
        if self.measurement_data_ref is not None:
            _require_refs(self.measurement_data_ref)
        if not self.driven_channel or not self.observed_channel:
            raise ValueError('leakage needs driven/observed channels')
        if self.driven_channel == self.observed_channel:
            raise ValueError(
                'driven and observed channels must differ — self-channel '
                'is not a leakage measurement'
            )
        if self.method == 'unknown':
            raise ValueError(
                'measurement method must be declared'
            )
        if self.level_db is None and self.measurement_data_ref is None:
            raise ValueError(
                'a measurement needs a level or pinned measurement data'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'InterchannelLeakageMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'xtk'
        )


class ChannelSeparationQualification(BaseModel):
    """Sealed separation verdict across a channel set."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    stage: LeakageStage
    threshold_db: float
    worst_case_db: float | None = None
    measured_pairs: int = 0
    required_pairs: int = 0
    intended_matrix_declared: bool = False
    verdict: SeparationVerdict = 'insufficient_evidence'
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ChannelSeparationQualification':
        if self.threshold_db >= 0:
            raise ValueError(
                'threshold_db must be negative — separation is expressed '
                'as leakage level below the driven channel'
            )
        if self.worst_case_db is not None and self.worst_case_db >= 0:
            raise ValueError('worst_case_db must be negative')
        if self.measured_pairs < 0 or self.required_pairs < 0:
            raise ValueError('pair counts cannot be negative')
        if (
            self.verdict == 'qualified'
            and self.required_pairs > 0
            and self.measured_pairs < self.required_pairs
        ):
            raise ValueError(
                'qualified requires all required channel pairs measured'
            )
        if (
            self.verdict == 'qualified'
            and self.worst_case_db is not None
            and self.worst_case_db > self.threshold_db
        ):
            raise ValueError(
                'qualified requires worst_case_db at or below threshold'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ChannelSeparationQualification':
        return _seal(
            cls,
            payload,
            'qualification_id',
            'qualification_sha256',
            'csep',
        )


def evaluate_separation_claim(
    routing_verified: bool,
    qualification: ChannelSeparationQualification | None,
) -> tuple[SeparationVerdict, str]:
    """Fail-closed separation gate.

    Correct routing alone never proves isolation; a qualification
    verdict is required before claiming acceptable separation.
    """
    if qualification is None:
        return 'insufficient_evidence', 'no_separation_evidence'
    if qualification.verdict == 'unqualified':
        return 'unqualified', 'worst_case_above_threshold'
    if qualification.verdict == 'qualified':
        return ('qualified', 'separation_measured') \
            if routing_verified else (
                'qualified_with_limitations', 'routing_unverified'
            )
    if qualification.verdict == 'qualified_with_limitations':
        return 'qualified_with_limitations', 'partial_coverage'
    return 'insufficient_evidence', 'qualification_inconclusive'
