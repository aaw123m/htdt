import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import type { ContextPayload } from './plots'

type Project = { id: string; name: string }
type OverviewContextPayload = ContextPayload & {
  measurement_point: ContextPayload['measurement_point'] & { point_id?: string }
  avr?: unknown
}
type ContextRecord = { id: string; revision_number: number; payload: OverviewContextPayload }
type Measurement = {
  id: string
  dataset_id: string
  context_id: string
  channel_role: string
  evidence_type: string
  quality_status: string
  quality_reasons: string[]
  repeat_group: string | null
  imported_at: string
}
type Difference = { path: string; a: unknown; b: unknown }
type Comparison = {
  id: string
  dataset_a_id: string
  dataset_b_id: string
  created_at: string
  spec: { low_hz?: number; high_hz?: number; label?: string | null }
  result: {
    comparison_role?: string
    mean_difference_db?: number | null
    rms_difference_db?: number | null
    level_offset_db?: number | null
    shape_rms_db?: number | null
    confounders?: Difference[]
    interpretation_warnings?: string[]
  }
}

type Point = { x_m: number; y_m: number; z_m: number }

function pointIdentity(context: ContextRecord | undefined): string {
  if (!context) return ''
  return context.payload.measurement_point.point_id || context.payload.measurement_point.label
}

function distance(a: Point | null | undefined, b: Point | null | undefined): number | null {
  if (!a || !b) return null
  return Math.hypot(a.x_m - b.x_m, a.y_m - b.y_m, a.z_m - b.z_m)
}

function speakerMap(context: ContextRecord | undefined): Map<string, Point | null> {
  const result = new Map<string, Point | null>()
  for (const speaker of context?.payload.speakers ?? []) result.set(speaker.speaker_id, speaker.position ?? null)
  return result
}

