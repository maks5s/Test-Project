import { useEffect, useState } from 'react'
import { apiRequest } from '../api'
import { formatTimestamp } from '../status'

const POLL_INTERVAL_MS = 3000

export default function MockHelpdesk() {
  const [replies, setReplies] = useState([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    let controller = new AbortController()

    const load = async () => {
      controller.abort()
      controller = new AbortController()
      try {
        const data = await apiRequest('/mock/helpdesk/replies?limit=50', {
          signal: controller.signal,
        })
        if (!active) {
          return
        }
        setReplies(data.items)
        setTotal(data.total)
        setError(null)
      } catch (err) {
        if (!active || err.name === 'AbortError') {
          return
        }
        setError(err.message)
      } finally {
        if (active) {
          setLoading(false)
        }
      }
    }

    load()
    const timer = setInterval(load, POLL_INTERVAL_MS)
    return () => {
      active = false
      controller.abort()
      clearInterval(timer)
    }
  }, [])

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 bg-white px-5 py-4">
        <h2 className="text-sm font-semibold text-gray-900">
          Mock helpdesk inbox <span className="font-normal text-gray-500">({total})</span>
        </h2>
        <p className="mt-1 text-xs text-gray-500">
          Every reply the service POSTs to a mock <code className="font-mono">reply_url</code>.
          Repeated deliveries share one <code className="font-mono">Idempotency-Key</code> and are
          stored once.
        </p>
      </div>

      {error ? (
        <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
          Could not load replies: {error}
        </p>
      ) : null}

      {replies.length === 0 && !loading ? (
        <p className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500">
          Nothing delivered yet. Approve a draft to make the service send a reply here.
        </p>
      ) : null}

      <ul className="space-y-3">
        {replies.map((reply) => (
          <li key={reply.id} className="rounded-lg border border-gray-200 bg-white px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-medium text-gray-900">{reply.ticket_external_id}</span>
              <span className="text-xs text-gray-500">{formatTimestamp(reply.received_at)}</span>
            </div>
            <p className="mt-2 text-sm whitespace-pre-wrap text-gray-800">
              {reply.payload.message}
            </p>
            <p className="mt-2 font-mono text-xs break-all text-gray-400">
              {reply.idempotency_key}
            </p>
          </li>
        ))}
      </ul>
    </div>
  )
}
