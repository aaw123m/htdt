from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import json
import pytest

from htdt.acoustic_benchmark import AcousticMaterial, SpecificImpedancePoint
from htdt.cad_acoustic_snapshot import (
    build_acoustic_prediction_request,
    build_acoustic_scene_snapshot,
)
from htdt.cad_acoustic_solver_adapter import (
    bind_prediction_request_to_solver_adapter,
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
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_candidate_wave_execution import (
    CandidateWaveExecutionError,
)
from htdt.cad_scene import Position3
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
)
from htdt.treatment_boundary_overlay import (
    TreatmentBoundaryCompileInput,
    compile_treatment_boundary_overlays,
)
from htdt.treatment_boundary_overlay_repository import (
    TreatmentBoundaryOverlayRepository,
)
from htdt.acoustic_pffdtd_impedance_adapter import (
    PFFDTD_IMPEDANCE_MAPPING_ID,
    PFFDTD_IMPEDANCE_MAPPING_VERSION,
    pffdtd_impedance_mapping_authority_payload,
)
from scripts.run_r130a_candidate_wave_execution import _fixture as r130_fixture


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _wave_treatment_definition(*, impedance: float | None):
    """An attached treatment with an exact, solver-ready acoustic model."""
    if impedance is None:
        material = AcousticMaterial(
            material_id='treatment-material-rigid',
            provenance='fixture treatment acoustic material',
            version='1',
            wave_model='rigid',
            geometric_model='unsupported',
        )
    else:
        material = AcousticMaterial(
            material_id='treatment-material-impedance',
            provenance='fixture measured impedance treatment material',
            version='1',
            wave_model='specific_impedance_table',
            specific_impedance=tuple(
                SpecificImpedancePoint(
                    frequency_hz=frequency,
                    resistance_pa_s_m=impedance,
                    reactance_pa_s_m=0.0,
                )
                for frequency in (40.0, 80.0)
            ),
            geometric_model='unsupported',
        )
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
    uncertainty = TreatmentUncertainty(
        kind='quantified',
        value=0.05,
        unit='fixture',
        note='fixture uncertainty',
    )
    band = TreatmentFrequencyBand(min_hz=40.0, max_hz=80.0)
    evidence = build_treatment_evidence_authority(
        source_kind='measurement',
        source_id=f'treatment-wave-{material.material_id}',
        source_version='1',
        source_sha256='9' * 64,
        reference='treatment wave fixture',
        extraction_id='fixture-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id=f'treatment-{material.material_id}',
            definition_version='1.0',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=layers,
            acoustic_model=TreatmentAcousticModelSubject(
                model_id=f'model-{material.material_id}',
                model_version='1',
                evidence_basis='measured',
                valid_frequency_band=band,
                uncertainty=uncertainty,
                material=material,
            ),
        ),
    )
    model = TreatmentAcousticModel(
        model_id=f'model-{material.material_id}',
        model_version='1',
        evidence_basis='measured',
        valid_frequency_band=band,
        uncertainty=uncertainty,
        provenance=evidence.as_provenance(),
        material=material,
    )
    definition = build_acoustic_treatment_definition(
        definition_id=f'treatment-{material.material_id}',
        version='1.0',
        name=f'{material.wave_model} treatment',
        treatment_type='porous_absorber',
        provenance=evidence.as_provenance(),
        dimensions=dimensions,
        air_gap_m=0.05,
        layers=layers,
        acoustic_model=model,
    )
    return definition, evidence


