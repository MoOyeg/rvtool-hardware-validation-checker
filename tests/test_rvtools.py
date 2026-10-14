from pathlib import Path

import pytest
from make_fixtures import HEADERS_A, ROWS_A, write_xls, write_xlsx

from rvtools_hw_check.rvtools import (
    Normalizer,
    display_names,
    RVToolsFileError,
    expand_inputs,
    group_platforms,
    load_hosts,
    normalize_header,
)


def test_reads_current_xlsx_layout(sample_files, normalizer):
    hosts = load_hosts(sample_files[0], normalizer)
    assert [h.host for h in hosts] == ["esx01.lab.local", "esx02.lab.local", "esx03.lab.local", "esx04.lab.local"]
    first = hosts[0]
    assert first.source_file == "rvtools_sample_a.xlsx"
    assert (first.vendor, first.model) == ("Dell", "PowerEdge R650")
    assert (first.vendor_raw, first.model_raw) == ("Dell Inc.", "PowerEdge R650")
    assert first.datacenter == "DC1"
    assert first.cluster == "Prod"
    assert first.cpu_count == "2"
    assert first.esx_version.startswith("VMware ESXi 7.0.3")
    assert first.serial == "ABC1234"


def test_reads_older_xls_with_odd_header_case(sample_files, normalizer):
    hosts = load_hosts(sample_files[1], normalizer)
    assert [(h.vendor, h.model) for h in hosts] == [
        ("Dell", "PowerEdge R650"),
        ("Lenovo", "ThinkSystem SR650"),
        ("Acme Corp", "Widget 9000"),
    ]
    assert hosts[1].model_raw == "ThinkSystem SR650 -[7X06CTO1WW]-"
    assert hosts[0].source_file == "rvtools_sample_b.xls"


def test_header_normalization():
    assert normalize_header("  ESX   Version ") == "esx version"
    assert normalize_header(None) == ""


def test_blank_rows_are_skipped(sample_files, normalizer):
    hosts = load_hosts(sample_files[0], normalizer)
    assert all(h.host for h in hosts)


def test_missing_vhost_sheet_is_invalid(tmp_path, normalizer):
    path = write_xlsx(tmp_path / "other.xlsx", HEADERS_A, ROWS_A, sheet="vInfo2")
    with pytest.raises(RVToolsFileError, match="vHost"):
        load_hosts(path, normalizer)


@pytest.mark.parametrize("drop", ["Vendor", "Model"])
def test_missing_required_column_is_invalid(tmp_path, normalizer, drop):
    idx = HEADERS_A.index(drop)
    headers = [h for i, h in enumerate(HEADERS_A) if i != idx]
    rows = [[v for i, v in enumerate(r) if i != idx] for r in ROWS_A]
    path = write_xlsx(tmp_path / "bad.xlsx", headers, rows)
    with pytest.raises(RVToolsFileError, match=drop):
        load_hosts(path, normalizer)


def test_xls_missing_column_is_invalid(tmp_path, normalizer):
    path = write_xls(tmp_path / "bad.xls", ["Host", "Vendor"], [["esx1", "Dell"]])
    with pytest.raises(RVToolsFileError, match="Model"):
        load_hosts(path, normalizer)


def test_non_excel_file_is_invalid(tmp_path, normalizer):
    path = tmp_path / "notes.xlsx"
    path.write_text("not a workbook")
    with pytest.raises(RVToolsFileError, match="Cannot read"):
        load_hosts(path, normalizer)


def test_unsupported_extension(tmp_path, normalizer):
    path = tmp_path / "export.csv"
    path.write_text("Host,Vendor,Model\n")
    with pytest.raises(RVToolsFileError, match="Unsupported"):
        load_hosts(path, normalizer)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Dell Inc.", "Dell"),
        ("DELL", "Dell"),
        ("Dell Technologies", "Dell"),
        ("HPE", "HPE"),
        ("HP", "HPE"),
        ("Hewlett Packard Enterprise", "HPE"),
        ("Hewlett-Packard", "HPE"),
        ("Cisco Systems Inc", "Cisco"),
        ("LENOVO", "Lenovo"),
        ("Supermicro", "Supermicro"),
        ("Super Micro Computer Inc.", "Supermicro"),
        ("Acme  Corp", "Acme Corp"),
        ("", ""),
    ],
)
def test_vendor_normalization(normalizer, raw, expected):
    assert normalizer.vendor(raw) == expected


