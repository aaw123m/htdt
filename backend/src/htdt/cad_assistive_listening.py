"""Assistive-listening-system authority (issue #726).

Good loudspeaker SPL, STI and dialogue performance do not prove an
assistive path — induction loop, FM/IR receiver, hard-wired receiver
or Auracast/Bluetooth broadcast — is present, correctly routed or
qualified. Engineering evidence is modelled without inventing
jurisdictional obligations.

Basis: IEC 60118-4:2014+AMD1:2017 (induction-loop field strength /
SNR / frequency-response requirements and measurement).
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


_ALS_KINDS = (
    'induction_loop', 'fm_receiver', 'ir_receiver',
    'hardwired_receiver', 'auracast_broadcast', 'other', 'unknown',
)

AlsVerdict = Literal[
    'qualified_path',
    'path_declared_unmeasured',
    'receiver_incompatible',
    'routing_unverified',
    'speaker_perf_is_not_als',
]


class AssistiveListeningPath(BaseModel):
    """Declared assistive-listening path (#726) — delivery technology
    and routing source. 'unknown' technology is not a claim."""

    model_config = ConfigDict(frozen=True)

    path_id: str
    path_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    technology: Literal[
        'induction_loop', 'fm_receiver', 'ir_receiver',
        'hardwired_receiver', 'auracast_broadcast', 'other', 'unknown',
    ]
    source_ref: AuthorityRef | None = None
    coverage_area_descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('technology') not in _ALS_KINDS:
                raise ValueError('unknown ALS technology')
            if data.get('technology') == 'unknown':
                raise ValueError(
                    'an assistive path must declare its delivery '
                    'technology'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'path_id', 'path_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'AssistiveListeningPath':
        return _seal(
            cls, payload, 'path_id', 'path_sha256', 'alsp'
        )


class ALSQualification(BaseModel):
    """Field/measurement qualification of an assistive path (#726) —
    IEC 60118-4 class evidence for induction loops; equivalent
    field-strength/SNR evidence for other technologies."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_ref: AuthorityRef
    field_strength_ref: AuthorityRef | None = None
    snr_ref: AuthorityRef | None = None
    frequency_response_ref: AuthorityRef | None = None
    standard_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('path_ref') is None:
                raise ValueError(
                    'ALS qualification requires a pinned path'
                )
        return data

    @property
    def measured(self) -> bool:
        return (
            self.field_strength_ref is not None
            or self.snr_ref is not None
            or self.frequency_response_ref is not None
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ALSQualification':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256',
            'alsq',
        )


class ReceiverCompatibilityEvidence(BaseModel):
    """Receiver/hearing-aid compatibility evidence (#726) — telecoil
    for loops, matching tuner for FM/IR, Auracast assistant support."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_ref: AuthorityRef
    receiver_kind: str
    compatible: bool

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('path_ref') is None:
                raise ValueError(
                    'receiver compatibility requires a pinned path'
                )
            if not data.get('receiver_kind'):
                raise ValueError('receiver kind is required')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ReceiverCompatibilityEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'rcomp'
        )


def evaluate_als_claim(
    path: AssistiveListeningPath | None,
    qualification: ALSQualification | None,
    receiver: ReceiverCompatibilityEvidence | None,
    *,
    routing_verified: bool = False,
    speaker_qualified: bool = False,
) -> tuple[AlsVerdict, str]:
    """Judge an assistive-listening claim (#726)."""
    if path is None:
        if speaker_qualified:
            return (
                'speaker_perf_is_not_als',
                'loudspeaker performance does not prove an assistive '
                'path exists or is qualified',
            )
        return (
            'path_declared_unmeasured',
            'no assistive-listening path declared',
        )
    if not routing_verified:
        return (
            'routing_unverified',
            'the path is declared but its audio routing is not '
            'verified to the program source',
        )
    if qualification is None or not qualification.measured:
        return (
            'path_declared_unmeasured',
            'no pinned field-strength/SNR/frequency-response '
            'evidence',
        )
    if receiver is not None and not receiver.compatible:
        return (
            'receiver_incompatible',
            'declared receiver is incompatible with the path',
        )
    return (
        'qualified_path',
        f'{path.technology} path routed and field-qualified',
    )


ALS_LABELS: dict[str, str] = {
    'qualified_path': '適格補聴経路',
    'path_declared_unmeasured': '経路未測定',
    'receiver_incompatible': '受信機不適合',
    'routing_unverified': 'ルーティング未検証',
    'speaker_perf_is_not_als': 'スピーカー性能は補聴証拠ではない',
}
