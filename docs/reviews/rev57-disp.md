# REV57-DISP レビュー — 直視ディスプレイ動的/空間資格 + 観察者メタメリズム + 視聴環境権威

対象 issue: #625 (Direct-view display dynamic/spatial qualification — APL、ローカルディミング、ABL/ASBL、視角)、#626 (Display observer-metamerism authority — 計器一致色度は全観察者で一致色を意味しない)、#633 (Video viewing-environment authority — ディスプレイ校正と周辺輝度/環境光を分離)。
スキーマ: NATIVE_SCHEMA_VERSION 39 (`_migrate_38_to_39` — 16 テーブル + 12 インデックスを baseline DDL から冪等作成)。REV57-PROJ が先に v38 を取得したため、本スライスは v39 に配置。authority_version は `direct-view-display-1` / `observer-metamerism-1` / `viewing-environment-1`。

## 実装範囲

### cad_direct_view_display.py (#625) — 7 封印レコード + fail-closed 評価

- `DirectViewDisplayState` (`dvs:`) — 実機状態の同一性を構成する全コントロール: manufacturer/model/hardware_revision/panel_technology (`woled`/`qd_oled`/`lcd_va`/`lcd_ips`/`lcd_ads`/`miniled_lcd`/`microled`/`direct_view_led`/`other_*`/`unknown`) / firmware / picture_mode / content_mode (SDR〜Dolby Vision) / backlight_setting / oled_light_setting / local_dimming (`off`/`low`/`medium`/`high`/`auto`/`custom`/`unknown`) / contrast_enhancer / tone_mapping_mode / ambient_light_adaptation / eco_power_state / motion_processing_state / refresh_rate_hz / source・color profile ref。**`unknown_fields` プロパティが未確定の重要コントロールを列挙し、`adaptive_behavior_risk` が ambient adaptation または eco が on/auto/unknown の間成立 — UNKNOWN は決して off に既定しない。**
- `DisplayStimulusContext` (`dst:`) — 刺激同一性 (§2): `field_kind` (windowed/full_field — full-field に 100% 未満 window は拒否、windowed の 100% は拒否)、window_size_percent、patch_position、surround_background_level、**apl_percent**、sequence_id+sequence_order、hold_duration_s、inter_pattern_interval_s、content_kind (static/dynamic/mixed)、eotf、hdr_metadata_state、container、preconditioning_ref。#608 stimulus registry には `stimulus_ref`/`stimulus_sha256` で結合可能。
- `DisplayPhotometricMeasurement` (`dpm:`) — 状態・刺激に pin された測光値。`quantity` 分類: `transient_peak_luminance` / `stabilized_window_luminance` / `sustained_full_field_luminance` / `time_series_luminance` / `black_luminance` / `chromaticity` / `eotf_tracking_point` — **量の種類は measurand の一部であり、transient は sustained に決して昇格しない**。`instrument_floor_cd_m2` + `below_instrument_floor` で計器フロア以下を正直に扱い (「bound / below measurable floor」、決して infinite contrast)、warmup_minutes / prior_stimulus_ref / thermal_telemetry_ref / measurement order で #573 の warm-up・熱・測定順序権威と合成する。
- `DisplayTemporalObservation` (`dto:`) — 時間調光の証拠。`state`: `no_observed_dimming` / `temporal_dimming_observed` / `provider_documented_protection` (**provider_document_ref 必須 — コミュニティ用語 ASBL/TPC/GSR はメーカー・ファームウェア間で転用しない**) / `state_unknown`。経過時間・輝度低下率・領域 (whole_screen/partial_region)・回復状況・観測時の firmware/picture_mode/温度状態を保持。
- `DisplaySpatialMeasurement` (`dsm:`) — 画面座標均一性。`points` は正規化座標の raw 測定 (一意座標強制)。ヒートマップ/補間は派生物であり格納しない — **生測定のみが正史**。`coverage` プロパティが single_point/sparse/grid を正直分類。#619 投影+スクリーン系の均一性権威とは別物。
- `DisplayAngleMeasurement` (`dam:`) — 水平/垂直視角・距離・seat_ref に pin したオフアクシス計測 (輝度・色度・黒・ローカルディミング外観)。#259 viewing envelope と合成する。
- `evaluate_direct_view_qualification` → `DirectViewQualification` (`dvq:`) — claim 種別ごとに fail-closed 判定:
  - `peak_luminance`: transient/stabilized 必要 + 宣言 window size (`WINDOW_SIZE_UNDECLARED`) + **APL スパン ≥2 未満 → `APL_COVERAGE_MISSING` (単一 0% APL でコンテンツ横断のピーク claim 不可 — ABL は状態依存の実測振る舞いであり「校正ドリフト」ではない)**。
  - `sustained_full_field_luminance`: full-field コンテキストの sustained 実測必須。**transient のみ → `unsupported` + `PEAK_PROMOTED_TO_SUSTAINED`**。warmup 未記録 → `WARMUP_STATE_UNKNOWN`; 順序未記録 → `MEASUREMENT_ORDER_UNRECORDED`; 時間調光観測あり → `TEMPORAL_DIMMING_OBSERVED`/`TEMPORAL_PROTECTION_UNRESOLVED`; 自発光/ゾーン機で APL 単一 → `ABL_BEHAVIOR_UNCHARACTERIZED`。
  - `black_level`/`contrast`: フロア値 → `INSTRUMENT_FLOOR_LIMITED` (bounded 報告、決して無限コントラスト); local dimming 有効 → `LOCAL_DIMMING_ACTIVE_UNMEASURED` (全面黒 1 点では実コンテンツの黒を証明しない); 未知 → `LOCAL_DIMMING_STATE_UNKNOWN`; **contrast kind 不一致しか証拠がない → `CONTRAST_KIND_CONFLATED` (full-field/ANSI/local/content/off-axis は決して混同しない)**。
  - `eotf_tracking`: 全点 static のみ → `CONTENT_STATE_MISMATCH` — 静的スイープは動的トーンマッピングを決して証明しない。
  - `uniformity`: grid 未満 → `UNIFORMITY_PARTIAL`。
  - `off_axis_performance`/`multi_seat_consistency`: claim が seat_refs を必須化; 未測定座席 → `SEAT_COVERAGE_INCOMPLETE` + `unverified_seat_refs` に列挙 (**平均スコアで悪い座席を決して隠さない**)。
  - `content_independent_performance`: 常に `unsupported` — 直視ディスプレイにコンテンツ非依存の性能状態は存在しない。
  - `requires_reference_environment` かつ `adaptive_behavior_risk` → `ADAPTIVE_BEHAVIOR_UNDECLARED` で降格。
  - ロールアップ: qualified → qualified_with_limitations → insufficient_evidence → unsupported → conflicting_evidence。

