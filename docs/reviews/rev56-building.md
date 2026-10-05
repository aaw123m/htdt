# REV56-BUILDING — 室間遮音権威 + ラトル修飾 + 座席/占有音響権威

スコープ: issues #576 (P1), #589 (P1), #590 (P1)
ブランチ: `devin/rev56-building`
スキーマ: native schema v31 → v32（13 テーブル + 4 インデックス追加;
v31 は REV56-TRANSPORT/LIFECYCLE が確保済み）

## 実装範囲

### #576 Inter-room sound-isolation qualification authority

新規 `backend/src/htdt/cad_isolation_authority.py` +
`cad_isolation_authority_repository.py`（テーブル
`cad_isolation_elements` / `cad_interroom_scenarios` /
`cad_interroom_field_measurements` / `cad_isolation_calibrations` /
`cad_isolation_qualifications`）。#559 の `cad_sound_isolation.py`
（直接経路の bounded 推定器）の上に qualification 層を追加 —
既存封印レコードは不変。

- `IsolationConstructionElement`（`ise:` 封印）— 建築要素の権威:
  construction_class / 表面密度 / 層数 / キャビティ / スタッド /
  制振分離 / 断熱充填 + `IsolationElementEvidence`（lab_tl_spectrum /
  lab_rating / field_measurement / declared / generic_label）。
  `generic_label`（販売名 'double stud wall' 等）は TL 値も定格も
  持てない — バリデータで拒否。`IsolationJunctionEvidence` は Kij
  数値に lab_measured / standard_table 証拠を強制（推測値不可）。
- `InterRoomIsolationScenario`（`irs:` 封印）— 順序付き
  source→receiving リージョン対 + `IsolationTransmissionPath` の
  経路分解（direct_partition / flanking_wall /
  floor_ceiling_structure / door / window / duct_hvac /
  service_penetration / open_portal / structure_borne / unknown）+
  `IsolationSourceStressProfile`（content class・LFE・バスマネ・
  バンド別ソーススペクトラム — 宣言なしでは受音 SPL 推定不能）+
  `ReceivingRoomCriterion`（証拠とは別層の判定基準）+
  construction ライフサイクル（design_prediction → … →
  qualified_with_limitations）。
- `InterRoomFieldMeasurement`（`irm:` 封印）— ISO 16283-1 型の
  バンド別実測: metric identity（DnT / Dn / R' / D / 受音 SPL —
  絶対に合併しない）、源・受音・暗騒レベル、RT/吸収正規化、室容積、
  開口状態、運用状態 ref（#573 合成点）。`standard_conformant` は
  計算プロパティ — メソッド・位置・暗騒・正規化・容積の最小証拠が
  無い記録は診断として残るが修飾判定を駆動しない。
- `IsolationCalibrationRecord`（`isc:` 封印）— 予測↔実測校正の
  fit/holdout 分離（交差はバリデータ拒否）+ bounded refinement 宣言。
- `SoundIsolationQualification`（`siq:` 封印）—
  `evaluate_isolation_qualification` のみ生成:
  - バンド別 evidence_state（measured / modelled / declared /
    unknown）+ LF ドメイン（standard_rating_domain /
    extended_low_frequency_measured / extended_low_frequency_modelled /
    below_validated_domain — 100 Hz 未満で測定もモデル宣言も無い帯域は
    必ず below_validated_domain で UNKNOWN; Rw/STC の低域外挿は構造的に
    不可能）。
  - 宣言済みだがモデル未搭載の flank / ダクト / 貫通 / 開口 /
    structure-borne / unknown 経路は `unmodelled_path_ids` として列挙、
    modelled バンドを declared に降格（fail-closed）。**測定済みバンドは
    降格しない** — 適合フィールド測定は flanking 込みの合成実態を捕捉
    済み。経路は attribution 診断として `limiting_path_ids` に残る。
  - 受音 SPL 推定 = 宣言ソース帯域レベル − 実測レベル差（DnT/Dn/D）。
    単一数字定格からは絶対に導出しない。
  - ライフサイクルは fail-closed: `field_measured` を自称しても適合
    測定が bind されなければ `as_built_unverified` へ降格;
    `qualified_with_limitations` は全 criterion が評価済みの場合のみ。

### #589 Mechanical rattle / structure-borne noise qualification

新規 `backend/src/htdt/cad_mechanical_noise.py` +
`cad_mechanical_noise_repository.py`（テーブル
`cad_mechanical_noise_tests` / `cad_rattle_events` /
`cad_remediation_actions` / `cad_mechanical_noise_qualifications`）。

