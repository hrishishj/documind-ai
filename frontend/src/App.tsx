import { useEffect, useState } from 'react'
import './App.css'

type Health = { status: string; app: string; env: string }

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/health')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<Health>
      })
      .then(setHealth)
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main style={{ padding: 24 }}>
      <h1>DocuMind AI</h1>
      {health && <p data-testid="status">Backend: {health.status} ({health.env})</p>}
      {error && <p data-testid="status">Backend unreachable: {error}</p>}
      {!health && !error && <p>Connecting…</p>}
    </main>
  )
}
