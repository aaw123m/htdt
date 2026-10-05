# REV49-FULLSWEEP — 全体完成度スイープ

Scope: 全リポジトリを「ソフトの完成度」観点で広く深くレビュー —
仕様不整合 (UI表示と実挙動・ドキュメントとコード・権威間の未結線)、
ユーザビリティ (日本語・ツールチップ・エラー品質・デザイン崩れ)、
精度 (丸め・単位・閾値)、計算負荷・高速化 (冗長再計算・N+1・UIブロック)、
追加機能候補 (既存権威間の自然な結線)。

REV42–48 の既検証範囲は除外 (REV42 watch-dir 系、REV43 seams、
REV44-STAGED 未配線ファミリー、REV45/46 issue-verification、REV47 #2/#533/
#534/#538、REV48 受入ウィザード・映像調整 — 後者は REV49-NEWSURFACES 担当)。

Method: (1) 全 ~640 モジュール + docs/ の横断監査 — UI 文字列・例外経路・
DBクエリパターン・権威配線・ドキュメント一致を系統的に走査;
(2) 各候補をコード再読込・サイズヒント実測・既存テスト契約で検証し、
確定したもののみ実装; (3) 回帰テスト `backend/tests/test_rev49_fullsweep.py`
(10 件) + 影響スイートの scoped pytest。

## 実装した改善 (重要度順)

| # | 改善 | 重要度 | 内容 |
|---|------|--------|------|
| 1 | **Ctrl+K パレットがプレゼン/映像調整ワークスペース上でクラッシュ** — `CommandContext` に PRESENTATION/VIDEO が未登録のまま #534/#541 で WorkspaceId 側にだけ追加されていた。palette の `context_provider` が `CommandContext('presentation')` で ValueError を投げ、当該ワークスペースではパレット自体が開けなかった。さらに `palette_search.py` の `COMMAND_CONTEXT_LABELS` も未更新で同様に KeyError。 | **High** (実害: 機能が2面で完全に死ぬ) | `CommandContext` に 2 member 追加 + `COMMAND_CONTEXT_LABELS` 補完。回帰: `test_command_context_covers_every_workspace` が全 WorkspaceId を網羅 → 将来の workspace 追加で再発しない。COMMAND_SYSTEM.md に両 enum の parity 注意書きを追加。 |
| 2 | **プレゼン/映像調整/受入検証にナビゲーション command が無かった** — rail・メニュー以外の到達経路 (palette・キーボード・検索) が欠落。round-13 で rail destination は全て palette-reachable と定義済みだったのに、後続追加ワークスペース (presentation/video workspace, acceptance destination) が漏れていた仕様不整合。 | **High** (discoverability 欠落) | `navigation.presentation` (プレゼン) / `navigation.video` (映像調整) / `navigation.acceptance` (受入検証) を既存ラベル定数で登録。全て GLOBAL context + WorkspaceDeepLink。 |
| 3 | **3 パネルが display_input.length_unit / numeric_precision を無視** — `cad_display_units` の docstring が部屋 inspector・geometry panel 等でポリシー適用を謳うが、実際に bound だったのは SelectionInspector + measure panel のみ。room_geometry_panel は " m" 固定 suffix、installation_panel の clearance 7 フィールド、field_explorer_panel の stride/probe 全てハードコード。 | **High** (精度・一貫性: 設定が嘘になる) | `length_spinbox.py` を新設 (room_workspace の MetricSpinBox/_PendingTextSpinBox を共有モジュールへ移動 + `display_unit()`/`set_minimum_m`/`set_maximum_m` 追加)。3 パネル全て MetricSpinBox 化し `set_length_policy(policy)` 実装。`workflow_application` に generic `bind_display_length_policy` + `bind_length_policy_widget` を追加し geometry/installation/field-explorer を preferences に bind。SI メートル権威は不変 (display は cosmetic)。**実装中に発見・修正**: `field_explorer` の stride が `value()` (表示単位) を metres として builder へ渡していた — mm ポリシー下で 0.1m が 100m になる精度破損。 |
| 4 | **ユーザー向けラベルに生の例外テキストが漏れる** 17 箇所 — `user_facing_error.py` の契約 (mapped JA `operation_error_message` を表示、生 `{exc}` は不可) に反する surface。英語トレースバック的な文がステータス欄に出ていた。 | **High** (UX・誤診リスク) | installation_record_surfaces(3) / installation_panel(1) / measurement_authority_dialogs(2) / measurement_record_surfaces(7) / optimization_validation_controller(1) / prediction_matrix_dialog(5) / workflow_application のテンプレート保存 QMessageBox(1) を `operation_error_message(exc)` に統一。JA の typed error は pass-through、英語はマッピング済み JA に。 |
| 5 | **HealthCheckDialog の N+1 リフレッシュ** — `reload()` 1 回で `list_baselines` ×4、`list_plans` を baseline ごとに ×2 (内部で `cad_health_baselines` を plan 行ごとに再検証する二重 N+1)、`list_presets` ×2、`get_preset` を baseline ごとに。 | **Medium** (計算負荷) | `CadSystemHealthRepository.list_plans_for_document(document_id)` 追加 — JOIN で baseline 一致を強制 (`_plan_from_row` の fail-closed 保証と同じ契約、query 1 回)。dialog は `_load_catalog()` で baselines/plans/presets を 1 スナップショット取得し全 refresh が共有。reload あたり `2+B+2P` クエリ → 3 クエリ。 |
| 6 | **dialog が画面 (768px) より高い** — AVSyncRecordDialog 1150px、HealthCheckDialog 933px を sizeHint 実測。スクロールなしで最下部のボタンが届かない (REV44 で実機確認済みの問題)。 | **Medium** (到達不能 UI) | `_scroll_wrap()` ヘルパで各タブ/内容を QScrollArea 化 + 初期サイズを 768px 内に収まる `resize()` に。OperatingPresetRecordDialog のタブも同パターンで統一。 |
| 7 | **ヘルプボタンが全エラーを generic トピックへ** — ~40 個の UserFacingError code (storage.*/io.*/rew.*/migration.*/authority.*) が全て `trouble.operation_error` に集約され、原因別の対処へ辿れなかった。 | **Medium** (エラーからの回復導線) | 3 つの family トピック追加: `trouble.rew_errors` (rew.*/import.rew*)、`trouble.storage_errors` (storage.*/io.*/schema.*/migration.*/backup/ingress)、`trouble.authority_errors` (authority.*)。それぞれ JA+EN の原因別対処セクション。umbrella は残りを継続カバー。 |
| 8 | **管理系ボタンにツールチップ 0 件** — measurement_record_surfaces (11 ボタン)、data_management_ui (12)、playback_chain (3)、optimization_validation_controller (2) — 権威記録・復元・移行のような不可逆度の高い操作に説明なし。 | **Medium** (discoverability) | 全 28 ボタンに JA ツールチップ追加 (「何を権威として記録/復元するか」を1文で)。 |
| 9 | **COMMAND_SYSTEM.md の drift** — workspace 一覧が `overview/room/measurement/optimization` のみ (presentation/video 欠落、acceptance destination 未記載)、「初期command」表が登録 78 件中 16 件のみで網羅表に見える表記。 | **Medium** (ドキュメント=正本の乖離) | workspace + application destination 一覧を現行に更新、表を「初期セットの例」と明記し palette/registry を正本に誘導、WorkspaceId↔CommandContext parity の落とし穴注意を追加。 |
| 10 | **9 コントロールに accessibleName 欠落** — `test_mounted_destinations_have_no_unnamed_controls` が HEAD 時点で既に赤 (video workspace 導入で回帰した既存 rot): InstallationPanel の実測クリアランス spin 6 件 + videoProposalsList/videoActionsTable/videoDeltasTable の 3 件。スクリーンリーダー利用者に内容が一切届かない。 | **Medium** (a11y) | 各 widget に役割名を設定: `実測クリアランス <軸名>` / `必要クリアランス` / `調整提案の一覧` / `推奨される調整操作の一覧` / `調整前後の指標差分表`。video 側は sibling surface だが 3 行の追記のみ (他変更と衝突最小)。 |
| 11 | **MetricSpinBox 重複実装の構造整理** — inspector 系にだけ存在した MetricSpinBox を共有モジュール化。`room_workspace` は re-export で後方互換 (既存テストの import 経路を維持)。bind_inspector/bind_measure の 2 系統のほぼ同一 weakref subscriber を generic binder へ統合。 | **Medium** (重複解消 + H3 の前提) | `length_spinbox.py` (190 行)。room_workspace から ~140 行削減。 |

## 判断事項・除外 (報告のみ)

- **presentation_workspace.py の生 `{exc}` 8 箇所** — 同じ leak 系だが REV49-NEWSURFACES (sibling session) の PR#547 対象 surface のため本スコープでは未修正。 sibling へ伝達推奨。なお video workspace の a11y 命名 3 件は sibling surface だが HEAD でテストが赤だったため最小追記で実装済み (#10)。
- **`required_spin` (上書き基準) の accessibleName** — 動的行のため mount 時点では未検出だが #10 で併せて命名済み。
- **cad_* domain 層の `:.2f m`/`.3f` 直書き** — 検証 detail ・ constraint label 等の engineering-layer メッセージ。display policy は「ユーザー入力フィールド」向けで、domain 層の内部表現は対象外と判断 (権威テキストは SI 固定が正しい)。
- **HealthCheckDialog の `list_runs` per-plan 呼び出し** (`_build_run_rows`) — plan ごとの run 一覧は読み取り専用ビューのため据え置き (runs は plan 軸で必要なので N+1 ではない)。
- **verify_persisted_* の検証ループ** (cad_system_health_repository) — verify-only 経路であり UI スレッドのホットパスではない。REV44 で既承認。
- **`installation_panel` の metric-spinbox 化に伴う validation 範囲** — `set_minimum_m(0.0)` は「負の clearance 禁止」を引き継ぐ (旧 `setMinimum(0)` と同値)。`_spin_value_changed` の tolerance は display unit 変換済み (旧 0.5ulp-of-m 固定よりむしろ正確)。
- **追加機能候補 (設計判断として記録)**: palette の command search に help topic を混ぜる (`trouble.*` を reason-code から直接 deep-link) — エラー通知の ヘルプ ボタンで既に到達可能だが、palette 検索経路は未実装。外部権威不要・配線可能だが検索結果の ranking 設計が要るため今回は記録のみ。
- **`navigation.acceptance` 以外の app destinations** — projects/inbox/activity/library/support は round-13 で既に実装済み (重複指摘しない)。

## 検証

- `backend/tests/test_rev49_fullsweep.py` — 10 tests: CommandContext 全 workspace 網羅 (crash 回帰)、新 nav command の deep-link/ラベル/実行、`list_plans_for_document` が per-baseline 列挙と一致 + fail-closed JOIN、MetricSpinBox の unit 切替・min/max SI 変換、3 パネルの policy 伝播、help topic family バインド。
- 影響 scoped pytest: command_registry / palette_search / help_registry / cad_system_health / rev44 系 surfaces (healthsync/staged/install/authority/surfaces) / field_explorer / geomport / room_workspace / room_inspector / room_cadux / prefs / data_management / accessible_labels / workflow_application 等 42 ファイル。

## 実機 GUI 検証 (Windows 実 display, 1024×768) — 全項目 PASS

- Ctrl+K パレットが全 6 ワークスペース (概要/部屋/測定/最適化/プレゼン/映像調整) で開くことを確認 — 旧 ValueError/KeyError サイト両方修復。
- 「プレゼン」「映像調整」「受入検証」パレット検索→実行で各 surface へ遷移。
- 長さ表示ポリシー: pref=mm で geometry 天井高・installation 実測クリアランス・field-explorer グリッド間隔/プローブが mm 表示、m へ切替で開いたままの dialog も live 変換 (0.10 m ⇔ 100.00 mm)、SI 権威不変。
- stride 精度修正を機能実証: 0.10 m ストライドで 41×51 サンプル生成 — mm ポリシー下でも正しい (旧バグなら ~1 サンプルに壊れていた)。
- AV同期/健全性チェック/プリセット記録 dialog: 768px 高さでスクロールし最下部ボタンへ到達可能。
- 管理ボタンの JA ツールチップ確認、全 touch パネルにデザイン崩れなし。

## 残タスク・フォローアップ

- **`音場エクスプローラー…` ボタンがダークテーマでほぼ不可視** — 実機検証で発見。存在してクリックは可能だがコントラストが低すぎて空白に見える。styling 追従として別途修正候補 (LOW: 機能は動作、視認性のみ)。
- 検証時に GPU-less box で Mesa DLL 欠落による起動クラッシュを経験 — blueprint initialize の Mesa 配置ステップを手動復元で解決。環境依存でコード不具合ではない。
