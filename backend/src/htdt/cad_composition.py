from __future__ import annotations

from PySide6.QtCore import QTimer

from .cad_scene import RoomPrism
from .wall_editor import WallEditorWindow


class CadEditorWindow(WallEditorWindow):
    """Product composition layer coordinating room and wall edit contracts."""

    def _topology_change_guidance(self) -> None:
        self.statusBar().showMessage(
            '壁情報の作成後は「壁を編集」から分割・結合・削除してください · '
            '部屋編集では頂点移動・寸法・天井高を変更できます'
        )

    def start_room_sketch(self) -> None:
        if self._current_topology() is not None:
            self._topology_change_guidance()
            return
        super().start_room_sketch()

    def _insert_room_vertex(self, edge_index: int) -> None:
        if self._current_topology() is not None:
            self._topology_change_guidance()
            return
        super()._insert_room_vertex(edge_index)

    def delete_room_vertex(self) -> None:
        if self._current_topology() is not None:
            self._topology_change_guidance()
            return
        super().delete_room_vertex()

    def _replace_room(self, room: RoomPrism) -> bool:
        topology = self._current_topology()
        if topology is None:
            return super()._replace_room(room)
        try:
            changed = self.working.replace_room_topology(room, topology)
        except ValueError as exc:
            message = f'部屋の変更を確定できません · 壁・開口参照を確認してください · {exc}'
            QTimer.singleShot(0, lambda: self.statusBar().showMessage(message))
            self._refresh_room_inspector()
            return False
        if changed:
            self._sync_recovery()
        self._update_actions()
        return changed

    def start_room_edit(self) -> None:
        super().start_room_edit()
        if self.room_mode == 'edit' and self._current_topology() is not None:
            self.statusBar().showMessage(
                '部屋編集 · 頂点移動・寸法・天井高を変更できます · '
                '頂点数を変える操作は「壁を編集」を使用してください'
            )

    def finish_room_edit(self) -> None:
        was_editing = self.room_mode == 'edit'
        super().finish_room_edit()
        if was_editing and self.room_mode == 'idle':
            self.statusBar().showMessage('部屋編集を終了しました · オブジェクト編集が有効です')

    def _update_actions(self) -> None:
        super()._update_actions()
        if not hasattr(self, 'draw_room_action'):
            return
        if self._current_topology() is None or self.wall_edit_active:
            return
        # Once stable wall references exist, changes to vertex count must use
        # wall split/delete so opening/constraint migration stays explicit.
        self.draw_room_action.setEnabled(False)
        if self.room_mode == 'edit':
            self.insert_vertex_action.setEnabled(False)
            self.delete_vertex_action.setEnabled(False)
