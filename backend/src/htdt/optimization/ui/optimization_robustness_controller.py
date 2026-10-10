from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pyvista as pv

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem

from ..persistence.cad_robustness_repository import CadRobustnessRepository
from ...cad_search_models import constraint_workspace_snapshot
from ...tree_item_role import ROLE
from ..services.optimization_robustness_overlay import build_robustness_overlay_model
from ...error_boundary import EXPECTED_OPERATION_ERRORS
from ...user_facing_error import operation_error_message
from ..services.optimization_robustness_presenter import (
    build_robustness_candidate_presentation,
    robustness_comparison_eligibility,
)


class RobustnessControllerMixin:
    """Read-only O90D integration over existing O90A-O90C authorities."""

    def _create_robustness_controls(self) -> None:
        self.robustness_summary_label = QLabel(
            '候補を選択し、既存のばらつき評価を確認してください'
        )
        self.robustness_summary_label.setWordWrap(True)
        self.robustness_comparison_tree = QTreeWidget()
        self.robustness_comparison_tree.setAccessibleName('ばらつき比較')
        self.robustness_comparison_tree.setHeaderLabels(['項目'])
        self.robustness_tree = QTreeWidget()
        self.robustness_tree.setAccessibleName('ばらつき評価')
        self.robustness_tree.setHeaderLabels(
            [
                '候補',
                '指標',
                '方向',
                'ノミナル',
                '評価サンプル内の不利側最大値',
                '感度',
                '評価状況',
            ]
        )
        self.robustness_tree.itemSelectionChanged.connect(
            self._robustness_row_selected
        )
        self.robustness_sensitivity_plot = pg.PlotWidget()
        self.robustness_sensitivity_plot.showGrid(x=True, y=True, alpha=0.18)
        self.robustness_sensitivity_plot.setLabel('left', '感度')
        self.robustness_sensitivity_plot.setLabel('bottom', '不確かさ軸')
        self.robustness_distribution_plot = pg.PlotWidget()
        self.robustness_distribution_plot.showGrid(x=True, y=True, alpha=0.18)
        self.robustness_distribution_plot.setLabel('left', 'サンプル数')
        self.robustness_distribution_plot.setLabel('bottom', '評価値')
        self.robustness_distribution_note_label = QLabel(
            '有限サンプルの頻度表示です。明示的な確率モデルがない限り確率分布ではありません。'
        )
        self.robustness_distribution_note_label.setWordWrap(True)
        self.robustness_probability_label = QLabel(
            '確率指標は、明示的な確率分布または重みがある場合だけ表示します'
        )
        self.robustness_probability_label.setWordWrap(True)
        self.robustness_detail_label = QLabel('ばらつき評価が未選択です')
        self.robustness_detail_label.setWordWrap(True)
        self.robustness_advanced_label = QLabel('内部権威情報は未選択です')
        self.robustness_advanced_label.setWordWrap(True)

        self.robustness_tree.setToolTip(
            '候補ごとのばらつき評価です。配置・向きが少しずれたとき指標がどれだけ悪化するかを示します。'
        )
        self.robustness_comparison_tree.setToolTip(
            '複数候補のばらつき指標の並行比較です。列は候補、行は指標です。'
        )
        robustness_header = self.robustness_tree.headerItem()
        for column, text in {
            0: '評価対象の候補',
            1: '評価指標の名前',
            2: '指標の良い方向（小さいほど良い等）',
            3: '基準配置（ずれなし）での指標値',
            4: 'ばらつき評価点の中で最も悪い値',
            5: '入力が1単位ずれたときの指標変化量',
            6: '評価データの充足状況',
        }.items():
            robustness_header.setToolTip(column, text)
        self.robustness_sensitivity_plot.setToolTip(
            '各不確かさ軸に対する指標の感度です。値が大きい軸ほど配置誤差の影響が大きいです。'
        )
        self.robustness_distribution_plot.setToolTip(
            'ばらつき評価点での指標値の頻度表示です。確率分布ではなく、有限サンプルの度数です。'
        )
        self._robustness_presentations = ()
        self._robustness_row_payload: dict[int, tuple[object, object]] = {}
        self.robustness_viewport = None
        self._render_robustness_document = None
        self._robustness_actor_names: set[str] = set()

    def bind_robustness_viewport(self, viewport, render_document) -> None:
        self.robustness_viewport = viewport
        self._render_robustness_document = render_document

    @staticmethod
    def _render_point(point: tuple[float, float, float]) -> tuple[float, float, float]:
        return (float(point[0]), -float(point[1]), float(point[2]))

    def _remove_robustness_overlays(self) -> None:
        if self.robustness_viewport is None:
            self._robustness_actor_names.clear()
            return
        for name in tuple(self._robustness_actor_names):
            try:
                self.robustness_viewport.remove_actor(
                    name,
                    reset_camera=False,
                    render=False,
                )
            except EXPECTED_OPERATION_ERRORS:  # error-boundary: overlay teardown — a dead/renamed actor must not abort the rebuild; expected adapter failures are absorbed by design
                pass
        self._robustness_actor_names.clear()

    def _render_robustness_overlay(self, candidate_id: str) -> None:
        if (
            self.robustness_viewport is None
            or self._render_robustness_document is None
            or self.search_selected_spec_id is None
        ):
            return
        search_spec = self.search_repository.get(self.search_selected_spec_id)
        if search_spec is None:
            return
        specs = self.robustness_repository.list_specs_for_candidate(
            document_id=self.document_id,
            scene_revision_id=search_spec.scene_revision_id,
            candidate_id=candidate_id,
        )
        if not specs:
            return
        spec = specs[-1]
        source_revision = self.repository.get(spec.scene_revision_id)
        if source_revision is None:
            return
        samples = self.robustness_repository.list_samples(
            spec.robustness_spec_id
        )
        try:
            overlay = build_robustness_overlay_model(
                source_revision=source_revision,
                spec=spec,
                samples=samples,
            )
        except ValueError as exc:
            self.robustness_detail_label.setText(
                f'3Dばらつき表示を構築できません: {operation_error_message(exc)}'
            )
            return

        self._render_robustness_document(overlay.document)
        self._remove_robustness_overlays()

        for index, item in enumerate(overlay.position_tolerances):
            line_name = f'robust-position-line:{index}'
            nominal_name = f'robust-position-nominal:{index}'
            minimum_name = f'robust-position-min:{index}'
            maximum_name = f'robust-position-max:{index}'
            self._robustness_actor_names.update(
                (line_name, nominal_name, minimum_name, maximum_name)
            )
            self.robustness_viewport.add_mesh(
                pv.Line(
                    self._render_point(item.minimum),
                    self._render_point(item.maximum),
                ),
                name=line_name,
                line_width=4,
                pickable=False,
                render=False,
            )
            for name, point, radius in (
                (minimum_name, item.minimum, 0.035),
                (nominal_name, item.nominal, 0.055),
                (maximum_name, item.maximum, 0.035),
            ):
                self.robustness_viewport.add_mesh(
                    pv.Sphere(
                        radius=radius,
                        center=self._render_point(point),
                    ),
                    name=name,
                    style='wireframe',
                    line_width=3,
                    pickable=False,
                    render=False,
                )

        for index, item in enumerate(overlay.angular_tolerances):
            for suffix, endpoint, width in (
                ('minus', item.minus_endpoint, 2),
                ('nominal', item.nominal_endpoint, 4),
                ('plus', item.plus_endpoint, 2),
            ):
                name = f'robust-angle-{suffix}:{index}'
                self._robustness_actor_names.add(name)
                self.robustness_viewport.add_mesh(
                    pv.Line(
                        self._render_point(item.origin),
                        self._render_point(endpoint),
                    ),
                    name=name,
                    line_width=width,
                    pickable=False,
                    render=False,
                )

        for index, item in enumerate(overlay.infeasible_markers):
            name = f'robust-infeasible:{index}'
            self._robustness_actor_names.add(name)
            self.robustness_viewport.add_mesh(
                pv.Sphere(
                    radius=0.075,
                    center=self._render_point(item.point),
                ),
                name=name,
                style='wireframe',
                line_width=5,
                pickable=False,
                render=False,
            )

        label_name = 'robust-overlay-label'
        self._robustness_actor_names.add(label_name)
        self.robustness_viewport.add_text(
            'ばらつき範囲 / 照準 · 太線マーカー=制約外評価点 · シーン未変更',
            position='upper_right',
            font_size=9,
            name=label_name,
            render=False,
        )
        self.robustness_viewport.render()

    def refresh_robustness_view(self) -> None:
        tree = self.robustness_tree
        tree.clear()
        comparison_tree = self.robustness_comparison_tree
        comparison_tree.clear()
        comparison_tree.setHeaderLabels(['項目'])
        self._robustness_row_payload.clear()
        self._robustness_presentations = ()

        search_spec_id = self.search_selected_spec_id
        if search_spec_id is None:
            self.robustness_summary_label.setText(
                '探索設定を選択してください。ばらつき評価は保存済み探索設定と候補に固定されています。'
            )
            self.robustness_probability_label.setText(
                '確率指標は、明示的な確率分布または重みがある場合だけ表示します'
            )
            return

        search_spec = self.search_repository.get(search_spec_id)
        if search_spec is None:
            self.robustness_summary_label.setText(
                '選択した探索設定を再解決できません。再読み込みしてください。'
            )
            return

        current_revision = self.repository.current_head(self.document_id)
        _, current_constraint_workspace_hash = constraint_workspace_snapshot(
            self.constraint_set
        )
        nominal_evaluations = self.objective_repository.latest_evaluations_by_candidate(
            search_spec_id
        )
        presentations = []
        candidate_display = {
            evaluation.candidate_id: f'候補 {index}'
            for index, evaluation in enumerate(nominal_evaluations, start=1)
        }

        for nominal in nominal_evaluations:
            specs = self.robustness_repository.list_specs_for_candidate(
                document_id=self.document_id,
                scene_revision_id=search_spec.scene_revision_id,
                candidate_id=nominal.candidate_id,
            )
            if not specs:
                continue
            robust_spec = specs[-1]
            evaluations = self.robustness_repository.list_evaluations(
                robust_spec.robustness_spec_id
            )
            samples = self.robustness_repository.list_samples(
                robust_spec.robustness_spec_id
            )
            exact_nominal = self.objective_repository.get_evaluation(
                robust_spec.nominal_objective_evaluation_id
            )
            presentation = build_robustness_candidate_presentation(
                spec=robust_spec,
                evaluations=evaluations,
                samples=samples,
                current_revision=current_revision,
                current_search_spec=search_spec,
                nominal_objective=exact_nominal,
                current_constraint_workspace_hash=current_constraint_workspace_hash,
            )
            presentations.append(presentation)
            label = candidate_display.get(nominal.candidate_id, '候補')
            for metric in presentation.objectives:
                unit = metric.unit
                sensitivity = (
                    '—'
                    if metric.sensitivity_value is None
                    else f'{metric.sensitivity_value:.3g} {unit}/単位'
                )
                status = presentation.completeness.state_label
                if not presentation.current:
                    status = '要再評価'
                row = QTreeWidgetItem(
                    [
                        label,
                        metric.objective_label,
                        metric.direction_label,
                        f'{metric.nominal_value:.3g} {unit}',
                        f'{metric.sampled_adverse_value:.3g} {unit}',
                        sensitivity,
                        status,
                    ]
                )
                row.setData(0, ROLE, nominal.candidate_id)
                tree.addTopLevelItem(row)
                self._robustness_row_payload[id(row)] = (presentation, metric)
                if nominal.candidate_id == self.search_selected_candidate_id:
                    row.setSelected(True)

        self._robustness_presentations = tuple(presentations)
        if not presentations:
            self.robustness_summary_label.setText(
                'この探索設定には保存済みのばらつき評価がありません。'
                ' 既存O90実行ワークフローで評価を作成してください。'
            )
            return

        labels = [
            candidate_display.get(item.candidate_id, f'候補 {index}')
            for index, item in enumerate(presentations, start=1)
        ]
        comparison_tree.setColumnCount(1 + len(labels))
        comparison_tree.setHeaderLabels(['項目', *labels])
        if presentations:
            first = presentations[0]
            objective_ids = [item.objective_id for item in first.objectives]
            for objective_id in objective_ids:
                row_metrics = []
                for candidate in presentations:
                    metric = next(
                        (
                            item
                            for item in candidate.objectives
                            if item.objective_id == objective_id
                        ),
                        None,
                    )
                    row_metrics.append(metric)
                first_metric = row_metrics[0]
                if first_metric is None:
                    continue
                rows = (
                    (
                        f'{first_metric.objective_label} / Nominal',
                        lambda metric: (
                            '—'
                            if metric is None
                            else f'{metric.nominal_value:.3g} {metric.unit}'
                        ),
                    ),
                    (
                        f'{first_metric.objective_label} / 評価サンプル内の不利側最大値',
                        lambda metric: (
                            '—'
                            if metric is None
                            else f'{metric.sampled_adverse_value:.3g} {metric.unit}'
                        ),
                    ),
                    (
                        f'{first_metric.objective_label} / 感度',
                        lambda metric: (
                            '—'
                            if metric is None or metric.sensitivity_value is None
                            else f'{metric.sensitivity_value:.3g} {metric.unit}/単位'
                        ),
                    ),
                    (
                        f'{first_metric.objective_label} / p95',
                        lambda metric: (
                            '未対応'
                            if metric is None or not metric.percentile_supported
                            else (
                                '—'
                                if metric.percentile_p95 is None
                                else f'{metric.percentile_p95:.3g} {metric.unit}'
                            )
                        ),
                    ),
                    (
                        f'{first_metric.objective_label} / 制約違反確率',
                        lambda metric: (
                            '未対応'
                            if metric is None or not metric.probability_supported
                            else (
                                '—'
                                if metric.violation_probability is None
                                else f'{metric.violation_probability * 100.0:.1f}%'
                            )
                        ),
                    ),
                )
                for label, formatter in rows:
                    comparison_tree.addTopLevelItem(
                        QTreeWidgetItem(
                            [label, *(formatter(metric) for metric in row_metrics)]
                        )
                    )
            completeness_rows = (
                ('評価サンプル数', lambda item: str(item.completeness.sample_count)),
                ('実行可能', lambda item: str(item.completeness.feasible_count)),
                ('制約外', lambda item: str(item.completeness.infeasible_count)),
                ('予測失敗', lambda item: str(item.completeness.failed_prediction_count)),
                ('未対応', lambda item: str(item.completeness.unsupported_count)),
                ('評価状況', lambda item: item.completeness.state_label),
            )
            for label, formatter in completeness_rows:
                comparison_tree.addTopLevelItem(
                    QTreeWidgetItem(
                        [label, *(formatter(item) for item in presentations)]
                    )
                )

        eligibility = robustness_comparison_eligibility(
            tuple(presentations)
        )
        if eligibility.eligible:
            self.robustness_summary_label.setText(
                '候補間の比較条件が一致しています。ノミナル・ばらつき・制約・評価完全性を独立して比較できます。'
            )
        else:
            self.robustness_summary_label.setText(
                '比較不可: ' + ' / '.join(eligibility.reasons)
            )

        unsupported = []
        for candidate in presentations:
            for metric in candidate.objectives:
                if not metric.percentile_supported and metric.percentile_reason:
                    unsupported.append(metric.percentile_reason)
        if unsupported:
            self.robustness_probability_label.setText(
                ' / '.join(dict.fromkeys(unsupported))
            )
        else:
            self.robustness_probability_label.setText(
                '確率モデルあり: p95・制約違反確率を表示できます。'
            )

        if tree.topLevelItemCount() and not tree.selectedItems():
            tree.topLevelItem(0).setSelected(True)
        if tree.selectedItems():
            self._robustness_row_selected()

    def _robustness_row_selected(self) -> None:
        selected = self.robustness_tree.selectedItems()
        if not selected:
            self.robustness_detail_label.setText('ばらつき評価が未選択です')
            self.robustness_advanced_label.setText('内部権威情報は未選択です')
            return
        payload = self._robustness_row_payload.get(id(selected[0]))
        if payload is None:
            return
        candidate, metric = payload
        completeness = candidate.completeness
        probability = (
            '未対応'
            if not metric.probability_supported
            else (
                '—'
                if metric.violation_probability is None
                else f'{metric.violation_probability * 100.0:.1f}%'
            )
        )
        p95 = (
            '未対応'
            if not metric.percentile_supported
            else (
                '—'
                if metric.percentile_p95 is None
                else f'{metric.percentile_p95:.3g} {metric.unit}'
            )
        )
        self.robustness_sensitivity_plot.clear()
        sensitivities = [
            item
            for item in metric.axis_sensitivities
            if item.magnitude_per_unit is not None
        ]
        if sensitivities:
            x_values = np.arange(len(sensitivities), dtype=float)
            y_values = np.asarray(
                [float(item.magnitude_per_unit) for item in sensitivities],
                dtype=float,
            )
            self.robustness_sensitivity_plot.plot(
                x_values,
                y_values,
                symbol='o',
            )
            self.robustness_sensitivity_plot.getAxis('bottom').setTicks(
                [
                    [
                        (float(index), item.label)
                        for index, item in enumerate(sensitivities)
                    ]
                ]
            )
            self.robustness_sensitivity_plot.setLabel(
                'left',
                f'感度 ({metric.unit}/入力単位)',
            )

        self.robustness_distribution_plot.clear()
        values = np.asarray(metric.sampled_values, dtype=float)
        probability_mass = False
        histogram_weights = None
        if (
            metric.probability_supported
            and len(metric.sampled_weights) == len(metric.sampled_values)
            and metric.sampled_weights
            and all(weight is not None for weight in metric.sampled_weights)
        ):
            raw_weights = np.asarray(metric.sampled_weights, dtype=float)
            mass = float(raw_weights.sum())
            if mass > 0.0:
                histogram_weights = raw_weights / mass
                probability_mass = True
        if len(values):
            bin_count = max(1, min(12, int(np.ceil(np.sqrt(len(values))))))
            counts, edges = np.histogram(
                values,
                bins=bin_count,
                weights=histogram_weights,
            )
            centers = (edges[:-1] + edges[1:]) / 2.0
            widths = edges[1:] - edges[:-1]
            for center, count, width in zip(
                centers,
                counts,
                widths,
                strict=True,
            ):
                self.robustness_distribution_plot.addItem(
                    pg.BarGraphItem(
                        x=[float(center)],
                        height=[float(count)],
                        width=max(float(width) * 0.9, 1e-9),
                        brush=pg.mkBrush(128, 128, 128, 150),
                    )
                )
            self.robustness_distribution_plot.setLabel(
                'bottom',
                f'評価値 ({metric.unit})',
            )
            self.robustness_distribution_plot.setLabel(
                'left',
                (
                    '条件付き確率質量'
                    if probability_mass
                    else 'サンプル数'
                ),
            )
        self.robustness_distribution_note_label.setText(
            (
                '実行可能な評価点に条件付けた確率質量です。制約違反確率は別指標として表示します。'
                if probability_mass
                else '有限サンプルの頻度表示です。頻度を確率として解釈しません。'
            )
        )

        details = [
            f'Nominal: {metric.nominal_value:.3g} {metric.unit}',
            (
                f'{metric.sampled_adverse_label}: '
                f'{metric.sampled_adverse_value:.3g} {metric.unit}'
            ),
            (
                '平均: 未対応'
                if metric.mean_value is None
                else f'平均: {metric.mean_value:.3g} {metric.unit}'
            ),
            (
                '中央値: 未対応'
                if metric.median_value is None
                else f'中央値: {metric.median_value:.3g} {metric.unit}'
            ),
            f'p95: {p95}',
            f'制約違反確率: {probability}',
            (
                f'評価: {completeness.sample_count}件 / '
                f'実行可能 {completeness.feasible_count} / '
                f'制約外 {completeness.infeasible_count} / '
                f'予測失敗 {completeness.failed_prediction_count} / '
                f'未対応 {completeness.unsupported_count}'
            ),
        ]
        if metric.feasible_fraction is not None:
            details.append(
                f'実行可能割合: {metric.feasible_fraction * 100.0:.1f}%'
            )
        details.extend(candidate.stale_reasons)
        details.extend(completeness.reasons)
        if metric.percentile_reason and not metric.percentile_supported:
            details.append(metric.percentile_reason)
        self.robustness_detail_label.setText('\n'.join(dict.fromkeys(details)))
        self._render_robustness_overlay(candidate.candidate_id)
        self.robustness_advanced_label.setText(
            '\n'.join(
                (
                    f'堅牢性仕様: {candidate.spec_id}',
                    f'シーンリビジョン: {candidate.scene_revision_id}',
                    f'探索仕様: {candidate.search_spec_id}',
                    f'モデル: {candidate.model_id} / {candidate.model_version}',
                    f'プロバイダー: {candidate.prediction_provider_id}',
                    f'忠実度: {candidate.fidelity}',
                    (
                        '指標権威: '
                        f'{candidate.objective_evaluation_spec_sha256}'
                    ),
                )
            )
        )
