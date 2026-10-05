# REV56-BASSSTIM — バス管理/クロスオーバー適格性 + 刺激/校正アセット登録簿

スコープ: issues #574 (P0), #608 (P0)
ブランチ: `devin/<ts>-rev56-bassstim`
スキーマ: native schema v23 → v24（5 テーブル追加）
マージ: merge-test 経由で main へセルフマージ（実施後にコミット ID を記入）

## 実装範囲

### #574 バス管理/クロスオーバー適格性

新規 `backend/src/htdt/cad_bass_management_qualification.py`（#633
`BassManagementProfile` の宣言権威に対する*測定照合ゲート*として合成。
ルーティング/トポロジー権威を複製せず `evaluate_bass_management` を再利用）。

- `SplicePathEvidence`（封印, `evidence_sha256`）: メイン単独
  （`main_only`）/ サブ単独（`sub_only`）/ 同時再生和
  （`main_plus_sub_summed`）/ `lfe_only` / `redirected_bass_only` の
  各経路を role↔sub_group↔seat に紐付けた測定証拠。`ResponseCurve` は
  magnitude 必須、phase・delay・polarity は任意 — 位相証拠が無い場合は
  推定せず UNKNOWN。
- `evaluate_splice`: crossover 帯域（既定 ±0.5 oct）で
  `worst_splice_margin = min(summed − max(main, sub))` を算出。
  −6 dB 超の落ち込みは*実測された*相殺として `cancellation`。
  両経路に位相証拠がある場合は宣言 delay（−2πf·Δt）と polarity（+π）
  を適用した複素和を予測し、実測との RMS 差が 3 dB を超えたら
  `device_state_mismatch`（宣言と実装状態が違う）。相殺の原因帰属:
  `polarity` → `polarity_mismatch`、delay がノッチ周波数で λ/2±25%
  （半周期剰余）→ `delay_mismatch`、それ以外 →
  `phase_cancellation_at_splice`。
- `evaluate_usable_band`: crossover が両側の実測 usable band に
  margin（既定 5 Hz）で収まるか。未記録は not_evaluated、範囲外は
  `main_too_weak_below_crossover` / `sub_too_weak_above_crossover`。
- `evaluate_sub_headroom`: 転送バス + LFE + EQ boost + 配信ゲインを
  コヒーレント worst-case で総和し、宣言 capability と照合
  （`insufficient_sub_headroom`）。capability 未記録は not_evaluated —
  平坦な小信号スイープだけでは番組ヘッドルームの証拠にならない。
- `evaluate_bass_management_qualification`: ゲート列（routing /
  duplicate_low_frequency_path / usable_band[role] / filter_topology /
  splice_summation / sub_headroom[dest] / deployed_state）は降格のみ
  行い昇格しない。判定 `qualified / qualified_with_limitations /
  not_qualified / insufficient_evidence` と範囲
  `unqualified / candidate / qualified_point / qualified_region` を
  分離 — 制御席のみ良好なら qualified_point、holdout 席でも良ければ
  qualified_region。holdout での相殺は `multi_seat_instability` として
  失敗理由に残し scope を抑止。
- 失敗分類は issue §14 の 12 項目をそのまま literal 化。
- `BassManagementQualification`（`bmq-<sha24>`）は profile_id +
  profile_sha256 を結合 — 適格後にプロファイルを編集した古い判定を
  再解釈できない。`lifecycle_at_evaluation` と
  `remeasured_post_apply` 証拠が deployed_state を制御。
- 永続化 `cad_bass_qualification_repository.py`:
  `cad_bass_splice_evidence` / `cad_bass_qualifications` テーブル、
  append-only、行↔ペイロード再検証。

### #608 刺激/校正アセット登録簿

新規 `backend/src/htdt/cad_stimulus_registry.py`。

- `StimulusAssetEntry`（`stimulus_sha256` 封印）: 種別
  （origin_class 10 値 / subtype 23 値）はラベルに過ぎず同一性を
  構成しない。同一性は `content_sha256`、完全な `StimulusGeneratorSpec`
  （generator id/version、sweep law、帯域、duration、amplitude、
  fade、silence、normalization、生成物 hash）、
  `StochasticRealization`（seeded は seed+rng 必須、
  fixed_realization は realization hash 必須、unseeded_live /
  statistical_profile_only は bit-exact 不可として顕在化）、
  `StimulusLevelSemantics`（digital peak/RMS/crest factor/FS 約束/
  アナログ/音響レベル/校正参照）、`StimulusChannelIdentity`、
  `StimulusMediaFormat`（映像の raster/range/EOTF 含む）、
  `StandardProfileRef`（standard_id+revision+publisher checksum
  +algorithm）、`rights` で構成。origin ごとに必須ブロックを
  validator で強制（file/video/av_sync/immersive/standard_profiled は
  content hash 必須等）。
