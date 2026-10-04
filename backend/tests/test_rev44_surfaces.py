"""REV44 — production surfaces for prediction matrices and manual attestations.

Before REV44 the only writers for three features lived in script lanes:
persisted solver results could never become prediction providers in the
app, ``create_matrix``/``run_matrix`` were dead, and no path could mint a
manual applicability attestation — so the 行列 UI permanently showed
「行列なし」 and manual mode could only fail. These tests exercise the new
in-app surfaces end to end against the real persisted solver stack.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest  # noqa: E402

from scripts.run_r130a_candidate_wave_execution import (  # noqa: E402
    _fixture as r130_fixture,
)
from test_cad_applicability_gate import _fixture as _gate_fixture  # noqa: E402

from htdt.cad_applicability import (  # noqa: E402
    APPLICABILITY_MANUAL_EVALUATOR_ID,
    evaluate_applicability,
)
from htdt.cad_candidate_wave_execution import (  # noqa: E402
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
)
from htdt.cad_acoustic_solver_result import (  # noqa: E402
    AcousticSolverObservableArtifact,
    build_acoustic_solver_result_envelope,
)
from htdt.cad_prediction_registration import PredictionAuthorityLane  # noqa: E402
from htdt.prediction_matrix_service import PredictionMatrixService  # noqa: E402
from htdt.optimization_validation_controller import (  # noqa: E402
    ApplicabilityAttestationDialog,
)
from htdt.optimization_workflow_controller import (  # noqa: E402
    OptimizationWorkflowController,
)
from htdt.prediction_matrix_dialog import PredictionMatrixDialog  # noqa: E402
from htdt.room_prediction import (  # noqa: E402
    RoomPredictionController,
    RoomPredictionPanel,
)
from htdt.room_workspace import RoomWorkspaceController  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _persist_result(fixture: dict):
    """Persist one complex-pressure solver result through the real models.

    Mirrors the executor's persisted payload layout (schema-bound artifact +
    execution provenance + sealed envelope) without running PFFDTD.
    """
    store = fixture['store']
    snapshot = fixture['snapshot']
    request = fixture['request']
    dispatch = fixture['dispatch']
    receivers = tuple(snapshot.receivers)
    source = snapshot.sources[0]
    wave_bindings = tuple(
        item
        for item in snapshot.wave_source_excitation_bindings
        if item.source_entity_id == source.source_entity_id
    )
    wave_binding = wave_bindings[0] if wave_bindings else None
    domain = request.requested_frequency_domain
    axis = [
        domain.minimum_hz,
        (domain.minimum_hz + domain.maximum_hz) / 2.0,
        domain.maximum_hz,
    ]
    execution_id = f'r130a-test:{dispatch.binding_id}'
    source_authority = {
        'r110_compiled_source_sha256': source.r110_compiled_source_sha256,
    }
    if wave_binding is not None:
        source_authority['wave_excitation_binding_sha256'] = (
            wave_binding.semantic_sha256
        )
    artifact_payload = {
        'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        'quantity_type': 'complex_pressure',
        'complex_representation': {
            'form': 'cartesian_real_imag',
            'phasor_convention': 'exp(-i*omega*t)',
            'analysis_fourier_kernel': 'exp(+i*omega*t)',
        },
        'receiver_identity_order': [
            {
                'receiver_id': item.receiver_id,
                'entity_id': item.entity_id,
                'position_m': [
                    float(item.world_position.x_m),
                    float(item.world_position.y_m),
                    float(item.world_position.z_m),
                ],
            }
            for item in receivers
        ],
        'frequency_axis_hz': list(axis),
        'units': 'Pa',
        'valid_domain': domain.model_dump(mode='json'),
        'solver_execution_id': execution_id,
        'source_authority': source_authority,
        'pressure_real_pa': [
            [0.02 + 0.001 * index for index in range(len(axis))]
            for _ in receivers
        ],
        'pressure_imag_pa': [
            [0.01 + 0.001 * index for index in range(len(axis))]
            for _ in receivers
        ],
    }
    artifact_ref = store.put_json(
        'acoustic-solver-artifact',
        COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
        artifact_payload,
    )
    provenance_ref = store.put_json(
        'solver-execution-provenance',
        'htdt.r130a.candidate-execution-provenance-1',
        {
            'schema_version': (
                'htdt.r130a.candidate-execution-provenance-1'
            ),
            'execution_id': execution_id,
            'candidate_only': True,
            'production_solver_selected': False,
            'owned_room_evidence': False,
            'solver_implementation_ref': (
                dispatch.solver_implementation_ref.model_dump(mode='json')
            ),
            'solver_configuration_ref': (
                dispatch.solver_configuration_ref.model_dump(mode='json')
            ),
        },
    )
    envelope = build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id=execution_id,
        execution_provenance_ref=provenance_ref,
        artifacts=(
            AcousticSolverObservableArtifact(
                observable='complex_pressure',
                artifact_authority=artifact_ref,
                encoding_schema_ref=fixture['output_schema_ref'],
                valid_frequency_domain=domain,
            ),
        ),
        completed_at_utc=datetime.now(timezone.utc).isoformat(),
        artifact_manifest_resolver=(
            store.solver_artifact_manifest_resolver(
                encoding_schema_ref=fixture['output_schema_ref'],
            )
        ),
    )
    return fixture['result_repository'].save(envelope)


def _registered_provider(fixture: dict):
    """Persist a result and register it through the in-app lane."""
    envelope = _persist_result(fixture)
    lane = PredictionAuthorityLane(
        fixture['scene_repository'], authority_root=fixture['authority_root']
    )
    document_id = fixture['snapshot'].document_id
    entries = lane.list_registrable_results(document_id)
    entry = next(
        item for item in entries if item.result_id == envelope.result_id
    )
    assert entry.eligible is True, entry.reason
    assert entry.registered is False
    provider = lane.register_provider(envelope.result_id)
    return envelope, lane, provider


def test_lane_registers_persisted_result_and_room_lane_sees_it(tmp_path) -> None:
    """create→visible: a persisted solver result becomes a real provider."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    envelope, lane, provider = _registered_provider(fixture)
    document_id = fixture['snapshot'].document_id

    assert provider.evidence_state == 'candidate'
    assert provider.evidence_scope == 'unvalidated'
    assert provider.provider_id.startswith('r170a-provider:')

    # The provider replays from persisted bytes on reopen — the lane now
    # reports the result as registered and list_providers serves it back.
    entries = lane.list_registrable_results(document_id)
    entry = next(
        item for item in entries if item.result_id == envelope.result_id
    )
    assert entry.registered is True
    reopened = PredictionAuthorityLane(
        fixture['scene_repository'], authority_root=fixture['authority_root']
    )
    ids = {
        item.provider_id
        for item in reopened.provider_repository.list_providers(document_id)
    }
    assert provider.provider_id in ids

    # The same repository backs the room prediction options lane (#938):
    # the registered provider must appear as a runnable option.
    room = RoomWorkspaceController(fixture['scene_repository'], document_id)
    prediction = RoomPredictionController(
        fixture['scene_repository'],
        room,
        provider_repository=reopened.provider_repository,
    )
    receiver_entity_id = (
        provider.receiver_identities[0].receiver_binding.entity_id
    )
    options = prediction.prediction_options(
        receiver_entity_id, max_mode_hz=80.0
    )
    wave = next(
        option
        for option in options
        if option.model_key.endswith(provider.provider_id)
    )
    assert wave.state == 'READY'
    assert wave.runnable is True
    prediction.dispose()


