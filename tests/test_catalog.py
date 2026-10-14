import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from conftest import ACCESS_TOKEN, CATALOG_FIXTURES, DETAIL_RE, detail_callback, search_callback

from rvtools_hw_check.auth import TokenManager
from rvtools_hw_check.cache import Cache
from rvtools_hw_check.catalog import (
    EXACT,
    NO,
    NONE,
    PARTIAL,
    SEARCH_URL,
    UNKNOWN,
    YES,
    CatalogClient,
    match_listings,
    parse_detail_page,
)
from rvtools_hw_check.ratelimit import MAX_IN_FLIGHT, PoliteSession
from rvtools_hw_check.rvtools import Host, Platform

DETAIL_LINK_RE = re.compile(r"^https://catalog\.redhat\.com/hardware/system/detail/\d+$")


def platform(vendor, model, raw_vendor=None, raw_model=None) -> Platform:
    host = Host("a.xlsx", "esx1", vendor, model, raw_vendor or vendor, raw_model or model)
    return Platform(vendor, model, [host])


@pytest.fixture
def client(catalog_api, fast_http, normalizer, tmp_path):
    return CatalogClient(fast_http, normalizer, cache=Cache(tmp_path / "cache"), token_manager=TokenManager("offline"))


def search_calls(rsps):
    return [c for c in rsps.calls if c.request.url.startswith(SEARCH_URL)]


def test_search_request_is_filtered_and_authenticated(client, catalog_api):
    client.search("Dell PowerEdge R650")
    call = search_calls(catalog_api)[0]
    params = parse_qs(urlparse(call.request.url).query)
    assert params["q"] == ["Dell PowerEdge R650"]
    assert params["fq"] == ["documentKind:CertifiedHardware", "certified_category:System"]
    assert params["rows"] == ["100"]
    assert "id" in params["fl"][0]
    assert call.request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize(
    "vendor, model, fixture, expected_ids",
    [
        ("Dell", "PowerEdge R650", "dell-poweredge-r650", ["81185"]),
        ("HPE", "ProLiant DL380 Gen10", "hpe-proliant-dl380-gen10", ["6590"]),
        ("Lenovo", "ThinkSystem SR650", "lenovo-thinksystem-sr650", ["6526"]),
        ("Cisco", "UCS C220 M5SX", "cisco-ucs-c220-m5sx", ["294347"]),
    ],
)
def test_exact_matches_against_recorded_search(normalizer, vendor, model, fixture, expected_ids):
    import json

    docs = json.loads((CATALOG_FIXTURES / f"search_{fixture}.json").read_text())["response"]["docs"]
    confidence, matched = match_listings(platform(vendor, model), docs, normalizer)
    assert confidence == EXACT
    assert [d["id"] for d in matched] == expected_ids


def test_partial_match_lists_candidates(normalizer):
    docs = [
        {"id": "1", "partnerName": "Hewlett Packard Enterprise", "partnerProductName": "HPE ProLiant DL380 Gen10", "make": "HPE ProLiant", "model": "DL380 Gen10"},
        {"id": "2", "partnerName": "Hewlett Packard Enterprise", "partnerProductName": "HPE ProLiant DL380 Gen10 Plus", "make": "HPE ProLiant", "model": "DL380 Gen10 Plus"},
        {"id": "3", "partnerName": "Dell Technologies", "partnerProductName": "Dell PowerEdge R650", "make": "Dell PowerEdge", "model": "R650"},
    ]
    confidence, matched = match_listings(platform("HPE", "ProLiant DL380 Gen10 Plus v2"), docs, normalizer)
    assert confidence == PARTIAL
    assert [d["id"] for d in matched][:1] == ["2"]
    assert "3" not in [d["id"] for d in matched]


