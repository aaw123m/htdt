# REV59-LISTENEXP — 聴取実験/補聴経路/動的バイノーラル

schema v63・`cad_listening_evidence_repository`（9ストア）・
`test_rev59_listenexp.py`（15テスト）。

## #696 主観聴取実験（P1）

- `ListeningExperimentPlan`（lexp-）: 手法ファミリ
  （bs1116_3/mushra_bs1534_3/bs1284_2/abx/paired_comparison）と
  impairment regime を宣言必須。'unknown' は fail-closed、
  MUSHRA を small_impairment で宣言すると拒否（regime 不一致）。
- `ListenerQualification`（lqual-）: スクリーニング/訓練/
  test-retest 証拠を束縛。`qualified` プロパティは3条件全充足。
- `SubjectiveInferenceRecord`（sinf-）: plan pin 必須、
  `significant` verdict は CI 必須。
- `evaluate_experiment_claim` → 6 判定: 計画なし →
  unverified_result、ランダム化なし → unrandomized_trials、
  パネル未適格 → unqualified_listeners、試行量不足 →
  insufficient_trials、推定なし → unverified_result。

根拠: ITU-R BS.1116-3/BS.1534-3/BS.1284-2 の手法別適用域
（draft 版は production profile でない）。

## #726 補聴システム（P2）

- `AssistiveListeningPath`（alsp-）: 技術種別
  （induction_loop/fm_receiver/ir_receiver/hardwired/auracast）
  宣言必須、'unknown' は fail-closed。
- `ALSQualification`（alsq-）: IEC 60118-4 系の磁界強度/SNR/
  周波数応答証拠を pin。
- `ReceiverCompatibilityEvidence`（rcomp-）: telecoil 等の受信機
  互換証拠。
- `evaluate_als_claim` → スピーカー性能は補聴証拠でない
  （speaker_perf_is_not_als）、ルーティング未検証、
  フィールド未測定、受信機不適合を各々 fail-closed。

## #727 動的頭部追跡バイノーラル（P2）

- `DynamicBinauralSession`（dbin-）: HRTF クラス
  （individual/estimated/generic、'unknown' 拒否）+ 姿勢
  フレーム + 更新レート。
- `PoseTrackingEvidence`（ptrk-）: motion-to-audio 遅延の宣言値
  は測定 ref 必須（未測定遅延はゼロでない — slippage）。
- `BinauralQualification`（bqual-）: 角度サンプリング証拠 pin。
- `evaluate_binaural_claim` → 静的レンダーは動的 claim の代理に
  ならない（static_is_not_dynamic — 頭部運動自体が定位手がかり）、
  遅延未測定 → tracker_latency_unmeasured。

根拠: Begault/Wenzel/Anderson JAES 2001（頭部追跡・個別 HRTF・
残響が定位誤差/前後逆転/頭外定位に有意影響）、AES 2025 動的
バイノーラル比較（HRTF クラスは動的でも有意）。

## 残件

- 実試験設計のワークフロー/登録 UI は未配線 — 本ラウンドは権威
  モデルとゲートのみ。
- ALS の管轄区域要件は外部（#599 規格レジストリ経由で版 pin 可）。
- 動的レンダーのリアルタイム経路は未実装 — 権威は証拠ゲート層。
