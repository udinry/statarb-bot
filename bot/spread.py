from collections import deque

import numpy as np


class SpreadAnalyzer:
    """
    Tracks spread history and provides:
      - Rolling z-score for entry/exit signals
      - Ornstein-Uhlenbeck half-life for time-stop calibration

    Both quantities are computed over the most recent N observations.
    The deque is bounded to max(spread_window, halflife_lookback) so we
    hold only what we actually need.
    """

    def __init__(self, window: int = 100, halflife_lookback: int = 200):
        self.window = window
        self.halflife_lookback = halflife_lookback
        self._buf: deque[float] = deque(maxlen=max(window, halflife_lookback))

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def push(self, spread: float) -> None:
        self._buf.append(spread)

    def __len__(self) -> int:
        return len(self._buf)

    # ------------------------------------------------------------------
    # Z-score
    # ------------------------------------------------------------------

    def z_score(self) -> float | None:
        """
        Z-score of the most recent spread observation relative to the
        rolling mean and std of the last `window` observations.

        Returns None during the warm-up period.
        """
        if len(self._buf) < self.window:
            return None

        arr = np.array(list(self._buf)[-self.window:], dtype=float)
        mu = arr.mean()
        sigma = arr.std(ddof=1)

        if sigma < 1e-12:
            return None

        return float((self._buf[-1] - mu) / sigma)

    # ------------------------------------------------------------------
    # Ornstein-Uhlenbeck half-life
    # ------------------------------------------------------------------

    def half_life(self) -> float | None:
        """
        Estimate the mean-reversion half-life via a discrete OU regression.

        Model:  ΔS_t = α + β * S_{t-1} + ε
        The speed of mean reversion is θ = -β  (β must be negative for OU).
        Half-life = ln(2) / θ  =  -ln(2) / β

        Returns None if the spread is not mean-reverting (β ≥ 0) or if
        insufficient data is available.
        """
        if len(self._buf) < self.halflife_lookback:
            return None

        arr = np.array(list(self._buf)[-self.halflife_lookback:], dtype=float)
        S_lag = arr[:-1]                  # S_{t-1}
        dS = np.diff(arr)                 # ΔS_t

        # OLS: dS = α + β * S_lag
        X = np.column_stack([np.ones(len(S_lag)), S_lag])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, dS, rcond=None)
        except np.linalg.LinAlgError:
            return None

        beta = coeffs[1]
        if beta >= 0.0:
            # Spread is trending, not mean-reverting — skip trade
            return None

        hl = -np.log(2.0) / beta
        return float(hl)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def current_spread(self) -> float | None:
        return self._buf[-1] if self._buf else None

    @property
    def warmed_up(self) -> bool:
        return len(self._buf) >= self.window
