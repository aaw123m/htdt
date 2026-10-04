from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMicrophoneCapture,
    MEASUREMENT_QUALITY_CHECKS,
    build_acquisition_context,
    measurement_repeatability_rms_db,
)
from htdt.cad_measurement_quality_producer import (
    QUALITY_PRODUCER_VERSION,
    CadMeasurementQualityProducer,
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
from htdt.cad_scene import make_f1_scene
from htdt.measurement_workflow import (
    AcquisitionCapture,
    MeasurementAssignment,
    MeasurementWorkflowController,
)


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision


def _controller(
    scene_repository, document_id
) -> tuple[
    MeasurementWorkflowController,
    CadMeasurementRepository,
    CadMeasurementQualityRepository,
]:
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return controller, measurement_repository, quality_repository


def _assignment(**overrides) -> MeasurementAssignment:
    values = dict(
        measurement_entity_id="point-mlp",
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        routing_evidence="manual",
    )
    values.update(overrides)
    return MeasurementAssignment(**values)


def _save_measurement_directly(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    level_db: tuple[float, ...] = (70.0, 71.0, 69.0),
):
    """Persist a measurement the way a pre-producer import would have."""
    processing = {"fixture_raw": measurement_id}
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_deg=None,
        phase_status="absent",
        level_reference="unknown",
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        "point-mlp",
        measurement_id=measurement_id,
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        routing_evidence="manual",
        imported_at="2026-09-19T00:00:00+00:00",
        source_kind="unknown",
        external_source_id=f"rew-{measurement_id}",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f"dataset-{measurement_id}",
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_deg=None,
        phase_status="absent",
        level_reference="unknown",
        processing_json=canonical_json(processing),
        source_sha256=sha256(declared_raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f"{measurement_id}.json",
        raw_bytes=declared_raw,
    )
    return record, dataset


def _check(report, name: str):
    return getattr(report, name)


# ---------------------------------------------------------------------
# Derivation at the commit/promote path


def test_commit_pending_produces_honest_report(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
    record = controller.commit_pending(_assignment())

    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    dataset = measurement_repository.dataset_for_measurement(
        record.measurement_id
    )
    assert report.dataset_id == dataset.dataset_id
    assert report.measurement_sha256 is not None

    # Provable evidence only: the imported grid bounds are attested through
    # a machine observation pinned to the raw asset.
    assert report.evidence.usable_frequency_band_hz == (20.0, 80.0)
    assert report.usable_frequency_band.status == "PASS"
    assert report.observation is not None
    assert report.evidence.evidence_source == "raw_asset"
    observation = quality_repository.get_observation(
        report.observation.observation_id
    )
    assert observation is not None
    assert observation.source_asset_sha256 == dataset.source_sha256
    assert observation.usable_frequency_band_hz == (20.0, 80.0)
    assert '"measurement-quality-producer-1"' in observation.provenance_json

    # Everything the imports cannot prove stays honestly unresolved —
    # never a fabricated PASS.
    assert report.clipping.status == "UNKNOWN"
    assert report.noise_snr.status == "UNKNOWN"
    assert report.polarity.status == "UNKNOWN"
    assert report.timing_reference.status == "UNKNOWN"
    assert report.ir_window.status == "NOT_EVALUATED"
    assert report.calibration.status == "UNKNOWN"
    assert report.repeatability.status == "NOT_EVALUATED"
    assert report.evidence.clipping_detected is None
    assert report.evidence.snr_db is None
    assert report.evidence.polarity_correct is None
    assert report.evidence.calibration_file_sha256 is None
    assert report.acquisition_context is None
    for field in (
        "timing_reference_valid",
        "timing_reference_id",
        "clock_source",
        "sample_rate_hz",
        "delay_correction_s",
    ):
        assert getattr(report.evidence, field) is None
    assert report.retake_recommendation == "UNKNOWN"


def test_commit_binds_acquisition_context_verbatim(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
    record = controller.commit_pending(
        _assignment(
            acquisition=AcquisitionCapture(
                source_kind="native",
                timing_reference_valid=True,
                timing_reference_id="ref-clock-1",
                clock_source="word_clock",
                sample_rate_hz=48000,
                delay_correction_s=0.0,
                acquisition_session_id="session-1",
            )
        )
    )

    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    assert report.acquisition_context is not None
    context = quality_repository.get_acquisition_context(
        report.acquisition_context.acquisition_context_id
    )
    assert context is not None
    # Timing evidence is the context's attested values verbatim.
    assert report.evidence.timing_reference_valid is True
    assert report.evidence.timing_reference_id == "ref-clock-1"
    assert report.evidence.clock_source == "word_clock"
    assert report.evidence.sample_rate_hz == 48000
    assert report.evidence.delay_correction_s == 0.0
    assert report.timing_reference.status == "PASS"
    assert report.capability("common_timing").decision == "ALLOWED"


def test_report_production_is_idempotent(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
    record = controller.commit_pending(_assignment())
    first = quality_repository.latest_report(record.measurement_id)
    assert first is not None

    producer = CadMeasurementQualityProducer(quality_repository)
    again = producer.produce_report(record.measurement_id)
    assert again.status == "current"
    assert again.report == first
    assert len(quality_repository.list_reports(record.measurement_id)) == 1
    assert (
        len(quality_repository.list_observations(record.measurement_id)) == 1
    )


def test_backfill_on_first_quality_read(tmp_path: Path) -> None:
    """A pre-producer committed measurement derives on first quality read."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    record, dataset = _save_measurement_directly(
        measurement_repository, revision, "legacy-measurement"
    )
    assert (
        quality_repository.latest_report(record.measurement_id) is None
    )

    rows = {
        row.measurement_id: row
        for row in controller.measurement_views()
    }
    view = rows[record.measurement_id]
    assert view.quality_report_state == "current"
    checks = {item.check: item.status for item in view.quality_checks}
    assert checks["usable_frequency_band"] == "PASS"
    assert checks["calibration"] == "UNKNOWN"

    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    assert report.dataset_id == dataset.dataset_id


def test_ensure_reports_backfill_is_idempotent(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    _, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    record, _ = _save_measurement_directly(
        measurement_repository, revision, "legacy-measurement"
    )

    producer = CadMeasurementQualityProducer(quality_repository)
    first = producer.ensure_reports(revision.document_id)
    assert [result.status for result in first] == ["produced"]

    second = producer.ensure_reports(revision.document_id)
    assert second == ()
    assert len(quality_repository.list_reports(record.measurement_id)) == 1


def test_ir_import_produces_new_report_epoch(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
    record = controller.commit_pending(_assignment())
    first = quality_repository.latest_report(record.measurement_id)
    assert first is not None
    assert first.evidence.has_impulse_response is False
    assert first.ir_window.status == "NOT_EVALUATED"

    ir_raw = b"0.0 1.0\n0.001 0.5\n0.002 -0.2\n0.003 0.05\n"
    controller.import_ir_for_measurement(
        record.measurement_id, ir_raw, filename="ir.txt"
    )

    reports = quality_repository.list_reports(record.measurement_id)
    assert len(reports) == 2
    latest = reports[-1]
    assert latest.evidence.has_impulse_response is True
    assert latest.evidence.ir_window_start_s == 0.0
    assert latest.evidence.ir_window_end_s is not None
    assert latest.evidence.ir_window_end_s > 0.0
    # Truncation cannot be disproved by a text export: honest UNKNOWN.
    assert latest.evidence.ir_truncated is None
    assert latest.ir_window.status == "UNKNOWN"
    assert latest.evidence.evidence_source == "mixed"
    assert latest.observation is not None
    observation = quality_repository.get_observation(
        latest.observation.observation_id
    )
    assert observation.source_kind == "mixed"
    assert observation.has_impulse_response is True


def test_same_binding_commits_derive_repeatability(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "first.txt")
    first_record = controller.commit_pending(_assignment())
    controller.stage_rew_text(b"20 71\n40 72\n80 70\n", "second.txt")
    second_record = controller.commit_pending(_assignment())

    first_dataset = measurement_repository.dataset_for_measurement(
        first_record.measurement_id
    )
    second_dataset = measurement_repository.dataset_for_measurement(
        second_record.measurement_id
    )
    report = quality_repository.latest_report(second_record.measurement_id)
    assert report is not None
    assert report.evidence.repeat_measurement_ids == (
        first_record.measurement_id,
        second_record.measurement_id,
    )
    assert report.evidence.repeatability_rms_db == (
        measurement_repeatability_rms_db((first_dataset, second_dataset))
    )
    # No profile threshold is configured: evidence exists, honestly
    # not evaluated rather than claimed good or bad.
    assert report.repeatability.status == "NOT_EVALUATED"

    # The first measurement's report is a sealed historical epoch.
    first_report = quality_repository.latest_report(
        first_record.measurement_id
    )
    assert first_report.evidence.repeat_measurement_ids == ()


def test_declared_calibration_only_claims_when_retained(
    tmp_path: Path,
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    # A declared digest that never resolved to a retained asset claims
    # nothing: the calibration check stays honestly UNKNOWN.
    phantom = sha256(b"never-retained").hexdigest()
    controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
    record = controller.commit_pending(
        _assignment(
            acquisition=AcquisitionCapture(
                source_kind="native",
                microphone=CadMicrophoneCapture(
                    calibration_sha256=phantom,
                    calibration_filename="ghost.cal",
                ),
            )
        )
    )
    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    assert report.evidence.calibration_file_sha256 is None
    assert report.evidence.expected_calibration_file_sha256 is None
    assert report.calibration.status == "UNKNOWN"

    # A retained calibration asset is provable provenance: the produced
    # report binds it as both the expected and the applied authority.
    retained = quality_repository.save_calibration_file(
        filename="mic.cal", raw_bytes=b"microphone-calibration-data"
    )
    controller.stage_rew_text(b"20 72\n40 73\n80 71\n", "seat2.txt")
    record2 = controller.commit_pending(
        _assignment(
            channel_role="center",
            acquisition=AcquisitionCapture(
                source_kind="native",
                microphone=CadMicrophoneCapture(
                    calibration_sha256=retained,
                    calibration_filename="mic.cal",
                ),
            )
        )
    )
    report2 = quality_repository.latest_report(record2.measurement_id)
    assert report2 is not None
    assert report2.evidence.calibration_file_sha256 == retained
    assert report2.evidence.expected_calibration_file_sha256 == retained
    assert report2.evidence.calibration_filename == "mic.cal"
    assert report2.calibration.status == "PASS"


def test_producer_fails_closed_on_missing_subject(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    _, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    producer = CadMeasurementQualityProducer(quality_repository)

    result = producer.produce_report("no-such-measurement")
    assert result.status == "unresolved"
    assert result.report is None

    # Nothing is ever persisted for an unverifiable subject.
    import sqlite3

    with sqlite3.connect(quality_repository.path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM cad_measurement_quality_reports"
        ).fetchone()[0]
    assert rows == 0


def test_producer_version_is_recorded(tmp_path: Path) -> None:
    assert QUALITY_PRODUCER_VERSION == "measurement-quality-producer-1"
    # The producer only derives; every check the evaluator emits comes from
    # the canonical algorithm, never from producer-side statuses.
    assert len(MEASUREMENT_QUALITY_CHECKS) == 8


def test_calibration_plan_resolves_produced_report_end_to_end(
    tmp_path: Path,
) -> None:
    """The calibration-plan consumer binds the produced report verbatim.

    REV42-AUTOMATE found this consumer could never resolve: plans require
    ``measurement_quality_report_id`` but no production path ever created
    reports. With the producer wired into the commit path, the same
    report the commit derived is what the plan binds.
    """
    from htdt.cad_calibration import (
        CadCalibrationChannel,
        CadDeviceCapabilityConstraints,
        CadTargetCurve,
        CadTargetCurvePoint,
        CadTargetNormalizationCondition,
        build_calibration_plan,
    )
    from htdt.cad_system_variant import build_system_variant
    from htdt.cad_system_variant_repository import CadSystemVariantRepository

    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=revision,
        name="Producer consumer variant",
        role_bindings=(),
        proposed_entities=(),
        created_at_utc="2026-09-19T12:30:00+00:00",
    )
    variant_repository.save_variant(variant)

    controller.stage_rew_text(b"20 72\n40 73\n80 71\n", "seat1.txt")
    record = controller.commit_pending(
        _assignment(
            channel_role="front_left",
            acquisition=AcquisitionCapture(
                source_kind="native",
                timing_reference_valid=True,
                timing_reference_id="loopback-1",
                clock_source="umik-1-usb",
                sample_rate_hz=48000,
                delay_correction_s=0.00025,
            ),
        )
    )
    dataset = measurement_repository.dataset_for_measurement(
        record.measurement_id
    )
    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None

    plan = build_calibration_plan(
        scene_revision=revision,
        system_variant=variant,
        measurement=record,
        dataset=dataset,
        quality_report=report,
        channels=(
            CadCalibrationChannel(
                channel_id="FL",
                role_id="FL",
                source_entity_id="speaker-fl",
                physical_output_id="out-fl",
                sample_rate_hz=48000,
                gain_db=0.0,
                delay_s=0.0,
                polarity="normal",
                routing=("main",),
            ),
        ),
        sample_rate_hz=48000,
        device_constraints=CadDeviceCapabilityConstraints(
            capability_id="generic-device",
            capability_version="1",
            supported_sample_rates_hz=(48000,),
            supported_filter_types=("peaking",),
            max_filters_per_channel=4,
            max_boost_db=6.0,
            max_cut_db=12.0,
            min_gain_db=-12.0,
            max_gain_db=6.0,
            max_delay_s=0.050,
            supported_crossover_orders=(2, 4),
        ),
        max_boost_db=6.0,
        max_cut_db=12.0,
        target_curve=CadTargetCurve(
            points=(
                CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
                CadTargetCurvePoint(frequency_hz=20000.0, level_db=-6.0),
            ),
            normalization=CadTargetNormalizationCondition(
                method="reference_frequency",
                reference_frequency_hz=1000.0,
            ),
        ),
        created_at_utc="2026-09-19T12:33:00+00:00",
    )
    assert plan.measurement_quality_report_id == report.report_id
    assert plan.measurement_quality_report_sha256 == report.report_sha256
