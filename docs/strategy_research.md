# StatArb Strategy Research

## Core Question: How to build a fee-positive pairs trading bot on Hyperliquid?

---

## 1. Fee Structure (Hyperliquid Base Tier, May 2026)

| Order type | Rate | Notes |
|---|---|---|
| Taker (market) | 0.045% | Applied to each order's notional |
| Maker rebate | 0.015% | Received for post-only limit orders that add liquidity |

**Round-trip cost (maker entry + taker exit, 2 legs):**
```
fee = (-maker_rebate + taker) * (notional_a + notional_b)
    = (0.00045 - 0.00015) * ~$1500
    = 0.0003 * $1500 = $0.45/trade
```

**Fee-positive condition:**
```
entry_z * sigma * notional_a > fee
entry_z * sigma > 0.0003
At entry_z=2.0: sigma > 1.5e-4 (0.015% log-unit spread std)
```

Volume tier discounts: at $5M/day → taker=0.038%, maker=0. At $500M/day → taker=0.028%, maker=0.
These are unreachable for a single-bot operation; base tier costs dominate.

---

## 2. Pair Empirical Analysis (2026-05-20, 120 bars × 5s live data)

| Pair | sigma | hl (bars) | E[gross] | fee | E[net] | Viable? |
|---|---|---|---|---|---|---|
| HYPE/SOL | 1.77e-3 | 7.3 | $3.54 | $0.45 | +$3.09 | YES ★★★ |
| HYPE/ETH | 1.32e-3 | 4.3 | $2.65 | $0.45 | +$2.20 | YES ★★★ |
| HYPE/BTC | 1.04e-3 | 3.0 | $2.07 | $0.45 | +$1.62 | YES ★★ |
| NEAR/SOL | 9.06e-4 | 13.2 | $1.81 | $0.45 | +$1.36 | BLOCKED (hl>8) |
| ONDO/ETH | 8.19e-4 | 4.6 | $1.64 | $0.45 | +$1.19 | YES ★★ |
| DOGE/SOL | 7.56e-4 | 5.8 | $1.51 | $0.45 | +$1.06 | YES ★ |
| SUI/SOL | 6.96e-4 | 4.3 | $1.39 | $0.45 | +$0.94 | YES ★ |
| ETH/BTC | 1.91e-4 | 2.8 | $0.38 | $0.45 | -$0.07 | NO |

**ETH/BTC is structurally fee-negative.** Even with higher-vol days (sigma peaks ~2e-4),
it can barely break even. The problem: ETH and BTC are TOO correlated — spreads are tiny.

