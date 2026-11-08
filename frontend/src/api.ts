import { parseSSE } from './sse'

export type Session = { id: string; title: string; created_at: string; updated_at: string }
export type IndexStatus = 'pending' | 'indexing' | 'ready' | 'failed' | 'skipped'
export type FileOut = {
  id: string; original_name: string; kind: string; size_bytes: number; created_at: string
  index_status: IndexStatus; chunk_count: number; index_error: string
}
export type Citation = {
  ref: number; source_type: 'document' | 'web'; title: string; locator: string | null; snippet: string
  file_id: string | null; url: string | null
}
export type VersionOut = { version: number; summary: string; created_at: string }
export type ArtifactOut = { id: string; name: string; kind: string; versions: VersionOut[] }
export type RenderInfo = { applied: number; warnings: string[]; preserved: boolean; violations: string[] }
export type ArtifactRef = { id: string; name: string; format: string; version: number }
export type MessageMeta = { artifact?: ArtifactRef; render?: RenderInfo }
export type Message = {
  id: string; role: 'user' | 'assistant' | 'system'; content: string; meta: MessageMeta; created_at: string
  citations?: Citation[]
}
export type SessionDetail = Session & { messages: Message[]; files: FileOut[]; artifacts: ArtifactOut[] }
export type Analysis = Record<string, unknown> & { format: string; counts: Record<string, number>; warnings: string[] }
export type UploadOut = { file: FileOut; analysis: Analysis | null }

export class ApiError extends Error {
  code: string
  status: number
  constructor(message: string, code = 'error', status = 0) {
    super(message)
    this.code = code
    this.status = status
  }
}

export const BASE = '/api'

async function failure(res: Response): Promise<ApiError> {
  try {
    const body = await res.json()
    if (body?.error) return new ApiError(body.error.message, body.error.code, res.status)
    if (Array.isArray(body?.detail)) return new ApiError('Invalid request: ' + body.detail.map((d: { msg: string }) => d.msg).join('; '), 'validation', res.status)
  } catch {
    /* not JSON */
  }
  return new ApiError(`Request failed (HTTP ${res.status})`, 'http_error', res.status)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, init)
  } catch {
    throw new ApiError('Cannot reach the server. Is the backend running?', 'network')
  }
  if (!res.ok) throw await failure(res)
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

export const api = {
  listSessions: () => request<Session[]>('/sessions'),
  createSession: () =>
    request<Session>('/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  getSession: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: 'DELETE' }),
  upload: (sessionId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<UploadOut>(`/sessions/${sessionId}/files`, { method: 'POST', body: fd })
  },
  profile: (sessionId: string, fileId: string) => request<Record<string, any>>(`/sessions/${sessionId}/files/${fileId}/profile`),
  artifactUrl: (sessionId: string, artifactId: string, version: number) =>
    `${BASE}/sessions/${sessionId}/artifacts/${artifactId}/versions/${version}/download`,
  fileUrl: (sessionId: string, fileId: string) => `${BASE}/sessions/${sessionId}/files/${fileId}/download`,
}

export type ChatEvent =
  | { type: 'node'; label: string }
  | { type: 'final'; content: string; meta: MessageMeta; intent?: string }
  | { type: 'error'; message: string; code: string }

/** POST a chat message and surface server-sent events. Resolves when the stream ends. */
export async function streamChat(sessionId: string, message: string, onEvent: (e: ChatEvent) => void, signal?: AbortSignal): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${BASE}/sessions/${sessionId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
      signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError('Cannot reach the server. Is the backend running?', 'network')
  }
  if (!res.ok) throw await failure(res)
  if (!res.body) throw new ApiError('The server sent no response body', 'empty')

  const reader = res.body.getReader()
  const decoder = new TextDecoder() // streaming mode keeps multi-byte characters intact across chunks
  let buffer = ''
  let sawTerminal = false
  const dispatch = (events: ReturnType<typeof parseSSE>['events']) => {
    for (const { event, data } of events) {
      const d = data as Record<string, any>
      if (event === 'node') onEvent({ type: 'node', label: String(d.label ?? d.node ?? '') })
      else if (event === 'final') {
        sawTerminal = true
        onEvent({ type: 'final', content: d.content ?? '', meta: d.meta ?? {}, intent: d.intent })
      } else if (event === 'error') {
        sawTerminal = true
        onEvent({ type: 'error', message: d.message ?? 'Something went wrong', code: d.code ?? 'error' })
      }
    }
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const { events, rest } = parseSSE(buffer)
    buffer = rest
    dispatch(events)
  }
  buffer += decoder.decode()
  dispatch(parseSSE(buffer + '\n\n').events) // flush a final event that lacked its blank-line terminator
  if (!sawTerminal) throw new ApiError('The connection closed before the answer finished.', 'truncated')
}
