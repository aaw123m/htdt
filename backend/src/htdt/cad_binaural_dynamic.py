"""Dynamic head-tracked binaural auralization authority (issue #727).

A static BRIR/HRTF render can be technically correct while a
head-tracked dynamic render is spatially wrong: listener-pose frame,
tracker latency, update cadence, pose prediction, HRTF/BRIR angular
sampling and motion-to-sound synchronization must each be qualified.
Head movement itself provides localization cues — a static render is
not a valid substitute for a dynamic claim.

Basis: Begault, Wenzel & Anderson (JAES 2001) — head tracking,
individualized HRTFs and reverberation significantly affect
localization errors, reversals and externalization; head-tracker
delay degrades auditory tracking ('slippage'); HRTF class matters
under dynamic rendering (AES 2025).
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


_HRTF_CLASSES = ('individual', 'estimated', 'generic', 'unknown')

BinauralVerdict = Literal[
    'qualified_dynamic',
    'pose_frame_unqualified',
    'tracker_latency_unmeasured',
    'angular_sampling_unqualified',
    'static_is_not_dynamic',
    'stale_pose_evidence',
]


class DynamicBinauralSession(BaseModel):
    """Declared head-tracked render session (#727) — pose frame,
    tracker and render binding. A dynamic claim requires each bound
    component pinned."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    session_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    hrtf_class: Literal['individual', 'estimated', 'generic', 'unknown']
    hrtf_ref: AuthorityRef | None = None
    pose_frame_descriptor: str | None = None
    update_rate_hz: float | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('hrtf_class') not in _HRTF_CLASSES:
                raise ValueError('unknown HRTF class')
            if data.get('hrtf_class') == 'unknown':
                raise ValueError(
                    'a dynamic binaural session must declare its HRTF '
                    'class — generic and individual differ'
                )
            rate = data.get('update_rate_hz')
            if rate is not None and rate <= 0:
                raise ValueError('update rate must be positive')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'DynamicBinauralSession':
        return _seal(
            cls, payload, 'session_id', 'session_sha256', 'dbin'
        )


class PoseTrackingEvidence(BaseModel):
    """Measured tracker/pose evidence (#727) — motion-to-audio latency
    and update cadence. Tracker delay degrades localization
    ('slippage'); an unmeasured latency is not zero."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    motion_to_audio_latency_ms: float | None = None
    update_rate_hz: float | None = None
    latency_measurement_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('session_ref') is None:
                raise ValueError(
                    'pose evidence requires a pinned session'
                )
            if data.get('motion_to_audio_latency_ms') is not None and (
                data.get('latency_measurement_ref') is None
            ):
                raise ValueError(
                    'a declared latency value requires a measurement '
                    'reference'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'PoseTrackingEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'ptrk'
        )


class BinauralQualification(BaseModel):
    """Verdict record for a dynamic session (#727) — angular sampling
    adequacy and localization evidence binding."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    angular_sampling_ref: AuthorityRef | None = None
    localization_evidence_ref: AuthorityRef | None = None
    verdict: Literal[
        'qualified', 'partial', 'unqualified',
    ]

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('session_ref') is None:
                raise ValueError(
                    'a binaural qualification requires a pinned '
                    'session'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'BinauralQualification':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256',
            'bqual',
        )


def evaluate_binaural_claim(
    session: DynamicBinauralSession | None,
    pose: PoseTrackingEvidence | None,
    qualification: BinauralQualification | None,
    *,
    claimed_dynamic: bool = True,
    static_render_ref: AuthorityRef | None = None,
) -> tuple[BinauralVerdict, str]:
    """Judge a dynamic-binaural claim (#727)."""
    if session is None:
        return (
            'pose_frame_unqualified',
            'no declared dynamic session — pose frame, HRTF class and '
            'cadence are unqualified',
        )
    if claimed_dynamic and pose is None:
        if static_render_ref is not None:
            return (
                'static_is_not_dynamic',
                'a static BRIR render cannot stand for a head-tracked '
                'dynamic claim — head movement itself carries '
                'localization cues',
            )
        return (
            'pose_frame_unqualified',
            'dynamic claim without pose-tracking evidence',
        )
    if pose is not None:
        if pose.motion_to_audio_latency_ms is None:
            return (
                'tracker_latency_unmeasured',
                'motion-to-audio latency unmeasured — tracker delay '
                'degrades localization (slippage)',
            )
    if qualification is None:
        return (
            'angular_sampling_unqualified',
            'no qualification record — HRTF/BRIR angular sampling '
            'adequacy unknown',
        )
    if qualification.angular_sampling_ref is None:
        return (
            'angular_sampling_unqualified',
            'angular sampling evidence not pinned',
        )
    if qualification.verdict != 'qualified':
        return (
            'stale_pose_evidence',
            f'qualification verdict: {qualification.verdict}',
        )
    return (
        'qualified_dynamic',
        'dynamic session with measured latency and qualified angular '
        'sampling',
    )


BINAURAL_LABELS: dict[str, str] = {
    'qualified_dynamic': '適格動的バイノーラル',
    'pose_frame_unqualified': '姿勢フレーム未適格',
    'tracker_latency_unmeasured': 'トラッカー遅延未測定',
    'angular_sampling_unqualified': '角度サンプリング未適格',
    'static_is_not_dynamic': '静的レンダーは動的ではない',
    'stale_pose_evidence': '姿勢証拠陳腐',
}
