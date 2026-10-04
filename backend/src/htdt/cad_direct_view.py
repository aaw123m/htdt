"""Direct-view display authority (#637).

HTDT's canonical video geometry contract is projection-specific:
``VideoGeometryRequest`` requires a projector entity plus an exact
``ProjectorSpecification`` and a passive ``screen`` entity. A TV /
OLED / MiniLED / MicroLED / direct-view display is an *active* image
device — it is not a passive screen, not a projector, and not a fake
zero-throw projector. This module adds the first-class ``display``
scene kind (``cad_scene.PHYSICAL_ENTITY_KINDS``) plus the typed
specification and viewing-geometry authority for direct-view systems:

- ``DirectViewDisplaySpecification`` — immutable/versioned equipment
  authority (manufacturer/model, chassis vs active-image dimensions,
  mounting, video + photometric capability) with evidence provenance.
  Composes with #569 installed instances by id/version/hash — serial
  numbers never live in the reusable model definition.
- ``DisplayGeometryBinding`` — the active image aperture inside the
  chassis: visible width/height, image-center offset, frame/bezel
  clearance, mounting context. The visible image area is distinct from
  the chassis body; the entity's local frame (X right, Y normal, Z up)
  is identical to the screen convention, so tilted wall mounts consume
  the exact display-plane orientation.
- ``DirectViewGeometryRequest`` / ``DirectViewGeometryEvaluation`` —
  per-seat viewing angles, sightlines, riser interactions and
  installation collision/clearance, reusing the projector-independent
  surface math of ``cad_video_geometry``. Projection-only concerns
  (throw ratio, lens shift, optical axis) return ``NOT_APPLICABLE`` by
  type — never a guessed ``UNKNOWN``.

Capture -> HTDT semantic preservation is explicit:
``CAPTURE_ENTITY_TYPE_TO_SCENE_KIND`` maps Capture ``display`` ->
HTDT ``display``, ``projection_screen`` -> ``screen``, ``projector`` ->
``projector``. ``display`` is never silently downgraded to ``screen``.
"""

from __future__ import annotations

from math import sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_scene import Offset3, Position3, SceneDocument, SceneEntity, Size3
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_repository import SceneRevision
from .cad_scene import scene_content_hash
from .cad_video_geometry import (
    CollisionResult,
    EvaluationStatus,
    SeatGeometryBinding,
    SeatSightlineResult,
    SeatViewingResult,
    RiserInteractionResult,
    VideoGeometryPolicy,
    VideoGeometryTarget,
    _collision_payload,
    _collision_results,
    _combine_status,
    _digest,
    _finite,
    _plane_frame,
    _position,
    _riser_results,
    _seat_identity_payload,
    _sightline_results,
    _viewing_result,
)
from .canonical_json import canonicalize_payload


DIRECT_VIEW_SPEC_SCHEMA_VERSION = 1
DIRECT_VIEW_SPEC_AUTHORITY_VERSION = 'direct-view-display-spec-1'
DIRECT_VIEW_REQUEST_SCHEMA_VERSION = 1
DIRECT_VIEW_REQUEST_AUTHORITY_VERSION = 'direct-view-geometry-1'

DirectViewDisplayClass = Literal[
    'lcd',
    'oled',
    'miniled',
    'microled',
    'other',
    'unknown',
]
DisplayMountingKind = Literal[
    'wall',
    'stand',
    'furniture',
    'recessed',
    'custom',
    'unknown',
]

#: Deterministic Capture entity-type -> HTDT scene-kind promotion map
#: (issue #637 §13). ``display`` maps to ``display``, never to ``screen``;
#: ``projection_screen`` maps to the passive ``screen`` kind.
#: ``listening_position`` is a seat location in scene terms; ``custom`` and
#: ``acoustic_treatment`` are captured physical objects — ``furniture`` is
#: the honest generic physical kind (never equipment). ``reference_point``
#: is a non-physical anchor — the ``measurement_point`` kind. Types absent
#: from this map have no HTDT scene kind and stay suggestion-only.
CAPTURE_ENTITY_TYPE_TO_SCENE_KIND: dict[str, str] = {
    'speaker': 'speaker',
    'subwoofer': 'speaker',
    'display': 'display',
    'projection_screen': 'screen',
    'projector': 'projector',
    'listening_position': 'seat',
    'seat': 'seat',
    'acoustic_treatment': 'furniture',
    'custom': 'furniture',
    'equipment_rack': 'av_equipment',
    'reference_point': 'measurement_point',
    'measurement_point': 'measurement_point',
}


