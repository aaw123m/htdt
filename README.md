# Home Theater Digital Twin

HTDTは、Windows上で**部屋・ホームシアター配置・測定・予測・最適化を一つの3D空間モデルへ統合するnative desktop digital twin**です。

## North Star Architecture Principle

HTDTは、source-neutralなreal-room evidence、実機equipment/directivity/playback-chain authority、acoustic treatment、measurement evidenceを、immutable SceneRevision とexplicit provenanceによって**一つのrevisioned Digital Twin**へ統合します。

raw visual evidence、semantic acoustic authority、compiled/solver representation、predicted result、measured evidence、derived result、hypothesisは別authorityとして扱い、unknown/unsupportedを暗黙補完しません。current / proposed / as-built / measured lifecycleを飛び越えず、SystemVariant比較は同一Digital Twin上のexact authority comparisonとして行います。

最上位の設計原則とauthority flowは [docs/ARCHITECTURE_PRINCIPLES.md](docs/ARCHITECTURE_PRINCIPLES.md) を正本とします。実装済み/未実装の状態は同文書ではなく [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) と [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md) を参照します。

## 現在の製品状態

stable personal Windows releaseは **0.1.0** です。

主UIはPySide6 / Qt Widgets + PyVista / VTK / PyVistaQtによるnative 3D CAD editorです。browser UIはlegacy/rollback用としてsourceを保持していますが、新しいCAD機能の正本ではなく、native releaseの必須gateからも外しています。

実装済みの主経路:

- 3D CAD型のselection / gizmo / snapping / numeric edit / Undo/Redo
- 凹polygon room、wall / opening、speaker / seat / screen / furniture / AV機器 / measurement point
- placement hard constraintと理由overlay
- immutable SceneRevision / recovery / view state
- REW実測workspace、Frequency Response表示、過去配置ghost、A/B比較
- prediction authority / geometry compatibility / reflection overlay
- deterministic SearchSpec / placement candidate
- objective vector / Pareto比較
- candidate→exact SceneRevision→Measurement Plan→N60 measured evidenceの閉ループ
- O60 holdout trend / sensitivity / repeatability / applicability validation authority
- stable Windows package / installer / update / backup / restore / uninstall data retention

N05〜N90のnative release pathとO10〜O80のsoftware pathは実装済みです。O90 robust/tolerance-aware optimizationはO90A〜O90Dまで実装済みです。bounded / distribution / empirical / discrete uncertainty、probability-gated statistics、cancel/cache/resume/stale protection、auditable multi-fidelity screening、common-fidelity robust-Pareto finalizationに加え、PR #249でworkflow-first「ばらつき耐性」UIまでmainへ反映済みです。残るO90E owned-room robust validationとUX160 owned-Windows visual acceptanceは別gateです。O100 system expansion / virtual channel topology optimizationはO100A〜O100Fまで実装済みです。O100G backendはPR #235でproposal→explicit As-built、PR #239でexact As-built→measured evidence binding、PR #244でSystemVariant-specific MeasurementPlan / Campaignまで実装済みです。PR #255でRoom/Optimize workflow-first UX、proposed/as-built/measured badge・ghost、SystemVariant comparison、apply confirmation、measured presentationも実装済みです。残件はUX160 owned-Windows visual acceptanceのfull gateです（PR #291で部分acceptanceは反映済み）。詳細な現在地は[実装ステータス](docs/IMPLEMENTATION_STATUS.md)を正本とします。  
O70 Adaptive Plannerは `development_synthetic` で、O80 Extended SearchとAdaptive Extended acquisitionはsynthetic directional capabilityでsoftware pathを最後まで確認できます。一方、`production_owned_room` recommendationとowned-room directional capabilityは、**独立した実室O60 validation evidenceが成立するまでfail-closed**です。

