import { logger } from './logger.js'

const BASE = '/api/v1'

// Set by App.jsx so a session that expires mid-use (401 on any call) can bounce
// the user back to the login screen instead of surfacing a raw fetch error.
let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

async function get(path) {
  const start = performance.now()
  let r
  try {
    r = await fetch(`${BASE}${path}`)
  } catch (e) {
    logger.error('api', `Network error fetching ${path}: ${e.message}`)
    throw e
  }
  const duration_ms = Math.round(performance.now() - start)
  if (!r.ok) {
    if (r.status === 401) onUnauthorized()
    const body = await r.text()
    logger.error('api', `${path} -> ${r.status} (${duration_ms}ms)`)
    throw new Error(`${r.status} ${body}`)
  }
  logger.debug('api', `${path} -> ${r.status} (${duration_ms}ms)`)
  return r.json()
}

async function send(method, path, body, failMessage) {
  let r
  try {
    r = await fetch(`${BASE}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (e) {
    logger.error('api', `Network error on ${method} ${path}: ${e.message}`)
    throw e
  }
  if (!r.ok) {
    if (r.status === 401) onUnauthorized()
    logger.error('api', `${method} ${path} -> ${r.status}`)
    throw new Error(failMessage)
  }
  return r.json()
}

export const api = {
  login: (username, password) => send('POST', '/auth/login', { username, password }, 'login failed'),
  logout: () => send('POST', '/auth/logout', {}, 'logout failed'),
  me: () => get('/auth/me'),
  dataProfiles: () => get('/reports/data-profile'),
  dataProfileUrl: (name) => `${BASE}/reports/data-profile/${encodeURIComponent(name)}`,
  listingsSummary: () => get('/listings/summary'),
  listingsFilters: () => get('/listings/filters'),
  listings: (params) => get(`/listings?${new URLSearchParams(params)}`),
  listingsDistributions: (params = {}) => get(`/listings/distributions?${new URLSearchParams(params)}`),
  listingsGeo: (params = {}) => get(`/listings/geo?${new URLSearchParams(params)}`),
  listing: (id) => get(`/listings/${encodeURIComponent(id)}`),
  listingsRuns: () => get('/listings/runs'),
  listingsRunDiff: (run_a, run_b) => get(`/listings/runs/diff?${new URLSearchParams({ run_a, run_b })}`),
  listingFlagsMeta: () => get('/listings/flags/meta'),
  listingFlagsSummary: () => get('/listings/flags/summary'),
  listingFlags: (params = {}) => get(`/listings/flags?${new URLSearchParams(params)}`),
  createListingFlag: (body) => send('POST', '/listings/flags', body, 'creating the flag failed'),
  resolveListingFlag: (id, resolution_note) =>
    send('PATCH', `/listings/flags/${id}/resolve`, { resolution_note }, 'resolving the flag failed'),
  deleteListingFlag: (id) => send('DELETE', `/listings/flags/${id}`, {}, 'deleting the flag failed'),
  listingFlagsExportUrl: (params = {}) => `${BASE}/listings/flags/export.csv?${new URLSearchParams(params)}`,
}

