import { useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'
import { api } from './api'
import type { ContextPayload } from './plots'

type ContextRecord = { id: string; revision_number: number; payload: ContextPayload }
type RuleKind = 'allowed_region' | 'exclusion_region' | 'wall_clearance' | 'axis_range' | 'movement_budget' | 'pair_distance' | 'linked_placement'
type Rule = Record<string, unknown> & { constraint_id: string; kind: RuleKind }
type Point2D = { x_m: number; y_m: number }
type ProfileDraft = Record<string, { radius: string; margin: string }>
type Draft = {
  id: string; kind: RuleKind; entityA: string; entityB: string; entities: string[]
  polygon: Point2D[]; polygonGroups: Point2D[][]; edgeId: string; min: string; max: string; axis: 'x' | 'y' | 'z'; fixed: string
  distanceMode: '3d' | 'horizontal_xy'; distanceReference: 'center' | 'envelope_clearance'
  relation: 'mirror_x' | 'equal_x' | 'equal_y' | 'equal_z' | 'equal_delta_x' | 'equal_delta_y' | 'equal_delta_z'
  mirrorAxis: string; tolerance: string
}

const ruleMeta: Record<RuleKind, { symbol: string; label: string; short: string }> = {
  allowed_region: { symbol: '□', label: '設置エリア', short: 'ここだけ置ける' },
  exclusion_region: { symbol: '⊘', label: '禁止エリア', short: 'ここには置かない' },
  wall_clearance: { symbol: '↔', label: '壁離隔', short: '壁から距離を取る' },
  axis_range: { symbol: 'XYZ', label: '座標固定', short: '軸を固定・制限' },
  movement_budget: { symbol: 'Δ', label: '移動上限', short: '現在位置から制限' },
  pair_distance: { symbol: '⇄', label: '相互距離', short: '2点の間隔を制限' },
  linked_placement: { symbol: '⟷', label: '連動配置', short: '左右を揃えて動かす' },
}
function numberOrNull(value: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) throw new Error(`数値「${value}」が不正です`)
  return parsed
}

// Mirrors the backend model validators in placement_constraints.py so bad rules
// fail fast instead of round-tripping to a 422.
function boundPair(minText: string, maxText: string, label: string, nonNegative: boolean): { min: number | null; max: number | null } {
  const min = numberOrNull(minText)
  const max = numberOrNull(maxText)
  if (min === null && max === null) throw new Error(`${label}: MINまたはMAXを入力してください`)
  if (min !== null && max !== null && max < min) throw new Error(`${label}: MAXはMIN以上にしてください`)
  if (nonNegative && ((min !== null && min < 0) || (max !== null && max < 0))) throw new Error(`${label}: 0以上の値を入力してください`)
  return { min, max }
}

function entityIds(context: ContextRecord): string[] {
  const mlp = context.payload.measurement_point.point_id ?? context.payload.measurement_point.label
  return [mlp, ...context.payload.speakers.map((speaker) => speaker.speaker_id)]
}

function roomVertices(context: ContextRecord) {
  const room = context.payload.room
  return room.geometry_kind === 'polygon_prism' && room.footprint_vertices?.length
    ? room.footprint_vertices
    : [
        { vertex_id: 'front_left', x_m: 0, y_m: 0 },
        { vertex_id: 'front_right', x_m: room.width_m, y_m: 0 },
        { vertex_id: 'rear_right', x_m: room.width_m, y_m: room.depth_m },
        { vertex_id: 'rear_left', x_m: 0, y_m: room.depth_m },
      ]
}

function wallEdges(context: ContextRecord) {
  const vertices = roomVertices(context)
  return vertices.map((start, index) => ({
    id: `${start.vertex_id}->${vertices[(index + 1) % vertices.length].vertex_id}`,
    start,
    end: vertices[(index + 1) % vertices.length],
  }))
}

