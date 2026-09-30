"""R150 late-field energy authority — bounded deterministic GA complement.

This module is the energy-side companion to
``cad_geometric_acoustics_adapter`` (R150 deterministic direct/specular path
authority). It emits *typed late-energy path contributions* — per-surface
scattering and declared edge/aperture diffraction — that carry their own
explicit bounds and provenance so the R160 hybrid composition authority can
consume a ``late_energy_decay`` observable without ever blurring deterministic
specular identity into late energy.

Bounded capability contract (fail closed, never silently degraded):

- **surface_scattering**: each room-boundary surface contributes a first-order
  scattered-energy *upper bound* per band. The redirected fraction is the
  material authority's own banded scalar GA scatter quantity
  ``(1 - absorption) * scattering`` — the exact complement of the specular
  factor ``(1 - absorption) * (1 - scattering)`` used by the deterministic
  specular authority. The geometric bound treats the whole surface patch as a
  single energy-domain re-emission centroid, overestimates the intercepted
  solid angle by ``patch_area / (4*pi*d_min**2)`` with ``d_min`` the minimum
  source-to-patch-vertex distance, and re-emits isotropically bounded by
  ``1 / (4*pi*d2_min**2)``. The result is a conservative energy upper bound,
  not a point estimate, and it is labeled so
  (``energy_semantics='upper_bound_not_point_estimate'``).
- **edge_diffraction / aperture_diffraction**: only edges *explicitly declared*
  in :class:`LateFieldConfiguration` are diffraction candidates, and only when
  the exact compiled mesh resolves them. GA-ready compiled geometry is always
  watertight, so every diffracting edge has exactly two incident non-coplanar
  triangles; the declared semantics distinguish the two representable cases —
  a ``wedge`` is a solid single-surface edge (one incident surface), an
  ``aperture_rim`` is the rim where an opening joins two different surfaces
  (two incident surfaces). The diffracting point is the Fermat shortest-broken-path apex on
  the declared edge segment (Keller's equal-angle condition reduces to
  minimizing ``|S-A| + |A-R|`` over the segment). Both segments must clear the
  exact triangle occlusion test; a blocked segment means the single-edge
  contribution is not valid and the candidate is rejected with
  ``EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION`` rather than approximated.
  The diffracted energy is bounded by a caller-declared
  ``diffraction_energy_bound_factor`` — a conservative amplitude-scaling bound,
  not a UTD/BTD coefficient model.
- **Unsupported**: the multi-region Portal policy, non-banded materials,
  edges that do not resolve exactly, higher-order diffraction, directional
  scattering kernels, and any coherent-phase claim (the late-field authority is
  an energy-domain result; ``coherent_phase='NOT_APPLICABLE_ENERGY_DOMAIN'``).

Everything is deterministic-as-authored: identity is a content hash of the
semantic payload, persistence re-resolves every exact authority and regenerates
the artifact from them on reopen, and an R160
:class:`AcousticSolverResultEnvelope` row exposes the artifact as the typed
``late_energy_decay`` observable.

The ``late_energy_decay`` observable name is shared with the R160 bounded
late-energy decay artifact (``cad_hybrid_late_energy``,
``R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF`` — derived decay samples, not
upper bounds). Envelopes disambiguate the two encodings by
``encoding_schema_ref``; ``build_hybrid_acoustic_result`` fails closed when
more than one ``late_energy_decay`` candidate is bound.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from math import acos, degrees, pi, sqrt
from pathlib import Path
from typing import Any, Literal, Sequence
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from .cad_acoustic_solver_dispatch_repository import CadAcousticSolverDispatchRepository
from .cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from .cad_directivity import DirectivityDataset
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    ConfigurationResolver,
    DeterministicGaExecutionInput,
    DeterministicGaUnsupportedError,
    GeometricSurfacePlane,
    GeometryAuthorityResolver,
    MaterialAuthorityResolver,
    SourceDirectivityContribution,
    _directivity_contribution,
    _material_contribution,
    _occluder_triangles,
    _position_tuple,
    _region_point_membership,
    _round_float,
    _rounded_direction,
    _rounded_position,
    _segment_blocked,
    _segment_triangle_intersection_parameter,
    _triangle_vertices,
    _validate_supported_topology,
    _cross,
    _distance,
    _dot,
    _norm,
    _point_strictly_inside_box,
    _unit,
    _vector,
    _authority_ref,
)
from .cad_repository import SceneRepository
from .cad_scene import Direction3, Position3
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
from .occluder_grid_index import _IndexedOccluderRows


LATE_FIELD_SCHEMA_VERSION = 1
LATE_FIELD_AUTHORITY_VERSION = 'r150-late-field-energy-1'
LATE_FIELD_ADAPTER_ID = 'htdt.r150.late-field-energy'
LATE_FIELD_ADAPTER_VERSION = '1'

LATE_FIELD_ENGINE_ID = 'htdt.r150.late_field_energy_bound'
LATE_FIELD_ENGINE_VERSION = '1'

LATE_ENERGY_DECAY_OBSERVABLE = 'late_energy_decay'

LateFieldPathKind = Literal[
    'surface_scattering',
    'edge_diffraction',
    'aperture_diffraction',
]
SUPPORTED_LATE_FIELD_KINDS: tuple[LateFieldPathKind, ...] = (
    'surface_scattering',
    'edge_diffraction',
    'aperture_diffraction',
)

LateFieldCandidateDecision = Literal[
    'BLOCKED_VISIBILITY',
    'UNSUPPORTED_DIRECTIVITY',
    'UNSUPPORTED_BOUNDARY_QUANTITY',
    'UNSUPPORTED_GEOMETRY',
    'UNDIFRACTING_EDGE',
    'EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION',
]

EdgeSemantics = Literal['wedge', 'aperture_rim']

LATE_FIELD_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-late-field-energy',
    authority_version=LATE_FIELD_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': LATE_FIELD_ENGINE_ID,
            'version': LATE_FIELD_ENGINE_VERSION,
            'construction': (
                'bounded_first_order_energy_domain_surface_scatter_'
                'and_declared_edge_diffraction'
            ),
            'maximum_scattering_order': 1,
            'maximum_diffraction_order': 1,
            'coherent_phase': 'not_applicable_energy_domain',
            'energy_semantics': 'upper_bound_not_point_estimate',
        }
    ),
)

LATE_FIELD_ARTIFACT_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.late-field-energy-artifact.schema',
    authority_version=LATE_FIELD_AUTHORITY_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'LateFieldEnergyArtifact',
            'schema_version': LATE_FIELD_SCHEMA_VERSION,
            'quantity': 'late_energy_upper_bound_per_m2',
            'coherent_phase': 'not_applicable_energy_domain',
        }
    ),
)


class HtdtLateFieldEnergyEngine:
    """Exact engine marker for the bounded R150 late-field energy kernel.

    The kernel is energy-domain only: it never synthesizes coherent phase and
    never claims point estimates — every emitted contribution is a labeled
    upper bound. ``solver_implementation_ref`` is the kernel's own authority;
    the artifact separately records the deterministic-GA dispatch's
    ``solver_implementation_ref`` under which the execution ran.
    """

    engine_id = LATE_FIELD_ENGINE_ID
    engine_version = LATE_FIELD_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = LATE_FIELD_IMPLEMENTATION_REF
    maximum_scattering_order = 1
    maximum_diffraction_order = 1
    coherent_phase = 'not_applicable_energy_domain'
    energy_semantics = 'upper_bound_not_point_estimate'


class DeclaredDiffractingEdge(BaseModel):
    """Caller-declared diffracting-edge candidate on the exact compiled mesh.

    ``vertex_a``/``vertex_b`` are compiled-geometry vertex indices in canonical
    order (``vertex_a < vertex_b``). ``edge_semantics`` declares which exact
    diffraction topology the edge must resolve to:

    - ``wedge``: a solid single-surface edge — exactly two incident
      non-coplanar triangles belonging to one surface;
    - ``aperture_rim``: the rim where an opening joins two surfaces — exactly
      two incident non-coplanar triangles belonging to two different surfaces.

    (GA-ready compiled geometry is watertight, so both edge semantics have
    exactly two incident triangles; free single-incidence edges cannot exist
    in the compiler contract.)
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    vertex_a: int = Field(ge=0)
    vertex_b: int = Field(ge=0)
    edge_semantics: EdgeSemantics

    @model_validator(mode='after')
    def validate_declared_edge(self) -> 'DeclaredDiffractingEdge':
        if self.vertex_a >= self.vertex_b:
            raise ValueError('declared diffracting edge requires vertex_a < vertex_b')
        return self

    @property
    def edge_key(self) -> str:
        return f'{self.edge_semantics}:{self.vertex_a}-{self.vertex_b}'


