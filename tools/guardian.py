#!/usr/bin/env python3
"""
StatArb Guardian — autonomous 24x7 watchdog for the statarb systemd service.

Runs as statarb-guardian.service on the VPS. Every CHECK_INTERVAL seconds:
  1. Parses recent EXIT trades from journalctl
  2. Applies circuit breakers (consecutive losses, cumulative loss rate)
  3. Stops the statarb service + enters cooldown if triggered
  4. Auto-restarts after cooldown

Designed to run completely independently of Claude — true 24x7 operation.
"""

import logging
import re
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s guardian — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/statarb-bot/guardian.log"),
    ],
)
logger = logging.getLogger("guardian")

# --- Thresholds ---
# Stop if the last N trades have cumulative net below this value.
ROLLING_WINDOW = 5
ROLLING_LOSS_LIMIT = -3.00       # $-3 across last 5 trades
HARD_STOP_WINDOW = 10
HARD_STOP_LIMIT = -6.00          # $-6 across last 10 trades — serious regime failure

# How long to pause after tripping a circuit breaker.
SOFT_COOLDOWN = 1800             # 30 min for rolling circuit
HARD_COOLDOWN = 3600             # 1 hr for hard circuit

CHECK_INTERVAL = 300             # check every 5 minutes


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def is_service_active(name: str) -> bool:
    return run(["systemctl", "is-active", name]).strip() == "active"


def stop_service(reason: str) -> None:
    logger.warning("STOPPING statarb | %s", reason)
    subprocess.run(["systemctl", "stop", "statarb"])


def start_service() -> None:
    logger.info("STARTING statarb after cooldown")
    subprocess.run(["systemctl", "start", "statarb"])


def get_recent_exits(n: int = 20, since: float = 0.0) -> list[float]:
    """Return net P&L of the last n EXIT trades from journalctl (most recent last).

    since: unix timestamp; if > 0, only trades logged after this time are counted.
    This prevents re-firing on stale pre-cooldown trades after a circuit resume.
    When since > 0, omit -n cap so all trades in the session are visible — the
    -n 500 line limit silently truncated active sessions to the last ~4 min.
    """
    if since > 0:
        cmd = ["journalctl", "-u", "statarb", "--no-pager",
               "--since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))]
    else:
        cmd = ["journalctl", "-u", "statarb", "--no-pager", "-n", "500"]
    raw = run(cmd)
    nets = []
    for line in raw.splitlines():
        if "EXIT (paper)" not in line:
            continue
        m = re.search(r"net=\$(-?[\d.]+)", line)
        if m:
            nets.append(float(m.group(1)))
    return nets[-n:] if len(nets) > n else nets


def get_current_cumulative_net() -> float | None:
    """Most recent cumulative_net value from logs."""
    raw = run(["journalctl", "-u", "statarb", "--no-pager", "-n", "100"])
    last_net = None
    for line in raw.splitlines():
        m = re.search(r"cumulative_net=\$(-?[\d.]+)", line)
        if m:
            last_net = float(m.group(1))
    return last_net


def main() -> None:
    logger.info("Guardian started | rolling_limit=$%.2f/%db hard_limit=$%.2f/%db",
                ROLLING_LOSS_LIMIT, ROLLING_WINDOW, HARD_STOP_LIMIT, HARD_STOP_WINDOW)

    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    session_start: float = time.time()  # only count trades after this timestamp

    while True:
        now = time.time()

        # --- Cooldown phase ---
        if now < cooldown_until:
            remaining = int(cooldown_until - now)
            logger.info("Cooldown active | %ds remaining | reason: %s", remaining, cooldown_reason)
            if is_service_active("statarb"):
                stop_service("still running during cooldown")
            time.sleep(min(CHECK_INTERVAL, remaining + 10))
            continue

        # --- Resume after cooldown ---
        if cooldown_until > 0 and not is_service_active("statarb"):
            start_service()
            cooldown_until = 0.0
            cooldown_reason = ""
            session_start = time.time()  # reset: only count trades from this new session
            time.sleep(CHECK_INTERVAL)
            continue

        # --- Normal check ---
        if not is_service_active("statarb"):
            logger.warning("statarb is not running (not in cooldown) — skipping check")
            time.sleep(CHECK_INTERVAL)
            continue

        exits = get_recent_exits(max(ROLLING_WINDOW, HARD_STOP_WINDOW), since=session_start)
        cum_net = get_current_cumulative_net()

        if len(exits) < ROLLING_WINDOW:
            logger.info("Not enough trades yet (%d), skipping circuit check", len(exits))
            time.sleep(CHECK_INTERVAL)
            continue

        rolling_net = sum(exits[-ROLLING_WINDOW:])
        hard_net = sum(exits[-HARD_STOP_WINDOW:]) if len(exits) >= HARD_STOP_WINDOW else None

        logger.info(
            "Health | trades=%d rolling%d=$%.4f hard%d=%s cumulative=%s",
            len(exits),
            ROLLING_WINDOW, rolling_net,
            HARD_STOP_WINDOW, f"${hard_net:.4f}" if hard_net is not None else "N/A",
            f"${cum_net:.4f}" if cum_net is not None else "N/A",
        )

        if hard_net is not None and hard_net < HARD_STOP_LIMIT:
            reason = f"hard circuit: last {HARD_STOP_WINDOW} trades net ${hard_net:.4f} < ${HARD_STOP_LIMIT}"
            stop_service(reason)
            cooldown_until = now + HARD_COOLDOWN
            cooldown_reason = reason
        elif rolling_net < ROLLING_LOSS_LIMIT:
            reason = f"soft circuit: last {ROLLING_WINDOW} trades net ${rolling_net:.4f} < ${ROLLING_LOSS_LIMIT}"
            stop_service(reason)
            cooldown_until = now + SOFT_COOLDOWN
            cooldown_reason = reason

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
