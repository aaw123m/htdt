# REV57-PROJ — プロジェクター系権威: 空間均一性 + 幾何/マスキング + ハッシュボックス + 光放射安全

スコープ: issues #619 (P1), #622 (P1), #624 (P1), #627 (P1)
ブランチ: `devin/<ts>-rev57-proj`
スキーマ: native schema v37 → v38（17 テーブル + 18 インデックス追加）

## 実装範囲

### #619 Spatial projection-image qualification

新規 `backend/src/htdt/cad_spatial_image_authority.py` +
`cad_spatial_image_repository.py`（テーブル
`cad_spatial_measurement_plans` / `cad_spatial_measurement_sets` /
`cad_spatial_derived_maps` / `cad_spatial_uniformity_evaluations`）。

- `CadSpatialMeasurementPlan`（spplan- 封印）— どこを・何を・どの
  条件で測るかを先に封印。`SpatialGridLayout`（ansi_9_point /
  center_only / multi_point_custom / …）と測定点集合
  `CadSpatialSamplePoint`（center / edge / corner / auxiliary /
  custom ロール + 画素比率座標 + 実 xyz）、対象量
  `SpatialQuantity`（white_luminance / white_chromaticity /
  luminance_uniformity 等 9 種）、測定点ごとの
  `CadMeasurementViewpoint`（席結合・計器位置・スクリーン距離/角度）、
  `CadScreenStateSnapshot`（素材・ゲイン・カーブ・マスキング状態）、
  `CadProjectorOpticalState`（ピクチャーモード・光源モード・
  レンズメモリ・ズーム・レンズシフト・ウォームアップ秒・
  `StabilizationState`）。center_only レイアウトはちょうど
  1 点（center）のみ、ansi_9_point はちょうど 9 点を構造的に要求。
- `CadSpatialMeasurementSet`（spset- 封印）— raw 観測の束。
  `CadSpatialObservation` は point/viewpoint/quantity 一意、
  値には単位必須、色度は x/y ペア必須、計器参照は sha ピン。
  セットは plan の sha をピン — 別プランの観測を差し替えることは
  できない。`SpatialEvidenceKind`（field_measured / predicted）で
  予測と実測を分離。
- `CadSpatialDerivedMap`（spmap- 封印）— ヒートマップ等の派生物。
  source set の sha ピン + 補間アルゴリズム/バージョン/グリッド
  解像度/外挿フラグを必須化 — 派生物が raw を偽装しない。
- `evaluate_spatial_uniformity`（speval- 封印）— 判定基準
  `CadSpatialCriterion`（min_over_max_ratio / center_deviation_pct /
  max_xy_distance × >= / <=）は評価プロファイルが供給 — HTDT は
  許容値を捏造しない。基準未束縛は `criterion_unbound`、観測なしは
  `insufficient_evidence`、中心のみでは `center_only` カバレッジで
  `insufficient_coverage` — 単一中心読みは全域均一性に昇格しない。
  多点あるが要求ロール欠落なら `partial_spatial_coverage` +
  `missing_roles` を明示。安定化要求のあるプランで warming/unknown
  状態なら `insufficient_evidence`。視点ごとの評価は worst-of で
  集約 — どの座席から見ても成立しなければ成立しない。

### #622 Projection image-geometry / masking

新規 `backend/src/htdt/cad_projection_geometry_authority.py` +
`cad_projection_geometry_repository.py`（テーブル
`cad_presentation_geometry_bindings` / `cad_image_geometry_measurements` /
`cad_lens_memory_recalls` / `cad_geometry_evaluations`）。

- `CadRasterStageDeclaration` — content → decoded → scaled →
  raster → projected の 5 段ラスタチェーンを明示。幅・高さは
  ペア必須、単位必須。
- `CadPresentationGeometryBinding`（geobind- 封印）— 提示
  プロファイルと実像の結合: content/projected アスペクト、
  レンズメモリ id・ズーム比・レンズシフト、キーストーン/ワープ/
  アナモフィック状態、プロセッサ scaling モード、マスキング状態。
  `digital_correction_active()` でデジタル補正稼働を派生。
