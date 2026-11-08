export const ACCEPT = '.docx,.pptx,.potx,.xlsx,.xls,.pdf,.txt,.md'

/** How often the file list is refreshed while documents are still being indexed for search. */
export const POLL_MS = 2000

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
