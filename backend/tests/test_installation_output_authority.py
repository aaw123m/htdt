"""Issue #420 — installation output must be generated only from
replay-validated stored project evidence.

``InstallationReportService`` is the production boundary: section authorities
are resolved by exact id through each repository's canonical read/replay
validator, typed evidence refs must resolve against the exact
Scene/SystemVariant target, and a persisted ``InstallationOutput`` must
rebuild semantically identical from its recorded authority.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.acoustic_benchmark import AcousticMaterial, GeometricAcousticBand
from htdt.cad_acoustic_treatment import (
    TreatmentAcousticModel,
    TreatmentAcousticModelSubject,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentFrequencyBand,
    TreatmentUncertainty,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
    CadVerificationMeasurementPoint,
    build_biquad_filter,
    build_calibration_lifecycle_event,
    build_calibration_plan,
    build_generic_biquad_export,
    build_verification_measurement_plan,
)
from htdt.cad_calibration_repository import CadCalibrationRepository
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    measurement_sha256,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.cad_standards_evidence import build_standards_observation_authority
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposalEvidenceRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_video_geometry import (
    PROJECTOR_SPEC_EVIDENCED_FIELDS,
    AngleRange,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecification,
    ProjectorSpecificationEvidence,
    ProjectorSpecificationProvenance,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    build_projector_spec_document_evidence,
    build_projector_spec_field_assertions,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
    projector_spec_optical_values,
)
from htdt.cad_video_geometry_repository import CadVideoGeometryRepository
from htdt.installation_output_authority import (
    InstallationAuthorityError,
    InstallationReportService,
    InstallationTreatmentInstanceRef,
)
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.report import (
    InstallationEvidenceRef,
    InstallationOutput,
    build_installation_output,
    render_installation_csv,
    render_installation_report_html,
)
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


NOW = '2026-09-19T12:00:00+00:00'


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _scene():
    base = make_f1_scene()
    additions = (
        SceneEntity(
            entity_id='projector-main',
            kind='projector',
            name='Projector',
            position=Position3(x_m=3.0, y_m=4.0, z_m=1.8),
            size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
        ),
        SceneEntity(
            entity_id='screen-main',
            kind='screen',
            name='Screen',
            position=Position3(x_m=3.0, y_m=0.15, z_m=1.4),
            size_m=Size3(x_m=2.4, y_m=0.1, z_m=1.4),
        ),
        SceneEntity(
            entity_id='seat-front',
            kind='seat',
            name='Front seat',
            position=Position3(x_m=3.0, y_m=2.0, z_m=0.5),
            size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        ),
        SceneEntity(
            entity_id='seat-rear',
            kind='seat',
            name='Rear seat',
            position=Position3(x_m=3.0, y_m=3.2, z_m=0.5),
            size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        ),
    )
    return base.model_copy(update={'entities': base.entities + additions})


def _semantic_scene(document):
    """Attach R120 semantic geometry so treatment host bindings are exact."""

    raw = b"""\
