import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

const ticks = value => value == null ? '—' : `${(Number(value) / 100).toFixed(2)}¢`
const probability = value => value == null ? '—' : ticks(Number(value) * 10000)
const number = value => Number(value ?? 0).toLocaleString()

function BookChart({ events, hoveredIndex, onHover }) {
  const books = events.map((event, index) => ({ ...event, index, mid_ticks: event.mid == null ? null : Number(event.mid) * 10000 }))
  const values = books.flatMap(event => [event.best_yes_bid, event.best_yes_ask, event.mid_ticks]).filter(value => value != null).map(Number)
  if (values.length < 2) return <p className="empty">Need at least two quoted book snapshots to chart bid, ask, and midpoint.</p>
  const rawMin = Math.min(...values), rawMax = Math.max(...values), padding = Math.max(25, (rawMax - rawMin) * 0.18)
  const min = rawMin - padding, max = rawMax + padding, range = max - min
  const x = index => books.length === 1 ? 50 : index / (books.length - 1) * 100
  const y = value => 92 - ((Number(value) - min) / range) * 78
  const points = field => books.filter(event => event[field] != null).map(event => `${x(event.index)},${y(event[field])}`).join(' ')
  const active = books[hoveredIndex] ?? books.at(-1)
  const moveCursor = event => {
    const rect = event.currentTarget.getBoundingClientRect()
    onHover(Math.max(0, Math.min(books.length - 1, Math.round((event.clientX - rect.left) / rect.width * (books.length - 1)))))
  }
  return <>
    <div className="chart-legend"><span><i className="bid-dot" />YES bid</span><span><i className="ask-dot" />YES ask</span><span><i className="mid-dot" />Mid</span><em>Zoomed to quoted range</em></div>
    <div className="chart"><div className="axis-label top">{ticks(max)}</div><div className="axis-label middle">{ticks((max + min) / 2)}</div><svg viewBox="0 0 100 100" preserveAspectRatio="none" onMouseMove={moveCursor} onMouseLeave={() => onHover(null)}><line className="grid" x1="0" y1="14" x2="100" y2="14" /><line className="grid" x1="0" y1="53" x2="100" y2="53" /><line className="grid" x1="0" y1="92" x2="100" y2="92" /><polyline className="bid-line" points={points('best_yes_bid')} /><polyline className="ask-line" points={points('best_yes_ask')} /><polyline className="mid-line" points={points('mid_ticks')} />{active && <line className="cursor" x1={x(active.index)} y1="8" x2={x(active.index)} y2="94" />}</svg><div className="axis-label bottom">{ticks(min)}</div></div>
    {active && <div className="quote-readout"><span>{active.ts?.replace('T', ' ').replace('+00:00', ' UTC')}</span><b>Bid {ticks(active.best_yes_bid)}</b><b>Ask {ticks(active.best_yes_ask)}</b><b>Mid {ticks(active.mid_ticks)}</b><b>Spread {ticks(active.spread_ticks)}</b></div>}
  </>
}

function Depth({ event }) {
  if (!event) return <p className="empty">Select a captured market to inspect its latest book.</p>
  const max = Math.max(1, ...event.yes_bids.map(x => Number(x.count)), ...event.no_bids.map(x => Number(x.count)))
  return <div className="depth-grid"><div><h3>YES bids</h3>{event.yes_bids.slice(0, 8).map(level => <div className="level yes" key={`y-${level.price_ticks}`}><span>{ticks(level.price_ticks)}</span><i style={{ width: `${Number(level.count) / max * 100}%` }} /><b>{level.count}</b></div>)}</div><div><h3>NO bids</h3>{event.no_bids.slice(0, 8).map(level => <div className="level no" key={`n-${level.price_ticks}`}><span>{ticks(level.price_ticks)}</span><i style={{ width: `${Number(level.count) / max * 100}%` }} /><b>{level.count}</b></div>)}</div></div>
}

function App() {
  const [overview, setOverview] = useState(null), [markets, setMarkets] = useState([]), [selected, setSelected] = useState(null), [replay, setReplay] = useState([]), [error, setError] = useState(null), [hoveredIndex, setHoveredIndex] = useState(null)
  useEffect(() => { Promise.all([fetch('/api/overview').then(r => r.json()), fetch('/api/markets').then(r => r.json())]).then(([o, m]) => { setOverview(o); setMarkets(m); setSelected(m.find(x => Number(x.full_depth_snapshots) > 0 && Number(x.quote_levels) > 2)?.ticker ?? m.find(x => Number(x.full_depth_snapshots) > 0)?.ticker ?? m[0]?.ticker) }).catch(e => setError(e.message)) }, [])
  useEffect(() => { if (selected) fetch(`/api/replay/${selected}`).then(r => r.json()).then(data => { setReplay(data.events ?? []); setHoveredIndex(null) }).catch(e => setError(e.message)) }, [selected])
  const latest = replay.at(-1)
  const cards = useMemo(() => overview && [['Tracked markets', overview.markets], ['Full-depth snapshots', overview.full_depth_snapshots], ['Captured markets', overview.captured_markets], ['Stored trades', overview.trades]], [overview])
  if (error) return <main><h1>Calibr</h1><p className="error">Dashboard API unavailable: {error}</p></main>
  return <main><header><div><p className="eyebrow">KALSHI MARKET MICROSTRUCTURE</p><h1>Calibr <span>Research Console</span></h1></div><p className="badge">Read-only · PostgreSQL replay</p></header>{!overview ? <p>Loading captured market data…</p> : <><section className="cards">{cards.map(([label, value]) => <article key={label}><span>{label}</span><strong>{number(value)}</strong></article>)}</section><section className="panel"><div className="panel-head"><div><p className="eyebrow">TOP OF BOOK REPLAY</p><h2>{selected ?? 'No captured market'}</h2></div><select value={selected ?? ''} onChange={e => setSelected(e.target.value)}>{markets.filter(m => Number(m.full_depth_snapshots) > 0).map(m => <option key={m.ticker} value={m.ticker}>{m.ticker} · {number(m.quote_levels)} quote levels</option>)}</select></div><BookChart events={replay} hoveredIndex={hoveredIndex} onHover={setHoveredIndex} /><div className="stats"><span>Latest mid <b>{probability(latest?.mid)}</b></span><span>Spread <b>{ticks(latest?.spread_ticks)}</b></span><span>Imbalance <b>{latest?.imbalance == null ? '—' : Number(latest.imbalance).toFixed(2)}</b></span><span>Snapshots <b>{number(replay.length)}</b></span></div></section><section className="panel"><div className="panel-head"><div><p className="eyebrow">LATEST FULL-DEPTH BOOK</p><h2>Displayed liquidity</h2></div><p>{latest?.ts?.replace('T', ' ').replace('+00:00', ' UTC')}</p></div><Depth event={latest} /></section><section className="panel"><div className="panel-head"><div><p className="eyebrow">CAPTURE INVENTORY</p><h2>Market coverage</h2></div></div><div className="market-table">{markets.slice(0, 12).map(m => <button key={m.ticker} onClick={() => setSelected(m.ticker)}><span>{m.ticker}</span><span>{number(m.full_depth_snapshots)} books · {number(m.quote_levels)} quote levels</span><span>{number(m.trades)} trades</span></button>)}</div></section></>}</main>
}

createRoot(document.getElementById('root')).render(<App />)
