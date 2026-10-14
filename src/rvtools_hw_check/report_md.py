"""Markdown report (spec FR-5). The PDF is rendered from this same Markdown."""

from __future__ import annotations

import json
from html import escape
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .catalog import EXACT, NO, OCP, UNKNOWN, VM_HOST_FEATURE, YES, Certification, PlatformResult
from .rvtools import Host


@dataclass
class FileSummary:
    name: str
    hosts: int = 0
    platforms: int = 0
    error: str = ""


@dataclass
class RunReport:
    run_date: datetime
    files: list[FileSummary]
    results: list[PlatformResult]
    hosts: list[Host]
    errors: list[tuple[str, str]] = field(default_factory=list)
    app_version: str = __version__

    @property
    def run_date_iso(self) -> str:
        return self.run_date.isoformat().replace("+00:00", "Z")

    @property
    def file_stamp(self) -> str:
        return self.run_date.strftime("%Y%m%d-%H%M%S")

    def counts(self) -> dict[str, int]:
        return {
            "Unique platforms": len(self.results),
            "Certified for OpenShift": sum(r.ocp_certified == YES for r in self.results),
            "Certified for OpenShift Virtualization": sum(r.ocp_virt_certified == YES for r in self.results),
            "Certified for RHEL": sum(bool(r.rhel_versions) for r in self.results),
            "Not certified": sum(r.ocp_certified == NO for r in self.results),
            "Unknown (lookup failed)": sum(r.ocp_certified == UNKNOWN for r in self.results),
            "Needs review (partial match)": sum(r.needs_review for r in self.results),
        }

    def result_for(self, host: Host) -> PlatformResult | None:
        for result in self.results:
            if result.platform.key == host.platform_key:
                return result
        return None


def report_filename(report: RunReport, ext: str) -> str:
    return f"hw-validation-report-{report.file_stamp}.{ext}"


def _cell(value) -> str:
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ") or "-"


def _link_text(label: str) -> str:
    return label.replace("[", "(").replace("]", ")").replace("|", "/")


def _links(result: PlatformResult) -> str:
    return "<br>".join(f"[{_link_text(label)}]({url})" for label, url in result.catalog_links)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _versions(result: PlatformResult, versions: str) -> str:
    if versions and result.versions_are_major_only:
        return f"{versions} (major only; see catalog)"
    return versions


def build_markdown(report: RunReport) -> str:
    out: list[str] = [
        "# Hardware Validation Report",
        "",
        f"**Run date:** {report.run_date_iso}  ",
        f"**App version:** {report.app_version}  ",
        "**Source:** [Red Hat Ecosystem Catalog](https://catalog.redhat.com/hardware)",
        "",
        "## Summary",
        "",
        "> **Platforms vs hosts.** A *host* is one physical ESXi server from the RVTools exports. "
        "A *platform* is a hardware model (vendor + model, e.g. Dell PowerEdge R650) shared by one or more hosts. "
        "Red Hat certifies hardware models, not individual servers, so each platform is checked once and every "
        f"host inherits its platform's result. This run found **{len(report.hosts)} host(s)** across "
        f"**{len(report.results)} platform(s)**. Use **Platforms** to see which models are certified; "
        "use **Hosts** to find where each server is (RVTools file, datacenter, cluster).",
        "",
    ]
    out += _table(["Metric", "Count"], [[k, str(v)] for k, v in report.counts().items()])
    out += ["", "### Input files", ""]
    out += _table(
        ["File", "Hosts", "Platforms", "Status"],
        [[_cell(f.name), str(f.hosts), str(f.platforms), _cell(f"Skipped: {f.error}" if f.error else "OK")] for f in report.files],
    )

    out += ["", "## Platforms", "", "One row per hardware model, with its certification result and where its hosts are.", ""]
    out += _table(
        [
            "Vendor",
            "Model",
            "OpenShift",
            "OCP versions",
            "OpenShift Virtualization",
            "OCP-V versions",
            "RHEL versions",
            "Match",
            "Hosts",
            "Datacenters",
            "RVTools files",
            "Catalog link",
        ],
        [
            [
                _cell(r.platform.vendor),
                _cell(r.platform.model),
                r.ocp_certified,
                _cell(_versions(r, r.ocp_versions)),
                r.ocp_virt_certified,
                _cell(_versions(r, r.ocp_virt_versions)),
                _cell(_versions(r, r.rhel_versions)),
                r.match_confidence + (" (review)" if r.needs_review else ""),
                str(r.platform.host_count),
                _cell(", ".join(f"{name} ({count})" for name, count in r.platform.datacenters.items())),
                _cell(", ".join(f"{name} ({count})" for name, count in r.platform.files.items())),
                _links(r),
            ]
            for r in report.results
        ],
    )

    out += ["", *_divider("Hosts", f"{len(report.hosts)} ESXi host(s) from {sum(not f.error for f in report.files)} RVTools file(s). "
                         "Each host shows the result of its platform (hardware model) from the Platforms section."), ""]
    out += ["## Hosts", "", "One row per ESXi server; the status shown is its platform's result.", ""]
    host_rows = []
    for host in report.hosts:
        result = report.result_for(host)
        host_rows.append(
            [
                _cell(host.source_file),
                _cell(host.datacenter),
                _cell(host.cluster),
                _cell(host.host),
                _cell(host.vendor_raw),
                _cell(host.model_raw),
                _cell(host.cpu_model),
                _cell(host.esx_version),
                result.ocp_certified if result else UNKNOWN,
                result.ocp_virt_certified if result else UNKNOWN,
                _links(result) if result else "-",
            ]
        )
    out += _table(
        ["RVTools file", "Datacenter", "Cluster", "ESXi host", "Vendor", "Model", "CPU model", "ESX version", "OpenShift", "OCP-V", "Catalog link"],
        host_rows,
    )

    out += ["", "## Errors", ""]
    errors = list(report.errors) + [
        (f"{r.platform.vendor} {r.platform.model}".strip() or "(blank)", r.error) for r in report.results if r.error
    ]
    if errors:
        out += _table(["Item", "Reason"], [[_cell(item), _cell(reason)] for item, reason in errors])
    else:
        out.append("No errors.")

    out += ["", *_method_section(report), ""]
    return "\n".join(out)


