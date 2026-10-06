# 空間 RIR ベンチマーク admission 計画（issue #766）

REV59-BENCH2 で採録した admission 計画。dEchorate + MeshRIR を、HTDT の既存 FLAIR/Aalto/BRAS 検証資産を重複させずに補完する空間 RIR 証拠として位置づける。本書は admission 判定のみ — データセットの DL・取込は行わない。

## 対象データセット

### dEchorate
- 出典: https://link.springer.com/article/10.1186/s13636-021-00229-0 / https://zenodo.org/records/6576203 / https://github.com/Chutlhu/dEchorate
- 内容: 校正済みマルチチャネル RIR + 初期反射タイミング注釈 + マイク・実音源・鏡像音源の 3D 位置（壁構成複数）
- 役割: 初期反射照合（#677）、予測↔実測登録（#564）、鏡像源パス検証、吸音壁条件の差分

### MeshRIR
- 出典: https://zenodo.org/records/5472814 / 論文（arXiv 2106.12116）
- 内容: 高密度に測定された RIR グリッド（空間音場解析・合成検証向け）
- 役割: 空間場補間検証（#755）、アレイ/空間サンプリング（#658）、音場再構成 vs 補間の区別

## 採録条件
- ライセンス: dEchorate CC BY / MeshRIR — 取込時に各データセットの正確なライセンス文言を `ExternalStandardsRegistry` 相当の証拠レコードに pin する
- 計量証拠: dEchorate のマイク位置・鏡像源位置は校正済み宣言として扱う（独立再計量なし）。MeshRIR のグリッド座標も宣言値
- 重複回避: 既存 FLAIR/Aalto/BRAS fixture と目的が重ならないことを各 fixture 登録時に宣言

## fixture 化方針（#566 AccuracyEnvelope 準拠）
- 分類: 実測 RIR fixture（VAL60 系スロット相当）— 「解析解」ではなく「実測参照」として取り扱い、単一のグローバルスコアにしない
- holdout 分割は dEchorate の部屋構成・MeshRIR の空間ブロックで事前固定（#698 リーケージ防止 — 隣接グリッド点を校正/検証で混ぜない）
- メトリクス: 初期反射タイミング差・経路照合・空間補間誤差・フィールド再構成誤差を別指標として保持

## 統合面（未実装・残件）
- 実取込パイプライン・fixture 変換・envelope 接続は後続トラック
- #564 登録レコードとの接続（位置・座標系ピン）は取込時に実装
