# Issue #867 — 製品性能バジェット (REV67)

プライマリワークフローの名前付きベンチマーク可能操作を、決定的代表
フィクスチャに対して分布 (p50/p95) で計測し、明示予算と突合する
製品性能契約。一回の anecdotal timing ではなく分布のみを証拠とし、
未計測の操作は絶対にパスにならない。

## 責務の分離 (issue 要求)

- **性能バジェット** (本層): UX 応答性・スループット・メモリの製品
  ゲート。数値収束やソルバー妥当性の証明ではない。
- **数値収束**: ソルバー側のゲートが別途担う — ベンチマーク時間は
  ソルバーが正しいことの証拠に使われない。
- **UX 正しさ**: タイミングは UX 受入の代替ではない (受入ゲートは
  別レイヤ)。
- **ハードウェア依存操作** (3D viewport): fake CI timing を主張
  しない。owned-machine プロトコルのみ (下記)。

## 構成

### `perf_fixtures.py` — 決定的代表プロジェクト

- `FIXTURE_SPECS` — small (24 entities / 4 revisions) / medium
  (300 / 12) / large (1500 / 24)。seed + 部屋寸法を spec に固定。
- `fixture_spec_sha256(size)` — ジェネレータ版 +
  spec の canonical sha。**同一 spec は全マシンで同一 sha を持つ**。
- `ensure_fixture(root, size)` — `fixture_identity.json` の
  spec_sha256 が期待値と一致すれば再利用、なければ stale db を
  削除して再生成。実保存チェーン (parent_revision_id 連鎖) を持つ
  実 `cad.sqlite3` を作る — 合成モックではない。
- entity 構成はドメイン制約準拠: speaker (`speaker_role` 必須) /
  seat / measurement_point (size_m 不可) / sized kinds。

### `perf_harness.py` — 操作レジストリ + 実行

- `OPERATIONS` — 13 の名前付き操作:
  `application_startup_cold`/`warm` (実サブプロセス import)、
  `project_open`/`save`/`reopen` (実 SceneRepository 経路)、
  `scene_revision_apply`、`workspace_first_mount` (非VTK 4面の
  初回マウント)、`workspace_transition` (マウント済み定常遷移)、
  `measurement_plot_load` (model層)、`comparison_view_prep`
  (revision diff)、`memory_idle_rss`/`memory_loaded_rss` (実 RSS)、
  `shutdown_close`。**`viewport_3d_interaction` は
  hardware_sensitive=True** — ハーネスは実行せずプロトコルに委譲。
- `run_benchmark` — warmup + iterations の分布収集。操作が例外を
  投げた場合はその操作が「サンプル無し」として記録される
  (on_error コールバック経由) — 例外はハーネスを殺さず、
  unmeasured 証拠として報告される。
- `process_rss_bytes` — psutil 不使用の実 RSS プローブ
  (Windows: psapi GetProcessMemoryInfo / POSIX: /proc/self/statm)。
- room/video ワークスペースは VTK 面を持つため offscreen
  ベンチマークの対象外 — 除外を result detail に明示し、
  viewport プロトコルがカバーする。

### `perf_budget.py` — 予算 + 判定 + 報告

- `PerfEnvironment` — 実行同一性: app sha (実 `git rev-parse`,
  失敗時は `'unknown'`+dirty として記録) / app_dirty / OS / machine /
  python / cpu / 総メモリ (ctypes GlobalMemoryStatusEx /
  /proc/meminfo) / gpu (プローブ不可時は `None` — 捏造しない) /
  fixture_id + fixture_document_sha256 / iterations /
  benchmark_version / recorded_at_utc。
- `OperationDistribution` — samples + p50/p95/min/max +
  rss_bytes_after。**分布のみ、単一 timing なし**。
- `PerfBudgetEntry` — 1 操作 × 1 サイズの予算
  (`p95_budget_s` / `memory_budget_bytes`)。少なくとも一方の
  次元を assert する構造必須 — 何も assert しないエントリは
  ValidationError (偽のパスを構造拒否)。
- `evaluate_distribution` ラダー — 予算無し → `unmeasured`;
  p95 超過 → `exceeds_budget`; rss 未プローブでメモリ予算あり →
  `unmeasured` (「計測不能」はパスでない); rss 超過 →
  `exceeds_budget`; 全 assert を内側 → `within_budget`。
- `evaluate_report` → 封緘 `PerfRunReport` (`perf-` +
  canonical sha256): いずれかの exceeds → `failed`; いずれかの
  unmeasured → `incomplete`; 全て within → `passed`。
  hardware_sensitive 操作は `hardware_sensitive_operations` に
  逐語列挙されるため `passed` がそのカバレッジを偽装しない。
- 同一入力は同一 sha を持つ (canonical JSON)。

### `scripts/perf_benchmark.py` — CLI

- `--fixture-size small|medium|large` `--iterations N` `--warmup M`
  `--check` `--out DIR` `--fixtures-root DIR` `--operations …`
- 出力: `perf-report-<size>.json` (機械可読封緘レポート) +
  `perf-report-<size>.md` (人間可読) を out dir へ — リリース
  証跡と一緒に保持される artifacts。
- `--check` で manifest 突合: verdict `passed` → exit 0、
  `failed`/`incomplete` → exit 2。manifest 不在 + `--check` は
  exit 2 (予算なしの通過を許さない)。

### `scripts/perf_budgets.yaml` — 予算 manifest

- 各 entry は rationale 付きで、reference box 実測ベースライン +
  2-4x ヘッドルームを文書化。回帰を黙らせるための引き下げは
  記録なしに行わない。

### リリースゲート統合

- `release_verification_manifest.yaml` の `performance-budget`
  クラス (required, dev プロファイル除外) — small フィクスチャの
  `--check` 実行 + `test_issue_867_perf_budget.py` を
  release profile で強制。**required クラスは capability probe に
  依存できない** (必須が環境 skip を許すと破綻するため、本クラスは
  `requires_capability` を持たない設計)。

## 回帰検出の証明

- `test_intentional_slowdown_regression_is_detected` — 実ハーネスが
  実 small フィクスチャで `project_open` を計測 → 意図的に締めた
  予算 (p95 の 50%) で `failed`/`exceeds_budget` となることを
  end-to-end で証明。
- `test_slow_operation_measured_higher` — 実際に遅いコード経路
  (sleep 混入) が分布としてより大きなサンプルを生むことを証明。
- `test_failing_operation_is_unmeasured_never_passed` — 例外を
  投げる操作は unmeasured 証拠として記録され、レポートは
  `incomplete` — 絶対に pass にならない。

## owned-machine プロトコル (ハードウェア依存操作)

`viewport_3d_interaction` 等は reference マシン上で実行し、
report の `environment` (app sha + dirty + host 同一性) を
証跡と一緒に保持する。CI timing を viewport 証拠として名乗らない。

## 境界 (non-goals)

- マイクロ最適化ターゲットではない — 予算は回帰ゲート。
- ベンチマーク timing はソルバー妥当性の証拠にならない。
- 絶対値をハードウェア横断で一致させる要求ではない — 同一性は
  fixture spec sha256 + environment identity で担保し、予算は
  各マシン固有のベースライン記録に対する上限。
- 計測不能 (Qt 非搭載環境等) は `incomplete` — fake pass ではない。