def _treated_fixture(tmp_path: Path, *, impedance: float | None):
    fixture = r130_fixture(
        tmp_path / 'r130-treated',
        tmp_path / 'unused-pffdtd-upstream',
        boundary_mode='impedance',
    )
    scene_repository = fixture['scene_repository']
    store = fixture['store']

    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        CadSystemVariantRepository(scene_repository),
    )
    overlay_repository = TreatmentBoundaryOverlayRepository(
        scene_repository,
        treatment_repository,
        fixture['r120_repository'],
    )

    definition, evidence = _wave_treatment_definition(impedance=impedance)
    treatment_repository.save_evidence(evidence)
    definition = treatment_repository.save_definition(definition)

    # Attach the treatment to the fixture's rigid companion surface: the one
    # whose base boundary physics resolves to a rigid_zero_normal_velocity
    # payload.
    rigid_item = None
    for surface in fixture['snapshot'].surface_boundary_configuration:
        payload = store.read_payload(surface.boundary_physics_authority)
        if (
            isinstance(payload, dict)
            and payload.get('model') == 'rigid_zero_normal_velocity'
        ):
            rigid_item = surface
            break
    assert rigid_item is not None
    base_binding = SurfaceBoundaryAuthorityBinding(
        source_surface_id=rigid_item.source_surface_id,
        material_authority=rigid_item.material_authority,
        boundary_physics_authority=rigid_item.boundary_physics_authority,
    )

    placement = build_treatment_placement(
        definition=definition,
        revision=fixture['revision'],
        instance_id='treatment-instance-1',
        position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        coverage=TreatmentCoverage(
            width_m=1.0,
            height_m=1.0,
            host_surface_fraction=1.0,
        ),
        host_surface_id=rigid_item.source_surface_id,
    )
    treatment_repository.save_placement(placement)
    evaluation = treatment_repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fixture['revision'].revision_id,
    )
    compile_input = TreatmentBoundaryCompileInput(
        definition=definition,
        placement=placement,
        surface_binding_evaluation=evaluation,
    )
    result = compile_treatment_boundary_overlays(
        fixture['revision'],
        fixture['compiled'],
        (compile_input,),
        target_domain='wave',
        base_surface_bindings=(base_binding,),
    )[0]

    if result.overlay is not None:
        overlay_repository.save_overlay(result.overlay)
        store.put_exact_json(
            result.overlay.as_external_authority_ref(),
            result.overlay.model_dump(
                mode='json',
                exclude={'overlay_id', 'overlay_hash_sha256'},
            ),
        )
    if result.composition_request is not None:
        composition = result.composition_request
        overlay_repository.save_composition(composition)
        store.put_exact_json(
            composition.as_external_authority_ref(),
            composition.model_dump(
                mode='json',
                exclude={'composition_id', 'composition_hash_sha256'},
            ),
        )
        model = definition.acoustic_model
        assert model is not None
        store.put_exact_json(
            ExactExternalAuthorityRef(
                authority_id=f'treatment-acoustic-model:{model.model_id}',
                authority_version=model.model_version,
                semantic_hash_sha256=_digest(
                    model.model_dump(mode='json')
                ),
            ),
            model.model_dump(mode='json'),
        )
    store.put_exact_json(
        ExactExternalAuthorityRef(
            authority_id=(
                f'{PFFDTD_IMPEDANCE_MAPPING_ID}:'
                f'{_digest(pffdtd_impedance_mapping_authority_payload())}'
            ),
            authority_version=PFFDTD_IMPEDANCE_MAPPING_VERSION,
            semantic_hash_sha256=_digest(
                pffdtd_impedance_mapping_authority_payload()
            ),
        ),
        pffdtd_impedance_mapping_authority_payload(),
    )

    snapshot_repository = fixture['snapshot_repository']
    snapshot_repository.treatment_boundary_repository = overlay_repository

    original = fixture['snapshot']
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fixture['revision'],
        compiled_geometry=fixture['compiled'],
        source_models=(fixture['source'],),
        receivers=original.receivers,
        requested_frequency_domain=original.requested_frequency_domain,
        requested_observables=original.requested_observables,
        system_variant=fixture['variant'],
        environment=fixture['environment'],
        valid_frequency_domain=original.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            original.valid_frequency_domain_authority_ref
        ),
        treatment_boundary_results=(result,),
        wave_source_excitation_bindings=original.wave_source_excitation_bindings,
    )
    snapshot_repository.save_snapshot(snapshot)
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=fixture['request'].model_solver_role_id,
        requested_frequency_domain=original.requested_frequency_domain,
        requested_observables=original.requested_observables,
        numerical_fidelity_policy_ref=fixture['fidelity_ref'],
    )
    snapshot_repository.save_prediction_request(request)
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=fixture['descriptor'],
        solver_configuration_ref=(
            fixture['configuration'].as_external_ref()
        ),
        numerical_fidelity_policy=fixture['fidelity_policy'],
    )
    fixture['dispatch_repository'].save_dispatch(dispatch)
    fixture['treated_snapshot'] = snapshot
    fixture['treated_dispatch'] = dispatch
    fixture['treatment_result'] = result
    fixture['overlay_repository'] = overlay_repository
    fixture['treatment_repository'] = treatment_repository
    fixture['rigid_surface_id'] = rigid_item.source_surface_id
    return fixture


def test_wave_treatment_composition_compiles_to_impedance_boundary(
    tmp_path: Path,
) -> None:
    fixture = _treated_fixture(
        tmp_path,
        impedance=3.0 * 1.2 * 343.0,
    )
    authority, _model = fixture['executor'].compile_input(
        dispatch_binding_id=fixture['treated_dispatch'].binding_id,
        configuration=fixture['configuration'],
    )
    treated = [
        item
        for item in authority.boundary_bindings
        if item.treatment_composition is not None
    ]
    assert len(treated) == 1
    binding = treated[0]
    assert binding.source_surface_id == fixture['rigid_surface_id']
    composition = fixture['treatment_result'].composition_request
    assert binding.boundary_physics_authority == (
        composition.as_external_authority_ref()
    )
    # The base material authority is preserved and recorded — never silently
    # replaced by the treatment authority.
    assert binding.material_authority == (
        fixture['treatment_result'].r120_surface_binding.material_authority
    )
    assert binding.treatment_composition.base_material_authority == (
        binding.material_authority
    )
    assert binding.impedance_mapping is not None
    assert binding.impedance_mapping.normalized_impedance == pytest.approx(3.0)
    assert (
        binding.impedance_mapping.physical_resistance_pa_s_m
        == pytest.approx(3.0 * 1.2 * 343.0)
    )
    assert binding.impedance_mapping.boundary_provenance['kind'] == (
        'treatment_boundary_composition'
    )


