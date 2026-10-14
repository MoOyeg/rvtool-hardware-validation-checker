# RVTools Hardware Validation Checker — Specification

Status: Implemented (v0.1.0)
Date: 2026-10-14

## 1. Purpose

Customers moving from VMware vSphere to OpenShift need to know whether the
physical servers they already own are certified by Red Hat. The app reads one
or more RVTools exports, pulls out the ESXi host hardware platforms
(vendor + model), and looks up each platform in the Red Hat Ecosystem Catalog.
For each one, it reports whether the platform is certified for:

- Red Hat OpenShift Container Platform (OCP)
- Red Hat OpenShift Virtualization (OCP-V)

and which product versions the certification covers.

## 2. Scope

### In scope
- Parsing RVTools exports in `.xlsx` (current RVTools) and `.xls` (older RVTools)
- Processing several input files in one run
- Authenticating to Red Hat APIs with a user-supplied offline token
- Querying the Red Hat Ecosystem Catalog for hardware certifications
- Writing a dated report

### Out of scope (v1)
- Validating individual components (NICs, HBAs, GPUs)
- Sizing or capacity planning
- Any changes to vCenter or to an OpenShift cluster

## 3. Users and Usage

The primary user is a Red Hat SA/consultant or a customer infrastructure
engineer. The app runs as a CLI on the user's workstation or in a container.

```
rvtools-hw-check [OPTIONS] [FILE ...]

  FILE                  RVTools exports (.xlsx / .xls). Globs and directories
                        are accepted; directories are scanned for *.xlsx and
                        *.xls. Optional when an input folder is given.

Options:
  -i, --input-dir DIR   Folder of RVTools exports; every .xlsx/.xls in it is
                        read (default: $RVTOOLS_INPUT_DIR). Repeatable.
  -r, --recursive       Also read exports in subfolders of input folders
  -o, --output-dir DIR  Where to write reports (default: ./reports)
  -f, --format FMT      md | pdf | all (default: md)
  --token-env NAME      Env var holding the offline token (default: RH_OFFLINE_TOKEN)
  --no-cache            Do not use the local catalog lookup cache
  --cache-dir DIR       Cache location (default: ~/.cache/rvtools-hw-check)
  --rate-limit N        Max catalog API requests per second (default: 2, max: 5)
  --aliases FILE        Custom vendor alias / model rewrite file
  --log-level LEVEL     DEBUG | INFO | WARNING | ERROR (default: INFO)
  --version
  -h, --help
```

## 4. Functional Requirements

### FR-1 Input files
- FR-1.1 Accept one or more file paths, globs, or directories.
- FR-1.1a **Input folder.** The user can point the app at a folder of
  RVTools exports with `--input-dir` or the `RVTOOLS_INPUT_DIR` environment
  variable. The app reads every `.xlsx`/`.xls` file in the folder in one run,
  so files don't have to be listed one at a time. `--recursive` includes
  subfolders. Excel lock files (`~$*`) are skipped. When two files found
  this way share a name, the report labels them by path so the source of
  every host stays unambiguous. If no FILE and no input folder is given, the
  app exits with a usage error.
- FR-1.2 Support `.xlsx` (via `openpyxl`) and `.xls` (via `xlrd`).
- FR-1.3 Skip unreadable or non-RVTools files with a warning and keep going
  with the remaining files. Exit non-zero only if no file could be processed.
- FR-1.4 Record the source file name for every host row so the report can be
  traced back to its source file.

### FR-2 Hardware extraction
- FR-2.1 Read the `vHost` sheet. Required columns: `Host`, `Vendor`, `Model`.
  Optional columns: `Datacenter`, `Cluster`, `CPU Model`, `# CPU`,
  `Cores per CPU`, `# Memory`, `ESX Version`, `Service tag`/`Serial number`.
- FR-2.2 Match column names case-insensitively and ignore extra whitespace,
  because column headers vary between RVTools versions.
