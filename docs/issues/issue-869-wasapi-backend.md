# Issue #869 — WASAPI オーディオバックエンド実装 (REV70)

`WasapiAudioBackend` を fail-closed スタブから実働実装へ。
IMMDeviceEnumerator → IAudioClient の共有モード同時再生録音を
COM/ctypes で直接駆動し (オーディオ用の新規外部依存は追加しない)、
`cad_wasapi_io` の注入可能ドライバシーム上に構築する。
`backend_is_simulated` は `False` のまま — この経路の完了は実機
デバイス I/O の証跡であり、品質ゲートはその実証跡を評価する。

## 構成

- `backend/src/htdt/cad_wasapi_io.py` — WASAPI 層。
  - `WasapiDriverBase` / `WasapiRenderClientBase` /
    `WasapiCaptureClientBase` — ドライバシーム。列挙・既定エンド
    ポイント・クライアント生成・フレーム授受だけを規定し、テストは
    同一シームのスクリプト化フェイクを注入する。
  - `CtypesWasapiDriver` — 実装。`IMMDeviceEnumerator` の
    アクティブエンドポイント列挙、フレンドリ名
    (PKEY_Device_FriendlyName)、`GetMixFormat` によるミックス
    レート/チャンネル数/チャンネルマスク、`IsFormatSupported`
    による共有モード float32 受付確認、`Initialize`(共有モード,
    50ms 要求バッファ) + `GetService` での
    `IAudioRenderClient` / `IAudioCaptureClient` 束縛を行う。
    HRESULT は型付き `WasapiDriverError` 系に写像する:
    `WasapiEndpointGoneError` (DEVICE_INVALIDATED /
    RESOURCES_INVALIDATED / NOTFOUND)、`WasapiBusyError`
    (DEVICE_IN_USE / EXCLUSIVE_MODE_NOT_ALLOWED —
    排他モード占有は「所有アプリを閉じるか別エンドポイントを
    選択」という操作可能な文言)、`WasapiUnsupportedFormatError`。
  - `_WasapiStream` — ポンプループ。録音を先に起動し (録音は
    正直にプリロールの室内ノイズを含む)、再生バッファをプリフィル
    してから再生開始。再生側は束縛チャンネルのみへ書き込み、
    録音側は `AUDCLNT_BUFFERFLAGS_*` を正直な `CaptureEvent` に
    写像する (DATA_DISCONTINUITY→xrun、TIMESTAMP_ERROR→drift)。
    |sample|≥0.9999999 をクリップとして計数 (int24 フルスケールを
    含み、通常の大音量は誤爆しない)。既定エンドポイントの途中変更・
    エンドポイント喪失・デッドライン超過は `device_lost` /
    `truncated` として即時終了する — 継続して成功を装わない。
  - `channel_mask_labels` / `stereo_pair_channels` —
    WAVEFORMATEXTENSIBLE のスピーカーマスクを #621/#876 の
    論理チャンネル語彙 ('FL','FR','C','LFE','SL','SR','SBL','SBR',
    'TFL','TFR','TML','TMR','TRL','TRR','L','R') へ写像する。
    マスク無し・不足分は `ch<N>` と正直にラベルし、推測しない。
    ステレオペア選択は `CANONICAL_SPEAKER_PAIRS` と同一の語彙・
    順序を使う (インポート可能な環境では正準表そのものを使い、
    lean 環境では逐語ミラーを使う — パリティはテストが保証)。

## `WasapiAudioBackend` (`cad_sweep_acquisition.py`)

- コンストラクタは任意で `driver` を受ける — 既定は
  `CtypesWasapiDriver` (遅延生成、Windows のみ)。注入ドライバで
  列挙マッピング・交渉・エラーパスをハードウェア無しで検証可能。
- `available()` — 非 Windows、ドライバ初期化失敗 (Windows Audio
  サービス停止等)、列挙失敗、アクティブな render/capture 端点の
  欠如を段階的に判定し、`unavailable_reason()` は各段階の
  正確な理由を返す (裸の 'unavailable' は返さない)。
- `enumerate_devices()` — `WasapiEndpointInfo` を正直な
  `AudioDeviceInfo` に写像する。`supported_sample_rates` は
  共有モードが束縛できる唯一のミックスレートのみ、
  `supported_formats` は受け入れるドメイン形式 (float64 は
  float32 へ変換され、`CaptureResult.actual_sample_format` が
  実線上の 'float32' を報告)、`channel_mask` / `channel_labels`
  に実マスクとラベルを載せる。ミックス形式を報告できない端点は
  空の対応表で列挙し、束縛不能にする (黙って開かない)。
- `open_stream()` — バックエンド不可用なら先に
  `BackendUnavailableError`。以降はフェイクと同一の検証順:
  未知デバイス→`DeviceNotFoundError`、方向・チャンネル範囲・
  レート (端点ミックスレートとの一致必須)・フォーマット不整合→
  `UnsupportedConfigurationError`、ビジー/消滅/ドライバ失敗→
  `BackendUnavailableError`。片側の束縛に失敗したらもう片方を
  必ず close する。

## 正直さの境界

- `backend_version` は `'wasapi-audio-io-1'` — スタブ版
  `'wasapi-unavailable-1'` の証跡とは区別される。
- `backend_is_simulated` は `backend_id` 由来で `False` のまま。
  一方 #876 の判定ラダーは `machine_verified_*` を実バックエンド経路
  のみに許すため、このバックエンド経由の実行のみ機械検証に到達し得る。
- フェイクドライバ注入のテストは**実バックエンドのコード経路**を
  検証するが、スクリプト化入力は音響証拠ではない — 実音響測定の
  証跡性は実機実行に限られる。
- UI 文言の "このビルドでは未実装" は実状に反するため、
  `MeasurementPageWorkspace` の 2 箇所のラベルを実可用性表示
  (利用可能 / 利用不可 + 理由) に更新した。

## テスト (`tests/test_issue_869_wasapi_backend.py`)

41 テスト + スキップ条件付き実機スモーク。マスクラベル写像・
語彙パリティ、可用性ステージングの全分岐、列挙マッピング、
`open_stream` 検証順 (未知/方向/範囲/レート/フォーマット/ビジー/
消滅/片側失敗時 close)、ポンプループの完了・キャンセル・
既定端点変更/喪失による device_lost・不連続フラグの xrun・
タイムスタンプ誤差の drift・クリップ計数・デッドライン切り詰め・
アンダーフロー一回報告、および注入ドライバ経由のエンジン
完走 (outcome 'completed'、backend_is_simulated でないことを
確認)。実機スモークは `available()` False で skip する。

## デバイス専用に残るもの

実エンドポイント上の実音響測定 (実チャンネルマスクの観測・
実 xrun/ドリフト・実レイテンシ)、排他モード、イベント駆動
(AUDCLNT_STREAMFLAGS_EVENTCALLBACK) 化、公式な
デフォルト端点変更通知 (IMMNotificationClient) への移行 —
現行はポーリングによる検出であり、変更検出に最大 ~0.25s の
遅延がありうることは正直な限界としてここに記す。