def test_lane_fails_closed_when_authority_is_missing(tmp_path) -> None:
    """A persisted result whose sealed authority is gone cannot register."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    envelope = _persist_result(fixture)
    lane = PredictionAuthorityLane(
        fixture['scene_repository'], authority_root=fixture['authority_root']
    )
    document_id = fixture['snapshot'].document_id

    # Delete the artifact authority payload — revalidation now fails closed.
    artifact = envelope.artifacts[0]
    missing = fixture['store'].path_for(artifact.artifact_authority)
    assert missing.exists()
    missing.unlink()

    entries = lane.list_registrable_results(document_id)
    entry = next(
        item for item in entries if item.result_id == envelope.result_id
    )
    assert entry.eligible is False
    assert entry.reason
    with pytest.raises(ValueError):
        lane.register_provider(envelope.result_id)


def test_matrix_create_run_and_presentation(tmp_path) -> None:
    """run→results: a registered provider feeds create_matrix + run_matrix."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, _lane, provider = _registered_provider(fixture)
    scene_repository = fixture['scene_repository']
    document_id = fixture['snapshot'].document_id

    service = PredictionMatrixService(scene_repository, document_id)
    source_entity_id = provider.source_identity.source_binding.source_entity_id
    providers = {source_entity_id: provider}

    plan = service.creation_plan(providers)
    assert plan.problems == ()
    assert plan.source_entity_ids == (source_entity_id,)
    assert plan.frequency_axis_hz
    spec = service.create_matrix(
        source_entity_ids=plan.source_entity_ids,
        receiver_ids=plan.receiver_ids,
        providers=providers,
        frequency_axis_hz=plan.frequency_axis_hz,
        solver_implementation_ref=plan.solver_implementation_ref,
        valid_frequency_domain=plan.valid_frequency_domain,
    )

    # No run yet — the presentation must say unrun honestly (QUEUED cells,
    # no run state).
    presentation = service.matrix_presentation(spec.spec_id)
    assert presentation.run_state is None
    assert len(presentation.cells) == len(plan.receiver_ids)
    assert all(cell.state == 'QUEUED' for cell in presentation.cells)

    run = service.run_matrix(spec.spec_id, providers)
    assert run.state == 'READY'
    assert run.result_set_sha256

    presentation = service.matrix_presentation(spec.spec_id)
    assert presentation.run_state == 'READY'
    assert presentation.run_attempt == 1
    assert all(
        cell.state in ('READY', 'CACHED') for cell in presentation.cells
    )
    for cell in presentation.cells:
        transfer = service.cell_transfers(
            spec.spec_id,
            cell.matrix_source_id,
            cell.matrix_receiver_id,
        )
        assert transfer is not None
        _reference, frequencies, magnitudes, _phases = transfer
        assert len(frequencies) == len(plan.frequency_axis_hz)
        assert len(magnitudes) == len(frequencies)


