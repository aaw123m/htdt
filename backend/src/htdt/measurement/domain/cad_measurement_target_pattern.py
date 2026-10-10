"""Reproducible multi-point microphone target patterns (#543).

A MeasurementTargetPattern pins one exact anchor (seat listener reference,
existing measurement point, or explicit world point) inside one exact
SceneRevision, plus a deterministic, immutable offset list declared in an
explicit coordinate frame. Materializing a pattern produces exact
``measurement_point`` SceneEntity targets that carry their lineage
(pattern identity, anchor revision, offset index) — historical points are
never retranslated when the anchor later moves; a moved anchor requires an
explicit rebase producing a new pattern version.

Discrete patterns are never moving-microphone trajectories: an MMM method
would need a separate trajectory/time authority rather than pretending a
continuous sweep is a set of exact point measurements.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_scene import (
    Position3,
    Quaternion4,
    SceneEntity,
    acoustic_reference_position,
    quaternion_to_matrix3,

)

from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload


PatternAnchorKind = Literal['seat', 'measurement_point', 'explicit_point']
PatternOffsetFrame = Literal['world', 'anchor_local']
PatternPointPurpose = Literal['measurement', 'calibration', 'holdout', 'diagnostic']
TARGET_PATTERN_SCHEMA_VERSION = 'measurement-target-pattern-1'






def _position_payload(position: Position3) -> dict[str, float]:
    return {'x_m': position.x_m, 'y_m': position.y_m, 'z_m': position.z_m}


class TargetPatternOffset(BaseModel):
    """One deterministic pattern point in the declared offset frame."""

    model_config = ConfigDict(frozen=True)

    offset_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    offset_m: tuple[float, float, float]
    purpose: PatternPointPurpose = 'measurement'
    # Acceptance guidance radius for the physical microphone placement; it
    # is guidance authority, never proof the microphone was within it.
    placement_tolerance_m: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_offset(self) -> 'TargetPatternOffset':
        if len(self.offset_m) != 3 or any(not isfinite(float(v)) for v in self.offset_m):
            raise ValueError('pattern offsets require three finite metre values')
        return self


class MeasurementTargetPattern(BaseModel):
    """Immutable anchored, versioned spatial sampling pattern.

    ``anchor_*`` pins the exact anchor semantics inside the exact
    SceneRevision the pattern was authored against; ``offsets`` are the
    complete deterministic target list in ``offset_frame``. The
    ``supersedes_pattern_sha256`` field links an explicit rebase to the exact
    earlier pattern version — never a silent translation.
    """

    model_config = ConfigDict(frozen=True)

    pattern_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    anchor_revision_id: str = Field(min_length=1)
    anchor_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    anchor_kind: PatternAnchorKind
    anchor_entity_id: str | None = Field(default=None, min_length=1)
    anchor_position: Position3
    anchor_orientation: Quaternion4 | None = None
    offset_frame: PatternOffsetFrame = 'world'
    pattern_version: int = Field(ge=1)
    offsets: tuple[TargetPatternOffset, ...] = Field(min_length=1)
    supersedes_pattern_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    created_at: str = Field(min_length=1)
    pattern_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_pattern(self) -> 'MeasurementTargetPattern':
        indices = [offset.offset_index for offset in self.offsets]
        if len(indices) != len(set(indices)):
            raise ValueError('pattern offset indices must be unique')
        if indices != sorted(indices):
            raise ValueError('pattern offsets must be ordered by offset_index')
        if self.anchor_kind == 'explicit_point':
            if self.anchor_entity_id is not None:
                raise ValueError('explicit-point anchors carry no entity id')
            if self.offset_frame == 'anchor_local':
                raise ValueError('explicit-point anchors have no local frame')
        else:
            if self.anchor_entity_id is None:
                raise ValueError('entity anchors require anchor_entity_id')
        if self.pattern_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement target pattern hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': TARGET_PATTERN_SCHEMA_VERSION,
            'document_id': self.document_id,
            'anchor_revision_id': self.anchor_revision_id,
            'anchor_revision_content_hash': self.anchor_revision_content_hash,
            'anchor_kind': self.anchor_kind,
            'anchor_entity_id': self.anchor_entity_id,
            'anchor_position': _position_payload(self.anchor_position),
            'anchor_orientation': (
                None
                if self.anchor_orientation is None
                else {
                    'w': self.anchor_orientation.w,
                    'x': self.anchor_orientation.x,
                    'y': self.anchor_orientation.y,
                    'z': self.anchor_orientation.z,
                }
            ),
            'offset_frame': self.offset_frame,
            'pattern_version': self.pattern_version,
            'offsets': [
                {
                    'offset_index': offset.offset_index,
                    'label': offset.label,
                    'offset_m': list(offset.offset_m),
                    'purpose': offset.purpose,
                    'placement_tolerance_m': offset.placement_tolerance_m,
                }
                for offset in self.offsets
            ],
            'supersedes_pattern_sha256': self.supersedes_pattern_sha256,
        }


class MaterializedPatternPoint(BaseModel):
    """Lineage record for one exact pattern-generated measurement target.

    The resolved world position is materialized once and frozen here: the
    record always reports the position/anchor revision it was created from,
    so historical evidence never moves when the anchor later moves.
    """

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    pattern_id: str = Field(min_length=1)
    pattern_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    pattern_version: int = Field(ge=1)
    document_id: str = Field(min_length=1)
    created_in_revision_id: str = Field(min_length=1)
    anchor_kind: PatternAnchorKind
    anchor_entity_id: str | None = None
    anchor_revision_id: str = Field(min_length=1)
    offset_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    offset_m: tuple[float, float, float]
    position: Position3
    purpose: PatternPointPurpose = 'measurement'
    placement_tolerance_m: float | None = None
    measurement_point_entity_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    point_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_point(self) -> 'MaterializedPatternPoint':
        if self.point_sha256 != _hash(self.identity_payload()):
            raise ValueError('materialized pattern point hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': TARGET_PATTERN_SCHEMA_VERSION,
            'pattern_id': self.pattern_id,
            'pattern_sha256': self.pattern_sha256,
            'pattern_version': self.pattern_version,
            'document_id': self.document_id,
            'created_in_revision_id': self.created_in_revision_id,
            'anchor_kind': self.anchor_kind,
            'anchor_entity_id': self.anchor_entity_id,
            'anchor_revision_id': self.anchor_revision_id,
            'offset_index': self.offset_index,
            'label': self.label,
            'offset_m': list(self.offset_m),
            'position': _position_payload(self.position),
            'purpose': self.purpose,
            'placement_tolerance_m': self.placement_tolerance_m,
            'measurement_point_entity_id': self.measurement_point_entity_id,
        }


