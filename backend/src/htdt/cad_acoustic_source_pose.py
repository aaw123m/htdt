"""Acoustic speaker localization authority (#783).

Derives an *observed acoustic source pose* from measurement evidence —
never a Scene mutation. The staged contract:

- **ASL10** distance consistency: per-receiver source ranges must satisfy
  the triangle inequality against known receiver geometry — impossible
  ranges surface as ``inconsistent``.
- **ASL20** bounded 3D multilateration: requires >= 4 receivers with
  unambiguous timing-capable direct arrivals and non-degenerate geometry;
  returns a position plus a conservative radial bound, or a verdict —
  ``solvable`` / ``weakly_conditioned`` / ``inconsistent`` /
  ``insufficient_evidence`` — never a forced coordinate.
- **ASL30** aim/directivity fit stays capability-gated: it refuses without
  an exact directivity dataset and multiple receivers — a single SPL
  reading never yields an aim.
- **ASL40** commissioning comparison: observed pose is compared against
  planned design and recorded as-built poses with the #520 tolerance
  contract; physical cabinet pose and acoustic reference stay distinct.

Timing-reference semantics are explicit: absolute time-of-flight methods
require a common timing reference or calibrated latency; unknown transport
latency permits relative diagnostics only. #732 receiver uncertainty is
consumed — receiver bounds widen the reported source bound.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Sequence
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_pose import SpatialUncertainty
from .cad_scene import Position3
from .r120_geometry_compiler import ExactExternalAuthorityRef


TimingReferenceCapability = Literal[
    'common_clock_exact',
    'common_clock_bounded',
    'calibrated_latency_offset',
    'uncalibrated',
    'unknown',
]

ArrivalAmbiguity = Literal['unambiguous', 'ambiguous', 'failed']
ReflectionContamination = Literal['low', 'medium', 'high', 'unknown']

ASLVerdict = Literal[
    'solvable',
    'weakly_conditioned',
    'inconsistent',
    'insufficient_evidence',
]

PoseComparisonStatus = Literal[
    'within_tolerance', 'outside_tolerance', 'indeterminate'
]

_SOURCE_POSE_PREFIX = 'acoustic-source-pose:'
_COMPARISON_PREFIX = 'source-pose-comparison:'

# Multilateration thresholds — deterministic, not tuned per-fixture.
_MIN_RECEIVERS = 4
_MAX_GN_ITERATIONS = 50
_GN_CONVERGENCE_M = 1e-9
_WEAK_CONDITIONING = 25.0
_RESIDUAL_TOLERANCE_FACTOR = 3.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class AcousticReceiverAnchor(BaseModel):
    """One receiver's known pose evidence feeding localization (#732)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    position_m: Position3
    uncertainty: SpatialUncertainty


