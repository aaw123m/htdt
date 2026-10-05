# REV56-CAMPPROFILE — 空間測定キャンペーン設計 + CEDIA RP32 コミッショニングプロファイル

スコープ: issues #581 (P1), #585 (P1)
ブランチ: `devin/1791203572-rev56-campprofile`
スキーマ: native schema v25 → v26（9 テーブル追加。
REV56-SNAPSTD が v25 を先取りしたため本 PR は v26 へリナンバー）

## 実装範囲

### #581 空間測定キャンペーン設計権威

新規 `backend/src/htdt/cad_spatial_campaign.py`。

- `ListeningAreaSpec` + `ListeningZone`: リスニングエリアは凸包推論では
  なく**明示宣言**された `axis_aligned_bounds` ゾーン群
  （kind = seat/row/region/holdout_excluded/challenge_region）で定義。
  `zone_for()` は excluded ゾーンを veto として先に評価し、残りを宣言順
  優先で解決。`aggregate_bounds()` なし宣言ゾーンは area 未定義扱い —
  暗黙の幾何推論を行わない。
- `CampaignPoint`: `position`（宣言）と role 集合。role literal は
  `reference_alignment / optimization / spatial_holdout / repeatability /
  diagnostic / boundary_stress / standards_required`。
  `FORBIDDEN_ROLE_PAIRS` で filter-design 点（optimization）・repeatability・
  diagnostic が spatial_holdout と同居することをモデルレベルで拒否
  （validator）。adaptive 点は `adaptive_algorithm` + `adaptive_reason`
  必須かつ spatial_holdout を絶対に持てない（適応点を holdout に事後
  ラベルできない）。`channel_coverage='scoped'` は
  `channel_entity_ids` 必須。
- `SpatialCampaignDesign`（封印, `design_id=spatial-campaign:<sha256>`,
  ConfigDict frozen+forbid）: area・点集合・`CaptureOrderPlan`
  （strategy literal + declared_sequence + interleaved_pairs +
  randomization_seed）・`SamplingExpectation`（min_axis_span_m /
  min_pairwise_spacing_m / duplicate_tolerance_m / required_zone_ids /
  min_holdout_count / density_weight_alert_ratio — 全て宣言ポリシー。
  None はその検査を無効化する。Dirac の 30 cm 等の数値は権威内部では
  なくテンプレート/期待値に外出し）。
- `compute_coverage_metrics`: 記述メトリクスのみ — 最近傍距離分布
  （min/median/max）、軸別スパン、高さ多様性、ゾーン別点数・
  `declared_weight_share`/`density_implied_share`/
  `density_weight_max_ratio`（重みと実密度の乖離）、未サンプル
  ゾーン一覧、近接重複ペア（duplicate_tolerance_m 未満）。単一スコアや
  汎用の最小間隔閾値を埋め込まない。
- `evaluate_campaign_design`（fail-closed）: 状態は
  `invalid / valid_with_warnings / valid` と理由コード列で返す。
  hard 理由: holdout が宣言エリア外 / holdout が設計点に一致
  （duplicate tolerance 内）/ required zone 未サンプル / min_holdout
  不足 / 軸スパン不足 / 近接重複 / forbidden pair（防御的二重化 —
  モデル validator が先に拒否する）。warning 理由: repeatability
  を coverage として数えられないこと / 密度が宣言 weight を大きく超過
  （hidden weighting）/ diagnostic 点が weight を持つ
  （`diagnostic_point_carries_weight` — 診断点が目的関数を黙って
  変えられない）/ interleaved 指定で片方が欠落。
- `CampaignPointBinding`（封印）: 宣言位置と `observed_position` を
  別々に保持し `deviation_m` を `build_point_binding` が自動計算
  （observed 無しは 0 ではなく `None` = UNKNOWN 正直）。
  `capture_order_index` + `captured_at_utc` を永続化。
  `evaluate_placements` で deviation_tolerance 超過・未観測・
  未知 point_id を flag。
