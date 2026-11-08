import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const enc = new TextEncoder()
const ev = (event: string, data: unknown) => enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

vi.mock('./utils', async (orig) => ({ ...(await orig<typeof import('./utils')>()), POLL_MS: 25 }))  // fast polling for tests

type Srv = {
  sessions: any[]
  detail: any
  chat: (body: any) => Response | Promise<Response>
  upload: (name: string) => Response
  profile?: any
}

function fakeServer(): Srv {
  const detail = { id: 's1', title: 'New chat', created_at: 't', updated_at: 't', messages: [] as any[], files: [] as any[], artifacts: [] as any[] }
  const srv: Srv = {
    sessions: [{ id: 's1', title: 'New chat', created_at: 't', updated_at: 't' }],
    detail,
    chat: () => json({ error: { code: 'x', message: 'unset' } }, 500),
    upload: (name) => {
      if (name.endsWith('.exe')) return json({ error: { code: 'unsafe_file', message: "file type '.exe' is not supported" } }, 400)
      const f = { id: 'f1', original_name: name, kind: 'docx', size_bytes: 2048, created_at: 't' }
      detail.files.push(f)
      return json({ file: f, analysis: { format: 'docx', counts: {}, warnings: [] } }, 201)
    },
  }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = url.replace('/api', ''), m = init?.method ?? 'GET'
    if (u === '/sessions' && m === 'GET') return json(srv.sessions)
    if (u === '/sessions' && m === 'POST') {
      const s = { id: 's1', title: 'New chat', created_at: 't', updated_at: 't' }
      srv.sessions = [s]
      return json(s, 201)
    }
    if (u === '/sessions/s1' && m === 'GET') return json(detail)
    if (u === '/sessions/s1/files' && m === 'POST') return srv.upload(((init!.body as FormData).get('file') as File).name)
    if (u === '/sessions/s1/chat') return srv.chat(JSON.parse(init!.body as string))
    if (u === '/sessions/s1/files/f1/profile') return json(srv.profile ?? { format: 'docx', counts: { paragraphs: 3, tables: 1, images: 2 },
      sections: [{ page_width_in: 8.5, page_height_in: 11, orientation: 'PORTRAIT', margins_in: { top: 1, bottom: 1, left: 1.25, right: 0.75 }, header: { present: false }, footer: {} }],
      formatting_census: { fonts: [{ value: 'Arial', share: 0.88 }], sizes_pt: [{ value: 10, share: 0.8 }] }, outline: [{ level: 1, text: 'Quarterly Report' }], images: [], warnings: ['a warning'] })
    return json({ error: { code: 'not_found', message: `no route ${m} ${u}` } }, 404)
  }))
  return srv
}

let srv: Srv
beforeEach(() => { srv = fakeServer() })
afterEach(() => vi.restoreAllMocks())

