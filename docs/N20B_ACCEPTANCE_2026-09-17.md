# N20b A07 acceptance — 2026-09-17

Status: **PASS**

Branch: `feat/n20b-multiselect-snap`

## Environment

- Windows 11 x64, owned PC
- Ryzen 7 8845HS / Radeon 780M / 31.3 GiB
- Physical display: 2880×1800
- OS scaling: 200%
- Qt logical screen: 1440×900 / DPR 2.0
- Python 3.12.10 with the repository's pinned N05/N20 dependencies
- Fixture: F1 for functional checks; F4-equivalent load for performance checks

## Functional A07 result

Actual Qt/VTK renderer and Windows input passed the A07 gate.

Confirmed behavior:

- vertex, edge, midpoint and alignment snap candidates are reachable;
- screen-space snap uses 8 DIP acquire / 12 DIP retain hysteresis;
- Ctrl-click creates a two-entity ordered selection with a primary entity;
- group Move uses one common world-axis delta and preserves relative placement;
- actual mouse group Move snaps to `speaker-fr:vertex:0:x` and commits as one history command;
- group Rotate uses the common selection pivot and commits as one history command;
- 15° angle snap supports the validated -45° actual-mouse rotation;
- Undo restores the complete group atomically;
- unknown speaker aim remains unknown through group transforms;
- object transforms do not move the camera;
- numeric multi-selection Move uses the same domain validation/command path;
- W/R shortcuts do not fire while a numeric editor owns keyboard focus.

## DPI and input-path corrections

The selector was checked at DPR 1.0, 1.5 and 2.0 equivalents. A 7 DIP probe acquired the candidate and a 10 DIP probe retained it at all three scales.

Move handle acquisition uses a 12 DIP screen-space distance to the projected axis segment, leaving visible geometry independent from hit tolerance. Scene entity selection is handled before VTK's default interaction path, while gizmo hit-testing is isolated so a handle press cannot collapse multi-selection by picking an entity behind it.

## Final F4 performance

Load: F1 plus 50 editable objects and 1,000 analysis markers represented as one point-cloud actor.

Formal Windows harness on exact product head `69991a96905d4842ead3dbf0d3b41b54d150da8d`:

- Windows/Qt input baseline: p95 **52.14 ms**;
- pick → corresponding selection render complete: p95 **88.26 ms**, max **113.18 ms**;
- Move drag: p95 **30.11 ms**, max **98.01 ms**, **0** samples over 100 ms;
- orbit: p95 **23.97 ms**, max **34.06 ms**, **0** samples over 100 ms;
- orbit camera movement: confirmed.

All initial F4 targets are satisfied: pick p95 ≤100 ms, drag/orbit p95 ≤33 ms, and no >100 ms drag/orbit stalls.

The key final latency correction changed the selection projection timer from a fixed 16 ms delay to a zero-delay single-shot. This preserves next-event-loop projection/coalescing while removing unnecessary fixed latency.

## Automated validation

- focused document/repository/snap regression passed throughout implementation;
- Windows Actions patch gate passed backend regression, CAD compile/launcher checks, PowerShell syntax, frontend build and built-app smoke for the final interaction changes;
- normal PR CI passed before the final record-only edit; the current documentation head must also remain green before merge;
- actual-machine A07 + F4 final harness exited 0;
- temporary remote workflow branches were deleted;
- N20b local temporary files were cleaned;
- tracked Windows worktree remained clean after acceptance.

N20b technical and performance gates are complete. Next native CAD milestone is N30a room sketch/editing.
