import type { ReactNode } from 'react'

// Emphasis markers must hug their text ("*word*", never "* word *"), otherwise "2 * 3 and 4 * 5" would be
// mistaken for italics and the asterisks silently dropped from what the user reads.
const INLINE = /(\*\*[^\s*](?:[^*\n]*[^\s*])?\*\*|\*[^\s*](?:[^*\n]*[^\s*])?\*|`[^`\n]+`)/g

/** Inline **bold**, *italic*, `code`. Everything else is plain text (React escapes it). */
export function inline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

/**
 * Tiny, safe markdown subset (paragraphs, "- " lists, inline emphasis). Builds React elements only:
 * model output can never inject HTML or script.
 */
export function RichText({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/).filter((b) => b.trim())
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split('\n')
        if (lines.every((l) => /^\s*[-•]\s+/.test(l))) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-•]\s+/, ''))}</li>
              ))}
            </ul>
          )
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <span key={j}>
                {j > 0 && <br />}
                {inline(l)}
              </span>
            ))}
          </p>
        )
      })}
    </>
  )
}
