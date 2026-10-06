# REV60-COLLABENV レビュー記録 — コラボレーション/承認権威・材質環境/経年適用性

対象 issue: #721 (P1), #776 (P2)
着地: schema v76、新規 15 テーブル、リポジトリ `cad_collaboration_repository` +
`cad_material_condition_repository`、回帰テスト `test_rev60_collabenv.py` 29 件

## 実装権威

### #721 `cad_collaboration.py`

製品境界は `docs/COLLABORATION_PRODUCT_BOUNDARY.md` に従う: リアルタイム/クラウド
共同編集・アカウント・RBAC・CRDT は scope 外。実装したのは**権威記録層** —
誰が・何を・どの系譜で・どの用途向けに承認したかの証跡であり、同期エンジンではない。

- `CollaborationActor` (cact-): 参加者 + 時点の役割。`identity_basis`
  （declared/externally_verified/pseudonymous/unknown）は主張の根拠を記録し、
  役割は capability のみを与える — 資格証明ではない。
- `check_capability` / `require_capability`: `_ROLE_CAPABILITIES` の最小権限
  マップ。installer は `update_asbuilt` 可・`edit_design`/`promote_proposal` 不可、
  observer は `view` のみ、approve は reviewer_approver のみ、client_accept は
  client_stakeholder のみ。未知アクションは fail-closed。
- `RevisionAuthorship` (raut-): 作者 ref・parent/result リビジョン ID・
  `changed_refs`（触れた権威）・`depends_refs`（依存した権威）・`change_scope`
  （declared_refs/whole_document/imported_external）。declared_refs は
  changed_refs 必須 — 宣言不能なら whole_document に倒れる。
- `InformationStateRecord` (ist-): 正準状態梯子 work_in_progress → draft →
  shared_for_coordination → review_required → reviewed →
  approved_for_declared_use → as_installed_observed → commissioned_verified、
  plus rejected/superseded/archived。`evaluate_state_transition` が遷移を検査。
  `approved_for_declared_use` は `declared_use_profile` 必須。**状態はワークフロー
  位置であり技術的正しさではない**。
- `ApprovalRecord` (appr-): `scope_kind`（whole_revision/system_variant/room/
  equipment_substitution/…）でスコープ限定 — 部分承認は漏れない。
  approved/approved_with_exceptions は `permitted_uses` 必須、例外付きは
  `exception_refs` pin 必須、rejected/withdrawn は用途を持てない。
- `evaluate_approval_currency`: current / stale_dependency_changed / superseded /
  never_in_force / subject_missing — 陳腐化した承認も記録として残る。
- `ReviewDecision` (rdec-): #730 ReviewNote と合成 — 決定結果 + 解決理由を pin。
  resolved は `resolution_note` 必須。
- `SiblingDivergence` (sdiv-) + `evaluate_divergence`: 祖先証明 → convergent /
  auto_merge_safe、同 parent + 非重複宣言 → auto_merge_safe、片側が他方の
  depends を変更 → dependency_stale、changed 重複 → authority_conflict（
  overlap refs 必須）、スコープ未宣言 → manual_review_required、親不一致+
  祖先未証明 → rebase_required。**テキストマージ可能でも意味衝突を隠さない**。
- `ConflictResolution` (cres-): keep_left/keep_right/manual_composition は
  result revision pin 必須、defer/abandon は結果なしでも証跡として残る。
- `BranchProposal` (bprp-) + `ProposalPromotion` (pprm-): 提案は parent identity
  を保持し、昇格は親を変更せず新しい authoritative descendant を生成。
- `ClientAcceptance` (cacc-): `accepted_dimensions` は aesthetic/commercial/
  presentation_choice/schedule_cost に閉じ、技術範囲を表現不能化。
  `compose_acceptance_verdict` は常に `client_accepted_non_technical` —
  技術的制限のある代替品は技術的制限のまま残る。
- `CollaborationEvent` (cevt-): append-only 監査イベント。approve/reject/
  conflict_resolution/state_promotion/commissioning_signoff/exception_acceptance
  は actor 必須（責任帰属）。import/export は省略可。

### #776 `cad_material_condition.py`

- `AcousticMaterialConditionState` (amcs-): `context`（source_specimen/installed）
  で試料状態と設置状態を分離。試料側: 調湿温湿度・含水率+測定法・密度/厚さ/
  圧縮・facing/backing・保管履歴・測定法/日付・出所。**unknown は unknown のまま
  — 履歴メーカー曲線に調湿メタデータを捏造しない**。設置側: 設置日・実厚/
  圧縮・キャビティ種別・水分イベント refs・HVAC 曝露・汚染/損傷/facing 変更
  （TriState）・点検 refs。**目視で無傷でも輸送/音響パラメータ不変の証拠ではない**。
  `conditioned` は調湿事実 1 つ以上必須、含水率は測定法必須、コンテキスト間
  フィールド混入は拒否。
- `MaterialDurabilityEvidence` (mdev-): 証拠クラス（long_term_field_measurement /
  accelerated_aging_test / controlled_climate_exposure / before_after_lab_test /
  manufacturer_durability_claim / in_situ_remeasurement / literature_material_class
  / unknown）は**別クラスのまま**保持。曝露ドメイン（温度/RH/時間）を宣言。
  `service_year_mapping_validated=False` が既定 — 加速劣化は検証済み写像が
  明示されない限り耐用年数に換算しない。lab/exposure 系は affected_quantities
  必須（量別の変化）、long-term は duration 必須。
