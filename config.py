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
    entry_z: float = 2.3       # open trade (raised from 2.0: higher expected gross per trade; E[net]=$1.66 vs $1.39)
    exit_z: float = 0.1        # exit when z <= 0.1 (captures near-zero reversions that bounce before crossing 0)
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
    max_half_life_bars: float = 6.5   # Trade2 lost $6.67 entering at hl=7.5b (1.36× typical 5.5b) — pump exposure too long
    # Skip entry if hl < this many bars. Blocks sub-1.5b OLS estimates which are unreliable.
    # Note: low hl is actually safer (time_stop fires faster, less adverse drift exposure).
    # 4.0 was too conservative — it blocked the entire 1.7-2.4b fast-reversion regime.
    min_half_life_bars: float = 1.5

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

    # Only allow reversion exit when gross PnL > fees × this factor.
    # 0.0 = disabled (exit whenever z crosses exit_z).
    # 1.1 = require gross > 110% of round-trip fees before taking profit.
    # Prevents quick exits on tiny spread moves that don't cover trading costs.
    # Useful for high-sigma pairs (HYPE/ETH); leave at 0 for ETH/BTC.
    min_profit_factor: float = 1.1   # exit only when gross > 1.1x fees; guards against σ-expansion false exits

    # Exit immediately when unrealized gross loss exceeds this amount.
    # Catches slow drifts where the rolling z-score mean adapts to the trend
    # (z stays "normal" while dollar loss grows). Complements z-based stop_loss.
    # 0.0 = disabled. At $1000 notional, $3.0 = 0.3% adverse move on HYPE leg.
    # HYPE/ETH loss 2026-05-23: long HYPE, HYPE fell -0.43%, gross=-$4.27 despite
    # z never exceeding 2.2 — rolling mean tracked the drift, z-stop never fired.
    max_adverse_gross_usd: float = 3.0

    # Number of recent hl estimates used to compute the hl trend slope.
    # is_spread_trending() fires when slope > 0.03 bars/tick over this window.
    hl_trend_lookback: int = 20

    # Beta drift filter: the Kalman hedge ratio drifts upward when asset_a is
    # systematically rising vs asset_b (pump regime). Block entries in the trending
    # direction when beta_now - beta_{N bars ago} exceeds this threshold.
    # 300 bars = 25 min window. Threshold 2.5e-4: ~5σ above HYPE/ETH noise floor
    # (ETH co-moves with HYPE so Kalman beta noise is larger than HYPE/SOL ~11σ).
    # Lowered from 3e-4 on 2026-05-23: Trade4 loss -$7.84 had drift=3e-4 exactly
    # at old threshold (strict > missed it); new threshold catches this regime.
    beta_drift_window: int = 300
    beta_drift_threshold: float = 0.00025

    # Price momentum filter: block entries when price_a has moved more than this
    # fraction in the opposite direction to the proposed trade, measured over the
    # last momentum_lookback_bars bars. Prevents fading strong momentum (e.g., HYPE
    # pumping +10% over 7h triggered repeated SHORT HYPE stop-losses).
    # 60 bars × 5s = 5-minute window. 1.0% ≈ 2× typical 5-min HYPE vol (~0.5-0.6%).
    # Prior pump stop-losses had 0.3-1.3% 5-min momentum — 1.5% missed them, 1.0% catches
    # the acceleration windows that triggered entries while HYPE was trending up.
    momentum_lookback_bars: int = 60
    momentum_threshold: float = 0.010  # 1.0%

    # Require this many consecutive bars with |z| >= stop_z before triggering stop-loss.
    # 1 = trigger immediately (original behavior). 2 = skip single-bar spikes that self-revert.
    # Costs one extra bar of loss on genuine trend breaks but prevents whipsaw stops on HYPE spikes.
    stop_z_confirm_bars: int = 2

    # Require this many consecutive bars with |z| >= entry_z before entering a trade.
    # 1 = enter immediately (old behavior). 2 = skip 1-bar spikes (z jumps from <2.3 to >2.3
    # in one tick and reverts next bar — these have no time to confirm mean-reversion intent).
    # HYPE/ETH loss at 22:10:28: z=1.14→3.17 in one bar, then drifted → time_stop -$0.84.
    entry_confirm_bars: int = 2

    # Max slippage tolerated on a market order before we treat it as failed
    order_slippage: float = 0.01  # 1%

    # Main loop poll interval in seconds
    poll_interval_seconds: float = 5.0

    # Paper mode — set PAPER_MODE=false in .env to trade live
    paper_mode: bool = field(
        default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() != "false"
    )
