# N40 Windows acceptance — A10

Date: 2026-09-17  
Tracking: Issue #55 / PR #56  
Accepted product code: `2e2a3f814276bc3fcd3f392addfd70a10efbe295`

## Result

N40 A10 passed on the owned Windows PC using the normal native product composition and Win32 real mouse input for the primary CAD flow.

The accepted flow was:

`empty project → L-shaped room → 3.0.2 template → seat → explicit speaker aim → screen/furniture → mouse distance correction → mistaken transform → Undo → duplicate → hide/lock → Save → reopen`

A separate precision harness verified Inspector dimension/role refinement and exact save/reopen restoration.

## Environment

- Windows 11 Pro build 26200
- Ryzen 7 8845HS / Radeon 780M
- 2880×1800 display, Windows 200% DPI
- Python 3.12.10 from repository `.venv`
- PySide6 6.11.2
- PyVista 0.49.0
- VTK 9.7.0

The system-default Python was 3.14.5 and did not have PySide6. That was an environment-selection issue, not a product failure; the acceptance was run with the repository's supported Python 3.12 environment.

## GitHub CI gate

GitHub Actions CI run #220 (`35196621511`) passed on accepted head `2e2a3f814276bc3fcd3f392addfd70a10efbe295`.

It covered backend tests, backend/native launcher CLI checks, compilation of A07/A08/A09/A10 Windows harnesses, PowerShell syntax, frontend build, and built-app smoke test.

## First-use findings and fixes

The final Windows gate was intentionally used to expose workflow friction rather than only confirm code paths.

1. The N40 side docks reduce viewport width at 200% DPI, so a fixed A08 camera scale could place the 6 m fixture edge outside the viewport. The A10 gate now frames the fixture from the actual render-window aspect ratio.
2. Closing a newly drawn room originally left the product in room-edit mode. At 200% DPI the separate Done Room toolbar control can overflow, leaving object palette actions disabled. N40 now returns directly to object mode after successful room creation; explicit room editing remains available through the existing room-edit action.
3. Screen and front speakers initially shared the same default front strip, causing Top-view pick overlap. The screen now defaults closer to the front wall.
4. Seat and furniture initially shared the same default center position, causing later selection overlap. Default theater-object insertion now uses separated horizontal lanes for furniture, AV equipment, measurement points, and standalone speakers while leaving the seat central.

These changes were made in product code where the friction represented a real first-use problem; only fixture framing stayed in the acceptance harness.

## A10 core result

```text
A10_JAPANESE_DISCOVERY True
A10_CAMERA_FRAME 1844 2160 aspect=0.854 scale=3.924
A10_MOUSE_L_ROOM True
A10_MOUSE_302_TEMPLATE True
A10_MOUSE_SEAT True
A10_EXPLICIT_SEAT_AIM True
A10_MOUSE_SCREEN_FURNITURE True
A10_MOUSE_DISTANCE_CORRECTION True
A10_MOUSE_UNDO_MISTAKE True
A10_MOUSE_DUPLICATE True
A10_HIDE_VIEW_ONLY True
A10_LOCK_VIEW_ONLY True
A10_MOUSE_SAVE True
A10_REOPEN_EXACT True
A10_FIRST_USE_SECONDS 15.75
A10_MISSELECTIONS 0
A10_GUIDANCE_REQUIRED 0
A10_RESULT PASS
A10_CORE_EXIT=0
```

The exact UUIDs generated during the run are intentionally omitted from the stable record.

## Numeric precision result

```text
A10_PRECISION_MOUSE_SELECT True
A10_PRECISION_NUMERIC_EDIT True
A10_PRECISION_SAVE True
A10_PRECISION_REOPEN_EXACT True
A10_PRECISION_RESULT PASS
A10_PRECISION_EXIT=0
```

The precision run changed a speaker physical dimension and `speaker_role` through the Inspector contract, while preserving unrelated pose/orientation/aim fields, then verified exact persistence after reopen.

## Contract checks

- 3.0.2 is a template, not a closed role schema.
- Speaker aim stays unknown after ordinary creation/move and becomes known only through the explicit aim action.
- Aim updates the acoustic axis without rotating the physical body pose.
- Seat body dimensions and its acoustic reference offset remain separate.
- Duplicate preserves dimensions/reference semantics while creating a new stable entity ID.
- Hide/lock modify editor view/edit state only; physical `SceneDocument` content is unchanged.
- Save/reopen restores role, dimensions, positions, acoustic references, and editor hide/lock state as expected.
- The incorrect mouse transform is restored by one Undo.

## Local hygiene

The Windows worktree was clean before and after acceptance:

```text
PRE_STATUS_COUNT=0
POST_STATUS_COUNT=0
```

No local coding was performed. The machine only fetched the GitHub branch, checked out the accepted commit, ran the two acceptance harnesses, and restored its previous detached checkout.