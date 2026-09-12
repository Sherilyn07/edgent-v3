import { useEffect, useState } from 'react'
import * as api from '../api'

// Three of the five processes this system needs are on Colab, so a dot going
// red here usually means the runtime was recycled and the tunnels are gone --
// not that anything on the AWS side is broken.
//
// Two calls, deliberately kept separate: GET /health is cheap liveness for
// the API itself and needs no auth; GET /config/services is where the three
// GPU services' addresses and health actually live (`ServiceConfigOut` --
// {urls, healthy, ready}), and needs a signed-in user (see
// backend/api/routes/config.py -- every user can *read* this, only admins can
// *write* it). Fixed here from an earlier version of this file that read
// `health.services`/`health.service_urls`/`health.storage`, none of which
// GET /health has ever returned -- that made every dot permanently red
// regardless of the services' real state.

const LABELS = { embedding: 'embedding', stt: 'stt', llm: 'llm' }

/** trycloudflare URLs are long and all look alike; the host is the useful bit. */
function hostOf(url) {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

export default function ServiceStrip({ onReconnect }) {
  const [config, setConfig] = useState(null)
  const [reachable, setReachable] = useState(true)

  useEffect(() => {
    const check = async () => {
      try {
        await api.getHealth()
        setReachable(true)
      } catch {
        setReachable(false)
        return
      }
      try {
        setConfig(await api.getServiceConfig())
      } catch {
        // The API is up (we just confirmed that above) but this call failed
        // -- most likely a session that has expired. Leave the last known
        // config showing rather than blanking the strip on a blip.
      }
    }
    check()
    const timer = setInterval(check, 10000)
    return () => clearInterval(timer)
  }, [])

  if (!reachable) {
    return (
      <div className="strip">
        <span className="dot bad" />
        <span>API not reachable — is the backend running?</span>
      </div>
    )
  }

  if (!config) return null

  const anyDown = Object.values(config.healthy || {}).some((ok) => !ok)
  // Always offer the way back, not only when something is red: you may want to
  // point at a different Colab instance while this one is still perfectly fine.

  return (
    <div className="strip">
      <span className="dot good" />
      <span className="strip-label">api</span>

      {Object.entries(LABELS).map(([key, label]) => (
        <span
          key={key}
          className="strip-item"
          title={config.urls?.[key] || ''}
        >
          <span className={`dot ${config.healthy?.[key] ? 'good' : 'bad'}`} />
          <span className="strip-label">
            {label}
            {config.urls?.[key] && (
              <span className="strip-host">{hostOf(config.urls[key])}</span>
            )}
          </span>
        </span>
      ))}

      {onReconnect && (
        <button
          className={`strip-action${anyDown ? ' urgent' : ''}`}
          onClick={onReconnect}
        >
          {anyDown ? 'reconnect' : 'change'}
        </button>
      )}
    </div>
  )
}
