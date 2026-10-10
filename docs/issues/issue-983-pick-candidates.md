# #983 重なった3Dオブジェクトの選択候補を可視化し対象間違いを防ぐ

## 変更

- `RoomViewport3D` に候補チューザー (pick-candidate popover) を実装:
  - `pick_actor_candidates` の複数候補 (= `vtkCellPicker` の前面→背面
    Prop3D スタックが2件以上) のときだけ、カーソル近傍に
    `_PickCandidatePopover` を表示。ヘッダは `重なり候補 {i}/{n}`、
    行は `{i}. {名前} · {種別}` (種別は一覧 #978 と同じ和名
    `_PICK_KIND_LABELS`)
  - 候補は安定 entity ID の `PickCandidateEntry` で保持。ロック中は
    `ロック中`、非表示は `非表示`、シーン外は `シーンに存在しません`、
    文書全体が編集不可 (`can_edit` False) は `編集不可` を行に明示 —
    選択は常に許可、選択 ≠ 編集許可
  - 従来の同一箇所クリック巡回 (`_cycle_pick_candidate`) はそのまま
    維持し、チューザーの候補/インデックスは `_cycle_ids` /
    `_cycle_index` を共有するため追加の scene pick は一切行わない
    (#867 ベンチ対象: 開閉・巡回・確定・解除の全経路で picker.Pick
    発行ゼロを `test_no_scene_repicks_through_chooser_lifecycle` で固定)
- 操作系:
  - マウスホイール / ↑↓キー / Tab・Shift+Tab で候補を巡回
    (Tab=次・Shift+Tab=前・ホイール下=奥方向)
  - Enter で確定 → `entityPicked`/`entitySelected` を通常pickと同じ
    経路で発火するため、オブジェクト一覧 #978・インスペクタ・
    view_state.selection が同一に同期
  - Esc で破棄 (選択は確定直前まで変更しない)
  - 行クリック = その候補の即確定 (メニュー意味論)
  - `_PickCandidateKeyFilter` をpopover表示中だけ interactor に
    install (最後のinstall=先頭実行のため transform/camera 系フィルタ
    より先にキー/ホイールを消費)。ShortcutOverride も受理するので
    ワークスペースの Esc=中止 / Enter=確定ショートカットが背後で
    発火しない
- preview-only ハイライト: 巡回中の候補には `pick-candidate-{id}` の
  ワイヤフレーム包絡 actor を表示 (確定前に SceneRevision を書き換え
  ない)。現在選択中の候補は本来の選択アウトラインがあるため preview
  は描かない。セクションカット中は `_apply_section` 適用済み
- 古い候補セットの破棄 (`dismiss_pick_candidates`):
  - カメラ変化: pan/orbit/zoom/標準view/fit/focus/camera スナップショット
    復元/view-from-seat
  - セクションカット変更 (`set_aux_render_state`)
  - `render_document` 再構築時に document または hidden 集合が変化
    (同一documentの選択/ロック再描画では行更新+preview再配置で生存)
  - marquee 活性化 / コンテキストメニュー / 空クリック /
    underlay・proposed pick / リサイズ・DPI変化 / フォーカス喪失
  - active gizmo: `RoomEntityTransformController._arm` が閉じて
    `pick_popover_enabled=False` で抑止、ジェスチャ終了で復帰
- `RoomWorkspace._pick_candidate_uneditable_reason` を
  `pick_candidate_reason_provider` として接続 — viewport から見えない
  編集不能事由 (回復候補・room 未定義・preview 中) を行理由に反映

## 確認

- `backend/tests/test_issue_983_pick_candidates.py` (17件):
  複数候補で `1/3 · 名前 · 種別` 表示 / 単一候補では開かない /
  キー・ホイール巡回 / Enter確定 / Esc破棄 / 行クリック確定 /
  ロック理由 / 編集不可理由 / preview は render-only /
  camera・section・document・hidden・marquee で破棄 /
  gizmo 抑止 / 再pick ゼロ / 従来クリック巡回の温存
- `test_room_viewport_r9.py` 他 既存 viewport/workspace/input 群:
  全件緑 (marquee・click-through・再入安全の既存契約を維持)

Refs #983
