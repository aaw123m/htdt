"""Round 21 — export completeness & fidelity regressions.

Every export surface must carry what the user sees and means: the columns
the exporting view shows, the provenance needed to interpret values later,
and every curve a comparison renders — never just the difference trace.
"""

from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from htdt.analysis_export import (
    build_analysis_export,
    comparison_export_parts,
    render_analysis_csv,
    series_from_measurement_dataset,
)
from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
    build_biquad_filter,
    build_calibration_plan,
    build_generic_biquad_export,
    render_generic_biquad_csv,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
    normalize_rew_text,
)
from htdt.cad_prediction_matrix_repository import (
    CadPredictionMatrixRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.cad_system_variant import build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.comparison import FrequencyResponse, compare_frequency_responses
from htdt.installation_handoff import (
    HandoffReview,
    InstallationHandoff,
    render_settings_csv,
)
from htdt.prediction_matrix_service import PredictionMatrixService
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.report import (
    InstallationAuthorityBinding,
    InstallationCalibrationChannelSummary,
    InstallationCalibrationPeqSummary,
    InstallationCalibrationSummary,
    InstallationOutput,
)

NOW = '2026-09-30T00:00:00+00:00'


def _csv_rows(text: str):
    return list(csv.reader(io.StringIO(text.lstrip('﻿'))))


def _comparison_fixture(tmp_path: Path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repo.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repo = CadMeasurementRepository(scene_repo)
    record_a, dataset_a, fn_a, raw_a = normalize_rew_text(
        revision, 'point-mlp', b'20 70\n40 71\n80 69\n',
        filename='a.txt', imported_at=NOW,
    )
    repo.save(record_a, dataset_a, raw_filename=fn_a, raw_bytes=raw_a)
    record_b, dataset_b, fn_b, raw_b = normalize_rew_text(
        revision, 'point-mlp', b'20 69\n40 70\n80 68\n',
        filename='b.txt', imported_at=NOW,
    )
    repo.save(record_b, dataset_b, raw_filename=fn_b, raw_bytes=raw_b)
    result = compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0,
        80.0,
    )
    comparison = repo.save_comparison(
        dataset_a.dataset_id,
        dataset_b.dataset_id,
        result,
        semantics_json='{"verdict": "fixture"}',
    )
    return revision, repo, dataset_a, comparison


def test_analysis_csv_carries_document_identity_and_axis_labels(
    tmp_path: Path,
) -> None:
    """A CSV opened later must say which document/spec produced it and what
    its axes mean — identity fields the JSON payload already carried."""
    revision, repo, dataset_a, comparison = _comparison_fixture(tmp_path)
    record_a = repo.get_measurement(dataset_a.measurement_id)
    series, metadata = comparison_export_parts(
        comparison, current_scene_revision_id=revision.revision_id
    )
    export = build_analysis_export(
        document_id=revision.document.document_id,
        title='fidelity check',
        generated_at_utc=NOW,
        series=(
            series_from_measurement_dataset(
                dataset_a, record_a,
                current_scene_revision_id=revision.revision_id,
            ),
            *series,
        ),
        metadata=metadata,
    )
    rows = _csv_rows(render_analysis_csv(export))
    header_rows = {r[1]: r[2] for r in rows if len(r) == 3 and r[0] == 'export'}
    assert header_rows['document_id'] == revision.document.document_id
    assert header_rows['schema_version'] == '1'
    assert header_rows['authority_version'] == 'analysis-export-1'
    series_header = next(r for r in rows if r and r[0] == 'series_id')
    assert 'x_label' in series_header and 'y_label' in series_header
    row = next(r for r in rows if r and r[0] == f'dataset:{dataset_a.dataset_id}')
    labelled = dict(zip(series_header, row))
    assert labelled['x_label'] == 'Frequency'
    assert labelled['y_label'] == 'Level'


def test_comparison_export_ships_all_three_workspace_curves(
    tmp_path: Path,
) -> None:
    """The comparison surface renders side A, side B and the difference —
    the export must carry all three plus the semantics payload."""
    revision, _repo, _dataset_a, comparison = _comparison_fixture(tmp_path)
    series, metadata = comparison_export_parts(
        comparison, current_scene_revision_id=revision.revision_id
    )
    ids = [s.series_id for s in series]
    assert ids == [
        f'comparison:{comparison.comparison_id}:a',
        f'comparison:{comparison.comparison_id}:b',
        f'comparison:{comparison.comparison_id}',
    ]
    by_id = {s.series_id: s for s in series}
    assert len(by_id[ids[0]].points) == len(comparison.grid_hz)
    assert [p.y for p in by_id[ids[0]].points] == list(comparison.a_db)
    assert [p.y for p in by_id[ids[1]].points] == list(comparison.b_db)
    assert [p.y for p in by_id[ids[2]].points] == list(comparison.difference_db)
    keys = {m.key: m.value for m in metadata}
    assert (
        keys[f'comparison.{comparison.comparison_id}.semantics_json']
        == '{"verdict": "fixture"}'
    )


def _calibration_fixture(tmp_path: Path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repo.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repo)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    system_variant_repository = CadSystemVariantRepository(scene_repo)
    variant = build_system_variant(
        baseline=revision,
        name='v',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(variant)

    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing={'fixture_raw': 'raw-m-1'},
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id='m-1',
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at=NOW,
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-m-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json({'fixture_raw': 'raw-m-1'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record, dataset, raw_filename='m-1.json', raw_bytes=raw
    )
    fields = {'usable_frequency_band_hz': (20.0, 20000.0)}
    obs = build_measurement_observation(
        observation_id='obs-1',
        measurement_id='m-1',
        source_kind='manual',
        observed_at_utc=NOW,
        **fields,
    )
    quality_repository.save_observation(obs)
    quality_repository.save_report(
        build_measurement_quality_report(
            measurement=record,
            dataset=dataset,
            evidence=CadMeasurementQualityEvidence(
                **fields, evidence_source='manual'
            ),
            profile=build_measurement_quality_profile(
                profile_version='verify-quality-1'
            ),
            observation=observation_binding(obs),
            report_id='q-1',
            created_at_utc=NOW,
        )
    )

    channel = CadCalibrationChannel(
        channel_id='FL',
        role_id='FL',
        source_entity_id='speaker-fl',
        physical_output_id='out-fl',
        sample_rate_hz=48000,
        gain_db=-1.5,
        delay_s=0.0,
        polarity='normal',
        crossovers=(
            CadCrossoverSetting(
                crossover_type='high_pass',
                frequency_hz=90.0,
                filter_order=4,
            ),
        ),
        peq=(
            build_biquad_filter(
                filter_id='peq-1',
                filter_type='peaking',
                frequency_hz=63.0,
                q=1.4,
                gain_db=-3.0,
                sample_rate_hz=48000,
            ),
        ),
        routing=('main',),
    )
    device = CadDeviceCapabilityConstraints(
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
        measurement=record,
        dataset=dataset,
        quality_report=quality_repository.get_report('q-1'),
        channels=(channel,),
        sample_rate_hz=48000,
        device_constraints=device,
        max_boost_db=6.0,
        max_cut_db=12.0,
        target_curve=target,
        plan_id='plan-1',
        plan_version='v1',
        created_at_utc=NOW,
        source_kind='provided_fixture',
    )
    return revision, variant, channel, plan


def test_calibration_csv_carries_provenance_and_full_channel_fields(
    tmp_path: Path,
) -> None:
    """Biquad coefficients are uninterpretable without their sample rate,
    and the review shows source entity / crossovers / routing — all must
    survive into the CSV."""
    _revision, _variant, _channel, plan = _calibration_fixture(tmp_path)
    snapshot = build_generic_biquad_export(plan=plan, created_at_utc=NOW)
    rows = _csv_rows(render_generic_biquad_csv(snapshot))
    header_rows = {r[1]: r[2] for r in rows if len(r) == 3 and r[0] == 'export'}
    assert header_rows['export_id'] == snapshot.export_id
    assert header_rows['calibration_plan_id'] == 'plan-1'
    assert header_rows['sample_rate_hz'] == '48000'
    assert header_rows['requested_plan_semantic_sha256'] == (
        plan.plan_semantic_sha256
    )
    assert header_rows['exported_settings_semantic_sha256'] == (
        snapshot.exported_settings_semantic_sha256
    )
    assert header_rows['quantization_applied'] == 'false'
    table_header = next(r for r in rows if r and r[0] == 'channel_id')
    row = next(r for r in rows if r and r[0] == 'FL')
    record = dict(zip(table_header, row))
    assert record['source_entity_id'] == 'speaker-fl'
    assert json.loads(record['crossover_json']) == [
        {'crossover_type': 'high_pass', 'filter_order': 4, 'frequency_hz': 90.0}
    ]
    assert json.loads(record['routing_json']) == ['main']
    assert record['filter_id'] == 'peq-1'


def _handoff_with_calibration() -> InstallationHandoff:
    channel = InstallationCalibrationChannelSummary(
        settings_source='exported',
        channel_id='FL',
        role_id='FL',
        source_entity_id='speaker-fl',
        physical_output_id='out-fl',
        sample_rate_hz=48000,
        gain_db=-1.5,
        delay_s=0.0125,
        polarity='normal',
        crossovers=(('high_pass', 90.0, 4),),
        peq=(
            InstallationCalibrationPeqSummary(
                filter_index=0,
                filter_id='peq-1',
                filter_type='peaking',
                frequency_hz=63.0,
                q=1.4,
                gain_db=-3.0,
            ),
        ),
        routing=('main',),
    )
    output = InstallationOutput.model_construct(
        authority=InstallationAuthorityBinding(
            document_id='doc-1',
            scene_revision_id='rev-1',
            scene_content_hash='a' * 64,
            effective_scene_content_hash='b' * 64,
        ),
        calibration=InstallationCalibrationSummary(
            status='AVAILABLE',
            plan_id='plan-1',
            sample_rate_hz=48000,
            exported_channels=(channel,),
            lifecycle_state='exported',
        ),
        semantic_sha256='c' * 64,
    )
    return InstallationHandoff.model_construct(
        output=output,
        review=HandoffReview(complete=False, sections=(), degraded=()),
        generated_at_utc=NOW,
    )


def test_handoff_settings_csv_carries_full_channel_fields() -> None:
    """The HTML report in the same package shows role, source entity,
    crossover, ordered PEQ and sample rate — settings.csv must too."""
    handoff = _handoff_with_calibration()
    rows = _csv_rows(render_settings_csv(handoff))
    table_header = rows[0]
    row = rows[1]
    record = dict(zip(table_header, row))
    assert record['settings_source'] == 'exported'
    assert record['role_id'] == 'FL'
    assert record['source_entity_id'] == 'speaker-fl'
    assert record['sample_rate_hz'] == '48000'
    assert json.loads(record['crossover_json']) == [['high_pass', 90.0, 4]]
    assert json.loads(record['peq_json']) == [
        {
            'filter_index': 0,
            'filter_id': 'peq-1',
            'filter_type': 'peaking',
            'frequency_hz': 63.0,
            'gain_db': -3.0,
            'q': 1.4,
        }
    ]
    assert json.loads(record['routing_json']) == ['main']
    assert record['lifecycle_state'] == 'exported'


# -- prediction matrix cell transfer export --------------------------------

MATRIX_DOC = 'o986-matrix-fixture'
AXIS = (20.0, 100.0, 200.0)
DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)


def _solver_ref() -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id='solver:fixture',
        authority_version='1',
        semantic_hash_sha256=sha256(b'solver').hexdigest(),
    )


def _matrix_fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=MATRIX_DOC,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='speaker-fl',
                speaker_role='FL',
                position=Position3(x_m=-1.0, y_m=0.0, z_m=1.0),
                size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
            ),
            SceneEntity(
                entity_id='seat-a',
                kind='seat',
                name='seat-a',
                position=Position3(x_m=0.0, y_m=2.0, z_m=0.5),
                size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
                acoustic_reference_offset_m=Offset3(z_m=0.5),
            ),
        ),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    service = PredictionMatrixService(scene_repository, MATRIX_DOC)
    return scene_repository, revision, service


