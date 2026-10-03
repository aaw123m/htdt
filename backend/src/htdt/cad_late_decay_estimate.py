"""R150 bounded analytic late-decay estimation lane.

This module implements the next declared R150 sub-item: an energy-decay-curve
decay-time estimation engine that derives per-band late-field decay constants
from the exact compiled geometry and material authorities — the
``analytic_estimate`` provenance hook of ``LateFieldBandDecay``.

The estimator is the canonical Sabine diffuse-field relation evaluated against
the sole closed AcousticRegion of a ``general_planar_closed_polyhedral_v1`` or
``exact_axis_aligned_closed_shoebox_v1`` execution input:

    T60(f) = 55.3 * V / (c * A(f)),
    A(f)   = sum over room-boundary surfaces S_i * alpha_i(f)

where ``V`` is the exact room-boundary shell volume of the compiled R120
geometry (recomputed with the same divergence-theorem routine the GA adapter
uses), ``S_i`` are the exact per-surface areas of the compiled triangles, and
``alpha_i(f)`` resolves only through declared banded material authorities.

The estimate is region-level evidence: it carries no source/receiver
dependence, never synthesizes phase, and never claims a measured or declared
law. It emits a ``LateDecayEstimateArtifact`` (content-addressed, persisted in
``cad_late_decay_estimate_artifacts``) whose ``late_decay_estimate_decay_law``
helper derives the ``LateEnergyDecayLaw`` the R160 late-energy solver consumes.

Fail-closed rules (all raise, never degrade):

- the engine must be the registered ``htdt.r150.late_decay_estimate`` kernel;
- the execution input must reproduce the exact compiled-geometry identity and
  the exact region/Portal/boundary-termination authorities;
- geometry policy must be one of the two closed single-region policies, the
  authority must declare exactly one AcousticRegion, and that region must be
  the exact room-boundary shell (closed manifold, exact preservation);
- object/interior surfaces are outside the declared model — their presence
  fails closed instead of silently under-counting volume/absorption.

Per-band rejections (recorded, not raised) cover boundary surfaces whose
material authority is missing or does not resolve the declared band —
``UNSUPPORTED_BOUNDARY_QUANTITY`` — since a Sabine sum without every boundary
term is fabrication.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from math import isfinite
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from .cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from .cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_adapter import (
    DeterministicGaExecutionInput,
    DeterministicGaUnsupportedError,
    GeometryAuthorityResolver,
    MaterialAuthorityResolver,
    _authority_ref,
    _cross,
    _material_contribution,
    _norm,
    _require_supported_feature_scale,
    _room_boundary_shell_metrics,
    _triangle_vertices,
    _validate_supported_topology,
    _vector,
)
from .cad_hybrid_late_energy import (
    LateEnergyDecayLaw,
    LateFieldBandDecay,
    build_late_energy_decay_law,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .canonical_json import canonical_sha256 as _semantic_hash
from .clock import utc_now_iso as _utc_now
from .r120_geometry_compiler import (
    AcousticRegionAuthority,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
)


LATE_DECAY_ESTIMATE_SCHEMA_VERSION = 1
LATE_DECAY_ESTIMATE_AUTHORITY_VERSION = 'r150-late-decay-estimate-1'

LATE_DECAY_ESTIMATE_ENGINE_ID = 'htdt.r150.late_decay_estimate'
LATE_DECAY_ESTIMATE_ENGINE_VERSION = '1'

LATE_DECAY_ESTIMATE_OBSERVABLE = 'late_decay_estimate'

LATE_DECAY_ESTIMATE_DECAY_MODEL = 'sabine_single_region_v1'
LATE_DECAY_ESTIMATE_SCOPE = 'single_region_analytic_sabine_decay_estimate_v1'
SABINE_DECAY_CONSTANT = 55.3

HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-late-decay-estimate',
    authority_version=LATE_DECAY_ESTIMATE_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': LATE_DECAY_ESTIMATE_ENGINE_ID,
            'version': LATE_DECAY_ESTIMATE_ENGINE_VERSION,
            'construction': (
                'bounded_single_region_analytic_sabine_decay_estimate'
            ),
            'decay_model': LATE_DECAY_ESTIMATE_DECAY_MODEL,
            'sabine_constant': SABINE_DECAY_CONSTANT,
            'region_topology': 'exactly_one_closed_acoustic_region',
            'boundary_quantity': 'banded_scalar_ga_absorption_fraction',
            'coherent_phase': 'not_applicable_energy_domain',
            'energy_semantics': 'analytic_estimate_not_measured',
        }
    ),
)

LATE_DECAY_ESTIMATE_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.late-decay-estimate-artifact.schema',
    authority_version=LATE_DECAY_ESTIMATE_AUTHORITY_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'LateDecayEstimateArtifact',
            'schema_version': LATE_DECAY_ESTIMATE_SCHEMA_VERSION,
            'quantity': 'late_decay_time_s',
            'coherent_phase': 'not_applicable_energy_domain',
        }
    ),
)


class HtdtLateDecayEstimateEngine:
    """Exact engine marker for the bounded analytic late-decay estimator."""

    engine_id = LATE_DECAY_ESTIMATE_ENGINE_ID
    engine_version = LATE_DECAY_ESTIMATE_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF
    decay_model = LATE_DECAY_ESTIMATE_DECAY_MODEL
    coherent_phase = 'not_applicable_energy_domain'
    energy_semantics = 'analytic_estimate_not_measured'


class LateDecayEstimateCapabilityRecord(BaseModel):
    """Explicit bounded-capability disclosure persisted inside the artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    supported_geometry_policies: tuple[str, ...] = (
        'exact_axis_aligned_closed_shoebox_v1',
        'general_planar_closed_polyhedral_v1',
    )
    decay_model: Literal['sabine_single_region_v1'] = (
        LATE_DECAY_ESTIMATE_DECAY_MODEL
    )
    sabine_constant: Literal[55.3] = SABINE_DECAY_CONSTANT
    boundary_quantity: Literal['banded_scalar_ga_absorption_fraction'] = (
        'banded_scalar_ga_absorption_fraction'
    )
    energy_semantics: Literal['analytic_estimate_not_measured'] = (
        'analytic_estimate_not_measured'
    )
    coherent_phase: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    unsupported_capabilities: tuple[str, ...] = (
        'multi-region Portal geometry policy '
        '(general_planar_multi_region_portal_v1) — the analytic estimate '
        'requires exactly one closed AcousticRegion',
        'interior/object surfaces — the declared model is the empty-region '
        'Sabine shell; unmodeled interior geometry fails closed',
        'non-banded or unresolved material authorities — every room-boundary '
        'surface must resolve the declared band; otherwise the band is '
        'rejected rather than partially summed',
        'measured or declared decay laws — the estimate carries only the '
        'analytic_estimate provenance',
        'coherent phase — decay-time estimates are energy-domain results '
        'and carry no phase authority',
        'frequency-independent (Sabine) mixing — seat/microphone resolved '
        'energy decay curves are the R160 solver\'s scope, not this lane',
    )


