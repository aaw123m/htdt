# Issue #869 — HTDTネイティブ掃引測定エンジン (REV66)

REW 等の外部ツールを介さず、HTDT 自身が掃引信号の生成・再生・録音・
インパルス応答導出を行う取得権威。「録音が完了した」≠「有効な測定」を
強制するため、全結果は品質ゲートを通り、証跡として封緘保存される。

## ドメインコア (`cad_sweep_acquisition.py`)

- `SweepStimulusSpec` / `SweepStimulusGenerator` — 対数正弦掃引。
  開始/終了周波数・掃引長・レベル dBFS・pre/post-roll・フェード・
  リピート回数・ギャップ・厳密なサンプルレート束縛。`params_sha256` は
  spec + ジェネレータバージョン (`SWEEP_GENERATOR_VERSION`) のハッシュ、
  `samples_sha256` は生成波形バイトのハッシュ — 同じ spec は常に同じ
  アイデンティティを持つ。
- `AudioIOBackend` — デバイス列挙 / チャンネルルーティング束縛 /
  レート・フォーマット検証 / 同時再生録音の抽象。暗黙の
  デバイス・レート・チャンネルフォールバックは存在しない:
  未束縛・非対応は `DeviceNotFoundError` / `UnsupportedConfigurationError` /
  `BackendUnavailableError` で fail closed。
  - `WasapiAudioBackend` — スタブ。`available()=False`、列挙は空、
    `open_stream` は `BackendUnavailableError`。本ビルドでは実デバイスI/Oは
    未実装であり、それを正直に報告する。
  - `FakeAudioBackend` + `FakeBackendScenario` — 遅延 (電気/音響)、
    ドリフト、クリッピング、xrun、ノイズ、中断、デバイスロス、
    切り詰めを決定論的にシミュレート。**シミュレートされた完了は
    テスト証跡のみ**であり、実音響キャプチャではない
    (記録には `backend_is_simulated=True` が必須で残る)。
- `MeasurementAcquisitionEngine` — 状態機械:
  `precheck -> ready -> armed -> playing_recording -> processing ->
  quality_check -> completed / failed / cancelled`。全遷移に明示的な理由と
  時刻を保持 (`StageTransition`)。アーム後の構成変更 (`configure` 再実行、
  `notify_configuration_changed`) は armed を `ready` に無効化する。
  キャンセルは全ての非終端ステージから第一級。
- `TimingReferenceResolver` — タイミング証拠を明示:
  有線ループバック基準 → `qualified` (`loopback_cross_correlation`);
  検出 onset のみ / 宣言同期 → `limited`
  (`stimulus_onset_detection` / `unsynchronized_declared`);
  何も検出できない → `unqualified`。非適格なタイミングが
  黙って「有効」に整列されることはない。
- `ImpulseResponseDeriver` — バージョン付き逆掃引畳み込み
  (`IR_DERIVATION_VERSION`) + レイテンシ整列 + IR 窓原点
  (`loopback_reference` / `detected_onset`) + 正規化 + 任意の FR 導出。
  生録音と導出 IR は別々の証跡アーティファクト。アルゴリズム
  バージョンは各レコードにピンされ、歴史的証跡を遡って変更しない。

## 品質ゲート (`evaluate_acquisition_quality`)

録音完了は自動的に有効ではない。各不合格理由は別個の verdict reason:
`clipping_detected` / `insufficient_snr` / `capture_truncated` /
`xrun_detected` / `device_lost` / `calibration_missing` /
`calibration_invalid` / `synchronization_unusable` / `excessive_noise` /
`inconsistent_repetitions` / `cancelled` / `backend_unavailable` /
`open_failed`。verdict は `valid` / `limited` / `invalid` /
`unevaluated`。`QualityGateThresholds` で閾値は構成可能。
絶対レベル要求 (`requires_absolute_level`) 時は校正の
missing/unknown/invalid が fail closed になる。校正未束縛のままの
完了は警告 (`absolute SPL stays UNKNOWN`) を残す。

## レベル安全ゲート

`LevelSafetyPolicy` (出力上限 dBFS、ループバック要求) と
`ArmConfirmation`。アームには「ready ステージ」+「表示された
デバイス・チャンネル・レベルがアーム対象設定と一致」の両方が必要。
不一致・ポリシー超過は `ArmBlockedError` で拒否される。

## 封緘証跡 (`cad_sweep_acquisition_evidence.py` + repository)

3つの `_SealedStore` テーブル (スキーマ v97):

- `cad_sweep_stimulus_definitions` (`swstim-`) — spec・ブロック形状・
  params/samples ハッシュ・ジェネレータバージョン。
- `cad_sweep_acquisition_runs` (`swrun-`) — 刺激 ref、エンジン/生成器/
  導出/バックエンドのバージョン + ID、`backend_is_simulated`、
  デバイス/チャンネル/ルーティング (アームされた値)、
  requested-vs-actual のレート/フォーマット、タイミング方式 +
  レイテンシ証拠、生音声/IR の sha + 資産パス、校正状態 + ref、
  project/scene/campaign/role/position/orientation ref、品質 verdict/
  reasons/warnings/測定値、outcome、armed/captured/completed 時刻。
- `cad_sweep_acquisition_stage_events` (`swstg-`) — 全遷移を
  `run_seq` 順に封緘。

`CadSweepAcquisitionRepository.record_run` は生録音と導出 IR を
管理資産 (content-addressed `ManagedAssetStore`) にインストールし、
`cad_measurement_assets` の索引行と封緘行を同一トランザクションで
書く — 参照される資産が欠落しない。読み出しは全バインド列の
再検証付き (改ざん → `SweepAcquisitionIntegrityError`、
同一 ID 別 payload → `SweepAcquisitionConflictError`)。

## 統合点

- `cad_schema` / `cad_schema_ddl` / `native_row_integrity` /
  `native_authority_audit` (`_TABLE_POLICY` / `_RepositoryChain` /
  `_ReplayProbe`) / `application_pages` ラベルレジストリ配線済み。
- 測定ワークスペースに `acquisition` コンテキスト追加
  (`掃引測定` タブ): バックエンド状態・デバイス列挙・ルーティング・
  掃引パラメータ・ポリシー上限・ステージ + ブロック理由 +
  次の許可操作・品質 verdict・証跡アイデンティティ・証跡保存ボタン。
  既定は WASAPI スタブ (列挙は正直に空)。テストは `acquisition_backend`
  注入でフェイクを使用。
- REW 境界: 本エンジンは REW を置き換えるものではない。REW との相互運用
  (エクスポート/インポート) は従来通り委託証跡レイヤが担い、外部解析は
  取得証拠を置き換えない。本スライスではエクスポート経路は未実装 —
  境界はここに文書化される。

## デバイス専用に残るもの

実 WASAPI キャプチャ (実機ドライバ経由の同時再生録音)、実機
ループバック配線の検証、実デバイスの xrun/ドリフト観測。
フェイクバックエンドのシミュレート完了はテスト証跡のみであり、
実音響測定ではない。実バックエンド着工時は `run_acquisition` の
実行をワーカープールに移す設計余地を UI 側に残した (現在は同期実行)。

Refs #869
