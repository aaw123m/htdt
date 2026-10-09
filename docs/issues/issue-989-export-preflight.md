# Issue #989 — 出力前の機密データ検査（export preflight）

## Scope

`cad_project_data_privacy.py` already ships the canonical authority —
`ProjectDataClassification`, `SensitiveArtifactPolicy`,
`ExportRedactionManifest`, `evaluate_export_eligibility` — and fails
closed for external sharing. What was missing is the human path: every
export surface went straight from "pick a destination" to a worker,
with no review of classifications, send scope, or omit/degrade reasons.

This change adds that review without duplicating export authority:
the preflight inspects exactly what would leave the project, the
operator confirms classifications and scope, the canonical
`evaluate_export_eligibility` + allowlist `ExportRedactionManifest`
gate external output, and a post-export inspection verifies nothing
unapproved shipped. Nothing is auto-sanitized and nothing is ever
transmitted — HTDT writes files only.

## New modules

- `backend/src/htdt/export_preflight.py` — the review engine:
  - `scan_sensitive_text` — heuristic flags (address-like, network
    endpoints, secret-like, serials, customer/credential fields) as
    *review aids*, never a verdict.
  - `analyze_bundle_plan` / `analyze_member_files` /
    `analyze_member_categories` — per-element inventory: kind, size,
    proposed classification, stored classification, risk flags.
  - `PreflightPlan` / `PreflightElement` — the reviewed model;
    `reproducibility()` is `degraded` whenever anything is excluded.
  - `record_confirmations` — persists operator-confirmed
    classifications as sealed `ProjectDataClassification` records
    (human judgement becomes authority).
  - `build_manifest` — allowlist `ExportRedactionManifest` over the
    included refs; raises (blocks the export) when nothing is
    eligible. Excluded elements land in `excluded_refs`, documented —
    never silent.
  - `evaluate_elements` — canonical gate per element; blockers stop
    output on unclassified / scope-mismatch / unknown credential /
    unconfirmed-rights elements.
  - `inspect_exported` + `verdict_payload` + `write_verdict_sidecar` —
    post-export verification: member set must match the approved
    selection, text members are re-scanned, and the verdict lands in
    `<dest>.preflight-verdict.json` (plus `privacy/preflight-verdict.json`
    inside `.htdtproject` archives).
- `backend/src/htdt/export_preflight_dialog.py` — the review dialog:
  per-element rows (kind / size / risk / classification / rights /
  verdict / include), scope radios
  `完全再現用（非公開保存）` vs `外部レビュー用（最小限の項目）`,
  source pin (revision + SHA), exact included/excluded counts and
  bytes, degraded-reproducibility warning, blocked-reason label, and
  出力する / 保留 / キャンセル.

## Surfaces wired

- `.htdtproject` bundle export — collection was split out of
  `export_project_bundle` into `collect_project_bundle` →
  `BundleWritePlan`, so the dialog reviews the exact plan the writer
  serializes; `apply_exclusions` drops withheld tables/assets and
  records `BundleOmission`s. The verdict JSON rides inside the
  archive (`privacy/preflight-verdict.json`, the only added member
  `_validate_bundle_members` accepts).
- Installation handoff — member files are preflighted before the
  content preview; `write_handoff_package` gained `exclude_members`
  (dropped before the manifest is rendered, so digests always match
  the shipped set).
- Analysis export — the three generated members are preflighted
  before the destination prompt; `_write_analysis_export` drops
  excluded members.
- Presentation review/proposal — sealed packages cannot drop
  members, so the gate is whole-or-nothing: any ineligible category
  blocks the export.

Private-archive scope ships everything (display only — the gate is
for humans). External scope persists the manifest and blocks when
the eligibility evaluation reports a blocker.

## Honesty

- No automatic sanitization is claimed anywhere; excluded content is
  listed with reasons, and degraded reproducibility is recorded in
  the manifest omissions + UI (`incomplete/degraded`).
- Unclassified, undeclared/licensed_no_redistribution rights,
  credential_or_secret and unknown_classification elements cannot be
  included in an external share — they are excluded or the export
  stops when nothing eligible remains.
- Source-change safety: if the head revision moves between
  collection and write, the export re-runs the preflight instead of
  shipping stale contents.
- Cancel/hold touches nothing: preflight writes only when the
  operator approves (policy records + manifest are sealed on accept).

## Tests

`backend/tests/test_issue_989_export_preflight.py` (12 tests):
sensitive-fixture scanning (address/serial/secret/customer/
credential flags), unclassified elements lock out external share,
external manifest raises when nothing is eligible, exclusions record
omissions + degraded reproducibility, exported bundle carries and
passes inspection, inspection catches secret content + member
mismatches, dialog renders counts, and the preflight leaves the
source unchanged on cancel.
