# REV56-INFRA レビュー — ラック/電源/熱適格性 + イマーシブレンダーパス + ハム/バズEMC診断

対象 issue: #587 (rack/power/thermal qualification), #603 (render-path qualification), #606 (hum/buzz/grounding-EMC diagnosis)。
スキーマ: NATIVE_SCHEMA_VERSION 35 (`_migrate_34_to_35`)。authority_version は `rev56-infra-*-1` / `rev56-render-*-1` / `rev56-humbuzz-*-1` 系。

## 実装範囲

### cad_infrastructure.py (#587)
- `RackEnclosure` — 収容(U数)/密閉度/換気経路(吸気・排気・パッシブ開放)/ブランキング/設計周囲温度/サービスアクセス。`enclosure_kind` は `closed_cabinet` 等の明示分類。
- `InstalledRackDevice` — role(amp/processor/projector/HDMI等)、発熱3値(idle/nominal/max W)、`ambient_limit_c`、発熱証拠クラス(`measured_load`/`nameplate`/`derived_from_input_power`/`declared_only`/`unknown`)、熱保護宣言、ファン挙動(#580 の機械ノイズ権威へ委譲)。`circuit_ref` は生成された回路の semantic id を指す。
- `BranchCircuit` — 電圧/周波数/ブレーカ定格、`continuous_load_policy`=`continuous_derated` で usable = V×A×0.8(NEC 80% 連続負荷)。`ups_ref` で保護機器と結合。
- `PowerProtectionDevice` — capabilities(`surge_protection`/`ups_backup`/`power_conditioning`/`sequencing`…)、W/VA 定格、ランタイム実測/宣言、観察状態。VA のみの UPS は W 換算不能→ insufficient。
- `PoEBudget` — ポート毎天井(`_POE_PORT_CEILING_W`: af=15.4/at=30/bt3=60/bt4=90 W)と総量バジェットの二重判定。
- `InfrastructureScenario` + `ScenarioDeviceLoad` — 実稼働シナリオ(負荷%は明示; idle のみ判定禁止)。repeat device_ref は封印時拒否。
- `RackThermalMeasurement` — 多点温度/時間/暖機完了/上昇継続フラグ/実測電力。`steady_state_ok` と `max_point_c`/`ambient_c` を導出。一次計算(BTU=3.412×W, 1.08 係数)は計算クラスに留まり実測と混同しない。
- `evaluate_infrastructure` — overall: `qualified` / `qualified_with_limitations` / `insufficient_evidence` / `not_qualified` / `over_capacity` / `design_only`。未計測・未宣言は fail-closed で降格。回路 overload は `over_capacity`、UPS 不足は `not_qualified`。

### cad_render_path.py (#603)
- `ImmersiveContentProfile` — コンテナ/format_label/atmos,dtsx,auro,pcm…/`metadata_class`(object/bed/channel)、宣言レイアウト、オブジェクト数。構造的カウントには `container_parser`/`bitstream_inspector` 等の構造ソースが必須(リスニング観察やユーザー申告では不可)。
- `RendererCapabilityProfile` — `native_renderer_formats` は非宣言証拠が必須(メーカー文書/デコーダ列挙/機器読み戻し)。max_channels、プロセッサ動作モード、ファームウェア。
- `SpeakerLayoutDeclaration` — `kind`: `content_declared`/`processor_configured`/`physically_installed`/`actually_rendered` の4面を分離。LFE チャネル数と物理サブウーファ数を分離(リダイレクト低音≠サブ)。
- `RenderSession` + `RenderedOutputObservation` — content_ref(kind=`immersive_content_profile`)/config/installed/renderer ref の縛り、観察 outputs(active/silent/mapped_away/unverified per channel)。
- `evaluate_render_path` — `qualified`/`qualified_with_limitations`/`mismatch`/`fallback_only`/`insufficient_evidence`。transported≠source → `transport_fallback`。upmix/virtualized は本物レンダリングと明示区別。`axis_result()` で metadata→decoder→layout→output の各軸を個別診断。

### cad_electrical_noise.py (#606)
- `ElectricalNoiseObservation` — 症状(hum/buzz/mechanical_rattle/distortion/emi_interference/unknown)、スペクトル成分( mains fundamental / harmonic / switching / broadband / unknown )、観測器/チャネル経路/ミュート状態を保存。
- `AudioInterconnectEvidence` — balanced/unbalanced 明示(推定しない)、シールド終端、pin-1(シャーシ入口終端)は manufacturer_doc/inspected 証拠必須。`assumed_from_connector` は shield=unknown を強制。
- `NoiseIsolationTest` — 安全な分離手順(input_disconnect/ground_lift_forbidden は受理しない設計/経路分離など)と結果。
- `HumBuzzDiagnostic` — suspected_causes(ground_loop/emi_pickup/dimmer/hash/psu/mechanical/building_earthing…) と confirmed_cause の分離。`classified_external` は外部権威参照必須、`building_earthing` は電気工事士照会を強制、安全介入確認は日時必須。
- `NoiseMitigationAttempt` + `HumBuzzVerdict` — before/after 観察 ref で監査可能。`protective_earth_preserved: Literal[True]` — PE defeat(アースリフト)を構造的に禁止。
- `evaluate_humbuzz` — `resolved`/`mitigated_unverified`/`diagnosed_unresolved`/`suspected`/`external_referral`/`insufficient_evidence`。スペクトル整合は仮説提示に留め原因確定しない。

### 横断
- 3 repository (fail-closed、sha由来 semantic id、保存時 ref-integrity、冪等再保存)。
- native_row_integrity `_ROW_BINDINGS` ×20、native_authority_audit `_ReplayProbe` ×20。
- **OPS 修正**: `_RepositoryChain._build` に欠落していた `security_authority`/`control_scenario`/`safe_listening` 系 6 ブランチを補完(populated table で KeyError になっていた潜在バグ)。
- application_pages JA ラベル ×20、manifest に 3 issue 分の fixtures/テストマッピング。

## 文献根拠

- NEC NFPA 70 — 連続負荷 80% ディレーティング (210.19(A)(1), 210.20(A))、ブランチ回路容量規定。
- IEEE 802.3af/at/bt — PoE クラス電力天井 (Type1/2/3/4 = 15.4/30/60/90 W)。
- Middle Atlantic / CEDIA-CEST ラック熱設計実務 — BTU 換算 (1 W = 3.412 BTU/h)、温度上昇と機器寿命(Arrhenius 的経験則)、front-to-rear 気流、ブランキングパネルの再循環防止。
- AVIXA Rack Design / CEDIA 電源設計ガイド — 専用回路、突入電流、シーケンシング、UPS 選定(W とランタイム)、サージ保護の分離評価。
- Dolby Atmos for Home Theater / DTS:X / Auro-3D — メタデータ(ベッド+オブジェクト)→レンダラ→レイアウトの経路、スピーカー数と対応フォーマットの非等価性。
- ITU-R BS.2076 (ADM)、ITU-R BS.2127 — イマーシブメタデータ表現とレンダリング義務。
- CEDIA/AVIXA HDMI 実務 — bitstream 伝送確認とデコーダ状態の区別(#583 連携)。
- AES48 (pin-1 / シールド相互接続の設計と試験)、AES54 (接地・シールド実務) — ハム/バズ分離、ground loop vs EMI 誘導 vs 機械ノイズ。
- Ebtech/Eaton/Whirlwind 等の安全ガイダンス — 3線アースリフト(PE defeat)の禁止、安全な分離手順のみを許容。

## 検証
- `test_rev56_infra.py` 30 件 + `test_cad_schema.py` 24 件 + 監査/完全性近傍 58 件グリーン。
- RPT10–80/RND10–70/HUM10–70 フィクスチャで qualified/insufficient/fail-closed の各経路を実測。
- `assert_native_authority_graph` で populated DB の全権威リプレイを実施。

## 残存事項
- 実測温度の継続的テレメトリ(時系列取り込み)は measurement モデルのみで、継続監視の運用面は未実装。
- レンダーパスの LFE/バスマネ実判定は #574 権威への構成子として据置き(本件では宣言・観察の分離のみ)。
- ハム診断の自動スペクトル推定はヒューリスティック(仮説提示のみ)。FFT 確度・機器較正の外部権威(#599)への正式接続は後続。
- UI 配線は JA ラベル登録まで。閲覧専用ページの作成は別 issue 想定。
