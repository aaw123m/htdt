# #952 権利安全な実メーカー CLF .CF1/.CF2 の調査 + 互換性判定

## 調査結果: fixture (発見 / 許諾 / SHA256)

実配布 binary を **5 種特定・取得**。全て `CLF_LICENSED_FIXTURES`
(`htdt.cad_loudspeaker_interchange`) に sealed record として pin。
いずれの供給元も **再配布許諾を明示していない**ため、fixture bytes は
repo に vendor しない — 証跡は SHA256 + size + verbatim permission
context のみ (issue の「保存不可なら hash/期待判定の実行記録」に準拠)。

| fixture_id | ファイル | 出典 | SHA256 (先頭16) | size | 許諾 verbatim |
|---|---|---|---|---|---|
| clfx-ces-cls3100-cf1 | `cls-3100.CF1` (CES Audio CLS-3100, v1 layout) | clfgroup.org/files/dls/181 | `a73f232de176e7a5` | 34476 | "End users can use the free CLF viewer to view the contents of a CLF file. To use the data for a design you need software that can import the binary CLF format data (CF1 and CF2)." (CLF Group FAQ) |
| clfx-ces-cls3100-cf2 | `cls-3100.CF2` (同上, cf2_v1) | clfgroup.org/files/dls/182 | `30490c90f3a6d503` | 334716 | 同上 |
| clfx-genelec-4020a-cf2 | `Genelec Oy-4020A.CF2` (cf2_v1) | genelec.com/simulation-files → assets.ctfassets.net zip | `820ea8d787b01498` | 334040 | "Speakers data are available on for GLL, EASE 3, EASE 4 and CLF softwares." |
| clfx-clfgroup-v2-sample-cf2 | `clf2_v2_passive_broadband_nophase_nofilter.CF2` (cf2_v2) | CLF Viewer v2.1b installer 同梱 sample | `87c4742f0fd465e7` | 334572 | CLF Group FAQ (同上) |

権利状態の正直な整理:

- **use/analysis = granted** (両ソースの verbatim 文言が配布目的を明示)。
- **redistribution = not_stated** — どのページにも再配布条項無し
  (genelec.com terms-of-use リンクは 404)。bytes の repo 取り込み不可。
