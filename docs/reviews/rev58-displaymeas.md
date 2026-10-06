# REV58-DISPLAYMEAS — パターンジェネレータ忠実度 + プローブマッチング + 加法性/RGB分離 + 時間応答 + LUT クローズドループ 権威

スコープ: issue #682 / #680 / #686 / #647 / #666（全 P1/P2）
ブランチ: `devin/1791274958-rev58-displaymeas`（merge-test 経由マージ）
スキーマ: native schema v48 → v49（26 テーブル + インデックス追加 —
`NATIVE_SCHEMA_TABLES` 登録済み。REV58-VALIDMETH が v48 を先取した
ため連番で v49 に着地 — 両側のテーブル・バインディング・移行関数を併存）

設計原則は REV57/REV58 と同じ: すべての権威レコードは
content-addressed な frozen pydantic モデル（`_seal` + sha256 +
semantic id）、append-only リポジトリ（同 sha 再保存は no-op、
カラム改竄は IntegrityError で fail-closed）、評価器は純粋関数で
verdict を新規封印する。CLAIM > EVIDENCE を構造的に禁止する。

## 実装範囲

### #682 パターンジェネレータ送出忠実度（`cad_pattern_generator_fidelity.py`）

- `PatternGeneratorInstance`（pginst- 封印）— 同一性に firmware /
  control software / API adapter / port / config hash を含む。
  `GeneratorCapability` は入力制御・内部処理・出力リンクの bit depth を
  独立宣言 — 出力 10bit の表示は上流の 10bit 到達性を証明しない。
- `RequestedVideoPatch`（reqpatch-）— #608 刺激資産を sha pin +
  `PatchNumericIdentity`（code values / bit depth / range / encoding /
  colorimetry / EOTF / chroma / raster / HDR metadata）。
  HDR metadata は non-none なら内容 sha 必須。
- `DeliveredStimulusObservation`（delobs-）— 観測点
  （generator_output / link_negotiated / display_input /
  displayed_optical）と検証手段を宣言。`unverified` は delivered
  値を主張できない。protocol_readback / link_analyzer は証拠 pin 必須。
- `evaluate_patch_delivery` — code value・range・bit depth・encoding・
  HDR metadata の mismatch を分類して `PatchMismatch` として記録。
- `evaluate_generator_fidelity` — 梯子: delivered_mismatch 1件でも
  `fidelity_unqualified`、全 verified → `fidelity_qualified`、
  transform 宣言あり込み → `qualified_with_limitations`、それ以外
  （unverified 混在）→ `insufficient_evidence`。要求 bit depth が
  宣言能力を超える場合は qualified → limited に降格。

### #680 カラリメータ分光ミスマッチ / プローブマッチング（`cad_meter_match.py`）

- `InstrumentIdentity` — kind + manufacturer + model + serial +
  firmware + mode + integration time + #611 校正 ref（calibrated
  provenance を主張するなら pin 必須）。
- `DisplaySpdIdentity` — display instance/model + firmware +
  picture/gamut/white-point/emitter/content/dimming/optical-path。
  `spd_basis='measured_spd'` は実測 SPD 内容 sha 必須。
- `DisplayMeterMatchProfile`（mmprof-）— target は
  tristimulus_colorimeter 限定、reference は spectroradiometer/
  spectrophotometer 限定、同機種同シリアル拒否。既存補正状態を
  `PreExistingCorrection` で pin（active 系は correction_ref 必須、
  unknown は正直に記録）。
- `ProbeMatchObservation`（pmobs-）— 全パッチに残余 ΔE 必須。
  `ProbeMatchVerification`（pmver-）— 独立 holdout パッチセットで
  検証、pass 時の最大残余 ≤ 閾値をバリデータが強制。
- `evaluate_correction_applicability`（mmappl-）— 別ユニット/
  別ディスプレイ/表示状態差 → `not_applicable`、correction_ref 不一致
  の custom_match_active → `DOUBLE_APPLICATION_RISK`、
  検証欠落 → `applicable_with_limitations` 上限。

### #686 加法性 / RGB 分離 / ボリューム特性（`cad_display_additivity.py`）

- `DisplayAdditivityObservation`（daobs-）— レベル別の R/G/B 単独
  XYZ と合成 XYZ を一つの #625 display state に pin。
  `evaluate_additivity` はスケール正規化残余を返す。
- `RGBSeparationAssessment`（rgbs-）— r/g/b ランプ必須、結合残余
  ΔE を宣言。
- `VolumetricCharacterisation`（dvol-）— `measured_count >=
  grid_size³` を構造強制（疎 pass は volume でない）。
