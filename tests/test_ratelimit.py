import threading
import time

import pytest
import requests
import responses

from rvtools_hw_check.ratelimit import (
    BACKOFF_CAP,
    MAX_IN_FLIGHT,
    USER_AGENT,
    ApiError,
    CircuitOpenError,
    PoliteSession,
    TokenBucket,
    backoff_delay,
    parse_retry_after,
)

URL = "https://catalog.redhat.com/api/solr"


def make_session(clock, **kwargs):
    kwargs.setdefault("rate", 2)
    return PoliteSession(clock=clock, sleep=clock.sleep, rng=lambda: 0.0, **kwargs)


def test_token_bucket_spaces_requests(clock):
    bucket = TokenBucket(2, clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(6):
        bucket.acquire()
        stamps.append(clock.now)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(gap >= 0.5 - 1e-9 for gap in gaps)


@responses.activate
@pytest.mark.parametrize("rate", [1, 2, 5])
def test_request_rate_never_exceeds_limit(clock, rate):
    stamps = []

    def callback(_request):
        stamps.append(clock.now)
        return 200, {}, "{}"

    responses.add_callback(responses.GET, URL, callback=callback)
    session = make_session(clock, rate=rate)
    for _ in range(10):
        session.get(URL)
    assert len(stamps) == 10
    for a, b in zip(stamps, stamps[1:]):
        assert b - a >= 1 / rate - 1e-9
    # Over any window, requests/second stays within the limit.
    assert (len(stamps) - 1) / (stamps[-1] - stamps[0]) <= rate + 1e-9


@pytest.mark.parametrize("rate", [0, -1, 5.01, 10])
def test_rate_outside_allowed_range_is_rejected(rate):
    with pytest.raises(ValueError):
        PoliteSession(rate=rate)


@responses.activate
def test_sends_user_agent(clock):
    responses.add(responses.GET, URL, json={})
    make_session(clock).get(URL)
    assert responses.calls[0].request.headers["User-Agent"] == USER_AGENT


@responses.activate
def test_retry_after_is_honored_on_429(clock):
    responses.add(responses.GET, URL, status=429, headers={"Retry-After": "7"})
    responses.add(responses.GET, URL, json={"ok": True})
    session = make_session(clock)
    assert session.get(URL).json() == {"ok": True}
    assert 7 in clock.sleeps
    assert len(responses.calls) == 2


@responses.activate
def test_retry_after_is_honored_on_503(clock):
    responses.add(responses.GET, URL, status=503, headers={"Retry-After": "3"})
    responses.add(responses.GET, URL, json={})
    make_session(clock).get(URL)
    assert 3 in clock.sleeps


@responses.activate
def test_exponential_backoff_then_gives_up_after_retry_limit(clock):
    responses.add(responses.GET, URL, status=500)
    session = make_session(clock, breaker_threshold=100)
    with pytest.raises(ApiError, match="gave up after 5 retries"):
        session.get(URL)
    assert len(responses.calls) == 6  # 1 attempt + 5 retries
    backoffs = [s for s in clock.sleeps if s >= 1]
    assert backoffs == [1, 2, 4, 8, 16]


@responses.activate
def test_connection_errors_are_retried(clock):
    responses.add(responses.GET, URL, body=requests.ConnectionError("down"))
    responses.add(responses.GET, URL, json={})
    make_session(clock).get(URL)
    assert len(responses.calls) == 2


@responses.activate
def test_other_4xx_is_not_retried(clock):
    responses.add(responses.GET, URL, status=404)
    session = make_session(clock)
    with pytest.raises(ApiError) as err:
        session.get(URL)
    assert err.value.status == 404
    assert len(responses.calls) == 1
    assert not session.breaker.open


@responses.activate
def test_circuit_breaker_opens_after_five_consecutive_failures(clock):
    responses.add(responses.GET, URL, status=503)
    session = make_session(clock)
    with pytest.raises(CircuitOpenError):
        session.get(URL)
    assert len(responses.calls) == 5
    assert session.breaker.open
    # No further requests reach the API once the breaker is open.
    with pytest.raises(CircuitOpenError):
        session.get(URL)
    assert len(responses.calls) == 5


@responses.activate
def test_success_resets_failure_count(clock):
    for _ in range(3):
        responses.add(responses.GET, URL, status=500)
        responses.add(responses.GET, URL, json={})
    session = make_session(clock)
    for _ in range(3):
        session.get(URL)
    assert not session.breaker.open


@responses.activate
def test_401_refreshes_token_once(clock):
    class Tokens:
        def __init__(self):
            self.tokens = iter(["old", "new"])
            self.current = next(self.tokens)
            self.invalidated = 0

        def auth_header(self):
            return {"Authorization": f"Bearer {self.current}"}

        def invalidate(self):
            self.invalidated += 1
            self.current = next(self.tokens)

    responses.add(responses.GET, URL, status=401)
    responses.add(responses.GET, URL, json={})
    tokens = Tokens()
    make_session(clock).get(URL, token_manager=tokens)
    assert tokens.invalidated == 1
    assert [c.request.headers["Authorization"] for c in responses.calls] == ["Bearer old", "Bearer new"]


def test_backoff_delay_is_capped_with_jitter():
    assert backoff_delay(0, rng=lambda: 0.0) == 1
    assert backoff_delay(3, rng=lambda: 0.0) == 8
    assert backoff_delay(3, rng=lambda: 1.0) == pytest.approx(8.8)
    assert backoff_delay(10, rng=lambda: 1.0) == BACKOFF_CAP


def test_parse_retry_after():
    assert parse_retry_after("12") == 12
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


@responses.activate
def test_at_most_two_requests_in_flight():
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def callback(_request):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.5)
        with lock:
            state["current"] -= 1
        return 200, {}, "{}"

    responses.add_callback(responses.GET, URL, callback=callback)
    session = PoliteSession(rate=5)
    threads = [threading.Thread(target=session.get, args=(URL,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert MAX_IN_FLIGHT == 2
    assert state["peak"] == 2
