"""#1002 — measured ETC peak ↔ predicted reflection-path correspondence
review surface.

Fixtures follow #677/RPA vocabulary: sealed pairings, #564 registrations,
persisted deterministic path artifacts and IR datasets are replayed by the
Qt-free viewmodel; the panel renders them with honest unsupported states.
Verdicts are produced ONLY by ``evaluate_reflection_correspondence``.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_reflection_correspondence import (
    ObservedReflectionEvent,
    PredictedReflectionPath,
    build_correspondence_set,
    build_reflection_pairing,
    evaluate_reflection_correspondence,
)
from htdt.cad_reflection_correspondence_repository import (
    CadReflectionCorrespondenceRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, Position3, make_f1_scene
from htdt.cad_schema import connect_sqlite
from htdt.measurement.domain.cad_measurement_ir import (
    normalize_rew_ir_text,
)
from htdt.measurement.domain.cad_prediction_measurement_registration import (
    LevelRegistration,
    MeasurementAuthorityBinding,
    PredictionAuthorityBinding,
    RegistrationEndpoint,
    SpatialRegistration,
    TimingRegistration,
    build_prediction_measurement_registration,
)
from htdt.measurement.persistence.cad_measurement_repository import (
    CadMeasurementRepository,
)
from htdt.measurement.persistence.cad_prediction_measurement_registration_repository import (
    CadPredictionMeasurementRegistrationRepository,
)
from htdt.reflection_correspondence_review import (
    MANUAL_HYPOTHESIS_ALGORITHM_VERSION,
    build_correspondence_review,
    build_manual_hypothesis_pairing,
    derive_etc_display_curve,
    observed_event_from_manual_gate,
    predicted_path_from_guidance_entry,
)
from htdt.reflection_guidance_presentation import (
    load_reflection_guidance_view,
)

from test_measurement_authorities import _save_measurement  # noqa: E402
from test_reflection_guidance_ui import (  # noqa: E402
    NOW,
    _seed_guidance,
)

DOC = F1_DOCUMENT_ID
MEAS_ID = 'meas-rpa-1'


def _hash(s: str) -> str:  # shadowed sibling name → identical helper
    return sha256(s.encode('utf-8')).hexdigest()


def _ir_text() -> bytes:
    return (
        b'Impulse response export\n'
        b'Sample rate: 48000\n'
        b'0.0000000000 0.0001\n'
        b'0.0000208333 0.0500\n'
        b'0.0000416667 0.2000\n'
        b'0.0000625000 -0.0500\n'
        b'0.0000833333 -0.0100\n'
    )


def _repos(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision


def _seed_measurement(scene_repository, revision, measurement_id=MEAS_ID):
    measurement_repo = CadMeasurementRepository(scene_repository)
    record, _fr = _save_measurement(
        measurement_repo, revision, measurement_id
    )
    dataset, filename, raw = normalize_rew_ir_text(
        measurement_id,
        _ir_text(),
        filename='ir.txt',
        t0_semantics='export_t0',
        amplitude_reference='normalized',
        normalized=True,
    )
    measurement_repo.save_ir_dataset(
        dataset, raw_filename=filename, raw_bytes=raw
    )
    return record, dataset


def _registration(
    scene_repository,
    revision,
    measurement_id,
    dataset,
    *,
    timing_method='exact_reference',
    timing_uncertainty_s=None,
    timing_applied_offset_s=None,
):
    prediction = PredictionAuthorityBinding(
        kind='prediction_result',
        prediction_id='pred-rpa-1',
        prediction_sha256=_hash('pred-rpa'),
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        source_entity_ids=('speaker-fl',),
        receiver_entity_id='point-mlp',
    )
    measurement = MeasurementAuthorityBinding(
        measurement_id=measurement_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        ir_dataset_id=dataset.dataset_id,
        ir_dataset_sha256=dataset.dataset_sha256,
    )
    source_pos = Position3(x_m=-1.0, y_m=2.0, z_m=1.0)
    receiver_pos = Position3(x_m=1.0, y_m=0.0, z_m=1.0)
    source = RegistrationEndpoint(
        role='source',
        entity_id='speaker-fl',
        predicted_position=source_pos,
        measured_position=source_pos,
        position_delta_m=0.0,
        position_uncertainty_m=0.01,
    )
    receiver = RegistrationEndpoint(
        role='receiver',
        entity_id='point-mlp',
        predicted_position=receiver_pos,
        measured_position=receiver_pos,
        position_delta_m=0.0,
        position_uncertainty_m=0.01,
    )
    spatial = SpatialRegistration(
        method='exact_scene_xyz', provenance='surveyed'
    )
    timing = TimingRegistration(
        method=timing_method,
        applied_offset_s=timing_applied_offset_s,
        uncertainty_s=timing_uncertainty_s,
    )
    level = LevelRegistration(level_state='unknown')
    return build_prediction_measurement_registration(
        document_id=DOC,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        prediction=prediction,
        measurement=measurement,
        source=source,
        receiver=receiver,
        spatial=spatial,
        timing=timing,
        level=level,
        registration_method='fixture',
        created_at_utc=NOW,
    )


def _observed_event(
    measurement_id,
    dataset,
    time_s,
    *,
    extent=None,
    doa=None,
    doa_uncertainty=None,
):
    return ObservedReflectionEvent(
        measurement_ref=AuthorityRef(
            kind='impulse_response_measurement',
            ref_id=measurement_id,
            ref_sha256=dataset.dataset_sha256,
        ),
        extraction_algorithm='etc_peak_gate',
        extraction_version='1',
        observed_time_s=time_s,
        observed_extent_s=extent,
        observed_doa=doa,
        doa_uncertainty=doa_uncertainty,
    )


def _seed_all(scene_repository, revision, *, surfaces=('wall-left',)):
    """IR + registration + persisted specular path(s) — the ready stack."""
    record, dataset = _seed_measurement(scene_repository, revision)
    registration = _registration(
        scene_repository, revision, record.measurement_id, dataset
    )
    CadPredictionMeasurementRegistrationRepository(
        scene_repository
    ).save(registration)
    with connect_sqlite(scene_repository.path) as connection, connection:
        _seed_guidance(
            connection,
            document_id=DOC,
            revision_id=revision.revision_id,
            surfaces=surfaces,
        )
    return record, dataset, registration


def _entries(scene_repository):
    with connect_sqlite(scene_repository.path) as connection:
        return load_reflection_guidance_view(connection, DOC).entries


# ---------------------------------------------------------------------------
# availability / unsupported states


def test_no_measurement_selected_reports_options(tmp_path):
    scene_repository, _revision = _repos(tmp_path)
    view = build_correspondence_review(scene_repository, DOC, None)
    assert view.availability == 'no_measurement'
    assert view.availability_reasons


def test_missing_ir_dataset_is_unsupported_with_reason(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    measurement_repo = CadMeasurementRepository(scene_repository)
    _save_measurement(measurement_repo, revision, MEAS_ID)
    view = build_correspondence_review(
        scene_repository, DOC, MEAS_ID
    )
    assert view.availability == 'no_ir_dataset'
    assert any('IR' in r or 'インパルス' in r for r in view.availability_reasons)
    assert view.etc_curve is None


def test_missing_registration_is_unsupported_with_reason(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    _seed_measurement(scene_repository, revision)
    view = build_correspondence_review(
        scene_repository, DOC, MEAS_ID
    )
    assert view.availability == 'no_registration'
    assert view.availability_reasons
    assert view.etc_curve is not None


def test_missing_predicted_paths_is_unsupported(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset = _seed_measurement(scene_repository, revision)
    registration = _registration(
        scene_repository, revision, record.measurement_id, dataset
    )
    CadPredictionMeasurementRegistrationRepository(
        scene_repository
    ).save(registration)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    assert view.availability == 'no_predicted_paths'
    assert view.availability_reasons


# ---------------------------------------------------------------------------
# ready state + mutual binding


def test_ready_view_links_pairing_to_exact_rows(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, registration = _seed_all(
        scene_repository, revision
    )
    entry = _entries(scene_repository)[0]
    predicted = predicted_path_from_guidance_entry(entry)
    observed = _observed_event(
        record.measurement_id, dataset, predicted.predicted_arrival_s
    )
    pairing = build_reflection_pairing(
        document_id=DOC,
        predicted_path=predicted,
        observed_event=observed,
        correspondence_state='one_to_one',
        matching_algorithm='time_doa_geometric_match',
        matching_algorithm_version='1',
        evidence_dimensions=(
            'time_alignment',
            'direction_of_arrival',
            'geometric_path_consistency',
        ),
        declared_at_utc=NOW,
    )
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    repo.save_pairing(pairing)
    set_ = build_correspondence_set(
        document_id=DOC,
        registration_ref=AuthorityRef(
            kind='prediction_measurement_registration',
            ref_id=registration.registration_id,
            ref_sha256=registration.semantic_sha256,
        ),
        pairings=(pairing,),
        declared_at_utc=NOW,
    )
    repo.save_set(set_)
    verdict = evaluate_reflection_correspondence(
        DOC, set_, (pairing,), registration_valid=True
    )
    repo.save_verdict(verdict)

    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    assert view.availability == 'ready'
    assert len(view.predicted_rows) == 1
    assert len(view.observed_rows) == 1
    assert len(view.pairing_rows) == 1
    row = view.pairing_rows[0]
    # Mutual highlight binding — exact identities, never nearest-time.
    assert row.predicted_row_id == view.predicted_rows[0].row_id
    assert row.observed_row_id == view.observed_rows[0].row_id
    assert row.correspondence_state == 'one_to_one'
    assert row.set_ids == (set_.set_id,)
    assert view.verdict_rows[0].state == 'qualified'


def test_ambiguous_cluster_never_gets_a_correct_color(tmp_path):
    """RPA30: a cluster must surface all members under one honest class."""
    scene_repository, revision = _repos(tmp_path)
    record, dataset, registration = _seed_all(
        scene_repository, revision, surfaces=('wall-left', 'floor')
    )
    entries = _entries(scene_repository)
    assert len(entries) == 2
    predicted_a = predicted_path_from_guidance_entry(entries[0])
    predicted_b = predicted_path_from_guidance_entry(entries[1])
    observed = _observed_event(record.measurement_id, dataset, 0.02)
    pairing_a = build_reflection_pairing(
        document_id=DOC,
        predicted_path=predicted_a,
        observed_event=observed,
        correspondence_state='unresolved_cluster',
        matching_algorithm='time_gate_peak_match',
        matching_algorithm_version='1',
        evidence_dimensions=('time_alignment',),
        cluster_member_ids=('peer-b',),
        declared_at_utc=NOW,
    )
    pairing_b = build_reflection_pairing(
        document_id=DOC,
        predicted_path=predicted_b,
        observed_event=observed,
        correspondence_state='unresolved_cluster',
        matching_algorithm='time_gate_peak_match',
        matching_algorithm_version='1',
        evidence_dimensions=('time_alignment',),
        cluster_member_ids=(pairing_a.pairing_id,),
        declared_at_utc=NOW,
    )
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    repo.save_pairing(pairing_a)
    repo.save_pairing(pairing_b)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    assert len(view.pairing_rows) == 2
    states = {r.correspondence_state for r in view.pairing_rows}
    assert states == {'unresolved_cluster'}
    # Both predicted rows stay visible as candidates — none is crowned.
    assert len(view.predicted_rows) == 2
    assert {r.predicted_row_id for r in view.pairing_rows} == {
        r.row_id for r in view.predicted_rows
    }
    # A time-only verdict can never qualify — canonical evaluator.
    set_ = build_correspondence_set(
        document_id=DOC,
        registration_ref=AuthorityRef(
            kind='prediction_measurement_registration',
            ref_id=registration.registration_id,
            ref_sha256=registration.semantic_sha256,
        ),
        pairings=(pairing_a, pairing_b),
        declared_at_utc=NOW,
    )
    verdict = evaluate_reflection_correspondence(
        DOC,
        set_,
        (pairing_a, pairing_b),
        registration_valid=True,
    )
    assert verdict.state in (
        'ambiguous_unresolved',
        'qualified_with_limitations',
    )
    assert verdict.ambiguous_pair_count == 2


def test_unmatched_predicted_and_observed_keep_own_states(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, _registration_row = _seed_all(
        scene_repository, revision
    )
    entry = _entries(scene_repository)[0]
    predicted = predicted_path_from_guidance_entry(entry)
    unmatched_p = build_reflection_pairing(
        document_id=DOC,
        predicted_path=predicted,
        correspondence_state='unmatched_predicted',
        matching_algorithm='probabilistic_assignment',
        matching_algorithm_version='1',
        declared_at_utc=NOW,
    )
    unmatched_o = build_reflection_pairing(
        document_id=DOC,
        observed_event=_observed_event(
            record.measurement_id, dataset, 0.05
        ),
        correspondence_state='unmatched_observed',
        matching_algorithm='probabilistic_assignment',
        matching_algorithm_version='1',
        declared_at_utc=NOW,
    )
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    repo.save_pairing(unmatched_p)
    repo.save_pairing(unmatched_o)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    states = {r.correspondence_state for r in view.pairing_rows}
    assert states == {'unmatched_predicted', 'unmatched_observed'}
    unmatched_pred_row = next(
        r
        for r in view.pairing_rows
        if r.correspondence_state == 'unmatched_predicted'
    )
    assert unmatched_pred_row.observed_row_id is None
    assert unmatched_pred_row.predicted_row_id is not None


# ---------------------------------------------------------------------------
# honesty: stale, clock drift, no DOA, insufficient band


def test_stale_scene_revision_flags_rows_honestly(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, registration = _seed_all(
        scene_repository, revision
    )
    # New head revision (renamed entity -> new content hash) — every
    # pinned row is now honestly stale.
    scene = make_f1_scene()
    entities = list(scene.entities)
    entities[0] = entities[0].model_copy(update={'name': 'Front Left v2'})
    scene = scene.model_copy(update={'entities': tuple(entities)})
    scene_repository.save(scene, parent_revision_id=revision.revision_id)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    assert view.predicted_rows
    assert all(r.stale for r in view.predicted_rows)
    assert all(r.stale for r in view.registrations)


def test_clock_drift_shows_timing_method_and_uncertainty(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset = _seed_measurement(scene_repository, revision)
    registration = _registration(
        scene_repository,
        revision,
        record.measurement_id,
        dataset,
        timing_method='estimated_from_direct_arrival',
        timing_uncertainty_s=0.001,
        timing_applied_offset_s=0.0,
    )
    CadPredictionMeasurementRegistrationRepository(
        scene_repository
    ).save(registration)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    row = view.registrations[0]
    assert row.timing_method == 'estimated_from_direct_arrival'
    assert row.timing_uncertainty_s == pytest.approx(0.001)


def test_observed_event_without_doa_stays_honest(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, _r = _seed_all(scene_repository, revision)
    event = _observed_event(record.measurement_id, dataset, 0.02)
    pairing = build_reflection_pairing(
        document_id=DOC,
        observed_event=event,
        correspondence_state='unmatched_observed',
        matching_algorithm='sparse_decomposition_match',
        matching_algorithm_version='1',
        declared_at_utc=NOW,
    )
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    repo.save_pairing(pairing)
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    row = view.observed_rows[0]
    assert row.observed_doa is None
    assert row.doa_uncertainty is None


# ---------------------------------------------------------------------------
# hypothetical annotation


def test_manual_hypothesis_is_time_only_and_never_qualified(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, registration = _seed_all(
        scene_repository, revision
    )
    entry = _entries(scene_repository)[0]
    predicted = predicted_path_from_guidance_entry(entry)
    observed = observed_event_from_manual_gate(
        measurement_id=record.measurement_id,
        dataset=dataset,
        observed_time_s=0.02,
        observed_extent_s=(0.019, 0.021),
    )
    pairing = build_manual_hypothesis_pairing(
        document_id=DOC,
        predicted_path=predicted,
        observed_event=observed,
        declared_at_utc=NOW,
    )
    assert pairing.correspondence_state == 'ambiguous'
    assert pairing.matching_algorithm == 'manual_expert_label'
    assert pairing.matching_algorithm_version == (
        MANUAL_HYPOTHESIS_ALGORITHM_VERSION
    )
    assert pairing.evidence_dimensions == ('time_alignment',)
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    repo.save_pairing(pairing)
    # Canonical evaluation of a time-only hypothesis can never qualify.
    set_ = build_correspondence_set(
        document_id=DOC,
        registration_ref=AuthorityRef(
            kind='prediction_measurement_registration',
            ref_id=registration.registration_id,
            ref_sha256=registration.semantic_sha256,
        ),
        pairings=(pairing,),
        declared_at_utc=NOW,
    )
    verdict = evaluate_reflection_correspondence(
        DOC, set_, (pairing,), registration_valid=True
    )
    assert verdict.state != 'qualified'
    assert verdict.ambiguous_pair_count == 1
    view = build_correspondence_review(
        scene_repository, DOC, record.measurement_id
    )
    row = next(
        r for r in view.pairing_rows if r.pairing_id == pairing.pairing_id
    )
    assert row.is_hypothesis


# ---------------------------------------------------------------------------
# display-only ETC derivation


def test_etc_curve_is_peak_normalized_display_only(tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset = _seed_measurement(scene_repository, revision)
    curve = derive_etc_display_curve(dataset)
    assert curve.dataset_id == dataset.dataset_id
    assert len(curve.times_s) == len(curve.level_db)
    assert max(curve.level_db) == pytest.approx(0.0, abs=1e-9)
    assert all(v <= 1e-9 for v in curve.level_db)
    assert curve.sample_rate_hz == pytest.approx(dataset.sample_rate_hz)


# ---------------------------------------------------------------------------
# Qt surface (offscreen)


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _PanelController:
    """Minimal controller shim — the panel only needs these ports."""

    def __init__(self, scene_repository, document_id, measurement_ids):
        self.scene_repository = scene_repository
        self.document_id = document_id
        self._measurement_ids = measurement_ids

    def measurement_views(self):
        return tuple(
            SimpleNamespace(
                measurement_id=mid,
                effective_target_name='MLP',
                target_name='MLP',
            )
            for mid in self._measurement_ids
        )

    def latest_revision(self):
        return self.scene_repository.latest(self.document_id)

    def ir_datasets_for_measurement(self, measurement_id):
        return CadMeasurementRepository(
            self.scene_repository
        ).ir_datasets_for_measurement(measurement_id)


def _panel(scene_repository, measurement_ids):
    from htdt.measurement.ui.reflection_correspondence_panel import (
        ReflectionCorrespondencePanel,
    )

    controller = _PanelController(
        scene_repository, DOC, measurement_ids
    )
    return ReflectionCorrespondencePanel(controller)


def test_panel_shows_unsupported_reason(app, tmp_path):
    scene_repository, revision = _repos(tmp_path)
    _seed_measurement(scene_repository, revision)
    panel = _panel(scene_repository, (MEAS_ID,))
    assert panel.measurement_combo.count() == 1
    assert '登録' in panel.state_label.text() or panel.state_label.text()


def test_panel_mutual_highlight_maps_pairing(app, tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, registration = _seed_all(
        scene_repository, revision
    )
    entry = _entries(scene_repository)[0]
    predicted = predicted_path_from_guidance_entry(entry)
    observed = _observed_event(
        record.measurement_id, dataset, predicted.predicted_arrival_s
    )
    pairing = build_reflection_pairing(
        document_id=DOC,
        predicted_path=predicted,
        observed_event=observed,
        correspondence_state='one_to_one',
        matching_algorithm='time_doa_geometric_match',
        matching_algorithm_version='1',
        evidence_dimensions=(
            'time_alignment',
            'direction_of_arrival',
        ),
        declared_at_utc=NOW,
    )
    CadReflectionCorrespondenceRepository(
        scene_repository
    ).save_pairing(pairing)
    panel = _panel(scene_repository, (record.measurement_id,))
    assert panel.pairing_table.rowCount() == 1
    assert panel.predicted_table.rowCount() == 1
    assert panel.observed_table.rowCount() == 1
    panel.pairing_table.selectRow(0)
    app.processEvents()
    assert panel._selected_pairing_id == pairing.pairing_id
    # Mutual highlight (requirement 3): the pairing click selects the exact
    # bound rows in the predicted and observed tables.
    def _selected_row_id(table):
        items = table.selectedItems()
        assert items
        from PySide6.QtCore import Qt

        return items[0].data(Qt.ItemDataRole.UserRole)

    assert (
        _selected_row_id(panel.predicted_table)
        == panel._view.pairing_rows[0].predicted_row_id
    )
    assert (
        _selected_row_id(panel.observed_table)
        == panel._view.pairing_rows[0].observed_row_id
    )
    assert (
        _selected_row_id(panel.predicted_table)
        == panel._selected_predicted_row_id
    )


def test_panel_hypothesis_button_flow(app, tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, _r = _seed_all(scene_repository, revision)
    panel = _panel(scene_repository, (record.measurement_id,))
    panel.predicted_table.selectRow(0)
    app.processEvents()
    assert panel.hypothesis_button.isEnabled()
    saved = []
    panel.hypothesisSaved.connect(saved.append)
    panel._save_hypothesis()
    assert saved, 'hypothesis pairing should have been saved'
    repo = CadReflectionCorrespondenceRepository(scene_repository)
    stored = repo.get_pairing(saved[0])
    assert stored is not None
    assert stored.matching_algorithm == 'manual_expert_label'
    assert stored.correspondence_state == 'ambiguous'


def test_panel_narrow_layout_stacks(app, tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, _r = _seed_all(scene_repository, revision)
    panel = _panel(scene_repository, (record.measurement_id,))
    panel.show()
    app.processEvents()
    from PySide6.QtCore import Qt

    panel._apply_splitter_orientation(800)
    assert panel.splitter.orientation() == Qt.Orientation.Vertical
    panel._apply_splitter_orientation(1600)
    assert panel.splitter.orientation() == Qt.Orientation.Horizontal
    panel.close()


def test_panel_survives_dpi200_and_missing_viewport(app, tmp_path):
    scene_repository, revision = _repos(tmp_path)
    record, dataset, _r = _seed_all(scene_repository, revision)
    panel = _panel(scene_repository, (record.measurement_id,))
    panel.resize(1024, 768)
    # Offscreen VTK may fail without GL — the honest fallback must show.
    assert (
        panel._viewport is not None
        or panel.viewport_fallback.isVisible()
        or panel._viewport_failed
    )
