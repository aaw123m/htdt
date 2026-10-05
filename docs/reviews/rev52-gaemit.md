# REV52-GAEMIT — 能力 emit 残経路 (GA persist + verify-only レーン) 実装

Scope: REV51-WIRE (`docs/reviews/rev51-wire.md`) の残存 emit 事項のうち
A (GA descriptor persist 経路) と B (予測/行列レーン verify-only 永続化)
を実装。C (ハイブリッド provider authority) は検証のうえ構造判断を
記録した。emit 未配線は全て実呼出し点で確認してから配線した (推測なし)。

共通規約ファイル `C:\Users\Administrator\prompts\rev52\common.md` はこの
VM には存在せず回復不能 (REV49-51 と同じ状況) のため、メモリ規約
(merge-test:main・scoped pytest・fail-closed 原則) で代替している。

## A. GA descriptor persist 経路 — 実装済み

REV51-WIRE 記載「persist されれば同一導出で自動 emit」を文字通り実装:
**宣言 emit 点を `save_descriptor` 自体に移した**。これにより GA を
含む全レーンで descriptor 永続化が宣言 emit を兼ね、スクリプト側の
明示 emit は同一内容の deduped no-op になる (契約不変、挙動不変)。

```
宣言 emit (descriptor persist)
  CadAcousticSolverDispatchRepository.save_descriptor
    → (insert or dedupe early-return の後) _emit_declared_capability_manifest
    → build_solver_capability_manifest(
        descriptor, derive_solver_capability_rows(descriptor))
    → save_capability_manifest (manifest_id dedupe → 冪等)
  ※ emit は with closing(self._connect()) ブロックの外 — 同一 DB への
    nested sqlite 接続デッドロックを回避

産出 emit (GA result commit)
  CadDeterministicPathArtifactRepository.save
    → (insert or dedupe early-return の後) _emit_produced_capability_manifest
    → dispatch_repository.get_descriptor(adapter_descriptor_id)
       ※ None または adapter_descriptor_sha256 不一致 → ValueError (fail closed)
    → derive_solver_capability_rows(descriptor,
        produced_observables=(DETERMINISTIC_PATHS_OBSERVABLE,))
       ※ r130a/polyhedral と同じ produced 導出: direct_sound /
         specular_reflection のみ証拠昇格、宣言済み非産出 observable の
         証拠は剥がれ UNSUPPORTED
    → dispatch_repository.save_capability_manifest
```

**宣言 emit と産出 emit は別行** (導出理由文が「no declared ...」対
「no produced ...」で異なり manifest が区別される) — rev51-wire.md
の監査分離設計と同型。`list_capability_manifests` は seq 順で
(宣言, 産出) の2行を返す。

**fail-closed 維持**:

- 証拠なし現象はドメイン理由付き UNSUPPORTED のまま (geometric の
  coherent_phase / edge_diffraction / scattering /
  low_frequency_modal_response は `_DOMAIN_UNSUPPORTED_REASONS` で
  明示記録)。
- produced_observables に宣言外 observable を渡すと ValueError —
  descriptor/result 乖離は記録しない。
- GA artifact の adapter_descriptor_sha256 が persisted descriptor と
  一致しない場合 emit は raise (persist 自体の _validate_artifact と
  同一の fail-closed 境界)。

回帰: `test_rev52_gaemit.py::test_ga_*` (宣言 emit・産出 emit・
冪等再コミット)、`test_solver_capability_manifest.py` の3件の
アサーションを (宣言 + 産出) 2行型に更新。

## B. 予測/行列レーン verify-only 永続化 — 実装済み

REV51-WIRE 記載「予測/行列レーンは verify-only 永続化なし」— 行列
実行結果は `TransferMatrixResultSet` + `MatrixExecutionRun` として
永続化されるが、「永続化済み provider 権威と照合した再検証」の記録は
存在しなかった。measurement quality producer の実績パターン (検証
レコードを authority store に hash-bound で永続化) に倣い、
**新規 DB テーブルを作らず `ExactJsonAuthorityStore` の record として
検証を永続化** する経路を追加した。

```
verify-only 永続化 (実行後)
  PredictionAuthorityLane.persist_matrix_run_verification(run_id)
    → matrix_repository.get_run / get_spec
    → list_result_sets(spec_id) で run.result_set_sha256 一致を走査
       (MatrixExecutionRun は sha256 のみ保持 — 実体はハッシュ照合)
    → cells[].provider_ref から PredictionProviderRef を収集し
      provider_repository.get_provider で永続済み provider を再解決
      (semantic_sha256 一致必須 — fail closed)
    → build_matrix_run_verification(spec, result_set, run, providers)
    → authority_store.put_json('prediction-matrix-run-verification',
        'prediction-matrix-run-verification-1', payload)
      ※ content-addressed → 同一検証の再永続化は同一 ref (冪等)

能力 emit への接続 (昇格)
  PredictionAuthorityLane.promote_provider_via_matrix_run(
      provider_id, verification_ref)
    → read_payload で hash-bound に再検証 (改竄 record は拒否)
    → provider_entry(provider_id) 必須 + coverage_complete 必須
    → envelope→snapshot→request→dispatch 再解決後
      build_r130_low_band_prediction_provider(
        evidence_state='validated',
        evidence_scope='synthetic_fixture',
        validation_authority_ref=verification_ref)
    → provider_repository.save_provider (冪等)
```