- `AcousticDiversitySpec` + `acoustic_diversity_report`
  （封印, `diagnostic_only=True` 固定）: 宣言アルゴリズム版・
  `metric='pearson_db_magnitude'`・`band_hz`・redundancy_threshold に従い
  実測応答対の冗長性フラグを出す。未知 point_id の応答はエラー。
  **report は holdout メンバシップを絶対に書き換えない**
  （predeclared membership は設計権威のみが保持）。
- `partition_for_point` / `assert_campaign_partition_disjoint` /
  `check_binding_partition_consistency`: REV55
  `assert_partition_disjoint` との整合 — holdout → 'holdout'、
  repeatability → 'repeatability'、opt/ref/boundary/standards →
  'calibration'、diagnostic → 'unassigned'。holdout 束縛が校正
  パーティションに出現したら不一致を fail。
- `SpatialCampaignTemplate` + `builtin_campaign_templates()` +
  `instantiate_campaign_template`: ベンダ実務パターンを
  **provenance 付き compatibility template** として同梱
  （Dirac/Audyssey/Trinnov 型の多点配置 + 単席 + sofa2 + リスニング
  エリア型）。インスタンス化はゾーン毎格子生成 + ラウンドロビン
  `_distribute`（RSP アンカー＝最大 declared_weight ゾーン中心、
  予約位置との近接重複回避、完全決定的）。

永続化 `cad_spatial_campaign_repository.py`
（`CadSpatialCampaignRepository`）: `cad_spatial_campaign_designs` /
`cad_spatial_campaign_evaluations` / `cad_spatial_campaign_bindings`。
append-only（同 id+sha は no-op、同 id 異 sha は
`SpatialCampaignConflictError`）。evaluation は先行して永続化された
一致 sha の design を要求、binding は design + 既知 point_id を要求
（「宣言→測定」のコミット順を機械強制）。行↔ペイロード再検証で
`SpatialCampaignIntegrityError`。

### #585 CEDIA RP32 コミッショニングプロファイル

新規 `backend/src/htdt/cad_rp32_profile.py`。

- `CommissioningDocumentIdentity`: publisher / document_id / title /
  revision / source_uri / `source_access_kind`
  （`public_announcement / public_draft / full_document /
  licensed_access`）/ document_sha256 / license_note / parser_version /
  related_rp22_revision。`full_document` は document_sha256 必須。
- `Rp32CommissioningProfile`（封印）: `clause_mapping_state` を
  **実フィールド**として保持（builder 計算値のみ受理 — 外部からの
  asserted 値は validator が拒否:
  `unpopulated_pending_lawful_source / partially_mapped / mapped`）。
  `is_rp32_claimable()` = full_document + mapped。
- `Rp32RequirementMapping`: `mapping_status` 7 状態
  （supported / supported_with_limitations / manual_evidence_required /
  external_tool_required / unsupported / not_applicable / unmapped）。
  supported 系は authority_refs + review_evidence 必須、clause_id は
  review_evidence 必須、unmapped/unsupported は authority_refs を持てない
  — **条項内容を創作しない**。
- `rp32_builtin_profile()`: CEDIA RP32 `announced-2024`、
  `public_announcement`、13 ドメイン全て `unmapped`。公開資料は
  「objective, repeatable methods for measuring and verifying audio
  system performance（vs RP22 パラメータ）」としか言及しておらず
  条項レベルの公開本文が存在しないため、同梱プロファイルは
  **断言された条項マッピングをゼロで出荷**する（UNKNOWN 正直）。
- `DesignTargetBinding`（scene_revision_id + scene_content_hash 対、
  rp22_revision/level、期待 speaker/sub 数、channel labels、equipment
  ids、任意 listening_area_design 対）と `AsBuiltObservation` の照合:
  `evaluate_designed_built_reconciliation` → 封印
  `AsBuiltReconciliation`（reconciled / reconciled_with_findings /
  incompatible / insufficient_evidence。findings =
  speaker_count_mismatch / room_geometry_mismatch /
  seating_positions_undeclared 等；unknown フィールド追跡）。
- `InstrumentEvidence.calibration_state(at_utc)` →
  calibrated / expired / unknown。不明要件は UNKNOWN のまま。
