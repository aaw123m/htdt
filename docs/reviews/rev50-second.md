# REV50-SECOND — REV49 変更の再レビュー + 仕様/ドキュメント drift 監査

Scope: REV46 が実証したパターンに基づく second-pass — REV49 でマージされた
PR #552 (NEWSURFACES 24件) / PR #553 (FULLSWEEP 11件) / 9125ae74
(presentation warn_user 化) の各修正が新たな欠陥を生んでいないかを
批判的に検査し、さらに docs/・能力マニフェスト・workspace/command/
help-topic の parity を機械的に監査して drift を全件列挙。

## A. REV49 diff の再レビュー結果

### 検証済み・問題なし (読み合わせで一致を確認)

- **length_spinbox 抽出**: `MetricSpinBox`/`_PendingTextSpinBox` の
  room_workspace → length_spinbox 移動は挙動完全一致。`setToolTip` の
  forward、`set_minimum_m`/`set_maximum_m` の変換、pending-text 契約維持。
- **generic binder**: `bind_display_length_policy` は weakref 参照 +
  死亡時 self-prune でリークなし。field_explorer_panel は mount 時に
  `bind_length_policy_widget` 経由で既に接続済み (controller が
  getattr で発見)。stride 変換は全呼出しで `value_m()` に正規化済み。
- **CommandContext parity**: PRESENTATION/VIDEO メンバー追加は
  `COMMAND_CONTEXT_LABELS`・palette context_provider・全 78 command の
  context 宣言と整合。`_score` は context をフィルタではなく +20 ブースト
  として扱うため、新 context の追加で既存コマンドの検索性は変わらない。
- **help topic 追加**: `trouble.rew_errors`/`trouble.storage_errors`/
  `trouble.authority_errors` は全エミットされた reason_code と 1:1 に
  対応 (機械的突合で未クレーム 0・多重クレーム 0)。umbrella 残存も
  設計通り。
- **HealthCheckDialog JOIN 化**: `list_plans_for_document` の JOIN は
  `_plan_from_row` の fail-closed 契約と等価 (baseline 不一致行を捨てる)。
- **warn_user typed-error pass-through**: `PresentationConflictError` 等は
  `_SUFFIX_PATTERNS` で `authority.*` の JA メッセージにマップ済み。
  JA-looking ≤160 文字の ValueError/TypeError pass-through も実装通り。
- **9125ae74 presentation warn_user**: 同上 — pass-through 条件は
  `user_facing_error._looks_ja` で担保。

### 修正した欠陥 (実欠陥 — テスト付き)

| # | 欠陥 | 性質 | 修正 |
|---|------|------|------|
| 1 | **drop された auto-check 結果が phantom journal revision を発行** — REV49 が `_on_check_result` に non-pending/unknown-step の drop ガードを導入したが、drop 後も `repository.commit(latest, steps)` が走り、`next_run_revision` が revision+hash チェーンを増やしていた。レース時に空の revision が残り journal の整合性を汚染。 | **導入欠陥** | `_apply_check_result(steps, step_id, result, capture_only) -> list|None` を純粋関数として抽出し、`_on_check_result` は `None` のとき commit をスキップ (row selection のみ)。 |
| 2 | **persistence probe が非 dict JSON で AttributeError → 'unavailable'** — `json.loads` が list/scalar を返すと `marker.get('run_id')` が落ち、run_auto_check の fail-closed が不可解な 'unavailable' を返す。retry 不能でないが診断不能。 | **導入欠陥** (REV49 でこの関数が作られた) | `isinstance(parsed, dict)` ガード — 非オブジェクトは「marker なし」と同じ再発行経路へ (unparseable と同契約)。 |
| 3 | **radius baseline が表示単位** — `_baseline['radius']` に `radius_field.value()` (表示単位) を保存し、`_widget_matches_baseline`/`_body_geometry_edited` でも `.value()` と比較。`set_display_units` が Vector3Editor は再ベースラインするが radius は未対応 → mm→inch で phantom dirty → 無編集でも body_geometry 再書込みが発生。commit 側は `value_m()` を使うため保存値は正しいが、dirty 判定が誤発火。 | **既存欠陥** (REV49 の unit 化で顕在化) | 3 箇所を `value_m()` に統一 — SI メートル権威と commit 経路に一致。 |
| 4 | **probe_x に accessibleName が無い** — REV36 の a11y ラベル化で Y/Z のみ設定され X が漏れ。 | **既存欠陥** | `probe_x.setAccessibleName('プローブ位置 X座標')` 追加。 |
| 5 | **`derived_yaw_steps(0)` が ZeroDivisionError** — session フローは `ge=1` で検証済みだが direct API で `step_deg=0` を渡すと `reach_deg // step_deg` が落ちる。 | 堅牢化 | 先頭で `ValueError` を送出。 |