**中核規則**: パネル保護機構の無効化を推奨しない (結果として発生するのみ記録)。ABL/ASBL 等の時間調光は観測またはメーカー文書証拠のみ。IDMS v1.3 は製造現行基準、v1.4 は研究のみ — メソッドはコンプライアンス限界を意味しない。

### cad_observer_metamerism.py (#626) — 5 封印レコード + 3 クラス分離評価

- `SpdBlock`/`SpdSample` — 実測 SPD 証拠 (波長昇順・一意・宣言範囲内・normalization 宣言必須)。`ChromaticityPoint` は x/y 要約のみ — **SPD の代用品ではない**。
- `DisplaySpectralState` (`dss:`) — display_ref + system_kind (`direct_view_emissive`/`direct_view_backlit`/`projected_reflected`/`other`) + ディスプレイ状態・刺激への sha pin + `evidence_class`: `spectroradiometer_measured` は SPD + instrument_ref 必須、`tristimulus_only`/`unknown` は SPD を持てない。**`spectral_evaluable` は真の SPD がある場合のみ真**。
- `ObserverModelProfile` (`omp:`) — 計算プロファイルの正確な同一性: `iec_ts_61966_13_2023_cor1_2025` / `ansi_cta_6035_2026` / `cie_standard_observer_reference` / `cie_2006_cone_fundamentals` / `project_research_model` / …; バージョン種は `revision` 必須。**IEC/CTA プロファイルは `applicable_system_kinds` を direct-view 系に制限 — reflected projection 系への構築は拒否**。
- `ObserverMetamerismEvaluation` (`ome:`) — ペア結果: reference + DUT sha + profile sha を pin。`result_class` で **計器分光不一致 / 標準観察者差 / 観察者メタメリズム不成立** を厳格分離。メトリクス値は単位・意味論必須。
- `PerceptualMatchRecord` (`pmr:`) — 制御されたマッチ記録。`nominal_target` は不変の標準ターゲット、`selected_target` は観察者が選んだずれ — **ユーザーの白オフセットは記録済みデルタであり D65 を再定義しない**。`observer_identity_class` は再現性クラスのみ (single_calibrator/observer_panel_pseudonymous/anonymous_sample) — 健康・遺伝・視覚異常データは保持しない; `observer_count>1` はパネル/サンプルクラス必須。
- `evaluate_observer_metamerism` → `ObserverMetamerismQualification` (`omq:`) — verdicts: profile 不適用 (PROJECTION_OUT_OF_PROFILE_SCOPE) → insufficient_spectral_evidence (x/y のみ → TRISTIMULUS_ONLY_EVIDENCE) → instrument_mismatch_explains → verified_within_profile / verified_with_limitations (**常に `METER_CORRECTION_NOT_OBSERVER_PROOF` を伴う — 計器一致は全観察者の一致を意味しない**; identical SPD ペアは `SAME_SPD_PAIR` で残余リスクが物理的に存在しないことを示す) → unpaired evaluation → `conflicting_evidence` (`UNPAIRED_SCALAR_REJECTED`) → `unverified_residual_risk`。単一観察者マッチ → `SINGLE_OBSERVER_NOT_UNIVERSAL` で verified を降格; 分散記録 → `MULTI_OBSERVER_DISPERSION_PRESENT`。

