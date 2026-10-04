"""REV44-QUALITYAUTH: production registration surfaces for the quality
authority families.

Each test drives the real registration affordance — the dialog's
``build_record`` plus the repository save, or the workspace handler —
then proves the downstream consumer actually reaches the new state:
the onboarding SPL step leaves 'manual', the routing combo populates,
the producer resolves beyond UNKNOWN, and the persisted context carries
the bound authority hash. Cancel leaves state untouched; invalid input
fails closed inside the dialog without persisting anything.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct
import wave
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMicrophoneCapture,
    build_acquisition_context,
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
from htdt.measurement_instrument_onboarding import evaluate_instrument_onboarding
from htdt.measurement_workflow import (
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
):
    processing = {"fixture_raw": measurement_id}
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
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
        level_db=(70.0, 71.0, 69.0),
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


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _workspace(controller):
    from htdt.measurement_page_workspace import MeasurementPageWorkspace

    return MeasurementPageWorkspace(controller)


def _close(workspace) -> None:
    workspace.close()
    workspace.deleteLater()


# ---------------------------------------------------------------------------
# Timing reference — typed registration + acquisition binding


def _build_persistent_loopback_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Build the dialog record the way the 登録 dialog does."""
    from htdt.measurement_authority_dialogs import TimingReferenceDialog

    dialog = TimingReferenceDialog()
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('loopback')
    )
    dialog.reference_channel_edit.setText('UMIK ch2 (loopback)')
    dialog.input_clock_edit.setText('umik-1-in-clock')
    dialog.output_clock_edit.setText('umik-1-out-clock')
    dialog.sample_rate_edit.setText('48000')
    dialog.t0_combo.setCurrentIndex(
        dialog.t0_combo.findData('loopback_edge')
    )
    dialog._add_correction_row()
    value_item = dialog.corrections_table.item(0, 1)
    value_item.setText('1.2')
    dialog.scope_combo.setCurrentIndex(
        dialog.scope_combo.findData('persistent')
    )
    dialog.signal_path_edit.setText('hdmi-avr→umik-1/usb')
    record = dialog.build_record()
    dialog.deleteLater()
    return record


def test_timing_reference_registers_lists_and_binds(qapp, tmp_path: Path) -> None:
    from htdt.measurement_authority_dialogs import TimingReferenceDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    dialog = TimingReferenceDialog()
    assert dialog.exec is not None  # sanity: real dialog object
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('loopback')
    )
    dialog.reference_channel_edit.setText('UMIK ch2 (loopback)')
    dialog.input_clock_edit.setText('umik-1-in-clock')
    dialog.output_clock_edit.setText('umik-1-out-clock')
    dialog.sample_rate_edit.setText('48000')
    dialog.t0_combo.setCurrentIndex(
        dialog.t0_combo.findData('loopback_edge')
    )
    dialog._add_correction_row()
    dialog.corrections_table.item(0, 1).setText('1.2')
    dialog.scope_combo.setCurrentIndex(
        dialog.scope_combo.findData('persistent')
    )
    dialog.signal_path_edit.setText('hdmi-avr→umik-1/usb')
    reference = dialog.build_record()
    dialog.deleteLater()

    quality_repository.save_timing_reference(reference)

    # Consumer 1: the listing surface sees it.
    listed = quality_repository.list_timing_references()
    assert any(
        entry.timing_reference_id == reference.timing_reference_id
        for entry in listed
    )

    # Consumer 2: the acquisition card combo offers it.
    workspace = _workspace(controller)
    try:
        index = workspace.timing_reference_combo.findData(
            reference.timing_reference_id
        )
        assert index > 0
        workspace.timing_reference_combo.setCurrentIndex(index)
        # Selecting the reference auto-fills its scope identity.
        workspace._timing_reference_changed()
        assert workspace.signal_path_edit.text() == 'hdmi-avr→umik-1/usb'
        assert workspace.mic_sample_rate_edit.text() == '48000'

        capture, _direction, error = workspace._collect_acquisition_capture()
        assert error is None
        assert capture is not None
        # The bound authority derives the flat fields verbatim.
        assert capture.timing_reference_valid is True
        assert capture.timing_reference_id == reference.timing_reference_id
        assert capture.timing_reference_sha256 == reference.timing_reference_sha256
        assert capture.clock_source == 'umik-1-in-clock'
        assert capture.sample_rate_hz == 48000
        assert capture.delay_correction_s == pytest.approx(0.0012)

        controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
        record = controller.commit_pending(_assignment(acquisition=capture))
    finally:
        _close(workspace)

    # Consumer 3: the produced report replays the binding to PASS/ALLOWED.
    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    assert report.timing_reference.status == "PASS"
    assert report.capability("common_timing").decision == "ALLOWED"
    context = quality_repository.get_acquisition_context(
        report.acquisition_context.acquisition_context_id
    )
    assert context.signal_path_identity == 'hdmi-avr→umik-1/usb'