describe('App: search indexing and citations', () => {
  const fileRow = (over = {}) => ({ id: 'f1', original_name: 'report.docx', kind: 'docx', size_bytes: 10, created_at: 't',
    index_status: 'pending', chunk_count: 0, index_error: '', ...over })

  it('refreshes a "preparing" badge by itself, then stops polling once the file is searchable', async () => {
    srv.detail.files.push(fileRow())
    render(<App />)
    expect(await screen.findByText('Preparing for search…')).toBeInTheDocument()
    setTimeout(() => Object.assign(srv.detail.files[0], { index_status: 'ready', chunk_count: 7 }), 60)  // the server finishes in the background
    expect(await screen.findByText('Searchable · 7 passages')).toBeInTheDocument()
    expect(screen.queryByText('Preparing for search…')).toBeNull()
    const sessionCalls = () => (globalThis.fetch as any).mock.calls.filter((c: any[]) => String(c[0]).endsWith('/sessions/s1')).length
    const before = sessionCalls()
    await new Promise((r) => setTimeout(r, 120))
    expect(sessionCalls()).toBe(before)  // no wasted requests once nothing is pending
  })

  it('stops polling the moment its conversation is deleted (no requests for a session that no longer exists)', async () => {
    srv.detail.files.push(fileRow({ index_status: 'indexing' }))   // keeps polling active
    let deleted = false
    const base = globalThis.fetch as any
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => {
      const m = i?.method ?? 'GET'
      if (deleted) calls.push(`${m} ${u}`)
      if (m === 'DELETE' && u === '/api/sessions/s1') { deleted = true; return new Response(null, { status: 204 }) }
      if (deleted && u === '/api/sessions' && m === 'GET') return json([])      // nothing left, so the app creates a new one
      if (deleted && u === '/api/sessions' && m === 'POST') return json({ id: 's2', title: 'New chat', created_at: 't', updated_at: 't' }, 201)
      if (deleted && u === '/api/sessions/s2') return json({ id: 's2', title: 'New chat', created_at: 't', updated_at: 't', messages: [], files: [], artifacts: [] })
      if (deleted && u === '/api/sessions/s1') return json({ error: { code: 'not_found', message: 'session not found' } }, 404)
      return base(u, i)
    }))
    vi.stubGlobal('confirm', () => true)   // this DOM environment has no window.confirm to spy on
    render(<App />)
    await screen.findByText('Preparing for search…')
    await userEvent.hover(screen.getByRole('button', { name: 'New chat' }))
    await userEvent.click(screen.getByRole('button', { name: /Delete conversation/ }))
    await waitFor(() => expect(deleted).toBe(true))
    const stray = () => calls.filter((c) => c === 'GET /api/sessions/s1').length
    await new Promise((r) => setTimeout(r, 120))
    const early = stray()
    await new Promise((r) => setTimeout(r, 200))                     // many more poll intervals (mocked at 25 ms)
    // one poll that was already in flight when the DELETE landed may still arrive; after that, polling must have stopped
    expect(early).toBeLessThanOrEqual(1)
    expect(stray()).toBe(early)
  })

  it('stops polling for good if the server says the conversation is gone', async () => {
    srv.detail.files.push(fileRow({ index_status: 'indexing' }))
    const base = globalThis.fetch as any
    let gone = false, hits = 0
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => {
      if (gone && u === '/api/sessions/s1') { hits++; return json({ error: { code: 'not_found', message: 'session not found' } }, 404) }
      return base(u, i)
    }))
    render(<App />)
    await screen.findByText('Preparing for search…')
    gone = true                                                       // e.g. deleted from another tab
    await new Promise((r) => setTimeout(r, 250))
    expect(hits).toBe(1)                                              // one 404, then the timer is cleared
  })

  it('does not poll at all when every file is already settled', async () => {
    srv.detail.files.push(fileRow({ index_status: 'ready', chunk_count: 2 }))
    render(<App />)
    await screen.findByText('Searchable · 2 passages')
    const calls = () => (globalThis.fetch as any).mock.calls.length
    const before = calls()
    await new Promise((r) => setTimeout(r, 120))
    expect(calls()).toBe(before)
  })

  it('shows stored citations on a reloaded conversation', async () => {
    srv.detail.messages.push({ id: 'a1', role: 'assistant', content: 'Revenue was 331,839 [1].', meta: {}, created_at: 't',
      citations: [{ ref: 1, source_type: 'document', title: 'report.docx', locator: 'Table 3', snippet: 'Total revenue | 331839', file_id: 'f1', url: null }] })
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: /^Source 1: report.docx, Table 3/ }))
    expect(screen.getByText('Total revenue | 331839', { selector: 'blockquote' })).toBeInTheDocument()
  })
})