// Ray-cast even-odd test in room coordinates (m).
function pointInPolygon(x: number, y: number, vertices: { x_m: number; y_m: number }[]): boolean {
  let inside = false
  for (let index = 0, prev = vertices.length - 1; index < vertices.length; prev = index++) {
    const a = vertices[index]
    const b = vertices[prev]
    if ((a.y_m > y) !== (b.y_m > y) && x < ((b.x_m - a.x_m) * (y - a.y_m)) / (b.y_m - a.y_m) + a.x_m) inside = !inside
  }
  return inside
}

// Closest point on the polygon boundary — pulls out-of-footprint clicks onto
// the nearest wall so a polygon room never emits a vertex the backend 422s on.
function snapToPolygon(x: number, y: number, vertices: { x_m: number; y_m: number }[]): Point2D {
  let best = { x_m: x, y_m: y }
  let bestDistance = Infinity
  for (let index = 0; index < vertices.length; index++) {
    const a = vertices[index]
    const b = vertices[(index + 1) % vertices.length]
    const dx = b.x_m - a.x_m
    const dy = b.y_m - a.y_m
    const length2 = dx * dx + dy * dy
    const t = length2 === 0 ? 0 : Math.min(1, Math.max(0, ((x - a.x_m) * dx + (y - a.y_m) * dy) / length2))
    const px = a.x_m + t * dx
    const py = a.y_m + t * dy
    const distance2 = (x - px) * (x - px) + (y - py) * (y - py)
    if (distance2 < bestDistance) {
      bestDistance = distance2
      best = { x_m: px, y_m: py }
    }
  }
  return best
}
function blankDraft(context: ContextRecord, count: number): Draft {
  const ids = entityIds(context)
  return {
    id: `rule-${count + 1}`, kind: 'allowed_region', entityA: ids[0] ?? '', entityB: ids[1] ?? ids[0] ?? '', entities: ids.slice(0, 1),
    polygon: [], polygonGroups: [], edgeId: wallEdges(context)[0]?.id ?? '', min: '', max: '', axis: 'x', fixed: '',
    distanceMode: 'horizontal_xy', distanceReference: 'center', relation: 'equal_y', mirrorAxis: '', tolerance: '0.001',
  }
}

function EntityPicker({ ids, selected, onChange, single = false }: {
  ids: string[]; selected: string[]; onChange: (value: string[]) => void; single?: boolean
}) {
  return <div className="entity-picker">{ids.map((id) => {
    const active = selected.includes(id)
    return <button key={id} type="button" className={active ? 'entity-chip active' : 'entity-chip'} onClick={() => {
      if (single) onChange([id])
      else onChange(active ? selected.filter((item) => item !== id) : [...selected, id])
    }}><span className="entity-dot" />{id}</button>
  })}</div>
}

const MAP_W = 640
const MAP_H = 390
const MAP_PAD = 34

function mapPoint(context: ContextRecord, point: { x_m: number; y_m: number }) {
  const width = Math.max(context.payload.room.width_m, 0.001)
  const depth = Math.max(context.payload.room.depth_m, 0.001)
  return {
    x: MAP_PAD + point.x_m / width * (MAP_W - MAP_PAD * 2),
    y: MAP_H - MAP_PAD - point.y_m / depth * (MAP_H - MAP_PAD * 2),
  }
}
const NUDGE_STEP_M = 0.05
const NUDGE_STEP_SHIFT_M = 0.25

function arrowDelta(key: string, step: number): { x: number; y: number } | null {
  if (key === 'ArrowLeft') return { x: -step, y: 0 }
  if (key === 'ArrowRight') return { x: step, y: 0 }
  if (key === 'ArrowUp') return { x: 0, y: step }
  if (key === 'ArrowDown') return { x: 0, y: -step }
  return null
}

