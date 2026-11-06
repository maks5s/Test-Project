export const TICKET_STATUSES = [
  'received',
  'generating',
  'draft_ready',
  'approved',
  'sending',
  'sent',
  'rejected',
  'generation_failed',
  'send_failed',
]

const STATUS_CLASSES = {
  received: 'bg-gray-100 text-gray-700 border-gray-200',
  generating: 'bg-blue-50 text-blue-700 border-blue-200',
  draft_ready: 'bg-amber-50 text-amber-800 border-amber-200',
  approved: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  sending: 'bg-blue-50 text-blue-700 border-blue-200',
  sent: 'bg-green-50 text-green-700 border-green-200',
  rejected: 'bg-gray-100 text-gray-600 border-gray-200',
  generation_failed: 'bg-red-50 text-red-700 border-red-200',
  send_failed: 'bg-red-50 text-red-700 border-red-200',
}

export function statusClasses(status) {
  return STATUS_CLASSES[status] ?? 'bg-gray-100 text-gray-700 border-gray-200'
}

export function prettyStatus(status) {
  return String(status ?? 'unknown').replaceAll('_', ' ')
}

export function formatTimestamp(value) {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return String(value)
  }
  return date.toLocaleString()
}

export function shortId(value) {
  return value ? String(value).slice(0, 8) : '—'
}
