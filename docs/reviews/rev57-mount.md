# REV57-MOUNT — AV 取付/構造支持証拠権威

スコープ: issue #620 (P1)
ブランチ: `devin/<ts>-rev57-mount`（PR 作成後に確定、merge-test 経由マージ）
スキーマ: native schema v41 → v42（7 テーブル + インデックス追加 —
REV57-INST が v41 を先取したため連番で v42 に着地）

## 実装範囲

### #620 AV mounting / structural-support evidence authority

新規 `backend/src/htdt/cad_mounting_support_authority.py` +
`cad_mounting_support_repository.py`（テーブル
`cad_mount_assemblies` / `cad_mount_load_evidence` /
`cad_mount_support_elements` / `cad_mount_manufacturer_requirements` /
`cad_mount_structural_approvals` / `cad_mount_inspection_records` /
`cad_mount_qualifications`）。

- `CadMountingAssembly`（mntassy- 封印）— CAD 配置にぶら下がる
  完全な取付アセンブリ: `equipment_ref`（kind='installed_instance'
  強制、sha ピン必須）→ 機器、マウント構成部品リスト
  `CadMountingComponent`（role: equipment_interface /
  bracket_or_frame / fastener_hardware / safety_retention /
  support_point / blocking_or_subframe / pole_or_rig /
  isolation_element … + manufacturer/model/revision +
  `identity_state` = declared / verified_observed / unknown —
  宣言だけのブラケットを実測済と読まない）、`support_method`
  （wall/ceiling/floor/rack/pole/suspended_rigging/
  manufacturer_stand/custom）、`overhead_suspension`、
  `secondary_retention_state`（required_installed /
  required_missing / not_required / unknown — 必須欠落は
  incompatible 理由）、`duty_state`（static / moving_motorized /
  moving_manual / unknown）、`isolation_mount` フラグ、
  `declared_configuration_ids`（一意、メーカー禁止構成と突合）、
  `interference_state`（clear / conflict_observed / unresolved /
  unknown — #614 音響境界条件とは別物の構造干渉）、
  `cable_route_refs`（kind='cable_run' ピン — #597 との結合点、
  垂れ荷重・曲げ半径の計算ではなく証拠ピン）、`installed_pose`。
- `CadMountLoadEvidence`（mntload- 封印）— 需要側証拠:
  mass_kg / weight_n / center_of_gravity / attachment_loads
  （名前一意）/ duty_state / service_load_state / source_class
  （manufacturer_published / approved_document / field_measured /
  installer_declared / assumed / unknown）。少なくとも 1 つの
  需要値が必須 — 「何も測っていない」証拠レコードを作らせない。
- `CadSupportElementRecord`（mntsup- 封印）— 建物側支持要素:
  element_class（structural_steel / concrete / timber /
  blocking_subframe / ceiling_deck / suspended_ceiling_grid /
  wall_framing / baffle_wall_structure / rigging_support_point /
  manufacturer_stand_base / other / unknown）+
  `geometry_ref`（kind 不問だが sha ピン必須 — #578/#613 の
  BIM/scan 幾何を「位置の証拠」として pin 可能）+
  `hidden_condition_state`（verified / inaccessible / unknown —
  「verified + 吊天井グリッド」の組合せはバリデータ拒否:
  グリッドを見て支持構造が見えたことにはならない）+
  `declared_capacity`（CadDeclaredCapacity: 値・単位・証拠
  クラス・文書参照 — HTDT は容量を計算しない、宣言値の
  記録のみ）。IFC 要素型・スキャン外観から容量を推論しない
  （#620 §14 境界を validator で構造強制）。
- `CadManufacturerMountingRequirement`（mntreq- 封印）—
  メーカー取付要件: 承認取付点 / 姿勢制限 / 締結要件 /
  二次保持（required / optional / prohibited / unspecified）/
  禁止構成 ID / 環境・クリアランス制限 / `enclosure_suspension`
  （e1_8_rated / manufacturer_rated / not_rated_for_suspension /
  not_applicable / unknown — `e1_8_rated` は pinned ソース証拠
  必須、E1.8 適格性を推論で与えない）/ `vesa_pattern` +
  `source_ref`/`source_version`。
