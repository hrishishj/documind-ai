export const ACCEPT = '.docx,.pptx,.potx,.xlsx,.xls,.pdf,.txt,.md'

/** A URL is only ever turned into a link if it is plain http(s): never javascript:, data:, file: ... (defence in depth,
 *  the server filters too). Returns null for anything else. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const u = new URL(url)
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null
  } catch {
    return null
  }
}

/** How often the file list is refreshed while documents are still being indexed for search. */
export const POLL_MS = 2000

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