def _matrix_provider(revision, source_entity_id: str) -> SimpleNamespace:
    snapshot_sha = sha256(b'snapshot').hexdigest()
    return SimpleNamespace(
        provider_id='r170a-provider:'
        + sha256(source_entity_id.encode()).hexdigest(),
        current_authority=SimpleNamespace(
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            acoustic_scene_snapshot_id='snapshot-1',
            acoustic_scene_snapshot_sha256=snapshot_sha,
            solver_implementation_ref=_solver_ref(),
        ),
        source_identity=SimpleNamespace(
            source_binding=SimpleNamespace(source_entity_id=source_entity_id),
            source_binding_sha256=sha256(
                f'bind:{source_entity_id}'.encode()
            ).hexdigest(),
        ),
        receiver_identities=(
            SimpleNamespace(
                receiver_binding=SimpleNamespace(
                    receiver_id='seat-a', entity_id='seat-a'
                ),
                receiver_binding_sha256=sha256(b'rbind:seat-a').hexdigest(),
            ),
        ),
        capability=lambda observable: SimpleNamespace(
            state='READY', reason=None
        ),
        phase_capability='READY',
        ref=lambda: None,
        result_artifact_ref=_solver_ref(),
        source_normalization_id='norm-shared',
        timing_authority='absolute_propagation_time',
        receiver_responses=(
            SimpleNamespace(
                receiver_id='seat-a',
                frequency_hz=AXIS,
                magnitude_pa=(1.0, 0.5, 0.25),
                phase_deg=(0.0, -10.0, -20.0),
                pressure_reference_pa=20.0e-6,
                phase_convention='exp(-iwt)',
            ),
        ),
    )


