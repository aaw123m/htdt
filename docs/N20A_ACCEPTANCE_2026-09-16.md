# N20a Windows acceptance — A05 / A06

> 実施日: 2026-09-16  
> 対象: `feat/n20a-basic-transform-snap`  
> 実機: Windows 11 x64 / Ryzen 7 8845HS / Radeon 780M / 31.3 GB / 2880×1800 / OS 200% DPI  
> Python: 3.12.10 / N05でfreezeしたWindows依存環境

## 対象

正本 `IMPLEMENTATION_ROADMAP.md` N20a と `CAD_EDITOR_ACCEPTANCE.md` A05/A06について、N05/N10のF1 fixtureを使いnative Qt/PyVista editorをWindows実機で操作した。

N20aで追加した主な契約:

- entity body poseのnormalized quaternion orientation
- speaker body poseと`aim_xyz`の分離
- domain left-handed pose → VTK right-handed poseの`C4 * Td * inverse(C4)`変換
- Move / Rotateの共通preview→commit/cancel lifecycle
- world X/Y/Z translation / rotation gizmo
- 位置grid snapとangle snap
- Inspector XYZ / Yaw-Pitch-Roll編集
- Esc / window外release / deactivate / tool・view切替 / capture lossのcancel

## 自動検証

Windows実機の開発環境で実施。

- N20a focused domain/editor/snap tests: **18 pass**
- backend全回帰: **pass**（既存1 skip）
- 既知warning: Starlette/httpx関連deprecationのみ

orientation、snap、Undo/Redo、unknown aim保持、domain→render pose変換は回帰損害が大きいため自動テスト対象とした。Qt/VTKの実マウス操作はunit testへ置き換えず、以下の実機受入で確認した。

## A05 — 基本Move / Rotate / snap

### Move

OS実マウス入力でFL speakerをworld axis dragし、0.10 m grid snapを有効化した。

| View | 操作axis | 結果 |
|---|---:|---|
| Top | X | pass。Xのみ変化、0.10 m刻み、Undo復元 |
| Front | Z | pass。Zのみ変化、0.10 m刻み、Undo復元 |
| Side | Y | pass。Yのみ変化、0.10 m刻み、Undo復元 |
| Perspective | X | pass。Xのみ変化、0.10 m刻み、Undo復元 |

すべてのviewでdomain world値を正本とし、camera/view依存値は保存していない。移動前後ともFLの`aim_xyz=None`を保持した。

### Rotate

OS実マウス入力でrotation ringをdragし、15° angle snapを有効化した。

| View | world axis | 結果 |
|---|---:|---|
| Top | Z | pass。yaw -45°、Undo復元 |
| Front | Y | pass。pitch -45°、Undo復元 |
| Side | X | pass。roll -45°、Undo復元 |
| Perspective | Z | pass。yaw -45°、Undo復元 |

body poseはnormalized quaternionとして保存し、Inspector表示/入力のみYaw-Pitch-Rollへ変換する。body rotate後もFLの`aim_xyz=None`を保持したため、筐体poseとspeaker acoustic aimは混同していない。

### Inspector

- Xを1.50 mへ数値編集 → SceneDocumentへ同じMove command経路で反映 → Undo pass
- Yawを30°へ数値編集 → quaternionへ変換 → Undo pass
- Inspector rotate後もunknown aim保持

### rotation ring操作性

初回実機受入で、200% DPI時にrotation ringの投影位置が整数pixelへ丸められるとpickerが外れるケースを検出した。ring tube半径を`max(radius*0.040, 0.008 m)`へ拡大し、Front/Y ringを含む4 viewで実マウスdragを再確認した。

これは受入ハーネス用の特例ではなく、高DPI環境の実ユーザーがringを掴みやすくするUI改善として実装へ反映した。

**A05 result: pass**

## A06 — cancel / capture / input state

FLのMove previewを実際に開始し、以下をそれぞれ独立に実施した。

| ケース | 結果 |
|---|---|
| Esc | pass。preview前位置へ復元 |
| window frame外でbutton release | pass。preview前位置へ復元 |
| Alt+Tab / WindowDeactivate | pass。preview前位置へ復元 |
| Move→Rotate tool切替 | pass。preview cancel後にtool切替 |
| Perspective→Top view切替 | pass。preview cancel後にcamera切替 |
| mouse capture loss | pass。preview前位置へ復元 |

各ケースで確認した事項:

- committed Scene値がbeforeと一致
- CommandHistory長が増えない
- recovery snapshotへ未確定値を保存しない
- Qt mouse grabが残留しない
- cancel後すぐ通常Moveが可能
- cancel後すぐRotateが可能
- selection解除後の通常Orbitが可能

### capture loss実装

PySide6実機では`releaseMouse()`時に`QEvent.UngrabMouse`がviewportへ通知されなかった。`QWidget.mouseGrabber()`は即座に`None`へ変化したため、active preview中だけ40 ms周期の`QTimer`でgrab ownerを監視し、viewport以外になった時点で同一`cancel_preview()` transactionへ流す方式にした。

通常release時は明示grabがrelease callbackまで維持されるため、正常commitと競合しない。watchdogはpreview開始時だけ起動し、commit/cancel/closeで停止する。

**A06 result: pass**

## DPIについて

今回のA05/A06は実OS **200% DPI** で実施した。VTK display pixelはdevice pixel、Qt widget/global位置はlogical pixelであるため、受入ハーネスではrender-window heightとDPRを使い明示変換した。

window外release検証ではlogical desktop外の座標を使わず、同じphysical screen内かつwindow frame外の点を選択した。これによりQtのdesktop外座標fallbackを誤って「window外release」と判定しないようにした。

## 非スコープ

N20a完了を以下の完成とは扱わない。

- multi-select / common pivot
- vertex / edge / midpoint / alignment snap
- persistent group / transform hierarchy
- room sketch / wall editing
- Resize / scale gizmo

これらは正本ロードマップのN20b以降で扱う。
