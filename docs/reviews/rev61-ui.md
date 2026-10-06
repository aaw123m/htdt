# REV61-UI — UI スライス・ディープレビュー

Scope: Qt サーフェス全般 (a11y / ユーザ向けエラー文言 / 未配線サーフェス /
ダイアログ幾何 / Qt スレッド / N+1)。手法はコード検証 + offscreen プローブ
(全 `*Panel`/`*Dialog`/`Workspace` クラスのインスタンス化走査、sizeHint
計測、無名ウィジェット列挙)。推測で挙げた項目は全て実コード・実挙動で
確認してから修正した。

## 実装した改善

### ユーザ向けエラーへの生例外混入 (raw exception leak)

`operation_error_message()` (REV49 導入済み) を通さず `str(exc)` /
`{exc}` / `{type(exc).__name__}: {exc}` をオペレータ向け表示にそのまま
流していた箇所を是正。英語の `[Errno 2] ...`・pydantic ノイズ・例外
クラス名が JA UI / 証跡に混入する既定違反。生テキストは
evidence/diagnostic フィールド側に残る設計は維持した。

| # | 対象 | 内容 |
| --- | --- | --- |
| E1 | `installation_panel._save_context` (context エラーラベル) | `str(exc)` → `operation_error_message(exc)`。同ファイルの `_context_selected` は既に同関数を使用していた抜け漏れ |
| E2 | `installation_panel._save_datum` (datum エラーラベル) | 同上 |
| E3 | `room_acoustics_panel._pick_source_file` (QMessageBox) | `出典ファイルを読み込めませんでした: {exc}` → マップ済み文言 |
| E4 | `reflection_guidance_ui.refresh` (summary ラベル) | `ガイダンスの読み込みに失敗しました: {exc}` → 同上 |
| E5 | `reflection_guidance_ui._run_scrub` (scrub 結果ラベル) | `スクラブを計算できませんでした: {exc}` → 同上 |
| E6 | `verification_wizard_page._on_manifest_failed` (verdict 理由ラベル) | `{path}: {type(exc).__name__}: {exc}` → `{path}: {mapped}` |
| E7 | `verification_wizard_page._commit_manual_evidence` (QMessageBox) | `{path}: {exc}` → `{path}: {mapped}` |
| E8 | `acceptance_checks._preflight_or_unavailable` (`detail_ja`) | REW preflight 例外の生テキスト → マップ済み。`detail_ja` は `acceptance_page.step_result` ラベルに直接描画されるユーザ向けフィールド |
| E9 | `acceptance_checks.run_auto_check` fail-closed ラッパー (`detail_ja`) | `チェック実行中にエラー: {type(exc).__name__}: {exc}` → マップ済み。`evidence['error']` には生の型名+テキストを残す (診断用) |

### 表示の正直さ

| # | 対象 | 内容 |
| --- | --- | --- |
| V1 | `reflection_guidance_ui.refresh` の失敗表示が死んでいた | except で設定した失敗文言を、続く `_refresh_entries()` が `ガイダンス 0 件（0 組）` で無条件上書き → 読み込み失敗が「正常な空」として表示されていた。`load_error` を遅延適用しエントリ数表示の後に書き戻す |

### アクセシビリティ

| # | 対象 | 内容 |
| --- | --- | --- |
| A1 | `measurement_page_workspace` `registration_table` | `setAccessibleName('予測↔実測の登録レコード一覧')`。
  mounted-destinations スイープ (`test_accessible_labels`) が
  HEAD で赤だった唯一の無名コントロール |

### 回帰テスト

`backend/tests/test_rev61_ui.py` (9 tests): registration_table の
accessibleName、installation_panel の context/datum エラーマッピング、
TreatmentDefinitionDialog の出典ファイル失敗、`refresh()` 失敗時の
文言可視性+マッピング、`run_auto_check`/`_preflight_or_unavailable` の
detail_ja マッピング (evidence 側に生診断を残すことも検証)、ウィザード
manifest 失敗・証跡ファイル失敗のマッピング。

## 監査したが問題なし (checked-clean)

- `CommandContext` ↔ `WorkspaceId` パリティ (REV49 の Ctrl+K ValueError
  クラス): 全 workspace をカバー、app 宛先は GLOBAL フォールバック。
- 未配線サーフェス: 82 UI クラス全走査、残った 4 件は全て偽陽性
  (クラスメソッドファクトリ経由 / pydantic モデル / 共通基底)。
- ダイアログ高さ: 無引数で構築可能な全ダイアログの sizeHint 計測で
  560px 超なし (REV49/50 のスクロール化対象は既存テストが担保)。
- cp1252 露出: UI 系 `read_text` は全て encoding= 指定済み。
- QThread ワーカー: 12 モジュール全て emit のみ・ウィジェット直触りなし。
- N+1: リスト再構築のヒューリスティック走査 — 実質ヒットは
  `room_acoustics_panel` の配置ごと evaluate+get_definition のみで、
  配置数は小さい (≤20 想定) ため報告のみ。

## LOW / 判断事項

- `room_acoustics_panel._refresh_placements` — placement 毎に
  `evaluate_placement_surface_binding` + `get_definition` の 2 クエリ
  (N+1)。材質配置は小規模で、バッチ読み API の新設は本ラウンドの
  最小差分方針を超えるため次ラウンド送り。
- `equipment_library` の `str(exc)` 直接表示 — AUTHORITY スライス
  (別セッション) の管轄として共有済み。
- `_ManifestLoadWorker` 初回マウント ~20s — QThread 上で正しく
  オフロード済み (UI フリーズしない) ので仕様どおり。