**中核規則**: 厳格評価には両側の SPD が必須。技術ラベル (量子ドット OLED 等) は SPD 証拠ではない。プロファイルの適用域を超えた評価は構築自体を拒否する — false compliance を記録しない。

### cad_viewing_environment.py (#633) — 4 封印レコード + プロファイル評価 + 比較

- `ViewingEnvironmentObservation` (`veo:`) — 部屋状態の一回の捕捉: `ambient_illuminance_lx` (室内一般照度)、`IncidentLightState` (**画像面への入射照度 — 一般 room lux の代替ではない**)、`SurroundState` (サラウンド輝度・周縁輝度・色度・CCT・**spectral_evidence クラス** — `spectral_measured`/`cct_chromaticity_measured`/`photometric_only`/`manufacturer_declared`/`unknown`; CCT のみで D65 分光一致を主張しない)、バイアス照明状態、壁/天井/床反射率+色、stray_light_sources、ブラインド/ドア、automation_scene_ref、instrument pin。
- `ViewingGeometryObservation` (`vgo:`) — 視聴者/画面幾何: 距離・角度・画角・眼高・seat_ref。`bound` プロパティ = 距離または座席で結合。
- `LightingSceneRecord` (`lsr:`) — 自動化シーン宣言 (movie_dark/intermission/…)。`bound_observation` に実環境観測を pin — 複数の正当な視聴条件が共存し、単一「リファレンス環境」を全シナリオに要求しない。
- `evaluate_viewing_environment` → `ViewingEnvironmentQualification` (`veq:`) — `_PROFILE_REQUIREMENTS` でプロファイル別要件セット: **ITU BT.2166** (surround lum + neutral chromaticity + spectral evidence + periphery lum + incident + no stray + geometry + dark_room)、**BT.2035** (subset)、BT.500 lab/home、project_defined_*、normal_use_scenario (capture-only)。`_HARD_REQUIREMENTS` = 中立色度・迷光なし・暗室 — unmet で `profile_not_met`。`selected_as_requirement=False` または normal_use スコープ → `NOT_REQUESTED_PROFILE` + `PROFILE_SCOPE_NOTE` — **放送プロファイルはメソッドプロファイルであり、住宅の部屋が BT.2166 を満たさないからといって「不良」とは宣言しない**。`prior_change_axes` で環境変化による証拠陳腐化をディスプレイ連鎖と独立に記録。
- `compare_viewing_environments` → `EnvironmentComparability` — ビフォーアフター比較で変更軸を列挙 (ambient/incident/surround lum/periphery/surround chromaticity/bias light/wall reflectance・colour/stray/blind/scene)。surround/stray/wall 変更 → `not_comparable`; その他 → `limited_comparability`。**照明変更と校正変更を分離 — 知覚差をディスプレイ設定のみに帰因しない**。

**中核規則**: ディスプレイ出力状態 ≠ 視聴環境状態。「display calibrated」バッジは `reference viewing condition` を意味しない。環境変化は証拠を独立に陳腐化させる。

### 統合

