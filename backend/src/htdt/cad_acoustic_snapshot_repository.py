from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import NamedTuple

from .cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticSceneSnapshot,
    ReceiverMeasurementAuthority,
    SnapshotEnvironmentAuthorityRef,
    SurfaceBoundaryConfiguration,
    TreatmentBoundaryOverlaySnapshotRef,
    _derive_readiness,
    _derive_unresolved_conditions,
    _require_geometric_topology_preflight_authority,
    _snapshot_schema_version,
    source_binding_from_r110,
)
from .cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
    NumericalFidelityPolicyResolver,
    numerical_fidelity_policy_request_reasons,
)
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_portal import GeometricPortalGraph
from .cad_r110_source_repository import CadR110SourceRepository
from .cad_repository import SceneRepository
from .cad_scene import acoustic_reference_position
from .cad_schema import ensure_native_schema
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_wave_excitation import CadWaveExcitationRepository
from .r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    R120CompiledGeometry,
)
from .r120_geometry_compiler_repository import R120GeometryCompilerRepository
from .treatment_boundary_overlay_repository import TreatmentBoundaryOverlayRepository


SnapshotEnvironmentResolver = Callable[
    [ExactExternalAuthorityRef],
    SnapshotEnvironmentAuthorityRef | None,
]
SnapshotScalarAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    float | None,
]
SnapshotReceiverMeasurementResolver = Callable[
    [ExactExternalAuthorityRef],
    ReceiverMeasurementAuthority | None,
]
SnapshotFrequencyDomainResolver = Callable[
    [ExactExternalAuthorityRef],
    FrequencyDomain | None,
]
SnapshotTopologyPreflightResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometricPortalGraph | None,
]
SnapshotExternalAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    ExactExternalAuthorityRef | None,
]


class AcousticSnapshotAuthorityResolvers(NamedTuple):
    """Typed external-authority resolver registry for AcousticSceneSnapshot.

    Each resolver maps an exact authority ref carried by a snapshot to the
    canonical external record (or attested value) it still resolves to,
    returning None when the authority does not exist. A snapshot carrying a
    ref category without a configured resolver fails closed on save and on
    every authoritative read; an opaque ref never opens readiness.
    """

    environment: SnapshotEnvironmentResolver | None = None
    sound_speed_source: SnapshotScalarAuthorityResolver | None = None
    temperature_source: SnapshotScalarAuthorityResolver | None = None
    receiver_measurement: SnapshotReceiverMeasurementResolver | None = None
    valid_frequency_domain: SnapshotFrequencyDomainResolver | None = None
    geometric_topology_preflight: SnapshotTopologyPreflightResolver | None = None
    external_authority: SnapshotExternalAuthorityResolver | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


