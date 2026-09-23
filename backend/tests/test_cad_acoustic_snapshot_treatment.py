from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from htdt.acoustic_benchmark import (
    AcousticMaterial,
    GeometricAcousticBand,
    SpecificImpedancePoint,
)
from htdt.cad_acoustic_snapshot import (
    AcousticSceneSnapshot,
    _digest,
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
)
from htdt.cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from htdt.cad_acoustic_treatment import (
    TreatmentAcousticModel,
    TreatmentAcousticModelSubject,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentFrequencyBand,
    TreatmentLayer,
    TreatmentUncertainty,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneDocument, SceneEntity
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.r120_geometry_compiler import (
    AcousticRegionDeclaration,
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
    make_acoustic_region_authority,
    make_boundary_termination_authority,
    make_portal_authority,
    make_r120_geometry_compilation_request,
)
from htdt.r120_geometry_compiler_repository import R120GeometryCompilerRepository
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)
from htdt.treatment_boundary_overlay import (
    TreatmentBoundaryCompileInput,
    TreatmentBoundaryCompositionRequest,
    TreatmentBoundaryOverlay,
    _semantic_hash,
    adapt_treatment_boundary_composition_to_r120,
    compile_treatment_boundary_overlays,
)
from htdt.treatment_boundary_overlay_repository import TreatmentBoundaryOverlayRepository


CLOSED_TETRA = b'''\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
'''


def _external(name: str, token: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=token * 64,
    )


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


def _authority_resolvers(snapshot) -> AcousticSnapshotAuthorityResolvers:
    """Honest registry resolving every external authority a snapshot claims."""
    refs: dict[tuple[str, str, str], ExactExternalAuthorityRef] = {}
    for ref in (
        snapshot.acoustic_region_authority_ref,
        snapshot.portal_authority_ref,
        snapshot.boundary_termination_authority_ref,
    ):
        if ref is not None:
            refs[_ref_key(ref)] = ref
    for surface in snapshot.surface_boundary_configuration:
        for ref in (
            surface.material_authority,
            surface.boundary_physics_authority,
        ):
            if ref is not None:
                refs[_ref_key(ref)] = ref
    for binding in snapshot.treatment_boundary_bindings:
        for ref in (
            binding.base_material_authority,
            binding.base_boundary_physics_authority,
            *binding.selected_treatment_material_authorities,
        ):
            if ref is not None:
                refs[_ref_key(ref)] = ref
    return AcousticSnapshotAuthorityResolvers(
        external_authority=lambda ref: refs.get(_ref_key(ref)),
    )


def _model_material(kind: str, suffix: str = '') -> AcousticMaterial | None:
    if kind == 'none':
        return None
    if kind == 'geometric':
        material = AcousticMaterial(
            material_id=f'fixture-geometric-treatment{suffix}',
            provenance='fixture',
            version='1',
            wave_model='unsupported',
            geometric_model='banded',
            geometric_bands=(
                GeometricAcousticBand(
                    center_hz=500.0,
                    absorption=0.40,
                    scattering=0.20,
                ),
            ),
        )
    elif kind == 'wave':
        material = AcousticMaterial(
            material_id=f'fixture-wave-treatment{suffix}',
            provenance='fixture measured complex impedance',
            version='1',
            wave_model='specific_impedance_table',
            specific_impedance=(
                SpecificImpedancePoint(
                    frequency_hz=100.0,
                    resistance_pa_s_m=420.0,
                    reactance_pa_s_m=-80.0,
                ),
                SpecificImpedancePoint(
                    frequency_hz=200.0,
                    resistance_pa_s_m=450.0,
                    reactance_pa_s_m=-40.0,
                ),
            ),
            geometric_model='unsupported',
        )
    elif kind == 'both':
        material = AcousticMaterial(
            material_id=f'fixture-both-treatment{suffix}',
            provenance='fixture measured wave plus geometric authority',
            version='1',
            wave_model='specific_impedance_table',
            specific_impedance=(
                SpecificImpedancePoint(
                    frequency_hz=100.0,
                    resistance_pa_s_m=410.0,
                    reactance_pa_s_m=-70.0,
                ),
                SpecificImpedancePoint(
                    frequency_hz=200.0,
                    resistance_pa_s_m=440.0,
                    reactance_pa_s_m=-35.0,
                ),
            ),
            geometric_model='banded',
            geometric_bands=(
                GeometricAcousticBand(
                    center_hz=500.0,
                    absorption=0.50,
                    scattering=0.15,
                ),
            ),
        )
    else:
        raise ValueError(kind)
    return material


