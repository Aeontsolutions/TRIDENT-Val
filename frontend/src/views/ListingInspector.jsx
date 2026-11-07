import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Carousel, FALLBACK_IMG, Spinner } from '../components/shared.jsx'
import { fmtPrice } from '../components/ListingsShell.jsx'
import { REASON_LABEL } from './ListingsFlags.jsx'
import ModelEstimate from '../components/ModelEstimate.jsx'

const statusStyle = {
  parsed: 'bg-ok/10 text-ok border-ok/30',
  unparsed: 'bg-flag/10 text-flag border-flag/30',
  derived: 'bg-amber/10 text-amber border-amber/30',
  raw_only: 'bg-inkmute/10 text-inkmute border-inkmute/30',
  missing: 'bg-line/40 text-inkmute border-line',
}
const statusText = { parsed: 'parsed', unparsed: 'raw not parsed', derived: 'no raw text', raw_only: 'raw only', missing: 'missing' }

const show = (v) => (v == null || v === '' ? <span className="text-inkmute">—</span> : String(v))

function Link({ href }) {
  if (!href) return <span className="text-inkmute">—</span>
  return /^https?:/.test(href)
    ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-teal hover:underline break-all">{href} ↗</a>
    : <span className="figure break-all" title="Private GCS object (not browser-openable)">{href}</span>
}

function FlagForm({ recordId, row, reasons, onDone, onCancel }) {
  const [reason, setReason] = useState(row.status === 'unparsed' ? 'not_parsed' : 'wrong_value')
  const [note, setNote] = useState('')
  const [expected, setExpected] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const submit = async () => {
    setBusy(true); setErr(null)
    try {
      await api.createListingFlag({ record_id: recordId, field: row.field, reason, note, expected_value: expected || null })
      onDone()
    } catch (e) { setErr(e.message); setBusy(false) }
  }
  const inp = 'border border-line rounded-sm px-2 py-1 bg-white text-sm'
  return (
    <div className="bg-amber/10 border border-amber/40 rounded-sm p-3 space-y-2">
      <div className="text-sm font-medium">Flag “{row.field}” as a bad parse
        <span className="text-xs text-inkmute font-normal"> · raw {String(row.raw ?? '—')} → parsed {String(row.parsed ?? '—')}</span></div>
      <div className="flex flex-wrap gap-2 items-center">
        <select className={inp} value={reason} onChange={(e) => setReason(e.target.value)}>
          {reasons.map((r) => <option key={r} value={r}>{REASON_LABEL[r] || r}</option>)}
        </select>
        <input className={`${inp} w-48`} placeholder="Expected value (optional)" value={expected}
          onChange={(e) => setExpected(e.target.value)} maxLength={256} />
        <input className={`${inp} flex-1 min-w-[12rem]`} placeholder="Note — what is wrong?" value={note}
          onChange={(e) => setNote(e.target.value)} maxLength={2000} />
      </div>
      <div className="flex items-center gap-3 text-sm">
        <button disabled={busy} onClick={submit} className="px-3 py-1 bg-teal text-white rounded-sm disabled:opacity-50">Submit flag</button>
        <button onClick={onCancel} className="text-inkmute hover:underline">Cancel</button>
        {err && <span className="text-flag text-xs">{err}</span>}
      </div>
    </div>
  )
}

