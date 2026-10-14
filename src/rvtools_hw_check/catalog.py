"""Red Hat Ecosystem Catalog client, matching and classification (spec FR-4).

Data sources (see spec section 8):
- Search: the catalog's Solr endpoint, filtered to certified hardware systems.
- Per-version detail: the public hardware detail page, which embeds each
  certification (product, startVersion, endVersion, features) as JSON.

OpenShift Virtualization is reported as certified when an OpenShift Container
Platform certification lists the "Virtual Machine (Host)" compute feature as
supported. That is how the catalog records that a system was tested as a
virtualization host.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode

from rapidfuzz import fuzz

from .cache import Cache
from .ratelimit import ApiError, PoliteSession
from .rvtools import Normalizer, Platform

logger = logging.getLogger(__name__)

SEARCH_URL = "https://catalog.redhat.com/api/solr"
DETAIL_PAGE_URL = "https://catalog.redhat.com/en/hardware/system/detail/{id}"
DETAIL_LINK = "https://catalog.redhat.com/hardware/system/detail/{id}"
SEARCH_LINK = "https://catalog.redhat.com/search?{query}"
SEARCH_ROWS = 100  # one page is plenty for a single model; avoids follow-up requests
SEARCH_FIELDS = (
    "id,partnerProductName,make,model,partnerName,certified_products,"
    "certified_versions,certified_granularity_compute,view_uri"
)
OCP = "Red Hat OpenShift Container Platform"
RHEL = "Red Hat Enterprise Linux"
VM_HOST_FEATURE = "Virtual Machine (Host)"
PARTIAL_THRESHOLD = 90
MAX_CANDIDATES = 3
CACHE_VERSION = "v1"

YES, NO, UNKNOWN = "Yes", "No", "Unknown"
EXACT, PARTIAL, NONE = "Exact", "Partial", "None"


@dataclass
class Certification:
    id: str
    product: str
    start_version: str
    end_version: str
    architecture: str = ""
    vm_host: bool = False

    @property
    def version_range(self) -> str:
        if not self.end_version or self.end_version == self.start_version:
            return self.start_version
        return f"{self.start_version}-{self.end_version}"


@dataclass
class Listing:
    id: str
    name: str
    partner: str
    url: str
    certified_products: list[str] = field(default_factory=list)
    certified_versions: list[str] = field(default_factory=list)
    compute_features: list[str] = field(default_factory=list)
    certifications: list[Certification] | None = None  # None = detail page unavailable

    @classmethod
    def from_doc(cls, doc: dict) -> "Listing":
        listing_id = str(doc.get("id", ""))
        name = doc.get("partnerProductName") or " ".join(filter(None, [doc.get("make"), doc.get("model")]))
        return cls(
            id=listing_id,
            name=name,
            partner=doc.get("partnerName", ""),
            url=doc.get("view_uri") or DETAIL_LINK.format(id=listing_id),
            certified_products=list(doc.get("certified_products") or []),
            certified_versions=list(doc.get("certified_versions") or []),
            compute_features=list(doc.get("certified_granularity_compute") or []),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Listing":
        data = dict(data)
        certs = data.pop("certifications", None)
        listing = cls(**data)
        if certs is not None:
            listing.certifications = [Certification(**c) for c in certs]
        return listing

    def product_versions(self) -> tuple[list[str], list[str], list[str], bool]:
        """Return (ocp ranges, ocp-virt ranges, rhel ranges, minor versions known)."""
        if self.certifications is not None:
            ocp = [c for c in self.certifications if c.product == OCP]
            rhel = [c.version_range for c in self.certifications if c.product == RHEL]
            return [c.version_range for c in ocp], [c.version_range for c in ocp if c.vm_host], rhel, True
        # Fallback to search data: only the major version is known.
        rhel = self._major_versions(RHEL)
        if OCP not in self.certified_products:
            return [], [], rhel, False
        majors = self._major_versions(OCP) or ["4.x"]
        virt = majors if VM_HOST_FEATURE in self.compute_features else []
        return majors, virt, rhel, False

    def _major_versions(self, product: str) -> list[str]:
        # "Red Hat Enterprise Linux 9" -> "9.x"; skips "... for Real Time 9"
        pattern = re.compile(re.escape(product) + r" (\d+)$")
        return [f"{m.group(1)}.x" for v in self.certified_versions if (m := pattern.match(v))]


@dataclass
class PlatformResult:
    platform: Platform
    match_confidence: str = NONE
    listings: list[Listing] = field(default_factory=list)
    error: str = ""
    search_query: str = ""

    @property
    def lookup_failed(self) -> bool:
        return bool(self.error)

    def _versions(self) -> tuple[list[str], list[str], list[str], bool]:
        ocp: set[str] = set()
        virt: set[str] = set()
        rhel: set[str] = set()
        exact_versions = True
        for listing in self.listings:
            o, v, r, known = listing.product_versions()
            ocp.update(o)
            virt.update(v)
            rhel.update(r)
            exact_versions = exact_versions and known
        return sort_versions(ocp), sort_versions(virt), sort_versions(rhel), exact_versions

    @property
    def ocp_certified(self) -> str:
        if self.lookup_failed:
            return UNKNOWN
        return YES if self._versions()[0] else NO

    @property
    def ocp_virt_certified(self) -> str:
        if self.lookup_failed:
            return UNKNOWN
        return YES if self._versions()[1] else NO

    @property
    def ocp_versions(self) -> str:
        return ", ".join(self._versions()[0])

    @property
    def ocp_virt_versions(self) -> str:
        return ", ".join(self._versions()[1])

    @property
    def rhel_versions(self) -> str:
        return ", ".join(self._versions()[2])

    @property
    def versions_are_major_only(self) -> bool:
        return bool(self.listings) and not self._versions()[3]

    @property
    def needs_review(self) -> bool:
        return self.match_confidence == PARTIAL

    @property
    def catalog_urls(self) -> list[str]:
        """Hardware detail links for matches, else a pre-filled catalog search link (FR-4.7)."""
        if self.listings:
            return [listing.url for listing in self.listings]
        return [search_link(self.platform)]

    @property
    def catalog_links(self) -> list[tuple[str, str]]:
        """(label, url) pairs for the report."""
        if self.listings:
            return [(listing.name, listing.url) for listing in self.listings]
        return [("Search catalog", search_link(self.platform))]


def sort_versions(ranges) -> list[str]:
    """Sort ranges like "6.9-6.x", "10.0-10.x" numerically by their start version."""

    def key(value: str):
        start = value.split("-", 1)[0]
        return [int(p) if p.isdigit() else float("inf") for p in start.split(".")]

    return sorted(ranges, key=key)


def search_link(platform: Platform) -> str:
    query = " ".join(filter(None, [platform.vendor, platform.model]))
    return SEARCH_LINK.format(query=urlencode({"searchType": "hardware", "type": "System", "q": query}))


def _words(value: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()


def _model_key(text: str, vendor_words: set[str]) -> str:
    words = _words(text)
    while words and words[0] in vendor_words:
        words = words[1:]
    return " ".join(words)


def match_listings(platform: Platform, docs: list[dict], normalizer: Normalizer) -> tuple[str, list[dict]]:
    """Pick the catalog documents that describe this platform (FR-4.4)."""
    vendor_words = set(_words(platform.vendor))
    target = _model_key(platform.model, vendor_words)
    if not target:
        return NONE, []

    exact: list[dict] = []
    partial: list[tuple[tuple[float, float], dict]] = []
    for doc in docs:
        doc_vendor = normalizer.vendor(doc.get("partnerName", ""))
        product_name = doc.get("partnerProductName", "")
        if doc_vendor != platform.vendor and not vendor_words & set(_words(product_name)[:2]):
            continue
        doc_vendor_words = vendor_words | set(_words(doc_vendor)) | set(_words(doc.get("partnerName", "")))
        keys = {
            _model_key(product_name, doc_vendor_words),
            _model_key(f"{doc.get('make', '')} {doc.get('model', '')}", doc_vendor_words),
            _model_key(doc.get("model", ""), doc_vendor_words),
        } - {""}
        if target in keys:
            exact.append(doc)
            continue
        # token_set_ratio finds subsets ("DL380 Gen10" within "DL380 Gen10 Plus");
        # plain ratio breaks ties in favour of the closest overall name.
        score = max(((fuzz.token_set_ratio(target, k), fuzz.ratio(target, k)) for k in keys), default=(0, 0))
        if score[0] >= PARTIAL_THRESHOLD:
            partial.append((score, doc))

    if exact:
        return EXACT, exact
    if partial:
        partial.sort(key=lambda item: item[0], reverse=True)
        return PARTIAL, [doc for _, doc in partial[:MAX_CANDIDATES]]
    return NONE, []


_NEXT_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def parse_detail_page(html: str, listing_id: str) -> list[Certification] | None:
    """Extract certifications from the hardware detail page's embedded JSON."""
    for match in _NEXT_PUSH.finditer(html):
        try:
            chunk = json.loads(match.group(1))
        except ValueError:
            continue
        if '"certifications"' not in chunk:
            continue
        _, _, payload = chunk.partition(":")
        try:
            tree = json.loads(payload)
        except ValueError:
            continue
        data = _find_listing(tree, str(listing_id))
        if data is not None:
            return [_certification(c) for c in data.get("certifications") or []]
    return None


