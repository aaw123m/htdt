# REV56-ELEC — アンプ↔スピーカー電気適合性 + as-built ケーブル/ポート追跡権威

スコープ: issues #593 (P1), #597 (P1)
ブランチ: `devin/1759688800-rev56-elec`（実ブランチはコミット時刻で採番）
スキーマ: native schema v28 → v29（4 テーブル + 7 インデックス追加;
v28 は REV56-METRICS が確保 — マージ時に再採番済み）

## 実装範囲

### #593 Amplifier–loudspeaker electrical compatibility

新規 `backend/src/htdt/cad_electrical_compatibility.py` +
`cad_electrical_compatibility_repository.py`（テーブル
`cad_electrical_qualifications`）。既存権威の合成レイヤーとして実装 —
`cad_amplifier_headroom`（PlaybackChainScalar 算術）、
`cad_speaker_impedance`（インピーダンス階層 + AmplifierLoadDomain +
AmplifierElectricalLimitAuthority）、
`cad_speaker_level_transfer`（ケーブル伝達）を組み合わせて適合判定を
行う。

- `ElectricalQualificationScenario`（`elecsc-<sha24>`、sha256 封印）—
  宣言されたアンプ仕様（capability + impedance タプル + 電気限界）×
  スピーカー仕様（機器定義 + インピーダンス権威 + ケーブル経路）の
  評価対象を封印。目標 SPL・リスニング距離・周波数帯域・同時駆動 ch 数・
  EQ ブースト・ヘッドルーム要求・応力プロファイルを明示。
  - `drive_kind='active'` レーン: 内蔵アンプ構成では amp 参照を拒否し、
    感度→必要 SPL → スピーカー出力上限ゲートのみ評価。
  - `drive_kind='passive'` では `amplifier_capability` 必須 — 未指定で
    電気適合を暗黙合格にしない。
- `evaluate_electrical_compatibility` →
  `ElectricalPlaybackQualification`（`elecq-<sha24>`、sha256 封印、
  append-only リポジトリ）。参照は AuthorityRef → sha256 に再検証
  （存在しない権威・型不一致・ドリフトは即拒否 = fail-closed）。
- 必要電力の導出（文献式）:
  `required_level = target + 20·log10(distance/reference) + eq_boost +
  headroom`、V は `10^(Δ/20)`、W は `10^(Δ/10)` でスケーリング。
  V↔W 変換は `exact_resistive_reference` インピーダンス階層でのみ許可
  — 実インピーダンス曲線で電圧↔電力を混同しない。
- ゲート列（各々独立に reason code を記録）:
  - `load_below_amplifier_rating` — 帯域内最低インピーダンスが
    AmplifierLoadDomain の下限を下回る。**最低インピーダンスは公称値を
    下回り得る**: 評価は曲線内の最小絶対値を使用し、公称値のみの階層
    （`nominal_impedance_only`）では負荷適合を `insufficient_evidence`
    に格下げ（曲線証拠なしの UNKNOWN を捏造しない）。
  - `amplifier_clipping` — 必要端子電圧がアンプ出力上限を超過。
  - `voltage_margin_insufficient` — 上限内だが宣言マージン未満。
  - `current_margin_insufficient` — 最悪ケース電流需要（帯域内
    |Z|min で除算）が AmplifierElectricalLimitAuthority の
    `rms_current_ceiling_a` を超過。
  - `thermal_derating` — sustained/thermally_stabilized 応力では連続
    定格（V/W 双方で変換可能な範囲で）を比較。
  - `multichannel_power_limit` — 「全 ch 同時駆動の実出力」と
    スペックシートの 2ch 駆動値を混同しない:
    `AmplifierChannelCountCondition` が
    `shared_supply_evidence=False` で `simultaneous_channel_count>1`
    を拒否する既存権威を合成し、シナリオの同時駆動数が証拠なし条件に
    抵触したらこのコードを記録。
  - `digital_headroom_limit` — 必要 dBFS マージンが宣言
    `digital_headroom_db` を超過。
  - `cable_loss_excessive` — ケーブル伝達評価（既存
    `evaluate_speaker_level_transfer`）のアンプ側必要電圧が上限を超過。
  - `loudspeaker_compression_limit` — 必要 SPL が SplCapability の
    連続/ピーク上限（応力プロファイルで選択）を超過。
  - `protection_engagement` — 保護条件との干渉（限界権威の条件域）。
  - `insufficient_evidence` — 上記いずれかを評価する証拠が欠ける場合。