def test_timing_reference_non_authorizing_method_stays_honest(
    qapp, tmp_path: Path
) -> None:
    """A manual-declared reference binds as evidence but never upgrades it:
    the context records the binding while ``timing_reference_valid`` stays
    unset so the report honestly resolves UNKNOWN, not PASS."""
    from htdt.measurement_authority_dialogs import TimingReferenceDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    dialog = TimingReferenceDialog()
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('manual')
    )
    dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData('unknown'))
    reference = dialog.build_record()
    dialog.deleteLater()
    quality_repository.save_timing_reference(reference)

    workspace = _workspace(controller)
    try:
        index = workspace.timing_reference_combo.findData(
            reference.timing_reference_id
        )
        workspace.timing_reference_combo.setCurrentIndex(index)
        capture, _direction, error = workspace._collect_acquisition_capture()
        assert error is None
        assert capture is not None
        assert capture.timing_reference_id == reference.timing_reference_id
        # The binding is recorded but the evidence verdict is honest:
        # a manual declaration can never produce valid=True.
        assert capture.timing_reference_valid is None

        controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "seat.txt")
        record = controller.commit_pending(_assignment(acquisition=capture))
    finally:
        _close(workspace)

    report = quality_repository.latest_report(record.measurement_id)
    assert report is not None
    assert report.timing_reference.status == "UNKNOWN"
    assert report.capability("common_timing").decision == "UNKNOWN"


def test_timing_reference_dialog_rejects_invalid_input(qapp) -> None:
    from htdt.measurement_authority_dialogs import TimingReferenceDialog

    dialog = TimingReferenceDialog()
    # persistent scope without a signal-path identity fails closed.
    dialog.scope_combo.setCurrentIndex(
        dialog.scope_combo.findData('persistent')
    )
    with pytest.raises(ValueError, match='信号パス'):
        dialog.build_record()
    # shared_clock without both clock identities fails closed.
    dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData('unknown'))
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('shared_clock')
    )
    with pytest.raises(ValueError, match='クロック'):
        dialog.build_record()
    assert dialog.record is None
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Acoustic level calibration — onboarding SPL readiness


def test_level_calibration_dialog_registers_and_onboarding_leaves_manual(
    qapp, tmp_path: Path
) -> None:
    from htdt.measurement_authority_dialogs import LevelCalibrationDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    # Before registration the SPL step can only be manual.
    steps = evaluate_instrument_onboarding(
        context=None,
        level_calibrations=quality_repository.list_level_calibrations(),
        plan_count=0,
    )
    spl = next(step for step in steps if step.key == 'spl_readiness')
    assert spl.status == 'manual'

    dialog = LevelCalibrationDialog(views=(), contexts=())
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('acoustic_calibrator')
    )
    dialog.instrument_identity_edit.setText('sc-05 sn-1234')
    dialog.instrument_profile_edit.setText('cal-session-2026-10-04')
    dialog.input_device_edit.setText('USB Audio (UMIK-1)')
    dialog.reference_level_edit.setText('94.0')
    dialog.reference_frequency_edit.setText('1000')
    dialog.scope_combo.setCurrentIndex(
        dialog.scope_combo.findData('instrument')
    )
    dialog.input_path_combo.setEditText('umik-1:usb-in:ch0')
    calibration = dialog.build_record()
    dialog.deleteLater()

    quality_repository.save_level_calibration(calibration)

    # Consumer: the onboarding SPL-readiness step can finally complete.
    steps = evaluate_instrument_onboarding(
        context=None,
        level_calibrations=quality_repository.list_level_calibrations(),
        plan_count=0,
    )
    spl = next(step for step in steps if step.key == 'spl_readiness')
    assert spl.status == 'ready'
    assert 'acoustic_calibrator' in spl.detail


