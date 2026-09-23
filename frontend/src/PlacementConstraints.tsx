import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import { ConstraintBuilder } from './ConstraintBuilder'
import type { ContextPayload } from './plots'

type ContextRecord = { id: string; revision_number: number; payload: ContextPayload }
type ConstraintSetRecord = {
  id: string
  context_id: string
  name: string | null
  created_at: string
  integrity_valid: boolean
  spec: {
    engine_version: string
    geometry_version: string
    entity_profiles: { entity_id: string; footprint_radius_m: number; safety_margin_m: number }[]
    constraints: { constraint_id: string; kind: string }[]
  }
}
type DraftPosition = { x: string; y: string; z: string }
type EvaluationObservation = {
  constraint_id: string
  kind: string
  entity_ids: string[]
  actual: Record<string, unknown>
  required: Record<string, unknown>
  passed: boolean
}
type EvaluationRejection = {
  constraint_id: string
  kind: string
  entity_ids: string[]
  message: string
  details: Record<string, unknown>
}
type EvaluationResult = {
  classification: string
  engine_version: string
  geometry_version: string
  feasible: boolean
  overridden_entity_ids: string[]
  observations: EvaluationObservation[]
  rejections: EvaluationRejection[]
}

function formatValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function baselinePositions(context: ContextRecord): Record<string, DraftPosition> {
  const result: Record<string, DraftPosition> = {}
  const mlp = context.payload.measurement_point.position
  const measurementPointId = context.payload.measurement_point.point_id ?? context.payload.measurement_point.label
  result[measurementPointId] = { x: String(mlp.x_m), y: String(mlp.y_m), z: String(mlp.z_m) }
  for (const speaker of context.payload.speakers) {
    if (!speaker.position) continue
    result[speaker.speaker_id] = {
      x: String(speaker.position.x_m), y: String(speaker.position.y_m), z: String(speaker.position.z_m),
    }
  }
  return result
}

