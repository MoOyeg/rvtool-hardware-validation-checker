from datetime import datetime, timezone

import pytest
from pypdf import PdfReader

from rvtools_hw_check.catalog import EXACT, NONE, PARTIAL, Certification, Listing, PlatformResult
from rvtools_hw_check.report_md import FileSummary, RunReport, build_markdown, write_markdown
from rvtools_hw_check.rvtools import Host, Platform

RUN_DATE = datetime(2026, 10, 14, 9, 30, 5, tzinfo=timezone.utc)
DETAIL = "https://catalog.redhat.com/hardware/system/detail/81185"


def make_report() -> RunReport:
    dell_hosts = [
        Host("a.xlsx", "esx01", "Dell", "PowerEdge R650", "Dell Inc.", "PowerEdge R650", "DC1", "Prod"),
        Host("b.xls", "esx10", "Dell", "PowerEdge R650", "Dell Inc.", "PowerEdge R650", "DC2", "A"),
    ]
    acme_host = Host("b.xls", "esx12", "Acme Corp", "Widget|9000", "Acme Corp", "Widget|9000")
    hpe_host = Host("a.xlsx", "esx03", "HPE", "ProLiant DL380 Gen10 Plus v2", "HPE", "ProLiant DL380 Gen10 Plus v2")
    broken_host = Host("a.xlsx", "esx04", "Cisco", "UCS C220 M5SX", "Cisco Systems Inc", "UCSC-C220-M5SX")

    dell = PlatformResult(
        Platform("Dell", "PowerEdge R650", dell_hosts),
        match_confidence=EXACT,
        listings=[
            Listing(
                "81185",
                "Dell PowerEdge R650",
                "Dell Technologies",
                DETAIL,
                certifications=[
                    Certification("1", "Red Hat OpenShift Container Platform", "4.8", "4.12", vm_host=True),
                    Certification("2", "Red Hat OpenShift Container Platform", "4.13", "4.x", vm_host=False),
                    Certification("3", "Red Hat Enterprise Linux", "9.0", "9.x"),
                    Certification("4", "Red Hat Enterprise Linux", "8.2", "8.x"),
                    Certification("5", "Red Hat Enterprise Linux for Real Time", "9.0", "9.x"),
                ],
            )
        ],
    )
    hpe = PlatformResult(
        Platform("HPE", "ProLiant DL380 Gen10 Plus v2", [hpe_host]),
        match_confidence=PARTIAL,
        listings=[
            Listing(
                "124815",
                "HPE ProLiant DL380 Gen10 Plus",
                "HPE",
                "https://catalog.redhat.com/hardware/system/detail/124815",
                certified_products=["Red Hat OpenShift Container Platform"],
                certified_versions=["Red Hat OpenShift Container Platform 4", "Red Hat Enterprise Linux 8", "Red Hat Enterprise Linux for Real Time 8"],
            )
        ],
    )
    acme = PlatformResult(Platform("Acme Corp", "Widget|9000", [acme_host]), match_confidence=NONE)
    broken = PlatformResult(Platform("Cisco", "UCS C220 M5SX", [broken_host]), error="HTTP 503 from catalog")
    return RunReport(
        run_date=RUN_DATE,
        files=[FileSummary("a.xlsx", 3, 3), FileSummary("b.xls", 2, 2), FileSummary("junk.xlsx", error="No 'vHost' sheet found")],
        results=[acme, dell, hpe, broken],
        hosts=dell_hosts + [acme_host, hpe_host, broken_host],
        errors=[("junk.xlsx", "No 'vHost' sheet found")],
    )


@pytest.fixture
def report() -> RunReport:
    return make_report()


def test_markdown_has_run_date_files_and_counts(report):
    md = build_markdown(report)
    assert "**Run date:** 2026-10-14T09:30:05Z" in md
    for name in ("a.xlsx", "b.xls", "junk.xlsx"):
        assert name in md
    assert "Skipped: No 'vHost' sheet found" in md
    assert "| Certified for OpenShift | 2 |" in md
    assert "| Certified for OpenShift Virtualization | 1 |" in md
    assert "| Not certified | 1 |" in md
    assert "| Unknown (lookup failed) | 1 |" in md
    assert "| Needs review (partial match) | 1 |" in md
    assert "| Certified for RHEL | 2 |" in md


def test_markdown_platform_rows_have_status_versions_and_links(report):
    md = build_markdown(report)
    dell_row = next(line for line in md.splitlines() if line.startswith("| Dell | PowerEdge R650"))
    assert "| Yes | 4.8-4.12, 4.13-4.x | Yes | 4.8-4.12 | 8.2-8.x, 9.0-9.x | Exact |" in dell_row
    assert f"[Dell PowerEdge R650]({DETAIL})" in dell_row
    assert "| DC1 (1), DC2 (1) | a.xlsx (1), b.xls (1) |" in dell_row

    hpe_row = next(line for line in md.splitlines() if line.startswith("| HPE |"))
    assert "Partial (review)" in hpe_row
    assert "4.x (major only; see catalog)" in hpe_row
    assert "| 8.x (major only; see catalog) |" in hpe_row

    acme_row = next(line for line in md.splitlines() if line.startswith("| Acme Corp |"))
    assert "Widget\\|9000" in acme_row  # pipes escaped so the table is not broken
    assert "[Search catalog](https://catalog.redhat.com/search?" in acme_row

    cisco_row = next(line for line in md.splitlines() if line.startswith("| Cisco |"))
    assert "| Unknown |" in cisco_row


