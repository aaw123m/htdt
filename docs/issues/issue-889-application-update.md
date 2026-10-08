# #889 — Safe application updater with compatibility preflight, health check and rollback

## Scope

A self-update path for HTDT that never leaves the install in an
unverifiable state:

```
CHECK -> VERIFY PACKAGE -> COMPATIBILITY PREFLIGHT
     -> BACKUP/RECOVERY POINT -> INSTALL -> RESTART
     -> HEALTH CHECK -> ACCEPT / ROLLBACK
```

implemented as a sealed authority (`cad_application_update.py`) plus a
staged-applier service (`ApplicationUpdateService`) persisted through
`cad_application_update_repository.py` (schema v108, tables
`cad_update_*` ×8). Everything the updater decides is recoverable from
a sealed transition log — a crash at any point resumes deterministically
via `derive_update_state`.

Out of scope per the issue: silent forced updates, downgrading projects
across irreversible migrations, treating code signing as proof of
compatibility, and network I/O inside the updater core (all package
ingress is behind the `UpdatePackageSource` protocol).

## Sealed records

| Record | Id | Pins |
|---|---|---|
| `UpdatePackageDescriptor` | `upkg-` | target version, artifact refs **sha256-pinned**, schema floor/ceiling window, migration reversibility, platform/runtime floors, dropped adapter ids, adapter API floor, #849 `UpdateSignatureState`, optional #833 `release_ref` |
| `UpdateSessionRecord` | `upd-` | package ref, install/data/update roots, policy in force (signature policy, release-evidence requirement, channel opt-in, data backup kind), pre-update app/schema identity |
| `UpdateStageTransition` | `utr-` | the append-only transition log — every machine step, rejection, hold and terminal decision |
| `UpdatePreflightReport` | `upre-` | named checks, the derived verdict (validator re-aggregates — a report cannot claim eligibility its checks do not produce), environment snapshot + its sha, forward-only disclosure flag |
| `UpdateRestorePoint` | `urp-` | backup root + manifest sha, install tree sha, every captured item's sha, schema version before, `migration_boundary`, `rollback_scope_capable` |
| `UpdateHealthReport` | `uhc-` | post-swap checks, re-derived verdict, observed version/schema pair |
| `UpdateOperatorAuthorization` | `uauth-` | one-shot apply/rollback grant pinning the exact package + preflight it approved, forward-only acknowledgement |
| `UpdateOutcomeRecord` | `uout-` | terminal verdict, rollback scope + verified flag + restore-point/health/preflight refs |

## Vocabulary

* **Stages**: `opened → package_fetched → package_verified →
  preflight_evaluated → recovery_point_captured → staged → applied →
  health_checked`, then `committed`; branches: `blocked`
  (incompatible/unverifiable preflight), `failed` (tamper or
  unrecoverable step), `cancelled` (only before mutation),
  `rolling_back → rolled_back | rollback_failed`.
* **Preflight verdicts**: `eligible`, `eligible_with_warnings`,
  `incompatible`, `unverifiable`. Aggregation is fail-closed: any `fail`
  blocks, any `unknown` is unverifiable, any `warn` is eligible with
  warnings — nothing is silently eligible.
* **Health verdicts**: `healthy`, `healthy_with_warnings`,
  `unhealthy`, `unverifiable` — only the first two commit.
* **Outcomes**: `committed`, `rolled_back`, `rollback_failed`,
  `blocked`, `failed`, `cancelled`.
* **Rollback scope (honesty bound)**: `not_attempted`, `binary_only`,
  `full`. `binary_only` never claims project/schema state was restored —
  it is the *strongest* claim available across a `forward_only`
  migration boundary or under a `manifest_only` backup, and the
  restore-point + outcome records pin this capability so an overclaim
  fails record validation.

## Preflight checks (named, fail-closed)

`release_channel` (opt-in), `signature` (#849 states — `unsigned` is a
state, `signing_failed` always blocks, `unverifiable` blocks under
`require_signed`, unknown otherwise), `release_evidence` (#833 pin or
explicit absence), `supported_platform`, `runtime_floor`,
`schema_window` (current data schema within `[floor, ceiling]`),
`migration_direction` (schema downgrade → fail; forward-only → warn +
disclosure flag), `pending_migrations` (unapplied migrations on the
current install block), `disk_space` (unobserved → unknown),
`running_transactions` (in-flight commissioning/device transactions
block), `crash_recovery_pending` (unresolved #883 recovery state
blocks; unobserved → unknown), `adapter_compatibility` (dropped
bound adapter → fail; API floor → fail; undeclared → unknown).

`irreversible_migration_disclosed` is derived (a `forward_only_migration`
warn check). Authorization of `apply` requires
`acknowledges_irreversible_migration=True` when it is set.

## Staged apply + verified rollback

`UpdateInstallDriver` is the mutation seam; `FilesystemUpdateDriver`
implements it over the layout:

```
<update_area>/fetched/          verified package artifacts
<update_area>/staged/           payload prepared for swap
<update_area>/previous-install/ diverted pre-swap install
<update_area>/restore-point/    install/ + data/ + manifest.json
<update_area>/failed-install-N/ post-check install diverted on rollback
```

The restore point self-verifies at capture; swap is two renames;
`inspect_install_state` reads the marker set (`clean` | `applied` |
`partial`) for resume decisions. Rollback *restores and re-verifies* —
tree sha + per-item content sha against the sealed manifest, plus data
items under a `full` backup — and reports `rolled_back` only when the
restored content verifies. An unverifiable restore is
`rollback_failed`, never `rolled_back`.

## Crash recovery

`resume(session)` re-derives state from the sealed log on next launch:

* `rolling_back` → the bounded rollback re-runs (idempotent);
* `applied` → the post-swap health check re-runs and decides;
* `staged` + driver says `partial` → automatic verified rollback;
* `staged` + driver says `applied` → the swap-applied transition is
  sealed after the fact and the pipeline continues;
* earlier stages → informational only; the operator re-invokes.

## Test seams (no network I/O)

* `UpdatePackageSource` — `LocalDirectoryPackageSource` for real
  bundles, `FakeUpdatePackageSource` + `FakePackageScenario`
  (deterministic corruption / truncation / missing / fetch failure).
* `UpdateInstallDriver` — `FakeUpdateInstallDriver` +
  `FakeDriverScenario` (capture/stage/swap/restore failures, backup
  corruption, mid-swap and mid-restore crashes).
* `UpdateEnvironmentProbe` — `FakeUpdateEnvironmentProbe` (scripted
  facts + health), `LiveUpdateEnvironmentProbe` (real observation via
  injected callables for transactions, recoveries, adapter inventory,
  schema and disk; health via an injected runner).

`backend_is_simulated` on the fake seams lands on reports
(`probe_is_simulated` / `driver_is_simulated`) so simulated evidence
never reads as machine-observed.

## What remains device-only

* A real process restart: the health check observes the *staged*
  install in-place; exercising an actual OS-level restart belongs to the
  owned-Windows acceptance run (one upgrade + one rollback), not the
  in-repo harness.
* Publisher signature *verification* itself (Authenticode trust chain)
  is consumed as the sealed #849 state — validating signatures against
  the platform trust store happens in the release pipeline (#833/#849),
  outside this authority.
* `LiveUpdateEnvironmentProbe` reads what the host offers; on hosts
  that cannot observe adapter API level or pending recoveries those
  checks stay `unknown` and the update stays `unverifiable`.