export default function ListingInspector({ recordId, onBack, user, onOpen }) {
  const [d, setD] = useState(null)
  const [flags, setFlags] = useState([])
  const [reasons, setReasons] = useState([])
  const [flagging, setFlagging] = useState(null)
  const canFlag = user && user.role !== 'Viewer'
  const loadFlags = () => api.listingFlags({ record_id: recordId, status: 'open' }).then((x) => setFlags(x.items)).catch(() => setFlags([]))
  useEffect(() => { setFlagging(null); loadFlags(); api.listingFlagsMeta().then((m) => setReasons(m.reasons)).catch(() => {}) }, [recordId])
  const [err, setErr] = useState(null)
  const [photo, setPhoto] = useState(0)
  useEffect(() => { setD(null); api.listing(recordId).then(setD).catch((e) => setErr(e.message)) }, [recordId])
  if (err) return <div className="card p-4 text-sm text-flag">Could not load listing: {err}</div>
  if (!d) return <Spinner label="Loading listing…" />

  const { summary: s, provenance: p, quality: q, record: r } = d
  const images = d.images.map((i) => ({ url: i.source_url, label: i.caption_raw }))
  const prov = [
    ['site_listing_ref', p.site_listing_ref], ['scrape_run_id', p.scrape_run_id],
    ['fetched_at', p.fetched_at], ['http_status', p.http_status],
    ['parser_version', p.parser_version], ['scraper_version', p.scraper_version],
    ['robots_allowed', p.robots_allowed == null ? null : String(p.robots_allowed)],
    ['record_id', r.record.record_id],
  ]

  return (
    <div className="space-y-4">
      <button onClick={onBack} className="text-sm text-teal hover:underline">← Back to listings</button>
      <div className="card p-5">
        <div className="label">{s.property_type_raw} · {s.listing_type_raw}</div>
        <h1 className="text-xl font-semibold mt-1">{s.title}</h1>
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 mt-2">
          <span className="figure text-2xl font-semibold text-tealdeep">{fmtPrice(s.amount, s.currency)}</span>
          <span className="text-sm text-inkmute">{s.community}, {s.parish_raw}</span>
          <span className="text-xs text-inkmute figure">{s.latitude?.toFixed(5)}, {s.longitude?.toFixed(5)} · geo {s.geo_precision || '—'}</span>
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-1 mt-3 text-sm">
          <span>Listing page: <Link href={p.listing_url} /></span>
          {p.canonical_url && p.canonical_url !== p.listing_url && <span>Canonical: <Link href={p.canonical_url} /></span>}
        </div>
      </div>

      <ModelEstimate recordId={recordId} askingAmount={s.amount} currency={s.currency} />

      <div className="grid lg:grid-cols-3 gap-4 items-start">
        <div className="card p-5 lg:col-span-2">
          <div className="label mb-2">Parsed vs raw</div>
          <table className="w-full text-sm">
            <thead className="text-inkmute text-xs text-left">
              <tr><th className="py-1 w-32">Field</th><th>Raw (as scraped)</th><th>Parsed</th><th className="w-32">Status</th><th className="w-16">Flag</th></tr>
            </thead>
            <tbody>
              {d.comparison.map((c) => {
                const open = flags.filter((x) => x.field === c.field)
                return (
                  <React.Fragment key={c.field}>
                    <tr className="border-t border-line align-top">
                      <td className="py-1.5 font-medium">{c.field}</td>
                      <td className="figure text-xs break-words max-w-[16rem]">{show(c.raw)}</td>
                      <td className="figure text-xs">{show(c.parsed)}{c.parsed != null && c.extra ? ` ${c.extra}` : ''}</td>
                      <td><span className={`inline-block px-2 py-0.5 text-[11px] border rounded-sm ${statusStyle[c.status]}`}>{statusText[c.status]}</span></td>
                      <td className="text-xs whitespace-nowrap">
                        {canFlag && (
                          <button onClick={() => setFlagging(flagging === c.field ? null : c.field)} title="Flag this field as a bad parse"
                            aria-label={`Flag ${c.field} as a bad parse`} className="text-flag hover:bg-flag/10 rounded-sm px-1.5">⚑</button>
                        )}
                        {open.length > 0 && <span className="text-flag font-medium" title={open.map((x) => `#${x.id} ${REASON_LABEL[x.reason] || x.reason}: ${x.note || ''}`).join('; ')}> {open.length}</span>}
                      </td>
                    </tr>
                    {flagging === c.field && (
                      <tr><td colSpan={5} className="pb-2">
                        <FlagForm recordId={recordId} row={c} reasons={reasons}
                          onCancel={() => setFlagging(null)} onDone={() => { setFlagging(null); loadFlags() }} />
                      </td></tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
          {flags.length > 0 && (
            <div className="mt-3 text-xs border border-flag/30 bg-flag/5 rounded-sm p-2">
              <b className="text-flag">{flags.length} open bad-parse flag{flags.length === 1 ? '' : 's'}</b> on this observation:{' '}
              {flags.map((x) => `${x.field} (${REASON_LABEL[x.reason] || x.reason}, ${x.created_by})`).join(' · ')}
            </div>
          )}
          {r.attributes?.features_raw?.length > 0 && (
            <div className="mt-3 text-xs text-inkmute">Features: {r.attributes.features_raw.join(' · ')}</div>
          )}
        </div>

        <div className="space-y-4">
          <div className="card p-5">
            <div className="label mb-2">Provenance</div>
            <dl className="text-xs space-y-1">
              {prov.map(([k, v]) => (
                <div key={k} className="flex gap-2"><dt className="w-32 text-inkmute flex-none">{k}</dt><dd className="figure break-all">{show(v)}</dd></div>
              ))}
              <div className="flex gap-2"><dt className="w-32 text-inkmute flex-none">snapshot_uri</dt><dd><Link href={p.snapshot_uri} /></dd></div>
              <div className="flex gap-2"><dt className="w-32 text-inkmute flex-none">raw_payload_uri</dt><dd><Link href={p.raw_payload_uri} /></dd></div>
            </dl>
          </div>
          {d.other_observations?.length > 0 && (
            <div className="card p-5">
              <div className="label mb-2">Same listing in other runs ({d.other_observations.length})</div>
              <ul className="text-xs space-y-1">
                {d.other_observations.map((o) => (
                  <li key={o.record_id}>
                    <button className="text-teal hover:underline figure text-left" onClick={() => onOpen?.(o.record_id)}>
                      {(o.fetched_at || '').slice(0, 16).replace('T', ' ')} · {o.scrape_run_id}
                    </button> · {fmtPrice(o.amount, o.currency)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="card p-5">
            <div className="label mb-2">Quality flags</div>
            <dl className="text-xs space-y-1">
              {['parse_errors', 'missing_required', 'pii_redacted'].map((k) => (
                <div key={k} className="flex gap-2"><dt className="w-32 text-inkmute flex-none">{k}</dt>
                  <dd className={`figure ${k !== 'pii_redacted' && q[k]?.length ? 'text-flag' : ''}`}>{q[k]?.length ? q[k].join(', ') : 'none'}</dd></div>
              ))}
              <div className="flex gap-2"><dt className="w-32 text-inkmute flex-none">duplicate_of</dt><dd className="figure">{show(q.duplicate_of)}</dd></div>
            </dl>
          </div>
        </div>
      </div>

      <div className="card p-5">
        <div className="label mb-2">Photos ({images.length})</div>
        {images.length === 0 ? <div className="text-sm text-inkmute">No images recorded.</div> : (
          <div className="flex flex-col md:flex-row gap-4">
            <Carousel images={images} index={photo} onIndexChange={setPhoto} showDots={false}
              className="md:w-[440px] flex-none" imgClassName="h-72" />
            <div className="flex-1 grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-2 content-start">
              {d.images.map((im, idx) => (
                <button key={im.source_url} type="button" onClick={() => setPhoto(idx)}
                  className={`text-left rounded-sm overflow-hidden border ${idx === photo ? 'border-teal ring-1 ring-teal' : 'border-line hover:border-teal/50'}`}>
                  <img src={im.source_url} alt="" loading="lazy" referrerPolicy="no-referrer"
                    onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = FALLBACK_IMG }}
                    className="w-full h-16 object-cover" />
                  <div className="text-[10px] px-1.5 py-1 text-inkmute figure">#{im.order_index}{im.is_primary ? ' · primary' : ''} · {im.download_status}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="text-[11px] text-inkmute mt-2">Images are hotlinked from the source site's CDN; none have been downloaded to storage.</div>
      </div>

      {r.listing?.description && (
        <div className="card p-5">
          <div className="label mb-2">Description (raw)</div>
          <p className="text-sm leading-relaxed whitespace-pre-line">{r.listing.description}</p>
        </div>
      )}
    </div>
  )
}
