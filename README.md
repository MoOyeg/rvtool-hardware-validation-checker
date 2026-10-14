# RVTools Hardware Validation Checker

`rvtools-hw-check` reads one or more [RVTools](https://www.robware.net/rvtools/) exports, pulls out
the ESXi host hardware (vendor and model), and checks each platform against the
[Red Hat Ecosystem Catalog](https://catalog.redhat.com/hardware). For every platform it reports:

- whether it is certified for **Red Hat OpenShift Container Platform**, and for which versions
- whether it is certified for **Red Hat OpenShift Virtualization**, and for which versions
- a link to the platform's hardware detail page in the catalog

It writes a dated report in Markdown, PDF, or both. See [spec](spec) for the full specification.

## Install

Requires Python 3.11+. PDF output uses [WeasyPrint](https://weasyprint.org/), which needs the Pango
system library (`dnf install pango` on RHEL/Fedora, `apt install libpango-1.0-0 libpangoft2-1.0-0` on Debian/Ubuntu).

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .            # or: pip install -e '.[test]' for development
```

## Getting a Red Hat API token

The app needs a Red Hat **offline token**. It exchanges this token with Red Hat SSO for short-lived
access tokens.

- Generate a token: <https://access.redhat.com/management/api>
- How-to guide (generating an offline token and exchanging it for an access token):
  <https://access.redhat.com/articles/3626371>

Steps:

1. Sign in at <https://access.redhat.com/management/api> with your Red Hat account.
2. Click **Generate token** and copy the token. It is only shown once.
3. Make it available to the app in one of two ways:
   - Export it: `export RH_OFFLINE_TOKEN='<token>'`. Use `--token-env NAME` to read a different variable.
   - Or run the app interactively and paste it when prompted. Your input is hidden.

Offline tokens expire after 30 days without use. If you see an authentication error, generate a new
token from the same page.

The token stays in memory only. It is never logged or written to a report or cache file.

## Usage

```bash
rvtools-hw-check [OPTIONS] [FILE ...]
```

The easiest way to run it is to put all your RVTools exports in one folder and point the app at that
folder. It reads every `.xlsx` and `.xls` file in it, so you don't have to list files one at a time:

```bash
rvtools-hw-check --input-dir ./rvtools-exports -f all
# or set it once
export RVTOOLS_INPUT_DIR=./rvtools-exports
rvtools-hw-check -f all
```

`FILE` can also be individual `.xlsx` or `.xls` files, globs, or folders, and these can be combined
with `--input-dir`. Excel lock files (`~$...`) are skipped.

| Option | Default | Description |
|---|---|---|
| `-i, --input-dir DIR` | `$RVTOOLS_INPUT_DIR` | Folder of RVTools exports; every `.xlsx`/`.xls` in it is read. Repeatable |
| `-r, --recursive` | off | Also read exports in subfolders of the input folders |
| `-o, --output-dir DIR` | `./reports` | Where to write reports |
| `-f, --format FMT` | `md` | `md`, `pdf`, or `all` |
| `--token-env NAME` | `RH_OFFLINE_TOKEN` | Env var holding the offline token |
| `--no-cache` | off | Do not use the local lookup cache |
| `--cache-dir DIR` | `~/.cache/rvtools-hw-check` | Cache location (7-day TTL) |
| `--rate-limit N` | `2` | Max catalog requests per second (max `5`) |
| `--aliases FILE` | built-in | Custom vendor alias / model rewrite file |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Examples:

```bash
# Every export in a folder, both report formats
rvtools-hw-check -i ./exports -f all

# A folder with one subfolder per site
rvtools-hw-check -i ./exports --recursive -o ./out

# Specific files
rvtools-hw-check site-a.xlsx site-b.xls

# Try it with the bundled samples
rvtools-hw-check -i tests/fixtures
```

Exit codes: `0` success, `1` fatal error (authentication failure or no valid input), `2` finished
but some files were skipped or some lookups failed.

## The report

Reports are named `hw-validation-report-YYYYMMDD-HHMMSS.md` / `.pdf`, using the run time in UTC.
Each report has these sections:

- **Summary**: run date, app version, input files with host and platform counts, and totals by status
- **Platforms**: one row per unique vendor and model, with OpenShift and OpenShift Virtualization status, versions, match quality, host count, **Datacenters** and **RVTools files** (each with a host count), and **catalog link(s)**
- **Hosts**: one row per ESXi host, with the **RVTools file** it came from, its **Datacenter**, cluster, hardware, its platform's status, and the catalog link
- **Errors**: skipped files and failed lookups

How to read the statuses:

| Value | Meaning |
|---|---|
| `Yes` / `No` | The catalog lookup succeeded and the platform is / is not certified |
| `Unknown` | The lookup failed (network/API error). This does **not** mean "not certified" |
| Match `Exact` | The vendor and model match a catalog listing exactly |
| Match `Partial (review)` | Close candidates were found (for example `Gen10` vs `Gen10 Plus`). Check them with the links |
| Match `None` | Nothing found. The link opens a pre-filled catalog search |

Version ranges are shown as the catalog publishes them. For example, `4.13-4.x` means 4.13 and later.
OpenShift Virtualization is marked certified when the OpenShift certification includes the
*Virtual Machine (Host)* feature. If a detail page can't be read, versions fall back to the major
version, for example `4.x (major only; see catalog)`.

## Being gentle with the Red Hat API

- Each unique vendor and model is looked up once per run, however many hosts or files share it.
  Results are then cached for 7 days.
- Requests are rate limited for the whole client: 2 per second by default and never more than 5,
  with at most 2 in flight at a time.
- `429` and `503` responses honour `Retry-After`. Other failures back off exponentially with jitter
  (1 s, 2 s, 4 s, and so on, capped at 60 s, at most 5 retries).
- After 5 consecutive failures the app stops calling the API and marks the remaining platforms `Unknown`.

## Vendor and model normalization

RVTools reports vendor strings such as `Dell Inc.` or `Cisco Systems Inc`, and models such as
`ThinkSystem SR650 -[7X06CTO1WW]-` or `UCSC-C220-M5SX`. These are normalized using
[`vendor_aliases.yaml`](src/rvtools_hw_check/config/vendor_aliases.yaml). To add aliases or model
rewrites, copy that file and pass it with `--aliases`.

## Container

```bash
podman build -t rvtools-hw-check -f Containerfile .
podman run --rm -it -e RH_OFFLINE_TOKEN -v "$PWD:/data:Z" rvtools-hw-check -i /data/exports -o /data/reports -f all
```

## Development

```bash
pip install -e '.[test]'
pytest
```

Tests never call live services. Red Hat SSO and the catalog are mocked with recorded responses in
`tests/fixtures/catalog/`. To refresh those recordings (this makes about 11 live requests):

```bash
python tests/fixtures/record_catalog.py
python tests/fixtures/make_fixtures.py   # regenerate the sample RVTools exports
```
