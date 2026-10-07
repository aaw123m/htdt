# Issue #865 — Issue-lifecycle evidence integrity

## Scope

`scripts/issue_lifecycle.py` now resolves every repo-local `evidence_ref`
in `scripts/issue_lifecycle_manifest.yaml` and fails closed (error finding,
exit 1) when a reference is missing, renamed, deleted, duplicated, or not
repository-local. Landed/accepted lifecycle states additionally require at
least one resolvable evidence ref.

## Reference classes and resolution rules

Authoritative reference classes:

| class | resolution |
|---|---|
| repo-local file path (`evidence_refs`) | resolved against the lifecycle manifest's own location (`<repo>/scripts/`, so `<repo>/` is the root) or `--repo-root` override — never the caller's cwd. Must be POSIX-style relative; absolute paths, drive letters, `~`, `//` and `..` are rejected as `evidence_ref_not_repo_local` — proof can never live outside the audited tree. |
| `landed.prs` | positive integers (syntactic; GitHub resolution is not claimed offline) |
| `landed.commits` | sha strings 7–64 chars (syntactic) |
| `issue`, `superseded_by` | positive integers; `--issues-json` additionally checks lifecycle entries against the supplied GitHub issue set |

Missing/renamed/deleted path → `evidence_ref_unresolvable` (error).
Repeated ref in one entry → `evidence_ref_duplicate` (error).
Landed state (`software_landed`, `acceptance_remaining`,
`physical_evidence_remaining`, `structural_followup_remaining`,
`complete`) with zero `evidence_refs` → `landed_without_evidence` (error).
Non-landed states (`planned`, `in_progress`) may carry no refs, but any
ref present must still resolve.

Diagnostics name the issue, lifecycle state, the bad ref, and the expected
evidence class in the finding message; `--json` emits the same findings
machine-readably (`findings[].code`, `issue`, `severity`).

## Physical vs software evidence

`evidence_refs` asserts *repository* evidence only. A resolving doc proves
software landed, never physical/device/UX acceptance — those remain
`remaining_gates` of kind `physical`/`manual` and are outside this check
by design (per the issue's non-goals).

## Integration

`scripts/release_verification_manifest.yaml` gained required class
`issue-lifecycle-integrity` (check `lifecycle-evidence-resolution` runs
`issue_lifecycle.py`; `lifecycle-evidence-regression` runs
`backend/tests/test_issue_865_lifecycle_evidence.py`). Any error finding
makes the release-verification run fail — evidence drift can no longer
coast through as a warning.

## Repairs applied with this change

Stale `evidence_refs` corrected: #805 → `issue-805-standards-coverage.md`,
#807 → `issue-807-package-boundaries.md`, #809 →
`issue-809-benchmark-qualification.md`, #812 → `issue-812-hybrid-composition.md`,
#814 → `issue-814-confidence-envelope.md`, #833 →
`issue-833-release-verification-gate.md`. #849 gained
`docs/issues/issue-849-release-signing.md`; #808 (research synthesis,
complete) cites the confidence-model docs its research produced
(#811/#814) — a research issue's conclusion record is its evidence.

## Tests

`backend/tests/test_issue_865_lifecycle_evidence.py` — resolution of
valid refs; missing/renamed/duplicate/repo-escaping refs rejected;
landed-state evidence requirement per state; `main()` exit-1 + JSON output;
and a meta-test asserting the checked-in manifest itself resolves
(Regression canary — fails on the next stale ref).
