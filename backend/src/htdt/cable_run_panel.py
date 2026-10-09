"""ケーブル配線 listing surface (#1011, M1).

Read-only panel mounted on the room workspace's placement page, beside
the installation record forms. It lists every persisted ``CableRun`` of
the document with its endpoint bindings, signal-path edge reference,
per-segment path kinds and declared lengths, service loop, version
lineage, and current/stale/missing freshness against the head revision —
and it is the honesty boundary for the 3D view: only exactly-bound
endpoints resolve to a position, and a run without recorded
``CableRunGeometry`` shows ``経路形状未登録`` rather than an invented
route.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_cable_run_inspection import (
    CableRunInspection,
    inspect_cable_runs,
)
from .cad_cable_run_geometry_repository import CadCableRunGeometryRepository
from .cad_cable_run_repository import CadCableRunRepository
from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
    format_length_m,
)
from .cad_repository import SceneRepository
from .cad_signal_path_repository import CadSignalPathRepository
from .room_viewport import (
    CableRouteEndpointItem,
    CableRouteOverlayItem,
    CableRouteSegmentRouteItem,
)
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


_FRESHNESS_LABELS = {
    'current': '最新',
    'stale': '基リビジョン変更',
    'missing': '参照先なし',
}
_BINDING_LABELS = {
    'bound': 'バインド済',
    'unbound': 'シーン外',
    'missing': 'エンティティなし',
}
_PATH_KIND_LABELS = {
    'in_wall': '壁内',
    'in_ceiling': '天井内',
    'under_floor': '床下',
    'conduit': '配管',
    'surface_raceway': 'モール',
    'exposed': '露出',
    'other': 'その他',
}
_ROUTE_STATE_LABELS = {
    'unregistered': '経路形状未登録',
    'registered': '経路登録済',
    'stale': '経路情報が最新ではありません',
}
_RECORD_KIND_LABELS = {'design': '設計', 'as_built': '施工済'}
_SOURCE_LABELS = {
    'authored': 'アプリ内記録',
    'imported': '外部取込',
    'as_built_survey': '現況測定転記',
}


def _freshness_label(status: str) -> str:
    return _FRESHNESS_LABELS.get(status, status)


def _position_tuple(position) -> tuple[float, float, float] | None:
    if position is None:
        return None
    return (
        float(position.x_m), float(position.y_m), float(position.z_m)
    )


def overlay_item_for_inspection(
    inspection: CableRunInspection,
    *,
    waypoint_segments: tuple[CableRouteSegmentRouteItem, ...] = (),
    record_kind: str = 'design',
) -> CableRouteOverlayItem:
    """Project one listing entry into the viewport overlay contract.

    Only ``bound`` endpoints carry a position — the 3D surface literally
    cannot place anything else, so it can never draw a route from thin
    air.
    """

    def endpoint(binding) -> CableRouteEndpointItem:
        return CableRouteEndpointItem(
            label=binding.label,
            position=(
                _position_tuple(binding.position)
                if binding.status == 'bound'
                else None
            ),
            status=binding.status,
        )

    return CableRouteOverlayItem(
        run_id=inspection.run_id,
        label=inspection.label,
        route_state=inspection.route_state,
        from_endpoint=endpoint(inspection.from_endpoint),
        to_endpoint=endpoint(inspection.to_endpoint),
        waypoint_segments=waypoint_segments,
        record_kind=record_kind,
    )


class CableRunPanel(QFrame):
    """ケーブル配線の一覧 — 記録済み CableRun の状態と系譜を示す。"""

    routeSelectionChanged = Signal(object)

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        cable_run_repository: CadCableRunRepository | None = None,
        geometry_repository: CadCableRunGeometryRepository | None = None,
        signal_path_repository: CadSignalPathRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.cable_run_repository = (
            cable_run_repository
            if cable_run_repository is not None
            else CadCableRunRepository(scene_repository)
        )
        self.geometry_repository = (
            geometry_repository
            if geometry_repository is not None
            else CadCableRunGeometryRepository(
                scene_repository, self.cable_run_repository
            )
        )
        self.signal_path_repository = (
            signal_path_repository
            if signal_path_repository is not None
            else CadSignalPathRepository(scene_repository)
        )
        self._length_policy = display_length_policy('m')
        self._inspections: tuple[CableRunInspection, ...] = ()
        self._selected_run_id: str | None = None
        self._build_ui()
        self.refresh()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel('ケーブル配線')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            '記録済みの配線計画とその状態を一覧します。'
            '経路の形状が未登録の区間は3D上に線を引きません。'
        )
        set_typography_role(note, TypographyRole.SECONDARY)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.run_list = QListWidget()
        self.run_list.setAccessibleName('ケーブル配線一覧')
        self.run_list.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.run_list)

        self.detail = QLabel('')
        self.detail.setAccessibleName('配線の詳細')
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.detail.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        layout.addWidget(self.detail)

    # -- refresh / selection -------------------------------------------------

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        self._length_policy = policy
        self._render_detail()

    def refresh(self) -> None:
        self._inspections = inspect_cable_runs(
            scene_repository=self.scene_repository,
            document_id=self.document_id,
            cable_run_repository=self.cable_run_repository,
            geometry_repository=self.geometry_repository,
            signal_path_repository=self.signal_path_repository,
        )
        selected = self._selected_run_id
        self.run_list.blockSignals(True)
        self.run_list.clear()
        restored_row = -1
        for index, inspection in enumerate(self._inspections):
            item = QListWidgetItem(
                f'{inspection.label} — {_freshness_label(inspection.freshness_status)}'
            )
            item.setData(Qt.ItemDataRole.UserRole, inspection.run_id)
            self.run_list.addItem(item)
            if inspection.run_id == selected:
                restored_row = index
        self.run_list.blockSignals(False)
        if restored_row >= 0:
            self.run_list.setCurrentRow(restored_row)
        elif self._inspections:
            self._selected_run_id = None
            self._render_detail()
            self._emit_selection()
        else:
            self._selected_run_id = None
            self._render_detail()
            self._emit_selection()

    def _current_inspection(self) -> CableRunInspection | None:
        row = self.run_list.currentRow()
        if row < 0 or row >= len(self._inspections):
            return None
        return self._inspections[row]

    def _selection_changed(self, row: int) -> None:  # noqa: ARG002
        inspection = self._current_inspection()
        self._selected_run_id = (
            inspection.run_id if inspection is not None else None
        )
        self._render_detail()
        self._emit_selection()

    def _emit_selection(self) -> None:
        inspection = self._current_inspection()
        if inspection is None:
            self.routeSelectionChanged.emit(None)
            return
        self.routeSelectionChanged.emit(
            overlay_item_for_inspection(
                inspection,
                waypoint_segments=self._waypoint_segments(inspection),
                record_kind=self._record_kind(inspection),
            )
        )

    def _waypoint_segments(
        self, inspection: CableRunInspection
    ) -> tuple[CableRouteSegmentRouteItem, ...]:
        if inspection.geometry is None or inspection.route_state != 'registered':
            return ()
        geometry = self.geometry_repository.get_geometry(
            inspection.geometry.geometry_id,
            inspection.geometry.version,
        )
        if geometry is None:
            return ()
        segments = []
        for segment in geometry.segment_geometries:
            segments.append(
                CableRouteSegmentRouteItem(
                    segment_sequence=segment.segment_sequence,
                    points=tuple(
                        (
                            float(waypoint.x_m),
                            float(waypoint.y_m),
                            float(waypoint.z_m),
                        )
                        for waypoint in segment.waypoints
                    ),
                )
            )
        return tuple(segments)

    @staticmethod
    def _record_kind(inspection: CableRunInspection) -> str:
        if (
            inspection.geometry is not None
            and 'as_built' in inspection.geometry.record_kinds
        ):
            return 'as_built'
        return 'design'

    # -- detail rendering -----------------------------------------------------

    def _format_length(self, value_m: float | None) -> str:
        if value_m is None:
            return '—'
        return format_length_m(float(value_m), self._length_policy)

    def _render_detail(self) -> None:
        inspection = self._current_inspection()
        if inspection is None:
            self.detail.setText(
                '記録済みの配線はまだありません。'
                if not self._inspections
                else '配線を選択すると詳細を表示します。'
            )
            return
        lines = [
            f'{inspection.label}（run {inspection.run_id} / v{inspection.version}）',
            f'状態: {_freshness_label(inspection.freshness_status)}',
        ]
        for reason in inspection.freshness_reasons:
            lines.append(f'・{reason}')
        for side, endpoint in (
            ('起点', inspection.from_endpoint),
            ('終点', inspection.to_endpoint),
        ):
            entity = (
                endpoint.entity_id
                if endpoint.entity_id is not None
                else '（シーン外）'
            )
            lines.append(
                f'{side}: {endpoint.label} — {entity} '
                f'[{_BINDING_LABELS[endpoint.status]}]'
            )
        if inspection.signal_path_edge_id is not None:
            edge_state = (
                '解決可'
                if inspection.signal_path_edge_present
                else '解決不可'
            )
            lines.append(
                f'信号経路エッジ: {inspection.signal_path_edge_id}（{edge_state}）'
            )
        lines.append(
            f'経路の状態: {_ROUTE_STATE_LABELS[inspection.route_state]}'
        )
        for segment in inspection.segments:
            path_label = _PATH_KIND_LABELS.get(
                segment.path_kind, segment.path_kind
            )
            row = (
                f'区間{segment.sequence}: {path_label} '
                f'記録長 {self._format_length(segment.declared_length_m)}'
            )
            if segment.geometry_registered:
                row += (
                    f' / 幾何長 '
                    f'{self._format_length(segment.geometric_length_m)}'
                )
            else:
                row += ' / 経路形状未登録'
            if segment.description:
                row += f' — {segment.description}'
            lines.append(row)
        lines.append(
            f'サービスループ {self._format_length(inspection.service_loop_m)}'
            f' / 合計 {self._format_length(inspection.total_length_m)}'
        )
        if len(inspection.versions) > 1:
            lines.append(
                '系譜: '
                + ' → '.join(
                    f'v{entry.version}' for entry in inspection.versions
                )
            )
        geometry = inspection.geometry
        if geometry is not None:
            kinds = '・'.join(
                _RECORD_KIND_LABELS.get(kind, kind)
                for kind in geometry.record_kinds
            )
            sources = '・'.join(
                _SOURCE_LABELS.get(source, source)
                for source in geometry.sources
            )
            lines.append(f'経路記録: {kinds}（{sources}）')
            lines.append(
                f'経路ジオメトリ鮮度: '
                f'{_freshness_label(geometry.freshness_status)}'
            )
            divergence = geometry.divergence
            if divergence.divergent:
                lines.append(
                    '長さの差: 記録合計 '
                    f'{self._format_length(divergence.declared_total_length_m)}'
                    ' / 幾何合計 '
                    f'{self._format_length(divergence.geometric_length_m)}'
                    f'（差 {divergence.total_delta_m:+.3f} m）'
                )
        self.detail.setText('\n'.join(lines))


__all__ = [
    'CableRunPanel',
    'overlay_item_for_inspection',
]