def _definition(kind: str, suffix: str = ''):
    definition_id = f'treatment-{kind}{suffix}'
    dimensions = TreatmentDimensions(
        width_m=1.0,
        height_m=1.0,
        thickness_m=0.1,
    )
    layers = (
        TreatmentLayer(
            layer_id='core',
            material_name='fixture core',
            thickness_m=0.1,
            density_kg_m3=48.0,
        ),
    )
    material = _model_material(kind, suffix)
    band = TreatmentFrequencyBand(
        min_hz=80.0,
        max_hz=4000.0,
    )
    uncertainty = TreatmentUncertainty(
        kind='quantified',
        value=0.05,
        unit='fixture',
        note='fixture uncertainty',
    )
    model_subject = (
        None
        if material is None
        else TreatmentAcousticModelSubject(
            model_id=f'fixture-{kind}-model{suffix}',
            model_version='1',
            evidence_basis='measured',
            valid_frequency_band=band,
            uncertainty=uncertainty,
            material=material,
        )
    )
    evidence = build_treatment_evidence_authority(
        source_kind='measurement',
        source_id=f'treatment-snapshot-fixture{suffix}',
        source_version='1',
        source_sha256='1' * 64,
        reference='Issue #171 treatment snapshot fixture',
        extraction_id='fixture-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id=definition_id,
            definition_version='1.0',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=layers,
            acoustic_model=model_subject,
        ),
    )
    provenance = evidence.as_provenance()
    acoustic_model = (
        None
        if material is None
        else TreatmentAcousticModel(
            model_id=f'fixture-{kind}-model{suffix}',
            model_version='1',
            evidence_basis='measured',
            valid_frequency_band=band,
            uncertainty=uncertainty,
            provenance=provenance,
            material=material,
        )
    )
    definition = build_acoustic_treatment_definition(
        definition_id=definition_id,
        version='1.0',
        name=f'{kind} treatment {suffix}',
        treatment_type='porous_absorber',
        provenance=provenance,
        dimensions=dimensions,
        air_gap_m=0.05,
        layers=layers,
        acoustic_model=acoustic_model,
    )
    return definition, (evidence,)


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    mesh = import_raw_visual_mesh(
        CLOSED_TETRA,
        source_name='treatment-snapshot.obj',
    )
    conversion = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are exact HTDT metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, conversion)
    revision = scene_repository.save(
        SceneDocument(
            document_id='treatment-snapshot-fixture',
            schema_version=4,
            room=None,
            r120_semantic_geometry=geometry,
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    surface_id = geometry.surfaces[0].surface_id
    base_binding = SurfaceBoundaryAuthorityBinding(
        source_surface_id=surface_id,
        material_authority=_external('base-construction-material', 'a'),
        boundary_physics_authority=_external('base-boundary-physics', 'b'),
    )
    region = make_acoustic_region_authority(
        (
            AcousticRegionDeclaration(
                region_id='room-air',
                boundary_surface_ids=(surface_id,),
            ),
        )
    )
    portals = make_portal_authority(declaration_mode='explicit_none')
    terminations = make_boundary_termination_authority(
        declaration_mode='explicit_none'
    )
    compile_request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(
        revision,
        compile_request,
        surface_boundary_bindings=(base_binding,),
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    assert compiled.readiness.wave_geometry_ready is True
    assert compiled.readiness.geometric_acoustics_geometry_ready is True

    r120_repository = R120GeometryCompilerRepository(scene_repository)
    r120_repository.save_compiled_geometry(
        compiled,
        surface_boundary_bindings=(base_binding,),
        region_authority=region,
        portal_authority=portals,
        boundary_termination_authority=terminations,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        variant_repository,
    )
    overlay_repository = TreatmentBoundaryOverlayRepository(
        scene_repository,
        treatment_repository,
        r120_repository,
    )
    return {
        'scene_repository': scene_repository,
        'variant_repository': variant_repository,
        'treatment_repository': treatment_repository,
        'overlay_repository': overlay_repository,
        'r120_repository': r120_repository,
        'revision': revision,
        'geometry': geometry,
        'surface_id': surface_id,
        'base_binding': base_binding,
        'region': region,
        'portals': portals,
        'terminations': terminations,
        'compiled': compiled,
    }


def _compile_result(
    fx,
    *,
    kind: str,
    instance_id: str,
    target_domain: str,
    suffix: str = '',
    installed: bool = False,
):
    definition, evidence = _definition(kind, suffix)
    for item in evidence:
        fx['treatment_repository'].save_evidence(item)
    definition = fx['treatment_repository'].save_definition(definition)
    proposed = build_treatment_placement(
        definition=definition,
        revision=fx['revision'],
        instance_id=instance_id,
        position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        coverage=TreatmentCoverage(
            width_m=1.0,
            height_m=1.0,
            host_surface_fraction=1.0,
        ),
        host_surface_id=fx['surface_id'],
    )
    fx['treatment_repository'].save_placement(proposed)
    placement = proposed
    if installed:
        placement = revise_treatment_placement(
            proposed,
            revision=fx['revision'],
            lifecycle='installed',
        )
        fx['treatment_repository'].save_placement(placement)
    evaluation = fx['treatment_repository'].evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fx['revision'].revision_id,
    )
    item = TreatmentBoundaryCompileInput(
        definition=definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )
    result = compile_treatment_boundary_overlays(
        fx['revision'],
        fx['compiled'],
        (item,),
        target_domain=target_domain,
        base_surface_bindings=(fx['base_binding'],),
    )[0]
    return result, item


def _persist_result(fx, result) -> None:
    if result.overlay is not None:
        fx['overlay_repository'].save_overlay(result.overlay)
    if result.composition_request is not None:
        fx['overlay_repository'].save_composition(result.composition_request)


def _snapshot(fx, *results):
    return build_acoustic_scene_snapshot(
        scene_revision=fx['revision'],
        compiled_geometry=fx['compiled'],
        source_models=(),
        receivers=(),
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=100.0,
            maximum_hz=1000.0,
        ),
        requested_observables=(
            'complex_pressure',
            'deterministic_paths',
        ),
        treatment_boundary_results=tuple(results),
    )


def _prediction(snapshot):
    return build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id='future-r130-role',
        requested_frequency_domain=snapshot.requested_frequency_domain,
        requested_observables=('complex_pressure',),
        numerical_fidelity_policy_ref=_external(
            'fixture-numerical-fidelity-policy',
            'f',
        ),
    )


