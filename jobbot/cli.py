import argparse
import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from jobbot.config import ConfigError, load_companies, load_filters, load_profile
from jobbot.datasync import REPO_ROOT, DataBranch, GitError, Journal, PushConflict, replay
from jobbot.digest import Digest, DigestConfigError, SmtpConfig, send_digest
from jobbot.dispatch import RunState, run_once
from jobbot.pipeline import DetailFetcher, Evaluator, IngestResult
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
        if s.gaps:
            print(f"{'':>6}gaps: {', '.join(s.gaps)}")
        if show_url:
            print(f"{'':>6}{s.job.url}")
        if s.notes:
            print(f"{'':>6}note: {s.notes}")


# --- run ------------------------------------------------------------------------------


def _mailer(args: argparse.Namespace, smtp: SmtpConfig | None) -> Callable[[Digest], None]:
    def send(d: Digest) -> None:
        if args.html:
            args.html.write_text(d.html)
        if smtp is None:
            print(f"Subject: {d.subject}\n\n{d.text}\n")
        else:
            send_digest(d, smtp)
            print(f"sent to {smtp.to}: {d.subject}")

    return send


def _smtp_or_none(args: argparse.Namespace) -> tuple[SmtpConfig | None, int]:
    if args.dry_run:
        return None, 0
    try:
        return SmtpConfig.from_env(), 0  # fail before fetching anything
    except DigestConfigError as exc:
        print(f"error: {exc}")
        return None, 2


def _execute(
    args: argparse.Namespace,
    apply: Callable[[Path, int], None],
) -> tuple[int, object]:
    """Run `apply` against the right DB: temp copy (dry run), data branch, or local file."""
    if args.dry_run:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "jobs.db"
            if args.sync:
                if data_branch(args, copy).fetch() is None:
                    print("error: no data branch yet (see `jobbot sync --seed`)")
                    return 2, None
            elif args.db.exists():
                shutil.copy(args.db, copy)
            apply(copy, 1)
        return 0, None
    if args.sync:
        try:
            return 0, data_branch(args).publish(apply, require_existing=True)
        except (GitError, PushConflict) as exc:
            print(f"error: {exc}")
            return 1, None
    apply(args.db, 1)
    return 0, None


def _print_plan(state: RunState) -> None:
    plan = state.plan
    if plan is None:
        return
    if plan.alerts:
        print(f"hit-list alert: {plan.alerts} job(s)")
    if plan.digest:
        print(f"digest: {plan.digest} job(s)")
    if not plan.emailed:
        print("no new matches; nothing sent")
    if plan.next_digest:
        print(f"next digest window: {plan.next_digest:%a %-I%p} ET")


