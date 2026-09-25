"""
rate_limiter.py
---------------
Thread-safe token-bucket rate limiter for Gemini API calls.

Usage:
    limiter = RateLimiter(calls_per_minute=15)
    limiter.acquire()          # blocks until a token is available
    # ... make your API call ...

Configure via .env:
    GEMINI_RPM=15              # requests per minute ceiling
"""
from __future__ import annotations

import os
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token-bucket rate limiter. Thread-safe.

    - `calls_per_minute` tokens are replenished every 60 seconds.
    - `acquire(block=True)` waits until a token is available, then consumes one.
    - `acquire(block=False)` returns False immediately if no token is available.

    Example:
        limiter = RateLimiter(calls_per_minute=15)
        if limiter.acquire():
            response = gemini.generate_content(prompt)
    """

    def __init__(self, calls_per_minute: Optional[int] = None):
        self._rpm        = calls_per_minute or int(os.getenv("GEMINI_RPM", "15"))
        self._tokens     = float(self._rpm)               # start full
        self._max_tokens = float(self._rpm)
        self._lock       = threading.Lock()
        self._last_refill = time.monotonic()
        logger.info(f"[RATE LIMITER] Initialised: {self._rpm} calls/min ceiling.")

    # ── Public API ────────────────────────────────────────────────────────────

    def acquire(self, block: bool = True) -> bool:
        """
        Consume one token.

        If block=True (default): sleeps until a token is available, returns True.
        If block=False: returns True if token was available, False otherwise.
        """
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            if not block:
                return False

            # Calculate wait time and sleep outside the lock
            wait_secs = self._wait_seconds_needed()
            logger.info(f"[RATE LIMITER] Waiting {wait_secs:.1f}s before next Gemini call...")
            time.sleep(max(0.1, wait_secs))

    def try_acquire(self) -> bool:
        """Non-blocking convenience alias for acquire(block=False)."""
        return self.acquire(block=False)

    @property
    def calls_per_minute(self) -> int:
        return self._rpm

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refill(self) -> None:
        """Refill tokens based on elapsed time. Must be called inside the lock."""
        now     = time.monotonic()
        elapsed = now - self._last_refill
        # Add tokens proportional to elapsed time
        new_tokens = elapsed * (self._rpm / 60.0)
        self._tokens     = min(self._max_tokens, self._tokens + new_tokens)
        self._last_refill = now

    def _wait_seconds_needed(self) -> float:
        """Estimate how long to wait for 1 token to become available."""
        with self._lock:
            self._refill()
            deficit = 1.0 - self._tokens
            if deficit <= 0:
                return 0.0
            # Time for `deficit` tokens at rate RPM/60
            return deficit / (self._rpm / 60.0)

    def __repr__(self) -> str:
        return f"RateLimiter(rpm={self._rpm}, tokens={self._tokens:.1f})"
