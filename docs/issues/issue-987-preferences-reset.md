# #987 設定の全件リセットを差分確認・明示承認付きに

## 変更

- `PreferencesWidget._reset_all` は確認無しに全キーを既定値へ書き込んでいた。
  以下へ変更:
  - `_reset_candidates()` で既定値と異なるキーだけを列挙 (0件なら何もしない)。
  - `PreferencesResetDialog` — カテゴリ別チェックボックス + 行ごとの
    `現在値 → 既定値 (適用の影響: 即時/再起動後/連携設定)`。「選択カテゴリのみ
    初期化」「すべて初期化」「キャンセル」の3択。確認前に store/binding へ
    通知しない (dialog 完了後にのみ `store.update`)。
  - リセット成功後に `元の設定に戻す` ボタンが表示され、非秘密設定の
    スナップショットから `store.update(snapshot)` で復元できる
    (外部機器状態は対象外と tooltip 明記)。
  - `PreferenceNotificationError` は「保存成功＋一部反映失敗」と表示し
    rollback 済とは見なさない。
- 破損/新schema ファイルの sanctioned reset は `SanctionedResetDialog` で
  「`.recovery` に保存される」旨を明示した上でのみ実行。

Refs #987
