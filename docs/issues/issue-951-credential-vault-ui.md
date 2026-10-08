# Issue #951 — 資格情報ヴォールト オペレーター UI

## スコープ

#890 で実装した資格情報ヴォールト権威 (`CredentialVaultService` +
封緘 `CredentialReference`/`CredentialLifecycleEvent` テーブル) に、
オペレーター向け UI を追加する。新しいトップレベル workspace は
作らず、既存のサポート宛先内に `CredentialVaultDialog` として
マウントする (権威グラフ・ソルバー診断と同じパターン — 開いている
プロジェクトの `document_id` に束縛)。

## 実装

- `backend/src/htdt/credential_vault_panel.py` (新規)
  - `CredentialVaultPanel` — 参照一覧 (QTreeWidget、
    `credential_id`・スコープ・種別・保管範囲・状態・版・ヒント・
    登録日時のみ、秘密値は列を持たない) + ライフサイクル監査ログ
    (同意・登録・回転・失効・削除・利用・取得拒否)。
    ヴォールト状態バナーが `unlocked` / `locked` / `unavailable` を
    正直に表示し、locked/unavailable では登録・回転・失効・削除を
    無効化 (参照・監査・コピー系は読み取りのみ継続)。
  - `CredentialStoreDialog` — 新規登録。初回は同意チェックボックスが
    必須で、パネル側が `consent_recorded` を封緘してから
    `store_credential` を呼ぶ。秘密フィールドは password echo、
    `take_secret()` が一度だけ値を返して即クリア。`identity_hint` は
    非シークレット (封緘モデルが secret 形状を拒否)。
  - `CredentialRotateDialog` — `credential_id` を保ったまま新版へ
    差し替え (参照行 append-only、version+1)。
  - `CredentialConfirmDialog` — 失効/削除の確認。#946 の機器適用
    one-shot 承認とは別物: こちらはヴォールトのライフサイクル
    イベントを封緘するだけで、デプロイ承認の混同はしない。
  - `CredentialReferencePickerDialog` — アダプタ設定向けの参照
    ピッカー。返すのは `credential_id` のみ (値は一切返さない)。
    active 行だけを列挙し、`expected_scope_kind`/`expected_scope_ref`
    指定時はスコープ不一致を actionable エラーでブロック。
  - `CredentialVaultDialog` — モーダルホスト。サポート宛先の
    「資格情報ヴォールト」ボタンから開く。
- `CredentialVaultService` に読み取り専用の薄い面を追加:
  `vault_state()` / `has_consent()` / `list_references()` /
  `list_events()` — いずれもメタデータのみ、秘密素材は関与しない。
- `SupportPage` に `open_credential_vault` コールバックと
  `supportOpenCredentialVault` ボタンを追加。
- `workflow_application._open_credential_vault` で
  `CadCredentialVaultRepository(self.repository)` +
  `platform_vault()` + `CredentialVaultService` を open 時に構成
  (`_LAZY_IMPORTS` 経由 — ブートコストを増やさない)。

## 非シークレット保証

- UI が表示・コピーするのは参照 ID とメタデータだけ。「参照IDを
  コピー」は `credential_id`、`監査マニフェストをコピー」は
  `export_manifest` (vault_key を含まない) の JSON。
- テストで回帰: ツリー/監査テキスト、クリップボード、
  `export_manifest` repr、DB `payload_json` の全行に秘密値が
  存在しないこと。
- #884 サポートバンドル経路への `build_redactor` 配線は #890 の
  残件として継続 (本 issue ではバンドル側は既存の正規表現
  スクラビングのまま)。

## エラー表面

`CredentialAuthRequiredError` の actionable メッセージ (revoked /
deleted / locked / unavailable / not found) を「取得を検証」と各
操作失敗でそのまま表示。スコープ不一致はピッカー側で
ブロックする (対象に合う参照を選び直すよう誘導)。

## 残存ゲート

- Windows DPAPI 実機でのアカウント切替 (user scope ↔ machine scope、
  別アカウントでの unreadable 挙動) は親 #890 の physical gate で
  管理 — この VM では検証不能。

## 検証

- `backend/tests/test_issue_951_credential_vault_ui.py` — オフスクリーン
  Qt: store 同意ゲート/echo/clear、登録・回転・失効・削除の
  サービス駆動、参照一覧メタデータのみ、ピッカー (ID返却・
  revoked/deleted 非表示・スコープドリフトブロック)、locked /
  unavailable の正直な無効化、監査イベント列、クリップボード /
  manifest / DB の秘密値非含有、resolver との参照 ID 互換、
  資格情報なし旧プロジェクトの空表示。