describe('App: first-visit identity race', () => {
  it('keeps every control disabled until the first load finishes (that request establishes the cookie)', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const base = globalThis.fetch as any
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => {
      if (u === '/api/sessions' && (i?.method ?? 'GET') === 'GET') await gate   // the slow first request
      return base(u, i)
    }))
    render(<App />)
    expect(screen.getByRole('button', { name: '+ New chat' })).toBeDisabled()
    expect(screen.getByLabelText('Message')).toBeDisabled()
    expect(screen.getByLabelText('Attach a file')).toBeDisabled()
    const sent = (globalThis.fetch as any).mock.calls.map((c: any[]) => `${c[1]?.method ?? 'GET'} ${c[0]}`)
    expect(sent).toEqual(['GET /api/sessions'])           // nothing else may be in flight yet
    release()
    await waitFor(() => expect(screen.getByRole('button', { name: '+ New chat' })).toBeEnabled())
    expect(screen.getByLabelText('Message')).toBeEnabled()
  })

  it('recovers instead of dead-ending when the opened conversation no longer exists', async () => {
    const base = globalThis.fetch as any
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => {
      if (u === '/api/sessions/s1') return json({ error: { code: 'not_found', message: 'session not found' } }, 404)
      return base(u, i)
    }))
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('That conversation is no longer available.')
    expect(screen.getByLabelText('Message')).toBeDisabled()               // nothing to talk to until the user picks one
    await userEvent.click(screen.getByRole('button', { name: '+ New chat' }))
    expect(screen.getByRole('button', { name: '+ New chat' })).toBeEnabled()  // the UI is still usable
  })
})

