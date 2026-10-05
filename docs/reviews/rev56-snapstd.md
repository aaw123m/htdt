# REV56-SNAPSTD — デバイス設定スナップショット/復元権威 + 外部規格レジストリ権威

スコープ: issues #592 (P0), #599 (P0)
ブランチ: `devin/1759720800-rev56-snapstd`
スキーマ: native schema v24 → v25（11 テーブル追加）

## 実装範囲

### #592 デバイス設定スナップショット/復元権威

新規 `backend/src/htdt/cad_device_snapshot.py` +
`cad_device_snapshot_repository.py`。全レコード封印済み
（content-derived id + sha256 再計算検証、semantic_payload は sha と
hash 由来 id の双方を除外）。

- `DeviceConfigurationSnapshot`（`devsnap-<sha24>`）: メーカー/機種/
  HW リビジョン/シリアル/ファームウェア/モジュール状態/プリセットと
  設定フィールド群・観測時刻・取得手段・アダプタを pin。
  未観測情報は UNKNOWN（`manufacturer=None` 等は「機種不明」と正直に
  表示）。`state_content_sha256` は識別 pin + フィールドだけを正規化
  digest 化 — 同じデバイス状態の二つのスナップショットはキャプチャ
  時刻が違っても状態同一として差分 0 を示せる。
- 証拠クラス 7 種（`device_readback` / `device_export_backup` /
  `htdt_applied_request` / `user_recorded` / `screenshot_documented` /
  `inferred_from_measurement` / `unknown`）。各クラスは対応する証拠 pin
  を必須化（例: `device_export_backup` は `backup_artifact_sha256`
  必須、`device_readback` はフィールドか観測 pin の一方必須）—
  空の証拠主張はモデルが拒否。
- `parent_snapshot_ids` による append-only な provenance DAG —
  実験ブランチは既知良好を上書きしない（親参照のみ）。リポジトリは
  親が永続化済みでない子を拒否。
- `ObservedDeviceField(field, state, value)` — `observed` 以外の状態は
  値を持てない（「未観測の値」を捏造しない）。
- `diff_snapshots`: 両側にフィールドがある場合のみセマンティック差分
  （フィールド名 + before/after + 状態変化を列挙）、どちらか不透明
  （`fields=()`）なら opaque モードで `state_content_sha256` の一致/
  不一致のみ報告 — 「同じプリセット名だから変化なし」とは絶対に
  言わない。
- `DeviceKnownGoodBaseline`（`devkg-`）+ `evaluate_known_good_eligibility`:
  昇格は検証済み証拠からのみ — `device_readback`/`device_export_backup`
  証拠、バックアップ実物保持、post-verification 合格を要求し、不足は
  理由列挙で不合格。
- `DeviceFirmwareTransition`（`devfw-`）: from/to ファームウェア（from
  は unknown 可、同値は拒否）、根拠・時刻・操作者・リリースノート・
  pre/post スナップショット pin・移行結果・ロールバック状態・再検証が
  必要なドメインを記録。
- `PreUpdateDeclaration` + `evaluate_pre_update_gate` →
  `update_ready` / `update_unverified` / `update_blocked` — 宣言の
  `None` は「未確認」として fail-closed に blocked 扱い。
- `ConfigurationRestoreRecord`（`devrst-`）: `artifact_sha256` または
  `source_snapshot_sha256` のどちらか必須（「何を」復元したか不明な
  復元は登録不可）。verdict は `evaluate_restore_verdict` が証拠から
  導出: `restored_exact_observed_state`（要 success + セマンティック差分
  一致）、`restored_with_differences`、`restore_unverified`、`restore_incompatible`
  （FAIL チェック必須）、`restore_failed`。ベンダーの「Load しました」
  ack は verdict に昇格しない — success かつ差分一致のみ exact。
  モデル validator が verdict↔証拠の整合を再検証（成功＋差分無しで
  exact を名乗る等の虚偽は拒否）。
- `DeviceConfigurationBackupArtifact`（#1055 既存モデル）の永続化 —
  `content_sha256` を行キーとし、同一内容を別識別（artifact_id・
  firmware 等）で再登録するのは conflict（内容アドレス化）。
- `ReplacementDeviceAssessment`（`devrpl-`）+ `ReplacementPortabilityEntry`:
  機器交換時の項目別携行性 7 クラス
  （`portable_to_same_model` / `portable_with_firmware_constraint` /
  `device_instance_bound` / `license_bound` /
  `measurement_reuse_conditional` / `not_portable` / `unknown`）。
  `replacement_blocking_entries`/`replacement_conditional_entries` で
  fail-closed 集約。
