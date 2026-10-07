# Issue #833 — 再現性あるローカル/リリース検証ゲート

「この正確なソース revision はソフトウェア検証済みか?」を 1 エントリポイントで回答し、
PASS した証拠をリリース成果物の provenance にバインドする。詳細な運用面は
`docs/RELEASE_VERIFICATION.md` 参照。

## 構成

- `scripts/release_verification_manifest.yaml` (`version: 1`) — 13 の検証クラス
  (dependency-lock / canonical-references / package-boundary / schema-migration /
  authority-invariants / persistence / backup-restore / measurement-evidence /
  recommendation-gating / solver-provenance / golden-path-preflight /
  native-startup-smoke / package-smoke) を宣言順・決定的に列挙。各クラスは
  `required` / `include_in_dev` / `requires_capability` を持ち、チェックは
  `pytest` / `script` の2種。`external_gates` に実室受入・実GPU検証・UX160 実機受入を
  明示 — ソフトウェア verdict がこれらを名乗ることはない。
- `scripts/run_release_verification.py` — manifest を fail-closed でロードし
  (`required` + `requires_capability` の組合せ・未知 capability・重複 check id を拒否)、
  `verify_open_issues.py` の bounded subprocess 機構 (Job Object kill-on-close・attempt
  retry・`_check_env`/`_env_fingerprint`) を再利用して実行。`--profile release|dev`、
  `--classes`、`--dry-run`、`--rerun-failed`。verdict は `passed` / `failed` /
  `incomplete` — 中断・tool error・未実行 required check は絶対に PASS しない。
- `scripts/package_smoke.py` — 任意クラス: `dist-native/HTDT` の `build_info.json` が
  チェックアウト commit と lock sha256 に一致し、`HTDT.exe --version` が同一 revision を
  報告することを検査 (capability `native_package` で skip 制御)。
- `scripts/verification_evidence.ps1` — `Get-VerificationBlock`: evidence JSON を
  fail-closed で検証 (`passed` + `run_completed` + `profile: release` + `coverage: full` +
  commit 一致のみ `verified`)。`build-installer.ps1 -VerificationEvidence` /
  `-RequireVerification` と `build-windows-artifacts.yml` の `run_software_verification`
  input 経由で `htdt-release-manifest/1` の `verification` ブロックに焼き付け。

## 証拠

`release_verification_evidence.json` (schema `htdt-release-verification/1`): revision
(sha/branch/dirty)、manifest sha256、runner 自己 sha256、toolchain (python/pip/環境
fingerprint/lock ファイル sha256)、per-check status/duration/log/skip_reason/attempts、
external_gates、verdict + reasons。加えて `release-verification-<date>.md` と
per-check ログ。`coverage: full` は release profile の全クラス実行のみ。

Refs #833