function parsePosition(position: DraftPosition, entityId: string) {
  const values = [Number(position.x), Number(position.y), Number(position.z)]
  if (values.some((value) => !Number.isFinite(value))) {
    throw new Error(`${entityId}: X/Y/Zをすべて数値で入力してください`)
  }
  return { x_m: values[0], y_m: values[1], z_m: values[2] }
}
function ConstraintMap({ context, positions, result }: {
  context: ContextRecord
  positions: Record<string, DraftPosition>
  result: EvaluationResult | null
}) {
  const width = context.payload.room.width_m
  const depth = context.payload.room.depth_m
  const vertices = context.payload.room.geometry_kind === 'polygon_prism' && context.payload.room.footprint_vertices?.length
    ? context.payload.room.footprint_vertices
    : [
        { vertex_id: 'front_left', x_m: 0, y_m: 0 },
        { vertex_id: 'front_right', x_m: width, y_m: 0 },
        { vertex_id: 'rear_right', x_m: width, y_m: depth },
        { vertex_id: 'rear_left', x_m: 0, y_m: depth },
      ]
  const sx = (x: number) => 60 + (x / width) * 880
  const sy = (y: number) => 640 - (y / depth) * 580
  const baseline = baselinePositions(context)
  const rejected = new Set(result?.rejections.flatMap((item) => item.entity_ids) ?? [])
  const polygonPoints = vertices.map((point) => `${sx(point.x_m)},${sy(point.y_m)}`).join(' ')
  const entries = Object.entries(positions).flatMap(([entityId, point]) => {
    const x = Number(point.x); const y = Number(point.y)
    return Number.isFinite(x) && Number.isFinite(y) ? [[entityId, x, y] as const] : []
  })

  return <div className="constraint-map-shell">
    <svg className="constraint-map" viewBox="0 0 1000 700" role="img" aria-label="Top-down placement constraint map">
      <defs>
        <pattern id="grid" width="44" height="44" patternUnits="userSpaceOnUse">
          <path d="M 44 0 L 0 0 0 44" className="map-grid-line" />
        </pattern>
      </defs>
      <rect x="24" y="24" width="952" height="652" rx="38" className="map-canvas" />
      <rect x="44" y="44" width="912" height="612" rx="28" fill="url(#grid)" opacity=".65" />
      <polygon points={polygonPoints} className="room-footprint" />
      <text x="60" y="675" className="map-axis-label">FRONT · Y=0</text>
      {entries.map(([entityId, x, y]) => {
        const base = baseline[entityId]
        const bx = base ? Number(base.x) : x; const by = base ? Number(base.y) : y
        const changed = Math.abs(x - bx) > 1e-9 || Math.abs(y - by) > 1e-9
        return <g key={entityId}>
          {changed && <line x1={sx(bx)} y1={sy(by)} x2={sx(x)} y2={sy(y)} className="movement-line" />}
          {changed && <circle cx={sx(bx)} cy={sy(by)} r="8" className="baseline-dot" />}
          <circle cx={sx(x)} cy={sy(y)} r="16" className={rejected.has(entityId) ? 'entity-dot rejected' : 'entity-dot'} />
          <text x={sx(x)} y={sy(y) - 25} textAnchor="middle" className="entity-label">{entityId}</text>
        </g>
      })}
    </svg>
    <div className="map-legend"><span><i className="legend-dot candidate" />候補位置</span><span><i className="legend-dot baseline" />Context基準</span><span><i className="legend-line" />移動</span></div>
  </div>
}
export function PlacementConstraintPanel({ projectId, context }: { projectId: string; context: ContextRecord | null }) {
  const [sets, setSets] = useState<ConstraintSetRecord[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [positions, setPositions] = useState<Record<string, DraftPosition>>({})
  const [result, setResult] = useState<EvaluationResult | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const selected = useMemo(
    () => sets.find((item) => item.id === selectedId) ?? sets[0] ?? null,
    [sets, selectedId],
  )
  const entityIds = useMemo(() => Object.keys(positions).sort(), [positions])

  async function reloadConstraintSets() {
    if (!projectId || !context) return
    const items = await api<ConstraintSetRecord[]>(`/api/projects/${projectId}/constraint-sets?context_id=${encodeURIComponent(context.id)}`)
    setSets(items)
    setSelectedId((current) => items.some((item) => item.id === current) ? current : (items[0]?.id ?? ''))
  }

  useEffect(() => {
    setResult(null); setError(''); setSelectedId(''); setSets([])
    if (!projectId || !context) return
    setPositions(baselinePositions(context))
    void reloadConstraintSets().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'ConstraintSet読込失敗'))
  }, [projectId, context])

  async function evaluate() {
    if (!projectId || !context || !selected) return
    setLoading(true); setError('')
    try {
      const payload: Record<string, { x_m: number; y_m: number; z_m: number }> = {}
      for (const entityId of entityIds) payload[entityId] = parsePosition(positions[entityId], entityId)
      setResult(await api<EvaluationResult>(
        `/api/projects/${projectId}/constraint-sets/${selected.id}/evaluate`,
        { method: 'POST', body: JSON.stringify({ positions: payload }) },
      ))
    } catch (reason) {
      setResult(null)
      setError(reason instanceof Error ? reason.message : 'Constraint評価失敗')
    } finally { setLoading(false) }
  }

  if (!context) return null
  return <section className="panel constraint-panel" id="constraints">
    <div className="section-title premium-title">
      <div><span className="section-kicker">Step 3 · G10 · Physical feasibility</span><h2>Placement Constraints</h2></div>
      <span className="status-pill neutral">Context R{context.revision_number}</span>
    </div>
    <div className="constraint-intro">
      <div><strong>置ける場所だけを、探索へ。</strong><p>家具・通路・壁離隔・筐体余白・左右連動をhard gateとして先に判定します。</p></div>
      <div className="constraint-summary">
        <span>{sets.length}<small>saved sets</small></span>
        <span>{selected?.spec.constraints.length ?? 0}<small>hard rules</small></span>
        <span>{selected?.spec.entity_profiles.length ?? 0}<small>profiles</small></span>
      </div>
    </div>
    {error && <div className="notice error">{error}</div>}
    <ConstraintBuilder key={context.id} projectId={projectId} context={context} onSaved={reloadConstraintSets} />
    <div className="constraint-toolbar">
      <label>ConstraintSet
        <select value={selected?.id ?? ''} onChange={(event) => { setSelectedId(event.target.value); setResult(null) }}>
          <option value="">選択</option>
          {sets.map((item) => <option key={item.id} value={item.id}>{item.name ?? item.id.slice(0, 8)}</option>)}
        </select>
      </label>
      {selected && <div className="constraint-chips">
        {selected.spec.constraints.map((item) => <span key={item.constraint_id} title={item.constraint_id}>{item.kind.replaceAll('_', ' ')}</span>)}
      </div>}
    </div>
    {sets.length === 0 && <div className="empty-state">
      <div className="empty-icon">⌁</div><strong>まだ制約セットがありません</strong>
      <p>家具位置や必要離隔など、実測していない値は推測せず保存しません。</p>
    </div>}
    {selected && <div className="constraint-workspace">
      <ConstraintMap context={context} positions={positions} result={result} />
      <div className="constraint-editor">
        <div className="editor-heading"><div><span className="section-kicker">Candidate</span><h3>候補座標</h3></div>
          {result && <span className={result.feasible ? 'status-pill success' : 'status-pill danger'}>{result.feasible ? 'FEASIBLE' : 'REJECTED'}</span>}
        </div>
        <div className="entity-position-list">
          {entityIds.map((entityId) => <div className="entity-position-card" key={entityId}>
            <strong>{entityId}</strong>
            <div className="axis-fields">{(['x', 'y', 'z'] as const).map((axis) => <label key={axis}><span>{axis.toUpperCase()}</span><input
              aria-label={`${entityId} ${axis.toUpperCase()}`}
              inputMode="decimal"
              value={positions[entityId][axis]}
              onChange={(event) => setPositions({ ...positions, [entityId]: { ...positions[entityId], [axis]: event.target.value } })}
            /></label>)}</div>
          </div>)}
        </div>
        <div className="row action-row">
          <button type="button" disabled={loading || !selected.integrity_valid} onClick={() => void evaluate()}>{loading ? 'Checking…' : '配置可能性を判定'}</button>
          <button type="button" className="ghost" onClick={() => { setPositions(baselinePositions(context)); setResult(null) }}>基準位置へ戻す</button>
        </div>
      </div>
    </div>}
    {result && <div className={result.feasible ? 'constraint-result-card success' : 'constraint-result-card danger'}>
      <div className="result-hero">
        <div><span className="section-kicker">Hard gate result</span><h3>{result.feasible ? 'この配置は設置制約を満たします' : 'この配置は探索対象から除外されます'}</h3></div>
        <strong>{result.rejections.length}</strong><span>rejections</span>
      </div>
      {result.feasible ? <p className="hint">物理的に配置可能という判定です。音響的に優れていることはまだ意味しません。</p> : <div className="rejection-list">
        {result.rejections.map((item, index) => <article key={`${item.constraint_id}-${index}`}>
          <div className="rejection-index">{index + 1}</div>
          <div><strong>{item.constraint_id}</strong><span>{item.kind} · {item.entity_ids.join(', ')}</span><p>{item.message}</p><code>{formatValue(item.details)}</code></div>
        </article>)}
      </div>}
    </div>}
    {selected && <div className="constraint-meta">
      <span>{selected.spec.engine_version}</span><span>{selected.spec.geometry_version}</span><span>immutable ConstraintSet · {selected.id.slice(0, 8)}</span>
    </div>}
  </section>
}