- `CadImageGeometryMeasurement`（geomeas- 封印）—
  `test_pattern_identity` 必須（テストパターン特定なしの幾何
  判断は存在しない — SMPTE RP40/DPROVE 系の実務）。11 幾何量
  （`GeometryQuantity`: aspect / 水平・垂直スケール / 中心オフセット /
  回転 / コーナー誤差 / 台形 / 樽・糸巻歪 / エッジボウ / 辺別
  クロップ / マスキング重なり・隙間）を PASS/FAIL/UNKNOWN/
  NOT_APPLICABLE で読む。`CadCropAttribution` は辺ごとの
  クロップを原因別（source_cropped / scaling_crop / masking /
  keystone 等）に帰属。`CadMaskingEdgeState` は辺ごとの
  overlap_m / gap_m を保持。
- `CadLensMemoryRecallRecord`（lensrec- 封印）— レンズメモリ
  呼出の再現性: メモリ id + ファームウェア + サイクル番号、
  要求 vs 実測位置、位置誤差 + 単位。
- `evaluate_presentation_geometry`（geoeval- 封印）— 11 量
  すべて必須（欠落量は「問題なし」とは読まない）。投写計算だけで
  幾何を推論しない: 測定なし → `insufficient_evidence`。アナモ
  フィック宣言と実アスペクトの矛盾（電子ストレッチ + 等アスペクト
  + 光学補正なし）→ `failed`。FAIL → `failed`。ラスタ消費系
  デジタル補正（digital_keystone / warp_geometric_processing /
  scaling_crop）が有効なら `verified_with_digital_correction` +
  `correction_costs` に実解像度損失の留保を記録 — CAD 上 16:9 で
  も実クロップなら verified と読ませない。UNKNOWN が残れば
  `verified_with_limitations`。physical_alignment は verdict と
  別フィールドで保持。

### #624 Projector hush-box / enclosure co-design

新規 `backend/src/htdt/cad_hushbox_authority.py` +
`cad_hushbox_repository.py`（テーブル
`cad_projector_install_constraints` / `cad_projector_enclosure_plans` /
`cad_enclosure_operating_observations` /
`cad_enclosure_acoustic_observations` /
`cad_enclosure_qualifications`）。

- `CadProjectorInstallConstraints`（pjcons- 封印）— メーカー
  記載の動作域: 温度/湿度/高度/吸排気クリアランス +
  `source_document` + revision + sha。出典なしの制約は存在しない。
- `CadProjectorEnclosurePlan`（hushplan- 封印）— エンクロージャ
  宣言: 気流経路 `CadEnclosureAirPath`（吸気源・排気先・
  `RecirculationState`）、ファン `CadEnclosureFan`（CFM には
  `AirflowBasis` 必須 — free_air_rating と installed_measured を
  区別）、フィルタ `CadEnclosureFilter`（service_state）、光学
  ポート `CadOpticalPort`（透過損失・色度シフト・ゴースティング/
  コントラスト/フォーカス影響; `characterized()` で特性化済みか
  判定）。`remote_projection=True` なら光学ポート必須。
- `CadEnclosureOperatingObservation`（hushobs- 封印）—
  持続シナリオの実測（duration_s>0 必須 — 冷態 5 分ではなく
  温まった複数時間状態）: 吸気/排気/筐体内温度、プロジェクタ
  ファン状態、`ProtectionEvent`（none / throttling / shutdown /
  unknown）、安定到達時間。
- `CadEnclosureAcousticObservation`（hushac- 封印）— 箱なし/
  ありの SPL ペア + `AcousticComparability`（same_state /
  different_state / unknown）— 異なる動作状態の測定を比較
  可能とは読まない。
