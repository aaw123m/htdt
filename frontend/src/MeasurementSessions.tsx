import { useEffect, useMemo, useState } from 'react'
import { api, fileToBase64 } from './api'

type Project = { id: string; name: string; created_at: string }
type ContextRecord = { id: string; revision_number: number; created_at: string }
type MeasurementSession = {
  id: string
  project_id: string
  purpose: string | null
  started_at: string | null
  notes: string | null
  created_at: string
  measurement_count: number
}
type Measurement = {
  id: string
  dataset_id: string
  session_id: string | null
  context_id: string
  channel_role: string
  quality_status: string
  repeat_group: string | null
}

type ImportResult = {
  measurement_id: string
  dataset_id: string
  session_id: string | null
  duplicate_asset: boolean
  existing_dataset_count: number
}

function parseList(text: string): string[] {
  return text.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean)
}

function sessionLabel(session: MeasurementSession): string {
  const purpose = session.purpose?.trim() || 'Untitled session'
  const started = session.started_at?.trim() || session.created_at
  return `${purpose} · ${started}`
}

export function MeasurementSessionsPanel() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [contexts, setContexts] = useState<ContextRecord[]>([])
  const [sessions, setSessions] = useState<MeasurementSession[]>([])
  const [measurements, setMeasurements] = useState<Measurement[]>([])
  const [selectedContextId, setSelectedContextId] = useState('')
  const [selectedSessionId, setSelectedSessionId] = useState('')
  const [purpose, setPurpose] = useState('Baseline / placement A/B')
  const [startedAt, setStartedAt] = useState('')
  const [notes, setNotes] = useState('')
  const [measurementFile, setMeasurementFile] = useState<File | null>(null)
  const [channelRole, setChannelRole] = useState('front_left')
  const [sourceSpeakerIds, setSourceSpeakerIds] = useState('FL')
  const [qualityStatus, setQualityStatus] = useState('unknown')
  const [qualityReasons, setQualityReasons] = useState('')
  const [repeatGroup, setRepeatGroup] = useState('')
  const [routingEvidence, setRoutingEvidence] = useState('unknown')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const selectedSession = useMemo(
    () => sessions.find((session) => session.id === selectedSessionId) ?? null,
    [sessions, selectedSessionId],
  )

  async function loadProjects() {
    const rows = await api<Project[]>('/api/projects')
    setProjects(rows)
    setProjectId((current) => current || rows[0]?.id || '')
  }

  async function loadProject(id: string) {
    if (!id) {
      setContexts([])
      setSessions([])
      setMeasurements([])
      setSelectedContextId('')
      setSelectedSessionId('')
      return
    }
    const [contextRows, sessionRows, measurementRows] = await Promise.all([
      api<ContextRecord[]>(`/api/projects/${id}/contexts`),
      api<MeasurementSession[]>(`/api/projects/${id}/sessions`),
      api<Measurement[]>(`/api/projects/${id}/measurements`),
    ])
    setContexts(contextRows)
    setSessions(sessionRows)
    setMeasurements(measurementRows)
    setSelectedContextId((current) => contextRows.some((row) => row.id === current) ? current : contextRows[0]?.id || '')
    setSelectedSessionId((current) => sessionRows.some((row) => row.id === current) ? current : sessionRows[0]?.id || '')
  }

  useEffect(() => {
    void loadProjects().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Project読込失敗'))
  }, [])

  useEffect(() => {
    void loadProject(projectId).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Session読込失敗'))
  }, [projectId])

  async function createSession() {
    try {
      if (!projectId) throw new Error('Projectを選択してください')
      setBusy(true)
      setError('')
      const created = await api<MeasurementSession>(`/api/projects/${projectId}/sessions`, {
        method: 'POST',
        body: JSON.stringify({
          purpose: purpose.trim() || null,
          started_at: startedAt.trim() || null,
          notes: notes.trim() || null,
        }),
      })
      await loadProject(projectId)
      setSelectedSessionId(created.id)
      setNotes('')
      setMessage('Measurement Sessionを作成しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Session作成失敗')
    } finally {
      setBusy(false)
    }
  }

  async function importIntoSession() {
    try {
      if (!projectId || !selectedSessionId || !selectedContextId || !measurementFile) {
        throw new Error('Project、Session、Context、測定ファイルを選択してください')
      }
      setBusy(true)
      setError('')
      const raw_base64 = await fileToBase64(measurementFile)
      const result = await api<ImportResult>(`/api/projects/${projectId}/measurements`, {
        method: 'POST',
        body: JSON.stringify({
          filename: measurementFile.name,
          raw_base64,
          context_id: selectedContextId,
          session_id: selectedSessionId,
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
      await loadProject(projectId)
      setMeasurementFile(null)
      setMessage(result.duplicate_asset
        ? `Sessionへ保存しました。同じRawAssetを使う既存Datasetが${result.existing_dataset_count}件あります`
        : 'Sessionへ測定を原本付きで保存しました')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Session測定保存失敗')
    } finally {
      setBusy(false)
    }
  }

  const sessionMeasurements = selectedSessionId
    ? measurements.filter((measurement) => measurement.session_id === selectedSessionId)
    : []

  return (
    <main className="shell supplemental-shell">
      <section className="panel">
        <div className="section-title">
          <h2>Measurement Sessions</h2>
          <span>schema v3 · explicit grouping</span>
        </div>
        <p className="hint">
          Sessionは「同じ測定作業のまとまり」です。repeat_groupは同条件再測定の系列なので別に保持します。既存測定を推測でSessionへ移しません。
        </p>
        {(message || error) && <div className={error ? 'notice error' : 'notice'}>{error || message}</div>}

        <div className="grid2">
          <label>Project
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
              <option value="">選択</option>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <label>Current Session
            <select value={selectedSessionId} onChange={(event) => setSelectedSessionId(event.target.value)} disabled={!projectId}>
              <option value="">Sessionなし</option>
              {sessions.map((session) => <option key={session.id} value={session.id}>{sessionLabel(session)}</option>)}
            </select>
          </label>
        </div>

        <h3>新しいSession</h3>
        <div className="grid3">
          <label>目的<input value={purpose} onChange={(event) => setPurpose(event.target.value)} /></label>
          <label>開始日時<input type="datetime-local" value={startedAt} onChange={(event) => setStartedAt(event.target.value)} /></label>
          <label>メモ<input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="任意" /></label>
        </div>
        <div className="row action-row">
          <button type="button" disabled={!projectId || busy} onClick={() => void createSession()}>Sessionを作成</button>
        </div>

        {selectedSession && <>
          <div className="preview">
            <strong>{selectedSession.purpose ?? 'Untitled session'}</strong>
            <span>started: {selectedSession.started_at ?? 'unknown'}</span>
            <span>{selectedSession.measurement_count} measurement(s)</span>
            <code>{selectedSession.id.slice(0, 8)}</code>
          </div>

          <h3>このSessionへ測定を保存</h3>
          <div className="grid4">
            <label>Context
              <select value={selectedContextId} onChange={(event) => setSelectedContextId(event.target.value)}>
                <option value="">選択</option>
                {contexts.map((context) => <option key={context.id} value={context.id}>R{context.revision_number}</option>)}
              </select>
            </label>
            <label>Channel role<input value={channelRole} onChange={(event) => setChannelRole(event.target.value)} /></label>
            <label>Source speaker IDs<input value={sourceSpeakerIds} onChange={(event) => setSourceSpeakerIds(event.target.value)} /></label>
            <label>Repeat group<input value={repeatGroup} onChange={(event) => setRepeatGroup(event.target.value)} placeholder="任意" /></label>
          </div>
          <div className="grid4">
            <label>REW text<input type="file" accept=".txt,.csv,.frd" onChange={(event) => setMeasurementFile(event.target.files?.[0] ?? null)} /></label>
            <label>Quality
              <select value={qualityStatus} onChange={(event) => setQualityStatus(event.target.value)}>
                <option value="unknown">unknown</option><option value="usable">usable</option><option value="warning">warning</option><option value="invalid">invalid</option>
              </select>
            </label>
            <label>Routing evidence
              <select value={routingEvidence} onChange={(event) => setRoutingEvidence(event.target.value)}>
                <option value="unknown">unknown</option><option value="verified">verified</option><option value="manual">manual</option><option value="inferred">inferred</option>
              </select>
            </label>
            <label>Quality reasons<input value={qualityReasons} onChange={(event) => setQualityReasons(event.target.value)} placeholder="任意" /></label>
          </div>
          <div className="row action-row">
            <button type="button" disabled={!measurementFile || !selectedContextId || busy} onClick={() => void importIntoSession()}>Session付きで保存</button>
          </div>

          <div className="cards">
            {sessionMeasurements.map((measurement) => <article key={measurement.id}>
              <strong>{measurement.channel_role}</strong>
              <span>quality: {measurement.quality_status}</span>
              <span>repeat: {measurement.repeat_group ?? '—'}</span>
              <code>{measurement.dataset_id.slice(0, 8)}</code>
            </article>)}
          </div>
        </>}
      </section>
    </main>
  )
}
