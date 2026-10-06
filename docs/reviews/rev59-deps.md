# REV59-DEPS — 権威依存/陳腐化グラフ + 証拠アテステーション/時刻権威 + プロジェクトアーカイブ/移行権威

スコープ: issue #729 (P0) / #725 (P1) / #718 (P1)
ブランチ: `devin/1791281095-rev59-deps`（merge-test 経由マージ）
スキーマ: native schema v48 → v49（12 テーブル + インデックス追加 —
`NATIVE_SCHEMA_TABLES` 登録済み）

設計原則は REV57/REV58 と同じ: すべての権威レコードは
content-addressed な frozen pydantic モデル（`_seal` + sha256 +
semantic id）、append-only リポジトリ（同 sha 再保存は no-op、
カラム改竄は IntegrityError で fail-closed）、評価器は純粋関数で
verdict を新規封印する。

## 実装範囲

### #729 Authority dependency / staleness graph (P0)

`backend/src/htdt/cad_authority_dependency.py` +
`cad_authority_dependency_repository.py`（テーブル
`cad_dependency_edge_declarations` /
`cad_dependency_change_events` /
`cad_dependency_rule_profiles` /
`cad_staleness_assessments` /
`cad_revalidation_plans`）。

- `DependencyEdgeDeclaration`（depedge- 封印）— 型付き依存宣言:
  `used_as_input` / `derived_from` / `calibrated_from` /
  `validated_against` / `registered_to` / `configured_by` /
  `measured_under_state` / `applicable_under_profile` / `assumes` /
  `requires_capability` / `approved_for_use_by` / `supersedes` /
  `references_only`（最後は非影響 — 検査用に保持されるが失効を
  伝搬しない、§3）。汎用 `depends_on` は存在しない。
  `DependencyScope` がエッジの footprint を宣言する:
  `target_fields`（追跡する対象フィールド — scope 外交化は
  エッジを発火しない）、`observables` /
  `retained_observables` +
  `residual_independence='declared'`（能力レベル失効 — 絶対 SPL
  は陳腐化しても相対タイミングは現行に残る）。残差独立性未宣言の
  発火エッジは保守的に全体を陳腐化させる。
- `SemanticChangeEvent`（depevt- 封印）— 変更の意味クラス
  （geometry_material / label_metadata / equipment_definition /
  device_firmware / device_configuration / measurement_state /
  calibration_parameters / standard_profile_revision /
  solver_algorithm / derived_transform / project_metadata /
  approval_scope / other_declared）+ `changed_fields` を宣言 —
  ハッシュだけではラベル改名と幾何変更を区別できない。
- `DependencyRuleProfile`（deprule- 封印）— 版付き失効ルール:
  `(edge kind, change class) → InvalidationEffect`（none /
  recompute / remeasure / readback_required / review_required /
  incompatible / superseded）+ 任意の明示 `RevalidationActionKind`。
  ルールはデータで `ruleset_version` を持つ — ルール変更は過去の
  verdict を暗黙に再解釈しない（verdict は評価時のプロファイルを
  pin）。
- `evaluate_staleness` → `StalenessAssessment`（stale- 封印）:
  - 推移閉包を固定点反復で計算 — 到達パスは
    `StalenessEntry.path`（subject→changed ref の順の ref 列）として
    証跡に残る。
  - パス深刻度は伝搬中に累積: 子孫は依存経路上の最強の制約を
    継承する（§11 — 不適格ソースから再計算された verdict は旧
    PASS を維持しない）。
  - scope 宣言済み残差独立性 → `valid_with_limitations` +
    affected/retained observables 分離。
  - 発火エッジにルール無し → `stale_review_required` +
    `conservative`（安全と裁定できない）。
  - `unknown_subjects`（依存宣言カバレッジ無しの権威）→
    `unknown_dependency` — 沈黙的に現行扱いしない（fail-closed）。
  - `lineage: dict[ref_key → complete|partial|unknown]` —
    部分/不明 lineage は保守判定へ落とす（§16）。
