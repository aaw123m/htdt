# REV59-ACOUST3 — 有限吸音体/先行効果/防火証拠

schema v62・`cad_treatment_safety_repository`（6ストア）・
`test_rev59_acoust3.py`（14テスト）。

## #694 有限吸音体の縁・サイズ効果（P1）

- `cad_finite_absorber.py` — `FiniteAbsorberGeometry`（fag-）:
  有限寸法・縁状態（exposed/sealed/baffled/mixed）・取付種別
  （wall_patch/free_hanging_cloud/corner_trap/…）・隣接バッフル・
  パネル間隔を pin。周長/面積比を派生。
- `FiniteTreatmentBoundaryModel`（ftbm-）: 幾何 ref + 反応種別
  （locally/non-locally reacting）+ 縁効果モデル ref。
  `claims_infinite_plane_equivalence` は縁効果証拠 pin 必須。
- `evaluate_finite_absorber_claim` — 5 判定:
  幾何なし → insufficient_geometry、有限物体にモデルなし →
  infinite_plane_misapplied、試料≠設置物 → sample_size_unaccounted
  （ISO 354 の試料サイズ感度）、縁/取付未宣言 →
  edge_effect_unqualified。

## #646 先行効果/エコー知覚リスク（P2）

- `cad_precedence_echo.py` — `PrecedenceProfile`（prec-）:
  刺激種別（speech/music/impulsive/…）を必須宣言 — エコー閾値は
  刺激依存のため 'unknown' は fail-closed。
- `EchoRiskObservation`（erk-）: 遅延/相対レベル/方向分離を
  現象分類（融合・定位優位・識別抑制・エコー分離・像シフト）に
  束縛。融合≠分離の区別を強制。
- `evaluate_echo_risk_claim` — 固定遅延ルール（>20ms=エコー等）
  → fixed_rule_misapplied。物理データのみ → unresolved_risk。

文献根拠: 先行効果レビュー（Litovsky et al. 1999）、Haas 効果、
刺激/遅延/レベル/方向依存のエコー閾値。

## #648 音響仕上げの防火/対火証拠（P2）

- `cad_fire_evidence.py` — `ReactionToFireEvidence`（rtf-）:
  試験規格（astm_e84/nfpa_286/en_13501_1/ul_723/iso_9705）+
  被試験組立体記述 + レポート ref を必須化。規格未特定は
  fail-closed。
- `FinishAssemblySafetyEvidence`（fas-）: 組立部品
  （absorber/fabric_facing/stretched_fabric_system/…）+ 証拠 ref +
  設置文脈。
- `evaluate_fire_eligibility_claim` — 音響適合 →
  acoustic_not_safety、要求規格と不一致 → test_scope_mismatch、
  試験結果のみ（組立未束縛）→ lab_result_is_not_installation。

根拠: ASTM E84-26a（比較的表面燃焼試験 — ASTM 自身が不燃認定・
完全リスク評価でないと明記）、NFPA 286（ルームコーナー寄与 —
 別測定量・別組立文脈）。

## 残件

- 有限吸音体の定量的縁効果モデル（波長 vs パネル寸法）は係数
  テーブル/CFD 検証が要 — 本ラウンドは権威・ゲートのみ。
- エコー閾値の刺激別モデル係数は盲聴実験証拠（#696 連携）が要。
- 防火の管轄区域適合判定は外部 — 本権威は証拠 pin と組立束縛まで。
