from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

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
    evaluate_treatment_surface_binding,
    revise_treatment_placement,
)
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
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadAcquisitionContextBinding,
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneDocument, make_f1_scene
from htdt.cad_system_variant import build_system_variant
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.report import (
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


NOW = '2026-09-19T13:00:00+00:00'


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


def _scene_authorities(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'installation-v3.sqlite3')
    root = repository.save(make_f1_scene(), parent_revision_id=None).revision

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
    mesh = import_raw_visual_mesh(raw, source_name='installation-v3-treatment.obj')
    triangle_ids = raw_triangle_ids(mesh)
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=root.revision_id,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='installation report fixture uses HTDT scene metres',
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
    payload = root.document.model_dump(mode='json')
    payload['schema_version'] = max(4, int(payload.get('schema_version', 1)))
    payload['r120_semantic_geometry'] = geometry.model_dump(mode='json')
    revision = repository.save(
        SceneDocument.model_validate(payload),
        parent_revision_id=root.revision_id,
    ).revision
    variant = build_system_variant(
        baseline=revision,
        name='InstallationOutput v3 fixture',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    return repository, revision, variant, geometry


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
    return build_acoustic_treatment_definition(
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


def _surface(geometry):
    return next(
        item for item in geometry.surfaces
        if item.surface_key == 'front-treatment-host'
    )


def _treatment_authorities(revision, variant, geometry):
    definition = _treatment_definition()
    surface = _surface(geometry)

    proposed_a = build_treatment_placement(
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
    proposed_b = build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id='panel-b',
        position=Position3(x_m=1.0, y_m=0.05, z_m=1.0),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant,
        host_surface_id=surface.surface_id,
    )
    install_seed = build_treatment_placement(
        definition=definition,
        revision=revision,
        instance_id='panel-c',
        position=Position3(x_m=1.7, y_m=0.05, z_m=1.0),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
        system_variant=variant,
        host_surface_id=surface.surface_id,
    )
    installed = revise_treatment_placement(
        install_seed,
        revision=revision,
        lifecycle='installed',
        system_variant=variant,
    )
    placements = (proposed_a, proposed_b, installed)
    evaluations = tuple(
        evaluate_treatment_surface_binding(
            placement,
            bound_revision=revision,
            evaluated_revision=revision,
            evaluated_revision_id=revision.revision_id,
        )
        for placement in placements
    )
    return definition, placements, evaluations


def _measurement_authority(revision):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0),
        phase_status='valid',
        level_reference='spl',
        processing={'fixture_raw': 'installation-v3-calibration-measurement'},
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
        external_source_id='rew-installation-v3',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-before',
        measurement_id=measurement.measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0),
        phase_status='valid',
        level_reference='spl',
        processing_json=canonical_json({
            'fixture_raw': 'installation-v3-calibration-measurement'
        }),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    evidence = CadMeasurementQualityEvidence(
        usable_frequency_band_hz=(20.0, 20000.0),
        timing_reference_valid=True,
        timing_reference_id='loopback-installation-v3',
        clock_source='shared-clock-installation-v3',
        sample_rate_hz=48000,
        delay_correction_s=0.0,
        polarity_correct=True,
        polarity_confidence=0.99,
        evidence_source='manual',
    )
    profile = build_measurement_quality_profile(
        profile_version='installation-v3-quality-1',
        minimum_polarity_confidence=0.9,
    )
    quality = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=CadAcquisitionContextBinding(
            acquisition_context_id='installation-v3-acquisition',
            acquisition_context_sha256=sha256(b'installation-v3-acquisition').hexdigest(),
            source_kind='manual',
        ),
        report_id='quality-installation-v3',
        created_at_utc='2026-09-19T13:02:00+00:00',
    )
    return measurement, dataset, quality


