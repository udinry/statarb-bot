"""
StatArb bot — Hyperliquid perpetuals pairs trader.

Entry point. Runs the async main loop which:
  1. Fetches live mid prices for both assets
  2. Updates the Kalman filter hedge ratio
  3. Computes z-score and OU half-life on the spread
  4. Manages open position exits (reversion / stop-loss / time-stop)
  5. Evaluates new entry conditions including funding rate overlay
"""

import asyncio
import logging
import math
import os
import sys

import eth_account
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from bot.client import HyperliquidClient
from bot.execution import ExecutionEngine
from bot.funding import FundingRateChecker
from bot.kalman import KalmanHedgeRatio
from bot.spread import SpreadAnalyzer
from config import TradingConfig

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("statarb.log"),
    ],
)
logger = logging.getLogger("statarb")


# ----------------------------------------------------------------------
# SDK bootstrap
# ----------------------------------------------------------------------

def _build_sdk(cfg: TradingConfig) -> tuple[HyperliquidClient, Info]:
    private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("HYPERLIQUID_PRIVATE_KEY not set in environment")

    account = eth_account.Account.from_key(private_key)
    address = os.environ.get("HYPERLIQUID_ADDRESS", account.address)

    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    if cfg.paper_mode:
        # In paper mode we never call exchange methods, so a dummy is fine.
        exchange = None
    else:
        exchange = Exchange(
            account,
            constants.MAINNET_API_URL,
            account_address=address,
        )

    client = HyperliquidClient(address, exchange, info)
    return client, info


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

async def run(cfg: TradingConfig) -> None:
    client, info = _build_sdk(cfg)

    # Fetch per-asset size precision (szDecimals) so orders are correctly rounded for live mode.
    # HYPE=2, ETH=4, BTC=5, SOL=2. Default to 4 if asset not found.
    loop = asyncio.get_event_loop()
    try:
        universe_data, _ = await loop.run_in_executor(None, info.meta_and_asset_ctxs)
        sz_dec = {a["name"]: int(a.get("szDecimals", 4)) for a in universe_data["universe"]}
    except Exception:
        sz_dec = {}
    sz_a = sz_dec.get(cfg.asset_a, 4)
    sz_b = sz_dec.get(cfg.asset_b, 4)

    kalman = KalmanHedgeRatio(delta=cfg.kalman_delta, R=cfg.kalman_R)
    analyzer = SpreadAnalyzer(
        window=cfg.spread_window,
        halflife_lookback=cfg.halflife_lookback,
        hl_trend_lookback=cfg.hl_trend_lookback,
    )
    funding = FundingRateChecker(info, max_net_cost=cfg.max_net_funding_rate)
    engine = ExecutionEngine(client, cfg, sz_decimals_a=sz_a, sz_decimals_b=sz_b)

    logger.info("Size precision | %s=%d decimals %s=%d decimals",
                cfg.asset_a, sz_a, cfg.asset_b, sz_b)

    logger.info(
        "StatArb bot starting | pair=%s/%s entry_z=%.2f exit_z=%.2f stop_z=%.2f "
        "notional=$%.0f paper=%s",
        cfg.asset_a, cfg.asset_b,
        cfg.entry_z, cfg.exit_z, cfg.stop_z,
        cfg.notional_usd, cfg.paper_mode,
    )

    while True:
        try:
            await _tick(cfg, client, kalman, analyzer, funding, engine)
        except Exception:
            logger.exception("Unhandled exception in tick — continuing")

        await asyncio.sleep(cfg.poll_interval_seconds)


