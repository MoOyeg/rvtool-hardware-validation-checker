"""Record trimmed Red Hat catalog responses for the offline test suite.

Run manually (needs network); tests never call the live API:

    python tests/fixtures/record_catalog.py

Search responses are saved as JSON. Detail pages are reduced to a minimal HTML
page that keeps the same embedded-JSON structure the real page uses.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rvtools_hw_check.catalog import DETAIL_PAGE_URL, SEARCH_FIELDS, SEARCH_URL, _NEXT_PUSH, _find_listing  # noqa: E402

OUT = Path(__file__).parent / "catalog"
QUERIES = [
    "Dell PowerEdge R650",
    "HPE ProLiant DL380 Gen10",
    "Lenovo ThinkSystem SR650",
    "Cisco UCS C220 M5SX",
    "Acme Widget 9000",
    "Widget 9000",
]
DETAIL_IDS = ["81185", "6590", "124815", "6526", "294347"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def trim_cert(cert: dict) -> dict:
    keep = {k: cert.get(k) for k in ("id", "product", "startVersion", "majorVersion", "endVersion", "architecture")}
    keep["granularityData"] = [
        {"category": g["category"], "features": [{"name": f["name"], "supported": f["supported"]} for f in g.get("features", [])]}
        for g in cert.get("granularityData") or []
        if g.get("category") == "Compute"
    ]
    return keep


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "rvtools-hw-check-fixture-recorder"
    for query in QUERIES:
        params = {
            "type": "search",
            "redhat_client": "ecosystem-catalog",
            "q": query,
            "fq": ["documentKind:CertifiedHardware", "certified_category:System"],
            "fl": SEARCH_FIELDS,
            "rows": 100,
        }
        body = session.get(SEARCH_URL, params=params, timeout=30).json()
        trimmed = {"response": {"numFound": body["response"]["numFound"], "docs": body["response"]["docs"]}}
        (OUT / f"search_{slug(query)}.json").write_text(json.dumps(trimmed, indent=1) + "\n")
        print("search", query, trimmed["response"]["numFound"])
        time.sleep(1)

    for listing_id in DETAIL_IDS:
        html = session.get(DETAIL_PAGE_URL.format(id=listing_id), timeout=30).text
        data = None
        for m in _NEXT_PUSH.finditer(html):
            chunk = json.loads(m.group(1))
            if '"certifications"' in chunk:
                data = _find_listing(json.loads(chunk.partition(":")[2]), listing_id)
                if data:
                    break
        if data is None:
            print("detail", listing_id, "NOT FOUND")
            continue
        small = {"id": data["id"], "name": data["name"], "certifications": [trim_cert(c) for c in data["certifications"]]}
        payload = "18:" + json.dumps(["$", "div", None, {"className": "detail-page", "children": ["$", "$L4b", None, {"data": small}]}])
        page = (
            "<!DOCTYPE html><html><head><title>recorded</title></head><body>"
            f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script></body></html>\n"
        )
        (OUT / f"detail_{listing_id}.html").write_text(page)
        print("detail", listing_id, len(small["certifications"]), "certifications")
        time.sleep(1)


if __name__ == "__main__":
    main()