def test_level_calibration_dialog_rejects_missing_scope(qapp) -> None:
    from htdt.measurement_authority_dialogs import LevelCalibrationDialog

    dialog = LevelCalibrationDialog(views=(), contexts=())
    dialog.method_combo.setCurrentIndex(
        dialog.method_combo.findData('acoustic_calibrator')
    )
    dialog.reference_level_edit.setText('94.0')
    dialog.reference_frequency_edit.setText('1000')
    # instrument scope without the required pair fails closed.
    dialog.scope_combo.setCurrentIndex(
        dialog.scope_combo.findData('instrument')
    )
    with pytest.raises(ValueError, match='機器識別子'):
        dialog.build_record()
    assert dialog.record is None
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Stimulus profile + excitation asset — file-picker upload surface


def _write_wav(path: Path) -> bytes:
    frames = struct.pack('<8h', 0, 1000, -1000, 500, -500, 250, -250, 0)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(frames)
    return path.read_bytes()


def test_stimulus_profile_dialog_uploads_asset_and_registers(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htdt import file_dialog_memory
    from htdt.measurement_authority_dialogs import StimulusProfileDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    dialog = StimulusProfileDialog(controller, views=())
    stim = tmp_path / 'sweep.wav'
    payload = _write_wav(stim)
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: (str(stim), '')),
    )

    dialog._pick_excitation_file()

    # The bytes landed in the managed store with verified metadata.
    assets = quality_repository.list_excitation_assets(
        controller.document_id
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset.filename == 'sweep.wav'
    assert asset.sha256 == sha256(payload).hexdigest()
    assert asset.byte_length == len(payload)
    # WAV header metadata was honestly probed, not declared.
    assert asset.format == 'wav'
    assert asset.sample_rate_hz == 48000.0
    assert asset.channel_count == 1
    # The pick auto-selects the persisted asset in the combo.
    assert dialog.asset_combo.currentData() == asset.excitation_asset_id

    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData('log_sweep'))
    dialog.intent_combo.setCurrentIndex(
        dialog.intent_combo.findData('measurement')
    )
    profile = dialog.build_record()
    dialog.deleteLater()
    quality_repository.save_stimulus_profile(profile)

    # Consumer: the profile listing resolves the bound asset authority.
    profiles = quality_repository.list_stimulus_profiles(
        controller.document_id
    )
    assert len(profiles) == 1
    assert profiles[0].excitation_asset is not None
    assert (
        profiles[0].excitation_asset.excitation_asset_id
        == asset.excitation_asset_id
    )


def test_stimulus_profile_dialog_rejects_bad_numbers(qapp, tmp_path: Path) -> None:
    from htdt.measurement_authority_dialogs import StimulusProfileDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, _quality_repository = _controller(
        scene_repository, revision.document_id
    )
    dialog = StimulusProfileDialog(controller, views=())
    dialog.duration_edit.setText('not-a-number')
    with pytest.raises(ValueError, match='継続時間'):
        dialog.build_record()
    assert dialog.record is None
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Routing profile — the combo is no longer （未選択）-only


