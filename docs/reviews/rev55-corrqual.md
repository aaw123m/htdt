# REV55-CORRQUAL: ルーム補正の適格性ゲート (issue #568)

## 実装範囲

補正フィルタが「このシステム・この受聴領域に対して修飾済み(qualified)」と
言えるための横断的証拠契約。新しい逆フィルタアルゴリズムでも新しい DSP
転送でもなく、**qualification evidence contract**。

### 新規権威

- `backend/src/htdt/cad_correction_qualification.py`
  - `CorrectionQualificationRecord` — 自己シール不変レコード
    (`correction-qualification:<semantic_sha256>`)。フィルタ同一性
    (subject_id + semantic hash)、対象領域、登録証拠、測定セット
    (baseline/post、校正用 vs holdout の機械的分離)、正則化パラメータ、
    制約宣言を一つの payload に封じる
  - `CorrectionSubjectRef` — calibration_plan / fir_artifact 両対応の被験体ピン
  - `ListeningRegion` — design / holdout / repeatability の機械的パーティション
    (design∩holdout、holdout∩repeatability は拒否。design∩repeatability は
    同一座席再測定＝安定性証拠として許容)
  - `ConstraintDeclaration` — max_boost/cut・usable_band・headroom・
    pre-ringing・latency。未宣言は unknown であり unlimited ではない
  - `evaluate_correction_qualification()` — 8 ゲートを評価し証拠ラダーから
    state/scope を導出する純粋関数
  - 独立 observables — target_deviation, seat_to_seat_variance,
    worst_seat_deviation, group_delay, pre_ringing, latency, peak_boost,
    headroom_margin, capability_margin, before_after_measurement,
    deployed_state_match, repeatability。各軸は独立の結果であって
    単一の合格 bool ではない
  - JA ラベル (`QUALIFICATION_STATE_LABELS`/`QUALIFICATION_SCOPE_LABELS`/
    `GATE_STATUS_LABELS`)

- `backend/src/htdt/cad_correction_qualification_repository.py`
  - `cad_correction_qualifications` テーブルへの追記専用永続化
  - `current_for_correction` は subject semantic sha 完全一致のみ返す —
    フィルタ係数・ターゲット・デバイス・ルーティングの実質変更は
    古い修飾を自動的に失効させる
  - 行ペイロードは読み出し時に再検証(自己シール + 列/ペイロード一致)

### ゲート(fail-closed)

1. `identity` — UNSUPPORTED プランは不適合
2. `boost_cut` — 宣言 max_boost/cut、デバイス capability、
   EQP10 `SpatialCorrectionEvidence` の帯域別許容ブースト(local null の
   過剰ブーストは帯域毎に拒否)
3. `usable_band` — 補正帯域がスピーカー有効帯域の外側: 完全域外→fail、
   部分的域外→limitation
4. `headroom` — ピークブーストが宣言 headroom 超過→fail、未宣言→not_evaluated
5. `pre_ringing` — FIR の pre-ringing energy 比と latency を検査
   (peq は前応答を持たないため pass、topology unknown は not_evaluated)
6. `control_fidelity` — 設計点での実測悪化→fail、改善なし→limitation
7. `holdout_independence` — 宣言 holdout の物理再測定が必須。未宣言→
   limitation、未測定→limitation、悪化→fail、全点改善→pass
8. `closed_loop_deployment` — observed_mismatch→fail、not_observable→
   limitation(実測レーンを通過しない)

### state/scope

- state(証拠ラダー): INSUFFICIENT_EVIDENCE → DESIGN_ONLY → SIMULATED →
  MEASURED_AT_CONTROL_POINTS → SPATIALLY_HOLDOUT_VERIFIED →
  DEPLOYED_AND_REMEASURED。修飾クレーム到達時に制限が残る場合は
  QUALIFIED_WITH_LIMITATIONS、ハード失敗は INCOMPATIBLE
- scope(修飾クレーム): unqualified/candidate/qualified_point/
  qualified_region。`qualified_region` は holdout ゲート pass +
  宣言 holdout 全点の物理再測定を要求(レコード validator でも機械的強制)
- 単一点のみの証拠は正直に `qualified_point` — 領域 claim はしない
- 主観証拠(`subjective_evidence`)は記録されるがゲートを開けない

### UI 配線(最小限)

