"""Measurement receiver reference-point authority (issue #774).

#654 separates a loudspeaker's cabinet pose from its acoustic
reference origin; no equivalent existed for the measurement
receiver. IEC 61094 distinguishes a microphone reference point from
its acoustic centre — if a manufacturer reference point (diaphragm
center, protection grid) is used instead of the acoustic centre, the
difference is an uncertainty in source distance and therefore sound
pressure; acoustic-centre position can vary with frequency and
source distance.

Basis: IEC 61094-8 (reference point vs acoustic centre).
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


_POINT_KINDS = (
    'acoustic_centre', 'diaphragm_center', 'protection_grid',
    'manufacturer_reference', 'capsule_tip', 'other', 'unknown',
)
_PRECISION_CLAIMS = (
    'direct_arrival_time', 'source_receiver_distance',
    'early_reflection_delay', 'phase', 'inverse_distance_level',
    'array_geometry', 'registration', 'other',
)

ReceiverVerdict = Literal[
    'reference_qualified',
    'reference_point_unqualified',
    'acoustic_centre_conflation',
    'uncertainty_budget_required',
]


class ReceiverReferencePoint(BaseModel):
    """Declared receiver reference semantics (#774) — which physical
    point the capsule position stands for, and the pinned calibration
    that establishes the acoustic centre."""

    model_config = ConfigDict(frozen=True)

    reference_id: str
    reference_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    point_kind: Literal[
        'acoustic_centre', 'diaphragm_center', 'protection_grid',
        'manufacturer_reference', 'capsule_tip', 'other', 'unknown',
    ]
    acoustic_centre_offset_m: tuple[float, float, float] | None = None
    offset_uncertainty_m: float | None = None
    calibration_ref: AuthorityRef | None = None
    frequency_dependent: bool | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('point_kind') not in _POINT_KINDS:
                raise ValueError('unknown reference-point kind')
            if data.get('point_kind') == 'unknown':
                raise ValueError(
                    'a receiver reference must declare which physical '
                    'point its position stands for'
                )
            kind = data.get('point_kind')
            if kind != 'acoustic_centre' and (
                data.get('acoustic_centre_offset_m') is None
                and data.get('offset_uncertainty_m') is None
            ):
                raise ValueError(
                    'a non-acoustic-centre reference must declare the '
                    'offset or its uncertainty (IEC 61094-8: the '
                    'difference is source-distance uncertainty)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'reference_id', 'reference_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ReceiverReferencePoint':
        return _seal(
            cls, payload, 'reference_id', 'reference_sha256', 'rref'
        )


class MicrophoneCapsulePose(BaseModel):
    """One capsule's surveyed pose bound to its declared reference
    point (#774) — position + orientation + the reference semantics
    that interpret it."""

    model_config = ConfigDict(frozen=True)

    pose_id: str
    pose_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    reference_ref: AuthorityRef
    position_m: tuple[float, float, float]
    orientation_deg: tuple[float, float, float] | None = None
    survey_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('reference_ref') is None:
                raise ValueError(
                    'a capsule pose requires a pinned reference-point '
                    'record'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'pose_id', 'pose_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'MicrophoneCapsulePose':
        return _seal(
            cls, payload, 'pose_id', 'pose_sha256', 'mcp'
        )


def evaluate_receiver_origin_claim(
    reference: ReceiverReferencePoint | None,
    pose: MicrophoneCapsulePose | None,
    *,
    precision_claim: str | None = None,
) -> tuple[ReceiverVerdict, str]:
    """Judge whether receiver geometry may carry a precision claim
    (#774)."""
    if reference is None:
        return (
            'reference_point_unqualified',
            'no declared receiver reference point — the measured '
            'position has no acoustic meaning',
        )
    if (
        reference.point_kind != 'acoustic_centre'
        and reference.offset_uncertainty_m is None
        and precision_claim in _PRECISION_CLAIMS
    ):
        return (
            'uncertainty_budget_required',
            f'{precision_claim}: non-acoustic-centre reference with '
            'no declared uncertainty — IEC 61094-8 treats the '
            'difference as source-distance uncertainty',
        )
    if pose is None:
        return (
            'reference_point_unqualified',
            'reference semantics declared but no capsule pose bound',
        )
    if (
        precision_claim in ('phase', 'early_reflection_delay')
        and reference.frequency_dependent is not True
    ):
        return (
            'acoustic_centre_conflation',
            'phase/delay claims need frequency-dependent acoustic-'
            'centre evidence — a fixed reference point is nominal',
        )
    return (
        'reference_qualified',
        'receiver reference point declared and pose bound',
    )


RECEIVER_LABELS: dict[str, str] = {
    'reference_qualified': '受信基準点適格',
    'reference_point_unqualified': '受信基準点未適格',
    'acoustic_centre_conflation': '音響中心混同',
    'uncertainty_budget_required': '不確かさ予算必要',
}
