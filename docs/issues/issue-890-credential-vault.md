# Issue #890 — 資格情報ヴォールト + シークレット参照権威

## スコープ

デバイスアダプタ/委託プロバイダ/AVR LAN transport が増えるにつれて、
パスワード・APIトークン・証明書などの資格情報が蓄積する。
本 issue は「全アダプタが経由する単一のヴォールト抽象」+「プロジェクト
データには非シークレット参照のみを持つ封緘権威」を実装する。

## モデル

`CredentialReference` (封緘、append-only、v107) と `SecretMaterial`
(プロセス内のみ) を分離:

- プロジェクト DB が保持するのは参照のみ: `credential_id` (安定論理ID)、
  `scope_kind`/`scope_ref` (provider/device/service + 対象)、
  `credential_type`、`vault_scope` (user/machine)、`vault_key`
  (`htdt-cred/<uuid>` 不透明ハンドル)、`identity_hint` (非シークレット —
  `pass`/`token`/`secret`/`material` マーカーをバリデータで拒否)。
- `SecretMaterial` は `repr`/`str` が常に `«credential material — redacted»`
  — 値は `reveal()`/`use(fn)` でのみ取り出せるため、ログ/例外への
  偶発的な漏洩を構造的に防ぐ。`fingerprint()` は診断で比較可能な
  非シークレット sha256。
- `CredentialLifecycleEvent` (clev-) が consent/store/rotate/revoke/
  delete/use/deny を記録 — `details` はメタデータのみ (secret 系キーと
  `key`/`value` を拒否)。

## ヴォールトバックエンド

- Windows: `DpapiCredentialVault` — 資格ごとに DPAPI 保護 blob。
  `user` スコープは CurrentUser、`machine` スコープは
  `CRYPTPROTECT_LOCAL_MACHINE`。
- 非 Windows/DPAPI 失敗時: `UnavailableCredentialVault` — 全操作が
  fail-closed (平文フォールバックは存在しない)。
- `platform_vault()` ファクトリで選択。テストは `MemoryCredentialVault`
  (`set_locked()` で locked 状態も再現可能)。

## Fail-closed 規約

- ロック/利用不可ヴォールト、欠落・revoked・deleted 資格は全て
  `CredentialAuthRequiredError` (アクション可能なメッセージ)。
  revoked/deleted への retrieve は `retrieve_denied` イベントを残す。
- 初回 store は当該ドキュメントの `consent_recorded` イベントを要求
  (`CredentialConsentRequiredError`)。
- machine スコープは DPAPI バックエンドのみ — 他では
  `CredentialStorageError`。
- ローテーションは新しい参照行 (version+1、supersedes ピン) — 
  `credential_id` は不変なので下流の証跡/バインドは書き換え不要。
- 参照は append-only: revoked/deleted は tombstone 行で、監査鎖を
  切断しない。

## エクスポート/診断安全性

- `export_manifest(document_id)` は非シークレットメタデータのみ —
  `vault_key` すら含まない。
- `build_redactor(document_id)` はアクティブ資格の素材を全登録した
  `SecretRedactor` を返し、transport transcript/診断/#884 バンドルから
  `«redacted:<credential_id>»` へ置換する。
- CLI/ヘッドレス (#888) は `credential_ref` (credential_id/reference_id/
  vault_key の名前解決) を受け取り、平文引数を要求しない —
  `service.resolver(actor)` が #726/#879 の `credential_resolver` 規約の
  まま差し込める。

## 機械/ユーザースコープ意味論

- `user`: 現在のユーザプロファイル (DPAPI CurrentUser)。同一ユーザでのみ
  解読可。
- `machine`: そのホストの全アカウント (LocalMachine)。再イメージや
  ユーザ切替後は読めない可能性 — プロジェクトのコピー/移行では
  資格はコピーされない (参照のみが移る) ことに注意。

## 残作業 (デバイス/運用のみ)

- 実 Windows マシンでのアカウント切替・再イメージ後の DPAPI 挙動確認。
- HTDT UI 経路からの credential 登録/回転パネル (現状は API のみ)。
- #884 バンドル経路への `build_redactor` の組み込み (バンドル収集側の
  フック)。