class LateFieldConfiguration(BaseModel):
    """Versioned bounded policy authority for late-field energy execution."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LATE_FIELD_SCHEMA_VERSION
    authority_version: Literal['r150-late-field-energy-1'] = (
        LATE_FIELD_AUTHORITY_VERSION
    )
    configuration_id: str = Field(
        pattern=r'^r150-late-field-configuration:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    enabled_kinds: tuple[LateFieldPathKind, ...] = Field(min_length=1)
    scattering_energy_policy: Literal['patch_centroid_reemission_energy_bound_v1'] = (
        'patch_centroid_reemission_energy_bound_v1'
    )
    scattering_fraction_policy: Literal[
        'banded_scalar_ga_scatter_fraction'
    ] = 'banded_scalar_ga_scatter_fraction'
    scattering_redistribution: Literal[
        'isotropic_energy_upper_bound'
    ] = 'isotropic_energy_upper_bound'
    diffraction_edge_policy: Literal['explicit_declared_compiled_edges_v1'] = (
        'explicit_declared_compiled_edges_v1'
    )
    diffraction_apex_policy: Literal['shortest_total_path_apex_on_segment_v1'] = (
        'shortest_total_path_apex_on_segment_v1'
    )
    diffraction_energy_policy: Literal['declared_bound_factor_isotropic_v1'] = (
        'declared_bound_factor_isotropic_v1'
    )
    diffraction_energy_bound_factor: float = Field(gt=0.0, le=1.0)
    energy_semantics: Literal['upper_bound_not_point_estimate'] = (
        'upper_bound_not_point_estimate'
    )
    coherent_phase_policy: Literal['not_applicable_energy_domain'] = (
        'not_applicable_energy_domain'
    )
    declared_diffracting_edges: tuple[DeclaredDiffractingEdge, ...] = ()

    @model_validator(mode='after')
    def validate_configuration(self) -> 'LateFieldConfiguration':
        kinds = tuple(sorted(set(self.enabled_kinds)))
        if kinds != self.enabled_kinds:
            raise ValueError('late-field enabled kinds must be unique and sorted')
        unsupported = set(kinds) - set(SUPPORTED_LATE_FIELD_KINDS)
        if unsupported:
            raise ValueError(
                f'late-field configuration declares unsupported kinds: '
                f'{sorted(unsupported)}'
            )
        edge_keys = [item.edge_key for item in self.declared_diffracting_edges]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError('declared diffracting edges must be unique')
        if edge_keys != sorted(edge_keys):
            raise ValueError('declared diffracting edges must use canonical ordering')
        if (
            'edge_diffraction' not in kinds
            and 'aperture_diffraction' not in kinds
            and self.declared_diffracting_edges
        ):
            raise ValueError(
                'declared diffracting edges require an enabled diffraction kind'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('LateFieldConfiguration semantic hash mismatch')
        if self.configuration_id != f'r150-late-field-configuration:{expected}':
            raise ValueError('LateFieldConfiguration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'configuration_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.configuration_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_late_field_configuration(
    *,
    enabled_kinds: Sequence[str] = SUPPORTED_LATE_FIELD_KINDS,
    diffraction_energy_bound_factor: float = 0.1,
    declared_diffracting_edges: Sequence[DeclaredDiffractingEdge] = (),
) -> LateFieldConfiguration:
    kinds = tuple(sorted(set(str(kind) for kind in enabled_kinds)))
    edges = tuple(
        sorted(
            declared_diffracting_edges,
            key=lambda item: item.edge_key,
        )
    )
    core: dict[str, Any] = {
        'schema_version': LATE_FIELD_SCHEMA_VERSION,
        'authority_version': LATE_FIELD_AUTHORITY_VERSION,
        'enabled_kinds': list(kinds),
        'scattering_energy_policy': 'patch_centroid_reemission_energy_bound_v1',
        'scattering_fraction_policy': 'banded_scalar_ga_scatter_fraction',
        'scattering_redistribution': 'isotropic_energy_upper_bound',
        'diffraction_edge_policy': 'explicit_declared_compiled_edges_v1',
        'diffraction_apex_policy': 'shortest_total_path_apex_on_segment_v1',
        'diffraction_energy_policy': 'declared_bound_factor_isotropic_v1',
        'diffraction_energy_bound_factor': float(diffraction_energy_bound_factor),
        'energy_semantics': 'upper_bound_not_point_estimate',
        'coherent_phase_policy': 'not_applicable_energy_domain',
        'declared_diffracting_edges': [
            item.model_dump(mode='json') for item in edges
        ],
    }
    digest = _semantic_hash(core)
    return LateFieldConfiguration(
        configuration_id=f'r150-late-field-configuration:{digest}',
        semantic_sha256=digest,
        **core,
    )


class LateFieldCapabilityRecord(BaseModel):
    """Explicit bounded-capability disclosure persisted inside the artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    supported_geometry_policies: tuple[str, ...] = (
        'exact_axis_aligned_closed_shoebox_v1',
        'general_planar_closed_polyhedral_v1',
    )
    supported_kinds: tuple[LateFieldPathKind, ...] = SUPPORTED_LATE_FIELD_KINDS
    maximum_scattering_order: Literal[1] = 1
    maximum_diffraction_order: Literal[1] = 1
    scattering_quantity: Literal['banded_scalar_ga_scatter_fraction'] = (
        'banded_scalar_ga_scatter_fraction'
    )
    diffraction_quantity: Literal['declared_energy_bound_factor'] = (
        'declared_energy_bound_factor'
    )
    energy_semantics: Literal['upper_bound_not_point_estimate'] = (
        'upper_bound_not_point_estimate'
    )
    coherent_phase: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    unsupported_capabilities: tuple[str, ...] = (
        'multi-region Portal geometry policy '
        '(general_planar_multi_region_portal_v1) — late-field authority '
        'requires exactly one closed AcousticRegion',
        'higher-order diffraction — a path may contain at most one '
        'diffracting edge; blocked single-edge segments are rejected as '
        'EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION',
        'wedge/aperture diffraction coefficient models (UTD/BTD) — the '
        'diffracted term is a caller-declared energy bound factor, not a '
        'frequency-differentiated wedge solution',
        'directional or lobed scattering redistribution — only isotropic '
        'upper bounds from banded scalar GA scatter fractions are emitted',
        'coherent phase — late-field contributions are energy-domain results '
        'and carry no phase authority',
    )


class DiffractingEdgeRecord(BaseModel):
    """Resolved exact diffracting edge on the compiled mesh."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    edge_semantics: EdgeSemantics
    vertex_a: int = Field(ge=0)
    vertex_b: int = Field(ge=0)
    endpoint_a: Position3
    endpoint_b: Position3
    incident_triangle_indices: tuple[int, ...] = Field(min_length=2, max_length=2)
    incident_surface_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    wedge_face_separation_angle_deg: float = Field(gt=0.0, le=180.0)

    @model_validator(mode='after')
    def validate_record(self) -> 'DiffractingEdgeRecord':
        if self.vertex_a >= self.vertex_b:
            raise ValueError('diffracting edge requires vertex_a < vertex_b')
        if len(set(self.incident_surface_ids)) != len(self.incident_surface_ids):
            raise ValueError('diffracting edge incident surface ids must be unique')
        if self.edge_semantics == 'wedge':
            if len(self.incident_surface_ids) != 1:
                raise ValueError(
                    'wedge diffraction requires exactly one incident surface'
                )
        else:
            if len(self.incident_surface_ids) != 2:
                raise ValueError(
                    'aperture rim diffraction requires exactly two incident surfaces'
                )
        return self

    @property
    def edge_key(self) -> str:
        return f'{self.edge_semantics}:{self.vertex_a}-{self.vertex_b}'


class LateEnergyBandQuantity(BaseModel):
    """One frequency band of one typed late-energy path contribution."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_definition: Literal['exact_center_frequency_sample'] = (
        'exact_center_frequency_sample'
    )
    center_hz: float = Field(gt=0.0)
    quantity: Literal['late_energy_upper_bound_per_m2'] = (
        'late_energy_upper_bound_per_m2'
    )
    patch_area_m2: float | None = Field(default=None, gt=0.0)
    incident_distance_bound_m: float = Field(gt=0.0)
    emergent_distance_bound_m: float = Field(gt=0.0)
    redirected_fraction: float = Field(ge=0.0, le=1.0)
    late_energy_upper_bound_per_m2: float = Field(ge=0.0)
    source_directivity: SourceDirectivityContribution
    boundary_material: BoundaryMaterialContribution | None = None
    coherent_phase: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )


