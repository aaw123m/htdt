# Issue #118 / UX130 — 測定 workspace integration

更新日: 2026-09-19

## 実装状態

UX130 の「測定」は、旧 `MeasurementWorkspaceWindow` / 「実測」dock の再配置ではなく、
shell に直接 mount する page workspace として実装する。

page は canonical workflow context と 1:1 で対応する。

1. `import` — 読み込み
2. `assignment` — 割り当て
3. `quality` — 品質
4. `comparison` — 比較

実装:

- `htdt.measurement_workflow.MeasurementWorkflowController`
  - Qt 非依存の薄い orchestration / view-model 境界
  - transient な import staging だけを所有する
- `htdt.measurement_page_workspace.MeasurementPageWorkspace`
  - `QWidget + QStackedWidget` の document-like workspace
  - `QMainWindow` / `QDockWidget` を使用しない
- `build_measurement_workspace_mount(controller)`
  - 既存 `WorkspaceMount` 契約へ接続する
- `create_measurement_workspace_factory(repository, document_id, ...)`
  - `build_canonical_workspace_registrations()` へ渡せる lazy factory

## Authority 境界

UX130 は measurement semantics を新設しない。

| concern | authority | UX130 の役割 |
| --- | --- | --- |
| measurement persistence / SceneRevision binding | `CadMeasurementRepository` | 保存・列挙を委譲 |
| REW text parsing | `parse_rew_frequency_response()` | 保存前 preview に利用 |
| REW text normalization | `normalize_rew_text()` | assignment 確定後に委譲 |
| REW API snapshot | `RewApiClient.get_frequency_response_snapshot()` / `normalize_rew_api_snapshot()` | background read と保存を委譲 |
| A/B calculation | `compare_frequency_responses()` | 計算を委譲 |
| comparison persistence | `CadMeasurementRepository.save_comparison()` | dataset / revision 固定保存を委譲 |
| quality | `CadMeasurementRecord.quality_status / quality_reasons / quality_source` | read-only 表示 |
| phase/timing capability | `CadFrequencyResponseDataset.phase_status` | 既存 Overview と同じく `valid` のみ利用可能として表示 |
| stale/current scene | record の `scene_content_hash` と current saved revision | 表示状態のみ導出 |

import staging は measurement evidence ではない。REW を読み込んだ直後は repository へ保存せず、
「割り当て」で measurement point / evidence type / channel role / source speaker /
radiation scope / routing evidence を指定した時点で、既存 normalizer と repository を通して
immutable record を保存する。

staging 後に current saved `SceneRevision` が変わった場合は fail closed とし、
新しい revision へ暗黙に付け替えない。REW を読み込み直して assignment を再確認する。

## predicted vs measured

「比較」page は `CadMeasurementRepository` 内で
`evidence_type == measured` と `evidence_type == predicted` として保存済みの
frequency-response dataset を候補にする。

`CadPredictionResult` の geometry mode / reflection result から frequency response を
UI 側で合成しない。そのような変換 authority は UX130 の責務外である。

比較計算・補間・overlap・指標は既存 `comparison.py` に委譲し、結果は
`save_comparison()` で exact dataset / exact SceneRevision に固定する。

## dark-first visualization

shared `DARK_THEME` token を使用する。

- measured: `DARK_THEME.scientific.measured` + solid line
- predicted: `DARK_THEME.scientific.predicted` + dashed line
- difference: `DARK_THEME.scientific.primary_trace` + dotted line
- surface / grid / text: shared surface/scientific tokens

色だけで measured / predicted を区別しない。

## Shell integration point

この PR は制約に従い `native_cad.py` と `workflow_shell.py` を変更しない。
shell composer 側では、既存 legacy measurement factory を次の factory へ差し替えるだけで mount できる。

```python
from htdt.measurement_page_workspace import create_measurement_workspace_factory
from htdt.workflow_navigation import WorkspaceId

factories[WorkspaceId.MEASUREMENT] = create_measurement_workspace_factory(
    repository,
    document_id,
)
```

factory が返す `WorkspaceMount` は次を接続済み。

- `on_activate -> workspace.refresh`
- `on_context_changed -> workspace.set_context`
- `on_entity_requested -> workspace.focus_entity`

canonical context ID は既存 `workflow_navigation.CANONICAL_WORKSPACE_CONTEXTS` の
`import / assignment / quality / comparison` をそのまま使用する。

## Command integration point

`MeasurementPageWorkspace.import_rew_text_dialog()` は既存
`measurements.import_rew` command の実行先として利用できる。

現行 `native_command_adapter` の measurement availability は旧 editor の
`_saved_measurement_target()` を前提とする legacy adapter なので、新 shell へ実際に
factory を切り替える integration PR では legacy availability を持ち込まず、
「保存済み SceneRevision が存在するか」を controller 境界で確認する binding に切り替える。
UX130 workspace 自体は command registry の authority を複製しない。

## Verification

追加テストは新しい境界だけを対象にする。

- import staging が保存を発生させない
- assignment 後の保存が既存 repository / normalizer を通る
- staging 後の SceneRevision change を fail closed にする
- quality / phase capability が保存済み record / dataset をそのまま投影する
- predicted vs measured comparison が既存 comparison authority で保存される
- workspace が dock を持たず、4 page と `WorkspaceMount` interface を公開する

REW parser、repository の immutable binding、comparison algorithm 自体は既存テストを
authority とし、UX130 用に重複テストしない。


## Integration update — 2026-09-19

The UX120–UX140 integration composition now uses this page workspace as the workflow-shell Measurement factory. `measurements.import_rew` is bound to this workspace on activation, and active REW background jobs block workspace disposal/destructive restore. The legacy measurement QMainWindow is no longer used by the `--workflow-shell` preview.
