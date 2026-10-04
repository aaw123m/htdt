"""Explicit Architectural -> Derivation -> SolverGeometry provenance record.

The solver input chain already persists every hop as an independent typed
authority (RawVisualMesh -> SemanticGeometryConversionRequest ->
SemanticAcousticGeometry -> R120GeometryCompilationRequest ->
R120CompiledGeometry). What was missing is one auditable record that names the
chain end to end and enumerates the derivation decision categories
(removed/simplified features, merged faces, opening treatment, edge/diffraction
treatment, scattering substitution) as typed fields.

Fail-closed rules:
* every category decision is declared exactly once;
* categories the chain has no mechanism for (merged faces, edge diffraction
  treatment, scattering substitution) must be NOT_APPLIED — the record cannot
  claim them silently;
* applied categories must carry their typed payloads verbatim from the
  compiled-geometry authority (they are projections, not new claims);
* the record id binds the full payload hash, so a persisted row is
  self-verifying against tampering.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _semantic_hash
from .r120_geometry_compiler import (
    ApproximationOperation,
    BoundaryTerminationAuthority,
    DroppedFeature,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
)
from .semantic_geometry import SemanticAcousticGeometry

ACOUSTIC_GEOMETRY_DERIVATION_AUTHORITY_VERSION = '1'

GEOMETRY_DERIVATION_CATEGORIES: tuple[str, ...] = (
    'removed_or_simplified_features',
    'merged_faces',
    'opening_treatment',
    'edge_diffraction_treatment',
    'scattering_substitution',
)

# Categories with no mechanism in the chain today. They are recorded as
# NOT_APPLIED so results carry the honest limit; declaring them APPLIED is a
# schema violation until the pipeline actually performs them.
_NEVER_APPLIED_CATEGORIES = frozenset(
    {'merged_faces', 'edge_diffraction_treatment', 'scattering_substitution'}
)

GeometryDerivationCategory = Literal[
    'removed_or_simplified_features',
    'merged_faces',
    'opening_treatment',
    'edge_diffraction_treatment',
    'scattering_substitution',
]


class GeometryDerivationCategoryDecision(BaseModel):
    """One derivation category decision; an absent mechanism is declared."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    category: GeometryDerivationCategory
    state: Literal['APPLIED', 'NOT_APPLIED']
    detail: str = Field(min_length=1)


