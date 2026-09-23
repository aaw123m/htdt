# HTDT Design System

Issue #118 と [UI_DESIGN.md](UI_DESIGN.md) の Visual / Interaction Language を Qt/3D で共有するための実装契約。正本の実装は `backend/src/htdt/ui_theme.py`。

この文書は navigation、command、domain/service authority を定義しない。Agent A の shell/navigation と Agent C の command system はこの design system を利用する側であり、ここで二重実装しない。

## 1. 使用原則

- dark-first を初期 release の authoritative appearance とする。
- 画面ごとに raw hex、独自 QSS、独自 margin/radius/control height を追加しない。
- pure black 一色で階層を作らない。surface token の相対明度で canvas/base/raised/overlay/modal を分ける。
- blur/transparency は temporary overlay の文脈保持に限る。plot/table/text/input surface は opaque を基本とする。
- UI accent は primary action、selection、active navigation/context、focus に限定する。
- warning/error/stale/unsupported と measured/predicted/scientific scale は accent から分離する。
- 状態は色だけで伝えない。icon、label、line style、outline 等を併用する。
- Apple 固有 asset/font/control は使用しない。Windows platform UI font を優先する。
- 3D viewport と scientific visualization は同じ token module を参照するが、UI accent を scientific scale として使わない。

## 2. Surface tokens

| token | value | 用途 |
|---|---|---|
| `surface.canvas` | `#0F141A` | 3D viewport / graph background |
| `surface.base` | `#141A21` | main page |
| `surface.raised` | `#1B232D` | sidebar / inspector / card |
| `surface.overlay` | `#222C38` | menu / popover / tool HUD |
| `surface.modal` | `#293542` | modal / sheet |
| `surface.separator` | `#2A3542` | 必要な group boundary |
| `surface.border_strong` | `#3A4858` | focus 以外で必要な stronger edge |

surface は全 component を箱で囲むための token ではない。separator/border は group boundary、input affordance、temporary overlay など意味のある箇所に限定する。

## 3. Accent / semantic / scientific colors

UI accent:

| token | value |
|---|---|
| `accent.primary` | `#6BA6FF` |
| `accent.hover` | `#83B5FF` |
| `accent.pressed` | `#4E8EEA` |
| `accent.selection_fill` | `#263D5C` |
| `accent.focus_ring` | `#6BA6FF` |

Semantic state:

| token | value | 意味 |
|---|---|---|
| `semantic.success` | `#68B98A` | 完了/有効 |
| `semantic.warning` | `#E1B15A` | 注意/要対応 |
| `semantic.error` | `#E47B7B` | error/failure |
| `semantic.stale` | `#B09BC6` | 要再計算/履歴 |
| `semantic.unsupported` | `#98A2AD` | unsupported/capability unavailable |

Scientific visualization:

| token | value |
|---|---|
| `scientific.measured` | `#4CC5B1` |
| `scientific.predicted` | `#BD9CF4` |
| `scientific.primary_trace` | `#7ED6FF` |
| `scientific.secondary_trace` | `#94A0AC` |
| `scientific.grid` | `#2E3945` |
| `scientific.cursor` | `#E9CE7A` |
| `scientific.scale` | `#440154 / #31688E / #35B779 / #FDE725` |

warning color を heatmap の通常値へ、accent color を measured/predicted へ流用しない。FR 等では measured/predicted を line style・badge text と組み合わせる。

## 4. Spacing / radius / control rhythm

Spacing は logical px の `4 / 8 / 12 / 16 / 24 / 32` のみを基本系列とする。

Radius:

- small: 4 px
- standard: 8 px
- large: 12 px

Control height:

- compact: 28 px
- standard: 34 px
- prominent: 40 px

prominent height は primary action など明確な理由がある場合だけ使う。全 control を pill にしない。

## 5. Typography

Windows platform UI font を使用する。利用可能なら `Segoe UI Variable`、次に `Segoe UI`、それ以外は Qt の platform default を使う。font を bundle しない。

| role | size | weight |
|---|---:|---:|
| workspace title | 18 px | 600 |
| section title | 14 px | 600 |
| body/control | 13 px | 400 |
| secondary/metadata | 12 px | 400 |
| numeric/readout | 13 px | 600 |