class LateDecaySurfaceContribution(BaseModel):
    """Exact per-surface absorption-area contribution of one band estimate."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(min_length=1)
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef | None = None
    surface_area_m2: float = Field(ge=0.0)
    absorption: float = Field(ge=0.0, le=1.0)
    absorption_area_m2: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite(self) -> 'LateDecaySurfaceContribution':
        if not all(
            isfinite(item)
            for item in (
                float(self.surface_area_m2),
                float(self.absorption),
                float(self.absorption_area_m2),
            )
        ):
            raise ValueError(
                'late-decay surface contribution must be finite'
            )
        return self


class LateDecayBandEstimate(BaseModel):
    """Analytic decay-time estimate for one declared band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    center_hz: float = Field(gt=0.0)
    total_absorption_area_m2: float = Field(gt=0.0)
    decay_time_s: float = Field(gt=0.0)
    provenance: Literal['analytic_estimate'] = 'analytic_estimate'
    surface_contributions: tuple[LateDecaySurfaceContribution, ...]

    @model_validator(mode='after')
    def ordered(self) -> 'LateDecayBandEstimate':
        surface_ids = [
            item.source_surface_id for item in self.surface_contributions
        ]
        if surface_ids != sorted(set(surface_ids)):
            raise ValueError(
                'late-decay band surface contributions must be unique/sorted'
            )
        if not all(
            isfinite(item)
            for item in (
                float(self.total_absorption_area_m2),
                float(self.decay_time_s),
            )
        ):
            raise ValueError('late-decay band estimate must be finite')
        return self