def test_no_match_for_other_vendor(normalizer):
    docs = [{"id": "3", "partnerName": "Dell Technologies", "partnerProductName": "Dell PowerEdge R650", "make": "Dell PowerEdge", "model": "R650"}]
    assert match_listings(platform("HPE", "PowerEdge R650"), docs, normalizer) == (NONE, [])


def test_parse_detail_page_reads_ocp_certifications():
    certs = parse_detail_page((CATALOG_FIXTURES / "detail_81185.html").read_text(), "81185")
    ocp = [c for c in certs if c.product == "Red Hat OpenShift Container Platform"]
    assert [c.version_range for c in ocp] == ["4.8-4.12", "4.13-4.x"]
    assert all(c.vm_host for c in ocp)


def test_parse_detail_page_handles_unrelated_html():
    assert parse_detail_page("<html><body>nothing here</body></html>", "1") is None


def test_lookup_certified_platform_with_versions_and_links(client):
    result = client.lookup(platform("Dell", "PowerEdge R650"))
    assert result.match_confidence == EXACT
    assert result.ocp_certified == YES
    assert result.ocp_versions == "4.8-4.12, 4.13-4.x"
    assert result.ocp_virt_certified == YES
    assert result.ocp_virt_versions == "4.8-4.12, 4.13-4.x"
    assert not result.versions_are_major_only
    assert result.catalog_urls == ["https://catalog.redhat.com/hardware/system/detail/81185"]


@pytest.mark.parametrize(
    "vendor, model",
    [("Dell", "PowerEdge R650"), ("HPE", "ProLiant DL380 Gen10"), ("Lenovo", "ThinkSystem SR650"), ("Cisco", "UCS C220 M5SX")],
)
def test_every_matched_platform_has_a_detail_link(client, vendor, model):
    result = client.lookup(platform(vendor, model))
    assert result.listings
    assert all(DETAIL_LINK_RE.match(url) for url in result.catalog_urls)


def test_unmatched_platform_gets_search_link_and_no_status(client, catalog_api):
    result = client.lookup(platform("Acme Corp", "Widget 9000"))
    assert result.match_confidence == NONE
    assert result.ocp_certified == NO
    assert result.ocp_virt_certified == NO
    [url] = result.catalog_urls
    assert url.startswith("https://catalog.redhat.com/search?")
    assert parse_qs(urlparse(url).query)["q"] == ["Acme Corp Widget 9000"]
    # vendor+model query first, then model alone
    queries = [parse_qs(urlparse(c.request.url).query)["q"][0] for c in search_calls(catalog_api)]
    assert queries == ["Acme Corp Widget 9000", "Widget 9000"]


def test_api_failure_is_unknown_not_no(normalizer, fast_http, tmp_path):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, SEARCH_URL, status=500)
        client = CatalogClient(fast_http, normalizer, cache=Cache(tmp_path))
        result = client.lookup(platform("Dell", "PowerEdge R650"))
    assert result.ocp_certified == UNKNOWN
    assert result.ocp_virt_certified == UNKNOWN
    assert result.error


def test_circuit_breaker_marks_remaining_platforms_unknown(normalizer, fast_http, tmp_path):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, SEARCH_URL, status=503)
        client = CatalogClient(fast_http, normalizer, cache=Cache(tmp_path))
        results = client.lookup_all([platform("Dell", "PowerEdge R650"), platform("HPE", "ProLiant DL380 Gen10"), platform("Cisco", "UCS C220 M5SX")])
        # Threshold is 5; with 2 workers one more request may already be in flight when it opens.
        assert 5 <= len(rsps.calls) <= 5 + MAX_IN_FLIGHT - 1
    assert all(r.ocp_certified == UNKNOWN for r in results)
    assert all("consecutive failures" in r.error for r in results)