def _find_listing(tree, listing_id: str) -> dict | None:
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "certifications" in node and str(node.get("id")) == listing_id:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _certification(raw: dict) -> Certification:
    vm_host = any(
        feature.get("name") == VM_HOST_FEATURE and feature.get("supported")
        for group in raw.get("granularityData") or []
        for feature in group.get("features") or []
    )
    return Certification(
        id=str(raw.get("id", "")),
        product=raw.get("product") or "",
        start_version=str(raw.get("startVersion") or ""),
        end_version=str(raw.get("endVersion") or ""),
        architecture=raw.get("architecture") or "",
        vm_host=vm_host,
    )


class CatalogClient:
    def __init__(
        self,
        http: PoliteSession,
        normalizer: Normalizer,
        cache: Cache | None = None,
        token_manager=None,
        workers: int = 2,
    ):
        self.http = http
        self.normalizer = normalizer
        self.cache = cache or Cache(enabled=False)
        self.token_manager = token_manager
        self.workers = workers

    def search(self, query: str) -> list[dict]:
        params = {
            "type": "search",
            "redhat_client": "ecosystem-catalog",
            "q": query,
            "fq": ["documentKind:CertifiedHardware", "certified_category:System"],
            "fl": SEARCH_FIELDS,
            "rows": SEARCH_ROWS,
        }
        response = self.http.get(SEARCH_URL, params=params, token_manager=self.token_manager)
        try:
            return response.json()["response"]["docs"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(f"Unexpected search response format: {exc}") from exc

    def certifications(self, listing_id: str) -> list[Certification] | None:
        try:
            response = self.http.get(DETAIL_PAGE_URL.format(id=listing_id))
        except ApiError as exc:
            if exc.status is None or exc.status >= 500:
                raise
            logger.warning("Detail page for listing %s unavailable: %s", listing_id, exc)
            return None
        certs = parse_detail_page(response.text, listing_id)
        if certs is None:
            logger.warning("Could not parse detail page for listing %s; using major versions only", listing_id)
        return certs

    def lookup(self, platform: Platform) -> PlatformResult:
        result = PlatformResult(platform)
        if not platform.lookup_possible:
            result.error = "Vendor or model is blank in the RVTools export"
            return result

        cache_key = f"{CACHE_VERSION}|{platform.vendor}|{platform.model}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for %s %s", platform.vendor, platform.model)
            result.match_confidence = cached["match_confidence"]
            result.search_query = cached.get("search_query", "")
            result.listings = [Listing.from_dict(item) for item in cached["listings"]]
            return result

        try:
            for query in dict.fromkeys([f"{platform.vendor} {platform.model}", platform.model]):
                result.search_query = query
                confidence, docs = match_listings(platform, self.search(query), self.normalizer)
                if docs:
                    break
            result.match_confidence = confidence
            result.listings = [Listing.from_doc(doc) for doc in docs]
            for listing in result.listings:
                listing.certifications = self.certifications(listing.id)
        except ApiError as exc:
            result.error = str(exc)
            result.match_confidence = NONE
            result.listings = []
            logger.error("Lookup failed for %s %s: %s", platform.vendor, platform.model, exc)
            return result

        self.cache.set(
            cache_key,
            {
                "match_confidence": result.match_confidence,
                "search_query": result.search_query,
                "listings": [asdict(listing) for listing in result.listings],
            },
        )
        logger.info(
            "%s %s: %s match, OCP=%s, OCP-V=%s",
            platform.vendor,
            platform.model,
            result.match_confidence,
            result.ocp_certified,
            result.ocp_virt_certified,
        )
        return result

    def lookup_all(self, platforms: list[Platform]) -> list[PlatformResult]:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(self.lookup, platforms))
