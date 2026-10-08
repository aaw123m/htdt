# #880 owned-Windows UX160 受入証跡ランナー

## スコープ

UX160 受入マトリクス (#804) の客観的・反復的部分を自動化する。視覚的・
主観的な基準への人間判断は残したまま、エビデンス取得だけを1コマンドで
実行・封存する。成果物はランナー + 証跡 authority であり、UX160 の
受入宣言ではない。

- `scripts/ux160_acceptance_run.py` — マトリクス1行分の収録ランナー
- `backend/src/htdt/cad_ux_acceptance_evidence.py` — 封存バンドルレコード
  (`CadUxAcceptanceBundleRecord`, `uxbnd-<sha>`) および検証語彙
- `backend/src/htdt/cad_ux_acceptance_evidence_repository.py` —
  `cad_ux_acceptance_bundle_records` テーブルの `_SealedStore` リポジトリ
  (append-only、列↔payload 突合、tamper は `DeploymentIntegrityError`)

## 語彙 (verdict / state)

自動 PASS が行の受入を意味しないよう、語彙は取得状態だけを述べる:

- `UxBundleVerdict`
  - `evidence_captured` — 要求した全チェックポイントが実行済み
    (`finding` は格下げしない: finding は取得済み証跡そのもの)
  - `capture_incomplete` — 走行完了したが `blocked`/`skipped` の
    チェックポイントが残った (ナビゲーション拒否など)
  - `capture_failed` — ドライバが完走しなかった (クラッシュ/タイムアウト/
    起動失敗)。`failure_detail` 必須
- `UxCheckpointStatus` — `pass` / `finding` / `blocked` / `skipped`
- `UxReviewState` — `manual_review_remaining` / `manual_review_none`。
  手動項目が1つでも残る行は構造的に `manual_review_remaining`
  (レコード検証でそれ以外を拒否)
- `UxCaptureMode` — `owned_windows` / `offscreen_fixture`。fixture
  バンドルは `non_claims` に offscreen 非主張を必須とし、
  owned-Windows 証跡に成りすませない
- `UxBaselineVerdict` — `matched` / `diverged` / `not_run`

## 証跡モデル

1行 = `<work>/<matrix_id>/<row_id>/attempt-<NNN>/` の1バンドル:

- `manifest.json` (`htdt-ux-acceptance-bundle-1`) — 環境束縛、
  チェックポイント、メトリクス、アーティファクト SHA、手動項目、
  非主張、`lifecycle_ref`
- `shots/*.png` — canonical スクリーンショット (checkpoints の
  `evidence` に sha256 束縛)
- `report.md` — 人間向けレポート (チェックポイント表 + 手動項目
  チェックボックス + 非主張)
- `driver/driver_result.json` + `driver/stdout.log` + `clean_exit.marker`
- `latest.json` — `<matrix>/<row>/` 直下の安定ポインタ:
  `{latest_attempt, bundle_ref, record:{table, bundle_id, bundle_sha256}}`。
  `scripts/issue_lifecycle.py` / #804 系 surface が参照するための
  安定 ref (本 issue では manifests を編集しない)

封存レコード (`CadUxAcceptanceBundleRecord`) は `bundle_ref`・
`manifest_sha256`・`report_sha256`・`artifacts[]` (sha256/byte_count)・
環境束縛 (#833 パターン: version/commit/dirty/source + python/Qt/PySide/
qpa/QT_SCALE_FACTOR/レンダラ/画面列 + lock ファイル sha + env fingerprint)
を同一 sha に含む。`manifest_sha256` は二段書き (artifacts 空 → 収集後に
再書込) で manifest 最終バイト列を束縛し、`bundle_id` ↔ manifest の
ハッシュ循環を回避する。

## 実行面

- 収録器はデフォルトでドライバをサブプロセス起動
  (`--driver` 内部モード、同じスクリプト)。環境に `QT_SCALE_FACTOR` と
  `HTDT_UX_CAPTURE_MODE` を立てる。`--in-process` は fixture/診断用。
- VTK/GL プローブは別サブプロセスで実行 — ネイティブクラッシュ
  (access violation) がランナーを落とさないよう fail-closed
  (`unavailable`/`unverified`/`probe_crashed(rc=N)`) で文字列化する。
- チェックポイントは `ux160_driver.py` の安定フック
  (`WorkflowApplicationComposition` / router / window.navigate /
  `_settle`) を使う。ピクセルマクロは使わない (non_claim に明記)。
- `run_attempt` は「ディスク上の最大 + 1」とリポジトリ
  `next_run_attempt()` の max — append-only で再利用しない。
- `--baseline <manifest>` でジオメトリ等のスカラーを前回バンドルと比較
  (window_allocated / 各 destination の page_size・overflow_count・
  navigated・page_visible)。差は `baseline_divergences` に列挙。
- 終了コード: `0` captured / `3` incomplete / `2` failed / `1` usage。

## 統合ポイント

- `native_authority_audit.py` — `_ReplayProbe('ux_acceptance_bundle',
  'cad_ux_acceptance_bundle_records', ('bundle_id',), …)` +
  `_RepositoryChain` 分岐 `ux_acceptance_evidence` (replay_canonical)
- `native_row_integrity.py` — `_ROW_BINDINGS` に全 16 列束縛
  (`manifest_sha256` のみ optional)
- `application_pages.py` — `_LIFECYCLE_TABLE_LABELS` に
  「UX受入証跡バンドル」
- スキーマ: `cad_ux_acceptance_bundle_records` (v104)、DDL は
  `NATIVE_BASELINE_DDL`、`_migrate_103_to_104` で再生
- `test_cad_schema.py` 台帳行 `(104, 'migrate native schema to v104')`

## 手動レビュー項目 (自動化しない)

`UX160_MANUAL_ITEMS` に4項目を固定 (ポインタ/VTK ジェスチャ、
可読性・JA 文言、初回 discoverability、視覚密度)。全バンドルに
原文で埋め込み、`review_state` は常に `manual_review_remaining`。

## デバイス限定 (残る人間ゲート)

- 実 DPI/スケーリングでの描画品質・HDR/色深度・実 GPU ドライバ差
- 実ポインタ操作の感触、フォーカス可視リングの視認性
- offscreen fixture (本箱のテストレーン) は収録経路の自己検証のみ —
  owned-Windows 受入証跡ではない。実機走行は別途の物理ゲート。
