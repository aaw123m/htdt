"""Video cadence and reference-listening-room authorities (issues
#777, #778).

A video path can negotiate the expected resolution/HDR mode and
still reproduce motion incorrectly — content frame cadence not
preserved, frames repeated/dropped, motion interpolation active,
fixed-refresh conversion, wrong VRR/QMS state, unstable
presentation timing (#777). And ITU-R BS.1116 / EBU Tech 3276
reference listening-room conditions are valuable controlled-
listening benchmarks but are NOT universal home-theater design
targets and must not be silently merged with CEDIA/CTA-RP22 (#778).
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


class CadenceDeliveryEvidence(BaseModel):
    """Declared frame-cadence delivery (#777) — content cadence,
    output refresh relationship, repeat/drop behaviour, VRR/QMS
    state and presentation-timing evidence; negotiated mode alone
    is not motion fidelity."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    content_cadence_kind: Literal[
        'film_24', 'broadcast_50', 'broadcast_60', 'mixed',
        'variable', 'unknown',
    ]
    refresh_relationship: Literal[
        'integer_multiple', 'vrr_matched', 'qms_switch',
        'fixed_conversion', 'unknown',
    ]
    motion_processing_declared: bool = False
    repeat_drop_observed: bool | None = None
    timing_evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('content_cadence_kind') not in (
                'film_24', 'broadcast_50', 'broadcast_60',
                'mixed', 'variable', 'unknown',
            ):
                raise ValueError('unknown content cadence kind')
            if data.get('content_cadence_kind') == 'unknown':
                raise ValueError(
                    'declare the content cadence — negotiated '
                    'mode is not cadence evidence'
                )
            if data.get('refresh_relationship') not in (
                'integer_multiple', 'vrr_matched', 'qms_switch',
                'fixed_conversion', 'unknown',
            ):
                raise ValueError('unknown refresh relationship')
            if data.get('refresh_relationship') == 'unknown':
                raise ValueError(
                    'declare the output refresh relationship'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'CadenceDeliveryEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'cax'
        )


class ReferenceRoomProfile(BaseModel):
    """Pinned controlled-listening reference profile (#778) —
    BS.1116 / EBU Tech 3276 style benchmark kept distinct from
    CEDIA/CTA-RP22 design targets; the framework is referenced,
    not merged."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    framework: Literal[
        'itu_bs1116', 'ebu_tech3276', 'other_standard', 'unknown',
    ]
    room_criteria_ref: AuthorityRef | None = None
    loudspeaker_criteria_ref: AuthorityRef | None = None
    listener_position_ref: AuthorityRef | None = None
    claimed_as_design_target: bool = False

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('framework') not in (
                'itu_bs1116', 'ebu_tech3276', 'other_standard',
                'unknown',
            ):
                raise ValueError('unknown reference framework')
            if data.get('framework') == 'unknown':
                raise ValueError(
                    'declare the reference framework'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ReferenceRoomProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'rrx'
        )


CadenceVerdict = Literal[
    'qualified_cadence',
    'negotiated_is_not_motion',
    'conversion_declared',
    'processing_active',
    'cadence_unqualified',
]


def evaluate_cadence_claim(
    evidence: CadenceDeliveryEvidence | None,
    *,
    mode_negotiated: bool = False,
) -> tuple[CadenceVerdict, str]:
    """Judge a motion/cadence claim (#777)."""
    if evidence is None:
        if mode_negotiated:
            return (
                'negotiated_is_not_motion',
                'negotiated resolution/HDR mode does not prove '
                'frame cadence preserved',
            )
        return ('cadence_unqualified', 'no cadence evidence')
    if evidence.motion_processing_declared:
        return (
            'processing_active',
            'motion interpolation/processing is active — '
            'source cadence is not being preserved',
        )
    if evidence.refresh_relationship == 'fixed_conversion':
        return (
            'conversion_declared',
            'fixed-refresh conversion — cadence preservation is '
            'a declared compromise, not a pass',
        )
    if evidence.repeat_drop_observed is True:
        return (
            'cadence_unqualified',
            'frame repeats/drops observed',
        )
    if evidence.timing_evidence_ref is None:
        return (
            'cadence_unqualified',
            'no presentation-timing evidence pinned',
        )
    return (
        'qualified_cadence',
        'cadence relationship declared with timing evidence',
    )


ReferenceVerdict = Literal[
    'qualified_reference',
    'reference_is_not_design_target',
    'profile_unqualified',
]


def evaluate_reference_claim(
    profile: ReferenceRoomProfile | None,
    *,
    cited_as_design_target: bool = False,
) -> tuple[ReferenceVerdict, str]:
    """Judge a reference-room claim (#778)."""
    if profile is None:
        return ('profile_unqualified', 'no profile pinned')
    if cited_as_design_target or profile.claimed_as_design_target:
        return (
            'reference_is_not_design_target',
            'BS.1116/EBU 3276 is a controlled-listening '
            'benchmark — not a universal home-theater design '
            'target; do not merge with CEDIA/CTA-RP22',
        )
    if (
        profile.room_criteria_ref is None
        or profile.loudspeaker_criteria_ref is None
        or profile.listener_position_ref is None
    ):
        return (
            'profile_unqualified',
            'room, loudspeaker and listener-position criteria '
            'refs are all required',
        )
    return (
        'qualified_reference',
        'reference framework pinned with criteria',
    )


CADENCE_LABELS: dict[str, str] = {
    'qualified_cadence': 'コーデンス適格',
    'negotiated_is_not_motion': 'ネゴシエーションは動画忠実度ではない',
    'conversion_declared': '変換宣言済み',
    'processing_active': 'モーション処理有効',
    'cadence_unqualified': 'コーデンス未検証',
}

REFERENCE_LABELS: dict[str, str] = {
    'qualified_reference': '参照室適格',
    'reference_is_not_design_target': '参照基準は設計目標ではない',
    'profile_unqualified': 'プロファイル未検証',
}
