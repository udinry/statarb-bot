"""
Funding Rate Scanner — shows Hyperliquid predicted funding rates and
identifies harvest opportunities.

A harvest trade collects funding by entering a position that will receive
the hourly funding payment, then exiting immediately after.

Break-even math (maker entry + taker exit, $1000 notional, 2 legs):
  fee = (taker - maker) * notional_a * (1 + beta) ≈ 0.0003 * $1500 = $0.45
  funding = rate * notional_a per hourly tick
  break-even: rate > $0.45 / $1000 = 0.045%/hr per leg

For a hedged pair (short high-rate asset + long correlated):
  net_rate = rate_a - rate_b * beta
  break-even: net_rate > 0.045%/hr

Usage:
    python tools/funding_scan.py
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperliquid.info import Info
from hyperliquid.utils import constants

NOTIONAL_USD      = 1000.0
TAKER_FEE_RATE    = 0.00045
MAKER_REBATE_RATE = 0.00015
ROUND_TRIP_FEE    = (TAKER_FEE_RATE - MAKER_REBATE_RATE) * NOTIONAL_USD  # $0.30 single-leg

# Threshold: hourly rate must exceed round-trip cost / notional
HARVEST_THRESHOLD_HOURLY = ROUND_TRIP_FEE / NOTIONAL_USD  # 0.03%/hr

# High-liquidity assets worth monitoring for harvest opportunities
WATCH_LIST = [
    "BTC", "ETH", "SOL", "HYPE", "BNB", "SUI", "AVAX",
    "DOGE", "LINK", "ONDO", "NEAR", "AAVE", "TON", "XMR",
    "CHIP", "FOGO", "SNX", "JTO", "W",
]


def main():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    print(f"Fetching predicted funding rates from Hyperliquid...")

    raw = info.post("/info", {"type": "predictedFundings"})
    now_ts = int(time.time() * 1000)

    # Build {coin: {rate_per_hour, next_funding_ms, minutes_until}}
    rates: dict[str, dict] = {}
    for coin_data in raw:
        coin = coin_data[0]
        for venue, data in coin_data[1]:
            if venue == "HlPerp":
                rate = float(data["fundingRate"])
                next_ts = int(data["nextFundingTime"])
                interval_h = int(data.get("fundingIntervalHours", 1))
                # Rate in the API is already per-interval; normalize to per-hour
                rate_per_hour = rate / interval_h
                minutes_until = (next_ts - now_ts) / 60_000
                rates[coin] = {
                    "rate_hr": rate_per_hour,
                    "rate_raw": rate,
                    "interval_h": interval_h,
                    "next_ts": next_ts,
                    "minutes_until": minutes_until,
                }
                break

    # Sort by absolute hourly rate descending
    sorted_coins = sorted(rates, key=lambda c: abs(rates[c]["rate_hr"]), reverse=True)

    print(f"\n{'Coin':12s} | {'Rate/hr':9s} | {'Rate/day':9s} | {'$/trade':7s} | "
          f"{'vs fee':7s} | {'Next in':7s} | Viable?")
    print("-" * 80)

    viable = []
    for coin in sorted_coins[:30]:
        d = rates[coin]
        rh = d["rate_hr"]
        funding_per_trade = abs(rh) * NOTIONAL_USD  # $ collected per hourly payment
        fee_ratio = funding_per_trade / ROUND_TRIP_FEE
        viable_mark = "YES" if abs(rh) > HARVEST_THRESHOLD_HOURLY else "no"
        if abs(rh) > HARVEST_THRESHOLD_HOURLY:
            viable.append((coin, d, funding_per_trade, fee_ratio))

        print(
            f"{coin:12s} | {rh*100:+8.5f}% | {rh*2400:+8.4f}% | "
            f"${funding_per_trade:6.4f} | "
            f"{fee_ratio:5.2f}x  | "
            f"{d['minutes_until']:5.1f}min | {viable_mark}"
        )

    print(f"\nBreak-even threshold: {HARVEST_THRESHOLD_HOURLY*100:.4f}%/hr "
          f"(${ROUND_TRIP_FEE:.2f} single-leg round-trip on ${NOTIONAL_USD:.0f})")

    if viable:
        print(f"\nHARVEST OPPORTUNITIES ({len(viable)} found):")
        for coin, d, funding, ratio in viable:
            direction = "LONG (receive from shorts)" if d["rate_hr"] < 0 else "SHORT (receive from longs)"
            print(f"  {coin}: {d['rate_hr']*100:+.5f}%/hr → ${funding:.4f}/trade ({ratio:.2f}x fee)")
            print(f"    Direction: {direction}")
            print(f"    Next funding: {d['minutes_until']:.1f} min")
            print(f"    Enter now and exit in ~{d['minutes_until']:.0f} min for full harvest")
    else:
        print(f"\nNo harvest opportunities — all rates near base ({0.00125:.5f}%/hr)")
        print("Check again during market stress events when funding spikes.")


if __name__ == "__main__":
    main()