class LateEnergyPathContribution(BaseModel):
    """One typed bounded late-energy path contribution.

    ``kind`` distinguishes the physical mechanism and never blurs into the
    deterministic specular identity: ``surface_scattering`` binds a room
    boundary surface patch centroid, ``edge_diffraction``/``aperture_diffraction``
    bind a resolved :class:`DiffractingEdgeRecord` apex.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    contribution_id: str = Field(pattern=r'^late-energy-path:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str
    receiver_id: str
    receiver_entity_id: str
    kind: LateFieldPathKind
    interaction_surface_id: str | None = Field(
        default=None,
        pattern=r'^semantic-surface:[0-9a-f]{64}$',
    )
    diffracting_edge: DiffractingEdgeRecord | None = None
    interaction_point: Position3
    apex_clamped_to_segment_endpoint: bool | None = None
    geometric_path_length_m: float = Field(gt=0.0)
    propagation_delay_s: float = Field(gt=0.0)
    departure_direction: Direction3
    arrival_direction: Direction3
    direction_semantics: Literal[
        'world_propagation_direction_source_out_and_receiver_in'
    ] = 'world_propagation_direction_source_out_and_receiver_in'
    bands: tuple[LateEnergyBandQuantity, ...] = Field(min_length=1)
    adapter_id: Literal['htdt.r150.late-field-energy'] = LATE_FIELD_ADAPTER_ID
    adapter_version: Literal['1'] = LATE_FIELD_ADAPTER_VERSION
    late_field_implementation_ref: ExactExternalAuthorityRef
    energy_semantics: Literal['upper_bound_not_point_estimate'] = (
        'upper_bound_not_point_estimate'
    )
    coherent_phase: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    execution_input_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_contribution(self) -> 'LateEnergyPathContribution':
        if self.kind == 'surface_scattering':
            if self.interaction_surface_id is None:
                raise ValueError('surface scattering requires an interaction surface')
            if self.diffracting_edge is not None:
                raise ValueError('surface scattering cannot carry a diffracting edge')
            if self.apex_clamped_to_segment_endpoint is not None:
                raise ValueError(
                    'surface scattering cannot carry apex clamp evidence'
                )
            for band in self.bands:
                if band.boundary_material is None:
                    raise ValueError(
                        'surface scattering requires a boundary material contribution'
                    )
                if band.patch_area_m2 is None:
                    raise ValueError('surface scattering requires a patch area')
                if (
                    band.boundary_material.source_surface_id
                    != self.interaction_surface_id
                ):
                    raise ValueError(
                        'surface scattering material must match interaction surface'
                    )
        else:
            if self.diffracting_edge is None:
                raise ValueError('diffraction requires a resolved diffracting edge')
            if self.interaction_surface_id is not None:
                raise ValueError('diffraction cannot carry an interaction surface')
            if self.apex_clamped_to_segment_endpoint is None:
                raise ValueError('diffraction requires apex clamp evidence')
            expected_semantics = (
                'aperture_rim'
                if self.kind == 'aperture_diffraction'
                else 'wedge'
            )
            if self.diffracting_edge.edge_semantics != expected_semantics:
                raise ValueError('diffraction kind/edge semantics mismatch')
            for band in self.bands:
                if band.boundary_material is not None:
                    raise ValueError(
                        'diffraction cannot carry a boundary material contribution'
                    )
                if band.patch_area_m2 is not None:
                    raise ValueError('diffraction cannot carry a patch area')

        centers = [band.center_hz for band in self.bands]
        if len(centers) != len(set(centers)):
            raise ValueError('late-energy contribution duplicate frequency centers')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('LateEnergyPathContribution semantic hash mismatch')
        if self.contribution_id != f'late-energy-path:{expected}':
            raise ValueError('LateEnergyPathContribution id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'contribution_id', 'semantic_sha256'},
        )
        if self.interaction_surface_id is None:
            payload.pop('interaction_surface_id', None)
        if self.diffracting_edge is None:
            payload.pop('diffracting_edge', None)
        if self.apex_clamped_to_segment_endpoint is None:
            payload.pop('apex_clamped_to_segment_endpoint', None)
        payload['bands'] = [
            _prune_late_band_payload(band) for band in payload['bands']
        ]
        return payload


def _prune_late_band_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a serialized late-energy band payload for content hashing."""
    if payload.get('patch_area_m2') is None:
        payload.pop('patch_area_m2', None)
    if payload.get('boundary_material') is None:
        payload.pop('boundary_material', None)
    return payload


