"""End-to-end tests: real files in, recorded catalog responses, md + pdf out."""

import re

import pytest
import responses
from pypdf import PdfReader

from rvtools_hw_check.auth import TOKEN_GENERATE_URL, TOKEN_HOWTO_URL, TOKEN_URL
from rvtools_hw_check.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL, build_parser, main


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_OFFLINE_TOKEN", "offline-token")

    def _run(*args):
        out = tmp_path / "reports"
        code = main([*args, "-o", str(out), "--cache-dir", str(tmp_path / "cache"), "--rate-limit", "5"])
        return code, out

    return _run


def test_end_to_end_md_and_pdf(run, sample_files, catalog_api, capsys):
    code, out = run(str(sample_files[0]), str(sample_files[1]), "-f", "all")
    assert code == EXIT_OK

    [md_path] = out.glob("hw-validation-report-*.md")
    [pdf_path] = out.glob("hw-validation-report-*.pdf")
    stamp = re.search(r"(\d{8})-(\d{6})", md_path.name)
    assert pdf_path.stem == md_path.stem

    md = md_path.read_text()
    run_date = re.search(r"\*\*Run date:\*\* (\S+)", md).group(1)
    assert run_date.replace("-", "").replace(":", "").startswith(f"{stamp.group(1)}T{stamp.group(2)}")
    assert "rvtools_sample_a.xlsx" in md and "rvtools_sample_b.xls" in md

    rows = {line.split(" | ")[0].lstrip("| ") + " " + line.split(" | ")[1]: line for line in md.splitlines() if line.startswith("| ") and " | " in line}
    assert "| Yes | 4.8-4.12, 4.13-4.x | Yes |" in rows["Dell PowerEdge R650"]
    assert "(https://catalog.redhat.com/hardware/system/detail/81185)" in rows["Dell PowerEdge R650"]
    assert "rvtools_sample_a.xlsx (2), rvtools_sample_b.xls (1)" in rows["Dell PowerEdge R650"]
    assert "| Exact |" in rows["HPE ProLiant DL380 Gen10"]
    assert "detail/6590)" in rows["HPE ProLiant DL380 Gen10"]
    assert "detail/6526)" in rows["Lenovo ThinkSystem SR650"]
    assert "detail/294347)" in rows["Cisco UCS C220 M5SX"]
    assert "| No |" in rows["Acme Corp Widget 9000"]
    assert "[Search catalog](https://catalog.redhat.com/search?" in rows["Acme Corp Widget 9000"]

    reader = PdfReader(str(pdf_path))
    text = " ".join(page.extract_text() for page in reader.pages)
    assert run_date in text
    assert "rvtools_sample_a.xlsx" in text and "rvtools_sample_b.xls" in text
    uris = {
        a.get_object()["/A"]["/URI"]
        for page in reader.pages
        for a in page.get("/Annots") or []
        if "/URI" in a.get_object().get("/A", {})
    }
    assert "https://catalog.redhat.com/hardware/system/detail/81185" in uris

    printed = capsys.readouterr().out
    assert "Report written:" in printed
    assert "offline-token" not in printed and "test-access-token" not in printed


def test_default_format_is_markdown_only(run, sample_files, catalog_api):
    code, out = run(str(sample_files[0]))
    assert code == EXIT_OK
    assert [p.suffix for p in out.iterdir()] == [".md"]


def test_unique_platforms_looked_up_once_across_files(run, sample_files, catalog_api):
    run(str(sample_files[0]), str(sample_files[1]))
    searches = [c for c in catalog_api.calls if "/api/solr" in c.request.url]
    # 5 unique platforms; only the unmatched one needs a second (model-only) query.
    assert len(searches) == 6


def test_bad_file_is_skipped_and_exit_code_is_partial(run, sample_files, catalog_api, tmp_path):
    bad = tmp_path / "broken.xlsx"
    bad.write_text("nope")
    code, out = run(str(sample_files[0]), str(bad))
    assert code == EXIT_PARTIAL
    md = next(out.glob("*.md")).read_text()
    assert "broken.xlsx" in md and "Skipped:" in md


def test_no_valid_input_is_fatal(run, tmp_path):
    bad = tmp_path / "broken.xlsx"
    bad.write_text("nope")
    code, out = run(str(bad), str(tmp_path / "missing.xlsx"))
    assert code == EXIT_FATAL
    assert not out.exists()


