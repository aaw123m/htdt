from __future__ import annotations

from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CadConstraintPoint2D(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_m: float
    y_m: float

    @model_validator(mode='after')
    def finite_coordinates(self) -> 'CadConstraintPoint2D':
        if not all(isfinite(float(value)) for value in (self.x_m, self.y_m)):
            raise ValueError('constraint region coordinates must be finite')
        return self


class CadAllowedRegionConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    kind: Literal['allowed_region'] = 'allowed_region'
    name: str = Field(min_length=1)
    entity_ids: tuple[str, ...] = Field(min_length=1)
    vertices: tuple[CadConstraintPoint2D, ...] = Field(min_length=3)

    @model_validator(mode='after')
    def unique_entities(self) -> 'CadAllowedRegionConstraint':
        if len(self.entity_ids) != len(set(self.entity_ids)):
            raise ValueError('constraint entity ids must be unique')
        return self


class CadExclusionRegionConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    kind: Literal['exclusion_region'] = 'exclusion_region'
    name: str = Field(min_length=1)
    entity_ids: tuple[str, ...] = Field(min_length=1)
    vertices: tuple[CadConstraintPoint2D, ...] = Field(min_length=3)
    region_role: Literal['exclusion', 'walkway'] = 'exclusion'

    @model_validator(mode='after')
    def unique_entities(self) -> 'CadExclusionRegionConstraint':
        if len(self.entity_ids) != len(set(self.entity_ids)):
            raise ValueError('constraint entity ids must be unique')
        return self


class CadWallClearanceConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    kind: Literal['wall_clearance'] = 'wall_clearance'
    name: str = Field(min_length=1)
    entity_ids: tuple[str, ...] = Field(min_length=1)
    wall_id: str = Field(min_length=1)
    min_m: float | None = Field(default=None, ge=0.0)
    max_m: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def valid_range(self) -> 'CadWallClearanceConstraint':
        if len(self.entity_ids) != len(set(self.entity_ids)):
            raise ValueError('constraint entity ids must be unique')
        if self.min_m is None and self.max_m is None:
            raise ValueError('wall clearance requires min_m or max_m')
        if self.min_m is not None and self.max_m is not None and self.max_m < self.min_m:
            raise ValueError('wall clearance max_m must be >= min_m')
        return self


class CadPairDistanceConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    kind: Literal['pair_distance'] = 'pair_distance'
    name: str = Field(min_length=1)
    entity_a: str = Field(min_length=1)
    entity_b: str = Field(min_length=1)
    min_m: float | None = Field(default=None, ge=0.0)
    max_m: float | None = Field(default=None, ge=0.0)
    distance_mode: Literal['3d', 'horizontal_xy'] = 'horizontal_xy'
    distance_reference: Literal['center', 'envelope_clearance'] = 'envelope_clearance'

    @model_validator(mode='after')
    def valid_pair(self) -> 'CadPairDistanceConstraint':
        if self.entity_a == self.entity_b:
            raise ValueError('pair distance requires two different entities')
        if self.min_m is None and self.max_m is None:
            raise ValueError('pair distance requires min_m or max_m')
        if self.min_m is not None and self.max_m is not None and self.max_m < self.min_m:
            raise ValueError('pair distance max_m must be >= min_m')
        if self.distance_reference == 'envelope_clearance' and self.distance_mode != 'horizontal_xy':
            raise ValueError('envelope clearance requires horizontal_xy distance mode')
        return self


CadPlacementConstraint = Annotated[
    CadAllowedRegionConstraint
    | CadExclusionRegionConstraint
    | CadWallClearanceConstraint
    | CadPairDistanceConstraint,
    Field(discriminator='kind'),
]


class CadConstraintSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    document_id: str = Field(min_length=1)
    constraints: tuple[CadPlacementConstraint, ...] = ()

    @model_validator(mode='after')
    def unique_constraint_ids(self) -> 'CadConstraintSet':
        ids = [item.constraint_id for item in self.constraints]
        if len(ids) != len(set(ids)):
            raise ValueError('constraint ids must be unique')
        return self

    def constraint(self, constraint_id: str) -> CadPlacementConstraint:
        for item in self.constraints:
            if item.constraint_id == constraint_id:
                return item
        raise KeyError(constraint_id)


class CadConstraintResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    constraint_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str
    entity_ids: tuple[str, ...]
    wall_id: str | None = None
    region_role: Literal['allowed', 'exclusion', 'walkway'] | None = None
    passed: bool
    reason_code: str = Field(min_length=1)
    reason_ja: str = Field(min_length=1)
    actual_m: float | None = None
    required_min_m: float | None = None
    required_max_m: float | None = None
    raw_actual: dict[str, Any] = Field(default_factory=dict)
    raw_required: dict[str, Any] = Field(default_factory=dict)


class CadConstraintEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraints_satisfied: bool
    results: tuple[CadConstraintResult, ...]

    @property
    def violations(self) -> tuple[CadConstraintResult, ...]:
        return tuple(item for item in self.results if not item.passed)
