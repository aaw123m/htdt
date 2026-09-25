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
from htdt.cad_acoustic_treatment import (
    TreatmentAcousticModel,
    TreatmentAcousticModelSubject,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentFrequencyBand,
    TreatmentLayer,
    TreatmentProvenance,
    TreatmentUncertainty,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneDocument, SceneEntity
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
    compile_r120_geometry,
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
    compile_treatment_boundary_overlays,
)
from htdt.treatment_boundary_overlay_repository import TreatmentBoundaryOverlayRepository


# Closed unit box [0,1]^3, consistently outward-wound. The top face (z=1,
# faces 3-4) is the planar 'host wall' treatments mount on; the remaining
# faces form the room shell.
BOX_ROOM = b'''\
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 3 4 8
f 3 8 7
f 1 8 4
f 1 5 8
f 2 3 7
f 2 7 6
'''


def _external(name: str, token: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=name,
        authority_version='fixture-v1',
        semantic_hash_sha256=token * 64,
    )


def _model_material(kind: str) -> AcousticMaterial | None:
    material: AcousticMaterial
    if kind == 'none':
        return None
    if kind == 'geometric':
        material = AcousticMaterial(
            material_id='fixture-geometric-treatment',
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
                GeometricAcousticBand(
                    center_hz=1000.0,
                    absorption=0.55,
                    scattering=0.25,
                ),
            ),
        )
    elif kind == 'wave':
        material = AcousticMaterial(
            material_id='fixture-wave-treatment',
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
            material_id='fixture-both-treatment',
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
    dimensions = TreatmentDimensions(width_m=1.0, height_m=1.0, thickness_m=0.1)
    layers = (
        TreatmentLayer(
            layer_id='core',
            material_name='fixture core',
            thickness_m=0.1,
            density_kg_m3=48.0,
        ),
    )
    material = _model_material(kind)
    band = TreatmentFrequencyBand(min_hz=80.0, max_hz=4000.0)
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
            model_id=f'fixture-{kind}-model',
            model_version='1',
            evidence_basis='measured',
            valid_frequency_band=band,
            uncertainty=uncertainty,
            material=material,
        )
    )
    evidence = build_treatment_evidence_authority(
        source_kind='measurement',
        source_id='treatment-boundary-fixture',
        source_version='1',
        source_sha256='1' * 64,
        reference='Issue #171 x #101 boundary overlay fixture',
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
            model_id=f'fixture-{kind}-model',
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
    mesh = import_raw_visual_mesh(BOX_ROOM, source_name='treatment-boundary.obj')
    triangle_ids = raw_triangle_ids(mesh)
    host_triangle_ids = (triangle_ids[2], triangle_ids[3])
    conversion = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='fixture coordinates are exact HTDT metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=host_triangle_ids,
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key='room-rest',
                triangle_ids=tuple(
                    triangle_id
                    for index, triangle_id in enumerate(triangle_ids)
                    if index not in (2, 3)
                ),
                semantic_class='room_boundary',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, conversion)
    revision = scene_repository.save(
        SceneDocument(
            document_id='treatment-boundary-fixture',
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
    request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(
        revision,
        request,
        surface_boundary_bindings=(base_binding,),
    )
    r120_repository = R120GeometryCompilerRepository(scene_repository)
    r120_repository.save_compiled_geometry(compiled)
    variant_repository = CadSystemVariantRepository(scene_repository)
    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        variant_repository,
    )
    return {
        'scene_repository': scene_repository,
        'treatment_repository': treatment_repository,
        'r120_repository': r120_repository,
        'revision': revision,
        'compiled': compiled,
        'surface_id': surface_id,
        'base_binding': base_binding,
    }


def _input(
    fixture,
    definition,
    *,
    instance_id: str,
    fraction: float | None = 1.0,
    installed: bool = False,
    evaluated_revision=None,
):
    treatment_repository = fixture['treatment_repository']
    revision = fixture['revision']
    definition, evidence = definition
    for item in evidence:
        treatment_repository.save_evidence(item)
    definition = treatment_repository.save_definition(definition)
    proposed = build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id=instance_id,
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        coverage=TreatmentCoverage(
            width_m=1.0,
            height_m=1.0,
            host_surface_fraction=fraction,
        ),
        host_surface_id=fixture['surface_id'],
    )
    treatment_repository.save_placement(proposed)
    placement = proposed
    if installed:
        placement = revise_treatment_placement(
            proposed,
            revision=revision,
            lifecycle='installed',
        )
        treatment_repository.save_placement(placement)
    target_revision = revision if evaluated_revision is None else evaluated_revision
    evaluation = treatment_repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=target_revision.revision_id,
    )
    return TreatmentBoundaryCompileInput(
        definition=definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )


