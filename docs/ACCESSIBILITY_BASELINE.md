# HTDT Native Accessibility Baseline — Issue #731

> 制定: 2026-09-24 / 対象: shell navigation・engineering forms・plots・Room 3D・dialogs・Settings/Help

This is the bounded Windows native accessibility contract. It is **not** a
claim of complete WCAG conformance for a desktop CAD product — it is the
baseline every surface must meet before it ships as normal/stable UI
(composes with #578/#591 visual rules and #723 Golden Path acceptance).

## 1. Keyboard-only workflow

Core project completion must not require a mouse-only affordance:

- navigate the shell and trigger Overview actions;
- select Room entities through the Scene tree;
- edit exact transforms/values through Inspector forms;
- use Measurements and Optimize controls;
- open Settings/Help/Command Palette;
- accept/cancel dialogs.

Room spatial editing may remain most efficient by mouse, but every
essential command needs a keyboard-accessible alternative. No essential
action may be hover-only, right-click-only, or drag-only.

## 2. Focus model

- Deterministic Tab / Shift+Tab order, declared per surface via
  `native_accessibility.FocusSurface`.
- Focus is preserved or deliberately restored after dialogs, overlays
  and workspace refresh — `FocusSurface.restore_focus` keeps the focused
  control while it still exists; it never resets to the top of a page on
  routine refresh.
- Visible focus indication is distinct from selected/current state.
- No focus traps in nested scroll areas or tables.

## 3. Accessible name / role / value

- Icon-only buttons carry meaningful accessible names.
- Inputs expose label, current value and units.
- Disabled primary controls expose a human-readable reason —
  `DisabledControlReason` rejects terse/internal strings.
- Tables expose meaningful row/column labels.
- Lifecycle badges (Current / Proposed / Measured / Stale) are textually
  inspectable, not color-only.
- Internal class names, UUIDs and schema tokens are never the primary
  accessible label.

## 4. Dynamic state

Important transitions must not rely on visual change alone. The events
in `REQUIRED_ANNOUNCEMENTS` (import result, queued/running/completed/
failed operations, retake required, stale result, save state, device
disconnect) reach the user through a readable/focusable status surface
or a native accessibility announcement. High-frequency viewport motion
is never announced.

## 5. Color, contrast, motion

- #579/#592 semantics: measured/predicted/current/stale/warning are
  never color-only encodings.
- High-contrast and larger system text must not hide essential controls.
- Reduced-motion mode simplifies nonessential transitions; motion is
  never required to understand state.

## 6. Room 3D boundary

The VTK viewport itself is not a screen-reader geometry representation.
Equivalent semantic entities/properties are always reachable through
Scene tree, Inspector, commands and textual status. A viewport-only
blocker needs a textual companion if it prevents an action.

## Acceptance fixture (owned-Windows, batched with UX160/#723)

1. Fresh project / Overview using keyboard only.
2. Room: select through tree, edit XYZ/rotation, save without mouse.
3. Measurements: select dataset, inspect quality, open comparison.
4. Optimize: traverse setup/candidate/result controls.
5. Open Settings/Help and return with focus preserved.
6. Force a blocked state and verify its reason is readable.
7. Representative screens under Windows high contrast / larger text /
   reduced motion.
8. Inspect representative controls with Narrator / Accessibility
   Insights or equivalent native tooling.