describe('App', () => {
  it('boots into the existing conversation and shows the empty state', async () => {
    render(<App />)
    expect(await screen.findByText(/Edit Office documents by chatting/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New chat' })).toHaveAttribute('aria-current', 'true')
  })

  it('creates the first conversation when there are none', async () => {
    const calls: string[] = []
    const base = (globalThis.fetch as any)
    srv.sessions = []
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => { calls.push(`${i?.method ?? 'GET'} ${u}`); return base(u, i) }))
    render(<App />)
    await screen.findByText(/Edit Office documents by chatting/)
    expect(calls).toContain('POST /api/sessions')
  })

  it('creates exactly ONE conversation on first load even under StrictMode double effects', async () => {
    const posts: string[] = []
    const base = globalThis.fetch as any
    srv.sessions = []
    vi.stubGlobal('fetch', vi.fn(async (u: string, i?: RequestInit) => { if (i?.method === 'POST' && u === '/api/sessions') posts.push(u); return base(u, i) }))
    render(<StrictMode><App /></StrictMode>)
    await screen.findByText(/Edit Office documents by chatting/)
    expect(posts).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'New chat' })).toHaveLength(1)
  })

  it('uploads a file and lists it with its analysis action', async () => {
    render(<App />)
    await screen.findByText(/Edit Office documents/)
    await userEvent.upload(screen.getByTestId('file-input'), new File(['x'], 'report.docx'))
    const panel = screen.getByLabelText('Files')
    expect(await within(panel).findByText('report.docx')).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Template analysis' })).toBeInTheDocument()
    expect(within(panel).getByText('2 KB')).toBeInTheDocument()
  })

  it('shows the server\'s reason when a dropped file is rejected, and stays usable', async () => {
    // drag-and-drop bypasses the file picker's `accept` filter, so the server-side check is what protects us here
    const { container } = render(<App />)
    await screen.findByText(/Edit Office documents/)
    fireEvent.drop(container.querySelector('.app')!, { dataTransfer: { files: [new File(['x'], 'run.exe')], types: ['Files'] } })
    expect(await screen.findByRole('alert')).toHaveTextContent("run.exe: file type '.exe' is not supported")
    expect(screen.getByLabelText('Message')).toBeEnabled()
    await userEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('streams progress while working, then shows the answer and the download card', async () => {
    let push!: (c: Uint8Array) => void, close!: () => void
    srv.chat = () => new Response(new ReadableStream({ start(c) { push = (x) => c.enqueue(x); close = () => c.close() } }), { status: 200 })
    render(<App />)
    await screen.findByText(/Edit Office documents/)
    await userEvent.type(screen.getByLabelText('Message'), 'Add a slide{Enter}')

    // mid-stream: user message is visible immediately, input is locked, steps accumulate
    expect(await screen.findByText('Add a slide')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    push(ev('start', {})); push(ev('node', { node: 'plan', label: 'Planning the edit' }))
    const working = await screen.findByLabelText('Working')
    expect(within(working).getByText('Planning the edit')).toBeInTheDocument()
    push(ev('node', { node: 'render', label: 'Applying changes to a copy of your file' }))
    expect(await within(working).findByText('Applying changes to a copy of your file')).toBeInTheDocument()
    expect(within(working).getByText('Planning the edit')).toHaveClass('done')

    // the server persists the turn, then streams `final`
    const meta = { artifact: { id: 'art1', name: 'deck (edited).pptx', format: 'pptx', version: 1 }, render: { applied: 2, warnings: [], preserved: true, violations: [] } }
    srv.detail.messages.push({ id: 'u1', role: 'user', content: 'Add a slide', meta: {}, created_at: 't' },
      { id: 'a1', role: 'assistant', content: 'Done. I created **deck (edited).pptx**.', meta, created_at: 't' })
    srv.detail.artifacts.push({ id: 'art1', name: 'deck (edited).pptx', kind: 'pptx', versions: [{ version: 1, summary: 'Added agenda', created_at: 't' }] })
    push(ev('final', { content: 'Done.', meta, intent: 'edit' })); close()

    await waitFor(() => expect(screen.queryByLabelText('Working')).toBeNull())
    const card = document.querySelector('.artifact-card') as HTMLElement
    expect(within(card).getByText('deck (edited).pptx')).toBeInTheDocument()
    expect(within(card).getByText(/layout and formatting preserved/)).toBeInTheDocument()
    const links = screen.getAllByRole('link', { name: /Download|v1/ })
    expect(links.some((l) => l.getAttribute('href') === '/api/sessions/s1/artifacts/art1/versions/1/download')).toBe(true)
    expect(screen.getByLabelText('Message')).toBeEnabled()
    expect(screen.getByText('Added agenda', { exact: false })).toBeInTheDocument() // version list in the side panel
  })

  it('shows a friendly error banner when the server refuses the chat, and recovers', async () => {
    srv.chat = () => json({ error: { code: 'external_service_error', message: 'the language model is not configured (set GROQ_API_KEY)' } }, 502)
    render(<App />)
    await screen.findByText(/Edit Office documents/)
    await userEvent.type(screen.getByLabelText('Message'), 'Hello{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('GROQ_API_KEY')
    await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled() // box cleared; nothing stuck in a busy state
  })

  it('reports a dropped connection mid-answer instead of hanging', async () => {
    srv.chat = () => new Response(new ReadableStream({ start(c) { c.enqueue(ev('node', { node: 'plan', label: 'Planning the edit' })); c.close() } }), { status: 200 })
    render(<App />)
    await screen.findByText(/Edit Office documents/)
    await userEvent.type(screen.getByLabelText('Message'), 'Change it{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('connection closed before the answer finished')
    expect(screen.getByLabelText('Message')).toBeEnabled()
  })

  it('opens the template analysis dialog with real facts, and closes on Escape', async () => {
    srv.detail.files.push({ id: 'f1', original_name: 'report.docx', kind: 'docx', size_bytes: 10, created_at: 't' })
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: 'Template analysis' }))
    const dlg = await screen.findByRole('dialog', { name: /Template analysis of report.docx/ })
    expect(await within(dlg).findByText(/8.5" × 11"/)).toBeInTheDocument()
    expect(within(dlg).getByText(/Arial 88%/)).toBeInTheDocument()
    expect(within(dlg).getByText(/left 1.25"/)).toBeInTheDocument()
    expect(within(dlg).getByText('a warning')).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('renders hostile assistant text as inert text', async () => {
    srv.detail.messages.push({ id: 'a1', role: 'assistant', content: '<img src=x onerror="document.title=\'pwned\'">', meta: {}, created_at: 't' })
    const { container } = render(<App />)
    await screen.findByText(/<img src=x/)
    expect(container.querySelector('img')).toBeNull()
    expect(document.title).not.toBe('pwned')
  })
})
