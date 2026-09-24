import argparse
import logging

from jobbot.config import load_companies, load_filters
from jobbot.models import Match
from jobbot.pipeline import select_matches
from jobbot.sources import fetch_all
from jobbot.sources.base import make_client


def format_yoe(min_yoe: float | None) -> str:
    if min_yoe is None:
        return "-"
    return f"{min_yoe:g}+"


def print_matches(matches: list[Match]) -> None:
    for m in matches:
        posted = m.job.posted_at.date().isoformat() if m.job.posted_at else "?"
        print(
            f"{m.score:>3}  {format_yoe(m.min_yoe):>5}  {posted}  {m.job.company:<8} "
            f"{m.job.title}  [{m.job.location}]"
        )


def cmd_run(args: argparse.Namespace) -> int:
    companies = load_companies()
    cfg = load_filters()
    with make_client() as client:
        fetched = fetch_all(companies, client)
    for name, reason in fetched.failures.items():
        print(f"FAILED {name}: {reason}")

    selected = select_matches(fetched.jobs, cfg)
    dropped = ", ".join(f"{k} {v}" for k, v in selected.dropped.most_common())
    print(
        f"fetched {len(fetched.jobs)} | duplicates {selected.duplicates} | "
        f"dropped: {dropped} | matches {len(selected.matches)}\n"
    )
    print("score   yoe  posted      company  title  [location]")
    print_matches(selected.matches)
    # Storage and the digest land in later phases.
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
