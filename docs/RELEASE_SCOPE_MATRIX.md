# HTDT Release Scope & Capability Maturity Matrix — Issue #735

> 制定: 2026-09-24 / basis: main@e7012185 / 対象: post-0.1 release planning・roadmap convergence

This is the **canonical** release/scope answer to:

> Which capabilities are required for the next product-ready release,
> which may ship as explicit experimental/preview functionality, and
> which are intentionally post-1.0 so the product can converge?

It is release/planning authority, not runtime project truth. It composes
with — never replaces — #655 (domain depth tiers), #521 (dependency
phases), and #723 (Golden Path closed-loop acceptance).

## 1. Two axes — never conflate scope with maturity

### Product scope / release target

| Scope | Meaning |
|---|---|
| `NEXT_RELEASE_REQUIRED` | Must ship, product-ready, in the next stable release |
| `NEXT_RELEASE_OPTIONAL` | May ship if it reaches product-readiness in time; never a blocker |
| `EXPERIMENTAL_PREVIEW` | May be exposed with explicit maturity labeling; production gates stay closed |
| `POST_1_0` | Intentionally after the next stable release |
| `DEFERRED` / `OUT_OF_SCOPE` | Documented but not planned — including NO_GO research remains |

### Capability maturity

| Maturity | Meaning |
|---|---|
| `SPEC_ONLY` | Contract written; no working slice |
| `SOFTWARE_IMPLEMENTED` | Backend authority/tests exist; not yet product-ready |
| `UI_INTEGRATED` | Reachable through the native UI behind normal workflows |
| `WINDOWS_ACCEPTED` | Passed the owned-Windows acceptance gate |
| `NUMERICALLY_VALIDATED` | Passed the quantitative validation gate where physical claims require it |
| `OWNED_ROOM_VALIDATED` | Verified against owned-room evidence |
| `PRODUCTION_QUALIFIED` | Cleared for stable UI claims |
| `NOT_VALIDATED` | Explicitly unvalidated — shown as such |
| `NO_GO` | Retired research; documented evidence only |

A capability may be product-required but blocked, implemented but
experimental, or post-1.0 even when backend code already exists. A
backend schema/repository existing is **not** a product-ready feature.

## 2. Bounded next stable release (decision)

The next stable release (`R-next`) is bounded to the ordinary-project
closed loop the #723 Golden Path exercises:

