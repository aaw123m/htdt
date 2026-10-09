# #979 スナップと移動・回転ギズモにカーソル追従HUDと数値差分入力を統合する

## 変更

- `RoomViewport3D` にカーソル近傍スナップ/移動量 HUD (`_SnapHud`) を実装:
  - `render_snap_feedback(label, *, screen_position=..., hud_lines=...)` を
    拡張。`screen_position` (インタラクタ DIP 座標) があれば候補点の脇に
    `Qt.ToolTip | FramelessWindowHint` のフローティング HUD を表示 —
    `_PickCandidatePopover` (#983) と同じ窓形状
    (子widget化しない・`WA_ShowWithoutActivating`・`NoFocus`)
  - HUD 行構成: `snap状態行` (頂点/中点/辺/整列 · 対象名 (ID) · 軸=座標 ·
    距離 px) / `Δ行` (`ΔX/ΔY/ΔZ ±val` または `角度 ±deg°`) /
    `入力: <buffer>` (数値入力中のみ) / `モード行` (移動|回転 · 軸拘束 ·
    N項目 · 数値入力) / `Enter: 確定 · Esc: 中止`
  - 配置は右下オフセット (+18, +14 DIP)、左右端では反転して
    カーソル下の対象を遮らない。常にインタラクタ内 4px マージンに
    クランプ — 200% DPI (DIP 空間で計算)・高解像度・多ディスプレイ
    (`mapToGlobal`)・カメラ回転で投影不能な点は `None` を返し
    左下ラベルにフォールバック
  - 位置なし/非表示の場合は従来通り `snap-feedback-label` を
    `position='lower_left'` に描画 (フォールバック経路は維持)
  - `_snap_hud_active` フラグで「フィードバック中」を区別:
    実GUI検証で 200% DPI 時に VTK サーフェスの再親子付けが
    Resize/ScreenChangeInternal/Hide を毎フレーム発火させ、
    HUD が表示直後に消され続ける不具合を確認。ジェスチャ中は
    これらのイベントで隠さず (次の push が必ず再配置する)、
    `render_snap_feedback(None)` の明示クリアでのみ畳む
- `world_to_widget_position` を追加: VTK display px → Qt widget DIP
  変換 (`_display_to_widget_position` を再利用)。`SnapSelector` の
  取得/保持半径は DIP 指定のため、プロジェクションも DIP に統一 —
  200% DPI で実効スナップ半径が半減していた不具合もここで解消
- `RoomWorkspace.set_snap_feedback` が `screen_position`/`hud_lines` を
  そのまま viewport に受け渡し (ステータス行テキストは従来通り)
- `RoomEntityTransformController`:
  - HUD プッシュ経路 `_push_feedback`: ドラッグ中の `_preview_move` /
    `_preview_rotate` がスナップ状態・Δ・モード行を組み立てて表示。
    軸拘束 / グリッド / オブジェクトスナップを区別し、無効時は理由
    (Shift一時解除 / オフ / 取得範囲外) を表示
  - 数値差分入力: アーム済みモードで数字・`.`・`-` を打つと入力開始
    (`G/R` 流儀の最小構成)。入力値は display 単位として
    `display_to_si` で m に正規化し `preview_move`/`preview_rotate`
    (軸拘束があればその軸、無ければ X移動/Z回転) に正確な Δ として
    適用 — ドラッグと同じ `TheaterWorkingDocument` プレビュー/コミット
    ポートを通るため 2 重コミットは発生しない。Enter=確定 /
    Esc=取消 / Backspace=訂正 / X・Y・Z=軸再設定 (即時再プレビュー)
  - 入力中は他の全キー・マウスプレスを消費 (Delete/L/矢印など
    誤爆防止): `_modal_key_press` が KeyPress を飲み、
    `eventFilter` が `ShortcutOverride` を accept して
    QShortcut 系の発火経路も閉じる (実GUIで L がロックを発火
    する不具合を確認済み)。ポインタの移動は型値を上書きしない。
    マウスリリースでも Enter でも同じ `finish_at` →
    `commit_preview` 経路
  - ドラッグ中に数字キーを押した場合は既開のプレビューをそのまま
    引き継いで数値入力へ移行 (real-GUI で未捕捉の
    `EditStateError: another preview is already active` が
    出た不具合への対応 — `_begin_gesture` を再呼出ししない)
  - `set_snap_feedback(None)` 時に `吸着:` プレフィックスの
    ステータス行もクリア (残存ラベル不具合)
  - `set_length_policy(policy)` + `bind_length_policy_widget` 経由で
    #496 の表示単位をバインド (HUD テキストと数値パースに反映、
    内部 authority は m のまま)
- `command_registry`: `room.transform.move` に `G` ショートカット
  エイリアスを追加 (Blender 流儀の grab キー)
- 既存振る舞いの保持: ロック選択・can_edit 不可・geometry 編集中は
  `_arm` で拒否 (ステータスに理由を表示)。preview 中断・undo/redo は
  従来の working-document 権威のまま

## 確認

- `backend/tests/test_issue_979_snap_hud.py` (25件):
  HUD が `screen_position` に追従・左下固定でないこと / 端で反転・
  4px クランプ / 位置なし時の lower_left フォールバック /
  ライブ HUD が interactor Resize/Hide/ScreenChange で消えない
  (200% DPI 回帰) / 対象種別・名前・ID・距離・座標の表示 /
  mm ポリシーでの単位表示 / 軸拘束・グリッド・オブジェクトスナップと
  オフ理由の区別 / Shift 一時解除表示 / 0.125 m 移動・15° 回転・
  複数選択・mm 入力の正確コミット / Esc 取消で SceneRevision 不変・
  プレビュー無し / 入力中の Delete・L・矢印キーが発火しない /
  ShortcutOverride も抑止 / ドラッグ中の数字入力が
  EditStateError を出さず引継ぎ / `吸着:` ステータス残存を
  クリア / 入力中のポインタ移動・クリックが型値を確定 /
  Backspace 編集 / ロック選択はアーム不可
- `test_room_cadux.py` / `test_cad_input.py` / `test_command_registry.py` /
  `test_issue_983_pick_candidates.py` / `test_room_viewport_r9.py` /
  `test_placement_constraints.py` / `test_cad_snap.py` /
  `test_room_workspace.py` / `test_rev56_snapstd.py` /
  `test_cad_search.py` / `test_cad_topology_search.py`: 全件緑 —
  既存ドラッグ・nudge・候補チューザー・コマンド登録の契約を維持
- 実 GUI (Mesa GL) 検証: 別タスク — HUD がカーソルに追従し、
  数値入力の確定/取消と 200% DPI でのクランプを確認
  (owned-Windows マウス/VTK/DPI 4 条件マトリクス自体は #804 の範囲)

Refs #979
