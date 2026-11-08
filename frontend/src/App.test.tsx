import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const enc = new TextEncoder()
const ev = (event: string, data: unknown) => enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

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
