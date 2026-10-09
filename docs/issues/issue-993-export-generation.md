# #993 複数ファイル出力を generation ディレクトリとして原子公開

## 問題

`claim_export_stem` + `write_export_files` は:

- stem 採番が `Path.exists()` 事前確認のみ — 同一 stem を並行2ジョブが
  採用すると、チェック後のレースで一方が他方の成果物を置換し得た
  (TOCTOU)。
- メンバは1ファイルずつ `write_text_atomic` → `os.replace` で公開 —
  プロセス強制停止 (kill -9) は except 節を経ず、途中のファイル群が
  完成品として残った。

## 実装 (`backend/src/htdt/export_io.py`)

**`write_export_generation(directory, base, files, *, bom_suffixes, manifest_extra)`**:

1. **予約 = atomic mkdir**: `<stem>.export-staging` を `os.mkdir` —
   作成できた者が stem を所有 (事前存在確認ではこの保証は不可能)。
   staging 名・最終名・旧フラット命名 `<stem>_*` のいずれかが存在すれば
   その stem を skip し、番号は新旧両レイアウトで単調増加を維持。
2. **staging**: 全メンバを staging 内へ atomic 書込、各バイト列を
   sha256+size で `manifest.json` に記録 (最後に書込)。
   `manifest_extra` で呼出元の識別 (export_id 等) をピン。
3. **単一公開点**: `os.rename(staging, <stem>/)` — 最終名は
   「完全な generation」か「存在しない」のどちらかのみ。
4. **fail-closed**: 失敗時は staging ごと削除して例外伝播。
   公開直前に最終名が出現していたら FileExistsError (別アプリの
   同名作成に merge しない)。

**公開済み世代の検証機械**:
- `iter_export_generations(dir, base)` — manifest 付き generation の列挙
- `verify_export_generation(path)` — manifest との再突合 (欠落/破損/
  未記録ファイル混入をそれぞれ名前付きで拒否)
- `find_export_staging` / `cleanup_export_staging` — クラッシュ残置の
  staging を診断可能に列挙・明示的に cleanup (自動回収しない:
  staging は生存 writer の予約でもある)

**旧 API 撤去**: `claim_export_stem` / `write_export_files` を削除し、
`workflow_application` の analysis 出力 (`_write_analysis_export`) と
校正設定出力を新 API に統一 — 旧 API を誤用する余地を残さない。

**命名の変更**: `analysis_export.csv` → `analysis/export.csv`
(generation ディレクトリ内の定数メンバ名)。ユーザーには保存先
フォルダを開いた際「`analysis-N/` フォルダが1回の出力」として
一貫して説明可能。

## テスト (`test_issue_993_export_generation.py`、17件)

- 5並行 writer + barrier → 全件成功・stem 一意・上書き0
- kill 注入 (2ファイル目書込時/rename 直前) → 部分公開0・staging 掃除
- 模擬クラッシュ残置 staging → stem 繰上げ・診断列挙・明示 cleanup
- 最終名 squat・書込不可 dir・manifest 破損/欠落/混入・legacy フラット
  成果物の stem 繰上げ
