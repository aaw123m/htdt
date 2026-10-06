"""Loudspeaker front-layer / grille-transfer authority (issue #735).

A loudspeaker hidden behind decorative fabric, a protective grille, a
perforated panel or architectural trim does not necessarily radiate
like the same loudspeaker in free air — even when the covering is
marketed as acoustically transparent. ``acoustically transparent =
true`` is never a full-band transfer function.

Basis: Kılıçkaya & Erol, Noise Control Engineering Journal 74(1)
49–66, 2026 (DOI 10.3397/1/37745 — grille thickness, hole geometry and
open area measurably change tweeter response in roughly the 3–20 kHz
region; geometry-specific findings, not universal rules), Olsen et al.,
AES Automotive Audio Conference 2017 P1-3 (loudspeaker + interface +
grille as one acoustic subsystem; geometry/material accuracy matters at
high frequency), manufacturer practice (e.g. Dynaudio grille-detection
EQ — evidence the state is acoustically material, not a universal
correction profile).

Projection screens remain #282/#166; treatment facing remains
#615/#631 — a porous absorber's absorption coefficient does not specify
speaker-through-fabric transmission loss, and the same material may
play both roles only through separate records.
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


FrontLayerKind = Literal[
    'oem_speaker_grille',
    'speaker_grille_cloth_only',
    'frame_plus_cloth',
    'perforated_metal',
    'perforated_plastic',
    'slotted_panel',
    'architectural_fabric',
    'acoustic_panel_facing',
    'decorative_trim_edge',
    'multi_layer_assembly',
    'unknown',
]
TransferEvidenceClass = Literal[
    'oem_validated_configuration',
    'manufacturer_measured',
    'independent_measured',
    'htdt_user_measured',
    'numerically_modeled',
    'empirical_derived',
    'marketing_declared',
    'unknown',
]
TransferKind = Literal[
    'complex_transfer',
    'impulse_response',
    'magnitude_only',
    'scalar_insertion_loss',
    'unknown',
]
BaseDatasetGrilleState = Literal[
    'grille_on', 'grille_off', 'unknown',
]
ApplicabilityVerdict = Literal[
    'base_directivity_directly_applicable',
    'base_directivity_plus_measured_transfer',
    'installed_front_layer_directivity_available',
    'on_axis_only_correction',
    'directivity_limited',
    'unknown',
]

LAYER_KIND_LABELS: dict[str, str] = {
    'oem_speaker_grille': 'OEM スピーカーグリル',
    'speaker_grille_cloth_only': 'グリルクロスのみ',
    'frame_plus_cloth': 'フレーム+クロス',
    'perforated_metal': '穿孔金属',
    'perforated_plastic': '穿孔プラスチック',
    'slotted_panel': 'スロットパネル',
    'architectural_fabric': '建築ファブリック',
    'acoustic_panel_facing': '吸音パネル表面材',
    'decorative_trim_edge': '装飾トリム/エッジ',
    'multi_layer_assembly': '多層アセンブリ',
    'unknown': '不明',
}
EVIDENCE_CLASS_LABELS: dict[str, str] = {
    'oem_validated_configuration': 'OEM 検証済み構成',
    'manufacturer_measured': 'メーカー実測',
    'independent_measured': '独立実測',
    'htdt_user_measured': 'HTDT ユーザー実測',
    'numerically_modeled': '数値モデル',
    'empirical_derived': '経験式/導出',
    'marketing_declared': 'マーケティング記載',
    'unknown': '不明',
}
TRANSFER_KIND_LABELS: dict[str, str] = {
    'complex_transfer': '複素伝達関数',
    'impulse_response': 'インパルス応答',
    'magnitude_only': '振幅のみ伝達',
    'scalar_insertion_loss': 'スカラー挿入損失',
    'unknown': '不明',
}
GRILLE_STATE_LABELS: dict[str, str] = {
    'grille_on': 'グリルありデータ',
    'grille_off': 'グリルなしデータ',
    'unknown': '不明',
}
APPLICABILITY_LABELS: dict[str, str] = {
    'base_directivity_directly_applicable': 'ベース指向特性を直接適用',
    'base_directivity_plus_measured_transfer': 'ベース指向特性+実測伝達',
    'installed_front_layer_directivity_available': '前面層込み指向特性あり',
    'on_axis_only_correction': 'オンアクシス補正のみ',
    'directivity_limited': '指向特性制限',
    'unknown': '不明',
}

_MEASURED_CLASSES = {
    'oem_validated_configuration',
    'manufacturer_measured',
    'independent_measured',
    'htdt_user_measured',
    'numerically_modeled',
}


class LoudspeakerFrontLayer(BaseModel):
    """One non-screen layer between loudspeaker and room.

    Material, geometry, frame and spacing all participate in identity —
    the same fabric on a different frame, or the same grille at a
    different spacing, is a different acoustic subsystem, and as-built
    state (#613) is bound so a changed layer stales dependent
    predictions (#729).
    """

    model_config = ConfigDict(frozen=True)

    layer_id: str
    layer_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    loudspeaker_ref: AuthorityRef
    kind: FrontLayerKind = 'unknown'
    material: str = ''
    thickness_mm: float | None = Field(default=None, ge=0)
    open_area_ratio: float | None = Field(default=None, ge=0, le=1)
    hole_geometry: str = ''
    frame_present: bool = False
    frame_thickness_mm: float | None = Field(default=None, ge=0)
    spacing_mm: float | None = Field(default=None, ge=0)
    orientation_deg: float | None = Field(default=None, ge=0, le=360)
    asbuilt_ref: AuthorityRef | None = None
    provenance_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'LoudspeakerFrontLayer':
        _require_refs(self.loudspeaker_ref)
        for ref in (self.asbuilt_ref, self.provenance_ref):
            if ref is not None:
                _require_refs(ref)
        if self.kind in ('frame_plus_cloth', 'multi_layer_assembly') and (
            not self.frame_present
        ):
            raise ValueError(
                f'a {self.kind} layer declares a frame — frame_present '
                'must be true'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'layer_id', 'layer_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'LoudspeakerFrontLayer':
        return _seal(cls, kwargs, 'layer_id', 'layer_sha256', 'lfly')


class GrilleTransferEvidence(BaseModel):
    """Measured/modeled/declared transfer through one front layer.

    Magnitude-only and complex transfer stay distinct — a comb/cavity
    mechanism is never reduced to minimum-phase insertion loss without
    evidence. The applicability window (spacing, angles, frequencies)
    bounds where the transfer may be applied; outside it the evidence
    is derived/extrapolated, not measured.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    layer_ref: AuthorityRef
    evidence_class: TransferEvidenceClass = 'unknown'
    transfer_kind: TransferKind = 'unknown'
    frequency_min_hz: float | None = Field(default=None, gt=0)
    frequency_max_hz: float | None = Field(default=None, gt=0)
    spacing_min_mm: float | None = Field(default=None, ge=0)
    spacing_max_mm: float | None = Field(default=None, ge=0)
    measured_angles_deg: tuple[float, ...] = ()
    layer_on_capture_ref: AuthorityRef | None = None
    layer_off_capture_ref: AuthorityRef | None = None
    oem_compensation_state: str = ''
    uncertainty_db: float | None = Field(default=None, ge=0)
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'GrilleTransferEvidence':
        _require_refs(self.layer_ref)
        for ref in (
            self.layer_on_capture_ref, self.layer_off_capture_ref
        ):
            if ref is not None:
                _require_refs(ref)
        if (
            (self.frequency_min_hz is None)
            != (self.frequency_max_hz is None)
        ):
            raise ValueError(
                'a frequency applicability window needs both edges'
            )
        if (
            self.frequency_min_hz is not None
            and self.frequency_max_hz is not None
            and self.frequency_max_hz <= self.frequency_min_hz
        ):
            raise ValueError(
                'frequency_max_hz must exceed frequency_min_hz'
            )
        if (
            (self.spacing_min_mm is None)
            != (self.spacing_max_mm is None)
        ):
            raise ValueError(
                'a spacing applicability window needs both edges'
            )
        if (
            self.spacing_min_mm is not None
            and self.spacing_max_mm is not None
            and self.spacing_max_mm < self.spacing_min_mm
        ):
            raise ValueError(
                'spacing_max_mm must be >= spacing_min_mm'
            )
        if (
            self.evidence_class == 'htdt_user_measured'
            and (
                self.layer_on_capture_ref is None
                or self.layer_off_capture_ref is None
            )
        ):
            raise ValueError(
                'a field-measured transfer requires both layer-on and '
                'layer-off captures under controlled geometry'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'GrilleTransferEvidence':
        return _seal(
            cls, kwargs, 'evidence_id', 'evidence_sha256', 'gtrf'
        )


class FrontLayerApplicability(BaseModel):
    """Sealed verdict on how base loudspeaker data may be used.

    ``verdict`` is sealed by the producer; ``evaluate_front_layer``
    re-derives it. A ``grille_on`` base dataset must not receive a
    second grille correction — double application is rejected at save
    time.
    """

    model_config = ConfigDict(frozen=True)

    applicability_id: str
    applicability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    layer_ref: AuthorityRef | None = None
    evidence_ref: AuthorityRef | None = None
    base_dataset_ref: AuthorityRef
    base_dataset_grille_state: BaseDatasetGrilleState = 'unknown'
    verdict: ApplicabilityVerdict = 'unknown'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'FrontLayerApplicability':
        _require_refs(self.base_dataset_ref)
        for ref in (self.layer_ref, self.evidence_ref):
            if ref is not None:
                _require_refs(ref)
        if (
            self.base_dataset_grille_state == 'grille_on'
            and self.verdict
            in (
                'base_directivity_plus_measured_transfer',
                'on_axis_only_correction',
            )
        ):
            raise ValueError(
                'the base dataset is already grille-on — applying a '
                'second grille correction double-counts the layer'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'applicability_id', 'applicability_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'FrontLayerApplicability':
        return _seal(
            cls, kwargs, 'applicability_id', 'applicability_sha256',
            'flap',
        )


def evaluate_front_layer(
    layer: LoudspeakerFrontLayer | None,
    evidence: GrilleTransferEvidence | None,
    asbuilt_spacing_mm: float | None = None,
) -> tuple[ApplicabilityVerdict, tuple[str, ...]]:
    """Fail-closed applicability of a bare-driver dataset.

    No layer → the base directivity applies directly. Unknown or
    marketing-declared transfer → ``unknown``: the bare response is
    NOT applied (a marketing phrase is not measured truth). Measured
    or modeled transfer within its spacing/frequency window composes
    with the base dataset; outside the window the layer makes the base
    directivity limited, never silently applicable.
    """
    if layer is None:
        return 'base_directivity_directly_applicable', ()
    if layer.kind == 'unknown':
        return 'unknown', ('layer_kind_unknown',)
    if evidence is None or evidence.evidence_class in (
        'unknown', 'marketing_declared',
    ):
        return 'unknown', ('transfer_evidence_absent_or_marketing',)
    if evidence.evidence_class not in _MEASURED_CLASSES:
        return 'unknown', ('transfer_evidence_derived_only',)
    spacing = (
        asbuilt_spacing_mm
        if asbuilt_spacing_mm is not None
        else layer.spacing_mm
    )
    if (
        spacing is not None
        and evidence.spacing_min_mm is not None
        and evidence.spacing_max_mm is not None
        and not (
            evidence.spacing_min_mm <= spacing <= evidence.spacing_max_mm
        )
    ):
        return 'directivity_limited', ('spacing_outside_evidence_window',)
    if evidence.transfer_kind in ('complex_transfer', 'impulse_response'):
        if evidence.measured_angles_deg:
            return 'installed_front_layer_directivity_available', ()
        return 'base_directivity_plus_measured_transfer', ()
    if evidence.transfer_kind == 'magnitude_only':
        if evidence.measured_angles_deg:
            return 'base_directivity_plus_measured_transfer', ()
        return 'on_axis_only_correction', ()
    if evidence.transfer_kind == 'scalar_insertion_loss':
        return 'on_axis_only_correction', ('scalar_loss_only',)
    return 'unknown', ('transfer_kind_unknown',)
