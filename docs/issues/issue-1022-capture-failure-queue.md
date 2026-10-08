# Issue #1022 — Capture監視失敗の正直な完了表現 + 失敗キュー

## スコープ

キャプチャ監視フォルダーの失敗を「完了」扱いにしていた fail-open の
表示契約を、型付きの終了状態へ直す (#988 受信ボックス検索が届かない
「inbox 行ゼロ」の経路を閉じる)。自動再試行の上限
(`_ROUTE_MAX_ATTEMPTS = 3`) に達して `_seen` マーカーが残った
ファイルは従来、UI 上どこにも現れなかった。本 issue では永続化
された有界な失敗キューを受信ボックスに追加し、オペレーターが
明示的に再処理できる面を提供する。昇格・スキーマ修復・自動再登録は
一切しない (完了状態 ≠ 権威昇格 ≠ inbox ステージ ≠ プロジェクト
割当 — 混同しない)。

## 実装

- `backend/src/htdt/capture_watch_failures.py` (新規)
  - `CaptureWatchFailure` — 凍結モデル: `path` / `basename` /
    `error_kind` (サニタイズ済みの失敗種別) / `failure_class`
    (`retryable` / `unsupported` / `user_action_required`) /
    `detail` / `attempts` / `first_seen_utc` / `last_seen_utc` /
    `(mtime_ns, size)` フィンガープリント / `watch_root` /
    `diagnostic_id` (経路ごとに安定した `[diag: XXXX]` 相関ID)。
  - `CaptureWatchFailureQueue` — データディレクトリ直下の
    `capture-watch-failures.json` (schema_version=1)。パスごとの
    upsert、`first_seen`/相関ID の保持、上限 50 件 + TTL 14 日の
    prune、mkstemp+os.replace によるアトミック保存 (保存失敗は
    非致命的)、破損ファイルは空として読む。新しい到達ソース
    `watch_queue_retry` を定義 (実際の drop の `watch_folder` とは
    区別される正直な出自)。
  - `verify_watch_retry` — 「安全に再試行」ゲート:
    クラス → 存在 → フィンガープリント `(mtime_ns, size)` →
    ウォッチルートのエポックの順に検査し、
    `OK` / `NOT_RETRYABLE` / `DELETED` / `REPLACED` /
    `EPOCH_MISMATCH` をオペレーター向け理由つきで返す。
    書き込み途中のファイルはシグネチャが違うので `REPLACED` で
    拒否される。
  - `classify_route_failure` — ルーティング例外を
    `routing_error`/`retryable` に、`invalid_or_unsupported` を
    `unsupported` に、`user_action_required` を
    `user_action_required` に、その他の失敗アウトカムを
    `import_failed`/`retryable` に写像する。生の例外文字列は
    キューに入れない (ログ側に残る)。
  - `write_watch_failure_diagnostic` — サポート共有向けの
    サニタイズ済みメモを `diagnostics/capture-watch-failure-<id>.json`
    へ。basename・種別・試行数・フィンガープリント・ルートの
    basename のみ — フルパスも生バンドルも書かない。

- `capture_watch_runner.py` — `routes_exhausted = Signal(object)` を
  追加。試行回数が上限に達した drop は `WatchRouteExhaustion` として
  放出される (`_seen` に残ったシグネチャがそのまま失敗した
  バイト列のフィンガープリント)。自動再試行のループ・ストール
  ガード・世代管理は据え置き (#1014/#1021 のスコープ)。

- `workflow_application.py`
  - `_on_capture_watch_completed` — 型付き完了:
    - 全件ステージ → `capture_watch_stage` を COMPLETED。
    - 全件失敗 → `fail(error_summary=…)` (result_summary に
      例外を隠して成功を装わない)。
    - 混在 → 「partial」終了状態が存在しないため、ステージ側と
      失敗側に分割して COMPLETED 行 + FAILED 行の 2 オペレーション
      (片方の結果がもう片方の嘘にならない)。
    - ステージされたパスは失敗キューから `resolve()`。
  - `_on_capture_watch_exhausted` — 上限到達レコードを永続キューへ
    記録し、15s ステータスバーで失敗キューへの導線を出し、
    inbox マウントを再描画。
  - `_retry_watch_failure` — 「安全に再試行」:
    `verify_watch_retry` の `OK` の場合のみ、バンドルプール上で
    実ルーティングへ投入 (`_bundle_busy` による同時実行制御と
    アクティビティ行の RUNNING→終了状態を持つ)。検証失敗は警告
    ダイアログで理由を示して終わる。
  - `_import_watch_failure_file` — 「このファイルを選んで取込」:
    フィンガープリント/エポックを敢えて通さない明示的取込
    (オペレーターが現在のファイルを取り込みたいと明示した場合)。
    存在確認のみ。コンテンツの検証はルーター自身が行う。
  - `_watch_queue_route_done` — 再取り込みの終了処理:
    `staged_for_review`/`already_staged` → キュー解消 + COMPLETED。
    それ以外 → `record_retry_attempt` で試行数を繋げて FAILED。
  - `_diagnose_watch_failure` — 「サポート診断」: 上記の
    サニタイズ済みメモを書き出し、相関 ID と保存先を表示する。

- `application_pages.py` — `CaptureInboxPage` に「失敗キュー」タブ
  (配送タブの隣)。表: ファイル名・エラー種別・分類・試行回数・
  最終確認 (UTC)。行選択で詳細 (記録理由・初回/最終確認・
  ウォッチルート・現在の検証状態・再試行不可理由・診断ID) を表示。
  ボタン: 「安全に再試行…」(検証 OK のみ有効、実行前に元情報と
  記録理由を確認表示)、「このファイルを選んで取込…」(REPLACED
  でも選択可、内容差異は事前表示)、「サポート診断」。
  `refresh()` で失敗キューも再読込。

## 混同しない境界

- 完了状態 (アクティビティ行の terminal state) / 権威昇格
  (evidence promotion) / inbox ステージ / プロジェクト割当は独立。
  本 issue は完了表現の正直化と手動再処理のみ — 自動スキーマ修復や
  自動昇格は実装しない。`invalid_or_unsupported` は元の拒否理由を
  そのまま表示する。

## 残存ゲート

- Win11 実機 GUI での最終目視 (タブ配置・日本語ラベル) と
  エポック競合の長時間観測は Devin セッション上では完全再現できない
  — offscreen テストで代替し、実機確認は release gate 側に残す。

## 検証

- `backend/tests/test_issue_1022_capture_failure_queue.py` — 12 件:
  全成功 COMPLETED / 全失敗 FAILED / 混在の 2 行分割、ランナーの
  exhaustion 放出 (フィンガープリント = `_seen` 署名、inbox 行なし)、
  キュー永続化 + 再読込、フィンガープリント/エポック拒否全種別、
  REPLACED 拒否 + ルート未投入、検証済み再取込 → 正確に 1 行 +
  `watch_queue_retry` 出自、失敗再取込で試行数繋げ + FAILED、
  inbox ページの行表示とボタン有効化遷移、診断メモのパス漏洩なし、
  破損ストアの fail-closed 空読込。
- `backend/tests/test_workflow_application.py` の REV43-SEAMS を
  FAILED+error_summary 契約へ更新。
- 実行: `test_issue_1022_capture_failure_queue.py` +
  `test_capture_watch_runner.py` + `test_workflow_application.py`
  で 50 件パス (Windows offscreen, Python 3.12.10)。
