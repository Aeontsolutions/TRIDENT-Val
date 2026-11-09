import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from '../components/shared.jsx'
import { fmtNum, fmtPrice } from '../components/ListingsShell.jsx'
import { Elapsed, fmtGap } from '../components/ModelEstimate.jsx'

const SLOTS = ['champion', 'challenger']
const SLOT_LABEL = { champion: 'Champion', challenger: 'Challenger' }
const SLOT_TEXT = { champion: 'text-teal', challenger: 'text-amber' }
const POLL_MS = 2000

// ---------- pure helpers ----------

// api.js throws `${status} ${body}`; the backend's body is {"detail": "..."}.
function errorText(e) {
  const m = /^\d{3} (.*)$/s.exec(e?.message || '')
  if (!m) return e?.message || 'request failed'
  try { return JSON.parse(m[1]).detail || m[1] } catch { return m[1] }
}

const okSlot = (row, name) => (row.slots?.[name]?.status === 'ok' ? row.slots[name] : null)

// The biggest distance from asking, over the slots that have an estimate and a gap.
export function absGap(row) {
  const gaps = SLOTS.map((n) => okSlot(row, n)?.gap_pct).filter((g) => g != null)
  return gaps.length ? Math.max(...gaps.map(Math.abs)) : null
}

const SORTERS = {
  gap: absGap,
  ref: (r) => r.ref,
  type: (r) => r.property_type,
  parish: (r) => r.parish_input || r.parish,
  asking: (r) => r.asking?.jmd ?? null,
  champion: (r) => okSlot(r, 'champion')?.pred_price_jmd ?? null,
  challenger: (r) => okSlot(r, 'challenger')?.pred_price_jmd ?? null,
}

// Rows without a value for the sort column go last, whichever way it is sorted.
export function sortRows(rows, key, dir) {
  const get = SORTERS[key] || SORTERS.gap
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = get(a)
    const y = get(b)
    if (x == null && y == null) return 0
    if (x == null) return 1
    if (y == null) return -1
    if (typeof x === 'string' || typeof y === 'string') return sign * String(x).localeCompare(String(y))
    return sign * (x - y)
  })
}

// What a reader should know before trusting the table: counts of rows scored with less than the model normally sees.
export function batchNotes(rows) {
  const notes = []
  const noLocation = rows.filter((r) => (r.flags || []).includes('location_features_missing')).length
  if (noLocation > 0) {
    notes.push(`${fmtNum(noLocation)} of ${fmtNum(rows.length)} listings have no location features yet (distance to coast, elevation, nearby schools and roads): `
      + 'the geo and spatial feature tables have no row for them, so their estimates lean on the other fields.')
  }
  const noGap = rows.filter((r) => r.asking?.jmd == null && r.asking?.amount != null).length
  if (noGap > 0) {
    notes.push(`${fmtNum(noGap)} listings have no gap because their asking price has no JMD value: a USD listing outside the clean table is `
      + 'converted only if the Bank of Jamaica rates table (fx_rates_boj) has a rate within 5 days before it was fetched.')
  }
  return notes
}

// JMD amounts in millions keep the table narrow (the caption says the estimates are JMD).
const fmtM = (v) => (v == null ? '—' : Math.abs(v) >= 1e6 ? `${fmtNum(v / 1e6, 1)}M` : fmtNum(v))
const fmtRange = (s) => (s?.lower_90_jmd == null || s?.upper_90_jmd == null ? null
  : `${fmtNum(s.lower_90_jmd / 1e6, 1)}–${fmtNum(s.upper_90_jmd / 1e6, 1)}M`)