- `Rp32VerificationPlan`（封印）: profile id+sha・target・任意
  spatial_design id+sha・tasks・instrument_evidence を束縛。
  `build_verification_plan` はプロファイルが support しない条項を
  タスクが主張したら拒否。
- `evaluate_measurement_readiness`（fail-closed ゲート）: REV55–56 の
  既存権威出力（#572/#573 不確かさ予算・stimulus pin・#581 空間評価・
  measurement state）を**再利用し複製しない**。reasons があれば
  incompatible、unknowns があれば insufficient_evidence、limitations
  があれば ready_with_limitations、無ければ ready。機器 calibration
  expired → incompatible、unknown → insufficient_evidence。
- `Rp32VerificationItemResult` + `build_verification_record`（封印）:
  measured は measurement_ids 必須、pass は 'unknown' 根拠を持てない。
  `overall_state` 派生: any fail → failed / タスク不足・未検証・
  evidence 不足 → incomplete / indeterminate → inconclusive /
  client-accepted exception または not_applicable 残存 →
  verified_with_limitations / else verified。
  `rp22_state` は `rp22_rp32_measured_verified[_with_limitations] /
  _failed / _incomplete`（#579 定義を複製しない）。
  `prior_record_ids` で attempt チェーン（失敗 baseline を保持）。
  `CommissioningException` — **client acceptance は技術的 pass に
  昇格させない**。
- `evaluate_verification_freshness`: `STALENESS_TRIGGERS` 9 タグ
  （機器/ファームウェア/DSP/位置/部屋/座席/トポロジー変更等）で
  fresh / stale / insufficient_evidence。未知イベントタグを flag。
- `build_commissioning_report`（封印 `CommissioningReport`）:
  claim_text に clause_mapping_state と「CEDIA 認定ではない」ことを
  明示。record↔plan↔profile チェーンを検証（id/sha 不一致は拒否）。

永続化 `cad_rp32_repository.py`（`CadRp32Repository`）:
`cad_rp32_profiles` / `cad_rp32_verification_plans` /
`cad_rp32_reconciliations` / `cad_rp32_readiness` /
`cad_rp32_verification_records` / `cad_rp32_reports`。append-only +
コミット順強制（record は永続化済み plan+readiness+reconciliation と
全 prior_record_ids を要求、report は record+plan+profile チェーン
検証）。`Rp32ConflictError` / `Rp32IntegrityError`。

### スキーマ / 監査 / UI

