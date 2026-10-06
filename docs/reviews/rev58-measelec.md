# REV58-MEASELEC — オーディオI/F ループバック校正 + ゲイン構造/ノイズ床 + 再生ダイナミクス + アクティブXO + 測定法再現性 権威

スコープ: issue #699 / #651 / #649 / #665 / #693（全 P1）
ブランチ: `devin/1791334520-rev58-measelec`（merge-test 経由マージ、PR #769）
スキーマ: native schema v49 → v50（18 テーブル + インデックス追加 —
`NATIVE_SCHEMA_TABLES` 登録済み。VALIDMETH が v48、DISPLAYMEAS が v49
を先取したため連番で v50 に着地 — 3 側のテーブル・バインディング・
移行関数を併存）

設計原則は REV57/REV58 と同じ: すべての権威レコードは
content-addressed な frozen pydantic モデル（`_seal_model` + sha256 +
semantic id）、append-only リポジトリ（同 sha 再保存は no-op、
カラム改竄は IntegrityError で fail-closed）、評価器は純粋関数で
verdict を新規封印する。CLAIM > EVIDENCE を構造的に禁止する。

## 実装範囲

### #699 オーディオI/F 伝達 / ループバック校正（`cad_interface_loopback.py`）

- `CadInterfaceIoPath` — 出力→入力の厳密な経路同一性（device/port/
  channel/gain/pad/range/mode/phantom/SR/bit depth/driver/clock ref/
  loopback kind/cable/firmware/acquisition class）。
  `mismatches_against` は欠損フィールドを `unknown_<name>` として正直に
  報告 — 未記録は一致ではなく limitation。
- `CadLoopbackObservation`（ifcobs-）— #608 stimulus・#695 measchain・
  #573 state を sha pin。観測レベルに peak/rms 意味づけ。
- `CadInterfaceTransferCalibration`（ifccal-）— `CalibrationKind` で
  合成ループバックと単側校正を区別。`output_path_characterized_*` /
  `input_path_characterized_*` は `CadDeembeddingEvidence`
  （独立参照計器 + 手法）必須 — 合成1本で両側の真値は決して得られない。
  `digital_loopback` はルーティング/フォーマット検証専用でアナログ
  伝達量を保持不可。`complex_response`/`latency_delay` は
  `CadPhaseSemantics`（#609 timebase sha 必須）。`usb_integrated_capture`
  は analog ループバックを拒否。`usable` 帯域は `measured` 帯域を
  超えられない — 端値保持は宣言ポリシーであり coverage ではない。
- `evaluate_interface_correction`（ifcqual-）— 梯子: calibration 欠落
  → `correction_missing`、要求量なし → `correction_not_required`、
  SR 不一致/USB境界/デジタル/材質 mismatch → `correction_ineligible`、
  帯域外・位相 pin 欠落・unknown フィールド・SR 等価依拠 →
  `correction_applied_with_limitations`。`component_truth_valid` は
  単側 kind + de-embedding 証拠の時だけ `valid`。

### #651 ゲイン構造 / ノイズ床 / クリップ余裕（`cad_gain_noise_structure.py`）

- `CadSignalLevelReference`（lvlref-）— 段別 dBFS↔dBu/dBV/Vrms 参照。
  `analog_unit='unknown'` 拒否 — 単位なき数値は dBu/dBV/Vrms を黙って
  混ぜる。V RMS は正値強制。
- `CadNoiseFloorObservation`（gnobs-）— `NoiseClass` で電気チェーン/
  スピーカー自騒音/室内背景音/ハム・EMC/機械を分離 — 音響ヒス測定が
  電気段の所在を自称しない。`measurement_floor_limited()`: 測定器
  フロアから 6dB 以内は bounded 結果のみ報告。
- `CadClippingMarginQualification`（clipm-）— 型付きメカニズム
  （digital FS / analog stage / amp VI limit / dynamic limiter /
  loudspeaker compression）は観測閾値必須。`clipping_margin_db()` は
  単位一致時のみ算出。
