from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Offset3,
    Position3,
    RoomPrism,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)
from htdt.prediction_interpretation import (
    ProviderEvidence,
    interpret_prediction_results,
    provider_evidence,
)
from htdt.room_prediction import RoomPredictionController, RoomPredictionPanel
from htdt.room_workspace import RoomWorkspace, RoomWorkspaceController


def _speaker(entity_id: str, x_m: float, y_m: float, z_m: float = 1.0) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind="speaker",
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
        acoustic_reference_offset_m=Offset3(),
        speaker_role="FL",
    )


def _rect_scene(document_id: str = "prediction-rect") -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id="a", x_m=10.0, y_m=20.0),
            RoomVertex(vertex_id="b", x_m=16.0, y_m=20.0),
            RoomVertex(vertex_id="c", x_m=16.0, y_m=24.0),
            RoomVertex(vertex_id="d", x_m=10.0, y_m=24.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            _speaker("speaker-fl", 11.4, 20.8, 1.05),
            SceneEntity(
                entity_id="point-mlp",
                kind="measurement_point",
                name="MLP",
                position=Position3(x_m=13.0, y_m=23.0, z_m=1.1),
            ),
        ),
    )


def _l_scene(document_id: str = "prediction-l") -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id="a", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="b", x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id="c", x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id="d", x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id="e", x_m=4.0, y_m=2.0),
            RoomVertex(vertex_id="f", x_m=2.0, y_m=2.0),
            RoomVertex(vertex_id="g", x_m=2.0, y_m=4.0),
            RoomVertex(vertex_id="h", x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            _speaker("speaker-fl", 1.0, 0.8),
            SceneEntity(
                entity_id="point-mlp",
                kind="measurement_point",
                name="MLP",
                position=Position3(x_m=1.0, y_m=1.5, z_m=1.1),
            ),
        ),
    )


def _rect_run(tmp_path, *, max_mode_hz: float = 300.0):
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(_rect_scene(), parent_revision_id=None).revision
    results = analyze_native_rectangular_geometry(
        revision, "point-mlp", max_mode_hz=max_mode_hz
    )
    return repository, revision, results


def _provider_double():
    return SimpleNamespace(
        provider_id="r170a-provider:" + "a" * 64,
        semantic_sha256="b" * 64,
        evidence_state="candidate",
        evidence_scope="unvalidated",
        observable_capabilities=(
            SimpleNamespace(
                observable="frequency_response_magnitude",
                state="READY",
                reason=None,
            ),
            SimpleNamespace(
                observable="rt60",
                state="UNSUPPORTED",
                reason="R170A has no decay authority for RT60",
            ),
        ),
        valid_frequency_domain=SimpleNamespace(minimum_hz=40.0, maximum_hz=80.0),
        result_envelope_id="acoustic-solver-result:" + "c" * 64,
        result_envelope_sha256="c" * 64,
        current_authority=SimpleNamespace(
            source_entity_id="speaker-fl",
            acoustic_scene_snapshot_id="acoustic-scene-snapshot:" + "d" * 64,
            acoustic_scene_snapshot_sha256="d" * 64,
            prediction_request_id="acoustic-prediction-request:" + "e" * 64,
            prediction_request_sha256="e" * 64,
            scene_revision_id="rev-1",
            scene_content_hash="f" * 64,
        ),
        receiver_responses=(
            SimpleNamespace(receiver_entity_id="point-mlp"),
        ),
        adapter_id="htdt.r170a.r130_complex_pressure",
        adapter_version="1",
        authority_version="r170a-low-band-provider-1",
    )