def test_bad_token_is_fatal_and_points_to_token_docs(run, sample_files, capsys):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TOKEN_URL, status=400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"})
        code, _ = run(str(sample_files[0]))
    assert code == EXIT_FATAL
    printed = capsys.readouterr().out
    assert "invalid_grant" in printed
    assert TOKEN_GENERATE_URL in printed and TOKEN_HOWTO_URL in printed


def test_missing_token_without_tty_is_fatal(sample_files, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RH_OFFLINE_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code = main([str(sample_files[0]), "-o", str(tmp_path)])
    assert code == EXIT_FATAL
    assert TOKEN_HOWTO_URL in capsys.readouterr().out


def test_help_mentions_token_links(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    help_text = capsys.readouterr().out
    assert TOKEN_GENERATE_URL in help_text
    assert TOKEN_HOWTO_URL in help_text


@pytest.mark.parametrize("value", ["0", "6", "abc"])
def test_rate_limit_is_capped(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["x.xlsx", "--rate-limit", value])


def _hosts_section(md: str) -> str:
    return md.split("## Hosts")[1].split("## Errors")[0]


def test_input_dir_reads_every_export_in_folder(run, sample_files, catalog_api):
    folder = sample_files[0].parent
    code, out = run("--input-dir", str(folder))
    assert code == EXIT_OK
    md = next(out.glob("*.md")).read_text()
    assert "| rvtools_sample_a.xlsx | 4 | 3 | OK |" in md
    assert "| rvtools_sample_b.xls | 3 | 3 | OK |" in md

    dell = next(line for line in md.splitlines() if line.startswith("| Dell | PowerEdge R650"))
    assert "| DC-East (1), DC1 (2) |" in dell  # Datacenters column
    assert "| rvtools_sample_a.xlsx (2), rvtools_sample_b.xls (1) |" in dell  # RVTools files column

    hosts = _hosts_section(md)
    assert "| RVTools file | Datacenter | Cluster | ESXi host |" in hosts
    assert "| rvtools_sample_a.xlsx | DC1 | Prod | esx01.lab.local |" in hosts
    assert "| rvtools_sample_b.xls | DC-East | Cluster-A | esx10.corp.local |" in hosts


def test_input_dir_from_environment(run, sample_files, catalog_api, monkeypatch):
    monkeypatch.setenv("RVTOOLS_INPUT_DIR", str(sample_files[0].parent))
    code, out = run()
    assert code == EXIT_OK
    md = next(out.glob("*.md")).read_text()
    assert "rvtools_sample_a.xlsx" in md and "rvtools_sample_b.xls" in md


def test_input_dir_combines_with_files(run, sample_files, catalog_api, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    sample_files[1].rename(other / sample_files[1].name)
    code, out = run("-i", str(sample_files[0].parent), str(other / sample_files[1].name))
    assert code == EXIT_OK
    md = next(out.glob("*.md")).read_text()
    assert "rvtools_sample_a.xlsx" in md and "rvtools_sample_b.xls" in md


def test_recursive_input_dir_labels_same_named_files_by_path(run, catalog_api, tmp_path):
    from make_fixtures import make_samples

    root = tmp_path / "exports"
    make_samples(root / "site1")
    make_samples(root / "site2")

    code, out = run("-i", str(root))
    assert code == EXIT_FATAL  # top level has no exports without --recursive

    code, out = run("-i", str(root), "--recursive")
    assert code == EXIT_OK
    hosts = _hosts_section(next(out.glob("*.md")).read_text())
    assert f"| {root / 'site1' / 'rvtools_sample_a.xlsx'} | DC1 |" in hosts
    assert f"| {root / 'site2' / 'rvtools_sample_a.xlsx'} | DC1 |" in hosts


def test_no_input_is_a_usage_error(run, monkeypatch, capsys):
    monkeypatch.delenv("RVTOOLS_INPUT_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2
    assert "--input-dir" in capsys.readouterr().err


def test_input_dir_must_be_a_folder(run, sample_files):
    with pytest.raises(SystemExit):
        run("--input-dir", str(sample_files[0]))


def test_report_shows_virtualization_evidence_from_recorded_catalog(run, sample_files, catalog_api):
    import json

    code, out = run("-i", str(sample_files[0].parent))
    assert code == EXIT_OK
    md = next(out.glob("*.md")).read_text()
    evidence = json.loads(md.split("## How certification is checked")[1].split("```json")[1].split("```")[0])
    cert = evidence["certifications"][0]
    assert cert["product"] == "Red Hat OpenShift Container Platform"
    assert {"name": "Virtual Machine (Host)", "supported": True} in cert["granularityData"][0]["features"]
    assert f"/hardware/system/detail/{evidence['id']})" in md
