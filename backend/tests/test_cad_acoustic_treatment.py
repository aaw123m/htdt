from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_benchmark import AcousticMaterial, GeometricAcousticBand
from htdt.cad_acoustic_treatment import (
    AcousticTreatmentDefinition,
    TreatmentAcousticModel,
    TreatmentAcousticModelSubject,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceAuthority,
    TreatmentEvidenceSubject,
    TreatmentFrequencyBand,
    TreatmentLayer,
    TreatmentPhysicalParameters,
    TreatmentUncertainty,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    evaluate_treatment_prediction_capability,
    revise_treatment_placement,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument
from htdt.cad_system_variant import (
    VariantProvenanceItem,
    build_system_variant,
    materialize_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-19T11:30:00+00:00'
SURFACE_AUTHORITY_SHA = 'a' * 64


def _baseline(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id='treatment-fixture',
            room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_a = build_system_variant(
        baseline=revision,
        name='Treatment A',
        role_bindings=(),
        proposed_entities=(),
        provenance=(VariantProvenanceItem(key='treatment_plan', value='A'),),
        created_at_utc=NOW,
    )
    variant_b = build_system_variant(
        baseline=revision,
        name='Treatment B',
        role_bindings=(),
        proposed_entities=(),
        provenance=(VariantProvenanceItem(key='treatment_plan', value='B'),),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant_a)
    variant_repository.save_variant(variant_b)
    return scene_repository, variant_repository, revision, variant_a, variant_b


def _save_definition(
    repository: CadAcousticTreatmentRepository,
    definition: AcousticTreatmentDefinition,
    evidence: tuple[TreatmentEvidenceAuthority, ...],
) -> AcousticTreatmentDefinition:
    for item in evidence:
        repository.save_evidence(item)
    return repository.save_definition(definition)


def _porous_definition():
    dimensions = TreatmentDimensions(width_m=0.6, height_m=1.2, thickness_m=0.1)
    layers = (
        TreatmentLayer(
            layer_id='porous-core',
            material_name='mineral wool',
            thickness_m=0.1,
            density_kg_m3=48.0,
            airflow_resistivity_pa_s_m2=12000.0,
        ),
    )
    parameters = TreatmentPhysicalParameters(
        bulk_density_kg_m3=48.0,
        airflow_resistivity_pa_s_m2=12000.0,
    )
    band = TreatmentFrequencyBand(min_hz=125.0, max_hz=4000.0)
    uncertainty = TreatmentUncertainty(
        kind='quantified',
        value=0.05,
        unit='absorption_coefficient',
        note='fixture uncertainty',
    )
    material = AcousticMaterial(
        material_id='porous-panel-measured',
        provenance='Issue #171 test fixture',
        version='1',
        wave_model='unsupported',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(center_hz=125.0, absorption=0.35, scattering=0.05),
            GeometricAcousticBand(center_hz=500.0, absorption=0.90, scattering=0.05),
            GeometricAcousticBand(center_hz=2000.0, absorption=0.95, scattering=0.05),
        ),
    )
    evidence = build_treatment_evidence_authority(
        source_kind='measurement',
        source_id='lab-panel-600x1200x100',
        source_version='2026-09-01',
        source_sha256='1' * 64,
        reference='fixture measurement authority',
        extraction_id='fixture-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='porous-panel-100',
            definition_version='1.0',
            treatment_type='absorber_with_air_gap',
            dimensions=dimensions,
            air_gap_m=0.1,
            layers=layers,
            parameters=parameters,
            acoustic_model=TreatmentAcousticModelSubject(
                model_id='porous-panel-geometric-bands',
                model_version='1',
                evidence_basis='measured',
                valid_frequency_band=band,
                uncertainty=uncertainty,
                material=material,
            ),
        ),
    )
    provenance = evidence.as_provenance()
    acoustic_model = TreatmentAcousticModel(
        model_id='porous-panel-geometric-bands',
        model_version='1',
        evidence_basis='measured',
        valid_frequency_band=band,
        uncertainty=uncertainty,
        provenance=provenance,
        material=material,
    )
    definition = build_acoustic_treatment_definition(
        definition_id='porous-panel-100',
        version='1.0',
        name='100 mm porous panel',
        treatment_type='absorber_with_air_gap',
        provenance=provenance,
        dimensions=dimensions,
        air_gap_m=0.1,
        layers=layers,
        parameters=parameters,
        acoustic_model=acoustic_model,
    )
    return definition, (evidence,)


def _membrane_definition():
    dimensions = TreatmentDimensions(width_m=0.6, height_m=1.2, thickness_m=0.08)
    layers = (
        TreatmentLayer(
            layer_id='membrane',
            material_name='plywood membrane',
            thickness_m=0.006,
            surface_density_kg_m2=4.2,
        ),
        TreatmentLayer(
            layer_id='cavity-fill',
            material_name='porous fill',
            thickness_m=0.05,
            density_kg_m3=32.0,
        ),
    )
    parameters = TreatmentPhysicalParameters(
        membrane_surface_density_kg_m2=4.2,
        cavity_depth_m=0.074,
    )
    evidence = build_treatment_evidence_authority(
        source_kind='user_defined',
        source_id='membrane-concept',
        source_version='1',
        reference='geometry only; acoustic model not yet qualified',
        extraction_id='manual-declaration',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='membrane-panel-80',
            definition_version='1.0',
            treatment_type='membrane_panel_absorber',
            dimensions=dimensions,
            air_gap_m=0.0,
            layers=layers,
            parameters=parameters,
        ),
    )
    definition = build_acoustic_treatment_definition(
        definition_id='membrane-panel-80',
        version='1.0',
        name='80 mm membrane absorber',
        treatment_type='membrane_panel_absorber',
        provenance=evidence.as_provenance(),
        dimensions=dimensions,
        layers=layers,
        parameters=parameters,
        acoustic_model=None,
    )
    return definition, (evidence,)


