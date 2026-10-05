# REV51-WIRE — 能力 emit の runtime 配線 + 欠落 help topic 実装

Scope: REV50-SECOND (`docs/reviews/rev50-second.md`) の drift 判断事項のうち
実装可能なものを実装した実行レポート。実装判断は全て実コードの呼出し点を
追って行った (推測なし)。

共通規約ファイル `C:\Users\Administrator\prompts\rev51\common.md` はこの
子セッション VM には存在せず回復不能 (REV49/REV50 と同じ状況) のため、
メモリ規約 (merge-test:main 規約・scoped pytest・fail-closed 原則) で
代替している。

## A. 能力 emit の runtime 配線

### A-1. SolverCapabilityManifest (9 現象宣言)

REV50-SECOND の指摘どおり、manifest は `test_solver_capability_manifest.py`
からのみ emit され、実実行経路が無かった。実呼出し点の追跡結果と配線:

**emit 経路図**

```
descriptor persist (宣言時 emit)
  scripts/run_r130a_candidate_wave_execution.py::_fixture
    → dispatch_repository.save_descriptor(descriptor)
    → dispatch_repository.save_capability_manifest(
        build_solver_capability_manifest(
          descriptor, derive_solver_capability_rows(descriptor)))
      ※ produced_observables 未指定 = 宣言 observables 全体で導出
      ※ r130a/r130b/r130c/r130d 全レーンがこの fixture を共有

result-commit emit (実結果時 emit)
  PffdtdCandidateWaveExecutor.execute
    → result_repository.save(envelope)
    → self._persist_capability_manifest(
        dispatch, produced_observables=[a.observable for a in artifacts])
  PffdtdPolyhedralCandidateWaveExecutor (acoustic_pffdtd_polyhedral_geometry)
    → base_executor.result_repository.save(result)
    → base_executor._persist_capability_manifest(dispatch, produced=...)
```

**導出規則** (`derive_solver_capability_rows`, cad_solver_capability_manifest.py):

- `_OBSERVABLE_EVIDENCE` テーブル: `{acoustic_domain: {observable:
  {phenomenon: (state, bound|None)}}}` — 宣言 observable が直接証拠を
  与える現象だけを SUPPORTED/BOUNDED に導出する。テーブル外の現象は
  declaration だけでは昇格しない。
- `_DOMAIN_UNSUPPORTED_REASONS`: ドメイン上実現不能な現象
  (geometric の coherent_phase / edge_diffraction / scattering /
  low_frequency_modal_response、wave の portal_region_coupling) は
  明示的なドメイン理由で UNSUPPORTED。
- `produced_observables` 指定時 (result-commit emit):
  - produced ⊆ declared を再検証 — 宣言外を産出した場合は
    descriptor/result 乖離として **fail-closed (raise)**、記録しない。
  - 宣言済みだが産出されなかった observable の証拠は剥がれ、それでしか
    主張できない現象は UNSUPPORTED + "no produced observable on this
    execution evidences ..." で記録される (overclaim しない)。
- SUPPORTED > BOUNDED の最強クレームマージ。BOUNDED の bound 記述は
  `; ` 連結。
- `save_capability_manifest` は同一 semantics なら persisted を返す
  (冪等) — fixture emit と result-commit emit が同一内容になる限り
  重複行は発生しない。produced ⊊ declared の場合は実行固有の
  別 manifest として追記される (監査上は「その実行が実際に根拠づけた
  宣言」として正しい)。

**検証済み「emit 地点でない」経路 (重複配線しない根拠)**

- `PredictionAuthorityLane` (cad_prediction_registration.py): verify-only
  の resolver lane で solver authority を永続化しない → manifest emit
  地点ではない。
- GA executor 群 (deterministic-path / late-field / stochastic-ray):
  dispatch/descriptor を read するだけで GA descriptor を persist する
  runtime 経路が存在しない → GA manifest は GA descriptor が永続化された
  時点で同一導出テーブル経由で自動的に emit 可能 (residual)。
- ハイブリッド provider: 独自の READY/UNSUPPORTED 判定テーブル
  (provider authority model) で manifest とは別権威。

### A-2. AuralizationCapability + routing + review package

`materialize_measured_auralization` は spec/artifact を永続化するが、
routing/capability/package は呼ばれていなかった。`cad_auralization_service.py`
に emit 経路を追加:

```
materialize_measured_review_package(repo, artifact=, ir_dataset=, label=, created_at_utc=)
  → repo.get_render_spec(artifact.spec_id)     (persisted spec 必須)
  → repo.read_artifact_wav(artifact)           (digest 再検証済み bytes)
  → build_measured_stem_routing(spec, artifact)
       mono_mix 1 stem: source=spec.source_scenario_id、dry/impulse ref を
       そのまま、gain_db=artifact.applied_gain_db、delay 0、filter 'none'
  → build_measured_capability(spec, artifact, routing, ir_dataset)
       ir_origin='measured' (impulse_authority.kind とバリデータ整合)、
       prediction_measurement_identity=ir_dataset.measurement_id、
       ir_producer=(htdt.rew_ir_import, ir_dataset.importer_version)、
       hrtf_processing='none'、confidence_state='measured_reference'、
       各 authority state='unknown' (正直な欠損)、validated_bands/
       phenomenon_capabilities/known_limitations=空
  → build_review_package(..., created_at_utc=呼出し固定値)
  → save_routing / save_capability / save_review_package
       各 find_*_by_sha / get_* で冪等化 (append-only repo との整合)
```