- 判定語彙: `qualified` / `unqualified` / `indeterminate` — 実害コード
  1 件でもあれば unqualified、証拠不足のみなら indeterminate、合格は
  全ゲートを証拠付きで通過した場合のみ。**indeterminate を不合格にも
  合格にもレンダリングしない**（`measurement_evidence_display.py` の
  `証拠不足（未判定）` ラベルで区別）。
- `dominant_limiter` は固定優先順位マップ（multichannel > load_domain
  > thermal > voltage-clipping > current > voltage-margin > cable >
  speaker_compression > protection > digital）で記録された失敗から
  決定 — 評価順序依存の上書きを排除。
- `CapabilityEvidenceClass`（RP22 風能力証拠クラス）:
  `spec_sheet_estimate` < `electrically_qualified_model` <
  `acoustic_output_measured` < `in_room_commissioned` — コミッショニング
  証拠（`commissioning_evidence_ref`）や実測 provenance
  （`acoustic_output_measured` は SPL capability の provenance が
  measured の場合のみ）で昇格。スペックシート推定を実測済み能力として
  表示しない。

### #597 As-built cable / port traceability

新規 `backend/src/htdt/cad_physical_interconnect.py` +
`cad_wiring_trace_repository.py`（テーブル
`cad_physical_interconnects` / `cad_wiring_verifications` /
`cad_logical_physical_bindings`）。

- `PhysicalInterconnect`（`wire-<sha24>`、sha256 封印、バージョン付き
  append-only）— 論理経路とは別の「現場に引かれた配線」の宣言:
  `from`/`to` の `CableTermination`（機器/ポート/端子 anchor +
  ラベル + 極性）、経路クラス（アナログ・スピーカー線〜HDMI・AVoIP・
  制御・無線・電源の 10 種）、媒体・長さ・中間ホップ列・
  `PathEvidenceState`（designed / installed_reported / field_observed /
  verified）+ `ObservationState`（両端目視 / 片端 / 書面のみ /
  測定推定 / 不明）。
  - `observed_both_ends`/`observed_one_end` は実際に観測されたラベル
    （`observed_label_from/to`）を必須化 — 目視宣言に目視内容がない
    記録は封印しない。
  - `permanent_run` フラグの経路は `inwall_reference_id` 必須。
  - ラベル規格は `label_profile_standard_ref` に
    `standard_id@edition` 形式で参照 — #599 の外部規格レジストリ
    （`cad_external_standards`）に登録された版への pin を要求し、
    ラベル体系を再発明しない。
- `WiringVerificationRecord`（`wvrf-<sha24>`、sha256 封印）— 経路
  クラスごとに適用可能な試験種を限定（例: `continuity_test` は
  スピーカー線/アナログ音声に適用、`bitstream_integrity_check` は
  HDMI/デジタル/AVoIP、`domain_qualification` は #645 等のドメイン
  検証結果への evidence 参照）。不適合な試験種での検証は拒否。
  `measured_quantity` ↔ `measured_value`/`unit` の整合ペアリング強制。
- `evaluate_physical_path_state` — 宣言状態と検証状態を分離評価:
  - 不合格試験 1 件 → `failed_path`
  - 適用可能試験の合格 ≥1 → `verified_path` に昇格
  - 宣言 `verified_path` に合格試験がない → `unverified_declaration`
    + `promotion_gap` 注記（宣言が検証を装わない）
  - 検証が宣言より上位に昇格した場合も `promotion_gap` に記録
- `LogicalPhysicalBinding` + `evaluate_logical_physical_binding` —
  論理信号パス（`cad_signal_path` 系の論理参照）を物理経路 sha に
  pin。状態語彙: `verified_binding` / `observed_binding` /
  `reported_binding` / `designed_binding` / `unverified_routing` /
  `unverified_declaration` / `mismatched` / `stale` / `failed_path`。
  - 物理経路が存在しない・id 不一致 → `unverified_routing`
    （**設計上の経路と現場配線を混同しない** — バインドがない論理経路は
    この語彙に落ちる）
  - 経路改訂で pin sha が乖離 → `stale`
  - 端点 anchor が論理経路の端点と不一致 → `mismatched`
- `record_service_change` — サービス変更は新バージョンとして
  supersede チェーンに記録（旧版は削除しない）。bookkeeping 項目
  （version/timestamp/supersedes/service_action）以外に物理差分がない
  「変更」は拒否 — 無変更の点検は WiringVerificationRecord で記録する。
