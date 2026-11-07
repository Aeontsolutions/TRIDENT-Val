import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './shared.jsx'
import { fmtPrice } from './ListingsShell.jsx'

const SLOTS = ['champion', 'challenger']
const SLOT_LABEL = { champion: 'Champion', challenger: 'Challenger' }
// Classes spelled out in full so Tailwind sees them.
const SLOT_STYLE = {
  champion: { bg: 'bg-teal', text: 'text-teal' },
  challenger: { bg: 'bg-amber', text: 'text-amber' },
  asking: { bg: 'bg-flag', text: 'text-flag' },
}

// ---------- pure helpers ----------

// Where things sit on the price line, as percentages of the line's width. The line runs from 90% of the
// lowest value to 110% of the highest, over {asking, each ok slot's 90% range}. Returns null when there is
// nothing to draw. `pct(x)` maps a JMD value to its position.
export function priceLineLayout(askingJmd, slots) {
  const ok = SLOTS.filter((n) => slots?.[n]?.status === 'ok' && slots[n].pred_price_jmd != null)
  const values = [askingJmd]
  ok.forEach((n) => values.push(slots[n].lower_90_jmd, slots[n].upper_90_jmd, slots[n].pred_price_jmd))
  const known = values.filter((v) => v != null && Number.isFinite(v))
  if (ok.length === 0 || known.length === 0) return null
  const lo = Math.min(...known) * 0.9
  const hi = Math.max(...known) * 1.1
  const pct = (x) => ((x - lo) / (hi - lo)) * 100
  const bands = []
  const markers = []
  ok.forEach((n) => {
    const s = slots[n]
    if (s.lower_90_jmd != null && s.upper_90_jmd != null) {
      bands.push({ key: n, left: pct(s.lower_90_jmd), width: pct(s.upper_90_jmd) - pct(s.lower_90_jmd) })
    }
    markers.push({ key: n, pos: pct(s.pred_price_jmd), text: `${SLOT_LABEL[n]}${s.gap_pct == null ? '' : ` ${fmtGap(s.gap_pct, 0)}`}` })
  })
  if (askingJmd != null) markers.push({ key: 'asking', pos: pct(askingJmd), text: 'Asking' })
  // Alternate labels above and below the line, left to right, so neighbours do not overprint.
  markers.sort((a, b) => a.pos - b.pos).forEach((m, i) => { m.below = i % 2 === 1 })
  return { bands, markers }
}

export const fmtGap = (g, digits = 1) => {
  if (g == null) return '—'
  const v = Number(g.toFixed(digits))
  return `${v < 0 ? '−' : v > 0 ? '+' : ''}${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: digits })}%`
}

const fmtJmd = (v) => (v == null ? '—' : fmtPrice(v, 'JMD'))
const fmtRange = (s) => (s.lower_90_jmd == null || s.upper_90_jmd == null ? '—' : `${fmtJmd(s.lower_90_jmd)} – ${fmtJmd(s.upper_90_jmd).replace('JMD ', '')}`)

// api.js throws `${status} ${body}`; the backend's body is {"detail": "..."}.
function errorText(e) {
  const m = /^\d{3} (.*)$/s.exec(e?.message || '')
  if (!m) return e?.message || 'request failed'
  try { return JSON.parse(m[1]).detail || m[1] } catch { return m[1] }
}

const fmtInput = (v) => (typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v))

// ---------- pieces ----------

function PriceLine({ layout }) {
  const edge = (pos) => (pos < 12 ? { left: `${pos}%` } : pos > 88 ? { right: `${100 - pos}%` } : { left: `${pos}%`, transform: 'translateX(-50%)' })
  return (
    <div className="relative h-14 mt-3 mb-1">
      <div className="absolute left-0 right-0 top-7 h-0.5 bg-line" />
      {layout.bands.map((b) => (
        <div key={b.key} className={`absolute top-[23px] h-2.5 rounded-full opacity-30 ${SLOT_STYLE[b.key].bg}`}
          style={{ left: `${b.left}%`, width: `${b.width}%` }} title={`${SLOT_LABEL[b.key]} 90% range`} />
      ))}
      {layout.markers.map((m) => (
        <React.Fragment key={m.key}>
          <div className={`absolute top-[23px] w-3 h-3 -ml-1.5 ${SLOT_STYLE[m.key].bg} ${m.key === 'asking' ? 'rounded-sm' : 'rounded-full'}`}
            style={{ left: `${m.pos}%` }} />
          <div className={`absolute text-[11px] whitespace-nowrap ${SLOT_STYLE[m.key].text} ${m.below ? 'top-[42px]' : 'top-0'}`} style={edge(m.pos)}>
            {m.text}
          </div>
        </React.Fragment>
      ))}
    </div>
  )
}