class CadAcousticSnapshotRepository:
    """Append-only exact AcousticSceneSnapshot and request persistence.

    External environment, receiver-measurement, frequency-domain, topology
    preflight and surface material/boundary authorities are exact external
    inputs. They are re-resolved through the caller-provided
    ``authority_resolvers`` registry on every save and every authoritative
    read; an opaque stored hash alone is not treated as proof that the
    external authority still exists with the same semantics. Snapshots
    carrying such refs fail closed when the matching resolver is missing.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        variant_repository: CadSystemVariantRepository | None = None,
        r110_repository: CadR110SourceRepository | None = None,
        r120_repository: R120GeometryCompilerRepository | None = None,
        treatment_boundary_repository: TreatmentBoundaryOverlayRepository | None = None,
        wave_excitation_repository: CadWaveExcitationRepository | None = None,
        fidelity_policy_resolver: NumericalFidelityPolicyResolver | None = None,
        authority_resolvers: AcousticSnapshotAuthorityResolvers | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.authority_resolvers = (
            authority_resolvers
            if authority_resolvers is not None
            else AcousticSnapshotAuthorityResolvers()
        )
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.r110_repository = (
            r110_repository
            if r110_repository is not None
            else CadR110SourceRepository(
                scene_repository,
                variant_repository=self.variant_repository,
            )
        )
        self.r120_repository = (
            r120_repository
            if r120_repository is not None
            else R120GeometryCompilerRepository(scene_repository)
        )
        self.treatment_boundary_repository = treatment_boundary_repository
        self.fidelity_policy_resolver = fidelity_policy_resolver
        self.wave_excitation_repository = (
            wave_excitation_repository
            if wave_excitation_repository is not None
            else CadWaveExcitationRepository(
                scene_repository,
                r110_repository=self.r110_repository,
            )
        )
        self.path = Path(scene_repository.path)
        if Path(self.wave_excitation_repository.path) != self.path:
            raise ValueError(
                'AcousticSceneSnapshot and wave-excitation repositories '
                'must share one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_scene_snapshots (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    system_variant_id TEXT,
                    system_variant_sha256 TEXT,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    r120_compiled_geometry_sha256 TEXT NOT NULL,
                    material_boundary_configuration_sha256 TEXT NOT NULL,
                    environment_authority_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(system_variant_id)
                        REFERENCES cad_system_variants(variant_id),
                    FOREIGN KEY(r120_compiled_geometry_id)
                        REFERENCES cad_r120_compiled_geometry(compiled_geometry_id)
                );
                CREATE INDEX IF NOT EXISTS idx_acoustic_snapshot_scene
                    ON cad_acoustic_scene_snapshots(scene_revision_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_acoustic_prediction_requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    request_semantic_sha256 TEXT NOT NULL UNIQUE,
                    acoustic_scene_snapshot_id TEXT NOT NULL,
                    acoustic_scene_snapshot_sha256 TEXT NOT NULL,
                    deterministic_input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(acoustic_scene_snapshot_id)
                        REFERENCES cad_acoustic_scene_snapshots(snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_acoustic_request_snapshot
                    ON cad_acoustic_prediction_requests(
                        acoustic_scene_snapshot_id, seq ASC
                    );
                """
            )

    def _validate_snapshot(
        self,
        snapshot: AcousticSceneSnapshot,
    ) -> AcousticSceneSnapshot:
        snapshot = AcousticSceneSnapshot.model_validate(
            snapshot.model_dump(mode='python')
        )

        revision = self.scene_repository.get(snapshot.scene_revision_id)
        if revision is None:
            raise ValueError(
                'AcousticSceneSnapshot references missing SceneRevision'
            )
        if (
            revision.document_id != snapshot.document_id
            or revision.content_hash != snapshot.scene_content_hash
        ):
            raise ValueError(
                'AcousticSceneSnapshot exact SceneRevision binding mismatch'
            )
        geometry = revision.document.r120_semantic_geometry
        if geometry is None:
            raise ValueError(
                'AcousticSceneSnapshot SceneRevision has no SemanticAcousticGeometry'
            )
        if (
            geometry.geometry_id != snapshot.semantic_geometry_id
            or geometry.semantic_hash_sha256
            != snapshot.semantic_geometry_sha256
        ):
            raise ValueError(
                'AcousticSceneSnapshot SemanticAcousticGeometry binding mismatch'
            )

        variant = None
        if snapshot.system_variant_id is not None:
            variant = self.variant_repository.get_variant(
                snapshot.system_variant_id
            )
            if variant is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing SystemVariant'
                )
            if variant.variant_sha256 != snapshot.system_variant_sha256:
                raise ValueError(
                    'AcousticSceneSnapshot SystemVariant hash mismatch'
                )
            if (
                variant.document_id != revision.document_id
                or variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError(
                    'AcousticSceneSnapshot SystemVariant/SceneRevision mismatch'
                )

        compiled = self.r120_repository.get_compiled_geometry(
            snapshot.r120_compiled_geometry_id
        )
        if compiled is None:
            raise ValueError(
                'AcousticSceneSnapshot references missing R120CompiledGeometry'
            )
        if (
            compiled.compiled_hash_sha256
            != snapshot.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != snapshot.compiled_topology_sha256
            or compiled.exact_scene_revision_id != revision.revision_id
            or compiled.exact_scene_revision_content_hash
            != revision.content_hash
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120CompiledGeometry binding mismatch'
            )
        if (
            compiled.exact_semantic_geometry_id != snapshot.semantic_geometry_id
            or compiled.exact_semantic_geometry_hash_sha256
            != snapshot.semantic_geometry_sha256
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120/SemanticAcousticGeometry mismatch'
            )
        if (
            compiled.geometric_tolerance_m != snapshot.geometric_tolerance_m
            or compiled.approximation_error_bound_m
            != snapshot.approximation_error_bound_m
            or compiled.approximation_error_status
            != snapshot.approximation_error_status
            or compiled.maximum_dropped_feature_extent_m
            != snapshot.maximum_dropped_feature_extent_m
        ):
            raise ValueError(
                'AcousticSceneSnapshot R120 approximation metadata mismatch'
            )
        expected_surface_configuration = tuple(
            SurfaceBoundaryConfiguration(
                source_surface_id=item.source_surface_id,
                material_authority=item.material_authority,
                boundary_physics_authority=item.boundary_physics_authority,
            )
            for item in sorted(
                compiled.surface_mapping,
                key=lambda item: item.source_surface_id,
            )
        )
        if (
            expected_surface_configuration
            != snapshot.surface_boundary_configuration
        ):
            raise ValueError(
                'AcousticSceneSnapshot material/boundary configuration mismatch'
            )
        if compiled.region_authority_ref != snapshot.acoustic_region_authority_ref:
            raise ValueError(
                'AcousticSceneSnapshot AcousticRegion authority mismatch'
            )
        if compiled.portal_authority_ref != snapshot.portal_authority_ref:
            raise ValueError(
                'AcousticSceneSnapshot Portal authority mismatch'
            )
        if (
            compiled.boundary_termination_authority_ref
            != snapshot.boundary_termination_authority_ref
        ):
            raise ValueError(
                'AcousticSceneSnapshot BoundaryTermination authority mismatch'
            )

        if snapshot.treatment_boundary_bindings:
            if self.treatment_boundary_repository is None:
                raise ValueError(
                    'AcousticSceneSnapshot treatment bindings require typed '
                    'TreatmentBoundaryOverlayRepository resolution'
                )
            mapping_by_surface = {
                item.source_surface_id: item
                for item in compiled.surface_mapping
            }
            for binding in snapshot.treatment_boundary_bindings:
                mapping = mapping_by_surface.get(binding.host_surface_id)
                if mapping is None:
                    raise ValueError(
                        'AcousticSceneSnapshot treatment host surface is missing '
                        'from R120CompiledGeometry'
                    )
                if (
                    binding.base_material_authority != mapping.material_authority
                    or binding.base_boundary_physics_authority
                    != mapping.boundary_physics_authority
                ):
                    raise ValueError(
                        'AcousticSceneSnapshot treatment base boundary mismatch'
                    )

                resolved_overlays = []
                for overlay_binding in binding.attached_treatment_overlays:
                    overlay = self.treatment_boundary_repository.get_overlay(
                        overlay_binding.overlay_ref.authority_id
                    )
                    if overlay is None:
                        raise ValueError(
                            'AcousticSceneSnapshot references missing treatment overlay'
                        )
                    expected_overlay_binding = TreatmentBoundaryOverlaySnapshotRef(
                        overlay_ref=overlay.as_external_authority_ref(),
                        host_surface_authority_sha256=(
                            overlay.host_surface_authority_sha256
                        ),
                        lifecycle=overlay.lifecycle,
                        treatment_definition_id=overlay.treatment_definition_id,
                        treatment_definition_version=(
                            overlay.treatment_definition_version
                        ),
                        treatment_definition_hash_sha256=(
                            overlay.treatment_definition_hash_sha256
                        ),
                        treatment_placement_instance_id=(
                            overlay.treatment_placement_instance_id
                        ),
                        treatment_placement_version=(
                            overlay.treatment_placement_version
                        ),
                        treatment_placement_hash_sha256=(
                            overlay.treatment_placement_hash_sha256
                        ),
                        surface_binding_evaluation_id=(
                            overlay.surface_binding_evaluation_id
                        ),
                        surface_binding_evaluation_hash_sha256=(
                            overlay.surface_binding_evaluation_hash_sha256
                        ),
                        wave_capability_state=overlay.wave_capability_state,
                        geometric_capability_state=(
                            overlay.geometric_capability_state
                        ),
                    )
                    if (
                        expected_overlay_binding != overlay_binding
                        or overlay.host_surface_id != binding.host_surface_id
                        or overlay.exact_scene_revision_id != revision.revision_id
                        or overlay.exact_scene_revision_content_hash
                        != revision.content_hash
                        or overlay.exact_semantic_geometry_id != geometry.geometry_id
                        or overlay.exact_semantic_geometry_hash_sha256
                        != geometry.semantic_hash_sha256
                        or overlay.exact_r120_compiled_geometry_id
                        != compiled.compiled_geometry_id
                        or overlay.exact_r120_compiled_geometry_hash_sha256
                        != compiled.compiled_hash_sha256
                    ):
                        raise ValueError(
                            'AcousticSceneSnapshot treatment overlay exact identity mismatch'
                        )
                    resolved_overlays.append(overlay)

                if binding.status == 'AVAILABLE':
                    assert binding.composition_id is not None
                    composition = self.treatment_boundary_repository.get_composition(
                        binding.composition_id
                    )
                    if composition is None:
                        raise ValueError(
                            'AcousticSceneSnapshot references missing treatment composition'
                        )
                    if (
                        composition.composition_hash_sha256
                        != binding.composition_hash_sha256
                        or composition.authority_version
                        != binding.composition_authority_version
                        or composition.target_domain != binding.target_domain
                        or composition.host_surface_id != binding.host_surface_id
                        or composition.exact_scene_revision_id
                        != revision.revision_id
                        or composition.exact_scene_revision_content_hash
                        != revision.content_hash
                        or composition.exact_semantic_geometry_id
                        != geometry.geometry_id
                        or composition.exact_semantic_geometry_hash_sha256
                        != geometry.semantic_hash_sha256
                        or composition.exact_r120_compiled_geometry_id
                        != compiled.compiled_geometry_id
                        or composition.exact_r120_compiled_geometry_hash_sha256
                        != compiled.compiled_hash_sha256
                        or composition.selected_treatment_lifecycle
                        != binding.lifecycle
                        or composition.base_material_authority
                        != binding.base_material_authority
                        or composition.base_boundary_physics_authority
                        != binding.base_boundary_physics_authority
                        or composition.attached_treatment_overlays
                        != tuple(
                            item.overlay_ref
                            for item in binding.attached_treatment_overlays
                        )
                        or composition.selected_treatment_material_authorities
                        != binding.selected_treatment_material_authorities
                    ):
                        raise ValueError(
                            'AcousticSceneSnapshot treatment composition exact identity mismatch'
                        )
                    for overlay in resolved_overlays:
                        expected_material = (
                            overlay.wave_material_candidate_ref
                            if binding.target_domain == 'wave'
                            else overlay.geometric_material_candidate_ref
                        )
                        if (
                            expected_material is None
                            or expected_material
                            not in binding.selected_treatment_material_authorities
                            or overlay.lifecycle != binding.lifecycle
                        ):
                            raise ValueError(
                                'AcousticSceneSnapshot treatment composition '
                                'capability/lifecycle mismatch'
                            )
                elif binding.composition_id is not None:
                    raise ValueError(
                        'blocked AcousticSceneSnapshot treatment binding cannot '
                        'resolve as AVAILABLE composition'
                    )

        if snapshot.sources and variant is None:
            raise ValueError(
                'AcousticSceneSnapshot R110 sources require exact SystemVariant'
            )
        for binding in snapshot.sources:
            source = self.r110_repository.get_model(
                binding.r110_compiled_source_sha256
            )
            if source is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing R110 source authority'
                )
            if source_binding_from_r110(source) != binding:
                raise ValueError(
                    'AcousticSceneSnapshot R110 source exact identity mismatch'
                )
            if (
                source.scene_revision_id != revision.revision_id
                or source.scene_content_hash != revision.content_hash
            ):
                raise ValueError(
                    'AcousticSceneSnapshot R110 source SceneRevision mismatch'
                )
            if variant is not None and (
                source.system_variant_id != variant.variant_id
                or source.system_variant_sha256 != variant.variant_sha256
            ):
                raise ValueError(
                    'AcousticSceneSnapshot R110 source SystemVariant mismatch'
                )

        source_by_hash = {
            item.r110_compiled_source_sha256: item
            for item in snapshot.sources
        }
        for wave_binding in snapshot.wave_source_excitation_bindings:
            source_binding = source_by_hash.get(
                wave_binding.r110_compiled_source_sha256
            )
            if source_binding is None:
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation references '
                    'missing R110 source'
                )
            resolved_binding = self.wave_excitation_repository.get_binding(
                wave_binding.binding_id
            )
            if resolved_binding is None:
                raise ValueError(
                    'AcousticSceneSnapshot references missing wave '
                    'excitation binding'
                )
            if resolved_binding != wave_binding:
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation exact identity mismatch'
                )
            if (
                wave_binding.source_entity_id != source_binding.source_entity_id
                or wave_binding.equipment_definition_sha256
                != source_binding.equipment_definition_sha256
            ):
                raise ValueError(
                    'AcousticSceneSnapshot wave excitation source/equipment mismatch'
                )

        effective_scene = (
            revision.document
            if variant is None
            else materialize_system_variant(revision, variant)
        )
        for receiver in snapshot.receivers:
            try:
                entity = effective_scene.entity(receiver.entity_id)
            except KeyError as exc:
                raise ValueError(
                    'AcousticSceneSnapshot receiver entity is missing'
                ) from exc
            exact_position = acoustic_reference_position(entity)
            if exact_position is None or exact_position != receiver.world_position:
                raise ValueError(
                    'AcousticSceneSnapshot receiver reference position mismatch'
                )
            if (
                receiver.orientation is not None
                and receiver.orientation != entity.orientation
            ):
                raise ValueError(
                    'AcousticSceneSnapshot receiver orientation mismatch'
                )
            expected_semantics = (
                'explicit_measurement_authority'
                if receiver.measurement_authority_ref is not None
                else 'scene_acoustic_reference_position'
            )
            if receiver.acoustic_reference_semantics != expected_semantics:
                raise ValueError(
                    'AcousticSceneSnapshot receiver reference semantics do not '
                    'reproduce canonical materialization'
                )

        self._resolve_snapshot_external_authorities(
            snapshot,
            compiled=compiled,
        )

        expected_schema_version = _snapshot_schema_version(
            compiled=compiled,
            treatment_bindings=snapshot.treatment_boundary_bindings,
            wave_excitation_bindings=snapshot.wave_source_excitation_bindings,
            geometric_acoustics_topology_preflight_ref=(
                snapshot.geometric_acoustics_topology_preflight_ref
            ),
        )
        if snapshot.schema_version != expected_schema_version:
            raise ValueError(
                'AcousticSceneSnapshot schema version does not reproduce '
                'from exact authorities'
            )
        expected_readiness = _derive_readiness(
            compiled=compiled,
            sources=snapshot.sources,
            receivers=snapshot.receivers,
            environment=snapshot.environment,
            requested_observables=snapshot.requested_observables,
            treatment_bindings=snapshot.treatment_boundary_bindings,
            wave_excitation_bindings=snapshot.wave_source_excitation_bindings,
            requested_frequency_domain=snapshot.requested_frequency_domain,
            schema_version=snapshot.schema_version,
            geometric_acoustics_topology_preflight_ref=(
                snapshot.geometric_acoustics_topology_preflight_ref
            ),
        )
        if snapshot.readiness != expected_readiness:
            raise ValueError(
                'AcousticSceneSnapshot readiness does not reproduce from '
                'exact authorities'
            )
        expected_unresolved = _derive_unresolved_conditions(
            compiled=compiled,
            sources=snapshot.sources,
            receivers=snapshot.receivers,
            environment=snapshot.environment,
            readiness=expected_readiness,
            valid_frequency_domain=snapshot.valid_frequency_domain,
            requested_frequency_domain=snapshot.requested_frequency_domain,
            treatment_bindings=snapshot.treatment_boundary_bindings,
            wave_excitation_bindings=snapshot.wave_source_excitation_bindings,
        )
        if snapshot.unresolved_conditions != expected_unresolved:
            raise ValueError(
                'AcousticSceneSnapshot unresolved conditions do not '
                'reproduce from exact authorities'
            )

        # Canonical materialization ordering: the snapshot builder sorts each
        # binding family; a persisted snapshot must reproduce that exact order.
        if snapshot.sources != tuple(
            sorted(
                snapshot.sources,
                key=lambda item: (
                    item.source_entity_id,
                    item.r110_compiled_source_sha256,
                ),
            )
        ):
            raise ValueError(
                'AcousticSceneSnapshot sources do not reproduce canonical '
                'materialization order'
            )
        if snapshot.receivers != tuple(
            sorted(snapshot.receivers, key=lambda item: item.receiver_id)
        ):
            raise ValueError(
                'AcousticSceneSnapshot receivers do not reproduce canonical '
                'materialization order'
            )
        if snapshot.wave_source_excitation_bindings != tuple(
            sorted(
                snapshot.wave_source_excitation_bindings,
                key=lambda item: (
                    item.source_entity_id,
                    item.semantic_sha256,
                ),
            )
        ):
            raise ValueError(
                'AcousticSceneSnapshot wave excitation bindings do not '
                'reproduce canonical materialization order'
            )
        if snapshot.treatment_boundary_bindings != tuple(
            sorted(
                snapshot.treatment_boundary_bindings,
                key=lambda item: (
                    item.host_surface_id,
                    item.target_domain,
                    item.status,
                    item.composition_hash_sha256 or '',
                ),
            )
        ):
            raise ValueError(
                'AcousticSceneSnapshot treatment bindings do not reproduce '
                'canonical materialization order'
            )

        return snapshot

    def _resolve_snapshot_external_authorities(
        self,
        snapshot: AcousticSceneSnapshot,
        *,
        compiled: R120CompiledGeometry,
    ) -> None:
        resolvers = self.authority_resolvers
        environment = snapshot.environment
        if environment is not None:
            if resolvers.environment is None:
                raise ValueError(
                    'AcousticSceneSnapshot environment authority requires a '
                    'typed environment resolver'
                )
            resolved_environment = resolvers.environment(environment.authority)
            if resolved_environment is None:
                raise ValueError(
                    'environment exact external authority does not exist'
                )
            resolved_environment = SnapshotEnvironmentAuthorityRef.model_validate(
                resolved_environment.model_dump(mode='python')
            )
            if resolved_environment != environment:
                raise ValueError(
                    'environment exact external authority mismatch'
                )
            if environment.sound_speed_source_authority is not None:
                if resolvers.sound_speed_source is None:
                    raise ValueError(
                        'AcousticSceneSnapshot sound speed source authority '
                        'requires a typed sound speed resolver'
                    )
                sound_speed = resolvers.sound_speed_source(
                    environment.sound_speed_source_authority
                )
                if sound_speed is None:
                    raise ValueError(
                        'sound speed source exact external authority '
                        'does not exist'
                    )
                if float(sound_speed) != environment.sound_speed_m_s:
                    raise ValueError(
                        'sound speed value does not reproduce from exact '
                        'source authority'
                    )
            if environment.temperature_source_authority is not None:
                if resolvers.temperature_source is None:
                    raise ValueError(
                        'AcousticSceneSnapshot temperature source authority '
                        'requires a typed temperature resolver'
                    )
                temperature = resolvers.temperature_source(
                    environment.temperature_source_authority
                )
                if temperature is None:
                    raise ValueError(
                        'temperature source exact external authority '
                        'does not exist'
                    )
                if float(temperature) != environment.temperature_c:
                    raise ValueError(
                        'temperature value does not reproduce from exact '
                        'source authority'
                    )

        for receiver in snapshot.receivers:
            measurement_ref = receiver.measurement_authority_ref
            if measurement_ref is None:
                continue
            if resolvers.receiver_measurement is None:
                raise ValueError(
                    'AcousticSceneSnapshot explicit receiver measurement '
                    'authority requires a typed receiver measurement resolver'
                )
            measurement = resolvers.receiver_measurement(measurement_ref)
            if measurement is None:
                raise ValueError(
                    'receiver measurement exact external authority '
                    'does not exist'
                )
            measurement = ReceiverMeasurementAuthority.model_validate(
                measurement.model_dump(mode='python')
            )
            if (
                measurement.authority_ref != measurement_ref
                or measurement.entity_id != receiver.entity_id
                or measurement.world_position != receiver.world_position
                or (
                    receiver.orientation is not None
                    and measurement.orientation != receiver.orientation
                )
            ):
                raise ValueError(
                    'receiver measurement authority does not bind the exact '
                    'receiver position/context'
                )

        domain_ref = snapshot.valid_frequency_domain_authority_ref
        if domain_ref is not None:
            if resolvers.valid_frequency_domain is None:
                raise ValueError(
                    'AcousticSceneSnapshot valid frequency domain authority '
                    'requires a typed frequency domain resolver'
                )
            resolved_domain = resolvers.valid_frequency_domain(domain_ref)
            if resolved_domain is None:
                raise ValueError(
                    'valid frequency domain exact external authority '
                    'does not exist'
                )
            resolved_domain = FrequencyDomain.model_validate(
                resolved_domain.model_dump(mode='python')
            )
            if resolved_domain != snapshot.valid_frequency_domain:
                raise ValueError(
                    'valid frequency domain does not reproduce from exact '
                    'authority'
                )

        preflight_ref = snapshot.geometric_acoustics_topology_preflight_ref
        if preflight_ref is not None:
            if resolvers.geometric_topology_preflight is None:
                raise ValueError(
                    'AcousticSceneSnapshot geometric topology preflight '
                    'authority requires a typed preflight resolver'
                )
            resolved_preflight = resolvers.geometric_topology_preflight(
                preflight_ref
            )
            if resolved_preflight is None:
                raise ValueError(
                    'geometric topology preflight exact external authority '
                    'does not exist'
                )
            resolved_preflight = GeometricPortalGraph.model_validate(
                resolved_preflight.model_dump(mode='python')
            )
            if resolved_preflight.as_external_ref() != preflight_ref:
                raise ValueError(
                    'geometric topology preflight exact external authority '
                    'mismatch'
                )
            _require_geometric_topology_preflight_authority(
                preflight_ref=preflight_ref,
                compiled=compiled,
                requested_observables=snapshot.requested_observables,
            )
            region_ref = snapshot.acoustic_region_authority_ref
            portal_ref = snapshot.portal_authority_ref
            if region_ref is None or portal_ref is None:
                raise ValueError(
                    'geometric topology preflight requires exact R120 '
                    'region/portal authorities'
                )
            if (
                resolved_preflight.r120_compiled_geometry_id
                != snapshot.r120_compiled_geometry_id
                or resolved_preflight.r120_compiled_geometry_sha256
                != snapshot.r120_compiled_geometry_sha256
                or resolved_preflight.region_authority_id
                != region_ref.authority_id
                or resolved_preflight.region_authority_sha256
                != region_ref.semantic_hash_sha256
                or resolved_preflight.portal_authority_id
                != portal_ref.authority_id
                or resolved_preflight.portal_authority_sha256
                != portal_ref.semantic_hash_sha256
            ):
                raise ValueError(
                    'geometric topology preflight record does not bind the '
                    'exact snapshot geometry authorities'
                )

        external_refs: dict[
            tuple[str, str, str],
            tuple[ExactExternalAuthorityRef, str],
        ] = {}
        for ref, label in (
            (snapshot.acoustic_region_authority_ref, 'acoustic region'),
            (snapshot.portal_authority_ref, 'portal'),
            (
                snapshot.boundary_termination_authority_ref,
                'boundary termination',
            ),
        ):
            if ref is not None:
                external_refs.setdefault(_ref_key(ref), (ref, label))
        for surface in snapshot.surface_boundary_configuration:
            if surface.material_authority is not None:
                external_refs.setdefault(
                    _ref_key(surface.material_authority),
                    (surface.material_authority, 'surface material'),
                )
            if surface.boundary_physics_authority is not None:
                external_refs.setdefault(
                    _ref_key(surface.boundary_physics_authority),
                    (
                        surface.boundary_physics_authority,
                        'surface boundary physics',
                    ),
                )
        for binding in snapshot.treatment_boundary_bindings:
            if binding.base_material_authority is not None:
                external_refs.setdefault(
                    _ref_key(binding.base_material_authority),
                    (
                        binding.base_material_authority,
                        'treatment base material',
                    ),
                )
            if binding.base_boundary_physics_authority is not None:
                external_refs.setdefault(
                    _ref_key(binding.base_boundary_physics_authority),
                    (
                        binding.base_boundary_physics_authority,
                        'treatment base boundary physics',
                    ),
                )
            for material_ref in binding.selected_treatment_material_authorities:
                external_refs.setdefault(
                    _ref_key(material_ref),
                    (material_ref, 'selected treatment material'),
                )
        for ref, label in external_refs.values():
            self._resolve_external_authority_ref(ref, label=label)

    def _resolve_external_authority_ref(
        self,
        ref: ExactExternalAuthorityRef,
        *,
        label: str,
    ) -> ExactExternalAuthorityRef:
        resolver = self.authority_resolvers.external_authority
        if resolver is None:
            raise ValueError(
                f'AcousticSceneSnapshot {label} authority requires a typed '
                'external authority resolver'
            )
        resolved = resolver(ref)
        if resolved is None:
            raise ValueError(
                f'{label} exact external authority does not exist'
            )
        resolved = ExactExternalAuthorityRef.model_validate(
            resolved.model_dump(mode='python')
        )
        if resolved != ref:
            raise ValueError(f'{label} exact external authority mismatch')
        return resolved

    def save_snapshot(
        self,
        snapshot: AcousticSceneSnapshot,
    ) -> AcousticSceneSnapshot:
        snapshot = self._validate_snapshot(snapshot)
        environment_hash = (
            None
            if snapshot.environment is None
            else snapshot.environment.authority.semantic_hash_sha256
        )

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSceneSnapshot.model_validate_json(
                    existing['payload_json']
                )
                if persisted != snapshot:
                    raise ValueError(
                        'AcousticSceneSnapshot id already exists with different semantics'
                    )
                return self._validate_snapshot(persisted)

            collision = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE semantic_sha256=?
                """,
                (snapshot.semantic_sha256,),
            ).fetchone()
            if collision is not None:
                persisted = AcousticSceneSnapshot.model_validate_json(
                    collision['payload_json']
                )
                if persisted != snapshot:
                    raise ValueError(
                        'AcousticSceneSnapshot hash collision with different semantics'
                    )
                return self._validate_snapshot(persisted)

            connection.execute(
                """
                INSERT INTO cad_acoustic_scene_snapshots(
                    snapshot_id,
                    semantic_sha256,
                    document_id,
                    scene_revision_id,
                    scene_content_hash,
                    system_variant_id,
                    system_variant_sha256,
                    r120_compiled_geometry_id,
                    r120_compiled_geometry_sha256,
                    material_boundary_configuration_sha256,
                    environment_authority_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.semantic_sha256,
                    snapshot.document_id,
                    snapshot.scene_revision_id,
                    snapshot.scene_content_hash,
                    snapshot.system_variant_id,
                    snapshot.system_variant_sha256,
                    snapshot.r120_compiled_geometry_id,
                    snapshot.r120_compiled_geometry_sha256,
                    snapshot.material_boundary_configuration_sha256,
                    environment_hash,
                    snapshot.model_dump_json(),
                    _utc_now(),
                ),
            )
        return snapshot

    def get_snapshot(
        self,
        snapshot_id: str,
    ) -> AcousticSceneSnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT snapshot_id, semantic_sha256, payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = AcousticSceneSnapshot.model_validate_json(
            row['payload_json']
        )
        if (
            snapshot.snapshot_id != row['snapshot_id']
            or snapshot.semantic_sha256 != row['semantic_sha256']
        ):
            raise ValueError(
                'persisted AcousticSceneSnapshot payload identity mismatch'
            )
        return self._validate_snapshot(snapshot)

    def get_snapshot_by_hash(
        self,
        semantic_sha256: str,
    ) -> AcousticSceneSnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT snapshot_id, semantic_sha256, payload_json
                FROM cad_acoustic_scene_snapshots
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        snapshot = AcousticSceneSnapshot.model_validate_json(
            row['payload_json']
        )
        if (
            snapshot.snapshot_id != row['snapshot_id']
            or snapshot.semantic_sha256 != row['semantic_sha256']
        ):
            raise ValueError(
                'persisted AcousticSceneSnapshot payload identity mismatch'
            )
        return self._validate_snapshot(snapshot)

    def _resolve_fidelity_policy(
        self,
        request: AcousticPredictionRequest,
    ) -> AcousticNumericalFidelityPolicy:
        if self.fidelity_policy_resolver is None:
            raise ValueError(
                'AcousticPredictionRequest requires a numerical fidelity '
                'policy resolver'
            )
        resolved = self.fidelity_policy_resolver(
            request.numerical_fidelity_policy_ref
        )
        if resolved is None:
            raise ValueError(
                'numerical fidelity policy exact external authority '
                'does not exist'
            )
        policy = AcousticNumericalFidelityPolicy.model_validate(
            resolved.model_dump(mode='python')
        )
        if policy.authority_ref != request.numerical_fidelity_policy_ref:
            raise ValueError(
                'numerical fidelity policy exact external authority mismatch'
            )
        reasons = numerical_fidelity_policy_request_reasons(
            policy=policy,
            request=request,
        )
        if reasons:
            raise ValueError(
                'numerical fidelity policy is not applicable to the '
                f'AcousticPredictionRequest: {", ".join(reasons)}'
            )
        return policy

    def save_prediction_request(
        self,
        request: AcousticPredictionRequest,
    ) -> AcousticPredictionRequest:
        request = AcousticPredictionRequest.model_validate(
            request.model_dump(mode='python')
        )
        snapshot = self.get_snapshot(
            request.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                'AcousticPredictionRequest references unpersisted AcousticSceneSnapshot'
            )
        if (
            snapshot.semantic_sha256
            != request.acoustic_scene_snapshot_sha256
        ):
            raise ValueError(
                'AcousticPredictionRequest snapshot hash mismatch'
            )
        self._resolve_fidelity_policy(request)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_prediction_requests
                WHERE request_id=?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticPredictionRequest.model_validate_json(
                    existing['payload_json']
                )
                if persisted != request:
                    raise ValueError(
                        'AcousticPredictionRequest id already exists with different semantics'
                    )
                return persisted

            connection.execute(
                """
                INSERT INTO cad_acoustic_prediction_requests(
                    request_id,
                    request_semantic_sha256,
                    acoustic_scene_snapshot_id,
                    acoustic_scene_snapshot_sha256,
                    deterministic_input_hash,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.request_semantic_sha256,
                    request.acoustic_scene_snapshot_id,
                    request.acoustic_scene_snapshot_sha256,
                    request.deterministic_input_hash,
                    request.model_dump_json(),
                    _utc_now(),
                ),
            )
        return request

    def get_prediction_request(
        self,
        request_id: str,
    ) -> AcousticPredictionRequest | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_prediction_requests
                WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        request = AcousticPredictionRequest.model_validate_json(
            row['payload_json']
        )
        snapshot = self.get_snapshot(
            request.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                'persisted AcousticPredictionRequest references missing snapshot'
            )
        if (
            snapshot.semantic_sha256
            != request.acoustic_scene_snapshot_sha256
        ):
            raise ValueError(
                'persisted AcousticPredictionRequest snapshot hash mismatch'
            )
        self._resolve_fidelity_policy(request)
        return request
