import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const money = (value) => value == null ? '—' : `${(value * 100).toFixed(1)}¢`
const number = (value) => Number(value ?? 0).toLocaleString()

function LineChart({ events }) {
  const values = events.map((event) => event.mid).filter((value) => value != null)
  if (values.length < 2) return <p className="empty">Need at least two two-sided book snapshots to chart the mid.</p>
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, 0.01)
  const points = values.map((value, index) => `${(index / (values.length - 1)) * 100},${92 - ((value - min) / range) * 78}`).join(' ')
  return <div className="chart"><div className="axis-label top">{money(max)}</div><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points={points} /></svg><div className="axis-label bottom">{money(min)}</div></div>
}

function Depth({ event }) {
  if (!event) return <p className="empty">Select a captured market to inspect its latest book.</p>
  const max = Math.max(1, ...event.yes_bids.map((x) => Number(x.count)), ...event.no_bids.map((x) => Number(x.count)))
  return <div className="depth-grid"><div><h3>YES bids</h3>{event.yes_bids.slice(0, 8).map((level) => <div className="level yes" key={`y-${level.price_cents}`}><span>{level.price_cents}¢</span><i style={{ width: `${Number(level.count) / max * 100}%` }} /><b>{level.count}</b></div>)}</div><div><h3>NO bids</h3>{event.no_bids.slice(0, 8).map((level) => <div className="level no" key={`n-${level.price_cents}`}><span>{level.price_cents}¢</span><i style={{ width: `${Number(level.count) / max * 100}%` }} /><b>{level.count}</b></div>)}</div></div>
}

function App() {
  const [overview, setOverview] = useState(null), [markets, setMarkets] = useState([]), [selected, setSelected] = useState(null), [replay, setReplay] = useState([]), [error, setError] = useState(null)
  useEffect(() => { Promise.all([fetch('/api/overview').then(r => r.json()), fetch('/api/markets').then(r => r.json())]).then(([o, m]) => { setOverview(o); setMarkets(m); setSelected(m.find(x => Number(x.full_depth_snapshots) > 0)?.ticker ?? m[0]?.ticker) }).catch(e => setError(e.message)) }, [])
  useEffect(() => { if (selected) fetch(`/api/replay/${selected}`).then(r => r.json()).then(data => setReplay(data.events ?? [])).catch(e => setError(e.message)) }, [selected])
  const latest = replay.at(-1)
  const cards = useMemo(() => overview && [["Tracked markets", overview.markets], ["Full-depth snapshots", overview.full_depth_snapshots], ["Captured markets", overview.captured_markets], ["Stored trades", overview.trades]], [overview])
  if (error) return <main><h1>Calibr</h1><p className="error">Dashboard API unavailable: {error}</p></main>
  return <main><header><div><p className="eyebrow">KALSHI MARKET MICROSTRUCTURE</p><h1>Calibr <span>Research Console</span></h1></div><p className="badge">Read-only · PostgreSQL replay</p></header>{!overview ? <p>Loading captured market data…</p> : <><section className="cards">{cards.map(([label, value]) => <article key={label}><span>{label}</span><strong>{number(value)}</strong></article>)}</section><section className="panel"><div className="panel-head"><div><p className="eyebrow">REPLAY</p><h2>{selected ?? 'No captured market'}</h2></div><select value={selected ?? ''} onChange={e => setSelected(e.target.value)}>{markets.filter(m => Number(m.full_depth_snapshots) > 0).map(m => <option key={m.ticker} value={m.ticker}>{m.ticker}</option>)}</select></div><LineChart events={replay} /><div className="stats"><span>Latest mid <b>{money(latest?.mid)}</b></span><span>Spread <b>{latest?.spread_cents ?? '—'}¢</b></span><span>Imbalance <b>{latest?.imbalance == null ? '—' : Number(latest.imbalance).toFixed(2)}</b></span><span>Snapshots <b>{number(replay.length)}</b></span></div></section><section className="panel"><div className="panel-head"><div><p className="eyebrow">LATEST FULL-DEPTH BOOK</p><h2>Displayed liquidity</h2></div><p>{latest?.ts?.replace('T', ' ').replace('+00:00', ' UTC')}</p></div><Depth event={latest} /></section><section className="panel"><div className="panel-head"><div><p className="eyebrow">CAPTURE INVENTORY</p><h2>Market coverage</h2></div></div><div className="market-table">{markets.slice(0, 12).map(m => <button key={m.ticker} onClick={() => setSelected(m.ticker)}><span>{m.ticker}</span><span>{number(m.full_depth_snapshots)} books</span><span>{number(m.trades)} trades</span></button>)}</div></section></>}</main>
}

createRoot(document.getElementById('root')).render(<App />)
