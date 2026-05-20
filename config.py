import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # Pair
    asset_a: str = field(default_factory=lambda: os.getenv("ASSET_A", "ETH"))
    asset_b: str = field(default_factory=lambda: os.getenv("ASSET_B", "BTC"))

    # Kalman filter parameters
    # delta controls how fast the hedge ratio can drift (higher = more reactive)
    # R is measurement noise (higher = smoother hedge ratio)
    kalman_delta: float = 1e-4
    kalman_R: float = 1e-2

    # Z-score thresholds
    entry_z: float = 2.5       # open trade
    exit_z: float = 0.5        # close trade on reversion (|z| < exit_z)
    stop_z: float = 4.0        # stop loss — spread blowing out

    # Rolling window for z-score normalization (in bars)
    spread_window: int = 100

    # Bars of history for Ornstein-Uhlenbeck half-life estimation
    halflife_lookback: int = 200

    # Close if trade age exceeds this multiple of the OU half-life
    time_stop_multiplier: float = 2.0

    # Notional USD per leg (leg B is beta-adjusted to match leg A notional)
    notional_usd: float = field(
        default_factory=lambda: float(os.getenv("NOTIONAL_USD", "1000"))
    )

    # Maximum acceptable net 8h funding rate for the combined position
    # Positive = we are net payers. Reject entry if net_rate > this threshold.
    max_net_funding_rate: float = 0.0001  # 0.01 % per 8 h

    # Max slippage tolerated on a market order before we treat it as failed
    order_slippage: float = 0.01  # 1%

    # Main loop poll interval in seconds
    poll_interval_seconds: float = 5.0

    # Paper mode — set PAPER_MODE=false in .env to trade live
    paper_mode: bool = field(
        default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() != "false"
    )