- workflow-first shell + project library/lifecycle;
- Room CAD (geometry, entities, placements);
- equipment/source basics;
- Measurements (capture import, quality, comparison);
- capability-valid prediction (bounded provider set);
- Optimize/Pareto (screening → reduced set → finalists);
- result trust/readiness surfacing (#727);
- backup/project portability;
- first-run/Help + accessibility baseline (#731).

Research and adjunct features do not automatically become release
blockers merely because an issue exists.

## 3. Experimental feature policy

Experimental surfaces may ship only when:

- exact maturity is labeled in the UI (`EXPERIMENTAL_PREVIEW`);
- production recommendation gates stay closed;
- experimental providers cannot look like default trusted output;
- results remain reopenable if the experimental path is retired;
- the surface can be hidden from ordinary users.

## 4. Stable-release blocker rule

A feature blocks a stable release **only** when it is in the declared
release scope and its required acceptance gate is unmet. The whole
application is never blocked on multi-room/site, cloud collaboration,
every Tier B adjunct, or arbitrary-room research milestones unless the
release explicitly includes them.

## 5. Product-ready definition

For a feature shown as normal/stable UI, the relevant subset of:

- authority correctness;
- native workflow exposure;
- user-facing help/readiness;
- persistence/migration;
- backup/project portability behavior;
- stale/dependency handling;
- failure/recovery behavior;
- Windows interaction acceptance;
- numerical/owned-room validation where quantitative physical claims require it.

## 6. Canonical capability matrix

| Capability | Depth tier (#655) | Target scope | Impl state | UI state | Validation | Owned-room gate | Default-visible | Owner issue |
|---|---|---|---|---|---|---|---|---|
| Shell/workflow-first nav | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | WINDOWS_ACCEPTED* | n/a | yes | #300 |
| Project library/lifecycle | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | WINDOWS_ACCEPTED* | n/a | yes | #602/#607 |
| Room CAD core | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | WINDOWS_ACCEPTED* | pending | yes | #101/#118 |
| Equipment/source basics | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | SPEC_ONLY→WINDOWS | pending | yes | #140/#142 |
| Measurement capture/quality | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | NUMERICALLY_VALIDATED* | pending | yes | #142/#543 |
| Prediction (bounded providers) | A | NEXT_RELEASE_REQUIRED | SOFTWARE_IMPLEMENTED | yes | NUMERICALLY_VALIDATED* | pending | yes | #101 |
| Optimize/Pareto | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | candidate | pending | yes | #118 |
| Result trust/readiness (#727) | A | NEXT_RELEASE_REQUIRED | SOFTWARE_IMPLEMENTED | partial | SPEC_ONLY | n/a | yes | #727 |
| Backup/portability | A | NEXT_RELEASE_REQUIRED | UI_INTEGRATED | yes | WINDOWS_ACCEPTED* | n/a | yes | #603/#620 |
| Golden Path acceptance (#723) | A | NEXT_RELEASE_REQUIRED | SPEC_ONLY | n/a | NOT_VALIDATED | pending | n/a | #723 |
| Accessibility baseline (#731) | A | NEXT_RELEASE_REQUIRED | SOFTWARE_IMPLEMENTED | partial | NOT_VALIDATED | pending | yes | #731 |
| Arbitrary-room R-series | A | POST_1_0 | SOFTWARE_IMPLEMENTED | partial | NOT_VALIDATED | pending | no | #101 R140 |
| Auralization compare (#784) | B | NEXT_RELEASE_OPTIONAL | SOFTWARE_IMPLEMENTED | partial | NUMERICALLY_VALIDATED* | pending | yes | #784 |
| Install drawing + visual QA (#785) | A | NEXT_RELEASE_OPTIONAL | UI_INTEGRATED | yes | WINDOWS_ACCEPTED* | n/a | yes | #644/#785 |
| Compute envelope (#778) | A | NEXT_RELEASE_REQUIRED | SOFTWARE_IMPLEMENTED | partial | SPEC_ONLY | n/a | yes | #778 |
| Validation dashboard (#780) | A | NEXT_RELEASE_OPTIONAL | SOFTWARE_IMPLEMENTED | partial | SPEC_ONLY | n/a | dev-facing | #780 |
| Release matrix (#735, this doc) | — | NEXT_RELEASE_REQUIRED | SOFTWARE_IMPLEMENTED | n/a | n/a | n/a | dev-facing | #735 |
| Site/space hierarchy (#729) | B | POST_1_0 | SOFTWARE_IMPLEMENTED | no | SPEC_ONLY | n/a | no | #729 |
| Field companion (#728) | B | POST_1_0 | SOFTWARE_IMPLEMENTED | no | SPEC_ONLY | pending | no | #728 |
| Cloud/realtime collaboration | — | OUT_OF_SCOPE | SPEC_ONLY | no | n/a | n/a | no | #730 |
| Video/lighting/electrical adjuncts | B | POST_1_0 | SOFTWARE_IMPLEMENTED | partial | mixed | pending | no | #655 tier B |
| Sound isolation multi-room | B | POST_1_0 | SOFTWARE_IMPLEMENTED | partial | NOT_VALIDATED | pending | no | #602/#655 |

`*` = gate pending the next owned-Windows/numeric acceptance batch —
tracked, not claimed. Entries marked `dev-facing` are development/
release surfaces, not ordinary-user UI.

## 7. Golden Path pin

The #723 Golden Path fixture is pinned to the **R-next profile** (§2):
shell → project → Room CAD → equipment → measurement → bounded
prediction → optimize → readiness → backup. New feature issues do not
expand it; a release-profile revision is an explicit decision here.

## 8. GitHub metadata

Milestones/labels (e.g. `release:r-next`, `scope:post-1.0`,
`maturity:experimental`) may mirror this matrix for filtering; the
documented matrix remains canonical — metadata does not replace it.

## 9. Completion / retirement semantics

Work leaves the active roadmap when: completed/accepted, superseded,
intentionally deferred, NO_GO/retired experiment, or post-1.0. An
implemented NO_GO research path remains documented evidence and must
never look like an active stable-release blocker.