def _compile(fixture, item, target: str, *, revision=None, compiled=None):
    return compile_treatment_boundary_overlays(
        fixture['revision'] if revision is None else revision,
        fixture['compiled'] if compiled is None else compiled,
        (item,),
        target_domain=target,
        base_surface_bindings=(fixture['base_binding'],),
    )[0]


def _second_revision_and_compiled(fixture):
    original = fixture['revision']
    document = SceneDocument(
        document_id=original.document.document_id,
        schema_version=original.document.schema_version,
        room=original.document.room,
        wall_topology=original.document.wall_topology,
        r120_semantic_geometry=original.document.r120_semantic_geometry,
        entities=(
            SceneEntity(
                entity_id='revision-marker',
                kind='measurement_point',
                name='revision marker',
                position=Position3(x_m=0.1, y_m=0.1, z_m=0.1),
            ),
        ),
    )
    revision = fixture['scene_repository'].save(
        document,
        parent_revision_id=original.revision_id,
    ).revision
    request = make_r120_geometry_compilation_request(
        revision,
        geometric_tolerance_m=1.0e-6,
    )
    compiled = compile_r120_geometry(
        revision,
        request,
        surface_boundary_bindings=(fixture['base_binding'],),
    )
    return revision, compiled


def test_full_surface_geometric_treatment_is_available_without_coefficient_folding(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('geometric'), instance_id='panel-geometric')
    result = _compile(fixture, item, 'geometric')

    assert result.status == 'AVAILABLE'
    assert result.overlay is not None
    assert result.overlay.geometric_capability_state == 'AVAILABLE'
    assert result.overlay.wave_capability_state == 'UNKNOWN'
    assert result.overlay.transmission_capability_state == 'UNKNOWN'
    bands = item.definition.acoustic_model.material.geometric_bands
    assert bands[0].absorption == 0.40
    assert bands[0].scattering == 0.20
    assert result.composition_request.transmission_capability_state == 'UNKNOWN'


def test_wave_capable_treatment_requires_concrete_wave_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('wave'), instance_id='panel-wave')

    wave = _compile(fixture, item, 'wave')
    geometric = _compile(fixture, item, 'geometric')

    assert wave.status == 'AVAILABLE'
    assert wave.overlay.wave_capability_state == 'AVAILABLE'
    assert wave.overlay.wave_material_candidate_ref is not None
    assert geometric.status == 'BLOCKED_GEOMETRIC_MODEL_UNAVAILABLE'
    assert item.definition.acoustic_model.material.specific_impedance


def test_no_acoustic_model_is_blocked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('none'), instance_id='panel-no-model')

    result = _compile(fixture, item, 'geometric')

    assert result.status == 'BLOCKED_NO_ACOUSTIC_MODEL'
    assert result.overlay.wave_capability_state == 'UNKNOWN'
    assert result.overlay.geometric_capability_state == 'UNKNOWN'
    assert result.composition_request is None


def test_geometric_only_treatment_never_becomes_fake_wave_impedance(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('geometric'), instance_id='panel-ga-only')

    wave = _compile(fixture, item, 'wave')
    geometric = _compile(fixture, item, 'geometric')

    assert wave.status == 'BLOCKED_WAVE_MODEL_UNAVAILABLE'
    assert wave.overlay.wave_material_candidate_ref is None
    assert item.definition.acoustic_model.material.specific_impedance == ()
    assert geometric.status == 'AVAILABLE'


