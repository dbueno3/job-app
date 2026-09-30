import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from urllib.parse import quote

from jobbot.models import utcnow
from jobbot.storage import StoredJob

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS
SMTP_TIMEOUT = 30


class DigestConfigError(Exception):
    pass


@dataclass
class SmtpConfig:
    user: str
    password: str
    to: str

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        """SMTP_USER, SMTP_APP_PASSWORD, and optional DIGEST_TO (defaults to SMTP_USER)."""
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_APP_PASSWORD", "").replace(" ", "")
        if not user or not password:
            raise DigestConfigError(
                "SMTP_USER and SMTP_APP_PASSWORD must be set to send email "
                "(or use --dry-run). See https://myaccount.google.com/apppasswords"
            )
        to = os.environ.get("DIGEST_TO", "").strip() or user
        return cls(user=user, password=password, to=to)


@dataclass
class Digest:
    subject: str
    html: str
    text: str


def recruiter_search_url(company: str) -> str:
    return (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={quote(company)}%20technical%20recruiter"
    )


def format_yoe(min_yoe: float | None) -> str:
    return "not stated" if min_yoe is None else f"{min_yoe:g}+ yrs"


_VARIANT_SPLIT = re.compile(r"\s+[-–—]\s+")


def split_title(title: str) -> tuple[str, str | None]:
    """("Software Engineer, Agent", "Retail") from "Software Engineer, Agent - Retail"."""
    parts = _VARIANT_SPLIT.split(title, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (title.strip(), None)


@dataclass
class Row:
    """One digest row: a single job, or same-company variants of one base title."""

    jobs: list[StoredJob]

    @property
    def lead(self) -> StoredJob:
        return self.jobs[0]

    @property
    def title(self) -> str:
        return split_title(self.lead.job.title)[0] if len(self.jobs) > 1 else self.lead.job.title

    @property
    def variants(self) -> list[tuple[str, StoredJob]]:
        return [(split_title(s.job.title)[1] or s.job.title, s) for s in self.jobs]

    @property
    def min_yoe(self) -> float | None:
        stated = [s.min_yoe for s in self.jobs if s.min_yoe is not None]
        return min(stated) if stated else None


def group_rows(jobs: list[StoredJob]) -> list[Row]:
    """Group same-company roles sharing a base title; keeps the input (score) order."""
    rows: dict[tuple[str, str], Row] = {}
    for s in jobs:
        base, variant = split_title(s.job.title)
        key = (s.job.company.lower(), (base if variant else s.job.title).lower())
        if key in rows:
            rows[key].jobs.append(s)
        else:
            rows[key] = Row([s])
    return list(rows.values())


def build_digest(jobs: list[StoredJob], now: datetime | None = None) -> Digest:
    """Render new matches (already sorted by score, then recency) as HTML + plain text."""
    now = now or utcnow()
    n = len(jobs)
    subject = f"jobbot: {n} new SWE match{'es' if n != 1 else ''} ({now:%b %-d})"
    rows = group_rows(jobs)
    return Digest(subject=subject, html=_html(rows, subject), text=_text(rows))


def _ids(row: Row) -> str:
    return ", ".join(str(s.id) for s in row.jobs)


def _html(rows: list[Row], subject: str) -> str:
    cell = "padding:8px 10px;border-bottom:1px solid #e5e5e5;vertical-align:top;"
    sub = "color:#666;font-size:12px;margin-top:2px;"
    head = f"{cell}text-align:left;background:#f5f5f5;font-weight:600;"
    out = []
    for row in rows:
        s = row.lead
        j = s.job
        posted = j.posted_at.strftime("%b %-d") if j.posted_at else "—"
        title = f'<a href="{escape(j.url)}">{escape(row.title)}</a>'
        if len(row.jobs) > 1:
            links = " · ".join(
                f'<a href="{escape(v.job.url)}">{escape(name)}</a>' for name, v in row.variants
            )
            title += (
                f' <span style="color:#666;">({len(row.jobs)} variants)</span>'
                f'<div style="{sub}">{links}</div>'
            )
        out.append(
            "<tr>"
            f'<td style="{cell}text-align:right;color:#888;">{_ids(row)}</td>'
            f'<td style="{cell}"><strong>{escape(j.company)}</strong></td>'
            f'<td style="{cell}">{title}'
            f'<div style="{sub}">{escape(j.location)}</div></td>'
            f'<td style="{cell}text-align:center;">{max(v.score for v in row.jobs)}</td>'
            f'<td style="{cell}white-space:nowrap;">{escape(format_yoe(row.min_yoe))}</td>'
            f'<td style="{cell}white-space:nowrap;">{posted}</td>'
            f'<td style="{cell}white-space:nowrap;">'
            f'<a href="{escape(j.url)}">Apply</a> · '
            f'<a href="{escape(recruiter_search_url(j.company))}">Recruiters</a></td>'
            "</tr>"
        )
    headers = "".join(
        f'<th style="{head}">{h}</th>'
        for h in ("#", "Company", "Role", "Score", "YOE", "Posted", "Links")
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;color:#222;">'
        f'<h2 style="font-size:18px;margin:0 0 12px;">{escape(subject)}</h2>'
        '<table style="border-collapse:collapse;width:100%;">'
        f"<thead><tr>{headers}</tr></thead><tbody>{''.join(out)}</tbody></table>"
        '<p style="color:#888;font-size:12px;margin-top:16px;">'
        "Track one: <code>jobbot status &lt;#&gt; applied</code>, then <code>jobbot sync</code></p>"
        "</div>"
    )


def _text(rows: list[Row]) -> str:
    lines = []
    for row in rows:
        s = row.lead
        j = s.job
        count = f" ({len(row.jobs)} variants)" if len(row.jobs) > 1 else ""
        lines += [
            f"[{_ids(row)}] {j.company} — {row.title}{count}",
            f"     {j.location} | score {max(v.score for v in row.jobs)} | "
            f"YOE {format_yoe(row.min_yoe)}",
        ]
        if len(row.jobs) > 1:
            lines += [f"     - {name}: {v.job.url}" for name, v in row.variants]
        else:
            lines.append(f"     apply:      {j.url}")
        lines += [f"     recruiters: {recruiter_search_url(j.company)}", ""]
    lines.append("Track one: jobbot status <#> applied, then jobbot sync")
    return "\n".join(lines)


def send_digest(digest: Digest, cfg: SmtpConfig) -> None:
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = cfg.user
    msg["To"] = cfg.to
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)
