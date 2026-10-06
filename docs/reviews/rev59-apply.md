# REV59-APPLY — デバイス適用トランザクション/ロールバック権威（#723、本セッション実装）

## スコープ

issue #723（P0）: 書込み成功が「デプロイ済み」を意味しない問題を、NETCONF RFC 6241 相当のトランザクション意味論を独自権威として導入（AVR/DSP が NETCONF を実装しているとは仮定しない）。

## 実装（`backend/src/htdt/cad_apply_transaction.py` + repository + v50 スキーマ）

封印済みレコード（全て frozen + canonical sha + `prefix-<digest[:24]>` id）:

- `ApplyCapabilityProfile` (apcap-) — デバイス/アダプタ/ファームウェアごとの変化意味論。能力分類9種（`stage_validate_commit_atomic` … `no_rollback`/`unknown`）。**アトミック能力は `api_documented` または `behavior_observed` の証拠が必須** — 「複数フィールドを受け付ける API」からアトミック性を推論しない
- `DeviceApplyPlan` (applan-) — 変更不能の適用計画。目標状態ハッシュ・意味差分・書込順（全 delta フィールドをカバー必須）・事前状態証拠（6段階）・排他要求・ロールバック戦略・タイムアウト/リトライ・適用後チェック。目標を変えれば別計画（内容アドレスで自然に別 id）
- `ApplyWriteRecord` (apwrite-) — 各書込みステップの正直な結果（acknowledged/rejected/timeout/communication_error/unknown）。`acknowledged` は ack ペイロード sha 必須
- `ApplyVerificationRecord` (apver-) — 適用後 readback 比較。verified/failed/unknown の各バケットは排他
- `RollbackPlan` (rbplan-) / `RollbackExecutionRecord` (rbexec-) — 復元可能/不能フィールドを分離し、**exact 復元は `full_readback_captured`/`readback_plus_backup` + 非復元ゼロ + readback 済み**のみ許可（Trinnov 判例: プリセットバックアップにソース構成は含まれない）
- `DeviceApplyTransaction` (aptxn-) — 計画+能力+ロック+書込み+検証+ロールバックを結ぶライフサイクル封印。`fully_applied` は検証参照必須、`rolled_back` は実行記録必須

評価関数:
- `evaluate_apply_state` — fail-closed 梯子: rollback 実行 pin → rolled_back / 書込みなし → not_started / rejected → apply_failed / 通信断 → unknown_state / 一部 ack → partially_applied / readback 検証なし → insufficient_evidence / 全フィールド readback 検証 → fully_applied。他計画の書込みはカウントしない（plan_ref sha 照合）
- `evaluate_rollback_claim` — 適用前ゲート: 事前証拠の階層で主張可能量を決める（user_recorded_only/unknown では復元を約束しない）
- `evaluate_rollback_outcome` — 計画の claim を超えた exact 復元主張は unknown に降格
- `build_apply_transaction` — capability と plan が別デバイスを指せば拒否

## 永続化・監査配線

- `cad_apply_transaction_repository.py` — 7 テーブルの append-only store（`CadApplyTransactionRepository`）、列改ざん検出（`_assert_sealed` + 列比較）
- schema v50（`_migrate_49_to_50`）、`NATIVE_SCHEMA_TABLES` +7、`_ROW_BINDINGS` +7、`_RepositoryChain._build` `apply_transaction` ブランチ + `_ReplayProbe` ×7、`application_pages` JA ラベル、`measurement_evidence_display` に `apply_transaction_line`/`rollback_claim_line`

## 検証

36 回帰テスト全グリーン（`backend/tests/test_rev59_apply.py`）。スキーマ契約・行完全性・監査プローブ・ページ表示を scoped pytest で確認。

## 残件

実機デバイスへの適用実行・HTDT/外部ロック取得・readback 自動取込はデバイスアダプタ側の実装が残件（測定ウィザード経由で自動化予定）。
