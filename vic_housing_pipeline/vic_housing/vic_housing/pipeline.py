"""
pipeline.py — CLI orchestrator for the vic_housing pipeline.

USAGE:
    python -m vic_housing.pipeline              # run all connectors + export
    python -m vic_housing.pipeline --only vgv   # single connector
    python -m vic_housing.pipeline --only rba abs  # multiple
    python -m vic_housing.pipeline --skip asx   # exclude one
    python -m vic_housing.pipeline --export-only   # export without fetching
    python -m vic_housing.pipeline --no-export     # fetch without exporting

DESIGN:
    - Each connector is isolated; failure in one doesn't kill the rest
    - All connectors are idempotent (INSERT OR IGNORE)
    - Every run is logged to the pipeline_runs table for observability
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from . import vgv, rental, exports
from . import abs as abs_mod
from . import rba, asx
from .core import init_db, get_conn, get_logger

log = get_logger("pipeline")

CONNECTORS: dict[str, Callable[[], int]] = {
    "vgv":    vgv.run,
    "rental": rental.run,
    "abs":    abs_mod.run,
    "rba":    rba.run,
    "asx":    asx.run,
}


def _log_run(connector: str, status: str, new_rows: int,
             elapsed: float, error_msg: str | None = None) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_runs
                   (connector, status, new_rows, elapsed_s, error_msg)
                   VALUES (?, ?, ?, ?, ?)""",
                (connector, status, new_rows, round(elapsed, 2), error_msg),
            )
    except Exception:
        pass  # don't crash over observability


def run_pipeline(only: list[str] | None = None,
                 skip: list[str] | None = None,
                 export: bool = True) -> None:
    init_db()

    chosen = list(CONNECTORS.keys())
    if only:
        chosen = [c for c in chosen if c in only]
    if skip:
        chosen = [c for c in chosen if c not in skip]

    log.info("=" * 60)
    log.info(f"vic_housing pipeline — connectors: {', '.join(chosen)}")
    log.info("=" * 60)

    summary: dict[str, tuple[str, int, float]] = {}

    for name in chosen:
        start = time.time()
        try:
            log.info(f"--- {name.upper()} starting ---")
            new_rows = CONNECTORS[name]()
            elapsed  = time.time() - start
            summary[name] = ("ok", new_rows, elapsed)
            _log_run(name, "ok", new_rows, elapsed)
        except Exception as e:
            elapsed = time.time() - start
            log.error(f"--- {name.upper()} FAILED: {e} ---")
            summary[name] = ("fail", 0, elapsed)
            _log_run(name, "fail", 0, elapsed, str(e))

    log.info("=" * 60)
    log.info("PIPELINE SUMMARY")
    log.info("=" * 60)
    for name, (status, count, elapsed) in summary.items():
        status_icon = "✓" if status == "ok" else "✗"
        log.info(f"  {status_icon} {name:<10} {status:<6}  {count:>8,} new rows   ({elapsed:.1f}s)")
    log.info("=" * 60)

    if export:
        log.info("--- EXPORT starting ---")
        try:
            exports.run()
        except Exception as e:
            log.error(f"Export failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Victorian Housing Data Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--only", nargs="+", choices=list(CONNECTORS),
        metavar="CONNECTOR",
        help="Run only these connectors (space-separated)",
    )
    parser.add_argument(
        "--skip", nargs="+", choices=list(CONNECTORS),
        metavar="CONNECTOR",
        help="Skip these connectors",
    )
    parser.add_argument(
        "--no-export", action="store_true",
        help="Skip the CSV/Excel export step",
    )
    parser.add_argument(
        "--export-only", action="store_true",
        help="Skip all fetching; re-export from existing database only",
    )
    args = parser.parse_args()

    if args.export_only:
        init_db()
        exports.run()
        return

    run_pipeline(
        only=args.only,
        skip=args.skip,
        export=not args.no_export,
    )


if __name__ == "__main__":
    sys.exit(main())
