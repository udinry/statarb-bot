import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .client import HyperliquidClient
from config import TradingConfig

logger = logging.getLogger(__name__)


class Side(Enum):
    LONG_A_SHORT_B = "long_a_short_b"   # entered when z < -entry_z (spread cheap)
    LONG_B_SHORT_A = "long_b_short_a"   # entered when z > +entry_z (spread rich)


@dataclass
class Position:
    side: Side
    sz_a: float
    sz_b: float
    beta: float
    entry_spread: float
    entry_z: float
    half_life_bars: Optional[float]
    entry_price_a: float = 0.0
    entry_price_b: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def age_bars(self, poll_interval_seconds: float) -> float:
        elapsed = (datetime.now(timezone.utc) - self.opened_at).total_seconds()
        return elapsed / poll_interval_seconds


class ExecutionEngine:
    """
    Manages the full lifecycle of a pairs trade:
      1. Dual-leg simultaneous entry (asyncio.gather)
      2. Leg-risk unwind if one leg fails to fill
      3. Exit on z-score reversion, stop-loss, or OU time-stop
      4. Paper mode for dry runs (all logic active, no real orders sent)
    """

    def __init__(self, client: HyperliquidClient, config: TradingConfig,
                 sz_decimals_a: int = 4, sz_decimals_b: int = 4):
        self._client = client
        self._cfg = config
        self._sz_decimals_a = sz_decimals_a
        self._sz_decimals_b = sz_decimals_b
        self.position: Optional[Position] = None
        self._paper_gross_pnl: float = 0.0  # cumulative gross (before fees) — validates strategy
        self._paper_pnl: float = 0.0        # cumulative net (after fees) — reflects live reality
        self._stop_consecutive_bars: int = 0  # tracks bars above stop_z for confirmation

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _compute_sizes(
        self, price_a: float, price_b: float, beta: float
    ) -> tuple[float, float]:
        """
        Notional-balanced sizing:
          sz_a in asset_a units = notional / price_a
          sz_b in asset_b units = (beta * sz_a * price_a) / price_b

        This makes the dollar value of leg B equal to beta * notional_a,
        which exactly hedges the Kalman-estimated exposure.
        """
        sz_a = round(self._cfg.notional_usd / price_a, self._sz_decimals_a)
        sz_b = round((beta * sz_a * price_a) / price_b, self._sz_decimals_b)
        min_a = 10 ** -self._sz_decimals_a
        min_b = 10 ** -self._sz_decimals_b
        return max(sz_a, min_a), max(sz_b, min_b)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def enter(
        self,
        z: float,
        spread: float,
        price_a: float,
        price_b: float,
        beta: float,
        half_life: Optional[float],
    ) -> bool:
        """
        Fire both legs concurrently. If one leg fills and the other fails,
        immediately market-close the filled leg to eliminate naked exposure.

        Returns True if both legs filled successfully.
        """
        if self.position is not None:
            return False

        # z < 0 → spread below mean → buy A (cheap), sell B (rich)
        long_a = z < 0.0
        side = Side.LONG_A_SHORT_B if long_a else Side.LONG_B_SHORT_A
        sz_a, sz_b = self._compute_sizes(price_a, price_b, beta)

        logger.info(
            "ENTRY | side=%s z=%.3f spread=%.6f beta=%.4f "
            "sz_a=%s sz_b=%s notional=$%.2f paper=%s",
            side.value, z, spread, beta, sz_a, sz_b,
            self._cfg.notional_usd, self._cfg.paper_mode,
        )

        self._stop_consecutive_bars = 0
        if self._cfg.paper_mode:
            self.position = Position(
                side=side, sz_a=sz_a, sz_b=sz_b, beta=beta,
                entry_spread=spread, entry_z=z, half_life_bars=half_life,
                entry_price_a=price_a, entry_price_b=price_b,
            )
            return True

        # ------ Live execution ------
        leg_a_task = asyncio.create_task(
            self._client.market_open(
                self._cfg.asset_a, long_a, sz_a, self._cfg.order_slippage
            )
        )
        leg_b_task = asyncio.create_task(
            self._client.market_open(
                self._cfg.asset_b, not long_a, sz_b, self._cfg.order_slippage
            )
        )

        results = await asyncio.gather(leg_a_task, leg_b_task, return_exceptions=True)
        res_a, res_b = results

        leg_a_ok = not isinstance(res_a, Exception) and HyperliquidClient.order_ok(res_a)
        leg_b_ok = not isinstance(res_b, Exception) and HyperliquidClient.order_ok(res_b)

        if leg_a_ok and leg_b_ok:
            self.position = Position(
                side=side, sz_a=sz_a, sz_b=sz_b, beta=beta,
                entry_spread=spread, entry_z=z, half_life_bars=half_life,
                entry_price_a=price_a, entry_price_b=price_b,
            )
            logger.info("ENTRY OK | both legs filled")
            return True

        # --- Leg risk: unwind whichever leg filled ---
        if leg_a_ok and not leg_b_ok:
            logger.error(
                "LEG RISK | leg_b failed (%s). Unwinding leg_a immediately.",
                res_b if isinstance(res_b, Exception) else res_b.get("status"),
            )
            await self._emergency_close(self._cfg.asset_a)

        elif leg_b_ok and not leg_a_ok:
            logger.error(
                "LEG RISK | leg_a failed (%s). Unwinding leg_b immediately.",
                res_a if isinstance(res_a, Exception) else res_a.get("status"),
            )
            await self._emergency_close(self._cfg.asset_b)

        else:
            logger.error("ENTRY FAILED | both legs failed. No exposure taken.")

        return False

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    async def exit(
        self,
        reason: str,
        price_a: Optional[float] = None,
        price_b: Optional[float] = None,
    ) -> bool:
        if self.position is None:
            return False

        pos = self.position

        if self._cfg.paper_mode and price_a is not None and price_b is not None:
            # Real dollar P&L using actual prices:
            #   Long A leg:  (exit - entry) * sz_a
            #   Short B leg: -(exit - entry) * sz_b
            if pos.side == Side.LONG_A_SHORT_B:
                pnl = (price_a - pos.entry_price_a) * pos.sz_a \
                    - (price_b - pos.entry_price_b) * pos.sz_b
            else:
                pnl = -(price_a - pos.entry_price_a) * pos.sz_a \
                    + (price_b - pos.entry_price_b) * pos.sz_b

            # Fee simulation: maker entry (post-only limit) + taker exit (market close).
            # Entry rebate reduces cost; exit is always taker (market order).
            entry_notional = pos.entry_price_a * pos.sz_a + pos.entry_price_b * pos.sz_b
            exit_notional = price_a * pos.sz_a + price_b * pos.sz_b
            fees = (
                -self._cfg.maker_rebate_rate * entry_notional   # rebate received at entry
                + self._cfg.taker_fee_rate * exit_notional      # fee paid at exit
            )
            net_pnl = pnl - fees
            self._paper_gross_pnl += pnl
            self._paper_pnl += net_pnl
            logger.info(
                "EXIT (paper) | reason=%s entry_a=%.4f exit_a=%.4f "
                "entry_b=%.4f exit_b=%.4f gross=$%.4f fees=$%.4f net=$%.4f "
                "cumulative_gross=$%.4f cumulative_net=$%.4f",
                reason,
                pos.entry_price_a, price_a,
                pos.entry_price_b, price_b,
                pnl, fees, net_pnl, self._paper_gross_pnl, self._paper_pnl,
            )
            self.position = None
            return True

        logger.info("EXIT | reason=%s", reason)

        close_a = asyncio.create_task(
            self._client.market_close(self._cfg.asset_a, self._cfg.order_slippage)
        )
        close_b = asyncio.create_task(
            self._client.market_close(self._cfg.asset_b, self._cfg.order_slippage)
        )

        results = await asyncio.gather(close_a, close_b, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("EXIT | close leg %d exception: %s", i + 1, r)
            elif not HyperliquidClient.order_ok(r):
                logger.error("EXIT | close leg %d failed: %s", i + 1, r)

        self.position = None
        logger.info("EXIT COMPLETE")
        return True

    # ------------------------------------------------------------------
    # Exit conditions
    # ------------------------------------------------------------------

    def compute_gross_pnl(self, price_a: float, price_b: float) -> float:
        """Real-time unrealized gross PnL for the open position."""
        if self.position is None:
            return 0.0
        pos = self.position
        if pos.side == Side.LONG_A_SHORT_B:
            return (price_a - pos.entry_price_a) * pos.sz_a \
                 - (price_b - pos.entry_price_b) * pos.sz_b
        else:
            return -(price_a - pos.entry_price_a) * pos.sz_a \
                  + (price_b - pos.entry_price_b) * pos.sz_b

    def estimate_round_trip_fees(self) -> float:
        """Estimated maker-in taker-out round-trip fees based on entry notional."""
        if self.position is None:
            return 0.0
        pos = self.position
        entry_notional = pos.entry_price_a * pos.sz_a + pos.entry_price_b * pos.sz_b
        return (-self._cfg.maker_rebate_rate + self._cfg.taker_fee_rate) * entry_notional

    def should_exit_reversion(self, z: float, price_a: float = 0.0, price_b: float = 0.0) -> bool:
        """
        True when the spread has reverted enough to take profit.

        If min_profit_factor > 0 and prices are provided, also requires that
        gross PnL > fees × min_profit_factor before allowing a reversion exit.
        This prevents exiting on tiny moves that don't cover fees.
        """
        if self.position is None:
            return False
        # Z-score threshold
        if self.position.side == Side.LONG_A_SHORT_B:
            z_ok = z >= -self._cfg.exit_z
        else:
            z_ok = z <= self._cfg.exit_z
        if not z_ok:
            return False

        # Minimum profit guard
        if self._cfg.min_profit_factor > 0.0 and price_a > 0.0 and price_b > 0.0:
            gross = self.compute_gross_pnl(price_a, price_b)
            fee_est = self.estimate_round_trip_fees()
            if gross < fee_est * self._cfg.min_profit_factor:
                return False

        return True

    def should_stop_loss(self, z: float) -> bool:
        """True when z has been above stop_z for stop_z_confirm_bars consecutive bars.
        Requires confirmation to avoid exiting on single-bar spikes that self-revert."""
        if self.position is None:
            return False
        if abs(z) >= self._cfg.stop_z:
            self._stop_consecutive_bars += 1
            return self._stop_consecutive_bars >= self._cfg.stop_z_confirm_bars
        self._stop_consecutive_bars = 0
        return False

    def should_time_stop(self, current_half_life: Optional[float]) -> bool:
        """
        True when the trade has been open past 2x its OU half-life.

        Uses the half-life at entry if available (more stable than the
        current estimate which may have shifted during the trade).
        """
        if self.position is None:
            return False
        hl = self.position.half_life_bars or current_half_life
        if hl is None or hl <= 0:
            return False
        age = self.position.age_bars(self._cfg.poll_interval_seconds)
        return age > self._cfg.time_stop_multiplier * hl

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emergency_close(self, coin: str) -> None:
        try:
            result = await self._client.market_close(coin, self._cfg.order_slippage)
            if HyperliquidClient.order_ok(result):
                logger.info("Emergency close OK | coin=%s", coin)
            else:
                logger.critical(
                    "EMERGENCY CLOSE FAILED | coin=%s result=%s — CHECK EXCHANGE MANUALLY",
                    coin, result,
                )
        except Exception as exc:
            logger.critical(
                "EMERGENCY CLOSE EXCEPTION | coin=%s error=%s — CHECK EXCHANGE MANUALLY",
                coin, exc,
            )