def test_creation_plan_reports_unavailable_and_conflicting_inputs(tmp_path) -> None:
    """Honest gating: no providers → JA problem; wrong key → problem."""
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _envelope, _lane, provider = _registered_provider(fixture)
    service = PredictionMatrixService(
        fixture['scene_repository'], fixture['snapshot'].document_id
    )

    empty = service.creation_plan({})
    assert empty.problems == ('実行プロバイダーが選択されていません。',)

    # A provider keyed under an entity it is not bound to is reported, and
    # create_matrix refuses the same selection (no silent pass-through).
    bad_key = 'not-the-bound-source'
    plan = service.creation_plan({bad_key: provider})
    assert plan.problems
    with pytest.raises(ValueError):
        service.create_matrix(
            source_entity_ids=(bad_key,),
            receiver_ids=plan.receiver_ids or ('receiver-1',),
            providers={bad_key: provider},
            frequency_axis_hz=plan.frequency_axis_hz or (40.0, 60.0, 80.0),
            solver_implementation_ref=plan.solver_implementation_ref
            or provider.current_authority.solver_implementation_ref,
            valid_frequency_domain=plan.valid_frequency_domain
            or provider.valid_frequency_domain,
        )


def test_prediction_matrix_dialog_round_trip(tmp_path) -> None:
    """The dialog surface performs register→create→run end to end."""
    _app()
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _persist_result(fixture)
    lane = PredictionAuthorityLane(
        fixture['scene_repository'], authority_root=fixture['authority_root']
    )
    document_id = fixture['snapshot'].document_id
    room = RoomWorkspaceController(fixture['scene_repository'], document_id)
    controller = RoomPredictionController(
        fixture['scene_repository'],
        room,
        provider_repository=lane.provider_repository,
    )

    dialog = PredictionMatrixDialog(
        controller=controller, lane=lane, parent=None
    )

    # The solver-results tree lists the persisted result as 登録可.
    assert dialog.results.topLevelItemCount() == 1
    result_item = dialog.results.topLevelItem(0)
    assert result_item.text(3) == '登録可'

    # Register it through the button path.
    dialog.results.setCurrentItem(result_item)
    dialog._register_selected()
    assert dialog.providers.topLevelItemCount() >= 1
    provider_item = dialog.providers.topLevelItem(0)
    assert provider_item.data(0, Qt.ItemDataRole.UserRole)

    provider_item.setCheckState(0, Qt.CheckState.Checked)
    dialog._recompute_plan()
    assert not dialog._plan.problems
    assert dialog.create_button.isEnabled()

    # Create the matrix through the button path.
    dialog._create_matrix()
    assert dialog.run_button.isEnabled()

    # Run it through the button path.
    dialog._run_matrix()
    spec = dialog.matrix_service.repository.latest_spec(document_id)
    assert spec is not None
    runs = dialog.matrix_service.repository.run_history(spec.spec_id)
    assert runs and runs[-1].state == 'READY'

    dialog.close()
    dialog.deleteLater()
    controller.dispose()