function PolygonSketch({ context, points, groups, onChange, onGroupsChange, mode }: {
  context: ContextRecord; points: Point2D[]; groups: Point2D[][]; onChange: (points: Point2D[]) => void
  onGroupsChange: (groups: Point2D[][]) => void; mode: 'allowed' | 'exclusion'
}) {
  const vertices = roomVertices(context)
  const boundary = [...vertices, vertices[0]].map((point) => mapPoint(context, point))
  const drawn = points.map((point) => mapPoint(context, point))
  const completed = groups.map((group) => group.map((point) => mapPoint(context, point)))
  const room = context.payload.room
  const svgRef = useRef<SVGSVGElement>(null)
  const [cursor, setCursor] = useState<Point2D | null>(null)
  const [hint, setHint] = useState('')
  const footprintOnly = room.geometry_kind === 'polygon_prism' && (room.footprint_vertices?.length ?? 0) >= 3
  const center = {
    x_m: vertices.reduce((sum, vertex) => sum + vertex.x_m, 0) / vertices.length,
    y_m: vertices.reduce((sum, vertex) => sum + vertex.y_m, 0) / vertices.length,
  }
  const cursorPoint = cursor ? mapPoint(context, cursor) : null

  function commitPoint(rawX: number, rawY: number) {
    let x = Math.min(room.width_m, Math.max(0, rawX))
    let y = Math.min(room.depth_m, Math.max(0, rawY))
    if (footprintOnly && !pointInPolygon(x, y, vertices)) {
      const snapped = snapToPolygon(x, y, vertices)
      x = snapped.x_m
      y = snapped.y_m
      setHint('部屋の外側だったため、最も近い境界上の点へ寄せました')
    } else {
      setHint('')
    }
    const point = { x_m: Math.round(x * 100) / 100, y_m: Math.round(y * 100) / 100 }
    setCursor(point)
    onChange([...points, point])
  }

  function addPoint(event: MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    const sx = (event.clientX - rect.left) / rect.width * MAP_W
    const sy = (event.clientY - rect.top) / rect.height * MAP_H
    commitPoint(
      (sx - MAP_PAD) / (MAP_W - MAP_PAD * 2) * room.width_m,
      (MAP_H - MAP_PAD - sy) / (MAP_H - MAP_PAD * 2) * room.depth_m,
    )
  }

  function removePoint(index: number) {
    onChange(points.filter((_, itemIndex) => itemIndex !== index))
    svgRef.current?.focus()
  }

  function sketchKeys(event: KeyboardEvent<SVGSVGElement>) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      const target = cursor ?? center
      commitPoint(target.x_m, target.y_m)
      return
    }
    const delta = arrowDelta(event.key, event.shiftKey ? NUDGE_STEP_SHIFT_M : NUDGE_STEP_M)
    if (!delta) return
    event.preventDefault()
    const base = cursor ?? center
    const next = {
      x_m: Math.round(Math.min(room.width_m, Math.max(0, base.x_m + delta.x)) * 100) / 100,
      y_m: Math.round(Math.min(room.depth_m, Math.max(0, base.y_m + delta.y)) * 100) / 100,
    }
    if (footprintOnly && !pointInPolygon(next.x_m, next.y_m, vertices)) {
      setHint('カーソルは部屋の内側にのみ移動できます')
      return
    }
    setHint('')
    setCursor(next)
  }

  function vertexKeys(event: KeyboardEvent<SVGGElement>, index: number) {
    const point = points[index]
    if (!point) return
    event.stopPropagation()
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault()
      removePoint(index)
      return
    }
    const delta = arrowDelta(event.key, event.shiftKey ? NUDGE_STEP_SHIFT_M : NUDGE_STEP_M)
    if (!delta) return
    event.preventDefault()
    const next = {
      x_m: Math.round(Math.min(room.width_m, Math.max(0, point.x_m + delta.x)) * 100) / 100,
      y_m: Math.round(Math.min(room.depth_m, Math.max(0, point.y_m + delta.y)) * 100) / 100,
    }
    // A vertex already outside the footprint stays nudgeable so bad input can be rescued.
    if (footprintOnly && pointInPolygon(point.x_m, point.y_m, vertices) && !pointInPolygon(next.x_m, next.y_m, vertices)) {
      setHint('頂点は部屋の外へ移動できません')
      return
    }
    setHint('')
    onChange(points.map((item, itemIndex) => (itemIndex === index ? next : item)))
  }

  return <div className={`polygon-sketch ${mode}`}>
    <svg ref={svgRef} viewBox={`0 0 ${MAP_W} ${MAP_H}`} onClick={addPoint} onKeyDown={sketchKeys} tabIndex={0}
      role="application" aria-label="Room polygon editor — click or press Enter to add a vertex, arrow keys move the cursor">
      <path className="sketch-room" d={`M ${boundary.map((point) => `${point.x},${point.y}`).join(' L ')} Z`} />
      {completed.map((area, index) => <path key={index} className="sketch-area completed" d={`M ${area.map((point) => `${point.x},${point.y}`).join(' L ')} Z`} />)}
      {drawn.length > 1 && <path className="sketch-area" d={`M ${drawn.map((point) => `${point.x},${point.y}`).join(' L ')}${drawn.length >= 3 ? ' Z' : ''}`} />}
      {drawn.map((point, index) => <g key={index} className="sketch-vertex" tabIndex={0} role="button"
        aria-label={`Vertex ${index + 1} at ${points[index].x_m} m, ${points[index].y_m} m — arrow keys nudge, Enter removes`}
        onClick={(event) => { event.stopPropagation(); removePoint(index) }}
        onKeyDown={(event) => vertexKeys(event, index)}>
        <circle className="sketch-point" cx={point.x} cy={point.y} r="7" />
        <text className="sketch-index" x={point.x} y={point.y + 3}>{index + 1}</text>
      </g>)}
      {cursorPoint && <g className="sketch-cursor" aria-hidden="true">
        <circle cx={cursorPoint.x} cy={cursorPoint.y} r="10" />
        <line x1={cursorPoint.x - 15} y1={cursorPoint.y} x2={cursorPoint.x + 15} y2={cursorPoint.y} />
        <line x1={cursorPoint.x} y1={cursorPoint.y - 15} x2={cursorPoint.x} y2={cursorPoint.y + 15} />
      </g>}
      <text className="sketch-front" x={MAP_W / 2} y={MAP_H - 8}>FRONT</text>
    </svg>
    <div className="sketch-toolbar">
      <span className={points.length >= 3 ? 'status-pill success' : 'status-pill neutral'}>{points.length} pts</span>
      <button type="button" className="ghost compact" disabled={!points.length} onClick={() => onChange(points.slice(0, -1))}>↶</button>
      <button type="button" className="ghost compact" disabled={!points.length} onClick={() => onChange([])}>Clear</button>
    </div>
    {hint && <p className="sketch-hint">{hint}</p>}
  </div>
}
function WallEdgePicker({ context, selected, onChange }: {
  context: ContextRecord; selected: string; onChange: (edgeId: string) => void
}) {
  const vertices = roomVertices(context)
  const boundary = [...vertices, vertices[0]].map((point) => mapPoint(context, point))
  const edges = wallEdges(context)
  const edgeRefs = useRef<(SVGGElement | null)[]>([])
  const selectedIndex = edges.findIndex((edge) => edge.id === selected)

  function edgeKeys(event: KeyboardEvent<SVGGElement>, index: number) {
    let next = -1
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % edges.length
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + edges.length) % edges.length
    else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onChange(edges[index].id)
      return
    } else return
    event.preventDefault()
    onChange(edges[next].id)
    edgeRefs.current[next]?.focus()
  }

  return <div className="wall-picker">
    <svg viewBox={`0 0 ${MAP_W} ${MAP_H}`} role="radiogroup" aria-label="Wall edge picker">
      <path className="sketch-room muted" d={`M ${boundary.map((point) => `${point.x},${point.y}`).join(' L ')} Z`} />
      {edges.map((edge, index) => {
        const start = mapPoint(context, edge.start)
        const end = mapPoint(context, edge.end)
        const active = edge.id === selected
        return <g key={edge.id} ref={(node) => { edgeRefs.current[index] = node }}
          className={active ? 'wall-edge active' : 'wall-edge'} role="radio" aria-checked={active}
          tabIndex={active || (selectedIndex < 0 && index === 0) ? 0 : -1}
          aria-label={`Wall ${index + 1}: ${edge.start.vertex_id} to ${edge.end.vertex_id}`}
          onClick={() => onChange(edge.id)} onKeyDown={(event) => edgeKeys(event, index)}>
          <line className="wall-hit" x1={start.x} y1={start.y} x2={end.x} y2={end.y} />
          <line className="wall-line" x1={start.x} y1={start.y} x2={end.x} y2={end.y} />
          <circle className="wall-node" cx={(start.x + end.x) / 2} cy={(start.y + end.y) / 2} r="11" />
          <text className="wall-number" x={(start.x + end.x) / 2} y={(start.y + end.y) / 2 + 3}>{index + 1}</text>
        </g>
      })}
      <text className="sketch-front" x={MAP_W / 2} y={MAP_H - 8}>FRONT</text>
    </svg>
    <div className="selected-wall"><span>Wall</span><strong>{selectedIndex < 0 ? '—' : selectedIndex + 1}</strong></div>
  </div>
}

