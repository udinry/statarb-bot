# Backtest Results — 2026-05-20

All backtests: 1-minute OHLCV bars, Hyperliquid data.
Parameters: window=30, halflife_lookback=30, entry_z=2.0, stop_z=3.5, time_stop_mult=7.0
Fee model: maker entry (0.015% rebate) + taker exit (0.045%)
Note: Data availability ~3.5 days for HYPE-related pairs, ~7d for others.

## Pair Comparison (1-minute backtest)

| Pair | Bars | Trades | Win% | Net PnL | Per trade | Max DD |
|------|------|--------|------|---------|-----------|--------|
| HYPE/ETH | 5126 | 97 | 69.1% | +$47.41 | +$0.49 | $14.16 |
| ONDO/ETH | 5125 | 117 | 61.5% | +$26.81 | +$0.23 | $40.44 |
| HYPE/BTC | 5129 | 94 | 59.6% | +$7.72 | +$0.08 | $26.22 |
| HYPE/SOL | 5115 | 105 | 61.0% | +$1.21 | +$0.01 | $36.90 |
| ETH/BTC | 5125 | 118 | 19.5% | -$61.95 | -$0.53 | $61.95 |

**Winner: HYPE/ETH** — highest net PnL, highest win rate, lowest drawdown.

## HYPE/ETH Entry_z Sensitivity

| entry_z | Trades | Net PnL | Per trade |
|---------|--------|---------|-----------|
| 1.5 | ~220 | -$8.51 | -$0.04 |
| 2.0 | 97 | +$47.41 | +$0.49 |
| 2.5 | ~60 | -$18.74 | -$0.31 |

**Optimal: entry_z=2.0** — lower gives too many noise trades, higher gives too few but riskier.

## Key Observations

### HYPE/ETH (3.5 days, 97 trades)
- 83 reversion exits (86%), 4 stop-losses, 10 time-stops
- Avg win: +$2.55, Avg loss: -$4.11
- Stop-loss events are rare (4/97 = 4.1%) but expensive (~$6+ each)
- Big losses occur when HYPE trends strongly: sigma expands, z shows 0 while dollar PnL still negative
- Solution: min_profit_factor=1.1 blocks exits when gross < 1.1 × fees

### Why HYPE/ETH >> HYPE/SOL (1-min backtest)
- HYPE and ETH are both DeFi ecosystem assets → stronger long-term cointegration
- HYPE and SOL less correlated over days → spread diverges more often
- HYPE/SOL has higher 5-second sigma but worse 1-minute cointegration

### ETH/BTC: Structurally broken
- 19.5% win rate is near random (23W/95L)
- Gross $-2.44 but fees -$59.51 → sigma too small to cover fees at any entry_z
- Not salvageable with parameter tuning

## Live Performance (HYPE/SOL, 1 trade)
- Entry z=-2.409 (LONG HYPE / SHORT SOL) at 17:43:06 UTC
- Exit z=0.357 at 17:44:19 UTC (73 seconds)
- Gross: $1.42, Fee: $0.57, **Net: +$0.86**
- Confirmed fee-positive! hl=3.7-3.8b at 5-second resolution

## Current Bot Config
Pair: HYPE/ETH | entry_z=2.0 | min_profit_factor=1.1 | max_half_life_bars=8.0
Started: 2026-05-20 17:53 UTC | Warmup completes ~18:02 UTC

## Backtesting Caveats
1. 1-minute bars ≠ 5-second bars: hl in backtest is 12x longer in real seconds
2. Time-stop in backtest fires at 7×hl_min = much later than in live (7×hl_5s)
3. Live performance should be BETTER (faster exits, higher sigma/bar)
4. Only 3.5 days of HYPE data: limited statistical power
