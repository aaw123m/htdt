# Issue #876 — チャンネル識別/ルーティング/極性の自動検証 (REV67)

宣言された #621 チャンネル識別チェーンを、#869 ネイティブ掃引
エンジンでチャンネルごとに励起し、応答の到着/レベル/極性から
ルーティング結線を機械検証する権威層。「測定が走った」≠
「結線が検証された」を強制し、機械判定に届かないものは
オペレータ証言へ明確に降格する。

## ドメインコア (`cad_channel_verification.py`)

- `ChannelVerificationPlan` (`cvpl-`) — #621 `CadChannelIdentityChain` の
  chain_ref (id + sha ピン) と正確な `ChannelRouting` から派生する
  封緘プラン。ルーティングの無いチェーンは計画を拒否
  (fail closed — 「検証します」が先走らない)。スティミュラス
  spec の全フィールドをプラン自身が封緘するため、実行の再現と
  陳腐化判定が構造で保証される。
- `run_verification_plan` / `run_channel_excitation` — #869
  `MeasurementAcquisitionEngine` を対象チャンネルごとに
  configure → arm (オペレータ確認が実リクエストに束縛される) →
  start まで駆動する。各励起は #869 証跡権威の実レコードとして
  封緘され (`save_stimulus_definition` + `record_run` /
  `build_acquisition_run`)、`acquisition_run_ref` は保存済み
  `swrun-` の実 sha をピンする — 合成 run-id ハッシュは存在しない。
- `ChannelExcitationResult` (`cvex-`) — 1 チャンネルの励起結果。
  `capture_quality` (`passed`/`invalid`/`cancelled`/`blocked`)、
  #869 品質ゲートの判定 `quality_verdict` とタイミング資格
  `timing_quality` をそのまま記録する — 'limited' は絶対レベルへの
  注意であって識別証拠を否定しないため、`passed` の要件に含める。
- 応答解析 — 導出 IR はピークで正規化されるためレベル証拠を持たない。
  そのため `level_dbfs` はエンジン計測値 (`noise_floor_dbfs` +
  `snr_db` の掃引窓内信号レベル) から導出し、検出判定は
  `min_snr_db` と `min_response_level_dbfs` の両ゲートを通る。
  到着サンプルは IR 窓原点基準 (ループバック参照) でチャンネル番号に
  依らない。`response_signature_sha256` は (到着, 量子化レベル,
  極性符号) のハッシュ — **同一署名を持つ別チャンネルは両方とも
  機械検証できない**。
- `evaluate_channel_verdicts` — プラン横断の判定ラダー。
  重複検出は署名の計画全域共有、レベル偏差は全応答の最大値基準、
  極性は `reference_channel` 基準のみ評価 (絶対極性は判定しない)。
  - ルーティング: `verified` / `missing_output` /
    `duplicate_suspect` / `level_mismatch` / `unexpected_response` /
    `unverified`
  - 極性: `consistent` / `inverted` / `ambiguous` / `unevaluated`
  - 証拠クラス: `machine_verified_routing` /
    `machine_verified_relative_polarity` /
    `machine_assisted_ambiguous` / `operator_attested` / `unknown`
  - **シミュレートバックエンドは `machine_assisted_ambiguous` /
    `unknown` を上限** — `backend_is_simulated` の実行が実配線を
    証明することは構造上ありえない。
- `OperatorChannelAttestation` (`cvoa-`) — 恒久的に手動のフォールバック
  証拠。機械検証の昇格には使われず、unknown/ambiguous のチャンネルを
  `operator_attested` に格上げするだけで、`machine_verified_*` を
  上書きしない。
- `MapVerificationState` — `verified` (全員機械検証) /
  `failed` (欠落・レベル・極性のハード欠陥) / `ambiguous`
  (署名衝突・証言・シミュレーション混在) / `incomplete` /
  `unknown` (全員 unknown)。重複署名は「失敗」ではなく「曖昧」:
  励起自体は成功したため、オペレータが識別を解決する。

## 陳腐化

`routing_signature(routing, sample_rate)` が (再生/録音デバイス +
チャンネル + レート) のハッシュを各結果に封緘。
`stale_channels_for_routing(plan, results, current_routings, rate)` は
構成変更後に**影響チャンネルのみ**を陳腐として列挙する — ルーティング
変更が他のチャンネルの既存検証を暗黙に失効させない。

## 永続化 (`cad_channel_verification_repository.py`)

4 テーブル (`cad_channel_verification_plans` / `_excitation_results` /
`_operator_attestations` / `_verification_verdicts`)、全て `_SealedStore` +
列ドリフト検査 + 追記専用 (同一 id の異なる内容は
`ChannelVerificationConflictError`)。ネイティブスキーマ v100 で追加、
行整合性 (`_ROW_BINDINGS`) と権威監査 (`_ReplayProbe` ×4,
`'channel_verification'` repo ケース) を配線済み。

## 正直さの境界

- WASAPI は #869 同様 fail-closed スタブのまま — この権威はバックエンドが
  存在する経路では完全動作するが、実機 I/O 非対応を隠さない。
- 'limited' な品質判定はルーティング証拠として受理するが、
  `quality_verdict`/`timing_quality` は結果レコードにそのまま残り、
  `timing_unqualified` は欠陥フラグとして可視。
- 検証プランは #621 チェーンを変更しない — チェーンの訂正は
  その権威の責務。

テスト: `backend/tests/test_issue_876_channel_verification.py` (22件)。
