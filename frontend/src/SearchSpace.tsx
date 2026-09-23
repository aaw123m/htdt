import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import type { ContextPayload } from './plots'

type ContextRecord = { id: string; revision_number: number; payload: ContextPayload }
type AxisName = 'x' | 'y' | 'z'
type AxisDraft = { key: string; entity_id: string; axis: AxisName; min_m: string; max_m: string; step_m: string }
type LinkedConstraint = { constraint_id: string; kind: string; entity_a: string; entity_b: string; relation: string }
type ConstraintSetRecord = {
  id: string; context_id: string; name: string | null; integrity_valid: boolean
  spec: { constraints: Array<{ constraint_id: string; kind: string; entity_a?: string; entity_b?: string; relation?: string }> }
}
type SearchSpecRecord = {
  id: string; context_id: string; constraint_set_id: string; name: string | null
  spec_sha256: string; integrity_valid: boolean; created_at: string
  spec: { algorithm_version: string; axes: Array<{ entity_id: string; axis: AxisName; min_m: number; max_m: number; step_m: number }>; linked_derivations: Array<{ constraint_id: string; master_entity_id: string }>; candidate_limit: number }
}
type SearchPreview = { classification: string; raw_candidate_count: number; linked_derivation_count: number; axis_counts: Array<{ entity_id: string; axis: AxisName; count: number }> }
type Candidate = { candidate_id: string; raw_index: number; feasible_index: number; positions: Record<string, { x_m: number; y_m: number; z_m: number }> }
type SearchResult = {
  raw_candidate_count: number; feasible_candidate_count: number; rejected_candidate_count: number; duplicate_candidate_count: number
  candidate_set_sha256: string; offset: number; limit: number; returned_candidate_count: number
  rejection_counts: Record<string, number>; candidates: Candidate[]
}

function entityPositions(context: ContextRecord) {
  const rows: Record<string, { x_m: number; y_m: number; z_m: number }> = {}
  const point = context.payload.measurement_point
  rows[point.point_id ?? point.label] = point.position
  for (const speaker of context.payload.speakers) if (speaker.position) rows[speaker.speaker_id] = speaker.position
  return rows
}

function numeric(value: string, label: string): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) throw new Error(`${label}を入力してください`)
  return parsed
}

function SearchMap({ context, result, focusEntity, selectedId }: {
  context: ContextRecord; result: SearchResult | null; focusEntity: string; selectedId: string
}) {
  const width = context.payload.room.width_m
  const depth = context.payload.room.depth_m
  const vertices = context.payload.room.geometry_kind === 'polygon_prism' && context.payload.room.footprint_vertices?.length
    ? context.payload.room.footprint_vertices
    : [
        { vertex_id: 'v0', x_m: 0, y_m: 0 }, { vertex_id: 'v1', x_m: width, y_m: 0 },
        { vertex_id: 'v2', x_m: width, y_m: depth }, { vertex_id: 'v3', x_m: 0, y_m: depth },
      ]
  const sx = (x: number) => 55 + (x / width) * 890
  const sy = (y: number) => 640 - (y / depth) * 580
  const baseline = entityPositions(context)[focusEntity]
  const points = result?.candidates.flatMap((candidate) => {
    const point = candidate.positions[focusEntity]
    return point ? [{ candidate, point }] : []
  }) ?? []
  return <div className="search-map-shell">
    <svg viewBox="0 0 1000 700" className="search-map" role="img" aria-label="Search candidate map">
      <polygon className="search-room" points={vertices.map((p) => `${sx(p.x_m)},${sy(p.y_m)}`).join(' ')} />
      <text x="55" y="674" className="map-axis-label">FRONT · Y=0</text>
      {baseline && <circle cx={sx(baseline.x_m)} cy={sy(baseline.y_m)} r="10" className="search-baseline" />}
      {points.map(({ candidate, point }) => <circle key={candidate.candidate_id} cx={sx(point.x_m)} cy={sy(point.y_m)}
        r={candidate.candidate_id === selectedId ? 14 : 7}
        className={candidate.candidate_id === selectedId ? 'search-point selected' : 'search-point'} />)}
    </svg>
    <div className="map-legend"><span><i className="legend-dot candidate" />feasible candidates</span><span><i className="legend-dot baseline" />Context baseline</span></div>
  </div>
}

