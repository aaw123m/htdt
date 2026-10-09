"""ケーブル経路ウェイポイント記録 (#1011, M3).

3D ビューポート上で配線区間の経路点 (waypoint) を記録し、M2 の
``cad_cable_run_geometries`` 権威へ保存するオーサリング UI。

Honesty contract
----------------
* 記録点は必ず ``CableRun`` が宣言した ``segment_sequence`` に紐づく。
  宣言にない区間への登録は ``build_cable_run_geometry`` /
  ``save_geometry`` が拒否し、こちら側でも先に検証する (fail closed)。
* 点は部屋の面に落ちる: エンティティ・床・壁・天井・オーサリング面の
  上で拾われた点、または部屋境界とのレイ交差。空間の自由点を勝手に
  作らない。
* undo/redo は記録中の点列のみに作用する — シーンの編集を巻き戻さない。
* 下書きオーバーレイは ``cable-route-draft-`` プレフィックスの非永続
  アクタであり、登録済み経路の表示とは色を分ける。
* 保存は ``CadCableRunGeometryRepository.save_geometry`` のみ — ピンが
  古い場合に例外を握り潰さず、セッションを保持して再試行を許す。
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cad_cable_run import CablePathKind, CableRun
from .cad_cable_run_geometry import (
    CableRunGeometry,
    CableRunSegmentGeometry,
    build_cable_run_geometry,
)
from .cad_cable_run_geometry_repository import CadCableRunGeometryRepository
from .cad_cable_run_inspection import (
    CableRunInspection,
    CableRunSegmentListing,
    scene_surface_ids,
)
from .cad_cable_run_repository import CadCableRunRepository
from .cad_scene import Position3
from .cable_run_panel import _PATH_KIND_LABELS, _RECORD_KIND_LABELS, _SOURCE_LABELS
from .clock import utc_now_iso
from .room_viewport import CableRouteSegmentRouteItem
from .ui_theme import TypographyRole, set_typography_role


class CableRunWaypointController(QObject):
    """Recording session state + commit path for #1011 waypoint authoring.

    Owns the in-progress point list (with honest undo/redo), the staged
    ``CableRunSegmentGeometry`` map, and the field set each stage/commit
    writes into the record. The viewport arm/disarm and the draft overlay
    push live here so the workspace only routes signals.
    """

    stateChanged = Signal()
    committed = Signal(object)  # CableRunGeometry

    def __init__(
        self,
        workspace,
        viewport,
        *,
        cable_run_repository: CadCableRunRepository,
        geometry_repository: CadCableRunGeometryRepository,
        inspection_provider: Callable[[str], CableRunInspection | None],
        parent: QObject | None = None,
    ) -> None:
        # The controller is owned by the workspace's lifetime, not Qt's
        # parent tree — the workspace argument may be a plain test double.
        super().__init__(parent)
        self.workspace = workspace
        self.viewport = viewport
        self.cable_run_repository = cable_run_repository
        self.geometry_repository = geometry_repository
        self._inspection_provider = inspection_provider

        self._active = False
        self._inspection: CableRunInspection | None = None
        self._points: list[tuple[float, float, float]] = []
        self._undone: list[tuple[float, float, float]] = []
        self._staged: list[CableRunSegmentGeometry] = []
        # 記録対象区間 + 記録フィールド (区間確定時に CableRunSegmentGeometry へ)
        self._segment_sequence = 0
        self._concealment_kind: CablePathKind = 'conduit'
        self._traversed_surface_ids: tuple[str, ...] = ()
        self._source = 'authored'
        self._record_kind = 'design'
        self._note: str | None = None

    # -- session state --------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def run_id(self) -> str | None:
        if self._inspection is None:
            return None
        return self._inspection.run_id

    @property
    def points(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(self._points)

    @property
    def staged(self) -> tuple[CableRunSegmentGeometry, ...]:
        return tuple(self._staged)

    @property
    def can_undo(self) -> bool:
        return bool(self._points)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def can_stage(self) -> bool:
        return len(self._points) >= 2

    @property
    def segment_sequence(self) -> int:
        return self._segment_sequence

    @property
    def concealment_kind(self) -> CablePathKind:
        return self._concealment_kind

    @property
    def traversed_surface_ids(self) -> tuple[str, ...]:
        return self._traversed_surface_ids

    @property
    def source(self) -> str:
        return self._source

    @property
    def record_kind(self) -> str:
        return self._record_kind

    @property
    def note(self) -> str | None:
        return self._note

    @property
    def inspection(self) -> CableRunInspection | None:
        return self._inspection

    def declared_segments(self) -> tuple[CableRunSegmentListing, ...]:
        if self._inspection is None:
            return ()
        return self._inspection.segments

    def scene_surface_ids(self) -> tuple[str, ...]:
        """現シーンの表面 id — 通過面チェックボックスの母集団。"""

        document = getattr(self.workspace.controller, 'document', None)
        if document is None:
            return ()
        return scene_surface_ids(document)

    # -- lifecycle --------------------------------------------------------------

    def begin(self, run_id: str) -> bool:
        """Arm waypoint recording for one persisted run.

        Resolves the current inspection so declared segments are never
        guessed; a run that no longer resolves fails closed.
        """

        inspection = self._inspection_provider(run_id)
        if inspection is None:
            self.workspace._set_status(
                '経路記録の対象配線が見つかりません', error=True
            )
            return False
        if not inspection.segments:
            self.workspace._set_status(
                'この配線には区間が宣言されていないため記録できません',
                error=True,
            )
            return False
        if self._active:
            self._teardown()
        self._active = True
        self._inspection = inspection
        self._points = []
        self._undone = []
        self._staged = []
        # 既定値: 最初の区間とその宣言済み経路種別をそのまま採用。
        self._segment_sequence = inspection.segments[0].sequence
        self._concealment_kind = inspection.segments[0].path_kind
        self._traversed_surface_ids = ()
        self._source = 'authored'
        self._record_kind = 'design'
        self._note = None
        arm = getattr(self.viewport, 'set_waypoint_pick_armed', None)
        if callable(arm):
            arm(True)
        self.workspace._set_status(
            f'経路記録中: {inspection.label} — '
            '区間上の点を3Dでクリックしてください（Escで中止）'
        )
        self.stateChanged.emit()
        self._push_draft()
        return True

    def cancel(self) -> None:
        """Discard the in-progress session — staged and uncommitted alike."""

        if not self._active:
            return
        self._teardown()
        self.workspace._set_status('経路記録を中止しました')
        self.stateChanged.emit()

    def _teardown(self) -> None:
        self._active = False
        self._inspection = None
        self._points = []
        self._undone = []
        self._staged = []
        arm = getattr(self.viewport, 'set_waypoint_pick_armed', None)
        if callable(arm):
            arm(False)
        render = getattr(self.viewport, 'render_cable_route_draft', None)
        if callable(render):
            render((), ())

    def refresh_run(self) -> None:
        """Re-resolve the bound run after a document/listing refresh.

        The listing may have moved: a run that vanished ends the session
        honestly; a run still present keeps recording (the commit path
        re-pins to the current record anyway). A staged segment whose
        sequence is no longer declared is surfaced immediately — it can
        never be committed.
        """

        if not self._active or self._inspection is None:
            return
        inspection = self._inspection_provider(self._inspection.run_id)
        if inspection is None:
            self._teardown()
            self.workspace._set_status(
                '記録中の配線が見つからなくなったため、'
                '経路記録を中止しました',
                error=True,
            )
            self.stateChanged.emit()
            return
        self._inspection = inspection
        declared = {segment.sequence for segment in inspection.segments}
        dropped = [
            segment.segment_sequence
            for segment in self._staged
            if segment.segment_sequence not in declared
        ]
        if dropped:
            self.workspace._set_status(
                '区間'
                + '・'.join(str(item) for item in dropped)
                + 'が配線の宣言から外れました — 確定済み点を見直してください',
                error=True,
            )
        self.stateChanged.emit()

    # -- point recording --------------------------------------------------------

    def record_domain_point(self, point: Sequence[float]) -> bool:
        """Record a domain-space (x, y, z) triple as the next waypoint."""

        if not self._active:
            return False
        triple = (
            float(point[0]),
            float(point[1]),
            float(point[2]),
        )
        self._points.append(triple)
        # 新しい記録は redo 履歴を破棄 — 枝分かれした点列は存在しない。
        self._undone = []
        self._push_draft()
        self.stateChanged.emit()
        return True

    def record_at(self, display_position) -> bool:
        """Resolve a display-space click to a room point and record it."""

        if not self._active:
            return False
        pick = getattr(self.viewport, 'pick_waypoint_domain', None)
        point = pick(display_position) if callable(pick) else None
        if point is None:
            self.workspace._set_status(
                'その位置から部屋の面上の点を取得できませんでした',
                error=True,
            )
            return True
        return self.record_domain_point(point)

    # -- honest undo/redo over the point list only ------------------------------

    def undo(self) -> bool:
        """Remove the last recorded point. Never touches scene edits."""

        if not self._active or not self._points:
            return False
        self._undone.append(self._points.pop())
        self._push_draft()
        self.stateChanged.emit()
        return True

    def redo(self) -> bool:
        """Re-record a point undone in this session."""

        if not self._active or not self._undone:
            return False
        self._points.append(self._undone.pop())
        self._push_draft()
        self.stateChanged.emit()
        return True

    def clear_points(self) -> None:
        """Drop every uncommitted point (undo history included)."""

        if not self._active or not self._points:
            return
        self._points = []
        self._undone = []
        self._push_draft()
        self.stateChanged.emit()

    # -- field edits -------------------------------------------------------------

    def set_segment_sequence(self, sequence: int) -> bool:
        """Bind the in-progress point list to a declared segment."""

        if not self._active or self._inspection is None:
            return False
        declared = {segment.sequence for segment in self._inspection.segments}
        if sequence not in declared:
            return False
        self._segment_sequence = int(sequence)
        # 区間の宣言経路種別を concealment の既定値として引き継ぐ。
        for segment in self._inspection.segments:
            if segment.sequence == sequence:
                self._concealment_kind = segment.path_kind
                break
        self.stateChanged.emit()
        return True

    def set_concealment_kind(self, kind: CablePathKind) -> None:
        self._concealment_kind = kind
        self.stateChanged.emit()

    def set_traversed_surface_ids(self, ids: Sequence[str]) -> bool:
        """Set traversed surfaces — unknown ids fail closed."""

        known = set(self.scene_surface_ids())
        unknown = [surface_id for surface_id in ids if surface_id not in known]
        if unknown:
            self.workspace._set_status(
                '存在しない面を参照できません: '
                + '、'.join(str(item) for item in unknown),
                error=True,
            )
            return False
        self._traversed_surface_ids = tuple(sorted({str(item) for item in ids}))
        self.stateChanged.emit()
        return True

    def set_source(self, source: str) -> None:
        self._source = source
        self.stateChanged.emit()

    def set_record_kind(self, kind: str) -> None:
        self._record_kind = kind
        self.stateChanged.emit()

    def set_note(self, note: str | None) -> None:
        cleaned = note.strip() if isinstance(note, str) else ''
        self._note = cleaned or None

    # -- stage / commit -------------------------------------------------------------

    def stage_segment(self) -> bool:
        """Snapshot the point list + current fields into a staged segment."""

        if not self._active or self._inspection is None:
            return False
        if len(self._points) < 2:
            self.workspace._set_status(
                '区間を確定するには2点以上の記録が必要です',
                error=True,
            )
            return False
        declared = {
            segment.sequence for segment in self._inspection.segments
        }
        if self._segment_sequence not in declared:
            # 記録先は宣言済み区間のみ — コンボが壊れても fail closed。
            self.workspace._set_status(
                '宣言されていない区間には確定できません',
                error=True,
            )
            return False
        try:
            segment = CableRunSegmentGeometry(
                segment_sequence=self._segment_sequence,
                waypoints=tuple(
                    Position3(x_m=x, y_m=y, z_m=z)
                    for x, y, z in self._points
                ),
                concealment_kind=self._concealment_kind,
                traversed_surface_ids=self._traversed_surface_ids,
                source=self._source,
                record_kind=self._record_kind,
                note=self._note,
            )
        except (ValueError, TypeError) as exc:
            self.workspace._set_status(
                f'区間を確定できません: {exc}', error=True
            )
            return False
        # 同じ区間へ再度確定した場合は最新の記録で置き換え — コミットは
        # 常に宣言済み区間に一意に写る (model が重複を拒否する)。
        self._staged = [
            existing
            for existing in self._staged
            if existing.segment_sequence != self._segment_sequence
        ]
        self._staged.append(segment)
        count = len(self._points)
        self._points = []
        self._undone = []
        self._push_draft()
        self.workspace._set_status(
            f'区間{self._segment_sequence}に {count} 点を確定しました'
        )
        self.stateChanged.emit()
        return True

    def unstage(self, sequence: int) -> bool:
        """Drop a staged segment so its points may be re-recorded."""

        before = len(self._staged)
        self._staged = [
            segment
            for segment in self._staged
            if segment.segment_sequence != sequence
        ]
        if len(self._staged) == before:
            return False
        self._push_draft()
        self.stateChanged.emit()
        return True

    def commit(self) -> bool:
        """Persist staged segments through ``save_geometry``.

        The pinned run is re-resolved at commit time — never the stale
        ``begin``-time snapshot — and the repository's exact-pin check is
        the final gate. Any failure keeps the session intact so nothing
        recorded is silently lost.
        """

        if not self._active:
            return False
        if len(self._points) == 1:
            self.workspace._set_status(
                '未確定の点が1点残っています — 点を追加するか取り消してください',
                error=True,
            )
            return False
        if self._points and not self.stage_segment():
            return False
        if not self._staged:
            self.workspace._set_status(
                '確定済みの区間がありません — 先に2点以上を記録して確定してください',
                error=True,
            )
            return False
        inspection = self._inspection
        if inspection is None:
            return False
        # コミット時点の最新版にピンする。begin 時点の記録は
        # authoritative ではない — 変更があれば新しい版へ、無ければ
        # 失敗する (stale pin は決して新しい版を上書きしない)。
        run = self._resolve_current_run(inspection.run_id)
        if run is None:
            self.workspace._set_status(
                '対象の配線が解決できないため保存できません'
                '（記録は保持されています）',
                error=True,
            )
            return False
        latest = self.geometry_repository.latest_geometry_for_run(
            run.run_id, run.version
        )
        geometry_id = latest.geometry_id if latest is not None else None
        if (
            latest is not None
            and isinstance(latest.version, str)
            and latest.version.isdigit()
        ):
            version = str(int(latest.version) + 1)
        else:
            version = '1'
        # The committed record is a snapshot of the WHOLE run's route, so
        # previously committed segments merge under the staged set —
        # otherwise recording segment N+1 would silently unregister
        # segments 0..N. Staged entries win on the same sequence (a
        # re-recorded segment replaces its prior geometry).
        merged: dict[int, CableRunSegmentGeometry] = {}
        if latest is not None:
            for segment in latest.segment_geometries:
                merged[segment.segment_sequence] = segment
        for segment in self._staged:
            merged[segment.segment_sequence] = segment
        try:
            geometry = build_cable_run_geometry(
                run=run,
                segment_geometries=[
                    merged[key] for key in sorted(merged)
                ],
                created_at_utc=utc_now_iso(),
                geometry_id=geometry_id,
                version=version,
            )
            self.geometry_repository.save_geometry(geometry)
        except (ValueError, TypeError) as exc:
            self.workspace._set_status(
                f'経路を保存できません（記録は保持されています）: {exc}',
                error=True,
            )
            return False
        label = inspection.label
        segments = len(self._staged)
        self._teardown()
        self.workspace._set_status(
            f'{label} の経路を保存しました（{segments}区間・'
            f'{geometry.geometric_length_m:.2f} m）'
        )
        self.committed.emit(geometry)
        self.stateChanged.emit()
        self.workspace.refresh()
        return True

    def _resolve_current_run(self, run_id: str) -> CableRun | None:
        """Latest persisted version of ``run_id`` — the only honest pin."""

        versions = self.cable_run_repository.list_run_versions(run_id)
        if not versions:
            return None
        return versions[-1]

    # -- viewport draft -------------------------------------------------------------

    def _push_draft(self) -> None:
        render = getattr(self.viewport, 'render_cable_route_draft', None)
        if not callable(render):
            return
        staged = tuple(
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
            for segment in self._staged
        )
        render(tuple(self._points), staged)


class CableRunWaypointPanel(QWidget):
    """経路ウェイポイント記録カード — 区間・通過面・保存操作 (#1011 M3).

    Bound one-to-one to a ``CableRunWaypointController``; the panel never
    owns record state — every field delegates so the controller's session
    is the single source of truth.
    """

    def __init__(
        self,
        controller: CableRunWaypointController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName('cableRunWaypointPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel('経路ウェイポイント記録')
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.session_label = QLabel('3D上で点を記録して配線区間の経路を登録します')
        self.session_label.setWordWrap(True)
        set_typography_role(self.session_label, TypographyRole.SECONDARY)
        layout.addWidget(self.session_label)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.segment_combo = QComboBox()
        self.segment_combo.setAccessibleName('記録先の区間')
        self.segment_combo.setToolTip(
            'この配線が宣言している区間のみ選択できます — '
            '宣言外の区間へは経路を記録できません'
        )
        self.segment_combo.currentIndexChanged.connect(self._segment_changed)
        form.addRow('記録先の区間', self.segment_combo)

        self.concealment_combo = QComboBox()
        self.concealment_combo.setAccessibleName('隠蔽・経路種別')
        self.concealment_combo.setToolTip(
            '記録された経路の隠蔽種別 — 設計の宣言値と異なる施工経路も'
            'そのまま記録できます'
        )
        for kind, label in _PATH_KIND_LABELS.items():
            self.concealment_combo.addItem(label, kind)
        self.concealment_combo.currentIndexChanged.connect(
            self._concealment_changed
        )
        form.addRow('隠蔽・経路種別', self.concealment_combo)

        self.record_kind_combo = QComboBox()
        self.record_kind_combo.setAccessibleName('記録区分')
        self.record_kind_combo.setToolTip(
            '設計上の経路か施工済み(現況)の経路かを記録します'
        )
        for kind, label in _RECORD_KIND_LABELS.items():
            self.record_kind_combo.addItem(label, kind)
        self.record_kind_combo.currentIndexChanged.connect(
            self._record_kind_changed
        )
        form.addRow('記録区分', self.record_kind_combo)

        self.source_combo = QComboBox()
        self.source_combo.setAccessibleName('記録の由来')
        self.source_combo.setToolTip(
            'この経路点列の記録方法 — アプリ内で記録したか、外部から'
            '取り込んだか、現況測定の転記かを区別します'
        )
        for source, label in _SOURCE_LABELS.items():
            self.source_combo.addItem(label, source)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        form.addRow('記録の由来', self.source_combo)

        self.note_edit = QLineEdit()
        self.note_edit.setAccessibleName('経路メモ')
        self.note_edit.setToolTip('経路に関する任意のメモ (施工条件など)')
        self.note_edit.setPlaceholderText('任意メモ')
        self.note_edit.editingFinished.connect(self._note_changed)
        form.addRow('経路メモ', self.note_edit)
        layout.addLayout(form)

        surfaces_label = QLabel('通過する面')
        surfaces_label.setAccessibleName('通過する面の見出し')
        set_typography_role(surfaces_label, TypographyRole.SECONDARY)
        layout.addWidget(surfaces_label)
        self.surface_list = QListWidget()
        self.surface_list.setAccessibleName('通過する面の一覧')
        self.surface_list.setToolTip(
            '経路が通過するシーン上の面 (壁・開口・造作面)。'
            '存在しない面は選べません'
        )
        self.surface_list.setMaximumHeight(96)
        self.surface_list.itemChanged.connect(self._surfaces_changed)
        layout.addWidget(self.surface_list)

        points_row = QHBoxLayout()
        self.points_label = QLabel('記録点: 0点')
        self.points_label.setAccessibleName('記録中の点数')
        points_row.addWidget(self.points_label)
        self.undo_button = QPushButton('点を取り消す')
        self.undo_button.setAccessibleName('記録点をひとつ取り消す')
        self.undo_button.setToolTip(
            '最後に記録した点を取り消します（シーンの編集は戻りません）'
        )
        self.undo_button.clicked.connect(lambda: self.controller.undo())
        points_row.addWidget(self.undo_button)
        self.redo_button = QPushButton('やり直す')
        self.redo_button.setAccessibleName('取り消した記録点をやり直す')
        self.redo_button.setToolTip('取り消した点を記録し直します')
        self.redo_button.clicked.connect(lambda: self.controller.redo())
        points_row.addWidget(self.redo_button)
        self.clear_button = QPushButton('点をクリア')
        self.clear_button.setAccessibleName('記録中の点をすべてクリア')
        self.clear_button.setToolTip('未確定の記録点をすべて削除します')
        self.clear_button.clicked.connect(
            lambda: self.controller.clear_points()
        )
        points_row.addWidget(self.clear_button)
        layout.addLayout(points_row)

        stage_row = QHBoxLayout()
        self.stage_button = QPushButton('区間に確定')
        self.stage_button.setAccessibleName('記録点を選択中の区間に確定')
        self.stage_button.setToolTip(
            '記録した点列を選択中の区間の経路として確定します'
            '（2点以上が必要です）'
        )
        self.stage_button.clicked.connect(
            lambda: self.controller.stage_segment()
        )
        stage_row.addWidget(self.stage_button)
        self.unstage_button = QPushButton('確定を取り消す')
        self.unstage_button.setAccessibleName('確定済み区間を取り消す')
        self.unstage_button.setToolTip(
            '下の一覧で選択した確定済み区間を取り消します'
        )
        self.unstage_button.clicked.connect(self._unstage_selected)
        stage_row.addWidget(self.unstage_button)
        layout.addLayout(stage_row)

        self.staged_list = QListWidget()
        self.staged_list.setAccessibleName('確定済み区間の一覧')
        self.staged_list.setToolTip(
            '保存待ちの区間 — 各行は1区間分の確定済み点列'
        )
        self.staged_list.setMaximumHeight(72)
        layout.addWidget(self.staged_list)

        commit_row = QHBoxLayout()
        self.commit_button = QPushButton('経路を保存')
        self.commit_button.setAccessibleName('記録した経路を保存')
        self.commit_button.setToolTip(
            '確定済み区間をこの配線の最新版にピンして保存します。'
            'ピンが古い場合は保存されず記録も保持されます'
        )
        self.commit_button.clicked.connect(
            lambda: self.controller.commit()
        )
        commit_row.addWidget(self.commit_button)
        self.cancel_button = QPushButton('記録を中止')
        self.cancel_button.setAccessibleName('経路記録を中止')
        self.cancel_button.setToolTip(
            '記録中の点・確定済み区間をすべて破棄して終了します'
        )
        self.cancel_button.clicked.connect(
            lambda: self.controller.cancel()
        )
        commit_row.addWidget(self.cancel_button)
        layout.addLayout(commit_row)

        controller.stateChanged.connect(self._sync)
        self._sync()

    # -- controller -> view ------------------------------------------------------

    def _sync(self) -> None:
        active = self.controller.is_active
        inspection = self.controller.inspection
        self.setVisible(active)
        if not active or inspection is None:
            return
        self.session_label.setText(
            f'記録対象: {inspection.label}（run {inspection.run_id}）'
        )
        self.segment_combo.blockSignals(True)
        self.segment_combo.clear()
        staged = {
            segment.segment_sequence for segment in self.controller.staged
        }
        for segment in self.controller.declared_segments():
            kind = _PATH_KIND_LABELS.get(segment.path_kind, segment.path_kind)
            label = f'区間{segment.sequence}: {kind}'
            if segment.geometry_registered:
                label += '（登録済）'
            elif segment.sequence in staged:
                label += '（確定済）'
            self.segment_combo.addItem(label, segment.sequence)
        index = self.segment_combo.findData(self.controller.segment_sequence)
        if index >= 0:
            self.segment_combo.setCurrentIndex(index)
        self.segment_combo.blockSignals(False)
        kind_index = self.concealment_combo.findData(
            self.controller.concealment_kind
        )
        if kind_index >= 0:
            self.concealment_combo.blockSignals(True)
            self.concealment_combo.setCurrentIndex(kind_index)
            self.concealment_combo.blockSignals(False)
        for combo, value in (
            (self.record_kind_combo, self.controller.record_kind),
            (self.source_combo, self.controller.source),
        ):
            found = combo.findData(value)
            if found >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(found)
                combo.blockSignals(False)
        if not self.note_edit.hasFocus():
            self.note_edit.blockSignals(True)
            self.note_edit.setText(self.controller.note or '')
            self.note_edit.blockSignals(False)
        self._sync_surfaces()
        self._sync_staged()
        self.points_label.setText(
            f'記録点: {len(self.controller.points)}点'
        )
        self.undo_button.setEnabled(self.controller.can_undo)
        self.redo_button.setEnabled(self.controller.can_redo)
        self.clear_button.setEnabled(bool(self.controller.points))
        self.stage_button.setEnabled(self.controller.can_stage)
        self.commit_button.setEnabled(
            bool(self.controller.staged) or self.controller.can_stage
        )

    def _sync_surfaces(self) -> None:
        current = set(self.controller.traversed_surface_ids)
        self.surface_list.blockSignals(True)
        self.surface_list.clear()
        for surface_id in self.controller.scene_surface_ids():
            item = QListWidgetItem(surface_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if surface_id in current
                else Qt.CheckState.Unchecked
            )
            self.surface_list.addItem(item)
        self.surface_list.blockSignals(False)

    def _sync_staged(self) -> None:
        self.staged_list.blockSignals(True)
        self.staged_list.clear()
        for segment in self.controller.staged:
            self.staged_list.addItem(
                QListWidgetItem(
                    f'区間{segment.segment_sequence}: '
                    f'{len(segment.waypoints)}点 / '
                    f'{segment.polyline_length_m():.2f} m'
                )
            )
        self.staged_list.blockSignals(False)
        self.unstage_button.setEnabled(bool(self.controller.staged))

    # -- view -> controller ---------------------------------------------------------

    def _segment_changed(self, index: int) -> None:
        if index < 0:
            return
        sequence = self.segment_combo.itemData(index)
        if sequence is not None:
            self.controller.set_segment_sequence(int(sequence))

    def _concealment_changed(self, index: int) -> None:
        if index < 0:
            return
        kind = self.concealment_combo.itemData(index)
        if kind is not None:
            self.controller.set_concealment_kind(kind)

    def _record_kind_changed(self, index: int) -> None:
        if index < 0:
            return
        kind = self.record_kind_combo.itemData(index)
        if kind is not None:
            self.controller.set_record_kind(kind)

    def _source_changed(self, index: int) -> None:
        if index < 0:
            return
        source = self.source_combo.itemData(index)
        if source is not None:
            self.controller.set_source(source)

    def _note_changed(self) -> None:
        self.controller.set_note(self.note_edit.text())

    def _surfaces_changed(self, _item) -> None:
        ids = []
        for row in range(self.surface_list.count()):
            item = self.surface_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.text())
        self.controller.set_traversed_surface_ids(ids)

    def _unstage_selected(self) -> None:
        item = self.staged_list.currentItem()
        if item is None:
            return
        text = item.text()
        # 行テキストは '区間N: ...' — 宣言済み区間 id を再解析する。
        try:
            sequence = int(text.split(':', 1)[0].replace('区間', '').strip())
        except ValueError:
            return
        self.controller.unstage(sequence)


__all__ = [
    'CableRunWaypointController',
    'CableRunWaypointPanel',
]
