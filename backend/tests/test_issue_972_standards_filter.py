"""Offscreen coverage for #972 — standards criterion list search,
status/domain narrowing, the result-count line, and honest reporting of
hard-constraint selections hidden by the active filter.

The criterion tree resolves every check by ``criterion_id`` (never by row
position), so filters may hide rows but must never drop the opt-in: a
checked constraint keeps participating in ``hard_constraint_gate`` and the
panel reports it as 「フィルタ外の制約選択 N件」.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from htdt.cad_standards import (  # noqa: E402
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.standards_workspace import (  # noqa: E402
    DOMAIN_FILTER_ALL,
    STATUS_FILTER_ALL,
    STATUS_FILTER_SELECTED,
    STATUS_FILTER_UNEVALUATED,
    StandardsCriterionPanel,
    StandardsWorkspaceModel,
)

from test_standards_workspace import (  # noqa: E402
    NOW,
    _authority,
    _item,
    _observation,
    _seed,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


SOURCE_A = CriterionSource(
    publisher="RP22 Committee",
    document_title="RP22 spatial layout",
    document_version="1.2",
    reference="§4.1",
)
SOURCE_B = CriterionSource(
    publisher="AURO Authority",
    document_title="AURO-3D layout",
    document_version="rev12",
    reference="Table 3",
)


def _criterion(
    criterion_id: str,
    *,
    name: str | None = None,
    domain: str = "room",
    source: CriterionSource = SOURCE_A,
) -> CriterionDefinition:
    return CriterionDefinition(
        criterion_id=criterion_id,
        name=name or f"基準 {criterion_id}",
        source=source,
        quantity=f"{criterion_id}_quantity",
        unit="m",
        applicable_domains=(domain,),
        required_inputs=(f"{criterion_id}_input",),
        required_capabilities=("fixture-capability-v1",),
        rule=CriterionRule(operator="max", maximum=1.0),
    )


def _profile(version: str = "1.0"):
    return build_user_standards_profile(
        profile_id="s972-filter",
        version=version,
        name="S972 絞り込み基準",
        criteria=(
            _criterion("c-pass", name="境界までの距離上限", domain="room"),
            _criterion(
                "c-fail",
                name="高層スピーカー仰角上限",
                domain="auro_height_speaker",
                source=SOURCE_B,
            ),
            _criterion(
                "c-unknown",
                name="スピーカー配置整合",
                domain="speaker_layout",
            ),
            _criterion("c-na", name="プロジェクター照度", domain="projector"),
        ),
    )


def _panel(tmp_path, *, evaluate: bool = True):
    """Panel seeded with one evaluation: PASS / FAIL / UNKNOWN /
    NOT_APPLICABLE across four differently-domained criteria."""
    app = _app()
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    profile = _profile()
    repository.save_profile(profile)
    model = StandardsWorkspaceModel(scene_repository, revision.document_id)
    target = model.target_view(None).target
    if evaluate:
        repository.save_evaluation(
            evaluate_standards_profile(
                profile=profile,
                target=target,
                observations=(
                    _observation(
                        "c-pass",
                        0.5,
                        authority=_authority(
                            repository, target, "c-pass", 0.5
                        ),
                    ),
                    _observation(
                        "c-fail",
                        2.0,
                        authority=_authority(
                            repository, target, "c-fail", 2.0
                        ),
                    ),
                    _observation(
                        "c-unknown",
                        0.5,
                        authority=_authority(
                            repository,
                            target,
                            "c-unknown",
                            0.5,
                            capability=False,
                        ),
                        capability=False,
                    ),
                ),
                created_at_utc=NOW,
            )
        )
    panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    assert panel.select_profile(profile.profile_id, profile.version)
    panel.refresh()
    return app, panel


def _visible_ids(panel: StandardsCriterionPanel) -> list[str]:
    ids = []
    for index in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(index)
        value = item.data(0, Qt.ItemDataRole.UserRole)
        if value is not None:
            ids.append(str(value))
    return ids


def _set_status_filter(panel: StandardsCriterionPanel, key: str) -> None:
    index = panel.status_filter_combo.findData(key)
    assert index >= 0, key
    panel.status_filter_combo.setCurrentIndex(index)


def _set_domain_filter(panel: StandardsCriterionPanel, key: str) -> None:
    index = panel.domain_filter_combo.findData(key)
    assert index >= 0, key
    panel.domain_filter_combo.setCurrentIndex(index)


def _close(app: QApplication, panel: StandardsCriterionPanel) -> None:
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_search_matches_name_id_source_and_domain(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        panel.search_edit.setText("境界")  # JA criterion name
        assert _visible_ids(panel) == ["c-pass"]
        panel.search_edit.setText("C-FAIL")  # criterion_id, case-insensitive
        assert _visible_ids(panel) == ["c-fail"]
        panel.search_edit.setText("table 3")  # source reference
        assert _visible_ids(panel) == ["c-fail"]
        panel.search_edit.setText("speaker_layout")  # raw domain key
        assert _visible_ids(panel) == ["c-unknown"]
        panel.search_edit.setText("高層スピーカー")  # domain JA label
        assert _visible_ids(panel) == ["c-fail"]
        panel.search_edit.clear()
        assert len(_visible_ids(panel)) == 4
    finally:
        _close(app, panel)


def test_status_filters_narrow_by_verdict(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        _set_status_filter(panel, "PASS")
        assert _visible_ids(panel) == ["c-pass"]
        _set_status_filter(panel, "FAIL")
        assert _visible_ids(panel) == ["c-fail"]
        _set_status_filter(panel, "UNKNOWN")
        assert _visible_ids(panel) == ["c-unknown"]
        _set_status_filter(panel, "NOT_APPLICABLE")
        assert _visible_ids(panel) == ["c-na"]
        _set_status_filter(panel, STATUS_FILTER_ALL)
        assert len(_visible_ids(panel)) == 4
    finally:
        _close(app, panel)


def test_unevaluated_filter_before_and_after_evaluation(tmp_path) -> None:
    app, panel = _panel(tmp_path, evaluate=False)
    try:
        _set_status_filter(panel, STATUS_FILTER_UNEVALUATED)
        assert len(_visible_ids(panel)) == 4
        _set_status_filter(panel, "FAIL")
        assert _visible_ids(panel) == []

        _set_status_filter(panel, STATUS_FILTER_ALL)
        panel.evaluate_selected()
        # After evaluation every criterion has a verdict — 未評価のみ
        # honestly shows the zero state rather than stale rows.
        _set_status_filter(panel, STATUS_FILTER_UNEVALUATED)
        assert _visible_ids(panel) == []
        assert panel.tree.topLevelItemCount() == 1
        zero = panel.tree.topLevelItem(0)
        assert "一致する基準はありません" in zero.text(0)
        assert not (zero.flags() & Qt.ItemFlag.ItemIsSelectable)
    finally:
        _close(app, panel)


def test_domain_filter_narrows_by_applicable_domain(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        keys = {
            panel.domain_filter_combo.itemData(i)
            for i in range(panel.domain_filter_combo.count())
        }
        assert {
            DOMAIN_FILTER_ALL,
            "room",
            "auro_height_speaker",
            "speaker_layout",
            "projector",
        } <= keys
        _set_domain_filter(panel, "room")
        assert _visible_ids(panel) == ["c-pass"]
        _set_domain_filter(panel, "auro_height_speaker")
        assert _visible_ids(panel) == ["c-fail"]
        _set_domain_filter(panel, DOMAIN_FILTER_ALL)
        assert len(_visible_ids(panel)) == 4
    finally:
        _close(app, panel)


def test_result_count_line_tracks_filters_and_zero_state(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        assert "基準 4 件" in panel.result_label.text()
        assert "制約選択 0 件" in panel.result_label.text()
        assert "表示" not in panel.result_label.text()
        assert not panel.reset_filter_button.isEnabled()

        panel.search_edit.setText("境界")
        assert "表示 1/4" in panel.result_label.text()
        assert panel.reset_filter_button.isEnabled()

        panel.search_edit.setText("存在しない基準名")
        assert "表示 0/4" in panel.result_label.text()
        zero = panel.tree.topLevelItem(0)
        assert "一致する基準はありません" in zero.text(0)
        assert "存在しない基準名" in zero.toolTip(0)
        # The zero-state message must span the tree — spanning only takes
        # effect after the item is inserted (a pre-insert call no-ops).
        assert zero.isFirstColumnSpanned()

        panel.reset_filter_button.click()
        assert len(_visible_ids(panel)) == 4
        assert not panel.reset_filter_button.isEnabled()
    finally:
        _close(app, panel)


def test_hidden_selected_constraints_reported_and_still_block(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        _item(panel, "c-fail").setCheckState(0, Qt.CheckState.Checked)
        gate = panel.model.hard_constraint_gate(
            panel.evaluation, panel.selected_criterion_ids
        )
        assert not gate.allowed

        _set_status_filter(panel, "PASS")
        assert _visible_ids(panel) == ["c-pass"]
        # A filtered-out selection stays opted in AND is reported —
        # constraints must never silently disappear. The warning leads
        # the line so a clipped tail can never hide it.
        assert panel.result_label.text().startswith(
            "フィルタ外の制約選択 1 件"
        )
        gate = panel.model.hard_constraint_gate(
            panel.evaluation, panel.selected_criterion_ids
        )
        assert not gate.allowed
        assert gate.blocking_criterion_ids == ("c-fail",)

        panel.reset_filters()
        assert (
            _item(panel, "c-fail").checkState(0) == Qt.CheckState.Checked
        )
    finally:
        _close(app, panel)


def test_selected_filter_lists_opted_in_and_uncheck_leaves_view(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        _item(panel, "c-pass").setCheckState(0, Qt.CheckState.Checked)
        _item(panel, "c-fail").setCheckState(0, Qt.CheckState.Checked)
        _set_status_filter(panel, STATUS_FILTER_SELECTED)
        assert _visible_ids(panel) == ["c-pass", "c-fail"]

        # Unchecking under the selected-only view removes the row after
        # the deferred refilter — and releases the gate.
        _item(panel, "c-pass").setCheckState(0, Qt.CheckState.Unchecked)
        app.processEvents()
        assert _visible_ids(panel) == ["c-fail"]
        assert panel.selected_criterion_ids == ("c-fail",)
        assert "制約選択 1 件" in panel.result_label.text()
    finally:
        _close(app, panel)


def test_filter_and_check_state_survive_refresh(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        _item(panel, "c-fail").setCheckState(0, Qt.CheckState.Checked)
        panel.search_edit.setText("境界")  # hides the checked row
        assert _visible_ids(panel) == ["c-pass"]

        panel.refresh()  # filter stays; check state is keyed by id
        assert _visible_ids(panel) == ["c-pass"]
        assert panel.selected_criterion_ids == ("c-fail",)
        assert "フィルタ外の制約選択 1 件" in panel.result_label.text()

        panel.search_edit.clear()
        assert _item(panel, "c-fail").checkState(0) == Qt.CheckState.Checked
    finally:
        _close(app, panel)


def test_columns_keep_original_indices_with_appended_tail(tmp_path) -> None:
    app, panel = _panel(tmp_path)
    try:
        headers = [
            panel.tree.headerItem().text(index)
            for index in range(panel.tree.columnCount())
        ]
        assert headers[:5] == [
            "配置制約 / 基準",
            "状態",
            "観測値",
            "必要条件",
            "証拠",
        ]
        assert headers[5:] == ["領域", "出典"]

        fail_item = _item(panel, "c-fail")
        assert fail_item.data(0, Qt.ItemDataRole.UserRole) == "c-fail"
        assert fail_item.text(1) == "✕ 不適合"
        assert fail_item.text(5) == "Auro 高層スピーカー"
        assert fail_item.text(6) == "AURO Authority · Table 3"
    finally:
        _close(app, panel)


def test_empty_profile_shows_honest_empty_state(tmp_path) -> None:
    app = _app()
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    empty_profile = build_user_standards_profile(
        profile_id="s972-empty",
        version="1.0",
        name="空の基準セット",
        criteria=(),
    )
    repository.save_profile(empty_profile)
    panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    try:
        assert panel.select_profile(
            empty_profile.profile_id, empty_profile.version
        )
        panel.refresh()
        assert _visible_ids(panel) == []
        assert panel.tree.topLevelItemCount() == 1
        assert "基準項目がありません" in panel.tree.topLevelItem(0).text(0)
        assert "基準 0 件" in panel.result_label.text()
    finally:
        _close(app, panel)


def test_profile_switch_resets_domain_choices_but_keeps_search(tmp_path) -> None:
    app = _app()
    scene_repository, revision, _variant_repository, repository = _seed(tmp_path)
    repository.save_profile(_profile())
    narrow = build_user_standards_profile(
        profile_id="s972-narrow",
        version="1.0",
        name="room だけの基準",
        criteria=(_criterion("only-room", domain="room"),),
    )
    repository.save_profile(narrow)

    panel = StandardsCriterionPanel(scene_repository, revision.document_id)
    try:
        profile = _profile()
        assert panel.select_profile(profile.profile_id, profile.version)
        panel.refresh()
        _set_domain_filter(panel, "projector")
        panel.search_edit.setText("照度")
        assert _visible_ids(panel) == ["c-na"]

        # Switching profiles rebuilds the domain combo from the new
        # profile's own domains — a now-missing pick falls back to 全て,
        # while the search text stays as typed.
        assert panel.select_profile(narrow.profile_id, narrow.version)
        panel.refresh()
        keys = {
            panel.domain_filter_combo.itemData(i)
            for i in range(panel.domain_filter_combo.count())
        }
        assert keys == {DOMAIN_FILTER_ALL, "room"}
        assert panel.domain_filter_combo.currentData() == DOMAIN_FILTER_ALL
        assert panel.search_edit.text() == "照度"
        assert _visible_ids(panel) == []  # 照度 matches nothing here
        panel.search_edit.clear()
        assert _visible_ids(panel) == ["only-room"]
    finally:
        _close(app, panel)
