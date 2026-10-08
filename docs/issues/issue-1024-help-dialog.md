# Issue #1024 — ヘルプ長文のスクロール + 関連操作への実行導線

## スコープ

`workflow_help.py::HelpDialog` は各トピック本文を折返し QLabel として
`QVBoxLayout` 直下に積み、`resize(480,360)` 固定だった。QScrollArea が
無いため 10+ セクションの長文・40+ 行のショートカット一覧は低解像度・
200% テキストで下端まで届かず、`related_commands` も
「関連操作: A、B」の文字列だけで遷移できなかった。同ファイルの
`GlossaryDialog` が持つ QScrollArea + `htdt-topic:` リンクの作法を
ヘルプ本体へ移植する。新しい workspace やヘルプ機能の再実装はしない。

## 実装

- `HelpDialog.__init__` 再構成:
  - 見出しは dialog 直下に固定 (wordWrap 有効)、本文は
    `_BodyScrollArea` (QScrollArea, widgetResizable) 内の host へ収容。
  - `_BodyScrollArea.keyPressEvent` が PageUp/PageDown/Home/End/↑/↓ を
    縦スクロールバーへ変換 — プレーンな QScrollArea はこれらのキーを
    処理せず、キーボード/スクリーンリーダーだけでは末尾へ到達不能
    だった (子 widget フォーカス時も未処理キーは親へ伝播して拾う)。
    StrongFocus + accessibleName (ヘルプ本文 / Help body) 付きで
    初期フォーカスはスクロール領域。
  - `_fit_to_screen` が `screen().availableGeometry()` の 90% を上限に
    maximumSize + resize を決定 (DIP 単位なので DPI/テキスト倍率に
    自動追従)。最小 240×200 で潰れない。既定サイズは 560×420。
  - 「閉じる / Close」ボタンを追加 (Esc のみだった抜け道を明示)。
- `HelpDialog.topic` に `help_registry` 引数を追加:
  - `related_commands` — 各 command id を QPushButton 化。
    `CommandRegistry.availability()` の現在値で enabled を決め、
    無効時は `localized_message(locale)` を理由ラベル + tooltip に
    表示。クリックは `CommandRegistry.execute()` へ投げるだけ —
    freeze ゲート・availability provider・deep-link handler・
    error handler を必ず経由し、ヘルプ本文から権威操作を直接実行
    しない。dispatch 成功時のみ dialog を閉じて遷移先を見せる
    (アプリ側 navigation history が戻り文脈を保持)。
    registry 未供給 (measurement fallback 等) は
    「実行できません」、未知 id は「利用できません」の平文行へ降格。
    有効ボタンにはショートカット hint を併記。
  - `related_topics` — `関連ヘルプ:` 行を `htdt-topic:` アンカーで
    描画 (GlossaryDialog と同じ形式)。クリックは該当トピックを
    ネストした modal exec で開き、閉じると元の dialog へ戻る。
    未知トピックは `id（利用できません）` の平文へ降格。
- `workflow_application._open_help_topic` と
  `measurement_page_workspace._show_help_topic` が help_registry を
  引き渡すように配線。`GlossaryDialog._open_related_topic` も
  ネスト先へ registry を伝搬。
- モーダル exec のままなので、ヘルプを閉じても進捗/dirty state に
  副作用は出ない (execute は palette と同じゲートを通る)。

## 非目標

- `topic.deep_links` フィールドのボタン化 — registry に raw deep-link
  の公開 dispatch 経路が無く、issue の範囲外。コマンド経由で代替。
- GlossaryDialog 本体の chrome ローカライズ — 本 issue では触らない。

## 検証

- `backend/tests/test_issue_1024_help_dialog.py` (新規) —
  見出し固定 + 本文スクロール、PageUp/PageDown/Home/End/↑↓で
  末尾まで到達、40 ショートカット完全到達 (最終行が viewport 内)、
  DPI 行列 (480×360 〜 2560×1440 + 200%相当) のサイズ上限、
  execute 経由の dispatch/閉鎖、deep_link→workspace 遷移、
  freeze/blocked コマンドの理由付き無効化、未知 command/topic の
  平文降格、ネストトピック遷移、JA/EN wrap + chrome ローカライズ、
  出荷 registry 全 related_commands の描画網羅。
- `backend/tests/test_workflow_help.py` 更新 — 関連操作が
  ボタン + 平文降格の新形状を検証。
- 実機 GUI (Mesa GL): 200% テキスト相当での下端までの閲覧と
  関連操作ボタンからのワークスペース遷移を目視確認。

## 関連

#959 (first-run wizard), #975 (アクセシビリティ), #804 (UI実機),
#936 (習熟度), #776 (availability reason codes), #585/#623 (palette
help provider / help registry), REV32-TERMS (glossary surface)。