def test_interpretation_explains_modes_and_reflections_with_exact_links(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    assert interpretation is not None

    modes_result = next(
        item for item in results if item.result_kind == "geometry_modes"
    )
    reflections_result = next(
        item for item in results if item.result_kind == "geometry_reflections"
    )

    mode_findings = [
        item for item in interpretation.findings if item.kind == "room_mode"
    ]
    assert mode_findings
    first_mode = modes_result.modes[0]
    assert f"{first_mode.frequency_hz:.1f} Hz" in mode_findings[0].title
    assert "MLP" in mode_findings[0].detail
    assert mode_findings[0].spatial is not None
    assert mode_findings[0].spatial.receiver_entity_id == "point-mlp"

    reflection_findings = [
        item for item in interpretation.findings if item.kind == "reflection_path"
    ]
    assert reflection_findings
    for finding in reflection_findings:
        link = finding.spatial
        assert link is not None and link.kind == "reflection_path"
        stored = reflections_result.reflections[link.reflection_index]
        # Spatial authority is exact: the link replays the persisted row.
        assert link.speaker_entity_id == stored.speaker_entity_id
        assert link.surface_key == stored.surface_key
        assert link.surface_identity == stored.surface_identity
        assert link.source_position == stored.source_position
        assert link.receiver_position == stored.receiver_position
        assert link.reflection_position == stored.reflection_position
        assert f"+{stored.excess_delay_ms:.2f} ms" in finding.detail
        assert finding.authorities
        assert finding.authorities[0].sha256 == reflections_result.result_sha256
        assert finding.authorities[0].ref_id == reflections_result.prediction_id

    # The earliest-arriving path is explicitly identified, not ranked as best.
    earliest = min(
        range(len(reflections_result.reflections)),
        key=lambda index: reflections_result.reflections[index].excess_delay_ms,
    )
    earliest_finding = interpretation.finding(f"reflection:{earliest}")
    assert earliest_finding is not None
    assert "最も早く到達" in earliest_finding.detail


def test_interpretation_makes_band_and_validation_explicit(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    reliability = interpretation.reliability
    assert reliability.freshness == "current"
    assert reliability.evidence_state == "unvalidated"
    assert reliability.approximation_state == "exact_for_model_geometry"
    assert any(
        band.label == "room mode候補" and band.maximum_hz == 300.0
        for band in reliability.valid_bands
    )

    coverage = [
        item.detail + item.title
        for item in interpretation.findings
        if item.kind == "coverage"
    ]
    assert any("300" in text and "未評価" in text for text in coverage)
    validation = [
        item
        for item in interpretation.findings
        if item.kind == "validation"
    ]
    assert any("validation は未完了" in item.title for item in validation)

    capabilities = {item.observable: item for item in reliability.capabilities}
    assert capabilities["room_mode_frequencies"].state == "READY"
    assert capabilities["reflection_path_geometry"].state == "READY"
    # Unavailable capability stays explicit, never hidden.
    assert capabilities["frequency_response_magnitude"].state == "UNSUPPORTED"
    assert capabilities["frequency_response_phase"].state == "UNSUPPORTED"
    assert capabilities["spatial_pressure_field"].state == "UNSUPPORTED"
    assert capabilities["speaker_directivity"].state == "UNSUPPORTED"
    assert capabilities["material_boundary"].state == "UNSUPPORTED"
    assert capabilities["measurement_validation"].state == "UNSUPPORTED"
    # Reasons trace back to stored assumptions where they exist.
    assert (
        capabilities["frequency_response_phase"].reason
        == "反射の振幅/位相は未評価"
    )


def test_interpretation_actions_are_neutral_hypotheses(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    assert interpretation.next_actions
    assert all(
        action.basis == "hypothesis" for action in interpretation.next_actions
    )
    # No fabricated winner or automatic recommendation.
    combined = " ".join(
        action.label + action.detail for action in interpretation.next_actions
    )
    for forbidden in ("最適", "推奨", "best", "recommended"):
        assert forbidden not in combined
    labels = {action.label for action in interpretation.next_actions}
    assert "seat候補を比較" in labels
    assert "speaker位置を比較" in labels
    assert "この反射面を確認" in labels
    assert "材料を設定" in labels
    assert "実測で検証" in labels


def test_interpretation_keeps_model_and_hashes_in_advanced(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    advanced = "\n".join(interpretation.advanced_lines)
    assert results[0].model_id in advanced
    assert results[0].model_version in advanced
    assert results[0].run_id in advanced
    assert results[0].input_hash in advanced
    assert results[0].result_sha256 in advanced
    assert results[0].scene_revision_id in advanced
    # Finding titles never depend on solver/model identity.
    assert all(
        results[0].model_id not in finding.title
        for finding in interpretation.findings
    )


def test_interpretation_marks_unsupported_geometry_and_stale_state(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = repository.save(_l_scene(), parent_revision_id=None).revision
    results = analyze_native_rectangular_geometry(revision, "point-mlp")

    interpretation = interpret_prediction_results(
        results, is_current=False, document=revision.document
    )
    assert interpretation is not None
    compatibility = [
        item
        for item in interpretation.findings
        if item.kind == "compatibility"
    ]
    assert compatibility
    assert "対象外" in compatibility[0].title
    assert interpretation.reliability.freshness == "stale"
    assert interpretation.reliability.approximation_state == "unsupported"
    capabilities = {
        item.observable: item for item in interpretation.reliability.capabilities
    }
    assert capabilities["room_mode_frequencies"].state == "UNSUPPORTED"
    assert capabilities["reflection_path_geometry"].state == "UNSUPPORTED"
    assert any(
        action.action_id == "rerun" for action in interpretation.next_actions
    )


def test_interpretation_surfaces_stored_speaker_warnings(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    # F1 speakers have no acoustic reference offset, so the run persists
    # speaker_acoustic_reference_unknown warnings and zero reflections.
    revision = repository.save(
        SceneDocument(
            document_id=F1_DOCUMENT_ID,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
            entities=(
                SceneEntity(
                    entity_id="speaker-fl",
                    kind="speaker",
                    name="Front Left",
                    speaker_role="FL",
                    position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                    size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                ),
                SceneEntity(
                    entity_id="point-mlp",
                    kind="measurement_point",
                    name="MLP",
                    position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    results = analyze_native_rectangular_geometry(revision, "point-mlp")

    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    warnings = [item for item in interpretation.findings if item.kind == "warning"]
    assert warnings
    speaker_warning = next(
        item
        for item in warnings
        if item.spatial is not None
        and item.spatial.speaker_entity_id == "speaker-fl"
    )
    assert "音響基準点が未設定" in speaker_warning.title
    assert speaker_warning.spatial is not None
    assert speaker_warning.spatial.kind == "source"
    assert speaker_warning.spatial.speaker_entity_id == "speaker-fl"


def test_provider_evidence_projects_exact_authority() -> None:
    provider = _provider_double()
    evidence = provider_evidence(provider)
    assert evidence.provider_id == provider.provider_id
    assert evidence.semantic_sha256 == provider.semantic_sha256
    assert evidence.evidence_state == "candidate"
    assert evidence.valid_band_hz == (40.0, 80.0)
    states = {item.observable: item.state for item in evidence.capabilities}
    assert states["frequency_response_magnitude"] == "READY"
    assert states["rt60"] == "UNSUPPORTED"
    rt60 = next(
        item for item in evidence.capabilities if item.observable == "rt60"
    )
    assert rt60.reason == "R170A has no decay authority for RT60"
    assert evidence.stale_state == "unknown"
    assert evidence.source_entity_id == "speaker-fl"
    assert evidence.receiver_entity_ids == ("point-mlp",)
    kinds = {ref.kind for ref in evidence.authority_refs}
    assert {"prediction_provider", "solver_result"} <= kinds


def test_interpretation_consumes_provider_path(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    provider = _provider_double()
    resolution = SimpleNamespace(
        stale_state="STALE", reasons=("SceneRevision changed",)
    )

    interpretation = interpret_prediction_results(
        results,
        is_current=True,
        document=revision.document,
        provider=provider,
        provider_resolution=resolution,
    )
    titles = [item.title for item in interpretation.findings]
    assert any("40–80 Hz" in title for title in titles)
    assert any("80 Hz より上" in title for title in titles)
    assert any("STALE" in title for title in titles)
    assert any("candidate" in title for title in titles)
    reliability = interpretation.reliability
    assert reliability.evidence_state == "candidate"
    assert reliability.provider_stale_state == "STALE"
    assert reliability.provider_stale_reasons == ("SceneRevision changed",)
    observables = {
        item.observable for item in reliability.capabilities
    }
    assert "rt60" in observables  # provider capabilities merged verbatim


def test_interpretation_accepts_prebuilt_provider_evidence(tmp_path) -> None:
    _repository, revision, results = _rect_run(tmp_path)
    evidence = ProviderEvidence(
        provider_id="r170a-provider:" + "a" * 64,
        semantic_sha256="b" * 64,
        evidence_state="validated",
        evidence_scope="synthetic_fixture",
        stale_state="CURRENT",
        stale_reasons=(),
        valid_band_hz=(40.0, 80.0),
        capabilities=(),
        authority_refs=(),
    )
    interpretation = interpret_prediction_results(
        results,
        is_current=True,
        document=revision.document,
        provider=evidence,
    )
    assert interpretation.reliability.evidence_state == "validated"
    assert any(
        "validated" in item.title for item in interpretation.findings
    )


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _RecordingPlotter:
    def __init__(self) -> None:
        self.meshes: list[dict] = []

    def add_mesh(self, *_args, **kwargs):
        self.meshes.append(kwargs)
        return None

    def add_text(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass


from PySide6.QtCore import Signal  # noqa: E402
from PySide6.QtWidgets import QFrame  # noqa: E402


class _FakeViewport(QFrame):
    """Viewport stand-in recording prediction overlay calls."""

    entitySelected = Signal(object)
    proposedEntitySelected = Signal(object)
    contextMenuRequested = Signal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _RecordingPlotter()
        self.prediction_calls: list[dict] = []

    def render_document(self, *_args, **_kwargs) -> None:
        pass

    def render_proposed_entities(self, *_args, **_kwargs) -> None:
        pass

    def render_prediction_results(self, results, *, highlight=None) -> None:
        self.prediction_calls.append(
            {"results": results, "highlight": highlight}
        )

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, _entity_id) -> None:
        pass


def test_panel_explains_findings_and_emits_spatial_link(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(_rect_scene("prediction-rect"), parent_revision_id=None)
    room = RoomWorkspaceController(repository, "prediction-rect")
    controller = RoomPredictionController(repository, room)
    panel = RoomPredictionPanel(controller)
    app.processEvents()

    from threading import Event

    spec = controller.prepare_run("point-mlp")
    results = controller._analyze(spec, Event())
    accepted = controller.accept_results(spec, results)
    assert accepted is not None

    emitted: list[object] = []
    panel.findingSelected.connect(emitted.append)
    panel._show_results(accepted)
    app.processEvents()

    # Normal view leads with explanation, not model ids.
    assert panel.findings.count() > 0
    reliability_text = panel.reliability.text()
    assert "現在の条件に一致" in reliability_text
    assert "validation" in reliability_text
    assert "300" in reliability_text  # evaluated band is explicit
    assert "未評価" in reliability_text  # unsupported capability is explicit
    assert "htdt.rectangular_geometry" not in reliability_text
    assert controller.interpretation_for(accepted) is not None

    titles = [
        panel.findings.item(index).text()
        for index in range(panel.findings.count())
    ]
    assert any("反射経路" in title for title in titles)
    assert any("Hz" in title and "モード候補" in title for title in titles)

    # Selecting a finding emits its exact spatial link for the viewport.
    reflection_row = next(
        index for index, title in enumerate(titles) if "反射経路" in title
    )
    panel.findings.setCurrentRow(reflection_row)
    app.processEvents()
    assert emitted
    finding = emitted[-1]
    assert finding is not None
    assert finding.spatial is not None
    assert finding.spatial.kind == "reflection_path"
    assert finding.spatial.surface_identity
    assert "根拠:" in panel.finding_detail.text()

    # Solver/model identity stays available but only under Advanced.
    assert "htdt.rectangular_geometry" in panel.advanced.text()
    assert panel.advanced.isHidden() is True
    panel.advanced_toggle.setChecked(True)
    assert panel.advanced.isHidden() is False

    # Neutral next steps carry no fabricated winner.
    assert "仮説" in panel.next_steps.text()

    controller.dispose()
    panel.deleteLater()
    app.processEvents()


def test_workspace_prediction_focus_reaches_viewport_highlight(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    revision = repository.save(
        _rect_scene("prediction-rect"), parent_revision_id=None
    ).revision
    results = analyze_native_rectangular_geometry(revision, "point-mlp")
    interpretation = interpret_prediction_results(
        results, is_current=True, document=revision.document
    )
    link = next(
        item.spatial
        for item in interpretation.findings
        if item.kind == "reflection_path"
    )

    viewport = _FakeViewport()
    workspace = RoomWorkspace(
        repository,
        "prediction-rect",
        viewport_factory=lambda parent: viewport,
    )
    # Prediction overlays only render while the acoustics overlay is on.
    workspace.overlay_controls.acoustics.setChecked(True)
    workspace.set_prediction_results(results)
    workspace.set_prediction_focus(link)
    assert viewport.prediction_calls
    assert viewport.prediction_calls[-1]["highlight"] is link

    # Selecting a different run clears the spatial focus.
    workspace.set_prediction_results(())
    assert workspace.prediction_focus is None

    workspace.close()
    app.processEvents()


def test_panel_view_does_not_claim_environment_selection(tmp_path) -> None:
    """#915 regression: opening the panel must not persist an environment
    selection the operator never made (that made a clean project dirty)."""
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(_rect_scene("prediction-env"), parent_revision_id=None)
    room = RoomWorkspaceController(repository, "prediction-env")
    controller = RoomPredictionController(repository, room)

    assert not room.is_dirty
    panel = RoomPredictionPanel(controller)  # noqa: F841 — init refreshes
    app.processEvents()

    assert not room.is_dirty
    assert controller.selected_environment_profile() is None
