# Issue #849 — Windows リリースの Authenticode 署名ステージ

## 背景

HTDT のリリース機械は「改訂 + dirty + ロック + ソフトウェア検証証跡 +
成果物 SHA-256」を既に束縛している（#833）。Windows 実行体の署名
（publisher identity）は別権威であり、個人ビルドの正しさゲートでは
ない — しかし配布用安定ビルドには OS レベルの署名者同一性が要る。

## 実装

### `scripts/sign-release.ps1`（新規）

`signtool` を使う任意の Authenticode ステージ:

- `-Artifacts` — 対象ファイル列（インストーラ exe、ポータブル内部の
  主実行体など明示列挙）
- `-Thumbprint`（証明書ストア、推奨）または `-PfxPath` +
  `HTDT_SIGN_PFX_PASSWORD` 環境変数 — **秘密は絶対に repo/成果物/
  レポートに書かない**
- `-TimestampUrl`（既定 digicert）— `/tr` + `/td sha256`
- sign → `signtool verify /pa` → 署名後 SHA-256 再計算 →
  `<artifact>.signature.json` レポート（status/signer_subject/
  signer_thumbprint/timestamped/pre_sign_sha256/signed_sha256）
- `-RequireSigning` — 1件でも `signed_verified` 以外なら非ゼロ終了

### `scripts/release_signature.py`（新規）

signature-report をリリースマニフェストにマージ:

- `publisher_signature` ブロックは `verification` の**隣** —
  署名は決して正しさ検証を押し上げない（`scope: publisher_identity_only`）
- `artifact_sha256` はディスク上の実ファイルから再計算 — レポートの
  主張ハッシュは信頼しない（不一致ならエラー）
- `signed_verified` のみ signer identity / timestamp を記録
  （失敗・未署名レポートの「署名者」主張は meaningless として null）
- `--require-signed` で非 `signed_verified` を fatal
- レポートスキーマに秘密面を持たない（password/key 系フィールドは
  構造的に拒否）

### `.github/workflows/build-windows-artifacts.yml`

`require_signature` dispatch input を追加。有効時は
`HTDT_CODESIGN_PFX_BASE64`/`HTDT_CODESIGN_PFX_PASSWORD` secrets から
PFX を $RUNNER_TEMP に展開 → sign-release.ps1 → release_signature.py
でマニフェスト更新 → finally で PFX 削除。未設定なら未署名のまま
（manifest は `unsigned`/`unverifiable` と明示）。

## 署名スコープの明示

- 署名対象: インストーラ実行体 + 明示列挙した exe
- **ポータブル ZIP の署名は中身の exe を署名しない** — 内側の
  実行体は zip 化前に dist-native へ別途 sign-release.ps1 を掛ける
- 署名後バイト列が配布対象 → manifest の `artifact_sha256` は署名後値
- pre-sign identity（revision + lock + verification）と
  signed identity（最終成果物 sha256）は別途保持 — 署名をまたぐ
  byte-for-byte 再現性は主張しない

## #725 との境界

#725 はエンジニアリングエビデンスの署名。本件は Windows バイナリの
publisher identity 署名。両者は別概念であり混同しない。

## 検証

`backend/tests/test_issue_849_release_signing.py`（11 件）: マージ
セマンティクス（verification 不変・sha 再計算・signer 必須）、
require-signed fail-closed、unsigned の可視性、レポートの fail-closed
パース・秘密面なし、ワークフロー配線。