class AcousticGeometryDerivation(BaseModel):
    """End-to-end solver-geometry provenance and decision record.

    Every hop is bound by exact id + semantic hash. Category payloads are
    verbatim projections of the compiled-geometry authority — this record adds
    the enumeration and audit surface, never new geometry claims.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['1'] = ACOUSTIC_GEOMETRY_DERIVATION_AUTHORITY_VERSION
    derivation_id: str = Field(
        pattern=r'^acoustic-geometry-derivation:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    # Architectural input hop.
    scene_revision_id: str = Field(min_length=1)
    scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    input_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_diagnostic_id: str = Field(
        pattern=r'^(?:raw-mesh-diagnostic|repaired-raw-mesh-diagnostic):[0-9a-f]{64}$'
    )
    input_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_mesh_repair_plan_id: str | None = Field(
        default=None, pattern=r'^raw-mesh-repair-plan:[0-9a-f]{64}$'
    )

    # Derivation hop (architectural -> acoustic semantic geometry).
    semantic_geometry_id: str = Field(
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$'
    )
    semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    conversion_request_id: str = Field(
        pattern=r'^semantic-geometry-request:[0-9a-f]{64}$'
    )
    conversion_profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repair_lineage_step_ids: tuple[str, ...]
    semantic_unresolved_conditions: tuple[str, ...]

    # Solver-geometry hop (semantic -> compiled solver geometry).
    r120_compile_request_id: str = Field(
        pattern=r'^r120-geometry-compile-request:[0-9a-f]{64}$'
    )
    r120_compiled_geometry_id: str = Field(
        pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$'
    )
    r120_compiled_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compiler_id: Literal['htdt.r120.solver_neutral_geometry_compiler']
    compiler_version: Literal['1']
    geometric_tolerance_m: float = Field(gt=0)
    approximation_policy: str = Field(min_length=1)
    tiny_feature_policy: str = Field(min_length=1)
    coplanar_handling_policy: str = Field(min_length=1)
    surface_triangulation_policy: str = Field(min_length=1)

    # Derivation decision categories — declared once each, fail-closed.
    category_decisions: tuple[GeometryDerivationCategoryDecision, ...]

    # Typed category payloads (verbatim projections when APPLIED).
    approximation_operations: tuple[ApproximationOperation, ...]
    dropped_features: tuple[DroppedFeature, ...]
    closed_shell: bool
    boundary_edge_count: int = Field(ge=0)
    non_manifold_edge_count: int = Field(ge=0)
    declared_portal_ids: tuple[str, ...]
    declared_termination_ids: tuple[str, ...]
    portal_authority_ref: ExactExternalAuthorityRef | None = None
    boundary_termination_authority_ref: ExactExternalAuthorityRef | None = None
    approximation_error_bound_m: float | None = Field(default=None, ge=0)
    approximation_error_status: Literal[
        'exact_preservation', 'not_computed_for_dropped_features'
    ]
    maximum_dropped_feature_extent_m: float = Field(ge=0)
    compiler_warnings: tuple[str, ...]
    solver_unresolved_conditions: tuple[str, ...]

    @model_validator(mode='after')
    def validate_record(self) -> 'AcousticGeometryDerivation':
        categories = [item.category for item in self.category_decisions]
        if sorted(categories) != sorted(GEOMETRY_DERIVATION_CATEGORIES):
            raise ValueError(
                'derivation record must declare every category exactly once: '
                f'{sorted(GEOMETRY_DERIVATION_CATEGORIES)}'
            )
        by_category = {item.category: item for item in self.category_decisions}
        for category in _NEVER_APPLIED_CATEGORIES:
            if by_category[category].state != 'NOT_APPLIED':
                raise ValueError(
                    f'derivation category {category} has no mechanism in the '
                    'chain and cannot be declared APPLIED'
                )
        removed = by_category['removed_or_simplified_features']
        removed_payloads = bool(
            self.approximation_operations or self.dropped_features
        )
        if removed.state == 'APPLIED' and not removed_payloads:
            raise ValueError(
                'removed_or_simplified_features APPLIED requires typed '
                'approximation/dropped-feature payloads'
            )
        if removed.state == 'NOT_APPLIED' and removed_payloads:
            raise ValueError(
                'removed_or_simplified_features payload present but decision '
                'is NOT_APPLIED'
            )
        if by_category['opening_treatment'].state != 'APPLIED':
            raise ValueError(
                'opening treatment is always decided by the chain '
                '(closed-shell diagnostics plus portal authority), so the '
                'decision must be APPLIED'
            )
        if not self.declared_portal_ids and self.portal_authority_ref is not None:
            pass  # explicit_none portal authority is legitimate
        payload = self.model_dump(
            mode='json', exclude={'derivation_id', 'semantic_sha256'}
        )
        expected = _semantic_hash(payload)
        if self.semantic_sha256 != expected:
            raise ValueError('acoustic geometry derivation hash mismatch')
        if self.derivation_id != f'acoustic-geometry-derivation:{expected}':
            raise ValueError('acoustic geometry derivation id mismatch')
        return self


def build_acoustic_geometry_derivation(
    *,
    semantic_geometry: SemanticAcousticGeometry,
    compiled_geometry: R120CompiledGeometry,
    portal_authority: PortalAuthority | None = None,
    boundary_termination_authority: BoundaryTerminationAuthority | None = None,
) -> AcousticGeometryDerivation:
    """Project the persisted hop authorities into one auditable record.

    The builder refuses (fail-closed) when the supplied authorities do not
    bind exactly: the compiled geometry must name this semantic geometry and
    scene revision by id and hash.
    """

    request = compiled_geometry.request
    if compiled_geometry.exact_semantic_geometry_id != semantic_geometry.geometry_id:
        raise ValueError('compiled geometry does not bind this semantic geometry id')
    if (
        compiled_geometry.exact_semantic_geometry_hash_sha256
        != semantic_geometry.semantic_hash_sha256
    ):
        raise ValueError('compiled geometry does not bind this semantic geometry hash')
    if request.semantic_geometry_id != semantic_geometry.geometry_id:
        raise ValueError('compile request semantic geometry id mismatch')
    conversion = semantic_geometry.conversion_request
    if semantic_geometry.conversion_request_id != conversion.request_id:
        raise ValueError('semantic geometry conversion request binding mismatch')
    lineage = conversion.raw_mesh_repair_lineage
    repair_plan_id = lineage.repair_plan_id if lineage is not None else None

    diagnostics = compiled_geometry.closed_shell_diagnostics
    declared_portal_ids: tuple[str, ...] = ()
    declared_termination_ids: tuple[str, ...] = ()
    if portal_authority is not None:
        if compiled_geometry.portal_authority_ref is None:
            raise ValueError(
                'portal authority supplied but compiled geometry declares none'
            )
        if (
            portal_authority.authority_id
            != compiled_geometry.portal_authority_ref.authority_id
            or portal_authority.authority_version
            != compiled_geometry.portal_authority_ref.authority_version
            or portal_authority.semantic_hash_sha256
            != compiled_geometry.portal_authority_ref.semantic_hash_sha256
        ):
            raise ValueError('portal authority does not match compiled geometry ref')
        declared_portal_ids = tuple(
            item.portal_id for item in portal_authority.declarations
        )
    if boundary_termination_authority is not None:
        if compiled_geometry.boundary_termination_authority_ref is None:
            raise ValueError(
                'boundary termination authority supplied but compiled '
                'geometry declares none'
            )
        ref = compiled_geometry.boundary_termination_authority_ref
        if (
            boundary_termination_authority.authority_id != ref.authority_id
            or boundary_termination_authority.authority_version != ref.authority_version
            or boundary_termination_authority.semantic_hash_sha256
            != ref.semantic_hash_sha256
        ):
            raise ValueError(
                'boundary termination authority does not match compiled '
                'geometry ref'
            )
        declared_termination_ids = tuple(
            item.termination_id
            for item in boundary_termination_authority.declarations
        )

    removed_applied = bool(
        compiled_geometry.approximation_operations
        or compiled_geometry.dropped_features
    )
    removed_detail = (
        'approximation policy applied: '
        f'{len(compiled_geometry.approximation_operations)} operation(s), '
        f'{len(compiled_geometry.dropped_features)} dropped feature(s)'
        if removed_applied
        else 'no features removed or simplified: approximation policy '
        f'{compiled_geometry.request.approximation_policy}, '
        f'tiny-feature policy {compiled_geometry.request.tiny_feature_policy}'
    )
    opening_detail = (
        'openings classified by closed-shell diagnostics '
        f'(closed_shell={diagnostics.closed_shell}, '
        f'boundary_edges={diagnostics.boundary_edge_count}, '
        f'non_manifold_edges={diagnostics.non_manifold_edge_count}); '
        f'{len(declared_portal_ids)} declared portal(s), '
        f'{len(declared_termination_ids)} declared termination(s)'
    )
    decisions = (
        GeometryDerivationCategoryDecision(
            category='removed_or_simplified_features',
            state='APPLIED' if removed_applied else 'NOT_APPLIED',
            detail=removed_detail,
        ),
        GeometryDerivationCategoryDecision(
            category='merged_faces',
            state='NOT_APPLIED',
            detail='no face-merging mechanism exists in the derivation chain',
        ),
        GeometryDerivationCategoryDecision(
            category='opening_treatment',
            state='APPLIED',
            detail=opening_detail,
        ),
        GeometryDerivationCategoryDecision(
            category='edge_diffraction_treatment',
            state='NOT_APPLIED',
            detail=(
                'the derivation chain performs no edge/diffraction treatment; '
                'bounded diffraction consumes separately declared edges and '
                'coherent diffraction is not modeled'
            ),
        ),
        GeometryDerivationCategoryDecision(
            category='scattering_substitution',
            state='NOT_APPLIED',
            detail='no scattering-substitution mechanism exists in the chain',
        ),
    )

    payload = {
        'authority_version': ACOUSTIC_GEOMETRY_DERIVATION_AUTHORITY_VERSION,
        'scene_revision_id': compiled_geometry.exact_scene_revision_id,
        'scene_revision_content_hash': (
            compiled_geometry.exact_scene_revision_content_hash
        ),
        'input_raw_mesh_id': semantic_geometry.input_raw_mesh_id,
        'input_raw_mesh_semantic_hash': (
            semantic_geometry.input_raw_mesh_semantic_hash
        ),
        'input_diagnostic_id': semantic_geometry.input_diagnostic_id,
        'input_diagnostic_semantic_hash': (
            semantic_geometry.input_diagnostic_semantic_hash
        ),
        'raw_mesh_repair_plan_id': repair_plan_id,
        'semantic_geometry_id': semantic_geometry.geometry_id,
        'semantic_geometry_hash_sha256': semantic_geometry.semantic_hash_sha256,
        'conversion_request_id': conversion.request_id,
        'conversion_profile_semantic_hash': (
            semantic_geometry.conversion_profile_semantic_hash
        ),
        'repair_lineage_step_ids': [
            item.operation_id for item in semantic_geometry.repair_lineage
        ],
        'semantic_unresolved_conditions': list(
            semantic_geometry.unresolved_conditions
        ),
        'r120_compile_request_id': request.request_id,
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_hash_sha256': (
            compiled_geometry.compiled_hash_sha256
        ),
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'compiler_id': request.compiler_id,
        'compiler_version': request.compiler_version,
        'geometric_tolerance_m': compiled_geometry.geometric_tolerance_m,
        'approximation_policy': request.approximation_policy,
        'tiny_feature_policy': request.tiny_feature_policy,
        'coplanar_handling_policy': request.coplanar_handling_policy,
        'surface_triangulation_policy': request.surface_triangulation_policy,
        'category_decisions': [
            item.model_dump(mode='json') for item in decisions
        ],
        'approximation_operations': [
            item.model_dump(mode='json')
            for item in compiled_geometry.approximation_operations
        ],
        'dropped_features': [
            item.model_dump(mode='json')
            for item in compiled_geometry.dropped_features
        ],
        'closed_shell': diagnostics.closed_shell,
        'boundary_edge_count': diagnostics.boundary_edge_count,
        'non_manifold_edge_count': diagnostics.non_manifold_edge_count,
        'declared_portal_ids': list(declared_portal_ids),
        'declared_termination_ids': list(declared_termination_ids),
        'portal_authority_ref': (
            compiled_geometry.portal_authority_ref.model_dump(mode='json')
            if compiled_geometry.portal_authority_ref is not None
            else None
        ),
        'boundary_termination_authority_ref': (
            compiled_geometry.boundary_termination_authority_ref.model_dump(
                mode='json'
            )
            if compiled_geometry.boundary_termination_authority_ref is not None
            else None
        ),
        'approximation_error_bound_m': (
            compiled_geometry.approximation_error_bound_m
        ),
        'approximation_error_status': (
            compiled_geometry.approximation_error_status
        ),
        'maximum_dropped_feature_extent_m': (
            compiled_geometry.maximum_dropped_feature_extent_m
        ),
        'compiler_warnings': list(compiled_geometry.compiler_warnings),
        'solver_unresolved_conditions': list(
            compiled_geometry.unresolved_conditions
        ),
    }
    digest = _semantic_hash(payload)
    return AcousticGeometryDerivation(
        derivation_id=f'acoustic-geometry-derivation:{digest}',
        semantic_sha256=digest,
        **payload,
    )