def _calibration_authorities(revision, variant, *, plan_id='cal-plan-1', max_boost_db=6.0):
    measurement, dataset, quality = _measurement_authority(revision)
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
        max_boost_db=max_boost_db,
        max_cut_db=12.0,
        target_curve=target,
        plan_id=plan_id,
        plan_version='fixture-1',
        created_at_utc='2026-09-19T13:03:00+00:00',
        source_kind='provided_fixture',
    )
    export = build_generic_biquad_export(
        plan,
        export_id=f'{plan_id}-export',
        created_at_utc='2026-09-19T13:04:00+00:00',
    )
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
        before_measurement_ids=('measurement-before',),
        after_measurement_ids=('measurement-after',),
        verification_plan_id=f'{plan_id}-verification',
        created_at_utc='2026-09-19T13:05:00+00:00',
    )
    proposed_event = build_calibration_lifecycle_event(
        plan=plan,
        state='proposed',
        event_id=f'{plan_id}-event-proposed',
        # Deliberately later than the exported timestamp. Append order, not time, is authority.
        created_at_utc='2026-09-19T15:00:00+00:00',
    )
    exported_event = build_calibration_lifecycle_event(
        plan=plan,
        state='exported',
        exported_settings=export,
        supersedes_event=proposed_event,
        event_id=f'{plan_id}-event-exported',
        created_at_utc='2026-09-19T14:00:00+00:00',
    )
    applied_event = build_calibration_lifecycle_event(
        plan=plan,
        state='user_applied',
        exported_settings=export,
        supersedes_event=exported_event,
        event_id=f'{plan_id}-event-applied',
        created_at_utc='2026-09-19T13:00:00+00:00',
    )
    remeasured_event = build_calibration_lifecycle_event(
        plan=plan,
        state='remeasured',
        exported_settings=export,
        verification_plan=verification,
        measurement_ids=('measurement-after',),
        supersedes_event=applied_event,
        event_id=f'{plan_id}-event-remeasured',
        created_at_utc='2026-09-19T12:00:00+00:00',
    )
    validated_event = build_calibration_lifecycle_event(
        plan=plan,
        state='validated',
        exported_settings=export,
        verification_plan=verification,
        measurement_ids=('measurement-after',),
        supersedes_event=remeasured_event,
        event_id=f'{plan_id}-event-validated',
        created_at_utc='2026-09-19T11:00:00+00:00',
    )
    events = (
        proposed_event,
        exported_event,
        applied_event,
        remeasured_event,
        validated_event,
    )
    return plan, export, verification, events


def test_treatment_proposed_installed_quantity_and_capability_summary(tmp_path: Path) -> None:
    _repository, revision, variant, geometry = _scene_authorities(tmp_path)
    definition, placements, evaluations = _treatment_authorities(
        revision, variant, geometry
    )

    output = build_installation_output(
        revision,
        variant=variant,
        treatment_definitions=(definition,),
        treatment_placements=placements,
        treatment_surface_evaluations=evaluations,
    )

    assert output.schema_version == 4
    assert output.authority_version == 'installation-output-4'
    assert output.treatment is not None
    assert output.treatment.status == 'AVAILABLE'
    by_id = {item.instance_id: item for item in output.treatment.instances}
    assert by_id['panel-a'].lifecycle == 'proposed'
    assert by_id['panel-c'].lifecycle == 'installed'
    assert by_id['panel-c'].placement_version == 2
    assert by_id['panel-a'].host_binding_state == 'exact'
    assert by_id['panel-a'].host_surface_semantic_class == 'room_boundary'
    assert by_id['panel-a'].system_variant_id == variant.variant_id
    assert by_id['panel-a'].physical_width_m == pytest.approx(0.6)
    assert by_id['panel-a'].physical_height_m == pytest.approx(1.2)
    assert by_id['panel-a'].thickness_m == pytest.approx(0.1)
    assert by_id['panel-a'].air_gap_m == pytest.approx(0.05)
    assert by_id['panel-a'].coverage_width_m == pytest.approx(0.6)
    assert by_id['panel-a'].host_surface_fraction == pytest.approx(0.1)
    assert by_id['panel-a'].material_id == 'panel-material'
    assert by_id['panel-a'].wave_material_capability == 'SUPPORTED'
    assert by_id['panel-a'].geometric_material_capability == 'SUPPORTED'
    assert by_id['panel-a'].solver_prediction_readiness == 'UNKNOWN'
    assert by_id['panel-a'].evidence_basis == 'measured'
    assert by_id['panel-a'].uncertainty_kind == 'quantified'

    quantity = {
        (item.definition_id, item.lifecycle): item
        for item in output.treatment.quantities
    }
    assert quantity[('panel-600x1200', 'proposed')].quantity == 2
    assert quantity[('panel-600x1200', 'proposed')].total_face_area_m2 == pytest.approx(1.44)
    assert quantity[('panel-600x1200', 'installed')].quantity == 1
    assert next(
        item for item in output.sections if item.section == 'treatment_plan'
    ).status == 'AVAILABLE'


