"""Bounded R150 stochastic-ray receiver estimator (slice b of the R150
decomposition).

This module implements a seeded, deterministic Monte Carlo ray estimator that
produces per-(source, receiver, frequency band) relative-energy receiver
estimates with declared convergence evidence. It is a separate engine/authority
family from the deterministic path lane: the estimator runs on the same exact
R120 compiled geometry + READY dispatch chain but emits
``StochasticReceiverEstimateArtifact`` under its own implementation ref, policy
authority, observable (``stochastic_receiver_estimate``), persistence table and
artifact schema ref.

Estimator contract (declared in
:data:`HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF`):

- directions are drawn from a SHA-256 counter stream over
  ``(seed, ray_index)`` and mapped uniformly to the unit sphere
  (``sha256_counter_uniform_spherical_v1``) — pure integer/byte arithmetic, so
  the direction sequence is bit-identical across runs and platforms;
- each ray propagates in straight legs; a boundary surface hit either
  transmits through a declared Portal aperture polygon (free transmission,
  consumes one interaction slot) or reflects specularly off the surface plane,
  applying the banded scalar specular energy factor ``(1-a)(1-s)`` per band;
- non-room-boundary (occluder) triangles absorb the ray; a ray that hits
  nothing within the bounded escape length terminates ('escape');
- a receiver captures a ray when the ray's first leg segment that passes within
  ``receiver_capture_radius_m`` of the receiver point exists — the deposit is
  the ray's per-band weight at that leg;
- the per-level estimate is
  ``E = 4 * sum(x_j) / (r^2 * N)`` over the first ``N`` rays, where ``x_j`` is
  the captured weight or 0 — the free-field limit reproduces the deterministic
  lane's direct-path ``w / L^2`` convention;
- convergence evidence is prefix-consistent refinement over strictly
  increasing ray budgets plus the final-level sample standard error and the
  declared relative-change bound verdict.

The estimator never fabricates: unresolvable directivity or boundary material
band quantities reject the affected estimates (fail closed), and policy/
topology violations raise typed errors.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from math import cos, isfinite, pi, sin, sqrt
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_solver_adapter import (
    AcousticSolverAdapterDescriptor,
    AcousticSolverDispatchBinding,
)
from .cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from .cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from .cad_directivity import DirectivityDataset
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_adapter import (
    ConfigurationResolver,
    DeterministicGaExecutionInput,
    DeterministicGaUnsupportedError,
    GeometricMaterialAuthority,
    GeometricSurfacePlane,
    GeometryAuthorityResolver,
    MaterialAuthorityResolver,
    _authority_ref,
    _directivity_contribution,
    _material_contribution,
    _occluder_object_triangle_indices,
    _point_on_triangle_surface,
    _position_tuple,
    _require_supported_feature_scale,
    _segment_triangle_intersection_parameter,
    _triangle_vertices,
    _validate_supported_topology,
    _vector,
    _dot,
    _norm,
    _unit,
    _cross,
)
from .cad_geometric_acoustics_portal import (
    GeometricPortalAperture,
    point_in_portal_aperture,
)
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .r120_geometry_compiler import (
    AcousticRegionAuthority,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
)
from .canonical_json import canonical_sha256 as _semantic_hash
from .clock import utc_now_iso as _utc_now


STOCHASTIC_RAY_SCHEMA_VERSION = 1
STOCHASTIC_RAY_AUTHORITY_VERSION = 'r150-stochastic-ray-1'
STOCHASTIC_RAY_ADAPTER_ID = 'htdt.r150.stochastic-ray-receiver'
STOCHASTIC_RAY_ADAPTER_VERSION = '1'

STOCHASTIC_RAY_ENGINE_ID = 'htdt.r150.stochastic_ray_receiver_estimate'
STOCHASTIC_RAY_ENGINE_VERSION = '1'

STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE = 'stochastic_receiver_estimate'

#: Bounded estimator numerical envelope. Declared here and pinned in the
#: implementation ref payload so a policy cannot quietly widen the lane.
MIN_RAY_BUDGET = 64
MAX_RAY_BUDGET = 1_000_000
MAX_BOUNCE_BOUND = 8
#: A surface hit must occur strictly more than this many geometric tolerances
#: beyond the leg origin; closer hits are self-intersection artifacts of the
#: bounce/crossing seeding and are ignored.
_SELF_HIT_EPSILON_FACTOR = 4.0
#: The receiver capture radius must clear the geometric tolerance by this
#: factor and stay under this share of the smallest scene extent.
_CAPTURE_RADIUS_MIN_FACTOR = 10.0
_CAPTURE_RADIUS_MAX_EXTENT_FRACTION = 0.25
#: Termination distance for a ray leg that hits no boundary/occluder, as a
#: multiple of the compiled-geometry bounding diagonal. Only a non-watertight
#: shell can produce an escape; the event is recorded, never silently dropped.
_ESCAPE_LENGTH_FACTOR = 8.0

StochasticEstimateDecision = Literal[
    'UNSUPPORTED_DIRECTIVITY',
    'UNSUPPORTED_BOUNDARY_QUANTITY',
]
ConvergenceVerdict = Literal['WITHIN_DECLARED_BOUND', 'OUTSIDE_DECLARED_BOUND']
StochasticUnsupportedReason = Literal[
    'UNSUPPORTED_GEOMETRY',
    'UNSUPPORTED_PORTAL_TOPOLOGY',
    'UNSUPPORTED_REGION_TOPOLOGY',
    'UNSUPPORTED_REGION_MEMBERSHIP',
    'CAPTURE_RADIUS_OUT_OF_BOUNDS',
]


class StochasticRayUnsupportedError(ValueError):
    """Typed fail-closed capability error for the stochastic estimator lane."""

    def __init__(
        self,
        reason_code: StochasticUnsupportedReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-stochastic-ray-receiver-estimate',
    authority_version=STOCHASTIC_RAY_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': STOCHASTIC_RAY_ENGINE_ID,
            'version': STOCHASTIC_RAY_ENGINE_VERSION,
            'construction': (
                'seeded_uniform_spherical_ray_casting_bounded_specular_bounce_'
                'receiver_sphere_capture'
            ),
            'direction_sampling': 'sha256_counter_uniform_spherical_v1',
            'receiver_capture_model': (
                'first_leg_closest_approach_within_radius_v1'
            ),
            'portal_aperture_semantics': (
                'free_transmission_consumes_interaction_slot_v1'
            ),
            'occluder_semantics': 'absorbing_termination_v1',
            'bounce_material_semantics': (
                'banded_scalar_specular_energy_factor_v1'
            ),
            'estimator_contract': (
                'capture_weight_sum_times_4_over_n_r2_per_m2_v1'
            ),
            'convergence_rule': (
                'final_level_relative_change_within_declared_bound_v1'
            ),
            'maximum_bounces_range': [1, MAX_BOUNCE_BOUND],
            'ray_budget_range': [MIN_RAY_BUDGET, MAX_RAY_BUDGET],
            'energy_semantics': 'monte_carlo_point_estimate_not_upper_bound',
            'coherent_phase': 'not_applicable_energy_domain',
        }
    ),
)

STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.stochastic-receiver-estimate-artifact.schema',
    authority_version=STOCHASTIC_RAY_AUTHORITY_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'StochasticReceiverEstimateArtifact',
            'schema_version': STOCHASTIC_RAY_SCHEMA_VERSION,
            'quantity': 'estimated_relative_energy_transport_per_m2',
            'coherent_phase': 'not_applicable_energy_domain',
        }
    ),
)


class HtdtStochasticRayReceiverEngine:
    """Exact engine marker for the bounded R150 stochastic-ray kernel.

    The kernel is energy-domain only: it never synthesizes coherent phase.
    ``solver_implementation_ref`` is the kernel's own authority — distinct
    from the dispatch's deterministic-GA solver implementation ref, which
    the artifact records separately under ``solver_implementation_ref``.
    """

    engine_id = STOCHASTIC_RAY_ENGINE_ID
    engine_version = STOCHASTIC_RAY_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF
    direction_sampling = 'sha256_counter_uniform_spherical_v1'
    receiver_capture_model = 'first_leg_closest_approach_within_radius_v1'
    coherent_phase = 'not_applicable_energy_domain'
    energy_semantics = 'monte_carlo_point_estimate_not_upper_bound'


class StochasticRayEstimationPolicy(BaseModel):
    """Versioned bounded sampling policy authority for estimator execution."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STOCHASTIC_RAY_SCHEMA_VERSION
    authority_version: Literal['r150-stochastic-ray-1'] = (
        STOCHASTIC_RAY_AUTHORITY_VERSION
    )
    policy_id: str = Field(pattern=r'^r150-stochastic-ray-policy:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    sampling_seed: int = Field(ge=0, lt=2**64)
    ray_budgets: tuple[int, ...] = Field(min_length=2)
    maximum_bounces: int = Field(ge=1, le=MAX_BOUNCE_BOUND)
    receiver_capture_radius_m: float = Field(gt=0.0)
    declared_convergence_bound: float = Field(gt=0.0, le=1.0)
    convergence_floor_per_m2: float = Field(gt=0.0)
    direction_sampling: Literal['sha256_counter_uniform_spherical_v1'] = (
        'sha256_counter_uniform_spherical_v1'
    )
    receiver_capture_model: Literal[
        'first_leg_closest_approach_within_radius_v1'
    ] = 'first_leg_closest_approach_within_radius_v1'
    estimator_contract: Literal[
        'capture_weight_sum_times_4_over_n_r2_per_m2_v1'
    ] = 'capture_weight_sum_times_4_over_n_r2_per_m2_v1'
    convergence_rule: Literal[
        'final_level_relative_change_within_declared_bound_v1'
    ] = 'final_level_relative_change_within_declared_bound_v1'

    @model_validator(mode='after')
    def validate_policy(self) -> 'StochasticRayEstimationPolicy':
        budgets = tuple(self.ray_budgets)
        for budget in budgets:
            if budget < MIN_RAY_BUDGET or budget > MAX_RAY_BUDGET:
                raise ValueError(
                    f'stochastic ray budget {budget} is outside the declared '
                    f'bounded range [{MIN_RAY_BUDGET}, {MAX_RAY_BUDGET}]'
                )
        if tuple(sorted(set(budgets))) != budgets or len(set(budgets)) != len(
            budgets
        ):
            raise ValueError(
                'stochastic ray budgets must be unique and strictly increasing'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('StochasticRayEstimationPolicy semantic hash mismatch')
        if self.policy_id != f'r150-stochastic-ray-policy:{expected}':
            raise ValueError('StochasticRayEstimationPolicy id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'policy_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.policy_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_stochastic_ray_estimation_policy(
    *,
    sampling_seed: int,
    ray_budgets: Sequence[int] = (128, 512, 2048),
    maximum_bounces: int = 4,
    receiver_capture_radius_m: float,
    declared_convergence_bound: float,
    convergence_floor_per_m2: float = 1.0e-9,
) -> StochasticRayEstimationPolicy:
    budgets = tuple(int(item) for item in ray_budgets)
    core: dict[str, Any] = {
        'schema_version': STOCHASTIC_RAY_SCHEMA_VERSION,
        'authority_version': STOCHASTIC_RAY_AUTHORITY_VERSION,
        'sampling_seed': int(sampling_seed),
        'ray_budgets': list(budgets),
        'maximum_bounces': int(maximum_bounces),
        'receiver_capture_radius_m': float(receiver_capture_radius_m),
        'declared_convergence_bound': float(declared_convergence_bound),
        'convergence_floor_per_m2': float(convergence_floor_per_m2),
        'direction_sampling': 'sha256_counter_uniform_spherical_v1',
        'receiver_capture_model': 'first_leg_closest_approach_within_radius_v1',
        'estimator_contract': 'capture_weight_sum_times_4_over_n_r2_per_m2_v1',
        'convergence_rule': 'final_level_relative_change_within_declared_bound_v1',
    }
    digest = _semantic_hash(core)
    return StochasticRayEstimationPolicy(
        policy_id=f'r150-stochastic-ray-policy:{digest}',
        semantic_sha256=digest,
        **core,
    )


class StochasticRayCapabilityRecord(BaseModel):
    """Explicit bounded-capability disclosure persisted inside the artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    supported_geometry_policies: tuple[str, ...] = (
        'general_planar_closed_polyhedral_v1',
        'general_planar_multi_region_portal_v1',
    )
    direction_sampling: Literal['sha256_counter_uniform_spherical_v1'] = (
        'sha256_counter_uniform_spherical_v1'
    )
    receiver_capture_model: Literal[
        'first_leg_closest_approach_within_radius_v1'
    ] = 'first_leg_closest_approach_within_radius_v1'
    estimator_contract: Literal[
        'capture_weight_sum_times_4_over_n_r2_per_m2_v1'
    ] = 'capture_weight_sum_times_4_over_n_r2_per_m2_v1'
    convergence_rule: Literal[
        'final_level_relative_change_within_declared_bound_v1'
    ] = 'final_level_relative_change_within_declared_bound_v1'
    energy_semantics: Literal['monte_carlo_point_estimate_not_upper_bound'] = (
        'monte_carlo_point_estimate_not_upper_bound'
    )
    coherent_phase: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    unsupported_capabilities: tuple[str, ...] = (
        'legacy axis-aligned shoebox plane lane '
        '(exact_axis_aligned_closed_shoebox_v1) — the estimator traces '
        'compiled triangles only',
        'diffuse or lobed scattering redistribution — each bounce applies the '
        'banded scalar specular energy factor (1-a)(1-s); scattered energy '
        'leaves the ray',
        'edge or aperture diffraction — rays resolve against boundary '
        'triangles and declared Portal aperture polygons only',
        'late-decay envelope or arrival-time structure — the artifact carries '
        'energy estimates only; the late-field observable is a separate '
        'bounded lane',
        'coherent phase — estimates are energy-domain only and carry no '
        'phase authority',
    )


class StochasticEstimateLevelEvidence(BaseModel):
    """Per-budget-level convergence sample for one estimate."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    ray_budget: int = Field(gt=0)
    captured_ray_count: int = Field(ge=0)
    estimate_per_m2: float = Field(ge=0.0)


class StochasticReceiverBandEstimate(BaseModel):
    """One (source, receiver, band) receiver estimate with convergence proof."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    estimate_id: str = Field(
        pattern=r'^stochastic-receiver-band-estimate:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    center_hz: float = Field(gt=0.0)
    levels: tuple[StochasticEstimateLevelEvidence, ...] = Field(min_length=2)
    estimate_per_m2: float = Field(ge=0.0)
    standard_error_per_m2: float = Field(ge=0.0)
    relative_deviation_from_previous_level: float = Field(ge=0.0)
    convergence_verdict: ConvergenceVerdict

    @model_validator(mode='after')
    def validate_estimate(self) -> 'StochasticReceiverBandEstimate':
        budgets = [item.ray_budget for item in self.levels]
        if budgets != sorted(set(budgets)):
            raise ValueError(
                'stochastic estimate levels must use strictly increasing budgets'
            )
        if self.estimate_per_m2 != self.levels[-1].estimate_per_m2:
            raise ValueError(
                'stochastic estimate headline value must reproduce the final level'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError(
                'StochasticReceiverBandEstimate semantic hash mismatch'
            )
        if self.estimate_id != f'stochastic-receiver-band-estimate:{expected}':
            raise ValueError('StochasticReceiverBandEstimate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'estimate_id', 'semantic_sha256'},
        )


class StochasticEstimateRejectedCandidate(BaseModel):
    """Typed rejection record for a contaminated (source, receiver, band)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    center_hz: float = Field(gt=0.0)
    interaction_surface_id: str | None = None
    decision: StochasticEstimateDecision
    reason: str = Field(min_length=1)


class StochasticReceiverEstimateArtifact(BaseModel):
    """Immutable energy-domain stochastic receiver estimate authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STOCHASTIC_RAY_SCHEMA_VERSION
    authority_version: Literal['r150-stochastic-ray-1'] = (
        STOCHASTIC_RAY_AUTHORITY_VERSION
    )
    artifact_id: str = Field(
        pattern=r'^stochastic-receiver-estimate-artifact:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(
        pattern=r'^r150-stochastic-ray-execution:[0-9a-f]{64}$'
    )
    execution_input_id: str
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    stochastic_policy_ref: ExactExternalAuthorityRef
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    engine_id: str
    engine_version: str
    stochastic_ray_implementation_ref: ExactExternalAuthorityRef
    candidate_source_commit: str | None = None
    numeric_comparison_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    frequency_domain: FrequencyDomain
    estimation_scope: Literal['bounded_stochastic_ray_receiver_estimate_v1'] = (
        'bounded_stochastic_ray_receiver_estimate_v1'
    )
    energy_semantics: Literal[
        'monte_carlo_point_estimate_with_convergence_evidence'
    ] = 'monte_carlo_point_estimate_with_convergence_evidence'
    coherent_phase_authority: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )

    sampling_seed: int = Field(ge=0)
    ray_budgets: tuple[int, ...] = Field(min_length=2)
    maximum_bounces: int = Field(ge=1, le=MAX_BOUNCE_BOUND)
    receiver_capture_radius_m: float = Field(gt=0.0)
    declared_convergence_bound: float = Field(gt=0.0, le=1.0)
    convergence_floor_per_m2: float = Field(gt=0.0)

    capability_record: StochasticRayCapabilityRecord
    convergence_status: Literal[
        'WITHIN_DECLARED_BOUND',
        'OUTSIDE_DECLARED_BOUND',
        'NO_EVALUABLE_ESTIMATE',
    ]
    estimates: tuple[StochasticReceiverBandEstimate, ...]
    rejected_candidates: tuple[StochasticEstimateRejectedCandidate, ...]

    @model_validator(mode='after')
    def validate_artifact(self) -> 'StochasticReceiverEstimateArtifact':
        budgets = tuple(self.ray_budgets)
        if tuple(sorted(set(budgets))) != budgets or len(set(budgets)) != len(
            budgets
        ):
            raise ValueError(
                'stochastic estimate artifact ray budgets must be unique and '
                'strictly increasing'
            )
        estimate_ids = [item.estimate_id for item in self.estimates]
        if len(estimate_ids) != len(set(estimate_ids)):
            raise ValueError(
                'stochastic estimate artifact contains duplicate estimate ids'
            )
        ordering = [
            (item.source_entity_id, item.receiver_id, item.center_hz)
            for item in self.estimates
        ]
        if ordering != sorted(ordering):
            raise ValueError(
                'stochastic receiver estimates must use canonical ordering'
            )
        for estimate in self.estimates:
            if tuple(
                item.ray_budget for item in estimate.levels
            ) != budgets:
                raise ValueError(
                    'stochastic estimate level budgets must reproduce the '
                    'declared policy budgets'
                )
        rejected_order = [
            (
                item.source_entity_id,
                item.receiver_id,
                item.center_hz,
                item.interaction_surface_id or '',
                item.decision,
                item.reason,
            )
            for item in self.rejected_candidates
        ]
        if rejected_order != sorted(rejected_order):
            raise ValueError(
                'stochastic estimate rejections must use canonical ordering'
            )
        estimate_keys = {
            (item.source_entity_id, item.receiver_id, item.center_hz)
            for item in self.estimates
        }
        rejected_keys = {
            (item.source_entity_id, item.receiver_id, item.center_hz)
            for item in self.rejected_candidates
        }
        if estimate_keys & rejected_keys:
            raise ValueError(
                'a stochastic estimate cannot both emit and reject one '
                '(source, receiver, band) row'
            )
        evaluable_verdicts = {
            item.convergence_verdict for item in self.estimates
        }
        expected_status = (
            'NO_EVALUABLE_ESTIMATE'
            if not self.estimates
            else (
                'WITHIN_DECLARED_BOUND'
                if evaluable_verdicts == {'WITHIN_DECLARED_BOUND'}
                else 'OUTSIDE_DECLARED_BOUND'
            )
        )
        if self.convergence_status != expected_status:
            raise ValueError(
                'stochastic artifact convergence status does not reproduce '
                'the per-estimate verdicts'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError(
                'StochasticReceiverEstimateArtifact semantic hash mismatch'
            )
        if self.artifact_id != f'stochastic-receiver-estimate-artifact:{expected}':
            raise ValueError('StochasticReceiverEstimateArtifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )
        for rejection in payload['rejected_candidates']:
            if rejection.get('interaction_surface_id') is None:
                rejection.pop('interaction_surface_id', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def execution_provenance_ref(self) -> ExactExternalAuthorityRef:
        payload = {
            'execution_id': self.execution_id,
            'execution_input_id': self.execution_input_id,
            'execution_input_sha256': self.execution_input_sha256,
            'adapter_descriptor_id': self.adapter_descriptor_id,
            'adapter_descriptor_sha256': self.adapter_descriptor_sha256,
            'solver_implementation_ref': self.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': self.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'stochastic_policy_ref': self.stochastic_policy_ref.model_dump(
                mode='json'
            ),
            'engine_id': self.engine_id,
            'engine_version': self.engine_version,
            'stochastic_ray_implementation_ref': (
                self.stochastic_ray_implementation_ref.model_dump(mode='json')
            ),
            'candidate_source_commit': self.candidate_source_commit,
        }
        digest = _semantic_hash(payload)
        return ExactExternalAuthorityRef(
            authority_id=f'r150-stochastic-ray-execution-provenance:{digest}',
            authority_version=self.authority_version,
            semantic_hash_sha256=digest,
        )


# ---------------------------------------------------------------------------
# Deterministic sampling + tracing internals
# ---------------------------------------------------------------------------


def _sha256_unit_interval(seed: int, index: int, lane: int) -> float:
    """Deterministic uniform(0,1) draw from (seed, ray index, draw lane).

    SHA-256 over an ASCII counter label keeps the stream byte-stable across
    platforms; nothing in the mapping touches nondeterministic state.
    """

    digest = sha256(
        f'r150-stochastic-ray|{seed}|{index}|{lane}'.encode('ascii')
    ).digest()
    value = int.from_bytes(digest[:8], 'big')
    return (value + 0.5) / 18446744073709551616.0


def _ray_direction(seed: int, index: int) -> tuple[float, float, float]:
    """Uniform spherical direction for ray ``index`` under ``seed``."""

    u1 = _sha256_unit_interval(seed, index, 0)
    u2 = _sha256_unit_interval(seed, index, 1)
    z = 1.0 - 2.0 * u1
    azimuth = 2.0 * pi * u2
    radius_xy = sqrt(max(0.0, 1.0 - z * z))
    return (radius_xy * cos(azimuth), radius_xy * sin(azimuth), z)


def _ray_triangle_intersection(
    start: Sequence[float],
    direction: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> float | None:
    """First ray parameter (meters for unit ``direction``) inside a triangle.

    Mirrors the Möller–Trumbore evaluation in
    ``_segment_triangle_intersection_parameter`` for an unbounded ray; hits at
    or below ``tolerance`` are self-intersection artifacts and are rejected.
    """

    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)
    if abs(determinant) <= tolerance:
        return None
    inv_det = 1.0 / determinant
    tvec = _vector(triangle[0], start)
    u = _dot(tvec, pvec) * inv_det
    if u < -tolerance or u > 1.0 + tolerance:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv_det
    if v < -tolerance or u + v > 1.0 + tolerance:
        return None
    t = _dot(edge2, qvec) * inv_det
    if t <= tolerance * _SELF_HIT_EPSILON_FACTOR:
        return None
    return t


@dataclass(frozen=True)
class _RayHit:
    kind: Literal['boundary', 'occluder']
    distance_m: float
    plane: GeometricSurfacePlane | None


def _nearest_ray_hit(
    start: Sequence[float],
    direction: Sequence[float],
    boundary_rows: Sequence[tuple[GeometricSurfacePlane, tuple]],
    occluder_rows: Sequence[tuple],
    *,
    tolerance: float,
) -> _RayHit | None:
    best: _RayHit | None = None
    for plane, triangle in boundary_rows:
        hit = _ray_triangle_intersection(
            start, direction, triangle, tolerance=tolerance
        )
        if hit is not None and (best is None or hit < best.distance_m):
            best = _RayHit('boundary', hit, plane)
    for _surface_id, triangle in occluder_rows:
        hit = _ray_triangle_intersection(
            start, direction, triangle, tolerance=tolerance
        )
        if hit is not None and (best is None or hit < best.distance_m):
            best = _RayHit('occluder', hit, None)
    return best


def _aperture_at_point(
    apertures: Sequence[GeometricPortalAperture] | None,
    point: Sequence[float],
    *,
    tolerance: float,
) -> GeometricPortalAperture | None:
    if not apertures:
        return None
    for aperture in apertures:
        if point_in_portal_aperture(aperture, point, tolerance_m=tolerance):
            return aperture
    return None


def _reflect(
    direction: Sequence[float],
    plane: GeometricSurfacePlane,
) -> tuple[float, float, float]:
    """Specular reflection off the surface plane (plane normal semantics)."""

    if plane.normal is not None:
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
    elif plane.axis is not None and plane.coordinate_m is not None:
        axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
        normal = tuple(
            1.0 if index == axis_index else 0.0 for index in range(3)
        )
    else:
        raise ValueError('reflection surface plane representation is incomplete')
    unit_direction = _unit(direction)
    if _dot(unit_direction, normal) > 0.0:
        normal = tuple(-value for value in normal)
    factor = 2.0 * _dot(unit_direction, normal)
    return _unit(
        tuple(
            unit_direction[index] - factor * normal[index] for index in range(3)
        )
    )


def _segment_receiver_capture(
    start: Sequence[float],
    direction: Sequence[float],
    length_m: float,
    receiver: Sequence[float],
    *,
    capture_radius_m: float,
) -> float | None:
    """Closest-approach parameter along the segment, or None when no capture."""

    offset = _dot(_vector(start, receiver), direction)
    parameter = min(max(offset, 0.0), length_m)
    closest = tuple(
        float(start[index]) + parameter * float(direction[index])
        for index in range(3)
    )
    distance = _norm(_vector(closest, receiver))
    if distance <= capture_radius_m:
        return parameter
    return None


def _triangle_shell_point_membership(
    compiled: R120CompiledGeometry,
    triangles: Sequence[tuple[Sequence[float], Sequence[float], Sequence[float]]],
    point: Sequence[float],
    *,
    tolerance: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    """Parity membership over an explicit triangle shell.

    Same evaluation as ``_region_point_membership`` but over triangle vertex
    tuples, so a Portal aperture polygon — a hole in the declared boundary
    surfaces — can be counted as part of each region's enclosing shell.
    """

    if not triangles:
        return 'ambiguous'
    if any(
        _point_on_triangle_surface(point, triangle, tolerance=tolerance)
        for triangle in triangles
    ):
        return 'boundary'

    bounds = compiled.bounding_volume
    diagonal = sqrt(
        (bounds.max_x_m - bounds.min_x_m) ** 2
        + (bounds.max_y_m - bounds.min_y_m) ** 2
        + (bounds.max_z_m - bounds.min_z_m) ** 2
    )
    ray_length = max(1.0, diagonal * 4.0)
    directions = (
        _unit((1.0, 0.3713906763541037, 0.217031)),
        _unit((-0.419, 1.0, 0.163)),
        _unit((0.271, -0.337, 1.0)),
    )
    decisions: list[bool] = []
    t_tolerance = max(1.0e-12, tolerance / ray_length * 4.0)
    for direction in directions:
        end = tuple(
            float(point[index]) + ray_length * direction[index]
            for index in range(3)
        )
        hits = sorted(
            hit
            for triangle in triangles
            if (
                hit := _segment_triangle_intersection_parameter(
                    point,
                    end,
                    triangle,
                    tolerance=tolerance,
                    distance_scaled_tolerance=True,
                )
            )
            is not None
        )
        distinct_hits: list[float] = []
        for hit in hits:
            if not distinct_hits or abs(hit - distinct_hits[-1]) > t_tolerance:
                distinct_hits.append(hit)
        decisions.append(len(distinct_hits) % 2 == 1)
    if len(set(decisions)) != 1:
        return 'ambiguous'
    return 'inside' if decisions[0] else 'outside'


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_stochastic_ray_receiver_estimate(
    *,
    execution_input: DeterministicGaExecutionInput,
    stochastic_policy: StochasticRayEstimationPolicy,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    material_resolver: MaterialAuthorityResolver,
    engine: HtdtStochasticRayReceiverEngine | None = None,
) -> StochasticReceiverEstimateArtifact:
    """Evaluate the bounded stochastic-ray receiver estimate authority.

    Fails closed (raises) on engine/topology/policy violations and records
    per-(source, receiver, band) rejections for unresolvable directivity or
    boundary material quantities — never silently degraded or fabricated.
    """

    if engine is None:
        engine = HtdtStochasticRayReceiverEngine()
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
    )
    stochastic_policy = StochasticRayEstimationPolicy.model_validate(
        stochastic_policy.model_dump(mode='python')
    )
    if (
        engine.engine_id != STOCHASTIC_RAY_ENGINE_ID
        or engine.engine_version != STOCHASTIC_RAY_ENGINE_VERSION
        or engine.solver_implementation_ref
        != HTDT_STOCHASTIC_RAY_RECEIVER_IMPLEMENTATION_REF
    ):
        raise ValueError(
            'stochastic engine id/version does not reproduce the registered '
            'exact solver implementation authority'
        )
    if (
        compiled_geometry.compiled_geometry_id
        != execution_input.r120_compiled_geometry_id
        or compiled_geometry.compiled_hash_sha256
        != execution_input.r120_compiled_geometry_sha256
        or compiled_geometry.topology_identity_sha256
        != execution_input.topology_identity_sha256
    ):
        raise ValueError(
            'stochastic execution compiled geometry exact identity mismatch'
        )
    if (
        _authority_ref(region_authority) != execution_input.region_authority_ref
        or _authority_ref(portal_authority) != execution_input.portal_authority_ref
        or _authority_ref(boundary_termination_authority)
        != execution_input.boundary_termination_authority_ref
    ):
        raise ValueError('stochastic exact geometry authority mismatch')

    if execution_input.geometry_policy not in (
        'general_planar_closed_polyhedral_v1',
        'general_planar_multi_region_portal_v1',
    ):
        raise StochasticRayUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'stochastic receiver estimation requires compiled-triangle '
            'boundary geometry (general_planar_closed_polyhedral_v1 or '
            'general_planar_multi_region_portal_v1); the legacy axis-aligned '
            'shoebox plane lane is unsupported',
        )
    if execution_input.unsupported_reflection_surface_ids:
        raise StochasticRayUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'stochastic receiver estimation requires every reflection surface '
            'to carry exact compiled triangles',
        )
    if (
        execution_input.occluder_triangle_indices
        != _occluder_object_triangle_indices(compiled_geometry)
    ):
        raise ValueError(
            'stochastic execution input occluder triangle set does not '
            'reproduce the compiled geometry authority'
        )
    _require_supported_feature_scale(compiled_geometry)
    portal_geometry = (
        execution_input.geometry_policy
        == 'general_planar_multi_region_portal_v1'
    )
    if portal_geometry:
        # Multi-region Portal lane: declared AcousticRegions connected by an
        # explicit Portal list; termination authority is not reinterpreted.
        if (
            boundary_termination_authority.declaration_mode != 'explicit_none'
        ):
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'stochastic receiver estimation does not reinterpret or '
                'transmit nontrivial BoundaryTermination authority',
            )
        if (
            len(region_authority.declarations) < 2
            or portal_authority.declaration_mode != 'explicit_list'
            or not portal_authority.declarations
        ):
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'stochastic portal execution requires two or more declared '
                'AcousticRegions connected by an explicit Portal list',
            )
    else:
        try:
            _validate_supported_topology(
                region_authority=region_authority,
                portal_authority=portal_authority,
                boundary_termination_authority=boundary_termination_authority,
            )
        except DeterministicGaUnsupportedError as error:
            reason = (
                'UNSUPPORTED_REGION_TOPOLOGY'
                if getattr(error, 'reason', None)
                == 'UNSUPPORTED_REGION_TOPOLOGY'
                else 'UNSUPPORTED_PORTAL_TOPOLOGY'
            )
            raise StochasticRayUnsupportedError(reason, str(error)) from error
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError(
            'stochastic authority rejects approximated/dropped R120 geometry'
        )
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError('stochastic authority requires exact R120 preservation')

    tolerance = execution_input.geometric_tolerance_m
    if portal_geometry:
        if (
            execution_input.portal_graph is None
            or not execution_input.portal_apertures
            or execution_input.region_declarations is None
            or any(
                item.acoustic_region_id is None
                for item in execution_input.sources
            )
            or any(
                item.acoustic_region_id is None
                for item in execution_input.receivers
            )
        ):
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_PORTAL_TOPOLOGY',
                'stochastic portal execution requires exact Portal graph, '
                'apertures, AcousticRegion declarations and region bindings',
            )

    mapping_by_surface = {
        item.source_surface_id: item for item in compiled_geometry.surface_mapping
    }
    region_triangle_indices_by_id: dict[str, tuple[int, ...]] = {}
    declarations = (
        execution_input.region_declarations
        if execution_input.region_declarations is not None
        else region_authority.declarations
    )
    for declaration in declarations:
        indices = tuple(
            sorted(
                {
                    index
                    for surface_id in declaration.boundary_surface_ids
                    for index in mapping_by_surface[
                        surface_id
                    ].compiled_triangle_indices
                }
            )
        )
        region_triangle_indices_by_id[declaration.region_id] = indices

    # Region-membership gates: every source and receiver must sit unambiguously
    # inside its declared AcousticRegion — a ray cast from a boundary or
    # exterior seed is not a qualified emission.
    def _require_inside_region(
        point: Sequence[float], region_id: str | None, label: str
    ) -> None:
        if region_id is None:
            if len(region_triangle_indices_by_id) != 1:
                raise StochasticRayUnsupportedError(
                    'UNSUPPORTED_REGION_MEMBERSHIP',
                    f'{label} is not bound to an AcousticRegion in a '
                    'multi-region execution input',
                )
            region_id = next(iter(region_triangle_indices_by_id))
        indices = region_triangle_indices_by_id.get(region_id)
        if indices is None:
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} is bound to undeclared AcousticRegion {region_id}',
            )
        triangles = tuple(
            _triangle_vertices(compiled_geometry, index) for index in indices
        )
        if portal_geometry:
            # An open Portal aperture is a hole in the declared boundary
            # shell; each region's membership test treats the aperture
            # polygon as part of its enclosing surface.
            apertures_for_membership = (
                execution_input.portal_apertures or ()
            )
            for aperture in apertures_for_membership:
                if region_id not in (
                    aperture.from_region_id,
                    aperture.to_region_id,
                ):
                    continue
                vertices = tuple(
                    _position_tuple(item)
                    for item in aperture.ordered_vertices_m
                )
                for vertex_index in range(1, len(vertices) - 1):
                    triangles += (
                        (
                            vertices[0],
                            vertices[vertex_index],
                            vertices[vertex_index + 1],
                        ),
                    )
        membership = _triangle_shell_point_membership(
            compiled_geometry,
            triangles,
            point,
            tolerance=tolerance,
        )
        if membership != 'inside':
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_REGION_MEMBERSHIP',
                f'{label} is not unambiguously inside AcousticRegion '
                f'{region_id} (membership={membership})',
            )

    # Capture-radius bounds: the radius must clear the geometric tolerance yet
    # stay a bounded fraction of the smallest scene extent.
    bounds = compiled_geometry.bounding_volume
    extent = min(
        float(bounds.max_x_m) - float(bounds.min_x_m),
        float(bounds.max_y_m) - float(bounds.min_y_m),
        float(bounds.max_z_m) - float(bounds.min_z_m),
    )
    radius = float(stochastic_policy.receiver_capture_radius_m)
    if (
        radius <= tolerance * _CAPTURE_RADIUS_MIN_FACTOR
        or radius > extent * _CAPTURE_RADIUS_MAX_EXTENT_FRACTION
    ):
        raise StochasticRayUnsupportedError(
            'CAPTURE_RADIUS_OUT_OF_BOUNDS',
            f'receiver capture radius {radius} must exceed '
            f'{_CAPTURE_RADIUS_MIN_FACTOR}× the geometric tolerance and stay '
            f'within {_CAPTURE_RADIUS_MAX_EXTENT_FRACTION} of the smallest '
            f'scene extent {extent}',
        )

    # Compiled-triangle interaction sets (world frame).
    boundary_rows: list[tuple[GeometricSurfacePlane, tuple]] = []
    for plane in execution_input.boundary_planes:
        if not plane.compiled_triangle_indices:
            raise StochasticRayUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'stochastic receiver estimation requires every boundary plane '
                'to carry exact compiled triangle indices',
            )
        for index in plane.compiled_triangle_indices:
            boundary_rows.append(
                (plane, _triangle_vertices(compiled_geometry, index))
            )
    occluder_rows = tuple(
        (
            compiled_geometry.triangles[index].source_surface_id,
            _triangle_vertices(compiled_geometry, index),
        )
        for index in execution_input.occluder_triangle_indices
    )
    apertures = (
        tuple(execution_input.portal_apertures)
        if execution_input.portal_apertures is not None
        else ()
    )

    diagonal = sqrt(
        (float(bounds.max_x_m) - float(bounds.min_x_m)) ** 2
        + (float(bounds.max_y_m) - float(bounds.min_y_m)) ** 2
        + (float(bounds.max_z_m) - float(bounds.min_z_m)) ** 2
    )
    escape_length = diagonal * _ESCAPE_LENGTH_FACTOR
    budgets = tuple(stochastic_policy.ray_budgets)
    max_budget = budgets[-1]
    centers = tuple(float(item) for item in execution_input.frequency_centers_hz)
    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}

    estimates: list[StochasticReceiverBandEstimate] = []
    rejected: list[StochasticEstimateRejectedCandidate] = []

    for source in execution_input.sources:
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                'stochastic execution missing exact DirectivityDataset for '
                f'{source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError(
                'stochastic execution DirectivityDataset identity mismatch'
            )
        source_world = _position_tuple(source.source_reference_point)
        _require_inside_region(
            source_world, source.acoustic_region_id, 'stochastic ray source'
        )
        for receiver in execution_input.receivers:
            _require_inside_region(
                _position_tuple(receiver.world_position),
                receiver.acoustic_region_id,
                'stochastic ray receiver',
            )
        receiver_points = [
            _position_tuple(item.world_position)
            for item in execution_input.receivers
        ]

        # Per-band contamination state: a band dies for this source (all
        # receivers) once one traced ray hits an unresolvable quantity for it.
        dead_bands: dict[float, tuple[StochasticEstimateDecision, str, str | None]] = {}

        # Accumulators keyed by (receiver_index, band): per-level sums.
        sums: dict[tuple[int, float], list[float]] = {}
        sums_sq: dict[tuple[int, float], list[float]] = {}
        captures: dict[tuple[int, float], list[int]] = {}

        for ray_index in range(max_budget):
            direction = _unit(_ray_direction(stochastic_policy.sampling_seed, ray_index))
            emission: dict[float, float] = {}
            for center in centers:
                if center in dead_bands:
                    continue
                directivity = _directivity_contribution(
                    dataset,
                    frequency_hz=center,
                    source_axis=source.source_axis,
                    departure_direction=direction,
                    tolerance=tolerance,
                )
                if directivity is None or not isfinite(
                    directivity.energy_factor
                ):
                    dead_bands[center] = (
                        'UNSUPPORTED_DIRECTIVITY',
                        'departure direction exceeds the exact DirectivityDataset '
                        'domain or produced a non-finite energy factor',
                        None,
                    )
                    continue
                emission[center] = directivity.energy_factor
            weights = dict(emission)

            # Receiver capture happens on the first qualifying leg; the
            # deposited weight is the per-band weight at that leg, before the
            # leg's terminal interaction applies.
            ray_captured: dict[int, dict[float, float]] = {}
            position = source_world
            ray_direction = direction
            for _interaction in range(stochastic_policy.maximum_bounces + 1):
                hit = _nearest_ray_hit(
                    position,
                    ray_direction,
                    boundary_rows,
                    occluder_rows,
                    tolerance=tolerance,
                )
                if hit is None:
                    leg_end = tuple(
                        float(position[index])
                        + escape_length * float(ray_direction[index])
                        for index in range(3)
                    )
                    leg_length = escape_length
                    hit_kind = 'escape'
                    hit_plane = None
                    hit_point = leg_end
                else:
                    leg_end = tuple(
                        float(position[index])
                        + hit.distance_m * float(ray_direction[index])
                        for index in range(3)
                    )
                    leg_length = hit.distance_m
                    hit_kind = hit.kind
                    hit_plane = hit.plane
                    hit_point = leg_end

                for receiver_index, receiver_point in enumerate(receiver_points):
                    if receiver_index in ray_captured:
                        continue
                    parameter = _segment_receiver_capture(
                        position,
                        ray_direction,
                        leg_length,
                        receiver_point,
                        capture_radius_m=radius,
                    )
                    if parameter is not None:
                        ray_captured[receiver_index] = dict(weights)

                if hit_kind == 'escape' or hit_kind == 'occluder':
                    break
                assert hit_plane is not None
                if (
                    _aperture_at_point(
                        apertures, hit_point, tolerance=tolerance
                    )
                    is not None
                ):
                    # Free transmission through the declared Portal aperture:
                    # direction unchanged, one interaction slot consumed.
                    position = hit_point
                    continue

                # Specular bounce: apply the banded scalar specular energy
                # factor per live band.
                if hit_plane.material_authority is None:
                    dead_surface_reason = (
                        'UNSUPPORTED_BOUNDARY_QUANTITY',
                        'boundary surface has no exact material authority',
                        hit_plane.source_surface_id,
                    )
                    for center in list(weights):
                        if center not in dead_bands:
                            dead_bands[center] = dead_surface_reason
                            del weights[center]
                else:
                    resolved_material = material_resolver(
                        hit_plane.material_authority
                    )
                    for center in list(weights):
                        contribution = (
                            None
                            if resolved_material is None
                            else _material_contribution(
                                resolved_material,
                                hit_plane,
                                frequency_hz=center,
                                tolerance=tolerance,
                            )
                        )
                        if contribution is None or not isfinite(
                            contribution.specular_energy_factor
                        ):
                            dead_bands[center] = (
                                'UNSUPPORTED_BOUNDARY_QUANTITY',
                                'boundary material authority is stale or does '
                                'not resolve the declared band quantity',
                                hit_plane.source_surface_id,
                            )
                            del weights[center]
                            continue
                        weights[center] = (
                            weights[center] * contribution.specular_energy_factor
                        )
                if not weights:
                    break
                ray_direction = _reflect(ray_direction, hit_plane)
                position = hit_point

            # Accumulate this ray into every level whose budget covers it.
            for level_index, budget in enumerate(budgets):
                if ray_index >= budget:
                    continue
                for receiver_index in range(len(receiver_points)):
                    captured_weights = ray_captured.get(receiver_index)
                    for center in centers:
                        if center in dead_bands or center not in emission:
                            continue
                        weight = (
                            captured_weights.get(center, 0.0)
                            if captured_weights is not None
                            else 0.0
                        )
                        value = weight
                        key = (receiver_index, center)
                        sums.setdefault(key, [0.0] * len(budgets))[
                            level_index
                        ] += value
                        sums_sq.setdefault(key, [0.0] * len(budgets))[
                            level_index
                        ] += value * value
                        if receiver_index in ray_captured:
                            captures.setdefault(key, [0] * len(budgets))[
                                level_index
                            ] += 1

        # Bands that died mid-run are rejected for every receiver.
        for receiver_index, receiver in enumerate(execution_input.receivers):
            for center in centers:
                rejection = dead_bands.get(center)
                key = (receiver_index, center)
                if rejection is not None:
                    rejected.append(
                        StochasticEstimateRejectedCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            center_hz=center,
                            interaction_surface_id=rejection[2],
                            decision=rejection[0],
                            reason=rejection[1],
                        )
                    )
                    continue
                level_rows: list[StochasticEstimateLevelEvidence] = []
                for level_index, budget in enumerate(budgets):
                    level_sums = sums.get(key)
                    estimate = (
                        4.0 * level_sums[level_index] / (radius * radius * budget)
                        if level_sums is not None
                        else 0.0
                    )
                    level_rows.append(
                        StochasticEstimateLevelEvidence(
                            ray_budget=budget,
                            captured_ray_count=(
                                captures.get(key, [0] * len(budgets))[level_index]
                            ),
                            estimate_per_m2=estimate,
                        )
                    )
                final_level = level_rows[-1]
                previous_level = level_rows[-2]
                deviation = abs(
                    final_level.estimate_per_m2 - previous_level.estimate_per_m2
                ) / max(
                    previous_level.estimate_per_m2,
                    stochastic_policy.convergence_floor_per_m2,
                )
                verdict: ConvergenceVerdict = (
                    'WITHIN_DECLARED_BOUND'
                    if deviation <= stochastic_policy.declared_convergence_bound
                    else 'OUTSIDE_DECLARED_BOUND'
                )
                n = budgets[-1]
                sum_x = sums.get(key, [0.0] * len(budgets))[-1]
                sum_x2 = sums_sq.get(key, [0.0] * len(budgets))[-1]
                mean = sum_x / n
                sample_variance = max(
                    0.0, (sum_x2 - n * mean * mean) / (n - 1)
                )
                standard_error = (4.0 / (radius * radius)) * sqrt(
                    sample_variance / n
                )
                core: dict[str, Any] = {
                    'source_entity_id': source.source_entity_id,
                    'receiver_id': receiver.receiver_id,
                    'center_hz': center,
                    'levels': [
                        item.model_dump(mode='json') for item in level_rows
                    ],
                    'estimate_per_m2': final_level.estimate_per_m2,
                    'standard_error_per_m2': standard_error,
                    'relative_deviation_from_previous_level': deviation,
                    'convergence_verdict': verdict,
                }
                digest = _semantic_hash(core)
                estimates.append(
                    StochasticReceiverBandEstimate(
                        estimate_id=(
                            f'stochastic-receiver-band-estimate:{digest}'
                        ),
                        semantic_sha256=digest,
                        **core,
                    )
                )

    estimates.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            item.center_hz,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            item.center_hz,
            item.interaction_surface_id or '',
            item.decision,
            item.reason,
        )
    )
    evaluable_verdicts = {item.convergence_verdict for item in estimates}
    convergence_status = (
        'NO_EVALUABLE_ESTIMATE'
        if not estimates
        else (
            'WITHIN_DECLARED_BOUND'
            if evaluable_verdicts == {'WITHIN_DECLARED_BOUND'}
            else 'OUTSIDE_DECLARED_BOUND'
        )
    )

    execution_payload: dict[str, Any] = {
        'execution_input_id': execution_input.execution_input_id,
        'solver_implementation_ref': execution_input.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'stochastic_policy_ref': stochastic_policy.as_external_ref().model_dump(
            mode='json'
        ),
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
    }
    execution_digest = _semantic_hash(execution_payload)
    artifact_core: dict[str, Any] = {
        'schema_version': STOCHASTIC_RAY_SCHEMA_VERSION,
        'authority_version': STOCHASTIC_RAY_AUTHORITY_VERSION,
        'execution_id': f'r150-stochastic-ray-execution:{execution_digest}',
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'prediction_request_sha256': execution_input.prediction_request_sha256,
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'dispatch_binding_sha256': execution_input.dispatch_binding_sha256,
        'adapter_descriptor_id': execution_input.adapter_descriptor_id,
        'adapter_descriptor_sha256': execution_input.adapter_descriptor_sha256,
        'solver_implementation_ref': execution_input.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': execution_input.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'stochastic_policy_ref': stochastic_policy.as_external_ref().model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': execution_input.r120_compiled_geometry_sha256,
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'stochastic_ray_implementation_ref': (
            engine.solver_implementation_ref.model_dump(mode='json')
        ),
        'candidate_source_commit': engine.candidate_source_commit,
        'numeric_comparison_tolerance_m': execution_input.geometric_tolerance_m,
        'identity_decimal_places': execution_input.identity_decimal_places,
        'frequency_domain': execution_input.frequency_domain.model_dump(
            mode='json'
        ),
        'estimation_scope': 'bounded_stochastic_ray_receiver_estimate_v1',
        'energy_semantics': 'monte_carlo_point_estimate_with_convergence_evidence',
        'coherent_phase_authority': 'NOT_APPLICABLE_ENERGY_DOMAIN',
        'sampling_seed': stochastic_policy.sampling_seed,
        'ray_budgets': list(budgets),
        'maximum_bounces': stochastic_policy.maximum_bounces,
        'receiver_capture_radius_m': radius,
        'declared_convergence_bound': stochastic_policy.declared_convergence_bound,
        'convergence_floor_per_m2': stochastic_policy.convergence_floor_per_m2,
        'capability_record': StochasticRayCapabilityRecord().model_dump(
            mode='json'
        ),
        'convergence_status': convergence_status,
        'estimates': [item.model_dump(mode='json') for item in estimates],
        'rejected_candidates': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if key != 'interaction_surface_id' or value is not None
            }
            for item in rejected
        ],
    }
    artifact_digest = _semantic_hash(artifact_core)
    return StochasticReceiverEstimateArtifact(
        artifact_id=f'stochastic-receiver-estimate-artifact:{artifact_digest}',
        semantic_sha256=artifact_digest,
        **artifact_core,
    )


def stochastic_receiver_estimate_observable_manifest(
    artifact: StochasticReceiverEstimateArtifact,
) -> AcousticSolverObservableArtifact:
    """Expose a persisted estimate artifact as the stochastic observable."""

    return AcousticSolverObservableArtifact(
        observable=STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE,
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )


def build_stochastic_receiver_estimate_result_envelope(
    *,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    artifact: StochasticReceiverEstimateArtifact,
    completed_at_utc: str,
    artifact_manifest_resolver=None,
) -> AcousticSolverResultEnvelope:
    """Bind the estimate artifact to its exact READY dispatch as a solver result.

    The request must declare the ``stochastic_receiver_estimate`` observable;
    the envelope authority enforces the exact observable-set match.
    """

    manifest = stochastic_receiver_estimate_observable_manifest(artifact)
    provenance = artifact.execution_provenance_ref()
    return build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id=artifact.execution_id,
        execution_provenance_ref=provenance,
        artifacts=(manifest,),
        completed_at_utc=completed_at_utc,
        artifact_manifest_resolver=(
            artifact_manifest_resolver
            if artifact_manifest_resolver is not None
            else lambda ref: (
                AcousticSolverArtifactManifest(
                    artifact_ref=ref,
                    observable=STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE,
                    encoding_schema_ref=STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF,
                    valid_frequency_domain=artifact.frequency_domain,
                    solver_lineage={
                        'execution_id': artifact.execution_id,
                        'execution_input_id': artifact.execution_input_id,
                        'execution_input_sha256': artifact.execution_input_sha256,
                        'dispatch_binding_id': artifact.dispatch_binding_id,
                        'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
                        'stochastic_policy_id': (
                            artifact.stochastic_policy_ref.authority_id
                        ),
                        'stochastic_policy_sha256': (
                            artifact.stochastic_policy_ref.semantic_hash_sha256
                        ),
                    },
                )
                if ref == artifact.as_external_ref()
                else None
            )
        ),
    )


StochasticPolicyResolver = Callable[
    [ExactExternalAuthorityRef], StochasticRayEstimationPolicy | None
]


class CadStochasticReceiverEstimateRepository:
    """Persisted estimate artifact store with full stale revalidation.

    Reopening re-resolves every exact authority the artifact binds — snapshot,
    prediction request, READY dispatch, adapter descriptor, GA configuration,
    stochastic policy, compiled geometry + topology, region/Portal/termination
    authorities — and regenerates the artifact deterministically from them.
    Any drift fails closed.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver: ConfigurationResolver,
        stochastic_policy_resolver: StochasticPolicyResolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
        material_resolver: MaterialAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.stochastic_policy_resolver = stochastic_policy_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.material_resolver = material_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'stochastic estimate and {label} repositories must share '
                    'one CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection, 'cad_stochastic_receiver_estimate_artifacts'
            )

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        item = self.geometry_authority_resolver(ref)
        if item is None or not isinstance(item, expected_type):
            raise ValueError(
                f'stochastic estimate exact {label} authority is missing'
            )
        if _authority_ref(item) != ref:
            raise ValueError(
                f'stochastic estimate exact {label} authority mismatch'
            )
        return item

    def _load_execution_input(
        self,
        artifact: StochasticReceiverEstimateArtifact,
    ) -> DeterministicGaExecutionInput:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (artifact.execution_input_id,),
            ).fetchone()
        if row is None:
            raise ValueError(
                'stochastic estimate exact GA execution input is missing'
            )
        execution_input = DeterministicGaExecutionInput.model_validate_json(
            row['payload_json']
        )
        if (
            execution_input.semantic_sha256 != artifact.execution_input_sha256
            or execution_input.snapshot_id != artifact.snapshot_id
            or execution_input.prediction_request_id
            != artifact.prediction_request_id
            or execution_input.dispatch_binding_id != artifact.dispatch_binding_id
            or execution_input.r120_compiled_geometry_id
            != artifact.r120_compiled_geometry_id
        ):
            raise ValueError(
                'stochastic estimate exact GA execution input is mismatched'
            )
        return execution_input

    def _validate(
        self, artifact: StochasticReceiverEstimateArtifact
    ) -> StochasticReceiverEstimateArtifact:
        artifact = StochasticReceiverEstimateArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        execution_input = self._load_execution_input(artifact)
        snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if (
            snapshot is None
            or snapshot.semantic_sha256 != artifact.snapshot_sha256
        ):
            raise ValueError(
                'stochastic estimate exact snapshot is missing or mismatched'
            )
        request = self.snapshot_repository.get_prediction_request(
            artifact.prediction_request_id,
            _validated_snapshot=snapshot,
        )
        if (
            request is None
            or request.request_semantic_sha256
            != artifact.prediction_request_sha256
        ):
            raise ValueError(
                'stochastic estimate exact prediction request is missing or '
                'mismatched'
            )
        dispatch = self.dispatch_repository.get_dispatch(
            artifact.dispatch_binding_id,
            _validated_snapshot=snapshot,
            _validated_request=request,
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != artifact.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError(
                'stochastic estimate exact READY dispatch is missing or '
                'mismatched'
            )
        descriptor = self.dispatch_repository.get_descriptor(
            artifact.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != artifact.adapter_descriptor_sha256
        ):
            raise ValueError(
                'stochastic estimate exact adapter descriptor is missing or '
                'mismatched'
            )
        if (
            artifact.solver_implementation_ref
            != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref
            != dispatch.solver_configuration_ref
        ):
            raise ValueError(
                'stochastic estimate solver implementation/config mismatch'
            )

        configuration = self.configuration_resolver(
            artifact.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref()
            != artifact.solver_configuration_ref
        ):
            raise ValueError(
                'stochastic estimate exact GA configuration is missing or '
                'mismatched'
            )
        policy = self.stochastic_policy_resolver(artifact.stochastic_policy_ref)
        if (
            policy is None
            or policy.as_external_ref() != artifact.stochastic_policy_ref
        ):
            raise ValueError(
                'stochastic estimate exact policy is missing or mismatched'
            )
        if (
            policy.sampling_seed != artifact.sampling_seed
            or tuple(policy.ray_budgets) != tuple(artifact.ray_budgets)
            or policy.maximum_bounces != artifact.maximum_bounces
            or policy.receiver_capture_radius_m
            != artifact.receiver_capture_radius_m
            or policy.declared_convergence_bound
            != artifact.declared_convergence_bound
            or policy.convergence_floor_per_m2
            != artifact.convergence_floor_per_m2
        ):
            raise ValueError(
                'stochastic estimate policy payload does not reproduce the '
                'resolved policy authority'
            )

        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            artifact.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError(
                'stochastic estimate exact R120 geometry is missing or '
                'mismatched'
            )
        if compiled.region_authority_ref is None:
            raise ValueError(
                'stochastic estimate R120 region authority is missing'
            )
        if compiled.portal_authority_ref is None:
            raise ValueError(
                'stochastic estimate R120 portal authority is missing'
            )
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError(
                'stochastic estimate R120 termination authority is missing'
            )
        region_authority = self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        portal_authority = self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        termination_authority = self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )

        datasets: list[DirectivityDataset] = []
        for source_input in execution_input.sources:
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source_input.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError(
                    'stochastic estimate exact DirectivityDataset is missing'
                )
            datasets.append(dataset)

        regenerated = execute_stochastic_ray_receiver_estimate(
            execution_input=execution_input,
            stochastic_policy=policy,
            compiled_geometry=compiled,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=termination_authority,
            directivity_datasets=datasets,
            material_resolver=self.material_resolver,
        )
        if regenerated != artifact:
            raise ValueError(
                'stochastic estimate does not reproduce from exact current '
                'authorities'
            )
        return artifact

    def save(
        self, artifact: StochasticReceiverEstimateArtifact
    ) -> StochasticReceiverEstimateArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_stochastic_receiver_estimate_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = (
                    StochasticReceiverEstimateArtifact.model_validate_json(
                        existing['payload_json']
                    )
                )
                if persisted != artifact:
                    raise ValueError(
                        'stochastic estimate artifact id exists with different '
                        'semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_stochastic_receiver_estimate_artifacts(
                    artifact_id,
                    semantic_sha256,
                    execution_id,
                    execution_provenance_authority_id,
                    execution_input_id,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.execution_id,
                    provenance.authority_id,
                    artifact.execution_input_id,
                    artifact.snapshot_id,
                    artifact.prediction_request_id,
                    artifact.dispatch_binding_id,
                    artifact.r120_compiled_geometry_id,
                    artifact.model_dump_json(),
                    _utc_now(),
                ),
            )
        return artifact

    def get(
        self, artifact_id: str
    ) -> StochasticReceiverEstimateArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_stochastic_receiver_estimate_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            StochasticReceiverEstimateArtifact.model_validate_json(
                row['payload_json']
            )
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith(
            'stochastic-receiver-estimate-artifact:'
        ):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith(
            'r150-stochastic-ray-execution-provenance:'
        ):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_stochastic_receiver_estimate_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                StochasticReceiverEstimateArtifact.model_validate_json(
                    row['payload_json']
                )
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None

    def resolve_artifact_manifest(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverArtifactManifest | None:
        """Resolve the persisted estimate artifact as a typed solver manifest."""

        if not ref.authority_id.startswith(
            'stochastic-receiver-estimate-artifact:'
        ):
            return None
        artifact = self.get(ref.authority_id)
        if artifact is None or artifact.as_external_ref() != ref:
            return None
        return AcousticSolverArtifactManifest(
            artifact_ref=ref,
            observable=STOCHASTIC_RECEIVER_ESTIMATE_OBSERVABLE,
            encoding_schema_ref=STOCHASTIC_RECEIVER_ESTIMATE_SCHEMA_REF,
            valid_frequency_domain=artifact.frequency_domain,
            solver_lineage={
                'execution_id': artifact.execution_id,
                'execution_input_id': artifact.execution_input_id,
                'execution_input_sha256': artifact.execution_input_sha256,
                'dispatch_binding_id': artifact.dispatch_binding_id,
                'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
                'stochastic_policy_id': (
                    artifact.stochastic_policy_ref.authority_id
                ),
                'stochastic_policy_sha256': (
                    artifact.stochastic_policy_ref.semantic_hash_sha256
                ),
            },
        )
