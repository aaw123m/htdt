# #954 htdt domain split — Phase 1 (measurement package + boundary audit)

First bounded slice of the #807 flat-namespace decomposition: one domain
moved into a layered sub-package, every old import path preserved, and
the boundary audit extended into a reproducible baseline/diff gate.

## Domain chosen: `measurement`

Candidates were scored on measured dependency shape (fan-in/fan-out,
cycle membership, Qt leakage, internal layering cleanliness):

| domain | modules | all four layers real? | internal violations | verdict |
|---|---|---|---|---|
| **measurement** | 45 | yes — domain/services/persistence/ui | 0 | **chosen** |
| acoustics | ~8 | no services/ui split material | n/a | too thin to prove the boundary |
| calibration | ~20 | thin ui layer | n/a | defer to a later slice |

`measurement` is the only candidate that exercises the full
`ui -> services -> persistence -> domain` stack with a clean internal
shape: its 45 modules (~50k lines, fan-in 58) produce zero layering
violations under the package rule. Acoustics/calibration stay flat as
later #807 slices.

## Boundary shape

```
htdt.measurement
├── domain/          27 modules — sealed models, plans, authorities, math
├── services/         7 modules — campaigns, loops, workflow orchestration
├── persistence/      6 modules — repositories, stores
└── ui/               5 modules — Qt surfaces (dialogs, workspaces)
```

Documented import direction: **ui → services → persistence → domain**
(kernel sits below domain; application above ui). Enforced by the
`package_import_direction` audit rule — a lower layer importing a higher
packaged layer, or reaching *through* a shim to a higher layer, is a
violation. Flat-target ranks (`kernel:-1, domain:0, persistence:1, ui:3,
application:4`) let packaged modules' edges to still-flat modules be
scored on the same axis, so e.g. `measurement.domain -> cad_repository`
records as debt rather than escaping classification.

## Shim approach — `sys.modules` alias

Each of the 45 flat `htdt.<stem>` files now reads (in full):

```python
import sys as _sys
import htdt.measurement.<layer>.<stem> as _impl
_sys.modules[__name__] = _impl
```

Importing `htdt.<stem>` returns **the canonical module object itself** —
not a copy and not a forwarding table. That makes the compat surface
exactly old-behaviour for:

- `import htdt.x` / `from htdt import x` / `import htdt.x as m`
- `from htdt.x import name` — resolved off the impl's `sys.modules` entry,
  so a pre-existing broken `__all__` (`routing_profile`, listed but
  undefined) fails identically to before instead of being silently fixed
- attribute *writes* — tests monkeypatch `_utc_now`/helpers on the flat
  module name; writes land on the real module (this was the failure mode
  of a name-copy shim: 23 campaign-test failures, now 0)
- `inspect`, `pickle`-adjacent `__module__` references, module `is`
  identity

No lazy `__getattr__` escape hatches, no module-level mutable state in
the shims — cycle safety is structural (an import-time re-entry through
a shim would already be an import cycle at HEAD).

## Audit modes (`scripts/package_boundary_audit.py`)

Deterministic, AST-based, no repo state:

- **default run**: full report — module/edge counts, layer histogram,
  violations, Tarjan SCCs, size budget, `qt_domain_crossings`,
  `persistence_coupling`, per-package membership; exits 1 on any
  unexempted violation
- **`--write-baseline <path>`**: records the current violation/cycle set
  as the managed-debt inventory
  (`scripts/package_boundary_inventory.json`, committed)
- **`--diff <baseline>`**: fails (exit 1) on *new* violations,
  *new/merged* cycles, or newly oversized modules vs the baseline;
  grown cycles are reported as warnings, resolved entries are listed;
  inventoried debt passes
- `--json` / `--markdown` for machine/PR-consumable output

New edges that violate are computed by `violation_key =
rule|module|detail`, so a repeat offender is still flagged when its
detail line changes.

## Measured state after the split

- 1108 module nodes, 5311 resolved import edges (45 shims add one edge
  each; the mega-SCC still stands — shrinkage is the point of later
  slices, not this one)
- 26 inventoried violations: 14 `lower_layer_imports_ui` +
  12 `package_import_direction` — all pre-existing edges re-expressed by
  the split (e.g. flat domain modules importing the moved ui layer), now
  individually named in the inventory
- 4 cycles (the 580-member mega-SCC + 3 known small cycles), 8 oversized
  modules (all in `SIZE_EXEMPTIONS`, 0 unexempted)
- `--diff` against the inventory is clean: `26 inventoried violations,
  0 resolved, 0 grown cycles`

## Tests added

`backend/tests/test_issue_954_domain_split.py` — 99 tests:

- flat path ≡ canonical module object, all 45 stems ×3 import forms
- attribute writes through the flat path reach the impl (monkeypatch
  parity)
- `PACKAGE_LAYERS` exhaustiveness vs on-disk package contents
- no moved module reaches a sibling through a flat shim (source scan)
- shims are pure aliases (no `__getattr__` forwarding tables)
- sealed authority hash equality: `build_timing_reference` via old and
  new paths produces byte-identical sealed records
- audit report `package_layers` classification; violations ⊆ inventory
- baseline round-trip + diff classifier flags fabricated new
  violations/cycles
- domain-layer flat shim stays PySide6-free on import

Regression run over the 138 test files that reference moved stems:
all previously passing tests still pass; the only failures observed are
2 `test_round8_measurement_journey` cases that fail identically on a
pristine `main` worktree (missing `osmesa.dll` / `RewApiUnavailable` —
environmental, unrelated).

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.measurement.*`
- shrink the mega-SCC by migrating the next domain (acoustics or
  calibration)
- resolve the 26 inventory entries; require `--diff` in CI so the
  inventory can only shrink

Phase 2 (capture package) landed separately — see
`docs/issues/issue-954-domain-split-phase2.md`.
