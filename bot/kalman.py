import numpy as np


class KalmanHedgeRatio:
    """
    Online Kalman filter for estimating a time-varying hedge ratio beta.

    State-space model (scalar):
        Observation:  price_a[t] = beta[t] * price_b[t] + v[t]
        State:        beta[t]    = beta[t-1]             + w[t]

    w ~ N(0, Q)  — process noise (hedge ratio drift)
    v ~ N(0, R)  — measurement noise

    Q = delta / (1 - delta)  controls how fast beta can adapt.
    Small delta → slow adaptation (use for stable cointegrated pairs).
    Large delta → fast adaptation (reacts to regime shifts sooner but is noisier).

    All maths done in scalar form; no matrix library needed.
    """

    def __init__(self, delta: float = 1e-4, R: float = 1e-2):
        if not (0 < delta < 1):
            raise ValueError("delta must be in (0, 1)")
        self.Q = delta / (1.0 - delta)
        self.R = R

        self._beta: float = 0.0
        self._P: float = 1.0          # error variance
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, price_a: float, price_b: float) -> tuple[float, float]:
        """
        Ingest one new observation and return the updated (beta, spread).

        spread = price_a - beta * price_b
        A positive spread means price_a is rich relative to price_b.
        """
        if not self._initialized:
            self._beta = price_a / price_b if price_b != 0 else 1.0
            # Initialize P to the steady-state value rather than 1.0.
            # P=1.0 causes K*price_b ≈ 1 on the first real tick, absorbing
            # ~100% of the spread and resetting beta to the instantaneous ratio.
            # P_ss = Q*R / (Q*price_b^2 + R) keeps initial absorption at
            # the same level as all subsequent ticks (~31% for log prices).
            self._P = self.Q * self.R / (self.Q * price_b ** 2 + self.R)
            self._initialized = True
            return self._beta, price_a - self._beta * price_b

        # --- Prediction step -------------------------------------------
        P_pred = self._P + self.Q                          # propagate variance

        # --- Update step -----------------------------------------------
        # Innovation: how far is price_a from the model's prediction?
        innovation = price_a - self._beta * price_b

        # Innovation variance
        S = price_b ** 2 * P_pred + self.R

        # Kalman gain: weight given to the new measurement
        K = P_pred * price_b / S

        # Posterior state estimate
        self._beta = self._beta + K * innovation

        # Posterior error variance (Joseph form for numerical stability)
        self._P = (1.0 - K * price_b) * P_pred

        spread = price_a - self._beta * price_b
        return self._beta, spread

    @property
    def beta(self) -> float:
        return self._beta

    @property
    def error_variance(self) -> float:
        return self._P

    @property
    def initialized(self) -> bool:
        return self._initialized
