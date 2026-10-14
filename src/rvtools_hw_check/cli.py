"""Command-line entry point: rvtools-hw-check (spec section 3)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .auth import DEFAULT_TOKEN_ENV, TOKEN_GENERATE_URL, TOKEN_HOWTO_URL, AuthError, TokenManager, resolve_offline_token
from .cache import Cache, default_cache_dir
from .catalog import YES, CatalogClient
from .ratelimit import DEFAULT_RATE, MAX_IN_FLIGHT, MAX_RATE, PoliteSession
from .report_md import FileSummary, RunReport, write_markdown
from .rvtools import Normalizer, RVToolsFileError, display_names, expand_inputs, group_platforms, load_hosts

logger = logging.getLogger("rvtools_hw_check")

EXIT_OK, EXIT_FATAL, EXIT_PARTIAL = 0, 1, 2
INPUT_DIR_ENV = "RVTOOLS_INPUT_DIR"

EPILOG = f"""\
Input:
  Pass files, globs or folders as FILE, or point --input-dir (or ${INPUT_DIR_ENV})
  at a folder to read every .xlsx/.xls export in it.


Red Hat API token:
  An offline token is required. It is read from ${DEFAULT_TOKEN_ENV} (or the
  variable named by --token-env); otherwise you are prompted for it.
    Generate a token: {TOKEN_GENERATE_URL}
    How-to guide:     {TOKEN_HOWTO_URL}

Exit codes: 0 success, 1 fatal error, 2 completed with some file or lookup errors.
"""


def _rate(value: str) -> float:
    try:
        rate = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 < rate <= MAX_RATE:
        raise argparse.ArgumentTypeError(f"must be greater than 0 and at most {MAX_RATE:g}")
    return rate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rvtools-hw-check",
        description=(
            "Check the ESXi host hardware in RVTools exports against the Red Hat Ecosystem Catalog "
            "for OpenShift and OpenShift Virtualization certification."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files", nargs="*", metavar="FILE", help="RVTools exports (.xlsx/.xls), globs, or directories (optional with --input-dir)"
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=f"folder of RVTools exports; every .xlsx/.xls in it is read. Repeatable. (default: ${INPUT_DIR_ENV} if set)",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="also read exports in subfolders of input folders")
    parser.add_argument("-o", "--output-dir", default="reports", help="where to write reports (default: ./reports)")
    parser.add_argument("-f", "--format", choices=["md", "pdf", "all"], default="md", help="report format (default: md)")
    parser.add_argument(
        "--token-env", default=DEFAULT_TOKEN_ENV, help=f"env var holding the offline token (default: {DEFAULT_TOKEN_ENV})"
    )
    parser.add_argument("--no-cache", action="store_true", help="do not use the local catalog lookup cache")
    parser.add_argument("--cache-dir", default=None, help=f"cache directory (default: {default_cache_dir()})")
    parser.add_argument(
        "--rate-limit",
        type=_rate,
        default=DEFAULT_RATE,
        metavar="N",
        help=f"max catalog API requests per second (default: {DEFAULT_RATE:g}, max: {MAX_RATE:g})",
    )
    parser.add_argument("--aliases", default=None, metavar="FILE", help="custom vendor_aliases.yaml")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO", help="logging level (default: INFO)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def setup_logging(level: str) -> None:
    root = logging.getLogger("rvtools_hw_check")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)


def print_summary(report: RunReport, written: list[Path]) -> None:
    print()
    print(f"Hardware validation run {report.run_date_iso}")
    for label, value in report.counts().items():
        print(f"  {label:<42} {value}")
    if report.results:
        print()
        width = max(len(f"{r.platform.vendor} {r.platform.model}") for r in report.results)
        print(f"  {'Platform':<{width}}  {'OCP':<7}  {'OCP-V':<7}  Match")
        for r in report.results:
            name = f"{r.platform.vendor} {r.platform.model}"
            print(f"  {name:<{width}}  {r.ocp_certified:<7}  {r.ocp_virt_certified:<7}  {r.match_confidence}")
    print()
    for path in written:
        print(f"Report written: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_dirs = args.input_dir or ([os.environ[INPUT_DIR_ENV]] if os.environ.get(INPUT_DIR_ENV) else [])
    if not args.files and not input_dirs:
        parser.error(f"give at least one FILE, or a folder with --input-dir / ${INPUT_DIR_ENV}")
    for directory in input_dirs:
        if not Path(directory).is_dir():
            parser.error(f"--input-dir {directory!r} is not a folder")
    setup_logging(args.log_level)
    run_date = datetime.now(timezone.utc).replace(microsecond=0)  # one timestamp for the whole run (FR-5.1)
    logger.info("Starting hardware validation run %s", run_date.isoformat())

    try:
        normalizer = Normalizer.from_file(args.aliases)
    except (OSError, ValueError) as exc:
        logger.error("Cannot load vendor aliases: %s", exc)
        return EXIT_FATAL

    paths, errors = expand_inputs([*input_dirs, *args.files], recursive=args.recursive)
    logger.info("Found %d RVTools file(s) to read", len(paths))
    names = display_names(paths)
    files: list[FileSummary] = [FileSummary(name=item, error=reason) for item, reason in errors]
    hosts = []
    for path in paths:
        name = names[path]
        try:
            file_hosts = load_hosts(path, normalizer, name)
        except RVToolsFileError as exc:
            logger.warning("Skipping %s: %s", path, exc)
            files.append(FileSummary(name=name, error=str(exc)))
            errors.append((name, str(exc)))
            continue
        files.append(FileSummary(name=name, hosts=len(file_hosts), platforms=len({h.platform_key for h in file_hosts})))
        hosts.extend(file_hosts)

    if not any(f.error == "" for f in files):
        logger.error("No valid RVTools file could be processed")
        return EXIT_FATAL

    platforms = group_platforms(hosts)
    logger.info("Found %d host(s) and %d unique platform(s)", len(hosts), len(platforms))

    try:
        token_manager = TokenManager(resolve_offline_token(args.token_env))
        token_manager.access_token()  # fail fast on a bad token
    except AuthError as exc:
        logger.error("%s", exc)
        return EXIT_FATAL

    http = PoliteSession(rate=args.rate_limit, max_in_flight=MAX_IN_FLIGHT)
    cache = Cache(args.cache_dir, enabled=not args.no_cache)
    client = CatalogClient(http, normalizer, cache=cache, token_manager=token_manager, workers=MAX_IN_FLIGHT)
    results = client.lookup_all(platforms)
    logger.info("Catalog requests sent: %d", http.request_count)

    report = RunReport(run_date=run_date, files=files, results=results, hosts=hosts, errors=errors)
    output_dir = Path(args.output_dir)
    written = []
    if args.format in ("md", "all"):
        written.append(write_markdown(report, output_dir))
    if args.format in ("pdf", "all"):
        from .report_pdf import write_pdf

        written.append(write_pdf(report, output_dir))

    print_summary(report, written)
    failed = errors or any(r.error for r in results)
    certified = sum(r.ocp_certified == YES for r in results)
    logger.info("Done: %d of %d platform(s) certified for OpenShift", certified, len(results))
    return EXIT_PARTIAL if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