workspace title を web page の hero heading のように大型化しない。numeric/readout は寸法・Hz・dB の比較を優先する。

## 6. Qt application foundation

application composition root で一度だけ適用する。

~~~python
from PySide6.QtWidgets import QApplication

from htdt.ui_theme import apply_dark_theme

app = QApplication(...)
apply_dark_theme(app)
~~~

`ui_theme` 自体は import 時に global state を変更しない。native launcher / new shell の composition root が `QApplication` 作成直後に `apply_dark_theme(app)` を一度だけ呼ぶ。

QSS は標準 control に以下を共通定義する。

- hover
- pressed
- focus
- disabled
- checked/selected
- item selection
- menu/tab interaction
- input focus/disabled
- scrollbar/chrome

各 screen で同じ state の独自 stylesheet を再定義しない。

## 7. Dynamic property API

surface:

~~~python
from htdt.ui_theme import SurfaceRole, set_surface_role

set_surface_role(inspector, SurfaceRole.RAISED)
~~~

semantic state:

~~~python
from htdt.ui_theme import SemanticState, set_semantic_state

set_semantic_state(status_label, SemanticState.STALE)
# 状態解消時
set_semantic_state(status_label, None)
~~~

typography/control size/primary action:

~~~python
from htdt.ui_theme import (
    ControlSize,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_typography_role,
)

set_typography_role(title, TypographyRole.WORKSPACE_TITLE)
set_control_size(tool_button, ControlSize.COMPACT)
set_primary_action(run_button)
~~~

dynamic property helper は repolish まで行う。各 feature 側で `style().unpolish()/polish()` を複製しない。

## 8. 3D viewport reuse

3D 側では Qt stylesheet を参照せず token data を直接使う。

~~~python
from htdt.ui_theme import DARK_THEME

viewport.set_background(DARK_THEME.viewport.background.hex)

grid_minor = DARK_THEME.viewport.grid_minor.rgb01()
grid_major = DARK_THEME.viewport.grid_major.rgb01()
selection = DARK_THEME.viewport.selection_outline.hex
~~~

viewport token:

- background: `#0F141A`
- floor: `#171F27`
- grid minor/major: `#202A34 / #2D3945`
- geometry/edge: `#7A8794 / #A3ADB7`
- selection outline: UI selection accent
- handle: `#DDE8F7`
- gizmo X/Y/Z: `#D66A6A / #6EBD78 / #668FE0`

これは配色 authority だけを提供する。Room workspace の camera、lighting、overlay density、CAD interaction は Agent A/Room 実装側の責務であり、この module へ入れない。

## 9. 新しい token を追加する条件

新しい raw color/spacing/radius/control height を feature code へ直接追加する前に、既存 token で意味を表現できない理由を確認する。新しい semantic meaning が必要な場合だけ central token を追加し、この文書と contract test を同じ PR で更新する。

以下は禁止:

- screen 単位の巨大 `setStyleSheet()`
- component ごとに微妙に違う gray/blue を増やすこと
- warning と scientific scale の色共用
- measured/predicted を selection accent で代用
- decorative glass/blur を読み取り content に常設
- Apple 固有 font/control/asset の模倣

## 10. Integration points / remaining work

この design-system PR は navigation/router、command palette、workspace composition を実装しない。統合側の責務:

- Agent A: new shell/widget に surface/typography/control role を割り当て、composition root の theme install を維持する。
- Agent C: command/action の enabled/disabled/selected 状態を既存 command authority から UI property へ写像する。availability authority を theme module に持ち込まない。
- Room/3D: `DARK_THEME.viewport` を scene background/grid/selection/gizmo material へ接続する。
- Measurements/plots: `DARK_THEME.scientific` を measured/predicted/trace/scale に接続し、line style/label と併用する。
- UX150/UX160: Windows 実機で 100/150/200% DPI、focus visibility、Japanese text clipping、3D readability を最終調整する。

RDC はこの foundation 実装では使用しない。visual tuning の実機 gate は UX150/UX160 の統合時にまとめて行う。
