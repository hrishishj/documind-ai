import { describe, expect, it } from 'vitest'
import { parseSSE } from './sse'

describe('parseSSE', () => {
  it('parses complete events and keeps no remainder', () => {
    const { events, rest } = parseSSE('event: node\ndata: {"label":"Planning"}\n\nevent: final\ndata: {"content":"ok"}\n\n')
    expect(events).toEqual([{ event: 'node', data: { label: 'Planning' } }, { event: 'final', data: { content: 'ok' } }])
    expect(rest).toBe('')
  })

  it('keeps an incomplete trailing event for the next chunk', () => {
    const { events, rest } = parseSSE('event: node\ndata: {"label":"A"}\n\nevent: fin')
    expect(events).toHaveLength(1)
    expect(rest).toBe('event: fin')
  })

  it('gives the same result however the stream is split into chunks', () => {
    const full = 'event: node\ndata: {"label":"Reading"}\n\nevent: final\ndata: {"content":"héllo → ✓","meta":{}}\n\n'
    const whole = parseSSE(full).events
    for (let cut = 1; cut < full.length; cut++) {
      let buf = ''
      const got: unknown[] = []
      for (const part of [full.slice(0, cut), full.slice(cut)]) {
        buf += part
        const r = parseSSE(buf)
        buf = r.rest
        got.push(...r.events)
      }
      expect(got).toEqual(whole)
    }
  })

  it('handles CRLF, comments, multi-line data and non-JSON data', () => {
    const { events } = parseSSE(': keep-alive\r\n\r\nevent: x\r\ndata: line1\r\ndata: line2\r\n\r\nevent: y\r\ndata: {"a":1}\r\n\r\n')
    expect(events).toEqual([{ event: 'x', data: 'line1\nline2' }, { event: 'y', data: { a: 1 } }])
  })

  it('ignores blocks without data and defaults the event name', () => {
    const { events } = parseSSE('event: empty\n\ndata: {"z":1}\n\n')
    expect(events).toEqual([{ event: 'message', data: { z: 1 } }])
  })
})