**HYPE pairs dominate** because HYPE (Hyperliquid's native token) has 3-4x the daily vol of ETH/SOL
and its price is not tightly coupled to any single alt — it's driven by DEX activity.

---

## 3. Kalman Filter Calibration

The Kalman filter tracks the dynamic hedge ratio beta = log(price_a) / log(price_b).

Key parameter: `delta` (process noise Q = delta/(1-delta)):
- Too large (1e-4): filter adapts in 4 bars, absorbs 16.8%/bar — spread artificially returns to 0 via filter, not real price reversion. Signals are phantom.
- Too small (1e-6): filter barely adapts — beta stays near initial estimate even as true ratio drifts. Spread accumulates real drift.
- **Correct (2e-5)**: absorbs ~4.6%/bar. Spread persists 15-20 bars for real mean-reversion to dominate.

For log-price space (log(~50) ≈ 3.9, log(~87) ≈ 4.5): Kalman gain K ≈ P/(P+R).
With R=5e-2 and P small (steady-state), K is small → slow adaptation → spread persists.
This is CORRECT behavior for pairs trading.

---

## 4. OU Half-Life Calibration

Model: ΔS_t = α + β·S_{t-1} + ε, where β < 0 for mean-reversion.
Half-life = -ln(2) / β

Observed values:
- ETH/BTC: hl = 1.2-2.8 bars (6-14 seconds) — very fast
- SOL/BTC: hl = 2.3-3.9 bars (12-20 seconds)
- HYPE/SOL: hl = 7.3 bars (36.5 seconds) — scanner measured, confirmed
- HYPE/ETH: hl = 4.3 bars (21.5 seconds)

**Why hl matters:**
1. `time_stop = time_stop_multiplier × hl` — wrong hl → premature exits
2. Large hl (>max_half_life_bars) indicates trending spread → skip entry
3. Increasing hl trend → spread losing mean-reversion property → regime change

**For HYPE/SOL at hl=7.3b:**
- time_stop fires at 7 × 7.3 = 51 bars = 255 seconds
- After 3×hl=21.9 bars, spread has decayed to 12.5% of initial → should cross z=0 well before time_stop

---

## 5. Z-Score Thresholds

| Parameter | Value | Rationale |
|---|---|---|
| entry_z | 2.0 | 2σ entry captures significant dislocations; lower → more noise, higher → fewer trades |
| exit_z | 0.0 | Full zero-crossing maximizes expected capture; 0.5 exits too early (misses overshoot) |
| stop_z | 3.5 | At z>3.5 the spread is MORE LIKELY trend than reversion (empirically observed for ETH/BTC) |

For HYPE/SOL at sigma=1.77e-3:
- 2.0σ entry: spread = 3.54e-3 from mean → gross = $3.54 if full reversion
- 3.5σ stop: loss = (3.5 - 2.0) × 1.77e-3 × $1000 = $2.66 (still profitable on 2:1 win/loss)

---

## 6. Regime Detection

**Problem:** SOL/BTC failure (2026-05-20 afternoon):
- Entry at z=-2.0, spread continued to z=-3.0+
- Root cause: hl INCREASED from 2.3b → 3.8b during the session → spread was trending, not oscillating
- The max_half_life_bars=5.0 check didn't catch this because hl was still under 5

**Solution implemented: `is_spread_trending()`**
- Tracks last 20 hl estimates
- Computes OLS slope of hl over time
- Returns True if slope > 0.03 bars/tick (hl growing faster than 1 bar per 30 ticks = 150 seconds)
- This catches the SOL/BTC pattern: hl 2.3→3.8 in ~20 bars = slope ≈ 0.075 > 0.03

---

## 7. Funding Rate Harvesting (Research)

Hyperliquid pays funding hourly (1-hour interval). Predicted rates available via `/info` POST with `{"type": "predictedFundings"}`.

**Break-even analysis for harvest trade:**
- Fee for single-asset position (maker in, taker out): 0.03% = $0.30 on $1000
- For a hedged pair (2 legs): 0.03% × ~$1500 = $0.45
- Break-even hourly rate: $0.45 / $1000 = 0.045%/hr

**Current market conditions (2026-05-20):**
- Highest rates: CHIP (-0.028%/hr), FOGO (-0.027%/hr), SNX (-0.008%/hr)
- Base rate: 0.00125%/hr (all major assets)
- Maximum found: 0.028%/hr (below the 0.045% threshold)

**Conclusion:** Funding harvesting is currently NOT VIABLE at base volume tier.
Would only work during extreme market stress events when rates spike to 0.1%+/hr (e.g., liquidation cascades, major protocol news).

---

## 8. Current Strategy: Statistical Arbitrage on HYPE/SOL

### Entry conditions (all must be met):
1. `abs(z) >= entry_z (2.0)` — significant spread dislocation
2. `abs(z) < stop_z (3.5)` — not already in momentum zone
3. `half_life is not None` — OU model has converged (100+ bars warmup)
4. `half_life <= max_half_life_bars (8.0)` — spread is oscillating, not trending
5. `is_spread_trending() == False` — hl not increasing over last 20 estimates
6. `net_funding_rate <= max_net_funding_rate` — acceptable funding cost

### Exit conditions (checked in order):
1. `abs(z) >= stop_z`: stop-loss exit immediately
2. `age_bars > time_stop_multiplier × half_life`: time-stop (stale trade)
3. `z crosses exit_z (0.0)` toward mean: take profit

### Expected performance on HYPE/SOL:
- E[gross] per trade: ~$3.54 (at full reversion from z=2.0 to z=0)
- E[fee] per trade: ~$0.45
- E[net] per trade: ~+$3.09 (7x fee coverage)
- Expected win rate: depends on regime stability (80%+ in stationary regimes)
- Expected trade frequency: ~6-12 trades/hour when z crosses 2.0 regularly

---

## 9. Implementation Architecture

```
main.py (async tick loop, 5s)
  │
  ├── KalmanHedgeRatio (bot/kalman.py)
  │     └── delta=2e-5, R=5e-2, log-price space
  │
  ├── SpreadAnalyzer (bot/spread.py)  
  │     ├── z_score() — rolling 100-bar window, sigma floor 3e-5
  │     ├── half_life() — OU OLS regression, 100-bar window
  │     └── is_spread_trending() — hl slope > 0.03/tick over 20 hl estimates
  │
  ├── FundingRateChecker (bot/funding.py)
  │     └── 30s cache, rejects entry if net_cost > 0.01%/8h
  │
  └── ExecutionEngine (bot/execution.py)
        ├── enter() — paper: instant fill at mid; live: asyncio.gather both legs
        ├── exit() — paper: fee sim; live: gather both closes
        ├── compute_gross_pnl() — real-time unrealized PnL
        ├── estimate_round_trip_fees() — based on entry notional
        ├── should_exit_reversion() — z-cross + optional min_profit_factor
        ├── should_stop_loss() — |z| >= stop_z
        └── should_time_stop() — age > multiplier * hl
```

---

## 10. Next Steps

1. **Live results on HYPE/SOL** — monitor first 20 trades, verify E[net] ≈ +$3.09
2. **Multi-pair runner** — simultaneously run HYPE/SOL + HYPE/ETH for diversification
3. **Backtesting** — historical OHLCV data from HL for out-of-sample validation
4. **Adaptive entry_z** — raise entry_z when sigma is very high (avoid false signals at low z)
5. **Live mode** — when paper results confirm E[net] > 0 consistently over 50+ trades