- `CadStructuralApprovalRecord`（mntappr- 封印）— 承認証拠の
  クラス分離（#620 §4 全 9 クラス）:
  manufacturer_installation_requirement /
  structural_engineer_design / qualified_professional_record /
  local_code_permit_record / rigging_standard_profile（
  `standard_ref` kind='standards_profile' sha ピン必須 — #599
  プロファイル束縛、E1.47-2020 とその改訂案は別プロファイル）/
  installer_declaration / field_inspection / load_proof_test
  （文書または宣言定格必須）/ user_assumed / unknown
  （assumed/unknown は宣言定格を持てない — 推定は証拠にならない）。
  professional 系は approver 同一性必須、permit 系は
  jurisdiction 必須。`approval_scope`（assembly /
  support_element / rigging_system / project / unknown）で
  「プロジェクト許可」と「当該アセンブリの承認」を分離。
  `duty_coverage`（static_only / static_and_dynamic /
  moving_system / unknown）で §10 の動的負荷をカバー範囲として
  記録 — static-only 承認が moving 機器を修飾しない。
- `CadMountingInspectionRecord`（mntinsp- 封印）— 施工/竣工/
  定期検査の竣工証拠: inspection_kind（installation / periodic /
  post_event / commissioning）/ inspector_class（installer /
  qualified_inspector / engineer / other / unknown）/
  支持点・二次保持・締結・姿勢の各観測状態（verified / mismatch /
  absent / not_visible / unknown）+ `inaccessible_points`
  （一意リスト — 写真で見えないアンカーを推論しない）+
  findings（pass / pass_with_notes / findings_open / failed /
  inconclusive）+ `profile_ref`（standards_profile ピン）+
  inspected/next_due（ISO8601、期限は検査より未来必須 —
  §12 のライフサイクル保持、汎用間隔は捏造しない）。
  findings='pass' で mismatch/absent 観測は validator 拒否 —
  「合格だが見えなかった」は構造的に書けない。
- `CadMountingQualification`（mntqual- 封印）—
  `evaluate_mounting_support` が deepest-defect-first の梯子で
  出力する封印 verdict:

  1. `stale_after_change` — substitution_flags
     （equipment_mass_or_cg / mount_or_bracket / support_point /
     orientation / duty_state / equipment_identity、#596 §13 の
     軸）があれば最優先
  2. `manufacturer_mounting_incompatible` — 禁止構成一致 /
     retention required_missing / 吊り機器に not_rated /
     VESA パターン不一致
  3. `support_unknown` — 要素レコードはあるが全て
     element_class='unknown'（記録ゼロは承認梯子へ）
  4. `as_built_mismatch` — 最新検査が pose differs /
     support_point mismatch / failed / inconclusive
  5. `support_capacity_insufficient` — 宣言需要 > 宣言定格
     （単位換算: kg/N/kN/lbf、余裕は `demand_vs_rating_kg`
     として報告のみ、適合計算ではない）
  6. `structural_approval_required` — interference
     conflict_observed / overhead で professional 承認なし
     （installer_declaration は承認にならない）/ 吊りスピーカー
     の 3 層のいずれかが unknown / isolation で professional なし /
     あらゆる承認証拠が未結合
  7. `approved_with_limitations` — duty_uncovered（moving 機器に
     static-only 承認 — MNT70 相当）/ 検査未結合・open・
     inaccessible 含み / 干渉未解消 / 文書クラスのみの承認 /
     equipment_ref 未結合 / overhead で需要証拠なし
  8. `installation_inspection_required` — overhead または
     professional 承認済みだが検査レコードなし
  9. `design_support_evidence_complete` — 上記全て通過時のみ

  吊りスピーカーは 3 層分離（#620 §7）:
  `CadSuspensionLayerStates` = enclosure_capability
  （E1.8/manufacturer 定格 — 製品証拠）× building_support_point
  （吊天井グリッド→incompatible、inaccessible→unknown）×
  field_rigging_assembly（構成部品 + retention 状態）。
  E1.8 筐体証拠が建物支持点承認にならない構造。
  HTDT は構造容量を認証・計算・「safe」バッジ化しない。

## 統合・UI 配線

- `cad_schema_ddl.py`: 7 テーブル + 14 インデックス
  （document_id / 親参照複合）。`cad_schema.py`: v41→v42
  マイグレーション `_migrate_41_to_42` + `NATIVE_SCHEMA_TABLES`
  台帳登録。`test_cad_schema.py` 台帳に `(41, …)` 追加。
- `native_authority_audit.py`: `mounting_support` factory 分岐 +
  7 `_ReplayProbe`（sha 再計算による改竄検出を replay 監査が
  検証）。
- `native_row_integrity.py`: 7 テーブル分の `_ROW_BINDINGS`
  （id/sha + 全バインド列の fail-closed 比較。nullable 参照・
  実数・INTEGER フラグ列は optional バインド）。
