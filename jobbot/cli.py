import argparse
import logging

from jobbot.config import load_companies
from jobbot.sources import fetch_all
from jobbot.sources.base import make_client


def cmd_run(args: argparse.Namespace) -> int:
    companies = load_companies()
    with make_client() as client:
        result = fetch_all(companies, client)
    for name, count in result.counts.items():
        print(f"{name:<12} {count:>4} jobs")
    for name, reason in result.failures.items():
        print(f"{name:<12} FAILED  {reason}")
    print(f"total        {len(result.jobs):>4} jobs")
    # Filtering, scoring, storage, and the digest land in later phases.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="fetch, filter, store, and send the digest").set_defaults(
        func=cmd_run
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