def test_room_prediction_panel_exposes_matrix_manager(tmp_path, monkeypatch) -> None:
    """The 予測 panel wires 行列・プロバイダー管理 to the real dialog."""
    app = _app()
    fixture = r130_fixture(tmp_path, tmp_path / 'unused-pffdtd-upstream')
    _persist_result(fixture)
    lane = PredictionAuthorityLane(
        fixture['scene_repository'], authority_root=fixture['authority_root']
    )
    document_id = fixture['snapshot'].document_id
    room = RoomWorkspaceController(fixture['scene_repository'], document_id)
    controller = RoomPredictionController(
        fixture['scene_repository'],
        room,
        provider_repository=lane.provider_repository,
    )
    panel = RoomPredictionPanel(controller, prediction_lane=lane)

    opened = []
    monkeypatch.setattr(
        PredictionMatrixDialog,
        'exec',
        lambda self: opened.append(self) or QDialog.DialogCode.Rejected,
    )
    panel.matrix_manage_button.click()
    assert len(opened) == 1

    panel.close()
    panel.deleteLater()
    controller.dispose()
    app.processEvents()


def test_manual_attestation_unblocks_manual_mode(tmp_path) -> None:
    """attestation→manual passes: a persisted attestation satisfies 手動証拠."""
    env = _gate_fixture(tmp_path)
    spec = env.spec
    campaign = SimpleNamespace(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        campaign_id='campaign-1',
    )
    repository = env.repository.applicability_attestations

    _app()
    dialog = ApplicabilityAttestationDialog(
        campaign=campaign,
        code='geometry',
        repository=repository,
        parent=None,
    )
    dialog.actor.setText('田中 太郎')
    dialog.decision.setCurrentIndex(0)  # 合格
    dialog.reference.setText('測定メモ 2026-10-04')
    dialog.summary.setText('幾何モデルが実測の部屋形状と一致することを確認した')
    dialog._register()
    assert dialog.selected_attestation_id
    attestation = repository.get(dialog.selected_attestation_id)
    assert attestation is not None
    assert attestation.code == 'geometry'
    assert attestation.decision == 'pass'
    assert attestation.document_id == spec.document_id
    assert attestation.search_spec_sha256 == spec.search_spec_sha256
    assert attestation.actor == '田中 太郎'
    assert attestation.evidence()['reference'] == '測定メモ 2026-10-04'
    dialog.close()
    dialog.deleteLater()

    # Manual mode resolves it through the real evaluator path.
    check = evaluate_applicability(
        env.context,
        code='geometry',
        evaluator_id=APPLICABILITY_MANUAL_EVALUATOR_ID,
        detail='manual fixture',
        attestation_id=attestation.attestation_id,
    )
    assert check.passed is True
    assert check.evidence_refs[0].source_id == attestation.attestation_id
    assert check.evidence_refs[0].source_sha256 == (
        attestation.attestation_sha256
    )