- `application_pages.py` `_LIFECYCLE_TABLE_LABELS`: 7 テーブルの
  JA ラベル（取付アセンブリ / 取付荷重証拠 / 構造支持要素 /
  メーカー取付要件 / 構造承認記録 / 取付検査記録 /
  取付支持修飾）。`measurement_evidence_display.py`:
  `mounting_support_state_label`（9 verdict の JA 表示 —
  `support_capacity_insufficient` は「支持容量不足（宣言値）」と
  宣言ベースを明示）+ `suspension_layer_label` +
  `mounting_qualification_line`（verdict + 吊下げ 3 層 +
  宣言定格−宣言荷重の余裕 + 陳腐化軸 + 先頭理由を 1 行で —
  CAD 配置だけでは「取付可能」と読まない）。
- `backend/tests/test_rev57_mount.py`: 23 テスト — MNT10 完全
  証拠チェーン（complete）/ MNT20 CAD のみ天井スピーカー
  （structural_approval_required）/ MNT30 E1.8 のみ吊り
  （3 層分離 + fail-closed）+ グリッド吊り incompatible /
  MNT40 置換陳腐化 / MNT50 隠蔽構造 support_unknown /
  MNT60 免振に professional 要求 / MNT70 動的機器に
  static-only → limitations / 容量不足・検査不一致・干渉
  冲突・検査欠落・文書級のみ承認の各梯子 / バリデータ拒否
  （ref kind・需要必須・グリッド verified 拒否・approver 必須・
  assumed 定格拒否・E1.8 ソース必須・pass+mismatch 拒否・
  封印 tamper）/ リポジトリ ラウンドトリップ + 冪等 + 行改竄
  IntegrityError。

## 文献根拠

- **ANSI E1.56-2026**（ESTA 公開 2026-03-02、2018 版 supersede）—
  恒久施設構造に付く固定リギング支持点の設計・製作・設置・検査・
  文書化の最低要件。支持点を独立要素レコードとして保持し、
  CAD 幾何と構造支持を分離する構造の直接根拠。
- **ANSI E1.8-2018 (R2023)** — 頭上吊り下げ用ラウドスピーカー
  筐体の構造特性・製造管理・試験。筐体/吊り製品の証拠であって
  建物構造や現場付着の証拠ではない — 3 層分離の根拠。
- **ANSI E1.47-2020** — 娯楽リギング検査ガイダンス。2026 公開
  レビュー中の改訂案と別プロファイル（#599 で版管理）。
  検査ライフサイクル（profile/last/next due/findings/qualified
  inspector）の記録構造の根拠 — 汎用間隔は HTDT が捏造しない。
- **AVIXA AV/IT higher-ed ガイドライン 2021** — 落下時に人を
  傷つけ得る機器の取付は構造技術者レビューを推奨。overhead
  吊り下げに professional 承認クラス（engineer/qualified
  professional/permit/proof test）を要求し installer_declaration
  では解消しない設計の根拠。
- **IEBC/I-SEC 系の振動・衝撃・地震考慮 + VESA インターフェース**
  — duty_state/duty_coverage による静的/動的承認範囲の分離、
  `vesa_pattern` の宣言的適合チェック（パターン不一致 →
  manufacturer_mounting_incompatible）。安全率は
  `declared_safety_factor` として記録 — HTDT は係数を計算しない。
- これらは娯楽/AV 参照規格であり住宅建築基準の代替ではない —
  jurisdiction/code_edition は承認レコードの自由記述として
  保持し、HTDT が地域コードを内蔵しない設計。

## 残存事項

- **#596 置換連携の自動化**: `substitution_flags` は評価器の
  入力。`cad_substitution_impact` の affected entry（role=
  `mounting_fit` → disposition → 軸マッピング）から stale フラグを
  自動導出する配線は別タスク（issue の「invalidate via #596」の
  完全結線）。
- **#597 ケーブル干渉の計算**: `cable_route_refs` は証拠ピンのみ。
  垂れ荷重・曲げ半径・経路冲突の幾何計算は cable traceability
  権威側の責務として未結線。
- **検査期限スケジューラ**: `next_due_at_utc` は保持済みだが
  #595 の lifecycle 監視への登録は未実装 — 期限切れを能動的に
  flag するのは別タスク。
- **UI 入力フォーム**: 7 テーブルの証拠入力は repository API
  経由のみ。GUI の取付証拠入力フォームは既存 authority 入力
  パターンに従う別タスク。
- **地域法規プロファイル**: jurisdiction/code_edition は承認
  レコードの記述フィールド。版管理された法規プロファイル権威は
  別 REV スコープ。
- **構造計算の恒久除外**: 荷重計算・アンカー選定・安全率の
  推定は設計上 HTDT の責務外 — 「構造技術者が計算した値」の
  証拠保持のみを行う。