- `MechanicalNoiseTest`（`mnt:` 封印）— `NoiseStressStimulus` が
  正確なストレス信号を保持（stepped_sine / slow_sine_sweep /
  band_limited_noise / lfe_program_content / single_tone / other +
  掃引・dwell・レベル・チャンネル・サブ群・バスマネ・EQ・出力状態）。
  `frequency_response_measurement` は `RATTLE_STRESS_STIMULI` に
  含まれない — FR 適合性は「ラトルなし」の証拠にならない
  （fail-closed の核心）。
- `NoiseTestOperatingState` — ドア/窓/キャビネット/家具移動/HVAC/
  占有状態の bind（#573 合成点）。
- `RattleEvent`（`rte:` 封印）— 分類（rattle_impact_chatter /
  buzz_contact_vibration / panel_resonance / fixture_resonance /
  duct_grille_vibration / screen_frame_vibration /
  furniture_cabinet_vibration / door_hardware_vibration /
  electrical_lighting_buzz / loudspeaker_self_noise / unknown —
  確率的、unknown 許容）+ 発症レベル/帯域 + ヒステリシス/間欠 +
  再現性 + 音響証拠 + 振動証拠 + `localization_state` 状態機械
  （suspected → correlated_with_object → confirmed_by_intervention →
  resolved / unresolved）。`confirmed_by_intervention` は
  suspected_object_refs を必須化。`attribution` で室物体と
  スピーカー自由来歪み（#192 境界）を分離 — 未確定は undetermined。
- `VibrationSensorEvidence` — `calibrated` は calibration_ref 必須、
  さもなくば relative_diagnostic に留まる。コンシューマ加速度計は
  自己昇格不能。
- `RemediationAction`（`rma:` 封印）— append-only、as-built revision
  まで追跡可能。`event_refs` は (event_id, sha) 完全 pin。
- `MechanicalNoiseQualification`（`mnq:` 封印）—
  `evaluate_mechanical_noise`:
  - ストレス試験なし → `insufficient_evidence`（FR-only は不可）。
  - 「修復した」は retest 後のみ到達可能 — remediation 有・retest 無し
    → `insufficient_evidence`。
  - `resolved_at_tested_level` / `resolved_with_limitations` /
    `reduced_but_present` / `moved_to_different_frequency` /
    `new_artifact_introduced` / `not_resolved` /
    `insufficient_evidence` の verdict 集合。
  - project_target > tested_max → resolved は resolved_with_limitations
    へ降格、overall は qualified_with_limitations が上限。

### #590 Seating / occupancy acoustic authority

新規 `backend/src/htdt/cad_seating_acoustics.py` +
`cad_seating_acoustics_repository.py`（テーブル
`cad_seat_acoustic_models` / `cad_occupancy_scenarios` /
`cad_clearance_evaluations` / `cad_seating_commissioning_results`）。
#1038 `cad_occupancy_acoustics.py`（solver 参加ゲート）、#546
座席エンティティ、ListenerPoseAuthority と合成。

- `SeatAcousticModel`（`sam:` 封印）— `SeatGeometryEvidence`（source:
  measured_product / cad_derived / generic_assumed / unknown + 座面・
  背もたれ・ヘッドレスト・アーム・リクライニング・エンベロープ +
  occupied エンベロープ）と `SeatAcousticEvidence`（
  lab_measured_seating_block / product_manufacturer /
  literature_generic / in_situ_estimated / calibrated_model /
  user_assumed / unknown + zone block の周長・面積・列間・rake +
  バンド係数）を **別レイヤー** で保持。`geometric_model_state` /
  `acoustic_model_state` は派生プロパティ — 相互昇格不可。
  `measurement_grade=True` は generic/assumed クラスで拒否。
- `ListenerEarAuthority` — 座席参照点とは別権威の耳位置
  （nominal_ear / L・R / yaw / 着座高域 / 位置公差 / 占有者クラス）。
- `OccupancyScenario`（`ocs:` 封印）— state（empty /
  seats_present_unoccupied / design_occupancy / partial_occupancy /
  full_occupancy / custom / unknown）+ 占有・非占有座席集合 +
  `SeatingZoneBlock` + occupant model。`comparability_key` は内容由来
  の派生ハッシュ — `evaluate_occupancy_comparability` が同内容なら
  compatible、差異なら incompatible、いずれか unknown なら unknown
  を返す（測定比較は占有状態一致時のみ）。
- `DirectSoundClearanceEvaluation`（`dce:` 封印）— スピーカー→リスナー
  の幾何学的資格ゲート（eligibility gate — 減衰量は推定しない）。
  `evaluate_clearance_path`: 耳位置未宣言 → `unknown_geometry`;
  occluder box がセグメントと交差 → `occluded`（enumerable
  failure_reason: seat_back / preceding_row / bar_furniture）;
  10cm 公差マージン内の掠め → `clear_with_position_tolerance_risk`;
  さもなくば `clear`。耳端点を含む occluder は経路を塞げない。
