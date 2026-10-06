# REV59-ROOMQ レビュー記録 — G値・共振吸音・保守性権威

対象 issue: #761 (P1-validation), #704 (P1), #707 (P1)
着地: schema v58、新規 6 テーブル、リポジトリ `cad_room_qualification_repository`、回帰テスト `test_rev59_roomq.py` 25 件

## 実装権威

### #761 `cad_sound_strength.py`
- `SoundStrengthObservation` (gobs-): メソッド（reference_source/source_power/IR算出/宣言のみ）+ 帯域 + 音源パワー ref を pin。reference/source_power メソッドは ref 必須、unknown は値を持てない
- `SoundStrengthQualification` (gqual-): verdict を観測 pin に束縛
- `evaluate_g_claim`: 設置 SPL・ルームゲイン・正規化 FR は G ではない。絶対音源参照なしは `g_relative_only`

### #704 `cad_resonant_treatment.py`
- `ResonantAbsorberProfile` (res-): Helmholtz/穿孔/MPP/膜/パネル/スロットを物理パラメータ（キャビティ深さ・穿孔率・孔径・パネル質量）で宣言。'porous' は #615 に委譲して拒否、共振系は物理パラメータ必須
- `ResonantPerformanceRecord` (rpr-): 測定/算出の派生を pin、'declared_only' は測定値を持てない
- `evaluate_resonance_claim`: 単一オクターブ係数・多孔質等価の claim を拒否

### #707 `cad_serviceability.py`
- `ServiceEnvelopeProfile` (svc-): 必要アクセス項目（フィルタ/コネクタ/ランプ/ループ/締結/装置交換）を機器ごとに pin
- `ServiceAccessObservation` (svo-): 設置状態の reachable/blocked を pin（両属は不可）
- `evaluate_serviceability_claim`: CAD 適合は保守性を意味しない。必須項目の遮断・未検証は verdict を降格

## 文献根拠
- ISO 3382-1/ISO 3741（G・音源パワー）、Cox & D'Antonio・Ingard・Maa（共振吸音理論）、AVIXA performance-verification（保守性）

## 検証
- `test_rev59_roomq.py` 25 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 6 + `_migrate_57_to_58` + `_ROW_BINDINGS` 6 + `room_qualification` ブランチ + `_ReplayProbe`×6 + labels + JA 行 + manifest 3 issue

## 残件
- 参照音源での実測・共振体の実測特性・設置後アクセス検証は実機残件
