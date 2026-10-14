"""A polite HTTP client for the Red Hat catalog: rate limit, backoff, circuit breaker (spec FR-4.8)."""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from typing import Callable

import requests

from . import __version__

logger = logging.getLogger(__name__)

DEFAULT_RATE = 2.0
MAX_RATE = 5.0
MAX_IN_FLIGHT = 2
MAX_RETRIES = 5
BREAKER_THRESHOLD = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
RETRY_AFTER_CAP = 300.0
USER_AGENT = f"rvtools-hw-check/{__version__}"


class ApiError(Exception):
    """A catalog request failed after retries, or returned a non-retryable error."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CircuitOpenError(ApiError):
    """Too many consecutive failures; no more requests will be sent this run."""


class TokenBucket:
    """Thread-safe token bucket. Capacity 1 means requests are evenly spaced at `rate` per second."""

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                self._sleep(wait)
                self._last = self._clock()
                self._tokens = 0.0
            else:
                self._tokens -= 1


class CircuitBreaker:
    def __init__(self, threshold: int = BREAKER_THRESHOLD):
        self.threshold = threshold
        self._failures = 0
        self._lock = threading.Lock()
        self.reason = ""

    @property
    def open(self) -> bool:
        return self._failures >= self.threshold

    def check(self) -> None:
        if self.open:
            raise CircuitOpenError(f"Stopped calling the Red Hat catalog after {self.threshold} consecutive failures ({self.reason})")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._failures += 1
            self.reason = reason
            if self._failures == self.threshold:
                logger.error("Circuit breaker opened: %s", reason)


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff (1 s, 2 s, 4 s, ... capped at 60 s) with up to 10% jitter."""
    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return min(BACKOFF_CAP, delay + delay * 0.1 * rng())


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class PoliteSession:
    """Wraps requests with a shared rate limit, in-flight cap, retries and a circuit breaker."""

    def __init__(
        self,
        rate: float = DEFAULT_RATE,
        max_in_flight: int = MAX_IN_FLIGHT,
        max_retries: int = MAX_RETRIES,
        timeout: float = 30,
        breaker_threshold: int = BREAKER_THRESHOLD,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ):
        if not 0 < rate <= MAX_RATE:
            raise ValueError(f"rate must be between 0 and {MAX_RATE} requests/second")
        self.bucket = TokenBucket(rate, clock=clock, sleep=sleep)
        self.breaker = CircuitBreaker(breaker_threshold)
        self.max_retries = max_retries
        self.timeout = timeout
        self._in_flight = threading.BoundedSemaphore(max_in_flight)
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._sleep = sleep
        self._rng = rng
        self.request_count = 0

    def get(self, url: str, params=None, token_manager=None) -> requests.Response:
        refreshed = False
        attempt = 0
        while True:
            self.breaker.check()
            self.bucket.acquire()
            headers = token_manager.auth_header() if token_manager is not None else {}
            try:
                with self._in_flight:
                    self.request_count += 1
                    response = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                reason = f"{exc.__class__.__name__} for {url}"
                self.breaker.record_failure(reason)
                attempt = self._retry_or_raise(attempt, reason, None)
                continue

            status = response.status_code
            if status == 401 and token_manager is not None and not refreshed:
                # Expired or revoked access token: refresh once and try again (FR-3.4).
                token_manager.invalidate()
                refreshed = True
                continue
            if status == 429 or status >= 500:
                reason = f"HTTP {status} from {url}"
                self.breaker.record_failure(reason)
                wait = parse_retry_after(response.headers.get("Retry-After")) if status in (429, 503) else None
                attempt = self._retry_or_raise(attempt, reason, wait, status)
                continue

            # The server answered; even a 4xx means it is healthy.
            self.breaker.record_success()
            if status >= 400:
                raise ApiError(f"HTTP {status} from {url}", status)
            return response

    def _retry_or_raise(self, attempt: int, reason: str, retry_after: float | None, status: int | None = None) -> int:
        if attempt >= self.max_retries:
            raise ApiError(f"{reason} (gave up after {self.max_retries} retries)", status)
        self.breaker.check()
        if retry_after is not None:
            delay = min(retry_after, RETRY_AFTER_CAP)
        else:
            delay = backoff_delay(attempt, self._rng)
        logger.warning("%s; retrying in %.1fs (retry %d/%d)", reason, delay, attempt + 1, self.max_retries)
        self._sleep(delay)
        return attempt + 1