LateDecayRejectedBandDecision = Literal['UNSUPPORTED_BOUNDARY_QUANTITY']


class LateDecayRejectedBand(BaseModel):
    """Typed rejection record for a band that could not be evaluated."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    center_hz: float = Field(gt=0.0)
    decision: LateDecayRejectedBandDecision
    unresolved_surface_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def ordered(self) -> 'LateDecayRejectedBand':
        surface_ids = list(self.unresolved_surface_ids)
        if surface_ids != sorted(set(surface_ids)):
            raise ValueError(
                'late-decay rejected band surfaces must be unique/sorted'
            )
        return self


class LateDecayEstimateArtifact(BaseModel):
    """Immutable analytic late-decay estimate authority for R160 consumption."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LATE_DECAY_ESTIMATE_SCHEMA_VERSION
    authority_version: Literal['r150-late-decay-estimate-1'] = (
        LATE_DECAY_ESTIMATE_AUTHORITY_VERSION
    )
    artifact_id: str = Field(
        pattern=r'^late-decay-estimate-artifact:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(
        pattern=r'^r150-late-decay-estimate-execution:[0-9a-f]{64}$'
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
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    engine_id: str
    engine_version: str
    late_decay_implementation_ref: ExactExternalAuthorityRef
    candidate_source_commit: str | None = None
    identity_decimal_places: int = Field(ge=6, le=15)
    frequency_domain: FrequencyDomain
    estimation_scope: Literal[
        'single_region_analytic_sabine_decay_estimate_v1'
    ] = LATE_DECAY_ESTIMATE_SCOPE
    decay_model: Literal['sabine_single_region_v1'] = (
        LATE_DECAY_ESTIMATE_DECAY_MODEL
    )
    sabine_constant: Literal[55.3] = SABINE_DECAY_CONSTANT
    region_id: str = Field(min_length=1)
    region_volume_m3: float = Field(gt=0.0)
    sound_speed_m_s: float = Field(gt=0.0)
    estimation_status: Literal['EVALUATED', 'NO_EVALUABLE_ESTIMATE']
    energy_semantics: Literal['analytic_estimate_not_measured'] = (
        'analytic_estimate_not_measured'
    )
    coherent_phase_authority: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    capability_record: LateDecayEstimateCapabilityRecord
    estimates: tuple[LateDecayBandEstimate, ...]
    rejected_candidates: tuple[LateDecayRejectedBand, ...]

    @model_validator(mode='after')
    def validate_artifact(self) -> 'LateDecayEstimateArtifact':
        estimate_centers = [item.center_hz for item in self.estimates]
        if estimate_centers != sorted(set(estimate_centers)):
            raise ValueError(
                'late-decay estimate bands must be unique/sorted by center_hz'
            )
        rejected_order = [
            (
                item.center_hz,
                item.decision,
                item.unresolved_surface_ids,
                item.reason,
            )
            for item in self.rejected_candidates
        ]
        if rejected_order != sorted(rejected_order):
            raise ValueError(
                'late-decay rejected bands must use canonical ordering'
            )
        if set(estimate_centers) & {
            item.center_hz for item in self.rejected_candidates
        }:
            raise ValueError(
                'late-decay band cannot be both estimated and rejected'
            )
        if self.estimation_status == 'EVALUATED':
            if not self.estimates:
                raise ValueError(
                    'evaluated late-decay estimate requires band estimates'
                )
        elif self.estimates:
            raise ValueError(
                'no-evaluable-estimate status cannot carry band estimates'
            )
        if not isfinite(float(self.region_volume_m3)):
            raise ValueError('late-decay region volume must be finite')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('LateDecayEstimateArtifact semantic hash mismatch')
        if self.artifact_id != f'late-decay-estimate-artifact:{expected}':
            raise ValueError('LateDecayEstimateArtifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )

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
            'engine_id': self.engine_id,
            'engine_version': self.engine_version,
            'late_decay_implementation_ref': (
                self.late_decay_implementation_ref.model_dump(mode='json')
            ),
            'candidate_source_commit': self.candidate_source_commit,
        }
        digest = _semantic_hash(payload)
        return ExactExternalAuthorityRef(
            authority_id=f'r150-late-decay-estimate-execution-provenance:{digest}',
            authority_version=self.authority_version,
            semantic_hash_sha256=digest,
        )


def _surface_area_m2(
    compiled: R120CompiledGeometry,
    triangle_indices: Sequence[int],
) -> float:
    area = 0.0
    for index in triangle_indices:
        a, b, c = _triangle_vertices(compiled, index)
        area += _norm(_cross(_vector(a, b), _vector(a, c))) / 2.0
    return area


def execute_late_decay_estimate(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    material_resolver: MaterialAuthorityResolver,
    engine: HtdtLateDecayEstimateEngine | None = None,
) -> LateDecayEstimateArtifact:
    """Evaluate the bounded analytic late-decay estimate, fail closed."""
    if engine is None:
        engine = HtdtLateDecayEstimateEngine()
    if (
        engine.engine_id != LATE_DECAY_ESTIMATE_ENGINE_ID
        or engine.engine_version != LATE_DECAY_ESTIMATE_ENGINE_VERSION
        or engine.solver_implementation_ref
        != HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF
    ):
        raise ValueError(
            'late-decay estimate engine does not reproduce the registered '
            'exact implementation authority'
        )
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
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
            'late-decay estimate compiled geometry exact identity mismatch'
        )
    if (
        _authority_ref(region_authority) != execution_input.region_authority_ref
        or _authority_ref(portal_authority) != execution_input.portal_authority_ref
        or _authority_ref(boundary_termination_authority)
        != execution_input.boundary_termination_authority_ref
    ):
        raise ValueError(
            'late-decay estimate exact geometry authority mismatch'
        )

    if execution_input.geometry_policy == 'general_planar_multi_region_portal_v1':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate authority supports exactly one closed '
            'AcousticRegion; the multi-region Portal policy is unsupported',
        )
    if execution_input.geometry_policy is not None and (
        execution_input.geometry_policy != 'general_planar_closed_polyhedral_v1'
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate authority does not support geometry policy '
            f'{execution_input.geometry_policy!r}',
        )
    _require_supported_feature_scale(compiled_geometry)
    _validate_supported_topology(
        region_authority=region_authority,
        portal_authority=portal_authority,
        boundary_termination_authority=boundary_termination_authority,
    )
    if len(region_authority.declarations) != 1:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate authority requires exactly one closed '
            'AcousticRegion',
        )
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError(
            'late-decay estimate rejects approximated/dropped R120 geometry'
        )
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError(
            'late-decay estimate requires exact R120 preservation'
        )

    tolerance = execution_input.geometric_tolerance_m
    general_geometry = (
        execution_input.geometry_policy == 'general_planar_closed_polyhedral_v1'
    )
    region = region_authority.declarations[0]
    region_surfaces = set(region.boundary_surface_ids)
    mapping_by_surface = {
        item.source_surface_id: item
        for item in compiled_geometry.surface_mapping
    }
    if any(item not in mapping_by_surface for item in region_surfaces):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate region declares surfaces outside the exact '
            'compiled geometry',
        )
    room_boundary_ids = {
        item.source_surface_id
        for item in compiled_geometry.surface_mapping
        if item.semantic_class == 'room_boundary'
    }
    if region_surfaces != room_boundary_ids:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'explicit acoustic region boundary surfaces do not match exact '
            'room-boundary shell',
        )
    interior_surface_ids = sorted(
        item.source_surface_id
        for item in compiled_geometry.surface_mapping
        if item.semantic_class != 'room_boundary'
    )
    if interior_surface_ids:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate is the empty-region Sabine shell; interior '
            'object surfaces are outside the declared model: '
            + ', '.join(interior_surface_ids),
        )

    if general_geometry:
        region_mappings = tuple(
            mapping_by_surface[surface_id]
            for surface_id in sorted(region_surfaces)
        )
        (
            shell_boundary_edges,
            shell_non_manifold_edges,
            region_volume_m3,
        ) = _room_boundary_shell_metrics(compiled_geometry, region_mappings)
        if shell_boundary_edges or shell_non_manifold_edges:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'late-decay estimate requires the exact region room-boundary '
                'subset to be a closed manifold; holes/open edges are not '
                'filled',
            )
    else:
        bounds = compiled_geometry.bounding_volume
        region_volume_m3 = (
            (float(bounds.max_x_m) - float(bounds.min_x_m))
            * (float(bounds.max_y_m) - float(bounds.min_y_m))
            * (float(bounds.max_z_m) - float(bounds.min_z_m))
        )
    if not isfinite(region_volume_m3) or region_volume_m3 <= tolerance**3:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate region shell does not enclose a positive '
            'finite volume',
        )

    plane_by_surface = {
        item.source_surface_id: item for item in execution_input.boundary_planes
    }
    if any(
        surface_id not in plane_by_surface
        for surface_id in region_surfaces
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-decay estimate requires every region boundary surface to '
            'resolve an exact plane authority',
        )
    for plane in execution_input.boundary_planes:
        if plane.source_surface_id not in region_surfaces:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'late-decay estimate evaluates room-boundary surfaces only; '
                f'{plane.source_surface_id} is outside the explicit region',
            )

    surface_area_by_id = {
        surface_id: _surface_area_m2(
            compiled_geometry,
            mapping_by_surface[surface_id].compiled_triangle_indices,
        )
        for surface_id in sorted(region_surfaces)
    }
    resolved_materials = {}
    for surface_id in sorted(region_surfaces):
        plane = plane_by_surface[surface_id]
        if plane.material_authority is None:
            continue
        resolved = material_resolver(plane.material_authority)
        if resolved is None or resolved.authority_ref != plane.material_authority:
            continue
        resolved_materials[surface_id] = resolved

    sound_speed = float(execution_input.sound_speed_m_s)
    estimates: list[LateDecayBandEstimate] = []
    rejected_bands: list[LateDecayRejectedBand] = []
    for frequency_hz in sorted(
        {float(item) for item in execution_input.frequency_centers_hz}
    ):
        contributions: list[LateDecaySurfaceContribution] = []
        unresolved: list[str] = []
        for surface_id in sorted(region_surfaces):
            plane = plane_by_surface[surface_id]
            resolved = resolved_materials.get(surface_id)
            band_contribution = (
                None
                if resolved is None or plane.material_authority is None
                else _material_contribution(
                    resolved,
                    plane,
                    frequency_hz=frequency_hz,
                    tolerance=tolerance,
                )
            )
            if band_contribution is None or not isfinite(
                float(band_contribution.absorption)
            ):
                unresolved.append(surface_id)
                continue
            absorption_area = (
                surface_area_by_id[surface_id]
                * float(band_contribution.absorption)
            )
            contributions.append(
                LateDecaySurfaceContribution(
                    source_surface_id=surface_id,
                    material_authority=band_contribution.material_authority,
                    boundary_physics_authority=(
                        band_contribution.boundary_physics_authority
                    ),
                    surface_area_m2=surface_area_by_id[surface_id],
                    absorption=band_contribution.absorption,
                    absorption_area_m2=absorption_area,
                )
            )
        if unresolved:
            rejected_bands.append(
                LateDecayRejectedBand(
                    center_hz=frequency_hz,
                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                    unresolved_surface_ids=tuple(unresolved),
                    reason=(
                        'one or more room-boundary surfaces lack an exact '
                        'banded absorption authority at this band; a partial '
                        'Sabine sum is not fabricated'
                    ),
                )
            )
            continue
        total_absorption_area = sum(
            item.absorption_area_m2 for item in contributions
        )
        if not isfinite(total_absorption_area) or total_absorption_area <= 0.0:
            rejected_bands.append(
                LateDecayRejectedBand(
                    center_hz=frequency_hz,
                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                    unresolved_surface_ids=tuple(sorted(region_surfaces)),
                    reason=(
                        'zero total absorption area yields no finite decay '
                        'time; the estimate is not fabricated'
                    ),
                )
            )
            continue
        decay_time_s = (
            SABINE_DECAY_CONSTANT
            * region_volume_m3
            / (sound_speed * total_absorption_area)
        )
        if not isfinite(decay_time_s) or decay_time_s <= 0.0:
            rejected_bands.append(
                LateDecayRejectedBand(
                    center_hz=frequency_hz,
                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                    unresolved_surface_ids=tuple(sorted(region_surfaces)),
                    reason=(
                        'analytic decay time is not finitely representable '
                        'under the declared authorities'
                    ),
                )
            )
            continue
        estimates.append(
            LateDecayBandEstimate(
                center_hz=frequency_hz,
                total_absorption_area_m2=total_absorption_area,
                decay_time_s=decay_time_s,
                surface_contributions=tuple(contributions),
            )
        )

    estimates_sorted = tuple(
        sorted(estimates, key=lambda item: item.center_hz)
    )
    rejected_sorted = tuple(
        sorted(
            rejected_bands,
            key=lambda item: (
                item.center_hz,
                item.decision,
                item.unresolved_surface_ids,
                item.reason,
            ),
        )
    )
    execution_payload = {
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'estimation_scope': LATE_DECAY_ESTIMATE_SCOPE,
        'region_id': region.region_id,
    }
    execution_digest = _semantic_hash(execution_payload)
    artifact_core: dict[str, object] = {
        'schema_version': LATE_DECAY_ESTIMATE_SCHEMA_VERSION,
        'authority_version': LATE_DECAY_ESTIMATE_AUTHORITY_VERSION,
        'execution_id': f'r150-late-decay-estimate-execution:{execution_digest}',
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'prediction_request_sha256': (
            execution_input.prediction_request_sha256
        ),
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'dispatch_binding_sha256': execution_input.dispatch_binding_sha256,
        'adapter_descriptor_id': execution_input.adapter_descriptor_id,
        'adapter_descriptor_sha256': (
            execution_input.adapter_descriptor_sha256
        ),
        'solver_implementation_ref': (
            execution_input.solver_implementation_ref.model_dump(mode='json')
        ),
        'solver_configuration_ref': (
            execution_input.solver_configuration_ref.model_dump(mode='json')
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': (
            execution_input.r120_compiled_geometry_sha256
        ),
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'late_decay_implementation_ref': (
            HTDT_LATE_DECAY_ESTIMATE_IMPLEMENTATION_REF.model_dump(mode='json')
        ),
        'candidate_source_commit': engine.candidate_source_commit,
        'identity_decimal_places': execution_input.identity_decimal_places,
        'frequency_domain': execution_input.frequency_domain.model_dump(
            mode='json'
        ),
        'estimation_scope': LATE_DECAY_ESTIMATE_SCOPE,
        'decay_model': LATE_DECAY_ESTIMATE_DECAY_MODEL,
        'sabine_constant': SABINE_DECAY_CONSTANT,
        'region_id': region.region_id,
        'region_volume_m3': region_volume_m3,
        'sound_speed_m_s': sound_speed,
        'estimation_status': (
            'EVALUATED' if estimates_sorted else 'NO_EVALUABLE_ESTIMATE'
        ),
        'energy_semantics': 'analytic_estimate_not_measured',
        'coherent_phase_authority': 'NOT_APPLICABLE_ENERGY_DOMAIN',
        'capability_record': LateDecayEstimateCapabilityRecord().model_dump(
            mode='json'
        ),
        'estimates': [
            item.model_dump(mode='json') for item in estimates_sorted
        ],
        'rejected_candidates': [
            item.model_dump(mode='json') for item in rejected_sorted
        ],
    }
    artifact_digest = _semantic_hash(artifact_core)
    return LateDecayEstimateArtifact(
        artifact_id=f'late-decay-estimate-artifact:{artifact_digest}',
        semantic_sha256=artifact_digest,
        **artifact_core,
    )


