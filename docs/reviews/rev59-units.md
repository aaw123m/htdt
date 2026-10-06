# REV59-UNITS — 型付き物理量/単位権威 + 工学仮定/許容用途台帳 + 知覚関連性/可聴性権威 + 残差診断仮説権威

スコープ: issue #728 (P0) / #730 (P1) / #720 (P1) / #719 (P1)
ブランチ: `devin/1791279230-rev59-units`（merge-test 経由マージ）
スキーマ: native schema v48 → v49（11 テーブル + インデックス追加）

## 実装範囲

### #728 Typed physical-quantity / unit authority

新規 `backend/src/htdt/cad_typed_quantity.py` +
`cad_typed_quantity_repository.py`（テーブル
`cad_typed_quantities` / `cad_quantity_operations`）。

- `CadTypedQuantity`（tqty- 封印）— すべての線形物理量を型化記録:
  - `quantity_kind` (`QuantityKind`, 44 種): セマンティック量同一性
    （#728 §1）。SI 次元を共有しても異なる物理 — gauge vs absolute
    pressure、電気 vs 音響 power、frequency vs angular frequency —
    は別 kind であり決して黙って相互運用しない。
  - `DimensionFamily` (25 種): 単位換算が意味を持つのは同一 family
    内のみ。family は換算を支配し、kind は演算を支配する
    （W は electrical/acoustic/radiometric/thermal を共有するが
    演算は混ぜない）。
  - `value_kind` (`ValueKind`): absolute / difference_delta /
    gauge_relative / normalized / count / declared_other —
    加法の合法性は値の認識論的クラスで決まる（#728 §6/§8）。
  - `RatioSemantics`: duty cycle / efficiency / probability / normalized
    coordinate は数値上同型だが意味別 — 同一性の一部で、
    効率 0.5 と duty 50% は黙って比較されない（#728 §8）。
  - `AngleSemantics`: absolute_heading vs relative_rotation —
    identity として保持し、deg→rad 換算は同一 family 内で許可、
    heading+rotation の演算は拒否。
  - `CadUnitDef` レジストリ（~70 単位）: 各単位に canonical への
    `scale`（値 ×scale = canonical 値）+ `offset`（affine: °C/°F の
    み）。`to_canonical`/`from_canonical` は affine 値を
    `value_kind` が `difference_delta` のとき offset なしスケールに
    切替（ΔT 5°C = ΔT 9°F で +32 を足さない）。
  - `CadSourceObservation`: 元値+元単位+source ref（sha ピン）を
    canonical 値と併記 — 往復で入力文字列を保持。
- `CadQuantityOperation`（tqop- 封印）=
  `evaluate_quantity_operation(...)`: fail-closed 演算判定 —
  - `compare`/`add`/`subtract`: 量 identity（kind + value_kind +
    angle/ratio semantics + declared reference）一致のみ `computed`。
    違えば `incompatible_quantities`。absolute−absolute は同 kind の
    `difference_delta` 導出量を返す（affine kind では宣言済みの
    `_ABS_DIFF_PAIR` に沿って temperature_difference へ）。
  - `multiply`/`divide`: `_PRODUCT_KINDS`/`_QUOTIENT_KINDS` に宣言
    された物理的派生（length×length→area、length/time→velocity、
    volume/time→volume_flow、power/area→illuminance 系 …）のみ
    `computed`。次元整合は必要だが十分ではない — 名付けられた
    派生のみ許可。それ以外は `unlisted_derivation`。
  - `ratio_of`: 同 kind 同 value_kind のみ → ratio 導出量。
    kind を跨ぐ比は作らない。
  - affine 跨ぎ add/subtract: `_ABS_DIFF_PAIR` で宣言された
    absolute↔difference の対のみ — 絶対温度+温度差→絶対温度、
    絶対温度−温度差→絶対温度、温度差−絶対温度→
    `incompatible_quantities`（#728 §6）。
  - `to_logarithmic`: 線形→対数の橋渡しは #691 の型付き dB 権威に
    委譲 — ここでは `requires_log_quantity_bridge` を返して止める。
  - 換算・演算の不成立は理由を `reasons` に JA/EN で保持し、
    値を捏造しない。
