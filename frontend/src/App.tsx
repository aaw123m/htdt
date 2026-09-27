import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api, fileToBase64, parseList } from './api'
import { FeatureCandidatePanel } from './FeatureCandidates'
import { PlacementConstraintPanel } from './PlacementConstraints'
import { SearchSpacePanel } from './SearchSpace'
import { FrequencyPlot, RoomPlot, type ComparisonResult, type ContextPayload, type Speaker } from './plots'

type Health = {
  status: string
  version: string
  platform_target: string
  schema_version: number
}

type Project = { id: string; name: string; created_at: string }
type ContextRecord = { id: string; revision_number: number; created_at: string; payload: ContextPayload }
type QualityStatus = 'usable' | 'warning' | 'invalid' | 'unknown'
type Measurement = {
  id: string
  dataset_id: string
  context_id: string
  channel_role: string
  evidence_type: string
  routing_evidence: string
  quality_status: QualityStatus
  quality_reasons: string[]
  quality_source: string
  repeat_group: string | null
  points: number
  frequency_min_hz: number
  frequency_max_hz: number
  metadata: { phase_status: string; warnings: string[] }
}
type Preview = {
  filename: string
  sha256: string
  points: number
  frequency_min_hz: number
  frequency_max_hz: number
  phase_status: string
  warnings: string[]
}
type ContextDifference = { path: string; a: unknown; b: unknown }
type MeasurementSnapshot = {
  measurement_id: string
  context_id: string
  channel_role: string
  evidence_type: string
  quality_status: QualityStatus
  quality_reasons: string[]
  quality_source: string
  repeat_group: string | null
}
type ExtendedComparisonResult = ComparisonResult & {
  comparison_role?: string
  measurement_a?: MeasurementSnapshot
  measurement_b?: MeasurementSnapshot
  context_differences?: ContextDifference[]
  intended_changes?: ContextDifference[]
  confounders?: ContextDifference[]
  interpretation_warnings?: string[]
}
type Comparison = {
  id: string
  result: ExtendedComparisonResult
  spec: { label?: string | null; expected_change_paths?: string[] }
}
type Attachment = {
  id: string
  asset_sha256: string
  measurement_id: string | null
  context_id: string | null
  kind: string
  label: string | null
  filename: string
  size_bytes: number
}
type RoomMode = {
  n_x: number
  n_y: number
  n_z: number
  frequency_hz: number
  mode_class: string
}
type ReflectionCandidate = {
  speaker_id: string
  speaker_role: string
  surface: string
  reflection_point_m: [number, number, number]
  direct_length_m: number
  reflected_length_m: number
  excess_length_m: number
  excess_delay_ms: number
  first_destructive_hz: number | null
}
type AcousticAnalysis = {
  classification: 'predicted_geometry_candidate'
  algorithm_version: string
  assumptions: string[]
  sound_speed_m_s: number
  max_mode_hz: number
  room_modes: RoomMode[]
  first_order_reflections: ReflectionCandidate[]
  skipped_speaker_ids: string[]
}
type SpeakerDraft = {
  speaker_id: string
  role: string
  model: string
  x: string
  y: string
  z: string
}
type ExcludedBand = { low_hz: number; high_hz: number }
type MicOrientation = 'ceiling' | 'toward_speakers' | 'unknown'
type RoomGeometryKind = 'rectangular' | 'reference_box' | 'polygon_prism'
type ReadinessCheck = { key: string; passed: boolean; detail: string }
type MeasurementReadiness = {
  classification: string
  machine_ready: boolean
  status: 'blocked' | 'manual_confirmation_required'
  context_id: string
  checks: ReadinessCheck[]
  failed_check_keys: string[]
  manual_confirmation_required: string[]
  notice: string
}

const workflowNav = [
  { href: '#project', icon: 'project', label: 'Project' },
  { href: '#room', icon: 'room', label: 'Room' },
  { href: '#constraints', icon: 'constraints', label: 'Constraints' },
  { href: '#search', icon: 'search', label: 'Search' },
  { href: '#measure', icon: 'measure', label: 'Measure' },
  { href: '#compare', icon: 'compare', label: 'Compare' },
  { href: '#model', icon: 'model', label: 'Model' },
  { href: '#features', icon: 'features', label: 'Features' },
] as const

type WorkflowIconName = typeof workflowNav[number]['icon']

const initialSpeakers: SpeakerDraft[] = [
  { speaker_id: 'FL', role: 'front_left', model: '', x: '', y: '', z: '' },
  { speaker_id: 'C', role: 'front_center', model: '', x: '', y: '', z: '' },
  { speaker_id: 'FR', role: 'front_right', model: '', x: '', y: '', z: '' },
  { speaker_id: 'HL', role: 'height_front_left', model: '', x: '', y: '', z: '' },
  { speaker_id: 'HR', role: 'height_front_right', model: '', x: '', y: '', z: '' },
]

function WorkflowIcon({ name }: { name: WorkflowIconName }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  return <svg viewBox="0 0 24 24" aria-hidden="true">
    {name === 'project' && <><rect {...common} x="4" y="5" width="16" height="14" rx="3"/><path {...common} d="M8 9h8M8 13h5"/></>}
    {name === 'room' && <><path {...common} d="M4 11 12 5l8 6v8H4z"/><path {...common} d="M9 19v-5h6v5"/></>}
    {name === 'constraints' && <><circle {...common} cx="7" cy="7" r="2"/><circle {...common} cx="17" cy="17" r="2"/><path {...common} d="M9 7h8M17 9v6M7 9v8h8"/></>}
    {name === 'search' && <><circle {...common} cx="11" cy="11" r="6"/><path {...common} d="m16 16 4 4M8 11h6M11 8v6"/></>}
    {name === 'measure' && <><path {...common} d="M5 17V7M9 17V4M13 17v-7M17 17V6"/><path {...common} d="M4 19h16"/></>}
    {name === 'compare' && <><path {...common} d="M5 8h11M13 5l3 3-3 3M19 16H8M11 13l-3 3 3 3"/></>}
    {name === 'model' && <><path {...common} d="m12 4 7 4-7 4-7-4zM5 12l7 4 7-4M5 16l7 4 7-4"/></>}
    {name === 'features' && <><path {...common} d="M4 15h3l2-7 3 10 2-6 2 3h4"/></>}
  </svg>
}

