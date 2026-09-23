from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.cad_standards_evidence import (
    StandardsObservationAuthority,
    build_standards_observation_authority,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import ChannelRoleBinding, build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.optimization_workflow_workspace import OptimizationWorkflowWorkspace
from htdt.room_workspace import RoomWorkspace
from htdt.standards_workspace import (
    StandardsCriterionPanel,
    StandardsVariantComparisonPanel,
    StandardsWorkspaceModel,
)


NOW = "2026-09-20T05:40:00+00:00"
SOURCE = CriterionSource(
    publisher="Fixture Standards",
    document_title="S130 fixture",
    document_version="1.0",
    reference="§1",
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _criterion(
    criterion_id: str,
    *,
    maximum: float = 1.0,
    domain: str = "room",
    capability: str = "fixture-capability-v1",
) -> CriterionDefinition:
    return CriterionDefinition(
        criterion_id=criterion_id,
        name=f"基準 {criterion_id}",
        source=SOURCE,
        quantity=f"{criterion_id}_quantity",
        unit="m",
        applicable_domains=(domain,),
        required_inputs=(f"{criterion_id}_input",),
        required_capabilities=(capability,),
        rule=CriterionRule(operator="max", maximum=maximum),
    )


def _profile(version: str = "1.0", *, maximum: float = 1.0):
    return build_user_standards_profile(
        profile_id="s130-fixture",
        version=version,
        name="S130 ユーザー定義基準",
        criteria=(
            _criterion("pass", maximum=maximum),
            _criterion("fail", maximum=maximum),
            _criterion("unknown", maximum=maximum),
            _criterion("not-applicable", maximum=maximum, domain="projector"),
        ),
    )


def _authority(
    repository: CadStandardsRepository,
    target: StandardsEvaluationTarget,
    criterion_id: str,
    value: float,
    *,
    capability: bool = True,
    evidence_basis: str = "predicted",
) -> StandardsObservationAuthority:
    authority = build_standards_observation_authority(
        document_id=target.document_id,
        scene_revision_id=target.scene_revision_id,
        scene_content_hash=target.scene_content_hash,
        system_variant_id=target.system_variant_id,
        system_variant_sha256=target.system_variant_sha256,
        quantity=f"{criterion_id}_quantity",
        unit="m",
        observed_value=value,
        evidence_basis=evidence_basis,
        provided_inputs=(f"{criterion_id}_input",),
        capabilities=(("fixture-capability-v1",) if capability else ()),
        observed_at_utc=NOW,
    )
    repository.save_observation_authority(authority)
    return authority


def _observation(
    criterion_id: str,
    value: float,
    *,
    authority: StandardsObservationAuthority,
    capability: bool = True,
    evidence_basis: str = "predicted",
) -> CriterionObservation:
    return CriterionObservation(
        criterion_id=criterion_id,
        observed_value=value,
        unit="m",
        evidence_basis=evidence_basis,
        evidence_refs=(authority.ref(),),
        provided_inputs=(f"{criterion_id}_input",),
        capabilities=(("fixture-capability-v1",) if capability else ()),
    )


def _seed(tmp_path):
    scene_repository = SceneRepository(tmp_path / "scenes.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    standards_repository = CadStandardsRepository(
        scene_repository,
        variant_repository,
    )
    return scene_repository, revision, variant_repository, standards_repository


def _tree_statuses(panel: StandardsCriterionPanel) -> dict[str, str]:
    return {
        str(panel.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)):
            panel.tree.topLevelItem(index).text(1)
        for index in range(panel.tree.topLevelItemCount())
    }


def _item(panel: StandardsCriterionPanel, criterion_id: str):
    for index in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(index)
        if item.data(0, Qt.ItemDataRole.UserRole) == criterion_id:
            return item
    raise AssertionError(criterion_id)


def test_s130_room_panel_renders_exact_statuses_japanese_and_provenance(tmp_path) -> None:
    app = _app()
    scene_repository, revision, variant_repository, repository = _seed(tmp_path)
    profile = _profile()
    repository.save_profile(profile)

    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    target = model.target_view(None).target
    fail_authority = _authority(
        repository, target, "fail", 2.0, evidence_basis="measured"
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(
            _observation(
                "pass",
                0.5,
                authority=_authority(repository, target, "pass", 0.5),
            ),
            _observation(
                "fail",
                2.0,
                authority=fail_authority,
                evidence_basis="measured",
            ),
            _observation(
                "unknown",
                0.5,
                authority=_authority(
                    repository, target, "unknown", 0.5, capability=False
                ),
                capability=False,
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_evaluation(evaluation)

    panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    assert panel.select_profile(profile.profile_id, profile.version)
    panel.refresh()

    statuses = _tree_statuses(panel)
    assert statuses == {
        "pass": "✓ 適合",
        "fail": "✕ 不適合",
        "unknown": "? 判定材料不足",
        "not-applicable": "— 対象外",
    }
    visible = " ".join(statuses.values())
    assert "UNKNOWN" not in visible
    assert "NOT_APPLICABLE" not in visible

    fail_item = _item(panel, "fail")
    assert "実測" in fail_item.text(4)
    unknown_item = _item(panel, "unknown")
    assert "不足能力" in unknown_item.text(4)

    advanced = panel.advanced_text()
    assert revision.revision_id in advanced
    assert profile.profile_id in advanced
    assert profile.version in advanced
    assert fail_authority.authority_id in advanced
    assert "Evaluation SHA-256" in advanced

    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_s130_hard_constraint_opt_in_is_explicit_and_fail_closed(tmp_path) -> None:
    app = _app()
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    profile = _profile()
    repository.save_profile(profile)
    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    target = model.target_view(None).target
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(
            _observation(
                "pass",
                0.5,
                authority=_authority(repository, target, "pass", 0.5),
            ),
            _observation(
                "fail",
                2.0,
                authority=_authority(repository, target, "fail", 2.0),
            ),
            _observation(
                "unknown",
                0.5,
                authority=_authority(
                    repository, target, "unknown", 0.5, capability=False
                ),
                capability=False,
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_evaluation(evaluation)

    panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    assert panel.select_profile(profile.profile_id, profile.version)
    panel.refresh()
    assert panel.evaluation is not None

    # Unselected FAIL stays advisory and cannot block the target.
    gate = panel.model.hard_constraint_gate(
        panel.evaluation,
        panel.selected_criterion_ids,
    )
    assert gate.allowed

    fail_item = _item(panel, "fail")
    fail_item.setCheckState(0, Qt.CheckState.Checked)
    gate = panel.model.hard_constraint_gate(
        panel.evaluation,
        panel.selected_criterion_ids,
    )
    assert not gate.allowed
    assert gate.blocking_criterion_ids == ("fail",)

    fail_item.setCheckState(0, Qt.CheckState.Unchecked)
    unknown_item = _item(panel, "unknown")
    unknown_item.setCheckState(0, Qt.CheckState.Checked)
    gate = panel.model.hard_constraint_gate(
        panel.evaluation,
        panel.selected_criterion_ids,
    )
    assert not gate.allowed
    assert gate.blocking_criterion_ids == ("unknown",)
    assert "判定材料不足" in panel.gate_label.text()
    assert "安全側にブロック" in panel.gate_label.text()
    assert "UNKNOWN" not in panel.gate_label.text()

    # NOT_APPLICABLE remains irrelevant even when explicitly checked.
    unknown_item.setCheckState(0, Qt.CheckState.Unchecked)
    na_item = _item(panel, "not-applicable")
    na_item.setCheckState(0, Qt.CheckState.Checked)
    gate = panel.model.hard_constraint_gate(
        panel.evaluation,
        panel.selected_criterion_ids,
    )
    assert gate.allowed

    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_s130_profile_version_reevaluation_preserves_old_evaluation(tmp_path) -> None:
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    profile_v1 = build_user_standards_profile(
        profile_id="s130-history",
        version="1.0",
        name="履歴基準",
        criteria=(_criterion("pass", maximum=1.0),),
    )
    profile_v2 = build_user_standards_profile(
        profile_id="s130-history",
        version="2.0",
        name="履歴基準",
        criteria=(_criterion("pass", maximum=0.25),),
    )
    repository.save_profile(profile_v1)
    repository.save_profile(profile_v2)

    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    target = model.target_view(None).target
    old = evaluate_standards_profile(
        profile=profile_v1,
        target=target,
        observations=(
            _observation(
                "pass",
                0.5,
                authority=_authority(repository, target, "pass", 0.5),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_evaluation(old)

    newer = model.evaluate(profile_v2, variant_id=None)
    assert newer.profile_version == "2.0"
    assert newer.reevaluation_of_id == old.evaluation_id
    assert newer.results[0].status == "FAIL"
    assert repository.get_evaluation(old.evaluation_id) == old

    history = repository.list_evaluations_for_scene(revision.revision_id)
    assert [item.profile_version for item in history if item.profile_id == "s130-history"] == [
        "1.0",
        "2.0",
    ]


def test_s130_system_variant_matrix_binds_exact_variant_without_ranking(tmp_path) -> None:
    app = _app()
    scene_repository, revision, variant_repository, repository = _seed(tmp_path)
    variant = build_system_variant(
        baseline=revision,
        name="5.0.2 提案A",
        role_bindings=(
            ChannelRoleBinding(role_id="FL", display_name="Front Left"),
        ),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    profile = build_user_standards_profile(
        profile_id="s130-variant",
        version="1.0",
        name="構成比較基準",
        criteria=(_criterion("fail"),),
    )
    repository.save_profile(profile)

    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    baseline_target = model.target_view(None).target
    variant_target = model.target_view(variant.variant_id).target

    baseline_eval = evaluate_standards_profile(
        profile=profile,
        target=baseline_target,
        observations=(),
        created_at_utc=NOW,
    )
    variant_eval = evaluate_standards_profile(
        profile=profile,
        target=variant_target,
        observations=(
            _observation(
                "fail",
                2.0,
                authority=_authority(
                    repository, variant_target, "fail", 2.0
                ),
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_evaluation(baseline_eval)
    repository.save_evaluation(variant_eval)

    panel = StandardsVariantComparisonPanel(
        scene_repository,
        revision.document_id,
    )
    assert panel.select_profile(profile.profile_id, profile.version)
    panel.refresh()

    headers = [
        panel.matrix.headerItem().text(index)
        for index in range(panel.matrix.columnCount())
    ]
    assert "現在の部屋" in headers
    assert variant.name in headers
    assert not any(token in " ".join(headers) for token in ("winner", "総合点", "ranking"))

    variant_column = headers.index(variant.name)
    item = panel.matrix.topLevelItem(0)
    assert item.data(variant_column, Qt.ItemDataRole.UserRole) == "FAIL"

    # Unselected FAIL does not block this SystemVariant.
    gate = panel.gate_for_variant(variant.variant_id)
    assert gate is not None and gate.allowed

    # The same explicit criterion selection blocks FAIL and fail-closes UNKNOWN.
    item.setCheckState(0, Qt.CheckState.Checked)
    variant_gate = panel.gate_for_variant(variant.variant_id)
    baseline_gate = panel.gate_for_variant(None)
    assert variant_gate is not None and not variant_gate.allowed
    assert baseline_gate is not None and not baseline_gate.allowed
    assert variant_gate.blocking_criterion_ids == ("fail",)
    assert baseline_gate.blocking_criterion_ids == ("fail",)

    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_s130_profile_switch_clears_explicit_hard_constraint_opt_in(tmp_path) -> None:
    app = _app()
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    profile_a = build_user_standards_profile(
        profile_id="s130-switch-a",
        version="1.0",
        name="切替前基準",
        criteria=(_criterion("only-a"),),
    )
    profile_b = build_user_standards_profile(
        profile_id="s130-switch-b",
        version="1.0",
        name="切替後基準",
        criteria=(_criterion("only-b"),),
    )
    repository.save_profile(profile_a)
    repository.save_profile(profile_b)

    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    target = model.target_view(None).target
    repository.save_evaluation(
        evaluate_standards_profile(
            profile=profile_a,
            target=target,
            observations=(
                _observation(
                    "only-a",
                    2.0,
                    authority=_authority(repository, target, "only-a", 2.0),
                ),
            ),
            created_at_utc=NOW,
        )
    )
    repository.save_evaluation(
        evaluate_standards_profile(
            profile=profile_b,
            target=target,
            observations=(
                _observation(
                    "only-b",
                    0.5,
                    authority=_authority(repository, target, "only-b", 0.5),
                ),
            ),
            created_at_utc=NOW,
        )
    )

    room_panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    assert room_panel.select_profile(profile_a.profile_id, profile_a.version)
    room_panel.refresh()
    _item(room_panel, "only-a").setCheckState(0, Qt.CheckState.Checked)
    assert room_panel.selected_criterion_ids == ("only-a",)
    assert room_panel.select_profile(profile_b.profile_id, profile_b.version)
    assert room_panel.selected_criterion_ids == ()
    assert _item(room_panel, "only-b").checkState(0) == Qt.CheckState.Unchecked

    compare_panel = StandardsVariantComparisonPanel(
        scene_repository,
        revision.document_id,
    )
    assert compare_panel.select_profile(profile_a.profile_id, profile_a.version)
    compare_panel.refresh()
    compare_panel.matrix.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    assert compare_panel.selected_criterion_ids == ("only-a",)
    assert compare_panel.select_profile(profile_b.profile_id, profile_b.version)
    assert compare_panel.selected_criterion_ids == ()
    assert compare_panel.matrix.topLevelItem(0).checkState(0) == Qt.CheckState.Unchecked

    room_panel.close()
    room_panel.deleteLater()
    compare_panel.close()
    compare_panel.deleteLater()
    app.processEvents()


class _FakePlotter:
    def add_mesh(self, *_args, **_kwargs):
        return object()

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def add_text(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass


class _FakeViewport(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _FakePlotter()
        self.render_calls: list[tuple[str | None, bool]] = []

    def render_document(
        self,
        document,
        *,
        selected_id: str | None,
        selected_ids=(),
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        overlays=None,
        reset_camera: bool = False,
    ) -> None:
        self.render_calls.append((selected_id, reset_camera))

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, _entity_id: str) -> None:
        pass

    def render_proposed_entities(
        self,
        _entities,
        *,
        selected_id: str | None = None,
    ) -> None:
        pass


def test_s130_is_first_class_in_room_and_optimize_existing_contexts(tmp_path) -> None:
    app = _app()
    scene_repository, revision, _variant_repository, _standards_repository = _seed(tmp_path)

    room = RoomWorkspace(
        scene_repository,
        revision.document_id,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )
    room.set_context("placement")
    assert room.right_stack.currentWidget() is room.placement_panel
    assert isinstance(room.standards_panel, StandardsCriterionPanel)

    optimize = OptimizationWorkflowWorkspace(
        scene_repository,
        revision.document_id,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )
    optimize.select_section("comparison")
    assert optimize.current_page_id == "comparison"
    assert isinstance(
        optimize.standards_comparison_panel,
        StandardsVariantComparisonPanel,
    )

    room.close()
    room.deleteLater()
    optimize.close()
    optimize.deleteLater()
    app.processEvents()
