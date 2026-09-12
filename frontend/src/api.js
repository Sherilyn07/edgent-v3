// Every call the frontend makes to the API.
//
// VITE_API_BASE is /api in a production build (see frontend/.env.production),
// so the browser talks to the same origin it was served from and nginx
// forwards it. Same origin means CORS never comes into it.
//
// New in v3: every call now carries a Cognito ID token as
// `Authorization: Bearer <token>` -- added in exactly one place, `request()`,
// because every function below already funnels through it. Two exceptions,
// both deliberate:
//
//   uploadToStorage()   PUTs straight to a presigned S3 URL. It must NOT get
//                        this header -- Authorization is not part of what S3
//                        signed, and adding it would break the upload.
//   eventsUrl()          a plain browser EventSource cannot send headers at
//                        all. See useSessionEvents.js and getEventsTicket()
//                        below for how the stream is authenticated instead.

import { getIdToken } from './auth'

const BASE = (import.meta.env.VITE_API_BASE || 'http://localhost:8000').replace(/\/$/, '')

async function request(path, options = {}) {
  const token = await getIdToken()
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(`${BASE}${path}`, { ...options, headers })

  if (!response.ok) {
    let detail = await response.text()
    try {
      detail = JSON.parse(detail).detail || detail
    } catch {
      /* not JSON; use the raw body */
    }
    throw new Error(`${response.status} — ${detail}`)
  }

  return response.status === 204 ? null : response.json()
}

/** Which of the five processes are actually running. */
export function getHealth() {
  return request('/health')
}

/**
 * Where the three GPU services currently live, and whether they answer.
 *
 * Admin-only in v3 -- see shared/auth.py::require_admin. A non-admin call
 * fails with 403, which ServiceStrip/ServiceSetup should treat the same as
 * "not configured yet" rather than an error to show.
 */
export function getServiceConfig() {
  return request('/config/services')
}

export function setServiceConfig(urls) {
  return request('/config/services', {
    method: 'PUT',
    body: JSON.stringify(urls),
  })
}

export function createSession() {
  return request('/sessions', { method: 'POST' })
}

/** Ask for one upload URL per file. Returns { targets: [...] }. */
export function presignUploads(sessionId, files) {
  return request(`/sessions/${sessionId}/uploads`, {
    method: 'POST',
    body: JSON.stringify({
      files: files.map((file) => ({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size: file.size,
      })),
    }),
  })
}

/**
 * PUT one file straight to storage.
 *
 * XHR rather than fetch, because fetch cannot report upload progress. The URL
 * is a presigned S3 link from the backend -- deliberately no Authorization
 * header here; see the file-level comment above.
 */
export function uploadToStorage(file, url, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream')

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`upload failed with ${xhr.status}`))
    xhr.onerror = () => reject(new Error('upload failed — is the API running?'))

    xhr.send(file)
  })
}

/** Tell the backend the uploads are done, and start processing. */
export function registerFiles(sessionId, fileIds) {
  return request(`/sessions/${sessionId}/files/register`, {
    method: 'POST',
    body: JSON.stringify({ files: fileIds.map((file_id) => ({ file_id })) }),
  })
}

export function getStatus(sessionId) {
  return request(`/sessions/${sessionId}/status`)
}

export function postChat(sessionId, content) {
  return request(`/sessions/${sessionId}/chat`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  })
}

/**
 * A short-lived ticket for the event stream, exchanged for a real request the
 * normal, authenticated way. See useSessionEvents.js -- a plain EventSource
 * cannot carry a header, so it carries this instead, as a query parameter.
 */
export function getEventsTicket(sessionId) {
  return request(`/sessions/${sessionId}/events/ticket`, { method: 'POST' })
}

/** The event stream URL for a session, ticket included. */
export function eventsUrl(sessionId, ticket) {
  return `${BASE}/sessions/${sessionId}/events?ticket=${encodeURIComponent(ticket)}`
}

export function getMessages(sessionId) {
  return request(`/sessions/${sessionId}/chat`)
}