- `CadSnrDeclaration` — weighting 必須 — SNR は裸の数字ではない。
- `evaluate_gain_structure`（gnqual-）— 証拠ゼロ →
  `unqualified_insufficient_evidence`、フロア支配 →
  `measurement_floor_limited`、ステージ無名の型付きクリップ →
  `clip_stage_unresolved`（汎用「system clipping」診断は出さない）、
  電気ノイズ無帰属 → `noise_floor_unattributed`。マルチch/LFE
  ストレス要求は単ch 証拠を継承しない。`limiting_stage_label` は
  nominal との最小マージン段。

### #649 再生ダイナミクス / リミッタ（`cad_playback_dynamics.py`）

- `CadPlaybackDynamicsState`（dynstate-）— device/decoder/codec/
  firmware/preset/volume/output mode/downmix/room correction/bass
  management + 機構レコード列。`CadDynamicsMechanismRecord` は
  DRC/dialnorm/night mode/loudness comp/limiter/protection/thermal を
  METADATA_PRESENT vs DECODER_CONFIGURED vs APPLIED_OBSERVED で区別、
  `engaged_during_capture` は runtime 証拠必須。`CadContentMetadata` の
  `applied_gain_shift_db` は `decoder_configured_to_apply` 必須
  （ATSC A/85 意味論: dialnorm は宣言値であり適用はデコーダ設定依存）。
- `CadLevelSweepObservation`（dynobs-）— ≥2 レベル点、verdict に
  `level_dependent_gain`/`level_dependent_spectral_change`/
  `limiter_compression_suspected`/`protection_engaged`。confirmed stage
  帰属は stage 名必須、`not_performed` は封印拒否。
- `evaluate_playback_dynamics`（dynqual-）— `before_after_comparison` は
  baseline との状態一致が前提 — 機構差・同一性フィールド差は
  `state_mismatch` で `comparison_eligible`/`causal_attribution_valid`
  を `invalid`。隠れ処理（`processing_topology_unknown`）は max
  capability 系を `hidden_processing_uncharacterized` に降格。

### #665 アクティブ多ウェイ XO 校正（`cad_active_crossover.py`）

- `CadMultiwaySpeakerDefinition`（axospk-）— ≥2 way、各 way に物理
  ドライバ同一性必須、#654 acoustic origin ref pin。
- `CadActiveCrossoverPlan`（axoplan-）— `CadWayFilterSpec`
  （family/order/Hz/topology/domain）、`CadWayAlignment`
  （requested vs deployed gain/delay/dsp polarity）、
  `CadWayRoutingProof`（verified は channel+polarity+cross-route
  の全チェック必須、`method='unknown'` は verified 不可）、
  `CadManufacturerEnvelope`（minimum HPF）。
  `protection` kind は `mandatory_protection=True` 強制、逆に
  mandatory_protection は hpf/protection kind のみ。
- `CadDriverAlignmentMeasurement`（axomeas-）— way 別に stimulus/
  timebase/calibration/measchain/state を sha pin、遅延・レベル・
  音響極性を記録。
- `CadSpliceAssessment` — `coherent_sum` は複素/位相証拠 pin 必須
  （magnitude-only ではキャンセルを検出不可）、`unevaluated` 拒否。
- `evaluate_active_crossover`（axoqual-）— fail-closed 順序:
  未証明ルーティング → エンベロープ/保護違反 → deployed 不一致 →
  破壊的合成 → 未測定 way → qualified/limited。
  `room_correction_eligible` は qualified-ish + routing + protection
  が揃う時のみ `valid` — XO 未較正のまま室補正結果を claim しない。

### #693 測定法再現性 / inter-operator 精度（`cad_method_reproducibility.py`）

- `CadMethodProcedure`（repproc-）— 手順名 + 版 + `documented`。
  stimulus/pipeline/measchain の pin は任意。
- `CadReproducibilityCampaign`（repcamp-）— `CampaignDesignClass`
  （crossed_factorial/single_factor_at_a_time/nested/repeated_only）と
  disjoint な `varied_factors`/`held_factors`、`CadCampaignRun` 列
  （completed は artifact sha 必須、excluded は理由必須）。
  `EvidenceTier`: repeatability_only は varied factor 不可、
  intermediate 以上は varied factor 必須、`interlaboratory_formal` は
  crossed_factorial 必須 — 内部研究は正式 ISO 5725 適合を自称しない。
  主張 tier に ≥2 completed runs 必須。
