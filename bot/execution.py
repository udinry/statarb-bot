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

    def __init__(self, client: HyperliquidClient, config: TradingConfig):
        self._client = client
        self._cfg = config
        self.position: Optional[Position] = None
        self._paper_pnl: float = 0.0      # cumulative simulated P&L in paper mode

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
        sz_a = round(self._cfg.notional_usd / price_a, 4)
        sz_b = round((beta * sz_a * price_a) / price_b, 4)
        return max(sz_a, 0.0001), max(sz_b, 0.0001)

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

        if self._cfg.paper_mode:
            self.position = Position(
                side=side, sz_a=sz_a, sz_b=sz_b, beta=beta,
                entry_spread=spread, entry_z=z, half_life_bars=half_life,
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

    async def exit(self, reason: str, current_spread: Optional[float] = None) -> bool:
        if self.position is None:
            return False

        pos = self.position

        if self._cfg.paper_mode and current_spread is not None:
            if pos.side == Side.LONG_A_SHORT_B:
                # Profit when spread rises: exit_spread - entry_spread
                pnl = (current_spread - pos.entry_spread) * (
                    self._cfg.notional_usd / pos.entry_spread
                    if pos.entry_spread != 0 else 0
                )
            else:
                pnl = (pos.entry_spread - current_spread) * (
                    self._cfg.notional_usd / pos.entry_spread
                    if pos.entry_spread != 0 else 0
                )
            self._paper_pnl += pnl
            logger.info(
                "EXIT (paper) | reason=%s pnl=%.4f cumulative_pnl=%.4f",
                reason, pnl, self._paper_pnl,
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

    def should_exit_reversion(self, z: float) -> bool:
        """True when the spread has reverted enough to take profit."""
        if self.position is None:
            return False
        if self.position.side == Side.LONG_A_SHORT_B:
            # Entered at z << 0; exit when z climbs back above -exit_z
            return z >= -self._cfg.exit_z
        else:
            # Entered at z >> 0; exit when z falls back below +exit_z
            return z <= self._cfg.exit_z

    def should_stop_loss(self, z: float) -> bool:
        """True when z has blown out far enough to signal cointegration break."""
        if self.position is None:
            return False
        return abs(z) >= self._cfg.stop_z

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
