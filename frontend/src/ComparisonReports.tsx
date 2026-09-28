import { useEffect, useState } from 'react'
import { api } from './api'
import { CopyCode } from './copy'
import { channelRoleLabel, comparisonRoleLabel, qualityStatusLabel } from './labels'

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

  function retryLoad() {
    setError('')
    void api<Project[]>('/api/projects').then(async (items) => {
      setProjects(items)
      const id = projectId || items[0]?.id || ''
      setProjectId((current) => current || items[0]?.id || '')
      if (id) setComparisons(await api<Comparison[]>(`/api/projects/${id}/comparisons`))
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '読込に失敗しました'))
  }

  useEffect(() => {
    void api<Project[]>('/api/projects').then((items) => {
      setProjects(items)
      setProjectId((current) => current || items[0]?.id || '')
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'プロジェクト一覧の読込に失敗しました'))
  }, [])

  useEffect(() => {
    setComparisons([])
    if (!projectId) return
    let cancelled = false
    void api<Comparison[]>(`/api/projects/${projectId}/comparisons`).then((rows) => {
      if (!cancelled) setComparisons(rows)
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '比較履歴の読込に失敗しました')
    })
    return () => { cancelled = true }
  }, [projectId])

  return (
    <div className="shell">
      <section className="panel" id="reports">
        <div className="section-title"><h2>保存済み比較</h2><span>self-contained HTML / JSON</span></div>
        <p className="hint">保存済みComparisonスナップショットからレポートを生成します。現在の配置や測定を再計算しないため、過去の比較根拠をそのまま持ち出せます。</p>
        <label>Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">選択</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" className="ghost compact" onClick={retryLoad}>再読込</button></div>}
        <div className="cards">
          {comparisons.map((comparison) => {
            const a = comparison.result.measurement_a
            const b = comparison.result.measurement_b
            const base = `/api/projects/${projectId}/comparisons/${comparison.id}`
            return <article key={comparison.id}>
              <strong>{comparison.spec.label || '保存済みA/B比較'}</strong>
              <span>{comparisonRoleLabel(comparison.result.comparison_role)} · {new Date(comparison.created_at).toLocaleString()}</span>
              <span>A: {channelRoleLabel(a?.channel_role)} / {qualityStatusLabel(a?.quality_status)} · B: {channelRoleLabel(b?.channel_role)} / {qualityStatusLabel(b?.quality_status)}</span>
              {(comparison.result.interpretation_warnings?.length ?? 0) > 0 && <small>{comparison.result.interpretation_warnings?.join(' / ')}</small>}
              <div className="row">
                <a className="button-link" href={`${base}/report.html`}>HTMLレポート</a>
                <a className="button-link" href={`${base}/report.json`}>JSONスナップショット</a>
              </div>
              <CopyCode value={comparison.id} display={comparison.id.slice(0, 13)} />
            </article>
          })}
          {projectId && comparisons.length === 0 && <article><strong>保存済み比較なし</strong><span>A/B比較を保存するとここからレポートを取得できます。</span></article>}
        </div>
      </section>
    </div>
  )
}
