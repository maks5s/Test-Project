import { useState } from 'react'
import KnowledgeBase from './components/KnowledgeBase'
import MockHelpdesk from './components/MockHelpdesk'
import TicketsPage from './components/TicketsPage'

const TABS = [
  { id: 'tickets', label: 'Tickets' },
  { id: 'kb', label: 'Knowledge Base' },
  { id: 'mock', label: 'Mock Helpdesk' },
]

export default function App() {
  const [tab, setTab] = useState('tickets')
  const [articleSlug, setArticleSlug] = useState(null)

  const openArticle = (slug) => {
    setArticleSlug(slug)
    setTab('kb')
  }

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <header className="border-b border-gray-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Helpdesk Draft Console</h1>
            <p className="text-sm text-gray-500">Review AI-drafted replies before delivery</p>
          </div>
          <nav className="flex gap-1 rounded-lg border border-gray-200 p-1">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className={`rounded px-3 py-1.5 text-sm font-medium ${
                  tab === item.id
                    ? 'bg-gray-900 text-white'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-6">
        {tab === 'tickets' ? <TicketsPage onOpenArticle={openArticle} /> : null}
        {tab === 'kb' ? (
          <KnowledgeBase selectedSlug={articleSlug} onSelect={setArticleSlug} />
        ) : null}
        {tab === 'mock' ? <MockHelpdesk /> : null}
      </main>
    </div>
  )
}
