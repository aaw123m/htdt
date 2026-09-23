from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WallSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    wall_id: str = Field(min_length=1)
    from_vertex_id: str = Field(min_length=1)
    to_vertex_id: str = Field(min_length=1)
    thickness_m: float = Field(default=0.10, gt=0.0)
    source_wall_id: str | None = None

    @model_validator(mode='after')
    def distinct_endpoints(self) -> 'WallSegment':
        if self.from_vertex_id == self.to_vertex_id:
            raise ValueError('wall endpoints must be distinct')
        if not isfinite(float(self.thickness_m)):
            raise ValueError('wall thickness must be finite')
        return self


class WallOpening(BaseModel):
    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    wall_id: str = Field(min_length=1)
    offset_m: float = Field(ge=0.0)
    width_m: float = Field(gt=0.0)
    sill_m: float = Field(default=0.0, ge=0.0)
    height_m: float = Field(gt=0.0)
    kind: Literal['door', 'window', 'passage', 'other'] = 'door'
    is_open: bool = False

    @model_validator(mode='after')
    def finite_dimensions(self) -> 'WallOpening':
        values = (self.offset_m, self.width_m, self.sill_m, self.height_m)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('opening dimensions must be finite')
        return self


class WallConstraintBinding(BaseModel):
    """N30b wall-ID binding only; solver semantics remain outside the CAD topology."""

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    wall_ids: tuple[str, ...] = Field(min_length=1)
    kind: Literal['clearance'] = 'clearance'
    clearance_m: float = Field(ge=0.0)

    @model_validator(mode='after')
    def valid_binding(self) -> 'WallConstraintBinding':
        if len(self.wall_ids) != len(set(self.wall_ids)):
            raise ValueError('constraint binding wall ids must be unique')
        if not isfinite(float(self.clearance_m)):
            raise ValueError('constraint clearance must be finite')
        return self


class WallTopology(BaseModel):
    model_config = ConfigDict(frozen=True)

    walls: tuple[WallSegment, ...]
    openings: tuple[WallOpening, ...] = ()
    constraint_bindings: tuple[WallConstraintBinding, ...] = ()

    @model_validator(mode='after')
    def unique_ids_and_references(self) -> 'WallTopology':
        wall_ids = [wall.wall_id for wall in self.walls]
        if len(wall_ids) != len(set(wall_ids)):
            raise ValueError('wall ids must be unique')
        opening_ids = [opening.opening_id for opening in self.openings]
        if len(opening_ids) != len(set(opening_ids)):
            raise ValueError('opening ids must be unique')
        binding_ids = [binding.binding_id for binding in self.constraint_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError('constraint binding ids must be unique')
        known_walls = set(wall_ids)
        dangling_openings = [opening.opening_id for opening in self.openings if opening.wall_id not in known_walls]
        if dangling_openings:
            raise ValueError(f'openings reference unknown walls: {dangling_openings}')
        dangling_bindings = [
            binding.binding_id
            for binding in self.constraint_bindings
            if any(wall_id not in known_walls for wall_id in binding.wall_ids)
        ]
        if dangling_bindings:
            raise ValueError(f'constraint bindings reference unknown walls: {dangling_bindings}')
        return self