**再検証の fail-closed** (`build_matrix_run_verification`):

- spec/result_set/run 三者の id + sha256 不一致 → raise。
- 非終端 run (QUEUED/RUNNING) → raise。非終端 cell (QUEUED/RUNNING/
  STALE) → raise。BLOCKED は正当な未産出記録として verbatim 記録。
- `run.cell_state_counts` を result_set から再計算して一致確認。
- READY/CACHED cell 毎に collect 時と同じ `result_sha256`
  (`_cell_transfer_result_sha256`) と全転送フィールドを再導出し、
  一致しない場合 raise — 永続化済み結果の replay 確認こそが検証。
- provider が cell を産出していない receiver (coverage 不足) は
  `unverified_cells` + `coverage_complete=False` として正直に記録。

**overclaim 抑止**: promotion が昇格するのは `validated` +
`synthetic_fixture` のみ — `owned_room` (実測) は行列検証では主張
しない (`build_r130_low_band_prediction_provider` の invariant が
production+non-owned も拒否)。検証 record を持たない provider /
coverage 不完全 provider / 改竄 record は全て ValueError。

実呼出し点: `PredictionMatrixDialog._run_matrix` は run_matrix 成功後に
`persist_matrix_run_verification` を呼び、失敗は独立の JA エラー文で
status 表示 (実行結果の破壊なし)。

回帰: `test_rev52_gaemit.py::test_matrix_*` — 永続化+hash-bound
replay・candidate→validated/synthetic_fixture 昇格・改竄 record 拒否・
未登録 provider 拒否・ghost-receiver の部分 coverage 記録と昇格拒否。

## C. ハイブリッド provider authority — 検証済み・別設計として記録

REV51-WIRE 記載「ハイブリッドは provider authority model」の現状検証
結果:

**provider authority model は capability emit を既に持つ**。
`HybridPredictionProvider` は `observable_capabilities` (宣言能力行) +
`capability(observable)` を provider record 自体に持ち、
`provider_repository` 永続化で参照可能 — 「能力 emit」の provider 層
部分は既に配線済み。

**行列検証経路には構造的に接続できない**。matrix lane の provider
契約は `LowBandPredictionProvider.receiver_responses` (spec-pinned
receiver 行) に依存するが、hybrid は `absolute_pressure_samples`
(非 pinned の点群) のみ持ち receiver_responses がない。cell 転送の
`result_sha256` 再導出も `receiver_responses[].receiver_id` の照合を
要し、hybrid の任意点群ではそもそも「spec が pin した receiver」との
対応が定義できない。無理に配線すれば spec 意味論 (receiver 行 =
binding sha256 で pin) が崩れるため、matrix verify の対象を
LowBand 系に限定したままとした。

**SolverCapabilityManifest 化には別設計が要る**: manifest は
`adapter_descriptor` (solver lane 宣言) に hash-bind されるが、
hybrid provider は descriptor に束縛されない provider authority
record — descriptor→manifest の導出と別の authority 写像
(provider→capability manifest 導出テーブル + produced 証拠接続) の
設計が必要。REV52 のスコープ (既存 emit 経路の欠落配線) ではなく
新規設計事項として報告する (無理に配線しない方針に従う)。

## 回帰結果

scoped pytest (19 ファイル、`-n auto`):
`test_rev52_gaemit.py`(5 新規), `test_solver_capability_manifest.py`,
`test_rev44_surfaces.py`, `test_cad_prediction_matrix*.py`,
`test_cad_prediction_*.py`, `test_room_prediction*.py`,
`test_cad_geometric_acoustics_*.py`, `test_cad_hybrid_prediction_provider.py`,
`test_prediction_interpretation.py`, `test_prediction_request_identity.py`,
`test_support_matrix_consistency.py`, `test_import_truth_matrix.py`

- 変更関連は全てグリーン。
- `test_support_matrix_consistency.py::test_no_orphan_or_shared_schema_documents`
  は clean HEAD (4ba4b7bf) でも同一失敗 (既存 rot — 'htdt-ingestion-plan-v1'
  schema が support matrix に未記載)。REV52 無関係、未対処。

## 残存事項

- hybrid → SolverCapabilityManifest の provider-authority 写像設計
  (上記 C)。次の REV で別設計として扱うべき項目。
- 'htdt-ingestion-plan-v1' の support matrix 記載欠落 (別 rot、
  本 REV の修正対象外として記録)。
