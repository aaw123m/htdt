from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_treatment import (
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentLayer,
    TreatmentPhysicalParameters,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
)
from htdt.cad_acoustic_treatment_comparison import (
    CadAcousticTreatmentComparisonRepository,
    build_treatment_design_candidate,
    build_treatment_design_comparison,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument
from htdt.cad_system_variant import VariantProvenanceItem, build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-20T00:00:00+00:00'


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        SceneDocument(
            document_id='treatment-comparison-fixture',
            room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_a = build_system_variant(
        baseline=baseline,
        name='Treatment A',
        role_bindings=(),
        proposed_entities=(),
        provenance=(VariantProvenanceItem(key='treatment_plan', value='A'),),
        created_at_utc=NOW,
    )
    variant_b = build_system_variant(
        baseline=baseline,
        name='Treatment B',
        role_bindings=(),
        proposed_entities=(),
        provenance=(VariantProvenanceItem(key='treatment_plan', value='B'),),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant_a)
    variant_repository.save_variant(variant_b)

    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        variant_repository,
    )
    dimensions = TreatmentDimensions(
        width_m=0.6,
        height_m=1.2,
        thickness_m=0.1,
    )
    layers = (
        TreatmentLayer(
            layer_id='core',
            material_name='porous core',
            thickness_m=0.1,
            density_kg_m3=48.0,
        ),
    )
    evidence = build_treatment_evidence_authority(
        source_kind='user_defined',
        source_id='comparison-panel',
        source_version='1',
        extraction_id='manual-declaration',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='comparison-panel',
            definition_version='1',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.0,
            layers=layers,
            parameters=TreatmentPhysicalParameters(),
        ),
    )
    definition = build_acoustic_treatment_definition(
        definition_id='comparison-panel',
        version='1',
        name='Comparison panel',
        treatment_type='porous_absorber',
        provenance=evidence.as_provenance(),
        dimensions=dimensions,
        layers=layers,
    )
    treatment_repository.save_evidence(evidence)
    treatment_repository.save_definition(definition)
    placement_a = build_treatment_placement(
        definition=definition,
        revision=baseline,
        instance_id='panel-a',
        position=Position3(x_m=0.1, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant_a,
    )
    placement_b = build_treatment_placement(
        definition=definition,
        revision=baseline,
        instance_id='panel-b',
        position=Position3(x_m=5.9, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant_b,
    )
    treatment_repository.save_placement(placement_a)
    treatment_repository.save_placement(placement_b)
    return (
        scene_repository,
        variant_repository,
        treatment_repository,
        baseline,
        variant_a,
        variant_b,
        placement_a,
        placement_b,
    )


def test_named_no_treatment_a_b_comparison_is_exact_and_deterministic(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        _variant_repository,
        _treatment_repository,
        baseline,
        variant_a,
        variant_b,
        placement_a,
        placement_b,
    ) = _fixture(tmp_path)

    no_treatment = build_treatment_design_candidate(
        baseline=baseline,
        label='No treatment',
        role='no_treatment',
    )
    treatment_a = build_treatment_design_candidate(
        baseline=baseline,
        label='Treatment A',
        role='treatment',
        system_variant=variant_a,
        placements=(placement_a,),
    )
    treatment_b = build_treatment_design_candidate(
        baseline=baseline,
        label='Treatment B',
        role='treatment',
        system_variant=variant_b,
        placements=(placement_b,),
    )

    first = build_treatment_design_comparison(
        name='No treatment vs A vs B',
        baseline=baseline,
        candidates=(no_treatment, treatment_a, treatment_b),
    )
    repeated = build_treatment_design_comparison(
        name='No treatment vs A vs B',
        baseline=baseline,
        candidates=(no_treatment, treatment_a, treatment_b),
    )

    assert repeated == first
    assert no_treatment.placements == ()
    assert treatment_a.placements[0].placement_sha256 == placement_a.placement_sha256
    assert treatment_b.placements[0].placement_sha256 == placement_b.placement_sha256
    assert treatment_a.system_variant_sha256 == variant_a.variant_sha256
    assert treatment_b.system_variant_sha256 == variant_b.variant_sha256


def test_treatment_comparison_save_reopen_reresolves_variants_and_placements(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        variant_repository,
        treatment_repository,
        baseline,
        variant_a,
        variant_b,
        placement_a,
        placement_b,
    ) = _fixture(tmp_path)

    spec = build_treatment_design_comparison(
        name='Persisted treatment comparison',
        baseline=baseline,
        candidates=(
            build_treatment_design_candidate(
                baseline=baseline,
                label='No treatment',
                role='no_treatment',
            ),
            build_treatment_design_candidate(
                baseline=baseline,
                label='Treatment A',
                role='treatment',
                system_variant=variant_a,
                placements=(placement_a,),
            ),
            build_treatment_design_candidate(
                baseline=baseline,
                label='Treatment B',
                role='treatment',
                system_variant=variant_b,
                placements=(placement_b,),
            ),
        ),
    )
    repository = CadAcousticTreatmentComparisonRepository(
        scene_repository,
        variant_repository=variant_repository,
        treatment_repository=treatment_repository,
    )
    repository.save(spec)

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_treatments = CadAcousticTreatmentRepository(
        reopened_scene,
        reopened_variants,
    )
    reopened = CadAcousticTreatmentComparisonRepository(
        reopened_scene,
        variant_repository=reopened_variants,
        treatment_repository=reopened_treatments,
    ).get(spec.comparison_id)

    assert reopened == spec


def test_treatment_candidate_rejects_wrong_variant_placement(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        _variant_repository,
        _treatment_repository,
        baseline,
        _variant_a,
        variant_b,
        placement_a,
        _placement_b,
    ) = _fixture(tmp_path)

    with pytest.raises(ValueError, match='placement SystemVariant mismatch'):
        build_treatment_design_candidate(
            baseline=baseline,
            label='Mismatched treatment',
            role='treatment',
            system_variant=variant_b,
            placements=(placement_a,),
        )


def test_no_treatment_candidate_cannot_hide_treatment_placement(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        _variant_repository,
        _treatment_repository,
        baseline,
        variant_a,
        _variant_b,
        placement_a,
        _placement_b,
    ) = _fixture(tmp_path)

    with pytest.raises(ValueError, match='no-treatment candidate'):
        build_treatment_design_candidate(
            baseline=baseline,
            label='Invalid no treatment',
            role='no_treatment',
            system_variant=variant_a,
            placements=(placement_a,),
        )