def test_cell_transfers_returns_frequency_magnitude_and_phase(
    tmp_path: Path,
) -> None:
    """The cell transfer export promises (frequency, magnitude, phase) —
    phase must reach the caller, not be silently dropped."""
    _scene_repository, revision, service = _matrix_fixture(tmp_path)
    spec = service.create_matrix(
        source_entity_ids=('speaker-fl',),
        receiver_ids=('seat-a',),
        providers={'speaker-fl': _matrix_provider(revision, 'speaker-fl')},
        frequency_axis_hz=AXIS,
        solver_implementation_ref=_solver_ref(),
        valid_frequency_domain=DOMAIN,
    )
    service.run_matrix(spec.spec_id, {
        'speaker-fl': _matrix_provider(revision, 'speaker-fl')
    })
    cell = service.cell_transfers(
        spec.spec_id, 'source:speaker-fl', 'receiver:seat-a'
    )
    assert cell is not None
    reference_pa, frequency_hz, magnitude_pa, phase_deg = cell
    assert reference_pa == 20.0e-6
    assert frequency_hz == AXIS
    assert magnitude_pa == (1.0, 0.5, 0.25)
    assert phase_deg == (0.0, -10.0, -20.0)
    assert service.cell_transfers(
        spec.spec_id, 'source:speaker-fl', 'receiver:seat-b'
    ) is None