- JA 単位ラベル辞書 `UNIT_LABELS_JA` + `unit_ja_label()` —
  表示は値とは独立のレイヤ、値 identity に混入しない。

### #730 Engineering assumption / permissible-use ledger

新規 `backend/src/htdt/cad_assumption_ledger.py` +
`cad_assumption_ledger_repository.py`（テーブル
`cad_engineering_assumptions` / `cad_assumption_resolutions` /
`cad_permissible_use_assessments`）。

- `CadEngineeringAssumption`（asm- 封印）:
  - `assumption_kind` (`AssumptionKind`, 13 種): physical_parameter /
    boundary_condition / geometry_omission / source_model /
    receiver_model / measurement_chain / device_dsp / environment /
    material_generic / scope_exclusion / user_preference_target /
    commercial_project_constraint / other_declared。
    `user_preference_target`・`commercial_project_constraint` は
    「物理がそうである」claim には使えない非物理 kind
    （`_NONPHYSICAL_KINDS`）— measured/declared/reference/derived
    evidence をバリデータが拒否。
  - `evidence_state` (`EvidenceState`, 10 種): measured_observed /
    manufacturer_declared / literature_assumed / project_assumed /
    inferred_derived / unknown は仮定クラス
    （`ASSUMPTION_EVIDENCE_STATES` 6 種 — unknown 入力が
    公称値/仕様表値に化けないよう根拠を必須化）。
    unknown ⇒ value 非持ち、非 unknown ⇒ value 必須。
  - `CadAssumptionValue`: declared_value_label / quantity_ref /
    basis_ref / bounds_label / effective_domain — 値自体ではなく
    「どう扱うか」の宣言。
  - `permissible_uses` (`IntendedUse`, 9 種): exploratory_idea /
    concept_design / schematic_design / design_development /
    construction_documentation / commissioning_evidence /
    claim_or_reporting / rp22_design_evaluation /
    other_declared — 空タプルはバリデータ拒否
    （「どこでも有効」既定値こそ台帳が防ぐ対象、#730 §5）。
  - `supersedes_ref` で改版系譜、`assumption_lineage_issues` が
    ダングリング参照・循環を列挙、`active_assumptions` が
    最新版のみ抽出。
- `CadAssumptionResolution`（asmres- 封印）— 仮定の事後検証:
  resolved_by_measurement / resolved_by_superseding /
  withdrawn_as_wrong / still_assumed。測定解消は証拠 ref 必須。
- `CadPermissibleUseAssessment`（asmpu- 封印）=
  `evaluate_permissible_use(...)`: fail-closed —
  - `_VALIDATION_GRADE_STRICTNESS`: claim_or_reporting=5,
    rp22_design_evaluation=5, commissioning_evidence=4,
    construction_documentation=3, design_development=2,
    schematic_design=1, concept_design=1, exploratory_idea=0。
  - 要求用途が宣言用途の厳格度を超える → `use_blocked` +
    残仮定列挙（例: literature_assumed は concept までで、
    claim/rp22/commissioning には不可）。
  - 使用領域に未解消仮定 → `assumption_blocks_use`; 全解消 or
    用途内 → `use_permitted`（残仮定は reasons に保持）。

### #720 Perceptual relevance / audibility authority

新規 `backend/src/htdt/cad_audibility.py` +
`cad_audibility_repository.py`（テーブル
`cad_perceptual_model_profiles` / `cad_audibility_assessments`）。

