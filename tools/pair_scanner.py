"""
Pair Scanner — measures sigma and half-life across multiple Hyperliquid pairs.

Runs 100 bars (8.3 min at 5s) of live price data through the Kalman+OU
framework for every candidate pair, then ranks by expected net PnL per
trade after maker/taker fees.

Usage:
    python tools/pair_scanner.py

Output:
    Ranked table of pairs: sigma, hl, expected_gross, fee, expected_net
"""

import asyncio
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Make sure parent dir is in path so we can import bot modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperliquid.info import Info
from hyperliquid.utils import constants

from bot.kalman import KalmanHedgeRatio
from bot.spread import SpreadAnalyzer

logging.basicConfig(level=logging.WARNING)  # suppress noise during scan

# ── Configuration ────────────────────────────────────────────────────────────

NOTIONAL_USD      = 1000.0
TAKER_FEE_RATE    = 0.00045   # 0.045% HL base tier
MAKER_REBATE_RATE = 0.00015   # 0.015% HL base tier
ENTRY_Z           = 2.3
POLL_INTERVAL     = 5.0       # seconds between samples
N_BARS            = 120       # warmup + measurement window

# Pairs to test: (asset_a, asset_b)
CANDIDATE_PAIRS = [
    # Established liquid pairs
    ("ETH",    "BTC"),
    ("SOL",    "BTC"),
    ("SOL",    "ETH"),
    # High-vol candidates
    ("HYPE",   "ETH"),
    ("HYPE",   "BTC"),
    ("HYPE",   "SOL"),
    # Alt/meme
    ("DOGE",   "BTC"),
    ("DOGE",   "SOL"),
    ("AVAX",   "ETH"),
    ("AVAX",   "SOL"),
    ("LINK",   "ETH"),
    ("NEAR",   "SOL"),
    ("SUI",    "SOL"),
    ("ONDO",   "ETH"),
    ("AAVE",   "ETH"),
]


@dataclass
class PairState:
    asset_a:  str
    asset_b:  str
    kalman:   KalmanHedgeRatio = field(default_factory=lambda: KalmanHedgeRatio(delta=2e-5, R=5e-2))
    analyzer: SpreadAnalyzer   = field(default_factory=lambda: SpreadAnalyzer(window=100, halflife_lookback=100))
    last_price_a: float = 0.0
    last_price_b: float = 0.0


def compute_fee(price_a, sz_a, price_b, sz_b):
    """Round-trip fee: maker entry rebate + taker exit fee."""
    notional = price_a * sz_a + price_b * sz_b
    return -MAKER_REBATE_RATE * notional + TAKER_FEE_RATE * notional  # (taker-maker) × notional


def compute_expected_net(state: PairState) -> dict:
    """Compute expected net PnL metrics for a warmed-up pair."""
    z = state.analyzer.z_score()
    hl = state.analyzer.half_life()

    if z is None or hl is None:
        return None

    # Estimate sigma from last spread / z
    spreads = list(state.analyzer._buf)[-100:]
    import numpy as np
    sigma = float(np.std(spreads, ddof=1))

    sz_a = NOTIONAL_USD / state.last_price_a
    sz_b = (0.5 * sz_a * state.last_price_a) / state.last_price_b  # rough beta=0.5 for fee estimate
    fee = compute_fee(state.last_price_a, sz_a, state.last_price_b, sz_b)

    expected_gross = ENTRY_Z * sigma * NOTIONAL_USD
    expected_net   = expected_gross - fee

    return {
        "pair":           f"{state.asset_a}/{state.asset_b}",
        "sigma":          sigma,
        "hl_bars":        hl,
        "z_now":          z,
        "expected_gross": expected_gross,
        "fee":            fee,
        "expected_net":   expected_net,
        "beta":           state.kalman._beta if hasattr(state.kalman, '_beta') else None,
    }


