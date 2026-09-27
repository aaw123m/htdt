"""Resolution-aware viewing envelope evaluation (#1039).

For each seat: viewing distance, horizontal/vertical angular image size,
and pixels-per-degree against the *delivered* raster and the exact active
presentation aperture — under a versioned policy, never a hard-coded
"ideal viewing angle".

Authorities:

- :class:`ViewingResolutionPolicy` — versioned per-criterion bounds
  (min pixels/degree, max/min angles, distance window). A criterion the
  policy does not constrain reports ``NOT_APPLICABLE``; no universal
  threshold is invented.
- :class:`ViewingResolutionEvaluation` — per-seat results computed from
  the active aperture, seat eye reference and the delivered raster.

Honesty rules:

- Raster kind is explicit (``source_content`` / ``negotiated_transport``
  / ``renderer_output`` / ``display_native`` / ``unknown``): pixels per
  degree is computed against the *delivered* raster only when
  ``raster_semantics_exact`` is set — unknown scaling semantics still
  report physical geometry but leave the interpretation ``UNKNOWN``.
- Results are per-seat and independent; no hidden overall score.
- Projection apertures and direct-view panel apertures share this
  contract — the evaluator only sees aperture geometry + raster.
"""

from __future__ import annotations

from math import acos, degrees, sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_scene import Direction3, Position3
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash




RasterKind = Literal[
    'source_content',
    'negotiated_transport',
    'renderer_output',
    'display_native',
    'unknown',
]


