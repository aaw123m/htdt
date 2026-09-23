import { useEffect, useMemo, useState } from 'react'
import { api } from './api'

type RewStatus = {
  connected: boolean
  read_only: boolean
  base_url: string
  measurement_count: number | null
  error: string | null
}

type RewAudioPreflight = {
  read_only: boolean
  audio_enabled: boolean | null
  audio_ready: boolean | null
  driver: string
  sample_rate_hz: number | null
  sample_rate_unit: string | null
  java: null | {
    input_device: string | null
    input_devices: string[]
    hardware_input_channels: number
    input: string | null
    inputs: string[]
    input_channel: number | null
    input_channels: number
    input_endpoint_ready: boolean
    input_cal_selection: string | null
    input_cal_file: string | null
    input_cal_file_present: boolean
    output_device: string | null
    output_devices: string[]
    hardware_output_channels: number
    output_channels: string[]
    output_channel_mapping: Array<{ index?: number; hardwareChannel?: number; channelLabel?: string }>
    stereo_only: boolean | null
    exclusive_output_candidates: string[]
    current_output_is_exclusive: boolean
    multichannel_ready: boolean
  }
  warnings: string[]
}

type RewMeasurement = Record<string, unknown> & {
  uuid?: string
  title?: string
  name?: string
}

type Project = { id: string; name: string; created_at: string }
type ContextRecord = { id: string; revision_number: number; created_at: string }
type SessionRecord = { id: string; purpose: string | null; started_at: string | null; created_at: string }
type QualityStatus = 'usable' | 'warning' | 'invalid' | 'unknown'
type RoutingEvidence = 'verified' | 'manual' | 'inferred' | 'unknown'
type EvidenceType = 'measured' | 'derived' | 'predicted' | 'unknown'
type SnapshotImportResult = {
  measurement_id: string
  dataset_id: string
  asset_sha256: string
  duplicate_asset: boolean
  existing_dataset_count: number
  quality_status: QualityStatus
  routing_evidence: RoutingEvidence
  evidence_type: EvidenceType
}

type RewFrequencyResponse = {
  measurement_id: string
  unit: string | null
  smoothing: string | null
  start_frequency_hz: number
  points_per_octave: number | null
  frequency_step_hz: number | null
  frequency_hz: number[]
  magnitude: number[]
  phase_deg: number[] | null
  requested_unit: string
  requested_ppo: number | null
  requested_smoothing: string | null
}

function measurementLabel(measurement: RewMeasurement): string {
  const title = typeof measurement.title === 'string' && measurement.title.trim()
    ? measurement.title.trim()
    : typeof measurement.name === 'string' && measurement.name.trim()
      ? measurement.name.trim()
      : null
  const uuid = typeof measurement.uuid === 'string' ? measurement.uuid : ''
  if (title && uuid) return `${title} · ${uuid.slice(0, 8)}`
  return title || uuid || 'unnamed measurement'
}

function validMeasurementId(measurement: RewMeasurement): string | null {
  return typeof measurement.uuid === 'string' && measurement.uuid.trim() ? measurement.uuid : null
}

function finiteRange(values: number[]): { min: number; max: number } | null {
  const finite = values.filter(Number.isFinite)
  if (!finite.length) return null
  return { min: Math.min(...finite), max: Math.max(...finite) }
}

