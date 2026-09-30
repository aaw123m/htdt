"""REV21-EXPORT part 2: calibration CSV + handoff settings.csv + cell_transfers."""
import csv
import io
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, 'backend/src')

from htdt.cad_calibration import (
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    CadCalibrationChannel,
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
    build_biquad_filter,
    build_calibration_plan,
    render_generic_biquad_csv,
    build_generic_biquad_export,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
    CadMeasurementQualityEvidence,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_system_variant import build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository

NOW = '2026-09-30T00:00:00+00:00'


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    scene_repo = SceneRepository(tmp / 'cad.sqlite3')
    revision = scene_repo.save(make_f1_scene(), parent_revision_id=None).revision
    mrepo = CadMeasurementRepository(scene_repo)
    qrepo = CadMeasurementQualityRepository(mrepo)
    vrepo = CadSystemVariantRepository(scene_repo)
    variant = build_system_variant(
        baseline=revision, name='v', role_bindings=(), proposed_entities=(),
        created_at_utc=NOW,
    )
    vrepo.save_variant(variant)

    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent', level_reference='spl',
        processing={'fixture_raw': 'raw-m-1'},
    )
    record = measurement_record_for_revision(
        revision, 'point-mlp', measurement_id='m-1', evidence_type='measured',
        channel_role='FL', source_speaker_ids=('speaker-fl',),
        radiation_scope='single', routing_evidence='verified',
        imported_at=NOW, source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-m-1', measurement_id='m-1',
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent', level_reference='spl',
        processing_json=canonical_json({'fixture_raw': 'raw-m-1'}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    mrepo.save(record, dataset, raw_filename='m-1.json', raw_bytes=raw)
    fields = {'usable_frequency_band_hz': (20.0, 20000.0)}
    obs = build_measurement_observation(
        observation_id='obs-1', measurement_id='m-1', source_kind='manual',
        observed_at_utc=NOW, **fields,
    )
    qrepo.save_observation(obs)
    qrepo.save_report(build_measurement_quality_report(
        measurement=record, dataset=dataset,
        evidence=CadMeasurementQualityEvidence(**fields, evidence_source='manual'),
        profile=build_measurement_quality_profile(
            profile_version='verify-quality-1'
        ),
        observation=observation_binding(obs),
        report_id='q-1', created_at_utc=NOW,
    ))

    channel = CadCalibrationChannel(
        channel_id='FL', role_id='FL', source_entity_id='speaker-fl',
        physical_output_id='out-fl', sample_rate_hz=48000,
        gain_db=-1.5, delay_s=0.0, polarity='normal',
        crossovers=(CadCrossoverSetting(
            crossover_type='high_pass', frequency_hz=90.0, filter_order=4
        ),),
        peq=(build_biquad_filter(
            filter_id='peq-1', filter_type='peaking', frequency_hz=63.0,
            q=1.4, gain_db=-3.0, sample_rate_hz=48000,
        ),),
        routing=('main',),
    )
    device = CadDeviceCapabilityConstraints(
        capability_id='generic-device', capability_version='1',
        supported_sample_rates_hz=(48000,), supported_filter_types=('peaking',),
        max_filters_per_channel=4, max_boost_db=6.0, max_cut_db=12.0,
        min_gain_db=-12.0, max_gain_db=6.0, max_delay_s=0.050,
        supported_crossover_orders=(2, 4),
        allowed_physical_outputs=('out-fl',),
    )
    target = CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=20000.0, level_db=-6.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='reference_frequency', reference_frequency_hz=1000.0,
        ),
    )
    plan = build_calibration_plan(
        scene_revision=revision, system_variant=variant,
        measurement=record, dataset=dataset,
        quality_report=qrepo.get_report('q-1'),
        channels=(channel,), sample_rate_hz=48000,
        device_constraints=device, max_boost_db=6.0, max_cut_db=12.0,
        target_curve=target, plan_id='plan-1', plan_version='v1',
        created_at_utc=NOW, source_kind='provided_fixture',
    )
    snapshot = build_generic_biquad_export(plan=plan, created_at_utc=NOW)
    text = render_generic_biquad_csv(snapshot)
    print('=== F2: calibration settings CSV ===')
    print(text)
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    cells = {c for r in rows for c in r}
    for key in ('sample_rate_hz', 'calibration_plan_id', 'export_id',
                'source_entity_id', 'routing', 'crossover', '48000',
                'quantization'):
        print(f'F2 {key!r} present:', any(key in c for c in cells) or key in header)
    print('F2 header:', header)
    print('snapshot.channel has crossovers:', len(snapshot.channels[0].crossovers),
          'routing:', snapshot.channels[0].routing,
          'source_entity:', snapshot.channels[0].source_entity_id)


if __name__ == '__main__':
    main()
