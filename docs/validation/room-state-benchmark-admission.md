# 室状態感度ベンチマーク admission 計画（issue #768）

REV59-BENCH2 で採録した admission 計画。Aalto Motus + Arni を「単一静的室精度」ではなく「室状態変化への感度」検証に用いる。本書は admission 判定のみ — データセットの DL・取込は行わない。

## 対象データセット

### Motus（CC BY 4.0）
- 出典: https://research.aalto.fi/en/datasets/motus-a-dataset-of-higher-order-ambisonic-room-impulse-responses-/ DOI 10.5281/zenodo.4923187
- 内容: 830 家具配置 × 4 音源/受信構成 = 3320 次高次 Ambisonic RIR + 3D モデル + 360°写真（非一様吸収分布、直接パス遮蔽ケースあり）
- 役割: 家具状態感度・占有/非占有効果・非一様吸収分布・直接波遮蔽診断・複数スロープ異常・auralization 状態比較
- 注意: 各家具配置を単一の等価吸収面積スカラーに潰さない（3D/物体状態が利用可能）

### Arni（CC BY 4.0）
- 出典: https://research.aalto.fi/en/datasets/dataset-of-impulse-responses-from-variable-acoustics-room-arni-at-/ DOI 10.5281/zenodo.6985104
- 内容: 5342 吸収構成 × 55 可変パネル、無指向音源 × 5 受信、繰返しスイープ = 132,037 実測 IR + パネル状態行列 + 室/音源/受信メタ
- 役割: 処理状態感度・Sabine/Eyring/統計モデル校正・同室多構成一般化・繰返し再現性・減衰メトリクス頑健性・処理状態推定のオーバーフィット検査
- 注意: 132k IR を 132k 独立室として扱わない — 室/音源/受信構造を共有する反復条件多数（#698 サンプル依存と合成）

## 変化ベース検証メトリクス（A/B 対で保持）
- measured observable A/B、measured delta、predicted observable A/B、predicted delta
- delta 符号正当性・delta 大きさ誤差・構成間順位正当性・不確かさ
- 絶対バイアスがあっても介入効果を正しく予測するモデルはあり得る — 絶対精度と状態変化感度を別指標で報告

## Holdout 分割（リーケージ対策）
- Motus: 校正家具レイアウト / holdout レイアウト / 音源受信 holdout / 遮蔽直接パスを challenge ケースとして固定
- Arni: 校正パネル状態組合せ / holdout 組合せ / 受信 holdout / 繰返しスイープは再現性用に分離（構成多様性と混ぜない）
- 相関の強い繰返しスイープを無作為分割して独立室検証と称しない — 分割はチューニング前に固定

## 材料/処理校正境界
- HTDT が一部状態から実効材料パラメータを適合する場合: 適合した状態は CALIBRATION とマーク、材料値は逆推定（#689）のまま、holdout 構成は手つかず、パラメータ変更でソース材料証拠（#570）を書き換えない
- 多数パネル状態への適合は一意性を証明しない — claim を制限

## 統合面（未実装・残件）
- 実取込・fixture 化・envelope 接続は後続トラック
- #566 accuracy envelope・#604 propagation への接続は取込時に実装