def test_treatment_stale_surface_and_exact_binding_mismatches_fail_closed(tmp_path: Path) -> None:
    repository, revision, variant, geometry = _scene_authorities(tmp_path)
    definition, placements, evaluations = _treatment_authorities(
        revision, variant, geometry
    )

    changed_speaker = revision.document.entity('speaker-fl').model_copy(
        update={'name': 'Changed after treatment placement'}
    )
    changed_document = revision.document.model_copy(
        update={
            'entities': tuple(
                changed_speaker if item.entity_id == 'speaker-fl' else item
                for item in revision.document.entities
            )
        }
    )
    newer = repository.save(
        changed_document,
        parent_revision_id=revision.revision_id,
    ).revision
    stale = evaluate_treatment_surface_binding(
        placements[0],
        bound_revision=revision,
        evaluated_revision=newer,
        evaluated_revision_id=newer.revision_id,
    )
    assert stale.binding_state in {'stale_scene_revision', 'stale_semantic_geometry'}
    with pytest.raises(ValueError, match='stale or invalid'):
        build_installation_output(
            revision,
            variant=variant,
            treatment_definitions=(definition,),
            treatment_placements=(placements[0],),
            treatment_surface_evaluations=(stale,),
        )

    with pytest.raises(ValueError, match='treatment SceneRevision mismatch'):
        build_installation_output(
            newer,
            treatment_definitions=(definition,),
            treatment_placements=(placements[0],),
            treatment_surface_evaluations=(evaluations[0],),
        )

    other_variant = build_system_variant(
        baseline=revision,
        name='different installation variant',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc='2026-09-19T13:10:00+00:00',
    )
    with pytest.raises(ValueError, match='treatment SystemVariant mismatch'):
        build_installation_output(
            revision,
            variant=other_variant,
            treatment_definitions=(definition,),
            treatment_placements=(placements[0],),
            treatment_surface_evaluations=(evaluations[0],),
        )

    changed_definition = build_acoustic_treatment_definition(
        definition_id=definition.definition_id,
        version=definition.version,
        name='Different immutable definition',
        treatment_type='porous_absorber',
        provenance=definition.provenance,
        dimensions=definition.dimensions,
        air_gap_m=definition.air_gap_m,
        layers=definition.layers,
        parameters=definition.parameters,
        acoustic_model=definition.acoustic_model,
    )
    assert changed_definition.definition_sha256 != definition.definition_sha256
    with pytest.raises(ValueError, match='treatment definition hash mismatch'):
        build_installation_output(
            revision,
            variant=variant,
            treatment_definitions=(changed_definition,),
            treatment_placements=(placements[0],),
            treatment_surface_evaluations=(evaluations[0],),
        )


def test_no_treatment_or_calibration_authority_stays_unknown(tmp_path: Path) -> None:
    _repository, revision, variant, _geometry = _scene_authorities(tmp_path)
    output = build_installation_output(revision, variant=variant)

    assert output.treatment is not None
    assert output.treatment.status == 'UNKNOWN'
    assert output.calibration is not None
    assert output.calibration.status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'treatment_plan'
    ).status == 'UNKNOWN'
    assert next(
        item for item in output.sections if item.section == 'calibration_plan'
    ).status == 'UNKNOWN'


