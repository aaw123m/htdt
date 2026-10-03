"""Inline explanations for the measurement surface (REV32-TERMS).

One canonical registry of plain-language JA explanations covering every
field, column and metric the measurement workflow shows: what it MEANS,
its unit (when there is one) and what values are valid — so a first-time
operator never has to leave the app to understand a term.

Two application paths:

* ``apply_explanation`` / ``explain_table_columns`` install tooltip +
  WhatsThis text at mount time (static: inputs, combos, table headers);
* ``status_explanation`` / ``metric_explanation`` answer meaning +
  next-step text for the *status codes* and *metric names* emitted into
  table cells at fill time.

Where a domain term or a help topic exists the explanation links to it
(``term`` → glossary entry, ``topic`` → help topic id), keeping this
registry the single source of truth for "what does this thing mean".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from .localization import PresentationLocale, TermId, term_text

if TYPE_CHECKING:
    from PySide6.QtWidgets import QFormLayout, QTableWidget, QWidget


@dataclass(frozen=True, slots=True)
class FieldExplanation:
    """Operator-facing explanation of one field / column / metric."""

    meaning: str
    unit: str | None = None
    valid: str | None = None
    term: TermId | None = None
    topic: str | None = None

    def text(
        self, locale: PresentationLocale = PresentationLocale.JAPANESE
    ) -> str:
        """Tooltip/WhatsThis body: meaning, then unit and validity hints."""
        parts = [self.meaning]
        if self.unit:
            parts.append(f'単位: {self.unit}')
        if self.valid:
            parts.append(self.valid)
        if self.term is not None:
            parts.append(f'用語集: {term_text(self.term, locale)}')
        if self.topic:
            parts.append('詳しくは用語集・ヘルプボタンから関連トピックを開けます')
        return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Registry — dotted keys are stable identifiers; widgets bind by key.
# ---------------------------------------------------------------------------

FIELD_EXPLANATIONS: dict[str, FieldExplanation] = {
    # --- Import page -----------------------------------------------------
    'import.rew_measurement': FieldExplanation(
        'REW アプリケーション内の測定エントリです。「REWから一覧を更新」で'
        'REW が起動中の場合に取得できます。',
        term=TermId.REW,
        topic='workflow.measurements',
    ),
    'import.attachment_kind': FieldExplanation(
        '一括添付で登録するファイルの種別です（測定ソース、校正データ、'
        'ノートなど）。種別に応じて解析・表示の扱いが変わります。',
    ),
    'import.batch_table.file': FieldExplanation(
        '取り込まれた REW テキスト測定の名前です。'
        'インポート時のモード表記を含みます。',
    ),
    'import.batch_table.status': FieldExplanation(
        'この項目の処理状態です。保留中はまだ保存されていません。',
    ),
    'import.batch_table.band': FieldExplanation(
        '測定の周波数応答が有効な帯域です。',
        unit='Hz',
        term=TermId.FREQUENCY_RESPONSE,
    ),
    'import.batch_table.phase': FieldExplanation(
        '位相データ（波形の位相成分）が測定に含まれるかを示します。',
        term=TermId.PHASE,
    ),
    'import.batch_table.duplicate': FieldExplanation(
        '既存の測定との重複判定です。同じ内容や同じ取得条件の測定が'
        '既にある場合に表示されます。',
    ),
    'import.batch_table.resolution': FieldExplanation(
        '重複した項目の扱いです。既存測定を再利用するか、'
        '新しい測定として保存するかを選びます。',
    ),
    'import.batch_table.saved': FieldExplanation(
        '保存された測定と割り当てです。',
    ),
    # --- Assignment page -------------------------------------------------
    'assignment.scope': FieldExplanation(
        'このフォームが編集する対象です。新しい測定の割り当て、'
        '既存割り当ての訂正、一括取り込み項目のいずれかになります。',
    ),
    'assignment.target': FieldExplanation(
        '測定位置・受音位置です。マイクを置いたシーン上の実体を選びます。',
        term=TermId.LISTENING_POSITION,
    ),
    'assignment.evidence_type': FieldExplanation(
        '証拠種別です。実測は収録された応答、予測はシミュレーション結果、'
        '派生は他の測定から生成したデータです。',
        term=TermId.MEASURED,
        topic='concept.evidence_vs_assumption',
    ),
    'assignment.channel_role': FieldExplanation(
        '入力役割です。この測定データが表すスピーカーの役割'
        '（FL=左フロント、C=センター、Sub=サブウーファーなど）を示します。',
    ),
    'assignment.radiation_scope': FieldExplanation(
        '放射範囲です。この測定が単一音源だけの応答か、'
        'バスマネジメントなど複数音源の合成応答かを示します。',
        term=TermId.BASS_MANAGEMENT,
    ),
    'assignment.routing_evidence': FieldExplanation(
        'ルーティング根拠です。信号経路が検証済みか、手動設定か、'
        '推定かを示します。',
        term=TermId.UNVERIFIED,
        topic='concept.evidence_vs_assumption',
    ),
    'assignment.acquisition_revision': FieldExplanation(
        '取得時の配置です。この測定を結びつけるシーンリビジョンを'
        '選びます。測定時の部屋配置を記録するために使います。',
        term=TermId.SCENE_REVISION,
        topic='concept.scene_vs_revision',
    ),
    'assignment.routing_profile': FieldExplanation(
        '保存済みの検証済みルーティング設定です。'
        '過去に確認した配線・処理構成を再利用します。',
    ),
    'assignment.source_speakers': FieldExplanation(
        'この測定で実際に音を出したスピーカーです。'
        'バスマネジメント等で複数鳴らした場合はすべて選びます。',
    ),
    # --- Acquisition conditions ------------------------------------------
    'acquisition.preset': FieldExplanation(
        '保存済みの取得条件（マイク・AVR 設定の一式）です。'
        '再利用すると同じ条件で測定を記録できます。',
        term=TermId.ACQUISITION_CONTEXT,
    ),
    'acquisition.mic_orientation': FieldExplanation(
        '測定時のマイクの向きです。0°は天井方向（上向き）、'
        '90°はスピーカー方向（前向き）が一般的です。',
        unit='deg',
        valid='妥当値: 0 / 90 / 180 など 0–360 の角度',
    ),
    'acquisition.mic_manufacturer': FieldExplanation(
        '測定に使ったマイクのメーカー名です。',
    ),
    'acquisition.mic_model': FieldExplanation(
        '測定に使ったマイクのモデル名です。',
    ),
    'acquisition.mic_serial': FieldExplanation(
        'マイクのシリアル番号です。校正ファイルが個体別の場合に'
        '一致確認に使います。',
    ),
    'acquisition.sample_rate': FieldExplanation(
        '測定時のサンプルレートです。',
        unit='Hz',
        valid='妥当値: 44100 / 48000 / 96000 などの標準値',
    ),
    'acquisition.calibration_file': FieldExplanation(
        'マイク校正ファイルの名前です。周波数特性の補正に使います。',
        term=TermId.CALIBRATION,
    ),
    'acquisition.calibration_sha': FieldExplanation(
        '校正ファイルの SHA-256 ハッシュです。'
        'ファイルが同一かどうかを確認するための指紋です。',
        term=TermId.CALIBRATION,
        valid='妥当値: 64 桁の 16 進数',
    ),
    'acquisition.output_device': FieldExplanation(
        '測定信号の出力に使ったデバイス（オーディオインターフェース等）'
        'です。',
    ),
    'acquisition.avr_model': FieldExplanation(
        '測定に使った AV アンプ・レシーバーのモデル名です。',
        term=TermId.AVR,
    ),
    'acquisition.avr_volume': FieldExplanation(
        '測定時の AVR ボリューム設定です。再現性確認に使います。',
        term=TermId.AVR,
        unit='dB',
        valid='妥当値: AVR の表示範囲（例: -80 ～ 0）',
    ),
    'acquisition.avr_processing': FieldExplanation(
        '測定時の AVR 処理モードです（ステレオ / ダイレクト / '
        'サラウンドなど）。処理の有無が測定結果に影響します。',
        term=TermId.AVR,
    ),
    'acquisition.avr_peq': FieldExplanation(
        'AVR のパラメトリック EQ モードです。EQ が有効だと'
        '測定結果に反映されます。',
        term=TermId.PEQ,
    ),
    # --- Campaign page ---------------------------------------------------
    'campaign.plan': FieldExplanation(
        '実行する測定計画です。「計画を作成」で作った全×全計画か、'
        '登録済みの詳細計画を選びます。',
        topic='workflow.measurements',
    ),
    'campaign.sources': FieldExplanation(
        '計画に含める音源です。チェックしたスピーカー×入力役割ごとに'
        '測定セルが作られます。',
    ),
    'campaign.targets': FieldExplanation(
        '計画に含める測定位置です。チェックした位置ごとに'
        '測定セルが作られます。',
        term=TermId.LISTENING_POSITION,
    ),
    'campaign.purpose': FieldExplanation(
        'セルの目的です。測定=通常の収録、校正=校正用、'
        'ホールドアウト=検証用に取っておく、診断=問題切り分け用。',
    ),
    'campaign.repeat': FieldExplanation(
        '1 セルあたりの繰り返し測定回数です。'
        '繰り返し精度の確認に使います。',
        valid='妥当値: 1–16 回',
    ),
    'campaign.pattern': FieldExplanation(
        'ターゲットパターンです。グリッド・ラインなどの'
        'パターンで測定位置を一括選択します。',
    ),
    'campaign.variant_plan': FieldExplanation(
        'システムバリアントや検証ワークフローが登録した'
        '詳細な測定計画です。そのまま実行用セルとして開きます。',
    ),
    'campaign.measurement': FieldExplanation(
        '選択中のセルに登録する測定です。測定後に結果を'
        'セルへ結びつけます。',
    ),
    'campaign.table.channel_role': FieldExplanation(
        'このセルが必要とする入力役割です。',
    ),
    'campaign.table.target': FieldExplanation(
        'このセルの測定位置です。',
        term=TermId.LISTENING_POSITION,
    ),
    'campaign.table.repeat': FieldExplanation(
        '何回目の測定かを示します（リピート回数内の番号）。',
    ),
    'campaign.table.status': FieldExplanation(
        'セルの進行状態です。未着手 → 品質確認待ち → 完了、'
        'または要再測定・スキップ。',
    ),
    'campaign.table.measurement': FieldExplanation(
        'このセルに登録された測定です。',
    ),
    # --- Quality page ----------------------------------------------------
    'quality.table.channel': FieldExplanation(
        '測定の入力役割です（実効値）。',
    ),
    'quality.table.evidence': FieldExplanation(
        '証拠種別です（実測 / 予測 / 派生 / 未確認）。',
        topic='concept.evidence_vs_assumption',
    ),
    'quality.table.target': FieldExplanation(
        '測定位置です。',
    ),
    'quality.table.quality': FieldExplanation(
        '品質状態です。品質プロファイルのチェック結果に基づきます'
        '（PASS / WARN / FAIL / 不明）。',
        topic='trouble.unknown_value',
    ),
    'quality.table.phase': FieldExplanation(
        '位相データの有効性です。位相比較に使えるかを示します。',
        term=TermId.PHASE,
    ),
    'quality.table.timing': FieldExplanation(
        '共通タイミング基準の判定です。測定間で時間軸を'
        '揃えられるかを示します。',
        term=TermId.TIMING_REFERENCE,
    ),
    'quality.table.placement': FieldExplanation(
        '配置の一致です。測定時の配置と現在のシーン配置が'
        '同じかどうかを示します。',
        topic='concept.scene_vs_revision',
    ),
    'quality.table.band': FieldExplanation(
        '有効周波数帯域です。',
        unit='Hz',
        term=TermId.FREQUENCY_RESPONSE,
    ),
    'quality.table.disposition': FieldExplanation(
        'ライフサイクル状態です（有効 / 除外 / 誤割り当て / '
        'テスト / 重複取り込み など）。',
        topic='concept.lifecycle_states',
    ),
    'quality.table.retake': FieldExplanation(
        '再測定の推奨です。品質証拠が不足している場合に'
        '再測定を促します。',
    ),
    'quality.smoothing': FieldExplanation(
        '表示用の 1/N オクターブ平滑化です。見やすくするだけで'
        'データ自体は変更しません。',
        term=TermId.SMOOTHING,
        valid='妥当値: OFF / 1/3 / 1/6 / 1/12 / 1/24 オクターブ',
    ),
    'quality.target': FieldExplanation(
        'グラフに重ねて表示する目標カーブです。',
    ),
    'quality.phase_unwrap': FieldExplanation(
        '位相の ±180° の折返しを解除して連続した位相として'
        '表示します。',
        term=TermId.PHASE,
    ),
    'quality.spatial_mode': FieldExplanation(
        '空間表示のモードです。測定時の配置そのままか、'
        '現在のシーンとの差分を強調するかを選びます。',
    ),
    'quality.disposition': FieldExplanation(
        '測定のライフサイクル状態を記録します。除外や'
        '誤割り当ての記録は削除ではなく履歴として残ります。',
        topic='concept.lifecycle_states',
    ),
    'quality.attach_kind': FieldExplanation(
        '添付ソースの種別です（測定ソース / 校正 / ノートなど）。',
    ),
    # --- Comparison page -------------------------------------------------
    'comparison.preset': FieldExplanation(
        '候補リストの絞り込みプリセットです。比較可能な'
        '組み合わせに絞り込みます。',
    ),
    'comparison.dataset_a': FieldExplanation(
        '比較元のデータセット（A）です。',
    ),
    'comparison.dataset_b': FieldExplanation(
        '比較先のデータセット（B）です。',
    ),
    'comparison.band_low': FieldExplanation(
        '比較帯域の下限です。この周波数から上を比較します。',
        unit='Hz',
        valid='妥当値: 1–100000、上限より小さい値',
    ),
    'comparison.band_high': FieldExplanation(
        '比較帯域の上限です。この周波数までを比較します。',
        unit='Hz',
        valid='妥当値: 1–100000、下限より大きい値',
    ),
    'comparison.reference_band': FieldExplanation(
        'レベル合わせに使う参照帯域です。この帯域で両方の'
        'レベルを揃えてから差を計算します。',
        unit='Hz',
        valid='妥当値: 比較帯域内の範囲（例: 20–120 Hz）',
    ),
    'comparison.excluded_band': FieldExplanation(
        '指標計算から除外する帯域です。ノイズやノッチが集中する'
        '帯域を除いて差を評価します。',
        unit='Hz',
        valid='妥当値: 下限 < 上限 の組み合わせ',
    ),
    'comparison.smoothing': FieldExplanation(
        '比較表示用の平滑化です。見やすくするだけで'
        'データは変更しません。',
        term=TermId.SMOOTHING,
    ),
    'comparison.metrics.name': FieldExplanation(
        '指標の名前です。',
    ),
    'comparison.metrics.value': FieldExplanation(
        '指標の値です。',
        unit='dB',
    ),
    'comparison.history.created': FieldExplanation(
        '比較を保存した日時です（UTC 表記）。',
    ),
    'comparison.history.band': FieldExplanation(
        '実際に比較できた周波数帯域です（両データが有効な範囲）。',
        unit='Hz',
    ),
    'comparison.history.reference': FieldExplanation(
        'レベル参照帯域です。',
        unit='Hz',
    ),
    'comparison.history.excluded': FieldExplanation(
        '指標から除外した帯域です。',
        unit='Hz',
    ),
    'comparison.history.rms': FieldExplanation(
        'RMS 差: 帯域内の偏差の二乗平均平方根です。'
        '値が大きいほど差が大きいことを意味します。',
        unit='dB',
    ),
    'comparison.history.offset': FieldExplanation(
        'レベル差: 参照帯域で揃えたときの平均レベル差です。',
        unit='dB',
    ),
    'comparison.history.shape': FieldExplanation(
        '形状 RMS: レベル差を除いた形状だけの差です。',
        unit='dB',
    ),
    # --- Legacy measurement editor dock ----------------------------------
    'editor.pattern_anchor': FieldExplanation(
        'パターン作成の基準となる測定位置です。',
    ),
    'editor.pattern_spacing': FieldExplanation(
        'パターンの測定位置どうしの間隔です。',
        unit='m',
        valid='妥当値: 0.01–1.0',
    ),
    'editor.channel_role': FieldExplanation(
        '入力役割です。この測定データが表すスピーカーの役割を'
        '英語トークンで入力します。',
        valid='妥当値: front_left / center / front_right / subwoofer / unknown',
    ),
    # --- Calibration page ------------------------------------------------
    'calibration.table.step': FieldExplanation(
        'オンボーディングの工程です。',
    ),
    'calibration.table.status': FieldExplanation(
        '工程の状態です（完了 / 操作が必要 / 手動確認）。',
    ),
    'calibration.table.check': FieldExplanation(
        'この工程で確認する内容です。',
    ),
}


def explanation(key: str) -> FieldExplanation | None:
    """Return the explanation for ``key`` (None when unregistered)."""
    return FIELD_EXPLANATIONS.get(key)


def apply_explanation(widget: "QWidget", key: str) -> bool:
    """Install tooltip + WhatsThis text on ``widget`` for ``key``.

    Returns False when the key has no registered explanation so callers
    can keep the wiring declarative without per-field conditionals.
    """
    entry = FIELD_EXPLANATIONS.get(key)
    if entry is None:
        return False
    text = entry.text()
    widget.setToolTip(text)
    widget.setWhatsThis(text)
    return True


def explain_form_row(
    form_layout: "QFormLayout", widget: "QWidget", key: str
) -> bool:
    """Explain a QFormLayout row: the input AND its label get the text."""
    if not apply_explanation(widget, key):
        return False
    label = form_layout.labelForField(widget)
    if label is not None:
        text = FIELD_EXPLANATIONS[key].text()
        label.setToolTip(text)
        label.setWhatsThis(text)
    return True


def explain_table_columns(
    table: "QTableWidget",
    keys: Mapping[int, str],
) -> int:
    """Install header tooltips: column index → explanation key.

    Returns how many columns got an explanation (for tests).
    """
    applied = 0
    for column, key in keys.items():
        item = table.horizontalHeaderItem(column)
        entry = FIELD_EXPLANATIONS.get(key)
        if item is None or entry is None:
            continue
        text = entry.text()
        item.setToolTip(text)
        item.setWhatsThis(text)
        applied += 1
    return applied


def explain_combo_items(combo: "QWidget", *, domain: str | None = None) -> int:
    """Give each combo item a tooltip from STATUS_EXPLANATIONS.

    Item tooltips show inside the dropdown so a status value like
    「要再測定」 explains itself where the operator picks it. ``domain``
    selects a column-specific vocabulary for colliding codes.
    Returns how many items got tooltips.
    """
    from PySide6.QtCore import Qt

    applied = 0
    for index in range(combo.count()):
        text = status_explanation(str(combo.itemData(index)), domain=domain)
        if text is not None:
            combo.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)
            applied += 1
    return applied


# ---------------------------------------------------------------------------
# Status codes → "意味 · 次にやること" (cell tooltips at fill time)
# ---------------------------------------------------------------------------

STATUS_EXPLANATIONS: dict[str, str] = {
    # Batch import 状態
    'staged': '取り込み途中（ステージ）です。保存には割り当てが必要です。',
    'committed': '測定として保存されました。',
    'reused': '既存の測定を再利用しました（新規保存はしません）。',
    'failed': 'この項目は処理に失敗しました。内容を確認してやり直してください。',
    # 重複判定
    'exact_duplicate': '既存測定と内容が同一です。',
    'same_acquisition': '同じ取得条件の測定が既にあります。',
    'new': '重複はありません。新しい測定です。',
    # 解決
    'reuse_existing': '既存の測定を使います。',
    'import_as_new': '新しい測定として保存します。',
    # Campaign cell 状態
    'not_started': 'まだ測定を登録していません。',
    'assignment_incomplete': '測定はありますが割り当てが不完全です。割り当てページで確認してください。',
    'quality_pending': '測定済みです。品質ページで確認してください。',
    'retake_required': '品質証拠が不足しています。再測定してください。',
    'completed': 'このセルは完了です。',
    'skipped': 'このセルはスキップしました。',
    # 配置一致
    'current': '現在のシーンと同じ配置です。',
    'stale': '測定時の配置と現在の配置が異なります。古い測定の可能性があります。',
    'missing': '配置情報がありません。',
    # ライフサイクル（disposition）
    'active': '通常利用できる状態です。',
    'excluded_from_normal_use': '通常利用から除外しています（データは保持されます）。',
    'misassigned': '誤った割り当てとして記録されています。訂正してください。',
    'test_only': 'テスト目的の測定として記録されています。',
    'duplicate_import': '重複して取り込まれた測定として記録されています。',
    'corrected': '訂正済みです。履歴に残ります。',
    # 再測定推奨
    'RETAKE': '再測定を推奨します。品質証拠が不足しています。',
    'NOT_NEEDED': '再測定は不要です。',
    'UNVERIFIED': '再測定の要否は未評価です。',
    # オンボーディング工程
    'ready': 'この工程は完了しています。',
    'action': 'この工程では操作が必要です。',
    'manual': '外部ツールでの手動確認が必要です。',
    'synthetic_fixture': 'テストデータによる状態です。',
    # チェック状態（割り当て確認カード等）
    'verified': '有効と確認されています。',
    'not_applicable': 'この条件では評価しません。',
    # レベル互換性
    'absolute_level_comparable': '絶対レベルで比較できます。',
    'normalized_shape_comparable': '形状のみ比較できます（レベルは参照帯域で正規化されます）。',
    'diagnostic_only': '診断用途のみです。レベル差は絶対的な音圧差ではありません。',
    # 空間差分
    'moved': '測定時から位置が移動しました。',
    'removed': 'この実体は現在のシーンから削除されています。',
    'added': 'この実体は現在のシーンで追加されました。',
    'changed': '測定時から内容が変わりました。',
    # 品質チェック名（_check_label の語彙）
    'clipping': 'クリッピング（波形の歪み）の有無を評価します。',
    'noise_snr': '信号対雑音比（SNR）を評価します。',
    'usable_frequency_band': '使用可能な周波数帯域を確認します。',
    'timing_reference': '共通のタイミング基準があるか評価します。',
    'polarity': '極性の正しさを評価します。',
    'ir_window': 'インパルス応答の窓（時間範囲）を確認します。',
    'repeatability': '繰り返し測定の再現性を評価します。',
}


# Domain-scoped vocabularies: several surfaces emit the same raw code
# with different meaning ('unknown' alone is emitted by the evidence
# column, the quality column AND the phase column; 'UNKNOWN' by both the
# capability decision and the quality-decision vocabulary; 'calibration'
# by attachment kinds and campaign purposes). Callers pass the domain of
# the cell/combo they are filling so colliding codes resolve to the text
# meant for that column.
_STATUS_DOMAINS: dict[str, dict[str, str]] = {
    # 証拠種別 (evidence_type)
    'evidence': {
        'measured': '実測データです（実際に収録された応答）。',
        'derived': '他の測定から派生したデータです。',
        'predicted': '予測で生成したデータです。実測ではありません。',
        'unverified': '検証されていないデータです。',
        'unknown': '証拠の種別は未確認です。',
    },
    # 品質列 (record.quality_status: usable/warning/invalid/unknown)
    'quality': {
        'usable': '品質上は利用可能と評価されています。',
        'warning': '注意点があります。品質レポートを確認してください。',
        'invalid': '品質上の問題が報告されています。再測定を検討してください。',
        'unknown': '品質はまだ評価されていません。品質確認を実行してください。',
        'synthetic_fixture': 'テストデータによる状態です。',
    },
    # 位相列 (phase_status: valid/absent/unknown)
    'phase': {
        'valid': '有効と確認された位相データがあります。',
        'absent': '位相データは含まれていません。',
        'unknown': '位相の有効性は未確認です。',
    },
    # 能力判定 (ALLOWED/BLOCKED/UNKNOWN — 共通タイミングなど)
    'capability': {
        'ALLOWED': '条件を満たしています。この用途に使用できます。',
        'BLOCKED': '証拠が不足または矛盾しています。この用途には使えません。',
        'UNKNOWN': '条件を判定する証拠が不足しています。利用可否は未確認です。',
    },
    # 品質評価の判定語彙 (QualityDecision: PASS/FAIL/UNKNOWN/NOT_EVALUATED)
    'quality_decision': {
        'PASS': '品質チェックをすべて通過しています。',
        'FAIL': '品質チェックに不合格があります。レポートを確認し、再測定を検討してください。',
        'UNKNOWN': '品質レポートがないか、まだ評価されていません。',
        'NOT_EVALUATED': 'まだ評価されていません。',
    },
    # 添付種別 (MEASUREMENT_ATTACHMENT_KINDS)
    'attach': {
        'mdat': 'REW の .mdat 測定ファイルです。',
        'calibration': '校正用途のファイル添付です。',
        'notes': '設定ノート・メモの添付です。',
        'other': 'その他の添付ファイルです。',
    },
    # キャンペーン目的 (_VARIANT_PURPOSE_LABELS の語彙)
    'purpose': {
        'measurement': '通常の収録測定です。',
        'calibration': '校正目的で取得する測定です。',
        'holdout': '検証用に取っておく測定です（他の用途には使いません）。',
        'diagnostic': '問題切り分け用の診断測定です。',
        'validation': '検証のための測定です。',
    },
}


def status_explanation(code: str | None, *, domain: str | None = None) -> str | None:
    """Meaning + next-step text for a raw status code (None = unknown).

    ``domain`` selects a column-specific vocabulary when the same raw
    code carries different meanings across surfaces (e.g. 'unknown').
    """
    if code is None:
        return None
    if domain is not None:
        text = _STATUS_DOMAINS.get(domain, {}).get(code)
        if text is not None:
            return text
    return STATUS_EXPLANATIONS.get(code)


# Comparison-metric row names → what the metric means.
METRIC_EXPLANATIONS: dict[str, str] = {
    'RMS差': '帯域内での偏差の二乗平均平方根です。値が大きいほど差が大きい（単位: dB）。',
    '形状RMS': 'レベル差を除いた形状だけの差です（単位: dB）。',
    '平均差': '帯域内の平均差です（単位: dB）。',
    'レベル差': '参照帯域で揃えたときの平均レベル差です（単位: dB）。',
    '有効点': '指標計算に使えた周波数グリッド点数 / 全グリッド点数です。',
    '実帯域': '実際に比較できた周波数帯域です（単位: Hz）。',
}


def metric_explanation(name: str) -> str | None:
    """Meaning text for a comparison-metric row name (None = unknown)."""
    return METRIC_EXPLANATIONS.get(name)


__all__ = [
    'FIELD_EXPLANATIONS',
    'FieldExplanation',
    'METRIC_EXPLANATIONS',
    'STATUS_EXPLANATIONS',
    'apply_explanation',
    'explain_combo_items',
    'explain_form_row',
    'explain_table_columns',
    'explanation',
    'metric_explanation',
    'status_explanation',
]
