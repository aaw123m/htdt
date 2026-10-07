# Issue #803 — repository-migration traceability repair

Docs + tooling only. No behavior changes, no schema changes, no GitHub issue
renumbering, no history rewriting, no fabricated data.

## Problem

This repository was migrated from `bolph71656-ai/Home-Theater-Digital-Twin`
to `ka0923s-a11y/HTDT` (2026-10-05). Two consequences made bare `#N`
references in canonical docs unsafe:

1. **Both repositories' merge histories live in this git history.** Old-repo
   merges (`from bolph71656-ai/...`) and current-repo merges
   (`from ka0923s-a11y/...`) coexist, and PR numbers overlap (e.g. `#430`,
   `#432`, `#433`, `#527`–`#551`, `#748` each name two different real PRs).
2. **Issue numbers were not preserved.** Only 19 originally-referenced issues
   were imported into the current repo, all renumbered (see below). The
   current repo's `#N` is almost always a *different* object than the
   original repo's `#N` — so a bare `#83` written before the migration can
   silently resolve to an unrelated imported issue.

## Reference model (canon)

| Surface form | Meaning |
|---|---|
| `<owner>/<repo>#<N>` | qualified reference — always required |
| `repo#N1/#N2` | chain — trailing members inherit the leading repo |
| `repo#N1–#N2` | inclusive range — members inherit the leading repo |
| ` (merge <sha8>)` | implementation/merge claim — merge commit on `main` |
| ` (unavailable)` | object cannot be resolved on any reachable repo |
| ` (superseded)` | ref was replaced by a different PR before landing |
| `run-<N>` | diagnostic workflow run — project artifact, not a repo object |
| `（repo unavailable）` | original repository is offline |

`CI repo#N`, `Windows Release Artifact repo#N` are historical execution
records of the deleted GitHub Actions CI (see commit `b47f052`); they carry
`(unavailable)` because the artifacts cannot be fetched.

## Machine-checkable artifacts

- `docs/MIGRATION_REFERENCE_MANIFEST.yaml` — single authority. Every
  referenced `(repo, number, kind)` → status
  (`current | historical | imported | unavailable | superseded`), merge SHA
  where resolvable, `current_mapping` for the 19 imported issues, and a
  `number_collisions` table of verified same-number/different-object pairs.
- `scripts/validate_canonical_references.py` — scans the canonical docs and
  fails on: bare `#N`, refs absent from the manifest, merge claims without
  the recorded SHA (or a wrong SHA), `unavailable`/`superseded` refs
  presented without their marker, `run #N` syntax, ambiguous kinds, and raw
  URLs pointing at the offline original repos.
- `backend/tests/test_issue_803_traceability.py` — unit tests + the real
  `validate_all` corpus gate.

## Canonical-document set (validator scope)

`README.md`, `docs/IMPLEMENTATION_STATUS.md`,
`docs/IMPLEMENTATION_STATUS_ARCHIVE_2026-09-16.md` (frozen snapshot, banner
kept), `docs/IMPLEMENTATION_ROADMAP.md`, `docs/PROJECT_PLAN.md`,
`docs/PRODUCT_DOMAIN_BOUNDARY_CHARTER.md`, `docs/CAD_EDITOR_SPEC.md`,
`docs/CAD_EDITOR_ACCEPTANCE.md`, `docs/UI_DESIGN.md`.

`docs/issues/*.md` are issue-scoped working notes written in the
current-repo namespace, not canonical status documents — out of scope by
design. The manifest itself is YAML and carries no `#N` syntax.

## Inventory results (563 raw occurrences → 238 manifest entries)

| kind | count | notes |
|---|---|---|
| pull_request | 116 | 95 legacy + 21 current |
| issue | 102 | 86 legacy + 9 capture-legacy + 7 current (incl. ranges) |
| ci_run | 16 | all `unavailable` (deleted GitHub Actions) |
| release_artifact | 3 | all `unavailable` |
| diagnostic_run | 1 | `run-76` (R130D trace) |

Status counts: 165 historical, 27 current, 22 unavailable, 21 imported,
3 superseded.

## Imported issue map (original → current)

All under `bolph71656-ai/Home-Theater-Digital-Twin` → `ka0923s-a11y/HTDT`:

`#83→#1 #101→#2 #118→#3 #140→#4 #142→#5 #726→#11 #792→#64 #801→#67
#978→#201 #980→#203 #992→#215 #994→#217 #1034→#254 #1035→#255 #1049→#269
#1057→#277 #1069→#289 #1072→#292 #1082→#302 #1083→#303 #1084→#304`

Import footers read `_Imported from bolph71656-ai/Home-Theater-Digital-Twin#N_`.
All other doc-referenced original issues were **never imported** and are
marked `historical` (subject known from the docs) or `unavailable`.

## Known unresolvable references (no fabrication)

- `bolph71656-ai/Home-Theater-Digital-Twin#37` (PR) — feature-parity policy
  ref; PR object and merge commit unrecoverable. Documented as policy
  history in ROADMAP §2.
- `bolph71656-ai/Home-Theater-Digital-Twin#122` (PR) — workflow-shell
  bridge design; superseded by the
  `bolph71656-ai/Home-Theater-Digital-Twin#127` integration (merge
  `c0210441`).
- `bolph71656-ai/HTDT-Capture#369` — capture-tracking issue; no surviving
  object.
- All `CI repo#N`, `Release Artifact repo#N`, `workflow run repo#4` — the
  deleted Actions history; run IDs preserved as text evidence.
- `ka0923s-a11y/HTDT#544` (PR) — closed **unmerged** upstream; the work
  landed via direct commit `ef85080c`. Marked `superseded`.
- `bolph71656-ai/Home-Theater-Digital-Twin#76` (PR) — draft could not be
  un-drafted; identical head `ce92d6d0…` merged via
  `bolph71656-ai/Home-Theater-Digital-Twin#78` (`b0b56497`). Marked
  `superseded`.
- `bolph71656-ai/Home-Theater-Digital-Twin#131` (PR) — folded into
  `bolph71656-ai/Home-Theater-Digital-Twin#133` integration squash
  `a8cab563`. Marked `superseded`.
- `Issue bolph71656-ai/Home-Theater-Digital-Twin#2`-era objects below
  `#10` — the current `ka0923s-a11y/HTDT#2` (arbitrary-room acoustics) is a
  *different* object; the original cannot be inspected.

## Verified collision examples

`bolph71656-ai/Home-Theater-Digital-Twin#140` (O90 planning) ↔
`ka0923s-a11y/HTDT#140` (drawing correctness);
`bolph71656-ai/Home-Theater-Digital-Twin#538` (cable runs) ↔
`ka0923s-a11y/HTDT#538` (auralization); `#792` adapter matrix ↔ imported
`ka0923s-a11y/HTDT#64` ↔ `ka0923s-a11y/HTDT#792` product-lifecycle;
`bolph71656-ai/Home-Theater-Digital-Twin#803` (integrity hardening) ↔
`ka0923s-a11y/HTDT#803` (this task); `#430/#432/#433/#527–#551` legacy
solver-era PRs ↔ same-numbered current REV-era PRs;
`bolph71656-ai/Home-Theater-Digital-Twin#748` (install batch PR) ↔
`ka0923s-a11y/HTDT#748` (theater lighting issue);
`bolph71656-ai/HTDT-Capture#334` (compat CI) ↔
`ka0923s-a11y/HTDT-Capture#334` (UI audit); `run-76` ↔ `PR #76` ↔
issue `#76` — three different objects sharing the surface number.

The full table is `number_collisions` in the manifest.
