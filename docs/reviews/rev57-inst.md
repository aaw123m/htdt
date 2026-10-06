# REV57-INST レビュー — HVAC 音響/気流共同設計 + 再生参照校正 + as-built トリートメント + 触覚/振動権威

対象 issue: #616 (Theater HVAC acoustic/airflow co-design — ダクト減衰・再生ノイズ・換気性能の共同設計)、#618 (Playback reference-calibration authority — テスト信号レベル・チャネルトリム・LFE 入力の分離)、#631 (As-built acoustic-treatment qualification — 設置後の厚さ・空気層・面積が計画と一致することの証明)、#612 (Tactile/seat-vibration authority — シート振動/触感を空気伝播音と分離測定)。

スキーマ: NATIVE_SCHEMA_VERSION 41 (`_migrate_40_to_41` — 16 テーブル + 16 インデックスを baseline DDL から冪等作成)。REV57-AUD が先に v40 を取得したため本スライスは v41 に配置。authority_version は `hvac-1` / `refcal-1` / `tai-1` / `tacvib-1`、evaluation_version は `hvac-eval-1` / `refcal-eval-1` / `tai-eval-1` / `tacvib-eval-1`。

## 実装範囲

### cad_hvac_authority.py (#616) — 4 封印レコード + fail-closed 評価