def test_supported_calibration_requested_exported_quantization_and_export_only_lifecycle(
    tmp_path: Path,
) -> None:
    _repository, revision, variant, _geometry = _scene_authorities(tmp_path)
    plan, export, verification, _events = _calibration_authorities(revision, variant)

    output = build_installation_output(
        revision,
        variant=variant,
        calibration_plan=plan,
        calibration_export_snapshot=export,
        verification_measurement_plan=verification,
    )
    assert output.calibration is not None
    summary = output.calibration
    assert summary.status == 'AVAILABLE'
    assert summary.support_state == 'SUPPORTED'
    assert summary.plan_id == plan.plan_id
    assert summary.plan_semantic_sha256 == plan.plan_semantic_sha256
    assert summary.device_capability_id == 'generic-device'
    assert summary.target_curve_semantic_sha256 is not None
    assert summary.export_id == export.export_id
    assert summary.exported_settings_semantic_sha256 == export.exported_settings_semantic_sha256
    assert summary.quantization_applied is True
    assert summary.verification_plan_id == verification.verification_plan_id
    assert summary.verification_semantic_sha256 == verification.verification_semantic_sha256
    assert summary.lifecycle_state == 'exported'
    assert len(summary.requested_channels) == 1
    assert len(summary.exported_channels) == 1
    requested = summary.requested_channels[0]
    exported = summary.exported_channels[0]
    assert requested.settings_source == 'requested'
    assert exported.settings_source == 'exported'
    assert requested.physical_output_id == 'out-fl'
    assert requested.gain_db == pytest.approx(-1.2)
    assert exported.gain_db == pytest.approx(-1.0)
    assert requested.delay_s == pytest.approx(0.0012)
    assert exported.delay_s == pytest.approx(0.001)
    assert requested.peq[0].frequency_hz == pytest.approx(80.3)
    assert exported.peq[0].frequency_hz == pytest.approx(80.0)
    assert next(
        item for item in output.sections if item.section == 'calibration_plan'
    ).status == 'AVAILABLE'


@pytest.mark.parametrize(
    ('event_count', 'expected_state'),
    (
        (3, 'user_applied'),
        (4, 'remeasured'),
        (5, 'validated'),
    ),
)
def test_calibration_lifecycle_uses_explicit_append_order_not_timestamps(
    tmp_path: Path,
    event_count: int,
    expected_state: str,
) -> None:
    _repository, revision, variant, _geometry = _scene_authorities(tmp_path)
    plan, export, verification, events = _calibration_authorities(revision, variant)

    output = build_installation_output(
        revision,
        variant=variant,
        calibration_plan=plan,
        calibration_export_snapshot=export,
        verification_measurement_plan=verification,
        calibration_lifecycle_events=events[:event_count],
    )
    assert output.calibration is not None
    assert output.calibration.lifecycle_state == expected_state
    assert tuple(row[2] for row in output.calibration.lifecycle_events) == tuple(
        event.state for event in events[:event_count]
    )


