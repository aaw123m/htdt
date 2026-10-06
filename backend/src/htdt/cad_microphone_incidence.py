"""Measurement-microphone angular-response / incidence authority (#732).

A valid microphone calibration does NOT imply that the microphone
measures the same sound-pressure quantity independently of incidence
angle, orientation, capsule geometry, calibration field or mounting
condition. ``calibration file applied`` is not angle-independent
pressure truth.

Basis: IEC 61094-3:2016 (primary free-field sensitivity — complex, per
reference direction), IEC 61094-5:2016 (pressure sensitivity by
comparison), IEC 61183:1994 (random-incidence/diffuse-field sensitivity
of sound level meters), ITU-R BS.2419-0 (2018, In force — microphone
directivity/diameter/orientation materially affect level calibration of
advanced multichannel systems; evidence for an applicability authority,
not a universal correction curve).

Free-field, pressure and random/diffuse-field calibration quantities
are not interchangeable labels for one universal microphone response,
and 0-degree/90-degree manufacturer correction files are never merged
into one generic profile.
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


CalibrationFieldKind = Literal[
    'pressure_sensitivity',
    'free_field_sensitivity',
    'random_incidence_sensitivity',
    'diffuse_field_sensitivity',
    'manufacturer_0deg_correction',
    'manufacturer_90deg_correction',
    'angular_response_dataset',
    'user_measured_directional_response',
    'unknown',
]
OrientationFrame = Literal[
    'world_fixed', 'operator_fixed', 'randomized_trajectory', 'unknown',
]
OrientationStrategy = Literal[
    'fixed_orientation_common_reference',
    'rotate_to_each_speaker',
    'angular_compensation_model',
    'small_capsule_assumed_limited_directivity',
    'provider_defined_method',
    'unknown',
]
IncidenceMeasurand = Literal[
    'direct_source',
    'room_response_full',
    'decay_metric',
    'early_reflection',
    'spatial_diffuse',
    'binaural_directional',
    'unknown',
]
IncidenceVerdict = Literal[
    'directly_applicable',
    'angular_effect_negligible_within_evidence',
    'correction_available',
    'limited',
    'incompatible',
    'unknown',
]
CorrectionScope = Literal[
    'measured_grid', 'derived_extrapolated', 'none', 'unknown',
]

FIELD_KIND_LABELS: dict[str, str] = {
    'pressure_sensitivity': '圧力感度校正',
    'free_field_sensitivity': '自由音場感度校正',
    'random_incidence_sensitivity': 'ランダム入射感度校正',
    'diffuse_field_sensitivity': '拡散音場感度校正',
    'manufacturer_0deg_correction': 'メーカー 0° 補正',
    'manufacturer_90deg_correction': 'メーカー 90° 補正',
    'angular_response_dataset': '角度応答データセット',
    'user_measured_directional_response': 'ユーザー実測指向特性',
    'unknown': '不明',
}
ORIENTATION_FRAME_LABELS: dict[str, str] = {
    'world_fixed': 'ワールド座標固定',
    'operator_fixed': 'オペレータ/機器固定',
    'randomized_trajectory': 'ランダム化/回転軌道',
    'unknown': '不明',
}
STRATEGY_LABELS: dict[str, str] = {
    'fixed_orientation_common_reference': '固定向き共通基準',
    'rotate_to_each_speaker': '各スピーカーへ回転',
    'angular_compensation_model': '角度補正モデル',
    'small_capsule_assumed_limited_directivity': '小型カプセル（指向性限定的仮定）',
    'provider_defined_method': 'プロバイダ定義手法',
    'unknown': '不明',
}
MEASURAND_LABELS: dict[str, str] = {
    'direct_source': '直接音ソース',
    'room_response_full': '室内応答全体',
    'decay_metric': '残響/減衰指標',
    'early_reflection': '初期反射',
    'spatial_diffuse': '空間/拡散場量',
    'binaural_directional': 'バイノーラル/方向指標',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'directly_applicable': '直接適用可能',
    'angular_effect_negligible_within_evidence': '証拠内で角度影響微小',
    'correction_available': '角度補正あり',
    'limited': '限定的',
    'incompatible': '不適合',
    'unknown': '不明',
}
SCOPE_LABELS: dict[str, str] = {
    'measured_grid': '実測グリッド内',
    'derived_extrapolated': '導出/外挿',
    'none': '補正なし',
    'unknown': '不明',
}

_FREE_FIELD_KINDS = {
    'free_field_sensitivity',
    'manufacturer_0deg_correction',
    'angular_response_dataset',
    'user_measured_directional_response',
}
_DIFFUSE_KINDS = {
    'random_incidence_sensitivity',
    'diffuse_field_sensitivity',
}
_DIFFUSE_MEASURANDS = {'spatial_diffuse', 'room_response_full', 'decay_metric'}
_DIRECTIONAL_MEASURANDS = {'binaural_directional'}


class MeasurementMicrophoneDirectionalProfile(BaseModel):
    """One physical microphone/capsule's directional calibration state.

    #611 owns the calibration lifecycle (validity, events, fitness);
    this profile consumes that evidence and declares which *field kind*
    the calibration expresses and whether angular-response data exist.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    microphone_ref: AuthorityRef
    calibration_ref: AuthorityRef | None = None
    calibration_field_kind: CalibrationFieldKind = 'unknown'
    capsule_diameter_mm: float | None = Field(default=None, gt=0)
    protection_grid_state: str = ''
    windscreen_state: str = ''
    reference_direction_deg: float | None = Field(
        default=None, ge=0, le=360
    )
    angular_dataset_ref: AuthorityRef | None = None
    angular_grid_deg: tuple[float, ...] = ()
    frequency_grid_hz: tuple[float, ...] = ()
    angle_sensitive_above_hz: float | None = Field(default=None, gt=0)
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'MeasurementMicrophoneDirectionalProfile':
        _require_refs(self.microphone_ref)
        for ref in (self.calibration_ref, self.angular_dataset_ref):
            if ref is not None:
                _require_refs(ref)
        if self.angular_dataset_ref is not None and not (
            self.angular_grid_deg and self.frequency_grid_hz
        ):
            raise ValueError(
                'an angular-response dataset must declare its measured '
                'angle and frequency grids'
            )
        if self.calibration_field_kind in (
            'angular_response_dataset',
            'user_measured_directional_response',
        ) and self.angular_dataset_ref is None:
            raise ValueError(
                'a directional-response calibration kind requires its '
                'angular dataset reference'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(
        cls, **kwargs: Any
    ) -> 'MeasurementMicrophoneDirectionalProfile':
        return _seal(cls, kwargs, 'profile_id', 'profile_sha256', 'mdpf')


class ReceiverOrientationState(BaseModel):
    """First-class orientation evidence for one capture.

    Orientation is measurement state, not display metadata: changing it
    can change a high-frequency result with no room or loudspeaker
    change, and #219 moving-microphone trajectories must declare whether
    orientation was world-fixed, device-fixed or uncontrolled.
    """

    model_config = ConfigDict(frozen=True)

    state_id: str
    state_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    capture_ref: AuthorityRef | None = None
    yaw_deg: float | None = None
    pitch_deg: float | None = None
    roll_deg: float | None = None
    reference_axis: str = ''
    orientation_frame: OrientationFrame = 'unknown'
    method: str = ''
    uncertainty_deg: float | None = Field(default=None, ge=0)
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'ReceiverOrientationState':
        if self.capture_ref is not None:
            _require_refs(self.capture_ref)
        if self.orientation_frame == 'world_fixed' and self.yaw_deg is None:
            raise ValueError(
                'a world-fixed orientation state requires measured '
                'orientation angles'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'state_id', 'state_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ReceiverOrientationState':
        return _seal(cls, kwargs, 'state_id', 'state_sha256', 'rors')


class MicrophoneIncidenceApplicability(BaseModel):
    """Sealed applicability of a correction to one capture/measurand.

    ``verdict`` is sealed by the producer; ``evaluate_incidence`` re-
    derives it so a 0-degree file cannot launder onto a 90-degree
    arrival, and a pressure calibration cannot pose as full-band
    free-field truth.
    """

    model_config = ConfigDict(frozen=True)

    applicability_id: str
    applicability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    orientation_ref: AuthorityRef | None = None
    strategy: OrientationStrategy = 'unknown'
    incidence_deg: float | None = Field(default=None, ge=0, le=180)
    measurand: IncidenceMeasurand = 'unknown'
    frequency_hz: float | None = Field(default=None, gt=0)
    correction_ref: AuthorityRef | None = None
    correction_scope: CorrectionScope = 'none'
    verdict: IncidenceVerdict = 'unknown'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'MicrophoneIncidenceApplicability':
        _require_refs(self.profile_ref)
        for ref in (self.orientation_ref, self.correction_ref):
            if ref is not None:
                _require_refs(ref)
        if self.correction_scope == 'measured_grid' and (
            self.correction_ref is None
        ):
            raise ValueError(
                'a measured-grid correction scope requires the '
                'correction dataset reference'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'applicability_id', 'applicability_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'MicrophoneIncidenceApplicability':
        return _seal(
            cls, kwargs, 'applicability_id', 'applicability_sha256',
            'miap',
        )


def evaluate_incidence(
    profile: MeasurementMicrophoneDirectionalProfile,
    orientation: ReceiverOrientationState | None,
    measurand: IncidenceMeasurand,
    incidence_deg: float | None,
    frequency_hz: float | None,
) -> tuple[IncidenceVerdict, CorrectionScope, tuple[str, ...]]:
    """Fail-closed applicability of a calibration to one observable.

    Frequency dependence is explicit: ``angle_sensitive_above_hz``
    (when declared by profile evidence) separates the angle-negligible
    band from the band where incidence matters. A pressure sensitivity
    can never claim free-field truth; a 0-degree correction degrades
    with incidence; a diffuse measurand needs a random/diffuse-field
    quantity, not a direct-sound correction.
    """
    reasons: list[str] = []
    kind = profile.calibration_field_kind
    if kind == 'unknown':
        return 'unknown', 'none', ('calibration_field_unknown',)

    angle_sensitive = False
    if (
        frequency_hz is not None
        and profile.angle_sensitive_above_hz is not None
        and frequency_hz >= profile.angle_sensitive_above_hz
    ):
        angle_sensitive = True
    elif (
        frequency_hz is not None
        and profile.angle_sensitive_above_hz is None
        and profile.angular_dataset_ref is None
        and kind in _FREE_FIELD_KINDS | _DIFFUSE_KINDS
    ):
        # No declared angular evidence: stay honest at all frequencies
        # rather than asserting angle independence.
        angle_sensitive = True

    orientation_known = (
        orientation is not None
        and orientation.yaw_deg is not None
        and orientation.orientation_frame != 'unknown'
    )
    incidence_known = incidence_deg is not None

    if measurand in _DIRECTIONAL_MEASURANDS and kind not in (
        'angular_response_dataset',
        'user_measured_directional_response',
    ):
        return 'incompatible', 'none', ('directional_measurand',)

    if kind == 'pressure_sensitivity':
        if measurand in _DIFFUSE_MEASURANDS or measurand in (
            'direct_source', 'early_reflection'
        ):
            # Pressure sensitivity is a different physical quantity —
            # usable honestly only where angular effects are negligible.
            if angle_sensitive:
                return 'limited', 'none', (
                    'pressure_calibration_not_free_field',
                )
            return 'angular_effect_negligible_within_evidence', 'none', ()
        return 'directly_applicable', 'none', ()

    if kind in _DIFFUSE_KINDS:
        if measurand in ('direct_source', 'early_reflection'):
            return 'limited', 'none', ('diffuse_quantity_for_direct_path',)
        return 'directly_applicable', 'none', ()

    # Free-field / manufacturer orientation-specific corrections.
    if kind == 'manufacturer_90deg_correction':
        reference_deg = 90.0
    else:
        reference_deg = profile.reference_direction_deg or 0.0

    if not orientation_known and incidence_deg is None:
        if angle_sensitive:
            return 'limited', 'none', ('orientation_or_incidence_unknown',)
        return 'angular_effect_negligible_within_evidence', 'none', ()
    if not incidence_known:
        if measurand in _DIFFUSE_MEASURANDS:
            return 'limited', 'none', ('diffuse_arrival_angles',)
        if angle_sensitive:
            return 'limited', 'none', ('incidence_unknown',)
        return 'angular_effect_negligible_within_evidence', 'none', ()

    deviation = abs(incidence_deg - reference_deg)
    if deviation <= 15.0:
        verdict: IncidenceVerdict = 'directly_applicable'
        scope: CorrectionScope = 'none'
    elif profile.angular_dataset_ref is not None:
        if (
            profile.angular_grid_deg
            and any(
                abs(g - incidence_deg) <= 15.0
                for g in profile.angular_grid_deg
            )
            and (
                frequency_hz is None
                or not profile.frequency_grid_hz
                or frequency_hz <= max(profile.frequency_grid_hz)
            )
        ):
            return 'correction_available', 'measured_grid', ()
        return 'limited', 'derived_extrapolated', (
            'correction_outside_measured_grid',
        )
    elif deviation <= 45.0:
        verdict = 'correction_available' if not angle_sensitive else 'limited'
        scope = 'none'
        reasons.append('off_axis_without_angular_evidence')
    else:
        if measurand in _DIFFUSE_MEASURANDS:
            return 'limited', 'none', ('diffuse_arrival_angles',)
        return 'incompatible', 'none', ('incidence_far_from_correction',)

    if angle_sensitive and verdict == 'directly_applicable':
        return 'correction_available' if (
            profile.angular_dataset_ref is not None
        ) else 'limited', scope, ('angle_sensitive_band',)
    return verdict, scope, tuple(reasons)
