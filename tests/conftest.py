from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from make_fixtures import make_samples  # noqa: E402

from rvtools_hw_check.auth import TOKEN_URL  # noqa: E402
from rvtools_hw_check.catalog import SEARCH_URL  # noqa: E402
from rvtools_hw_check.ratelimit import PoliteSession  # noqa: E402
from rvtools_hw_check.rvtools import Normalizer  # noqa: E402

CATALOG_FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
DETAIL_RE = re.compile(r"https://catalog\.redhat\.com/en/hardware/system/detail/(\d+)")
ACCESS_TOKEN = "test-access-token"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def search_callback(request):
    query = parse_qs(urlparse(request.url).query)["q"][0]
    path = CATALOG_FIXTURES / f"search_{slug(query)}.json"
    body = path.read_text() if path.exists() else '{"response": {"numFound": 0, "docs": []}}'
    return 200, {"Content-Type": "application/json"}, body


def detail_callback(request):
    listing_id = DETAIL_RE.match(request.url).group(1)
    path = CATALOG_FIXTURES / f"detail_{listing_id}.html"
    if not path.exists():
        return 404, {}, "not found"
    return 200, {"Content-Type": "text/html"}, path.read_text()


def register_catalog(rsps: responses.RequestsMock) -> None:
    rsps.add(responses.POST, TOKEN_URL, json={"access_token": ACCESS_TOKEN, "expires_in": 900})
    rsps.add_callback(responses.GET, SEARCH_URL, callback=search_callback)
    rsps.add_callback(responses.GET, DETAIL_RE, callback=detail_callback)


@pytest.fixture
def catalog_api():
    """Mocked Red Hat SSO and catalog backed by recorded responses. No live calls."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        register_catalog(rsps)
        yield rsps


@pytest.fixture
def sample_files(tmp_path) -> tuple[Path, Path]:
    return make_samples(tmp_path / "input")


@pytest.fixture
def normalizer() -> Normalizer:
    return Normalizer.from_file()


class FakeClock:
    """Deterministic clock; sleep() advances time instead of blocking."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fast_http(clock) -> PoliteSession:
    return PoliteSession(rate=5, clock=clock, sleep=clock.sleep, rng=lambda: 0.0)
