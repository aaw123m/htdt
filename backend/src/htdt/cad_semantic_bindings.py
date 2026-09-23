"""Scene entity extensibility boundary (Issue #641).

``SceneEntity.kind`` is a closed enum, and the old answer to a new object
type was to grow the enum and pile fields onto the base record. This module
provides the boundary instead: entities carry typed *semantic capability
bindings* (``semantic_bindings`` on ``SceneEntity``), where each binding
names a registered capability and carries parameters validated against that
capability's typed schema.

- ``CAPABILITY_REGISTRY`` maps a capability id to its parameter schema (a
  frozen pydantic model). Bind a capability via
  :func:`validate_capability_binding` — unknown capabilities and malformed
  parameters fail closed at authoring time, never at read time.
- :func:`default_semantic_bindings` derives the implicit binding set for the
  legacy closed kinds, so a speaker keeps meaning "acoustic source" without
  growing a new enum member, and consumers can migrate to capability reads.
- :func:`entity_capabilities` resolves the effective capability set of an
  entity: explicit bindings merged over the kind-derived defaults.
- Extension entities that do not fit any legacy ``kind`` still need a
  ``kind`` value for the closed schema — the registry boundary keeps that
  field honest: ``kind='furniture'`` carries generic physical extent while
  the capabilities record the actual semantics (e.g. ``equipment_rack``).
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .cad_scene import SceneEntity, SemanticCapabilityBinding


CAPABILITY_BINDINGS_VERSION: Literal['1'] = '1'


class UnknownCapabilityError(ValueError):
    """A binding referenced a capability the registry does not define."""


class _EmptyParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class AcousticSourceParameters(BaseModel):
    """Speaker-style acoustic source binding."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    channel_role: str | None = None
    reference_offset_m: tuple[float, float, float] | None = None


class ListenerParameters(BaseModel):
    """Seat-style acoustic listener binding."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    ear_height_m: float | None = Field(default=None, gt=0)


class DisplaySurfaceParameters(BaseModel):
    """Screen/projection display-surface binding."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    gains_aimed: bool = False


class ProjectedImageParameters(BaseModel):
    """Projector binding: emits onto a display surface."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    throw_direction: Literal['front', 'rear'] = 'front'


class MountableParameters(BaseModel):
    """Entity can be mounted on/supported by another entity (Issue #661)."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    max_payload_kg: float | None = Field(default=None, gt=0)
    mounting_pattern: str | None = None  # e.g. 'vesa-100', 'keyhole', 'rack-u'


class SupportSurfaceParameters(BaseModel):
    """Entity offers a horizontal/supporting surface children may attach to."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    surface_height_m: float | None = Field(default=None, ge=0)
    max_load_kg: float | None = Field(default=None, gt=0)
    rack_units: int | None = Field(default=None, ge=1)


class OperationalMotionParameters(BaseModel):
    """Entity sweeps a clearance volume in normal operation (Issue #651)."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    motion_kind: Literal['door_swing', 'recline', 'service_slide', 'rotate'] | None = None


class EnclosedVolumeParameters(BaseModel):
    """Entity encloses a separate air volume (cabinet, enclosure)."""

    model_config = ConfigDict(frozen=True, extra='forbid')
    vented: bool = False


CAPABILITY_REGISTRY: Mapping[str, type[BaseModel]] = {
    'acoustic_source': AcousticSourceParameters,
    'listener': ListenerParameters,
    'display_surface': DisplaySurfaceParameters,
    'projected_image': ProjectedImageParameters,
    'mountable': MountableParameters,
    'support_surface': SupportSurfaceParameters,
    'operational_motion': OperationalMotionParameters,
    'enclosed_volume': EnclosedVolumeParameters,
    'obstruction': _EmptyParameters,
}


def capability_names() -> frozenset[str]:
    return frozenset(CAPABILITY_REGISTRY)


def validate_capability_binding(
    binding: SemanticCapabilityBinding,
) -> SemanticCapabilityBinding:
    """Validate a binding against the registered capability schema.

    Unknown capability ids and parameter shapes fail closed here — at write
    time — so persisted scenes only ever contain registry-conformant
    bindings.
    """

    schema = CAPABILITY_REGISTRY.get(binding.capability)
    if schema is None:
        raise UnknownCapabilityError(
            f'unknown semantic capability: {binding.capability}'
        )
    try:
        validated = schema.model_validate(dict(binding.parameters))
    except ValidationError as exc:
        raise ValueError(
            f'invalid parameters for capability {binding.capability}: {exc}'
        ) from exc
    return SemanticCapabilityBinding(
        capability=binding.capability,
        parameters=validated.model_dump(mode='python', exclude_none=True),
    )


_KIND_DEFAULT_BINDINGS: Mapping[str, tuple[tuple[str, dict[str, Any]], ...]] = {
    'speaker': (('acoustic_source', {}), ('obstruction', {}), ('mountable', {})),
    'seat': (('listener', {}), ('obstruction', {})),
    'screen': (('display_surface', {}), ('mountable', {}), ('obstruction', {})),
    'projector': (('projected_image', {}), ('mountable', {}), ('obstruction', {})),
    'riser': (('support_surface', {}), ('obstruction', {})),
    'furniture': (('obstruction', {}),),
    'av_equipment': (('mountable', {}), ('operational_motion', {}), ('obstruction', {})),
    'measurement_point': (('listener', {}),),
}


def default_semantic_bindings(
    entity: SceneEntity,
) -> tuple[SemanticCapabilityBinding, ...]:
    """Implicit capability set derived from the entity's legacy closed kind.

    These defaults keep kind-typed entities fully described under the
    capability model without requiring persisted bindings — an explicit
    binding on the entity overrides the same capability's default.
    """

    return tuple(
        SemanticCapabilityBinding(capability=name, parameters=dict(params))
        for name, params in _KIND_DEFAULT_BINDINGS.get(entity.kind, ())
    )


def entity_capabilities(entity: SceneEntity) -> frozenset[str]:
    """Effective capability set: explicit bindings merged over kind defaults."""

    capabilities = {
        binding.capability for binding in default_semantic_bindings(entity)
    }
    for binding in entity.semantic_bindings or ():
        capabilities.add(binding.capability)
    return frozenset(capabilities)


def entity_semantic_bindings(
    entity: SceneEntity,
) -> tuple[SemanticCapabilityBinding, ...]:
    """Effective bindings (defaults merged with explicit overrides)."""

    merged: dict[str, SemanticCapabilityBinding] = {
        binding.capability: binding for binding in default_semantic_bindings(entity)
    }
    for binding in entity.semantic_bindings or ():
        merged[binding.capability] = validate_capability_binding(binding)
    return tuple(merged[name] for name in sorted(merged))


def bind_capabilities(
    entity: SceneEntity,
    bindings: tuple[SemanticCapabilityBinding, ...] | list[SemanticCapabilityBinding],
) -> SceneEntity:
    """Return the entity with validated bindings replacing its explicit set."""

    validated = [validate_capability_binding(binding) for binding in bindings]
    return entity.model_copy(
        update={'semantic_bindings': tuple(validated) or None}
    )


def entity_has_capability(entity: SceneEntity, capability: str) -> bool:
    return capability in entity_capabilities(entity)
