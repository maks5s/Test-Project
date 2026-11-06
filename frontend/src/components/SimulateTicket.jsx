import { useState } from 'react'
import { apiRequest } from '../api'

const DEFAULTS = {
  event_id: '',
  ticket_id: '',
  customer_email: 'demo.customer@example.com',
  message: 'Hi! How can I export a report to PDF? The export button does nothing.',
}

export default function SimulateTicket({ onCreated }) {
  const [form, setForm] = useState(DEFAULTS)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const update = (field) => (event) => {
    setForm((current) => ({ ...current, [field]: event.target.value }))
  }

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setResult(null)

    const payload = {}
    for (const [key, value] of Object.entries(form)) {
      if (value.trim()) {
        payload[key] = value
      }
    }

    try {
      const accepted = await apiRequest('/api/dev/tickets', { method: 'POST', body: payload })
      setResult(accepted)
      onCreated?.(accepted)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="rounded-lg border border-gray-200 bg-white">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-gray-800">
        Simulate ticket
      </summary>
      <form onSubmit={submit} className="space-y-3 border-t border-gray-200 px-4 py-4">
        <p className="text-xs text-gray-500">
          Sends the same payload a helpdesk webhook would. Leave ids empty to generate them.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="block text-xs font-medium text-gray-600">
            Event id
            <input
              value={form.event_id}
              onChange={update('event_id')}
              placeholder="auto"
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
            />
          </label>
          <label className="block text-xs font-medium text-gray-600">
            Ticket id
            <input
              value={form.ticket_id}
              onChange={update('ticket_id')}
              placeholder="auto"
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
            />
          </label>
        </div>
        <label className="block text-xs font-medium text-gray-600">
          Customer email
          <input
            type="email"
            value={form.customer_email}
            onChange={update('customer_email')}
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
          />
        </label>
        <label className="block text-xs font-medium text-gray-600">
          Message
          <textarea
            value={form.message}
            onChange={update('message')}
            rows={3}
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
          />
        </label>

        {error ? (
          <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">{error}</p>
        ) : null}
        {result ? (
          <p className="rounded border border-green-200 bg-green-50 p-2 text-sm text-green-800">
            Accepted {result.external_id}
            {result.duplicate ? ' (duplicate delivery)' : ''}.
          </p>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Sending…' : 'Create test ticket'}
        </button>
      </form>
    </details>
  )
}
