from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QToolBar

from .cad_document import EditStateError, EditorViewState
from .cad_objects import TheaterObjectError, speaker_aim_replacements
from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID, Position3, Size3, make_empty_scene, make_f1_scene
from .theater_document import TheaterWorkingDocument
from .theater_editor import TheaterEditorWindow


class TheaterWorkflowWindow(TheaterEditorWindow):
    """Product N40 workflow layer: explicit acoustic aiming plus theater editing."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        self.aim_action: QAction | None = None
        super().__init__(repository, document_id)

        toolbar = QToolBar('音響', self)
        self.addToolBar(toolbar)
        self.aim_action = QAction('座席へ向ける', self)
        self.aim_action.setToolTip(
            '座席または測定点を選択し、スピーカーの音響軸をその基準点へ向けます。'
            'スピーカーも複数選択している場合は選択したスピーカーだけを更新します。'
        )
        self.aim_action.triggered.connect(self.aim_speakers_at_selected_reference)
        toolbar.addAction(self.aim_action)
        self._update_actions()

    def _load_or_seed(self) -> None:
        revision = self.repository.current_head(self.document_id)
        if revision is None:
            seed = make_f1_scene() if self.document_id == F1_DOCUMENT_ID else make_empty_scene(self.document_id)
            revision = self.repository.save(seed, parent_revision_id=None).revision
        self.working = TheaterWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        record = self.repository.view_state(self.document_id)
        if record is not None:
            self.view_state = EditorViewState(
                selected_id=record.selected_id,
                selected_ids=list(record.selected_ids),
                hidden_ids=set(record.hidden_ids),
                locked_ids=set(record.locked_ids),
            )
            self.view_state.sanitize(revision.document)
        self._sync_transform_controls()
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = self.repository.recovery(self.document_id)
        self._rebuild(reset_camera=True)
        if self.recovery_candidate is not None:
            self.statusBar().showMessage(
                f'revision {revision.revision_id[:8]} · recovery available · choose Recover Draft or Discard Recovery'
            )
        else:
            room_state = '部屋あり' if revision.document.room is not None else '空シーン · 部屋を描画して開始'
            self.statusBar().showMessage(f'revision {revision.revision_id[:8]} · {room_state} · clean')

    def recover_draft(self) -> None:
        if self.recovery_candidate is None:
            return
        source_id = self.recovery_candidate.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            self.statusBar().showMessage('復旧元 revision がないためドラフトを開けません')
            return
        self.working = TheaterWorkingDocument(
            self.recovery_candidate.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = None
        self._rebuild(reset_camera=True)
        self.statusBar().showMessage(f'revision {source.revision_id[:8]} からドラフトを復旧しました · dirty')

    def close_room_sketch(self) -> None:
        was_sketching = self.room_mode == 'sketch'
        super().close_room_sketch()
        if was_sketching and self.room_mode == 'edit' and self._current_room() is not None:
            self.finish_room_edit()
            self.statusBar().showMessage(
                '部屋を作成しました · オブジェクトを追加できます · 形状調整は「部屋編集」から行えます'
            )

    def _object_position(self, kind: str, size: Size3 | None = None) -> Position3:
        room = self._current_room()
        if room is None:
            return super()._object_position(kind, size)

        min_x, min_y, max_x, max_y = room.bounds_m
        width = max_x - min_x
        depth = max_y - min_y
        center_x = min_x + width * 0.5

        if kind == 'screen':
            y_m = min_y + min(depth * 0.04, 0.15)
            z_m = min(max(room.height_m * 0.55, 0.5), max(room.height_m - 0.1, 0.1))
            return Position3(x_m=center_x, y_m=y_m, z_m=z_m)

        base = super()._object_position(kind, size)
        lane = min(max(width * 0.18, 0.45), 1.10)
        offsets = {
            'speaker': lane * 0.45,
            'furniture': lane,
            'av_equipment': -lane,
            'measurement_point': -lane * 0.55,
        }
        offset = offsets.get(kind, 0.0)
        if abs(offset) <= 1e-12:
            return base

        half_width = size.x_m * 0.5 if size is not None else 0.08
        minimum_x = min_x + half_width + 0.05
        maximum_x = max_x - half_width - 0.05
        if minimum_x > maximum_x:
            x_m = center_x
        else:
            x_m = min(max(base.x_m + offset, minimum_x), maximum_x)
        return Position3(x_m=x_m, y_m=base.y_m, z_m=base.z_m)

    def _aim_target_id(self) -> str | None:
        if self.working is None:
            return None
        document = self.working.committed_document
        selection = self.view_state.selection
        if self.selected_id is not None:
            primary = document.entity(self.selected_id)
            if primary.kind in {'seat', 'measurement_point'}:
                return primary.entity_id
        targets = [
            entity_id
            for entity_id in selection
            if document.entity(entity_id).kind in {'seat', 'measurement_point'}
        ]
        return targets[0] if len(targets) == 1 else None

    def _aim_source_ids(self, target_id: str | None = None) -> tuple[str, ...]:
        if self.working is None:
            return ()
        document = self.working.committed_document
        selected_speakers = tuple(
            entity_id
            for entity_id in self.view_state.selection
            if entity_id != target_id and document.entity(entity_id).kind == 'speaker'
        )
        candidates = selected_speakers or tuple(
            entity.entity_id for entity in document.entities if entity.kind == 'speaker'
        )
        return tuple(
            entity_id for entity_id in candidates if not self.view_state.is_locked(entity_id)
        )

    def aim_speakers_at_selected_reference(self) -> None:
        if not self._object_edit_available() or self.working is None:
            return
        target_id = self._aim_target_id()
        if target_id is None:
            self.statusBar().showMessage('座席または測定点を1つ選択してから「座席へ向ける」を実行してください')
            return
        speaker_ids = self._aim_source_ids(target_id)
        if not speaker_ids:
            self.statusBar().showMessage('向きを変更できるスピーカーがありません · ロック状態も確認してください')
            return
        try:
            replacements = speaker_aim_replacements(
                self.working.committed_document,
                speaker_ids,
                target_id,
            )
            changed = self.working.replace_entities(replacements)
        except (TheaterObjectError, EditStateError, ValueError) as exc:
            self.statusBar().showMessage(f'スピーカーを座席へ向けられません · {exc}')
            return
        if changed:
            self._sync_recovery()
            self._rebuild()
            self.statusBar().showMessage(
                f'{len(speaker_ids)}台のスピーカーを選択した音響基準点へ向けました · 本体の回転は変更していません'
            )

    def _update_actions(self) -> None:
        super()._update_actions()
        if self.aim_action is None or self.working is None:
            return
        target_id = self._aim_target_id()
        self.aim_action.setEnabled(
            self._object_edit_available()
            and target_id is not None
            and bool(self._aim_source_ids(target_id))
        )
