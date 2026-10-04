"""Guided video commissioning journey authority (#541).

Composes the existing video authorities — never a second color model:

- :class:`VideoColorTargetProfile` / :class:`VideoColorMeasurementSet` /
  :func:`evaluate_video_color` (#647) supply target semantics, XYZ evidence
  and per-group pass/fail truth;
- :class:`PhotometricEvaluation` (#557) supplies luminance/contrast
  evidence for the projection path;
- :class:`CorrectionCompatibility` /
  :class:`ColorimeterCorrectionProfile` (#1063) pin which meter correction
  applies and prove it was evaluated;
- :class:`AppliedCalibrationState` records "what the device was set to"
  separately from measured truth.

This module adds the orchestration records the journey needs:

- :class:`GuidedVideoCommissioningSession` — an immutable, hash-pinned
  binding of surface, display profile, picture mode, target, mode
  (SDR/HDR), signal semantics, instrument and correction. Changing any
  binding axis means building a new session (``supersedes_session_id``
  carries the lineage) — prior evidence is never rewritten.
- :func:`evaluate_video_readiness` — the explicit READY /
  READY_WITH_LIMITATIONS / INCOMPATIBLE / INSUFFICIENT_EVIDENCE gate
  shown before analysis. Missing signal-path or correction evidence is
  reported, never manufactured.
- :func:`diagnose_video_measurement` — folds
  :func:`evaluate_video_color` (and an optional photometric evaluation)
  into typed findings with plain-language Japanese explanations.
- :func:`propose_video_actions` — bounded corrective actions citing the
  exact evidence that triggered them; ``no_safe_action_from_current_
  evidence`` when the record cannot justify one.
- :class:`VideoOperatorAdjustment` + :class:`VideoBeforeAfterComparison`
  — the append-only iteration chain: measurement → selected action →
  operator adjustment → re-measurement → deltas. Comparisons of
  incompatible sets return ``incomparable`` with reasons.

A generated test pattern (#1075) is a stimulus artifact — sessions carry
``pattern_references`` for the exact pattern identities launched, but
patterns never count as measurement evidence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_colorimetry import (
    AppliedCalibrationState,
    ColorCriterionResult,
    ColorimeterCorrectionProfile,
    ColorMetricFamily,
    StimulusDefinition,
    TristimulusSample,
    VideoColorEvaluation,
    VideoColorMeasurementSet,
    VideoColorTargetProfile,
    _sample_group,
    evaluate_video_color,
)
from .cad_meter_correction import CorrectionCompatibility
from .cad_photometric import PhotometricEvaluation
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


VideoCommissioningMode = Literal['sdr', 'hdr']
"""The two distinct diagnosis semantics. ``sdr`` and ``hdr`` sessions pin
different readiness axes, step plans and action vocabularies — HDR is never
"SDR analysis with a different label"."""

VideoCommissioningStatus = Literal['open', 'completed', 'abandoned']

VideoReadinessState = Literal[
    'READY', 'READY_WITH_LIMITATIONS', 'INCOMPATIBLE', 'INSUFFICIENT_EVIDENCE'
]

VideoSurfaceKind = Literal['direct_view', 'projection']

VideoSignalRange = Literal['narrow', 'full', 'unknown']

VideoActionKind = Literal[
    'check_signal_range',
    'adjust_black_level',
    'adjust_white_level',
    'adjust_gamma',
    'adjust_eotf',
    'adjust_white_balance_low',
    'adjust_white_balance_high',
    'adjust_cms_primary',
    'adjust_cms_secondary',
    'reduce_clipping',
    'remeasure',
    'no_safe_action_from_current_evidence',
]

VideoFindingKind = Literal[
    'white_point',
    'grayscale',
    'eotf_tracking',
    'peak_luminance',
    'gamut',
    'peak_white_luminance',
    'black_floor',
    'on_off_contrast',
]

VideoComparisonStatus = Literal['comparable', 'incomparable']

VideoDeltaDirection = Literal[
    'improved', 'regressed', 'inconclusive', 'unknown'
]

VideoJourneyStepStatus = Literal['done', 'current', 'pending', 'blocked']


# ---------------------------------------------------------------------------
# JA labels for the derived records + surfaces
# ---------------------------------------------------------------------------

READINESS_STATE_LABELS: dict[str, str] = {
    'READY': '測定できます',
    'READY_WITH_LIMITATIONS': '条件付きで測定できます',
    'INCOMPATIBLE': '現在の条件では評価できません',
    'INSUFFICIENT_EVIDENCE': '証拠が不足しています',
}

SESSION_STATUS_LABELS: dict[str, str] = {
    'open': '進行中',
    'completed': '完了',
    'abandoned': '中止',
}

SURFACE_KIND_LABELS: dict[str, str] = {
    'direct_view': '直視ディスプレイ',
    'projection': 'プロジェクター + スクリーン',
}

MODE_LABELS: dict[str, str] = {
    'sdr': 'SDR',
    'hdr': 'HDR',
}

SIGNAL_RANGE_LABELS: dict[str, str] = {
    'narrow': 'ナローレンジ（リミテッド）',
    'full': 'フルレンジ',
    'unknown': '不明',
}

ACTION_KIND_LABELS: dict[str, str] = {
    'check_signal_range': '信号レンジを確認する',
    'adjust_black_level': '黒レベル（ブライトネス）を調整する',
    'adjust_white_level': '白レベル（コントラスト）を調整する',
    'adjust_gamma': 'ガンマを調整する',
    'adjust_eotf': 'EOTF/トーンマッピング関連の調整を検討する',
    'adjust_white_balance_low': '低輝度側のホワイトバランスを調整する',
    'adjust_white_balance_high': '高輝度側のホワイトバランスを調整する',
    'adjust_cms_primary': 'CMSの原色（R/G/B）を調整する',
    'adjust_cms_secondary': 'CMSの補色（C/M/Y）を調整する',
    'reduce_clipping': '白つぶれを抑える（クリッピング低減）',
    'remeasure': '再測定する',
    'no_safe_action_from_current_evidence': '現在の証拠から安全な操作は提案できません',
}

FINDING_KIND_LABELS: dict[str, str] = {
    'white_point': '白色点（色温度）',
    'grayscale': 'グレースケール（RGBバランス）',
    'eotf_tracking': 'ガンマ/EOTF追従',
    'peak_luminance': 'ピーク輝度',
    'gamut': '色域（原色・補色）',
    'peak_white_luminance': 'ピーク白輝度（実測 vs 予測）',
    'black_floor': '黒床輝度（実測 vs 予測）',
    'on_off_contrast': '全白/全黒コントラスト（実測 vs 予測）',
}

_DELTA_DIRECTION_LABELS: dict[str, str] = {
    'improved': '改善',
    'regressed': '悪化',
    'inconclusive': '判定不能',
    'unknown': '不明',
}

# Gamut stimulus ids the existing authority understands, keyed to whether the
# sample is a primary or secondary — used to scope corrective actions without
# inventing a per-primary diagnosis the aggregated group never produced.
_PRIMARY_STIMULUS_PREFIXES = ('r', 'g', 'b', 'red', 'green', 'blue')
_SECONDARY_STIMULUS_PREFIXES = ('c', 'm', 'y')


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class VideoCommissioningStep(BaseModel):
    """One step of the guided plan bound into the session at creation.

    The plan pins the *order and vocabulary* of the journey; each step's
    live status is re-derived from persisted evidence every time the
    session is read — a plan row is a promise, never a checkmark.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    index: int = Field(ge=0)
    title: str = Field(min_length=1)
    required: bool = True


