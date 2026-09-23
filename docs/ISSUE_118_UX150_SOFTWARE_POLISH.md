# Issue #118 — UX150 software visual / interaction / language polish

Date: 2026-09-19

## Scope

UX150のうち、Windows実機を使わずGitHub Actions / offscreen Qtで検証できる品質を固定する。

このsliceはdomain/service/SceneRevision/evidence semanticsを変更しない。
UX160のowned-Windows visual acceptanceを代替しない。

## Shared visual language

`ui_theme.py` をauthorityとして次を共通化する。

- radius: 6 / 10 / 14 px
- control height: 30 / 36 / 42 px
- raised QFrameのsubtle border / radius
- workflow rail / context barはedge separatorへoverride
- workspace navigation checked state
- status bar separator
- splitter handle hover
- scroll area / checkbox / combo rhythm
- existing hover / pressed / focus / disabled / checked / selected semanticsを維持

各workspaceへraw colorを追加しない。

## Responsive shell

`WorkflowRail`:
- expanded: 184 logical px
- compact: 112 logical px
- shell width < 1120 logical pxでcompact
- compact時はbrandを退避し、workspace labelsは維持

`TopContextBar`:
- compact時は重複情報であるworkspace titleを退避
- context actionは残す
- margin / spacingだけ縮退

これは高DPI時にglobal navigationがcontentを圧迫しないためのprogressive disclosureであり、
機能やworkspace destinationを隠すものではない。

## Room responsive layout

通常幅:
- Objects/Placementではobject paletteを表示
- right contextual panelを約300 logical px

compact:
- viewportを優先し、object paletteは既定で閉じる
- 「オブジェクト追加」で必要時だけtoggle
- right contextual panelを280 px、さらに狭い場合260 pxへ縮退
- Geometry / Acoustics / selection authorityは変更しない

## 3D viewport readability

Room viewport:
- neutral dark floor surface
- 0.5 m minor grid
- 2.0 m major grid
- gridをfloorよりわずかに前面へ出してz-fightingを回避
- room outline contrastを調整
- entityへ低specularのneutral ambient/diffuse lighting
- selection outline / acoustics / scientific colorsは既存semantic tokenを維持

floor/grid/lightingはpresentation onlyであり、Scene geometryやsolver inputではない。

## Measurements plot readability

PyQtGraph共通設定:
- lower-contrast grid
- log-frequency axisを維持
- axis text spacing / content margin
- view padding
- clip-to-view
- peak-preserving automatic downsampling

measured/predicted scientific color semanticsは変更しない。

## Optimize layout and language

Candidates:
- side panel minimum widthを430 -> 300 logical pxへ縮退
- viewport側stretchを強める
- default splitter ratioを設定

User-facing copy:
- 通常ラベルは日本語優先
- REW / FR / SPL / GP / O80 / Extended等、自然なtechnical termは維持

## Software layout acceptance matrix

Windows scalingを固定physical displayから得られるlogical client areaとしてモデル化し、
offscreen Qtで次を全て確認する。

| physical | scaling | logical |
|---|---:|---:|
| 1280×800 | 100% | 1280×800 |
| 1440×900 | 100% | 1440×900 |
| 1280×800 | 150% | 853×533 |
| 1440×900 | 150% | 960×600 |
| 1280×800 | 200% | 640×400 |
| 1440×900 | 200% | 720×450 |

Gate:
- shell compact stateがthresholdと一致
- canonical 4 workspace context buttonsが全てvisible
- context buttonsがoverlapしない
- context controlsがcontainer外へclippingしない
- Room compact layoutでpaletteがviewportを常時圧迫しない
- paletteはexplicit actionで再表示可能
- right contextual panelは狭幅で縮退

## UX160 boundary

このsoftware gateでは最終判定しないもの:

- 実フォントrasterization
- Windows compositor / GPU / VTKの実際の見え方
- 100/150/200% DPIの実機pixel density
- human first-use discoverability
- motionの主観品質
- screenshotによるdark 3D readability
- actual mouse shortcut feel

これらはUX160でowned-Windows visual acceptanceとして一度に確認する。

RDC is not used in this UX150 software slice.


## Japanese-first copy / internal-ID audit

UX150 review additionally treats the following as normal-user presentation debt rather than
domain semantics:

- standard Optimize lists show `候補 1`, `実測候補 1`, `検証条件 1`, `検証 1`
  instead of truncated candidate / measurement / campaign / validation IDs
- raw IDs continue to be stored in Qt `UserRole` and repository records; selection and
  persistence authority are unchanged
- Pareto UI keeps objective IDs internally but presents natural labels such as
  `応答形状 RMS`, and evidence is shown as `実測 / 予測 / 派生 / 仮説`
- SearchSpec / SceneRevision / stale / Campaign / ValidationRecord / authority /
  evidence jargon is removed from the normal task path and replaced by task-oriented Japanese
- REW UUIDs and SceneRevision IDs are not used as fallback display labels
- algorithm/model names, REW, Pareto, GP, O70/O80A, yaw and toe-in remain where they are
  natural technical terms, especially under progressive-disclosure advanced controls

Internal provenance values are not deleted; they are moved out of the standard presentation.
