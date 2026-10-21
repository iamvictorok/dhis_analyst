/**
 * MapView — choropleth / point map from a `map_config` SSE event.
 *
 * Uses Plotly's MapLibre-based `choroplethmap` + `scattermap` traces (already
 * bundled in plotly.js-dist-min) on free Carto "positron" tiles: no API key.
 * Everything is driven by the payload: geometry, legend classes, units, title
 * and the bounding box of the returned features (no fixed country or zoom).
 */
import { useEffect, useRef, useState } from 'preact/hooks';
import Plotly from 'plotly.js-dist-min';
import DownloadBar from './DownloadBar.jsx';

const MAP_HEIGHT = 560;

function fitView(bbox, width, height) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return { center: { lon: 0, lat: 0 }, zoom: 1 };
  const [minLon, minLat, maxLon, maxLat] = bbox;
  const center = { lon: (minLon + maxLon) / 2, lat: (minLat + maxLat) / 2 };
  // Web-mercator fit with a small margin.
  const latRad = (lat) => {
    const s = Math.sin((lat * Math.PI) / 180);
    return Math.log((1 + s) / (1 - s)) / 2;
  };
  const lonSpan = Math.max(maxLon - minLon, 0.01);
  const latSpan = Math.max(latRad(maxLat) - latRad(minLat), 0.0002);
  const zLon = Math.log2((width * 360) / (512 * lonSpan));
  const zLat = Math.log2((height * 2 * Math.PI) / (512 * latSpan));
  const zoom = Math.max(0, Math.min(zLon, zLat, 14) - 0.15);
  return { center, zoom };
}

function buildTraces(map) {
  const traces = [];
  const fc = map.geojson || { type: 'FeatureCollection', features: [] };
  const feats = fc.features || [];
  const pts = map.points || [];
  const legend = map.legend || [];
  const hover = '<b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>';

  const groups = legend.map((c, i) => ({ ...c, i }));
  groups.push({ i: -1, label: 'No data', color: map.no_data_color || '#d1d5db' });

  for (const g of groups) {
    const inClass = feats.filter((f) => (f.properties?.class ?? -1) === g.i);
    const ptsIn = pts.filter((p) => (p.class ?? -1) === g.i);
    if (inClass.length) {
      traces.push({
        type: 'choroplethmap',
        geojson: fc,
        featureidkey: 'properties.id',
        locations: inClass.map((f) => f.properties.id),
        z: inClass.map(() => 1),
        customdata: inClass.map((f) => [f.properties.name, f.properties.label]),
        hovertemplate: hover,
        colorscale: [[0, g.color], [1, g.color]],
        showscale: false,
        marker: { line: { color: '#ffffff', width: 1 }, opacity: 0.85 },
        name: g.label,
        legendgroup: String(g.i),
        showlegend: true,
      });
    }
    if (ptsIn.length) {
      traces.push({
        type: 'scattermap',
        mode: 'markers',
        lon: ptsIn.map((p) => p.lon),
        lat: ptsIn.map((p) => p.lat),
        customdata: ptsIn.map((p) => [p.name, p.label]),
        hovertemplate: hover,
        marker: { size: 12, color: g.color, opacity: 0.95 },
        name: g.label,
        legendgroup: String(g.i),
        showlegend: !inClass.length,
      });
    }
  }
  return traces;
}

export default function MapView({ map }) {
  const ref = useRef(null);
  const [pngBusy, setPngBusy] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const width = el.clientWidth || 900;
    const { center, zoom } = fitView(map.bbox, width, MAP_HEIGHT - 70);
    const unitTxt = map.unit === '%' ? ' (%)' : '';
    // Two-line title so long period labels never get cut on a projector.
    const full = map.title || 'Map';
    const pl = map.period_label || '';
    const head = pl && full.endsWith(`, ${pl}`) ? full.slice(0, -(pl.length + 2)) : full;
    const titleText = pl && head !== full ? `${esc(head)}<br><span style="font-size:14px;color:#475569">${esc(pl)}</span>` : esc(full);
    const layout = {
      title: { text: titleText, font: { size: 18, color: '#0f172a' }, x: 0.01, xanchor: 'left', y: 0.985, yanchor: 'top' },
      height: MAP_HEIGHT,
      margin: { l: 0, r: 0, t: 66, b: 0 },
      paper_bgcolor: '#ffffff',
      map: { style: 'carto-positron', center, zoom },
      legend: {
        title: { text: `${map.metric || 'Value'}${unitTxt}`, font: { size: 13, color: '#0f172a' } },
        font: { size: 13, color: '#0f172a' },
        bgcolor: 'rgba(255,255,255,0.9)',
        bordercolor: '#cbd5e1',
        borderwidth: 1,
        x: 0.01, y: 0.02, xanchor: 'left', yanchor: 'bottom',
      },
      hoverlabel: { font: { size: 14 } },
    };
    const config = {
      responsive: true,
      displaylogo: false,
      scrollZoom: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
      toImageButtonOptions: { format: 'png', filename: slug(map.title), width: 1400, height: 900, scale: 1 },
    };
    Plotly.react(el, buildTraces(map), layout, config);
    return () => { if (el) Plotly.purge(el); };
  }, [map]);

  const downloadPng = async () => {
    if (!ref.current) return;
    setPngBusy(true);
    try {
      await Plotly.downloadImage(ref.current, { format: 'png', width: 1400, height: 900, filename: slug(map.title) });
    } catch (err) {
      console.error('PNG export failed:', err);
    } finally {
      setPngBusy(false);
    }
  };

  const rows = map.table?.rows || [];
  const headers = map.table?.headers || ['Organisation unit', 'Value'];
  return (
    <div class="map-view">
      <div class="map-container" ref={ref} id={`map-${map.id}`} />
      <div class="map-actions">
        <button class="btn-sm" onClick={downloadPng} disabled={pngBusy} title="Download the map as PNG">
          {pngBusy ? '…' : 'PNG'}
        </button>
        {map.export && <DownloadBar exportData={map.export} allowedFormats={['XLSX', 'CSV']} />}
      </div>
      <div class="map-table-wrapper">
        <table class="map-table">
          <thead><tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}><td>{r[0]}</td><td class="num">{fmt(r[1], map.unit)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function fmt(v, unit) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  const s = n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return unit === '%' ? `${s}%` : s;
}

function esc(t) {
  return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function slug(t) {
  return (t || 'dhis2-map').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 80) || 'dhis2-map';
}