- 感度クラス 3 種（`public` / `local_private` / `secret_bearing`）—
  秘密を含む設定値は権威が所有しない。

### #599 外部規格レジストリ権威

新規 `backend/src/htdt/cad_external_standards.py` +
`cad_external_standards_repository.py`。

- `ExternalStandardDocument`（registry key `standard_id@edition`）:
  発行体・規格番号・タイトル・版・発行日・言語・ライフサイクル・
  後継/前身 pin・依存参照・一次/二次 URL・一次ソース tier・確認時刻・
  文書 hash・権利クラス・admission 状態・レビュー方針・備考。封印済み
  （`document_sha`）。`replaced_by`/`supersedes` は自己参照禁止。
  一次 URL を名乗るなら tier 必須。
- ライフサイクル 14 状態（非線形 — 撤回からの復活・確認継続・改定
  進行を表現）: `draft` / `public_review` / `dis_fdis` /
  `industry_review` / `published_current` / `reaffirmed` /
  `under_revision` / `superseded` / `revised` / `withdrawn` /
  `replaced_by` / `historical` / `status_conflict` / `unknown`。
- ソース優先度 6 tier（standards-body catalog > publisher announcement
  > official store > committee/review page > secondary distributor >
  third-party）。`StandardLifecycleObservation`（`stdobs-`）は「この tier
  のこの URL がこの状態を主張していた」を時刻付きで記録 —
  `detect_lifecycle_conflict` は**同一 tier** での不一致のみを真の
  衝突とし（高位 tier が勝つ階層解決）、`effective_lifecycle` は衝突を
  `status_conflict` として返す（隠蔽しない）。
- `StandardProfileMapping`（`stdmap-`）: プロファイル名・マッピング版・
  要件 id・要件本文・入力権威・計算版・出力意味・非対応要件・解釈注記・
  検証証拠 id を pin。
- `StandardsEvaluationPin`（`stdevpin-`）: 評価は文書版 + マッピング版 +
  計算版 + 入力証拠 id + 結果を不変 pin — マッピングは必ず pin 対象の
  standard_id/edition と一致（builder が不一致を拒否）。評価状態 9 種。
- `StandardsRevisionDiff`（`stddiff-`）: 改定差 7 種
  （`scope_changed` / `title_changed` / `normative_added` /
  `normative_removed` / `threshold_changed` / `method_changed` /
  `editorial`）。`replaced_by` は発行体証拠があって初めて記録。
- `standards_profile_capability` → 9 verdict:
  `not_registered` → `draft_research_only`（ドラフトは生産引用不可）→
  `source_ambiguous`（同一 tier 衝突）→ `superseded_historical_only` /
  `unknown_lifecycle` → `retired_for_new_projects` / `limited` /
  `mapping_unvalidated` / `rights_restricted` → `production_eligible`。
- `standard_citation_allowed` — 未登録・ドラフト・状態衝突・死んだ
  ライフサイクルを production 引用しようとするのは fail-closed。
  `affected_evaluation_pins` は上位版が来ても pin を自動移行しない
  （再評価対象の列挙のみ）。
- admission ワークフロー 9 状態: `discovered` →
  `primary_source_confirmed` → `rights_reviewed` →
  `profile_parsed_mapped` → `mapping_reviewed` → `validated` →
  `production_eligible` / `limited` / `retired_for_new_projects`。
- 権利クラス 8 種（`public_metadata_only` / `abstract_allowed` /
  `normative_text_allowed` / `licensed_internal_use` /
  `licensed_redistributable` / `restricted` / `purchase_required` /
  `unknown`）— HTDT は規格本文を所有しない。

### registry seed（実在引用の洗い出し）

`seed_standard_documents()` は repo 内の実引用から ~30 件を登録。
文献検証済みのライフサイクル（webstore/カタログ照合 2026-10-05）:

- `iso-3382-1@2009`: 2021 確認継続、**改定中**（ed.2 は DIS）→
  `under_revision`。ed2 ドラフトは別キー `iso-3382-1@ed2-draft` で
  `draft` + `draft_research_only` — 混同しない。
- `iso-3382-2@2008`: 確認継続 `reaffirmed`。
- `iso-10534-2@1998`: 撤回 → `iso-10534-2@2023`（`replaced_by` pin）。
- `iec-60268-5@2003`: 2026-04-17 撤回 → `iec-60268-21@2018`
  （Sound system equipment Part 21 出力ベース音響測定へ置換）。
