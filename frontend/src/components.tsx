import { useEffect, useRef, useState } from 'react'
import { api, type ArtifactOut, type FileOut, type Message, type Session } from './api'
import { RichText } from './RichText'

export const ACCEPT = '.docx,.pptx,.potx,.xlsx,.xls,.pdf,.txt,.md'

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// ------------------------------------------------------------------ sidebar
export function Sidebar(props: {
  sessions: Session[]
  activeId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
  disabled: boolean
}) {
  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="brand">DocuMind <span>AI</span></div>
      <button className="btn primary" onClick={props.onNew} disabled={props.disabled}>+ New chat</button>
      <ul className="session-list">
        {props.sessions.map((s) => (
          <li key={s.id} className={s.id === props.activeId ? 'active' : ''}>
            <button className="session-btn" onClick={() => props.onSelect(s.id)} disabled={props.disabled} aria-current={s.id === props.activeId}>
              {s.title}
            </button>
            <button className="icon-btn" aria-label={`Delete conversation ${s.title}`} onClick={() => props.onDelete(s.id)} disabled={props.disabled}>×</button>
          </li>
        ))}
      </ul>
    </nav>
  )
}

// ------------------------------------------------------------------ messages
function ArtifactCard({ sessionId, m }: { sessionId: string; m: Message }) {
  const a = m.meta.artifact
  if (!a) return null
  const r = m.meta.render
  return (
    <div className="artifact-card">
      <div className="artifact-head">
        <span className={`badge ${a.format}`}>{a.format.toUpperCase()}</span>
        <strong>{a.name}</strong>
        <span className="muted">v{a.version}</span>
        <a className="btn small" href={api.artifactUrl(sessionId, a.id, a.version)} download>Download</a>
      </div>
      {r && (
        <div className={`preserve ${r.preserved ? 'ok' : 'warn'}`}>
          {r.preserved ? '✓ Original layout and formatting preserved' : '⚠ Differences from the original design: ' + r.violations.join('; ')}
        </div>
      )}
      {r && r.warnings.length > 0 && (
        <ul className="notes">{r.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
      )}
    </div>
  )
}

export function MessageList(props: {
  sessionId: string
  messages: Message[]
  steps: string[]
  busy: boolean
}) {
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView?.({ block: 'end' }) }, [props.messages.length, props.steps.length])
  return (
    <div className="messages" role="log" aria-live="polite" aria-label="Conversation">
      {props.messages.length === 0 && !props.busy && (
        <div className="empty">
          <h2>Edit Office documents by chatting</h2>
          <p>Attach a Word, PowerPoint or Excel file, then ask for a change. Your template's fonts, colours, logos and layout are kept.</p>
          <ul>
            <li>“Add a slide titled <em>Agenda</em> after slide 2 with three bullets”</li>
            <li>“Change the report title and add a row for West to the table”</li>
            <li>“Append a row to the Sales sheet”</li>
          </ul>
        </div>
      )}
      {props.messages.map((m) => (
        <div key={m.id} className={`msg ${m.role}`}>
          <div className="who">{m.role === 'user' ? 'You' : 'DocuMind'}</div>
          <div className="bubble">
            <RichText text={m.content} />
            {m.role === 'assistant' && <ArtifactCard sessionId={props.sessionId} m={m} />}
          </div>
        </div>
      ))}
      {props.busy && (
        <div className="msg assistant">
          <div className="who">DocuMind</div>
          <div className="bubble progress" aria-label="Working">
            <ol>
              {props.steps.map((s, i) => (
                <li key={i} className={i === props.steps.length - 1 ? 'current' : 'done'}>{s}</li>
              ))}
              {props.steps.length === 0 && <li className="current">Starting…</li>}
            </ol>
          </div>
        </div>
      )}
      <div ref={end} />
    </div>
  )
}

