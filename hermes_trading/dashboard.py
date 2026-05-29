"""Lightweight web dashboard for hermes-trading. Reads from state/ files."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

STATE_DIR = Path("state")

app = FastAPI(title="hermes-trading")


def _read_trades() -> list[dict]:
    p = STATE_DIR / "trades.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().strip().splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def _read_yaml(name: str) -> dict:
    p = STATE_DIR / name
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _read_hypotheses() -> list[dict]:
    p = STATE_DIR / "hypotheses.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().strip().splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def _read_heartbeat() -> dict:
    p = STATE_DIR / "heartbeat.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _compute_portfolio(trades: list[dict], starting_balance: float = 10000.0) -> dict:
    balance = starting_balance
    equity_curve = [{"t": "start", "balance": balance}]
    wins = 0
    losses = 0
    pnl_pcts = []
    for t in trades:
        pnl_pct = t.get("pnl_pct", 0.0)
        # position size is fraction of balance risked
        position_size_r = 0.5  # default
        size_dollars = balance * position_size_r
        pnl_dollars = size_dollars * pnl_pct
        balance += pnl_dollars
        equity_curve.append({"t": t.get("exit_time", ""), "balance": round(balance, 2)})
        if pnl_pct > 0:
            wins += 1
        elif pnl_pct < 0:
            losses += 1
        pnl_pcts.append(pnl_pct)

    realised_return = (balance - starting_balance) / starting_balance if starting_balance else 0
    win_rate = wins / (wins + losses) if (wins + losses) else 0

    # max drawdown
    peak = starting_balance
    max_dd = 0.0
    for point in equity_curve:
        peak = max(peak, point["balance"])
        dd = (peak - point["balance"]) / peak if peak else 0
        max_dd = max(max_dd, dd)

    return {
        "starting_balance": starting_balance,
        "current_balance": round(balance, 2),
        "realised_return_pct": round(realised_return * 100, 3),
        "max_drawdown_pct": round(max_dd * 100, 3),
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate * 100, 1),
        "equity_curve": equity_curve,
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    trades = _read_trades()
    strategy = _read_yaml("strategy.yaml")
    goal = _read_yaml("goal.yaml")
    hypotheses = _read_hypotheses()
    heartbeat = _read_heartbeat()
    portfolio = _compute_portfolio(trades)
    return JSONResponse({
        "strategy": strategy,
        "goal": goal,
        "portfolio": portfolio,
        "trades": trades[-50:],   # last 50
        "hypotheses": hypotheses[-20:],
        "heartbeat": heartbeat,
    })


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<title>hermes-trading</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0b0d10; --panel: #14181d; --line: #232932;
    --text: #d8e0ec; --dim: #7a8597; --good: #4ade80; --bad: #f87171; --accent: #60a5fa;
    --mono: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: var(--bg); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  h1 { margin: 0 0 4px 0; font-size: 24px; font-weight: 600; }
  .sub { color: var(--dim); margin-bottom: 24px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 16px; margin-bottom: 24px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
          padding: 20px; }
  .label { color: var(--dim); font-size: 12px; text-transform: uppercase;
           letter-spacing: .05em; margin-bottom: 8px; }
  .value { font-size: 28px; font-weight: 600; font-family: var(--mono); }
  .value.good { color: var(--good); } .value.bad { color: var(--bad); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
           padding: 20px; margin-bottom: 16px; }
  .panel h2 { margin: 0 0 16px 0; font-size: 16px; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 13px; }
  th { text-align: left; color: var(--dim); font-weight: 500;
       padding: 8px 12px; border-bottom: 1px solid var(--line); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--line); }
  tr:last-child td { border-bottom: none; }
  .pnl-pos { color: var(--good); } .pnl-neg { color: var(--bad); }
  pre { font-family: var(--mono); font-size: 12px; color: var(--dim);
        background: #0b0d10; padding: 12px; border-radius: 6px; overflow: auto; margin: 0; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block;
         margin-right: 6px; vertical-align: middle; }
  .dot.ok { background: var(--good); } .dot.err { background: var(--bad); }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
  .muted { color: var(--dim); }
  canvas { max-height: 240px; }
</style>
</head>
<body>
<h1>hermes-trading</h1>
<div class=sub>Self-improving paper trading agent. Refreshes every 10s.</div>

<div class=grid>
  <div class=card><div class=label>Portfolio value</div>
    <div class=value id=balance>—</div>
    <div class=muted id=balance-sub style="font-size:12px;margin-top:6px"></div></div>
  <div class=card><div class=label>Realised return</div>
    <div class=value id=return>—</div>
    <div class=muted id=return-sub style="font-size:12px;margin-top:6px"></div></div>
  <div class=card><div class=label>Max drawdown</div>
    <div class=value id=dd>—</div>
    <div class=muted id=dd-sub style="font-size:12px;margin-top:6px"></div></div>
  <div class=card><div class=label>Win rate</div>
    <div class=value id=winrate>—</div>
    <div class=muted id=winrate-sub style="font-size:12px;margin-top:6px"></div></div>
</div>

<div class=panel>
  <h2>Equity curve</h2>
  <canvas id=equity></canvas>
</div>

<div class=two-col>
  <div class=panel>
    <h2>Current strategy</h2>
    <pre id=strategy></pre>
  </div>
  <div class=panel>
    <h2>Worker status</h2>
    <pre id=heartbeat></pre>
  </div>
</div>

<div class=panel>
  <h2>Closed trades (last 50)</h2>
  <table>
    <thead><tr><th>Exit time</th><th>Asset</th><th>Direction</th><th>Entry</th><th>Exit</th><th>PnL %</th><th>Strategy v</th></tr></thead>
    <tbody id=trades></tbody>
  </table>
</div>

<div class=panel>
  <h2>Hermes hypotheses (last 20)</h2>
  <table>
    <thead><tr><th>Time</th><th>Mode</th><th>Variable changed</th><th>Old</th><th>New</th><th>Reason</th><th>v</th></tr></thead>
    <tbody id=hypotheses></tbody>
  </table>
</div>

<script>
let chart = null;
function fmt(n, decimals=2) { return (n>=0?'+':'') + n.toFixed(decimals); }
function fmtMoney(n) { return '$' + n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function load() {
  fetch('/api/state').then(r=>r.json()).then(d=>{
    const p = d.portfolio, g = d.goal;
    document.getElementById('balance').textContent = fmtMoney(p.current_balance);
    document.getElementById('balance-sub').textContent = 'started at ' + fmtMoney(p.starting_balance) + ' (paper)';
    const ret = document.getElementById('return');
    ret.textContent = fmt(p.realised_return_pct) + '%';
    ret.className = 'value ' + (p.realised_return_pct >= 0 ? 'good' : 'bad');
    document.getElementById('return-sub').textContent = 'target: +' + ((g.target_return_30d||0)*100).toFixed(0) + '% / 30d';
    const dd = document.getElementById('dd');
    dd.textContent = p.max_drawdown_pct.toFixed(2) + '%';
    dd.className = 'value ' + (p.max_drawdown_pct > (g.max_drawdown||1)*100 ? 'bad' : '');
    document.getElementById('dd-sub').textContent = 'limit: ' + ((g.max_drawdown||0)*100).toFixed(0) + '%';
    document.getElementById('winrate').textContent = p.win_rate_pct.toFixed(0) + '%';
    document.getElementById('winrate-sub').textContent = p.wins + 'W / ' + p.losses + 'L (' + p.total_trades + ' total)';

    document.getElementById('strategy').textContent = JSON.stringify(d.strategy, null, 2);
    const hb = d.heartbeat;
    const status = hb.status || 'unknown';
    const dot = status === 'ok' ? 'ok' : 'err';
    document.getElementById('heartbeat').innerHTML =
      '<span class="dot '+dot+'"></span>' + status +
      '\\nlast tick: ' + (hb.last_tick || 'n/a') +
      '\\nfailures:  ' + (hb.consecutive_failures || 0);

    const tbody = document.getElementById('trades');
    tbody.innerHTML = d.trades.slice().reverse().map(t => `
      <tr>
        <td>${(t.exit_time||'').replace('T',' ').replace(/\\..*$/,'')}</td>
        <td>${t.asset||''}</td>
        <td>${t.direction||''}</td>
        <td>$${(t.entry_price||0).toFixed(2)}</td>
        <td>$${(t.exit_price||0).toFixed(2)}</td>
        <td class="${t.pnl_pct>=0?'pnl-pos':'pnl-neg'}">${(t.pnl_pct*100).toFixed(3)}%</td>
        <td>v${t.strategy_version||'?'}</td>
      </tr>`).join('') || '<tr><td colspan=7 class=muted>No trades yet — worker is waiting for entry conditions.</td></tr>';

    const hbody = document.getElementById('hypotheses');
    hbody.innerHTML = d.hypotheses.slice().reverse().map(h => `
      <tr>
        <td>${(h.timestamp||'').replace('T',' ').replace(/\\..*$/,'')}</td>
        <td>${h.mode||''}</td>
        <td>${h.changed_var||''}</td>
        <td>${h.old_val ?? ''}</td>
        <td>${h.new_val ?? ''}</td>
        <td>${h.reason||''}</td>
        <td>v${h.version||'?'}</td>
      </tr>`).join('') || '<tr><td colspan=7 class=muted>No reflections yet — first one fires after 5 closed trades.</td></tr>';

    // equity chart
    const labels = d.portfolio.equity_curve.map((_,i) => i);
    const data = d.portfolio.equity_curve.map(p => p.balance);
    if (chart) { chart.data.labels = labels; chart.data.datasets[0].data = data; chart.update(); }
    else {
      const ctx = document.getElementById('equity').getContext('2d');
      chart = new Chart(ctx, { type:'line',
        data: { labels, datasets: [{ data, borderColor:'#60a5fa', backgroundColor:'rgba(96,165,250,.1)', fill:true, tension:.2, pointRadius:0, borderWidth:2 }] },
        options: { plugins:{legend:{display:false}}, scales:{ x:{grid:{color:'#232932'},ticks:{color:'#7a8597'}}, y:{grid:{color:'#232932'},ticks:{color:'#7a8597',callback:v=>'$'+v.toLocaleString()}} } } });
    }
  }).catch(e => console.error(e));
}
load();
setInterval(load, 10000);
</script>
</body>
</html>
"""


def start_dashboard_in_background():
    """Start uvicorn in a background thread so the trading loop keeps running."""
    import threading
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    def _run():
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print(f"Dashboard listening on :{port}", flush=True)
