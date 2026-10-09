# #990 Reference Library を横断検索・比較・出典確認できるようにする

## 問題

`ReferenceLibraryPage` は機材のメーカー/モデル/バージョン 3 列表と
ファミリ別一覧を持つだけで:

- 材料・規格・スピーカー等のファミリ横断検索がなく、
  出典 (source) 名やバージョン、権限 ID では探せなかった。
- 行を選んでも権威 (authority) の kind/id/version/SHA、出典区分
  (measured / manufacturer / analytic …) や権利状態が表示されなかった。
- 同名・同モデルの別バージョン/別メーカーを比較する手段がなく、
  入れ替え時に失効し得る参照元 (scene entity / installed instance)
  が見えなかった。
- シーン内で使われている機材からライブラリ側の権威詳細へ
  辿る導線がなかった。

## 実装

### `backend/src/htdt/reference_library_browser.py` (新規・Qt 非依存)

既存の権威サービスだけを読む read-model。新しい権威を一切作らない。

- `collect_library_rows(index, *, detail_resolver, usage_sites)`
  → `LibraryRow`。アーカイブも含め全エントリを収集。
  `(family, identity)` グループ内で「`is_latest` を自称する行の
  うち最大バージョン」を最新とみなし、他は旧版 (superseded) とする —
  同名・同 ID でバージョン違いの行は絶対に併合しない
  (識別子は常に `identity@version#authority_hash` の semantic_key)。
- `build_reference_library_detail_resolver(scene_repository, data_dir)`
  — `EquipmentLibraryService` / `CadSpeakerLibraryRepository` /
  `CadMaterialLibraryRepository` / `StandardsProfileLibraryService` /
  プロジェクター・触覚の同梱リファレンス pack を `authority_hash` で
  突合するスナップショット resolver。未解決は `record_missing`
  (= 記録なし) として正直に表す。
  - 権利: スピーカー・材料は license / redistribution_permitted を
    集約 → `再配布可(名前)` / `再配布不可・制限あり` / `権利未確認`。
    機材定義に権利欄は存在しないため一律 `権利情報なし`。
  - 出典区分: `EquipmentDataProvenance.evidence_kind` や
    provenance_class を JA ラベル化 (実測/メーカー公称/解析/推定…)。
- `collect_usage_sites(scene_repository, document_id)`
  — 現行ドキュメントでの使用箇所を semantic_key ごとに集約。
  読むのは canonical な2系統のみ:
  `resolve_current_equipment_binding` (明示 binding・バリアント由来の
  双方) と `CadInstalledEquipmentRepository.resolve_instance_definition`
  の型付き解決 (RESOLVED_EXACT / CONFLICT / UNRESOLVED … をそのまま表示)。
  書込・rebind は一切しない。
- `filter_rows` — クエリ (NFKC+casefold で
  名前/メーカー/ID/バージョン/出典/カテゴリ/使用箇所ロール を横断) +
  ファミリ/カテゴリ/出典/状態フィルタ
  (すべて・最新のみ・要注意・未適格/アーカイブ)。
- `compare_rows` — 同ファミリの2行で項目ごとの差分と、
  各行の依存使用箇所 (入替で失効し得る参照元) を返す。

### `application_pages.py` — `ReferenceLibraryPage`

`library_index=None` なら従来表示のまま (後方互換)。index 付きでは
QTabWidget に「横断検索・比較」タブを追加:

- 検索欄 + ファミリ/区分/出典/状態コンボ。
- 結果表 6 列 (名前/種別/区分/スコープ/バージョン/状態バッジ)。
  UserRole に semantic_key、選択は行全体・複数選択可。
- **遅延描画**: 50 行ずつ「さらに読み込む」+ 「全 N 件 · a–b 件を表示」。
  0/100/1000 件でテスト済み。
- 詳細パネル (QScrollArea): 項目/値 2 列表に権威 kind/ID/
  バージョン/SHA-256/参照キー/スコープ/出典/根拠区分/権利/欠損根拠。
- **使用箇所**: site ごとに「開く」ボタン → `NavigationTarget`
  (SCENE_ENTITY / INSTALLED_EQUIPMENT_INSTANCE、intent=INSPECT) を発行。
- **比較**: 2 行選択で項目ごと A/B 差分 (差分セル強調) + 依存先一覧。

### 双方向ディープリンク (参照のみ・binding 変更なし)

- `InstallationPanel.libraryRequested` + 「ライブラリで確認」ボタン →
  `WorkflowDeepLink(LIBRARY, kind=EQUIPMENT_DEFINITION, intent=PROVENANCE)`。
- ライブラリ側 usage-site 行の「開く」→ `navigate_to_target` を
  `QTimer.singleShot(0)` 経由で呼ぶ (#1023 の signal 中 dispose 回避)。
- 採用 (adoption) は従来どおり明示フローのみ。この変更は参照導線。

## 誠実さの契約

旧版・記録なし・権利未確認・未適格の行はバッジで区別され、
いかなる適合認定も表示しない。機材の「権利情報なし」は
嘘ではなく license フィールド非保有の事実表示。

## テスト (`backend/tests/test_issue_990_reference_library.py`, 16件)

- 同名・同 ID の複数バージョン/別メーカーが ID 衝突せず、
  is_latest 自称が衝突しても最大バージョンのみ「最新」
- 名前/メーカー/ID/バージョン/出典/根拠区分/スピーカーロールでの検索
- 状態フィルタ4種・record_missing の未適格化と表示
- 詳細の kind/id/version/SHA/出典/根拠区分/権利の正確性
- 同ファミリ比較の差分と dependents、別ファミリ比較の拒否
- usage site 「開く」が正しい NavigationTarget を発行
- 50 行ページャ + 0/100/1000 件の遅延描画
- `focus_definition` がフィルタを跨いで行を reveal
- 実 SceneRepository での `collect_usage_sites` end-to-end
- 新規コントロール全てに accessibleName
