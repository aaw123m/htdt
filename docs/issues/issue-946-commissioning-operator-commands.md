# Issue #946 — CommissioningPanel オペレータコマンド配線

`commissioning_panel.py` を読み取り専用表示から、#868
クローズドループ・オーケストレータを駆動する操作パネルへ接続する。
新しい状態機械は作らない — 全ての「次に実行可能な操作」は
`CommissioningRunState` + 許可遷移 (`next_permitted_actions`) から
導出される。

## 構成

- `commissioning_operations.py` (新規) — コマンドポート。
  `CommissioningOperatorController` が `advance / authorize_deploy /
  deploy / readback / rollback / restore_connectivity / cancel` を
  既存オーケストレータサービスへ委譲する。コントローラは真実を持たず、
  有効/無効は許可遷移 + サービス可用性から都度導出する。
- `CommissioningOperatorServices` — 注入境界。アダプタ /
  バインディング / マニフェスト / プラン / エクスポート / 宣言 /
  測定リクエスト / 前後比較入力は全て `None` 可能であり、欠けた入力は
  日本語の disabled reason として正直に表示される (成功を捏造しない)。
- `commissioning_panel.py` — 7 ボタンのコマンド行 + 結果ラベル +
  `CommissioningApprovalDialog`。デバイス変異 (deploy/rollback) および
  承認では、対象デバイス / シーン内容ハッシュ / 校正候補 /
  エクスポート / マテリアライゼーション / 変更差分 / 承認スコープを
  モーダルに列挙し、ワンショット性を明示してから実行する。

## 承認・ワンショット性

- `deploy_apply` 承認は校正候補セマンティック SHA
  (`export.requested_plan_semantic_sha256`) にピンされる。パネルが
  オペレータ ID を収集し、コントローラが既存の未消費・未期限切れ
  承認を再利用するか新規に封緘する。
- 消費済み / 期限切れ / 別候補ピン / スコープ不一致の承認は
  `_find_deploy_authorization` が選ばない — 拒否はシール済み
  `rejected` 遷移としてオーケストレータが記録する (fail-closed)。
- `rollback_apply` 承認は解決済みデプロイメントレコードにピンされる。
- ブロックされた適用試行 (apply_error) は承認を消費しない — 機械の
  既存のワンショット規則 (`deploy_acked`/`advanced` 証跡参照) に従う。

## 結果語彙の正直さ

- apply 結果と readback/verification は混同しない — 機械固有の
  `deploy_acked` と `readback_evaluated` を別コマンド・別ステージとして
  残し、partial-write / readback 不可 / rollback 失敗は機械の
  遷移理由 (`apply_error:` / `diverged` / `rollback_verify_failed:`) を
  そのまま結果ラベルへ表示する。
- 再開時の正直さ: compile 済みマテリアライゼーションはセッション内
  キャッシュのみ。再起動後に新しいコントローラを作るとデプロイ操作は
  「コンパイル結果がありません」として正直に無効化される — 既存の
  封緘モデルでは再導出できないため、捏造しない。

## 失敗経路

- パネルクリック時にも有効性を再確認するため、古いパネルは
  stale な押下で例外を起こさず `unavailable` 結果を返す。
- `_execute` の busy ガードで二重実行を防止し、ボタンはコマンド実行中
  全て無効化される。
- 切断 (`report_disconnect`) で許可遷移は restore/abort のみへ畳まれ、
  復旧 (`restore_connectivity`) で通常操作へ戻る。abort は到達可能な
  全ステージから `cancel` で発行できる。

## テスト

`backend/tests/test_issue_946_commissioning_ops.py` — コントローラ
(正規経路フルラダー / ワンショット / 誤候補・期限切れ承認の非再利用 /
apply 失敗 / readback 差異 / rollback 往復 / abort / 切断復旧 /
busy ガード / 再開時のマテリアライゼーション喪失) と
オフスクリーンパネル (ボタン有効性 / advance 駆動 / 承認・デプロイ・
ロールバック・キャンセルダイアログ / 拒否時に遷移を出さない /
ダイアログ OK ゲート)。

実機 DSP/AVR での物理受入と音響改善証跡は #868/#878/#801 の
物理ゲートに残る — 本 issue の検証はソフトウェア E2E のみ。