class DirectArrivalEvidence(BaseModel):
    """One extracted direct arrival; a failed/ambiguous pick stays UNKNOWN."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    arrival_time_s: float = Field(ge=0.0)
    picker_method: str = Field(min_length=1)
    ambiguity: ArrivalAmbiguity = 'unambiguous'
    reflection_contamination: ReflectionContamination = 'unknown'


class AcousticSourcePoseObservation(BaseModel):
    """Immutable observed acoustic source pose evidence (#783).

    The observation records the exact evidence set, timing capability,
    verdict and diagnostics; a non-solvable verdict carries no position.
    It never promotes to as-built/Scene authority — promotion is a separate
    explicit user action through canonical lifecycle authority.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.acoustic-source-pose-observation'] = (
        'htdt.acoustic-source-pose-observation'
    )
    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    acquisition_session_ref: str = Field(min_length=1)
    localization_method: Literal[
        'asl10_distance_consistency', 'asl20_multilateration'
    ] = 'asl20_multilateration'
    method_version: str = Field(min_length=1)
    timing_capability: TimingReferenceCapability
    sound_speed_mps: float = Field(gt=0.0)
    receiver_count: int = Field(ge=0)
    common_latency_s: float = Field(ge=0.0)
    observed_position_m: Position3 | None = None
    uncertainty: SpatialUncertainty
    verdict: ASLVerdict
    range_residuals_m: tuple[float, ...] = ()
    max_residual_m: float | None = Field(default=None, ge=0.0)
    diagnostics: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_observation(self) -> 'AcousticSourcePoseObservation':
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if not self.observation_id.startswith(_SOURCE_POSE_PREFIX):
            raise ValueError(
                'observation id must use acoustic-source-pose: prefix'
            )
        if self.verdict in ('solvable', 'weakly_conditioned'):
            if self.observed_position_m is None:
                raise ValueError(
                    'a solvable verdict requires a position estimate'
                )
            if self.uncertainty.kind == 'unknown':
                raise ValueError(
                    'a solvable verdict requires a bounded uncertainty'
                )
        else:
            if self.observed_position_m is not None:
                raise ValueError(
                    'non-solvable verdicts must not carry a position — '
                    'do not force a coordinate from weak evidence'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'acoustic source pose semantic hash mismatch'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'observation_id': self.observation_id,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'source_entity_id': self.source_entity_id,
            'acquisition_session_ref': self.acquisition_session_ref,
            'localization_method': self.localization_method,
            'method_version': self.method_version,
            'timing_capability': self.timing_capability,
            'sound_speed_mps': self.sound_speed_mps,
            'receiver_count': self.receiver_count,
            'common_latency_s': self.common_latency_s,
            'uncertainty': self.uncertainty.model_dump(mode='json'),
            'verdict': self.verdict,
            'range_residuals_m': list(self.range_residuals_m),
            'diagnostics': list(self.diagnostics),
            'evidence_refs': list(self.evidence_refs),
            'limitations': list(self.limitations),
            'observed_at_utc': self.observed_at_utc,
        }
        for key, value in (
            ('observed_position_m', self.observed_position_m),
            ('max_residual_m', self.max_residual_m),
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


def _receiver_bound_m(anchor: AcousticReceiverAnchor) -> float:
    bound = anchor.uncertainty.bound_m()
    return bound if bound is not None else 1.0


def _linear_solution(
    points: np.ndarray, ranges: np.ndarray
) -> np.ndarray | None:
    """Algebraic initial estimate: subtract the reference receiver's
    sphere equation to linearize, then least-squares solve."""
    p0 = points[0]
    a = 2.0 * (points[1:] - p0)
    b = (
        np.sum(points[1:] ** 2, axis=1)
        - np.sum(p0**2)
        + ranges[0] ** 2
        - ranges[1:] ** 2
    )
    try:
        solution, *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return solution


def _gauss_newton(
    points: np.ndarray, ranges: np.ndarray, x0: np.ndarray
) -> np.ndarray | None:
    x = np.asarray(x0, dtype=float)
    for _ in range(_MAX_GN_ITERATIONS):
        diffs = x - points
        norms = np.linalg.norm(diffs, axis=1)
        if np.any(norms < 1e-9):
            return None
        residuals = norms - ranges
        jacobian = diffs / norms[:, None]
        try:
            step, *_ = np.linalg.lstsq(jacobian, -residuals, rcond=None)
        except np.linalg.LinAlgError:
            return None
        x = x + step
        if float(np.linalg.norm(step)) < _GN_CONVERGENCE_M:
            break
    return x


def localize_acoustic_source(
    *,
    document_id: str,
    source_entity_id: str,
    acquisition_session_ref: str,
    receivers: Sequence[AcousticReceiverAnchor],
    arrivals: Sequence[DirectArrivalEvidence],
    timing_capability: TimingReferenceCapability,
    sound_speed_mps: float = 343.0,
    common_latency_s: float = 0.0,
    latency_bound_s: float = 0.0,
    evidence_refs: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    observation_id: str | None = None,
    authority_version: str = '1',
    observed_at_utc: str | None = None,
) -> AcousticSourcePoseObservation:
    """ASL10+ASL20: bounded multilateration over known receiver poses.

    Returns an immutable ``AcousticSourcePoseObservation``; the verdict
    records why a position exists or why it stays UNKNOWN.
    """

    diagnostics: list[str] = []
    if sound_speed_mps <= 0:
        raise ValueError('sound_speed_mps must be positive')
    if latency_bound_s < 0 or common_latency_s < 0:
        raise ValueError('latency values must be non-negative')

    def _finish(
        verdict: ASLVerdict,
        *,
        position: Position3 | None = None,
        uncertainty: SpatialUncertainty | None = None,
        residuals: tuple[float, ...] = (),
        max_residual: float | None = None,
        receiver_count: int = 0,
    ) -> AcousticSourcePoseObservation:
        payload: dict[str, Any] = {
            'observation_id': observation_id
            or f'{_SOURCE_POSE_PREFIX}{uuid4()}',
            'authority_version': authority_version,
            'document_id': document_id,
            'source_entity_id': source_entity_id,
            'acquisition_session_ref': acquisition_session_ref,
            'localization_method': 'asl20_multilateration',
            'method_version': 'asl20-gauss-newton-1',
            'timing_capability': timing_capability,
            'sound_speed_mps': sound_speed_mps,
            'receiver_count': receiver_count,
            'common_latency_s': common_latency_s,
            'observed_position_m': position,
            'uncertainty': uncertainty or SpatialUncertainty(kind='unknown'),
            'verdict': verdict,
            'range_residuals_m': residuals,
            'max_residual_m': max_residual,
            'diagnostics': tuple(diagnostics),
            'evidence_refs': evidence_refs,
            'limitations': limitations,
            'observed_at_utc': observed_at_utc or _utc_now(),
        }
        provisional = AcousticSourcePoseObservation.model_construct(
            **payload, semantic_sha256='0' * 64
        )
        return AcousticSourcePoseObservation.model_validate(
            {
                **payload,
                'semantic_sha256': _hash(provisional.identity_payload()),
            }
        )

    if timing_capability in ('uncalibrated', 'unknown'):
        diagnostics.append(
            f'timing capability {timing_capability} does not support '
            'absolute time-of-flight — only relative diagnostics'
        )
        return _finish('insufficient_evidence')

    anchors = {anchor.receiver_id: anchor for anchor in receivers}
    usable: list[tuple[AcousticReceiverAnchor, DirectArrivalEvidence]] = []
    for arrival in arrivals:
        anchor = anchors.get(arrival.receiver_id)
        if anchor is None:
            diagnostics.append(
                f'arrival for unknown receiver {arrival.receiver_id} ignored'
            )
            continue
        if arrival.ambiguity != 'unambiguous':
            diagnostics.append(
                f'arrival at {arrival.receiver_id} is {arrival.ambiguity} '
                '— not used for ranging'
            )
            continue
        if arrival.reflection_contamination == 'high':
            diagnostics.append(
                f'arrival at {arrival.receiver_id} is reflection-dominated '
                '— not used for ranging'
            )
            continue
        usable.append((anchor, arrival))

    if len(usable) < _MIN_RECEIVERS:
        diagnostics.append(
            f'{len(usable)} usable receivers — need at least '
            f'{_MIN_RECEIVERS} for 3D multilateration'
        )
        return _finish('insufficient_evidence', receiver_count=len(usable))

    # ASL20: derive ranges; #732 receiver bounds + latency bound widen them.
    points = np.array(
        [
            [a.position_m.x_m, a.position_m.y_m, a.position_m.z_m]
            for a, _ in usable
        ],
        dtype=float,
    )
    ranges = np.array(
        [
            max(0.0, (arr.arrival_time_s - common_latency_s) * sound_speed_mps)
            for _, arr in usable
        ],
        dtype=float,
    )
    bounds = np.array(
        [_receiver_bound_m(a) for a, _ in usable], dtype=float
    ) + latency_bound_s * sound_speed_mps

    # ASL10 consistency: pairwise ranges must satisfy the triangle
    # inequality against known receiver geometry within evidence bounds.
    inconsistent = False
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            d_ij = float(np.linalg.norm(points[i] - points[j]))
            gap = abs(ranges[i] - ranges[j]) - d_ij
            if gap > bounds[i] + bounds[j] + 1e-9:
                inconsistent = True
                diagnostics.append(
                    'range inconsistency: receivers '
                    f'{usable[i][0].receiver_id}/{usable[j][0].receiver_id} '
                    f'(|r_i-r_j|-d={gap:.4f}m exceeds combined bound '
                    f'{bounds[i] + bounds[j]:.4f}m)'
                )
            if ranges[i] + ranges[j] < d_ij - bounds[i] - bounds[j] - 1e-9:
                inconsistent = True
                diagnostics.append(
                    'range impossibility: receivers '
                    f'{usable[i][0].receiver_id}/{usable[j][0].receiver_id} '
                    'ranges cannot reach both positions'
                )
    if inconsistent:
        return _finish(
            'inconsistent',
            receiver_count=len(usable),
        )

    initial = _linear_solution(points, ranges)
    if initial is None or not np.all(np.isfinite(initial)):
        diagnostics.append('no algebraic initial solution')
        return _finish('insufficient_evidence', receiver_count=len(usable))
    estimate = _gauss_newton(points, ranges, initial)
    if estimate is None or not np.all(np.isfinite(estimate)):
        diagnostics.append('multilateration did not converge')
        return _finish('insufficient_evidence', receiver_count=len(usable))

    diffs = estimate - points
    norms = np.linalg.norm(diffs, axis=1)
    jacobian = diffs / norms[:, None]
    try:
        condition = float(np.linalg.cond(jacobian))
    except np.linalg.LinAlgError:
        condition = float('inf')

    residuals = norms - ranges
    max_residual = float(np.max(np.abs(residuals)))
    residual_tolerance = float(
        np.max(bounds) * _RESIDUAL_TOLERANCE_FACTOR
    )
    if max_residual > residual_tolerance:
        diagnostics.append(
            f'max range residual {max_residual:.4f}m exceeds the evidence '
            f'bound {residual_tolerance:.4f}m — inconsistent data'
        )
        return _finish(
            'inconsistent',
            residuals=tuple(float(r) for r in residuals),
            max_residual=max_residual,
            receiver_count=len(usable),
        )

    verdict: ASLVerdict = 'solvable'
    if not np.isfinite(condition) or condition > _WEAK_CONDITIONING:
        verdict = 'weakly_conditioned'
        diagnostics.append(
            f'poorly conditioned receiver geometry (cond={condition:.1f})'
        )

    # Reported bound: worst receiver bound scaled by conditioning so
    # centimetre-level receiver evidence never yields millimetre claims.
    radial = float(np.max(bounds)) * max(1.0, min(condition, 10.0))
    uncertainty = SpatialUncertainty(
        kind='radial_tolerance', radial_bound_m=radial
    )
    if verdict == 'weakly_conditioned':
        diagnostics.append(
            'reported bound widened for conditioning; consider more '
            'receivers or better angular diversity'
        )

    return _finish(
        verdict,
        position=Position3(
            x_m=float(estimate[0]),
            y_m=float(estimate[1]),
            z_m=float(estimate[2]),
        ),
        uncertainty=uncertainty,
        residuals=tuple(float(r) for r in residuals),
        max_residual=max_residual,
        receiver_count=len(usable),
    )


def check_aim_capability(
    *,
    directivity_dataset_ref: str | None,
    receiver_count: int,
) -> tuple[bool, str]:
    """ASL30 gate: aim estimation needs exact directivity evidence plus
    >= 2 receivers with compatible data — a single SPL reading never
    yields an aim hypothesis."""
    if not directivity_dataset_ref:
        return False, 'no directivity dataset bound to the source'
    if receiver_count < 2:
        return False, 'aim cannot be derived from a single receiver'
    return True, 'aim estimation is capability-satisfied'


class SourcePoseComparison(BaseModel):
    """ASL40: observed vs. design vs. as-built pose comparison (#783/#520).

    All three poses are shown separately; status reuses the tolerance
    contract — 'indeterminate' whenever a required pose or bound is absent.
    """

    model_config = ConfigDict(frozen=True)

    comparison_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    design_position_m: Position3 | None = None
    as_built_position_m: Position3 | None = None
    observed_position_m: Position3 | None = None
    delta_vs_design_m: tuple[float, float, float] | None = None
    delta_vs_as_built_m: tuple[float, float, float] | None = None
    status_vs_design: PoseComparisonStatus
    status_vs_as_built: PoseComparisonStatus
    tolerance_bound_m: float | None = None
    observed_uncertainty_bound_m: float | None = None
    detail: str = ''


def _compare(
    observed: Position3 | None,
    reference: Position3 | None,
    tolerance_bound_m: float | None,
) -> tuple[tuple[float, float, float] | None, PoseComparisonStatus]:
    if observed is None or reference is None or tolerance_bound_m is None:
        return None, 'indeterminate'
    delta = (
        observed.x_m - reference.x_m,
        observed.y_m - reference.y_m,
        observed.z_m - reference.z_m,
    )
    norm = float(sum(d * d for d in delta) ** 0.5)
    return delta, (
        'within_tolerance' if norm <= tolerance_bound_m
        else 'outside_tolerance'
    )


def compare_source_pose(
    observation: AcousticSourcePoseObservation,
    *,
    design_position_m: Position3 | None,
    as_built_position_m: Position3 | None = None,
    tolerance_bound_m: float | None = None,
    comparison_id: str | None = None,
) -> SourcePoseComparison:
    """Compare the observed acoustic reference point to design/as-built
    poses — which point is compared is the acoustic reference, never the
    cabinet centroid."""

    observed = observation.observed_position_m
    delta_design, status_design = _compare(
        observed, design_position_m, tolerance_bound_m
    )
    delta_built, status_built = _compare(
        observed, as_built_position_m, tolerance_bound_m
    )
    if design_position_m is None and as_built_position_m is None:
        detail = 'no reference pose supplied — comparison indeterminate'
    elif observed is None:
        detail = 'observation verdict carried no position'
    else:
        detail = ''
    return SourcePoseComparison(
        comparison_id=comparison_id or f'{_COMPARISON_PREFIX}{uuid4()}',
        source_entity_id=observation.source_entity_id,
        design_position_m=design_position_m,
        as_built_position_m=as_built_position_m,
        observed_position_m=observed,
        delta_vs_design_m=delta_design,
        delta_vs_as_built_m=delta_built,
        status_vs_design=status_design,
        status_vs_as_built=status_built,
        tolerance_bound_m=tolerance_bound_m,
        observed_uncertainty_bound_m=observation.uncertainty.bound_m(),
        detail=detail,
    )


class AcousticSourcePoseRepository:
    """Append-only acoustic-source-pose store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_source_poses (
                    observation_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    observed_at_utc TEXT NOT NULL,
                    semantic_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def save_observation(
        self, observation: AcousticSourcePoseObservation
    ) -> AcousticSourcePoseObservation:
        """Append; identical re-save is a no-op, conflicting content fails."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_acoustic_source_poses('
                'observation_id, document_id, source_entity_id, verdict, '
                'observed_at_utc, semantic_sha256, payload_json) '
                'VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(observation_id) DO NOTHING',
                (
                    observation.observation_id,
                    observation.document_id,
                    observation.source_entity_id,
                    observation.verdict,
                    observation.observed_at_utc,
                    observation.semantic_sha256,
                    observation.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone()
            if row['payload_json'] != observation.model_dump_json():
                raise ValueError(
                    f'acoustic source pose {observation.observation_id} '
                    'already persisted with different content — '
                    'observations are immutable'
                )
        return observation

    def get_observation(
        self, observation_id: str
    ) -> AcousticSourcePoseObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticSourcePoseObservation.model_validate_json(
            row['payload_json']
        )

    def list_observations_for_source(
        self, document_id: str, source_entity_id: str
    ) -> tuple[AcousticSourcePoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_source_poses '
                'WHERE document_id=? AND source_entity_id=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (document_id, source_entity_id),
            ).fetchall()
        return tuple(
            AcousticSourcePoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )


__all__ = [
    'AcousticReceiverAnchor',
    'AcousticSourcePoseObservation',
    'AcousticSourcePoseRepository',
    'ArrivalAmbiguity',
    'ASLVerdict',
    'DirectArrivalEvidence',
    'PoseComparisonStatus',
    'ReflectionContamination',
    'SourcePoseComparison',
    'TimingReferenceCapability',
    'check_aim_capability',
    'compare_source_pose',
    'localize_acoustic_source',
]
