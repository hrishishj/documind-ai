import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Composer, FilesPanel, MessageList } from './components'
import { fmtSize } from './utils'
import { RichText } from './RichText'
import type { Citation, Message } from './api'

const msg = (over: Partial<Message>): Message => ({ id: 'm', role: 'assistant', content: '', meta: {}, created_at: '', ...over })

describe('RichText', () => {
  it('renders emphasis, code and lists', () => {
    const { container } = render(<RichText text={'Done. **Bold** and *it* and `code`.\n\n- one\n- two'} />)
    expect(container.querySelector('strong')?.textContent).toBe('Bold')
    expect(container.querySelector('em')?.textContent).toBe('it')
    expect(container.querySelector('code')?.textContent).toBe('code')
    expect([...container.querySelectorAll('li')].map((l) => l.textContent)).toEqual(['one', 'two'])
  })

  it('never turns model output into HTML (XSS safety)', () => {
    const evil = '<img src=x onerror="alert(1)"> <script>alert(2)</script> **<b>x</b>**'
    const { container } = render(<RichText text={evil} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">')  // shown as literal text
  })

  it('leaves unbalanced markers alone', () => {
    const { container } = render(<RichText text="2 * 3 = 6 and **unclosed" />)
    expect(container.textContent).toBe('2 * 3 = 6 and **unclosed')
  })

  it.each([
    ['2 * 3 and 4 * 5', '2 * 3 and 4 * 5'],            // spaced asterisks are arithmetic, not emphasis
    ['a*b*c', 'abc'],                                   // hugging markers are italics
    ['Total: **5 * 4**', 'Total: **5 * 4**'],           // nested asterisks aren't parsed; shown literally, nothing lost
    ['**bold****bold**', 'boldbold'],
  ])('never loses characters unintentionally: %s', (input, visible) => {
    const { container } = render(<RichText text={input} />)
    expect(container.textContent).toBe(visible)
  })
})

const cite = (ref: number, over = {}): Citation => ({
  ref, source_type: 'document', title: 'annual.docx', locator: '¶12-14', snippet: 'Total revenue was 331,839 million.',
  file_id: 'f1', url: null, ...over,
})

describe('citations', () => {
  it('renders known [n] markers as chips and leaves unknown ones as plain text', async () => {
    const onCite = vi.fn()
    const { container } = render(<RichText text="Revenue was 331,839 [1]. Odd claim [4]." citations={[cite(1)]} onCite={onCite} />)
    const chips = container.querySelectorAll('button.cite')
    expect(chips).toHaveLength(1)
    expect(chips[0]).toHaveAccessibleName('Source 1: annual.docx, ¶12-14')
    expect(container.textContent).toContain('[4]')  // never a link to a source that does not exist
    await userEvent.click(chips[0])
    expect(onCite).toHaveBeenCalledWith(1)
  })

  it('shows a Sources list under the answer and reveals the quoted passage on demand', async () => {
    render(<MessageList sessionId="s" busy={false} steps={[]}
      messages={[msg({ id: 'a1', content: 'Revenue was 331,839 [1] and R&D 35,562 [2].', citations: [cite(1), cite(2, { locator: 'Sheet \'x\' row 5', snippet: 'R&D | 35562' })] })]} />)
    const sources = screen.getByLabelText('Sources')
    expect(within(sources).getByText('¶12-14')).toBeInTheDocument()
    expect(within(sources).getByText("Sheet 'x' row 5")).toBeInTheDocument()
    expect(screen.queryByText('R&D | 35562')).toBeNull()                    // collapsed by default
    await userEvent.click(within(sources).getByRole('button', { name: /Sheet 'x' row 5/ }))
    expect(screen.getByText('R&D | 35562')).toBeInTheDocument()
    await userEvent.click(within(sources).getByRole('button', { name: /Sheet 'x' row 5/ }))
    expect(screen.queryByText('R&D | 35562')).toBeNull()                    // toggles closed again
  })

  it('clicking an inline chip opens that source', async () => {
    render(<MessageList sessionId="s" busy={false} steps={[]}
      messages={[msg({ id: 'a1', content: 'Revenue was 331,839 [1].', citations: [cite(1)] })]} />)
    await userEvent.click(screen.getByRole('button', { name: /^Source 1:/ }))
    expect(screen.getByText('Total revenue was 331,839 million.', { selector: 'blockquote' })).toBeInTheDocument()
  })

  it('keeps each message\'s open source independent', async () => {
    render(<MessageList sessionId="s" busy={false} steps={[]} messages={[
      msg({ id: 'a1', content: 'One [1].', citations: [cite(1, { snippet: 'first passage' })] }),
      msg({ id: 'a2', content: 'Two [1].', citations: [cite(1, { snippet: 'second passage' })] })]} />)
    await userEvent.click(screen.getAllByRole('button', { name: /^Source 1:/ })[0])
    expect(screen.getByText('first passage', { selector: 'blockquote' })).toBeInTheDocument()
    expect(screen.queryByText('second passage', { selector: 'blockquote' })).toBeNull()
  })

  it('has no Sources section when there are no citations', () => {
    render(<MessageList sessionId="s" busy={false} steps={[]} messages={[msg({ content: 'Hello' })]} />)
    expect(screen.queryByLabelText('Sources')).toBeNull()
  })
})

describe('index status badges', () => {
  const file = (over = {}) => ({ id: 'f', original_name: 'a.docx', kind: 'docx', size_bytes: 10, created_at: '', index_status: 'ready' as const, chunk_count: 3, index_error: '', ...over })
  const panel = (f: ReturnType<typeof file>) => render(<FilesPanel sessionId="s" files={[f]} artifacts={[]} uploading={[]} onAnalysis={() => {}} />)

  it('says when a file is being prepared, ready (with count) or not searchable (with the reason)', () => {
    const { unmount } = panel(file({ index_status: 'indexing' }))
    expect(screen.getByRole('status')).toHaveTextContent('Preparing for search')
    unmount()
    const r2 = panel(file({ chunk_count: 1 }))
    expect(screen.getByText('Searchable · 1 passage')).toBeInTheDocument()
    r2.unmount()
    const r3 = panel(file({ chunk_count: 42 }))
    expect(screen.getByText('Searchable · 42 passages')).toBeInTheDocument()
    r3.unmount()
    panel(file({ index_status: 'failed', index_error: 'No searchable text was found (is this a scanned image?)', chunk_count: 0 }))
    expect(screen.getByText(/Not searchable: No searchable text/)).toBeInTheDocument()
  })

  it('shows nothing for files that skip indexing', () => {
    panel(file({ index_status: 'skipped' }))
    expect(screen.queryByText(/search/i)).toBeNull()
  })
})

describe('Composer', () => {
  it('sends on Enter, not on Shift+Enter, and clears the box', async () => {
    const onSend = vi.fn()
    render(<Composer busy={false} onSend={onSend} onFiles={() => {}} onStop={() => {}} />)
    const box = screen.getByLabelText('Message')
    await userEvent.type(box, 'hello{Shift>}{Enter}{/Shift}world')
    expect(onSend).not.toHaveBeenCalled()
    await userEvent.type(box, '{Enter}')
    expect(onSend).toHaveBeenCalledWith('hello\nworld')
    expect(box).toHaveValue('')
  })

  it('ignores empty/whitespace messages', async () => {
    const onSend = vi.fn()
    render(<Composer busy={false} onSend={onSend} onFiles={() => {}} onStop={() => {}} />)
    await userEvent.type(screen.getByLabelText('Message'), '   {Enter}')
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('shows Stop (and blocks sending/attaching) while busy', async () => {
    const onStop = vi.fn(); const onSend = vi.fn()
    render(<Composer busy onSend={onSend} onFiles={() => {}} onStop={onStop} />)
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(onStop).toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Message'), 'x{Enter}')
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Attach a file')).toBeDisabled()
  })

  it('passes chosen files up', async () => {
    const onFiles = vi.fn()
    render(<Composer busy={false} onSend={() => {}} onFiles={onFiles} onStop={() => {}} />)
    const f = new File(['x'], 'a.docx')
    await userEvent.upload(screen.getByTestId('file-input'), f)
    expect(onFiles).toHaveBeenCalledWith([f])
  })
})

describe('MessageList', () => {
  const artifactMsg = (render_: Message['meta']['render']) => msg({
    id: 'a1', content: 'Done.', meta: { artifact: { id: 'art', name: 'report (edited).docx', format: 'docx', version: 2 }, render: render_ },
  })

  it('shows an artifact card with a versioned download link and a preserved badge', () => {
    render(<MessageList sessionId="s1" busy={false} steps={[]}
      messages={[artifactMsg({ applied: 1, warnings: [], preserved: true, violations: [] })]} />)
    const link = screen.getByRole('link', { name: 'Download' })
    expect(link).toHaveAttribute('href', '/api/sessions/s1/artifacts/art/versions/2/download')
    expect(screen.getByText('v2')).toBeInTheDocument()
    expect(screen.getByText(/layout and formatting preserved/)).toBeInTheDocument()
  })

  it('surfaces preservation violations and warnings instead of hiding them', () => {
    render(<MessageList sessionId="s1" busy={false} steps={[]}
      messages={[artifactMsg({ applied: 1, warnings: ['chart(s) re-serialized'], preserved: false, violations: ['theme changed'] })]} />)
    expect(screen.getByText(/Differences from the original design: theme changed/)).toBeInTheDocument()
    expect(screen.getByText('chart(s) re-serialized')).toBeInTheDocument()
  })

  it('shows the empty state only when idle with no messages', () => {
    const { rerender } = render(<MessageList sessionId="s" messages={[]} steps={[]} busy={false} />)
    expect(screen.getByText(/Edit Office documents by chatting/)).toBeInTheDocument()
    rerender(<MessageList sessionId="s" messages={[]} steps={['Planning the edit']} busy />)
    expect(screen.queryByText(/Edit Office documents by chatting/)).toBeNull()
    expect(within(screen.getByLabelText('Working')).getByText('Planning the edit')).toBeInTheDocument()
  })

  it('does not render an artifact card for user messages', () => {
    render(<MessageList sessionId="s" busy={false} steps={[]} messages={[msg({ role: 'user', content: 'hi', meta: { artifact: { id: 'x', name: 'n', format: 'docx', version: 1 } } })]} />)
    expect(screen.queryByRole('link', { name: 'Download' })).toBeNull()
  })
})

describe('fmtSize', () => {
  it.each([[500, '500 B'], [2048, '2 KB'], [5 * 1024 * 1024, '5.0 MB']])('%d -> %s', (n, s) => expect(fmtSize(n)).toBe(s))
})
