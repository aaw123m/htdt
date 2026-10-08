# #905 CLF ペイロードファミリの正直な分類 — authoring text vs 配布 binary

## 問題 (issue の一次資料調査より)

`qualify_clf` は UTF-8 テキストのみを前提とし、decode 失敗は一律 `not_clf`
だった。しかしメーカーが実際にエンドユーザーへ配布する CLF は **binary
`.CF1` / `.CF2`** (ODEON 19 manual §3.3、CATT directivity support、
PRONOM fmt/1944 / fmt/1945)。つまり実際の配布ファイルが「CLF ではない」
と誤判定され、text fixture 全緑が製品互換を意味しない状態だった。

## 変更

- `detect_clf_family()` — 3 ファミリ分類: `authoring_text` /
  `binary_cf1` / `binary_cf2` / `unknown`。binary 検出は PRONOM
  シーケンスのみ (lead 0x40/0x41 + `BD 0A 00 01`、offset 20 に
  `v{1,2}.0`)。シグネチャ不成立・切断は `unknown` に fail-closed。
- `ClfVerdict` に `unsupported` 追加 — 認識した `.CF1`/`.CF2` は
  `not_clf` でも `qualified` でもなく「認識したが HTDT に licensed
  decoder が無いため unsupported」を正確に返す。ペイロード自体は
  解析しない (権利境界 — author identity/改変保護を侵さない)。
- `ClfQualification.family` フィールド追加 (既定 `unknown`)。
- `INTERCHANGE_MATRIX` に `binary_cf1`/`binary_cf2` エントリを追加、
  `user_import_candidate` — binary そのものは import 対象外、
  メーカー提供またはユーザーが合法的に取得した tabular 派生物のみ。
- モジュール docstring 修正 — 「free, plain-text interchange」という
  配布形式に関する誤解を招く記述を、authoring text と配布 binary の
  分離として書き換え。

## 権利方針 (#905 準拠)

binary CLF の無断解析/変換は行わない。メーカー提供の合法的
API/変換手段が得られない場合は明示的に `unsupported` を維持し、
ユーザーが合法的に取得した phase/polar tabular 派生物のみを import する。

## 残 (issue 側)

- 実配布 `.CF1`/`.CF2` fixture を許諾確認の上で取得 (取得不可なら
  BLOCKED/NO_FIXTURE 記録) — issue の次アクションとして残る。
- メーカー提供の合法 import/transform 路線の ADR 比較 — 取得可否が
  決まってから。
- end-to-end preflight→admission→directivity の実ファイル検証。
