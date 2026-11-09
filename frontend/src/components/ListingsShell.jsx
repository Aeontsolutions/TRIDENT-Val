import React from 'react'

// Persistent on every Listings view: the crawl only covers the top of one site's market.
export function CoverageBanner() {
  return (
    <div role="note" className="border border-amber/40 bg-amber/10 text-ink rounded-sm px-4 py-2.5 text-sm leading-relaxed">
      <span className="font-semibold text-amber">Coverage caveat · </span>
      Single-site crawl (cbjamaica.com) of the <b>top of the market only</b> — collected price-descending,
      ~540 of ~7,600 listings (the site 403s past page ~50). Prices are <b>asking prices, not sale prices</b>.
      Do not read these distributions as the whole market.
    </div>
  )
}

const SUBTABS = [
  { id: 'listings-quality', label: 'Quality overview' },
  { id: 'listings', label: 'Listings grid' },
  { id: 'listings-distributions', label: 'Distributions' },
  { id: 'listings-map', label: 'Map' },
  { id: 'listings-runs', label: 'Run comparison' },
  { id: 'listings-model', label: 'Model batch' },
  { id: 'listings-flags', label: 'Flags' },
]

export function ListingsShell({ view, onNavigate, children }) {
  return (
    <div className="space-y-4">
      <CoverageBanner />
      <div className="flex gap-1 border-b border-line">
        {SUBTABS.map((t) => (
          <button key={t.id} onClick={() => onNavigate(t.id)}
            className={`px-4 py-2 text-sm -mb-px border-b-2 ${
              view === t.id ? 'border-teal text-tealdeep font-medium' : 'border-transparent text-inkmute hover:text-ink'}`}>
            {t.label}
          </button>
        ))}
      </div>
      {children}
    </div>
  )
}

export const fmtNum = (n, d = 0) =>
  n == null ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d })

export const fmtPrice = (amount, currency) =>
  amount == null ? '—' : `${currency || ''} ${Number(amount).toLocaleString(undefined, { maximumFractionDigits: 0 })}`.trim()
