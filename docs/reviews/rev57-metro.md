# REV57-METRO — 測定タイムベース/クロック権威 + 再現可能証拠バンドル + 校正ライフサイクル

スコープ: issues #609 (P0), #610 (P0), #611 (P0)
ブランチ: `devin/<ts>-rev57-metro`
スキーマ: native schema v35 → v36（15 テーブル + 15 インデックス追加）

## 実装範囲

### #609 Measurement timebase / clock authority

新規 `backend/src/htdt/cad_timebase_authority.py` +
`cad_timebase_authority_repository.py`（テーブル
`cad_timebase_clock_domains` / `cad_measurement_timebases` /
`cad_timebase_capability_assessments`）。

- `CadClockDomain`（clkdom- 封印）— クロック源 1 系統の宣言。
  `domain_kind`（playback_output / capture_input / input_channel /
  external_word_clock / external_digital_sync / file_generator /
  device_internal / unknown）、`rate_basis`（device_specification /
  driver_report / measured / assumed_nominal / unknown）、
  nominal rate・measurand 実効レート・ドリフト ppm・
  reference class（device_internal / wired_loopback_reference /
  acoustic_timing_reference / digital_reference /
  known_trigger_or_pilot / host_operating_system_clock /
  unknown）を宣言フィールドとして保持。ホスト OS 時計を
  サンプルクロックへ昇格させる道は存在しない — 宣言された reference
  class がそのまま残る。
- `CadMeasurementTimebase`（mtimebase- 封印）— 測定 1 件が
  実際に使ったタイミング基準。clock domain 参照（単一/複数入力）、
  `clock_topology`（common_hardware_clock / digitally_locked /
  shared_interface_unconfirmed / independent_asynchronous /
  file_playback_external_device / network_synced / unknown）、
  `synchronization_method`（hardware_wire / digital_audio_lock /
  software_resample_correction / sequential_anchor_alignment /
  post_hoc_alignment / assumed_same_interface / not_applicable /
  unknown）、ドリフト推定（未推定/測定 ppm）、補正記録
  （raw を保持した派生成果物として）、複数入力同期証拠
  （same ADC clock・最大 inter-channel skew・証拠基盤）、
  `file_playback_external_device` では stimulus 参照必須
  （#874 topology と整合）。
- `evaluate_timebase_capability`（tbcap- 封印）— 8 派生能力を
  fail-closed で評価: magnitude_valid /
  relative_phase_valid_within_capture / absolute_phase_valid /
  inter_channel_phase_valid / absolute_delay_valid /
  relative_delay_valid / vector_averaging_eligible /
  complex_transfer_eligible（各 valid / limited / invalid /
  unknown）。名義レート一致から位相有効を推論しない;
  `shared_interface_unconfirmed` は honest に `unknown` を返し、
  `independent_asynchronous` は位相系を `invalid` に倒す。
  単一入力同期系では vector averaging は valid（REW の標準運用と
  整合）、多入力は確認済み inter-channel 証拠が必須。
- `SequentialTimingAnchor` — 連続掃引セグメント間の繋ぎ方
  （common_reference / stable_loopback / acoustic_reference /
  post_hoc_alignment / magnitude_only / not_applicable /
  unknown）。magnitude_only は位相・遅延・複素伝達を自動で
  limited/invalid に降格。
- timing 不確かさは |rate_ratio − 1| × duration（+未補正残差）
  を導出値として保持 — 既存 #572 uncertainty 権威に送らず
  timebase 自身の評価記録に残す。

### #610 Reproducible evidence bundle / integrity manifest

新規 `backend/src/htdt/cad_evidence_bundle.py` +
`cad_evidence_bundle_repository.py`（テーブル
`cad_evidence_bundles` / `cad_evidence_artifacts` /
`cad_evidence_derivation_edges` / `cad_evidence_attestations` /
`cad_evidence_bundle_validations`）。

- `CadEvidenceBundle`（evbundle- 封印）— id / schema_version /
  purpose / producer / code version ピン / manifest root hash /
  status（draft / finalized / superseded / retracted）/
  supersedes 参照。バンドル修正は append-only — 新バージョンが
  draft を supersedes し、改竄検出は manifest root の再計算比較。
- `CadEvidenceArtifact`（evart- 封印）— manifest 1 要素:
  role（measurement_raw / measurement_derived / configuration /
  code_revision / environment / reference_external /
  standards_profile / presentation_report /
  verification_evidence / validation_evidence / decision_verdict /
  other）、path/URI、内容型、size、sha256、required フラグ、
  rights/redaction 注記。raw vs derived は `rawness_class`
  （raw_ingested / derived_preserved_raw / derived_raw_lost /
  unknown）で分離。
