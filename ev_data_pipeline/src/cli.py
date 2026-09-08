"""CLI entrypoint: python -m src.cli {fetch-stations,generate-sessions}."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Stage 0 data pipeline for the EV charging analytics project.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=config.SEED,
        help="Global RNG seed (default %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser(
        "fetch-stations", help="Fetch charging stations from Open Charge Map"
    )
    fetch.add_argument(
        "--seed",
        type=int,
        default=config.SEED,
        help="Global RNG seed (default %(default)s)",
    )
    fetch.add_argument("--country", default="PL", help="ISO country code (default: PL)")
    fetch.add_argument(
        "--limit", type=int, default=200, help="Max stations to fetch (default: 200)"
    )
    fetch.add_argument("--out", default="data/stations.csv", help="Output CSV path")

    gen = sub.add_parser(
        "generate-sessions", help="Generate synthetic charging sessions"
    )
    gen.add_argument(
        "--stations", default=None, help="Path to stations.csv from fetch-stations"
    )
    gen.add_argument(
        "--seed",
        type=int,
        default=config.SEED,
        help="Global RNG seed (default %(default)s)",
    )
    gen.add_argument(
        "--synthetic-stations",
        type=int,
        default=0,
        help="Generate N fake stations instead of reading --stations",
    )
    gen.add_argument(
        "--rows",
        type=int,
        default=config.DEFAULT_ROWS,
        help="Number of sessions (default: 80000)",
    )
    gen.add_argument(
        "--start", default=config.DEFAULT_START, help="Range start YYYY-MM-DD"
    )
    gen.add_argument(
        "--end", default=config.DEFAULT_END, help="Range end YYYY-MM-DD (inclusive)"
    )
    gen.add_argument(
        "--anomaly-rate",
        type=float,
        default=config.DEFAULT_ANOMALY_RATE,
        help="Total anomaly share (default: 0.03)",
    )
    gen.add_argument("--out", default="data/sessions.csv", help="Output CSV path")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    seed = args.seed

    if args.command == "fetch-stations":
        from .stations import run_fetch

        run_fetch(args.country, args.limit, args.out, seed)
        return 0

    if args.command == "generate-sessions":
        from .generator import run_generate

        run_generate(
            stations_path=args.stations,
            synthetic_stations=args.synthetic_stations,
            n_rows=args.rows,
            start=args.start,
            end=args.end,
            anomaly_rate=args.anomaly_rate,
            seed=seed,
            out_path=args.out,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        sys.exit(1)