class GuidedVideoCommissioningSession(BaseModel):
    """One bound video-commissioning session — immutable evidence anchor.

    All binding axes are semantic-hash inputs: exact surface, display
    profile, picture mode, target profile identity+hash, SDR/HDR mode,
    signal semantics, instrument, correction and stimulus path. ``session_-
    sha256`` covers the whole record, so a "same session" check is a hash
    check, and any axis change is a new session, not an edit.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['guided-video-commissioning-1'] = (
        'guided-video-commissioning-1'
    )
    session_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    surface_entity_id: str = Field(min_length=1)
    surface_kind: VideoSurfaceKind
    display_specification_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    projector_image_profile_id: str | None = None
    projector_image_profile_version: str | None = None
    projector_image_profile_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    presentation_profile_id: str | None = None
    presentation_profile_version: str | None = None
    presentation_profile_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    picture_mode: str | None = None
    scene_revision_id: str | None = None
    scene_content_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    mode: VideoCommissioningMode
    signal_range: VideoSignalRange = 'unknown'
    bit_depth: int | None = Field(default=None, ge=8)
    encoding: str = 'unknown'
    target_id: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    target_sha256: str = Field(min_length=16)
    meter: str = Field(min_length=1)
    meter_correction: ColorimeterCorrectionProfile | None = None
    correction_compatibility_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    stimulus: StimulusDefinition = Field(default_factory=StimulusDefinition)
    pattern_references: tuple[str, ...] = ()
    ambient_reference: str | None = None
    steps: tuple[VideoCommissioningStep, ...] = ()
    status: VideoCommissioningStatus = 'open'
    supersedes_session_id: str | None = None
    created_at_utc: str = Field(min_length=1)
    completed_at_utc: str | None = None
    session_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'session_sha256', 'session_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'GuidedVideoCommissioningSession':
        digest = _hash(self.semantic_payload())
        if self.session_sha256 != digest:
            raise ValueError(
                'guided video commissioning session semantic hash mismatch'
            )
        if self.session_id != 'vcs-' + digest[:24]:
            raise ValueError(
                'guided video commissioning session id mismatch'
            )
        if self.status != 'open' and self.completed_at_utc is None:
            raise ValueError(
                'a completed/abandoned session needs completed_at_utc'
            )
        if self.status == 'open' and self.completed_at_utc is not None:
            raise ValueError(
                'an open session cannot carry completed_at_utc'
            )
        triples = (
            (
                self.projector_image_profile_id,
                self.projector_image_profile_version,
                self.projector_image_profile_sha256,
            ),
            (
                self.presentation_profile_id,
                self.presentation_profile_version,
                self.presentation_profile_sha256,
            ),
        )
        for identity, version, digest_ in triples:
            bound = (
                identity is not None,
                version is not None,
                digest_ is not None,
            )
            if any(bound) and not all(bound):
                raise ValueError(
                    'profile bindings must supply id, version and sha256 '
                    'together'
                )
        return self


_SDR_STEP_PLAN: tuple[tuple[str, str], ...] = (
    ('signal_range', '信号レンジと白/黒つぶれの準備'),
    ('baseline', 'ベースライン測定（輝度・黒・コントラスト）'),
    ('grayscale', 'グレースケール / ガンマ'),
    ('white_point', '白色点 / 色温度'),
    ('rgb_balance', 'RGBバランス'),
    ('gamut', '色域（原色・補色）'),
    ('sweeps', '彩度スイープ / カラーチェッカー（利用できる場合）'),
    ('verify', '最終確認測定'),
)

_HDR_STEP_PLAN: tuple[tuple[str, str], ...] = (
    ('signal_range', '信号レンジ・ビット深度・色度方式の確認'),
    ('hdr_binding', 'HDR方式（PQ/HLG）と表示モードの確認'),
    ('baseline', 'ベースライン測定（ピーク輝度・黒）'),
    ('eotf', 'EOTF追従 / トーンマッピング'),
    ('white_point', '白色点 / 色温度'),
    ('rgb_balance', 'RGBバランス'),
    ('gamut', '色域（BT.2020コンテナ内の実測）'),
    ('verify', '最終確認測定'),
)


def _step_plan(mode: VideoCommissioningMode) -> tuple[VideoCommissioningStep, ...]:
    plan = _HDR_STEP_PLAN if mode == 'hdr' else _SDR_STEP_PLAN
    return tuple(
        VideoCommissioningStep(key=key, index=index, title=title)
        for index, (key, title) in enumerate(plan)
    )


def build_guided_video_session(
    *,
    document_id: str,
    surface_entity_id: str,
    surface_kind: VideoSurfaceKind,
    mode: VideoCommissioningMode,
    target_id: str,
    target_version: str,
    target_sha256: str,
    meter: str,
    signal_range: VideoSignalRange = 'unknown',
    bit_depth: int | None = None,
    encoding: str = 'unknown',
    meter_correction: ColorimeterCorrectionProfile | None = None,
    correction_compatibility_sha256: str | None = None,
    display_specification_sha256: str | None = None,
    projector_image_profile_id: str | None = None,
    projector_image_profile_version: str | None = None,
    projector_image_profile_sha256: str | None = None,
    presentation_profile_id: str | None = None,
    presentation_profile_version: str | None = None,
    presentation_profile_sha256: str | None = None,
    picture_mode: str | None = None,
    scene_revision_id: str | None = None,
    scene_content_sha256: str | None = None,
    stimulus: StimulusDefinition | None = None,
    pattern_references: tuple[str, ...] = (),
    ambient_reference: str | None = None,
    supersedes_session_id: str | None = None,
    status: VideoCommissioningStatus = 'open',
    created_at_utc: str | None = None,
    completed_at_utc: str | None = None,
) -> GuidedVideoCommissioningSession:
    """Build one bound session. ``created_at_utc`` participates in the hash so
    two sessions opened at different times never collapse into one row."""

    probe = GuidedVideoCommissioningSession.model_construct(
        **canonicalize_payload(GuidedVideoCommissioningSession, dict(
            session_id='',
            document_id=document_id,
            surface_entity_id=surface_entity_id,
            surface_kind=surface_kind,
            display_specification_sha256=display_specification_sha256,
            projector_image_profile_id=projector_image_profile_id,
            projector_image_profile_version=projector_image_profile_version,
            projector_image_profile_sha256=projector_image_profile_sha256,
            presentation_profile_id=presentation_profile_id,
            presentation_profile_version=presentation_profile_version,
            presentation_profile_sha256=presentation_profile_sha256,
            picture_mode=picture_mode,
            scene_revision_id=scene_revision_id,
            scene_content_sha256=scene_content_sha256,
            mode=mode,
            signal_range=signal_range,
            bit_depth=bit_depth,
            encoding=encoding,
            target_id=target_id,
            target_version=target_version,
            target_sha256=target_sha256,
            meter=meter,
            meter_correction=meter_correction,
            correction_compatibility_sha256=correction_compatibility_sha256,
            stimulus=stimulus if stimulus is not None else StimulusDefinition(),
            pattern_references=tuple(pattern_references),
            ambient_reference=ambient_reference,
            steps=_step_plan(mode),
            status=status,
            supersedes_session_id=supersedes_session_id,
            created_at_utc=created_at_utc or _utc_now(),
            completed_at_utc=completed_at_utc,
            session_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return GuidedVideoCommissioningSession(
        **probe.model_dump(
            mode='python', exclude={'session_sha256', 'session_id'}
        ),
        session_id='vcs-' + digest[:24],
        session_sha256=digest,
    )


def rebind_video_session(
    session: GuidedVideoCommissioningSession,
    **changes: Any,
) -> GuidedVideoCommissioningSession:
    """A binding change produces a NEW session that supersedes the old one —
    never an in-place edit. ``changes`` maps field names to new values."""

    payload = session.model_dump(mode='python')
    payload.pop('session_id', None)
    payload.pop('session_sha256', None)
    payload['supersedes_session_id'] = session.session_id
    for key, value in changes.items():
        if key not in payload:
            raise ValueError(f'unknown session field for rebind: {key}')
        payload[key] = value
    # created_at stays the original axis — the supersede lineage carries the
    # timeline; a new wall-clock stamp is still fine to pass in changes.
    payload.setdefault('created_at_utc', _utc_now())
    probe = GuidedVideoCommissioningSession.model_construct(
        **canonicalize_payload(GuidedVideoCommissioningSession, payload)
    )
    digest = _hash(probe.semantic_payload())
    return GuidedVideoCommissioningSession(
        **probe.model_dump(
            mode='python', exclude={'session_sha256', 'session_id'}
        ),
        session_id='vcs-' + digest[:24],
        session_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Readiness gate
# ---------------------------------------------------------------------------


class VideoReadinessCheck(BaseModel):
    """One readiness axis. ``blocking`` marks the checks whose failure means
    the journey cannot honestly proceed at all (vs. UNKNOWN limitations
    surfaced but not blocking)."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    status: EvaluationStatus
    blocking: bool = True
    title: str = Field(min_length=1)
    detail: str | None = None