def cmd_run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    smtp, code = _smtp_or_none(args)
    if code:
        return code
    cfg = load_filters()
    evaluator = Evaluator(cfg, load_profile())
    companies = load_companies()
    priorities = {c.name: c.priority for c in load_companies(include_disabled=True)}
    state = RunState()
    send = _mailer(args, smtp)

    with make_client() as client:
        fetched = fetch_all(companies, client)
        details = DetailFetcher(client)

        def apply(path: Path, attempt: int = 1) -> None:
            run_once(
                path, fetched.listings, evaluator, details, state, send,
                priorities=priorities, searches=cfg.linkedin_searches,
                do_refilter=args.refilter, dry_run=args.dry_run,
            )  # fmt: skip

        code, publish = _execute(args, apply)
        if code:
            return code

    for name, reason in fetched.failures.items():
        print(f"FAILED {name}: {reason}")
    ing = state.ingest or IngestResult()
    dropped = ", ".join(f"{k} {v}" for k, v in ing.dropped.most_common()) or "none"
    print(
        f"listed {len(fetched.jobs)} | new {ing.new} (matched {ing.matched}, reposts "
        f"{ing.reposts}) | dropped: {dropped} | descriptions fetched {details.requests}"
        f" ({details.failures} failed) | pruned {state.pruned}"
    )
    if state.refilter:
        rf = state.refilter
        print(
            f"refilter: checked {rf.checked} | newly matched {rf.newly_matched} | newly "
            f"unmatched {rf.newly_unmatched} | descriptions fetched {rf.descriptions_fetched}"
        )
    _print_plan(state)
    if publish:
        print(f"data branch: {publish.outcome} (attempt {publish.attempts})")
    if args.dry_run:
        print("dry run: nothing saved, sent, or pushed")
    print(f"took {time.monotonic() - started:.1f}s")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Send everything pending now, in one email with the hit list on top."""
    smtp, code = _smtp_or_none(args)
    if code:
        return code
    cfg = load_filters()
    priorities = {c.name: c.priority for c in load_companies(include_disabled=True)}
    state = RunState()
    send = _mailer(args, smtp)

    def apply(path: Path, attempt: int = 1) -> None:
        run_once(
            path, [], Evaluator(cfg), DetailFetcher(None), state, send,
            priorities=priorities, searches=cfg.linkedin_searches,
            dry_run=args.dry_run, force_digest=True,
        )  # fmt: skip

    code, publish = _execute(args, apply)
    if code:
        return code
    _print_plan(state)
    if publish:
        print(f"data branch: {publish.outcome} (attempt {publish.attempts})")
    return 0


# --- verify ---------------------------------------------------------------------------


def cmd_verify_companies(args: argparse.Namespace) -> int:
    """Report each enabled board as error, empty, or OK. Never edits companies.yaml."""
    companies = load_companies()
    with make_client() as client:
        result = fetch_all(companies, client)

    def label(c) -> str:
        return f"{c.name} [{c.ats}/{c.slug}]"

    errors = [c for c in companies if c.name in result.failures]
    empty = [c for c in companies if result.counts.get(c.name) == 0]
    ok = [c for c in companies if result.counts.get(c.name, 0) > 0]

    print(f"ERROR ({len(errors)}): 404 or failed request")
    for c in errors:
        print(f"  {label(c)}: {result.failures[c.name]}")
    print(f"EMPTY ({len(empty)}): board exists but has zero jobs (abandoned or moved?)")
    for c in empty:
        print(f"  {label(c)}")
    print(f"OK ({len(ok)}):")
    for c in sorted(ok, key=lambda c: -result.counts[c.name]):
        print(f"  {label(c)}: {result.counts[c.name]} jobs")
    return 1 if errors else 0


# --- tracking -----------------------------------------------------------------------


def journal_for(args: argparse.Namespace) -> Journal:
    """Pending local changes live next to the DB they were made in."""
    return Journal(args.db.parent / "pending-changes.jsonl")


def data_branch(args: argparse.Namespace, db_path: Path | None = None) -> DataBranch:
    return DataBranch(db_path or args.db, repo=args.repo)


def _pending_hint(args: argparse.Namespace) -> None:
    n = len(journal_for(args).entries())
    if n:
        print(f"\n{n} local change(s) not synced yet: run `jobbot sync`")


def cmd_list(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        print_jobs(store.list_jobs(args.status, matched_only=not args.all), show_url=args.urls)
    _pending_hint(args)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print("no local database yet: run `jobbot sync` first")
        return 1
    with Storage(args.db) as store:
        updated = store.set_status(args.id, args.status, note=args.note)
        if updated is None:
            print(f"no job with id {args.id}")
            return 1
        store.commit()
    journal_for(args).append(updated.job.source, updated.job.source_job_id, args.status, args.note)
    print_jobs([updated])
    _pending_hint(args)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Pull the latest DB from the data branch, replay my local changes, push."""
    branch = data_branch(args)
    if args.seed:
        if not args.db.exists():
            print(f"error: nothing to seed; {args.db} doesn't exist")
            return 1
        if branch.exists():
            print("error: data branch already exists; not overwriting it")
            return 1
        with Storage(args.db) as store:  # migrates to the current schema if needed
            store.commit()
        # The empty lease is a backstop: the push fails if the branch appeared meanwhile.
        if not branch.push(expected=None, message="Seed jobs.db"):
            print("error: data branch already exists; not overwriting it")
            return 1
        print("seeded the data branch from the local database")
        return 0

    journal = journal_for(args)
    entries = journal.entries()
    warnings: list[str] = []

    def apply(path: Path, attempt: int) -> None:
        with Storage(path) as store:
            warnings[:] = replay(store, entries)
            store.commit()

    try:
        result = branch.publish(
            apply, message=f"Sync {len(entries)} status change(s)", require_existing=True
        )
    except (GitError, PushConflict) as exc:
        print(f"error: {exc}")
        return 1
    journal.clear()
    for w in warnings:
        print(f"warning: {w}")
    if entries:
        print(f"pushed {len(entries)} change(s) ({result.outcome}, attempt {result.attempts})")
    else:
        print("up to date with the data branch")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        stats = store.stats()
        counts = store.counts()
    print(f"applications this week: {stats.applied_this_week}")
    print("by status: " + ", ".join(f"{k} {v}" for k, v in stats.by_status.items()))
    print(
        f"stored: {counts['jobs']} jobs, {counts['matched']} matched, "
        f"{counts['with_description']} with descriptions"
    )
    if stats.by_company:
        print("\nresponse rate by company (responded / applied):")
        for company, applied, responded in stats.by_company:
            print(f"  {company:<16} {responded}/{applied}  ({responded / applied:.0%})")
    _pending_hint(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch, filter, store, and send the digest")
    run.add_argument(
        "--dry-run", action="store_true", help="print the digest instead of sending; save nothing"
    )
    run.add_argument("--html", type=Path, help="also write the digest HTML to this file")
    run.add_argument(
        "--refilter", action="store_true", help="re-apply current filters to every stored job"
    )
    run.add_argument(
        "--sync", action="store_true", help="read and publish jobs.db via the data branch"
    )
    run.set_defaults(func=cmd_run)

    dg = sub.add_parser("digest", help="send everything pending now (hit list on top)")
    dg.add_argument("--dry-run", action="store_true", help="print instead of sending")
    dg.add_argument("--html", type=Path, help="also write the digest HTML to this file")
    dg.add_argument("--sync", action="store_true", help="read and publish via the data branch")
    dg.set_defaults(func=cmd_digest)

    verify = sub.add_parser(
        "verify-companies", help="check every enabled board: error / empty / OK (read-only)"
    )
    verify.set_defaults(func=cmd_verify_companies)

    statuses = [s.value for s in Status]
    ls = sub.add_parser("list", help="list stored jobs (matched only, unless --all)")
    ls.add_argument("--status", choices=statuses, help="only jobs with this status")
    ls.add_argument("--urls", action="store_true", help="show apply links")
    ls.add_argument("--all", action="store_true", help="include jobs that didn't match")
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="set a job's status, e.g. `jobbot status 12 applied`")
    st.add_argument("id", type=int)
    st.add_argument("status", choices=statuses)
    st.add_argument("--note", help="replace the job's note")
    st.set_defaults(func=cmd_status)

    sy = sub.add_parser("sync", help="pull the data branch, replay local changes, push")
    sy.add_argument(
        "--seed", action="store_true", help="one-time: create the data branch from the local DB"
    )
    sy.set_defaults(func=cmd_sync)

    stats = sub.add_parser("stats", help="applications this week, statuses, response rates")
    stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
