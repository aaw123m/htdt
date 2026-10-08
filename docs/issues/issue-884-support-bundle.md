# Issue #884 — プライバシー安全な診断/サポートバンドル (REV67)

フィールド障害のサポート問い合わせ用に「ワンアクションで生成できる
決定的・プライバシー分類済みの診断バンドル」。#604 の
`DiagnosticPackageBuilder` を拡張し、フィールド単位の分類・書込時
レダクション・完全プレビュー・整合性マニフェストを追加した。

## 受入基準との対応

- **ワンアクションで生成**: サポートページ「診断パッケージを
  エクスポート」→ プレビュー → 保存先選択 → バンドル。既定値だけで
  有用な内容になる。
- **決定的**: 同一状態で同一メンバ集合・同一 sha256。ZIP エントリの
  タイムスタンプは固定 (1980-01-01) でアーカイブのメンバは完全に
  再現可能。`manifest.json` の `created_at` のみ実行時刻を含む。
- **秘密情報は絶対に入らない**: 書込時レダクション
  (`redact_support_value`) が **全メンバに** 適用される — コレクタの
  バグや将来のカテゴリでも漏れない。辞書キーが秘密名
  (password/token/api_key/credential/private_key/authorization) の
  値は丸ごと置換される。バンドル対象ディレクトリを読むだけで、
  プロジェクト DB・測定資産・キャプチャデータを**走査しない**
  構造も不変。
- **機微データは既定で除外**: `PACKAGE_EXCLUSIONS` (生測定音声、
  部屋ジオメトリ、シリアル番号、ユーザーメモ、プロジェクト
  バンドル、資格情報) に加え、絶対パス・IP/ホスト名・顧客/個人名は
  書込時に `<redacted>`/`<path>`/`<ip>` 化される。
- **プレビューは実際のエクスポートと一致**: `preview()` と `build()`
  が同一の `_stage()` (収集→レダクション→予算処理) を共有するため、
  プレビューの各メンバ sha256 がエクスポート後のアーカイブと一致
  することをテストで検証。
- **レダクションテスト**: パス/IP/ホスト/名前/トークン/プロジェクト
  ペイロードを個別に検証 (27 テスト)。
- **整合性マニフェスト**: 各メンバに `sha256`/`classification`/
  `redactions`、マニフェストに `integrity`/`excluded_categories`/
  `collection_errors`/`bundle_format` (schema_version=2)。
- **バンドル生成自体がクラッシュしない**: 各コンテキストプロバイダを
  try/except で隔離し、失敗は `collection_errors` に記録するのみ。

## 分類語彙 (`FieldClassification`)

| 分類 | 意味 | 扱い |
|---|---|---|
| `SAFE_DIAGNOSTIC` | 診断に必要・機微でない | 既定で含める |
| `PROJECT_METADATA` | プロジェクト ID/表示名 | 明示的オプトインのみ |
| `SENSITIVE_PROJECT_DATA` | 部屋ジオメトリ等 | 構造上バンドル対象外 (オペレータ添付のみ) |
| `SECRET_NEVER_EXPORT` | 資格情報・秘密鍵 | 絶対に収集しない (元帳上の宣言) |

## 新規コンテキストカテゴリ (`support_bundle_collectors.py`)

収集できない場合は `status: unprobed` + 理由 — 偽の成功を報告しない。

- `runtime_context` — OS build/locale/表示スケーリング/Python +
  依存ピン (PySide6/numpy/scipy/pydantic/vtk/sounddevice/pyyaml)
- `gpu_context` — VTK モジュール存在/Qt プラットフォーム/Mesa ヒント。
  GL コンテキストは診断対象の障害源になり得るため **故意に作らない**
  (`renderer: unprobed`)
- `audio_context` — オーディオバックエンド ID + 利用可否 (WASAPI スタブは
  `backend_unavailable`、Fake は `simulated: true` を明示)
- `workflow_state` — 現在のワークスペース/ワークフロー段階 (シェルから注入)
- `release_evidence` — リリース検証レポートの sha256 + verdict + サイズ
  (ペイロードではなく識別情報)
- `measurement_engine_status` / `device_transaction_status` —
  エンジン状態/トランザクション ID+状態+readback (ペイロードなし、
  シェル注入)

## プレビューダイアログ (`support_bundle_dialog.py`)

保存先選択 **より前に** 表示: メンバ名・分類 (JA 表示)・サイズ・
状態/秘匿箇所数の一覧、除外カテゴリ、収集エラー、「プロジェクト ID
を含める」チェック (トグルで再ステージ = プレビューと実出力が常に一致)、
エクスポート/キャンセル。

## 変更ファイル

- `backend/src/htdt/support_diagnostics.py` — `FieldClassification`,
  `CATEGORY_CLASSIFICATION`, `redact_support_value`, `BundlePreview`,
  `BundleMemberPreview`, `_stage()`/`preview()` 共有パス、決定的 ZIP,
  manifest v2 (`integrity`/`classification`/`redactions`/
  `excluded_categories`/`collection_errors`)、`context_providers`
- `backend/src/htdt/support_bundle_collectors.py` — 新規
- `backend/src/htdt/support_bundle_dialog.py` — 新規プレビュー UI
- `backend/src/htdt/workflow_application.py` — エクスポート経路を
  プレビューファーストに再配線
- `backend/tests/test_issue_884_support_bundle.py` — 27 テスト
- `backend/tests/test_support_diagnostics.py` — 秘密値プレースホルダを
  `<redacted>` に更新 (より強い表記)

## 境界 (非目標)

- リモート送信/自動アップロードは行わない — バンドルはローカル
  アーカイブであり送信はオペレータの判断。
- `SENSITIVE_PROJECT_DATA` の明示添付 UI は将来の issue — 現在は構造的に
  バンドル対象外。
- ログのローテーション歴は既存 `htdt-native.log*` 範囲のみ。