def test_calibration_scene_variant_export_verification_and_lifecycle_mismatches_reject(
    tmp_path: Path,
) -> None:
    repository, revision, variant, _geometry = _scene_authorities(tmp_path)
    plan, export, verification, events = _calibration_authorities(revision, variant)

    changed = revision.document.entity('speaker-fr').model_copy(
        update={'name': 'new revision after calibration plan'}
    )
    newer = repository.save(
        revision.document.model_copy(
            update={
                'entities': tuple(
                    changed if item.entity_id == 'speaker-fr' else item
                    for item in revision.document.entities
                )
            }
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    with pytest.raises(ValueError, match='CalibrationPlan SceneRevision mismatch'):
        build_installation_output(newer, variant=None, calibration_plan=plan)

    other_variant = build_system_variant(
        baseline=revision,
        name='other calibration variant',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc='2026-09-19T13:20:00+00:00',
    )
    with pytest.raises(ValueError, match='CalibrationPlan SystemVariant mismatch'):
        build_installation_output(
            revision,
            variant=other_variant,
            calibration_plan=plan,
        )

    other_plan, other_export, other_verification, other_events = _calibration_authorities(
        revision,
        variant,
        plan_id='cal-plan-other',
        max_boost_db=5.0,
    )
    assert other_plan.plan_semantic_sha256 != plan.plan_semantic_sha256
    with pytest.raises(ValueError, match='calibration export plan hash mismatch'):
        build_installation_output(
            revision,
            variant=variant,
            calibration_plan=plan,
            calibration_export_snapshot=other_export,
        )
    with pytest.raises(ValueError, match='VerificationMeasurementPlan mismatch'):
        build_installation_output(
            revision,
            variant=variant,
            calibration_plan=plan,
            calibration_export_snapshot=export,
            verification_measurement_plan=other_verification,
        )
    with pytest.raises(ValueError, match='calibration lifecycle event plan hash mismatch'):
        build_installation_output(
            revision,
            variant=variant,
            calibration_plan=plan,
            calibration_export_snapshot=export,
            verification_measurement_plan=verification,
            calibration_lifecycle_events=(other_events[0],),
        )

    wrong_export_event = build_calibration_lifecycle_event(
        plan=plan,
        state='exported',
        exported_settings=other_export,
        event_id='cal-plan-1-event-wrong-export',
        created_at_utc='2026-09-19T13:30:00+00:00',
    )
    with pytest.raises(ValueError, match='calibration lifecycle event export hash mismatch'):
        build_installation_output(
            revision,
            variant=variant,
            calibration_plan=plan,
            calibration_export_snapshot=export,
            calibration_lifecycle_events=(events[0], wrong_export_event),
        )


def test_csv_html_semantic_payload_and_export_timestamp_are_deterministic(
    tmp_path: Path,
) -> None:
    _repository, revision, variant, geometry = _scene_authorities(tmp_path)
    definition, placements, evaluations = _treatment_authorities(
        revision, variant, geometry
    )
    plan, export, verification, events = _calibration_authorities(revision, variant)
    output = build_installation_output(
        revision,
        variant=variant,
        treatment_definitions=(definition,),
        treatment_placements=placements,
        treatment_surface_evaluations=evaluations,
        calibration_plan=plan,
        calibration_export_snapshot=export,
        verification_measurement_plan=verification,
        calibration_lifecycle_events=events,
    )
    semantic_hash = output.semantic_sha256

    csv_a = render_installation_csv(output)
    csv_b = render_installation_csv(output)
    assert csv_a == csv_b
    assert 'authority_record,treatment,' in csv_a
    assert 'authority_record,calibration,' in csv_a
    assert 'treatment_instance,panel-a' in csv_a
    assert 'treatment_quantity,panel-600x1200@1,proposed,2' in csv_a
    assert 'calibration_setting,requested,FL,out-fl' in csv_a
    assert 'calibration_setting,exported,FL,out-fl' in csv_a

    html_a = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T16:00:00+00:00',
    )
    html_b = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T17:00:00+00:00',
    )
    assert html_a != html_b
    assert output.semantic_sha256 == semantic_hash
    assert 'Acoustic treatment' in html_a
    assert 'Calibration plan' in html_a
    assert 'requested' in html_a
    assert 'exported' in html_a
    marker = '<script type="application/json" id="htdt-installation-output">'
    payload_a = html_a.split(marker, 1)[1].split('</script>', 1)[0]
    payload_b = html_b.split(marker, 1)[1].split('</script>', 1)[0]
    assert payload_a == payload_b
    assert 'exported_at' not in payload_a


def test_v1_and_v2_serialized_installation_outputs_remain_loadable(tmp_path: Path) -> None:
    _repository, revision, variant, _geometry = _scene_authorities(tmp_path)
    v3 = build_installation_output(revision, variant=variant)

    common = {
        'coordinate_system': v3.coordinate_system,
        'authority': v3.authority.model_dump(mode='json'),
        'evidence': [item.model_dump(mode='json') for item in v3.evidence],
        'entities': [item.model_dump(mode='json') for item in v3.entities],
        'dimensions': [item.model_dump(mode='json') for item in v3.dimensions],
        'sections': [item.model_dump(mode='json') for item in v3.sections],
    }
    v1_identity = {
        'schema_version': 1,
        'authority_version': 'installation-output-1',
        **common,
    }
    v1 = InstallationOutput.model_validate_json(
        json.dumps(
            {
                **v1_identity,
                'semantic_sha256': _digest(v1_identity),
            }
        )
    )
    assert v1.schema_version == 1
    assert v1.projector is None
    assert v1.standards is None
    assert v1.treatment is None
    assert v1.calibration is None

    v2_identity = {
        'schema_version': 2,
        'authority_version': 'installation-output-2',
        **common,
        'projector': v3.projector.model_dump(mode='json'),
        'standards': v3.standards.model_dump(mode='json'),
    }
    v2 = InstallationOutput.model_validate_json(
        json.dumps(
            {
                **v2_identity,
                'semantic_sha256': _digest(v2_identity),
            }
        )
    )
    assert v2.schema_version == 2
    assert v2.projector is not None
    assert v2.standards is not None
    assert v2.treatment is None
    assert v2.calibration is None
