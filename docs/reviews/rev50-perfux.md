# REV50-PERFUX — 計算負荷・高速化・デザイン/UX スイープ

Scope: 全トラック共通規約に従い、計算負荷 (N回DB問合せ・反復ロード・
キャッシュ可能な計算・eager work) / UI スレッド / デザイン崩れ /
ユーザビリティ細部 / 精度 (単位一貫性) をコード検証でレビュー。
推測は使わず、各件は実際の呼出し頻度を追って確認した。

計測: `sqlite3.connect` の monkeypatch + `set_trace_callback` で
発行ステートメント数とコネクション数を記録 (時間は同一 DB・同データ
(50 evaluations / 2 pareto sets / 7 runner plans / 2 speakers ×
binding+context / 8 joint specs) の best-of-N)。

## 実装した改善 (HIGH/MED)

### Backend 計算負荷

| # | 対象 | BEFORE | AFTER | 内容 |
| --- | --- | --- | --- | --- |
| P1 | `OptimizationWorkflowWorkspace._refresh_journey` の評価件数 | `len(list_evaluations)` 489ms / 203 stmts / 51 conns | `count_evaluations` 2.9ms / 2 / 1 | 表示カウンタ専用の `COUNT(*)` (権威 replay を skip)。`count_candidates` の docstring 規約踏襲 |
| P2 | 同 pareto set 件数 | `len(list_pareto_sets)` 170ms / 72 / 23 | `count_pareto_sets` 5.9ms / 2 / 1 | 同上 |
| P3 | 同 validation campaign / model validation 件数 | `len(list_for_search_spec)` / `len(inspect_for_search_spec)` (全件デシリアライズ+再 attest) | `count_for_search_spec` | 同上 (campaign は payload decode を skip、validation は evidence re-attestation ごと skip) |
| P4 | `JointOptimizationPanel._refresh_saved_specs` (saved specs × staleness) | spec 8 件で `resolve_baseline` 8 回 = 3904ms / 1720 stmts / 440 conns | baseline 1 回解決 + 使い回し = 263ms / 215 / 55 (~15×) | `assess_spec_staleness(spec_id, *, baseline=)` — `_UNSET_BASELINE` sentinel で従来の lazy 単発呼出しは不変 |
| P5 | `MeasurementWorkflow.runner_progress` | get_run+get_plan+list_events+状態再計算 = 22ms / 12 / 5 (workflow 側で fail-closed 検査を複写) | `progress_for_run` 10ms / 7 / 3 | リポジトリが 1 回の read で plan+cell states を返す。'runner event binds an unplanned cell' 等の fail-closed 検査はリポジトリ側に集約 |
| P6 | `InterventionPlannerPanel._refresh_install_summary` / `InstallationRecordSurfaces.reload` | speaker 毎に `latest_binding_for_current_entity` + `get_context_for_entity` (entity N×2 クエリ。2 台で 32ms / 22 / 10) | `latest_bindings_for_document` + `latest_contexts_for_document` の doc 単位バッチ読み (2 台で 28ms / 17 / 8、規模に比例せず一定) | `ORDER BY seq DESC` 一発 + 先勝ち。binding 側は head/definition decode を共有キャッシュ |
| P7 | `runner_repository.list_plans` / `list_plan_created_at_utc` | 全行 payload decode → Python で document_id 絞込 | `WHERE json_extract(payload_json,'$.document_id')=?` (created_at は payload 列自体を読まない) | 小 DB では stmts 数は同等だが他文書 payload の decode がゼロに (規模比例の節約) |

### デザイン崩れ

| # | 対象 | 内容 |
| --- | --- | --- |
| D1 | `音場エクスプローラー…` ほぼ不可視 (既報) | `ui_theme.py` `QPushButton:disabled, QToolButton:disabled` の `border: transparent` → `border-color: separator`。disabled 面 `#181E25` と背景 `#141A21` が近接し輪郭が消えていた。`build_dark_stylesheet()` に border 非 transparent の回帰テスト |
| D2 | `GeometryImportDialog` 1130px > 768px | `resize(680,720)` だけでは 4 group box が潰れ下部+OK が見えない。コンテンツを `QScrollArea`(NoFrame) へ、OK/キャンセルはスクロール外にピン留め |
| D3 | `ProjectorSpecDialog` 738px (chrome 含めると 768 溢れ) | 同様に form 部分をスクロール化。sizeHint 738→336px |

### 精度 / 単位一貫性

| # | 対象 | 内容 |
| --- | --- | --- |
| U1 | `room_video_panel` の m 固定表示 16 フィールド | screen/display 画域・オフセット・フレーム余白・視線クリアランス + seat 行 (眼/頭 Z・頭半径) + `DisplaySpecDialog` 5 フィールドを `MetricSpinBox` へ (SI m が権威、表示単位は #496 ポリシー追従)。`bind_length_policy_widget(workspace.video_panel, …)` を `_make_room` に配線、`DisplaySpecDialog(..., length_policy=panel.length_policy())`。seat 行は署名 rebuild 時に保持ポリシーを適用。度の `max_axis_deviation` と無次元の throw/shift は据え置き |

### 回帰テスト

`backend/tests/test_rev50_perfux.py` (12 tests): count==list 長
(evaluations/pareto/campaign/validation)、supplied baseline で
resolve_baseline 0 回、batch reads == per-entity reads (binding/context、
latest 勝ち含む)、`progress_for_run` == 個別読み、plan 一覧の
document スコープ、disabled border、video panel ポリシー伝播
(mm 表示でも `value_m` が SI 権威)、dialog スクロール。

## LOW / 判断事項

| # | 項目 | 判断 |
| --- | --- | --- |
| L1 | `GeometryImportDialog.__init__` で `import_raw_visual_mesh`+diagnose+health を eager 実行 | メッシュ parse はファイル選択後 1 回のみ・bounded (`read_file_bounded`)。worker 化は UX 変更 (進行表示が要る) — 現状は待機として許容。ファイルが巨大な時だけ体感遅延 |
| L2 | `MeasurementPageWorkspace._refresh_journey` の `list_measurement_plans` | 版連鎖検証 (最新版のみ数える) セマンティクスがあり `COUNT(*)` に置き換えられない。一覧は plan 数規模 (通常 <数十) で実害なし — 据え置き |
| L3 | `list_plans`/`list_plan_created_at_utc` の SQL 絞込差は小規模では僅少 (5.3→3.1ms / 4.2→3.1ms) | 正しさ (他文書 payload を読まない) の観点で採用。大規模で効く |
| L4 | 残りの登録ダイアログ (`ScreenTransferDialog` 502px、`DisplaySpecDialog` 605px) | 768px 内に収まるためスクロール未適用。OK ボタンの `setDefault` は QDialogButtonBox の規定動作に任せた (別 round の UX 一貫作業で扱う候補) |
| L5 | `RoomVideoPanel` seat 行の `_SpinRow` は他の単位系 (度) を取り得ない | 現在全行が metre。度系の行を増やすなら `_SpinRow` に unit パラメータを追加する拡張点として残す |

## 補足

- 旧 `runner_progress` の workflow 内 fail-closed 検査 (`plan missing`,
  `event binds an unplanned cell`) は `progress_for_run` 内へ移し、
  workflow は `RunnerError` → `MeasurementWorkflowError` に wrap するだけに。
  呼出し側の意味論は不変。
- `count_*` メソッドは全て `count_candidates` 由来の「Metadata only —
  表示カウンタ専用、payload の主張には使わない」契約を docstring に明記。