class LateFieldRejectedCandidate(BaseModel):
    """Typed rejection record for an evaluated-but-fail-closed candidate."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str
    receiver_id: str
    kind: LateFieldPathKind
    interaction_key: str = Field(min_length=1)
    decision: LateFieldCandidateDecision
    reason: str = Field(min_length=1)


class LateFieldEnergyArtifact(BaseModel):
    """Immutable energy-domain late-field path authority for R160 consumption."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LATE_FIELD_SCHEMA_VERSION
    authority_version: Literal['r150-late-field-energy-1'] = (
        LATE_FIELD_AUTHORITY_VERSION
    )
    artifact_id: str = Field(
        pattern=r'^late-field-energy-artifact:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(pattern=r'^r150-late-field-execution:[0-9a-f]{64}$')
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
    late_field_configuration_ref: ExactExternalAuthorityRef
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    engine_id: str
    engine_version: str
    late_field_implementation_ref: ExactExternalAuthorityRef
    candidate_source_commit: str | None = None
    identity_decimal_places: int = Field(ge=6, le=15)
    frequency_domain: FrequencyDomain
    path_scope: Literal['single_region_bounded_late_field_energy_v1'] = (
        'single_region_bounded_late_field_energy_v1'
    )
    energy_semantics: Literal['upper_bound_not_point_estimate'] = (
        'upper_bound_not_point_estimate'
    )
    coherent_phase_authority: Literal['NOT_APPLICABLE_ENERGY_DOMAIN'] = (
        'NOT_APPLICABLE_ENERGY_DOMAIN'
    )
    capability_record: LateFieldCapabilityRecord
    contributions: tuple[LateEnergyPathContribution, ...]
    rejected_candidates: tuple[LateFieldRejectedCandidate, ...]

    @model_validator(mode='after')
    def validate_artifact(self) -> 'LateFieldEnergyArtifact':
        contribution_ids = [
            item.contribution_id for item in self.contributions
        ]
        if len(contribution_ids) != len(set(contribution_ids)):
            raise ValueError(
                'late-field artifact contains duplicate contribution ids'
            )
        ordering = [
            (
                item.source_entity_id,
                item.receiver_id,
                item.kind,
                item.interaction_surface_id
                if item.interaction_surface_id is not None
                else (item.diffracting_edge.edge_key if item.diffracting_edge else ''),
                item.contribution_id,
            )
            for item in self.contributions
        ]
        if ordering != sorted(ordering):
            raise ValueError(
                'late-field contributions must use canonical ordering'
            )
        rejected_order = [
            (
                item.source_entity_id,
                item.receiver_id,
                item.kind,
                item.interaction_key,
                item.decision,
                item.reason,
            )
            for item in self.rejected_candidates
        ]
        if rejected_order != sorted(rejected_order):
            raise ValueError(
                'late-field rejected candidates must use canonical ordering'
            )
        for contribution in self.contributions:
            if (
                contribution.execution_input_semantic_sha256
                != self.execution_input_sha256
            ):
                raise ValueError(
                    'late-field contribution execution input hash mismatch'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('LateFieldEnergyArtifact semantic hash mismatch')
        if self.artifact_id != f'late-field-energy-artifact:{expected}':
            raise ValueError('LateFieldEnergyArtifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
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
            'late_field_configuration_ref': (
                self.late_field_configuration_ref.model_dump(mode='json')
            ),
            'engine_id': self.engine_id,
            'engine_version': self.engine_version,
            'late_field_implementation_ref': (
                self.late_field_implementation_ref.model_dump(mode='json')
            ),
            'candidate_source_commit': self.candidate_source_commit,
        }
        digest = _semantic_hash(payload)
        return ExactExternalAuthorityRef(
            authority_id=f'r150-late-field-execution-provenance:{digest}',
            authority_version=self.authority_version,
            semantic_hash_sha256=digest,
        )


def _edge_incident_triangles(
    compiled: R120CompiledGeometry,
    vertex_a: int,
    vertex_b: int,
) -> tuple[int, ...]:
    """Return compiled triangle indices incident to the (a, b) edge."""
    incident: list[int] = []
    for index, triangle in enumerate(compiled.triangles):
        vertices = {triangle.a, triangle.b, triangle.c}
        if vertex_a in vertices and vertex_b in vertices:
            incident.append(index)
    return tuple(sorted(incident))


def _resolve_diffracting_edge(
    compiled: R120CompiledGeometry,
    declared: DeclaredDiffractingEdge,
    *,
    identity_decimal_places: int,
    tolerance: float,
) -> DiffractingEdgeRecord:
    """Resolve a declared candidate edge against the exact compiled mesh.

    Raises :class:`_UndiffractingEdge` when the edge does not resolve to the
    declared diffraction topology — the caller records ``UNDIFRACTING_EDGE``;
    raises ``ValueError`` on out-of-range or degenerate declarations.
    """
    vertex_count = len(compiled.vertices)
    if declared.vertex_a >= vertex_count or declared.vertex_b >= vertex_count:
        raise ValueError(
            'declared diffracting edge vertex index is outside the compiled mesh'
        )
    incident = _edge_incident_triangles(
        compiled,
        declared.vertex_a,
        declared.vertex_b,
    )
    incident_surface_ids = tuple(
        sorted({compiled.triangles[index].source_surface_id for index in incident})
    )
    vertex_a = compiled.vertices[declared.vertex_a]
    vertex_b = compiled.vertices[declared.vertex_b]
    endpoint_a = _rounded_position(
        _position_tuple(vertex_a),
        identity_decimal_places,
    )
    endpoint_b = _rounded_position(
        _position_tuple(vertex_b),
        identity_decimal_places,
    )
    edge_length = _distance(
        _position_tuple(vertex_a),
        _position_tuple(vertex_b),
    )
    if edge_length <= tolerance:
        raise ValueError('declared diffracting edge is degenerate within tolerance')

    if len(incident) != 2:
        raise _UndiffractingEdge(
            f'diffracting edge requires exactly two incident triangles on the '
            f'exact closed mesh; mesh resolves {len(incident)}'
        )
    normals = []
    for index in incident:
        va, vb, vc = _triangle_vertices(compiled, index)
        normal = _cross(_vector(va, vb), _vector(va, vc))
        length = _norm(normal)
        if length <= tolerance * tolerance:
            raise _UndiffractingEdge(
                'diffracting-edge incident triangle is degenerate within tolerance'
            )
        normals.append(_unit(normal))
    cosine = max(-1.0, min(1.0, _dot(normals[0], normals[1])))
    separation_deg = degrees(acos(cosine))
    if separation_deg <= degrees(acos(1.0 - tolerance)):
        raise _UndiffractingEdge(
            'incident faces are coplanar within tolerance; the edge is not a '
            'diffracting rim'
        )

    if declared.edge_semantics == 'aperture_rim':
        if len(incident_surface_ids) != 2:
            raise _UndiffractingEdge(
                'aperture rim requires exactly two incident surfaces — the rim '
                'where an opening joins two surfaces; mesh resolves '
                f'{len(incident_surface_ids)}'
            )
        return DiffractingEdgeRecord(
            edge_semantics='aperture_rim',
            vertex_a=declared.vertex_a,
            vertex_b=declared.vertex_b,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            incident_triangle_indices=incident,
            incident_surface_ids=incident_surface_ids,
            wedge_face_separation_angle_deg=separation_deg,
        )

    if len(incident_surface_ids) != 1:
        raise _UndiffractingEdge(
            'wedge diffraction requires exactly one incident surface — a solid '
            'single-surface edge; mesh resolves '
            f'{len(incident_surface_ids)}'
        )
    return DiffractingEdgeRecord(
        edge_semantics='wedge',
        vertex_a=declared.vertex_a,
        vertex_b=declared.vertex_b,
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
        incident_triangle_indices=incident,
        incident_surface_ids=incident_surface_ids,
        wedge_face_separation_angle_deg=separation_deg,
    )


class _UndiffractingEdge(ValueError):
    """Internal marker: declared edge does not resolve as a diffracting edge."""


def _diffraction_apex(
    *,
    source: Sequence[float],
    receiver: Sequence[float],
    endpoint_a: Sequence[float],
    endpoint_b: Sequence[float],
    tolerance: float,
) -> tuple[tuple[float, float, float], bool]:
    """Shortest-broken-path apex on the edge segment.

    Minimizes ``|S - A(t)| + |A(t) - R|`` over ``t in [0, 1]`` with
    ``A(t) = a + t*(b - a)``. The unconstrained minimizer projects each
    endpoint onto the edge axis and interpolates by perpendicular distance —
    the discrete Keller equal-angle apex. Returns the apex plus whether the
    parameter was clamped to a segment endpoint.
    """
    axis = _vector(endpoint_a, endpoint_b)
    length = _norm(axis)
    unit_axis = _unit(axis)
    source_vector = _vector(endpoint_a, source)
    receiver_vector = _vector(endpoint_a, receiver)
    source_t = _dot(source_vector, unit_axis) / length
    receiver_t = _dot(receiver_vector, unit_axis) / length
    source_perp_distance = sqrt(
        max(
            0.0,
            _dot(source_vector, source_vector)
            - (source_t * length) ** 2,
        )
    )
    receiver_perp_distance = sqrt(
        max(
            0.0,
            _dot(receiver_vector, receiver_vector)
            - (receiver_t * length) ** 2,
        )
    )
    if (
        source_perp_distance <= tolerance
        or receiver_perp_distance <= tolerance
    ):
        raise ValueError(
            'diffraction apex is degenerate: source or receiver lies on the '
            'edge axis within tolerance'
        )
    t_apex = (
        (source_perp_distance * receiver_t)
        + (receiver_perp_distance * source_t)
    ) / (source_perp_distance + receiver_perp_distance)
    clamped = False
    if t_apex < 0.0:
        t_apex = 0.0
        clamped = True
    elif t_apex > 1.0:
        t_apex = 1.0
        clamped = True
    apex = (
        endpoint_a[0] + axis[0] * t_apex,
        endpoint_a[1] + axis[1] * t_apex,
        endpoint_a[2] + axis[2] * t_apex,
    )
    return apex, clamped


def _patch_geometry(
    compiled: R120CompiledGeometry,
    triangle_indices: Sequence[int],
) -> tuple[tuple[float, float, float], float, tuple[tuple[float, float, float], ...]]:
    """Surface patch centroid, total area, and unique vertex tuples."""
    points: list[tuple[float, float, float]] = []
    seen: set[tuple[float, float, float]] = set()
    area = 0.0
    for index in triangle_indices:
        va, vb, vc = _triangle_vertices(compiled, index)
        for vertex in (va, vb, vc):
            if vertex not in seen:
                seen.add(vertex)
                points.append(vertex)
        area += 0.5 * _norm(_cross(_vector(va, vb), _vector(va, vc)))
    centroid = (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
        sum(point[2] for point in points) / len(points),
    )
    return centroid, area, tuple(points)


def _min_vertex_distance(
    point: Sequence[float],
    vertices: Sequence[Sequence[float]],
) -> float:
    return min(_distance(point, vertex) for vertex in vertices)


def _segment_blocked_ignoring_triangles(
    compiled: R120CompiledGeometry,
    start: Sequence[float],
    end: Sequence[float],
    *,
    tolerance: float,
    ignored_triangle_indices: frozenset[int],
    occluder_records: tuple | None = None,
) -> bool:
    """Triangle-level occlusion check skipping only the incident triangles.

    Non-incident faces of the *same* surface still block — a segment that
    crosses a solid obstacle's far faces requires a different diffraction
    treatment and must not silently pass.
    """
    if occluder_records is None:
        occluder_records = tuple(
            (index,) + _triangle_vertices(compiled, index)
            for index in range(len(compiled.triangles))
        )
    candidates = (
        occluder_records._segment_candidates(start, end)
        if isinstance(occluder_records, _IndexedOccluderRows)
        else occluder_records
    )
    for index, vertex_a, vertex_b, vertex_c in candidates:
        if index in ignored_triangle_indices:
            continue
        hit = _segment_triangle_intersection_parameter(
            start,
            end,
            (vertex_a, vertex_b, vertex_c),
            tolerance=tolerance,
            distance_scaled_tolerance=True,
        )
        if hit is not None:
            return True
    return False


def execute_late_field_energy(
    *,
    execution_input: DeterministicGaExecutionInput,
    late_field_configuration: LateFieldConfiguration,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    material_resolver: MaterialAuthorityResolver,
    engine: HtdtLateFieldEnergyEngine | None = None,
) -> LateFieldEnergyArtifact:
    """Evaluate the bounded R150 late-field energy authority.

    Fails closed (raises) on topology/geometry-policy violations and records
    per-candidate rejections for visibility, material capability, directivity
    and diffraction-topology failures — never silently degraded or fabricated.
    """
    if engine is None:
        engine = HtdtLateFieldEnergyEngine()
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
    )
    late_field_configuration = LateFieldConfiguration.model_validate(
        late_field_configuration.model_dump(mode='python')
    )
    if (
        compiled_geometry.compiled_geometry_id
        != execution_input.r120_compiled_geometry_id
        or compiled_geometry.compiled_hash_sha256
        != execution_input.r120_compiled_geometry_sha256
        or compiled_geometry.topology_identity_sha256
        != execution_input.topology_identity_sha256
    ):
        raise ValueError('late-field execution compiled geometry exact identity mismatch')

    if (
        _authority_ref(region_authority) != execution_input.region_authority_ref
        or _authority_ref(portal_authority) != execution_input.portal_authority_ref
        or _authority_ref(boundary_termination_authority)
        != execution_input.boundary_termination_authority_ref
    ):
        raise ValueError('late-field exact geometry authority mismatch')

    if execution_input.geometry_policy == 'general_planar_multi_region_portal_v1':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            'late-field energy authority supports exactly one closed '
            'AcousticRegion; the multi-region Portal policy is unsupported',
        )
    if execution_input.geometry_policy is not None and (
        execution_input.geometry_policy != 'general_planar_closed_polyhedral_v1'
    ):
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_GEOMETRY',
            f'late-field energy authority does not support geometry policy '
            f'{execution_input.geometry_policy!r}',
        )
    _validate_supported_topology(
        region_authority=region_authority,
        portal_authority=portal_authority,
        boundary_termination_authority=boundary_termination_authority,
    )
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError('late-field authority rejects approximated/dropped R120 geometry')
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError('late-field authority requires exact R120 preservation')

    tolerance = execution_input.geometric_tolerance_m
    places = execution_input.identity_decimal_places
    sound_speed = float(execution_input.sound_speed_m_s)

    general_geometry = (
        execution_input.geometry_policy == 'general_planar_closed_polyhedral_v1'
    )
    region_surfaces = set(region_authority.declarations[0].boundary_surface_ids)
    mapping_by_surface = {
        item.source_surface_id: item
        for item in compiled_geometry.surface_mapping
    }
    if general_geometry:
        region_triangle_indices = tuple(
            sorted(
                {
                    index
                    for surface_id in region_surfaces
                    for index in mapping_by_surface[surface_id].compiled_triangle_indices
                }
            )
        )
        region_bounds = None
    else:
        bounds = compiled_geometry.bounding_volume
        region_bounds = (
            (float(bounds.min_x_m), float(bounds.max_x_m)),
            (float(bounds.min_y_m), float(bounds.max_y_m)),
            (float(bounds.min_z_m), float(bounds.max_z_m)),
        )
        region_triangle_indices = ()

    def require_inside_region(point: tuple[float, float, float], label: str) -> None:
        if general_geometry:
            membership = _region_point_membership(
                compiled_geometry,
                region_triangle_indices,
                point,
                tolerance=tolerance,
            )
            if membership != 'inside':
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_REGION_MEMBERSHIP',
                    f'{label} is not unambiguously inside the sole explicit '
                    f'acoustic region (membership={membership})',
                )
        else:
            assert region_bounds is not None
            if not _point_strictly_inside_box(
                point,
                bounds=region_bounds,
                tolerance_m=tolerance,
            ):
                raise ValueError(
                    f'{label} is not strictly inside the sole explicit acoustic '
                    'region; unmodeled external space is not an implicit '
                    'propagation region'
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

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    for source in execution_input.sources:
        require_inside_region(
            _position_tuple(source.source_reference_point),
            f'source {source.source_entity_id} acoustic reference point',
        )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                'late-field execution missing exact DirectivityDataset for '
                f'{source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError(
                'late-field execution DirectivityDataset identity mismatch'
            )
    for receiver in execution_input.receivers:
        require_inside_region(
            _position_tuple(receiver.world_position),
            f'receiver {receiver.receiver_id} position',
        )

    for plane in execution_input.boundary_planes:
        if plane.source_surface_id not in region_surfaces:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'late-field authority evaluates room-boundary surfaces only; '
                f'{plane.source_surface_id} is outside the explicit region',
            )

    resolved_edges: dict[str, DiffractingEdgeRecord | str] = {}
    diffraction_kinds_enabled = (
        'edge_diffraction' in late_field_configuration.enabled_kinds
        or 'aperture_diffraction' in late_field_configuration.enabled_kinds
    )
    if diffraction_kinds_enabled:
        for declared in late_field_configuration.declared_diffracting_edges:
            try:
                record = _resolve_diffracting_edge(
                    compiled_geometry,
                    declared,
                    identity_decimal_places=places,
                    tolerance=tolerance,
                )
            except _UndiffractingEdge as exc:
                resolved_edges[declared.edge_key] = str(exc)
            else:
                resolved_edges[declared.edge_key] = record

    contributions: list[LateEnergyPathContribution] = []
    rejected: list[LateFieldRejectedCandidate] = []

    occluder_triangles = _occluder_triangles(compiled_geometry)
    occluder_records = _IndexedOccluderRows(
        (index,) + _triangle_vertices(compiled_geometry, index)
        for index in range(len(compiled_geometry.triangles))
    )

    for source in execution_input.sources:
        dataset = dataset_by_hash[source.directivity_dataset_sha256]
        source_point = _position_tuple(source.source_reference_point)
        source_axis = source.source_axis
        source_patch_cache: dict[str, tuple] = {}
        for receiver in execution_input.receivers:
            receiver_point = _position_tuple(receiver.world_position)

            if 'surface_scattering' in late_field_configuration.enabled_kinds:
                for plane in execution_input.boundary_planes:
                    rejected_record = _evaluate_scattering_candidate(
                        execution_input=execution_input,
                        compiled_geometry=compiled_geometry,
                        plane=plane,
                        surface_mapping=mapping_by_surface.get(
                            plane.source_surface_id
                        ),
                        source=source,
                        source_point=source_point,
                        source_axis=source_axis,
                        dataset=dataset,
                        receiver=receiver,
                        receiver_point=receiver_point,
                        material_resolver=material_resolver,
                        tolerance=tolerance,
                        places=places,
                        sound_speed=sound_speed,
                        source_patch_cache=source_patch_cache,
                        occluder_triangles=occluder_triangles,
                    )
                    if isinstance(rejected_record, LateFieldRejectedCandidate):
                        rejected.append(rejected_record)
                    elif rejected_record is not None:
                        contributions.append(rejected_record)

            if diffraction_kinds_enabled:
                for declared in late_field_configuration.declared_diffracting_edges:
                    enabled_kind = (
                        'edge_diffraction'
                        if declared.edge_semantics == 'wedge'
                        else 'aperture_diffraction'
                    )
                    if enabled_kind not in late_field_configuration.enabled_kinds:
                        rejected.append(
                            LateFieldRejectedCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                kind=enabled_kind,
                                interaction_key=declared.edge_key,
                                decision='UNDIFRACTING_EDGE',
                                reason=(
                                    'declared diffracting edge resolves to a '
                                    f'{declared.edge_semantics} kind that is not '
                                    'enabled in the late-field configuration'
                                ),
                            )
                        )
                        continue
                    resolved = resolved_edges.get(declared.edge_key)
                    if isinstance(resolved, str) or resolved is None:
                        rejected.append(
                            LateFieldRejectedCandidate(
                                source_entity_id=source.source_entity_id,
                                receiver_id=receiver.receiver_id,
                                kind=enabled_kind,
                                interaction_key=declared.edge_key,
                                decision='UNDIFRACTING_EDGE',
                                reason=(
                                    resolved
                                    if isinstance(resolved, str)
                                    else 'declared diffracting edge did not resolve'
                                ),
                            )
                        )
                        continue
                    result = _evaluate_diffraction_candidate(
                        execution_input=execution_input,
                        late_field_configuration=late_field_configuration,
                        compiled_geometry=compiled_geometry,
                        edge=resolved,
                        source=source,
                        source_point=source_point,
                        source_axis=source_axis,
                        dataset=dataset,
                        receiver=receiver,
                        receiver_point=receiver_point,
                        tolerance=tolerance,
                        places=places,
                        sound_speed=sound_speed,
                        kind=enabled_kind,
                        occluder_records=occluder_records,
                    )
                    if isinstance(result, LateFieldRejectedCandidate):
                        rejected.append(result)
                    elif result is not None:
                        contributions.append(result)

    ordering = lambda item: (  # noqa: E731
        item.source_entity_id,
        item.receiver_id,
        item.kind,
        item.interaction_surface_id
        if item.interaction_surface_id is not None
        else (item.diffracting_edge.edge_key if item.diffracting_edge else ''),
        item.contribution_id,
    )
    contributions_tuple = tuple(sorted(contributions, key=ordering))
    rejected_tuple = tuple(
        sorted(
            rejected,
            key=lambda item: (
                item.source_entity_id,
                item.receiver_id,
                item.kind,
                item.interaction_key,
                item.decision,
                item.reason,
            ),
        )
    )

    execution_id_digest = _semantic_hash(
        {
            'execution_input_id': execution_input.execution_input_id,
            'execution_input_sha256': execution_input.semantic_sha256,
            'late_field_configuration_ref': (
                late_field_configuration.as_external_ref().model_dump(mode='json')
            ),
            'engine_id': engine.engine_id,
            'engine_version': engine.engine_version,
        }
    )
    core: dict[str, Any] = {
        'schema_version': LATE_FIELD_SCHEMA_VERSION,
        'authority_version': LATE_FIELD_AUTHORITY_VERSION,
        'execution_id': f'r150-late-field-execution:{execution_id_digest}',
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
        'late_field_configuration_ref': (
            late_field_configuration.as_external_ref().model_dump(mode='json')
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': execution_input.r120_compiled_geometry_sha256,
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'late_field_implementation_ref': (
            engine.solver_implementation_ref.model_dump(mode='json')
        ),
        'candidate_source_commit': engine.candidate_source_commit,
        'identity_decimal_places': places,
        'frequency_domain': execution_input.frequency_domain.model_dump(mode='json'),
        'path_scope': 'single_region_bounded_late_field_energy_v1',
        'energy_semantics': 'upper_bound_not_point_estimate',
        'coherent_phase_authority': 'NOT_APPLICABLE_ENERGY_DOMAIN',
        'capability_record': LateFieldCapabilityRecord().model_dump(mode='json'),
        'contributions': [
            item.model_dump(mode='json') for item in contributions_tuple
        ],
        'rejected_candidates': [
            item.model_dump(mode='json') for item in rejected_tuple
        ],
    }
    digest = _semantic_hash(core)
    return LateFieldEnergyArtifact(
        artifact_id=f'late-field-energy-artifact:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _evaluate_scattering_candidate(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    plane: GeometricSurfacePlane,
    surface_mapping,
    source,
    source_point: tuple[float, float, float],
    source_axis: Direction3,
    dataset: DirectivityDataset,
    receiver,
    receiver_point: tuple[float, float, float],
    material_resolver: MaterialAuthorityResolver,
    tolerance: float,
    places: int,
    sound_speed: float,
    source_patch_cache: dict[str, tuple] | None = None,
    occluder_triangles: tuple | None = None,
) -> LateEnergyPathContribution | LateFieldRejectedCandidate | None:
    kind: LateFieldPathKind = 'surface_scattering'
    interaction_key = plane.source_surface_id

    def reject(decision: LateFieldCandidateDecision, reason: str) -> LateFieldRejectedCandidate:
        return LateFieldRejectedCandidate(
            source_entity_id=source.source_entity_id,
            receiver_id=receiver.receiver_id,
            kind=kind,
            interaction_key=interaction_key,
            decision=decision,
            reason=reason,
        )

    cached_patch = (
        source_patch_cache.get(interaction_key)
        if source_patch_cache is not None
        else None
    )
    if cached_patch is None:
        if plane.compiled_triangle_indices:
            patch_triangle_indices = tuple(plane.compiled_triangle_indices)
        elif (
            surface_mapping is not None
            and surface_mapping.compiled_triangle_indices
        ):
            patch_triangle_indices = tuple(
                surface_mapping.compiled_triangle_indices
            )
        else:
            cached_patch = (
                'reject',
                'UNSUPPORTED_GEOMETRY',
                'boundary surface resolves no compiled triangles in the exact mesh',
            )
        if cached_patch is None:
            centroid, patch_area, vertices = _patch_geometry(
                compiled_geometry,
                patch_triangle_indices,
            )
            if patch_area <= tolerance * tolerance:
                cached_patch = (
                    'reject',
                    'UNSUPPORTED_GEOMETRY',
                    'boundary surface patch area is degenerate within tolerance',
                )
            elif _segment_blocked(
                compiled_geometry,
                source_point,
                centroid,
                tolerance=tolerance,
                distance_scaled_tolerance=True,
                occluder_triangles=occluder_triangles,
            ):
                cached_patch = (
                    'reject',
                    'BLOCKED_VISIBILITY',
                    'source-to-patch-centroid segment is occluded by the exact shell',
                )
            else:
                cached_patch = ('ok', centroid, patch_area, vertices)
        if source_patch_cache is not None:
            source_patch_cache[interaction_key] = cached_patch
    if cached_patch[0] == 'reject':
        return reject(cached_patch[1], cached_patch[2])
    _, centroid, patch_area, vertices = cached_patch

    if _segment_blocked(
        compiled_geometry,
        centroid,
        receiver_point,
        tolerance=tolerance,
        distance_scaled_tolerance=True,
        occluder_triangles=occluder_triangles,
    ):
        return reject(
            'BLOCKED_VISIBILITY',
            'patch-centroid-to-receiver segment is occluded by the exact shell',
        )

    material = (
        material_resolver(plane.material_authority)
        if plane.material_authority is not None
        else None
    )
    if material is None or material.authority_ref != plane.material_authority:
        return reject(
            'UNSUPPORTED_BOUNDARY_QUANTITY',
            'boundary surface exact material authority is unresolved',
        )

    departure = _vector(source_point, centroid)
    arrival = _vector(centroid, receiver_point)
    departure_length = _norm(departure)
    arrival_length = _norm(arrival)
    if (
        departure_length <= tolerance
        or arrival_length <= tolerance
    ):
        return reject(
            'UNSUPPORTED_GEOMETRY',
            'source/receiver lies on the scattering patch within tolerance',
        )
    departure_direction = _unit(departure)
    arrival_direction = _unit(arrival)
    path_length = departure_length + arrival_length
    delay_s = path_length / sound_speed
    d1_min = _min_vertex_distance(source_point, vertices)
    d2_min = _min_vertex_distance(receiver_point, vertices)
    if d1_min <= tolerance or d2_min <= tolerance:
        return reject(
            'UNSUPPORTED_GEOMETRY',
            'source/receiver is coincident with the scattering patch boundary',
        )

    bands: list[LateEnergyBandQuantity] = []
    for center in execution_input.frequency_centers_hz:
        directionality = _directivity_contribution(
            dataset,
            frequency_hz=center,
            source_axis=source_axis,
            departure_direction=departure_direction,
            tolerance=tolerance,
        )
        if directionality is None:
            return reject(
                'UNSUPPORTED_DIRECTIVITY',
                'exact DirectivityDataset does not cover the departure '
                'direction at the declared band center',
            )
        boundary = _material_contribution(
            material,
            plane,
            frequency_hz=center,
            tolerance=tolerance,
        )
        if boundary is None:
            return reject(
                'UNSUPPORTED_BOUNDARY_QUANTITY',
                'boundary material exposes no exact banded scalar GA '
                'absorption/scattering at the declared band center',
            )
        redirected = (
            (1.0 - float(boundary.absorption)) * float(boundary.scattering)
        )
        intercepted = (
            float(directionality.energy_factor)
            * patch_area
            / (4.0 * pi * d1_min * d1_min)
        )
        emergent = 1.0 / (4.0 * pi * d2_min * d2_min)
        bound = intercepted * redirected * emergent
        bands.append(
            LateEnergyBandQuantity(
                center_hz=float(center),
                patch_area_m2=patch_area,
                incident_distance_bound_m=d1_min,
                emergent_distance_bound_m=d2_min,
                redirected_fraction=redirected,
                late_energy_upper_bound_per_m2=bound,
                source_directivity=directionality,
                boundary_material=boundary,
            )
        )

    payload: dict[str, Any] = {
        'source_entity_id': source.source_entity_id,
        'receiver_id': receiver.receiver_id,
        'receiver_entity_id': receiver.entity_id,
        'kind': kind,
        'interaction_surface_id': plane.source_surface_id,
        'interaction_point': _rounded_position(centroid, places).model_dump(
            mode='json'
        ),
        'geometric_path_length_m': _round_float(path_length, places),
        'propagation_delay_s': _round_float(delay_s, places),
        'departure_direction': _rounded_direction(
            departure_direction, places
        ).model_dump(mode='json'),
        'arrival_direction': _rounded_direction(
            arrival_direction, places
        ).model_dump(mode='json'),
        'direction_semantics': (
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        'bands': [
            _prune_late_band_payload(band.model_dump(mode='json'))
            for band in bands
        ],
        'adapter_id': LATE_FIELD_ADAPTER_ID,
        'adapter_version': LATE_FIELD_ADAPTER_VERSION,
        'late_field_implementation_ref': (
            LATE_FIELD_IMPLEMENTATION_REF.model_dump(mode='json')
        ),
        'energy_semantics': 'upper_bound_not_point_estimate',
        'coherent_phase': 'NOT_APPLICABLE_ENERGY_DOMAIN',
        'execution_input_semantic_sha256': execution_input.semantic_sha256,
    }
    digest = _semantic_hash(payload)
    return LateEnergyPathContribution(
        contribution_id=f'late-energy-path:{digest}',
        semantic_sha256=digest,
        **payload,
    )


def _evaluate_diffraction_candidate(
    *,
    execution_input: DeterministicGaExecutionInput,
    late_field_configuration: LateFieldConfiguration,
    compiled_geometry: R120CompiledGeometry,
    edge: DiffractingEdgeRecord,
    source,
    source_point: tuple[float, float, float],
    source_axis: Direction3,
    dataset: DirectivityDataset,
    receiver,
    receiver_point: tuple[float, float, float],
    tolerance: float,
    places: int,
    sound_speed: float,
    kind: LateFieldPathKind,
    occluder_records: tuple | None = None,
) -> LateEnergyPathContribution | LateFieldRejectedCandidate | None:
    interaction_key = edge.edge_key

    def reject(decision: LateFieldCandidateDecision, reason: str) -> LateFieldRejectedCandidate:
        return LateFieldRejectedCandidate(
            source_entity_id=source.source_entity_id,
            receiver_id=receiver.receiver_id,
            kind=kind,
            interaction_key=interaction_key,
            decision=decision,
            reason=reason,
        )

    endpoint_a = _position_tuple(edge.endpoint_a)
    endpoint_b = _position_tuple(edge.endpoint_b)
    try:
        apex, clamped = _diffraction_apex(
            source=source_point,
            receiver=receiver_point,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            tolerance=tolerance,
        )
    except ValueError as exc:
        return reject('UNSUPPORTED_GEOMETRY', str(exc))

    ignored = frozenset(edge.incident_triangle_indices)
    if _segment_blocked_ignoring_triangles(
        compiled_geometry,
        source_point,
        apex,
        tolerance=tolerance,
        ignored_triangle_indices=ignored,
        occluder_records=occluder_records,
    ):
        return reject(
            'EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION',
            'source-to-edge-apex segment is occluded by a non-incident '
            'face; single-edge diffraction is not valid',
        )
    if _segment_blocked_ignoring_triangles(
        compiled_geometry,
        apex,
        receiver_point,
        tolerance=tolerance,
        ignored_triangle_indices=ignored,
        occluder_records=occluder_records,
    ):
        return reject(
            'EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION',
            'edge-apex-to-receiver segment is occluded by a non-incident '
            'face; single-edge diffraction is not valid',
        )

    departure = _vector(source_point, apex)
    arrival = _vector(apex, receiver_point)
    departure_length = _norm(departure)
    arrival_length = _norm(arrival)
    if departure_length <= tolerance or arrival_length <= tolerance:
        return reject(
            'UNSUPPORTED_GEOMETRY',
            'source/receiver is coincident with the diffraction apex',
        )
    departure_direction = _unit(departure)
    arrival_direction = _unit(arrival)
    path_length = departure_length + arrival_length
    delay_s = path_length / sound_speed
    bound_factor = float(
        late_field_configuration.diffraction_energy_bound_factor
    )
    # Conservative bound: minimum endpoint distances overestimate incident
    # and emergent energy everywhere on the declared edge.
    d1_bound = min(
        _distance(source_point, endpoint_a),
        _distance(source_point, endpoint_b),
    )
    d2_bound = min(
        _distance(receiver_point, endpoint_a),
        _distance(receiver_point, endpoint_b),
    )
    if d1_bound <= tolerance or d2_bound <= tolerance:
        return reject(
            'UNSUPPORTED_GEOMETRY',
            'source/receiver is coincident with a declared edge endpoint',
        )

    bands: list[LateEnergyBandQuantity] = []
    for center in execution_input.frequency_centers_hz:
        directionality = _directivity_contribution(
            dataset,
            frequency_hz=center,
            source_axis=source_axis,
            departure_direction=departure_direction,
            tolerance=tolerance,
        )
        if directionality is None:
            return reject(
                'UNSUPPORTED_DIRECTIVITY',
                'exact DirectivityDataset does not cover the apex departure '
                'direction at the declared band center',
            )
        incident = (
            float(directionality.energy_factor)
            / (4.0 * pi * d1_bound * d1_bound)
        )
        emergent = 1.0 / (
            4.0 * pi * d2_bound * d2_bound
        )
        bound = incident * bound_factor * emergent
        bands.append(
            LateEnergyBandQuantity(
                center_hz=float(center),
                incident_distance_bound_m=d1_bound,
                emergent_distance_bound_m=d2_bound,
                redirected_fraction=bound_factor,
                late_energy_upper_bound_per_m2=bound,
                source_directivity=directionality,
            )
        )

    payload: dict[str, Any] = {
        'source_entity_id': source.source_entity_id,
        'receiver_id': receiver.receiver_id,
        'receiver_entity_id': receiver.entity_id,
        'kind': kind,
        'diffracting_edge': edge.model_dump(mode='json'),
        'interaction_point': _rounded_position(apex, places).model_dump(
            mode='json'
        ),
        'apex_clamped_to_segment_endpoint': clamped,
        'geometric_path_length_m': _round_float(path_length, places),
        'propagation_delay_s': _round_float(delay_s, places),
        'departure_direction': _rounded_direction(
            departure_direction, places
        ).model_dump(mode='json'),
        'arrival_direction': _rounded_direction(
            arrival_direction, places
        ).model_dump(mode='json'),
        'direction_semantics': (
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        'bands': [
            _prune_late_band_payload(band.model_dump(mode='json'))
            for band in bands
        ],
        'adapter_id': LATE_FIELD_ADAPTER_ID,
        'adapter_version': LATE_FIELD_ADAPTER_VERSION,
        'late_field_implementation_ref': (
            LATE_FIELD_IMPLEMENTATION_REF.model_dump(mode='json')
        ),
        'energy_semantics': 'upper_bound_not_point_estimate',
        'coherent_phase': 'NOT_APPLICABLE_ENERGY_DOMAIN',
        'execution_input_semantic_sha256': execution_input.semantic_sha256,
    }
    digest = _semantic_hash(payload)
    return LateEnergyPathContribution(
        contribution_id=f'late-energy-path:{digest}',
        semantic_sha256=digest,
        **payload,
    )


def late_energy_observable_manifest(
    artifact: LateFieldEnergyArtifact,
) -> AcousticSolverObservableArtifact:
    """Expose a persisted late-field artifact as the R160 late-energy observable."""
    return AcousticSolverObservableArtifact(
        observable=LATE_ENERGY_DECAY_OBSERVABLE,
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=LATE_FIELD_ARTIFACT_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )


def build_late_field_result_envelope(
    *,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    artifact: LateFieldEnergyArtifact,
    completed_at_utc: str,
    artifact_manifest_resolver=None,
) -> AcousticSolverResultEnvelope:
    """Bind the late-field artifact to its exact READY dispatch as a solver result.

    The request must declare the ``late_energy_decay`` observable; the envelope
    authority enforces the exact observable-set match.
    """
    manifest = late_energy_observable_manifest(artifact)
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
                    observable=LATE_ENERGY_DECAY_OBSERVABLE,
                    encoding_schema_ref=LATE_FIELD_ARTIFACT_SCHEMA_REF,
                    valid_frequency_domain=artifact.frequency_domain,
                    solver_lineage={
                        'execution_id': artifact.execution_id,
                        'execution_input_id': artifact.execution_input_id,
                        'execution_input_sha256': artifact.execution_input_sha256,
                        'dispatch_binding_id': artifact.dispatch_binding_id,
                        'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
                        'late_field_configuration_id': (
                            artifact.late_field_configuration_ref.authority_id
                        ),
                        'late_field_configuration_sha256': (
                            artifact.late_field_configuration_ref.semantic_hash_sha256
                        ),
                    },
                )
                if ref == artifact.as_external_ref()
                else None
            )
        ),
    )