def capture_entity_type_to_scene_kind(entity_type: str) -> str | None:
    """Map a Capture entity type to its HTDT scene kind, or ``None``.

    ``None`` means the type has no scene-enterable mapping — the record
    stays evidence/suggestion-only and is never downgraded to a generic
    kind silently.
    """

    return CAPTURE_ENTITY_TYPE_TO_SCENE_KIND.get(entity_type)


class DisplayVideoCapability(BaseModel):
    """Typed video/signal endpoint capability of a direct-view display.

    Consumed by #570 signal-path evaluation; every field is optional —
    absent data stays ``None`` (UNKNOWN), never a guessed value.
    """

    model_config = ConfigDict(frozen=True)

    native_width_px: int | None = Field(default=None, gt=0)
    native_height_px: int | None = Field(default=None, gt=0)
    max_refresh_hz: float | None = Field(default=None, gt=0)
    hdr_format_families: tuple[str, ...] = ()
    earc_supported: bool | None = None
    hdmi_input_count: int | None = Field(default=None, ge=0)

    @field_validator('max_refresh_hz')
    @classmethod
    def finite_refresh(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value)


class DisplayPhotometricCapability(BaseModel):
    """Declared display photometric/HDR capability consumed by #557.

    Declared (manufacturer or user) values only — measured values live
    in photometric evidence records, never in the specification.
    """

    model_config = ConfigDict(frozen=True)

    peak_luminance_cd_m2: float | None = Field(default=None, gt=0)
    full_field_luminance_cd_m2: float | None = Field(default=None, gt=0)
    black_level_cd_m2: float | None = Field(default=None, ge=0)
    contrast_ratio: float | None = Field(default=None, gt=0)
    tone_mapping: Literal['none', 'static', 'dynamic', 'unknown'] = 'unknown'

    @field_validator(
        'peak_luminance_cd_m2',
        'full_field_luminance_cd_m2',
        'black_level_cd_m2',
        'contrast_ratio',
    )
    @classmethod
    def finite_metric(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value)


