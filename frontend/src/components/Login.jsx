import { useState } from 'react'
import * as auth from '../auth'

// The gate in front of everything else. Nothing in the app runs until
// somebody is signed in -- v3's whole reason for Cognito is that a session id
// used to be the only "identity" anything checked (see V3_DESIGN.html's
// discussion of it), and this is the screen that closes that gap.
//
// Three small forms in one component rather than three files, because they
// share so much state (email, password, error, busy) that splitting them
// would mean passing all of it down anyway.

const MODE_SIGN_IN = 'sign-in'
const MODE_SIGN_UP = 'sign-up'
const MODE_CONFIRM = 'confirm'

export default function Login({ onSignedIn }) {
  const [mode, setMode] = useState(MODE_SIGN_IN)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleSignIn(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await auth.signIn(email.trim(), password)
      onSignedIn()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  async function handleSignUp(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await auth.signUp(email.trim(), password)
      setMode(MODE_CONFIRM)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirm(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await auth.confirmSignUp(email.trim(), code.trim())
      // Confirmed, but not yet signed in -- Cognito treats those as separate
      // steps. Send them back to the sign-in form with the password they
      // already typed still in memory, so this does not feel like starting over.
      setMode(MODE_SIGN_IN)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card setup-panel">
      <h2>{mode === MODE_SIGN_UP ? 'Create an account' : mode === MODE_CONFIRM ? 'Check your email' : 'Sign in'}</h2>

      {mode === MODE_SIGN_IN && (
        <form onSubmit={handleSignIn}>
          <label className="setup-field">
            <span className="setup-field-label">Email</span>
            <input type="email" required autoComplete="username"
                  value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="setup-field">
            <span className="setup-field-label">Password</span>
            <input type="password" required autoComplete="current-password"
                  value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>

          {error && (
            <div className="error setup-error">
              <strong>Could not sign in</strong>
              <span>{error}</span>
            </div>
          )}

          <button className="primary" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <p className="setup-footnote">
            No account yet?{' '}
            <button type="button" className="linklike" onClick={() => { setMode(MODE_SIGN_UP); setError(null) }}>
              Create one
            </button>
          </p>
        </form>
      )}

      {mode === MODE_SIGN_UP && (
        <form onSubmit={handleSignUp}>
          <label className="setup-field">
            <span className="setup-field-label">Email</span>
            <input type="email" required autoComplete="username"
                  value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="setup-field">
            <span className="setup-field-label">Password</span>
            <input type="password" required autoComplete="new-password" minLength={8}
                  value={password} onChange={(e) => setPassword(e.target.value)} />
            <span className="setup-field-hint">At least 8 characters.</span>
          </label>

          {error && (
            <div className="error setup-error">
              <strong>Could not create the account</strong>
              <span>{error}</span>
            </div>
          )}

          <button className="primary" type="submit" disabled={busy}>
            {busy ? 'Creating…' : 'Create account'}
          </button>
          <p className="setup-footnote">
            Already have one?{' '}
            <button type="button" className="linklike" onClick={() => { setMode(MODE_SIGN_IN); setError(null) }}>
              Sign in
            </button>
          </p>
        </form>
      )}

      {mode === MODE_CONFIRM && (
        <form onSubmit={handleConfirm}>
          <p className="setup-lead">We sent a code to {email}. Enter it below.</p>
          <label className="setup-field">
            <span className="setup-field-label">Confirmation code</span>
            <input type="text" inputMode="numeric" required
                  value={code} onChange={(e) => setCode(e.target.value)} />
          </label>

          {error && (
            <div className="error setup-error">
              <strong>Could not confirm</strong>
              <span>{error}</span>
            </div>
          )}

          <button className="primary" type="submit" disabled={busy}>
            {busy ? 'Confirming…' : 'Confirm'}
          </button>
        </form>
      )}
    </section>
  )
}