- `CadEvidenceDerivationEdge`（evedge- 封印）— PROV 流の
  派生 DAG 辺（derived_from / generated_by / configured_by /
  validated_by / attested_by）。辺・要素は bundle id に結合。
- manifest root 円環性の解決 — 要素/辺が bundle_id を
  identity に封印し、bundle が要素 sha から root を計算すると
  不動点になるため、要素は **draft** バンドルに結合し、
  `finalize_evidence_bundle()` が root を計算して `supersedes`
  参照付きの新レコードを発行する。validator は
  {bundle_id, supersedes_ref} 両方を成员として認める。
- `validate_evidence_bundle`（evval- 封印）— completeness
  profile（reproduction_core / commissioning_minimum /
  audit_full）に対して fail-closed 検証。状態優先度:
  integrity_failure（ダイジェスト不一致 / root 不一致 /
  derivation 整合失敗 / 解決不能+resolver あり）→
  unresolved_reference → profile_mismatch → incomplete →
  complete_but_external_dependencies → complete_valid。
  unresolved / unverifiable を「失敗」ではなく明示状態として残す。
- `CadEvidenceAttestation`（evatt- 封印）— in-toto 流の
  レイアウト証明（statement kind / subject bundle / 機能 /
  証明者 / payload digest）。BundleCheckResult =
  verified / limited / failed / not_applicable。

### #611 Measurement-instrument calibration lifecycle

新規 `backend/src/htdt/cad_calibration_lifecycle.py` +
`cad_calibration_lifecycle_repository.py`（テーブル
`cad_instrument_instances` / `cad_calibration_events` /
`cad_calibration_interval_policies` /
`cad_instrument_verification_checks` /
`cad_instrument_service_events` /
`cad_instrument_fitness_assessments` /
`cad_out_of_tolerance_reviews`）。

- `CadInstrumentInstance`（instr- 封印）— シリアル単位の
  物理機器実体（型番ではない）。カテゴリ約14種
  （measurement_microphone / spl_meter / audio_analyzer /
  signal_generator / sound_level_calibrator / pistonphone /
  accelerometer / impedance_fixture / voltage_reference /
  temperature_sensor / humidity_sensor / interface /
  other_instrument / unknown）。`service_state`
  （in_service / out_for_service / damaged_pending_review /
  retired / unknown）。
- `CadCalibrationEvent`（calib- 封印）— 校正イベント:
  lab / field / initial / post_service / unknown 種別、
  as-found/as-left 状態、結果（in_tolerance / adjusted /
  out_of_tolerance / limited / failed / unknown）、
  トレーサビリティ（accredited_iso17025 /
  traceable_declared / field_reference / unqualified /
  unknown）、証明書 id、有効期限、校正点・補正値、
  `used_standard_under_development` フラグ（IEC 60942 Ed.5
  系の開発中規格は lab-grade 校正と組み合わせると fail-closed で
  拒否 — research-only）。accredited / traceable_declared は
  provider + certificate_id 必須。
- `CadCorrectionFileBinding` — 補正ファイルを実機・向き・周波数域
  に結合。0°フリーフィールド補正が 90°測定へ無言で適用される
  経路は存在しない; `allows_extrapolation` は明示の限定選択。
- `CadCalibrationIntervalPolicy`（calpol- 封印）— インターバル
  根拠を basis 宣言必須で保持（manufacturer_recommended /
  accreditation_requirement / regulation / usage_based /
  risk_based / project_policy / unknown）。「一律6ヶ月」的な
  ハードコード期限は存在しない。
- `CadInstrumentVerificationCheck`（vcheck- 封印）— 現場
  pre/post チェック（class_1_calibrator 等リファレンス源・
  得値・許容差・偏差）。`drift_exceeds_tolerance` / 失敗は
  無効化ではなく CALIBRATION_REVIEW_REQUIRED への入力。
  IEC 61672-3 定期試験とフィールドチェックは別種として保持。
- `CadInstrumentServiceEvent`（svcevent- 封印）— サービス/
  損傷イベント。`calibration_invalidation`（none /
  suspected / confirmed / unknown）で校正無効化疑義を宣言 —
  suspected/confirmed は fitness を review_required に落とす。