class VideoReadinessReport(BaseModel):
    """The gate evaluated against a session's exact bindings."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    report_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(min_length=16)
    state: VideoReadinessState
    checks: tuple[VideoReadinessCheck, ...]
    evaluated_at_utc: str = Field(min_length=1)
    report_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'report_sha256', 'report_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoReadinessReport':
        digest = _hash(self.semantic_payload())
        if self.report_sha256 != digest:
            raise ValueError('video readiness report semantic hash mismatch')
        if self.report_id != 'vrr-' + digest[:24]:
            raise ValueError('video readiness report id mismatch')
        return self


def _check(
    check_id: str,
    status: EvaluationStatus,
    title: str,
    detail: str | None = None,
    *,
    blocking: bool = True,
) -> VideoReadinessCheck:
    return VideoReadinessCheck(
        check_id=check_id,
        status=status,
        blocking=blocking,
        title=title,
        detail=detail,
    )


def evaluate_video_readiness(
    session: GuidedVideoCommissioningSession,
    *,
    target: VideoColorTargetProfile | None = None,
    correction_compatibility: CorrectionCompatibility | None = None,
    measurement_sets: tuple[VideoColorMeasurementSet, ...] = (),
    evaluated_at_utc: str | None = None,
) -> VideoReadinessReport:
    """Evaluate the §3 readiness gate for a bound session.

    ``target``/``correction_compatibility``/``measurement_sets`` are the
    resolved authorities, not just ids — absent authorities surface as
    UNKNOWN/INSUFFICIENT rather than being assumed compatible.  A journey
    typically collects several sets (e.g. a grayscale export plus a
    primaries export); required fields are evaluated on the union of all
    session-linked sets.
    """

    checks: list[VideoReadinessCheck] = []

    checks.append(
        _check(
            'display_bound',
            'PASS' if session.surface_entity_id else 'FAIL',
            '対象の表示機器（画面/スクリーン）',
            (
                'セッションは表示面 '
                f'{session.surface_entity_id} に結び付いています'
                if session.surface_entity_id
                else '対象の表示面が結び付いていません'
            ),
        )
    )

    profile_bound = (
        session.presentation_profile_id is not None
        or session.picture_mode is not None
        or session.projector_image_profile_id is not None
    )
    checks.append(
        _check(
            'picture_profile',
            'PASS' if profile_bound else 'FAIL',
            '映像モード / ピクチャーメモリ',
            (
                '映像モードは明示されています'
                if profile_bound
                else '映像モード（ピクチャーメモリ/プリセット）が未指定です'
            ),
        )
    )

    checks.append(
        _check(
            'mode_explicit',
            'PASS',
            'SDR / HDR モード',
            f'モード: {MODE_LABELS.get(session.mode, session.mode)}',
        )
    )

    if target is None:
        checks.append(
            _check(
                'target_bound',
                'FAIL',
                '測定ターゲット',
                'ターゲットプロファイルを解決できませんでした'
                '（セッションが参照する権威が存在しません）',
            )
        )
        checks.append(
            _check(
                'eotf_explicit',
                'UNKNOWN',
                '色空間 / EOTF / ガンマの意味',
                'ターゲットが無いため EOTF 意味を確認できません',
            )
        )
    else:
        hash_matches = (
            target.target_id == session.target_id
            and target.version == session.target_version
            and target.target_sha256 == session.target_sha256
        )
        checks.append(
            _check(
                'target_bound',
                'PASS' if hash_matches else 'FAIL',
                '測定ターゲット',
                (
                    f'ターゲット {session.target_id} v{session.target_version} '
                    'に結び付いています'
                    if hash_matches
                    else 'ターゲットの id/バージョン/ハッシュがセッションの'
                    '結び付けと一致しません'
                ),
            )
        )
        if target.eotf == 'unknown':
            checks.append(
                _check(
                    'eotf_explicit',
                    'FAIL',
                    '色空間 / EOTF / ガンマの意味',
                    'ターゲットの EOTF が記録されていません',
                )
            )
        elif target.eotf == 'other':
            checks.append(
                _check(
                    'eotf_explicit',
                    'PASS',
                    '色空間 / EOTF / ガンマの意味',
                    f'独自カーブ: {target.eotf_label} — 既知の標準ではないため'
                    '限定的な評価になります',
                    blocking=False,
                )
            )
        else:
            checks.append(
                _check(
                    'eotf_explicit',
                    'PASS',
                    '色空間 / EOTF / ガンマの意味',
                    f'EOTF: {target.eotf}',
                )
            )

    if session.signal_range == 'unknown' or session.encoding == 'unknown':
        checks.append(
            _check(
                'signal_range_known',
                'UNKNOWN',
                '信号レンジ / 色度方式',
                '信号レンジや色度方式が不明です — 仮定はせず '
                'UNKNOWN のまま扱います',
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                'signal_range_known',
                'PASS',
                '信号レンジ / 色度方式',
                f'レンジ: {SIGNAL_RANGE_LABELS.get(session.signal_range)} / '
                f'符号: {session.encoding}',
            )
        )

    checks.append(
        _check(
            'instrument_bound',
            'PASS' if session.meter else 'FAIL',
            '測定器',
            (
                f'測定器: {session.meter}'
                if session.meter
                else '測定器が記録されていません'
            ),
        )
    )

    correction_bound = session.meter_correction is not None
    if not correction_bound:
        checks.append(
            _check(
                'correction_evaluated',
                'UNKNOWN',
                'メーター補正の適合性',
                '補正（CCMX/CCSS）が結び付いていません — '
                '未補正の読み値として評価します',
                blocking=False,
            )
        )
    elif correction_compatibility is None:
        checks.append(
            _check(
                'correction_evaluated',
                'UNKNOWN',
                'メーター補正の適合性',
                '補正が結び付いていますが、適合性の評価権威が見つかりません',
            )
        )
    elif correction_compatibility.readiness == 'ready':
        checks.append(
            _check(
                'correction_evaluated',
                'PASS',
                'メーター補正の適合性',
                '補正はこのメーター/表示面の組み合わせに適合します',
            )
        )
    elif correction_compatibility.readiness == 'limited':
        checks.append(
            _check(
                'correction_evaluated',
                'UNKNOWN',
                'メーター補正の適合性',
                '補正は部分的にのみ適合します: '
                + ' / '.join(correction_compatibility.reasons),
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                'correction_evaluated',
                'FAIL',
                'メーター補正の適合性',
                '補正はこの組み合わせに適合しません: '
                + ' / '.join(correction_compatibility.reasons),
            )
        )

    if not measurement_sets or session.meter_correction is None:
        checks.append(
            _check(
                'correction_single_application',
                'NOT_APPLICABLE' if session.meter_correction is None else 'UNKNOWN',
                '補正の二重適用防止',
                (
                    '補正なし — 二重適用の余地がありません'
                    if session.meter_correction is None
                    else '測定セットが無いため二重適用を確認できません'
                ),
                blocking=False,
            )
        )
    else:
        set_corrections = [
            s.meter_correction for s in measurement_sets
        ]
        if any(c is None for c in set_corrections):
            checks.append(
                _check(
                    'correction_single_application',
                    'UNKNOWN',
                    '補正の二重適用防止',
                    '測定セットが補正の適用位置を記録していません',
                    blocking=False,
                )
            )
        else:
            same_correction = all(
                c.correction_id == session.meter_correction.correction_id
                and c.version == session.meter_correction.version
                for c in set_corrections
            )
            checks.append(
                _check(
                    'correction_single_application',
                    'PASS' if same_correction else 'FAIL',
                    '補正の二重適用防止',
                    (
                        'セッションと測定セットが同一の補正を一度だけ参照します'
                        if same_correction
                        else 'セッションと測定セットが異なる補正を参照します — '
                        '二重適用の可能性があります'
                    ),
                )
            )

    if session.surface_kind == 'projection':
        checks.append(
            _check(
                'measurement_geometry',
                'UNKNOWN',
                '測定ジオメトリ',
                'プロジェクション — メーターがスクリーン反射光を向いているかは'
                '実機で確認してください（記録からは検証できません）',
                blocking=False,
            )
        )
    else:
        checks.append(
            _check(
                'measurement_geometry',
                'PASS',
                '測定ジオメトリ',
                '直視ディスプレイ — 直接測光',
            )
        )

    if not measurement_sets:
        checks.append(
            _check(
                'measurement_fields',
                'UNKNOWN',
                '要求される測定項目',
                '測定セットがまだ取り込まれていません',
            )
        )
    elif target is None:
        checks.append(
            _check(
                'measurement_fields',
                'UNKNOWN',
                '要求される測定項目',
                'ターゲットが無いため必須項目を評価できません',
            )
        )
    else:
        all_samples = [
            s for ms in measurement_sets for s in ms.samples
        ]
        groups_present = {
            _sample_group_of(s.stimulus_id) for s in all_samples
        }
        needed: list[str] = []
        tol = target.tolerances
        if tol.white_point_delta_e is not None and 'white_point' not in groups_present:
            needed.append('白色点')
        if tol.grayscale_delta_e is not None and 'grayscale' not in groups_present:
            needed.append('グレースケール')
        if tol.gamut_delta_e is not None and 'gamut' not in groups_present:
            needed.append('色域')
        if tol.eotf_deviation_fraction is not None and not any(
            s.stimulus_level is not None for s in all_samples
        ):
            needed.append('刺激レベル付きサンプル')
        if needed:
            checks.append(
                _check(
                    'measurement_fields',
                    'UNKNOWN',
                    '要求される測定項目',
                    'ターゲットが要求する項目の測定がありません: '
                    + '、'.join(needed),
                )
            )
        else:
            checks.append(
                _check(
                    'measurement_fields',
                    'PASS',
                    '要求される測定項目',
                    'ターゲットが評価できる測定項目が揃っています',
                )
            )

    blocking_fail = any(
        c.status == 'FAIL' and c.blocking for c in checks
    )
    has_unknown = any(c.status == 'UNKNOWN' for c in checks)
    if blocking_fail:
        state: VideoReadinessState = 'INCOMPATIBLE'
    elif has_unknown:
        evidence_unknown = any(
            c.status == 'UNKNOWN'
            and c.check_id in ('measurement_fields', 'correction_evaluated')
            and c.blocking
            for c in checks
        )
        state = (
            'INSUFFICIENT_EVIDENCE' if evidence_unknown else 'READY_WITH_LIMITATIONS'
        )
    else:
        state = 'READY'

    probe = VideoReadinessReport.model_construct(
        **canonicalize_payload(VideoReadinessReport, dict(
            report_id='',
            session_id=session.session_id,
            session_sha256=session.session_sha256,
            state=state,
            checks=tuple(checks),
            evaluated_at_utc=evaluated_at_utc or _utc_now(),
            report_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return VideoReadinessReport(
        **probe.model_dump(mode='python', exclude={'report_sha256', 'report_id'}),
        report_id='vrr-' + digest[:24],
        report_sha256=digest,
    )


def _sample_group_of(stimulus_id: str) -> str:
    # The existing #647 authority owns the stimulus-id → criterion-group
    # mapping; this is the same lookup, re-exported for the journey layer.
    return _sample_group(stimulus_id)


# ---------------------------------------------------------------------------
# Diagnosis — typed findings + JA explanations
# ---------------------------------------------------------------------------


class VideoDiagnosisFinding(BaseModel):
    """One per-group/per-criterion finding with evidence and JA explanation."""

    model_config = ConfigDict(frozen=True)

    kind: VideoFindingKind
    verdict: EvaluationStatus
    metric: str | None = None
    observed: float | None = None
    target_value: float | None = None
    deviation: float | None = None
    measured_points: int = 0
    evidence_note: str | None = None
    explanation: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()


class VideoCommissioningDiagnosis(BaseModel):
    """Derived per-measurement diagnosis. ``diagnosis_id`` is a content hash —
    re-diagnosing the same inputs reproduces the same record (idempotent
    persistence)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    diagnosis_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(min_length=16)
    measurement_set_id: str = Field(min_length=1)
    measurement_set_sha256: str = Field(min_length=16)
    target_id: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    target_sha256: str = Field(min_length=16)
    mode: VideoCommissioningMode
    color_evaluation_id: str | None = None
    color_evaluation_sha256: str | None = None
    photometric_evaluation_id: str | None = None
    photometric_evaluation_sha256: str | None = None
    findings: tuple[VideoDiagnosisFinding, ...]
    overall: EvaluationStatus
    diagnosed_at_utc: str = Field(min_length=1)
    diagnosis_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'diagnosis_sha256', 'diagnosis_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoCommissioningDiagnosis':
        digest = _hash(self.semantic_payload())
        if self.diagnosis_sha256 != digest:
            raise ValueError('video diagnosis semantic hash mismatch')
        if self.diagnosis_id != 'vcd-' + digest[:24]:
            raise ValueError('video diagnosis id mismatch')
        return self


