# REV49-FULLSWEEP — Real-GUI verification test plan

App: HTDT (PySide6) — `C:/devin/python/python.exe -m htdt.native_cad --document-id fixture-f1`
Repo: C:\Users\Administrator\repos\HTDT @ c60d0666 (merge of devin/1791154356-rev49-fullsweep)
Screen: 1024x768 (matters for dialog-scroll test)

Setup state (done): app launched, fixture-f1 open, window shows rail with workspaces
概要/部屋/測定/最適化/プレゼン/映像調整 + app destinations + 設定.

---

## Test 1 — Ctrl+K palette opens on EVERY workspace (crash regression)

Previously `CommandContext(current.value)` raised ValueError on プレゼン/映像調整
(contexts missing from enum) → palette could not open there.

Steps: for each rail workspace button — 概要, 部屋, 測定, 最適化, プレゼン, 映像調整:
  1. Click rail button, wait for workspace mount.
  2. Press Ctrl+K.
  3. Assert: palette dialog opens showing search field + command result list.
  4. Close with Esc.

Pass criteria:
- Palette opens on ALL 6 workspaces; no exception dialog, no frozen shell, app stays responsive.
- Most valuable on プレゼン and 映像調整 (previously dead). If palette were still broken,
  pressing Ctrl+K there would show an error dialog or nothing would appear.
- Broken-implementation discriminant: on the two previously-crashing workspaces the
  palette would simply not appear.

## Test 2 — palette navigation commands reach their destinations

New commands: navigation.presentation (プレゼン), navigation.video (映像調整),
navigation.acceptance (受入検証) — all WorkspaceDeepLink / destination links.

Steps:
  1. From 概要 workspace open palette (Ctrl+K), type 「プレゼン」, press Enter on the
     プレゼン result.
     Assert: app navigates to presentation workspace (rail プレゼン selected,
     presentation content mounted).
  2. Ctrl+K, type 「映像調整」, Enter on 映像調整 result.
     Assert: navigates to video workspace (映像調整 workspace content visible).
  3. Ctrl+K, type 「受入検証」, Enter on 受入検証 result.
     Assert: navigates to acceptance destination (受入検証 page visible under
     アプリケーション destinations).

Pass criteria: each typed query produces a matching command entry and executing it
switches the active workspace/destination. If commands were unregistered, the search
would show no such entries / nothing would navigate.

## Test 3 — display-unit policy on new panels (mm ↔ m)

Preference `display_input.length_unit` (default 'mm') + numeric_precision.
Panels newly bound via MetricSpinBox + set_length_policy: room geometry panel,
installation panel (実測クリアランス spins), field-explorer panel (stride/probe).

Steps:
  1. Open 設定 (bottom-left rail) → 環境設定 tab → confirm 長さの表示単位 combo; set to mm.
  2. 部屋 workspace → geometry context (default right dock shows 部屋形状 panel).
     Assert: 天井高 spin field shows " mm" suffix (not " m").
  3. 配置 (placement) tab → scroll to Installation panel.
     Assert: 実測クリアランス spin fields show " mm" suffix.
  4. Field explorer (stretch — requires geometry_modes prediction run): 音響 tab →
     run a prediction if possible → 音場エクスプローラー… → assert stride/probe " mm".
     If no run can be produced quickly (no room/analytic modes), mark this sub-item
     untested with reason; the shared MetricSpinBox class is already proven by
     geometry+installation.
  5. Back in 環境設定 set 長さの表示単位 to m.
     Assert: the same fields now show " m" suffix; values convert — e.g. a field
     showing 2500 mm must show 2.5 m (SI authority preserved: same SI value,
     display rescaled).

Pass criteria: suffix follows the preference live (no restart); values rescale
exactly by the unit factor (mm→m divides by 1000). Broken implementation would keep
" m" suffix in mm mode (old hardcoded behaviour) or show wrong magnitudes.

## Test 4 — dialog scroll reachability on 768px screen

measurement_record_surfaces.py: AVSyncRecordDialog (~1150px hint) and
HealthCheckDialog (~933px hint) now wrapped in QScrollArea + resized to fit.

Steps:
  1. 測定 workspace → navigate to 品質 (quality) context page (journey step buttons
     or context tabs).
  2. Scroll page to bottom → 「AV同期を記録…」 button → open dialog.
     Assert: dialog fits within 768px-tall screen, a vertical scrollbar is present,
     and scrolling reaches the bottom-most controls (buttons/commit row visible).
     Close.
  3. 「健全性チェックを記録…」 → same assertions.

Pass criteria: on a broken (pre-fix) build the dialog would be ~933–1150px tall and
bottom buttons would be unreachable/off-screen. Post-fix: scrollbars present and
bottom row reachable.

## Test 5 — JA tooltips + general sanity

28 JA tooltips added on management/record buttons (data_management_ui 12,
measurement_record_surfaces 11, playback_chain 3, optimization_validation 2).

Steps:
  1. Settings 設定 → データ管理 tab → hover e.g. バックアップ作成/移行 export button.
     Assert: tooltip appears with Japanese text (e.g. '全データのバックアップファイルを作成します').
  2. On measurement quality page hover 「AV同期を記録…」 — tooltip in JA.
  3. Visual sweep: room geometry panel, placement/installation panel, video +
     presentation workspaces — no obvious layout breakage (overlaps, clipped text,
     dead white areas).

Pass criteria: tooltips render JA text; panels look structurally intact.

---

Evidence: single continuous recording, annotated per test. Screenshots of key
states (palette on 映像調整, mm/mm field suffixes, dialog scrollbar) saved to
C:\Users\Administrator\screenshots\.