def test_partial_coverage_compiles_to_derived_patch(tmp_path: Path) -> None:
    """#976: a partial wall panel compiles to a clipped solver-facing patch,
    never blocked and never silently full-surface."""
    fixture = _fixture(tmp_path)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-partial',
        fraction=None,
    )
    # Shrink the panel: 0.4x0.4 m centered off-centre on the 1x1 host face.
    placement = revise_treatment_placement(
        item.placement,
        revision=fixture['revision'],
        position=Position3(x_m=0.3, y_m=0.3, z_m=1.0),
        coverage=TreatmentCoverage(width_m=0.4, height_m=0.4),
    )
    fixture['treatment_repository'].save_placement(placement)
    evaluation = fixture['treatment_repository'].evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fixture['revision'].revision_id,
    )
    item = TreatmentBoundaryCompileInput(
        definition=item.definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )

    result = _compile(fixture, item, 'geometric')

    assert result.status == 'AVAILABLE'
    footprint = result.overlay.derived_footprint
    assert footprint is not None
    assert footprint.patch_area_m2 == pytest.approx(0.16)
    assert footprint.derived_fraction == pytest.approx(0.16)
    assert footprint.covers_entire_surface is False
    assert result.composition_request.derived_coverage_fraction == (
        pytest.approx(0.16)
    )
    assert result.composition_request.covers_entire_surface is False
    assert (
        result.composition_request.treatment_footprint_sha256
        == footprint.footprint_sha256
    )
    # The untreated remainder keeps the exact base boundary authorities.
    assert result.composition_request.base_material_authority == (
        fixture['base_binding'].material_authority
    )
    assert result.composition_request.base_boundary_physics_authority == (
        fixture['base_binding'].boundary_physics_authority
    )


def test_supplied_fraction_disagreeing_with_derived_geometry_fails_closed(
    tmp_path: Path,
) -> None:
    """#976: a caller-asserted fraction can never authorize the patch."""
    fixture = _fixture(tmp_path)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-mismatch',
        fraction=0.5,
    )

    result = _compile(fixture, item, 'geometric')

    # Placement covers the full host face; the supplied 0.5 scalar disagrees
    # with the derived 1.0 fraction and is rejected rather than trusted.
    assert result.status == 'BLOCKED_COVERAGE_MISMATCH'
    assert result.overlay.derived_footprint.derived_fraction == (
        pytest.approx(1.0)
    )
    assert result.r120_surface_binding is None
    assert result.composition_request is None


def test_supplied_full_coverage_claim_requires_derived_proof(
    tmp_path: Path,
) -> None:
    """#976: host_surface_fraction=1.0 on a small panel cannot become a
    whole-surface replacement."""
    fixture = _fixture(tmp_path)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-fake-full',
        fraction=1.0,
    )
    placement = revise_treatment_placement(
        item.placement,
        revision=fixture['revision'],
        position=Position3(x_m=0.3, y_m=0.3, z_m=1.0),
        coverage=TreatmentCoverage(
            width_m=0.4,
            height_m=0.4,
            host_surface_fraction=1.0,
        ),
    )
    fixture['treatment_repository'].save_placement(placement)
    evaluation = fixture['treatment_repository'].evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fixture['revision'].revision_id,
    )
    item = TreatmentBoundaryCompileInput(
        definition=item.definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )

    result = _compile(fixture, item, 'geometric')

    assert result.status == 'BLOCKED_COVERAGE_MISMATCH'
    assert result.overlay.derived_footprint.derived_fraction == (
        pytest.approx(0.16)
    )
    assert result.composition_request is None


def test_disjoint_panels_on_one_surface_both_compile(tmp_path: Path) -> None:
    """#976: only true geometric overlap is blocked; separated panels on one
    wall each get their own derived patch."""
    fixture = _fixture(tmp_path)

    def panel(instance_id: str, x_m: float, y_m: float):
        item = _input(
            fixture,
            _definition('both', instance_id),
            instance_id=instance_id,
            fraction=None,
        )
        placement = revise_treatment_placement(
            item.placement,
            revision=fixture['revision'],
            position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
            coverage=TreatmentCoverage(width_m=0.4, height_m=0.4),
        )
        fixture['treatment_repository'].save_placement(placement)
        evaluation = fixture[
            'treatment_repository'
        ].evaluate_placement_surface_binding(
            placement,
            scene_revision_id=fixture['revision'].revision_id,
        )
        return TreatmentBoundaryCompileInput(
            definition=item.definition,
            placement=placement,
            surface_binding_evaluation=evaluation,
        )

    first = panel('panel-left', 0.2, 0.2)
    second = panel('panel-right', 0.8, 0.8)

    results = compile_treatment_boundary_overlays(
        fixture['revision'],
        fixture['compiled'],
        (first, second),
        target_domain='geometric',
        base_surface_bindings=(fixture['base_binding'],),
    )

    assert [item.status for item in results] == ['AVAILABLE', 'AVAILABLE']
    footprints = [item.overlay.derived_footprint for item in results]
    assert all(
        item is not None
        and item.derived_fraction == pytest.approx(0.16)
        and not item.covers_entire_surface
        for item in footprints
    )
    assert (
        footprints[0].footprint_sha256 != footprints[1].footprint_sha256
    )