// ------------------------------------------------------------------ composer
export function Composer(props: { busy: boolean; onSend: (text: string) => void; onFiles: (files: File[]) => void; onStop: () => void }) {
  const [text, setText] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const submit = () => {
    const t = text.trim()
    if (!t || props.busy) return
    props.onSend(t)
    setText('')
  }
  return (
    <form className="composer" onSubmit={(e) => { e.preventDefault(); submit() }}>
      <input ref={fileInput} type="file" multiple hidden accept={ACCEPT} data-testid="file-input"
             onChange={(e) => { const f = Array.from(e.target.files ?? []); if (f.length) props.onFiles(f); e.target.value = '' }} />
      <button type="button" className="icon-btn attach" aria-label="Attach a file" onClick={() => fileInput.current?.click()} disabled={props.busy}>📎</button>
      <textarea
        aria-label="Message"
        placeholder="Ask for a change to your document…"
        value={text}
        rows={1}
        maxLength={4000}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() } }}
      />
      {props.busy
        ? <button type="button" className="btn" onClick={props.onStop}>Stop</button>
        : <button type="submit" className="btn primary" disabled={!text.trim()}>Send</button>}
    </form>
  )
}

// ------------------------------------------------------------------ side panel
export function FilesPanel(props: {
  sessionId: string
  files: FileOut[]
  artifacts: ArtifactOut[]
  uploading: string[]
  onAnalysis: (f: FileOut) => void
}) {
  const analysable = (k: string) => ['docx', 'pptx', 'xlsx'].includes(k)
  return (
    <aside className="panel" aria-label="Files">
      <h3>Your files</h3>
      {props.files.length === 0 && props.uploading.length === 0 && <p className="muted">Nothing attached yet. Drop a file anywhere or use 📎.</p>}
      <ul className="files">
        {props.uploading.map((n) => <li key={n} className="uploading">Uploading {n}…</li>)}
        {props.files.map((f) => (
          <li key={f.id}>
            <div className="file-row">
              <span className={`badge ${f.kind}`}>{f.kind.toUpperCase()}</span>
              <span className="fname" title={f.original_name}>{f.original_name}</span>
              <span className="muted">{fmtSize(f.size_bytes)}</span>
            </div>
            <div className="file-actions">
              {analysable(f.kind) && <button className="btn small" onClick={() => props.onAnalysis(f)}>Template analysis</button>}
              <a className="btn small" href={api.fileUrl(props.sessionId, f.id)} download>Download</a>
            </div>
          </li>
        ))}
      </ul>
      <h3>Generated documents</h3>
      {props.artifacts.length === 0 && <p className="muted">Edits you request appear here as versions.</p>}
      <ul className="files">
        {props.artifacts.map((a) => (
          <li key={a.id}>
            <div className="file-row">
              <span className={`badge ${a.kind}`}>{a.kind.toUpperCase()}</span>
              <span className="fname" title={a.name}>{a.name}</span>
            </div>
            <ul className="versions">
              {[...a.versions].reverse().map((v) => (
                <li key={v.version}>
                  <a href={api.artifactUrl(props.sessionId, a.id, v.version)} download>v{v.version}</a>
                  <span className="muted"> — {v.summary || 'edit'}</span>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </aside>
  )
}

// ------------------------------------------------------------------ template analysis modal
function KV({ k, children }: { k: string; children: React.ReactNode }) {
  return <><dt>{k}</dt><dd>{children}</dd></>
}

const inch = (v?: number) => (v == null ? '–' : `${v}"`)

function ProfileBody({ p }: { p: Record<string, any> }) {
  if (p.format === 'docx') {
    const s = p.sections?.[0] ?? {}
    const m = s.margins_in ?? {}
    const c = p.formatting_census ?? {}
    return (
      <dl>
        <KV k="Page">{inch(s.page_width_in)} × {inch(s.page_height_in)}, {String(s.orientation ?? '').toLowerCase()}</KV>
        <KV k="Margins">top {inch(m.top)} · bottom {inch(m.bottom)} · left {inch(m.left)} · right {inch(m.right)}</KV>
        <KV k="Dominant fonts">{(c.fonts ?? []).slice(0, 3).map((f: any) => `${f.value} ${(f.share * 100).toFixed(0)}%`).join(', ') || '–'}</KV>
        <KV k="Font sizes">{(c.sizes_pt ?? []).slice(0, 3).map((f: any) => `${f.value}pt`).join(', ') || '–'}</KV>
        <KV k="Header / footer">{s.header?.present === false ? 'none' : 'present'} / {s.footer?.present === false ? 'none' : 'present'}{s.footer?.has_page_number ? ' (page numbers)' : ''}</KV>
        <KV k="Counts">{p.counts?.paragraphs} paragraphs · {p.counts?.tables} tables · {p.counts?.images} images</KV>
        <KV k="Headings">{(p.outline ?? []).slice(0, 8).map((o: any) => `${'  '.repeat(o.level)}${o.text}`).join(' | ') || 'none'}</KV>
        <KV k="Images">{(p.images ?? []).slice(0, 6).map((i: any, n: number) => (
          <div key={n}>{i.placement} {i.width_in}"×{i.height_in}"{i.pos_h ? ` at (${i.pos_h.offset_in ?? i.pos_h.align}", ${i.pos_v?.offset_in ?? i.pos_v?.align}") from ${i.pos_h.relative_from}` : ''}{i.location ? ` in ${i.location}` : ''}</div>
        ))}{(p.images ?? []).length === 0 && 'none'}</KV>
      </dl>
    )
  }
  if (p.format === 'pptx') {
    const m = p.masters?.[0] ?? {}
    return (
      <dl>
        <KV k="Slide size">{inch(p.slide_size?.width_in)} × {inch(p.slide_size?.height_in)}</KV>
        <KV k="Theme fonts">{m.theme?.fonts?.major} / {m.theme?.fonts?.minor}</KV>
        <KV k="Counts">{p.counts?.slides} slides · {p.counts?.layouts} layouts · {p.counts?.images} images</KV>
        <KV k="Master logos / decor">{(m.decor ?? []).map((d: any, i: number) => <div key={i}>{d.kind} “{d.name}” at ({d.x_in}", {d.y_in}") {d.w_in}"×{d.h_in}"</div>)}{(m.decor ?? []).length === 0 && 'none'}</KV>
        <KV k="Layouts">{(m.layouts ?? []).map((l: any) => `${l.name} (${l.used_by_slides})`).join(', ')}</KV>
        <KV k="Slides">{(p.slides ?? []).slice(0, 12).map((s: any) => <div key={s.index}>{s.index}. {s.title || '(no title)'} <span className="muted">[{s.layout}]</span></div>)}</KV>
      </dl>
    )
  }
  return (
    <dl>
      <KV k="Source">{p.source_format}{p.conversion ? ' (converted from legacy .xls)' : ''}</KV>
      <KV k="Sheets">{p.counts?.sheets}</KV>
      {(p.sheets ?? []).slice(0, 8).map((s: any) => (
        <KV key={s.name} k={s.name}>
          {s.dimensions} · {s.merged_count ?? 0} merged · freeze {s.freeze_panes ?? 'none'} · {Object.keys(s.column_widths ?? {}).length} custom column widths
          {s.state && s.state !== 'visible' ? ` · ${s.state}` : ''}
        </KV>
      ))}
      {(p.sheets ?? []).length > 8 && <KV k="…">{p.sheets.length - 8} more sheets</KV>}
    </dl>
  )
}

export function ProfileModal({ sessionId, file, onClose }: { sessionId: string; file: FileOut; onClose: () => void }) {
  const [profile, setProfile] = useState<Record<string, any> | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api.profile(sessionId, file.id).then((p) => live && setProfile(p)).catch((e) => live && setError(e.message))
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => { live = false; window.removeEventListener('keydown', onKey) }
  }, [sessionId, file.id, onClose])
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={`Template analysis of ${file.original_name}`} onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Template analysis</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <p className="muted">{file.original_name}</p>
        {error && <div className="banner error">{error}</div>}
        {!profile && !error && <p>Analysing…</p>}
        {profile && (
          <>
            <ProfileBody p={profile} />
            {(profile.warnings ?? []).map((w: string, i: number) => <div key={i} className="banner warn">{w}</div>)}
            <details>
              <summary>Raw analysis (JSON)</summary>
              <pre>{JSON.stringify(profile, null, 2).slice(0, 60000)}</pre>
            </details>
          </>
        )}
      </div>
    </div>
  )
}