- `evaluate_instrument_fitness`（fitness- 封印）— 指定
  タイムスタンプ時点の適合性を fail-closed 評価:
  無効化サービスイベント → calibration_review_required;
  直近校正在庫 + post-cal OOT チェック → out_of_tolerance;
  ポリシー/証明書期限切れ → calibration_overdue_by_policy;
  それ以外は field_reference / unqualified 校正 →
  fit_with_limitations、accredited + 有効 → fit_for_purpose、
  チェックのみ → check_required、何もなし → unknown。
  期限切れは「物理的故障」ではなくポリシー証拠の問題として
  無効化せず制限付きで残す。
- `CadOutOfToleranceReview`（ootrev- 封印）— OOT 影響レビュー
  （affected measurement refs、判断、軽減）を破壊的訂正でなく
  append 記録として保持。

## 統合・UI 配線

- `cad_schema_ddl.py`: 15 テーブル + 15 インデックス
  （document_id 複合）。`cad_schema.py`: v35→v36 マイグレーション
  + 台帳登録。
- `native_authority_audit.py`: 3 factory 分岐 + 15
  `_ReplayProbe`（sha 再計算で改竄検出経路を監査 replay が検証）。
- `native_row_integrity.py`: 15 テーブル分の `_ROW_BINDINGS`
  （id/sha + 全バインド列の fail-closed 比較 — 1 bit の書換で
  IntegrityError）。
- `application_pages.py` + `measurement_evidence_display.py`:
  タイムベース能力・バンドル検証状態・機器適合性の JA 表示
  ラベル/行（unknown 正直表示含む）。
- `test_rev57_metro.py`: ~44 テスト — fixture シナリオ
  （CLK10–80 / EVB10–80 / CAL10–80 系列）+ リポジトリ
  ラウンドトリップ + 行改竄 fail-closed 検証。

## 文献根拠

- **#609**: REW/ARTA 系の loopback タイミング基準運用
  （wired/acoustic reference を t=0 に据える実務 — #642
  t=0 method 権威と合成可能に分離）。AES17 / ITU-R BS.1770 系の
  「nominal レート一致≠位相同期」原則 — 実効レート (ppm 偏移) を
  別フィールドとして保持。マルチデバイス同期の一般的実務
  （word clock / AES デジタルロック / PTP）を topology 語彙として
  採用; file playback + 外部機器は topology を独立軸として宣言。
- **#610**: BagIt (RFC 8493) の manifest+tagmanifest 構造、
  RO-Crate の研究パッケージ単位、W3C PROV-O の派生 DAG、
  in-toto の attestation/layout モデルを語彙基盤に採用し、
  HTDT 固有の「raw 保持」「completeness profile」「honest
  unresolved」要件を追加。hash 連鎖改竄検出は manifest root の
  再計算一致に帰着。
- **#611**: GUM/JCGM VIM の計量トレーサビリティ連鎖
  （accredited → traceable_declared → field_reference →
  unqualified の段階を ISO/IEC 17025 校正証明書慣行に沿って
  分離）。IEC 61672-3 periodic verification と現場
  field check（IEC 60942 系 calibrator による1点確認）を別
  イベント種に分離。as-found/as-left・OOT impact review は
  ISO 10012 / ANSI/NCSL Z540.3 の校正管理実務に準拠。
  インターバル根拠は ILAC-G24/OIML D10 のリスクベース方針 —
  一律期間のハードコードを排除。IEC 60942 Ed.5（開発中）は
  research-only として lab-grade 経路を構造的に拒否。

## 残存事項

- **レポート生成配線**: bundle の producer/purpose を report
  生成経路が実際に書き込む統合は別 REV スコープ — 現状は権威
  + バリデータ + リポジトリ + JA 表示まで。report 側が
  `finalize_evidence_bundle` を呼ぶ接続点は未実装。
- **timebase 入力の自動採取**: clock domain 宣言は現状 UI/API
  からの明示入力; ドライバ実効レートの自動取得
  （sample rate negotiation 実績）は別 issue の ingest 経路依存。
- **ppm ドリフト実測**: ドリフト推定フィールドと評価は実装済み
  だが、loopback 差分からの自動 ppm 推定アルゴリズムは未実装
  （宣言/手入力経路のみ）。
- **OOT review → measurement 降格**: review 記録は保持するが
  測定側レコードへの逆参照降格マーキングは #572 uncertainty
  権威側の将来接続に委ねる。
- **test_application_pages.py の 2 件の失敗**: REV47-ISS534
  （081cccb4）が `PROJECT_WORKSPACE_IDS` に presentation/video
  を追加した際に本テストを未更新 — main 上で既発の
  stale-test であり本 PR の変更とは無関係。
