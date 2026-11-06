import StatusBadge from './StatusBadge'
import { formatTimestamp, prettyStatus } from '../status'

const REGENERATABLE = new Set(['draft_ready', 'generation_failed', 'rejected'])

function Field({ label, children }) {
  return (
    <div>
      <dt className="text-xs font-medium tracking-wide text-gray-500 uppercase">{label}</dt>
      <dd className="mt-1 text-sm break-words text-gray-800">{children}</dd>
    </div>
  )
}

function SourceBadge({ source }) {
  const fallback = source === 'fallback'
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${
        fallback
          ? 'border-amber-200 bg-amber-50 text-amber-800'
          : 'border-blue-200 bg-blue-50 text-blue-700'
      }`}
    >
      {fallback ? 'fallback' : 'LLM'}
    </span>
  )
}

export default function TicketDetail({ ticket, busy, error, onAction, onOpenArticle }) {
  const draft = ticket.current_draft
  const outbound = ticket.outbound
  const canModerate = ticket.status === 'draft_ready'
  const canRegenerate = REGENERATABLE.has(ticket.status)
  const canRetry = ticket.status === 'send_failed'

  return (
    <section className="rounded-lg border border-gray-200 bg-white">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-200 px-5 py-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">{ticket.external_id}</h2>
          <p className="text-sm text-gray-500">{ticket.customer_email}</p>
        </div>
        <StatusBadge status={ticket.status} />
      </header>

      <div className="space-y-6 px-5 py-4">
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Reply target">
            <span className="font-mono text-xs">{ticket.reply_url}</span>
          </Field>
          <Field label="Received">{formatTimestamp(ticket.created_at)}</Field>
          <Field label="Approved">{formatTimestamp(ticket.approved_at)}</Field>
          <Field label="Sent">{formatTimestamp(ticket.sent_at)}</Field>
        </dl>

        <div>
          <h3 className="text-xs font-medium tracking-wide text-gray-500 uppercase">
            Customer message
          </h3>
          <p className="mt-2 rounded-md border border-gray-200 bg-gray-50 p-3 text-sm whitespace-pre-wrap text-gray-800">
            {ticket.message}
          </p>
        </div>

        {draft ? (
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-xs font-medium tracking-wide text-gray-500 uppercase">
                Draft v{draft.version}
              </h3>
              <SourceBadge source={draft.source} />
              {draft.model ? <span className="text-xs text-gray-500">{draft.model}</span> : null}
              {draft.latency_ms !== null && draft.latency_ms !== undefined ? (
                <span className="text-xs text-gray-500">{draft.latency_ms} ms</span>
              ) : null}
            </div>
            <p className="mt-2 rounded-md border border-gray-200 p-3 text-sm whitespace-pre-wrap text-gray-800">
              {draft.content}
            </p>
            {draft.citations.length > 0 ? (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <span className="text-xs text-gray-500">Sources:</span>
                {draft.citations.map((citation) => (
                  <button
                    key={citation.slug}
                    type="button"
                    onClick={() => onOpenArticle(citation.slug)}
                    className="rounded border border-gray-300 px-2 py-0.5 text-xs text-blue-700 hover:bg-gray-50"
                  >
                    {citation.title ?? citation.slug}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <p className="text-sm text-gray-500">No draft yet.</p>
        )}

        <div>
          <h3 className="text-xs font-medium tracking-wide text-gray-500 uppercase">Delivery</h3>
          {outbound ? (
            <dl className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Status">{prettyStatus(outbound.status)}</Field>
              <Field label="Attempts">{outbound.attempts}</Field>
              <Field label="Sent at">{formatTimestamp(outbound.sent_at)}</Field>
              <Field label="Idempotency key">
                <span className="font-mono text-xs">{outbound.idempotency_key}</span>
              </Field>
              {outbound.last_error ? (
                <div className="sm:col-span-2">
                  <dt className="text-xs font-medium tracking-wide text-red-600 uppercase">
                    Last error
                  </dt>
                  <dd className="mt-1 rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
                    {outbound.last_error}
                  </dd>
                </div>
              ) : null}
            </dl>
          ) : (
            <p className="mt-2 text-sm text-gray-500">Nothing queued for delivery.</p>
          )}
        </div>

        {ticket.drafts.length > 1 ? (
          <div>
            <h3 className="text-xs font-medium tracking-wide text-gray-500 uppercase">
              Draft history
            </h3>
            <ul className="mt-2 space-y-1 text-sm text-gray-700">
              {ticket.drafts.map((version) => (
                <li key={version.id} className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">v{version.version}</span>
                  <span className="text-xs text-gray-500">
                    {formatTimestamp(version.created_at)}
                  </span>
                  <SourceBadge source={version.source} />
                  {version.id === ticket.current_draft_id ? (
                    <span className="text-xs text-gray-500">(current)</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {error ? (
          <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">{error}</p>
        ) : null}

        <div className="flex flex-wrap gap-2 border-t border-gray-200 pt-4">
          <button
            type="button"
            disabled={!canModerate || busy !== null}
            onClick={() => onAction('approve')}
            className="rounded bg-green-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy === 'approve' ? 'Approving…' : 'Approve'}
          </button>
          <button
            type="button"
            disabled={!canModerate || busy !== null}
            onClick={() => onAction('reject')}
            className="rounded border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy === 'reject' ? 'Rejecting…' : 'Reject'}
          </button>
          <button
            type="button"
            disabled={!canRegenerate || busy !== null}
            onClick={() => onAction('regenerate')}
            className="rounded border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy === 'regenerate' ? 'Queuing…' : 'Regenerate'}
          </button>
          <button
            type="button"
            disabled={!canRetry || busy !== null}
            onClick={() => onAction('retry-delivery')}
            className="rounded border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy === 'retry-delivery' ? 'Queuing…' : 'Retry delivery'}
          </button>
        </div>
      </div>
    </section>
  )
}