- FR-2.3 If `vHost` is missing or lacks `Vendor` or `Model`, flag the file as
  invalid (see FR-1.3).
- FR-2.4 Normalize vendor names (for example `Dell Inc.` → `Dell`,
  `HPE`/`Hewlett Packard Enterprise`/`HP` → `HPE`, `Cisco Systems Inc` →
  `Cisco`, `Lenovo`, `Supermicro`) using a mapping table that can be edited
  in `src/rvtools_hw_check/config/vendor_aliases.yaml`. The file ships with
  the package and can be overridden with `--aliases`.
- FR-2.5 Normalize model strings by trimming whitespace and collapsing
  repeated spaces, while keeping the original value for the report. Then
  apply the regex rewrites from the same alias file, for example removing
  Lenovo's machine-type suffix (`ThinkSystem SR650 -[7X06CTO1WW]-` →
  `ThinkSystem SR650`) or turning Cisco PIDs into catalog names
  (`UCSC-C220-M5SX` → `UCS C220 M5SX`).
- FR-2.6 Deduplicate on `(normalized vendor, normalized model)` across all
  input files. Each unique platform is looked up only once, and the host
  count and host list are kept for each file.

### FR-3 Authentication
- FR-3.1 The app needs a Red Hat offline (refresh) token.
  - Generate a token: https://access.redhat.com/management/api
  - How-to guide (generating an offline token and exchanging it for an
    access token): https://access.redhat.com/articles/3626371
  Both links must appear in:
  - `README.md`, in a "Getting a Red Hat API token" section with
    step-by-step instructions
  - the `--help` text
  - the interactive token prompt (FR-3.2)
  - every authentication error message (FR-3.6), so a user with a missing,
    expired, or revoked token always knows how to get a new one
- FR-3.2 The app resolves the token in this order:
  1. The environment variable named by `--token-env` (default `RH_OFFLINE_TOKEN`)
  2. **An interactive prompt** that hides input (`getpass`) when stdin is a
     TTY. The prompt shows the FR-3.1 links for generating a token.
  3. If neither yields a token, exit with a clear error.
