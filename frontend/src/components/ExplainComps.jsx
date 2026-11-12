import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './shared.jsx'
import { fmtPrice } from './ListingsShell.jsx'

// Drivers (TreeSHAP of the bundle's tree members) and comparable listings for one listing, on demand.
// Every figure is an ASKING price: the model predicts asking prices and comps are other listings' asking prices.

const fmtJmd = (v) => (v == null ? '—' : fmtPrice(v, 'JMD'))
const fmtM = (v) => (v == null ? '—' : `J$${(v / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1, minimumFractionDigits: 1 })}M`)
export const fmtPct = (p, digits = 1) => {
  if (p == null) return '—'
  const v = Number((p * 100).toFixed(digits))
  return `${v < 0 ? '−' : v > 0 ? '+' : ''}${Math.abs(v).toFixed(digits)}%`
}
const num = (v) => (v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: 0 }))

function errorText(e) {
  const m = /^\d{3} (.*)$/s.exec(e?.message || '')
  if (!m) return e?.message || 'request failed'
  try { return JSON.parse(m[1]).detail || m[1] } catch { return m[1] }
}

// Bar width for an effect, relative to the largest one shown.
export function barWidth(pct, maxAbs) {
  if (pct == null || !maxAbs) return 0
  return Math.min(100, (Math.abs(Math.log1p(pct)) / maxAbs) * 100)
}

