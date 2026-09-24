import argparse
import logging
from collections.abc import Callable
from pathlib import Path

from jobbot.config import load_companies, load_filters
from jobbot.digest import DigestConfigError, SmtpConfig, build_digest, send_digest
from jobbot.models import Match
from jobbot.pipeline import select_matches
from jobbot.sources import fetch_all
from jobbot.sources.base import make_client
from jobbot.storage import DEFAULT_DB, Status, Storage, StoredJob


def format_yoe(min_yoe: float | None) -> str:
    if min_yoe is None:
        return "-"
    return f"{min_yoe:g}+"


def print_jobs(jobs: list[StoredJob], show_url: bool = False) -> None:
    if not jobs:
        print("(none)")
        return
    print(f"{'id':>4}  {'status':<12} {'score':>5} {'yoe':>5}  {'posted':<10}  company / title")
    for s in jobs:
        posted = s.job.posted_at.date().isoformat() if s.job.posted_at else "?"
        print(
            f"{s.id:>4}  {s.status:<12} {s.score:>5} {format_yoe(s.min_yoe):>5}  {posted:<10}  "
            f"{s.job.company}: {s.job.title}  [{s.job.location}]"
        )
        if show_url:
            print(f"{'':>6}{s.job.url}")
        if s.notes:
            print(f"{'':>6}note: {s.notes}")


def store_and_notify(
    store: Storage,
    matches: list[Match],
    notify: Callable[[list[StoredJob]], None],
    dry_run: bool = False,
) -> list[StoredJob]:
    """Stage new matches, notify, then commit, so a failed send never marks jobs as seen.

    Dry run always rolls back: it shows exactly what a real run would send, and writes nothing.
    """
    new = store.add_new(matches)
    try:
        if new:
            notify(new)
    except BaseException:
        store.rollback()
        raise
    if dry_run:
        store.rollback()
    else:
        store.commit()
    return new


def cmd_run(args: argparse.Namespace) -> int:
    smtp = None
    if not args.dry_run:
        try:
            smtp = SmtpConfig.from_env()  # fail before fetching anything
        except DigestConfigError as exc:
            print(f"error: {exc}")
            return 2

    companies = load_companies()
    cfg = load_filters()
    with make_client() as client:
        fetched = fetch_all(companies, client)
    for name, reason in fetched.failures.items():
        print(f"FAILED {name}: {reason}")
    selected = select_matches(fetched.jobs, cfg)

    def notify(new: list[StoredJob]) -> None:
        digest = build_digest(new)
        if args.html:
            args.html.write_text(digest.html)
        if smtp is None:
            print(f"Subject: {digest.subject}\n\n{digest.text}\n")
        else:
            send_digest(digest, smtp)
            print(f"sent digest to {smtp.to}: {digest.subject}")

    with Storage(args.db) as store:
        new = store_and_notify(store, selected.matches, notify, dry_run=args.dry_run)

    dropped = ", ".join(f"{k} {v}" for k, v in selected.dropped.most_common())
    print(
        f"fetched {len(fetched.jobs)} | duplicates {selected.duplicates} | dropped: {dropped} | "
        f"matches {len(selected.matches)} | new {len(new)}"
        + (" | dry run: nothing saved or sent" if args.dry_run else "")
    )
    if not new:
        print("no new matches; nothing sent")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        print_jobs(store.list_jobs(args.status), show_url=args.urls)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        updated = store.set_status(args.id, args.status, note=args.note)
    if updated is None:
        print(f"no job with id {args.id}")
        return 1
    print_jobs([updated])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch, filter, store new matches, and send the digest")
    run.add_argument(
        "--dry-run", action="store_true", help="print the digest instead of sending; save nothing"
    )
    run.add_argument("--html", type=Path, help="also write the digest HTML to this file")
    run.set_defaults(func=cmd_run)

    statuses = [s.value for s in Status]
    ls = sub.add_parser("list", help="list stored jobs")
    ls.add_argument("--status", choices=statuses, help="only jobs with this status")
    ls.add_argument("--urls", action="store_true", help="show apply links")
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="set a job's status, e.g. `jobbot status 12 applied`")
    st.add_argument("id", type=int)
    st.add_argument("status", choices=statuses)
    st.add_argument("--note", help="replace the job's note")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
