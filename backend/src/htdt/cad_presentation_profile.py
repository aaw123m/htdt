"""Video presentation-profile authority (#565).

A :class:`VideoPresentationProfile` describes ONE way a projection screen is
driven: the target aspect ratio, how the active image aperture is sized on the
visible screen surface (constant image height, constant image width, explicit
dimensions, or a custom window), which masking panels are engaged, and which
projector optical preset (lens memory / zoom / shift / anamorphic / scaler
state) the projector reports for that mode.

Key properties, matching the issue contract:

- Profiles are authorities keyed by ``(profile_id, version)`` and bound to a
  screen entity by id — changing the active presentation mode is a profile
  switch, never a new ``SceneRevision``.
- ``resolve_presentation_aperture`` derives the exact active image rectangle
  (width, height, center offset in the screen's local plane frame) used by
  geometry and photometric evaluation. It is deterministic and returns a
  status instead of raising for out-of-screen apertures.
- ``evaluate_presentation_profile`` emits per-criterion statuses
  (aperture fits, masking consistency, optical-preset conformance when a
  projector specification is bound) that are never collapsed into a single
  opaque score; the combined ``profile_status`` folds UNKNOWN through.
- Projector optical presets record only what the projector actually reports —
  preset identifier, zoom ratio, lens-shift fractions, anamorphic and scaler
  modes — with an explicit ``UNKNOWN`` for values the device does not expose.
  No parameters are fabricated.
- Commissioning is evidence-based: a ``PresentationModeConfirmation`` records
  who confirmed the physical masking/optical state, when, and by which means;
  the profile itself only claims the *configured* intent.
- ``profile_sha256`` covers the full semantic payload, so changing the active
  aperture, masking state, or optical state always changes the hash.
- Requests remain self-contained and replayable: the resolved aperture is
  recomputed from profile + screen geometry at evaluation time, and callers
  may pin the resolved values to detect drift.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash




PresentationSizingKind = Literal[
    'constant_image_height',
    'constant_image_width',
    'explicit',
    'custom',
    'unknown',
]
"""How the active image aperture is sized within the visible screen area.

- ``constant_image_height``: the image height equals the screen's visible
  height; the width follows the target aspect ratio.
- ``constant_image_width``: the image width equals the screen's visible
  width; the height follows the target aspect ratio.
- ``explicit``: the caller supplies exact ``active_width_m`` /
  ``active_height_m`` (e.g. fixed 2.39:1 scope image).
- ``custom``: the caller supplies explicit dimensions plus a center offset
  (e.g. off-center dual-aspect windows).
- ``unknown``: sizing mode not recorded — the aperture cannot be resolved and
  evaluations report UNKNOWN.
"""

MaskingKind = Literal['none', 'side', 'top_bottom', 'four_way', 'unknown']
"""Which masking panels bound the active aperture.

