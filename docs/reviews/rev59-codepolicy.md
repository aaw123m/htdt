# REV59-CODEPOLICY レビュー記録 — 避難/出入権威 + 照明時間変調 + データプライバシー

対象 issue: #746 (P1), #748 (P1), #722 (P1)
着地: schema v64、新規 13 テーブル、リポジトリ `cad_code_policy_repository`、回帰テスト `test_rev59_codepolicy.py` 44 件

## 実装権威

### #746 `cad_life_safety.py` — 避難・出入路・アクセシビリティ
- `ProjectLifeSafetyProfile` (lsp-): 法域・occupancy・採用規範（`standard_id@edition` 厳密 pin、#599 と合成）・適用決定を権威化。`inferred_from_room_label` による適用推論は新規レコードで拒否（部屋ラベルは適用を決めない）
- `CirculationRoute` (rte-): seat/zone → exit の経路グラフを家具状態ごとに pin。segments は非空・出口要素で終端必須。幅・開口・歩行距離は生量を保持（採否は外部プロファイルの責務）
- `SeatingAccessibilityRequirement` (acr-): 車椅子席・同伴席・同等視線等の要求項目をプロファイルに束縛（適用外なら惰性情報のまま）
- `EgressEvidence` (egx-): 証拠クラス（CAD 導出/現地確認/専門家レビュー/竣工）を pin。`stale_after_change` は変更 ref 必須、unknown 根拠は geometry_only/insufficient/not_applicable のみ
- `ProfessionalApprovalReference` (appr-): 決定済み verdict は支持文書 ref 必須 — HTDT は承認を捏造しない
- `evaluate_egress_claim`: CAD 適合は常に `geometry_only_not_compliance`。適合 claim は宣言済み適用 + 経路 + 非陳腐な現地/レビュー証拠 + 決定済み専門家承認が要る

### #748 `cad_lighting_tlm.py` — 照明時間変調 TLM/TLA
- `DimmingTemporalProfile` (dtp-): 評価時の正確な照明状態（灯具・調光器・シーン・指令/実測調光率・CCT・ウォームアップ・電源状態）を pin。調光状態が変われば資格同一性は新規 — 全点灯結果は低調光シーンへ移行しない
- `TemporalLightWaveform` (tlw-): 生光波形が正準証拠。派生メトリクスは参照に留まり置き換えない。サンプルレート・帯域・照度を pin
- `LightingTLMObservation` (tlmo-): フリッカー/ストロボ/ファントムアレイを別現象として pin。`power_quality_induced` は #738 電源イベント ref 必須（光フリッカーだけでは電源起因と診断しない）
- `LightingTLAAssessment` (tlaa-): メトリクスを方法/版/適用域に束縛。`CIE 249:2022`（被 Cor1 廃止）を拒否、`IEEE 1789-2015`（2026-03-26 Inactive-Reserved）は `historical_ieee1789` 域のみ、`IEC TR 63158:2018` は `svm_indoor_gt100lx` 域のみ、Mp は `phantom_array_research` のみ、`limited_by_corrigendum` verdict は CIE 249:2022-Cor1 のみ
- `evaluate_tla_claim`: 照度適合は `illuminance_is_not_tla`。計測帯域不足・<100lx での SVM（域外）・Cor1 の SVM/Mp 留保・電源品質起因を fail-closed に分解

### #722 `cad_project_data_privacy.py` — データプライバシー・共有
- `ProjectDataClassification` (pdc-): アーティファクト単位の分類（9 区分）。`unknown_classification` は approved 不可・共有 fail-closed、`credential_or_secret` は rationale 必須（秘密の平文は格納しない）。derived_from_refs で派生の機微継承を可視化
- `SensitiveArtifactPolicy` (sap-): view/edit/export/share/delete を別権限とする最小特権。secret/unknown 区分への export/share 権限付与は構成時に拒否
- `ExportRedactionManifest` (erm-): included/excluded の disjunction・redaction（ref, transform, reason）の included 参照・結果バンドル sha256 を pin — 生成物の漏れが検出可能
- `RetentionPolicyRecord` (rtn-): retain_n_days は日数必須、legal_hold は hold ref 必須、`raw_deleted_hash_retained` は削除時刻必須 — 削除は再現性を正直に降格
- `evaluate_export_eligibility`: 未分類/秘密/ライセンス/ポリシー無/マニフェスト外/リダクション要/許可 の 7 ゲート fail-closed

## 文献根拠
- #746: 2024 IBC Ch.10（means of egress・assembly aisles §1030）、ISO 21542:2021（アクセス・出入路・避難）、2010 ADA Standards §221/§802（車椅子席・分散・同等視線）
- #748: CIE 249:2022-Cor1（2026-10-05 発行・2022 版を廃止 — SVM 手法は更なる検証要、phantom-array 枠組みは標準化手法ではない旨留保）、IEC TR 61547-1:2020（客観フリッカーメータ、内在 vs 電源変動起因を区別）、IEC TR 63158:2018（SVM の宣言適用域: 屋内 >100lx）、IEEE 1789-2015（Inactive-Reserved — 履歴参照のみ）、CIE TN 012:2021（測定ガイダンス）
- #722: ISO 19650-5:2020（security-minded 情報管理 — アーキテクチャ指針、認証主張ではない）、ISO/IEC 27701:2025（PIMS の原則）、GDPR/CCPA の同意・最小必要の概念

## 検証
- `test_rev59_codepolicy.py` 44 テスト + fresh-migrate・roundtrip・tamper + 隣接 suite（roomq/deps 56 件）リグレッション確認
- 登録面: `NATIVE_SCHEMA_TABLES` + DDL 13 + `_migrate_62_to_63` + `_ROW_BINDINGS` 13 + `code_policy` ブランチ + `_ReplayProbe`×13 + labels + JA 行 3 + manifest 3 issue

## 残件
- 法域別の実規範条文マッピング・AHJ/専門家ワークフロー UI・実機での波形取得パイプライン・エクスポート生成器への manifest 適用は残件。HTDT が規範適合や ISO 認証を自ら宣言することはない（専門家承認レコードへの参照に留まる）
