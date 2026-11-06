import { useEffect, useState } from 'react'
import { apiRequest } from '../api'
import StatusBadge from './StatusBadge'
import TicketDetail from './TicketDetail'
import SimulateTicket from './SimulateTicket'
import { TICKET_STATUSES, formatTimestamp, prettyStatus } from '../status'

const POLL_INTERVAL_MS = 3000

export default function TicketsPage({ onOpenArticle }) {
  const [statusFilter, setStatusFilter] = useState('')
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [listError, setListError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [loading, setLoading] = useState(true)

  // Selection is derived: until the operator clicks, the newest ticket is open.
  const activeId = selectedId ?? items[0]?.id ?? null

  useEffect(() => {
    let active = true
    let controller = new AbortController()
    const query = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : ''

    const load = async () => {
      controller.abort()
      controller = new AbortController()
      try {
        const list = await apiRequest(`/api/tickets${query}`, { signal: controller.signal })
        if (!active) {
          return
        }
        setItems(list.items)
        setTotal(list.total)
        setListError(null)

        if (activeId) {
          const current = await apiRequest(`/api/tickets/${activeId}`, {
            signal: controller.signal,
          })
          if (active) {
            setDetail(current)
          }
        } else {
          setDetail(null)
        }
      } catch (err) {
        if (!active || err.name === 'AbortError') {
          return
        }
        setListError(err.message)
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
  }, [statusFilter, activeId, refreshKey])

  const runAction = async (action) => {
    if (!activeId) {
      return
    }
    setBusy(action)
    setActionError(null)
    try {
      const updated = await apiRequest(`/api/tickets/${activeId}/${action}`, { method: 'POST' })
      setDetail(updated)
      setRefreshKey((key) => key + 1)
    } catch (err) {
      setActionError(err.message)
    } finally {
      setBusy(null)
    }
  }

  const handleCreated = (accepted) => {
    setSelectedId(accepted.ticket_id)
    setRefreshKey((key) => key + 1)
  }

  return (
    <div className="space-y-4">
      <SimulateTicket onCreated={handleCreated} />

      {listError ? (
        <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
          Could not load tickets: {listError}
        </p>
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <section className="rounded-lg border border-gray-200 bg-white">
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 px-4 py-3">
            <h2 className="text-sm font-semibold text-gray-900">
              Tickets <span className="font-normal text-gray-500">({total})</span>
            </h2>
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              className="rounded border border-gray-300 px-2 py-1 text-sm text-gray-800 focus:border-blue-500 focus:outline-none"
            >
              <option value="">All statuses</option>
              {TICKET_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {prettyStatus(status)}
                </option>
              ))}
            </select>
          </header>

          <ul className="divide-y divide-gray-200">
            {items.map((ticket) => (
              <li key={ticket.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(ticket.id)}
                  className={`block w-full px-4 py-3 text-left hover:bg-gray-50 ${
                    ticket.id === activeId ? 'bg-blue-50' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium text-gray-900">{ticket.external_id}</span>
                    <StatusBadge status={ticket.status} />
                  </div>
                  <p className="mt-1 truncate text-xs text-gray-500">{ticket.customer_email}</p>
                  <p className="mt-1 text-xs text-gray-400">{formatTimestamp(ticket.created_at)}</p>
                </button>
              </li>
            ))}
          </ul>

          {items.length === 0 && !loading ? (
            <p className="px-4 py-6 text-sm text-gray-500">
              No tickets{statusFilter ? ` with status ${prettyStatus(statusFilter)}` : ''} yet.
            </p>
          ) : null}
        </section>

        {detail ? (
          <TicketDetail
            ticket={detail}
            busy={busy}
            error={actionError}
            onAction={runAction}
            onOpenArticle={onOpenArticle}
          />
        ) : (
          <section className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500">
            Select a ticket to review its draft and delivery state.
          </section>
        )}
      </div>
    </div>
  )
}