async def _tick(
    cfg: TradingConfig,
    client: HyperliquidClient,
    kalman: KalmanHedgeRatio,
    analyzer: SpreadAnalyzer,
    funding: FundingRateChecker,
    engine: ExecutionEngine,
) -> None:
    # ---- 1. Fetch prices ----
    try:
        prices = await client.get_mid_prices()
    except Exception as exc:
        logger.warning("Price fetch failed: %s", exc)
        return

    price_a = prices.get(cfg.asset_a)
    price_b = prices.get(cfg.asset_b)

    if price_a is None or price_b is None:
        logger.warning(
            "Missing price | %s=%s %s=%s",
            cfg.asset_a, price_a, cfg.asset_b, price_b,
        )
        return

    # Guard: log-price Kalman requires both prices > $1. If either price < $1,
    # log(price) is negative and the initial beta = log(A)/log(B) gets the wrong
    # sign, causing the bot to trade in reverse for hundreds of bars until the
    # filter converges. Abort the tick — this pair is unsupported in log-price space.
    if price_a < 1.0 or price_b < 1.0:
        logger.error(
            "PRICE < $1 | %s=%.4f %s=%.4f — log-price Kalman invalid for sub-dollar assets. "
            "Use a pair where both prices are > $1.",
            cfg.asset_a, price_a, cfg.asset_b, price_b,
        )
        return

    # ---- 2. Kalman filter → spread (log-price space) ----
    # Using log prices prevents the Kalman gain from absorbing 100% of the
    # spread every tick (which happens with raw prices where price_b ≈ 77 000).
    beta, spread = kalman.update(math.log(price_a), math.log(price_b))
    analyzer.push(spread)

    # ---- 3. Signal computation ----
    z = analyzer.z_score()
    half_life = analyzer.half_life()

    if z is None:
        logger.info(
            "Warming up | bars=%d/%d price_%s=%.4f price_%s=%.4f beta=%.4f spread=%.6f",
            len(analyzer), cfg.spread_window,
            cfg.asset_a, price_a, cfg.asset_b, price_b, beta, spread,
        )
        return

    logger.info(
        "TICK | %s=%.4f %s=%.4f beta=%.4f spread=%.6f z=%.3f hl=%s pos=%s",
        cfg.asset_a, price_a, cfg.asset_b, price_b,
        beta, spread, z,
        f"{half_life:.1f}b" if half_life else "N/A",
        engine.position.side.value if engine.position else "FLAT",
    )

    # ---- 4. Manage open position ----
    if engine.position is not None:
        if engine.should_stop_loss(z):
            await engine.exit(reason=f"stop_loss z={z:.3f}", price_a=price_a, price_b=price_b)
            return

        if engine.should_time_stop(half_life):
            age = engine.position.age_bars(cfg.poll_interval_seconds)
            await engine.exit(
                reason=f"time_stop age={age:.0f}b hl={half_life:.1f}",
                price_a=price_a, price_b=price_b,
            )
            return

        if engine.should_exit_reversion(z, price_a=price_a, price_b=price_b):
            gross = engine.compute_gross_pnl(price_a, price_b)
            fee_est = engine.estimate_round_trip_fees()
            await engine.exit(
                reason=f"reversion z={z:.3f} gross=${gross:.4f} fee_est=${fee_est:.4f}",
                price_a=price_a, price_b=price_b,
            )
        return

    # ---- 5. Look for new entry ----
    # Guard: only enter inside the valid zone [entry_z, stop_z).
    # Entering at |z| >= stop_z means we'd immediately stop-loss on any
    # continuation of the move — we entered above our own stop threshold.
    if not (cfg.entry_z <= abs(z) < cfg.stop_z):
        return

    # Guard: require established half-life (bars 100-199 have z but not hl).
    # Also skip if hl is too large — spread is trending, not mean-reverting.
    if cfg.require_half_life:
        if half_life is None:
            logger.info("Entry skipped | half_life not yet established (warming up)")
            return
        if half_life > cfg.max_half_life_bars:
            logger.info(
                "Entry skipped | half_life=%.1fb > max=%.1fb (trending spread)",
                half_life, cfg.max_half_life_bars,
            )
            return

    # Guard: skip entry when hl is INCREASING (spread losing mean-reversion property).
    # Catches trending regimes even when current hl is below max_half_life_bars.
    if analyzer.is_spread_trending():
        logger.info(
            "Entry skipped | hl trend detected (spread trending, not oscillating)",
        )
        return

    long_a = z < 0.0
    funding_ok, net_rate = await funding.evaluate(cfg.asset_a, cfg.asset_b, long_a)

    if not funding_ok:
        logger.info(
            "Entry skipped | funding unfavorable net_rate=%.6f (max=%.6f)",
            net_rate, cfg.max_net_funding_rate,
        )
        return

    await engine.enter(
        z=z,
        spread=spread,
        price_a=price_a,
        price_b=price_b,
        beta=beta,
        half_life=half_life,
    )


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run(TradingConfig()))
