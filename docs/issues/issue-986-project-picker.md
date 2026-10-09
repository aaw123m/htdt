# #986 プロジェクト一覧の検索・絞込・並替 + 安定ID選択

## 変更

- `filter_project_entries()` (application_pages) — 一覧画面とメニューの
  「プロジェクトを開く」選択ダイアログで共有するフィルタ語彙:
  - 名前の部分一致検索 (casefold)
  - 並替: 最近使った順 (既定) / 作成日時 / 名前
  - 状態絞込: 全件 / 作業中 / アーカイブ済み
- `ProjectEntry.last_opened_at_utc` を新設 (canonical record の
  last-opened を表示用に渡す。未定義は空欄)
- `ProjectLibraryPage`:
  - 検索欄 + 並替/状態コンボ (2行ヘッダ)。テーブルは7列化し
    「最終アクセス」「ID先頭8文字」を追加 (既存5列の位置は維持)
  - 選択は `project_id` にピン — refresh/並替/絞込の後も同じ ID の行が
    見えていれば行位置ではなく ID で復元する
  - `selection_status` (objectName `projectSelectionStatus`) に
    選択中プロジェクトが開けない理由を表示
    (アーカイブ済み / 現在開いている / リビジョン無し)
- `workflow_application._choose_project`:
  - 同じ検索/並替/状態コントロールをダイアログにも追加
  - `_refill()` はダイアログ再読込の際、UserRole の `project_id` で
    選択を保持 (同名案件へ drift しない)
  - アーカイブ済み行は表示名に `（アーカイブ済み）` を付与

同名案件の誤選択防止のため、行→プロジェクト解決は全経路で
`Qt.UserRole` の `project_id` を用いる (表示名一致は使わない)。

Refs #986
