# REV53-CAPTURE — capture/inbox/ingestion/lineage 系の深掘りレビュー

Scope: `capture_inbox.py` (staging 契約・`capture_ingestion_lineages`・
`reconcile_orphaned_ingestions`)、`capture_ingestion_transaction.py`
(原子性・rollback・plan_json・run_id/lineage_digest 一意性)、
`capture_entity_promotion.py` (重複 promote・再試行・per-space 権威解決)、
`capture_mesh_ingestion.py` / `capture_receiver.py` / `capture_import.py`
(受信検証ゲート)、`capture_compatibility.py` (registry drift)、inbox UI
(`CaptureInboxPage`)。全件を実コードで検証 (推測なし)。

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | 冪等 re-ingest / `verify_persisted_ingestion` で `_resolve_persisted_quality` の UPDATE (および `_register_capture_revision`/`_persist_capture_bundle` の修復 write) が commit されず破棄 — レガシー `quality_state='unresolved'` の run は永久に unresolved のまま `require_quality_state` を塞ぐ | HIGH correctness (legacy DB 永久ブロック) | FIXED — `ingest()` の検証済み分岐を `rollback()`→`commit()`、`verify_persisted_ingestion()` に `commit()` 追加 |
| 2 | `_world_to_scene_by_space` が "newest per space" を謳いながら `list_promotions()` (promotion_id ASC = コンテンツハッシュ順) の last-write-wins → 同一 (document, space) の複数 promotion で選択が任意 | MED correctness (誤権威の silent 適用) | FIXED — `(created_at_utc, promotion_id)` で昇順ソートして last-write = newest に |
| 3 | `route_capture_intent` が `import_capture_artifact` に `arrival_source=CAPTURE_ARRIVAL_SOURCE` (定数 'document_open') を固定渡し → watch-folder 経路の rejection が `document_open` ラベルで stage される | MED audit (出所改竄) | FIXED — パラメータ `arrival_source` をそのまま転送 |
| 4 | `test_runner_never_promotes_and_rejects_junk` が main 上で常時 RED — `stage_rejected` 導入以降の stale 期待値 ("junk drop は inbox 行を残さない") | — test rot | FIXED — rejected 行が stage される契約に更新 + watch_folder provenance 断言 (欠陥3の回帰) |
| 5 | CLI `htdt-capture-import` が成功 import を stage しない — 次の reconcile まで不可視で、復旧時に `restart_recovery` と誤ラベルされる | MED UX/audit | FIXED — `main()` が成功後に `inbox.stage(..., arrival_source='cli_import')` (失敗は警告のみ、commit 済みを誤報しない) |
| 6 | `stage()` / `stage_bundle_without_ingestion` / `reconcile_orphaned_ingestions` の境界条件 | — | VERIFIED — 後述 |
| 7 | ingestion 原子性 (BEGIN IMMEDIATE・rollback・plan_json・run_id 一意性) | — | VERIFIED |
| 8 | mesh 取込・receiver・compatibility registry | — | VERIFIED |
| 9 | inbox UI (昇格 UX・エラー表示・選択復元) | — | VERIFIED (変更なし) |

## 1 — 品質 backfill が永続化されない (修正済み)

`_verify_persisted_materialization` は末尾で `_resolve_persisted_quality` を
呼ぶ。後者は最新 run の `quality_state` が 'validated' でなければ
`quality/capture-quality.json` の retained evidence を再検証し、
`UPDATE capture_ingestion_runs SET quality_state=?, quality_payload_sha256=?,
quality_ruleset_version=?` を発行する — **が、この UPDATE は両呼出し経路で
commit されずに捨てられていた**:

- `ingest()` の idempotent 分岐: 検証直後に `connection.rollback()` を呼ぶ。
  これは verify 自体の write (UPDATE) に加え、欠落していた
  `capture_revisions` / `capture_bundles` 行の修復 INSERT も巻き戻す。
  戻り値は修復後の 'validated' を報告するのに DB は 'unresolved' のまま —
  結果と persisted 状態が矛盾する。
- `verify_persisted_ingestion()`: `closing(self._connect())` で commit なしに
  close → implicit transaction は rollback。

sqlite3 の既定 isolation level では `close()` 時に未 commit は破棄される
ため、pre-#337 時代に 'unresolved' で永続化された run は **再取込しても
永久に unresolved のまま** `require_quality_state` を fail し続け、
semantic promotion ゲートを永遠に塞ぐ。

修正: `ingest()` 側は `rollback()`→`commit()` (検証 *成功* 後のみコミット —
検証自体が raise した場合は外側の except で rollback が走るため原子性は
不変)、`verify_persisted_ingestion()` 側は verify 後に `connection.commit()`。

