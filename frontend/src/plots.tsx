import { useEffect, useMemo, useRef } from 'react'
import Plotly, { type Data, type Layout } from 'plotly.js-dist-min'

import { channelRoleLabel } from './labels'

export type Speaker = {
  speaker_id: string
  role: string
  model?: string | null
  position?: { x_m: number; y_m: number; z_m: number } | null
}

export type ContextPayload = {
  room: {
    width_m: number
    depth_m: number
    height_m: number
    geometry_kind?: 'rectangular' | 'reference_box' | 'polygon_prism'
    footprint_vertices?: { vertex_id: string; x_m: number; y_m: number }[] | null
  }
  speakers: Speaker[]
  measurement_point: {
    point_id?: string
    label: string
    position: { x_m: number; y_m: number; z_m: number }
    aim_xyz?: [number, number, number] | null
  }
  microphone?: {
    manufacturer: string
    model: string
    serial?: string | null
    connection?: string
    sample_rate_hz?: number | null
    calibration_profile?: '0deg' | '90deg' | 'unknown'
    calibration_filename?: string | null
  } | null
}

export type ComparisonResult = {
  grid_hz: number[]
  a_db: number[]
  b_db: number[]
  difference_db: number[]
  mean_difference_db: number | null
  rms_difference_db: number | null
  level_offset_db: number | null
  shape_rms_db: number | null
  valid_points: number
  total_grid_points: number
  actual_band_hz: [number, number]
}

function usePlot(data: Data[], layout: Partial<Layout>) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = ref.current
    if (!node) return
    void Plotly.react(node, data, layout, {
      responsive: true,
      displaylogo: false,
      scrollZoom: true,
    }).catch((reason: unknown) => console.error('Plot render failed', reason))
    return () => {
      Plotly.purge(node)
    }
  }, [data, layout])
  return ref
}

function prefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches === true
}

function themedLayout(): Partial<Layout> {
  if (!prefersDark()) return {}
  return {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b9b9bf' },
  }
}

export function FrequencyPlot({ result }: { result: ComparisonResult }) {
  const data = useMemo<Data[]>(() => [
    { type: 'scatter', mode: 'lines', name: 'A', x: result.grid_hz, y: result.a_db },
    { type: 'scatter', mode: 'lines', name: 'B', x: result.grid_hz, y: result.b_db },
  ], [result])
  const layout = useMemo<Partial<Layout>>(() => ({
    autosize: true,
    height: 420,
    margin: { l: 60, r: 20, t: 30, b: 55 },
    xaxis: { type: 'log', title: { text: '周波数 (Hz)' } },
    yaxis: { title: { text: 'レベル (dB)' } },
    legend: { orientation: 'h' },
    ...themedLayout(),
  }), [])
  const ref = usePlot(data, layout)
  return <div ref={ref} className="plot" role="img" aria-label="A/B周波数特性プロット" />
}

export function DifferencePlot({ result }: { result: ComparisonResult }) {
  const data = useMemo<Data[]>(() => [
    { type: 'scatter', mode: 'lines', name: 'A − B', x: result.grid_hz, y: result.difference_db },
  ], [result])
  const layout = useMemo<Partial<Layout>>(() => ({
    autosize: true,
    height: 300,
    margin: { l: 60, r: 20, t: 30, b: 55 },
    xaxis: { type: 'log', title: { text: '周波数 (Hz)' } },
    yaxis: { title: { text: 'レベル差 (dB)' }, zeroline: true, zerolinewidth: 2 },
    showlegend: false,
    ...themedLayout(),
  }), [])
  const ref = usePlot(data, layout)
  return <div ref={ref} className="plot" role="img" aria-label="A−B差分プロット" />
}

export function RoomPlot({ context }: { context: ContextPayload }) {
  const { traces, layout } = useMemo(() => {
    const polygonVertices = context.room.geometry_kind === 'polygon_prism' && context.room.footprint_vertices?.length
      ? context.room.footprint_vertices
      : [
          { vertex_id: 'front_left', x_m: 0, y_m: 0 },
          { vertex_id: 'front_right', x_m: context.room.width_m, y_m: 0 },
          { vertex_id: 'rear_right', x_m: context.room.width_m, y_m: context.room.depth_m },
          { vertex_id: 'rear_left', x_m: 0, y_m: context.room.depth_m },
        ]
    const closedBoundary = [...polygonVertices, polygonVertices[0]]
    const boundaryName = context.room.geometry_kind === 'reference_box' ? '参照ボックス' : '部屋境界'
    const boundaryTraces: Data[] = [0, context.room.height_m].map((height, index) => ({
      type: 'scatter3d', mode: 'lines', name: boundaryName,
      x: closedBoundary.map((point) => point.x_m),
      y: closedBoundary.map(() => height),
      z: closedBoundary.map((point) => point.y_m),
      showlegend: index === 0,
    }))
    polygonVertices.forEach((point) => boundaryTraces.push({
      type: 'scatter3d', mode: 'lines', name: boundaryName,
      x: [point.x_m, point.x_m],
      y: [0, context.room.height_m],
      z: [point.y_m, point.y_m],
      showlegend: false,
    }))
    const positioned = context.speakers.flatMap((speaker) => speaker.position ? [{ speaker, position: speaker.position }] : [])
    const speakerTrace: Data = {
      type: 'scatter3d',
      mode: 'markers+text',
      name: 'スピーカー',
      x: positioned.map((item) => item.position.x_m),
      y: positioned.map((item) => item.position.z_m),
      z: positioned.map((item) => item.position.y_m),
      text: positioned.map((item) => channelRoleLabel(item.speaker.role)),
      textposition: 'top center',
      marker: { size: 6 },
    }
    const mlp = context.measurement_point.position
    const listenerTrace: Data = {
      type: 'scatter3d',
      mode: 'markers+text',
      name: context.measurement_point.label,
      x: [mlp.x_m],
      y: [mlp.z_m],
      z: [mlp.y_m],
      text: [context.measurement_point.label],
      textposition: 'top center',
      marker: { size: 7 },
    }
    const layoutValue: Partial<Layout> = {
      autosize: true,
      height: 430,
      margin: { l: 0, r: 0, t: 20, b: 0 },
      scene: {
        xaxis: { title: { text: 'X 右 (m)' }, range: [0, context.room.width_m] },
        yaxis: { title: { text: 'Z 上 (m)' }, range: [0, context.room.height_m] },
        zaxis: { title: { text: 'Y 後方 (m)' }, range: [0, context.room.depth_m] },
        aspectmode: 'data',
      },
      legend: { orientation: 'h' },
      ...themedLayout(),
    }
    return { traces: [...boundaryTraces, speakerTrace, listenerTrace], layout: layoutValue }
  }, [context])
  const ref = usePlot(traces, layout)
  return <div ref={ref} className="plot" role="img" aria-label="部屋の空間プロット" />
}
