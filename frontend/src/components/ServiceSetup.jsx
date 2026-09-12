import { useEffect, useState } from 'react'
import * as api from '../api'

// The first screen. Nothing can be uploaded until the backend can reach all
// three GPU services, because every one of them is needed: the embedding
// service to index, the STT service for video, the LLM to answer.
//
// The addresses come from the Colab terminal. They change every time the Colab
// runtime restarts, which is why this screen exists at all rather than the
// three URLs living in a .env file.

const FIELDS = [
  { key: 'embedding', label: 'Embedding', port: 8001,
    hint: 'turns your documents into vectors, and searches them' },
  { key: 'stt', label: 'Speech to text', port: 8002,
    hint: 'transcribes video and audio' },
  { key: 'llm', label: 'Language model', port: 8003,
    hint: 'writes the answers' },
]

export default function ServiceSetup({ config, onConfigured }) {
  const [urls, setUrls] = useState({ embedding: '', stt: '', llm: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // Prefill with whatever the backend already has, so a reload does not make
  // you paste three URLs again. Only fill boxes that are still empty -- a
  // background refresh of `config` must not overwrite what you are typing.
  useEffect(() => {
    if (!config?.urls) return
    setUrls((prev) => {
      const next = { ...prev }
      for (const key of ['embedding', 'stt', 'llm']) {
        if (!next[key] && config.urls[key]) next[key] = config.urls[key]
      }
      return next
    })
  }, [config])

  const allFilled = FIELDS.every(({ key }) => urls[key]?.trim())

  async function handleConnect(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const fresh = await api.setServiceConfig({
        embedding: urls.embedding.trim(),
        stt: urls.stt.trim(),
        llm: urls.llm.trim(),
      })
      onConfigured(fresh)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  /** Paste all three at once — the launcher prints them as a block. */
  function handlePasteAll(event) {
    const text = event.clipboardData?.getData('text') || ''
    const found = text.match(/https?:\/\/[^\s"']+/g)
    if (!found || found.length < 3) return

    event.preventDefault()
    const cleaned = found.map((u) => u.replace(/[.,)]+$/, ''))
    const pick = (name, index) =>
      cleaned.find((u) => u.toLowerCase().includes(name)) || cleaned[index]

    setUrls({
      embedding: pick('embedding', 0),
      stt: pick('stt', 1),
      llm: pick('llm', 2),
    })
  }

  return (
    <section className="card setup-panel">
      <h2>Connect your GPU services</h2>
      <p className="setup-lead">
        The three model services run on Colab. Start them there, then paste the
        three HTTPS addresses it prints.
      </p>

      <details className="setup-help">
        <summary>How do I get these?</summary>
        <ol>
          <li>Upload the <code>services</code> folder to your Drive.</li>
          <li>Open Colab, choose a GPU runtime, and mount Drive.</li>
          <li>
            In the Colab terminal run{' '}
            <code>bash /content/drive/MyDrive/edgentrag_services/run_colab.sh</code>
          </li>
          <li>Copy the three addresses it prints at the end.</li>
        </ol>
        <p className="setup-note">
          Tip: copy all three lines at once and paste into any box below — they
          will be split into the right fields.
        </p>
      </details>

      <form onSubmit={handleConnect}>
        {FIELDS.map(({ key, label, port, hint }) => (
          <label className="setup-field" key={key}>
            <span className="setup-field-label">
              {label}
              <span className="setup-field-port">:{port}</span>
              {config?.healthy?.[key] && <span className="dot good" />}
            </span>
            <input
              type="url"
              inputMode="url"
              spellCheck="false"
              autoComplete="off"
              placeholder="https://something-random.trycloudflare.com"
              value={urls[key]}
              onPaste={handlePasteAll}
              onChange={(e) => setUrls({ ...urls, [key]: e.target.value })}
            />
            <span className="setup-field-hint">{hint}</span>
          </label>
        ))}

        {error && (
          <div className="error setup-error">
            <strong>Could not connect</strong>
            <span>{error}</span>
          </div>
        )}

        <button className="primary" type="submit" disabled={!allFilled || busy}>
          {busy ? 'Checking all three…' : 'Connect'}
        </button>
      </form>

      <p className="setup-footnote">
        Each address is checked before it is saved, so if this button fails the
        URL is wrong or the Colab runtime has stopped.
      </p>
    </section>
  )
}