- `CadPerceptualModelProfile`（pprof- 封印）:
  - `model_kind` (`PerceptualModelKind`, 10 種): iso226_equal_loudness /
    iso532_loudness / just_noticeable_difference_level /
    jnd_frequency / jnd_timing / spectral_irregularity /
    masking_threshold / reverberance_jnd / other_literature /
    composite。
  - `scope_class` (`ModelScopeClass`): applicable_as_reference /
    with_limitations / outside_scope / research_only。
    ISO 226 プロファイルに `applicable_as_reference` を要求するのは
    pure_tone + free_field_frontal + normal_hearing_18_25 の条件のみ
    （ISO 226:2023 の宣言範囲をバリデータ強制）。ISO 532 loudness は
    `requires_level_calibration=True` 必須。
  - `CadAudibilityThreshold` 群: observable 別に文献値を pin —
    level_db（Bücklein 1981 の 1 kHz 0.2–0.4 dB 系）、
    frequency_relative_jnd、timing_ms、spectral_irregularity_db
    （peak/dip 別）、reverberance_relative_jnd（5–10 % 帯）、
    masking threshold、composite — 出典 ref（sha ピン）必須。
- `CadAudibilityDelta`: 被比較量の差 — observable
  (`AudibilityObservable`, 12 種: spl_level_db / spectral_level_db /
  early_level_db / late_level_db / clarity_c80_db / definition_d50 /
  centre_time_ms / reverberance_t30 / lateral_fraction /
  frequency_response_irregularity / timing_offset_ms /
  other_declared)。
- `CadListeningConditions`: 再生レベル参照（`level_reference_ref`
  sha ピン必須）、空間、帯域、リスナー想定。
- `CadAudibilityAssessment`（aud- 封印）=
  `evaluate_audibility(...)`: fail-closed —
  - プロファイル `outside_scope`/`research_only` →
    `outside_model_scope`
  - 聴取条件とモデル scope 不一致 → `outside_model_scope`
  - level 系で level_reference 未供給 → `insufficient_evidence`
  - observable に対応する閾値なし → `indeterminate`
  - |Δ| ≤ threshold → `not_distinguishable_under_profile` —
    「小さい差」を順位に化けさせない（#720 核心）。
  - |Δ| > threshold → `potentially_audible`（可聴の可能性 —
    「確実に聞こえる」とは言わない）。