const mmss = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`

// ---------- pieces ----------

function GapText({ gap, threshold }) {
  if (gap == null) return <span className="text-inkmute">no gap</span>
  return <span className={Math.abs(gap) > threshold ? 'text-flag font-medium' : ''}>{fmtGap(gap)}</span>
}

function SlotCell({ slot, threshold }) {
  if (!slot || slot.status === 'skipped') return <span className="text-inkmute">—</span>
  if (slot.status === 'pending') return <span className="text-inkmute">…</span>
  if (slot.status === 'not_registered') return <span className="text-inkmute text-xs">not registered</span>
  if (slot.status === 'error') return <span className="text-flag text-xs" title={slot.message}>error</span>
  const range = fmtRange(slot)
  return (
    <div>
      <div className="figure text-xs whitespace-nowrap">{fmtM(slot.pred_price_jmd)} <GapText gap={slot.gap_pct} threshold={threshold} /></div>
      {range && <div className="text-[11px] text-inkmute figure whitespace-nowrap" title="90% range">{range}</div>}
    </div>
  )
}

function Progress({ job }) {
  const prep = job.progress?.prepare
  const stageText = {
    starting: 'Starting…',
    registry: 'Reading the model registry…',
    prepare: prep
      ? { pages: `Fetching the stored listing pages ${fmtNum(prep.done)} / ${fmtNum(prep.total)}`,
          lookups: 'Looking up the clean table and the location features…',
          inputs: `Building the input rows ${fmtNum(prep.done)} / ${fmtNum(prep.total)}` }[prep.stage] || 'Preparing the inputs…'
      : 'Preparing the inputs…',
    scoring: 'Scoring…',
    finished: '',
  }[job.stage] ?? ''
  return (
    <div className="card p-4 space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
        {job.status === 'running' && <span>{stageText}</span>}
        <span className="text-inkmute text-xs">
          {job.status === 'running' ? <>Running for <Elapsed since={job.started_at * 1000} /></> : `Took ${mmss(job.elapsed_s)}`}
        </span>
      </div>
      <div className="grid sm:grid-cols-2 gap-2">
        {SLOTS.map((n) => {
          const s = job.slots?.[n] || {}
          const text = {
            waiting: 'waiting',
            skipped: 'not included',
            not_registered: `no ${n} registered`,
            error: s.message || 'failed',
            done: s.started_at && s.finished_at ? `done in ${mmss(s.finished_at - s.started_at)}` : 'done',
          }[s.state]
          return (
            <div key={n} className="text-xs">
              <span className={`font-medium ${SLOT_TEXT[n]}`}>{SLOT_LABEL[n]}</span>{' '}
              {s.state === 'running'
                ? <>scoring… <Elapsed since={s.started_at * 1000} />{n === 'challenger' && ' · TabPFN on a CPU takes a few minutes whatever the number of listings'}</>
                : <span className={s.state === 'error' ? 'text-flag' : 'text-inkmute'}>{text}</span>}
              {s.version && <span className="text-inkmute"> · v{s.version}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function SummaryTable({ title, groups }) {
  if (!groups?.length) return null
  return (
    <div className="card p-4">
      <div className="label mb-2">{title}</div>
      <table className="w-full text-xs">
        <thead className="text-inkmute text-left">
          <tr><th className="py-1">Group</th><th className="text-right">Listings</th>
            <th className="text-right">Champion median gap</th><th className="text-right">Challenger median gap</th>
            <th className="text-right">Large gaps</th></tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.value} className="border-t border-line">
              <td className="py-1">{g.value}</td>
              <td className="figure text-right">{fmtNum(g.count)}</td>
              <td className="figure text-right">{g.champion_gap_count ? fmtGap(g.champion_median_gap_pct, 0) : '—'}</td>
              <td className="figure text-right">{g.challenger_gap_count ? fmtGap(g.challenger_median_gap_pct, 0) : '—'}</td>
              <td className="figure text-right">{fmtNum(g.large_gap_count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Unscored({ items }) {
  const groups = useMemo(() => {
    const g = {}
    items.forEach((u) => {
      const k = `${u.listing_type || 'unknown listing type'} / ${u.property_type || 'unknown property type'}`
      ;(g[k] ||= []).push(u)
    })
    return Object.entries(g).sort((a, b) => b[1].length - a[1].length)
  }, [items])
  if (!items.length) return null
  return (
    <details className="card p-4">
      <summary className="cursor-pointer text-sm">
        <span className="font-medium">{fmtNum(items.length)} new listings were not scored</span>
        <span className="text-inkmute"> · {groups.slice(0, 4).map(([k, v]) => `${k}: ${v.length}`).join(' · ')}{groups.length > 4 ? ' · …' : ''}</span>
      </summary>
      <div className="mt-3 text-xs text-inkmute">The model covers for-sale houses, apartments and townhouses; listings whose inputs could not be built are listed with the reason.</div>
      <table className="w-full text-xs mt-2">
        <thead className="text-inkmute text-left"><tr><th className="py-1">Ref</th><th>Title</th><th>Type</th><th>Reason</th></tr></thead>
        <tbody>
          {items.map((u) => (
            <tr key={u.record_id} className="border-t border-line align-top">
              <td className="py-1 figure">{u.ref}</td>
              <td className="max-w-xs truncate">{u.title}</td>
              <td>{u.listing_type} / {u.property_type}</td>
              <td className="text-inkmute">{u.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

function Th({ id, sort, onSort, children, className = '' }) {
  const active = sort.key === id
  return (
    <th className={`py-1 cursor-pointer select-none ${className}`} onClick={() => onSort(id)}>
      {children}{active ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
    </th>
  )
}

function ResultsTable({ rows, threshold, onOpen }) {
  const [sort, setSort] = useState({ key: 'gap', dir: 'desc' })
  const onSort = (key) => setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'gap' ? 'desc' : 'asc' }))
  const sorted = useMemo(() => sortRows(rows, sort.key, sort.dir), [rows, sort])
  return (
    <div className="card p-4 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-inkmute text-xs text-left">
          <tr>
            <Th id="ref" sort={sort} onSort={onSort}>Ref</Th>
            <th>Title</th>
            <Th id="type" sort={sort} onSort={onSort}>Type</Th>
            <Th id="parish" sort={sort} onSort={onSort}>Parish</Th>
            <Th id="asking" sort={sort} onSort={onSort} className="text-right">Asking</Th>
            <Th id="champion" sort={sort} onSort={onSort}><span className={SLOT_TEXT.champion}>Champion</span></Th>
            <Th id="challenger" sort={sort} onSort={onSort}><span className={SLOT_TEXT.challenger}>Challenger</span></Th>
            <Th id="gap" sort={sort} onSort={onSort}>Biggest gap</Th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const a = r.asking || {}
            return (
              <tr key={r.record_id} className={`border-t border-line hover:bg-line/30 cursor-pointer align-top ${r.large_gap ? 'bg-flag/5' : ''}`}
                onClick={() => onOpen(r.record_id)}>
                <td className="py-1.5 figure text-xs">{r.ref}</td>
                <td className="max-w-[16rem] truncate text-xs" title={r.title}>{r.title}</td>
                <td className="text-xs">{r.property_type}</td>
                <td className="text-xs">{r.parish_input || r.parish || <span className="text-inkmute">—</span>}</td>
                <td className="figure text-xs text-right whitespace-nowrap pl-2"
                  title={a.jmd == null && a.amount != null ? 'No JMD value for this asking price, so no gap is shown'
                    : a.fx ? `${fmtPrice(a.amount, a.currency)} at the Bank of Jamaica mid rate of ${a.fx.date} (${a.fx.rate})` : undefined}>
                  {a.jmd != null ? fmtM(a.jmd) : fmtPrice(a.amount, a.currency)}
                </td>
                <td className="px-3"><SlotCell slot={r.slots?.champion} threshold={threshold} /></td>
                <td className="px-3"><SlotCell slot={r.slots?.challenger} threshold={threshold} /></td>
                <td className="figure text-xs px-2">{absGap(r) == null ? <span className="text-inkmute">—</span> : `${fmtNum(absGap(r), 0)}%`}</td>
                <td className="text-[11px] text-inkmute">{(r.flags || []).slice(0, 2).join(', ')}{(r.flags || []).length > 2 ? ` +${r.flags.length - 2}` : ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ---------- the view ----------

export default function ModelBatch({ onOpen }) {
  const [job, setJob] = useState(undefined) // undefined = loading, null = no batch yet
  const [rows, setRows] = useState([])
  const [unscored, setUnscored] = useState([])
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [starting, setStarting] = useState(false)
  const [withChallenger, setWithChallenger] = useState(true)
  const [limit, setLimit] = useState('')
  const rowsVersion = useRef(null)

  const apply = useCallback((snap) => {
    if (!snap || snap.job === null) { setJob(null); return }
    const { rows: newRows, unscored: newUnscored, summary: newSummary, ...head } = snap
    setJob(head)
    if (newRows) {
      setRows(newRows); setUnscored(newUnscored || []); setSummary(newSummary || null)
      rowsVersion.current = head.rows_version
    }
  }, [])

  useEffect(() => {
    api.modelBatch().then(apply).catch((e) => { setError(errorText(e)); setJob(null) })
  }, [apply])

  const running = job?.status === 'running'
  useEffect(() => {
    if (!running) return undefined
    const t = setInterval(() => {
      api.modelBatch(rowsVersion.current).then(apply).catch((e) => setError(errorText(e)))
    }, POLL_MS)
    return () => clearInterval(t)
  }, [running, job?.id, apply])

  const start = async () => {
    setError(null); setStarting(true)
    try {
      const snap = await api.modelBatchStart({ challenger: withChallenger ? 'challenger' : null, limit: limit ? Number(limit) : null })
      rowsVersion.current = null
      apply(snap)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setStarting(false)
    }
  }

  const cancel = async () => {
    setError(null)
    try {
      await api.modelBatchCancel()
      apply(await api.modelBatch(rowsVersion.current))
    } catch (e) {
      setError(errorText(e))
    }
  }

  const notes = useMemo(() => batchNotes(rows), [rows])
  if (job === undefined) return <Spinner label="Loading…" />
  const run = job?.run
  const sel = 'border border-line rounded-sm px-2 py-1 bg-white text-sm'
  return (
    <div className="space-y-4">
      <div className="card p-5 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="label">Model batch</div>
            <div className="text-sm text-inkmute mt-1 max-w-3xl">
              Scores every <b>new</b> listing of the latest scrape run (listings that appear in no earlier run) with the registry
              champion and challenger, and lists the biggest gaps to the asking price first. Estimates are asking prices in JMD.
              Read only; nothing is written. Results stay on this screen only.
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-1 text-inkmute" title="TabPFN predicts all rows in one go, so the time is mostly fixed (a few minutes)">
              <input type="checkbox" checked={withChallenger} disabled={running} onChange={(e) => setWithChallenger(e.target.checked)} />
              include the challenger
            </label>
            <label className="flex items-center gap-1 text-inkmute">
              first
              <input className={`${sel} w-20`} type="number" min="1" placeholder="all" value={limit} disabled={running}
                onChange={(e) => setLimit(e.target.value)} />
              listings
            </label>
            {running
              ? <button onClick={cancel} className="px-3 py-1 border border-flag text-flag rounded-sm">Cancel</button>
              : <button onClick={start} disabled={starting} className="px-3 py-1 bg-teal text-white rounded-sm disabled:opacity-50">
                {job ? 'Run again' : 'Score the new listings'}</button>}
          </div>
        </div>
        {error && <div className="text-sm text-flag">{error}</div>}
        {run && (
          <div className="text-xs text-inkmute">
            Run <span className="figure">{run.run_id}</span> (first fetch {(run.first_fetched_at || '').slice(0, 16).replace('T', ' ')}):{' '}
            <b>{fmtNum(run.new_listings)}</b> new of {fmtNum(run.listings_in_run)} listings in the run, {fmtNum(run.earlier_runs)} earlier runs compared.
            {' '}{fmtNum(run.supported)} are for-sale houses, apartments or townhouses
            {run.limit ? <> (scoring the first {fmtNum(run.limit)})</> : null}.
            {run.unkeyed_rows > 0 && ` ${fmtNum(run.unkeyed_rows)} rows without a listing key could not be classified.`}
          </div>
        )}
      </div>

      {job && <Progress job={job} />}
      {job && (job.status === 'error' || job.status === 'cancelled') && job.message && (
        <div className={`text-sm border rounded-sm p-3 ${job.status === 'error' ? 'border-flag/40 bg-flag/5 text-flag' : 'border-amber/40 bg-amber/10'}`}>{job.message}</div>
      )}

      {summary && rows.some((r) => SLOTS.some((n) => okSlot(r, n))) && (
        <div className="grid lg:grid-cols-2 gap-4">
          <SummaryTable title="Median gap to asking, by property type" groups={summary.by_property_type} />
          <SummaryTable title="Median gap to asking, by parish" groups={summary.by_parish} />
        </div>
      )}

      {job && rows.length > 0 && (
        <>
          <div className="text-xs text-inkmute">
            {fmtNum(rows.length)} listings · gap = (estimate − asking) ÷ asking · rows beyond ±{job.gap_threshold_pct}% are highlighted ·
            click a header to sort, a row to open the listing · amounts are JMD, in millions (M), with the 90% range under each estimate.
          </div>
          {notes.length > 0 && (
            <ul className="text-xs border border-amber/40 bg-amber/10 rounded-sm p-3 space-y-1 list-disc pl-6">
              {notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
          )}
          <ResultsTable rows={rows} threshold={job.gap_threshold_pct} onOpen={onOpen} />
        </>
      )}
      <Unscored items={unscored} />
    </div>
  )
}
