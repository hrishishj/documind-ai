import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { api, ApiError, streamChat, type FileOut, type Message, type Session, type SessionDetail } from './api'
import { Composer, FilesPanel, MessageList, ProfileModal, Sidebar } from './components'
import { POLL_MS } from './utils'

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [local, setLocal] = useState<Message[]>([]) // optimistic messages for the turn in flight
  const [steps, setSteps] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [viewing, setViewing] = useState<FileOut | null>(null)
  const [dragging, setDragging] = useState(false)
  // False until the first load finishes. That first request is what gives a brand-new browser its identity cookie;
  // a second request sent before it returns would get a *different* identity and its conversation would 404.
  const [ready, setReady] = useState(false)
  const abort = useRef<AbortController | null>(null)

  const loadSessions = useCallback(async () => setSessions(await api.listSessions()), [])
  const loadDetail = useCallback(async (id: string) => setDetail(await api.getSession(id)), [])

  const select = useCallback(async (id: string) => {
    setActiveId(id); setLocal([]); setError(null)
    try {
      await loadDetail(id)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        // the conversation vanished (deleted elsewhere, or this browser's identity changed): resync instead of dead-ending
        setError('That conversation is no longer available.')
        try { setSessions(await api.listSessions()) } catch { /* the banner above already explains */ }
        setActiveId(null); setDetail(null)
      } else setError(errMsg(e))
    }
  }, [loadDetail])

  // Boot: open the most recent conversation, or create the first one. Guarded to run once: React StrictMode
  // (dev) invokes effects twice, and each run would otherwise see "no sessions" and create its own.
  const booted = useRef(false)
  useEffect(() => {
    if (booted.current) return
    booted.current = true
    ;(async () => {
      try {
        let list = await api.listSessions()
        if (!list.length) list = [await api.createSession()]
        setSessions(list)
        await select(list[0].id)
      } catch (e) { setError(errMsg(e)) }
      finally { setReady(true) }
    })()
  }, [select])

  const newChat = async () => {
    try {
      const s = await api.createSession()
      await loadSessions()
      await select(s.id)
    } catch (e) { setError(errMsg(e)) }
  }

  const removeChat = async (id: string) => {
    if (!window.confirm('Delete this conversation and its files?')) return
    try {
      await api.deleteSession(id)
      if (id === activeId) { setActiveId(null); setDetail(null) }  // stop polling/refreshing a conversation that no longer exists
      const list = await api.listSessions()
      if (!list.length) list.push(await api.createSession())
      setSessions(list)
      if (id === activeId) await select(list[0].id)
    } catch (e) { setError(errMsg(e)) }
  }

  const send = async (text: string) => {
    if (!activeId || busy) return
    const id = activeId
    setError(null); setBusy(true); setSteps([])
    setLocal([{ id: 'local-user', role: 'user', content: text, meta: {}, created_at: new Date().toISOString() }])
    abort.current = new AbortController()
    try {
      await streamChat(id, text, (e) => {
        if (e.type === 'node') setSteps((s) => (s[s.length - 1] === e.label ? s : [...s, e.label]))
        else if (e.type === 'error') setError(e.message)
      }, abort.current.signal)
    } catch (e) {
      if ((e as Error).name !== 'AbortError') setError(errMsg(e))
    } finally {
      setBusy(false); setSteps([]); setLocal([])
      abort.current = null
      try { await Promise.all([loadDetail(id), loadSessions()]) } catch (e) { setError(errMsg(e)) }
    }
  }

  const upload = async (files: File[]) => {
    if (!activeId) return
    const id = activeId
    for (const f of files) {
      setUploading((u) => [...u, f.name])
      try {
        await api.upload(id, f)
        const fresh = await api.getSession(id)
        // set both in one synchronous block so React batches them: otherwise the finished file and its
        // "Uploading…" placeholder are briefly on screen together
        setDetail(fresh)
        setUploading((u) => u.filter((n) => n !== f.name))
      } catch (e) {
        setError(`${f.name}: ${errMsg(e)}`)
        setUploading((u) => u.filter((n) => n !== f.name))
      }
    }
  }

  // While any file is still being indexed for search, refresh quietly so its badge flips to "Searchable".
  const waiting = detail?.files.some((f) => f.index_status === 'pending' || f.index_status === 'indexing') ?? false
  useEffect(() => {
    if (!waiting || !activeId || busy) return
    const id = activeId
    const t = setInterval(() => {
      api.getSession(id)
        .then((d) => setDetail((cur) => (cur && cur.id === id ? d : cur)))
        .catch((e) => { if (e instanceof ApiError && e.status === 404) clearInterval(t) })  // gone: never poll it again
    }, POLL_MS)
    return () => clearInterval(t)
  }, [waiting, activeId, busy])

  const messages = [...(detail?.messages ?? []), ...local]

  return (
    <div className="app"
         onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } }}
         onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false) }}
         onDrop={(e) => { e.preventDefault(); setDragging(false); const f = Array.from(e.dataTransfer.files); if (f.length) upload(f) }}>
      <Sidebar sessions={sessions} activeId={activeId} onSelect={select} onNew={newChat} onDelete={removeChat} disabled={busy || !ready} />
      <main className="main">
        {error && (
          <div className="banner error" role="alert">
            <span>{error}</span>
            <button className="icon-btn" aria-label="Dismiss" onClick={() => setError(null)}>×</button>
          </div>
        )}
        {activeId && detail
          ? <MessageList sessionId={activeId} messages={messages} steps={steps} busy={busy} />
          : <div className="messages"><div className="empty"><p>Connecting…</p></div></div>}
        <Composer busy={busy} disabled={!ready || !activeId} onSend={send} onFiles={upload} onStop={() => abort.current?.abort()} />
        {dragging && <div className="drop-overlay">Drop files to attach (DOCX, PPTX, XLSX, PDF, TXT)</div>}
      </main>
      {activeId && detail && (
        <FilesPanel sessionId={activeId} files={detail.files} artifacts={detail.artifacts} uploading={uploading} onAnalysis={setViewing} />
      )}
      {viewing && activeId && <ProfileModal sessionId={activeId} file={viewing} onClose={() => setViewing(null)} />}
    </div>
  )
}

export { ApiError }