def test_routing_profile_dialog_registers_and_combo_populates(
    qapp, tmp_path: Path
) -> None:
    from htdt.measurement_authority_dialogs import RoutingProfileDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )

    dialog = RoutingProfileDialog(controller)
    # The derive affordance pre-fills one entry per speaker role.
    assert dialog.entries_table.rowCount() >= 1
    # No acquisition context declares an output device yet — the operator
    # names the output path the map was verified under for every entry.
    for row in range(dialog.entries_table.rowCount()):
        dialog.entries_table.item(row, 0).setText('USB Audio (HDMI-AVR)')
    verification_combo = dialog.entries_table.cellWidget(0, 4)
    verification_combo.setCurrentIndex(
        verification_combo.findData('verified')
    )
    profile = dialog.build_record()
    dialog.deleteLater()

    quality_repository.save_routing_profile(profile)
    assert profile.document_id == controller.document_id
    assert profile.scene_revision_id == revision.revision_id

    # Consumer 1: document-scoped listing now returns the authority.
    listed = quality_repository.list_routing_profiles(
        document_id=controller.document_id
    )
    assert [entry.routing_profile_id for entry in listed] == [
        profile.routing_profile_id
    ]

    # Consumer 2: the assignment combo finally offers a real row.
    workspace = _workspace(controller)
    try:
        index = workspace.routing_profile_combo.findData(
            profile.routing_profile_id
        )
        assert index > 0
        assert '（未選択）' not in workspace.routing_profile_combo.itemText(index)
    finally:
        _close(workspace)


def test_routing_profile_dialog_requires_a_row(qapp, tmp_path: Path) -> None:
    from htdt.measurement_authority_dialogs import RoutingProfileDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, _quality_repository = _controller(
        scene_repository, revision.document_id
    )
    dialog = RoutingProfileDialog(controller)
    while dialog.entries_table.rowCount():
        dialog.entries_table.removeRow(0)
        del dialog._row_speakers[0]
    with pytest.raises(ValueError, match='チャンネルマップ'):
        dialog.build_record()
    assert dialog.record is None
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Dataset level reference — derived binding re-derives the report


def test_dataset_level_reference_registers_and_report_resolves(
    qapp, tmp_path: Path
) -> None:
    from htdt.measurement_authority_dialogs import DatasetLevelReferenceDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    record, dataset = _save_measurement_directly(
        measurement_repository, revision, 'm-1'
    )
    # A measurement-scope calibration over this measurement.
    dialog_cal = None
    from htdt.measurement_authority_dialogs import LevelCalibrationDialog

    views = controller.measurement_views()
    dialog_cal = LevelCalibrationDialog(views=views, contexts=())
    dialog_cal.method_combo.setCurrentIndex(
        dialog_cal.method_combo.findData('acoustic_calibrator')
    )
    dialog_cal.instrument_identity_edit.setText('sc-05 sn-1234')
    dialog_cal.instrument_profile_edit.setText('cal-session-2026-10-04')
    dialog_cal.input_device_edit.setText('USB Audio (UMIK-1)')
    dialog_cal.reference_level_edit.setText('94.0')
    dialog_cal.reference_frequency_edit.setText('1000')
    dialog_cal.scope_combo.setCurrentIndex(
        dialog_cal.scope_combo.findData('measurement')
    )
    m_index = dialog_cal.measurement_combo.findData(record.measurement_id)
    assert m_index >= 0
    dialog_cal.measurement_combo.setCurrentIndex(m_index)
    calibration = dialog_cal.build_record()
    dialog_cal.deleteLater()
    quality_repository.save_level_calibration(calibration)

    # The covering acquisition context the applicability replay requires.
    quality_repository.save_acquisition_context(
        build_acquisition_context(
            source_kind='native',
            subject_measurement_ids=(record.measurement_id,),
        )
    )

    view = next(
        entry
        for entry in controller.measurement_views()
        if entry.measurement_id == record.measurement_id
    )
    dialog = DatasetLevelReferenceDialog(
        view, dataset, quality_repository.list_level_calibrations()
    )
    dialog.kind_combo.setCurrentIndex(
        dialog.kind_combo.findData('absolute_spl')
    )
    reference = dialog.build_record()
    dialog.deleteLater()
    quality_repository.save_dataset_level_reference(reference)

    # Consumer 1: the pinned reference resolves for the dataset.
    assert (
        quality_repository.get_dataset_level_reference(dataset.dataset_id)
        == reference
    )
    # Consumer 2: re-derivation seals the pin and resolves absolute SPL.
    report = controller.reproduce_quality_report(record.measurement_id)
    assert report is not None
    assert report.level_reference is not None
    assert report.level_reference.level_reference_id == reference.level_reference_id
    assert report.capability('absolute_spl').decision == 'ALLOWED'