def test_attestation_dialog_rejects_missing_actor_or_evidence(tmp_path) -> None:
    """Fail closed: no attestation is minted while required fields are empty."""
    env = _gate_fixture(tmp_path)
    campaign = SimpleNamespace(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        campaign_id='campaign-1',
    )
    repository = env.repository.applicability_attestations

    _app()
    dialog = ApplicabilityAttestationDialog(
        campaign=campaign,
        code='band',
        repository=repository,
        parent=None,
    )
    dialog._register()
    assert dialog.selected_attestation_id is None
    assert '証明者' in dialog.status.text()
    assert repository.list_for_search_spec(env.spec.search_spec_id) == ()

    dialog.actor.setText('田中 太郎')
    dialog.summary.setText('帯域カバレッジを確認した')
    dialog._register()  # evidence reference still empty
    assert dialog.selected_attestation_id is None
    assert '証拠' in dialog.status.text()
    assert repository.list_for_search_spec(env.spec.search_spec_id) == ()

    dialog.reference.setText('解析ログ #42')
    dialog._register()
    assert dialog.selected_attestation_id
    kept = repository.get(dialog.selected_attestation_id)
    assert kept is not None and kept.code == 'band'
    dialog.close()
    dialog.deleteLater()


def test_attestation_picker_only_offers_in_scope_entries(tmp_path) -> None:
    """The picker lists attestations anchored to this spec + code only."""
    env = _gate_fixture(tmp_path)
    repository = env.repository.applicability_attestations
    campaign = SimpleNamespace(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        campaign_id='campaign-1',
    )

    _app()

    def _mint(code: str, reference: str, summary: str) -> str:
        dialog = ApplicabilityAttestationDialog(
            campaign=campaign,
            code=code,
            repository=repository,
            parent=None,
        )
        dialog.actor.setText('田中 太郎')
        dialog.reference.setText(reference)
        dialog.summary.setText(summary)
        dialog._register()
        attestation_id = dialog.selected_attestation_id
        dialog.close()
        dialog.deleteLater()
        assert attestation_id
        return attestation_id

    first_id = _mint('geometry', '測定メモ A', '幾何の確認')
    _mint('band', '測定メモ B', '帯域の確認')  # different code → not listed

    picker = ApplicabilityAttestationDialog(
        campaign=campaign,
        code='geometry',
        repository=repository,
        parent=None,
    )
    entries = [
        picker.existing.topLevelItem(index)
        for index in range(picker.existing.topLevelItemCount())
    ]
    keyed = [
        item for item in entries
        if item.data(0, Qt.ItemDataRole.UserRole) is not None
    ]
    assert len(keyed) == 1
    picker.existing.setCurrentItem(keyed[0])
    picker._use_selected()
    assert picker.selected_attestation_id == first_id
    picker.close()
    picker.deleteLater()


def test_attestation_button_opens_dialog_via_controller(
    tmp_path, monkeypatch,
) -> None:
    """The 証明… button opens a widget-parented dialog through the real
    controller path — regression coverage for parenting the dialog to the
    QObject controller, which raised TypeError in the real GUI."""
    env = _gate_fixture(tmp_path)
    _app()
    controller = OptimizationWorkflowController(
        env.scene_repo, env.spec.document_id
    )
    campaign = SimpleNamespace(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        campaign_id='campaign-1',
    )
    monkeypatch.setattr(controller, '_selected_campaign', lambda: campaign)

    captured: dict[str, object] = {}

    def _exec(dialog: ApplicabilityAttestationDialog):
        captured['parent'] = dialog.parent()
        dialog.actor.setText('田中 太郎')
        dialog.reference.setText('測定メモ 2026-10-04')
        dialog.summary.setText('幾何モデルを実測と照合した')
        dialog._register()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplicabilityAttestationDialog, 'exec', _exec)
    controller.campaign_applicability_attest['geometry'].click()

    attest_button = controller.campaign_applicability_attest['geometry']
    assert captured['parent'] is attest_button
    attestation_id = (
        controller.campaign_applicability_detail['geometry'].text()
    )
    repository = controller.validation_service.applicability_attestations
    attestation = repository.get(attestation_id)
    assert attestation is not None
    assert attestation.code == 'geometry'
    assert attestation.document_id == env.spec.document_id
    state = controller.campaign_applicability_state['geometry']
    assert state.currentData() == 'manual'
    controller.dispose()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