def late_decay_estimate_decay_law(
    artifact: LateDecayEstimateArtifact,
    *,
    decay_time_grid_s: Sequence[float],
    evidence_ref: ExactExternalAuthorityRef | None = None,
) -> LateEnergyDecayLaw:
    """Derive the declared R160 decay law from a persisted estimate artifact.

    The estimate supplies per-band analytic decay times with
    ``analytic_estimate`` provenance; the caller supplies the absolute decay
    time grid (which only the late-field composition knows — the law's first
    grid point must sit at or after the last deterministic arrival). The
    law's evidence binds the exact estimate artifact unless the caller passes
    a stronger authority.

    Fails closed when the artifact carries no evaluated bands.
    """
    artifact = LateDecayEstimateArtifact.model_validate(
        artifact.model_dump(mode='python')
    )
    if not artifact.estimates:
        raise ValueError(
            'late-decay estimate artifact has no evaluated bands; no '
            'decay law is derived'
        )
    return build_late_energy_decay_law(
        bands=tuple(
            LateFieldBandDecay(
                center_hz=item.center_hz,
                decay_time_s=item.decay_time_s,
                provenance='analytic_estimate',
            )
            for item in artifact.estimates
        ),
        decay_time_grid_s=decay_time_grid_s,
        evidence_ref=(
            evidence_ref
            if evidence_ref is not None
            else artifact.as_external_ref()
        ),
        rationale=(
            'analytic Sabine late-decay estimate over the exact single '
            'closed acoustic region (empty-region boundary absorption; '
            'energy-domain, no coherent phase)'
        ),
    )