def test_wave_treatment_unsupported_model_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = r130_fixture(
        tmp_path / 'r130-blocked',
        tmp_path / 'unused-pffdtd-upstream-b',
        boundary_mode='impedance',
    )
    scene_repository = fixture['scene_repository']
    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        CadSystemVariantRepository(scene_repository),
    )
    overlay_repository = TreatmentBoundaryOverlayRepository(
        scene_repository,
        treatment_repository,
        fixture['r120_repository'],
    )
    # No acoustic model authority -> the wave composition stays blocked.
    dimensions = TreatmentDimensions(width_m=1.0, height_m=1.0, thickness_m=0.1)
    layers = (
        TreatmentLayer(
            layer_id='core',
            material_name='fixture core',
            thickness_m=0.1,
        ),
    )
    evidence = build_treatment_evidence_authority(
        source_kind='user_defined',
        source_id='treatment-no-model',
        source_version='1',
        reference='treatment wave fixture without acoustic model',
        extraction_id='fixture-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='treatment-no-model',
            definition_version='1.0',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=layers,
            acoustic_model=None,
        ),
    )
    treatment_repository.save_evidence(evidence)
    definition = treatment_repository.save_definition(
        build_acoustic_treatment_definition(
            definition_id='treatment-no-model',
            version='1.0',
            name='unmodelled treatment',
            treatment_type='porous_absorber',
            provenance=evidence.as_provenance(),
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=layers,
            acoustic_model=None,
        )
    )
    rigid_item = None
    for surface in fixture['snapshot'].surface_boundary_configuration:
        payload = fixture['store'].read_payload(
            surface.boundary_physics_authority
        )
        if (
            isinstance(payload, dict)
            and payload.get('model') == 'rigid_zero_normal_velocity'
        ):
            rigid_item = surface
            break
    assert rigid_item is not None
    base_binding = SurfaceBoundaryAuthorityBinding(
        source_surface_id=rigid_item.source_surface_id,
        material_authority=rigid_item.material_authority,
        boundary_physics_authority=rigid_item.boundary_physics_authority,
    )
    placement = build_treatment_placement(
        definition=definition,
        revision=fixture['revision'],
        instance_id='treatment-instance-1',
        position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        coverage=TreatmentCoverage(
            width_m=1.0,
            height_m=1.0,
            host_surface_fraction=1.0,
        ),
        host_surface_id=rigid_item.source_surface_id,
    )
    treatment_repository.save_placement(placement)
    evaluation = treatment_repository.evaluate_placement_surface_binding(
        placement,
        scene_revision_id=fixture['revision'].revision_id,
    )
    result = compile_treatment_boundary_overlays(
        fixture['revision'],
        fixture['compiled'],
        (
            TreatmentBoundaryCompileInput(
                definition=definition,
                placement=placement,
                surface_binding_evaluation=evaluation,
            ),
        ),
        target_domain='wave',
        base_surface_bindings=(base_binding,),
    )[0]
    assert result.status == 'BLOCKED_NO_ACOUSTIC_MODEL'
    overlay_repository.save_overlay(result.overlay)

    snapshot_repository = fixture['snapshot_repository']
    snapshot_repository.treatment_boundary_repository = overlay_repository
    original = fixture['snapshot']
    snapshot = build_acoustic_scene_snapshot(
        scene_revision=fixture['revision'],
        compiled_geometry=fixture['compiled'],
        source_models=(fixture['source'],),
        receivers=original.receivers,
        requested_frequency_domain=original.requested_frequency_domain,
        requested_observables=original.requested_observables,
        system_variant=fixture['variant'],
        environment=fixture['environment'],
        valid_frequency_domain=original.valid_frequency_domain,
        valid_frequency_domain_authority_ref=(
            original.valid_frequency_domain_authority_ref
        ),
        treatment_boundary_results=(result,),
        wave_source_excitation_bindings=original.wave_source_excitation_bindings,
    )
    snapshot_repository.save_snapshot(snapshot)
    assert not snapshot.readiness.wave_boundary_ready
    request = build_acoustic_prediction_request(
        snapshot=snapshot,
        model_solver_role_id=fixture['request'].model_solver_role_id,
        requested_frequency_domain=original.requested_frequency_domain,
        requested_observables=original.requested_observables,
        numerical_fidelity_policy_ref=fixture['fidelity_ref'],
    )
    snapshot_repository.save_prediction_request(request)
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=snapshot,
        request=request,
        adapter=fixture['descriptor'],
        solver_configuration_ref=(
            fixture['configuration'].as_external_ref()
        ),
        numerical_fidelity_policy=fixture['fidelity_policy'],
    )
    fixture['dispatch_repository'].save_dispatch(dispatch)
    assert dispatch.state != 'READY'
    with pytest.raises(
        CandidateWaveExecutionError,
        match='READY solver dispatch',
    ):
        fixture['executor'].compile_input(
            dispatch_binding_id=dispatch.binding_id,
            configuration=fixture['configuration'],
        )
