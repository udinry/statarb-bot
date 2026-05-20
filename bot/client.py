import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Thin async wrapper around the blocking Hyperliquid Python SDK.

    All SDK calls are blocking HTTP requests; we push them into the
    default ThreadPoolExecutor via run_in_executor so the main asyncio
    loop never stalls.
    """

    def __init__(self, address: str, exchange, info):
        self._address = address
        self._exchange = exchange
        self._info = info

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_mid_prices(self) -> dict[str, float]:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._info.all_mids)
        return {k: float(v) for k, v in raw.items()}

    async def get_user_state(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._info.user_state(self._address)
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    async def market_open(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        slippage: float = 0.01,
    ) -> dict[str, Any]:
        """
        Open a position with a market order.

        The SDK's market_open sends a limit order priced at (1 ± slippage)
        relative to the current mid, which behaves as a market order with
        a guaranteed fill price band.
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._exchange.market_open(coin, is_buy, sz, None, slippage),
        )
        logger.debug("market_open %s is_buy=%s sz=%s -> %s", coin, is_buy, sz, result)
        return result

    async def market_close(
        self,
        coin: str,
        slippage: float = 0.01,
    ) -> dict[str, Any]:
        """
        Close the full open position for coin.

        Passing sz=None tells the SDK to close the entire position, which
        is safer than tracking exact sizes ourselves when partial fills
        are possible.
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._exchange.market_close(coin, None, None, slippage),
        )
        logger.debug("market_close %s -> %s", coin, result)
        return result

    # ------------------------------------------------------------------
    # Result introspection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def order_ok(result: dict) -> bool:
        return result.get("status") == "ok"

    @staticmethod
    def filled_size(result: dict) -> float:
        """Extract total filled size from an SDK order response."""
        try:
            statuses = result["response"]["data"]["statuses"]
            return float(statuses[0]["filled"]["totalSz"])
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0
