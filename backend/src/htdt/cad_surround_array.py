"""Same-channel surround-array qualification authority (issue #737).

One logical surround channel feeding multiple physical loudspeakers is
NOT equivalent to one ideal loudspeaker with wider coverage — the array
can improve seat coverage while changing localization, comb filtering,
lobing, spectral balance, inter-seat consistency and object-rendering
behaviour.

Basis: CEDIA/CTA-RP22 v1.2 §5.6.2.1 — physical arrays (multiple speakers
sharing one processor output) reduce object localization and are not
recommended for object-based immersive reproduction; software-managed
arrays retain per-member level/delay and can be content-dependent
(discrete for immersive content, arrayed for channel-based content).
#690 supplies the coherent-source summation semantics — members
reproducing one correlated signal are never energy-summed as
independent noise sources.
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


ArrayTopology = Literal[
    'physical_shared_output_array',
    'independent_output_fixed_array',
    'software_managed_content_dependent_array',
    'distributed_channel_reproduction',
    'external_processor_array',
    'unknown',
]
RenderMode = Literal[
    'channel_5_1', 'channel_7_1', 'atmos_object', 'dtsx', 'auro_3d',
    'upmixed', 'legacy_fallback', 'unknown',
]
MemberBehavior = Literal[
    'discrete_members', 'arrayed_members', 'mixed', 'unknown',
]
MemberPolarity = Literal['normal', 'inverted', 'unknown']
LocalizationImpact = Literal[
    'none_observed', 'suspected', 'observed', 'unknown',
]
ArrayVerdict = Literal[
    'coverage_uniform',
    'spectral_limited',
    'localization_tradeoff',
    'seat_anomaly',
    'topology_limited',
    'unqualified',
]

TOPOLOGY_LABELS: dict[str, str] = {
    'physical_shared_output_array': '物理共有出力アレイ',
    'independent_output_fixed_array': '独立出力固定アレイ',
    'software_managed_content_dependent_array': 'ソフトウェア管理アレイ（コンテンツ依存）',
    'distributed_channel_reproduction': '分散チャンネル再生',
    'external_processor_array': '外部プロセッサアレイ',
    'unknown': '不明',
}
RENDER_MODE_LABELS: dict[str, str] = {
    'channel_5_1': '5.1 チャンネル',
    'channel_7_1': '7.1 チャンネル',
    'atmos_object': 'Atmos/オブジェクト',
    'dtsx': 'DTS:X',
    'auro_3d': 'Auro-3D',
    'upmixed': 'アップミックス',
    'legacy_fallback': 'レガシーフォールバック',
    'unknown': '不明',
}
BEHAVIOR_LABELS: dict[str, str] = {
    'discrete_members': 'メンバー個別再生',
    'arrayed_members': 'アレイ合成再生',
    'mixed': '混在',
    'unknown': '不明',
}
POLARITY_LABELS: dict[str, str] = {
    'normal': '正極性', 'inverted': '逆極性', 'unknown': '不明',
}
LOCALIZATION_LABELS: dict[str, str] = {
    'none_observed': '定位影響なし観測',
    'suspected': '定位影響の疑い',
    'observed': '定位影響観測あり',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'coverage_uniform': 'カバレッジ均一',
    'spectral_limited': 'スペクトル制限',
    'localization_tradeoff': '定位トレードオフ',
    'seat_anomaly': '席異常',
    'topology_limited': 'トポロジー制限',
    'unqualified': '未修飾',
}

_SHARED_OUTPUT = {'physical_shared_output_array'}


class ArrayMember(BaseModel):
    """One physical loudspeaker inside a same-channel array.

    ``independent_adjustment`` records whether the topology allows
    per-member level/delay/polarity/EQ — a shared-output array cannot
    be proposed per-speaker alignment it physically cannot implement.
    """

    model_config = ConfigDict(frozen=True)

    speaker_ref: AuthorityRef
    output_ref: AuthorityRef | None = None
    independent_adjustment: bool = True
    polarity: MemberPolarity = 'unknown'
    gain_db: float | None = None
    delay_ms: float | None = Field(default=None, ge=0)
    spacing_to_next_m: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def _validate(self) -> 'ArrayMember':
        _require_refs(self.speaker_ref)
        if self.output_ref is not None:
            _require_refs(self.output_ref)
        return self


class SameChannelSpeakerArray(BaseModel):
    """One logical channel reproduced by multiple physical loudspeakers.

    Physical shared-output members cannot declare independent trims:
    the wiring is the constraint, and proposing per-speaker alignment
    the topology cannot implement is rejected at save time.
    """

    model_config = ConfigDict(frozen=True)

    array_id: str
    array_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    logical_channel_ref: AuthorityRef
    topology: ArrayTopology = 'unknown'
    members: tuple[ArrayMember, ...]
    wiring_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SameChannelSpeakerArray':
        _require_refs(self.logical_channel_ref)
        if self.wiring_ref is not None:
            _require_refs(self.wiring_ref)
        if len(self.members) < 2:
            raise ValueError(
                'an array requires at least two physical members'
            )
        if self.topology in _SHARED_OUTPUT:
            for member in self.members:
                if member.independent_adjustment:
                    raise ValueError(
                        'a physical shared-output array cannot grant '
                        'per-member independent adjustment'
                    )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'array_id', 'array_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'SameChannelSpeakerArray':
        return _seal(cls, kwargs, 'array_id', 'array_sha256', 'scar')


class ArrayReproductionMode(BaseModel):
    """Content/codec-dependent array state (RP22 §5.6.2.1).

    The same hardware layout may be discrete under object mode and
    arrayed under channel-based content — those are different
    reproduction states requiring separate qualification.
    """

    model_config = ConfigDict(frozen=True)

    mode_id: str
    mode_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    array_ref: AuthorityRef
    render_mode: RenderMode = 'unknown'
    member_behavior: MemberBehavior = 'unknown'
    provider_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ArrayReproductionMode':
        _require_refs(self.array_ref)
        if self.provider_ref is not None:
            _require_refs(self.provider_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'mode_id', 'mode_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ArrayReproductionMode':
        return _seal(cls, kwargs, 'mode_id', 'mode_sha256', 'armd')


class ArrayAcousticQualification(BaseModel):
    """Listening-area qualification of one array/mode state.

    Coverage is evaluated across the listening area, not one seat:
    ``measured_seat_refs`` and ``holdout_seat_refs`` are required for a
    ``coverage_uniform`` verdict, and a coherent cancellation seat is
    never averaged away (``seat_anomaly``).
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    array_ref: AuthorityRef
    mode_ref: AuthorityRef | None = None
    measured_seat_refs: tuple[AuthorityRef, ...] = ()
    holdout_seat_refs: tuple[AuthorityRef, ...] = ()
    member_evidence_refs: tuple[AuthorityRef, ...] = ()
    summed_evidence_ref: AuthorityRef | None = None
    coherent_sum_ref: AuthorityRef | None = None
    level_spread_db: float | None = Field(default=None, ge=0)
    spectral_spread_db: float | None = Field(default=None, ge=0)
    timing_spread_ms: float | None = Field(default=None, ge=0)
    cancellation_seats: int = Field(default=0, ge=0)
    localization_impact: LocalizationImpact = 'unknown'
    verdict: ArrayVerdict = 'unqualified'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ArrayAcousticQualification':
        _require_refs(self.array_ref)
        for ref in (
            self.mode_ref,
            *self.measured_seat_refs,
            *self.holdout_seat_refs,
            *self.member_evidence_refs,
            self.summed_evidence_ref,
            self.coherent_sum_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ArrayAcousticQualification':
        return _seal(
            cls, kwargs, 'qualification_id', 'qualification_sha256',
            'arqu',
        )


def evaluate_array_qualification(
    array: SameChannelSpeakerArray,
    mode: ArrayReproductionMode | None,
    qual: ArrayAcousticQualification,
) -> tuple[ArrayVerdict, tuple[str, ...]]:
    """Fail-closed array qualification verdict.

    Single-seat measurement cannot claim whole-area coverage
    (``unqualified``); a coherent-cancellation seat is surfaced as
    ``seat_anomaly`` rather than averaged away; arrayed object-mode
    reproduction records the RP22 ``localization_tradeoff``;
    a physical shared-output array never earns a verdict implying
    per-member control it cannot implement.
    """
    reasons: list[str] = []
    if array.topology == 'unknown':
        return 'unqualified', ('topology_unknown',)
    n_members = len(array.members)
    if len(qual.member_evidence_refs) not in (0, n_members):
        reasons.append('member_evidence_incomplete')
    if not qual.measured_seat_refs:
        return 'unqualified', ('no_seat_measurements',)
    if qual.summed_evidence_ref is None:
        return 'unqualified', ('no_summed_field_evidence',)
    if len(qual.measured_seat_refs) < 2 or not qual.holdout_seat_refs:
        return 'unqualified', ('listening_area_not_covered',)
    if qual.cancellation_seats > 0:
        return 'seat_anomaly', ('coherent_cancellation_seat',)
    if mode is not None and (
        mode.render_mode in ('atmos_object', 'dtsx', 'auro_3d')
        and mode.member_behavior == 'arrayed_members'
    ):
        return 'localization_tradeoff', ('object_mode_arrayed',)
    if qual.localization_impact == 'observed':
        return 'localization_tradeoff', ('localization_impact_observed',)
    if (
        qual.spectral_spread_db is not None
        and qual.level_spread_db is not None
        and qual.spectral_spread_db > 2.0 * qual.level_spread_db
    ):
        reasons.append('spectral_nonuniformity')
        return 'spectral_limited', tuple(reasons)
    if qual.localization_impact == 'suspected':
        return 'localization_tradeoff', ('localization_impact_suspected',)
    return 'coverage_uniform', tuple(reasons)


def assert_member_adjustment(
    array: SameChannelSpeakerArray,
    member_index: int,
) -> None:
    """Topology gate: per-member trims only where the wiring allows."""
    member = array.members[member_index]
    if array.topology in _SHARED_OUTPUT or not member.independent_adjustment:
        raise ValueError(
            'per-member level/delay/polarity adjustment is not '
            'implementable on this array topology'
        )