- `evaluate_enclosure`（hushqual- 封印）— 熱・音響・光学・
  サービスの 4 軸を独立に判定し合成。「静かになった」だけでは
  合格にしない。熱: メーカー制約未提供 → `ventilation_requirement_
  unknown`（HTDT は万能な許容 ΔT を捏造しない）→ verdict
  `insufficient_evidence`。保護イベント発動 → `over_temperature_
  event` → `failed`。吸気温が記載最大超 → `manufacturer_
  constraint_violated` → `failed`。ファン増速・再循環は
  `thermally_limited`。音響: 比較不能 → `not_comparable`;
  post ≥ pre またはファン増速 → `fan_escalation_negates` →
  `failed`。光学: 特性化未了 → `port_uncharacterized` → 限定付き;
  実測劣化 → `port_degrades_image`。サービス: アクセス不明は
  `unknown`、`qualified` は `service_access_documented` のみ。

### #627 Projector optical-radiation safety

新規 `backend/src/htdt/cad_optical_safety_authority.py` +
`cad_optical_safety_repository.py`（テーブル
`cad_projector_safety_identities` / `cad_manufacturer_safety_constraints` /
`cad_projector_placements` / `cad_optical_safety_evaluations`）。

- `CadProjectorSafetyIdentity`（pjsafe- 封印）—
  `risk_group`（IEC 62471-5 の RG0–RG3）と `laser_class`
  （IEC 60825-1 の class_1–4 / not_applicable）を**別フィールド**
  で保持 — 同じ量として混同しない。分類するなら出典必須
  （risk_group_source / laser_class_source）。
- `CadManufacturerSafetyConstraints`（pjscons- 封印）—
  メーカー文書化のハザード距離ルール `CadHazardDistanceRule`
  （適用対象・hazard_distance_m・restricted_below_m）。
- `CadProjectorPlacementDeclaration`（pjplace- 封印）— 配置:
  安全アイデンティティの sha ピン、`OperatingState`（通常/
  低電力/テストパターン整準/service_interlock_defeat/
  service_open_housing/unknown）、投距、レンズアクセサリ、
  マウント向き、`ViewerPosition`、到達可能位置
  `CadAccessiblePosition`（距離・高さ・光路内）。`remote_power_
  capable` は記録されるが安全根拠として一切クレジットしない —
  「IP で消せる」は安全制御ではない。
- `evaluate_optical_safety`（pjseval- 封印）— fail-closed。
  配置が前回判定と変わっていれば `stale_after_change`（旧判定を
  引き継がない）。サービス状態 → `service_state_not_user_safe`。
  アイデンティティ/分類なし → `insufficient_evidence`。メーカー
  制約なし → `local_review_required`（安全距離を即興計算しない
  — SAFETY_REVIEW_REQUIRED 系）。アクセサリに適用ルールなし →
  `lens_accessory_applicability_unknown`。ゾーン判定: 光路内 +
  ハザード距離未満 + （制限高なしまたは高さ未宣言/未満）→
  `safety_zone_conflict`。位置未宣言/不明 →
  `qualified_with_limitations`、全クリアのみ
  `installation_within_documented_constraints`。
  インターロック/保護ハウジングを無効化する案内は出力しない。

## 統合・UI 配線

- `cad_schema_ddl.py`: 17 テーブル + インデックス
  （document_id / 親参照複合）。`cad_schema.py`: v37→v38
  マイグレーション `_migrate_37_to_38` + `NATIVE_SCHEMA_TABLES`
  台帳登録（REV57-PHYS の v37 に連番で後続）。
- `native_authority_audit.py`: 4 factory 分岐（spatial_image /
  projection_geometry / hushbox / optical_safety）+ 17
  `_ReplayProbe`（sha 再計算による改竄検出経路を監査 replay が
  検証）。