def test_dataset_level_reference_non_absolute_records_semantics(
    qapp, tmp_path: Path
) -> None:
    """A non-absolute declaration is honest evidence too — it needs no
    calibration and the producer reports the dataset's semantics as bound."""
    from htdt.measurement_authority_dialogs import DatasetLevelReferenceDialog

    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    record, dataset = _save_measurement_directly(
        measurement_repository, revision, 'm-1'
    )
    view = next(
        entry
        for entry in controller.measurement_views()
        if entry.measurement_id == record.measurement_id
    )
    dialog = DatasetLevelReferenceDialog(view, dataset, ())
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData('dbfs'))
    reference = dialog.build_record()
    dialog.deleteLater()
    quality_repository.save_dataset_level_reference(reference)

    report = controller.reproduce_quality_report(record.measurement_id)
    assert report is not None
    assert report.level_reference is not None
    # dbfs is an honest declaration — absolute SPL stays BLOCKED.
    assert report.capability('absolute_spl').decision == 'BLOCKED'


# ---------------------------------------------------------------------------
# Workspace affordances — cancel persists nothing


def test_registration_cancel_persists_nothing(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htdt import measurement_authority_dialogs

    scene_repository, revision = _saved_f1(tmp_path)
    controller, _measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    workspace = _workspace(controller)
    try:
        monkeypatch.setattr(
            measurement_authority_dialogs.TimingReferenceDialog,
            'exec',
            lambda self: 0,
        )
        workspace._register_timing_reference()
        assert quality_repository.list_timing_references() == ()

        monkeypatch.setattr(
            measurement_authority_dialogs.LevelCalibrationDialog,
            'exec',
            lambda self: 0,
        )
        workspace._register_level_calibration()
        assert quality_repository.list_level_calibrations() == ()

        monkeypatch.setattr(
            measurement_authority_dialogs.RoutingProfileDialog,
            'exec',
            lambda self: 0,
        )
        workspace._register_routing_profile()
        assert (
            quality_repository.list_routing_profiles(
                document_id=controller.document_id
            )
            == ()
        )
    finally:
        _close(workspace)


def test_workspace_level_reference_row_states(
    qapp, tmp_path: Path
) -> None:
    """The lifecycle card surfaces bound state honestly: unregistered →
    the affordance is live; bound → read-only label."""
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    record, dataset = _save_measurement_directly(
        measurement_repository, revision, 'm-1'
    )
    workspace = _workspace(controller)
    try:
        view = next(
            entry
            for entry in controller.measurement_views()
            if entry.measurement_id == record.measurement_id
        )
        workspace._update_level_reference_row(view)
        assert '未登録' in workspace.level_reference_label.text()
        assert workspace.level_reference_button.isEnabled()

        from htdt.cad_measurement_authorities import (
            build_dataset_level_reference,
        )

        reference = build_dataset_level_reference(
            measurement_id=record.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            level_reference_kind='spl_uncalibrated',
        )
        quality_repository.save_dataset_level_reference(reference)
        workspace._update_level_reference_row(view)
        assert 'spl_uncalibrated' in workspace.level_reference_label.text() or (
            'SPL' in workspace.level_reference_label.text()
        )
        assert not workspace.level_reference_button.isEnabled()
    finally:
        _close(workspace)