function Drivers({ data }) {
  const shown = data.drivers.filter((d) => Math.abs(d.log_effect ?? 0) >= 0.0005)
  const maxAbs = Math.max(...shown.map((d) => Math.abs(d.log_effect ?? 0)), 1e-9)
  return (
    <div>
      <div className="text-xs text-inkmute mb-1">
        From the model's baseline <span className="figure">{fmtM(data.base_jmd)}</span> (a typical training listing) to
        its estimate <span className="figure">{fmtM(data.pred_jmd)}</span>: each driver multiplies the asking price, and the
        effects multiply up exactly to the estimate.
      </div>
      <table className="w-full text-xs">
        <thead className="text-inkmute text-left"><tr><th className="py-1">Driver</th><th>This property</th><th className="w-20 text-right">Effect</th><th className="w-40" /></tr></thead>
        <tbody>
          {shown.map((d) => (
            <tr key={d.group} className="border-t border-line align-top" title={d.top.map((t) => `${t.label} ${fmtPct(t.pct_effect)}`).join(', ')}>
              <td className="py-1 pr-2">{d.group}</td>
              <td className="pr-2 text-inkmute">{d.value_text || '—'}</td>
              <td className={`figure text-right ${d.pct_effect < 0 ? 'text-flag' : 'text-teal'}`}>{fmtPct(d.pct_effect)}</td>
              <td className="pl-2">
                <div className="h-2 bg-line/40 rounded-sm">
                  <div className={`h-2 rounded-sm ${d.pct_effect < 0 ? 'bg-flag/60' : 'bg-teal/60'}`} style={{ width: `${barWidth(d.pct_effect, maxAbs)}%` }} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Comps({ comps, predJmd }) {
  const [grid, setGrid] = useState(false)
  if (!comps.available) return <div className="text-xs text-inkmute">{comps.message}</div>
  if (comps.comps.length === 0) return <div className="text-xs text-inkmute">No comparable listings. {comps.notes.join('; ')}</div>
  const groups = [...new Set(comps.grid.map((g) => g.group))]
  const cell = (i, grp) => comps.grid.find((g) => g.comp === i && g.group === grp)
  return (
    <div>
      <table className="w-full text-xs">
        <thead className="text-inkmute text-left">
          <tr><th className="py-1">Listing</th><th>Status</th><th>Listed</th><th>Bed / bath / sq ft</th><th className="text-right">km</th><th className="text-right">Similarity</th><th className="text-right">Asking</th><th className="text-right">Adj.</th><th className="text-right">Adjusted</th><th className="text-right">Gross</th></tr>
        </thead>
        <tbody>
          {comps.comps.map((c) => (
            <tr key={c.key} className="border-t border-line" title={c.match}>
              <td className="py-1 pr-2">
                {c.listing_url ? <a className="underline" href={c.listing_url} target="_blank" rel="noreferrer">{c.key.split('/').pop()}</a> : c.key}
                {c.same_building && <span className="ml-1 text-inkmute">(same building)</span>}
                {!c.match.startsWith('same building or community, 12') && <div className="text-[10px] text-amber">{c.match}</div>}
              </td>
              <td className="pr-2">{(c.listing_status || '—').replace('_', ' ')}</td>
              <td className="pr-2 figure">{c.listing_date || '—'}</td>
              <td className="pr-2 figure">{num(c.bedrooms)} / {num(c.bathrooms)} / {num(c.floor_area_sqft)}</td>
              <td className="figure text-right">{c.distance_km == null ? '—' : c.distance_km.toFixed(1)}</td>
              <td className="figure text-right">{c.proximity == null ? '—' : c.proximity.toFixed(2)}</td>
              <td className="figure text-right">{fmtM(c.price_jmd)}</td>
              <td className="figure text-right">{fmtPct(c.adjustment_pct)}</td>
              <td className="figure text-right font-medium">{fmtM(c.adjusted_jmd)}</td>
              <td className={`figure text-right ${c.gross_adjustment_pct > 0.25 ? 'text-flag' : ''}`}>{fmtPct(c.gross_adjustment_pct, 0).replace('+', '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-xs mt-2">
        Comps-indicated asking price (similarity-weighted): <span className="figure font-medium">{fmtM(comps.indicated_jmd)}</span>
        {' '}against the model's <span className="figure">{fmtM(predJmd)}</span> · {comps.candidates} candidate(s) passed the rules
        {' '}<button className="ml-2 underline text-inkmute" onClick={() => setGrid(!grid)}>{grid ? 'hide' : 'show'} adjustment grid</button>
      </div>
      {comps.notes.length > 0 && <div className="text-xs text-amber mt-1">{comps.notes.join('; ')}</div>}
      {grid && (
        <table className="w-full text-xs mt-2">
          <thead className="text-inkmute text-left"><tr><th className="py-1">Driver (comp → subject)</th>{comps.comps.map((c) => <th key={c.comp} className="text-right">#{c.comp}</th>)}</tr></thead>
          <tbody>
            {groups.map((grp) => (
              <tr key={grp} className="border-t border-line">
                <td className="py-1 pr-2">{grp}</td>
                {comps.comps.map((c) => {
                  const g = cell(c.comp, grp)
                  return <td key={c.comp} className="figure text-right">{g && Math.abs(g.jmd) >= 5e4 ? `${fmtPct(g.pct_effect)} (${(g.jmd / 1e6).toFixed(1)}M)` : '—'}</td>
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export default function ExplainComps({ recordId, model }) {
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [started, setStarted] = useState(null)
  const run = useRef(0)
  useEffect(() => { run.current += 1; setData(null); setErr(null); setBusy(false) }, [recordId, model])

  const go = async () => {
    const mine = ++run.current
    setBusy(true); setErr(null); setData(null); setStarted(Date.now())
    try {
      const d = await api.explainListing({ record_id: recordId, model })
      if (run.current === mine) setData(d)
    } catch (e) {
      if (run.current === mine) setErr(errorText(e))
    } finally {
      if (run.current === mine) setBusy(false)
    }
  }

  return (
    <div className="bg-paper border border-line rounded-sm p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="label">Drivers and comparable listings</div>
        <button onClick={go} disabled={busy} className="px-2 py-0.5 text-xs border border-teal text-teal rounded-sm disabled:opacity-50">
          {data ? 'Again' : `Explain ${model}`}
        </button>
      </div>
      {!data && !busy && !err && (
        <div className="text-xs text-inkmute mt-1">
          Why the model gave this asking-price estimate (exact TreeSHAP), and 3-5 comparable listings adjusted to this one by
          the model. About 10 s; the first time the scorer also prepares the trees and the comps pool (about 30 s).
        </div>
      )}
      {busy && <Spinner label={`Explaining… (started ${new Date(started).toLocaleTimeString()})`} />}
      {err && <div className="text-xs text-flag mt-1">{err}</div>}
      {data && (
        <div className="space-y-3 mt-2">
          <Drivers data={data} />
          <div className="label">Comparable listings (asking prices, not sales)</div>
          <Comps comps={data.comps} predJmd={data.pred_jmd} />
          <div className="text-[11px] text-inkmute">
            Model v{data.model_version} ({data.point_model}{data.point_model === 'stage3_jev' ? '; the explorer has no photo scores, so photo condition counts as missing' : ''}).
            Took {data.elapsed_s} s (drivers {data.seconds?.drivers} s, comps {data.seconds?.comps} s). Sum check gap {data.check_gap?.toExponential(1)} in log price.
            No time adjustment: the model has no time input.
          </div>
        </div>
      )}
    </div>
  )
}
