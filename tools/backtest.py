"""
Historical backtest on Hyperliquid candles data.

Uses 1-minute OHLC bars from the HL API to validate pair mean-reversion
over historical data. Because bars are 1-minute (not 5-second like live),
parameters are adjusted proportionally.

Live: 5s bars, window=100, hl~7.3b = 36.5s
1-min: 60s bars, window=15, hl~0.6b ← mostly noise, use window=30 instead

This backtest primarily validates:
1. Was the HYPE/SOL spread mean-reverting over the past N days?
2. Were there trending regimes that would have caused losses?
3. What is the approximate win rate and fee-adjusted P&L?

Usage:
    python tools/backtest.py [--pair HYPE/SOL] [--days 7] [--entry-z 2.0]
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from hyperliquid.info import Info
from hyperliquid.utils import constants

from bot.kalman import KalmanHedgeRatio
from bot.spread import SpreadAnalyzer


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    asset_a: str = "HYPE"
    asset_b: str = "SOL"
    days: int = 7
    interval: str = "1m"          # candle resolution
    window: int = 30              # rolling window in bars (30 min = 0.5h)
    halflife_lookback: int = 30
    hl_trend_lookback: int = 10
    entry_z: float = 2.0
    exit_z: float = 0.0
    stop_z: float = 3.5
    max_half_life_bars: float = 30.0  # 30 min (scaled from 8 bars × 5s = 40s)
    time_stop_multiplier: float = 7.0
    notional_usd: float = 1000.0
    taker_fee_rate: float = 0.00045
    maker_rebate_rate: float = 0.00015
    kalman_delta: float = 2e-5
    kalman_R: float = 5e-2


# ── Position ───────────────────────────────────────────────────────────────

@dataclass
class BacktestPosition:
    long_a: bool
    entry_idx: int
    entry_spread: float
    entry_z: float
    entry_price_a: float
    entry_price_b: float
    sz_a: float
    sz_b: float
    half_life: Optional[float]


# ── Main ────────────────────────────────────────────────────────────────────

def fetch_candles(info: Info, coin: str, interval: str, days: int) -> list[dict]:
    import time
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    return info.candles_snapshot(coin, interval, start_ms, now_ms)


def run_backtest(cfg: BacktestConfig) -> dict:
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    print(f"Fetching {cfg.days}d of {cfg.interval} candles for {cfg.asset_a} and {cfg.asset_b}...")
    candles_a = fetch_candles(info, cfg.asset_a, cfg.interval, cfg.days)
    candles_b = fetch_candles(info, cfg.asset_b, cfg.interval, cfg.days)

    # Align by timestamp (use close prices)
    ts_a = {c["t"]: float(c["c"]) for c in candles_a}
    ts_b = {c["t"]: float(c["c"]) for c in candles_b}
    common_ts = sorted(set(ts_a) & set(ts_b))
    prices_a = [ts_a[t] for t in common_ts]
    prices_b = [ts_b[t] for t in common_ts]

    print(f"Aligned bars: {len(common_ts)} (from {len(candles_a)} {cfg.asset_a}, {len(candles_b)} {cfg.asset_b})")

    kalman = KalmanHedgeRatio(delta=cfg.kalman_delta, R=cfg.kalman_R)
    analyzer = SpreadAnalyzer(
        window=cfg.window,
        halflife_lookback=cfg.halflife_lookback,
        hl_trend_lookback=cfg.hl_trend_lookback,
    )

    trades = []
    position: Optional[BacktestPosition] = None
    gross_pnl = 0.0
    net_pnl = 0.0
    equity_curve = [0.0]

    for i, (pa, pb) in enumerate(zip(prices_a, prices_b)):
        beta, spread = kalman.update(math.log(pa), math.log(pb))
        analyzer.push(spread)

        z = analyzer.z_score()
        hl = analyzer.half_life()

        if z is None:
            continue

        # ── Manage open position ──
        if position is not None:
            age = i - position.entry_idx

            # Stop loss
            if abs(z) >= cfg.stop_z:
                gross, net = _close(position, pa, pb, cfg, "stop_loss")
                gross_pnl += gross
                net_pnl += net
                equity_curve.append(net_pnl)
                trades.append({"reason": "stop_loss", "gross": gross, "net": net, "age_bars": age, "entry_z": position.entry_z, "exit_z": z})
                position = None
                continue

            # Time stop
            if hl and age > cfg.time_stop_multiplier * hl:
                gross, net = _close(position, pa, pb, cfg, "time_stop")
                gross_pnl += gross
                net_pnl += net
                equity_curve.append(net_pnl)
                trades.append({"reason": "time_stop", "gross": gross, "net": net, "age_bars": age, "entry_z": position.entry_z, "exit_z": z})
                position = None
                continue

            # Reversion exit
            z_ok = (position.long_a and z >= -cfg.exit_z) or (not position.long_a and z <= cfg.exit_z)
            if z_ok:
                gross, net = _close(position, pa, pb, cfg, "reversion")
                gross_pnl += gross
                net_pnl += net
                equity_curve.append(net_pnl)
                trades.append({"reason": "reversion", "gross": gross, "net": net, "age_bars": age, "entry_z": position.entry_z, "exit_z": z})
                position = None
            continue

        # ── Entry conditions ──
        if not (cfg.entry_z <= abs(z) < cfg.stop_z):
            continue
        if hl is None or hl > cfg.max_half_life_bars:
            continue
        if analyzer.is_spread_trending():
            continue

        long_a = z < 0.0
        sz_a = round(cfg.notional_usd / pa, 6)
        sz_b = round((beta * sz_a * pa) / pb, 6)
        position = BacktestPosition(
            long_a=long_a, entry_idx=i, entry_spread=spread, entry_z=z,
            entry_price_a=pa, entry_price_b=pb,
            sz_a=max(sz_a, 1e-6), sz_b=max(sz_b, 1e-6), half_life=hl,
        )

    # Close any open position at end
    if position is not None:
        gross, net = _close(position, prices_a[-1], prices_b[-1], cfg, "eod")
        gross_pnl += gross
        net_pnl += net
        equity_curve.append(net_pnl)
        trades.append({"reason": "eod", "gross": gross, "net": net, "age_bars": len(common_ts) - position.entry_idx, "entry_z": position.entry_z, "exit_z": 0})

    return {
        "total_bars": len(common_ts),
        "trades": trades,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "equity_curve": equity_curve,
    }


def _close(pos: BacktestPosition, pa: float, pb: float, cfg: BacktestConfig, reason: str):
    if pos.long_a:
        gross = (pa - pos.entry_price_a) * pos.sz_a - (pb - pos.entry_price_b) * pos.sz_b
    else:
        gross = -(pa - pos.entry_price_a) * pos.sz_a + (pb - pos.entry_price_b) * pos.sz_b
    entry_notional = pos.entry_price_a * pos.sz_a + pos.entry_price_b * pos.sz_b
    exit_notional = pa * pos.sz_a + pb * pos.sz_b
    fees = (-cfg.maker_rebate_rate * entry_notional + cfg.taker_fee_rate * exit_notional)
    return gross, gross - fees


def print_results(result: dict, cfg: BacktestConfig) -> None:
    trades = result["trades"]
    if not trades:
        print("No trades executed.")
        return

    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    rev = [t for t in trades if t["reason"] == "reversion"]
    stops = [t for t in trades if t["reason"] == "stop_loss"]
    tstops = [t for t in trades if t["reason"] == "time_stop"]
    eods = [t for t in trades if t["reason"] == "eod"]

    avg_win = np.mean([t["net"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["net"] for t in losses]) if losses else 0
    win_rate = len(wins) / len(trades) * 100

    print(f"\n{'='*70}")
    print(f"BACKTEST: {cfg.asset_a}/{cfg.asset_b}  "
          f"{cfg.days}d {cfg.interval} bars  "
          f"window={cfg.window}  entry_z={cfg.entry_z}")
    print(f"{'='*70}")
    print(f"Total bars:     {result['total_bars']}")
    print(f"Total trades:   {len(trades)}")
    print(f"  Reversion:    {len(rev)}")
    print(f"  Stop-loss:    {len(stops)}")
    print(f"  Time-stop:    {len(tstops)}")
    print(f"  End-of-data:  {len(eods)}")
    print(f"Win rate:       {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg win:        ${avg_win:+.4f}")
    print(f"Avg loss:       ${avg_loss:+.4f}")
    print(f"Profit factor:  {abs(avg_win/avg_loss):.2f}x" if avg_loss < 0 else "No losses")
    print(f"")
    print(f"Gross PnL:      ${result['gross_pnl']:+.4f}")
    print(f"Net PnL:        ${result['net_pnl']:+.4f}")
    print(f"Per trade avg:  ${result['net_pnl']/len(trades):+.4f}")
    print(f"")

    ec = result["equity_curve"]
    peak = max(ec)
    trough_after_peak = min(ec[ec.index(peak):]) if peak > 0 else min(ec)
    max_dd = peak - trough_after_peak
    print(f"Max drawdown:   ${max_dd:.4f}")
    print(f"Peak equity:    ${peak:.4f}")
    print(f"Final equity:   ${ec[-1]:.4f}")
    print(f"")

    if len(trades) > 5:
        print("Last 5 trades:")
        for t in trades[-5:]:
            print(f"  {t['reason']:12s} entry_z={t['entry_z']:+.2f} exit_z={t['exit_z']:+.2f} "
                  f"age={t['age_bars']:3d}b gross=${t['gross']:+.4f} net=${t['net']:+.4f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="HYPE/SOL")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--window", type=int, default=30)
    args = parser.parse_args()

    a, b = args.pair.split("/")
    cfg = BacktestConfig(
        asset_a=a, asset_b=b,
        days=args.days,
        entry_z=args.entry_z,
        window=args.window,
        halflife_lookback=args.window,
        hl_trend_lookback=max(5, args.window // 3),
    )

    result = run_backtest(cfg)
    print_results(result, cfg)


if __name__ == "__main__":
    main()