function numeric(value: string, label: string): number {
  const result = Number(value)
  if (!Number.isFinite(result)) throw new Error(`${label}を入力してください`)
  return result
}

function speakerPayload(draft: SpeakerDraft): Speaker {
  const coordinates = [draft.x, draft.y, draft.z]
  const hasAny = coordinates.some((value) => value.trim() !== '')
  const hasAll = coordinates.every((value) => value.trim() !== '')
  if (hasAny && !hasAll) throw new Error(`${draft.role}: 座標はX/Y/Zをすべて入力してください`)
  return {
    speaker_id: draft.speaker_id,
    role: draft.role,
    model: draft.model || null,
    position: hasAll
      ? { x_m: numeric(draft.x, 'X'), y_m: numeric(draft.y, 'Y'), z_m: numeric(draft.z, 'Z') }
      : null,
  }
}

function positive(value: string, label: string): number {
  const result = numeric(value, label)
  if (result <= 0) throw new Error(`${label}は0より大きい値を入力してください`)
  return result
}

function integer(value: string, label: string): number {
  const result = numeric(value, label)
  if (!Number.isInteger(result)) throw new Error(`${label}は整数で入力してください`)
  return result
}

function parseExcludedBands(text: string): ExcludedBand[] {
  if (!text.trim()) return []
  return text.split(/[,;]+/).map((chunk) => {
    const match = chunk.trim().match(/^([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)$/)
    if (!match) throw new Error(`除外帯域「${chunk.trim()}」は 70-90 の形式で入力してください`)
    const low_hz = Number(match[1])
    const high_hz = Number(match[2])
    if (low_hz <= 0 || high_hz <= low_hz) throw new Error(`除外帯域「${chunk.trim()}」が不正です`)
    return { low_hz, high_hz }
  })
}

function parseRoomVertices(text: string): { vertex_id: string; x_m: number; y_m: number }[] {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (lines.length < 3) throw new Error('多角形の部屋は3頂点以上を入力してください')
  const vertices = lines.map((line, index) => {
    const parts = line.split(',').map((part) => part.trim())
    if (parts.length !== 3 || !parts[0]) throw new Error(`頂点${index + 1}は vertex_id,x,y の形式で入力してください`)
    return { vertex_id: parts[0], x_m: numeric(parts[1], `頂点${index + 1} X`), y_m: numeric(parts[2], `頂点${index + 1} Y`) }
  })
  if (new Set(vertices.map((vertex) => vertex.vertex_id)).size !== vertices.length) throw new Error('vertex_idは重複できません')
  return vertices
}


function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [contexts, setContexts] = useState<ContextRecord[]>([])
  const [measurements, setMeasurements] = useState<Measurement[]>([])
  const [comparisons, setComparisons] = useState<Comparison[]>([])
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  // Mutating actions are single-flight: double-clicking save/import/compare
  // must not fire two writes (immutable records would still duplicate work).
  const [busy, setBusy] = useState(false)
  const [helpVisible, setHelpVisible] = useState(false)

  const [projectName, setProjectName] = useState('Home Theater')
  const [room, setRoom] = useState({ width: '', depth: '', height: '' })
  const [roomGeometryKind, setRoomGeometryKind] = useState<RoomGeometryKind>('rectangular')
  const [roomVerticesText, setRoomVerticesText] = useState('')
  const [mlp, setMlp] = useState({ x: '', y: '', z: '' })
  const [micOrientation, setMicOrientation] = useState<MicOrientation>('ceiling')
  const [micSerial, setMicSerial] = useState('')
  const [micSampleRate, setMicSampleRate] = useState('48000')
  const [micCalibrationProfile, setMicCalibrationProfile] = useState<'0deg' | '90deg' | 'unknown'>('90deg')
  const [micCalibrationFilename, setMicCalibrationFilename] = useState('')
  const [speakers, setSpeakers] = useState<SpeakerDraft[]>(initialSpeakers)

  const [selectedContextId, setSelectedContextId] = useState('')
  const [measurementFile, setMeasurementFile] = useState<File | null>(null)
  const [measurementBase64, setMeasurementBase64] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [channelRole, setChannelRole] = useState('front_left')
  const [sourceSpeakerIds, setSourceSpeakerIds] = useState('FL')
  const [qualityStatus, setQualityStatus] = useState<QualityStatus>('unknown')
  const [qualityReasons, setQualityReasons] = useState('')
  const [repeatGroup, setRepeatGroup] = useState('')
  const [routingEvidence, setRoutingEvidence] = useState('unknown')

  const [attachmentFile, setAttachmentFile] = useState<File | null>(null)
  const [attachmentKind, setAttachmentKind] = useState('mdat')
  const [attachmentLabel, setAttachmentLabel] = useState('')
  const [attachmentMeasurementId, setAttachmentMeasurementId] = useState('')

  const [datasetA, setDatasetA] = useState('')
  const [datasetB, setDatasetB] = useState('')
  const [band, setBand] = useState({ low: '60', high: '200', refLow: '60', refHigh: '200' })
  const [excludedBandsText, setExcludedBandsText] = useState('')
  const [expectedChangesText, setExpectedChangesText] = useState('speakers.FL.position')
  const [activeComparison, setActiveComparison] = useState<Comparison | null>(null)

  const [maxModeHz, setMaxModeHz] = useState('300')
  const [soundSpeed, setSoundSpeed] = useState('343')
  const [acoustics, setAcoustics] = useState<AcousticAnalysis | null>(null)
  const [readiness, setReadiness] = useState<MeasurementReadiness | null>(null)
  const [readinessLoading, setReadinessLoading] = useState(false)

  // Monotonic guards: async responses for an old project/context/file must not
  // overwrite state for the selection that replaced them.
  const projectLoadSeq = useRef(0)
  const contextChangeSeq = useRef(0)
  const measurementFileSeq = useRef(0)

  const activeContext = useMemo(
    () => contexts.find((context) => context.id === selectedContextId) ?? contexts[0] ?? null,
    [contexts, selectedContextId],
  )

  async function reloadProjects() {
    const items = await api<Project[]>('/api/projects')
    setProjects(items)
    if (!projectId && items[0]) setProjectId(items[0].id)
  }

  async function reloadProjectData(id: string) {
    const seq = ++projectLoadSeq.current
    if (!id) return
    const [contextRows, measurementRows, comparisonRows, attachmentRows] = await Promise.all([
      api<ContextRecord[]>(`/api/projects/${id}/contexts`),
      api<Measurement[]>(`/api/projects/${id}/measurements`),
      api<Comparison[]>(`/api/projects/${id}/comparisons`),
      api<Attachment[]>(`/api/projects/${id}/attachments`),
    ])
    if (seq !== projectLoadSeq.current) return
    setContexts(contextRows)
    setMeasurements(measurementRows)
    setComparisons(comparisonRows)
    setAttachments(attachmentRows)
    if (contextRows[0]) setSelectedContextId((current) => current || contextRows[0].id)
    if (measurementRows[0]) setDatasetA((current) => current || measurementRows[0].dataset_id)
    if (measurementRows[1]) setDatasetB((current) => current || measurementRows[1].dataset_id)
  }

  useEffect(() => {
    void Promise.all([api<Health>('/api/health').then(setHealth), reloadProjects()]).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : '初期化に失敗しました')
    })
  }, [])

  useEffect(() => {
    setSelectedContextId('')
    setDatasetA('')
    setDatasetB('')
    setActiveComparison(null)
    setAcoustics(null)
    void reloadProjectData(projectId).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '読込失敗'))
  }, [projectId])

  useEffect(() => {
    contextChangeSeq.current += 1
    setAcoustics(null)
    setReadiness(null)
    // A superseded checkMeasurementReadiness() skips its own loading-flag
    // clear, so the invalidation that dropped it must release the flag.
    setReadinessLoading(false)
  }, [selectedContextId])

  useEffect(() => {
    document.body.classList.toggle('help-visible', helpVisible)
    return () => document.body.classList.remove('help-visible')
  }, [helpVisible])

  function notify(text: string) {
    setMessage(text)
    setError('')
  }

  async function createProject(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const project = await api<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name: projectName }) })
      await reloadProjects()
      setProjectId(project.id)
      notify('プロジェクトを作成しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '作成失敗')
    } finally {
      setBusy(false)
    }
  }

  function copyActiveContextToEditor() {
    if (!activeContext) return
    const payload = activeContext.payload
    setRoom({ width: String(payload.room.width_m), depth: String(payload.room.depth_m), height: String(payload.room.height_m) })
    setRoomGeometryKind(payload.room.geometry_kind ?? 'rectangular')
    setRoomVerticesText(payload.room.footprint_vertices?.map((vertex) => `${vertex.vertex_id},${vertex.x_m},${vertex.y_m}`).join('\n') ?? '')
    setMlp({
      x: String(payload.measurement_point.position.x_m),
      y: String(payload.measurement_point.position.y_m),
      z: String(payload.measurement_point.position.z_m),
    })
    const aim = payload.measurement_point.aim_xyz
    setMicOrientation(aim?.[2] === 1 ? 'ceiling' : aim?.[1] === -1 ? 'toward_speakers' : 'unknown')
    setMicSerial(payload.microphone?.serial ?? '')
    setMicSampleRate(payload.microphone?.sample_rate_hz ? String(payload.microphone.sample_rate_hz) : '48000')
    setMicCalibrationProfile(payload.microphone?.calibration_profile ?? '90deg')
    setMicCalibrationFilename(payload.microphone?.calibration_filename ?? '')
    setSpeakers(payload.speakers.map((speaker) => ({
      speaker_id: speaker.speaker_id,
      role: speaker.role,
      model: speaker.model ?? '',
      x: speaker.position ? String(speaker.position.x_m) : '',
      y: speaker.position ? String(speaker.position.y_m) : '',
      z: speaker.position ? String(speaker.position.z_m) : '',
    })))
    notify(`R${activeContext.revision_number} を編集フォームへ複製しました。保存するまで元版は変わりません`)
  }

  async function saveContext(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      if (!projectId) throw new Error('先にプロジェクトを作成してください')
      const payload = {
        room: {
          width_m: positive(room.width, '部屋幅'), depth_m: positive(room.depth, '部屋奥行'), height_m: positive(room.height, '部屋高さ'),
          geometry_kind: roomGeometryKind,
          ...(roomGeometryKind === 'polygon_prism' ? { footprint_vertices: parseRoomVertices(roomVerticesText) } : {}),
        },
        speakers: speakers.map(speakerPayload),
        measurement_point: {
          point_id: 'MLP',
          label: 'MLP',
          position: { x_m: numeric(mlp.x, 'MLP X'), y_m: numeric(mlp.y, 'MLP Y'), z_m: numeric(mlp.z, 'MLP Z') },
          aim_xyz: micOrientation === 'ceiling' ? [0, 0, 1] : micOrientation === 'toward_speakers' ? [0, -1, 0] : null,
        },
        microphone: {
          manufacturer: 'miniDSP',
          model: 'UMIK-1',
          serial: micSerial.trim() || null,
          connection: 'usb',
          sample_rate_hz: integer(micSampleRate, 'マイクサンプルレート'),
          calibration_profile: micCalibrationProfile,
          calibration_filename: micCalibrationFilename.trim() || null,
        },
        avr: { manufacturer: 'Yamaha', model: 'RX-A4A' },
        parent_context_id: activeContext?.id ?? null,
      }
      const saved = await api<ContextRecord>(`/api/projects/${projectId}/contexts`, { method: 'POST', body: JSON.stringify(payload) })
      await reloadProjectData(projectId)
      setSelectedContextId(saved.id)
      notify(`配置・条件 R${saved.revision_number} を保存しました`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失敗')
    } finally {
      setBusy(false)
    }
  }

  async function chooseMeasurementFile(file: File | null) {
    const seq = ++measurementFileSeq.current
    setMeasurementFile(file)
    setPreview(null)
    setMeasurementBase64('')
    if (!file) return
    try {
      const raw_base64 = await fileToBase64(file)
      if (seq !== measurementFileSeq.current) return
      setMeasurementBase64(raw_base64)
      const result = await api<Preview>('/api/import/preview', { method: 'POST', body: JSON.stringify({ filename: file.name, raw_base64 }) })
      if (seq !== measurementFileSeq.current) return
      setPreview(result)
      notify('取込プレビューを検証しました')
    } catch (reason) {
      if (seq !== measurementFileSeq.current) return
      setError(reason instanceof Error ? reason.message : 'プレビュー失敗')
    }
  }

  async function importMeasurement() {
    if (busy) return
    setBusy(true)
    try {
      if (!projectId || !selectedContextId || !measurementFile || !measurementBase64) throw new Error('プロジェクト、Context、測定ファイルを選択してください')
      const imported = await api<{ duplicate_asset: boolean; existing_dataset_count: number }>(`/api/projects/${projectId}/measurements`, {
        method: 'POST',
        body: JSON.stringify({
          filename: measurementFile.name,
          raw_base64: measurementBase64,
          context_id: selectedContextId,
          channel_role: channelRole,
          evidence_type: 'measured',
          source_speaker_ids: parseList(sourceSpeakerIds),
          radiation_scope: 'unknown',
          routing_evidence: routingEvidence,
          quality_status: qualityStatus,
          quality_reasons: parseList(qualityReasons),
          quality_source: qualityStatus === 'unknown' ? 'unknown' : 'manual',
          repeat_group: repeatGroup.trim() || null,
        }),
      })
      await reloadProjectData(projectId)
      setMeasurementFile(null)
      setMeasurementBase64('')
      setPreview(null)
      notify(imported.duplicate_asset
        ? `測定を保存しました。同じRawAssetを使う既存Datasetが${imported.existing_dataset_count}件ありますが、自動統合していません`
        : '測定を原本付きで保存しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取込失敗')
    } finally {
      setBusy(false)
    }
  }

  async function uploadAttachment() {
    if (busy) return
    setBusy(true)
    try {
      if (!projectId || !attachmentFile) throw new Error('プロジェクトと添付ファイルを選択してください')
      const raw_base64 = await fileToBase64(attachmentFile)
      await api(`/api/projects/${projectId}/attachments`, {
        method: 'POST',
        body: JSON.stringify({
          filename: attachmentFile.name,
          raw_base64,
          kind: attachmentKind,
          label: attachmentLabel.trim() || null,
          measurement_id: attachmentMeasurementId || null,
          context_id: selectedContextId || null,
        }),
      })
      await reloadProjectData(projectId)
      setAttachmentFile(null)
      setAttachmentLabel('')
      notify('再解析・復元用の添付原本を保存しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '添付保存失敗')
    } finally {
      setBusy(false)
    }
  }

  async function compare() {
    if (busy) return
    setBusy(true)
    try {
      if (!projectId || !datasetA || !datasetB) throw new Error('A/Bの測定を選択してください')
      const lowHz = positive(band.low, '帯域下限')
      const highHz = positive(band.high, '帯域上限')
      if (highHz <= lowHz) throw new Error('帯域上限は下限より大きい値を入力してください')
      const refLowHz = positive(band.refLow, '基準帯域下限')
      const refHighHz = positive(band.refHigh, '基準帯域上限')
      if (refHighHz <= refLowHz) throw new Error('基準帯域上限は下限より大きい値を入力してください')
      const comparison = await api<Comparison>(`/api/projects/${projectId}/comparisons`, {
        method: 'POST',
        body: JSON.stringify({
          dataset_a_id: datasetA,
          dataset_b_id: datasetB,
          low_hz: lowHz,
          high_hz: highHz,
          reference_low_hz: refLowHz,
          reference_high_hz: refHighHz,
          excluded_bands: parseExcludedBands(excludedBandsText),
          expected_change_paths: parseList(expectedChangesText),
          label: 'Speaker setting A/B',
        }),
      })
      setActiveComparison(comparison)
      await reloadProjectData(projectId)
      notify('比較条件、品質、条件差と結果を保存しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '比較失敗')
    } finally {
      setBusy(false)
    }
  }

  async function checkMeasurementReadiness() {
    if (!projectId || !activeContext) return
    const seq = contextChangeSeq.current
    setReadinessLoading(true)
    try {
      const result = await api<MeasurementReadiness>(`/api/projects/${projectId}/contexts/${activeContext.id}/measurement-readiness`)
      if (seq !== contextChangeSeq.current) return
      setReadiness(result)
      notify(result.machine_ready
        ? '自動preflightは通過しました。物理マイク向きとRX-A4A routingは手動確認が必要です'
        : '測定preflightに未充足項目があります')
    } catch (reason) {
      if (seq !== contextChangeSeq.current) return
      setReadiness(null)
      setError(reason instanceof Error ? reason.message : '測定preflight失敗')
    } finally {
      if (seq === contextChangeSeq.current) setReadinessLoading(false)
    }
  }

  async function runAcoustics() {
    const seq = contextChangeSeq.current
    if (busy) return
    setBusy(true)
    try {
      if (!projectId || !activeContext) throw new Error('Contextを選択してください')
      if ((activeContext.payload.room.geometry_kind ?? 'rectangular') !== 'rectangular') throw new Error('現在のA01幾何解析は矩形室専用です。polygon室では実行しません')
      const maxHz = positive(maxModeHz, 'モード上限')
      if (maxHz > 2000) throw new Error('モード上限は2000 Hz以下で入力してください')
      const speed = numeric(soundSpeed, '音速')
      if (!(speed > 250 && speed < 400)) throw new Error('音速は250〜400 m/sの範囲で入力してください')
      const result = await api<AcousticAnalysis>(`/api/projects/${projectId}/contexts/${activeContext.id}/acoustics?max_hz=${encodeURIComponent(maxHz)}&sound_speed_m_s=${encodeURIComponent(speed)}`)
      if (seq !== contextChangeSeq.current) return
      setAcoustics(result)
      notify('幾何モデル候補を計算しました。実測診断とは別表示です')
    } catch (reason) {
      if (seq !== contextChangeSeq.current) return
      setError(reason instanceof Error ? reason.message : '解析失敗')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <p className="eyebrow">v0.2 foundation · measured evidence first</p>
          <h1>Home Theater Digital Twin</h1>
          <p className="lead">測定品質・配置履歴・原本を固定し、再現可能なA/B比較でセッティングを改善します。幾何モデルは候補生成に限定します。</p>
        </div>
        <div className="runtime">
          <strong>{health?.status ?? 'checking'}</strong>
          <span>{health?.platform_target}</span>
          <span>schema {health?.schema_version ?? '—'}</span>
        </div>
      </header>

      <nav className="workflow-nav" aria-label="Workflow">
        <div className="workflow-links">{workflowNav.map((item) => <a key={item.href} href={item.href}><WorkflowIcon name={item.icon} />{item.label}</a>)}</div>
        <button type="button" className={helpVisible ? "help-toggle active" : "help-toggle"} aria-pressed={helpVisible} onClick={() => setHelpVisible(!helpVisible)}>?</button>
      </nav>

      {(message || error) && <div className={error ? 'notice error' : 'notice'}>{error || message}</div>}

      <section className="panel" id="project">
        <div className="section-title"><h2>Project</h2><span>ローカル保存</span></div>
        <form className="row" onSubmit={createProject}>
          <input value={projectName} onChange={(event) => setProjectName(event.target.value)} aria-label="Project name" />
          <button type="submit" disabled={busy}>新規作成</button>
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)} aria-label="Project">
            <option value="">プロジェクトを選択</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </form>
      </section>

      <section className="panel" id="room">
        <div className="section-title"><h2>Room & Layout</h2><span>{contexts.length ? `${contexts.length} revisions` : '未登録'}</span></div>
        <form onSubmit={saveContext}>
          <div className="grid3">
            <label>幅 X (m)<input value={room.width} onChange={(event) => setRoom({ ...room, width: event.target.value })} /></label>
            <label>奥行 Y (m)<input value={room.depth} onChange={(event) => setRoom({ ...room, depth: event.target.value })} /></label>
            <label>高さ Z (m)<input value={room.height} onChange={(event) => setRoom({ ...room, height: event.target.value })} /></label>
          </div>
          <div className="grid2">
            <label>Room geometry<select value={roomGeometryKind} onChange={(event) => setRoomGeometryKind(event.target.value as RoomGeometryKind)}><option value="rectangular">矩形（exact）</option><option value="polygon_prism">多角形prism（exact footprint）</option><option value="reference_box">reference boxのみ（形状unknown）</option></select></label>
            {roomGeometryKind === 'polygon_prism' && <label>Footprint vertices (vertex_id,x,y)<textarea rows={8} value={roomVerticesText} onChange={(event) => setRoomVerticesText(event.target.value)} placeholder={'v0,0,0\nv1,4,0\nv2,4,5\n…'} /></label>}
          </div>
          {roomGeometryKind === 'polygon_prism' && <p className="hint">頂点はX=右/Y=後方の順序付き境界です。最初の頂点を末尾へ重複入力しません。8角形なら8行入力します。</p>}
          <h3>MLP</h3>
          <div className="grid3">
            <label>X<input value={mlp.x} onChange={(event) => setMlp({ ...mlp, x: event.target.value })} /></label>
            <label>Y<input value={mlp.y} onChange={(event) => setMlp({ ...mlp, y: event.target.value })} /></label>
            <label>Z<input value={mlp.z} onChange={(event) => setMlp({ ...mlp, z: event.target.value })} /></label>
          </div>
          <h3>Measurement microphone — miniDSP UMIK-1</h3>
          <div className="grid3">
            <label>向き<select value={micOrientation} onChange={(event) => setMicOrientation(event.target.value as MicOrientation)}><option value="ceiling">天井向き (90°)</option><option value="toward_speakers">スピーカー向き (0°)</option><option value="unknown">unknown</option></select></label>
            <label>Sample rate (Hz)<input value={micSampleRate} onChange={(event) => setMicSampleRate(event.target.value)} /></label>
            <label>Calibration<select value={micCalibrationProfile} onChange={(event) => setMicCalibrationProfile(event.target.value as '0deg' | '90deg' | 'unknown')}><option value="90deg">90deg</option><option value="0deg">0deg</option><option value="unknown">unknown</option></select></label>
            <label>Serial（ローカル保存）<input value={micSerial} onChange={(event) => setMicSerial(event.target.value)} placeholder="実機到着後に入力" /></label>
            <label>Calibration filename<input value={micCalibrationFilename} onChange={(event) => setMicCalibrationFilename(event.target.value)} placeholder="例: 7001234_90deg.txt" /></label>
          </div>
          <p className="hint">ホームシアター基準は48 kHz・天井向き・個体別90°校正。校正ファイル原本はRaw attachmentsで保存します。</p>
          <h3>Speakers — 現在の3.0.2を初期行として表示。増減可能</h3>
          <div className="speaker-table">
            {speakers.map((speaker, index) => {
              const rowLabel = speaker.role.trim() || speaker.speaker_id.trim() || `speaker ${index + 1}`
              return <div className="speaker-row" key={`${speaker.speaker_id}-${index}`}>
                <input value={speaker.speaker_id} onChange={(event) => setSpeakers(speakers.map((item, i) => i === index ? { ...item, speaker_id: event.target.value } : item))} aria-label={`${rowLabel} speaker ID`} />
                <input value={speaker.role} onChange={(event) => setSpeakers(speakers.map((item, i) => i === index ? { ...item, role: event.target.value } : item))} aria-label={`${rowLabel} role`} />
                <input placeholder="model" value={speaker.model} onChange={(event) => setSpeakers(speakers.map((item, i) => i === index ? { ...item, model: event.target.value } : item))} aria-label={`${rowLabel} model`} />
                {(['x', 'y', 'z'] as const).map((axis) => <input key={axis} placeholder={axis.toUpperCase()} value={speaker[axis]} onChange={(event) => setSpeakers(speakers.map((item, i) => i === index ? { ...item, [axis]: event.target.value } : item))} aria-label={`${rowLabel} ${axis.toUpperCase()} position (m)`} />)}
                <button type="button" className="ghost" aria-label={`${rowLabel} を削除`} onClick={() => setSpeakers(speakers.filter((_, i) => i !== index))}>削除</button>
              </div>
            })}
          </div>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setSpeakers([...speakers, { speaker_id: `SP${speakers.length + 1}`, role: 'other', model: '', x: '', y: '', z: '' }])}>スピーカー追加</button>
            <button type="submit" disabled={busy}>新しい不変版として保存</button>
            <select value={selectedContextId} onChange={(event) => setSelectedContextId(event.target.value)}>
              {contexts.map((context) => <option key={context.id} value={context.id}>R{context.revision_number}</option>)}
            </select>
            <button type="button" className="ghost" disabled={!activeContext} title={!activeContext ? '保存済みのContextを選択してください' : undefined} onClick={copyActiveContextToEditor}>選択版を複製して編集</button>
          </div>
        </form>
        {activeContext && <>
          <RoomPlot context={activeContext.payload} />
          <div className="row action-row">
            <button type="button" className="ghost" disabled={readinessLoading} onClick={() => void checkMeasurementReadiness()}>
              {readinessLoading ? '照合中…' : 'REW測定条件を照合'}
            </button>
            <span className="hint">保存Contextと現在のREW/UMIK-1/HDMI状態をGET-onlyで照合します。</span>
          </div>
          {readiness && <div className="preview">
            <strong>Measurement readiness: {readiness.status}</strong>
            <span>machine checks: {readiness.machine_ready ? 'pass' : 'blocked'}</span>
            {readiness.checks.map((item) => <span key={item.key}>{item.passed ? 'PASS' : 'FAIL'} · {item.key} · {item.detail}</span>)}
            <em>{readiness.notice}</em>
            {readiness.manual_confirmation_required.map((item) => <em key={item}>Manual: {item}</em>)}
          </div>}
        </>}
      </section>

      <PlacementConstraintPanel projectId={projectId} context={activeContext} />
      <SearchSpacePanel projectId={projectId} context={activeContext} />

      <section className="panel" id="measure">
        <div className="section-title"><h2>Measurements</h2><span>原本 + quality snapshot</span></div>
        <div className="grid2">
          <label>測定ファイル<input type="file" accept=".txt,.csv,.dat,.frd" onChange={(event) => void chooseMeasurementFile(event.target.files?.[0] ?? null)} /></label>
          <label>Context<select value={selectedContextId} onChange={(event) => setSelectedContextId(event.target.value)}><option value="">選択</option>{contexts.map((context) => <option key={context.id} value={context.id}>R{context.revision_number}</option>)}</select></label>
          <label>入力role<input value={channelRole} onChange={(event) => setChannelRole(event.target.value)} /></label>
          <label>実際の音源ID（カンマ区切り）<input value={sourceSpeakerIds} onChange={(event) => setSourceSpeakerIds(event.target.value)} /></label>
          <label>品質<select value={qualityStatus} onChange={(event) => setQualityStatus(event.target.value as QualityStatus)}><option value="unknown">unknown</option><option value="usable">usable</option><option value="warning">warning</option><option value="invalid">invalid</option></select></label>
          <label>ルーティング根拠<select value={routingEvidence} onChange={(event) => setRoutingEvidence(event.target.value)}><option value="unknown">unknown</option><option value="manual">manual</option><option value="verified">verified</option><option value="inferred">inferred</option></select></label>
          <label>同条件再測定グループ<input value={repeatGroup} onChange={(event) => setRepeatGroup(event.target.value)} placeholder="例: FL-R1-baseline" /></label>
          <label>品質理由<input value={qualityReasons} onChange={(event) => setQualityReasons(event.target.value)} placeholder="例: level checked, no clip warning" /></label>
        </div>
        {preview && <div className="preview"><strong>{preview.filename}</strong><span>{preview.points} points</span><span>{preview.frequency_min_hz}–{preview.frequency_max_hz} Hz</span><span>phase: {preview.phase_status}</span><span>SHA {preview.sha256.slice(0, 12)}…</span>{preview.warnings.map((warning) => <em key={warning}>{warning}</em>)}</div>}
        <button disabled={busy || !preview} title={!preview ? '測定ファイルを選択してプレビューを確認してください' : undefined} onClick={() => void importMeasurement()}>{busy ? '保存中…' : 'この測定を保存'}</button>
        <p className="hint">品質は有限なFR値から自動推定しません。実測条件を確認できない間は unknown のまま保存してください。</p>
        <div className="cards">
          {measurements.length === 0 && <article><strong>測定はまだありません</strong><span>測定ファイルを選択してプレビュー後に保存するとここへ表示されます。</span></article>}
          {measurements.map((measurement) => <article key={measurement.id}>
            <strong>{measurement.channel_role}</strong>
            <span>{measurement.points} pts · {measurement.frequency_min_hz}–{measurement.frequency_max_hz} Hz</span>
            <span>quality: {measurement.quality_status} · evidence: {measurement.evidence_type}</span>
            <span>repeat: {measurement.repeat_group ?? '—'} · routing: {measurement.routing_evidence}</span>
            {measurement.quality_reasons.length > 0 && <small>{measurement.quality_reasons.join(' / ')}</small>}
            <code>{measurement.dataset_id.slice(0, 8)}</code>
          </article>)}
        </div>
      </section>

      <section className="panel" id="assets">
        <div className="section-title"><h2>Source Files</h2><span>.mdat / calibration / AVR settings</span></div>
        <p className="hint">周波数応答テキストとは別に、再解析や復元に必要な原本をRawAssetとして保存します。自動解析はしません。</p>
        <div className="grid2">
          <label>添付ファイル<input type="file" onChange={(event) => setAttachmentFile(event.target.files?.[0] ?? null)} /></label>
          <label>種別<select value={attachmentKind} onChange={(event) => setAttachmentKind(event.target.value)}><option value="mdat">mdat</option><option value="microphone_calibration">microphone_calibration</option><option value="avr_settings">avr_settings</option><option value="measurement_note">measurement_note</option><option value="image">image</option><option value="other">other</option></select></label>
          <label>ラベル<input value={attachmentLabel} onChange={(event) => setAttachmentLabel(event.target.value)} placeholder="任意" /></label>
          <label>関連測定<select value={attachmentMeasurementId} onChange={(event) => setAttachmentMeasurementId(event.target.value)}><option value="">Project/Contextのみ</option>{measurements.map((measurement) => <option key={measurement.id} value={measurement.id}>{measurement.channel_role} · {measurement.dataset_id.slice(0, 8)}</option>)}</select></label>
        </div>
        <button disabled={busy || !attachmentFile || !projectId} title={!projectId ? 'プロジェクトを選択してください' : !attachmentFile ? '添付ファイルを選択してください' : undefined} onClick={() => void uploadAttachment()}>添付原本を保存</button>
        <div className="cards">
          {attachments.length === 0 && <article><strong>添付原本はまだありません</strong><span>.mdat・校正ファイル・AVR設定などの原本を保存するとここへ表示されます。</span></article>}
          {attachments.map((attachment) => <article key={attachment.id}><strong>{attachment.kind}</strong><span>{attachment.filename}</span><span>{attachment.label ?? '—'} · {attachment.size_bytes} bytes</span><code>{attachment.asset_sha256.slice(0, 12)}</code></article>)}
        </div>
      </section>

      <section className="panel" id="compare">
        <div className="section-title"><h2>Compare</h2><span>96 PPO / log₂ interpolation / A−B</span></div>
        <div className="grid2">
          <label>A<select value={datasetA} onChange={(event) => setDatasetA(event.target.value)}><option value="">選択</option>{measurements.map((measurement) => <option key={measurement.dataset_id} value={measurement.dataset_id}>{measurement.channel_role} · {measurement.quality_status} · {measurement.dataset_id.slice(0, 8)}</option>)}</select></label>
          <label>B<select value={datasetB} onChange={(event) => setDatasetB(event.target.value)}><option value="">選択</option>{measurements.map((measurement) => <option key={measurement.dataset_id} value={measurement.dataset_id}>{measurement.channel_role} · {measurement.quality_status} · {measurement.dataset_id.slice(0, 8)}</option>)}</select></label>
        </div>
        <div className="grid4">
          <label>評価Low Hz<input value={band.low} onChange={(event) => setBand({ ...band, low: event.target.value })} /></label>
          <label>評価High Hz<input value={band.high} onChange={(event) => setBand({ ...band, high: event.target.value })} /></label>
          <label>基準Low Hz<input value={band.refLow} onChange={(event) => setBand({ ...band, refLow: event.target.value })} /></label>
          <label>基準High Hz<input value={band.refHigh} onChange={(event) => setBand({ ...band, refHigh: event.target.value })} /></label>
        </div>
        <label>除外帯域（例: 70-90, 120-130）<input value={excludedBandsText} onChange={(event) => setExcludedBandsText(event.target.value)} placeholder="任意" /></label>
        <label>意図した変更パス（例: speakers.FL.position）<input value={expectedChangesText} onChange={(event) => setExpectedChangesText(event.target.value)} /></label>
        <div className="row action-row"><button disabled={busy} onClick={() => void compare()}>{busy ? '処理中…' : '比較して保存'}</button></div>
        {activeComparison && <>
          <div className="metrics">
            <div><span>Mean A−B</span><strong>{activeComparison.result.mean_difference_db?.toFixed(2) ?? '—'} dB</strong></div>
            <div><span>RMS diff</span><strong>{activeComparison.result.rms_difference_db?.toFixed(2) ?? '—'} dB</strong></div>
            <div><span>Level offset</span><strong>{activeComparison.result.level_offset_db?.toFixed(2) ?? '—'} dB</strong></div>
            <div><span>Shape RMS</span><strong>{activeComparison.result.shape_rms_db?.toFixed(2) ?? '—'} dB</strong></div>
          </div>
          <p className="hint">comparison role: {activeComparison.result.comparison_role ?? 'legacy'}</p>
          {(activeComparison.result.interpretation_warnings?.length ?? 0) > 0 && <div className="preview">
            <strong>Interpretation warnings</strong>
            {activeComparison.result.interpretation_warnings?.map((warning) => <em key={warning}>{warning}</em>)}
          </div>}
          <div className="grid2">
            <div><h3>Intended changes</h3>{activeComparison.result.intended_changes?.length ? activeComparison.result.intended_changes.map((item) => <p className="hint" key={item.path}>{item.path}: {displayValue(item.a)} → {displayValue(item.b)}</p>) : <p className="hint">なし / 未分類</p>}</div>
            <div><h3>Confounders</h3>{activeComparison.result.confounders?.length ? activeComparison.result.confounders.map((item) => <p className="hint" key={item.path}>{item.path}: {displayValue(item.a)} → {displayValue(item.b)}</p>) : <p className="hint">検出なし</p>}</div>
          </div>
          <FrequencyPlot result={activeComparison.result} />
        </>}
        {!activeComparison && comparisons[0] && <button className="ghost" onClick={() => setActiveComparison(comparisons[0])}>最新の保存済み比較を表示</button>}
      </section>

      <section className="panel" id="model">
        <div className="section-title"><h2>Geometry</h2><span>予測候補 · 実測診断ではない</span></div>
        <p className="hint">矩形室の固有周波数と、各スピーカー→MLPの一次鏡像反射を計算します。壁の吸音率、反射位相、スピーカーの指向性、開口や家具はまだモデル化しません。</p>
        <div className="grid3">
          <label>Room mode上限 (Hz)<input value={maxModeHz} onChange={(event) => setMaxModeHz(event.target.value)} /></label>
          <label>音速 (m/s)<input value={soundSpeed} onChange={(event) => setSoundSpeed(event.target.value)} /></label>
          <div className="field-action"><button disabled={busy || !activeContext || (activeContext.payload.room.geometry_kind ?? 'rectangular') !== 'rectangular'} title={!activeContext ? '保存済みのContextを選択してください' : (activeContext.payload.room.geometry_kind ?? 'rectangular') !== 'rectangular' ? 'この解析は矩形室専用です' : undefined} onClick={() => void runAcoustics()}>選択版を解析</button></div>
        </div>
        {activeContext && (activeContext.payload.room.geometry_kind ?? 'rectangular') !== 'rectangular' && <p className="hint">このA01解析は矩形専用です。polygon/reference box Contextへ矩形モード・6面反射を誤適用しません。</p>}
        {acoustics && <>
          <div className="analysis-banner"><strong>{acoustics.classification}</strong><span>{acoustics.algorithm_version}</span><span>c={acoustics.sound_speed_m_s} m/s</span></div>
          {acoustics.skipped_speaker_ids.length > 0 && <p className="hint">座標未入力のため反射計算を省略: {acoustics.skipped_speaker_ids.join(', ')}</p>}
          <h3>Room modes（先頭24件）</h3>
          <div className="analysis-grid">
            {acoustics.room_modes.slice(0, 24).map((mode) => <div key={`${mode.n_x}-${mode.n_y}-${mode.n_z}`}><strong>{mode.frequency_hz.toFixed(1)} Hz</strong><span>({mode.n_x},{mode.n_y},{mode.n_z}) · {mode.mode_class}</span></div>)}
          </div>
          <h3>一次反射候補</h3>
          <div className="analysis-grid wide">
            {acoustics.first_order_reflections.map((candidate) => <div key={`${candidate.speaker_id}-${candidate.surface}`}><strong>{candidate.speaker_id} · {candidate.surface}</strong><span>ΔL {candidate.excess_length_m.toFixed(3)} m · +{candidate.excess_delay_ms.toFixed(2)} ms</span><span>180°幾何候補 {candidate.first_destructive_hz ? `${candidate.first_destructive_hz.toFixed(1)} Hz` : '—'}</span></div>)}
          </div>
        </>}
      </section>

      <FeatureCandidatePanel projectId={projectId} measurements={measurements} />
    </main>
  )
}
