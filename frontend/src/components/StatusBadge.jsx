import { prettyStatus, statusClasses } from '../status'

export default function StatusBadge({ status }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${statusClasses(status)}`}
    >
      {prettyStatus(status)}
    </span>
  )
}