export function SearchSpacePanel({ projectId, context }: { projectId: string; context: ContextRecord | null }) {
  const [constraintSets, setConstraintSets] = useState<ConstraintSetRecord[]>([])
  const [constraintSetId, setConstraintSetId] = useState('')
  const [savedSpecs, setSavedSpecs] = useState<SearchSpecRecord[]>([])
  const [selectedSpecId, setSelectedSpecId] = useState('')
  const [name, setName] = useState('Placement grid')
  const [axes, setAxes] = useState<AxisDraft[]>([])
  const [candidateLimit, setCandidateLimit] = useState('10000')
  const [derived, setDerived] = useState<Record<string, string>>({})
  const [preview, setPreview] = useState<SearchPreview | null>(null)
  const [result, setResult] = useState<SearchResult | null>(null)
  const [focusEntity, setFocusEntity] = useState('')
  const [selectedCandidateId, setSelectedCandidateId] = useState('')
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const pageSize = 120

  const positions = useMemo(() => context ? entityPositions(context) : {}, [context])
  const entityIds = useMemo(() => Object.keys(positions).sort(), [positions])
  const selectedSet = constraintSets.find((item) => item.id === constraintSetId) ?? constraintSets[0] ?? null
  const linkedConstraints = useMemo(() => (selectedSet?.spec.constraints ?? []).flatMap((item) =>
    item.kind === 'linked_placement' && item.entity_a && item.entity_b && item.relation
      ? [item as LinkedConstraint] : []), [selectedSet])
  const selectedSpec = savedSpecs.find((item) => item.id === selectedSpecId) ?? savedSpecs[0] ?? null
  const selectedCandidate = result?.candidates.find((item) => item.candidate_id === selectedCandidateId) ?? null

  async function reload() {
    if (!projectId || !context) return
    const [sets, specs] = await Promise.all([
      api<ConstraintSetRecord[]>(`/api/projects/${projectId}/constraint-sets?context_id=${encodeURIComponent(context.id)}`),
      api<SearchSpecRecord[]>(`/api/projects/${projectId}/search-specs?context_id=${encodeURIComponent(context.id)}`),
    ])
    setConstraintSets(sets)
    setSavedSpecs(specs)
    setConstraintSetId((current) => sets.some((item) => item.id === current) ? current : (sets[0]?.id ?? ''))
    setSelectedSpecId((current) => specs.some((item) => item.id === current) ? current : (specs[0]?.id ?? ''))
  }

  useEffect(() => {
    setConstraintSets([]); setSavedSpecs([]); setConstraintSetId(''); setSelectedSpecId('')
    setAxes([]); setDerived({}); setPreview(null); setResult(null); setError(''); setOffset(0)
    if (!projectId || !context) return
    const ids = Object.keys(entityPositions(context)).sort()
    setFocusEntity(ids[0] ?? '')
    void reload().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Search Space読込失敗'))
  }, [projectId, context])

  useEffect(() => {
    setPreview(null); setDerived({}); setResult(null); setOffset(0)
  }, [constraintSetId])

  function addAxis(entityId: string, axis: AxisName) {
    if (axes.some((item) => item.entity_id === entityId && item.axis === axis)) return
    const base = positions[entityId]?.[`${axis}_m`]
    if (base === undefined) return
    setAxes([...axes, { key: `${entityId}-${axis}`, entity_id: entityId, axis, min_m: String(base), max_m: String(base), step_m: '0.10' }])
    setPreview(null)
  }

  function requestPayload() {
    if (!selectedSet) throw new Error('ConstraintSetを選択してください')
    if (axes.length === 0) throw new Error('可動軸を1つ以上追加してください')
    return {
      constraint_set_id: selectedSet.id,
      name: name.trim() || null,
      algorithm: 'deterministic_grid',
      axes: axes.map((item) => ({
        entity_id: item.entity_id, axis: item.axis,
        min_m: numeric(item.min_m, `${item.entity_id}.${item.axis} min`),
        max_m: numeric(item.max_m, `${item.entity_id}.${item.axis} max`),
        step_m: numeric(item.step_m, `${item.entity_id}.${item.axis} step`),
      })),
      linked_derivations: Object.entries(derived).filter(([, master]) => master).map(([constraint_id, master_entity_id]) => ({ constraint_id, master_entity_id })),
      candidate_limit: numeric(candidateLimit, 'candidate limit'),
    }
  }

  function loadSelectedAsDraft() {
    if (!selectedSpec) return
    setConstraintSetId(selectedSpec.constraint_set_id)
    setName(selectedSpec.name ? `${selectedSpec.name} copy` : 'Placement grid copy')
    setCandidateLimit(String(selectedSpec.spec.candidate_limit))
    setAxes(selectedSpec.spec.axes.map((item) => ({
      key: `${item.entity_id}-${item.axis}`, entity_id: item.entity_id, axis: item.axis,
      min_m: String(item.min_m), max_m: String(item.max_m), step_m: String(item.step_m),
    })))
    setDerived(Object.fromEntries(selectedSpec.spec.linked_derivations.map((item) => [item.constraint_id, item.master_entity_id])))
    setPreview(null); setResult(null); setOffset(0)
  }

  async function runPreview() {
    setLoading(true); setError('')
    try {
      const value = await api<SearchPreview>(`/api/projects/${projectId}/search-specs/preview`, { method: 'POST', body: JSON.stringify(requestPayload()) })
      setPreview(value)
    } catch (reason) { setPreview(null); setError(reason instanceof Error ? reason.message : '候補数確認失敗') }
    finally { setLoading(false) }
  }

  async function saveSpec() {
    setLoading(true); setError('')
    try {
      const payload = requestPayload()
      const checked = await api<SearchPreview>(`/api/projects/${projectId}/search-specs/preview`, { method: 'POST', body: JSON.stringify(payload) })
      setPreview(checked)
      const created = await api<SearchSpecRecord>(`/api/projects/${projectId}/search-specs`, { method: 'POST', body: JSON.stringify(payload) })
      await reload()
      setSelectedSpecId(created.id)
      setResult(null); setOffset(0)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'SearchSpec保存失敗') }
    finally { setLoading(false) }
  }

  async function generate(specId = selectedSpec?.id, pageOffset = offset) {
    if (!specId) return
    setLoading(true); setError('')
    try {
      const value = await api<SearchResult>(`/api/projects/${projectId}/search-specs/${specId}/generate?offset=${pageOffset}&limit=${pageSize}`, { method: 'POST' })
      setResult(value); setOffset(pageOffset)
      setSelectedCandidateId(value.candidates[0]?.candidate_id ?? '')
      const candidateEntities = Object.keys(value.candidates[0]?.positions ?? {})
      setFocusEntity((current) => candidateEntities.includes(current) ? current : (candidateEntities[0] ?? current))
    } catch (reason) { setResult(null); setError(reason instanceof Error ? reason.message : '候補生成失敗') }
    finally { setLoading(false) }
  }

  if (!context) return null
  return <section className="panel search-panel" id="search">
    <div className="section-title premium-title">
      <div><span className="section-kicker">O10 · Deterministic Search Space</span><h2>Search Space</h2></div>
      <span className="status-pill neutral">Context R{context.revision_number}</span>
    </div>
    <div className="constraint-intro search-intro">
      <div><strong>動かす範囲を、候補へ。</strong><p>刻みと連動規則を固定し、G10を通る配置だけを同じ順序で再生成します。</p></div>
      <div className="constraint-summary">
        <span>{savedSpecs.length}<small>saved specs</small></span>
        <span>{preview?.raw_candidate_count ?? result?.raw_candidate_count ?? '—'}<small>raw grid</small></span>
        <span>{result?.feasible_candidate_count ?? '—'}<small>feasible</small></span>
      </div>
    </div>
    {error && <div className="notice error">{error}</div>}
    <div className="search-builder">
      <div className="search-config-head">
        <label>ConstraintSet<select value={selectedSet?.id ?? ''} onChange={(event) => setConstraintSetId(event.target.value)}>
          <option value="">選択</option>{constraintSets.map((item) => <option key={item.id} value={item.id}>{item.name ?? item.id.slice(0, 8)}</option>)}
        </select></label>
        <label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label>Candidate limit<input inputMode="numeric" value={candidateLimit} onChange={(event) => { setCandidateLimit(event.target.value); setPreview(null) }} /></label>
      </div>

      {selectedSet && <>
        <div className="search-step">
          <div className="step-head"><span className="step-number">1</span><strong>可動軸</strong><span className="step-count">{axes.length} axes</span></div>
          <div className="axis-selector-grid">
            {entityIds.map((entityId) => <article key={entityId}>
              <strong>{entityId}</strong>
              <div className="segmented">{(['x', 'y', 'z'] as AxisName[]).map((axis) => <button type="button" key={axis}
                className={axes.some((item) => item.entity_id === entityId && item.axis === axis) ? 'active' : ''}
                onClick={() => addAxis(entityId, axis)}>{axis.toUpperCase()}</button>)}</div>
            </article>)}
          </div>
          <div className="search-axis-list">
            {axes.map((item) => <article key={item.key}>
              <div className="axis-badge"><strong>{item.entity_id}</strong><span>{item.axis.toUpperCase()}</span></div>
              {(['min_m', 'max_m', 'step_m'] as const).map((field) => <label key={field}><span>{field.replace('_m', '')}</span><div className="unit-input"><input inputMode="decimal" value={item[field]}
                onChange={(event) => { setAxes(axes.map((row) => row.key === item.key ? { ...row, [field]: event.target.value } : row)); setPreview(null) }} /><b>m</b></div></label>)}
              <button type="button" className="icon-button" aria-label="Remove axis" onClick={() => { setAxes(axes.filter((row) => row.key !== item.key)); setPreview(null) }}>×</button>
            </article>)}
          </div>
        </div>

        <div className="search-step">
          <div className="step-head"><span className="step-number">2</span><strong>連動</strong><span className="step-count">{linkedConstraints.length} available</span></div>
          <div className="link-grid">
            {linkedConstraints.map((link) => <article key={link.constraint_id} className={derived[link.constraint_id] ? 'active' : ''}>
              <div><strong>{link.relation.replaceAll('_', ' ')}</strong><span>{link.entity_a} ↔ {link.entity_b}</span></div>
              <select value={derived[link.constraint_id] ?? ''} onChange={(event) => { setDerived({ ...derived, [link.constraint_id]: event.target.value }); setPreview(null) }}>
                <option value="">OFF</option><option value={link.entity_a}>{link.entity_a} → {link.entity_b}</option><option value={link.entity_b}>{link.entity_b} → {link.entity_a}</option>
              </select>
            </article>)}
            {linkedConstraints.length === 0 && <div className="compact-empty">linked placementなし</div>}
          </div>
        </div>
      </>}

      <div className="search-actions">
        <button type="button" className="ghost" disabled={loading || !selectedSet || axes.length === 0} onClick={() => void runPreview()}>候補数を確認</button>
        <button type="button" disabled={loading || !selectedSet || axes.length === 0} onClick={() => void saveSpec()}>SearchSpecを保存</button>
        {preview && <div className="search-preview-pill"><strong>{preview.raw_candidate_count}</strong><span>raw candidates</span></div>}
      </div>
    </div>

    <div className="constraint-toolbar search-toolbar">
      <label>Saved SearchSpec<select value={selectedSpec?.id ?? ''} onChange={(event) => { setSelectedSpecId(event.target.value); setResult(null); setOffset(0) }}>
        <option value="">選択</option>{savedSpecs.map((item) => <option key={item.id} value={item.id}>{item.name ?? item.id.slice(0, 8)}</option>)}
      </select></label>
      <button type="button" disabled={loading || !selectedSpec?.integrity_valid} onClick={() => void generate(selectedSpec?.id, 0)}>{loading ? 'Generating…' : '候補を生成'}</button>
      <button type="button" className="ghost" disabled={!selectedSpec} onClick={loadSelectedAsDraft}>複製して編集</button>
      {selectedSpec && <span className="immutable-mark">◇ immutable · {selectedSpec.spec.algorithm_version} · {selectedSpec.id.slice(0, 8)}</span>}
    </div>

    {selectedSpec && result && <>
      <div className="search-result-metrics">
        <div><span>raw</span><strong>{result.raw_candidate_count}</strong></div>
        <div><span>feasible</span><strong>{result.feasible_candidate_count}</strong></div>
        <div><span>rejected</span><strong>{result.rejected_candidate_count}</strong></div>
        <div><span>duplicate</span><strong>{result.duplicate_candidate_count}</strong></div>
      </div>
      <div className="search-workspace">
        <div>
          <div className="search-map-toolbar"><label>Map entity<select value={focusEntity} onChange={(event) => setFocusEntity(event.target.value)}>{entityIds.map((id) => <option key={id}>{id}</option>)}</select></label><span>{offset + 1}–{offset + result.returned_candidate_count} / {result.feasible_candidate_count}</span></div>
          <SearchMap context={context} result={result} focusEntity={focusEntity} selectedId={selectedCandidateId} />
          <div className="search-pagination">
            <button type="button" className="ghost" disabled={loading || offset === 0} onClick={() => void generate(selectedSpec.id, Math.max(0, offset - pageSize))}>←</button>
            <span>{Math.floor(offset / pageSize) + 1}</span>
            <button type="button" className="ghost" disabled={loading || offset + pageSize >= result.feasible_candidate_count} onClick={() => void generate(selectedSpec.id, offset + pageSize)}>→</button>
          </div>
        </div>
        <div className="candidate-browser">
          <div className="editor-heading"><div><span className="section-kicker">Feasible page</span><h3>候補</h3></div><code>{result.candidate_set_sha256.slice(0, 10)}…</code></div>
          <div className="candidate-list">{result.candidates.map((candidate) => <button type="button" key={candidate.candidate_id}
            className={candidate.candidate_id === selectedCandidateId ? 'candidate-row active' : 'candidate-row'}
            onClick={() => setSelectedCandidateId(candidate.candidate_id)}>
            <span>{candidate.feasible_index + 1}</span><div><strong>{candidate.candidate_id.slice(0, 13)}</strong><small>raw #{candidate.raw_index}</small></div>
          </button>)}</div>
          {selectedCandidate && <div className="candidate-detail">
            <div><span>SELECTED</span><strong>#{selectedCandidate.feasible_index + 1}</strong></div>
            <div className="candidate-position-grid">{Object.entries(selectedCandidate.positions).map(([entityId, point]) => <article key={entityId}>
              <strong>{entityId}</strong><span>X {point.x_m.toFixed(2)}</span><span>Y {point.y_m.toFixed(2)}</span><span>Z {point.z_m.toFixed(2)}</span>
            </article>)}</div>
          </div>}
        </div>
      </div>
      {Object.keys(result.rejection_counts).length > 0 && <div className="constraint-chips rejection-summary">{Object.entries(result.rejection_counts).map(([id, count]) => <span key={id}>{id} · {count}</span>)}</div>}
    </>}
  </section>
}
