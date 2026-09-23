"""Persisted models for physical attachment and construction assembly.

These live outside ``cad_scene`` (like ``cad_wall_models``) so the schema
module can import them without import cycles; they deliberately avoid
importing cad_scene types.
"""

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --- Physical attachment (Issue #661) ----------------------------------------

AttachmentKind = Literal[
    'stand_on',      # child rests on a parent's horizontal surface
    'mounted_to',    # child rigidly mounts to a parent face/structural point
    'racked_in',     # child occupies rack units inside a rack parent
    'placed_inside', # child placed on an interior shelf/compartment
    'stacked_on',    # child stacked directly atop the parent's envelope
]

AttachmentAnchor = Literal[
    'top_surface',   # parent's horizontal top plane
    'front_face',
    'rear_face',
    'left_face',
    'right_face',
    'interior',      # inside the parent's volume (racks/cabinets)
]


class EntityAttachment(BaseModel):
    """One child-relative placement edge in the attachment graph.

    The child entity's ``position`` remains its world anchor for rendering,
    but its authoritative placement is derived: ``child_position =
    parent_surface(anchor) + child_anchor_offset_m``. Attachments form a
    forest — the acyclic invariant is enforced by
    ``physical_attachment.validate_attachment_graph``.
    """

    model_config = ConfigDict(frozen=True)
    attachment_id: str = Field(min_length=1)
    child_entity_id: str = Field(min_length=1)
    parent_entity_id: str = Field(min_length=1)
    kind: AttachmentKind
    parent_anchor: AttachmentAnchor
    # Offset applied in the parent's local frame at the anchor point.
    child_anchor_offset_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # For racked_in: the rack-U index (1-based) the child occupies.
    rack_unit: int | None = Field(default=None, ge=1)
    label: str | None = None

    @model_validator(mode='after')
    def valid_attachment(self) -> 'EntityAttachment':
        if self.child_entity_id == self.parent_entity_id:
            raise ValueError('an entity cannot attach to itself')
        if self.kind == 'racked_in' and self.rack_unit is None:
            raise ValueError('racked_in attachments require rack_unit')
        if self.kind != 'racked_in' and self.rack_unit is not None:
            raise ValueError('rack_unit is only valid for racked_in attachments')
        if self.kind in ('stand_on', 'stacked_on') and self.parent_anchor != 'top_surface':
            raise ValueError('stand_on/stacked_on attach to the parent top surface')
        if self.kind in ('racked_in', 'placed_inside') and self.parent_anchor != 'interior':
            raise ValueError('interior attachments use the interior anchor')
        if any(not isfinite(float(c)) for c in self.child_anchor_offset_m):
            raise ValueError('attachment offset must be finite')
        return self


# --- Construction assembly (Issue #657) --------------------------------------

AssemblyElementKind = Literal['wall', 'floor', 'ceiling', 'soffit', 'riser']

# Ordered outer→inner for walls/ceilings, bottom→top for floors/risers.
AssemblyLayerKind = Literal[
    'finish', 'substrate', 'vapor_barrier', 'insulation', 'cavity', 'framing'
]

SubstrateMaterial = Literal[
    'drywall', 'plywood', 'osb', 'masonry', 'concrete', 'metal_stud',
    'wood_stud', 'glass', 'acoustic_panel', 'unknown',
]


class AssemblyLayer(BaseModel):
    """One physical layer of a construction assembly."""

    model_config = ConfigDict(frozen=True)
    layer_id: str = Field(min_length=1)
    kind: AssemblyLayerKind
    material: SubstrateMaterial = 'unknown'
    material_name: str | None = None
    thickness_m: float = Field(gt=0)
    density_kg_m3: float | None = Field(default=None, gt=0)
    # Evidence provenance: what the layer claim is based on.
    evidence_source: Literal['capture', 'operator', 'specification', 'assumed'] = 'assumed'
    # Optional acoustic surface data when the layer is the room-facing finish.
    absorption_coefficients: tuple[float, ...] | None = None

    @model_validator(mode='after')
    def valid_layer(self) -> 'AssemblyLayer':
        if not isfinite(float(self.thickness_m)):
            raise ValueError('layer thickness must be finite')
        if self.absorption_coefficients is not None:
            if not self.absorption_coefficients:
                raise ValueError('absorption coefficients must be non-empty when present')
            if any(
                not (0.0 <= float(c) <= 1.0) for c in self.absorption_coefficients
            ):
                raise ValueError('absorption coefficients must be within [0, 1]')
        return self


class ConstructionAssembly(BaseModel):
    """Unified physical-layer stack for one room element (Issue #657).

    A wall/floor/ceiling is one ordered stack of physical layers — finish,
    substrate, cavity, framing — shared across Capture observations,
    acoustic isolation and installation feasibility, instead of three
    parallel partial descriptions.
    """

    model_config = ConfigDict(frozen=True)
    assembly_id: str = Field(min_length=1)
    element: AssemblyElementKind
    # Binds to a wall_id (walls) or the region/element name for
    # floor/ceiling/soffit/riser.
    element_ref: str = Field(min_length=1)
    layers: tuple[AssemblyLayer, ...] = Field(min_length=1)
    air_gap_m: float | None = Field(default=None, ge=0)
    evidence_source: Literal['capture', 'operator', 'specification', 'assumed'] = 'assumed'
    notes: str | None = None

    @model_validator(mode='after')
    def valid_assembly(self) -> 'ConstructionAssembly':
        layer_ids = [layer.layer_id for layer in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError('assembly layer ids must be unique')
        kinds = [layer.kind for layer in self.layers]
        if kinds.count('finish') > 1 or kinds.count('substrate') > 1:
            raise ValueError('an assembly has at most one finish and one substrate layer')
        if self.element == 'wall' and not self.element_ref:
            raise ValueError('wall assemblies must bind a wall element')
        return self

    def layer_of_kind(self, kind: AssemblyLayerKind) -> AssemblyLayer | None:
        for layer in self.layers:
            if layer.kind == kind:
                return layer
        return None

    @property
    def total_thickness_m(self) -> float:
        return sum(float(layer.thickness_m) for layer in self.layers)

    @property
    def cavity_depth_m(self) -> float | None:
        cavity = self.layer_of_kind('cavity')
        return float(cavity.thickness_m) if cavity is not None else None