- **decode = blocked** — binary spec は by-request のみ (CLF spec §14:
  "can be read by ... software outside of the group that can request
  the binary specification")。公開 binary spec / licensed decoder は
  見つからず (CFxLib/free_clf は GitHub/PyPI に所在確認できず、
  製品名マッチなし)。実 payload は非暗号 (plaintext author tag +
  float32 配列) だが、改変 checksum 付き "secured" container であり、
  self-authored decoder は「documented + licensed decode API」要件を
  満たさない → 実装しない。

合法 path (将来): メーカーが発行した tabular 派生物 (viewer の
"Edit | Copy EA Data" export / 公式 EASE export) → `polar_table` /
normalized JSON adapter 経由で `cad_directivity_import` へ。

## 変更 (`cad_loudspeaker_interchange.py`)

- **signature 堅牢化** — lead `[\x40\x41] BD 0A 00` + generation byte
  `[\x01\x02]` + offset-20 marker `v{1,2}.0` で generation↔marker の
  一致を要求 (全 14 kit samples + 実配布 5 種で確認: `01↔v1.0`,
  `02↔v2.0` のみ存在)。旧 regex は `00 02` gen-2 .CF2 を `not_clf` に
  誤判定していたのを修正。`detect_clf_binary_variant()` →
  `cf1_v1` / `cf2_v1` / `cf2_v2` (cf1 の gen-2 レイアウトは実在
  しないため fail-closed)。`ClfQualification.binary_variant` 追加。
- **`inspect_clf_declared_surface()`** — binary の先頭 4 KiB に限定し
  printable-ASCII run (≥4 chars, ≤32 個, ≤256 chars) を抽出する
  viewer-visible surface 読み取り。author/model/measurement note の
  provenance 固定用であり、secured data array は一切解析しない
  (declared であり verified ではない)。
- **`ClfFixtureRecord` + `CLF_LICENSED_FIXTURES`** — 上表の sealed
  registry。`verify_clf_fixture()` は sha256+size+signature-variant の
  三重一致でのみ `matches`。
- **`evaluate_clf_payload()` → `ClfImportEvaluation`** — payload →
  権利 → スキーマ → solver-input の fail-closed pipeline。outcome
  vocabulary: `licensed_ok` / `decode_blocked` / `rights_unknown` /
  `no_licensed_fixture` / `solver_input_insufficient` / `unsupported` /
  `not_clf`。binary は rights 確立後も decode_blocked (licensed
  decoder 非存在) が正直な状態。
- **`ClfSufficiencyColumns`** — #935 audit 契約の 6 列
  (available → rights_admissible → schema_admitted →
  solver_input_sufficient → reproducibly_predicted →
  physically_applicable) × `PASS/PARTIAL/UNSUPPORTED/UNKNOWN/BLOCKED`
  + column ごとの reason 文字列。
- **`CLF_FIELD_DECLARATIONS`** — フィールド別正直テーブル (spec §10/§14
  + ODEON manual のみを根拠、payload から推論しない):
  coordinate convention (spherical az/el grid), angular resolution
  (CF1 10° / CF2 5°), frequency resolution (1/1 vs 1/3 oct), units (SI),
  reference distance (MEASUREMENT-DISTANCE), absolute SPL vs sensitivity
  (BALLOON-REF), phase (CF2 v2 のみ optional), bandwidth window,
  origin/authorship (declared surface のみ readable)。per-file value は
  全て `not_decoded`。
- **`INTERCHANGE_MATRIX`** — binary_cf1/binary_cf2 の basis に
  registry + decode_blocked を明記。
- **interop corpus (#892)** — `clf/vendor-binary-v2.CF2` synthetic stub
  追加 (gen-2 signature を assert: family=`binary_cf2`,
  `binary_variant`=`cf2_v2`)。harness `_lane_clf` の observed に
  `binary_variant` を追加。実 fixture bytes は vendored しない
  (redistribution not_stated) — corpus は引き続き synthetic のみ。

## 実行記録 (実 fixture による再現)

実 bytes を `evaluate_clf_payload()` に通した結果 (2026-10-08,
記録のみ — bytes 非保存):

- `cls-3100.CF1` → outcome `decode_blocked`, variant `cf1_v1`,
  fixture_match `matches`, declared surface に "ETC, Inc., 8780
  Rufing Road, Greenville, IN, US" / "CLS-3100" / "CES Audio" /
  "Anechoic to 40ms" / "Normalized to 1 meter"。columns:
  available=PASS, rights_admissible=PARTIAL (use/analysis granted,
  redistribution not_stated), schema_admitted=UNSUPPORTED,
  solver_input_sufficient=UNKNOWN, reproducibly_predicted=BLOCKED,
  physically_applicable=UNKNOWN。
- `cls-3100.CF2` → 同上 (variant `cf2_v1`)。
- `Genelec Oy-4020A.CF2` → 同上 (variant `cf2_v1`)。
- `clf2_v2_passive_broadband_nophase_nofilter.CF2` → 同上
  (variant `cf2_v2` — 新規 gen-2 signature 実ファイルで検証済)。

## テスト

`backend/tests/test_issue_952_clf_binary_import.py` — 33 tests:

- signature hardening (gen-2 検出、gen↔marker 不一致 fail-closed、
  cf1-gen2 拒絶、truncation)
- licensed-fixture registry 完全性 (CF1+CF2 manufacturer record、
  verbatim permission、sha pattern、variant-family 整合、
  redistribution 非許諾が全 record で一貫)
- declared-surface bound (4KiB 境界外の文字列を読まない)
- evaluator fail-closed: unknown-rights binary → `rights_unknown`;
  pin mismatch → `rights_unknown` + `mismatch` (tamper 証拠として扱い、
  fixture として扱わない); denied record → `no_licensed_fixture`;
  licensed pinned binary → `decode_blocked` + #935 columns
- authoring text: licensed+qualified → `licensed_ok`;
  qualified+no-license → `rights_unknown`;
  licensed+unqualified → `solver_input_insufficient`
- real-manufacturer record lane は Test*Records クラスで synthetic と
  明確に分離

`test_issue_905_clf_families.py` — `_cf` stub の generation byte を
version digit に合わせて修正 (旧 stub は実在しない `01`+`v2.0` 組合
せを生成していた; 全 assertion の意味は不変)。

`test_issue_892_interop_corpus.py` — corpus 実行で新 fixture が
`unsupported_as_declared` verdict で green。

## 残 / 非目標

- binary decode は引き続き存在しない — 公開 spec / licensed decoder が
  得られた場合のみ adapter 実装が可能 (その際 CLF_FIELD_DECLARATIONS の
  per_file_state を実装に合わせて更新)。
- メーカー発行 tabular 派生物の受領時は `polar_table` adapter 経由。
- GLL は opaque のまま (本 issue の範囲外、GLL_BOUNDARY 維持)。
