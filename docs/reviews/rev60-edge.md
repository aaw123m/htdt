# REV60-EDGE レビュー記録 — 超低域音響 + 外装遮音 + 材料防火証拠 + アクセシブル再生

対象 issue: #779 (P1), #781 (P1), #782 (P1), #783 (P1)
着地: schema v76、新規 16 テーブル、リポジトリ `cad_edge_repository`、回帰テスト `test_rev60_edge.py` 45 件

## 実装権威

### #779 `cad_ulf_acoustics.py` — サブ 20Hz / 超低音権威
- `UltraLowFrequencyAcousticProfile` (ulfap-): 要求帯域（0 < low < high ≤ 20Hz）・分解能・室境界状態（ドア/窓/HVAC/漏気/接続容積）・ソルバー検証下限を pin。帯域外は推論しない
- `InfrasonicMeasurementCapability` (imc-): 測定チェーンの校正下限・インタフェース状態（AC 結合・隠れ HPF・DC 除去・AGC・前処理）・ノイズ床・過負荷限界を pin。`ulf_calibrated` は校正ファイル ref + 校正規格 + valid_low_hz 必須 — 民生チェーンは超低域を騙れない
- `ULFAcousticObservation` (ulfo-): 量種別・実測カバー帯域・不確かさ・座席/ソース文脈を pin。`g_weighted_level` は `ISO 7196@<edition>` 参照必須 — 無印「dB」は G を意味しない
- `ULFSystemQualification` (ulfq-): 実測で届いた下限 `measured_low_hz` のみ claim。`measured`/`measured_with_limitations` は観測 ref + カバー整合必須。曝露解釈は常に `delegated_to_issue_602`
- `evaluate_ulf_claim`: ソルバー検証域を下回る要求・可聴帯域のみのチェーン・チェーン下限超過・帯域カバーなしを fail-closed に分解。可聴域の確認は超低域能力を意味しない

### #781 `cad_noise_ingress.py` — 外部騒音侵入 / 外装遮音権威
- `ExternalNoiseIngressScenario` (enis-): 音源種別（道路/鉄道/航空/室外機等）・帯域別スペクトル・時間統計・入射条件・稼働状態・気象注記を pin — 「静かな通り」はシナリオではない
- `FacadeTransmissionModel` (ftm-): 外装要素（壁・窓・扉・屋根・ベント・貫通・接合部）ごとの帯域別透過損失 + 方法（lab/field/予測/宣言）+ 規格 pin + 設置状態を列挙。`field_observed_state`/`qualified_state` は as_built ref 必須
- `ExternalNoiseIngressMeasurement` (enim-): 測定クラス（facade_insulation/indoor_receiving/background_noise）を分離。50Hz 未満の帯域は `standard_method_domain` を名乗れず（ISO 16283-3:2016 の検証域）、ISO/CD 16283-3 Ed.2 など draft は `draft_research_only` で標準域の根拠にできない
- `IndoorNoiseIngressQualification` (iniq-): 予測 vs 実測帯域・主制限経路・不確かさ・プロジェクト基準（#580 室騒音プロファイル等への ref）を束縛。`stale_after_envelope_change` は staling ref 必須
- `evaluate_ingress_claim`: 設計包絡のみ・実測なしは `design_model_only`。検証域外・陳腐化・基準不適合を fail-closed に分解

### #782 `cad_material_fire_safety.py` — 材料防火証拠権威
- `FireSafetyEvidenceProfile` (fsep-): 法域・occupancy/project 区分・採用規範（`standard_id@edition` pin）・承認状態を権威化。決定済み状態は reviewer_ref 必須 — 「要求なし」は省略ではなく決定
- `MaterialReactionToFireEvidence` (mrfe-): 試験体 vs 実装の正確な build-up（基材・facing・backing・接着・下地・塗装・厚さ・密度・向き・空隙・取付・接合）を別々に pin。規格版のライフサイクル（current/withdrawn/amendment/draft）を分離。宣言・データシート・draft 規格は `directly_applicable` 不可、試験・分類種別は report_ref 必須 — 販売文言は分類ではない
- `InstalledMaterialSafetyRequirement` (imsr-): 要求証拠種別・充足 ref・代替履歴を pin。`required_evidence_present` 系は satisfied_by_refs 必須、`stale_after_substitution` は staling/substitution ref 必須
- `FireSafetyApprovalReference` (fsar-): AHJ/防火技術者の決定参照。決定済み verdict は支持文書 pin 必須 — HTDT は承認を捏造しない
- `evaluate_material_deployability`: 証拠欠・構成不一致・陳腐化・承認待ちを fail-closed に分解。音響・幾何適合は設置安全を意味しない

