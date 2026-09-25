from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from threading import Event
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QSignalBlocker, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QListWidget,
    QListWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analysis_markers import render_analysis_marker_cloud
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedCandidateSetPage,
    CadExtendedSearchAxis,
    aim_horizontal_yaw_deg,
    aim_pitch_deg,
    body_horizontal_yaw_deg,
    apply_extended_candidate,
    build_extended_model_capability,
    build_extended_parameter_evidence,
    build_extended_search_spec,
    extended_candidate_preview_document,
    generate_extended_candidates,
)
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_objectives import build_pareto_set
from .cad_objective_repository import CadObjectiveRepository
from .cad_scene import F1_DOCUMENT_ID
from .cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    generate_cad_candidates,
    search_spec_current_working,
)
from .cad_search_models import CadCandidate, CadCandidateSetPage, CadSearchAxis, CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .measurement_workspace import _MeasurementScrollArea
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_model_validation_service import CadModelValidationService
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_measurement_loop import build_measurement_plan, complete_measurement_plan
from .cad_validation_campaign import (
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
)
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_campaign_service import CadValidationCampaignService
from .cad_validation_metrics import CadApplicabilityCheck
from .native_editor import ROLE
from .native_worker import WORKER_CANCELLED


class ExtendedSearchControllerMixin:
    def _refresh_extended_entities(self) -> None:
        combo = self.extended_entity_combo
        if combo is None or self.working is None:
            return
        previous = combo.currentData()
        with QSignalBlocker(combo):
            combo.clear()
            for entity in self.working.committed_document.entities:
                if entity.kind == 'speaker' and entity.aim_xyz is not None:
                    try:
                        yaw = aim_horizontal_yaw_deg(entity.aim_xyz)
                    except ValueError:
                        yaw = None
                    pitch = aim_pitch_deg(entity.aim_xyz)
                    heading = (
                        f'yaw {yaw:.1f}°'
                        if yaw is not None
                        else 'yaw undefined (vertical aim)'
                    )
                    combo.addItem(
                        f'{entity.name} · {heading} · pitch {pitch:.1f}°',
                        entity.entity_id,
                    )
            if previous is not None:
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)
        self._seed_extended_aim_range()

    def _seed_extended_aim_range(self) -> None:
        if (
            self.extended_entity_combo is None
            or self.extended_parameter_combo is None
            or self.extended_min_field is None
            or self.extended_max_field is None
            or self.working is None
        ):
            return
        entity_id = self.extended_entity_combo.currentData()
        parameter = self.extended_parameter_combo.currentData()
        if not isinstance(entity_id, str) or not isinstance(parameter, str):
            return
        try:
            entity = self.working.committed_document.entity(entity_id)
            if parameter == 'body_yaw_deg':
                center, bound = body_horizontal_yaw_deg(entity), 180.0
            elif entity.aim_xyz is not None:
                if parameter == 'aim_pitch_deg':
                    center, bound = aim_pitch_deg(entity.aim_xyz), 89.0
                else:
                    center, bound = (
                        aim_horizontal_yaw_deg(entity.aim_xyz),
                        180.0,
                    )
            else:
                return
        except (KeyError, ValueError):
            return
        self.extended_min_field.setValue(max(-bound, center - 15.0))
        self.extended_max_field.setValue(min(bound, center + 15.0))

    def _refresh_extended_capabilities(
        self,
        *,
        select_capability_id: str | None = None,
    ) -> None:
        combo = self.extended_capability_combo
        if combo is None:
            return
        previous = (
            select_capability_id
            if select_capability_id is not None
            else combo.currentData()
        )
        with QSignalBlocker(combo):
            combo.clear()
            for capability in self.extended_repository.list_capabilities():
                combo.addItem(
                    f'{capability.evidence_scope} · '
                    f'{capability.model_id}/{capability.model_version}',
                    capability.capability_id,
                )
            if previous is not None:
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)

    def create_synthetic_extended_capability(self) -> None:
        try:
            evidence = tuple(
                build_extended_parameter_evidence(
                    parameter=parameter,
                    model_id='synthetic-directional-fixture',
                    model_version='1',
                    evidence_scope='synthetic_fixture',
                    tested_min_deg=-89.0 if parameter == 'aim_pitch_deg' else -180.0,
                    tested_max_deg=89.0 if parameter == 'aim_pitch_deg' else 180.0,
                    source_kind='synthetic_fixture',
                    source_id=(
                        'synthetic-directional-evidence:' + parameter
                    ),
                    source_sha256=sha256(
                        (
                            'htdt-synthetic-extended-parameter-evidence:'
                            + parameter
                        ).encode('utf-8')
                    ).hexdigest(),
                    detail='declared synthetic fixture evidence; not owned-room validation',
                    created_at_utc=datetime.now(timezone.utc).isoformat(),
                )
                for parameter in ('aim_yaw_deg', 'aim_pitch_deg', 'body_yaw_deg')
            )
            for item in evidence:
                existing_evidence = next(
                    (
                        stored
                        for stored in self.extended_repository.list_parameter_evidence()
                        if stored.evidence_sha256 == item.evidence_sha256
                    ),
                    None,
                )
                if existing_evidence is None:
                    self.extended_repository.save_parameter_evidence(item)
            built = build_extended_model_capability(
                model_id='synthetic-directional-fixture',
                model_version='1',
                evidence_scope='synthetic_fixture',
                supported_parameters=('aim_yaw_deg', 'aim_pitch_deg', 'body_yaw_deg'),
                parameter_evidence=evidence,
                detail='software acceptance for acoustic aim (yaw/pitch) and physical body yaw; not owned-room evidence',
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            existing = next(
                (
                    item
                    for item in self.extended_repository.list_capabilities()
                    if item.capability_sha256 == built.capability_sha256
                ),
                None,
            )
            capability = existing or built
            if existing is None:
                self.extended_repository.save_capability(capability)
        except Exception as exc:
            self.statusBar().showMessage(
                f'Synthetic extended capabilityを保存できません · {exc}'
            )
            return
        self._refresh_extended_capabilities(
            select_capability_id=capability.capability_id
        )
        self.statusBar().showMessage(
            'Synthetic acoustic-aim/body-yaw capabilityを保存しました · 開発受入専用です'
        )

    def create_owned_room_extended_capability(self) -> None:
        record = self._selected_validation_record()
        if record is None:
            self.statusBar().showMessage(
                'owned-room capabilityにはValidationRecordを選択してください'
            )
            return
        # #384: an owned-room capability needs exact per-parameter evidence —
        # generic O60 model eligibility alone can never authorize a
        # directional parameter. Gather the persisted owned-room evidence
        # records for this exact model/version first; a missing binding
        # fails closed with a status message.
        parameters = ['aim_yaw_deg', 'body_yaw_deg']
        try:
            stored = self.extended_repository.list_parameter_evidence()
            # Pitch is additive: an owned-room capability claims it only when
            # exact owned-room evidence for this model/version already exists.
            if any(
                item.parameter == 'aim_pitch_deg'
                and item.model_id == record.model_id
                and item.model_version == record.model_version
                and item.evidence_scope == 'owned_room'
                for item in stored
            ):
                parameters.insert(1, 'aim_pitch_deg')
            parameters = tuple(parameters)
        except Exception as exc:
            self.statusBar().showMessage(
                f'extended parameter evidenceを読み込めません · {exc}'
            )
            return
        evidence = []
        missing = []
        for parameter in parameters:  # noqa: B007 - ordered parameters tuple
            match = next(
                (
                    item
                    for item in stored
                    if item.parameter == parameter
                    and item.model_id == record.model_id
                    and item.model_version == record.model_version
                    and item.evidence_scope == 'owned_room'
                ),
                None,
            )
            if match is None:
                missing.append(parameter)
            else:
                evidence.append(match)
        if missing:
            self.statusBar().showMessage(
                'owned-room directional evidenceが未登録です · '
                + ', '.join(missing)
                + ' — O90E eligible decision由来のparameter evidenceが必要です'
            )
            return
        try:
            capability = build_extended_model_capability(
                model_id=record.model_id,
                model_version=record.model_version,
                evidence_scope='owned_room',
                supported_parameters=parameters,
                parameter_evidence=tuple(evidence),
                detail='owned-room validated directional aim (yaw/pitch)/body-yaw capability',
                validation=record,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            existing = next(
                (
                    item
                    for item in self.extended_repository.list_capabilities()
                    if item.capability_sha256 == capability.capability_sha256
                ),
                None,
            )
            if existing is None:
                self.extended_repository.save_capability(capability)
            else:
                capability = existing
        except Exception as exc:
            self.statusBar().showMessage(
                f'owned-room extended capabilityを保存できません · {exc}'
            )
            return
        self._refresh_extended_capabilities(
            select_capability_id=capability.capability_id
        )
        self.statusBar().showMessage(
            f'owned-room extended capabilityを保存しました · '
            f'{capability.capability_id[:8]}'
        )

    def add_or_update_extended_axis(self) -> None:
        if (
            self.extended_entity_combo is None
            or self.extended_parameter_combo is None
            or self.extended_min_field is None
            or self.extended_max_field is None
            or self.extended_step_field is None
        ):
            return
        entity_id = self.extended_entity_combo.currentData()
        parameter = self.extended_parameter_combo.currentData()
        if not isinstance(entity_id, str) or not isinstance(parameter, str):
            self.statusBar().showMessage(
                'extended軸へ追加するexplicit-aim speaker/parameterを選択してください'
            )
            return
        try:
            axis = CadExtendedSearchAxis(
                entity_id=entity_id,
                parameter=parameter,
                min_value=float(self.extended_min_field.value()),
                max_value=float(self.extended_max_field.value()),
                step=float(self.extended_step_field.value()),
            )
        except Exception as exc:
            self.statusBar().showMessage(f'extended軸が不正です · {exc}')
            return
        key = (entity_id, parameter)
        self.extended_axes[key] = axis
        self._refresh_extended_axis_tree()
        self.statusBar().showMessage(
            f'extended軸を追加/更新しました · {entity_id} · {parameter}'
        )

    def remove_selected_extended_axis(self) -> None:
        tree = self.extended_axis_tree
        if tree is None:
            return
        item = tree.currentItem()
        raw_key = None if item is None else item.data(0, ROLE)
        if (
            not isinstance(raw_key, (tuple, list))
            or len(raw_key) != 2
        ):
            return
        key = (str(raw_key[0]), str(raw_key[1]))
        self.extended_axes.pop(key, None)
        self._refresh_extended_axis_tree()
        self.statusBar().showMessage(
            f'extended軸を削除しました · {key[0]} · {key[1]}'
        )

    def _refresh_extended_axis_tree(self) -> None:
        tree = self.extended_axis_tree
        if tree is None:
            return
        tree.clear()
        for key in sorted(self.extended_axes):
            axis = self.extended_axes[key]
            entity_id = axis.entity_id
            name = entity_id
            if self.working is not None:
                try:
                    name = self.working.committed_document.entity(entity_id).name
                except KeyError:
                    pass
            item = QTreeWidgetItem([
                name,
                axis.parameter,
                f'{axis.min_value:.1f}°',
                f'{axis.max_value:.1f}°',
                f'{axis.step:.1f}°',
            ])
            item.setData(0, ROLE, key)
            tree.addTopLevelItem(item)

    def _selected_extended_capability(self):
        combo = self.extended_capability_combo
        if combo is None:
            return None
        capability_id = combo.currentData()
        if not isinstance(capability_id, str):
            return None
        return self.extended_repository.get_capability(capability_id)

    def save_extended_search_spec(self) -> None:
        base = self._selected_search_spec()
        page = self.search_candidate_page
        capability = self._selected_extended_capability()
        if (
            base is None
            or page is None
            or capability is None
            or self.working is None
        ):
            self.statusBar().showMessage(
                'base SearchSpec候補とmodel capabilityを先に準備してください'
            )
            return
        if (
            page.search_spec_id != base.search_spec_id
            or not search_spec_current_working(
                base,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.statusBar().showMessage(
                'currentなbase SearchSpec候補だけをExtended Searchへ使えます'
            )
            return
        if self.extended_entity_combo is None:
            return
        entity_id = self.extended_entity_combo.currentData()
        if not isinstance(entity_id, str):
            self.statusBar().showMessage(
                'explicit aimを持つspeakerを選択してください'
            )
            return
        if not self.extended_axes:
            self.statusBar().showMessage(
                '1つ以上のextended軸を追加してからExtended SearchSpecを保存してください'
            )
            return
        try:
            revision = self.repository.get(base.scene_revision_id)
            if revision is None:
                raise ValueError('base SceneRevision does not exist')
            candidate_limit = (
                10_000
                if self.extended_limit_field is None
                else int(self.extended_limit_field.value())
            )
            spec = build_extended_search_spec(
                source_revision=revision,
                base_spec=base,
                base_candidate_set_sha256=page.candidate_set_sha256,
                base_candidate_count=page.feasible_candidate_count,
                capability=capability,
                axes=tuple(
                    self.extended_axes[key]
                    for key in sorted(self.extended_axes)
                ),
                candidate_limit=candidate_limit,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            self.extended_repository.save_spec(spec)
        except Exception as exc:
            self.statusBar().showMessage(
                f'Extended SearchSpecを保存できません · {exc}'
            )
            return

        self.extended_selected_spec_id = spec.extended_search_id
        self.extended_candidate_page = None
        self.extended_selected_candidate_id = None
        self.extended_preview_candidate_id = None
        self._refresh_extended_specs()
        self._remove_extended_overlays()
        self.statusBar().showMessage(
            f'Extended SearchSpecを保存しました · {spec.extended_search_id[:8]}'
        )

    def _refresh_extended_specs(self) -> None:
        tree = self.extended_spec_tree
        if tree is None:
            return
        base = self._selected_search_spec()
        with QSignalBlocker(tree):
            tree.clear()
            if base is None:
                self.extended_selected_spec_id = None
                return
            specs = self.extended_repository.list_for_base_search(
                base.search_spec_id
            )
            valid_ids = {item.extended_search_id for item in specs}
            if self.extended_selected_spec_id not in valid_ids:
                self.extended_selected_spec_id = (
                    specs[-1].extended_search_id if specs else None
                )
            selected_item = None
            current = (
                self.working is not None
                and search_spec_current_working(
                    base,
                    self.working,
                    self.constraint_set,
                    current_document_id=self.document_id,
                )
            )
            for spec in reversed(specs):
                capability = self.extended_repository.get_capability(
                    spec.capability_id
                )
                model_text = (
                    'missing capability'
                    if capability is None
                    else (
                        f'{capability.evidence_scope} · '
                        f'{capability.model_id}/{capability.model_version}'
                    )
                )
                axes_text = ', '.join(
                    f'{axis.entity_id}:{axis.parameter}'
                    for axis in spec.axes
                )
                item = QTreeWidgetItem([
                    spec.extended_search_id[:8],
                    model_text,
                    axes_text,
                    'current' if current else 'stale',
                ])
                item.setData(0, ROLE, spec.extended_search_id)
                tree.addTopLevelItem(item)
                if spec.extended_search_id == self.extended_selected_spec_id:
                    selected_item = item
            if selected_item is not None:
                tree.setCurrentItem(selected_item)
        self._refresh_extended_binding_state()

    def _extended_spec_selected(self) -> None:
        tree = self.extended_spec_tree
        if tree is None:
            return
        item = tree.currentItem()
        spec_id = None if item is None else item.data(0, ROLE)
        normalized = None if spec_id is None else str(spec_id)
        if normalized != self.extended_selected_spec_id:
            self.extended_candidate_page = None
            self.extended_selected_candidate_id = None
            self.extended_preview_candidate_id = None
        self.extended_selected_spec_id = normalized
        self._refresh_extended_candidate_tree()
        self._refresh_extended_binding_state()
        self._render_extended_overlay()
        self.refresh_adaptive_extended_plans()

    def _selected_extended_spec(self):
        if self.extended_selected_spec_id is None:
            return None
        return self.extended_repository.get_spec(
            self.extended_selected_spec_id
        )

    def _selected_extended_candidate(self) -> CadExtendedCandidate | None:
        page = self.extended_candidate_page
        candidate_id = self.extended_selected_candidate_id
        if page is None or candidate_id is None:
            return None
        return next(
            (
                candidate
                for candidate in page.candidates
                if candidate.candidate_id == candidate_id
            ),
            None,
        )

    def _refresh_extended_binding_state(self) -> None:
        base = self._selected_search_spec()
        spec = self._selected_extended_spec()
        current = (
            base is not None
            and spec is not None
            and self.working is not None
            and spec.base_search_spec_id == base.search_spec_id
            and spec.base_search_spec_sha256 == base.search_spec_sha256
            and search_spec_current_working(
                base,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        )
        busy = (
            self._current_search_task_id is not None
            or self._current_extended_task_id is not None
        )
        extended_busy = self._current_extended_task_id is not None
        if self.extended_generate_button is not None:
            self.extended_generate_button.setEnabled(not busy and current)
        if self.extended_cancel_button is not None:
            self.extended_cancel_button.setEnabled(extended_busy)
        candidate = self._selected_extended_candidate()
        if self.extended_preview_button is not None:
            self.extended_preview_button.setEnabled(
                current and candidate is not None
            )
        if self.extended_apply_button is not None:
            self.extended_apply_button.setEnabled(
                current and candidate is not None
            )
        if self.adaptive_extended_build_button is not None:
            self.adaptive_extended_build_button.setEnabled(
                current and not busy
            )
        if self.extended_clear_preview_button is not None:
            self.extended_clear_preview_button.setEnabled(
                self.extended_preview_candidate_id is not None
            )
        page = self.extended_candidate_page
        if self.extended_prev_button is not None:
            self.extended_prev_button.setEnabled(
                not busy and current and page is not None and page.offset > 0
            )
        if self.extended_next_button is not None:
            self.extended_next_button.setEnabled(
                not busy
                and current
                and page is not None
                and page.offset + len(page.candidates)
                < page.feasible_candidate_count
            )

    def generate_extended_candidates_async(
        self,
        *,
        offset: int | None = None,
    ) -> None:
        if (
            self._current_search_task_id is not None
            or self._current_extended_task_id is not None
        ):
            return
        base = self._selected_search_spec()
        spec = self._selected_extended_spec()
        if base is None or spec is None or self.working is None:
            self.statusBar().showMessage(
                '生成するExtended SearchSpecを選択してください'
            )
            return
        if (
            spec.base_search_spec_id != base.search_spec_id
            or not search_spec_current_working(
                base,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.statusBar().showMessage(
                'staleなbase SearchSpecからextended候補を生成できません'
            )
            return

        page_offset = 0 if offset is None else max(0, int(offset))
        key = str(uuid4())
        self._current_extended_task_id = key
        self._extended_task_spec_ids[key] = (
            base.search_spec_id,
            spec.extended_search_id,
        )
        self._refresh_search_binding_state()
        self._refresh_extended_binding_state()
        self.statusBar().showMessage(
            f'Extended候補生成中… {spec.extended_search_id[:8]} · '
            f'offset {page_offset}'
        )
        self._start_extended_task(
            key,
            lambda cancel_event: generate_extended_candidates(
                self.repository,
                base,
                spec,
                offset=page_offset,
                limit=self.search_page_limit,
                cancelled=cancel_event.is_set,
            ),
        )

    def _start_extended_task(
        self,
        key: str,
        operation: Callable[[Event], object],
    ) -> None:
        if self._disposed:
            return
        self._extended_pool.start(
            key,
            operation,
            self._extended_task_completed,
        )

    def cancel_extended_generation(self) -> None:
        key = self._current_extended_task_id
        if key is None:
            return
        self._extended_pool.cancel(key)
        self.statusBar().showMessage(
            'Extended候補生成をキャンセルしています…'
        )

    @Slot(object, object, object)
    def _extended_task_completed(
        self,
        key: object,
        result: object,
        error: object,
    ) -> None:
        if self._disposed:
            return
        task_id = str(key)
        authority = self._extended_task_spec_ids.pop(task_id, None)
        # The pool keeps the task record until QThread.finished so the worker
        # stays busy-visible during the completed -> finished interval.
        if self._current_extended_task_id == task_id:
            self._current_extended_task_id = None
        self._refresh_search_binding_state()

        if error == WORKER_CANCELLED:
            self.statusBar().showMessage(
                'Extended候補生成をキャンセルしました'
            )
            self._refresh_extended_binding_state()
            return
        if error is not None:
            self.statusBar().showMessage(
                f'Extended候補生成に失敗しました · {error}'
            )
            self._refresh_extended_binding_state()
            return
        if not isinstance(result, CadExtendedCandidateSetPage):
            self.statusBar().showMessage(
                'Extended候補生成結果を拒否しました · contract mismatch'
            )
            self._refresh_extended_binding_state()
            return
        if authority is None:
            self.statusBar().showMessage(
                'Extended候補生成結果を破棄しました · authority missing'
            )
            return
        base_id, extended_id = authority
        base = self.search_repository.get(base_id)
        spec = self.extended_repository.get_spec(extended_id)
        if (
            base is None
            or spec is None
            or self.working is None
            or not search_spec_current_working(
                base,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
            or self.search_selected_spec_id != base_id
            or self.extended_selected_spec_id != extended_id
        ):
            self.statusBar().showMessage(
                '古いExtended候補生成結果を破棄しました · authorityが変更されています'
            )
            self._refresh_extended_binding_state()
            return

        self.extended_candidate_page = result
        self.extended_preview_candidate_id = None
        self.extended_selected_candidate_id = (
            result.candidates[0].candidate_id
            if result.candidates
            else None
        )
        self._refresh_extended_candidate_tree()
        self._refresh_extended_binding_state()
        self._render_extended_overlay()
        if self.extended_summary_label is not None:
            self.extended_summary_label.setText(
                f'raw {result.raw_candidate_count} · '
                f'feasible {result.feasible_candidate_count} · '
                f'表示 {result.offset + 1 if result.candidates else 0}–'
                f'{result.offset + len(result.candidates)} · '
                f'set {result.candidate_set_sha256[:8]}'
            )
        self.statusBar().showMessage(
            f'Extended候補を生成しました · '
            f'{result.feasible_candidate_count}件'
        )

    def _refresh_extended_candidate_tree(self) -> None:
        tree = self.extended_candidate_tree
        if tree is None:
            return
        page = self.extended_candidate_page
        selected_item = None
        with QSignalBlocker(tree):
            tree.clear()
            if page is None:
                return
            for candidate in page.candidates:
                position_text = ' · '.join(
                    f'{entity_id}:('
                    f'{position["x_m"]:.2f},'
                    f'{position["y_m"]:.2f},'
                    f'{position["z_m"]:.2f})'
                    for entity_id, position
                    in sorted(candidate.positions.items())
                )
                aim_text = ' · '.join(
                    f'{entity_id}:{yaw:.1f}°'
                    for entity_id, yaw
                    in sorted(candidate.aim_yaw_deg.items())
                )
                body_text = ' · '.join(
                    f'{entity_id}:{yaw:.1f}°'
                    for entity_id, yaw
                    in sorted(candidate.body_yaw_deg.items())
                )
                item = QTreeWidgetItem([
                    candidate.candidate_id[:14],
                    candidate.base_candidate_id[:12],
                    position_text,
                    aim_text,
                    body_text,
                ])
                item.setData(0, ROLE, candidate.candidate_id)
                tree.addTopLevelItem(item)
                if candidate.candidate_id == self.extended_selected_candidate_id:
                    selected_item = item
            if selected_item is not None:
                tree.setCurrentItem(selected_item)

    def _extended_candidate_selected(self) -> None:
        tree = self.extended_candidate_tree
        if tree is None:
            return
        item = tree.currentItem()
        candidate_id = None if item is None else item.data(0, ROLE)
        self.extended_selected_candidate_id = (
            None if candidate_id is None else str(candidate_id)
        )
        self._refresh_extended_binding_state()
        self._render_extended_overlay()

    def previous_extended_page(self) -> None:
        page = self.extended_candidate_page
        if page is None:
            return
        self.generate_extended_candidates_async(
            offset=max(0, page.offset - self.search_page_limit)
        )

    def next_extended_page(self) -> None:
        page = self.extended_candidate_page
        if page is None:
            return
        next_offset = page.offset + self.search_page_limit
        if next_offset < page.feasible_candidate_count:
            self.generate_extended_candidates_async(offset=next_offset)

    def preview_selected_extended_candidate(self) -> None:
        candidate = self._selected_extended_candidate()
        if candidate is None:
            return
        self.extended_preview_candidate_id = candidate.candidate_id
        self._refresh_extended_binding_state()
        self._render_extended_overlay()
        self.statusBar().showMessage(
            'extended候補preview · Scene/Undo履歴は変更していません'
        )

    def clear_extended_preview(self) -> None:
        if self.extended_preview_candidate_id is None:
            return
        self.extended_preview_candidate_id = None
        self._refresh_extended_binding_state()
        self._render_extended_overlay()
        self.statusBar().showMessage('extended previewを解除しました')

    def apply_selected_extended_candidate(self) -> None:
        base = self._selected_search_spec()
        spec = self._selected_extended_spec()
        candidate = self._selected_extended_candidate()
        if (
            base is None
            or spec is None
            or candidate is None
            or self.working is None
        ):
            return
        try:
            changed = apply_extended_candidate(
                self.working,
                candidate,
                extended_spec=spec,
                base_spec=base,
                current_constraint_set=self.constraint_set,
                current_document_id=self.document_id,
            )
        except Exception as exc:
            self.statusBar().showMessage(
                f'extended候補を適用できません · {exc}'
            )
            self._refresh_extended_binding_state()
            return
        if not changed:
            self.statusBar().showMessage(
                'extended候補は現在の配置/body/aimと同一です'
            )
            return

        entity_ids = tuple(
            sorted(
                set(candidate.positions)
                | set(candidate.aim_yaw_deg)
                | set(candidate.body_yaw_deg)
            )
        )
        self.view_state.set_selection(
            entity_ids,
            primary_id=entity_ids[-1] if entity_ids else None,
        )
        self.selected_id = self.view_state.selected_id
        self.extended_preview_candidate_id = None
        self._sync_recovery()
        self._persist_view_state()
        self._rebuild()
        self._set_dirty_status()
        self._refresh_search_specs()
        self._refresh_extended_specs()
        self.statusBar().showMessage(
            '位置+body orientation+acoustic aimを1 commandで適用しました · '
            'Undoでまとめて復元できます'
        )

    def _remove_extended_overlays(self) -> None:
        for name in tuple(self._extended_actor_names):
            try:
                self.viewport.remove_actor(
                    name,
                    reset_camera=False,
                    render=False,
                )
            except Exception:
                pass
        self._extended_actor_names.clear()

    def _render_extended_overlay(self) -> None:
        if not hasattr(self, 'viewport'):
            return
        self._remove_extended_overlays()
        base = self._selected_search_spec()
        spec = self._selected_extended_spec()
        candidate = self._selected_extended_candidate()
        if (
            base is None
            or spec is None
            or candidate is None
            or self.working is None
            or not search_spec_current_working(
                base,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.viewport.render()
            return

        primary_entity_id = spec.axes[0].entity_id
        primary = candidate.positions.get(primary_entity_id)
        if primary is None:
            current = self.working.committed_document.entity(
                primary_entity_id
            ).position
            center = (
                float(current.x_m),
                -float(current.y_m),
                float(current.z_m),
            )
        else:
            center = (
                float(primary['x_m']),
                -float(primary['y_m']),
                float(primary['z_m']),
            )
        marker_name = 'extended-selected-candidate'
        self._extended_actor_names.add(marker_name)
        self.viewport.add_mesh(
            pv.Sphere(radius=0.065, center=center),
            name=marker_name,
            style='wireframe',
            line_width=4,
            pickable=False,
            render=False,
        )

        preview_candidate = (
            candidate
            if candidate.candidate_id == self.extended_preview_candidate_id
            else None
        )
        if preview_candidate is not None:
            try:
                preview = extended_candidate_preview_document(
                    self.working.committed_document,
                    preview_candidate,
                )
            except Exception:
                preview = None
            if preview is not None:
                for index, entity_id in enumerate(
                    sorted(preview_candidate.aim_yaw_deg)
                ):
                    entity = preview.entity(entity_id)
                    aim = entity.aim_xyz
                    if aim is None:
                        continue
                    start = (
                        float(entity.position.x_m),
                        -float(entity.position.y_m),
                        float(entity.position.z_m),
                    )
                    length = 0.6
                    end = (
                        start[0] + float(aim.x) * length,
                        start[1] - float(aim.y) * length,
                        start[2] + float(aim.z) * length,
                    )
                    line_name = f'extended-aim-line:{index}'
                    self._extended_actor_names.add(line_name)
                    self.viewport.add_mesh(
                        pv.Line(start, end),
                        name=line_name,
                        line_width=5,
                        pickable=False,
                        render=False,
                    )
                label_name = 'extended-preview-label'
                self._extended_actor_names.add(label_name)
                self.viewport.add_text(
                    'acoustic aim preview · Scene未変更',
                    position='upper_left',
                    font_size=9,
                    name=label_name,
                    render=False,
                )
        self.viewport.render()

