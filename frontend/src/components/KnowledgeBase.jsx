import { useEffect, useState } from 'react'
import { apiRequest } from '../api'
import { formatTimestamp } from '../status'

export default function KnowledgeBase({ selectedSlug, onSelect }) {
  const [items, setItems] = useState([])
  const [article, setArticle] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    apiRequest('/api/articles', { signal: controller.signal })
      .then((data) => {
        setItems(data.items)
        setError(null)
      })
      .catch((err) => {
        if (err.name !== 'AbortError') {
          setError(err.message)
        }
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!selectedSlug) {
      return undefined
    }
    const controller = new AbortController()
    apiRequest(`/api/articles/${encodeURIComponent(selectedSlug)}`, { signal: controller.signal })
      .then((data) => {
        setArticle(data)
        setError(null)
      })
      .catch((err) => {
        if (err.name !== 'AbortError') {
          setError(err.message)
        }
      })
    return () => controller.abort()
  }, [selectedSlug])

  useEffect(() => {
    if (!selectedSlug && items.length > 0) {
      onSelect(items[0].slug)
    }
  }, [items, selectedSlug, onSelect])

  return (
    <div className="space-y-4">
      {error ? (
        <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">{error}</p>
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.8fr)]">
        <section className="rounded-lg border border-gray-200 bg-white">
          <header className="border-b border-gray-200 px-4 py-3">
            <h2 className="text-sm font-semibold text-gray-900">
              Articles <span className="font-normal text-gray-500">({items.length})</span>
            </h2>
          </header>
          <ul className="divide-y divide-gray-200">
            {items.map((item) => (
              <li key={item.slug}>
                <button
                  type="button"
                  onClick={() => onSelect(item.slug)}
                  className={`block w-full px-4 py-3 text-left hover:bg-gray-50 ${
                    item.slug === selectedSlug ? 'bg-blue-50' : ''
                  }`}
                >
                  <span className="text-sm font-medium text-gray-900">{item.title}</span>
                  <span className="mt-1 block font-mono text-xs text-gray-400">{item.slug}</span>
                </button>
              </li>
            ))}
          </ul>
          {items.length === 0 && !loading ? (
            <p className="px-4 py-6 text-sm text-gray-500">
              No articles yet. Run <code className="font-mono">python -m app.seed</code>.
            </p>
          ) : null}
        </section>

        <section className="rounded-lg border border-gray-200 bg-white">
          {article && article.slug === selectedSlug ? (
            <article className="px-5 py-4">
              <h2 className="text-lg font-semibold text-gray-900">{article.title}</h2>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                <span className="font-mono">{article.slug}</span>
                <span>updated {formatTimestamp(article.updated_at)}</span>
              </div>
              {article.tags.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-1">
                  {article.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded border border-gray-200 bg-gray-50 px-2 py-0.5 text-xs text-gray-600"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}
              <p className="mt-4 text-sm leading-6 whitespace-pre-wrap text-gray-800">
                {article.body}
              </p>
            </article>
          ) : (
            <p className="px-5 py-6 text-sm text-gray-500">
              Select an article to read the text the drafts are grounded in.
            </p>
          )}
        </section>
      </div>
    </div>
  )
}
