# Issue #883 — クラッシュ安全な自動保存とセッション復旧

アプリが強制終了・クラッシュ・OS 再起動で途切れても、未保存の作業状態を
「推測せず、証拠のある範囲だけ」復元できるようにする。ジャーナルは
揮発トレースであり、正本は常にシール済み権威 (sealed authority) 側に
残る — 復旧は既存の正本を上書きしない。

## 構成

- `session_recovery.py` — セッションジャーナル、終端分類、
  検査・照合、意思決定、リテンションの中核。
- `session_recovery_repository.py` — 復旧権威のシール済みストア
  (3 テーブル、ネイティブスキーマ v100)。
- `session_recovery_dialog.py` — 起動時の復旧ダイアログ (日本語 UI)。

### セッションジャーナル (揮発)

`<data_dir>/session-journal/<session_id>/` に `envelope.json` +
`journal.jsonl` (+ 大きいペイロード用 `payloads/`) を置く:

- エントリは SHA-256 チェーン (`prev_sha256` → `entry_sha256`) で連結し、
  追記専用。末尾の不完全行は「破断」として許容し、途中改竄は失敗で
  閉じる (fail closed)。
- サイドカー (`payloads/<sha>.json`) はハッシュ検証してから展開する。
- シークレット系キー (`password`, `*_token`, `api_key`, `secret` 等) は
  付け表されず書き込み拒否。
- ハートビート + 運用イベントの結合上限で圧縮 (`snapshot_compaction`
  エントリに折り畳み)、リテンションで本数・年代を制限。

### 記録する状態

ワークスペース/ナビゲーション (destination, mounts, dirty_documents,
contexts)、未保存シーンドラフト (document_id + content_hash +
source_revision_id)、保留アノテーション、保留インポート (REW 本文は
base64、スナップショット実体は「再取得必要」フラグのみ)、進行中の
オペレーション (`device_apply_transaction` / `sweep_acquisition` /
`commissioning_run` の開始・終了)、取得ステージ、ファイル書き込み
インテント (期待 sha256)、プロジェクトバインド、障害分類。

### 終端分類 (証拠順序)

`classify_session_ending` は厳密に証拠だけで分類する:
`closed_clean` マーカー → ジャーナル/ネイティブスキーマ不整合
(`incompatible`) → pid 生存 (`still_running`) → ブートトークン変化
(`os_restart_or_power_loss`) → 障害記録/起動レコード相関
(`application_crash`) → 上記いずれでもなくプロセス消失
(`forced_termination`)。推測で「正常に終わった」とはしない。

### 照合 (reconciliation)

起動時の `inspect_recoverable_sessions` が未解決セッションごとに
`SessionRecoveryReport` を組み立てる。アイテムは
`available`/`blocked` (証拠ハッシュ不一致等) で表示し、照合結果は
`RECONCILIATION_REQUIRED` / `DEVICE_STATE_UNKNOWN` /
`ACQUISITION_INCOMPLETE` / `WRITE_COMPLETION_UNKNOWN` — 成功は推論
しない。デバイス読み戻しフックがある場合のみ `confirmed` に解決し、
`SessionReconciliationRecord` をシールする。

### シール済み権威 (スキーマ v100)

`session_recovery_journals` (検知記録、session_id で一意、
`head_entry_sha256`/`envelope_sha256` でジャーナルへの照合可能)、
`session_recovery_decisions` (追記専用の意思決定ログ:
`restore_accepted`/`reconcile_*`/`restore_completed`/`discarded`/
`deferred`/`evidence_rejected`/`retention_discard` — `restoring_session_id`
で系譜を保持)、`session_recovery_reconciliations` (照合結果)。
監査配線済み: `_ROW_BINDINGS` / `_RepositoryChain('session_recovery')` /
`_ReplayProbe` x3。バンドルエクスポートは `NATIVE_TABLES` 経由で自動。

### UI

起動時、検知済みで未解決のセッションごとに終端種別・ビルド・時刻・
復元可否を示すダイアログを表示する (復元する / 復旧データを破棄 /
あとで決める)。「あとで決める」は `deferred` をシールして保留のまま
次回再提示、破棄は確認後に `discard_session` で決定をシールして
ジャーナルを削除、拒否された証拠は `evidence_rejected` をシール。

## 受入基準との対応

- 強制終了でも未保存編集が残る — 実プロセス kill テストで検証。
- 復元状態はコミットされるまで識別される — 復元は
  `restore_accepted` → (照合) → `restore_completed` のシール済み
  系譜として残り、正本を暗黙に上書きしない。
- 測定中クラッシュは完了測定にならない — 非終端ステージは常に
  `ACQUISITION_INCOMPLETE`。
- デバイス適用後・検証前のクラッシュは `RECONCILIATION_REQUIRED`
  / `DEVICE_STATE_UNKNOWN`、読み戻しフックで解決可能。
- 破損・互換性なしデータは理由付きで拒否。
- リテンションは件数・年代で上限化し、未解決分は捨てる前に
  `retention_discard` をシール。

設計: `docs/design/issue-883-session-recovery-design.md`。