- `build_revalidation_plan` → `RevalidationPlan`（revplan-）—
  最小の再検証作業集合: 各 stale エントリは勝ちルールの要求
  アクション（または規定の state→action 写像）のみ寄与し、
  `(kind, sorted subject set)` で重複排除。`full_recommission` は
  決して既定にならない（§15）。
- `authority_graph.py` (#590) は read 側の lineage 投影であり、
  本権威は persisted 失効エンジン — 衝突なし。
- `authority_revalidation.py` は再封印 lane — 本権威は「何が
  陳腐か」を決める層。

### #725 Evidence attestation / trusted timestamp authority (P1)

`cad_evidence_attestation.py` +
`cad_evidence_attestation_repository.py`（テーブル
`cad_signed_manifests` / `cad_manifest_attestations` /
`cad_attestation_verifications` — 既存 `cad_evidence_attestations`
は #610 bundle 証跡のまま残し、manifest 層と分離）。

- `SignedManifestRecord`（sigman- 封印）— 署名対象の正準
  manifest 同一性: `manifest_sha256`（正準バイト列の digest —
  rendered PDF は副産物であり唯一の機械可読権威ではない、§2/§3）、
  `CanonicalizationSpec`（直列化形式 + canonicalization 手続 +
  版 + 非署名フィールド — 別手続の再直列化は別 manifest）、
  `subject_refs`、`ApprovalScope`（integrity_only /
  design_revision / as_built_observation / measurement_campaign /
  verification_result / equipment_substitution /
  commissioning_handoff / exception_acceptance / other_declared —
  全体署名が全アーティファクトの技術承認を意味しない、§7）、
  `referenced_artifacts`（大容量 raw は digest 参照、埋め込まない、
  §13）。
- `EvidenceAttestation`（attest- 封印）— manifest × signer を
  `AttestationKind` で束縛: `hash_only` / `mac_authenticated` /
  `digitally_signed` /
  `digitally_signed_with_certificate_chain` /
  `trusted_timestamp_only` / `signed_and_trusted_timestamped` /
  `external_signature_reference` / `unknown` — 汎用
  `signed=true` フラグではない（§1）。`SignatureDescriptor` は
  検証側メタデータのみ（algorithm / format / `key_id` /
  `signer_type` / `signature_sha256` / 宣言時刻）— 秘密鍵も署名
  バイト列自体も保存しない（§20）。`TrustedTimestampPin` は
  RFC 3161 型トークンの pin（TSA 身元・トークン digest・刻印
  アルゴリズム・主張時刻）。`attests_attestation_ref` で
  アテステーションの連鎖（attestation の attestation）を
  サポート（§15）。
- `evaluate_attestation` → `AttestationVerification`（attver-）—
  検証 verdict 自体が派生証拠レコード（§10）。fail-closed 規則:
  - attestation が別 manifest identity を pin / マニフェスト
    バイト不一致 → `verification_failed`（redaction/再出力が
    元署名を継承しない、§12）。
  - digest 方針 `unsupported` → `verification_failed`（§4
    algorithm agility）。
  - `hash_only` → `integrity_confirmed`（署名者は永久に主張
    できない）。
  - `trusted_timestamp_only` + token 検証済 → `timestamp_confirmed`
    （存在-時刻証明のみ）。
  - 署名系: 署名未検査 → `attested_unprovened`（検査せず PASS
    しない）；署名検証済み + 信頼タイムスタンプ →
    `attested_verified`；署名検証済み・トークン無し・宣言時刻のみ
    → `attested_verified_declared_time`；署名時点で鍵が失効/
    取消済みだが信頼時刻でアンカー → `attested_verified_historical`
    （§9 — 鍵状態は評価時点ではなく署名時点で問う）。
  - `external_signature_reference` / `unknown` → `unverifiable` —
    輸入アテステーションは通さない。
  - 時刻権威階層: `trusted_timestamp` > `declared_time` >
    `unknown_time`（§8）。

### #718 Project archival / schema-migration authority (P1)

`cad_archive_migration.py` +
`cad_archive_migration_repository.py`（テーブル
`cad_archive_snapshots` / `cad_archive_verifications` /
`cad_migration_records` / `cad_migration_verifications`）。

- `ArchiveSnapshot`（arc- 封印）— アーカイブ単位:
  キャプチャ時 `schema_version`、`content_hash`（fixity）、
  `snapshot_semantic_checksum`（意味同一性 — 「バイト差」と
  「意味差」を分離、§13）、`PreservationScope`
  （full_semantics_and_provenance / evidence_provenance_only /
  measurement_data_only / project_structure_only /
  other_declared — アーカイブが何を保存するかの宣言、§4）、
  `write_procedure` + `declared_readback_procedure` の
  `ProcedurePin`（手続 id + 版 + 構成 — OAIS の表現情報 pin、
  §10）、`prior_archive_ref`（系列リンク）。
- `evaluate_archive` → `ArchiveVerification`（arcver-）—
  宣言済み再読出し検証: 必須 4 検査（container_legible /
  content_hash_match / semantic_checksum_match /
  schema_version_legible）全 pass → `verified_legible`；必須未実行
  → `partially_verified`；fail 1 件 → `verification_failed`；
  検査ゼロ → `unverified`（宣言のみ、§11）。
- `MigrationRecord`（mig- 封印）— 移行同一性:
  `MigrationKind`（schema_upgrade / format_transform /
  storage_transform / other_declared — 移行種別を混同しない、§2）、
  source/target archive pin、from/to schema version、
  `migration_procedure` pin、`comparison_policy_id`+版、
  `FieldChangeIntent` 宣言（renamed/retyped/dropped/added/
  rescaled — 未宣言の意味ドリフトは検証で捕捉、§13）。
  書き込み時ステータスは常に `declared` — 検証は別封印 verdict。
- `evaluate_migration` → `MigrationVerification`（migver-）:
  - 未説明の fail → `verification_failed`。
  - 必須検査（target_readable / content_hash_match /
    referenced_pins_resolve / preservation_scope_complete /
    declared_transformations_applied）未実行 → `unverified` —
    未検証移行は保存を主張しない。
  - semantic checksum 一致 → `verified_equivalent`。
  - checksum 不一致だが宣言・受理済み差分で説明済 →
    `verified_with_declared_differences`（等価ではない）。
- #710 汎用 project export と #720 report package は別 lane —
  本権威は archivability/migration 層として合成。#564 glossary
  意味は `FieldChangeIntent` で明示宣言される（§15）。

## UI / 表示配線

- `application_pages.py` `_LIFECYCLE_TABLE_LABELS` — 12 テーブルの
  JA ラベル（削除プラン列挙が未知名を出さない）。
- `measurement_evidence_display.py` REV59-DEPS 節 —
  staleness/edge-kind/change-class/action、attestation
  kind/state/time-authority、archive/migration status/preservation
  scope の JA ラベル + `staleness_assessment_line` /
  `revalidation_plan_line` / `attestation_verification_line` /
  `archive_verification_line` / `migration_verification_line`。

## 永続化 / 監査統合

- `native_row_integrity.py` `_ROW_BINDINGS` — 12 テーブル全ての
  ミラーカラムをペイロードに束縛；カウントカラムは
  `_list_count` ExtraCheck で `len(payload[..])` 一致を検証。
- `native_authority_audit.py` — `authority_dependency` /
  `evidence_attestation` / `archive_migration` リポジトリ登録 +
  `_REPLAY_PROBES` 12 本（全行の canonical 再読込）。

## テスト

`backend/tests/test_rev59_deps.py` 32 本:

- DEP 系: 厳密失効範囲・推移閉包+経路証跡・references_only
  非伝搬・scope 外不発火・未宣言件 fail-close・ルール無し保守
  レビュー・最小再検証計画・往復+改竄・append-only 衝突・
  label_metadata 非失効・封印ペイロード改竄拒否
- ATT 系: 署名+信頼時刻検証・hash_only 完全性のみ・未検査署名
  unprovened・宣言時刻のみ・履歴的検証・manifest 不一致 fail・
  TSA 単体・外部参照 unverifiable・連鎖・往復+改竄・未支持 digest
  fail-close
- ARC 系: legible/partial/failed/unverified ゲート・等価検証・
  宣言差分検証・未説明差分 fail・必須検査欠落 unverified・
  書込時 declared・往復+改竄・append-only 衝突

scoped pytest: `test_rev59_deps.py` + `test_cad_schema.py` +
`test_cad_schema_ddl_contract.py` + `test_native_row_integrity.py` +
`test_native_authority_audit.py` +
`test_native_authority_audit_hardening.py` +
`test_authority_audit_coverage.py` — 全グリーン。

## 文献根拠

- **依存/陳腐化 (#729)**: Mokhov, Mitchell & Peyton Jones 2018
  *Build Systems à la Carte*（fine-grained 依存追跡と再構築戦略 —
  dirty bit / verifying trace / constructive trace の分類）/
  make の DAG 推移閉包（変更したノードからの逆方向到達可能集合が
  最小再構築集合）/ Adler et al. incremental computation（変更
  伝搬の健全性 — 未宣言依存は保守的失効）/
  capability-scoped invalidation（trace に依存 footprint を載せる
  early cutoff / verifying trace の類比）。
- **アテステーション/時刻権威 (#725)**: RFC 3161（Time-Stamp
  Protocol — TSA トークンは「ある時点以前にデータが存在した」
  ことの証明であり、局所 `created_at` とは別権威）/
  NIST FIPS 186-5（電子署名は署名者を認証するが、周辺の鍵/身元
  保証系に依存 — 暗号的有効性 ≠ 法的効力）/ PKI 証明連鎖
  （X.509）/ in-toto + SLSA supply-chain attestation モデル
  （attestation は subject+predicate+signer の束縛、連鎖で来歴
  を保持）/ RFC 7515 JWS（署名直列化の一形式）。
- **アーカイブ/移行 (#718)**: ISO 14721 OAIS 参照モデル
  （Preservation Description Information — provenance / fixity /
  representation information を対象と一緒に保持）/ Bearman /
  CAMiLEON 系の migration vs emulation 文献（移行は検証下の
  carrier 変更であり、意味保存が主張対象 — バイト同一ではない）/
  format obsolescence リスク管理（schema 版 + 読出し手続 pin が
  「読める」宣言の機械判読形）/ OAIS の ingest/AIP/dissemination
  単位（アーカイブ単位ごとに fixity + 表現情報を封印）。

## 残存事項

- 既存 verdict/evidence 書き出し経路からの `depedge` 宣言自動
  emit、staleness verdict の derive/evaluate ゲートへの実配線
  （本件は権威層 + 評価器 + 永続化まで — 上流 emit は別 issue）。
- 実 TSA/CA 連携（RFC 3161 トークン取得・証明書連鎖検証器）は
  宣言インタフェースのみ — `VerificationInputs` は宣言済み検査
  結果を受ける（HTDT は暗号プリミティブを再実装しない）。
- `attested_unprovened` の下流ゲート連携（レビュー遮断ルールへの
  組み込み）は残件。
- #710 export 経路からの `ArchiveSnapshot` 自動 emit、実 schema
  移行経路からの `MigrationRecord` emit、意味チェックサムの実
  semantic-comparator は残件（宣言 id + 検証判定のみ）。
- DEP/ATT/ARC fixture 群（DEP10-90 / ATT10-90 / ARC10-90）の
  実運用シナリオ再現は宣言 id のみ。
- `cad_evidence_attestations`（#610 bundle 層）との統合移行は
  未着手 — manifest 層は独立に共存。
