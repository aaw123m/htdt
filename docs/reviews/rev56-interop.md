# REV56-INTEROP — openBIM IFC 相互運用 + CEDIA RP1 Performance Facts 取込

スコープ: issues #578 (P1), #586 (P1)
ブランチ: `devin/1791225826-rev56-interop`
スキーマ: native schema v33 → v34（12 テーブル + 3 インデックス追加;
v33 は REV56-OPS が確保済みのため繰り上げ）

## 実装範囲

### #578 IFCImportAuthority — IFC 4.3 import/export

新規 `backend/src/htdt/ifc_step.py`（STEP Part 21 トークナイザ/
パーサ）+ `cad_ifc_interop.py`（解決・権威・差分・評価・export）+
`cad_ifc_repository.py`（append-only 永続化。テーブル
`cad_ifc_import_artifacts` / `cad_ifc_entity_mappings` /
`cad_ifc_revision_deltas` / `cad_ifc_intake_profiles` /
`cad_ifc_intake_evaluations` / `cad_ifc_exports`）。

- **Bounded STEP P21 パーサ**: ファイル 256 MiB / 2M entity /
  引数ネスト深さ 64 の上限、ヘッダ（FILE_DESCRIPTION の
  schema_identifier、FILE_NAME、FILE_SCHEMA）と `#id=TYPE(args);`
  の完全パース。エスケープは `''`・`\\`・`\S\c`・`\X\hh`・
  `\X2\`・`\X4\` を仕様どおり処理。失敗は常に
  `IfcStepParseError` — 部分パース結果を返さない（fail-closed）。
  対応スキーマ宣言は IFC4X3_ADD2 / IFC4X3 / IFC4 / IFC4_ADD2 /
  IFC4_ADD2_TC1 のみ（それ以外は拒否、推測読みしない）。
- **座標忠実度（P0 正しさを P1 機能内で担保）**: 長さ単位は
  IfcSIUnit(prefix,name) / IfcConversionBasedUnit のみ受理して
  メートル換算スケールを宣言として pin。単位未宣言（SI でない・
  変換係数を持たない・単位代入が無い）場合はメートル変換行列を
  **出力しない** — mm↔m の黙示補完を一切行わない
  （`world_transform_m=None` + coverage の
  `unresolved_placement_count` + warning）。軸規約・WCS 原点・
  真北ベクトル・ジオリファレンス（MapConversion /
  ProjectedCRS 存在時）は `IfcCoordinateAuthority` に
  宣言されたまま保持し、欠落は declared/undeclared/partial
  の状態として正直に出す。`world_transform_source_units` は常に
  ソース単位系で保持し、二段階で honest に使い分ける。
- **プレースメント解決**: IfcLocalPlacement の親チェーンを
  PlacementRelTo まで遡り Axis2Placement2D/3D を合成。サイクルは
  `IfcInteropError`、解決不能なら identity 代替ではなく
  transform 保留（上記）。IfcBuildingStorey Elevation はチェーンに
  明示的な placement が無い場合の補助としてのみ使い、記録する。
- **意味マッピング**（issue の語彙そのまま）: IfcSpace→
  `room_candidate`、IfcWall/IfcSlab/IfcRoof/IfcWallStandardCase
  →`boundary`、IfcDoor/IfcWindow→`opening`、IfcCovering→
  `finish_hint`、IfcFurnishingElement→`obstacle`、
  IfcBuildingElementProxy→`generic_unknown`。開口は
  RelVoidsElement（opening）→ RelFillsElement（door/window
  filling）でリンクして記録。RelAggregates で project→site→
  building→storey の空間骨格を復元し、RelContainedInSpatialStructure
  で要素を階層へ割当。
- **材料はヒントのみ**: IfcMaterialLayerSet / IfcMaterialLayer /
  RelAssociatesMaterial から層名・材料名・層厚（ソース単位）を
  抽出するが、これは construction identity であって音響真実では
  ない — 吸収係数・散乱を合成するコードパスは存在しない。
  IfcPropertySingleValue（RelDefinesByProperties 経由）は
  name/nominal/unit を逐語保持し、未対応の property 表現
  （complex/list/bounded/table）は `dropped_property_count` に数えて
  warning に出す。
- **Honest coverage report**: `IfcImportCoverage` に
  entity_total / mapped / spatial / unresolved_placement /
  unhandled_entity_types（型別カウント）/ unresolved_representation_items /
  dropped_property_count / relation_counts / warnings を収集。
  読み捨てたものは黙らない。
- **リビジョン差分**: GlobalId の安定識別で unchanged /
  geometry_changed / semantics_changed / added / removed /
  ambiguous_rebind を分類し、プロジェクトレベルの座標権威差異を
  `coordinate_changed` として検出（意味+音響の波及を needs_review
  に降格）。ambiguous rebind は自動確定せず
  `mark_mapping_reconciliation` の明示レビュー必須。
- **IDS 型取込評価**: `IfcIntakeProfile`（宣言要件集合:
  units_declared / true_north / spatial_skeleton / spaces_present /
  openings_linked / materials_declared / georeference 等）を
  `evaluate_ifc_intake` で検査し、要件ごとに充足/未充足 +
  actionable な missing-data 診断（例 `missing_adjacency`）を返す。
  パーサが落ちる入力も例外で吹き飛ばさず、全要件未充足 +
  `model_resolution_failed` 診断の封印済み評価にする。
- **宣言対称 export**: `build_ifc_export_package` は
  REFERENCE_EXPORT / UPDATE_PROPOSAL / AS_BUILT_HANDOFF の
  3 モードで create-new の STEP テキストを生成（project/units/
  site→building→storey 骨格/space + 任意閉プロファイルの
  SweptSolid + Pset_HTDT_Provenance）。source artifact のメートル
  変換が無い space、extruded-area-solid を持たない space、非空間
  entity、材料層、科学的配列（RIR/FR 証拠）は STEP に書き込まず、
  理由つきで `unexported_items` に列挙 — 出せるものと出せない
  ものを分離。`step_sha256` はリポジトリ保存時に実ファイルと
  照合される。
- **封印・永続化**: `IfcImportArtifact`(`iia:`) /
  `IfcEntityMapping`(`iem:`) / `IfcRevisionDelta`(`ird:`) /
  `IfcIntakeProfile`(`iip:`) / `IfcIntakeEvaluation`(`iie:`) /
  `IfcExportPackage`(`ixp:`) は canonical-JSON 封印済み
  frozen モデル。リポジトリは mapping→artifact、delta→両 artifact、
  evaluation→profile、export→artifact の親 sha 存在を強制し、
  同一 id の異ハッシュを ConflictError で拒否。export 保存は
  `step_sha256` の再計算検証まで行う。
- **UI 最小配線**: 12 テーブルの JA ライフサイクルラベル +
  監査 `_ReplayProbe` 12 本のみ。IFC 内部構造を通常編集 UI に
  露出しない（issue #578 §15）。

### #586 RP1IngestionProfile — CEDIA RP1 Performance Facts 取込

新規 `cad_performance_facts.py` + `cad_performance_facts_repository.py`
（`cad_performance_fact_profiles` / `cad_performance_fact_products` /
`cad_performance_facts` / `cad_performance_fact_imports` /
`cad_performance_fact_evaluations` / `cad_performance_fact_rebinds`）。

- **プロファイル識別**: `PerformanceFactsProfile`(`pfp:`) は
  publisher / family（rp1_1_loudspeakers 等 RP1 ファミリ列挙 +
  other_declared）/ document_reference / document_revision /
  maturity_state（industry_review / final_published / revised /
  withdrawn / superseded / unknown）/ field_mapping_version /
  content hash を封印。industry review ドラフトは final を名乗れない。
- **製品識別は variant を合併しない**: `ManufacturerProductIdentity`(`mfp:`)
  は manufacturer/model/variant_identifiers/source_document_ref 等を
  保持し、同一モデルの別バリアントは別レコード。
- **宣言権威としてのファクト**: `PerformanceFact`(`pff:`) は
  quantity_kind（frequency_response / sensitivity / directivity /
  max_output / compression / distortion / impedance /
  power_handling / thermal_limit / amplifier_output / dsp_capability /
  channel_capability / video_display / projector_performance /
  screen_optical / screen_acoustic / other_declared）と
  evidence_class（rp1_profiled_manufacturer_data /
  manufacturer_datasheet / independent_lab /
  standardized_open_dataset / htdt_measured / user_entered /
  derived / unknown）を分離 — メーカー宣言は宣言のまま、測定に
  昇格しない。value は numeric/banded/text/enumerated/curve_ref/
  not_reported の kind 整合性をバリデータで強制。
- **試験条件を値と不可分に**: `PerformanceFactConditions`
  （自由空間/バッフル/距離/入力電力帯/帯域/窓・時間/空間平均/
  温度/歪率基準/ブランク判定等）は全 optional だが、
  `evaluate_fact_sufficiency` が spec.required_conditions または
  quantity 既定の必須条件を点検し、欠落は
  engineering_grade→limited→insufficient に正直降格
  （「100 W/ch と文脈なし」は amplifier capability として不完全）。
  test_method は publisher/number/revision を逐語保持し
  リライトしない。
- **import 記録**: `PerformanceFactsImport`(`pfi:`) は
  extraction_state（complete/partial/failed）+ 宣言フィールドの
  ディスポジションを保持。宣言外の source field は捨てずに
  verbatim 保持 + warning。
- **適合評価**: `evaluate_product_suitability` が requirement
  （output_at_distance / bandwidth / amplifier_capability /
  directivity_compatible / physical_envelope / generic）ごとに
  eligible / eligible_with_limitations / insufficient_data /
  incompatible の verdict + reason_codes を出す。衝突ソースは
  共存（上書きなし）— `reconcile_conflicting_facts` は
  「共存している」ビューを返し、解消は明示オペレータ決定のみ。
- **レビュー→最終は履歴保持**: `PerformanceFactsProfileRebind`(`pfr:`)
  は from≠to を強制し、旧プロファイルの全宣言フィールドの
  disposition（carried/remapped/dropped/superseded）が揃わないと
  封印しない — 再バインドで値を黙って落とさない。
- **RP22 供給**: `rp22_consumption_view` はファクトを
  RP22ParameterObservation（basis は nominal_spec_only /
  modelled_with_output_limits にキャップ、evidence_class は常に
  design_prediction）に射影 — commissioning 証拠として昇格しない。

## 文献根拠

- **IFC 4.3 / ISO 16739-1:2024** — IFC4X3_ADD2 スキーマ識別子を受理
  セットの先頭に。IfcProjectUnits/IfcUnitAssignment による
  長さ単位宣言、IfcGeometricRepresentationContext の WCS/真北、
  IfcMapConversion+IfcProjectedCRS のジオリファレンスは全て
  宣言として pin。属性 index は EXPRESS 定義どおり（UnitsInContext
  =8, RelatedElements=4/RelatingStructure=5, NominalValue=2/Unit=3,
  IfcBuildingStorey Elevation=9, IfcMaterialLayer LayerThickness=1
  等）。IfcSpace 11 属性・IfcExtrudedAreaSolid（SweptArea/Position/
  ExtrudedDirection/Depth）・IfcArbitraryClosedProfileDef も同様。
  GlobalId は base64 様 22 文字（`0-9A-Za-z_$`・先頭は数字）を
  圧縮/展開。
- **IFC import 実務の落とし穴** — 単位混在（mm↔m の黙示補完）、
  原点/真北の未宣言、ローカル配置チェーン、材質→音響値の無断
  プリセット化。これらを「推測しない・保留する・数えて出す」方針で
  構造化した。
- **CEDIA RP1** — Performance Facts はメーカー標準エンジニアリング
  データの統一様式。RP1-1 (loudspeakers) が CEDIA Expo 2025 時点で
  industry review 版であり、最終スキーマを捏造しない —
  maturity_state と profile 再バインドでレビュー→公開の履歴を保持
  する設計にした（issue #586 の要求どおり source/revision driven）。

## テスト

`backend/tests/test_rev56_interop.py`（28 テスト）:
BIM10-80（単位 pin・transform 保留・配置合成・開口リンク・材料
ヒント・coverage・delta 分類・intake 診断・export 往復・封印検証）
+ RP1F10-60（profile hash・variant 分離・条件完全性・verdict・
rebind disposition・衝突共存・RP22 cap・リポジトリ封印/改竄拒否）。
scoped pytest: test_rev56_interop + test_cad_schema +
test_cad_schema_ddl_contract + test_native_row_integrity +
test_authority_audit_coverage + test_native_authority_audit +
test_rev56_targets = 139 グリーン。

## 残存事項

- 実 BIM オーサリングツール由来の実 IFC ファイルでの往復検証
  （フィクスチャは合成 STEP）。FacetedBrep は面リストまでの
  記述に留め完全トポロジ評価なし。カーテンウォール/複合形状は
  unhandled として数えられるのみ。
- UPDATE_PROPOSAL の上流 BIM への実差分適用（create-new のみ
  実装、issue の first-impl 許容範囲内）。
- IFC 以外の BIM 形式（gbXML 等）、IDS 本体（XML）のネイティブ
  パース — 現行は宣言要件タプルの評価のみ。
- RP1 最終公開版スキーマ確定時の profile rebind、実メーカー
  シート（PDF/表計算）からの抽出器、第三者ラボソース接続。
- export の STEP テキストは自パーサで往復可能なことを確認済みだが、
  第三者 BIM ビューアでの受入検証は未実施。