回帰テスト:
`test_reingest_persists_legacy_quality_backfill`、
`test_verify_persisted_ingestion_persists_quality_backfill`
(`capture_ingestion_runs` を直接 'unresolved' に改竄して backfill の
永続化を断言)。

## 2 — per-space 権威解決がハッシュ順に依存 (修正済み)

`_world_to_scene_by_space` の docstring は "collects the newest one per
space" と明言するが、実装は `list_promotions()` (`ORDER BY promotion_id
ASC`) をそのまま反復し `by_space[space_id] = authority` の last-write-wins。
promotion_id はコンテンツハッシュ (`capture-semantic-promotion:<sha256>`)
なので反復順は実質ランダム — 同一 (target_document, coordinate_space) に
2つの semantic promotion が残ると **古い権威が任意に勝つ**。

発生条件: 同一文書を指す同一空間の promotion が複数 run で記録される
(再取込・再 promote)。authority の transform が異なれば entity 昇格の
world→scene 座標が静默に変わる — fail-closed ではなく silent 誤適用。

修正: 反復前に `sorted(records, key=(created_at_utc, promotion_id))` —
最後に書き込まれる側が newest、同刻は promotion_id で決定的 tiebreak。
`promotion_request` 参照が `None` の行は従来通り skip。

回帰テスト: `test_newest_authority_wins_per_space` — 旧 promotion に
lexical に大きい promotion_id + +100m x 平行移動、新に小さい id +
恒等変換を与え、昇格後の entity が x≈1.0 (恒等) を断言 (pre-fix では
x≈101.0 で失敗)。

per-space 化の浸透確認: 呼出しは `promote_annotations` の 1経路のみで、
annotation ごとに `annotation.coordinate_space_id` で lookup + identity
fallback。REV46+7157ca2b の残件「全呼出しへの浸透」は浸透済みで、
本件は残存していた "newest" 順序の誤りのみ。

## 3 — watch-folder rejection の provenance 誤ラベル (修正済み)

`route_capture_intent` の import 呼出しが `arrival_source=
CAPTURE_ARRIVAL_SOURCE` (定数 'document_open') をハードコード。
`capture_watch_runner` が `arrival_source=WATCH_ARRIVAL_SOURCE`
('watch_folder') を渡しても、rejection の inbox 行は 'document_open'
として stage されていた (成功経路の `inbox.stage` はパラメータを正しく
使っているため、**reject だけが嘘をつく**非対称)。出所監査の改竄。

修正: `arrival_source=arrival_source` を転送。`capture_watch_runner` の
stale テスト更新 (欠陥4) で `arrival_source == WATCH_ARRIVAL_SOURCE` を
rejected 行に断言して回帰化。

## 4 — stale テスト: junk drop の期待値が古い (修正済み)

`test_runner_never_promotes_and_rejects_junk` は main 上で常時 RED。
`stage_rejected` 導入以降、validation 拒否は rejected-envelope 行として
stage されるのが契約なのに、テストは "junk drop は inbox 行を残さない"
を断言していた (rejected-envelope 機能より前の書き方)。

更新: `bundle_validation == 'rejected'` の行が1件存在し、
`arrival_source == WATCH_ARRIVAL_SOURCE` であることを断言 — これは
欠陥3の回帰にもなる。なお receiver の `handle_delivery` 経路
(`rejected`/`already_staged`) は独立に clean。

## 5 — CLI import が成功時に stage しない (修正済み)

`import_capture_artifact` の docstring どおり rejection staging は
`inbox_repository` 渡し時のみ。`launch_router` は成功側を別途 `inbox.stage`
するが、`capture_import.main()` (CLI `htdt-capture-import`) は成功後に
何も stage しない。結果、CLI で import した bundle は inbox に現れず、
別 lane の `reconcile_orphaned_ingestions` に拾われるまで不可視で、
拾われた時点で `arrival_source='restart_recovery'` に誤ラベルされる。

修正: `main()` が commit 成功後に plan を rebuild し
`inbox.stage(plan, arrival_source='cli_import', source_detail=str(artifact))`。
staging の失敗は commit 済みの事実を曲げないよう stderr 警告のみで
exit 0 を維持。

回帰: `test_import_cli_roundtrip` を拡張 — `arrival_source == 'cli_import'`
の validated 行が1件、再実行でも冪等に1件。

## 6 — `capture_inbox.py` — VERIFIED

- `stage()` (645–774): persisted ingestion 必須を `get_ingestion(plan.
  lineage_digest) is None → CaptureInboxError` で fail-closed に強制
  (672–681)。`stage_bundle_without_ingestion` は persisted 行が無い旨の
  明示契約で、both で `capture_inbox_items` への冪等 INSERT または既存行
  の arrival_count 更新。
- `stage_rejected` (776–920): `REJECTED_LINEAGE_DOMAIN` で決定的
  `rejected:` lineage を mint、`INSERT OR IGNORE INTO
  capture_ingestion_lineages` で衝突吸収、scope は
  `CAPTURE_INBOX_UNASSIGNED_SCOPE`、series/revision='unknown'。
- `reconcile_orphaned_ingestions` (922–968): `capture_ingestion_runs`
  に存在し `capture_inbox_items` に存在しない `validated` lineage のみを
  `arrival_source='restart_recovery'`・detail '未ステージの取り込みを復旧'
  で stage。rejected 行は `NOT EXISTS` 条件により再 stage されず
  (重複 restage なし)、retention purge は run と item を同時に消すため
  偽 orphan 化しない。全 lane (receiver handle_delivery /
  launch_router / inbox page の `list_items` wrapper) が自身の ingest
  前に reconcile するため recoverability は担保済み。
- `inspect` (1231+): rejected envelope は早期 return、`inbox item
  survives without its ingestion run` は fail-closed raise (整合性破壊を
  誤魔化さない)。
- `_record_outcome` (1596–1712): supersession ガード (1633–1642)、重複
  'promoted' 記録の許容は監査 multiplicity として意図的。
- `promote` (1734–1784) / `supersede` (1786–1903): promoted kinds が
  全種揃った時のみ 'superseded' フリップ。

## 7 — `capture_ingestion_transaction.py` — VERIFIED (修正箇所以外)

- `ingest()` (2061–2322): pre-txn validation → `BEGIN IMMEDIATE` →
  revision 登録 → bundle 永続化 → existing-run 検査 → lineage/run/
  authorities/links INSERT → `commit()`。except で rollback +
  `_persist_revision_conflict_outcome` (2324–2337) を別 txn で記録。
  部分失敗は rollback で全滅 or 全勝。
- `verify_persisted_ingestion` (2339–2393): 新規 commit 追加済み。
- `_resolve_persisted_quality` (4423–4514): `ORDER BY recorded_at_utc
  DESC, ingestion_run_id DESC` で最新 run 選択、evidence 再検証経路
  込みで backfill。
- `run_id` / `lineage_digest` はコンテンツ由来決定的 (`_ingestion_run_id`
  = lineage + ingestor + config + plan_sha256) → 冪等と一意性が一致。
- `capture_ingestion_runs.lineage_digest` は非一意 — multi-run lineage
  (#413) の許容は意図的。

## 8 — mesh / receiver / compatibility — VERIFIED

- `capture_mesh_ingestion.py`: sha256・vertex/face 配列・binding id の
  検証 + record schema/version ゲート、境界で fail-closed。
- `capture_receiver.py`: pairing lifecycle、`handle_delivery` の全
  ゲート、`_record_delivery` の BEGIN IMMEDIATE dedup
  (rejected→rewrite retry、already_staged→accepted upgrade 1060–1077)。
- `capture_import.py` `import_capture_artifact`: 5段階パイプラインの
  stage 付き fail-closed; rejection は `inbox_repository` 渡し時のみ
  `stage_rejected`。
- `capture_compatibility.py`: `HTDT_PROTOCOL_REGISTRY` 10種 v1 全て
  実サポート済みと一致; `evaluate_compatibility` は exact-set 意味論で
  'unknown' 経路は防御的 dead code (到達不能だが無害)。
- `capture_bundle.py`: `ZipSource`/`DirectorySource`/`_MappingSource`
  での検証、compression ratio・サイズ・symlink 拒否、`_schema_document_
  for_version` → unsupported_newer/unsupported_legacy の拒否境界。

## 9 — inbox UI — VERIFIED (変更なし)

- `application_pages.py` `CaptureInboxPage` (680–1059): `list_items`
  が `reconcile_orphaned_ingestions()` を包み込み、昇格は
  `entity_promotion.promotion_executor(document_id)` に配線済み。
  `warn_user` でエラー表示、選択は `inbox_item_id` で復元される。
- `workflow_application.py` (2390–2438): promote/stage の UI 配線が
  executor を通る。

## 残存事項

- `evaluate_compatibility` の 'unknown' ステータス経路は dead code
  (registry の unknown kind は exact-set 比較で 'not_in_registry' に
  落ちるため到達不能)。無害のため未対応。
- inbox UI の変更は今回の修正に関わらないため `htdt-native-gui-testing`
  による GUI 検証は不要と判断。
