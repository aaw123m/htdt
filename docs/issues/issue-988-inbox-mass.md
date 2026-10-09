# Issue #988 — Capture Inbox 大量受信トリアージ

キャプチャ配送タブに検索・状態別キュー・ソート・グループ化と
「次の未処理を表示」を追加し、10/100/1000件規模の受信でも
誤昇格なしにさばけるようにする。検索・並び替えはあくまで表示整理であって
承認権限にはならない —— 昇格・却下・割当は従来どおり各行の
`lineage_digest` への明示操作のみ。

## Layer shape

- `application_pages.py` モジュールヘルパ（Qt フリー）:
  - `_inbox_queue_state(item)` — 行の保持 facet のみから状態キューを
    導く。`deferred`/`rejected`/`processed`(promoted・superseded・
    partially_promoted 以外) / `blocked`（バンドル未検証、同一性
    ダイジェスト競合、依存未解決、証拠競合 open、整列 blocked —
    `_check_promotable` の veto と同じ条件）/ `promotable`（検証済み
    + スコープ割当済み + 非 blocked）/ `pending`（その他）。
    inspect() を呼ばないため 1000 件でも即座に再絞り込みできる。
  - `_inbox_search_text(item)` — スコープ/シリーズ/リビジョン/分類
    (+flags)/disposition/到着元/到着詳細/ID を小文字化した haystack。
  - `_inbox_sort_key` / `_inbox_group_sort` / `_inbox_group_title` —
    到着昇降順・状態キュー・スコープ・シリーズ・分類の安定ソートと
    グループ見出し。ソートキー末尾は `lineage_digest` で完全決定。
  - `_inbox_state_cell(item)` — 状態列はキュー語彙を主表示とし、
    生 disposition が追加情報を持つ場合のみ括弧で併記
    （例: `ブロック（一部昇格）`）。
- `CaptureInboxPage`（キャプチャ配送タブ）:
  - フィルタ行: 検索 `QLineEdit`、状態 `QComboBox`
    （要レビュー=保留中+昇格可能+ブロック / すべて / 6 キュー別、
    各エントリに件数併記）、ソート、グループ、
    `要レビュー N 件 / 表示 M / 全 K 件` カウンタ。既定は
    要レビュー —— 処理済み・却下・延期は既定では見えない。
  - 主操作: 「次の未処理を表示」（アクション可能キューのみ巡回、
    グループ見出し行は選択不可なので止まらない）と
    「詳細を確認」（未選択なら次の未処理を選んでから詳細へフォーカス）。
  - 詳細ペインに 3 行追加: `適用可能な操作`（_sync_actions の
    有効化規則の表示ミラー）、`不足・ブロック理由`（検証/依存/整列/
    証拠競合/未割当/昇格不能権威の detail 付き理由）、
    `次のアクション`（キュー+promotability からの最短提案）。
  - 狭幅・高 DPI 折りたたみ: `_actions_required_width()` で
    子ウィジェットの sizeHint 総和（stretch のスコープコンボは
    minimumSizeHint のみ計上）を実測し、行の必要幅がページの
    与えられる幅を超えたとき二次操作（延期/却下/再開/昇格/割当/リンク）を
    「操作 ▾」QToolButton+QMenu へ退避。固定の px 閾値はページ自身の
    最小幅を割って発火しない可能性があるため使わない（実 GUI で検証済）。
    主操作は折りたたまない。メニューの有効化はボタンと同一規則
    （`aboutToShow` で再同期）。
  - 行 identity: 0 列セルに `inbox_item_id`（UserRole）と
    `lineage_digest`（`_INBOX_LINEAGE_ROLE`）を保持。ソート/
    フィルタ/グループ/refresh 後も `_select_delivery_row` で
    同じ項目へ再選択。group 見出し行は `ItemIsEnabled` のみ・
    span 1 行で identity を持たず、`_displayed_items`（None=見出し）
    と並行管理して選択・巡回から除外。
  - `_refresh_keep_selection`: 操作後に行が絞り込みから外れたら
    次の未処理へ自動送り —— 古い選択や別行への誤適用を防ぐ。
  - `reveal_all_items` + `inbox_focus` 更新: 深リンク
    （測定ワークスペース/フィールドリターン等）は絞り込みを
    「すべて」へ戻してから ID 検索 —— 延期/処理済み行にも到達可。
    測定取り込みリンクは `WorkspaceDeepLink(MEASUREMENT, "import")`、
    フィールドリターンは同ページ内タブ切替で誠実に遷移。
  - ゼロ状態: 絞り込み結果 0 件 → 「条件に一致する項目はありません」、
    受信 0 件 → 従来の空案内、未処理 0 件 → 「未処理の項目はありません」。

## 権威・安全性

- `_inbox_queue_state` の blocked 判定は `_check_promotable` の
  veto facet をミラーしているだけ —— 昇格可否の権威は依然
  `CaptureInboxRepository.inspect` + 既存の
  permission/explicit-confirmation 経路（理由入力ダイアログ、
  スコープ割当確認）。検索結果の並び順は承認意味を持たない。
- 処理済み/却下行では昇格・延期・割当ボタンが無効化されることを
  Qt テストで固定（`test_processed_and_rejected_rows_offer_no_approval`）。
  自動一括昇格は導入しない（明示要求なし・誤昇格リスク）。
- 受信順変化・通知同期への耐性: `refresh` は `_items` スナップショットを
  取り直して `_rebuild_delivery_rows` に委譲するため、届出順が変わっても
  選択 identity が維持される（`test_sort_modes_and_identity_pinned_selection`）。

## 検証

- `backend/tests/test_issue_988_inbox_mass.py`（10 件）:
  キュー分類の gate 一致、既定要レビューの件数/ゼロ状態、
  10/100/1000 件の即時絞り込み（`_refilter` 実測 < 2s、実測値は
  約 0.1s 未満）、ソート後の identity 維持、グループ見出しの
  非選択性と次の未処理巡回、処理済み/却下行の非承認、詳細の
  操作/理由/次アクション行、狭幅折りたたみと メニュー経由操作、
  `inbox_focus` の reveal、実リポジトリでの延期→自動送り→割当の
  identity 保持。既存 `test_application_pages.py` は新フィルタ
  意味に合わせて更新（延期行は延期キューで確認）。
- 手作業メトリクス（#936 指標、実 GUI 計測）: GUI 上で 1 件の
  延期処理 = 3 クリック + 理由入力で約 8-10 秒。次の未処理は
  1 クリック/進行、4 件の要レビューを流すのに約 4 クリック。
  実機検証（mixed-state 6 件シード、絞り込み/ソート/グループ/次の未処理/
  詳細/深リンク/狭幅折りたたみ/メニュー経由の延期→自動送り）を
  録画付きで PR に添付。
- 受信側・デバイス側への自動操作は一切なし。