function FrequencyPreview({ response }: { response: RewFrequencyResponse }) {
  const geometry = useMemo(() => {
    const points = response.frequency_hz
      .map((frequency, index) => ({ frequency, level: response.magnitude[index] }))
      .filter((point) => Number.isFinite(point.frequency) && point.frequency > 0 && Number.isFinite(point.level))
    if (points.length < 2) return null

    const frequencyRange = finiteRange(points.map((point) => point.frequency))
    const levelRange = finiteRange(points.map((point) => point.level))
    if (!frequencyRange || !levelRange || frequencyRange.min === frequencyRange.max) return null

    const width = 960
    const height = 300
    const left = 56
    const right = 18
    const top = 18
    const bottom = 38
    const plotWidth = width - left - right
    const plotHeight = height - top - bottom
    const logMin = Math.log2(frequencyRange.min)
    const logMax = Math.log2(frequencyRange.max)
    const levelPadding = Math.max(1, (levelRange.max - levelRange.min) * 0.08)
    const levelMin = levelRange.min - levelPadding
    const levelMax = levelRange.max + levelPadding

    const x = (frequency: number) => left + ((Math.log2(frequency) - logMin) / (logMax - logMin)) * plotWidth
    const y = (level: number) => top + ((levelMax - level) / (levelMax - levelMin)) * plotHeight
    const polyline = points.map((point) => `${x(point.frequency).toFixed(2)},${y(point.level).toFixed(2)}`).join(' ')
    const frequencyTicks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
      .filter((frequency) => frequency >= frequencyRange.min && frequency <= frequencyRange.max)
    const levelTicks = Array.from({ length: 5 }, (_, index) => levelMin + (index * (levelMax - levelMin)) / 4)

    return { width, height, left, top, plotWidth, plotHeight, x, y, polyline, frequencyTicks, levelTicks, levelMin, levelMax }
  }, [response])

  if (!geometry) return <p className="hint">FRプレビューに必要な有効点が不足しています。</p>

  return (
    <svg className="rew-fr-preview" viewBox={`0 0 ${geometry.width} ${geometry.height}`} role="img" aria-label="REW frequency response preview">
      <rect x={geometry.left} y={geometry.top} width={geometry.plotWidth} height={geometry.plotHeight} fill="white" stroke="currentColor" opacity="0.18" />
      {geometry.frequencyTicks.map((frequency) => (
        <g key={frequency}>
          <line x1={geometry.x(frequency)} x2={geometry.x(frequency)} y1={geometry.top} y2={geometry.top + geometry.plotHeight} stroke="currentColor" opacity="0.10" />
          <text x={geometry.x(frequency)} y={geometry.height - 12} textAnchor="middle" fontSize="11" fill="currentColor">{frequency >= 1000 ? `${frequency / 1000}k` : frequency}</text>
        </g>
      ))}
      {geometry.levelTicks.map((level) => (
        <g key={level}>
          <line x1={geometry.left} x2={geometry.left + geometry.plotWidth} y1={geometry.y(level)} y2={geometry.y(level)} stroke="currentColor" opacity="0.10" />
          <text x={geometry.left - 8} y={geometry.y(level) + 4} textAnchor="end" fontSize="11" fill="currentColor">{level.toFixed(1)}</text>
        </g>
      ))}
      <polyline points={geometry.polyline} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      <text x={geometry.left + geometry.plotWidth / 2} y={geometry.height - 1} textAnchor="middle" fontSize="11" fill="currentColor">Frequency (Hz, log)</text>
      <text x="12" y={geometry.top + geometry.plotHeight / 2} textAnchor="middle" fontSize="11" fill="currentColor" transform={`rotate(-90 12 ${geometry.top + geometry.plotHeight / 2})`}>Magnitude ({response.unit ?? response.requested_unit})</text>
    </svg>
  )
}