def test_no_treatment_snapshot_regression_stays_v1(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    snapshot = _snapshot(fx)

    assert snapshot.schema_version == 1
    assert snapshot.authority_version == '1'
    assert snapshot.compiler_version == '1'
    assert snapshot.treatment_boundary_bindings == ()
    assert snapshot.readiness.geometric_boundary_ready is None


def test_wave_capable_treatment_composition_is_exact_input(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='wave',
        instance_id='panel-wave',
        target_domain='wave',
    )
    snapshot = _snapshot(fx, result)
    binding = snapshot.treatment_boundary_bindings[0]

    assert result.status == 'AVAILABLE'
    assert snapshot.schema_version == 2
    assert binding.composition_id == result.composition_request.composition_id
    assert binding.composition_hash_sha256 == (
        result.composition_request.composition_hash_sha256
    )
    assert binding.target_domain == 'wave'
    assert snapshot.readiness.wave_boundary_ready is True
    assert snapshot.readiness.geometric_boundary_ready is False


def test_geometric_only_treatment_does_not_block_geometric_domain(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='geometric',
        instance_id='panel-geometric',
        target_domain='geometric',
    )
    snapshot = _snapshot(fx, result)
    path_state = next(
        item
        for item in snapshot.readiness.observable_readiness
        if item.observable == 'deterministic_paths'
    )

    assert result.status == 'AVAILABLE'
    assert snapshot.readiness.wave_boundary_ready is False
    assert snapshot.readiness.geometric_boundary_ready is True
    assert 'geometric_boundary_not_ready' not in path_state.reasons


def test_treatment_model_unknown_blocks_boundary_capability(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='none',
        instance_id='panel-unknown',
        target_domain='wave',
    )
    snapshot = _snapshot(fx, result)
    binding = snapshot.treatment_boundary_bindings[0]

    assert result.status == 'BLOCKED_NO_ACOUSTIC_MODEL'
    assert binding.composition_id is None
    assert binding.attached_treatment_overlays[0].wave_capability_state == 'UNKNOWN'
    assert binding.attached_treatment_overlays[0].geometric_capability_state == 'UNKNOWN'
    assert snapshot.readiness.wave_boundary_ready is False
    assert snapshot.readiness.geometric_boundary_ready is False


def test_proposed_lifecycle_is_bound_exactly(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-proposed',
        target_domain='wave',
    )
    snapshot = _snapshot(fx, result)

    assert result.status == 'AVAILABLE'
    assert snapshot.treatment_boundary_bindings[0].lifecycle == 'proposed'
    assert (
        snapshot.treatment_boundary_bindings[0]
        .attached_treatment_overlays[0]
        .lifecycle
        == 'proposed'
    )


def test_installed_lifecycle_is_bound_exactly_and_changes_identity(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    proposed, proposed_item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-lifecycle',
        target_domain='wave',
    )
    installed_placement = revise_treatment_placement(
        proposed_item.placement,
        revision=fx['revision'],
        lifecycle='installed',
    )
    fx['treatment_repository'].save_placement(installed_placement)
    installed_evaluation = (
        fx['treatment_repository'].evaluate_placement_surface_binding(
            installed_placement,
            scene_revision_id=fx['revision'].revision_id,
        )
    )
    installed_item = TreatmentBoundaryCompileInput(
        definition=proposed_item.definition,
        placement=installed_placement,
        surface_binding_evaluation=installed_evaluation,
    )
    installed = compile_treatment_boundary_overlays(
        fx['revision'],
        fx['compiled'],
        (installed_item,),
        target_domain='wave',
        base_surface_bindings=(fx['base_binding'],),
    )[0]
    proposed_snapshot = _snapshot(fx, proposed)
    installed_snapshot = _snapshot(fx, installed)

    assert installed.status == 'AVAILABLE'
    assert installed_snapshot.treatment_boundary_bindings[0].lifecycle == 'installed'
    assert installed_snapshot.semantic_sha256 != proposed_snapshot.semantic_sha256
    assert _prediction(installed_snapshot).deterministic_input_hash != (
        _prediction(proposed_snapshot).deterministic_input_hash
    )


def test_base_material_and_boundary_remain_separate_from_treatment(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-base',
        target_domain='wave',
    )
    snapshot = _snapshot(fx, result)
    base = snapshot.surface_boundary_configuration[0]
    binding = snapshot.treatment_boundary_bindings[0]

    assert base.material_authority == fx['base_binding'].material_authority
    assert base.boundary_physics_authority == (
        fx['base_binding'].boundary_physics_authority
    )
    assert binding.base_material_authority == base.material_authority
    assert binding.base_boundary_physics_authority == base.boundary_physics_authority
    assert binding.composition_id != base.boundary_physics_authority.authority_id


def test_overlay_change_changes_snapshot_hash(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    first, _ = _compile_result(
        fx,
        kind='both',
        instance_id='panel-overlay-a',
        target_domain='wave',
        suffix='-overlay-a',
    )
    second, _ = _compile_result(
        fx,
        kind='both',
        instance_id='panel-overlay-b',
        target_domain='wave',
        suffix='-overlay-b',
    )
    first_snapshot = _snapshot(fx, first)
    second_snapshot = _snapshot(fx, second)

    assert first.overlay.overlay_id != second.overlay.overlay_id
    assert first_snapshot.semantic_sha256 != second_snapshot.semantic_sha256
    assert first_snapshot.snapshot_id != second_snapshot.snapshot_id


def test_composition_change_changes_prediction_input_hash(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    wave, item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-composition',
        target_domain='wave',
    )
    geometric = compile_treatment_boundary_overlays(
        fx['revision'],
        fx['compiled'],
        (item,),
        target_domain='geometric',
        base_surface_bindings=(fx['base_binding'],),
    )[0]
    wave_snapshot = _snapshot(fx, wave)
    geometric_snapshot = _snapshot(fx, geometric)

    assert wave.composition_request.composition_id != (
        geometric.composition_request.composition_id
    )
    assert wave_snapshot.semantic_sha256 != geometric_snapshot.semantic_sha256
    assert _prediction(wave_snapshot).deterministic_input_hash != (
        _prediction(geometric_snapshot).deterministic_input_hash
    )


def test_stale_overlay_is_rejected(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-stale-overlay',
        target_domain='wave',
    )
    stale_overlay = result.overlay.model_copy(
        update={'host_surface_authority_sha256': '0' * 64}
    )
    stale_result = result.model_copy(update={'overlay': stale_overlay})

    with pytest.raises(ValueError):
        _snapshot(fx, stale_result)


def test_stale_host_surface_is_rejected(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-stale-host',
        target_domain='wave',
    )
    stale_result = result.model_copy(
        update={'host_surface_id': f"semantic-surface:{'0' * 64}"}
    )

    with pytest.raises(ValueError):
        _snapshot(fx, stale_result)


def test_wrong_r120_compiled_geometry_is_rejected(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-wrong-r120',
        target_domain='wave',
    )
    alternate_request = make_r120_geometry_compilation_request(
        fx['revision'],
        geometric_tolerance_m=2.0e-6,
    )
    alternate = compile_r120_geometry(
        fx['revision'],
        alternate_request,
        surface_boundary_bindings=(fx['base_binding'],),
        region_authority=fx['region'],
        portal_authority=fx['portals'],
        boundary_termination_authority=fx['terminations'],
    )

    with pytest.raises(ValueError):
        build_acoustic_scene_snapshot(
            scene_revision=fx['revision'],
            compiled_geometry=alternate,
            source_models=(),
            receivers=(),
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=1000.0,
            ),
            requested_observables=('complex_pressure',),
            treatment_boundary_results=(result,),
        )


def test_wrong_scene_revision_is_rejected(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-wrong-revision',
        target_domain='wave',
    )
    changed_document = fx['revision'].document.model_copy(
        update={
            'entities': (
                SceneEntity(
                    entity_id='revision-marker',
                    kind='measurement_point',
                    name='marker',
                    position=Position3(x_m=0.1, y_m=0.1, z_m=0.1),
                ),
            )
        }
    )
    other_revision = fx['scene_repository'].save(
        changed_document,
        parent_revision_id=fx['revision'].revision_id,
    ).revision

    with pytest.raises(ValueError):
        build_acoustic_scene_snapshot(
            scene_revision=other_revision,
            compiled_geometry=fx['compiled'],
            source_models=(),
            receivers=(),
            requested_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=1000.0,
            ),
            requested_observables=('complex_pressure',),
            treatment_boundary_results=(result,),
        )