- `MaterialEvidenceApplicability` (mapa-) + `evaluate_material_applicability`:
  verdict 梯子 = directly_applicable / applicable_within_declared_domain /
  applicable_with_unquantified_environmental_limitation /
  remeasurement_recommended / insufficient_durability_evidence /
  unknown_applicability。**室温 RH からの普遍補正係数は存在しない** —
  `consumed_refs` が条件別パラメータ（#615 経年パラメータ等）の消費を pin し、
  証拠適用性が予測 identity に参加する。
- `ReinspectionAssessment` (risp-) + `evaluate_reinspection_need`: 水漏れ/
  HVAC 高湿/垂下圧縮/交換/清掃汚染/定期レビューのトリガ →
  remeasurement_recommended/inspection_recommended/monitor/not_required。
  **暦の一律期限はない**。#595 drift がイベント後に材質証拠を stale/recheck
  できる接続点。

## 既存権威との非重複確認

- `cad_repository.SceneRevision` (#663): リビジョン DAG は既存権威が所有 — 本
  層は authorship/state/approval を pin するだけで履歴を書き換えない。
- `cad_review_note` (#730): コメント本文は ReviewNote が所有 — `ReviewDecision` は
  決定結果と解決を pin し note_refs で束ねる。
- `cad_treatment_asbuilt_authority` (#631): 設置識別は installation_ref で借用。
- `cad_porous_absorber` (#615) / `cad_resonant_treatment` (#704): 条件別パラメータを
  `consumed_refs` で消費可能 — **正規の新品パラメータ記録を上書きしない**。
  共振処理に多孔質劣化想定を類別適用しない。
- `cad_health_drift` (#595): 再点検 verdict は drift 監視のトリガ入力。
- `cad_diagnostic_hypothesis` (#719): 残留差からの材質劣化「自動診断」経路は
  作らない — 劣化は常に hypothesis。
- 空気伝播状態 (#2/#161) と材質条件状態は別物理として分離 — RH 観測は両方に
  影響し得るが証拠モデルは別。

## 文献根拠

- ISO 19650-1:2018 / ISO 19650-2:2018: 情報管理（交換・記録・版管理・整理）、
  status code による許可用途、明示承認セマンティクス — **アーキテクチャ参照のみ**。
  BIM/ISO 適合は主張しない。ISO/DIS 19650 Edition 2 は公開前ドラフトのため
  参照のみ。
- Ando & Kosaka, "Effect of humidity on sound absorption of porous materials",
  Applied Acoustics 3(3), 1970, DOI 10.1016/0003-682X(70)90024-1 — RH が
  適用性変数になり得る根拠。現代材料への普遍補正の根拠には**ならない**。
- "Experimental assessment of the water content influence on thermo-acoustic
  performance of building insulation materials", Construction and Building
  Materials (2018) — 含水率影響は材料依存（天然系はより相関）であり単調劣化
  仮定は成立しない。
- Yang et al., "A Study on the Acoustic Durability of Sound-Absorbing Porous
  Materials", ICSV 2023 — PU フォーム加速熱劣化で吸音率低下・通気抵抗率変化・
  細胞形態変化。
- "Durability of recycled and natural fibrous acoustic materials undergoing
  long-term indoor ageing" (2026) — 多くの繊維系材料は音響特性をほぼ保持、
  天然/混合系が合成系より敏感 — `old = degraded` 単純化への反証。
- ISO 16544:2012 (confirmed 2023): 指定温湿度での平衡含水率 conditioning —
  **音響性能規格ではなく conditioning 文脈のみ**。
- ISO 354:2003 (confirmed 2024): 残響室吸音測定 — 試料/測定条件は元測定
  プロファイルが所有。

## 検証

- `test_rev60_collabenv.py` 29 テスト: COL10–COL80（非重複マージ/同一オブジェクト
  競合/依存陳腐化/部分承認/クライアント非技術軸/オフライン分岐/承認陳腐化/
  監査責任）+ MATENV10–MATENV70（安定材料/湿度敏感試料/RH のみ/水イベント/
  熱劣化消費/加速クラス保持/校正トラップ）+ validator fail-closed +
  repository roundtrip/冪等/tamper 検出 + fresh-migrate 15 テーブル。
- 登録面: NATIVE_SCHEMA_TABLES + DDL 15 テーブル + `_migrate_75_to_76` +
  `_ROW_BINDINGS` 15 + `_RepositoryChain` `collaboration`/`material_condition` +
  `_ReplayProbe`×15 + `application_pages` ラベル 15 +
  `measurement_evidence_display` JA 行 7 + manifest 2 issue。

## 残件

- 権威作成 UI（参加者登録・承認フロー・分岐解決 UI）は後続 — 現状はドメイン
  API + repository + JA 表示行のみ。
- 分岐評価の祖先判定は呼び出し側が `left_ancestor_of_right`/
  `right_ancestor_of_left` で供給 — revision DAG walk の自動解決は後続。
- #719 calibration が残留差を材質劣化 hypothesis として提示する接続は後続
  （現状は hypothesis のみ許可する権威境界）。
- 実機・実利用での承認ワークフロー/再点検運用検証は manifest manual check に
  委譲。
