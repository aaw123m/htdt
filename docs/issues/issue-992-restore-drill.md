# #992 バックアップの隔離復元テスト（リストアドリル）

データ管理に「このバックアップを復元テスト」アクションを追加し、選択した
バックアップをユーザー指定の隔離ディレクトリへ**本物の復元機構で**復元・
独立検証する。本番データ・プロセス・キャッシュ・資格情報ボールトには
一切触れない。結果は「証明したこと/証明していないこと」を分離して正直に
報告する。

## Authority: `htdt/restore_drill.py`

- `run_restore_drill(backup_path, sandbox_root, live_data_dir, ...)` —
  1回の演習。フェーズ: archive（検証+条件判定）→ restore（隔離復元）→
  verify（独立検証）→ migrate（必要時のみ、サンドボックス内で実移行）。
  `BackupCancelledError` は伝播させてコントローラのキャンセルレーンへ。
  `RestoreDrillError` はサンドボックス不適格時に作業開始前に送出。
- `RestoreDrillResult` — 評決 (`restorable` / `restorable_with_conditions`
  / `failed` / `not_verifiable`) + チェック列 + `claims`（証明した状態）
  + `non_claims`（証明していない状態）の不変レコード。
- `RestoreDrillCheck` — `check_id` / `status`（passed/failed/conditional/
  unknown/skipped）/ `detail_ja` の3点。署名チェックは形式に署名欄が
  ないことを `skipped` で正直に報告する（偽の UNKNOWN でも PASS でもない）。
- `_assert_sandbox_isolated` — サンドボックスと live ルートの双方向の
  包含関係を拒否（中に入れても、live を内包させても fail closed）。
  書込プローブで作成可能性も確認。
- `latest_drill_result(data_dir)` — 追記専用 JSONL ジャーナル
  （`restore-drill-results.jsonl`、registered operational component、
  backup 対象外・relocation 時は持ち越し）の末尾結果を読む。
  破損行は `None` で安全側に倒れる。

## 検証内容（すべて独立の再検証）

| チェック | 内容 |
|---|---|
| `archive_verified` | 既存 `inspect_backup`（第2の検証器は作らない） |
| `build_provenance` | manifest の作成ビルド。無ければ `unknown`（旧形式） |
| `signature` | 形式に署名欄がない事実を `skipped` で明示 |
| `schema_compatibility` | `native_schema_compatibility` — 現行 pass、移行必要 conditional、より新しい incompatible は即 failed |
| `archive_degraded` | 宣言済み stale authorities の件数を `conditional` で明示 |
| `disk_space` | 展開後サイズ×2+64MiB 未満の空きなら failed（fail closed） |
| `isolated_restore` | 既存 `restore_backup` を空の隔離 dir へ実行 |
| `sqlite_integrity` | 復元後 DB を read-only URI で integrity+FK チェック |
| `landed_hashes` | 着地した全ファイルを manifest と再ハッシュ照合 |
| `same_machine_opened` | `_assert_staged_database_openable`（実オープン経路） |
| `authority_audit` | `audit_native_authority_graph` を clone に再生。clone は復元 data ルート内に置き managed-asset 相対パスを解決可能にする。宣言済み stale のみ許容 |
| `scene_count` | 読取専用 `scene_document_heads` 件数 |
| `schema_migration` | 移行必要な場合 `execute_native_upgrade` を sandbox 内で実行し全検証を再走（成功=conditional、失敗=failed） |
| `live_data_untouched` | `managed_data_fingerprint` の演習前後一致（不一致なら verdict を failed に倒す） |
| `sandbox_cleanup` | `htdt-drill-<id>/` の完全削除 |

## claims / non_claims の分離（虚偽成功の防止）

`claims` は実証済みの状態のみ: `archive_verified` /
`isolated_restore_succeeded` / `same_machine_opened`。
`non_claims` は常に `other_pc_migration` / `physical_disaster_recovery` /
`operator_acceptance` — 別PC移行や物理復旧、運用者受入は本演習では
証明できないことを明記する。

## Wiring: `htdt/data_management.py` + `data_management_ui.py`

- `DataOperationKind.RESTORE_DRILL`（ActivityCenter タイトル
  「バックアップの復元テスト」、CANCELLABLE、lifecycle_mode `'none'` —
  live ツリーは指紋採取のみで、quiesce 不要）。
- `DataManagementBackend.run_restore_drill` /
  `DataManagementController.restore_drill` /
  `restore_drill_completed` シグナル。
- データ管理画面: 世代行に「このバックアップを復元テスト…」ボタン、
  隔離フォルダ選択（`choose_drill_sandbox`）、結果カード
  （評決+全チェック+証明/未証明の分離表示）、「前回の復元テスト」
  サマリ行。

## Tests: `backend/tests/test_issue_992_restore_drill.py`

8件 — 正常アーカイブ全脚 pass+本番不変+journal round-trip、改竄
アーカイブ failed（claim しない）、不在ファイル、サンドボックス包含
拒否（双方向）、キャンセル伝播、既存バックアップ非削除、複数結果
追記、旧形式（バージョン無し）アーカイブの conditional+migration 脚。