- `native_row_integrity.py`: 17 テーブル分の `_ROW_BINDINGS`
  （id/sha + 全バインド列の fail-closed 比較 — 1 bit の書換で
  IntegrityError）。nullable 参照列は optional バインド。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS`: 17 テーブルの
  JA ラベル。`measurement_evidence_display.py`: 各評価の JA 1 行
  表示（`spatial_uniformity_line` / `geometry_evaluation_line` /
  `enclosure_qualification_line` / `optical_safety_line`）—
  UNKNOWN 正直表示、デジタル補正と物理アライメントの分離表示。
- `test_rev57_proj.py`: 49 テスト — レイアウト/バリデーション境界、
  各評価器の fail-closed 遷移、リポジトリ ラウンドトリップ + 行
  改竄 fail-closed 検証（`*IntegrityError`）。

## 文献根拠

- **#619**: IEC 61947-1:2002（ANSI IT7.215/IT7.228 の正統後継）の
  電子投影測定法、IEC 62906-5-1:2021 / ISO 21118:2020 の 9 点
  輝度グリッド → `ansi_9_point` レイアウトと center/edge/corner
  ロール。ISO 26431-1:2008（DCI 系のスクリーン輝度許容）と
  SMPTE ST 431-1:2006 / RP 431-2:2011（レビュー/シアター
  環境）を評価プロファイル側の基準源として想定 — 閾値自体は
  profile-bound で HTDT は発明しない。AVIXA Systems Performance
  Verification Guide の「設置系（projector-room-screen）での
  実測」原則: 予測 vs 実測を `SpatialEvidenceKind` で分離。
- **#622**: SMPTE RP40 / DPROVE 系のテストフィルムによる実像
  検証実務 → `test_pattern_identity` 必須。キーストーン/デジタル
  ワープはラスタを消費し実解像度を損なう — 光学ズーム/レンズ
  シフト（`CorrectionKind` の物理系）と分離し、有効なら
  `correction_costs` に留保を残す。プロジェクターメーカー文書
  （Sony VPL-XW7000ES 設置ガイド、JVC DLA-NZ900 系のレンズ
  メモリ仕様）に基づくレンズメモリ再現性記録。
- **#624**: レーザ光源プロジェクタの冷却要件はメーカー文書化
  値にのみ基づく（Sony/JVC 設置マニュアルの吸排気・温度域・
  クリアランス表）。自由空間ファン CFM と実装気流の非等価は
  `AirflowBasis` で構造的に分離。ハッシュボックス実務は CEDIA
  ショーケース系ケーススタディの共同設計原則（防音・換気・
  光学透過・サービスの同時適格）を採用 — 「減音のみの成功」は
  合格条件に含めない。
- **#627**: IEC 62471-5:2015（画像プロジェクタの光生物学的
  リスクグループ RG0–RG3）と IEC 60825-1:2014（レーザ製品
  クラス）を別分類系として保持。ハザード距離（HD）と制限高は
  メーカー文書化ルールでのみ評価 — HTDT は安全距離を即興導出
  せず `local_review_required` に倒す。RG/クラス不明、配置変更後、
  アクセサリ適用不明はすべて安全側に倒れる。

## 残存事項

- **評価プロファイルの実体**: `evaluation_profile_ref` /
  criterion 供給元となるプロファイル権威（DCI/SMPTE 参照プロ
  ファイル等）との結合は別 REV スコープ — 現状は基準注入
  インターフェースまで。
- **UI フォーム側の入力経路**: プラン/観測/制約の登録は
  repository API 経由。ページ上の入力フォーム生成は既存の
  authority 入力パターンに従う別タスク。
- **派生マップ生成アルゴリズム**: `CadSpatialDerivedMap` の
  provenance は封印するが、補間自体の生成器（グリッド→ヒート
  マップ）は未実装 — 外部ツール成果物の登録経路のみ。
- **#627 zone 判定の自動位置列挙**: 到達可能位置は宣言入力 —
  シーン幾何からの自動候補生成（座席・通路の自動抽出）は将来の
  接続点。
- **レーザ整合系**: `LaserClass` の specific MPE/NOHD 計算は
  HTDT スコープ外 — メーカー/規格文書の値を引用するのみ。
