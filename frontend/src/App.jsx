import React, { useEffect, useState } from 'react'
import DataProfile from './views/DataProfile.jsx'
import ListingsQuality from './views/ListingsQuality.jsx'
import ListingsGrid from './views/ListingsGrid.jsx'
import ListingsDistributions from './views/ListingsDistributions.jsx'
import ListingsMap from './views/ListingsMap.jsx'
import ListingsRuns from './views/ListingsRuns.jsx'
import ListingsFlags from './views/ListingsFlags.jsx'
import ListingInspector from './views/ListingInspector.jsx'
import { ListingsShell } from './components/ListingsShell.jsx'
import Login from './views/Login.jsx'
import { Spinner } from './components/shared.jsx'
import { logger } from './logger.js'
import { api, setUnauthorizedHandler } from './api.js'

const BASE_TABS = [
  { id: 'listings-quality', label: 'Listings' },
]

function tabsForRole(role) {
  const tabs = [...BASE_TABS]
  if (role === 'Admin') tabs.push({ id: 'data-profile', label: 'Data Profile' })
  return tabs
}

const VIEW_IDS = new Set([
  'data-profile', 'listings', 'listings-quality', 'listings-distributions', 'listings-map',
  'listings-runs', 'listings-flags',
])

const LISTINGS_VIEWS = new Set(['listings-quality', 'listings', 'listings-distributions', 'listings-map',
  'listings-runs', 'listings-flags'])

function stateToPath({ view, pid }) {
  if (view === 'listing' && pid != null) return `/listing/${encodeURIComponent(pid)}`
  return `/${view}`
}

function stateFromLocation() {
  const segments = window.location.pathname.split('/').filter(Boolean)
  const [head, arg] = segments
  if (head === 'listing' && arg) return { view: 'listing', pid: decodeURIComponent(arg) }
  if (VIEW_IDS.has(head)) return { view: head, pid: null }
  return { view: 'listings-quality', pid: null }
}

export default function App() {
  const [{ view, pid }, setNavState] = useState(stateFromLocation)
  const [listingFilters, setListingFilters] = useState({})
  // undefined = checking for an existing session, null = logged out, object = logged in
  const [user, setUser] = useState(undefined)

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null))
    api.me().then(setUser).catch(() => setUser(null))
  }, [])

  // Always land on the Listings overview right after signing in, regardless of whatever
  // path/view was in the URL before login (e.g. a bookmarked deep link).
  const handleLoggedIn = (loggedInUser) => {
    setUser(loggedInUser)
    navigate({ view: 'listings-quality', pid: null }, { replace: true })
  }

  const handleLogout = async () => {
    logger.track('auth', `Logged out '${user.username}'.`)
    try { await api.logout() } catch { /* session is being cleared client-side regardless */ }
    setUser(null)
  }

  // Pushes a new browser history entry so the back/forward buttons move between
  // views instead of leaving every page stuck on the initial "/" entry.
  // The history mutation happens here, outside the setState updater — React's
  // StrictMode double-invokes updater functions in dev, which would otherwise
  // push two history entries per navigation.
  const navigate = (next, { replace = false } = {}) => {
    const merged = { view, pid, ...next }
    const path = stateToPath(merged)
    if (replace) window.history.replaceState(merged, '', path)
    else window.history.pushState(merged, '', path)
    setNavState(merged)
  }

  useEffect(() => {
    // Normalize the initial URL (e.g. bare "/") without adding an extra history entry.
    const initial = stateFromLocation()
    window.history.replaceState(initial, '', stateToPath(initial))

    const onPopState = () => setNavState(stateFromLocation())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const openTab = (id) => {
    logger.track('app', `Switched to "${id}" view.`)
    navigate({ view: id, pid: null })
  }

  return (
    <div className="min-h-screen">
      <header className="bg-ink text-white">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center gap-8">
          <div>
            <div className="font-semibold tracking-wide">TRIDENT-Val</div>
            <div className="text-[11px] text-white/60 tracking-wide">Listings data explorer · local validation tool</div>
          </div>
          {user && (
            <nav className="flex gap-1 ml-auto">
              {tabsForRole(user.role).map((t) => (
                <button key={t.id} onClick={() => openTab(t.id)}
                  className={`px-4 py-1.5 text-sm rounded-sm transition-colors ${
                    view === t.id
                      || (t.id === 'listings-quality' && (LISTINGS_VIEWS.has(view) || view === 'listing'))
                      ? 'bg-white/15 text-white' : 'text-white/70 hover:text-white'}`}>
                  {t.label}
                </button>
              ))}
            </nav>
          )}
          {user && (
            <div className="flex items-center gap-3 text-sm text-white/70">
              <span>{user.username} · {user.role}</span>
              <button onClick={handleLogout}
                className="text-white/70 hover:text-white underline underline-offset-2">
                Log out
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-6">
        {user === undefined && <Spinner label="Checking session…" />}
        {user === null && <Login onLoggedIn={handleLoggedIn} />}
        {user && (
          <>
            {view === 'data-profile' && <DataProfile />}
            {LISTINGS_VIEWS.has(view) && (
              <ListingsShell view={view} onNavigate={(id) => { if (id === 'listings') setListingFilters({}); openTab(id) }}>
                {view === 'listings-quality' && (
                  <ListingsQuality onFilter={(f) => { setListingFilters(f); openTab('listings') }}
                    onFlags={() => openTab('listings-flags')} />
                )}
                {view === 'listings-distributions' && (
                  <ListingsDistributions
                    onFilter={(f) => { setListingFilters(f); openTab('listings') }}
                    onOpen={(id) => navigate({ view: 'listing', pid: id })} />
                )}
                {view === 'listings-runs' && (
                  <ListingsRuns onOpen={(id) => navigate({ view: 'listing', pid: id })} />
                )}
                {view === 'listings-flags' && (
                  <ListingsFlags user={user} onOpen={(id) => navigate({ view: 'listing', pid: id })} />
                )}
                {view === 'listings-map' && (
                  <ListingsMap initialFilters={listingFilters}
                    onOpen={(id) => navigate({ view: 'listing', pid: id })} />
                )}
                {view === 'listings' && (
                  <ListingsGrid key={JSON.stringify(listingFilters)} initialFilters={listingFilters}
                    onMap={(f) => { setListingFilters(f); openTab('listings-map') }}
                    onOpen={(id) => navigate({ view: 'listing', pid: id })} />
                )}
              </ListingsShell>
            )}
            {view === 'listing' && (
              <ListingsShell view="listings" onNavigate={openTab}>
                <ListingInspector recordId={pid} user={user} onOpen={(id) => navigate({ view: 'listing', pid: id })} onBack={() => navigate({ view: 'listings', pid: null })} />
              </ListingsShell>
            )}
          </>
        )}
      </main>

      <footer className="max-w-7xl mx-auto px-6 py-4 text-[11px] text-inkmute border-t border-line">
        Scraped listings are asking prices from a single site, collected for data validation. Not valuations, and not for credit decisions.
      </footer>
    </div>
  )
}