- #564 測定差評価との整合: 差分はここで知覚閾値を通す —
  decision rule (#577) がサブ閾値差を採点に使うことを型で塞ぐ。

### #719 Residual diagnostic-hypothesis authority

新規 `backend/src/htdt/cad_diagnostic_hypothesis.py` +
`cad_diagnostic_hypothesis_repository.py`（テーブル
`cad_diagnostic_cases` / `cad_diagnostic_hypotheses` /
`cad_diagnostic_tests` / `cad_diagnostic_verdicts`）。

- `CadDiagnosticCase`（diagcase- 封印）: 症例 — 予測 ref・実測
  ref・`CadResidualSignature`（方向/極性/周波数形/空間パターン/
  時間形/大きさ + symptom refs sha ピン）を束ねる。位置 vs タイミング
  交絡の標識は signature に直接載る。
- `CadDiagnosticHypothesis`（diahyp- 封印）:
  - `cause_family` (`CauseFamily`, 16 種): material_boundary /
    source_model_directivity / receiver_placement /
    registration_pose / measurement_chain / device_dsp /
    environment_condition / model_form_inadequacy /
    solver_convergence / data_processing / nuisance_parameter /
    multiple_faults / operator_error / scope_mismatch /
    other_declared / unknown — モデル形式不備・多重故障は
    第一級原因として型化（残差パターン=材料故障、の短絡を防ぐ）。
  - `required_evidence`: この仮説が confirmed になるのに要する
    証拠の記述（sha ではなく宣言 — 仮説は検証設計を含む）。
  - `discriminating_symptoms`: これを立てたら見えるはずの兆候。
- `CadDiagnosticTest`（diatest- 封印）:
  - `test_kind` (`TestKind`): discrimination_measurement /
    isolation_measurement / loopback_check / controlled_intervention /
    independent_remeasurement / model_counterfactual。
  - `predeclared_prediction` + `executed_at_utc` +
    `result_evidence_ref` — confirmed verdict はこの三点を
    バリデータ必須（事後適合の検定を封印しない）。
  - `controlled_intervention` は `intervention_label` +
    `held_constant_label` 必須（何を動かし何を固定したか）。
  - `outcomes` は仮説 ref sha ピン + supported / contradicted /
    indistinguishable / not_addressed + note。
  - `verdict` (`InterventionVerdict`): confirmed_by_intervention_
    and_remeasurement / supported / inconclusive / contradicted /
    design_flaw / refused。
- `CadDiagnosticVerdict`（diaverdict- 封印）=
  `evaluate_diagnostic_verdict(...)`: fail-closed —
  - `_derive_hypothesis_state` のラダー: contradicted >
    confirmed（controlled_intervention / independent_remeasurement /
    loopback_check / isolation_measurement で
    confirmed_by_intervention_and_remeasurement）>
    test_supported > model_supported（宣言 counterfactual）>
    confounded（indistinguishable）> candidate/not_tested。
  - confirmed 存在 → `root_cause_confirmed_within_declared_scope`
    （宣言範囲内のみ、複数なら多重故障保持）
  - 全仮説 contradicted → `unresolved`（最悪でも best-of-bad を
    選ばない）
  - confounded ≥2 または supported+open 混在 →
    `diagnostically_confounded` + `confounded_hypothesis_ids` 列挙
    （次の試験は「再測定」ではなく「判別」）
  - supported のみ → `supported_but_confounded`（支持 ≠ 原因確認）
  - 何も無ければ `unresolved` / `partially_explained`。
  - `diagnostic_claim_allowed(hypothesis, tests)` —
    confirmed 以外は claim 不可、confirmation 級 evidence の
    レベル（`controlled_intervention_support` /
    `independent_remeasurement_support`）を返す。
- #564 残差エンジンと整合: 残差署名はこの権威の入力であって原因の
  出力ではない — 「パターンが合う＝原因」を型で禁止。

## 統合

- スキーマ v49: `NATIVE_BASELINE_DDL` に 11 CREATE TABLE + INDEX、
  `NATIVE_SCHEMA_TABLES` 登録、`_migrate_48_to_49`、
  `_MIGRATIONS[49]`、`test_cad_schema.py` 台帳に
  `(49, 'migrate native schema to v49')`。
- `native_row_integrity.py` `_ROW_BINDINGS` に 11 テーブル分の
  列↔payload バインド。
- `native_authority_audit.py` に `_RepositoryChain` ファクトリ
  （typed_quantity / assumption_ledger / audibility /
  diagnostic_hypothesis）+ 全 11 テーブルの `_ReplayProbe`。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` に 11 テーブルの
  JA ラベル（型付き物理量 / 物理量演算評価 / 工学仮定 /
  仮定解消記録 / 許容用途評価 / 知覚モデルプロファイル /
  可聴性評価 / 診断ケース / 診断仮説 / 診断試験 / 診断判定）。

## テスト

`backend/tests/test_rev59_units.py`（40 テスト）:
- UNIT 系: 次元換算・family 隔離・affine °C/Δ°C・kind 別電力・
  angle semantics・ratio semantics・演算 fail-closed・演算結果の
  型・リポジトリ往復+改竄検出・sha/ID バリデータ
- ASM 系: evidence_state ルール（unknown ⇒ no value）、非物理 kind
  の evidence 拒否、permissible-use 厳格度ラダー、claim/rp22/
  commissioning ブロック、supersedes 系譜・dangling/cycle 検出、
  resolution 測定 ref 必須、往復+改竄
- PER 系: ISO 226 scope 適用・ISO 532 level calibration 必須・
  閾値内→not_distinguishable・閾値外→potentially_audible・
  scope/条件不一致・閾値なし→indeterminate・level ref 必須・
  往復+改竄
- DIA 系: 原因 family 多様性・intervention 宣言必須・事前宣言
  予測必須・確認ラダー・交絡・多重故障・全矛盾→unresolved・
  claim 許可ゲート・往復+改竄
- 横断: v49 スキーマ台帳・`NATIVE_SCHEMA_TABLES` 登録・
  row-integrity binding 完全性・audit probe 網羅

scoped pytest: `test_rev59_units.py` 40 本 +
`test_cad_schema.py` + `test_cad_schema_ddl_contract.py` +
`test_native_row_integrity.py` + `test_authority_audit_coverage.py` +
`test_native_authority_audit.py` + `test_native_authority_audit_hardening.py` +
`test_application_pages.py` + `test_authority_lifecycle_integrity.py`
— 全グリーン。

## 文献根拠

- **型付き量 (#728)**: BIPM SI Brochure 9th ed.（基本量とその単位 —
  「値 = 数値 × 単位」の枠組みと、dB 等の対数量は参照付き別体系）/
  ISO 80000-1:2022（量・単位・次元の規則; 量は名前と記号で型化、
  単位系は換算可能な family として管理）/ F# units-of-measure
  （型レベルの単位検査は演算を fail-closed にする先行例）/
  #691 typed-dB との境界: 線形→対数の換算は dB 権威に委譲、
  こちらは線形量のみ保持。
- **仮定台帳 (#730)**: NASA-STD-7009B（models & simulations の
  許容用途・既知限界の宣言）/ aleatory vs epistemic 不確かさの区別
  （#604 伝播と整合 — こちらは epistemic 側、設計中の「よく分かって
  いない」の明示）/ engineering assumptions are claims about the
  world and carry scope（#730 issue 本文）。
- **可聴性 (#720)**: ISO 226:2023（等ラウドネス — 純音・自由場・
  正面・18–25 歳正常聴力の宣言範囲を型で強制）/ ISO 532-1/2:2017
  （Zwicker/Moore loudness — レベル校正必須）/ Bücklein 1981 JAES
  29(3)（ゲイン/レベル JND: 1 kHz 0.2–0.4 dB 系）/ Mäkivirta 2003
  JAES 51(5)（音響パラメータ JND 合成）/ Elliott/Holland/Newell
  AES 57th 2015（RT/可聴差の範囲）/ Bistafa & Bradley JASA 108(4)
  2000（早/後期エネルギーの差異閾限）/ masking / difference limen
  — 測定差が閾値内なら順位に載せない。
- **診断仮説 (#719)**: NIST Engineering Statistics Handbook
  §pmd44（残差診断: パターン→原因ではなくパターン→仮説→検証）/
  Beaton & Xiang JASA 141(6) 2017 DOI 10.1121/1.4983301（空間残差
  パターンからのモデル形式診断）/ Yang/Dong/Wu JCP 545 (2026)
  114469（診断的仮説検証の枠組み）/ abduction→deduction→induction
  の検証ループ — 仮説生成と確認を分離。

## 残存事項

- 型付き量を既存演算パス（HVAC・機器 API・CAD）へ段階移行 —
  現状は authority 層 + repository のみで、上流の float 使用箇所は
  未改修。
- #691 dB 権威との bridge 演算子の実装（`to_logarithmic` は
  `requires_log_quantity_bridge` で止めるのみ）。
- 知覚モデルプロファイルの標準プリセット供給（聴取条件別
  閾値セットは宣言インタフェースのみ — 実測プロファイル自動
  供給は未配線）。
- 診断仮説の UI ワークフロー（仮説→試験→判定の編集 UI 未着手、
  replay/audit は配線済み）。
- assumption ledger と #604 伝播エンジンの直接結合（仮定に紐づく
  不確かさを伝播側でスコープ外扱いする機構）。
- UI 入力フォーム（JA ラベルは配線済み、仮定/仮説/閾値の登録 UI
  は未実装）。