- `CadMethodPrecisionModel`（repmod-）— `CadMetricPrecision`:
  `CadVarianceComponent`（variance²≈std_dev 整合性）、
  `repeatability_limit_r` ≤ `reproducibility_limit_R`。
- `evaluate_reproducibility`（repqual-）— undocumented 手順は証拠を
  生まない。repeatability-only は `repeatability_only_established` で
  `prediction_gate_eligible`/`decision_gate_eligible`（#566/#577 帰結
  ゲート）は閉じる。crossed + bound model →
  `precision_model_established` で両ゲート開放。
  single-factor-at-a-time は confound を `confounded_factors` に記録し
  `confounded_design`。

## リポジトリ / スキーマ / 監査配線

- 5 リポジトリ（`*_repository.py`）— `_assert_sealed` + append-only
  save（同 id+同 sha no-op、異 sha ConflictError）、読み出しで
  payload×カラム整合を再検証 → IntegrityError。
- `cad_schema_ddl.py`: 18 テーブル CREATE + doc インデックス、
  `NATIVE_SCHEMA_TABLES` 登録。`cad_schema.py`: `_migrate_49_to_50`、
  `NATIVE_SCHEMA_VERSION = 50`。
- `native_row_integrity.py`: 18 テーブル全ての行バインディング
  （nested ref パスは `_b(col, 'parent', 'child', optional=True)`）。
- `native_authority_audit.py`: リポジトリ resolver に 5 分岐、
  `_REPLAY_PROBES` に 18 プローブ追加。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` + 
  `measurement_evidence_display.py` の JA 表示行 5 関数追加。

## 文献根拠

- ループバック: 合成 DAC→ADC ループは両側伝達の積しか測れない —
  単側の真値には独立参照（de-embedding）が必要。REW 系 cal ファイル
  慣習（usable vs measured 帯域・補間ポリシー・範囲外処理）と
  USB マイク一体型境界。
- ゲイン構造: −20dBFS=+4dBu 型参照は段別証拠であり全局仮定ではない。
  ノイズ床積算・段別クリップマージン・測定器フロア近接時の bounded
  報告。
- 再生ダイナミクス: ATSC A/85 dialnorm とデコーダ適用ゲイン、EBU
  ラウドネス正規化、DRC/ナイトモード/リミッタ/保護機構を隠れ DSP
  ではなく宣言ランタイム状態として扱う。
- アクティブ XO: ドライバ段適格化は室補正に先行。極性・遅延・レベルを
  way 別に照合し、XO 帯域合成は複素（位相）和が必須 —
  magnitude-only では相殺を検出できない。
- ISO 5725: repeatability（同一条件）と reproducibility
  （operator/instrument/session 変動）は別の精度階層。
  手順文書化 ≠ 再現性 — 分散成分を個別推定し、単因子逐次計画は
  変動因子を confound する。

## 検証

- `backend/tests/test_rev58_measelec.py`: 33 テスト全グリーン
  （シール/ID 接頭辞、sha pin 強制、各 verdict 梯子、append-only
  再保存、カラム改竄 IntegrityError、fresh-migrate 全テーブル存在）。
- スコープ外スイート: `test_cad_schema`（v50 台帳含む）+
  row-integrity/audit/coverage/revalidation + application_pages +
  REV57/58 sibling 全グリーン。
- マージコンフリクト解消: VALIDMETH (v48)・DISPLAYMEAS (v49) との
  併存を両側保持で解消 — DDL タプル・`_ROW_BINDINGS`・
  `_REPLAY_PROBES`・resolver・labels・display 行を両保持、
  `ast.parse` + fresh v50 構築で全テーブル確認。

## 残件

- 実 I/F・AVR・DSP の実測ウィザード配線 — 権威レコードは完成、
  UI 経路は後続（device 列挙・プロトコル依存）。
- #651 は #646 gain-structure scenario への pin を保持するが、
  scenario 生成側から本権威への自動呼出は未配線。
- #649 level sweep の測定 UI / 自動判定点列生成は後続。
- #693 operator 収集ワークフロー（誰が・いつ・どの条件で）の
  campaign 実行 UI は後続 — 現状は記録・評価権威のみ。