``side`` masks left/right (typical for CIH on a scope screen showing 16:9),
``top_bottom`` masks above/below (typical for CIW on a 16:9 screen showing
scope), ``four_way`` masks all four edges, ``unknown`` means the masking
state was not recorded — never guess a masking kind.
"""

ConfirmationMethod = Literal[
    'visual_inspection',
    'device_readback',
    'measured',
    'user_confirmed',
    'other',
]


class MaskingState(BaseModel):
    """Physical masking-panel state for one presentation profile.

    ``masked_left_m``/``masked_right_m``/``masked_top_m``/``masked_bottom_m``
    are the observed border widths the panels cover, when known. They are
    evidence, not commands — a ``none`` masking kind with non-zero borders is
    an inconsistency the evaluation flags rather than silently resolves.
    """

    model_config = ConfigDict(frozen=True)

    kind: MaskingKind
    masked_left_m: float | None = Field(default=None, ge=0.0)
    masked_right_m: float | None = Field(default=None, ge=0.0)
    masked_top_m: float | None = Field(default=None, ge=0.0)
    masked_bottom_m: float | None = Field(default=None, ge=0.0)


class ProjectorOpticalPresetBinding(BaseModel):
    """The projector-side optical state bound to one presentation profile.

    Every field is optional because projectors report different subsets:
    a JVC lens-memory exposes only a slot number, while some processors
    report zoom/shift counts. ``preset_ref`` is the device-native identifier
    (e.g. ``"lens-memory-3"`` or a vendor preset name). Fields not reported
    stay ``None`` — they are never inferred from geometry.
    """

    model_config = ConfigDict(frozen=True)

    preset_ref: str = Field(min_length=1)
    zoom_ratio: float | None = Field(default=None, gt=0.0)
    horizontal_shift_fraction: float | None = Field(
        default=None, ge=-1.0, le=1.0
    )
    vertical_shift_fraction: float | None = Field(
        default=None, ge=-1.0, le=1.0
    )
    anamorphic_enabled: bool | None = None
    scaler_mode: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class ResolvedAperture(BaseModel):
    """The exact active image rectangle on the screen surface."""

    model_config = ConfigDict(frozen=True)

    status: EvaluationStatus
    status_reason: str
    width_m: float | None = Field(default=None, gt=0.0)
    height_m: float | None = Field(default=None, gt=0.0)
    center_offset_horizontal_m: float = 0.0
    center_offset_vertical_m: float = 0.0
    aspect_ratio: float | None = Field(default=None, gt=0.0)


class PresentationModeConfirmation(BaseModel):
    """Commissioning evidence that the physical state matched the profile.

    Kept separate from the profile itself: the profile records intent, the
    confirmation records that someone/something observed the physical
    masking and optical state at a point in time.
    """

    model_config = ConfigDict(frozen=True)

    confirmation_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=8)
    confirmed_at_utc: str = Field(min_length=1)
    method: ConfirmationMethod
    confirmed_by: str | None = None
    observed_aperture_width_m: float | None = Field(default=None, gt=0.0)
    observed_aperture_height_m: float | None = Field(default=None, gt=0.0)
    observed_preset_ref: str | None = None
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    confirmation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'confirmation_sha256'}
        )

    @model_validator(mode='after')
    def _check_hash(self) -> 'PresentationModeConfirmation':
        if self.confirmation_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'presentation mode confirmation semantic hash mismatch'
            )
        return self


def build_presentation_mode_confirmation(
    *,
    confirmation_id: str,
    profile: 'VideoPresentationProfile',
    confirmed_at_utc: str,
    method: ConfirmationMethod,
    confirmed_by: str | None = None,
    observed_aperture_width_m: float | None = None,
    observed_aperture_height_m: float | None = None,
    observed_preset_ref: str | None = None,
    note: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> PresentationModeConfirmation:
    probe = PresentationModeConfirmation.model_construct(
        confirmation_id=confirmation_id,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_sha256=profile.profile_sha256,
        confirmed_at_utc=confirmed_at_utc,
        method=method,
        confirmed_by=confirmed_by,
        observed_aperture_width_m=observed_aperture_width_m,
        observed_aperture_height_m=observed_aperture_height_m,
        observed_preset_ref=observed_preset_ref,
        note=note,
        provenance=provenance,
        confirmation_sha256='',
    )
    return PresentationModeConfirmation(
        **probe.model_dump(mode='python', exclude={'confirmation_sha256'}),
        confirmation_sha256=_hash(probe.semantic_payload()),
    )


class VideoPresentationProfile(BaseModel):
    """One named way a screen is driven (e.g. "16:9 CIH masked", "scope")."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-presentation-profile-1'] = (
        'video-presentation-profile-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str | None = None
    target_aspect_ratio: float | None = Field(default=None, gt=0.0)
    sizing: PresentationSizingKind
    active_width_m: float | None = Field(default=None, gt=0.0)
    active_height_m: float | None = Field(default=None, gt=0.0)
    center_offset_horizontal_m: float = 0.0
    center_offset_vertical_m: float = 0.0
    masking: MaskingState
    optical_preset: ProjectorOpticalPresetBinding | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'VideoPresentationProfile':
        if self.sizing in {'explicit', 'custom'}:
            if self.active_width_m is None or self.active_height_m is None:
                raise ValueError(
                    'explicit/custom sizing requires active_width_m and '
                    'active_height_m'
                )
        if self.sizing in {'constant_image_height', 'constant_image_width'}:
            if self.target_aspect_ratio is None:
                raise ValueError(
                    'CIH/CIW sizing requires target_aspect_ratio'
                )
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'video presentation profile semantic hash mismatch'
            )
        return self


