# Issue #975 — 動的UIのフォーカス・読み上げ・無効化理由の統一

#941 `DecisionBriefPanel`（カード再生成）、#942 `GeometryIntakePanel`
（欠陥/修復提案テーブル再構築）、`MeasurementPageWorkspace` の品質
テーブル、`StandardsCriterionPanel` の評価更新を対象に、stable item ID
ベースのフォーカス維持・消滅時フォールバック、`StatusAnnouncement`
モデルの1回だけの通知、画面内「できない理由 + 解消先」の掲示を
共通契約として実装する。VTK 3D の実ジェスチャ/実スクリーンリーダー
読み上げ検査は #804 の owned-Windows manual gate に残す。

## Layer shape

- `dynamic_a11y.py`（新規、Qt 依存の共有ヘルパ）:
  - `STABLE_ID_ROLE = Qt.ItemDataRole.UserRole + 40` — テーブル/ツリー
    項目の安定識別子を束ねるロール（既存 `+41` 用途と衝突しない）。
  - `FocusToken(widget_name, is_view, item_id, widget_ref)` —
    `capture_focus(root)` が `QApplication.focusWidget()` を記録。
    対象外フォーカスは `None` を返し、refresh が関係ないフォーカスを
    盗まないことを保証する。
  - `restore_focus(root, token, fallback)` — アイテムビューは同じ
    stable id の行へ currentIndex を再設定し、行が消えた場合は
    キーボードの錨をビュー自体に残す（矢印キーで再アンカー可能）。
    生き残ったウィジェットは weakref で判定してフォーカスを移さず、
    壊れたウィジェットのみ同名 objectName へ再解決する。先頭で
    `DeferredDelete` + `MetaCall` の posted イベントを吐き出し、
    削除予定のゾンビと「表示予約中」の新コントロールを確定させてから
    解決する（`setRowCount(0)` や `deleteLater()` の破棄・子の暗黙的
    show はいずれも posted event として遅延されるため）。
  - `DynamicAnnouncer(target)` — `announce(event, text, urgent=)` は
    `announce_status` 経由で `QAccessibleAnnouncementEvent` を発行
    （urgent は assertive）。`announce_state(aspect, state, ...)`
    は `(aspect, state)` が変化した時だけ発火し、同一状態の
    再描画・ポーリングを黙らせる。発火した `StatusAnnouncement`
    を `events` に記録し、テストが sink ではなくモデルを検証できる。
  - `reason_label(text, parent)` — 無効化理由を同画面に置く
    word-wrap の SECONDARY `QLabel`（`StrongFocus` + テキスト選択
    可能）。無効ボタン自体は Tab 順から外れるため、理由は
    フォーカス可能なラベルでなければキーボードから読めない。
  - `disabled_hint(control_id, reason, resolution)` —
    `DisabledControlReason.display_text()`（`理由 — 解消: 解消先`）。
- `native_accessibility.py`:
  - `AnnouncementEvent` に `operation_blocked` / `operation_unblocked`
    を追加し `REQUIRED_ANNOUNCEMENTS` に登録。
  - `DisabledControlReason.resolution` 追加 + `display_text()`。
    人間可読チェックは「空白を含む or CJK を含む 8 文字以上」—
    日本語フレーズは空白を持たないため従来ルールでは検証不能だった。

## 各パネルの適用

- `GeometryIntakePanel` — `set_report` / `set_proposal` で
  `capture_focus` → 再構築 → `restore_focus`（fallback は診断/派生
  ボタン）。欠陥・提案の0列に `STABLE_ID_ROLE`（`defect_id` /
  `action_id`）を束ね、セル内ボタンに `intake-accept:{id}` 等の
  安定 objectName を付与。`set_stage` が `stage_hint` に
  「できない理由 — 解消: 解消先」を連結表示し、診断/派生の
  disabled 理由を `room_workspace._sync_geometry_intake_panel` が
  状態（対象なし / 提案なし / 決定未完了 / 派生済み）から生成。
  診断・判定・決定の各遷移を aspect 付きで1回通知（stale は
  `result_stale`、失敗は assertive の `operation_failed`）。
- `DecisionBriefPanel` — `refresh()` 全体を try/finally で包み、
  例外時でも `restore_focus` が走る。カードに
  `brief-action:{action_id}`、ボタンに `brief-next:` / `brief-gap:`
  objectName。unreachable のボタンはカード内に `brief-hint:` の
  reason_label を併記（ボタンはTab順から外れるため）。ブリーフの
  current/stale 遷移と保存・失敗を通知。カードは
  `QWidget(self.actions_container)` として生成 — 親なしの
  トップレベルウィジェットを `addWidget` で再親付けすると
  posted show が降りるまで非表示のまま残るため。
- `StandardsCriterionPanel` — `_sync_evaluate_enabled` が
  「プロファイルなし」「評価対象なし」の理由+解消先を
  `evaluate_hint` とボタン tooltip に反映。`refresh()` でツリーの
  `criterion_id`（列0の `UserRole`）に基づく currentItem を復元。
  評価結果を `evaluation_id` 単位で通知、ゲート制約の
  ブロック/解除を `operation_blocked` / `operation_unblocked` で
  状態遷移として通知。
- `MeasurementPageWorkspace`（品質ページ）— `quality_table` に
  objectName と measurement_id（列0 `UserRole`）。`_refresh_quality`
  でフォーカスを捕獲・復元し、フィルタで選択行が消えても錨は
  テーブルに残す。`_sync_quality_actions_enabled` が
  retake/disposition/correct/attach の無効化理由を
  `disposition_label` / `retake_label` と tooltip に併記。
  `quality_report_state != 'current'` の選択行は
  `result_stale` を measurement_id 単位で1回通知。disposition
  適用・添付・dataset-level 参照の成功/失敗も通知。

## 確認済みのこと / 残ること

- `backend/tests/test_dynamic_a11y.py` — fake
  `QAccessibleAnnouncementEvent` sink で 1 回通知・urgent マッピング・
  フォーカス復元（view の stable id / 同名再解決 / 消滅時ビュー残し）・
  各パネルの理由ラベル・ブロック/解除通知を検証（offscreen、
  実受入ではない）。
- `scripts/ux160_acceptance_run.py` に `dynamic_a11y` チェックを追加
  （`ALL_CHECKS` と fixture lane の両方）。`ux160_driver.
  check_dynamic_a11y` がアナウンサー保持パネルを探索し、stable
  アンカーのフォーカス保持・同一状態再描画の沈黙・画面内理由の有無を
  採点する。offscreen fixture run は引き続き「証拠採取」であり
  受入ではない。
- 実 Windows UIA 読み上げ・実スクリーンリーダーでの確認は
  #804 owned-Windows manual gate の範囲。本 PR では Qt 側の
  `QAccessibleAnnouncementEvent` 発行を sink で検証するに留める。
