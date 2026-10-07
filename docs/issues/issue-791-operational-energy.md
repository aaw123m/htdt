# Issue #791 — 運用エネルギー / 電源モード観測権威

追加日: 2026-10-06 (REV63)。スキーマ v89。

## 概要

パワーモード・消費電力・ネットワークスタンバイの証拠を、機器の
**正確な状態**(ファームウェア/構成/ネットワーク/PowerManagement)に
束縛して管理する sealed 権威を追加する。「スタンバイ○ワット」の
数値だけでは証拠にならない。

## レコード

| モデル | テーブル | 前置 |
|--------|---------|------|
| `DevicePowerModeObservation` | `cad_device_power_mode_observations` | dpmo- |
| `NetworkedStandbyEvidence` | `cad_networked_standby_evidence` | nse- |
| `OperationalEnergyScenario` | `cad_operational_energy_scenarios` | oes- |
| `EnergyUseDerivation` | `cad_energy_use_derivations` | eud- |

### DevicePowerModeObservation

- `device_state_ref` (#592/#595 の state epoch) にピン — 後の FW や
  クイックスタート/電源管理変更で正確に stale 化できる。
- モード/量(実電力 W、Wh+区間、VA、PF、電流、電圧、遷移 Wh、
  dwell)/状態(構成プリセット・有効 NIC・wake 機能・PM 設定・
  表示/アンプ/無線伝送状態)は一体。
- `standard_profile != 'unknown'` は `standard_reference` に
  `IEC 63474@2026` 形式のエディション・ピン必須。
- IEC 62087 プロファイルの **active 系モード**主張は
  `stimulus_ref` (#608) 必須 — 刺激/媒体なしの active 数値を拒否。

### NetworkedStandbyEvidence

- IEC 63474:2026 のフィールド: 有効 NIC、接続状態、ネットワーク
  利用可否、wake 機能、PM 設定、低電力遷移フラグ、測定長と
  安定化ルール、結果値。
- **ANSI/CTA-6043 は `adopted_base_profile =
  'iec_63474_2023_withdrawn'` のみ許可** — 米国採用は 2023 版の
  コピーであり、IEC 63474:2026 と同一証拠ではない。逆に IEC
  63474:2026 直接結果は adopted base を持たない。

### OperationalEnergyScenario

- `ScenarioComponent` は observation ref + sha ピン + 機器 ID +
  モードを必須 — 常時オンの常夜灯機器がシステム平均に隠れない。

### EnergyUseDerivation

- `derived_schedule` は mode/hours の明示スケジュール + 出典必須
  (平均だけでは不可)。`long_term_meter` はメータレシピ/期間を要求。
- 関税は currency+rate+unit+source の all-or-none。同じ材料から
  `derivation_version` で再計算可能。

## 評価器

- `evaluate_observation_currency(obs, current_state_ref)` —
  未ピン/現行不明は `unknown`、state 不一致は
  `stale_after_state_change`。
- `evaluate_networked_standby_claim(mode, evidence)` — 非
  スタンバイ/証拠なし/プロファイル・低電力・時間・結果値欠落は
  全て fail-closed (`not_networked_standby` /
  `networked_standby_unverified`)。

## 電気的・光学的範囲外の注意

電源モードの値は「この機器がこの状態で何ワット使うか」のみ。
サージ耐性は #789、所内給配電の回路負荷(サービス容量 vs 実際の
プロフィール)は利用用途モデル扱いとして別レイヤで扱う。

テスト: `backend/tests/test_issue_791_operational_energy.py` (17 本)。
