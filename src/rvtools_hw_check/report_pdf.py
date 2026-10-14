"""PDF report rendered from the Markdown report (spec FR-5.3, FR-5.5)."""

from __future__ import annotations

from pathlib import Path

import markdown

from .report_md import RunReport, build_markdown, report_filename

CSS = """
@page { size: A4 landscape; margin: 12mm;
        @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #555; } }
body { font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif; font-size: 8pt; color: #151515; }
h1 { font-size: 16pt; color: #ee0000; margin: 0 0 6pt; }
h2 { font-size: 12pt; border-bottom: 1px solid #ccc; margin-top: 14pt; }
h3 { font-size: 10pt; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 4pt 0; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #bbb; padding: 2pt 3pt; text-align: left; vertical-align: top;
         overflow-wrap: anywhere; hyphens: auto; }
th { background: #f0f0f0; }
a { color: #0066cc; text-decoration: none; }
blockquote { margin: 4pt 0; padding: 4pt 8pt; border-left: 3px solid #ee0000; background: #f7f7f7; }
pre { background: #f4f4f4; border: 1px solid #ddd; padding: 4pt 6pt; font-size: 7.5pt; white-space: pre-wrap; page-break-inside: avoid; }
code { font-family: "DejaVu Sans Mono", monospace; }
hr { display: none; }
.section-divider { page-break-before: always; page-break-after: always; height: 150mm;
                   display: flex; flex-direction: column; justify-content: center; align-items: center;
                   text-align: center; border-top: 4px solid #ee0000; border-bottom: 4px solid #ee0000; }
.section-divider h1 { font-size: 36pt; margin: 4pt 0; color: #151515; }
.section-divider p { font-size: 11pt; color: #555; margin: 0; max-width: 160mm; }
.section-divider .divider-kicker { font-size: 10pt; letter-spacing: 2pt; text-transform: uppercase; color: #ee0000; }
"""


def build_html(report: RunReport) -> str:
    body = markdown.markdown(build_markdown(report), extensions=["tables", "fenced_code"])
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Hardware Validation Report {report.run_date_iso}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def write_pdf(report: RunReport, output_dir: Path) -> Path:
    from weasyprint import HTML  # imported lazily: needs system Pango libraries

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / report_filename(report, "pdf")
    HTML(string=build_html(report)).write_pdf(str(path))
    return path
