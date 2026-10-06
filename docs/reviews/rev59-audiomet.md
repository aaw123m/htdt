# REV59-AUDIOMET — 受信基準点/治具散乱/エコー診断/DRR

schema v64・`cad_acoustic_metrology_repository`（8ストア）・
`test_rev59_audiomet.py`（24テスト）。

## #774 測定受信基準点（P1）

- `ReceiverReferencePoint`（rref-）: カプセル位置がどの物理点を
  表すかを宣言（acoustic_centre / diaphragm_center /
  protection_grid / manufacturer_reference）。IEC 61094-8:
  非音響中心の基準点はオフセットかその不確かさを必須宣言。
- `MicrophoneCapsulePose`（mcp-）: 測量位置を基準点レコードに
  束縛。
- `evaluate_receiver_origin_claim` → phase/early_reflection_delay
  系の精密 claim は周波数依存の音響中心証拠を要求
  （acoustic_centre_conflation）。不確かさ未宣言の非音響中心
  基準点での精密 claim → uncertainty_budget_required。

## #743 測定治具/観測者散乱（P2）

- `MeasurementFixture`（mfx-）: クリップ・ブーム・スタンド・
  アレイフレーム・ケーブル・PC/台・オペレータ身体を登録。
- `FixtureScatteringEvidence`（fsx-）: measured/estimated/
  declared_absent の散乱上限。'measured' は証拠 ref 必須、
  'declared_absent' は fixture 列挙を拒否。
- `evaluate_fixture_claim` → **校正済みマイクはセットアップの
  音響透明性を証明しない**（calibrated_mic_is_not_setup）。
  登録治具に上限なし → scattering_unbounded。

根拠: Terashima et al. 2021 — 1mm ホルダーバンドで 20kHz 付近
~2dB の応答偏差を実測。

## #773 フラッター/集束エコー診断（P1）

- `DiscreteReflectionEvent`（dre-）: 遅延/レベル/周期性を ETC
  証拠に束縛。periodic_train は繰返し間隔必須。
- `EchoDiagnostic`（edia-）: 信号クラス（speech/music）スコープ
  の Dietsch 系診断 — 'unknown' 拒否、disturbing verdict は
  criterion 値 + 閾値 ref 必須。
- `evaluate_echo_diagnostic_claim` → **広帯域指標の良好さはエコー
  証拠でない**（broadband_metrics_are_not_echo_evidence）。

根拠: Dietsch & Kraak 1986（ODEON/EASERA 実装）— ただし閾値は
信号クラス/聴取者/反射パターン依存で普遍真理でない。

## #678 DRR 権威（P2）

- `DRRMethodProfile`（drrm-）: 直接音窓・受信機種別（omni/
  binaural/directional）・帯域・境界近接を pin — 'unknown' 受信機
  や非正の窓は fail-closed。
- `DRRMeasurement`（drrv-）: 手法+測定 ref 必須。
- `evaluate_drr_claim` → 裸スカラー → unbounded_scalar、窓/
  受信機/帯域が違う比較 → cross_method_comparison。

## 残件

- 実測の散乱上限推定・ETC からのイベント検出器は未実装 —
  権威は証拠ゲート層。
- Dietsch 判定式本体は係数テーブル+検証が要（現状は verdict
  レコードのみ）。
