# Issue #848 — エビデンス対応の issue ライフサイクル権威

## 背景

GitHub の open/closed は二値だが、HTDT の完了モデルは二値ではない。
#804/#805/#806/#807/#815/#833 のように「ソフトウェアは着地済み、残るは
手動・物理・構造フォローアップのゲート」の issue が open のまま残り、
外部レビュアが「未実装」と誤読していた。

## 実装

### `scripts/issue_lifecycle_manifest.yaml`（新規）

追跡対象 issue の canonical ライフサイクルメタデータ:

- `lifecycle`: `planned` / `in_progress` / `software_landed` /
  `acceptance_remaining` / `physical_evidence_remaining` /
  `structural_followup_remaining` / `complete` / `withdrawn` / `superseded`
- `landed`: 着地した実装の PR/commit 参照（ソフトウェア着地状態では必須）
- `remaining_gates`: `{kind: manual|physical|structural|verification,
  description}` — 残るゲートを種別ごとに分離
- `evidence_refs`: canonical ドキュメント参照
- `close_when_gates_clear`: ゲート解消でクローズ予定か

### `scripts/issue_lifecycle.py`（新規）

- `load_lifecycle_manifest` — fail-closed パース（未知の lifecycle/ゲート
  種別はエラー終了）
- `drift_findings` — ドリフト検出（報告のみ、GitHub は変更しない）:
  - `landed_without_refs` — 着地状態なのに refs が無い（error）
  - `complete_with_gates` — complete なのに残ゲートあり（error）
  - `superseded_without_target` — superseded に superseded_by なし（error）
  - `closed_but_active` — GitHub closed なのにアクティブ lifecycle（error）
  - `landed_no_gate_not_complete` — 着地済み・残ゲート無し・未 complete（warning）
  - `unclassified` — 検証マニフェストの open issue に lifecycle 記録なし
    （warning、GitHub-closed は除外）
- `classify_issue` — バックログバケット: `implementation_missing` /
  `implementation_present_checks_red` / `gate_remaining` /
  `closeable` / `closed` / `unclassified`
- CLI: `--issues-json` にプリフェッチした GitHub issues JSON を渡せば
  完全オフラインで決定的に再生成できる

## 設計上の約束

- **ソフトウェア緑は完了を意味しない** — 自動チェックが全て passed でも
  remaining_gates があれば `gate_remaining` のまま。
- **物理/手動受理はソフトウェアテストから推測しない** — ゲート種別は
  マニフェストの宣言のみ。
- **自動クローズはしない** — closeable は表示上のバケットであり、
  GitHub への書き込みは行わない。

## 現在の分類結果（2026-10-07 時点）

- `gate_remaining` 18 件: #801/#804/#813（手動・物理）,
  #805/#806/#807/#809/#836（構造フォローアップ）, #789-#793/#810-#815
  （ドメイン受理）
- `closeable` 3 件: #803, #808, #833
- `implementation_missing` 5 件: #832, #838, #839, #848, #849
- 全 open issue のドリフトエラー 0 件

## 検証

`backend/tests/test_issue_848_lifecycle.py`（14 件）: 実マニフェストが
パースされ #848 指名の 6 issue を履歴確認なしで分類できること、
fail-closed パース、全ドリフトルール、バケット判定
（緑ソフトウェアが完了を意味しないこと含む）。