- 校正設定エクスポートのプラン選択行に `修飾状態: <JA scope>` を表示
  (subject sha 不一致の古いレコードは 未修飾 として表示)
- `_LIFECYCLE_TABLE_LABELS` に `cad_correction_qualifications` =
  「補正修飾レコード」追加

### スキーマ/監査

- `NATIVE_SCHEMA_VERSION` 20→21 + `_migrate_20_to_21`
- `_ReplayProbe('correction_qualification', ...)` + repository factory
- `native_row_integrity` `_ROW_BINDINGS` 登録(列↔ペイロードドリフト検査)

## 文献根拠

- **Kirkeby & Nelson 1999 (JAES 47(7/8))**: 逆フィルタ設計には周波数依存
  正則化が必須 — 深いノッチの反転は無限ブーストを要求し物理的に不可能。
  → `SpatialCorrectionEvidence` の local_null 帯域許容ブーストを
  boost_cut ゲートの上限として採用(政策の max_boost_db と独立に、
  帯域別許容量が上書き拒否できる)
- **Cecchi et al. 2018 (Applied Sciences 8(1),16)** ルーム応答
  イコライゼーション総説: (a) 単一点 EQ は受聴位置が変わると悪化しうる
  → design/holdout の機械的分離と qualified_point vs qualified_region の
  scope 区別、(b) 混合位相補正は pre-ringing を生じ知覚劣化しうる
  → pre_ringing ゲート + observable、(c) スピーカー有効帯域外の補正は
  非線形・損傷リスク → usable_band + headroom ゲート
- **Stefanakis/Sarris/Jacobsen 多点 EQ 正則化**: 制御点集合と独立な評価点
  での検証が領域一般化の証拠になる → holdout_independence ゲートは
  design 集合に混入しない独立宣言集合を要求
- **実務的安全制約(REV55 調査)**: ~6dB 程度のブースト上限・ヘッドルーム
  マージンは文献的一般慣行だが、評価器は上限を捏造しない — すべて
  `ConstraintDeclaration` の明示宣言に委ねる(未宣言は not_evaluated +
  limitation、never pass)

## #564/#566/#570/#571 との関係

- #564(REGCAL): `RegistrationEvidence` として registration の id/sha/
  partition/ComparabilityState を束縛可能。incomparable→disqualifier、
  comparable_with_limitations/insufficient_evidence→limitation。
  registration 非束縛でも動作(comparability=None の正直な未登録)
- #566/#570/#571 の envelope/材料互換/指標適用性ゲートとは矛盾しない —
  本契約は補正フィルタの修飾のみを扱い、それらの適用域には踏み込まない

## 残存事項

- 測定インポート/REW パイプラインからの baseline/post セット自動構築は
  未配線(評価器は `PositionResponseSet` を直接受け取る)。呼び出し側は
  実測のみを physical_measurement として渡す責任を持つ
- ワークフロー UI はエクスポート選択行への scope 表示のみ —
  修飾レコードの一覧・詳細画面は未実装
- CamillaDSP read-back 等の deployed-state 観測器との結線は
  `DeploymentObservation` の入力として残置(自動化は後続)
- `headroom_db` 宣言はオペレータ入力 — `AmplifierOutputCapability`
  との自動合成は別タスク(権威は既存)
- **既知の既存ロット(本変更と無関係)**: `test_cad_schema_ddl_contract.py::
  test_baseline_ddl_only_creates_declared_tables` は a781e5d6 で追加された
  `capture_authoring_provenances` が `NATIVE_SCHEMA_TABLES` 未登録のため
  main HEAD(bda6add5)でも失敗。本PRでは修正していない

## 回帰テスト

`backend/tests/test_rev55_corrqual.py` — 34 テスト:

- レコードシール/改竄拒否、全ゲート網羅性
- DESIGN_ONLY/SIMULATED が自動修飾されないこと
- qualified_point/qualified_region/DEPLOYED_AND_REMEASURED の到達条件
- boost/device/usable-band/headroom/pre-ringing/holdout 各 fail 経路
- 制約未宣言が pass にならないこと(UNKNOWN 正直)
- design∩holdout の拒否、repeatability が holdout を満たさないこと
- subject sha 不一致/ポスト位置未宣言/UNSUPPORTED プランの fail-closed
- 主観証拠がゲートを開かないこと、incomparable registration の disqualify
- 永続化 roundtrip・stale-sha 非返却・競合検出
