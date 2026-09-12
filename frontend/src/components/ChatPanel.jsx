import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

const POLL_MS = 1500

const WAITING_LABEL = {
  pending: 'queued…',
  answering: 'searching your documents and writing an answer…',
}

export default function ChatPanel({ sessionId, status, onReset }) {
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const bottomRef = useRef(null)

  const waiting = messages.some(
    (m) => m.role === 'assistant' && ['pending', 'answering'].includes(m.status),
  )

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      try {
        const rows = await api.getMessages(sessionId)
        if (!cancelled) setMessages(rows)
      } catch (e) {
        if (!cancelled) setError(String(e.message || e))
      }
    }

    tick()
    if (!waiting) return () => { cancelled = true }

    const timer = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [sessionId, waiting])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, waiting])

  async function send() {
    const content = draft.trim()
    if (!content) return

    setSending(true)
    setError(null)
    try {
      await api.postChat(sessionId, content)
      setDraft('')
      setMessages(await api.getMessages(sessionId))
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setSending(false)
    }
  }

  const fileCount = status?.files?.length ?? 0

  return (
    <section className="card chat">
      <div className="card-head">
        <h2>Ask a question</h2>
        <button className="ghost" onClick={onReset}>
          Start over
        </button>
      </div>
      <p className="hint">
        Ready — {fileCount} {fileCount === 1 ? 'file' : 'files'} indexed.
      </p>

      {error && <div className="error inline"><span>{error}</span></div>}

      <div className="messages">
        {messages.length === 0 && (
          <p className="empty">Nothing asked yet. Try a question about what you uploaded.</p>
        )}

        {messages.map((message) => (
          <div key={message.id} className={`bubble ${message.role}`}>
            {message.content ? (
              <p>{message.content}</p>
            ) : (
              <p className="muted italic">
                {WAITING_LABEL[message.status] || message.status}
              </p>
            )}

            {/* Showing the sources is what keeps a RAG demo honest. If the
                answer is wrong, you can see straight away whether retrieval
                found the wrong chunks or the model ignored the right ones. */}
            {message.sources?.length > 0 && (
              <details>
                <summary>{message.sources.length} sources</summary>
                <ul className="sources">
                  {message.sources.map((source) => (
                    <li key={source.chunk_id}>
                      <span className="src-head">
                        <strong>{source.source}</strong>
                        {source.section && <span className="muted"> · {source.section}</span>}
                        {typeof source.score === 'number' && (
                          <span className="score">{source.score.toFixed(3)}</span>
                        )}
                      </span>
                      {source.text && <p className="src-text">{source.text.slice(0, 300)}…</p>}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="composer">
        <input
          value={draft}
          placeholder="Ask something about your files…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !sending && send()}
          disabled={sending}
        />
        <button className="primary" onClick={send} disabled={sending || !draft.trim()}>
          Send
        </button>
      </div>
    </section>
  )
}