def test_stale_surface_binding_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    revision2, compiled2 = _second_revision_and_compiled(fixture)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-stale-surface',
        evaluated_revision=revision2,
    )

    result = _compile(
        fixture,
        item,
        'geometric',
        revision=revision2,
        compiled=compiled2,
    )

    assert item.surface_binding_evaluation.binding_state == 'stale_scene_revision'
    assert result.status == 'BLOCKED_STALE_SURFACE'


def test_stale_r120_compiled_geometry_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    revision2, _compiled2 = _second_revision_and_compiled(fixture)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-stale-r120',
        evaluated_revision=revision2,
    )

    result = _compile(
        fixture,
        item,
        'geometric',
        revision=revision2,
        compiled=fixture['compiled'],
    )

    assert result.status == 'BLOCKED_STALE_R120_COMPILED_GEOMETRY'


def test_multiple_treatments_on_same_surface_fail_closed_as_overlap(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = _input(
        fixture,
        _definition('both', '-a'),
        instance_id='panel-overlap-a',
    )
    second = _input(
        fixture,
        _definition('both', '-b'),
        instance_id='panel-overlap-b',
    )

    results = compile_treatment_boundary_overlays(
        fixture['revision'],
        fixture['compiled'],
        (first, second),
        target_domain='geometric',
        base_surface_bindings=(fixture['base_binding'],),
    )

    assert [item.status for item in results] == ['BLOCKED_OVERLAP', 'BLOCKED_OVERLAP']
    assert all(item.composition_request is None for item in results)


def test_proposed_lifecycle_is_exact_solver_scenario_selection(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-proposed')
    result = _compile(fixture, item, 'wave')

    assert result.status == 'AVAILABLE'
    assert result.overlay.lifecycle == 'proposed'
    assert result.composition_request.selected_treatment_lifecycle == 'proposed'


def test_installed_lifecycle_is_distinct_and_not_implicitly_selected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-installed',
        installed=True,
    )
    result = _compile(fixture, item, 'wave')

    assert item.placement.lifecycle == 'installed'
    assert result.status == 'AVAILABLE'
    assert result.overlay.lifecycle == 'installed'
    assert result.composition_request.selected_treatment_lifecycle == 'installed'


def test_base_material_is_preserved_and_composition_keeps_base_boundary(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-base-preserved')
    result = _compile(fixture, item, 'wave')

    assert result.status == 'AVAILABLE'
    assert result.r120_surface_binding.material_authority == (
        fixture['base_binding'].material_authority
    )
    assert result.composition_request.base_material_authority == (
        fixture['base_binding'].material_authority
    )
    assert result.composition_request.base_boundary_physics_authority == (
        fixture['base_binding'].boundary_physics_authority
    )
    assert (
        result.r120_surface_binding.boundary_physics_authority.authority_id
        == result.composition_request.composition_id
    )


def test_same_exact_input_produces_same_overlay_and_composition_hash(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-deterministic')

    first = _compile(fixture, item, 'geometric')
    second = _compile(fixture, item, 'geometric')

    assert first.status == second.status == 'AVAILABLE'
    assert first.overlay.overlay_hash_sha256 == second.overlay.overlay_hash_sha256
    assert first.overlay.overlay_id == second.overlay.overlay_id
    assert (
        first.composition_request.composition_hash_sha256
        == second.composition_request.composition_hash_sha256
    )


def test_overlay_and_composition_save_reopen_reresolve_all_authorities(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-persisted')
    result = _compile(fixture, item, 'geometric')
    assert result.status == 'AVAILABLE'

    repository = TreatmentBoundaryOverlayRepository(
        fixture['scene_repository'],
        fixture['treatment_repository'],
        fixture['r120_repository'],
    )
    repository.save_overlay(result.overlay)
    repository.save_composition(result.composition_request)

    reopened_scene = SceneRepository(fixture['scene_repository'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_treatments = CadAcousticTreatmentRepository(
        reopened_scene,
        reopened_variants,
    )
    reopened_r120 = R120GeometryCompilerRepository(reopened_scene)
    reopened = TreatmentBoundaryOverlayRepository(
        reopened_scene,
        reopened_treatments,
        reopened_r120,
    )

    assert reopened.get_overlay(result.overlay.overlay_id) == result.overlay
    assert (
        reopened.get_composition(result.composition_request.composition_id)
        == result.composition_request
    )


def _overlay_repository(fixture) -> TreatmentBoundaryOverlayRepository:
    return TreatmentBoundaryOverlayRepository(
        fixture['scene_repository'],
        fixture['treatment_repository'],
        fixture['r120_repository'],
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


def _insert_overlay_row(path: Path, overlay: TreatmentBoundaryOverlay) -> None:
    """Insert a fully self-consistent overlay row, bypassing save validation."""

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
                overlay.overlay_id,
                overlay.overlay_hash_sha256,
                overlay.exact_scene_revision_id,
                overlay.exact_r120_compiled_geometry_id,
                overlay.treatment_definition_id,
                overlay.treatment_definition_version,
                overlay.treatment_placement_instance_id,
                overlay.treatment_placement_version,
                overlay.surface_binding_evaluation_hash_sha256,
                overlay.model_dump_json(),
                '2026-01-01T00:00:00+00:00',
            ),
        )


def _insert_composition_row(
    path: Path,
    composition: TreatmentBoundaryCompositionRequest,
) -> None:
    """Insert a fully self-consistent composition row, bypassing save."""

    with closing(sqlite3.connect(path)) as connection, connection:
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
                composition.composition_id,
                composition.composition_hash_sha256,
                composition.exact_scene_revision_id,
                composition.exact_r120_compiled_geometry_id,
                composition.host_surface_id,
                composition.target_domain,
                composition.model_dump_json(),
                '2026-01-01T00:00:00+00:00',
            ),
        )


def test_save_overlay_replays_canonical_compiler_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-tamper-save')
    result = _compile(fixture, item, 'wave')
    assert result.status == 'AVAILABLE'
    repository = _overlay_repository(fixture)

    repository.save_overlay(result.overlay)
    repository.save_overlay(result.overlay)
    assert repository.get_overlay(result.overlay.overlay_id) == result.overlay

    overlay = result.overlay
    tampered_variants = (
        _rehashed_overlay(overlay, thickness_m=0.25),
        _rehashed_overlay(overlay, air_gap_m=0.0),
        _rehashed_overlay(
            overlay,
            treatment_coverage=TreatmentCoverage(
                width_m=1.0,
                height_m=1.0,
                host_surface_fraction=0.5,
            ),
        ),
        _rehashed_overlay(overlay, evidence_basis='inferred'),
        _rehashed_overlay(
            overlay,
            valid_frequency_band=TreatmentFrequencyBand(
                min_hz=100.0,
                max_hz=2000.0,
            ),
        ),
        _rehashed_overlay(overlay, treatment_acoustic_model_version='2'),
        _rehashed_overlay(
            overlay,
            uncertainty=TreatmentUncertainty(
                kind='quantified',
                value=0.5,
                unit='fixture',
                note='tampered uncertainty',
            ),
        ),
        _rehashed_overlay(
            overlay,
            wave_capability_state='UNKNOWN',
            wave_material_candidate_ref=None,
        ),
        _rehashed_overlay(
            overlay,
            geometric_capability_state='UNKNOWN',
            geometric_material_candidate_ref=None,
        ),
        _rehashed_overlay(
            overlay,
            wave_material_candidate_ref=_external(
                'attacker-wave-material',
                '9',
            ),
        ),
        _rehashed_overlay(
            overlay,
            geometric_material_candidate_ref=_external(
                'attacker-geometric-material',
                '8',
            ),
        ),
    )
    for tampered in tampered_variants:
        assert tampered.overlay_id != overlay.overlay_id
        with pytest.raises(ValueError):
            repository.save_overlay(tampered)


def test_persisted_overlay_row_tampering_fails_closed_on_read(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-row-tamper')
    result = _compile(fixture, item, 'wave')
    repository = _overlay_repository(fixture)
    repository.save_overlay(result.overlay)

    tampered = _rehashed_overlay(result.overlay, thickness_m=0.25)
    _insert_overlay_row(fixture['scene_repository'].path, tampered)

    with pytest.raises(ValueError, match='not reproducible'):
        repository.get_overlay(tampered.overlay_id)

    # In-place payload tamper under the original overlay key is also rejected.
    with closing(
        sqlite3.connect(fixture['scene_repository'].path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_treatment_boundary_overlays
            SET payload_json=?
            WHERE overlay_id=?
            """,
            (tampered.model_dump_json(), result.overlay.overlay_id),
        )
    with pytest.raises(ValueError):
        repository.get_overlay(result.overlay.overlay_id)
    with pytest.raises(ValueError):
        repository.save_overlay(result.overlay)


def test_save_composition_replays_canonical_compiler_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-tamper-comp')
    result = _compile(fixture, item, 'wave')
    assert result.status == 'AVAILABLE'
    repository = _overlay_repository(fixture)
    repository.save_overlay(result.overlay)
    repository.save_composition(result.composition_request)
    repository.save_composition(result.composition_request)

    composition = result.composition_request
    assert repository.get_composition(composition.composition_id) == composition

    forged_overlay = _rehashed_overlay(result.overlay, thickness_m=0.25)
    # Another persisted overlay exposes a real but incorrect material authority.
    other = _input(
        fixture,
        _definition('wave', '-other'),
        instance_id='panel-other-material',
    )
    other_result = _compile(fixture, other, 'wave')
    repository.save_overlay(other_result.overlay)
    other_material_ref = other_result.overlay.wave_material_candidate_ref
    assert other_material_ref != result.overlay.wave_material_candidate_ref
    tampered_variants = (
        # base material/boundary refs moved away from the exact R120 binding
        _rehashed_composition(
            composition,
            base_material_authority=_external('attacker-base-material', '7'),
        ),
        _rehashed_composition(
            composition,
            base_boundary_physics_authority=_external(
                'attacker-base-physics',
                '6',
            ),
        ),
        # selected treatment material replaced by another valid-looking ref
        _rehashed_composition(
            composition,
            selected_treatment_material_authorities=(other_material_ref,),
        ),
        _rehashed_composition(
            composition,
            selected_treatment_material_authorities=(
                _external('attacker-treatment-material', '5'),
            ),
        ),
        # lifecycle flip away from the resolved overlay lifecycle
        _rehashed_composition(
            composition,
            selected_treatment_lifecycle='installed',
        ),
        # a forged overlay cannot be substituted for the canonical one
        _rehashed_composition(
            composition,
            attached_treatment_overlays=(
                forged_overlay.as_external_authority_ref(),
            ),
        ),
    )
    for tampered in tampered_variants:
        assert tampered.composition_id != composition.composition_id
        with pytest.raises(ValueError):
            repository.save_composition(tampered)

    # The canonical composition for the sibling target domain remains valid.
    geometric = _compile(fixture, item, 'geometric')
    assert geometric.status == 'AVAILABLE'
    assert geometric.composition_request.composition_id != composition.composition_id
    repository.save_composition(geometric.composition_request)
    assert (
        repository.get_composition(geometric.composition_request.composition_id)
        == geometric.composition_request
    )


def test_composition_rejects_multi_overlay_attachments(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = _input(
        fixture,
        _definition('both', '-first'),
        instance_id='panel-multi-a',
    )
    second = _input(
        fixture,
        _definition('both', '-second'),
        instance_id='panel-multi-b',
    )
    first_result = _compile(fixture, first, 'wave')
    second_result = _compile(fixture, second, 'wave')
    assert first_result.status == second_result.status == 'AVAILABLE'
    repository = _overlay_repository(fixture)
    repository.save_overlay(first_result.overlay)
    repository.save_overlay(second_result.overlay)

    composition = first_result.composition_request
    multi_overlay = _rehashed_composition(
        composition,
        attached_treatment_overlays=(
            first_result.overlay.as_external_authority_ref(),
            second_result.overlay.as_external_authority_ref(),
        ),
        selected_treatment_material_authorities=(
            first_result.overlay.wave_material_candidate_ref,
            second_result.overlay.wave_material_candidate_ref,
        ),
    )
    with pytest.raises(ValueError, match='exactly one canonical overlay'):
        repository.save_composition(multi_overlay)


def test_persisted_composition_row_tampering_fails_closed_on_read(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-comp-read')
    result = _compile(fixture, item, 'wave')
    repository = _overlay_repository(fixture)
    repository.save_overlay(result.overlay)
    repository.save_composition(result.composition_request)

    tampered = _rehashed_composition(
        result.composition_request,
        base_material_authority=_external('attacker-base-material', '7'),
    )
    _insert_composition_row(fixture['scene_repository'].path, tampered)

    with pytest.raises(ValueError, match='not reproducible'):
        repository.get_composition(tampered.composition_id)

    # In-place payload tamper under the original composition key is rejected.
    with closing(
        sqlite3.connect(fixture['scene_repository'].path)
    ) as connection, connection:
        connection.execute(
            """
            UPDATE cad_treatment_boundary_compositions
            SET payload_json=?
            WHERE composition_id=?
            """,
            (
                tampered.model_dump_json(),
                result.composition_request.composition_id,
            ),
        )
    with pytest.raises(ValueError):
        repository.get_composition(result.composition_request.composition_id)


def test_overlay_save_fails_closed_when_placement_authority_is_missing(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    treatment_repository = fixture['treatment_repository']
    definition, evidence = _definition('both')
    for item in evidence:
        treatment_repository.save_evidence(item)
    definition = treatment_repository.save_definition(definition)
    placement = build_treatment_placement(
        definition=definition,
        revision=fixture['revision'],
        instance_id='panel-unsaved-placement',
        position=Position3(x_m=0.5, y_m=0.5, z_m=1.0),
        coverage=TreatmentCoverage(
            width_m=1.0,
            height_m=1.0,
            host_surface_fraction=1.0,
        ),
        host_surface_id=fixture['surface_id'],
    )
    evaluation = treatment_repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fixture['revision'].revision_id,
    )
    item = TreatmentBoundaryCompileInput(
        definition=definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )
    result = _compile(fixture, item, 'wave')
    assert result.status == 'AVAILABLE'

    repository = _overlay_repository(fixture)
    with pytest.raises(ValueError, match='placement does not exist'):
        repository.save_overlay(result.overlay)


def test_overlay_and_composition_reads_fail_closed_when_authorities_disappear(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(
        fixture,
        _definition('both'),
        instance_id='panel-dangling-authority',
    )
    result = _compile(fixture, item, 'wave')
    repository = _overlay_repository(fixture)
    repository.save_overlay(result.overlay)
    repository.save_composition(result.composition_request)
    path = fixture['scene_repository'].path

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_acoustic_treatment_placements WHERE instance_id=?',
            (item.placement.instance_id,),
        )
    with pytest.raises(ValueError, match='placement does not exist'):
        repository.get_overlay(result.overlay.overlay_id)
    with pytest.raises(ValueError, match='placement does not exist'):
        repository.get_composition(result.composition_request.composition_id)


def test_composition_fails_closed_when_attached_overlay_disappears(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-lost-overlay')
    result = _compile(fixture, item, 'wave')
    repository = _overlay_repository(fixture)
    repository.save_overlay(result.overlay)
    repository.save_composition(result.composition_request)
    path = fixture['scene_repository'].path

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('DELETE FROM cad_treatment_boundary_overlays')

    with pytest.raises(ValueError, match='unpersisted overlay'):
        repository.get_composition(result.composition_request.composition_id)


def test_composition_rejects_overlay_refs_that_do_not_resolve(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    item = _input(fixture, _definition('both'), instance_id='panel-ghost-ref')
    result = _compile(fixture, item, 'wave')
    repository = _overlay_repository(fixture)

    forged_composition = _rehashed_composition(
        result.composition_request,
        attached_treatment_overlays=(
            _external(f'treatment-boundary-overlay:{"0" * 64}', 'a'),
        ),
    )
    with pytest.raises(ValueError, match='unpersisted overlay'):
        repository.save_composition(forged_composition)
