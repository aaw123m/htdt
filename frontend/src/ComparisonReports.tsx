import { useEffect, useState } from 'react'
import { api } from './api'

type Project = { id: string; name: string }
type Comparison = {
  id: string
  created_at: string
  spec: { label?: string | null }
  result: {
    comparison_role?: string
    measurement_a?: { channel_role?: string; quality_status?: string }
    measurement_b?: { channel_role?: string; quality_status?: string }
    interpretation_warnings?: string[]
  }
}

export function ComparisonReportPanel() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [comparisons, setComparisons] = useState<Comparison[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    void api<Project[]>('/api/projects').then((items) => {
      setProjects(items)
      setProjectId((current) => current || items[0]?.id || '')
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'プロジェクト一覧の読込に失敗しました'))
  }, [])

  useEffect(() => {
    setComparisons([])
    if (!projectId) return
    void api<Comparison[]>(`/api/projects/${projectId}/comparisons`).then(setComparisons)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '比較履歴の読込に失敗しました'))
  }, [projectId])

  return (
    <main className="shell">
      <section className="panel">
        <div className="section-title"><h2>Saved Comparisons</h2><span>self-contained HTML / JSON</span></div>
        <p className="hint">保存済みComparisonスナップショットからレポートを生成します。現在の配置や測定を再計算しないため、過去の比較根拠をそのまま持ち出せます。</p>
        <label>Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">選択</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        {error && <div className="notice error">{error}</div>}
        <div className="cards">
          {comparisons.map((comparison) => {
            const a = comparison.result.measurement_a
            const b = comparison.result.measurement_b
            const base = `/api/projects/${projectId}/comparisons/${comparison.id}`
            return <article key={comparison.id}>
              <strong>{comparison.spec.label || 'Saved A/B comparison'}</strong>
              <span>{comparison.result.comparison_role ?? 'legacy'} · {new Date(comparison.created_at).toLocaleString()}</span>
              <span>A: {a?.channel_role ?? '—'} / {a?.quality_status ?? '—'} · B: {b?.channel_role ?? '—'} / {b?.quality_status ?? '—'}</span>
              {(comparison.result.interpretation_warnings?.length ?? 0) > 0 && <small>{comparison.result.interpretation_warnings?.join(' / ')}</small>}
              <div className="row">
                <a className="button-link" href={`${base}/report.html`}>HTML report</a>
                <a className="button-link" href={`${base}/report.json`}>JSON snapshot</a>
              </div>
              <code>{comparison.id}</code>
            </article>
          })}
          {projectId && comparisons.length === 0 && <article><strong>保存済み比較なし</strong><span>A/B比較を保存するとここからレポートを取得できます。</span></article>}
        </div>
      </section>
    </main>
  )
}
