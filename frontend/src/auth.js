// Cognito sign-up/sign-in/token-refresh, and nothing else.
//
// amazon-cognito-identity-js rather than the full aws-amplify: it is the
// focused User Pool SDK, without Amplify's larger dependency tree and its own
// state/UI layer (Hub, DataStore) this app does not need. Everything else
// here stays the same hand-rolled React style as the rest of the app.
//
// The rest of the frontend touches this module through three functions:
// signUp/confirmSignUp/signIn to get a session going, signOut to end one, and
// getIdToken() -- called by api.js on every request and by
// useSessionEvents.js when it asks for an SSE ticket.

import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserAttribute,
  CognitoUserPool,
} from 'amazon-cognito-identity-js'

const REGION = import.meta.env.VITE_COGNITO_REGION || ''
const USER_POOL_ID = import.meta.env.VITE_COGNITO_USER_POOL_ID || ''
const CLIENT_ID = import.meta.env.VITE_COGNITO_APP_CLIENT_ID || ''

/**
 * Whether Cognito is actually configured for this build.
 *
 * The backend has the identical fallback (shared/auth.py::cognito_configured)
 * so a laptop can run the whole stack against local SQLite before a User Pool
 * exists, with no separate "local mode" flag anywhere.
 */
export function authConfigured() {
  return Boolean(REGION && USER_POOL_ID && CLIENT_ID)
}

let pool = null
function getPool() {
  if (!pool) pool = new CognitoUserPool({ UserPoolId: USER_POOL_ID, ClientId: CLIENT_ID })
  return pool
}

function currentUser() {
  return getPool().getCurrentUser()
}

/** Create an account. Cognito emails a confirmation code. */
export function signUp(email, password) {
  return new Promise((resolve, reject) => {
    const attributes = [new CognitoUserAttribute({ Name: 'email', Value: email })]
    getPool().signUp(email, password, attributes, null, (err, result) => {
      if (err) reject(err)
      else resolve(result)
    })
  })
}

/** Finish creating the account with the code Cognito emailed. */
export function confirmSignUp(email, code) {
  return new Promise((resolve, reject) => {
    const user = new CognitoUser({ Username: email, Pool: getPool() })
    user.confirmRegistration(code, true, (err, result) => {
      if (err) reject(err)
      else resolve(result)
    })
  })
}

/** Sign in. Resolves once a session (and its tokens) is established. */
export function signIn(email, password) {
  return new Promise((resolve, reject) => {
    const user = new CognitoUser({ Username: email, Pool: getPool() })
    const details = new AuthenticationDetails({ Username: email, Password: password })
    user.authenticateUser(details, {
      onSuccess: (session) => resolve(session),
      onFailure: (err) => reject(err),
    })
  })
}

export function signOut() {
  const user = currentUser()
  if (user) user.signOut()
}

/**
 * The current ID token, refreshing it first if it is close to expiring.
 *
 * Returns null when nobody is signed in (or Cognito is not configured), which
 * is what tells api.js to send no Authorization header at all rather than a
 * broken one.
 */
export function getIdToken() {
  return new Promise((resolve) => {
    const user = currentUser()
    if (!user) {
      resolve(null)
      return
    }
    user.getSession((err, session) => {
      if (err || !session || !session.isValid()) {
        resolve(null)
        return
      }
      resolve(session.getIdToken().getJwtToken())
    })
  })
}

/** The signed-in user's email, or null. Used only for display. */
export function currentEmail() {
  const user = currentUser()
  return user ? user.getUsername() : null
}
