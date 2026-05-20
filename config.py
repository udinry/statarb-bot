import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # Pair
    asset_a: str = field(default_factory=lambda: os.getenv("ASSET_A", "ETH"))
    asset_b: str = field(default_factory=lambda: os.getenv("ASSET_B", "BTC"))

    # Kalman filter parameters — tuned for log-price space.
    # Q = delta/(1-delta) is the process noise; R is measurement noise.
    # With raw BTC prices (~77 000), K*price_b → 1 and the filter absorbs 100%
    # of the spread each tick (effectively zeroing it). Using log prices keeps
    # price_b ≈ 11, so Q*price_b^2 << R and the spread persists correctly.
    kalman_delta: float = 2e-5   # hedge-ratio drift speed; slower → spread persists longer (real price reversion, not Kalman pull-back)
    kalman_R: float = 5e-2       # measurement noise (log-price units)

    # Z-score thresholds
    entry_z: float = 2.0       # open trade
    exit_z: float = 0.0        # close trade on full reversion (z crosses 0); time_stop backstops if z stalls
    stop_z: float = 3.5        # stop loss — spread blowing out; >3.5 is momentum not reversion

    # Rolling window for z-score normalization (in bars)
    spread_window: int = 100

    # Bars of history for Ornstein-Uhlenbeck half-life estimation
    # 100 bars = 33 full reversion cycles at hl≈3b — statistically as robust as 200
    # and aligns warmup with spread_window so both are ready at bar 100 (8.3 min)
    halflife_lookback: int = 100

    # Require OU half-life to be established before entering any trade.
    # Entries in bars 100-199 (z ready but hl not) risk entering momentum moves.
    require_half_life: bool = True
    # Skip entry if hl > this many bars — spread is trending, not mean-reverting.
    max_half_life_bars: float = 5.0

    # Close if trade age exceeds this multiple of the OU half-life.
    # ETH/BTC hl ≈ 1.8 bars. At 2x that's only 18s — too aggressive.
    # 5x = 45s gives the spread enough time to revert before bailing.
    time_stop_multiplier: float = 7.0

    # Notional USD per leg (leg B is beta-adjusted to match leg A notional)
    notional_usd: float = field(
        default_factory=lambda: float(os.getenv("NOTIONAL_USD", "1000"))
    )

    # Maximum acceptable net 8h funding rate for the combined position
    # Positive = we are net payers. Reject entry if net_rate > this threshold.
    max_net_funding_rate: float = 0.0001  # 0.01 % per 8 h

    # Fee simulation for paper mode (Hyperliquid base tier, May 2026).
    # Live strategy: maker entry (post-only limit) + taker exit (market close).
    # Actual HL rates: taker 0.045%, maker rebate 0.015% per order.
    # Net round-trip (maker in, taker out, 2 legs each side): 0.03% × combined_notional.
    taker_fee_rate: float = 0.00045   # 0.045% taker — applied to exit notional
    maker_rebate_rate: float = 0.00015 # 0.015% rebate — subtracted from entry notional

    # Max slippage tolerated on a market order before we treat it as failed
    order_slippage: float = 0.01  # 1%

    # Main loop poll interval in seconds
    poll_interval_seconds: float = 5.0

    # Paper mode — set PAPER_MODE=false in .env to trade live
    paper_mode: bool = field(
        default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() != "false"
    )