- `cad_schema_ddl.py`: 上記 9 テーブルの CREATE TABLE（baseline DDL）+
  12 インデックス + ソート済みテーブルリスト登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 26`、`_migrate_25_to_26`
  （baseline DDL 再実行）、`_MIGRATIONS[26]`。test_cad_schema 台帳に
  `(26, 'migrate native schema to v26')`。
- `native_row_integrity.py`: 9 テーブル分の `_ROW_BINDINGS`
  （profile document のネスト path は `_b('document_id', 'document',
  'document_id')` 等の varargs 形式；`deviation_m` 等は optional）。
- `native_authority_audit.py`: repo resolver `'spatial_campaign'` /
  `'rp32_commissioning'` + 9 `_ReplayProbe`。
- `measurement_evidence_display.py`: JA ラベル辞書
  （`_POINT_ROLE_LABELS` / `_EVALUATION_STATE_LABELS` /
  `_RP32_STATE_LABELS` / `_RP22_STATE_LABELS`）+ `spatial_binding_line`
  （観測位置未記録は「位置未記録」と正直表示）。
- `measurement_page_workspace.py`: 遅延リポジトリ +
  `_measurement_evidence_lines` から `_spatial_campaign_lines` を連鎖
  （未束縛→「空間キャンペーン: 未束縛」、取得失敗→unavailable 行、
  束縛済み→binding 行 + 点 role 表示）。
- `application_pages.py`: `_LIFECYCLE_TABLE_LABELS` に 9 テーブルの
  JA ラベル。

### テスト

`backend/tests/test_rev56_campprofile.py`（55 件全グリーン）:
SMP10–SMP72（封印性・id 一意・明示エリア・fail-closed 理由コード・
repeat-capture 非水増し・density-hidden-weight 警告・diagnostic weight
警告・deviation/未観測/未知点・placement flag・adaptive 正直・
チャネルカバレッジ・partition 不整合・順序計画・diversity
diagnostic-only + 未知点拒否・template provenance/決定性・リポジトリ
往復/append-only/コミット順/行ドリフト）と RP32F10–F63（正直な
builtin プロファイル・full_document sha 必須・asserted 値拒否・
mapping 正直ルール・reconciliation 各状態・plan pin・条項主張拒否・
機器校正状態・readiness 各状態・record overall/rp22 状態・
measured 必須・client acceptance 非昇格・attempt チェーン・
freshness・封印 report・リポジトリ証拠チェーン順序・行ドリフト）。

## 文献根拠（web_search 一次資料）

- Dirac（helpdesk.dirac.com）: 最初の測定点はメイン/sweet-spot で
  ゲイン・遅延の基準（→ `include_reference` RSP アンカー）、点間隔
  約 30 cm 以上、測定領域を狭め過ぎると過補正、高さ・奥行きを変えた
  配置、領域外をわずかに含める点。30 cm は権威の内部定数ではなく
  template の `spacing_m` / `SamplingExpectation` の宣言値として外出し。
- Trinnov（kb.trinnov.com）: RSP が定位の基準。副測定点は繰り返しで
  はなく音響的多様性を追加する情報。多点最適化は平均ではなく joint
  analysis —→ repeat-capture は coverage に数えない・diversity report
  は診断専用という設計の直接の根拠。
- Welti & Devantier（JAES 54(5) 2006 pp.347–364; AES 115 paper 5942;
  AES 133 paper 8748）: seat-to-seat の低域変動は座席領域全体に渡る
  最適化が必要 —→ 明示ゾーン被覆・ゾーン別メトリクス・holdout 領域。
- REV55-CORRQUAL `assert_partition_disjoint` との整合: holdout は
  fitting と disjoint、事前宣言のみ —→ `partition_for_point` /
  `assert_campaign_partition_disjoint` /
  `check_binding_partition_consistency`。
- CEDIA RP32: 公開 Expo/ISE 資料は「RP22 パラメータに対する
  objective で repeatable な測定・検証手法」とのみ記述 — 条項レベル
  の公開本文が無いため同梱プロファイルは全ドメイン `unmapped` で
  出荷し、条項 id を持つ task はプロファイルが support を断言しない
  限り計画に束縛できない。

## 残存事項

- RP32 条項マッピングの実填入には CEDIA 正式文書の合法的なレビューが
  必要（`full_document` + `document_sha256` + `review_evidence`）。
  同梱 profile は claim 不能（`is_rp32_claimable()==False`）のまま。
- `Rp32TaskSpec.rp32_clause_ids` は unpopulated profile では常に空 —
  条項タグは exact source 確認後にのみ populated。
- acoustic diversity は pearson_db_magnitude 実装のみ — メトリクス
  追加時は `AcousticDiversitySpec.algorithm_version` をバンプ。
- UI は evidence 表示配線のみ（作成/設計 UX は別 issue スコープ）。
- 併走 REV セッションとの schema version 衝突: 実際に発生 —
  REV56-SNAPSTD が v25 を先にマージしたため本 PR は v26 に
  リナンバー済み（両側のマイグレーションを連鎖で保持）。

## スコープ外で観測した既存 rot（本 PR 非関与）

- `test_application_pages.py::{test_shell_registers_application_destinations,
  test_shell_project_identity_visible}` — origin/main（bd8ce1e8）
  のクリーン worktree でも同一失敗を確認（workspace enum 追加済み
  `presentation`/`video` が workflow shell registrations に未配線）。
  no-CI 環境の既存不具合。REV56-CAMPPROFILE の diff は
  application_pages の `_LIFECYCLE_TABLE_LABELS` 9 行のみで非関連。