_COLOR_GROUP_TO_KIND: dict[str, VideoFindingKind] = {
    'white_point': 'white_point',
    'grayscale': 'grayscale',
    'eotf_tracking': 'eotf_tracking',
    'peak_luminance': 'peak_luminance',
    'gamut': 'gamut',
}

_PHOTOMETRIC_CRITERION_TO_KIND: dict[str, VideoFindingKind] = {
    'peak_white_luminance': 'peak_white_luminance',
    'black_floor': 'black_floor',
    'on_off_contrast': 'on_off_contrast',
}


def _finding_explanation(
    kind: VideoFindingKind,
    verdict: EvaluationStatus,
    mode: VideoCommissioningMode,
    *,
    worst_delta_e: float | None = None,
    measured_points: int = 0,
    note: str | None = None,
    predicted: float | None = None,
    measured: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Plain-language JA explanation + honest limitations per finding."""

    label = FINDING_KIND_LABELS[kind]
    limitations: list[str] = []
    if mode == 'hdr':
        limitations.append(
            '静的パターンの測定であり、コンテンツ適応型トーンマッピングの'
            '挙動すべてを証明するものではありません'
        )

    if verdict == 'PASS':
        detail = f'{label} は目標内です'
        if worst_delta_e is not None:
            detail += f'（最大差 {worst_delta_e:.2f}）'
        return detail + '。', tuple(limitations)

    if verdict == 'FAIL':
        if worst_delta_e is not None:
            detail = f'{label} が目標を外れています（最大差 {worst_delta_e:.2f}）'
        elif predicted is not None and measured is not None:
            detail = (
                f'{label} が目標を外れています'
                f'（予測 {predicted:.3g} / 実測 {measured:.3g}）'
            )
        else:
            detail = f'{label} が目標を外れています'
        return detail + '。', tuple(limitations)

    if verdict == 'NOT_APPLICABLE':
        return (
            f'{label} はこのターゲットでは評価対象外です。'
        ), tuple(limitations)

    # UNKNOWN — say exactly what is missing.
    base = note or ''
    if 'meter floor' in base:
        missing = 'サンプルがすべてメーターの検出下限にあります'
    elif 'no samples' in base or 'no usable' in base:
        missing = 'この項目に使える測定サンプルがありません'
    elif 'metric' in base:
        missing = '許容誤差に結び付いた ΔE 指標と評価指標が一致しません'
    elif 'stimulus_level' in base:
        missing = '刺激レベル付きの測定サンプルがありません'
    elif 'black level' in base:
        missing = 'BT.1886 の評価には黒レベルの実測値が必要です'
    else:
        missing = 'この項目を評価する証拠が不足しています'
    return f'{label}: {missing}。', tuple(limitations)


def _finding_from_criterion(
    criterion: ColorCriterionResult,
    kind: VideoFindingKind,
    mode: VideoCommissioningMode,
) -> VideoDiagnosisFinding:
    explanation, limitations = _finding_explanation(
        kind,
        criterion.status,
        mode,
        worst_delta_e=criterion.worst_delta_e,
        measured_points=criterion.measured_points,
        note=criterion.note,
    )
    return VideoDiagnosisFinding(
        kind=kind,
        verdict=criterion.status,
        metric=criterion.group,
        deviation=criterion.worst_delta_e,
        measured_points=criterion.measured_points,
        evidence_note=criterion.note,
        explanation=explanation,
        limitations=limitations,
    )


def diagnose_video_measurement(
    *,
    session: GuidedVideoCommissioningSession,
    measurement_set: VideoColorMeasurementSet,
    target: VideoColorTargetProfile,
    metric_family: ColorMetricFamily = 'dE2000',
    metric_version: str | None = None,
    photometric_evaluation: PhotometricEvaluation | None = None,
    diagnosed_at_utc: str | None = None,
) -> VideoCommissioningDiagnosis:
    """Run the composed evaluation and fold it into findings.

    The color groups come from :func:`evaluate_video_color` (the #647/#1018
    authority — never reimplemented here); photometric criteria come from a
    caller-computed :class:`PhotometricEvaluation` when the projection path
    produced one.
    """

    color_evaluation = evaluate_video_color(
        target=target,
        measurement_set=measurement_set,
        metric_family=metric_family,
        metric_version=metric_version,
    )

    findings: list[VideoDiagnosisFinding] = []
    for criterion in color_evaluation.groups:
        kind = _COLOR_GROUP_TO_KIND.get(criterion.group)
        if kind is None:
            continue
        findings.append(
            _finding_from_criterion(criterion, kind, session.mode)
        )

    if photometric_evaluation is not None:
        for criterion in photometric_evaluation.criteria:
            kind = _PHOTOMETRIC_CRITERION_TO_KIND.get(criterion.criterion)
            if kind is None:
                continue
            explanation, limitations = _finding_explanation(
                kind,
                criterion.status,
                session.mode,
                predicted=criterion.predicted,
                measured=criterion.measured,
            )
            findings.append(
                VideoDiagnosisFinding(
                    kind=kind,
                    verdict=criterion.status,
                    metric=criterion.criterion,
                    observed=criterion.measured,
                    target_value=criterion.predicted,
                    measured_points=1,
                    evidence_note=criterion.note
                    or (
                        '互換性軸: ' + ', '.join(criterion.blocking_axes)
                        if criterion.blocking_axes
                        else None
                    ),
                    explanation=explanation,
                    limitations=limitations,
                )
            )

    overall = _combine_status(tuple(f.verdict for f in findings))

    probe = VideoCommissioningDiagnosis.model_construct(
        **canonicalize_payload(VideoCommissioningDiagnosis, dict(
            diagnosis_id='',
            session_id=session.session_id,
            session_sha256=session.session_sha256,
            measurement_set_id=measurement_set.measurement_set_id,
            measurement_set_sha256=measurement_set.measurement_set_sha256,
            target_id=target.target_id,
            target_version=target.version,
            target_sha256=target.target_sha256,
            mode=session.mode,
            color_evaluation_id=color_evaluation.evaluation_id,
            color_evaluation_sha256=color_evaluation.evaluation_sha256,
            photometric_evaluation_id=(
                photometric_evaluation.evaluation_id
                if photometric_evaluation is not None
                else None
            ),
            photometric_evaluation_sha256=(
                photometric_evaluation.evaluation_sha256
                if photometric_evaluation is not None
                else None
            ),
            findings=tuple(findings),
            overall=overall,
            diagnosed_at_utc=diagnosed_at_utc or _utc_now(),
            diagnosis_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return VideoCommissioningDiagnosis(
        **probe.model_dump(
            mode='python', exclude={'diagnosis_sha256', 'diagnosis_id'}
        ),
        diagnosis_id='vcd-' + digest[:24],
        diagnosis_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Corrective actions
# ---------------------------------------------------------------------------


class VideoCorrectiveAction(BaseModel):
    """One bounded corrective proposal. ``evidence`` names the findings and
    measurement hash that justify it; ``limitations`` keeps the proposal
    honest (what it can NOT fix, what it might make worse)."""

    model_config = ConfigDict(frozen=True)

    kind: VideoActionKind
    evidence: tuple[str, ...] = ()
    confidence: Literal['evidence_bound', 'indicative'] = 'evidence_bound'
    explanation: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    requires_remeasure: bool = True


class VideoCommissioningProposal(BaseModel):
    """The ordered action list derived from one diagnosis."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    proposal_id: str = Field(min_length=1)
    diagnosis_id: str = Field(min_length=1)
    diagnosis_sha256: str = Field(min_length=16)
    actions: tuple[VideoCorrectiveAction, ...]
    proposed_at_utc: str = Field(min_length=1)
    proposal_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'proposal_sha256', 'proposal_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoCommissioningProposal':
        digest = _hash(self.semantic_payload())
        if self.proposal_sha256 != digest:
            raise ValueError('video action proposal semantic hash mismatch')
        if self.proposal_id != 'vcp-' + digest[:24]:
            raise ValueError('video action proposal id mismatch')
        return self


def _white_balance_kinds(
    diagnosis: VideoCommissioningDiagnosis,
    measurement_set: VideoColorMeasurementSet,
    target: VideoColorTargetProfile,
) -> tuple[VideoActionKind, ...]:
    """Pick the white-balance action scope the evidence supports.

    A white-point finding only justifies the high end; a grayscale finding
    dominated by low-level samples only justifies the low end. Anything less
    scoped proposes both ends with indicative confidence.
    """

    kinds: list[VideoActionKind] = []
    for finding in diagnosis.findings:
        if finding.verdict != 'FAIL':
            continue
        if finding.kind == 'white_point':
            kinds.append('adjust_white_balance_high')
        elif finding.kind == 'grayscale':
            grayscale = [
                s
                for s in measurement_set.samples
                if _sample_group_of(s.stimulus_id) == 'grayscale'
            ]
            leveled = [s for s in grayscale if s.stimulus_level is not None]
            if leveled and all(
                (s.stimulus_level or 0.0) < 0.5 for s in leveled
            ):
                kinds.append('adjust_white_balance_low')
            elif leveled and all(
                (s.stimulus_level or 0.0) >= 0.5 for s in leveled
            ):
                kinds.append('adjust_white_balance_high')
            else:
                kinds.append('adjust_white_balance_low')
                kinds.append('adjust_white_balance_high')
    return tuple(dict.fromkeys(kinds))


def propose_video_actions(
    diagnosis: VideoCommissioningDiagnosis,
    *,
    session: GuidedVideoCommissioningSession | None = None,
    measurement_set: VideoColorMeasurementSet | None = None,
    target: VideoColorTargetProfile | None = None,
    proposed_at_utc: str | None = None,
) -> VideoCommissioningProposal:
    """Derive bounded corrective actions from a diagnosis.

    Rules carried from the issue:
    - brand-neutral by default — actions name the control class (black
      level, CMS primary), never a vendor menu path;
    - every action cites the finding kinds that triggered it;
    - unknown/missing evidence proposes REMEASURE or
      ``no_safe_action_from_current_evidence``, never an invented fix;
    - adjustments always ``requires_remeasure`` — a proposal is not a
      verified improvement until re-measurement exists.
    """

    set_sha = diagnosis.measurement_set_sha256
    actions: list[VideoCorrectiveAction] = []

    if session is not None and (
        session.signal_range == 'unknown' or session.encoding == 'unknown'
    ):
        actions.append(
            VideoCorrectiveAction(
                kind='check_signal_range',
                evidence=('session_binding', session.session_sha256),
                confidence='evidence_bound',
                explanation=(
                    '信号レンジ/色度方式が不明です。ソース機器と表示機器の'
                    'レンジ設定（リミテッド/フル）が一致しているか先に確認'
                    'してください — 不一致は白つぶれや黒浮きとして現れます。'
                ),
            )
        )

    for finding in diagnosis.findings:
        evidence = (finding.kind, set_sha)
        if finding.verdict == 'FAIL':
            if finding.kind == 'white_point':
                actions.append(
                    VideoCorrectiveAction(
                        kind='adjust_white_balance_high',
                        evidence=evidence,
                        confidence='evidence_bound',
                        explanation=(
                            '白色点（色温度）が目標からずれています。'
                            '高輝度側のホワイトバランス（ゲイン/ドライブ）'
                            'を確認してください。'
                        ),
                        limitations=(
                            'この機器が高輝度/低輝度の別調整を持つかは'
                            '機器定義の権威で確認してください',
                        ),
                    )
                )
            elif finding.kind == 'grayscale':
                for kind in (
                    _white_balance_kinds(
                        diagnosis, measurement_set, target
                    )
                    if measurement_set is not None and target is not None
                    else ('adjust_white_balance_low', 'adjust_white_balance_high')
                ):
                    actions.append(
                        VideoCorrectiveAction(
                            kind=kind,
                            evidence=evidence,
                            confidence='indicative',
                            explanation=(
                                'グレースケールの RGB バランスがずれています。'
                                'ずれている輝度帯のホワイトバランスを確認して'
                                'ください。'
                            ),
                            limitations=(
                                '一段階の調整が他の輝度帯を悪化させることが'
                                'あります — 必ず再測定で確認してください',
                            ),
                        )
                    )
            elif finding.kind == 'eotf_tracking':
                kind: VideoActionKind = (
                    'adjust_eotf' if diagnosis.mode == 'hdr' else 'adjust_gamma'
                )
                actions.append(
                    VideoCorrectiveAction(
                        kind=kind,
                        evidence=evidence,
                        confidence='evidence_bound',
                        explanation=(
                            'EOTF追従が目標カーブを外れています。HDR機器側の'
                            'トーンマッピング/ダイナミック処理の設定を確認して'
                            'ください。'
                            if kind == 'adjust_eotf'
                            else 'ガンマ追従が目標を外れています。機器のガンマ'
                            '設定を確認してください。'
                        ),
                        limitations=(
                            '静的パターンの測定です。動的トーンマッピングの'
                            '全挙動を代表するものではありません',
                        )
                        if kind == 'adjust_eotf'
                        else (),
                    )
                )
            elif finding.kind == 'peak_luminance':
                actions.append(
                    VideoCorrectiveAction(
                        kind='adjust_white_level',
                        evidence=evidence,
                        confidence='evidence_bound',
                        explanation=(
                            'ピーク輝度が目標を外れています。白レベル'
                            '（コントラスト/ピーク輝度系の設定）を確認して'
                            'ください。'
                        ),
                    )
                )
            elif finding.kind == 'gamut':
                primaries: list[VideoActionKind] = []
                if measurement_set is not None:
                    worst_primary = any(
                        _sample_group_of(s.stimulus_id) == 'gamut'
                        and s.stimulus_id.lower().startswith(
                            _PRIMARY_STIMULUS_PREFIXES
                        )
                        for s in measurement_set.samples
                    )
                    worst_secondary = any(
                        _sample_group_of(s.stimulus_id) == 'gamut'
                        and s.stimulus_id.lower().startswith(
                            _SECONDARY_STIMULUS_PREFIXES
                        )
                        for s in measurement_set.samples
                    )
                    if worst_primary:
                        primaries.append('adjust_cms_primary')
                    if worst_secondary:
                        primaries.append('adjust_cms_secondary')
                if not primaries:
                    primaries = ['adjust_cms_primary', 'adjust_cms_secondary']
                for kind in primaries:
                    actions.append(
                        VideoCorrectiveAction(
                            kind=kind,
                            evidence=evidence,
                            confidence='indicative',
                            explanation=(
                                '色域のずれを示す測定点があります。機器の CMS'
                                '（色管理）の該当色を確認してください。'
                            ),
                            limitations=(
                                '1色の調整が他の色の誤差を増やす場合があります'
                                ' — 再測定で全体を確認してください',
                            ),
                        )
                    )
            elif finding.kind in (
                'peak_white_luminance',
                'black_floor',
            ):
                actions.append(
                    VideoCorrectiveAction(
                        kind=(
                            'adjust_black_level'
                            if finding.kind == 'black_floor'
                            else 'adjust_white_level'
                        ),
                        evidence=evidence,
                        confidence='indicative',
                        explanation=(
                            '黒床輝度が予測と合いません。黒レベル'
                            '（ブライトネス）を確認してください。'
                            if finding.kind == 'black_floor'
                            else 'ピーク白輝度が予測と合いません。光源モード/'
                            '絞り/ランプ出力の設定を確認してください。'
                        ),
                        limitations=(
                            '実測と予測の互換性軸（状態・口径・環境光）が'
                            '一致しない場合は調整ではなく測定条件の不一致の'
                            '可能性があります',
                        ),
                    )
                )
            elif finding.kind == 'on_off_contrast':
                actions.append(
                    VideoCorrectiveAction(
                        kind='check_signal_range',
                        evidence=evidence,
                        confidence='indicative',
                        explanation=(
                            '全白/全黒コントラストが予測と合いません。'
                            '信号レンジの不一致や環境光を確認してください。'
                        ),
                    )
                )
        elif finding.verdict == 'UNKNOWN':
            actions.append(
                VideoCorrectiveAction(
                    kind='remeasure',
                    evidence=evidence,
                    confidence='evidence_bound',
                    explanation=(
                        f'{FINDING_KIND_LABELS[finding.kind]}: '
                        '評価できる証拠が不足しています。該当する測定を'
                        '追加してから再評価してください。'
                    ),
                )
            )

    if not actions and not (
        diagnosis.findings
        and all(f.verdict == 'PASS' for f in diagnosis.findings)
    ):
        actions.append(
            VideoCorrectiveAction(
                kind='no_safe_action_from_current_evidence',
                evidence=(set_sha,),
                confidence='evidence_bound',
                explanation=(
                    '現在の証拠からは安全に提案できる調整がありません。'
                    '測定条件を揃えてからやり直してください。'
                ),
                requires_remeasure=False,
            )
        )

    # Never propose the same control twice — dedupe by kind, first citation
    # wins (first diagnosis order).
    deduped: list[VideoCorrectiveAction] = []
    seen: set[str] = set()
    for action in actions:
        if action.kind in seen:
            continue
        seen.add(action.kind)
        deduped.append(action)

    probe = VideoCommissioningProposal.model_construct(
        **canonicalize_payload(VideoCommissioningProposal, dict(
            proposal_id='',
            diagnosis_id=diagnosis.diagnosis_id,
            diagnosis_sha256=diagnosis.diagnosis_sha256,
            actions=tuple(deduped),
            proposed_at_utc=proposed_at_utc or _utc_now(),
            proposal_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return VideoCommissioningProposal(
        **probe.model_dump(
            mode='python', exclude={'proposal_sha256', 'proposal_id'}
        ),
        proposal_id='vcp-' + digest[:24],
        proposal_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Iteration chain — adjustment record + before/after comparison
# ---------------------------------------------------------------------------


class VideoOperatorAdjustment(BaseModel):
    """One operator-performed adjustment — the append-only link between
    baseline measurement, the selected actions and the re-measurement.

    ``followup_measurement_set_id`` may be ``None`` at record time (the
    re-measurement has not been imported yet); the comparison record
    produced later carries the resolved delta, so the adjustment row
    itself never needs rewriting.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    adjustment_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(min_length=16)
    iteration_index: int = Field(ge=1)
    diagnosis_id: str | None = None
    diagnosis_sha256: str | None = None
    before_measurement_set_id: str = Field(min_length=1)
    before_measurement_set_sha256: str = Field(min_length=16)
    selected_actions: tuple[VideoActionKind, ...]
    operator_note: str | None = None
    changed_controls: tuple[tuple[str, str], ...] = ()
    applied_calibration_state: AppliedCalibrationState | None = None
    followup_measurement_set_id: str | None = None
    followup_measurement_set_sha256: str | None = Field(
        default=None, min_length=16
    )
    recorded_at_utc: str = Field(min_length=1)
    adjustment_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'adjustment_sha256', 'adjustment_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoOperatorAdjustment':
        digest = _hash(self.semantic_payload())
        if self.adjustment_sha256 != digest:
            raise ValueError('video operator adjustment semantic hash mismatch')
        if self.adjustment_id != 'voa-' + digest[:24]:
            raise ValueError('video operator adjustment id mismatch')
        followup = (
            self.followup_measurement_set_id is not None,
            self.followup_measurement_set_sha256 is not None,
        )
        if any(followup) and not all(followup):
            raise ValueError(
                'follow-up measurement set needs id and sha256 together'
            )
        return self


def record_video_operator_adjustment(
    *,
    session: GuidedVideoCommissioningSession,
    iteration_index: int,
    before_measurement_set: VideoColorMeasurementSet,
    selected_actions: tuple[VideoActionKind, ...],
    diagnosis: VideoCommissioningDiagnosis | None = None,
    operator_note: str | None = None,
    changed_controls: tuple[tuple[str, str], ...] = (),
    applied_calibration_state: AppliedCalibrationState | None = None,
    followup_measurement_set: VideoColorMeasurementSet | None = None,
    recorded_at_utc: str | None = None,
) -> VideoOperatorAdjustment:
    probe = VideoOperatorAdjustment.model_construct(
        **canonicalize_payload(VideoOperatorAdjustment, dict(
            adjustment_id='',
            session_id=session.session_id,
            session_sha256=session.session_sha256,
            iteration_index=iteration_index,
            diagnosis_id=(
                diagnosis.diagnosis_id if diagnosis is not None else None
            ),
            diagnosis_sha256=(
                diagnosis.diagnosis_sha256 if diagnosis is not None else None
            ),
            before_measurement_set_id=before_measurement_set.measurement_set_id,
            before_measurement_set_sha256=(
                before_measurement_set.measurement_set_sha256
            ),
            selected_actions=tuple(selected_actions),
            operator_note=operator_note,
            changed_controls=tuple(changed_controls),
            applied_calibration_state=applied_calibration_state,
            followup_measurement_set_id=(
                followup_measurement_set.measurement_set_id
                if followup_measurement_set is not None
                else None
            ),
            followup_measurement_set_sha256=(
                followup_measurement_set.measurement_set_sha256
                if followup_measurement_set is not None
                else None
            ),
            recorded_at_utc=recorded_at_utc or _utc_now(),
            adjustment_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return VideoOperatorAdjustment(
        **probe.model_dump(
            mode='python', exclude={'adjustment_sha256', 'adjustment_id'}
        ),
        adjustment_id='voa-' + digest[:24],
        adjustment_sha256=digest,
    )


class VideoMetricDelta(BaseModel):
    """One before/after metric row. ``direction`` is relative to the target:
    a smaller deviation from target is ``improved``, a larger one
    ``regressed``; identical evidence reports ``inconclusive`` rather than a
    vacuous "no change = success"."""

    model_config = ConfigDict(frozen=True)

    metric: str = Field(min_length=1)
    label: str = Field(min_length=1)
    before: float | None = None
    after: float | None = None
    delta: float | None = None
    direction: VideoDeltaDirection


class VideoBeforeAfterComparison(BaseModel):
    """First-class comparison between two iterations' measurement sets.

    Compatibility is checked first — different surface, meter, correction
    or stimulus semantics produce ``incomparable`` with the blocking
    reasons, never a misleading improvement table.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    comparison_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(min_length=16)
    iteration_index: int = Field(ge=1)
    before_measurement_set_id: str = Field(min_length=1)
    before_measurement_set_sha256: str = Field(min_length=16)
    after_measurement_set_id: str = Field(min_length=1)
    after_measurement_set_sha256: str = Field(min_length=16)
    status: VideoComparisonStatus
    incompatibility_reasons: tuple[str, ...] = ()
    rows: tuple[VideoMetricDelta, ...] = ()
    overall: VideoDeltaDirection
    compared_at_utc: str = Field(min_length=1)
    comparison_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'comparison_sha256', 'comparison_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoBeforeAfterComparison':
        digest = _hash(self.semantic_payload())
        if self.comparison_sha256 != digest:
            raise ValueError('video comparison semantic hash mismatch')
        if self.comparison_id != 'vbc-' + digest[:24]:
            raise ValueError('video comparison id mismatch')
        if self.status == 'incomparable' and not self.incompatibility_reasons:
            raise ValueError(
                'an incomparable comparison must name its reasons'
            )
        return self


def _group_worst(
    evaluation: VideoColorEvaluation, group: str
) -> float | None:
    for criterion in evaluation.groups:
        if criterion.group == group:
            return criterion.worst_delta_e
    return None


def _group_status(
    evaluation: VideoColorEvaluation, group: str
) -> EvaluationStatus:
    for criterion in evaluation.groups:
        if criterion.group == group:
            return criterion.status
    return 'NOT_APPLICABLE'


#: Groups whose luminance extrema name the display's white/black ramp —
#: a primary colour at low level is a chroma measurement, never the
#: "black floor", and a dim colour primary is never "peak white".
_ACHROMATIC_GROUPS = frozenset({'white_point', 'grayscale'})


def _achromatic_luminances(
    samples: tuple[TristimulusSample, ...],
) -> list[float]:
    return [
        s.y_luminance
        for s in samples
        if not s.at_meter_floor
        and _sample_group(s.stimulus_id) in _ACHROMATIC_GROUPS
    ]


def _max_luminance(samples: tuple[TristimulusSample, ...]) -> float | None:
    usable = _achromatic_luminances(samples)
    return max(usable) if usable else None


def _min_luminance(samples: tuple[TristimulusSample, ...]) -> float | None:
    usable = _achromatic_luminances(samples)
    return min(usable) if usable else None


MetricPolarity = Literal['lower', 'higher', 'none']


def _metric_row(
    metric: str,
    label: str,
    before: float | None,
    after: float | None,
    *,
    better: MetricPolarity = 'lower',
) -> VideoMetricDelta:
    if before is None or after is None:
        direction: VideoDeltaDirection = 'unknown'
        delta = None
    elif better == 'none':
        # A measured change with no intrinsic better/worse direction
        # (e.g. peak-white capability — its goodness is target-relative,
        # which the deviation rows below already carry).
        direction, delta = 'inconclusive', after - before
    elif after < before:
        direction, delta = (
            ('improved' if better == 'lower' else 'regressed'),
            after - before,
        )
    elif after > before:
        direction, delta = (
            ('regressed' if better == 'lower' else 'improved'),
            after - before,
        )
    else:
        direction, delta = 'inconclusive', 0.0
    return VideoMetricDelta(
        metric=metric,
        label=label,
        before=before,
        after=after,
        delta=delta,
        direction=direction,
    )


def _compatibility_reasons(
    before: VideoColorMeasurementSet,
    after: VideoColorMeasurementSet,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if before.surface_entity_id != after.surface_entity_id:
        reasons.append('表示面が異なります（同一面どうしのみ比較できます）')
    if before.meter != after.meter:
        reasons.append('測定器が異なります')
    before_correction = before.meter_correction
    after_correction = after.meter_correction
    if (before_correction is None) != (after_correction is None):
        reasons.append('一方だけメーター補正が適用されています')
    elif (
        before_correction is not None
        and after_correction is not None
        and (
            before_correction.correction_id != after_correction.correction_id
            or before_correction.version != after_correction.version
        )
    ):
        reasons.append('メーター補正の種類またはバージョンが異なります')
    if before.stimulus.encoding != after.stimulus.encoding:
        reasons.append('信号符号化が異なります')
    if before.stimulus.bit_depth != after.stimulus.bit_depth:
        reasons.append('ビット深度が異なります')
    if before.stimulus.patch_size_percent != after.stimulus.patch_size_percent:
        reasons.append('パッチサイズが異なります')
    if before.stimulus.apl_percent != after.stimulus.apl_percent:
        reasons.append('APL（平均映像レベル）が異なります')
    if before.stimulus.pattern_generator != after.stimulus.pattern_generator:
        reasons.append('パターンジェネレータが異なります')
    if before.stimulus.signal_path != after.stimulus.signal_path:
        reasons.append('信号経路が異なります')
    before_ids = {s.stimulus_id for s in before.samples}
    after_ids = {s.stimulus_id for s in after.samples}
    if before_ids and after_ids and not (before_ids & after_ids):
        reasons.append('比較できる共通の測定点がありません')
    return tuple(reasons)


def compare_video_measurements(
    *,
    session: GuidedVideoCommissioningSession,
    before_set: VideoColorMeasurementSet,
    after_set: VideoColorMeasurementSet,
    target: VideoColorTargetProfile,
    iteration_index: int,
    metric_family: ColorMetricFamily = 'dE2000',
    metric_version: str | None = None,
    compared_at_utc: str | None = None,
) -> VideoBeforeAfterComparison:
    """Before/after comparison between two compatible measurement sets.

    Both sides are re-evaluated through :func:`evaluate_video_color` so the
    delta is "distance to target" — never a raw sample subtraction.
    """

    reasons = _compatibility_reasons(before_set, after_set)
    if reasons:
        probe = VideoBeforeAfterComparison.model_construct(
            **canonicalize_payload(VideoBeforeAfterComparison, dict(
                comparison_id='',
                session_id=session.session_id,
                session_sha256=session.session_sha256,
                iteration_index=iteration_index,
                before_measurement_set_id=before_set.measurement_set_id,
                before_measurement_set_sha256=(
                    before_set.measurement_set_sha256
                ),
                after_measurement_set_id=after_set.measurement_set_id,
                after_measurement_set_sha256=after_set.measurement_set_sha256,
                status='incomparable',
                incompatibility_reasons=reasons,
                rows=(),
                overall='unknown',
                compared_at_utc=compared_at_utc or _utc_now(),
                comparison_sha256='',
            ))
        )
        digest = _hash(probe.semantic_payload())
        return VideoBeforeAfterComparison(
            **probe.model_dump(
                mode='python',
                exclude={'comparison_sha256', 'comparison_id'},
            ),
            comparison_id='vbc-' + digest[:24],
            comparison_sha256=digest,
        )

    before_eval = evaluate_video_color(
        target=target,
        measurement_set=before_set,
        metric_family=metric_family,
        metric_version=metric_version,
    )
    after_eval = evaluate_video_color(
        target=target,
        measurement_set=after_set,
        metric_family=metric_family,
        metric_version=metric_version,
    )

    rows: list[VideoMetricDelta] = [
        _metric_row(
            'peak_white_cd_m2',
            'ピーク白輝度 (cd/m²)',
            _max_luminance(before_set.samples),
            _max_luminance(after_set.samples),
            better='none',
        ),
        _metric_row(
            'black_floor_cd_m2',
            '黒床輝度 (cd/m²)',
            _min_luminance(before_set.samples),
            _min_luminance(after_set.samples),
        ),
    ]
    for group, label in (
        ('white_point', '白色点 ΔE'),
        ('grayscale', 'グレースケール 最大ΔE'),
        ('eotf_tracking', 'EOTF 最大偏差'),
        ('peak_luminance', 'ピーク輝度偏差'),
        ('gamut', '色域 最大ΔE'),
    ):
        # A group whose evaluation was not possible on either side reports
        # an honest unknown row — the row exists to say "we could not check".
        rows.append(
            _metric_row(
                group,
                label,
                _group_worst(before_eval, group),
                _group_worst(after_eval, group),
            )
        )

    # Overall fold: any regression dominates; then improvements; unknowns
    # when nothing could be compared.
    directions = {row.direction for row in rows}
    if 'regressed' in directions:
        overall: VideoDeltaDirection = 'regressed'
    elif 'improved' in directions:
        overall = 'improved'
    elif directions - {'unknown'}:
        overall = 'inconclusive'
    else:
        overall = 'unknown'

    probe = VideoBeforeAfterComparison.model_construct(
        **canonicalize_payload(VideoBeforeAfterComparison, dict(
            comparison_id='',
            session_id=session.session_id,
            session_sha256=session.session_sha256,
            iteration_index=iteration_index,
            before_measurement_set_id=before_set.measurement_set_id,
            before_measurement_set_sha256=before_set.measurement_set_sha256,
            after_measurement_set_id=after_set.measurement_set_id,
            after_measurement_set_sha256=after_set.measurement_set_sha256,
            status='comparable',
            incompatibility_reasons=(),
            rows=tuple(rows),
            overall=overall,
            compared_at_utc=compared_at_utc or _utc_now(),
            comparison_sha256='',
        ))
    )
    digest = _hash(probe.semantic_payload())
    return VideoBeforeAfterComparison(
        **probe.model_dump(
            mode='python', exclude={'comparison_sha256', 'comparison_id'}
        ),
        comparison_id='vbc-' + digest[:24],
        comparison_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Journey evaluation — the numbered guide strip derives from persisted state
# ---------------------------------------------------------------------------


class VideoJourneyStep(BaseModel):
    """One numbered journey step rendered by the surface."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    number: int = Field(ge=1)
    title: str = Field(min_length=1)
    status: VideoJourneyStepStatus
    detail: str


def evaluate_video_journey(
    *,
    session_exists: bool,
    readiness_state: VideoReadinessState | None,
    measurement_set_count: int,
    diagnosis_count: int,
    adjustment_count: int,
    comparison_count: int,
    session_status: VideoCommissioningStatus | None,
) -> tuple[VideoJourneyStep, ...]:
    """Derive the 7-step journey from persisted state — a recommended order,
    not hard gates (imported measurement sets are legitimate without a
    wizard-created session)."""

    steps: list[VideoJourneyStep] = []

    def add(key: str, title: str, status: VideoJourneyStepStatus, detail: str):
        steps.append(
            VideoJourneyStep(
                key=key,
                number=len(steps) + 1,
                title=title,
                status=status,
                detail=detail,
            )
        )

    add(
        'session',
        '対象と条件を決める',
        'done' if session_exists else 'current',
        (
            'セッションが作成されています'
            if session_exists
            else '対象の表示面・モード・ターゲットを結び付けます'
        ),
    )
    add(
        'readiness',
        '条件を確認する',
        (
            'done'
            if readiness_state in ('READY', 'READY_WITH_LIMITATIONS')
            else 'blocked'
            if readiness_state in ('INCOMPATIBLE', 'INSUFFICIENT_EVIDENCE')
            else 'current'
            if session_exists
            else 'pending'
        ),
        (
            READINESS_STATE_LABELS.get(readiness_state, '')
            if readiness_state is not None
            else 'セッション作成後に条件を評価します'
        ),
    )
    add(
        'measure',
        '測定を取り込む',
        (
            'done'
            if measurement_set_count
            else 'current'
            if session_exists
            else 'pending'
        ),
        (
            f'測定セット {measurement_set_count} 件'
            if measurement_set_count
            else 'HCFRのCSV書き出し等から測定を取り込みます'
        ),
    )
    add(
        'diagnose',
        '診断する',
        (
            'done' if diagnosis_count else 'current' if measurement_set_count else 'pending'
        ),
        (
            f'診断 {diagnosis_count} 件'
            if diagnosis_count
            else '測定をターゲットと比較評価します'
        ),
    )
    add(
        'adjust',
        '対策を実行する',
        (
            'done' if adjustment_count else 'current' if diagnosis_count else 'pending'
        ),
        (
            f'調整記録 {adjustment_count} 件'
            if adjustment_count
            else '提案された対策から選び、実施した調整を記録します'
        ),
    )
    add(
        'verify',
        '再測定して比較する',
        (
            'done' if comparison_count else 'current' if adjustment_count else 'pending'
        ),
        (
            f'比較 {comparison_count} 件'
            if comparison_count
            else '調整後の再測定で前後の差を確認します'
        ),
    )
    add(
        'report',
        '結果をまとめる',
        (
            'done'
            if session_status == 'completed'
            else 'current'
            if comparison_count
            else 'pending'
        ),
        (
            'セッションは完了として記録されています'
            if session_status == 'completed'
            else '診断・調整・比較の記録を1つの結果にまとめます'
        ),
    )
    return tuple(steps)


def session_iteration_phase(
    adjustments: tuple[VideoOperatorAdjustment, ...],
    comparisons: tuple[VideoBeforeAfterComparison, ...],
) -> tuple['VideoIterationPhase', ...]:
    """Fold adjustments + comparisons into per-iteration phase views for the
    timeline UI (read-side composite — rows never mutate)."""

    phases: list[VideoIterationPhase] = []
    for adjustment in sorted(adjustments, key=lambda a: a.iteration_index):
        comparison = next(
            (
                c
                for c in comparisons
                if c.iteration_index == adjustment.iteration_index
            ),
            None,
        )
        if comparison is None:
            phase: VideoIterationPhaseValue = 'awaiting_remeasure'
        elif comparison.status == 'incomparable':
            phase = 'incomparable'
        else:
            phase = 'compared'
        phases.append(
            VideoIterationPhase(
                iteration_index=adjustment.iteration_index,
                adjustment=adjustment,
                comparison=comparison,
                phase=phase,
            )
        )
    return tuple(phases)


VideoIterationPhaseValue = Literal[
    'awaiting_remeasure', 'compared', 'incomparable'
]

ITERATION_PHASE_LABELS: dict[str, str] = {
    'awaiting_remeasure': '再測定待ち',
    'compared': '比較済み',
    'incomparable': '比較不能',
}


class VideoIterationPhase(BaseModel):
    """Read-side per-iteration view: adjustment record + its comparison."""

    model_config = ConfigDict(frozen=True)

    iteration_index: int = Field(ge=1)
    adjustment: VideoOperatorAdjustment
    comparison: VideoBeforeAfterComparison | None
    phase: VideoIterationPhaseValue


__all__ = [
    'ACTION_KIND_LABELS',
    'FINDING_KIND_LABELS',
    'ITERATION_PHASE_LABELS',
    'MODE_LABELS',
    'READINESS_STATE_LABELS',
    'SESSION_STATUS_LABELS',
    'SIGNAL_RANGE_LABELS',
    'SURFACE_KIND_LABELS',
    'GuidedVideoCommissioningSession',
    'VideoActionKind',
    'VideoBeforeAfterComparison',
    'VideoCommissioningDiagnosis',
    'VideoCommissioningMode',
    'VideoCommissioningProposal',
    'VideoCommissioningStatus',
    'VideoCommissioningStep',
    'VideoComparisonStatus',
    'VideoCorrectiveAction',
    'VideoDeltaDirection',
    'VideoDiagnosisFinding',
    'VideoFindingKind',
    'VideoIterationPhase',
    'VideoIterationPhaseValue',
    'VideoJourneyStep',
    'VideoJourneyStepStatus',
    'VideoMetricDelta',
    'VideoOperatorAdjustment',
    'VideoReadinessCheck',
    'VideoReadinessReport',
    'VideoReadinessState',
    'VideoSignalRange',
    'VideoSurfaceKind',
    'build_guided_video_session',
    'compare_video_measurements',
    'diagnose_video_measurement',
    'evaluate_video_journey',
    'evaluate_video_readiness',
    'propose_video_actions',
    'rebind_video_session',
    'record_video_operator_adjustment',
    'session_iteration_phase',
]
