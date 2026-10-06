# REV59-VERAUTO — 実機検証自動化権威

schema v72・`cad_verification_repository`（2ストア）・
`test_rev59_verauto.py`（11テスト）。

実機検証が必要な issue の手順最小化: `VerificationRequirement`
（vrq-）が「どの収集セル（ch ロール×ソース×ターゲット×回数）
がゲートに必要か」「どの evaluator が判定するか」を封印宣言。
ここから MeasurementRunnerPlan を機械導出 — 手動手順は「生成
済みチェックリストを実行する」だけになる。`VerificationClosure`
（vcl-）が導出計画・コミット済みセル・束縛証拠・判定を封印 —

- evaluator で authority_evaluator は evaluator_ref 必須
- closed=True は evaluator_verdict=='passed' のみ
- 必要セル未コミット=cells_incomplete、証拠未束縛=
  evidence_unbound、未合格=verdict_not_passed
- 全て揃って closable — issue クローズは証拠駆動

残件: manifest の pytest チェック→requirement への生成
ブリッジ・REW 実行連携は別途。
