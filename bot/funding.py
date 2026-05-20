import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class FundingRateChecker:
    """
    Fetches current 8-hour funding rates from Hyperliquid and evaluates
    whether the proposed position has acceptable net funding.

    Hyperliquid funding sign convention:
      - Positive rate: longs pay shorts (rate > 0 is costly for longs)
      - Negative rate: shorts pay longs (rate < 0 is costly for shorts)

    For a Long-A / Short-B position:
      net_cost = rate_A - rate_B
        rate_A > 0  → we PAY on our long
        rate_B > 0  → we RECEIVE on our short (so it subtracts from cost)

    We reject entry if net_cost > max_net_cost (we'd be net payers above threshold).
    """

    _CACHE_TTL_SECONDS: float = 30.0

    def __init__(self, info, max_net_cost: float = 0.0001):
        self._info = info
        self._max_net_cost = max_net_cost
        self._rates: dict[str, float] = {}
        self._fetched_at: float = 0.0

    # ------------------------------------------------------------------
    # Internal fetch
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        if time.monotonic() - self._fetched_at < self._CACHE_TTL_SECONDS:
            return

        loop = asyncio.get_event_loop()
        try:
            universe, contexts = await loop.run_in_executor(
                None, self._info.meta_and_asset_ctxs
            )
        except Exception as exc:
            logger.warning("Funding rate fetch failed: %s", exc)
            return

        rates: dict[str, float] = {}
        for asset_meta, ctx in zip(universe["universe"], contexts):
            coin = asset_meta["name"]
            try:
                rates[coin] = float(ctx.get("funding", 0.0))
            except (TypeError, ValueError):
                rates[coin] = 0.0

        self._rates = rates
        self._fetched_at = time.monotonic()
        logger.debug("Funding rates refreshed for %d assets", len(rates))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_rate(self, coin: str) -> float:
        await self._refresh()
        return self._rates.get(coin, 0.0)

    async def evaluate(
        self,
        asset_a: str,
        asset_b: str,
        long_a: bool,
    ) -> tuple[bool, float]:
        """
        Returns (entry_acceptable, net_8h_cost).

        long_a=True  → Long A / Short B
        long_a=False → Long B / Short A
        """
        await self._refresh()
        rate_a = self._rates.get(asset_a, 0.0)
        rate_b = self._rates.get(asset_b, 0.0)

        if long_a:
            net_cost = rate_a - rate_b    # pay on A, receive on B
        else:
            net_cost = rate_b - rate_a    # pay on B, receive on A

        acceptable = net_cost <= self._max_net_cost

        logger.info(
            "Funding check | %s/%s long_a=%s | rate_a=%.6f rate_b=%.6f "
            "net_cost=%.6f max=%.6f -> %s",
            asset_a, asset_b, long_a, rate_a, rate_b,
            net_cost, self._max_net_cost,
            "OK" if acceptable else "SKIP",
        )
        return acceptable, net_cost