function maxSpeakerMovement(reference: ContextRecord | undefined, candidate: ContextRecord | undefined): number | null {
  const a = speakerMap(reference)
  const b = speakerMap(candidate)
  const distances: number[] = []
  for (const id of new Set([...a.keys(), ...b.keys()])) {
    const value = distance(a.get(id), b.get(id))
    if (value !== null) distances.push(value)
  }
  return distances.length ? Math.max(...distances) : null
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function pairComparison(comparisons: Comparison[], referenceDatasetId: string, candidateDatasetId: string): Comparison | null {
  if (referenceDatasetId === candidateDatasetId) return null
  return comparisons.find((comparison) =>
    (comparison.dataset_a_id === referenceDatasetId && comparison.dataset_b_id === candidateDatasetId)
      || (comparison.dataset_b_id === referenceDatasetId && comparison.dataset_a_id === candidateDatasetId),
  ) ?? null
}

function formatMetric(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)} dB` : '—'
}

export function PlacementOverviewPanel() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [contexts, setContexts] = useState<ContextRecord[]>([])
  const [measurements, setMeasurements] = useState<Measurement[]>([])
  const [comparisons, setComparisons] = useState<Comparison[]>([])
  const [channelRole, setChannelRole] = useState('')
  const [pointId, setPointId] = useState('')
  const [referenceDatasetId, setReferenceDatasetId] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    void api<Project[]>('/api/projects').then((items) => {
      setProjects(items)
      setProjectId((current) => current || items[0]?.id || '')
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'プロジェクト一覧の読込に失敗しました'))
  }, [])

  useEffect(() => {
    setContexts([])
    setMeasurements([])
    setComparisons([])
    setReferenceDatasetId('')
    if (!projectId) return
    void Promise.all([
      api<ContextRecord[]>(`/api/projects/${projectId}/contexts`),
      api<Measurement[]>(`/api/projects/${projectId}/measurements`),
      api<Comparison[]>(`/api/projects/${projectId}/comparisons`),
    ]).then(([contextRows, measurementRows, comparisonRows]) => {
      setContexts(contextRows)
      setMeasurements(measurementRows)
      setComparisons(comparisonRows)
      const firstMeasured = measurementRows.find((measurement) => measurement.evidence_type === 'measured')
      setChannelRole(firstMeasured?.channel_role ?? '')
      const firstContext = contextRows.find((context) => context.id === firstMeasured?.context_id)
      setPointId(pointIdentity(firstContext))
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '配置比較データの読込に失敗しました'))
  }, [projectId])

  const contextsById = useMemo(() => new Map(contexts.map((context) => [context.id, context])), [contexts])
  const channelRoles = useMemo(() => Array.from(new Set(measurements.filter((row) => row.evidence_type === 'measured').map((row) => row.channel_role))).sort(), [measurements])
  const pointIds = useMemo(() => Array.from(new Set(contexts.map((context) => pointIdentity(context)).filter(Boolean))).sort(), [contexts])
  const rows = useMemo(() => measurements.filter((measurement) => {
    if (measurement.evidence_type !== 'measured') return false
    if (channelRole && measurement.channel_role !== channelRole) return false
    const context = contextsById.get(measurement.context_id)
    if (!context) return false
    if (pointId && pointIdentity(context) !== pointId) return false
    return true
  }), [measurements, channelRole, pointId, contextsById])

  useEffect(() => {
    if (!rows.some((row) => row.dataset_id === referenceDatasetId)) setReferenceDatasetId(rows[0]?.dataset_id ?? '')
  }, [rows, referenceDatasetId])

  const referenceMeasurement = rows.find((row) => row.dataset_id === referenceDatasetId)
  const referenceContext = referenceMeasurement ? contextsById.get(referenceMeasurement.context_id) : undefined

  return (
    <main className="shell">
      <section className="panel">
        <div className="section-title"><h2>Layout History</h2><span>履歴一覧 · ランキングしない</span></div>
        <p className="hint">同じchannel / measurement pointの実測を配置版ごとに並べます。移動量、品質、AVR/部屋/MLP差、保存済みA/B比較を確認できます。この一覧自体は「最良配置」を選びません。</p>
        <div className="grid4">
          <label>Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">選択</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
          <label>Channel<select value={channelRole} onChange={(event) => { setChannelRole(event.target.value); setReferenceDatasetId('') }}><option value="">すべて</option>{channelRoles.map((role) => <option key={role} value={role}>{role}</option>)}</select></label>
          <label>Measurement point<select value={pointId} onChange={(event) => { setPointId(event.target.value); setReferenceDatasetId('') }}><option value="">すべて</option>{pointIds.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
          <label>Reference<select value={referenceDatasetId} onChange={(event) => setReferenceDatasetId(event.target.value)}><option value="">選択</option>{rows.map((row) => {
            const context = contextsById.get(row.context_id)
            return <option key={row.dataset_id} value={row.dataset_id}>R{context?.revision_number ?? '?'} · {row.quality_status} · {row.dataset_id.slice(0, 8)}</option>
          })}</select></label>
        </div>
        {error && <div className="notice error">{error}</div>}
        <div className="cards">
          {rows.map((measurement) => {
            const context = contextsById.get(measurement.context_id)
            if (!context) return null
            const comparison = referenceDatasetId ? pairComparison(comparisons, referenceDatasetId, measurement.dataset_id) : null
            const speakerMove = maxSpeakerMovement(referenceContext, context)
            const mlpMove = distance(referenceContext?.payload.measurement_point.position, context.payload.measurement_point.position)
            const avrChanged = referenceContext ? !sameJson(referenceContext.payload.avr, context.payload.avr) : false
            const roomChanged = referenceContext ? !sameJson(referenceContext.payload.room, context.payload.room) : false
            const isReference = measurement.dataset_id === referenceDatasetId
            return <article key={measurement.dataset_id}>
              <strong>{isReference ? 'REFERENCE · ' : ''}R{context.revision_number} · {measurement.channel_role}</strong>
              <span>quality {measurement.quality_status} · repeat {measurement.repeat_group ?? '—'}</span>
              <span>MLP movement {mlpMove === null ? '—' : `${(mlpMove * 100).toFixed(1)} cm`} · max known speaker movement {speakerMove === null ? '—' : `${(speakerMove * 100).toFixed(1)} cm`}</span>
              <span>room {roomChanged ? 'CHANGED' : 'same'} · AVR {avrChanged ? 'CHANGED' : 'same'}</span>
              {measurement.quality_reasons.length > 0 && <small>{measurement.quality_reasons.join(' / ')}</small>}
              {isReference
                ? <small>比較基準</small>
                : comparison
                  ? <>
                    <span>saved A/B: {comparison.spec.low_hz ?? '—'}–{comparison.spec.high_hz ?? '—'} Hz · shape RMS {formatMetric(comparison.result.shape_rms_db)} · RMS {formatMetric(comparison.result.rms_difference_db)}</span>
                    <span>confounders {comparison.result.confounders?.length ?? 0} · warnings {comparison.result.interpretation_warnings?.length ?? 0}</span>
                    <div className="row"><a className="button-link" href={`/api/projects/${projectId}/comparisons/${comparison.id}/report.html`}>比較レポート</a></div>
                  </>
                  : <small>基準との保存済みA/B比較なし</small>}
              <code>{measurement.dataset_id}</code>
            </article>
          })}
          {projectId && rows.length === 0 && <article><strong>対象実測なし</strong><span>選択したchannel / measurement pointに一致するmeasured Datasetがありません。</span></article>}
        </div>
      </section>
    </main>
  )
}