def _divider(title: str, subtitle: str) -> list[str]:
    """A section divider. In the PDF it is a page of its own; in Markdown a rule and a banner."""
    return [
        "---",
        "",
        f'<div class="section-divider"><p class="divider-kicker">Section</p><h1>{escape(title)}</h1>'
        f"<p>{escape(subtitle)}</p></div>",
        "",
    ]


# Shown when this run has no certification detail to quote (e.g. nothing matched).
EXAMPLE_EVIDENCE = (
    "Dell PowerEdge R650",
    "https://catalog.redhat.com/hardware/system/detail/81185",
    Certification("561407", OCP, "4.13", "4.x", "x86_64", vm_host=True),
)


def _evidence(report: RunReport) -> tuple[str, str, Certification, bool]:
    """Pick a real OCP certification from this run to quote, preferring one with the VM host feature."""
    certs = [
        (listing.name, listing.url, cert)
        for result in report.results
        if result.match_confidence == EXACT
        for listing in result.listings
        for cert in listing.certifications or []
        if cert.product == OCP
    ]
    for name, url, cert in sorted(certs, key=lambda c: not c[2].vm_host):
        return name, url, cert, True
    return (*EXAMPLE_EVIDENCE, False)


def evidence_json(listing_id: str, cert: Certification) -> str:
    """The catalog fields the app reads for one certification, in the catalog's own field names."""
    return json.dumps(
        {
            "id": listing_id,
            "certifications": [
                {
                    "id": cert.id,
                    "product": cert.product,
                    "startVersion": cert.start_version,
                    "endVersion": cert.end_version,
                    "architecture": cert.architecture,
                    "granularityData": [
                        {
                            "category": "Compute",
                            "features": [{"name": VM_HOST_FEATURE, "supported": cert.vm_host}],
                        }
                    ],
                }
            ],
        },
        indent=2,
    )


def _method_section(report: RunReport) -> list[str]:
    name, url, cert, from_run = _evidence(report)
    listing_id = url.rstrip("/").rsplit("/", 1)[-1]
    source = (
        f"from this run's lookup of [{_link_text(name)}]({url})"
        if from_run
        else f"an illustrative example ([{_link_text(name)}]({url})); no certification detail was retrieved in this run"
    )
    return [
        "## How certification is checked",
        "",
        "For each platform the app reads the certifications on its Red Hat Ecosystem Catalog hardware page. "
        f"Below are the fields it uses, trimmed to one certification, {source}:",
        "",
        "```json",
        evidence_json(listing_id, cert),
        "```",
        "",
        f"- **OpenShift**: a certification whose `product` is `{OCP}`. "
        "Its versions are `startVersion`-`endVersion` (`4.x` means \"and later\").",
        f"- **OpenShift Virtualization**: the catalog has no separate product for it. A platform counts as certified "
        f"when its OpenShift certification lists the `{VM_HOST_FEATURE}` compute feature with `\"supported\": true`; "
        "the OpenShift Virtualization versions are that certification's versions.",
        "- If the hardware page can't be read, the app falls back to the catalog search fields "
        "`certified_products`, `certified_versions` and `certified_granularity_compute`. These only give the major "
        "version, so the report shows `4.x (major only; see catalog)`.",
        "- Partial matches need a person to confirm them using the catalog link.",
    ]


def write_markdown(report: RunReport, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / report_filename(report, "md")
    path.write_text(build_markdown(report), encoding="utf-8")
    return path
