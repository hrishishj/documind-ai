export type SSEEvent = { event: string; data: unknown }

/**
 * Incremental Server-Sent-Events parser. Network chunks can split an event anywhere (even inside a UTF-8
 * character or between "event:" and "data:"), so callers feed text and keep the returned `rest` buffer.
 */
export function parseSSE(buffer: string): { events: SSEEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const blocks = normalized.split('\n\n')
  const rest = blocks.pop() ?? '' // the last block may be incomplete
  const events: SSEEvent[] = []
  for (const block of blocks) {
    let event = 'message'
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue // comment / keep-alive
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (!data.length) continue
    const raw = data.join('\n')
    try {
      events.push({ event, data: JSON.parse(raw) })
    } catch {
      events.push({ event, data: raw })
    }
  }
  return { events, rest }
}
