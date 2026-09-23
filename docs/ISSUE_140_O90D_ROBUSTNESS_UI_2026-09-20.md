# Issue #140 / O90D workflow-first robustness UI — 2026-09-20

## Scope

This slice connects existing O90A-O90C robustness authority to the workflow-first Optimize workspace. It does not reimplement sampling, robustness algorithms, Pareto logic, prediction, or solver execution.

## Navigation

Optimize now has the canonical sub-context:

```text
探索設定 → 候補 → 比較 → ばらつき耐性 → 測定・検証
```

No new global destination and no legacy optimization dock were added.

## Presentation semantics

The page reads persisted `RobustnessSpec`, `RobustnessEvaluation` and `PerturbationSample` evidence and presents independently:

- Nominal objective
- local sensitivity plus an axis-by-axis sensitivity chart
- 「評価サンプル内の不利側最大値」 for finite sampled-worst evidence
- feasible / infeasible / failed / unsupported counts
- evaluation completeness
- mean / p95 / violation probability only when backend probability semantics allow them
- comparison eligibility and explicit blocked reasons
- finite PerturbationSample performance-distribution histogram; unweighted frequency is never relabeled as probability, while explicit sample weights are shown only as feasible-sample-conditional probability mass
- dedicated 3D tolerance/aim overlay from exact RobustnessSpec axes
- explicit infeasible position-sample markers; overlay is evaluated evidence, not a safe-region promise

Minimize/maximize direction comes from each robustness evaluation/objective definition. There is no hidden robustness score, automatic winner, or recommendation.

Bounded interval and unweighted empirical/discrete samples do not expose fake p95/probability. Explicit distribution/weighted authorities may expose those metrics.

## Exact/stale gate

The presentation layer checks exact SceneRevision/content hash, SearchSpec/hash, nominal ObjectiveEvaluation/hash/spec, RobustnessSpec/hash, candidate, model/version, prediction provider and fidelity. Mismatch is displayed as a re-evaluation requirement and blocks candidate comparison.

## UI architecture

- Standard view uses Japanese-first labels and human candidate labels.
- Internal RobustnessSpec/SceneRevision/SearchSpec identifiers, hashes, model/provider/fidelity are isolated under Advanced.
- Page refresh is read-only and performs no solver or sampling work on the UI thread.
- Candidate selection state is not rewritten by page navigation.
- A separate read-only 3D viewport renders the exact candidate document plus declared position/aim/body-yaw tolerance primitives.
- Infeasible sample markers are rendered only from persisted PerturbationSample evidence.
- Existing UX150 responsive/offscreen workflow shell remains the layout authority.

## Acceptance boundary

Software layout/semantics are CI-testable without RDC. Actual Windows DPI/font/mouse/3D readability/visual clipping/first-use acceptance remains UX160 and is not claimed here.

RDC was not used.
