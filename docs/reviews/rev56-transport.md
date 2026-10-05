# REV56-TRANSPORT — A/V遅延権威 + HDMI設計検証 + ネットワークAV伝送

スコープ: issues #582 (P1), #583 (P1), #591 (P1)
ブランチ: `devin/<ts>-rev56-transport`
スキーマ: native schema v29 → v30（16 テーブル + 8 インデックス追加;
v29 は REV56-ELEC が確保 — マージ時に再採番済み）

## 実装範囲

### #582 End-to-end A/V latency authority

新規 `backend/src/htdt/cad_av_latency.py` + `cad_av_latency_repository.py`
（テーブル `cad_av_latency_profiles` / `cad_av_latency_paths` /
`cad_av_latency_path_measurements` / `cad_av_latency_qualifications`）。

- `AVLatencyPath`（path_sha256 封印、path_id+version で版管理）—
  `signal_path` を id+version+sha256 の三点 pin で参照（#570 権威と
  合成アンカー共有）。ステージタクソノミー `LatencyComponentKind`:
  `device_processing` / `filter_implementation_delay` /
  `buffering` / `transport` / `video_frame_processing` /
  `display_scanout_emission` / `audio_output_electrical` /
  `acoustic_propagation`。`acoustic_propagation` はスピーカー間
  アライメントと二重計上しない旨をフィールドで明示。
- 各ステージは nominal / measured / range のいずれか +
  `LatencyEvidenceClass`（`protocol_reported` /
  `manufacturer_declared` / `derived_from_configuration` /
  `electrically_measured` / `acoustically_optically_measured` /
  `unknown`）を保持。値あり必ず evidence_class 必須 —
  `unknown` 証拠での数値はバリデータで拒否。
  `composite_latency_seconds(role)` は未測定ステージがあれば
  None（fail-closed）— "遅延ゼロ" claim は構造的に不可能。
- 符号規約を `audio_late_positive` に固定（正 = 音が映像より遅い）。
  裸の +80 ms などの符号なし記録は受理しない。
