"""Measurement pose observation authority (#732).

Planned microphone targets (#543 patterns, #472 seat reference) describe
where a capsule SHOULD be placed; this module provides first-class
immutable evidence for where the capsule was actually OBSERVED to be,
with an explicit method and bounded spatial uncertainty. The three
authorities stay separate:

1. planned MeasurementPoint / target position
2. allowed placement tolerance / guidance
3. observed physical capsule pose + uncertainty

A planned target must never be silently promoted to an observed physical
position, and a user attestation within tolerance must never collapse into
a zero-uncertainty coordinate. Missing pose evidence remains UNKNOWN — it
gates only downstream claims that need an exact receiver coordinate.

Records are immutable, content-hashed and append-only in the shared
native store; a measurement binds its observation by exact identity, so a
historical measurement's pose evidence never moves when a seat moves or a
pattern is rebased.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_schema import connect_sqlite, ensure_native_schema, require_native_tables
from .cad_scene import Position3
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


MeasurementPoseMethod = Literal[
    'user_attested_within_tolerance',
    'manual_numeric_measurement',
    'tape_or_laser_from_datum',
    'fixed_jig',
    'capture_derived',
    'imported_survey',
    'unknown',
]

PoseUncertaintyKind = Literal[
    'per_axis_bounds',
    'radial_tolerance',
    'angular_bounds',
    'covariance',
    'unknown',
]

PoseDeltaClass = Literal['within_tolerance', 'outside_tolerance', 'unknown']

_OBSERVATION_PREFIX = 'measurement-pose:'
_DELTA_PREFIX = 'pose-delta:'

# Methods whose evidence is physical/survey-grade enough to carry an
# arbitrary covariance; attestation and capture-derived detections may be
# bounded but never zero-error, and 'unknown' carries no position at all.
_EXACT_CAPABLE_METHODS = frozenset(
    {
        'manual_numeric_measurement',
        'tape_or_laser_from_datum',
        'fixed_jig',
        'imported_survey',
    }
)


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class SpatialUncertainty(BaseModel):
    """Bounded, transparent spatial/angular uncertainty (#732).

    A simple placement radius is reported as a radial bound — never
    silently upgraded to a Gaussian covariance. ``bound_m`` collapses the
    positional component to a conservative scalar or ``None`` (UNKNOWN).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: PoseUncertaintyKind
    per_axis_bound_m: tuple[float, float, float] | None = None
    radial_bound_m: float | None = Field(default=None, ge=0.0)
    angular_bound_deg: float | None = Field(default=None, ge=0.0)
    # Row-major 3x3 positional covariance, only where the producer really
    # supplies one.
    covariance_m2: tuple[float, ...] | None = None

    @model_validator(mode='after')
    def valid_uncertainty(self) -> 'SpatialUncertainty':
        if self.kind == 'unknown':
            if any(
                v is not None
                for v in (
                    self.per_axis_bound_m,
                    self.radial_bound_m,
                    self.angular_bound_deg,
                    self.covariance_m2,
                )
            ):
                raise ValueError(
                    'unknown uncertainty must not carry numeric bounds'
                )
        elif self.kind == 'radial_tolerance':
            if self.radial_bound_m is None:
                raise ValueError('radial_tolerance requires radial_bound_m')
        elif self.kind == 'per_axis_bounds':
            if self.per_axis_bound_m is None or any(
                v < 0 for v in self.per_axis_bound_m
            ):
                raise ValueError(
                    'per_axis_bounds requires a non-negative 3-tuple'
                )
        elif self.kind == 'angular_bounds':
            if self.angular_bound_deg is None:
                raise ValueError(
                    'angular_bounds requires angular_bound_deg'
                )
        elif self.kind == 'covariance':
            cov = self.covariance_m2
            if cov is None or len(cov) != 9:
                raise ValueError(
                    'covariance requires a 3x3 row-major matrix'
                )
            # Symmetry + non-negative diagonal; no claim of PSD beyond.
            for i in range(3):
                if cov[3 * i + i] < 0:
                    raise ValueError('covariance diagonal must be >= 0')
                for j in range(3):
                    if abs(cov[3 * i + j] - cov[3 * j + i]) > 1e-9:
                        raise ValueError('covariance must be symmetric')
        return self

    def bound_m(self) -> float | None:
        """Conservative positional bound in metres, or None when unknown."""
        if self.kind == 'unknown':
            return None
        if self.kind == 'radial_tolerance':
            return self.radial_bound_m
        if self.kind == 'per_axis_bounds':
            assert self.per_axis_bound_m is not None
            return float(
                sum(v * v for v in self.per_axis_bound_m) ** 0.5
            )
        if self.kind == 'covariance':
            assert self.covariance_m2 is not None
            diag = (self.covariance_m2[0], self.covariance_m2[4], self.covariance_m2[8])
            return float(max(diag) ** 0.5)
        return None


UNKNOWN_UNCERTAINTY = SpatialUncertainty(kind='unknown')


class MeasurementPoseObservation(BaseModel):
    """Immutable evidence for where the capsule was actually placed (#732).

    ``observed_position_m`` may be ``None`` — UNKNOWN is a legal, honest
    pose. The record pins method, uncertainty, coordinate frame and
    provenance; it never merges into or overwrites the planned target.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.measurement-pose-observation'] = (
        'htdt.measurement-pose-observation'
    )
    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_ref: str | None = None
    planned_target_ref: str | None = None
    revision_id: str | None = None
    system_variant_id: str | None = None
    observed_position_m: Position3 | None = None
    observed_direction: Position3 | None = None
    coordinate_frame: str = 'scene_world'
    method: MeasurementPoseMethod
    uncertainty: SpatialUncertainty
    observed_at_utc: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_observation(self) -> 'MeasurementPoseObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if not self.observation_id.startswith(_OBSERVATION_PREFIX):
            raise ValueError(
                'observation id must use measurement-pose: prefix'
            )
        if self.method == 'unknown':
            if self.observed_position_m is not None:
                raise ValueError(
                    'method "unknown" cannot carry a position'
                )
            if self.observed_direction is not None:
                raise ValueError(
                    'method "unknown" cannot carry a direction'
                )
        if self.observed_position_m is None:
            if self.uncertainty.kind != 'unknown':
                raise ValueError(
                    'absent pose must declare unknown uncertainty'
                )
        else:
            if self.uncertainty.kind == 'unknown':
                raise ValueError(
                    'a concrete observed pose requires a bounded '
                    'uncertainty — never unquantified'
                )
            bound = self.uncertainty.bound_m()
            if bound is not None and bound <= 0.0:
                raise ValueError(
                    'an observed pose may not claim exact zero-error '
                    'placement — every method has a positive bound'
                )
            if self.method == 'user_attested_within_tolerance' and (
                self.uncertainty.kind not in
                {'radial_tolerance', 'per_axis_bounds', 'angular_bounds'}
            ):
                raise ValueError(
                    'attestation-within-tolerance must carry the declared '
                    'tolerance bound, not a survey covariance'
                )
        if (
            self.uncertainty.kind == 'covariance'
            and self.method not in _EXACT_CAPABLE_METHODS
        ):
            raise ValueError(
                'covariance requires a producer that actually measures it '
                f'(not {self.method})'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement pose semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'observation_id': self.observation_id,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'coordinate_frame': self.coordinate_frame,
            'method': self.method,
            'uncertainty': self.uncertainty.model_dump(mode='json'),
            'observed_at_utc': self.observed_at_utc,
            'evidence_refs': list(self.evidence_refs),
            'limitations': list(self.limitations),
        }
        for key, value in (
            ('measurement_ref', self.measurement_ref),
            ('planned_target_ref', self.planned_target_ref),
            ('revision_id', self.revision_id),
            ('system_variant_id', self.system_variant_id),
            ('observed_position_m', self.observed_position_m),
            ('observed_direction', self.observed_direction),
        ):
            if value is not None:
                payload[key] = (
                    value.model_dump(mode='json')
                    if isinstance(value, BaseModel)
                    else value
                )
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.observation_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_pose_observation(
    *,
    document_id: str,
    method: MeasurementPoseMethod,
    uncertainty: SpatialUncertainty = UNKNOWN_UNCERTAINTY,
    observed_position_m: Position3 | None = None,
    observed_direction: Position3 | None = None,
    measurement_ref: str | None = None,
    planned_target_ref: str | None = None,
    revision_id: str | None = None,
    system_variant_id: str | None = None,
    coordinate_frame: str = 'scene_world',
    evidence_refs: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    observation_id: str | None = None,
    authority_version: str = '1',
    observed_at_utc: str | None = None,
) -> MeasurementPoseObservation:
    """Assemble a sealed observation; missing pose stays UNKNOWN."""

    payload: dict[str, Any] = {
        'observation_id': observation_id
        or f'{_OBSERVATION_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'document_id': document_id,
        'measurement_ref': measurement_ref,
        'planned_target_ref': planned_target_ref,
        'revision_id': revision_id,
        'system_variant_id': system_variant_id,
        'observed_position_m': observed_position_m,
        'observed_direction': observed_direction,
        'coordinate_frame': coordinate_frame,
        'method': method,
        'uncertainty': uncertainty,
        'observed_at_utc': observed_at_utc or _utc_now(),
        'evidence_refs': evidence_refs,
        'limitations': limitations,
    }
    provisional = MeasurementPoseObservation.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return MeasurementPoseObservation.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class PlannedObservedPoseDelta(BaseModel):
    """Derived evidence: planned target vs. observed capsule pose (#732).

    A delta record is derived from both immutable authorities and mutates
    neither; ``classification`` is within/outside/unknown — UNKNOWN when
    either the observed pose or the tolerance bound is absent.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    delta_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    planned_target_ref: str | None = None
    planned_position_m: Position3 | None = None
    observed_position_m: Position3 | None = None
    delta_m: tuple[float, float, float] | None = None
    delta_norm_m: float | None = None
    tolerance_bound_m: float | None = Field(default=None, gt=0.0)
    classification: PoseDeltaClass
    detail: str = ''
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_delta(self) -> 'PlannedObservedPoseDelta':
        if not self.delta_id.startswith(_DELTA_PREFIX):
            raise ValueError('delta id must use pose-delta: prefix')
        if self.classification == 'unknown':
            if self.delta_m is not None or self.delta_norm_m is not None:
                raise ValueError(
                    'unknown delta must not carry a numeric difference'
                )
        else:
            if self.delta_m is None or self.delta_norm_m is None:
                raise ValueError(
                    'a classified delta requires numeric difference'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('pose delta semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'delta_id': self.delta_id,
            'observation_id': self.observation_id,
            'classification': self.classification,
            'detail': self.detail,
        }
        for key, value in (
            ('planned_target_ref', self.planned_target_ref),
            ('planned_position_m', self.planned_position_m),
            ('observed_position_m', self.observed_position_m),
            ('delta_m', self.delta_m),
            ('delta_norm_m', self.delta_norm_m),
            ('tolerance_bound_m', self.tolerance_bound_m),
        ):
            if value is not None:
                payload[key] = (
                    value.model_dump(mode='json')
                    if isinstance(value, BaseModel)
                    else value
                )
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.delta_id,
            authority_version='1',
            semantic_hash_sha256=self.semantic_sha256,
        )


def evaluate_pose_delta(
    observation: MeasurementPoseObservation,
    *,
    planned_position_m: Position3 | None,
    tolerance_bound_m: float | None = None,
    planned_target_ref: str | None = None,
    delta_id: str | None = None,
) -> PlannedObservedPoseDelta:
    """Derive the planned-vs-observed delta without mutating either side.

    'unknown' covers: no observed pose, no planned position, or no
    tolerance bound — never force a pass/fail from missing evidence.
    """

    if (
        observation.observed_position_m is None
        or planned_position_m is None
        or tolerance_bound_m is None
    ):
        return _make_delta(
            observation,
            classification='unknown',
            planned_position_m=planned_position_m,
            tolerance_bound_m=tolerance_bound_m,
            planned_target_ref=planned_target_ref
            or observation.planned_target_ref,
            detail=(
                'observed pose, planned position or tolerance is missing'
            ),
            delta_id=delta_id,
        )
    observed = observation.observed_position_m
    delta = (
        observed.x_m - planned_position_m.x_m,
        observed.y_m - planned_position_m.y_m,
        observed.z_m - planned_position_m.z_m,
    )
    norm = float(sum(d * d for d in delta) ** 0.5)
    classification: PoseDeltaClass = (
        'within_tolerance' if norm <= tolerance_bound_m else 'outside_tolerance'
    )
    return _make_delta(
        observation,
        classification=classification,
        planned_position_m=planned_position_m,
        tolerance_bound_m=tolerance_bound_m,
        planned_target_ref=planned_target_ref
        or observation.planned_target_ref,
        delta_m=delta,
        delta_norm_m=norm,
        delta_id=delta_id,
    )


def _make_delta(
    observation: MeasurementPoseObservation,
    *,
    classification: PoseDeltaClass,
    planned_position_m: Position3 | None,
    tolerance_bound_m: float | None,
    planned_target_ref: str | None,
    detail: str = '',
    delta_m: tuple[float, float, float] | None = None,
    delta_norm_m: float | None = None,
    delta_id: str | None = None,
) -> PlannedObservedPoseDelta:
    payload: dict[str, Any] = {
        'delta_id': delta_id or f'{_DELTA_PREFIX}{uuid4()}',
        'observation_id': observation.observation_id,
        'planned_target_ref': planned_target_ref,
        'planned_position_m': planned_position_m,
        'observed_position_m': observation.observed_position_m,
        'delta_m': delta_m,
        'delta_norm_m': delta_norm_m,
        'tolerance_bound_m': tolerance_bound_m,
        'classification': classification,
        'detail': detail,
    }
    provisional = PlannedObservedPoseDelta.model_construct(**canonicalize_payload(PlannedObservedPoseDelta, dict(
        **payload, semantic_sha256='0' * 64
    )))
    return PlannedObservedPoseDelta.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class MeasurementPoseBinding(BaseModel):
    """Resolution-time view binding a measurement to its pose evidence."""

    model_config = ConfigDict(frozen=True)

    measurement_ref: str
    planned_target_ref: str | None = None
    planned_position_m: Position3 | None = None
    observation: MeasurementPoseObservation | None = None
    delta: PlannedObservedPoseDelta | None = None

    @property
    def pose_state(self) -> str:
        """Campaign-UX state label (#732 acceptance): machine-friendly."""
        if self.observation is None:
            return 'not_recorded'
        if self.observation.observed_position_m is None:
            return 'not_recorded'
        if self.delta is None or self.delta.classification == 'unknown':
            if self.observation.method == 'user_attested_within_tolerance':
                return 'confirmed_within_tolerance'
            return 'measured_offset'
        if self.delta.classification == 'within_tolerance':
            return 'confirmed_within_tolerance'
        return 'measured_offset'


class MeasurementPoseObservationRepository:
    """Append-only pose-observation store on the shared cad.sqlite3 DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_measurement_pose_observations',
                'cad_planned_observed_deltas',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_observation(
        self, observation: MeasurementPoseObservation
    ) -> MeasurementPoseObservation:
        """Append an observation; re-saving an identical row is a no-op,
        a conflicting row under the same id is a hard failure."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_measurement_pose_observations('
                'observation_id, document_id, measurement_ref, '
                'planned_target_ref, method, observed_at_utc, '
                'semantic_sha256, payload_json) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(observation_id) DO NOTHING',
                (
                    observation.observation_id,
                    observation.document_id,
                    observation.measurement_ref,
                    observation.planned_target_ref,
                    observation.method,
                    observation.observed_at_utc,
                    observation.semantic_sha256,
                    observation.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT semantic_sha256, payload_json FROM '
                'cad_measurement_pose_observations WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone()
            if row['payload_json'] != observation.model_dump_json():
                raise ValueError(
                    f'pose observation {observation.observation_id} '
                    'already persisted with different content — '
                    'observations are immutable'
                )
        return observation

    def get_observation(
        self, observation_id: str
    ) -> MeasurementPoseObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return MeasurementPoseObservation.model_validate_json(
            row['payload_json']
        )

    def list_observations_for_measurement(
        self, measurement_ref: str
    ) -> tuple[MeasurementPoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE measurement_ref=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (measurement_ref,),
            ).fetchall()
        return tuple(
            MeasurementPoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def list_observations_for_document(
        self, document_id: str
    ) -> tuple[MeasurementPoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE document_id=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MeasurementPoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def save_delta(
        self, delta: PlannedObservedPoseDelta
    ) -> PlannedObservedPoseDelta:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_planned_observed_deltas('
                'delta_id, observation_id, classification, payload_json) '
                'VALUES(?,?,?,?) ON CONFLICT(delta_id) DO NOTHING',
                (
                    delta.delta_id,
                    delta.observation_id,
                    delta.classification,
                    delta.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_planned_observed_deltas '
                'WHERE delta_id=?',
                (delta.delta_id,),
            ).fetchone()
            if row['payload_json'] != delta.model_dump_json():
                raise ValueError(
                    f'pose delta {delta.delta_id} already persisted with '
                    'different content — deltas are immutable'
                )
        return delta

    def get_delta(
        self, delta_id: str
    ) -> PlannedObservedPoseDelta | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_planned_observed_deltas '
                'WHERE delta_id=?',
                (delta_id,),
            ).fetchone()
        if row is None:
            return None
        return PlannedObservedPoseDelta.model_validate_json(
            row['payload_json']
        )


__all__ = [
    'MeasurementPoseBinding',
    'MeasurementPoseMethod',
    'MeasurementPoseObservation',
    'MeasurementPoseObservationRepository',
    'PlannedObservedPoseDelta',
    'PoseDeltaClass',
    'PoseUncertaintyKind',
    'SpatialUncertainty',
    'UNKNOWN_UNCERTAINTY',
    'build_pose_observation',
    'evaluate_pose_delta',
]
