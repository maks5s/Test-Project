import { useEffect, useState } from 'react'

export default function App() {
  const [health, setHealth] = useState('checking...')

  useEffect(() => {
    const controller = new AbortController()

    fetch('/health', { signal: controller.signal })
      .then((response) => (response.ok ? response.text() : `HTTP ${response.status}`))
      .then(setHealth)
      .catch((error) => {
        if (error.name !== 'AbortError') {
          setHealth('unreachable')
        }
      })

    return () => controller.abort()
  }, [])

  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-2xl font-semibold text-gray-900">Helpdesk Draft Console</h1>
      <p className="mt-2 text-sm text-gray-600">
        API health: <span className="font-mono">{health}</span>
      </p>
      <p className="mt-6 text-sm text-gray-500">
        The operator console is implemented in a later milestone. The API reference is available
        at <a className="text-blue-600 underline" href="/docs">/docs</a>.
      </p>
    </main>
  )
}