LateFieldConfigurationResolver = Callable[
    [ExactExternalAuthorityRef], LateFieldConfiguration | None
]


class CadLateFieldEnergyArtifactRepository:
    """Persisted late-field artifact store with full stale revalidation.

    Reopening re-resolves every exact authority the artifact binds — snapshot,
    prediction request, READY dispatch, adapter descriptor, GA configuration,
    late-field configuration, compiled geometry + topology, region/Portal/
    termination authorities — and regenerates the artifact deterministically
    from them. Any drift fails closed.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver: ConfigurationResolver,
        late_field_configuration_resolver: LateFieldConfigurationResolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
        material_resolver: MaterialAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.late_field_configuration_resolver = late_field_configuration_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.material_resolver = material_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'late-field artifact and {label} repositories must share one CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_late_field_artifacts')

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        item = self.geometry_authority_resolver(ref)
        if item is None or not isinstance(item, expected_type):
            raise ValueError(f'late-field artifact exact {label} authority is missing')
        if _authority_ref(item) != ref:
            raise ValueError(f'late-field artifact exact {label} authority mismatch')
        return item

    def _validate(self, artifact: LateFieldEnergyArtifact) -> LateFieldEnergyArtifact:
        artifact = LateFieldEnergyArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        execution_input = self._load_execution_input(artifact)
        snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if snapshot is None or snapshot.semantic_sha256 != artifact.snapshot_sha256:
            raise ValueError('late-field artifact exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            artifact.prediction_request_id,
            _validated_snapshot=snapshot,
        )
        if (
            request is None
            or request.request_semantic_sha256 != artifact.prediction_request_sha256
        ):
            raise ValueError(
                'late-field artifact exact prediction request is missing or mismatched'
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
                'late-field artifact exact READY dispatch is missing or mismatched'
            )
        descriptor = self.dispatch_repository.get_descriptor(artifact.adapter_descriptor_id)
        if (
            descriptor is None
            or descriptor.semantic_sha256 != artifact.adapter_descriptor_sha256
        ):
            raise ValueError(
                'late-field artifact exact adapter descriptor is missing or mismatched'
            )
        if (
            artifact.solver_implementation_ref != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref != dispatch.solver_configuration_ref
        ):
            raise ValueError(
                'late-field artifact solver implementation/config mismatch'
            )

        configuration = self.configuration_resolver(artifact.solver_configuration_ref)
        if (
            configuration is None
            or configuration.as_external_ref() != artifact.solver_configuration_ref
        ):
            raise ValueError(
                'late-field artifact exact GA configuration is missing or mismatched'
            )
        late_field_configuration = self.late_field_configuration_resolver(
            artifact.late_field_configuration_ref
        )
        if (
            late_field_configuration is None
            or late_field_configuration.as_external_ref()
            != artifact.late_field_configuration_ref
        ):
            raise ValueError(
                'late-field artifact exact late-field configuration is missing '
                'or mismatched'
            )

        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            artifact.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256 != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError(
                'late-field artifact exact R120 geometry is missing or mismatched'
            )
        if compiled.region_authority_ref is None:
            raise ValueError('late-field artifact R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('late-field artifact R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError(
                'late-field artifact R120 termination authority is missing'
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
                    'late-field artifact exact DirectivityDataset is missing'
                )
            datasets.append(dataset)

        regenerated = execute_late_field_energy(
            execution_input=execution_input,
            late_field_configuration=late_field_configuration,
            compiled_geometry=compiled,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=termination_authority,
            directivity_datasets=datasets,
            material_resolver=self.material_resolver,
        )
        if regenerated != artifact:
            raise ValueError(
                'late-field artifact does not reproduce from exact current authorities'
            )
        return artifact

    def _load_execution_input(
        self,
        artifact: LateFieldEnergyArtifact,
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
                'late-field artifact exact GA execution input is missing'
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
                'late-field artifact exact GA execution input is mismatched'
            )
        return execution_input

    def save(self, artifact: LateFieldEnergyArtifact) -> LateFieldEnergyArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_late_field_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = LateFieldEnergyArtifact.model_validate_json(
                    existing['payload_json']
                )
                if persisted != artifact:
                    raise ValueError(
                        'late-field artifact id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_late_field_artifacts(
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

    def get(self, artifact_id: str) -> LateFieldEnergyArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_late_field_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            LateFieldEnergyArtifact.model_validate_json(row['payload_json'])
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == LATE_FIELD_ARTIFACT_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith('late-field-energy-artifact:'):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith('r150-late-field-execution-provenance:'):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_late_field_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                LateFieldEnergyArtifact.model_validate_json(row['payload_json'])
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None

    def resolve_artifact_manifest(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverArtifactManifest | None:
        """Resolve the persisted late-field artifact as a typed solver manifest."""
        if not ref.authority_id.startswith('late-field-energy-artifact:'):
            return None
        artifact = self.get(ref.authority_id)
        if artifact is None or artifact.as_external_ref() != ref:
            return None
        return AcousticSolverArtifactManifest(
            artifact_ref=ref,
            observable=LATE_ENERGY_DECAY_OBSERVABLE,
            encoding_schema_ref=LATE_FIELD_ARTIFACT_SCHEMA_REF,
            valid_frequency_domain=artifact.frequency_domain,
            solver_lineage={
                'execution_id': artifact.execution_id,
                'execution_input_id': artifact.execution_input_id,
                'execution_input_sha256': artifact.execution_input_sha256,
                'dispatch_binding_id': artifact.dispatch_binding_id,
                'dispatch_binding_sha256': artifact.dispatch_binding_sha256,
                'late_field_configuration_id': (
                    artifact.late_field_configuration_ref.authority_id
                ),
                'late_field_configuration_sha256': (
                    artifact.late_field_configuration_ref.semantic_hash_sha256
                ),
            },
        )
