import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api'
import * as auth from './auth'
import { useSessionEvents } from './useSessionEvents'
import Login from './components/Login'
import ServiceStrip from './components/ServiceStrip'
import ServiceSetup from './components/ServiceSetup'
import UploadPanel from './components/UploadPanel'
import StatusPanel from './components/StatusPanel'
import ChatPanel from './components/ChatPanel'

// The whole app is one state machine:
//
//   connect    point the backend at the three Colab services
//   setup      pick files
//   uploading  PUT each file to storage, one progress bar each
//   processing frozen, asking the API every 2 seconds whether it is done
//   ready      chat
//   failed     something went wrong; show why
//
// New in v3: signing in comes first. Nothing below is reachable until
// `signedIn` is true -- see Login.jsx. If Cognito is not configured
// (auth.authConfigured() is false), the gate is skipped entirely, which is
// what lets a laptop run the whole stack against local SQLite before a User
// Pool exists.

const STATUS_POLL_MS = 15000

export default function App() {
  const [signedIn, setSignedIn] = useState(null)   // null = still checking
  const [phase, setPhase] = useState('connect')
  const [serviceConfig, setServiceConfig] = useState(null)
  const [checkingServices, setCheckingServices] = useState(true)
  const [sessionId, setSessionId] = useState(null)
  const [status, setStatus] = useState(null)
  const [progress, setProgress] = useState({})
  const [error, setError] = useState(null)
  const timerRef = useRef(null)

  // On load: if auth is not configured, skip the gate. Otherwise, a token
  // already in the Cognito SDK's own storage (localStorage, refreshed as
  // needed) means the browser was already signed in.
  useEffect(() => {
    if (!auth.authConfigured()) {
      setSignedIn(true)
      return
    }
    auth.getIdToken().then((token) => setSignedIn(Boolean(token)))
  }, [])

  const handleSignedIn = useCallback(() => setSignedIn(true), [])

  const handleSignOut = useCallback(() => {
    auth.signOut()
    setSignedIn(false)
    setSessionId(null)
    setStatus(null)
    setPhase('connect')
  }, [])

  // On load, ask the backend whether it can already reach all three. If it can
  // -- because they were set earlier and the tunnels are still open -- skip
  // straight past the connect screen.
  useEffect(() => {
    if (!signedIn) return undefined
    let cancelled = false
    api
      .getServiceConfig()
      .then((config) => {
        if (cancelled) return
        setServiceConfig(config)
        if (config.ready) setPhase('setup')
      })
      .catch(() => {
        // GET /config/services is require_user, not require_admin (see
        // backend/api/routes/config.py) -- every signed-in user can read
        // this, so a failure here means the API itself is unreachable, not
        // a permissions gap. Leave the connect screen showing "not ready"
        // rather than a scary error for what is likely a transient blip.
        if (!cancelled) setError(null)
      })
      .finally(() => !cancelled && setCheckingServices(false))
    return () => {
      cancelled = true
    }
  }, [signedIn])

  const handleConfigured = useCallback((config) => {
    setServiceConfig(config)
    setError(null)
    if (config.ready) setPhase('setup')
  }, [])

  // "Start again" after a session. Re-check the services first: the Colab
  // runtime may have died while we were chatting, and dropping straight back
  // onto the upload screen would walk past the gate.
  const reset = useCallback(() => {
    clearInterval(timerRef.current)
    setSessionId(null)
    setStatus(null)
    setProgress({})
    setError(null)
    setPhase('connect')
    setCheckingServices(true)
    api
      .getServiceConfig()
      .then((config) => {
        setServiceConfig(config)
        setPhase(config.ready ? 'setup' : 'connect')
      })
      .catch(() => setPhase('connect'))
      .finally(() => setCheckingServices(false))
  }, [])

  /** Go back to the connect screen — the Colab runtime restarted, say. */
  const reconnect = useCallback(() => {
    clearInterval(timerRef.current)
    setPhase('connect')
    setSessionId(null)
    setStatus(null)
    setProgress({})
    setError(null)
    api.getServiceConfig().then(setServiceConfig).catch(() => {})
  }, [])

  // One connection per session, opened as soon as we have an id.
  const stream = useSessionEvents(sessionId)

  // File-level progress, straight from the workers.
  useEffect(() => {
    const live = Object.values(stream.files)
    if (live.length === 0) return
    setStatus((prev) => {
      const byId = new Map((prev?.files || []).map((f) => [f.file_id, f]))
      for (const f of live) {
        byId.set(f.file_id, {
          file_id: f.file_id,
          filename: f.filename,
          kind: byId.get(f.file_id)?.kind || '',
          status: f.status,
          chunk_count: f.chunk_count ?? 0,
          error: f.error ?? null,
        })
      }
      return { ...(prev || {}), files: Array.from(byId.values()) }
    })
  }, [stream.files])

  // Session-level progress: the signal to unfreeze the chat box.
  useEffect(() => {
    if (!stream.session) return
    setStatus((prev) => ({ ...(prev || {}), status: stream.session.status,
                           error: stream.session.error }))
    if (stream.session.status === 'ready') setPhase('ready')
    if (stream.session.status === 'failed') {
      setError(stream.session.error || 'processing failed')
      setPhase('failed')
    }
  }, [stream.session])

  // A slow fallback poll, in case the stream cannot be established.
  useEffect(() => {
    if (phase !== 'processing' || !sessionId) return undefined

    const tick = async () => {
      try {
        const fresh = await api.getStatus(sessionId)
        setStatus(fresh)
        if (fresh.status === 'ready') setPhase('ready')
        if (fresh.status === 'failed') {
          setError(fresh.error || 'processing failed')
          setPhase('failed')
        }
      } catch (e) {
        setError(String(e.message || e))
      }
    }

    tick()
    timerRef.current = setInterval(tick, STATUS_POLL_MS)
    return () => clearInterval(timerRef.current)
  }, [phase, sessionId])

  async function handleStart(files) {
    setError(null)
    try {
      const { session_id } = await api.createSession()
      setSessionId(session_id)
      setPhase('uploading')

      const { targets } = await api.presignUploads(session_id, files)

      // Upload everything at once, straight to storage. The API is not
      // involved and never sees a byte.
      await Promise.all(
        targets.map((target, index) =>
          api.uploadToStorage(files[index], target.upload_url, (percent) =>
            setProgress((prev) => ({ ...prev, [target.filename]: percent })),
          ),
        ),
      )

      await api.registerFiles(session_id, targets.map((t) => t.file_id))
      setPhase('processing')
    } catch (e) {
      setError(String(e.message || e))
      setPhase('setup')
    }
  }

  if (signedIn === null) {
    return (
      <div className="app">
        <header className="masthead">
          <p className="eyebrow">Advanced AI Engineering · Version 3</p>
          <h1>EdgentRAG</h1>
        </header>
      </div>
    )
  }

  if (!signedIn) {
    return (
      <div className="app">
        <header className="masthead">
          <p className="eyebrow">Advanced AI Engineering · Version 3</p>
          <h1>EdgentRAG</h1>
          <p className="subtitle">Sign in to upload documents and video, then ask questions about them.</p>
        </header>
        <Login onSignedIn={handleSignedIn} />
      </div>
    )
  }

  return (
    <div className="app">
      <header className="masthead">
        <p className="eyebrow">Advanced AI Engineering · Version 3</p>
        <h1>EdgentRAG</h1>
        <p className="subtitle">Upload documents and video, then ask questions about them.</p>
        {auth.authConfigured() && (
          <p className="subtitle">
            {auth.currentEmail()} — <button type="button" className="linklike" onClick={handleSignOut}>sign out</button>
          </p>
        )}
      </header>

      <ServiceStrip onReconnect={reconnect} />

      {error && (
        <div className="error">
          <strong>Error</strong>
          <span>{error}</span>
        </div>
      )}

      {phase === 'connect' &&
        (checkingServices ? (
          <section className="card setup-panel">
            <p className="setup-lead">Checking whether the services are reachable…</p>
          </section>
        ) : (
          <ServiceSetup config={serviceConfig} onConfigured={handleConfigured} />
        ))}

      {phase === 'setup' && <UploadPanel onStart={handleStart} />}

      {(phase === 'uploading' || phase === 'processing' || phase === 'failed') && (
        <StatusPanel phase={phase} status={status} progress={progress} onReset={reset} />
      )}

      {phase === 'ready' && (
        <ChatPanel sessionId={sessionId} status={status} onReset={reset} />
      )}
    </div>
  )
}
