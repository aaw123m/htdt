# サポート・診断・書き出し面

> 2026-10-01 / 実装済みのサポート・エクスポート面の利用ガイド。
> 入口はすべて workflow shell の「サポート」workspaceとコマンドパレット（Ctrl+K）です。

## 1. サポート workspace

「サポート」ページには診断ログの保存先・バージョン情報・パッケージ書き出しが集約されています。
起動に失敗した場合は、ここに表示される診断ログディレクトリ（既定では user data root 配下の `diagnostics/`）をサポートへ共有してください。

### ソルバー出力の診断

「ソルバー出力の診断」ボタンは、現在のdocumentのsolver出力ledger（`SolverOutputLedger`）を読み取り専用で表示します。
リビジョンごとに bound/unbound の solver artifact（生成元・capability・observable・provenance）を確認でき、
解決済みの行から権威グラフ inspector を直接開けます。診断内容は開くたびに再構築され、永続化しません。

### 診断パッケージをエクスポート

「診断パッケージをエクスポート」ボタンは、サポート共有用のZIPアーカイブ（`DiagnosticPackageBuilder`）を作成します。
health check結果と失敗したoperationの履歴を含みます。プロジェクト識別情報は設定 `diagnostics.include_project_ids`
を有効にした場合のみアーカイブへ含まれます（既定は含めません）。

## 2. ハンドオフ・書き出しコマンド（コマンドパレット）

いずれも読み取り専用で、既存データを変更しません。

| コマンド | 表示名 | 内容 |
|---|---|---|
| `installation.export_handoff` | 設置ハンドオフを書き出す | SceneRevisionとSystemVariantを選択→プレビュー→寸法図CSV・設定CSV・座標CSV・HTMLレポート・manifestを選んだディレクトリへ書き出し |
| `analysis.export_bundle` | 解析データをエクスポートする | 測定・予測・比較の解析bundleを書き出し |
| `equipment.export_capture_catalog` | Capture用機材カタログを書き出す | capture取込用の機材カタログスナップショットを書き出し |
| `calibration.export_settings` | 校正設定（汎用バイクアッド）を書き出す | AVR/PEQ向けの汎用biquad校正設定を書き出し |
| `project.deliverables` | プロジェクト デリバラブルセンター | 生成物の一覧と書き出しセンターを開く |

## 3. アプリ内ヘルプ

- `F1`: ショートカット一覧
- `Ctrl+K` → 「ヘルプ」/「使い方」: トピック・用語集の検索（`HelpRegistry`）
- パレットで無効なコマンドを選択して `Enter`: その操作が使えない理由を説明するヘルプトピックを表示
  （理由コード → `availability_reasons.AVAILABILITY_REASONS` の `help_topic_id` → トピック）