async def run_scanner():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    states = [PairState(a, b) for a, b in CANDIDATE_PAIRS]

    print(f"\nScanning {len(CANDIDATE_PAIRS)} pairs × {N_BARS} bars × {POLL_INTERVAL}s = "
          f"~{N_BARS * POLL_INTERVAL / 60:.1f} min\n")
    print(f"{'Pair':15s} | {'Bars':4s} | {'z':6s} | {'hl':5s} | "
          f"{'sigma':8s} | {'E[gross]':8s} | {'fee':6s} | {'E[net]':8s}")
    print("-" * 80)

    bar = 0
    while bar < N_BARS:
        t0 = time.monotonic()

        # Fetch all mid prices once per tick
        try:
            loop = asyncio.get_event_loop()
            raw_mids = await loop.run_in_executor(None, info.all_mids)
            mids = {k: float(v) for k, v in raw_mids.items()}
        except Exception as e:
            print(f"Price fetch error: {e}", file=sys.stderr)
            await asyncio.sleep(POLL_INTERVAL)
            continue

        for s in states:
            pa = mids.get(s.asset_a)
            pb = mids.get(s.asset_b)
            if pa is None or pb is None:
                continue
            if pa < 1.0 or pb < 1.0:
                continue  # log-price Kalman invalid for sub-dollar assets
            s.last_price_a = pa
            s.last_price_b = pb
            _, spread = s.kalman.update(math.log(pa), math.log(pb))
            s.analyzer.push(spread)

        bar += 1

        # Print current status every 10 bars
        if bar % 10 == 0 or bar == N_BARS:
            print(f"\n=== Bar {bar}/{N_BARS} ===")
            results = []
            for s in states:
                r = compute_expected_net(s)
                if r:
                    results.append(r)

            # Sort by expected_net descending
            results.sort(key=lambda x: x["expected_net"], reverse=True)

            for r in results:
                hl_str = f"{r['hl_bars']:.1f}" if r['hl_bars'] else "N/A"
                net_mark = " ✓" if r["expected_net"] > 0 else "  "
                print(
                    f"{r['pair']:15s} | {len(states[0].analyzer):4d} | "
                    f"{r['z_now']:+6.2f} | {hl_str:5s} | "
                    f"{r['sigma']:.2e} | ${r['expected_gross']:7.4f} | "
                    f"${r['fee']:5.4f} | ${r['expected_net']:+7.4f}{net_mark}"
                )

        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, POLL_INTERVAL - elapsed)
        await asyncio.sleep(sleep_for)

    # Final ranking
    print("\n" + "=" * 80)
    print("FINAL RANKING — Expected net PnL per trade after maker/taker fees")
    print("=" * 80)
    final = []
    invalid = []
    for s in states:
        if s.last_price_a > 0.0 and s.last_price_b > 0.0 and (s.last_price_a < 1.0 or s.last_price_b < 1.0):
            low = s.asset_a if s.last_price_a < 1.0 else s.asset_b
            low_p = s.last_price_a if s.last_price_a < 1.0 else s.last_price_b
            invalid.append(f"{s.asset_a}/{s.asset_b} ({low}=${low_p:.4f})")
            continue
        r = compute_expected_net(s)
        if r:
            final.append(r)
    final.sort(key=lambda x: x["expected_net"], reverse=True)

    for rank, r in enumerate(final, 1):
        hl_str = f"{r['hl_bars']:.1f}b" if r['hl_bars'] else "N/A"
        viability = "FEE-POSITIVE" if r["expected_net"] > 0 else "fee-negative"
        print(
            f"#{rank:2d} {r['pair']:15s}  sigma={r['sigma']:.2e}  hl={hl_str:6s}  "
            f"E[gross]=${r['expected_gross']:.4f}  fee=${r['fee']:.4f}  "
            f"E[net]=${r['expected_net']:+.4f}  [{viability}]"
        )

    print("\nFee-positive pairs (expected net > $0 per trade):")
    pos = [r for r in final if r["expected_net"] > 0]
    if pos:
        for r in pos:
            print(f"  → {r['pair']}: E[net]=${r['expected_net']:+.4f}/trade, hl={r['hl_bars']:.1f}b")
    else:
        print("  (none found in current market conditions)")
    if invalid:
        print("\nSkipped (log-price Kalman invalid):")
        for label in invalid:
            print(f"  ✗ {label}")


if __name__ == "__main__":
    asyncio.run(run_scanner())
