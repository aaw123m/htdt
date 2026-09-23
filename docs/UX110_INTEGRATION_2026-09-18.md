# UX110 parallel-slice integration — 2026-09-18

Issue #118 の並列実装 PR #123/#125/#126 と Issue #102 PR #124 を main へ統合した後、
workflow shell PR #122 の統合境界を再レビューして修正した記録。

## Integrated authorities

- Design system: `ui_theme.py`
- Command registry / Ctrl+K: `command_registry.py`, `command_palette.py`
- Overview readiness: `overview_readiness.py`
- Backup/restore application controller: `data_management.py`
- Shared navigation contract: `workflow_navigation.py`
- Workflow shell: `workflow_shell.py`
- Temporary legacy workspace safety bridge: `workflow_legacy_bridge.py`

Shell / command / Overview は `WorkspaceId` と `WorkspaceDeepLink` を
`workflow_navigation.py` から共有する。第二の workspace ID authority や alias は作らない。

## SceneRevision stale-state correction

PR #122 の初版は Room / Measurement / Optimization を別々の legacy
`QMainWindow` instance として lazy mount していた。各 window が独立
`WorkingDocument` を保持するため、複数 workspace で異なる SceneRevision を基に
編集すると stale parent から新しい revision を保存できる危険があった。

UX120-UX140 で shared-document workspace view へ置換するまで、bridge は fail-closed とする。

- draft が dirty の間は workspace 移動を拒否する
- active preview がある間は移動を拒否する
- recovery decision が残る間は移動を拒否する
- workspace 配下の QThread が running の間は移動を拒否する
- clean workspace を再activateした時、repository latest が変わっていれば
  existing `_load_or_seed()` authority で latest SceneRevision を読み直す
- entity deep-link は shell が entity semantics を解釈せず、mount callback 経由で
  workspace-owned selection API へ渡す

これは repository save semantics を変更せず、legacy bridge 上で同時 stale draft を
作らないための暫定安全境界である。

## Shell integration

- shell 独自 raw-hex QSS は使用しない
- `QApplication` composition root で `apply_dark_theme(app)` を一度適用する
- Ctrl+K palette は shell をparentとして一度生成する
- task command は destination workspace の lazy mount 時に existing authorityへbindする
- Save / Undo / Redo は active legacy editor にだけbindする
- Overview は `OverviewReadinessService` の read-only view model を描画する
- context bar は bridge中は該当legacy dockをbest-effortでraiseする
- Room bridgeは現行N20-N70のplacement/prediction capabilityを失わないよう
  `PredictionWorkspaceWindow` を使用する

## Launch policy

UX160のWindows visual acceptance前に既定product launcherを置換しない。

通常起動:
```text
python -m htdt.native_cad
```

UX110 shell preview:
```text
python -m htdt.native_cad --workflow-shell
```

dark-first themeはworkflow shell previewのcomposition rootで適用する。accepted legacy launcherのappearanceはUX150/UX160 acceptance前に変更しない。

## Issue #102

PR #124 の backend/controller slice は main へ統合済み。
`native_backup` が唯一のbackup authorityであり、archive format/validation/rollbackを
GUI側へ複製していない。

`設定 > データ管理` の最終widget/file-dialog compositionは、新shellのSettings surfaceを
作る時に `DataManagementController` を接続する。restore時のhandle release/reopen contractは
すでにcontroller側に存在する。

## Remaining UX work

- UX120: Room workspaceをlegacy window bridgeからshared-document viewport-centric viewへ置換
- UX130: Measurementsをpage layoutへ置換
- UX140: OptimizeをSetup/Candidates/Compare/Measure-Validateへ分割
- Settings/Data Management UIをUX110+ shellへ接続
- UX150/160で日本語実文字列、100/150/200% DPI、3D visual、focus、motionをWindows実機確認
- workflow shellを既定launcherへ昇格するのは上記acceptance後

RDCはこのintegrationでは使用しない。
