# Issue #1018 — SupportPage 読み取り専用ヘルスチェック面 (REV73)

SupportPage に `run_health_checks` をそのまま可視化する診断レーンを追加。
既存バリデータの表面化のみ — 新しい検査器は作らない。

## 表面形状 (`application_pages.py::SupportPage`)

「アプリとプロジェクトの状態診断」セクション:
- `supportRunHealthCheck` ボタン → 診断開始。`supportHealthCheckCancel` で
  協調キャンセル。`supportHealthStatus` が最終実行時刻・対象データ
  フォルダー・データ状態フィンガープリントを表示。
- `supportHealthResults` (QScrollArea) にカテゴリ別の検査行を列挙:
  `アプリ・プロジェクトの保存データ` / `データの意味整合性` /
  `外部連携（任意）` の順。各行は JA チェック名・状態・summary・detail・
  原因別ガイダンス。セマンティック状態色 (`set_semantic_state`) で
  PASS/ATTENTION/FAIL を区別。

## 非同期経路 (`support_health_runner.py::SupportHealthRunner`)

`NativeWorkerPool` + `ActivityCenter` の既存約束に従う:
- `operation_kind='support_health_check'`、`CANCELLABLE` +
  `cancel_callback → pool.cancel`、`SAFE_NEW_ATTEMPT` 再試行、
  `BACKGROUNDABLE` ナビゲーション。
- `input_authority_refs=(managed-data:<fingerprint>,)` を押印 — プロジェクト
  切替・復元で `COMPLETED_FOR_HISTORICAL_INPUT` に再分類され、旧
  データ状態の判定が「現在」として残らない。
- 同時実行は 1 本のみ — busy 中の `start()` は False を返す (キューしない)。
- UI スレッドのファクトリがスナップショットを凍結してワーカー job を返す
  (`_support_health_job_factory`): REW URL と受信機状態は UI スレッドで
  採取し、ワーカーが Qt 非安全オブジェクトに触れない。

## プローブ (`support_diagnostics.py`)

`run_health_checks` が受け取る既存の `integrity_runner` /
`integration_probes` フックに配線:
- `semantic_integrity_check` — #426 権威グラフ監査を読み取り専用で実行。
  SQLite `backup()` API でライブ DB を一貫スナップショット化し、
  `measurement-assets/` はハードリンク (失敗時コピー)。プローブ用一時
  ディレクトリは `finally` で必ず除去。ライブ DB は mode=ro 経由で
  読むだけで、リポジトリ構築 (=スキーマ初期化/移行の可能性がある書き込み)
  をライブファイルに対して行わない。
- `rew_api_probe(url)` — 有界タイムアウトの status 読み取りのみ。
- `capture_receiver_probe(state)` — UI スレッドで凍結された状態辞書を評価。
  未構成は NOT_APPLICABLE (障害ではない)。
- `vtk_probe()` — `importlib.util.find_spec` による import 可否のみ。
  GL コンテキストは絶対に作らない (壊れた GPU 環境をさらに壊さない)。

## 重要度の誠実さ

- `HealthReport.overall` が既に実装する規則: INTEGRATIONS の FAIL は
  全体を ATTENTION に留め、APP_STORAGE/SEMANTIC_INTEGRITY の FAIL のみが
  全体 FAIL を出す。ページ側はカテゴリヘッダーを独自ロールアップ
  (FAIL > ATTENTION > UNKNOWN > PASS) で表示し、連携セクションには
  「アプリやプロジェクトDBの障害ではありません」の注記を出す。

## ガイダンス対応 (`health_check_guidance`)

check_id → (JA 対処文, 遷移先 surface) の対応表。遷移先は全て既存面:
`data_management` (バックアップ復元プレビュー), `authority` (権威グラフ),
`preferences`, `capture_settings`, `activity` (アクティビティワークスペース),
`export` (診断パッケージ)。未知の check_id はカテゴリ既定にフォールバック。
PASS/NOT_APPLICABLE はガイダンスなし。自動修復は一切行わない — 修復系の
文言は必ず復元プレビュー経由。

## 再表示の誠実さ

`refresh()` は保持しているレポートを再描画するだけで、背後で診断を
再実行しない。表示中のレポートは実行時刻・対象データディレクトリ・
フィンガープリントを常に伴う。

## テスト (`backend/tests/test_issue_1018_support_health_check.py`)

16 本: ボタン→非同期 job→結果描画、実ランナーの ActivityCenter 記録、
busy 中の再実行拒否、キャンセル経路 (cancelled が結果にならない)、
失敗 job の正直な報告、連携 FAIL の非エスカレーション、ガイダンス対応表
全項目 + フォールバック、ガイダンスボタンの実面遷移、全 PASS 偽装なし、
refresh の再実行なし・時刻保持、フィンガープリント表示、
narrow/DPI200/UIA 到達可能性、close 時の runner drain、
semantic check の読み取り専用 (一時 dir 残置なし・ライブファイル不変)、
実 runner 経由の run_health_checks E2E。