実装済み・未検証項目の事実は [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md)、今後の実装順とgateは [`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md) を正本とします。Issue #101の任意形状音響solverはR100〜R180として計画化し、技術判断は [`docs/ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md`](docs/ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md) に記録しています。

## Windows stable release

### インストール

N90のWindows installerはper-user installです。

- program root: `%LOCALAPPDATA%\Programs\Home Theater Digital Twin`
- user data root: `%LOCALAPPDATA%\HomeTheaterDigitalTwin`
- stable AppIdで上書きupdate
- uninstallはprogram files / shortcutを削除するが、user dataを暗黙削除しない
- 個人利用buildではcode signingをcorrectness gateにしない

最終A15では実機Windows上で `0.1.0.dev0 -> 0.1.0` update、backup/restore、GUI reopen、uninstall/reinstall、user-data retentionを一連でPASSしています。

詳細: [`docs/N90_ACCEPTANCE_2026-09-18.md`](docs/N90_ACCEPTANCE_2026-09-18.md)

### Native起動

リポジトリから:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".\backend[dev]"
python -m htdt.native_cad
```

entry point:

```powershell
htdt-native
```

repository helper:

```powershell
.\scripts\run-native.ps1
```

`run-native.ps1` は既定で `--document-id fixture-f1` を付けて起動します（開発用fixture projectが開く）。自分の最後に開いたprojectを開くには `python -m htdt.native_cad` を直接使います。

installed buildではinstallerが配置した `HTDT\HTDT.exe` を起動します。

初回起動は既定のworkflow shellの「概要」workspaceを開きます。プロジェクトが未作成の場合は自動で初期project（「My Home Theater」）が作成され、「概要」画面下端のprimary buttonが次の推奨操作（「部屋を作成」「スピーカーを追加」等）を示すので、そのままguided path（部屋→スピーカー→測定→予測→最適化）を辿れます。

操作手順は [ユーザーガイド](docs/USER_GUIDE_JA.md)（日本語）を参照してください。

画面内ヘルプ: `Ctrl+K` のコマンドpaletteで `help` または `ショートカット` を検索すると、ショートカット一覧・用語・workflow説明のhelp topicを開けます。「測定」workspaceには常設の「用語集・ヘルプ」buttonがあり、エラーダイアログの「ヘルプ」buttonは該当トピックを直接開きます。

起動option（`HTDT.exe` / `python -m htdt.native_cad` 共通）:

- `FILE`（positional）: `.htdtproject` / `.htdtcapture` / `.htdt-backup` を開く。installerはこの3拡張子をHTDT.exeへ関連付けます（`.htdt-backup` はpreview表示のみ。復元は別途 `--restore` の明示操作です）
- `--data-dir <dir>`: user data rootを既定 `%LOCALAPPDATA%\HomeTheaterDigitalTwin` から変更する
- `--document-id <id>`: 指定document idのprojectを開く（未登録ならprojectとして登録）。通常は最後に開いたprojectが自動で開かれます
- ~~`--legacy-ui`~~: 削除済み（REV36-UX140C）。旧 `OptimizationWorkspaceWindow` 構成は撤去され、指定時は日本語エラーで終了します
- `--safe-mode`: 連携・保存済みlayout・自動オープンintentなしのguarded launchで起動

## Backup / restore

stable packageはGUI起動前にmaintenance CLIを処理します。

version:

```powershell
HTDT.exe --version
```

backup:

```powershell
HTDT.exe --backup "D:\Backups\home-theater.htdt-backup"
```

restore:

```powershell
HTDT.exe --restore "D:\Backups\home-theater.htdt-backup"
```

repository起動でも同じoptionを使用できます。

```powershell
python -m htdt.native_cad --backup "D:\Backups\home-theater.htdt-backup"
python -m htdt.native_cad --restore "D:\Backups\home-theater.htdt-backup"
```

関連するmaintenance option（`--backup` / `--restore` / `--automatic-backup` / `--seed-synthetic-demo` / `--migrate-legacy-data` / `--revalidate` は相互排他です）:

- `--automatic-backup`: 期限の来た自動バックアップを1回実行して終了（スケジュールタスク用）
- `--migrate-legacy-data`: 廃止されたbrowser store（`htdt.sqlite3`）をnative projectへ移行して終了。移行対象がなければ `{"state":"nothing_to_migrate"}` を返します
- `--revalidate`: update後に要検証となった記録を現在のbuildで再検証（再導出できる記録を再署名）して終了
- `--backup-allow-stale`: 要再検証の記録をmanifestに明記したうえでbackupを許可（`--backup` と併用）
- `--seed-synthetic-demo`: 合成 O10-O80 開発demoをseedして終了（後述）

backupはlive SQLite fileの単純copyではなくSQLite backup APIでconsistent snapshotを作り、N60 measurement raw assetsもSHA-256で検証してarchiveへ含めます。restoreはarchive traversal、manifest/hash、SQLite integrity/foreign key、asset hashを全検証してからstagingし、現在dataをpre-restore backupへ退避して置換します。

native GUI / backup / restore / synthetic seedはdata directory単位のOS lockを共有します。同じuser-data directoryを別プロセスが使用中の場合は起動・maintenance処理を開始せずfailします。lock権限はdata directory内 `.instance.lock` のbyte-range lock（Windowsでは `msvcrt.locking`、POSIXでは `flock`）です。filesystem基準で排他するため、別Windows session（RDP・fast user switching）や、byte-range lockに対応したshared filesystem（SMB等）経由の別machineからの同時利用も排除します。lockはOSが保有しprocess終了時に解放されるため、残存したlock fileやmetadataでdata directoryが永続的にlockされることはありません。byte-range lock非対応のfilesystemでは取得がfailし、fail-closedで起動を拒否します。

native `cad-scenes.sqlite3` は中央schema versionを持ちます。0.1.0以前のpre-versioned native DBはintegrity/foreign-key検証後にbaseline v1へadoptし、このアプリより新しいschemaはdowngradeせずfail-closedで拒否します。

restoreはarchive/member/総展開量/member数を上限付きで検証し、各memberをstreaming SHA-256検証してからstagingします。

この `.htdt-backup` authorityのみがサポート対象です。legacy browser backend (`python -m htdt` / `htdt.server:app`) の `GET /api/backup`・`POST /api/restore` は廃止され、常に `410 Gone` を返します。これらはhash未検証manifestと上限のないZIP展開を持つ独立したbackup経路だったため、native authorityへ一本化しました。

## CAD / analysis architecture

HTDTの中心は、数値フォームを先に埋める方式ではなく、同一Sceneを3D空間として直接操作するeditorです。

- room footprintをclickして描く
- wall vertexをdragし、寸法入力で精密化する
- speaker、seat、screen、furnitureをpaletteから配置する
- objectを選択してgizmoでmove/rotateする
- grid / axis / angle / wall / vertexへsnapする
- Top / Front / Side / Perspectiveを切り替える
- Scene tree / viewport / Inspectorを同じselectionへ同期する
- measurement、constraint、reflection、prediction、placement candidateを同じsceneへ重畳する

解析結果はSceneRevisionへ版固定し、measured / derived / predicted / hypothesisを区別します。非矩形roomへ矩形専用modelを無言で適用しません。

配置探索も単一の「音質総合点」へ縮約せず、独立objective vectorとPareto集合を保持します。実測validationで傾向・順位・感度・再現性・適用条件が成立しないmodelから自動推薦を出しません。

## 現在の主要実装

- N20b: multi-select、common pivot、object/grid/angle snap、hide/lock
- N30a: 凹polygon room sketch、vertex edit、dimension、self-intersection拒否
- N30b: stable wall ID、opening、wall editと参照migration
- N40: theater objects / 3.0.2 template / duplicate / acoustic reference / explicit aim
- N50: placement constraint adapter / allowed / exclusion / walkway / wall clearance
- N60: immutable measurement/revision binding / REW import/read / FR dock / ghost / A/B comparison
- N70: immutable prediction authority / geometry compatibility / prediction overlays / bulk marker rendering
- N80: SearchSpec / candidate preview+apply / O20 batch prediction / objective / Pareto / Measurement Plan
- O60: calibration/holdout分離 / trend / sensitivity / repeatability / applicability / recommendation gate
- O70: objective別residual GP / uncertainty / adaptive measurement proposal / synthetic・owned-room scope分離
- O80: capability-gated Extended Search / acoustic aim yaw (`aim_yaw_deg`) / physical cabinet toe-in (`body_yaw_deg`) / orientation-aware hard constraints / preview+apply+Undo / Adaptive Extended acquisition over normalized O10+O80 features
- O90A: immutable RobustnessSpec / bounded ± local sensitivity / speaker・listener XYZ / aim yaw・pitch / cabinet yaw / perturbationごとのG10・O80再評価 / infeasible evidence保持 / sampled_worst semantics / persistence
- O90B: deterministic multidimensional bounded / explicit distribution・empirical・discrete uncertainty / linked axes / probability-gated mean・percentile・violation probability / sampled envelope / feasible fraction / cancel・cache・resume・stale protection / nominal vs sampled_worst Pareto
- O90C: auditable multi-fidelity hard-gate / validated screening / budget defer / R140 exact execution+resource-bounded batching+cache/resume / common-fidelity exact robust-Pareto finalization
- O90D: workflow-first「ばらつき耐性」/ nominal・sampled adverse・sensitivity・feasibility・completeness / finite-sample distribution / 3D tolerance+aim overlay / stale・comparison eligibility
- O100A: immutable SystemVariant / ProposedEntitySpec / ChannelRoleBinding / exact add-remove-replace diff / proposed lifecycle / explicit apply→new SceneRevision / proposal lineage
- O100B: TopologySearchSpec / proposed XYZ・height・aim/toe-in / allowed・exclusion regions / linked SL/SR / O10+G10+O80 deterministic placement / candidate→SystemVariant
- O100C: EquipmentDefinition / DirectivityDataset / exact equipment-source binding / R110 source capability
- O100D: coverage / direct SPL / acoustic+electrical headroom / named topology comparison / direction-aware Pareto
- O100E: auditable multi-fidelity hard-gate / validated screening / budget defer / exact common-fidelity finalization
- O100F: proposal-aware O90 local+multidimensional robustness / exact O100D bundle lineage / nominal-vs-sampled-worst robust Pareto
- O100G backend: descendant-aware proposal lineage / explicit As-built / exact measured evidence / SystemVariant-specific MeasurementPlan+Campaign preregistration / measured lifecycle
- O100G UX: PR #255 workflow-first Room/Optimize / lifecycle badge・proposal ghost / SystemVariant comparison / apply confirmation / measured-state presentation（UX160 full Windows visual acceptanceは別gate）
- N90: reproducible package / per-user installer / backup+restore / update+uninstall data retention

## 対象環境

- Windows 11 x64
- Python 3.12 x64
- 個人利用
- Yamaha RX-A4A
- 主要speaker構成: 3.0.2（data model上は可変）
- サブウーファーなしを主要シナリオとするが将来追加可能
- REW V5.40 beta 135 API版をowned Windows環境で使用
- miniDSP UMIK-1を主要測定マイクとして想定

## Legacy browser path

旧browser/FastAPI UI sourceはmigration rollback/historyのため保持していますが、native CADの新機能を二重実装しません。

旧browser pathの開発確認が必要な場合のみ:

```powershell
.\scripts\run-local.ps1
```

native stable releaseのcorrectnessはfrontend buildへ依存しません。

## 設計・受入文書

| 文書 | 内容 |
|---|---|
| [ARCHITECTURE_PRINCIPLES.md](docs/ARCHITECTURE_PRINCIPLES.md) | **North Star Architecture Principle / authority flowの正本** |
| [IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md) | **実装順・milestone・受入条件の正本** |
| [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) | main / branch / accepted gate / 未検証の実装事実 |
| [N90_ACCEPTANCE_2026-09-18.md](docs/N90_ACCEPTANCE_2026-09-18.md) | stable 0.1.0 / A15 Windows受入 |
| [RELEASING.md](docs/RELEASING.md) | canonical version source, build identity, release manifest |
| [CAD_EDITOR_SPEC.md](docs/CAD_EDITOR_SPEC.md) | Scene、操作、保存、座標、wall/opening、非同期契約 |
| [CAD_EDITOR_ACCEPTANCE.md](docs/CAD_EDITOR_ACCEPTANCE.md) | fixture、DPI/性能、A01〜A15 |
| [UI_DESIGN.md](docs/UI_DESIGN.md) | native CADの画面・mouse/keyboard設計 |
| [PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | CAD-first製品スコープとrelease方針 |
| [PRODUCT_DOMAIN_BOUNDARY_CHARTER.md](docs/PRODUCT_DOMAIN_BOUNDARY_CHARTER.md) | domain所有深度（Tier A/B/C）とcanonical domain mapの製品charter |
| [CAD_EDITOR_OSS_RESEARCH.md](docs/CAD_EDITOR_OSS_RESEARCH.md) | 3D CAD/OSS調査、採否、参照コード |
| [ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md](docs/ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md) | Issue #101のwave/geometric hybrid、OSS、CPU/GPU、材料、validation調査 |
| [COMPETITIVE_PRODUCT_RESEARCH_2026-09-19.md](docs/COMPETITIVE_PRODUCT_RESEARCH_2026-09-19.md) | 類似製品比較、HTDTの機能gap、追加/修正候補と優先順位 |
| [ADR-0001](docs/adr/0001-native-cad-editor-stack.md) | native CAD editor技術決定 |
| [DATA_AND_ANALYSIS.md](docs/DATA_AND_ANALYSIS.md) | 不変履歴、比較、座標、保存契約 |
| [MEASUREMENT_WORKFLOW.md](docs/MEASUREMENT_WORKFLOW.md) | REW / Windows / AVRの測定境界 |
| [PLACEMENT_OPTIMIZATION_ROADMAP.md](docs/PLACEMENT_OPTIMIZATION_ROADMAP.md) | 配置探索算法。O70/O80、O90A〜D、O100A〜Gの実装済みauthority・workflow UXとO90E・UX160・owned-room gateを含む |
| [O90_ROBUST_OPTIMIZATION.md](docs/O90_ROBUST_OPTIMIZATION.md) | 設置誤差・入力不確かさに対するrobust/tolerance-aware最適化の正式仕様 |
| [O100_SYSTEM_EXPANSION_OPTIMIZATION.md](docs/O100_SYSTEM_EXPANSION_OPTIMIZATION.md) | 仮想SL/SR等の追加、system topology/equipment/placement比較、As-built/Measured移行の正式仕様 |
| [ROOM_GEOMETRY.md](docs/ROOM_GEOMETRY.md) | polygon room geometry contract |
| [PLACEMENT_CONSTRAINTS.md](docs/PLACEMENT_CONSTRAINTS.md) | placement hard constraints |
| [REW_API.md](docs/REW_API.md) | REW API契約 |
| [SUPPORT_AND_EXPORTS.md](docs/SUPPORT_AND_EXPORTS.md) | サポート画面の診断・診断パッケージ・設置ハンドオフ等の書き出し面 |

## 開発運用

Windows実機確認が必要な場合のlocal worktreeは `C:\Users\ka092\Desktop\HTDT\repo` です。

ただし、**計画、設計判断、実装記録、検証結果、進捗、成果物の正本はGitHubに残します**。検証はlocalのbackend test suite (`backend/tests`) とpackaging/validation gate (`scripts/build-native.ps1`, `scripts/check_dependency_lock.py` 等)で行い、RDCは実機GPU/UI/installer等でしか確認できないgateへ限定します。

### Synthetic O70/O80 development demo

物理測定を待たず最適化software pathを確認する場合は、通常データと分けたdata directoryへ明示的なsynthetic fixtureを作成できます。

```powershell
python -m htdt.native_cad --data-dir .\\demo-data --seed-synthetic-demo
python -m htdt.native_cad --data-dir .\\demo-data --document-id htdt-synthetic-o70-o80-demo-v1
```

このfixtureは実測ではなく、owned-room recommendation gateを開きません。詳細は [docs/O70_O80_SYNTHETIC_COMPLETION.md](docs/O70_O80_SYNTHETIC_COMPLETION.md)。