- `CadVentilationScenario` (`hvacscn:`) — 換気意図の宣言: occupancy、significant heat load、required supply/return flow (l/s)、cooling goal、operating_state (`hvac_off`/`low_speed`/`normal_occupied`/`warmed_projector_load`/`maximum_load`/`balancing_test`/`other`/`unknown`)、requirement_source。**HTDT は建築基準の必要風量を一切推定しない** — シナリオ無しでは `airflow_requirement_unbound` を返す。冷却負荷の計算もしない: 「静かだが換気不足」はそれ自体が失敗で、機器故障リスクは音響好成績に吸収されない。
- `CadHvacPath` (`hvacpath:`) — ダクト/空気経路の明示グラフ。node = `source_fan`/`duct_run`/`elbow_transition`/`branch_takeoff`/`silencer`/`lining`/`terminal_device`/`diffuser_grille`/`fire_smoke_damper`/`room_side_boundary`/`other`、leg = `duct_borne_internal`/`duct_breakout`/`break_in`/`structure_borne`/`cross_talk_airborne`/`cross_talk_structure`/`other`/`unsupported`。両端 node は宣言ノード集合内必須。**`broadband_loss_db` 単スカラーのみの leg は拒否** — 周波数帯域別データか `unsupported` の明示がない定量経路は構築できない (ASHRAE 帯域別減衰/再生ノイズの精神)。`flanking_role`: `suspected_path`/`confirmed_path` は `flanking_target_rooms` 必須、`isolated_from_targets` は `isolation_scenario_ref` (#576 boundary) 推奨。
- `CadHvacComponentEvidence` (`hvaccomp:`) — コンポーネント証拠。`method` = `manufacturer_application_table`/`engineering_model`/`iso_5135_terminal`/`iso_7235_laboratory`/`iso_11820_in_situ`/`field_measurement`。**ISO 7235 は実験室エビデンスであり設置系の真実ではない** — in-situ/field メソッドで flow_rate_lps を持つには `airflow_evidence_class=field_measured` 必須。**サイレンサーは挿入損失のみでは不完全** — 再生ノイズ (regenerated noise) または圧力損失のどちらかが無ければ構築拒否 (ASHRAE の silencer 三要素)。tonal_flags は絞り・ターミナル・ファンのトーン生成を別記録。
- `CadHvacFieldObservation` (`hvacobs:`) — 試運転時の現場読取: 実測 supply/return flow、室内騒音 (weighting + spectrum)、tonal_observed、`rattle_routing` (`rattle_suspected_route_to_589` — **HVAC 経由のラトルは #589 acoustic-rattle 権威に回送し、気流ノイズ証拠に決して吸収しない**)、`balancing_state` (`rebalanced_since_qualification` → 再評価で qualification stale)。
- `evaluate_hvac_path` → `CadHvacQualification` (`hvacqual:`) — 独立軸: `airflow_eligibility` (required 未満 → `under_ventilated` → verdict `ineligible_airflow`)、`acoustic_state` (`iso_11820_in_situ` または測定スペクトル → `contributions_documented`; 7235-only → `lab_evidence_not_installed_truth`; tonal → `tonal_content_unresolved`)、`flanking_state` (`confirmed` → `flanking_path_open` → failed)、`tonal_state`。loose evidence (node に pin されていない証拠) は count しない。

### cad_playback_reference_authority.py (#618) — 4 封印レコード + fail-closed 評価

- `CadReferenceProfile` (`refprof:`) — 校正文書の pin: `smpte_cinema_reference`/`itu_bs775_lfe`/`dolby`/`cedia_rp22`/`device_vendor`/`project_defined`/`diagnostic_only`/`other`、`lifecycle_state` — **`draft_research_only`/`superseded`/`withdrawn` は評価時に raise** (#599 文書ライフサイクル)。`target_spl_dbc` + `lfe_in_band_gain_db` (通常 +10 dB)。`is_84db_informative_note` プロパティで 84 dB の情報的注記を保持 (THX 文書で言及されるも公称ではない)。
- `CadCalibrationStimulus` (`refstim:`) — 試験信号の同一性。`external_registry_asset` は sha-pinned `asset_ref` 必須 (#608 registry)、`external_file` はファイル識別、`device_internal_test` は `device_identity` 必須 — **デバイス内蔵トーンのレベルは文書が無ければ UNKNOWN が既定**。**redirected bass・bass-managed sum・summed subwoofer output はネイティブ LFE チャネルの刺激として流用しない** — `signal_class` で厳密分離。
- `CadChannelCalibrationObservation` (`refobs:`) — 測定 SPL は `weighting`+`time_weighting`+`integration_quantity` (`slow`/`fast`/`integrating` — THX 慣行の slow+C と Leq を混同しない) + `instrument_ref` + `position_ref` を必須化。`redirected_bass`/`summed_subwoofer_output`/`unknown` 信号クラスは `lfe_in_band_gain_db` を持てない。
- `evaluate_reference_calibration` → `CadReferenceCalibrationQualification` (`refqual:`) — 独立軸: stimulus_state (exact / device_internal_documented / **device_internal_undocumented → device_profile_only で頭打ち**)、measurement_state (全観測が semantics 完備か)、lfe_state (**broadband sub-out メーター差 +10 dB は帯域内再生ゲインの証拠ではない** → `meter_delta_only_not_proof`; `redirected_bass_isolated`; in-band 宣言が profile ±0.1 dB で `verified`)、alignment_state (main-channel 観測を profile target と tolerance_db 比較 — **tolerance 宣言なしでは `unverifiable`、HTDT は容差を捏造しない**)、capability_separation_state (**校正済み ≠ 最大能力 ≠ 聴取レベル** — capability 評価に同じ測定を再利用しても分離軸に記録)。

### cad_treatment_asbuilt_authority.py (#631) — 4 封印レコード + fail-closed 評価

- `CadTreatmentInstallSpec` (`taispec:`) — 設計意図の pin。`lab_evidence_class` = `iso_354_specimen`/`iso_11654_rating`/`iso_20189_single_object` は **mounting/specimen condition の宣言必須** (ISO 354/11654/20189 は測定面積・取り付け条件までが仕様)。`acoustic_role` = `absorption`/`diffusion`/`bass_trap`/`early_reflection_control`/`flutter_echo_control`/`hybrid`/`finish_infrastructure`/`unknown`。`custom_built_in_assembly` は `construction_layers_json` 必須 — **職人仕上げやタイル/壁組込みは「製品の仮の porous absorber」に近似せず、層構成の宣言が境界モデル**。
- `CadTreatmentAsBuiltObservation` (`taiobs:`) — パラメータ単位の証拠状態: `field_measured`/`field_observed`/`installer_documented`/`manufacturer_build_spec`/`design_only`/`inferred_from_geometry`/`hidden_unverified`/`unknown`。`field_measured` は値必須。**パネルが見えることは隠れた空気層の証明ではない** — `hidden_unverified` は UNKNOWN に留まる。`substituted=True` は `substitution_evidence` 必須 — **同等吸収率データシートでも置換は設計同一性ではない** (#596 substitute≠equivalent)。
- `CadTreatmentInspection` (`taiinsp:`) — 写真 artifact・verifier 資格・hidden_parameters・調査注記を含む不変の現場記録 (解体しない限り空気層は見えないことを記録)。
- `evaluate_treatment_asbuilt` → `CadTreatmentQualification` (`taieval:`) — 全 `_TREATMENT_PARAMETERS` (thickness/air_gap/area/facing/orientation/placement/backing + product_identity) をパラメータ比較: `spec_value` (数値 spec) または `text_spec` (facing/backing/orientation=pose_json/placement=surface_ref) と観測を照合。deviation → `prediction_validity=stale` → verdict `prediction_stale` (**build-up の変更は既検証 boundary を生きたままにしない**); 未知パラメータは `limited` で降格; `before_after_result='inconsistent_with_expected'` → `incompatible` (**before/after 測定の不一致は構築不整合として扱い、素材識別には使わない** — ISO 20189 単体測定は音色保証ではない)。

### cad_tactile_vibration_authority.py (#612) — 4 封印レコード + fail-closed 評価

- `CadTactilePath` (`tvpath:`) — `bass_signal_source`→`processor_dsp`→`amplifier`→`transducer`→`seat_frame`/`seat_platform`→`contact_surface` の機械チェーン (transducer + seat_platform または contact_surface ノード必須)。**触覚ルーティングは LFE と同じベース信号を共有しても音響 LFE チャネルとは別経路** — #638 actuator device ref で合成。
- `CadTactileVibrationMeasurement` (`tvmeas:`) — `acceleration`/`velocity`/`displacement` の実測 (value+unit 対必須)、`axis` (`x`/`y`/`z`/`composite`/`unknown`)、`contact_point`、**`occupancy_state` (`empty_seat`/`occupied_generic`/`occupied_measured`/`unknown`) は伝達特性の一部** — 空席特性は着席特性の代理ではない、`sensor_evidence_class` (`traceable_calibrated` は #611 sensor_ref 必須; スマホ読みは `diagnostic_relative` に留まる)、`transfer_json` (周波数→振幅/位相マップ — **トランスデューサ駆動 watts は座席実測の代理ではない**)、`tactile_delay_ms` + `timebase_ref` (#609 — **DSP 遅延設定値は物理的同期の証拠ではない**)。
- `CadTactileProfile` (`tvprof:`) — **preference ターゲットと exposure/comfort limit を分離**: `preference_target`/`platform_capability` は制御目標、`exposure_comfort_limit`/`iso_2631_2_2026_building` は超過判定用で feel ターゲットとして使わない。**ISO 2631-1:1997+Amd.1 は現行 (`iso_2631_1_1997_amd1_current`)、ISO/FDIS 2631-1 Ed.3 は `draft_research_only` — ターゲット/リミット値を持てず、発行済みとして引用不可** (#599 lifecycle)。**「理想触感カーブ」や「推奨 tactile 応答」は権威として存在しない** — `project_defined`/`manufacturer`/`research_document`/`preference_target` のみ。
- `evaluate_tactile_vibration` → `CadTactileVibrationQualification` (`tvqual:`) — `transfer_state` (measured/partially_measured/unmeasured — 未測定は `insufficient_evidence`、決して推定しない)、`occupancy_state` (空席のみは限定降格)、`timing_state` (physically_measured/dsp_setting_only/unknown)、`acoustic_side_effect_state` (**触覚駆動由来の構造ラトル・可聴漏れは #589 rattle finding へ回送 — 「触覚良好」は音響副作用を隠さない**)、`building_coupling_state` (**意図する座席振動と建築構造への振動伝搬は独立評価 — ISO 2631-2 building domain**)、`profile_verdict` (preference/exposure 別 — limit 超過は failed、preference 未達は qualified_with_limitations)。**医療・治療の主張は一切しない**。

### 統合

- `cad_schema_ddl.py`: 16 テーブル (`cad_hvac_ventilation_scenarios`/`cad_hvac_path_declarations`/`cad_hvac_component_evidence`/`cad_hvac_field_observations`/`cad_hvac_qualifications`、`cad_ref_cal_profiles`/`cad_ref_cal_stimuli`/`cad_ref_cal_observations`/`cad_ref_cal_qualifications`、`cad_treatment_install_specs`/`cad_treatment_asbuilt_observations`/`cad_treatment_inspections`/`cad_treatment_qualifications`、`cad_tactile_vibration_paths`/`cad_tactile_vibration_measurements`/`cad_tactile_profiles`/`cad_tactile_vibration_qualifications`) + document_id/seq インデックスを `NATIVE_BASELINE_DDL` 末尾に追加、表名を `NATIVE_SCHEMA_TABLES` に登録。
- `cad_schema.py`: v41 への `_migrate_40_to_41` — baseline DDL から冪等作成のみ (データ移行なし、append-only)。
- `native_authority_audit.py`: `_RepositoryChain.repo` に `hvac`/`playback_reference`/`treatment_asbuilt`/`tactile_vibration` ファクトリ + 16 `_ReplayProbe`。
- `native_row_integrity.py`: 16 テーブルの duplicated 列 → payload path バインディング (`_b`)。
- `application_pages.py`: ライフサイクル一覧に JA ラベル 16 件。
- `measurement_evidence_display.py`: `hvac_qualification_line`/`reference_calibration_line`/`treatment_asbuilt_line`/`tactile_vibration_line` (duck-typed、JA 表記)。
- `scripts/issue_verification_manifest.yaml`: 4 issue の pytest+manual チェック登録。
- `backend/tests/test_rev57_inst.py`: 51 テスト (HVAC10–60 / REFCAL10–80 / TAI10–60 / TAC10–80 + repository チェーン・親未持続・偽造拒否)。

## 文献根拠

- **ASHRAE duct design / regenerated noise**: ダクト減衰・再生ノイズ・静圧は帯域別かつ設計点固有 — `broadband_loss_db` 単一スカラーを定量経路として拒否し、`attenuation_band_db_json`/`regenerated_noise_band_db_json`/`insertion_loss_band_db_json` を帯域別 JSON で保持。ISO 5135 (空気端末器)、ISO 7235 (実験室ダクト付き消音測定)、ISO 11820 (現場 in-situ 測定) を `method` で厳格分離 — 7235 実験室値は設置系の真実として昇���しない。
- **ASHRAE Fundamentals / HVAC Applications — ventilation**: 必要風量は占有・負荷・要求根拠の宣言で、HTDT が推定しない。冷却負荷の設計計算は本権威の外 — 不足は `under_ventilated` として音響評価とは独立に記録。
- **SMPTE / THX / ITU BS.775-4**: マルチチャネルの参照較正は信号→デバイス→聴取位置の三段経路。**LFE の +10 dB は帯域内再生ゲインであり broadband サブウーファ出力メーターの差ではない** (BS.775-4 附属書 — LFE チャネルの帯域制限内ゲイン)。内部校正トーンのレベルは文書がなければ UNKNOWN。リダイレクトされたベースは LFE チャネルの別モードであり、native LFE ゲインの証拠として混同しない。チャネル間トリムは profile target ± declared tolerance で判定 — 「AVR の数値表示が合っている」は校正ではない (校正記録の不在で device_profile_only)。
- **ISO 354 / ISO 11654 / ISO 20189**: 吸収係数は試料取り付け条件・面積・端面処理込みの実験室証拠 — `lab_mounting_condition` を必須化し、as-built の設置条件が lab 条件と一致しなくても「実験室不一致」として記録するのみ (設置系が lab 値を保証しない)。before/after 測定の不一致は構築不整合として分離し、素材同定に使わない。
- **ISO 2631-1:1997+Amd.1 / ISO/FDIS 2631-1 Ed.3 / ISO 2631-2:2026**: 全身振動評価の現行版と FDIS ドラフトを lifecycle で分離 — ドラフトの基準値を本番 claim に使わない。building ドメイン (2631-2) と seat/人間ドメイン (2631-1) は別評価 — 座席で許容される振動が建築構造で許容とは限らない。

## 残存事項

- ASHRAE の帯域別ダクト減衰/再生ノイズ係数モデル (矩形/円形ダクト・エルボ・ダンパ類別) の実数化は残件 — 現段階では帯域別 JSON 保持+class 分離まで。
- #576 isolation scenario との `isolation_scenario_ref` 結合 UI、#608 stimulus registry asset_ref 実 pin UI、#611 instrument sensor_ref / #609 timebase_ref の sha pin UI は別タスク (既存 authority 入力パターンに従う)。
- AVR 自動校正 (Audyssey/Dirac) レポートの取り込み、複数シートの触覚集計ビュー (per-seat 保持のまま)、バランシング差分による自動 stale 検出、ISO 2631-1 Ed.3 発行時の lifecycle 昇格フローは残件。
- ページ上の入力フォーム生成 (各 authority の宣言 UI) は既存 authority 入力パターンに従う別タスク — manifest の manual remainder に記録済み。