- `AVSyncCompensationEntry` — 補正適用箇所
  （source_avr_dsp_audio_delay / source_video_delay /
  display_reported / hdmi_lip_automatic / other 等）。LIP 自動補正は
  `lip_evidence_ref` で HDMI transport 権威 (#1041) の観測を pin。
- `AVLatencyProfile`（`avlatprof:` 封印）— 版付き知覚プロファイル。
  `build_itu_bt1359_profile` / `build_atsc_is191_profile` の2シード。
  detectability 窓は acceptability 窓の内側であることを
  バリデータで強制。
- `AVLatencyPathMeasurement`（`avlatm:` 封印）— 測定権威:
  stimulus/hash、共通クロック、センサー遅延、アルゴリズム版、
  繰り返し数、不確かさ、分布統計（observations と
  min/mean/max/p95 が整合しないと拒否）。`lip_protocol_reported`
  メソッドは物理観測を持てず `protocol_physical_mismatch` で
  「プロトコル申告値と物理残差の不一致」を機械検出。
- `AVLatencyQualification`（`avlatq:` 封印）—
  `evaluate_av_latency_path` のみが生成。測定なし →
  `insufficient_evidence`; path/profile の sha・版不一致 → `stale`;
  プロファイルが物理測定必須で LIP-only → `insufficient_evidence`;
  acceptability 内 → `qualified_within_profile`、外 →
  `exceeds_profile_bounds`。

### #583 HDMI system design & verification

新規 `backend/src/htdt/cad_hdmi_verification.py` +
`cad_hdmi_verification_repository.py`（テーブル
`cad_hdmi_signal_profiles` / `cad_hdmi_edid_artifacts` /
`cad_hdmi_hdcp_observations` / `cad_hdmi_link_observations` /
`cad_hdmi_verification_records` / `cad_hdmi_qualifications` /
`cad_rp28_profiles`）。

- `HDMISignalProfile`（`hdmiprof:` 封印）— 検証前に要求信号を
  明示（解像度/リフレッシュ/HDR/eARC/HDCP/帯域/必須機能集合）。
  `requires_earc` は `required_features` に `earc` を含むこと。
- `EDIDArtifact`（`edid:` 封印）— raw EDID sha256 + 版付き解釈
  （`parser_version` 必須）+ `EDIDInterceptionKind` で
  native_sink / repeated / synthesized / emulated を区別
  （合成 EDID 検出）。
- `HDCPStateObservation`（`hdcpobs:` 封印）— 認証状態の記録のみ。
  回避・解除は一切モデル化しない。`authenticated` は
  negotiated_version 必須。
- `LinkStateObservation`（`linkobs:` 封印）— リンクモード
  （tmds/frl/unknown）と交渉レート。`rate_evidence_class` が
  `unknown`/`marketing` ではレート値を持てない fail-closed。
- `HDMIVerificationRecord`（`hdmi-ver:` 封印）—
  verdict タクソノミー `pass_stable` / `pass_with_limitations` /
  `intermittent` / `fail_negotiation` / `fail_link` /
  `fail_feature` / `insufficient_evidence`。fail/intermittent は
  `failure_reasons`（source/sink/intermediate/cable/edid/hdcp/
  firmware/bandwidth/intermittent）必須、pass_with_limitations は
  `limitations` 必須、pass_stable は failure_reasons 禁止。
  `DiagnosticBypassTest` は独立記録 — bypass 成功は失敗経路の
  証拠を上書きしない。`StressScenario`（intermittent/thermal/
  input_switching 等）を別フィールドで保持。
- `Rp28VerificationProfile`（`rp28:` 封印）—
  `standard_id='cedia-cta-rp28'`。6 ドメイン
  （hdmi_version_feature_set / edid_capability / hdcp /
  link_training / cable_class / mode_verification）の
  `Rp28RequirementDomain` を `seed_rp28_profile()` で空シード —
  RP28 本文はライセンス文書のため要件テキストは利用者が記入
  （`mapped` には requirement_text 必須）。
- `evaluate_hdmi_qualification` — 未検証で `unsupported` を
  置かない。記録なし: 理論適合なら
  `theoretically_supported_unverified`、さもなくば
  `insufficient_evidence`。path sha 不一致 → `stale`。
  pass_stable → `verified`、pass_with_limitations →
  `verified_with_limitations`、intermittent/fail_* → `failed`。

### #591 Networked AV transport qualification

新規 `backend/src/htdt/cad_network_av.py` +
`cad_network_av_repository.py`（テーブル `cad_network_av_paths` /
`cad_network_media_flows` / `cad_network_transport_observations` /
`cad_network_timing_observations` / `cad_network_av_qualifications`）。

- `NetworkAVPath`（path_sha256 封印、path_id+version 版管理）—
  `NetworkNodeDecl`（endpoint/switch/uplink 等 +
  `NetworkPortDecl`: speed/medium/VLAN/PoE/LAG）+
  `NetworkLinkDecl`（from/to port 存在検証、nominal_capacity、
  is_uplink）。
- `NetworkMediaFlow`（`netflow:` 封印）— プロバイダ profile
  （aes67/dante/st2110/avb/ravenna/vendor_other/generic）、
  unicast/multicast（multicast は group 必須、unicast は禁止）、
  codec/チャンネル/サンプルレート/必須帯域・バースト・遅延/
  `ClockRequirement`（ptp_required/ptp_preferred/none）/冗長性。
  burst ≥ sustained を強制。
- `NetworkTransportObservation`（`netobs:` 封印）—
  kind = packet_quality / qos_state / multicast_state /
  redundancy_event / stress_soak / bandwidth_capacity / other。
  カウンタは全て Optional — None = UNKNOWN であり、0 を捏造しない。
  qos_state は `qos_evidence_level`（endpoint_marking_declared /
  switch_policy_observed / end_to_end_behavior_verified）必須、
  stress_soak は duration 必須、redundancy_event は説明必須。
- `NetworkTimingObservation`（`ptpobs:` 封印）— PTP ドメイン/
  grandmaster/offset/path delay/lock state/clock class/フェイル
  オーバー。signed offset は `units_note` 必須（単位混同防止）。
- `NetworkAVQualification`（`netq:` 封印）—
  `evaluate_network_av_qualification` のみ生成。チェック集合
  （capacity/oversubscription/packet_quality/ptp_timing/qos/
  multicast/redundancy）を fail-closed に評価:
  - capacity+packet_quality 両 verified & 全チェック verified/na →
    `media_stream_qualified`; capacity のみ → `control_established`
    まで降格。
  - packet_quality/capacity/oversubscription の失敗（証拠不足）→
    `insufficient_evidence`; ptp_timing/redundancy/multicast/qos
    （要求宣言された機能）の失敗 → `failed`。
  - 観測ゼロ → `insufficient_evidence`。flow sha 不一致 → `stale`。

## 文献根拠

- ITU-R BT.1359-1: 音先行/映像先行の検出域 ~+45/−125 ms・
  許容域 ~+90/−185 ms（sound-advance-positive 規約）→ 本実装では
  `audio_late_positive` に写像し detectability=[−45,+125]、
  acceptability=[−90,+185] ms として `itu_r_bt_1359_1`
  プロファイルにシード。
- ATSC IS-191: 音声は先行 15 ms・遅延 45 ms を超えない →
  `atsc_is_191` プロファイル [−15,+45] ms。
- HDMI 2.2: 96 Gbps / Ultra96 ケーブル / LIP (Latency Indication
  Protocol)。「HDMI 2.2 = LIP 動作」の推論は禁止 — LIP 値は
  `protocol_reported` 証拠としてのみ遅延合成に入り、物理残差との
  不一致は `protocol_physical_mismatch` で検出。
- CEDIA/CTA-RP28 (2025-03): HDMI System Design & Verification
  Recommended Practice — ライセンス文書のため本文は複写せず、
  ドメイン骨格のみ `seed_rp28_profile` で提供。
- AES67-2023 / ST 2110-30 / IEEE 1588 PTP / DSCP QoS / マルチ
  キャスト — QoS 証拠ラダーと PTP 観測モデルの根拠。

## 残存事項

- #582: 実機 LIP readback の自動取り込み（`cad_hdmi_transport`
  の `HDMILatencyIndicationEvidence` からの ingest wiring）は未接続
  — `lip_evidence_ref` は手動 pin。測定ドライバ実装は別 issue。
- #583: RP28 各ドメインの requirement_text は空シード —
  ライセンス本文の転記は利用者側で実施。EDID パーサ実装
  （`parser_version` を持つ実パーサ）も残件。
- #591: スイッチ実測インタフェース（SNMP/sFlow/NETCONF 取り込み）
  は未実装 — observation は宣言的記録のみ。AES67 PTP 実測の
  自動収集も残件。
- UI: `_LIFECYCLE_TABLE_LABELS` の JA ラベルのみ（REV56 系の
  最小配線規約）。専用ページは別タスク。