class ViewingResolutionPolicy(BaseModel):
    """Versioned evaluation policy — all bounds optional, none invented."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['viewing-resolution-policy-1'] = (
        'viewing-resolution-policy-1'
    )
    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str | None = None
    min_horizontal_pixels_per_degree: float | None = Field(
        default=None, gt=0.0
    )
    min_vertical_pixels_per_degree: float | None = Field(
        default=None, gt=0.0
    )
    min_horizontal_angle_deg: float | None = Field(default=None, gt=0.0)
    max_horizontal_angle_deg: float | None = Field(default=None, gt=0.0)
    min_viewing_distance_m: float | None = Field(default=None, gt=0.0)
    max_viewing_distance_m: float | None = Field(default=None, gt=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    policy_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'policy_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ViewingResolutionPolicy':
        expected = _hash(self.semantic_payload())
        if expected != self.policy_sha256:
            raise ValueError('viewing resolution policy hash mismatch')
        return self


class ViewingSeatBinding(BaseModel):
    """Seat entity + eye reference for one evaluated seat."""

    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    eye_position: Position3


class SeatViewingCriterion(BaseModel):
    """Per-criterion result for one seat."""

    model_config = ConfigDict(frozen=True)

    criterion: str
    status: EvaluationStatus
    measured: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    note: str | None = None


class SeatViewingResult(BaseModel):
    """Physical quantities + policy status for one seat (#1039 §7)."""

    model_config = ConfigDict(frozen=True)

    seat_entity_id: str
    viewing_distance_m: float
    horizontal_angle_deg: float
    vertical_angle_deg: float
    horizontal_pixels_per_degree: float | None = None
    vertical_pixels_per_degree: float | None = None
    status: EvaluationStatus
    criteria: tuple[SeatViewingCriterion, ...] = ()
    unknown_reasons: tuple[str, ...] = ()


class ViewingResolutionEvaluation(BaseModel):
    """Evaluation of one aperture × raster × policy over seats."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['viewing-resolution-evaluation-1'] = (
        'viewing-resolution-evaluation-1'
    )
    evaluation_id: str = Field(min_length=1)
    surface_entity_id: str | None = None
    aperture_center: Position3
    aperture_width_m: float = Field(gt=0.0)
    aperture_height_m: float = Field(gt=0.0)
    aperture_normal: Direction3
    presentation_mode: str | None = None
    raster_width_px: int | None = Field(default=None, gt=0)
    raster_height_px: int | None = Field(default=None, gt=0)
    raster_kind: RasterKind = 'unknown'
    raster_semantics_exact: bool = False
    raster_provenance: str | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    policy_sha256: str | None = None
    seat_results: tuple[SeatViewingResult, ...] = ()
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ViewingResolutionEvaluation':
        triple = (self.policy_id, self.policy_version, self.policy_sha256)
        if (None in triple) and any(v is not None for v in triple):
            raise ValueError(
                'policy id/version/sha256 must be supplied together or '
                'not at all'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.evaluation_sha256:
            raise ValueError('viewing resolution evaluation hash mismatch')
        return self


def _angle_deg(eye: tuple[float, float, float],
               a: tuple[float, float, float],
               b: tuple[float, float, float]) -> float:
    v1 = tuple(a[i] - eye[i] for i in range(3))
    v2 = tuple(b[i] - eye[i] for i in range(3))
    d = sum(v1[i] * v2[i] for i in range(3))
    n1 = sqrt(sum(x * x for x in v1))
    n2 = sqrt(sum(x * x for x in v2))
    if n1 <= 0.0 or n2 <= 0.0:
        return 0.0
    return degrees(acos(max(-1.0, min(1.0, d / (n1 * n2)))))


def _eval_seat(
    seat: ViewingSeatBinding,
    *,
    aperture_center: tuple[float, float, float],
    aperture_normal: tuple[float, float, float],
    aperture_right: tuple[float, float, float],
    aperture_width_m: float,
    aperture_height_m: float,
    raster_width_px: int | None,
    raster_height_px: int | None,
    raster_exact: bool,
    policy: ViewingResolutionPolicy | None,
) -> SeatViewingResult:
    eye = (
        float(seat.eye_position.x_m),
        float(seat.eye_position.y_m),
        float(seat.eye_position.z_m),
    )
    # distance along aperture normal (planar distance to image plane)
    rel = tuple(eye[i] - aperture_center[i] for i in range(3))
    distance = sqrt(sum(x * x for x in rel))
    half_w = aperture_width_m / 2.0
    half_h = aperture_height_m / 2.0
    right = aperture_right
    up = (
        aperture_normal[1] * right[2] - aperture_normal[2] * right[1],
        aperture_normal[2] * right[0] - aperture_normal[0] * right[2],
        aperture_normal[0] * right[1] - aperture_normal[1] * right[0],
    )
    left_pt = tuple(
        aperture_center[i] - right[i] * half_w for i in range(3)
    )
    right_pt = tuple(
        aperture_center[i] + right[i] * half_w for i in range(3)
    )
    top_pt = tuple(
        aperture_center[i] + up[i] * half_h for i in range(3)
    )
    bottom_pt = tuple(
        aperture_center[i] - up[i] * half_h for i in range(3)
    )
    h_angle = _angle_deg(eye, left_pt, right_pt)
    v_angle = _angle_deg(eye, top_pt, bottom_pt)

    unknowns: list[str] = []
    h_ppd: float | None = None
    v_ppd: float | None = None
    if not raster_exact:
        unknowns.append(
            'raster/scaling semantics not exact — physical geometry only'
        )
    elif raster_width_px is None or raster_height_px is None:
        unknowns.append('delivered raster unbound')
    elif h_angle <= 0.0 or v_angle <= 0.0:
        unknowns.append('degenerate viewing angle')
    else:
        h_ppd = raster_width_px / h_angle
        v_ppd = raster_height_px / v_angle

    criteria: list[SeatViewingCriterion] = []

    def _min_crit(name, value, minimum):
        if policy is None or minimum is None:
            return SeatViewingCriterion(
                criterion=name,
                status='NOT_APPLICABLE',
                measured=value,
                note='policy does not constrain this criterion',
            )
        if value is None:
            return SeatViewingCriterion(
                criterion=name,
                status='UNKNOWN',
                minimum=minimum,
                note='quantity could not be computed',
            )
        return SeatViewingCriterion(
            criterion=name,
            status='PASS' if value >= minimum else 'FAIL',
            measured=value,
            minimum=minimum,
        )

    def _range_crit(name, value, minimum, maximum):
        if policy is None or (minimum is None and maximum is None):
            return SeatViewingCriterion(
                criterion=name,
                status='NOT_APPLICABLE',
                measured=value,
                note='policy does not constrain this criterion',
            )
        if value is None:
            return SeatViewingCriterion(
                criterion=name, status='UNKNOWN', minimum=minimum,
                maximum=maximum,
            )
        if minimum is not None and value < minimum:
            status: EvaluationStatus = 'FAIL'
        elif maximum is not None and value > maximum:
            status = 'FAIL'
        else:
            status = 'PASS'
        return SeatViewingCriterion(
            criterion=name, status=status, measured=value,
            minimum=minimum, maximum=maximum,
        )

    p = policy
    criteria.append(
        _min_crit(
            'horizontal_pixels_per_degree',
            h_ppd,
            p.min_horizontal_pixels_per_degree if p else None,
        )
    )
    criteria.append(
        _min_crit(
            'vertical_pixels_per_degree',
            v_ppd,
            p.min_vertical_pixels_per_degree if p else None,
        )
    )
    criteria.append(
        _range_crit(
            'horizontal_angle',
            h_angle,
            p.min_horizontal_angle_deg if p else None,
            p.max_horizontal_angle_deg if p else None,
        )
    )
    criteria.append(
        _range_crit(
            'viewing_distance',
            distance,
            p.min_viewing_distance_m if p else None,
            p.max_viewing_distance_m if p else None,
        )
    )
    status = _combine_status(tuple(c.status for c in criteria))
    return SeatViewingResult(
        seat_entity_id=seat.seat_entity_id,
        viewing_distance_m=distance,
        horizontal_angle_deg=h_angle,
        vertical_angle_deg=v_angle,
        horizontal_pixels_per_degree=h_ppd,
        vertical_pixels_per_degree=v_ppd,
        status=status,
        criteria=tuple(criteria),
        unknown_reasons=tuple(unknowns),
    )


def evaluate_viewing_resolution(
    *,
    evaluation_id: str,
    aperture_center: Position3,
    aperture_width_m: float,
    aperture_height_m: float,
    aperture_normal: Direction3,
    aperture_right: Direction3 | None = None,
    seats: tuple[ViewingSeatBinding, ...],
    surface_entity_id: str | None = None,
    presentation_mode: str | None = None,
    raster_width_px: int | None = None,
    raster_height_px: int | None = None,
    raster_kind: RasterKind = 'unknown',
    raster_semantics_exact: bool = False,
    raster_provenance: str | None = None,
    policy: ViewingResolutionPolicy | None = None,
) -> ViewingResolutionEvaluation:
    """Evaluate one aperture × delivered raster × policy over seats."""
    normal = (
        float(aperture_normal.x), float(aperture_normal.y),
        float(aperture_normal.z),
    )
    if aperture_right is not None:
        right = (
            float(aperture_right.x), float(aperture_right.y),
            float(aperture_right.z),
        )
    else:
        # default right = normal × world-up fallback (frontal aperture)
        up_world = (0.0, 1.0, 0.0)
        right = (
            normal[1] * up_world[2] - normal[2] * up_world[1],
            normal[2] * up_world[0] - normal[0] * up_world[2],
            normal[0] * up_world[1] - normal[1] * up_world[0],
        )
        norm = sqrt(sum(x * x for x in right))
        if norm <= 1e-9:
            right = (1.0, 0.0, 0.0)
        else:
            right = tuple(x / norm for x in right)
    center = (
        float(aperture_center.x_m), float(aperture_center.y_m),
        float(aperture_center.z_m),
    )
    results = tuple(
        _eval_seat(
            seat,
            aperture_center=center,
            aperture_normal=normal,
            aperture_right=right,
            aperture_width_m=aperture_width_m,
            aperture_height_m=aperture_height_m,
            raster_width_px=raster_width_px,
            raster_height_px=raster_height_px,
            raster_exact=raster_semantics_exact,
            policy=policy,
        )
        for seat in seats
    )
    probe = ViewingResolutionEvaluation.model_construct(
        evaluation_id='',
        surface_entity_id=surface_entity_id,
        aperture_center=aperture_center,
        aperture_width_m=aperture_width_m,
        aperture_height_m=aperture_height_m,
        aperture_normal=aperture_normal,
        presentation_mode=presentation_mode,
        raster_width_px=raster_width_px,
        raster_height_px=raster_height_px,
        raster_kind=raster_kind,
        raster_semantics_exact=raster_semantics_exact,
        raster_provenance=raster_provenance,
        policy_id=policy.policy_id if policy else None,
        policy_version=policy.version if policy else None,
        policy_sha256=policy.policy_sha256 if policy else None,
        seat_results=results,
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return ViewingResolutionEvaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='vre-' + digest[:24],
        evaluation_sha256=digest,
    )


def build_viewing_resolution_policy(**kwargs) -> ViewingResolutionPolicy:
    probe = ViewingResolutionPolicy.model_construct(
        policy_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return ViewingResolutionPolicy(
        **probe.model_dump(
            mode='python', exclude={'policy_sha256', 'schema_version'}
        ),
        policy_sha256=digest,
    )
