import { useEffect, useRef, useState } from 'react'
import * as api from './api'

// One connection per session, replacing the two-second poll. Same reasoning
// as version 2: EventSource rather than a WebSocket because the traffic is
// one-directional, and the browser reconnects on its own if the connection
// drops.
//
// New in v3: a plain EventSource cannot send an Authorization header, so it
// cannot carry a Cognito ID token the way every other call now does (see
// api.js). Before opening the stream, this hook makes one ordinary,
// authenticated call for a short-lived ticket (api.getEventsTicket) and puts
// *that* in the URL instead. See backend/api/routes/tickets.py for why it is
// a ticket and not the token itself, and why it is not single-use -- a
// single-use ticket would make the browser's automatic reconnect fail
// immediately the moment it tried to use it a second time.

/**
 * Subscribe to everything happening in one session.
 *
 * Returns { connected, files, session, tokens } where `tokens` accumulates
 * answer fragments keyed by message id — so a streaming answer can be
 * rendered as it arrives rather than when it is finished.
 */
export function useSessionEvents(sessionId) {
  const [connected, setConnected] = useState(false)
  const [files, setFiles] = useState({})
  const [session, setSession] = useState(null)
  const [tokens, setTokens] = useState({})
  const sourceRef = useRef(null)

  useEffect(() => {
    if (!sessionId) return undefined

    let cancelled = false
    let source = null

    api
      .getEventsTicket(sessionId)
      .then(({ ticket }) => {
        if (cancelled) return

        source = new EventSource(api.eventsUrl(sessionId, ticket))
        sourceRef.current = source

        source.addEventListener('open', () => setConnected(true))
        source.onopen = () => setConnected(true)

        // A file changed status.
        source.addEventListener('file', (e) => {
          const d = JSON.parse(e.data)
          setFiles((prev) => ({ ...prev, [d.file_id]: d }))
        })

        // The session changed status — the signal to unfreeze the chat box.
        source.addEventListener('session', (e) => setSession(JSON.parse(e.data)))

        // A message changed status: retrieving, generating, done, failed.
        source.addEventListener('message', (e) => {
          const d = JSON.parse(e.data)
          setTokens((prev) => ({
            ...prev,
            [d.message_id]: { ...(prev[d.message_id] || { text: '' }), status: d.status, stage: d.stage },
          }))
        })

        // A fragment of an answer. Appended, so this already works token by
        // token the moment the model service can stream.
        source.addEventListener('token', (e) => {
          const d = JSON.parse(e.data)
          setTokens((prev) => {
            const existing = prev[d.message_id] || { text: '', status: 'answering' }
            return { ...prev, [d.message_id]: { ...existing, text: existing.text + d.text } }
          })
        })

        // The browser retries on its own; this only reflects it in the UI.
        // A ticket lasts several minutes (sse_ticket_seconds), well past any
        // ordinary reconnect, so the same ticket keeps working across one.
        source.onerror = () => setConnected(false)
      })
      .catch(() => {
        if (!cancelled) setConnected(false)
      })

    return () => {
      cancelled = true
      if (source) source.close()
      sourceRef.current = null
      setConnected(false)
    }
  }, [sessionId])

  return { connected, files, session, tokens }
}
