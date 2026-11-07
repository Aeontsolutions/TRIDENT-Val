import React, { useState } from 'react'

export const FALLBACK_IMG =
  'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="100%25" height="100%25" fill="%23DDE3E0"/><text x="50%25" y="50%25" font-family="sans-serif" font-size="16" fill="%235B6B77" text-anchor="middle">Residential asset</text></svg>'

export function Spinner({ label = 'Loading…' }) {
  return <div className="py-16 text-center text-inkmute text-sm">{label}</div>
}

// Labeled image carousel — no third-party slider dependency, just prev/next + dot controls.
// `index`/`onIndexChange` are optional: pass both to drive the carousel from an external
// control (e.g. a thumbnail rail) instead of its own internal state.
export function Carousel({ images, className = '', imgClassName = 'h-64',
  index: controlledIndex, onIndexChange, showDots = true }) {
  const [internalIndex, setInternalIndex] = useState(0)
  const list = images || []

  if (list.length === 0) {
    return (
      <div className={`${imgClassName} ${className} flex items-center justify-center bg-line/40 text-inkmute text-sm rounded-sm border border-line`}>
        No photos available
      </div>
    )
  }

  const rawIndex = controlledIndex != null ? controlledIndex : internalIndex
  const index = ((rawIndex % list.length) + list.length) % list.length
  const img = list[index]
  const go = (n) => {
    const next = ((n % list.length) + list.length) % list.length
    if (onIndexChange) onIndexChange(next)
    else setInternalIndex(next)
  }

  return (
    <div className={className}>
      <div className="relative">
        <img
          key={img.url}
          src={img.url}
          alt={img.label || ''}
          onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = FALLBACK_IMG }}
          className={`w-full ${imgClassName} object-cover rounded-sm border border-line`}
        />
        {list.length > 1 && (
          <>
            <button type="button" onClick={() => go(index - 1)} aria-label="Previous photo"
              className="absolute left-1.5 top-1/2 -translate-y-1/2 w-7 h-7 rounded-full bg-ink/60 hover:bg-ink/80 text-white text-base leading-none flex items-center justify-center">‹</button>
            <button type="button" onClick={() => go(index + 1)} aria-label="Next photo"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 w-7 h-7 rounded-full bg-ink/60 hover:bg-ink/80 text-white text-base leading-none flex items-center justify-center">›</button>
            <div className="absolute top-2 right-2 bg-ink/60 text-white text-[11px] px-1.5 py-0.5 rounded-sm figure">
              {index + 1}/{list.length}
            </div>
          </>
        )}
        {img.label && (
          <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-ink/80 to-transparent text-white text-xs px-2.5 py-2 rounded-b-sm">
            {img.label}
          </div>
        )}
      </div>
      {showDots && list.length > 1 && (
        <div className="flex justify-center gap-1.5 mt-2">
          {list.map((im, idx) => (
            <button key={idx} type="button" onClick={() => go(idx)}
              aria-label={`Go to photo ${idx + 1}${im.label ? `: ${im.label}` : ''}`}
              className={`h-1.5 rounded-full transition-all ${idx === index ? 'w-4 bg-teal' : 'w-1.5 bg-line'}`} />
          ))}
        </div>
      )}
    </div>
  )
}