- FR-3.3 Exchange the offline token for an access token. This uses the same
  flow as the reference implementation
  (https://github.com/MoOyeg/simple-ocp-costmgmt-script/blob/main/app/app.py):
  ```
  POST https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
  grant_type=refresh_token
  client_id=rhsm-api
  refresh_token=<offline token>
  ```
  Send the result as `Authorization: Bearer <access_token>` on catalog
  requests.
- FR-3.4 Refresh the access token automatically when it expires (it lasts
  about 15 minutes) or when a request returns `401`.
- FR-3.5 Never log, print, or write either token to a report or cache file.
- FR-3.6 Handle SSO errors as the reference script does: if the response
  contains `error`/`error_description`, log the message and exit non-zero.

### FR-4 Catalog lookup
- FR-4.1 For each unique platform, search the Red Hat Ecosystem Catalog's
  certified hardware (system) listings by vendor and model. The app uses the
  catalog's own search endpoint, which is the one the catalog website calls:
  ```
  GET https://catalog.redhat.com/api/solr?type=search&redhat_client=ecosystem-catalog
      &q=<vendor model>&fq=documentKind:CertifiedHardware&fq=certified_category:System
      &fl=<only the fields used>&rows=100
  ```
  If `vendor model` finds no candidates, the app retries once with the model
  alone.
- FR-4.2 For each matched listing, fetch its public hardware detail page
  (`https://catalog.redhat.com/en/hardware/system/detail/<id>`). The page
  embeds every certification as JSON, with `product`, `startVersion`,
  `endVersion`, `architecture` and the certified features. From it, extract:
  - Catalog listing ID and hardware detail URL (see FR-4.7)
  - Certified vendor/make and model name
  - Each certified product and its version range, for example OCP
    `4.8-4.12` and `4.13-4.x`
  - Architecture
  - **RHEL:** the version ranges of every `Red Hat Enterprise Linux`
    certification (RHEL for Real Time is excluded), sorted numerically, for
    example `6.9-6.x, 7.3-7.x, 8.0-8.x, 9.0-9.x, 10.0-10.x`.
  - **OpenShift Virtualization:** the catalog has no separate product for
    it. A platform counts as certified for OpenShift Virtualization when an
    OpenShift Container Platform certification lists the
    `Virtual Machine (Host)` compute feature as supported. The OCP-V
    versions are those certifications' version ranges.
  - If the detail page can't be fetched or parsed, fall back to the search
    data (`certified_products`, `certified_versions`,
    `certified_granularity_compute`). That data only gives the major version,
    so the report shows `4.x (major only; see catalog)`.
- FR-4.3 Classify each platform as follows:
  | Field | Values |
  |---|---|
  | `ocp_certified` | `Yes` / `No` / `Unknown` |
  | `ocp_versions` | comma-separated versions, or empty |
  | `ocp_virt_certified` | `Yes` / `No` / `Unknown` |
  | `ocp_virt_versions` | comma-separated versions, or empty |
  | `rhel_versions` | comma-separated RHEL version ranges the hardware is certified for, or empty |
  | `match_confidence` | `Exact` / `Partial` / `None` |
  | `catalog_urls` | hardware detail page link(s) for the matching listings (FR-4.7) |

  `Unknown` means the lookup failed (network or API error). It is never the
  same as `No`.
- FR-4.4 Matching:
  - **Exact**: the normalized vendor and model equal the catalog's make and
    model, ignoring case and punctuation. A leading vendor name is ignored, so
    `PowerEdge R650` matches `Dell PowerEdge R650`.
  - **Partial**: the vendor matches and the models are close by fuzzy match
    (`rapidfuzz` token-set ratio ≥ 90, for example `ProLiant DL380 Gen10` vs
    `ProLiant DL380 Gen10 Plus`). Candidates are ranked by overall similarity
    and the best 3 are kept. The report lists them so a person can confirm.
  - **None**: no candidates found.
- FR-4.5 Requests use `requests` with timeouts (default 30 s). This builds on
  the reference script's `requests_with_error_handling` pattern, but the
  function must always return or raise a defined value. The reference version
  can return an undefined `response` after an exception.
- FR-4.6 Cache lookup results on disk (`~/.cache/rvtools-hw-check/`) keyed by
  normalized vendor+model, with a 7-day TTL by default. Disable the cache with
  `--no-cache`.
- FR-4.7 **Hardware detail link.** For every matched certification, build the
  public catalog detail URL
  `https://catalog.redhat.com/hardware/system/detail/<listing id>` and carry
  it into every output (see FR-5.4). If a platform has several matching
  listings (for example different OCP versions or partial-match candidates),
  include every link. Platforms with no match get a pre-filled catalog search
  link (`https://catalog.redhat.com/search?searchType=hardware&type=System&q=<vendor model>`) instead,
  so a reviewer can check by hand.
- FR-4.8 **Protect the Red Hat API from overload.** The app must be a polite
  client:
  - Look up each unique platform once per run (FR-2.6) and serve repeats
    from the cache (FR-4.6), so API calls scale with the number of unique
    platforms, not hosts.
  - Global client-side rate limit (token bucket) shared by all workers:
    default 2 requests/second, configurable with `--rate-limit`, hard cap of
    5 requests/second.
  - At most 2 requests in flight at once.
  - On `429` or `503`, honor the `Retry-After` header. Otherwise back off
    exponentially with jitter (1 s, 2 s, 4 s, ... capped at 60 s), with up
    to 5 retries. Retry other `5xx` errors the same way, but do not retry
    other `4xx` errors.
  - If 5 consecutive requests fail, stop issuing calls (circuit breaker),
    mark the remaining platforms `Unknown`, and record the reason in the
    report. A request that is already in flight when the breaker opens is
    allowed to finish.
  - Ask for the largest page size the API allows, so fewer requests are
    needed.
  - Send a descriptive `User-Agent` (`rvtools-hw-check/<version>`).
  - Exchange the SSO token once and reuse it until it expires, rather than
    once per request.

### FR-5 Run date and report
- FR-5.1 Record the run timestamp once at start, in UTC ISO-8601, and use the
  same value everywhere in the run.
- FR-5.2 Put the run date in the output file name:
  `hw-validation-report-YYYYMMDD-HHMMSS.<ext>`.
- FR-5.3 Output formats are **Markdown** (`.md`) and **PDF** (`.pdf`). Build
  the Markdown report first, then render the PDF from the same content so the
  two never disagree. Both show the run date in the report header, along with
  the app version and the list of input files.
- FR-5.4 Report sections (the same in both formats):
  1. **Summary**: starts with a short note on **platforms vs hosts**. A
     host is one ESXi server; a platform is a hardware model (vendor + model)
     shared by one or more hosts. Red Hat certifies models, so each platform
     is checked once and its hosts inherit the result. The note includes this
     run's host and platform counts. The Platforms and Hosts sections each
     open with a one-line description. Then: run date, app version, the input files with their per-file
     host and platform counts, and totals by status (certified OCP /
     certified OCP-V / not certified / unknown / needs review)
  2. **Platforms**: a table with one row per unique platform: vendor, model,
     the FR-4.3 fields (including **RHEL versions**), host count, a **Datacenters** column (each
     datacenter's name and host count), an **RVTools files** column (each
     source file and its host count), and a **Catalog link** column with the
     hardware detail link(s) from FR-4.7
  3. **Divider**: in the PDF, a page of its own titled **Hosts** with the
     host and file counts, so the platform results and the host inventory
     are clearly separated. In Markdown it is a rule and a banner.
     **Hosts**: a table with one row per ESXi host. It starts with the
     **RVTools file** the host came from and its **Datacenter**, followed by
     cluster, ESXi host, vendor, model, CPU model and ESX version, then that
     host's platform status and catalog link
  4. **Errors**: files skipped and lookups that failed, with the reason for
     each
  5. **How certification is checked**: a short JSON example of the catalog
     fields the app reads (`product`, `startVersion`, `endVersion`,
     `architecture`, and the Compute `Virtual Machine (Host)` feature with
     `supported`). It is taken from a real exact-match certification in the
     run, preferring one with the VM host feature, and links to that hardware
     page. If the run has none, a clearly labelled illustrative example is
     shown instead. Brief notes explain the OpenShift and OpenShift
     Virtualization rules (FR-4.2) and the major-version fallback.
- FR-5.5 Links must be clickable in both formats: Markdown links
  `[catalog](https://...)` in `.md`, and real hyperlinks in the `.pdf`. Long
  host tables in the PDF wrap or paginate, and must never be cut off.
- FR-5.6 Print a short summary table to stdout at the end of the run.

## 5. Non-Functional Requirements

- **Language/runtime:** Python 3.11+
- **Dependencies:** `requests`, `openpyxl`, `xlrd`, `rapidfuzz` (fuzzy
  matching), `pyyaml`, `markdown` + `weasyprint` (Markdown → HTML → PDF with
  working hyperlinks), `rich` (console output, optional)
- **Logging:** stdout logger with the same format as the reference script
  (`%(asctime)s - %(name)s - %(levelname)s - %(message)s`); level set by
  `--log-level`
- **Security:** tokens are held only in memory, the input files are treated
  as read-only, and output files contain no credentials. RVTools exports can
  hold sensitive inventory data, so reports go only to the local output
  directory.
- **Performance:** a run with 10 files, 2,000 hosts, and around 50 unique
  platforms finishes in under 2 minutes with a cold cache, while staying
  within the FR-4.8 rate limits. Correct behavior toward the API takes
  priority over speed.
- **Packaging:** installable with `pip install .` (console script
  `rvtools-hw-check`) and shipped as a UBI-based container image
  (`Containerfile`).
- **Exit codes:** `0` success, `1` fatal error (auth failure, no valid
  input), `2` completed with some lookup or file errors.

## 6. Layout

```
rvtool-hardware-validation-checker/
├── spec                      # this document
├── README.md
├── pyproject.toml
├── Containerfile
├── src/rvtools_hw_check/
│   ├── __init__.py
│   ├── config/
│   │   └── vendor_aliases.yaml   # vendor aliases + model rewrites (packaged)
│   ├── cli.py                # arg parsing, orchestration, run timestamp
│   ├── auth.py               # token prompt + SSO exchange/refresh
│   ├── rvtools.py            # xls/xlsx reading, vHost extraction, normalization
│   ├── catalog.py            # Red Hat catalog client, matching, classification, detail links
│   ├── ratelimit.py          # token bucket, backoff/Retry-After, circuit breaker
│   ├── cache.py
│   ├── report_md.py          # Markdown report
│   └── report_pdf.py         # PDF rendered from the Markdown report
└── tests/
    ├── conftest.py           # mocked SSO + catalog backed by recorded responses
    ├── fixtures/
    │   ├── catalog/          # recorded, trimmed catalog search + detail responses
    │   ├── make_fixtures.py  # generates the sample RVTools exports
    │   ├── record_catalog.py # re-records catalog responses (manual, live)
    │   ├── rvtools_sample_a.xlsx
    │   └── rvtools_sample_b.xls
    ├── test_rvtools.py
    ├── test_auth.py
    ├── test_catalog.py
    ├── test_ratelimit.py
    ├── test_report.py
    └── test_cli.py           # end-to-end
```

## 7. Testing

- Unit tests for column detection across RVTools versions, vendor/model
  normalization, deduplication across files, and classification logic.
- Auth tests with mocked SSO responses (success, `invalid_grant`, network
  error), plus a test confirming the interactive prompt runs only when no
  env token is set. Assert that the prompt and the auth error messages
  contain the token how-to link.
- Catalog client tests against recorded responses (`responses`), with no
  live calls in CI.
- Rate-limit tests: the request rate never exceeds the configured limit,
  `Retry-After` is honored on `429`, backoff stops after the retry limit, and
  the circuit breaker opens after 5 consecutive failures.
- Link tests: every matched platform has a
  `https://catalog.redhat.com/hardware/system/detail/<id>` link, and
  unmatched platforms get a search link.
- An end-to-end test that feeds two fixture files (one `.xlsx`, one `.xls`)
  and checks that both the `.md` and `.pdf` reports contain the run date,
  every input file, the correct statuses, and clickable catalog links.

## 8. Open Questions / Assumptions

1. **Catalog API endpoint (resolved 2026-10-14).** Red Hat does not
   document a public REST API for certified hardware. The app uses the same
   endpoints the catalog website uses (FR-4.1, FR-4.2), and both work
   anonymously. Because they are undocumented, Red Hat could change them
   without notice. The detail-page parser falls back to search data, and the
   recorded fixtures (`tests/fixtures/record_catalog.py`) make any breakage
   easy to spot. The SSO token flow (FR-3) is still required by the product
   requirements, and the bearer token is sent on catalog API requests.
2. **OpenShift Virtualization (resolved 2026-10-14).** The catalog has no
   separate OpenShift Virtualization product for hardware. It is derived from
   the `Virtual Machine (Host)` feature on the OCP certification (FR-4.2).
   Confirm this rule with the Red Hat certification team.
3. **Version representation (resolved 2026-10-14).** The catalog publishes
   ranges such as `4.13-4.x`, meaning 4.13 and later. The report shows them
   as published.
4. **API rate limits.** Red Hat has not published limits for the catalog
   API. The defaults in FR-4.8 are deliberately conservative, so adjust them
   if Red Hat provides official numbers.
5. **Assumption:** the `vHost` sheet gives enough detail (vendor + model) to
   identify a platform. Component-level checks (NIC/HBA) are deferred.
