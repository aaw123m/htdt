# #899 ライフサイクル状態ドリフトの是正 — ソフトウェア残と物理/受入ゲートの分離

## 問題

`scripts/issue_lifecycle_manifest.yaml` が「ソフトウェアがまだ未実装」の
issue を「物理/手動受入のみ残」として追跡していた。#848 の証跡対応
ライフサイクルは状態 + `remaining_gates` の種別 (`manual`/`physical`/
`structural`/`verification`) を持つため、複数ゲート種別は既存語彙で
正直に表現できる — スキーマ拡張は不要だった。

## main での検証 (2026-10-08)

| issue | 確認したソフトウェア残 | 証跡 |
| --- | --- | --- |
| #869 | `WasapiAudioBackend.available()` が `False`、`open_stream()` が `BackendUnavailableError` を送出 — 実デバイス同時再生/録音は未実装 | `backend/src/htdt/cad_sweep_acquisition.py:352-375` |
| #866 | 実 IFC/GLB ソース取込と `GeometryIntakePanel` の Room ワークスペース配線が未実装 (issue 文書「What remains device-only」に明示 + コードで配線なし) | `docs/issues/issue-866-geometry-intake.md` |
| #868 | `commissioning_panel.py` は読み取り専用 (refresh ボタンのみ)。advance/authorize/deploy/readback/rollback を駆動するオペレータ承認付きユーザーコマンドパスは未配線 — パネル自体はデバイスを変更しない設計 | `backend/src/htdt/commissioning_panel.py` |
| #875/#876/#877/#886 | 同一ブロッカー (WASAPI バックエンド未実装) に下流依存 — 各々の physical ゲートは #869 の structural ギャップが解けるまで到達不能 | 同上 |

## 変更

- 上記 7 issue の `lifecycle` を `physical_evidence_remaining`/`acceptance_remaining` → `structural_followup_remaining` に是正。
- `remaining_gates` を `structural` (具体的な未実装ソフトウェア) と `physical`/`manual` (ハードウェア/UX 受入) に分割 — 着地済みの `landed.prs` と `evidence_refs` は全て維持。
- #884 は残ゲートなし → `complete` (ローダーに無い状態 `landed` を使っていた事前ドリフトも解消)。
- #886 は `landed` → `structural_followup_remaining` に是正 (WASAPI 下流ブロッカー)。

## 回帰ガード

`backend/tests/test_issue_899_lifecycle_drift.py` が
`load_lifecycle_manifest` 経由で:

1. 検証済み structural issue が `structural` ゲートを必須とし、physical/manual ゲートと併記されること
2. `structural` ゲートを持つエントリが physical/acceptance-only を意味する状態を名乗れないこと (どのエントリにも効く一般則)
3. 着地エントリが landed refs + evidence_refs を保持すること

を主張する。`scripts/issue_lifecycle.py` 本家チェッカーも finding 0 で通過。

## 残

- `unclassified` 警告 (verification manifest に記録があるが lifecycle エントリ無し) は別 issue の既知の残作業。
- WASAPI 実バックエンドの実装は #869 の structural ギャップ本体 — 本修正は追跡の正直化であり、実装そのものではない。
