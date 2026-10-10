"""Seating / occupancy acoustic authority (#590).

Home-theater seating changes the direct-sound story twice over: the seat
back can geometrically block speaker -> ear paths, and occupied seats add
absorption that an empty-room prediction never sees. This module binds
both layers as honest, sealed records:

- :class:`SeatAcousticModel` — per-seat geometry evidence (measured
  product vs generic-assumed vs CAD-derived, always labelled) plus a
  *separate* acoustic-evidence layer (lab block measurements, product
  data, literature-generic values, in-situ estimates, calibrated
  models, user assumptions). The geometric layer and the acoustic layer
  never upgrade each other.
- :class:`OccupancyScenario` — which seats are occupied under which
  occupancy state, with the seating-zone block parameters (perimeter /
  area, row spacing, rake) that drive occupied-absorption estimates, and
  the occupant model class. The scenario's content hash is the
  comparability key: predictions and measurements taken under different
  occupancy scenarios are not comparable.
- :class:`DirectSoundClearanceEvaluation` — per speaker -> listener
  geometric line-of-sight verdicts
  (clear / partially_occluded / occluded /
  clear_with_position_tolerance_risk / unknown_geometry). This is an
  *eligibility gate*, never an attenuation estimate — a path judged
  occluded fails closed; equalization cannot fix a blocked direct path.
- :class:`SeatingCommissioningResult` — as-built vs design: actual seat
  models, recline state, measured ear position, occupancy scenario; the
  failure reasons stay enumerable
  (seat_back_occlusion / preceding_row_occlusion /
  bar_furniture_occlusion / ear_height_outside_design_range /
  occupancy_state_mismatch / seating_acoustic_model_unknown).

Contract properties:

- ``generic_assumed`` geometry and ``literature_generic`` /
  ``user_assumed`` absorption can never be marked measurement-grade —
  there is nothing measured to grade;
- a listener's ear position is a separate authority from the seat's
  reference point: the seat does not decide where the head is;
- occupancy comparability is content-derived: the same occupied-seat set
  + zone under the same state compares, anything else reports
  ``incompatible`` (unknown state reports ``unknown``);
- clearance verdicts are geometric claims only — they carry no dB.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...cad_equipment import EquipmentDataProvenance, FrequencyDomain
from ...cad_scene import Offset3, Position3
from ...canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

SEATING_ACOUSTICS_SCHEMA_VERSION = 1
SEATING_ACOUSTICS_AUTHORITY_VERSION = 'seating-acoustics-1'

SeatGeometrySource = Literal[
    'measured_product',
    'cad_derived',
    'generic_assumed',
    'unknown',
]

SeatAcousticEvidenceClass = Literal[
    'lab_measured_seating_block',
    'product_manufacturer',
    'literature_generic',
    'in_situ_estimated',
    'calibrated_model',
    'user_assumed',
    'unknown',
]

OccupancyState = Literal[
    'empty',
    'seats_present_unoccupied',
    'design_occupancy',
    'partial_occupancy',
    'full_occupancy',
    'custom',
    'unknown',
]

OccupantModelClass = Literal[
    'generic_occupant_model',
    'measured_research_profile',
    'project_assumed',
    'unknown',
]

UpholsteryClass = Literal[
    'lightly_upholstered',
    'medium_upholstered',
    'heavily_upholstered',
    'leather',
    'unknown',
]

ClearanceStatus = Literal[
    'clear',
    'partially_occluded',
    'occluded',
    'clear_with_position_tolerance_risk',
    'unknown_geometry',
]

OccluderKind = Literal[
    'seat_back',
    'preceding_row',
    'bar_furniture',
    'other_listener',
    'other_furniture',
    'unknown',
]

OcclusionFailureReason = Literal[
    'seat_back_occlusion',
    'preceding_row_occlusion',
    'bar_furniture_occlusion',
    'ear_height_outside_design_range',
    'occupancy_state_mismatch',
    'seating_acoustic_model_unknown',
    'none',
]

SpeakerBandSensitivity = Literal[
    'high_frequency_direct_vulnerable',
    'broadband',
    'low_frequency_insensitive',
    'unknown',
]

OccupancyComparability = Literal[
    'compatible',
    'incompatible',
    'unknown',
]

#: JA labels for UI display.
SEAT_GEOMETRY_SOURCE_LABELS = {
    'measured_product': '実測製品',
    'cad_derived': 'CAD由来',
    'generic_assumed': '汎用仮定',
    'unknown': '不明',
}

SEAT_ACOUSTIC_EVIDENCE_LABELS = {
    'lab_measured_seating_block': '座席ブロック実測',
    'product_manufacturer': '製品公表値',
    'literature_generic': '文献汎用値',
    'in_situ_estimated': '現地推定',
    'calibrated_model': '校正済みモデル',
    'user_assumed': 'ユーザー仮定',
    'unknown': '不明',
}

OCCUPANCY_STATE_LABELS = {
    'empty': '空室',
    'seats_present_unoccupied': '座席あり・無人',
    'design_occupancy': '設計占有',
    'partial_occupancy': '部分占有',
    'full_occupancy': '満席',
    'custom': 'カスタム',
    'unknown': '不明',
}

CLEARANCE_STATUS_LABELS = {
    'clear': 'クリア',
    'partially_occluded': '一部遮蔽',
    'occluded': '遮蔽',
    'clear_with_position_tolerance_risk': 'クリア（位置公差リスク）',
    'unknown_geometry': '形状不明',
}

OCCLUDER_KIND_LABELS = {
    'seat_back': 'シートバック',
    'preceding_row': '前列',
    'bar_furniture': 'バー・家具',
    'other_listener': '他の聴取者',
    'other_furniture': 'その他家具',
    'unknown': '不明',
}

OCCLUSION_FAILURE_LABELS = {
    'seat_back_occlusion': 'シートバック遮蔽',
    'preceding_row_occlusion': '前列遮蔽',
    'bar_furniture_occlusion': 'バー・家具遮蔽',
    'ear_height_outside_design_range': '耳位置が設計範囲外',
    'occupancy_state_mismatch': '占有状態不一致',
    'seating_acoustic_model_unknown': '座席音響モデル不明',
    'none': 'なし',
}

OCCUPANCY_COMPARABILITY_LABELS = {
    'compatible': '比較可能',
    'incompatible': '比較不可',
    'unknown': '不明',
}


def _require_finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


class SeatGeometryEvidence(BaseModel):
    """Geometric evidence for one seat entity. ``source`` declares where
    the numbers came from; ``generic_assumed`` is honest but cannot carry
    product-grade claims."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_entity_id: str = Field(min_length=1)
    source: SeatGeometrySource
    seat_pan_height_m: float | None = None
    backrest_height_m: float | None = None
    backrest_width_m: float | None = None
    backrest_thickness_m: float | None = None
    headrest_height_m: float | None = None
    armrest_present: bool | None = None
    recline_deg: float | None = None
    #: World-space envelope box: origin + extents.
    envelope_origin: Position3 | None = None
    envelope_size: Offset3 | None = None
    occupied_envelope_origin: Position3 | None = None
    occupied_envelope_size: Offset3 | None = None
    upholstery_class: UpholsteryClass = 'unknown'
    provenance: EquipmentDataProvenance | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'SeatGeometryEvidence':
        for name in (
            'seat_pan_height_m',
            'backrest_height_m',
            'backrest_width_m',
            'backrest_thickness_m',
            'headrest_height_m',
            'recline_deg',
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if (self.envelope_origin is None) != (self.envelope_size is None):
            raise ValueError(
                'envelope_origin and envelope_size must be set together'
            )
        if (
            self.occupied_envelope_origin is None
        ) != (self.occupied_envelope_size is None):
            raise ValueError(
                'occupied envelope fields must be set together'
            )
        return self


class SeatingZoneBlock(BaseModel):
    """Seating-zone block parameters that drive occupied absorption —
    perimeter/area effects are first-class (Bradley 1996)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_count: int = Field(ge=0)
    row_spacing_m: float | None = None
    perimeter_m: float | None = None
    occupied_area_m2: float | None = None
    rake_deg: float | None = None
    riser_present: bool | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'SeatingZoneBlock':
        for name in (
            'row_spacing_m', 'perimeter_m', 'occupied_area_m2', 'rake_deg'
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if self.occupied_area_m2 is not None and self.occupied_area_m2 < 0.0:
            raise ValueError('occupied_area_m2 must be >= 0')
        return self


class SeatAcousticEvidence(BaseModel):
    """The acoustic layer — kept separate from geometry. ``measurement_
    grade`` is only reachable for lab/in-situ/calibrated evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_class: SeatAcousticEvidenceClass
    applies_to_state: Literal[
        'unoccupied', 'occupied', 'either', 'unknown'
    ] = 'unknown'
    zone_block: SeatingZoneBlock | None = None
    upholstery_class: UpholsteryClass = 'unknown'
    band_coefficients: dict[str, float] | None = None
    band_spec: Literal['octave', 'third_octave', 'other'] | None = None
    method_ref: str | None = None
    standard_ref: str | None = None
    publication_ref: str | None = None
    uncertainty_db: float | None = None
    measurement_grade: bool = False
    applicability_note: str | None = None
    provenance: EquipmentDataProvenance | None = None

    @model_validator(mode='after')
    def _honesty(self) -> 'SeatAcousticEvidence':
        if self.uncertainty_db is not None:
            _require_finite(self.uncertainty_db, 'uncertainty_db')
        if self.band_coefficients is not None:
            for key, value in self.band_coefficients.items():
                _require_finite(value, f'band_coefficients[{key!r}]')
                if not 0.0 <= value <= 2.0:
                    raise ValueError(
                        'absorption coefficients must stay in [0, 2]'
                    )
        if self.measurement_grade and self.evidence_class in (
            'literature_generic',
            'user_assumed',
            'unknown',
        ):
            raise ValueError(
                'generic/assumed evidence cannot be measurement_grade'
            )
        if (self.band_coefficients is not None) != (self.band_spec is not None):
            raise ValueError('band_coefficients and band_spec together')
        return self


class SeatAcousticModel(BaseModel):
    """One seat's acoustic authority record — geometry + acoustics bound
    to the seat entity. Content-sealed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_model_id: str
    document_id: str = Field(min_length=1)
    seat_entity_id: str = Field(min_length=1)
    geometry: SeatGeometryEvidence
    acoustics: tuple[SeatAcousticEvidence, ...] = ()
    provenance: EquipmentDataProvenance | None = None
    note: str | None = None
    seat_model_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'SeatAcousticModel':
        if self.geometry.seat_entity_id != self.seat_entity_id:
            raise ValueError('geometry must describe the same seat')
        expected = _digest(self.identity_payload())
        if self.seat_model_sha256 != expected:
            raise ValueError('seat_model_sha256 does not match content')
        if self.seat_model_id != f'sam:{expected}':
            raise ValueError('seat_model_id must be sam:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('seat_model_id', None)
        payload.pop('seat_model_sha256', None)
        return payload

    @property
    def geometric_model_state(self) -> Literal['evidenced', 'assumed', 'unknown']:
        if self.geometry.source in ('measured_product', 'cad_derived'):
            return 'evidenced'
        if self.geometry.source == 'generic_assumed':
            return 'assumed'
        return 'unknown'

    @property
    def acoustic_model_state(self) -> Literal['evidenced', 'assumed', 'unknown']:
        if not self.acoustics:
            return 'unknown'
        classes = {a.evidence_class for a in self.acoustics}
        if classes & {
            'lab_measured_seating_block',
            'product_manufacturer',
            'in_situ_estimated',
            'calibrated_model',
        }:
            return 'evidenced'
        if classes & {'literature_generic', 'user_assumed'}:
            return 'assumed'
        return 'unknown'


class ListenerEarAuthority(BaseModel):
    """Ear/head authority for a listener at a seat — a separate record
    from the seat's reference point. The seat does not decide where the
    head is."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    listener_ref: str = Field(min_length=1)
    seat_entity_id: str | None = None
    nominal_ear_position: Position3 | None = None
    nominal_ear_left: Position3 | None = None
    nominal_ear_right: Position3 | None = None
    head_orientation_yaw_deg: float | None = None
    seated_height_range_m: tuple[float, float] | None = None
    position_tolerance_m: float | None = None
    occupant_class: Literal['adult', 'child', 'unknown'] = 'unknown'
    source: Literal['measured', 'design_nominal', 'assumed', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def _finite(self) -> 'ListenerEarAuthority':
        if self.head_orientation_yaw_deg is not None:
            _require_finite(
                self.head_orientation_yaw_deg, 'head_orientation_yaw_deg'
            )
        if self.position_tolerance_m is not None:
            _require_finite(
                self.position_tolerance_m, 'position_tolerance_m'
            )
        if self.seated_height_range_m is not None:
            low, high = self.seated_height_range_m
            _require_finite(low, 'seated_height_range_m[0]')
            _require_finite(high, 'seated_height_range_m[1]')
            if low > high:
                raise ValueError('seated height range inverted')
        return self


class OccupancyScenario(BaseModel):
    """Which seats are occupied, under which declared state, over which
    zone. Its content hash is the comparability key."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    occupancy_scenario_id: str
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: OccupancyState
    occupied_seat_ids: tuple[str, ...] = ()
    unoccupied_seat_ids: tuple[str, ...] = ()
    zone_block: SeatingZoneBlock | None = None
    acoustic_evidence_refs: tuple[tuple[str, str], ...] = ()
    occupant_model: OccupantModelClass = 'unknown'
    provenance: EquipmentDataProvenance | None = None
    created_at_utc: str = Field(min_length=1)
    note: str | None = None
    occupancy_sha256: str = Field(pattern=_SHA256_PATTERN)
    comparability_key: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'OccupancyScenario':
        overlap = set(self.occupied_seat_ids) & set(self.unoccupied_seat_ids)
        if overlap:
            raise ValueError(
                'a seat cannot be both occupied and unoccupied'
            )
        for pair in self.acoustic_evidence_refs:
            if len(pair) != 2 or not pair[0] or not pair[1]:
                raise ValueError(
                    'acoustic_evidence_refs entries must be (id, sha)'
                )
        expected = _digest(self.identity_payload())
        if self.occupancy_sha256 != expected:
            raise ValueError('occupancy_sha256 does not match content')
        if self.occupancy_scenario_id != f'ocs:{expected}':
            raise ValueError('occupancy_scenario_id must be ocs:<sha256>')
        key = _digest(self.comparability_payload())
        if self.comparability_key != key:
            raise ValueError('comparability_key does not match content')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        for field in ('occupancy_scenario_id', 'occupancy_sha256', 'comparability_key'):
            payload.pop(field, None)
        return payload

    def comparability_payload(self) -> dict[str, Any]:
        """The occupancy-affecting subset that decides comparability."""
        return {
            'state': self.state,
            'occupied_seat_ids': sorted(self.occupied_seat_ids),
            'unoccupied_seat_ids': sorted(self.unoccupied_seat_ids),
            'zone_block': (
                self.zone_block.model_dump(mode='json')
                if self.zone_block is not None
                else None
            ),
            'occupant_model': self.occupant_model,
        }


class PathOccluder(BaseModel):
    """A geometric occluder between speaker and ear — an axis-aligned
    box. ``kind`` says what the box is so the failure reason is
    enumerable."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    occluder_id: str = Field(min_length=1)
    kind: OccluderKind
    origin: Position3
    size: Offset3
    seat_entity_id: str | None = None
    note: str | None = None


class ClearancePathResult(BaseModel):
    """Per speaker -> listener verdict. Geometric claims only."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    speaker_id: str = Field(min_length=1)
    listener_ref: str = Field(min_length=1)
    status: ClearanceStatus
    band_sensitivity: SpeakerBandSensitivity = 'unknown'
    obstructor_ids: tuple[str, ...] = ()
    failure_reason: OcclusionFailureReason = 'none'
    method: Literal[
        'geometric_line_of_sight', 'not_evaluated'
    ] = 'geometric_line_of_sight'
    limitation_note: str | None = None


class DirectSoundClearanceEvaluation(BaseModel):
    """Clearance gate across declared speaker -> listener paths, bound
    to an occupancy scenario. Content-sealed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluation_id: str
    document_id: str = Field(min_length=1)
    occupancy_scenario_id: str = Field(min_length=1)
    occupancy_scenario_sha256: str = Field(min_length=1)
    listener_ref: str = Field(min_length=1)
    results: tuple[ClearancePathResult, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'DirectSoundClearanceEvaluation':
        expected = _digest(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('evaluation_sha256 does not match content')
        if self.evaluation_id != f'dce:{expected}':
            raise ValueError('evaluation_id must be dce:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('evaluation_id', None)
        payload.pop('evaluation_sha256', None)
        return payload


class SeatingCommissioningResult(BaseModel):
    """As-built vs design record. Failure reasons stay enumerable so the
    commissioning report can say *why* direct-sound goals failed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_id: str
    document_id: str = Field(min_length=1)
    seat_model_refs: tuple[tuple[str, str], ...] = ()
    measured_recline_deg: float | None = None
    measured_ear_position: Position3 | None = None
    design_ear_position: Position3 | None = None
    ear_position_deviation_m: float | None = None
    occupancy_scenario_ref: tuple[str, str] | None = None
    failure_reasons: tuple[OcclusionFailureReason, ...] = ()
    verdict: Literal['matches_design', 'deviated', 'unknown'] = 'unknown'
    measured_at_utc: str = Field(min_length=1)
    note: str | None = None
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'SeatingCommissioningResult':
        for pair in self.seat_model_refs:
            if len(pair) != 2 or not pair[0] or not pair[1]:
                raise ValueError(
                    'seat_model_refs entries must be (id, sha)'
                )
        for name in ('measured_recline_deg', 'ear_position_deviation_m'):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if self.occupancy_scenario_ref is not None and (
            len(self.occupancy_scenario_ref) != 2
            or not self.occupancy_scenario_ref[0]
        ):
            raise ValueError('occupancy_scenario_ref must be (id, sha)')
        expected = _digest(self.identity_payload())
        if self.result_sha256 != expected:
            raise ValueError('result_sha256 does not match content')
        if self.result_id != f'scr:{expected}':
            raise ValueError('result_id must be scr:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('result_id', None)
        payload.pop('result_sha256', None)
        return payload


def _point_in_box(
    point: Position3, origin: Position3, size: Offset3
) -> bool:
    return (
        origin.x_m <= point.x_m <= origin.x_m + size.x_m
        and origin.y_m <= point.y_m <= origin.y_m + size.y_m
        and origin.z_m <= point.z_m <= origin.z_m + size.z_m
    )


def _segment_hits_box(
    start: Position3, end: Position3, occluder: PathOccluder
) -> bool:
    """Axis-aligned slab test for segment -> box intersection."""
    d = (
        end.x_m - start.x_m,
        end.y_m - start.y_m,
        end.z_m - start.z_m,
    )
    lo = (
        occluder.origin.x_m,
        occluder.origin.y_m,
        occluder.origin.z_m,
    )
    hi = (
        occluder.origin.x_m + occluder.size.x_m,
        occluder.origin.y_m + occluder.size.y_m,
        occluder.origin.z_m + occluder.size.z_m,
    )
    s = (start.x_m, start.y_m, start.z_m)
    t_min, t_max = 0.0, 1.0
    for axis in range(3):
        if abs(d[axis]) < 1e-12:
            if s[axis] < lo[axis] or s[axis] > hi[axis]:
                return False
            continue
        t1 = (lo[axis] - s[axis]) / d[axis]
        t2 = (hi[axis] - s[axis]) / d[axis]
        t_near, t_far = min(t1, t2), max(t1, t2)
        t_min = max(t_min, t_near)
        t_max = min(t_max, t_far)
        if t_min > t_max:
            return False
    return True


def _segment_box_clearance(
    start: Position3, end: Position3, occluder: PathOccluder
) -> float:
    """Approximate minimum clearance: distance from box centre-line
    sample to segment, sampled coarsely. Used only to flag the
    position-tolerance risk state."""
    best = float('inf')
    steps = 21
    for i in range(steps):
        t = i / (steps - 1)
        px = start.x_m + t * (end.x_m - start.x_m)
        py = start.y_m + t * (end.y_m - start.y_m)
        pz = start.z_m + t * (end.z_m - start.z_m)
        dx = max(
            occluder.origin.x_m - px,
            0.0,
            px - (occluder.origin.x_m + occluder.size.x_m),
        )
        dy = max(
            occluder.origin.y_m - py,
            0.0,
            py - (occluder.origin.y_m + occluder.size.y_m),
        )
        dz = max(
            occluder.origin.z_m - pz,
            0.0,
            pz - (occluder.origin.z_m + occluder.size.z_m),
        )
        dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        best = min(best, dist)
    return best


_FAILURE_REASON_BY_OCCLUDER = {
    'seat_back': 'seat_back_occlusion',
    'preceding_row': 'preceding_row_occlusion',
    'bar_furniture': 'bar_furniture_occlusion',
    'other_listener': 'seat_back_occlusion',
    'other_furniture': 'bar_furniture_occlusion',
    'unknown': 'seat_back_occlusion',
}


def evaluate_clearance_path(
    *,
    speaker_id: str,
    speaker_position: Position3,
    listener: ListenerEarAuthority,
    occluders: Sequence[PathOccluder],
    band_sensitivity: SpeakerBandSensitivity = 'unknown',
    tolerance_risk_margin_m: float = 0.10,
) -> ClearancePathResult:
    """Geometric line-of-sight eligibility gate for one speaker ->
    listener pair. No attenuation is estimated — a blocked path is a
    blocked path, and EQ cannot fix it.

    - ``unknown_geometry`` when the ear authority has no position;
    - ``occluded`` when any occluder box intersects the segment;
    - ``clear_with_position_tolerance_risk`` when the path clears but a
      seat-back/row occluder sits within the tolerance margin — posture
      or ear-height drift could block it;
    - ``clear`` otherwise.
    """
    ear = listener.nominal_ear_position
    if ear is None:
        return ClearancePathResult(
            speaker_id=speaker_id,
            listener_ref=listener.listener_ref,
            status='unknown_geometry',
            band_sensitivity=band_sensitivity,
            method='not_evaluated',
            limitation_note='listener ear position undeclared',
        )
    hits = [
        occluder
        for occluder in occluders
        if _segment_hits_box(speaker_position, ear, occluder)
        # An occluder containing the ear endpoint cannot block the ray to
        # that ear — occlusion is a path property, not a destination one.
        and not _point_in_box(ear, occluder.origin, occluder.size)
    ]
    if hits:
        kinds = {occluder.kind for occluder in hits}
        reason: OcclusionFailureReason = 'seat_back_occlusion'
        for kind in ('seat_back', 'preceding_row', 'bar_furniture'):
            if kind in kinds:
                reason = _FAILURE_REASON_BY_OCCLUDER[kind]  # type: ignore[assignment]
                break
        return ClearancePathResult(
            speaker_id=speaker_id,
            listener_ref=listener.listener_ref,
            status='occluded',
            band_sensitivity=band_sensitivity,
            obstructor_ids=tuple(o.occluder_id for o in hits),
            failure_reason=reason,
        )
    near = [
        occluder
        for occluder in occluders
        if occluder.kind in ('seat_back', 'preceding_row')
        and _segment_box_clearance(speaker_position, ear, occluder)
        < tolerance_risk_margin_m
    ]
    if near:
        return ClearancePathResult(
            speaker_id=speaker_id,
            listener_ref=listener.listener_ref,
            status='clear_with_position_tolerance_risk',
            band_sensitivity=band_sensitivity,
            obstructor_ids=tuple(o.occluder_id for o in near),
            limitation_note='within position-tolerance margin of occlusion',
        )
    return ClearancePathResult(
        speaker_id=speaker_id,
        listener_ref=listener.listener_ref,
        status='clear',
        band_sensitivity=band_sensitivity,
    )


def build_clearance_evaluation(
    *,
    document_id: str,
    occupancy_scenario: OccupancyScenario,
    listener_ref: str,
    results: Sequence[ClearancePathResult],
    evaluated_at_utc: str,
) -> DirectSoundClearanceEvaluation:
    probe = DirectSoundClearanceEvaluation.model_construct(
        **_canon(
            DirectSoundClearanceEvaluation,
            dict(
                evaluation_id='',
                document_id=document_id,
                occupancy_scenario_id=occupancy_scenario.occupancy_scenario_id,
                occupancy_scenario_sha256=occupancy_scenario.occupancy_sha256,
                listener_ref=listener_ref,
                results=tuple(results),
                evaluated_at_utc=evaluated_at_utc,
                evaluation_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return DirectSoundClearanceEvaluation(
        **probe.model_dump(
            exclude={'evaluation_id', 'evaluation_sha256'}
        ),
        evaluation_id=f'dce:{sha}',
        evaluation_sha256=sha,
    )


def build_seat_model(
    *,
    document_id: str,
    seat_entity_id: str,
    geometry: SeatGeometryEvidence,
    acoustics: Sequence[SeatAcousticEvidence] = (),
    **fields: Any,
) -> SeatAcousticModel:
    probe = SeatAcousticModel.model_construct(
        **_canon(
            SeatAcousticModel,
            dict(
                seat_model_id='',
                document_id=document_id,
                seat_entity_id=seat_entity_id,
                geometry=geometry,
                acoustics=tuple(acoustics),
                seat_model_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return SeatAcousticModel(
        **probe.model_dump(
            exclude={'seat_model_id', 'seat_model_sha256'}
        ),
        seat_model_id=f'sam:{sha}',
        seat_model_sha256=sha,
    )


def build_occupancy_scenario(
    *,
    document_id: str,
    label: str,
    state: OccupancyState,
    created_at_utc: str,
    occupied_seat_ids: Sequence[str] = (),
    unoccupied_seat_ids: Sequence[str] = (),
    zone_block: SeatingZoneBlock | None = None,
    occupant_model: OccupantModelClass = 'unknown',
    **fields: Any,
) -> OccupancyScenario:
    probe = OccupancyScenario.model_construct(
        **_canon(
            OccupancyScenario,
            dict(
                occupancy_scenario_id='',
                document_id=document_id,
                label=label,
                state=state,
                occupied_seat_ids=tuple(occupied_seat_ids),
                unoccupied_seat_ids=tuple(unoccupied_seat_ids),
                zone_block=zone_block,
                occupant_model=occupant_model,
                created_at_utc=created_at_utc,
                occupancy_sha256='',
                comparability_key='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    key = _digest(probe.comparability_payload())
    return OccupancyScenario(
        **probe.model_dump(
            exclude={
                'occupancy_scenario_id',
                'occupancy_sha256',
                'comparability_key',
            }
        ),
        occupancy_scenario_id=f'ocs:{sha}',
        occupancy_sha256=sha,
        comparability_key=key,
    )


def build_commissioning_result(
    *,
    document_id: str,
    measured_at_utc: str,
    **fields: Any,
) -> SeatingCommissioningResult:
    probe = SeatingCommissioningResult.model_construct(
        **_canon(
            SeatingCommissioningResult,
            dict(
                result_id='',
                document_id=document_id,
                measured_at_utc=measured_at_utc,
                result_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return SeatingCommissioningResult(
        **probe.model_dump(exclude={'result_id', 'result_sha256'}),
        result_id=f'scr:{sha}',
        result_sha256=sha,
    )


def evaluate_occupancy_comparability(
    first: OccupancyScenario,
    second: OccupancyScenario,
) -> OccupancyComparability:
    """Two measurements/predictions are comparable only when their
    occupancy-affecting content matches exactly. An ``unknown`` state on
    either side reports ``unknown`` — never assumed compatible."""
    if first.state == 'unknown' or second.state == 'unknown':
        return 'unknown'
    if first.comparability_key == second.comparability_key:
        return 'compatible'
    return 'incompatible'


def evaluate_commissioning_verdict(
    *,
    seat_models: Sequence[SeatAcousticModel],
    measured_ear_position: Position3 | None,
    design_ear_position: Position3 | None,
    ear_position_tolerance_m: float = 0.10,
    occupancy_matches: bool | None,
    clearance_failure_reasons: Sequence[OcclusionFailureReason] = (),
    document_id: str,
    measured_at_utc: str,
) -> SeatingCommissioningResult:
    """Combine as-built evidence into a commissioning verdict with
    enumerable failure reasons — 'the seats sound wrong' becomes a list
    of specific geometric/occupancy causes."""
    reasons: list[OcclusionFailureReason] = list(clearance_failure_reasons)
    if not seat_models or any(
        m.geometric_model_state == 'unknown' for m in seat_models
    ):
        reasons.append('seating_acoustic_model_unknown')
    if occupancy_matches is False:
        reasons.append('occupancy_state_mismatch')
    deviation: float | None = None
    if (
        measured_ear_position is not None
        and design_ear_position is not None
    ):
        dz = measured_ear_position.z_m - design_ear_position.z_m
        dx = measured_ear_position.x_m - design_ear_position.x_m
        dy = measured_ear_position.y_m - design_ear_position.y_m
        deviation = (dx * dx + dy * dy + dz * dz) ** 0.5
        if deviation > ear_position_tolerance_m:
            reasons.append('ear_height_outside_design_range')
    elif design_ear_position is not None:
        reasons.append('ear_height_outside_design_range')
    unique_reasons = tuple(
        dict.fromkeys(r for r in reasons if r != 'none')
    )
    if unique_reasons:
        verdict: Literal['matches_design', 'deviated', 'unknown']
        verdict = 'deviated'
    elif seat_models:
        verdict = 'matches_design'
    else:
        verdict = 'unknown'
    return build_commissioning_result(
        document_id=document_id,
        measured_at_utc=measured_at_utc,
        seat_model_refs=tuple(
            (m.seat_model_id, m.seat_model_sha256) for m in seat_models
        ),
        measured_ear_position=measured_ear_position,
        design_ear_position=design_ear_position,
        ear_position_deviation_m=deviation,
        failure_reasons=unique_reasons,
        verdict=verdict,
    )


__all__ = [
    'CLEARANCE_STATUS_LABELS',
    'OCCLUDER_KIND_LABELS',
    'OCCLUSION_FAILURE_LABELS',
    'OCCUPANCY_COMPARABILITY_LABELS',
    'OCCUPANCY_STATE_LABELS',
    'SEATING_ACOUSTICS_AUTHORITY_VERSION',
    'SEATING_ACOUSTICS_SCHEMA_VERSION',
    'SEAT_ACOUSTIC_EVIDENCE_LABELS',
    'SEAT_GEOMETRY_SOURCE_LABELS',
    'ClearancePathResult',
    'ClearanceStatus',
    'DirectSoundClearanceEvaluation',
    'ListenerEarAuthority',
    'OccluderKind',
    'OcclusionFailureReason',
    'OccupancyComparability',
    'OccupancyScenario',
    'OccupancyState',
    'OccupantModelClass',
    'PathOccluder',
    'SeatAcousticEvidence',
    'SeatAcousticEvidenceClass',
    'SeatAcousticModel',
    'SeatGeometryEvidence',
    'SeatGeometrySource',
    'SeatingCommissioningResult',
    'SeatingZoneBlock',
    'SpeakerBandSensitivity',
    'UpholsteryClass',
    'build_clearance_evaluation',
    'build_commissioning_result',
    'build_occupancy_scenario',
    'build_seat_model',
    'evaluate_clearance_path',
    'evaluate_commissioning_verdict',
    'evaluate_occupancy_comparability',
]