- `iec-60268-16@2011` → `iec-60268-16@2020`（ed.4）。
- `ansi-asa-s12-2@2019`: S12.2-2026 に置換済み — ただし
  `cedia-cta-rp22@v1.2` が依存 pin として 2019 版を指す実例として
  「superseded を pin する依存」の正直なモデル（pin 内部では有効、
  新規直接引用は拒否）。
- `avixa-v202-01@2016` ↔ `@2026`: AVIXA リソースページは 2016 を
  現行表示、ストアは 2026 販売中 — tier 階層で superseded に解決する
  一方、両観測を `seed_lifecycle_observations()` で記録（不信を消さない）。
- `iec-61672-1@2013`、`jcgm-100/101/106`（GUM 系）、`ilac-g8@09/2019`
  （判定ルール）、`aes17@2020`、`aes75@2023`、`dolby-atmos-home-guide@r3.1`、
  `auro3d-home-guide@v12` 等。

### 統合・UI

- `cad_schema_ddl.py`: 11 テーブル DDL（外部規格 5 + デバイス 6）+
  索引、`NATIVE_SCHEMA_TABLES` ソート登録、`NATIVE_SCHEMA_VERSION`=25、
  `_migrate_24_to_25`（冪等ベースライン再生）。
- `native_row_integrity.py`: 全テーブルの `_ROW_BINDINGS`（NULL 可列は
  `optional=True`）。`registry_key` は派生 property のため列としては
  結ばない（standard_id+edition で検証）。
- `native_authority_audit.py`: `_build` に両リポジトリ + 11 種の
  `_ReplayProbe`（内容アドレス化された参照先 pin で再取得）。
- `application_pages._LIFECYCLE_TABLE_LABELS`: 11 テーブルの JA 名。
- `measurement_evidence_display.py`: JA ラベル辞書 8 種 +
  `standards_document_line` / `standards_pin_line` /
  `device_snapshot_line` / `restore_record_line` — メーカー/機種/
  ファームウェア欠落は「機種不明」「ファームウェア不明」と正直表示、
  verdict ラベルも JA。

## 文献根拠（web_search 一次情報）

- Trinnov Altitude: セットアップは export/import 可能な独自バックアップ
  ファイル —「内容は不透明・SHA-256 で完全性のみ証明」の
  DeviceConfigurationBackupArtifact モデルの根拠（パースしない）。
- Dirac Live: `.liveproject` プロジェクトファイルは適用結果ではなく
  「適用したい意図」の記録 — project-file ≠ deployed-state 分離の根拠。
- CEDIA/AV 業界ガイダンス: ファームウェア更新前の設定バックアップ必須・
  更新後の全機能再検証 — PreUpdateDeclaration と
  requalification_domains の根拠。
- ISO/IEC webstore・ANSI/ASA・AVIXA ストアの掲載状態を実確認（上記）。
- ILAC-G8:09/2019 判定ルール・GUM JCGM 100/101/106 の現行版確認 —
  判定ルール権威（REV55 系）の pin に対応。

## 残存事項

- **実デバイス接続なし**: adapter 経由の readback/apply は枠組みのみ
  （`htdt_applied_request` 証拠クラスと pre/post スナップショット pin は
  整備済み、実機適用は将来の #1055 adapter 実装に委譲）。「宣言＋検証
  枠組み」のみ — vendor ack で exact を名乗らない設計は意図通り。
- **GUI 配線は最小**: ライフサイクル登録ページ・監査ビューは既存
  lifecycle 表示配線を通じて自動表示されるが、snapshot 一覧専用
  ページ/ダイアログは未実装（authority は measuremt スコープではない
  ため既存パネルへ配線せず）。
- **スナップショット取得の自動化なし**: `user_recorded`/`unknown`
  証拠を使った手動登録 UI は将来課題 — 現状はプログラム経路のみ。
- **規格本文管理外**: 権利クラス通り、規格本文・図版は保持しない。
  改定差は publisher の差分告知を人間が `StandardsRevisionDiff` に
  記録する運用（自動フェッチなし）。
- **backup artifact バイナリ格納外**: 実ファイルはユーザー管理、権威は
  SHA-256 + メタデータのみ（必要に応じて今後 CAS 連携）。
- `evaluation_pin` の一括再評価トリガーは未実装（`affected_evaluation_pins`
  で列挙 → 人間が再評価する運用）。
