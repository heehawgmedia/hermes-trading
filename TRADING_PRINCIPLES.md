# Hermes Trading Principles — how this bot stays profitable

These are hard-won lessons from auditing and backtesting this bot. Treat them as
standing rules when you reflect and edit `state/strategy.yaml`. They override any
instinct to chase a single flashy metric.

## The prime directive: expectancy, not win rate
- A strategy is profitable when **expectancy > 0**: `avg_win × win_rate > avg_loss × loss_rate`.
- **Win rate alone is gameable** and meaningless in isolation. You can hit 90% win
  rate by taking tiny profits and letting losers run — and still go broke. Optimize
  **profit factor (>1.3)** and **expectancy per trade**, with win rate as a *secondary*
  health check, never the target.
- Always compare against **buy-and-hold**. If the bot can't beat just holding BTC,
  it has no edge. This bot's edge is *sitting out downtrends* — that's the whole game.

## Never fight the trend (the #1 rule)
- **Only go long when price is above the long trend SMA (SMA200 on 1H).** Buying
  oversold dips in a downtrend = catching falling knives = the single biggest
  money-loser we found. It cost the account -12% before this rule existed.
- The winning construction is **pullback-in-uptrend**: a *longer*-timeframe uptrend
  filter (SMA200) + a *short*-term oversold entry (RSI<45). They must be on different
  horizons or they contradict each other (SMA50+RSI35 on the same TF never triggers).

## Data integrity is non-negotiable
- Indicators are only valid on **real OHLC candles** (Alpaca bars), never sampled
  spot prices. A noisy/stale price series makes every signal worthless.
- If `candle_source` in the heartbeat is not `alpaca-bars`, something is broken —
  investigate before trusting any signal.

## Reward:risk and sizing
- Keep **take_profit_pct > stop_loss_pct** (reward:risk ≥ 1.5:1). Current 5%/3% = 1.67:1.
  A negative R:R needs an unrealistic win rate just to break even.
- **Cap position size** (currently 30% of cash). Crypto gaps overnight; a 50%+
  position can blow through the stop. Single-trade account risk ≈ size × stop_loss_pct.

## How to reflect (process discipline)
1. **Backtest before you deploy.** Run `backtest.py` / `backtest_explore.py` on the
   change. Never ship a parameter you haven't validated on historical candles.
2. **One variable per cycle.** Change exactly one thing; predict its effect on BOTH
   expectancy and drawdown; reject changes that help one while breaking the other.
3. **Respect the 8% max-drawdown hard limit.** No edge is worth breaching it.
4. **Preserve history** — every prior version to `state/history/`, every hypothesis
   to `state/hypotheses.jsonl`.
5. A change that improves win rate but worsens total return or breaches drawdown is
   a **FAILED** cycle — revert it.

## Current validated baseline (v06, 2026-06-13)
Pullback-in-uptrend on BTC/USD 1H: `price > SMA200 & RSI < 45`, exit on `RSI ≥ 60`
or `+5% TP` or `-3% SL`, size 30%. Backtested across 60/90/120/180-day windows:
62-73% win rate, +1% to +5.6% return, <5.2% drawdown — while buy-and-hold was
-5% to -27%. Evolve FROM this baseline; don't regress below it without backtest proof.
