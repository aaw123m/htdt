# REV51-PASS3 — REV50 変更の third-pass レビュー

Scope: REV50-SECOND が REV49 で 2 件の導入欠陥を見つけたパターンの継続 —
REV50 でマージされた PR #554 (4bfb108b REV50-SECOND 修正) / PR #555
(06abdcfe REV50-PERFUX) / PR #556 (375e6c1e 配置列オーバーフロー) /
PR #557 (49979542 配置列の深掘り) + 9125ae74 (daafc74e warn_user 化) を
批判的に精読し、各変更が新たな欠陥・副作用を生んでいないかを検証。

## 結論

**修正した欠陥: 0 件** — 8 検証領域すべて読み合わせ + オフスクリーン
実レイアウト検証でクリーンを確認。scoped pytest 279 件グリーン。

## A. 検証済み・問題なし

### 1. count_* メタデータカウンタ / batch latest-reads (#555)

「最新」の定義 (REV49 で rowid 化した経緯) が正確に継承されている。

- `cad_equipment_binding_repository.latest_bindings_for_document`:
  `ORDER BY seq DESC` + first-per-entity で、
  `latest_binding_for_current_entity` の `ORDER BY seq DESC LIMIT 1`
  と同一行を選択 (seq は `INTEGER PRIMARY KEY AUTOINCREMENT` = rowid)。
  `_HEAD_UNSET` センチネルで未指定 head と caller の None を区別。
  `_decode_binding_row` は共有 head + `_definition_cache` を用い、
  単体パスと同一の「head に残存すれば speaker でなければならない」
  fail-closed 検査を保持。
- `cad_installation_context_repository.latest_contexts_for_document`:
  同構造 — newest-wins 等価。読出し時の `_check_context` は
  単体・バッチどちらにも無し (契約不変)。
- `cad_measurement_runner_repository.list_plans` /
  `list_plan_created_at_utc`: `json_extract(payload_json,'$.document_id')=?`
  を SQL へ押下げ — NULL document_id は両経路で等しく除外
  (list_plans は Python 側の再フィルタも残存、冗長だが一致)。
- `progress_for_run`: `cell_states`/`_plan_for_run` と同一チェック
  (run 永続済・plan_sha256 一致・cell_index < len(plan.cells)) を保持。
  `measurement_workflow.runner_progress` は RunnerError を
  MeasurementWorkflowError (JA) にラップ — 呼出し側
  (measurement_page_workspace) は RunnerError を catch しておらず
  型変更の影響なし (どちらも未捕捉で伝播するだけ、JA 化は改善)。
- `cad_objective_repository.count_evaluations` /
  `cad_model_validation_repository.count_pareto_sets` /
  `cad_validation_campaign_repository.count_for_search_spec`:
  すべて `COUNT(*) WHERE search_spec_id=?` で、対応する list
  メソッドと字句的に同一の WHERE 述語 (JOIN 無し、NULL セマンティクス
  同一)。ペイロード破損行もカウントするが docstring が metadata-only
  と明記 — 意図通り。
- `installation_record_surfaces.py` のバッチ化は同一行選択 +
  同一 fail-closed 検査であり、かつバッチのほうが並行書込みに対し
  単一スナップショットでより一貫。

検証方法: `git show 49979542:<file>` で各リポジトリを行単位読み合わせ
+ `backend/tests/test_rev50_perfux.py` グリーン (counters/batch reads
/runner scoping の回帰テスト群)。

### 2. QScrollArea 化 (#555)

- `GeometryImportDialog`: ヘッダ QLabel はスクロール外に固定、
  4 グループボックスがスクロールコンテンツ、`QDialogButtonBox` は
  スクロール後に固定配置で `accepted→_accept`/`rejected→reject` の
  配線は不変 (`_accept` は検証経路を経て `self.accept()`)。
- `ProjectorSpecDialog` 他 room_video_panel の 3 ダイアログ: 同パターン
  (`_scroll_wrap` 相当 — `setWidgetResizable(True)` + `NoFrame`)。
- Tab フォーカス: QScrollArea は子へフォーカスを素通し (Qt 標準) —
  フォーカス遷移の変更無し。minimumSize はスクロールが吸収、
  ダイアログの resize(680,720) は据置き。

検証方法: ctor 全体の読み合わせ + `test_tall_dialogs_scroll_instead_of_overflowing`
(scroll 子の存在 + sizeHint < 700) グリーン。

### 3. パネル最小幅縮小 (#556/#557)