function ruleSummary(rule: Rule): string {
  if (rule.kind === 'allowed_region' || rule.kind === 'exclusion_region') return `${(rule.entity_ids as string[]).join(' · ')} · ${(rule.region as { vertices: Point2D[] }).vertices.length} pts`
  if (rule.kind === 'wall_clearance') return `${(rule.entity_ids as string[]).join(' · ')} · ${rule.min_m ?? '—'}–${rule.max_m ?? '—'} m`
  if (rule.kind === 'axis_range') return `${rule.entity_id} · ${String(rule.axis).toUpperCase()}`
  if (rule.kind === 'movement_budget') return `${rule.entity_id} · ≤ ${rule.max_distance_m} m`
  if (rule.kind === 'pair_distance') return `${rule.entity_a} ⇄ ${rule.entity_b}`
  return `${rule.entity_a} ⟷ ${rule.entity_b}`
}
export function ConstraintBuilder({ projectId, context, onSaved }: {
  projectId: string
  context: ContextRecord
  onSaved: () => void | Promise<void>
}) {
  const ids = useMemo(() => entityIds(context), [context])
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('Placement limits')
  const [rules, setRules] = useState<Rule[]>([])
  const [draft, setDraft] = useState<Draft>(() => blankDraft(context, 0))
  const [profiles, setProfiles] = useState<ProfileDraft>(() => Object.fromEntries(ids.map((id) => [id, { radius: '', margin: '' }])))
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  function updateKind(kind: RuleKind) {
    setDraft({ ...blankDraft(context, rules.length), id: draft.id, kind })
  }

  function addRule() {
    try {
      if (!draft.id.trim()) throw new Error('Constraint IDを入力してください')
      if (rules.some((rule) => rule.constraint_id === draft.id.trim())) throw new Error('Constraint IDが重複しています')
      let rule: Rule
      if (draft.kind === 'allowed_region' || draft.kind === 'exclusion_region') {
        if (!draft.entities.length) throw new Error('対象を選択してください')
        if (draft.polygon.length < 3) throw new Error('部屋図を3点以上クリックしてください')
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_ids: draft.entities, region: { vertices: draft.polygon } }
      } else if (draft.kind === 'wall_clearance') {
        if (!draft.entities.length) throw new Error('対象を選択してください')
        if (!draft.edgeId) throw new Error('基準の壁を選択してください')
        const { min, max } = boundPair(draft.min, draft.max, '壁離隔', true)
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_ids: draft.entities, edge_id: draft.edgeId,
          min_m: min, max_m: max }
      } else if (draft.kind === 'axis_range') {
        const min = numberOrNull(draft.min)
        const max = numberOrNull(draft.max)
        const fixed = numberOrNull(draft.fixed)
        if (fixed === null && min === null && max === null) throw new Error('軸固定: FIXED・MIN・MAXのいずれかを入力してください')
        if (min !== null && max !== null && max < min) throw new Error('軸固定: MAXはMIN以上にしてください')
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_id: draft.entityA, axis: draft.axis,
          min_m: min, max_m: max, fixed_m: fixed }
      } else if (draft.kind === 'movement_budget') {
        const max = numberOrNull(draft.max)
        if (max === null) throw new Error('最大移動量を入力してください')
        if (max < 0) throw new Error('最大移動量は0以上にしてください')
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_id: draft.entityA,
          max_distance_m: max, distance_mode: draft.distanceMode }
      } else if (draft.kind === 'pair_distance') {
        if (draft.entityA === draft.entityB) throw new Error('相互距離は異なる2点を選択してください')
        const { min, max } = boundPair(draft.min, draft.max, '相互距離', true)
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_a: draft.entityA, entity_b: draft.entityB,
          min_m: min, max_m: max, distance_mode: draft.distanceMode,
          distance_reference: draft.distanceReference }
      } else {
        if (draft.entityA === draft.entityB) throw new Error('連動配置は異なる2点を選択してください')
        const tolerance = numberOrNull(draft.tolerance) ?? .001
        if (tolerance < 0) throw new Error('TOLERANCEは0以上にしてください')
        rule = { constraint_id: draft.id.trim(), kind: draft.kind, entity_a: draft.entityA, entity_b: draft.entityB,
          relation: draft.relation, tolerance_m: tolerance,
          ...(draft.relation === 'mirror_x' && draft.mirrorAxis.trim() ? { mirror_axis_x_m: numberOrNull(draft.mirrorAxis) } : {}) }
      }
      setRules([...rules, rule])
      setDraft(blankDraft(context, rules.length + 1))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '制約追加失敗')
    }
  }

  async function save() {
    setSaving(true); setError('')
    try {
      if (!rules.length) throw new Error('制約を1件以上追加してください')
      const entity_profiles = Object.entries(profiles).flatMap(([entity_id, values]) => {
        const radius = numberOrNull(values.radius)
        const margin = numberOrNull(values.margin)
        if (radius === null && margin === null) return []
        return [{ entity_id, footprint_radius_m: radius ?? 0, safety_margin_m: margin ?? 0 }]
      })
      await api(`/api/projects/${projectId}/constraint-sets`, {
        method: 'POST', body: JSON.stringify({ context_id: context.id, name: name.trim() || null, entity_profiles, constraints: rules }),
      })
      setRules([]); setDraft(blankDraft(context, 0)); setOpen(false)
      await onSaved()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'ConstraintSet保存失敗')
    } finally { setSaving(false) }
  }

  if (!open) return <div className="builder-launch">
    <button type="button" className="builder-launch-button" onClick={() => setOpen(true)}><span>＋</span> ConstraintSet</button>
  </div>
  return <div className="constraint-builder visual-builder">
    <div className="builder-header">
      <div><span className="section-kicker">NEW CONSTRAINT SET</span><h3>配置ルール</h3></div>
      <button type="button" className="icon-button" aria-label="閉じる" onClick={() => setOpen(false)}>×</button>
    </div>
    {error && <div className="notice error">{error}</div>}
    <div className="builder-name-row">
      <label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
      <span className="status-pill neutral">R{context.revision_number}</span>
    </div>

    <section className="builder-step">
      <div className="step-head"><span className="step-number">1</span><strong>Footprint</strong></div>
      <div className="profile-grid compact-profiles">{ids.map((id) => <div className="profile-card" key={id}>
        <strong>{id}</strong>
        <label><span>Radius</span><div className="unit-input"><input inputMode="decimal" value={profiles[id]?.radius ?? ''}
          onChange={(event) => setProfiles({ ...profiles, [id]: { ...profiles[id], radius: event.target.value } })} /><b>m</b></div></label>
        <label><span>Margin</span><div className="unit-input"><input inputMode="decimal" value={profiles[id]?.margin ?? ''}
          onChange={(event) => setProfiles({ ...profiles, [id]: { ...profiles[id], margin: event.target.value } })} /><b>m</b></div></label>
      </div>)}</div>
    </section>

    <section className="builder-step">
      <div className="step-head"><span className="step-number">2</span><strong>Rule</strong><span className="step-count">{rules.length} saved</span></div>
      <div className="rule-kind-grid">{(Object.keys(ruleMeta) as RuleKind[]).map((kind) => {
        const meta = ruleMeta[kind]
        return <button type="button" key={kind} className={draft.kind === kind ? 'rule-kind active' : 'rule-kind'} onClick={() => updateKind(kind)}>
          <span className="rule-symbol">{meta.symbol}</span><strong>{meta.label}</strong><small>{meta.short}</small>
        </button>
      })}</div>
      <div className="rule-config-head"><label><span>ID</span><input value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value })} /></label></div>
      {(draft.kind === 'allowed_region' || draft.kind === 'exclusion_region') && <div className="visual-rule-editor">
        <div className="visual-side"><span className="micro-label">TARGET</span><EntityPicker ids={ids} selected={draft.entities} onChange={(entities) => setDraft({ ...draft, entities })} /></div>
        <PolygonSketch context={context} points={draft.polygon} groups={draft.polygonGroups}
          onChange={(polygon) => setDraft({ ...draft, polygon })} onGroupsChange={(polygonGroups) => setDraft({ ...draft, polygonGroups })}
          mode={draft.kind === 'allowed_region' ? 'allowed' : 'exclusion'} />
      </div>}

      {draft.kind === 'wall_clearance' && <div className="visual-rule-editor">
        <div className="visual-side"><span className="micro-label">TARGET</span><EntityPicker ids={ids} selected={draft.entities} onChange={(entities) => setDraft({ ...draft, entities })} />
          <div className="dual-number"><label><span>MIN</span><div className="unit-input"><input inputMode="decimal" value={draft.min} onChange={(event) => setDraft({ ...draft, min: event.target.value })} /><b>m</b></div></label>
          <label><span>MAX</span><div className="unit-input"><input inputMode="decimal" value={draft.max} onChange={(event) => setDraft({ ...draft, max: event.target.value })} /><b>m</b></div></label></div></div>
        <WallEdgePicker context={context} selected={draft.edgeId} onChange={(edgeId) => setDraft({ ...draft, edgeId })} />
      </div>}

      {draft.kind === 'axis_range' && <div className="compact-config">
        <div><span className="micro-label">TARGET</span><EntityPicker ids={ids} selected={[draft.entityA]} single onChange={(value) => setDraft({ ...draft, entityA: value[0] })} /></div>
        <div className="segmented">{(['x','y','z'] as const).map((axis) => <button type="button" key={axis} className={draft.axis === axis ? 'active' : ''} onClick={() => setDraft({ ...draft, axis })}>{axis.toUpperCase()}</button>)}</div>
        <div className="triple-number"><label><span>FIXED</span><div className="unit-input"><input value={draft.fixed} onChange={(event) => setDraft({ ...draft, fixed: event.target.value })} /><b>m</b></div></label>
          <label><span>MIN</span><div className="unit-input"><input value={draft.min} onChange={(event) => setDraft({ ...draft, min: event.target.value })} /><b>m</b></div></label>
          <label><span>MAX</span><div className="unit-input"><input value={draft.max} onChange={(event) => setDraft({ ...draft, max: event.target.value })} /><b>m</b></div></label></div>
      </div>}
      {draft.kind === 'movement_budget' && <div className="compact-config">
        <div><span className="micro-label">TARGET</span><EntityPicker ids={ids} selected={[draft.entityA]} single onChange={(value) => setDraft({ ...draft, entityA: value[0] })} /></div>
        <div className="dual-number"><label><span>MAX MOVE</span><div className="unit-input"><input value={draft.max} onChange={(event) => setDraft({ ...draft, max: event.target.value })} /><b>m</b></div></label>
          <label><span>MODE</span><select value={draft.distanceMode} onChange={(event) => setDraft({ ...draft, distanceMode: event.target.value as Draft['distanceMode'] })}><option value="horizontal_xy">XY</option><option value="3d">3D</option></select></label></div>
      </div>}

      {draft.kind === 'pair_distance' && <div className="compact-config pair-config">
        <div className="pair-picker"><div><span className="micro-label">A</span><EntityPicker ids={ids} selected={[draft.entityA]} single onChange={(value) => setDraft({ ...draft, entityA: value[0] })} /></div>
          <span className="pair-arrow">⇄</span><div><span className="micro-label">B</span><EntityPicker ids={ids} selected={[draft.entityB]} single onChange={(value) => setDraft({ ...draft, entityB: value[0] })} /></div></div>
        <div className="quad-number"><label><span>MIN</span><div className="unit-input"><input value={draft.min} onChange={(event) => setDraft({ ...draft, min: event.target.value })} /><b>m</b></div></label>
          <label><span>MAX</span><div className="unit-input"><input value={draft.max} onChange={(event) => setDraft({ ...draft, max: event.target.value })} /><b>m</b></div></label>
          <label><span>MODE</span><select value={draft.distanceMode} onChange={(event) => setDraft({ ...draft, distanceMode: event.target.value as Draft['distanceMode'] })}><option value="horizontal_xy">XY</option><option value="3d">3D</option></select></label>
          <label><span>FROM</span><select value={draft.distanceReference} onChange={(event) => setDraft({ ...draft, distanceReference: event.target.value as Draft['distanceReference'], distanceMode: event.target.value === 'envelope_clearance' ? 'horizontal_xy' : draft.distanceMode })}><option value="center">Center</option><option value="envelope_clearance">Cabinet</option></select></label></div>
      </div>}

      {draft.kind === 'linked_placement' && <div className="compact-config pair-config">
        <div className="pair-picker"><div><span className="micro-label">A</span><EntityPicker ids={ids} selected={[draft.entityA]} single onChange={(value) => setDraft({ ...draft, entityA: value[0] })} /></div>
          <span className="pair-arrow">⟷</span><div><span className="micro-label">B</span><EntityPicker ids={ids} selected={[draft.entityB]} single onChange={(value) => setDraft({ ...draft, entityB: value[0] })} /></div></div>
        <div className="dual-number"><label><span>RELATION</span><select value={draft.relation} onChange={(event) => setDraft({ ...draft, relation: event.target.value as Draft['relation'] })}><option value="mirror_x">Mirror X</option><option value="equal_y">Equal Y</option><option value="equal_x">Equal X</option><option value="equal_z">Equal Z</option><option value="equal_delta_x">ΔX equal</option><option value="equal_delta_y">ΔY equal</option><option value="equal_delta_z">ΔZ equal</option></select></label>
          <label><span>TOLERANCE</span><div className="unit-input"><input value={draft.tolerance} onChange={(event) => setDraft({ ...draft, tolerance: event.target.value })} /><b>m</b></div></label></div>
        {draft.relation === 'mirror_x' && <label className="single-field"><span>MIRROR X</span><div className="unit-input"><input value={draft.mirrorAxis} onChange={(event) => setDraft({ ...draft, mirrorAxis: event.target.value })} placeholder={`${(context.payload.room.width_m / 2).toFixed(2)}`} /><b>m</b></div></label>}
      </div>}
      <div className="add-rule-row"><button type="button" className="primary-wide" onClick={addRule}>＋ Add rule</button></div>
    </section>

    {rules.length > 0 && <section className="builder-step">
      <div className="step-head"><span className="step-number">3</span><strong>Review</strong><span className="step-count">{rules.length}</span></div>
      <div className="rule-stack visual-stack">{rules.map((rule, index) => <article key={rule.constraint_id}>
        <span className="rule-stack-symbol">{ruleMeta[rule.kind].symbol}</span>
        <div><strong>{ruleMeta[rule.kind].label}</strong><small>{ruleSummary(rule)}</small></div>
        <button type="button" className="icon-button danger-text" aria-label="削除" onClick={() => setRules(rules.filter((_, itemIndex) => itemIndex !== index))}>×</button>
      </article>)}</div>
    </section>}

    <div className="builder-footer visual-footer">
      <span className="immutable-mark">◇ Immutable</span>
      <button type="button" disabled={saving || !rules.length} onClick={() => void save()}>{saving ? 'Saving…' : 'Save ConstraintSet'}</button>
    </div>
  </div>
}