- `created_at_utc` は package の semantic_payload に含まれるため
  呼出し側固定引数とした — 同一値なら再 emit は完全冪等、新値なら
  意図的に新しい package 行になる。
- `ir_dataset.dataset_id != spec.impulse_authority.artifact_id` は
  fail-closed で拒否 (外部 IR を render の capability に混入しない)。

## B. workflow.* help topic ×3

`help_registry.py` に実トピックを追加 (既存 `_topic` パターン、JA/EN 両
ロケール、パレット help provider が自動索引):

| topic_id | deep_link | related_commands | 内容の根拠 |
|----------|-----------|------------------|-----------|
| workflow.presentation | WorkspaceId.PRESENTATION | navigation.presentation | presentation_workspace.py: session/compare/decisions/export、読み取り専用 replay surface |
| workflow.video | WorkspaceId.VIDEO | navigation.video | cad_video_commissioning.evaluate_video_journey の 7 ステップを忠実に記述 (推奨順・非ハードゲート明記) |
| workflow.acceptance | (なし — ApplicationDestinationId は deep_link 不可) | navigation.acceptance | acceptance_page.py: 自動/手順/証明の 3 step kind、append-only run revision、replayable |

- 3 ワークスペースは reason_code を emit しないため reason_codes 不付与
  (catalog 外 code を束縛すると registry 検証が落ちる)。
- `test_help_registry.py::test_required_content_coverage` に 3 件追加。
- 実GUI 検証: `windows` QPA で `HelpDialog.topic` を実描画 + `widget.grab()`
  PNG 3 件 (JA タイトル/概要/セクション/関連操作が全て描画)。パレット検索で
  「プレゼン」「映像」「受入」「HCFR」等が各トピックをトップヒットすることを確認。

## C. 残り判断事項の再判定

| # | 事項 | 判定 | 措置 |
|---|------|------|------|
| 1 | UI_DESIGN.md「global destination 原則4〜5個」vs 実際 workspace 6 + app destination 6 | **実 drift → ��正済み** | 「少数に限定 (現行構成 workspace 6 + application destination 6)」へ文言修正。併せて同文の「19 topic」表記も陳腐化していたため件数固定表現を除去 (現 26 topic) |
| 2 | docs/ISSUE_118*.md 等の歴史設計書が 4-workspace 記述 | **意図的 → 根拠記録のみ** | 過去時点の設計スナップショット。現行参照は USER_GUIDE_JA / UI_DESIGN (REV50 で更新済み) が担う。歴史文書の改竄は権威履歴を壊すため触らない |
| 3 | achromatic-only extrema が 'other' stimulus group を除外 | **意図的・正しい → 根拠記録のみ** | `_ACHROMATIC_GROUPS={'white_point','grayscale'}` (cad_video_commissioning.py)。`_sample_group` は w*/gray_*/eotf_* を achromatic、r/g/b/c/m/y を gamut、その他を 'other' と分類。HTDT JSON importer は任意 stimulus_id を許容し得るため 'other' の luminance 意味論は不定 — achromatic 群だけに extrema を絞るのは未知入力に対する正しい fail-closed。将来 non-achromatic 群を扱うなら仕様追加 |

## テスト

新規/更新テスト:

- `test_solver_capability_manifest.py` +6: wave/geometric 導出行列、
  produced 絞込 + 宣言外 produced で fail-closed、result-commit emit の
  冪等性、stale descriptor で fail-closed。
- `test_cad_auralization_leg.py` +2: routing/capability/package 全権威の
  emit + 冪等再 emit、外部 IR dataset で fail-closed。
- `test_help_registry.py`: coverage に workflow.presentation/video/
  acceptance を追加。

Scoped pytest (変更面):

- test_solver_capability_manifest / test_cad_auralization_leg /
  test_help_registry / test_workflow_help /
  test_rev34_helpdesk_coverage / test_round23_help_guidance /
  test_cad_auralization_review / test_cad_candidate_wave_execution —
  **全件 green**。

## 残存事項

- **GA solver path の manifest**: geometric domain の導出テーブルは整備済みだが、
  GA descriptor を persist する runtime 経路自体が未存在 (REV50-SECOND #1 の
  残半分)。GA descriptor persist 点が実装されれば emit は同一機構に乗る。
- **in-app 予測/行列実行レーンからの result-commit emit**:
  PredictionAuthorityLane は永続化を行わない verify-only lane のため emit 対象外。
  ハイブリッド結果生成は provider authority model — manifest 化するには
  provider→descriptor の権威マップが必要 (仕様事項)。
- **既存 emit との重複**: r130a fixture emit (宣言) と result-commit emit
  (実結果) は宣言=産出の場合に同一 manifest に冪等収束する。produced⊊declared
  の実行では別 manifest が追記される — 意図的な監査分離。