def test_missing_detail_page_falls_back_to_major_versions(normalizer, fast_http, tmp_path):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, SEARCH_URL, callback=search_callback)
        rsps.add(responses.GET, re.compile(r"https://catalog\.redhat\.com/en/hardware/.*"), status=404)
        client = CatalogClient(fast_http, normalizer, cache=Cache(tmp_path))
        result = client.lookup(platform("Dell", "PowerEdge R650"))
    assert result.ocp_certified == YES
    assert result.ocp_versions == "4.x"
    assert result.ocp_virt_certified == YES
    assert result.versions_are_major_only
    assert result.rhel_versions == "7.x, 8.x, 9.x"


def test_blank_vendor_or_model_is_not_looked_up(client, catalog_api):
    result = client.lookup(platform("Dell", ""))
    assert result.ocp_certified == UNKNOWN
    assert not search_calls(catalog_api)


def test_cache_avoids_repeat_api_calls(client, catalog_api, normalizer, fast_http, tmp_path):
    first = client.lookup(platform("Dell", "PowerEdge R650"))
    calls = len(catalog_api.calls)
    again = CatalogClient(fast_http, normalizer, cache=Cache(tmp_path / "cache")).lookup(platform("Dell", "PowerEdge R650"))
    assert len(catalog_api.calls) == calls
    assert again.ocp_versions == first.ocp_versions
    assert again.catalog_urls == first.catalog_urls


def test_disabled_or_expired_cache_refetches(client, catalog_api, normalizer, fast_http, tmp_path):
    client.lookup(platform("Dell", "PowerEdge R650"))
    calls = len(catalog_api.calls)
    CatalogClient(fast_http, normalizer, cache=Cache(tmp_path / "cache", enabled=False)).lookup(platform("Dell", "PowerEdge R650"))
    assert len(catalog_api.calls) > calls
    calls = len(catalog_api.calls)
    expired = Cache(tmp_path / "cache", ttl=-1)
    CatalogClient(fast_http, normalizer, cache=expired).lookup(platform("Dell", "PowerEdge R650"))
    assert len(catalog_api.calls) > calls


def test_failed_lookups_are_not_cached(normalizer, fast_http, tmp_path):
    cache = Cache(tmp_path)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, SEARCH_URL, status=500)
        CatalogClient(fast_http, normalizer, cache=cache).lookup(platform("Dell", "PowerEdge R650"))
    assert list(tmp_path.iterdir()) == []


def test_each_platform_costs_one_search_when_matched(client, catalog_api):
    client.lookup(platform("HPE", "ProLiant DL380 Gen10"))
    assert len(search_calls(catalog_api)) == 1


def test_rate_limit_is_shared_by_parallel_lookups(normalizer, tmp_path):
    # Real clock: worker threads share one PoliteSession and must not exceed its rate together.
    stamps = []

    def stamped(callback):
        def wrapper(request):
            stamps.append(time.monotonic())
            return callback(request)

        return wrapper

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, SEARCH_URL, callback=stamped(search_callback))
        rsps.add_callback(responses.GET, DETAIL_RE, callback=stamped(detail_callback))
        client = CatalogClient(PoliteSession(rate=5), normalizer, cache=Cache(tmp_path), workers=2)
        client.lookup_all([platform("Dell", "PowerEdge R650"), platform("HPE", "ProLiant DL380 Gen10"), platform("Lenovo", "ThinkSystem SR650")])
    stamps.sort()
    assert len(stamps) == 6  # 3 searches + 3 detail pages
    for a, b in zip(stamps, stamps[1:]):
        assert b - a >= 0.2 - 0.02


def test_rhel_versions_sorted_numerically(client):
    result = client.lookup(platform("HPE", "ProLiant DL380 Gen10"))
    assert result.rhel_versions == "6.9-6.x, 7.3-7.x, 8.0-8.x, 9.0-9.x, 10.0-10.x"


def test_rhel_versions_exclude_real_time(client):
    result = client.lookup(platform("Dell", "PowerEdge R650"))
    assert result.rhel_versions == "7.9-7.x, 8.2-8.x, 9.0-9.x"