class DirectViewDisplaySpecification(BaseModel):
    """Immutable/versioned reference authority for one direct-view display model.

    Model-level identity only: serial numbers and installed-unit evidence
    belong to the InstalledEquipmentInstance (#569) layer. Evidence
    provenance reuses ``EquipmentDataProvenance``; a manufacturer-claimed
    spec requires manufacturer-sourced provenance.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DIRECT_VIEW_SPEC_SCHEMA_VERSION
    authority_version: Literal[
        'direct-view-display-spec-1'
    ] = DIRECT_VIEW_SPEC_AUTHORITY_VERSION
    specification_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)
    display_class: DirectViewDisplayClass = 'unknown'
    chassis_size_m: Size3
    active_image_width_m: float = Field(gt=0)
    active_image_height_m: float = Field(gt=0)
    active_image_center_offset_m: Offset3 = Field(default_factory=Offset3)
    supported_mountings: tuple[DisplayMountingKind, ...] = ()
    video_capability: DisplayVideoCapability = DisplayVideoCapability()
    photometric_capability: DisplayPhotometricCapability = DisplayPhotometricCapability()
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    specification_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('active_image_width_m', 'active_image_height_m')
    @classmethod
    def finite_dimension(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def valid_specification(self) -> 'DirectViewDisplaySpecification':
        if self.active_image_width_m > self.chassis_size_m.x_m + 1e-9:
            raise ValueError('active image width must fit inside chassis width')
        if self.active_image_height_m > self.chassis_size_m.z_m + 1e-9:
            raise ValueError('active image height must fit inside chassis height')
        if self.manufacturer is not None or self.model is not None:
            if self.manufacturer is None or self.model is None:
                raise ValueError('manufacturer and model must be supplied together')
            if not any(
                item.evidence_kind == 'manufacturer' for item in self.provenance
            ):
                raise ValueError(
                    'manufacturer display data requires manufacturer provenance'
                )
        elif self.user_label is None:
            raise ValueError(
                'user-defined display specification requires user_label'
            )
        provenance_keys = [
            (
                item.evidence_kind,
                item.source_name,
                item.source_version,
                item.source_reference,
                item.source_sha256,
            )
            for item in self.provenance
        ]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('display specification provenance records must be unique')
        if self.specification_sha256 != _digest(self.semantic_payload()):
            raise ValueError('DirectViewDisplaySpecification semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'specification_id': self.specification_id,
            'version': self.version,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'user_label': self.user_label,
            'display_class': self.display_class,
            'chassis_size_m': self.chassis_size_m.model_dump(mode='json'),
            'active_image_width_m': self.active_image_width_m,
            'active_image_height_m': self.active_image_height_m,
            'active_image_center_offset_m': (
                self.active_image_center_offset_m.model_dump(mode='json')
            ),
            'supported_mountings': list(self.supported_mountings),
            'video_capability': self.video_capability.model_dump(mode='json'),
            'photometric_capability': (
                self.photometric_capability.model_dump(mode='json')
            ),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }


def build_direct_view_display_specification(
    *,
    specification_id: str,
    version: str,
    manufacturer: str | None,
    model: str | None,
    user_label: str | None,
    display_class: DirectViewDisplayClass,
    chassis_size_m: Size3,
    active_image_width_m: float,
    active_image_height_m: float,
    active_image_center_offset_m: Offset3 | None = None,
    supported_mountings: Sequence[DisplayMountingKind] = (),
    video_capability: DisplayVideoCapability | None = None,
    photometric_capability: DisplayPhotometricCapability | None = None,
    provenance: Sequence[EquipmentDataProvenance],
) -> DirectViewDisplaySpecification:
    """Build a self-hashed display specification with exact provenance."""
    if not provenance:
        raise ValueError('display specification requires at least one provenance record')
    center_offset = (
        Offset3() if active_image_center_offset_m is None
        else active_image_center_offset_m
    )
    mountings = tuple(supported_mountings)
    video = (
        DisplayVideoCapability() if video_capability is None else video_capability
    )
    photometric = (
        DisplayPhotometricCapability()
        if photometric_capability is None
        else photometric_capability
    )
    probe = DirectViewDisplaySpecification.model_construct(**canonicalize_payload(DirectViewDisplaySpecification, dict(
        specification_id=specification_id,
        version=version,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        display_class=display_class,
        chassis_size_m=chassis_size_m,
        active_image_width_m=active_image_width_m,
        active_image_height_m=active_image_height_m,
        active_image_center_offset_m=center_offset,
        supported_mountings=mountings,
        video_capability=video,
        photometric_capability=photometric,
        provenance=tuple(provenance),
        specification_sha256='0' * 64,
    )))
    return DirectViewDisplaySpecification(
        specification_id=specification_id,
        version=version,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        display_class=display_class,
        chassis_size_m=chassis_size_m,
        active_image_width_m=active_image_width_m,
        active_image_height_m=active_image_height_m,
        active_image_center_offset_m=center_offset,
        supported_mountings=mountings,
        video_capability=video,
        photometric_capability=photometric,
        provenance=tuple(provenance),
        specification_sha256=_digest(probe.semantic_payload()),
    )


class DisplayGeometryBinding(BaseModel):
    """Active image aperture inside a physical ``display`` entity.

    Same axis convention as ``ScreenGeometryBinding``: the entity's local
    X axis is image right, Y the display normal, Z image up. A direct-view
    display is never acoustically transparent — that field does not exist
    here by design (issue #637 §16).
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    visible_width_m: float = Field(gt=0)
    visible_height_m: float = Field(gt=0)
    image_center_offset_local_m: Offset3 = Field(default_factory=Offset3)
    frame_clearance_m: float = Field(ge=0)
    mounting: DisplayMountingKind = 'unknown'

    @field_validator('visible_width_m', 'visible_height_m', 'frame_clearance_m')
    @classmethod
    def finite_metric(cls, value: float) -> float:
        return _finite(value)


class DirectViewGeometryRequest(BaseModel):
    """One exact direct-view viewing-geometry request.

    The display-specification pin (id + version + hash triple) is
    optional: viewing geometry does not depend on a spec being bound,
    but spec-derived conformance checks only run when it is present.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DIRECT_VIEW_REQUEST_SCHEMA_VERSION
    authority_version: Literal[
        'direct-view-geometry-1'
    ] = DIRECT_VIEW_REQUEST_AUTHORITY_VERSION
    display: DisplayGeometryBinding
    display_specification_id: str | None = Field(default=None, min_length=1)
    display_specification_version: str | None = Field(default=None, min_length=1)
    display_specification_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    seats: tuple[SeatGeometryBinding, ...]
    policy: VideoGeometryPolicy
    collision_entity_ids: tuple[str, ...]
    request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('seats')
    @classmethod
    def canonical_seats(
        cls,
        values: tuple[SeatGeometryBinding, ...],
    ) -> tuple[SeatGeometryBinding, ...]:
        return tuple(sorted(values, key=lambda item: item.entity_id))

    @field_validator('collision_entity_ids')
    @classmethod
    def canonical_collision_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    @model_validator(mode='after')
    def valid_request(self) -> 'DirectViewGeometryRequest':
        seat_ids = [item.entity_id for item in self.seats]
        if len(seat_ids) != len(set(seat_ids)):
            raise ValueError('seat geometry bindings must be unique')
        if len(self.collision_entity_ids) != len(set(self.collision_entity_ids)):
            raise ValueError('collision entity ids must be unique')
        spec_fields = (
            self.display_specification_id,
            self.display_specification_version,
            self.display_specification_sha256,
        )
        if any(item is None for item in spec_fields) != all(
            item is None for item in spec_fields
        ):
            raise ValueError(
                'display specification id/version/hash must be supplied together'
            )
        if self.request_sha256 != _digest(self.identity_payload()):
            raise ValueError('DirectViewGeometryRequest semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'display': self.display.model_dump(mode='json'),
            'display_specification_id': self.display_specification_id,
            'display_specification_version': self.display_specification_version,
            'display_specification_sha256': self.display_specification_sha256,
            'seats': [_seat_identity_payload(item) for item in self.seats],
            'policy': self.policy.model_dump(mode='json'),
            'collision_entity_ids': list(self.collision_entity_ids),
        }


def build_direct_view_geometry_request(
    *,
    display: DisplayGeometryBinding,
    seats: Sequence[SeatGeometryBinding],
    policy: VideoGeometryPolicy,
    collision_entity_ids: Sequence[str] = (),
    display_specification: DirectViewDisplaySpecification | None = None,
) -> DirectViewGeometryRequest:
    """Build a self-hashed direct-view request; the spec pin is optional."""
    ordered_seats = tuple(sorted(tuple(seats), key=lambda item: item.entity_id))
    ordered_collision_ids = tuple(sorted(tuple(collision_entity_ids)))
    spec_id = spec_version = spec_hash = None
    if display_specification is not None:
        spec_id = display_specification.specification_id
        spec_version = display_specification.version
        spec_hash = display_specification.specification_sha256
    probe = DirectViewGeometryRequest.model_construct(
        display=display,
        display_specification_id=spec_id,
        display_specification_version=spec_version,
        display_specification_sha256=spec_hash,
        seats=ordered_seats,
        policy=policy,
        collision_entity_ids=ordered_collision_ids,
        request_sha256='0' * 64,
    )
    return DirectViewGeometryRequest(
        display=display,
        display_specification_id=spec_id,
        display_specification_version=spec_version,
        display_specification_sha256=spec_hash,
        seats=ordered_seats,
        policy=policy,
        collision_entity_ids=ordered_collision_ids,
        request_sha256=_digest(probe.identity_payload()),
    )


class DirectViewSurfaceResult(BaseModel):
    """Resolved active image surface in world space."""

    model_config = ConfigDict(frozen=True)

    status: EvaluationStatus
    image_center: Position3
    image_plane_corners: tuple[Position3, Position3, Position3, Position3]
    visible_width_m: float
    visible_height_m: float
    diagonal_m: float
    aspect_ratio: float
    chassis_containment_status: EvaluationStatus
    chassis_containment_reason: str = Field(min_length=1)
    spec_conformance_status: EvaluationStatus
    spec_conformance_reason: str = Field(min_length=1)


class DirectViewGeometryEvaluation(BaseModel):
    """Immutable direct-view geometry result; projection fields are absent
    by construction — direct view has no throw/lens semantics to report."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DIRECT_VIEW_REQUEST_SCHEMA_VERSION
    authority_version: Literal[
        'direct-view-geometry-1'
    ] = DIRECT_VIEW_REQUEST_AUTHORITY_VERSION
    evaluation_id: str = Field(min_length=1)
    target: VideoGeometryTarget
    request: DirectViewGeometryRequest
    display_specification_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    surface: DirectViewSurfaceResult
    projection_status: Literal['NOT_APPLICABLE'] = 'NOT_APPLICABLE'
    projection_status_reason: str = Field(
        default='direct-view display; no projection geometry applies',
        min_length=1,
    )
    viewing: tuple[SeatViewingResult, ...]
    sightlines: tuple[SeatSightlineResult, ...]
    risers: tuple[RiserInteractionResult, ...]
    collisions: tuple[CollisionResult, ...]
    geometry_status: EvaluationStatus
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'DirectViewGeometryEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('DirectViewGeometryEvaluation semantic hash mismatch')
        if self.evaluation_id != 'dvge-' + digest[:24]:
            raise ValueError('DirectViewGeometryEvaluation deterministic id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'target': self.target.model_dump(mode='json'),
            'request': self.request.model_dump(mode='json'),
            'display_specification_sha256': self.display_specification_sha256,
            'surface': self.surface.model_dump(mode='json'),
            'projection_status': self.projection_status,
            'projection_status_reason': self.projection_status_reason,
            'viewing': [item.model_dump(mode='json') for item in self.viewing],
            'sightlines': [item.model_dump(mode='json') for item in self.sightlines],
            'risers': [item.model_dump(mode='json') for item in self.risers],
            'collisions': [_collision_payload(item) for item in self.collisions],
            'geometry_status': self.geometry_status,
        }


def _display_frame(
    display_entity: SceneEntity,
    binding: DisplayGeometryBinding,
):
    if display_entity.kind != 'display':
        raise ValueError('display binding must reference a display SceneEntity')
    return _plane_frame(
        display_entity,
        width_m=binding.visible_width_m,
        height_m=binding.visible_height_m,
        center_offset_local_m=binding.image_center_offset_local_m,
    )


def _chassis_containment(
    display_entity: SceneEntity,
    binding: DisplayGeometryBinding,
) -> tuple[EvaluationStatus, str]:
    """Active image must fit inside the chassis envelope (local frame)."""
    assert display_entity.size_m is not None
    offset = binding.image_center_offset_local_m
    within_width = (
        abs(offset.x_m) + binding.visible_width_m * 0.5
        <= display_entity.size_m.x_m * 0.5 + 1e-6
    )
    within_height = (
        abs(offset.z_m) + binding.visible_height_m * 0.5
        <= display_entity.size_m.z_m * 0.5 + 1e-6
    )
    within_depth = abs(offset.y_m) <= display_entity.size_m.y_m * 0.5 + 1e-6
    if within_width and within_height and within_depth:
        return 'PASS', 'active image area is inside the chassis envelope'
    return 'FAIL', 'active image area exceeds the chassis envelope'


def _spec_conformance(
    specification: DirectViewDisplaySpecification | None,
    request: DirectViewGeometryRequest,
) -> tuple[EvaluationStatus, str]:
    """Check the authored aperture against the bound display specification.

    Absent specification -> UNKNOWN (no declared dims to check against).
    Mismatched active dimensions or unsupported mounting -> FAIL.
    """
    binding = request.display
    if specification is None:
        return 'UNKNOWN', 'no display specification bound to the request'
    if (
        abs(binding.visible_width_m - specification.active_image_width_m) > 1e-6
        or abs(binding.visible_height_m - specification.active_image_height_m)
        > 1e-6
        or abs(binding.image_center_offset_local_m.x_m
               - specification.active_image_center_offset_m.x_m) > 1e-6
        or abs(binding.image_center_offset_local_m.y_m
               - specification.active_image_center_offset_m.y_m) > 1e-6
        or abs(binding.image_center_offset_local_m.z_m
               - specification.active_image_center_offset_m.z_m) > 1e-6
    ):
        return 'FAIL', 'bound aperture differs from specification active image'
    if (
        binding.mounting != 'unknown'
        and specification.supported_mountings
        and binding.mounting not in specification.supported_mountings
    ):
        return 'FAIL', 'declared mounting kind is not supported by the specification'
    return 'PASS', 'bound aperture matches the specification active image'


def _validate_scene_bindings(
    *,
    scene: SceneDocument,
    request: DirectViewGeometryRequest,
) -> None:
    display = scene.entity(request.display.entity_id)
    if display.kind != 'display':
        raise ValueError('display binding must reference a display entity')
    for binding in request.seats:
        if scene.entity(binding.entity_id).kind != 'seat':
            raise ValueError('seat geometry binding must reference a seat entity')
        if binding.riser_entity_id is not None:
            if scene.entity(binding.riser_entity_id).kind != 'riser':
                raise ValueError('riser_entity_id must reference a riser entity')
    for entity_id in request.collision_entity_ids:
        scene.entity(entity_id)


def evaluate_direct_view_geometry(
    *,
    baseline: SceneRevision,
    variant: SystemVariant | None,
    display_specification: DirectViewDisplaySpecification | None = None,
    request: DirectViewGeometryRequest,
) -> DirectViewGeometryEvaluation:
    """Evaluate one SceneRevision/SystemVariant for a direct-view display.

    Shares the surface-neutral viewing/sightline/riser/collision math
    with projection evaluation; no projector or screen entity is needed.
    """
    if display_specification is None:
        if request.display_specification_id is not None:
            raise ValueError('request pins a display specification that was not supplied')
    else:
        if (
            request.display_specification_id
            != display_specification.specification_id
            or request.display_specification_version != display_specification.version
            or request.display_specification_sha256
            != display_specification.specification_sha256
        ):
            raise ValueError('request display specification binding mismatch')

    if variant is None:
        scene = baseline.document
        target = VideoGeometryTarget(
            document_id=baseline.document_id,
            scene_revision_id=baseline.revision_id,
            scene_content_hash=baseline.content_hash,
            system_variant_id=None,
            system_variant_sha256=None,
            evaluated_scene_content_hash=baseline.content_hash,
        )
    else:
        scene = materialize_system_variant(baseline, variant)
        target = VideoGeometryTarget(
            document_id=baseline.document_id,
            scene_revision_id=baseline.revision_id,
            scene_content_hash=baseline.content_hash,
            system_variant_id=variant.variant_id,
            system_variant_sha256=variant.variant_sha256,
            evaluated_scene_content_hash=scene_content_hash(scene),
        )

    _validate_scene_bindings(scene=scene, request=request)
    display_entity = scene.entity(request.display.entity_id)
    center, right, up, _normal, corners = _display_frame(
        display_entity, request.display
    )
    containment_status, containment_reason = _chassis_containment(
        display_entity, request.display
    )
    conformance_status, conformance_reason = _spec_conformance(
        display_specification, request
    )
    visible_w = request.display.visible_width_m
    visible_h = request.display.visible_height_m
    surface = DirectViewSurfaceResult(
        status=_combine_status((containment_status, conformance_status)),
        image_center=_position(center),
        image_plane_corners=corners,
        visible_width_m=visible_w,
        visible_height_m=visible_h,
        diagonal_m=sqrt(visible_w * visible_w + visible_h * visible_h),
        aspect_ratio=visible_w / visible_h,
        chassis_containment_status=containment_status,
        chassis_containment_reason=containment_reason,
        spec_conformance_status=conformance_status,
        spec_conformance_reason=conformance_reason,
    )
    viewing = tuple(
        _viewing_result(
            seat_entity=scene.entity(binding.entity_id),
            binding=binding,
            center=center,
            right=right,
            up=up,
            width_m=visible_w,
            height_m=visible_h,
            policy=request.policy,
        )
        for binding in request.seats
    )
    sightlines = _sightline_results(
        scene=scene,
        bindings=request.seats,
        center=center,
        right=right,
        up=up,
        width_m=visible_w,
        height_m=visible_h,
        policy=request.policy,
    )
    risers = _riser_results(
        scene=scene,
        bindings=request.seats,
        policy=request.policy,
    )
    collisions = _collision_results(
        scene=scene,
        collision_entity_ids=request.collision_entity_ids,
        surface_entity_id=request.display.entity_id,
        surface_frame_clearance_m=request.display.frame_clearance_m,
        policy=request.policy,
    )
    geometry_status = _combine_status((
        containment_status,
        *(
            status
            for item in viewing
            for status in (
                item.horizontal_status,
                item.vertical_status,
                item.center_elevation_status,
            )
        ),
        *(item.status for item in sightlines),
        *(item.status for item in risers),
        *(item.status for item in collisions),
    ))

    probe = DirectViewGeometryEvaluation.model_construct(
        target=target,
        request=request,
        display_specification_sha256=(
            None
            if display_specification is None
            else display_specification.specification_sha256
        ),
        surface=surface,
        viewing=viewing,
        sightlines=sightlines,
        risers=risers,
        collisions=collisions,
        geometry_status=geometry_status,
        evaluation_sha256='0' * 64,
        evaluation_id='pending',
    )
    digest = _digest(probe.identity_payload())
    return DirectViewGeometryEvaluation(
        evaluation_id='dvge-' + digest[:24],
        target=target,
        request=request,
        display_specification_sha256=probe.display_specification_sha256,
        surface=surface,
        viewing=viewing,
        sightlines=sightlines,
        risers=risers,
        collisions=collisions,
        geometry_status=geometry_status,
        evaluation_sha256=digest,
    )