- `derive_cable_schedule` → `CableSchedule` — as-built ケーブル一覧:
  ラベル・媒体・長さ（`designed|estimated|measured|unknown` の出所
  付き）・両端・経路クラス・検証状態を行ごとに保持し、
  `unverified_routing` の論理経路は `unbound_logical_refs` に列挙。
- `candidate_fault_segments` — 失敗/不一致経路について、未検証ホップや
  片端のみ目視の区間を故障候補セグメントとして列挙。

### 統合・UI 最小配線

- `native_authority_audit.py`: `electrical_compatibility` /
  `wiring_trace` ファクトリ + 4 テーブルの `_ReplayProbe` 追加
  （fail-closed 監査対象に登録 — 登録漏れは監査自体が落ちる）。
- `native_row_integrity.py`: 4 テーブルの `_ROW_BINDINGS` 追加
  （ネスト `_b()` パスで sha/オプション列をバインド）。
- `application_pages.py`: `_LIFECYCLE_TABLE_LABELS` に JA ラベル
  4 件（アンプ・スピーカー電気適合性評価 / 物理配線経路（as-built）/
  配線検証レコード / 論理経路・物理配線バインディング）。
- `measurement_evidence_display.py`: REV56-ELEC セクション — verdict/
  failure code/capability class/応力・経路証拠状態・観測状態・バインド
  状態・試験種/結果の JA ラベルと `*_line` ヘルパー 4 件。
- 注: 作業開始時点の main では `measurement_evidence_display.py` の
  `spatial_binding_line` `return (` が未閉鎖だった（SyntaxError）—
  REV56-METRICS の 21391abb で先に修正済みのため本スライスでは差分なし。

## 文献根拠（web_search で確認した一次情報）

- 必要電力/SPL 導出: 目標レベル = `target + 20·log10(d/d_ref) +
  boost + headroom`、V は `10^(Δ/20)`、W は `10^(Δ/10)` —
  カスケード音量・感度（2.83 V/1 m）規約の標準式。
- IEC 60268-3:2018 — マルチチャンネルアンプの定格条件は「全 ch 同時」
  条件を伴う。スペックシートの「○○W (2ch driven)」を全 ch 同時実出力
  として読むのは誤読であり、同時駆動数と共有電源証拠をゲート化した。
- IEC 60268-5 旧条項（16.1: 最低モジュラス ≥ 公称 80%）は撤回済み
  （2026-04-17 正式に withdrawn）— 最低インピーダンスは公称を下回り
  得るという実務認識を、曲線証拠なしに UNKNOWN へ格下げする形で
  コード化（規格の「継続的有効性」を主張しない）。
- IEC 61938:2018 §11 — AV 機器の相互接続・配線識別の実務枠組み。
- AVIXA F501.01:2015 — ケーブルラベリング/ドキュメント体系（Published
  / Under Revision のライフサイクルは #599 レジストリで管理）。
- CEDIA RP22（既存 `cad_rp22_profile` 権威の証拠クラス階層に準拠）—
  ~105 dB SPL 能力ターゲットのコンテキストと capability evidence
  class のモデル（spec < model < measured < commissioned）。

## 残存事項

- 位相角（EPDR 相当の複素インピーダンスによる電流需要推定）は未実装 —
  `complex_curve` 階層でも評価は |Z|min のみ。EPDR ベースの電流需要
  ゲートは issue 本体の残件。
- アンプの時間領域クリップ/保護モデル（crest factor 依存の実歪）は
  プロキシ式のまま — 波形ベース検証は別 issue。
- ケーブルスケジュールの GUI 一覧画面は未配線（`derive_cable_schedule`
  + JA ラベルのみ — Quality ページ側の消費は後続スライス）。
- 経路長の伝送損失フィードバックは `cad_speaker_level_transfer` 経由で
  #593 側に反映済みだが、#597 側の Path 長と電気経路長の相互 pin は
  evidence ref ベースのまま（自動双方向検証ではない）。
- `label_profile_standard_ref` は registry_key 形式のみ検証 —
  レジストリ実在チェックは呼び出し側責任（fail-closed な参照取得
  ヘルパーは残件）。

## 既知の pre-existing 失敗（この変更とは無関係）

- `test_application_pages.py::test_shell_registers_application_destinations`
  / `::test_shell_project_identity_visible` — clean main（stash 検証済み）
  でも失敗。
- `measurement_evidence_display.py` の SyntaxError（修正済み、本 PR
  同梱）。