## B. drift 監査 — 全件列挙

### 修正済み

| # | drift | 修正 |
|---|-------|------|
| 1 | `docs/USER_GUIDE_JA.md` の workspace タブ表が 4 件 (概要/部屋/測定/最適化) で プレゼン/映像調整 が欠落 | 両行を追加 |
| 2 | `docs/UI_DESIGN.md` の rail 列挙が 4 workspace + 5 destination — WorkspaceId と不一致、受入検証 destination 欠落 | プレゼン/映像調整 + 受入検証を追加 |
| 3 | `docs/GUIDED_ACCEPTANCE.md` が「`unavailable` → step blocked」と記述 — REV49 で pending-with-recorded-reason に意味変更済み | 現行の pending + reason 記録の説明に更新 (blocked は model 内で有効だが auto path からは設定されない旨を明記) |

### 判断事項 (設計判断が要るため修正せず記録)

| # | drift | 判断を要する理由 |
|---|-------|------------------|
| 1 | **`SolverCapabilityManifest` の 9 現象 + `AuralizationCapability` が production 非接続** — `build_solver_capability_manifest`/`save_capability_manifest`/`build_auralization_capability`/`save_capability` はテストからのみ呼ばれ、実ランタイムがマニフェストを書く経路が無い。宣言 schema は実装と一致するが emit されない。 | 「計算能力を宣言に残すが runtime 未配線」の意図か、書き込み経路を接続すべきかはプロダクト判断。emit 経路を足すと unused-warning になるだけなので現状でも害はない。 |
| 2 | **workflow.* help topic が presentation/video/acceptance 系に無い** — help_registry のトピック集合は既存 family (workspace.*/trouble.*/preference.*) に揃っているが、新規 workspace に対応する workflow topic が未作成。 | 新規 help topic の追加はコンテンツ執筆を伴う設計作業であり drift 修正というより新規要求。umbrella topic で現状カバーされているため緊急性は低い。 |
| 3 | **UI_DESIGN.md「global destination は原則4〜5個以内」のルールと実際の 6 destination** — ルール自体はガイドライン文であり、rail は collapse 可能と併記済み。受入検証の追加で 6 になったが原則違反というより原則の見直し対象。 | デザイン原則の更新はプロダクト判断。本文に「原則」とあるので当面運用で調整可能。 |
| 4 | **`docs/ISSUE_118*.md` 等の歴史的設計書が 4-workspace 記述** | 過去時点の設計スナップショットであり、現在値との差は歴史的 drift として許容。現行参照は USER_GUIDE/UI_DESIGN が担う (今回修正済み)。 |
| 5 | **achromatic-only extrema が 'other' stimulus group を除外** — REV49 で achromatic 限定に意図的に絞られた実装だが、importer vocabulary に 'other' が存在し得る。 | 現行 importer は achromatic 系のみを emit するため現状では未到達。将来 non-achromatic group が来た時の扱いは仕様追加で判断。 |

### 機械的 parity チェック結果 (全件クリア)

- **WorkspaceId ↔ CommandContext ↔ COMMAND_CONTEXT_LABELS**: 全 enum メンバーが三箇所で一致 (REV49 で追加済み)。
- **workspace ↔ command parity**: 全 78 command が各 workspace context から reachable (`_score` の +20 boost により制限なし)。プレゼン/映像調整/受入検証の navigation command も登録済み (REV49 追加)。
- **help topic ↔ emitted reason_code**: コード上の全 `UserFacingError` reason code が一意の topic にマップ (umbrella + family + 個別の 3 層)。未クレーム 0、多重クレーム 0 (comment 内の見かけ上の code 表記を除去したうえでの突合)。
- **能力マニフェスト schema ↔ 実装**: 9 現象の enum と AuralizationCapability のフィールドは実装と一致 — だが emit 経路なし (判断事項 #1)。

## テスト

`backend/tests/test_rev50_second.py` (9 件):
- `_apply_check_result` の drop ケース 3 (decided step / unknown step / pending pass mutation / unavailable stays pending)
- 非 dict probe marker → deferred 再発行 (従来は AttributeError)
- radius baseline の display-unit switch で phantom dirty 不発 + 実編集で dirty 発火
- probe_x accessibleName 存在
- `derived_yaw_steps` の 0/負 step で ValueError + 正常系の値列

Scoped pytest: 新規 9 件 + test_rev49_fullsweep + test_rev49_news_surfaces +
test_cad_acceptance + test_cad_body_geometry + test_cad_field_explorer +
test_user_facing_error + test_room_workspace + test_rev47_iss2_acceptance =
**126 件 green** (-n 4)。
