# Issue #885 — ガイド付きトラブルシューティング・オーケストレータ (REV68)

症状観測から始まる診断ループ
`OBSERVATION → HYPOTHESES → SAFE DISCRIMINATING TESTS →
AUTOMATIC MEASUREMENT/READBACK → EVIDENCE UPDATE →
RANK/ELIMINATE → NEXT TEST / RESOLUTION`
を #869 掃引エンジン・#876 チャンネル検証・#806/#878
デプロイ/バインディング権威・#719 証拠ラダーの上に載せる
封緘権威。**弁別試験の駆動であり、自動診断ではない** —
機械は仮説を反証・支持するが、単一残存仮説を物理的根本原因と
宣言することは構造的に禁止されている。

## ドメインコア (`cad_diagnostic_orchestrator.py`)

### ステージ機械

`DiagnosticStage` は
`observation → hypotheses → test_planning → measurement →
evidence_update → ranking` のループと終端
`resolved / unresolved / needs_inspection / aborted` を持つ。
`derive_session_state` は封緘済み遷移ログ (`dtr-`) を seq 順に
畳み込んで状態を完全に再構成する (#868 と同じ resume 手法) —
セッションの永続化は遷移の追記のみで、状態は常に再導出される。
拒否・ブロック・情報イベントも全て `rejected`/`blocked`/
`informational` 遷移として追記され、監査面で「試みたが通ら
なかった」こと自体が証跡になる。

### 封緘レコード

- `DiagnosticSessionRecord` (`diag-`) — 症状参照 (ref+sha ピン)、
  対象フォルトツリー、#719 ケース参照、任意のデバイス
  バインディング (id+sha) と `expected_deployed_config_sha256`、
  セッションの音響露出上限 `max_stimulus_level_dbfs`。
- `DiagnosticStageTransition` (`dtr-`) — 全イベント/判定/拒否の
  追記専用ログ。
- `DiagnosticHypothesisEntry` (`dhyp-`) — セッション内仮説キーと
  #719 `diagnostic_hypothesis` 参照の束縛。
- `DiagnosticTestPlan` (`dtp-`) — テンプレートから生成された
  封緘試験計画。メカニズム・安全クラス・弁別対象・事前宣言された
  判別規則 (`DiscriminationRule`) を固定する。
- `DiagnosticObservationRecord` (`dob-`) — 機械事実
  `(key, value)` と封緘証拠参照。`operator_observation`
  メカニズムは証拠参照を構造的に持てない。
- `DiagnosticEvidenceUpdate` (`dev-`) — 観測適用後の仮説効果と
  ランキング before/after を封緘 (変更は必ず観測を伴う)。
- `DiagnosticOperatorAuthorization` (`dau-`) — 1 プラン 1 回使い
  切りの明示承認。消費は観測の `authorization_ref` から導出する。
- `DiagnosticResolutionRecord` (`dres-`) — 判定語彙
  `root_cause_confirmed / fault_isolated / unresolved /
  needs_inspection` + 残存仮説 + #719 verdict 参照。

### フォルトツリー (`FAULT_TREES`)

代表 3 本を封緘辞書として宣言:

- `channel_missing_output` — ルーティング誤結線 / 未デプロイ /
  ミュート・ゼロゲイン / 測定系故障 / ハード故障 (検査専用)。
  試験: デバイス read-back → チャンネル検証 (cvpl-) → 代替出力
  掃引 → オペレータ検査。
- `sub_output_low` — サブ未ルーティング / 極性・クロスオーバー
  キャンセル / ゲイン・プリセット不一致 / リミッター余量 /
  測定系故障 / 実室挙動 (検査専用)。試験: read-back → サブのみ
  掃引 → メインのみ掃引 → メイン+サブ極性比較 → オペレータ検査。
- `prediction_residual_unexplained` — レジストレーション誤差 /
  測定系故障 / デバイス設定ドリフト / モデル形式不足 (検査専用)。

テンプレートのバリデータは構造的安全を強制する:
`SAFETY_FORBIDDEN_ACTION_TOKENS` (protective_earth / ground_lift /
mains_live / cheater_plug …) を action/test ラベルに含む計画は
**構築不能**。`reconfiguration` は `rollback_note` 必須、
`device_mutation` は `operator_authorization` クラス必須。

### 安全評価 (`evaluate_test_plan_safety`)

プラン段階の判定語彙: `prohibited` / `requires_authorization` /
`requires_rollback_plan` / `requires_level_cap` (テンプレートの
`max_level_dbfs` がセッション上限超過) / `permitted`。
`plan_next_test` は非 `permitted` を `blocked` 遷移として封緘して
`DiagnosticSafetyError` で拒否し、承認が必要なら
`authorization_class` を立てて計画を止める。

### メカニズム駆動

- `sweep_acquisition` — #869 `MeasurementAcquisitionEngine` を
  チャンネルごとに configure→arm→start。掃引 stimulus と
  `swrun-` を sweep 権威へ封緘し、参照を観測にピン。
  事実: `response_detected` / `quality_verdict` /
  `level_dbfs` / `combined_polarity` (IR ピーク符号比較)。
- `channel_verification` — #876 `run_verification_plan` を実行し
  `cvvd-` 判定をピン。事実: `map_state` /
  `channel_routing_state` / `channel_polarity_state` /
  `reference_channel_responded`。
- `device_readback` — #806 `build_observation` (source='read_back')
  をアダプタ経由で取得し `effective_settings_snapshot` をピン。
  事実: `deployed_config_match` / `deviation_count` /
  `channel_muted_or_zero`。**手作業再入力を提案する前に常に
  機械 read-back が先に走る** — ツリー構造上、オペレータ検査
  テンプレートは `manual_requires_machine_exhaustion` フラグで
  自動試験が尽きるまで選択不能。
- `operator_observation` — オペレータが宣言した事実のみ記録
  (証拠参照なし、manual 観測として明示)。

測定不能な量 (例: 物理実態) の事実は生成されず、ルールは
`missing` にフォールバックして `insufficient_evidence` と評価する —
捏造された観測は存在しない。

### ランキングと解決

`compute_hypothesis_states` は効果を畳み込む
(`contradicted` は吸収状態)。`rank_hypotheses` は
`confirmed > test_supported > candidate > confounded >
contradicted` → `rank_seed` の決定的順序。
`derive_resolution` は #719 `evaluate_diagnostic_verdict` と
残存仮説の合成で判定:

- `root_cause_confirmed` — #719 が宣言済みスコープ内の確認を
  示す場合のみ (controlled intervention 証拠が要る)。
- `fault_isolated` — 単一残存仮説が機械支持を持つ場合。
  残存が検査専用なら `needs_inspection` に降格する。
- `unresolved` — 残存ゼロ、または支持なし+試験尽きた。
- `needs_inspection` — 検査専用仮説の残存等、機械が決め切れない
  全ての曖昧終端。

**単一残存でも自動で root cause と称しない** — `fault_isolated`
は「局所化」の主張であり、物理的根本原因確定は #719 の
controlled-intervention 経路に限定される。

## 永続化 (`cad_diagnostic_orchestrator_repository.py`)

8 つの `_SealedStore` (sessions / transitions / hypotheses /
test_plans / observations / evidence_updates / resolutions /
authorizations)。列値と封緘ペイロードは読み出し時に整合検証
され、不一致は `DeploymentIntegrityError` でフェイルする。
スキーマは v106 (`cad_diagnostic_*` 8 テーブル)。

## オペレータ面

`DIAGNOSTIC_STAGE_LABELS` / `DIAGNOSTIC_RESOLUTION_LABELS` /
`DIAGNOSTIC_MECHANISM_LABELS` / `CAUSE_FAMILY_LABEL_KEYS` の
JA ラベル辞書を同梱。読み取り専用パネルへの配線は UI スコープ外
(`application_pages.py` のテーブルラベルのみ追加済み)。

## デバイス専用で残るもの

- 実機 AVR/機器での read-back 観測精度 (FakeAvrLanTransport は
  `simulated` 権威)。
- 音響応答の真偽 — FakeBackend 証拠は #876 の
  `simulated_backend` 欠陥フラグで manual-tier にキャップされる。
- `root_cause_confirmed` への到達 — controlled intervention
  (物理変更 + 症状消滅の再確認) は機械駆動できない。