def test_save_reopen_exactly_reresolves_treatment_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-persisted',
        target_domain='wave',
    )
    _persist_result(fx, result)
    snapshot = _snapshot(fx, result)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        r120_repository=fx['r120_repository'],
        treatment_boundary_repository=fx['overlay_repository'],
        authority_resolvers=_authority_resolvers(snapshot),
    )
    repository.save_snapshot(snapshot)

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_treatments = CadAcousticTreatmentRepository(
        reopened_scene,
        reopened_variants,
    )
    reopened_r120 = R120GeometryCompilerRepository(reopened_scene)
    reopened_overlays = TreatmentBoundaryOverlayRepository(
        reopened_scene,
        reopened_treatments,
        reopened_r120,
    )
    reopened_repository = CadAcousticSnapshotRepository(
        reopened_scene,
        r120_repository=reopened_r120,
        treatment_boundary_repository=reopened_overlays,
        authority_resolvers=_authority_resolvers(snapshot),
    )

    assert reopened_repository.get_snapshot(snapshot.snapshot_id) == snapshot
    with pytest.raises(ValueError):
        CadAcousticSnapshotRepository(reopened_scene).get_snapshot(
            snapshot.snapshot_id
        )


def test_v1_persisted_snapshot_compatibility(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    snapshot = _snapshot(fx)
    legacy_payload = snapshot.model_dump(mode='json')
    legacy_payload.pop('treatment_boundary_bindings')
    legacy_payload['readiness'].pop('geometric_boundary_ready')

    parsed = AcousticSceneSnapshot.model_validate(legacy_payload)
    assert parsed.snapshot_id == snapshot.snapshot_id
    assert parsed.semantic_sha256 == snapshot.semantic_sha256

    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        r120_repository=fx['r120_repository'],
        authority_resolvers=_authority_resolvers(parsed),
    )
    repository.save_snapshot(parsed)
    reopened = CadAcousticSnapshotRepository(
        SceneRepository(fx['scene_repository'].path),
        authority_resolvers=_authority_resolvers(snapshot),
    ).get_snapshot(snapshot.snapshot_id)
    assert reopened == snapshot


