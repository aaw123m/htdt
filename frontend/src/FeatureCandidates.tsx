import { useEffect, useState } from 'react'
import { api } from './api'

type MeasurementOption = {
  dataset_id: string
  channel_role: string
  quality_status: string
  evidence_type: string
}

type Feature = {
  kind: 'peak' | 'dip'
  frequency_hz: number
  level_db: number
  baseline_db: number
  signed_deviation_db: number
  prominence_db: number
}

type CandidateMatch = {
  feature_index: number
  feature_kind: string
  feature_frequency_hz: number
  candidate_kind: 'room_mode' | 'first_order_reflection'
  candidate_frequency_hz: number
  distance_octaves: number
  details: Record<string, unknown>
}

type FeatureResult = {
  classification: string
  eligible_for_candidate_matching: boolean
  warnings: string[]
  feature_detection: {
    algorithm_version: string
    parameters: {
      ppo: number
      requested_low_hz: number
      requested_high_hz: number
      prominence_db: number
      baseline_window_octaves: number
      min_spacing_octaves: number
    }
    features: Feature[]
  }
  candidate_matches: CandidateMatch[]
  match_parameters: {
    tolerance_octaves: number
    sound_speed_m_s: number
    geometry_algorithm_version: string | null
  }
}

type Props = {
  projectId: string
  measurements: MeasurementOption[]
}

function matchLabel(match: CandidateMatch): string {
  if (match.candidate_kind === 'room_mode') {
    const nx = String(match.details.n_x ?? '?')
    const ny = String(match.details.n_y ?? '?')
    const nz = String(match.details.n_z ?? '?')
    const modeClass = String(match.details.mode_class ?? 'mode')
    return `room mode (${nx},${ny},${nz}) · ${modeClass}`
  }
  const speaker = String(match.details.speaker_id ?? '?')
  const surface = String(match.details.surface ?? '?')
  return `reflection ${speaker} · ${surface}`
}

export function FeatureCandidatePanel({ projectId, measurements }: Props) {
  const [datasetId, setDatasetId] = useState('')
  const [lowHz, setLowHz] = useState('20')
  const [highHz, setHighHz] = useState('300')
  const [prominenceDb, setProminenceDb] = useState('3')
  const [result, setResult] = useState<FeatureResult | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!measurements.some((measurement) => measurement.dataset_id === datasetId)) {
      setDatasetId(measurements[0]?.dataset_id ?? '')
      setResult(null)
    }
  }, [measurements, datasetId])

  useEffect(() => {
    setResult(null)
    setError('')
  }, [projectId])

  async function analyze() {
    if (!projectId || !datasetId) return
    try {
      setError('')
      const query = new URLSearchParams({
        low_hz: lowHz,
        high_hz: highHz,
        prominence_db: prominenceDb,
      })
      const payload = await api<FeatureResult>(`/api/projects/${projectId}/datasets/${datasetId}/feature-candidates?${query.toString()}`)
      setResult(payload)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '特徴候補の解析に失敗しました')
    }
  }

  const matchesByFeature = new Map<number, CandidateMatch[]>()
  for (const match of result?.candidate_matches ?? []) {
    const rows = matchesByFeature.get(match.feature_index) ?? []
    rows.push(match)
    matchesByFeature.set(match.feature_index, rows)
  }

  return (
    <section className="panel" id="features">
      <div className="section-title"><h2>Features</h2><span>近接候補 ≠ 原因診断</span></div>
      <p className="hint">保存済みFRを96 PPOへ再標本化し、baselineからのpeak/dipを検出します。room modeや一次反射との周波数近接は、次に確認する候補を絞るための情報であり原因確定ではありません。</p>
      <div className="grid4">
        <label>Dataset<select value={datasetId} onChange={(event) => { setDatasetId(event.target.value); setResult(null) }}><option value="">選択</option>{measurements.map((measurement) => <option key={measurement.dataset_id} value={measurement.dataset_id}>{measurement.channel_role} · {measurement.quality_status} · {measurement.evidence_type} · {measurement.dataset_id.slice(0, 8)}</option>)}</select></label>
        <label>Low Hz<input value={lowHz} onChange={(event) => setLowHz(event.target.value)} /></label>
        <label>High Hz<input value={highHz} onChange={(event) => setHighHz(event.target.value)} /></label>
        <label>Prominence dB<input value={prominenceDb} onChange={(event) => setProminenceDb(event.target.value)} /></label>
      </div>
      <button disabled={!projectId || !datasetId} onClick={() => void analyze()}>特徴と幾何候補を解析</button>
      {error && <div className="notice error">{error}</div>}
      {result && <>
        <div className="analysis-banner">
          <strong>{result.classification}</strong>
          <span>{result.feature_detection.algorithm_version}</span>
          <span>{result.feature_detection.parameters.ppo} PPO</span>
          <span>match ±{result.match_parameters.tolerance_octaves.toFixed(4)} oct</span>
        </div>
        {result.warnings.length > 0 && <div className="preview"><strong>Interpretation warnings</strong>{result.warnings.map((warning) => <em key={warning}>{warning}</em>)}</div>}
        <p className="hint">candidate matching: {result.eligible_for_candidate_matching ? 'enabled' : 'disabled'} · baseline {result.feature_detection.parameters.baseline_window_octaves.toFixed(3)} oct · min spacing {result.feature_detection.parameters.min_spacing_octaves.toFixed(3)} oct</p>
        <div className="analysis-grid wide">
          {result.feature_detection.features.length === 0 && <div><strong>特徴なし</strong><span>現在の帯域・prominence条件ではpeak/dipを検出しませんでした。</span></div>}
          {result.feature_detection.features.map((feature, index) => {
            const matches = matchesByFeature.get(index) ?? []
            return <div key={`${feature.kind}-${feature.frequency_hz}`}>
              <strong>{feature.kind.toUpperCase()} · {feature.frequency_hz.toFixed(1)} Hz · {feature.signed_deviation_db >= 0 ? '+' : ''}{feature.signed_deviation_db.toFixed(2)} dB</strong>
              <span>level {feature.level_db.toFixed(2)} dB · baseline {feature.baseline_db.toFixed(2)} dB · prominence {feature.prominence_db.toFixed(2)} dB</span>
              {matches.length === 0
                ? <span>近接する幾何候補なし</span>
                : matches.map((match) => <span key={`${match.candidate_kind}-${match.candidate_frequency_hz}-${match.distance_octaves}`}>{matchLabel(match)} · {match.candidate_frequency_hz.toFixed(1)} Hz · Δ {match.distance_octaves.toFixed(4)} oct</span>)}
            </div>
          })}
        </div>
      </>}
    </section>
  )
}