export function RewReadonlyPanel() {
  const [status, setStatus] = useState<RewStatus | null>(null)
  const [measurements, setMeasurements] = useState<RewMeasurement[]>([])
  const [preflight, setPreflight] = useState<RewAudioPreflight | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [ppo, setPpo] = useState('96')
  const [unit, setUnit] = useState('SPL')
  const [smoothing, setSmoothing] = useState('')
  const [response, setResponse] = useState<RewFrequencyResponse | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [contexts, setContexts] = useState<ContextRecord[]>([])
  const [contextId, setContextId] = useState('')
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [sessionId, setSessionId] = useState('')
  const [channelRole, setChannelRole] = useState('front_left')
  const [sourceSpeakerIds, setSourceSpeakerIds] = useState('')
  const [qualityStatus, setQualityStatus] = useState<QualityStatus>('unknown')
  const [routingEvidence, setRoutingEvidence] = useState<RoutingEvidence>('unknown')
  const [evidenceType, setEvidenceType] = useState<EvidenceType>('unknown')
  const [repeatGroup, setRepeatGroup] = useState('')
  const [saveResult, setSaveResult] = useState<SnapshotImportResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function refreshTargets() {
    const rows = await api<Project[]>('/api/projects')
    setProjects(rows)
    setProjectId((current) => rows.some((item) => item.id === current) ? current : rows[0]?.id ?? '')
  }

  async function refreshStatus() {
    setLoading(true)
    setError('')
    setResponse(null)
    try {
      const nextStatus = await api<RewStatus>('/api/rew/status')
      setStatus(nextStatus)
      if (!nextStatus.connected) {
        setMeasurements([])
        setPreflight(null)
        setSelectedId('')
        return
      }
      const [rows, audio] = await Promise.all([
        api<RewMeasurement[]>('/api/rew/measurements'),
        api<RewAudioPreflight>('/api/rew/audio-preflight'),
      ])
      setMeasurements(rows)
      setPreflight(audio)
      const firstId = rows.map(validMeasurementId).find((id): id is string => id !== null) ?? ''
      setSelectedId((current) => rows.some((item) => validMeasurementId(item) === current) ? current : firstId)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'REW状態の取得に失敗しました')
    } finally {
      setLoading(false)
    }
  }

  async function preview() {
    if (!selectedId) return
    const parsedPpo = Number(ppo)
    if (!Number.isInteger(parsedPpo) || parsedPpo < 1 || parsedPpo > 384) {
      setError('PPOは1〜384の整数で指定してください')
      return
    }
    setLoading(true)
    setError('')
    try {
      const query = new URLSearchParams({ ppo: String(parsedPpo), unit: unit.trim() || 'SPL' })
      if (smoothing.trim()) query.set('smoothing', smoothing.trim())
      const next = await api<RewFrequencyResponse>(`/api/rew/measurements/${encodeURIComponent(selectedId)}/frequency-response?${query.toString()}`)
      setResponse(next)
    } catch (reason) {
      setResponse(null)
      setError(reason instanceof Error ? reason.message : 'FRの取得に失敗しました')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refreshStatus()
    void refreshTargets().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'HTDT保存先の取得に失敗しました'))
  }, [])

  useEffect(() => {
    setSaveResult(null)
    if (!projectId) {
      setContexts([])
      setSessions([])
      setContextId('')
      setSessionId('')
      return
    }
    void Promise.all([
      api<ContextRecord[]>(`/api/projects/${projectId}/contexts`),
      api<SessionRecord[]>(`/api/projects/${projectId}/sessions`),
    ]).then(([contextRows, sessionRows]) => {
      setContexts(contextRows)
      setSessions(sessionRows)
      setContextId((current) => contextRows.some((item) => item.id === current) ? current : contextRows[0]?.id ?? '')
      setSessionId((current) => sessionRows.some((item) => item.id === current) ? current : '')
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'HTDT保存先の取得に失敗しました'))
  }, [projectId])

  async function saveSnapshot() {
    if (!selectedId) return
    if (!projectId || !contextId) {
      setError('保存先ProjectとContextを選択してください')
      return
    }
    if (!channelRole.trim()) {
      setError('channel roleを入力してください')
      return
    }
    const speakerIds = sourceSpeakerIds.split(/[,;\n]+/).map((value) => value.trim()).filter(Boolean)
    if (!speakerIds.length) {
      setError('source speaker IDsを1件以上入力してください')
      return
    }
    const parsedPpo = Number(ppo)
    if (!Number.isInteger(parsedPpo) || parsedPpo < 1 || parsedPpo > 384) {
      setError('PPOは1〜384の整数で指定してください')
      return
    }
    setSaving(true)
    setError('')
    setSaveResult(null)
    try {
      const result = await api<SnapshotImportResult>(`/api/projects/${projectId}/rew-snapshots`, {
        method: 'POST',
        body: JSON.stringify({
          measurement_uuid: selectedId,
          context_id: contextId,
          session_id: sessionId || null,
          channel_role: channelRole.trim(),
          evidence_type: evidenceType,
          source_speaker_ids: speakerIds,
          radiation_scope: 'unknown',
          routing_evidence: routingEvidence,
          quality_status: qualityStatus,
          quality_reasons: [],
          quality_source: qualityStatus === 'unknown' ? 'unknown' : 'manual',
          repeat_group: repeatGroup.trim() || null,
          unit: unit.trim() || 'SPL',
          ppo: parsedPpo,
          smoothing: smoothing.trim() || null,
        }),
      })
      setSaveResult(result)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'REW snapshotの保存に失敗しました')
    } finally {
      setSaving(false)
    }
  }

  const selectableMeasurements = measurements.filter((measurement) => validMeasurementId(measurement) !== null)

  const umik1Visible = preflight?.java?.input_devices.some((name) => /UMIK[- ]?1/i.test(name)) ?? false
  const currentInputIsUmik1 = /UMIK[- ]?1/i.test(preflight?.java?.input_device ?? '')
  const umik1RateReady = preflight?.sample_rate_hz === 48000
  const surroundCalHint = preflight?.java?.input_cal_file?.toLowerCase().includes('_90deg') ?? false

  return (
    <main className="shell supplemental-shell">
      <section className="panel">
        <div className="section-title">
          <h2>REW read-only browser</h2>
          <span>localhost GET only · snapshot writes HTDT only</span>
        </div>
        <p className="hint">
          REW 5.40系の読取専用APIを確認し、選択したFRをHTDTへsnapshot保存できます。REW側にはHTTP GETしか行わず、測定開始・Generator・REW設定変更は行いません。
        </p>
        <div className="row action-row">
          <button type="button" className="ghost" disabled={loading} onClick={() => void refreshStatus()}>{loading ? '確認中…' : 'REW接続を再確認'}</button>
          {status && <span className="hint">{status.base_url} · read-only: {status.read_only ? 'yes' : 'no'}</span>}
        </div>

        {error && <div className="notice error">{error}</div>}

        {status && !status.connected && (
          <div className="analysis-banner">
            <strong>offline</strong>
            <span>REW未起動でもHTDTの保存済みデータは通常利用できます。</span>
            {status.error && <span>{status.error}</span>}
          </div>
        )}

        {status?.connected && (
          <>
            <div className="analysis-banner">
              <strong>connected · read-only</strong>
              <span>{status.measurement_count ?? measurements.length} measurement(s)</span>
              <span>REWへのHTTP GETのみ</span>
            </div>
            {preflight && <div className="preview rew-preflight">
              <strong>Measurement audio preflight</strong>
              <span>driver: {preflight.driver} · audio ready: {preflight.audio_ready === null ? 'unknown' : preflight.audio_ready ? 'yes' : 'no'}</span>
              <span>sample rate: {preflight.sample_rate_hz ?? 'unknown'} {preflight.sample_rate_unit ?? ''}</span>
              <span>target mic: miniDSP UMIK-1 · visible: {umik1Visible ? 'yes' : 'no'} · selected: {currentInputIsUmik1 ? 'yes' : 'no'} · 48 kHz: {umik1RateReady ? 'yes' : 'no'}</span>
              {preflight.java ? <>
                <span>input: {preflight.java.input_device ?? 'unknown'} · {preflight.java.input ?? 'unknown'} · ch {preflight.java.input_channel ?? 'unknown'}/{preflight.java.input_channels}</span>
                <span>input endpoint ready: {preflight.java.input_endpoint_ready ? 'yes' : 'no'} · mic cal file: {preflight.java.input_cal_file_present ? preflight.java.input_cal_file : 'not selected'}</span>
                <span>HTDT surround baseline: ceiling orientation + UMIK-1 90° cal · filename hint: {surroundCalHint ? '90deg' : 'not confirmed'}</span>
                <span>output: {preflight.java.output_device ?? 'unknown'}</span>
                <span>hardware channels: {preflight.java.hardware_output_channels} · stereo-only: {preflight.java.stereo_only === null ? 'unknown' : preflight.java.stereo_only ? 'yes' : 'no'}</span>
                <span>WASAPI Exclusive: {preflight.java.current_output_is_exclusive ? 'selected' : 'not selected'} · multichannel ready: {preflight.java.multichannel_ready ? 'yes' : 'no'}</span>
                <span>EXCL candidates: {preflight.java.exclusive_output_candidates.length ? preflight.java.exclusive_output_candidates.join(' / ') : 'none'}</span>
                <span>mapping: {preflight.java.output_channel_mapping.length ? preflight.java.output_channel_mapping.map((item) => `${item.channelLabel ?? '?'}→HW${item.hardwareChannel ?? '?'}`).join(', ') : 'none'}</span>
              </> : <span>Java output preflight is not applicable for the selected driver.</span>}
              {preflight.warnings.map((warning) => <em key={warning}>{warning}</em>)}
            </div>}
            <div className="grid4 rew-controls">
              <label>Measurement
                <select value={selectedId} onChange={(event) => { setSelectedId(event.target.value); setResponse(null); setSaveResult(null) }}>
                  <option value="">選択</option>
                  {selectableMeasurements.map((measurement) => {
                    const id = validMeasurementId(measurement)!
                    return <option key={id} value={id}>{measurementLabel(measurement)}</option>
                  })}
                </select>
              </label>
              <label>Requested PPO<input inputMode="numeric" value={ppo} onChange={(event) => setPpo(event.target.value)} /></label>
              <label>Unit<input value={unit} onChange={(event) => setUnit(event.target.value)} /></label>
              <label>Smoothing<input value={smoothing} onChange={(event) => setSmoothing(event.target.value)} placeholder="空欄=指定なし" /></label>
            </div>
            <div className="row action-row">
              <button type="button" disabled={!selectedId || loading} onClick={() => void preview()}>{loading ? '取得中…' : '96 PPO FRをプレビュー'}</button>
              {measurements.length > selectableMeasurements.length && <span className="hint">UUIDを持たない項目はFR選択から除外しています。</span>}
            </div>
          </>
        )}

        {response && (
          <>
            <div className="metrics rew-metrics">
              <div><span>Points</span><strong>{response.frequency_hz.length}</strong></div>
              <div><span>Range</span><strong>{response.frequency_hz[0]?.toFixed(1) ?? '—'}–{response.frequency_hz.at(-1)?.toFixed(1) ?? '—'} Hz</strong></div>
              <div><span>Requested</span><strong>{response.requested_ppo ?? '—'} PPO · {response.requested_unit}</strong></div>
              <div><span>Returned</span><strong>{response.points_per_octave ?? 'linear'} PPO · {response.unit ?? 'unknown'}</strong></div>
            </div>
            <div className="preview">
              <strong>Provenance</strong>
              <span>requested smoothing: {response.requested_smoothing ?? 'none'}</span>
              <span>returned smoothing: {response.smoothing ?? 'unknown/none'}</span>
              <span>phase: {response.phase_deg ? `${response.phase_deg.length} points` : 'not returned'}</span>
            </div>
            <FrequencyPreview response={response} />
            <p className="hint">この曲線はREWからその場でGETしたプレビューです。保存操作ではプレビューを流用せず、REW measurementを再GETしてbefore/after summary一致を確認したsnapshotをHTDTへ保存します。</p>
            <div className="preview">
              <strong>HTDTへスナップショット保存</strong>
              <span>保存先とprovenanceを明示してください。API取得だけでは measured / usable / routing verified に自動昇格しません。</span>
              <div className="grid2 rew-snapshot-form">
                <label>Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">選択</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
                <label>Context<select value={contextId} onChange={(event) => setContextId(event.target.value)}><option value="">選択</option>{contexts.map((context) => <option key={context.id} value={context.id}>R{context.revision_number}</option>)}</select></label>
                <label>Session（任意）<select value={sessionId} onChange={(event) => setSessionId(event.target.value)}><option value="">なし</option>{sessions.map((session) => <option key={session.id} value={session.id}>{session.purpose || session.started_at || session.id.slice(0, 8)}</option>)}</select></label>
                <label>Channel role<input value={channelRole} onChange={(event) => setChannelRole(event.target.value)} /></label>
                <label>Source speaker IDs<input value={sourceSpeakerIds} onChange={(event) => setSourceSpeakerIds(event.target.value)} placeholder="FL, FR" /></label>
                <label>Repeat group<input value={repeatGroup} onChange={(event) => setRepeatGroup(event.target.value)} placeholder="任意" /></label>
                <label>Evidence<select value={evidenceType} onChange={(event) => setEvidenceType(event.target.value as EvidenceType)}><option value="unknown">unknown</option><option value="measured">measured</option><option value="derived">derived</option><option value="predicted">predicted</option></select></label>
                <label>Quality<select value={qualityStatus} onChange={(event) => setQualityStatus(event.target.value as QualityStatus)}><option value="unknown">unknown</option><option value="usable">usable</option><option value="warning">warning</option><option value="invalid">invalid</option></select></label>
                <label>Routing evidence<select value={routingEvidence} onChange={(event) => setRoutingEvidence(event.target.value as RoutingEvidence)}><option value="unknown">unknown</option><option value="manual">manual</option><option value="verified">verified</option><option value="inferred">inferred</option></select></label>
              </div>
              <div className="row action-row">
                <button type="button" disabled={saving || !projectId || !contextId || !selectedId} onClick={() => void saveSnapshot()}>{saving ? '保存中…' : 'HTDTへスナップショット保存'}</button>
                <span className="hint">REW UUIDだけで既存Measurementと自動統合しません。同一RawAssetでも新しいMeasurement/Datasetとして保存します。</span>
              </div>
              {saveResult && <div className="analysis-banner">
                <strong>saved</strong>
                <span>measurement {saveResult.measurement_id.slice(0, 8)} · dataset {saveResult.dataset_id.slice(0, 8)}</span>
                <span>raw SHA {saveResult.asset_sha256.slice(0, 12)}… · duplicate raw: {saveResult.duplicate_asset ? `yes (${saveResult.existing_dataset_count} existing dataset(s))` : 'no'}</span>
                <span>quality {saveResult.quality_status} · evidence {saveResult.evidence_type} · routing {saveResult.routing_evidence}</span>
              </div>}
            </div>
          </>
        )}
      </section>
    </main>
  )
}