def late_decay_estimate_observable_manifest(
    artifact: LateDecayEstimateArtifact,
) -> AcousticSolverObservableArtifact:
    """Expose a persisted estimate artifact as the late-decay observable."""
    return AcousticSolverObservableArtifact(
        observable=LATE_DECAY_ESTIMATE_OBSERVABLE,
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=LATE_DECAY_ESTIMATE_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )


def build_late_decay_estimate_result_envelope(
    *,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    artifact: LateDecayEstimateArtifact,
    completed_at_utc: str,
    artifact_manifest_resolver=None,
) -> AcousticSolverResultEnvelope:
    """Bind the estimate artifact to its exact READY dispatch as a solver result.

    The request must declare the ``late_decay_estimate`` observable; the
    envelope authority enforces the exact observable-set match.
    """
    manifest = late_decay_estimate_observable_manifest(artifact)
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
                    observable=LATE_DECAY_ESTIMATE_OBSERVABLE,
                    encoding_schema_ref=LATE_DECAY_ESTIMATE_SCHEMA_REF,
                    valid_frequency_domain=artifact.frequency_domain,
                    solver_lineage={
                        'execution_id': artifact.execution_id,
                        'execution_input_id': artifact.execution_input_id,
                        'execution_input_sha256': artifact.execution_input_sha256,
                        'dispatch_binding_id': artifact.dispatch_binding_id,
                        'dispatch_binding_sha256': (
                            artifact.dispatch_binding_sha256
                        ),
                    },
                )
                if ref == artifact.as_external_ref()
                else None
            )
        ),
    )


