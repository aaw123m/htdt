# HGUI-00 — GUI visual design inventory / freeze (#578)

Review basis: `origin/main @ 24375438` (plus the first PLOT/HGUI sibling
landings). This document is the design-inventory deliverable of #578 — it
records what exists, what the frozen tokens are, and which child issues own
each phase. It makes no production UI change by itself.

## 1. Screen inventory

| Surface | File | Current composition |
|---|---|---|
| Desktop shell | `workflow_shell.py` | `WorkflowRail` (expanded 184 px / compact 112 px, text-only buttons), `TopContextBar` (44–48 px target, text buttons, no overflow policy yet), `WorkspaceRouter` (QStackedWidget), bottom status strip |
| Overview | `overview_workspace.py` | title, summary, warning/error cards, one primary next-action button |
| Room CAD | `room_workspace.py` | context tool strip + scene-objects palette column + overlay-controls row + 3D viewport + inspector column + status bar |
| Room viewport | `room_viewport.py` | PyVista renderer; semantic category palette + glyph proxies landed under #572 |
| Measurements | `measurement_page_workspace.py` | page-first import/assignment/campaign/quality/comparison sections; plots standardized under #579 via `scientific_plot_style.py` |
| Optimize | `optimization_workflow_workspace.py` | task sections driven by saved search specs and candidate lists |

## 2. Control inventory (gaps called out in #578)

| Control | Current state | Target |
|---|---|---|
| Global rail | text-only; compact = 112 px with hidden brand | icon+label; true 64–72 px compact with tooltips |
| Context bar | text buttons; no overflow policy | restrained segmented control with 「その他」 overflow |
| Object creation | permanent full-height palette column | contextual Add → popover/temporary drawer |
| Scene objects | exists as list rows (#482) | eye/lock rows, accent selection marker |
| Inspector | flat `QFormLayout` | sectioned: identity/transform/geometry/speaker/advanced (#583, landed: `SelectionInspector` + `Vector3Editor` + `MetricSpinBox` + `InspectorSection`) |
| Overlay controls | permanent full-width checkbox row | compact top-right viewport menu |
| Status area | single bottom strip | transient only; long errors move to inline banners |

## 3. Design-token table (frozen in `ui_theme.py`)

| Group | Tokens |
|---|---|
| Surfaces | `canvas` / `base` / `raised` / `overlay` — raised only when a boundary matters |
| Typography | WORKSPACE_TITLE / SECTION_TITLE / BODY / SECONDARY via `set_typography_role` |
| Spacing | 4 px base rhythm: `xs`/micro through `xxl`; no arbitrary 7/13/19 px paddings |
| Radius | 4–6 px controls, 8 px cards — no pill buttons |
| Accent | `accent.primary` #6BA6FF = selection outline and single controlled accent |
| Scientific | `measured` #4CC5B1 / `predicted` #BD9CF4 / `primary_trace` #7ED6FF / `secondary_trace` #94A0AC / `cursor` #E9CE7A / `grid` #2E3945 / `target` #C9AE63 / bounded `channels` palette (8 muted colors) |
| Viewport categories | `architecture` / `source` / `listener` / `display` / `treatment` / `infrastructure` / `reference` — low-saturation, non-colliding with scientific overlays |

## 4. Semantic-state table

| State | Grammar | Where |
|---|---|---|
| selected | accent outline + filled selection, width emphasis | rail, scene objects, viewport, traces |
| proposed | ghost wireframe (never same fill grammar as current) | viewport proposals |
| measured / predicted | token color + solid/dashed style | plots, badges |
| stale | badge + reason + recompute action; never red recolor | overview, reports |
| unknown / unsupported | explicit label; empty ≠ missing | capability checks, plot states |
| error/blocked | error palette + inline message near the field | inspector validation, banners |
| no-data / loading | centered plot-state message (`show_plot_state`) | all scientific plots |

## 5. Phase → owner mapping

| Phase | Scope | Owner |
|---|---|---|
| HGUI-00 inventory | this document | #578 (done here) |
| HGUI-10 shared primitives | nav item, status badge, banner, empty state, task header, disclosure | #578 family |
| HGUI-20 shell polish | icon rail, compact rail, context overflow | child of #578 |
| HGUI-30 Overview | final hierarchy + #443 direct actions | child of #578 |
| HGUI-40 Room chrome | creation drawer, scene objects, inspector, overlay menu | #583 inspector landed; chrome remainder open |
| HGUI-50 3D semantics | #572 category palette + glyph proxies | #572 landed |
| HGUI-60 Measurements | PLOT-20 adoption | #579 module + adoption landed |
| HGUI-70 Optimize | dense engineering presentation | child of #578 |
| HGUI-80 consistency sweep | copy/spacing/semantic audit | open |
| HGUI-90 acceptance | Qt smoke + Windows visual gate | deferred to #118 UX160 |

## 6. Standing rules (from #578)

- Five visual levels only (L0 canvas → L4 advanced/provenance); L4 never dominates.
- No card-on-card; whitespace + headings carry grouping.
- Meaning never rides on hue alone (icon/label + optional color).
- Japanese task-first copy; no raw enum/snake_case on normal screens.
- 1440×900 and 1280×800 at 100/150/200% are the acceptance windows; real
  Windows visual validation is the UX160 gate — this plan does not self-certify it.