v 0 0 0
v 1 0 0
v 0 1 0
v 0 0 1
f 1 3 2
f 1 2 4
f 1 4 3
f 2 3 4
"""
    mesh = import_raw_visual_mesh(raw, source_name='issue-420-treatment.obj')
    triangle_ids = raw_triangle_ids(mesh)
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='issue #420 fixture uses HTDT scene metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='front-treatment-host',
                triangle_ids=(triangle_ids[0], triangle_ids[1]),
                semantic_class='room_boundary',
            ),
            SurfaceSemanticAssignment(
                surface_key='object-treatment-host',
                triangle_ids=(triangle_ids[2],),
                semantic_class='object_surface',
            ),
        ),
    )
    geometry = convert_raw_visual_mesh_to_semantic_geometry(mesh, request)
    payload = document.model_dump(mode='json')
    payload['schema_version'] = max(4, int(payload.get('schema_version', 1)))
    payload['r120_semantic_geometry'] = geometry.model_dump(mode='json')
    return SceneDocument.model_validate(payload), geometry


def _projector_spec_optical_values() -> dict:
    return projector_spec_optical_values(
        lens_reference_offset_m=Offset3(),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=3.0,
        optical_zoom_ratio=2.0,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _projector_spec_source_bytes() -> bytes:
    return json.dumps(
        {
            'schema': 'example.projector-spec-sheet.v1',
            'manufacturer': 'Example',
            'model': 'P1',
            'document_version': '1.0',
            'optical': _projector_spec_optical_values(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _projector_spec_evidence() -> ProjectorSpecificationEvidence:
    return build_projector_spec_document_evidence(
        evidence_kind='manufacturer_document',
        manufacturer='Example',
        model='P1',
        publisher='Example',
        document_title='P1 installation manual',
        document_version='1.0',
        reference='projection specifications',
        source_uri='https://example.invalid/p1',
        source_sha256=sha256(_projector_spec_source_bytes()).hexdigest(),
        extractor_id='htdt-test-spec-table',
        extractor_version='1.0',
        field_assertions=build_projector_spec_field_assertions(
            optical_values=_projector_spec_optical_values(),
            field_locators={
                field: 'installation manual p.2, optical table'
                for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
            },
        ),
    )


def _save_projector_specification(
    repository: CadVideoGeometryRepository,
    specification: ProjectorSpecification,
) -> ProjectorSpecification:
    """Persist the fixture spec together with its exact retained evidence."""
    return repository.save_projector_specification(
        specification,
        evidence=_projector_spec_evidence(),
        source_bytes=_projector_spec_source_bytes(),
        source_filename='example-p1-installation-manual.json',
        media_type='application/json',
    )


def _projector_spec(specification_id: str = 'projector-spec-main'):
    evidence = _projector_spec_evidence()
    return build_projector_specification(
        specification_id=specification_id,
        version='1',
        manufacturer='Example',
        model='P1',
        provenance=ProjectorSpecificationProvenance(
            source_kind='manufacturer',
            publisher='Example',
            document_title='P1 installation manual',
            document_version='1.0',
            reference='projection specifications',
            source_uri='https://example.invalid/p1',
            source_sha256=evidence.source_sha256,
            evidence=evidence.ref(),
        ),
        lens_reference_offset_m=Offset3(),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.0,
        throw_ratio_max=3.0,
        optical_zoom_ratio=2.0,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-1.0,
            maximum_fraction=1.0,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )


def _video_request(specification):
    return build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=specification,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=2.0,
            visible_height_m=1.125,
            frame_clearance_m=0.02,
            acoustically_transparent=True,
        ),
        seats=(
            SeatGeometryBinding(
                entity_id='seat-front',
                row_id='front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.18,
            ),
            SeatGeometryBinding(
                entity_id='seat-rear',
                row_id='rear',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.18,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(
                minimum_deg=1.0,
                maximum_deg=179.0,
            ),
            vertical_viewing_angle_deg=AngleRange(
                minimum_deg=1.0,
                maximum_deg=179.0,
            ),
            center_elevation_angle_deg=AngleRange(
                minimum_deg=-89.0,
                maximum_deg=89.0,
            ),
            sightline_samples=(
                SightlineSample(
                    sample_id='center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.5,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=('projector-main', 'screen-main'),
    )


def _standards(revision, *, variant=None):
    source = CriterionSource(
        publisher='HTDT fixture standards body',
        document_title='Video geometry criteria',
        document_version='2026.1',
        reference='clauses 1-3',
        source_uri='https://example.invalid/standards',
    )
    profile = build_user_standards_profile(
        profile_id='video-installation-profile',
        version='1',
        name='Video installation fixture profile',
        criteria=(
            CriterionDefinition(
                criterion_id='pass-angle',
                name='Passing angle',
                source=source,
                quantity='angle',
                unit='deg',
                applicable_domains=('video',),
                evidence_requirement='none',
                rule=CriterionRule(operator='max', maximum=20.0),
            ),
            CriterionDefinition(
                criterion_id='fail-angle',
                name='Failing angle',
                source=source,
                quantity='angle',
                unit='deg',
                applicable_domains=('video',),
                evidence_requirement='none',
                rule=CriterionRule(operator='max', maximum=5.0),
            ),
        ),
    )
    target = StandardsEvaluationTarget(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        entity_ids=('projector-main', 'screen-main'),
        applicable_domains=('video',),
    )
    pass_evidence = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        quantity='angle',
        unit='deg',
        observed_value=10.0,
        evidence_basis='measured',
        entity_ids=('projector-main',),
        note='pass-angle fixture observation',
        observed_at_utc=NOW,
    )
    fail_evidence = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        quantity='angle',
        unit='deg',
        observed_value=10.0,
        evidence_basis='measured',
        entity_ids=('projector-main',),
        note='fail-angle fixture observation',
        observed_at_utc=NOW,
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(
            CriterionObservation(
                criterion_id='pass-angle',
                entity_ids=('projector-main',),
                observed_value=10.0,
                unit='deg',
                evidence_basis='measured',
                evidence_refs=(pass_evidence.ref(),),
            ),
            CriterionObservation(
                criterion_id='fail-angle',
                entity_ids=('projector-main',),
                observed_value=10.0,
                unit='deg',
                evidence_basis='measured',
                evidence_refs=(fail_evidence.ref(),),
            ),
        ),
        created_at_utc=NOW,
    )
    return profile, evaluation, (pass_evidence, fail_evidence)


def _treatment_definition():
    definition_id = 'panel-600x1200'
    version = '1'
    dimensions = TreatmentDimensions(
        width_m=0.6,
        height_m=1.2,
        thickness_m=0.1,
    )
    material = AcousticMaterial(
        material_id='panel-material',
        provenance='fixture measured material',
        version='1',
        wave_model='rigid',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=1000.0,
                absorption=0.8,
                scattering=0.1,
            ),
        ),
    )
    band = TreatmentFrequencyBand(min_hz=100.0, max_hz=10000.0)
    uncertainty = TreatmentUncertainty(
        kind='quantified',
        value=0.05,
        unit='absorption_coefficient',
        note='fixture uncertainty',
    )
    model_subject = TreatmentAcousticModelSubject(
        model_id='panel-model',
        model_version='1',
        evidence_basis='measured',
        valid_frequency_band=band,
        uncertainty=uncertainty,
        material=material,
    )
    definition_evidence = build_treatment_evidence_authority(
        source_kind='manufacturer',
        source_id='panel-datasheet',
        source_version='2026.1',
        source_sha256='b' * 64,
        reference='fixture product definition',
        extraction_id='fixture-datasheet-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id=definition_id,
            definition_version=version,
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=(),
        ),
    )
    model_evidence = build_treatment_evidence_authority(
        source_kind='measurement',
        source_id='panel-measurement',
        source_version='1',
        source_sha256='a' * 64,
        reference='fixture acoustic model',
        extraction_id='fixture-measurement-extraction',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id=definition_id,
            definition_version=version,
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.05,
            layers=(),
            acoustic_model=model_subject,
        ),
    )
    definition = build_acoustic_treatment_definition(
        definition_id=definition_id,
        version=version,
        name='600 x 1200 panel',
        treatment_type='porous_absorber',
        provenance=definition_evidence.as_provenance(),
        dimensions=dimensions,
        air_gap_m=0.05,
        layers=(),
        acoustic_model=TreatmentAcousticModel(
            model_id='panel-model',
            model_version='1',
            evidence_basis='measured',
            valid_frequency_band=band,
            uncertainty=uncertainty,
            provenance=model_evidence.as_provenance(),
            material=material,
        ),
    )
    return definition, (definition_evidence, model_evidence)


def _save_treatment_authorities(repository, revision, variant, geometry):
    definition, evidences = _treatment_definition()
    for evidence in evidences:
        repository.save_evidence(evidence)
    repository.save_definition(definition)
    surface = next(
        item for item in geometry.surfaces
        if item.surface_key == 'front-treatment-host'
    )
    proposed = build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id='panel-a',
        position=Position3(x_m=0.3, y_m=0.05, z_m=1.0),
        coverage=TreatmentCoverage(
            width_m=0.6,
            height_m=1.2,
            host_surface_fraction=0.1,
        ),
        system_variant=variant,
        host_surface_id=surface.surface_id,
    )
    repository.save_placement(proposed)
    install_seed = build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id='panel-c',
        position=Position3(x_m=1.7, y_m=0.05, z_m=1.0),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant,
        host_surface_id=surface.surface_id,
    )
    repository.save_placement(install_seed)
    installed = revise_treatment_placement(
        install_seed,
        revision=revision,
        lifecycle='installed',
        system_variant=variant,
    )
    repository.save_placement(installed)
    return definition, (proposed, installed)


def _measurement_authority(measurement_repository, quality_repository, revision):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0),
        phase_status='valid',
        level_reference='spl',
        processing={'fixture_raw': 'issue-420-calibration-measurement'},
    )
    measurement = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id='measurement-before',
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T13:01:00+00:00',
        source_kind='unknown',
        external_source_id='rew-issue-420',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-before',
        measurement_id=measurement.measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0),
        phase_status='valid',
        level_reference='spl',
        processing_json=canonical_json({'fixture_raw': 'issue-420-calibration-measurement'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        measurement,
        dataset,
        raw_filename='measurement-before.json',
        raw_bytes=raw,
    )
    evidence = CadMeasurementQualityEvidence(
        usable_frequency_band_hz=(20.0, 20000.0),
        timing_reference_valid=True,
        timing_reference_id='loopback-issue-420',
        clock_source='shared-clock-issue-420',
        sample_rate_hz=48000,
        delay_correction_s=0.0,
        polarity_correct=True,
        polarity_confidence=0.99,
        evidence_source='manual',
    )
    profile = build_measurement_quality_profile(
        profile_version='issue-420-quality-1',
        minimum_polarity_confidence=0.9,
    )
    context = build_acquisition_context(
        acquisition_context_id='issue-420-acquisition',
        source_kind='manual',
        subject_measurement_ids=(measurement.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id='loopback-issue-420',
        clock_source='shared-clock-issue-420',
        sample_rate_hz=48000,
        delay_correction_s=0.0,
        created_at_utc='2026-09-19T13:01:30+00:00',
    )
    quality_repository.save_acquisition_context(context)
    observation = build_measurement_observation(
        observation_id='issue-420-observation',
        measurement_id=measurement.measurement_id,
        source_kind='manual',
        usable_frequency_band_hz=(20.0, 20000.0),
        polarity_correct=True,
        polarity_confidence=0.99,
        observed_at_utc='2026-09-19T13:01:45+00:00',
    )
    quality_repository.save_observation(observation)
    quality = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=acquisition_context_binding(context),
        observation=observation_binding(observation),
        report_id='quality-issue-420',
        created_at_utc='2026-09-19T13:02:00+00:00',
    )
    quality_repository.save_report(quality)
    return measurement, dataset, quality


def _save_calibration_authorities(
    calibration_repository,
    measurement_repository,
    quality_repository,
    revision,
    variant,
):
    measurement, dataset, quality = _measurement_authority(
        measurement_repository,
        quality_repository,
        revision,
    )
    peq = build_biquad_filter(
        filter_id='peq-1',
        filter_type='peaking',
        frequency_hz=80.3,
        q=1.03,
        gain_db=2.2,
        sample_rate_hz=48000,
    )
    channel = CadCalibrationChannel(
        channel_id='FL',
        role_id='FL',
        source_entity_id='speaker-fl',
        physical_output_id='out-fl',
        sample_rate_hz=48000,
        gain_db=-1.2,
        delay_s=0.0012,
        polarity='normal',
        crossovers=(
            CadCrossoverSetting(
                crossover_type='high_pass',
                frequency_hz=80.0,
                filter_order=2,
            ),
        ),
        peq=(peq,),
        routing=('main',),
    )
    constraints = CadDeviceCapabilityConstraints(
        capability_id='generic-device',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        max_filters_per_channel=4,
        max_boost_db=6.0,
        max_cut_db=12.0,
        min_gain_db=-12.0,
        max_gain_db=6.0,
        max_delay_s=0.050,
        supported_crossover_orders=(2, 4),
        allowed_physical_outputs=('out-fl',),
        frequency_resolution_hz=1.0,
        q_resolution=0.1,
        filter_gain_resolution_db=0.5,
        channel_gain_resolution_db=0.5,
        delay_resolution_s=0.001,
    )
    target = CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=20000.0, level_db=-6.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='reference_frequency',
            reference_frequency_hz=1000.0,
        ),
    )
    plan = build_calibration_plan(
        scene_revision=revision,
        system_variant=variant,
        measurement=measurement,
        dataset=dataset,
        quality_report=quality,
        channels=(channel,),
        sample_rate_hz=48000,
        device_constraints=constraints,
        max_boost_db=6.0,
        max_cut_db=12.0,
        target_curve=target,
        plan_id='cal-plan-1',
        plan_version='fixture-1',
        created_at_utc='2026-09-19T13:03:00+00:00',
        source_kind='provided_fixture',
    )
    calibration_repository.save_plan(plan)
    export = build_generic_biquad_export(
        plan,
        export_id='cal-plan-1-export',
        created_at_utc='2026-09-19T13:04:00+00:00',
    )
    calibration_repository.save_export(export)
    verification = build_verification_measurement_plan(
        plan=plan,
        exported_settings=export,
        measurement_points=(
            CadVerificationMeasurementPoint(
                point_id='mlp',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
        routing=('main',),
        reference_level_db_spl=75.0,
        required_measurement_capabilities=('magnitude_response',),
        before_measurement_ids=(measurement.measurement_id,),
        after_measurement_ids=(),
        verification_plan_id='cal-plan-1-verification',
        created_at_utc='2026-09-19T13:05:00+00:00',
    )
    calibration_repository.save_verification_plan(verification)
    proposed_event = build_calibration_lifecycle_event(
        plan=plan,
        state='proposed',
        event_id='cal-plan-1-event-proposed',
        created_at_utc='2026-09-19T13:06:00+00:00',
    )
    exported_event = build_calibration_lifecycle_event(
        plan=plan,
        state='exported',
        exported_settings=export,
        supersedes_event=proposed_event,
        event_id='cal-plan-1-event-exported',
        created_at_utc='2026-09-19T13:07:00+00:00',
    )
    applied_event = build_calibration_lifecycle_event(
        plan=plan,
        state='user_applied',
        exported_settings=export,
        supersedes_event=exported_event,
        event_id='cal-plan-1-event-applied',
        created_at_utc='2026-09-19T13:08:00+00:00',
    )
    for event in (proposed_event, exported_event, applied_event):
        calibration_repository.save_lifecycle_event(event)
    return plan, export, verification, (proposed_event, exported_event, applied_event)


def _authorities(tmp_path: Path, *, with_semantic_geometry: bool = True):
    """Persist a scene plus every repository authority and return the wiring."""

    database = tmp_path / 'issue-420.sqlite3'
    scene_repository = SceneRepository(database)
    document, geometry = _semantic_scene(_scene()) if with_semantic_geometry else (_scene(), None)
    revision = scene_repository.save(document, parent_revision_id=None).revision

    system_variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=revision,
        name='issue-420 installation variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        proposal_evidence=(
            ProposalEvidenceRef(
                evidence_kind='objective',
                evidence_id='objective-1',
                evidence_sha256='a' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(variant)

    video_repository = CadVideoGeometryRepository(
        scene_repository,
        system_variant_repository,
    )
    specification = _projector_spec()
    _save_projector_specification(video_repository, specification)
    video = evaluate_video_geometry(
        baseline=revision,
        variant=None,
        projector_specification=specification,
        request=_video_request(specification),
    )
    video_repository.save_evaluation(video)

    standards_repository = CadStandardsRepository(
        scene_repository,
        system_variant_repository,
    )
    profile, standards, standards_evidence = _standards(revision)
    standards_repository.save_profile(profile)
    for _evidence in standards_evidence:
        standards_repository.save_observation_authority(_evidence)
    standards_repository.save_evaluation(standards)

    treatment_repository = CadAcousticTreatmentRepository(
        scene_repository,
        system_variant_repository,
    )
    treatment = None
    if geometry is not None:
        treatment = _save_treatment_authorities(
            treatment_repository,
            revision,
            variant,
            geometry,
        )

    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    calibration_repository = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=system_variant_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )

    service = InstallationReportService(
        scene_repository=scene_repository,
        system_variant_repository=system_variant_repository,
        video_geometry_repository=video_repository,
        standards_repository=standards_repository,
        treatment_repository=treatment_repository,
        calibration_repository=calibration_repository,
    )
    return {
        'database': database,
        'scene_repository': scene_repository,
        'revision': revision,
        'variant': variant,
        'geometry': geometry,
        'specification': specification,
        'video': video,
        'profile': profile,
        'standards': standards,
        'treatment': treatment,
        'measurement_repository': measurement_repository,
        'quality_repository': quality_repository,
        'calibration_repository': calibration_repository,
        'video_repository': video_repository,
        'standards_repository': standards_repository,
        'treatment_repository': treatment_repository,
        'variant_repository': system_variant_repository,
        'service': service,
    }


def test_full_report_builds_from_replay_validated_authorities(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    variant = ctx['variant']
    service = ctx['service']

    plan, export, verification, _events = _save_calibration_authorities(
        ctx['calibration_repository'],
        ctx['measurement_repository'],
        ctx['quality_repository'],
        revision,
        variant,
    )
    _profile, variant_standards, variant_evidence = _standards(revision, variant=variant)
    for _evidence in variant_evidence:
        ctx['standards_repository'].save_observation_authority(_evidence)
    ctx['standards_repository'].save_evaluation(variant_standards)

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
        video_geometry_evaluation_id=None,
        standards_evaluation_id=variant_standards.evaluation_id,
        calibration_plan_id=plan.plan_id,
        calibration_export_id=export.export_id,
        calibration_verification_plan_id=verification.verification_plan_id,
    )

    assert output.schema_version == 5
    assert output.authority.system_variant_id == variant.variant_id
    assert output.standards is not None and output.standards.status == 'AVAILABLE'
    assert output.treatment is not None and output.treatment.status == 'AVAILABLE'
    assert output.calibration is not None and output.calibration.status == 'AVAILABLE'
    assert output.calibration.lifecycle_state == 'user_applied'
    assert output.calibration.export_id == export.export_id
    assert output.calibration.verification_plan_id == verification.verification_plan_id
    # Projector/standards were evaluated against the baseline (no variant): a
    # variant-scoped report cannot claim them — the baseline report can.
    assert output.projector is not None and output.projector.status == 'UNKNOWN'

    baseline_output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        video_geometry_evaluation_id=ctx['video'].evaluation_id,
        standards_evaluation_id=ctx['standards'].evaluation_id,
        treatment_instances=(),
    )
    assert baseline_output.projector is not None
    assert baseline_output.projector.status == 'AVAILABLE'
    assert baseline_output.projector.specification_sha256 == (
        ctx['specification'].specification_sha256
    )
    assert baseline_output.standards is not None
    assert baseline_output.standards.status == 'AVAILABLE'
    # Baseline report only sees baseline-scoped placements — the variant-bound
    # fixture placements do not apply.
    assert baseline_output.treatment is not None
    assert baseline_output.treatment.status == 'UNKNOWN'

    # The service output equals the pure builder over the same canonical
    # objects — the boundary adds resolution, not new semantics.
    direct = build_installation_output(
        revision,
        standards_profile=ctx['profile'],
        standards_evaluation=ctx['standards'],
        video_geometry_evaluation=ctx['video'],
        projector_specification=ctx['specification'],
        treatment_definitions=(),
        treatment_placements=(),
        treatment_surface_evaluations=(),
    )
    assert baseline_output == direct


def test_variant_scoped_report_replays_and_rejects_foreign_authority(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    variant = ctx['variant']
    service = ctx['service']

    variant_video = evaluate_video_geometry(
        baseline=revision,
        variant=variant,
        projector_specification=ctx['specification'],
        request=_video_request(ctx['specification']),
    )
    ctx['video_repository'].save_evaluation(variant_video)
    _profile, variant_standards, variant_evidence = _standards(revision, variant=variant)
    for _evidence in variant_evidence:
        ctx['standards_repository'].save_observation_authority(_evidence)
    ctx['standards_repository'].save_evaluation(variant_standards)

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
        video_geometry_evaluation_id=variant_video.evaluation_id,
        standards_evaluation_id=variant_standards.evaluation_id,
    )
    assert output.projector is not None and output.projector.status == 'AVAILABLE'
    assert output.standards is not None and output.standards.status == 'AVAILABLE'
    assert output.treatment is not None and output.treatment.status == 'AVAILABLE'
    # Proposal evidence is derived from the resolved variant authority.
    assert (
        'system_variant.proposal_evidence',
        'objective-1',
    ) in {(item.authority, item.evidence_id) for item in output.evidence}
    assert service.verify_installation_output_replay(output) == output

    # A baseline-bound evaluation is not applicable to the variant report.
    with pytest.raises(ValueError, match='SystemVariant'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=variant.variant_id,
            video_geometry_evaluation_id=ctx['video'].evaluation_id,
        )

    # An unknown variant id fails closed.
    with pytest.raises(InstallationAuthorityError, match='SystemVariant does not exist'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id='variant-missing',
        )


def test_noncanonical_video_geometry_evaluation_cannot_enter(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    service = ctx['service']
    video = ctx['video']

    # Unknown ids fail closed.
    with pytest.raises(InstallationAuthorityError, match='does not exist'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            video_geometry_evaluation_id='vge-nonexistent',
        )

    # A self-hash-valid but non-canonical payload (results tampered, identity
    # rehashed) is rejected by the repository replay on read.
    payload = video.model_dump(mode='json')
    payload['geometry_status'] = (
        'PASS' if video.geometry_status != 'PASS' else 'FAIL'
    )
    identity = {
        'schema_version': payload['schema_version'],
        'authority_version': payload['authority_version'],
        'target': payload['target'],
        'request': payload['request'],
        'projector_specification_sha256': payload['projector_specification_sha256'],
        'projection': payload['projection'],
        'viewing': payload['viewing'],
        'sightlines': payload['sightlines'],
        'risers': payload['risers'],
        'collisions': payload['collisions'],
        'geometry_status': payload['geometry_status'],
        'screen_acoustic_effect_status': payload['screen_acoustic_effect_status'],
        'screen_acoustic_effect_reason': payload['screen_acoustic_effect_reason'],
    }
    digest = _digest(identity)
    payload['evaluation_sha256'] = digest
    payload['evaluation_id'] = 'vge-' + digest[:24]
    from htdt.cad_video_geometry import VideoGeometryEvaluation

    forged = VideoGeometryEvaluation.model_validate(payload)
    with sqlite3.connect(ctx['database']) as connection:
        connection.execute(
            'UPDATE cad_video_geometry_evaluations SET payload_json=? WHERE evaluation_id=?',
            (forged.model_dump_json(), video.evaluation_id),
        )
        connection.commit()
    with pytest.raises(ValueError, match='not reproducible'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            video_geometry_evaluation_id=video.evaluation_id,
        )


def test_fabricated_standards_results_cannot_enter(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    service = ctx['service']
    standards = ctx['standards']

    # Fabricate a result row that claims a fabricated evidence ref and a PASS
    # the stored observations do not produce. The evaluation id is input-bound
    # and stays valid; only the semantic hash needs recomputing — exactly the
    # "self-hash-valid but non-canonical" shape the boundary must reject.
    payload = standards.model_dump(mode='json')
    for result in payload['results']:
        if result['criterion_id'] == 'fail-angle':
            result['status'] = 'PASS'
            result['evidence_refs'] = [
                {
                    'kind': 'standards_manual_observation',
                    'evidence_id': 'fabricated-evidence',
                    'evidence_sha256': 'd' * 64,
                    'detail': 'invented after the fact',
                }
            ]
    semantic_payload = {
        'schema_version': payload['schema_version'],
        'authority_version': payload['authority_version'],
        'evaluator_version': payload['evaluator_version'],
        'profile_id': payload['profile_id'],
        'profile_version': payload['profile_version'],
        'profile_semantic_hash': payload['profile_semantic_hash'],
        'target': payload['target'],
        'observations': payload['observations'],
        'reevaluation_of_id': payload['reevaluation_of_id'],
        'evaluation_id': payload['evaluation_id'],
        'results': payload['results'],
    }
    payload['evaluation_sha256'] = _digest(semantic_payload)
    from htdt.cad_standards import StandardsEvaluation

    forged = StandardsEvaluation.model_validate(payload)
    with sqlite3.connect(ctx['database']) as connection:
        connection.execute(
            'UPDATE cad_standards_evaluations SET payload_json=? WHERE evaluation_id=?',
            (forged.model_dump_json(), standards.evaluation_id),
        )
        connection.commit()
    with pytest.raises(
        ValueError,
        match='does not match evaluator authority',
    ):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            standards_evaluation_id=standards.evaluation_id,
        )


def test_treatment_pins_and_stale_authority_fail_closed(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    variant = ctx['variant']
    service = ctx['service']
    _definition, (proposed, installed) = ctx['treatment']

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
        treatment_instances=(
            InstallationTreatmentInstanceRef(
                instance_id='panel-c',
                placement_version=2,
                placement_sha256=installed.placement_sha256,
            ),
        ),
    )
    assert output.treatment is not None and output.treatment.status == 'AVAILABLE'
    assert [item.instance_id for item in output.treatment.instances] == ['panel-c']
    assert output.treatment.instances[0].lifecycle == 'installed'

    # Unknown pins fail closed.
    with pytest.raises(InstallationAuthorityError, match='does not exist'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=variant.variant_id,
            treatment_instances=(
                InstallationTreatmentInstanceRef(
                    instance_id='panel-missing',
                    placement_version=1,
                ),
            ),
        )

    # A wrong semantic hash pin fails closed.
    with pytest.raises(InstallationAuthorityError, match='semantic hash mismatch'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=variant.variant_id,
            treatment_instances=(
                InstallationTreatmentInstanceRef(
                    instance_id='panel-a',
                    placement_version=1,
                    placement_sha256='f' * 64,
                ),
            ),
        )

    # Pins for placements bound to another scene revision are foreign
    # authority, not report input.
    changed = revision.document.model_copy(
        update={
            'entities': tuple(
                item.model_copy(update={'name': 'renamed'})
                if item.entity_id == 'speaker-fr'
                else item
                for item in revision.document.entities
            )
        }
    )
    newer = ctx['scene_repository'].save(
        changed,
        parent_revision_id=revision.revision_id,
    ).revision
    with pytest.raises(InstallationAuthorityError, match='SceneRevision'):
        service.build_installation_output_from_authorities(
            scene_revision_id=newer.revision_id,
            treatment_instances=(
                InstallationTreatmentInstanceRef(
                    instance_id='panel-a',
                    placement_version=1,
                ),
            ),
        )

    # Discovery of the superseded (v1) placement must surface the installed
    # (v2) current version only.
    discovered = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
    )
    assert discovered.treatment is not None
    by_id = {item.instance_id: item for item in discovered.treatment.instances}
    assert by_id['panel-c'].placement_version == 2
    assert by_id['panel-c'].lifecycle == 'installed'
    assert proposed.instance_id in by_id


def test_calibration_authority_replay_and_missing_links_fail_closed(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    variant = ctx['variant']
    service = ctx['service']
    plan, export, verification, _events = _save_calibration_authorities(
        ctx['calibration_repository'],
        ctx['measurement_repository'],
        ctx['quality_repository'],
        revision,
        variant,
    )

    # The chain references the export: omitting the pin the lifecycle requires
    # fails closed instead of degrading.
    with pytest.raises(ValueError, match='export'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=variant.variant_id,
            calibration_plan_id=plan.plan_id,
        )

    export_id, verification_id = service.latest_calibration_authority_ids(plan.plan_id)
    assert (export_id, verification_id) == (export.export_id, verification.verification_plan_id)

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
        calibration_plan_id=plan.plan_id,
        calibration_export_id=export_id,
        calibration_verification_plan_id=verification_id,
    )
    assert output.calibration is not None
    assert output.calibration.status == 'AVAILABLE'
    assert output.calibration.lifecycle_state == 'user_applied'

    # A plan bound to another variant is foreign authority for this report.
    other_variant = build_system_variant(
        baseline=revision,
        name='other variant',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc='2026-09-19T13:20:00+00:00',
    )
    ctx['variant_repository'].save_variant(other_variant)
    with pytest.raises(InstallationAuthorityError, match='SystemVariant'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=other_variant.variant_id,
            calibration_plan_id=plan.plan_id,
            calibration_export_id=export_id,
            calibration_verification_plan_id=verification_id,
        )

    # Tampering with the persisted lifecycle chain fails the validated
    # read — the section cannot silently degrade to AVAILABLE.
    with sqlite3.connect(ctx['database']) as connection:
        connection.execute(
            'DELETE FROM cad_calibration_lifecycle_events WHERE event_id=?',
            ('cal-plan-1-event-exported',),
        )
        connection.commit()
    with pytest.raises(ValueError):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            system_variant_id=variant.variant_id,
            calibration_plan_id=plan.plan_id,
            calibration_export_id=export_id,
            calibration_verification_plan_id=verification_id,
        )


def test_generic_evidence_refs_must_resolve_to_applicable_authority(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    service = ctx['service']
    measurement_repository = ctx['measurement_repository']
    measurement, _dataset, _quality = _measurement_authority(
        measurement_repository,
        ctx['quality_repository'],
        revision,
    )

    # A measurement bound to the exact revision resolves and enters the output.
    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        evidence=(
            InstallationEvidenceRef(
                authority='measurement',
                evidence_id=measurement.measurement_id,
                evidence_sha256=measurement_sha256(measurement),
            ),
            InstallationEvidenceRef(
                authority='scene_revision',
                evidence_id=revision.revision_id,
                evidence_sha256=revision.content_hash,
            ),
            InstallationEvidenceRef(
                authority='video_geometry_evaluation',
                evidence_id=ctx['video'].evaluation_id,
                evidence_sha256=ctx['video'].evaluation_sha256,
            ),
        ),
    )
    keys = {(item.authority, item.evidence_id) for item in output.evidence}
    assert ('measurement', 'measurement-before') in keys
    assert ('scene_revision', revision.revision_id) in keys
    assert ('video_geometry_evaluation', ctx['video'].evaluation_id) in keys

    # An unsupported authority namespace is rejected, never rendered.
    with pytest.raises(InstallationAuthorityError, match='not a resolvable'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            evidence=(
                InstallationEvidenceRef(
                    authority='hand-written-note',
                    evidence_id='trust-me',
                ),
            ),
        )

    # A resolvable namespace with a nonexistent id is rejected.
    with pytest.raises(InstallationAuthorityError, match='does not resolve'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            evidence=(
                InstallationEvidenceRef(
                    authority='measurement',
                    evidence_id='measurement-missing',
                ),
            ),
        )

    # A wrong pinned hash is rejected.
    with pytest.raises(InstallationAuthorityError, match='semantic hash mismatch'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            evidence=(
                InstallationEvidenceRef(
                    authority='measurement',
                    evidence_id=measurement.measurement_id,
                    evidence_sha256='e' * 64,
                ),
            ),
        )

    # Evidence bound to a different scene revision does not apply.
    changed = revision.document.model_copy(
        update={
            'entities': tuple(
                item.model_copy(update={'name': 'renamed again'})
                if item.entity_id == 'speaker-fr'
                else item
                for item in revision.document.entities
            )
        }
    )
    newer = ctx['scene_repository'].save(
        changed,
        parent_revision_id=revision.revision_id,
    ).revision
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=None,
        phase_status='absent',
        level_reference='spl',
        processing={'fixture_raw': 'issue-420-other-revision'},
    )
    other_measurement = measurement_record_for_revision(
        newer,
        'point-mlp',
        measurement_id='measurement-other-revision',
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T13:30:00+00:00',
        source_kind='unknown',
        external_source_id='rew-issue-420-other',
    )
    other_dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-other-revision',
        measurement_id=other_measurement.measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=None,
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json({'fixture_raw': 'issue-420-other-revision'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        other_measurement,
        other_dataset,
        raw_filename='measurement-other.json',
        raw_bytes=raw,
    )
    with pytest.raises(InstallationAuthorityError, match='SceneRevision'):
        service.build_installation_output_from_authorities(
            scene_revision_id=revision.revision_id,
            evidence=(
                InstallationEvidenceRef(
                    authority='measurement',
                    evidence_id='measurement-other-revision',
                ),
            ),
        )


def test_replay_verifier_reproduces_semantics_after_restart(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    variant = ctx['variant']
    service = ctx['service']
    plan, export, verification, _events = _save_calibration_authorities(
        ctx['calibration_repository'],
        ctx['measurement_repository'],
        ctx['quality_repository'],
        revision,
        variant,
    )
    _profile, variant_standards, variant_evidence = _standards(revision, variant=variant)
    for _evidence in variant_evidence:
        ctx['standards_repository'].save_observation_authority(_evidence)
    ctx['standards_repository'].save_evaluation(variant_standards)

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        system_variant_id=variant.variant_id,
        standards_evaluation_id=variant_standards.evaluation_id,
        calibration_plan_id=plan.plan_id,
        calibration_export_id=export.export_id,
        calibration_verification_plan_id=verification.verification_plan_id,
    )

    # Reopen every repository against the same database — restart equivalence.
    database = ctx['database']
    scene_repository = SceneRepository(database)
    variant_repository = CadSystemVariantRepository(scene_repository)
    video_repository = CadVideoGeometryRepository(scene_repository, variant_repository)
    standards_repository = CadStandardsRepository(scene_repository, variant_repository)
    treatment_repository = CadAcousticTreatmentRepository(scene_repository, variant_repository)
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    calibration_repository = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    reopened = InstallationReportService(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        video_geometry_repository=video_repository,
        standards_repository=standards_repository,
        treatment_repository=treatment_repository,
        calibration_repository=calibration_repository,
    )
    rebuilt = reopened.verify_installation_output_replay(output)
    assert rebuilt == output
    assert rebuilt.semantic_sha256 == output.semantic_sha256

    # A serialized/reloaded snapshot verifies identically.
    reloaded = InstallationOutput.model_validate_json(output.model_dump_json())
    assert reopened.verify_installation_output_replay(reloaded) == output

    # Renderers stay pure views over the validated output.
    assert render_installation_csv(rebuilt) == render_installation_csv(output)
    marker = '<script type="application/json" id="htdt-installation-output">'
    assert (
        render_installation_report_html(rebuilt, exported_at_utc='2026-09-20T00:00:00+00:00')
        .split(marker, 1)[1]
        == render_installation_report_html(output, exported_at_utc='2026-09-20T01:00:00+00:00')
        .split(marker, 1)[1]
    )


def test_replay_verifier_fails_closed_on_tampered_authority(tmp_path: Path) -> None:
    ctx = _authorities(tmp_path)
    revision = ctx['revision']
    service = ctx['service']

    output = service.build_installation_output_from_authorities(
        scene_revision_id=revision.revision_id,
        video_geometry_evaluation_id=ctx['video'].evaluation_id,
        standards_evaluation_id=ctx['standards'].evaluation_id,
    )
    assert service.verify_installation_output_replay(output) == output

    # A snapshot with a tampered-but-self-consistent semantic hash is
    # rejected: rebuilding cannot reproduce the recorded identity.
    payload = output.model_dump(mode='json')
    payload['projector']['screen_frame_clearance_m'] = 9.99
    identity = dict(payload)
    identity.pop('semantic_sha256')
    payload['semantic_sha256'] = _digest(identity)
    forged = InstallationOutput.model_validate(payload)
    with pytest.raises(InstallationAuthorityError, match='does not replay'):
        service.verify_installation_output_replay(forged)

    # A snapshot that honestly records no standards authority replays to the
    # same UNKNOWN view — verification checks identity, not completeness.
    payload = output.model_dump(mode='json')
    payload['standards']['status'] = 'UNKNOWN'
    payload['standards']['evaluation_id'] = None
    payload['standards']['evaluation_sha256'] = None
    payload['standards']['criteria'] = ()
    payload['standards']['profile_id'] = None
    payload['standards']['profile_version'] = None
    payload['standards']['profile_semantic_hash'] = None
    payload['sections'] = [
        (
            dict(item, status='UNKNOWN', reason='no exact StandardsProfile/StandardsEvaluation authority is bound')
            if item['section'] == 'standards_profile'
            else item
        )
        for item in payload['sections']
    ]
    identity = dict(payload)
    identity.pop('semantic_sha256')
    payload['semantic_sha256'] = _digest(identity)
    tampered = InstallationOutput.model_validate(payload)
    rebuilt = service.verify_installation_output_replay(tampered)
    assert rebuilt == tampered
    assert rebuilt.standards is not None and rebuilt.standards.status == 'UNKNOWN'

    # Deleting the underlying evaluation makes the original snapshot fail
    # closed instead of preserving the AVAILABLE section.
    with sqlite3.connect(ctx['database']) as connection:
        connection.execute(
            'DELETE FROM cad_standards_evaluations WHERE evaluation_id=?',
            (ctx['standards'].evaluation_id,),
        )
        connection.commit()
    with pytest.raises(InstallationAuthorityError, match='does not exist'):
        service.verify_installation_output_replay(output)