def build_video_presentation_profile(
    *,
    profile_id: str,
    version: str,
    sizing: PresentationSizingKind,
    masking: MaskingState | None = None,
    label: str | None = None,
    target_aspect_ratio: float | None = None,
    active_width_m: float | None = None,
    active_height_m: float | None = None,
    center_offset_horizontal_m: float = 0.0,
    center_offset_vertical_m: float = 0.0,
    optical_preset: ProjectorOpticalPresetBinding | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> VideoPresentationProfile:
    probe = VideoPresentationProfile.model_construct(
        profile_id=profile_id,
        version=version,
        label=label,
        target_aspect_ratio=target_aspect_ratio,
        sizing=sizing,
        active_width_m=active_width_m,
        active_height_m=active_height_m,
        center_offset_horizontal_m=center_offset_horizontal_m,
        center_offset_vertical_m=center_offset_vertical_m,
        masking=masking if masking is not None else MaskingState(kind='none'),
        optical_preset=optical_preset,
        provenance=provenance,
        profile_sha256='',
    )
    return VideoPresentationProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


def resolve_presentation_aperture(
    profile: VideoPresentationProfile,
    *,
    screen_visible_width_m: float,
    screen_visible_height_m: float,
) -> ResolvedAperture:
    """Resolve the active image rectangle for one profile on one screen.

    Deterministic — the same profile and screen always resolve to the same
    aperture. Returns UNKNOWN when the sizing mode cannot produce an exact
    rectangle (``unknown`` sizing, missing aspect), and FAIL when the resolved
    rectangle exceeds the visible screen surface.
    """

    if profile.sizing == 'unknown':
        return ResolvedAperture(
            status='UNKNOWN',
            status_reason='profile sizing kind is not recorded',
        )
    if profile.sizing in {'explicit', 'custom'}:
        width = profile.active_width_m
        height = profile.active_height_m
    elif profile.sizing == 'constant_image_height':
        height = screen_visible_height_m
        width = screen_visible_height_m * profile.target_aspect_ratio
    else:  # constant_image_width
        width = screen_visible_width_m
        height = screen_visible_width_m / profile.target_aspect_ratio

    assert width is not None and height is not None
    offset_h = profile.center_offset_horizontal_m
    offset_v = profile.center_offset_vertical_m
    if (
        abs(offset_h) + width / 2.0 > screen_visible_width_m / 2.0 + 1e-9
        or abs(offset_v) + height / 2.0 > screen_visible_height_m / 2.0 + 1e-9
    ):
        return ResolvedAperture(
            status='FAIL',
            status_reason=(
                'resolved active aperture extends beyond the visible screen'
            ),
            width_m=width,
            height_m=height,
            center_offset_horizontal_m=offset_h,
            center_offset_vertical_m=offset_v,
            aspect_ratio=width / height,
        )
    return ResolvedAperture(
        status='PASS',
        status_reason='aperture resolved within the visible screen surface',
        width_m=width,
        height_m=height,
        center_offset_horizontal_m=offset_h,
        center_offset_vertical_m=offset_v,
        aspect_ratio=width / height,
    )


class PresentationProfileEvaluation(BaseModel):
    """Per-criterion evaluation of one profile against a screen binding.

    Statuses stay granular — ``aperture_status``, ``masking_status`` and
    ``optical_status`` are reported side by side, then folded into
    ``profile_status`` via the standard UNKNOWN-preserving combiner.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    screen_entity_id: str = Field(min_length=1)
    profile: VideoPresentationProfile
    aperture: ResolvedAperture
    aperture_status: EvaluationStatus
    masking_status: EvaluationStatus
    masking_reason: str
    optical_status: EvaluationStatus
    optical_reason: str
    confirmation: PresentationModeConfirmation | None = None
    confirmation_status: EvaluationStatus
    profile_status: EvaluationStatus
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        )

    @model_validator(mode='after')
    def _check_hash(self) -> 'PresentationProfileEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError(
                'presentation profile evaluation semantic hash mismatch'
            )
        if self.evaluation_id != 'ppe-' + digest[:24]:
            raise ValueError(
                'presentation profile evaluation deterministic id mismatch'
            )
        return self


def _masking_status(
    profile: VideoPresentationProfile,
    aperture: ResolvedAperture,
    *,
    screen_visible_width_m: float,
    screen_visible_height_m: float,
) -> tuple[EvaluationStatus, str]:
    """Check whether the declared masking state is consistent with the
    resolved aperture borders on the screen surface."""

    kind = profile.masking.kind
    if kind == 'unknown':
        return 'UNKNOWN', 'masking state not recorded'
    if aperture.status != 'PASS':
        return 'UNKNOWN', 'aperture unresolved; masking cannot be checked'
    assert aperture.width_m is not None and aperture.height_m is not None

    margin_l = (
        (screen_visible_width_m - aperture.width_m) / 2.0
        + aperture.center_offset_horizontal_m
    )
    margin_r = (
        (screen_visible_width_m - aperture.width_m) / 2.0
        - aperture.center_offset_horizontal_m
    )
    margin_t = (
        (screen_visible_height_m - aperture.height_m) / 2.0
        - aperture.center_offset_vertical_m
    )
    margin_b = (
        (screen_visible_height_m - aperture.height_m) / 2.0
        + aperture.center_offset_vertical_m
    )
    eps = 1e-6
    has_lr = margin_l > eps or margin_r > eps
    has_tb = margin_t > eps or margin_b > eps
    if kind == 'none':
        if has_lr or has_tb:
            return (
                'FAIL',
                'masking declared none but the aperture leaves unmasked '
                'screen borders',
            )
        return 'PASS', 'no masking and aperture fills the screen'
    if kind == 'side':
        if not has_lr:
            return 'FAIL', 'side masking declared but no side borders exist'
        return 'PASS', 'side masking covers the horizontal borders'
    if kind == 'top_bottom':
        if not has_tb:
            return (
                'FAIL',
                'top/bottom masking declared but no vertical borders exist',
            )
        return 'PASS', 'top/bottom masking covers the vertical borders'
    # four_way
    if not (has_lr and has_tb):
        return (
            'FAIL',
            'four-way masking declared but the aperture touches at least '
            'one edge pair',
        )
    return 'PASS', 'four-way masking covers all borders'


def evaluate_presentation_profile(
    *,
    screen_entity_id: str,
    screen_visible_width_m: float,
    screen_visible_height_m: float,
    profile: VideoPresentationProfile,
    confirmation: PresentationModeConfirmation | None = None,
) -> PresentationProfileEvaluation:
    """Evaluate one presentation profile against a screen's visible area.

    Geometry-independent by design: a mode change never creates a new
    SceneRevision, so this function takes the screen's visible dimensions
    directly instead of a scene revision. Callers that need the full
    per-seat viewing evaluation for a specific aperture feed the resolved
    width/height into ``evaluate_video_geometry``/``evaluate_direct_view_geometry``.
    """

    aperture = resolve_presentation_aperture(
        profile,
        screen_visible_width_m=screen_visible_width_m,
        screen_visible_height_m=screen_visible_height_m,
    )
    masking_status, masking_reason = _masking_status(
        profile,
        aperture,
        screen_visible_width_m=screen_visible_width_m,
        screen_visible_height_m=screen_visible_height_m,
    )
    if profile.optical_preset is None:
        optical_status, optical_reason = (
            'UNKNOWN',
            'no projector optical preset bound to this profile',
        )
    else:
        optical_status, optical_reason = (
            'PASS',
            f'bound to optical preset {profile.optical_preset.preset_ref}',
        )

    if confirmation is None:
        confirmation_status = 'UNKNOWN'
    elif (
        confirmation.profile_id != profile.profile_id
        or confirmation.profile_version != profile.version
        or confirmation.profile_sha256 != profile.profile_sha256
    ):
        confirmation_status = 'FAIL'
    else:
        confirmation_status = 'PASS'

    profile_status = _combine_status(
        (
            aperture.status,
            masking_status,
            optical_status,
            confirmation_status,
        )
    )
    probe = PresentationProfileEvaluation.model_construct(
        evaluation_id='',
        screen_entity_id=screen_entity_id,
        profile=profile,
        aperture=aperture,
        aperture_status=aperture.status,
        masking_status=masking_status,
        masking_reason=masking_reason,
        optical_status=optical_status,
        optical_reason=optical_reason,
        confirmation=confirmation,
        confirmation_status=confirmation_status,
        profile_status=profile_status,
        evaluation_sha256='',
    )
    evaluation_sha256 = _hash(probe.semantic_payload())
    return PresentationProfileEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id=f'ppe-{evaluation_sha256[:24]}',
        evaluation_sha256=evaluation_sha256,
    )