- 列スイープ (RoomWorkspace ctor): 全 combo に `minimumContentsLength(6)`
  + `AdjustToMinimumContentsLengthWithIcon` + `setMinimumWidth(72)` +
  `QSizePolicy.Ignored`、全 `QAbstractSpinBox`/`QLineEdit` に
  `72px floor + Ignored`、全 `QFormLayout` に `WrapLongRows`。
  `findChildren` は placement_body 配下を再帰的に網羅。
- 実行時生成ウィジェットは生成箇所で同一ポリシーを反復:
  `_OverrideRow` (axis/required/rationale)、seat card の
  `row_id`/`riser_combo`/`pose_combo`、`_ProposalSpeakerRow` の
  combo/zone_name、既存スピーカー行の equipment combo。
  seat_priority の `member_tree` role_combo は QTreeWidget の
  item widget — ツリー自身のスクロールに委譲 (対象外で正しい)。
- レイアウト構造化: `_SpinRow`・`_field_pair` は縦積み、
  制約追加 4 ボタンは 2×2 グリッド、実測クリアランスは 1 列、
  削除→詳細と controls 行化、ライブラリ/再生ボタン縦積み、
  `display_heading`/`member heading` の wordWrap — すべて
  縦方向の高さ増大を `placement_panel` (QScrollArea,
  `widgetResizable`, `ScrollBarAsNeeded`) が吸収する設計。
- `QSizePolicy.Ignored` は拡大を阻害しない — sizeHint 無視 =
  レイアウトの配分をそのまま受ける。72px floor が下限。
- splitter 相互作用: `right_stack` の実効最小幅 = 最大パネル最小幅
  (≤280) — splitter はその幅まで圧縮・再拡大可能。構造上の阻害無し。

検証方法: **オフスクリーン実レイアウト検証** (新規 —
`C:\t\rev51\check_reexpand.py`)。6 パネルを `placement_panel` と同一
設定 (widgetResizable + ScrollBarAsNeeded + NoFrame) の QScrollArea に
マウントし 600→283→600px でリサイズ: 全パネル narrow≈269-283px で
横スクロールバー非発生、再拡大で 600px に復帰 (wide=586-600 /
narrow=269-283 / rewide=586-600)。`check_drivers.py` で 200px 超の
最小寸法ドライバを列挙 — video=192 / installation=240 / その他 ≤240 で
ドキュメント記載の計測値と一致。併せて回帰テスト
`test_placement_column_content_stays_inside_right_stack`
(6 パネル sweep+show+processEvents → minSizeHint ≤280) グリーン。

### 4. MetricSpinBox の映像パネル導入 (#555)

- パネル内の 11 スピン + seat の `eye_z`/`head_z`/`head_r` `_SpinRow`
  は全て MetricSpinBox で、全 read/write が `value_m()`/`set_value_m()`
  (SI メートル権威 `_exact_m`)。`max_axis_deviation` (deg) 等の
  非長さ単位は QDoubleSpinBox のまま — cd/m² 等を長さ変換する
  誤り無し。
- `set_display_unit` は `_exact_sync_blocked` で `_exact_m` を保持
  → 表示単位切替で SI 値が不変。`bindingsChanged` 経由の
  `save_video_workspace` は document_id キーの UPSERT — 同一 SI 内容の
  再保存は `updated_at_utc` を動かすのみで無害。
- `sync_document` は `_syncing` ガード (try/finally) で bindingsChanged
  発火を抑止 — `_video_bindings_changed` は `_syncing` で早期 return。
- `setSingleStep` の外部呼出は `stepBy` オーバーライドが
  `_base_step*factor` を復元するため無害な no-op (dead だが副作用無し)。
- seat pose 保存/読込は `room_workspace` 側でも `value_m()` 使用
  (6050-6222)。`spacing_field`/`row_spacing_field`/`anchor_*_field` は
  従来通り QDoubleSpinBox ' m' (変更なし・想定内)。

検証方法: 全 `set_value`/`value`/`setSingleStep`/`setMaximum` 呼出を
`git show` で grep 網羅 + `bindingsChanged` 発火点とガードの読み合わせ。

### 5. warn_user 化 (9125ae74)

- `warn_user(parent, title_ja, exc)` はクラス名 leaf サフィックスで
  マップ: 'ConflictError' → ('authority.conflict', JA メッセージ,
  再試行案内)。`PresentationConflictError` は確実にマッピング経路を通る
  (raw `{exc}` 直出しではなく Details 展開子に技術文面を保持)。
- `presentation_workspace` の 8 呼出しは全て `warn_user(self, <JA文脈>, exc)`
  の正しいシグネチャ。

検証方法: `user_facing_error._SUFFIX_PATTERNS` の突合 +
全呼出し箇所の grep 網羅。

### 6. 受入検証 `_apply_check_result` 抽出 (#554)

drop/commit 全分岐:

| 状況 | 戻り値 | commit |
|---|---|---|
| step が pending でない | `None` (stale drop) | 無し — `_select_row` のみ |
| step_id 不一致 (全ループ終了) | `None` | 無し |
| capture_only または kind != 'auto' | check_detail のみ pending 維持 | 有り |
| auto + pass | passed + `verdict_source=auto_check` | 有り |
| auto + deferred/unavailable | pending + note (blocked にしない) | 有り |
| auto + fail/その他 | failed + `verdict_source=auto_check` | 有り |

- `_on_check_result` は `self._run.run_id != run_id` (実行切替) または
  `latest is None` で完全スキップ — 他実行への混入無し。
- 結果適用は `repository.latest(run_id)` の新鮮な steps に対し行い
  ローカル `self._run` 経由ではない — stale 上書き防止がリポジトリ先端基準。
- attest/guided-manual/human_confirm は `_mutate_current` 経路 (同期 UI
  アクション) で不変 — 抽出は auto-check worker 結果経路のみに閉じる。
- drop 時に commit しないため、REV50-SECOND が治した phantom journal
  revision が再発しない。

検証方法: 関数の全 return 経路の行単位読み合わせ + `test_rev50_second.py`
(dropped results の回帰) グリーン。

### 7. boot_id バインド (#554)

- `_PROCESS_BOOT_ID = secrets.token_hex(8)` はモジュール読込時に必ず
  生成される定数 — `ctx.boot_id or _PROCESS_BOOT_ID` で取得失敗経路は
  存在しない (empty/None → プロセス値へフォールバック)。
- 永続化 probe の遷移: marker 無し/非 dict → 新規発行 (deferred)、
  dict でも `boot_id` 欠落 → 同様に再発行 (再起動証明不能のため
  fail ではなく re-issue — 妥当)、`boot_id == boot_id` → deferred
  (同一ブートでは再起動を証明できない)、`!=` → pass。
- 新 probe は `attach_evidence` で追記 — `existing[-1]` は
  `evidence_for` の `ORDER BY seq ASC` 末尾で常に最新を読む。

検証方法: `check_persistence_probe` の全遷移読み合わせ +
`test_rev50_second.py` の回帰。

### 8. 非 dict marker 処理 (#554)

- marker=null/`"str"`/数値/`[array]` → `json.loads` 成功だが
  `isinstance(parsed, dict)` False → `marker=None` → 再発行。
  (dict.get の AttributeError を防ぐのが REV50-SECOND の修正目的 — 全ケース網羅)
- `parsed` が dict で `run_id` 不一致 (未設定含む → `None != run_id`)
  → fail (fail-closed)。`read_verified` が None (アセット破損) → fail。
- 例外系 (`TypeError`/`ValueError` on json.loads) → `parsed=None`
  → dict でない → 再発行。

検証方法: 全 JSON 値型の分岐読み合わせ + `test_rev50_second.py`
(non-dict marker の回帰) グリーン。

## B. 補助確認

- `cad_review_package.derived_yaw_steps(0)`: ZeroDivisionError →
  ValueError 化は早期失敗の明確化で問題なし。
- `field_explorer_panel`: `probe_x` に accessibleName 追加 — Y/Z と parity。
- テストヘルパ `_placement_column_sweep` は production スイープを忠実複製
  (combo: AdjustToContents 例外は撤廃され全 combo 適用に一致)。

## C. scoped pytest ゲート

```
test_rev50_second.py, test_rev50_perfux.py, test_cad_acceptance.py,
test_rev47_iss2_acceptance.py, test_cad_measurement_runner.py  → 59 pass
test_cad_objective_repository.py, test_cad_model_validation_repository.py,
test_cad_validation_campaign.py, test_rev47_presentation.py,
test_geometry_import_dialog.py, test_rev44_install_surfaces.py,
test_cad_field_explorer.py, test_standards_workspace.py,
test_measurement_workspace_composition.py                     → 111 pass
test_room_workspace.py, test_workspace_dirty_state.py,
test_t18_workspace_ux.py, test_cad_video_geometry.py,
test_cad_video_commissioning.py, test_cad_video_domain_authorities.py
                                                              → 109 pass
                                                        計 279 pass / 0 fail
```

## D. 補足 (手続きメモ)

- 検証用スクリプトは `C:\t\rev51\check_reexpand.py` / `check_drivers.py`
  (ローカルのみ・コミット対象外)。`sys.path` が stale working tree を
  指す初回計測で疑似「過大最小幅」が出たため、検証は必ず origin/main
  checkout 上で実行する。
- spec ファイル `prompts/rev51/common.md` は本 box に存在しない
  (親セッションの box のみ) — REV27 ヘッダ規約 + memory 規約で再構成済み。
