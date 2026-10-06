"""Accessible media playback authority (#783).

Content that *contains* a caption or audio-description component is not
thereby *perceivable*. Accessibility follows a chain: component present
in the content → discovered by the player → selected by the user
profile → rendered/played → verified in presentation. Each stage can
fail independently, and each must be evidenced separately. Caption
timing/placement/rendering compose with #733 timed-text; display
cropping/masking composes with #622; audio routing composes with the
output-path authorities. Personal preferences stay private-user-state
by default and never leak into verdict text.

Basis: issue #783 scope; ISO/IEC 20071-20:2025 & 20071-23:2018
(accessible ICT/media guidance); ISO/IEC TS 20071-21:2015 (caption
presentation); ANSI/CTA-708-E S-2023 (DTV closed captions); W3C IMSC
Text Profile 1.3; ISO/IEC 14496-30 timed-text carriage. HTDT verifies
presentation evidence — it never promises a viewer 'will see it'.
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


AccessibleComponentKind = Literal[
    'caption', 'subtitle', 'sdh_subtitle', 'audio_description',
    'audio_subtitle', 'sign_language_video', 'other_accessibility',
]

CaptionProfileKind = Literal[
    'cta_708e_s_2023', 'imsc_text_1_3', 'imsc_text_1_2',
    'webvtt_profile', 'bluray_disc_profile',
    'streaming_provider_profile', 'cea608', 'other', 'unknown',
]

PresentationStage = Literal[
    'content_component_present', 'player_component_discovered',
    'user_profile_selected', 'rendered_played',
    'presentation_verified',
]

PRESENTATION_STAGE_ORDER: tuple[str, ...] = (
    'content_component_present', 'player_component_discovered',
    'user_profile_selected', 'rendered_played',
    'presentation_verified',
)

CaptionReadabilityState = Literal[
    'fully_visible', 'clipped', 'overlaps_ui',
    'low_contrast_suspected', 'font_too_small_for_project_profile',
    'wrapping_layout_error', 'unsupported_script_glyph',
    'timing_error', 'unknown',
]

ADMixSemantics = Literal[
    'premixed_ad_track', 'separate_narration_mixed_by_player',
    'provider_personalization', 'dual_mono_stereo_variant', 'unknown',
]

ADOutputState = Literal[
    'verified_at_output', 'lost_in_routing', 'downmix_lost',
    'not_verified', 'unknown',
]

PlaybackFailureCause = Literal[
    'content_component_absent', 'player_profile_unsupported',
    'codec_decode_failure', 'renderer_downmix_loss',
    'hdmi_output_profile_issue', 'display_caption_crop_render_issue',
    'network_service_failure', 'unknown',
]

FallbackScenario = Literal[
    'cold_start', 'content_change', 'language_change',
    'profile_change', 'standby_resume', 'app_update',
    'device_restore', 'source_switch',
]

ACCESSIBLE_MEDIA_LABELS: dict[str, str] = {
    'verified_accessible_playback': 'アクセシブル再生が検証済み',
    'verified_with_limitations': 'アクセシブル再生（限定条件付き）',
    'presentation_failed': '提示段階で失敗',
    'routing_failed': '音声経路で失敗',
    'preference_not_persistent': '設定が持続しない',
    'stale_after_change': '変更により陳腐化',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象コンポーネントなし',
}


class ComponentTrack(BaseModel):
    """One declared accessible component stream: kind, track id,
    language, format profile (+ revision pin) and how it surfaces to
    the user (forced / default / selectable)."""

    model_config = ConfigDict(frozen=True)

    component_kind: AccessibleComponentKind
    track_id: str
    language: str | None = None
    format_profile: CaptionProfileKind = 'unknown'
    format_revision: str | None = None
    selection_kind: Literal[
        'forced', 'default', 'selectable', 'unknown',
    ] = 'unknown'

    @model_validator(mode='after')
    def _validate(self) -> 'ComponentTrack':
        if not self.track_id.strip():
            raise ValueError('track_id must not be empty')
        return self


class AccessibleMediaProfile(BaseModel):
    """The declared accessibility surface of one content item (amp-
    prefix): which components exist, which user preference state was in
    force, and where the declaration came from. Preferences are private
    user state by default — they qualify playback, they are never
    broadcast into reports."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    content_ref: AuthorityRef | None = None
    content_label: str
    edition_or_service: str | None = None
    language: str | None = None
    component_tracks: tuple[ComponentTrack, ...] = ()
    caption_language: str | None = None
    captions_enabled: bool | None = None
    caption_style_preset: str | None = None
    ad_enabled: bool | None = None
    ad_language: str | None = None
    privacy_scope: Literal[
        'private_user_state', 'project_shared',
    ] = 'private_user_state'
    profile_source: Literal[
        'user_selected', 'service_default', 'content_default',
        'unknown',
    ] = 'unknown'
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'AccessibleMediaProfile':
        if not self.content_label.strip():
            raise ValueError('content_label must not be empty')
        if self.content_ref is not None:
            _require_refs(self.content_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'AccessibleMediaProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'amp')


class CaptionPresentationObservation(BaseModel):
    """One caption/subtitle presentation record (cpo- prefix). Pins the
    component identity, the highest presentation stage actually
    reached, the display/geometry binding, timing evidence and the
    readability verdict. 'presentation_verified' is only possible with
    a bound display profile, a fully-visible readability state and
    observed on/offset times — presence in the bitstream is not
    presentation."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef | None = None
    content_ref: AuthorityRef | None = None
    component_kind: AccessibleComponentKind
    track_id: str
    language: str | None = None
    format_profile: CaptionProfileKind = 'unknown'
    format_revision: str | None = None
    selection_kind: Literal[
        'forced', 'default', 'selectable', 'unknown',
    ] = 'unknown'
    reached_stage: PresentationStage = 'content_component_present'
    display_profile_ref: AuthorityRef | None = None
    image_mask_ref: AuthorityRef | None = None
    raster_description: str | None = None
    scaling_overscan_state: str | None = None
    placement: str | None = None
    font_size_description: str | None = None
    style_description: str | None = None
    color_opacity: str | None = None
    line_count: int | None = None
    hdr_sdr_state: Literal['hdr', 'sdr', 'unknown'] | None = None
    cue_time_s: float | None = None
    observed_onset_s: float | None = None
    observed_offset_s: float | None = None
    av_sync_state: Literal[
        'in_sync', 'lead', 'lag', 'unknown',
    ] | None = None
    measurement_method: str | None = None
    readability_state: CaptionReadabilityState = 'unknown'
    failure_cause: PlaybackFailureCause | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CaptionPresentationObservation':
        for ref in (self.profile_ref, self.content_ref,
                    self.display_profile_ref, self.image_mask_ref):
            if ref is not None:
                _require_refs(ref)
        if not self.track_id.strip():
            raise ValueError('track_id must not be empty')
        if self.reached_stage == 'presentation_verified':
            if self.display_profile_ref is None:
                raise ValueError(
                    'presentation_verified requires a pinned '
                    'display_profile_ref')
            if self.readability_state != 'fully_visible':
                raise ValueError(
                    'presentation_verified requires readability_state '
                    "'fully_visible'")
            if self.observed_onset_s is None \
                    or self.observed_offset_s is None:
                raise ValueError(
                    'presentation_verified requires observed '
                    'onset/offset times')
        if self.line_count is not None and self.line_count < 1:
            raise ValueError('line_count must be >= 1')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'CaptionPresentationObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256',
            'cpo')


class AudioDescriptionPlaybackObservation(BaseModel):
    """One audio-description playback record (adpo- prefix). Pins the
    track identity and codec, the player/decoder output context, the
    mix semantics (premixed track vs player-mixed narration), and where
    the output was verified — 'verified_at_output' requires a bound
    output path and a measurement method, not just 'the track exists'.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef | None = None
    content_ref: AuthorityRef | None = None
    track_id: str
    language: str | None = None
    codec: str | None = None
    codec_profile: str | None = None
    channel_layout: str | None = None
    player_decoder_ref: AuthorityRef | None = None
    downmix_render_mode: str | None = None
    mix_semantics: ADMixSemantics = 'unknown'
    narration_gain_db: float | None = None
    reference_level_db: float | None = None
    output_path_ref: AuthorityRef | None = None
    output_state: ADOutputState = 'unknown'
    measurement_method: str | None = None
    failure_cause: PlaybackFailureCause | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'AudioDescriptionPlaybackObservation':
        for ref in (self.profile_ref, self.content_ref,
                    self.player_decoder_ref, self.output_path_ref):
            if ref is not None:
                _require_refs(ref)
        if not self.track_id.strip():
            raise ValueError('track_id must not be empty')
        if self.output_state == 'verified_at_output':
            if self.output_path_ref is None:
                raise ValueError(
                    'verified_at_output requires a pinned '
                    'output_path_ref')
            if not self.measurement_method:
                raise ValueError(
                    'verified_at_output requires a measurement_method')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'AudioDescriptionPlaybackObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256',
            'adpo')