- `HoldoutVerification`（dhold-）— 独立パッチで検証。
- `evaluate_model_eligibility`（cmelig-）— モデル系別:
  - `matrix_1d` / `sparse_lightning_lut` / `shaper_3d_lut` は加法性
    **AND** RGB 分離が必須 — 片方欠落は `insufficient_evidence`、
    測定不合格は `inappropriate`。
  - `fixed_grid_volumetric_lut` は密集ボリューム必須。
  - holdout 失敗は `inappropriate`、holdout 欠落は
    `eligible_with_limitations` 上限。
  - 証拠ゼロは `insufficient_evidence`。
- `CharacterisationPlan`（charplan-）— 証拠由来の最小密度宣言。

### #647 時間応答 / モーション忠実度（`cad_temporal_display.py`）

- `TemporalDisplayState`（tdstate-）— #625 display state に加えて
  refresh/VRR/interpolation/BFI/strobe/overdrive/low-latency を pin。
- `TemporalStepResponseMeasurement`（tstep-）— 波形 sha + 閾値定義
  （`StepResponseDefinition`）+ センサ帯域必須。
- `MotionArtifactMeasurement`（mart-）/ `FlickerMeasurement`
  （tflick-）/ `ImageRetentionObservation`（tret-）— メカニズム別
  クラス、instrumented/documented/subjective 分離。
- `evaluate_temporal_qualification`（tdq-）— claim 毎に判定、
  **この状態に sha 束縛された測定のみ**計上（他状態の証拠は数えない）、
  証拠欠落は `insufficient_evidence`、リフレッシュ表記は何も証明しない。

### #666 LUT クローズドループ較正（`cad_lut_closed_loop.py`）

- `DisplayLUTArtifact`（lutart-）— kind + grid/entry + precision +
  `LutDomainSpec`（5 ドメイン全宣言必須 — unknown 拒否）+
  `LutTargetSpec`（HDR は peak nits 必須）+ payload hash。
- `LUTGenerationRecord`（lutgen-）— characterisation + patch set +
  エンジン同一性（gamut_mapping 宣言必須）を pin。
- `LUTPreflightVerification`（lutpre-）— `numeric_validation_passed`
  かつ `domain_scaling_checked` でないと pass を封印できない。
- `LUTDeploymentRecord`（lutdep-）— device/slot/method + readback sha。
- `LUTPostVerification`（lutpost-）— holdout patch set + same physical
  path + side effects（gradation_loss 等）の正直記録。
- `evaluate_lut_closed_loop`（lutq-）— 梯子:
  generation 欠落/不整合 → `insufficient_evidence` +
  GENERATION_PROVENANCE_MISSING; preflight 欠落 → `loop_unverified`、
  失敗 → `loop_failed`; readback 欠落 → unverified、mismatch →
  failed; post 欠落/holdout が generation パッチと同一 → unverified
  （HOLDOUT_NOT_INDEPENDENT）、post 失敗 → failed; side effects →
  `loop_limited` 上限。

## リポジトリ / スキーマ / 監査配線

- `cad_display_metrology_repository.py` — 汎用 `_SealedStore`
  （append-only: 同 id+同 sha は no-op、同 id+異 sha は
  ConflictError、読み出しで payload×カラム整合を再検証）を駆動する
  5 リポジトリクラス。
- `cad_schema_ddl.py`: 26 テーブル CREATE + doc インデックス、
  `NATIVE_SCHEMA_TABLES` 登録済み。`cad_schema.py`: `_migrate_48_to_49`、
  `NATIVE_SCHEMA_VERSION = 49`。
- `native_row_integrity.py`: 26 テーブル全ての行バインディング。
- `native_authority_audit.py`: `_RepositoryChain._build` に 5 分岐、
  `_REPLAY_PROBES` に 26 プローブ追加。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` + 
  `measurement_evidence_display.py` の JA 表示行 6 関数追加。

## 検証

- `backend/tests/test_rev58_displaymeas.py`: 38 テスト全グリーン
  （シール/ID 接頭辞、sha pin 強制、各 verdict 梯子、append-only
  再保存、カラム改竄 IntegrityError、fresh-migrate 全テーブル存在）。
- マージコンフリクト解消: VALIDMETH (v48) との併存を両側保持で解消、
  DDL タプル・`_ROW_BINDINGS`・`_REPLAY_PROBES`・labels を両保持、
  `ast.parse` + fresh v49 構築で全テーブル確認。

## 残件

- 実ジェネレータ/測定器/ディスプレイの実測ウィザード（manifest
  manual チェック）— 接続プロトコル依存。
- 加法性/LUT の registration UI — 権威レコードは完成、UI 経路は後続。