def test_definition_identity_is_deterministic_and_does_not_infer_wave_impedance() -> None:
    first, _first_evidence = _porous_definition()
    second, _second_evidence = _porous_definition()

    assert first == second
    assert first.definition_sha256 == second.definition_sha256
    assert first.authority_role == 'attached_acoustic_treatment'
    assert first.dimensions.thickness_m == 0.1
    assert first.air_gap_m == 0.1
    assert first.layers[0].density_kg_m3 == 48.0

    assert first.acoustic_model is not None
    material = first.acoustic_model.material
    assert material.geometric_model == 'banded'
    assert material.wave_model == 'unsupported'
    assert material.specific_impedance == ()

    capability = evaluate_treatment_prediction_capability(first)
    assert capability.evidence_basis == 'measured'
    assert capability.geometric_material_capability == 'SUPPORTED'
    assert capability.wave_material_capability == 'UNKNOWN'
    assert capability.solver_prediction_readiness == 'UNKNOWN'


def test_unsupported_membrane_physics_remains_unknown_fail_closed() -> None:
    definition, _evidence = _membrane_definition()
    capability = evaluate_treatment_prediction_capability(definition)

    assert definition.acoustic_model is None
    assert capability.wave_material_capability == 'UNKNOWN'
    assert capability.geometric_material_capability == 'UNKNOWN'
    assert capability.solver_prediction_readiness == 'UNKNOWN'
    assert capability.uncertainty.kind == 'unknown'
    assert any('placement alone' in reason for reason in capability.reasons)


def test_no_treatment_baseline_and_ab_placements_persist_without_mutating_scene(
    tmp_path: Path,
) -> None:
    scene_repository, variant_repository, baseline, variant_a, variant_b = _baseline(tmp_path)
    before = baseline.document

    assert materialize_system_variant(baseline, variant_a) == before
    assert materialize_system_variant(baseline, variant_b) == before

    repository = CadAcousticTreatmentRepository(scene_repository, variant_repository)
    porous, porous_evidence = _porous_definition()
    membrane, membrane_evidence = _membrane_definition()
    porous = _save_definition(repository, porous, porous_evidence)
    membrane = _save_definition(repository, membrane, membrane_evidence)

    assert repository.list_placements_for_variant('no-treatment') == ()

    placement_a = build_treatment_placement(
        definition=porous,
        revision=baseline,
        instance_id='panel-a-01',
        position=Position3(x_m=0.05, y_m=2.0, z_m=1.2),
        coverage=TreatmentCoverage(
            width_m=porous.dimensions.width_m,
            height_m=porous.dimensions.height_m,
            host_surface_fraction=0.08,
        ),
        system_variant=variant_a,
        host_surface_id='wall-left',
        host_surface_authority_sha256=SURFACE_AUTHORITY_SHA,
    )
    placement_b = build_treatment_placement(
        definition=membrane,
        revision=baseline,
        instance_id='panel-b-01',
        position=Position3(x_m=5.95, y_m=2.0, z_m=1.2),
        coverage=TreatmentCoverage(
            width_m=membrane.dimensions.width_m,
            height_m=membrane.dimensions.height_m,
        ),
        system_variant=variant_b,
        host_surface_id='wall-right',
        host_surface_authority_sha256=SURFACE_AUTHORITY_SHA,
    )
    repository.save_placement(placement_a)
    repository.save_placement(placement_b)

    assert scene_repository.get(baseline.revision_id).document == before
    assert len(repository.list_placements_for_variant(variant_a.variant_id)) == 1
    assert len(repository.list_placements_for_variant(variant_b.variant_id)) == 1

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened = CadAcousticTreatmentRepository(reopened_scene, reopened_variants)
    assert reopened.get_definition(porous.definition_id, porous.version) == porous
    assert reopened.get_placement('panel-a-01', 1) == placement_a
    assert reopened.get_placement('panel-b-01', 1) == placement_b
    assert reopened_scene.get(baseline.revision_id).document == before


def test_proposed_to_installed_lifecycle_is_append_only_and_exact(tmp_path: Path) -> None:
    scene_repository, variant_repository, baseline, variant_a, _variant_b = _baseline(tmp_path)
    repository = CadAcousticTreatmentRepository(scene_repository, variant_repository)
    porous_definition, porous_evidence = _porous_definition()
    porous = _save_definition(repository, porous_definition, porous_evidence)

    proposed = build_treatment_placement(
        definition=porous,
        revision=baseline,
        instance_id='panel-lineage-01',
        position=Position3(x_m=0.05, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant_a,
        host_surface_id='wall-left',
        host_surface_authority_sha256=SURFACE_AUTHORITY_SHA,
    )
    repository.save_placement(proposed)

    installed = revise_treatment_placement(
        proposed,
        revision=baseline,
        lifecycle='installed',
        position=Position3(x_m=0.05, y_m=1.02, z_m=1.2),
        system_variant=variant_a,
    )
    repository.save_placement(installed)

    assert proposed.lifecycle == 'proposed'
    assert installed.lifecycle == 'installed'
    assert installed.placement_version == 2
    assert installed.previous_placement_version == 1
    assert installed.previous_placement_sha256 == proposed.placement_sha256
    assert installed.definition_sha256 == proposed.definition_sha256
    assert repository.get_placement(proposed.instance_id, 1) == proposed
    assert repository.latest_placement(proposed.instance_id) == installed

    with pytest.raises(ValueError, match='terminal'):
        revise_treatment_placement(
            installed,
            revision=baseline,
            lifecycle='installed',
            system_variant=variant_a,
        )