def test_blocked_treatment_result_never_becomes_available_input(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    blocked_wave, item = _compile_result(
        fx,
        kind='geometric',
        instance_id='panel-blocked-wave',
        target_domain='wave',
    )
    available_geometric = compile_treatment_boundary_overlays(
        fx['revision'],
        fx['compiled'],
        (item,),
        target_domain='geometric',
        base_surface_bindings=(fx['base_binding'],),
    )[0]
    snapshot = _snapshot(fx, blocked_wave, available_geometric)
    by_domain = {
        binding.target_domain: binding
        for binding in snapshot.treatment_boundary_bindings
    }

    assert blocked_wave.status == 'BLOCKED_WAVE_MODEL_UNAVAILABLE'
    assert available_geometric.status == 'AVAILABLE'
    assert by_domain['wave'].status == 'BLOCKED_WAVE_MODEL_UNAVAILABLE'
    assert by_domain['wave'].composition_id is None
    assert by_domain['wave'].selected_treatment_material_authorities == ()
    assert by_domain['geometric'].status == 'AVAILABLE'
    assert snapshot.readiness.wave_boundary_ready is False
    assert snapshot.readiness.geometric_boundary_ready is True


def _forged_snapshot(snapshot, **updates) -> AcousticSceneSnapshot:
    """Self-consistent forgery: claimed semantics with a recomputed identity."""
    forged = snapshot.model_copy(update=updates)
    digest = _digest(forged.semantic_payload())
    return AcousticSceneSnapshot.model_validate(
        {
            **forged.model_dump(mode='python'),
            'semantic_sha256': digest,
            'snapshot_id': f'acoustic-scene-snapshot:{digest}',
        }
    )


def _rehashed_overlay(
    overlay: TreatmentBoundaryOverlay,
    **updates,
) -> TreatmentBoundaryOverlay:
    """Coherently recompute the self hash/id of a modified overlay."""

    candidate = overlay.model_copy(update=updates)
    core = candidate.model_dump(
        mode='json',
        exclude={'overlay_id', 'overlay_hash_sha256'},
    )
    digest = _semantic_hash(core)
    return TreatmentBoundaryOverlay.model_validate(
        {
            **core,
            'overlay_id': f'treatment-boundary-overlay:{digest}',
            'overlay_hash_sha256': digest,
        }
    )


def _rehashed_composition(
    composition: TreatmentBoundaryCompositionRequest,
    **updates,
) -> TreatmentBoundaryCompositionRequest:
    """Coherently recompute the self hash/id of a modified composition."""

    candidate = composition.model_copy(update=updates)
    core = candidate.model_dump(
        mode='json',
        exclude={'composition_id', 'composition_hash_sha256'},
    )
    digest = _semantic_hash(core)
    return TreatmentBoundaryCompositionRequest.model_validate(
        {
            **core,
            'composition_id': f'treatment-boundary-composition:{digest}',
            'composition_hash_sha256': digest,
        }
    )


def test_incomplete_treatment_composition_cannot_claim_boundary_ready(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    blocked_wave, _item = _compile_result(
        fx,
        kind='geometric',
        instance_id='panel-forged-ready',
        target_domain='wave',
    )
    _persist_result(fx, blocked_wave)
    snapshot = _snapshot(fx, blocked_wave)
    binding = snapshot.treatment_boundary_bindings[0]

    assert binding.status == 'BLOCKED_WAVE_MODEL_UNAVAILABLE'
    assert binding.composition_id is None
    assert snapshot.readiness.wave_boundary_ready is False
    assert snapshot.readiness.geometric_boundary_ready is False
    assert (
        'treatment_wave_boundary_blocked_wave_model_unavailable'
        in snapshot.unresolved_conditions
    )

    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        r120_repository=fx['r120_repository'],
        treatment_boundary_repository=fx['overlay_repository'],
        authority_resolvers=_authority_resolvers(snapshot),
    )
    repository.save_snapshot(snapshot)
    assert repository.get_snapshot(snapshot.snapshot_id) == snapshot

    # A persisted BLOCKED binding cannot be rewritten into a boundary-READY
    # claim, even with a self-consistent recomputed snapshot identity.
    forged_ready = _forged_snapshot(
        snapshot,
        readiness=snapshot.readiness.model_copy(
            update={'wave_boundary_ready': True}
        ),
    )
    with pytest.raises(
        ValueError,
        match='readiness does not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged_ready)

    forged_clean = _forged_snapshot(
        snapshot,
        unresolved_conditions=tuple(
            condition
            for condition in snapshot.unresolved_conditions
            if not condition.startswith('treatment_')
        ),
    )
    assert forged_clean.unresolved_conditions != snapshot.unresolved_conditions
    with pytest.raises(
        ValueError,
        match='unresolved conditions do not reproduce from exact authorities',
    ):
        repository.save_snapshot(forged_clean)


def test_snapshot_consumes_only_replay_validated_treatment_authorities(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    result, _item = _compile_result(
        fx,
        kind='both',
        instance_id='panel-snapshot-replay',
        target_domain='wave',
    )
    _persist_result(fx, result)
    snapshot = _snapshot(fx, result)
    repository = CadAcousticSnapshotRepository(
        fx['scene_repository'],
        r120_repository=fx['r120_repository'],
        treatment_boundary_repository=fx['overlay_repository'],
        authority_resolvers=_authority_resolvers(snapshot),
    )
    repository.save_snapshot(snapshot)

    # A self-hash-valid forged overlay/composition pair that alters derived
    # treatment capability must not become solver-facing snapshot authority.
    forged_overlay = _rehashed_overlay(result.overlay, thickness_m=0.25)
    forged_composition = _rehashed_composition(
        result.composition_request,
        attached_treatment_overlays=(
            forged_overlay.as_external_authority_ref(),
        ),
    )
    forged_result = result.model_copy(
        update={
            'overlay': forged_overlay,
            'composition_request': forged_composition,
            'r120_surface_binding': (
                adapt_treatment_boundary_composition_to_r120(forged_composition)
            ),
        }
    )
    forged_snapshot = _snapshot(fx, forged_result)

    path = fx['scene_repository'].path
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO cad_treatment_boundary_overlays(
                overlay_id,
                overlay_hash_sha256,
                scene_revision_id,
                compiled_geometry_id,
                treatment_definition_id,
                treatment_definition_version,
                treatment_placement_instance_id,
                treatment_placement_version,
                surface_binding_evaluation_hash_sha256,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forged_overlay.overlay_id,
                forged_overlay.overlay_hash_sha256,
                forged_overlay.exact_scene_revision_id,
                forged_overlay.exact_r120_compiled_geometry_id,
                forged_overlay.treatment_definition_id,
                forged_overlay.treatment_definition_version,
                forged_overlay.treatment_placement_instance_id,
                forged_overlay.treatment_placement_version,
                forged_overlay.surface_binding_evaluation_hash_sha256,
                forged_overlay.model_dump_json(),
                '2026-01-01T00:00:00+00:00',
            ),
        )
        connection.execute(
            """
            INSERT INTO cad_treatment_boundary_compositions(
                composition_id,
                composition_hash_sha256,
                scene_revision_id,
                compiled_geometry_id,
                host_surface_id,
                target_domain,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forged_composition.composition_id,
                forged_composition.composition_hash_sha256,
                forged_composition.exact_scene_revision_id,
                forged_composition.exact_r120_compiled_geometry_id,
                forged_composition.host_surface_id,
                forged_composition.target_domain,
                forged_composition.model_dump_json(),
                '2026-01-01T00:00:00+00:00',
            ),
        )

    with pytest.raises(ValueError, match='not reproducible'):
        repository.save_snapshot(forged_snapshot)

    # The honest snapshot remains readable and valid.
    assert repository.get_snapshot(snapshot.snapshot_id) == snapshot