- `cad_schema_ddl.py`: 16 `CREATE TABLE` + 12 `CREATE INDEX` を `NATIVE_BASELINE_DDL` 末尾へ; `NATIVE_SCHEMA_TABLES` に 16 登録。
- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 39`、`_migrate_38_to_39`、`_MIGRATIONS[39]`。
- `native_row_integrity.py`: `_ROW_BINDINGS` に 16 エントリ (`point_count` は派生列のため未バインド — リポジトリが行読み出し時に `len(points)` で検証)。
- `native_authority_audit.py`: `_RepositoryChain._build` に `direct_view_display`/`observer_metamerism`/`viewing_environment` 遅延ファクトリ + `_ReplayProbe` ×16。
- `application_pages.py`: `_LIFECYCLE_TABLE_LABELS` JA ラベル ×16。
- `measurement_evidence_display.py`: JA label 関数 (`dv_*`/`om_*`/`ve_*`) + `direct_view_qualification_line` / `observer_metamerism_line` / `viewing_environment_line` / `environment_comparability_line`。
- `test_cad_schema.py`: ledger に `(39, 'migrate native schema to v39')`。
- `scripts/issue_verification_manifest.yaml`: #625/#626/#633 に pytest + manual 残件登録。
- 3 append-only repository: seal 検証、冪等再保存、divergent hash 競合拒否、親参照必須 (measurement→state+context、qualification→親 sha 一致)、行読み出し時の payload-vs-mirrored 再検証。

## 文献根拠

### #625 — ディスプレイ測定方法論
- **ICDM IDMS v1.3** (Information Display Measurements Standard, 現行製造版) — window size / APL / surround / 順序の刺激同一性、均一性の生測定点正史化。v1.4 は研究版のみで製造準拠には使用しない。
- **ANSI/CTA-2037-D** — HDR 測定のコンテンツ状態とメタデータの同一性 (static/dynamic/mixed の分離、HDR metadata/EOTF/container)。
- **ITU-R BT.2408-9** — HDR 制作の実践ガイダンス — transient vs sustained 輝度の区別、フルフィールド対ウィンドウ。
- **OLED ABL 挙動** (業界計測文献) — APL 依存の実ピーク輝度: 低 APL ピーク ≠ 高 APL 持続。ABL/ASBL/TPC/GSR は状態依存の測定振る舞い — 「キャリブレーションドリフト」として却下せず、無効化も推奨しない。
- **ローカルディミング** — ゾーン数・ブルーミングがコンテンツ依存の黒を生むため、全面黒 1 点 + active dimming は実コンテンツ黒を証明しない; コントラスト種別 (full-field/ANSI/local/content/off-axis) を区別。
- **視角 (IPS/VA/OLED)** — オフアクシス輝度・色度シフト・局部調光外観は座席単位で測定; 平均で悪席を隠蔽しない。

### #626 — 観察者メタメリズム
- **CIE 1931 標準観察者の限界** — x/y 一致は CIE 2° 標準観察者でのみ同一; SPD が異なる同色対は実観察者でずれる (observer metameric failure)。CIE 2006 cone fundamentals は標準観察者の改良モデルとして taxonomy に収録。
- **IEC TS 61966-13:2023+COR1:2025** — 直視ディスプレイ用観察者メタメリズム計算のピン留めプロファイル; **reflected projection 系への適用を構築拒否**。
- **ANSI/CTA-6035** — ディスプレイ色度観察者マッチングの方法プロファイル; 同じく direct-view 系に限定。
- 3 クラス分離 (計器分光不一致 / 標準観察者差 / 観察者メタメリズム失敗) と、厳格評価に両側 SPD が必須、計器補正済み ≠ 全観察者で知覚一致、white offset は公称ターゲットを変更しない、観察者データは仮名クラスのみ (健康/遺伝情報なし)、複数観察者分布対応。

### #633 — 視聴環境
- **ITU-R BT.2035** — HDTV リファレンス視聴環境: surround luminance・中立色度・照度制限・迷光排除・幾何結合の要件セット。
- **ITU-R BT.500-15** — 主観評価方法 (lab/home): 環境要件をメソッドプロファイルとして表現。
- **ITU-R BT.2166-0** — HDR/SDR クリティカルモニタリング環境: surround/periphery luminance・分光証拠・入射照度・暗室を含む最厳格セット。
- **SMPTE ST 2080-3** — リファレンスビューイング環境 (周辺輝度・分光・幾何) の業界先例。
- **CEDIA RP23 / CEB22** — 住宅向け周辺光・偏光・反射の推奨実践: 住宅プロファイルを「不良判定」に使わず、project_defined_* と normal_use_scenario で共存させる (issue 明記「Do not declare a residential room defective merely because it does not match BT.2166」)。
- 入射照度 ≠ 室内一般照度 (方向と対象が別 — 分離フィールド); 環境変化はディスプレイ校正と独立に証拠を陳腐化。

## 残存事項

- 実機計測取込と #611 instrument 権威 (分光放射計/輝度計) との instrument_sha256 結合 — 各レコードは instrument_ref 欄を持つが計器台帳側の双方向バインドは後続。
- ヒートマップ・補間可視化 (派生物)、パターンデータ配信 (#608 stimulus registry) への stimulus_sha256 相互結合 UI。
- IEC TS 61966-13 / ANSI CTA-6035 / CIE 2006 cone fundamentals の実メトリクス計算実装 (ΔI 等) — 現行はプロファイル同一性とペア結果格納のみ; メトリクス計算器は残件。
- 投影系 (#619) 観察者メタメリズムは別権威で扱うべき (IEC/CTA プロファイル適用外を明示済み)。
- 照明自動化連携 (シーン push)、CEDIA RP23/CEB22 住宅プロファイル要件セットの拡張、#259 geometry survey との seat 結合 UI、UI への環境評価カード配線。
- ABL/ASBL 時系列収集バッチ (time_series_luminance の量産経路)、視角測定姿勢管理ワークフロー。