- `evaluate_stimulus_eligibility`: 手順要件 `ProcedureStimulusRequirement`
  との fail-closed 照合。ゲート順序: 不適合（subtype/origin/標準
  プロファイル欠落 → INCOMPATIBLE、版不一致 → WRONG_REVISION、
  公表 checksum と登録 content hash の不一致 → INCOMPATIBLE「置換
  ファイル」）→ WRONG_SAMPLE_RATE → 帯域カバー →
  WRONG_LEVEL_OR_CREST_FACTOR → content hash 未保有 →
  INSUFFICIENT_EVIDENCE → bit-exact 配送（非 replayable 刺激 →
  INCOMPATIBLE、playback report 無し → INSUFFICIENT_EVIDENCE、
  非透過変換 → TRANSFORMED_NOT_BIT_EXACT、source 検証のみ →
  ELIGIBLE_WITH_LIMITATIONS）。検証は 3 段
  （source_asset_known / delivery_config_known /
  delivered_signal_verified）— ソース同一性と配送信号の検証を分離。
- `StimulusMeasurementPin`（`pin_sha256` 封印）: 測定/データセット
  ↔ 登録刺激を不変に結合。`stimulus_claim_allowed` は未 pin の測定に
  「校正済み」等の主張を `(False, 'claim_blocked_no_stimulus_pin…')`
  で fail-closed。
- 永続化 `cad_stimulus_registry_repository.py`:
  `cad_stimulus_assets`（content_sha256 索引付き —
  `lookup_by_content_hash` が同バイトの別名登録を検出）/
  `cad_stimulus_pins` / `cad_stimulus_eligibility` テーブル、
  append-only、pin は登録済み asset のみ解決。

### 統合 / UI

- schema v23 → v24: 上記 5 テーブル + 索引、`_migrate_23_to_24`、
  `NATIVE_SCHEMA_TABLES`、`_ROW_BINDINGS`（NULL 許容列は optional
  bind）、`_ReplayProbe`×5（`stimulus_registry` / `bass_qualification`
  リポジトリファクトリ追加）、`_LIFECYCLE_TABLE_LABELS` JA 5 件、
  テスト台帳に v24 行。
- `measurement_evidence_display.py`: 判定/スコープ/失敗理由/ピンの
  JA ラベルと行生成（Qt 非依存でテスト可能）。
- `measurement_page_workspace.py`: 品質詳細パネルに刺激ピン行を追加
  — 未ピンは「未登録（結ばれていません）」と正直表示、読込失敗は
  黙らない別行。

## 文献根拠

- 位相整合/相殺: メインとサブを個別測定→整合→和を再測定する手順、
  λ/2 経路差によるスプライス相殺（Dirac Bass Control / Trinnov
  実務）。相殺判定は宣言閾値 −6 dB（既定値、呼び出し側で宣言可能）。
- クロスオーバー実務: THX 80 Hz・LP 24 dB/oct + HP 12 dB/oct、
  CEDIA/CTA-RP22 の転送低域 ≈80–120 Hz 帯、LFE は独立信号経路
  （Dolby の LFE≠転送バス）。LFE 帯域内 +10 dB 余裕を headroom 集計に
  反映（kind='lfe' を別荷重として記録、軸混在は level_axis で防止）。
- 刺激: AES75-2023 Music-Noise 公式アセットは発行者 checksum を
  持ち、置換を検出可能（標準 profile + publisher checksum で検証）。
  IEC 60268-16:2020+COR1:2025 は男性音声スペクトルが 2011 版と異なる
  → `WRONG_REVISION` は専用判定。ESS/Farina 流儀で
  duration/amplitude/fade を生成同一性に含める。IEC 60268-21:2018
  「測定は正確な入力条件と不可分」— pin が無ければ校正主張を
  fail-closed。AES17-2020 のレベル表記（dBFS 約束の明示）。
- 非定常ノイズ（unseeded_live / statistical_profile_only）は
  bit-exact 再現不能として分類し、bit-exact 要件との組合せを
  INCOMPATIBLE に限定 — 固定 realization/seeded 再生のみが
  同一性を結べる。

## テスト

`backend/tests/test_rev56_bassstim.py` 41 件（STIM10–61 / BMQ10–91 /
UI ラベル）: 封印往復、append-only 競合、行改竄検出、版違い/
SR/帯域/crest/透過性の各判定、置換検出、未登録刺激の claim 阻止、
相殺検出と polarity/delay/phase 帰属、予測-実測乖離の
device_state_mismatch、scope cap（holdout 相殺、nominal 宣言）、
double_bass / lfe_routing_error、profile hash 結合。
`test_cad_schema*` / `test_native_row_integrity` /
`test_native_authority_audit` / `test_authority_audit_coverage` /
`test_cad_bass_management` / REV56-DECISION・MEASEV 回帰 グリーン。

## 残存事項

- UI は読み出し最小配線（品質詳細のピン行 + JA ラベル）。登録/
  評価のダイアログは未実装 — `StimulusProfileDialog` 流の拡張が次段。
- `evaluate_bass_management_qualification` は外部から渡される
  証拠集合を信用する — 測定キャンペーン権威との自動接続
  （pin 済み証拠のみ受理する等の強化）はフォロー課題。
- 実 DSP/AVR への適用・デプロイ検証リードバックはスコープ外
  （`ObservedDeployState` の宣言/再測定モデルのみ）。
- Music-Noise の公式 checksum 値は fixture 相当のプレースホルダ —
  実アセット登録時に AES 公表値を登録する運用が前提。
- #569（multi-sub 最適化）とは組成のみ（dest group 参照）— 最適化器
  本体の変更なし。#533（active LF control）は独立のまま。
