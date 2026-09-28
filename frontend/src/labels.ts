// Enum-token → Japanese display labels, mirroring the native app's maps in
// backend/src/htdt/measurement_page_workspace.py so both surfaces name the
// same stored value the same way. Unknown tokens fall back to the raw value.

function fromMap(map: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return map[value] ?? value
}

export function channelRoleLabel(value: string | null | undefined): string {
  return fromMap({
    front_left: 'フロント左 (FL)',
    center: 'センター (C)',
    front_right: 'フロント右 (FR)',
    subwoofer: 'サブウーファー',
    unknown: '未指定',
  }, value)
}

export function evidenceTypeLabel(value: string | null | undefined): string {
  return fromMap({
    measured: '実測',
    derived: '派生',
    predicted: '予測',
    unknown: '未確認',
  }, value)
}

export function qualityStatusLabel(value: string | null | undefined): string {
  return fromMap({
    usable: '使用可能',
    warning: '警告あり',
    invalid: '無効',
    unknown: '未確認',
  }, value)
}

export function routingEvidenceLabel(value: string | null | undefined): string {
  return fromMap({
    unknown: '未確認',
    verified: '検証済み',
    manual: '手動指定',
    inferred: '推定',
  }, value)
}

export function phaseStatusLabel(value: string | null | undefined): string {
  return fromMap({
    valid: '位相データ有効',
    absent: '位相データなし',
    unknown: '位相データ未確認',
  }, value)
}

export function comparisonRoleLabel(value: string | null | undefined): string {
  if (value === null || value === undefined) return '従来形式'
  return fromMap({
    repeatability: '繰り返し精度',
    configuration_ab: '構成A/B比較',
  }, value)
}

export function attachmentKindLabel(value: string | null | undefined): string {
  return fromMap({
    mdat: '.mdat',
    microphone_calibration: 'マイク校正',
    avr_settings: 'AVR設定',
    measurement_note: '測定メモ',
    image: '画像',
    other: 'その他',
  }, value)
}
