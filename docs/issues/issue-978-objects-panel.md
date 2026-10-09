# Issue #978 — オブジェクト一覧の検索・種別グループ・可視状態フィルタと一時隔離

`room_objects_panel.py` を、フラットな 4 列 `QTreeWidget` から
検索欄・状態フィルタ・種別グループ階層を備えた一覧へ作り替え、
「選択物だけを表示」「隔離解除」「フォーカス」を一覧内の操作として
提供する。検索・絞り込み・グループ化は表示整理のみで、
非表示・ロック・削除などの永続状態操作は従来どおり安定 entity ID
への明示操作。新しい scene truth は追加しない。

## Layer shape

- `room_objects_panel.py` モジュールヘルパ（Qt フリー）:
  - `entity_group_key(entity)` — `body_geometry.kind == 'mesh_asset'`
    の entity は `imported_mesh`（取込メッシュ）グループへ、それ以外は
    `entity.kind`。`_OBJECT_GROUP_ORDER` が表示順を固定し、
    未知の kind は既知グループの後ろに定数順で続く。
  - `entity_problem_reasons(document)` — entity_id → 理由タプルの
    dict。信号はドキュメント局所の 3 系統のみ: チャンネルロール未割当
    （`is_unassigned_speaker_role`）、ロール重複
    （`duplicated_speaker_roles`、UNASSIGNED プレースホルダは除外
    される点も journey ゲートと同じ）、中心位置が部屋の範囲外
    （多角形 footprint では shapely の `covers`、矩形では bounds と
    天井高の範囲チェック）。
  - `_entity_search_haystack(entity, kind_labels)` — 名前・種別キー・
    種別 JA ラベル・speaker_role・entity_id を casefold 連結した
    検索対象文字列。
  - `OBJECT_FILTER_ALL/SELECTED/HIDDEN/LOCKED/PROBLEM` と
    `_OBJECT_FILTER_ITEMS`（`全て`/`選択中のみ`/`非表示`/`ロック中`/
    `問題あり`）。
- `RoomObjectsPanel`:
  - 上部行: 検索 `QLineEdit`（objectName `objectsSearch`、即時絞込）、
    状態フィルタ `QComboBox`（`objectsFilter`）、
    `リセット` ボタン（`objectsFilterReset`、絞り込み中のみ有効）。
  - 操作行: `選択のみ表示`（`objectsIsolate`、選択時のみ有効）、
    `隔離解除`（`objectsClearIsolation`、隔離中のみ有効）、
    `フォーカス`（`objectsFocus`、選択時のみ有効）。これらは
    `isolationRequested`/`isolationClearRequested`/`focusRequested`
    シグナルとして放出し、パネル自身はコントローラを触らない。
  - ツリーは `setRootIsDecorated(True)` の 2 段階層。グループ見出しは
    `ItemIsEnabled` のみ（選択不可・ツールボタン列も空）、
    `種別ラベル（N件）` 表記。展開状態は `_collapsed_groups` で保持し、
    sync のたびに復元。primary entity（`selection[0]`）を含む
    グループは常に再展開して pick→一覧の同期で見切れさせない。
  - 行 identity: 0 列に `_ENTITY_ROLE`（安定 entity_id）を保持し、
    `_entity_items` dict が entity_id → item を引く
    `item_for_entity(entity_id)` を公開。show/hide/lock/delete の
    発行は全てこの identity 経由で、ツリー行 index には依らない。
  - 行表示: primary は `▶ ` 接頭辞、問題ありは `⚠` 接尾辞 +
    tooltip に理由列挙、非表示は disable パレット色で保持（従来通り）。
  - ゼロ状態: 絞込結果 0 件では非選択行 1 行に
    `条件に一致する項目はありません — リセットで全表示に戻せます`、
    tooltip にアクティブな検索語とフィルタ名を併記。
  - サマリ行に `表示 N/M`（絞り込み中）、`フィルタ外の選択 X 件`
    （選択 entity が絞込から外れたとき）、`隔離中`
    （`isolation_active=True` で同期されたとき）を追加。
- `RoomWorkspace`（呼び出し側のみの配線）:
  - 3 シグナルを `_objects_isolate`（`isolate_selection`）/
    `_objects_clear_isolation`（`clear_isolation`）/
    `_objects_focus`（`fit_selection`）へ接続。
  - `isolate_selection`/`isolate_kind`/`clear_isolation` が
    `_render` 後に `_sync_objects_panel()` を呼び、
    `sync_document(..., isolation_active=self._pre_isolation_hidden
    is not None)` で隔離表示を同期。
  - 3D pick → 一覧同期は従来の `_entity_picked` →
    `sync_document` + `scrollToItem` の流れを維持しつつ、
    primary の属するグループが畳まれていても自動展開される。

## 権威・安全性

- 隠す・ロックは従来どおり `view_state.hidden_ids`/`locked_ids` の
  永続状態で、一覧のボタンは同じ emit 経路。`隔離` は既存の
  `controller.isolate_entities` + `_pre_isolation_hidden` スナップショット
  機構を再利用する一時表示状態で、隔離解除は必ず事前の非表示集合へ
  戻る（パネル側は `隔離中` 表示とボタン有効化だけを担う）。
- `問題あり` は表示用のローカル信号のみ —— journey ゲートと同じ
  `is_unassigned_speaker_role`/`duplicated_speaker_roles` と
  部屋範囲外チェックをドキュメントから導出し、新しい scene truth
  やブロッキング権威は作らない。
- 編集は全て `_ENTITY_ROLE` の安定 ID 経由。ソート/グループ/
  フィルタで並び順が変わっても別 entity へ誤適用されない
  （Qt テストで emit された ID 集合を固定）。
- グループ見出しは `ItemIsEnabled` のみで identity を持たず、
  選択・ダブルクリック・行操作の対象から除外される。

## 検証

- `backend/tests/test_issue_978_objects_panel.py`（13 件）:
  検索（名前/種別 JA ラベル/ロール/entity_id）、4 状態フィルタと
  リセット、ゼロ状態行の文言+非選択+リセット導線、グループ
  折りたたみ永続と primary 再展開、隔離/解除/フォーカスの
  シグナル発行と有効化、フィルタ外選択の通知、
  `entity_problem_reasons`（未割当・重複・範囲外）と ⚠ 表示、
  取込メッシュ家具の `取込メッシュ` グループ化、フィルタ中操作が
  安定 ID を emit すること、120 entity シーンの sync<5s と
  非表示フィルタ下での show 発行、隔離が一時状態として
  事前の永続非表示を復元すること、フォーカスボタンが
  `viewport.focus_entities` に到達すること、pick で畳まれた
  グループの再展開。
- `backend/tests/test_room_objects_panel.py`（4 件）を
  グループ階層前提に更新 —— 行取得は `item_for_entity` 経由。
- 回帰: `test_rev32_room_guidance` / `test_rev35_ux140` /
  `test_command_registry` / `test_room_cadux` / `test_rev44_surfaces` /
  `test_t18_workspace_ux` 計 84 件パス。
- 実 GUI 検証（検索/フィルタ、グループ展開、隔離/復元、
  pick→一覧スクロール、260px 見切れなし）の録画は PR に添付。
