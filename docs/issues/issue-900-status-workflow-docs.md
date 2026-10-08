# #900 実装ステータスの workflow 記述を現行 main と整合させる

## 問題 (2026-10-08 確認)

`docs/IMPLEMENTATION_STATUS.md` 冒頭注記が「`.github/workflows` の GitHub
Actions CI はこの mirror では削除済み (commit `b47f052`)」と記述していた
が、現行 main には2つの workflow ファイルが存在する:

- `.github/workflows/build-windows-artifacts.yml` — `workflow_dispatch`
  のみ。`run_software_verification` 入力 (既定 on) で
  `scripts/run_release_verification.py --profile release` を実行し、
  証跡を成果物 manifest に束縛する
- `.github/workflows/verify-open-issues.yml` — `workflow_dispatch` のみ

`docs/RELEASE_VERIFICATION.md` / `docs/ISSUE_VERIFICATION.md` は既に
正しい記述だったため、矛盾は STATUS 冒頭注記に限られた。

## 変更

- 冒頭注記を書き換え: `b47f052` は歴史的削除イベントとして保持しつつ、
  現行の2 workflow を dispatch-only (手動起動) として記述。merge-blocking
  PR CI を置かない意図的ポリシー (#833) を明記。検証 PASS は常に
  revision・runner・evidence 束縛で、workflow ファイルの存在は実行や
  合格を意味しないことを区別した。
- `docs/IMPLEMENTATION_STATUS.md:193` の `ci.yml` 記述は当該ファイル
  個別の歴史として正しいため触らない。

## ドリフトガード

新規 release-verification クラス `workflow-docs-integrity`
(`scripts/release_verification_manifest.yaml`, required, include_in_dev)
が `backend/tests/test_issue_900_workflow_docs_drift.py` を駆動:

1. 現行状態を主張する文脈 (歴史限定子を持たない行) で指名された
   workflow ファイルが実在すること — 削除/改名された workflow を
   現行扱いする記述を fail closed で検出
2. 実在する workflow がいずれかの正本文書で指名されていること —
   文書未記載の workflow も drift
3. 全 workflow が `workflow_dispatch` を持ち、行頭トリガーとしての
   `push:`/`pull_request:`/`pull_request_target:` を持たないこと —
   no-PR-CI ポリシーの退行を検出
4. STATUS 冒頭注記 (現行事実ノート) が `.github/workflows` の
   現行不存在を主張しないこと — 削除主張は歴史限定付きでのみ許可

## 残

- workflow の最終実行結果・PASS 実績は本 issue のスコープ外 — dispatch
  実行の証跡は `release-verification-<run>` アーティファクト側で確認。