- `SeatingCommissioningResult`（`scr:` 封印）— as-built vs 設計:
  `evaluate_commissioning_verdict` が failure reasons を列挙
  （seat_back_occlusion / preceding_row_occlusion /
  bar_furniture_occlusion / ear_height_outside_design_range /
  occupancy_state_mismatch / seating_acoustic_model_unknown）。
  EQ では直達遮断を直せない — コミッショニング失敗理由は機械的に
  読み取れる形で残る。

### 統合・UI 配線

- スキーマ v32 + `_migrate_31_to_32`（idempotent baseline 再実行）。
- `native_row_integrity._ROW_BINDINGS` に 13 テーブルのミラー列バイ
  ンディング（`stimulus.signal_type` / `geometry.source` のネスト
  参照含む）。
- `native_authority_audit` — 13 本の `_ReplayProbe` +
  `isolation_authority` / `mechanical_noise` / `seating_acoustics`
  リポジトリファクトリ。
- `application_pages._LIFECYCLE_TABLE_LABELS` — 13 テーブルの JA
  表示名。
- `scripts/issue_verification_manifest.yaml` — #576/#589/#590 登録
  （pytest チェック + 実測キャンペーン等の manual 残件）。

## 文献根拠（一次情報）

- ISO 16283-1:2014 — 室容積 10–250 m³、50 Hz–5 kHz; 必須帯域
  100–3150 Hz (1/3 oct)、50/63/80 Hz は optional LF extension、
  default + low-frequency 手続。→ `method_profile`、
  `LF_EXTENSION_BANDS_HZ`、`standard_conformant` の最小証拠集合。
- ISO 12354-1:2017 — 詳細モデル 1/3 oct 100–3150 Hz、Annex I で
  50 Hz まで拡張可（要素+接合データが前提）; 直接 Dd + 側路 Ff/Fd/Df
  のエネルギー合成; Annex K 不確かさ。→ 経路タクソノミー、
  `extended_low_frequency_modelled` クラス、declaration-only の
  junction 証拠。
- ISO 717-1:2020 — 単一数字定格は標準帯域の派生ビューであり、
  ~125 Hz 以下の LFE 遮音を語らない。→ `derived_single_number` は
  派生フラグのみ、低域は below_validated_domain。
- CEDIA/CTA-RP22 v1.2 §8.4 — ラトル/構造ノイズの推奨検査手続
  （ストレス再生 + 聴感確認）。→ `RATTLE_STRESS_STIMULI`、
  retest-gated resolution。
- Temme/Brunet/Keele, AES 127th (2009) + AES 129th (2010) —
  rub & buzz / perceptual detection は通常の FR/THD では捉えられない
  刺激依存現象 → 「FR 適合 ≠ ラトルなし」の fail-closed 根拠。
- Lee & Jeong 2024, Building and Environment 255:111465 —
  座席吸音は座席位置・占有状態・アンダーパス形状・背もたれ高・
  着衣で変化 → evidence class 分離と zone block の周長/面積保持。
- Beranek, JASA 99(4) 2458 (1996) + JASA 101(5) (1997) — 占有/非占有
  座席吸音は仕上げ度（lightly/medium/heavily upholstered）で大きく
  異なる → `UpholsteryClass`、占有状態別 evidence。
- Bradley, JASA 99(2) 990 (1996) — 占有座席吸音は試料の
  perimeter/area 比にほぼ線形 → `SeatingZoneBlock.perimeter_m /
  occupied_area_m2` の一次保持。
- CEDIA/CTA-RP22 v1.2 §5.6.3.1 — 座席遮蔽・耳位置の要件 →
  clearance 資格ゲート + ear authority 分離。

## 残存事項

- ISO 12354-1 詳細モデル自体の計算（Kij 行列・側路 R_ij 合成）は
  未実装 — 本権威は宣言・実測・評価を記録し、推定計算は別 issue。
- 実測キャンペーン（フィールド DnT 収集・ストレス再生実施・占有/非
  占有座席吸音の in-situ 測定）は物理作業として残件（manifest に
  manual エントリとして登録済み）。
- #101/#102 ソルバーへの clearance ゲート伝搬、REW 等の測定 ingest
  配線、管理 UI の詳細画面（テーブル表示は JA ラベルで配線済み）は
  別 wave。
- 占有状態のモード/RT 影響の定量モデルは宣言層のみ — 実予測は
  #102 RT 権威との合成が前提。

## 検証

`backend/tests/test_rev56_building.py`（36 テスト）+
`test_cad_schema.py`（v32 ledger）+ row-integrity / audit coverage /
DDL contract / hardening / S8 回帰 — scoped pytest 全グリーン。