function SlotRow({ name, slot, registryError, askingNote }) {
  const label = <td className={`py-1.5 font-medium ${SLOT_STYLE[name].text}`}>{SLOT_LABEL[name]}</td>
  if (slot.status === 'ok') {
    return (
      <tr className="border-t border-line">
        {label}
        <td className="figure text-xs">{fmtJmd(slot.pred_price_jmd)}</td>
        <td className="figure text-xs">{fmtRange(slot)}</td>
        <td className="figure text-xs">{slot.gap_pct == null ? <span className="text-inkmute" title={askingNote || ''}>no gap</span> : fmtGap(slot.gap_pct)}</td>
      </tr>
    )
  }
  const text = slot.status === 'not_registered' ? `no ${name} registered`
    : registryError ? 'no result (see the message above)' : slot.message || 'no result'
  return (
    <tr className="border-t border-line">
      {label}
      <td colSpan={3} className={`text-xs ${slot.status === 'error' && !registryError ? 'text-flag' : 'text-inkmute'}`}>{text}</td>
    </tr>
  )
}

function Result({ res, recordId }) {
  const { asking, slots } = res
  const shown = SLOTS.filter((n) => slots?.[n] && slots[n].status !== 'skipped')
  let askingNote = null
  if (asking.amount == null) askingNote = 'no asking price to compare'
  else if (asking.jmd == null) {
    askingNote = asking.currency
      ? `${asking.currency} asking price: no JMD rate available for this listing, so no gap is shown`
      : 'Asking currency unknown: no JMD price available for this listing, so no gap is shown'
  }
  const layout = priceLineLayout(asking.jmd, slots)
  const labels = shown.filter((n) => slots[n].status === 'ok' && slots[n].label).map((n) => `${SLOT_LABEL[n].toLowerCase()} ${slots[n].label}`)
  const scoredAt = res.scored_at && !Number.isNaN(Date.parse(res.scored_at)) ? new Date(res.scored_at).toLocaleString() : null
  const scoredLine = [labels.length > 0 ? `from ${labels.join(' and ')}` : null, scoredAt ? `at ${scoredAt}` : null].filter(Boolean).join(' ')
  return (
    <div className="mt-3 space-y-3">
      {res.registry_error && (
        <div className="text-xs border border-amber/40 bg-amber/10 rounded-sm p-2">{res.registry_error}</div>
      )}
      {layout && <PriceLine layout={layout} />}
      <table className="w-full text-sm">
        <thead className="text-inkmute text-xs text-left">
          <tr><th className="py-1 w-28">Model</th><th>Estimate</th><th>90% range</th><th className="w-28">vs asking</th></tr>
        </thead>
        <tbody>
          {shown.map((n) => <SlotRow key={n} name={n} slot={slots[n]} registryError={res.registry_error} askingNote={askingNote} />)}
          <tr className="border-t border-line">
            <td className="py-1.5 font-medium text-flag">Asking</td>
            <td colSpan={3} className="figure text-xs">
              {asking.amount == null ? <span className="text-inkmute font-sans">no asking price to compare</span>
                : asking.jmd == null ? fmtPrice(asking.amount, asking.currency)
                  : <>{fmtJmd(asking.jmd)}{asking.currency !== 'JMD' && <span className="text-inkmute"> (from {fmtPrice(asking.amount, asking.currency)})</span>}</>}
            </td>
          </tr>
        </tbody>
      </table>
      {askingNote && asking.amount != null && <div className="text-xs text-inkmute">{askingNote}</div>}

      <div className="grid md:grid-cols-2 gap-3">
        <div className="bg-paper border border-line rounded-sm p-3">
          <div className="label mb-1">Inputs fed to the model</div>
          {res.inputs.length === 0 ? <div className="text-xs text-inkmute">No inputs were built for this listing.</div> : (
            <div className="max-h-72 overflow-y-auto">
            <table className="w-full text-xs">
              <tbody>
                {res.inputs.map((i) => (
                  <tr key={i.name} className="border-t border-line first:border-t-0">
                    <td className="py-1 pr-2 text-inkmute">{i.name}</td>
                    <td className={`figure ${i.flag === 'outlier' ? 'text-flag font-medium' : ''}`}>
                      {i.value == null ? <span className="text-inkmute font-sans">missing</span> : fmtInput(i.value)}
                      {i.flag === 'outlier' && <span className="ml-2 px-1.5 py-0.5 text-[10px] font-sans bg-flag/10 text-flag border border-flag/30 rounded-sm">outlier</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
          {res.input_source && <div className="text-[11px] text-inkmute mt-2">Inputs built from: {res.input_source}</div>}
        </div>
        <div className="bg-paper border border-line rounded-sm p-3">
          <div className="label mb-1">Warnings</div>
          {res.warnings.length === 0 ? <div className="text-xs text-inkmute">None.</div> : (
            <ul className="text-xs space-y-1.5 list-disc pl-4">
              {res.warnings.map((w, idx) => <li key={`${idx}:${w.code}`}>{w.message}</li>)}
            </ul>
          )}
        </div>
      </div>

      <div className="text-[11px] text-inkmute">
        Asking-price estimate, JMD.{scoredLine && ` Scored ${scoredLine}.`}
      </div>
    </div>
  )
}

// ---------- the section ----------

export default function ModelEstimate({ recordId, askingAmount, currency }) {
  const [reg, setReg] = useState(null) // /versions response
  const [regErr, setRegErr] = useState(null)
  const [pick, setPick] = useState({ champion: 'champion', challenger: 'challenger' })
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState(null)
  const current = useRef(recordId)

  useEffect(() => {
    api.modelVersions().then(setReg).catch((e) => setRegErr(errorText(e)))
  }, [])
  useEffect(() => { current.current = recordId; setRes(null); setErr(null); setBusy(false) }, [recordId])

  const score = async () => {
    setBusy(true); setErr(null); setRes(null)
    try {
      const out = await api.scoreListing({ record_id: recordId, champion: pick.champion, challenger: pick.challenger || null })
      if (current.current === recordId) setRes(out)
    } catch (e) {
      if (current.current === recordId) setErr(errorText(e))
    } finally {
      if (current.current === recordId) setBusy(false)
    }
  }

  const aliases = reg?.aliases || {}
  const options = (slot) => (
    <>
      {/* Until /versions answers (or if it fails), fall back to this slot's default alias only. */}
      {(reg ? [...new Set([...SLOTS, ...Object.keys(aliases)])] : [slot]).map((a) => <option key={`a-${a}`} value={a}>{a} (alias{aliases[a] ? ` → v${aliases[a]}` : reg ? ', not set' : ''})</option>)}
      {(reg?.versions || []).map((v) => (
        <option key={v.version} value={v.version}>v{v.version}{v.aliases.length ? ` (${v.aliases.join(', ')})` : ''}</option>
      ))}
      {slot === 'challenger' && <option value="">none</option>}
    </>
  )
  const sel = 'border border-line rounded-sm px-2 py-1 bg-white text-sm'
  const unsupported = res && res.supported === false

  return (
    <div className="card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="label">Model estimate</div>
        {!unsupported && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {SLOTS.map((n) => (
              <label key={n} className="flex items-center gap-1 text-inkmute">
                {SLOT_LABEL[n]}
                <select className={sel} value={pick[n]} disabled={busy} onChange={(e) => setPick({ ...pick, [n]: e.target.value })}>
                  {options(n)}
                </select>
              </label>
            ))}
            <button onClick={score} disabled={busy}
              className="px-3 py-1 bg-teal text-white rounded-sm disabled:opacity-50">Score this listing</button>
          </div>
        )}
      </div>
      {regErr && !res?.registry_error && <div className="mt-2 text-xs border border-amber/40 bg-amber/10 rounded-sm p-2">{regErr}</div>}
      {busy && <Spinner label="Scoring… the first run loads the model and can take a minute" />}
      {err && <div className="mt-2 text-sm text-flag">Could not score this listing: {err}</div>}
      {!res && !busy && !err && !regErr && (
        <div className="mt-2 text-xs text-inkmute">Scores this listing with the registered listing-price model. Read only; nothing is written.</div>
      )}
      {unsupported && (
        <div className="mt-2 text-sm">
          <div>{res.unsupported_reason}</div>
          {res.asking.amount != null && <div className="text-xs text-inkmute mt-1">Asking price on the page: <span className="figure">{fmtPrice(res.asking.amount, res.asking.currency)}</span></div>}
        </div>
      )}
      {res && !unsupported && <Result res={res} recordId={recordId} />}
    </div>
  )
}