class CadLateDecayEstimateRepository:
    """Persisted late-decay estimate store with full stale revalidation.

    Reopening re-resolves every exact authority the artifact binds — snapshot,
    prediction request, READY dispatch, adapter descriptor, GA configuration,
    compiled geometry + topology, region/Portal/termination authorities — and
    regenerates the estimate deterministically from them. Any drift fails
    closed.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
        material_resolver: MaterialAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.material_resolver = material_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'late-decay estimate and {label} repositories must share '
                    'one CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_late_decay_estimate_artifacts')

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        item = self.geometry_authority_resolver(ref)
        if item is None or not isinstance(item, expected_type):
            raise ValueError(
                f'late-decay estimate exact {label} authority is missing'
            )
        if _authority_ref(item) != ref:
            raise ValueError(
                f'late-decay estimate exact {label} authority mismatch'
            )
        return item

    def _validate(
        self,
        artifact: LateDecayEstimateArtifact,
    ) -> LateDecayEstimateArtifact:
        artifact = LateDecayEstimateArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        execution_input = self._load_execution_input(artifact)
        snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if snapshot is None or snapshot.semantic_sha256 != artifact.snapshot_sha256:
            raise ValueError(
                'late-decay estimate exact snapshot is missing or mismatched'
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
                'late-decay estimate exact prediction request is missing or '
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
                'late-decay estimate exact READY dispatch is missing or '
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
                'late-decay estimate exact adapter descriptor is missing or '
                'mismatched'
            )
        if (
            artifact.solver_implementation_ref
            != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref
            != dispatch.solver_configuration_ref
        ):
            raise ValueError(
                'late-decay estimate solver implementation/config mismatch'
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
                'late-decay estimate exact GA configuration is missing or '
                'mismatched'
            )

        compiled = (
            self.snapshot_repository.r120_repository.get_compiled_geometry(
                artifact.r120_compiled_geometry_id
            )
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id
            != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError(
                'late-decay estimate exact R120 geometry is missing or '
                'mismatched'
            )
        if compiled.region_authority_ref is None:
            raise ValueError(
                'late-decay estimate R120 region authority is missing'
            )
        if compiled.portal_authority_ref is None:
            raise ValueError(
                'late-decay estimate R120 portal authority is missing'
            )
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError(
                'late-decay estimate R120 termination authority is missing'
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

        regenerated = execute_late_decay_estimate(
            execution_input=execution_input,
            compiled_geometry=compiled,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=termination_authority,
            material_resolver=self.material_resolver,
        )
        if regenerated != artifact:
            raise ValueError(
                'late-decay estimate does not reproduce from exact current '
                'authorities'
            )
        return artifact

    def _load_execution_input(
        self,
        artifact: LateDecayEstimateArtifact,
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
                'late-decay estimate exact GA execution input is missing'
            )
        execution_input = DeterministicGaExecutionInput.model_validate_json(
            row['payload_json']
        )
        if (
            execution_input.semantic_sha256 != artifact.execution_input_sha256
            or execution_input.snapshot_id != artifact.snapshot_id
            or execution_input.prediction_request_id
            != artifact.prediction_request_id
            or execution_input.dispatch_binding_id
            != artifact.dispatch_binding_id
            or execution_input.r120_compiled_geometry_id
            != artifact.r120_compiled_geometry_id
        ):
            raise ValueError(
                'late-decay estimate exact GA execution input is mismatched'
            )
        return execution_input

    def save(
        self,
        artifact: LateDecayEstimateArtifact,
    ) -> LateDecayEstimateArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_late_decay_estimate_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = LateDecayEstimateArtifact.model_validate_json(
                    existing['payload_json']
                )
                if persisted != artifact:
                    raise ValueError(
                        'late-decay estimate artifact id exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_late_decay_estimate_artifacts(
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

    def get(self, artifact_id: str) -> LateDecayEstimateArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_late_decay_estimate_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            LateDecayEstimateArtifact.model_validate_json(row['payload_json'])
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == LATE_DECAY_ESTIMATE_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith('late-decay-estimate-artifact:'):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith(
            'r150-late-decay-estimate-execution-provenance:'
        ):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_late_decay_estimate_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                LateDecayEstimateArtifact.model_validate_json(
                    row['payload_json']
                )
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None

    def resolve_artifact_manifest(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverArtifactManifest | None:
        """Resolve the persisted estimate as a typed solver manifest."""
        if not ref.authority_id.startswith('late-decay-estimate-artifact:'):
            return None
        artifact = self.get(ref.authority_id)
        if artifact is None or artifact.as_external_ref() != ref:
            return None
        return AcousticSolverArtifactManifest(
            artifact_ref=ref,
            observable=LATE_DECAY_ESTIMATE_OBSERVABLE,
            encoding_schema_ref=LATE_DECAY_ESTIMATE_SCHEMA_REF,
            valid_frequency_domain=artifact.frequency_domain,
            solver_lineage={
                'execution_id': artifact.execution_id,
                'execution_input_id': artifact.execution_input_id,
                'execution_input_sha256': artifact.execution_input_sha256,
                'dispatch_binding_id': artifact.dispatch_binding_id,
                'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
            },
        )