@pytest.mark.parametrize(
    "vendor, raw, expected",
    [
        ("Dell", "  PowerEdge   R650 ", "PowerEdge R650"),
        ("Lenovo", "ThinkSystem SR650 -[7X06CTO1WW]-", "ThinkSystem SR650"),
        ("Cisco", "UCSC-C220-M5SX", "UCS C220 M5SX"),
        ("Cisco", "UCSB-B200-M5", "UCS B200 M5"),
        ("Dell", "PowerEdge R650-xs", "PowerEdge R650-xs"),
    ],
)
def test_model_normalization(normalizer, vendor, raw, expected):
    assert normalizer.model(vendor, raw) == expected


def test_custom_alias_file(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text("vendors:\n  Acme:\n    - acme corp\nmodel_rewrites: []\n")
    normalizer = Normalizer.from_file(path)
    assert normalizer.vendor("ACME Corp.") == "Acme"
    assert normalizer.vendor("Dell Inc.") == "Dell Inc."


def test_deduplicates_across_files(sample_files, normalizer):
    hosts = load_hosts(sample_files[0], normalizer) + load_hosts(sample_files[1], normalizer)
    platforms = group_platforms(hosts)
    keys = [p.key for p in platforms]
    assert len(keys) == len(set(keys)) == 5
    dell = next(p for p in platforms if p.key == ("Dell", "PowerEdge R650"))
    assert dell.host_count == 3
    assert dell.files == {"rvtools_sample_a.xlsx": 2, "rvtools_sample_b.xls": 1}


def test_expand_inputs_handles_dirs_globs_and_missing(sample_files, tmp_path):
    directory = sample_files[0].parent
    (directory / "~$rvtools_sample_a.xlsx").write_text("lock file")
    (directory / "readme.txt").write_text("x")

    files, errors = expand_inputs([str(directory)])
    assert sorted(f.name for f in files) == ["rvtools_sample_a.xlsx", "rvtools_sample_b.xls"]
    assert errors == []

    files, errors = expand_inputs([str(directory / "*.xls*"), str(sample_files[0]), str(tmp_path / "nope.xlsx")])
    assert sorted(f.name for f in files) == ["rvtools_sample_a.xlsx", "rvtools_sample_b.xls"]
    assert errors == [(str(tmp_path / "nope.xlsx"), "File not found")]

    empty = tmp_path / "empty"
    empty.mkdir()
    assert expand_inputs([str(empty)])[1][0][1].startswith("Directory contains no")


def test_committed_sample_files_match_generator(normalizer):
    fixtures = Path(__file__).parent / "fixtures"
    for name in ("rvtools_sample_a.xlsx", "rvtools_sample_b.xls"):
        assert load_hosts(fixtures / name, normalizer)


def test_expand_inputs_recursive(tmp_path):
    from make_fixtures import make_samples

    make_samples(tmp_path / "a" / "nested")
    assert expand_inputs([str(tmp_path / "a")])[0] == []
    files, errors = expand_inputs([str(tmp_path / "a")], recursive=True)
    assert sorted(f.name for f in files) == ["rvtools_sample_a.xlsx", "rvtools_sample_b.xls"]
    assert errors == []


def test_display_names_disambiguate_duplicates(tmp_path):
    one, two, other = tmp_path / "x" / "r.xlsx", tmp_path / "y" / "r.xlsx", tmp_path / "z.xlsx"
    names = display_names([one, two, other])
    assert names == {one: str(one), two: str(two), other: "z.xlsx"}


def test_platform_datacenters(sample_files, normalizer):
    hosts = load_hosts(sample_files[0], normalizer) + load_hosts(sample_files[1], normalizer)
    dell = next(p for p in group_platforms(hosts) if p.vendor == "Dell")
    assert dell.datacenters == {"DC-East": 1, "DC1": 2}