def test_markdown_hosts_section_links_each_host(report):
    md = build_markdown(report)
    hosts_section = md.split("## Hosts")[1].split("## Errors")[0]
    assert "| RVTools file | Datacenter | Cluster | ESXi host |" in hosts_section
    assert "| a.xlsx | DC1 | Prod | esx01 |" in hosts_section
    esx01 = next(line for line in hosts_section.splitlines() if "| esx01 |" in line)
    assert DETAIL in esx01
    assert "| Yes | Yes |" in esx01


def test_markdown_errors_section(report):
    errors = build_markdown(report).split("## Errors")[1]
    assert "junk.xlsx" in errors
    assert "HTTP 503 from catalog" in errors


def test_report_filename_contains_run_date(report, tmp_path):
    path = write_markdown(report, tmp_path / "out")
    assert path.name == "hw-validation-report-20261014-093005.md"
    assert path.read_text().startswith("# Hardware Validation Report")


def test_pdf_contains_run_date_and_clickable_links(report, tmp_path):
    from rvtools_hw_check.report_pdf import write_pdf

    path = write_pdf(report, tmp_path)
    assert path.name == "hw-validation-report-20261014-093005.pdf"
    reader = PdfReader(str(path))
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "2026-10-14T09:30:05Z" in text
    assert "a.xlsx" in text and "b.xls" in text
    uris = {
        annot.get_object()["/A"]["/URI"]
        for page in reader.pages
        for annot in page.get("/Annots") or []
        if "/A" in annot.get_object() and "/URI" in annot.get_object()["/A"]
    }
    assert DETAIL in uris
    assert any(u.startswith("https://catalog.redhat.com/search?") for u in uris)


def test_pdf_paginates_long_host_tables(tmp_path):
    from rvtools_hw_check.report_pdf import write_pdf

    report = make_report()
    template = report.hosts[0]
    report.hosts = [
        Host(template.source_file, f"esx{i:04d}.example.com", template.vendor, template.model, template.vendor_raw, template.model_raw)
        for i in range(400)
    ]
    reader = PdfReader(str(write_pdf(report, tmp_path)))
    assert len(reader.pages) > 3
    # Narrow cells may wrap long host names, so compare without whitespace.
    text = "".join("".join(page.extract_text() for page in reader.pages).split())
    assert "esx0000.example.com" in text and "esx0399.example.com" in text


def test_summary_explains_platforms_vs_hosts(report, tmp_path):
    md = build_markdown(report)
    summary = md.split("## Summary")[1].split("### Input files")[0]
    assert "Platforms vs hosts" in summary
    assert "**5 host(s)** across **4 platform(s)**" in summary
    assert md.index("Platforms vs hosts") < md.index("| Metric | Count |")

    from rvtools_hw_check.report_pdf import write_pdf

    text = " ".join(" ".join(p.extract_text() for p in PdfReader(str(write_pdf(report, tmp_path))).pages).split())
    assert "Platforms vs hosts" in text


def _method_json(md: str) -> dict:
    import json

    section = md.split("## How certification is checked")[1]
    return json.loads(section.split("```json")[1].split("```")[0])


def test_method_section_quotes_real_catalog_fields_from_this_run(report):
    md = build_markdown(report)
    section = md.split("## How certification is checked")[1]
    assert f"from this run's lookup of [Dell PowerEdge R650]({DETAIL})" in section
    data = _method_json(md)
    assert data["id"] == "81185"
    [cert] = data["certifications"]
    # The certification with the VM host feature is preferred as evidence.
    assert cert["product"] == "Red Hat OpenShift Container Platform"
    assert (cert["startVersion"], cert["endVersion"]) == ("4.8", "4.12")
    assert cert["granularityData"] == [
        {"category": "Compute", "features": [{"name": "Virtual Machine (Host)", "supported": True}]}
    ]
    assert "`Virtual Machine (Host)` compute feature" in section
    assert "certified_granularity_compute" in section
    # It is the last section, after Errors.
    assert md.index("## Errors") < md.index("## How certification is checked")


def test_method_section_falls_back_to_labelled_example(report):
    report.results = [r for r in report.results if r.match_confidence != EXACT]
    md = build_markdown(report)
    assert "illustrative example" in md.split("## How certification is checked")[1]
    assert _method_json(md)["certifications"][0]["granularityData"][0]["features"][0]["supported"] is True


def test_pdf_renders_method_json(report, tmp_path):
    from rvtools_hw_check.report_pdf import write_pdf

    text = " ".join(" ".join(p.extract_text() for p in PdfReader(str(write_pdf(report, tmp_path))).pages).split())
    assert "How certification is checked" in text
    assert '"name": "Virtual Machine (Host)"' in text
    assert "```" not in text  # fenced block rendered as code, not literal backticks


def test_platforms_table_has_rhel_column(report):
    md = build_markdown(report)
    header = next(line for line in md.splitlines() if line.startswith("| Vendor | Model |"))
    assert "| OCP-V versions | RHEL versions | Match |" in header


def test_divider_separates_platforms_from_hosts(report):
    md = build_markdown(report)
    platforms, divider, hosts = md.index("## Platforms"), md.index('class="section-divider"'), md.index("## Hosts")
    assert platforms < divider < hosts
    assert "<h1>Hosts</h1>" in md
    assert "5 ESXi host(s) from 2 RVTools file(s)" in md


def test_pdf_divider_is_its_own_page(report, tmp_path):
    from rvtools_hw_check.report_pdf import write_pdf

    pages = [p.extract_text() for p in PdfReader(str(write_pdf(report, tmp_path))).pages]
    divider_pages = [i for i, text in enumerate(pages) if "ESXi host(s) from" in text]
    assert len(divider_pages) == 1
    i = divider_pages[0]
    assert "Vendor" not in pages[i] and "esx01" not in pages[i]  # nothing else on that page
    assert "Platforms" in " ".join(pages[:i])
    assert "esx01" in pages[i + 1]  # hosts table starts on the next page
