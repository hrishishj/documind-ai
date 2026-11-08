import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, streamChat, type ChatEvent } from './api'

const enc = new TextEncoder()

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(x)); c.close() } })
}
const okStream = (chunks: Uint8Array[]) => new Response(streamOf(chunks), { status: 200, headers: { 'content-type': 'text/event-stream' } })
const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

afterEach(() => vi.restoreAllMocks())

const FINAL = 'event: final\ndata: ' + JSON.stringify({ content: 'Fertig — ✓ 日本語', meta: { artifact: { id: 'a', name: 'x.docx', format: 'docx', version: 1 } }, intent: 'edit' }) + '\n\n'
const NODE = 'event: node\ndata: {"node":"plan","label":"Planning the edit"}\n\n'

async function collect(chunks: Uint8Array[]): Promise<ChatEvent[]> {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okStream(chunks)))
  const out: ChatEvent[] = []
  await streamChat('s1', 'hi', (e) => out.push(e))
  return out
}

describe('streamChat', () => {
  it('delivers node and final events', async () => {
    const out = await collect([enc.encode(NODE + FINAL)])
    expect(out.map((e) => e.type)).toEqual(['node', 'final'])
    expect(out[0]).toMatchObject({ label: 'Planning the edit' })
  })

  it('survives a multi-byte UTF-8 character split across network chunks', async () => {
    const bytes = enc.encode(NODE + FINAL)
    const i = bytes.indexOf(0xe2) + 1 // 0xE2 starts the 3-byte "—": cut in the middle of it
    const out = await collect([bytes.slice(0, i), bytes.slice(i)])
    const final = out.find((e) => e.type === 'final') as Extract<ChatEvent, { type: 'final' }>
    expect(final.content).toBe('Fertig — ✓ 日本語')
  })

  it('works when every byte arrives separately', async () => {
    const out = await collect(Array.from(enc.encode(NODE + FINAL), (b) => new Uint8Array([b])))
    expect(out.map((e) => e.type)).toEqual(['node', 'final'])
  })

  it('flushes a final event that lacks the blank-line terminator', async () => {
    const out = await collect([enc.encode(FINAL.trimEnd())])
    expect(out.map((e) => e.type)).toEqual(['final'])
  })

  it('reports a stream that ends without a final/error as truncated', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okStream([enc.encode(NODE)])))
    await expect(streamChat('s1', 'hi', () => {})).rejects.toMatchObject({ code: 'truncated' })
  })

  it('surfaces a server error event', async () => {
    const out = await collect([enc.encode('event: error\ndata: {"code":"internal_error","message":"Something went wrong"}\n\n')])
    expect(out).toEqual([{ type: 'error', code: 'internal_error', message: 'Something went wrong' }])
  })

  it('turns an HTTP error body into an ApiError with the server message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: { code: 'external_service_error', message: 'GROQ_API_KEY is not configured' } }, 502)))
    await expect(streamChat('s1', 'hi', () => {})).rejects.toMatchObject({ message: 'GROQ_API_KEY is not configured', code: 'external_service_error', status: 502 })
  })

  it('explains a network failure instead of leaking a TypeError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(streamChat('s1', 'hi', () => {})).rejects.toMatchObject({ code: 'network', message: expect.stringContaining('Cannot reach the server') })
  })

  it('lets an abort propagate as an AbortError', async () => {
    const err = Object.assign(new Error('aborted'), { name: 'AbortError' })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(err))
    await expect(streamChat('s1', 'hi', () => {})).rejects.toMatchObject({ name: 'AbortError' })
  })
})

describe('api', () => {
  it('formats FastAPI validation errors readably', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: [{ msg: 'String should have at least 1 character' }] }, 422)))
    await expect(api.getSession('x')).rejects.toMatchObject({ code: 'validation', message: expect.stringContaining('at least 1 character') })
  })

  it('handles 204 No Content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(api.deleteSession('x')).resolves.toBeUndefined()
  })

  it('falls back to a generic message for non-JSON failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502 })))
    const e = await api.listSessions().catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e.message).toBe('Request failed (HTTP 502)')
  })

  it('builds download URLs under the proxy prefix', () => {
    expect(api.artifactUrl('s', 'a', 2)).toBe('/api/sessions/s/artifacts/a/versions/2/download')
  })
})
