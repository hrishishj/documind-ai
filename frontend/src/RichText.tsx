import type { ReactNode } from 'react'
import type { Citation } from './api'

// Emphasis markers must hug their text ("*word*", never "* word *"), otherwise "2 * 3 and 4 * 5" would be
// mistaken for italics and the asterisks silently dropped from what the user reads.
const INLINE = /(\[\d{1,2}\]|\*\*[^\s*](?:[^*\n]*[^\s*])?\*\*|\*[^\s*](?:[^*\n]*[^\s*])?\*|`[^`\n]+`)/g

type CiteProps = { citations?: Citation[]; onCite?: (ref: number) => void }

/** Inline **bold**, *italic*, `code` and [n] source markers. Everything else is plain text (React escapes it). */
function inline(text: string, { citations, onCite }: CiteProps): ReactNode[] {
  const known = new Map((citations ?? []).map((c) => [c.ref, c]))
  return text.split(INLINE).map((part, i) => {
    const m = /^\[(\d{1,2})\]$/.exec(part)
    if (m) {
      const c = known.get(Number(m[1]))
      // a marker without a stored citation is shown as plain text, never as a link to a source that does not exist
      if (!c) return part
      return (
        <button key={i} type="button" className="cite" onClick={() => onCite?.(c.ref)}
                aria-label={`Source ${c.ref}: ${c.title}${c.locator ? ', ' + c.locator : ''}`} title={c.snippet}>
          {c.ref}
        </button>
      )
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

/**
 * Tiny, safe markdown subset (paragraphs, "- " lists, inline emphasis, [n] citation chips). Builds React elements
 * only: model output can never inject HTML or script.
 */
export function RichText({ text, citations, onCite }: { text: string } & CiteProps) {
  const blocks = text.split(/\n{2,}/).filter((b) => b.trim())
  const c = { citations, onCite }
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split('\n')
        if (lines.every((l) => /^\s*[-•]\s+/.test(l))) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-•]\s+/, ''), c)}</li>
              ))}
            </ul>
          )
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <span key={j}>
                {j > 0 && <br />}
                {inline(l, c)}
              </span>
            ))}
          </p>
        )
      })}
    </>
  )
}
