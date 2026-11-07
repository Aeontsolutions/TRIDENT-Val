import React from 'react'
import { CircleMarker, MapContainer, Popup, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'

// A plain point map. The caller styles each point via `pointStyle` (-> {color, stroke, radius,
// fillOpacity, weight}), supplies the popup via `renderPopup`, and may pass extra react-leaflet
// layers as children. Each point needs `pid` (a unique key), `lat` and `lng`.
export default function PropertyMap({ points, center, zoom = 9, height = 480, pointStyle, renderPopup, children }) {
  return (
    <MapContainer center={center} zoom={zoom} preferCanvas style={{ height, width: '100%' }}>
      <TileLayer
        attribution="&copy; OpenStreetMap contributors"
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      {points.map((p) => {
        const st = pointStyle(p)
        return (
          <CircleMarker key={p.pid} center={[p.lat, p.lng]} radius={st.radius ?? 5}
            pathOptions={{ color: st.stroke ?? st.color, fillColor: st.color,
              fillOpacity: st.fillOpacity ?? 0.6, weight: st.weight ?? 1 }}>
            <Popup>{renderPopup?.(p)}</Popup>
          </CircleMarker>
        )
      })}
      {children}
    </MapContainer>
  )
}
