"""Lightweight web dashboard for hermes-trading. Reads from state/ files
and (when configured) live Alpaca account data."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

STATE_DIR = Path("state")

app = FastAPI(title="hermes-trading")


def _alpaca_configured() -> bool:
    return bool(os.getenv("ALPACA_API_KEY", "").strip() and os.getenv("ALPACA_API_SECRET", "").strip())


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "").strip(),
    }


def _alpaca_base() -> str:
    return os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")


def _fetch_alpaca() -> dict | None:
    """Pull live account, positions, and recent orders straight from Alpaca.
    Returns None if not configured or the call fails (dashboard falls back to file data)."""
    if not _alpaca_configured():
        return None
    base = _alpaca_base()
    headers = _alpaca_headers()
    try:
        with httpx.Client(timeout=10, headers=headers) as client:
            acct = client.get(f"{base}/v2/account").json()
            positions = client.get(f"{base}/v2/positions").json()
            # closed orders, newest first
            orders = client.get(
                f"{base}/v2/orders",
                params={"status": "closed", "limit": 50, "direction": "desc"},
            ).json()
    except Exception as e:
        print(f"[dashboard] Alpaca fetch failed: {e}", flush=True)
        return None

    if not isinstance(acct, dict) or "equity" not in acct:
        return None

    equity = float(acct.get("equity", 0))
    last_equity = float(acct.get("last_equity", equity) or equity)
    cash = float(acct.get("cash", 0))

    pos_list = []
    if isinstance(positions, list):
        for p in positions:
            pos_list.append({
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "avg_entry": float(p.get("avg_entry_price", 0)),
                "market_value": float(p.get("market_value", 0)),
                "unrealized_pl": float(p.get("unrealized_pl", 0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
                "current_price": float(p.get("current_price", 0) or 0),
            })

    order_list = []
    if isinstance(orders, list):
        for o in orders:
            order_list.append({
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": o.get("filled_qty") or o.get("qty"),
                "notional": o.get("notional"),
                "filled_avg_price": o.get("filled_avg_price"),
                "status": o.get("status"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
            })

    return {
        "account_number": acct.get("account_number"),
        "status": acct.get("status"),
        "is_paper": "paper" in base.lower(),
        "equity": equity,
        "last_equity": last_equity,
        "cash": cash,
        "buying_power": float(acct.get("buying_power", 0)),
        "long_market_value": float(acct.get("long_market_value", 0)),
        "today_change_dollars": round(equity - last_equity, 2),
        "today_change_pct": round((equity - last_equity) / last_equity * 100, 3) if last_equity else 0,
        "positions": pos_list,
        "orders": order_list,
    }


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


def _read_treasury() -> dict:
    p = STATE_DIR / "treasury.json"
    if not p.exists():
        return {
            "vault_balance": 0.0, "stock_fund_balance": 0.0,
            "vault_deposits": [], "stock_fund_deposits": [],
            "dca_history": [], "vault_park_history": [],
        }
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _read_compare() -> dict | None:
    p = STATE_DIR / "compare_report.json"
    if not p.exists():
        return None
    try:
        rep = json.loads(p.read_text())
    except Exception:
        return None
    # Defensive: coerce any non-finite floats (Infinity/NaN from older reports)
    # to None so the JSON response can never 500 the dashboard.
    import math as _math

    def _clean(o):
        if isinstance(o, float):
            return o if _math.isfinite(o) else None
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        return o
    return _clean(rep)


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


def _portfolio_from_alpaca(alp: dict, trades: list[dict]) -> dict:
    """Build the portfolio summary from real Alpaca equity, using local trades
    for win/loss stats and the equity curve."""
    equity = alp["equity"]
    # Alpaca paper accounts start at 100k by default; allow override
    starting = float(os.getenv("ALPACA_STARTING_EQUITY", "100000"))
    realised_return = (equity - starting) / starting if starting else 0

    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl_pct", 0) < 0)
    win_rate = wins / (wins + losses) if (wins + losses) else 0

    # Equity curve from closed trades' realised dollar PnL, anchored at `starting`
    bal = starting
    curve = [{"t": "start", "balance": round(bal, 2)}]
    for t in trades:
        bal += t.get("pnl_dollars", 0.0)
        curve.append({"t": t.get("exit_time", ""), "balance": round(bal, 2)})
    # Final point = real current equity
    curve.append({"t": "now", "balance": round(equity, 2)})

    peak = starting
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt["balance"])
        dd = (peak - pt["balance"]) / peak if peak else 0
        max_dd = max(max_dd, dd)

    return {
        "source": "alpaca",
        "starting_balance": starting,
        "current_balance": round(equity, 2),
        "cash": round(alp["cash"], 2),
        "invested": round(alp["long_market_value"], 2),
        "realised_return_pct": round(realised_return * 100, 3),
        "today_change_dollars": alp["today_change_dollars"],
        "today_change_pct": alp["today_change_pct"],
        "max_drawdown_pct": round(max_dd * 100, 3),
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate * 100, 1),
        "equity_curve": curve,
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    trades = _read_trades()
    strategy = _read_yaml("strategy.yaml")
    goal = _read_yaml("goal.yaml")
    hypotheses = _read_hypotheses()
    heartbeat = _read_heartbeat()
    treasury = _read_treasury()

    alpaca = _fetch_alpaca()
    if alpaca is not None:
        portfolio = _portfolio_from_alpaca(alpaca, trades)
    else:
        portfolio = _compute_portfolio(trades)
        portfolio["source"] = "simulation"

    # DCA progress
    dca_total = sum(d.get("dollars", 0) for d in treasury.get("dca_history", []))
    park_total = sum(d.get("dollars", 0) for d in treasury.get("vault_park_history", []))

    return JSONResponse({
        "strategy": strategy,
        "goal": goal,
        "portfolio": portfolio,
        "alpaca": alpaca,
        "compare": _read_compare(),
        "trades": trades[-50:],
        "hypotheses": hypotheses[-20:],
        "heartbeat": heartbeat,
        "treasury": {
            "vault_balance": treasury.get("vault_balance", 0),
            "vault_parked_total_dollars": park_total,
            "stock_fund_balance": treasury.get("stock_fund_balance", 0),
            "dca_total_invested": dca_total,
            "dca_history": treasury.get("dca_history", [])[-20:],
            "vault_deposits": treasury.get("vault_deposits", [])[-20:],
            "stock_fund_deposits": treasury.get("stock_fund_deposits", [])[-20:],
        },
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
<div class=sub id=subtitle>Self-improving trading agent. Refreshes every 10s.</div>

<div id=alpaca-banner class=panel style="display:none;border-color:#3a4a6a;margin-bottom:24px">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
    <div>
      <span class="dot ok"></span><b id=alp-mode>Alpaca</b>
      <span class=muted id=alp-acct style="margin-left:8px"></span>
    </div>
    <div style="display:flex;gap:32px;flex-wrap:wrap">
      <div><div class=label>Account equity</div><div style="font-family:var(--mono);font-size:18px" id=alp-equity>—</div></div>
      <div><div class=label>Cash</div><div style="font-family:var(--mono);font-size:18px" id=alp-cash>—</div></div>
      <div><div class=label>Invested</div><div style="font-family:var(--mono);font-size:18px" id=alp-invested>—</div></div>
      <div><div class=label>Today</div><div style="font-family:var(--mono);font-size:18px" id=alp-today>—</div></div>
    </div>
  </div>
</div>

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

<div class=panel id=positions-panel style="display:none">
  <h2>Open positions (live from Alpaca)</h2>
  <table>
    <thead><tr><th>Symbol</th><th>Qty</th><th>Avg entry</th><th>Current</th><th>Market value</th><th>Unrealized P/L</th></tr></thead>
    <tbody id=positions></tbody>
  </table>
</div>

<div class=grid>
  <div class=card style="border-color:#3a5a3a"><div class=label>Vault (locked)</div>
    <div class=value id=vault>—</div>
    <div class=muted id=vault-sub style="font-size:12px;margin-top:6px">10% of every winning trade</div></div>
  <div class=card style="border-color:#5a3a5a"><div class=label>Stock fund (pending DCA)</div>
    <div class=value id=fund>—</div>
    <div class=muted id=fund-sub style="font-size:12px;margin-top:6px">20% skim, fires DCA at $25</div></div>
  <div class=card style="border-color:#3a4a6a"><div class=label>DCA invested</div>
    <div class=value id=dca>—</div>
    <div class=muted id=dca-sub style="font-size:12px;margin-top:6px">ticker not set</div></div>
</div>

<div class=panel id=compare-panel style="display:none">
  <h2>Backtest vs Live — is the edge holding? <span id=cmp-when class=muted style="font-size:12px;font-weight:400"></span></h2>
  <div id=cmp-verdict style="padding:12px 14px;border-radius:8px;margin-bottom:14px;font-weight:600"></div>
  <table>
    <thead><tr><th></th><th>Trades</th><th>Win rate</th><th>Expectancy/trade</th><th>Profit factor</th><th>Max DD</th><th>Total return</th></tr></thead>
    <tbody id=cmp-rows></tbody>
  </table>
  <div id=cmp-flags class=muted style="font-size:12px;margin-top:10px"></div>
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

<div class=panel id=orders-panel style="display:none">
  <h2>Alpaca order fills (live, last 50)</h2>
  <table>
    <thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th>Qty / Notional</th><th>Fill price</th><th>Status</th></tr></thead>
    <tbody id=orders></tbody>
  </table>
</div>

<div class=panel>
  <h2>Treasury activity</h2>
  <table>
    <thead><tr><th>Time</th><th>Type</th><th>Ticker</th><th>$ amount</th><th>Note</th></tr></thead>
    <tbody id=treasury-activity></tbody>
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
    const p = d.portfolio, g = d.goal, alp = d.alpaca;

    // Alpaca live banner
    const banner = document.getElementById('alpaca-banner');
    const posPanel = document.getElementById('positions-panel');
    if (alp) {
      banner.style.display = 'block';
      posPanel.style.display = 'block';
      document.getElementById('alp-mode').textContent = alp.is_paper ? 'Alpaca — Paper' : 'Alpaca — LIVE (real money)';
      document.getElementById('alp-acct').textContent = 'acct ' + (alp.account_number||'') + ' · ' + (alp.status||'');
      document.getElementById('alp-equity').textContent = fmtMoney(alp.equity);
      document.getElementById('alp-cash').textContent = fmtMoney(alp.cash);
      document.getElementById('alp-invested').textContent = fmtMoney(alp.long_market_value);
      const today = document.getElementById('alp-today');
      today.textContent = fmt(alp.today_change_dollars) + ' (' + fmt(alp.today_change_pct) + '%)';
      today.style.color = alp.today_change_dollars >= 0 ? 'var(--good)' : 'var(--bad)';
      document.getElementById('subtitle').textContent =
        'Live data from your Alpaca ' + (alp.is_paper ? 'paper' : 'LIVE') + ' account. Refreshes every 10s.';

      // Positions table
      document.getElementById('positions').innerHTML = (alp.positions||[]).map(pos => `
        <tr>
          <td>${pos.symbol}</td>
          <td>${pos.qty}</td>
          <td>$${pos.avg_entry.toFixed(2)}</td>
          <td>$${pos.current_price.toFixed(2)}</td>
          <td>${fmtMoney(pos.market_value)}</td>
          <td class="${pos.unrealized_pl>=0?'pnl-pos':'pnl-neg'}">${fmt(pos.unrealized_pl)} (${fmt(pos.unrealized_plpc)}%)</td>
        </tr>`).join('') || '<tr><td colspan=6 class=muted>No open positions — bot is flat, waiting for entry.</td></tr>';

      // Alpaca order fills
      document.getElementById('orders-panel').style.display = 'block';
      document.getElementById('orders').innerHTML = (alp.orders||[]).map(o => `
        <tr>
          <td>${(o.submitted_at||'').replace('T',' ').replace(/\\..*$/,'')}</td>
          <td>${o.symbol||''}</td>
          <td class="${o.side==='buy'?'pnl-pos':'pnl-neg'}">${o.side||''}</td>
          <td>${o.qty ? (+o.qty).toString() : (o.notional ? '$'+o.notional : '')}</td>
          <td>${o.filled_avg_price ? '$'+(+o.filled_avg_price).toFixed(2) : '—'}</td>
          <td class=muted>${o.status||''}</td>
        </tr>`).join('') || '<tr><td colspan=6 class=muted>No orders yet on Alpaca.</td></tr>';
    }

    document.getElementById('balance').textContent = fmtMoney(p.current_balance);
    document.getElementById('balance-sub').textContent = (p.source === 'alpaca')
      ? 'Alpaca equity · cash ' + fmtMoney(p.cash||0) + ' + invested ' + fmtMoney(p.invested||0)
      : 'started at ' + fmtMoney(p.starting_balance) + ' (simulation)';
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

    // Treasury cards
    const tr = d.treasury || {};
    document.getElementById('vault').textContent = fmtMoney(tr.vault_balance || 0);
    document.getElementById('vault-sub').textContent =
      (tr.vault_parked_total_dollars > 0)
        ? 'parked: ' + fmtMoney(tr.vault_parked_total_dollars)
        : '10% of every winning trade';
    document.getElementById('fund').textContent = fmtMoney(tr.stock_fund_balance || 0);
    document.getElementById('fund-sub').textContent =
      ((tr.stock_fund_balance || 0) >= 25)
        ? 'ready to DCA — waiting for ticker'
        : 'fires at $25 (' + Math.round((tr.stock_fund_balance||0)/25*100) + '%)';
    document.getElementById('dca').textContent = fmtMoney(tr.dca_total_invested || 0);
    const lastDca = (tr.dca_history && tr.dca_history.length) ? tr.dca_history[tr.dca_history.length-1] : null;
    document.getElementById('dca-sub').textContent = lastDca
      ? 'last buy: ' + lastDca.ticker + ' ' + fmtMoney(lastDca.dollars)
      : 'no buys yet — set DCA_TICKER';

    // Treasury activity feed (merge deposits, DCA buys, vault park buys)
    const events = [];
    (tr.vault_deposits||[]).forEach(e => events.push({t:e.t, type:'vault skim', ticker:'', amt:e.amount, note:''}));
    (tr.stock_fund_deposits||[]).forEach(e => events.push({t:e.t, type:'fund skim', ticker:'', amt:e.amount, note:''}));
    (tr.dca_history||[]).forEach(e => events.push({t:e.t, type:'DCA buy', ticker:e.ticker, amt:e.dollars, note:'order '+(e.order_id||'').slice(0,8)}));
    (tr.vault_park_history||[]).forEach(e => events.push({t:e.t, type:'vault park', ticker:e.ticker, amt:e.dollars, note:'order '+(e.order_id||'').slice(0,8)}));
    events.sort((a,b)=>b.t.localeCompare(a.t));
    document.getElementById('treasury-activity').innerHTML = events.slice(0,30).map(e=>`
      <tr>
        <td>${e.t.replace('T',' ').replace(/\\..*$/,'')}</td>
        <td>${e.type}</td>
        <td>${e.ticker}</td>
        <td>${fmtMoney(e.amt)}</td>
        <td class=muted>${e.note}</td>
      </tr>`).join('') || '<tr><td colspan=5 class=muted>No treasury activity yet — fires on the first winning trade.</td></tr>';

    document.getElementById('strategy').textContent = JSON.stringify(d.strategy, null, 2);

    // Backtest vs Live comparison
    const cmp = d.compare;
    if (cmp) {
      document.getElementById('compare-panel').style.display = 'block';
      const when = (cmp.generated_at||'').replace('T',' ').replace(/\\..*$/,'');
      document.getElementById('cmp-when').textContent = '· v' + cmp.strategy_version + ' · last ' + cmp.lookback_days + 'd · ' + when;
      const colors = {edge_holding:['#13301f','#4ade80'], watch:['#33270d','#fbbf24'],
                      underperforming:['#3a1414','#f87171'], insufficient_live_data:['#1b2430','#7a8597']};
      const c = colors[cmp.verdict.status] || colors.insufficient_live_data;
      const vEl = document.getElementById('cmp-verdict');
      vEl.style.background = c[0]; vEl.style.color = c[1];
      vEl.textContent = cmp.verdict.headline;
      const row = (label, m) => {
        if (!m || !m.trades) return `<tr><td>${label}</td><td colspan=6 class=muted>no trades</td></tr>`;
        const pf = m.profit_factor===null||m.profit_factor>999 ? 'inf' : m.profit_factor.toFixed(2);
        return `<tr><td><b>${label}</b></td><td>${m.trades}</td>
          <td>${(m.win_rate*100).toFixed(0)}%</td>
          <td class="${m.expectancy>=0?'pnl-pos':'pnl-neg'}">${(m.expectancy*100>=0?'+':'')}${(m.expectancy*100).toFixed(2)}%</td>
          <td>${pf}</td><td>${(m.max_drawdown*100).toFixed(1)}%</td>
          <td class="${m.total_return>=0?'pnl-pos':'pnl-neg'}">${(m.total_return*100>=0?'+':'')}${(m.total_return*100).toFixed(1)}%</td></tr>`;
      };
      document.getElementById('cmp-rows').innerHTML = row('Backtest', cmp.backtest) + row('Live', cmp.live);
      document.getElementById('cmp-flags').innerHTML = (cmp.verdict.flags||[]).map(f=>'⚠ '+f).join('<br>');
    }
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