### #783 `cad_accessible_media.py` — アクセシブルメディア再生権威
- `AccessibleMediaProfile` (amp-): コンテンツ単位で宣言コンポーネント（字幕/SDH/音声解説/手話等）を track_id・言語・形式プロファイル（CTA-708-E S-2023 / IMSC 1.3 等 pin）付きで列挙。ユーザー選好は `private_user_state` 既定 — verdict 文言に漏れない
- `CaptionPresentationObservation` (cpo-): 到達段階を `content_component_present`→`player_component_discovered`→`user_profile_selected`→`rendered_played`→`presentation_verified` で pin。verified は display_profile_ref + `fully_visible` + 観測 onset/offset 必須 — ビットストリーム内存在は提示ではない
- `AudioDescriptionPlaybackObservation` (adpo-): トラック・コーデック・ミックス意味論（premixed/player-mixed/personalization）を分離。`verified_at_output` は output_path_ref + 測定方法必須
- `AccessiblePlaybackQualification` (apq-): 観測 ref・スタック同一性（機器/fw/app/表示モード）・フォールバック持続性（cold_start/standby_resume/app_update 等で preference_survived）を束縛。`stale_after_change` は staling ref 必須
- `evaluate_accessible_playback`: 提示段階未到達・AD ルーティング喪失・設定非持続を fail-closed に分解。verdict は証拠が届いた段階までしか昇らない

## 文献根拠
- #779: ISO 7196:1995（G 加重）、IEC TR 61094-10:2022（超低音マイク校正）、ANSI/CTA-2010-C（サブウーファー LF 測定）、Møller & Pedersen 2004 他の超低音知覚研究（曝露解釈は #602 へ委譲 — 本権威では計算しない）
- #781: ISO 16283-3:2016（外装遮音実測 — 検証域 ≥50Hz）、ISO/CD 16283-3 Ed.2（draft — research-only）、ISO 12354-3:2017（建物音響予測）、ISO 717-1:2020（等級評価）、WHO Environmental Noise Guidelines 2018（文脈のみ）
- #782: ISO 11925-2:2026（着火性 — 2020 版を置換）、ISO 5660-1:2015+Amd.1:2019（コーンカロリメータ熱放出）、NFPA 701:2023（繊維・フィルム）、NFPA 286:2023（室内コーナー）、ASTM E84-26a（表面燃焼 — #648 `cad_fire_evidence` と重複なく合成）
- #783: ISO/IEC 20071-20:2025・20071-23:2018（アクセシブル ICT/メディア指針）、ISO/IEC TS 20071-21:2015（字幕提示）、ANSI/CTA-708-E S-2023（DTV クローズドキャプション）、W3C IMSC Text Profile 1.3（#733 `cad_timed_text` と重複なく合成）

## 検証
- `test_rev60_edge.py` 45 テスト + fresh-migrate・roundtrip・column-tamper + `test_cad_schema`/`test_native_row_integrity`/`test_authority_audit_coverage`/`test_native_authority_audit`/`test_issue_verification` 隣接 suite リグレッション確認
- 登録面: `NATIVE_SCHEMA_TABLES` + DDL 16 + `_migrate_75_to_76` + `_ROW_BINDINGS` 16 + `edge` ブランチ + `_ReplayProbe`×16 + lifecycle labels 16 + JA 行 4（`ulf_capability_line`/`noise_ingress_line`/`material_fire_safety_line`/`accessible_playback_line`）+ manifest 4 issue

## 残件
- 実測チェーン校正ファイルの実機取り込み・外装測定の現地ワークフロー・AHJ 提出パッケージ生成・プレイヤー側プレゼンテーション実測パイプラインは残件。HTDT が防火適合・遮断性能・アクセシビリティ適合を自ら宣言することはない（専門家/AHJ/実測レコードへの参照に留まる）