class PlaybackStackIdentity(BaseModel):
    """The exact software/hardware stack a playback claim was verified
    on — a verdict is only as portable as this identity."""

    model_config = ConfigDict(frozen=True)

    source_device: str | None = None
    os_or_firmware: str | None = None
    app_version: str | None = None
    account_or_service: str | None = None
    content_version: str | None = None
    render_settings: str | None = None
    display_mode: str | None = None


class FallbackResult(BaseModel):
    """One fallback-scenario outcome: did the user preference survive
    the transition?"""

    model_config = ConfigDict(frozen=True)

    scenario: FallbackScenario
    preference_survived: bool
    notes: str | None = None


class AccessiblePlaybackQualification(BaseModel):
    """The verdict record for accessible playback of one content item
    on one stack (apq- prefix). Binds the caption/AD observations, the
    stack identity, fallback persistence results and the verdict.
    Stales when the app/firmware/content changes underneath it."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef | None = None
    caption_observation_refs: tuple[AuthorityRef, ...] = ()
    ad_observation_refs: tuple[AuthorityRef, ...] = ()
    stack_identity: PlaybackStackIdentity | None = None
    fallback_results: tuple[FallbackResult, ...] = ()
    verdict: Literal[
        'verified_accessible_playback', 'verified_with_limitations',
        'presentation_failed', 'routing_failed',
        'preference_not_persistent', 'stale_after_change',
        'insufficient_evidence', 'not_applicable',
    ]
    staling_ref: AuthorityRef | None = None
    limitation_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'AccessiblePlaybackQualification':
        if self.profile_ref is not None:
            _require_refs(self.profile_ref)
        for ref in self.caption_observation_refs:
            _require_refs(ref)
        for ref in self.ad_observation_refs:
            _require_refs(ref)
        if self.staling_ref is not None:
            _require_refs(self.staling_ref)
        if self.verdict in (
                'verified_accessible_playback',
                'verified_with_limitations') \
                and not (self.caption_observation_refs
                         or self.ad_observation_refs):
            raise ValueError(
                'a verified verdict requires at least one bound '
                'observation')
        if self.verdict == 'verified_with_limitations' \
                and not self.limitation_reasons:
            raise ValueError(
                'verified_with_limitations requires limitation_reasons')
        if self.verdict == 'stale_after_change' \
                and self.staling_ref is None:
            raise ValueError(
                'stale_after_change requires a staling_ref')
        if self.verdict == 'preference_not_persistent' \
                and not any(
                    r.preference_survived is False
                    for r in self.fallback_results):
            raise ValueError(
                'preference_not_persistent requires a failed '
                'fallback result')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'AccessiblePlaybackQualification':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256',
            'apq')


def evaluate_accessible_playback(
    profile: AccessibleMediaProfile | None,
    caption_observations: tuple[CaptionPresentationObservation, ...],
    ad_observations: tuple[AudioDescriptionPlaybackObservation, ...],
    qualification: AccessiblePlaybackQualification | None,
) -> tuple[str, str]:
    """Fail-closed accessible-playback gate (#783).

    Component availability, user selection, rendering and verified
    presentation are separate stages — the verdict only climbs as high
    as the bound observations prove. Preference persistence is checked
    via fallback results; a verdict never discloses preference values.
    """
    if profile is None or not profile.component_tracks:
        return ('not_applicable', 'no_declared_components')
    if qualification is None:
        return ('insufficient_evidence', 'no_qualification_record')
    if qualification.verdict == 'stale_after_change':
        return ('stale_after_change', 'staled_by_stack_change')
    for obs in ad_observations:
        if obs.output_state in ('lost_in_routing', 'downmix_lost'):
            return ('routing_failed',
                    'ad_output:' + obs.output_state)
    best_stage = 0
    for obs in caption_observations:
        stage = PRESENTATION_STAGE_ORDER.index(obs.reached_stage)
        best_stage = max(best_stage, stage)
        if obs.failure_cause == 'display_caption_crop_render_issue':
            return ('presentation_failed',
                    'caption_crop:' + obs.observation_id)
    if caption_observations and best_stage < len(
            PRESENTATION_STAGE_ORDER) - 1:
        return ('presentation_failed',
                'reached:' + PRESENTATION_STAGE_ORDER[best_stage])
    if qualification.verdict == 'preference_not_persistent':
        return ('preference_not_persistent',
                'fallback_failed')
    if qualification.verdict == 'presentation_failed':
        return ('presentation_failed',
                'qualification:presentation_failed')
    if qualification.verdict == 'routing_failed':
        return ('routing_failed', 'qualification:routing_failed')
    if qualification.verdict == 'insufficient_evidence':
        return ('insufficient_evidence',
                'qualification:insufficient_evidence')
    if qualification.verdict == 'not_applicable':
        return ('not_applicable', 'qualification:not_applicable')
    if qualification.verdict == 'verified_with_limitations':
        return ('verified_with_limitations',
                'limitations:' + ','.join(
                    qualification.limitation_reasons))
    return ('verified_accessible_playback',
            'qualification:' + qualification.qualification_id)
